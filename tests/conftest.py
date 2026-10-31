"""Tests run on PCs where the app itself is installed and running, so none of them may touch it: each
test gets its own app folder, named events, and mutexes, and none starts one of the app's processes,
shows a notification, downloads a calendar, opens a file or Granola, or writes the registry."""

import os
import subprocess
from types import SimpleNamespace

import pytest

from granola_autostart import calendar_feed, commands, granola_launch, notice, notify, processes, watcher


def _refuse(*_args, **_kwargs):
    # pytest.fail, not an exception: the app catches Exception around launches and spawns.
    pytest.fail("a test reached a real process, file, registry key, or Granola; fake it or use the spawned fixture")


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    prefix = f"Local\\GranolaAutostartTest{os.getpid()}"
    monkeypatch.setattr(commands, "PREFIX", f"{prefix}-")
    for name in ("WATCHER_MUTEX", "SUPERVISOR_MUTEX", "SETTINGS_MUTEX"):
        monkeypatch.setattr(processes, name, f"{prefix}{name}")
    for module, name in (
        (processes, "spawn"),
        (processes, "install"),
        (processes, "uninstall"),
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
    # The countdown card, the supervisor's watcher, schtasks and PowerShell, and calendar downloads.
    # A test that needs one replaces these with its own fake.
    monkeypatch.setattr(watcher, "subprocess", SimpleNamespace(run=_refuse, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(
        processes,
        "subprocess",
        SimpleNamespace(Popen=_refuse, run=_refuse, call=_refuse, DEVNULL=subprocess.DEVNULL),
    )
    monkeypatch.setattr(calendar_feed, "_opener", SimpleNamespace(open=_refuse))
    monkeypatch.setattr(notice, "keep_display_on", lambda: None)


@pytest.fixture
def spawned(monkeypatch) -> list[list[str]]:
    """The commands the app started, recorded instead of run."""
    commands_run: list[list[str]] = []
    monkeypatch.setattr(processes, "spawn", commands_run.append)
    return commands_run
