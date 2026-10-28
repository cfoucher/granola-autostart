"""Background watcher: calendar or Zoom, then a 10-second chance to cancel, then Granola."""

from __future__ import annotations

import argparse
import ctypes
import logging
from ctypes import wintypes
import subprocess
import sys
import threading
import time
import winreg
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw
import pystray

from calendar_feed import fetch_ics, events_between
from decide import Snapshot, evaluate, apply_result
from granola_launch import build_url, launch
from presence import idle_seconds, is_present
from settings import Settings, load_settings, setup_logging
from state_store import load_state, save_state
from zoom_meetings import granola_running, zoom_session, zoom_window_title

log = logging.getLogger("granola_autostart")
ROOT = Path(__file__).resolve().parent
NOTICE = ROOT / "notice.py"
TASK_LOGIN = "Granola Autostart"
TASK_WATCHDOG = "Granola Autostart Watchdog"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GranolaAutostart"
_MUTEX = None
_SUPERVISOR_MUTEX = None


class Control:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self._lock = threading.Lock()
        self.pause_until: datetime | None = None

    def set_pause(self, until: datetime | None) -> None:
        with self._lock:
            self.pause_until = until

    def current_pause(self) -> datetime | None:
        with self._lock:
            return self.pause_until

    def paused(self, now: datetime) -> bool:
        until = self.current_pause()
        return until is not None and now < until


class CalendarCache:
    def __init__(self) -> None:
        self.events = []
        self.fetched_at: datetime | None = None

    def current(self, now: datetime, settings: Settings) -> list:
        if not settings.ics_url:
            return []
        stale = self.fetched_at is None or (now - self.fetched_at).total_seconds() >= settings.calendar_refresh_seconds
        if stale:
            self._refresh(now, settings)
        return [event for event in self.events if event.end > now]

    def _refresh(self, now: datetime, settings: Settings) -> None:
        if not settings.ics_url.lower().startswith(("http://", "https://")):
            log.error("ics_url must be an http(s) address")
            self.fetched_at = now
            return
        try:
            text = fetch_ics(settings.ics_url)
            self.events = events_between(
                text,
                now - timedelta(hours=12),
                now + timedelta(hours=36),
                ignore_title_substrings=settings.ignore_title_substrings,
                user_email=settings.user_email,
            )
            self.fetched_at = now
            log.info("calendar feed has %s upcoming events", len(self.events))
        except Exception:
            log.exception("calendar fetch failed; keeping the previous list")
            self.fetched_at = now


def create_mutex(name: str) -> tuple[int, int]:
    """Return (handle, last_error). 183 means another instance already owns this name."""
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError.argtypes = [wintypes.DWORD]
    kernel32.SetLastError.restype = None
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, name)
    return int(handle or 0), int(kernel32.GetLastError())


def acquire_single_instance() -> bool:
    global _MUTEX
    handle, error = create_mutex("Local\\GranolaAutostart")
    _MUTEX = handle
    return error != 183


def ui_python() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return sys.executable


def ask_notice(title: str, seconds: int, kind: str) -> bool:
    command = [ui_python(), str(NOTICE), "--title", title, "--seconds", str(max(1, seconds)), "--kind", kind]
    try:
        completed = subprocess.run(command)
    except Exception:
        log.exception("countdown window failed to open")
        return False
    if completed.returncode == 0:
        return True
    if completed.returncode != 1:
        log.error("countdown window exited %s; not starting Granola", completed.returncode)
    return False


def run_loop(settings: Settings, control: Control, state_lock: threading.Lock, state) -> None:
    cache = CalendarCache()
    if not settings.ics_url:
        log.warning("No ICS url configured. Calendar meetings will not start notes. Zoom joins still will.")
    while not control.stop.is_set():
        try:
            now = datetime.now().astimezone()
            session_id, age = zoom_session()
            in_zoom = session_id is not None
            events = cache.current(now, settings)
            present = is_present(settings.idle_seconds)
            running = granola_running()
            title = zoom_window_title() if in_zoom else None
            with state_lock:
                prompt = evaluate(
                    now,
                    Snapshot(
                        present=present,
                        paused=control.paused(now),
                        in_zoom=in_zoom,
                        zoom_session_id=session_id,
                        zoom_age_seconds=age,
                        zoom_title=title,
                        events=events,
                        granola_running=running,
                    ),
                    state,
                    lookahead=timedelta(minutes=settings.zoom_lookahead_minutes),
                    fresh_zoom_seconds=settings.fresh_zoom_seconds,
                )
                _warn_if_launch_missing(state, now)
                save_state(state, control.current_pause())
            if prompt is not None and not control.stop.is_set():
                log.info("countdown for %s (%s)", prompt.title, prompt.kind)
                accepted = ask_notice(prompt.title, settings.countdown_seconds, prompt.kind)
                if control.stop.is_set():
                    break
                started = False
                if not accepted:
                    log.info("cancelled Granola for %s", prompt.title)
                elif settings.dry_run:
                    launch(build_url(prompt.payload), dry_run=True)
                else:
                    try:
                        launch(build_url(prompt.payload), dry_run=False)
                        started = True
                        log.info("started Granola for %s", prompt.title)
                    except Exception:
                        log.exception("could not start Granola for %s", prompt.title)
                with state_lock:
                    apply_result(state, prompt, started, datetime.now().astimezone())
                    save_state(state, control.current_pause())
        except Exception:
            log.exception("watcher tick failed")
        control.stop.wait(settings.poll_seconds)


def _warn_if_launch_missing(state, now: datetime) -> None:
    launch = state.active_launch
    if launch is None or launch.seen_running or launch.warned:
        return
    if now < launch.launched_at + timedelta(seconds=30):
        return
    launch.warned = True
    log.warning("Granola was not running 30s after the launch for %s", launch.title)


def tray_image() -> Image.Image:
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4, 4, 60, 60), fill=(125, 154, 106, 255))
    draw.ellipse((20, 20, 44, 44), fill=(255, 255, 255, 255))
    return image


def run_tray(settings: Settings) -> None:
    control = Control()
    state_lock = threading.Lock()
    state, saved_pause = load_state()
    if saved_pause is not None:
        control.set_pause(saved_pause)
    worker = threading.Thread(
        target=run_loop,
        args=(settings, control, state_lock, state),
        name="granola-autostart",
        daemon=False,
    )
    worker.start()

    def pause_until(until: datetime | None, icon: pystray.Icon, label: str) -> None:
        control.set_pause(until)
        with state_lock:
            save_state(state, control.current_pause())
        icon.title = label
        log.info(label)

    def on_hour(icon: pystray.Icon, _item) -> None:
        until = datetime.now().astimezone() + timedelta(hours=1)
        pause_until(until, icon, f"Paused until {until:%H:%M}")

    def on_today(icon: pystray.Icon, _item) -> None:
        now = datetime.now().astimezone()
        until = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        pause_until(until, icon, "Paused until tomorrow")

    def on_resume(icon: pystray.Icon, _item) -> None:
        pause_until(None, icon, "Granola autostart")

    def on_quit(icon: pystray.Icon, _item) -> None:
        log.info("quitting")
        control.stop.set()
        icon.stop()

    icon = pystray.Icon(
        "granola-autostart",
        tray_image(),
        "Granola autostart",
        menu=pystray.Menu(
            pystray.MenuItem("Pause for 1 hour", on_hour),
            pystray.MenuItem("Pause for today", on_today),
            pystray.MenuItem("Resume", on_resume),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", on_quit),
        ),
    )
    icon.run()
    control.stop.set()
    worker.join(timeout=settings.countdown_seconds + 15)


def acquire_supervisor() -> bool:
    global _SUPERVISOR_MUTEX
    handle, error = create_mutex("Local\\GranolaAutostartSupervisor")
    _SUPERVISOR_MUTEX = handle
    return error != 183


def quoted_command(*extra: str) -> str:
    parts = [f'"{ui_python()}"', f'"{Path(__file__).resolve()}"', *extra]
    return " ".join(parts)


def install_run_key() -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, quoted_command("--supervise"))


def remove_run_key() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except FileNotFoundError:
        pass


def try_scheduled_tasks() -> bool:
    command = f"{ui_python()} {Path(__file__).resolve()}"
    try:
        subprocess.check_call(
            ["schtasks", "/Create", "/F", "/TN", TASK_LOGIN, "/SC", "ONLOGON", "/TR", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.check_call(
            ["schtasks", "/Create", "/F", "/TN", TASK_WATCHDOG, "/SC", "MINUTE", "/MO", "1", "/TR", command],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except subprocess.CalledProcessError:
        for name in (TASK_LOGIN, TASK_WATCHDOG):
            subprocess.call(["schtasks", "/Delete", "/F", "/TN", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return False


def install_tasks() -> str:
    # The new process takes the single-instance mutex. This installer must not.
    if try_scheduled_tasks():
        subprocess.Popen([ui_python(), str(Path(__file__).resolve())], cwd=str(ROOT), close_fds=True)
        return "tasks"
    log.warning("Task Scheduler refused the install. Starting from the user startup list instead.")
    install_run_key()
    subprocess.Popen([ui_python(), str(Path(__file__).resolve()), "--supervise"], cwd=str(ROOT), close_fds=True)
    return "startup"


def stop_watchers() -> None:
    script = (
        "$procs = Get-CimInstance Win32_Process | Where-Object { "
        "$_.Name -match 'python' -and $_.CommandLine -like '*granola-autostart*watcher.py*' "
        "-and $_.CommandLine -notlike '*--uninstall*' }; "
        "$procs | Sort-Object { if ($_.CommandLine -like '*--supervise*') { 0 } else { 1 } } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )
    subprocess.call(["powershell", "-NoProfile", "-Command", script])


def uninstall_tasks() -> None:
    for name in (TASK_LOGIN, TASK_WATCHDOG):
        subprocess.call(["schtasks", "/Delete", "/F", "/TN", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    remove_run_key()
    stop_watchers()


def supervise() -> int:
    if not acquire_supervisor():
        log.info("supervisor already running")
        return 0
    log.info("supervisor running")
    python = ui_python()
    script = str(Path(__file__).resolve())
    while True:
        completed = subprocess.run([python, script], cwd=str(ROOT))
        log.info("watcher exited %s; restarting in 3s", completed.returncode)
        time.sleep(3)


def print_check(settings: Settings) -> None:
    now = datetime.now().astimezone()
    present = is_present(settings.idle_seconds)
    session_id, age = zoom_session()
    print(f"present: {present} (idle {idle_seconds():.0f}s, limit {settings.idle_seconds}s)")
    print(f"zoom: {'in meeting ' + session_id if session_id else 'not in a meeting'}")
    if age is not None:
        print(f"zoom age: {age:.0f}s")
    print(f"granola running: {granola_running()}")
    if not settings.ics_url:
        print("calendar: no ics_url in config")
        return
    events = events_between(
        fetch_ics(settings.ics_url),
        now - timedelta(hours=12),
        now + timedelta(hours=36),
        ignore_title_substrings=settings.ignore_title_substrings,
        user_email=settings.user_email,
    )
    upcoming = [event for event in events if event.end > now][:8]
    if not upcoming:
        print("calendar: no upcoming events in the feed")
        return
    print("calendar:")
    for event in upcoming:
        print(f"  {event.start:%Y-%m-%d %H:%M}  {event.summary}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Start Granola when a meeting begins")
    parser.add_argument("--install", action="store_true", help="start at logon and launch the watcher now")
    parser.add_argument("--uninstall", action="store_true", help="remove startup and stop the watcher")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="print presence, Zoom, and calendar status")
    parser.add_argument("--preview-notice", action="store_true", help="show the countdown once")
    parser.add_argument("--dry-run", action="store_true", help="show countdowns but do not launch Granola")
    args = parser.parse_args()
    logger = setup_logging()
    settings = load_settings()
    if args.dry_run:
        settings.dry_run = True
    if args.preview_notice:
        from notice import show_notice

        started = show_notice("Design review", settings.countdown_seconds)
        print("would start" if started else "cancelled")
        return 0
    if args.check:
        print_check(settings)
        return 0
    if args.supervise:
        return supervise()
    if args.uninstall:
        uninstall_tasks()
        logger.info("granola autostart removed")
        return 0
    if args.install:
        mode = install_tasks()
        logger.info("granola autostart installed (%s)", mode)
        print(f"installed via {mode}")
        return 0
    if not acquire_single_instance():
        logger.info("another watcher is already running")
        return 0
    logger.info("granola autostart running (dry_run=%s)", settings.dry_run)
    run_tray(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
