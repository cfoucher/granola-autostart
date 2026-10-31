import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import commands, processes, watcher
from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch, Prompt, WatcherState
from granola_autostart.settings import Settings, config_path, save_settings
from granola_autostart.status import find_problems, launch_problem_key, taking_notes

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)


def prompt(kind: str = "calendar") -> Prompt:
    return Prompt(
        kind=kind,
        title="Design review",
        payload={"id": "abc", "summary": "Design review"},
        keys=("event:abc",),
        expires_at=NOW + timedelta(minutes=30),
        covers=("event:other",),
    )


@pytest.fixture
def gate(monkeypatch):
    """The card's answer is world["accepted"]: True, False (Cancel), or None (the card couldn't run)."""
    launches: list[tuple[str, bool]] = []
    world = {"accepted": True, "unlocked": True, "idle": 1.0}
    monkeypatch.setattr(watcher, "ask_notice", lambda *_args: world["accepted"])
    monkeypatch.setattr(watcher, "is_unlocked", lambda: world["unlocked"])
    monkeypatch.setattr(watcher, "idle_seconds", lambda: world["idle"])
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: launches.append((url, dry_run)))

    def run(control=None, settings=None, kind="calendar", state=None):
        state = state or WatcherState()
        watcher.handle_prompt(prompt(kind), settings or Settings(), control or watcher.Control(), threading.Lock(), state)
        return state

    return world, launches, run


def test_finished_countdown_starts_granola(gate):
    _world, launches, run = gate
    state = run()
    assert len(launches) == 1
    assert "event:abc" in state.handled


def test_locking_during_the_countdown_starts_nothing_and_asks_again(gate):
    world, launches, run = gate
    world["unlocked"] = False
    state = run()
    assert launches == []
    assert state.handled == {}


def test_pausing_during_the_countdown_starts_nothing(gate):
    _world, launches, run = gate
    control = watcher.Control()
    control.set_pause(datetime.now().astimezone() + timedelta(hours=1))
    state = run(control)
    assert launches == []
    assert state.handled == {}


def test_cancel_records_the_decision(gate):
    world, launches, run = gate
    world["accepted"] = False
    state = run()
    assert launches == []
    assert "event:abc" in state.handled


def test_a_dry_run_answers_the_meeting_without_opening_or_watching_anything(gate):
    _world, launches, run = gate
    control = watcher.Control()
    state = run(control, Settings(dry_run=True))
    assert [dry_run for _url, dry_run in launches] == [True]  # launch() only logs a dry run's link
    # Answered like a real start (covering the other meeting under way), with nothing left to watch.
    assert {"event:abc", "event:other"} <= set(state.handled)
    assert state.active_launch is None and control.last_start is None


def test_a_card_that_cannot_run_still_starts_notes_and_says_the_card_is_broken(gate):
    world, launches, run = gate
    world["accepted"] = None
    control = watcher.Control()
    state = run(control)
    assert len(launches) == 1 and "event:abc" in state.handled
    assert control.card_failed
    assert [problem.key for problem in find_problems(None, [], None, card_failed=control.card_failed)] == ["card"]
    world["accepted"] = True
    run(control)
    assert not control.card_failed


def test_without_a_working_card_notes_are_not_restarted_after_granola_closes(gate):
    world, launches, run = gate
    world["accepted"] = None
    state = run(kind="crash")
    assert launches == [] and state.active_launch is None


def failed_launch(gate, monkeypatch, control):
    def refuse(url, dry_run):
        raise OSError("Could not open Granola (ShellExecute error 31)")

    monkeypatch.setattr(watcher, "launch", refuse)
    _world, _launches, run = gate
    return run(control)


def test_a_link_windows_cannot_open_is_reported_at_once_not_taken_as_cancel(gate, monkeypatch):
    control = watcher.Control()
    state = failed_launch(gate, monkeypatch, control)
    assert "event:abc" in state.handled
    assert state.active_launch.failed
    [problem] = find_problems(None, [], state.active_launch)
    assert problem.title == "Granola didn't open for Design review"
    assert control.note_launch(state.active_launch).granola_opened is False


def test_granola_already_running_does_not_hide_a_link_that_failed(gate, monkeypatch):
    control = watcher.Control()
    state = failed_launch(gate, monkeypatch, control)
    state.active_launch.seen_running = True  # Granola sits in the tray all day
    assert find_problems(None, [], state.active_launch) != []
    assert taking_notes(state.active_launch, NOW + timedelta(minutes=1)) is None
    assert control.note_launch(state.active_launch).granola_opened is False


def failed_state() -> WatcherState:
    """A start whose link failed, for a meeting still under way (Try again is only for those)."""
    ends = datetime.now().astimezone() + timedelta(minutes=30)
    launch = ActiveLaunch("Design review", {"id": "abc"}, ("event:abc",), ends, False, NOW, failed=True)
    return WatcherState(active_launch=launch)


def test_try_again_is_a_new_start_with_its_own_check_and_line_in_about(monkeypatch):
    opened = []
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: opened.append((url, dry_run)))
    state, control = failed_state(), watcher.Control()
    first = state.active_launch.launched_at
    watcher.retry_launch(threading.Lock(), state, control, launch_problem_key(state.active_launch), dry_run=False)
    assert len(opened) == 1 and "abc" in opened[0][0] and opened[0][1] is False
    launch = state.active_launch
    assert not launch.failed and launch.launched_at > first  # so the 30-second check runs again
    assert find_problems(None, [], launch) == []
    launch.seen_running = True
    assert control.note_launch(launch).granola_opened is True


def test_try_again_that_fails_again_says_so_with_a_new_notification(monkeypatch):
    def refuse(url, dry_run):
        raise OSError("Could not open Granola (ShellExecute error 31)")

    monkeypatch.setattr(watcher, "launch", refuse)
    state, control = failed_state(), watcher.Control()
    old_key = launch_problem_key(state.active_launch)
    watcher.retry_launch(threading.Lock(), state, control, old_key, dry_run=False)
    [problem] = find_problems(None, [], state.active_launch)
    assert state.active_launch.failed and problem.key != old_key
    assert control.note_launch(state.active_launch).granola_opened is False


def test_try_again_from_an_older_notification_or_a_second_click_does_nothing(monkeypatch):
    opened = []
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: opened.append(url))
    state, control = failed_state(), watcher.Control()
    key = launch_problem_key(state.active_launch)
    watcher.retry_launch(threading.Lock(), state, control, key, dry_run=False)
    watcher.retry_launch(threading.Lock(), state, control, key, dry_run=False)  # the second click
    older = ActiveLaunch("Design review", {}, ("event:abc",), NOW, False, NOW - timedelta(minutes=5))
    watcher.retry_launch(threading.Lock(), failed_state(), control, launch_problem_key(older), dry_run=False)
    watcher.retry_launch(threading.Lock(), WatcherState(), control, key, dry_run=False)
    assert len(opened) == 1


def card_process(run):
    return SimpleNamespace(run=run, TimeoutExpired=subprocess.TimeoutExpired)


@pytest.mark.parametrize(("exit_code", "answer"), [(0, True), (1, False), (2, None)])
def test_the_card_says_start_cancel_or_that_it_could_not_run(exit_code, answer, monkeypatch):
    monkeypatch.setattr(watcher, "subprocess", card_process(lambda command, timeout: subprocess.CompletedProcess(command, exit_code)))
    assert watcher.ask_notice("Design review", 10, "calendar") is answer


def test_a_card_that_never_closes_or_never_starts_could_not_run(monkeypatch):
    timeouts = []

    def hang(command, timeout):
        timeouts.append(timeout)
        raise subprocess.TimeoutExpired(command, timeout)

    def missing(command, timeout):
        raise OSError("pythonw.exe not found")

    for run in (hang, missing):
        monkeypatch.setattr(watcher, "subprocess", card_process(run))
        assert watcher.ask_notice("Design review", 10, "calendar") is None
    assert timeouts[0] > 10  # the card gets its whole countdown and then some


def test_granola_not_seen_after_thirty_seconds_raises_the_warning():
    launch = ActiveLaunch("Design review", {}, ("event:abc",), NOW + timedelta(minutes=30), seen_running=False, launched_at=NOW)
    state = WatcherState(active_launch=launch)
    watcher._warn_if_launch_missing(state, NOW + timedelta(seconds=29))
    assert not launch.warned
    watcher._warn_if_launch_missing(state, NOW + timedelta(seconds=30))
    assert launch.warned
    seen = ActiveLaunch("Design review", {}, ("event:abc",), NOW + timedelta(minutes=30), seen_running=True, launched_at=NOW)
    watcher._warn_if_launch_missing(WatcherState(active_launch=seen), NOW + timedelta(minutes=5))
    assert not seen.warned


class Cache:
    invalidated = 0

    def invalidate(self, urls):
        self.invalidated += 1


def test_a_saved_config_is_picked_up_and_a_broken_one_keeps_the_last_good_settings():
    control = watcher.Control()
    control.force_dry_run = True
    cache = Cache()
    save_settings(Settings(countdown_seconds=20))
    current = watcher.reload_settings(control, Settings(), cache)
    assert (current.countdown_seconds, current.dry_run, control.config_error, cache.invalidated) == (20, True, None, 0)
    config_path().write_text("{ broken", encoding="utf-8")
    assert watcher.reload_settings(control, current, cache) is current
    assert control.config_error.startswith("config.json is not valid JSON")
    save_settings(Settings(countdown_seconds=20, ignore_title_substrings=("lunch",)))
    fresh = watcher.reload_settings(control, current, cache)
    assert (control.config_error, cache.invalidated, fresh.ignore_title_substrings) == (None, 1, ("lunch",))
    assert json.loads(config_path().read_text(encoding="utf-8"))["countdown_seconds"] == 20


class Notifier:
    def __init__(self):
        self.shown = []

    def show(self, problem):
        self.shown.append(problem.key)


class Feeds:
    def feed_statuses(self, urls):
        return []


def test_each_tick_reports_the_problem_once_and_writes_the_status(monkeypatch):
    written = []
    monkeypatch.setattr(watcher, "write_status", lambda path, status: written.append(status))
    control = watcher.Control()
    notifier = Notifier()
    reporter = watcher.Reporter(control, notifier)
    launch = failed_state().active_launch
    control.record_start("Design review", launch.launched_at)
    for _tick in range(2):
        reporter.tick(NOW, Settings(), Feeds(), [], launch)
    assert notifier.shown == [launch_problem_key(launch)]
    assert control.view.kind == "attention"
    assert written[-1].last_start.granola_opened is False and written[-1].problems[0].key == launch_problem_key(launch)


def test_try_again_leaves_a_start_that_has_opened_since_or_whose_meeting_ended(monkeypatch):
    opened = []
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: opened.append(url))
    slow = failed_state()
    slow.active_launch.failed, slow.active_launch.warned, slow.active_launch.seen_running = False, True, True
    ended = failed_state()
    ended.active_launch.expires_at = datetime.now().astimezone() - timedelta(minutes=1)
    for state in (slow, ended):
        watcher.retry_launch(threading.Lock(), state, watcher.Control(), launch_problem_key(state.active_launch), dry_run=False)
    assert opened == []


def test_try_again_in_a_dry_run_answers_the_start_with_nothing_to_watch(monkeypatch):
    opened = []
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: opened.append(dry_run))
    state, control = failed_state(), watcher.Control()
    watcher.retry_launch(threading.Lock(), state, control, launch_problem_key(state.active_launch), dry_run=True)
    assert opened == [True] and state.active_launch is None and control.last_start is None


def test_without_a_working_card_a_crash_card_ends_that_start(gate):
    world, launches, run = gate
    world["accepted"] = None
    control = watcher.Control()
    running = ActiveLaunch("Design review", {"id": "abc"}, ("event:abc",), NOW + timedelta(minutes=30), True, NOW, missing_since=NOW)
    state = run(control, kind="crash", state=WatcherState(active_launch=running))
    assert launches == [] and state.active_launch is None
    assert "event:abc" in state.handled and control.card_failed


def test_without_a_working_card_a_zoom_join_still_starts_notes(gate):
    world, launches, run = gate
    world["accepted"] = None
    run(kind="zoom")
    assert len(launches) == 1


def test_try_again_after_granola_never_appeared_starts_clean(monkeypatch):
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: None)
    state = failed_state()
    state.active_launch.failed, state.active_launch.warned = False, True
    watcher.retry_launch(threading.Lock(), state, watcher.Control(), launch_problem_key(state.active_launch), dry_run=False)
    assert find_problems(None, [], state.active_launch) == []


def test_a_locked_state_file_still_records_the_start(gate, monkeypatch):
    def locked(*_args):
        raise PermissionError("state.json is open in another program")

    monkeypatch.setattr(watcher, "save_state", locked)
    _world, launches, run = gate
    control = watcher.Control()
    state = run(control)
    assert len(launches) == 1 and "event:abc" in state.handled and control.last_start.title == "Design review"


def test_quitting_during_the_countdown_opens_nothing(gate):
    _world, launches, run = gate
    control = watcher.Control()
    control.halt()
    state = run(control)
    assert launches == [] and state.handled == {}


def test_each_tick_writes_the_card_problem_the_pause_the_next_meeting_and_notes(monkeypatch):
    written = []
    monkeypatch.setattr(watcher, "write_status", lambda path, status: written.append(status))
    control = watcher.Control()
    control.card_failed = True
    pause = NOW + timedelta(hours=1)
    control.set_pause(pause)
    notifier = Notifier()
    standup = CalendarEvent("s", "Standup", NOW + timedelta(hours=2), NOW + timedelta(hours=3))
    opened = ActiveLaunch("Design review", {}, ("event:abc",), NOW + timedelta(minutes=30), True, NOW - timedelta(minutes=5))
    watcher.Reporter(control, notifier).tick(NOW, Settings(), Feeds(), [standup], opened)
    status = written[-1]
    assert notifier.shown == ["card"] and [problem.key for problem in status.problems] == ["card"]
    assert status.next_meeting == ("Standup", standup.start)
    assert status.taking_notes == ("Design review", NOW - timedelta(minutes=5))
    assert status.paused_until == pause
    watcher.Reporter(control, notifier).tick(pause, Settings(), Feeds(), [], None)
    assert written[-1].paused_until is None and written[-1].taking_notes is None


def test_granola_didn_t_open_clears_once_its_meeting_is_over(monkeypatch):
    written = []
    monkeypatch.setattr(watcher, "write_status", lambda path, status: written.append(status))
    launch = failed_state().active_launch
    reporter = watcher.Reporter(watcher.Control(), Notifier())
    reporter.tick(launch.expires_at - timedelta(minutes=1), Settings(), Feeds(), [], launch)
    assert [problem.key for problem in written[-1].problems] == [launch_problem_key(launch)]
    reporter.tick(launch.expires_at, Settings(), Feeds(), [], launch)
    assert written[-1].problems == []


@pytest.fixture
def tray(monkeypatch):
    """run_tray with everything around the icon faked; the watcher loop just waits for the stop."""
    loops = []

    def run_loop(settings, control, state_lock, state, reporter):
        loops.append(control)
        control.stop.wait(10)

    monkeypatch.setattr(watcher, "modern_menus", lambda: None)
    monkeypatch.setattr(watcher, "register_app_id", lambda logo: None)
    monkeypatch.setattr(watcher, "Notifier", lambda *args, **kwargs: None)
    monkeypatch.setattr(watcher, "run_loop", run_loop)
    return loops


def threads_named(name):
    return [thread for thread in threading.enumerate() if thread.name == name]


def test_a_tray_that_fails_still_ends_the_watcher_loop(tray, monkeypatch):
    class BrokenIcon:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, setup):
            raise RuntimeError("no notification area")

    monkeypatch.setattr(watcher.pystray, "Icon", BrokenIcon)
    with pytest.raises(RuntimeError):
        watcher.run_tray(Settings(setup_complete=True))
    [control] = tray
    assert control.stop.is_set()
    assert threads_named("granola-autostart") == []


def test_quit_from_settings_ends_the_tray_and_tells_the_supervisor(tray, monkeypatch):
    class Icon:
        def __init__(self, *args, **kwargs):
            self.visible = False
            self.stopped = threading.Event()

        def run(self, setup):
            try:
                setup(self)  # pystray runs it on its own thread, where a failure is only logged
            except RuntimeError:
                pass
            deadline = time.monotonic() + 10
            while not commands.send("quit"):
                assert time.monotonic() < deadline
                time.sleep(0.02)
            assert self.stopped.wait(10)

        def stop(self):
            self.stopped.set()

        def update_menu(self):
            raise RuntimeError("the menu could not be drawn")

    monkeypatch.setattr(watcher.pystray, "Icon", Icon)
    assert watcher.run_tray(Settings(setup_complete=True)) == processes.QUIT_EXIT_CODE
    for thread in threads_named("settings-commands"):
        thread.join(5)
    assert threads_named("settings-commands") == [] and threads_named("granola-autostart") == []
    assert commands.send("quit") is False
