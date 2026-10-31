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


@dataclass(frozen=True)
class Problem:
    key: str
    title: str
    detail: str
    section: str  # the Settings section that fixes it
    actions: tuple[tuple[str, str], ...] = ()  # (button label, action id) for the notification


def find_problems(config_error: str | None, feeds: list[FeedStatus], launch) -> list[Problem]:
    """Most urgent first: a launch that failed, then a broken config, then calendars that stopped updating."""
    problems: list[Problem] = []
    if launch is not None and launch.warned and not launch.seen_running:
        problems.append(
            Problem(
                key=f"launch:{launch.launched_at.isoformat()}",
                title=f"Granola didn't open for {launch.title}"[:120],
                detail="A Granola update may have changed how notes open. Open Granola yourself to take notes in this meeting.",
                section="about",
                actions=(("Try again", "retry"), ("Open log folder", "logs")),
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
        if feed.failures >= FAILURES_BEFORE_ALERT:
            reason = f" ({feed.last_error})" if feed.last_error else ""
            problems.append(
                Problem(
                    key=f"feed:{feed.id}",
                    title=f"{feed.label()} isn't updating",
                    detail=f"The last {feed.failures} downloads failed{reason}. Meetings on it won't start notes until it's fixed.",
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
    upcoming = [event for event in events if event.start > now and not getattr(event, "skipped", False)]
    return min(upcoming, key=lambda event: event.start) if upcoming else None


def taking_notes(launch, now: datetime) -> tuple[str, datetime] | None:
    """(title, since) while Granola is open for the meeting the watcher started it for."""
    if launch is None or not launch.seen_running or launch.missing_since is not None or now >= launch.expires_at:
        return None
    return launch.title, launch.launched_at


def when(moment: datetime, now: datetime) -> str:
    """10:00 today, Mon 09:30 within a week, else 3 Nov 09:30."""
    moment = moment.astimezone(now.tzinfo)
    if moment.date() == now.date():
        return f"{moment:%H:%M}"
    if moment.date() - now.date() < timedelta(days=7):
        return f"{moment:%a %H:%M}"
    return f"{moment.day} {moment:%b %H:%M}"


def tray_view(now: datetime, paused_until: datetime | None, problems: list[Problem], upcoming) -> TrayView:
    paused = paused_until is not None and now < paused_until
    if problems:
        return TrayView("attention", _shorten(problems[0].title), paused, problems[0].section)
    if paused:
        local = paused_until.astimezone(now.tzinfo)
        midnight = local.hour == 0 and local.minute == 0 and local.date() == now.date() + timedelta(days=1)
        return TrayView("paused", "Paused until tomorrow" if midnight else f"Paused until {when(local, now)}", True)
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
            return cls(str(raw["title"]), datetime.fromisoformat(raw["at"]), raw.get("granola_opened"))
        except (KeyError, TypeError, ValueError):
            return None


@dataclass
class Status:
    version: str
    paused_until: datetime | None = None
    next_meeting: tuple[str, datetime] | None = None
    taking_notes: tuple[str, datetime] | None = None
    feeds: list[FeedStatus] = field(default_factory=list)
    last_start: LastStart | None = None
    problems: list[Problem] = field(default_factory=list)
    config_error: str | None = None

    def to_json(self) -> dict:
        return {
            "version": self.version,
            "paused_until": self.paused_until.isoformat() if self.paused_until else None,
            "next_meeting": {"title": self.next_meeting[0], "start": self.next_meeting[1].isoformat()}
            if self.next_meeting
            else None,
            "taking_notes": {"title": self.taking_notes[0], "since": self.taking_notes[1].isoformat()}
            if self.taking_notes
            else None,
            "feeds": [feed.to_json() for feed in self.feeds],
            "last_start": self.last_start.to_json() if self.last_start else None,
            "problems": [{"key": p.key, "title": p.title, "detail": p.detail, "section": p.section} for p in self.problems],
            "config_error": self.config_error,
        }


_last_written: str | None = None


def write_status(path: Path, status: Status) -> None:
    global _last_written
    text = json.dumps(status.to_json(), indent=2) + "\n"
    if _last_written == f"{path}\n{text}" and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    _last_written = f"{path}\n{text}"


def read_status(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}
