"""This app's own processes: the command for each mode, the named mutexes that keep one of each,
the sign-in entry, stopping, and the supervisor that restarts the watcher.

A PyInstaller build is one GranolaAutostart.exe that runs every mode; from source the package's
__main__.py does. The supervisor, which runs all day, needs only this module and settings.py, so both
stick to the standard library.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import time
import winreg
from ctypes import wintypes
from pathlib import Path

from .settings import app_dir, config_path

log = logging.getLogger("granola_autostart")
FROZEN = bool(getattr(sys, "frozen", False))
MAIN = Path(__file__).resolve().with_name("__main__.py")
ROOT = Path(sys.executable).resolve().parent if FROZEN else MAIN.parent.parent
WATCHER_MUTEX = "Local\\GranolaAutostart"
SUPERVISOR_MUTEX = "Local\\GranolaAutostartSupervisor"
SETTINGS_MUTEX = "Local\\GranolaAutostartSettings"
# The installer writes the same startup entry (packaging/installer.iss, [Registry]); keep the two alike.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GranolaAutostart"
# --install from a source checkout used to create these; --uninstall still removes them.
LEGACY_TASKS = ("Granola Autostart", "Granola Autostart Watchdog")
QUIT_EXIT_CODE = 3  # the watcher exits with this after Quit, so the supervisor stops instead of restarting it
RESTART_DELAY_SECONDS = 3
MAX_RESTART_DELAY_SECONDS = 300
HEALTHY_RUN_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_held: dict[str, int] = {}


def app_command(*args: str) -> list[str]:
    """Start this app again with ``args``: the packaged exe, or pythonw with the package's __main__.py."""
    if FROZEN:
        return [sys.executable, *args]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.exists() else sys.executable), str(MAIN), *args]


def notice_command(title: str, seconds: int, kind: str = "calendar") -> list[str]:
    # "--title=" keeps a title that starts with "-" from being read as an option.
    return app_command("--notice", f"--title={title}", f"--seconds={max(1, seconds)}", f"--kind={kind}")


def spawn(command: list[str]) -> None:
    try:
        subprocess.Popen(command, cwd=str(ROOT), close_fds=True)
    except OSError:
        mode = next((part for part in command if part.startswith("--")), Path(command[0]).name)
        log.exception("could not start %s", mode)


def start_supervisor() -> None:
    spawn(app_command("--supervise"))


def open_settings(section: str = "calendars") -> None:
    spawn(app_command("--settings", f"--section={section}"))


def open_setup() -> None:
    spawn(app_command("--setup"))


def open_log_folder() -> None:
    os.startfile(str(app_dir()))


def open_config_file() -> None:
    spawn(["notepad.exe", str(config_path())])


def hold_mutex(name: str) -> bool:
    """Create the named mutex and keep it while this process runs. False when another process has it."""
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        _kernel32.CloseHandle(handle)
        return False
    _held[name] = handle
    return True


def release_mutex(name: str) -> None:
    handle = _held.pop(name, None)
    if handle:
        _kernel32.CloseHandle(handle)


def mutex_exists(name: str) -> bool:
    handle = _kernel32.OpenMutexW(SYNCHRONIZE, False, name)
    if not handle:
        return False
    _kernel32.CloseHandle(handle)
    return True


def watcher_running() -> bool:
    return mutex_exists(WATCHER_MUTEX)


def supervisor_running() -> bool:
    """True while the supervisor runs, including the pause before it restarts a watcher that crashed."""
    return mutex_exists(SUPERVISOR_MUTEX)


def quoted_command(*extra: str) -> str:
    return " ".join([*(f'"{part}"' for part in app_command()), *extra])


def install() -> None:
    """Start at every sign-in through the user's startup list, as the installer does, and start now."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, quoted_command("--supervise"))
    # The new process takes the single-instance mutexes. This one must not.
    start_supervisor()


def uninstall() -> None:
    for name in LEGACY_TASKS:
        subprocess.call(["schtasks", "/Delete", "/F", "/TN", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass
    stop()


def _powershell_like(text: str) -> str:
    escaped = "".join(f"`{char}" if char in "[]*?`" else char for char in text)
    return escaped.replace("'", "''")


def stop_script() -> str:
    """PowerShell that stops the supervisor first, so it cannot restart the watcher, then the rest.

    Packaged, that is this copy of the exe only. From source, it is this checkout's processes, plus
    any started with ``python -m granola_autostart``, whose command line names no folder.
    """
    if FROZEN:
        # Only this copy of the exe, so an installer stopping the old version leaves a source checkout alone.
        exe = str(Path(sys.executable).resolve()).replace("'", "''")
        match = f"$_.ExecutablePath -eq '{exe}' -and $_.ProcessId -ne {os.getpid()}"
    else:
        script_path = _powershell_like(str(MAIN))
        match = (
            f"$_.Name -match 'python' "
            f"-and ($_.CommandLine -like '*{script_path}*' -or $_.CommandLine -like '*-m granola_autostart*') "
            "-and $_.CommandLine -notlike '*--uninstall*' -and $_.CommandLine -notlike '*--install*' "
            "-and $_.CommandLine -notlike '*--stop*'"
        )
    return (
        "$procs = Get-CimInstance Win32_Process | Where-Object { " + match + " }; "
        "$procs | Sort-Object { if ($_.CommandLine -like '*--supervise*') { 0 } else { 1 } } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )


def stop() -> None:
    subprocess.call(["powershell", "-NoProfile", "-Command", stop_script()], creationflags=CREATE_NO_WINDOW)


def supervise() -> int:
    """Run the watcher, and start it again after a crash, sooner after a long run. Quit ends it."""
    if not hold_mutex(SUPERVISOR_MUTEX):
        log.info("supervisor already running")
        return 0
    log.info("supervisor running")
    command = app_command()
    delay = RESTART_DELAY_SECONDS
    while True:
        started = time.monotonic()
        completed = subprocess.run(command, cwd=str(ROOT))
        if completed.returncode == QUIT_EXIT_CODE:
            log.info("watcher quit; supervisor stopping until the next sign-in")
            return 0
        quick = time.monotonic() - started < HEALTHY_RUN_SECONDS
        if not quick:
            delay = RESTART_DELAY_SECONDS
        log.info("watcher exited %s; restarting in %ss", completed.returncode, delay)
        time.sleep(delay)
        if quick:
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)
