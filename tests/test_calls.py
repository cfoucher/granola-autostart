import os
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import calls
from granola_autostart.calls import MAX_TITLE, Call, CallWatch, best_window_title

# What process_table returns: lower-cased image names, each with its process ids.
TABLE = {
    "cpthost.exe": [912, 604],
    "zoom.exe": [300],
    "granola.exe": [77],
    "ms-teams.exe": [70],
    "chrome.exe": [50, 51],
    "msedge.exe": [60],
}
ALL_ON = {"zoom": True, "teams": True, "meet": True}
CHROME = "C:#Program Files#Google#Chrome#Application#chrome.exe"
EDGE = "C:#Program Files (x86)#Microsoft#Edge#Application#msedge.exe"
TEAMS = "MSTeams_8wekyb3d8bbwe"


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


def test_this_test_s_own_process_is_listed_with_its_start_time():
    table = calls.process_table()
    assert any(os.getpid() in pids for pids in table.values())
    assert all(name == name.casefold() for name in table)
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


# The microphone record, as winreg would show it: a key is a dict of subkeys (dicts) and values.

DENIED = object()  # a key Windows refuses to open


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
            if node is DENIED:
                raise PermissionError(path)
            if not isinstance(node, dict):
                raise FileNotFoundError(path)
        return Key(node)

    def EnumKey(self, key, index):
        names = [name for name, value in key.node.items() if isinstance(value, dict) or value is DENIED]
        if index >= len(names):
            raise OSError(None, "no more data", None, calls.ERROR_NO_MORE_ITEMS)  # as winreg says it
        return names[index]

    def QueryValueEx(self, key, name):
        value = key.node.get(name)
        if value is None or isinstance(value, dict):
            raise FileNotFoundError(name)
        return value, 11  # REG_QWORD


class Unreadable(Registry):
    """A microphone record Windows refuses to open (a lock, or a policy)."""

    def OpenKey(self, parent, path, *_args):
        raise PermissionError(path)


class CutShort(Registry):
    """A list of keys that fails after ``after`` names with something other than "no more items"."""

    def __init__(self, tree, after):
        super().__init__(tree)
        self.after = after

    def EnumKey(self, key, index):
        if index >= self.after:
            raise OSError(None, "the key could not be read", None, 5)  # access denied
        return super().EnumKey(key, index)


def filetime(seconds_ago: float) -> int:
    return int((time.time() - seconds_ago + calls.FILETIME_UNIX_EPOCH) * calls.FILETIME_TICKS)


def in_use(seconds_ago: float) -> dict:
    return {"LastUsedTimeStart": filetime(seconds_ago), "LastUsedTimeStop": 0}


def stopped(seconds_ago: float = 60, for_seconds: float = 30) -> dict:
    return {"LastUsedTimeStart": filetime(seconds_ago), "LastUsedTimeStop": filetime(seconds_ago - for_seconds)}


def use_registry(monkeypatch, packaged=None, non_packaged=None) -> None:
    """Fake the microphone record: ``packaged`` and ``non_packaged`` map each app's key name to its values."""
    tree = {**(packaged or {}), "NonPackaged": non_packaged or {}}
    for part in reversed(calls.CONSENT_KEY.split("\\")):
        tree = {part: tree}
    monkeypatch.setattr(calls, "winreg", Registry(tree))


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

def test_teams_is_in_a_call_while_it_holds_the_microphone_and_the_call_began_when_it_started(monkeypatch):
    values = in_use(150)
    use_registry(monkeypatch, packaged={TEAMS: values})
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    call = CallWatch().current({"teams": True}, TABLE)
    assert call.provider == "teams" and call.title == "Weekly sync"
    assert call.session_id == f"teams:{values['LastUsedTimeStart']}"
    assert 149 < call.age_seconds < 160


def test_a_teams_session_is_named_by_its_start(monkeypatch):
    values = {"LastUsedTimeStart": 134400000000000000, "LastUsedTimeStop": 0}
    use_registry(monkeypatch, packaged={TEAMS: values})
    use_windows(monkeypatch)
    assert CallWatch().current({"teams": True}, TABLE).session_id == "teams:134400000000000000"


def test_teams_holding_the_microphone_no_longer_is_not_a_call(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: stopped()})
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current({"teams": True}, TABLE) is None


def test_teams_holding_the_microphone_is_not_a_call_unless_teams_is_running(monkeypatch):
    # A crash can leave the record saying the microphone is still in use.
    use_registry(monkeypatch, packaged={TEAMS: in_use(600)})
    monkeypatch.setattr(calls, "user32", Denied())
    without_teams = {name: pids for name, pids in TABLE.items() if name != "ms-teams.exe"}
    assert CallWatch().current({"teams": True}, without_teams) is None
    monkeypatch.setattr(calls, "winreg", Denied())  # with no Teams running the record isn't even read
    assert calls.teams_calls(without_teams) == []
    assert calls.teams_calls({}) == []


def test_teams_asks_for_the_process_list_itself_when_it_is_not_given_one(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(30)})
    use_windows(monkeypatch)
    monkeypatch.setattr(calls, "process_table", lambda: TABLE)
    assert [call.provider for call in calls.teams_calls()] == ["teams"]
    monkeypatch.setattr(calls, "process_table", lambda: {})
    assert calls.teams_calls() == []


@pytest.mark.parametrize(
    "values",
    [
        {},  # never used the microphone
        {"LastUsedTimeStart": 0, "LastUsedTimeStop": 0},
        {"LastUsedTimeStart": filetime(60)},  # no stop value to say it is still going
        {"LastUsedTimeStop": 0},
        {"LastUsedTimeStart": "garbage", "LastUsedTimeStop": 0},
        {"LastUsedTimeStart": -5, "LastUsedTimeStop": 0},
        DENIED,
    ],
)
def test_missing_or_unreadable_values_mean_not_in_use(monkeypatch, values):
    use_registry(monkeypatch, packaged={TEAMS: values})
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current({"teams": True}, TABLE) is None


def test_without_the_microphone_record_or_a_teams_entry_there_is_no_teams_call(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Registry({}))  # no ConsentStore at all
    assert CallWatch().calls(ALL_ON, {}) == []
    use_registry(
        monkeypatch,
        packaged={"Microsoft.WindowsCamera_8wekyb3d8bbwe": in_use(30)},
        non_packaged={"C:#Tools#other.exe": in_use(30)},
    )
    assert CallWatch().calls({"teams": True, "meet": True}, TABLE) == []


def test_teams_is_found_by_its_package_whatever_the_case_and_its_publisher_suffix(monkeypatch):
    use_registry(monkeypatch, packaged={"msteams_somethingelse": in_use(10)})
    use_windows(monkeypatch)
    assert CallWatch().current({"teams": True}, TABLE).provider == "teams"


def test_teams_windows_name_the_meeting_unless_they_only_name_teams():
    assert calls.teams_window_title(["Weekly sync | Microsoft Teams", "Chat | Microsoft Teams"]) == "Weekly sync"
    assert calls.teams_window_title(["Calendar | Microsoft Teams", "Microsoft Teams", "Roadmap | microsoft teams"]) == "Roadmap"
    assert calls.teams_window_title(["Microsoft Teams", "Chat | Microsoft Teams", " "]) is None
    assert calls.teams_window_title([]) is None


def test_teams_main_window_titles_with_a_chat_or_channel_in_them_never_name_the_meeting():
    # Chat and channel titles keep a " | " after the suffix is gone, and can be longer than the meeting's own.
    titles = ["Chat | Alexandra Montgomery-Whitfield | Microsoft Teams", "Design team | General | Microsoft Teams", "Standup | Microsoft Teams"]
    assert calls.teams_window_title(titles) == "Standup"
    assert calls.teams_window_title(titles[:2]) is None


def test_teams_windows_are_read_once_a_tick_and_only_its_visible_ones_count(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(30)})
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
    assert not any(calls.is_meet_title(title) for title in ("Google Meet", "Meet", "Meeting - x", "Roadmap - Meet - x", "meet - x"))


def meet_world(monkeypatch, *windows, browser=CHROME, values=None):
    """A browser holding the microphone, with the windows it shows."""
    use_registry(monkeypatch, non_packaged={browser: values or in_use(20)})
    return use_windows(monkeypatch, *windows)


def test_a_browser_holding_the_microphone_in_front_of_a_meet_tab_is_a_meet_call(monkeypatch):
    values = in_use(20)
    meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"), values=values)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.provider == "meet" and call.title == "Weekly sync"
    assert call.session_id == f"meet:chrome:{values['LastUsedTimeStart']}" and 19 < call.age_seconds < 30


def test_a_meet_call_in_edge_is_recognised_the_same_way(monkeypatch):
    values = in_use(5)
    meet_world(monkeypatch, (1, 60, True, "Meet - abc-defg-hij - Work - Microsoft\u200b Edge"), browser=EDGE, values=values)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.session_id == f"meet:msedge:{values['LastUsedTimeStart']}" and call.title is None


def test_a_browser_holding_the_microphone_without_a_meet_tab_in_front_is_not_a_call(monkeypatch):
    meet_world(
        monkeypatch,
        (1, 50, True, "Voice recorder - Google Chrome"),
        (2, 50, False, "Meet - Hidden - Google Chrome"),  # not visible
        (3, 60, True, "Meet - Weekly sync - Microsoft\u200b Edge"),  # the other browser's
    )
    assert CallWatch().current({"meet": True}, TABLE) is None


def test_a_meet_call_is_decided_when_its_session_appears_and_kept_for_the_whole_session(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    # The user switches to another tab: the window now carries that tab's title.
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    later = watch.current({"meet": True}, TABLE)
    assert (later.session_id, later.title) == (first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # looked once, when the session was new


def test_a_session_that_was_not_a_meet_call_stays_not_a_call_even_if_a_meet_tab_comes_to_the_front(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Meet - Weekly sync - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None
    assert windows.enumerations == 1


def test_a_new_browser_session_is_looked_at_afresh_and_an_ended_one_is_forgotten(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE).title == "Weekly sync"
    use_registry(monkeypatch, non_packaged={CHROME: stopped()})
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    use_registry(monkeypatch, non_packaged={CHROME: in_use(1)})
    assert watch.current({"meet": True}, TABLE) is None  # the next use of the microphone isn't the old call
    assert windows.enumerations == 2


def test_a_browser_that_is_not_running_is_not_in_a_call_whatever_the_record_says(monkeypatch):
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    without_chrome = {name: pids for name, pids in TABLE.items() if name != "chrome.exe"}
    watch = CallWatch()
    assert watch.current({"meet": True}, without_chrome) is None  # a crash left the record saying it is in use
    assert windows.enumerations == 0  # and none of its windows were looked at
    # Chrome is the one running, and Edge's record is the stale one.
    use_registry(monkeypatch, non_packaged={CHROME: in_use(20), EDGE: in_use(900)})
    windows.windows[2] = (60, True, "Meet - Standup - Microsoft​ Edge")
    only_chrome = {name: pids for name, pids in TABLE.items() if name != "msedge.exe"}
    assert [call.title for call in watch.calls({"meet": True}, only_chrome)] == ["Weekly sync"]


def test_a_session_that_ended_with_its_browser_is_forgotten(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    assert watch.current({"meet": True}, {"granola.exe": [77]}) is None  # the browser has gone
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None  # decided afresh, as a new session is


def test_a_record_that_cannot_be_read_this_time_keeps_the_meet_calls_known(monkeypatch):
    watch = CallWatch()
    values = in_use(20)
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"), values=values)
    first = watch.current({"meet": True}, TABLE)
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    for _ in range(2):
        kept = watch.current({"meet": True}, TABLE)
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "meet")
        assert 19 < kept.age_seconds < 40
    use_registry(monkeypatch, non_packaged={CHROME: values})  # readable again, and still the same session
    again = watch.current({"meet": True}, TABLE)
    assert (again.session_id, again.title) == (first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # never looked at again
    use_registry(monkeypatch, non_packaged={CHROME: stopped()})  # a read that works is what ends it
    assert watch.current({"meet": True}, TABLE) is None


def test_a_process_list_that_could_not_be_read_keeps_the_meet_calls_known(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    for _ in range(2):
        kept = watch.current({"meet": True}, {})  # no list is not a list with no browser in it
        assert (kept.session_id, kept.title) == (first.session_id, "Weekly sync")
    assert watch.current({"meet": True}, TABLE).session_id == first.session_id  # the same session, never looked at again
    assert windows.enumerations == 1
    assert watch.current({"meet": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it


def test_a_process_list_that_could_not_be_read_keeps_the_teams_call_known(monkeypatch):
    watch = CallWatch()
    values = in_use(150)
    use_registry(monkeypatch, packaged={TEAMS: values})
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    first = watch.current({"teams": True}, TABLE)
    monkeypatch.setattr(calls, "winreg", Denied())  # kept, not read again
    kept = watch.current({"teams": True}, {})
    assert (kept.session_id, kept.title) == (first.session_id, "Weekly sync")
    use_registry(monkeypatch, packaged={TEAMS: stopped()})
    assert watch.current({"teams": True}, {}) is not None  # still no list
    assert watch.current({"teams": True}, TABLE) is None  # a list that works sorts it out
    assert watch.current({"teams": True}, {}) is None  # and the call that ended isn't kept


def test_a_record_that_cannot_be_read_this_time_keeps_the_teams_call_known(monkeypatch):
    watch = CallWatch()
    values = in_use(150)
    use_registry(monkeypatch, packaged={TEAMS: values})
    use_windows(monkeypatch, (1, 70, True, "Weekly sync | Microsoft Teams"))
    first = watch.current({"teams": True}, TABLE)
    for unreadable in (Unreadable({}), CutShort(calls.winreg.HKEY_CURRENT_USER.node, after=0)):
        monkeypatch.setattr(calls, "winreg", unreadable)
        kept = watch.current({"teams": True}, TABLE)
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    use_registry(monkeypatch, packaged={TEAMS: values})  # readable again, and still the same call
    assert watch.current({"teams": True}, TABLE).session_id == first.session_id
    use_registry(monkeypatch, packaged={TEAMS: stopped()})  # a read that works is what ends it
    assert watch.current({"teams": True}, TABLE) is None


@pytest.mark.parametrize("by", ["process list", "record"])
def test_a_teams_call_kept_through_a_failed_read_goes_on_growing_older(monkeypatch, by):
    watch = CallWatch()
    use_registry(monkeypatch, packaged={TEAMS: in_use(100)})
    use_windows(monkeypatch)
    first = watch.current({"teams": True}, TABLE)
    assert 99 < first.age_seconds < 110
    real = time.time()
    monkeypatch.setattr(calls, "time", SimpleNamespace(time=lambda: real + 400))
    if by == "record":
        monkeypatch.setattr(calls, "winreg", Unreadable({}))
    kept = watch.current({"teams": True}, TABLE if by == "record" else {})
    assert kept.session_id == first.session_id
    assert 499 < kept.age_seconds < 510
    monkeypatch.setattr(calls, "time", SimpleNamespace(time=lambda: real + 700))
    assert 799 < watch.current({"teams": True}, TABLE if by == "record" else {}).age_seconds < 810  # and again, from the start


def test_a_call_kept_with_an_id_that_has_no_readable_start_keeps_its_age():
    for session_id in ("zoom:604", "zoom:604:soon", "teams:soon", "teams", "meet:chrome:1"):
        call = Call(session_id.split(":")[0], session_id, 12.0, None)
        assert calls._reaged(call) is call


def test_a_record_that_cannot_be_read_knows_no_teams_call_that_was_never_seen(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    assert CallWatch().calls({"teams": True}, TABLE) == []


def test_teams_called_for_strictly_says_when_the_record_could_not_be_read(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    assert calls.teams_calls(TABLE) == []
    with pytest.raises(PermissionError):
        calls.teams_calls(TABLE, strict=True)
    monkeypatch.setattr(calls, "winreg", Registry({}))  # no record at all: nothing in use, and nothing wrong
    assert calls.teams_calls(TABLE, strict=True) == []


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


def test_a_call_never_seen_is_not_made_up_from_a_process_list_that_could_not_be_read(monkeypatch):
    watch = CallWatch()
    use_registry(monkeypatch, packaged={TEAMS: in_use(30)}, non_packaged={CHROME: in_use(30)})
    monkeypatch.setattr(calls, "user32", Denied())
    assert watch.calls(ALL_ON, {}) == []


def test_turning_teams_off_forgets_its_call(monkeypatch):
    watch = CallWatch()
    use_registry(monkeypatch, packaged={TEAMS: in_use(30)})
    use_windows(monkeypatch)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"teams": False}, TABLE) is None
    assert watch.current({"teams": True}, {}) is None


def test_a_record_that_cannot_be_read_knows_no_call_that_was_never_seen(monkeypatch):
    watch = CallWatch()
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    monkeypatch.setattr(calls, "user32", Denied())
    assert watch.calls({"meet": True}, TABLE) == []
    # Nor does it keep a session that wasn't a Meet call.
    windows = meet_world(monkeypatch, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.calls({"meet": True}, TABLE) == [] and windows.enumerations == 1
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    assert watch.calls({"meet": True}, TABLE) == []


def test_one_key_that_cannot_be_read_makes_the_whole_record_unknown_not_stopped(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    # Edge's key is refused while Chrome's can be read, and Chrome's says it stopped.
    use_registry(monkeypatch, non_packaged={CHROME: stopped(), EDGE: DENIED})
    assert watch.current({"meet": True}, TABLE).title == "Weekly sync"
    assert windows.enumerations == 1


def test_with_two_meet_tabs_the_named_one_gives_the_title(monkeypatch):
    meet_world(
        monkeypatch,
        (1, 50, True, "Meet - abc-defg-hij - Google Chrome"),
        (2, 51, True, "Meet - Weekly sync - Google Chrome"),
    )
    assert CallWatch().current({"meet": True}, TABLE).title == "Weekly sync"


def test_only_chrome_and_edge_count_as_meet_browsers(monkeypatch):
    use_registry(
        monkeypatch,
        non_packaged={
            "C:#Users#me#AppData#Local#PurpleShift#chromium#shift.exe": in_use(10),
            "C:#Users#me#AppData#Roaming#Zoom#bin#Zoom.exe": in_use(10),
            "C:#Tools#notchrome.exe": in_use(10),
        },
    )
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current({"meet": True}, TABLE) is None


def test_the_meet_browser_s_key_is_matched_whatever_its_case_or_folder(monkeypatch):
    browser = "C:#Users#me#AppData#Local#Google#Chrome#Application#CHROME.EXE"
    meet_world(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), browser=browser)
    assert CallWatch().current({"meet": True}, TABLE).title == "Standup"


# Which provider, and which call

def test_of_several_calls_the_most_recently_started_wins(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(300)}, non_packaged={CHROME: in_use(40)})
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) - timedelta(minutes=2))
    use_windows(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), (2, 912, True, "Roadmap"))
    watch = CallWatch()
    assert [call.provider for call in watch.calls(ALL_ON, TABLE)] == ["zoom", "teams", "meet"]
    assert watch.current(ALL_ON, TABLE).provider == "meet"
    assert watch.current({"zoom": True, "teams": True}, TABLE).provider == "zoom"
    assert watch.current({"teams": True}, TABLE).provider == "teams"


def test_a_call_with_no_known_start_ranks_behind_one_that_has_one(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(3000)})
    monkeypatch.setattr(calls, "process_start", lambda pid: None)
    use_windows(monkeypatch)
    assert CallWatch().current({"zoom": True, "teams": True}, TABLE).provider == "teams"
    assert CallWatch().current({"zoom": True}, TABLE).provider == "zoom"


def test_the_process_list_is_read_once_for_every_provider_when_none_is_given(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(30)}, non_packaged={CHROME: in_use(30)})
    use_windows(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), (2, 912, True, "Roadmap"))
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc) - timedelta(minutes=2))
    reads = []
    monkeypatch.setattr(calls, "process_table", lambda: reads.append(1) or TABLE)
    assert [call.provider for call in CallWatch().calls(ALL_ON)] == ["zoom", "teams", "meet"]
    assert reads == [1]


def test_with_no_call_there_is_nothing_to_find(monkeypatch):
    use_registry(monkeypatch)
    assert CallWatch().current(ALL_ON, {"granola.exe": [77]}) is None


def test_a_provider_that_is_off_or_not_mentioned_is_not_looked_at_at_all(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Denied())
    monkeypatch.setattr(calls, "user32", Denied())
    monkeypatch.setattr(calls, "kernel32", Denied())
    monkeypatch.setattr(calls, "process_table", lambda: pytest.fail("the process list was read"))
    watch = CallWatch()
    assert watch.calls({"zoom": False, "teams": False, "meet": False}) == []
    assert watch.calls({}) == []
    assert watch.current({"zoom": False}) is None


def test_turning_meet_off_forgets_what_it_decided(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    assert watch.current({"meet": False}, TABLE) is None
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None  # decided afresh, with another tab in front


# The microphone record itself

def test_an_unreadable_microphone_record_reads_as_nothing_in_use(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    assert calls.registry_apps(True, lambda name: True) == []
    assert calls.registry_apps(False, lambda name: True) == []


def test_asked_to_be_strict_the_record_says_when_it_could_not_be_read_but_not_when_it_is_missing(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    for packaged in (True, False):
        with pytest.raises(PermissionError):
            calls.registry_apps(packaged, lambda name: True, strict=True)
    monkeypatch.setattr(calls, "winreg", Registry({}))  # no ConsentStore at all: nothing in use, and nothing wrong
    assert calls.registry_apps(True, lambda name: True, strict=True) == []
    assert calls.registry_apps(False, lambda name: True, strict=True) == []
    # One app's key that is refused: not in use as far as anyone can tell, or an error when strict. The rest are read.
    use_registry(monkeypatch, non_packaged={CHROME: in_use(5), EDGE: DENIED})
    apps = calls.registry_apps(False, lambda name: True)
    assert {app.name: app.in_use for app in apps} == {CHROME: True, EDGE: False}
    with pytest.raises(PermissionError):
        calls.registry_apps(False, lambda name: True, strict=True)
    # A key that goes away between listing it and opening it is not an error either way.
    monkeypatch.setattr(calls, "_subkeys", lambda group, strict=False: iter(["gone"]))
    assert [app.in_use for app in calls.registry_apps(False, lambda name: True, strict=True)] == [False]


def test_a_list_of_keys_that_stops_with_an_error_is_cut_short_not_complete_and_only_strict_says_so(monkeypatch):
    tree = {CHROME: in_use(5), EDGE: in_use(5)}
    use_registry(monkeypatch, non_packaged=tree)
    monkeypatch.setattr(calls, "winreg", CutShort(calls.winreg.HKEY_CURRENT_USER.node, after=1))
    assert [app.name for app in calls.registry_apps(False, lambda name: True)] == [CHROME]  # as far as it got
    with pytest.raises(OSError, match="could not be read"):
        calls.registry_apps(False, lambda name: True, strict=True)
    # The list that ends the usual way is complete, strict or not.
    use_registry(monkeypatch, non_packaged=tree)
    assert len(calls.registry_apps(False, lambda name: True, strict=True)) == 2


def test_a_list_of_keys_that_cannot_be_read_makes_meet_keep_what_it_knew(monkeypatch):
    watch = CallWatch()
    windows = meet_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    tree = calls.winreg.HKEY_CURRENT_USER.node
    monkeypatch.setattr(calls, "winreg", CutShort(tree, after=0))  # listing fails at once, which isn't "no browsers"
    assert watch.current({"meet": True}, TABLE).session_id == first.session_id
    assert windows.enumerations == 1


def test_only_the_apps_asked_for_are_read(monkeypatch):
    use_registry(monkeypatch, packaged={TEAMS: in_use(5), "Other_abc": in_use(5)}, non_packaged={CHROME: in_use(5)})
    seen = []
    apps = calls.registry_apps(True, lambda name: seen.append(name) or name == TEAMS)
    assert [app.name for app in apps] == [TEAMS] and apps[0].in_use
    assert "NonPackaged" in seen  # listed, never read
    assert [app.name for app in calls.registry_apps(False, lambda name: True)] == [CHROME]


def test_a_filetime_is_an_age_that_is_never_negative():
    assert 99 < calls.filetime_age(filetime(100)) < 110
    assert calls.filetime_age(filetime(-500)) == 0.0
    assert calls.filetime_age(2**63) == 0.0


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


def test_teams_is_found_by_its_alias_or_its_microphone_entry(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    use_registry(monkeypatch, packaged={"Microsoft.WindowsCamera_8wekyb3d8bbwe": {}})
    assert calls.installed("teams") is False
    use_registry(monkeypatch, packaged={TEAMS: {}})  # a package that has never used the microphone
    assert calls.installed("teams") is True
    use_registry(monkeypatch)
    apps = tmp_path / "Microsoft" / "WindowsApps"
    apps.mkdir(parents=True)
    (apps / "ms-teams.exe").write_bytes(b"")
    monkeypatch.setattr(calls, "registry_apps", lambda *_args: pytest.fail("the alias was enough"))
    assert calls.installed("teams") is True


def test_meet_is_never_found_on_the_pc_because_it_is_in_chrome_or_edge():
    assert calls.installed("meet") is None
    assert calls.installed("something-else") is None


# On this PC, with nothing faked

def test_the_windows_calls_themselves_work_on_this_pc():
    """The ctypes and winreg calls with no fakes, which only read, so a wrong signature or key shows here and not on
    a user's PC. What they find depends on the PC and who is using it, so only the kinds of things they return are checked."""
    table = calls.process_table()
    pids = {pid for pids in table.values() for pid in pids}
    titles = calls.window_titles(pids)
    assert isinstance(titles, list) and all(isinstance(title, str) for title in titles)
    assert calls.window_titles(set()) == []
    for packaged in (True, False):
        apps = calls.registry_apps(packaged, lambda name: True)
        assert all(isinstance(app, calls.MicApp) and app.started >= 0 and app.stopped >= 0 for app in apps)
        assert calls.registry_apps(packaged, lambda name: False, strict=True) == []
    found = CallWatch().calls(ALL_ON)
    assert all(isinstance(call, Call) and call.provider in ALL_ON and call.session_id.startswith(f"{call.provider}:") for call in found)
    assert isinstance(calls.installed("zoom"), bool) and isinstance(calls.installed("teams"), bool)
    assert isinstance(calls.granola_running(), bool)
