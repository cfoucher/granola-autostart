"""Which note Granola's own window shows as being transcribed, read the way a screen reader reads it, through
Windows UI Automation (UIA). Granola is Electron, and its window exposes a ``Meeting controls`` group beside the note's
title, with a ``Stop`` button in it while that note is being transcribed. This reads only the title and that button,
never the notes or the transcript.

UIA calls can hang when the app they ask has, so they run on a worker thread of their own, and a check gives up after
a few seconds. The UIA layer is ctypes over COM's vtables, so the app needs no extra package. Every COM pointer is
released and every string freed, because this runs for as long as the app does."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import threading
from collections.abc import Iterable, Iterator
from ctypes import wintypes
from typing import Protocol

from .calls import pids_named, window_handles

log = logging.getLogger("granola_autostart")

CHECK_TIMEOUT_SECONDS = 3.0
GRANOLA_EXE = "Granola.exe"
WINDOW_CLASSES = frozenset({"Chrome_WidgetWin_1"})  # Electron's top-level windows, of which Granola has several
CONTROLS_NAME = "Meeting controls"
STOP_NAME = "Stop"  # while it transcribes; it reads "Resume" afterwards

# UI Automation's ids: tree scopes, control types, and the properties used.
SCOPE_CHILDREN = 2
SCOPE_DESCENDANTS = 4
BUTTON, EDIT, GROUP = 50000, 50004, 50026
CONTROL_TYPE_PROPERTY, NAME_PROPERTY, VALUE_PROPERTY = 30003, 30005, 30045

COINIT_MULTITHREADED = 0
CLSCTX_INPROC_SERVER = 1
VT_I4, VT_BSTR = 3, 8
# Where each method is in its interface's vtable: IUnknown's three, then the interface's own in order.
RELEASE = 2
AUTOMATION_ELEMENT_FROM_HANDLE, AUTOMATION_CONTROL_VIEW_WALKER = 6, 14
AUTOMATION_PROPERTY_CONDITION, AUTOMATION_AND_CONDITION = 23, 25
ELEMENT_FIND_FIRST, ELEMENT_PROPERTY_VALUE = 5, 10
WALKER_PARENT = 3


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_ulong), ("data2", ctypes.c_ushort), ("data3", ctypes.c_ushort), ("data4", ctypes.c_ubyte * 8)]


class _VariantValue(ctypes.Union):
    _fields_ = [("number", ctypes.c_long), ("pointer", ctypes.c_void_p), ("record", ctypes.c_void_p * 2)]


class VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("reserved", ctypes.c_ushort * 3), ("value", _VariantValue)]


_ole32 = ctypes.OleDLL("ole32")  # raises OSError for a failed HRESULT
_oleaut32 = ctypes.WinDLL("oleaut32")

_ole32.CLSIDFromString.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(GUID)]
_ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wintypes.DWORD]
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    wintypes.DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
_oleaut32.SysAllocString.argtypes = [ctypes.c_wchar_p]
_oleaut32.SysAllocString.restype = ctypes.c_void_p
_oleaut32.SysFreeString.argtypes = [ctypes.c_void_p]
_oleaut32.SysFreeString.restype = None
_oleaut32.VariantClear.argtypes = [ctypes.POINTER(VARIANT)]


def _guid(text: str) -> GUID:
    guid = GUID()
    _ole32.CLSIDFromString("{" + text + "}", ctypes.byref(guid))
    return guid


CLSID_AUTOMATION = _guid("ff48dba4-60ef-4201-aa87-54103eef594e")
IID_AUTOMATION = _guid("30cbe57d-d9d0-452a-ab13-7ac5ac4825ee")


class _ComObject:
    """A COM interface pointer that this owns one reference to, and lets go of in ``release``."""

    def __init__(self, address: int) -> None:
        self.address: int | None = address

    def call(self, index: int, argtypes: list, *args):
        """Call the method at ``index`` of the interface's vtable. A failed HRESULT raises OSError."""
        vtable = ctypes.cast(self.address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        return ctypes.WINFUNCTYPE(ctypes.HRESULT, ctypes.c_void_p, *argtypes)(vtable[index])(self.address, *args)

    def release(self) -> None:
        if self.address is None:
            return
        address, self.address = self.address, None
        vtable = ctypes.cast(address, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[RELEASE])(address)

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _returned(call) -> int | None:
    """The interface pointer a method hands back through its out parameter, which is null when nothing was found."""
    out = ctypes.c_void_p()
    call(ctypes.byref(out))
    return out.value


class Element(_ComObject):
    """One UIA element: a control in Granola's window."""

    def __init__(self, address: int, automation: Automation) -> None:
        super().__init__(address)
        self._automation = automation

    def find_first(self, scope: int, control_type: int, name: str | None = None) -> Element | None:
        """The first element of that control type (and exactly that name, when given) in ``scope``, in tree order."""
        with self._automation.condition(control_type, name) as condition:
            found = _returned(
                lambda out: self.call(
                    ELEMENT_FIND_FIRST, [ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], scope, condition.address, out
                )
            )
        return None if found is None else Element(found, self._automation)

    def parent(self) -> Element | None:
        with self._automation.control_view_walker() as walker:
            found = _returned(
                lambda out: walker.call(WALKER_PARENT, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], self.address, out)
            )
        return None if found is None else Element(found, self._automation)

    def value(self) -> str | None:
        """The element's Value, which for a text box is its text, or None when it has no string value."""
        variant = VARIANT()
        self.call(ELEMENT_PROPERTY_VALUE, [ctypes.c_int, ctypes.POINTER(VARIANT)], VALUE_PROPERTY, ctypes.byref(variant))
        try:
            pointer = variant.value.pointer
            return ctypes.wstring_at(pointer) if variant.vt == VT_BSTR and pointer else None
        finally:
            _oleaut32.VariantClear(ctypes.byref(variant))


class Automation(_ComObject):
    """The UIA client object. Create it on a thread that has called CoInitializeEx (see ``_apartment``)."""

    def __init__(self) -> None:
        address = ctypes.c_void_p()
        _ole32.CoCreateInstance(
            ctypes.byref(CLSID_AUTOMATION), None, CLSCTX_INPROC_SERVER, ctypes.byref(IID_AUTOMATION), ctypes.byref(address)
        )
        super().__init__(address.value)

    def from_handle(self, hwnd: int) -> Element:
        found = _returned(
            lambda out: self.call(AUTOMATION_ELEMENT_FROM_HANDLE, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], hwnd, out)
        )
        if found is None:
            raise OSError("UI Automation gave no element for the window")
        return Element(found, self)

    def control_view_walker(self) -> _ComObject:
        found = _returned(lambda out: self.call(AUTOMATION_CONTROL_VIEW_WALKER, [ctypes.POINTER(ctypes.c_void_p)], out))
        if found is None:
            raise OSError("UI Automation gave no tree walker")
        return _ComObject(found)

    def condition(self, control_type: int, name: str | None = None) -> _ComObject:
        """A condition that matches a control type and, when given, a name exactly."""
        by_type = self._property_condition(CONTROL_TYPE_PROPERTY, control_type)
        if name is None:
            return by_type
        with by_type:
            by_name = self._property_condition(NAME_PROPERTY, name)
            with by_name:
                both = _returned(
                    lambda out: self.call(
                        AUTOMATION_AND_CONDITION,
                        [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)],
                        by_type.address,
                        by_name.address,
                        out,
                    )
                )
        if both is None:
            raise OSError("UI Automation made no condition")
        return _ComObject(both)

    def _property_condition(self, prop: int, value: int | str) -> _ComObject:
        variant = VARIANT()
        if isinstance(value, str):
            variant.vt = VT_BSTR
            variant.value.pointer = _oleaut32.SysAllocString(value)
        else:
            variant.vt = VT_I4
            variant.value.number = value
        try:
            found = _returned(
                lambda out: self.call(
                    AUTOMATION_PROPERTY_CONDITION, [ctypes.c_int, VARIANT, ctypes.POINTER(ctypes.c_void_p)], prop, variant, out
                )
            )
        finally:
            _oleaut32.VariantClear(ctypes.byref(variant))  # frees the string, which the condition has its own copy of
        if found is None:
            raise OSError("UI Automation made no condition")
        return _ComObject(found)


class WindowElement(Protocol):
    """What ``note_title`` needs of an element, so that it can be tested with a stand-in for Granola's window."""

    def find_first(self, scope: int, control_type: int, name: str | None = None) -> WindowElement | None: ...

    def parent(self) -> WindowElement | None: ...

    def value(self) -> str | None: ...

    def release(self) -> None: ...

    def __enter__(self): ...

    def __exit__(self, *_exc) -> None: ...


def note_title(window: WindowElement) -> str | None:
    """The title of the note this window shows as being transcribed, or None for any other state of it: no note open, a
    note that isn't recording (its button reads Resume), or a title that is blank. The title is the first text box
    beside the ``Meeting controls`` group, its Value, because its Name is only the placeholder "New note"."""
    group = window.find_first(SCOPE_DESCENDANTS, GROUP, CONTROLS_NAME)
    if group is None:
        return None
    with group:
        stop = group.find_first(SCOPE_DESCENDANTS, BUTTON, STOP_NAME)
        if stop is None:
            return None
        stop.release()
        parent = group.parent()
        if parent is None:
            return None
        with parent:
            box = parent.find_first(SCOPE_CHILDREN, EDIT)
            if box is None:
                return None
            with box:
                title = box.value()
    return (title or "").strip() or None


def window_recording_title(automation: Automation, hwnd: int) -> str | None:
    """``note_title`` of the window with this handle."""
    with automation.from_handle(hwnd) as window:
        return note_title(window)


@contextlib.contextmanager
def _apartment() -> Iterator[None]:
    """COM for this thread, in the multithreaded apartment, undone at the end."""
    _ole32.CoInitializeEx(None, COINIT_MULTITHREADED)
    try:
        yield
    finally:
        _ole32.CoUninitialize()


def granola_windows(pids: set[int] | None = None) -> list[int]:
    """The top-level windows of Granola's processes (or of ``pids``) that could hold the note view, shown or not: only
    one of them does, and which is not known from outside."""
    if pids is None:
        pids = set(pids_named(GRANOLA_EXE))
    return window_handles(pids, WINDOW_CLASSES, visible_only=False)


def check_windows(windows: Iterable[int]) -> str | None:
    """The recording note's title in the first of these windows that shows one. It starts COM on the calling thread, so
    that must be a thread of its own (see ``recording_title``); a window that can't be read is skipped."""
    with _apartment(), Automation() as automation:
        for hwnd in windows:
            try:
                title = window_recording_title(automation, hwnd)
            except Exception:
                log.debug("could not read Granola's window %s", hwnd, exc_info=True)
                continue
            if title:
                return title
    return None


_gate = threading.Lock()
_worker: threading.Thread | None = None


def recording_title(timeout: float = CHECK_TIMEOUT_SECONDS) -> str | None:
    """The title of the note Granola shows as being transcribed, or None whenever that isn't known for sure: Granola not
    running, no such window, a note that isn't recording, a blank title, a failure, or a check that took longer than
    ``timeout`` seconds. It never raises.

    The check runs on a worker thread. One that hangs is not waited on again: while it is still running, later calls
    return None at once, without starting another."""
    global _worker
    try:
        with _gate:
            if _worker is not None and _worker.is_alive():
                return None
            windows = granola_windows()
            if not windows:
                return None
            found: list[str | None] = []
            worker = threading.Thread(target=_check, args=(windows, found), name="granola-window", daemon=True)
            _worker = worker
            worker.start()
        worker.join(timeout)
        if worker.is_alive():
            log.warning("Granola's window did not answer within %ss; not checking it until it does", timeout)
            return None
        return (found[0] or None) if found else None
    except Exception:
        log.debug("could not check Granola's window", exc_info=True)
        return None


def _check(windows: list[int], found: list[str | None]) -> None:
    try:
        found.append(check_windows(windows))
    except Exception:
        log.debug("could not read Granola's window", exc_info=True)
