import ctypes
import hashlib
import http.client
import io
import json
import logging
import threading
import urllib.error
import urllib.request
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from granola_autostart import processes, updates
from granola_autostart.settings import update_path
from granola_autostart.status import describe_error

NOW = datetime(2026, 11, 1, 9, 12, tzinfo=timezone.utc)
INSTALLER = b"MZ pretend installer bytes"
SHA256 = hashlib.sha256(INSTALLER).hexdigest()
ETAG = 'W/"4f2a"'
DOWNLOAD = "https://github.com/cfoucher/granola-autostart/releases/download/v1.2.0/GranolaAutostart-1.2.0-setup.exe"


@pytest.fixture(autouse=True)
def running(monkeypatch):
    monkeypatch.setattr(updates, "__version__", "1.1.0")


class Response(io.BytesIO):
    """What urllib's opener returns: a body to read, and its headers."""

    def __init__(self, body: bytes = b"", **headers: str) -> None:
        super().__init__(body)
        self.headers = headers


class Counted(Response):
    def __init__(self, body: bytes) -> None:
        super().__init__(body)
        self.consumed = 0

    def read1(self, size=-1):
        chunk = super().read1(size)
        self.consumed += len(chunk)
        return chunk


class Dropping(Response):
    """A connection that goes away after the first read."""

    def read1(self, size=-1):
        if self.tell():
            raise ConnectionResetError("connection dropped")
        return super().read1(size)


class Trickling(Response):
    """A server that sends a byte at a time, each taking ``seconds`` on ``clock``. A read that waits for a whole chunk
    of it, rather than for what has arrived, would never come back."""

    def __init__(self, body: bytes, clock: list[float], seconds: float) -> None:
        super().__init__(body)
        self.clock, self.seconds = clock, seconds

    def read(self, size=-1):
        pytest.fail("read waits for a whole chunk, which a trickle never fills")

    def read1(self, size=-1):
        self.clock[0] += self.seconds
        return super().read1(1)


def serving(monkeypatch, *answers) -> SimpleNamespace:
    """Replaces the opener: each request gets the next answer, a Response or an exception to raise."""
    pending = list(answers)
    served = SimpleNamespace(requests=[])

    def open_(request, timeout):
        served.requests.append((request, timeout))
        answer = pending.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr(updates, "_opener", SimpleNamespace(open=open_))
    return served


def release_json(version: str = "1.2.0", *, assets=None, digest=f"sha256:{SHA256}", size=len(INSTALLER), **extra) -> dict:
    name = f"GranolaAutostart-{version}-setup.exe"
    asset = {
        "name": name,
        "browser_download_url": f"https://github.com/cfoucher/granola-autostart/releases/download/v{version}/{name}",
        "size": size,
        "digest": digest,
    }
    return {
        "tag_name": f"v{version}",
        "html_url": f"https://github.com/cfoucher/granola-autostart/releases/tag/v{version}",
        "assets": [asset] if assets is None else assets,
        **extra,
    }


def answer(data, etag: str = ETAG) -> Response:
    return Response(json.dumps(data).encode(), ETag=etag)


def offered(version: str = "1.2.0", **kwargs) -> updates.Release:
    release = updates.pick_release(release_json(version, **kwargs))
    assert release is not None
    return release


def http_error(code: int, reason: str = "") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(updates.LATEST_URL, code, reason, {}, None)


# Versions


@pytest.mark.parametrize(
    ("tag", "running", "newer"),
    [
        ("v1.2.0", "1.1.0", True),
        ("1.2.0", "1.1.0", True),  # the tag's v is optional
        ("v1.1.1", "1.1.0", True),
        ("v1.10.0", "1.9.9", True),  # numbers, not text
        ("v2.0.0", "1.99.99", True),
        ("v1.1.0", "1.1.0", False),
        ("v1.0.9", "1.1.0", False),
        ("v0.9.0", "1.1.0", False),
        ("v1.2", "1.1.0", False),  # never newer when it isn't X.Y.Z
        ("v1.2.0.1", "1.1.0", False),
        ("v1.2.0-rc1", "1.1.0", False),
        ("V1.2.0", "1.1.0", False),
        ("v1.2.x", "1.1.0", False),
        ("v１.2.0", "1.1.0", False),  # a full-width digit
        ("latest", "1.1.0", False),
        ("", "1.1.0", False),
        ("v1.2.0", "dev", False),  # nor when the running version isn't
    ],
)
def test_a_tag_is_newer_only_when_its_numbers_are_higher(tag, running, newer):
    assert updates.is_newer(tag, running) is newer


def test_the_running_version_is_the_apps_own_unless_told_otherwise():
    assert updates.is_newer("v1.2.0") and not updates.is_newer("v1.1.0")


def test_only_an_installed_app_checks_for_updates(monkeypatch):
    assert updates.enabled() is processes.FROZEN is False
    monkeypatch.setattr(processes, "FROZEN", True)
    assert updates.enabled() is True


# The release and its installer


def test_the_installer_is_the_asset_named_for_the_version():
    other = {"name": "GranolaAutostart-1.2.0-setup.exe.zip", "browser_download_url": "https://x/y", "size": 1, "digest": f"sha256:{'0' * 64}"}
    older = {**other, "name": "GranolaAutostart-1.1.0-setup.exe"}
    data = release_json(assets=[other, "not an asset", older, release_json()["assets"][0]])
    assert updates.pick_release(data) == updates.Release(
        version="1.2.0",
        url="https://github.com/cfoucher/granola-autostart/releases/download/v1.2.0/GranolaAutostart-1.2.0-setup.exe",
        size=len(INSTALLER),
        sha256=SHA256,
        page="https://github.com/cfoucher/granola-autostart/releases/tag/v1.2.0",
    )


def test_the_digest_may_be_in_capitals():
    assert offered(digest=f"sha256:{SHA256.upper()}").sha256 == SHA256


@pytest.mark.parametrize(
    "data",
    [
        release_json(assets=[]),
        release_json(assets=None, tag_name="v1.2.0") | {"assets": None},
        release_json(assets=[{"name": "GranolaAutostart-1.1.0-setup.exe"}]),
        release_json(digest=None),
        release_json(digest="md5:" + "0" * 32),
        release_json(digest="sha256:abc"),
        release_json(digest=f"SHA256:{SHA256}"),
        release_json(size=0),
        release_json(size=updates.MAX_INSTALLER_BYTES + 1),
        release_json(size="26"),
        release_json(tag_name="nightly"),
        release_json() | {"tag_name": None},
    ],
)
def test_a_release_without_a_usable_installer_is_not_offered(data, caplog):
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        assert updates.pick_release(data) is None
    assert "update check:" in caplog.text


def test_an_installer_that_is_not_https_is_not_offered():
    data = release_json()
    data["assets"][0]["browser_download_url"] = "http://github.com/x/GranolaAutostart-1.2.0-setup.exe"
    assert updates.pick_release(data) is None


def test_a_release_page_that_is_not_https_is_left_out_but_the_release_is_still_offered():
    assert offered().page.startswith("https://github.com/")
    for page in ("http://github.com/x", "ftp://github.com/x", None):
        assert offered(html_url=page).page == ""
    data = release_json()
    del data["html_url"]
    assert updates.pick_release(data).page == ""


@pytest.mark.parametrize(
    "raw",
    [
        None,
        [],
        "release",
        {},
        {"version": "1.2.0", "url": DOWNLOAD, "size": 3},  # no sha256
        {"version": "v1.2.0", "url": DOWNLOAD, "size": 3, "sha256": SHA256},  # the tag's v is not the version
        {"version": "1.2", "url": DOWNLOAD, "size": 3, "sha256": SHA256},
        {"version": "../1.2.0", "url": DOWNLOAD, "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": "file:///C:/x.exe", "size": 3, "sha256": SHA256},
        # not this repository's download of that version's installer
        {"version": "1.2.0", "url": "https://x/y", "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("https://", "http://"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("cfoucher", "someone-else"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("github.com", "github.com.example"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("v1.2.0/", "v1.3.0/"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("1.2.0-setup", "1.3.0-setup"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD.replace("v1.2.0/", "../../x/"), "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD + "?x=1", "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD + "/", "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": 5, "size": 3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD, "size": True, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD, "size": 3.0, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD, "size": -3, "sha256": SHA256},
        {"version": "1.2.0", "url": DOWNLOAD, "size": 3, "sha256": "z" * 64},
        {"version": "1.2.0", "url": DOWNLOAD, "size": 3, "sha256": SHA256, "page": 7},
        {"version": 120, "url": DOWNLOAD, "size": 3, "sha256": SHA256},
    ],
)
def test_a_saved_release_that_cannot_be_trusted_is_dropped(raw):
    assert updates.Release.from_json(raw) is None


def test_a_download_under_a_tag_without_the_v_is_this_repositorys_too():
    raw = {"version": "1.2.0", "url": DOWNLOAD.replace("v1.2.0/", "1.2.0/"), "size": 3, "sha256": SHA256}
    assert updates.Release.from_json(raw) is not None


def test_a_release_survives_update_json():
    release = offered()
    assert updates.Release.from_json(json.loads(json.dumps(release.to_json()))) == release


# update.json


def test_the_whole_state_survives_update_json():
    state = updates.UpdateState(NOW, ETAG, offered(), "Couldn't check for updates (timed out). Trying again tomorrow.", updates.MISMATCH)
    update_path().write_text(json.dumps(state.to_json()), encoding="utf-8")
    assert updates.read_state() == state


def test_a_missing_or_garbled_update_file_reads_as_empty():
    assert updates.read_state() == updates.UpdateState()
    update_path().write_text("{not json", encoding="utf-8")
    assert updates.read_state() == updates.UpdateState()
    update_path().write_bytes(b"\xff\xfe{}")
    assert updates.read_state() == updates.UpdateState()
    for raw in (None, [], "update", 5):
        assert updates.UpdateState.from_json(raw) == updates.UpdateState()


def test_what_cannot_be_read_from_update_json_comes_back_empty_field_by_field():
    garbled = {
        "checked_at": "yesterday",
        "etag": 5,
        "latest": {"version": "1.2"},
        "error": ["a list"],
        "install_error": "",
    }
    assert updates.UpdateState.from_json(garbled) == updates.UpdateState()
    assert updates.UpdateState.from_json({"checked_at": 5, "error": "kept"}) == updates.UpdateState(error="kept")


def test_a_hand_edited_time_without_an_offset_is_read_as_local_time():
    state = updates.UpdateState.from_json({"checked_at": "2026-11-01T09:12"})
    assert state.checked_at == datetime(2026, 11, 1, 9, 12).astimezone()
    assert state.checked_at.tzinfo is not None


def test_an_update_is_available_only_when_the_latest_release_is_newer():
    assert updates.UpdateState().available() is None
    assert updates.UpdateState(latest=offered("1.2.0")).available() == offered("1.2.0")
    assert updates.UpdateState(latest=offered("1.1.0")).available() is None  # what is running
    assert updates.UpdateState(latest=offered("1.0.0")).available() is None


# Checking


def test_a_check_asks_github_for_the_latest_release_and_saves_what_it_finds(monkeypatch):
    github = serving(monkeypatch, answer(release_json()))
    state = updates.check(NOW)
    [(request, timeout)] = github.requests
    assert request.full_url == "https://api.github.com/repos/cfoucher/granola-autostart/releases/latest"
    assert timeout == 20
    assert request.get_header("User-agent") == "granola-autostart/1.1.0"
    assert request.get_header("Accept") == "application/vnd.github+json"
    assert not request.has_header("If-none-match")  # nothing seen yet
    assert state == updates.UpdateState(checked_at=NOW, etag=ETAG, latest=offered())
    assert updates.read_state() == state
    assert state.available() == offered()


def test_a_check_sends_the_etag_it_saved_and_a_304_keeps_the_last_result(monkeypatch):
    github = serving(monkeypatch, answer(release_json()), http_error(304, "Not Modified"))
    updates.check(NOW)
    later = NOW + timedelta(days=1)
    state = updates.check(later)
    assert github.requests[1][0].get_header("If-none-match") == ETAG
    assert state == updates.UpdateState(checked_at=later, etag=ETAG, latest=offered())


def test_a_304_clears_an_error_from_the_check_before(monkeypatch):
    serving(monkeypatch, answer(release_json()), http_error(500, "Server Error"), http_error(304, "Not Modified"))
    updates.check(NOW)
    assert updates.check(NOW).error
    state = updates.check(NOW)
    assert state.error is None and state.latest == offered()


@pytest.mark.parametrize("code", [304, 403, 404, 500])
def test_an_http_answer_that_is_an_error_is_closed_once_it_is_dealt_with(code, monkeypatch):
    body = io.BytesIO(b"nothing to read")
    serving(monkeypatch, urllib.error.HTTPError(updates.LATEST_URL, code, "x", {}, body))
    updates.check(NOW)
    assert body.closed  # an HTTPError holds its connection until it is closed


@pytest.mark.parametrize("code", [403, 429])
def test_the_rate_limit_is_said_plainly_and_the_last_result_stays(code, monkeypatch, caplog):
    serving(monkeypatch, answer(release_json()), http_error(code, "rate limit exceeded"))
    updates.check(NOW)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = updates.check(NOW + timedelta(days=1))
    assert state.error == "GitHub's limit on update checks was reached. Trying again tomorrow."
    assert (state.latest, state.etag, state.checked_at) == (offered(), ETAG, NOW + timedelta(days=1))
    assert state.available() == offered()
    assert "update check failed" in caplog.text and state.error in caplog.text


def test_no_release_yet_is_no_update_and_no_error(monkeypatch):
    serving(monkeypatch, answer(release_json()), http_error(404, "Not Found"))
    updates.check(NOW)
    state = updates.check(NOW)
    assert state == updates.UpdateState(checked_at=NOW)


@pytest.mark.parametrize(
    ("failure", "said", "etag"),
    [
        (http_error(500, "Internal Server Error"), "500 Internal Server Error", ETAG),
        (urllib.error.URLError("getaddrinfo failed"), "couldn't connect", ETAG),
        (urllib.error.URLError(TimeoutError()), "timed out", ETAG),
        (TimeoutError(), "timed out", ETAG),
        (ConnectionResetError("reset"), "ConnectionResetError", ETAG),
        (http.client.IncompleteRead(b"{"), "IncompleteRead", ETAG),
        # An answer that makes no sense, or a request that couldn't be made, may be the saved ETag's doing: it goes.
        (Response(b"{ not json"), "unexpected answer", None),
        (Response(b"[]"), "unexpected answer", None),
        (Response(b"\xff\xfe"), "unexpected answer", None),
        (Response(b" " * (updates.MAX_RELEASE_BYTES + 1)), "unexpected answer", None),
        (ValueError("Invalid header value"), "unexpected answer", None),
    ],
)
def test_a_check_that_fails_is_recorded_and_never_raised(failure, said, etag, monkeypatch, caplog):
    serving(monkeypatch, answer(release_json()), failure)
    updates.check(NOW)
    later = NOW + timedelta(days=1)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = updates.check(later)
    assert state.error == f"Couldn't check for updates ({said}). Trying again tomorrow."
    assert (state.latest, state.etag, state.checked_at) == (offered(), etag, later)  # an update already found stays on offer
    assert updates.read_state() == state and "update check failed" in caplog.text


def test_a_check_that_cannot_be_made_over_a_bad_etag_asks_again_without_it(monkeypatch):
    class Refusing:
        """http.client refuses a header value with a line break in it, and says so with a ValueError."""

        def __init__(self):
            self.requests = []

        def open(self, request, timeout):
            self.requests.append(request)
            if request.has_header("If-none-match"):
                raise ValueError("Invalid header value")
            return answer(release_json(), etag='W/"fresh"')

    github = Refusing()
    monkeypatch.setattr(updates, "_opener", github)
    update_path().write_text(json.dumps({"etag": ETAG, "latest": offered().to_json()}), encoding="utf-8")
    assert updates.check(NOW).etag is None  # that one failed, and the ETag that may have been why went with it
    state = updates.check(NOW)
    assert not github.requests[1].has_header("If-none-match")
    assert (state.error, state.etag, state.latest) == (None, 'W/"fresh"', offered())


@pytest.mark.parametrize(
    "etag",
    ['W/"4f2a"\r\nX-Injected: 1', 'W/"4f2a"\n', 'W/"caf\u00e9"', "\x00", "x" * (updates.MAX_ETAG_LENGTH + 1), "", 5, ["W/"], True],
)
def test_an_etag_that_is_not_safe_to_send_back_is_dropped_when_update_json_is_read(etag, monkeypatch):
    assert updates.UpdateState.from_json({"etag": etag}).etag is None
    update_path().write_text(json.dumps({"etag": etag, "latest": offered().to_json()}), encoding="utf-8")
    github = serving(monkeypatch, answer(release_json()))
    updates.check(NOW)
    assert not github.requests[0][0].has_header("If-none-match")


@pytest.mark.parametrize("etag", ['W/"4f2a"', '"abc def"', "x" * updates.MAX_ETAG_LENGTH, "a"])
def test_an_ordinary_etag_is_kept(etag):
    assert updates.UpdateState.from_json({"etag": etag}).etag == etag


@pytest.mark.parametrize("etag", ['W/"4f2a"\r\nX-Injected: 1', 'W/"caf\u00e9"', "x" * (updates.MAX_ETAG_LENGTH + 1), ""])
def test_an_etag_from_github_that_is_not_safe_to_send_back_is_not_saved(etag, monkeypatch):
    serving(monkeypatch, Response(json.dumps(release_json()).encode(), ETag=etag))
    assert updates.check(NOW) == updates.UpdateState(checked_at=NOW, etag=None, latest=offered())
    assert updates.read_state().etag is None


def test_a_check_reads_the_answer_as_it_arrives_and_gives_up_on_a_server_that_trickles(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(updates, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    body = json.dumps(release_json()).encode()
    serving(monkeypatch, answer(release_json()), Trickling(body, clock, seconds=updates.CHECK_SECONDS / 4))
    updates.check(NOW)
    later = NOW + timedelta(days=1)
    state = updates.check(later)
    assert state.error == "Couldn't check for updates (timed out). Trying again tomorrow."
    assert (state.latest, state.etag, state.checked_at) == (offered(), ETAG, later)  # the last result stays, and so does its ETag
    assert clock[0] < 1000.0 + updates.CHECK_SECONDS * 2  # it stopped after a few bytes, not after all of them


def test_a_check_that_runs_out_of_time_is_recorded_and_not_raised(monkeypatch):
    serving(monkeypatch, answer(release_json()))
    monkeypatch.setattr(updates, "CHECK_SECONDS", -1)
    state = updates.check(NOW)
    assert state.error == "Couldn't check for updates (timed out). Trying again tomorrow."
    assert state.latest is None


def test_an_answer_over_the_size_limit_is_dropped_before_the_rest_of_it_is_read(monkeypatch):
    big = Counted(b" " * (updates.MAX_RELEASE_BYTES * 3))
    serving(monkeypatch, big)
    assert updates.check(NOW).error == "Couldn't check for updates (unexpected answer). Trying again tomorrow."
    assert updates.MAX_RELEASE_BYTES < big.consumed < updates.MAX_RELEASE_BYTES + 2 * updates.CHUNK_BYTES


def test_a_check_after_a_failure_clears_the_error(monkeypatch):
    serving(monkeypatch, urllib.error.URLError("down"), answer(release_json()))
    assert updates.check(NOW).error
    assert updates.check(NOW).error is None


def test_a_release_without_an_installer_yet_leaves_the_last_one_on_offer_until_the_installer_arrives(monkeypatch, caplog):
    # The release is tagged 1.3.0, and Build attaches its installer a little later.
    serving(
        monkeypatch,
        answer(release_json()),
        answer(release_json("1.3.0", assets=[]), etag='W/"b"'),
        http_error(304, "Not Modified"),
        answer(release_json("1.3.0"), etag='W/"c"'),
    )
    updates.check(NOW)
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        state = updates.check(NOW)
    assert (state.latest, state.etag, state.error) == (offered(), 'W/"b"', None)  # the ETag moves on, so a 304 works
    assert state.available() == offered() and "has no GranolaAutostart-1.3.0-setup.exe" in caplog.text
    assert updates.check(NOW).latest == offered()  # a 304 reuses it
    state = updates.check(NOW)
    assert (state.latest, state.etag) == (offered("1.3.0"), 'W/"c"')


def test_a_first_check_that_finds_no_installer_offers_nothing(monkeypatch):
    serving(monkeypatch, answer(release_json("1.3.0", assets=[]), etag='W/"b"'))
    assert updates.check(NOW) == updates.UpdateState(checked_at=NOW, etag='W/"b"')


def test_a_release_without_an_installer_does_not_clear_the_install_error_of_the_one_on_offer(monkeypatch):
    serving(monkeypatch, answer(release_json()), answer(release_json("1.3.0", assets=[]), etag='W/"b"'))
    updates.check(NOW)
    updates._record_install_error(updates.MISMATCH)
    assert updates.check(NOW).install_error == updates.MISMATCH


def test_a_release_that_has_gone_clears_the_one_on_offer(monkeypatch):
    serving(monkeypatch, answer(release_json()), http_error(404, "Not Found"))
    updates.check(NOW)
    assert updates.check(NOW).latest is None  # no release at all is not a release still to come


def test_an_install_error_stays_until_a_different_release_comes_out(monkeypatch):
    serving(monkeypatch, answer(release_json()), answer(release_json()), http_error(304, "Not Modified"), answer(release_json("1.3.0")))
    updates.check(NOW)
    updates._record_install_error(updates.MISMATCH)
    assert updates.check(NOW).install_error == updates.MISMATCH
    assert updates.check(NOW).install_error == updates.MISMATCH
    state = updates.check(NOW)
    assert state.latest.version == "1.3.0" and state.install_error is None


def test_a_check_still_runs_over_an_unreadable_update_file(monkeypatch):
    serving(monkeypatch, answer(release_json()))
    update_path().write_text("{not json", encoding="utf-8")
    assert updates.check(NOW).latest == offered()


def test_an_update_file_another_program_holds_open_is_logged_not_raised(monkeypatch, caplog):
    def locked(path, data, **kwargs):
        raise PermissionError("held by antivirus")

    serving(monkeypatch, answer(release_json()))
    monkeypatch.setattr(updates, "write_json", locked)
    state = updates.check(NOW)
    assert state.latest == offered() and not update_path().exists()
    assert "could not save update.json (PermissionError)" in caplog.text


# When the watcher checks


def test_the_first_check_is_a_while_after_the_watcher_starts_and_then_every_day(monkeypatch):
    checked = []
    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "check", checked.append)
    monkeypatch.setattr(updates, "_first_check_delay", lambda: timedelta(minutes=7))
    checker = updates.Checker()

    def tick(at: datetime) -> None:
        checker.tick(at)
        if checker.thread is not None:
            checker.thread.join(10)

    tick(NOW)
    tick(NOW + timedelta(minutes=6, seconds=59))
    assert checked == []
    first = NOW + timedelta(minutes=7)
    tick(first)
    tick(first + timedelta(hours=23, minutes=59))
    assert checked == [first]
    tick(first + timedelta(days=1))
    tick(first + timedelta(days=1, minutes=1))
    assert checked == [first, first + timedelta(days=1)]


def test_the_first_check_comes_at_a_random_time_between_a_minute_and_half_an_hour(monkeypatch):
    low, high = updates.FIRST_CHECK_DELAY
    assert (low, high) == (timedelta(minutes=1), timedelta(minutes=30))
    delays = [updates._first_check_delay() for _ in range(200)]
    assert all(low <= delay <= high for delay in delays)
    assert len(set(delays)) > 100  # not one fixed moment, which would put every copy on GitHub at once
    monkeypatch.setattr(updates.random, "random", lambda: 0.0)
    assert updates._first_check_delay() == low
    monkeypatch.setattr(updates.random, "random", lambda: 1.0)
    assert updates._first_check_delay() == high


def test_each_start_picks_its_own_delay(monkeypatch):
    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "check", lambda now: pytest.fail("checked before it was due"))
    delays = iter([timedelta(minutes=3), timedelta(minutes=20)])
    monkeypatch.setattr(updates, "_first_check_delay", lambda: next(delays))
    first, second = updates.Checker(), updates.Checker()
    first.tick(NOW)
    second.tick(NOW)
    assert (first.due, second.due) == (NOW + timedelta(minutes=3), NOW + timedelta(minutes=20))


def test_check_now_has_no_delay(monkeypatch):
    monkeypatch.setattr(updates, "_first_check_delay", lambda: pytest.fail("Check now waited"))
    serving(monkeypatch, answer(release_json()))
    assert updates.check(NOW).latest == offered()


def test_a_slow_check_never_holds_up_a_tick(monkeypatch):
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "check", lambda now: (started.set(), release.wait(10)))
    monkeypatch.setattr(updates, "_first_check_delay", lambda: timedelta(minutes=1))
    checker = updates.Checker()
    checker.tick(NOW)
    checker.tick(NOW + timedelta(minutes=1))
    assert started.wait(10) and checker.thread.is_alive()
    release.set()
    checker.thread.join(10)


def test_a_check_that_fails_unexpectedly_is_logged_and_the_next_day_tries_again(monkeypatch, caplog):
    attempts = []

    def broken(now):
        attempts.append(now)
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(updates, "enabled", lambda: True)
    monkeypatch.setattr(updates, "check", broken)
    monkeypatch.setattr(updates, "_first_check_delay", lambda: timedelta(minutes=1))
    checker = updates.Checker()
    for at in (NOW, NOW + timedelta(minutes=1), NOW + timedelta(days=1, minutes=1)):
        checker.tick(at)
        if checker.thread is not None:
            checker.thread.join(10)
    assert len(attempts) == 2 and "update check failed" in caplog.text


def test_running_from_source_never_checks(monkeypatch):
    monkeypatch.setattr(updates, "check", lambda now: pytest.fail("checked from source"))
    checker = updates.Checker()
    for days in range(3):
        checker.tick(NOW + timedelta(days=days))
    assert checker.thread is None and checker.due is None


# Installing


@pytest.fixture
def started(monkeypatch) -> list[list[str]]:
    """The installer's command line, recorded instead of run."""
    commands_run: list[list[str]] = []
    monkeypatch.setattr(processes, "spawn_detached", commands_run.append)
    return commands_run


def installer_files(tmp_path: Path) -> list[str]:
    folder = tmp_path / "updates"
    return sorted(path.name for path in folder.iterdir()) if folder.exists() else []


def test_installing_downloads_checks_and_starts_the_installer(monkeypatch, tmp_path, started):
    github = serving(monkeypatch, Response(INSTALLER))
    updates._record_install_error("An old failure.")
    release = offered()
    assert updates.install(release) is updates.Install.STARTED
    [(request, timeout)] = github.requests
    assert request.full_url == release.url and timeout == 20
    assert request.get_header("User-agent") == "granola-autostart/1.1.0"
    folder = tmp_path / "updates"
    installer = folder / "GranolaAutostart-1.2.0-setup.exe"
    assert installer.read_bytes() == INSTALLER and installer_files(tmp_path) == [installer.name]  # no .part left
    assert started == [[str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={folder / 'install-log.txt'}"]]
    assert updates.read_state().install_error is None


def test_a_download_that_is_not_the_released_file_is_deleted_and_never_run(monkeypatch, tmp_path, started, caplog):
    serving(monkeypatch, Response(INSTALLER.upper()))  # the right size, the wrong bytes
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        assert updates.install(offered()) is updates.Install.FAILED
    assert installer_files(tmp_path) == [] and started == []
    assert updates.read_state().install_error == (
        "The download didn't match the release, so it wasn't installed. Try again, or download it from the release page."
    )
    assert "deleted it" in caplog.text


def test_a_download_longer_than_the_release_says_is_stopped_and_deleted(monkeypatch, tmp_path, started):
    body = Counted(b"x" * (10 * updates.CHUNK_BYTES))
    serving(monkeypatch, body)
    assert updates.install(offered(size=20)) is updates.Install.FAILED
    assert body.consumed <= updates.CHUNK_BYTES  # it stopped at the first read past the size
    assert installer_files(tmp_path) == [] and started == []
    assert updates.read_state().install_error == updates.MISMATCH


def test_a_download_the_server_ends_early_is_a_failed_connection_not_a_different_file(monkeypatch, tmp_path, started):
    serving(monkeypatch, Response(INSTALLER[:-1]), Response(b""))
    for _ in range(2):
        assert updates.install(offered()) is updates.Install.FAILED
        assert installer_files(tmp_path) == [] and started == []
        assert updates.read_state().install_error == (
            "Couldn't download the update (download was cut short). Try again, or download it from the release page."
        )


def test_the_reason_a_download_was_cut_short_is_one_describe_error_says_as_it_is():
    assert describe_error(ValueError(updates.CUT_SHORT)) == "download was cut short"


def test_a_download_that_cannot_finish_says_why_and_leaves_nothing_behind(monkeypatch, tmp_path, started):
    release = offered()
    for failure, said in (
        (http_error(404, "Not Found"), "404 Not Found"),
        (urllib.error.URLError("getaddrinfo failed"), "couldn't connect"),
        (Dropping(INSTALLER), "ConnectionResetError"),
    ):
        serving(monkeypatch, failure)
        assert updates.install(release) is updates.Install.FAILED
        assert updates.read_state().install_error == f"Couldn't download the update ({said}). Try again, or download it from the release page."
        assert installer_files(tmp_path) == []
    assert started == []


def test_a_download_that_takes_too_long_is_given_up_on(monkeypatch, tmp_path, started):
    serving(monkeypatch, Response(INSTALLER))
    monkeypatch.setattr(updates, "DOWNLOAD_SECONDS", -1)
    assert updates.install(offered()) is updates.Install.FAILED
    assert "(timed out)" in updates.read_state().install_error and installer_files(tmp_path) == []


def test_a_server_that_trickles_is_given_up_on_at_the_deadline_not_when_the_file_is_in(monkeypatch, tmp_path, started):
    clock = [1000.0]
    monkeypatch.setattr(updates, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    serving(monkeypatch, Trickling(INSTALLER, clock, seconds=updates.DOWNLOAD_SECONDS / 4))
    assert updates.install(offered()) is updates.Install.FAILED
    assert "(timed out)" in updates.read_state().install_error and installer_files(tmp_path) == [] and started == []
    assert clock[0] < 1000.0 + updates.DOWNLOAD_SECONDS * 2  # it stopped after a few bytes, not after all of them


def test_an_installer_that_cannot_start_is_reported(monkeypatch, tmp_path, caplog):
    def blocked(command):
        raise PermissionError("quarantined")

    serving(monkeypatch, Response(INSTALLER))
    monkeypatch.setattr(processes, "spawn_detached", blocked)
    assert updates.install(offered()) is updates.Install.FAILED
    assert updates.read_state().install_error == "The installer couldn't start. Try again, or download it from the release page."
    assert "could not start the installer for version 1.2.0 (PermissionError)" in caplog.text


def test_a_failed_install_can_be_tried_again(monkeypatch, tmp_path, started):
    serving(monkeypatch, Response(INSTALLER.upper()), Response(INSTALLER))
    release = offered()
    assert updates.install(release) is updates.Install.FAILED
    assert updates.read_state().install_error
    assert updates.install(release) is updates.Install.STARTED
    assert updates.read_state().install_error is None and len(started) == 1


def test_a_countdown_card_on_screen_stops_the_install_before_anything_is_downloaded(started):
    assert processes.hold_mutex(processes.CARD_MUTEX)
    try:
        assert updates.install(offered()) is updates.Install.LATER  # the opener refuses every request
    finally:
        processes.release_mutex(processes.CARD_MUTEX)
    assert started == [] and updates.read_state().install_error is None


def test_a_card_that_comes_up_during_the_download_stops_the_install_before_the_installer_starts(monkeypatch, tmp_path, started, caplog):
    cards = iter([False, True])  # none when install() looked, one by the time the download is done
    monkeypatch.setattr(processes, "card_showing", lambda: next(cards))
    serving(monkeypatch, Response(INSTALLER))
    with caplog.at_level(logging.INFO, logger="granola_autostart"):
        assert updates.install(offered()) is updates.Install.LATER
    assert started == [] and updates.read_state().install_error is None  # nothing went wrong: Settings says to wait
    assert "a countdown card came up while version 1.2.0 downloaded" in caplog.text
    # The checked installer is kept, and the next click runs it without downloading it again.
    assert installer_files(tmp_path) == ["GranolaAutostart-1.2.0-setup.exe"]
    monkeypatch.setattr(processes, "card_showing", lambda: False)
    github = serving(monkeypatch)  # any request would fail
    assert updates.install(offered()) is updates.Install.STARTED and len(started) == 1
    assert github.requests == [] and updates.read_state().install_error is None


def test_a_kept_installer_that_is_not_the_released_file_is_downloaded_again(monkeypatch, tmp_path, started):
    folder = tmp_path / "updates"
    folder.mkdir()
    installer = folder / "GranolaAutostart-1.2.0-setup.exe"
    for stale in (INSTALLER.upper(), INSTALLER[:-1], b""):  # the right size and wrong bytes, too short, empty
        installer.write_bytes(stale)
        github = serving(monkeypatch, Response(INSTALLER))
        assert updates.install(offered()) is updates.Install.STARTED
        assert len(github.requests) == 1 and installer.read_bytes() == INSTALLER
    assert len(started) == 3


def test_a_card_that_is_up_when_the_click_comes_does_not_touch_the_error_or_the_kept_installer(monkeypatch, tmp_path, started):
    updates._record_install_error("An old failure.")
    folder = tmp_path / "updates"
    folder.mkdir()
    (folder / "GranolaAutostart-1.2.0-setup.exe").write_bytes(INSTALLER)
    assert processes.hold_mutex(processes.CARD_MUTEX)
    try:
        assert updates.install(offered()) is updates.Install.LATER
    finally:
        processes.release_mutex(processes.CARD_MUTEX)
    assert started == [] and updates.read_state().install_error == "An old failure."
    assert installer_files(tmp_path) == ["GranolaAutostart-1.2.0-setup.exe"]


class Locked:
    """A file held open the way Windows holds a running program's: others may read it but not write to it."""

    def __init__(self, path: Path) -> None:
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.CreateFileW.restype = ctypes.c_void_p
        self.kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        self.kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        self.path = path

    def __enter__(self):
        generic_read, share_read, open_existing = 0x80000000, 0x1, 3
        self.handle = self.kernel32.CreateFileW(str(self.path), generic_read, share_read, None, open_existing, 0, None)
        assert self.handle not in (None, ctypes.c_void_p(-1).value)
        return self

    def __exit__(self, *_exc):
        self.kernel32.CloseHandle(self.handle)


def running_installers(monkeypatch, *image_names: str) -> None:
    """The process list has these (in lower case, as calls.process_table gives them) and some of Windows' own."""
    table = {"system": [4], "explorer.exe": [900], **{name: [4242] for name in image_names}}
    monkeypatch.setattr(updates, "process_table", lambda: table)


@pytest.mark.parametrize(
    "image_name",
    [
        "granolaautostart-1.2.0-setup.exe",  # the setup program that was started
        "granolaautostart-1.2.0-setup.tmp",  # Inno Setup's copy of itself, which does the work
    ],
)
def test_an_installer_that_is_already_running_is_not_downloaded_over_or_started_again(image_name, monkeypatch, tmp_path, started):
    folder = tmp_path / "updates"
    folder.mkdir()
    installer = folder / "GranolaAutostart-1.2.0-setup.exe"
    installer.write_bytes(INSTALLER)
    updates._record_install_error("An old failure.")
    github = serving(monkeypatch)  # a download here would be the confusing error this is about
    running_installers(monkeypatch, image_name)
    # Seconds after the click that started it: the same as that click's answer, and nothing else happens.
    assert updates.install(offered()) is updates.Install.STARTED
    assert github.requests == [] and started == [] and installer_files(tmp_path) == [installer.name]
    assert updates.read_state().install_error == "An old failure."  # not cleared: nothing new was tried
    running_installers(monkeypatch)  # the installer has ended, so the next click starts it afresh
    assert updates.install(offered()) is updates.Install.STARTED and len(started) == 1


def test_another_versions_installer_running_does_not_stop_this_one_being_installed(monkeypatch, tmp_path, started):
    running_installers(monkeypatch, "granolaautostart-1.1.0-setup.exe", "granolaautostart-1.3.0-setup.exe", "granolaautostart.exe")
    serving(monkeypatch, Response(INSTALLER))
    assert updates.install(offered()) is updates.Install.STARTED and len(started) == 1


def test_an_installer_is_running_when_the_process_list_names_it(monkeypatch):
    installer = Path("C:/Users/someone/AppData/Local/granola-autostart/updates/GranolaAutostart-1.2.0-setup.exe")
    assert not updates._running(installer)  # conftest's list is empty
    running_installers(monkeypatch)
    assert not updates._running(installer)
    running_installers(monkeypatch, "granolaautostart-1.2.0-setup.exe")
    assert updates._running(installer)
    running_installers(monkeypatch, "granolaautostart-1.2.0-setup.tmp")
    assert updates._running(installer)


def test_a_file_that_antivirus_has_open_is_not_taken_for_a_running_installer(monkeypatch, tmp_path, started):
    folder = tmp_path / "updates"
    folder.mkdir()
    installer = folder / "GranolaAutostart-1.2.0-setup.exe"
    installer.write_bytes(INSTALLER)
    github = serving(monkeypatch)
    with Locked(installer):  # others may read it but not write to it, as with a running program: but nothing is running
        assert updates.install(offered()) is updates.Install.STARTED
    assert len(started) == 1 and github.requests == []  # it was started, which "already running" would not have done


def test_a_download_that_fails_with_an_http_status_closes_the_answer(monkeypatch, tmp_path, started):
    body = io.BytesIO(b"Not found")
    serving(monkeypatch, urllib.error.HTTPError(updates.LATEST_URL, 404, "Not Found", {}, body))
    assert updates.install(offered()) is updates.Install.FAILED
    assert body.closed  # it held the connection until it was
    assert updates.read_state().install_error == "Couldn't download the update (404 Not Found). Try again, or download it from the release page."


def test_two_installs_at_once_start_one_installer(monkeypatch, started):
    serving(monkeypatch, Response(INSTALLER))
    assert processes.hold_mutex(processes.INSTALL_MUTEX)  # another thread, or the other process (tray or Settings), is at it
    try:
        assert updates.install(offered()) is updates.Install.LATER
        assert processes.mutex_exists(processes.INSTALL_MUTEX)  # the one that is installing still holds it
    finally:
        processes.release_mutex(processes.INSTALL_MUTEX)
    assert started == []


def test_the_install_mutex_is_held_while_installing_and_let_go_afterwards_whatever_happens(monkeypatch, started):
    held = []

    def download(release, installer):
        held.append(processes.mutex_exists(processes.INSTALL_MUTEX))
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(updates, "_download", download)
    assert updates.install(offered()) is updates.Install.FAILED
    assert held == [True] and not processes.mutex_exists(processes.INSTALL_MUTEX)
    monkeypatch.setattr(updates, "_download", lambda release, installer: held.append(processes.mutex_exists(processes.INSTALL_MUTEX)))
    assert updates.install(offered()) is updates.Install.STARTED
    assert held == [True, True] and not processes.mutex_exists(processes.INSTALL_MUTEX)
    assert len(started) == 1


def test_a_download_is_checked_as_it_is_on_disk_not_as_it_was_sent(monkeypatch, tmp_path, started):
    real = updates._file_sha256

    def after_antivirus(path):
        path.write_bytes(path.read_bytes()[:-1] + b"?")  # changed after it was written, which a stream's hash can't see
        return real(path)

    serving(monkeypatch, Response(INSTALLER))  # what is sent is right
    monkeypatch.setattr(updates, "_file_sha256", after_antivirus)
    assert updates.install(offered()) is updates.Install.FAILED
    assert installer_files(tmp_path) == [] and started == []
    assert updates.read_state().install_error == updates.MISMATCH


def test_the_hash_is_of_the_files_whole_content(tmp_path):
    content = b"x" * (3 * updates.CHUNK_BYTES + 5)  # several reads, the last one short
    (tmp_path / "installer").write_bytes(content)
    assert updates._file_sha256(tmp_path / "installer") == hashlib.sha256(content).hexdigest()


# Cleaning up


def test_cleaning_up_deletes_old_installers_and_half_finished_downloads_only(tmp_path):
    folder = tmp_path / "updates"
    folder.mkdir()
    gone = ["GranolaAutostart-1.0.1-setup.exe", "GranolaAutostart-1.1.0-setup.exe", "GranolaAutostart-1.2.0-setup.exe.part", "x.part"]
    kept = ["GranolaAutostart-1.1.1-setup.exe", "GranolaAutostart-1.10.0-setup.exe", "install-log.txt", "notes.txt", "GranolaAutostart-1.0.0-setup.exe.bak"]
    for name in gone + kept:
        (folder / name).write_bytes(b"x")
    assert updates.clean_up() is True
    assert sorted(path.name for path in folder.iterdir()) == sorted(kept)


def test_cleaning_up_with_no_updates_folder_does_nothing(tmp_path):
    assert updates.clean_up() is True
    assert not (tmp_path / "updates").exists()


def test_a_file_that_cannot_be_deleted_is_logged_and_the_rest_still_go(monkeypatch, tmp_path, caplog):
    folder = tmp_path / "updates"
    folder.mkdir()
    for name in ("GranolaAutostart-1.0.0-setup.exe", "GranolaAutostart-1.1.0-setup.exe"):
        (folder / name).write_bytes(b"x")
    real_unlink = Path.unlink

    def unlink(path, missing_ok=False):
        if path.name.startswith("GranolaAutostart-1.0.0"):
            raise PermissionError("still running")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert updates.clean_up() is False  # worth trying again
    assert [path.name for path in folder.iterdir()] == ["GranolaAutostart-1.0.0-setup.exe"]
    assert "could not delete GranolaAutostart-1.0.0-setup.exe (PermissionError)" in caplog.text


def test_an_updates_folder_that_cannot_be_read_is_logged_not_raised(monkeypatch, tmp_path, caplog):
    (tmp_path / "updates").mkdir()

    def denied(path):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", denied)
    assert updates.clean_up() is False
    assert "could not read the updates folder (PermissionError)" in caplog.text


@pytest.fixture
def cleanings(monkeypatch):
    """What clean_up is asked, and what it answers: ``results`` is a list with one answer per call."""
    record = SimpleNamespace(asked=0, results=[])

    def clean_up():
        record.asked += 1
        return record.results.pop(0)

    monkeypatch.setattr(updates, "clean_up", clean_up)
    return record


def test_the_checker_clears_out_old_installers_on_its_first_tick_and_once_is_enough(cleanings):
    cleanings.results = [True]
    checker = updates.Checker()
    for minutes in range(0, 60, 5):
        checker.tick(NOW + timedelta(minutes=minutes))
    assert cleanings.asked == 1  # not enabled here, so this is all it did


def test_an_installer_still_in_use_is_tried_again_later_a_few_times_and_then_left(cleanings):
    cleanings.results = [False] * updates.CLEAN_ATTEMPTS
    checker = updates.Checker()
    checker.tick(NOW)
    assert cleanings.asked == 1
    checker.tick(NOW + updates.CLEAN_RETRY - timedelta(seconds=1))
    assert cleanings.asked == 1  # not yet
    for attempt in range(2, updates.CLEAN_ATTEMPTS + 1):
        checker.tick(NOW + updates.CLEAN_RETRY * (attempt - 1))
        assert cleanings.asked == attempt
    checker.tick(NOW + updates.CLEAN_RETRY * 10)
    checker.tick(NOW + timedelta(days=1))
    assert cleanings.asked == updates.CLEAN_ATTEMPTS  # a file that never lets go isn't logged about for ever


def test_the_retry_stops_once_everything_is_gone(cleanings):
    cleanings.results = [False, False, True]
    checker = updates.Checker()
    for minutes in range(0, 30, 2):
        checker.tick(NOW + timedelta(minutes=minutes))
    assert cleanings.asked == 3


def test_the_installer_that_just_ran_is_deleted_by_the_retry_once_it_has_finished(monkeypatch, tmp_path):
    folder = tmp_path / "updates"
    folder.mkdir()
    old = folder / "GranolaAutostart-1.1.0-setup.exe"
    old.write_bytes(b"x")
    real_unlink = Path.unlink
    running = [True]

    def unlink(path, missing_ok=False):
        if path == old and running[0]:
            raise PermissionError("the installer is still running")  # Windows won't delete a running exe
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    checker = updates.Checker()
    checker.tick(NOW)
    assert old.exists()
    running[0] = False
    checker.tick(NOW + updates.CLEAN_RETRY)
    assert not old.exists()
