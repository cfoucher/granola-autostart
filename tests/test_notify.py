import logging
import sys
import winreg
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace

import pytest

from granola_autostart import notify
from granola_autostart.notify import register_app_id as real_register_app_id
from granola_autostart.status import Problem

real_show = notify.Notifier.show
PROBLEM = Problem(
    "launch:abc",
    "Granola didn't open",
    "Design review was due to start notes.",
    "about",
    actions=(("Try again", "retry"), ("Open log folder", "logs")),
)


class Toaster:
    def __init__(self, app_name, notifierAUMID):
        self.app = (app_name, notifierAUMID)
        self.shown = []
        self.broken = False

    def show_toast(self, toast):
        if self.broken:
            raise OSError("the notification platform is not available")
        self.shown.append(toast)


class Toast:
    def __init__(self, text_fields):
        self.text_fields = text_fields
        self.images = []
        self.actions = []
        self.on_activated = None

    def AddImage(self, image):
        self.images.append(image)

    def AddAction(self, button):
        self.actions.append(button)


@pytest.fixture
def toasters(monkeypatch) -> list[Toaster]:
    """windows_toasts, faked: the toasts the app built, never shown."""
    made: list[Toaster] = []
    module = ModuleType("windows_toasts")
    module.InteractableWindowsToaster = lambda *args, **kwargs: made.append(Toaster(*args, **kwargs)) or made[-1]
    module.Toast = Toast
    module.ToastButton = lambda label, action: (label, action)
    module.ToastDisplayImage = SimpleNamespace(fromPath=lambda path, position: (path, position))
    module.ToastImagePosition = SimpleNamespace(AppLogo="app logo")
    monkeypatch.setitem(sys.modules, "windows_toasts", module)
    monkeypatch.setattr(notify.Notifier, "show", real_show)
    return made


def test_a_problem_becomes_a_toast_with_its_buttons_and_the_app_s_logo(toasters, tmp_path):
    logo = tmp_path / "app.png"
    logo.write_bytes(b"png")
    notify.Notifier(lambda *_args: None, lambda *_args: pytest.fail("fell back to a balloon"), logo=logo).show(PROBLEM)
    [toaster] = toasters
    [toast] = toaster.shown
    assert toaster.app == ("Granola Autostart", "GranolaAutostart")
    assert toast.text_fields == [PROBLEM.title, PROBLEM.detail]
    assert toast.images == [(logo, "app logo")]
    assert toast.actions == [("Try again", "retry"), ("Open log folder", "logs")]


def test_a_logo_that_is_not_there_is_left_out(toasters, tmp_path):
    notify.Notifier(lambda *_args: None, lambda *_args: None, logo=tmp_path / "app.png").show(PROBLEM)
    assert toasters[0].shown[0].images == []


def test_a_button_runs_its_action_and_the_body_opens_settings(toasters):
    actions = []
    notify.Notifier(lambda action, problem: actions.append((action, problem.key)), lambda *_args: None).show(PROBLEM)
    toast = toasters[0].shown[0]
    toast.on_activated(SimpleNamespace(arguments="retry"))
    toast.on_activated(SimpleNamespace(arguments=""))
    toast.on_activated(object())
    assert actions == [("retry", "launch:abc"), ("open", "launch:abc"), ("open", "launch:abc")]


def test_an_action_that_fails_is_logged_not_raised_on_windows_thread(toasters, caplog):
    def broken(action, problem):
        raise RuntimeError("settings would not open")

    notify.Notifier(broken, lambda *_args: None).show(PROBLEM)
    with caplog.at_level(logging.ERROR, logger="granola_autostart"):
        toasters[0].shown[0].on_activated(SimpleNamespace(arguments="settings"))
    assert "notification action failed" in caplog.text


def test_without_windows_notifications_a_tray_balloon_says_it(monkeypatch):
    monkeypatch.setitem(sys.modules, "windows_toasts", None)  # the import fails
    monkeypatch.setattr(notify.Notifier, "show", real_show)
    balloons = []
    notify.Notifier(lambda *_args: None, lambda title, detail: balloons.append((title, detail))).show(PROBLEM)
    assert balloons == [(PROBLEM.title, PROBLEM.detail)]


def test_a_toast_that_cannot_be_shown_falls_back_to_a_balloon(toasters):
    balloons = []
    notifier = notify.Notifier(lambda *_args: None, lambda title, detail: balloons.append(title))
    toasters[0].broken = True
    notifier.show(PROBLEM)
    assert balloons == [PROBLEM.title]


def test_the_app_id_is_registered_with_its_name_and_icon(monkeypatch, tmp_path):
    values = {}

    @contextmanager
    def create_key(root, path, _reserved, access):
        assert (root, access) == (winreg.HKEY_CURRENT_USER, winreg.KEY_SET_VALUE)
        yield path

    registry = SimpleNamespace(
        HKEY_CURRENT_USER=winreg.HKEY_CURRENT_USER,
        KEY_SET_VALUE=winreg.KEY_SET_VALUE,
        REG_SZ=winreg.REG_SZ,
        CreateKeyEx=create_key,
        SetValueEx=lambda key, name, _reserved, kind, value: values.update({(key, name): (kind, value)}),
    )
    monkeypatch.setattr(notify, "winreg", registry)
    real_register_app_id(tmp_path / "app.png")
    key = r"Software\Classes\AppUserModelId\GranolaAutostart"
    assert values == {(key, "DisplayName"): (winreg.REG_SZ, "Granola Autostart"), (key, "IconUri"): (winreg.REG_SZ, str(tmp_path / "app.png"))}
