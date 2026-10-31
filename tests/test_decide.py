from datetime import datetime, timedelta, timezone

from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import ActiveLaunch, Snapshot, WatcherState, apply_result, evaluate

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
LOOKAHEAD = timedelta(minutes=10)


def event(summary="Design review", start=NOW, minutes=30, event_id="abc123", skipped=False):
    return CalendarEvent(id=event_id, summary=summary, start=start, end=start + timedelta(minutes=minutes), skipped=skipped)


def zoom_join(**overrides) -> Snapshot:
    return snap(in_zoom=True, zoom_session_id="9:100", zoom_age_seconds=1, **overrides)


def test_a_skipped_meeting_never_prompts_even_when_its_zoom_call_is_joined():
    lunch = event("Team lunch", skipped=True)
    prompt, state = decide(snap(events=(lunch,)))
    assert prompt is None
    prompt, state = decide(zoom_join(events=(lunch,)), state)
    assert prompt is None
    assert "zoom:9:100" in state.handled
    prompt, _state = decide(zoom_join(events=(lunch,)), state)
    assert prompt is None


def test_a_real_meeting_beats_a_skipped_one_for_the_same_zoom_call():
    lunch = event("Team lunch", start=NOW - timedelta(minutes=10), minutes=60, event_id="lunch", skipped=True)
    review = event("Design review", start=NOW - timedelta(minutes=2), event_id="review")
    prompt, _state = decide(zoom_join(events=(lunch, review)))
    assert prompt is not None and prompt.title == "Design review"


def test_a_zoom_call_with_no_meeting_at_all_still_prompts():
    prompt, _state = decide(zoom_join(events=()))
    assert prompt is not None and prompt.kind == "zoom"


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
    prompt, state = decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert prompt is None
    gone = NOW + timedelta(seconds=30)
    prompt, state = decide(snap(events=(meeting,), granola_running=False), state, now=gone)
    assert prompt is None
    later = gone + timedelta(seconds=10)
    prompt, state = decide(snap(events=(meeting,), granola_running=False), state, now=later)
    assert prompt is not None
    assert prompt.kind == "crash"
    apply_result(state, prompt, accepted=False, now=later)
    prompt, _state = decide(snap(events=(meeting,), granola_running=False), state, now=later)
    assert prompt is None


def test_a_brief_granola_restart_is_not_a_crash():
    meeting = event()
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=True, now=NOW)
    decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=20))
    decide(snap(events=(meeting,), granola_running=False), state, now=NOW + timedelta(seconds=25))
    prompt, state = decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=30))
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,), granola_running=False), state, now=NOW + timedelta(seconds=35))
    assert prompt is None


def test_cancelling_another_meeting_keeps_crash_recovery_for_the_running_one():
    first = event(summary="Standup", start=NOW - timedelta(minutes=5), minutes=60, event_id="first")
    second = event(summary="Design review", start=NOW + timedelta(minutes=10), event_id="second")
    state = WatcherState()
    prompt, _ = decide(snap(events=(first, second)), state)
    apply_result(state, prompt, accepted=True, now=NOW)
    decide(snap(events=(first, second), granola_running=True), state, now=NOW + timedelta(seconds=5))
    at_second = second.start + timedelta(seconds=1)
    prompt, state = decide(snap(events=(first, second), granola_running=True), state, now=at_second)
    assert prompt is not None and prompt.title == "Design review"
    apply_result(state, prompt, accepted=False, now=at_second)
    assert state.active_launch is not None
    assert state.active_launch.title == "Standup"


def test_adhoc_zoom_crash_recovery_ends_with_the_call():
    state = WatcherState()
    zoom = dict(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=3, zoom_title="Roadmap")
    prompt, state = decide(snap(**zoom), state)
    apply_result(state, prompt, accepted=True, now=NOW)
    decide(snap(**zoom, granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert state.active_launch is not None
    after_call = NOW + timedelta(minutes=30)
    decide(snap(granola_running=True), state, now=after_call)
    assert state.active_launch is None
    prompt, _state = decide(snap(granola_running=False), state, now=after_call + timedelta(minutes=1))
    assert prompt is None


def test_an_extended_meeting_does_not_prompt_again():
    meeting = event(minutes=30)
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=False, now=NOW)
    extended = CalendarEvent(id=meeting.id, summary=meeting.summary, start=meeting.start, end=meeting.end + timedelta(minutes=30))
    decide(snap(events=(extended,)), state, now=NOW + timedelta(minutes=20))
    prompt, _state = decide(snap(events=(extended,)), state, now=NOW + timedelta(minutes=40))
    assert prompt is None


def test_joining_zoom_early_matches_the_next_meeting_not_the_one_ending():
    ending = event(summary="Standup", start=NOW - timedelta(minutes=25), minutes=30, event_id="ending")
    nextup = event(summary="Design review", start=NOW + timedelta(minutes=5), event_id="next")
    prompt, state = decide(snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=3, events=(ending, nextup)))
    assert prompt is not None
    assert prompt.title == "Design review"
    apply_result(state, prompt, accepted=False, now=NOW)
    still_in_call = snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=400, events=(ending, nextup))
    prompt, _state = decide(still_in_call, state, now=nextup.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Design review"


def test_zoom_rejoin_near_the_end_of_a_decided_meeting_leaves_the_next_one_alone():
    current = event(summary="Standup", start=NOW - timedelta(minutes=52), minutes=60, event_id="current")
    nextup = event(summary="Design review", start=current.end, event_id="next")
    state = WatcherState()
    first = decide(snap(events=(current, nextup)), state, now=current.start)[0]
    apply_result(state, first, accepted=True, now=current.start)
    prompt, state = decide(
        snap(in_zoom=True, zoom_session_id="7:20", zoom_age_seconds=3, events=(current, nextup)),
        state,
    )
    assert prompt is None
    prompt, _state = decide(snap(events=(current, nextup)), state, now=nextup.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Design review"


def test_a_calendar_that_loads_after_a_zoom_prompt_does_not_prompt_again():
    zoom = dict(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=3, zoom_title="Roadmap")
    prompt, state = decide(snap(**zoom))
    assert prompt.payload["id"].startswith("zoom-")
    apply_result(state, prompt, accepted=True, now=NOW)
    current = event(summary="Roadmap", start=NOW - timedelta(minutes=2), event_id="current")
    later = event(summary="Overlap", start=NOW + timedelta(minutes=20), event_id="later")
    prompt, state = decide(snap(**zoom, events=(current, later)), state, now=NOW + timedelta(seconds=10))
    assert prompt is None
    prompt, _state = decide(snap(**zoom, events=(current, later)), state, now=later.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Overlap"


def test_a_zoom_call_found_already_running_does_not_hide_the_calendar_prompt():
    meeting = event(start=NOW - timedelta(minutes=20))
    prompt, _state = decide(snap(in_zoom=True, zoom_session_id="4:10", zoom_age_seconds=900, events=(meeting,)))
    assert prompt is not None
    assert prompt.kind == "calendar"


def test_a_zoom_call_right_after_a_decided_meeting_is_adhoc():
    meeting = event(start=NOW - timedelta(minutes=40), minutes=30)
    state = WatcherState()
    first = decide(snap(events=(meeting,)), state, now=meeting.start)[0]
    apply_result(state, first, accepted=True, now=meeting.start)
    prompt, _state = decide(
        snap(in_zoom=True, zoom_session_id="8:30", zoom_age_seconds=3, zoom_title="Hallway chat", events=(meeting,)),
        state,
    )
    assert prompt is not None
    assert prompt.title == "Hallway chat"
    assert prompt.payload["id"].startswith("zoom-")


def test_crash_recovery_follows_a_late_extension():
    meeting = event(minutes=30)
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=True, now=NOW)
    decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=5))
    decide(snap(events=(), granola_running=True), state, now=meeting.end + timedelta(minutes=1))
    extended = CalendarEvent(id=meeting.id, summary=meeting.summary, start=meeting.start, end=meeting.end + timedelta(minutes=30))
    decide(snap(events=(extended,), granola_running=True), state, now=meeting.end + timedelta(minutes=3))
    decide(snap(events=(extended,), granola_running=False), state, now=meeting.end + timedelta(minutes=4))
    prompt, _state = decide(snap(events=(extended,), granola_running=False), state, now=meeting.end + timedelta(minutes=5))
    assert prompt is not None
    assert prompt.kind == "crash"


def test_an_extension_seen_after_the_old_end_does_not_prompt_again():
    meeting = event(minutes=30)
    state = WatcherState()
    prompt, _ = decide(snap(events=(meeting,)), state)
    apply_result(state, prompt, accepted=False, now=NOW)
    decide(snap(events=()), state, now=meeting.end + timedelta(minutes=1))
    extended = CalendarEvent(id=meeting.id, summary=meeting.summary, start=meeting.start, end=meeting.end + timedelta(minutes=30))
    prompt, _state = decide(snap(events=(extended,)), state, now=meeting.end + timedelta(minutes=3))
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
