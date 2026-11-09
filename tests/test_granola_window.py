"""Granola's window, read through UI Automation. The real UIA layer is tried on windows these tests create themselves,
with standard Win32 controls that Windows' own UIA proxies expose as a Group, a Button and an Edit; the real Granola
is never touched (conftest refuses granola_windows)."""

import ctypes
import logging
import os
import threading
from ctypes import wintypes
from types import SimpleNamespace

import pytest

from granola_autostart import granola_window
from granola_autostart.granola_window import (
    BUTTON,
    EDIT,
    GROUP,
    SCOPE_CHILDREN,
    SCOPE_DESCENDANTS,
    Automation,
    check_windows,
    note_title,
    recording_title,
    window_recording_title,
)

real_granola_windows = granola_window.granola_windows  # conftest replaces it, for every test, with a refusal

# A fake of the note view

CONTROLS, STOP = "Meeting controls", "Stop"


class Node:
    """A control in a stand-in for Granola's window."""

    def __init__(self, control_type, name="", value=None, children=()):
        self.control_type, self.name, self.value, self.children, self.parent = control_type, name, value, list(children), None
        for child in self.children:
            child.parent = self

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class Handle:
    """What the code reads a Node through, and how many of them are still open."""

    def __init__(self, node, opened):
        self.node, self.released = node, False
        opened.append(self)
        self.opened = opened

    def find_first(self, scope, control_type, name=None):
        assert scope in (SCOPE_CHILDREN, SCOPE_DESCENDANTS)
        candidates = self.node.children if scope == SCOPE_CHILDREN else self.node.descendants()
        for node in candidates:
            if node.control_type == control_type and (name is None or node.name == name):
                return Handle(node, self.opened)
        return None

    def parent(self):
        return None if self.node.parent is None else Handle(self.node.parent, self.opened)

    def value(self):
        return self.node.value

    def release(self):
        assert not self.released, "released twice"
        self.released = True

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.release()


def view(button=STOP, title="Design review", controls=CONTROLS, edit_before=()):
    """A window with the note's controls and title side by side, as Granola's has them."""
    group = Node(GROUP, controls, children=[Node(BUTTON, "My notes"), Node(BUTTON, button), Node(BUTTON, "7")])
    return Node(50032, "Granola", children=[*edit_before, group, Node(EDIT, "New note", value=title), Node(EDIT, "Search", value="x")])


def read(window):
    opened = []
    title = note_title(Handle(window, opened))
    still_open = [handle for handle in opened[1:] if not handle.released]  # the window is the caller's own
    assert still_open == [], "an element was not released"
    return title


def test_a_note_being_transcribed_is_named_by_the_title_beside_its_controls():
    assert read(view()) == "Design review"


def test_the_title_is_the_value_trimmed_and_the_first_text_box_beside_the_group_wins():
    assert read(view(title="  Design review \n")) == "Design review"
    assert read(view(edit_before=[Node(EDIT, "Find", value="Earlier box")])) == "Earlier box"


@pytest.mark.parametrize("button", ["Resume", "stop", "Stop ", "Stopped", ""])
def test_only_a_stop_button_says_it_is_transcribing(button):
    assert read(view(button=button)) is None


@pytest.mark.parametrize("controls", ["Meeting control", "meeting controls", "", "Meeting controls "])
def test_only_the_meeting_controls_group_counts(controls):
    assert read(view(controls=controls)) is None


@pytest.mark.parametrize("title", [None, "", "   ", "\n\t"])
def test_a_title_that_is_missing_or_blank_is_no_title(title):
    assert read(view(title=title)) is None


def test_a_window_with_no_note_open_is_no_note():
    assert read(Node(50032, "Granola", children=[Node(EDIT, "Search", value="Design review")])) is None


def test_a_stop_button_outside_the_group_does_not_count():
    group = Node(GROUP, CONTROLS, children=[Node(BUTTON, "My notes")])
    window = Node(50032, "Granola", children=[group, Node(BUTTON, STOP), Node(EDIT, "New note", value="Design review")])
    assert read(window) is None


def test_a_stop_button_deeper_in_the_group_counts_and_a_title_box_deeper_than_its_sibling_does_not():
    deep = Node(GROUP, "Buttons", children=[Node(BUTTON, STOP)])
    group = Node(GROUP, CONTROLS, children=[deep])
    inner = Node(GROUP, "Header", children=[Node(EDIT, "New note", value="Design review")])
    assert read(Node(50032, "Granola", children=[group, inner])) is None
    assert read(Node(50032, "Granola", children=[group, Node(EDIT, "New note", value="Design review")])) == "Design review"


def test_a_group_with_no_parent_or_no_title_box_beside_it_is_no_title():
    group = Node(GROUP, CONTROLS, children=[Node(BUTTON, STOP)])
    opened = []

    class Lone(Handle):
        def find_first(self, scope, control_type, name=None):
            found = super().find_first(scope, control_type, name)
            return found if found is None else Lone(found.node, opened)

        def parent(self):
            return None

    assert note_title(Lone(Node(50032, "Granola", children=[group]), opened)) is None
    assert read(Node(50032, "Granola", children=[group])) is None


# Real windows

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32")
WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
WM_DESTROY, WM_CLOSE = 0x0002, 0x0010
WS_CHILD, WS_VISIBLE, WS_OVERLAPPEDWINDOW = 0x40000000, 0x10000000, 0x00CF0000
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
BS_PUSHBUTTON, BS_GROUPBOX, ES_AUTOHSCROLL = 0x0, 0x7, 0x80
SW_SHOWNA = 8
ERROR_CLASS_ALREADY_EXISTS = 1410


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.DWORD,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    ctypes.c_int,
    wintypes.HWND,
    wintypes.HMENU,
    wintypes.HINSTANCE,
    wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def _procedure(hwnd, message, wparam, lparam):
    if message == WM_DESTROY:
        user32.PostQuitMessage(0)
    return user32.DefWindowProcW(hwnd, message, wparam, lparam)


# One for every window: a class belongs to the thread that registered it no longer than the process, and a second
# window of the same class (on another thread) goes on calling it after the first thread has ended.
WINDOW_PROCEDURE = WNDPROC(_procedure)


class TestWindow:
    """A top-level window of its own thread, which runs a message loop, with a group box named ``controls`` holding a
    button named ``button`` and a text box beside the group, with ``title``. A window must be used from its own thread,
    and UI Automation must not be called from it, so the tests read it from another."""

    __test__ = False  # not a test class, for all it is named so

    def __init__(self, window_class, controls, button, title, visible=True):
        self.window_class, self.controls, self.button, self.title, self.visible = window_class, controls, button, title, visible
        self.hwnd = self.error = None
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run, name="test-window", daemon=True)

    def start(self):
        self.thread.start()
        assert self.ready.wait(10), "the window did not come up"
        if self.error is not None:
            raise self.error
        return self

    def close(self):
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_CLOSE, 0, 0)
        self.thread.join(10)

    def _run(self):
        instance = kernel32.GetModuleHandleW(None)
        try:
            window_class = WNDCLASSEXW(
                cbSize=ctypes.sizeof(WNDCLASSEXW), lpfnWndProc=WINDOW_PROCEDURE, hInstance=instance, lpszClassName=self.window_class
            )
            if not user32.RegisterClassExW(ctypes.byref(window_class)) and ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
                raise ctypes.WinError(ctypes.get_last_error())
            style = WS_OVERLAPPEDWINDOW | (WS_VISIBLE if self.visible else 0)
            # Off the screen and never activated, so it can't get in the way of whoever is using this PC.
            top = self._create(self.window_class, "Test note view", style, None, instance, -30000, -30000, 400, 200, WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            group = self._create("BUTTON", self.controls, WS_CHILD | WS_VISIBLE | BS_GROUPBOX, top, instance, 5, 5, 200, 80)
            self._create("BUTTON", self.button, WS_CHILD | WS_VISIBLE | BS_PUSHBUTTON, group, instance, 10, 25, 80, 30)
            self._create("EDIT", self.title, WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL, top, instance, 5, 100, 300, 24)
            if self.visible:
                user32.ShowWindow(top, SW_SHOWNA)
            self.hwnd = top
        except BaseException as error:
            self.error = error
            return
        finally:
            self.ready.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
        user32.UnregisterClassW(self.window_class, instance)

    @staticmethod
    def _create(window_class, text, style, parent, instance, x, y, width, height, extended=0):
        hwnd = user32.CreateWindowExW(extended, window_class, text, style, x, y, width, height, parent, None, instance, None)
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        return hwnd


@pytest.fixture
def window():
    """``make(...)`` creates a window and returns its handle; every one is closed afterwards."""
    made = []

    def make(controls=CONTROLS, button=STOP, title="Design review", window_class=None, visible=True):
        window_class = window_class or f"GranolaAutostartTestWindow{os.getpid()}x{len(made)}"
        made.append(TestWindow(window_class, controls, button, title, visible).start())
        return made[-1].hwnd

    yield make
    for each in made:
        each.close()


def on_a_thread(function, *args):
    """The result of ``function`` run on a thread of its own, which is where UI Automation is called from."""
    outcome = []

    def run():
        try:
            outcome.append((True, function(*args)))
        except BaseException as error:
            outcome.append((False, error))

    thread = threading.Thread(target=run, name="test-uia")
    thread.start()
    thread.join(30)
    assert outcome, "UI Automation did not answer"
    succeeded, result = outcome[0]
    if not succeeded:
        raise result
    return result


def read_window(hwnd):
    return on_a_thread(check_windows, [hwnd])


def test_a_real_window_being_transcribed_gives_the_title_of_its_note(window):
    assert read_window(window()) == "Design review"


def test_the_per_window_reading_works_on_a_given_handle(window):
    hwnd = window(title="  Weekly sync ")

    def read():
        with granola_window._apartment(), Automation() as automation:
            return window_recording_title(automation, hwnd)

    assert on_a_thread(read) == "Weekly sync"


def test_a_real_window_that_is_hidden_is_checked_without_a_fuss(window):
    # Windows leaves a hidden window's controls out of the UIA tree, so there is usually nothing to read in it.
    assert read_window(window(visible=False)) in (None, "Design review")


@pytest.mark.parametrize(("button", "controls", "title"), [("Resume", CONTROLS, "Design review"), (STOP, "Other controls", "Design review"), (STOP, CONTROLS, "   ")])
def test_a_real_window_that_is_not_transcribing_a_titled_note_gives_none(window, button, controls, title):
    assert read_window(window(controls=controls, button=button, title=title)) is None


def test_the_first_window_that_shows_a_recording_is_the_one_read_and_one_that_cannot_be_read_is_skipped(window, caplog):
    resumed, recording = window(button="Resume"), window(title="Roadmap")
    with caplog.at_level(logging.DEBUG, logger="granola_autostart"):
        assert on_a_thread(check_windows, [0x7FFF0, resumed, recording]) == "Roadmap"
    assert "could not read Granola's window" in caplog.text
    assert on_a_thread(check_windows, [0x7FFF0, resumed]) is None
    assert on_a_thread(check_windows, []) is None


def test_a_window_the_automation_has_no_element_for_is_an_error_the_caller_can_catch():
    def read():
        with granola_window._apartment(), Automation() as automation:
            return automation.from_handle(0x7FFF0)

    with pytest.raises(OSError):
        on_a_thread(read)


def test_an_element_with_no_text_value_has_no_value_and_a_released_element_is_not_released_again(window):
    hwnd = window()

    def read():
        with granola_window._apartment(), Automation() as automation:
            element = automation.from_handle(hwnd)
            group = element.find_first(SCOPE_DESCENDANTS, GROUP, CONTROLS)
            button = group.find_first(SCOPE_DESCENDANTS, BUTTON, STOP)
            seen = (element.value(), button.value())
            for each in (button, group, element):
                each.release()
                each.release()
            return seen

    assert on_a_thread(read) == ("", "")  # what the proxies give for a control with no text


@pytest.mark.parametrize("method", ["from_handle", "control_view_walker", "condition", "_property_condition"])
def test_a_call_that_hands_back_nothing_is_an_error(method, monkeypatch):
    monkeypatch.setattr(granola_window, "_returned", lambda call: None)
    arguments = {"from_handle": (1,), "control_view_walker": (), "condition": (GROUP, CONTROLS), "_property_condition": (GROUP, 1)}

    def read():
        with granola_window._apartment(), Automation() as automation:
            return getattr(automation, method)(*arguments[method])

    with pytest.raises(OSError):
        on_a_thread(read)


def test_a_name_and_type_condition_that_cannot_be_combined_is_an_error(monkeypatch):
    real, calls = granola_window._returned, []

    def returned(call):
        calls.append(1)
        return None if len(calls) == 3 else real(call)  # the third is the one that combines the other two

    monkeypatch.setattr(granola_window, "_returned", returned)

    def read():
        with granola_window._apartment(), Automation() as automation:
            return automation.condition(EDIT, "New note")

    with pytest.raises(OSError):
        on_a_thread(read)


def test_a_condition_on_a_type_alone_and_one_with_a_name_are_both_made_and_released():
    def read():
        with granola_window._apartment(), Automation() as automation:
            with automation.condition(EDIT) as alone, automation.condition(EDIT, "New note") as named:
                return alone.address is not None and named.address is not None and alone.address != named.address

    assert on_a_thread(read)


# The windows of Granola's processes

def test_the_windows_of_a_process_are_the_ones_of_the_electron_class_shown_or_not(window):
    shown = window(window_class="Chrome_WidgetWin_1")
    hidden = window(window_class="Chrome_WidgetWin_1", visible=False)
    other = window()  # another class
    found = real_granola_windows({os.getpid()})
    assert shown in found and hidden in found and other not in found
    assert real_granola_windows(set()) == [] and shown not in real_granola_windows({os.getpid() + 1_000_000})


def test_granola_s_own_processes_are_looked_up_by_name_when_no_pids_are_given(monkeypatch):
    monkeypatch.setattr(granola_window, "pids_named", lambda name: [os.getpid()] if name == "Granola.exe" else [])
    asked = []
    monkeypatch.setattr(granola_window, "window_handles", lambda pids, classes, *, visible_only: asked.append((pids, classes, visible_only)) or [7])
    assert real_granola_windows() == [7]
    assert asked == [({os.getpid()}, frozenset({"Chrome_WidgetWin_1"}), False)]


def test_the_whole_check_finds_a_real_window_through_its_process_and_class(window, monkeypatch):
    hwnd = window(window_class="Chrome_WidgetWin_1")
    window(window_class="Chrome_WidgetWin_1", button="Resume", title="Not this one", visible=False)
    monkeypatch.setattr(granola_window, "granola_windows", lambda: real_granola_windows({os.getpid()}))
    assert hwnd and recording_title() == "Design review"


# The check itself

@pytest.fixture
def check(monkeypatch):
    """recording_title with no real windows: ``windows`` are what Granola has, and ``check_windows`` answers, or runs
    ``behaviour`` instead. Whatever worker a test leaves behind is forgotten."""
    monkeypatch.setattr(granola_window, "_worker", None)
    world = SimpleNamespace(windows=[11, 22], answer="Design review", started=[], behaviour=None)

    def check_windows(windows):
        world.started.append(list(windows))
        if world.behaviour is not None:
            return world.behaviour()
        return world.answer

    monkeypatch.setattr(granola_window, "granola_windows", lambda: world.windows)
    monkeypatch.setattr(granola_window, "check_windows", check_windows)
    return world


def test_the_title_comes_back_from_the_worker_with_the_windows_it_was_given(check):
    assert recording_title() == "Design review"
    assert check.started == [[11, 22]]


def test_each_check_is_on_a_fresh_thread_of_its_own(check):
    threads = []
    check.behaviour = lambda: threads.append(threading.current_thread()) or "x"
    assert recording_title() == "x" and recording_title() == "x"
    assert len(threads) == 2 and threads[0] is not threads[1]
    assert all(thread is not threading.current_thread() and thread.daemon and not thread.is_alive() for thread in threads)


def test_no_windows_means_no_worker_and_no_title(check):
    check.windows = []
    assert recording_title() is None
    assert check.started == []


@pytest.mark.parametrize("answer", [None, ""])
def test_a_worker_that_found_nothing_gives_none(check, answer):
    check.answer = answer
    assert recording_title() is None


def test_a_check_that_breaks_gives_none_and_says_why_only_at_debug_level(check, caplog):
    def boom():
        raise OSError("UI Automation is not available")

    check.behaviour = boom
    with caplog.at_level(logging.DEBUG, logger="granola_autostart"):
        assert recording_title() is None
    assert "could not read Granola's window" in caplog.text
    assert not [record for record in caplog.records if record.levelno > logging.DEBUG]


def test_looking_for_the_windows_that_breaks_gives_none(check, monkeypatch, caplog):
    def boom():
        raise RuntimeError("the process list could not be read")

    monkeypatch.setattr(granola_window, "granola_windows", boom)
    with caplog.at_level(logging.DEBUG, logger="granola_autostart"):
        assert recording_title() is None
    assert "could not check Granola's window" in caplog.text


def test_a_hung_check_times_out_with_one_warning_and_blocks_no_new_one_until_it_ends(check, caplog):
    release = threading.Event()
    check.behaviour = lambda: release.wait(30) and "Design review"
    with caplog.at_level(logging.DEBUG, logger="granola_autostart"):
        assert recording_title(timeout=0.05) is None
        assert recording_title(timeout=0.05) is None  # at once: the one in flight is still stuck
        assert recording_title(timeout=0.05) is None
    assert len(check.started) == 1
    assert len([record for record in caplog.records if record.levelno == logging.WARNING]) == 1
    release.set()
    granola_window._worker.join(10)
    check.behaviour = None
    assert recording_title() == "Design review" and len(check.started) == 2


def test_a_second_timeout_after_a_recovery_warns_again(check, caplog):
    release = threading.Event()
    check.behaviour = lambda: release.wait(30) and "x"
    with caplog.at_level(logging.WARNING, logger="granola_autostart"):
        assert recording_title(timeout=0.01) is None
        release.set()
        granola_window._worker.join(10)
        release.clear()
        assert recording_title(timeout=0.01) is None
        release.set()
    assert len(caplog.records) == 2


def test_a_check_gets_about_three_seconds_by_default():
    assert granola_window.CHECK_TIMEOUT_SECONDS == 3.0
    assert recording_title.__defaults__ == (granola_window.CHECK_TIMEOUT_SECONDS,)
