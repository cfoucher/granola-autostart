import logging
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
from granola_autostart.processes import set_start_at_sign_in as real_set_start_at_sign_in
from granola_autostart.processes import spawn_detached as real_spawn_detached
from granola_autostart.processes import start_at_sign_in as real_start_at_sign_in
from granola_autostart.processes import uninstall as real_uninstall

EXE = r"C:\Users\someone\AppData\Local\Programs\Granola Autostart\GranolaAutostart.exe"
# Captured at import, before conftest gives each test its own names.
REAL_MUTEXES = (processes.WATCHER_MUTEX, processes.SUPERVISOR_MUTEX, processes.SETTINGS_MUTEX, processes.CARD_MUTEX)


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


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("Stand\x00up", "Standup"),  # a NUL would end the command line, and Python refuses to start the card with one
        ("Weekly\nsync\r\n\there", "Weeklysynchere"),
        ("\x1b[31mRed\x1b[0m\x7f\x85", "[31mRed[0m"),
        ("\x00\x01\x02", ""),
        ("Café 🎉 – 日本語 \u200d\u00a0ok", "Café 🎉 – 日本語 \u200d\u00a0ok"),  # joiners, no-break spaces and the like stay
    ],
)
@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_control_characters_are_dropped_from_a_title_before_it_reaches_the_card(mode, title, shown, request):
    if mode == "packaged":
        request.getfixturevalue("packaged")
    command = processes.notice_command(title, 10)
    assert command[command.index("--notice") + 1] == f"--title={shown}"
    args = build_parser().parse_args(command[command.index("--notice") + 1 :])
    assert args.title == shown


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


# PowerShell reads the plain single quote and the three curly ones as the same character.
SINGLE_QUOTES = pytest.mark.parametrize(
    "quote", ["'", "‘", "’", "‚", "‛"], ids=["U+0027", "U+2018", "U+2019", "U+201A", "U+201B"]
)


@SINGLE_QUOTES
def test_a_quote_in_the_install_folder_cannot_break_the_stop_script(quote, packaged, monkeypatch):
    monkeypatch.setattr(sys, "executable", rf"C:\Users\o{quote}brien\Granola Autostart\GranolaAutostart.exe")
    assert f"-eq 'C:\\Users\\o{quote}{quote}brien\\Granola Autostart\\GranolaAutostart.exe'" in processes.stop_script()


@SINGLE_QUOTES
def test_a_quote_or_bracket_in_the_source_folder_cannot_break_the_stop_script(quote, monkeypatch):
    monkeypatch.setattr(processes, "MAIN", Path(rf"C:\src\[o{quote}brien]\granola_autostart\__main__.py"))
    assert rf"-like '*C:\src\`[o{quote}{quote}brien`]\granola_autostart\__main__.py*'" in processes.stop_script()


@pytest.mark.parametrize("mode", ["source", "packaged"])
def test_the_stop_script_gives_up_on_a_process_list_that_never_answers(mode, request):
    if mode == "packaged":
        request.getfixturevalue("packaged")
    assert "Get-CimInstance Win32_Process -OperationTimeoutSec 10 | Where-Object" in processes.stop_script()


def test_the_installer_stops_the_app_with_the_quoting_and_timeout_of_stop_script():
    # packaging/installer.iss (StopAppProcesses) is the same PowerShell for an installed copy; keep the two alike.
    installer = (processes.ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    for quote in processes._POWERSHELL_QUOTES[1:]:
        doubled = f"#${ord(quote):04X}#${ord(quote):04X}"
        assert doubled in installer, f"setup doesn't double U+{ord(quote):04X}"
    assert "Get-CimInstance Win32_Process -OperationTimeoutSec 10 | Where-Object" in installer


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
    assert "--stop" in completed.stdout  # listed with the other modes that the package main handles


@pytest.mark.parametrize(
    ("argv", "called"),
    [
        (["--notice", "--title=Standup"], [("notice", ["--title=Standup"])]),
        (["--settings", "--section=about"], [("settings", ["--section=about"])]),
        (["--setup"], [("settings", ["--setup"])]),
        (["--supervise"], [("logging", "supervisor-log.txt"), ("supervise", None)]),
        (["--stop"], [("stop", None), ("logging", "cli-log.txt")]),
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
    monkeypatch.setattr(settings, "setup_logging", lambda name: calls.append(("logging", name)) or logging.getLogger("test"))
    monkeypatch.setattr(processes, "supervise", lambda: calls.append(("supervise", None)) or 0)
    monkeypatch.setattr(processes, "stop", lambda: calls.append(("stop", None)))
    assert entry.main() == 0
    assert calls == called


def test_a_stop_that_cannot_run_is_logged_and_fails(monkeypatch):
    # Nothing shows an error from a windowless run, so the reason goes to the log and the exit code says it failed.
    logged = []

    def broken():
        raise FileNotFoundError(2, "powershell is missing")

    monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", "--stop"])
    monkeypatch.setattr(processes, "stop", broken)
    monkeypatch.setattr(settings, "setup_logging", lambda name: SimpleNamespace(exception=lambda message: logged.append((name, message))))
    assert entry.main() == 1
    assert logged == [("cli-log.txt", "could not stop the app")]


def test_stopping_loads_only_the_standard_library_and_not_the_watcher():
    # It is the mode likeliest to run on libraries that a failed setup left half-copied, so it can't need the watcher's.
    script = (
        "import sys; sys.argv = ['GranolaAutostart.exe', '--stop']; "
        "import granola_autostart.processes as processes; processes.stop = lambda: None; "
        "import granola_autostart.__main__ as entry; code = entry.main(); "
        "print(code, sorted({m.split('.')[0] for m in sys.modules} & {'PIL', 'icalendar', 'pystray', 'webview', 'winrt', 'recurring_ical_events'}), "
        "'granola_autostart.watcher' in sys.modules)"
    )
    completed = subprocess.run([sys.executable, "-c", script], cwd=processes.ROOT, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "0 [] False"


def test_a_named_mutex_has_one_owner_until_released():
    name = f"Local\\GranolaAutostartTest{os.getpid()}-{uuid.uuid4().hex}"
    assert not processes.mutex_exists(name)
    assert processes.hold_mutex(name)
    assert processes.mutex_exists(name)
    assert not processes.hold_mutex(name)
    processes.release_mutex(name)
    assert not processes.mutex_exists(name)


def test_a_shared_mutex_is_kept_even_when_another_process_has_it_and_lasts_until_both_let_go(held_elsewhere):
    name = f"Local\\GranolaAutostartTest{os.getpid()}-{uuid.uuid4().hex}"
    other_lets_go = held_elsewhere(name)
    assert not processes.hold_mutex(name)  # on its own it is the other's, and this process keeps nothing
    assert processes.hold_mutex(name, shared=True)
    other_lets_go()
    assert processes.mutex_exists(name)  # this process's handle keeps it
    processes.release_mutex(name)
    assert not processes.mutex_exists(name)


def test_a_shared_mutex_is_also_the_first_holders_when_nobody_has_it():
    name = f"Local\\GranolaAutostartTest{os.getpid()}-{uuid.uuid4().hex}"
    assert processes.hold_mutex(name, shared=True) and processes.mutex_exists(name)
    processes.release_mutex(name)
    assert not processes.mutex_exists(name)


def test_holding_one_mutex_twice_in_a_process_keeps_one_handle_for_release_to_close():
    name = f"Local\\GranolaAutostartTest{os.getpid()}-{uuid.uuid4().hex}"
    assert processes.hold_mutex(name, shared=True) and processes.hold_mutex(name, shared=True)
    processes.release_mutex(name)
    assert not processes.mutex_exists(name)  # a leaked first handle would still be holding it


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
    KEY_READ = winreg.KEY_READ
    REG_SZ = winreg.REG_SZ
    REG_BINARY = winreg.REG_BINARY

    def __init__(self, *, run_key: bool = True):
        self.values: dict[tuple[str, str], tuple[int, str | bytes]] = {}
        self.opened = []
        # The keys that exist: the Run key, unless the test says it is missing. Windows adds the other one only when
        # someone switches a startup entry off, so a test that wants Windows' mark asks for it (switched_off).
        self.keys = {processes.RUN_KEY} if run_key else set()

    def switched_off(self, first_byte: int = 3) -> None:
        """What Windows writes when a startup entry is switched off: four flag bytes, then when."""
        self.keys.add(processes.STARTUP_APPROVED_KEY)
        self.values[(processes.STARTUP_APPROVED_KEY, processes.RUN_VALUE)] = (winreg.REG_BINARY, bytes([first_byte, 0, 0, 0]) + bytes(range(8)))

    @contextmanager
    def OpenKey(self, root, path, _reserved=0, access=winreg.KEY_READ):
        if path not in self.keys:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        self.opened.append((root, path, access))
        yield path

    def QueryValueEx(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        kind, value = self.values[(key, name)]
        return value, kind

    def SetValueEx(self, key, name, _reserved, kind, value):
        self.values[(key, name)] = (kind, value)

    def DeleteValue(self, key, name):
        if (key, name) not in self.values:
            raise FileNotFoundError(2, "The system cannot find the file specified")
        del self.values[(key, name)]


def test_install_adds_the_sign_in_entry_and_starts_the_supervisor(packaged, monkeypatch, spawned):
    registry = FakeRegistry()
    monkeypatch.setattr(processes, "winreg", registry)
    monkeypatch.setattr(processes, "set_start_at_sign_in", real_set_start_at_sign_in)
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
    monkeypatch.setattr(processes, "set_start_at_sign_in", real_set_start_at_sign_in)
    expected = [["schtasks", "/Delete", "/F", "/TN", task] for task in processes.LEGACY_TASKS] + ["stop"]
    real_uninstall()
    assert registry.values == {}
    assert calls == expected
    calls.clear()
    real_uninstall()  # the entry is already gone
    assert calls == expected


def test_the_sign_in_switch_is_whether_the_run_value_exists(monkeypatch):
    registry = FakeRegistry()
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is False
    registry.values[(processes.RUN_KEY, processes.RUN_VALUE)] = (winreg.REG_SZ, "any command at all")
    assert real_start_at_sign_in() is True
    assert registry.opened == [(winreg.HKEY_CURRENT_USER, processes.RUN_KEY, winreg.KEY_READ)] * 2
    registry.values[(processes.RUN_KEY, "SomethingElse")] = (winreg.REG_SZ, "kept")
    del registry.values[(processes.RUN_KEY, processes.RUN_VALUE)]
    assert real_start_at_sign_in() is False


def test_a_run_key_that_cannot_be_opened_reads_as_off(monkeypatch):
    monkeypatch.setattr(processes, "winreg", FakeRegistry(run_key=False))
    assert real_start_at_sign_in() is False


def test_switching_sign_in_on_writes_the_supervisor_command_and_off_deletes_only_that_value(packaged, monkeypatch, spawned):
    registry = FakeRegistry()
    registry.values[(processes.RUN_KEY, "SomethingElse")] = (winreg.REG_SZ, "kept")
    monkeypatch.setattr(processes, "winreg", registry)
    real_set_start_at_sign_in(True)
    assert registry.values[(processes.RUN_KEY, processes.RUN_VALUE)] == (winreg.REG_SZ, f'"{EXE}" --supervise')
    assert registry.opened == [(winreg.HKEY_CURRENT_USER, processes.RUN_KEY, winreg.KEY_SET_VALUE)]
    assert real_start_at_sign_in() is True
    real_set_start_at_sign_in(False)
    assert registry.values == {(processes.RUN_KEY, "SomethingElse"): (winreg.REG_SZ, "kept")}
    assert real_start_at_sign_in() is False
    real_set_start_at_sign_in(False)  # already off
    assert spawned == []  # unlike install, switching it only changes the entry


def test_switching_sign_in_off_with_no_run_key_is_not_an_error(monkeypatch):
    monkeypatch.setattr(processes, "winreg", FakeRegistry(run_key=False))
    real_set_start_at_sign_in(False)


RUN_ENTRY = (processes.RUN_KEY, processes.RUN_VALUE)
WINDOWS_MARK = (processes.STARTUP_APPROVED_KEY, processes.RUN_VALUE)


@pytest.mark.parametrize("first_byte", [1, 3, 5, 7])
def test_an_entry_that_startup_apps_switched_off_reads_as_off(first_byte, monkeypatch):
    registry = FakeRegistry()
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "any command at all")
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is True
    registry.switched_off(first_byte)
    assert real_start_at_sign_in() is False
    assert (winreg.HKEY_CURRENT_USER, processes.STARTUP_APPROVED_KEY, winreg.KEY_READ) in registry.opened


@pytest.mark.parametrize(
    "mark",
    [
        (winreg.REG_BINARY, bytes([2, 0, 0, 0]) + bytes(8)),  # on, as Windows writes it when it switches one back on
        (winreg.REG_BINARY, bytes([6, 0, 0, 0]) + bytes(8)),
        (winreg.REG_BINARY, b""),
        (winreg.REG_SZ, "\x03"),  # not the binary value Windows writes
        (winreg.REG_BINARY, "\x03"),
    ],
)
def test_a_mark_that_is_not_off_leaves_the_entry_on(mark, monkeypatch):
    registry = FakeRegistry()
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "any command at all")
    registry.keys.add(processes.STARTUP_APPROVED_KEY)
    registry.values[WINDOWS_MARK] = mark
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is True


def test_the_mark_of_an_entry_that_is_not_there_says_nothing(monkeypatch):
    registry = FakeRegistry()
    registry.switched_off()  # Windows still has the mark of an entry that was removed
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is False
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "any command at all")
    del registry.values[WINDOWS_MARK]  # the key is there, and has no mark for this entry
    assert real_start_at_sign_in() is True


def test_another_entrys_mark_is_not_this_ones(monkeypatch):
    registry = FakeRegistry()
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "any command at all")
    registry.switched_off()
    registry.values[(processes.STARTUP_APPROVED_KEY, "SomethingElse")] = registry.values.pop(WINDOWS_MARK)
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is True


def test_switching_sign_in_on_clears_windows_mark_that_switched_it_off_and_nothing_else(packaged, monkeypatch):
    registry = FakeRegistry()
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "an old command")
    registry.switched_off()
    other_mark = (winreg.REG_BINARY, bytes([3, 0, 0, 0]) + bytes(8))
    registry.values[(processes.STARTUP_APPROVED_KEY, "SomethingElse")] = other_mark
    monkeypatch.setattr(processes, "winreg", registry)
    assert real_start_at_sign_in() is False
    real_set_start_at_sign_in(True)
    assert registry.values == {RUN_ENTRY: (winreg.REG_SZ, f'"{EXE}" --supervise'), (processes.STARTUP_APPROVED_KEY, "SomethingElse"): other_mark}
    assert (winreg.HKEY_CURRENT_USER, processes.STARTUP_APPROVED_KEY, winreg.KEY_SET_VALUE) in registry.opened
    assert real_start_at_sign_in() is True


def test_switching_sign_in_on_leaves_alone_a_mark_that_says_on_or_no_mark_at_all(packaged, monkeypatch):
    registry = FakeRegistry()
    on = (winreg.REG_BINARY, bytes([2, 0, 0, 0]) + bytes(8))
    registry.keys.add(processes.STARTUP_APPROVED_KEY)
    registry.values[WINDOWS_MARK] = on
    monkeypatch.setattr(processes, "winreg", registry)
    real_set_start_at_sign_in(True)
    assert registry.values[WINDOWS_MARK] == on
    assert all(access != winreg.KEY_SET_VALUE or path == processes.RUN_KEY for _root, path, access in registry.opened)
    del registry.values[WINDOWS_MARK]
    real_set_start_at_sign_in(True)  # the key is there, with no mark in it
    registry.keys.discard(processes.STARTUP_APPROVED_KEY)
    real_set_start_at_sign_in(True)  # and the key itself is missing
    assert real_start_at_sign_in() is True


def test_a_mark_that_goes_while_it_is_being_cleared_is_not_an_error(packaged, monkeypatch):
    class Racing(FakeRegistry):
        def DeleteValue(self, key, name):
            if key == processes.STARTUP_APPROVED_KEY:
                raise FileNotFoundError(2, "The system cannot find the file specified")  # Windows got there first
            super().DeleteValue(key, name)

    registry = Racing()
    registry.switched_off()
    monkeypatch.setattr(processes, "winreg", registry)
    real_set_start_at_sign_in(True)
    assert registry.values[RUN_ENTRY] == (winreg.REG_SZ, f'"{EXE}" --supervise')


def test_switching_sign_in_off_leaves_windows_mark_for_the_next_time_it_is_switched_on(packaged, monkeypatch):
    registry = FakeRegistry()
    registry.values[RUN_ENTRY] = (winreg.REG_SZ, "any command at all")
    registry.switched_off()
    monkeypatch.setattr(processes, "winreg", registry)
    real_set_start_at_sign_in(False)
    assert RUN_ENTRY not in registry.values and WINDOWS_MARK in registry.values
    real_set_start_at_sign_in(True)
    assert WINDOWS_MARK not in registry.values and real_start_at_sign_in() is True


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


def test_a_countdown_card_shows_through_its_own_mutex():
    assert not processes.card_showing()
    assert processes.hold_mutex(processes.CARD_MUTEX)
    try:
        assert processes.card_showing() and not processes.watcher_running() and not processes.supervisor_running()
    finally:
        processes.release_mutex(processes.CARD_MUTEX)
    assert not processes.card_showing()


def test_the_watcher_supervisor_settings_and_card_each_have_their_own_mutex():
    assert len(set(REAL_MUTEXES)) == 4


def test_a_detached_process_is_started_apart_from_this_one(monkeypatch):
    started = []
    installer = r"C:\Users\someone\AppData\Local\granola-autostart\updates\GranolaAutostart-1.2.0-setup.exe"
    monkeypatch.setattr(processes, "subprocess", SimpleNamespace(Popen=lambda command, **options: started.append((command, options))))
    real_spawn_detached([installer, "/VERYSILENT"])
    detached_and_own_group = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    assert started == [([installer, "/VERYSILENT"], {"cwd": str(Path(installer).parent), "close_fds": True, "creationflags": detached_and_own_group})]


def test_a_detached_process_that_cannot_start_raises_so_the_caller_can_say_so(monkeypatch):
    def missing(command, **options):
        raise FileNotFoundError(2, "The system cannot find the file specified")

    monkeypatch.setattr(processes, "subprocess", SimpleNamespace(Popen=missing))
    with pytest.raises(FileNotFoundError):
        real_spawn_detached([EXE])
