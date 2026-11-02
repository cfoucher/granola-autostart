"""The watcher's calendars: every feed downloaded in the background, merged, and remembered between downloads."""

from __future__ import annotations

import ctypes
import dataclasses
import logging
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .calendar_feed import declined_addresses, events_between, feed_name, feed_owner, fetch_ics, meetings_next_day, merge_events
from .settings import Calendar, Settings, status_path
from .status import FeedStatus, describe_error, feed_id, read_status

log = logging.getLogger("granola_autostart")
RETURN_REFRESH_SECONDS = 60
FAILED_FEED_RETRY_SECONDS = 60
STUCK_REFRESH_SECONDS = 600
THREAD_PRIORITY_BELOW_NORMAL = -1


@dataclass
class FeedLoad:
    events: dict[str, list] = field(default_factory=dict)
    names: dict[str, str | None] = field(default_factory=dict)
    failures: list[tuple[int, str, Exception]] = field(default_factory=list)  # (1-based position in the config, url, error)


def load_feeds(now: datetime, settings: Settings, owners: dict[str, str | None]) -> FeedLoad:
    """Download every calendar that is on, then keep each one's meetings, leaving out those it declined.

    Each calendar's declines are checked against its own addresses (see ``declined_addresses``), so every
    download comes first: a calendar that names no address borrows the others' owners. Each calendar leaves out
    what it declined, so a meeting on several calendars counts if any of them kept it (see ``merge_events``).
    Each meeting also carries where it came from: its calendar's place in the list (0 is the top) and its name.
    ``events`` is filled in the list's order, so merging its groups keeps the copy on the highest calendar.

    ``owners`` holds each calendar's own address, calendars that are off included, and is updated in place,
    so a feed that fails this time, or is off, still lends the address it had last time.
    """
    calendars = settings.distinct_calendars()
    texts: dict[str, str] = {}
    result = FeedLoad()
    for index, calendar in enumerate(calendars, start=1):
        if not calendar.enabled:
            continue
        url = calendar.url
        try:
            texts[url] = fetch_ics(url)
            owners[url] = feed_owner(texts[url])
            result.names[url] = feed_name(texts[url])
        except Exception as error:
            result.failures.append((index, url, error))
    known = [owners.get(calendar.url) for calendar in calendars]
    for index, calendar in enumerate(calendars, start=1):
        url = calendar.url
        if url not in texts:
            continue
        try:
            found = events_between(
                texts[url],
                now - timedelta(hours=12),
                now + timedelta(hours=36),
                ignore_title_substrings=settings.ignore_title_substrings,
                user_emails=declined_addresses(calendar.declined_by, owners.get(url), known, settings.user_email),
                keep_skipped=True,
            )
            name = result.names.get(url) or ""
            result.events[url] = [dataclasses.replace(event, calendar_rank=index - 1, calendar_name=name) for event in found]
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
    """Feeds download on a background thread so a slow server never delays a call's card."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_url: dict[str, list] = {}
        self._events: list = []
        self._status: dict[str, FeedStatus] = {}
        # What the last run saved, so a calendar that is off, and so never downloaded, still has its name and
        # owner after a restart. Only those two are kept: nothing else about a feed is true before it downloads.
        self._previous = {feed.id: feed for feed in read_status(status_path()).feeds}
        self._fetched_at: datetime | None = None
        self._refresh_started: float | None = None
        self._generation = 0

    def current(self, now: datetime, settings: Settings, *, refresh: bool = True, eager: bool = False) -> list:
        """Return cached events. Downloads only start when ``refresh`` is set, i.e. someone is at the PC."""
        if not settings.enabled_urls():
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
                    args=(settings, self._generation, now),
                    name="calendar-refresh",
                    daemon=True,
                ).start()
            events = self._events
        return [event for event in events if event.end > now]

    def known_owners(self, calendars: list[Calendar]) -> dict[str, str | None]:
        """Each calendar's own address as last known, which a calendar that is off or failing still lends."""
        with self._lock:
            return {calendar.url: self._status_of(calendar.url).owner for calendar in calendars}

    def _status_of(self, url: str) -> FeedStatus:
        """A calendar's status, started from the last run's name and owner. Call with the lock held."""
        if url not in self._status:
            previous = self._previous.get(feed_id(url)) or FeedStatus(feed_id(url))
            self._status[url] = FeedStatus(feed_id(url), name=previous.name, owner=previous.owner)
        return self._status[url]

    def _refresh(self, settings: Settings, generation: int, now: datetime) -> None:
        try:
            lower_thread_priority()
            urls = settings.enabled_urls()
            owners = self.known_owners(settings.distinct_calendars())
            loaded = load_feeds(now, settings, owners)
            for index, _url, error in loaded.failures:
                log.error("calendar feed %s failed (%s); keeping its previous events", index, describe_error(error))
            fetched_at = now
            if loaded.failures:
                fetched_at = now - timedelta(seconds=max(0, settings.calendar_refresh_seconds - FAILED_FEED_RETRY_SECONDS))
            with self._lock:
                if generation != self._generation:
                    return
                self._by_url.update(loaded.events)
                for _index, url, error in loaded.failures:
                    self._status_of(url).failed(error)
                for url, events in loaded.events.items():
                    self._status_of(url).succeeded(
                        now, name=loaded.names.get(url), owner=owners.get(url), meetings_next_day=meetings_next_day(events, now)
                    )
                self._events = merged = self._merge(urls)
                self._fetched_at = fetched_at
            log.info("calendar feeds have %s upcoming events from %s calendars", len(merged), len(urls))
        except Exception as error:
            log.error("calendar refresh failed (%s)", type(error).__name__)
        finally:
            with self._lock:
                if generation == self._generation:
                    self._refresh_started = None

    def invalidate(self, urls: list[str]) -> None:
        """Download again on the next tick, because the calendars or their filters changed.

        ``urls`` are the calendars that are on. A download already under way used the old settings, so its
        result is dropped, and a calendar no longer in ``urls``, whether removed or switched off, stops
        counting at once. Its name and owner are kept, but its failures are forgotten: switched back on,
        it starts clean, instead of alerting before it has been downloaded once.
        """
        with self._lock:
            self._generation += 1
            self._fetched_at = None
            self._refresh_started = None
            self._by_url = {url: events for url, events in self._by_url.items() if url in urls}
            for url, status in self._status.items():
                if url not in urls:
                    status.failures, status.last_error = 0, None
            self._events = self._merge(urls)

    def _merge(self, urls: list[str]) -> list:
        """The meetings of the calendars in ``urls``, as one list. Call with the lock held."""
        return merge_events(*(self._by_url.get(url, []) for url in urls))

    def feed_statuses(self, calendars: list[Calendar]) -> list[FeedStatus]:
        """Every calendar's status, off ones included and marked, in the order the calendars are listed."""
        with self._lock:
            return [dataclasses.replace(self._status_of(calendar.url), enabled=calendar.enabled) for calendar in calendars]
