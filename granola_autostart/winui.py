"""Windows look and feel shared by the countdown card, Settings, and the tray: the app theme,
per-monitor DPI, and window colours. The standard library and settings.py only, so the card still opens quickly."""

from __future__ import annotations

import ctypes
import winreg
from ctypes import wintypes

from .settings import saved_appearance


def app_theme() -> str:
    """"light" or "dark": the Appearance chosen in Settings, or on Auto the Windows app theme."""
    chosen = saved_appearance()
    return windows_theme() if chosen == "auto" else chosen


def windows_theme() -> str:
    """"light" or "dark", following the Windows app theme."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def per_monitor_dpi() -> None:
    """Per-monitor v2, so a 150% laptop and a 100% external display each draw sharply."""
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        pass


def set_window_attribute(hwnd: int, attribute: int, value: int | str) -> None:
    """DwmSetWindowAttribute with a number, or a "#rrggbb" colour. Windows 10 ignores the Windows 11 ones."""
    if isinstance(value, str):
        red, green, blue = (int(value[i : i + 2], 16) for i in (1, 3, 5))
        value = red | (green << 8) | (blue << 16)
    data = ctypes.c_int(value)
    ctypes.windll.dwmapi.DwmSetWindowAttribute(wintypes.HWND(hwnd), attribute, ctypes.byref(data), ctypes.sizeof(data))
