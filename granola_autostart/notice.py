"""Countdown card. It exits 0 when the countdown runs out or Start now is clicked, and the watcher then
starts Granola; Cancel exits 1. A card that fails exits 2 after logging to notice-log.txt, and the
watcher starts notes anyway, except when it was asking to restart them after Granola closed.

When meetings start together the card lists them (--choices, a JSON list of {"title", "line"}), the first one
ticked, and a click on another ticks it. Starting then means the ticked meeting: the exit code is 0 for the first
and 10 + i for the one at place i (processes.CHOICE_EXIT_CODE). Cancel is still 1, whichever is ticked.

The card never takes keyboard focus, so typing in the current app is not interrupted. Pressing it anywhere but a button
or a meeting and dragging moves it out of the way. That answers nothing, and the countdown carries on.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import ntpath
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Sequence
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageTk

from . import processes
from .winui import app_theme, per_monitor_dpi, set_window_attribute

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
MAX_TITLE = 200
MAX_CHOICES = 4  # the most meetings the card lists (the same number as decide.MAX_CHOICES, which can't be imported here)
RING_STEPS = 120
# A click already on its way to that corner when the card appears must not skip the countdown.
START_NOW_DELAY_SECONDS = 0.75
# How often the card looks again for Granola's own meeting popup, which can appear after the card does.
POPUP_CHECK_MS = 500
# The pointer has to move this far from a press on the card's background before the card follows it, so a click never
# moves it. Scaled like the rest of the card.
DRAG_THRESHOLD = 4
# How much of a dragged card stays inside the virtual screen (all the monitors together), so it can't be lost off an edge.
# Scaled too.
KEEP_ON_SCREEN = 40
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
DWMWA_CLOAKED = 14
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GRANOLA_IMAGE = "granola.exe"  # not GranolaAutostart.exe, which is this app

PALETTES = {
    # The ring keeps "accent"; text and the Start button use darker greens so they reach 4.5:1.
    "light": {
        "bg": "#fbf8f1",
        "border": "#e2d9c6",
        "ink": "#2b2924",
        "muted": "#6f685b",
        "accent": "#5a8a47",
        "accent_text": "#42703a",
        "start": "#4b7a3a",
        "start_hover": "#3f6830",
        "on_accent": "#ffffff",
        "track": "#e9e2d2",
        "button_line": "#2b2924",
        "button_hover": "#f1eadb",
        "accent_soft": "#e8efdf",
        "warn": "#94501a",
    },
    "dark": {
        "bg": "#25231f",
        "border": "#3d3a33",
        "ink": "#f1ece1",
        "muted": "#b3ab9b",
        "accent": "#93c47d",
        "accent_text": "#a7d493",
        "start": "#93c47d",
        "start_hover": "#a7d493",
        "on_accent": "#1c2617",
        "track": "#3a3731",
        "button_line": "#f1ece1",
        "button_hover": "#33302a",
        "accent_soft": "#2c3627",
        "warn": "#e9a15c",
    },
}


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG), ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", _RECT),
        ("rcWork", _RECT),
        ("dwFlags", wintypes.DWORD),
    ]


def _primary_monitor() -> tuple[int, int, int, int, int]:
    """Work area of the primary monitor, which Windows calls the main display, plus that monitor's DPI."""
    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    # The primary monitor always contains (0, 0).
    monitor = user32.MonitorFromPoint(wintypes.POINT(0, 0), 1)  # MONITOR_DEFAULTTOPRIMARY
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(_MONITORINFO)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), 96
    dpi_x = wintypes.UINT(96)
    dpi_y = wintypes.UINT(96)
    try:
        ctypes.windll.shcore.GetDpiForMonitor.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.POINTER(wintypes.UINT),
            ctypes.POINTER(wintypes.UINT),
        ]
        ctypes.windll.shcore.GetDpiForMonitor.restype = ctypes.HRESULT
        ctypes.windll.shcore.GetDpiForMonitor(monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y))
    except Exception:
        pass
    work = info.rcWork
    return work.left, work.top, work.right, work.bottom, int(dpi_y.value or 96)


def _virtual_screen() -> tuple[int, int, int, int] | None:
    """The (left, top, right, bottom) of all the monitors together, which reaches into negative coordinates when one sits
    left of or above the primary. None when Windows won't say."""
    try:
        metrics = ctypes.windll.user32.GetSystemMetrics
        left, top, width, height = (metrics(index) for index in (SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN))
    except Exception:
        return None
    return (left, top, left + width, top + height) if width > 0 and height > 0 else None


def _granola_windows() -> list[tuple[int, int, int, int]]:
    """The (left, top, right, bottom), in physical pixels, of Granola's visible windows that stay on top, which its meeting
    popup is. [] when Windows won't say, because the card shows regardless."""
    try:
        user32, kernel32, dwmapi = ctypes.windll.user32, ctypes.windll.kernel32, ctypes.windll.dwmapi
        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows.argtypes = [enum_proc, wintypes.LPARAM]
        user32.EnumWindows.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        dwmapi.DwmGetWindowAttribute.restype = wintypes.LONG  # an HRESULT: 0 is success
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        # The callback only collects, so nothing in it can raise: the checks run out here, where a failure gives [].
        hwnds: list[int] = []

        def collect(hwnd, _lparam):
            hwnds.append(hwnd)
            return True

        callback = enum_proc(collect)  # kept until EnumWindows is done with it
        user32.EnumWindows(callback, 0)

        is_granola: dict[int, bool] = {}  # by process id, so each is opened once

        def granola_process(pid: int) -> bool:
            if pid not in is_granola:
                is_granola[pid] = False
                handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
                if handle:
                    try:
                        size = wintypes.DWORD(1024)
                        path = ctypes.create_unicode_buffer(size.value)
                        if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                            is_granola[pid] = ntpath.basename(path.value).casefold() == GRANOLA_IMAGE
                    finally:
                        kernel32.CloseHandle(handle)
            return is_granola[pid]

        found = []
        for hwnd in hwnds:
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                continue
            if not user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
                continue
            cloaked = wintypes.DWORD(0)  # stays 0 if the call fails, which counts as not cloaked
            if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(cloaked), ctypes.sizeof(cloaked)) == 0 and cloaked.value:
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            rect = _RECT()
            if granola_process(pid.value) and user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                found.append((rect.left, rect.top, rect.right, rect.bottom))
        return found
    except Exception:
        return []


def _clear_of(x: int, y: int, width: int, height: int, windows: Sequence[tuple[int, int, int, int]], bottom: int) -> int:
    """The top for a width x height card at x that overlaps none of ``windows`` (touching is not overlapping): y when
    nothing is in its way, else just below the lowest window that is, and then below any in the way there. Where that
    would end past ``bottom``, the work area's, y is kept and the card shows over them.

    There is no gap under a window. Granola's popup window has a transparent margin of its own, about 27 px at 100%
    below the popup, which is as much as the card's 24 px margin from the top of the screen.
    """
    top = y
    while True:
        in_the_way = [
            w_bottom
            for w_left, w_top, w_right, w_bottom in windows
            if min(w_right, x + width) > max(w_left, x) and min(w_bottom, top + height) > max(w_top, top)
        ]
        if not in_the_way:
            break
        top = max(in_the_way)  # always lower than before, so this ends
    return top if top + height <= bottom else y


def _keep_on_screen(x: int, y: int, width: int, height: int, screen: tuple[int, int, int, int], visible: int) -> tuple[int, int]:
    """x and y, moved the least that leaves ``visible`` pixels of a width x height card inside ``screen``, across and down."""
    left, top, right, bottom = screen
    return min(max(x, left + visible - width), right - visible), min(max(y, top + visible - height), bottom - visible)


def _wrapper_hwnd(root: tk.Tk) -> int:
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    return user32.GetParent(root.winfo_id()) or root.winfo_id()


def _style_card(hwnd: int, *, border: str) -> None:
    """No-activate tool window with a drop shadow and Windows 11 rounded corners (ignored on Windows 10)."""
    user32 = ctypes.windll.user32
    user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    ws_ex_noactivate, ws_ex_toolwindow = 0x08000000, 0x00000080
    user32.SetWindowLongPtrW(hwnd, -20, user32.GetWindowLongPtrW(hwnd, -20) | ws_ex_noactivate | ws_ex_toolwindow)
    user32.GetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetClassLongPtrW.restype = ctypes.c_size_t
    user32.SetClassLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_size_t]
    user32.SetClassLongPtrW(hwnd, -26, user32.GetClassLongPtrW(hwnd, -26) | 0x00020000)
    try:
        set_window_attribute(hwnd, 33, 2)  # DWMWA_WINDOW_CORNER_PREFERENCE: round
        set_window_attribute(hwnd, 34, border)  # DWMWA_BORDER_COLOR
    except Exception:
        pass


def _family(root: tk.Tk, *names: str) -> str:
    available = set(tkfont.families(root))
    return next((name for name in names if name in available), "Segoe UI")


def _fit_lines(font: tkfont.Font, text: str, width: int, max_lines: int) -> list[str]:
    """Word-wrap to at most max_lines, ending with an ellipsis when the text does not fit."""
    lines: list[str] = []
    current = ""
    for word in text.split():
        trial = f"{current} {word}" if current else word
        if font.measure(trial) <= width:
            current = trial
            continue
        if current:
            lines.append(current)
        current = word
        while font.measure(current) > width and len(current) > 1:
            cut = len(current) - 1
            while cut > 1 and font.measure(current[:cut]) > width:
                cut -= 1
            lines.append(current[:cut])
            current = current[cut:]
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines or [""]
    last = " ".join(lines[max_lines - 1 :])
    while last and font.measure(last.rstrip(" ,.;:-") + "\u2026") > width:
        last = last[:-1]
    return lines[: max_lines - 1] + [last.rstrip(" ,.;:-") + "\u2026"]


def _pill_image(
    width: int, height: int, fill: str | None, outline: str | None = None, line: float = 0, radius: float | None = None
) -> ImageTk.PhotoImage:
    """A rounded rectangle, a pill unless ``radius`` says how round its corners are."""
    ss = 4
    img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = line * ss / 2
    draw.rounded_rectangle(
        (inset, inset, width * ss - 1 - inset, height * ss - 1 - inset),
        radius=(height / 2 if radius is None else radius) * ss,
        fill=fill,
        outline=outline,
        width=round(line * ss) if outline else 0,
    )
    return ImageTk.PhotoImage(img.resize((width, height), Image.LANCZOS))


def _radio_image(size: int, stroke: float, ring: str, dot: str | None) -> ImageTk.PhotoImage:
    """A radio button: a ring, with a dot in the middle when it is ticked."""
    ss = 4
    s = size * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((0, 0, s - 1, s - 1), outline=ring, width=round(stroke * ss))
    if dot:
        draw.ellipse((s / 4, s / 4, s - 1 - s / 4, s - 1 - s / 4), fill=dot)
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


def _ring_image(size: int, stroke: float, fraction: float, track: str, accent: str) -> ImageTk.PhotoImage:
    """Progress ring with round caps; the remaining arc ends at 12 o'clock and shrinks clockwise."""
    ss = 2
    s = size * ss
    w = stroke * ss
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    box = (0, 0, s - 1, s - 1)
    draw.ellipse(box, outline=track, width=round(w))
    if fraction > 0:
        start, end = 270.0 - 360.0 * fraction, 270.0
        draw.arc(box, start, end, fill=accent, width=round(w))
        radius = (s - 1) / 2 - w / 2
        center = (s - 1) / 2
        for angle in (start, end):
            cx = center + radius * math.cos(math.radians(angle))
            cy = center + radius * math.sin(math.radians(angle))
            draw.ellipse((cx - w / 2, cy - w / 2, cx + w / 2, cy + w / 2), fill=accent)
    return ImageTk.PhotoImage(img.resize((size, size), Image.LANCZOS))


def _one_line(text: str) -> str:
    return " ".join(text.split())[:MAX_TITLE]


def parse_choices(text: str) -> list[tuple[str, str]]:
    """The (title, line) pairs of --choices, a JSON list of {"title", "line"} objects (the line may be left out). A value
    that is missing, isn't JSON, or isn't that shape is no choices at all, so the card shows its own title as before."""
    try:
        listed = json.loads(text)
    except ValueError:
        return []
    if not isinstance(listed, list):
        return []
    pairs = []
    for item in listed:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str) or not isinstance(item.get("line", ""), str):
            return []
        pairs.append((item["title"], item.get("line", "")))
    return pairs


def show_notice(title: str, seconds: int, *, kind: str = "calendar", choices: Sequence[tuple[str, str]] = ()) -> int | None:
    """Return which meeting to start notes for when the countdown finishes or Start now is clicked, None when the user cancels.

    That is 0 for the meeting in ``title``, or the first of ``choices`` when there are two or more: (title, line) pairs
    the card lists, up to MAX_CHOICES, with the first ticked. A click ticks another, and then its place is returned.
    A card for Granola closing never lists choices.
    """
    title = _one_line(title) or "Meeting"
    listed = [] if kind == "crash" or len(choices) < 2 else [(_one_line(name) or "Meeting", _one_line(line)) for name, line in choices[:MAX_CHOICES]]
    # Per-monitor so a 150% laptop and a 100% external display each size the card correctly.
    per_monitor_dpi()
    left, top, right, bottom, dpi = _primary_monitor()
    scale = max(1.0, dpi / 96)

    def px(value: float) -> int:
        return round(value * scale)

    colors = PALETTES[app_theme()]
    root = tk.Tk()
    root.tk.call("tk", "scaling", dpi / 72.0)
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=colors["bg"])
    state = {"value": None, "done": False}

    sans = _family(root, "Segoe UI Variable Text", "Segoe UI")
    sans_strong = _family(root, "Segoe UI Variable Text Semibold", "Segoe UI Semibold")
    display = _family(root, "Segoe UI Variable Display Semib", "Segoe UI Semibold")
    serif = _family(root, "Georgia", "Cambria", "Times New Roman")
    eyebrow_font = tkfont.Font(root=root, family=sans_strong, size=8)
    title_font = tkfont.Font(root=root, family=serif, size=13)
    row_font = tkfont.Font(root=root, family=serif, size=12)  # 16 px
    body_font = tkfont.Font(root=root, family=sans, size=9)
    button_font = tkfont.Font(root=root, family=sans_strong, size=10)
    count_font = tkfont.Font(root=root, family=display, size=17)
    unit_font = tkfont.Font(root=root, family=sans, size=7)

    width = px(404)
    pad = px(20)
    ring = px(68)
    stroke = max(3, px(5))
    text_x = pad + ring + px(18)
    text_w = width - text_x - pad
    canvas = tk.Canvas(root, bg=colors["bg"], highlightthickness=0, bd=0, width=width, height=10)
    canvas.pack()
    images: list[ImageTk.PhotoImage] = []

    y = pad
    eyebrow = "GRANOLA  \u00b7  " + ("RESTARTING NOTES" if kind == "crash" else "STARTING NOTES")
    canvas.create_text(text_x, y, text=eyebrow, anchor="nw", font=eyebrow_font, fill=colors["accent_text"])
    y += eyebrow_font.metrics("linespace") + px(4)
    row_tags = [f"row{index}" for index in range(len(listed))]
    row_faces: list[int] = []
    row_radios: list[int] = []
    face_images: dict[str, ImageTk.PhotoImage] = {}
    radio_images: dict[str, ImageTk.PhotoImage] = {}
    if listed:
        # Each row reaches past the text on both sides, so its tick lines up with the eyebrow above it.
        row_x = text_x - px(8)
        row_w = text_w + px(16)
        row_pad = px(8)
        radio = px(16)
        row_text_x = text_x + radio + px(10)
        row_text_w = text_x + text_w - row_text_x
        title_h = row_font.metrics("linespace")
        row_h = max(px(40), row_pad * 2 + title_h + px(2) + body_font.metrics("linespace"))
        face_images = {
            "idle": _pill_image(row_w, row_h, None, radius=px(6)),
            "hover": _pill_image(row_w, row_h, colors["button_hover"], radius=px(6)),
            "ticked": _pill_image(row_w, row_h, colors["accent_soft"], radius=px(6)),
        }
        radio_images = {
            "off": _radio_image(radio, max(1.0, 1.5 * scale), colors["muted"], None),
            "on": _radio_image(radio, max(1.0, 1.5 * scale), colors["accent"], colors["accent"]),
        }
        images.extend((*face_images.values(), *radio_images.values()))
        for tag, (name, line) in zip(row_tags, listed):
            row_faces.append(canvas.create_image(row_x, y, image=face_images["idle"], anchor="nw", tags=tag))
            radio_y = y + row_pad + (title_h - radio) // 2
            row_radios.append(canvas.create_image(row_x + row_pad, radio_y, image=radio_images["off"], anchor="nw", tags=tag))
            text_y = y + row_pad
            canvas.create_text(row_text_x, text_y, text=_fit_lines(row_font, name, row_text_w, 1)[0], anchor="nw", font=row_font, fill=colors["ink"], tags=tag)
            text_y += title_h + px(2)
            canvas.create_text(row_text_x, text_y, text=_fit_lines(body_font, line, row_text_w, 1)[0], anchor="nw", font=body_font, fill=colors["muted"], tags=tag)
            y += row_h + px(2)
        y -= px(2)
    else:
        for line in _fit_lines(title_font, title, text_w, 2):
            canvas.create_text(text_x, y, text=line, anchor="nw", font=title_font, fill=colors["ink"])
            y += title_font.metrics("linespace")
    y += px(4)
    status = None
    calm = ["Granola opens when the timer runs out."]
    if listed:
        calm = _fit_lines(body_font, "Granola opens the ticked meeting when the timer runs out.", text_w, 2)
    if kind != "crash":
        status = canvas.create_text(text_x, y, text="", anchor="nw", font=body_font, fill=colors["muted"])
        y += body_font.metrics("linespace") * len(calm)
    else:
        dot = px(6)
        line_h = body_font.metrics("linespace")
        canvas.create_oval(text_x, y + (line_h - dot) / 2, text_x + dot, y + (line_h + dot) / 2, fill=colors["warn"], outline="")
        for line in _fit_lines(body_font, "Granola closed. Starting again unless you cancel.", text_w - dot - px(8), 2):
            canvas.create_text(text_x + dot + px(8), y, text=line, anchor="nw", font=body_font, fill=colors["warn"])
            y += line_h

    ring_y = pad + px(2)
    ring_item = canvas.create_image(pad, ring_y, anchor="nw")
    count = canvas.create_text(pad + ring / 2, ring_y + ring / 2 - px(5), text="", font=count_font, fill=colors["ink"])
    canvas.create_text(pad + ring / 2, ring_y + ring / 2 + px(14), text="SEC", font=unit_font, fill=colors["muted"])

    y = max(y, ring_y + ring) + px(14)
    canvas.create_line(pad, y, width - pad, y, fill=colors["border"], dash=(max(2, px(3)), max(2, px(3))))
    y += px(12)

    button_h = px(32)
    line_w = max(1.0, 1.25 * scale)
    # The crash line above already says Granola is starting again, so the button matches the other cards.
    start_label = "Start now"
    cancel_w = max(px(96), button_font.measure("Cancel") + px(36))
    start_w = max(px(96), button_font.measure(start_label) + px(36))
    cancel_x = width - pad - cancel_w
    start_x = cancel_x - px(10) - start_w

    def add_button(tag: str, x: int, w: int, label: str, text_color: str, normal, hover) -> None:
        images.extend((normal, hover))
        face = canvas.create_image(x, y, image=normal, anchor="nw", tags=tag)
        canvas.create_text(x + w / 2, y + button_h / 2, text=label, font=button_font, fill=text_color, tags=tag)
        canvas.tag_bind(tag, "<Enter>", lambda _e: (canvas.itemconfigure(face, image=hover), canvas.configure(cursor="hand2")))
        canvas.tag_bind(tag, "<Leave>", lambda _e: (canvas.itemconfigure(face, image=normal), canvas.configure(cursor="")))

    add_button(
        "start",
        start_x,
        start_w,
        start_label,
        colors["on_accent"],
        _pill_image(start_w, button_h, colors["start"]),
        _pill_image(start_w, button_h, colors["start_hover"]),
    )
    add_button(
        "cancel",
        cancel_x,
        cancel_w,
        "Cancel",
        colors["ink"],
        _pill_image(cancel_w, button_h, None, colors["button_line"], line_w),
        _pill_image(cancel_w, button_h, colors["button_hover"], colors["button_line"], line_w),
    )
    y += button_h + pad
    canvas.configure(height=y)

    timer = {"job": None, "check": None}  # the countdown's next tick, and the next look for Granola's popup

    def finish(answer: int | None) -> None:
        # quit() rather than destroy(): destroying inside a canvas click handler crashes Tk.
        if state["done"]:
            return
        state["done"] = True
        state["value"] = answer
        for name in ("job", "check"):
            if timer[name] is not None:
                root.after_cancel(timer[name])
        root.quit()

    pressed = {"tag": None, "at": 0.0}
    shown_at = {"at": float("inf")}
    ticked = {"row": 0}  # the meeting that starts, as its place in the list
    hovered = {"row": None}

    def paint_rows() -> None:
        for index, (face, radio_item) in enumerate(zip(row_faces, row_radios)):
            on = index == ticked["row"]
            shade = "ticked" if on else "hover" if hovered["row"] == index else "idle"
            canvas.itemconfigure(face, image=face_images[shade])
            canvas.itemconfigure(radio_item, image=radio_images["on" if on else "off"])

    def hover_row(index: int | None) -> None:
        hovered["row"] = index
        paint_rows()
        canvas.configure(cursor="" if index is None else "hand2")

    def on_press(tag: str) -> None:
        pressed["tag"] = tag
        pressed["at"] = time.monotonic()

    def on_release(tag: str, event) -> None:
        was, pressed["tag"] = pressed["tag"], None
        if was != tag:
            return
        if tag == "cancel":
            finish(None)
            return
        x1, y1, x2, y2 = canvas.bbox(tag)
        over = x1 <= event.x <= x2 and y1 <= event.y <= y2
        if not over or pressed["at"] < shown_at["at"] + START_NOW_DELAY_SECONDS:
            return
        if tag == "start":
            finish(ticked["row"])
        else:
            ticked["row"] = row_tags.index(tag)
            paint_rows()

    for tag in ("start", "cancel", *row_tags):
        canvas.tag_bind(tag, "<ButtonPress-1>", lambda _e, t=tag: on_press(t))
        canvas.tag_bind(tag, "<ButtonRelease-1>", lambda e, t=tag: on_release(t, e))
    for index, tag in enumerate(row_tags):
        canvas.tag_bind(tag, "<Enter>", lambda _e, i=index: hover_row(i))
        canvas.tag_bind(tag, "<Leave>", lambda _e: hover_row(None))
    paint_rows()

    total = max(1, seconds)
    deadline = time.monotonic() + total
    ring_frame = {"step": None, "image": None}

    def tick() -> None:
        timer["job"] = None
        if state["done"]:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            finish(ticked["row"])
            return
        shown = math.ceil(remaining)
        step = round(RING_STEPS * remaining / total)
        if step != ring_frame["step"]:
            ring_frame["step"] = step
            ring_frame["image"] = _ring_image(ring, stroke, step / RING_STEPS, colors["track"], colors["accent"])
            canvas.itemconfigure(ring_item, image=ring_frame["image"])
        canvas.itemconfigure(count, text=str(shown))
        if status is not None:
            canvas.itemconfigure(status, text="\n".join(calm) if shown > 1 else "Opening Granola\u2026")
        timer["job"] = root.after(33, tick)

    root.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    root.update_idletasks()
    height = y
    margin = max(16, px(24))
    x = max(left, right - width - margin)
    top_y = top + margin
    if top_y + height > bottom:
        top_y = max(top, bottom - height - margin)
    # Granola's own meeting popup shows in the same corner, so the card goes below it.
    top_y = _clear_of(x, top_y, width, height, _granola_windows(), bottom)
    root.geometry(f"{width}x{height}+{x}+{top_y}")
    placed = {"x": x, "y": top_y, "dragged": False}  # where the card is, and whether the user has moved it
    screen = _virtual_screen() or (left, top, right, bottom)  # read once; the primary monitor when Windows won't say
    drag = {"from": None, "moving": False, "cursor": ""}  # a press on the background: pointer and card then, and whether it moves the card yet

    def grab(event) -> None:
        # Item bindings run before this one, so a press on a button or a meeting has already set pressed["tag"].
        if pressed["tag"] is None:
            drag["from"] = (event.x_root, event.y_root, placed["x"], placed["y"])

    def move(event) -> None:
        if drag["from"] is None:
            return
        from_x, from_y, card_x, card_y = drag["from"]
        across, down = event.x_root - from_x, event.y_root - from_y
        if not drag["moving"]:
            if math.hypot(across, down) < px(DRAG_THRESHOLD):
                return
            drag["moving"] = placed["dragged"] = True
            drag["cursor"] = canvas.cget("cursor")
            canvas.configure(cursor="fleur")
        placed["x"], placed["y"] = _keep_on_screen(card_x + across, card_y + down, width, height, screen, px(KEEP_ON_SCREEN))
        root.geometry(f"{width}x{height}+{placed['x']}+{placed['y']}")

    def drop(_event) -> None:
        if drag["moving"]:
            canvas.configure(cursor=drag["cursor"])
        drag["from"], drag["moving"] = None, False

    canvas.bind("<ButtonPress-1>", grab)
    canvas.bind("<B1-Motion>", move)
    canvas.bind("<ButtonRelease-1>", drop)

    def keep_clear() -> None:
        """The popup can come up after the card. Then the card moves down below it, and never back up, so nothing shifts
        under the pointer when the popup goes. It stays put while the pointer is held down on the card, and for good once
        the user has dragged it somewhere."""
        if pressed["tag"] is None and drag["from"] is None and not placed["dragged"]:
            lower = _clear_of(x, placed["y"], width, height, _granola_windows(), bottom)
            if lower > placed["y"]:
                placed["y"] = lower
                root.geometry(f"{width}x{height}+{x}+{lower}")
        timer["check"] = root.after(POPUP_CHECK_MS, keep_clear)

    _style_card(_wrapper_hwnd(root), border=colors["border"])
    root.deiconify()
    root.lift()
    shown_at["at"] = time.monotonic()
    tick()
    timer["check"] = root.after(POPUP_CHECK_MS, keep_clear)
    root.mainloop()
    root.destroy()
    return state["value"]


def keep_display_on() -> None:
    """The countdown must stay visible, so the display may not sleep while it runs."""
    try:
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_DISPLAY_REQUIRED)
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Show the Granola start countdown")
    parser.add_argument("--title", required=True)
    parser.add_argument("--seconds", type=int, default=10)
    parser.add_argument("--kind", default="calendar")
    parser.add_argument("--choices", default="", help='the meetings starting together, as JSON: [{"title": ..., "line": ...}, ...]')
    return parser


def main(argv: list[str] | None = None) -> int:
    """0 to start Granola, 1 for Cancel, and 10 + i to start the meeting at place i of --choices. __main__ turns a failure
    into exit code 2, with the reason logged."""
    args = build_parser().parse_args(argv)
    keep_display_on()
    # An update isn't installed while the card is up, because the installer stops every process of the app. Shared:
    # the watcher already holds it while this card starts, and a card that overlaps another keeps it up as well.
    processes.hold_mutex(processes.CARD_MUTEX, shared=True)
    try:
        picked = show_notice(args.title, args.seconds, kind=args.kind, choices=parse_choices(args.choices))
        if picked is None:
            return 1
        return 0 if picked == 0 else processes.CHOICE_EXIT_CODE + picked
    finally:
        processes.release_mutex(processes.CARD_MUTEX)
