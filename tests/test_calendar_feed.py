import gzip
from datetime import datetime, timedelta, timezone

import pytest
import recurring_ical_events
from icalendar import Calendar

from calendar_feed import _gunzip_capped, events_between, feed_owner, my_addresses, normalize_feed_url, prefilter_ics

ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//granola-autostart//test//EN
BEGIN:VEVENT
UID:abc123@google.com
SUMMARY:Design review
DTSTART:20261028T170000Z
DTEND:20261028T173000Z
STATUS:CONFIRMED
END:VEVENT
BEGIN:VEVENT
UID:allday@google.com
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261028
DTEND;VALUE=DATE:20261029
END:VEVENT
BEGIN:VEVENT
UID:focus@google.com
SUMMARY:Focus time
DTSTART:20261028T180000Z
DTEND:20261028T190000Z
END:VEVENT
BEGIN:VEVENT
UID:cancelled@google.com
SUMMARY:Cancelled sync
STATUS:CANCELLED
DTSTART:20261028T170000Z
DTEND:20261028T173000Z
END:VEVENT
BEGIN:VEVENT
UID:declined@google.com
SUMMARY:Skipped 1:1
DTSTART:20261028T190000Z
DTEND:20261028T193000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:conrad@example.com
END:VEVENT
BEGIN:VEVENT
UID:series@google.com
SUMMARY:Weekly
RRULE:FREQ=WEEKLY;COUNT=4
DTSTART:20261021T170000Z
DTEND:20261021T173000Z
END:VEVENT
END:VCALENDAR
"""


def test_feed_keeps_timed_meetings_and_google_instance_ids():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    events = events_between(
        ICS,
        start,
        end,
        ignore_title_substrings=("focus time", "out of office", "lunch"),
        user_emails=frozenset({"conrad@example.com"}),
    )
    titles = [event.summary for event in events]
    assert "Design review" in titles
    assert "Holiday" not in titles
    assert "Focus time" not in titles
    assert "Cancelled sync" not in titles
    assert "Skipped 1:1" not in titles
    review = next(event for event in events if event.summary == "Design review")
    assert review.id == "abc123"
    weekly = [event for event in events if event.summary == "Weekly"]
    assert len(weekly) == 1
    assert weekly[0].id == "series_20261028T170000Z"
    assert weekly[0].start == datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)


LONE_INSTANCE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//granola-autostart//test//EN
BEGIN:VEVENT
UID:shared@google.com
RECURRENCE-ID:20261028T170000Z
SUMMARY:Guild sync
DTSTART:20261028T173000Z
DTEND:20261028T180000Z
END:VEVENT
END:VCALENDAR
"""


def test_a_lone_moved_instance_keeps_its_google_instance_id():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    events = events_between(LONE_INSTANCE, start, start + timedelta(hours=8))
    assert [event.id for event in events] == ["shared_20261028T170000Z"]


def test_a_main_google_calendar_is_owned_by_the_address_it_is_named_after():
    ics = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Conrad.Foucher@Example.com\r\nEND:VCALENDAR\r\n"
    assert feed_owner(ics) == "conrad.foucher@example.com"


def test_a_folded_calendar_name_is_read_whole():
    ics = "BEGIN:VCALENDAR\r\nX-WR-CALNAME:conrad.foucher@exa\r\n mple.com\r\nEND:VCALENDAR\r\n"
    assert feed_owner(ics) == "conrad.foucher@example.com"


def test_a_secondary_calendar_or_an_event_field_has_no_owner():
    assert feed_owner("BEGIN:VCALENDAR\nX-WR-CALNAME:Granola watch\nEND:VCALENDAR\n") is None
    in_event = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nX-WR-CALNAME:someone@example.com\nEND:VEVENT\nEND:VCALENDAR\n"
    assert feed_owner(in_event) is None


def test_declines_are_checked_against_the_typed_address_and_every_owner():
    assert my_addresses(" Alias@Example.ca ", ["conrad@example.com", None, ""]) == {
        "alias@example.ca",
        "conrad@example.com",
    }
    assert my_addresses("", [None]) == frozenset()


FAR_AWAY = ICS.replace(
    "END:VCALENDAR",
    """BEGIN:VEVENT
UID:old@google.com
SUMMARY:Last year
DTSTART:20251028T170000Z
DTEND:20251028T173000Z
END:VEVENT
BEGIN:VEVENT
UID:ended@google.com
SUMMARY:Ended series
RRULE:FREQ=DAILY;UNTIL=20250101T000000Z
DTSTART:20241201T170000Z
DTEND:20241201T173000Z
END:VEVENT
BEGIN:VEVENT
UID:series@google.com
RECURRENCE-ID:20260930T170000Z
SUMMARY:Old moved weekly
DTSTART:20260930T180000Z
DTEND:20260930T183000Z
END:VEVENT
BEGIN:VEVENT
UID:moved@google.com
SUMMARY:Pulled in
RRULE:FREQ=WEEKLY;COUNT=3
DTSTART:20261007T200000Z
DTEND:20261007T203000Z
END:VEVENT
BEGIN:VEVENT
UID:moved@google.com
RECURRENCE-ID:20261021T200000Z
SUMMARY:Pulled in moved
DTSTART:20261028T200000Z
DTEND:20261028T203000Z
END:VEVENT
END:VCALENDAR""",
)


def test_prefilter_drops_far_one_offs_but_keeps_series_and_nearby_events():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    filtered = prefilter_ics(FAR_AWAY, start, end)
    assert "Last year" not in filtered
    assert "Ended series" not in filtered
    assert "Old moved weekly" not in filtered
    assert "Pulled in moved" in filtered
    assert "RRULE:FREQ=WEEKLY" in filtered
    assert "BEGIN:VCALENDAR" in filtered and "END:VCALENDAR" in filtered
    full = Calendar.from_ical(FAR_AWAY)
    expected = sorted(
        str(component.get("summary"))
        for component in recurring_ical_events.of(full).between(start, end)
    )
    found = sorted(
        str(component.get("summary"))
        for component in recurring_ical_events.of(Calendar.from_ical(filtered)).between(start, end)
    )
    assert found == expected


def test_feed_urls_must_be_https():
    assert normalize_feed_url(" webcal://calendar.example/secret.ics ") == "https://calendar.example/secret.ics"
    assert normalize_feed_url("https://calendar.example/x.ics") == "https://calendar.example/x.ics"
    with pytest.raises(ValueError) as error:
        normalize_feed_url("http://calendar.example/secret.ics")
    assert "secret" not in str(error.value)


def test_gzip_bodies_are_capped():
    body = gzip.compress(b"A" * 1000)
    assert _gunzip_capped(body, 1000) == b"A" * 1000
    with pytest.raises(ValueError):
        _gunzip_capped(body, 999)
