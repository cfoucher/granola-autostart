"""Detect a call under way on Zoom, Microsoft Teams (its desktop app, or in a browser), or Google Meet (in a browser)."""

from __future__ import annotations

import ctypes
import math
import os
import re
import threading
import time
import uuid
import winreg
from collections.abc import Iterable, Mapping
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

# Window titles that name the app rather than the meeting.
GENERIC_ZOOM_TITLES = {"zoom", "zoom meeting", "zoom workplace", "zoom webinar", "meeting chat"}
# Teams' own pages. A title that names Microsoft Teams is Teams' too (see _teams_meeting_name).
GENERIC_TEAMS_TITLES = {
    "chat", "activity", "calendar", "calls", "teams", "files", "apps", "communities", "onedrive", "meeting", "meetings",
    "home", "people", "copilot",
}
MAX_TITLE = 200
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

# Windows' microphone privacy settings list each packaged app that has asked for the microphone. Only Settings' hint
# that Teams is installed reads it. When an app last used the microphone was recorded there too, but Windows 11 has
# stopped updating that (seen on build 26300 since its October 2026 update), so calls aren't noticed from it.
CONSENT_KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\microphone"
TEAMS_PACKAGE_PREFIX = "msteams_"
TEAMS_EXE = "ms-teams.exe"
TEAMS_PREFIX = "ms-teams"  # ms-teams.exe, and the other processes of the Teams app that might record
WEBVIEW_EXE = "msedgewebview2.exe"  # the web view Teams draws in, which could be where it records
MAX_GENERATIONS = 8  # how far up from a web view process to look for the Teams process it belongs to
SAVED_FOR = 60.0  # seconds after it is made that a CallWatch's first look can still find a saved call (see CallWatch)
BROWSERS = ("chrome.exe", "msedge.exe", "shift.exe")  # the browsers whose Meet and Teams calls are noticed
FILETIME_TICKS = 10_000_000  # 100 ns ticks per second
FILETIME_UNIX_EPOCH = 11_644_473_600  # seconds from 1601-01-01 to 1970-01-01

# Windows' audio engine (Core Audio): its audio sessions are the volume mixer's list of which apps have a stream on a
# device. The ids are Microsoft's, from mmdeviceapi.h and audiopolicy.h.
CLSID_MM_DEVICE_ENUMERATOR = "BCDE0395-E52F-467C-8E3D-C4579291692E"
IID_IMM_DEVICE_ENUMERATOR = "A95664D2-9614-4F35-A746-DE8DB63617E6"
IID_IAUDIO_SESSION_MANAGER2 = "77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"
IID_IAUDIO_SESSION_CONTROL2 = "BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"
E_CAPTURE = 1  # EDataFlow: recording devices
DEVICE_STATE_ACTIVE = 1  # plugged in and switched on
CLSCTX_ALL = 23
AUDIO_SESSION_STATE_ACTIVE = 1  # a stream in the session is running
COINIT_MULTITHREADED = 0
RPC_E_CHANGED_MODE = -2147417850  # COM is already set up on this thread in the other mode, which works as well
# Each method's place in its interface's table, after IUnknown's QueryInterface (0), AddRef (1) and Release (2).
QUERY_INTERFACE, RELEASE = 0, 2
ENUM_AUDIO_ENDPOINTS = 3  # IMMDeviceEnumerator
COLLECTION_GET_COUNT, COLLECTION_ITEM = 3, 4  # IMMDeviceCollection
DEVICE_ACTIVATE = 3  # IMMDevice
GET_SESSION_ENUMERATOR = 5  # IAudioSessionManager2
SESSIONS_GET_COUNT, SESSIONS_GET_SESSION = 3, 4  # IAudioSessionEnumerator
SESSION_GET_STATE, SESSION_GET_PROCESS_ID = 3, 14  # IAudioSessionControl2

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
ole32 = ctypes.WinDLL("ole32")  # its own copy, so the signatures set below don't change any other library's calls


class GUID(ctypes.Structure):
    _fields_ = [("bytes", ctypes.c_ubyte * 16)]

    @classmethod
    def of(cls, text: str) -> GUID:
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


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
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
ole32.CoInitializeEx.restype = ctypes.c_long  # an HRESULT, checked by hand: one failure is fine (RPC_E_CHANGED_MODE)
ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
ole32.CoCreateInstance.restype = ctypes.HRESULT  # a failure raises OSError


class ProcessTable(dict[str, list[int]]):
    """Lower-cased image name to process ids, with each process's parent in ``parents``. A plain dict of the same
    shape works too, as one with no parents known."""

    def __init__(self, *args, parents: dict[int, int] | None = None, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.parents = parents or {}


@dataclass(frozen=True)
class Call:
    provider: str  # "zoom", "teams" or "meet"
    session_id: str  # the provider, a colon, then what tells this call from the next
    age_seconds: float | None  # None when Windows wouldn't say when it started
    title: str | None  # the meeting's name, when its windows show one


def process_table() -> ProcessTable:
    """Map lower-cased image name to process ids, from one Toolhelp snapshot, with each one's parent. A snapshot
    Windows won't give is empty, which means not known: a real list always has Windows' own processes in it."""
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        return ProcessTable()
    table = ProcessTable()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            pid = int(entry.th32ProcessID)
            table.setdefault(entry.szExeFile.casefold(), []).append(pid)
            table.parents[pid] = int(entry.th32ParentProcessID)
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


# The microphone, which Teams and the browsers' calls are noticed from

def filetime_age(ticks: int) -> float:
    """Seconds since a FILETIME, which counts 100 ns from 1601."""
    return max(0.0, time.time() - (ticks / FILETIME_TICKS - FILETIME_UNIX_EPOCH))


def filetime_now() -> int:
    return int((time.time() + FILETIME_UNIX_EPOCH) * FILETIME_TICKS)


def _filetime(moment: datetime) -> int:
    return int((moment.timestamp() + FILETIME_UNIX_EPOCH) * FILETIME_TICKS)


_com_threads = threading.local()


def _com_ready() -> None:
    """COM, set up once on each thread that asks the audio engine. A thread that already has it in the other mode is
    fine as it is."""
    if getattr(_com_threads, "ready", False):
        return
    result = ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    if result < 0 and result != RPC_E_CHANGED_MODE:
        raise ctypes.WinError(result)
    _com_threads.ready = True


def _method(interface: ctypes.c_void_p, index: int, restype, *argtypes):
    """Method ``index`` of a COM interface, from the interface's table of methods."""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])


def _call(interface: ctypes.c_void_p, index: int, argtypes: tuple, *args) -> None:
    """Call method ``index`` of a COM interface. A failure raises OSError (ctypes.HRESULT does that)."""
    _method(interface, index, ctypes.HRESULT, *argtypes)(interface, *args)


def _value(interface: ctypes.c_void_p, index: int, kind):
    """What a method that fills in one value of this ctypes kind gives."""
    out = kind()
    _call(interface, index, (ctypes.POINTER(kind),), ctypes.byref(out))
    return out.value


class _Interfaces:
    """The COM interfaces one look at the audio engine gets, each released when the look is over, however it ends."""

    def __init__(self) -> None:
        self.held: list[ctypes.c_void_p] = []

    def __enter__(self) -> _Interfaces:
        return self

    def __exit__(self, *_exc) -> None:
        for interface in reversed(self.held):
            _method(interface, RELEASE, ctypes.c_ulong)(interface)

    def create(self, clsid: str, iid: str) -> ctypes.c_void_p:
        out = ctypes.c_void_p()
        ole32.CoCreateInstance(ctypes.byref(GUID.of(clsid)), None, CLSCTX_ALL, ctypes.byref(GUID.of(iid)), ctypes.byref(out))
        return self._keep(out)

    def get(self, interface: ctypes.c_void_p, index: int, argtypes: tuple = (), *args) -> ctypes.c_void_p:
        """The interface method ``index`` gives, through the pointer it fills in after these arguments."""
        out = ctypes.c_void_p()
        _call(interface, index, (*argtypes, ctypes.POINTER(ctypes.c_void_p)), *args, ctypes.byref(out))
        return self._keep(out)

    def _keep(self, out: ctypes.c_void_p) -> ctypes.c_void_p:
        if not out.value:
            raise OSError("Windows gave no interface")
        self.held.append(out)
        return out


def capture_pids() -> set[int]:
    """The processes recording from a microphone now: each has a running stream on a recording device that is plugged
    in, whichever device that is (the volume mixer's list). Windows only says which processes, so nothing is ever
    recorded or listened to. Raises OSError when the audio engine couldn't be asked, which isn't the same as no one
    recording."""
    _com_ready()
    manager_iid = GUID.of(IID_IAUDIO_SESSION_MANAGER2)
    control_iid = GUID.of(IID_IAUDIO_SESSION_CONTROL2)
    pids: set[int] = set()
    with _Interfaces() as com:
        enumerator = com.create(CLSID_MM_DEVICE_ENUMERATOR, IID_IMM_DEVICE_ENUMERATOR)
        devices = com.get(enumerator, ENUM_AUDIO_ENDPOINTS, (ctypes.c_int, wintypes.DWORD), E_CAPTURE, DEVICE_STATE_ACTIVE)
        for index in range(_value(devices, COLLECTION_GET_COUNT, ctypes.c_uint)):
            device = com.get(devices, COLLECTION_ITEM, (ctypes.c_uint,), index)
            activate = (ctypes.POINTER(GUID), wintypes.DWORD, ctypes.c_void_p)
            manager = com.get(device, DEVICE_ACTIVATE, activate, ctypes.byref(manager_iid), CLSCTX_ALL, None)
            sessions = com.get(manager, GET_SESSION_ENUMERATOR)
            for number in range(_value(sessions, SESSIONS_GET_COUNT, ctypes.c_int)):
                control = com.get(sessions, SESSIONS_GET_SESSION, (ctypes.c_int,), number)
                session = com.get(control, QUERY_INTERFACE, (ctypes.POINTER(GUID),), ctypes.byref(control_iid))
                if _value(session, SESSION_GET_STATE, ctypes.c_int) != AUDIO_SESSION_STATE_ACTIVE:
                    continue
                try:
                    pids.add(_value(session, SESSION_GET_PROCESS_ID, wintypes.DWORD))
                except OSError:
                    pass  # a session no one process owns can't be anyone's call; the others still count
    pids.discard(0)
    return pids


def mic_users(table: Mapping[str, list[int]]) -> dict[str, set[int]]:
    """The apps whose calls are noticed that are recording from a microphone now, each with its processes doing it:
    "teams" for the Teams app, or a browser's exe name without ".exe". Raises OSError as capture_pids does."""
    names = {pid: name for name, pids in table.items() for pid in pids}
    parents = getattr(table, "parents", {})
    users: dict[str, set[int]] = {}
    for pid in capture_pids():
        app = _mic_app(pid, names, parents)
        if app is not None:
            users.setdefault(app, set()).add(pid)
    return users


def _mic_app(pid: int | None, names: Mapping[int, str], parents: Mapping[int, int]) -> str | None:
    """Which app a recording process is part of, if it is one whose calls are noticed. A browser is known by its own
    name, so a program it started (Zoom, from a link) isn't taken for it. Teams is known by its own name, or for a web
    view by that of the Teams process it belongs to."""
    name = names.get(pid)
    if name in BROWSERS:
        return name.removesuffix(".exe")
    for _ in range(MAX_GENERATIONS):
        if name is None:
            return None
        if name.startswith(TEAMS_PREFIX):
            return "teams"
        if name != WEBVIEW_EXE:
            return None
        pid = parents.get(pid)
        name = names.get(pid)
    return None


def _earliest_start(pids: Iterable[int]) -> int | None:
    """When the earliest of these processes started, as a FILETIME, or None when Windows won't say for any of them."""
    starts = [_filetime(started) for started in map(process_start, pids) if started is not None]
    return min(starts) if starts else None


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


# Google Meet and Microsoft Teams, in Chrome, Edge or Shift

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
class _MicSession:
    """One app's use of the microphone (see mic_users), and what was decided about it when it began."""

    app: str  # "teams" for the Teams app, or the browser's exe name without ".exe": "chrome", "msedge" or "shift"
    started: int  # a FILETIME: when it began, or for one already under way when it was first seen, when that was
    known: bool  # whether ``started`` is when it began
    provider: str | None  # "meet" or "teams", or None for a browser that had neither tab in front
    title: str | None

    def call(self) -> Call:
        """Its call. A browser's id names the browser ("meet:shift:<start>"), and the Teams app's is "teams:<start>"."""
        place = "" if self.app == "teams" else f"{self.app}:"
        age = filetime_age(self.started) if self.known else None
        return Call(self.provider, f"{self.provider}:{place}{self.started}", age, self.title)


def _parse_mic_id(session_id: str) -> tuple[str, str, int] | None:
    """A microphone call's id as (provider, app, start): "teams:<start>" is the Teams app's, and "meet:shift:<start>" or
    "teams:shift:<start>" a browser's. None for any other id, such as Zoom's or a calendar meeting's."""
    parts = session_id.split(":")
    if len(parts) == 2 and parts[0] == "teams":
        provider, app, start = "teams", "teams", parts[1]
    elif len(parts) == 3 and parts[0] in ("meet", "teams") and f"{parts[1]}.exe" in BROWSERS:
        provider, app, start = parts
    else:
        return None
    return (provider, app, int(start)) if start.isdigit() else None


class CallWatch:
    """Finds the call under way among the enabled providers. Kept for the watcher's life, because Windows says which
    apps are using the microphone but not since when, so each use is timed from when it was first seen. And a browser
    using the microphone is a Meet or Teams call only if a Meet or Teams tab was in front when that began, which is
    decided once per use, so switching tabs later doesn't end the call.

    ``saved`` is the call ids the watcher saved before it last stopped. A use of the microphone already under way at
    the first look takes the id of the saved call it can be (see _saved_call), so a restart in the middle of a call
    doesn't make it a new one. With no such call, when it began isn't known. Only the first look counts, and only in
    the watch's first SAVED_FOR seconds: a look later than that (with Meet and Teams off, or notes off, at the start)
    comes long enough after the stop for a saved call to have ended and another begun."""

    def __init__(self, saved: Iterable[str] = ()) -> None:
        self._zoom: Call | None = None
        self._mic: dict[str, _MicSession] = {}  # by app (see mic_users)
        self._looked = False  # whether the microphone has been read since Meet or Teams was switched on
        self._read = False  # whether the last look found out which calls are on the microphone (see under_way)
        self._saved = tuple(saved)
        self._saved_until = time.time() + SAVED_FOR

    def calls(self, enabled: Mapping[str, bool], table: Mapping[str, list[int]] | None = None) -> list[Call]:
        """Every call under way for the providers that are switched on. A provider that is off isn't read, except
        that the microphone is read when Meet or Teams is on, and a call is only reported when its own provider is on.

        When the process list couldn't be read (empty), that says nothing about whether Zoom, Teams or a browser is
        running, so their calls stay as they were until a read that works. The same goes for the microphone."""
        found: list[Call] = []
        if table is None and any(enabled.values()):
            table = process_table()  # one list for every provider
        known = bool(table)
        self._read = False
        if enabled.get("zoom"):
            if known:
                self._zoom = zoom_call(table)
            elif self._zoom is not None:
                self._zoom = _reaged(self._zoom)
            if self._zoom is not None:
                found.append(self._zoom)
        else:
            self._zoom = None
        if enabled.get("meet") or enabled.get("teams"):
            if known:
                self._read_mic(enabled, table)
            found.extend(session.call() for session in self._sessions() if session.provider and enabled.get(session.provider))
        else:
            self._mic = {}
            self._looked = False
            self._read = True  # none, as far as it is concerned
            self._saved = ()  # turned on again, it isn't a restart's first look
        return found

    def current(self, enabled: Mapping[str, bool], table: Mapping[str, list[int]] | None = None) -> Call | None:
        """The most recently started call, when several are under way."""
        found = self.calls(enabled, table)
        return min(found, key=_age) if found else None

    def under_way(self) -> list[str] | None:
        """The ids of the calls on the microphone under way at the last look, the current one or not, whichever switches
        are on: for the watcher to save, so after a restart each is still the same call (see ``saved``). None when that
        look couldn't read the microphone or the process list, so what is under way isn't known: the calls kept may
        have ended, and what was saved before is still the best there is."""
        if not self._read:
            return None
        return [session.call().session_id for session in self._sessions() if session.provider]

    def _read_mic(self, enabled: Mapping[str, bool], table: Mapping[str, list[int]]) -> None:
        try:
            using = mic_users(table)
        except OSError:
            # The audio engine couldn't be asked this time. That isn't the same as no one using the microphone, so the
            # uses known stay as they were, and the next good read sorts out which have ended.
            return
        now = filetime_now()
        sessions: dict[str, _MicSession] = {}
        for app, pids in using.items():
            session = self._mic.get(app) or self._begin(app, pids, now, table)
            if app == "teams" and enabled.get("teams"):
                # The Teams app's windows are read every time, so its meeting's name is found however late its window comes.
                session = replace(session, title=teams_window_title(app_window_titles((TEAMS_EXE,), table)))
            sessions[app] = session
        self._mic = sessions  # a use that has ended is forgotten
        self._looked = True
        self._read = True
        self._saved = ()  # only the first look after a restart can find a saved call, not a later one after Meet and Teams were off

    def _sessions(self) -> list[_MicSession]:
        """The Teams app's use of the microphone first, then the browsers', each in the order they began."""
        return sorted(self._mic.values(), key=lambda session: (session.app != "teams", session.started, session.app))

    def _begin(self, app: str, pids: set[int], now: int, table: Mapping[str, list[int]]) -> _MicSession:
        """A use of the microphone seen for the first time. One that began since the last look began about now. One
        already under way at the first look is the saved call it can be, or else began when no one can say."""
        provider, title = ("teams", None) if app == "teams" else self._first_look(app, table)
        if self._looked:
            return _MicSession(app, now, True, provider, title)
        saved = self._saved_call(app, pids, now)
        if saved is None:
            return _MicSession(app, now, False, provider, title)
        saved_provider, started = saved
        return _MicSession(app, started, True, saved_provider, title if saved_provider == provider else None)

    def _saved_call(self, app: str, pids: set[int], now: int) -> tuple[str, int] | None:
        """The newest saved call in this app, as its provider and start, that this use of the microphone can be: one
        begun by now, while the processes using the microphone were already running. A process that started since
        can't have been in that call (after the PC restarted, say)."""
        if time.time() > self._saved_until:
            return None
        saved = [(provider, started) for provider, place, started in filter(None, map(_parse_mic_id, self._saved)) if place == app]
        candidates = [(provider, started) for provider, started in saved if started <= now]
        if not candidates:
            return None
        running_since = _earliest_start(pids)
        fits = [call for call in candidates if running_since is None or running_since <= call[1]]
        return max(fits, key=lambda call: call[1], default=None)

    def _first_look(self, browser: str, table: Mapping[str, list[int]]) -> tuple[str | None, str | None]:
        """Which call the browser's use of the microphone is, and its name, from its windows now: a Meet tab in front
        makes it Meet, and otherwise a Teams tab makes it Teams, because a Meet tab is only open for a meeting while a
        Teams tab is often open all day. It doesn't depend on which switches are on."""
        titles = app_window_titles((f"{browser}.exe",), table)
        meet_windows = [title for title in titles if is_meet_title(title)]
        if meet_windows:
            return "meet", next((name for name in map(meet_title, meet_windows) if name), None)
        teams_windows = [title for title in titles if is_teams_web_title(title)]
        if teams_windows:
            return "teams", next((name for name in map(teams_web_title, teams_windows) if name), None)
        return None, None


def _age(call: Call) -> float:
    return math.inf if call.age_seconds is None else call.age_seconds


def _reaged(call: Call) -> Call:
    """A Zoom call kept through a process list that couldn't be read, with its age worked out again from the start at
    the end of its id (Unix seconds), so it goes on growing older instead of keeping the age it had. One with no start
    in its id keeps what it had. A call on the microphone works out its own age each time."""
    parts = call.session_id.split(":")
    if call.provider != "zoom" or len(parts) != 3 or not parts[2].isdigit():
        return call
    return replace(call, age_seconds=max(0.0, time.time() - int(parts[2])))


def installed(provider: str) -> bool | None:
    """Whether a provider's app was found on this PC, for Settings to say so. It never decides whether
    a call is noticed. For Teams it is the desktop app: Teams in a browser needs nothing installed. None for
    Meet, which is in a browser."""
    if provider == "zoom":
        # For this user, or for every user on the PC (a 32-bit install is under Program Files (x86)).
        return any(_exists(variable, "Zoom", "bin", "Zoom.exe") for variable in ("APPDATA", "ProgramFiles", "ProgramFiles(x86)"))
    if provider == "teams":
        return _exists("LOCALAPPDATA", "Microsoft", "WindowsApps", TEAMS_EXE) or _teams_asked_for_microphone()
    return None


def _teams_asked_for_microphone() -> bool:
    """Whether Windows' microphone privacy settings list the Teams package: it has asked for the microphone, whether
    or not it has used it."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, CONSENT_KEY) as group:
            return any(_is_teams(name) for name in _subkeys(group))
    except OSError:
        return False


def _subkeys(key):
    """The names of a key's subkeys, as far as Windows will list them."""
    index = 0
    while True:
        try:
            yield winreg.EnumKey(key, index)
        except OSError:
            return
        index += 1


def _exists(variable: str, *parts: str) -> bool:
    root = os.environ.get(variable)
    return bool(root) and os.path.exists(os.path.join(root, *parts))
