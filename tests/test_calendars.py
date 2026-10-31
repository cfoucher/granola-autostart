from datetime import datetime, timedelta, timezone

from granola_autostart.calendar_feed import CalendarEvent, merge_events
from granola_autostart.settings import Settings


def test_calendar_urls_keeps_the_primary_and_extra_feeds():
    settings = Settings(
        ics_url="https://calendar.example/primary.ics",
        ics_urls=("https://calendar.example/watch.ics", "https://calendar.example/primary.ics", ""),
    )
    assert settings.calendar_urls() == [
        "https://calendar.example/primary.ics",
        "https://calendar.example/watch.ics",
    ]


def test_the_same_meeting_on_two_feeds_is_one_prompt():
    start = datetime(2026, 10, 29, 17, 0, tzinfo=timezone.utc)
    shared = CalendarEvent(id="abc", summary="Launcher", start=start, end=start + timedelta(minutes=30))
    other = CalendarEvent(id="def", summary="Office hours", start=start, end=start + timedelta(minutes=30))
    merged = merge_events([shared], [shared, other])
    assert [event.id for event in merged] == ["abc", "def"]
