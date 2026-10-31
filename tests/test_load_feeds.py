import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import calendar_cache
from granola_autostart.settings import Settings

NOW = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
MAIN = "https://calendar.google.com/main.ics"
WATCH = "https://calendar.google.com/watch.ics"

MAIN_ICS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:conrad@example.com
BEGIN:VEVENT
UID:sync@google.com
SUMMARY:Weekly sync
DTSTART:20261028T170000Z
DTEND:20261028T173000Z
ATTENDEE;PARTSTAT=ACCEPTED:mailto:conrad@example.com
END:VEVENT
END:VCALENDAR
"""

WATCH_ICS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Granola watch
BEGIN:VEVENT
UID:crit@google.com
SUMMARY:Design crit
DTSTART:20261028T180000Z
DTEND:20261028T183000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:conrad@example.com
END:VEVENT
END:VCALENDAR
"""


@pytest.fixture
def feeds(monkeypatch):
    served = {MAIN: MAIN_ICS, WATCH: WATCH_ICS}

    def fetch(url):
        if served.get(url) is None:
            raise OSError("feed down")
        return served[url]

    monkeypatch.setattr(calendar_cache, "fetch_ics", fetch)
    return served


def titles(loaded):
    return sorted(event.summary for by_url in loaded.events.values() for event in by_url)


def test_a_secondary_calendar_skips_declines_by_the_main_calendars_address(feeds):
    owners = {}
    loaded = calendar_cache.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert loaded.failures == []
    assert titles(loaded) == ["Weekly sync"]
    assert owners == {MAIN: "conrad@example.com", WATCH: None}
    assert loaded.names == {MAIN: "conrad@example.com", WATCH: "Granola watch"}


def test_a_failed_main_calendar_still_lends_its_last_known_address(feeds):
    owners = {MAIN: "conrad@example.com"}
    feeds[MAIN] = None
    loaded = calendar_cache.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert [(index, url) for index, url, _error in loaded.failures] == [(1, MAIN)]
    assert titles(loaded) == []


def test_without_any_address_a_decline_cannot_be_recognised(feeds):
    feeds[MAIN] = None
    assert titles(calendar_cache.load_feeds([MAIN, WATCH], NOW, Settings(), {})) == ["Design crit"]
    settings = Settings(user_email="conrad@example.com")
    assert titles(calendar_cache.load_feeds([MAIN, WATCH], NOW, settings, {})) == []


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(calendar_cache, "lower_thread_priority", lambda: None)
    return calendar_cache.CalendarCache()


def test_the_cache_reports_each_calendars_health(feeds, cache):
    feeds[WATCH] = None
    settings = Settings(ics_urls=(MAIN, WATCH))
    for expected_failures in (1, 2, 3):
        cache._refresh(settings, cache._generation, NOW)
        main, watch = cache.feed_statuses([MAIN, WATCH])
        assert (watch.failures, watch.last_error) == (expected_failures, "OSError")
    assert (main.name, main.owner, main.failures, main.meetings_next_day) == ("conrad@example.com", "conrad@example.com", 0, 1)
    feeds[WATCH] = WATCH_ICS
    cache._refresh(settings, cache._generation, NOW)
    assert cache.feed_statuses([WATCH])[0].failures == 0


def test_a_calendar_that_fails_keeps_its_last_meetings(feeds, cache):
    settings = Settings(ics_urls=(MAIN, WATCH))
    cache._refresh(settings, cache._generation, NOW)
    feeds[MAIN] = None
    cache._refresh(settings, cache._generation, NOW)
    assert [event.summary for event in cache.current(NOW, settings, refresh=False)] == ["Weekly sync"]


def test_a_download_started_before_a_settings_change_is_dropped(feeds, cache):
    settings = Settings(ics_urls=(MAIN,))
    started = cache._generation
    cache.invalidate([MAIN])
    cache._refresh(settings, started, NOW)
    assert cache.current(NOW, settings, refresh=False) == []
    assert cache.feed_statuses([MAIN])[0].last_ok is None


def test_a_removed_calendar_stops_counting_before_the_next_download(feeds, cache):
    both = Settings(ics_urls=(MAIN, WATCH), ignore_title_substrings=())
    feeds[WATCH] = WATCH_ICS.replace("PARTSTAT=DECLINED", "PARTSTAT=ACCEPTED")
    cache._refresh(both, cache._generation, NOW)
    assert len(cache.current(NOW, both, refresh=False)) == 2
    main_only = Settings(ics_urls=(MAIN,))
    cache.invalidate(main_only.calendar_urls())
    assert [event.summary for event in cache.current(NOW, main_only, refresh=False)] == ["Weekly sync"]


@pytest.fixture
def downloads(monkeypatch):
    """The downloads current() starts, recorded instead of run, on a monotonic clock the test moves."""
    record = SimpleNamespace(started=[], clock=1000.0)

    class Thread:
        def __init__(self, target, args, name, daemon):
            self.target, self.args = target, args

        def start(self):
            record.started.append(self)

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(calendar_cache, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    monkeypatch.setattr(calendar_cache, "time", SimpleNamespace(monotonic=lambda: record.clock))
    return record


def test_one_download_at_a_time_and_only_when_someone_is_at_the_pc(feeds, cache, downloads):
    settings = Settings(ics_urls=(MAIN,))
    cache.current(NOW, settings, refresh=False)
    assert downloads.started == []
    cache.current(NOW, settings)
    cache.current(NOW, settings)
    assert len(downloads.started) == 1


def test_coming_back_to_the_pc_refreshes_sooner(feeds, cache, downloads):
    settings = Settings(ics_urls=(MAIN,), calendar_refresh_seconds=600)
    cache.current(NOW, settings)
    downloads.started[0].run()
    back = NOW + timedelta(seconds=calendar_cache.RETURN_REFRESH_SECONDS)
    cache.current(back, settings)
    assert len(downloads.started) == 1
    cache.current(back, settings, eager=True)
    assert len(downloads.started) == 2


def test_a_download_stuck_too_long_is_abandoned_for_a_new_one(feeds, cache, downloads):
    settings = Settings(ics_urls=(MAIN,))
    cache.current(NOW, settings)
    downloads.clock += calendar_cache.STUCK_REFRESH_SECONDS
    cache.current(NOW, settings)
    assert len(downloads.started) == 1
    downloads.clock += 1
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
    downloads.started[0].run()
    assert cache.current(NOW, settings, refresh=False) == []
    downloads.started[1].run()
    assert [event.summary for event in cache.current(NOW, settings, refresh=False)] == ["Weekly sync"]


def test_a_failed_download_is_retried_a_minute_later_not_a_full_interval(feeds, cache, downloads):
    feeds[MAIN] = None
    settings = Settings(ics_urls=(MAIN,), calendar_refresh_seconds=600)
    cache.current(NOW, settings)
    downloads.started[0].run()
    retry = NOW + timedelta(seconds=calendar_cache.FAILED_FEED_RETRY_SECONDS)
    cache.current(retry - timedelta(seconds=1), settings)
    assert len(downloads.started) == 1
    cache.current(retry, settings)
    assert len(downloads.started) == 2


def test_changing_calendars_starts_a_new_download_while_the_old_one_runs(feeds, cache, downloads):
    settings = Settings(ics_urls=(MAIN,))
    cache.current(NOW, settings)
    cache.invalidate(settings.calendar_urls())
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
    downloads.started[0].run()
    cache.current(NOW, settings)
    assert len(downloads.started) == 2


def test_changing_calendars_downloads_again_at_once_even_after_a_fresh_download(feeds, cache, downloads):
    settings = Settings(ics_urls=(MAIN,))
    cache.current(NOW, settings)
    downloads.started[0].run()
    cache.current(NOW, settings)
    assert len(downloads.started) == 1  # just downloaded, so not stale
    cache.invalidate(settings.calendar_urls())
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
