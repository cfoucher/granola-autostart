import ctypes
import json
import urllib.parse
from types import SimpleNamespace

import pytest

from granola_autostart import granola_launch
from granola_autostart.granola_launch import build_url, launch
from granola_autostart.granola_launch import shell_execute as real_shell_execute


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


def test_a_real_launch_hands_the_link_to_windows(monkeypatch):
    opened = []
    monkeypatch.setattr(granola_launch, "shell_execute", opened.append)
    launch(build_url(None), dry_run=False)
    assert opened == [build_url(None)]


def shell(monkeypatch, result) -> list[tuple]:
    """ShellExecuteW, answering ``result``: above 32 opened, 32 and below a Windows error."""
    calls = []
    shell32 = SimpleNamespace(ShellExecuteW=lambda *args: calls.append(args) or result)
    monkeypatch.setattr(granola_launch, "ctypes", SimpleNamespace(windll=SimpleNamespace(shell32=shell32), c_int=ctypes.c_int, c_void_p=ctypes.c_void_p))
    return calls


def test_windows_opens_the_link_or_says_why_it_could_not(monkeypatch):
    url = build_url(None)
    calls = shell(monkeypatch, 42)
    real_shell_execute(url)
    assert calls == [(None, "open", url, None, None, 1)]
    for result, code in ((31, 31), (2, 2), (None, 0)):  # 31: nothing handles granola://, as without Granola
        shell(monkeypatch, result)
        with pytest.raises(OSError, match=rf"\(ShellExecute error {code}\)"):
            real_shell_execute(url)
