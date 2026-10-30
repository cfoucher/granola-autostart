import sys

import pytest

import watcher
from notice import build_parser

EXE = r"C:\Users\someone\AppData\Local\Programs\Granola Autostart\GranolaAutostart.exe"


@pytest.fixture
def packaged(monkeypatch):
    monkeypatch.setattr(watcher, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", EXE)


def test_the_packaged_exe_shows_the_card_through_its_notice_mode(packaged):
    command = watcher.notice_command("-- standup", 10, "crash")
    assert command[:2] == [EXE, "--notice"]
    args = build_parser().parse_args(command[2:])
    assert (args.title, args.seconds, args.kind) == ("-- standup", 10, "crash")


def test_the_packaged_exe_starts_itself_for_every_mode(packaged):
    assert watcher.app_command() == [EXE]
    assert watcher.app_command("--supervise") == [EXE, "--supervise"]
    assert watcher.quoted_command("--supervise") == f'"{EXE}" --supervise'


def test_the_packaged_stop_only_matches_this_exe_and_spares_itself(packaged):
    script = watcher.stop_script()
    filter_clause = script.split("Where-Object { ", 1)[1].split(" }", 1)[0]
    assert filter_clause == f"$_.ExecutablePath -eq '{EXE}' -and $_.ProcessId -ne {watcher.os.getpid()}"


def test_stopping_from_source_spares_the_command_doing_the_stopping():
    script = watcher.stop_script()
    assert "watcher.py*'" in script
    for own_mode in ("--uninstall", "--install", "--stop"):
        assert f"-notlike '*{own_mode}*'" in script


def test_from_source_the_modes_run_this_script_with_pythonw():
    command = watcher.app_command("--supervise")
    assert command[1].endswith("watcher.py")
    assert command[2:] == ["--supervise"]
    assert watcher.notice_command("Standup", 10, "calendar")[1].endswith("notice.py")
