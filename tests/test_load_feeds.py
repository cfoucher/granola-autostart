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


def titles(events):
    return sorted(event.summary for by_url in events.values() for event in by_url)


def test_a_secondary_calendar_skips_declines_by_the_main_calendars_address(feeds):
    owners = {}
    events, failures = watcher.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert failures == []
    assert titles(events) == ["Weekly sync"]
    assert owners == {MAIN: "conrad@example.com", WATCH: None}


def test_a_failed_main_calendar_still_lends_its_last_known_address(feeds):
    owners = {MAIN: "conrad@example.com"}
    feeds[MAIN] = None
    events, failures = watcher.load_feeds([MAIN, WATCH], NOW, Settings(), owners)
    assert [index for index, _error in failures] == [1]
    assert titles(events) == []


def test_without_any_address_a_decline_cannot_be_recognised(feeds):
    feeds[MAIN] = None
    events, _failures = watcher.load_feeds([MAIN, WATCH], NOW, Settings(), {})
    assert titles(events) == ["Design crit"]
    events, _failures = watcher.load_feeds([MAIN, WATCH], NOW, Settings(user_email="conrad@example.com"), {})
    assert titles(events) == []
