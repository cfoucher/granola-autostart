"""The watcher: each tick it checks presence, Zoom, and the calendars, then shows the countdown card when a
meeting starts and opens Granola. It also runs the tray icon and the command line for the other modes."""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import logging
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import pystray

from . import __version__, commands, processes
from .calendar_cache import CalendarCache, load_feeds
from .calendar_feed import merge_events
from .decide import Prompt, Snapshot, evaluate, apply_result
from .granola_launch import build_url, launch
from .icon import draw_icon
from .notify import Notifier, register_app_id
from .presence import idle_seconds, is_present, is_unlocked
from .settings import ConfigError, Settings, app_dir, config_path, load_or_defaults, load_settings, setup_logging, status_path
from .state_store import load_state, save_state
from .status import (
    AlertBook,
    LastStart,
    Problem,
    Status,
    TrayView,
    describe_error,
    end_of_today,
    find_problems,
    launch_problem_key,
    next_meeting,
    read_status,
    taking_notes,
    tray_view,
    write_status,
)
from .winui import per_monitor_dpi
from .zoom_meetings import granola_running, process_table, zoom_session, zoom_window_title

log = logging.getLogger("granola_autostart")
LAUNCH_CHECK_SECONDS = 30


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
        self.card_failed = False  # the last countdown card couldn't run
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

    def record_start(self, title: str, at: datetime) -> None:
        with self._lock:
            self.last_start = LastStart(title, at)

    def note_launch(self, launch) -> LastStart | None:
        """Mark the last start as opened once Granola is seen, or as failed once the launch check gave up."""
        with self._lock:
            last = self.last_start
            if last is not None and launch is not None and launch.launched_at == last.at:
                if launch.did_not_open:
                    last.granola_opened = False
                elif launch.opened:
                    last.granola_opened = True
            return dataclasses.replace(last) if last else None

    def show(self, view: TrayView) -> None:
        with self._lock:
            changed = view != self.view
            self.view = view
            callback = self.on_view
        if changed and callback is not None:
            callback(view)


def ask_notice(title: str, seconds: int, kind: str) -> bool | None:
    """True when the countdown ran out or Start now was clicked, False on Cancel, None when the card couldn't run."""
    try:
        completed = subprocess.run(processes.notice_command(title, seconds, kind), timeout=max(1, seconds) + 60)
    except subprocess.TimeoutExpired:
        log.error("countdown window did not close")
        return None
    except Exception:
        log.exception("countdown window failed to open")
        return None
    if completed.returncode in (0, 1):
        return completed.returncode == 0
    log.error("countdown window exited %s", completed.returncode)
    return None


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
        cache.invalidate(fresh.calendar_urls())
    control.settings = fresh
    log.info("settings reloaded")
    return fresh


class Reporter:
    """Each tick: write status.json, turn new problems into notifications, and update the tray."""

    def __init__(self, control: Control, notifier) -> None:
        self.control = control
        self.notifier = notifier
        self.book = AlertBook()

    def tick(self, now: datetime, settings: Settings, cache: CalendarCache, events, launch) -> None:
        control = self.control
        feeds = cache.feed_statuses(settings.calendar_urls())
        # Once a start's meeting is over, "Granola didn't open" has nothing left to fix (Try again won't act).
        current = launch if launch is not None and now < launch.expires_at else None
        problems = find_problems(control.config_error, feeds, current, card_failed=control.card_failed)
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
                taking_notes=taking_notes(launch, now),
                feeds=feeds,
                last_start=control.note_launch(launch),
                problems=problems,
            ),
        )


def run_loop(settings: Settings, control: Control, state_lock: threading.Lock, state, reporter: Reporter) -> None:
    cache = CalendarCache()
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
            present = is_present(settings.idle_seconds)
            events = cache.current(now, settings, refresh=present, eager=present and not was_present)
            was_present = present
            running = granola_running(table)
            title = zoom_window_title(table) if session_id else None
            with state_lock:
                prompt = evaluate(
                    now,
                    Snapshot(
                        present=present,
                        paused=control.paused(now),
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
                launch_now = dataclasses.replace(state.active_launch) if state.active_launch else None
                _save_state(state, control)
            try:
                reporter.tick(now, settings, cache, events, launch_now)
            except Exception:
                log.exception("could not report the status")
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
    answer = ask_notice(prompt.title, settings.countdown_seconds, prompt.kind)
    control.card_failed = answer is None
    # A card that couldn't run counts as nobody answering, which starts notes, so a broken card never
    # quietly skips meetings. Not for restarting notes after Granola closed, though: the user may have
    # closed it on purpose, and without a card nothing would let them say no.
    accepted = answer if answer is not None else prompt.kind != "crash"
    if answer is None:
        log.info("%s Granola for %s without the card", "starting" if accepted else "not restarting", prompt.title)
    if control.stop.is_set():
        return
    if accepted and control.paused(datetime.now().astimezone()):
        log.info("paused during the countdown; not starting Granola for %s", prompt.title)
        return
    if accepted and not still_present(settings):
        log.info("left the computer during the countdown; will ask again for %s", prompt.title)
        return
    if answer is False:
        log.info("cancelled Granola for %s", prompt.title)
    opened = accepted and open_granola(prompt.payload, prompt.title, dry_run=settings.dry_run)
    at = datetime.now().astimezone()
    with state_lock:
        apply_result(state, prompt, accepted, at)
        if accepted and settings.dry_run:
            # Answered like a real start, but nothing opened, so there is nothing to watch for.
            state.active_launch = None
        elif accepted and not opened:
            # Kept as a start that failed, so "Granola didn't open" comes up at once.
            state.active_launch.failed = True
        _save_state(state, control)
    if accepted and not settings.dry_run:
        control.record_start(prompt.title, at)


def _save_state(state, control: Control) -> None:
    """Save state.json. A file another program holds open (antivirus, backup) is logged, never fatal:
    the next save catches up, and nothing that depends on this tick is skipped."""
    try:
        save_state(state, control.current_pause())
    except OSError:
        log.exception("could not save state.json")


def open_granola(payload: dict, title: str, *, dry_run: bool) -> bool:
    try:
        launch(build_url(payload), dry_run=dry_run)
    except Exception:
        log.exception("could not start Granola for %s", title)
        return False
    if not dry_run:
        log.info("started Granola for %s", title)
    return True


def _warn_if_launch_missing(state, now: datetime) -> None:
    launch = state.active_launch
    if launch is None or launch.seen_running or launch.warned or launch.failed:
        return
    if now < launch.launched_at + timedelta(seconds=LAUNCH_CHECK_SECONDS):
        return
    launch.warned = True
    log.warning("Granola was not running %ss after the launch for %s", LAUNCH_CHECK_SECONDS, launch.title)


def retry_launch(state_lock: threading.Lock, state, control: Control, problem_key: str, *, dry_run: bool) -> None:
    """The notification's Try again. It does nothing for an older notification, a second click, a start
    that has opened since, or a meeting that has ended.

    Trying again is a new start: its own 30-second check, its own line in About, and its own
    "Granola didn't open" if the link fails again. In a dry run it is answered like one, with nothing to watch.
    """
    at = datetime.now().astimezone()
    with state_lock:
        launch = state.active_launch
        if launch is None or launch_problem_key(launch) != problem_key or not launch.did_not_open or at >= launch.expires_at:
            log.info("Try again was for a start that has moved on; nothing to retry")
            return
        if dry_run:
            state.active_launch = None
        else:
            launch.failed = launch.warned = launch.seen_running = False
            launch.missing_since = None
            launch.launched_at = at
    if not dry_run:
        control.record_start(launch.title, at)
    log.info("trying Granola again from the notification")
    if not open_granola(launch.payload, launch.title, dry_run=dry_run):
        with state_lock:
            if state.active_launch is launch:
                launch.failed = True


def save_app_icon() -> Path:
    """The icons Windows shows on notifications (PNG) and on windows run from source (ICO)."""
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    image = draw_icon(64)
    image.save(folder / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    image.save(folder / "app.png")
    return folder / "app.png"


def modern_menus() -> None:
    """A crisp Windows 11 tray menu that follows the light or dark app theme.

    Without DPI awareness Windows draws the menu at 100% and stretches it on scaled displays.
    SetPreferredAppMode and FlushMenuThemes (uxtheme ordinals 135 and 136) are undocumented but
    stable since Windows 10 1903; without them menus stay light.
    """
    per_monitor_dpi()
    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        set_preferred_app_mode(1)  # AllowDark: follow the Windows app theme
        uxtheme[136]()  # FlushMenuThemes
    except (AttributeError, OSError):
        pass


def run_tray(settings: Settings, config_error: str | None = None, *, dry_run: bool = False) -> int:
    """Run until Quit; returns the exit code. Settings can pause, resume, and quit through commands.py."""
    modern_menus()
    control = Control(settings, config_error)
    control.force_dry_run = dry_run
    control.last_start = read_status(status_path()).last_start
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
            retry_launch(state_lock, state, control, problem.key, dry_run=control.settings.dry_run)
        elif action == "logs":
            processes.open_log_folder()
        elif action == "config":
            processes.open_config_file()
        elif action in ("settings", "open"):
            processes.open_settings(problem.section)

    reporter = Reporter(control, Notifier(on_notification, balloon, logo=logo))
    worker = threading.Thread(
        target=run_loop,
        args=(settings, control, state_lock, state, reporter),
        name="granola-autostart",
        daemon=False,
    )
    worker.start()
    if not settings.calendar_urls() and not settings.setup_complete and config_error is None:
        processes.open_setup()

    def pause_until(until: datetime | None, label: str) -> None:
        control.set_pause(until)
        with state_lock:
            _save_state(state, control)
        log.info(label)

    def pause_hour() -> None:
        until = datetime.now().astimezone() + timedelta(hours=1)
        pause_until(until, f"Paused until {until:%H:%M}")

    def pause_today() -> None:
        pause_until(end_of_today(datetime.now().astimezone()), "Paused until tomorrow")

    def resume() -> None:
        pause_until(None, "Resumed")

    quitting = threading.Event()

    def quit_app() -> None:
        log.info("quitting")
        quitting.set()
        control.halt()
        tray["icon"].stop()

    def refresh(view: TrayView) -> None:
        icon = tray["icon"]
        icon.icon = draw_icon(64, view.kind)
        icon.title = view.tooltip
        icon.update_menu()

    def ready(icon: pystray.Icon) -> None:
        # Settings' Quit must work even if showing the icon fails.
        handlers = {"pause-hour": pause_hour, "pause-today": pause_today, "resume": resume, "quit": quit_app}
        threading.Thread(target=commands.listen, args=(handlers, control.stop), name="settings-commands", daemon=True).start()
        icon.visible = True
        control.on_view = refresh
        refresh(control.view)

    # Left-clicking the icon runs the first default item: Fix in Settings... when something is wrong, else Settings...
    menu = pystray.Menu(
        pystray.MenuItem(lambda _item: control.view.header, lambda *_: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Fix in Settings…",
            lambda *_: processes.open_settings(control.view.problem_section or "calendars"),
            default=True,
            visible=lambda _item: control.view.kind == "attention",
        ),
        pystray.MenuItem("Resume", lambda *_: resume(), visible=lambda _item: control.view.paused),
        pystray.MenuItem("Pause for 1 hour", lambda *_: pause_hour(), visible=lambda _item: not control.view.paused),
        pystray.MenuItem("Pause for today", lambda *_: pause_today()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Settings…",
            lambda *_: processes.open_settings("calendars"),
            default=True,
            visible=lambda _item: control.view.kind != "attention",
        ),
        pystray.MenuItem("Open log folder", lambda *_: processes.open_log_folder()),
        pystray.MenuItem("About", lambda *_: processes.open_settings("about")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda *_: quit_app()),
    )
    try:
        tray["icon"] = pystray.Icon("granola-autostart", draw_icon(64, control.view.kind), control.view.tooltip, menu=menu)
        tray["icon"].run(setup=ready)
    finally:
        # Even when the tray fails, the loop stops, so the process exits and the supervisor starts it again.
        control.halt()
        worker.join(timeout=control.settings.countdown_seconds + 15)
    return processes.QUIT_EXIT_CODE if quitting.is_set() else 0


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
        print(f"  {event.start:%Y-%m-%d %H:%M}  {event.summary}{'  (skipped)' if event.skipped else ''}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start Granola when a meeting begins. With no mode, run the watcher.",
        epilog="The other modes: --settings [--section=calendars|notes|about], --setup, and --notice --title=TITLE.",
    )
    modes = parser.add_mutually_exclusive_group()
    for flag, help_text in (
        ("--install", "start at every sign-in, and start now"),
        ("--uninstall", "remove the sign-in entry and stop the watcher"),
        ("--stop", "stop the running watcher and supervisor"),
        ("--check", "print presence, Zoom, and calendar status"),
        ("--preview-notice", "show the countdown once"),
    ):
        modes.add_argument(flag, dest="mode", action="store_const", const=flag[2:], help=help_text)
    parser.add_argument("--dry-run", action="store_true", help="show countdowns but do not launch Granola")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode is None and not processes.hold_mutex(processes.WATCHER_MUTEX):
        if sys.stdout is not None:
            print("another watcher is already running; quit it from its tray icon first")
        return 0
    logger = setup_logging("log.txt" if args.mode is None else "cli-log.txt")
    settings, config_error = load_or_defaults()
    if config_error:
        logger.error("%s; using defaults until it is fixed", config_error)
    if args.dry_run:
        settings.dry_run = True
    if args.mode == "preview-notice":
        from .notice import show_notice

        started = show_notice("Design review", settings.countdown_seconds)
        print("would start" if started else "cancelled")
        return 0
    if args.mode == "check":
        print_check(settings, config_error)
        return 1 if config_error else 0
    if args.mode == "uninstall":
        processes.uninstall()
        logger.info("granola autostart removed")
        return 0
    if args.mode == "stop":
        # The installer runs this before replacing files and before uninstalling.
        processes.stop()
        logger.info("granola autostart stopped")
        return 0
    if args.mode == "install":
        processes.install()
        logger.info("granola autostart installed")
        print("installed: it starts at every sign-in")
        return 0
    logger.info("granola autostart %s running (dry_run=%s)", __version__, settings.dry_run)
    return run_tray(settings, config_error, dry_run=args.dry_run)
