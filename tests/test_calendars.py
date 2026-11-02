from datetime import datetime, timedelta, timezone

from granola_autostart.calendar_feed import CalendarEvent, meetings_next_day, merge_events
from granola_autostart.settings import Calendar, Settings


def test_calendar_urls_drops_blanks_and_repeats():
    settings = Settings(
        calendars=(
            Calendar(" https://calendar.example/primary.ics"),
            Calendar("https://calendar.example/watch.ics", enabled=False),
            Calendar("https://calendar.example/primary.ics", enabled=False),
            Calendar(""),
        ),
    )
    assert settings.calendar_urls() == [
        "https://calendar.example/primary.ics",
        "https://calendar.example/watch.ics",
    ]
    assert [(calendar.url, calendar.enabled) for calendar in settings.distinct_calendars()] == [
        ("https://calendar.example/primary.ics", True),  # the first entry for an address wins
        ("https://calendar.example/watch.ics", False),
    ]


def test_enabled_urls_leaves_out_the_calendars_that_are_off():
    settings = Settings(calendars=(Calendar("https://calendar.example/primary.ics"), Calendar("https://calendar.example/watch.ics", enabled=False)))
    assert settings.enabled_urls() == ["https://calendar.example/primary.ics"]
    assert Settings().enabled_urls() == []


def test_the_same_meeting_on_two_feeds_is_one_prompt():
    start = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc)
    shared = CalendarEvent(id="abc", summary="Launcher", start=start, end=start + timedelta(minutes=30))
    other = CalendarEvent(id="def", summary="Office hours", start=start, end=start + timedelta(minutes=30))
    merged = merge_events([shared], [shared, other])
    assert [event.id for event in merged] == ["abc", "def"]


def test_a_meeting_declined_on_either_calendar_is_skipped_whichever_copy_comes_first():
    start = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc)
    kept = CalendarEvent(id="abc", summary="Launcher", start=start, end=start + timedelta(minutes=30))
    declined = CalendarEvent(id="abc", summary="Launcher", start=start, end=start + timedelta(minutes=30), declined=True)
    other = CalendarEvent(id="def", summary="Office hours", start=start, end=start + timedelta(minutes=30))
    assert [event.id for event in merge_events([kept, other], [declined])] == ["def"]
    assert [event.id for event in merge_events([declined], [kept, other])] == ["def"]
    assert merge_events([declined]) == []
    # The same series at another time is another meeting.
    later = CalendarEvent(id="abc", summary="Launcher", start=start + timedelta(days=7), end=start + timedelta(days=7, minutes=30))
    assert merge_events([declined, later]) == [later]


def test_the_meetings_a_calendar_has_in_the_next_day_leave_out_skipped_and_declined_ones():
    now = datetime(2026, 10, 29, 9, 0, tzinfo=timezone.utc)
    soon = now + timedelta(hours=1)

    def event(event_id, **marks):
        return CalendarEvent(id=event_id, summary=event_id, start=soon, end=soon + timedelta(minutes=30), **marks)

    assert meetings_next_day([event("a"), event("b", skipped=True), event("c", declined=True)], now) == 1
