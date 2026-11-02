"""Open a Granola note and start transcription without joining the video call."""

from __future__ import annotations

import ctypes
import json
import logging
import urllib.parse
from ctypes import wintypes

log = logging.getLogger("granola_autostart")


def build_url(event: dict | None) -> str:
    """Build granola://new-document.

    Granola reads the custom-protocol query with URL.searchParams and then calls
    decodeURIComponent on each value. The event JSON is encoded twice so a title
    containing ``%`` still arrives intact. ``auto_transcribe=1`` starts capture.
    The link does not join the call, whichever app it is on.
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
