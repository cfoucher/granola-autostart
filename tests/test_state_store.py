import os
from datetime import datetime, timedelta, timezone

from decide import ActiveLaunch, WatcherState
from state_store import load_state, save_state


def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    os.environ["GRANOLA_AUTOSTART_HOME"] = str(tmp_path)
    until = datetime(2026, 10, 28, 18, 0, tzinfo=timezone.utc)
    pause = datetime(2026, 10, 28, 19, 0, tzinfo=timezone.utc)
    state = WatcherState(
        handled={"event:abc:start": until},
        zoom_session_id="4:10",
        zoom_needs_decision=True,
        active_launch=ActiveLaunch(
            title="Design review",
            payload={"id": "abc", "summary": "Design review"},
            keys=("event:abc:start",),
            expires_at=until + timedelta(minutes=5),
            seen_running=True,
            launched_at=until - timedelta(minutes=1),
        ),
    )
    save_state(state, pause)
    loaded, loaded_pause = load_state()
    assert loaded.zoom_session_id == "4:10"
    assert loaded.zoom_needs_decision is True
    assert loaded.handled["event:abc:start"] == until
    assert loaded.active_launch is not None
    assert loaded.active_launch.seen_running is True
    assert loaded.active_launch.payload["summary"] == "Design review"
    assert loaded_pause == pause
