"""Decide when to show the countdown. Pure logic, no Windows calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from calendar_feed import CalendarEvent
from granola_launch import adhoc_payload, meeting_title

ZOOM_HOLD = timedelta(hours=12)
CRASH_GRACE = timedelta(seconds=10)


@dataclass(frozen=True)
class Snapshot:
    present: bool
    paused: bool
    in_zoom: bool
    zoom_session_id: str | None
    zoom_age_seconds: float | None
    zoom_title: str | None
    events: tuple[CalendarEvent, ...] | list[CalendarEvent]
    granola_running: bool


@dataclass(frozen=True)
class Prompt:
    kind: str
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime


@dataclass
class ActiveLaunch:
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    seen_running: bool
    launched_at: datetime
    warned: bool = False


@dataclass
class WatcherState:
    handled: dict[str, datetime] = field(default_factory=dict)
    zoom_session_id: str | None = None
    zoom_needs_decision: bool = False
    active_launch: ActiveLaunch | None = None


def evaluate(
    now: datetime,
    snap: Snapshot,
    state: WatcherState,
    *,
    lookahead: timedelta,
    fresh_zoom_seconds: float,
) -> Prompt | None:
    _prune(state, now)
    _sync_granola(state, snap)
    _consider_zoom(now, snap, state, fresh_zoom_seconds)
    if snap.paused:
        return None
    crash = _crash_prompt(now, snap, state)
    if crash is not None and snap.present:
        return crash
    zoom = _zoom_prompt(now, snap, state, lookahead)
    if zoom is not None:
        return zoom
    return _calendar_prompt(now, snap, state)


def apply_result(state: WatcherState, prompt: Prompt, accepted: bool, now: datetime) -> None:
    for key in prompt.keys:
        previous = state.handled.get(key)
        state.handled[key] = prompt.expires_at if previous is None else max(previous, prompt.expires_at)
    if prompt.kind == "zoom":
        state.zoom_needs_decision = False
    if not accepted:
        state.active_launch = None
        return
    state.active_launch = ActiveLaunch(
        title=prompt.title,
        payload=prompt.payload,
        keys=prompt.keys,
        expires_at=prompt.expires_at,
        seen_running=False,
        launched_at=now,
    )


def _prune(state: WatcherState, now: datetime) -> None:
    state.handled = {key: until for key, until in state.handled.items() if until > now}
    if state.active_launch is not None and state.active_launch.expires_at <= now:
        state.active_launch = None


def _sync_granola(state: WatcherState, snap: Snapshot) -> None:
    if state.active_launch is not None and snap.granola_running:
        state.active_launch.seen_running = True


def _consider_zoom(now: datetime, snap: Snapshot, state: WatcherState, fresh_zoom_seconds: float) -> None:
    if not snap.in_zoom or not snap.zoom_session_id:
        state.zoom_session_id = None
        state.zoom_needs_decision = False
        return
    if snap.zoom_session_id == state.zoom_session_id:
        return
    age = snap.zoom_age_seconds
    transitioning = state.zoom_session_id is not None
    state.zoom_session_id = snap.zoom_session_id
    zoom_key = f"zoom:{snap.zoom_session_id}"
    fresh = transitioning or (age is not None and age <= fresh_zoom_seconds)
    if snap.paused or not fresh:
        state.zoom_needs_decision = False
        state.handled[zoom_key] = now + ZOOM_HOLD
        return
    state.zoom_needs_decision = True


def _zoom_prompt(now: datetime, snap: Snapshot, state: WatcherState, lookahead: timedelta) -> Prompt | None:
    if not state.zoom_needs_decision or not snap.present or not snap.zoom_session_id:
        return None
    zoom_key = f"zoom:{snap.zoom_session_id}"
    if zoom_key in state.handled:
        state.zoom_needs_decision = False
        return None
    event = _match_event(snap.events, now, lookahead)
    if event is not None and event.key() in state.handled:
        state.handled[zoom_key] = max(event.end, now + ZOOM_HOLD)
        state.zoom_needs_decision = False
        return None
    if event is not None:
        return Prompt(
            kind="zoom",
            title=event.summary or meeting_title(snap.zoom_title),
            payload=event.payload(),
            keys=(zoom_key, event.key()),
            expires_at=event.end,
        )
    title = meeting_title(snap.zoom_title)
    return Prompt(
        kind="zoom",
        title=title,
        payload=adhoc_payload(title, snap.zoom_session_id, now),
        keys=(zoom_key,),
        expires_at=now + timedelta(hours=4),
    )


def _calendar_prompt(now: datetime, snap: Snapshot, state: WatcherState) -> Prompt | None:
    if not snap.present:
        return None
    ongoing = [event for event in snap.events if event.start <= now < event.end and event.key() not in state.handled]
    if not ongoing:
        return None
    event = max(ongoing, key=lambda item: item.start)
    return Prompt(
        kind="calendar",
        title=event.summary,
        payload=event.payload(),
        keys=(event.key(),),
        expires_at=event.end,
    )


def _crash_prompt(now: datetime, snap: Snapshot, state: WatcherState) -> Prompt | None:
    launch = state.active_launch
    if launch is None or now >= launch.expires_at:
        return None
    if snap.granola_running or not launch.seen_running:
        return None
    if now < launch.launched_at + CRASH_GRACE:
        return None
    if not snap.present:
        return None
    return Prompt(
        kind="crash",
        title=launch.title,
        payload=launch.payload,
        keys=launch.keys,
        expires_at=launch.expires_at,
    )


def _match_event(events, now: datetime, lookahead: timedelta) -> CalendarEvent | None:
    ongoing = [event for event in events if event.start <= now < event.end]
    if ongoing:
        return max(ongoing, key=lambda event: event.start)
    soon = [event for event in events if now < event.start <= now + lookahead and event.end > now]
    if soon:
        return min(soon, key=lambda event: event.start)
    return None
