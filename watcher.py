"""Background watcher: calendar or Zoom, then a 10-second chance to cancel, then Granola."""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import logging
import os
from ctypes import wintypes
import subprocess
import sys
import threading
import time
import winreg
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw
import pystray

from app_status import (
    AlertBook,
    FeedStatus,
    LastStart,
    Problem,
    Status,
    TrayView,
    describe_error,
    feed_id,
    find_problems,
    next_meeting,
    read_status,
    tray_view,
    write_status,
)
from app_version import __version__
from calendar_feed import fetch_ics, events_between, feed_name, feed_owner, merge_events, my_addresses
from notify import Notifier, register_app_id
from decide import Prompt, Snapshot, evaluate, apply_result
from granola_launch import build_url, launch
from presence import idle_seconds, is_present, is_unlocked
from settings import ConfigError, Settings, app_dir, config_path, load_settings, setup_logging, status_path
from state_store import load_state, save_state
from zoom_meetings import granola_running, process_table, zoom_session, zoom_window_title

log = logging.getLogger("granola_autostart")
# A PyInstaller build is one GranolaAutostart.exe that runs every mode; from source each mode is a script.
FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
NOTICE = Path(__file__).resolve().parent / "notice.py"
TASK_LOGIN = "Granola Autostart"
TASK_WATCHDOG = "Granola Autostart Watchdog"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "GranolaAutostart"
ERROR_ALREADY_EXISTS = 183
CREATE_NO_WINDOW = 0x08000000
RESTART_DELAY_SECONDS = 3
MAX_RESTART_DELAY_SECONDS = 300
HEALTHY_RUN_SECONDS = 60
RETURN_REFRESH_SECONDS = 60
FAILED_FEED_RETRY_SECONDS = 60
STUCK_REFRESH_SECONDS = 600
THREAD_PRIORITY_BELOW_NORMAL = -1
_MUTEX = None
_SUPERVISOR_MUTEX = None


class Control:
    """Shared by the watcher loop and the tray: the pause, the current settings, and what the tray shows."""

    def __init__(self, settings: Settings | None = None, config_error: str | None = None) -> None:
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self.pause_until: datetime | None = None
        self.settings = settings or Settings()
        self.config_error = config_error
        self.force_dry_run = False
        self.last_start: LastStart | None = None
        self.last_payload: dict | None = None
        self.view = TrayView("watching", "Watching for meetings", False)
        self.on_view: Callable[[TrayView], None] | None = None

    def set_pause(self, until: datetime | None) -> None:
        with self._lock:
            self.pause_until = until
        self.wake.set()

    def current_pause(self) -> datetime | None:
        with self._lock:
            return self.pause_until

    def paused(self, now: datetime) -> bool:
        until = self.current_pause()
        return until is not None and now < until

    def halt(self) -> None:
        self.stop.set()
        self.wake.set()

    def sleep(self, seconds: float) -> None:
        """Wait for the next tick, or less when the tray changed something."""
        self.wake.wait(seconds)
        self.wake.clear()

    def record_start(self, title: str, payload: dict, at: datetime) -> None:
        with self._lock:
            self.last_start = LastStart(title, at)
            self.last_payload = payload

    def note_launch(self, launch) -> LastStart | None:
        """Mark the last start as opened once Granola is seen, or as failed once the launch check gave up."""
        with self._lock:
            last = self.last_start
            if last is not None and launch is not None and launch.launched_at == last.at:
                if launch.seen_running:
                    last.granola_opened = True
                elif launch.warned:
                    last.granola_opened = False
            return dataclasses.replace(last) if last else None

    def show(self, view: TrayView) -> None:
        with self._lock:
            changed = view != self.view
            self.view = view
            callback = self.on_view
        if changed and callback is not None:
            callback(view)


@dataclass
class FeedLoad:
    events: dict[str, list] = field(default_factory=dict)
    names: dict[str, str | None] = field(default_factory=dict)
    failures: list[tuple[int, str, Exception]] = field(default_factory=list)  # (1-based index, url, error)


def load_feeds(urls: list[str], now: datetime, settings: Settings, owners: dict[str, str | None]) -> FeedLoad:
    """Download every feed first, so each one's declines are checked against all of the user's addresses.

    ``owners`` holds each calendar's own address and is updated in place, so a feed that fails
    this time still lends the address it had last time.
    """
    texts: dict[str, str] = {}
    result = FeedLoad()
    for index, url in enumerate(urls, start=1):
        try:
            texts[url] = fetch_ics(url)
            owners[url] = feed_owner(texts[url])
            result.names[url] = feed_name(texts[url])
        except Exception as error:
            result.failures.append((index, url, error))
    addresses = my_addresses(settings.user_email, (owners.get(url) for url in urls))
    for index, url in enumerate(urls, start=1):
        if url not in texts:
            continue
        try:
            result.events[url] = events_between(
                texts[url],
                now - timedelta(hours=12),
                now + timedelta(hours=36),
                ignore_title_substrings=settings.ignore_title_substrings,
                user_emails=addresses,
            )
        except Exception as error:
            result.failures.append((index, url, error))
    return result


def lower_thread_priority() -> None:
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentThread.restype = wintypes.HANDLE
    kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
    kernel32.SetThreadPriority.restype = wintypes.BOOL
    kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)


class CalendarCache:
    """Feeds download on a background thread so a slow server never delays a Zoom prompt."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: list = []
        self._by_url: dict[str, list] = {}
        self._owners: dict[str, str | None] = {}
        self._status: dict[str, FeedStatus] = {}
        self._fetched_at: datetime | None = None
        self._refresh_started: float | None = None
        self._generation = 0

    def current(self, now: datetime, settings: Settings, *, refresh: bool = True, eager: bool = False) -> list:
        """Return cached events. Downloads only start when ``refresh`` is set, i.e. someone is at the PC."""
        if not settings.calendar_urls():
            return []
        limit = min(settings.calendar_refresh_seconds, RETURN_REFRESH_SECONDS) if eager else settings.calendar_refresh_seconds
        with self._lock:
            stale = self._fetched_at is None or (now - self._fetched_at).total_seconds() >= limit
            # A lookup that hangs past every socket timeout is abandoned rather than blocking refreshes forever.
            stuck = self._refresh_started is not None and time.monotonic() - self._refresh_started > STUCK_REFRESH_SECONDS
            if refresh and stale and (self._refresh_started is None or stuck):
                self._generation += 1
                self._refresh_started = time.monotonic()
                threading.Thread(
                    target=self._refresh,
                    args=(settings, self._generation),
                    name="calendar-refresh",
                    daemon=True,
                ).start()
            events = self._events
        return [event for event in events if event.end > now]

    def _refresh(self, settings: Settings, generation: int) -> None:
        try:
            lower_thread_priority()
            now = datetime.now().astimezone()
            urls = settings.calendar_urls()
            with self._lock:
                owners = dict(self._owners)
            loaded = load_feeds(urls, now, settings, owners)
            for index, _url, error in loaded.failures:
                log.error("calendar feed %s failed (%s); keeping its previous events", index, describe_error(error))
            fetched_at = now
            if loaded.failures:
                fetched_at = now - timedelta(seconds=max(0, settings.calendar_refresh_seconds - FAILED_FEED_RETRY_SECONDS))
            with self._lock:
                if generation != self._generation:
                    return
                self._owners = owners
                self._by_url.update(loaded.events)
                for _index, url, error in loaded.failures:
                    self._status.setdefault(url, FeedStatus(feed_id(url))).failed(error)
                for url, events in loaded.events.items():
                    self._status.setdefault(url, FeedStatus(feed_id(url))).succeeded(
                        now,
                        name=loaded.names.get(url),
                        owner=owners.get(url),
                        meetings_next_day=sum(1 for event in events if now <= event.start < now + timedelta(days=1)),
                    )
                merged = merge_events(*(self._by_url.get(url, []) for url in urls))
                self._events = merged
                self._fetched_at = fetched_at
            log.info("calendar feeds have %s upcoming events from %s calendars", len(merged), len(urls))
        except Exception as error:
            log.error("calendar refresh failed (%s)", type(error).__name__)
        finally:
            with self._lock:
                if generation == self._generation:
                    self._refresh_started = None

    def invalidate(self) -> None:
        """Download again on the next tick, because the calendars or their filters changed."""
        with self._lock:
            self._fetched_at = None

    def feed_statuses(self, urls: list[str]) -> list[FeedStatus]:
        with self._lock:
            return [dataclasses.replace(self._status[url]) if url in self._status else FeedStatus(feed_id(url)) for url in urls]


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
    return handle != 0 and error != ERROR_ALREADY_EXISTS


def ui_python() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    return sys.executable


def app_command(*args: str) -> list[str]:
    """Start this app again with ``args``: the packaged exe, or pythonw with this script."""
    if FROZEN:
        return [sys.executable, *args]
    return [ui_python(), str(Path(__file__).resolve()), *args]


def notice_command(title: str, seconds: int, kind: str) -> list[str]:
    # "--title=" keeps a title that starts with "-" from being read as an option.
    options = [f"--title={title}", f"--seconds={max(1, seconds)}", f"--kind={kind}"]
    if FROZEN:
        return [sys.executable, "--notice", *options]
    return [ui_python(), str(NOTICE), *options]


def ask_notice(title: str, seconds: int, kind: str) -> bool:
    try:
        completed = subprocess.run(notice_command(title, seconds, kind), timeout=max(1, seconds) + 60)
    except subprocess.TimeoutExpired:
        log.error("countdown window did not close; not starting Granola")
        return False
    except Exception:
        log.exception("countdown window failed to open")
        return False
    if completed.returncode == 0:
        return True
    if completed.returncode != 1:
        log.error("countdown window exited %s; not starting Granola", completed.returncode)
    return False


def config_stamp() -> tuple[int, int] | None:
    try:
        stat = config_path().stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def reload_settings(control: Control, current: Settings, cache: CalendarCache) -> Settings:
    """Pick up a saved config.json. A broken file keeps the last settings that worked."""
    try:
        fresh = load_settings()
    except ConfigError as error:
        log.error("%s; keeping the previous settings until it is fixed", error)
        control.config_error = str(error)
        return current
    control.config_error = None
    fresh.dry_run = fresh.dry_run or control.force_dry_run
    if (fresh.calendar_urls(), fresh.user_email, fresh.ignore_title_substrings) != (
        current.calendar_urls(),
        current.user_email,
        current.ignore_title_substrings,
    ):
        cache.invalidate()
    control.settings = fresh
    log.info("settings reloaded")
    return fresh


class Reporter:
    """Each tick: write status.json, turn new problems into notifications, and update the tray."""

    def __init__(self, control: Control, notifier=None) -> None:
        self.control = control
        self.notifier = notifier
        self.book = AlertBook()

    def tick(self, now: datetime, settings: Settings, cache: CalendarCache, events, launch) -> None:
        control = self.control
        feeds = cache.feed_statuses(settings.calendar_urls())
        problems = find_problems(control.config_error, feeds, launch)
        if self.notifier is not None:
            for problem in self.book.fresh(problems):
                self.notifier.show(problem)
        upcoming = next_meeting(events, now)
        pause = control.current_pause()
        control.show(tray_view(now, pause, problems, upcoming))
        write_status(
            status_path(),
            Status(
                version=__version__,
                paused_until=pause if pause is not None and now < pause else None,
                next_meeting=(upcoming.summary, upcoming.start) if upcoming else None,
                feeds=feeds,
                last_start=control.note_launch(launch),
                problems=problems,
                config_error=control.config_error,
            ),
        )


def run_loop(settings: Settings, control: Control, state_lock: threading.Lock, state, reporter: Reporter | None = None) -> None:
    cache = CalendarCache()
    reporter = reporter or Reporter(control)
    if not settings.calendar_urls():
        log.warning("No calendar feed configured. Calendar meetings will not start notes. Zoom joins still will.")
    stamp = config_stamp()
    was_present = False
    while not control.stop.is_set():
        try:
            now = datetime.now().astimezone()
            if config_stamp() != stamp:
                stamp = config_stamp()
                settings = reload_settings(control, settings, cache)
            table = process_table()
            session_id, age = zoom_session(table)
            in_zoom = session_id is not None
            present = is_present(settings.idle_seconds)
            events = cache.current(now, settings, refresh=present, eager=present and not was_present)
            was_present = present
            running = granola_running(table)
            title = zoom_window_title(table) if in_zoom else None
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
                launch_now = dataclasses.replace(state.active_launch) if state.active_launch else None
            reporter.tick(now, settings, cache, events, launch_now)
            if prompt is not None and not control.stop.is_set():
                handle_prompt(prompt, settings, control, state_lock, state)
        except Exception:
            log.exception("watcher tick failed")
        control.sleep(settings.poll_seconds)


def still_present(settings: Settings) -> bool:
    """The countdown itself adds idle time, so allow for it on top of the presence limit."""
    return is_unlocked() and idle_seconds() <= settings.idle_seconds + settings.countdown_seconds + 5


def handle_prompt(prompt: Prompt, settings: Settings, control: Control, state_lock: threading.Lock, state) -> None:
    log.info("countdown for %s (%s)", prompt.title, prompt.kind)
    accepted = ask_notice(prompt.title, settings.countdown_seconds, prompt.kind)
    if control.stop.is_set():
        return
    if accepted and control.paused(datetime.now().astimezone()):
        log.info("paused during the countdown; not starting Granola for %s", prompt.title)
        return
    if accepted and not still_present(settings):
        log.info("left the computer during the countdown; will ask again for %s", prompt.title)
        return
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
    at = datetime.now().astimezone()
    with state_lock:
        apply_result(state, prompt, started, at)
        save_state(state, control.current_pause())
    if started:
        control.record_start(prompt.title, prompt.payload, at)


def _warn_if_launch_missing(state, now: datetime) -> None:
    launch = state.active_launch
    if launch is None or launch.seen_running or launch.warned:
        return
    if now < launch.launched_at + timedelta(seconds=30):
        return
    launch.warned = True
    log.warning("Granola was not running 30s after the launch for %s", launch.title)


GREEN = (90, 138, 71, 255)  # the countdown ring's colour
GREY = (124, 119, 109, 255)
AMBER = (208, 112, 31, 255)
WHITE = (255, 255, 255, 255)


def tray_image(kind: str = "watching") -> Image.Image:
    """Green circle while watching, grey with pause bars while paused, and an amber dot when something is wrong."""
    s = 4  # drawn large, then reduced, for smooth edges
    image = Image.new("RGBA", (64 * s, 64 * s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((4 * s, 4 * s, 60 * s, 60 * s), fill=GREY if kind == "paused" else GREEN)
    if kind == "paused":
        draw.rounded_rectangle((22 * s, 19 * s, 29 * s, 45 * s), radius=2 * s, fill=WHITE)
        draw.rounded_rectangle((35 * s, 19 * s, 42 * s, 45 * s), radius=2 * s, fill=WHITE)
    else:
        draw.ellipse((21 * s, 21 * s, 43 * s, 43 * s), fill=WHITE)
    if kind == "attention":
        draw.ellipse((37 * s, 0, 64 * s, 27 * s), fill=WHITE)
        draw.ellipse((40 * s, 3 * s, 61 * s, 24 * s), fill=AMBER)
    return image.resize((64, 64), Image.LANCZOS)


def save_app_icon() -> Path:
    """The icons Windows shows on notifications (PNG) and on windows run from source (ICO)."""
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    tray_image().save(folder / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    tray_image().save(folder / "app.png")
    return folder / "app.png"


SETTINGS_SCRIPT = Path(__file__).resolve().parent / "settings_app.py"


def settings_command(*args: str) -> list[str]:
    if FROZEN:
        return [sys.executable, "--settings", *args]
    return [ui_python(), str(SETTINGS_SCRIPT), *args]


def _spawn(command: list[str]) -> None:
    try:
        subprocess.Popen(command, cwd=str(ROOT), close_fds=True)
    except OSError:
        log.exception("could not start %s", command[1:2])


def open_settings(section: str = "calendars") -> None:
    _spawn(settings_command(f"--section={section}"))


def open_setup() -> None:
    _spawn(settings_command("--setup"))


def open_log_folder() -> None:
    os.startfile(str(app_dir()))


def open_config_file() -> None:
    _spawn(["notepad.exe", str(config_path())])


def retry_launch(control: Control) -> None:
    payload = control.last_payload
    if payload is None:
        return
    log.info("trying Granola again from the notification")
    launch(build_url(payload), dry_run=control.settings.dry_run)


def run_tray(settings: Settings, config_error: str | None = None, *, dry_run: bool = False) -> None:
    control = Control(settings, config_error)
    control.force_dry_run = dry_run
    control.last_start = LastStart.from_json(read_status(status_path()).get("last_start"))
    logo = None
    try:
        logo = save_app_icon()
        register_app_id(logo)
    except OSError as error:
        log.warning("could not register for notifications (%s)", type(error).__name__)
    state_lock = threading.Lock()
    state, saved_pause = load_state()
    if saved_pause is not None:
        control.set_pause(saved_pause)
    tray: dict[str, pystray.Icon] = {}

    def balloon(title: str, message: str) -> None:
        if "icon" in tray:
            tray["icon"].notify(message, title)

    def on_notification(action: str, problem: Problem) -> None:
        if action == "retry":
            retry_launch(control)
        elif action == "logs":
            open_log_folder()
        elif action == "config":
            open_config_file()
        elif action in ("settings", "open"):
            open_settings(problem.section)

    reporter = Reporter(control, Notifier(on_notification, balloon, logo=logo))
    worker = threading.Thread(
        target=run_loop,
        args=(settings, control, state_lock, state, reporter),
        name="granola-autostart",
        daemon=False,
    )
    worker.start()
    if not settings.calendar_urls() and not settings.setup_complete and config_error is None:
        open_setup()

    def pause_until(until: datetime | None, label: str) -> None:
        control.set_pause(until)
        with state_lock:
            save_state(state, control.current_pause())
        log.info(label)

    def on_hour(_icon, _item) -> None:
        until = datetime.now().astimezone() + timedelta(hours=1)
        pause_until(until, f"Paused until {until:%H:%M}")

    def on_today(_icon, _item) -> None:
        now = datetime.now().astimezone()
        pause_until((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0), "Paused until tomorrow")

    def on_resume(_icon, _item) -> None:
        pause_until(None, "Resumed")

    def on_quit(icon: pystray.Icon, _item) -> None:
        log.info("quitting")
        control.halt()
        icon.stop()

    def refresh(view: TrayView) -> None:
        icon = tray["icon"]
        icon.icon = tray_image(view.kind)
        icon.title = view.tooltip
        icon.update_menu()

    def ready(icon: pystray.Icon) -> None:
        icon.visible = True
        control.on_view = refresh
        refresh(control.view)

    # Left-clicking the icon runs the first default item: Fix in Settings... when something is wrong, else Settings...
    menu = pystray.Menu(
        pystray.MenuItem(lambda _item: control.view.header, lambda *_: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Fix in Settings…",
            lambda *_: open_settings(control.view.problem_section or "calendars"),
            default=True,
            visible=lambda _item: control.view.kind == "attention",
        ),
        pystray.MenuItem("Resume", on_resume, visible=lambda _item: control.view.paused),
        pystray.MenuItem("Pause for 1 hour", on_hour, visible=lambda _item: not control.view.paused),
        pystray.MenuItem("Pause for today", on_today),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Settings…",
            lambda *_: open_settings("calendars"),
            default=True,
            visible=lambda _item: control.view.kind != "attention",
        ),
        pystray.MenuItem("Open log folder", lambda *_: open_log_folder()),
        pystray.MenuItem("About", lambda *_: open_settings("about")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit),
    )
    tray["icon"] = pystray.Icon("granola-autostart", tray_image(control.view.kind), control.view.tooltip, menu=menu)
    tray["icon"].run(setup=ready)
    control.halt()
    worker.join(timeout=control.settings.countdown_seconds + 15)


def acquire_supervisor() -> bool:
    global _SUPERVISOR_MUTEX
    handle, error = create_mutex("Local\\GranolaAutostartSupervisor")
    _SUPERVISOR_MUTEX = handle
    return handle != 0 and error != ERROR_ALREADY_EXISTS


def quoted_command(*extra: str) -> str:
    return " ".join([*(f'"{part}"' for part in app_command()), *extra])


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
    command = quoted_command()
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
        remove_run_key()
        subprocess.Popen(app_command(), cwd=str(ROOT), close_fds=True)
        return "tasks"
    log.warning("Task Scheduler refused the install. Starting from the user startup list instead.")
    install_run_key()
    subprocess.Popen(app_command("--supervise"), cwd=str(ROOT), close_fds=True)
    return "startup"


def _powershell_like(text: str) -> str:
    escaped = "".join(f"`{char}" if char in "[]*?`" else char for char in text)
    return escaped.replace("'", "''")


def stop_script() -> str:
    """PowerShell that stops this install's supervisor first, so it cannot restart the watcher, then the rest."""
    if FROZEN:
        # Only this copy of the exe, so an installer stopping the old version leaves a source checkout alone.
        exe = str(Path(sys.executable).resolve()).replace("'", "''")
        match = f"$_.ExecutablePath -eq '{exe}' -and $_.ProcessId -ne {os.getpid()}"
    else:
        script_path = _powershell_like(str(Path(__file__).resolve()))
        match = (
            f"$_.Name -match 'python' -and $_.CommandLine -like '*{script_path}*' "
            "-and $_.CommandLine -notlike '*--uninstall*' -and $_.CommandLine -notlike '*--install*' "
            "-and $_.CommandLine -notlike '*--stop*'"
        )
    return (
        "$procs = Get-CimInstance Win32_Process | Where-Object { " + match + " }; "
        "$procs | Sort-Object { if ($_.CommandLine -like '*--supervise*') { 0 } else { 1 } } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
    )


def stop_watchers() -> None:
    subprocess.call(["powershell", "-NoProfile", "-Command", stop_script()], creationflags=CREATE_NO_WINDOW)


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
    command = app_command()
    delay = RESTART_DELAY_SECONDS
    while True:
        started = time.monotonic()
        completed = subprocess.run(command, cwd=str(ROOT))
        quick = time.monotonic() - started < HEALTHY_RUN_SECONDS
        if not quick:
            delay = RESTART_DELAY_SECONDS
        log.info("watcher exited %s; restarting in %ss", completed.returncode, delay)
        time.sleep(delay)
        if quick:
            delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)


def print_check(settings: Settings, config_error: str | None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if config_error:
        print(f"config: {config_error} (using defaults)")
    now = datetime.now().astimezone()
    present = is_present(settings.idle_seconds)
    session_id, age = zoom_session()
    print(f"present: {present} (idle {idle_seconds():.0f}s, limit {settings.idle_seconds}s)")
    print(f"zoom: {'in meeting ' + session_id if session_id else 'not in a meeting'}")
    if age is not None:
        print(f"zoom age: {age:.0f}s")
    print(f"granola running: {granola_running()}")
    urls = settings.calendar_urls()
    if not urls:
        print("calendar: no calendar feed in config")
        return
    loaded = load_feeds(urls, now, settings, {})
    for index, _url, error in loaded.failures:
        print(f"calendar feed {index}: failed ({describe_error(error)})")
    events = merge_events(*loaded.events.values())
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
    parser.add_argument("--stop", action="store_true", help="stop the running watcher and supervisor")
    parser.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--check", action="store_true", help="print presence, Zoom, and calendar status")
    parser.add_argument("--preview-notice", action="store_true", help="show the countdown once")
    parser.add_argument("--dry-run", action="store_true", help="show countdowns but do not launch Granola")
    args = parser.parse_args()
    watching = not (args.supervise or args.install or args.uninstall or args.stop or args.check or args.preview_notice)
    if watching and not acquire_single_instance():
        if sys.stdout is not None:
            print("another watcher is already running; run --uninstall first to test with --dry-run")
        return 0
    if args.supervise:
        log_name = "supervisor-log.txt"
    elif watching:
        log_name = "log.txt"
    else:
        log_name = "cli-log.txt"
    logger = setup_logging(log_name)
    config_error = None
    try:
        settings = load_settings()
    except ConfigError as error:
        config_error = str(error)
        settings = Settings()
        if not args.supervise:
            logger.error("%s; using defaults until it is fixed", config_error)
    if args.dry_run:
        settings.dry_run = True
    if args.preview_notice:
        from notice import show_notice

        started = show_notice("Design review", settings.countdown_seconds)
        print("would start" if started else "cancelled")
        return 0
    if args.check:
        print_check(settings, config_error)
        return 1 if config_error else 0
    if args.supervise:
        return supervise()
    if args.uninstall:
        uninstall_tasks()
        logger.info("granola autostart removed")
        return 0
    if args.stop:
        # The installer runs this before replacing files and before uninstalling.
        stop_watchers()
        logger.info("granola autostart stopped")
        return 0
    if args.install:
        mode = install_tasks()
        logger.info("granola autostart installed (%s)", mode)
        print(f"installed via {mode}")
        return 0
    logger.info("granola autostart %s running (dry_run=%s)", __version__, settings.dry_run)
    run_tray(settings, config_error, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
