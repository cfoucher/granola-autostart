"""What the watcher knows, shared with the tray and the Settings window. Pure logic, no Windows calls.

The watcher writes status.json; Settings reads it. Nothing in it is a calendar address: feeds are
named by a hash of the address, which Settings recomputes from the addresses it decrypts.
"""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .settings import write_json

FAILURES_BEFORE_ALERT = 3
# Reasons that come from calendar_feed itself and are safe to show.
_KNOWN_REASONS = {"calendar feeds must use https", "calendar feed is too large", "calendar download was cut short"}


def feed_id(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:12]


def describe_error(error: BaseException) -> str:
    """A short reason for a failed download that never includes the address."""
    if isinstance(error, urllib.error.HTTPError):
        return f"{error.code} {error.reason}".strip()
    if isinstance(error, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(error, urllib.error.URLError):
        return "timed out" if isinstance(error.reason, (TimeoutError, socket.timeout)) else "couldn't connect"
    if isinstance(error, ValueError) and str(error) in _KNOWN_REASONS:
        return str(error)
    return type(error).__name__


@dataclass
class FeedStatus:
    id: str
    name: str | None = None
    owner: str | None = None
    meetings_next_day: int | None = None
    last_ok: datetime | None = None
    failures: int = 0
    last_error: str | None = None

    def succeeded(self, now: datetime, *, name: str | None, owner: str | None, meetings_next_day: int) -> None:
        self.name, self.owner, self.meetings_next_day = name, owner, meetings_next_day
        self.last_ok, self.failures, self.last_error = now, 0, None

    def failed(self, error: BaseException) -> None:
        self.failures += 1
        self.last_error = describe_error(error)

    def label(self) -> str:
        return f"“{self.name}” calendar" if self.name else "A calendar"

    @property
    def alerting(self) -> bool:
        return self.failures >= FAILURES_BEFORE_ALERT

    def failure_detail(self) -> str:
        reason = f" ({self.last_error})" if self.last_error else ""
        if self.alerting:
            return f"The last {self.failures} downloads failed{reason}. Meetings on this calendar won't start notes until it's fixed."
        return f"The last download failed{reason}. Trying again shortly."

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "owner": self.owner,
            "meetings_next_day": self.meetings_next_day,
            "last_ok": self.last_ok.isoformat() if self.last_ok else None,
            "failures": self.failures,
            "last_error": self.last_error,
        }

    @classmethod
    def from_json(cls, raw) -> FeedStatus | None:
        try:
            return cls(
                id=str(raw["id"]),
                name=raw.get("name"),
                owner=raw.get("owner"),
                meetings_next_day=None if raw.get("meetings_next_day") is None else int(raw["meetings_next_day"]),
                last_ok=_moment(raw.get("last_ok")),
                failures=int(raw.get("failures") or 0),
                last_error=raw.get("last_error"),
            )
        except (AttributeError, KeyError, OverflowError, TypeError, ValueError):  # OverflowError: Infinity
            return None


@dataclass(frozen=True)
class Problem:
    key: str
    title: str
    detail: str
    section: str  # the Settings section that fixes it
    actions: tuple[tuple[str, str], ...] = ()  # (button label, action id) for the notification, not saved

    def to_json(self) -> dict:
        return {"key": self.key, "title": self.title, "detail": self.detail, "section": self.section}

    @classmethod
    def from_json(cls, raw) -> Problem | None:
        try:
            return cls(str(raw["key"]), str(raw["title"]), str(raw.get("detail") or ""), str(raw["section"]))
        except (AttributeError, KeyError, TypeError):
            return None


def launch_problem_key(launch) -> str:
    """Names one start, so a notification's Try again acts only on the start it was about."""
    return f"launch:{launch.launched_at.isoformat()}"


def find_problems(config_error: str | None, feeds: list[FeedStatus], launch, *, card_failed: bool = False) -> list[Problem]:
    """Most urgent first: a launch that failed, a countdown card that can't open, a broken config, then
    calendars that stopped updating."""
    problems: list[Problem] = []
    if launch is not None and launch.did_not_open:
        problems.append(
            Problem(
                key=launch_problem_key(launch),
                title=f"Granola didn't open for {launch.title}"[:120],
                detail="A Granola update may have changed how notes open. Open Granola yourself to take notes in this meeting.",
                section="about",
                actions=(("Try again", "retry"), ("Open log folder", "logs")),
            )
        )
    if card_failed:
        problems.append(
            Problem(
                key="card",
                title="The countdown card couldn't open",
                detail="Meetings start notes without it, and you can't cancel them. The log folder says what went wrong.",
                section="about",
                actions=(("Open log folder", "logs"),),
            )
        )
    if config_error:
        problems.append(
            Problem(
                key="config",
                title="Settings couldn't be read",
                detail=f"{config_error}. Zoom joins still start notes. Calendar meetings won't until it's fixed.",
                section="calendars",
                actions=(("Open file", "config"), ("Dismiss", "dismiss")),
            )
        )
    for feed in feeds:
        if feed.alerting:
            problems.append(
                Problem(
                    key=f"feed:{feed.id}",
                    title=f"{feed.label()} isn't updating",
                    detail=feed.failure_detail(),
                    section="calendars",
                    actions=(("Open Settings", "settings"), ("Dismiss", "dismiss")),
                )
            )
    return problems


class AlertBook:
    """Each problem notifies once, and again only after it has gone away and come back."""

    def __init__(self) -> None:
        self.shown: set[str] = set()

    def fresh(self, problems: list[Problem]) -> list[Problem]:
        current = {problem.key for problem in problems}
        new = [problem for problem in problems if problem.key not in self.shown]
        self.shown = (self.shown & current) | {problem.key for problem in new}
        return new


@dataclass(frozen=True)
class TrayView:
    kind: str  # "watching" | "paused" | "attention"
    header: str
    paused: bool
    problem_section: str | None = None

    @property
    def tooltip(self) -> str:
        # Windows cuts tray tooltips at 127 characters.
        return f"Granola Autostart: {self.header}"[:127]


def next_meeting(events, now: datetime):
    upcoming = [event for event in events if event.start > now and not event.skipped]
    return min(upcoming, key=lambda event: event.start) if upcoming else None


def taking_notes(launch, now: datetime) -> tuple[str, datetime] | None:
    """(title, since) while Granola is open for the meeting the watcher started it for."""
    if launch is None or not launch.opened or launch.missing_since is not None:
        return None
    if now >= launch.expires_at:
        return None
    return launch.title, launch.launched_at


def when(moment: datetime, now: datetime) -> str:
    """10:00 today, Mon 09:30 within a week either way, else 3 Nov 09:30."""
    moment = moment.astimezone(now.tzinfo)
    if moment.date() == now.date():
        return f"{moment:%H:%M}"
    if abs(moment.date() - now.date()) < timedelta(days=7):
        return f"{moment:%a %H:%M}"
    return f"{moment.day} {moment:%b %H:%M}"


def end_of_today(now: datetime) -> datetime:
    """When "Pause for today" ends: the coming midnight."""
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def until_tomorrow(paused_until: datetime, now: datetime) -> bool:
    """A pause that ends at the coming midnight, which the tray and Settings call "until tomorrow"."""
    local = paused_until.astimezone(now.tzinfo)
    return (local.hour, local.minute) == (0, 0) and local.date() == now.date() + timedelta(days=1)


def tray_view(now: datetime, paused_until: datetime | None, problems: list[Problem], upcoming) -> TrayView:
    paused = paused_until is not None and now < paused_until
    if problems:
        return TrayView("attention", _shorten(problems[0].title), paused, problems[0].section)
    if paused:
        label = "Paused until tomorrow" if until_tomorrow(paused_until, now) else f"Paused until {when(paused_until, now)}"
        return TrayView("paused", label, True)
    if upcoming is not None:
        return TrayView("watching", _shorten(f"Next: {upcoming.summary}, {when(upcoming.start, now)}"), False)
    return TrayView("watching", "Watching for meetings", False)


def _shorten(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass
class LastStart:
    title: str
    at: datetime
    granola_opened: bool | None = None

    def to_json(self) -> dict:
        return {"title": self.title, "at": self.at.isoformat(), "granola_opened": self.granola_opened}

    @classmethod
    def from_json(cls, raw) -> LastStart | None:
        try:
            at = _moment(raw["at"])
            return cls(str(raw["title"]), at, raw.get("granola_opened")) if at else None
        except (AttributeError, KeyError, TypeError):
            return None


@dataclass
class Status:
    """status.json: what the watcher last knew. The watcher writes it each tick; Settings reads it."""

    version: str = ""
    paused_until: datetime | None = None
    next_meeting: tuple[str, datetime] | None = None
    taking_notes: tuple[str, datetime] | None = None
    feeds: list[FeedStatus] = field(default_factory=list)
    last_start: LastStart | None = None
    problems: list[Problem] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "next_meeting": _titled_json(self.next_meeting, "start"),
            "taking_notes": _titled_json(self.taking_notes, "since"),
            "feeds": [feed.to_json() for feed in self.feeds],
            "last_start": self.last_start.to_json() if self.last_start else None,
            "problems": [problem.to_json() for problem in self.problems],
        }

    @classmethod
    def from_json(cls, raw) -> Status:
        """Anything missing or unreadable comes back empty, as if the watcher hadn't said."""
        raw = raw if isinstance(raw, dict) else {}
        return cls(
            version=str(raw.get("version") or ""),
            paused_until=_moment(raw.get("paused_until")),
            next_meeting=_titled(raw.get("next_meeting"), "start"),
            taking_notes=_titled(raw.get("taking_notes"), "since"),
            feeds=[feed for feed in map(FeedStatus.from_json, _list(raw.get("feeds"))) if feed is not None],
            last_start=LastStart.from_json(raw.get("last_start")),
            problems=[problem for problem in map(Problem.from_json, _list(raw.get("problems"))) if problem is not None],
        )


def _titled_json(value: tuple[str, datetime] | None, moment_key: str) -> dict | None:
    return {"title": value[0], moment_key: value[1].isoformat()} if value else None


def _titled(raw, moment_key: str) -> tuple[str, datetime] | None:
    if not isinstance(raw, dict) or not raw.get("title"):
        return None
    moment = _moment(raw.get(moment_key))
    return (str(raw["title"]), moment) if moment else None


def _moment(raw) -> datetime | None:
    """A saved time. One without a UTC offset (a hand edit) is read as this PC's local time."""
    try:
        moment = datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None
    return moment.astimezone() if moment is not None and moment.tzinfo is None else moment


def _list(raw) -> list:
    return raw if isinstance(raw, list) else []


def write_status(path: Path, status: Status) -> None:
    write_json(path, status.to_json(), skip_unchanged=True)


def read_status(path: Path) -> Status:
    try:
        return Status.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return Status()
