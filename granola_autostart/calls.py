"""Detect a call under way on Zoom, Microsoft Teams, or Google Meet."""

from __future__ import annotations

import ctypes
import math
import os
import re
import time
import winreg
from collections.abc import Callable, Mapping
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

# Window titles that name the app rather than the meeting.
GENERIC_ZOOM_TITLES = {"zoom", "zoom meeting", "zoom workplace", "zoom webinar", "meeting chat"}
GENERIC_TEAMS_TITLES = {"microsoft teams", "chat", "activity", "calendar", "calls", "teams", "files", "apps", "communities", "onedrive"}
MAX_TITLE = 200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Windows records which apps use the microphone: packaged apps by package family, and the rest under
# NonPackaged by exe path with "#" for "\". It is an internal record, not an API.
CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
NON_PACKAGED = "NonPackaged"
TEAMS_PACKAGE_PREFIX = "msteams_"
TEAMS_EXE = "ms-teams.exe"
MEET_BROWSERS = ("chrome.exe", "msedge.exe")
FILETIME_TICKS = 10_000_000  # 100 ns ticks per second
FILETIME_UNIX_EPOCH = 11_644_473_600  # seconds from 1601-01-01 to 1970-01-01
ERROR_NO_MORE_ITEMS = 259  # what the registry says when a key has no more subkeys to list

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.restype = ctypes.c_int

ProcessTable = dict[str, list[int]]


@dataclass(frozen=True)
class Call:
    provider: str  # "zoom", "teams" or "meet"
    session_id: str  # the provider, a colon, then what tells this call from the next
    age_seconds: float | None  # None when Windows wouldn't say when it started
    title: str | None  # the meeting's name, when its windows show one


def process_table() -> ProcessTable:
    """Map lower-cased image name to process ids, from one Toolhelp snapshot. A snapshot Windows won't give is
    ``{}``, which means not known: a real list always has Windows' own processes in it."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return {}
    table: ProcessTable = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            table.setdefault(entry.szExeFile.casefold(), []).append(int(entry.th32ProcessID))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return table


def pids_named(image_name: str, table: ProcessTable | None = None) -> list[int]:
    if table is None:
        table = process_table()
    return list(table.get(image_name.casefold(), ()))


def process_start(pid: int) -> datetime | None:
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created = FILETIME()
        ignored = FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(ignored),
            ctypes.byref(ignored),
            ctypes.byref(ignored),
        ):
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks / 10)
    finally:
        kernel32.CloseHandle(handle)


def granola_running(table: ProcessTable | None = None) -> bool:
    return bool(pids_named("Granola.exe", table))


def window_titles(pids: set[int]) -> list[str]:
    """The titles of the visible windows these processes own."""
    if not pids:
        return []
    titles: list[str] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if window_pid.value not in pids:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        titles.append(buffer.value)
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(enum_proc(callback), 0)
    return titles


def app_window_titles(image_names: tuple[str, ...], table: ProcessTable | None = None) -> list[str]:
    """The titles of the visible windows of every process with one of these image names."""
    if table is None:
        table = process_table()
    return window_titles({pid for name in image_names for pid in pids_named(name, table)})


def _meeting_name(titles: list[str], generic: set[str]) -> str | None:
    """The longest title that isn't just the app's own name."""
    usable = [title.strip() for title in titles if title.strip() and title.strip().casefold() not in generic]
    return max(usable, key=len)[:MAX_TITLE] if usable else None


# Zoom

def best_window_title(titles: list[str]) -> str | None:
    """The meeting's name among Zoom's window titles: the longest one that isn't just Zoom's own name."""
    return _meeting_name(titles, GENERIC_ZOOM_TITLES)


def zoom_window_title(table: ProcessTable | None = None) -> str | None:
    return best_window_title(app_window_titles(("CptHost.exe", "Zoom.exe"), table))


def zoom_call(table: ProcessTable | None = None) -> Call | None:
    """The Zoom meeting under way: CptHost.exe runs only while one is open, and its start is the meeting's."""
    pids = pids_named("CptHost.exe", table)
    if not pids:
        return None
    pid = min(pids)
    started = process_start(pid)
    title = zoom_window_title(table)
    if started is None:
        return Call("zoom", f"zoom:{pid}", None, title)
    age = (datetime.now(timezone.utc) - started).total_seconds()
    return Call("zoom", f"zoom:{pid}:{int(started.timestamp())}", max(0.0, age), title)


# The microphone record, which Teams and Meet are noticed from

@dataclass(frozen=True)
class MicApp:
    """One app's entry in the microphone record, with when it last started and stopped using it."""

    name: str  # the key's name: a package family, or an exe path with "#" for "\"
    started: int  # LastUsedTimeStart, a FILETIME
    stopped: int  # LastUsedTimeStop: 0 while the microphone is in use

    @property
    def in_use(self) -> bool:
        return self.started != 0 and self.stopped == 0


def filetime_age(ticks: int) -> float:
    """Seconds since a FILETIME, which counts 100 ns from 1601."""
    return max(0.0, time.time() - (ticks / FILETIME_TICKS - FILETIME_UNIX_EPOCH))


def registry_apps(packaged: bool, wanted: Callable[[str], bool], *, strict: bool = False) -> list[MicApp]:
    """The microphone record's packaged apps (or, with ``packaged`` false, the others) whose key name
    ``wanted`` accepts. A key or value that is missing means not in use, and so does one that can't be read
    (or a list of keys that stops early), unless ``strict``: then that raises OSError, so a caller can tell
    not in use from not known."""
    path = CONSENT_KEY if packaged else f"{CONSENT_KEY}\\{NON_PACKAGED}"
    apps: list[MicApp] = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as group:
            for name in _subkeys(group, strict):
                if wanted(name):
                    apps.append(_read_app(group, name, strict))
    except FileNotFoundError:
        pass
    except OSError:
        if strict:
            raise
    return apps


def _subkeys(key, strict: bool = False):
    """The names of a key's subkeys. Listing ends at the first error, and when ``strict`` only "no more items"
    is the end: any other error is raised, so a list cut short isn't taken for the whole of it."""
    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError as error:
            if strict and error.winerror != ERROR_NO_MORE_ITEMS:
                raise
            return
        index += 1


def _read_app(group, name: str, strict: bool) -> MicApp:
    try:
        with winreg.OpenKey(group, name) as key:
            started = _qword(key, "LastUsedTimeStart")
            stopped = _qword(key, "LastUsedTimeStop")
    except FileNotFoundError:
        return MicApp(name, 0, 0)
    except OSError:
        if strict:
            raise
        return MicApp(name, 0, 0)
    if started is None or stopped is None:
        return MicApp(name, 0, 0)
    return MicApp(name, started, stopped)


def _qword(key, name: str) -> int | None:
    try:
        value, _kind = winreg.QueryValueEx(key, name)
    except FileNotFoundError:
        return None
    return value if isinstance(value, int) and value >= 0 else None


# Microsoft Teams

def _is_teams(package: str) -> bool:
    return package.casefold().startswith(TEAMS_PACKAGE_PREFIX)


_TEAMS_SUFFIX = re.compile(r"\s*\|\s*Microsoft Teams\s*$", re.IGNORECASE)


def teams_window_title(titles: list[str]) -> str | None:
    """The meeting's name among Teams' window titles, "Weekly sync | Microsoft Teams" without its suffix.
    A title that still has a " | " in it is the main window's chat or channel, not a meeting."""
    names = [_TEAMS_SUFFIX.sub("", title) for title in titles]
    return _meeting_name([name for name in names if " | " not in name], GENERIC_TEAMS_TITLES)


def teams_calls(table: ProcessTable | None = None, *, strict: bool = False) -> list[Call]:
    """Teams keeps the microphone open for the whole call, muted or not, so that is the call. Only while
    Teams runs: after a crash the record can go on saying the microphone is in use. With ``strict``, a record
    that can't be read raises OSError (see registry_apps), so a caller can tell no call from not known."""
    if table is None:
        table = process_table()
    if not pids_named(TEAMS_EXE, table):
        return []
    using = [app for app in registry_apps(True, _is_teams, strict=strict) if app.in_use]
    if not using:
        return []
    title = teams_window_title(app_window_titles((TEAMS_EXE,), table))
    return [Call("teams", f"teams:{app.started}", filetime_age(app.started), title) for app in using]


# Google Meet, in Chrome or Edge

def _is_meet_browser(path: str) -> bool:
    return path.casefold().endswith(tuple(f"#{name}" for name in MEET_BROWSERS))


def _browser_exe(path: str) -> str:
    return path.rsplit("#", 1)[-1].casefold()


_INVISIBLE = {ord(char): None for char in "\u200b\u200c\u200d\u2060\ufeff"}
_MEET_PREFIX = re.compile(r"Meet [-\u2013] ")
_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")
# Chrome puts the profile after its name ("Weekly sync - Google Chrome - Work"), Edge before it
# ("Weekly sync and 2 more pages - Work - Microsoft<zero-width space> Edge").
_CHROME_SUFFIX = re.compile(r"(?:^|\s+[-\u2013\u2014]\s+)Google Chrome(?:\s+[-\u2013\u2014]\s+.*)?$", re.IGNORECASE)
_EDGE_SUFFIX = re.compile(r"(?:^|\s+[-\u2013\u2014]\s+)Microsoft Edge$", re.IGNORECASE)
_MORE_PAGES = re.compile(r"\s+and \d+ more pages?$", re.IGNORECASE)
_MEETING_CODE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", re.IGNORECASE)


def is_meet_title(window_title: str) -> bool:
    return _MEET_PREFIX.match(window_title.translate(_INVISIBLE).strip()) is not None


def meet_title(window_title: str) -> str | None:
    """The meeting's name from a Meet tab's window title, or None when it is only a meeting code."""
    text = window_title.translate(_INVISIBLE).strip()
    prefix = _MEET_PREFIX.match(text)
    if prefix is None:
        return None
    text = text[prefix.end() :]
    chrome = _CHROME_SUFFIX.search(text)
    edge = _EDGE_SUFFIX.search(text)
    if chrome:
        text = text[: chrome.start()]
    elif edge:
        text = text[: edge.start()]
        separators = list(_SEPARATOR.finditer(text))
        if separators:
            text = text[: separators[-1].start()]  # the profile's name
    text = _MORE_PAGES.sub("", text).strip()
    if not text or _MEETING_CODE.fullmatch(text):
        return None
    return text[:MAX_TITLE]


@dataclass(frozen=True)
class _MeetSession:
    """What was decided about a browser's microphone session when it first appeared."""

    started: int  # the session's LastUsedTimeStart, a FILETIME
    is_call: bool
    title: str | None

    def call(self, session_id: str) -> Call:
        return Call("meet", session_id, filetime_age(self.started), self.title)


class CallWatch:
    """Finds the call under way among the enabled providers. Kept for the watcher's life, because a
    browser using the microphone is a Meet call only if a Meet tab was in front when that began, and
    that is decided once per session, so switching tabs later doesn't end the call."""

    def __init__(self) -> None:
        self._zoom: Call | None = None
        self._teams: list[Call] = []
        self._meet: dict[str, _MeetSession] = {}

    def calls(self, enabled: Mapping[str, bool], table: ProcessTable | None = None) -> list[Call]:
        """Every call under way for the providers that are switched on. A provider that is off isn't read.

        When the process list couldn't be read (``{}``), that says nothing about whether Zoom, Teams or a
        browser is running, so their calls stay as they were until a read that works. The same goes for
        Teams' and Meet's microphone record."""
        found: list[Call] = []
        if table is None and any(enabled.values()):
            table = process_table()  # one list for every provider
        known = bool(table)
        if enabled.get("zoom"):
            if known:
                self._zoom = zoom_call(table)
            elif self._zoom is not None:
                self._zoom = _reaged(self._zoom)
            if self._zoom is not None:
                found.append(self._zoom)
        else:
            self._zoom = None
        if enabled.get("teams"):
            read = False
            if known:
                try:
                    self._teams = teams_calls(table, strict=True)
                    read = True
                except OSError:
                    pass  # not the same as no one using the microphone: the call known stays, and a good read ends it
            if not read:
                self._teams = [_reaged(call) for call in self._teams]
            found.extend(self._teams)
        else:
            self._teams = []
        if enabled.get("meet"):
            found.extend(self._meet_calls(table, known))
        else:
            self._meet = {}
        return found

    def current(self, enabled: Mapping[str, bool], table: ProcessTable | None = None) -> Call | None:
        """The most recently started call, when several are under way."""
        found = self.calls(enabled, table)
        return min(found, key=_age) if found else None

    def _meet_calls(self, table: ProcessTable | None, known: bool) -> list[Call]:
        if not known:
            return self._kept_meet_calls()
        try:
            apps = registry_apps(False, _is_meet_browser, strict=True)
        except OSError:
            # The record couldn't be read this time. That isn't the same as no one using the microphone, so
            # the sessions known stay as they were, and the next good read sorts out which have ended.
            return self._kept_meet_calls()
        sessions: dict[str, _MeetSession] = {}
        found: list[Call] = []
        for app in apps:
            exe = _browser_exe(app.name)
            # A browser that crashed can leave the record saying it still uses the microphone.
            if not app.in_use or not pids_named(exe, table):
                continue
            session_id = f"meet:{exe.removesuffix('.exe')}:{app.started}"
            session = self._meet.get(session_id) or self._first_look(app.started, exe, table)
            sessions[session_id] = session
            if session.is_call:
                found.append(session.call(session_id))
        self._meet = sessions  # a session that has ended is forgotten
        return found

    def _kept_meet_calls(self) -> list[Call]:
        return [session.call(session_id) for session_id, session in self._meet.items() if session.is_call]

    def _first_look(self, started: int, exe: str, table: ProcessTable | None) -> _MeetSession:
        meet_windows = [title for title in app_window_titles((exe,), table) if is_meet_title(title)]
        title = next((name for name in map(meet_title, meet_windows) if name), None)
        return _MeetSession(started, bool(meet_windows), title)


def _age(call: Call) -> float:
    return math.inf if call.age_seconds is None else call.age_seconds


def _reaged(call: Call) -> Call:
    """A call kept through a read that failed, with its age worked out again from the start in its session id, so it
    goes on growing older instead of keeping the age it had. A Zoom id ends in the start (Unix seconds) and a Teams id
    is the start (a FILETIME). One that has no start in its id keeps what it had."""
    parts = call.session_id.split(":")
    try:
        if call.provider == "zoom" and len(parts) == 3:
            age = max(0.0, time.time() - int(parts[2]))
        elif call.provider == "teams" and len(parts) == 2:
            age = filetime_age(int(parts[1]))
        else:
            return call
    except ValueError:
        return call
    return replace(call, age_seconds=age)


def installed(provider: str) -> bool | None:
    """Whether a provider's app was found on this PC, for Settings to say so. It never decides whether
    a call is noticed. None for Meet, which is in Chrome or Edge."""
    if provider == "zoom":
        # For this user, or for every user on the PC (a 32-bit install is under Program Files (x86)).
        return any(_exists(variable, "Zoom", "bin", "Zoom.exe") for variable in ("APPDATA", "ProgramFiles", "ProgramFiles(x86)"))
    if provider == "teams":
        return _exists("LOCALAPPDATA", "Microsoft", "WindowsApps", TEAMS_EXE) or bool(registry_apps(True, _is_teams))
    return None


def _exists(variable: str, *parts: str) -> bool:
    root = os.environ.get(variable)
    return bool(root) and os.path.exists(os.path.join(root, *parts))
