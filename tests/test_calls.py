import ctypes
import os
import threading
import uuid
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import calls
from granola_autostart.calls import MAX_TITLE, Call, CallWatch, best_window_title

REAL_CAPTURE_PIDS = calls.capture_pids  # every test gets the fake microphone below instead

# What process_table returns: lower-cased image names, each with its process ids.
TABLE = {
    "cpthost.exe": [912, 604],
    "zoom.exe": [300],
    "granola.exe": [77],
    "ms-teams.exe": [70],
    "chrome.exe": [50, 51],
    "msedge.exe": [60],
    "shift.exe": [80, 81],
}
ALL_ON = {"zoom": True, "teams": True, "meet": True}
BOTH = {"meet": True, "teams": True}
# The process of each that records from the microphone. Chrome's and Shift's is a helper (the browser's audio
# service), not the one whose windows show the tabs.
CHROME, EDGE, SHIFT, TEAMS = 51, 60, 81, 70


class Clock:
    """calls' time.time(), which calls on the microphone are timed by. ``advance`` moves it on."""

    def __init__(self, now: float) -> None:
        self.now = now

    def time(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def filetime(self, seconds_ago: float = 0.0) -> int:
        return int((self.now - seconds_ago + calls.FILETIME_UNIX_EPOCH) * calls.FILETIME_TICKS)

    def started(self, seconds_ago: float) -> datetime:
        """What process_start says of a process that started that long ago."""
        return datetime.fromtimestamp(self.now - seconds_ago, timezone.utc)


class Mic:
    """Stands in for capture_pids: ``pids`` are the processes recording now, and ``broken`` makes the audio engine
    one that can't be asked. ``reads`` counts the times it was asked."""

    def __init__(self) -> None:
        self.pids: set[int] = set()
        self.broken = False
        self.reads = 0

    def __call__(self) -> set[int]:
        self.reads += 1
        if self.broken:
            raise OSError("the audio engine could not be asked")
        return set(self.pids)


@pytest.fixture(autouse=True)
def clock(monkeypatch) -> Clock:
    fake = Clock(1_790_000_000.0)  # any fixed moment (2026-09-21)
    monkeypatch.setattr(calls, "time", fake)
    return fake


@pytest.fixture(autouse=True)
def mic(monkeypatch) -> Mic:
    """No one is recording unless a test says so, and this PC's own microphone is never asked."""
    fake = Mic()
    monkeypatch.setattr(calls, "capture_pids", fake)
    return fake


def test_the_meeting_name_is_the_longest_title_that_is_not_zoom_itself():
    assert best_window_title(["Zoom Meeting", "Roadmap review", "Zoom"]) == "Roadmap review"
    assert best_window_title([" Meeting Chat ", "Roadmap"]) == "Roadmap"


def test_only_zoom_s_own_window_names_mean_no_meeting_name():
    assert best_window_title(["Zoom Workplace", "zoom webinar", "  ", ""]) is None
    assert best_window_title([]) is None


def test_a_very_long_title_is_shortened():
    assert best_window_title(["x" * 500]) == "x" * MAX_TITLE


def test_of_several_meeting_names_the_longest_wins_without_its_spaces():
    assert best_window_title(["Roadmap", "  Roadmap review  ", "Zoom"]) == "Roadmap review"


def test_processes_are_found_by_name_whatever_its_case():
    assert calls.pids_named("CptHost.exe", TABLE) == [912, 604]
    assert calls.pids_named("Teams.exe", TABLE) == []
    assert calls.granola_running(TABLE) and not calls.granola_running({})
def test_this_test_s_own_process_is_listed_with_its_start_time_and_its_parent():
    table = calls.process_table()
    assert any(os.getpid() in pids for pids in table.values())
    assert all(name == name.casefold() for name in table)
    assert table.parents[os.getpid()] == os.getppid()
    started = calls.process_start(os.getpid())
    assert datetime.now(timezone.utc) - timedelta(days=1) < started <= datetime.now(timezone.utc)


class Kernel32:
    """Toolhelp and process handles that Windows won't give out."""

    def __init__(self):
        self.snapshot = 0
        self.process = 0
        self.closed = []

    def CreateToolhelp32Snapshot(self, flags, pid):
        return self.snapshot

    def OpenProcess(self, access, inherit, pid):
        return self.process

    def GetProcessTimes(self, *_times):
        return False

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True


def test_a_process_list_or_start_time_windows_refuses_reads_as_none(monkeypatch):
    kernel32 = Kernel32()
    monkeypatch.setattr(calls, "kernel32", kernel32)
    assert calls.process_table() == {}
    kernel32.snapshot = calls.INVALID_HANDLE_VALUE
    assert calls.process_table() == {}
    assert calls.process_start(604) is None  # protected, or already gone
    kernel32.process = 0x88
    assert calls.process_start(604) is None
    assert kernel32.closed == [0x88]


class Windows:
    """user32's top-level windows, each (hwnd, pid, visible, title). Change ``windows`` to move a tab."""

    def __init__(self, *windows):
        self.windows = {hwnd: (pid, visible, title) for hwnd, pid, visible, title in windows}
        self.enumerations = 0

    def EnumWindows(self, callback, lparam):
        self.enumerations += 1
        for hwnd in list(self.windows):
            callback(hwnd, lparam)
        return True

    def IsWindowVisible(self, hwnd):
        return self.windows[hwnd][1]

    def GetWindowThreadProcessId(self, hwnd, pid):
        pid._obj.value = self.windows[hwnd][0]  # byref's target
        return 1

    def GetWindowTextLengthW(self, hwnd):
        return len(self.windows[hwnd][2])

    def GetWindowTextW(self, hwnd, buffer, size):
        buffer.value = self.windows[hwnd][2][: size - 1]
        return len(buffer.value)


class Denied:
    """Stands in for something a disabled provider must never touch."""

    def __getattr__(self, name):
        pytest.fail(f"{name} was used for a provider that is switched off")

# Windows' microphone settings, as winreg shows them: a key is a dict of subkeys. Only Settings' Teams hint reads them.

class Key:
    def __init__(self, node):
        self.node = node

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class Registry:
    def __init__(self, tree):
        self.HKEY_CURRENT_USER = Key(tree)

    def OpenKey(self, parent, path, *_args):
        node = parent.node
        for part in path.split("\\"):
            node = node.get(part) if isinstance(node, dict) else None
            if not isinstance(node, dict):
                raise FileNotFoundError(path)
        return Key(node)

    def EnumKey(self, key, index):
        names = list(key.node)
        if index >= len(names):
            raise OSError("no more data")
        return names[index]


class Refused(Registry):
    """Settings Windows refuses to open (a lock, or a policy)."""

    def OpenKey(self, parent, path, *_args):
        raise PermissionError(path)


def use_registry(monkeypatch, *packages) -> None:
    """Windows' microphone settings, listing these packaged apps."""
    tree = {name: {} for name in packages}
    for part in reversed(calls.CONSENT_KEY.split("\\")):
        tree = {part: tree}
    monkeypatch.setattr(calls, "winreg", Registry(tree))


def watching() -> CallWatch:
    """A CallWatch that has had its first look, with no one recording, so a use of the microphone it sees after that
    has just begun."""
    watch = CallWatch()
    assert watch.calls(BOTH, TABLE) == []
    return watch


def use_windows(monkeypatch, *windows) -> Windows:
    fake = Windows(*windows)
    monkeypatch.setattr(calls, "user32", fake)
    return fake


# Zoom

def test_a_zoom_meeting_is_its_first_cpthost_and_when_that_started(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=3)
    asked = []
    monkeypatch.setattr(calls, "process_start", lambda pid: asked.append(pid) or started)
    use_windows(monkeypatch, (2, 604, True, "Roadmap review"))
    call = calls.zoom_call(TABLE)
    assert asked == [604]
    assert call.provider == "zoom" and call.title == "Roadmap review"
    assert call.session_id == f"zoom:604:{int(started.timestamp())}" and 179 < call.age_seconds < 240


def test_without_cpthost_there_is_no_meeting_and_without_its_start_no_age(monkeypatch):
    assert calls.zoom_call({"zoom.exe": [300]}) is None
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    assert calls.zoom_call(TABLE) == Call("zoom", "zoom:604", None, None)


def test_a_meeting_that_seems_to_start_in_the_future_is_brand_new(monkeypatch):
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) + timedelta(minutes=5))
    use_windows(monkeypatch)
    assert calls.zoom_call(TABLE).age_seconds == 0.0


def test_the_meeting_name_comes_from_zoom_s_visible_titled_windows(monkeypatch):
    use_windows(
        monkeypatch,
        (1, 300, True, "Zoom Workplace"),
        (2, 604, True, "Roadmap review"),
        (3, 604, False, "Roadmap review and its hidden twin"),
        (4, 999, True, "A longer title in some other app"),
        (5, 300, True, ""),
    )
    assert calls.zoom_window_title(TABLE) == "Roadmap review"


def test_without_zoom_running_no_window_is_looked_at(monkeypatch):
    monkeypatch.setattr(calls, "process_table", lambda: {"granola.exe": [77]})
    monkeypatch.setattr(calls, "user32", SimpleNamespace())  # any window call would fail
    assert calls.zoom_window_title() is None
    assert calls.granola_running()


def test_zoom_s_windows_are_only_read_during_a_zoom_meeting(monkeypatch):
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current({"zoom": True}, {"granola.exe": [77]}) is None
# Microsoft Teams

def test_teams_is_in_a_call_while_it_records_and_the_call_began_when_it_was_first_seen(monkeypatch, mic, clock):
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    watch = watching()
    mic.pids = {TEAMS}
    began = clock.filetime()
    call = watch.current({"teams": True}, TABLE)
    assert (call.provider, call.session_id, call.title) == ("teams", f"teams:{began}", "Weekly sync")
    assert call.age_seconds == pytest.approx(0, abs=0.01)
    clock.advance(150)
    later = watch.current({"teams": True}, TABLE)
    assert later.session_id == call.session_id and later.age_seconds == pytest.approx(150, abs=0.01)


def test_teams_no_longer_recording_is_not_a_call(monkeypatch, mic):
    use_windows(monkeypatch)
    watch = watching()
    mic.pids = {TEAMS}
    assert watch.current({"teams": True}, TABLE) is not None
    mic.pids = set()
    monkeypatch.setattr(calls, "user32", Denied())  # with no call, no window is read
    assert watch.current({"teams": True}, TABLE) is None


def test_a_call_already_under_way_at_the_first_look_began_when_no_one_can_say(monkeypatch, mic, clock):
    use_windows(monkeypatch)
    mic.pids = {TEAMS}
    call = CallWatch().current({"teams": True}, TABLE)
    # Its id still has a start, when it was first seen, which tells it from the next call.
    assert call.session_id == f"teams:{clock.filetime()}" and call.age_seconds is None
    clock.advance(60)
    assert CallWatch().current({"teams": True}, TABLE).age_seconds is None


def test_teams_asks_for_the_process_list_itself_when_it_is_not_given_one(monkeypatch, mic):
    mic.pids = {TEAMS}
    use_windows(monkeypatch)
    monkeypatch.setattr(calls, "process_table", lambda: TABLE)
    assert [call.provider for call in CallWatch().calls({"teams": True})] == ["teams"]
    monkeypatch.setattr(calls, "process_table", lambda: {})
    assert CallWatch().calls({"teams": True}) == []


def test_only_teams_and_the_browsers_recording_can_be_calls(monkeypatch, mic):
    monkeypatch.setattr(calls, "user32", Denied())
    mic.pids = {300, 912, 77, 4242}  # Zoom, its meeting, Granola, and a process that isn't in the list
    assert CallWatch().calls(BOTH, TABLE) == []


def test_the_teams_app_s_meeting_name_is_looked_for_every_time_so_a_late_window_still_names_it(monkeypatch, mic):
    windows = use_windows(monkeypatch, (1, 70, True, "Calendar | Microsoft Teams"))
    watch = watching()
    mic.pids = {TEAMS}
    first = watch.current({"teams": True}, TABLE)
    assert first.title is None
    windows.windows[2] = (70, True, "Weekly sync | Microsoft Teams")
    later = watch.current({"teams": True}, TABLE)
    assert (later.session_id, later.title) == (first.session_id, "Weekly sync")


def test_teams_windows_name_the_meeting_unless_they_only_name_teams():
    assert calls.teams_window_title(["Weekly sync | Microsoft Teams", "Chat | Microsoft Teams"]) == "Weekly sync"
    assert calls.teams_window_title(["Calendar | Microsoft Teams", "Microsoft Teams", "Roadmap | microsoft teams"]) == "Roadmap"
    assert calls.teams_window_title(["Microsoft Teams", "Chat | Microsoft Teams", " "]) is None
    assert calls.teams_window_title([]) is None


def test_a_leading_unread_count_does_not_make_one_of_teams_own_pages_a_meeting_name():
    assert calls.teams_window_title(["(2) Chat | Microsoft Teams"]) is None
    assert calls.teams_window_title(["(12) Weekly sync | Microsoft Teams", "(2) Chat | Microsoft Teams"]) == "Weekly sync"
    for page in ("Meeting", "Meetings", "Home", "People", "Copilot", "Microsoft Teams (free)", "Microsoft Teams meeting"):
        assert calls.teams_window_title([f"{page} | Microsoft Teams", f"(4) {page} | Microsoft Teams"]) is None
    assert calls.teams_window_title(["Microsoft Teams meeting | Microsoft Teams", "Weekly sync | Microsoft Teams"]) == "Weekly sync"


def test_teams_main_window_titles_with_a_chat_or_channel_in_them_never_name_the_meeting():
    # Chat and channel titles keep a " | " after the suffix is gone, and can be longer than the meeting's own.
    titles = ["Chat | Alexandra Montgomery-Whitfield | Microsoft Teams", "Design team | General | Microsoft Teams", "Standup | Microsoft Teams"]
    assert calls.teams_window_title(titles) == "Standup"
    assert calls.teams_window_title(titles[:2]) is None

def test_teams_windows_are_read_once_a_tick_and_only_its_visible_ones_count(monkeypatch, mic):
    mic.pids = {TEAMS}
    windows = use_windows(
        monkeypatch,
        (1, 70, True, "Standup | Microsoft Teams"),
        (2, 70, False, "A hidden window | Microsoft Teams"),
        (3, 912, True, "Roadmap review"),  # Zoom's, not Teams'
    )
    assert CallWatch().current({"teams": True}, TABLE).title == "Standup"
    assert windows.enumerations == 1


# Google Meet

@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("Meet - Weekly sync - Google Chrome", "Weekly sync"),
        ("Meet \u2013 Weekly sync - Google Chrome", "Weekly sync"),
        ("Meet - Weekly sync - Microsoft Edge", "Weekly sync"),
        ("Meet - Weekly sync - Work - Microsoft\u200b Edge", "Weekly sync"),
        ("Meet - Weekly sync and 3 more pages - Work - Microsoft\u200b Edge", "Weekly sync"),
        ("Meet - Weekly sync and 1 more page - Personal - Microsoft\u200b Edge", "Weekly sync"),
        ("Meet - Weekly sync - Google Chrome - Work", "Weekly sync"),
        ("Meet - Weekly sync - Shift Browser", "Weekly sync"),
        ("Meet – Weekly sync - Shift Browser", "Weekly sync"),
        ("Meet - Q3 - planning - Shift Browser", "Q3 - planning"),
        ("Meet - Weekly sync and 2 more pages - Shift Browser", "Weekly sync"),
        ("Meet - abc-defg-hij - Shift Browser", None),
        ("Meet - Shift Browser", None),
        ("Meet - Q3 - planning - Google Chrome", "Q3 - planning"),
        ("Meet - Weekly sync", "Weekly sync"),  # an app window, with no browser name
        ("  Meet - Weekly sync - Google Chrome  ", "Weekly sync"),
        ("Meet - abc-defg-hij - Google Chrome", None),
        ("Meet - ABC-DEFG-HIJ - Microsoft\u200b Edge", None),
        ("Meet - abc-defg-hij and 2 more pages - Work - Microsoft\u200b Edge", None),
        ("Meet - Google Chrome", None),
        ("Meet - ", None),
        ("Meet - " + "x" * 500, "x" * MAX_TITLE),
        ("Google Meet - Weekly sync - Google Chrome", None),
        ("Weekly sync - Google Chrome", None),
        ("Meeting notes - Google Chrome", None),
        ("Meet", None),
    ],
)
def test_the_meeting_name_comes_from_the_meet_tab_s_window_title(window, expected):
    assert calls.meet_title(window) == expected


def test_only_a_title_that_starts_with_meet_and_a_dash_is_a_meet_tab():
    assert calls.is_meet_title("Meet - abc-defg-hij - Google Chrome")
    assert calls.is_meet_title("Meet \u2013 Weekly sync - Google Chrome")
    assert calls.is_meet_title("\u200bMeet - Weekly sync")
    assert calls.is_meet_title("Meet - abc-defg-hij - Shift Browser")
    assert not any(calls.is_meet_title(title) for title in ("Google Meet", "Meet", "Meeting - x", "Roadmap - Meet - x", "meet - x"))

def browser_world(monkeypatch, mic, *windows, recording=CHROME) -> Windows:
    """A browser recording from the microphone (its process ``recording``), with the windows it shows."""
    mic.pids = {recording}
    return use_windows(monkeypatch, *windows)


def test_a_browser_recording_in_front_of_a_meet_tab_is_a_meet_call(monkeypatch, mic, clock):
    watch = watching()
    browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    call = watch.current({"meet": True}, TABLE)
    assert (call.provider, call.title) == ("meet", "Weekly sync")
    assert call.session_id == f"meet:chrome:{clock.filetime()}" and call.age_seconds == pytest.approx(0, abs=0.01)
    clock.advance(20)
    assert watch.current({"meet": True}, TABLE).age_seconds == pytest.approx(20, abs=0.01)


def test_a_meet_call_in_edge_is_recognised_the_same_way(monkeypatch, mic, clock):
    browser_world(monkeypatch, mic, (1, 60, True, "Meet - abc-defg-hij - Work - Microsoft​ Edge"), recording=EDGE)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.session_id == f"meet:msedge:{clock.filetime()}" and call.title is None


def test_a_browser_recording_without_a_meet_tab_in_front_is_not_a_call(monkeypatch, mic):
    browser_world(
        monkeypatch,
        mic,
        (1, 50, True, "Voice recorder - Google Chrome"),
        (2, 50, False, "Meet - Hidden - Google Chrome"),  # not visible
        (3, 60, True, "Meet - Weekly sync - Microsoft​ Edge"),  # the other browser's
    )
    assert CallWatch().current({"meet": True}, TABLE) is None


def test_a_meet_call_is_decided_when_its_session_appears_and_kept_for_the_whole_session(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    # The user switches to another tab: the window now carries that tab's title.
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    later = watch.current({"meet": True}, TABLE)
    assert (later.session_id, later.title) == (first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # looked once, when the session was new


def test_a_session_that_was_not_a_meet_call_stays_not_a_call_even_if_a_meet_tab_comes_to_the_front(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Meet - Weekly sync - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None
    assert windows.enumerations == 1


def test_a_new_browser_session_is_looked_at_afresh_and_an_ended_one_is_forgotten(monkeypatch, mic, clock):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    assert first.title == "Weekly sync"
    mic.pids = set()
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    clock.advance(60)
    mic.pids = {CHROME}
    assert watch.current({"meet": True}, TABLE) is None  # the next use of the microphone isn't the old call
    assert windows.enumerations == 2
    mic.pids = set()
    watch.current({"meet": True}, TABLE)
    windows.windows[1] = (50, True, "Meet - Standup - Google Chrome")
    clock.advance(60)
    mic.pids = {CHROME}
    again = watch.current({"meet": True}, TABLE)
    assert (again.title, again.age_seconds) == ("Standup", pytest.approx(0, abs=0.01))
    assert again.session_id != first.session_id


def test_a_recording_process_that_is_not_in_the_process_list_is_no_one_s(monkeypatch, mic):
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    without_chrome = {name: pids for name, pids in TABLE.items() if name != "chrome.exe"}
    assert CallWatch().current({"meet": True}, without_chrome) is None  # it started after the list was made, say
    assert windows.enumerations == 0  # and no window was looked at


def test_a_session_that_ended_with_its_browser_is_forgotten(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    mic.pids = set()
    assert watch.current({"meet": True}, {"granola.exe": [77]}) is None  # the browser has gone
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    mic.pids = {CHROME}
    assert watch.current({"meet": True}, TABLE) is None  # decided afresh, as a new session is


def test_an_audio_engine_that_cannot_be_asked_this_time_keeps_the_meet_calls_known(monkeypatch, mic, clock):
    watch = watching()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    mic.broken = True
    for _ in range(2):
        clock.advance(10)
        kept = watch.current({"meet": True}, TABLE)
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "meet")
    assert kept.age_seconds == pytest.approx(20, abs=0.01)  # it goes on growing older
    mic.broken = False  # it can be asked again, and the browser is still recording: the same session
    again = watch.current({"meet": True}, TABLE)
    assert (again.session_id, again.title) == (first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # never looked at again
    mic.pids = set()  # a read that works is what ends it
    assert watch.current({"meet": True}, TABLE) is None


def test_a_process_list_that_could_not_be_read_keeps_the_meet_calls_known(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    for _ in range(2):
        kept = watch.current({"meet": True}, {})  # no list is not a list with no browser in it
        assert (kept.session_id, kept.title) == (first.session_id, "Weekly sync")
    assert mic.reads == 1  # the microphone isn't asked without a list to tell its processes by
    assert watch.current({"meet": True}, TABLE).session_id == first.session_id  # the same session, never looked at again
    assert windows.enumerations == 1
    assert watch.current({"meet": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it


def test_a_process_list_that_could_not_be_read_keeps_the_teams_call_known(monkeypatch, mic):
    watch = CallWatch()
    mic.pids = {TEAMS}
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    first = watch.current({"teams": True}, TABLE)
    monkeypatch.setattr(calls, "user32", Denied())  # kept, not read again
    kept = watch.current({"teams": True}, {})
    assert (kept.session_id, kept.title) == (first.session_id, "Weekly sync")
    mic.pids = set()
    assert watch.current({"teams": True}, {}) is not None  # still no list
    assert watch.current({"teams": True}, TABLE) is None  # a list that works sorts it out
    assert watch.current({"teams": True}, {}) is None  # and the call that ended isn't kept


def test_an_audio_engine_that_cannot_be_asked_this_time_keeps_the_teams_call_known(monkeypatch, mic):
    watch = CallWatch()
    mic.pids = {TEAMS}
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    first = watch.current({"teams": True}, TABLE)
    mic.broken = True
    kept = watch.current({"teams": True}, TABLE)
    assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    mic.broken = False  # it can be asked again, and still the same call
    assert watch.current({"teams": True}, TABLE).session_id == first.session_id
    mic.pids = set()  # a read that works is what ends it
    assert watch.current({"teams": True}, TABLE) is None


@pytest.mark.parametrize("by", ["process list", "audio engine"])
def test_a_teams_call_kept_through_a_failed_read_goes_on_growing_older(monkeypatch, mic, clock, by):
    use_windows(monkeypatch)
    watch = watching()
    mic.pids = {TEAMS}
    first = watch.current({"teams": True}, TABLE)
    clock.advance(400)
    mic.broken = by == "audio engine"
    table = TABLE if by == "audio engine" else {}
    kept = watch.current({"teams": True}, table)
    assert kept.session_id == first.session_id and kept.age_seconds == pytest.approx(400, abs=0.01)
    clock.advance(300)
    assert watch.current({"teams": True}, table).age_seconds == pytest.approx(700, abs=0.01)


def test_only_a_zoom_call_kept_has_its_age_worked_out_again_from_its_id():
    for session_id in ("zoom:604", "zoom:604:soon", "teams:1", "teams:soon", "teams", "meet:chrome:1", "teams:shift:1"):
        call = Call(session_id.split(":")[0], session_id, 12.0, None)
        assert calls._reaged(call) is call


def test_an_audio_engine_that_cannot_be_asked_knows_no_call_that_was_never_seen(monkeypatch, mic):
    monkeypatch.setattr(calls, "user32", Denied())
    mic.pids = {TEAMS, CHROME}
    mic.broken = True
    watch = CallWatch()
    assert watch.calls(BOTH, TABLE) == []
    # Nor does it keep a session that wasn't a call.
    mic.broken = False
    mic.pids = {CHROME}
    windows = use_windows(monkeypatch, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.calls({"meet": True}, TABLE) == [] and windows.enumerations == 1
    mic.broken = True
    assert watch.calls({"meet": True}, TABLE) == []


def test_zoom_keeps_its_call_while_the_process_list_cannot_be_read(monkeypatch):
    watch = CallWatch()
    started = datetime.now(timezone.utc) - timedelta(minutes=3)
    monkeypatch.setattr(calls, "process_start", lambda pid: started)
    use_windows(monkeypatch, (1, 604, True, "Roadmap review"))
    first = watch.current({"zoom": True}, TABLE)
    monkeypatch.setattr(calls, "user32", Denied())  # kept, not looked at again
    for _ in range(2):
        kept = watch.current({"zoom": True}, {})  # no list is not a list with no meeting in it
        assert (kept.session_id, kept.title) == (first.session_id, "Roadmap review")
    use_windows(monkeypatch, (1, 604, True, "Roadmap review"))
    assert watch.current({"zoom": True}, TABLE).session_id == first.session_id
    assert watch.current({"zoom": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it
    assert watch.current({"zoom": True}, {}) is None  # and the meeting that ended isn't kept


def test_a_zoom_call_kept_through_an_unreadable_process_list_goes_on_growing_older(monkeypatch):
    watch = CallWatch()
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    monkeypatch.setattr(calls, "process_start", lambda pid: started)
    use_windows(monkeypatch, (1, 604, True, "Roadmap review"))
    first = watch.current({"zoom": True}, TABLE)
    assert first.age_seconds < 40
    monkeypatch.setattr(calls, "time", SimpleNamespace(time=lambda: started.timestamp() + 500))
    kept = watch.current({"zoom": True}, {})
    assert (kept.session_id, kept.title) == (first.session_id, "Roadmap review")
    assert 499 < kept.age_seconds < 501  # not the 30 seconds it had when it was read


def test_a_zoom_call_whose_id_has_no_start_keeps_the_age_it_had_when_kept(monkeypatch):
    watch = CallWatch()
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    first = watch.current({"zoom": True}, TABLE)
    assert first.age_seconds is None
    assert watch.current({"zoom": True}, {}) == first


def test_a_zoom_meeting_never_seen_is_not_made_up_from_a_process_list_that_could_not_be_read():
    assert CallWatch().calls({"zoom": True}, {}) == []


def test_turning_zoom_off_forgets_its_call(monkeypatch):
    watch = CallWatch()
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    assert watch.current({"zoom": True}, TABLE) is not None
    assert watch.current({"zoom": False}, TABLE) is None
    assert watch.current({"zoom": True}, {}) is None


def test_a_call_never_seen_is_not_made_up_from_a_process_list_that_could_not_be_read(monkeypatch, mic):
    monkeypatch.setattr(calls, "user32", Denied())
    mic.pids = {TEAMS, CHROME}
    assert CallWatch().calls(ALL_ON, {}) == []
    assert mic.reads == 0


def test_turning_teams_off_forgets_its_call(monkeypatch, mic):
    watch = CallWatch()
    mic.pids = {TEAMS}
    use_windows(monkeypatch)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"teams": False}, TABLE) is None
    assert watch.current({"teams": True}, {}) is None


def test_with_two_meet_tabs_the_named_one_gives_the_title(monkeypatch, mic):
    browser_world(
        monkeypatch,
        mic,
        (1, 50, True, "Meet - abc-defg-hij - Google Chrome"),
        (2, 51, True, "Meet - Weekly sync - Google Chrome"),
    )
    assert CallWatch().current({"meet": True}, TABLE).title == "Weekly sync"


def test_a_meet_call_in_shift_is_recognised_the_same_way(monkeypatch, mic, clock):
    browser_world(monkeypatch, mic, (1, 80, True, "Meet - Weekly sync - Shift Browser"), recording=SHIFT)
    call = CallWatch().current({"meet": True}, TABLE)
    assert (call.provider, call.title) == ("meet", "Weekly sync")
    assert call.session_id == f"meet:shift:{clock.filetime()}"


def test_only_chrome_edge_and_shift_count_as_browsers(monkeypatch, mic):
    table = {**TABLE, "notchrome.exe": [90], "notshift.exe": [91], "shift.exe.bak": [92], "firefox.exe": [93]}
    mic.pids = {300, 90, 91, 92, 93}
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current(BOTH, table) is None


# Microsoft Teams, in a browser

@pytest.mark.parametrize(
    ("window", "expected"),
    [
        ("Weekly sync | Microsoft Teams - Shift Browser", "Weekly sync"),
        ("(3) Weekly sync | Microsoft Teams - Shift Browser", "Weekly sync"),
        ("Weekly sync | Microsoft Teams - Google Chrome - Work", "Weekly sync"),
        ("Weekly sync | Microsoft Teams - Work - Microsoft​ Edge", "Weekly sync"),
        ("Weekly sync | Microsoft Teams and 2 more pages - Work - Microsoft​ Edge", "Weekly sync"),
        ("Weekly sync | Microsoft Teams", "Weekly sync"),  # an installed web app's window, with no browser name
        ("(3) Weekly sync | Microsoft Teams", "Weekly sync"),
        ("Weekly sync | Microsoft Teams (free) - Shift Browser", "Weekly sync"),
        ("Q3 - planning | Microsoft Teams - Shift Browser", "Q3 - planning"),
        # Edge with no profile in its title: the last " - " is the page's own.
        ("Q3 - planning | Microsoft Teams - Microsoft​ Edge", "Q3 - planning"),
        ("Q3 - planning | Microsoft Teams and 2 more pages - Microsoft​ Edge", "Q3 - planning"),
        ("Q3 - planning | Microsoft Teams - Work - Microsoft​ Edge", "Q3 - planning"),
        ("  Weekly sync | microsoft teams - Shift Browser  ", "Weekly sync"),
        ("x" * 500 + " | Microsoft Teams - Shift Browser", "x" * MAX_TITLE),
        # Teams' own pages are Teams tabs that name no meeting, and so are chats and channels.
        ("(3) Chat | Microsoft Teams - Shift Browser", None),
        ("Calendar | Microsoft Teams and 2 more pages - Work - Microsoft​ Edge", None),
        ("Chat | Jane Doe | Microsoft Teams - Shift Browser", None),
        ("Design team | General | Microsoft Teams", None),
        ("Microsoft Teams - Shift Browser", None),
        ("(2) Microsoft Teams - Google Chrome - Work", None),
        ("Microsoft Teams (free) - Shift Browser", None),
        ("Home | Microsoft Teams (free)", None),
        ("Microsoft Teams", None),
        # Seen in Shift: a meeting's page, and the page that joins one by its ID.
        ("Microsoft Teams meeting | Microsoft Teams - Shift Browser", None),
        ("Join a Microsoft Teams Meeting by ID | Microsoft Teams - Shift Browser", None),
    ],
)
def test_the_meeting_name_comes_from_a_teams_tab_s_window_title(window, expected):
    assert calls.is_teams_web_title(window)
    assert calls.teams_web_title(window) == expected


@pytest.mark.parametrize(
    "window",
    [
        "(1) WhatsApp - Shift Browser",
        "Weekly sync - Shift Browser",
        "Meet - Weekly sync - Shift Browser",
        "Microsoft Teams admin center - Google Chrome",
        "How to use Microsoft Teams - Shift Browser",
        "Teams | Microsoft Teams Blog - Shift Browser",
        "Notes on Microsoft Teams",
        "",
        "   ",
    ],
)
def test_a_window_that_is_not_showing_teams_is_not_a_teams_tab(window):
    assert not calls.is_teams_web_title(window)
    assert calls.teams_web_title(window) is None


def test_an_ordinary_shift_tab_is_neither_a_meet_nor_a_teams_tab():
    window = "(1) WhatsApp - Shift Browser"
    assert not calls.is_meet_title(window) and calls.meet_title(window) is None
    assert not calls.is_teams_web_title(window) and calls.teams_web_title(window) is None

@pytest.mark.parametrize(
    ("recording", "pid", "window", "exe"),
    [
        (CHROME, 50, "Weekly sync | Microsoft Teams - Google Chrome - Work", "chrome"),
        (EDGE, 60, "Weekly sync | Microsoft Teams - Work - Microsoft​ Edge", "msedge"),
        (SHIFT, 80, "Weekly sync | Microsoft Teams - Shift Browser", "shift"),
        (EDGE, 60, "Weekly sync | Microsoft Teams", "msedge"),  # an installed web app's window
    ],
)
def test_a_browser_recording_in_front_of_a_teams_tab_is_a_teams_call(monkeypatch, mic, clock, recording, pid, window, exe):
    watch = watching()
    browser_world(monkeypatch, mic, (1, pid, True, window), recording=recording)
    call = watch.current({"teams": True}, TABLE)
    assert (call.provider, call.title) == ("teams", "Weekly sync")
    assert call.session_id == f"teams:{exe}:{clock.filetime()}" and call.age_seconds == pytest.approx(0, abs=0.01)


def test_a_teams_tab_that_names_no_meeting_is_still_a_teams_call_without_a_name(monkeypatch, mic):
    browser_world(monkeypatch, mic, (1, 80, True, "(3) Chat | Microsoft Teams - Shift Browser"), recording=SHIFT)
    call = CallWatch().current({"teams": True}, TABLE)
    assert call.provider == "teams" and call.title is None


def test_with_several_teams_windows_the_first_that_names_a_meeting_gives_the_title(monkeypatch, mic):
    browser_world(
        monkeypatch,
        mic,
        (1, 80, True, "Chat | Jane Doe | Microsoft Teams - Shift Browser"),
        (2, 80, True, "Calendar | Microsoft Teams - Shift Browser"),
        (3, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"),
        (4, 81, True, "Standup | Microsoft Teams - Shift Browser"),
        recording=SHIFT,
    )
    assert CallWatch().current({"teams": True}, TABLE).title == "Weekly sync"


def test_a_browser_recording_in_front_of_neither_meet_nor_teams_is_not_a_call(monkeypatch, mic):
    windows = browser_world(
        monkeypatch,
        mic,
        (1, 80, True, "(1) WhatsApp - Shift Browser"),
        (2, 80, False, "Weekly sync | Microsoft Teams - Shift Browser"),  # not visible
        (3, 50, True, "Weekly sync | Microsoft Teams - Google Chrome"),  # the other browser's
        (4, 60, True, "Meet - Standup - Microsoft​ Edge"),
        recording=SHIFT,
    )
    assert CallWatch().calls(BOTH, TABLE) == []
    assert windows.enumerations == 1


def test_a_meet_tab_in_front_makes_it_a_meet_call_even_with_a_teams_tab_in_front_too(monkeypatch, mic, clock):
    teams = (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser")  # the first window, and still not the call
    browser_world(monkeypatch, mic, teams, (2, 81, True, "Meet - Standup - Shift Browser"), recording=SHIFT)
    call = CallWatch().current(BOTH, TABLE)
    assert (call.provider, call.title) == ("meet", "Standup")
    assert call.session_id == f"meet:shift:{clock.filetime()}"


def test_a_meet_call_with_only_a_meeting_code_has_no_name_and_does_not_borrow_teams_one(monkeypatch, mic):
    teams = (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser")
    browser_world(monkeypatch, mic, teams, (2, 80, True, "Meet - abc-defg-hij - Shift Browser"), recording=SHIFT)
    call = CallWatch().current(BOTH, TABLE)
    assert (call.provider, call.title) == ("meet", None)


def test_a_teams_call_in_a_browser_is_decided_when_its_session_appears_and_kept_for_the_whole_session(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    first = watch.current({"teams": True}, TABLE)
    for tab in ("(1) WhatsApp - Shift Browser", "Meet - Standup - Shift Browser"):
        windows.windows[1] = (80, True, tab)  # the user switches to another tab
        later = watch.current(BOTH, TABLE)
        assert (later.provider, later.session_id, later.title) == ("teams", first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # looked once, when the session was new


def test_a_session_in_front_of_neither_stays_not_a_call_even_if_a_teams_tab_comes_to_the_front(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "(1) WhatsApp - Shift Browser"), recording=SHIFT)
    assert watch.calls(BOTH, TABLE) == []
    windows.windows[1] = (80, True, "Weekly sync | Microsoft Teams - Shift Browser")
    assert watch.calls(BOTH, TABLE) == []
    assert windows.enumerations == 1


def test_a_teams_session_that_ended_is_forgotten_and_the_next_one_is_looked_at_afresh(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"
    mic.pids = set()
    assert watch.current({"teams": True}, TABLE) is None
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    mic.pids = {SHIFT}
    assert watch.current({"teams": True}, TABLE) is None  # the next use of the microphone isn't the old call
    assert windows.enumerations == 2


# The switches, for calls on the microphone

def switch_world(monkeypatch, mic) -> Windows:
    """Teams in Shift and Meet in Chrome, both recording."""
    mic.pids = {SHIFT, CHROME}
    return use_windows(
        monkeypatch,
        (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"),
        (2, 50, True, "Meet - Standup - Google Chrome"),
    )


@pytest.mark.parametrize(
    ("enabled", "reported"),
    [
        ({"teams": True}, [("teams", "Weekly sync")]),
        ({"meet": True}, [("meet", "Standup")]),
        ({"teams": True, "meet": True}, [("meet", "Standup"), ("teams", "Weekly sync")]),
        ({"teams": True, "meet": False}, [("teams", "Weekly sync")]),
        ({"teams": False, "meet": True}, [("meet", "Standup")]),
        ({"teams": False, "meet": False}, []),
    ],
)
def test_a_call_in_a_browser_is_reported_only_while_its_own_provider_is_switched_on(monkeypatch, mic, enabled, reported):
    switch_world(monkeypatch, mic)
    found = CallWatch().calls(enabled, TABLE)
    assert [(call.provider, call.title) for call in found] == reported


def test_a_meet_and_a_teams_tab_in_front_make_a_meet_call_that_only_the_meet_switch_reports(monkeypatch, mic):
    browser_world(
        monkeypatch,
        mic,
        (1, 50, True, "Weekly sync | Microsoft Teams - Google Chrome"),
        (2, 50, True, "Meet - Standup - Google Chrome"),
    )
    assert CallWatch().calls({"teams": True}, TABLE) == []  # not a Teams call, though a Teams tab was in front
    assert [call.provider for call in CallWatch().calls({"meet": True}, TABLE)] == ["meet"]


def test_a_session_is_decided_whichever_switches_are_on_when_it_appears(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    assert watch.calls({"meet": True}, TABLE) == []  # looked at here, with Teams off
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    call = watch.current(BOTH, TABLE)
    assert (call.provider, call.title) == ("teams", "Weekly sync")
    assert windows.enumerations == 1


def test_turning_meet_or_teams_off_alone_keeps_what_a_browser_session_decided(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"meet": True}, TABLE) is None  # Teams is off, so its call isn't reported
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"  # and it was kept, not decided again
    assert windows.enumerations == 1


def test_turning_meet_and_teams_both_off_forgets_what_a_browser_session_decided(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"teams": False, "meet": False}, TABLE) is None
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    assert watch.current({"teams": True}, TABLE) is None  # decided afresh, with another tab in front
    assert windows.enumerations == 2


def test_the_microphone_is_not_asked_unless_meet_or_teams_is_on(monkeypatch, mic):
    monkeypatch.setattr(calls, "user32", Denied())
    mic.pids = {TEAMS, CHROME}
    assert CallWatch().calls({"zoom": True}, {"granola.exe": [77]}) == []
    assert CallWatch().calls({"zoom": True, "teams": False, "meet": False}, {"granola.exe": [77]}) == []
    assert mic.reads == 0


def test_a_teams_call_in_the_desktop_app_and_one_in_a_browser_are_both_reported(monkeypatch, mic, clock):
    use_windows(
        monkeypatch,
        (1, 70, True, "Standup | Microsoft Teams"),
        (2, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"),
    )
    watch = watching()
    mic.pids = {TEAMS}
    desktop = clock.filetime()
    watch.calls({"teams": True}, TABLE)
    clock.advance(260)
    mic.pids = {TEAMS, SHIFT}
    web = clock.filetime()
    found = watch.calls({"teams": True}, TABLE)
    assert [(call.provider, call.session_id, call.title) for call in found] == [
        ("teams", f"teams:{desktop}", "Standup"),
        ("teams", f"teams:shift:{web}", "Weekly sync"),
    ]
    assert watch.current({"teams": True}, TABLE).session_id == f"teams:shift:{web}"  # the newer
    assert watch.calls({"meet": True}, TABLE) == []  # with Teams off neither Teams call is reported


def test_a_process_list_that_could_not_be_read_keeps_the_teams_call_in_a_browser_known(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    first = watch.current({"teams": True}, TABLE)
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    for _ in range(2):
        kept = watch.current({"teams": True}, {})  # no list is not a list with no browser in it
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"  # the same session, never looked at again
    assert windows.enumerations == 1
    assert watch.current({"teams": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it
    assert watch.current({"teams": True}, {}) is None  # and the call that ended isn't kept


def test_an_audio_engine_that_cannot_be_asked_this_time_keeps_the_teams_call_in_a_browser_known(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    first = watch.current({"teams": True}, TABLE)
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    mic.broken = True
    kept = watch.current({"teams": True}, TABLE)
    assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    mic.broken = False  # it can be asked again, and still the same session
    assert watch.current({"teams": True}, TABLE).session_id == first.session_id
    assert windows.enumerations == 1
    mic.pids = set()  # a read that works is what ends it
    assert watch.current({"teams": True}, TABLE) is None


@pytest.mark.parametrize("by", ["process list", "audio engine"])
def test_sessions_kept_through_a_failed_read_are_reported_only_while_their_provider_is_on(monkeypatch, mic, by):
    watch = CallWatch()
    switch_world(monkeypatch, mic)
    assert len(watch.calls(BOTH, TABLE)) == 2
    mic.broken = by == "audio engine"
    table = TABLE if by == "audio engine" else {}
    assert [call.provider for call in watch.calls({"teams": True}, table)] == ["teams"]
    assert [call.provider for call in watch.calls({"meet": True}, table)] == ["meet"]
    assert [call.provider for call in watch.calls(BOTH, table)] == ["meet", "teams"]


@pytest.mark.parametrize("by", ["process list", "audio engine"])
def test_a_teams_call_in_a_browser_kept_through_a_failed_read_goes_on_growing_older(monkeypatch, mic, clock, by):
    watch = watching()
    browser_world(monkeypatch, mic, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), recording=SHIFT)
    first = watch.current({"teams": True}, TABLE)
    clock.advance(400)
    mic.broken = by == "audio engine"
    table = TABLE if by == "audio engine" else {}
    kept = watch.current({"teams": True}, table)
    assert kept.session_id == first.session_id and kept.age_seconds == pytest.approx(400, abs=0.01)
    clock.advance(300)
    assert watch.current({"teams": True}, table).age_seconds == pytest.approx(700, abs=0.01)


# Which provider, and which call

def test_of_several_calls_the_most_recently_started_wins(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) - timedelta(minutes=2))
    use_windows(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), (2, 912, True, "Roadmap"))
    watch = watching()
    mic.pids = {TEAMS}
    watch.calls({"teams": True}, TABLE)
    clock.advance(260)
    mic.pids = {TEAMS, CHROME}
    watch.calls(BOTH, TABLE)
    clock.advance(40)
    assert [call.provider for call in watch.calls(ALL_ON, TABLE)] == ["zoom", "teams", "meet"]
    assert watch.current(ALL_ON, TABLE).provider == "meet"  # 40 seconds old
    assert watch.current({"zoom": True, "teams": True}, TABLE).provider == "zoom"  # 2 minutes, to Teams' 5
    assert watch.current({"teams": True}, TABLE).provider == "teams"


def test_a_call_with_no_known_start_ranks_behind_one_that_has_one(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    watch = watching()
    mic.pids = {TEAMS}
    watch.calls({"teams": True}, TABLE)
    clock.advance(3000)
    assert watch.current({"zoom": True, "teams": True}, TABLE).provider == "teams"
    assert CallWatch().current({"zoom": True}, TABLE).provider == "zoom"
    # A call already under way at the first look has no known start either, so it ranks behind Zoom's.
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) - timedelta(hours=5))
    assert CallWatch().current({"zoom": True, "teams": True}, TABLE).provider == "zoom"


def test_the_process_list_is_read_once_for_every_provider_when_none_is_given(monkeypatch, mic):
    mic.pids = {TEAMS, CHROME}
    use_windows(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), (2, 912, True, "Roadmap"))
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) - timedelta(minutes=2))
    reads = []
    monkeypatch.setattr(calls, "process_table", lambda: reads.append(1) or TABLE)
    assert [call.provider for call in CallWatch().calls(ALL_ON)] == ["zoom", "teams", "meet"]
    assert reads == [1]


def test_with_no_call_there_is_nothing_to_find():
    assert CallWatch().current(ALL_ON, {"granola.exe": [77]}) is None


def test_a_provider_that_is_off_or_not_mentioned_is_not_looked_at_at_all(monkeypatch):
    monkeypatch.setattr(calls, "capture_pids", lambda: pytest.fail("the microphone was asked"))
    monkeypatch.setattr(calls, "user32", Denied())
    monkeypatch.setattr(calls, "kernel32", Denied())
    monkeypatch.setattr(calls, "process_table", lambda: pytest.fail("the process list was read"))
    watch = CallWatch()
    assert watch.calls({"zoom": False, "teams": False, "meet": False}) == []
    assert watch.calls({}) == []
    assert watch.current({"zoom": False}) is None


def test_turning_meet_off_forgets_what_it_decided(monkeypatch, mic):
    watch = CallWatch()
    windows = browser_world(monkeypatch, mic, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    assert watch.current({"meet": False}, TABLE) is None
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None  # decided afresh, with another tab in front


# Which app is recording

# Teams draws in web views, which may be what records. Outlook (olk.exe) has web views too. Windows reuses ids, so a
# process's parent can have gone, or two can each be the other's.
FAMILY = calls.ProcessTable(
    {**TABLE, "msedgewebview2.exe": [71, 72, 73, 95, 96, 97, 98], "ms-teams_modulehost.exe": [74], "olk.exe": [94]},
    parents={70: 4, 71: 70, 72: 71, 73: 72, 74: 70, 95: 94, 96: 9999, 97: 98, 98: 97, 300: 50},
)


@pytest.mark.parametrize(
    ("recording", "users"),
    [
        ({70}, {"teams": {70}}),
        ({74}, {"teams": {74}}),  # another of the Teams app's own processes
        ({73}, {"teams": {73}}),  # a web view of a web view of Teams
        ({71, 73}, {"teams": {71, 73}}),
        ({95}, {}),  # Outlook's web view
        ({96}, {}),  # a web view whose parent has gone
        ({97}, {}),  # web views that are each other's parents aren't followed for ever
        ({300}, {}),  # Zoom, though Chrome started it from a link
        ({51, 60, 81, 77}, {"chrome": {51}, "msedge": {60}, "shift": {81}}),  # Granola isn't a call
        ({4242}, {}),  # a process that isn't in the list
        (set(), {}),
    ],
)
def test_each_recording_process_is_put_down_to_its_app(mic, recording, users):
    mic.pids = recording
    assert calls.mic_users(FAMILY) == users


def test_with_no_parents_known_only_a_process_s_own_name_counts(mic):
    mic.pids = {70, 71}
    assert calls.mic_users({**TABLE, "msedgewebview2.exe": [71]}) == {"teams": {70}}


def test_teams_recording_through_its_web_view_is_a_teams_call(monkeypatch, mic):
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    mic.pids = {73}
    call = CallWatch().current({"teams": True}, FAMILY)
    assert (call.provider, call.session_id.split(":")[0], call.title) == ("teams", "teams", "Weekly sync")


# A restart in the middle of a call

def test_a_meet_call_still_under_way_after_a_restart_keeps_its_id_and_its_age(monkeypatch, mic, clock):
    began = clock.filetime(600)
    monkeypatch.setattr(calls, "process_start", lambda pid: clock.started(3600))
    browser_world(monkeypatch, mic, (1, 80, True, "Meet - Weekly sync - Shift Browser"), recording=SHIFT)
    call = CallWatch([f"meet:shift:{began}"]).current({"meet": True}, TABLE)
    assert (call.session_id, call.title) == (f"meet:shift:{began}", "Weekly sync")
    assert call.age_seconds == pytest.approx(600, abs=0.01)


def test_the_saved_call_says_whether_it_is_meet_or_teams_and_only_its_own_tab_names_it(monkeypatch, mic, clock):
    saved = [f"teams:shift:{clock.filetime(600)}"]
    monkeypatch.setattr(calls, "process_start", lambda pid: None)  # Windows won't say: the saved call is taken
    windows = browser_world(monkeypatch, mic, (1, 80, True, "(1) WhatsApp - Shift Browser"), recording=SHIFT)
    call = CallWatch(saved).current(BOTH, TABLE)
    assert (call.provider, call.session_id, call.title) == ("teams", saved[0], None)
    windows.windows[1] = (80, True, "Meet - Standup - Shift Browser")
    assert (CallWatch(saved).current(BOTH, TABLE).provider, CallWatch(saved).current(BOTH, TABLE).title) == ("teams", None)
    windows.windows[1] = (80, True, "Weekly sync | Microsoft Teams - Shift Browser")
    assert CallWatch(saved).current(BOTH, TABLE).title == "Weekly sync"


def test_a_saved_call_from_before_the_recording_process_started_is_not_this_one(monkeypatch, mic, clock):
    began = clock.filetime(600)
    monkeypatch.setattr(calls, "process_start", lambda pid: clock.started(300))  # the PC restarted since, say
    browser_world(monkeypatch, mic, (1, 80, True, "Meet - Weekly sync - Shift Browser"), recording=SHIFT)
    call = CallWatch([f"meet:shift:{began}"]).current({"meet": True}, TABLE)
    assert call.session_id == f"meet:shift:{clock.filetime()}" and call.age_seconds is None


def test_of_the_saved_calls_the_newest_that_fits_this_app_is_taken(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: clock.started(3600))
    mic.pids = {TEAMS}
    use_windows(monkeypatch)
    newest = clock.filetime(100)
    saved = [
        f"teams:{clock.filetime(900)}",
        f"teams:{newest}",
        f"teams:{clock.filetime(-60)}",  # begun after now: not this call
        f"teams:{clock.filetime(7200)}",  # begun before Teams' processes started
        f"teams:shift:{clock.filetime(50)}",  # in a browser
        f"meet:chrome:{clock.filetime(10)}",
        "zoom:604:1790000000",
        "Weekly sync|20260921T090000Z",  # a calendar meeting's key
    ]
    call = CallWatch(saved).current({"teams": True}, TABLE)
    assert call.session_id == f"teams:{newest}" and call.age_seconds == pytest.approx(100, abs=0.01)


def test_a_saved_call_is_only_taken_at_the_first_look(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    watch = CallWatch([f"teams:{clock.filetime(600)}"])
    assert watch.calls(BOTH, TABLE) == []  # no one recording
    mic.pids = {TEAMS}
    call = watch.current({"teams": True}, TABLE)
    assert call.session_id == f"teams:{clock.filetime()}" and call.age_seconds == pytest.approx(0, abs=0.01)


def test_the_calls_under_way_to_save_are_every_call_on_the_microphone_whichever_switches_are_on(monkeypatch, mic, clock):
    use_windows(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), (2, 80, True, "(1) WhatsApp - Shift Browser"))
    watch = watching()
    assert watch.under_way() == []
    mic.pids = {TEAMS, CHROME, SHIFT}  # Shift with neither tab in front isn't a call
    began = clock.filetime()
    assert [call.provider for call in watch.calls({"meet": True}, TABLE)] == ["meet"]  # Teams is off, and still saved
    assert watch.under_way() == [f"teams:{began}", f"meet:chrome:{began}"]
    # Saved and given to the watch after a restart, each is still the same call.
    saved = watch.under_way()
    monkeypatch.setattr(calls, "process_start", lambda pid: clock.started(3600))
    clock.advance(30)
    again = CallWatch(saved).calls(BOTH, TABLE)
    assert [(call.session_id, call.age_seconds) for call in again] == [
        (f"teams:{began}", pytest.approx(30, abs=0.01)),
        (f"meet:chrome:{began}", pytest.approx(30, abs=0.01)),
    ]


def test_what_is_under_way_is_not_known_after_a_look_that_could_not_read_the_microphone(mic):
    watch = watching()
    mic.pids = {TEAMS}
    watch.calls({"teams": True}, TABLE)
    known = watch.under_way()
    assert len(known) == 1
    mic.broken = True  # the call is kept, but it may have ended
    assert watch.calls({"teams": True}, TABLE) != [] and watch.under_way() is None
    assert watch.calls({"teams": True}, {}) != [] and watch.under_way() is None  # nor with no process list
    mic.broken = False
    watch.calls({"teams": True}, TABLE)
    assert watch.under_way() == known
    watch.calls({"meet": False, "teams": False}, TABLE)  # with Meet and Teams off, none is under way that it knows of
    assert watch.under_way() == []


def test_saved_calls_are_forgotten_after_the_first_look_so_meet_and_teams_back_on_do_not_take_one(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    watch = CallWatch([f"teams:{clock.filetime(600)}"])
    assert watch.calls(BOTH, TABLE) == []  # the first look, with no one recording
    assert watch.calls({"meet": False, "teams": False}, TABLE) == []
    mic.pids = {TEAMS}
    call = watch.current({"teams": True}, TABLE)  # a first look again, but not after a restart
    assert call.session_id == f"teams:{clock.filetime()}" and call.age_seconds is None


def test_a_first_look_that_fails_keeps_the_saved_calls_for_the_next(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    saved = f"teams:{clock.filetime(600)}"
    watch = CallWatch([saved])
    mic.pids = {TEAMS}
    mic.broken = True
    assert watch.calls({"teams": True}, TABLE) == []
    mic.broken = False
    assert watch.current({"teams": True}, TABLE).session_id == saved


def test_meet_and_teams_switched_off_after_a_first_look_that_failed_forget_the_saved_calls(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    watch = CallWatch([f"teams:{clock.filetime(600)}"])
    mic.pids = {TEAMS}
    mic.broken = True
    assert watch.calls({"teams": True}, TABLE) == []
    mic.broken = False
    assert watch.calls({"meet": False, "teams": False}, TABLE) == []
    call = watch.current({"teams": True}, TABLE)
    assert call.session_id == f"teams:{clock.filetime()}" and call.age_seconds is None


def test_a_first_look_later_than_a_minute_into_the_watch_finds_no_saved_call(monkeypatch, mic, clock):
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    mic.pids = {TEAMS}
    saved = f"teams:{clock.filetime(600)}"
    watch = CallWatch([saved])
    clock.advance(calls.SAVED_FOR)
    assert watch.current({"teams": True}, TABLE).session_id == saved  # just in time
    late = CallWatch([saved])
    clock.advance(calls.SAVED_FOR + 1)  # notes were off at the start, say
    call = late.current({"teams": True}, TABLE)
    assert call.session_id == f"teams:{clock.filetime()}" and call.age_seconds is None


def test_a_watch_with_no_saved_call_in_that_app_never_asks_when_its_processes_started(monkeypatch, mic):
    monkeypatch.setattr(calls, "process_start", lambda pid: pytest.fail("asked when a process started"))
    use_windows(monkeypatch)
    mic.pids = {TEAMS}
    assert CallWatch(["zoom:604:1790000000", "meet:shift:1"]).current({"teams": True}, TABLE).age_seconds is None


@pytest.mark.parametrize(
    ("session_id", "parsed"),
    [
        ("teams:134400000000000000", ("teams", "teams", 134400000000000000)),
        ("meet:shift:5", ("meet", "shift", 5)),
        ("teams:msedge:5", ("teams", "msedge", 5)),
        ("meet:chrome:5", ("meet", "chrome", 5)),
        ("teams:soon", None),
        ("teams", None),
        ("meet:5", None),
        ("zoom:604:5", None),
        ("meet:firefox:5", None),
        ("meet:shift:-5", None),
        ("meet:shift:", None),
        ("teams:shift:5:6", None),
        ("", None),
    ],
)
def test_only_the_microphone_s_own_call_ids_are_read_back(session_id, parsed):
    assert calls._parse_mic_id(session_id) == parsed


def test_a_filetime_is_an_age_that_is_never_negative(clock):
    assert calls.filetime_age(clock.filetime(100)) == pytest.approx(100, abs=0.01)
    assert calls.filetime_age(clock.filetime(-500)) == 0.0
    assert calls.filetime_age(2**63) == 0.0


# The audio engine itself, through COM interfaces made here

S_OK, E_FAIL, E_NOTIMPL, E_OUTOFMEMORY = 0, -2147467259, -2147467263, -2147024882
INACTIVE, ACTIVE, EXPIRED = 0, 1, 2
# Every argument taken as pointer-sized, which is how 64-bit Windows passes each of them.
METHOD = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_size_t)


def low(value: int) -> int:
    """A 32-bit argument, which arrives in a 64-bit register whose top half isn't defined."""
    return value & 0xFFFFFFFF


def iid_at(address: int) -> str:
    return str(uuid.UUID(bytes_le=bytes((ctypes.c_ubyte * 16).from_address(address)))).upper()


def put(address: int, kind, value) -> int:
    kind.from_address(address).value = value
    return S_OK


class FakeInterface:
    """A COM interface made in Python with a real table of methods, so capture_pids's own calls run against it.
    ``methods`` maps a method's place to a function of its arguments (integers: a pointer is an address) that returns
    an HRESULT, and can be changed at any time. Release is counted in ``released``."""

    def __init__(self, kind: str, methods: dict) -> None:
        self.kind = kind
        self.methods = methods
        self.released = 0
        self._callbacks = [METHOD(self._dispatcher(index)) for index in range(17)]
        self._table = (ctypes.c_void_p * 17)(*(ctypes.cast(callback, ctypes.c_void_p) for callback in self._callbacks))
        self._object = ctypes.c_void_p(ctypes.addressof(self._table))
        self.pointer = ctypes.addressof(self._object)

    def _dispatcher(self, index: int):
        def method(_this, *args):
            if index == calls.RELEASE:
                self.released += 1
                return 0
            function = self.methods.get(index)
            return E_NOTIMPL if function is None else function(*args)

        return method


class AudioEngine:
    """Recording devices, each a list of its sessions as (state, pid). A pid of E_FAIL is a session whose process
    Windows won't name. ``handed`` has every interface given out, in order, and ``kinds`` each kind made."""

    def __init__(self, *devices) -> None:
        self.kinds: dict[str, list[FakeInterface]] = {}
        self.handed: list[FakeInterface] = []
        self.asked: list[tuple[int, int]] = []
        self.activated: list[tuple[str, int]] = []
        self.queried: list[str] = []
        self.created: list[tuple[str, int, str]] = []
        made = [self._device(sessions) for sessions in devices]
        collection = self._make(
            "collection",
            {
                calls.COLLECTION_GET_COUNT: lambda out, *_: put(out, ctypes.c_uint, len(made)),
                calls.COLLECTION_ITEM: lambda index, out, *_: self._give(out, made[low(index)]),
            },
        )

        def endpoints(flow, state, out, _unused):
            self.asked.append((low(flow), low(state)))
            return self._give(out, collection)

        self.enumerator = self._make("enumerator", {calls.ENUM_AUDIO_ENDPOINTS: endpoints})

    def _make(self, kind: str, methods: dict) -> FakeInterface:
        interface = FakeInterface(kind, methods)
        self.kinds.setdefault(kind, []).append(interface)
        return interface

    def _give(self, out: int, interface: FakeInterface) -> int:
        self.handed.append(interface)
        return put(out, ctypes.c_void_p, interface.pointer)

    def _device(self, sessions) -> FakeInterface:
        controls = [self._session(state, pid) for state, pid in sessions]
        enumerator = self._make(
            "sessions",
            {
                calls.SESSIONS_GET_COUNT: lambda out, *_: put(out, ctypes.c_int, len(controls)),
                calls.SESSIONS_GET_SESSION: lambda index, out, *_: self._give(out, controls[low(index)]),
            },
        )
        manager = self._make("manager", {calls.GET_SESSION_ENUMERATOR: lambda out, *_: self._give(out, enumerator)})

        def activate(iid, context, _params, out):
            self.activated.append((iid_at(iid), low(context)))
            return self._give(out, manager)

        return self._make("device", {calls.DEVICE_ACTIVATE: activate})

    def _session(self, state: int, pid: int) -> FakeInterface:
        def process_id(out, *_):
            return pid if pid < 0 else put(out, wintypes.DWORD, pid)

        control2 = self._make(
            "control2",
            {calls.SESSION_GET_STATE: lambda out, *_: put(out, ctypes.c_int, state), calls.SESSION_GET_PROCESS_ID: process_id},
        )

        def query(iid, out, *_):
            self.queried.append(iid_at(iid))
            return self._give(out, control2)

        return self._make("control", {calls.QUERY_INTERFACE: query})

    def ole32(self) -> SimpleNamespace:
        def name(guid) -> str:
            return str(uuid.UUID(bytes_le=bytes(guid._obj.bytes))).upper()  # called from Python: byref's target is _obj

        def create(clsid, _outer, context, iid, out):
            self.created.append((name(clsid), context, name(iid)))
            self.handed.append(self.enumerator)
            out._obj.value = self.enumerator.pointer

        return SimpleNamespace(CoInitializeEx=lambda reserved, mode: S_OK, CoCreateInstance=create)


@pytest.fixture
def engine(monkeypatch):
    """``engine(*devices)`` makes the audio engine capture_pids asks (see AudioEngine), on a thread COM is new to."""
    monkeypatch.setattr(calls, "_com_threads", threading.local())

    def make(*devices) -> AudioEngine:
        made = AudioEngine(*devices)
        monkeypatch.setattr(calls, "ole32", made.ole32())
        return made

    return make


def released_once_each(made: AudioEngine) -> bool:
    """Every interface handed out was released once, and none that wasn't."""
    handed = {id(interface) for interface in made.handed}
    return all(interface.released == (id(interface) in handed) for kind in made.kinds.values() for interface in kind)


def test_the_processes_recording_are_those_with_a_running_session_on_any_microphone(engine):
    made = engine(
        [(ACTIVE, 81), (INACTIVE, 42), (ACTIVE, 0)],  # Shift; something that has stopped; Windows' own
        [],  # a microphone no one is using
        [(ACTIVE, 70), (EXPIRED, 43), (ACTIVE, 81)],
    )
    assert REAL_CAPTURE_PIDS() == {81, 70}
    assert made.created == [(calls.CLSID_MM_DEVICE_ENUMERATOR, calls.CLSCTX_ALL, calls.IID_IMM_DEVICE_ENUMERATOR)]
    assert made.asked == [(calls.E_CAPTURE, calls.DEVICE_STATE_ACTIVE)]  # recording devices, plugged in
    assert made.activated == [(calls.IID_IAUDIO_SESSION_MANAGER2, calls.CLSCTX_ALL)] * 3
    assert made.queried == [calls.IID_IAUDIO_SESSION_CONTROL2] * 6
    assert released_once_each(made) and len(made.handed) == 1 + 1 + 3 * 3 + 6 * 2


def test_with_no_microphone_no_one_is_recording(engine):
    made = engine()
    assert REAL_CAPTURE_PIDS() == set()
    assert released_once_each(made)


def test_a_session_whose_process_windows_will_not_name_is_left_out_and_the_others_still_count(engine):
    made = engine([(ACTIVE, E_FAIL), (ACTIVE, 51)])
    assert REAL_CAPTURE_PIDS() == {51}
    assert released_once_each(made)


@pytest.mark.parametrize(
    ("kind", "place"),
    [
        ("enumerator", calls.ENUM_AUDIO_ENDPOINTS),
        ("collection", calls.COLLECTION_GET_COUNT),
        ("collection", calls.COLLECTION_ITEM),
        ("device", calls.DEVICE_ACTIVATE),
        ("manager", calls.GET_SESSION_ENUMERATOR),
        ("sessions", calls.SESSIONS_GET_COUNT),
        ("sessions", calls.SESSIONS_GET_SESSION),
        ("control", calls.QUERY_INTERFACE),
        ("control2", calls.SESSION_GET_STATE),
    ],
)
def test_any_other_step_that_fails_makes_the_answer_unknown_and_lets_go_of_everything(engine, kind, place):
    made = engine([(ACTIVE, 81)], [(ACTIVE, 70)])
    made.kinds[kind][-1].methods[place] = lambda *_: E_FAIL
    with pytest.raises(OSError):
        REAL_CAPTURE_PIDS()
    assert released_once_each(made)


def test_an_interface_windows_said_it_gave_but_did_not_is_an_error(engine):
    made = engine([(ACTIVE, 81)])
    made.kinds["manager"][0].methods[calls.GET_SESSION_ENUMERATOR] = lambda out, *_: put(out, ctypes.c_void_p, None)
    with pytest.raises(OSError, match="no interface"):
        REAL_CAPTURE_PIDS()
    assert released_once_each(made)


def test_an_audio_engine_windows_will_not_make_is_an_error(engine, monkeypatch):
    engine()

    def refuse(*_args):
        raise OSError("class not registered")  # what ctypes.HRESULT raises

    monkeypatch.setattr(calls.ole32, "CoCreateInstance", refuse)
    with pytest.raises(OSError, match="not registered"):
        REAL_CAPTURE_PIDS()


def test_com_is_set_up_once_on_each_thread_and_a_thread_that_has_it_the_other_way_is_fine(monkeypatch):
    monkeypatch.setattr(calls, "_com_threads", threading.local())
    answers = [calls.RPC_E_CHANGED_MODE, S_OK]
    asked = []
    monkeypatch.setattr(calls, "ole32", SimpleNamespace(CoInitializeEx=lambda reserved, mode: asked.append(mode) or answers.pop(0)))
    calls._com_ready()
    calls._com_ready()
    assert asked == [calls.COINIT_MULTITHREADED]
    thread = threading.Thread(target=calls._com_ready)  # another thread sets it up for itself
    thread.start()
    thread.join()
    assert asked == [calls.COINIT_MULTITHREADED] * 2 and answers == []


def test_com_that_cannot_be_set_up_is_an_error_and_is_tried_again_next_time(monkeypatch):
    monkeypatch.setattr(calls, "_com_threads", threading.local())
    answers = [E_OUTOFMEMORY, 1]  # then S_FALSE: already set up the same way
    monkeypatch.setattr(calls, "ole32", SimpleNamespace(CoInitializeEx=lambda reserved, mode: answers.pop(0)))
    with pytest.raises(OSError):
        calls._com_ready()
    calls._com_ready()
    assert answers == []


# What is on this PC

def test_zoom_is_found_by_its_exe_under_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "machine"))  # this PC's own Zoom would answer otherwise
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "machine32"))
    assert calls.installed("zoom") is False
    (tmp_path / "roaming" / "Zoom" / "bin").mkdir(parents=True)
    (tmp_path / "roaming" / "Zoom" / "bin" / "Zoom.exe").write_bytes(b"")
    assert calls.installed("zoom") is True
    monkeypatch.delenv("APPDATA")
    assert calls.installed("zoom") is False


def test_zoom_installed_for_every_user_is_found_under_program_files(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "machine"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "machine32"))
    assert calls.installed("zoom") is False
    (tmp_path / "machine" / "Zoom" / "bin").mkdir(parents=True)
    (tmp_path / "machine" / "Zoom" / "bin" / "Zoom.exe").write_bytes(b"")
    assert calls.installed("zoom") is True
    monkeypatch.delenv("ProgramFiles")
    assert calls.installed("zoom") is False


def test_a_32_bit_zoom_is_found_under_program_files_x86(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "machine"))
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "machine32"))
    assert calls.installed("zoom") is False
    (tmp_path / "machine32" / "Zoom" / "bin").mkdir(parents=True)
    (tmp_path / "machine32" / "Zoom" / "bin" / "Zoom.exe").write_bytes(b"")
    assert calls.installed("zoom") is True
    monkeypatch.delenv("ProgramFiles(x86)")
    assert calls.installed("zoom") is False


def test_teams_is_found_by_its_alias_or_its_entry_in_the_microphone_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    use_registry(monkeypatch, "Microsoft.WindowsCamera_8wekyb3d8bbwe")
    assert calls.installed("teams") is False
    use_registry(monkeypatch, "Microsoft.WindowsCamera_8wekyb3d8bbwe", "MSTeams_8wekyb3d8bbwe")  # asked, used or not
    assert calls.installed("teams") is True
    use_registry(monkeypatch, "msteams_somethingelse")  # whatever the case and its publisher suffix
    assert calls.installed("teams") is True
    monkeypatch.setattr(calls, "winreg", Registry({}))  # no microphone settings at all
    assert calls.installed("teams") is False
    monkeypatch.setattr(calls, "winreg", Refused({}))
    assert calls.installed("teams") is False
    apps = tmp_path / "Microsoft" / "WindowsApps"
    apps.mkdir(parents=True)
    (apps / "ms-teams.exe").write_bytes(b"")
    monkeypatch.setattr(calls, "winreg", SimpleNamespace())  # the alias is enough: the settings aren't read
    assert calls.installed("teams") is True


def test_meet_is_never_found_on_the_pc_because_it_is_in_a_browser():
    assert calls.installed("meet") is None
    assert calls.installed("something-else") is None


# On this PC, with nothing faked

def test_the_windows_calls_themselves_work_on_this_pc(monkeypatch):
    """The ctypes, COM and winreg calls with no fakes, which only read, so a wrong signature, method or key shows here
    and not on a user's PC. What they find depends on the PC and who is using it, so only the kinds of things they
    return are checked. The microphone is asked from a thread of its own, as the watcher asks it, so this one's COM is
    left as it was; a PC with no audio engine running (a build server, say) may answer OSError, as capture_pids says."""
    table = calls.process_table()
    pids = {pid for pids in table.values() for pid in pids}
    titles = calls.window_titles(pids)
    assert isinstance(titles, list) and all(isinstance(title, str) for title in titles)
    assert calls.window_titles(set()) == []
    monkeypatch.setattr(calls, "capture_pids", REAL_CAPTURE_PIDS)
    found: dict = {}

    def ask() -> None:
        try:
            found["recording"] = calls.capture_pids()
        except OSError as error:
            found["error"] = error
        found["calls"] = CallWatch().calls(ALL_ON)

    thread = threading.Thread(target=ask)
    thread.start()
    thread.join()
    recording = found.get("recording", set())
    assert isinstance(recording, set) and all(isinstance(pid, int) and pid > 0 for pid in recording)
    assert ("recording" in found) != ("error" in found)
    assert all(
        isinstance(call, Call) and call.provider in ALL_ON and call.session_id.startswith(f"{call.provider}:") for call in found["calls"]
    )
    assert isinstance(calls.installed("zoom"), bool) and isinstance(calls.installed("teams"), bool)
    assert isinstance(calls.granola_running(), bool)
