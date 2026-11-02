"""Settings and first-run setup, in an Edge WebView2 window (pywebview). The page itself is web/.

    python -m granola_autostart --settings [--section=calendars|calls|notes|general]   Settings
    python -m granola_autostart --setup                                                first-run setup

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
from collections.abc import Callable, Iterable
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, commands, processes, updates
from .calendar_feed import (
    declined_addresses,
    events_between,
    feed_name,
    feed_owner,
    fetch_ics,
    meetings_next_day,
    normalize_feed_url,
    skip_word,
)
from .calls import installed as call_installed
from .settings import (
    APPEARANCES,
    CALL_PROVIDERS,
    DEFAULT_CALLS,
    Calendar,
    ConfigError,
    Settings,
    app_dir,
    clean_declined_by,
    load_or_defaults,
    load_settings,
    saved_appearance,
    save_settings,
    setup_logging,
    status_path,
    update_config,
)
from .status import FeedStatus, Status, describe_error, feed_id, read_status, until_tomorrow, when
from .winui import app_theme, set_window_attribute

log = logging.getLogger("granola_autostart")
# Beside this module from source and in the packaged build's _internal folder alike.
WEB = Path(__file__).resolve().with_name("web")
TITLES = {"settings": "Granola Autostart settings", "setup": "Set up Granola Autostart"}
SIZES = {"settings": (796, 660), "setup": (656, 680)}
COUNTDOWN_CHOICES = (5, 10, 15, 20, 30)
IDLE_CHOICES = (1, 2, 5, 10, 15)  # minutes
SECTIONS = ("calendars", "calls", "notes", "general")
CALL_NAMES = {"zoom": "Zoom", "teams": "Microsoft Teams", "meet": "Google Meet"}  # as the Calls page and setup list them
# General was About before: an old shortcut or command line with --section=about, and a problem an older
# version saved in status.json, still say so.
SECTION_ALIASES = {"about": "general"}
STARTING_SECONDS = 30  # after this window starts the watcher, how long "Starting…" shows before it counts as not running
RESTARTING_SECONDS = 30  # a supervisor with no watcher for longer is restarting one that keeps failing
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
NOT_AN_ADDRESS = "That doesn't look like an email address."


def section_id(name: str) -> str:
    """The Settings section a name stands for, with the old names accepted. Calendars when it isn't one."""
    name = SECTION_ALIASES.get(name, name)
    return name if name in SECTIONS else "calendars"


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


def calendar_line(feed: FeedStatus | None, now: datetime, enabled: bool = True) -> tuple[str, str]:
    """(state, text) for a calendar row: ok, pending, warn, or off."""
    if not enabled:
        return "off", "Off. Its meetings won't start notes."
    if feed is not None and feed.failures:
        return "warn" if feed.alerting else "pending", feed.failure_detail()
    if feed is None or feed.last_ok is None:
        return "pending", "Checking… The watcher downloads it within a minute."
    count = feed.meetings_next_day or 0
    return "ok", f"{count} meeting{'s' if count != 1 else ''} in the next day · checked {ago(feed.last_ok, now)}"


def update_view(state: updates.UpdateState, now: datetime) -> dict:
    """The General page's update row: whether updates come from git, the version available, when it was last
    checked ("today at 09:12"), why a check or an install failed, and whether a countdown card is in the way.
    An install that failed is only shown while that update is still on offer. ``installing`` is true while another
    process is installing (the tray, or another Settings window), and in the answer to Install update once an
    installer is running; the page then says so for ``install_wait_seconds``."""
    release = state.available()
    checked = None
    if state.checked_at:
        today = state.checked_at.astimezone(now.tzinfo).date() == now.date()
        checked = f"{'today at' if today else 'on'} {when(state.checked_at, now)}"
    return {
        "source": not updates.enabled(),
        "available": release.version if release else None,
        "page": release.page if release else None,
        "checked": checked,
        "error": state.error,
        "install_error": state.install_error if release else None,
        "card_showing": processes.card_showing(),
        "installing": processes.mutex_exists(processes.INSTALL_MUTEX),
        "install_wait_seconds": updates.INSTALL_WAIT_SECONDS,
    }


def summarize_feed(
    text: str, settings: Settings, known_owners: list[str | None], now: datetime, declined_by: tuple[str, ...] | None = None
) -> dict:
    """What a Test shows: the calendar's name, how many meetings in the next day, and the next few.

    It filters the way the watcher does for this calendar, with its ``declined_by`` list or, when that is
    None, its automatic addresses, so Test agrees with what the watcher will start notes for. The result
    also has the ``declined`` addresses in use, the ``note`` for them (see address_note), ``older``
    (see adds_older_address), and ``lent`` (see calendars_lent).
    """
    if "BEGIN:VCALENDAR" not in text[:4096]:
        return {"ok": False, "error": "That address didn't return a calendar. Check you copied the secret address in iCal format."}
    owner = feed_owner(text)
    words = settings.ignore_title_substrings
    note = address_note(declined_by, owner, known_owners, settings.user_email)
    addresses = declined_addresses(declined_by, owner, known_owners, settings.user_email)
    events = events_between(text, now, now + timedelta(days=1), ignore_title_substrings=words, user_emails=addresses, keep_skipped=True)
    events = [event for event in events if event.start >= now]
    meetings = [{"time": when(event.start, now), "title": event.summary, "skipped": _skip_reason(event, words)} for event in events[:4]]
    return {
        "ok": True,
        "name": feed_name(text) or "Calendar",
        "owner": owner,
        "count": meetings_next_day(events, now),
        "meetings": meetings,
        "declined": list(addresses if declined_by is None else declined_by),
        "note": note,
        "older": adds_older_address(declined_by, owner, known_owners, settings.user_email),
        "lent": calendars_lent(note, known_owners),
    }


def address_note(declined_by: tuple[str, ...] | None, owner: str | None, known_owners: Iterable[str | None], user_email: str) -> str:
    """Where a calendar's declined addresses come from, as the page says it under them: "unchecked" (an empty list:
    no declines are checked), "list" (typed in), "own" (the calendar's name is an address), "copied" (it names none,
    so the other calendars' are used), "kept" (no other calendar has one either: only the address an older version
    saved) or "none" (no address at all)."""
    if declined_by == ():
        return "unchecked"
    if declined_by is not None:
        return "list"
    if owner:
        return "own"
    if any(known_owners):
        return "copied"
    return "kept" if user_email else "none"


def adds_older_address(declined_by: tuple[str, ...] | None, owner: str | None, known_owners: Iterable[str | None], user_email: str) -> bool:
    """The address an older version saved (``user_email``) is an extra chip on an automatic calendar, on top of the ones
    its note says (its own, or the other calendars'), so the page says so. With no others the "kept" note already does."""
    others = list(known_owners)
    if declined_by is not None or not (owner or any(others)):
        return False
    return len(declined_addresses(None, owner, others, user_email)) > len(declined_addresses(None, owner, others, ""))


def calendars_lent(note: str, known_owners: Iterable[str | None]) -> int:
    """How many calendars lent their address to a "copied" note, which the page needs to say "calendar" or "calendars".
    That is not the number of addresses: two calendars can share one, and ``older`` adds another. 0 for any other note."""
    return sum(1 for owner in known_owners if owner) if note == "copied" else 0


def _skip_reason(event, words: Iterable[str]) -> str | None:
    if not event.skipped:
        return None
    word = skip_word(event.summary, words)  # None when the word sat past where the title was shortened
    return f"Skipped, title contains “{word}”" if word else "Skipped by a skip word"


class Api:
    """Called from web/app.js as window.pywebview.api.<name>(...). Every method returns plain JSON data."""

    def __init__(self, page: str, section: str, theme: str | None = None) -> None:
        """``theme`` is the one the window opens with, which its title bar is styled for as it is shown."""
        self.page = page
        self.section = section_id(section)
        # Private: pywebview walks every public attribute to expose it to the page, and the window's
        # .NET object graph would send it into endless recursion.
        self._window = None
        self._theme = theme  # the theme the title bar was last styled for
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
        theme = app_theme()
        self._follow_theme(theme)
        # The switch is read from config.json, so it is right at once and while no watcher runs. When that can't
        # be read, the watcher's last word is the best there is.
        off = status.off if config_error else not settings.autostart_notes
        return {
            "page": self.page,
            "section": self.section,
            "theme": theme,
            "version": __version__,
            "update": update_view(updates.read_state(), now),
            "config_error": config_error,
            "settings": None if config_error else self._settings_view(settings, status.feeds, now),
            "status": self._status_view(
                status,
                running,
                now,
                waiting=time.monotonic() - self._waiting_since if waiting else None,
                off=off,
                calls=any(settings.calls.values()),
            ),
            # Read on its own: a config.json that can't be loaded still has the choice in it, and Auto would be wrong.
            "appearance": saved_appearance(),
            "start_at_sign_in": processes.start_at_sign_in(),
            # From source there is no installed exe for the sign-in entry to start, so the page has no switch for it.
            # Nor is there a Start menu shortcut, which the page's note on Quit leaves out.
            "sign_in_available": processes.FROZEN,
        }

    def _follow_theme(self, theme: str) -> None:
        """Style the title bar again when the theme has changed since it was last styled: a new Appearance
        choice, or Windows changing its own on Auto. The page itself follows ``theme`` as it renders."""
        if theme != self._theme and self._window is not None:
            self._theme = theme
            _style_title_bar(self._window, theme == "dark")

    def _settings_view(self, settings: Settings, feeds: list[FeedStatus], now: datetime) -> dict:
        by_id = {feed.id: feed for feed in feeds}
        owners = _owners(settings, feeds)
        calendars = []
        # In the saved order, which is the one the page's arrows change and the one a countdown card breaks a tie by.
        for calendar in settings.distinct_calendars():
            feed = by_id.get(feed_id(calendar.url))
            state, line = calendar_line(feed, now, calendar.enabled)
            automatic = calendar.declined_by is None
            owner = feed.owner if feed else None
            # A calendar that hasn't downloaded since the watcher started, perhaps never, has no owner to go on: it may
            # name an address nobody knows yet, so the page doesn't offer addresses for it.
            unread = automatic and not owner and (feed is None or feed.last_ok is None)
            if unread:
                # "pending" while the watcher is about to download it, and "off" while it is off and won't until it is switched on.
                note, older = ("pending" if calendar.enabled else "off"), False
            else:
                note = address_note(calendar.declined_by, owner, owners, settings.user_email)
                older = adds_older_address(calendar.declined_by, owner, owners, settings.user_email)
            calendars.append(
                {
                    "id": feed_id(calendar.url),
                    "name": (feed.name if feed else None) or "New calendar",
                    "provider": provider(calendar.url),
                    "state": state,
                    "line": line,
                    "enabled": calendar.enabled,
                    # The chips: the list, or when automatic, the addresses the watcher uses for this calendar.
                    "declined": _declined_chips(calendar, owner, owners, settings.user_email),
                    "automatic": automatic,
                    # Where the addresses come from (see address_note), or "pending" or "off" while the calendar hasn't downloaded.
                    "note": note,
                    "older": older,  # see adds_older_address
                    "lent": calendars_lent(note, owners),  # see calendars_lent
                }
            )
        idle_minutes = min(IDLE_CHOICES, key=lambda choice: abs(choice * 60 - settings.idle_seconds))
        return {
            "calendars": calendars,
            # One per provider, in the order the page lists them. ``found``: whether the app is on this PC, which
            # is only for the page to say so (None for Meet, which is in a browser) and never decides a call.
            "calls": [
                {
                    "provider": provider,
                    "name": CALL_NAMES[provider],
                    "on": settings.calls.get(provider, DEFAULT_CALLS[provider]),
                    "found": call_installed(provider),
                }
                for provider in CALL_PROVIDERS
            ],
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

    def _status_view(
        self, status: Status, running: bool, now: datetime, *, waiting: float | None = None, off: bool = False, calls: bool = True
    ) -> dict:
        """The side panel's status box. Its buttons (see web/app.js) follow the state, like the tray menu.

        ``waiting``: for how many seconds the supervisor has been up with no watcher. Briefly, that is the
        watcher starting (at sign-in); for longer, the supervisor is restarting one that keeps failing.
        ``off``: Autostart notes is switched off. Off wins over a problem and a pause, but not over a watcher
        that isn't running: the box then says so, and the switch still shows.
        ``calls``: some call provider is on, so "Calls still start notes" is true.
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
        elif off:
            state, detail = "off", "Meetings and calls won't start notes. It stays off after a restart, until you switch it on."
        elif problems:
            state, detail = "attention", problems[0].title
        elif paused and today:
            state, detail = "paused", "Until tomorrow. Calls joined meanwhile won't be asked about later."
        elif paused:
            state, detail = "paused", f"Until {when(paused_until, now)}. No countdown cards until then."
        elif status.taking_notes:
            title, since = status.taking_notes
            state, detail = "notes", f"{title}, since {when(since, now)}"
        elif status.next_meeting:
            title, start = status.next_meeting
            state, detail = "watching", f"Next: {title} at {when(start, now)}"
        else:
            state, detail = "watching", "No meetings coming up. Calls still start notes." if calls else "No meetings coming up."
        return {
            "running": running,
            "state": state,
            "detail": detail,
            "paused": paused,
            "paused_today": today,
            "autostart_notes": not off,
            "fix_section": section_id(problems[0].section) if problems else None,
            "last_start": {"title": last.title, "when": when(last.at, now), "opened": last.granola_opened} if last else None,
        }

    # Calendars

    def test_address(self, url: str) -> dict:
        """Test an address before adding it. Besides the meetings, the result has the ``declined`` addresses
        the calendar would use if added as it is, and the ``note`` that says where they come from."""
        return self._test(url, None)

    def test_calendar(self, calendar_id: str) -> dict:
        calendar = self._calendar_for(calendar_id)
        return self._test(calendar.url, calendar.declined_by) if calendar else {"ok": False, "error": "That calendar was removed."}

    def _test(self, url: str, declined_by: tuple[str, ...] | None) -> dict:
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
            owners = _owners(settings, read_status(status_path()).feeds)
            return summarize_feed(text, settings, owners, datetime.now().astimezone(), declined_by)
        except Exception as error:
            log.warning("test could not read the calendar (%s)", type(error).__name__)
            return {"ok": False, "error": "That address returned a calendar this app couldn't read."}

    def add_calendar(self, url: str, declined_by: list[str] | None = None) -> dict:
        """Add a calendar. ``declined_by`` is what the editor ended with after Test, stored as it is given: the
        page sends None while the editor holds what Test suggested, which leaves the calendar automatic, and an empty
        list when it was emptied, which checks no declines."""
        cleaned = normalize_feed_url(url)
        settings = load_settings()
        if cleaned in settings.calendar_urls():
            return self.load()
        chosen = clean_declined_by(declined_by)
        if not _valid_addresses(chosen):
            return {**self.load(), "error": NOT_AN_ADDRESS}
        log.info("calendar added from Settings")
        calendar = Calendar(cleaned, declined_by=chosen)
        return self._save(settings, calendars=(*settings.distinct_calendars(), calendar))

    def remove_calendar(self, calendar_id: str) -> dict:
        settings = load_settings()
        log.info("calendar removed from Settings")
        return self._save(settings, calendars=tuple(calendar for calendar in settings.distinct_calendars() if feed_id(calendar.url) != calendar_id))

    def set_calendar_enabled(self, calendar_id: str, on: bool) -> dict:
        settings = load_settings()
        return self._save_calendar(settings, _find(settings, calendar_id), enabled=bool(on))

    def move_calendar(self, calendar_id: str, step: int) -> dict:
        """Move a calendar one place up (``step`` -1) or down (1) in the saved order, which is the order the page lists
        them in. A calendar that has gone, one already at that end, and any other step are left alone. Places are
        counted among the calendars as listed, so an address saved twice, which is one calendar, doesn't take one."""
        step = int(step)
        settings = load_settings()
        calendars = settings.distinct_calendars()
        at = next((index for index, calendar in enumerate(calendars) if feed_id(calendar.url) == calendar_id), None)
        if step not in (-1, 1) or at is None or not 0 <= at + step < len(calendars):
            return self.load()
        calendars[at], calendars[at + step] = calendars[at + step], calendars[at]
        log.info("calendar moved from Settings")
        return self._save(settings, calendars=tuple(calendars))

    def add_declined(self, calendar_id: str, address: str) -> dict:
        """Add an address a calendar's declines count for. The first one added to an automatic calendar
        starts from the addresses it was using."""
        address = (address or "").strip()
        if not _EMAIL.fullmatch(address):
            return {**self.load(), "error": NOT_AN_ADDRESS}
        settings = load_settings()
        calendar = _find(settings, calendar_id)
        if calendar is None:
            return self.load()
        current = _current_chips(settings, calendar)
        if address.casefold() in (piece.casefold() for piece in current):
            return self.load()
        return self._save_calendar(settings, calendar, declined_by=(*current, address))

    def remove_declined(self, calendar_id: str, address: str) -> dict:
        """Take an address away. With none left, the calendar checks no declines: Reset addresses is how it goes back
        to automatic."""
        settings = load_settings()
        calendar = _find(settings, calendar_id)
        if calendar is None:
            return self.load()
        current = _current_chips(settings, calendar)
        remaining = [piece for piece in current if piece.casefold() != (address or "").strip().casefold()]
        if len(remaining) == len(current):
            return self.load()
        return self._save_calendar(settings, calendar, declined_by=tuple(remaining))

    def reset_declined(self, calendar_id: str) -> dict:
        """Put a calendar's addresses back to automatic."""
        settings = load_settings()
        return self._save_calendar(settings, _find(settings, calendar_id), declined_by=None)

    # Updates

    def check_updates(self) -> dict:
        """Check now. It waits for GitHub, up to its timeout, so the page shows it working."""
        if updates.enabled():
            updates.check(datetime.now().astimezone())
        return self.load()

    def install_update(self) -> dict:
        """Install update. It downloads first, and once the installer has started it stops this window in a
        moment: ``update.installing`` says so. A failure comes back in ``update.install_error``. A countdown card
        that came up meanwhile, or the tray already installing, is neither: the page just shows the row again."""
        release = updates.read_state().available() if updates.enabled() else None
        started = release is not None and updates.install(release) is updates.Install.STARTED
        view = self.load()
        view["update"]["installing"] = started or view["update"]["installing"]  # the latter: another install is under way
        return view

    # Switches and appearance
    #
    # These two change their one key with update_config, which leaves the rest of config.json as it is on disk. The other
    # setters save every setting with _save, from the copy they have just loaded. The tray's Turn off writes autostart_notes
    # the same way, and a whole-file save here could put back a value it changed a moment ago. Appearance is a single key
    # too, so it needs no other setting read or decrypted.

    def set_autostart_notes(self, on: bool) -> dict:
        """The switch at the top of the status box. The running watcher reloads config.json, and a watcher that
        isn't running starts with it."""
        update_config("autostart_notes", bool(on))
        log.info("Autostart notes turned %s from Settings", "on" if on else "off")
        return self.load()

    def set_start_at_sign_in(self, on: bool) -> dict:
        processes.set_start_at_sign_in(bool(on))
        log.info("start at sign-in turned %s from Settings", "on" if on else "off")
        return self.load()

    def set_appearance(self, choice: str) -> dict:
        if choice not in APPEARANCES:
            return self.load()
        update_config("appearance", choice)
        return self.load()

    # Calls

    def set_call(self, provider: str, on: bool) -> dict:
        """The switch on the Calls page. A provider this version doesn't know is ignored."""
        if provider not in CALL_PROVIDERS:
            return self.load()
        settings = load_settings()
        log.info("%s calls turned %s from Settings", provider, "on" if on else "off")
        return self._save(settings, calls={**settings.calls, provider: bool(on)})

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

    def finish_setup(self, url: str | None, declined_by: list[str] | None = None, calls: dict[str, bool] | None = None) -> dict:
        """Setup's Finish, and its "Calls only for now", which sends no calendar. ``declined_by`` is as for add_calendar.
        ``calls`` is the Calls step's switches, by provider. One it leaves out, or None, keeps its saved value."""
        settings = load_settings()
        calendars = settings.distinct_calendars()
        cleaned = normalize_feed_url(url) if url else None
        chosen = clean_declined_by(declined_by)
        if cleaned and not _valid_addresses(chosen):
            return {**self.load(), "error": NOT_AN_ADDRESS}
        if cleaned and cleaned not in settings.calendar_urls():
            calendars.append(Calendar(cleaned, declined_by=chosen))
        save_settings(dataclasses.replace(settings, calendars=tuple(calendars), calls=_with_calls(settings.calls, calls), setup_complete=True))
        log.info("setup finished (%s calendars)", len(calendars))
        self._ensure_watcher()
        return self.load()

    def _save(self, settings: Settings, **changes) -> dict:
        """Save ``settings`` with ``changes``, which the running watcher picks up, and return the new view."""
        save_settings(dataclasses.replace(settings, **changes))
        return self.load()

    def _save_calendar(self, settings: Settings, calendar: Calendar | None, **changes) -> dict:
        """Save one calendar with ``changes``. A calendar that was removed in the meantime is left alone."""
        if calendar is None:
            return self.load()
        calendars = tuple(dataclasses.replace(each, **changes) if each == calendar else each for each in settings.distinct_calendars())
        return self._save(settings, calendars=calendars)

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

    def _calendar_for(self, calendar_id: str) -> Calendar | None:
        try:
            return _find(load_settings(), calendar_id)
        except ConfigError:
            return None


def _find(settings: Settings, calendar_id: str) -> Calendar | None:
    return next((calendar for calendar in settings.distinct_calendars() if feed_id(calendar.url) == calendar_id), None)


def _owners(settings: Settings, feeds: list[FeedStatus]) -> list[str | None]:
    """Each calendar's own address as the watcher last saw it, off calendars included, in the order they're listed.
    A calendar that has been removed no longer lends its address."""
    by_id = {feed.id: feed.owner for feed in feeds}
    return [by_id.get(feed_id(url)) for url in settings.calendar_urls()]


def _declined_chips(calendar: Calendar, owner: str | None, owners: list[str | None], user_email: str) -> list[str]:
    """The addresses a calendar shows: its list as typed or, when it is automatic, the ones the watcher uses for it.
    ``owner`` is its own address as the watcher last saw it, and ``owners`` is every calendar's (see _owners)."""
    if calendar.declined_by is not None:
        return list(calendar.declined_by)
    return list(declined_addresses(None, owner, owners, user_email))


def _current_chips(settings: Settings, calendar: Calendar) -> list[str]:
    """A calendar's addresses now, from what the watcher last saw."""
    feeds = read_status(status_path()).feeds
    owner = next((feed.owner for feed in feeds if feed.id == feed_id(calendar.url)), None)
    return _declined_chips(calendar, owner, _owners(settings, feeds), settings.user_email)


def _with_calls(current: dict[str, bool], choices) -> dict[str, bool]:
    """``current`` with the switches ``choices`` sets. Anything that isn't a provider's true or false is ignored."""
    if not isinstance(choices, dict):
        return current
    return {**current, **{provider: on for provider, on in choices.items() if provider in CALL_PROVIDERS and isinstance(on, bool)}}


def _valid_addresses(addresses: tuple[str, ...] | None) -> bool:
    return all(_EMAIL.fullmatch(address) for address in addresses or ())


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
    parser.add_argument("--section", default="calendars", choices=(*SECTIONS, *SECTION_ALIASES))
    args = parser.parse_args(argv)
    setup_logging("settings-log.txt")
    if not processes.hold_mutex(processes.SETTINGS_MUTEX):
        _bring_forward()
        return 0
    import webview

    page = "setup" if args.setup else "settings"
    theme = app_theme()
    dark = theme == "dark"
    api = Api(page, args.section, theme)
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
    window.events.shown += lambda: _style_title_bar(window, api._theme == "dark")
    window.events.loaded += lambda: _enable_text_field_menus(window)
    try:
        webview.start(gui="edgechromium", private_mode=True)
    except Exception:
        log.exception("the settings window could not open; opening config.json instead")
        processes.open_config_file()
        return 1
    finally:
        # The process can take a while to exit after its window has closed, and until it has, opening Settings again
        # would only look for a window that is gone.
        processes.release_mutex(processes.SETTINGS_MUTEX)
    return 0
