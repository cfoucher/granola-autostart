"""Decide when to show the countdown. Pure logic, no Windows calls."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .calendar_feed import DEFAULT_MEETING_LENGTH, CalendarEvent

ZOOM_HOLD = timedelta(hours=12)
CRASH_GRACE = timedelta(seconds=10)
ADHOC_ZOOM_LENGTH = timedelta(hours=4)
ADHOC_ZOOM_TITLE = "Zoom meeting"
# Longer than a calendar refresh, so an extension that arrives late still finds the decision.
HANDLED_GRACE = timedelta(minutes=15)


@dataclass(frozen=True)
class Snapshot:
    present: bool
    paused: bool
    zoom_session_id: str | None  # the Zoom call under way, if any
    zoom_age_seconds: float | None
    zoom_title: str | None  # the meeting's name from Zoom's windows, when they show one
    events: tuple[CalendarEvent, ...] | list[CalendarEvent]
    granola_running: bool


@dataclass(frozen=True)
class Prompt:
    kind: str
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    # An ad hoc call's own session: notes for it end with the call (see _consider_zoom).
    zoom_session_id: str | None = None
    cancel_keys: tuple[str, ...] | None = None
    # Other meetings already under way. Notes for this one answer them too, so a double booking or
    # an overlap does not ask again seconds later; a cancel leaves them free to ask.
    covers: tuple[str, ...] = ()
    # The Zoom call a calendar meeting was matched to (followed when the user rejoins it). Once no call
    # for it is running, Granola closing is the end of the meeting, so there is nothing to restart.
    call_session: str | None = None


@dataclass
class ActiveLaunch:
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    seen_running: bool
    launched_at: datetime
    warned: bool = False
    zoom_session_id: str | None = None
    missing_since: datetime | None = None
    # Windows couldn't open Granola's link, so Granola running later says nothing about this note.
    failed: bool = False
    call_session: str | None = None  # see Prompt.call_session

    @property
    def opened(self) -> bool:
        """Granola has been seen since this start, and the link itself didn't fail."""
        return self.seen_running and not self.failed

    @property
    def did_not_open(self) -> bool:
        """The link failed, or Granola still wasn't running when the launch check gave up."""
        return self.failed or (self.warned and not self.seen_running)


@dataclass
class WatcherState:
    handled: dict[str, datetime] = field(default_factory=dict)
    zoom_session_id: str | None = None
    zoom_needs_decision: bool = False
    active_launch: ActiveLaunch | None = None
    zoom_decided_session: str | None = None
    zoom_decided_at: datetime | None = None


def evaluate(
    now: datetime,
    snap: Snapshot,
    state: WatcherState,
    *,
    lookahead: timedelta,
    fresh_zoom_seconds: float,
) -> Prompt | None:
    _prune(state, now)
    _extend_running_meetings(state, snap)
    _sync_granola(state, snap, now)
    _consider_zoom(now, snap, state, fresh_zoom_seconds)
    _answer_meetings_of_an_adhoc_call(now, state, snap, lookahead)
    if snap.paused:
        return None
    crash = _crash_prompt(now, snap, state)
    if crash is not None:
        return crash
    zoom = _zoom_prompt(now, snap, state, lookahead)
    if zoom is not None:
        return zoom
    return _calendar_prompt(now, snap, state)


def apply_result(state: WatcherState, prompt: Prompt, accepted: bool, now: datetime) -> None:
    if accepted:
        recorded = prompt.keys + prompt.covers
    else:
        recorded = prompt.keys if prompt.cancel_keys is None else prompt.cancel_keys
    for key in recorded:
        previous = state.handled.get(key)
        state.handled[key] = prompt.expires_at if previous is None else max(previous, prompt.expires_at)
    if prompt.kind == "zoom":
        state.zoom_needs_decision = False
    if prompt.kind == "zoom" and prompt.zoom_session_id:
        # An ad hoc answer means no calendar meeting was known yet; one that appears later is the same call.
        state.zoom_decided_session = prompt.zoom_session_id
        state.zoom_decided_at = now
    if not accepted:
        launch = state.active_launch
        if launch is not None and (prompt.kind == "crash" or set(prompt.keys) & set(launch.keys)):
            state.active_launch = None
        return
    state.active_launch = ActiveLaunch(
        title=prompt.title,
        payload=prompt.payload,
        keys=prompt.keys,
        expires_at=prompt.expires_at,
        seen_running=False,
        launched_at=now,
        zoom_session_id=prompt.zoom_session_id,
        call_session=prompt.call_session,
    )


def _prune(state: WatcherState, now: datetime) -> None:
    state.handled = {key: until for key, until in state.handled.items() if until + HANDLED_GRACE > now}
    if state.active_launch is not None and state.active_launch.expires_at + HANDLED_GRACE <= now:
        state.active_launch = None


def _extend_running_meetings(state: WatcherState, snap: Snapshot) -> None:
    """A meeting moved or extended in the calendar keeps its decision until the new end."""
    launch = state.active_launch
    for event in snap.events:
        key = event.key()
        until = state.handled.get(key)
        if until is not None and until < event.end:
            state.handled[key] = event.end
        if launch is not None and key in launch.keys and launch.expires_at < event.end:
            launch.expires_at = event.end


def _sync_granola(state: WatcherState, snap: Snapshot, now: datetime) -> None:
    launch = state.active_launch
    if launch is None:
        return
    if snap.granola_running:
        launch.seen_running = True
        launch.missing_since = None
    elif launch.seen_running and launch.missing_since is None:
        launch.missing_since = now


def _consider_zoom(now: datetime, snap: Snapshot, state: WatcherState, fresh_zoom_seconds: float) -> None:
    launch = state.active_launch
    if launch is not None and launch.zoom_session_id and launch.zoom_session_id != snap.zoom_session_id:
        state.active_launch = None
    if not snap.zoom_session_id:
        state.zoom_session_id = None
        state.zoom_needs_decision = False
        state.zoom_decided_session = None
        state.zoom_decided_at = None
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


def _answer_meetings_of_an_adhoc_call(now: datetime, state: WatcherState, snap: Snapshot, lookahead: timedelta) -> None:
    """An ad hoc Zoom answer also answers the meetings that call could have been for, when the
    calendar only loads them during the call. Each is recorded as answered once it is under way in
    that call, so the answer (a start or a Cancel) outlasts the call instead of a card asking about it
    once the call ends. A meeting that only starts after the call keeps its own card."""
    if state.zoom_decided_at is None or not snap.zoom_session_id or state.zoom_decided_session != snap.zoom_session_id:
        return
    covered_until = min(now, state.zoom_decided_at + lookahead)
    for event in snap.events:
        if not event.skipped and event.start <= covered_until:
            previous = state.handled.get(event.key())
            state.handled[event.key()] = event.end if previous is None else max(previous, event.end)


def _zoom_prompt(now: datetime, snap: Snapshot, state: WatcherState, lookahead: timedelta) -> Prompt | None:
    if not state.zoom_needs_decision or not snap.present or not snap.zoom_session_id:
        return None
    zoom_key = f"zoom:{snap.zoom_session_id}"
    if zoom_key in state.handled:
        state.zoom_needs_decision = False
        return None
    meetings = [event for event in snap.events if not event.skipped]
    event = _match_event(meetings, now, lookahead, state.handled)
    if event is None:
        # A call during a meeting whose title has a skip word is that meeting, so it is skipped too.
        skipped = _match_event([event for event in snap.events if event.skipped], now, lookahead, state.handled)
        if skipped is not None:
            state.handled[zoom_key] = max(skipped.end, now + ZOOM_HOLD)
            state.zoom_needs_decision = False
            return None
    if event is not None and event.key() in state.handled:
        launch = state.active_launch
        if launch is not None and launch.call_session and event.key() in launch.keys:
            launch.call_session = snap.zoom_session_id  # the same meeting's call, rejoined
        state.handled[zoom_key] = max(event.end, now + ZOOM_HOLD)
        state.zoom_needs_decision = False
        return None
    if event is not None:
        upcoming = event.start > now
        return Prompt(
            kind="zoom",
            title=event.summary,
            payload=event.payload(),
            keys=(zoom_key, event.key()),
            expires_at=event.end,
            # Cancelling may mean "I'm still in the current meeting", so the next one keeps its own prompt.
            cancel_keys=(zoom_key,) if upcoming else None,
            covers=_covers(_ongoing(snap.events, now, state.handled), event),
            call_session=snap.zoom_session_id,
        )
    title = snap.zoom_title or ADHOC_ZOOM_TITLE
    # A call with no calendar meeting goes to Granola as an hour-long meeting of its own.
    adhoc = CalendarEvent(f"zoom-{snap.zoom_session_id.replace(':', '-')}", title, now, now + DEFAULT_MEETING_LENGTH)
    return Prompt(
        kind="zoom",
        title=title,
        payload=adhoc.payload(),
        keys=(zoom_key,),
        expires_at=now + ADHOC_ZOOM_LENGTH,
        zoom_session_id=snap.zoom_session_id,
    )


def _calendar_prompt(now: datetime, snap: Snapshot, state: WatcherState) -> Prompt | None:
    if not snap.present:
        return None
    ongoing = _ongoing(snap.events, now, state.handled)
    if not ongoing:
        return None
    event = max(ongoing, key=lambda item: item.start)
    return Prompt(
        kind="calendar",
        title=event.summary,
        payload=event.payload(),
        keys=(event.key(),),
        expires_at=event.end,
        covers=_covers(ongoing, event),
    )


def _ongoing(events, now: datetime, handled) -> list[CalendarEvent]:
    """Meetings under way now that nobody has answered yet: each would get a calendar card of its own.
    Skipped ones never count."""
    return [event for event in events if not event.skipped and event.start <= now < event.end and event.key() not in handled]


def _covers(ongoing: list[CalendarEvent], chosen: CalendarEvent) -> tuple[str, ...]:
    """The other meetings under way that starting notes for ``chosen`` also answers."""
    return tuple(event.key() for event in ongoing if event.key() != chosen.key())


def _crash_prompt(now: datetime, snap: Snapshot, state: WatcherState) -> Prompt | None:
    launch = state.active_launch
    # A link Windows couldn't open has no notes to restart; Try again on its notification is the way back.
    if launch is None or not launch.opened or now >= launch.expires_at:
        return None
    if snap.granola_running or launch.missing_since is None:
        return None
    # Notes started from a Zoom call: once that meeting's call is over, Granola closing ends the meeting too.
    if launch.call_session and snap.zoom_session_id != launch.call_session:
        return None
    # Granola was seen after the launch, so going missing comes later still: one grace covers both.
    if now < launch.missing_since + CRASH_GRACE:
        return None
    if not snap.present:
        return None
    # A meeting that began after these notes started gets its own card, which reopens Granola for it.
    if any(event.start > launch.launched_at for event in _ongoing(snap.events, now, state.handled)):
        return None
    return Prompt(
        kind="crash",
        title=launch.title,
        payload=launch.payload,
        keys=launch.keys,
        expires_at=launch.expires_at,
        zoom_session_id=launch.zoom_session_id,
        call_session=launch.call_session,
    )


def _match_event(events, now: datetime, lookahead: timedelta, handled=()) -> CalendarEvent | None:
    """Joining early for the next meeting beats an ongoing one that is about to end.

    An ongoing meeting that was already decided wins, because a Zoom rejoin near its end
    is far more likely than an early join.
    """
    horizon = now + lookahead
    ongoing = [event for event in events if event.start <= now < event.end]
    soon = [event for event in events if now < event.start <= horizon and event.end > now]
    early_join = all(event.end <= horizon and event.key() not in handled for event in ongoing)
    if soon and early_join:
        return min(soon, key=lambda event: event.start)
    if ongoing:
        return max(ongoing, key=lambda event: event.start)
    return None
