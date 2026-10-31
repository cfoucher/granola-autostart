from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from granola_autostart.calendar_feed import CalendarEvent
from granola_autostart.decide import HANDLED_GRACE, ActiveLaunch, Prompt, Snapshot, WatcherState, apply_result, evaluate

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
LOOKAHEAD = timedelta(minutes=10)


def event(summary="Design review", start=NOW, minutes=30, event_id="abc123", skipped=False):
    return CalendarEvent(id=event_id, summary=summary, start=start, end=start + timedelta(minutes=minutes), skipped=skipped)


def snap(**overrides) -> Snapshot:
    base = dict(
        present=True,
        paused=False,
        zoom_session_id=None,
        zoom_age_seconds=None,
        zoom_title=None,
        events=(),
        granola_running=False,
    )
    base.update(overrides)
    return Snapshot(**base)


def zoom_join(session="9:100", age=1, title=None, **overrides) -> Snapshot:
    return snap(zoom_session_id=session, zoom_age_seconds=age, zoom_title=title, **overrides)


def decide(snapshot, state=None, now=NOW):
    state = state or WatcherState()
    prompt = evaluate(now, snapshot, state, lookahead=LOOKAHEAD, fresh_zoom_seconds=120)
    return prompt, state


def answered(snapshot, accepted, state=None, now=NOW) -> WatcherState:
    prompt, state = decide(snapshot, state, now)
    assert prompt is not None
    apply_result(state, prompt, accepted=accepted, now=now)
    return state


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


def test_a_zoom_call_with_no_meeting_becomes_an_hour_long_note_of_its_own():
    prompt, _state = decide(zoom_join(session="10:20", title="Roadmap", events=()))
    assert (prompt.title, prompt.payload["summary"], prompt.payload["id"]) == ("Roadmap", "Roadmap", "zoom-10-20")
    assert prompt.payload["end"]["dateTime"] == (NOW + timedelta(hours=1)).isoformat()
    prompt, _state = decide(zoom_join(events=()))
    assert prompt.title == "Zoom meeting"


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
    state = answered(snap(events=(meeting,)), accepted=False)
    prompt, state = decide(snap(events=(meeting,)), state)
    assert prompt is None
    prompt, _state = decide(zoom_join(events=(meeting,)), state)
    assert prompt is None


def test_fresh_zoom_uses_a_nearby_calendar_event():
    meeting = event(start=NOW + timedelta(minutes=5))
    prompt, _state = decide(zoom_join("4:10", age=3, title="Zoom", events=(meeting,)))
    assert prompt is not None
    assert prompt.kind == "zoom"
    assert prompt.title == "Design review"
    assert "zoom:4:10" in prompt.keys
    assert meeting.key() in prompt.keys


def test_fresh_zoom_without_a_calendar_event_is_adhoc():
    prompt, _state = decide(zoom_join("4:10", age=3, title="Roadmap"))
    assert prompt is not None
    assert prompt.title == "Roadmap"
    assert prompt.payload["id"].startswith("zoom-")


def test_zoom_already_open_when_the_watcher_starts_does_not_prompt():
    prompt, state = decide(zoom_join("4:10", age=600))
    assert prompt is None
    prompt, _state = decide(zoom_join("4:10", age=605), state)
    assert prompt is None


def test_zoom_join_waits_for_presence():
    prompt, state = decide(zoom_join("4:10", age=2, title="Roadmap", present=False))
    assert prompt is None
    prompt, _state = decide(zoom_join("4:10", age=20, title="Roadmap"), state)
    assert prompt is not None
    assert prompt.title == "Roadmap"


def test_moving_straight_to_another_zoom_call_counts_as_a_fresh_join():
    state = answered(zoom_join("4:10", age=3), accepted=False)
    prompt, _state = decide(zoom_join("5:20", age=900), state, now=NOW + timedelta(minutes=15))
    assert prompt is not None and prompt.kind == "zoom"
    assert prompt.keys == ("zoom:5:20",)


def test_returning_to_a_zoom_call_already_answered_does_not_ask_again():
    state = answered(zoom_join("4:10", age=3), accepted=True)
    state = answered(zoom_join("5:20", age=3), accepted=False, state=state, now=NOW + timedelta(minutes=5))
    prompt, _state = decide(zoom_join("4:10", age=600), state, now=NOW + timedelta(minutes=10))
    assert prompt is None


def test_pause_swallows_a_zoom_join_and_holds_the_calendar_meeting():
    meeting = event()
    prompt, state = decide(zoom_join("4:10", paused=True, events=(meeting,)))
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,)), state)
    assert prompt is not None
    assert prompt.kind == "calendar"


def test_a_zoom_call_joined_while_paused_is_not_asked_about_after_resuming():
    prompt, state = decide(zoom_join("4:10", age=1, paused=True))
    assert prompt is None
    prompt, _state = decide(zoom_join("4:10", age=30), state, now=NOW + timedelta(seconds=29))
    assert prompt is None


def test_accepting_a_prompt_arms_crash_recovery():
    meeting = event()
    state = answered(snap(events=(meeting,)), accepted=True)
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


def test_the_crash_prompt_waits_for_the_user_to_be_present():
    meeting = event()
    state = answered(snap(events=(meeting,)), accepted=True)
    decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=5))
    gone = NOW + timedelta(seconds=30)
    decide(snap(events=(meeting,)), state, now=gone)
    later = gone + timedelta(seconds=10)
    prompt, state = decide(snap(present=False, events=(meeting,)), state, now=later)
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,)), state, now=later + timedelta(minutes=1))
    assert prompt is not None and prompt.kind == "crash"


def test_a_meeting_that_began_after_granola_closed_gets_its_own_card_not_the_crash_card():
    review = event()
    state = answered(snap(events=(review,)), accepted=True)
    decide(snap(events=(review,), granola_running=True), state, now=NOW + timedelta(seconds=5))
    decide(snap(present=False, events=(review,)), state, now=NOW + timedelta(seconds=30))
    standup = event(summary="Standup", start=NOW + timedelta(minutes=2), event_id="standup")
    back = NOW + timedelta(minutes=3)
    prompt, state = decide(snap(events=(review, standup)), state, now=back)
    assert (prompt.kind, prompt.title) == ("calendar", "Standup")
    apply_result(state, prompt, accepted=True, now=back)
    prompt, _state = decide(snap(events=(review, standup), granola_running=True), state, now=back + timedelta(seconds=5))
    assert prompt is None


def test_a_link_that_never_opened_is_not_a_crash():
    launch = ActiveLaunch(
        "Design review", {"id": "abc"}, ("event:abc",), NOW + timedelta(minutes=20), seen_running=True,
        launched_at=NOW - timedelta(minutes=1), missing_since=NOW - timedelta(seconds=30), failed=True,
    )
    prompt, _state = decide(snap(), WatcherState(active_launch=launch))
    assert prompt is None


def test_a_brief_granola_restart_is_not_a_crash():
    meeting = event()
    state = answered(snap(events=(meeting,)), accepted=True)
    decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=20))
    decide(snap(events=(meeting,), granola_running=False), state, now=NOW + timedelta(seconds=25))
    prompt, state = decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=30))
    assert prompt is None
    prompt, _state = decide(snap(events=(meeting,), granola_running=False), state, now=NOW + timedelta(seconds=35))
    assert prompt is None


def test_cancelling_another_meeting_keeps_crash_recovery_for_the_running_one():
    first = event(summary="Standup", start=NOW - timedelta(minutes=5), minutes=60, event_id="first")
    second = event(summary="Design review", start=NOW + timedelta(minutes=10), event_id="second")
    state = answered(snap(events=(first, second)), accepted=True)
    decide(snap(events=(first, second), granola_running=True), state, now=NOW + timedelta(seconds=5))
    at_second = second.start + timedelta(seconds=1)
    prompt, state = decide(snap(events=(first, second), granola_running=True), state, now=at_second)
    assert prompt is not None and prompt.title == "Design review"
    apply_result(state, prompt, accepted=False, now=at_second)
    assert state.active_launch is not None
    assert state.active_launch.title == "Standup"


def test_adhoc_zoom_crash_recovery_ends_with_the_call():
    call = zoom_join("4:10", age=3, title="Roadmap")
    state = answered(call, accepted=True)
    decide(replace(call, granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert state.active_launch is not None
    after_call = NOW + timedelta(minutes=30)
    decide(snap(granola_running=True), state, now=after_call)
    assert state.active_launch is None
    prompt, _state = decide(snap(granola_running=False), state, now=after_call + timedelta(minutes=1))
    assert prompt is None


def test_a_launch_is_forgotten_a_grace_period_after_its_meeting_ends():
    meeting = event()
    state = answered(snap(events=(meeting,)), accepted=True)
    decide(snap(), state, now=meeting.end + HANDLED_GRACE - timedelta(seconds=1))
    assert state.active_launch is not None
    decide(snap(), state, now=meeting.end + HANDLED_GRACE)
    assert state.active_launch is None


def test_an_extended_meeting_does_not_prompt_again():
    meeting = event(minutes=30)
    state = answered(snap(events=(meeting,)), accepted=False)
    extended = replace(meeting, end=meeting.end + timedelta(minutes=30))
    decide(snap(events=(extended,)), state, now=NOW + timedelta(minutes=20))
    prompt, _state = decide(snap(events=(extended,)), state, now=NOW + timedelta(minutes=40))
    assert prompt is None


def test_joining_zoom_early_matches_the_next_meeting_not_the_one_ending():
    ending = event(summary="Standup", start=NOW - timedelta(minutes=25), minutes=30, event_id="ending")
    nextup = event(summary="Design review", start=NOW + timedelta(minutes=5), event_id="next")
    prompt, state = decide(zoom_join("4:10", age=3, events=(ending, nextup)))
    assert prompt is not None
    assert prompt.title == "Design review"
    apply_result(state, prompt, accepted=False, now=NOW)
    still_in_call = zoom_join("4:10", age=400, events=(ending, nextup))
    prompt, _state = decide(still_in_call, state, now=nextup.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Design review"


def test_zoom_rejoin_near_the_end_of_a_decided_meeting_leaves_the_next_one_alone():
    current = event(summary="Standup", start=NOW - timedelta(minutes=52), minutes=60, event_id="current")
    nextup = event(summary="Design review", start=current.end, event_id="next")
    state = answered(snap(events=(current, nextup)), accepted=True, now=current.start)
    prompt, state = decide(zoom_join("7:20", age=3, events=(current, nextup)), state)
    assert prompt is None
    prompt, _state = decide(snap(events=(current, nextup)), state, now=nextup.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Design review"


def test_a_calendar_that_loads_after_a_zoom_prompt_does_not_prompt_again():
    call = zoom_join("4:10", age=3, title="Roadmap")
    prompt, state = decide(call)
    assert prompt.payload["id"].startswith("zoom-")
    apply_result(state, prompt, accepted=True, now=NOW)
    current = event(summary="Roadmap", start=NOW - timedelta(minutes=2), event_id="current")
    later = event(summary="Overlap", start=NOW + timedelta(minutes=20), event_id="later")
    prompt, state = decide(replace(call, events=(current, later)), state, now=NOW + timedelta(seconds=10))
    assert prompt is None
    prompt, _state = decide(replace(call, events=(current, later)), state, now=later.start + timedelta(seconds=5))
    assert prompt is not None
    assert prompt.title == "Overlap"


def test_a_zoom_call_found_already_running_does_not_hide_the_calendar_prompt():
    meeting = event(start=NOW - timedelta(minutes=20))
    prompt, _state = decide(zoom_join("4:10", age=900, events=(meeting,)))
    assert prompt is not None
    assert prompt.kind == "calendar"


def test_a_zoom_call_right_after_a_decided_meeting_is_adhoc():
    meeting = event(start=NOW - timedelta(minutes=40), minutes=30)
    state = answered(snap(events=(meeting,)), accepted=True, now=meeting.start)
    prompt, _state = decide(zoom_join("8:30", age=3, title="Hallway chat", events=(meeting,)), state)
    assert prompt is not None
    assert prompt.title == "Hallway chat"
    assert prompt.payload["id"].startswith("zoom-")


def test_crash_recovery_follows_a_late_extension():
    meeting = event(minutes=30)
    state = answered(snap(events=(meeting,)), accepted=True)
    decide(snap(events=(meeting,), granola_running=True), state, now=NOW + timedelta(seconds=5))
    decide(snap(events=(), granola_running=True), state, now=meeting.end + timedelta(minutes=1))
    extended = replace(meeting, end=meeting.end + timedelta(minutes=30))
    decide(snap(events=(extended,), granola_running=True), state, now=meeting.end + timedelta(minutes=3))
    decide(snap(events=(extended,), granola_running=False), state, now=meeting.end + timedelta(minutes=4))
    prompt, _state = decide(snap(events=(extended,), granola_running=False), state, now=meeting.end + timedelta(minutes=5))
    assert prompt is not None
    assert prompt.kind == "crash"


def test_an_extension_seen_after_the_old_end_does_not_prompt_again():
    meeting = event(minutes=30)
    state = answered(snap(events=(meeting,)), accepted=False)
    decide(snap(events=()), state, now=meeting.end + timedelta(minutes=1))
    extended = replace(meeting, end=meeting.end + timedelta(minutes=30))
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


def test_answering_keeps_the_later_of_the_old_and_new_expiry():
    prompt = Prompt(
        kind="calendar",
        title="Design review",
        payload={},
        keys=("event:a", "event:b"),
        expires_at=NOW + timedelta(minutes=30),
    )
    state = WatcherState(handled={"event:a": NOW + timedelta(hours=2), "event:b": NOW})
    apply_result(state, prompt, accepted=False, now=NOW)
    assert state.handled == {"event:a": NOW + timedelta(hours=2), "event:b": NOW + timedelta(minutes=30)}


def test_overlapping_meetings_prompt_for_the_one_that_started_latest():
    earlier = event(summary="Standup", start=NOW - timedelta(minutes=20), event_id="early")
    later = event(summary="Design review", start=NOW - timedelta(minutes=1), event_id="later")
    prompt, _state = decide(snap(events=(earlier, later)))
    assert prompt.title == "Design review"


def test_starting_notes_for_one_of_two_overlapping_meetings_does_not_ask_about_the_other():
    standup = event(summary="Standup", start=NOW - timedelta(minutes=20), event_id="early")
    review = event(summary="Design review", start=NOW - timedelta(minutes=1), event_id="later")
    state = answered(snap(events=(standup, review)), accepted=True)
    prompt, state = decide(snap(events=(standup, review), granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert prompt is None
    assert state.active_launch.title == "Design review"


def test_a_double_booking_asks_once():
    first = event(summary="Design review", event_id="a")
    second = event(summary="Vendor call", event_id="b")
    state = answered(snap(events=(first, second)), accepted=True)
    prompt, _state = decide(snap(events=(first, second), granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert prompt is None


def test_cancelling_one_of_two_overlapping_meetings_asks_about_the_other():
    standup = event(summary="Standup", start=NOW - timedelta(minutes=20), event_id="early")
    review = event(summary="Design review", start=NOW - timedelta(minutes=1), event_id="later")
    state = answered(snap(events=(standup, review)), accepted=False)
    prompt, _state = decide(snap(events=(standup, review)), state, now=NOW + timedelta(seconds=5))
    assert prompt is not None and prompt.title == "Standup"


def test_starting_notes_from_a_zoom_join_answers_the_other_meeting_under_way():
    standup = event(summary="Standup", start=NOW - timedelta(minutes=20), event_id="early")
    review = event(summary="Design review", start=NOW - timedelta(minutes=1), event_id="later")
    prompt, state = decide(zoom_join(events=(standup, review)))
    assert prompt.kind == "zoom" and prompt.title == "Design review"
    apply_result(state, prompt, accepted=True, now=NOW)
    prompt, _state = decide(zoom_join(events=(standup, review), granola_running=True), state, now=NOW + timedelta(seconds=5))
    assert prompt is None


def test_granola_closing_after_a_zoom_call_s_meeting_loads_late_still_gets_the_crash_card():
    call = zoom_join(session="4:10", age=3, title="Roadmap", events=())
    prompt, state = decide(call)
    assert prompt.kind == "zoom"
    apply_result(state, prompt, accepted=True, now=NOW)
    meeting = event(summary="Roadmap", start=NOW + timedelta(minutes=2), event_id="late")
    in_call = replace(call, zoom_age_seconds=400, events=(meeting,), granola_running=True)
    prompt, state = decide(in_call, state, now=NOW + timedelta(minutes=7))
    assert prompt is None  # the meeting that loaded late is that call, already answered
    closed = replace(in_call, granola_running=False)
    decide(closed, state, now=NOW + timedelta(minutes=12))
    prompt, _state = decide(closed, state, now=NOW + timedelta(minutes=12, seconds=15))
    assert prompt is not None and prompt.kind == "crash"


def adhoc_call_with_a_meeting_that_loads_late(accepted: bool):
    call = zoom_join(session="7:99", age=3, title="Quick sync", events=())
    state = answered(call, accepted=accepted)
    meeting = event(summary="Quick sync", start=NOW, event_id="late")
    in_call = replace(call, zoom_age_seconds=300, events=(meeting,))
    prompt, state = decide(in_call, state, now=NOW + timedelta(minutes=4))
    assert prompt is None
    return state, meeting


@pytest.mark.parametrize("accepted", [True, False])
def test_an_answer_to_an_adhoc_call_outlasts_the_call(accepted):
    state, meeting = adhoc_call_with_a_meeting_that_loads_late(accepted)
    prompt, _state = decide(snap(events=(meeting,)), state, now=NOW + timedelta(minutes=20))
    assert prompt is None


def test_zoom_dropping_for_a_moment_does_not_ask_about_the_call_s_meeting_again():
    state, meeting = adhoc_call_with_a_meeting_that_loads_late(True)
    prompt, state = decide(snap(events=(meeting,)), state, now=NOW + timedelta(minutes=5))
    assert prompt is None
    back = zoom_join(session="7:99", age=330, events=(meeting,))
    prompt, _state = decide(back, state, now=NOW + timedelta(minutes=5, seconds=5))
    assert prompt is None


def test_granola_closing_after_a_zoom_meeting_s_call_has_ended_is_not_a_crash():
    weekly = event(summary="Weekly", minutes=60, event_id="weekly")
    call = zoom_join(session="5:50", age=3, events=(weekly,))
    state = answered(call, accepted=True)
    decide(replace(call, zoom_age_seconds=60, granola_running=True), state, now=NOW + timedelta(minutes=1))
    after_call = snap(events=(weekly,))
    decide(after_call, state, now=NOW + timedelta(minutes=40))
    prompt, _state = decide(after_call, state, now=NOW + timedelta(minutes=40, seconds=15))
    assert prompt is None


def test_granola_closing_during_a_zoom_meeting_s_call_is_still_a_crash():
    weekly = event(summary="Weekly", minutes=60, event_id="weekly")
    call = zoom_join(session="5:50", age=3, events=(weekly,))
    state = answered(call, accepted=True)
    decide(replace(call, zoom_age_seconds=60, granola_running=True), state, now=NOW + timedelta(minutes=1))
    still_in_call = replace(call, zoom_age_seconds=600)
    decide(still_in_call, state, now=NOW + timedelta(minutes=10))
    prompt, _state = decide(still_in_call, state, now=NOW + timedelta(minutes=10, seconds=15))
    assert prompt is not None and prompt.kind == "crash"


def test_a_meeting_that_starts_after_an_adhoc_call_still_gets_its_card():
    call = zoom_join(session="7:99", age=3, title="Quick sync", events=())
    state = answered(call, accepted=False)
    planning = event(summary="Planning", start=NOW + timedelta(minutes=5), event_id="planning")
    prompt, state = decide(replace(call, zoom_age_seconds=60, events=(planning,)), state, now=NOW + timedelta(minutes=1))
    assert prompt is None
    decide(snap(events=(planning,)), state, now=NOW + timedelta(minutes=3))  # the call has ended
    prompt, _state = decide(snap(events=(planning,)), state, now=planning.start + timedelta(seconds=5))
    assert prompt is not None and prompt.title == "Planning"


def zoom_meeting_whose_granola_closes():
    weekly = event(summary="Weekly", minutes=60, event_id="weekly")
    call = zoom_join(session="1:1", age=3, events=(weekly,))
    state = answered(call, accepted=True)
    decide(replace(call, zoom_age_seconds=60, granola_running=True), state, now=NOW + timedelta(minutes=1))
    return weekly, state


@pytest.mark.parametrize("left_first", [True, False])
def test_rejoining_a_zoom_meeting_s_call_keeps_its_restart_card(left_first):
    weekly, state = zoom_meeting_whose_granola_closes()
    if left_first:
        decide(snap(events=(weekly,), granola_running=True), state, now=NOW + timedelta(minutes=9))
    rejoined = zoom_join(session="2:2", age=3, events=(weekly,), granola_running=True)
    prompt, state = decide(rejoined, state, now=NOW + timedelta(minutes=10))
    assert prompt is None  # the same meeting, already answered
    closed = replace(rejoined, zoom_age_seconds=600, granola_running=False)
    decide(closed, state, now=NOW + timedelta(minutes=20))
    prompt, _state = decide(closed, state, now=NOW + timedelta(minutes=20, seconds=15))
    assert prompt is not None and prompt.kind == "crash"


def test_restarted_notes_still_end_with_the_zoom_meeting_s_call():
    weekly, state = zoom_meeting_whose_granola_closes()
    in_call = zoom_join(session="1:1", age=600, events=(weekly,))
    decide(in_call, state, now=NOW + timedelta(minutes=10))
    crash, state = decide(in_call, state, now=NOW + timedelta(minutes=10, seconds=15))
    assert crash.kind == "crash"
    apply_result(state, crash, accepted=True, now=NOW + timedelta(minutes=10, seconds=15))
    decide(replace(in_call, granola_running=True), state, now=NOW + timedelta(minutes=11))
    after_call = snap(events=(weekly,))
    decide(after_call, state, now=NOW + timedelta(minutes=40))
    prompt, _state = decide(after_call, state, now=NOW + timedelta(minutes=40, seconds=15))
    assert prompt is None


def test_an_adhoc_answer_does_not_answer_the_meeting_of_the_next_call():
    state = answered(zoom_join(session="7:99", age=3, title="Quick sync"), accepted=True)
    review = event(summary="Design review", start=NOW + timedelta(minutes=5), event_id="next")
    next_call = zoom_join(session="8:11", age=900, events=(review,))
    state = answered(next_call, accepted=False, state=state, now=NOW + timedelta(minutes=1))  # "still in the last one"
    prompt, _state = decide(next_call, state, now=review.start + timedelta(seconds=5))
    assert prompt is not None and prompt.title == "Design review"
