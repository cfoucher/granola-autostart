from types import SimpleNamespace

from granola_autostart import presence


class User32:
    """The session's Win32 answers, from fields instead of the real desktop."""

    def __init__(self, desktop: int = 0, last_input: int | None = None):
        self.desktop = desktop
        self.last_input = last_input
        self.closed = []

    def OpenInputDesktop(self, flags, inherit, access):
        return self.desktop

    def CloseDesktop(self, desktop):
        self.closed.append(desktop)
        return True

    def GetLastInputInfo(self, info):
        if self.last_input is None:
            return False
        info._obj.dwTime = self.last_input  # byref's target, the LASTINPUTINFO
        return True


def ticks(monkeypatch, now: int) -> None:
    monkeypatch.setattr(presence, "kernel32", SimpleNamespace(GetTickCount=lambda: now))


def test_a_locked_session_has_no_input_desktop_and_an_unlocked_one_closes_it(monkeypatch):
    user32 = User32(desktop=0)
    monkeypatch.setattr(presence, "user32", user32)
    assert presence.is_unlocked() is False
    user32.desktop = 0x1234
    assert presence.is_unlocked() is True
    assert user32.closed == [0x1234]  # checked every tick all day, so the handle must not leak


def test_idle_time_survives_the_tick_count_wrapping_after_49_days(monkeypatch):
    monkeypatch.setattr(presence, "user32", User32(last_input=0xFFFFF000))
    ticks(monkeypatch, 5000)
    assert presence.idle_seconds() == (0x1000 + 5000) / 1000


def test_without_the_last_input_time_nobody_counts_as_idle(monkeypatch):
    monkeypatch.setattr(presence, "user32", User32(last_input=None))
    assert presence.idle_seconds() == 0.0


def test_present_means_unlocked_and_active_within_the_limit(monkeypatch):
    user32 = User32(desktop=1, last_input=1000)
    monkeypatch.setattr(presence, "user32", user32)
    ticks(monkeypatch, 61_000)
    assert presence.is_present(60)
    assert not presence.is_present(59)
    user32.desktop = 0
    assert not presence.is_present(600)
