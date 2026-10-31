import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from granola_autostart import processes, settings_app
from granola_autostart.settings import Settings, config_path, load_settings, status_path
from granola_autostart.status import FeedStatus, LastStart, Problem, Status, end_of_today, feed_id, write_status

GOOD = "https://calendar.google.com/calendar/ical/me/private-abc/basic.ics"
# The status box is checked at a fixed time, so a minute or a midnight can't move under the test.
FIXED = datetime(2026, 10, 30, 9, 0, tzinfo=timezone.utc)


def now() -> datetime:
    """For what the page reads against the clock (Test, "checked 2 minutes ago"): taken in the test."""
    return datetime.now().astimezone()


def ics(name: str, *events: tuple[str, datetime]) -> str:
    blocks = "".join(
        f"BEGIN:VEVENT\nUID:{i}@google.com\nSUMMARY:{title}\nDTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\n"
        f"DTEND:{(start + timedelta(minutes=30)).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\nEND:VEVENT\n"
        for i, (title, start) in enumerate(events)
    )
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nX-WR-CALNAME:{name}\n{blocks}END:VCALENDAR\n"


def write_raw_status(**status):
    status_path().write_text(json.dumps(status), encoding="utf-8")


@pytest.fixture
def api(monkeypatch, spawned):
    monkeypatch.setattr(processes, "watcher_running", lambda: True)
    monkeypatch.setattr(processes, "supervisor_running", lambda: True)
    served = {GOOD: ics("me@example.com", ("Design crit", now() + timedelta(hours=1)), ("Lunch", now() + timedelta(hours=2)))}

    def fetch(url, timeout=30):
        if url not in served:
            raise OSError("down")
        return served[url]

    monkeypatch.setattr(settings_app, "fetch_ics", fetch)
    return settings_app.Api("settings", "calendars")


def test_testing_an_address_shows_its_name_meetings_and_skips(api):
    result = api.test_address(GOOD.replace("https://", "webcal://"))
    assert result["ok"] and result["name"] == "me@example.com" and result["owner"] == "me@example.com"
    assert result["count"] == 1
    assert [(m["title"], m["skipped"]) for m in result["meetings"]] == [
        ("Design crit", None),
        ("Lunch", "Skipped, title contains “lunch”"),
    ]
    assert api.test_address("http://calendar.example/x.ics")["ok"] is False
    assert "couldn't" in api.test_address("https://calendar.example/missing.ics")["error"].lower()


def test_test_skips_exactly_what_the_watcher_skips_even_past_a_long_titles_cut():
    title = "Quarterly planning " + "x" * 200 + " lunch"
    result = settings_app.summarize_feed(ics("Work", (title, now() + timedelta(hours=1))), Settings(), [], now())
    assert result["count"] == 0
    assert result["meetings"][0]["skipped"] == "Skipped by a skip word"


def test_a_page_that_is_not_a_calendar_is_turned_away():
    result = settings_app.summarize_feed("<html>Sign in</html>", Settings(), [], now())
    assert result["ok"] is False and "iCal" in result["error"]


def test_adding_and_removing_a_calendar_saves_it_encrypted(api):
    view = api.add_calendar(GOOD)
    [calendar] = view["settings"]["calendars"]
    assert calendar["id"] == feed_id(GOOD) and calendar["provider"] == "Google Calendar" and calendar["state"] == "pending"
    assert GOOD not in config_path().read_text(encoding="utf-8")
    assert load_settings().calendar_urls() == [GOOD]
    assert api.test_calendar(calendar["id"])["ok"]
    assert api.remove_calendar(calendar["id"])["settings"]["calendars"] == []


def test_calendars_are_named_after_the_service_that_publishes_them():
    assert settings_app.provider("https://outlook.office365.com/owa/calendar/abc/calendar.ics") == "Outlook"
    assert settings_app.provider("https://calendar.example.org/feed.ics") == "calendar.example.org"
    assert settings_app.provider("not a url") == "Calendar"


def test_a_calendar_row_reports_what_the_watcher_saw(api):
    api.add_calendar(GOOD)
    feed = {"id": feed_id(GOOD), "name": "Work", "meetings_next_day": 7, "last_ok": (now() - timedelta(minutes=2)).isoformat()}
    write_raw_status(feeds=[feed, {"name": "an entry the app can't read"}])
    [calendar] = api.load()["settings"]["calendars"]
    assert (calendar["name"], calendar["state"], calendar["line"]) == ("Work", "ok", "7 meetings in the next day · checked 2 minutes ago")
    write_raw_status(feeds=[{**feed, "failures": 1, "last_error": "404 Not Found"}])
    [calendar] = api.load()["settings"]["calendars"]
    assert (calendar["state"], calendar["line"]) == ("pending", "The last download failed (404 Not Found). Trying again shortly.")
    write_raw_status(feeds=[{**feed, "failures": 3, "last_error": "404 Not Found"}])
    [calendar] = api.load()["settings"]["calendars"]
    assert calendar["state"] == "warn" and "(404 Not Found)" in calendar["line"]


def test_settings_reads_the_status_the_watcher_writes(api):
    api.add_calendar(GOOD)
    started = now() - timedelta(minutes=5)
    feeds = []
    for url, name in ((GOOD, "Work"), ("https://calendar.example/watch.ics", "Granola watch")):
        feeds.append(FeedStatus(feed_id(url)))
        feeds[-1].succeeded(now(), name=name, owner="me@example.com", meetings_next_day=2)
    write_status(status_path(), Status("test", taking_notes=("Design crit", started), feeds=feeds, last_start=LastStart("Design crit", started, True)))
    view = api.load()
    [calendar] = view["settings"]["calendars"]
    assert (calendar["name"], calendar["state"]) == ("Work", "ok")
    assert view["settings"]["detected_addresses"] == ["me@example.com"]  # once, though two calendars share it
    assert view["status"]["state"] == "notes" and view["status"]["detail"].startswith("Design crit, since ")
    assert (view["status"]["last_start"]["title"], view["status"]["last_start"]["opened"]) == ("Design crit", True)
    problem = Problem("feed:x", "“Work” calendar isn't updating", "The last 3 downloads failed.", "calendars")
    write_status(status_path(), Status("test", next_meeting=("Standup", now() + timedelta(hours=1)), problems=[problem]))
    status = api.load()["status"]
    assert (status["state"], status["detail"], status["fix_section"]) == ("attention", problem.title, "calendars")


def test_the_status_box_follows_the_watcher(api):
    def box(status=Status(), running=True, waiting=None):
        return api._status_view(status, running, FIXED, waiting=waiting)

    upcoming = box(Status(next_meeting=("Design crit", FIXED + timedelta(hours=1))))
    assert (upcoming["state"], upcoming["detail"]) == ("watching", "Next: Design crit at 10:00")
    assert box()["detail"] == "No meetings coming up. Zoom calls still start notes."
    assert box(Status(taking_notes=("Design crit", FIXED)))["state"] == "notes"
    paused = box(Status(paused_until=FIXED + timedelta(hours=1)))
    assert (paused["state"], paused["paused"], paused["paused_today"], paused["detail"]) == (
        "paused", True, False, "Until 10:00. No countdown cards until then."
    )
    today = box(Status(paused_until=end_of_today(FIXED)))
    assert today["paused_today"] and today["detail"].startswith("Until tomorrow")
    problem = Problem("config", "Settings couldn't be read", "", "calendars")
    attention = box(Status(paused_until=FIXED + timedelta(hours=1), problems=[problem]))
    assert (attention["state"], attention["fix_section"], attention["paused"]) == ("attention", "calendars", True)
    stopped = box(Status(paused_until=FIXED + timedelta(hours=1)), running=False)
    assert (stopped["state"], stopped["paused"]) == ("stopped", False)
    starting = box(running=False, waiting=2)
    assert (starting["state"], starting["detail"]) == ("starting", "Starting up.")
    crashing = box(running=False, waiting=settings_app.RESTARTING_SECONDS + 1)
    assert (crashing["state"], crashing["detail"]) == ("starting", "Restarting after a problem. The log folder says why.")


def test_a_supervisor_waiting_to_restart_the_watcher_reads_as_restarting(api, monkeypatch):
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    assert api.load()["status"]["state"] == "starting"
    monkeypatch.setattr(processes, "supervisor_running", lambda: False)
    assert api.load()["status"]["state"] == "stopped"


def test_starting_shows_until_the_new_watcher_reports(api, monkeypatch, spawned):
    write_raw_status()
    old = time.time() - 600
    os.utime(status_path(), (old, old))  # left by the watcher that was quit
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    monkeypatch.setattr(processes, "supervisor_running", lambda: False)
    assert api.start_watcher()["status"]["state"] == "starting"
    assert spawned[-1][-1] == "--supervise"
    monkeypatch.setattr(processes, "watcher_running", lambda: True)
    assert api.load()["status"]["state"] == "starting"
    fresh = time.time() + 1
    os.utime(status_path(), (fresh, fresh))  # the new watcher's first report
    assert api.load()["status"]["state"] == "watching"


def test_settings_only_starts_a_watcher_when_none_is_running(api, spawned):
    api.start_watcher()
    api.finish_setup(None, None)
    assert spawned == []


def test_starting_note_choices_are_validated(api):
    assert api.set_countdown(20)["settings"]["countdown_seconds"] == 20
    assert api.set_countdown(7)["settings"]["countdown_seconds"] == 20
    assert api.set_idle(10)["settings"]["idle_minutes"] == 10
    assert load_settings().idle_seconds == 600
    words = api.add_word("  Stand   up ")["settings"]["skip_words"]
    assert words[-1] == "Stand up"
    assert api.add_word("STAND UP")["settings"]["skip_words"] == words
    assert "lunch" not in api.remove_word("lunch")["settings"]["skip_words"]
    assert api.set_email("not an address")["error"]
    assert api.set_email("me@example.ca")["settings"]["user_email"] == "me@example.ca"


def test_setup_saves_the_calendar_and_marks_itself_done(api):
    api.finish_setup(GOOD, "me@example.com")
    settings = load_settings()
    assert settings.calendar_urls() == [GOOD]
    assert settings.user_email == "me@example.com"
    assert settings.setup_complete is True


def test_setup_done_turns_the_window_into_settings(api):
    api.page = "setup"
    api.finish_setup(GOOD, None)
    view = api.open_settings()
    assert (view["page"], view["section"]) == ("settings", "calendars")
    assert [c["id"] for c in view["settings"]["calendars"]] == [feed_id(GOOD)]


def test_zoom_only_setup_is_remembered(api):
    api.finish_setup(None, None)
    assert load_settings().setup_complete is True and load_settings().calendar_urls() == []


def test_a_config_that_cannot_be_read_shows_why_instead_of_the_settings(api):
    config_path().write_text("{ not json", encoding="utf-8")
    view = api.load()
    assert view["settings"] is None and view["config_error"].startswith("config.json is not valid JSON")


def test_preview_shows_the_card_without_launching_anything(api, spawned):
    api.preview()
    write_status(status_path(), Status(next_meeting=("Design crit", now() + timedelta(hours=1))))
    api.preview()
    assert all("--notice" in command for command in spawned)
    assert ["--title=Weekly product sync" in spawned[0], "--title=Design crit" in spawned[1]] == [True, True]


def test_relative_times():
    assert settings_app.ago(FIXED - timedelta(seconds=20), FIXED) == "just now"
    assert settings_app.ago(FIXED - timedelta(minutes=1), FIXED) == "1 minute ago"
    assert settings_app.ago(FIXED - timedelta(hours=3), FIXED) == "3 hours ago"


def test_pause_resume_and_quit_go_to_the_running_watcher(api, monkeypatch):
    sent = []
    monkeypatch.setattr(settings_app.commands, "send", lambda command: sent.append(command) or True)
    monkeypatch.setattr(settings_app, "_wait_for", lambda done, seconds=3: True)
    api.pause("hour")
    api.pause("today")
    api.pause("forever")
    api.resume()
    api.quit_app()
    assert sent == ["pause-hour", "pause-today", "resume", "quit"]


def test_test_skips_a_meeting_declined_by_another_calendars_owner():
    declined = ics("Granola watch", ("Design crit", now() + timedelta(hours=1))).replace(
        "END:VEVENT", "ATTENDEE;PARTSTAT=DECLINED:mailto:me@example.com\nEND:VEVENT"
    )
    assert settings_app.summarize_feed(declined, Settings(), [], now())["count"] == 1
    assert settings_app.summarize_feed(declined, Settings(), ["me@example.com"], now())["count"] == 0


def test_a_supervisor_left_waiting_reads_as_restarting_after_a_problem(api, monkeypatch):
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    assert api.load()["status"]["detail"] == "Starting up."
    api._waiting_since -= settings_app.RESTARTING_SECONDS + 1
    assert api.load()["status"]["detail"].startswith("Restarting after a problem")
    monkeypatch.setattr(processes, "watcher_running", lambda: True)
    api.load()
    assert api._waiting_since is None


def test_a_waiting_supervisor_is_left_to_start_the_watcher(api, monkeypatch, spawned):
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    api.start_watcher()
    assert spawned == []


def test_quit_waits_out_a_card_on_screen_and_the_supervisor(api, monkeypatch):
    waits = []
    monkeypatch.setattr(settings_app.commands, "send", lambda command: True)
    monkeypatch.setattr(settings_app, "_wait_for", lambda done, seconds=3: waits.append((done, seconds)) or True)
    api.quit_app()
    [(done, seconds)] = waits
    assert seconds == Settings().countdown_seconds + 15
    assert not done()
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    assert not done()
    monkeypatch.setattr(processes, "supervisor_running", lambda: False)
    assert done()


def test_setup_and_add_save_a_webcal_address_as_https(api):
    webcal = " " + GOOD.replace("https://", "webcal://") + " "
    api.finish_setup(webcal, None)
    assert load_settings().calendar_urls() == [GOOD]
    api.remove_calendar(api.load()["settings"]["calendars"][0]["id"])
    api.add_calendar(webcal)
    assert load_settings().calendar_urls() == [GOOD]
