"""Persist which meetings were cancelled or started, and an in-progress launch."""

from __future__ import annotations

import json
import logging
from datetime import datetime

from .decide import ActiveLaunch, WatcherState
from .settings import CALL_PROVIDERS, state_path, write_json

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
    decided_at = _either(raw, "call_decided_at", "zoom_decided_at")
    state = WatcherState(
        handled={str(key): _dt(value) for key, value in (raw.get("handled") or {}).items()},
        call_session_id=_session(_either(raw, "call_session_id", "zoom_session_id")),
        call_needs_decision=bool(_either(raw, "call_needs_decision", "zoom_needs_decision")),
        call_decided_session=_session(_either(raw, "call_decided_session", "zoom_decided_session")),
        call_decided_at=_dt(decided_at) if decided_at else None,
        calls_under_way=[str(session) for session in raw.get("calls_under_way") or () if session],
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
            call_session_id=_session(_either(launch, "call_session_id", "zoom_session_id")),
            missing_since=_dt(missing) if missing else None,
            failed=bool(launch.get("failed")),
            call_session=_session(launch.get("call_session")),
            overrunning=bool(launch.get("overrunning")),
        )
    pause = raw.get("pause_until")
    return state, _dt(pause) if pause else None


def _either(raw: dict, name: str, old_name: str):
    """A value saved under its current name, or under the name it had before calls covered Teams and Meet."""
    return raw[name] if name in raw else raw.get(old_name)


def _session(value) -> str | None:
    """A call's session id. One saved before Teams and Meet has no provider in front: it was Zoom's, so an
    upgrade in the middle of a call still recognises it."""
    if not value:
        return None
    text = str(value)
    return text if text.split(":", 1)[0] in CALL_PROVIDERS else f"zoom:{text}"


def save_state(state: WatcherState, pause_until: datetime | None) -> None:
    launch = state.active_launch
    payload = {
        "version": 1,
        "handled": {key: until.isoformat() for key, until in state.handled.items()},
        "call_session_id": state.call_session_id,
        "call_needs_decision": state.call_needs_decision,
        "call_decided_session": state.call_decided_session,
        "call_decided_at": state.call_decided_at.isoformat() if state.call_decided_at else None,
        "calls_under_way": list(state.calls_under_way),
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
            "call_session_id": launch.call_session_id,
            "missing_since": launch.missing_since.isoformat() if launch.missing_since else None,
            "failed": launch.failed,
            "call_session": launch.call_session,
            "overrunning": launch.overrunning,
        },
    }
    write_json(state_path(), payload, skip_unchanged=True)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.astimezone()  # this PC's local time, with the daylight-saving rule for that date
    return parsed
