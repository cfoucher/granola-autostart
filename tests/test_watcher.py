import ctypes
import dataclasses
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import calendar_cache, decide, notice, processes, updates, watcher
from granola_autostart.calendar_cache import FeedLoad
from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.calls import Call
from granola_autostart.decide import ActiveLaunch, Prompt, Snapshot, WatcherState
from granola_autostart.settings import (
    Calendar,
    Settings,
    config_path,
    load_settings,
    save_settings,
    status_path,
    update_config,
    update_path,
    write_json,
)
from granola_autostart.state_store import load_state, save_state
from granola_autostart.status import FeedStatus, LastStart, Problem, Status, TrayView, end_of_today, feed_id, read_status, write_status

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
WORK = "https://calendar.google.com/calendar/ical/me/private-abc/basic.ics"
HOME = "https://calendar.example/home.ics"


def prompt(title: str = "Roadmap review") -> Prompt:
    return Prompt(kind="call", title=title, payload={}, keys=("zoom:604",), expires_at=NOW + timedelta(hours=1))


class Watch:
    """CallWatch, answering with the world's call and recording which providers it was asked about."""

    def __init__(self, world):
        self.world = world

    def current(self, enabled, table):
        self.world.asked.append((dict(enabled), table))
        return self.world.call

    def calls(self, enabled, table=None):
        return [self.world.call] if self.world.call else []


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
    """What run_loop sees each tick, all faked: the user, the call, Granola, and the calendars.

    ``present`` is a list, one answer per tick. evaluate() records each Snapshot and what else it was
    given, and answers with ``prompts``; handle_prompt() records the prompts that would have shown a card."""
    world = SimpleNamespace(
        present=[True], call=None, granola=False, events=[], prompts=[], snapshots=[], cards=[], reports=[], asked=[], evaluated=[]
    )
    world.calendars = Calendars(world)
    world.watch = Watch(world)
    monkeypatch.setattr(watcher, "CalendarCache", lambda: world.calendars)
    monkeypatch.setattr(watcher, "CallWatch", lambda: world.watch)
    monkeypatch.setattr(watcher, "process_table", lambda: {"zoom.exe": [300]})
    monkeypatch.setattr(watcher, "is_present", lambda limit: world.present.pop(0))
    monkeypatch.setattr(watcher, "granola_running", lambda table: world.granola)

    def evaluate(now, snapshot, state, *, lookahead, fresh_call_seconds, skip_words, lead):
        world.snapshots.append(snapshot)
        world.evaluated.append((lookahead, fresh_call_seconds, skip_words, lead))
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


def run_loop(world, *, ticks: int = 1, settings: Settings | None = None, reporter=None, between=None, control=None) -> list[float]:
    """Runs the watcher loop for ``ticks`` ticks; ``between`` runs after each one. Returns each tick's sleep."""
    settings = settings or Settings()
    control = control or watcher.Control(settings)
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
    world.call, world.events = Call("zoom", "zoom:604:1761670800", 12.0, "Roadmap review"), [standup]
    world.prompts = [prompt()]
    run_loop(world)
    assert world.snapshots == [
        Snapshot(
            present=True,
            paused=False,
            call_session_id="zoom:604:1761670800",
            call_provider="zoom",
            call_age_seconds=12.0,
            call_title="Roadmap review",
            events=[standup],
            granola_running=False,
        )
    ]
    assert world.cards == ["Roadmap review"] and world.reports == [[standup]]
    assert load_state()[0] == WatcherState()  # saved each tick


def test_the_loop_sweeps_the_leftovers_of_unfinished_saves_once_at_the_start(world, monkeypatch):
    swept = []
    monkeypatch.setattr(watcher, "sweep_stale_temporaries", lambda: swept.append(len(world.snapshots)))
    world.present = [True] * 3
    run_loop(world, ticks=3)
    assert swept == [0]  # before the first tick, and not again


def test_a_tick_with_no_call_has_a_snapshot_with_none(world):
    run_loop(world)
    snapshot = world.snapshots[0]
    assert (snapshot.call_session_id, snapshot.call_provider, snapshot.call_age_seconds, snapshot.call_title) == (None, None, None, None)


def test_each_tick_asks_for_the_calls_that_are_switched_on_and_gives_the_decision_the_skip_words_and_the_countdown_as_its_lead(world):
    settings = Settings(
        calls={"zoom": False, "teams": True, "meet": True},
        ignore_title_substrings=("lunch",),
        zoom_lookahead_minutes=4,
        fresh_zoom_seconds=33,
        countdown_seconds=12,
    )
    run_loop(world, settings=settings)
    assert world.asked == [({"zoom": False, "teams": True, "meet": True}, {"zoom.exe": [300]})]
    assert world.evaluated == [(timedelta(minutes=4), 33, ("lunch",), timedelta(seconds=12))]


def test_switching_a_call_on_in_settings_takes_effect_from_the_next_tick(world):
    world.present = [True, True]
    teams_too = Settings(calls={"zoom": True, "teams": True, "meet": False}, setup_complete=True)
    run_loop(world, ticks=2, between=lambda: save_settings(teams_too))
    assert [enabled for enabled, _table in world.asked] == [
        {"zoom": True, "teams": False, "meet": False},
        {"zoom": True, "teams": True, "meet": False},
    ]


def test_one_call_watch_serves_the_whole_loop(world, monkeypatch):
    made = []
    monkeypatch.setattr(watcher, "CallWatch", lambda: made.append(1) or world.watch)
    world.present = [True, True, True]
    run_loop(world, ticks=3)
    assert made == [1]


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
    assert "No calendar feed is on" in caplog.text and "settings reloaded" in caplog.text


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


def test_the_watcher_ticks_the_update_checker_each_tick_which_also_clears_out_old_installers(world, monkeypatch):
    calls = []

    class Checker:
        def tick(self, now):
            calls.append(now)

    monkeypatch.setattr(updates, "Checker", Checker)
    monkeypatch.setattr(updates, "clean_up", lambda: pytest.fail("the checker cleans up, on its own schedule"))
    world.present = [True, True]
    run_loop(world, ticks=2)
    assert len(calls) == 2 and calls[0].tzinfo is not None  # the loop's own wall-clock time


def test_a_start_still_waiting_for_granola_is_neither_opened_nor_failed():
    control = watcher.Control()
    control.record_start("Design review", NOW)
    waiting = ActiveLaunch("Design review", {}, ("event:abc",), NOW + timedelta(minutes=30), seen_running=False, launched_at=NOW)
    assert control.note_launch(waiting) == LastStart("Design review", NOW)
    assert watcher.Control().note_launch(waiting) is None


RELEASE = updates.Release(
    "1.2.0",
    "https://github.com/cfoucher/granola-autostart/releases/download/v1.2.0/GranolaAutostart-1.2.0-setup.exe",
    26,
    "0" * 64,
    "https://github.com/cfoucher/granola-autostart/releases/tag/v1.2.0",
)


@pytest.fixture
def update_ready(monkeypatch):
    """An installed app on 1.1.0, with 1.2.0 on offer in update.json."""
    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "__version__", "1.1.0")
    monkeypatch.setattr(updates, "INSTALL_WAIT_SECONDS", 0)  # an installer that started is not waited on, unless a test says
    write_json(update_path(), updates.UpdateState(latest=RELEASE).to_json())


class Feeds:
    def feed_statuses(self, urls):
        return []


class Notifier:
    def show(self, problem):
        pass


def test_each_tick_puts_an_update_in_the_tray_until_it_is_installed(update_ready, monkeypatch):
    control = watcher.Control()
    reporter = watcher.Reporter(control, Notifier())
    reporter.tick(NOW, Settings(), Feeds(), [], None)
    assert (control.view.update, control.view.header) == ("1.2.0", "Version 1.2.0 is available")
    control.installing = "1.2.0"
    reporter.tick(NOW, Settings(), Feeds(), [], None)
    assert control.view.header == "Installing version 1.2.0…"
    control.installing = None
    monkeypatch.setattr(updates, "__version__", "1.2.0")  # the app restarted on the new version
    reporter.tick(NOW, Settings(), Feeds(), [], None)
    assert control.view.update is None and control.view.header == "Watching for meetings"


def test_off_shows_in_the_tray_and_in_status_json_and_the_update_still_does(update_ready):
    control = watcher.Control(Settings(autostart_notes=False))
    watcher.Reporter(control, Notifier()).tick(NOW, control.settings, Feeds(), [], None)
    assert control.view == TrayView("off", "Off. Meetings and calls won't start notes", False, update="1.2.0")
    assert read_status(status_path()).off is True
    control.set_notes(True)
    watcher.Reporter(control, Notifier()).tick(NOW, control.settings, Feeds(), [], None)
    assert control.view.kind == "watching" and read_status(status_path()).off is False


def test_off_wins_over_a_pause_and_a_problem_in_the_tray():
    control = watcher.Control(Settings(autostart_notes=False))
    control.set_pause(NOW + timedelta(hours=1))
    control.config_error = "config.json is not valid JSON"
    watcher.Reporter(control, Notifier()).tick(NOW, control.settings, Feeds(), [], None)
    assert (control.view.kind, control.view.paused, control.view.problem_section) == ("off", False, None)


def test_a_source_checkout_shows_no_update_even_when_the_installed_app_found_one(update_ready, monkeypatch):
    monkeypatch.setattr(updates, "enabled", lambda: False)
    control = watcher.Control()
    watcher.Reporter(control, Notifier()).tick(NOW, Settings(), Feeds(), [], None)
    assert control.view.update is None and control.view.header == "Watching for meetings"


def test_while_off_nothing_is_downloaded_or_asked_and_the_decision_runs_as_if_paused_with_no_call(world):
    world.call = Call("zoom", "zoom:604:1761670800", 30.0, "Roadmap review")
    world.events = [CalendarEvent("s", "Standup", NOW, NOW + timedelta(minutes=30))]
    world.granola = True
    world.present = [True, True]
    run_loop(world, ticks=2, settings=Settings(autostart_notes=False))
    assert world.asked == [] and world.calendars.asked == []  # no call is checked and no calendar is downloaded
    # Granola running is cheap, so it is still looked at.
    off = Snapshot(
        present=True,
        paused=True,
        call_session_id=None,
        call_provider=None,
        call_age_seconds=None,
        call_title=None,
        events=[],
        granola_running=True,
    )
    assert world.snapshots == [off, off] and world.cards == []
    assert world.reports == [[], []]  # nothing to report from the calendars either


def test_turning_notes_off_takes_effect_on_the_next_tick_without_a_reload(world):
    world.present = [True, True]
    settings = Settings()
    control = watcher.Control(settings)
    run_loop(world, ticks=2, settings=settings, control=control, between=lambda: control.set_notes(False))
    assert [snapshot.paused for snapshot in world.snapshots] == [False, True]
    assert len(world.asked) == 1 and len(world.calendars.asked) == 1


def test_turning_notes_on_checks_the_calls_and_refreshes_the_calendars_at_once_but_asks_nothing_that_tick(world, monkeypatch):
    def real_decision(now, snapshot, state, **options):
        world.snapshots.append(snapshot)
        return decide.evaluate(now, snapshot, state, **options)

    monkeypatch.setattr(watcher, "evaluate", real_decision)
    now = datetime.now().astimezone()
    standup = CalendarEvent("s", "Standup", now - timedelta(minutes=5), now + timedelta(minutes=25))
    world.call, world.events = Call("zoom", "zoom:604:1761670800", 30.0, "Roadmap review"), [standup]
    world.present = [True, True, True]
    turn_on = [lambda: save_settings(Settings(setup_complete=True)), lambda: None, lambda: None]
    run_loop(world, ticks=3, settings=Settings(autostart_notes=False), between=lambda: turn_on.pop(0)())
    # Off: no call seen. On, the first tick: a pause with the call seen, so it is recorded as handled, not asked about.
    assert [(snapshot.paused, snapshot.call_session_id) for snapshot in world.snapshots] == [
        (True, None),
        (True, "zoom:604:1761670800"),
        (False, "zoom:604:1761670800"),
    ]
    assert world.calendars.asked == [(True, True), (True, False)]  # eager on the tick that turned it on
    assert "zoom:604:1761670800" in load_state()[0].handled
    # The meeting under way gets its card on the tick after, as it does after Resume; the call gets none.
    assert world.cards == ["Standup"]


def test_update_checks_go_on_while_off(world, monkeypatch):
    ticks = []

    class Checker:
        def tick(self, now):
            ticks.append(now)

    monkeypatch.setattr(updates, "Checker", Checker)
    world.present = [True, True]
    run_loop(world, ticks=2, settings=Settings(autostart_notes=False))
    assert len(ticks) == 2


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


@pytest.mark.parametrize(
    ("appearance", "mode"), [("auto", 1), ("dark", 2), ("light", 3), ("something this version doesn't know", 1)]
)
def test_the_tray_menu_takes_the_appearance_chosen_and_follows_windows_on_auto(appearance, mode, monkeypatch):
    calls = []

    class Uxtheme:
        def __getitem__(self, ordinal):
            return lambda *args: calls.append((ordinal, args))

    monkeypatch.setattr(watcher, "per_monitor_dpi", lambda: calls.append("dpi"))
    monkeypatch.setattr(watcher, "ctypes", SimpleNamespace(WinDLL=lambda name: Uxtheme(), c_int=ctypes.c_int))
    watcher.modern_menus(appearance)
    assert calls == ["dpi", (135, (mode,)), (136, ())]  # SetPreferredAppMode, then FlushMenuThemes
    calls.clear()
    watcher.menu_theme(appearance)  # on a reload there is no DPI to set again
    assert calls == [(135, (mode,)), (136, ())]


def test_a_reload_sets_the_tray_menu_theme_when_the_appearance_changed_and_not_otherwise(monkeypatch):
    modes = []
    monkeypatch.setattr(watcher, "menu_theme", modes.append)
    start = Settings(setup_complete=True)
    control = watcher.Control(start)
    cache = Calendars(SimpleNamespace(events=[]))
    save_settings(dataclasses.replace(start, appearance="dark"))
    dark = watcher.reload_settings(control, start, cache)
    assert modes == ["dark"] and dark.appearance == "dark"
    save_settings(dataclasses.replace(dark, countdown_seconds=15))
    watcher.reload_settings(control, dark, cache)
    assert modes == ["dark"]  # something else changed
    update_config("appearance", "auto")
    assert watcher.reload_settings(control, dark, cache).appearance == "auto"
    assert modes == ["dark", "auto"]


def test_a_broken_config_keeps_the_tray_menu_theme_it_had(monkeypatch):
    modes = []
    monkeypatch.setattr(watcher, "menu_theme", modes.append)
    start = Settings(setup_complete=True, appearance="dark")
    config_path().write_text("{ broken", encoding="utf-8")
    assert watcher.reload_settings(watcher.Control(start), start, Calendars(SimpleNamespace(events=[]))) is start
    assert modes == []


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
    tray = SimpleNamespace(use=lambda icon: None, controls=[], notifiers=[], opened=[], spawned=spawned, menu_modes=[])

    class Notifier:
        def __init__(self, on_action, fallback, logo=None):
            self.on_action, self.balloon, self.logo = on_action, fallback, logo
            fallback("Too early", "There is no icon yet to show this")  # dropped, not raised
            tray.notifiers.append(self)

    def reporter(control, notifier):
        tray.controls.append(control)
        return None

    monkeypatch.setattr(watcher, "modern_menus", tray.menu_modes.append)
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
        control.show(TrayView("attention", "“Work” calendar isn't updating", False, "general"))
        assert "Fix in Settings…" in icon.shown() and "Settings…" not in icon.shown()
        icon.menu(icon)
        control.show(TrayView("paused", "Paused until tomorrow", True))
        assert "Resume" in icon.shown() and "Pause for 1 hour" not in icon.shown()
        icon.click("About")
        icon.click("Open log folder")
        icon.click("Quit")

    tray.use = use
    assert watcher.run_tray(Settings(setup_complete=True)) == processes.QUIT_EXIT_CODE
    assert [command[-1] for command in tray.spawned] == ["--section=calendars", "--section=general", "--section=general"]
    assert tray.opened == ["logs"]


def test_the_menu_turns_notes_off_after_the_pause_items_and_offers_turn_on_instead_while_off(tray):
    def use(icon):
        control = tray.controls[0]
        shown = icon.shown()
        assert shown.index("Turn off") == shown.index("Pause for today") + 1 and "Turn on" not in shown
        control.show(TrayView("paused", "Paused until tomorrow", True))
        shown = icon.shown()
        assert shown.index("Turn off") == shown.index("Pause for today") + 1 and "Resume" in shown
        control.show(TrayView("attention", "“Work” calendar isn't updating", False, "calendars"))
        assert "Turn off" in icon.shown() and "Fix in Settings…" in icon.shown()
        control.set_notes(False)
        control.show(TrayView("off", "Off. Meetings and calls won't start notes", False))
        shown = icon.shown()
        assert shown[0] == "Off. Meetings and calls won't start notes" and "Turn on" in shown
        for hidden in ("Resume", "Pause for 1 hour", "Pause for today", "Turn off", "Fix in Settings…"):
            assert hidden not in shown
        assert "Settings…" in shown and icon.item("Settings…").default  # still what a left click opens
        assert {"Open log folder", "About", "Quit"} <= set(shown)

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))


def test_the_menu_label_follows_the_switch_at_once_and_not_the_view_the_next_tick_draws(tray):
    def use(icon):
        control = tray.controls[0]
        view = control.view
        assert "Turn off" in icon.shown() and "Turn on" not in icon.shown()
        icon.click("Turn off")
        assert control.view == view  # the watcher hasn't ticked yet
        shown = icon.shown()
        assert "Turn on" in shown and "Turn off" not in shown
        assert "Pause for today" not in shown and "Pause for 1 hour" not in shown
        icon.click("Turn on")
        shown = icon.shown()
        assert "Turn off" in shown and "Turn on" not in shown and "Pause for today" in shown

    save_settings(Settings(setup_complete=True))
    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))


def test_a_tray_that_starts_with_notes_off_offers_turn_on(tray):
    def use(icon):
        shown = icon.shown()
        assert "Turn on" in shown and "Turn off" not in shown

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True, autostart_notes=False))


def test_the_menu_still_offers_an_update_while_off(tray, update_ready):
    def use(icon):
        tray.controls[0].set_notes(False)
        tray.controls[0].show(TrayView("off", "Off. Meetings and calls won't start notes", False, update="1.2.0"))
        shown = icon.shown()
        assert shown[:2] == ["Off. Meetings and calls won't start notes", "Install update…"] and "Turn on" in shown

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))


def test_turn_off_and_turn_on_save_only_that_key_and_act_at_once(tray, caplog):
    caplog.set_level(logging.INFO, logger="granola_autostart")
    save_settings(Settings(setup_complete=True))
    config = json.loads(config_path().read_text(encoding="utf-8"))
    # Changed in Settings a moment ago: the watcher's copy of the settings hasn't caught up.
    config.update(countdown_seconds=20, a_key_from_the_future=1, ics_url="https://calendar.example/plain.ics")
    config_path().write_text(json.dumps(config), encoding="utf-8")
    saved = []

    def use(icon):
        control = tray.controls[0]
        assert control.settings.autostart_notes and not control.wake.is_set()
        icon.click("Turn off")
        assert control.settings.autostart_notes is False and control.wake.is_set()  # the loop is woken at once
        saved.append(json.loads(config_path().read_text(encoding="utf-8")))
        control.show(TrayView("off", "Off. Meetings and calls won't start notes", False))
        icon.click("Turn on")
        assert control.settings.autostart_notes is True
        saved.append(json.loads(config_path().read_text(encoding="utf-8")))

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))
    assert saved == [{**config, "autostart_notes": False}, {**config, "autostart_notes": True}]
    assert "Autostart notes turned off" in caplog.text and "Autostart notes turned on" in caplog.text


def test_notes_turned_off_in_the_tray_stay_off_through_a_reload_and_a_save_from_settings(tray):
    def use(icon):
        control = tray.controls[0]
        icon.click("Turn off")
        cache = Calendars(SimpleNamespace(events=[]))
        reloaded = watcher.reload_settings(control, control.settings, cache)
        assert reloaded.autostart_notes is False and control.settings.autostart_notes is False
        # Settings saves everything it loaded, which includes the switch.
        save_settings(dataclasses.replace(load_settings(), countdown_seconds=15))
        assert watcher.reload_settings(control, reloaded, cache).autostart_notes is False

    save_settings(Settings(setup_complete=True))
    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))
    assert load_settings().autostart_notes is False and load_settings().countdown_seconds == 15


def test_a_config_that_cannot_be_written_still_turns_notes_off_for_now_and_leaves_the_file_alone(tray, caplog):
    config_path().write_text("{ broken", encoding="utf-8")

    def use(icon):
        icon.click("Turn off")
        assert tray.controls[0].settings.autostart_notes is False

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True), "config.json is not valid JSON")
    assert config_path().read_text(encoding="utf-8") == "{ broken"
    assert "could not save Autostart notes to config.json; it is off until config.json changes or this app restarts" in caplog.text


def test_the_tray_starts_with_the_menu_theme_that_was_chosen(tray):
    watcher.run_tray(Settings(setup_complete=True, appearance="dark"))
    watcher.run_tray(Settings(setup_complete=True))
    assert tray.menu_modes == ["dark", "auto"]


def finish_installs() -> None:
    for thread in threading.enumerate():
        if thread.name == "update-install":
            thread.join(10)


def test_the_menu_offers_install_update_right_after_the_header_and_waits_for_a_card(tray, update_ready):
    def use(icon):
        control = tray.controls[0]
        assert "Install update…" not in icon.shown()
        control.show(TrayView("watching", "Version 1.2.0 is available", False, update="1.2.0"))
        assert icon.shown()[:2] == ["Version 1.2.0 is available", "Install update…"]
        assert icon.item("Install update…").enabled
        control.set_card(True)  # the watcher is waiting on a countdown card
        assert not icon.item("Install update after the countdown").enabled and "Install update…" not in icon.shown()
        control.set_card(False)
        assert icon.item("Install update…").enabled
        assert processes.hold_mutex(processes.CARD_MUTEX)  # a card another process opened, like Settings' Preview
        try:
            assert not icon.item("Install update after the countdown").enabled
        finally:
            processes.release_mutex(processes.CARD_MUTEX)
        control.set_installing("1.2.0")
        assert not icon.item("Install update…").enabled
        control.set_installing(None)
        control.show(TrayView("watching", "Watching for meetings", False))  # installed, or nothing to install
        assert "Install update…" not in icon.shown()

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))


def test_install_update_in_the_menu_installs_on_its_own_thread(tray, update_ready, monkeypatch):
    installed = []
    monkeypatch.setattr(updates, "install", lambda release: installed.append(release) or updates.Install.STARTED)

    def use(icon):
        tray.controls[0].show(TrayView("watching", "Version 1.2.0 is available", False, update="1.2.0"))
        icon.click("Install update…")
        finish_installs()

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True))
    assert installed == [RELEASE] and tray.spawned == []  # the installer started, so Settings stays shut


def test_the_tray_installs_once_and_says_so_while_it_downloads(update_ready, monkeypatch, spawned):
    control = watcher.Control()
    entered, finish = threading.Event(), threading.Event()
    seen = []

    def install(release):
        seen.append((release, control.installing))
        entered.set()
        finish.wait(10)
        return updates.Install.STARTED

    monkeypatch.setattr(updates, "install", install)
    thread = watcher.start_install(control)
    assert entered.wait(10) and control.installing == "1.2.0" and control.wake.is_set()
    assert watcher.start_install(control) is None  # a second click does nothing
    finish.set()
    thread.join(10)
    assert seen == [(RELEASE, "1.2.0")] and control.installing is None and spawned == []


def test_the_tray_keeps_saying_it_is_installing_once_the_installer_has_started(update_ready, monkeypatch, spawned):
    control = watcher.Control()
    monkeypatch.setattr(updates, "INSTALL_WAIT_SECONDS", 60)
    monkeypatch.setattr(updates, "install", lambda release: updates.Install.STARTED)
    thread = watcher.start_install(control)
    thread.join(0.5)
    # The installer is about to stop this app, so Install update… isn't offered again meanwhile.
    assert thread.is_alive() and control.installing == "1.2.0"
    assert watcher.start_install(control) is None
    control.halt()  # the app ends, by Quit say
    thread.join(10)
    assert not thread.is_alive() and control.installing is None and spawned == []


def test_an_installer_that_started_but_never_stopped_the_app_is_given_up_on_after_a_while(update_ready, monkeypatch, spawned):
    control = watcher.Control()
    waits = []
    monkeypatch.setattr(updates, "install", lambda release: updates.Install.STARTED)
    monkeypatch.setattr(updates, "INSTALL_WAIT_SECONDS", 99)
    monkeypatch.setattr(control, "stop", SimpleNamespace(wait=waits.append))  # a wait that runs out
    watcher.start_install(control).join(10)
    assert waits == [99] and control.installing is None and spawned == []  # offered again, with no Settings window


def test_an_install_that_failed_opens_settings_at_general_where_it_says_why(update_ready, monkeypatch, spawned):
    control = watcher.Control()
    monkeypatch.setattr(updates, "install", lambda release: updates.Install.FAILED)
    watcher.start_install(control).join(10)
    assert control.installing is None and [command[-1] for command in spawned] == ["--section=general"]


def test_an_install_put_off_for_a_card_or_another_click_opens_no_settings_and_waits_for_nothing(update_ready, monkeypatch, spawned):
    control = watcher.Control()
    waits = []
    monkeypatch.setattr(updates, "install", lambda release: updates.Install.LATER)
    monkeypatch.setattr(updates, "INSTALL_WAIT_SECONDS", 99)
    monkeypatch.setattr(control, "stop", SimpleNamespace(wait=waits.append))
    watcher.start_install(control).join(10)
    # Nothing failed, so there is nothing for Settings to say, and Install update… is offered again at once.
    assert waits == [] and control.installing is None and spawned == []


def test_an_install_that_breaks_is_logged_and_opens_settings_at_general(update_ready, monkeypatch, spawned, caplog):
    def broken(release):
        raise RuntimeError("something nobody planned for")

    control = watcher.Control()
    monkeypatch.setattr(updates, "install", broken)
    watcher.start_install(control).join(10)
    assert control.installing is None and [command[-1] for command in spawned] == ["--section=general"]
    assert "could not install version 1.2.0" in caplog.text


def test_there_is_nothing_to_install_without_an_update_and_not_under_a_card(update_ready, monkeypatch):
    monkeypatch.setattr(updates, "install", lambda release: pytest.fail("installed"))
    control = watcher.Control()
    control.card_open = True
    assert watcher.start_install(control) is None
    control.card_open = False
    monkeypatch.setattr(updates, "__version__", "1.2.0")  # already running it
    assert watcher.start_install(control) is None and control.installing is None


def test_a_card_on_screen_rebuilds_the_tray_menu_and_a_menu_that_cannot_be_rebuilt_changes_nothing(caplog):
    control = watcher.Control()
    watcher.Control().set_card(True)  # before the tray exists there is no menu to rebuild
    seen = []
    control.on_view = lambda view: seen.append((view, control.card_open))
    control.set_card(True)
    control.set_card(False)
    assert seen == [(control.view, True), (control.view, False)]

    def broken(view):
        raise RuntimeError("the menu could not be drawn")

    control.on_view = broken
    control.set_card(True)
    assert control.card_open and "could not rebuild the tray menu" in caplog.text


def test_the_card_mutex_is_held_from_before_the_card_starts_to_after_it_closes_and_let_go_before_the_menu_asks(monkeypatch):
    seen = []
    control = watcher.Control()
    control.on_view = lambda view: seen.append(("menu", processes.card_showing()))

    def ask(title, seconds, kind, choices):
        seen.append(("card", processes.card_showing()))
        return watcher.CardAnswer(False)

    monkeypatch.setattr(watcher, "ask_notice", ask)
    assert not processes.card_showing()
    watcher.handle_prompt(prompt(), Settings(), control, threading.Lock(), WatcherState())
    # Up before the card's own process has started and taken it, and gone by the time the menu is rebuilt.
    assert seen == [("menu", True), ("card", True), ("menu", False)]
    assert not processes.card_showing()


def test_a_card_that_cannot_run_or_breaks_still_lets_the_watcher_go_of_the_card_mutex(monkeypatch):
    control = watcher.Control()
    monkeypatch.setattr(watcher, "ask_notice", lambda *_args: watcher.CardAnswer(None))
    monkeypatch.setattr(watcher, "still_present", lambda settings: False)
    watcher.handle_prompt(prompt(), Settings(), control, threading.Lock(), WatcherState())
    assert not processes.card_showing()

    def broken(*_args):
        raise RuntimeError("the card could not be started")

    monkeypatch.setattr(watcher, "ask_notice", broken)
    with pytest.raises(RuntimeError):
        watcher.handle_prompt(prompt(), Settings(), control, threading.Lock(), WatcherState())
    assert not processes.card_showing() and not control.card_open


def test_a_card_that_is_up_keeps_the_card_mutex_after_the_watcher_lets_go_of_its_own_hold(monkeypatch, held_elsewhere):
    seen = []
    card_lets_go = held_elsewhere(processes.CARD_MUTEX)  # the card's own process, which took it as it came up

    def ask(title, seconds, kind, choices):
        seen.append(processes.card_showing())
        return watcher.CardAnswer(False)

    monkeypatch.setattr(watcher, "ask_notice", ask)
    watcher.handle_prompt(prompt(), Settings(), watcher.Control(), threading.Lock(), WatcherState())
    assert seen == [True] and processes.card_showing()  # the card is still up
    card_lets_go()
    assert not processes.card_showing()


def test_notification_buttons_retry_open_the_logs_the_config_or_settings(tray, monkeypatch):
    retried = []
    monkeypatch.setattr(watcher, "retry_launch", lambda lock, state, control, key, *, dry_run: retried.append((key, dry_run)))
    problem = Problem("launch:abc", "Granola didn't open", "", "general")

    def use(icon):
        notifier = tray.notifiers[0]
        for action in ("retry", "logs", "config", "settings", "open", "something new"):
            notifier.on_action(action, problem)
        notifier.balloon("Calendar isn't updating", "The last 3 downloads failed.")
        assert icon.balloons == [("Calendar isn't updating", "The last 3 downloads failed.")]

    tray.use = use
    watcher.run_tray(Settings(setup_complete=True, dry_run=True))
    assert retried == [("launch:abc", True)] and tray.opened == ["logs"]
    assert [command[-1] for command in tray.spawned] == [str(config_path()), "--section=general", "--section=general"]


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
    world = SimpleNamespace(call=Call("zoom", "zoom:604:1761670800", 95.0, "Roadmap review"))
    monkeypatch.setattr(watcher, "CallWatch", lambda: Watch(world))
    monkeypatch.setattr(watcher, "granola_running", lambda: False)
    monkeypatch.setattr(watcher, "load_feeds", lambda now, settings, owners: FeedLoad({WORK: [over, lunch, standup]}, failures=[(2, HOME, TimeoutError())]))
    watcher.print_check(Settings(calendars=(Calendar(WORK), Calendar(HOME))), "config.json is not valid JSON")
    out = capsys.readouterr().out.splitlines()
    assert out[:6] == [
        "config: config.json is not valid JSON (using defaults)",
        "present: True (idle 12s, limit 300s)",
        'zoom: on, in a call (zoom:604:1761670800, started 95s ago, "Roadmap review")',
        "teams: off",
        "meet: off",
        "granola running: False",
    ]
    assert out[6:8] == ["calendar feed 2: failed (timed out)", "calendar:"]
    assert out[8].endswith("  Standup") and out[9].endswith("  Lunch  (skipped)") and len(out) == 10
    assert WORK not in "\n".join(out) and HOME not in "\n".join(out)


def test_check_says_for_each_provider_whether_it_is_on_and_whether_it_is_in_a_call(monkeypatch, capsys):
    class Calls:
        def calls(self, enabled, table=None):
            assert enabled == {"zoom": False, "teams": True, "meet": True}
            return [Call("teams", "teams:5", None, None), Call("meet", "meet:chrome:7", 12.4, "Standup")]

    monkeypatch.setattr(watcher, "is_present", lambda limit: True)
    monkeypatch.setattr(watcher, "idle_seconds", lambda: 1.0)
    monkeypatch.setattr(watcher, "CallWatch", Calls)
    monkeypatch.setattr(watcher, "granola_running", lambda: False)
    watcher.print_check(Settings(calls={"zoom": False, "teams": True, "meet": True}), None)
    out = capsys.readouterr().out.splitlines()
    assert out[1:5] == ["zoom: off", "teams: on, in a call (teams:5)", 'meet: on, in a call (meet:chrome:7, started 12s ago, "Standup")', "granola running: False"]


def test_check_says_when_there_is_no_calendar_or_nothing_coming_up(monkeypatch, capsys):
    monkeypatch.setattr(watcher, "is_present", lambda limit: False)
    monkeypatch.setattr(watcher, "idle_seconds", lambda: 900.0)
    monkeypatch.setattr(watcher, "CallWatch", lambda: Watch(SimpleNamespace(call=None)))
    monkeypatch.setattr(watcher, "granola_running", lambda: True)
    monkeypatch.setattr(watcher, "load_feeds", lambda now, settings, owners: FeedLoad({WORK: []}))
    watcher.print_check(Settings(), None)
    watcher.print_check(Settings(calendars=(Calendar(WORK, enabled=False),)), None)
    watcher.print_check(Settings(calendars=(Calendar(WORK),)), None)
    out = capsys.readouterr().out.splitlines()
    assert "zoom: on, not in a call" in out and "teams: off" in out and "meet: off" in out
    assert "calendar: no calendar feed in config" in out and "calendar: every calendar in config is switched off" in out
    assert out[-1] == "calendar: no upcoming events in the feed"


def test_check_downloads_the_calendars_that_are_on_each_with_its_own_declines(monkeypatch, capsys):
    soon = (datetime.now(timezone.utc) + timedelta(hours=1)).replace(microsecond=0)

    def feed(name, *events):
        stamp = f"{soon:%Y%m%dT%H%M%SZ}"
        end = f"{soon + timedelta(minutes=30):%Y%m%dT%H%M%SZ}"
        lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"X-WR-CALNAME:{name}"]
        for title, partstat, who in events:
            lines += ["BEGIN:VEVENT", f"UID:{title}@google.com", f"SUMMARY:{title}", f"DTSTART:{stamp}", f"DTEND:{end}"]
            lines += [f"ATTENDEE;PARTSTAT={partstat}:mailto:{who}", "END:VEVENT"]
        return "\n".join([*lines, "END:VCALENDAR", ""])

    served = {
        HOME: feed("Home", ("Dentist", "DECLINED", "me@example.com"), ("Pottery", "ACCEPTED", "me@example.com"), ("Choir", "DECLINED", "alias@example.ca")),
        WORK: feed("me@example.com", ("Planning", "ACCEPTED", "me@example.com"), ("Dentist", "DECLINED", "me@example.com")),
    }
    fetched = []
    monkeypatch.setattr(calendar_cache, "fetch_ics", lambda url: fetched.append(url) or served[url])
    monkeypatch.setattr(watcher, "is_present", lambda limit: True)
    monkeypatch.setattr(watcher, "idle_seconds", lambda: 1.0)
    monkeypatch.setattr(watcher, "CallWatch", lambda: Watch(SimpleNamespace(call=None)))
    monkeypatch.setattr(watcher, "granola_running", lambda: False)
    monkeypatch.setattr(watcher, "CalendarCache", lambda: pytest.fail("the check read the owners through a cache"))
    # The calendar that is off lent its address to the watcher, which saved it in status.json.
    work = FeedStatus(feed_id(WORK), name="me@example.com", owner="me@example.com")
    write_status(status_path(), Status("test", feeds=[work]))
    settings = Settings(calendars=(Calendar(WORK, enabled=False), Calendar(HOME, declined_by=None)))
    watcher.print_check(settings, None)
    assert fetched == [HOME]
    out = capsys.readouterr().out
    assert "Pottery" in out and "Choir" in out and "Dentist" not in out and "Planning" not in out
    fetched.clear()
    watcher.print_check(Settings(calendars=(Calendar(WORK, enabled=False), Calendar(HOME, declined_by=("alias@example.ca",)))), None)
    out = capsys.readouterr().out
    assert "Pottery" in out and "Dentist" in out and "Choir" not in out  # exactly the listed address
    # A meeting on two calendars is skipped only when it is declined on both. WORK declines the Dentist, which is also
    # on HOME. With every address removed nothing is declined on HOME, so the Dentist counts there, and so does Choir.
    watcher.print_check(Settings(calendars=(Calendar(WORK), Calendar(HOME, declined_by=()))), None)
    out = capsys.readouterr().out
    assert "Pottery" in out and "Dentist" in out and "Choir" in out and "Planning" in out
    # With only the alias on HOME's list, its Dentist isn't declined there (me@example.com declined it), but Choir is.
    watcher.print_check(Settings(calendars=(Calendar(WORK), Calendar(HOME, declined_by=("alias@example.ca",)))), None)
    out = capsys.readouterr().out
    assert "Pottery" in out and "Dentist" in out and "Planning" in out and "Choir" not in out
    # Automatic, HOME borrows WORK's address, so the Dentist is declined on both calendars and skipped.
    watcher.print_check(Settings(calendars=(Calendar(WORK), Calendar(HOME))), None)
    out = capsys.readouterr().out
    assert "Pottery" in out and "Planning" in out and "Dentist" not in out


@pytest.fixture
def cli(monkeypatch):
    """watcher.main for a command line, with every mode's real work recorded instead of done."""
    calls = []
    monkeypatch.setattr(watcher, "setup_logging", lambda name: calls.append(("log", name)) or logging.getLogger("granola_autostart"))
    monkeypatch.setattr(processes, "hold_mutex", lambda name: True)
    for mode in ("install", "uninstall"):
        monkeypatch.setattr(processes, mode, lambda mode=mode: calls.append(mode))
    monkeypatch.setattr(watcher, "print_check", lambda settings, error: calls.append(("check", error is not None)))
    monkeypatch.setattr(watcher, "run_tray", lambda settings, error, *, dry_run: calls.append(("tray", settings.dry_run, dry_run)) or 0)

    def run(*argv: str) -> int:
        monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", *argv])
        return watcher.main()

    return calls, run


@pytest.mark.parametrize("mode", ["install", "uninstall"])
def test_install_and_uninstall_do_their_one_job_and_log_to_the_cli_log(mode, cli):
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


@pytest.mark.parametrize(("picked", "said"), [(0, "would start"), (None, "cancelled")])
def test_preview_shows_the_card_once_and_says_what_it_would_do(picked, said, cli, monkeypatch, capsys):
    _calls, run = cli
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds: picked)
    assert run("--preview-notice") == 0
    assert capsys.readouterr().out.strip() == said


def test_only_one_mode_at_a_time(cli):
    _calls, run = cli
    with pytest.raises(SystemExit):
        run("--install", "--uninstall")


def test_stop_is_the_package_mains_mode_and_not_the_watchers(cli):
    # __main__ handles --stop before the watcher's command line sees it, so the watcher no longer parses it.
    calls, run = cli
    with pytest.raises(SystemExit):
        run("--stop")
    assert calls == []
    help_text = watcher.build_parser().format_help()
    assert "--stop" in help_text.split("The other modes:")[1]  # the epilog lists it with the other modes of __main__
    assert "stop the running watcher" not in help_text
