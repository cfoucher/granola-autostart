import threading
from datetime import datetime, timedelta, timezone

import pytest

from granola_autostart import watcher
from granola_autostart.decide import Prompt, WatcherState
from granola_autostart.settings import Settings

NOW = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)


def prompt() -> Prompt:
    return Prompt(
        kind="calendar",
        title="Design review",
        payload={"id": "abc", "summary": "Design review"},
        keys=("event:abc",),
        expires_at=NOW + timedelta(minutes=30),
    )


@pytest.fixture
def gate(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    launches: list[str] = []
    world = {"accepted": True, "unlocked": True, "idle": 1.0}
    monkeypatch.setattr(watcher, "ask_notice", lambda *_args: world["accepted"])
    monkeypatch.setattr(watcher, "is_unlocked", lambda: world["unlocked"])
    monkeypatch.setattr(watcher, "idle_seconds", lambda: world["idle"])
    monkeypatch.setattr(watcher, "launch", lambda url, dry_run: launches.append(url))

    def run(control=None):
        state = WatcherState()
        watcher.handle_prompt(prompt(), Settings(), control or watcher.Control(), threading.Lock(), state)
        return state

    return world, launches, run


def test_finished_countdown_starts_granola(gate):
    _world, launches, run = gate
    state = run()
    assert len(launches) == 1
    assert "event:abc" in state.handled


def test_locking_during_the_countdown_starts_nothing_and_asks_again(gate):
    world, launches, run = gate
    world["unlocked"] = False
    state = run()
    assert launches == []
    assert state.handled == {}


def test_pausing_during_the_countdown_starts_nothing(gate):
    _world, launches, run = gate
    control = watcher.Control()
    control.set_pause(datetime.now().astimezone() + timedelta(hours=1))
    state = run(control)
    assert launches == []
    assert state.handled == {}


def test_cancel_records_the_decision(gate):
    world, launches, run = gate
    world["accepted"] = False
    state = run()
    assert launches == []
    assert "event:abc" in state.handled
