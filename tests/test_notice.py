import ctypes
import json
import logging
import math
import re
import sys
from types import SimpleNamespace

import pytest

import granola_autostart
import granola_autostart.__main__ as entry
from granola_autostart import decide, notice, processes, settings, watcher
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


@pytest.mark.parametrize(("picked", "exit_code"), [(0, 0), (None, 1), (1, 11), (3, 13)])
def test_the_exit_code_tells_the_watcher_whether_to_start_granola_and_which_meeting(picked, exit_code, monkeypatch):
    shown = []
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds, *, kind, choices: shown.append((title, seconds, kind, choices)) or picked)
    assert run(monkeypatch, "--notice", "--title=-- standup", "--seconds=7", "--kind=crash") == exit_code
    assert shown == [("-- standup", 7, "crash", [])]


def test_the_card_is_given_the_meetings_it_lists_and_a_malformed_list_is_none(monkeypatch):
    shown = []
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds, *, kind, choices: shown.append(choices) or 0)
    listed = json.dumps([{"title": "Stand-up", "line": "09:00"}, {"title": "Reminder"}])
    assert run(monkeypatch, "--notice", "--title=Stand-up", f"--choices={listed}") == 0
    assert run(monkeypatch, "--notice", "--title=Stand-up", "--choices={not json") == 0
    assert shown == [[("Stand-up", "09:00"), ("Reminder", "")], []]


def test_the_exit_codes_are_the_ones_the_module_documents():
    assert processes.CHOICE_EXIT_CODE == 10 and "10 + i" in notice.__doc__
    assert notice.MAX_CHOICES == decide.MAX_CHOICES


def test_a_card_holds_the_card_mutex_while_it_is_up_so_an_update_waits_for_it(monkeypatch):
    seen = []

    def card(title, seconds, *, kind, choices):
        seen.append(processes.card_showing())
        return 0

    monkeypatch.setattr(notice, "show_notice", card)
    assert not processes.card_showing()
    assert notice.main(["--title=Standup"]) == 0
    assert seen == [True] and not processes.card_showing()


def test_a_card_keeps_the_card_mutex_up_when_the_watcher_or_another_card_that_held_it_lets_go_first(monkeypatch, held_elsewhere):
    seen = []
    watcher_lets_go = held_elsewhere(processes.CARD_MUTEX)  # the watcher holds it while the card starts

    def card(title, seconds, *, kind, choices):
        watcher_lets_go()  # a card that overlapped this one closed, or the watcher's hold ended
        seen.append(processes.card_showing())
        return 0

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
        face = next(item for item in self.items.values() if item["kind"] == "image" and item.get("tags") == tag)  # a row's first
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
        self.monitor = (0, 0, 1920, 1040, 96)  # the work area and DPI of the primary monitor
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

    def show(self, title: str = "Design review", seconds: int = 10, kind: str = "calendar", choices=()) -> tuple[int | None, float]:
        """The card's answer (the meeting to start, or None for Cancel), and how long after it appeared it closed."""
        shown = self.now
        answer = real_show_notice(title, seconds, kind=kind, choices=choices)
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
    monkeypatch.setattr(notice, "_primary_monitor", lambda: harness.monitor)
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
    assert card.show(seconds=10) == (0, 10)
    assert statuses == ["Granola opens when the timer runs out.", "Opening Granola…"]
    assert "GRANOLA  ·  STARTING NOTES" in card.texts()


def test_cancel_and_the_close_button_both_say_no_at_once(card):
    card.user = lambda card: card.click("cancel")
    assert card.show() == (None, 0)
    card.user = lambda card: (card.close(), card.close())
    assert card.show() == (None, 0)


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
    assert card.show() == (0, 1)


def test_letting_go_outside_start_now_starts_nothing(card):
    def user(card):
        card.wait(1)
        card.click("start", let_go_outside=True)
        assert card.root.ended_at is None
        card.click("cancel")

    card.user = user
    assert card.show() == (None, 1)


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


def test_the_card_sits_in_the_top_right_corner_of_the_primary_monitor(card):
    card.monitor = (0, 0, 2560, 1400, 144)  # the primary monitor, at 150%
    card.show()
    width, x, y = (int(part) for part in re.fullmatch(r"(\d+)x\d+\+(\d+)\+(\d+)", card.root.size).groups())
    assert (width, x, y) == (606, 2560 - 606 - 36, 36)
    assert card.styled == [(0x10, notice.PALETTES["light"]["border"])]


def test_a_card_too_tall_for_the_screen_keeps_its_top_on_screen(card):
    card.monitor = (0, 100, 800, 250, 96)
    card.theme = "dark"
    card.show()
    height, y = (int(part) for part in re.fullmatch(r"\d+x(\d+)\+\d+\+(\d+)", card.root.size).groups())
    assert height > 150 and y == 100
    assert card.canvas.options["bg"] == notice.PALETTES["dark"]["bg"]


STANDUP = ("Chromium Team: Stand-Up", "09:00\u201309:15 \u00b7 7 guests")
REMINDER = ("Reload 2Captcha Credits", "09:00\u201309:15 \u00b7 No guests")
ACCENT_SOFT = (0xE8, 0xEF, 0xDF, 255)
BUTTON_HOVER = (0xF1, 0xEA, 0xDB, 255)


def row_images(card, index):
    """A row's background and its radio button."""
    return [item for item in card.canvas.items.values() if item["kind"] == "image" and item.get("tags") == f"row{index}"]


def middle(item):
    image = item["image"]
    return image.getpixel((image.width // 2, image.height // 2))


def ticked_rows(card):
    rows = sorted(tag for tag in card.canvas.bindings if tag.startswith("row"))
    return [middle(row_images(card, index)[0]) == ACCENT_SOFT for index in range(len(rows))]


def row_press(card, index, *, wait=0):
    card.wait(wait)
    card.click(f"row{index}")


def test_a_card_for_meetings_starting_together_lists_them_with_the_first_ticked(card):
    calm = []
    card.user = lambda card: calm.append(" ".join(" ".join(text.split()) for text in card.texts()))
    card.show(choices=[STANDUP, REMINDER])
    texts = card.texts()
    assert "GRANOLA  \u00b7  STARTING NOTES" in texts
    assert [text for text in texts if text in STANDUP + REMINDER] == [*STANDUP, *REMINDER]
    assert "Design review" not in texts  # the rows take the title's place
    assert "Granola opens the ticked meeting when the timer runs out." in calm[0]
    assert ticked_rows(card) == [True, False]
    on, off = (row_images(card, index)[1] for index in (0, 1))
    assert middle(on) == (0x5A, 0x8A, 0x47, 255)  # a ring in the accent colour with a dot in it
    assert middle(off)[3] == 0 and off["image"].getpixel((0, off["image"].height // 2))[3] > 100  # a ring and nothing in it
    assert middle(row_images(card, 1)[0])[3] == 0  # no background until it is ticked or pointed at


def test_the_card_lists_at_most_four_meetings_and_a_single_one_or_a_crash_card_none(card):
    card.show(choices=[(f"Meeting {number}", "") for number in range(6)])
    assert [tag for tag in card.canvas.bindings if tag.startswith("row")] == ["row0", "row1", "row2", "row3"]
    for choices, kind in (([STANDUP], "calendar"), ([STANDUP, REMINDER], "crash"), ([], "calendar")):
        card.show(title="Design review", kind=kind, choices=choices)
        assert not any(tag.startswith("row") for tag in card.canvas.bindings)
        assert [item["font"].family for item in card.canvas.items.values() if item.get("text") == "Design review"] == ["Georgia"]
        if kind == "crash":
            assert "Granola closed. Starting again unless you cancel." in " ".join(card.texts())


def test_clicking_a_meeting_ticks_it_and_the_countdown_carries_on_to_start_it(card):
    def user(card):
        row_press(card, 1, wait=1)
        assert ticked_rows(card) == [False, True] and card.root.ended_at is None
        assert middle(row_images(card, 1)[1]) == (0x5A, 0x8A, 0x47, 255) and middle(row_images(card, 0)[1])[3] == 0
        row_press(card, 0, wait=1)  # and back
        assert ticked_rows(card) == [True, False]
        row_press(card, 1, wait=1)

    card.user = user
    assert card.show(choices=[STANDUP, REMINDER]) == (1, 10)  # the timer was never restarted


def test_start_now_starts_the_ticked_meeting_and_cancel_starts_none(card):
    card.user = lambda card: (row_press(card, 1, wait=1), card.click("start"))
    assert card.show(choices=[STANDUP, REMINDER]) == (1, 1)
    card.user = lambda card: (row_press(card, 1, wait=1), card.click("cancel"))
    assert card.show(choices=[STANDUP, REMINDER]) == (None, 1)


def test_a_click_already_on_its_way_does_not_change_the_pick(card):
    def user(card):
        card.click("row1")  # the moment the card appeared
        assert ticked_rows(card) == [True, False]
        card.wait(1)
        x1, y1, _x2, _y2 = card.canvas.bbox("row1")
        card.canvas.bindings["row1"]["<ButtonRelease-1>"](SimpleNamespace(x=x1 + 2, y=y1 + 2))  # pressed elsewhere
        assert ticked_rows(card) == [True, False]
        card.click("row1", let_go_outside=True)
        assert ticked_rows(card) == [True, False]
        card.click("row1")
        assert ticked_rows(card) == [False, True]

    card.user = user
    assert card.show(choices=[STANDUP, REMINDER])[0] == 1


def test_the_rows_light_up_under_the_pointer_unless_they_are_ticked(card):
    def user(card):
        second = card.canvas.bindings["row1"]
        second["<Enter>"](None)
        assert middle(row_images(card, 1)[0]) == BUTTON_HOVER and card.canvas.options["cursor"] == "hand2"
        second["<Leave>"](None)
        assert middle(row_images(card, 1)[0])[3] == 0 and card.canvas.options["cursor"] == ""
        card.canvas.bindings["row0"]["<Enter>"](None)
        assert ticked_rows(card) == [True, False]  # the ticked one keeps its own colour

    card.user = user
    card.show(choices=[STANDUP, REMINDER])


@pytest.mark.parametrize(("dpi", "height"), [(96, 40), (144, 60)])
def test_a_row_is_at_least_40_pixels_tall_scaled_for_the_screen(dpi, height, card, monkeypatch):
    class Small(Font):
        def metrics(self, name):
            return 4

    monkeypatch.setattr(notice, "tkfont", SimpleNamespace(Font=Small, families=lambda root: ("Segoe UI", "Georgia")))
    card.monitor = (0, 0, 1920, 1040, dpi)
    card.show(choices=[STANDUP, REMINDER])
    assert [row_images(card, index)[0]["image"].height for index in (0, 1)] == [height, height]


def test_the_card_grows_with_its_rows_and_stays_in_the_work_area(card):
    def size():
        return [int(part) for part in re.fullmatch(r"(\d+)x(\d+)\+(\d+)\+(\d+)", card.root.size).groups()]

    card.show(choices=[STANDUP, REMINDER])
    two = size()
    card.show(choices=[STANDUP, REMINDER, STANDUP, REMINDER])
    four = size()
    assert four[1] - two[1] == 2 * 52  # two more rows of 50 pixels, and the gap between rows
    card.monitor = (0, 0, 1920, 380, 96)  # too short for it at the usual place, but tall enough for all of it
    card.show(choices=[STANDUP, REMINDER, STANDUP, REMINDER])
    _width, height, _x, y = size()
    assert y < 24 and y + height <= 380


def test_a_long_title_or_line_stays_on_one_row_line_and_ends_in_an_ellipsis(card):
    card.show(choices=[("Quarterly planning review " * 5, "09:00\u201309:15 \u00b7 " + "a long calendar name " * 6), ("Standup\n\t ", " ")])
    rows = [item for item in card.canvas.items.values() if item["kind"] == "text" and item.get("tags") in ("row0", "row1")]
    first_title, first_line, second_title, second_line = (item["text"] for item in rows)
    assert first_title.endswith("\u2026") and first_line.endswith("\u2026") and "\n" not in first_title + first_line
    assert (second_title, second_line) == ("Standup", "")
    card.show(choices=[(" \n ", "x"), ("b", "y")])
    assert "Meeting" in card.texts()


def test_the_card_for_meetings_says_opening_granola_for_the_last_second(card):
    statuses = []

    def user(card):
        statuses.append([text for text in card.texts() if "Granola" in text and "GRANOLA" not in text][-1])
        card.wait(9.25)
        statuses.append([text for text in card.texts() if "Granola" in text and "GRANOLA" not in text][-1])

    card.user = user
    card.show(choices=[STANDUP, REMINDER])
    assert [" ".join(text.split()) for text in statuses] == ["Granola opens the ticked meeting when the timer runs out.", "Opening Granola\u2026"]


def test_the_palettes_have_the_ticked_row_s_colour():
    assert (notice.PALETTES["light"]["accent_soft"], notice.PALETTES["dark"]["accent_soft"]) == ("#e8efdf", "#2c3627")


@pytest.mark.parametrize(
    "text",
    ["", "{not json", '{"title": "Stand-up"}', '"Stand-up"', "[1]", '[{"line": "09:00"}]', '[{"title": 1}]', '[{"title": "a", "line": 2}]', '[{"title": "a"}, null]'],
)
def test_choices_that_are_missing_or_malformed_are_none_at_all(text):
    assert notice.parse_choices(text) == []


def test_choices_are_titles_and_lines_and_a_line_may_be_left_out():
    assert notice.parse_choices('[{"title": "Stand-up", "line": "09:00"}, {"title": "Reminder"}]') == [("Stand-up", "09:00"), ("Reminder", "")]
    assert notice.parse_choices("[]") == []


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
    """The primary monitor has ``work`` as its work area; None when Windows won't say. ``lookups`` has each
    monitor asked for, as ((x, y), flags)."""

    def monitor_from_point(point, flags):
        user32.lookups.append(((point.x, point.y), flags))
        return 0x99

    def monitor_info(monitor, info):
        if work is None:
            return False
        area = info._obj.rcWork  # byref's target
        area.left, area.top, area.right, area.bottom = work
        return True

    user32 = SimpleNamespace(
        lookups=[],
        MonitorFromPoint=monitor_from_point,
        GetMonitorInfoW=monitor_info,
        GetSystemMetrics=lambda index: (2560, 1440)[index],
    )
    return user32


def test_the_card_measures_the_primary_monitor(monkeypatch):
    def dpi(monitor, kind, dpi_x, dpi_y):
        dpi_y._obj.value = 144

    user32 = monitor_user32((0, 0, 2560, 1400))
    fake_ctypes(monkeypatch, user32=user32, shcore=SimpleNamespace(GetDpiForMonitor=dpi))
    assert notice._primary_monitor() == (0, 0, 2560, 1400, 144)
    assert user32.lookups == [((0, 0), 1)]  # (0, 0), with MONITOR_DEFAULTTOPRIMARY


def test_without_per_monitor_dpi_or_monitor_details_the_card_still_finds_a_place(monkeypatch):
    fake_ctypes(monkeypatch, user32=monitor_user32((0, 0, 1920, 1040)))  # Windows 8.0 has no shcore
    assert notice._primary_monitor() == (0, 0, 1920, 1040, 96)
    fake_ctypes(monkeypatch, user32=monitor_user32(None))
    assert notice._primary_monitor() == (0, 0, 2560, 1440, 96)


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
