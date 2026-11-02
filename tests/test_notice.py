import ctypes
import logging
import math
import re
import sys
from types import SimpleNamespace

import pytest

import granola_autostart
import granola_autostart.__main__ as entry
from granola_autostart import notice, processes, settings, watcher
from granola_autostart.notice import keep_display_on as real_keep_display_on
from granola_autostart.notice import show_notice as real_show_notice


@pytest.fixture
def app_log():
    """A logger without handlers, so setup_logging adds its own; they're removed and closed afterwards."""
    logger = logging.getLogger("granola_autostart")
    kept, level = logger.handlers[:], logger.level
    for handler in kept:
        logger.removeHandler(handler)
    yield
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    for handler in kept:
        logger.addHandler(handler)
    logger.setLevel(level)


def broken_card(*_args, **_kwargs):
    raise RuntimeError("no display")


def run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", *argv])
    return entry.main()


def test_a_card_that_fails_exits_2_and_says_why_in_its_own_log(monkeypatch, tmp_path, app_log):
    monkeypatch.setattr(notice, "show_notice", broken_card)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2
    text = (tmp_path / "notice-log.txt").read_text(encoding="utf-8")
    assert "the countdown card failed" in text and "RuntimeError: no display" in text


def test_a_card_that_fails_exits_2_even_when_it_cannot_log(monkeypatch):
    def no_log(_name):
        raise OSError("disk full")

    monkeypatch.setattr(notice, "show_notice", broken_card)
    monkeypatch.setattr(settings, "setup_logging", no_log)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2


def test_a_card_that_cannot_even_load_is_not_a_cancel_and_says_why(monkeypatch, tmp_path, app_log):
    monkeypatch.delattr(granola_autostart, "notice", raising=False)
    monkeypatch.setitem(sys.modules, "granola_autostart.notice", None)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2
    assert "the countdown card failed" in (tmp_path / "notice-log.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(("started", "exit_code"), [(True, 0), (False, 1)])
def test_the_exit_code_tells_the_watcher_whether_to_start_granola(started, exit_code, monkeypatch):
    shown = []
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds, *, kind: shown.append((title, seconds, kind)) or started)
    assert run(monkeypatch, "--notice", "--title=-- standup", "--seconds=7", "--kind=crash") == exit_code
    assert shown == [("-- standup", 7, "crash")]


def test_a_card_holds_the_card_mutex_while_it_is_up_so_an_update_waits_for_it(monkeypatch):
    seen = []

    def card(title, seconds, *, kind):
        seen.append(processes.card_showing())
        return True

    monkeypatch.setattr(notice, "show_notice", card)
    assert not processes.card_showing()
    assert notice.main(["--title=Standup"]) == 0
    assert seen == [True] and not processes.card_showing()


def test_a_card_keeps_the_card_mutex_up_when_the_watcher_or_another_card_that_held_it_lets_go_first(monkeypatch, held_elsewhere):
    seen = []
    watcher_lets_go = held_elsewhere(processes.CARD_MUTEX)  # the watcher holds it while the card starts

    def card(title, seconds, *, kind):
        watcher_lets_go()  # a card that overlapped this one closed, or the watcher's hold ended
        seen.append(processes.card_showing())
        return True

    monkeypatch.setattr(notice, "show_notice", card)
    assert notice.main(["--title=Standup"]) == 0
    assert seen == [True] and not processes.card_showing()


def test_a_card_that_fails_lets_go_of_the_card_mutex(monkeypatch):
    monkeypatch.setattr(notice, "show_notice", broken_card)
    with pytest.raises(RuntimeError):
        notice.main(["--title=Standup"])
    assert not processes.card_showing()


def test_a_watcher_that_crashes_says_why_in_its_log_and_exits_for_the_supervisor(monkeypatch, tmp_path, app_log):
    def broken_tray():
        raise RuntimeError("tray icon failed")

    monkeypatch.setattr(watcher, "main", broken_tray)
    assert run(monkeypatch) == 1
    text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "the watcher stopped" in text and "RuntimeError: tray icon failed" in text


class Font:
    """Every character 7 pixels wide, every line 16 high."""

    def __init__(self, root=None, family=None, size=None):
        self.family = family

    def measure(self, text):
        return 7 * len(text)

    def metrics(self, name):
        return 16


class Canvas:
    """Records what the card draws, and the handlers bound to its buttons."""

    def __init__(self, root, **options):
        self.options = options
        self.items: dict[int, dict] = {}
        self.bindings: dict[str, dict] = {}
        root.canvas = self

    def pack(self):
        pass

    def _add(self, kind, coords, options):
        self.items[len(self.items) + 1] = {"kind": kind, "coords": coords, **options}
        return len(self.items)

    def create_text(self, *coords, **options):
        return self._add("text", coords, options)

    def create_image(self, *coords, **options):
        return self._add("image", coords, options)

    def create_oval(self, *coords, **options):
        return self._add("oval", coords, options)

    def create_line(self, *coords, **options):
        return self._add("line", coords, options)

    def itemconfigure(self, item, **options):
        self.items[item].update(options)

    def configure(self, **options):
        self.options.update(options)

    def tag_bind(self, tag, sequence, handler):
        self.bindings.setdefault(tag, {})[sequence] = handler

    def bbox(self, tag):
        [face] = [item for item in self.items.values() if item["kind"] == "image" and item.get("tags") == tag]
        x, y = face["coords"]
        return x, y, x + face["image"].width, y + face["image"].height


class Root:
    """Tk's window and timer, run by the test's clock instead of an event loop."""

    def __init__(self, card):
        self.card = card
        self.tk = SimpleNamespace(call=lambda *_args: None)
        self.jobs: dict[str, object] = {}
        self.protocols = {}
        self.ended_at = None
        self.destroyed = False
        self.size = None

    def __getattr__(self, name):  # withdraw, overrideredirect, attributes, configure, deiconify, lift, ...
        return lambda *_args, **_kwargs: None

    def geometry(self, spec):
        self.size = spec

    def after(self, _milliseconds, callback):
        job = f"after#{len(self.jobs)}"
        self.jobs[job] = callback
        return job

    def after_cancel(self, job):
        del self.jobs[job]

    def protocol(self, name, callback):
        self.protocols[name] = callback

    def quit(self):
        self.ended_at = self.card.now

    def mainloop(self):
        self.card.user(self.card)
        self.card.wait(3600)  # whatever the user left, the countdown runs out
        assert self.ended_at is not None

    def destroy(self):
        self.destroyed = True


class Card:
    """show_notice on a fake Tk. ``user`` is what the user does while the card shows."""

    def __init__(self):
        self.now = 1000.0
        self.monitor = (0, 0, 1920, 1040, 96)  # the work area and DPI of the monitor under the pointer
        self.theme = "light"
        self.user = lambda card: None
        self.styled = []
        self.root: Root | None = None

    def make_root(self):
        self.root = Root(self)
        return self.root

    @property
    def canvas(self) -> Canvas:
        return self.root.canvas

    def texts(self) -> list[str]:
        return [item["text"] for item in self.canvas.items.values() if item["kind"] == "text"]

    def wait(self, seconds: float) -> None:
        """Let the countdown run, ticking as Tk's timer would, until it ends or the time is up."""
        end = self.now + seconds
        while self.now < end and self.root.ended_at is None:
            self.now = min(end, self.now + 0.25)
            jobs, self.root.jobs = self.root.jobs, {}
            for job in jobs.values():
                job()

    def click(self, tag: str, *, let_go_outside: bool = False) -> None:
        x1, y1, x2, _y2 = self.canvas.bbox(tag)
        handlers = self.canvas.bindings[tag]
        handlers["<ButtonPress-1>"](SimpleNamespace(x=x1 + 2, y=y1 + 2))
        handlers["<ButtonRelease-1>"](SimpleNamespace(x=x2 + 40 if let_go_outside else x1 + 2, y=y1 + 2))

    def close(self) -> None:
        self.root.protocols["WM_DELETE_WINDOW"]()

    def show(self, title: str = "Design review", seconds: int = 10, kind: str = "calendar") -> tuple[bool, float]:
        """The card's answer, and how long after it appeared it closed."""
        shown = self.now
        answer = real_show_notice(title, seconds, kind=kind)
        assert self.root.destroyed
        return answer, self.root.ended_at - shown


@pytest.fixture
def card(monkeypatch) -> Card:
    harness = Card()
    monkeypatch.setattr(notice, "tk", SimpleNamespace(Tk=harness.make_root, Canvas=Canvas))
    monkeypatch.setattr(notice, "tkfont", SimpleNamespace(Font=Font, families=lambda root: ("Segoe UI", "Segoe UI Semibold", "Georgia")))
    monkeypatch.setattr(notice, "ImageTk", SimpleNamespace(PhotoImage=lambda image: image))
    monkeypatch.setattr(notice, "time", SimpleNamespace(monotonic=lambda: harness.now))
    monkeypatch.setattr(notice, "per_monitor_dpi", lambda: None)
    monkeypatch.setattr(notice, "app_theme", lambda: harness.theme)
    monkeypatch.setattr(notice, "_monitor_at_cursor", lambda: harness.monitor)
    monkeypatch.setattr(notice, "_wrapper_hwnd", lambda root: 0x10)
    monkeypatch.setattr(notice, "_style_card", lambda hwnd, *, border: harness.styled.append((hwnd, border)))
    return harness


def test_the_countdown_running_out_starts_granola(card):
    statuses = []

    def user(card):
        statuses.append([text for text in card.texts() if "Granola" in text][-1])
        card.wait(9.25)
        statuses.append([text for text in card.texts() if "Granola" in text][-1])

    card.user = user
    assert card.show(seconds=10) == (True, 10)
    assert statuses == ["Granola opens when the timer runs out.", "Opening Granola…"]
    assert "GRANOLA  ·  STARTING NOTES" in card.texts()


def test_cancel_and_the_close_button_both_say_no_at_once(card):
    card.user = lambda card: card.click("cancel")
    assert card.show() == (False, 0)
    card.user = lambda card: (card.close(), card.close())
    assert card.show() == (False, 0)


def test_start_now_starts_granola_but_not_a_click_already_on_its_way(card):
    def user(card):
        card.click("start")  # the moment the card appeared
        assert card.root.ended_at is None
        card.wait(1)
        x1, y1, _x2, _y2 = card.canvas.bbox("start")
        card.canvas.bindings["start"]["<ButtonRelease-1>"](SimpleNamespace(x=x1 + 2, y=y1 + 2))  # pressed elsewhere
        assert card.root.ended_at is None
        card.click("start")

    card.user = user
    assert card.show() == (True, 1)


def test_letting_go_outside_start_now_starts_nothing(card):
    def user(card):
        card.wait(1)
        card.click("start", let_go_outside=True)
        assert card.root.ended_at is None
        card.click("cancel")

    card.user = user
    assert card.show() == (False, 1)


def test_the_buttons_light_up_under_the_pointer(card):
    def user(card):
        start = card.canvas.bindings["start"]
        start["<Enter>"](None)
        assert card.canvas.options["cursor"] == "hand2"
        start["<Leave>"](None)
        assert card.canvas.options["cursor"] == ""

    card.user = user
    card.show()


def test_a_card_for_granola_closing_says_so_instead_of_counting_down_to_open(card):
    card.user = lambda card: card.wait(9.5)
    card.show(kind="crash")
    texts = card.texts()
    assert "GRANOLA  ·  RESTARTING NOTES" in texts
    assert "Granola closed. Starting again unless you cancel." in " ".join(texts)
    assert not any(text.startswith(("Opening Granola", "Granola opens")) for text in texts)


def test_a_long_title_takes_two_lines_and_an_ellipsis(card):
    card.show(title="Quarterly\nplanning\t" + "review " * 40)
    title_lines = [item["text"] for item in card.canvas.items.values() if item.get("font") and item["font"].family == "Georgia"]
    assert len(title_lines) == 2 and title_lines[0].startswith("Quarterly planning review")
    assert title_lines[1].endswith("…")
    card.show(title=" \n ")
    assert "Meeting" in card.texts()


def test_the_card_sits_in_the_top_right_corner_of_the_pointer_s_screen(card):
    card.monitor = (1920, 0, 3840, 1040, 144)  # a second display, at 150%
    card.show()
    width, x, y = (int(part) for part in re.fullmatch(r"(\d+)x\d+\+(\d+)\+(\d+)", card.root.size).groups())
    assert (width, x, y) == (606, 3840 - 606 - 36, 36)
    assert card.styled == [(0x10, notice.PALETTES["light"]["border"])]


def test_a_card_too_tall_for_the_screen_keeps_its_top_on_screen(card):
    card.monitor = (0, 100, 800, 250, 96)
    card.theme = "dark"
    card.show()
    height, y = (int(part) for part in re.fullmatch(r"\d+x(\d+)\+\d+\+(\d+)", card.root.size).groups())
    assert height > 150 and y == 100
    assert card.canvas.options["bg"] == notice.PALETTES["dark"]["bg"]


def test_words_wrap_and_a_word_wider_than_the_line_is_broken():
    font = Font()  # 7 pixels a character, so 105 pixels hold 15
    assert notice._fit_lines(font, "Weekly product sync", 105, 2) == ["Weekly product", "sync"]
    assert notice._fit_lines(font, "Supercalifragilistic", 70, 3) == ["Supercalif", "ragilistic"]
    assert notice._fit_lines(font, "", 105, 2) == [""]


def test_text_past_the_last_line_ends_in_an_ellipsis_without_trailing_punctuation():
    assert notice._fit_lines(Font(), "Roadmap, Q4 planning", 70, 1) == ["Roadmap…"]


def test_the_ring_shows_the_time_left_ending_at_twelve_o_clock(monkeypatch):
    monkeypatch.setattr(notice, "ImageTk", SimpleNamespace(PhotoImage=lambda image: image))

    def colour(image, angle):
        """The ring's colour at ``angle`` degrees clockwise from three o'clock, in the middle of its stroke."""
        x = 50 + 44 * math.cos(math.radians(angle))
        y = 50 + 44 * math.sin(math.radians(angle))
        red, _green, blue, _alpha = image.getpixel((round(x), round(y)))
        return "accent" if red > blue else "track"

    quarter = notice._ring_image(100, 12, 0.25, "#0000ff", "#ff0000")
    assert [colour(quarter, angle) for angle in (225, 315, 45, 135)] == ["accent", "track", "track", "track"]
    empty = notice._ring_image(100, 12, 0, "#0000ff", "#ff0000")
    assert {colour(empty, angle) for angle in range(0, 360, 30)} == {"track"}


def test_fonts_fall_back_to_segoe_ui(monkeypatch):
    monkeypatch.setattr(notice, "tkfont", SimpleNamespace(families=lambda root: ("Arial", "Cambria")))
    assert notice._family(None, "Georgia", "Cambria") == "Cambria"
    assert notice._family(None, "Segoe UI Variable Text") == "Segoe UI"


def fake_ctypes(monkeypatch, **dlls) -> None:
    """ctypes with only the DLLs given, so a call the test leaves out is missing, as on an older Windows."""
    kept = {name: getattr(ctypes, name) for name in ("POINTER", "byref", "sizeof", "c_int", "c_ssize_t", "c_size_t", "HRESULT")}
    monkeypatch.setattr(notice, "ctypes", SimpleNamespace(windll=SimpleNamespace(**dlls), **kept))


def monitor_user32(work: tuple[int, int, int, int] | None) -> SimpleNamespace:
    """The pointer's monitor has ``work`` as its work area; None when Windows won't say."""

    def monitor_info(monitor, info):
        if work is None:
            return False
        area = info._obj.rcWork  # byref's target
        area.left, area.top, area.right, area.bottom = work
        return True

    return SimpleNamespace(
        GetCursorPos=lambda point: True,
        MonitorFromPoint=lambda point, flags: 0x99,
        GetMonitorInfoW=monitor_info,
        GetSystemMetrics=lambda index: (2560, 1440)[index],
    )


def test_the_card_measures_the_monitor_under_the_pointer(monkeypatch):
    def dpi(monitor, kind, dpi_x, dpi_y):
        dpi_y._obj.value = 144

    fake_ctypes(monkeypatch, user32=monitor_user32((1920, 0, 3840, 1040)), shcore=SimpleNamespace(GetDpiForMonitor=dpi))
    assert notice._monitor_at_cursor() == (1920, 0, 3840, 1040, 144)


def test_without_per_monitor_dpi_or_monitor_details_the_card_still_finds_a_place(monkeypatch):
    fake_ctypes(monkeypatch, user32=monitor_user32((0, 0, 1920, 1040)))  # Windows 8.0 has no shcore
    assert notice._monitor_at_cursor() == (0, 0, 1920, 1040, 96)
    fake_ctypes(monkeypatch, user32=monitor_user32(None))
    assert notice._monitor_at_cursor() == (0, 0, 2560, 1440, 96)


def test_the_card_is_styled_through_tk_s_wrapper_window(monkeypatch):
    root = SimpleNamespace(winfo_id=lambda: 0x10)
    fake_ctypes(monkeypatch, user32=SimpleNamespace(GetParent=lambda hwnd: 0x20))
    assert notice._wrapper_hwnd(root) == 0x20
    fake_ctypes(monkeypatch, user32=SimpleNamespace(GetParent=lambda hwnd: None))
    assert notice._wrapper_hwnd(root) == 0x10


def test_the_card_never_takes_focus_and_has_a_shadow(monkeypatch):
    styles = {("window", -20): 0x100, ("class", -26): 0x8}
    attributes = []

    def setter(kind):
        return lambda hwnd, index, value: styles.update({(kind, index): value})

    fake_ctypes(
        monkeypatch,
        user32=SimpleNamespace(
            GetWindowLongPtrW=lambda hwnd, index: styles[("window", index)],
            SetWindowLongPtrW=setter("window"),
            GetClassLongPtrW=lambda hwnd, index: styles[("class", index)],
            SetClassLongPtrW=setter("class"),
        ),
    )
    monkeypatch.setattr(notice, "set_window_attribute", lambda hwnd, attribute, value: attributes.append((attribute, value)))
    notice._style_card(0x20, border="#e2d9c6")
    no_activate, tool_window, drop_shadow = 0x08000000, 0x80, 0x00020000
    assert styles == {("window", -20): 0x100 | no_activate | tool_window, ("class", -26): 0x8 | drop_shadow}
    assert attributes == [(33, 2), (34, "#e2d9c6")]  # rounded corners and the border colour


def test_windows_10_ignoring_rounded_corners_is_fine(monkeypatch):
    def unsupported(*_args):
        raise OSError("DwmSetWindowAttribute: invalid parameter")

    user32 = SimpleNamespace(
        GetWindowLongPtrW=lambda *_args: 0, SetWindowLongPtrW=lambda *_args: 0, GetClassLongPtrW=lambda *_args: 0, SetClassLongPtrW=lambda *_args: 0
    )
    fake_ctypes(monkeypatch, user32=user32)
    monkeypatch.setattr(notice, "set_window_attribute", unsupported)
    notice._style_card(0x20, border="#e2d9c6")


def test_the_display_stays_on_while_a_card_counts_down(monkeypatch):
    states = []
    fake_ctypes(monkeypatch, kernel32=SimpleNamespace(SetThreadExecutionState=states.append))
    real_keep_display_on()
    assert states == [notice.ES_CONTINUOUS | notice.ES_DISPLAY_REQUIRED]
    fake_ctypes(monkeypatch)  # the call is missing: the card still shows
    real_keep_display_on()
