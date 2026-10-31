"""Persist which meetings were cancelled or started, and an in-progress launch."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .decide import ActiveLaunch, WatcherState
from .settings import state_path, write_json

log = logging.getLogger("granola_autostart")


def load_state() -> tuple[WatcherState, datetime | None]:
    path = state_path()
    if not path.exists():
        return WatcherState(), None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return _parse(raw)
    except Exception as error:
        log.warning("state.json unreadable (%s); starting fresh", type(error).__name__)
        return WatcherState(), None


def _parse(raw: dict) -> tuple[WatcherState, datetime | None]:
    state = WatcherState(
        handled={str(key): _dt(value) for key, value in (raw.get("handled") or {}).items()},
        zoom_session_id=raw.get("zoom_session_id"),
        zoom_needs_decision=bool(raw.get("zoom_needs_decision")),
        zoom_decided_session=raw.get("zoom_decided_session"),
        zoom_decided_at=_dt(raw["zoom_decided_at"]) if raw.get("zoom_decided_at") else None,
    )
    launch = raw.get("active_launch")
    if isinstance(launch, dict) and launch.get("expires_at") and launch.get("launched_at"):
        missing = launch.get("missing_since")
        state.active_launch = ActiveLaunch(
            title=str(launch.get("title") or "Meeting"),
            payload=launch.get("payload") or {},
            keys=tuple(str(key) for key in launch.get("keys") or ()),
            expires_at=_dt(launch["expires_at"]),
            seen_running=bool(launch.get("seen_running")),
            launched_at=_dt(launch["launched_at"]),
            warned=bool(launch.get("warned")),
            zoom_session_id=launch.get("zoom_session_id"),
            missing_since=_dt(missing) if missing else None,
            failed=bool(launch.get("failed")),
            call_session=launch.get("call_session"),
        )
    pause = raw.get("pause_until")
    return state, _dt(pause) if pause else None


def save_state(state: WatcherState, pause_until: datetime | None) -> None:
    launch = state.active_launch
    payload = {
        "version": 1,
        "handled": {key: until.isoformat() for key, until in state.handled.items()},
        "zoom_session_id": state.zoom_session_id,
        "zoom_needs_decision": state.zoom_needs_decision,
        "zoom_decided_session": state.zoom_decided_session,
        "zoom_decided_at": state.zoom_decided_at.isoformat() if state.zoom_decided_at else None,
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
            "zoom_session_id": launch.zoom_session_id,
            "missing_since": launch.missing_since.isoformat() if launch.missing_since else None,
            "failed": launch.failed,
            "call_session": launch.call_session,
        },
    }
    write_json(state_path(), payload, skip_unchanged=True)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()  # this PC's local time, with the daylight-saving rule for that date
    return parsed
