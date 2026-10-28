"""Persist which meetings were cancelled or started, and an in-progress launch."""

from __future__ import annotations

import json
from datetime import datetime

from decide import ActiveLaunch, WatcherState
from settings import state_path


def load_state() -> tuple[WatcherState, datetime | None]:
    path = state_path()
    if not path.exists():
        return WatcherState(), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return WatcherState(), None
    state = WatcherState(
        handled={key: _dt(value) for key, value in (raw.get("handled") or {}).items()},
        zoom_session_id=raw.get("zoom_session_id"),
        zoom_needs_decision=bool(raw.get("zoom_needs_decision")),
    )
    launch = raw.get("active_launch")
    if isinstance(launch, dict) and launch.get("expires_at") and launch.get("launched_at"):
        state.active_launch = ActiveLaunch(
            title=str(launch.get("title") or "Meeting"),
            payload=launch.get("payload") or {},
            keys=tuple(launch.get("keys") or ()),
            expires_at=_dt(launch["expires_at"]),
            seen_running=bool(launch.get("seen_running")),
            launched_at=_dt(launch["launched_at"]),
            warned=bool(launch.get("warned")),
        )
    pause = raw.get("pause_until")
    return state, _dt(pause) if pause else None


def save_state(state: WatcherState, pause_until: datetime | None) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    launch = state.active_launch
    payload = {
        "version": 1,
        "handled": {key: until.isoformat() for key, until in state.handled.items()},
        "zoom_session_id": state.zoom_session_id,
        "zoom_needs_decision": state.zoom_needs_decision,
        "pause_until": pause_until.isoformat() if pause_until else None,
        "active_launch": None
        if launch is None
        else {
            "title": launch.title,
            "payload": launch.payload,
            "keys": list(launch.keys),
            "expires_at": launch.expires_at.isoformat(),
            "seen_running": launch.seen_running,
            "launched_at": launch.launched_at.isoformat(),
            "warned": launch.warned,
        },
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed
