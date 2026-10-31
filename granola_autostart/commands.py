"""Settings tells the running watcher to pause, resume, or quit, just as the tray menu does.

Each command is a named Windows event in this sign-in session. The watcher creates the events and
waits on them; Settings opens one and sets it. Nothing is written to disk, so a command can't be
left over for a watcher that starts later.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger("granola_autostart")
COMMANDS = ("pause-hour", "pause-today", "resume", "quit")
PREFIX = "Local\\GranolaAutostart-"
EVENT_MODIFY_STATE = 0x0002
WAIT_MS = 1000
WAIT_FAILED = 0xFFFFFFFF


def _kernel32():
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
    return kernel32


def send(command: str) -> bool:
    """True once the running watcher has been told; False when no watcher is listening."""
    if command not in COMMANDS:
        raise ValueError(command)
    kernel32 = _kernel32()
    handle = kernel32.OpenEventW(EVENT_MODIFY_STATE, False, PREFIX + command)
    if not handle:
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def listen(handlers: dict[str, Callable[[], None]], stop: threading.Event) -> None:
    """Run a command's handler each time it is sent, until stop is set. Blocks, so give it a thread."""
    kernel32 = _kernel32()
    names = list(handlers)
    # Auto-reset events: each send wakes one wait, then the event clears itself.
    handles = [kernel32.CreateEventW(None, False, False, PREFIX + name) for name in names]
    waitable = (wintypes.HANDLE * len(handles))(*handles)
    try:
        if not all(handles):
            log.error("Settings can't pause, resume, or quit this watcher: its events could not be created")
            return
        while not stop.is_set():
            index = kernel32.WaitForMultipleObjects(len(handles), waitable, False, WAIT_MS)
            if index == WAIT_FAILED:
                log.error("stopped listening for Settings' commands: the wait failed")
                return
            if index < len(handles):
                log.info("%s from Settings", names[index])
                try:
                    handlers[names[index]]()
                except Exception:
                    log.exception("could not %s", names[index])
    finally:
        for handle in filter(None, handles):
            kernel32.CloseHandle(handle)
