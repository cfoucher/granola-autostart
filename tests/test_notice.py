import logging
import sys

import pytest

import granola_autostart
import granola_autostart.__main__ as entry
from granola_autostart import notice, settings, watcher


@pytest.fixture
def app_log():
    """A logger without handlers, so setup_logging adds its own; they're removed and closed afterwards."""
    logger = logging.getLogger("granola_autostart")
    kept, level = logger.handlers[:], logger.level
    for handler in kept:
        logger.removeHandler(handler)
    yield
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    for handler in kept:
        logger.addHandler(handler)
    logger.setLevel(level)


def broken_card(*_args, **_kwargs):
    raise RuntimeError("no display")


def run(monkeypatch, *argv: str) -> int:
    monkeypatch.setattr(sys, "argv", ["GranolaAutostart.exe", *argv])
    return entry.main()


def test_a_card_that_fails_exits_2_and_says_why_in_its_own_log(monkeypatch, tmp_path, app_log):
    monkeypatch.setattr(notice, "show_notice", broken_card)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2
    text = (tmp_path / "notice-log.txt").read_text(encoding="utf-8")
    assert "the countdown card failed" in text and "RuntimeError: no display" in text


def test_a_card_that_fails_exits_2_even_when_it_cannot_log(monkeypatch):
    def no_log(_name):
        raise OSError("disk full")

    monkeypatch.setattr(notice, "show_notice", broken_card)
    monkeypatch.setattr(settings, "setup_logging", no_log)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2


def test_a_card_that_cannot_even_load_is_not_a_cancel_and_says_why(monkeypatch, tmp_path, app_log):
    monkeypatch.delattr(granola_autostart, "notice", raising=False)
    monkeypatch.setitem(sys.modules, "granola_autostart.notice", None)
    assert run(monkeypatch, "--notice", "--title=Standup") == 2
    assert "the countdown card failed" in (tmp_path / "notice-log.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize(("started", "exit_code"), [(True, 0), (False, 1)])
def test_the_exit_code_tells_the_watcher_whether_to_start_granola(started, exit_code, monkeypatch):
    shown = []
    monkeypatch.setattr(notice, "show_notice", lambda title, seconds, *, kind: shown.append((title, seconds, kind)) or started)
    assert run(monkeypatch, "--notice", "--title=-- standup", "--seconds=7", "--kind=crash") == exit_code
    assert shown == [("-- standup", 7, "crash")]


def test_a_watcher_that_crashes_says_why_in_its_log_and_exits_for_the_supervisor(monkeypatch, tmp_path, app_log):
    def broken_tray():
        raise RuntimeError("tray icon failed")

    monkeypatch.setattr(watcher, "main", broken_tray)
    assert run(monkeypatch) == 1
    text = (tmp_path / "log.txt").read_text(encoding="utf-8")
    assert "the watcher stopped" in text and "RuntimeError: tray icon failed" in text
