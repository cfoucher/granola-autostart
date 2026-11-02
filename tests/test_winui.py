import ctypes
import json
import winreg
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from granola_autostart import winui
from granola_autostart.settings import config_path


class Personalize:
    """winreg with only the app theme value, or without it."""

    HKEY_CURRENT_USER = winreg.HKEY_CURRENT_USER

    def __init__(self, light: int | None):
        self.light = light

    @contextmanager
    def OpenKey(self, root, path):
        if self.light is None:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        yield path

    def QueryValueEx(self, key, name):
        assert name == "AppsUseLightTheme"
        return self.light, winreg.REG_DWORD


@pytest.mark.parametrize(("light", "theme"), [(1, "light"), (0, "dark"), (None, "light")])
def test_the_app_theme_follows_windows_and_is_light_when_unset(light, theme, monkeypatch):
    monkeypatch.setattr(winui, "winreg", Personalize(light))
    assert winui.app_theme() == theme


def save_appearance(value) -> None:
    config_path().write_text(json.dumps({"appearance": value}), encoding="utf-8")


@pytest.mark.parametrize(
    ("appearance", "windows_light", "theme"),
    [
        ("auto", 1, "light"),
        ("auto", 0, "dark"),
        ("light", 1, "light"),
        ("light", 0, "light"),
        ("dark", 1, "dark"),
        ("dark", 0, "dark"),
    ],
)
def test_the_app_theme_is_the_chosen_appearance_or_on_auto_whatever_windows_uses(appearance, windows_light, theme, monkeypatch):
    monkeypatch.setattr(winui, "winreg", Personalize(windows_light))
    save_appearance(appearance)
    assert winui.app_theme() == theme


@pytest.mark.parametrize("value", [None, "sepia", 3, ["dark"]])
def test_an_appearance_this_version_does_not_know_follows_windows_like_auto(value, monkeypatch):
    monkeypatch.setattr(winui, "winreg", Personalize(0))
    save_appearance(value)
    assert winui.app_theme() == "dark"


def test_a_missing_or_broken_config_follows_windows_and_never_stops_the_card_opening(monkeypatch):
    monkeypatch.setattr(winui, "winreg", Personalize(0))
    assert winui.app_theme() == "dark"  # no config.json
    config_path().write_text("{ broken", encoding="utf-8")
    assert winui.app_theme() == "dark"
    config_path().write_bytes(b"\xff\xfe not text")
    assert winui.app_theme() == "dark"
    monkeypatch.delenv("GRANOLA_AUTOSTART_HOME")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert winui.app_theme() == "dark"  # nowhere to look


def fake_ctypes(monkeypatch, **dlls) -> None:
    """ctypes with only the DLLs given, so a call the test leaves out is missing, as on an older Windows."""
    windll = SimpleNamespace(**dlls)
    monkeypatch.setattr(winui, "ctypes", SimpleNamespace(windll=windll, c_void_p=ctypes.c_void_p, c_int=ctypes.c_int, byref=ctypes.byref, sizeof=ctypes.sizeof))


def dpi_calls(monkeypatch, context_answer) -> list:
    calls = []

    def set_context(context):
        calls.append(("context", context.value))
        return context_answer

    fake_ctypes(
        monkeypatch,
        user32=SimpleNamespace(SetProcessDpiAwarenessContext=set_context),
        shcore=SimpleNamespace(SetProcessDpiAwareness=lambda level: calls.append(("awareness", level))),
    )
    return calls


def test_per_monitor_v2_is_used_when_windows_has_it(monkeypatch):
    calls = dpi_calls(monkeypatch, True)
    winui.per_monitor_dpi()
    assert calls == [("context", ctypes.c_void_p(-4).value)]


def test_an_older_windows_falls_back_to_per_monitor_awareness(monkeypatch):
    calls = dpi_calls(monkeypatch, False)
    winui.per_monitor_dpi()
    assert calls[-1] == ("awareness", 2)
    calls = []
    fake_ctypes(monkeypatch, user32=SimpleNamespace(), shcore=SimpleNamespace(SetProcessDpiAwareness=lambda level: calls.append(level)))
    winui.per_monitor_dpi()
    assert calls == [2]


def test_without_any_dpi_call_windows_just_stretches_the_window(monkeypatch):
    fake_ctypes(monkeypatch, user32=SimpleNamespace())
    winui.per_monitor_dpi()


def test_a_colour_reaches_windows_as_a_colorref_and_a_number_as_itself(monkeypatch):
    sent = []

    def dwm_set(hwnd, attribute, data, size):
        sent.append((hwnd.value, attribute, data._obj.value, size))

    fake_ctypes(monkeypatch, dwmapi=SimpleNamespace(DwmSetWindowAttribute=dwm_set))
    winui.set_window_attribute(0x1234, 34, "#e2d9c6")
    winui.set_window_attribute(0x1234, 33, 2)
    assert sent == [(0x1234, 34, 0xC6D9E2, 4), (0x1234, 33, 2, 4)]
