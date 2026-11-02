"""In-app updates: ask GitHub for the latest release, and download and run its installer.

The last check's result is update.json. The watcher checks once a day, Settings' Check now checks at once,
and the tray and Settings both read the file, so they always agree. It is a file of its own because the
watcher rewrites status.json every tick. An update is installed only when someone clicks Install update:
the installer stops every process of the app, replaces its files, and starts the supervisor again.
"""

from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, processes
from .calls import process_table
from .net import https_opener
from .settings import app_dir, update_path, write_json
from .status import describe_error, saved_moment

log = logging.getLogger("granola_autostart")
# Where updates come from, and the only place an installer is downloaded from: a release's link is used only if it
# is this repository's own download address (Release.from_json). A fork changes this one line.
REPO = "cfoucher/granola-autostart"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOADS = f"https://github.com/{REPO}/releases/download/"
TIMEOUT_SECONDS = 20
CHECK_SECONDS = 60
DOWNLOAD_SECONDS = 600
CHUNK_BYTES = 64 * 1024
MAX_RELEASE_BYTES = 1024 * 1024
MAX_INSTALLER_BYTES = 200 * 1024 * 1024
MAX_ETAG_LENGTH = 200
# Once an installer has started it stops this app in a moment. One that was refused or cancelled never does, and
# Install update is offered again after this long. The tray waits this long, and Settings gets it from update_view.
INSTALL_WAIT_SECONDS = 120
# The first check after the watcher starts comes at a random time in this range, so a new release doesn't
# bring every copy behind one address to GitHub at the same moment.
FIRST_CHECK_DELAY = (timedelta(minutes=1), timedelta(minutes=30))
CHECK_INTERVAL = timedelta(days=1)
# An installer that is still running (the one that just updated this app) can't be deleted yet: try again.
CLEAN_RETRY = timedelta(minutes=2)
CLEAN_ATTEMPTS = 4
# Shown in Settings, so each is short and never includes an address.
RATE_LIMITED = "GitHub's limit on update checks was reached. Trying again tomorrow."
MISMATCH = "The download didn't match the release, so it wasn't installed. Try again, or download it from the release page."
NOT_STARTED = "The installer couldn't start. Try again, or download it from the release page."
# One of status.py's known reasons, so describe_error says it as it is.
CUT_SHORT = "download was cut short"
_NUMBERS = r"[0-9]{1,9}\.[0-9]{1,9}\.[0-9]{1,9}"
_VERSION = re.compile(_NUMBERS)
_TAG = re.compile(f"v?({_NUMBERS})")
_INSTALLER = re.compile(rf"GranolaAutostart-({_NUMBERS})-setup\.exe")
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")
_lock = threading.Lock()  # one change to update.json at a time in this process
# GitHub sends a download to its own download host, which is followed, but never to plain http.
_opener = https_opener()


class _Mismatch(Exception):
    """The download is not the file the release describes."""


def enabled() -> bool:
    """Only the installed app checks for updates and installs them. A source checkout updates through git."""
    return processes.FROZEN


def version_of(tag: str) -> str | None:
    """1.2.0 from a release tag such as v1.2.0, or None when the tag isn't X.Y.Z."""
    match = _TAG.fullmatch(tag)
    return match[1] if match else None


def is_newer(tag: str, running: str | None = None) -> bool:
    """Whether the release tag is a higher version than the running one, compared as numbers."""
    latest, current = version_of(tag), version_of(running or __version__)
    return latest is not None and current is not None and _numbers(latest) > _numbers(current)


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def updates_dir() -> Path:
    return app_dir() / "updates"


def installer_name(version: str) -> str:
    return f"GranolaAutostart-{version}-setup.exe"


def _download_urls(version: str) -> tuple[str, str]:
    """Where GitHub serves a version's installer: this repository's download under its tag, with or without the v."""
    return f"{DOWNLOADS}v{version}/{installer_name(version)}", f"{DOWNLOADS}{version}/{installer_name(version)}"


@dataclass(frozen=True)
class Release:
    """A release with an installer this app can use."""

    version: str  # 1.2.0
    url: str  # where the installer downloads from: this repository's release download
    size: int
    sha256: str  # of the installer, in lower case
    page: str  # the release page, for What's new, or "" if GitHub gave none

    def to_json(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_json(cls, raw) -> Release | None:
        """The installer is run from this, so anything missing or unusable gives None and nothing is guessed."""
        try:
            version, url, size, sha256, page = raw["version"], raw["url"], raw["size"], raw["sha256"], raw.get("page") or ""
        except (AttributeError, KeyError, TypeError):
            return None
        if not (
            isinstance(version, str)
            and _VERSION.fullmatch(version)
            and isinstance(url, str)
            and url in _download_urls(version)
            and type(size) is int
            and 0 < size <= MAX_INSTALLER_BYTES
            and isinstance(sha256, str)
            and _SHA256.fullmatch(sha256)
            and isinstance(page, str)
        ):
            return None
        return cls(version, url, size, sha256.lower(), page if page.startswith("https://") else "")


@dataclass
class UpdateState:
    """update.json. ``latest`` is the newest release GitHub named that has an installer, whatever its version,
    so a 304 Not Modified can reuse it. A newer release with no installer yet (Build attaches it a little after
    the tag) leaves the last one in place. ``error`` is why the last check failed and ``install_error`` why the
    last install did; each is None when it worked."""

    checked_at: datetime | None = None
    etag: str | None = None
    latest: Release | None = None
    error: str | None = None
    install_error: str | None = None

    def available(self) -> Release | None:
        """The release to offer: the latest one, when it is newer than the running version."""
        return self.latest if self.latest is not None and is_newer(self.latest.version) else None

    def to_json(self) -> dict:
        return {
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "etag": self.etag,
            "latest": self.latest.to_json() if self.latest else None,
            "error": self.error,
            "install_error": self.install_error,
        }

    @classmethod
    def from_json(cls, raw) -> UpdateState:
        """Anything missing or unreadable comes back empty, as if no check had run."""
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            checked_at=saved_moment(raw.get("checked_at")),
            etag=_etag(raw.get("etag")),
            latest=Release.from_json(raw.get("latest")),
            error=_text(raw.get("error")),
            install_error=_text(raw.get("install_error")),
        )


def _text(raw) -> str | None:
    return raw if isinstance(raw, str) and raw else None


def _etag(raw) -> str | None:
    """An ETag that is safe to send back in a header: printable ASCII, and not long. A damaged or hand-edited one
    would fail every request, and so every check, until the file was deleted."""
    return raw if isinstance(raw, str) and 0 < len(raw) <= MAX_ETAG_LENGTH and raw.isascii() and raw.isprintable() else None


def read_state() -> UpdateState:
    try:
        return UpdateState.from_json(json.loads(update_path().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return UpdateState()


def _update(change: Callable[[UpdateState], None]) -> UpdateState:
    """Apply ``change`` to update.json as it is now, and save it. Checks and installs run on different threads,
    and the tray and Settings are different processes, so each change starts from the file, not from a copy
    read before a slow download. A file another program holds open (antivirus, backup) is logged, never fatal."""
    with _lock:
        state = read_state()
        change(state)
        try:
            write_json(update_path(), state.to_json())
        except OSError as error:
            log.warning("could not save update.json (%s)", type(error).__name__)
        return state


def pick_release(data: dict) -> Release | None:
    """The release's installer, named GranolaAutostart-X.Y.Z-setup.exe, with its download link, size, and
    SHA-256. A release without one isn't offered, and that is logged, not shown."""
    tag = data.get("tag_name")
    version = version_of(tag) if isinstance(tag, str) else None
    if version is None:
        log.info("update check: the latest release's tag is not a version number")
        return None
    name = installer_name(version)
    assets = data.get("assets")
    assets = assets if isinstance(assets, list) else []
    asset = next((item for item in assets if isinstance(item, dict) and item.get("name") == name), None)
    if asset is None:
        log.info("update check: version %s has no %s to download", version, name)
        return None
    digest = asset.get("digest")
    release = Release.from_json(
        {
            "version": version,
            "url": asset.get("browser_download_url"),
            "size": asset.get("size"),
            "sha256": digest[len("sha256:") :] if isinstance(digest, str) and digest.startswith("sha256:") else None,
            "page": data.get("html_url"),
        }
    )
    if release is None:
        log.info("update check: version %s's %s has no usable download link, size, or sha256 digest", version, name)
    return release


def _user_agent() -> str:
    return f"granola-autostart/{__version__}"


def check(now: datetime) -> UpdateState:
    """Ask GitHub for the latest release and save what it said in update.json. A check that fails is
    recorded and logged, never raised, and leaves the last result in place."""
    request = urllib.request.Request(LATEST_URL, headers={"User-Agent": _user_agent(), "Accept": "application/vnd.github+json"})
    sent = read_state().etag
    if sent:
        request.add_header("If-None-Match", sent)
    unchanged = False  # 304: what the last check found still stands
    unusable = False  # the release came back, but with no installer this app can use (yet)
    nonsense = False  # the answer made no sense, or the request couldn't be made: the saved ETag is suspect
    release: Release | None = None
    etag: str | None = None
    error: str | None = None
    deadline = time.monotonic() + CHECK_SECONDS
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response:
            body = bytearray()
            for chunk in _chunks(response, deadline):
                body += chunk
                if len(body) > MAX_RELEASE_BYTES:
                    raise ValueError("release is too large")
            etag = _etag(response.headers.get("ETag"))
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError("release is not an object")
        release = pick_release(data)
        unusable = release is None
    except urllib.error.HTTPError as failure:
        failure.close()  # it holds the connection until it is closed
        if failure.code == 304:
            unchanged = True
        elif failure.code in (403, 429):
            error = RATE_LIMITED
        elif failure.code != 404:  # 404: no release yet, so no update and no error
            error = _failed(describe_error(failure))
    except ValueError:
        error, nonsense = _failed("unexpected answer"), True
    except Exception as failure:
        error = _failed(describe_error(failure))

    def record(state: UpdateState) -> None:
        state.checked_at, state.error = now, error
        if error is not None:
            if nonsense:
                state.etag = None  # a bad one would fail every check after this; a good one costs one full answer
        elif not unchanged:
            if not unusable:
                if release != state.latest:
                    state.install_error = None  # it was about another release
                state.latest = release
            state.etag = etag

    saved = _update(record)
    if saved.error:
        log.warning("update check failed: %s", saved.error)
    else:
        log.info("update check: the latest release is %s", saved.latest.version if saved.latest else "not offered")
    return saved


def _chunks(response, deadline: float) -> Iterator[bytes]:
    """What arrives of ``response``, a read at a time, until the deadline (a time.monotonic value) passes.

    read1 gives what has arrived, not a whole chunk, so a server that trickles can't keep this going much past the
    deadline: it is looked at after every read (so one read can run a little over it), and the socket's timeout ends
    a read that gets nothing."""
    while chunk := response.read1(CHUNK_BYTES):
        if time.monotonic() > deadline:
            raise TimeoutError("the update server took too long")
        yield chunk


def _failed(reason: str) -> str:
    return f"Couldn't check for updates ({reason}). Trying again tomorrow."


class Install(enum.Enum):
    """How ``install`` ended."""

    STARTED = "started"  # the installer is running (started now, or by an earlier click) and stops this app in a moment
    FAILED = "failed"  # nothing was started, and ``install_error`` says why
    LATER = "later"  # nothing was started and nothing is wrong: a countdown card is up, or another click is installing


def install(release: Release) -> Install:
    """Download ``release``'s installer, check it, and start it.

    The installer then stops this app, replaces it, and starts the new version, so this process ends soon
    after. It isn't started while a countdown card is on screen, or while the tray or a Settings window is
    already installing: that comes back as LATER, and an installer already downloaded and checked is kept
    for the next click. A failure is saved as ``install_error`` for Settings to show, and FAILED comes back.
    """
    if processes.card_showing():
        log.info("not installing version %s while a countdown card is on screen", release.version)
        return Install.LATER
    if not processes.hold_mutex(processes.INSTALL_MUTEX):
        log.info("version %s is already being installed", release.version)
        return Install.LATER
    try:
        return _install(release)
    finally:
        processes.release_mutex(processes.INSTALL_MUTEX)


def _install(release: Release) -> Install:
    folder = updates_dir()
    installer = folder / installer_name(release.version)
    # The mutex is let go as soon as the installer has started, so a click that comes seconds later (from the tray
    # after Settings, say) is told apart from a download that failed by the installer still being in the process list.
    if _running(installer):
        log.info("version %s's installer is already running", release.version)
        return Install.STARTED
    # Cleared first: once the installer starts it stops this process, so nothing could be saved afterwards.
    _record_install_error(None)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not _downloaded(release, installer):  # one kept from a click that was put off is as good
            _download(release, installer)
    except _Mismatch:
        log.warning("version %s's installer is not the file its release describes; deleted it", release.version)
        return _fail(MISMATCH)
    except Exception as error:
        log.warning("could not download version %s (%s)", release.version, describe_error(error))
        return _fail(f"Couldn't download the update ({describe_error(error)}). Try again, or download it from the release page.")
    # The download can take minutes, and a card may have come up since install() looked.
    if processes.card_showing():
        log.info("a countdown card came up while version %s downloaded; not installing it yet", release.version)
        return Install.LATER
    command = [str(installer), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", f"/LOG={folder / 'install-log.txt'}"]
    try:
        processes.spawn_detached(command)
    except OSError as error:
        log.warning("could not start the installer for version %s (%s)", release.version, type(error).__name__)
        return _fail(NOT_STARTED)
    log.info("started the installer for version %s", release.version)
    return Install.STARTED


def _fail(message: str) -> Install:
    _record_install_error(message)
    return Install.FAILED


def _running(installer: Path) -> bool:
    """Whether this installer is running, by its image name in the process list. Windows also refuses to write to a
    file that antivirus or a backup program has open, and that mustn't pass for a running installer.

    Inno Setup does its work from a copy of itself in a temporary folder, named .tmp, so that counts too."""
    table = process_table()
    return any(table.get(name.casefold()) for name in (installer.name, installer.with_suffix(".tmp").name))


def _downloaded(release: Release, installer: Path) -> bool:
    """Whether ``installer`` is already on disk and is the release's, by size and SHA-256."""
    try:
        return installer.stat().st_size == release.size and _file_sha256(installer) == release.sha256
    except OSError:
        return False


def _record_install_error(message: str | None) -> None:
    def record(state: UpdateState) -> None:
        state.install_error = message

    _update(record)


def _download(release: Release, installer: Path) -> None:
    """Fetch the installer through a .part file, and keep it only once its size and SHA-256 match the release."""
    part = installer.with_name(installer.name + ".part")
    request = urllib.request.Request(release.url, headers={"User-Agent": _user_agent()})
    total = 0
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    try:
        with _opener.open(request, timeout=TIMEOUT_SECONDS) as response, part.open("wb") as out:
            for chunk in _chunks(response, deadline):
                total += len(chunk)
                if total > release.size:
                    raise _Mismatch
                out.write(chunk)
        if total < release.size:
            raise ValueError(CUT_SHORT)  # the server ended it early: the network's doing, not a different file
        # The file on disk is what gets run, so it is that file that is checked, not what was sent: antivirus
        # and backup programs can change one after it is written.
        if _file_sha256(part) != release.sha256:
            raise _Mismatch
        part.replace(installer)
    except Exception as error:
        if isinstance(error, urllib.error.HTTPError):
            error.close()  # it holds the connection until it is closed
        _remove(part)
        raise


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _remove(path: Path) -> bool:
    """Delete ``path``. False, and logged, when it can't be."""
    try:
        path.unlink(missing_ok=True)
    except OSError as error:
        log.warning("could not delete %s (%s)", path.name, type(error).__name__)
        return False
    return True


def clean_up() -> bool:
    """Delete installers for versions up to the running one, and any half-finished download. The install log stays.
    False when something couldn't be deleted, which is worth another try later."""
    try:
        paths = list(updates_dir().iterdir())
    except FileNotFoundError:
        return True
    except OSError as error:
        log.warning("could not read the updates folder (%s)", type(error).__name__)
        return False
    running = _numbers(version_of(__version__) or "0.0.0")
    clean = True
    for path in paths:
        match = _INSTALLER.fullmatch(path.name)
        if path.suffix == ".part" or (match and _numbers(match[1]) <= running):
            clean = _remove(path) and clean
    return clean


def _first_check_delay() -> timedelta:
    """When the first check comes after the watcher starts: any moment in FIRST_CHECK_DELAY."""
    low, high = FIRST_CHECK_DELAY
    return low + (high - low) * random.random()


class Checker:
    """Checks for an update between a minute and half an hour after the watcher starts (FIRST_CHECK_DELAY), and
    then a day after each check. It also clears out old installers at the start, and again a few times if one
    is still in use: the installer that just updated this app may be running still.

    Each check runs on its own thread, so a slow network never holds up a tick. Updates don't depend on
    pausing, so a pause doesn't stop it.
    """

    def __init__(self) -> None:
        self.due: datetime | None = None
        self.thread: threading.Thread | None = None
        self.clean_due: datetime | None = None  # None until the first try
        self.clean_left = CLEAN_ATTEMPTS

    def tick(self, now: datetime) -> None:
        self._clean(now)
        if not enabled():
            return
        if self.due is None:
            self.due = now + _first_check_delay()
        if now < self.due:
            return
        self.due = now + CHECK_INTERVAL
        self.thread = threading.Thread(target=self._run, args=(now,), name="update-check", daemon=True)
        self.thread.start()

    def _clean(self, now: datetime) -> None:
        if self.clean_left == 0 or (self.clean_due is not None and now < self.clean_due):
            return
        self.clean_left -= 1
        if clean_up():
            self.clean_left = 0
        else:
            self.clean_due = now + CLEAN_RETRY

    def _run(self, now: datetime) -> None:
        try:
            check(now)
        except Exception:
            log.exception("update check failed")
