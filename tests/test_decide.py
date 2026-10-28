from datetime import datetime, timedelta, timezone

from calendar_feed import CalendarEvent
from decide import ActiveLaunch, Snapshot, WatcherState, apply_result, evaluate

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
LOOKAHEAD = timedelta(minutes=10)


def event(summary="Design review", start=NOW, minutes=30, event_id="abc123"):
    return CalendarEvent(id=event_id, summary=summary, start=start, end=start + timedelta(minutes=minutes))


def snap(**overrides) -> Snapshot:
    base = dict(
        present=True,
        paused=False,
        in_zoom=False,
        zoom_session_id=None,
        zoom_age_seconds=None,
        zoom_title=None,
        events=(),
        granola_running=False,
    )
    base.update(overrides)
    return Snapshot(**base)


def decide(snapshot, state=None, now=NOW):
    state = state or WatcherState()
    prompt = evaluate(now, snapshot, state, lookahead=LOOKAHEAD, fresh_zoom_seconds=120)
    return prompt, state


def test_calendar_waits_until_the_user_is_present():
    meeting = event()
    prompt, state = decide(snap(present=False, events=(meeting,)))
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,)), state)
    assert prompt is not None
    assert prompt.kind == "calendar"
    assert prompt.title == "Design review"
    assert prompt.payload["id"] == "abc123"


def test_cancel_suppresses_the_same_meeting_including_a_later_zoom_join():
    meeting = event()
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=False, now=NOW)
    prompt, state = decide(snap(events=(meeting,)), state)
    assert prompt is None
    prompt, _state = decide(
        snap(in_zoom=True, zoom_session_id="9:100", zoom_age_seconds=1, events=(meeting,)),
        state,
    )
    assert prompt is None


def test_fresh_zoom_uses_a_nearby_calendar_event():
    meeting = event(start=NOW + timedelta(minutes=5))
    prompt, _state = decide(
        snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=3, zoom_title="Zoom", events=(meeting,))
    )
    assert prompt is not None
    assert prompt.kind == "zoom"
    assert prompt.title == "Design review"
    assert "zoom:4:10" in prompt.keys
    assert meeting.key() in prompt.keys


def test_fresh_zoom_without_a_calendar_event_is_adhoc():
    prompt, _state = decide(
        snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=3, zoom_title="Roadmap")
    )
    assert prompt is not None
    assert prompt.title == "Roadmap"
    assert prompt.payload["id"].startswith("zoom-")


def test_zoom_already_open_when_the_watcher_starts_does_not_prompt():
    state = WatcherState()
    prompt, state = decide(snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=600), state)
    assert prompt is None
    prompt, _state = decide(snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=605), state)
    assert prompt is None


def test_zoom_join_waits_for_presence():
    state = WatcherState()
    prompt, state = decide(
        snap(present=False, in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=2, zoom_title="Roadmap"),
        state,
    )
    assert prompt is None
    prompt, _state = decide(
        snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=20, zoom_title="Roadmap"),
        state,
    )
    assert prompt is not None
    assert prompt.title == "Roadmap"


def test_pause_swallows_a_zoom_join_and_holds_the_calendar_meeting():
    meeting = event()
    state = WatcherState()
    prompt, state = decide(
        snap(paused=True, in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=1, events=(meeting,)),
        state,
    )
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,)), state)
    assert prompt is not None
    assert prompt.kind == "calendar"


def test_accepting_a_prompt_arms_crash_recovery():
    meeting = event()
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=True, now=NOW)
    assert state.active_launch is not None
    state.active_launch.seen_running = True
    later = NOW + timedelta(seconds=30)
    prompt, state = decide(snap(events=(meeting,), granola_running=False), state, now=later)
    assert prompt is not None
    assert prompt.kind == "crash"
    apply_result(state, prompt, accepted=False, now=later)
    prompt, _state = decide(snap(events=(meeting,), granola_running=False), state, now=later)
    assert prompt is None


def test_crash_is_not_raised_before_granola_has_been_seen():
    state = WatcherState(
        active_launch=ActiveLaunch(
            title="Design review",
            payload={"id": "abc"},
            keys=("event:abc",),
            expires_at=NOW + timedelta(minutes=20),
            seen_running=False,
            launched_at=NOW - timedelta(seconds=30),
        )
    )
    prompt, _state = decide(snap(), state)
    assert prompt is None


def test_overlapping_meetings_prompt_for_the_one_that_started_latest():
    earlier = event(summary="Standup", start=NOW - timedelta(minutes=20), event_id="early")
    later = event(summary="Design review", start=NOW - timedelta(minutes=1), event_id="later")
    prompt, _state = decide(snap(events=(earlier, later)))
    assert prompt.title == "Design review"
