import dataclasses
import inspect
import json
import logging
import math
import os
import re
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

import pytest

from granola_autostart import processes, settings_app, updates, winui
from granola_autostart.settings import Calendar, ConfigError, Settings, config_path, load_settings, save_settings, status_path, update_path, write_json
from granola_autostart.status import FeedStatus, LastStart, Problem, Status, end_of_today, feed_id, write_status

GOOD = "https://calendar.google.com/calendar/ical/me/private-abc/basic.ics"
WATCH = "https://calendar.google.com/calendar/ical/watch/private-def/basic.ics"
NOT_AN_ADDRESS = "That doesn't look like an email address."
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


def watcher_saw(*calendars: tuple[str, str, str | None]) -> None:
    """status.json as the watcher wrote it after downloading these calendars: (address, name, owner)."""
    feeds = []
    for url, name, owner in calendars:
        feeds.append(FeedStatus(feed_id(url)))
        feeds[-1].succeeded(now(), name=name, owner=owner, meetings_next_day=1)
    write_status(status_path(), Status("test", feeds=feeds))


def serving(monkeypatch, feeds: dict[str, str]) -> None:
    """Settings' downloads, faked: the feeds served by address, and a failure for any other."""

    def fetch(url, timeout=30):
        if url not in feeds:
            raise OSError("down")
        return feeds[url]

    monkeypatch.setattr(settings_app, "fetch_ics", fetch)


def declined_by(url: str):
    [calendar] = [calendar for calendar in load_settings().calendars if calendar.url == url]
    return calendar.declined_by


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
    assert "detected_addresses" not in view["settings"] and "user_email" not in view["settings"]
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
    assert box()["detail"] == "No meetings coming up. Calls still start notes."
    assert api._status_view(Status(), True, FIXED, calls=False)["detail"] == "No meetings coming up."
    assert box(Status(taking_notes=("Design crit", FIXED)))["state"] == "notes"
    paused = box(Status(paused_until=FIXED + timedelta(hours=1)))
    assert (paused["state"], paused["paused"], paused["paused_today"], paused["detail"]) == (
        "paused", True, False, "Until 10:00. No countdown cards until then."
    )
    today = box(Status(paused_until=end_of_today(FIXED)))
    assert today["paused_today"] and today["detail"] == "Until tomorrow. Calls joined meanwhile won't be asked about later."
    problem = Problem("config", "Settings couldn't be read", "", "calendars")
    attention = box(Status(paused_until=FIXED + timedelta(hours=1), problems=[problem]))
    assert (attention["state"], attention["fix_section"], attention["paused"]) == ("attention", "calendars", True)
    stopped = box(Status(paused_until=FIXED + timedelta(hours=1)), running=False)
    assert (stopped["state"], stopped["paused"]) == ("stopped", False)
    starting = box(running=False, waiting=2)
    assert (starting["state"], starting["detail"]) == ("starting", "Starting up.")
    crashing = box(running=False, waiting=settings_app.RESTARTING_SECONDS + 1)
    assert (crashing["state"], crashing["detail"]) == ("starting", "Restarting after a problem. The log folder says why.")


def test_the_status_box_says_off_over_a_pause_and_a_problem_but_not_over_a_watcher_that_is_not_running(api):
    def box(status, running=True, off=True):
        return api._status_view(status, running, FIXED, off=off)

    problem = Problem("config", "Settings couldn't be read", "", "calendars")
    paused = Status(paused_until=FIXED + timedelta(hours=1), problems=[problem])
    assert (box(paused)["state"], box(paused)["autostart_notes"]) == ("off", False)
    assert box(paused)["detail"] == "Meetings and calls won't start notes. It stays off after a restart, until you switch it on."
    assert box(paused, off=False)["state"] == "attention" and box(paused, off=False)["autostart_notes"] is True
    stopped = box(paused, running=False)
    assert (stopped["state"], stopped["detail"], stopped["autostart_notes"]) == ("stopped", "Meetings won't start notes.", False)
    assert api._status_view(paused, True, FIXED, waiting=2, off=True)["state"] == "starting"


def test_the_autostart_notes_switch_is_saved_in_config_json_and_the_status_box_follows_at_once(api):
    status = api.load()["status"]
    assert (status["state"], status["autostart_notes"]) == ("watching", True)
    view = api.set_autostart_notes(False)
    assert json.loads(config_path().read_text(encoding="utf-8"))["autostart_notes"] is False
    assert (view["status"]["state"], view["status"]["autostart_notes"]) == ("off", False)
    # The watcher hasn't noticed yet, and status.json is as it was: the saved switch is what the box shows.
    assert api.load()["status"]["state"] == "off"
    assert api.set_autostart_notes(True)["status"]["autostart_notes"] is True
    assert load_settings().autostart_notes is True


def test_the_switch_is_saved_without_touching_the_rest_of_the_settings(api):
    api.add_calendar(GOOD)
    api.set_countdown(20)
    before = json.loads(config_path().read_text(encoding="utf-8"))
    api.set_autostart_notes(False)
    assert json.loads(config_path().read_text(encoding="utf-8")) == {**before, "autostart_notes": False}
    assert load_settings().calendar_urls() == [GOOD]


def test_the_switch_still_shows_and_works_while_no_watcher_is_running(api, monkeypatch):
    monkeypatch.setattr(processes, "watcher_running", lambda: False)
    monkeypatch.setattr(processes, "supervisor_running", lambda: False)
    status = api.set_autostart_notes(False)["status"]
    assert (status["state"], status["detail"], status["autostart_notes"]) == ("stopped", "Meetings won't start notes.", False)
    assert load_settings().autostart_notes is False
    assert api.set_autostart_notes(True)["status"]["autostart_notes"] is True


def test_a_config_that_cannot_be_read_leaves_the_switch_to_what_the_watcher_last_reported(api):
    config_path().write_text("{ broken", encoding="utf-8")
    write_raw_status(off=True)
    status = api.load()["status"]
    assert (status["state"], status["autostart_notes"]) == ("off", False)
    write_raw_status(off=False)
    assert api.load()["status"]["autostart_notes"] is True


def test_the_switch_cannot_be_changed_while_the_config_cannot_be_read(api):
    config_path().write_text("{ broken", encoding="utf-8")
    with pytest.raises(ConfigError):
        api.set_autostart_notes(False)
    assert config_path().read_text(encoding="utf-8") == "{ broken"


def test_the_sign_in_switch_is_whether_the_run_value_exists(api, monkeypatch):
    run = {"on": False}
    monkeypatch.setattr(processes, "start_at_sign_in", lambda: run["on"])
    monkeypatch.setattr(processes, "set_start_at_sign_in", lambda on: run.update(on=on))
    assert api.load()["start_at_sign_in"] is False
    assert api.set_start_at_sign_in(True)["start_at_sign_in"] is True and run == {"on": True}
    assert api.set_start_at_sign_in(False)["start_at_sign_in"] is False and run == {"on": False}
    run["on"] = True  # added or removed outside the app
    assert api.load()["start_at_sign_in"] is True


def test_the_page_has_a_sign_in_switch_only_in_the_installed_app(api, monkeypatch):
    assert processes.FROZEN is False
    assert api.load()["sign_in_available"] is False  # from source there is no installed exe for the entry to start
    monkeypatch.setattr(processes, "FROZEN", True)
    assert api.load()["sign_in_available"] is True


def test_the_sign_in_switch_works_while_the_config_cannot_be_read(api, monkeypatch):
    config_path().write_text("{ broken", encoding="utf-8")
    run = {"on": False}
    monkeypatch.setattr(processes, "start_at_sign_in", lambda: run["on"])
    monkeypatch.setattr(processes, "set_start_at_sign_in", lambda on: run.update(on=on))
    view = api.set_start_at_sign_in(True)
    assert view["start_at_sign_in"] is True and view["config_error"]


def test_the_appearance_is_saved_and_the_page_follows_it_at_once(api, monkeypatch):
    monkeypatch.setattr(winui, "windows_theme", lambda: "light")
    view = api.load()
    assert (view["appearance"], view["theme"]) == ("auto", "light")
    view = api.set_appearance("dark")
    assert (view["appearance"], view["theme"]) == ("dark", "dark")
    assert json.loads(config_path().read_text(encoding="utf-8"))["appearance"] == "dark"
    assert api.set_appearance("light")["theme"] == "light"
    monkeypatch.setattr(winui, "windows_theme", lambda: "dark")
    assert api.load()["theme"] == "light"  # a choice ignores Windows
    view = api.set_appearance("auto")
    assert (view["appearance"], view["theme"]) == ("auto", "dark")  # and Auto follows it again
    assert api.set_appearance("sepia")["appearance"] == "auto"  # not on offer: nothing changes
    assert json.loads(config_path().read_text(encoding="utf-8"))["appearance"] == "auto"


def test_the_appearance_shown_is_the_saved_one_even_when_the_rest_of_the_config_cannot_be_loaded(api, monkeypatch):
    monkeypatch.setattr(winui, "windows_theme", lambda: "light")
    config_path().write_text(json.dumps({"appearance": "dark", "calendars": 5}), encoding="utf-8")
    view = api.load()
    assert view["config_error"] and view["settings"] is None
    assert (view["appearance"], view["theme"]) == ("dark", "dark")  # Auto would say the choice was lost


def test_the_appearance_is_saved_alone(api):
    api.add_calendar(GOOD)
    before = json.loads(config_path().read_text(encoding="utf-8"))
    api.set_appearance("dark")
    assert json.loads(config_path().read_text(encoding="utf-8")) == {**before, "appearance": "dark"}


def test_the_title_bar_is_styled_again_when_the_theme_changes_and_not_otherwise(api, monkeypatch):
    styled = []
    monkeypatch.setattr(settings_app, "_style_title_bar", lambda window, dark: styled.append((window, dark)))
    monkeypatch.setattr(winui, "windows_theme", lambda: "light")
    window = Window()
    api = settings_app.Api("settings", "calendars", "light")  # the theme the window was opened with
    api._window = window
    api.load()
    api.load()
    assert styled == []
    api.set_appearance("dark")
    api.load()
    assert styled == [(window, True)]
    api.set_appearance("auto")  # and Windows' own change shows up on Auto too
    assert styled == [(window, True), (window, False)]
    monkeypatch.setattr(winui, "windows_theme", lambda: "dark")
    api.load()
    assert styled == [(window, True), (window, False), (window, True)]


def test_the_window_opens_with_the_theme_it_was_given(api):
    assert settings_app.Api("settings", "calendars", "dark")._theme == "dark"
    assert settings_app.Api("setup", "calendars", theme="light")._theme == "light"
    assert settings_app.Api("settings", "calendars")._theme is None


def test_nothing_is_styled_before_the_window_exists(api, monkeypatch):
    monkeypatch.setattr(settings_app, "_style_title_bar", lambda window, dark: pytest.fail("styled a window that isn't there"))
    api.set_appearance("dark")
    assert api._theme is None
    api._window = Window()
    monkeypatch.setattr(settings_app, "_style_title_bar", lambda window, dark: None)
    api.load()
    assert api._theme == "dark"


def test_the_calls_page_lists_zoom_teams_and_meet_with_their_switches_and_whether_the_app_is_here(api):
    assert api.load()["settings"]["calls"] == [
        {"provider": "zoom", "name": "Zoom", "on": True, "found": True},
        {"provider": "teams", "name": "Microsoft Teams", "on": False, "found": False},
        {"provider": "meet", "name": "Google Meet", "on": False, "found": None},
    ]


def test_a_call_found_on_this_pc_is_not_switched_on_for_that(api, monkeypatch):
    monkeypatch.setattr(settings_app, "call_installed", lambda provider: provider == "teams" or None)
    assert [(call["provider"], call["on"], call["found"]) for call in api.load()["settings"]["calls"]] == [
        ("zoom", True, None),
        ("teams", False, True),
        ("meet", False, None),
    ]


def test_a_call_switch_is_saved_and_the_page_follows_at_once(api):
    view = api.set_call("teams", True)
    assert [(call["provider"], call["on"]) for call in view["settings"]["calls"]] == [("zoom", True), ("teams", True), ("meet", False)]
    assert json.loads(config_path().read_text(encoding="utf-8"))["calls"] == {"zoom": True, "teams": True, "meet": False}
    view = api.set_call("zoom", False)
    assert [call["on"] for call in view["settings"]["calls"]] == [False, True, False]
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": False}
    assert [call["on"] for call in api.set_call("teams", False)["settings"]["calls"]] == [False, False, False]


def test_a_call_switch_is_saved_without_touching_the_rest_of_the_settings(api):
    api.add_calendar(GOOD)
    api.set_countdown(20)
    before = load_settings()
    api.set_call("meet", True)
    assert load_settings() == dataclasses.replace(before, calls={"zoom": True, "teams": False, "meet": True})


def test_a_provider_this_version_does_not_know_is_ignored(api):
    api.load()  # writes config.json, the first time
    before = config_path().read_text(encoding="utf-8")
    view = api.set_call("webex", True)
    assert [call["provider"] for call in view["settings"]["calls"]] == ["zoom", "teams", "meet"]
    assert config_path().read_text(encoding="utf-8") == before


def argument_count(page: str, open_at: int) -> int | None:
    """How many arguments the call whose "(" is at ``open_at`` passes, or None when a spread makes that unknowable.
    Commas inside brackets, braces, and quotes belong to something else."""
    depth, commas, quote, body = 0, 0, None, ""
    for char in page[open_at:]:
        if quote:
            quote = None if char == quote else quote
        elif char in "([{":
            depth += 1
            if depth == 1:
                continue  # the call's own bracket
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif char == "," and depth == 1:
            commas += 1
        elif char in "'\"`":
            quote = char
        body += char
    if "..." in body:
        return None
    return commas + 1 if body.strip() else 0


def test_every_call_the_page_makes_is_a_method_of_the_api_with_a_matching_number_of_arguments():
    page = (settings_app.WEB / "app.js").read_text(encoding="utf-8")
    calls = list(re.finditer(r"\bapi\(\)\.(\w+)\(", page))
    called = {call[1] for call in calls}
    assert len(called) > 20  # the pattern still finds the page's calls
    public = {name for name in dir(settings_app.Api) if not name.startswith("_") and callable(getattr(settings_app.Api, name))}
    assert called <= public, f"app.js calls {sorted(called - public)}, which Api doesn't have"
    counted = 0
    for call in calls:
        count = argument_count(page, call.end() - 1)
        if count is None:
            continue
        counted += 1
        parameters = list(inspect.signature(getattr(settings_app.Api, call[1])).parameters.values())[1:]  # not self
        required = sum(1 for parameter in parameters if parameter.default is parameter.empty and parameter.kind is parameter.POSITIONAL_OR_KEYWORD)
        most = math.inf if any(parameter.kind is parameter.VAR_POSITIONAL for parameter in parameters) else len(parameters)
        assert required <= count <= most, f"app.js calls {call[1]} with {count} arguments; the Api takes {required} to {most}"
    assert counted > 20  # and the counting still reads them


def test_the_argument_counter_reads_calls_the_way_the_page_writes_them():
    assert argument_count("f()", 1) == 0
    assert argument_count("f( )", 1) == 0
    assert argument_count("f(a)", 1) == 1
    assert argument_count("f(a, b, c)", 1) == 3
    assert argument_count("f(g(a, b), [c, d], {e: 1, f: 2}, 'x, y', `p, q`)", 1) == 5
    assert argument_count('f(a, "it\'s, here") + g(b, c)', 1) == 2
    assert argument_count("f(...args)", 1) is None


def test_the_status_box_stops_saying_calls_start_notes_when_every_call_is_off(api):
    assert api.load()["status"]["detail"] == "No meetings coming up. Calls still start notes."
    api.set_call("zoom", False)
    assert api.load()["status"]["detail"] == "No meetings coming up."
    api.set_call("meet", True)
    assert api.load()["status"]["detail"] == "No meetings coming up. Calls still start notes."


@pytest.mark.parametrize(
    ("name", "section"),
    [("calendars", "calendars"), ("calls", "calls"), ("notes", "notes"), ("general", "general"), ("about", "general"), ("nonsense", "calendars")],
)
def test_a_section_name_opens_that_page_and_the_old_name_about_opens_general(name, section):
    assert settings_app.section_id(name) == section
    assert settings_app.Api("settings", name).section == section


def test_a_problem_saved_with_the_old_name_about_sends_fix_to_general(api):
    problem = Problem("card", "The countdown card couldn't open", "", "about")
    write_status(status_path(), Status("test", problems=[problem]))
    assert api.load()["status"]["fix_section"] == "general"
    write_status(status_path(), Status("test", problems=[Problem("feed:x", "A calendar isn't updating", "", "calendars")]))
    assert api.load()["status"]["fix_section"] == "calendars"


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
    assert not hasattr(api, "set_email")


def test_setup_saves_the_calendar_and_marks_itself_done(api):
    api.finish_setup(GOOD, None)
    settings = load_settings()
    assert settings.calendars == (Calendar(GOOD),)
    assert settings.setup_complete is True


def test_setup_done_turns_the_window_into_settings(api):
    api.page = "setup"
    api.finish_setup(GOOD, None)
    view = api.open_settings()
    assert (view["page"], view["section"]) == ("settings", "calendars")
    assert [c["id"] for c in view["settings"]["calendars"]] == [feed_id(GOOD)]


def test_calls_only_setup_is_remembered(api):
    api.finish_setup(None, None, {"zoom": True, "teams": False, "meet": False})
    assert load_settings().setup_complete is True and load_settings().calendar_urls() == []


def test_setup_saves_the_calls_chosen_with_a_calendar_and_without_one(api):
    api.finish_setup(GOOD, None, {"zoom": False, "teams": True, "meet": True})
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": True}
    assert load_settings().calendar_urls() == [GOOD]
    view = api.finish_setup(None, None, {"zoom": True, "teams": False, "meet": False})
    assert load_settings().calls == {"zoom": True, "teams": False, "meet": False}
    assert [call["on"] for call in view["settings"]["calls"]] == [True, False, False]
    assert load_settings().calendar_urls() == [GOOD]


def test_setup_without_call_choices_leaves_the_saved_ones_as_they_are(api):
    api.set_call("teams", True)
    api.finish_setup(None, None)
    assert load_settings().calls == {"zoom": True, "teams": True, "meet": False}
    api.finish_setup(None, None, None)
    assert load_settings().calls == {"zoom": True, "teams": True, "meet": False}


def test_setup_keeps_a_call_it_was_not_told_about_and_ignores_providers_and_values_it_does_not_know(api):
    api.set_call("teams", True)
    api.finish_setup(None, None, {"zoom": False, "webex": True, "meet": "yes"})
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": False}
    api.finish_setup(None, None, ["zoom"])
    api.finish_setup(None, None, "teams")
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": False}


def test_a_config_that_cannot_be_read_shows_why_instead_of_the_settings(api):
    config_path().write_text("{ not json", encoding="utf-8")
    view = api.load()
    assert view["settings"] is None and view["config_error"].startswith("config.json is not valid JSON")


def test_the_owners_are_worked_out_once_however_many_calendars_there_are(api, monkeypatch):
    for url in (GOOD, WATCH, "https://calendar.google.com/calendar/ical/third/private-ghi/basic.ics"):
        api.add_calendar(url)
    owned = []
    real = settings_app._owners
    monkeypatch.setattr(settings_app, "_owners", lambda settings, feeds: owned.append(1) or real(settings, feeds))
    assert len(api.load()["settings"]["calendars"]) == 3
    assert owned == [1]


def test_a_calendar_row_says_whether_it_is_on_and_which_addresses_its_declines_use(api):
    api.add_calendar(GOOD)
    api.add_calendar(WATCH)
    watcher_saw((GOOD, "Work", "me@example.com"), (WATCH, "Granola watch", None))
    work, watch = api.load()["settings"]["calendars"]
    assert set(work) == {"id", "name", "provider", "state", "line", "enabled", "declined", "automatic", "note", "older", "lent"}
    assert (work["enabled"], work["automatic"], work["note"], work["declined"]) == (True, True, "own", ["me@example.com"])
    # A calendar that names no address borrows the other calendars' owners.
    assert (watch["enabled"], watch["automatic"], watch["note"], watch["declined"]) == (True, True, "copied", ["me@example.com"])
    assert (work["older"], watch["older"]) == (False, False)
    assert (work["lent"], watch["lent"]) == (0, 1)  # only a "copied" note counts the calendars that lent
    save_settings(dataclasses.replace(load_settings(), user_email="Old@Example.com"))
    work, watch = api.load()["settings"]["calendars"]
    assert work["declined"] == ["me@example.com", "old@example.com"] and watch["declined"] == ["me@example.com", "old@example.com"]
    assert (work["note"], watch["note"]) == ("own", "copied")
    assert (work["older"], watch["older"]) == (True, True)  # the old address is a chip the note has to account for
    assert watch["lent"] == 1  # but not a calendar that lent one
    api.add_declined(watch["id"], "alias@example.ca")
    watch = api.load()["settings"]["calendars"][1]
    assert (watch["automatic"], watch["note"], watch["declined"]) == (False, "list", ["me@example.com", "old@example.com", "alias@example.ca"])
    assert (watch["older"], watch["lent"]) == (False, 0)  # a list is what was typed, whatever an older version saved


def test_a_calendar_that_names_no_address_and_has_no_others_shows_no_addresses(api):
    api.add_calendar(WATCH)
    watcher_saw((WATCH, "Granola watch", None))
    [watch] = api.load()["settings"]["calendars"]
    assert (watch["declined"], watch["automatic"], watch["note"]) == ([], True, "none")


def test_a_calendar_that_names_no_address_and_has_no_others_keeps_the_address_an_older_version_saved(api):
    api.add_calendar(WATCH)
    watcher_saw((WATCH, "Granola watch", None))
    save_settings(dataclasses.replace(load_settings(), user_email="Old@Example.com"))
    [watch] = api.load()["settings"]["calendars"]
    assert (watch["declined"], watch["automatic"], watch["note"]) == (["old@example.com"], True, "kept")


def test_a_calendar_that_has_not_downloaded_yet_has_no_address_to_show_so_it_is_pending(api):
    api.add_calendar(WATCH)
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    [watch] = api.load()["settings"]["calendars"]  # the watcher has not written a status for it
    assert (watch["automatic"], watch["note"]) == (True, "pending")
    # Nor has one whose downloads have only failed so far, whatever else is known.
    api.add_calendar(GOOD)
    write_raw_status(feeds=[{"id": feed_id(WATCH), "failures": 2, "last_error": "404 Not Found"}, {"id": feed_id(GOOD), "owner": "me@example.com", "last_ok": now().isoformat()}])
    watch, work = api.load()["settings"]["calendars"]
    assert (watch["note"], work["note"]) == ("pending", "own")
    # Once it has downloaded, the note says where its addresses come from.
    watcher_saw((WATCH, "Granola watch", None), (GOOD, "Work", "me@example.com"))
    assert api.load()["settings"]["calendars"][0]["note"] == "copied"
    # One with a list of its own is never pending: what was typed needs no download to be known.
    third = "https://calendar.google.com/calendar/ical/third/private-ghi/basic.ics"
    api.add_calendar(third, ["alias@example.ca"])
    assert api.load()["settings"]["calendars"][-1]["note"] == "list"


def test_an_off_calendar_that_has_never_downloaded_is_off_not_pending(api):
    api.add_calendar(WATCH)
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    [watch] = api.set_calendar_enabled(feed_id(WATCH), False)["settings"]["calendars"]
    assert (watch["state"], watch["note"], watch["older"]) == ("off", "off", False)  # it won't download until it is switched on
    [watch] = api.set_calendar_enabled(feed_id(WATCH), True)["settings"]["calendars"]
    assert (watch["state"], watch["note"]) == ("pending", "pending")
    # One that downloaded before it was switched off knows its addresses, so it shows them as any other does.
    watcher_saw((WATCH, "Granola watch", None))
    [watch] = api.set_calendar_enabled(feed_id(WATCH), False)["settings"]["calendars"]
    assert (watch["state"], watch["note"], watch["declined"]) == ("off", "kept", ["old@example.com"])


def test_an_off_calendar_that_downloaded_before_a_restart_is_off_too_so_the_page_cannot_say_it_was_never_read(api):
    api.add_calendar(WATCH)
    api.set_calendar_enabled(feed_id(WATCH), False)
    # After a restart the watcher knows the name it saw, but not that it downloaded: last_ok isn't kept, and the calendar
    # names no address, so Settings can't tell it from one that was never read.
    write_raw_status(feeds=[{"id": feed_id(WATCH), "name": "Granola watch", "enabled": False}])
    [watch] = api.load()["settings"]["calendars"]
    assert (watch["name"], watch["state"], watch["note"]) == ("Granola watch", "off", "off")
    page = (settings_app.WEB / "app.js").read_text(encoding="utf-8")
    off_copy = re.search(r'^  off: "(.*)",$', page, re.M)[1]
    assert "read" not in off_copy and "never" not in off_copy  # so the page can't say the calendar was never read


def test_the_old_address_is_said_to_add_a_chip_only_when_it_adds_one_to_the_calendar_s_own_or_borrowed_addresses():
    adds = settings_app.adds_older_address
    assert adds(None, "me@example.com", ["me@example.com"], "old@example.com") is True
    assert adds(None, None, ["me@example.com", None], "old@example.com") is True
    assert adds(None, "me@example.com", [None], "ME@example.com") is False  # it is the calendar's own address already
    assert adds(None, None, ["me@example.com"], "Me@Example.com") is False  # or a borrowed one
    assert adds(None, "me@example.com", ["me@example.com"], "") is False  # nothing saved by an older version
    assert adds(None, None, [None], "old@example.com") is False  # alone, which the "kept" note says
    assert adds(None, None, [], "old@example.com") is False
    assert adds(("alias@example.ca",), "me@example.com", ["me@example.com"], "old@example.com") is False  # a list is as typed
    assert adds(None, "me@example.com", iter(["me@example.com"]), "old@example.com") is True  # whatever the others come in


def test_the_note_says_where_a_calendar_s_addresses_come_from():
    note = settings_app.address_note
    assert note(("a@example.com",), "me@example.com", [None], "old@example.com") == "list"  # typed in, whatever else is known
    assert note(None, "me@example.com", ["me@example.com"], "") == "own"
    assert note(None, "me@example.com", [None], "old@example.com") == "own"  # the old address is added to its own
    assert note(None, None, ["me@example.com", None], "") == "copied"
    assert note(None, None, ["me@example.com"], "old@example.com") == "copied"
    assert note(None, None, [None, None], "old@example.com") == "kept"
    assert note(None, None, [], "") == "none"


def test_a_copied_note_counts_the_calendars_that_lent_an_address_not_the_addresses():
    lent = settings_app.calendars_lent
    assert lent("copied", ["me@example.com", None]) == 1
    assert lent("copied", ["me@example.com", "you@example.com", None]) == 2
    assert lent("copied", ["me@example.com", "me@example.com"]) == 2  # one address, from two calendars
    assert lent("copied", iter(["me@example.com", "you@example.com"])) == 2  # whatever the others come in
    assert lent("own", ["me@example.com", "you@example.com"]) == 0  # no other note says it
    assert lent("kept", [None]) == 0 and lent("none", []) == 0 and lent("list", ["me@example.com"]) == 0


def test_a_calendar_row_says_how_many_calendars_lent_it_an_address(api):
    third = "https://calendar.google.com/calendar/ical/third/private-ghi/basic.ics"
    for url in (GOOD, WATCH, third):
        api.add_calendar(url)
    watcher_saw((GOOD, "Work", "me@example.com"), (WATCH, "Granola watch", None), (third, "Home", "me@example.com"))
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    work, watch, home = api.load()["settings"]["calendars"]
    # Two calendars lent the same address, so there is one chip for them, and the older version's makes two chips.
    assert (watch["note"], watch["declined"], watch["older"], watch["lent"]) == ("copied", ["me@example.com", "old@example.com"], True, 2)
    assert (work["lent"], home["lent"]) == (0, 0)


def test_the_page_has_words_for_every_note_python_can_send():
    page = (settings_app.WEB / "app.js").read_text(encoding="utf-8")
    block = re.search(r"const NOTES = \{(.*?)\n\};", page, re.S)[1]
    assert set(re.findall(r"^  (\w+):", block, re.M)) == {"own", "list", "copied", "kept", "none", "pending", "off"}


def test_switching_a_calendar_off_keeps_its_name_and_says_its_meetings_wont_start_notes(api):
    api.add_calendar(GOOD)
    api.add_calendar(WATCH)
    watcher_saw((GOOD, "Work", "me@example.com"), (WATCH, "Granola watch", None))
    # An off calendar is listed last, so Work follows Granola watch while it is off.
    watch, work = api.set_calendar_enabled(feed_id(GOOD), False)["settings"]["calendars"]
    assert (work["enabled"], work["state"], work["line"], work["name"]) == (False, "off", "Off. Its meetings won't start notes.", "Work")
    assert (watch["enabled"], watch["state"]) == (True, "ok")
    assert [(calendar.url, calendar.enabled) for calendar in load_settings().calendars] == [(GOOD, False), (WATCH, True)]
    work = api.set_calendar_enabled(feed_id(GOOD), True)["settings"]["calendars"][0]
    assert (work["enabled"], work["state"]) == (True, "ok")
    assert api.set_calendar_enabled(feed_id(GOOD), 0)["settings"]["calendars"][-1]["enabled"] is False  # whatever the page sends
    assert api.set_calendar_enabled("not-a-calendar", False)["settings"]["calendars"][0]["enabled"] is True  # one that has gone


def test_calendars_needing_attention_come_first_and_ones_that_are_off_last_the_rest_as_added(api):
    urls = [f"https://calendar.google.com/calendar/ical/{name}/private-abc/basic.ics" for name in ("a", "b", "c", "d", "e")]
    for url in urls:
        api.add_calendar(url)
    ids = [feed_id(url) for url in urls]

    def listed() -> list[str]:
        return [calendar["id"] for calendar in api.load()["settings"]["calendars"]]

    # Nothing checked yet: every row is still pending, so they stay in the order they were added.
    assert listed() == ids
    broken = {"failures": 3, "last_error": "404 Not Found"}
    feeds = [{"id": id, "name": name, "meetings_next_day": 1, "last_ok": now().isoformat(), **(broken if name in "ce" else {})} for id, name in zip(ids, "abcde")]
    write_raw_status(feeds=feeds)
    states = {calendar["id"]: calendar["state"] for calendar in api.load()["settings"]["calendars"]}
    assert [states[id] for id in ids] == ["ok", "ok", "warn", "ok", "warn"]
    # Attention first, in the order added, then the rest in the order added.
    assert listed() == [ids[2], ids[4], ids[0], ids[1], ids[3]]
    # Off goes last, even for a calendar that needed attention: its problem no longer applies.
    api.set_calendar_enabled(ids[0], False)
    api.set_calendar_enabled(ids[2], False)
    assert listed() == [ids[4], ids[1], ids[3], ids[0], ids[2]]
    # Turning one back on puts it back among the others, and the stored order never changes.
    api.set_calendar_enabled(ids[0], True)
    assert listed() == [ids[4], ids[0], ids[1], ids[3], ids[2]]
    assert [calendar.url for calendar in load_settings().calendars] == urls


def test_a_calendar_that_is_off_keeps_the_rest_of_its_settings(api):
    api.add_calendar(GOOD, ["me@example.com", "alias@example.ca"])
    api.set_calendar_enabled(feed_id(GOOD), False)
    assert load_settings().calendars == (Calendar(GOOD, False, ("me@example.com", "alias@example.ca")),)


def test_adding_a_declined_address_starts_an_automatic_calendar_from_its_automatic_addresses(api):
    api.add_calendar(GOOD)
    watcher_saw((GOOD, "Work", "me@example.com"))
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    view = api.add_declined(feed_id(GOOD), " Alias@Example.ca ")
    [calendar] = view["settings"]["calendars"]
    assert (calendar["declined"], calendar["automatic"]) == (["me@example.com", "old@example.com", "Alias@Example.ca"], False)
    assert declined_by(GOOD) == ("me@example.com", "old@example.com", "Alias@Example.ca")
    assert "error" not in view


def test_an_address_that_is_already_there_is_ignored_ignoring_case(api):
    api.add_calendar(GOOD)
    watcher_saw((GOOD, "Work", "me@example.com"))
    assert api.add_declined(feed_id(GOOD), "ME@example.com")["settings"]["calendars"][0]["automatic"]  # still automatic
    assert declined_by(GOOD) is None
    api.add_declined(feed_id(GOOD), "alias@example.ca")
    view = api.add_declined(feed_id(GOOD), "Alias@Example.CA")
    assert view["settings"]["calendars"][0]["declined"] == ["me@example.com", "alias@example.ca"] and "error" not in view
    assert declined_by(GOOD) == ("me@example.com", "alias@example.ca")


def test_a_declined_address_must_look_like_an_email_address(api):
    api.add_calendar(GOOD)
    for bad in ("not an address", "me@", "me@example", "me @example.com", "", "   ", None):
        view = api.add_declined(feed_id(GOOD), bad)
        assert view["error"] == NOT_AN_ADDRESS
        assert [calendar["id"] for calendar in view["settings"]["calendars"]] == [feed_id(GOOD)]
    assert declined_by(GOOD) is None


def test_adding_or_removing_an_address_for_a_calendar_that_has_gone_changes_nothing(api):
    api.add_calendar(GOOD)
    before = api.load()["settings"]
    assert api.add_declined("not-a-calendar", "me@example.com")["settings"] == before
    assert api.remove_declined("not-a-calendar", "me@example.com")["settings"] == before


def test_removing_an_address_leaves_the_others_and_the_last_one_goes_back_to_automatic(api):
    api.add_calendar(GOOD, ["me@example.com", "alias@example.ca"])
    assert declined_by(GOOD) == ("me@example.com", "alias@example.ca")
    view = api.remove_declined(feed_id(GOOD), " ALIAS@example.ca ")
    assert view["settings"]["calendars"][0]["declined"] == ["me@example.com"] and declined_by(GOOD) == ("me@example.com",)
    assert api.remove_declined(feed_id(GOOD), "nobody@example.com")["settings"] == view["settings"]
    watcher_saw((GOOD, "Work", "me@example.com"))
    [calendar] = api.remove_declined(feed_id(GOOD), "me@example.com")["settings"]["calendars"]
    assert declined_by(GOOD) is None and calendar["automatic"] and calendar["note"] == "own"


def test_removing_an_address_from_an_automatic_calendar_starts_from_its_automatic_addresses(api):
    api.add_calendar(GOOD)
    watcher_saw((GOOD, "Work", "me@example.com"))
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    [calendar] = api.remove_declined(feed_id(GOOD), "old@example.com")["settings"]["calendars"]
    assert (calendar["declined"], calendar["automatic"]) == (["me@example.com"], False)
    assert declined_by(GOOD) == ("me@example.com",)


def test_a_declined_address_a_removed_calendar_had_is_not_lent_to_the_others(api):
    api.add_calendar(GOOD)
    api.add_calendar(WATCH)
    watcher_saw((GOOD, "Work", "me@example.com"), (WATCH, "Granola watch", None))
    api.remove_calendar(feed_id(GOOD))  # status.json still has it until the watcher's next tick
    [watch] = api.load()["settings"]["calendars"]
    assert watch["declined"] == []


def test_testing_an_address_suggests_the_addresses_declines_would_use(api, monkeypatch):
    own = api.test_address(GOOD)
    assert (own["owner"], own["declined"], own["older"], own["note"], own["lent"]) == ("me@example.com", ["me@example.com"], False, "own", 0)
    assert "copied" not in own  # the note says it
    serving(monkeypatch, {WATCH: ics("Granola watch")})
    alone = api.test_address(WATCH)
    assert (alone["owner"], alone["declined"], alone["older"], alone["note"]) == (None, [], False, "none")
    save_settings(dataclasses.replace(load_settings(), user_email="old@example.com"))
    kept = api.test_address(WATCH)
    assert (kept["declined"], kept["older"], kept["note"]) == (["old@example.com"], False, "kept")  # not from a calendar
    api.add_calendar(GOOD)
    watcher_saw((GOOD, "Work", "me@example.com"))
    borrowed = api.test_address(WATCH)
    assert (borrowed["declined"], borrowed["older"], borrowed["note"]) == (["me@example.com", "old@example.com"], True, "copied")
    assert borrowed["lent"] == 1  # one calendar, though there are two addresses


def test_testing_a_calendar_that_is_added_filters_with_its_own_declined_addresses(api, monkeypatch):
    declined_by_alias = ics("me@example.com", ("Pottery", now() + timedelta(hours=1))).replace(
        "END:VEVENT", "ATTENDEE;PARTSTAT=DECLINED:mailto:alias@example.ca\nEND:VEVENT"
    )
    serving(monkeypatch, {GOOD: declined_by_alias})
    api.add_calendar(GOOD)
    watcher_saw((GOOD, "Work", "me@example.com"))
    automatic = api.test_calendar(feed_id(GOOD))
    assert (automatic["count"], automatic["declined"]) == (1, ["me@example.com"])  # the alias isn't one of its addresses yet
    api.add_declined(feed_id(GOOD), "alias@example.ca")
    listed = api.test_calendar(feed_id(GOOD))
    assert (listed["count"], listed["declined"], listed["older"], listed["note"]) == (0, ["me@example.com", "alias@example.ca"], False, "list")


def no_more_downloads(monkeypatch) -> None:
    """Adding a calendar saves what the page sent: it never downloads the calendar again to second-guess it."""
    monkeypatch.setattr(settings_app, "fetch_ics", lambda url, timeout=30: pytest.fail("the calendar was downloaded again"))


def test_a_calendar_the_page_sends_no_list_for_is_added_automatic(api, monkeypatch):
    # The page sends None while the editor holds what Test suggested, or nothing at all.
    no_more_downloads(monkeypatch)
    api.add_calendar(GOOD, None)
    assert load_settings().calendars == (Calendar(GOOD),)
    api.remove_calendar(feed_id(GOOD))
    api.add_calendar(GOOD, [])  # every address removed in the editor
    assert declined_by(GOOD) is None
    api.remove_calendar(feed_id(GOOD))
    api.add_calendar(GOOD)
    assert declined_by(GOOD) is None


def test_a_calendar_added_with_a_list_keeps_it_as_the_page_sent_it_whatever_automatic_would_use(api, monkeypatch):
    no_more_downloads(monkeypatch)
    api.add_calendar(GOOD, ["ME@example.com"])  # the page sends None for what Test suggested, so this is a choice
    assert declined_by(GOOD) == ("ME@example.com",)
    api.remove_calendar(feed_id(GOOD))
    api.add_calendar(GOOD, [" me@example.com ", "Alias@Example.ca", "alias@example.ca"])
    assert declined_by(GOOD) == ("me@example.com", "Alias@Example.ca")
    api.remove_calendar(feed_id(GOOD))
    api.add_calendar(GOOD, ["alias@example.ca"])
    assert declined_by(GOOD) == ("alias@example.ca",)


def test_a_calendar_is_not_added_when_one_of_its_addresses_is_not_an_address(api):
    view = api.add_calendar(GOOD, ["me@example.com", "not an address"])
    assert view["error"] == NOT_AN_ADDRESS and view["settings"]["calendars"] == []
    assert load_settings().calendars == ()


def test_adding_a_calendar_that_is_already_there_keeps_its_settings(api):
    api.add_calendar(GOOD, ["alias@example.ca"])
    api.set_calendar_enabled(feed_id(GOOD), False)
    api.add_calendar(GOOD.replace("https://", "webcal://"), ["other@example.com"])
    assert load_settings().calendars == (Calendar(GOOD, False, ("alias@example.ca",)),)


def test_removing_a_calendar_leaves_the_others_as_they_are(api):
    api.add_calendar(GOOD, ["alias@example.ca"])
    api.add_calendar(WATCH)
    api.set_calendar_enabled(feed_id(WATCH), False)
    api.remove_calendar(feed_id(GOOD))
    assert load_settings().calendars == (Calendar(WATCH, False, None),)


def test_setup_keeps_the_addresses_chosen_and_leaves_a_calendar_automatic_when_none_are_sent(api, monkeypatch):
    no_more_downloads(monkeypatch)
    api.finish_setup(GOOD, None)
    assert load_settings().calendars == (Calendar(GOOD),)
    api.remove_calendar(feed_id(GOOD))
    view = api.finish_setup(GOOD, ["me@example.com", "alias@example.ca"])
    assert load_settings().calendars == (Calendar(GOOD, True, ("me@example.com", "alias@example.ca")),) and "error" not in view
    assert load_settings().setup_complete
    api.remove_calendar(feed_id(GOOD))
    api.finish_setup(GOOD, ["me@example.com"])  # the page sends None for what Test suggested, so this is a choice
    assert declined_by(GOOD) == ("me@example.com",)


def test_setup_turns_a_bad_address_back_before_it_finishes(api):
    view = api.finish_setup(GOOD, ["not an address"])
    assert view["error"] == NOT_AN_ADDRESS
    assert load_settings().calendars == () and load_settings().setup_complete is False


def test_setting_up_again_with_a_calendar_that_is_already_there_leaves_it_as_it_is(api):
    api.add_calendar(GOOD, ["alias@example.ca"])
    api.finish_setup(GOOD, ["me@example.com"])
    assert load_settings().calendars == (Calendar(GOOD, True, ("alias@example.ca",)),)


def test_calls_only_setup_ignores_any_addresses(api):
    api.finish_setup(None, ["me@example.com", "not an address"], {"zoom": True})
    assert load_settings().calendars == () and load_settings().setup_complete is True


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


RELEASE = updates.Release(
    "1.2.0",
    "https://github.com/cfoucher/granola-autostart/releases/download/v1.2.0/GranolaAutostart-1.2.0-setup.exe",
    26,
    "0" * 64,
    "https://github.com/cfoucher/granola-autostart/releases/tag/v1.2.0",
)


@pytest.fixture
def installed(monkeypatch):
    """The installed app, on 1.1.0: the only kind that checks for updates."""
    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "__version__", "1.1.0")


def save_update(**fields) -> None:
    write_json(update_path(), updates.UpdateState(**fields).to_json())


def test_the_general_page_reads_the_update_from_update_json(api, installed):
    assert api.load()["update"] == {
        "source": False,
        "available": None,
        "page": None,
        "checked": None,
        "error": None,
        "install_error": None,
        "card_showing": False,
        "installing": False,
        "install_wait_seconds": 120,
    }
    save_update(checked_at=now(), latest=RELEASE, error="Couldn't check for updates (timed out). Trying again tomorrow.", install_error=updates.MISMATCH)
    view = api.load()["update"]
    assert (view["available"], view["page"]) == ("1.2.0", RELEASE.page)
    assert view["checked"].startswith(("today at ", "on ")) and view["error"].startswith("Couldn't check") and view["install_error"] == updates.MISMATCH
    save_update(latest=updates.Release("1.1.0", RELEASE.url, 26, "0" * 64, ""))
    assert api.load()["update"]["available"] is None  # that is the version already running


def test_a_failed_install_is_shown_only_while_that_update_is_still_on_offer(api, installed, monkeypatch):
    save_update(latest=RELEASE, install_error=updates.MISMATCH)
    assert api.load()["update"]["install_error"] == updates.MISMATCH
    monkeypatch.setattr(updates, "__version__", "1.2.0")  # the app restarted on that version, by another way
    view = api.load()["update"]
    assert view["available"] is None and view["install_error"] is None
    save_update(latest=None, install_error=updates.MISMATCH)  # a release that has since lost its installer
    assert api.load()["update"]["install_error"] is None
    # A failed check is its own message and doesn't depend on there being an update.
    save_update(error="Couldn't check for updates (timed out). Trying again tomorrow.", install_error=updates.MISMATCH)
    assert api.load()["update"]["error"].startswith("Couldn't check")


def test_a_countdown_card_on_screen_shows_in_the_update_view(api, installed):
    assert processes.hold_mutex(processes.CARD_MUTEX)
    try:
        assert api.load()["update"]["card_showing"] is True
    finally:
        processes.release_mutex(processes.CARD_MUTEX)


def test_running_from_source_says_updates_come_from_git(api):
    assert api.load()["update"]["source"] is True


def test_when_the_update_was_checked_reads_as_a_day_and_time():
    state = updates.UpdateState(checked_at=FIXED - timedelta(hours=3))
    assert settings_app.update_view(state, FIXED)["checked"] == "today at 06:00"
    assert settings_app.update_view(updates.UpdateState(checked_at=FIXED - timedelta(days=1)), FIXED)["checked"] == "on Thu 09:00"
    assert settings_app.update_view(updates.UpdateState(checked_at=FIXED - timedelta(days=10)), FIXED)["checked"] == "on 20 Oct 09:00"
    assert settings_app.update_view(updates.UpdateState(), FIXED)["checked"] is None


def test_check_now_checks_and_returns_the_new_view(api, installed, monkeypatch):
    checked = []
    monkeypatch.setattr(updates, "check", lambda at: checked.append(at) or save_update(checked_at=at, latest=RELEASE))
    view = api.check_updates()
    assert len(checked) == 1 and checked[0].tzinfo is not None
    assert view["update"]["available"] == "1.2.0" and view["update"]["checked"].startswith(("today at ", "on "))


def test_check_now_does_nothing_from_source(api, monkeypatch):
    monkeypatch.setattr(updates, "check", lambda at: pytest.fail("checked from source"))
    assert api.check_updates()["update"]["source"] is True


def test_install_update_installs_the_release_on_offer_and_returns_the_new_view(api, installed, monkeypatch):
    save_update(latest=RELEASE)
    installs = []

    def install(release):
        installs.append(release)
        updates._record_install_error(updates.MISMATCH)
        return updates.Install.FAILED

    monkeypatch.setattr(updates, "install", install)
    view = api.install_update()
    assert installs == [RELEASE] and view["update"]["install_error"] == updates.MISMATCH
    assert view["update"]["installing"] is False


def test_the_page_is_told_to_say_installing_only_once_an_installer_is_running(api, installed, monkeypatch):
    save_update(latest=RELEASE)
    for outcome, installing in (
        (updates.Install.STARTED, True),
        (updates.Install.FAILED, False),
        (updates.Install.LATER, False),  # a countdown card came up during the download, or the tray is installing
    ):
        monkeypatch.setattr(updates, "install", lambda release, outcome=outcome: outcome)
        view = api.install_update()
        assert view["update"]["installing"] is installing
        assert view["update"]["install_wait_seconds"] == updates.INSTALL_WAIT_SECONDS == 120
    assert api.load()["update"]["installing"] is False  # with no install under way, only the answer to a click says so


def test_another_install_under_way_shows_in_the_update_view_and_in_the_answer_to_install_update(api, installed, monkeypatch):
    save_update(latest=RELEASE)
    # The tray, or another Settings window, holds the install mutex while it downloads and starts the installer.
    assert processes.hold_mutex(processes.INSTALL_MUTEX)
    try:
        assert api.load()["update"]["installing"] is True
        monkeypatch.setattr(updates, "install", lambda release: updates.Install.LATER)  # what a click gets: it is already being installed
        assert api.install_update()["update"]["installing"] is True
        monkeypatch.setattr(updates, "install", lambda release: updates.Install.FAILED)
        assert api.install_update()["update"]["installing"] is True  # whatever this click did, the other install goes on
    finally:
        processes.release_mutex(processes.INSTALL_MUTEX)
    assert api.load()["update"]["installing"] is False
    assert settings_app.update_view(updates.UpdateState(), FIXED)["installing"] is False


def test_the_page_takes_the_install_wait_from_python():
    page = (settings_app.WEB / "app.js").read_text(encoding="utf-8")
    assert "INSTALL_WAIT_MS" not in page and "install_wait_seconds" in page  # one number, which Python sends


def test_install_update_has_nothing_to_install_without_an_update_or_from_source(api, installed, monkeypatch):
    monkeypatch.setattr(updates, "install", lambda release: pytest.fail("installed"))
    assert api.install_update()["update"]["available"] is None
    save_update(latest=RELEASE)
    monkeypatch.setattr(updates, "enabled", lambda: False)
    assert api.install_update()["update"]["source"] is True


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


def test_a_day_or_more_ago_shows_the_date():
    assert settings_app.ago(FIXED - timedelta(days=2), FIXED) == "on Wed 28 Oct"


def test_test_says_why_an_address_did_not_work_without_repeating_it(api, monkeypatch):
    def refused(url, timeout=30):
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    def unreadable(*_args):
        raise ValueError("no DTSTART")

    monkeypatch.setattr(settings_app, "fetch_ics", refused)
    result = api.test_address(GOOD)
    assert result == {"ok": False, "error": "Couldn't read that address (404 Not Found). Check you copied all of it."}
    monkeypatch.setattr(settings_app, "fetch_ics", lambda url, timeout=30: ics("Work"))
    monkeypatch.setattr(settings_app, "summarize_feed", unreadable)
    assert api.test_address(GOOD) == {"ok": False, "error": "That address returned a calendar this app couldn't read."}


def test_a_calendar_added_twice_is_kept_once(api):
    api.add_calendar(GOOD)
    assert len(api.add_calendar(GOOD.replace("https://", "webcal://"))["settings"]["calendars"]) == 1


def test_an_idle_time_not_on_offer_is_ignored(api):
    assert api.set_idle(3)["settings"]["idle_minutes"] == Settings().idle_seconds // 60


def test_testing_a_calendar_while_the_config_is_broken_says_it_is_gone(api):
    config_path().write_text("{ broken", encoding="utf-8")
    assert api.test_calendar(feed_id(GOOD)) == {"ok": False, "error": "That calendar was removed."}


def test_links_open_in_the_browser_only_when_they_are_https(api, monkeypatch, spawned):
    opened = []
    monkeypatch.setattr(os, "startfile", opened.append)
    monkeypatch.setattr(processes, "open_log_folder", lambda: opened.append("logs"))
    api.open_link("https://github.com/cfoucher/granola-autostart")
    api.open_link("file:///C:/Windows/System32/calc.exe")
    api.open_logs()
    api.open_config()
    assert opened == ["https://github.com/cfoucher/granola-autostart", "logs"]
    assert spawned == [["notepad.exe", str(config_path())]]


def test_with_no_watcher_listening_pause_and_quit_do_not_wait(api, monkeypatch):
    monkeypatch.setattr(settings_app.commands, "send", lambda command: False)
    monkeypatch.setattr(settings_app, "_wait_for", lambda *_args, **_kwargs: pytest.fail("waited for a watcher that isn't there"))
    api.pause("hour")
    api.resume()
    assert api.quit_app()["status"]["running"]


def test_waiting_ends_once_done_or_when_the_time_is_up(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(settings_app, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=lambda seconds: setattr(clock, "now", clock.now + seconds)))
    answers = iter([False, False, True])
    assert settings_app._wait_for(lambda: next(answers))
    assert clock.now == pytest.approx(0.2)
    assert not settings_app._wait_for(lambda: False, seconds=1)
    assert 1.1 < clock.now < 1.4


class Window:
    def __init__(self):
        self.calls = []

    def set_title(self, title):
        self.calls.append(("title", title))

    def resize(self, width, height):
        self.calls.append(("size", width, height))

    def destroy(self):
        self.calls.append("destroyed")


def test_setup_s_done_retitles_resizes_and_recentres_its_window(api, monkeypatch):
    window = Window()
    api._window = window
    api.page = "setup"
    monkeypatch.setattr(settings_app, "_center_on_primary", lambda window: window.calls.append("centred"))
    api.open_settings()
    assert window.calls == [("title", "Granola Autostart settings"), ("size", 796, 660), "centred"]


def test_run_setup_again_hands_over_to_a_setup_window(api, monkeypatch, spawned):
    released = []
    monkeypatch.setattr(processes, "release_mutex", released.append)
    window = Window()
    api._window = window
    api.run_setup()
    assert released == [processes.SETTINGS_MUTEX] and spawned[-1][-1] == "--setup" and window.calls == ["destroyed"]
    api._window = None
    api.close()  # before the window exists: nothing to close


def fake_user32(monkeypatch, **calls) -> None:
    monkeypatch.setattr(settings_app, "ctypes", SimpleNamespace(windll=SimpleNamespace(user32=SimpleNamespace(**calls))))


def test_a_second_window_brings_the_open_settings_or_setup_forward(monkeypatch):
    calls = []
    windows = {"Set up Granola Autostart": 0x77}
    fake_user32(
        monkeypatch,
        FindWindowW=lambda _class, title: windows.get(title),
        ShowWindow=lambda hwnd, command: calls.append(("restore", hwnd, command)),
        SetForegroundWindow=lambda hwnd: calls.append(("front", hwnd)),
    )
    settings_app._bring_forward()
    assert calls == [("restore", 0x77, 9), ("front", 0x77)]
    windows.clear()
    calls.clear()
    settings_app._bring_forward()  # it closed in the meantime
    assert calls == []


def native_window(handle: int = 0x42, **form) -> SimpleNamespace:
    return SimpleNamespace(native=SimpleNamespace(Handle=SimpleNamespace(ToInt64=lambda: handle), **form))


def test_the_title_bar_follows_the_theme_and_shows_the_app_icon_from_source(monkeypatch):
    attributes, messages = [], []
    monkeypatch.setattr(settings_app, "set_window_attribute", lambda hwnd, attribute, value: attributes.append((hwnd, attribute, value)))
    fake_user32(
        monkeypatch,
        LoadImageW=lambda *_args: 0x55,
        SendMessageW=lambda hwnd, message, which, icon: messages.append((hwnd.value, message, which, icon)),
    )
    monkeypatch.setattr(processes, "FROZEN", False)
    (settings_app.app_dir() / "app.ico").write_bytes(b"ico")
    settings_app._style_title_bar(native_window(), dark=True)
    assert messages == [(0x42, 0x80, 0, 0x55), (0x42, 0x80, 1, 0x55)]  # WM_SETICON, small and big
    assert attributes == [(0x42, 20, 1), (0x42, 35, "#1f1d1a"), (0x42, 36, "#f1ece1"), (0x42, 34, "#3d3a33")]
    monkeypatch.setattr(processes, "FROZEN", True)  # the packaged exe carries its own icon
    messages.clear()
    attributes.clear()
    settings_app._style_title_bar(native_window(), dark=False)
    assert messages == [] and attributes[0] == (0x42, 20, 0)


def test_a_title_bar_windows_will_not_colour_is_left_as_it_is():
    settings_app._style_title_bar(SimpleNamespace(native=None), dark=True)


@pytest.fixture
def dotnet(monkeypatch) -> SimpleNamespace:
    """pythonnet's System namespaces, faked: Invoke runs the call at once, as the window's UI thread would."""
    screen = SimpleNamespace(X=0, Y=40, Width=1920, Height=1040)  # the primary monitor's work area, below a top taskbar

    class Func:
        def __class_getitem__(cls, _types):
            return lambda call: call

    system, windows, forms, drawing = (ModuleType(name) for name in ("System", "System.Windows", "System.Windows.Forms", "System.Drawing"))
    system.Func, system.Type, system.Windows, system.Drawing = Func, object, windows, drawing
    windows.Forms = forms
    forms.Screen = SimpleNamespace(PrimaryScreen=SimpleNamespace(WorkingArea=screen))
    forms.FormStartPosition = SimpleNamespace(Manual="manual")
    drawing.Point = lambda x, y: (x, y)
    for module in (system, windows, forms, drawing):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    return screen


def form(width: int, height: int) -> SimpleNamespace:
    settings = SimpleNamespace(AreDefaultContextMenusEnabled=False)
    return SimpleNamespace(
        Width=width,
        Height=height,
        StartPosition="centre on the pointer's screen",
        Location=None,
        Invoke=lambda call: call(),
        webview=SimpleNamespace(CoreWebView2=SimpleNamespace(Settings=settings)),
    )


def test_a_new_window_opens_in_the_middle_of_the_primary_monitor(dotnet):
    window = SimpleNamespace(native=form(796, 660))
    settings_app._center_on_primary(window)
    assert (window.native.StartPosition, window.native.Location) == ("manual", (562, 230))
    tall = SimpleNamespace(native=form(796, 1200))
    settings_app._center_on_primary(tall)
    assert tall.native.Location == (562, 40)  # the title bar stays on screen


def test_text_fields_get_their_right_click_menu_back(dotnet):
    window = SimpleNamespace(native=form(796, 660))
    settings_app._enable_text_field_menus(window)
    assert window.native.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled is True


def test_without_pythonnet_the_window_still_opens_uncentred_and_without_menus(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "System", None)
    window = SimpleNamespace(native=form(796, 660))
    settings_app._center_on_primary(window)
    settings_app._enable_text_field_menus(window)
    assert window.native.Location is None and not window.native.webview.CoreWebView2.Settings.AreDefaultContextMenusEnabled
    assert "window not centered" in caplog.text and "right-click menus stay off" in caplog.text


class Event:
    """pywebview's window events: handlers are added with +=."""

    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


@pytest.fixture
def webview(monkeypatch, spawned) -> SimpleNamespace:
    """pywebview, faked: the windows it would have opened, and what the window's events did."""
    fake = SimpleNamespace(windows=[], started=[], broken=False, styled=[], spawned=spawned, on_start=lambda: None)

    def create_window(title, **options):
        events = SimpleNamespace(before_show=Event(), shown=Event(), loaded=Event())
        fake.windows.append(SimpleNamespace(title=title, options=options, events=events))
        return fake.windows[-1]

    def start(**options):
        fake.started.append(options)
        fake.on_start()
        if fake.broken:
            raise RuntimeError("the WebView2 runtime is missing")

    module = ModuleType("webview")
    module.create_window, module.start = create_window, start
    monkeypatch.setitem(sys.modules, "webview", module)
    monkeypatch.setattr(settings_app, "setup_logging", lambda name: logging.getLogger("granola_autostart"))
    monkeypatch.setattr(processes, "hold_mutex", lambda name: True)
    monkeypatch.setattr(settings_app, "app_theme", lambda: "dark")
    for name in ("_center_on_primary", "_enable_text_field_menus"):
        monkeypatch.setattr(settings_app, name, lambda window, name=name: fake.styled.append(name))
    monkeypatch.setattr(settings_app, "_style_title_bar", lambda window, dark: fake.styled.append(("_style_title_bar", dark)))
    return fake


def test_settings_opens_its_page_in_a_dark_window_and_starts_a_quit_app(webview):
    assert settings_app.main(["--section=general"]) == 0
    [window] = webview.windows
    assert window.title == "Granola Autostart settings"
    assert (window.options["width"], window.options["height"], window.options["background_color"]) == (796, 660, "#25231f")
    assert window.options["url"].endswith("/web/index.html") and window.options["js_api"].section == "general"
    assert window.options["js_api"]._theme == "dark"  # what the title bar is styled for until the theme changes
    assert window.options["js_api"]._window is window
    assert webview.started == [{"gui": "edgechromium", "private_mode": True}]
    assert webview.spawned[-1][-1] == "--supervise"
    for event in ("before_show", "shown", "loaded"):
        getattr(window.events, event).fire()
    assert webview.styled == ["_center_on_primary", ("_style_title_bar", True), "_enable_text_field_menus"]


def test_settings_lets_go_of_its_mutex_once_its_window_has_closed(webview, monkeypatch):
    released = []
    monkeypatch.setattr(processes, "release_mutex", released.append)
    webview.on_start = lambda: released.append("window open")
    assert settings_app.main([]) == 0
    assert released == ["window open", processes.SETTINGS_MUTEX]  # not before the window is done with


def test_settings_lets_go_of_its_mutex_when_the_window_could_not_open_too(webview, monkeypatch):
    released = []
    monkeypatch.setattr(processes, "release_mutex", released.append)
    monkeypatch.setattr(processes, "watcher_running", lambda: True)
    webview.broken = True
    assert settings_app.main([]) == 1
    assert released == [processes.SETTINGS_MUTEX]


def test_a_second_settings_window_does_not_let_go_of_the_first_one_s_mutex(webview, monkeypatch):
    released = []
    monkeypatch.setattr(processes, "release_mutex", released.append)
    monkeypatch.setattr(processes, "hold_mutex", lambda name: False)
    monkeypatch.setattr(settings_app, "_bring_forward", lambda: None)
    assert settings_app.main([]) == 0
    assert released == []


def test_the_calls_section_opens_the_calls_page(webview):
    assert settings_app.main(["--section=calls"]) == 0
    assert webview.windows[0].options["js_api"].section == "calls"


def test_the_old_section_name_about_still_opens_general(webview):
    assert settings_app.main(["--section=about"]) == 0
    assert webview.windows[0].options["js_api"].section == "general"


def test_the_title_bar_is_styled_for_the_theme_the_page_has_by_the_time_the_window_shows(webview):
    settings_app.main([])
    [window] = webview.windows
    window.options["js_api"]._theme = "light"  # the theme changed after the window was created
    window.events.shown.fire()
    assert webview.styled == [("_style_title_bar", False)]


def test_setup_opens_its_own_page_without_starting_the_watcher(webview):
    assert settings_app.main(["--setup"]) == 0
    [window] = webview.windows
    assert (window.title, window.options["width"], window.options["height"]) == ("Set up Granola Autostart", 656, 680)
    assert window.options["js_api"].page == "setup" and webview.spawned == []


def test_a_second_settings_window_brings_the_first_forward_instead(webview, monkeypatch):
    forward = []
    monkeypatch.setattr(processes, "hold_mutex", lambda name: False)
    monkeypatch.setattr(settings_app, "_bring_forward", lambda: forward.append(True))
    assert settings_app.main([]) == 0
    assert forward == [True] and webview.windows == []


def test_without_webview2_settings_opens_config_json_instead(webview, monkeypatch):
    monkeypatch.setattr(processes, "watcher_running", lambda: True)
    webview.broken = True
    assert settings_app.main([]) == 1
    assert webview.spawned == [["notepad.exe", str(config_path())]]
