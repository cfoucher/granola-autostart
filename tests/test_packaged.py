import subprocess
import sys

import pytest

from granola_autostart import watcher
from granola_autostart.notice import build_parser

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
    assert "__main__.py*'" in script
    for own_mode in ("--uninstall", "--install", "--stop"):
        assert f"-notlike '*{own_mode}*'" in script


def test_from_source_every_mode_runs_the_package_main_with_pythonw():
    command = watcher.app_command("--supervise")
    assert command[1].endswith("__main__.py")
    assert command[2:] == ["--supervise"]
    assert watcher.notice_command("Standup", 10, "calendar")[1:3] == [command[1], "--notice"]


def test_the_package_main_runs_by_file_path_from_any_folder(tmp_path):
    # A source checkout's sign-in entry starts it this way, with no current folder to import from.
    completed = subprocess.run([sys.executable, str(watcher.MAIN), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "--supervise" not in completed.stdout and "--dry-run" in completed.stdout


def test_the_supervisor_restarts_a_crash_but_not_a_quit(monkeypatch):
    exits = iter([1, watcher.QUIT_EXIT_CODE])
    runs = []

    def run(command, cwd):
        runs.append(command)
        return watcher.subprocess.CompletedProcess(command, next(exits))

    monkeypatch.setattr(watcher, "acquire_supervisor", lambda: True)
    monkeypatch.setattr(watcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(watcher.subprocess, "run", run)
    assert watcher.supervise() == 0
    assert len(runs) == 2
