import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from granola_autostart import zoom_meetings
from granola_autostart.zoom_meetings import MAX_TITLE, best_window_title

# What process_table returns: lower-cased image names, each with its process ids.
TABLE = {"cpthost.exe": [912, 604], "zoom.exe": [300], "granola.exe": [77]}


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
    assert zoom_meetings.pids_named("CptHost.exe", TABLE) == [912, 604]
    assert zoom_meetings.pids_named("Teams.exe", TABLE) == []
    assert zoom_meetings.granola_running(TABLE) and not zoom_meetings.granola_running({})


def test_a_zoom_meeting_is_its_first_cpthost_and_when_that_started(monkeypatch):
    started = datetime.now(timezone.utc) - timedelta(minutes=3)
    asked = []
    monkeypatch.setattr(zoom_meetings, "process_start", lambda pid: asked.append(pid) or started)
    session_id, age = zoom_meetings.zoom_session(TABLE)
    assert asked == [604]
    assert session_id == f"604:{int(started.timestamp())}" and 179 < age < 240


def test_without_cpthost_there_is_no_meeting_and_without_its_start_no_age(monkeypatch):
    assert zoom_meetings.zoom_session({"zoom.exe": [300]}) == (None, None)
    monkeypatch.setattr(zoom_meetings, "process_start", lambda pid: None)
    assert zoom_meetings.zoom_session(TABLE) == ("604", None)


def test_a_meeting_that_seems_to_start_in_the_future_is_brand_new(monkeypatch):
    monkeypatch.setattr(zoom_meetings, "process_start", lambda pid: datetime.now(timezone.utc) + timedelta(minutes=5))
    assert zoom_meetings.zoom_session(TABLE)[1] == 0.0


def test_this_test_s_own_process_is_listed_with_its_start_time():
    table = zoom_meetings.process_table()
    assert any(os.getpid() in pids for pids in table.values())
    assert all(name == name.casefold() for name in table)
    started = zoom_meetings.process_start(os.getpid())
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
    monkeypatch.setattr(zoom_meetings, "kernel32", kernel32)
    assert zoom_meetings.process_table() == {}
    kernel32.snapshot = zoom_meetings.INVALID_HANDLE_VALUE
    assert zoom_meetings.process_table() == {}
    assert zoom_meetings.process_start(604) is None  # protected, or already gone
    kernel32.process = 0x88
    assert zoom_meetings.process_start(604) is None
    assert kernel32.closed == [0x88]


class Windows:
    """user32's top-level windows, each (hwnd, pid, visible, title)."""

    def __init__(self, *windows):
        self.windows = {hwnd: (pid, visible, title) for hwnd, pid, visible, title in windows}

    def EnumWindows(self, callback, lparam):
        for hwnd in self.windows:
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


def test_the_meeting_name_comes_from_zoom_s_visible_titled_windows(monkeypatch):
    monkeypatch.setattr(
        zoom_meetings,
        "user32",
        Windows(
            (1, 300, True, "Zoom Workplace"),
            (2, 604, True, "Roadmap review"),
            (3, 604, False, "Roadmap review and its hidden twin"),
            (4, 999, True, "A longer title in some other app"),
            (5, 300, True, ""),
        ),
    )
    assert zoom_meetings.zoom_window_title(TABLE) == "Roadmap review"


def test_without_zoom_running_no_window_is_looked_at(monkeypatch):
    monkeypatch.setattr(zoom_meetings, "process_table", lambda: {"granola.exe": [77]})
    monkeypatch.setattr(zoom_meetings, "user32", SimpleNamespace())  # any window call would fail
    assert zoom_meetings.zoom_window_title() is None
    assert zoom_meetings.granola_running()
