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
    "shift.exe": [80, 81],
}
ALL_ON = {"zoom": True, "teams": True, "meet": True}
CHROME = "C:#Program Files#Google#Chrome#Application#chrome.exe"
EDGE = "C:#Program Files (x86)#Microsoft#Edge#Application#msedge.exe"
SHIFT = "C:#Users#me#AppData#Local#PurpleShift#chromium#shift.exe"
TEAMS = "MSTeams_8wekyb3d8bbwe"
MEETING = "ConfMultiTabContentWndClass"  # Zoom's meeting window


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
    """user32's top-level windows, each (hwnd, pid, visible, title), with its window class after them when it matters.
    Change ``windows`` to move a tab."""

    def __init__(self, *windows):
        self.windows = {hwnd: (pid, visible, title) for hwnd, pid, visible, title, *_ in windows}
        self.classes = {window[0]: window[4] for window in windows if len(window) > 4}
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

    def GetClassNameW(self, hwnd, buffer, size):
        buffer.value = self.classes.get(hwnd, "")[: size - 1]
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
    use_windows(monkeypatch, (2, 300, True, "Roadmap review", MEETING))
    call = calls.zoom_call(TABLE)
    assert asked == [604]
    assert call.provider == "zoom" and call.title == "Roadmap review" and call.started == started
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


def test_the_meeting_name_comes_from_zoom_s_visible_meeting_window(monkeypatch):
    use_windows(
        monkeypatch,
        (1, 300, True, "Zoom Workplace", "ZPPTMainFrmWndClassEx"),
        (2, 300, True, "Roadmap review", MEETING),
        (3, 300, False, "Roadmap review and its hidden twin", MEETING),
        (4, 999, True, "A longer title in some other app", MEETING),
        (5, 300, True, "", MEETING),
    )
    assert calls.zoom_window_title(TABLE) == "Roadmap review"
    use_windows(monkeypatch, (1, 300, True, "Roadmap review", "ZPContentViewWndClass"))  # an older Zoom's meeting window
    assert calls.zoom_window_title(TABLE) == "Roadmap review"


def test_zoom_s_toolbars_and_panels_never_name_the_meeting(monkeypatch):
    # The windows a Zoom meeting shows, its screen shared or not. Zoom titles the meeting window after itself, and every
    # other window after what it is, so none of them is the meeting's name.
    monkeypatch.setattr(calls, "process_start", lambda pid: datetime.now(timezone.utc))
    use_windows(
        monkeypatch,
        (1, 300, True, "Zoom Meeting", MEETING),
        (2, 300, True, "ZPToolBarParentWnd", "ZPToolBarParentWndClass"),
        (3, 300, True, "Screen sharing meeting controls", "ZPFloatToolbarClass"),
        (4, 300, True, "VideoFrameWnd", "VideoFrameWndClass"),
        (5, 300, True, "Annotation - Zoom", "ZoomAnnoWindowWndClass"),
        (6, 300, True, "Zoom Workplace", "ZPPTMainFrmWndClassEx"),
        (7, 604, True, "A window of the meeting's own process", "sharing frame"),
        (8, 300, True, "A window whose class can't be read"),
    )
    assert calls.zoom_call(TABLE).title is None


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
    assert call.started == calls.filetime_datetime(values["LastUsedTimeStart"])
    assert timedelta(seconds=149) < datetime.now(timezone.utc) - call.started < timedelta(seconds=160)


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


def browser_world(monkeypatch, *windows, browser=CHROME, values=None):
    """A browser holding the microphone, with the windows it shows."""
    use_registry(monkeypatch, non_packaged={browser: values or in_use(20)})
    return use_windows(monkeypatch, *windows)


def test_a_browser_holding_the_microphone_in_front_of_a_meet_tab_is_a_meet_call(monkeypatch):
    values = in_use(20)
    browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"), values=values)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.provider == "meet" and call.title == "Weekly sync"
    assert call.session_id == f"meet:chrome:{values['LastUsedTimeStart']}" and 19 < call.age_seconds < 30
    assert call.started == calls.filetime_datetime(values["LastUsedTimeStart"])


def test_a_meet_call_in_edge_is_recognised_the_same_way(monkeypatch):
    values = in_use(5)
    browser_world(monkeypatch, (1, 60, True, "Meet - abc-defg-hij - Work - Microsoft\u200b Edge"), browser=EDGE, values=values)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.session_id == f"meet:msedge:{values['LastUsedTimeStart']}" and call.title is None


def test_a_browser_holding_the_microphone_without_a_meet_tab_in_front_is_not_a_call(monkeypatch):
    browser_world(
        monkeypatch,
        (1, 50, True, "Voice recorder - Google Chrome"),
        (2, 50, False, "Meet - Hidden - Google Chrome"),  # not visible
        (3, 60, True, "Meet - Weekly sync - Microsoft\u200b Edge"),  # the other browser's
    )
    assert CallWatch().current({"meet": True}, TABLE) is None


def test_a_meet_call_is_decided_when_its_session_appears_and_kept_for_the_whole_session(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    first = watch.current({"meet": True}, TABLE)
    # The user switches to another tab: the window now carries that tab's title.
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    later = watch.current({"meet": True}, TABLE)
    assert (later.session_id, later.title) == (first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # looked once, when the session was new


def test_a_session_that_was_not_a_meet_call_stays_not_a_call_even_if_a_meet_tab_comes_to_the_front(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Meet - Weekly sync - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None
    assert windows.enumerations == 1


def test_a_new_browser_session_is_looked_at_afresh_and_an_ended_one_is_forgotten(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE).title == "Weekly sync"
    use_registry(monkeypatch, non_packaged={CHROME: stopped()})
    assert watch.current({"meet": True}, TABLE) is None
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    use_registry(monkeypatch, non_packaged={CHROME: in_use(1)})
    assert watch.current({"meet": True}, TABLE) is None  # the next use of the microphone isn't the old call
    assert windows.enumerations == 2


def test_a_browser_that_is_not_running_is_not_in_a_call_whatever_the_record_says(monkeypatch):
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
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
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    assert watch.current({"meet": True}, {"granola.exe": [77]}) is None  # the browser has gone
    windows.windows[1] = (50, True, "Inbox - Google Chrome")
    assert watch.current({"meet": True}, TABLE) is None  # decided afresh, as a new session is


def test_a_record_that_cannot_be_read_this_time_keeps_the_meet_calls_known(monkeypatch):
    watch = CallWatch()
    values = in_use(20)
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"), values=values)
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
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
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
    for session_id in ("zoom:604", "zoom:604:soon", "teams:soon", "teams", "meet:chrome:1", "teams:shift:1", "teams:shift:soon"):
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
    use_windows(monkeypatch, (1, 300, True, "Roadmap review", MEETING))
    first = watch.current({"zoom": True}, TABLE)
    monkeypatch.setattr(calls, "user32", Denied())  # kept, not looked at again
    for _ in range(2):
        kept = watch.current({"zoom": True}, {})  # no list is not a list with no meeting in it
        assert (kept.session_id, kept.title) == (first.session_id, "Roadmap review")
    use_windows(monkeypatch, (1, 300, True, "Roadmap review", MEETING))
    assert watch.current({"zoom": True}, TABLE).session_id == first.session_id
    assert watch.current({"zoom": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it
    assert watch.current({"zoom": True}, {}) is None  # and the meeting that ended isn't kept


def test_a_zoom_call_kept_through_an_unreadable_process_list_goes_on_growing_older(monkeypatch):
    watch = CallWatch()
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    monkeypatch.setattr(calls, "process_start", lambda pid: started)
    use_windows(monkeypatch, (1, 300, True, "Roadmap review", MEETING))
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
    windows = browser_world(monkeypatch, (1, 50, True, "Voice recorder - Google Chrome"))
    assert watch.calls({"meet": True}, TABLE) == [] and windows.enumerations == 1
    monkeypatch.setattr(calls, "winreg", Unreadable({}))
    assert watch.calls({"meet": True}, TABLE) == []


def test_one_key_that_cannot_be_read_makes_the_whole_record_unknown_not_stopped(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
    assert watch.current({"meet": True}, TABLE) is not None
    # Edge's key is refused while Chrome's can be read, and Chrome's says it stopped.
    use_registry(monkeypatch, non_packaged={CHROME: stopped(), EDGE: DENIED})
    assert watch.current({"meet": True}, TABLE).title == "Weekly sync"
    assert windows.enumerations == 1


def test_with_two_meet_tabs_the_named_one_gives_the_title(monkeypatch):
    browser_world(
        monkeypatch,
        (1, 50, True, "Meet - abc-defg-hij - Google Chrome"),
        (2, 51, True, "Meet - Weekly sync - Google Chrome"),
    )
    assert CallWatch().current({"meet": True}, TABLE).title == "Weekly sync"


def test_a_meet_call_in_shift_is_recognised_the_same_way(monkeypatch):
    values = in_use(5)
    browser_world(monkeypatch, (1, 80, True, "Meet - Weekly sync - Shift Browser"), browser=SHIFT, values=values)
    call = CallWatch().current({"meet": True}, TABLE)
    assert call.provider == "meet" and call.title == "Weekly sync"
    assert call.session_id == f"meet:shift:{values['LastUsedTimeStart']}"


def test_only_chrome_edge_and_shift_count_as_browsers(monkeypatch):
    use_registry(
        monkeypatch,
        non_packaged={
            "C:#Users#me#AppData#Roaming#Zoom#bin#Zoom.exe": in_use(10),
            "C:#Tools#notchrome.exe": in_use(10),
            "C:#Tools#notshift.exe": in_use(10),
            "C:#Tools#shift.exe.bak": in_use(10),
        },
    )
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().current({"meet": True, "teams": True}, TABLE) is None


def test_the_browser_s_key_is_matched_whatever_its_case_or_folder(monkeypatch):
    browser = "C:#Users#me#AppData#Local#Google#Chrome#Application#CHROME.EXE"
    browser_world(monkeypatch, (1, 50, True, "Meet - Standup - Google Chrome"), browser=browser)
    assert CallWatch().current({"meet": True}, TABLE).title == "Standup"


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
    ("browser", "pid", "window", "exe"),
    [
        (CHROME, 50, "Weekly sync | Microsoft Teams - Google Chrome - Work", "chrome"),
        (EDGE, 60, "Weekly sync | Microsoft Teams - Work - Microsoft​ Edge", "msedge"),
        (SHIFT, 80, "Weekly sync | Microsoft Teams - Shift Browser", "shift"),
        (EDGE, 60, "Weekly sync | Microsoft Teams", "msedge"),  # an installed web app's window
    ],
)
def test_a_browser_holding_the_microphone_in_front_of_a_teams_tab_is_a_teams_call(monkeypatch, browser, pid, window, exe):
    values = in_use(20)
    browser_world(monkeypatch, (1, pid, True, window), browser=browser, values=values)
    call = CallWatch().current({"teams": True}, TABLE)
    assert call.provider == "teams" and call.title == "Weekly sync"
    assert call.session_id == f"teams:{exe}:{values['LastUsedTimeStart']}" and 19 < call.age_seconds < 30


def test_a_teams_tab_that_names_no_meeting_is_still_a_teams_call_without_a_name(monkeypatch):
    browser_world(monkeypatch, (1, 80, True, "(3) Chat | Microsoft Teams - Shift Browser"), browser=SHIFT)
    call = CallWatch().current({"teams": True}, TABLE)
    assert call.provider == "teams" and call.title is None


def test_with_several_teams_windows_the_first_that_names_a_meeting_gives_the_title(monkeypatch):
    browser_world(
        monkeypatch,
        (1, 80, True, "Chat | Jane Doe | Microsoft Teams - Shift Browser"),
        (2, 80, True, "Calendar | Microsoft Teams - Shift Browser"),
        (3, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"),
        (4, 81, True, "Standup | Microsoft Teams - Shift Browser"),
        browser=SHIFT,
    )
    assert CallWatch().current({"teams": True}, TABLE).title == "Weekly sync"


def test_a_browser_holding_the_microphone_in_front_of_neither_meet_nor_teams_is_not_a_call(monkeypatch):
    windows = browser_world(
        monkeypatch,
        (1, 80, True, "(1) WhatsApp - Shift Browser"),
        (2, 80, False, "Weekly sync | Microsoft Teams - Shift Browser"),  # not visible
        (3, 50, True, "Weekly sync | Microsoft Teams - Google Chrome"),  # the other browser's
        (4, 60, True, "Meet - Standup - Microsoft​ Edge"),
        browser=SHIFT,
    )
    assert CallWatch().calls({"meet": True, "teams": True}, TABLE) == []
    assert windows.enumerations == 1


def test_a_meet_tab_in_front_makes_it_a_meet_call_even_with_a_teams_tab_in_front_too(monkeypatch):
    values = in_use(20)
    teams = (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser")  # the first window, and still not the call
    browser_world(monkeypatch, teams, (2, 81, True, "Meet - Standup - Shift Browser"), browser=SHIFT, values=values)
    call = CallWatch().current({"meet": True, "teams": True}, TABLE)
    assert (call.provider, call.title) == ("meet", "Standup")
    assert call.session_id == f"meet:shift:{values['LastUsedTimeStart']}"


def test_a_meet_call_with_only_a_meeting_code_has_no_name_and_does_not_borrow_teams_one(monkeypatch):
    teams = (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser")
    browser_world(monkeypatch, teams, (2, 80, True, "Meet - abc-defg-hij - Shift Browser"), browser=SHIFT)
    call = CallWatch().current({"meet": True, "teams": True}, TABLE)
    assert (call.provider, call.title) == ("meet", None)


def test_a_teams_call_in_a_browser_is_decided_when_its_session_appears_and_kept_for_the_whole_session(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    first = watch.current({"teams": True}, TABLE)
    for tab in ("(1) WhatsApp - Shift Browser", "Meet - Standup - Shift Browser"):
        windows.windows[1] = (80, True, tab)  # the user switches to another tab
        later = watch.current({"meet": True, "teams": True}, TABLE)
        assert (later.provider, later.session_id, later.title) == ("teams", first.session_id, "Weekly sync")
    assert windows.enumerations == 1  # looked once, when the session was new


def test_a_session_in_front_of_neither_stays_not_a_call_even_if_a_teams_tab_comes_to_the_front(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "(1) WhatsApp - Shift Browser"), browser=SHIFT)
    assert watch.calls({"meet": True, "teams": True}, TABLE) == []
    windows.windows[1] = (80, True, "Weekly sync | Microsoft Teams - Shift Browser")
    assert watch.calls({"meet": True, "teams": True}, TABLE) == []
    assert windows.enumerations == 1


def test_a_teams_session_that_ended_is_forgotten_and_the_next_one_is_looked_at_afresh(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"
    use_registry(monkeypatch, non_packaged={SHIFT: stopped()})
    assert watch.current({"teams": True}, TABLE) is None
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    use_registry(monkeypatch, non_packaged={SHIFT: in_use(1)})
    assert watch.current({"teams": True}, TABLE) is None  # the next use of the microphone isn't the old call
    assert windows.enumerations == 2


def test_a_browser_that_is_not_running_is_not_in_a_teams_call_whatever_the_record_says(monkeypatch):
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    without_shift = {name: pids for name, pids in TABLE.items() if name != "shift.exe"}
    assert CallWatch().calls({"teams": True}, without_shift) == []  # a crash left the record saying it is in use
    assert windows.enumerations == 0


# The switches, for calls in a browser

def switch_world(monkeypatch) -> Windows:
    """Teams in Shift and Meet in Chrome, both holding the microphone, in the order of the record."""
    use_registry(monkeypatch, non_packaged={SHIFT: in_use(30), CHROME: in_use(20)})
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
        ({"teams": True, "meet": True}, [("teams", "Weekly sync"), ("meet", "Standup")]),
        ({"teams": True, "meet": False}, [("teams", "Weekly sync")]),
        ({"teams": False, "meet": True}, [("meet", "Standup")]),
        ({"teams": False, "meet": False}, []),
    ],
)
def test_a_call_in_a_browser_is_reported_only_while_its_own_provider_is_switched_on(monkeypatch, enabled, reported):
    switch_world(monkeypatch)
    found = CallWatch().calls(enabled, TABLE)
    assert [(call.provider, call.title) for call in found] == reported


def test_a_meet_and_a_teams_tab_in_front_make_a_meet_call_that_only_the_meet_switch_reports(monkeypatch):
    browser_world(
        monkeypatch,
        (1, 50, True, "Weekly sync | Microsoft Teams - Google Chrome"),
        (2, 50, True, "Meet - Standup - Google Chrome"),
    )
    assert CallWatch().calls({"teams": True}, TABLE) == []  # not a Teams call, though a Teams tab was in front
    assert [call.provider for call in CallWatch().calls({"meet": True}, TABLE)] == ["meet"]


def test_a_session_is_decided_whichever_switches_are_on_when_it_appears(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    assert watch.calls({"meet": True}, TABLE) == []  # looked at here, with Teams off
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    call = watch.current({"meet": True, "teams": True}, TABLE)
    assert (call.provider, call.title) == ("teams", "Weekly sync")
    assert windows.enumerations == 1


def test_turning_meet_or_teams_off_alone_keeps_what_a_browser_session_decided(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"meet": True}, TABLE) is None  # Teams is off, so its call isn't reported
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"  # and it was kept, not decided again
    assert windows.enumerations == 1


def test_turning_meet_and_teams_both_off_forgets_what_a_browser_session_decided(monkeypatch):
    watch = CallWatch()
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT)
    assert watch.current({"teams": True}, TABLE) is not None
    assert watch.current({"teams": False, "meet": False}, TABLE) is None
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    assert watch.current({"teams": True}, TABLE) is None  # decided afresh, with another tab in front
    assert windows.enumerations == 2


def test_the_browsers_are_not_looked_at_unless_meet_or_teams_is_on(monkeypatch):
    monkeypatch.setattr(calls, "winreg", Denied())
    monkeypatch.setattr(calls, "user32", Denied())
    assert CallWatch().calls({"zoom": True}, {"granola.exe": [77]}) == []
    assert CallWatch().calls({"zoom": True, "teams": False, "meet": False}, {"granola.exe": [77]}) == []


def test_a_teams_call_in_the_desktop_app_and_one_in_a_browser_are_both_reported(monkeypatch):
    desktop, web = in_use(300), in_use(40)
    use_registry(monkeypatch, packaged={TEAMS: desktop}, non_packaged={SHIFT: web})
    use_windows(
        monkeypatch,
        (1, 70, True, "Standup | Microsoft Teams"),
        (2, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"),
    )
    watch = CallWatch()
    found = watch.calls({"teams": True}, TABLE)
    assert [(call.provider, call.session_id, call.title) for call in found] == [
        ("teams", f"teams:{desktop['LastUsedTimeStart']}", "Standup"),
        ("teams", f"teams:shift:{web['LastUsedTimeStart']}", "Weekly sync"),
    ]
    assert watch.current({"teams": True}, TABLE).session_id == f"teams:shift:{web['LastUsedTimeStart']}"  # the newer
    assert watch.calls({"meet": True}, TABLE) == []  # with Teams off neither Teams call is reported


def test_a_process_list_that_could_not_be_read_keeps_the_teams_call_in_a_browser_known(monkeypatch):
    watch = CallWatch()
    values = in_use(20)
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT, values=values)
    first = watch.current({"teams": True}, TABLE)
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    monkeypatch.setattr(calls, "winreg", Denied())  # kept, not read again
    for _ in range(2):
        kept = watch.current({"teams": True}, {})  # no list is not a list with no browser in it
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    use_registry(monkeypatch, non_packaged={SHIFT: values})
    assert watch.current({"teams": True}, TABLE).title == "Weekly sync"  # the same session, never looked at again
    assert windows.enumerations == 1
    assert watch.current({"teams": True}, {"granola.exe": [77]}) is None  # a list that works is what ends it
    assert watch.current({"teams": True}, {}) is None  # and the call that ended isn't kept


def test_a_record_that_cannot_be_read_this_time_keeps_the_teams_call_in_a_browser_known(monkeypatch):
    watch = CallWatch()
    values = in_use(20)
    windows = browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT, values=values)
    first = watch.current({"teams": True}, TABLE)
    windows.windows[1] = (80, True, "(1) WhatsApp - Shift Browser")
    for unreadable in (Unreadable({}), CutShort(calls.winreg.HKEY_CURRENT_USER.node, after=0)):
        monkeypatch.setattr(calls, "winreg", unreadable)
        kept = watch.current({"teams": True}, TABLE)
        assert (kept.session_id, kept.title, kept.provider) == (first.session_id, "Weekly sync", "teams")
    use_registry(monkeypatch, non_packaged={SHIFT: values})  # readable again, and still the same session
    assert watch.current({"teams": True}, TABLE).session_id == first.session_id
    assert windows.enumerations == 1
    use_registry(monkeypatch, non_packaged={SHIFT: stopped()})  # a read that works is what ends it
    assert watch.current({"teams": True}, TABLE) is None


@pytest.mark.parametrize("by", ["process list", "record"])
def test_sessions_kept_through_a_failed_read_are_reported_only_while_their_provider_is_on(monkeypatch, by):
    watch = CallWatch()
    switch_world(monkeypatch)
    assert len(watch.calls({"teams": True, "meet": True}, TABLE)) == 2
    if by == "record":
        monkeypatch.setattr(calls, "winreg", Unreadable({}))
    table = TABLE if by == "record" else {}
    assert [call.provider for call in watch.calls({"teams": True}, table)] == ["teams"]
    assert [call.provider for call in watch.calls({"meet": True}, table)] == ["meet"]
    assert [call.provider for call in watch.calls({"teams": True, "meet": True}, table)] == ["teams", "meet"]


@pytest.mark.parametrize("by", ["process list", "record"])
def test_a_teams_call_in_a_browser_kept_through_a_failed_read_goes_on_growing_older(monkeypatch, by):
    watch = CallWatch()
    browser_world(monkeypatch, (1, 80, True, "Weekly sync | Microsoft Teams - Shift Browser"), browser=SHIFT, values=in_use(100))
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
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
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
    windows = browser_world(monkeypatch, (1, 50, True, "Meet - Weekly sync - Google Chrome"))
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


def test_meet_is_never_found_on_the_pc_because_it_is_in_a_browser():
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
