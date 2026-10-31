from dataclasses import fields
from datetime import datetime, timedelta, timezone

from granola_autostart.decide import ActiveLaunch, WatcherState
from granola_autostart.state_store import load_state, save_state


def test_every_field_survives_a_restart():
    now = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    pause = now + timedelta(hours=1)
    state = WatcherState(
        handled={"event:abc:start": now + timedelta(minutes=30), "zoom:4:10": now + timedelta(hours=12)},
        zoom_session_id="4:10",
        zoom_needs_decision=True,
        active_launch=ActiveLaunch(
            title="Design review",
            payload={"id": "abc", "summary": "Design review", "start": {"dateTime": now.isoformat()}},
            keys=("zoom:4:10", "event:abc:start"),
            expires_at=now + timedelta(minutes=30),
            seen_running=True,
            launched_at=now - timedelta(minutes=1),
            warned=True,
            zoom_session_id="4:10",
            missing_since=now + timedelta(seconds=20),
            failed=True,
            call_session="4:10",
        ),
        zoom_decided_session="4:10",
        zoom_decided_at=now - timedelta(minutes=1),
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
