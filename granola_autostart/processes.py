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
import re
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
# The installer asks whether the app is running by these two names (packaging/installer.iss, StopRunningApp).
WATCHER_MUTEX = "Local\\GranolaAutostart"
SUPERVISOR_MUTEX = "Local\\GranolaAutostartSupervisor"
SETTINGS_MUTEX = "Local\\GranolaAutostartSettings"
# Held while a countdown card starts and is on screen, by the watcher and by the card itself, so an update is never
# installed under one (updates.py).
CARD_MUTEX = "Local\\GranolaAutostartCard"
# Held while an update downloads and its installer starts, so the tray and a Settings window never install at once.
INSTALL_MUTEX = "Local\\GranolaAutostartInstall"
# The installer writes the same startup entry (packaging/installer.iss, [Registry]); keep the two alike.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GranolaAutostart"
# Windows' Startup Apps page and Task Manager's Startup tab keep their own on/off mark for each Run value, under the
# same name. Without a mark an entry is on. A mark that starts with an odd byte (01 or 03, then when it was switched
# off) is off, and Windows doesn't run the entry at sign-in.
STARTUP_APPROVED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
# --install from a source checkout used to create these; --uninstall still removes them.
LEGACY_TASKS = ("Granola Autostart", "Granola Autostart Watchdog")
QUIT_EXIT_CODE = 3  # the watcher exits with this after Quit, so the supervisor stops instead of restarting it
RESTART_DELAY_SECONDS = 3
MAX_RESTART_DELAY_SECONDS = 300
HEALTHY_RUN_SECONDS = 60
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

# Unicode's control characters and nothing else, so emoji joiners and no-break spaces stay in a title.
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")

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
    # A control character can't go in a command line (Python refuses a NUL, and the card would never start), and a line
    # break or tab has no place in a card's title, so they are dropped.
    title = _CONTROL_CHARACTERS.sub("", title)
    # "--title=" keeps a title that starts with "-" from being read as an option.
    return app_command("--notice", f"--title={title}", f"--seconds={max(1, seconds)}", f"--kind={kind}")


def spawn(command: list[str]) -> None:
    try:
        subprocess.Popen(command, cwd=str(ROOT), close_fds=True)
    except OSError:
        mode = next((part for part in command if part.startswith("--")), Path(command[0]).name)
        log.exception("could not start %s", mode)


def spawn_detached(command: list[str]) -> None:
    """Start ``command`` apart from this process, so it carries on when this app is stopped.

    The update installer stops every process of the app, this one included, and must outlive it. Unlike
    ``spawn``, a command that can't start raises OSError, so the caller can say so.
    """
    subprocess.Popen(
        command,
        cwd=str(Path(command[0]).parent),
        close_fds=True,
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
    )


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


def hold_mutex(name: str, *, shared: bool = False) -> bool:
    """Create the named mutex and keep it while this process runs. False when another process has it.

    With ``shared`` this process keeps its handle even when another has the mutex, so the mutex lasts until every
    holder has let go of it, and the answer is True whenever Windows gave a handle. The countdown card is held that
    way, by the watcher as it starts a card and by the card itself, and a second card may overlap the first."""
    ctypes.set_last_error(0)
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        return False
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS and not shared:
        _kernel32.CloseHandle(handle)
        return False
    release_mutex(name)  # holding one name twice in this process replaces the first handle, and doesn't leak it
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


def card_showing() -> bool:
    """True while a countdown card is on screen, whichever process opened it."""
    return mutex_exists(CARD_MUTEX)


def quoted_command(*extra: str) -> str:
    return " ".join([*(f'"{part}"' for part in app_command()), *extra])


def start_at_sign_in() -> bool:
    """Whether the app starts at sign-in, which is the switch in General: the sign-in entry exists, and Windows'
    Startup Apps hasn't switched it off. The installer keeps the entry as the user left it."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return False
    return not _switched_off_in_windows()


def _switched_off_in_windows() -> bool:
    """Whether Startup Apps (or Task Manager) has switched the sign-in entry off. A mark that isn't there, or isn't
    the binary value Windows writes, says nothing."""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY) as key:
            mark, kind = winreg.QueryValueEx(key, RUN_VALUE)
    except OSError:
        return False
    return kind == winreg.REG_BINARY and isinstance(mark, bytes) and bool(mark) and mark[0] % 2 == 1


def set_start_at_sign_in(on: bool) -> None:
    """Add the sign-in entry through the user's startup list, as the installer does, or remove it.

    Switching it on also deletes Windows' mark that switched it off, which Windows reads as on. Otherwise the switch
    in General would say on while Windows still skipped the entry."""
    if on:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, quoted_command("--supervise"))
        if _switched_off_in_windows():
            _delete_windows_mark()
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass  # already off


def _delete_windows_mark() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_APPROVED_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass  # gone already


def install() -> None:
    """Start at every sign-in, and start now."""
    set_start_at_sign_in(True)
    # The new process takes the single-instance mutexes. This one must not.
    start_supervisor()


def uninstall() -> None:
    for name in LEGACY_TASKS:
        subprocess.call(["schtasks", "/Delete", "/F", "/TN", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    set_start_at_sign_in(False)
    stop()


# PowerShell reads the plain single quote and these three curly ones (U+2018 to U+201B) as the same character, so a
# path with any of them ends a single-quoted string unless each is doubled.
_POWERSHELL_QUOTES = "'\u2018\u2019\u201a\u201b"


def _powershell_quoted(text: str) -> str:
    """``text`` for the inside of a single-quoted PowerShell string."""
    return "".join(char * 2 if char in _POWERSHELL_QUOTES else char for char in text)


def _powershell_like(text: str) -> str:
    escaped = "".join(f"`{char}" if char in "[]*?`" else char for char in text)
    return _powershell_quoted(escaped)


def stop_script() -> str:
    """PowerShell that stops the supervisor first, so it cannot restart the watcher, then the rest.

    Packaged, that is this copy of the exe only. From source, it is this checkout's processes, plus
    any started with ``python -m granola_autostart``, whose command line names no folder.

    Setup and the uninstaller stop an installed copy with a PowerShell of their own (packaging/installer.iss,
    StopAppProcesses), because the exe may be unable to run then; they match the same way, so keep the two alike.
    """
    if FROZEN:
        # Only this copy of the exe, so an installer stopping the old version leaves a source checkout alone.
        exe = _powershell_quoted(str(Path(sys.executable).resolve()))
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
        "$procs = Get-CimInstance Win32_Process -OperationTimeoutSec 10 | Where-Object { " + match + " }; "
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
