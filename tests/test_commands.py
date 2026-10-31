import threading

import pytest

import commands


@pytest.fixture(autouse=True)
def private_names(monkeypatch):
    # Never reach a real watcher running on this PC.
    monkeypatch.setattr(commands, "PREFIX", "Local\\GranolaAutostartCommandsTest-")


def test_settings_reaches_the_watcher_only_while_it_listens():
    assert commands.send("pause-hour") is False
    received = []
    heard = threading.Event()
    stop = threading.Event()

    def handler(name):
        def run():
            received.append(name)
            heard.set()

        return run

    listener = threading.Thread(target=commands.listen, args=({name: handler(name) for name in commands.COMMANDS}, stop))
    listener.start()
    try:
        assert any(commands.send("resume") or stop.wait(0.05) for _ in range(40))
        assert heard.wait(2)
        assert received == ["resume"]
    finally:
        stop.set()
        listener.join(timeout=3)
    assert commands.send("resume") is False


def test_only_known_commands_can_be_sent():
    with pytest.raises(ValueError):
        commands.send("delete-everything")
