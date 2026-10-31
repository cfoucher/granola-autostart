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

from .calendar_feed import events_between, feed_name, feed_owner, fetch_ics, meetings_next_day, merge_events, my_addresses
from .settings import Settings
from .status import FeedStatus, describe_error, feed_id

log = logging.getLogger("granola_autostart")
RETURN_REFRESH_SECONDS = 60
FAILED_FEED_RETRY_SECONDS = 60
STUCK_REFRESH_SECONDS = 600
THREAD_PRIORITY_BELOW_NORMAL = -1


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
                keep_skipped=True,
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
                    args=(settings, self._generation, now),
                    name="calendar-refresh",
                    daemon=True,
                ).start()
            events = self._events
        return [event for event in events if event.end > now]

    def _refresh(self, settings: Settings, generation: int, now: datetime) -> None:
        try:
            lower_thread_priority()
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

        A download already under way used the old settings, so its result is dropped, and a
        calendar no longer in ``urls`` stops counting at once.
        """
        with self._lock:
            self._generation += 1
            self._fetched_at = None
            self._refresh_started = None
            self._by_url = {url: events for url, events in self._by_url.items() if url in urls}
            self._events = self._merge(urls)

    def _merge(self, urls: list[str]) -> list:
        """Every calendar's meetings, in the order the calendars are listed. Call with the lock held."""
        return merge_events(*(self._by_url.get(url, []) for url in urls))

    def feed_statuses(self, urls: list[str]) -> list[FeedStatus]:
        with self._lock:
            return [dataclasses.replace(self._status[url]) if url in self._status else FeedStatus(feed_id(url)) for url in urls]
