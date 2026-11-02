"""Tests run on PCs where the app itself is installed and running, so none of them may touch it: each
test gets its own app folder, named events, and mutexes, and none starts one of the app's processes,
shows a notification, downloads a calendar or an update, opens a file or Granola, or writes the registry."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from granola_autostart import calendar_feed, commands, granola_launch, notice, notify, processes, settings_app, updates, watcher


def _refuse(*_args, **_kwargs):
    # pytest.fail, not an exception: the app catches Exception around launches and spawns.
    pytest.fail("a test reached a real process, file, registry key, or Granola; fake it or use the spawned fixture")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    prefix = f"Local\\GranolaAutostartTest{os.getpid()}"
    monkeypatch.setattr(commands, "PREFIX", f"{prefix}-")
    for name in ("WATCHER_MUTEX", "SUPERVISOR_MUTEX", "SETTINGS_MUTEX", "CARD_MUTEX", "INSTALL_MUTEX"):
        monkeypatch.setattr(processes, name, f"{prefix}{name}")
    for module, name in (
        (processes, "spawn"),
        (processes, "spawn_detached"),  # the update installer
        (processes, "install"),
        (processes, "uninstall"),
        (processes, "set_start_at_sign_in"),  # the sign-in switch: the registry's Run key
        (processes, "stop"),
        (granola_launch, "shell_execute"),
        (notify, "register_app_id"),
        (watcher, "register_app_id"),  # the watcher calls its own imported copy
        (notice, "show_notice"),
        (watcher.pystray, "Icon"),  # a real tray icon
        (notify.Notifier, "show"),
        (os, "startfile"),
    ):
        monkeypatch.setattr(module, name, _refuse)
    # The countdown card, the supervisor's watcher, schtasks and PowerShell, and calendar and update downloads.
    # A test that needs one replaces these with its own fake.
    monkeypatch.setattr(watcher, "subprocess", SimpleNamespace(run=_refuse, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(
        processes,
        "subprocess",
        SimpleNamespace(Popen=_refuse, run=_refuse, call=_refuse, DEVNULL=subprocess.DEVNULL),
    )
    monkeypatch.setattr(calendar_feed, "_opener", SimpleNamespace(open=_refuse))
    monkeypatch.setattr(updates, "_opener", SimpleNamespace(open=_refuse))
    # Whether an update's installer is running is read off the process list, which is this PC's: no installer is.
    monkeypatch.setattr(updates, "process_table", lambda: {})
    monkeypatch.setattr(notice, "keep_display_on", lambda: None)
    monkeypatch.setattr(processes, "start_at_sign_in", lambda: False)  # reads that key, so it is the same on every PC
    # Whether Zoom and Teams are installed here: Settings' Calls page says so, and the answer would depend on the PC.
    monkeypatch.setattr(settings_app, "call_installed", lambda provider: {"zoom": True, "teams": False}.get(provider))


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    """The commands the app started, recorded instead of run."""
    commands_run: list[list[str]] = []
    monkeypatch.setattr(processes, "spawn", commands_run.append)
    return commands_run


@pytest.fixture
def held_elsewhere():
    """Holds a named mutex as if another process had it: ``hold(name)`` returns the function that lets go of it.
    Whatever is still held at the end of the test is let go."""
    handles: list[list] = []

    def hold(name: str):
        handle = [processes._kernel32.CreateMutexW(None, False, name)]
        assert handle[0]
        handles.append(handle)

        def let_go() -> None:
            if handle[0]:
                processes._kernel32.CloseHandle(handle[0])
                handle[0] = None

        return let_go

    yield hold
    for handle in handles:
        if handle[0]:
            processes._kernel32.CloseHandle(handle[0])
