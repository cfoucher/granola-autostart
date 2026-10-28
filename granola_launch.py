"""Open a Granola note and start transcription without joining the video call."""

from __future__ import annotations

import ctypes
import json
import logging
import urllib.parse
from ctypes import wintypes
from datetime import timedelta

log = logging.getLogger("granola_autostart")

GENERIC_ZOOM_TITLES = {
    "zoom",
    "zoom meeting",
    "zoom workplace",
    "zoom webinar",
    "meeting chat",
}


def meeting_title(zoom_title: str | None) -> str:
    cleaned = (zoom_title or "").strip()
    if not cleaned or cleaned.casefold() in GENERIC_ZOOM_TITLES:
        return "Zoom meeting"
    return cleaned[:200]


def adhoc_payload(title: str, session_id: str, now) -> dict:
    end = now + timedelta(hours=1)
    safe_id = session_id.replace(":", "-")
    return {
        "id": f"zoom-{safe_id}",
        "summary": title,
        "start": {"dateTime": now.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def build_url(event: dict | None) -> str:
    """Build granola://new-document.

    Granola reads the custom-protocol query with URL.searchParams and then calls
    decodeURIComponent on each value. The event JSON is encoded twice so a title
    containing ``%`` still arrives intact. ``auto_transcribe=1`` starts capture.
    The link does not open Zoom.
    """
    parts = ["creation_source=autostart", "auto_transcribe=1"]
    if event is not None:
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        once = urllib.parse.quote(payload, safe="")
        parts.append("event=" + urllib.parse.quote(once, safe=""))
    return "granola://new-document?" + "&".join(parts)


def launch(url: str, *, dry_run: bool) -> None:
    if not url.startswith("granola://new-document?"):
        raise ValueError("refusing to open a non-Granola link")
    if dry_run:
        log.info("dry-run launch %s", url)
        return
    log.info("launching Granola")
    log.debug("launch url %s", url)
    shell_execute(url)


def shell_execute(url: str) -> None:
    shell = ctypes.windll.shell32.ShellExecuteW
    shell.argtypes = [
        wintypes.HWND,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.c_int,
    ]
    shell.restype = ctypes.c_void_p
    result = shell(None, "open", url, None, None, 1)
    code = 0 if result is None else int(result)
    if code <= 32:
        raise OSError(f"Could not open Granola (ShellExecute error {code})")
