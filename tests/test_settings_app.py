import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from granola_autostart import settings_app
from granola_autostart.status import feed_id
from granola_autostart.settings import load_settings

GOOD = "https://calendar.google.com/calendar/ical/me/private-abc/basic.ics"
NOW = datetime.now().astimezone()


def ics(name: str, *events: tuple[str, datetime]) -> str:
    blocks = "".join(
        f"BEGIN:VEVENT\nUID:{i}@google.com\nSUMMARY:{title}\nDTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\n"
        f"DTEND:{(start + timedelta(minutes=30)).astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\nEND:VEVENT\n"
        for i, (title, start) in enumerate(events)
    )
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nX-WR-CALNAME:{name}\n{blocks}END:VCALENDAR\n"


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    monkeypatch.setattr(settings_app, "watcher_running", lambda: True)
    spawned = []
    monkeypatch.setattr(settings_app, "_spawn", spawned.append)
    served = {GOOD: ics("me@example.com", ("Design crit", NOW + timedelta(hours=1)), ("Lunch", NOW + timedelta(hours=2)))}

    def fetch(url, timeout=30):
        if url not in served:
            raise OSError("down")
        return served[url]

    monkeypatch.setattr(settings_app, "fetch_ics", fetch)
    instance = settings_app.Api("settings", "calendars")
    instance.spawned = spawned
    return instance


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


def test_adding_and_removing_a_calendar_saves_it_encrypted(api, tmp_path):
    view = api.add_calendar(GOOD)
    [calendar] = view["settings"]["calendars"]
    assert calendar["id"] == feed_id(GOOD) and calendar["provider"] == "Google Calendar" and calendar["state"] == "pending"
    assert GOOD not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert load_settings().calendar_urls() == [GOOD]
    assert api.test_calendar(calendar["id"])["ok"]
    assert api.remove_calendar(calendar["id"])["settings"]["calendars"] == []


def test_a_calendar_row_reports_what_the_watcher_saw(api, tmp_path):
    api.add_calendar(GOOD)
    status = {"feeds": [{"id": feed_id(GOOD), "name": "Work", "owner": "me@example.com", "meetings_next_day": 7,
                         "last_ok": (NOW - timedelta(minutes=2)).isoformat(), "failures": 0, "last_error": None}]}
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    view = api.load()
    [calendar] = view["settings"]["calendars"]
    assert (calendar["name"], calendar["state"], calendar["line"]) == ("Work", "ok", "7 meetings in the next day · checked 2 minutes ago")
    assert view["settings"]["detected_addresses"] == ["me@example.com"]
    status["feeds"][0].update(failures=3, last_error="404 Not Found")
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")
    [calendar] = api.load()["settings"]["calendars"]
    assert calendar["state"] == "warn" and "(404 Not Found)" in calendar["line"]


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
    api.skip_setup()
    assert load_settings().setup_complete is True and load_settings().calendar_urls() == []


def test_preview_shows_the_card_without_launching_anything(api):
    api.preview()
    [command] = api.spawned
    assert "--notice" in command
    assert "--title=Weekly product sync" in command


def test_relative_times():
    assert settings_app.ago(NOW - timedelta(seconds=20), NOW) == "just now"
    assert settings_app.ago(NOW - timedelta(minutes=1), NOW) == "1 minute ago"
    assert settings_app.ago(NOW - timedelta(hours=3), NOW) == "3 hours ago"


def write_status(tmp_path, **status):
    (tmp_path / "status.json").write_text(json.dumps(status), encoding="utf-8")


def test_the_status_box_follows_the_watcher(api, tmp_path, monkeypatch):
    write_status(tmp_path, next_meeting={"title": "Design crit", "start": (NOW + timedelta(hours=1)).isoformat()})
    status = api.load()["status"]
    assert status["state"] == "watching" and status["detail"].startswith("Next: Design crit at ")
    write_status(tmp_path)
    assert api.load()["status"]["detail"] == "No meetings coming up. Zoom calls still start notes."
    write_status(tmp_path, taking_notes={"title": "Design crit", "since": NOW.isoformat()})
    assert api.load()["status"]["state"] == "notes"
    in_an_hour = (NOW + timedelta(hours=1)).isoformat()
    write_status(tmp_path, paused_until=in_an_hour)
    status = api.load()["status"]
    assert (status["state"], status["paused"], status["paused_today"]) == ("paused", True, False)
    write_status(tmp_path, paused_until=settings_app._midnight(datetime.now().astimezone()).isoformat())
    assert api.load()["status"]["detail"].startswith("Until tomorrow")
    write_status(tmp_path, paused_until=in_an_hour, problems=[{"key": "config", "title": "Settings couldn't be read", "section": "calendars"}])
    status = api.load()["status"]
    assert (status["state"], status["fix_section"], status["paused"]) == ("attention", "calendars", True)
    monkeypatch.setattr(settings_app, "watcher_running", lambda: False)
    status = api.load()["status"]
    assert (status["state"], status["paused"]) == ("stopped", False)


def test_starting_shows_until_the_new_watcher_reports(api, tmp_path, monkeypatch):
    write_status(tmp_path)
    old = time.time() - 600
    os.utime(tmp_path / "status.json", (old, old))  # left by the watcher that was quit
    monkeypatch.setattr(settings_app, "watcher_running", lambda: False)
    assert api.start_watcher()["status"]["state"] == "starting"
    assert api.spawned[-1][-1] == "--supervise"
    monkeypatch.setattr(settings_app, "watcher_running", lambda: True)
    assert api.load()["status"]["state"] == "starting"
    fresh = time.time() + 1
    os.utime(tmp_path / "status.json", (fresh, fresh))  # the new watcher's first report
    assert api.load()["status"]["state"] == "watching"


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
