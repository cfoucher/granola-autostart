import json
import urllib.parse
from datetime import datetime, timedelta, timezone

from granola_launch import adhoc_payload, build_url, meeting_title


def decoded_params(url: str) -> dict:
    """Mirror Granola's protocol parser: URLSearchParams, then decodeURIComponent."""
    parsed = urllib.parse.urlsplit(url)
    assert parsed.scheme == "granola"
    assert parsed.netloc == "new-document"
    params = {}
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if value == "true":
            params[key] = True
        elif value == "false":
            params[key] = False
        else:
            params[key] = urllib.parse.unquote(value)
    return params


def test_launch_url_starts_transcription_without_opening_zoom():
    event = {
        "id": "abc123",
        "summary": "100% done",
        "start": {"dateTime": "2026-10-28T10:00:00-07:00"},
        "end": {"dateTime": "2026-10-28T10:30:00-07:00"},
    }
    url = build_url(event)
    assert "zoom.us" not in url
    assert "zoommtg" not in url
    params = decoded_params(url)
    assert params["creation_source"] == "autostart"
    assert params["auto_transcribe"] == "1"
    assert json.loads(params["event"]) == event


def test_adhoc_zoom_title_ignores_the_generic_window_name():
    assert meeting_title("Zoom Meeting") == "Zoom meeting"
    assert meeting_title("Roadmap") == "Roadmap"
    now = datetime(2026, 10, 28, 17, 0, tzinfo=timezone.utc)
    payload = adhoc_payload("Roadmap", "10:20", now)
    assert payload["summary"] == "Roadmap"
    assert payload["id"] == "zoom-10-20"
    assert payload["end"]["dateTime"] == (now + timedelta(hours=1)).isoformat()
