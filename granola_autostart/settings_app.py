"""Settings and first-run setup, in an Edge WebView2 window (pywebview). The page itself is web/.

    python -m granola_autostart --settings [--section=calendars|notes|about]   Settings
    python -m granola_autostart --setup                                         first-run setup

Changes are saved to config.json as they are made; the running watcher reloads it. Only one of
these windows is open at a time: opening another brings the first one forward.
"""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import logging
import os
import re
import time
import urllib.error
import urllib.parse
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from . import __version__, commands, processes
from .calendar_feed import (
    events_between,
    feed_name,
    feed_owner,
    fetch_ics,
    meetings_next_day,
    my_addresses,
    normalize_feed_url,
    skip_word,
)
from .settings import ConfigError, Settings, app_dir, load_or_defaults, load_settings, save_settings, setup_logging, status_path
from .status import FeedStatus, Status, describe_error, feed_id, read_status, until_tomorrow, when
from .winui import app_theme, set_window_attribute

log = logging.getLogger("granola_autostart")
# Beside this module from source and in the packaged build's _internal folder alike.
WEB = Path(__file__).resolve().with_name("web")
TITLES = {"settings": "Granola Autostart settings", "setup": "Set up Granola Autostart"}
SIZES = {"settings": (796, 660), "setup": (656, 680)}
COUNTDOWN_CHOICES = (5, 10, 15, 20, 30)
IDLE_CHOICES = (1, 2, 5, 10, 15)  # minutes
SECTIONS = ("calendars", "notes", "about")
STARTING_SECONDS = 30  # after this window starts the watcher, how long "Starting…" shows before it counts as not running
RESTARTING_SECONDS = 30  # a supervisor with no watcher for longer is restarting one that keeps failing
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def ago(moment: datetime, now: datetime) -> str:
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    return f"on {moment.astimezone(now.tzinfo):%a %d %b}"


def provider(url: str) -> str:
    host = (urllib.parse.urlsplit(url).hostname or "").casefold()
    if host.endswith("google.com"):
        return "Google Calendar"
    if "outlook" in host or "office" in host or "live.com" in host:
        return "Outlook"
    return host or "Calendar"


def calendar_line(feed: FeedStatus | None, now: datetime) -> tuple[str, str]:
    """(state, text) for a calendar row: ok, pending, or warn."""
    if feed is not None and feed.failures:
        return "warn" if feed.alerting else "pending", feed.failure_detail()
    if feed is None or feed.last_ok is None:
        return "pending", "Checking… The watcher downloads it within a minute."
    count = feed.meetings_next_day or 0
    return "ok", f"{count} meeting{'s' if count != 1 else ''} in the next day · checked {ago(feed.last_ok, now)}"


def summarize_feed(text: str, settings: Settings, known_owners: list[str], now: datetime) -> dict:
    """What a Test shows: the calendar's name, how many meetings in the next day, and the next few.

    It filters the way the watcher does, so Test agrees with what the watcher will start notes for.
    """
    if "BEGIN:VCALENDAR" not in text[:4096]:
        return {"ok": False, "error": "That address didn't return a calendar. Check you copied the secret address in iCal format."}
    owner = feed_owner(text)
    words = settings.ignore_title_substrings
    addresses = my_addresses(settings.user_email, [owner, *known_owners])
    events = events_between(text, now, now + timedelta(days=1), ignore_title_substrings=words, user_emails=addresses, keep_skipped=True)
    events = [event for event in events if event.start >= now]
    meetings = [{"time": when(event.start, now), "title": event.summary, "skipped": _skip_reason(event, words)} for event in events[:4]]
    return {"ok": True, "name": feed_name(text) or "Calendar", "owner": owner, "count": meetings_next_day(events, now), "meetings": meetings}


def _skip_reason(event, words) -> str | None:
    if not event.skipped:
        return None
    word = skip_word(event.summary, words)  # None when the word sat past where the title was shortened
    return f"Skipped, title contains “{word}”" if word else "Skipped by a skip word"


class Api:
    """Called from web/app.js as window.pywebview.api.<name>(...). Every method returns plain JSON data."""

    def __init__(self, page: str, section: str) -> None:
        self.page = page
        self.section = section if section in SECTIONS else "calendars"
        # Private: pywebview walks every public attribute to expose it to the page, and the window's
        # .NET object graph would send it into endless recursion.
        self._window = None
        self._started_at: float | None = None  # time.time() when this window last started the watcher
        self._waiting_since: float | None = None  # time.monotonic() when a supervisor was first seen with no watcher

    # Reading

    def load(self) -> dict:
        now = datetime.now().astimezone()
        status = read_status(status_path())
        settings, config_error = load_or_defaults()
        running = processes.watcher_running()
        waiting = not running and processes.supervisor_running()
        if not waiting:
            self._waiting_since = None
        elif self._waiting_since is None:
            self._waiting_since = time.monotonic()
        return {
            "page": self.page,
            "section": self.section,
            "theme": app_theme(),
            "version": __version__,
            "config_error": config_error,
            "settings": None if config_error else self._settings_view(settings, status.feeds, now),
            "status": self._status_view(status, running, now, waiting=time.monotonic() - self._waiting_since if waiting else None),
        }

    def _settings_view(self, settings: Settings, feeds: list[FeedStatus], now: datetime) -> dict:
        by_id = {feed.id: feed for feed in feeds}
        calendars = []
        for url in settings.calendar_urls():
            feed = by_id.get(feed_id(url))
            state, line = calendar_line(feed, now)
            calendars.append(
                {
                    "id": feed_id(url),
                    "name": (feed.name if feed else None) or "New calendar",
                    "provider": provider(url),
                    "state": state,
                    "line": line,
                }
            )
        idle_minutes = min(IDLE_CHOICES, key=lambda choice: abs(choice * 60 - settings.idle_seconds))
        return {
            "calendars": calendars,
            "detected_addresses": _owners(feeds),
            "user_email": settings.user_email,
            "countdown_seconds": settings.countdown_seconds,
            "countdown_choices": list(COUNTDOWN_CHOICES),
            "idle_minutes": idle_minutes,
            "idle_choices": list(IDLE_CHOICES),
            "skip_words": list(settings.ignore_title_substrings),
        }

    def _starting(self) -> bool:
        """Just started, and the new watcher hasn't written its first status yet."""
        if self._started_at is None or time.time() - self._started_at > STARTING_SECONDS:
            return False
        try:
            return status_path().stat().st_mtime < self._started_at
        except OSError:
            return True

    def _status_view(self, status: Status, running: bool, now: datetime, *, waiting: float | None = None) -> dict:
        """The side panel's status box. Its buttons (see web/app.js) follow the state, like the tray menu.

        ``waiting``: for how many seconds the supervisor has been up with no watcher. Briefly, that is the
        watcher starting (at sign-in); for longer, the supervisor is restarting one that keeps failing.
        """
        problems = status.problems
        paused_until = status.paused_until
        paused = bool(running and paused_until and now < paused_until)
        today = bool(paused and until_tomorrow(paused_until, now))
        last = status.last_start
        if self._starting():
            state, detail = "starting", "Checking your calendars."
        elif waiting is not None and waiting < RESTARTING_SECONDS:
            state, detail = "starting", "Starting up."
        elif waiting is not None:
            state, detail = "starting", "Restarting after a problem. The log folder says why."
        elif not running:
            state, detail = "stopped", "Meetings won't start notes."
        elif problems:
            state, detail = "attention", problems[0].title
        elif paused and today:
            state, detail = "paused", "Until tomorrow. Zoom calls won't be asked about later."
        elif paused:
            state, detail = "paused", f"Until {when(paused_until, now)}. No countdown cards until then."
        elif status.taking_notes:
            title, since = status.taking_notes
            state, detail = "notes", f"{title}, since {when(since, now)}"
        elif status.next_meeting:
            title, start = status.next_meeting
            state, detail = "watching", f"Next: {title} at {when(start, now)}"
        else:
            state, detail = "watching", "No meetings coming up. Zoom calls still start notes."
        return {
            "running": running,
            "state": state,
            "detail": detail,
            "paused": paused,
            "paused_today": today,
            "fix_section": problems[0].section if problems else None,
            "last_start": {"title": last.title, "when": when(last.at, now), "opened": last.granola_opened} if last else None,
        }

    # Calendars

    def test_address(self, url: str) -> dict:
        try:
            cleaned = normalize_feed_url(url)
        except ValueError:
            return {"ok": False, "error": "Calendar addresses start with https:// (or webcal://)."}
        settings, _error = load_or_defaults()
        try:
            text = fetch_ics(cleaned, timeout=20)
        except urllib.error.HTTPError as error:
            return {"ok": False, "error": f"Couldn't read that address ({describe_error(error)}). Check you copied all of it."}
        except Exception as error:
            return {"ok": False, "error": f"Couldn't download that address ({describe_error(error)})."}
        try:
            return summarize_feed(text, settings, _owners(read_status(status_path()).feeds), datetime.now().astimezone())
        except Exception as error:
            log.warning("test could not read the calendar (%s)", type(error).__name__)
            return {"ok": False, "error": "That address returned a calendar this app couldn't read."}

    def test_calendar(self, calendar_id: str) -> dict:
        url = self._url_for(calendar_id)
        return self.test_address(url) if url else {"ok": False, "error": "That calendar was removed."}

    def add_calendar(self, url: str) -> dict:
        cleaned = normalize_feed_url(url)
        settings = load_settings()
        if cleaned in settings.calendar_urls():
            return self.load()
        log.info("calendar added from Settings")
        return self._save(settings, ics_urls=(*settings.calendar_urls(), cleaned))

    def remove_calendar(self, calendar_id: str) -> dict:
        settings = load_settings()
        log.info("calendar removed from Settings")
        return self._save(settings, ics_urls=tuple(url for url in settings.calendar_urls() if feed_id(url) != calendar_id))

    def set_email(self, email: str) -> dict:
        email = (email or "").strip()
        if email and not _EMAIL.fullmatch(email):
            return {**self.load(), "error": "That doesn't look like an email address."}
        return self._save(load_settings(), user_email=email)

    # Starting notes

    def set_countdown(self, seconds: int) -> dict:
        if int(seconds) not in COUNTDOWN_CHOICES:
            return self.load()
        return self._save(load_settings(), countdown_seconds=int(seconds))

    def set_idle(self, minutes: int) -> dict:
        if int(minutes) not in IDLE_CHOICES:
            return self.load()
        return self._save(load_settings(), idle_seconds=int(minutes) * 60)

    def add_word(self, word: str) -> dict:
        word = " ".join((word or "").split())
        settings = load_settings()
        words = settings.ignore_title_substrings
        if not word or word.casefold() in (piece.casefold() for piece in words):
            return self.load()
        return self._save(settings, ignore_title_substrings=(*words, word))

    def remove_word(self, word: str) -> dict:
        settings = load_settings()
        return self._save(settings, ignore_title_substrings=tuple(piece for piece in settings.ignore_title_substrings if piece != word))

    def preview(self) -> None:
        """Show the countdown card. The card never starts Granola by itself, so nothing is launched."""
        upcoming = read_status(status_path()).next_meeting
        title = upcoming[0] if upcoming else "Weekly product sync"
        processes.spawn(processes.notice_command(title, load_or_defaults()[0].countdown_seconds))

    # First-run setup

    def finish_setup(self, url: str | None, email: str | None) -> dict:
        """Setup's Finish, and its "Zoom only for now", which sends neither."""
        settings = load_settings()
        urls = settings.calendar_urls()
        cleaned = normalize_feed_url(url) if url else None
        if cleaned and cleaned not in urls:
            urls.append(cleaned)
        email = (email or "").strip()
        user_email = email if _EMAIL.fullmatch(email) else settings.user_email
        save_settings(dataclasses.replace(settings, ics_urls=tuple(urls), user_email=user_email, setup_complete=True))
        log.info("setup finished (%s calendars)", len(urls))
        self._ensure_watcher()
        return self.load()

    def _save(self, settings: Settings, **changes) -> dict:
        """Save ``settings`` with ``changes``, which the running watcher picks up, and return the new view."""
        save_settings(dataclasses.replace(settings, **changes))
        return self.load()

    # Elsewhere

    def open_logs(self) -> None:
        processes.open_log_folder()

    def open_config(self) -> None:
        processes.open_config_file()

    def open_link(self, url: str) -> None:
        if url.startswith("https://"):
            os.startfile(url)

    def start_watcher(self) -> dict:
        self._ensure_watcher()
        return self.load()

    # The tray menu's commands, sent to the running watcher

    def pause(self, length: str) -> dict:
        command = {"hour": "pause-hour", "today": "pause-today"}.get(length)
        if command:
            self._change_pause(command)
        return self.load()

    def resume(self) -> dict:
        self._change_pause("resume")
        return self.load()

    def quit_app(self) -> dict:
        self._started_at = None
        if commands.send("quit"):
            # A countdown card on screen keeps the watcher busy until it closes.
            seconds = load_or_defaults()[0].countdown_seconds + 15
            _wait_for(lambda: not processes.watcher_running() and not processes.supervisor_running(), seconds=seconds)
        return self.load()

    def _change_pause(self, command: str) -> None:
        """Send it, then give the watcher a moment to write the new pause, so the page shows it at once."""
        before = read_status(status_path()).paused_until
        if commands.send(command):
            _wait_for(lambda: read_status(status_path()).paused_until != before)

    def open_settings(self) -> dict:
        """Setup's Done: the same window becomes Settings, so another calendar can be added straight away."""
        self.page, self.section = "settings", "calendars"
        if self._window is not None:
            self._window.set_title(TITLES["settings"])
            self._window.resize(*SIZES["settings"])
            _center_on_primary(self._window)
        return self.load()

    def run_setup(self) -> None:
        processes.release_mutex(processes.SETTINGS_MUTEX)
        processes.open_setup()
        self.close()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def _ensure_watcher(self) -> None:
        # A supervisor waiting to restart a watcher that crashed will start it; a second one would quit at once.
        if not processes.watcher_running() and not processes.supervisor_running():
            self._started_at = time.time()
            processes.start_supervisor()

    def _url_for(self, calendar_id: str) -> str | None:
        try:
            urls = load_settings().calendar_urls()
        except ConfigError:
            return None
        return next((url for url in urls if feed_id(url) == calendar_id), None)


def _owners(feeds: list[FeedStatus]) -> list[str]:
    """The calendars' own addresses, which also count as the user's for declined meetings."""
    return list(dict.fromkeys(feed.owner for feed in feeds if feed.owner))


def _wait_for(done: Callable[[], bool], seconds: float = 3) -> bool:
    deadline = time.monotonic() + seconds
    while not done():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _bring_forward() -> None:
    user32 = ctypes.windll.user32
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.FindWindowW.restype = wintypes.HWND
    for title in TITLES.values():
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return


def _style_title_bar(window, dark: bool) -> None:
    """Paper-coloured title bar on Windows 11 (ignored on Windows 10)."""
    try:
        hwnd = int(window.native.Handle.ToInt64())
        if not processes.FROZEN:
            # The packaged exe carries the icon; from source the window would show Python's.
            icon = app_dir() / "app.ico"
            if icon.exists():
                user32 = ctypes.windll.user32
                user32.LoadImageW.restype = wintypes.HANDLE
                handle = user32.LoadImageW(None, str(icon), 1, 0, 0, 0x00000010)  # IMAGE_ICON, LR_LOADFROMFILE
                for which in (0, 1):  # ICON_SMALL, ICON_BIG
                    user32.SendMessageW(wintypes.HWND(hwnd), 0x0080, which, handle)  # WM_SETICON
        set_window_attribute(hwnd, 20, 1 if dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_window_attribute(hwnd, 35, "#1f1d1a" if dark else "#f4efe4")  # DWMWA_CAPTION_COLOR
        set_window_attribute(hwnd, 36, "#f1ece1" if dark else "#2b2924")  # DWMWA_TEXT_COLOR
        set_window_attribute(hwnd, 34, "#3d3a33" if dark else "#e2d9c6")  # DWMWA_BORDER_COLOR
    except Exception as error:
        log.debug("title bar colours not applied (%s)", type(error).__name__)


def _center_on_primary(window) -> None:
    """Every new window opens in the middle of the primary monitor's work area.

    pywebview leaves WinForms to centre the window on whichever display has the pointer, and
    applies that when the window is shown. So placement is switched to manual first, on the
    window's UI thread, using WinForms' own screen and window coordinates.
    """
    try:
        import System.Windows.Forms as WinForms
        from System import Func, Type
        from System.Drawing import Point

        form = window.native

        def center():
            area = WinForms.Screen.PrimaryScreen.WorkingArea
            form.StartPosition = WinForms.FormStartPosition.Manual
            form.Location = Point(
                area.X + max(0, (area.Width - form.Width) // 2),
                area.Y + max(0, (area.Height - form.Height) // 2),
            )

        form.Invoke(Func[Type](center))
    except Exception as error:
        log.warning("window not centered (%s)", type(error).__name__)


def _enable_text_field_menus(window) -> None:
    """Right-click Cut, Copy, and Paste in text fields.

    pywebview switches WebView2's own context menu off outside debug mode. It is switched back on
    here, on the window's UI thread, and web/app.js keeps it to text fields so pages never offer Back or Reload.
    """
    try:
        from System import Func, Type  # pythonnet, already loaded by pywebview

        form = window.native

        def enable():
            form.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled = True

        form.Invoke(Func[Type](enable))
        log.info("text field context menus enabled")
    except Exception as error:
        log.warning("right-click menus stay off (%s)", type(error).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Granola Autostart settings")
    parser.add_argument("--setup", action="store_true", help="first-run setup")
    parser.add_argument("--section", default="calendars", choices=SECTIONS)
    args = parser.parse_args(argv)
    setup_logging("settings-log.txt")
    if not processes.hold_mutex(processes.SETTINGS_MUTEX):
        _bring_forward()
        return 0
    import webview

    page = "setup" if args.setup else "settings"
    dark = app_theme() == "dark"
    api = Api(page, args.section)
    if page == "settings":
        # The Start menu shortcut opens Settings, so opening it also starts an app that was quit.
        api._ensure_watcher()
    width, height = SIZES[page]
    window = webview.create_window(
        TITLES[page],
        url=(WEB / "index.html").as_uri(),
        js_api=api,
        width=width,
        height=height,
        resizable=False,
        background_color="#25231f" if dark else "#fbf8f1",
    )
    api._window = window
    window.events.before_show += lambda: _center_on_primary(window)
    window.events.shown += lambda: _style_title_bar(window, dark)
    window.events.loaded += lambda: _enable_text_field_menus(window)
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        log.exception("the settings window could not open; opening config.json instead")
        processes.open_config_file()
        return 1
    return 0
