import http.client
import logging
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from granola_autostart import calendar_cache, net
from granola_autostart.calendar_feed import merge_events
from granola_autostart.settings import Calendar, Settings, status_path
from granola_autostart.status import FeedStatus, Status, feed_id, find_problems, write_status

NOW = datetime(2026, 10, 28, 16, 0, tzinfo=timezone.utc)
MAIN = "https://calendar.google.com/main.ics"
WATCH = "https://calendar.google.com/watch.ics"
HOME = "https://calendar.google.com/home.ics"


def watching(*urls: str, **options) -> Settings:
    """Settings with these calendars, all on and automatic."""
    return Settings(calendars=tuple(Calendar(url) for url in urls), **options)

MAIN_ICS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:conrad@example.com
BEGIN:VEVENT
UID:sync@google.com
SUMMARY:Weekly sync
DTSTART:20261028T170000Z
DTEND:20261028T173000Z
ATTENDEE;PARTSTAT=ACCEPTED:mailto:conrad@example.com
END:VEVENT
END:VCALENDAR
"""

WATCH_ICS = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-CALNAME:Granola watch
BEGIN:VEVENT
UID:crit@google.com
SUMMARY:Design crit
DTSTART:20261028T180000Z
DTEND:20261028T183000Z
ATTENDEE;PARTSTAT=DECLINED:mailto:conrad@example.com
END:VEVENT
END:VCALENDAR
"""


HOME_ICS = WATCH_ICS.replace("Granola watch", "home@example.com").replace("crit@google.com", "chat@google.com")

# The same meeting, as each calendar lists it.
SHARED_ON_MAIN = MAIN_ICS.replace("UID:sync@google.com", "UID:shared@google.com")
SHARED_ON_WATCH = WATCH_ICS.replace("UID:crit@google.com", "UID:shared@google.com").replace("20261028T180000Z", "20261028T170000Z")


class Served(dict):
    """The feeds the world serves, by address (None for one that is down), and the addresses downloaded so far."""

    def __init__(self, feeds):
        super().__init__(feeds)
        self.fetched = []


@pytest.fixture
def feeds(monkeypatch):
    served = Served({MAIN: MAIN_ICS, WATCH: WATCH_ICS, HOME: HOME_ICS})

    def fetch(url):
        served.fetched.append(url)
        if served.get(url) is None:
            raise OSError("feed down")
        return served[url]

    monkeypatch.setattr(calendar_cache, "fetch_ics", fetch)
    return served


def titles(loaded):
    """What the watcher would keep: the calendars' meetings merged, each calendar having left out what it declined."""
    return sorted(event.summary for event in merge_events(*loaded.events.values()))


def test_a_secondary_calendar_skips_declines_by_the_main_calendars_address(feeds):
    owners = {}
    loaded = calendar_cache.load_feeds(NOW, watching(MAIN, WATCH), owners)
    assert loaded.failures == []
    assert titles(loaded) == ["Weekly sync"]
    assert owners == {MAIN: "conrad@example.com", WATCH: None}
    assert loaded.names == {MAIN: "conrad@example.com", WATCH: "Granola watch"}
    assert loaded.events[WATCH] == []  # its one meeting was declined, so it is left out
    assert [event.summary for event in loaded.events[MAIN]] == ["Weekly sync"]


def test_a_failed_main_calendar_still_lends_its_last_known_address(feeds):
    owners = {MAIN: "conrad@example.com"}
    feeds[MAIN] = None
    loaded = calendar_cache.load_feeds(NOW, watching(MAIN, WATCH), owners)
    assert [(index, url) for index, url, _error in loaded.failures] == [(1, MAIN)]
    assert titles(loaded) == []


def test_without_any_address_a_decline_cannot_be_recognised(feeds):
    feeds[MAIN] = None
    assert titles(calendar_cache.load_feeds(NOW, watching(MAIN, WATCH), {})) == ["Design crit"]
    settings = watching(MAIN, WATCH, user_email="conrad@example.com")
    assert titles(calendar_cache.load_feeds(NOW, settings, {})) == []


def test_a_calendar_that_names_its_own_address_checks_declines_against_that_one_only(feeds):
    # HOME is named after home@example.com, and its meeting was declined by conrad@example.com: not by HOME's owner.
    assert titles(calendar_cache.load_feeds(NOW, watching(MAIN, HOME), {})) == ["Design crit", "Weekly sync"]
    owner_declined = HOME_ICS.replace("mailto:conrad@example.com", "mailto:home@example.com")
    feeds[HOME] = owner_declined
    assert titles(calendar_cache.load_feeds(NOW, watching(MAIN, HOME), {})) == ["Weekly sync"]


def test_the_old_typed_address_counts_for_an_automatic_calendar_with_an_address_of_its_own(feeds):
    feeds[HOME] = HOME_ICS.replace("mailto:conrad@example.com", "mailto:alias@example.ca")
    assert titles(calendar_cache.load_feeds(NOW, watching(HOME), {})) == ["Design crit"]
    assert titles(calendar_cache.load_feeds(NOW, watching(HOME, user_email="alias@example.ca"), {})) == []


def test_a_list_of_addresses_is_exactly_those_aliases_included(feeds):
    feeds[WATCH] = WATCH_ICS.replace("mailto:conrad@example.com", "mailto:Alias@Example.ca")
    only_main = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=("conrad@example.com",))))
    assert titles(calendar_cache.load_feeds(NOW, only_main, {})) == ["Design crit", "Weekly sync"]
    with_alias = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=("conrad@example.com", " ALIAS@example.ca"))))
    assert titles(calendar_cache.load_feeds(NOW, with_alias, {})) == ["Weekly sync"]
    # A list ignores the calendar's own address and the old typed one.
    own_ignored = Settings(calendars=(Calendar(MAIN, declined_by=("alias@example.ca",)), Calendar(WATCH)), user_email="alias@example.ca")
    feeds[MAIN] = MAIN_ICS.replace("PARTSTAT=ACCEPTED", "PARTSTAT=DECLINED")
    assert titles(calendar_cache.load_feeds(NOW, own_ignored, {})) == ["Weekly sync"]


def test_a_calendar_that_is_off_is_not_downloaded_but_still_lends_its_last_known_address(feeds):
    settings = Settings(calendars=(Calendar(MAIN, enabled=False), Calendar(WATCH)))
    owners = {MAIN: "conrad@example.com"}
    loaded = calendar_cache.load_feeds(NOW, settings, owners)
    assert feeds.fetched == [WATCH] and list(loaded.events) == [WATCH] and loaded.failures == []
    assert titles(loaded) == []  # Design crit was declined by the main calendar's address
    feeds.fetched.clear()
    assert titles(calendar_cache.load_feeds(NOW, settings, {})) == ["Design crit"]  # nobody to lend it
    assert owners == {MAIN: "conrad@example.com", WATCH: None}


def test_a_failure_is_numbered_by_its_place_in_the_config_even_when_a_calendar_before_it_is_off(feeds):
    feeds[WATCH] = None
    settings = Settings(calendars=(Calendar(MAIN, enabled=False), Calendar(WATCH)))
    assert [(index, url) for index, url, _error in calendar_cache.load_feeds(NOW, settings, {}).failures] == [(2, WATCH)]


def test_a_meeting_on_two_calendars_starts_notes_unless_every_copy_is_declined_on_its_own_calendar(feeds):
    accepted_on_watch = SHARED_ON_WATCH.replace("PARTSTAT=DECLINED", "PARTSTAT=ACCEPTED")
    declined_on_main = SHARED_ON_MAIN.replace("PARTSTAT=ACCEPTED", "PARTSTAT=DECLINED")
    both = Settings(calendars=(Calendar(MAIN), Calendar(WATCH)))

    def merged(main, watch, settings=both):
        feeds[MAIN], feeds[WATCH] = main, watch
        return [event.summary for event in merge_events(*calendar_cache.load_feeds(NOW, settings, {}).events.values())]

    assert merged(SHARED_ON_MAIN, accepted_on_watch) == ["Weekly sync"]  # one copy, not two
    assert merged(declined_on_main, SHARED_ON_WATCH) == []  # declined on both: the watch calendar copies the main address
    # Declined on one calendar only, the other would start notes, and its copy is the one kept.
    assert merged(SHARED_ON_MAIN, SHARED_ON_WATCH) == ["Weekly sync"]
    assert merged(declined_on_main, accepted_on_watch) == ["Design crit"]
    # Each calendar checks its own addresses: the copy on WATCH isn't a decline when WATCH's list leaves conrad out,
    # so the meeting declined on the main calendar still starts notes. With both copies declined it doesn't.
    elsewhere = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=("other@example.com",))))
    assert merged(SHARED_ON_MAIN, SHARED_ON_WATCH, elsewhere) == ["Weekly sync"]
    assert merged(declined_on_main, SHARED_ON_WATCH, elsewhere) == ["Design crit"]
    declined_on_both = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=("conrad@example.com", "other@example.com"))))
    assert merged(declined_on_main, SHARED_ON_WATCH, declined_on_both) == []


def test_a_meeting_declined_on_the_main_calendar_starts_notes_when_the_watch_calendar_would_start_it(feeds):
    feeds[MAIN] = SHARED_ON_MAIN.replace("PARTSTAT=ACCEPTED", "PARTSTAT=DECLINED")
    feeds[WATCH] = SHARED_ON_WATCH  # its invite names conrad@example.com, who declined it

    def merged(watch_list):
        settings = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=watch_list)))
        return [event.summary for event in merge_events(*calendar_cache.load_feeds(NOW, settings, {}).events.values())]

    assert merged(None) == []  # the watch calendar names no address, so it copies the main one: declined on both
    assert merged(("conrad@example.com",)) == []
    assert merged(("alias@example.ca",)) == ["Design crit"]  # an alias that isn't on the invite: not declined on this calendar
    assert merged(()) == ["Design crit"]  # no addresses: nothing is declined on this calendar


def test_a_calendar_with_no_addresses_checks_no_declines(feeds):
    none_checked = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=())))
    loaded = calendar_cache.load_feeds(NOW, none_checked, {})
    assert loaded.failures == []
    # Design crit was declined by the main calendar's address, which this calendar doesn't look for.
    assert [event.summary for event in loaded.events[WATCH]] == ["Design crit"]
    assert titles(loaded) == ["Design crit", "Weekly sync"]
    # Automatic, or with a list that has the address, the decline stands.
    for settings in (watching(MAIN, WATCH), Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=("conrad@example.com",))))):
        loaded = calendar_cache.load_feeds(NOW, settings, {})
        assert loaded.events[WATCH] == []
        assert titles(loaded) == ["Weekly sync"]


@pytest.fixture
def cache(monkeypatch):
    monkeypatch.setattr(calendar_cache, "lower_thread_priority", lambda: None)
    return calendar_cache.CalendarCache()


def test_the_cache_reports_each_calendars_health(feeds, cache):
    feeds[WATCH] = None
    settings = watching(MAIN, WATCH)
    for expected_failures in (1, 2, 3):
        cache._refresh(settings, cache._generation, NOW)
        main, watch = cache.feed_statuses(settings.distinct_calendars())
        assert (watch.failures, watch.last_error) == (expected_failures, "OSError")
    assert (main.name, main.owner, main.failures, main.meetings_next_day) == ("conrad@example.com", "conrad@example.com", 0, 1)
    assert main.enabled and watch.enabled
    feeds[WATCH] = WATCH_ICS
    cache._refresh(settings, cache._generation, NOW)
    assert cache.feed_statuses([Calendar(WATCH)])[0].failures == 0


SECRET = "https://calendar.google.com/calendar/ical/me%40example.com/private-0123456789abcdef/basic.ics"


def _redirect_to_http():
    handler = net.HttpsOnlyRedirects()
    request = urllib.request.Request(SECRET)
    try:
        handler.redirect_request(request, None, 302, "Found", {}, "http://calendar.google.com/elsewhere")
    except urllib.error.HTTPError as error:
        return error
    raise AssertionError("the redirect was allowed")


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError(SECRET, 403, "Forbidden", {}, None),
        urllib.error.URLError(f"unknown url type: {SECRET}"),
        http.client.InvalidURL(f"URL can't contain control characters. {SECRET!r} (found at least ' ')"),
        ValueError(f"bad calendar {SECRET}"),
        OSError(f"cannot reach {SECRET}"),
        _redirect_to_http(),
    ],
    ids=["http-error", "url-error", "invalid-url", "value-error", "os-error", "redirect-refused"],
)
def test_a_failed_download_never_puts_the_address_in_the_log_or_status(error, cache, monkeypatch, caplog):
    def fetch(url):
        raise error

    monkeypatch.setattr(calendar_cache, "fetch_ics", fetch)
    settings = watching(SECRET)
    with caplog.at_level(logging.DEBUG, logger="granola_autostart"):
        cache._refresh(settings, cache._generation, NOW)
    [feed] = cache.feed_statuses(settings.distinct_calendars())
    assert feed.failures == 1
    assert "calendar feed 1 failed" in caplog.text
    for text in (caplog.text, feed.last_error, repr(feed)):
        assert SECRET not in text
        assert "private-0123456789abcdef" not in text
        assert "example.com" not in text


def test_the_cache_keeps_a_meeting_declined_on_the_main_calendar_when_the_watch_calendar_would_start_it(feeds, cache):
    feeds[MAIN] = SHARED_ON_MAIN.replace("PARTSTAT=ACCEPTED", "PARTSTAT=DECLINED")
    feeds[WATCH] = SHARED_ON_WATCH
    automatic = watching(MAIN, WATCH)
    cache._refresh(automatic, cache._generation, NOW)
    assert cache.current(NOW, automatic, refresh=False) == []  # declined on both
    for watch_list in (("alias@example.ca",), ()):
        settings = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, declined_by=watch_list)))
        cache.invalidate(settings.enabled_urls())
        cache._refresh(settings, cache._generation, NOW)
        assert [event.summary for event in cache.current(NOW, settings, refresh=False)] == ["Design crit"]
    # Switching the watch calendar off takes its copy away at once, so the one declined on the main calendar is all there is.
    watch_off = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, enabled=False, declined_by=())))
    cache.invalidate(watch_off.enabled_urls())
    assert cache.current(NOW, watch_off, refresh=False) == []


def test_a_calendar_that_fails_keeps_its_last_meetings(feeds, cache):
    settings = watching(MAIN, WATCH)
    cache._refresh(settings, cache._generation, NOW)
    feeds[MAIN] = None
    cache._refresh(settings, cache._generation, NOW)
    assert [event.summary for event in cache.current(NOW, settings, refresh=False)] == ["Weekly sync"]


def test_a_download_started_before_a_settings_change_is_dropped(feeds, cache):
    settings = watching(MAIN)
    started = cache._generation
    cache.invalidate([MAIN])
    cache._refresh(settings, started, NOW)
    assert cache.current(NOW, settings, refresh=False) == []
    assert cache.feed_statuses(settings.distinct_calendars())[0].last_ok is None


def test_a_removed_calendar_stops_counting_before_the_next_download(feeds, cache):
    both = watching(MAIN, WATCH, ignore_title_substrings=())
    feeds[WATCH] = WATCH_ICS.replace("PARTSTAT=DECLINED", "PARTSTAT=ACCEPTED")
    cache._refresh(both, cache._generation, NOW)
    assert len(cache.current(NOW, both, refresh=False)) == 2
    main_only = watching(MAIN)
    cache.invalidate(main_only.calendar_urls())
    assert [event.summary for event in cache.current(NOW, main_only, refresh=False)] == ["Weekly sync"]


def test_a_calendar_that_is_off_is_never_downloaded_and_never_alerts(feeds, cache):
    settings = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, enabled=False)))
    feeds[WATCH] = None  # it would fail, if it were downloaded
    for _download in range(4):
        cache._refresh(settings, cache._generation, NOW)
    assert set(feeds.fetched) == {MAIN}
    main, watch = cache.feed_statuses(settings.distinct_calendars())
    assert (main.enabled, watch.enabled, watch.failures) == (True, False, 0)
    assert find_problems(None, [main, watch], None) == []


def test_switching_a_calendar_off_drops_its_meetings_at_once_and_keeps_its_name_and_owner(feeds, cache):
    both = watching(MAIN, WATCH, ignore_title_substrings=())
    feeds[WATCH] = WATCH_ICS.replace("PARTSTAT=DECLINED", "PARTSTAT=ACCEPTED")
    cache._refresh(both, cache._generation, NOW)
    assert len(cache.current(NOW, both, refresh=False)) == 2
    watch_off = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, enabled=False)))
    cache.invalidate(watch_off.enabled_urls())
    assert [event.summary for event in cache.current(NOW, watch_off, refresh=False)] == ["Weekly sync"]
    main, watch = cache.feed_statuses(watch_off.distinct_calendars())
    assert (watch.enabled, watch.name) == (False, "Granola watch")
    assert main.enabled and main.name == "conrad@example.com"


def test_a_calendar_that_is_off_goes_on_lending_its_address_to_the_others_declines(feeds, cache):
    cache._refresh(watching(MAIN, WATCH), cache._generation, NOW)  # learns the main calendar's address
    main_off = Settings(calendars=(Calendar(MAIN, enabled=False), Calendar(WATCH)))
    cache.invalidate(main_off.enabled_urls())
    feeds.fetched.clear()
    cache._refresh(main_off, cache._generation, NOW)
    assert feeds.fetched == [WATCH]
    assert cache.current(NOW, main_off, refresh=False) == []  # Design crit was declined by the address of the calendar that's off
    assert cache.feed_statuses(main_off.distinct_calendars())[0].owner == "conrad@example.com"


def test_a_calendar_that_was_failing_starts_clean_when_it_is_switched_back_on(feeds, cache):
    feeds[WATCH] = None
    both = watching(MAIN, WATCH)
    for _download in range(3):
        cache._refresh(both, cache._generation, NOW)
    assert [problem.key for problem in find_problems(None, cache.feed_statuses(both.distinct_calendars()), None)] == [f"feed:{feed_id(WATCH)}"]
    watch_off = Settings(calendars=(Calendar(MAIN), Calendar(WATCH, enabled=False)))
    cache.invalidate(watch_off.enabled_urls())
    assert find_problems(None, cache.feed_statuses(watch_off.distinct_calendars()), None) == []
    cache.invalidate(both.enabled_urls())
    cache._refresh(both, cache._generation, NOW)
    watch = cache.feed_statuses(both.distinct_calendars())[1]
    assert (watch.failures, watch.alerting) == (1, False)  # the first failure since, not the fourth


def test_names_and_owners_come_back_from_status_json_after_a_restart(feeds, monkeypatch):
    monkeypatch.setattr(calendar_cache, "lower_thread_priority", lambda: None)
    saved = FeedStatus(feed_id(MAIN))
    saved.succeeded(NOW, name="conrad@example.com", owner="conrad@example.com", meetings_next_day=4)
    removed = FeedStatus(feed_id("https://calendar.google.com/removed.ics"), name="Removed", owner="removed@example.com")
    write_status(status_path(), Status("test", feeds=[saved, removed]))
    cache = calendar_cache.CalendarCache()
    settings = Settings(calendars=(Calendar(MAIN, enabled=False), Calendar(WATCH)))
    assert cache.known_owners(settings.distinct_calendars()) == {MAIN: "conrad@example.com", WATCH: None}  # not the removed one's
    main, watch = cache.feed_statuses(settings.distinct_calendars())
    assert (main.name, main.owner, main.enabled, main.last_ok, main.meetings_next_day) == ("conrad@example.com", "conrad@example.com", False, None, None)
    assert (watch.name, watch.owner, watch.enabled) == (None, None, True)
    cache._refresh(settings, cache._generation, NOW)
    assert feeds.fetched == [WATCH]
    assert cache.current(NOW, settings, refresh=False) == []  # declined by the address the off calendar had last time
    forgotten = calendar_cache.CalendarCache()
    forgotten._refresh(watching(WATCH), forgotten._generation, NOW)
    assert [event.summary for event in forgotten.current(NOW, watching(WATCH), refresh=False)] == ["Design crit"]


@pytest.fixture
def downloads(monkeypatch):
    """The downloads current() starts, recorded instead of run, on a monotonic clock the test moves."""
    record = SimpleNamespace(started=[], clock=1000.0)

    class Thread:
        def __init__(self, target, args, name, daemon):
            self.target, self.args = target, args

        def start(self):
            record.started.append(self)

        def run(self):
            self.target(*self.args)

    monkeypatch.setattr(calendar_cache, "threading", SimpleNamespace(Thread=Thread, Lock=threading.Lock))
    monkeypatch.setattr(calendar_cache, "time", SimpleNamespace(monotonic=lambda: record.clock))
    return record


def test_one_download_at_a_time_and_only_when_someone_is_at_the_pc(feeds, cache, downloads):
    settings = watching(MAIN)
    cache.current(NOW, settings, refresh=False)
    assert downloads.started == []
    cache.current(NOW, settings)
    cache.current(NOW, settings)
    assert len(downloads.started) == 1


def test_with_every_calendar_off_there_is_nothing_to_download_or_count(feeds, cache, downloads):
    settings = Settings(calendars=(Calendar(MAIN, enabled=False), Calendar(WATCH, enabled=False)))
    assert cache.current(NOW, settings) == []
    assert downloads.started == []


def test_coming_back_to_the_pc_refreshes_sooner(feeds, cache, downloads):
    settings = watching(MAIN, calendar_refresh_seconds=600)
    cache.current(NOW, settings)
    downloads.started[0].run()
    back = NOW + timedelta(seconds=calendar_cache.RETURN_REFRESH_SECONDS)
    cache.current(back, settings)
    assert len(downloads.started) == 1
    cache.current(back, settings, eager=True)
    assert len(downloads.started) == 2


def test_a_download_stuck_too_long_is_abandoned_for_a_new_one(feeds, cache, downloads):
    settings = watching(MAIN)
    cache.current(NOW, settings)
    downloads.clock += calendar_cache.STUCK_REFRESH_SECONDS
    cache.current(NOW, settings)
    assert len(downloads.started) == 1
    downloads.clock += 1
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
    downloads.started[0].run()
    assert cache.current(NOW, settings, refresh=False) == []
    downloads.started[1].run()
    assert [event.summary for event in cache.current(NOW, settings, refresh=False)] == ["Weekly sync"]


def test_a_failed_download_is_retried_a_minute_later_not_a_full_interval(feeds, cache, downloads):
    feeds[MAIN] = None
    settings = watching(MAIN, calendar_refresh_seconds=600)
    cache.current(NOW, settings)
    downloads.started[0].run()
    retry = NOW + timedelta(seconds=calendar_cache.FAILED_FEED_RETRY_SECONDS)
    cache.current(retry - timedelta(seconds=1), settings)
    assert len(downloads.started) == 1
    cache.current(retry, settings)
    assert len(downloads.started) == 2


def test_changing_calendars_starts_a_new_download_while_the_old_one_runs(feeds, cache, downloads):
    settings = watching(MAIN)
    cache.current(NOW, settings)
    cache.invalidate(settings.calendar_urls())
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
    downloads.started[0].run()
    cache.current(NOW, settings)
    assert len(downloads.started) == 2


def test_changing_calendars_downloads_again_at_once_even_after_a_fresh_download(feeds, cache, downloads):
    settings = watching(MAIN)
    cache.current(NOW, settings)
    downloads.started[0].run()
    cache.current(NOW, settings)
    assert len(downloads.started) == 1  # just downloaded, so not stale
    cache.invalidate(settings.calendar_urls())
    cache.current(NOW, settings)
    assert len(downloads.started) == 2
