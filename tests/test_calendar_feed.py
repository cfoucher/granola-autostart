from datetime import datetime, timedelta, timezone

from calendar_feed import events_between

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
        user_email="conrad@example.com",
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
