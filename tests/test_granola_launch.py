import json
import urllib.parse

import pytest

from granola_autostart.granola_launch import build_url, launch


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


def test_awkward_titles_survive_the_double_encoding():
    event = {
        "id": "x",
        "summary": "Q&A #3 + café = 100%?\nnext",
        "start": {"dateTime": "2026-10-28T10:00:00-07:00"},
        "end": {"dateTime": "2026-10-28T10:30:00-07:00"},
    }
    params = decoded_params(build_url(event))
    assert json.loads(params["event"]) == event
    assert set(params) == {"creation_source", "auto_transcribe", "event"}


def test_only_granola_links_are_opened():
    with pytest.raises(ValueError):
        launch("https://example.com/", dry_run=False)
    launch(build_url(None), dry_run=True)  # logged, never opened
