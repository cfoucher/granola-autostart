"""Countdown card. Timeout or Start now starts Granola. Cancel does not.

The card never takes keyboard focus, so typing in the current app is not interrupted.
"""

from __future__ import annotations

import argparse
import ctypes
import math
import os
import time
import tkinter as tk
import tkinter.font as tkfont
import winreg
from ctypes import wintypes

from PIL import Image, ImageDraw, ImageTk

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
THEME: str | None = None  # "light" | "dark" | None for the Windows app theme
MAX_TITLE = 200
RING_STEPS = 120
# A click already on its way to that corner when the card appears must not skip the countdown.
START_NOW_DELAY_SECONDS = 0.75

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


def _enable_per_monitor_dpi() -> None:
    try:
        # Per-monitor v2 so a 150% laptop and a 100% external display each size the card correctly.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _monitor_at_cursor() -> tuple[int, int, int, int, int]:
    """Work area of the monitor under the pointer, plus that monitor's DPI."""
    user32 = ctypes.windll.user32
    user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    user32.GetCursorPos.restype = wintypes.BOOL
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    user32.MonitorFromPoint.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MONITORINFO)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    point = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(point))
    monitor = user32.MonitorFromPoint(point, 2)
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


def _theme() -> str:
    if THEME in PALETTES:
        return THEME
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


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
        dwm = ctypes.windll.dwmapi
        round_corners = ctypes.c_int(2)
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 33, ctypes.byref(round_corners), ctypes.sizeof(round_corners))
        r, g, b = (int(border[i : i + 2], 16) for i in (1, 3, 5))
        colorref = ctypes.c_uint(r | (g << 8) | (b << 16))
        dwm.DwmSetWindowAttribute(wintypes.HWND(hwnd), 34, ctypes.byref(colorref), ctypes.sizeof(colorref))
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


def _pill_image(width: int, height: int, fill: str | None, outline: str | None = None, line: float = 0) -> ImageTk.PhotoImage:
    ss = 4
    img = Image.new("RGBA", (width * ss, height * ss), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    inset = line * ss / 2
    draw.rounded_rectangle(
        (inset, inset, width * ss - 1 - inset, height * ss - 1 - inset),
        radius=height * ss / 2,
        fill=fill,
        outline=outline,
        width=round(line * ss) if outline else 0,
    )
    return ImageTk.PhotoImage(img.resize((width, height), Image.LANCZOS))


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


def show_notice(title: str, seconds: int, *, kind: str = "calendar", auto: str | None = None) -> bool:
    """Return True when the countdown finishes or Start now is clicked, False when the user cancels."""
    title = " ".join(title.split())[:MAX_TITLE] or "Meeting"
    _enable_per_monitor_dpi()
    left, top, right, bottom, dpi = _monitor_at_cursor()
    scale = max(1.0, dpi / 96)

    def px(value: float) -> int:
        return round(value * scale)

    colors = PALETTES[_theme()]
    root = tk.Tk()
    root.tk.call("tk", "scaling", dpi / 72.0)
    root.withdraw()
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=colors["bg"])
    state = {"value": False, "done": False}

    sans = _family(root, "Segoe UI Variable Text", "Segoe UI")
    sans_strong = _family(root, "Segoe UI Variable Text Semibold", "Segoe UI Semibold")
    display = _family(root, "Segoe UI Variable Display Semib", "Segoe UI Semibold")
    serif = _family(root, "Georgia", "Cambria", "Times New Roman")
    eyebrow_font = tkfont.Font(root=root, family=sans_strong, size=8)
    title_font = tkfont.Font(root=root, family=serif, size=13)
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
    for line in _fit_lines(title_font, title, text_w, 2):
        canvas.create_text(text_x, y, text=line, anchor="nw", font=title_font, fill=colors["ink"])
        y += title_font.metrics("linespace")
    y += px(4)
    status = None
    if kind != "crash":
        status = canvas.create_text(text_x, y, text="", anchor="nw", font=body_font, fill=colors["muted"])
        y += body_font.metrics("linespace")
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

    timer = {"job": None}

    def finish(start: bool) -> None:
        # quit() rather than destroy(): destroying inside a canvas click handler crashes Tk.
        if state["done"]:
            return
        state["done"] = True
        state["value"] = start
        if timer["job"] is not None:
            root.after_cancel(timer["job"])
        root.quit()

    pressed = {"tag": None, "at": 0.0}
    shown_at = {"at": float("inf")}

    def on_press(tag: str) -> None:
        pressed["tag"] = tag
        pressed["at"] = time.monotonic()

    def on_release(tag: str, event) -> None:
        was, pressed["tag"] = pressed["tag"], None
        if was != tag:
            return
        if tag == "cancel":
            finish(False)
            return
        x1, y1, x2, y2 = canvas.bbox(tag)
        over = x1 <= event.x <= x2 and y1 <= event.y <= y2
        if over and pressed["at"] >= shown_at["at"] + START_NOW_DELAY_SECONDS:
            finish(True)

    for tag in ("start", "cancel"):
        canvas.tag_bind(tag, "<ButtonPress-1>", lambda _e, t=tag: on_press(t))
        canvas.tag_bind(tag, "<ButtonRelease-1>", lambda e, t=tag: on_release(t, e))

    total = max(1, seconds)
    deadline = time.monotonic() + total
    ring_frame = {"step": None, "image": None}

    def tick() -> None:
        timer["job"] = None
        if state["done"]:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            finish(True)
            return
        shown = math.ceil(remaining)
        step = round(RING_STEPS * remaining / total)
        if step != ring_frame["step"]:
            ring_frame["step"] = step
            ring_frame["image"] = _ring_image(ring, stroke, step / RING_STEPS, colors["track"], colors["accent"])
            canvas.itemconfigure(ring_item, image=ring_frame["image"])
        canvas.itemconfigure(count, text=str(shown))
        if status is not None:
            canvas.itemconfigure(status, text="Granola opens when the timer runs out." if shown > 1 else "Opening Granola\u2026")
        timer["job"] = root.after(33, tick)

    root.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    root.update_idletasks()
    height = y
    margin = max(16, px(24))
    x = max(left, right - width - margin)
    top_y = top + margin
    if top_y + height > bottom:
        top_y = max(top, bottom - height - margin)
    root.geometry(f"{width}x{height}+{x}+{top_y}")
    _style_card(_wrapper_hwnd(root), border=colors["border"])
    root.deiconify()
    root.lift()
    shown_at["at"] = time.monotonic()
    if os.environ.get("GRANOLA_NOTICE_DEBUG"):
        print(f"monitor {left},{top}-{right},{bottom} dpi={dpi} notice {width}x{height} at {x},{top_y}", flush=True)
    tick()
    if auto == "cancel":
        root.after(200, lambda: finish(False))
    elif auto == "start":
        root.after(200, lambda: finish(True))
    root.mainloop()
    root.destroy()
    return bool(state["value"])


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
    parser.add_argument("--auto", choices=("cancel", "start"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    keep_display_on()
    try:
        started = show_notice(args.title, args.seconds, kind=args.kind, auto=args.auto)
    except Exception:
        return 2
    return 0 if started else 1
