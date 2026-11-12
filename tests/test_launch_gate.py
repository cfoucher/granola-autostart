import dataclasses
import itertools
import json
import logging
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import commands, processes, watcher
from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch, Choice, Prompt, WatcherState
from granola_autostart.settings import Calendar, Settings, config_path, save_settings
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
    """The card's answer is world["accepted"]: True, False (Cancel), or None (the card couldn't run), with the
    meeting it ticked, world["picked"], and the card is asked what it is shown in world["asked"].

    Each look at Granola's window answers the next of world["recordings"], the title of the note it shows as
    recording, and None once they are used up; world["looks"] counts them."""
    launches: list[tuple[str, bool]] = []
    world = {"accepted": True, "picked": 0, "unlocked": True, "idle": 1.0, "asked": [], "recordings": [], "looks": 0}

    def ask(*args, **_kwargs):
        world["asked"].append(args)
        return watcher.CardAnswer(world["accepted"], world["picked"])

    def look():
        world["looks"] += 1
        return world["recordings"].pop(0) if world["recordings"] else None

    monkeypatch.setattr(watcher, "ask_notice", ask)
    monkeypatch.setattr(watcher.granola_window, "recording_title", look)
    monkeypatch.setattr(watcher, "is_unlocked", lambda: world["unlocked"])
    monkeypatch.setattr(watcher, "idle_seconds", lambda: world["idle"])
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: launches.append((url, dry_run)))

    def run(control=None, settings=None, kind="calendar", state=None, shown=None):
        state = state or WatcherState()
        watcher.handle_prompt(shown or prompt(kind), settings or Settings(), control or watcher.Control(), threading.Lock(), state)
        return state

    return world, launches, run


def test_finished_countdown_starts_granola(gate):
    _world, launches, run = gate
    state = run()
    assert len(launches) == 1
    assert "event:abc" in state.handled


def test_the_tray_menu_is_rebuilt_as_the_card_opens_and_closes(gate):
    _world, launches, run = gate
    control = watcher.Control()
    seen = []
    control.on_view = lambda view: seen.append(control.card_open)
    run(control)
    assert seen == [True, False] and not control.card_open and len(launches) == 1


def test_a_tray_menu_that_cannot_be_rebuilt_never_keeps_the_card_from_showing(gate):
    def broken(view):
        raise RuntimeError("the menu could not be drawn")

    _world, launches, run = gate
    control = watcher.Control()
    control.on_view = broken
    run(control)
    assert len(launches) == 1 and not control.card_open


def test_a_card_that_breaks_is_not_left_marked_as_on_screen(gate, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("the card could not be started")

    monkeypatch.setattr(watcher, "ask_notice", boom)
    _world, _launches, run = gate
    control = watcher.Control()
    with pytest.raises(RuntimeError):
        run(control)
    assert not control.card_open


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


def test_turning_notes_off_during_the_countdown_starts_nothing(gate):
    _world, launches, run = gate
    control = watcher.Control()
    control.set_notes(False)  # the tray's Turn off, in memory at once
    state = run(control)
    assert launches == [] and state.handled == {}


def test_notes_turned_off_in_settings_during_the_countdown_start_nothing_either(gate):
    # The loop is waiting on the card, so it hasn't reloaded config.json, and the watcher's copy still says on.
    world, launches, run = gate
    control = watcher.Control()
    save_settings(Settings(autostart_notes=False, setup_complete=True))
    assert control.settings.autostart_notes
    assert run(control).handled == {} and launches == []
    world["accepted"] = None  # a card that couldn't run starts notes, unless they are off
    assert run(control).handled == {} and launches == []


def test_the_switch_is_the_watchers_own_when_config_json_cannot_be_read(gate):
    _world, launches, run = gate
    config_path().write_text("{ broken", encoding="utf-8")
    control = watcher.Control()
    assert len(run(control).handled) == 2 and len(launches) == 1  # on in memory, and nothing saved says otherwise
    control.set_notes(False)
    assert run(control).handled == {} and len(launches) == 1  # off in memory, as the tray set it


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


def meeting(title, key, guests, calendar, *call):
    """A meeting the card lists, as decide offers it: its own key, after the call's when it is for a call."""
    end = NOW + timedelta(minutes=15)
    return Choice(title, {"id": key, "summary": title}, (*call, f"event:{key}"), end, NOW, end, guests, calendar)


def together(call=(), cancel_keys=None) -> Prompt:
    """Two meetings starting together, the stand-up first, as the watcher is handed them."""
    standup = meeting("Team stand-up", "standup", 7, "you@shift.com", *call)
    reminder = meeting("Renew parking permit", "reminder", 0, "Personal", *call)
    return Prompt(
        kind="call" if call else "calendar",
        title=standup.title,
        payload=standup.payload,
        keys=standup.keys,
        expires_at=standup.expires_at,
        cancel_keys=cancel_keys,
        covers=("event:reminder",),
        call_session=call[0] if call else None,
        choices=(standup, reminder),
    )


def test_the_card_is_asked_to_list_the_meetings_each_with_its_line_and_a_single_meeting_lists_none(gate):
    world, _launches, run = gate
    run(shown=together())
    title, seconds, kind, listed = world["asked"][0]
    assert (title, seconds, kind) == ("Team stand-up", Settings().countdown_seconds, "calendar")
    assert [name for name, _line in listed] == ["Team stand-up", "Renew parking permit"]
    assert listed[0][1].endswith(" · 7 guests · you@shift.com") and listed[1][1].endswith(" · No guests · Personal")
    run()
    assert world["asked"][1][3] == []


def test_the_meeting_ticked_on_the_card_is_the_one_opened_and_the_other_is_answered(gate):
    world, launches, run = gate
    world["picked"] = 1
    control = watcher.Control()
    state = run(control, shown=together())
    [(url, _dry_run)] = launches
    assert "reminder" in url and "standup" not in url
    assert state.active_launch.title == "Renew parking permit" and state.active_launch.keys == ("event:reminder",)
    assert set(state.handled) == {"event:reminder", "event:standup"}
    assert control.last_start.title == "Renew parking permit"


def test_the_first_meeting_is_opened_when_nothing_else_is_ticked_and_the_other_is_answered_too(gate):
    _world, launches, run = gate
    state = run(shown=together())
    [(url, _dry_run)] = launches
    assert "standup" in url and "reminder" not in url
    assert set(state.handled) == {"event:standup", "event:reminder"}


def test_a_card_that_cannot_run_starts_the_first_meeting_and_answers_both(gate):
    world, launches, run = gate
    world["accepted"] = None
    state = run(shown=together())
    assert len(launches) == 1 and "standup" in launches[0][0]
    assert set(state.handled) == {"event:standup", "event:reminder"}


def test_cancel_answers_every_meeting_the_card_listed_whichever_was_ticked(gate):
    world, launches, run = gate
    world["accepted"], world["picked"] = False, 1
    state = run(shown=together())
    assert launches == [] and state.active_launch is None
    assert set(state.handled) == {"event:standup", "event:reminder"}


def test_cancel_on_an_early_join_answers_only_the_call(gate):
    world, launches, run = gate
    world["accepted"] = False
    state = run(shown=together(call=("zoom:9:100",), cancel_keys=("zoom:9:100",)))
    assert launches == [] and set(state.handled) == {"zoom:9:100"}


def test_a_meeting_picked_for_a_call_keeps_the_call_and_answers_the_others(gate):
    world, launches, run = gate
    world["picked"] = 1
    state = run(shown=together(call=("zoom:9:100",)))
    assert "reminder" in launches[0][0]
    assert state.active_launch.keys == ("zoom:9:100", "event:reminder") and state.active_launch.call_session == "zoom:9:100"
    assert set(state.handled) == {"zoom:9:100", "event:reminder", "event:standup"}


def test_the_log_says_how_many_meetings_the_countdown_was_for_and_which_was_picked_without_the_calendars(gate, caplog):
    world, _launches, run = gate
    world["picked"] = 1
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        run(shown=together())
    assert "countdown for Team stand-up (calendar), 2 meetings" in caplog.text
    assert "picked Renew parking permit on the card" in caplog.text
    assert "Personal" not in caplog.text and "you@shift.com" not in caplog.text
    caplog.clear()
    world["picked"] = 0
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        run(shown=together())
        run()
    assert "picked" not in caplog.text and "2 meetings" in caplog.text
    assert "countdown for Design review (calendar)" in caplog.text and "Design review (calendar)," not in caplog.text


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


def test_try_again_is_a_new_start_with_its_own_check_and_line_in_general(monkeypatch):
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


class Card:
    """The card's process. ``wait`` gives each of ``results`` in turn: an exit code, or a poll that came up empty (a
    TimeoutExpired). Once told to close it exits with -1, unless it is ``stubborn``, which takes a kill."""

    def __init__(self, results, stubborn=False):
        self.results = iter(results)
        self.stubborn = stubborn
        self.terminated = self.killed = False

    def wait(self, timeout=None):
        if self.killed or (self.terminated and not self.stubborn):
            return -1
        if self.terminated:
            raise subprocess.TimeoutExpired("card", timeout)
        result = next(self.results)
        if result is None:
            raise subprocess.TimeoutExpired("card", timeout)
        return result

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def card_process(monkeypatch, *results, stubborn=False, clock=None):
    """Replace the card's process with a Card. Returns it, with the commands it was started with."""
    card, commands = Card(results, stubborn), []

    def popen(command):
        commands.append(command)
        return card

    monkeypatch.setattr(watcher, "subprocess", SimpleNamespace(Popen=popen, TimeoutExpired=subprocess.TimeoutExpired))
    if clock is not None:
        monkeypatch.setattr(watcher, "time", SimpleNamespace(monotonic=clock))
    return card, commands


@pytest.mark.parametrize(("exit_code", "answer"), [(0, True), (1, False), (2, None)])
def test_the_card_says_start_cancel_or_that_it_could_not_run(exit_code, answer, monkeypatch):
    card_process(monkeypatch, exit_code)
    assert watcher.ask_notice("Design review", 10, "calendar") == watcher.CardAnswer(answer)


@pytest.mark.parametrize(
    ("exit_code", "answer"),
    [
        (11, watcher.CardAnswer(True, 1)),
        (12, watcher.CardAnswer(True, 2)),
        (13, watcher.CardAnswer(None)),  # a third place the card never listed
        (10, watcher.CardAnswer(None)),  # the first meeting is the plain 0
        (0, watcher.CardAnswer(True, 0)),
    ],
)
def test_the_card_names_the_meeting_it_started_in_its_exit_code(exit_code, answer, monkeypatch):
    _card, commands = card_process(monkeypatch, exit_code)
    listed = [("Stand-up", "09:00"), ("Reminder", "09:00"), ("Review", "09:00")]
    assert watcher.ask_notice("Stand-up", 10, "calendar", listed) == answer
    [argument] = [part for part in commands[0] if part.startswith("--choices=")]
    assert json.loads(argument.removeprefix("--choices=")) == [{"title": name, "line": line} for name, line in listed]


def test_a_card_that_never_closes_is_killed_after_its_whole_countdown_and_then_some_and_could_not_run(monkeypatch, caplog):
    card, _commands = card_process(monkeypatch, *itertools.repeat(None, 100), clock=itertools.count(0, 30).__next__)
    with caplog.at_level(logging.ERROR, logger="granola_autostart"):
        assert watcher.ask_notice("Design review", 10, "calendar") == watcher.CardAnswer(None)
    # Started at 0, so the limit is 10 seconds and a minute more: reached on the third look, at 90.
    assert card.killed and not card.terminated
    assert "countdown window did not close" in caplog.text


def test_the_overall_limit_is_the_countdown_and_a_minute_whatever_it_is_and_never_under_a_second(monkeypatch):
    for seconds, limit in ((10, 70), (0, 61), (-5, 61)):
        card, _commands = card_process(monkeypatch, *itertools.repeat(None, 100), clock=iter([0, limit - 1, limit]).__next__)
        assert watcher.ask_notice("Design review", seconds, "calendar") == watcher.CardAnswer(None)
        assert card.killed


def test_a_card_that_cannot_start_could_not_run(monkeypatch, caplog):
    def missing(command):
        raise OSError("pythonw.exe not found")

    monkeypatch.setattr(watcher, "subprocess", SimpleNamespace(Popen=missing, TimeoutExpired=subprocess.TimeoutExpired))
    with caplog.at_level(logging.ERROR, logger="granola_autostart"):
        assert watcher.ask_notice("Design review", 10, "calendar") == watcher.CardAnswer(None)
    assert "countdown window failed to open" in caplog.text


def test_granola_is_looked_at_on_every_poll_of_the_card_and_a_card_that_closes_is_answered_as_before(monkeypatch):
    card_process(monkeypatch, None, None, None, 0, clock=itertools.count().__next__)
    looks = []
    answer = watcher.ask_notice("Design review", 10, "calendar", granola_has=lambda: looks.append(1))
    assert answer == watcher.CardAnswer(True) and len(looks) == 3


def test_granola_taking_notes_during_the_countdown_closes_the_card_and_says_which_meeting(monkeypatch, caplog):
    card, _commands = card_process(monkeypatch, None, None, None, clock=itertools.count().__next__)
    seen = iter([None, 1])
    listed = [("Stand-up", "09:00"), ("Reminder", "09:00")]
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        answer = watcher.ask_notice("Stand-up", 10, "calendar", listed, granola_has=lambda: next(seen))
    assert answer == watcher.CardAnswer(True, 1, granola=True)
    assert card.terminated and not card.killed
    assert "Granola started notes for Reminder during the countdown; closed the card" in caplog.text


def test_a_card_for_one_meeting_names_it_by_its_title_when_granola_starts_notes(monkeypatch, caplog):
    card, _commands = card_process(monkeypatch, None, None, clock=itertools.count().__next__)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        answer = watcher.ask_notice("Design review", 10, "calendar", granola_has=lambda: 0)
    assert answer == watcher.CardAnswer(True, 0, granola=True) and card.terminated
    assert "Granola started notes for Design review during the countdown; closed the card" in caplog.text


def test_a_card_that_does_not_close_when_told_to_is_killed(monkeypatch):
    card, _commands = card_process(monkeypatch, None, None, stubborn=True, clock=itertools.count().__next__)
    assert watcher.ask_notice("Design review", 10, "calendar", granola_has=lambda: 0).granola
    assert card.terminated and card.killed


def test_the_card_is_asked_nothing_about_granola_after_it_has_closed(monkeypatch):
    card_process(monkeypatch, 1)
    looks = []
    assert watcher.ask_notice("Design review", 10, "calendar", granola_has=lambda: looks.append(1)) == watcher.CardAnswer(False)
    assert looks == []


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
    urls = None

    def invalidate(self, urls):
        self.invalidated += 1
        self.urls = urls


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


def test_a_change_to_the_calendars_their_switches_or_declined_addresses_starts_the_cache_afresh():
    work, home = "https://calendar.example/work.ics", "https://calendar.example/home.ics"
    control, cache = watcher.Control(), Cache()

    def reload(current, *calendars, **options):
        save_settings(Settings(calendars=calendars, **options))
        return watcher.reload_settings(control, current, cache)

    settings = reload(Settings(), Calendar(work), Calendar(home))
    assert (cache.invalidated, cache.urls) == (1, [work, home])
    settings = reload(settings, Calendar(work), Calendar(home))
    assert cache.invalidated == 1  # nothing changed
    settings = reload(settings, Calendar(work), Calendar(home, enabled=False))
    assert (cache.invalidated, cache.urls) == (2, [work])  # the one that is on
    settings = reload(settings, Calendar(work, declined_by=("me@example.com", "alias@example.ca")), Calendar(home, enabled=False))
    assert cache.invalidated == 3
    settings = reload(settings, Calendar(work, declined_by=("me@example.com", "alias@example.ca")), Calendar(home, enabled=False), user_email="old@example.com")
    assert cache.invalidated == 4  # the old typed address still counts for automatic calendars


class Notifier:
    def __init__(self):
        self.shown = []

    def show(self, problem):
        self.shown.append(problem.key)


class Feeds:
    def feed_statuses(self, calendars):
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


def test_without_a_working_card_a_call_still_starts_notes(gate):
    world, launches, run = gate
    world["accepted"] = None
    run(kind="call")
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

    monkeypatch.setattr(watcher, "modern_menus", lambda appearance: None)
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


# Granola already taking notes for the meeting

def test_granola_already_taking_notes_for_the_meeting_shows_no_card_and_starts_nothing(gate, caplog):
    world, launches, run = gate
    world["recordings"] = ["Design review"]
    control = watcher.Control()
    shown = []
    control.on_view = lambda view: shown.append(control.card_open)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = run(control)
    assert world["asked"] == [] and shown == [] and launches == []
    assert "Granola is already taking notes for Design review; no card" in caplog.text
    assert "countdown for" not in caplog.text
    # The answer is recorded, with the other meeting it covers, as notes that are running and that Granola was seen taking.
    assert {"event:abc", "event:other"} <= set(state.handled)
    assert state.active_launch.title == "Design review" and state.active_launch.opened and not state.active_launch.failed
    assert control.last_start is None  # General's Last start is for the app's own starts


@pytest.mark.parametrize("how", ["dry run", "paused", "off", "away", "locked"])
def test_granola_having_the_notes_is_a_fact_whatever_the_app_would_have_done_about_it(how, gate):
    world, launches, run = gate
    world["recordings"] = ["Design review"]
    control, settings = watcher.Control(), Settings()
    if how == "dry run":
        settings.dry_run = True
    elif how == "paused":
        control.set_pause(datetime.now().astimezone() + timedelta(hours=1))
    elif how == "off":
        control.set_notes(False)
    elif how == "away":
        world["idle"] = 99999.0
    else:
        world["unlocked"] = False
    state = run(control, settings)
    assert launches == [] and "event:abc" in state.handled and state.active_launch.opened


def test_granola_having_the_notes_is_not_recorded_when_the_app_is_quitting(gate):
    world, launches, run = gate
    world["recordings"] = ["Design review"]
    control = watcher.Control()
    control.stop.set()
    state = run(control)
    assert launches == [] and state.handled == {} and state.active_launch is None


def test_the_notes_that_granola_is_taking_for_a_call_are_followed_like_any_other_start(gate):
    world, _launches, run = gate
    world["recordings"] = ["Design review"]
    shown = Prompt(
        kind="call",
        title="Design review",
        payload={},
        keys=("zoom:9:100", "event:abc"),
        expires_at=NOW + timedelta(minutes=30),
        call_session="zoom:9:100",
    )
    state = run(shown=shown)
    assert state.active_launch.call_session == "zoom:9:100" and state.active_launch.opened
    assert set(state.handled) == {"zoom:9:100", "event:abc"}


def test_granola_taking_notes_for_the_meeting_ticked_in_a_list_answers_the_others_as_a_start_does(gate):
    world, launches, run = gate
    world["recordings"] = ["  renew PARKING permit "]
    state = run(shown=together())
    assert world["asked"] == [] and launches == []
    assert state.active_launch.title == "Renew parking permit" and state.active_launch.opened
    assert set(state.handled) == {"event:reminder", "event:standup"}


def test_a_note_that_is_not_the_meeting_s_or_not_known_for_sure_leaves_the_card_as_it_was(gate):
    world, launches, run = gate
    for recording in ("Roadmap review", "", None):
        world["recordings"] = [recording]
        state = run()
        assert len(world["asked"]) and "event:abc" in state.handled and state.active_launch.seen_running is False
    assert len(launches) == 3 and world["looks"] == 6  # each time once before the card, and once just before opening


def test_a_list_with_two_meetings_of_that_title_shows_the_card(gate):
    world, launches, run = gate
    world["recordings"] = ["Team stand-up"]
    shown = together()
    twin = dataclasses.replace(shown.choices[1], title="team stand-up")
    state = run(shown=dataclasses.replace(shown, choices=(shown.choices[0], twin)))
    assert len(world["asked"]) == 1 and len(launches) == 1 and state.active_launch.seen_running is False


def test_granola_starting_notes_during_the_countdown_is_recorded_and_opens_nothing(gate, monkeypatch):
    world, launches, run = gate
    world["recordings"] = [None, None, "Design review"]  # before the card, then two looks of the countdown

    def ask(title, seconds, kind, choices, granola_has):
        world["asked"].append((title, seconds, kind, choices))
        assert granola_has() is None
        return watcher.CardAnswer(True, granola_has(), granola=True)

    monkeypatch.setattr(watcher, "ask_notice", ask)
    control = watcher.Control()
    control.card_failed = True  # the card before this one couldn't run, and this one did
    shown = []
    control.on_view = lambda view: shown.append(control.card_open)
    state = run(control)
    assert shown == [True, False] and not control.card_failed and launches == []
    assert {"event:abc", "event:other"} <= set(state.handled) and state.active_launch.opened
    assert control.last_start is None


def test_the_countdown_polls_granola_for_the_meetings_the_card_lists(gate, monkeypatch):
    world, launches, run = gate
    world["recordings"] = [None, "Renew parking permit"]

    def ask(title, seconds, kind, choices, granola_has):
        return watcher.CardAnswer(True, granola_has(), granola=True)

    monkeypatch.setattr(watcher, "ask_notice", ask)
    state = run(shown=together())
    assert launches == [] and state.active_launch.title == "Renew parking permit" and state.active_launch.opened
    assert set(state.handled) == {"event:reminder", "event:standup"}


def test_granola_starting_notes_during_the_countdown_is_not_recorded_when_the_app_is_quitting(gate, monkeypatch):
    _world, launches, run = gate
    control = watcher.Control()
    control.stop.set()
    monkeypatch.setattr(watcher, "ask_notice", lambda *_args, **_kwargs: watcher.CardAnswer(True, 0, granola=True))
    state = run(control)
    assert launches == [] and state.handled == {}


def test_a_click_on_granola_s_button_as_the_countdown_ends_opens_nothing(gate, caplog):
    world, launches, run = gate
    world["recordings"] = [None, "Design review"]
    control = watcher.Control()
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = run(control)
    assert launches == [] and control.last_start is None
    assert {"event:abc", "event:other"} <= set(state.handled) and state.active_launch.opened
    assert "Granola started notes for Design review as the countdown ended; not opening it" in caplog.text


def test_a_click_as_the_countdown_ends_beats_start_now_and_covers_the_meeting_ticked(gate):
    world, launches, run = gate
    world["picked"] = 1
    world["recordings"] = [None, "Renew parking permit"]
    state = run(shown=together())
    assert launches == [] and state.active_launch.title == "Renew parking permit" and state.active_launch.opened


def test_a_meeting_ticked_on_the_card_is_started_even_if_granola_has_notes_for_another_by_then(gate, caplog):
    world, launches, run = gate
    world["picked"] = 1
    world["recordings"] = [None, "Team stand-up"]
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = run(shown=together())
    assert len(launches) == 1 and world["looks"] == 2
    assert state.active_launch.title == "Renew parking permit" and not state.active_launch.opened
    assert "as the countdown ended" not in caplog.text


# Granola still taking notes for another meeting, in a call that began before this one

def test_a_card_in_an_earlier_call_waits_while_granola_takes_notes_for_another_meeting_and_says_so_once(gate, caplog):
    world, launches, run = gate
    world["recordings"] = ["Standup", "Standup"]
    control = watcher.Control()
    shown = dataclasses.replace(prompt(), in_earlier_call=True)
    state = WatcherState()
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        for _tick in range(2):
            run(control, state=state, shown=shown)
    assert world["asked"] == [] and launches == [] and state.handled == {} and state.active_launch is None
    assert caplog.text.count("Granola is still taking notes for Standup in an earlier call; the card for Design review waits") == 1
    run(control, state=state, shown=shown)  # Granola has stopped those notes: the card comes up
    assert len(world["asked"]) == 1 and len(launches) == 1 and "event:abc" in state.handled and control.held is None


def test_a_card_held_again_later_says_so_again(gate, caplog):
    world, _launches, run = gate
    control = watcher.Control()
    shown = dataclasses.replace(prompt(), in_earlier_call=True)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        for recording in ("Standup", "Weekly sync", "Weekly sync"):
            world["recordings"] = [recording]
            run(control, shown=shown)
    assert world["asked"] == [] and caplog.text.count("Granola is still taking notes for") == 2


def test_a_card_outside_an_earlier_call_does_not_wait_for_granola_s_notes_for_another_meeting(gate):
    world, launches, run = gate
    world["recordings"] = ["Standup"]
    state = run()
    assert len(world["asked"]) == 1 and len(launches) == 1 and "event:abc" in state.handled


def test_a_card_in_an_earlier_call_with_granola_taking_its_own_notes_is_answered_by_them(gate):
    world, launches, run = gate
    world["recordings"] = ["Design review"]
    state = run(shown=dataclasses.replace(prompt(), in_earlier_call=True))
    assert world["asked"] == [] and launches == [] and state.active_launch.opened and "event:abc" in state.handled


def test_with_the_first_meeting_still_ticked_granola_s_notes_for_another_stand(gate):
    world, launches, run = gate
    world["recordings"] = [None, "Renew parking permit"]
    state = run(shown=together())
    assert launches == [] and state.active_launch.title == "Renew parking permit" and state.active_launch.opened


def test_a_card_cancelled_is_cancelled_even_if_granola_has_notes_by_then(gate):
    world, launches, run = gate
    world["accepted"] = False
    world["recordings"] = [None, "Design review"]
    state = run()
    assert launches == [] and "event:abc" in state.handled and state.active_launch is None
    assert world["looks"] == 1  # only before the card


def test_a_start_the_app_stops_short_of_is_not_checked_again_either(gate):
    world, launches, run = gate
    world["unlocked"] = False
    world["recordings"] = [None, "Design review"]
    state = run()
    assert launches == [] and state.handled == {} and world["looks"] == 1


def test_a_card_that_cannot_run_still_gives_way_to_granola_taking_notes(gate):
    world, launches, run = gate
    world["accepted"] = None
    world["recordings"] = [None, "Design review"]
    state = run()
    assert launches == [] and state.active_launch.opened
