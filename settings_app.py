"""Settings and first-run setup, in an Edge WebView2 window (pywebview). The page itself is ui/.

    settings_app.py [--section=calendars|notes|about]   Settings
    settings_app.py --setup                              first-run setup

Changes are saved to config.json as they are made; the running watcher reloads it. Only one of
these windows is open at a time: opening another brings the first one forward.
"""

from __future__ import annotations

import argparse
import ctypes
import logging
import os
import re
import subprocess
import sys
import urllib.error
import winreg
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from app_status import describe_error, feed_id, read_status, when
from app_version import __version__
from calendar_feed import events_between, feed_name, feed_owner, fetch_ics, my_addresses, normalize_feed_url
from settings import ConfigError, Settings, app_dir, config_path, load_settings, save_settings, setup_logging, status_path

log = logging.getLogger("granola_autostart")
FROZEN = bool(getattr(sys, "frozen", False))
HERE = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
UI = HERE / "ui"
TITLES = {"settings": "Granola Autostart settings", "setup": "Set up Granola Autostart"}
SIZES = {"settings": (796, 660), "setup": (656, 680)}
COUNTDOWN_CHOICES = (5, 10, 15, 20, 30)
IDLE_CHOICES = (1, 2, 5, 10, 15)  # minutes
SECTIONS = ("calendars", "notes", "about")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def theme() -> str:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            return "light" if winreg.QueryValueEx(key, "AppsUseLightTheme")[0] else "dark"
    except OSError:
        return "light"


def watcher_running() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.OpenMutexW.restype = wintypes.HANDLE
    handle = kernel32.OpenMutexW(0x00100000, False, "Local\\GranolaAutostart")
    if not handle:
        return False
    kernel32.CloseHandle(handle)
    return True


def app_command(*args: str) -> list[str]:
    """This app's other modes: the packaged exe, or pythonw with the matching script."""
    if FROZEN:
        return [sys.executable, *args]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    python = str(pythonw if pythonw.exists() else sys.executable)
    if args and args[0] == "--notice":
        return [python, str(Path(__file__).resolve().parent / "notice.py"), *args[1:]]
    if args and args[0] in ("--settings", "--setup"):
        rest = args[1:] if args[0] == "--settings" else args
        return [python, str(Path(__file__).resolve()), *rest]
    return [python, str(Path(__file__).resolve().parent / "watcher.py"), *args]


def _spawn(command: list[str]) -> None:
    subprocess.Popen(command, close_fds=True)


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
    host = url.split("/")[2].casefold() if url.count("/") >= 2 else ""
    if host.endswith("google.com"):
        return "Google Calendar"
    if "outlook" in host or "office" in host or "live.com" in host:
        return "Outlook"
    return host or "Calendar"


def calendar_line(feed: dict | None, now: datetime) -> tuple[str, str]:
    """(state, text) for a calendar row: ok, pending, or warn."""
    if not feed:
        return "pending", "Checking… The watcher downloads it within a minute."
    failures = int(feed.get("failures") or 0)
    reason = f" ({feed['last_error']})" if feed.get("last_error") else ""
    if failures >= 3:
        return "warn", f"The last {failures} downloads failed{reason}. Meetings on this calendar won't start notes until it's fixed."
    if failures:
        return "pending", f"The last download failed{reason}. Trying again shortly."
    if not feed.get("last_ok"):
        return "pending", "Checking… The watcher downloads it within a minute."
    count = int(feed.get("meetings_next_day") or 0)
    checked = ago(datetime.fromisoformat(feed["last_ok"]), now)
    return "ok", f"{count} meeting{'s' if count != 1 else ''} in the next day · checked {checked}"


def summarize_feed(text: str, settings: Settings, known_owners: list[str], now: datetime) -> dict:
    """What a Test shows: the calendar's name, how many meetings in the next day, and the next few."""
    if "BEGIN:VCALENDAR" not in text[:4096]:
        return {"ok": False, "error": "That address didn't return a calendar. Check you copied the secret address in iCal format."}
    owner = feed_owner(text)
    addresses = my_addresses(settings.user_email, [owner, *known_owners])
    events = [event for event in events_between(text, now, now + timedelta(days=1), user_emails=addresses) if event.start >= now]
    meetings = []
    count = 0
    for event in events:
        word = next((piece for piece in settings.ignore_title_substrings if piece and piece.casefold() in event.summary.casefold()), None)
        if word is None:
            count += 1
        if len(meetings) < 4:
            meetings.append(
                {"time": when(event.start, now), "title": event.summary, "skipped": f"Skipped, title contains “{word}”" if word else None}
            )
    return {"ok": True, "name": feed_name(text) or "Calendar", "owner": owner, "count": count, "meetings": meetings}


class Api:
    """Called from ui/app.js as window.pywebview.api.<name>(...). Every method returns plain JSON data."""

    def __init__(self, page: str, section: str) -> None:
        self.page = page
        self.section = section if section in SECTIONS else "calendars"
        # Private: pywebview walks every public attribute to expose it to the page, and the window's
        # .NET object graph would send it into endless recursion.
        self._window = None

    # Reading

    def load(self) -> dict:
        now = datetime.now().astimezone()
        status = read_status(status_path())
        try:
            settings, config_error = load_settings(), None
        except ConfigError as error:
            settings, config_error = None, str(error)
        feeds = {feed.get("id"): feed for feed in status.get("feeds") or [] if isinstance(feed, dict)}
        running = watcher_running()
        return {
            "page": self.page,
            "section": self.section,
            "theme": theme(),
            "version": __version__,
            "config_error": config_error,
            "settings": self._settings_view(settings, feeds, now) if settings else None,
            "status": self._status_view(status, running, now),
        }

    def _settings_view(self, settings: Settings, feeds: dict, now: datetime) -> dict:
        calendars = []
        for url in settings.calendar_urls():
            feed = feeds.get(feed_id(url))
            state, line = calendar_line(feed, now)
            calendars.append(
                {
                    "id": feed_id(url),
                    "name": (feed or {}).get("name") or "New calendar",
                    "provider": provider(url),
                    "state": state,
                    "line": line,
                }
            )
        detected = []
        for feed in feeds.values():
            owner = feed.get("owner")
            if owner and owner not in detected:
                detected.append(owner)
        idle_minutes = min(IDLE_CHOICES, key=lambda choice: abs(choice * 60 - settings.idle_seconds))
        return {
            "calendars": calendars,
            "detected_addresses": detected,
            "user_email": settings.user_email,
            "countdown_seconds": settings.countdown_seconds,
            "countdown_choices": list(COUNTDOWN_CHOICES),
            "idle_minutes": idle_minutes,
            "idle_choices": list(IDLE_CHOICES),
            "skip_words": list(settings.ignore_title_substrings),
            "setup_complete": settings.setup_complete,
        }

    def _status_view(self, status: dict, running: bool, now: datetime) -> dict:
        problems = [p for p in status.get("problems") or [] if isinstance(p, dict)]
        paused_until = status.get("paused_until")
        upcoming = status.get("next_meeting")
        last = status.get("last_start")
        if not running:
            state, detail = "stopped", "Meetings won't start notes. Starting it…"
        elif problems:
            state, detail = "attention", problems[0].get("title", "")
        elif paused_until:
            state, detail = "paused", f"Until {when(datetime.fromisoformat(paused_until), now)}"
        elif upcoming:
            state, detail = "watching", f"Next: {upcoming['title']} at {when(datetime.fromisoformat(upcoming['start']), now)}"
        else:
            state, detail = "watching", "No more meetings in the next day"
        last_view = None
        if isinstance(last, dict) and last.get("title") and last.get("at"):
            opened = last.get("granola_opened")
            last_view = {
                "title": last["title"],
                "when": when(datetime.fromisoformat(last["at"]), now),
                "opened": opened,
            }
        return {"running": running, "state": state, "detail": detail, "problems": problems, "last_start": last_view, "next": upcoming}

    # Calendars

    def test_address(self, url: str) -> dict:
        try:
            cleaned = normalize_feed_url(url)
        except ValueError:
            return {"ok": False, "error": "Calendar addresses start with https:// (or webcal://)."}
        try:
            settings = load_settings()
        except ConfigError:
            settings = Settings()
        try:
            text = fetch_ics(cleaned, timeout=20)
        except urllib.error.HTTPError as error:
            return {"ok": False, "error": f"Couldn't read that address ({describe_error(error)}). Check you copied all of it."}
        except Exception as error:
            return {"ok": False, "error": f"Couldn't download that address ({describe_error(error)})."}
        status = read_status(status_path())
        owners = [feed.get("owner") for feed in status.get("feeds") or [] if isinstance(feed, dict) and feed.get("owner")]
        try:
            return summarize_feed(text, settings, owners, datetime.now().astimezone())
        except Exception as error:
            log.warning("test could not read the calendar (%s)", type(error).__name__)
            return {"ok": False, "error": "That address returned a calendar this app couldn't read."}

    def test_calendar(self, calendar_id: str) -> dict:
        url = self._url_for(calendar_id)
        return self.test_address(url) if url else {"ok": False, "error": "That calendar was removed."}

    def add_calendar(self, url: str) -> dict:
        cleaned = normalize_feed_url(url)
        settings = load_settings()
        if cleaned not in settings.calendar_urls():
            settings.ics_urls = (*settings.calendar_urls(), cleaned)
            save_settings(settings)
            log.info("calendar added from Settings")
        return self.load()

    def remove_calendar(self, calendar_id: str) -> dict:
        settings = load_settings()
        settings.ics_urls = tuple(url for url in settings.calendar_urls() if feed_id(url) != calendar_id)
        settings.ics_url = ""
        save_settings(settings)
        log.info("calendar removed from Settings")
        return self.load()

    def set_email(self, email: str) -> dict:
        email = (email or "").strip()
        if email and not _EMAIL.fullmatch(email):
            return {**self.load(), "error": "That doesn't look like an email address."}
        settings = load_settings()
        settings.user_email = email
        save_settings(settings)
        return self.load()

    # Starting notes

    def set_countdown(self, seconds: int) -> dict:
        if int(seconds) in COUNTDOWN_CHOICES:
            settings = load_settings()
            settings.countdown_seconds = int(seconds)
            save_settings(settings)
        return self.load()

    def set_idle(self, minutes: int) -> dict:
        if int(minutes) in IDLE_CHOICES:
            settings = load_settings()
            settings.idle_seconds = int(minutes) * 60
            save_settings(settings)
        return self.load()

    def add_word(self, word: str) -> dict:
        word = " ".join((word or "").split())
        settings = load_settings()
        if word and word.casefold() not in (piece.casefold() for piece in settings.ignore_title_substrings):
            settings.ignore_title_substrings = (*settings.ignore_title_substrings, word)
            save_settings(settings)
        return self.load()

    def remove_word(self, word: str) -> dict:
        settings = load_settings()
        settings.ignore_title_substrings = tuple(piece for piece in settings.ignore_title_substrings if piece != word)
        save_settings(settings)
        return self.load()

    def preview(self) -> None:
        """Show the countdown card. The card never starts Granola by itself, so nothing is launched."""
        try:
            seconds = load_settings().countdown_seconds
        except ConfigError:
            seconds = 10
        upcoming = read_status(status_path()).get("next_meeting") or {}
        title = upcoming.get("title") or "Weekly product sync"
        _spawn(app_command("--notice", f"--title={title}", f"--seconds={seconds}"))

    # First-run setup

    def finish_setup(self, url: str | None, email: str | None) -> dict:
        settings = load_settings()
        if url:
            cleaned = normalize_feed_url(url)
            if cleaned not in settings.calendar_urls():
                settings.ics_urls = (*settings.calendar_urls(), cleaned)
        if email and _EMAIL.fullmatch(email.strip()):
            settings.user_email = email.strip()
        settings.setup_complete = True
        save_settings(settings)
        log.info("setup finished (%s calendars)", len(settings.calendar_urls()))
        self._ensure_watcher()
        return self.load()

    def skip_setup(self) -> dict:
        settings = load_settings()
        settings.setup_complete = True
        save_settings(settings)
        log.info("setup finished for Zoom only")
        self._ensure_watcher()
        return self.load()

    # Elsewhere

    def open_logs(self) -> None:
        os.startfile(str(app_dir()))

    def open_config(self) -> None:
        _spawn(["notepad.exe", str(config_path())])

    def open_link(self, url: str) -> None:
        if url.startswith("https://"):
            os.startfile(url)

    def start_watcher(self) -> dict:
        self._ensure_watcher()
        return self.load()

    def run_setup(self) -> None:
        _release_window_mutex()
        _spawn(app_command("--setup"))
        self.close()

    def close(self) -> None:
        if self._window is not None:
            self._window.destroy()

    def _ensure_watcher(self) -> None:
        if not watcher_running():
            _spawn(app_command("--supervise"))

    def _url_for(self, calendar_id: str) -> str | None:
        try:
            urls = load_settings().calendar_urls()
        except ConfigError:
            return None
        return next((url for url in urls if feed_id(url) == calendar_id), None)


_WINDOW_MUTEX = None


def _hold_window_mutex() -> bool:
    global _WINDOW_MUTEX
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    _WINDOW_MUTEX = kernel32.CreateMutexW(None, False, "Local\\GranolaAutostartSettings")
    return bool(_WINDOW_MUTEX) and kernel32.GetLastError() != 183


def _release_window_mutex() -> None:
    global _WINDOW_MUTEX
    if _WINDOW_MUTEX:
        ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(_WINDOW_MUTEX))
        _WINDOW_MUTEX = None


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
        hwnd = wintypes.HWND(int(window.native.Handle.ToInt64()))
        dwm = ctypes.windll.dwmapi

        def set_attribute(attribute: int, value: int) -> None:
            data = ctypes.c_int(value)
            dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))

        def colorref(hex_colour: str) -> int:
            r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
            return r | (g << 8) | (b << 16)

        if not FROZEN:
            # The packaged exe carries the icon; from source the window would show Python's.
            icon = app_dir() / "app.ico"
            if icon.exists():
                user32 = ctypes.windll.user32
                user32.LoadImageW.restype = wintypes.HANDLE
                handle = user32.LoadImageW(None, str(icon), 1, 0, 0, 0x00000010)  # IMAGE_ICON, LR_LOADFROMFILE
                for which in (0, 1):  # ICON_SMALL, ICON_BIG
                    user32.SendMessageW(hwnd, 0x0080, which, handle)  # WM_SETICON
        set_attribute(20, 1 if dark else 0)  # DWMWA_USE_IMMERSIVE_DARK_MODE
        set_attribute(35, colorref("#1f1d1a" if dark else "#f4efe4"))  # DWMWA_CAPTION_COLOR
        set_attribute(36, colorref("#f1ece1" if dark else "#2b2924"))  # DWMWA_TEXT_COLOR
        set_attribute(34, colorref("#3d3a33" if dark else "#e2d9c6"))  # DWMWA_BORDER_COLOR
    except Exception as error:
        log.debug("title bar colours not applied (%s)", type(error).__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Granola Autostart settings")
    parser.add_argument("--setup", action="store_true", help="first-run setup")
    parser.add_argument("--section", default="calendars", choices=SECTIONS)
    args = parser.parse_args(argv)
    setup_logging("settings-log.txt")
    if not _hold_window_mutex():
        _bring_forward()
        return 0
    import webview

    page = "setup" if args.setup else "settings"
    dark = theme() == "dark"
    api = Api(page, args.section)
    if page == "settings":
        # The Start menu shortcut opens Settings, so opening it also starts an app that was quit.
        api._ensure_watcher()
    width, height = SIZES[page]
    window = webview.create_window(
        TITLES[page],
        url=(UI / "index.html").as_uri(),
        js_api=api,
        width=width,
        height=height,
        resizable=False,
        background_color="#25231f" if dark else "#fbf8f1",
    )
    api._window = window
    window.events.shown += lambda: _style_title_bar(window, dark)
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        log.exception("the settings window could not open; opening config.json instead")
        _spawn(["notepad.exe", str(config_path())])
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
