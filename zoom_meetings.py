"""Detect a Zoom meeting via CptHost.exe, which runs only while a meeting is open."""

from __future__ import annotations

import subprocess
from ctypes import wintypes
from datetime import datetime, timedelta, timezone

import ctypes

CREATE_NO_WINDOW = 0x08000000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
    ctypes.POINTER(FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL


def pids_named(image_name: str) -> list[int]:
    output = subprocess.check_output(
        ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    pids: list[int] = []
    for line in output.splitlines():
        if not line.startswith('"'):
            continue
        parts = line.split('","')
        if len(parts) < 2:
            continue
        try:
            pids.append(int(parts[1].strip('"')))
        except ValueError:
            continue
    return pids


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


def zoom_session() -> tuple[str | None, float | None]:
    """Return (session id, age seconds) while CptHost is running."""
    pids = pids_named("CptHost.exe")
    if not pids:
        return None, None
    pid = min(pids)
    started = process_start(pid)
    if started is None:
        return f"{pid}", None
    age = (datetime.now(timezone.utc) - started).total_seconds()
    return f"{pid}:{int(started.timestamp())}", max(0.0, age)


def granola_running() -> bool:
    return bool(pids_named("Granola.exe"))


def zoom_window_title() -> str | None:
    pids = set(pids_named("CptHost.exe") + pids_named("Zoom.exe"))
    if not pids:
        return None
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
        if buffer.value.strip():
            titles.append(buffer.value.strip())
        return True

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows(enum_proc(callback), 0)
    usable = [title for title in titles if title.casefold() not in {"zoom", "zoom meeting", "zoom workplace", "zoom webinar"}]
    if not usable:
        return None
    return max(usable, key=len)
