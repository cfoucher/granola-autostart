import json
import logging
import os
import sys
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from types import ModuleType, SimpleNamespace

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
    fake = SimpleNamespace(windows=[], started=[], broken=False, styled=[], spawned=spawned)

    def create_window(title, **options):
        events = SimpleNamespace(before_show=Event(), shown=Event(), loaded=Event())
        fake.windows.append(SimpleNamespace(title=title, options=options, events=events))
        return fake.windows[-1]

    def start(**options):
        fake.started.append(options)
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
    assert settings_app.main(["--section=about"]) == 0
    [window] = webview.windows
    assert window.title == "Granola Autostart settings"
    assert (window.options["width"], window.options["height"], window.options["background_color"]) == (796, 660, "#25231f")
    assert window.options["url"].endswith("/web/index.html") and window.options["js_api"].section == "about"
    assert window.options["js_api"]._window is window
    assert webview.started == [{"gui": "edgechromium", "private_mode": True}]
    assert webview.spawned[-1][-1] == "--supervise"
    for event in ("before_show", "shown", "loaded"):
        getattr(window.events, event).fire()
    assert webview.styled == ["_center_on_primary", ("_style_title_bar", True), "_enable_text_field_menus"]


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
