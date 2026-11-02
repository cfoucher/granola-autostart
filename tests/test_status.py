import json
import urllib.error
from datetime import datetime, timedelta, timezone

import pytest

from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch
from granola_autostart.status import (
    TrayView,
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
    saved_moment,
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


def test_a_calendar_that_is_off_never_becomes_a_problem():
    off = failing_feed(5)
    off.enabled = False
    assert find_problems(None, [off], None) == []
    assert [problem.key.split(":")[0] for problem in find_problems(None, [off, failing_feed(3)], None)] == ["feed"]


def test_a_launch_that_never_opened_granola_comes_first():
    problems = find_problems("config.json is not valid JSON", [failing_feed()], launch(warned=True))
    assert [problem.key.split(":")[0] for problem in problems] == ["launch", "config", "feed"]
    assert problems[0].title == "Granola didn't open for Weekly product sync"
    assert problems[1].detail == (
        "config.json is not valid JSON. Until it's fixed, the app keeps the last settings it could read, "
        "or the defaults if it started with this file."
    )
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


def test_the_update_the_install_and_the_off_switch_are_given_by_name():
    with pytest.raises(TypeError):
        tray_view(NOW, None, [], None, "1.2.0")  # a bare version could as well be anything else
    assert tray_view(NOW, None, [], None, update="1.2.0", installing=None, off=False).update == "1.2.0"


def test_an_update_takes_the_first_line_unless_something_needs_attention_or_it_is_paused():
    upcoming = event("Weekly product sync", NOW + timedelta(hours=1))
    ready = tray_view(NOW, None, [], upcoming, update="1.2.0")
    assert ready == TrayView("watching", "Version 1.2.0 is available", False, update="1.2.0")
    assert tray_view(NOW, None, [], None, update="1.2.0").header == "Version 1.2.0 is available"
    assert ready.tooltip == "Granola Autostart: Version 1.2.0 is available"
    # Otherwise the existing first line wins, and the update still shows in the badge and the menu.
    paused = tray_view(NOW, NOW + timedelta(hours=1), [], upcoming, update="1.2.0")
    assert (paused.kind, paused.header, paused.update) == ("paused", "Paused until 10:00", "1.2.0")
    problems = find_problems(None, [failing_feed()], None)
    attention = tray_view(NOW, None, problems, upcoming, update="1.2.0")
    assert (attention.kind, attention.header, attention.update) == ("attention", "“Granola watch” calendar isn't updating", "1.2.0")
    plain = tray_view(NOW, None, [], upcoming)
    assert (plain.header, plain.update) == ("Next: Weekly product sync, 10:00", None)


def test_off_wins_over_a_pause_and_a_problem_in_the_header_and_the_tooltip():
    upcoming = event("Weekly product sync", NOW + timedelta(hours=1))
    off = tray_view(NOW, None, [], upcoming, off=True)
    assert off == TrayView("off", "Off. Meetings and calls won't start notes", False)
    assert off.tooltip == "Granola Autostart: Off. Meetings and calls won't start notes"
    problems = find_problems(None, [failing_feed()], None)
    both = tray_view(NOW, NOW + timedelta(hours=1), problems, upcoming, off=True)
    assert (both.kind, both.paused, both.problem_section) == ("off", False, None)
    assert tray_view(NOW, None, [], upcoming, off=False).kind == "watching"


def test_an_update_shows_in_the_badge_and_menu_while_off_but_not_in_the_header():
    upcoming = event("Weekly product sync", NOW + timedelta(hours=1))
    off = tray_view(NOW, None, [], upcoming, update="1.2.0", off=True)
    assert (off.kind, off.header, off.update) == ("off", "Off. Meetings and calls won't start notes", "1.2.0")
    installing = tray_view(NOW, None, [], upcoming, update="1.2.0", installing="1.2.0", off=True)
    assert (installing.kind, installing.header) == ("off", "Installing version 1.2.0…")


def test_an_update_being_installed_takes_the_first_line_whatever_else_is_going_on():
    upcoming = event("Weekly product sync", NOW + timedelta(hours=1))
    problems = find_problems(None, [failing_feed()], None)
    for paused_until, found in ((None, []), (NOW + timedelta(hours=1), []), (None, problems)):
        view = tray_view(NOW, paused_until, found, upcoming, update="1.2.0", installing="1.2.0")
        assert view.header == "Installing version 1.2.0…" and view.update == "1.2.0"
    assert tray_view(NOW, None, [], upcoming, update="1.2.0", installing="1.2.0").kind == "watching"


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


def test_a_calendar_that_is_off_stays_off_in_status_json_and_a_status_without_the_mark_means_on():
    feed = FeedStatus(feed_id("https://calendar.example/watch.ics"), name="Granola watch", owner="me@example.com", enabled=False)
    assert FeedStatus.from_json(json.loads(json.dumps(feed.to_json()))) == feed
    assert FeedStatus.from_json({"id": "a"}).enabled and FeedStatus.from_json({"id": "a", "enabled": None}).enabled
    assert not FeedStatus.from_json({"id": "a", "enabled": False}).enabled


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


def test_off_survives_status_json_and_anything_but_true_reads_as_on():
    assert Status().to_json()["off"] is False
    assert Status.from_json(json.loads(json.dumps(Status(off=True).to_json()))).off is True
    for raw in (None, "true", 1, "yes", [True]):
        assert Status.from_json({"off": raw}).off is False


def test_the_problems_that_need_the_logs_or_a_look_at_the_app_point_at_general():
    problems = find_problems(None, [], launch(warned=True), card_failed=True)
    assert [(problem.key.split(":")[0], problem.section) for problem in problems] == [("launch", "general"), ("card", "general")]


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


@pytest.mark.parametrize("field", ["name", "owner", "last_error"])
@pytest.mark.parametrize("junk", [5, True, 1.5, ["me@example.com"], {"a": 1}])
def test_a_calendar_s_text_that_is_not_text_reads_as_missing(field, junk):
    feed = FeedStatus.from_json({"id": "a", field: junk})
    assert feed == FeedStatus("a")
    assert FeedStatus.from_json({"id": "a", field: "text"}) == FeedStatus("a", **{field: "text"})
    assert FeedStatus.from_json({"id": "a", field: None}) == FeedStatus("a")


def test_a_calendar_with_a_name_that_is_not_text_still_gets_a_label_and_a_problem():
    feed = FeedStatus.from_json({"id": "a", "name": ["Work"], "owner": 7, "failures": 3, "last_error": {"code": 404}})
    assert feed.label() == "A calendar" and feed.alerting
    # No reason, rather than the repr of whatever was in the file.
    assert feed.failure_detail() == "The last 3 downloads failed. Meetings on this calendar won't start notes until it's fixed."
    [problem] = find_problems(None, [feed], None)
    assert problem.title == "A calendar isn't updating"


@pytest.mark.parametrize(
    "raw",
    [
        "0001-01-01T00:00:00",  # nothing this PC's clock can show
        "9999-12-31T23:59:59",
        "0001-01-01T00:00:00+14:00",  # before the year 1 in UTC
        "9999-12-31T23:59:59-14:00",  # after the year 9999 in UTC
    ],
)
def test_a_saved_time_this_pc_cannot_show_is_no_time_at_all(raw):
    assert saved_moment(raw) is None
    status = Status.from_json(
        {
            "paused_until": raw,
            "next_meeting": {"title": "Design crit", "start": raw},
            "taking_notes": {"title": "Design crit", "since": raw},
            "feeds": [{"id": "a", "last_ok": raw}],
            "last_start": {"title": "Design crit", "at": raw},
        }
    )
    assert status == Status(feeds=[FeedStatus("a")])


def test_a_saved_time_is_kept_as_it_was_written_or_as_local_time_when_it_had_no_offset():
    written = "2026-10-30T09:00:00+02:00"
    assert saved_moment(written) == datetime.fromisoformat(written) and saved_moment(written).utcoffset() == timedelta(hours=2)
    assert saved_moment("2026-10-30T09:00") == datetime(2026, 10, 30, 9, 0).astimezone()
    for nothing in (None, "", 0, 5, "yesterday", ["2026-10-30"]):
        assert saved_moment(nothing) is None


def test_the_reason_an_update_download_was_cut_short_is_one_to_show_as_it_is():
    assert describe_error(ValueError("download was cut short")) == "download was cut short"
    assert describe_error(ValueError("download was cut short at https://example.com/x")) == "ValueError"


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
