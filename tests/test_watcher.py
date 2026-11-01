import ctypes
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import notice, processes, watcher
from granola_autostart.calendar_cache import FeedLoad
from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch, Prompt, Snapshot, WatcherState
from granola_autostart.settings import Settings, config_path, save_settings
from granola_autostart.state_store import load_state, save_state
from granola_autostart.status import LastStart, Problem, TrayView, end_of_today

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
WORK = "https://calendar.google.com/calendar/ical/me/private-abc/basic.ics"
HOME = "https://calendar.example/home.ics"


def prompt(title: str = "Roadmap review") -> Prompt:
    return Prompt(kind="zoom", title=title, payload={}, keys=("zoom:604",), expires_at=NOW + timedelta(hours=1))


class Calendars:
    """CalendarCache, serving the world's events and recording how often it was told to refresh."""

    def __init__(self, world):
        self.world = world
        self.asked = []

    def current(self, now, settings, *, refresh, eager):
        self.asked.append((refresh, eager))
        return self.world.events

    def invalidate(self, urls):
        pass


@pytest.fixture
def world(monkeypatch):
    """What run_loop sees each tick, all faked: the user, Zoom, Granola, and the calendars.

    ``present`` is a list, one answer per tick. evaluate() records each Snapshot and answers with
    ``prompts``; handle_prompt() records the prompts that would have shown a card."""
    world = SimpleNamespace(present=[True], zoom=(None, None), title=None, granola=False, events=[], prompts=[], snapshots=[], cards=[], reports=[])
    world.calendars = Calendars(world)
    monkeypatch.setattr(watcher, "CalendarCache", lambda: world.calendars)
    monkeypatch.setattr(watcher, "process_table", lambda: {"zoom.exe": [300]})
    monkeypatch.setattr(watcher, "zoom_session", lambda table: world.zoom)
    monkeypatch.setattr(watcher, "is_present", lambda limit: world.present.pop(0))
    monkeypatch.setattr(watcher, "granola_running", lambda table: world.granola)
    monkeypatch.setattr(watcher, "zoom_window_title", lambda table: world.title)

    def evaluate(now, snapshot, state, *, lookahead, fresh_zoom_seconds):
        world.snapshots.append(snapshot)
        return world.prompts.pop(0) if world.prompts else None

    monkeypatch.setattr(watcher, "evaluate", evaluate)
    monkeypatch.setattr(watcher, "handle_prompt", lambda prompt, settings, control, lock, state: world.cards.append(prompt.title))
    return world


class Reporter:
    def __init__(self, world, broken: bool = False):
        self.world = world
        self.broken = broken

    def tick(self, now, settings, cache, events, launch):
        self.world.reports.append(events)
        if self.broken:
            raise OSError("status.json is open in another program")


def run_loop(world, *, ticks: int = 1, settings: Settings | None = None, reporter=None, between=None) -> list[float]:
    """Runs the watcher loop for ``ticks`` ticks; ``between`` runs after each one. Returns each tick's sleep."""
    settings = settings or Settings()
    control = watcher.Control(settings)
    slept = []

    def sleep(seconds):
        slept.append(seconds)
        if between is not None:
            between()
        if len(slept) >= ticks:
            control.halt()

    control.sleep = sleep
    watcher.run_loop(settings, control, threading.Lock(), WatcherState(), reporter or Reporter(world))
    return slept


def test_each_tick_hands_what_it_sees_to_the_decision_and_its_prompt_to_a_card(world):
    standup = CalendarEvent("s", "Standup", NOW, NOW + timedelta(minutes=30))
    world.zoom, world.title, world.events = ("604:1761670800", 12.0), "Roadmap review", [standup]
    world.prompts = [prompt()]
    run_loop(world)
    assert world.snapshots == [
        Snapshot(
            present=True,
            paused=False,
            zoom_session_id="604:1761670800",
            zoom_age_seconds=12.0,
            zoom_title="Roadmap review",
            events=[standup],
            granola_running=False,
        )
    ]
    assert world.cards == ["Roadmap review"] and world.reports == [[standup]]
    assert load_state()[0] == WatcherState()  # saved each tick


def test_zoom_s_windows_are_only_read_during_a_zoom_meeting(world, monkeypatch):
    monkeypatch.setattr(watcher, "zoom_window_title", lambda table: pytest.fail("read Zoom's windows outside a meeting"))
    run_loop(world)
    assert world.snapshots[0].zoom_title is None


def test_calendars_refresh_only_while_someone_is_there_and_at_once_when_they_return(world):
    world.present = [False, True, True]
    run_loop(world, ticks=3)
    assert world.calendars.asked == [(False, False), (True, True), (True, False)]


def test_a_tick_that_fails_is_logged_and_the_next_one_still_runs(world, monkeypatch, caplog):
    tables = [RuntimeError("Toolhelp snapshot failed"), {"zoom.exe": [300]}]

    def process_table():
        table = tables.pop(0)
        if isinstance(table, Exception):
            raise table
        return table

    monkeypatch.setattr(watcher, "process_table", process_table)
    world.present = [True]
    assert len(run_loop(world, ticks=2)) == 2
    assert len(world.snapshots) == 1 and "watcher tick failed" in caplog.text


def test_a_status_that_cannot_be_written_still_shows_the_card(world, caplog):
    world.prompts = [prompt()]
    run_loop(world, reporter=Reporter(world, broken=True))
    assert world.cards == ["Roadmap review"] and "could not report the status" in caplog.text


def test_quitting_while_a_tick_runs_shows_no_card(world, monkeypatch):
    world.prompts = [prompt()]
    settings = Settings()
    control = watcher.Control(settings)
    control.sleep = lambda seconds: None

    class Quitting:
        def tick(self, *_args):
            control.halt()

    watcher.run_loop(settings, control, threading.Lock(), WatcherState(), Quitting())
    assert world.cards == []


def test_settings_saved_between_ticks_are_used_from_the_next_one(world, caplog):
    world.present = [True, True]
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        slept = run_loop(world, ticks=2, between=lambda: save_settings(Settings(poll_seconds=7, setup_complete=True)))
    assert slept == [Settings().poll_seconds, 7]
    assert "No calendar feed configured" in caplog.text and "settings reloaded" in caplog.text


def test_the_config_s_stamp_changes_when_it_is_saved():
    assert watcher.config_stamp() is None
    save_settings(Settings())
    stamp = watcher.config_stamp()
    assert stamp == (config_path().stat().st_mtime_ns, config_path().stat().st_size)


def test_a_tray_change_wakes_the_watcher_at_once():
    control = watcher.Control()
    control.set_pause(None)
    started = time.monotonic()
    control.sleep(30)
    assert time.monotonic() - started < 10 and not control.wake.is_set()


def test_the_tray_is_redrawn_only_when_what_it_shows_changes():
    control = watcher.Control()
    drawn = []
    control.on_view = drawn.append
    control.show(control.view)
    paused = TrayView("paused", "Paused until 18:00", True)
    control.show(paused)
    control.show(TrayView("paused", "Paused until 18:00", True))
    assert drawn == [paused]


def test_a_start_still_waiting_for_granola_is_neither_opened_nor_failed():
    control = watcher.Control()
    control.record_start("Design review", NOW)
    waiting = ActiveLaunch("Design review", {}, ("event:abc",), NOW + timedelta(minutes=30), seen_running=False, launched_at=NOW)
    assert control.note_launch(waiting) == LastStart("Design review", NOW)
    assert watcher.Control().note_launch(waiting) is None


def test_the_tray_menu_follows_the_dark_theme_where_windows_allows_it(monkeypatch):
    calls = []

    class Uxtheme:
        def __getitem__(self, ordinal):
            return lambda *args: calls.append((ordinal, args))

    def no_ordinals(name):
        raise OSError("uxtheme ordinal 135 not found")

    monkeypatch.setattr(watcher, "per_monitor_dpi", lambda: calls.append("dpi"))
    monkeypatch.setattr(watcher, "ctypes", SimpleNamespace(WinDLL=lambda name: Uxtheme(), c_int=ctypes.c_int))
    watcher.modern_menus()
    assert calls == ["dpi", (135, (1,)), (136, ())]  # AllowDark, then FlushMenuThemes
    monkeypatch.setattr(watcher, "ctypes", SimpleNamespace(WinDLL=no_ordinals))
    watcher.modern_menus()  # an older Windows keeps light menus


class Icon:
    """pystray's icon: run() hands it to the test's ``use`` instead of showing it."""

    def __init__(self, tray, name, image, title, menu):
        self.tray, self.menu, self.title = tray, menu, title
        self.balloons = []
        self.stopped = False

    def run(self, setup):
        self.tray.use(self)

    def stop(self):
        self.stopped = True

    def notify(self, message, title):
        self.balloons.append((title, message))

    def item(self, text):
        return next(item for item in self.menu.items if item.text == text)

    def click(self, text):
        self.item(text)(self)

    def shown(self) -> list[str]:
        return [item.text for item in self.menu]


@pytest.fixture
def tray(monkeypatch, spawned):
    """run_tray with a fake icon whose ``use`` the test sets, and a watcher loop that only waits for the stop."""
    tray = SimpleNamespace(use=lambda icon: None, controls=[], notifiers=[], opened=[], spawned=spawned)

    class Notifier:
        def __init__(self, on_action, fallback, logo=None):
            self.on_action, self.balloon, self.logo = on_action, fallback, logo
            fallback("Too early", "There is no icon yet to show this")  # dropped, not raised
            tray.notifiers.append(self)

    def reporter(control, notifier):
        tray.controls.append(control)
        return None

    monkeypatch.setattr(watcher, "modern_menus", lambda: None)
    monkeypatch.setattr(watcher, "register_app_id", lambda logo: None)
    monkeypatch.setattr(watcher, "Notifier", Notifier)
    monkeypatch.setattr(watcher, "Reporter", reporter)
    monkeypatch.setattr(watcher, "run_loop", lambda settings, control, *_args: control.stop.wait(10))
    monkeypatch.setattr(watcher.pystray, "Icon", lambda *args, **kwargs: Icon(tray, *args, **kwargs))
    monkeypatch.setattr(processes, "open_log_folder", lambda: tray.opened.append("logs"))
    return tray


def test_pausing_from_the_tray_is_saved_and_resuming_clears_it(tray):
    pauses = []

    def use(icon):
        control = tray.controls[0]
        before = datetime.now().astimezone()
        icon.click("Pause for 1 hour")
        assert before + timedelta(hours=1) <= control.current_pause() <= datetime.now().astimezone() + timedelta(hours=1)
        pauses.append(load_state()[1])
        today = end_of_today(datetime.now().astimezone())
        icon.click("Pause for today")
        pauses.append(load_state()[1])
        assert pauses[-1] == today
        icon.click("Resume")
        pauses.append(load_state()[1])

    tray.use = use
    assert watcher.run_tray(Settings(setup_complete=True)) == 0
    assert pauses[0] is not None and pauses[2] is None


def test_the_menu_offers_fix_in_settings_while_something_is_wrong_and_resume_while_paused(tray):
    def use(icon):
        control = tray.controls[0]
        assert icon.shown()[0] == "Watching for meetings" and "Settings…" in icon.shown()
        assert "Fix in Settings…" not in icon.shown() and "Resume" not in icon.shown()
        icon.menu(icon)  # a left click on the icon
        control.show(TrayView("attention", "“Work” calendar isn't updating", False, "about"))
        assert "Fix in Settings…" in icon.shown() and "Settings…" not in icon.shown()
        icon.menu(icon)
        control.show(TrayView("paused", "Paused until tomorrow", True))
        assert "Resume" in icon.shown() and "Pause for 1 hour" not in icon.shown()
        icon.click("About")
        icon.click("Open log folder")
        icon.click("Quit")

    tray.use = use
    assert watcher.run_tray(Settings(setup_complete=True)) == processes.QUIT_EXIT_CODE
    assert [command[-1] for command in tray.spawned] == ["--section=calendars", "--section=about", "--section=about"]
    assert tray.opened == ["logs"]


def test_notification_buttons_retry_open_the_logs_the_config_or_settings(tray, monkeypatch):
    retried = []
    monkeypatch.setattr(watcher, "retry_launch", lambda lock, state, control, key, *, dry_run: retried.append((key, dry_run)))
    problem = Problem("launch:abc", "Granola didn't open", "", "about")

    def use(icon):
        notifier = tray.notifiers[0]
        for action in ("retry", "logs", "config", "settings", "open", "something new"):
            notifier.on_action(action, problem)
        notifier.balloon("Calendar isn't updating", "The last 3 downloads failed.")
        assert icon.balloons == [("Calendar isn't updating", "The last 3 downloads failed.")]

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True, dry_run=True))
    assert retried == [("launch:abc", True)] and tray.opened == ["logs"]
    assert [command[-1] for command in tray.spawned] == [str(config_path()), "--section=about", "--section=about"]


def test_the_first_run_opens_setup_and_later_runs_do_not(tray):
    watcher.run_tray(Settings())
    watcher.run_tray(Settings(setup_complete=True))
    watcher.run_tray(Settings(), "config.json is not valid JSON")  # Settings shows the error instead
    assert [command[-1] for command in tray.spawned] == ["--setup"]


def test_a_pause_from_before_a_restart_is_kept(tray):
    until = datetime.now().astimezone() + timedelta(hours=1)
    save_state(WatcherState(), until)
    watcher.run_tray(Settings(setup_complete=True))
    assert tray.controls[0].current_pause() == until


def test_notifications_still_work_without_the_app_s_registration(tray, monkeypatch, caplog):
    def no_registry(logo):
        raise PermissionError("access denied")

    monkeypatch.setattr(watcher, "register_app_id", no_registry)
    watcher.run_tray(Settings(setup_complete=True))
    assert tray.notifiers[0].logo is not None and "could not register for notifications" in caplog.text


def test_check_prints_what_the_watcher_would_see(monkeypatch, capsys):
    now = datetime.now().astimezone()
    standup = CalendarEvent("s", "Standup", now + timedelta(hours=1), now + timedelta(hours=2))
    lunch = CalendarEvent("l", "Lunch", now + timedelta(hours=3), now + timedelta(hours=4), skipped=True)
    over = CalendarEvent("o", "Yesterday's retro", now - timedelta(days=1), now - timedelta(days=1, minutes=-30))
    monkeypatch.setattr(watcher, "is_present", lambda limit: True)
    monkeypatch.setattr(watcher, "idle_seconds", lambda: 12.4)
    monkeypatch.setattr(watcher, "zoom_session", lambda: ("604:1761670800", 95.0))
    monkeypatch.setattr(watcher, "granola_running", lambda: False)
    monkeypatch.setattr(watcher, "load_feeds", lambda urls, now, settings, owners: FeedLoad({WORK: [over, lunch, standup]}, failures=[(2, HOME, TimeoutError())]))
    watcher.print_check(Settings(ics_urls=(WORK, HOME)), "config.json is not valid JSON")
    out = capsys.readouterr().out.splitlines()
    assert out[:5] == [
        "config: config.json is not valid JSON (using defaults)",
        "present: True (idle 12s, limit 300s)",
        "zoom: in meeting 604:1761670800",
        "zoom age: 95s",
        "granola running: False",
    ]
    assert out[5:7] == ["calendar feed 2: failed (timed out)", "calendar:"]
    assert out[7].endswith("  Standup") and out[8].endswith("  Lunch  (skipped)") and len(out) == 9
    assert WORK not in "\n".join(out) and HOME not in "\n".join(out)


def test_check_says_when_there_is_no_calendar_or_nothing_coming_up(monkeypatch, capsys):
    monkeypatch.setattr(watcher, "is_present", lambda limit: False)
    monkeypatch.setattr(watcher, "idle_seconds", lambda: 900.0)
    monkeypatch.setattr(watcher, "zoom_session", lambda: (None, None))
    monkeypatch.setattr(watcher, "granola_running", lambda: True)
    monkeypatch.setattr(watcher, "load_feeds", lambda urls, now, settings, owners: FeedLoad({WORK: []}))
    watcher.print_check(Settings(), None)
    watcher.print_check(Settings(ics_urls=(WORK,)), None)
    out = capsys.readouterr().out.splitlines()
    assert "zoom: not in a meeting" in out and not any(line.startswith("zoom age") for line in out)
    assert "calendar: no calendar feed in config" in out and out[-1] == "calendar: no upcoming events in the feed"


@pytest.fixture
def cli(monkeypatch):
    """watcher.main for a command line, with every mode's real work recorded instead of done."""
    calls = []
    monkeypatch.setattr(watcher, "setup_logging", lambda name: calls.append(("log", name)) or logging.getLogger("granola_autostart"))
    monkeypatch.setattr(processes, "hold_mutex", lambda name: True)
    for mode in ("install", "uninstall", "stop"):
        monkeypatch.setattr(processes, mode, lambda mode=mode: calls.append(mode))
    monkeypatch.setattr(watcher, "print_check", lambda settings, error: calls.append(("check", error is not None)))
    monkeypatch.setattr(watcher, "run_tray", lambda settings, error, *, dry_run: calls.append(("tray", settings.dry_run, dry_run)) or 0)

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", *argv])
        return watcher.main()

    return calls, run


@pytest.mark.parametrize("mode", ["install", "uninstall", "stop"])
def test_install_uninstall_and_stop_do_their_one_job_and_log_to_the_cli_log(mode, cli):
    calls, run = cli
    assert run(f"--{mode}") == 0
    assert calls == [("log", "cli-log.txt"), mode]


def test_check_fails_while_the_config_is_broken(cli):
    calls, run = cli
    assert run("--check") == 0
    config_path().write_text("{ broken", encoding="utf-8")
    assert run("--check") == 1
    assert [call for call in calls if call[0] == "check"] == [("check", False), ("check", True)]


def test_with_no_mode_the_watcher_runs_and_dry_run_reaches_its_settings(cli):
    calls, run = cli
    assert run() == 0
    assert run("--dry-run") == 0
    assert [call for call in calls if call[0] != "log"] == [("tray", False, False), ("tray", True, True)]
    assert ("log", "log.txt") in calls


def test_a_second_watcher_leaves_the_first_one_running(cli, monkeypatch, capsys):
    calls, run = cli
    monkeypatch.setattr(processes, "hold_mutex", lambda name: False)
    assert run() == 0
    assert calls == [] and "another watcher is already running" in capsys.readouterr().out


@pytest.mark.parametrize(("started", "said"), [(True, "would start"), (False, "cancelled")])
def test_preview_shows_the_card_once_and_says_what_it_would_do(started, said, cli, monkeypatch, capsys):
    _calls, run = cli
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds: started)
    assert run("--preview-notice") == 0
    assert capsys.readouterr().out.strip() == said


def test_only_one_mode_at_a_time(cli):
    _calls, run = cli
    with pytest.raises(SystemExit):
        run("--install", "--stop")
