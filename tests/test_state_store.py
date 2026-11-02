import json
from dataclasses import fields
from datetime import datetime, timedelta, timezone

from granola_autostart.decide import ActiveLaunch, Snapshot, WatcherState, evaluate
from granola_autostart.state_store import load_state, save_state


def test_every_field_survives_a_restart():
    now = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    pause = now + timedelta(hours=1)
    state = WatcherState(
        handled={
            "event:abc:start": now + timedelta(minutes=30),
            "zoom:4:10": now + timedelta(hours=12),
            "teams:77": now + timedelta(hours=12),
        },
        call_session_id="zoom:4:10",
        call_needs_decision=True,
        active_launch=ActiveLaunch(
            title="Design review",
            payload={"id": "abc", "summary": "Design review", "start": {"dateTime": now.isoformat()}},
            keys=("zoom:4:10", "event:abc:start"),
            expires_at=now + timedelta(minutes=30),
            seen_running=True,
            launched_at=now - timedelta(minutes=1),
            warned=True,
            call_session_id="meet:chrome:5",
            missing_since=now + timedelta(seconds=20),
            failed=True,
            call_session="teams:77",
        ),
        call_decided_session="teams:77",
        call_decided_at=now - timedelta(minutes=1),
    )
    # A field added later fails here until it is filled in above, and so saved and loaded.
    assert all(getattr(state, field.name) for field in fields(WatcherState))
    assert all(getattr(state.active_launch, field.name) for field in fields(ActiveLaunch))
    save_state(state, pause)
    loaded, loaded_pause = load_state()
    assert loaded == state
    assert loaded_pause == pause


def test_unset_fields_and_no_pause_survive_a_restart():
    now = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    state = WatcherState(
        active_launch=ActiveLaunch(
            title="Roadmap",
            payload={"id": "zoom-4-10"},
            keys=("zoom:4:10",),
            expires_at=now + timedelta(hours=4),
            seen_running=False,
            launched_at=now,
        )
    )
    save_state(state, None)
    assert load_state() == (state, None)
    save_state(WatcherState(), None)
    assert load_state() == (WatcherState(), None)


def test_damaged_state_starts_fresh(tmp_path):
    (tmp_path / "state.json").write_text('{"handled": {"event:x": 5}, "active_launch": []}', encoding="utf-8")
    loaded, pause = load_state()
    assert loaded.handled == {}
    assert pause is None


# A state.json as the version before Teams and Meet wrote it: session ids with no provider, under zoom_* names.
OLD_STATE = """{
  "version": 1,
  "handled": {"event:abc:2026-10-28T17:00:00+00:00": "2026-10-28T17:30:00+00:00", "zoom:604:1761670800": "2026-10-29T05:00:00+00:00"},
  "zoom_session_id": "604:1761670800",
  "zoom_needs_decision": false,
  "zoom_decided_session": "604:1761670800",
  "zoom_decided_at": "2026-10-28T17:00:05+00:00",
  "pause_until": null,
  "active_launch": {
    "title": "Roadmap",
    "payload": {"id": "zoom-604-1761670800"},
    "keys": ["zoom:604:1761670800"],
    "expires_at": "2026-10-28T21:00:00+00:00",
    "seen_running": true,
    "launched_at": "2026-10-28T17:00:10+00:00",
    "warned": false,
    "zoom_session_id": "604:1761670800",
    "missing_since": null,
    "failed": false,
    "call_session": "604:1761670800"
  }
}
"""


def test_a_state_file_from_before_teams_and_meet_still_loads_with_its_zoom_call_recognised(tmp_path):
    (tmp_path / "state.json").write_text(OLD_STATE, encoding="utf-8")
    state, pause = load_state()
    assert pause is None
    assert state.call_session_id == "zoom:604:1761670800" and not state.call_needs_decision
    assert state.call_decided_session == "zoom:604:1761670800"
    assert state.call_decided_at == datetime(2026, 10, 28, 17, 0, 5, tzinfo=timezone.utc)
    assert "zoom:604:1761670800" in state.handled and len(state.handled) == 2
    launch = state.active_launch
    assert launch.call_session_id == "zoom:604:1761670800" and launch.call_session == "zoom:604:1761670800"


def test_an_upgrade_in_the_middle_of_a_zoom_call_does_not_ask_about_it_again(tmp_path):
    (tmp_path / "state.json").write_text(OLD_STATE, encoding="utf-8")
    state, _pause = load_state()
    now = datetime(2026, 10, 28, 17, 5, tzinfo=timezone.utc)
    in_call = Snapshot(
        present=True,
        paused=False,
        call_session_id="zoom:604:1761670800",
        call_provider="zoom",
        call_age_seconds=295.0,
        call_title="Roadmap",
        events=(),
        granola_running=True,
    )
    prompt = evaluate(now, in_call, state, lookahead=timedelta(minutes=10), fresh_call_seconds=120)
    assert prompt is None
    assert state.active_launch is not None  # its call is still the one running
    assert state.active_launch.call_session_id == "zoom:604:1761670800"


def test_once_saved_again_the_old_names_are_gone(tmp_path):
    (tmp_path / "state.json").write_text(OLD_STATE, encoding="utf-8")
    state, pause = load_state()
    save_state(state, pause)
    saved = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert not [key for key in saved if key.startswith("zoom_")]
    assert saved["call_session_id"] == "zoom:604:1761670800" and saved["call_decided_session"] == "zoom:604:1761670800"
    assert "zoom_session_id" not in saved["active_launch"]
    assert saved["active_launch"]["call_session_id"] == "zoom:604:1761670800"
    assert load_state() == (state, pause)


def test_session_ids_that_already_name_a_provider_are_left_alone_and_new_names_beat_old_ones(tmp_path):
    raw = {
        "call_session_id": "teams:134400000000000000",
        "zoom_session_id": "999:1",  # stale: the new name wins
        "call_decided_session": None,  # saved as nothing, so the old name isn't read either
        "zoom_decided_session": "999:1",
        "active_launch": {
            "expires_at": "2026-10-28T21:00:00+00:00",
            "launched_at": "2026-10-28T17:00:10+00:00",
            "call_session_id": "meet:chrome:5",
            "call_session": "zoom:4:10",
        },
    }
    (tmp_path / "state.json").write_text(json.dumps(raw), encoding="utf-8")
    state, _pause = load_state()
    assert state.call_session_id == "teams:134400000000000000"
    assert state.call_decided_session is None
    assert (state.active_launch.call_session_id, state.active_launch.call_session) == ("meet:chrome:5", "zoom:4:10")
