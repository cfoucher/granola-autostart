import os
import subprocess
import sys
import uuid
import winreg
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import granola_autostart.__main__ as entry
from granola_autostart import notice, processes, settings, settings_app, watcher
from granola_autostart.notice import build_parser

# conftest refuses these on the module for every test; the functions themselves, taken at import, are real.
from granola_autostart.processes import install as real_install
from granola_autostart.processes import uninstall as real_uninstall

EXE = r"C:\Users\someone\AppData\Local\Programs\Granola Autostart\GranolaAutostart.exe"
# Captured at import, before conftest gives each test its own names.
REAL_MUTEXES = (processes.WATCHER_MUTEX, processes.SUPERVISOR_MUTEX, processes.SETTINGS_MUTEX)


@pytest.fixture
def packaged(request, monkeypatch) -> str:
    """Run as the installed exe, at EXE unless the test passes another path through indirect parametrize."""
    exe = getattr(request, "param", EXE)
    monkeypatch.setattr(processes, "FROZEN", True)
    monkeypatch.setattr(sys, "executable", exe)
    return exe


@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_a_title_starting_with_a_dash_reaches_the_card_whole(mode, request):
    if mode == "packaged":
        request.getfixturevalue("packaged")
    command = processes.notice_command("-- standup", 10, "crash")
    args = build_parser().parse_args(command[command.index("--notice") + 1 :])
    assert (args.title, args.seconds, args.kind) == ("-- standup", 10, "crash")


def test_the_packaged_exe_starts_itself_for_every_mode(packaged):
    assert processes.app_command() == [EXE]
    assert processes.app_command("--supervise") == [EXE, "--supervise"]
    assert processes.notice_command("Standup", 10)[:2] == [EXE, "--notice"]
    assert processes.quoted_command("--supervise") == f'"{EXE}" --supervise'


def test_settings_starts_the_other_modes_through_the_same_commands(packaged, monkeypatch, spawned):
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    api = settings_app.Api("settings", "calendars")
    api.start_watcher()
    api.preview()
    assert [command[:2] for command in spawned] == [[EXE, "--supervise"], [EXE, "--notice"]]


def test_the_packaged_stop_only_matches_this_exe_and_spares_itself(packaged):
    script = processes.stop_script()
    filter_clause = script.split("Where-Object { ", 1)[1].split(" }", 1)[0]
    assert filter_clause == f"$_.ExecutablePath -eq '{EXE}' -and $_.ProcessId -ne {os.getpid()}"


@pytest.mark.parametrize("packaged", [r"C:\Users\o'brien\Granola Autostart\GranolaAutostart.exe"], indirect=True)
def test_a_quote_in_the_install_folder_cannot_break_the_stop_script(packaged):
    assert "-eq 'C:\\Users\\o''brien\\Granola Autostart\\GranolaAutostart.exe'" in processes.stop_script()


def test_a_quote_or_bracket_in_the_source_folder_cannot_break_the_stop_script(monkeypatch):
    monkeypatch.setattr(processes, "MAIN", Path(r"C:\src\[o'brien]\granola_autostart\__main__.py"))
    assert r"-like '*C:\src\`[o''brien`]\granola_autostart\__main__.py*'" in processes.stop_script()


def test_stopping_from_source_spares_the_command_doing_the_stopping():
    script = processes.stop_script()
    assert "__main__.py*'" in script and "-m granola_autostart*'" in script
    for own_mode in ("--uninstall", "--install", "--stop"):
        assert f"-notlike '*{own_mode}*'" in script


@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_stopping_ends_the_supervisor_first_so_it_cannot_restart_the_watcher(mode, request):
    if mode == "packaged":
        request.getfixturevalue("packaged")
    script = processes.stop_script()
    order = script.split("Sort-Object ", 1)[1].split(" | ", 1)[0]
    assert order == "{ if ($_.CommandLine -like '*--supervise*') { 0 } else { 1 } }"
    assert script.index("Sort-Object") < script.index("Stop-Process")


def test_from_source_every_mode_runs_the_package_main_with_pythonw_when_it_exists():
    command = processes.app_command("--supervise")
    if Path(sys.executable).with_name("pythonw.exe").exists():
        assert command[0].endswith("pythonw.exe")
    else:
        assert command[0] == sys.executable
    assert command[1:] == [str(processes.MAIN), "--supervise"]
    assert processes.notice_command("Standup", 10)[1:3] == [str(processes.MAIN), "--notice"]


def test_the_package_main_runs_by_file_path_from_any_folder(tmp_path):
    # A source checkout's sign-in entry starts it this way, with no current folder to import from.
    completed = subprocess.run(
        [sys.executable, str(processes.MAIN), "--help"], cwd=tmp_path, capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
    assert "--supervise" not in completed.stdout and "--dry-run" in completed.stdout and "--settings" in completed.stdout


@pytest.mark.parametrize(
    ("argv", "called"),
    [
        (["--notice", "--title=Standup"], [("notice", ["--title=Standup"])]),
        (["--settings", "--section=about"], [("settings", ["--section=about"])]),
        (["--setup"], [("settings", ["--setup"])]),
        (["--supervise"], [("logging", "supervisor-log.txt"), ("supervise", None)]),
        (["--check"], [("watcher", None)]),
        ([], [("watcher", None)]),
    ],
)
def test_each_mode_goes_to_its_own_module(argv, called, monkeypatch):
    calls = []
    monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", *argv])
    monkeypatch.setattr(notice, "main", lambda args: calls.append(("notice", args)) or 0)
    monkeypatch.setattr(settings_app, "main", lambda args: calls.append(("settings", args)) or 0)
    monkeypatch.setattr(watcher, "main", lambda: calls.append(("watcher", None)) or 0)
    monkeypatch.setattr(settings, "setup_logging", lambda name: calls.append(("logging", name)))
    monkeypatch.setattr(processes, "supervise", lambda: calls.append(("supervise", None)) or 0)
    assert entry.main() == 0
    assert calls == called


def test_a_named_mutex_has_one_owner_until_released():
    name = f"Local\\GranolaAutostartTest{os.getpid()}-{uuid.uuid4().hex}"
    assert not processes.mutex_exists(name)
    assert processes.hold_mutex(name)
    assert processes.mutex_exists(name)
    assert not processes.hold_mutex(name)
    processes.release_mutex(name)
    assert not processes.mutex_exists(name)


def supervised(monkeypatch, runs: list[tuple[int, int]]) -> list[float]:
    """The supervisor's sleeps over these watcher runs, each (exit code, seconds it ran), on a fake clock."""
    remaining = iter(runs)
    clock = [0.0]
    sleeps = []

    def run(command, cwd):
        code, seconds = next(remaining)
        clock[0] += seconds
        return subprocess.CompletedProcess(command, code)

    monkeypatch.setattr(processes, "hold_mutex", lambda name: True)
    monkeypatch.setattr(processes, "subprocess", SimpleNamespace(run=run, CompletedProcess=subprocess.CompletedProcess))
    monkeypatch.setattr(processes, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleeps.append))
    assert processes.supervise() == 0
    return sleeps


def test_the_supervisor_backs_off_quick_crashes_and_stops_on_quit(monkeypatch):
    assert supervised(monkeypatch, [(1, 0), (1, 0), (1, 0), (1, 120), (processes.QUIT_EXIT_CODE, 0)]) == [3, 6, 12, 3]


def test_the_supervisor_never_waits_longer_than_the_cap(monkeypatch):
    sleeps = supervised(monkeypatch, [(1, 0)] * 10 + [(processes.QUIT_EXIT_CODE, 0)])
    assert sleeps == [3, 6, 12, 24, 48, 96, 192, 300, 300, 300]
    assert max(sleeps) == processes.MAX_RESTART_DELAY_SECONDS


def test_a_second_supervisor_leaves_the_first_one_alone(monkeypatch):
    monkeypatch.setattr(processes, "hold_mutex", lambda name: False)
    monkeypatch.setattr(processes, "subprocess", SimpleNamespace(run=lambda *_args, **_kwargs: pytest.fail("started a watcher")))
    assert processes.supervise() == 0


class FakeRegistry:
    """Stands in for winreg: values live in a dict keyed by (key path, value name)."""

    HKEY_CURRENT_USER = winreg.HKEY_CURRENT_USER
    KEY_SET_VALUE = winreg.KEY_SET_VALUE
    REG_SZ = winreg.REG_SZ

    def __init__(self):
        self.values: dict[tuple[str, str], tuple[int, str]] = {}
        self.opened = []

    @contextmanager
    def OpenKey(self, root, path, _reserved, access):
        self.opened.append((root, path, access))
        yield path

    def SetValueEx(self, key, name, _reserved, kind, value):
        self.values[(key, name)] = (kind, value)

    def DeleteValue(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[(key, name)]


def test_install_adds_the_sign_in_entry_and_starts_the_supervisor(packaged, monkeypatch, spawned):
    registry = FakeRegistry()
    monkeypatch.setattr(processes, "winreg", registry)
    real_install()
    assert registry.opened == [(winreg.HKEY_CURRENT_USER, processes.RUN_KEY, winreg.KEY_SET_VALUE)]
    assert registry.values == {(processes.RUN_KEY, processes.RUN_VALUE): (winreg.REG_SZ, f'"{EXE}" --supervise')}
    assert spawned == [[EXE, "--supervise"]]


def test_uninstall_removes_both_sign_in_entries_then_stops_the_app(monkeypatch):
    registry = FakeRegistry()
    registry.values[(processes.RUN_KEY, processes.RUN_VALUE)] = (winreg.REG_SZ, processes.quoted_command("--supervise"))
    calls = []
    monkeypatch.setattr(processes, "winreg", registry)
    monkeypatch.setattr(
        processes,
        "subprocess",
        SimpleNamespace(call=lambda command, **_kwargs: calls.append(command) or 0, DEVNULL=subprocess.DEVNULL),
    )
    monkeypatch.setattr(processes, "stop", lambda: calls.append("stop"))
    expected = [["schtasks", "/Delete", "/F", "/TN", task] for task in processes.LEGACY_TASKS] + ["stop"]
    real_uninstall()
    assert registry.values == {}
    assert calls == expected
    calls.clear()
    real_uninstall()  # the entry is already gone
    assert calls == expected


def test_the_supervisor_mutex_is_its_own():
    assert not processes.supervisor_running()
    assert processes.hold_mutex(processes.SUPERVISOR_MUTEX)
    try:
        assert processes.supervisor_running() and not processes.watcher_running()
    finally:
        processes.release_mutex(processes.SUPERVISOR_MUTEX)
    assert not processes.supervisor_running()


def test_the_all_day_supervisor_loads_no_third_party_packages():
    script = (
        "import sys; import granola_autostart.processes, granola_autostart.settings; "
        "print(sorted({m.split('.')[0] for m in sys.modules} & {'PIL', 'icalendar', 'pystray', 'webview', 'winrt', 'recurring_ical_events'}))"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=processes.ROOT, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "[]"


def test_the_watcher_supervisor_and_settings_each_have_their_own_mutex():
    assert len(set(REAL_MUTEXES)) == 3
