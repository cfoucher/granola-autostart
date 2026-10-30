from datetime import datetime, timezone

import pytest

import watcher
from settings import Settings

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

    monkeypatch.setattr(watcher, "fetch_ics", fetch)
    return served


def titles(loaded):
    return sorted(event.summary for by_url in loaded.events.values() for event in by_url)


def test_a_secondary_calendar_skips_declines_by_the_main_calendars_address(feeds):
    owners = {}
    loaded = watcher.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert loaded.failures == []
    assert titles(loaded) == ["Weekly sync"]
    assert owners == {MAIN: "conrad@example.com", WATCH: None}
    assert loaded.names == {MAIN: "conrad@example.com", WATCH: "Granola watch"}


def test_a_failed_main_calendar_still_lends_its_last_known_address(feeds):
    owners = {MAIN: "conrad@example.com"}
    feeds[MAIN] = None
    loaded = watcher.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert [(index, url) for index, url, _error in loaded.failures] == [(1, MAIN)]
    assert titles(loaded) == []


def test_without_any_address_a_decline_cannot_be_recognised(feeds):
    feeds[MAIN] = None
    assert titles(watcher.load_feeds([MAIN, WATCH], NOW, Settings(), {})) == ["Design crit"]
    settings = Settings(user_email="conrad@example.com")
    assert titles(watcher.load_feeds([MAIN, WATCH], NOW, settings, {})) == []


def test_the_cache_reports_each_calendars_health(feeds, monkeypatch):
    monkeypatch.setattr(watcher, "lower_thread_priority", lambda: None)
    monkeypatch.setattr(watcher, "datetime", type("FixedNow", (datetime,), {"now": staticmethod(lambda tz=None: NOW)}))
    cache = watcher.CalendarCache()
    feeds[WATCH] = None
    settings = Settings(ics_urls=(MAIN, WATCH))
    for expected_failures in (1, 2, 3):
        cache._refresh(settings, cache._generation)
        main, watch = cache.feed_statuses([MAIN, WATCH])
        assert (watch.failures, watch.last_error) == (expected_failures, "OSError")
    assert (main.name, main.owner, main.failures, main.meetings_next_day) == ("conrad@example.com", "conrad@example.com", 0, 1)
    feeds[WATCH] = WATCH_ICS
    cache._refresh(settings, cache._generation)
    assert cache.feed_statuses([WATCH])[0].failures == 0
