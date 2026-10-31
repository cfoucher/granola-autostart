import threading
import time

import pytest

from granola_autostart import commands

PATIENCE = 10  # seconds; generous for a slow CI runner, and spent only when something is wrong


@pytest.fixture(autouse=True)
def quick_waits(monkeypatch):
    monkeypatch.setattr(commands, "WAIT_MS", 50)


def listening(handlers):
    stop = threading.Event()
    listener = threading.Thread(target=commands.listen, args=(handlers, stop))
    listener.start()
    return stop, listener


def send_when_listening(command: str) -> bool:
    """Send once the listener has created that command's event, which it does on its own thread."""
    deadline = time.monotonic() + PATIENCE
    while not commands.send(command):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.02)
    return True


def test_settings_reaches_the_watcher_only_while_it_listens():
    assert commands.send("pause-hour") is False
    received = []
    heard = threading.Event()

    def handler(name):
        def run():
            received.append(name)
            heard.set()

        return run

    stop, listener = listening({name: handler(name) for name in commands.COMMANDS})
    try:
        assert send_when_listening("resume")
        assert heard.wait(PATIENCE)
        assert received == ["resume"]
    finally:
        stop.set()
        listener.join(timeout=PATIENCE)
    assert not listener.is_alive()
    assert commands.send("resume") is False


def test_a_failing_command_does_not_stop_the_listener():
    heard = threading.Event()

    def broken():
        raise RuntimeError("pause failed")

    stop, listener = listening({"pause-hour": broken, "resume": heard.set})
    try:
        assert send_when_listening("pause-hour")
        assert send_when_listening("resume")
        assert heard.wait(PATIENCE)
    finally:
        stop.set()
        listener.join(timeout=PATIENCE)


def test_only_known_commands_can_be_sent():
    with pytest.raises(ValueError):
        commands.send("delete-everything")


class FakeKernel32:
    """Events numbered 1, 2, ... except the one named ``refuse``; every wait returns ``wait_result``."""

    def __init__(self, *, refuse: str = "", wait_result: int = commands.WAIT_FAILED):
        self.refuse, self.wait_result = refuse, wait_result
        self.created, self.closed, self.waits = [], [], 0

    def CreateEventW(self, _security, _manual_reset, _initial_state, name):
        if name == commands.PREFIX + self.refuse:
            return 0
        self.created.append(name)
        return len(self.created)

    def WaitForMultipleObjects(self, _count, _handles, _wait_all, _milliseconds):
        self.waits += 1
        return self.wait_result

    def CloseHandle(self, handle):
        self.closed.append(handle)


def listen_until_it_returns(kernel32, monkeypatch) -> bool:
    """Whether listen returned on its own within two seconds."""
    monkeypatch.setattr(commands, "_kernel32", lambda: kernel32)
    stop, listener = listening({name: lambda: None for name in commands.COMMANDS})
    listener.join(timeout=2)
    returned = not listener.is_alive()
    stop.set()
    listener.join(timeout=3)
    return returned


def test_the_listener_gives_up_when_its_events_cannot_be_created(monkeypatch, caplog):
    kernel32 = FakeKernel32(refuse="quit")
    assert listen_until_it_returns(kernel32, monkeypatch)
    assert kernel32.waits == 0
    assert kernel32.closed == [1, 2, 3]
    assert "its events could not be created" in caplog.text


def test_the_listener_stops_when_waiting_fails(monkeypatch, caplog):
    kernel32 = FakeKernel32(wait_result=commands.WAIT_FAILED)
    assert listen_until_it_returns(kernel32, monkeypatch)
    assert kernel32.waits == 1
    assert kernel32.closed == [1, 2, 3, 4]
    assert "the wait failed" in caplog.text
