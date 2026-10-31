import json
import urllib.error
from datetime import datetime, timedelta, timezone

from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch
from granola_autostart.status import (
    AlertBook,
    FeedStatus,
    LastStart,
    Problem,
    Status,
    describe_error,
    end_of_today,
    feed_id,
    find_problems,
    next_meeting,
    read_status,
    taking_notes,
    tray_view,
    until_tomorrow,
    when,
    write_status,
)

NOW = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def launch(*, warned: bool = False, seen_running: bool = False) -> ActiveLaunch:
    return ActiveLaunch(
        "Weekly product sync", {}, ("k",), NOW + timedelta(minutes=30), seen_running=seen_running, launched_at=NOW, warned=warned
    )


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
    problems = find_problems("config.json is not valid JSON", [failing_feed()], launch(warned=True))
    assert [problem.key.split(":")[0] for problem in problems] == ["launch", "config", "feed"]
    assert problems[0].title == "Granola didn't open for Weekly product sync"
    assert find_problems(None, [], launch(warned=True, seen_running=True)) == []


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


def test_taking_notes_while_granola_is_open_for_the_meeting():
    started = launch()
    assert taking_notes(started, NOW) is None  # not open yet
    started.seen_running = True
    assert taking_notes(started, NOW + timedelta(minutes=5)) == ("Weekly product sync", NOW)
    assert taking_notes(started, NOW + timedelta(minutes=30)) is None  # the meeting is over
    started.missing_since = NOW + timedelta(minutes=10)
    assert taking_notes(started, NOW + timedelta(minutes=11)) is None  # Granola was closed
    assert taking_notes(None, NOW) is None


def test_the_next_meeting_is_never_a_skipped_one():
    lunch = CalendarEvent("lunch", "Team lunch", NOW + timedelta(minutes=30), NOW + timedelta(hours=1), skipped=True)
    review = event("Design review", NOW + timedelta(hours=1))
    assert next_meeting([lunch, review], NOW) == review
    assert next_meeting([lunch], NOW) is None


def test_pause_for_today_ends_at_midnight_which_reads_as_tomorrow():
    late = NOW.replace(hour=23, minute=59, second=37, microsecond=123456)
    assert end_of_today(late) == datetime(2026, 10, 31, 0, 0, tzinfo=timezone.utc)
    assert until_tomorrow(end_of_today(late), late)
    assert not until_tomorrow(late + timedelta(hours=1), late)
    assert not until_tomorrow(end_of_today(late) + timedelta(days=1), late)


def test_feed_status_survives_status_json():
    feed = failing_feed(1)
    feed.succeeded(NOW, name="Work", owner="me@example.com", meetings_next_day=2)
    assert FeedStatus.from_json(feed.to_json()) == feed
    assert FeedStatus.from_json({"name": "no id"}) is None
    assert FeedStatus.from_json("not a feed") is None


def test_a_calendar_that_never_downloaded_survives_status_json():
    for feed in (FeedStatus(feed_id("https://calendar.example/new.ics")), failing_feed(2)):
        assert (feed.meetings_next_day, feed.last_ok) == (None, None)
        assert FeedStatus.from_json(json.loads(json.dumps(feed.to_json()))) == feed


def test_a_failing_calendar_says_whether_it_is_still_retrying():
    assert failing_feed(1).failure_detail() == "The last download failed (404 Not Found). Trying again shortly."
    assert failing_feed(3).failure_detail().startswith("The last 3 downloads failed (404 Not Found). Meetings on this calendar")


def test_times_read_naturally():
    assert when(NOW + timedelta(hours=3), NOW) == "12:00"
    assert when(NOW + timedelta(days=3), NOW) == "Mon 09:00"
    assert when(NOW + timedelta(days=10), NOW) == "9 Nov 09:00"


def test_a_last_start_survives_a_restart():
    saved = LastStart("Design crit", NOW, True).to_json()
    assert LastStart.from_json(saved) == LastStart("Design crit", NOW, True)
    assert LastStart.from_json(None) is None
    assert LastStart.from_json({"title": "x"}) is None


def full_status() -> Status:
    working = FeedStatus(feed_id("https://calendar.example/main.ics"))
    working.succeeded(NOW, name="Work", owner="me@example.com", meetings_next_day=4)
    return Status(
        version="1.0.2",
        paused_until=NOW + timedelta(hours=1),
        next_meeting=("Design crit", NOW + timedelta(hours=2)),
        taking_notes=("Weekly product sync", NOW - timedelta(minutes=5)),
        feeds=[working, failing_feed(3)],
        last_start=LastStart("Weekly product sync", NOW - timedelta(minutes=5), True),
        problems=[Problem("config", "Settings couldn't be read", "config.json is not valid JSON.", "calendars")],
    )


def test_the_whole_status_survives_status_json(tmp_path):
    status = full_status()
    assert Status.from_json(json.loads(json.dumps(status.to_json()))) == status
    write_status(tmp_path / "status.json", status)
    assert read_status(tmp_path / "status.json") == status


def test_an_unreadable_status_comes_back_empty_instead_of_raising():
    for raw in (None, [], "status", 5):
        assert Status.from_json(raw) == Status()
    garbled = {
        "version": None,
        "paused_until": "next tuesday",
        "next_meeting": {"title": "Design crit", "start": 5},
        "taking_notes": ["Weekly product sync"],
        "feeds": [{"id": "a", "failures": "many"}, {"id": "b", "last_ok": "yesterday"}, {"name": "no id"}, "feed", None],
        "last_start": {"title": "Design crit", "at": "yesterday"},
        "problems": [{"title": "no key"}, 7, {"key": "k", "title": "Kept", "section": "about"}],
    }
    # A feed keeps its place with an unreadable time, which then reads as never downloaded.
    assert Status.from_json(garbled) == Status(feeds=[FeedStatus("b")], problems=[Problem("k", "Kept", "", "about")])
    assert Status.from_json({"feeds": {"id": "a"}, "problems": "config", "paused_until": 5}) == Status()
    assert Status.from_json({"feeds": [{"id": "a", "failures": float("inf")}, {"id": "b", "meetings_next_day": float("inf")}]}) == Status()


def test_a_hand_edited_time_without_an_offset_is_read_as_local_time():
    status = Status.from_json(
        {"paused_until": "2026-10-30T10:00", "last_start": {"title": "Design crit", "at": "2026-10-30T09:00"},
         "feeds": [{"id": "a", "last_ok": "2026-10-30T08:00"}]}
    )
    local = datetime(2026, 10, 30, 10, 0).astimezone()
    assert status.paused_until == local and status.paused_until.tzinfo is not None
    assert status.last_start.at.tzinfo is not None and status.feeds[0].last_ok.tzinfo is not None
    assert status.paused_until > local - timedelta(hours=1)  # comparable with an aware now


def test_a_missing_or_garbled_status_file_reads_as_empty(tmp_path):
    path = tmp_path / "status.json"
    assert read_status(path) == Status()
    path.write_text("{not json", encoding="utf-8")
    assert read_status(path) == Status()
    path.write_bytes(b"\xff\xfe{}")
    assert read_status(path) == Status()


def test_a_time_more_than_a_week_away_either_way_shows_its_date():
    assert when(NOW - timedelta(days=1), NOW) == "Thu 09:00"
    assert when(NOW - timedelta(days=9), NOW) == "21 Oct 09:00"
    assert when(NOW - timedelta(days=30), NOW) == "30 Sep 09:00"


def test_a_weekday_is_shown_up_to_six_days_either_way():
    assert when(NOW + timedelta(days=6), NOW) == "Thu 09:00"
    assert when(NOW - timedelta(days=6), NOW) == "Sat 09:00"
    assert when(NOW + timedelta(days=7), NOW) == "6 Nov 09:00"
    assert when(NOW - timedelta(days=7), NOW) == "23 Oct 09:00"
