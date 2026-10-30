import urllib.error
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app_status import AlertBook, FeedStatus, LastStart, describe_error, feed_id, find_problems, tray_view, when
from calendar_feed import CalendarEvent

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


@dataclass
class Launch:
    title: str = "Weekly product sync"
    launched_at: datetime = NOW
    seen_running: bool = False
    warned: bool = False


def failing_feed(failures: int = 3) -> FeedStatus:
    feed = FeedStatus(feed_id("https://calendar.example/watch.ics"), name="Granola watch")
    for _ in range(failures):
        feed.failed(urllib.error.HTTPError("https://calendar.example/watch.ics", 404, "Not Found", {}, None))
    return feed


def test_errors_are_described_without_the_address():
    error = urllib.error.HTTPError("https://calendar.example/secret.ics", 404, "Not Found", {}, None)
    assert describe_error(error) == "404 Not Found"
    assert describe_error(urllib.error.URLError(TimeoutError())) == "timed out"
    assert describe_error(urllib.error.URLError("getaddrinfo failed")) == "couldn't connect"
    assert describe_error(ValueError("https://calendar.example/secret.ics is odd")) == "ValueError"
    assert describe_error(ValueError("calendar feed is too large")) == "calendar feed is too large"


def test_a_calendar_becomes_a_problem_after_three_failed_downloads():
    assert find_problems(None, [failing_feed(2)], None) == []
    [problem] = find_problems(None, [failing_feed(3)], None)
    assert problem.title == "“Granola watch” calendar isn't updating"
    assert "(404 Not Found)" in problem.detail
    assert problem.section == "calendars"


def test_a_launch_that_never_opened_granola_comes_first():
    problems = find_problems("config.json is not valid JSON", [failing_feed()], Launch(warned=True))
    assert [problem.key.split(":")[0] for problem in problems] == ["launch", "config", "feed"]
    assert problems[0].title == "Granola didn't open for Weekly product sync"
    assert find_problems(None, [], Launch(warned=True, seen_running=True)) == []


def test_each_problem_notifies_once_until_it_clears():
    book = AlertBook()
    problems = find_problems(None, [failing_feed()], None)
    assert book.fresh(problems) == problems
    assert book.fresh(problems) == []
    assert book.fresh([]) == []
    assert book.fresh(problems) == problems


def event(summary: str, start: datetime) -> CalendarEvent:
    return CalendarEvent(id=summary, summary=summary, start=start, end=start + timedelta(minutes=30))


def test_the_tray_shows_the_next_meeting_a_pause_or_the_problem():
    upcoming = event("Weekly product sync", NOW + timedelta(hours=1))
    view = tray_view(NOW, None, [], upcoming)
    assert (view.kind, view.header, view.paused) == ("watching", "Next: Weekly product sync, 10:00", False)
    assert tray_view(NOW, None, [], None).header == "Watching for meetings"
    paused = tray_view(NOW, NOW + timedelta(hours=1), [], upcoming)
    assert (paused.kind, paused.header) == ("paused", "Paused until 10:00")
    midnight = datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    assert tray_view(NOW, midnight, [], None).header == "Paused until tomorrow"
    problems = find_problems(None, [failing_feed()], None)
    attention = tray_view(NOW, NOW + timedelta(hours=1), problems, upcoming)
    assert (attention.kind, attention.paused, attention.problem_section) == ("attention", True, "calendars")
    assert attention.tooltip.startswith("Granola Autostart: “Granola watch”")


def test_times_read_naturally():
    assert when(NOW + timedelta(hours=3), NOW) == "12:00"
    assert when(NOW + timedelta(days=3), NOW) == "Mon 09:00"
    assert when(NOW + timedelta(days=10), NOW) == "9 Nov 09:00"


def test_a_last_start_survives_a_restart():
    saved = LastStart("Design crit", NOW, True).to_json()
    assert LastStart.from_json(saved) == LastStart("Design crit", NOW, True)
    assert LastStart.from_json(None) is None
    assert LastStart.from_json({"title": "x"}) is None
