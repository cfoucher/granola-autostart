"""Detect a call under way on Zoom, Microsoft Teams (its desktop app, or in a browser), or Google Meet (in a browser)."""

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
# The window classes of Zoom's meeting window, the only one of its windows whose title could be the meeting's (newer Zoom,
# then older). Its toolbars, panels and popups have titles of their own, such as "ZPToolBarParentWnd" or "Screen sharing
# meeting controls", which are no meeting's name.
ZOOM_MEETING_WINDOW_CLASSES = frozenset({"ConfMultiTabContentWndClass", "ZPContentViewWndClass"})
MAX_CLASS_NAME = 256  # the longest a window class name can be
# Teams' own pages. A title that names Microsoft Teams is Teams' too (see _teams_meeting_name).
GENERIC_TEAMS_TITLES = {
    "chat", "activity", "calendar", "calls", "teams", "files", "apps", "communities", "onedrive", "meeting", "meetings",
    "home", "people", "copilot",
}
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
BROWSERS = ("chrome.exe", "msedge.exe", "shift.exe")  # the browsers whose Meet and Teams calls are noticed
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
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.restype = ctypes.c_int

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


def window_titles(pids: set[int], classes: frozenset[str] | None = None) -> list[str]:
    """The titles of the visible windows these processes own. With ``classes``, only of their windows of those classes."""
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
        if classes is not None and _class_name(hwnd) not in classes:
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


def _class_name(hwnd) -> str:
    buffer = ctypes.create_unicode_buffer(MAX_CLASS_NAME)
    length = user32.GetClassNameW(hwnd, buffer, MAX_CLASS_NAME)
    return buffer.value if length > 0 else ""


def app_window_titles(
    image_names: tuple[str, ...], table: ProcessTable | None = None, classes: frozenset[str] | None = None
) -> list[str]:
    """The titles of the visible windows of every process with one of these image names (see window_titles)."""
    if table is None:
        table = process_table()
    return window_titles({pid for name in image_names for pid in pids_named(name, table)}, classes)


def _meeting_name(titles: list[str], generic: set[str]) -> str | None:
    """The longest title that isn't just the app's own name."""
    usable = [title.strip() for title in titles if title.strip() and title.strip().casefold() not in generic]
    return max(usable, key=len)[:MAX_TITLE] if usable else None


# Zoom

def best_window_title(titles: list[str]) -> str | None:
    """The meeting's name among the titles of Zoom's meeting windows: the longest one that isn't just Zoom's own name."""
    return _meeting_name(titles, GENERIC_ZOOM_TITLES)


def zoom_window_title(table: ProcessTable | None = None) -> str | None:
    """The meeting's name from Zoom's meeting window. Zoom Workplace titles that window "Zoom Meeting", whatever the
    meeting, so this is usually None and the call gets its ad hoc name (see decide.adhoc_title)."""
    return best_window_title(app_window_titles(("Zoom.exe",), table, ZOOM_MEETING_WINDOW_CLASSES))


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


# The microphone record, which Teams and the browsers' calls are noticed from

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
_UNREAD_COUNT = re.compile(r"^\s*\(\d+\)\s*")  # "(3) Chat | Microsoft Teams": three unread


def _teams_meeting_name(names: list[str]) -> str | None:
    """The meeting's name among Teams page titles that have lost their "| Microsoft Teams". A title that still
    has a " | " in it is the main window's chat or channel, not a meeting. A leading unread count isn't part of
    the name, so "(2) Chat" is only Teams' own Chat page. A title that names Microsoft Teams names Teams, not the
    meeting: in a browser, a meeting's page is "Microsoft Teams meeting", and its join page "Join a Microsoft Teams
    Meeting by ID"."""
    names = [_UNREAD_COUNT.sub("", name) for name in names]
    usable = [name for name in names if " | " not in name and "microsoft teams" not in name.casefold()]
    return _meeting_name(usable, GENERIC_TEAMS_TITLES)


def teams_window_title(titles: list[str]) -> str | None:
    """The meeting's name among Teams' window titles, "Weekly sync | Microsoft Teams" without its suffix."""
    return _teams_meeting_name([_TEAMS_SUFFIX.sub("", title) for title in titles])


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


# Google Meet and Microsoft Teams, in Chrome, Edge or Shift

def _is_browser(path: str) -> bool:
    return path.casefold().endswith(tuple(f"#{name}" for name in BROWSERS))


def _browser_exe(path: str) -> str:
    return path.rsplit("#", 1)[-1].casefold()


_INVISIBLE = {ord(char): None for char in "\u200b\u200c\u200d\u2060\ufeff"}
_MEET_PREFIX = re.compile(r"Meet [-\u2013] ")
_SEPARATOR = re.compile(r"\s+[-\u2013\u2014]\s+")
# Chrome puts the profile after its name ("Weekly sync - Google Chrome - Work"), Edge before it
# ("Weekly sync and 2 more pages - Work - Microsoft<zero-width space> Edge"), and Shift has none ("Weekly sync - Shift Browser").
_CHROME_SUFFIX = re.compile(r"(?:^|\s+[-\u2013\u2014]\s+)Google Chrome(?:\s+[-\u2013\u2014]\s+.*)?$", re.IGNORECASE)
_EDGE_SUFFIX = re.compile(r"(?:^|\s+[-\u2013\u2014]\s+)Microsoft Edge$", re.IGNORECASE)
_SHIFT_SUFFIX = re.compile(r"(?:^|\s+[-\u2013\u2014]\s+)Shift Browser$", re.IGNORECASE)
_MORE_PAGES = re.compile(r"\s+and \d+ more pages?$", re.IGNORECASE)
_MEETING_CODE = re.compile(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", re.IGNORECASE)
# Teams' own title, alone or after the page's name; a web app's window has no browser name after it.
_TEAMS_PAGE = re.compile(r"(?:^|\s+\|\s+)Microsoft Teams(?: \(free\))?$", re.IGNORECASE)


def _strip_browser(text: str, *, edge_profile: bool = True) -> str:
    """A window title without the browser's part of it: its name, the profile beside the name, and "and 2 more
    pages". A title with none of them, such as an installed web app's window, comes back as it was. The text has
    had its invisible characters taken out already.

    Edge's profile is taken to be the last " - " part before its name. ``edge_profile`` false keeps that part, for
    an Edge title with no profile in it, where the last " - " is in the page's own title."""
    chrome = _CHROME_SUFFIX.search(text)
    edge = _EDGE_SUFFIX.search(text)
    shift = _SHIFT_SUFFIX.search(text)
    if chrome:
        text = text[: chrome.start()]
    elif edge:
        text = text[: edge.start()]
        separators = list(_SEPARATOR.finditer(text))
        if edge_profile and separators:
            text = text[: separators[-1].start()]  # the profile's name
    elif shift:
        text = text[: shift.start()]
    return _MORE_PAGES.sub("", text).strip()


def is_meet_title(window_title: str) -> bool:
    return _MEET_PREFIX.match(window_title.translate(_INVISIBLE).strip()) is not None


def meet_title(window_title: str) -> str | None:
    """The meeting's name from a Meet tab's window title, or None when it is only a meeting code."""
    text = window_title.translate(_INVISIBLE).strip()
    prefix = _MEET_PREFIX.match(text)
    if prefix is None:
        return None
    text = _strip_browser(text[prefix.end() :])
    if not text or _MEETING_CODE.fullmatch(text):
        return None
    return text[:MAX_TITLE]


def _teams_page(window_title: str) -> str | None:
    """A browser window's page title without "| Microsoft Teams", the browser's part and an unread count, or None
    when the window isn't showing Teams. Teams' own title alone is a Teams page with an empty name. An Edge title
    is also tried as if it had no profile, so "Q3 - planning | Microsoft Teams - Microsoft Edge" is still Teams."""
    text = window_title.translate(_INVISIBLE).strip()
    for page in (_strip_browser(text), _strip_browser(text, edge_profile=False)):
        page = _UNREAD_COUNT.sub("", page)
        suffix = _TEAMS_PAGE.search(page)
        if suffix:
            return page[: suffix.start()]
    return None


def is_teams_web_title(window_title: str) -> bool:
    return _teams_page(window_title) is not None


def teams_web_title(window_title: str) -> str | None:
    """The meeting's name from a browser window showing Teams, or None when it only names a section of Teams, or is
    a chat or channel (see teams_window_title)."""
    page = _teams_page(window_title)
    return None if page is None else _teams_meeting_name([page])


@dataclass(frozen=True)
class _BrowserSession:
    """What was decided about a browser's microphone session when it first appeared."""

    browser: str  # the exe's name without ".exe": "chrome", "msedge" or "shift"
    started: int  # the session's LastUsedTimeStart, a FILETIME
    provider: str | None  # "meet", "teams", or None when no Meet or Teams tab was in front
    title: str | None

    def call(self) -> Call:
        return Call(self.provider, f"{self.provider}:{self.browser}:{self.started}", filetime_age(self.started), self.title)


class CallWatch:
    """Finds the call under way among the enabled providers. Kept for the watcher's life, because a
    browser using the microphone is a Meet or Teams call only if a Meet or Teams tab was in front when
    that began, and that is decided once per session, so switching tabs later doesn't end the call."""

    def __init__(self) -> None:
        self._zoom: Call | None = None
        self._teams: list[Call] = []
        self._browser: dict[tuple[str, int], _BrowserSession] = {}  # by browser and start

    def calls(self, enabled: Mapping[str, bool], table: ProcessTable | None = None) -> list[Call]:
        """Every call under way for the providers that are switched on. A provider that is off isn't read, except
        that the browsers' microphone record is read when Meet or Teams is on, and a call in a browser is only
        reported when its own provider is on.

        When the process list couldn't be read (``{}``), that says nothing about whether Zoom, Teams or a
        browser is running, so their calls stay as they were until a read that works. The same goes for
        the microphone record, Teams' and the browsers'."""
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
        if enabled.get("meet") or enabled.get("teams"):
            found.extend(self._browser_calls(enabled, table, known))
        else:
            self._browser = {}
        return found

    def current(self, enabled: Mapping[str, bool], table: ProcessTable | None = None) -> Call | None:
        """The most recently started call, when several are under way."""
        found = self.calls(enabled, table)
        return min(found, key=_age) if found else None

    def _browser_calls(self, enabled: Mapping[str, bool], table: ProcessTable | None, known: bool) -> list[Call]:
        if not known:
            return self._reported(enabled)
        try:
            apps = registry_apps(False, _is_browser, strict=True)
        except OSError:
            # The record couldn't be read this time. That isn't the same as no one using the microphone, so
            # the sessions known stay as they were, and the next good read sorts out which have ended.
            return self._reported(enabled)
        sessions: dict[tuple[str, int], _BrowserSession] = {}
        for app in apps:
            exe = _browser_exe(app.name)
            # A browser that crashed can leave the record saying it still uses the microphone.
            if not app.in_use or not pids_named(exe, table):
                continue
            key = (exe.removesuffix(".exe"), app.started)
            sessions[key] = self._browser.get(key) or self._first_look(*key, table)
        self._browser = sessions  # a session that has ended is forgotten
        return self._reported(enabled)

    def _reported(self, enabled: Mapping[str, bool]) -> list[Call]:
        """The calls of the sessions known that are Meet or Teams calls whose switch is on."""
        return [session.call() for session in self._browser.values() if session.provider and enabled.get(session.provider)]

    def _first_look(self, browser: str, started: int, table: ProcessTable | None) -> _BrowserSession:
        """Which call the browser's microphone session is, from its windows now: a Meet tab in front makes it Meet,
        and otherwise a Teams tab makes it Teams, because a Meet tab is only open for a meeting while a Teams tab is
        often open all day. It doesn't depend on which switches are on."""
        titles = app_window_titles((f"{browser}.exe",), table)
        meet_windows = [title for title in titles if is_meet_title(title)]
        if meet_windows:
            name = next((name for name in map(meet_title, meet_windows) if name), None)
            return _BrowserSession(browser, started, "meet", name)
        teams_windows = [title for title in titles if is_teams_web_title(title)]
        if teams_windows:
            name = next((name for name in map(teams_web_title, teams_windows) if name), None)
            return _BrowserSession(browser, started, "teams", name)
        return _BrowserSession(browser, started, None, None)


def _age(call: Call) -> float:
    return math.inf if call.age_seconds is None else call.age_seconds


def _reaged(call: Call) -> Call:
    """A call kept through a read that failed, with its age worked out again from the start in its session id, so it
    goes on growing older instead of keeping the age it had. A Zoom id ends in the start (Unix seconds) and a desktop
    Teams id is the start (a FILETIME). One that has no start in its id keeps what it had, and so does a call in a
    browser: its id has three parts as Zoom's does, but its session works out its own age each time."""
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
    a call is noticed. For Teams it is the desktop app: Teams in a browser needs nothing installed. None for
    Meet, which is in a browser."""
    if provider == "zoom":
        # For this user, or for every user on the PC (a 32-bit install is under Program Files (x86)).
        return any(_exists(variable, "Zoom", "bin", "Zoom.exe") for variable in ("APPDATA", "ProgramFiles", "ProgramFiles(x86)"))
    if provider == "teams":
        return _exists("LOCALAPPDATA", "Microsoft", "WindowsApps", TEAMS_EXE) or bool(registry_apps(True, _is_teams))
    return None


def _exists(variable: str, *parts: str) -> bool:
    root = os.environ.get(variable)
    return bool(root) and os.path.exists(os.path.join(root, *parts))
