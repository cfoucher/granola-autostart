import gzip
import io
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import recurring_ical_events
from icalendar import Calendar

from granola_autostart import calendar_feed
from granola_autostart.calendar_feed import (
    _gunzip_capped,
    _read_capped,
    declined_addresses,
    events_between,
    feed_owner,
    normalize_feed_url,
    prefilter_ics,
)

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


GUESTS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:reminder@google.com
SUMMARY:Reload credits
DTSTART:20261028T170000Z
DTEND:20261028T171500Z
END:VEVENT
BEGIN:VEVENT
UID:one@google.com
SUMMARY:One on one
DTSTART:20261028T170000Z
DTEND:20261028T173000Z
ATTENDEE;PARTSTAT=ACCEPTED:mailto:conrad@example.com
END:VEVENT
BEGIN:VEVENT
UID:standup@google.com
SUMMARY:Stand-up
DTSTART:20261028T170000Z
DTEND:20261028T171500Z
ATTENDEE;PARTSTAT=ACCEPTED:mailto:conrad@example.com
ATTENDEE;PARTSTAT=NEEDS-ACTION:mailto:ana@example.com
ATTENDEE;PARTSTAT=DECLINED:mailto:ben@example.com
END:VEVENT
END:VCALENDAR
"""


def test_a_meeting_s_guests_are_the_attendees_it_lists_declined_ones_included():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    events = events_between(GUESTS, start, start + timedelta(hours=4))
    assert {event.summary: event.guests for event in events} == {"Reload credits": 0, "One on one": 1, "Stand-up": 3}
    # Which calendar a meeting is on isn't known here, and Granola is told nothing of guests or calendars.
    standup = next(event for event in events if event.summary == "Stand-up")
    assert (standup.calendar_rank, standup.calendar_name) == (0, "")
    assert set(standup.payload()) == {"id", "summary", "start", "end"}


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


def test_skipped_meetings_can_be_kept_and_marked_for_zoom_matching():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    kept = events_between(ICS, start, end, ignore_title_substrings=("focus time",), keep_skipped=True)
    assert [(e.summary, e.skipped) for e in kept if e.summary == "Focus time"] == [("Focus time", True)]
    assert all(not e.skipped for e in kept if e.summary != "Focus time")
    dropped = events_between(ICS, start, end, ignore_title_substrings=("focus time",))
    assert "Focus time" not in [e.summary for e in dropped]


def test_a_meeting_is_left_out_when_one_of_the_addresses_declined_it_and_kept_for_any_other_address_or_none():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    me = frozenset({"conrad@example.com"})

    def summaries(**options):
        return [e.summary for e in events_between(ICS, start, end, **options)]

    everything = summaries()  # nothing is declined without addresses
    assert "Skipped 1:1" in everything
    declined = summaries(user_emails=me)
    assert "Skipped 1:1" not in declined
    assert declined == [summary for summary in everything if summary != "Skipped 1:1"]  # and nothing else is left out
    # A decline by an address that isn't one of the calendar's isn't a decline.
    assert summaries(user_emails=frozenset({"someone@example.com"})) == everything
    assert "Skipped 1:1" not in summaries(user_emails=("conrad@example.com",))  # any collection will do


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


def test_an_automatic_calendar_checks_declines_against_its_own_address_and_the_old_typed_one():
    own = declined_addresses(None, "conrad@example.com", ["other@example.com", "conrad@example.com"], " Alias@Example.ca ")
    assert own == ("conrad@example.com", "alias@example.ca")  # not other@example.com, which is another calendar's
    assert declined_addresses(None, "conrad@example.com", [], "") == ("conrad@example.com",)


def test_an_automatic_calendar_with_no_address_of_its_own_borrows_every_known_owner():
    known = ["conrad@example.com", None, "", "Other@Example.com", "conrad@example.com"]
    assert declined_addresses(None, None, known, "alias@example.ca") == ("conrad@example.com", "other@example.com", "alias@example.ca")
    assert declined_addresses(None, None, known, "") == ("conrad@example.com", "other@example.com")
    assert declined_addresses(None, None, [None], "") == ()


def test_a_list_of_declined_addresses_is_exactly_those_in_lower_case():
    listed = (" Alias@Example.ca", "conrad@example.com", "ALIAS@example.ca", "")
    assert declined_addresses(listed, "main@example.com", ["main@example.com"], "old@example.com") == ("alias@example.ca", "conrad@example.com")
    assert declined_addresses((), "main@example.com", [], "old@example.com") == ()


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


def test_downloads_are_capped_across_reads():
    # The body arrives in 64 KiB reads, so this one only goes over the cap on its second.
    body = b"A" * (100 * 1024)
    deadline = time.monotonic() + 60
    assert _read_capped(io.BytesIO(body), len(body), deadline) == body
    with pytest.raises(ValueError):
        _read_capped(io.BytesIO(body), len(body) - 1, deadline)


class Trickling:
    """A server that sends a byte at a time, each taking ``seconds`` on ``clock``. Reading for a whole chunk of that,
    rather than for what has arrived, would take for ever."""

    def __init__(self, body: bytes, clock: list[float], seconds: float) -> None:
        self.body, self.clock, self.seconds = io.BytesIO(body), clock, seconds

    def read(self, size=-1):
        pytest.fail("read waits for a whole chunk, which a trickle is slow to fill")

    def read1(self, size=-1):
        self.clock[0] += self.seconds
        return self.body.read(1)


def test_a_download_that_trickles_is_given_up_on_at_the_deadline_not_when_the_calendar_is_in(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(calendar_feed, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with pytest.raises(TimeoutError, match="took too long"):
        _read_capped(Trickling(b"A" * 1000, clock, seconds=30), 10_000, deadline=100.0)
    assert clock[0] <= 150  # a few bytes in, not all of them


def test_a_download_that_trickles_but_finishes_in_time_is_whole(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(calendar_feed, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    assert _read_capped(Trickling(b"BEGIN:VCALENDAR", clock, seconds=1), 100, deadline=100.0) == b"BEGIN:VCALENDAR"


class Served(io.BytesIO):
    def __init__(self, body: bytes, **headers: str) -> None:
        super().__init__(body)
        self.headers = headers


def test_a_calendar_is_downloaded_whole_and_unzipped_when_it_comes_gzipped(monkeypatch):
    text = "BEGIN:VCALENDAR\nX-WR-CALNAME:Work\nEND:VCALENDAR\n" * 3000  # several reads
    asked = []

    def serve(body, **headers):
        def open_(request, timeout):
            asked.append((request.full_url, request.get_header("Accept-encoding"), timeout))
            return Served(body, **headers)

        monkeypatch.setattr(calendar_feed, "_opener", SimpleNamespace(open=open_))

    serve(text.encode())
    assert calendar_feed.fetch_ics("webcal://calendar.example/work.ics") == text
    serve(gzip.compress(text.encode()), **{"Content-Encoding": "GZIP"})
    assert calendar_feed.fetch_ics("https://calendar.example/work.ics", timeout=5) == text
    assert asked == [("https://calendar.example/work.ics", "gzip", 30), ("https://calendar.example/work.ics", "gzip", 5)]


def _calendar(events: str) -> str:
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//granola-autostart//test//EN\n{events}END:VCALENDAR\n"


PACIFIC_SERIES = _calendar(
    """BEGIN:VTIMEZONE
TZID:America/Los_Angeles
BEGIN:DAYLIGHT
TZOFFSETFROM:-0800
TZOFFSETTO:-0700
TZNAME:PDT
DTSTART:19700308T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:-0700
TZOFFSETTO:-0800
TZNAME:PST
DTSTART:19701101T020000
RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:standup@google.com
SUMMARY:Standup
RRULE:FREQ=WEEKLY;COUNT=4
DTSTART;TZID=America/Los_Angeles:20261021T100000
DTEND;TZID=America/Los_Angeles:20261021T103000
END:VEVENT
"""
)


def test_a_pacific_series_keeps_its_local_time_when_daylight_saving_ends():
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    events = events_between(PACIFIC_SERIES, start, start + timedelta(days=10))
    assert [event.id for event in events] == ["standup_20261028T170000Z", "standup_20261104T180000Z"]
    assert [event.start for event in events] == [
        datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc),
    ]
    assert [event.end - event.start for event in events] == [timedelta(minutes=30)] * 2


SERIES_WITH_GAPS = _calendar(
    """BEGIN:VEVENT
UID:excluded@google.com
SUMMARY:Excluded weekly
RRULE:FREQ=WEEKLY;COUNT=3
EXDATE:20261028T170000Z
DTSTART:20261021T170000Z
DTEND:20261021T173000Z
END:VEVENT
BEGIN:VEVENT
UID:called-off@google.com
SUMMARY:Called-off weekly
RRULE:FREQ=WEEKLY;COUNT=3
DTSTART:20261021T180000Z
DTEND:20261021T183000Z
END:VEVENT
BEGIN:VEVENT
UID:called-off@google.com
RECURRENCE-ID:20261028T180000Z
SUMMARY:Called-off weekly
STATUS:CANCELLED
DTSTART:20261028T180000Z
DTEND:20261028T183000Z
END:VEVENT
"""
)


def test_an_excluded_date_removes_only_that_occurrence():
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)
    events = events_between(SERIES_WITH_GAPS, start, start + timedelta(days=21))
    assert [event.id for event in events if event.summary == "Excluded weekly"] == [
        "excluded_20261021T170000Z",
        "excluded_20261104T170000Z",
    ]


def test_a_cancelled_occurrence_is_dropped_but_the_rest_of_the_series_stays():
    start = datetime(2026, 10, 20, tzinfo=timezone.utc)
    events = events_between(SERIES_WITH_GAPS, start, start + timedelta(days=21))
    assert [event.id for event in events if event.summary == "Called-off weekly"] == [
        "called-off_20261021T180000Z",
        "called-off_20261104T180000Z",
    ]


def test_crlf_line_endings_parse_the_same_as_lf():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=8)
    with_lf = events_between(FAR_AWAY, start, end)
    with_crlf = events_between(FAR_AWAY.replace("\n", "\r\n"), start, end)
    assert len(with_lf) >= 3
    assert with_crlf == with_lf
    assert [event.key() for event in with_crlf] == [event.key() for event in with_lf]


NO_ENDS = _calendar(
    """BEGIN:VEVENT
UID:open-ended@google.com
SUMMARY:Open ended
DTSTART:20261028T170000Z
END:VEVENT
BEGIN:VEVENT
UID:timed@google.com
SUMMARY:Forty-five minutes
DTSTART:20261028T190000Z
DURATION:PT45M
END:VEVENT
BEGIN:VEVENT
UID:all-day@google.com
SUMMARY:All day
DTSTART;VALUE=DATE:20261028
END:VEVENT
"""
)


def test_a_meeting_without_an_end_lasts_an_hour():
    start = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
    events = events_between(NO_ENDS, start, start + timedelta(hours=8))
    lengths = {event.summary: event.end - event.start for event in events}
    assert lengths == {"Open ended": timedelta(hours=1), "Forty-five minutes": timedelta(minutes=45)}
    # A window that opens part-way through still finds it.
    under_way = events_between(NO_ENDS, datetime(2026, 10, 28, 17, 30, tzinfo=timezone.utc), start + timedelta(hours=2))
    assert [event.summary for event in under_way] == ["Open ended"]


FLOATING = _calendar(
    """BEGIN:VEVENT
UID:before@example.com
SUMMARY:Before the change
DTSTART:20261028T120000
DTEND:20261028T130000
END:VEVENT
BEGIN:VEVENT
UID:after@example.com
SUMMARY:After the change
DTSTART:20261104T120000
DTEND:20261104T130000
END:VEVENT
"""
)


def test_a_floating_time_follows_the_pcs_daylight_saving_rule_for_its_date(monkeypatch):
    pacific = ZoneInfo("America/Los_Angeles")
    monkeypatch.setattr(calendar_feed, "_local_time", lambda value: value.replace(tzinfo=pacific))
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    events = events_between(FLOATING, start, start + timedelta(days=10))
    assert [event.start for event in events] == [
        datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 4, 20, 0, tzinfo=timezone.utc),
    ]
    assert [event.end - event.start for event in events] == [timedelta(hours=1)] * 2


def test_a_floating_time_the_pc_cannot_place_keeps_todays_offset(monkeypatch):
    def unplaceable(value):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(calendar_feed, "_local_time", unplaceable)
    start = datetime(2026, 10, 27, tzinfo=timezone.utc)
    events = events_between(FLOATING, start, start + timedelta(days=10))
    assert [event.start.replace(tzinfo=None) for event in events] == [datetime(2026, 10, 28, 12), datetime(2026, 11, 4, 12)]
