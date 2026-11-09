"""Decide when to show the countdown. Pure logic, no Windows calls."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .calendar_feed import DEFAULT_MEETING_LENGTH, CalendarEvent, skip_word
from .status import when

CALL_HOLD = timedelta(hours=12)
CRASH_GRACE = timedelta(seconds=10)
ADHOC_CALL_LENGTH = timedelta(hours=4)
# What an ad hoc call is called when its windows show no meeting name, by provider.
ADHOC_CALL_TITLES = {"zoom": "Zoom meeting", "teams": "Teams call", "meet": "Google Meet call"}
ADHOC_CALL_TITLE = "Call"
# Longer than a calendar refresh, so an extension that arrives late still finds the decision.
HANDLED_GRACE = timedelta(minutes=15)
# How long after it last saw them a restart can take the calls under way for the same calls (WatcherState.call_ids):
# enough for an update to install, or for the supervisor to start the app again after a crash or two.
CALLS_KEPT_FOR = timedelta(minutes=5)
# The most meetings the countdown card lists (notice.MAX_CHOICES, which can't import this module, is the same number).
MAX_CHOICES = 4


@dataclass(frozen=True)
class Snapshot:
    present: bool
    paused: bool
    call_session_id: str | None  # the call under way, if any: its provider, a colon, then its own id
    call_provider: str | None  # "zoom", "teams" or "meet"
    call_age_seconds: float | None
    call_title: str | None  # the meeting's name from the call's windows, when they show one
    events: tuple[CalendarEvent, ...] | list[CalendarEvent]
    granola_running: bool


@dataclass(frozen=True)
class Choice:
    """One meeting the countdown card lists: what starting its notes takes, and what the card says about it."""

    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    start: datetime
    end: datetime
    guests: int
    calendar_name: str

    def line(self, now: datetime) -> str:
        """The muted line under its title on the card, such as "09:00–09:15 · 7 guests · you@shift.com"."""
        guests = "No guests" if self.guests == 0 else f"{self.guests} guest" + ("s" if self.guests > 1 else "")
        parts = [f"{when(self.start, now)}–{when(self.end, now)}", guests]
        if self.calendar_name:
            parts.append(self.calendar_name)
        return " · ".join(parts)


@dataclass(frozen=True)
class Prompt:
    kind: str
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    # An ad hoc call's own session: notes for it end with the call (see _consider_call).
    call_session_id: str | None = None
    cancel_keys: tuple[str, ...] | None = None
    # Other meetings already under way (or about to start), and the others the card lists. Notes for this one answer
    # them too, so a double booking or an overlap does not ask again seconds later; a cancel answers only the ones the
    # card listed (see apply_result).
    covers: tuple[str, ...] = ()
    # The call a calendar meeting was matched to (followed when the user rejoins it). Once no call
    # for it is running, Granola closing is the end of the meeting, so there is nothing to restart.
    call_session: str | None = None
    # When two or more meetings are equally candidates, each of them (up to MAX_CHOICES), the likeliest first, for the
    # card to list. The first is the meeting this prompt is for. Empty when there is nothing to choose.
    choices: tuple[Choice, ...] = ()

    def picked(self, index: int) -> Prompt:
        """The prompt for meeting ``index`` of ``choices``, once the card has ticked it (0 is this prompt's own).

        It keeps what belongs to the call, which is the call's session and, in ``keys``, its key, and takes that
        meeting's title, notes and expiry. Starting its notes answers the other meetings listed, and whatever this
        prompt already covers."""
        if not self.choices:
            return self
        choice = self.choices[index]
        others = (key for other in self.choices if other is not choice for key in other.keys)
        covers = dict.fromkeys(key for key in (*others, *self.covers) if key not in choice.keys)
        return dataclasses.replace(
            self,
            title=choice.title,
            payload=choice.payload,
            keys=choice.keys,
            expires_at=choice.expires_at,
            covers=tuple(covers),
            choices=(),
        )


@dataclass
class ActiveLaunch:
    title: str
    payload: dict
    keys: tuple[str, ...]
    expires_at: datetime
    seen_running: bool
    launched_at: datetime
    warned: bool = False
    call_session_id: str | None = None
    missing_since: datetime | None = None
    # Windows couldn't open Granola's link, so Granola running later says nothing about this note.
    failed: bool = False
    call_session: str | None = None  # see Prompt.call_session
    # Still being kept after its meeting's end, because the user is in the call that began before it (see _prune). Worked
    # out again every tick, so a saved one is only what the last tick found.
    overrunning: bool = False

    def follow(self, session: str) -> None:
        """Tie these notes to another call. It joins the keys, which also hold every call followed before it, so
        the notes can be followed back to any of them (see _follow_call) and a restart answers them all."""
        self.call_session = session
        if session not in self.keys:
            self.keys = (*self.keys, session)

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
    call_session_id: str | None = None
    call_needs_decision: bool = False
    active_launch: ActiveLaunch | None = None
    call_decided_session: str | None = None
    call_decided_at: datetime | None = None
    # Every call on the microphone that was under way at the last tick, the current one or not (CallWatch.under_way),
    # and the minute that was last seen, None with none under way. The watcher sets both each tick; they are only
    # saved, for call_ids after a restart. To the minute, so state.json changes once a minute in a call, not each tick.
    calls_under_way: list[str] = field(default_factory=list)
    calls_seen_at: datetime | None = None

    def call_ids(self, now: datetime) -> list[str]:
        """The ids of the calls under way when this state was saved, for CallWatch to know a call still under way after
        a restart (see CallWatch). Only those: a call that had ended, such as one answered earlier while another call
        went on, could pass its id to a new call. And only after a restart soon after (CALLS_KEPT_FOR): with the app
        stopped for longer, a call could have ended and another begun in the same app. A time seen that is later than
        now (the clock went back) says nothing about how long that was, and a state saved before calls_seen_at was says
        neither, so they offer none."""
        if self.calls_seen_at is None or not timedelta(0) <= now - self.calls_seen_at <= CALLS_KEPT_FOR:
            return []
        return list(dict.fromkeys(self.calls_under_way))


def evaluate(
    now: datetime,
    snap: Snapshot,
    state: WatcherState,
    *,
    lookahead: timedelta,
    fresh_call_seconds: float,
    skip_words: tuple[str, ...] = (),
    lead: timedelta = timedelta(0),
) -> Prompt | None:
    """What to ask now, if anything. ``lead`` is how early a calendar meeting counts as starting: its card comes up that
    long before the start, so the countdown ends about when the meeting begins."""
    _prune(state, snap, now)
    _extend_running_meetings(state, snap)
    _sync_granola(state, snap, now)
    joined = _consider_call(now, snap, state, fresh_call_seconds)
    _follow_call(now, snap, state, lookahead, joined)
    _answer_meetings_of_an_adhoc_call(now, state, snap, lookahead)
    if snap.paused:
        return None
    crash = _crash_prompt(now, snap, state, lead)
    if crash is not None:
        return crash
    call = _call_prompt(now, snap, state, lookahead, skip_words, lead)
    if call is not None:
        return call
    return _calendar_prompt(now, snap, state, lead, lookahead)


def apply_result(state: WatcherState, prompt: Prompt, accepted: bool, now: datetime) -> None:
    if accepted:
        recorded = prompt.keys + prompt.covers
    elif prompt.cancel_keys is not None:
        recorded = prompt.cancel_keys
    else:
        # Cancel answers every meeting the card listed, not only the one at the top of it.
        recorded = prompt.keys + tuple(key for choice in prompt.choices for key in choice.keys)
    for key in recorded:
        previous = state.handled.get(key)
        state.handled[key] = prompt.expires_at if previous is None else max(previous, prompt.expires_at)
    if prompt.kind == "call":
        state.call_needs_decision = False
    if prompt.kind == "call" and prompt.call_session_id:
        # An ad hoc answer means no calendar meeting was known yet; one that appears later is the same call.
        state.call_decided_session = prompt.call_session_id
        state.call_decided_at = now
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
        call_session_id=prompt.call_session_id,
        call_session=prompt.call_session,
    )


def _prune(state: WatcherState, snap: Snapshot, now: datetime) -> None:
    """Forget the answers, and the launch, of meetings that ended a grace period ago.

    A launch is kept past that while the user is still in a call that began before its meeting ended: the call it was
    started in, going on after the meeting has overrun. The notes are still being taken (``overrunning``), so the next
    meeting's card keeps waiting (see _waits_for_the_call) and the status still says so. It is forgotten as usual once
    that call ends, or when the only call is one that began after the meeting ended, such as a rejoin.
    """
    state.handled = {key: until for key, until in state.handled.items() if until + HANDLED_GRACE > now}
    launch = state.active_launch
    if launch is None:
        return
    launch.overrunning = now >= launch.expires_at and _call_began_before(snap, now, launch.expires_at)
    if not launch.overrunning and launch.expires_at + HANDLED_GRACE <= now:
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


def _consider_call(now: datetime, snap: Snapshot, state: WatcherState, fresh_call_seconds: float) -> bool:
    """Note the call under way. True when it became the current one just now as a join: it is new, or it replaced another
    (paused or not, and whether or not it gets a card)."""
    launch = state.active_launch
    if launch is not None and launch.call_session_id and launch.call_session_id != snap.call_session_id:
        state.active_launch = None
    if not snap.call_session_id:
        state.call_session_id = None
        state.call_needs_decision = False
        state.call_decided_session = None
        state.call_decided_at = None
        return False
    if snap.call_session_id == state.call_session_id:
        return False
    age = snap.call_age_seconds
    previous = state.call_session_id
    state.call_session_id = snap.call_session_id
    # One call replacing another counts as a join whatever its age when it can't be an older call coming back
    # as the newest ends: its age isn't known, or it is in the same place as the one it replaces, where only one
    # call can run (Zoom moving to another meeting).
    replaced = previous is not None and (age is None or _place(previous) == _place(snap.call_session_id))
    fresh = replaced or (age is not None and age <= fresh_call_seconds)
    if snap.paused or not fresh:
        state.call_needs_decision = False
        state.handled[snap.call_session_id] = now + CALL_HOLD
        return fresh
    state.call_needs_decision = True
    return True


def _place(session_id: str) -> str:
    """Where a call runs, which holds one call at a time: its provider, and for a call in a browser its browser too, as
    "meet:chrome" or "teams:shift", because a call in Chrome and another in Edge can be under way together. A browser
    call is Meet, or a Teams id with a browser in it ("teams:shift:<start>", where the desktop app's is "teams:<start>").
    That is by provider, not by the parts of the id: Zoom's ("zoom:<pid>:<start>") has three too, and is just "zoom"."""
    provider, _, rest = session_id.partition(":")
    browser, colon, _ = rest.partition(":")
    in_browser = provider == "meet" or (provider == "teams" and bool(colon))
    return f"{provider}:{browser}" if in_browser else provider


def _follow_call(now: datetime, snap: Snapshot, state: WatcherState, lookahead: timedelta, joined: bool) -> None:
    """Notes started from a calendar meeting's call follow the user to another call during that meeting: a rejoin,
    or a call on another provider and back. Checked every tick, because the older call that comes back isn't a
    join and never gets as far as a card. They follow only to a call that has just been joined (``joined``), or one
    they followed before (it is among their keys). An older call that was under way all along, such as an ad hoc
    Teams call when the meeting's Zoom was joined, isn't the meeting's: following it when Zoom ends would bring a
    restart card for a meeting whose call is over."""
    launch = state.active_launch
    if launch is None or not launch.call_session or not snap.call_session_id or snap.call_session_id == launch.call_session:
        return
    if not (joined or snap.call_session_id in launch.keys):
        return
    event = _match_event([event for event in snap.events if not event.skipped], now, lookahead, state.handled)
    if event is not None and event.key() in launch.keys:
        launch.follow(snap.call_session_id)


def _answer_meetings_of_an_adhoc_call(now: datetime, state: WatcherState, snap: Snapshot, lookahead: timedelta) -> None:
    """An ad hoc call's answer also answers the meetings that call could have been for, when the
    calendar only loads them during the call. Each is recorded as answered once it is under way in
    that call, so the answer (a start or a Cancel) outlasts the call instead of a card asking about it
    once the call ends. A meeting that only starts after the call keeps its own card."""
    if state.call_decided_at is None or not snap.call_session_id or state.call_decided_session != snap.call_session_id:
        return
    covered_until = min(now, state.call_decided_at + lookahead)
    for event in snap.events:
        if not event.skipped and event.start <= covered_until:
            previous = state.handled.get(event.key())
            state.handled[event.key()] = event.end if previous is None else max(previous, event.end)


def _call_prompt(
    now: datetime, snap: Snapshot, state: WatcherState, lookahead: timedelta, skip_words: tuple[str, ...], lead: timedelta
) -> Prompt | None:
    if not state.call_needs_decision or not snap.present or not snap.call_session_id:
        return None
    call_key = snap.call_session_id
    if call_key in state.handled:
        state.call_needs_decision = False
        return None
    meetings = [event for event in snap.events if not event.skipped]
    group = _match_group(meetings, now, lookahead, state.handled)
    event = group[0] if group else None
    if event is None:
        # A call during a meeting whose title has a skip word is that meeting, so it is skipped too.
        skipped = _match_event([event for event in snap.events if event.skipped], now, lookahead, state.handled)
        if skipped is not None:
            state.handled[call_key] = max(skipped.end, now + CALL_HOLD)
            state.call_needs_decision = False
            return None
        # So is a call with no meeting at all whose own title has one.
        if snap.call_title and skip_word(snap.call_title, skip_words) is not None:
            state.handled[call_key] = now + CALL_HOLD
            state.call_needs_decision = False
            # A meeting that loads later and is under way in this call is covered, as an ad hoc answer covers it.
            state.call_decided_session = call_key
            state.call_decided_at = now
            return None
    if event is not None and event.key() in state.handled:
        state.handled[call_key] = max(event.end, now + CALL_HOLD)
        state.call_needs_decision = False
        return None
    if event is not None:
        upcoming = event.start > now
        # The card lists the meetings the call could be for that nobody has answered: the ones under way when it is for
        # one of them, otherwise the ones about to start. Notes for any of them answer all of them.
        candidates = [other for other in group if other.key() not in state.handled]
        return Prompt(
            kind="call",
            title=event.summary,
            payload=event.payload(),
            keys=(call_key, event.key()),
            expires_at=event.end,
            # Cancelling may mean "I'm still in the current meeting", so the next one keeps its own prompt.
            cancel_keys=(call_key,) if upcoming else None,
            covers=_covers([*_ongoing(snap.events, now, state.handled, lead), *candidates], event),
            call_session=snap.call_session_id,
            choices=_choices(candidates, call_key),
        )
    title = snap.call_title or ADHOC_CALL_TITLES.get(snap.call_provider, ADHOC_CALL_TITLE)
    # A call with no calendar meeting goes to Granola as an hour-long meeting of its own.
    adhoc = CalendarEvent(snap.call_session_id.replace(":", "-"), title, now, now + DEFAULT_MEETING_LENGTH)
    return Prompt(
        kind="call",
        title=title,
        payload=adhoc.payload(),
        keys=(call_key,),
        expires_at=now + ADHOC_CALL_LENGTH,
        call_session_id=snap.call_session_id,
    )


def _calendar_prompt(now: datetime, snap: Snapshot, state: WatcherState, lead: timedelta, lookahead: timedelta) -> Prompt | None:
    if not snap.present:
        return None
    # A meeting that waits for the call to end (see _waits_for_the_call) is no candidate: it isn't listed, and starting
    # another's notes doesn't answer it either, so its card comes up by itself afterwards.
    ongoing = [
        event
        for event in _ongoing(snap.events, now, state.handled, lead)
        if not _waits_for_the_call(event, now, snap, state, lead, lookahead)
    ]
    if not ongoing:
        return None
    ranked = _ranked(ongoing)
    event = ranked[0]
    return Prompt(
        kind="calendar",
        title=event.summary,
        payload=event.payload(),
        keys=(event.key(),),
        expires_at=event.end,
        covers=_covers(ongoing, event),
        choices=_choices(ranked),
    )


def _waits_for_the_call(
    event: CalendarEvent, now: datetime, snap: Snapshot, state: WatcherState, lead: timedelta, lookahead: timedelta
) -> bool:
    """Whether a calendar meeting's card holds back, because the user is in a call that is likelier another meeting's.

    Notes for a meeting that runs long must not switch to the next one, whose card came up at its start, while the user
    is still in the first one's call. The app can't tell which meeting a call is for, so this goes by timing. The card
    waits while all of these hold:

    - A call is under way.
    - It began before this meeting's card time (its start less ``lead``) less the call ``lookahead``, so it can't be
      this meeting's own early join. A call of unknown age counts as having begun earlier.
    - Notes are running for another meeting, or an ad hoc call: there is a launch, and this meeting isn't one of its keys.

    Waiting records nothing, so the card comes up by itself once the call ends, if the meeting is still on. Joining this
    meeting's own call meanwhile gives a call card, as always (see _call_prompt).

    Consequences. Back-to-back meetings on the same Zoom link, such as a personal room: the second one's card waits until
    the user leaves, and its discussion lands in the first one's notes. An ad hoc call with no notes running, because
    its card was cancelled: there is no launch, so a meeting that starts during it still gets its card, as before.
    The wait lasts as long as the launch does, and a launch outlives its meeting while the call it was started in goes
    on (see _prune), so a meeting that runs long keeps its notes however far it overruns.
    """
    launch = state.active_launch
    if launch is None or event.key() in launch.keys:
        return False
    return _call_began_before(snap, now, event.start - lead - lookahead)


def _call_began_before(snap: Snapshot, now: datetime, moment: datetime) -> bool:
    """A call is under way and began before ``moment``. A call of unknown age counts as having begun earlier."""
    if not snap.call_session_id:
        return False
    return snap.call_age_seconds is None or now - timedelta(seconds=snap.call_age_seconds) < moment


def _ongoing(events, now: datetime, handled, lead: timedelta = timedelta(0)) -> list[CalendarEvent]:
    """Meetings under way now, or starting within ``lead``, that nobody has answered yet: each would get a calendar card
    of its own. Skipped ones never count."""
    return [event for event in events if not event.skipped and event.start - lead <= now < event.end and event.key() not in handled]


def _ranked(events: list[CalendarEvent], *, earliest: bool = False) -> list[CalendarEvent]:
    """Meetings that are equally candidates, the one to start notes for first.

    One with guests beats one without. Then the one on the calendar higher in the Calendars list wins, then the later
    start, so one that is about to begin comes before one already under way (with ``earliest``, for meetings that haven't
    started, the next to begin comes first). Meetings that tie on all three keep the order they came in.
    """
    sign = 1 if earliest else -1
    return sorted(events, key=lambda event: (event.guests == 0, event.calendar_rank, sign * event.start.timestamp()))


def _choices(ranked: list[CalendarEvent], *call_keys: str) -> tuple[Choice, ...]:
    """The card's list for meetings ranked by ``_ranked``: nothing when there is one, since there is nothing to choose.
    Each one's keys are what starting its notes records, ``call_keys`` first when it is for a call."""
    if len(ranked) < 2:
        return ()
    return tuple(
        Choice(event.summary, event.payload(), (*call_keys, event.key()), event.end, event.start, event.end, event.guests, event.calendar_name)
        for event in ranked[:MAX_CHOICES]
    )


def _covers(listed: list[CalendarEvent], chosen: CalendarEvent) -> tuple[str, ...]:
    """The other meetings, under way or listed on the card, that starting notes for ``chosen`` also answers."""
    return tuple(dict.fromkeys(event.key() for event in listed if event.key() != chosen.key()))


def _crash_prompt(now: datetime, snap: Snapshot, state: WatcherState, lead: timedelta) -> Prompt | None:
    launch = state.active_launch
    # A link Windows couldn't open has no notes to restart; Try again on its notification is the way back.
    if launch is None or not launch.opened or now >= launch.expires_at:
        return None
    if snap.granola_running or launch.missing_since is None:
        return None
    # Notes started from a call: once that meeting's call is over, Granola closing ends the meeting too.
    if launch.call_session and snap.call_session_id != launch.call_session:
        return None
    # Granola was seen after the launch, so going missing comes later still: one grace covers both.
    if now < launch.missing_since + CRASH_GRACE:
        return None
    if not snap.present:
        return None
    # A meeting that began after these notes started, or is about to, gets its own card, which reopens Granola for it.
    if any(event.start > launch.launched_at for event in _ongoing(snap.events, now, state.handled, lead)):
        return None
    return Prompt(
        kind="crash",
        title=launch.title,
        payload=launch.payload,
        keys=launch.keys,
        expires_at=launch.expires_at,
        call_session_id=launch.call_session_id,
        call_session=launch.call_session,
    )


def _match_event(events, now: datetime, lookahead: timedelta, handled=()) -> CalendarEvent | None:
    """The meeting a call is likeliest for (see ``_match_group``)."""
    group = _match_group(events, now, lookahead, handled)
    return group[0] if group else None


def _match_group(events, now: datetime, lookahead: timedelta, handled=()) -> list[CalendarEvent]:
    """The meetings a call could be for, the likeliest first (see ``_ranked``): the ones under way, or the ones about to
    start when the call is an early join.

    Joining early for the next meeting beats an ongoing one that is about to end.

    An ongoing meeting that was already decided wins, because a rejoin near its end
    is far more likely than an early join.
    """
    horizon = now + lookahead
    ongoing = [event for event in events if event.start <= now < event.end]
    soon = [event for event in events if now < event.start <= horizon and event.end > now]
    early_join = all(event.end <= horizon and event.key() not in handled for event in ongoing)
    if soon and early_join:
        return _ranked(soon, earliest=True)
    return _ranked(ongoing)
