"""The watcher: each tick it checks presence, calls, and the calendars, then shows the countdown card as a meeting
is about to start, so that Granola opens about when it begins. While Autostart notes is off it checks none of that.
It also runs the tray icon and the command line for the other modes."""

from __future__ import annotations

import argparse
import ctypes
import dataclasses
import logging
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

import pystray

from . import __version__, commands, granola_window, processes, updates
from .calendar_cache import CalendarCache, load_feeds
from .calendar_feed import merge_events
from .calls import Call, CallWatch, granola_running, process_table
from .decide import Prompt, Snapshot, apply_result, evaluate, recorded_choice
from .granola_launch import build_url, launch
from .icon import draw_icon
from .notify import Notifier, register_app_id
from .presence import idle_seconds, is_present, is_unlocked
from .settings import (
    CALL_PROVIDERS,
    ConfigError,
    Settings,
    app_dir,
    config_path,
    load_or_defaults,
    load_settings,
    saved_flag,
    setup_logging,
    status_path,
    sweep_stale_temporaries,
    update_config,
)
from .state_store import load_state, save_state
from .status import (
    AlertBook,
    LastStart,
    Problem,
    Status,
    TrayView,
    describe_error,
    end_of_today,
    feed_id,
    find_problems,
    launch_problem_key,
    next_meeting,
    read_status,
    taking_notes,
    tray_view,
    write_status,
)
from .winui import per_monitor_dpi

log = logging.getLogger("granola_autostart")
LAUNCH_CHECK_SECONDS = 30
CARD_POLL_SECONDS = 1.0  # how often Granola is looked at while the countdown card is up
CARD_END_SECONDS = 5  # how long a card told to close is given before it is killed
# SetPreferredAppMode's modes for each Appearance: AllowDark follows Windows, then ForceDark and ForceLight.
MENU_MODES = {"auto": 1, "dark": 2, "light": 3}


class Control:
    """Shared by the watcher loop and the tray: the pause, the current settings, and what the tray shows."""

    def __init__(self, settings: Settings | None = None, config_error: str | None = None) -> None:
        self.stop = threading.Event()
        self.wake = threading.Event()
        self._lock = threading.Lock()
        self.pause_until: datetime | None = None
        self.settings = settings or Settings()
        self.config_error = config_error
        self.force_dry_run = False
        self.last_start: LastStart | None = None
        self.card_failed = False  # the last countdown card couldn't run
        self.card_open = False  # a countdown card is on screen, and the watcher is waiting on it
        self.installing: str | None = None  # the version being installed
        self.view = TrayView("watching", "Watching for meetings", False)
        self.on_view: Callable[[TrayView], None] | None = None

    def set_pause(self, until: datetime | None) -> None:
        with self._lock:
            self.pause_until = until
        self.wake.set()

    def current_pause(self) -> datetime | None:
        with self._lock:
            return self.pause_until

    def paused(self, now: datetime) -> bool:
        until = self.current_pause()
        return until is not None and now < until

    def set_notes(self, on: bool) -> None:
        """Turn Autostart notes on or off in memory, at once. Writing config.json is the caller's."""
        with self._lock:
            self.settings.autostart_notes = on
        self.wake.set()

    def set_installing(self, version: str | None) -> None:
        with self._lock:
            self.installing = version
        self.wake.set()

    def set_card(self, showing: bool) -> None:
        """The watcher waits on a countdown card and ticks nothing meanwhile, but the tray menu is only built
        when its view changes, so it is rebuilt now. That is how Install update… follows the card."""
        with self._lock:
            self.card_open = showing
            view, callback = self.view, self.on_view
        if callback is not None:
            try:
                callback(view)
            except Exception:
                log.exception("could not rebuild the tray menu")

    def halt(self) -> None:
        self.stop.set()
        self.wake.set()

    def sleep(self, seconds: float) -> None:
        """Wait for the next tick, or less when the tray changed something."""
        self.wake.wait(seconds)
        self.wake.clear()

    def record_start(self, title: str, at: datetime) -> None:
        with self._lock:
            self.last_start = LastStart(title, at)

    def note_launch(self, launch) -> LastStart | None:
        """Mark the last start as opened once Granola is seen, or as failed once the launch check gave up."""
        with self._lock:
            last = self.last_start
            if last is not None and launch is not None and launch.launched_at == last.at:
                if launch.did_not_open:
                    last.granola_opened = False
                elif launch.opened:
                    last.granola_opened = True
            return dataclasses.replace(last) if last else None

    def show(self, view: TrayView) -> None:
        with self._lock:
            changed = view != self.view
            self.view = view
            callback = self.on_view
        if changed and callback is not None:
            callback(view)


class CardAnswer(NamedTuple):
    """What the countdown card said. ``accepted`` is True when the countdown ran out or Start now was clicked, False on
    Cancel, and None when the card couldn't run. ``picked`` is the meeting to start, as its place in the card's list: 0 is
    the first, and the only one when the card listed none. ``granola`` is True when the card was closed because Granola
    started taking notes for ``picked`` itself during the countdown, which is no answer of the card's own."""

    accepted: bool | None
    picked: int = 0
    granola: bool = False


def ask_notice(
    title: str,
    seconds: int,
    kind: str,
    choices: Sequence[tuple[str, str]] = (),
    granola_has: Callable[[], int | None] | None = None,
) -> CardAnswer:
    """Show the card, listing the meetings in ``choices`` (title and line each) when there are any.

    About once a second ``granola_has`` is asked whether Granola is already taking notes for one of the meetings (its
    place in the list, or None). If so, the card is closed and the answer says so."""
    try:
        card = subprocess.Popen(processes.notice_command(title, seconds, kind, choices))
    except Exception:
        log.exception("countdown window failed to open")
        return CardAnswer(None)
    deadline = time.monotonic() + max(1, seconds) + 60
    while True:
        try:
            code = card.wait(timeout=CARD_POLL_SECONDS)
            break
        except subprocess.TimeoutExpired:
            pass
        if time.monotonic() >= deadline:
            _end_card(card, kill=True)
            log.error("countdown window did not close")
            return CardAnswer(None)
        place = granola_has() if granola_has is not None else None
        if place is not None:
            _end_card(card)
            log.info("Granola started notes for %s during the countdown; closed the card", choices[place][0] if choices else title)
            return CardAnswer(True, place, granola=True)
    if code in (0, 1):
        return CardAnswer(code == 0)
    place = code - processes.CHOICE_EXIT_CODE
    if 0 < place < len(choices):
        return CardAnswer(True, place)
    log.error("countdown window exited %s", code)
    return CardAnswer(None)


def _end_card(card: subprocess.Popen, *, kill: bool = False) -> None:
    """Close the card's process, and wait for it. One that doesn't close when told to is killed."""
    if kill:
        card.kill()
    else:
        card.terminate()
    try:
        card.wait(timeout=CARD_END_SECONDS)
    except subprocess.TimeoutExpired:
        card.kill()
        card.wait()


def config_stamp() -> tuple[int, int] | None:
    try:
        stat = config_path().stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def reload_settings(control: Control, current: Settings, cache: CalendarCache) -> Settings:
    """Pick up a saved config.json. A broken file keeps the last settings that worked."""
    try:
        fresh = load_settings()
    except ConfigError as error:
        log.error("%s; keeping the previous settings until it is fixed", error)
        control.config_error = str(error)
        return current
    control.config_error = None
    fresh.dry_run = fresh.dry_run or control.force_dry_run
    # Anything that changes which meetings count: a calendar's address, switch or declined addresses, the old
    # extra address, or the skip words.
    if (fresh.distinct_calendars(), fresh.user_email, fresh.ignore_title_substrings) != (
        current.distinct_calendars(),
        current.user_email,
        current.ignore_title_substrings,
    ):
        cache.invalidate(fresh.enabled_urls())
    control.settings = fresh
    if fresh.appearance != current.appearance:
        menu_theme(fresh.appearance)
    log.info("settings reloaded")
    return fresh


class Reporter:
    """Each tick: write status.json, turn new problems into notifications, and update the tray."""

    def __init__(self, control: Control, notifier) -> None:
        self.control = control
        self.notifier = notifier
        self.book = AlertBook()

    def tick(self, now: datetime, settings: Settings, cache: CalendarCache, events, launch) -> None:
        control = self.control
        feeds = cache.feed_statuses(settings.distinct_calendars())
        # Once a start's meeting is over, "Granola didn't open" has nothing left to fix (Try again won't act).
        current = launch if launch is not None and now < launch.expires_at else None
        problems = find_problems(control.config_error, feeds, current, card_failed=control.card_failed)
        for problem in self.book.fresh(problems):
            self.notifier.show(problem)
        upcoming = next_meeting(events, now)
        pause = control.current_pause()
        update = updates.read_state().available() if updates.enabled() else None
        off = not settings.autostart_notes  # the loop's copy, which the tray's switch changes at once: what this tick did
        control.show(
            tray_view(
                now,
                pause,
                problems,
                upcoming,
                update=update.version if update else None,
                installing=control.installing,
                off=off,
            )
        )
        write_status(
            status_path(),
            Status(
                version=__version__,
                off=off,
                paused_until=pause if pause is not None and now < pause else None,
                next_meeting=(upcoming.summary, upcoming.start) if upcoming else None,
                taking_notes=taking_notes(launch, now),
                feeds=feeds,
                last_start=control.note_launch(launch),
                problems=problems,
            ),
        )


def run_loop(settings: Settings, control: Control, state_lock: threading.Lock, state, reporter: Reporter) -> None:
    cache = CalendarCache()
    checker = updates.Checker()
    with state_lock:
        watch = CallWatch(state.call_ids(datetime.now().astimezone()))  # so a call still under way after a restart is the same call
    sweep_stale_temporaries()  # once, at start: an update stops the app mid-save now and then
    if not settings.enabled_urls():
        log.warning("No calendar feed is on. Calendar meetings will not start notes. Calls still will.")
    stamp = config_stamp()
    was_present = False
    was_off = False
    while not control.stop.is_set():
        try:
            now = datetime.now().astimezone()
            checker.tick(now)
            if config_stamp() != stamp:
                stamp = config_stamp()
                settings = reload_settings(control, settings, cache)
            # While off nothing is downloaded or asked: the decision runs as if paused with no call and no
            # events, so its state stays consistent. Whether Granola is running is cheap, and still checked.
            off = not settings.autostart_notes  # config.json as last reloaded, or the tray's switch, which is at once
            just_on = was_off and not off
            table = process_table()
            call = None if off else watch.current(settings.calls, table)
            present = is_present(settings.idle_seconds)
            events = [] if off else cache.current(now, settings, refresh=present, eager=present and (not was_present or just_on))
            was_present = present
            was_off = off
            running = granola_running(table)
            with state_lock:
                # Saved, for the next start (see CallWatch). A look that couldn't read the microphone leaves them as they were.
                under_way = [] if off else watch.under_way()
                if under_way is not None:
                    state.calls_under_way = under_way
                    state.calls_seen_at = now.replace(second=0, microsecond=0) if under_way else None
                prompt = evaluate(
                    now,
                    Snapshot(
                        present=present,
                        # The first tick back on is a pause too, with calls noticed: a call already under way is
                        # recorded as handled, as Pause does, so turning on doesn't ask about it.
                        paused=control.paused(now) or off or just_on,
                        call_session_id=call.session_id if call else None,
                        call_provider=call.provider if call else None,
                        call_age_seconds=call.age_seconds if call else None,
                        call_title=call.title if call else None,
                        events=events,
                        granola_running=running,
                        # Local time by the rules of the call's own start, which a clock change since may differ from.
                        call_started=call.started.astimezone() if call and call.started else None,
                    ),
                    state,
                    lookahead=timedelta(minutes=settings.zoom_lookahead_minutes),
                    fresh_call_seconds=settings.fresh_zoom_seconds,
                    skip_words=settings.ignore_title_substrings,
                    lead=timedelta(seconds=settings.countdown_seconds),
                )
                _warn_if_launch_missing(state, now)
                launch_now = dataclasses.replace(state.active_launch) if state.active_launch else None
                _save_state(state, control)
            try:
                reporter.tick(now, settings, cache, events, launch_now)
            except Exception:
                log.exception("could not report the status")
            if prompt is not None and not control.stop.is_set():
                handle_prompt(prompt, settings, control, state_lock, state)
        except Exception:
            log.exception("watcher tick failed")
        control.sleep(settings.poll_seconds)


def notes_off(control: Control) -> bool:
    """Autostart notes is off, whether the tray just turned it off or Settings saved it while a countdown card
    held the loop, which can't reload config.json until the card closes."""
    return not control.settings.autostart_notes or not saved_flag("autostart_notes", True)


def still_present(settings: Settings) -> bool:
    """The countdown itself adds idle time, so allow for it on top of the presence limit."""
    return is_unlocked() and idle_seconds() <= settings.idle_seconds + settings.countdown_seconds + 5


def noted_by_granola(prompt: Prompt) -> int | None:
    """Which of the prompt's meetings Granola is already taking notes for, if it is, for sure (see recorded_choice)."""
    return recorded_choice(prompt, granola_window.recording_title())


def note_granola_has(prompt: Prompt, index: int, control: Control, state_lock: threading.Lock, state) -> None:
    """Record that Granola is already taking notes for meeting ``index`` of the prompt, as a start that has opened. It
    opens nothing, and is no start of this app's own (General's Last start). It is a fact, not a start, so a dry run, a
    pause, Autostart notes being off, and presence don't change it."""
    chosen = prompt.picked(index)
    at = datetime.now().astimezone()
    with state_lock:
        apply_result(state, chosen, True, at)
        state.active_launch.seen_running = True
        _save_state(state, control)


def handle_prompt(prompt: Prompt, settings: Settings, control: Control, state_lock: threading.Lock, state) -> None:
    already = noted_by_granola(prompt)
    if already is not None:
        log.info("Granola is already taking notes for %s; no card", prompt.picked(already).title)
        if not control.stop.is_set():
            note_granola_has(prompt, already, control, state_lock, state)
        return
    if prompt.choices:
        log.info("countdown for %s (%s), %s meetings", prompt.title, prompt.kind, len(prompt.choices))
    else:
        log.info("countdown for %s (%s)", prompt.title, prompt.kind)
    now = datetime.now().astimezone()
    listed = [(choice.title, choice.line(now)) for choice in prompt.choices]
    # Held from before the card starts, which takes a moment, until it has closed: an update isn't installed
    # in between. The card holds it too while it is up (notice.main).
    processes.hold_mutex(processes.CARD_MUTEX, shared=True)
    control.set_card(True)
    try:
        answer = ask_notice(prompt.title, settings.countdown_seconds, prompt.kind, listed, granola_has=lambda: noted_by_granola(prompt))
    finally:
        processes.release_mutex(processes.CARD_MUTEX)  # first: the tray menu rebuilt next asks whether a card is up
        control.set_card(False)
    if answer.granola:
        control.card_failed = False
        if not control.stop.is_set():
            note_granola_has(prompt, answer.picked, control, state_lock, state)
        return
    control.card_failed = answer.accepted is None
    # A card that couldn't run counts as nobody answering, which starts notes, so a broken card never
    # quietly skips meetings. Not for restarting notes after Granola closed, though: the user may have
    # closed it on purpose, and without a card nothing would let them say no.
    accepted = answer.accepted if answer.accepted is not None else prompt.kind != "crash"
    # The meeting ticked on the card. A Cancel answers the prompt as it was, with every meeting it listed.
    chosen = prompt.picked(answer.picked) if accepted else prompt
    if answer.accepted is None:
        log.info("%s Granola for %s without the card", "starting" if accepted else "not restarting", chosen.title)
    if accepted and answer.picked:
        log.info("picked %s on the card", chosen.title)
    if control.stop.is_set():
        return
    if accepted and control.paused(datetime.now().astimezone()):
        log.info("paused during the countdown; not starting Granola for %s", chosen.title)
        return
    if accepted and notes_off(control):
        log.info("turned off during the countdown; not starting Granola for %s", chosen.title)
        return
    if accepted and not still_present(settings):
        log.info("left the computer during the countdown; will ask again for %s", chosen.title)
        return
    if answer.accepted is False:
        log.info("cancelled Granola for %s", prompt.title)
    if accepted:
        # Once more just before opening it, for a click on Granola's own button in the countdown's last second. A meeting
        # ticked on the card other than the first, the one ticked to start with, is the user's choice, so Granola's notes
        # for another meeting don't stand in for it.
        late = noted_by_granola(prompt)
        if late is not None and answer.picked in (0, late):
            log.info("Granola started notes for %s as the countdown ended; not opening it", prompt.picked(late).title)
            note_granola_has(prompt, late, control, state_lock, state)
            return
    opened = accepted and open_granola(chosen.payload, chosen.title, dry_run=settings.dry_run)
    at = datetime.now().astimezone()
    with state_lock:
        apply_result(state, chosen, accepted, at)
        if accepted and settings.dry_run:
            # Answered like a real start, but nothing opened, so there is nothing to watch for.
            state.active_launch = None
        elif accepted and not opened:
            # Kept as a start that failed, so "Granola didn't open" comes up at once.
            state.active_launch.failed = True
        _save_state(state, control)
    if accepted and not settings.dry_run:
        control.record_start(chosen.title, at)


def _save_state(state, control: Control) -> None:
    """Save state.json. A file another program holds open (antivirus, backup) is logged, never fatal:
    the next save catches up, and nothing that depends on this tick is skipped."""
    try:
        save_state(state, control.current_pause())
    except OSError:
        log.exception("could not save state.json")


def open_granola(payload: dict, title: str, *, dry_run: bool) -> bool:
    try:
        launch(build_url(payload), dry_run=dry_run)
    except Exception:
        log.exception("could not start Granola for %s", title)
        return False
    if not dry_run:
        log.info("started Granola for %s", title)
    return True


def _warn_if_launch_missing(state, now: datetime) -> None:
    launch = state.active_launch
    if launch is None or launch.seen_running or launch.warned or launch.failed:
        return
    if now < launch.launched_at + timedelta(seconds=LAUNCH_CHECK_SECONDS):
        return
    launch.warned = True
    log.warning("Granola was not running %ss after the launch for %s", LAUNCH_CHECK_SECONDS, launch.title)


def retry_launch(state_lock: threading.Lock, state, control: Control, problem_key: str, *, dry_run: bool) -> None:
    """The notification's Try again. It does nothing for an older notification, a second click, a start
    that has opened since, or a meeting that has ended.

    Trying again is a new start: its own 30-second check, its own line in General, and its own
    "Granola didn't open" if the link fails again. In a dry run it is answered like one, with nothing to watch.
    """
    at = datetime.now().astimezone()
    with state_lock:
        launch = state.active_launch
        if launch is None or launch_problem_key(launch) != problem_key or not launch.did_not_open or at >= launch.expires_at:
            log.info("Try again was for a start that has moved on; nothing to retry")
            return
        if dry_run:
            state.active_launch = None
        else:
            launch.failed = launch.warned = launch.seen_running = False
            launch.missing_since = None
            launch.launched_at = at
    if not dry_run:
        control.record_start(launch.title, at)
    log.info("trying Granola again from the notification")
    if not open_granola(launch.payload, launch.title, dry_run=dry_run):
        with state_lock:
            if state.active_launch is launch:
                launch.failed = True


def start_install(control: Control) -> threading.Thread | None:
    """The tray's Install update…, on its own thread because the download takes a while.

    Once the installer has started, it stops this app and restarts it on the new version, so the menu keeps
    saying it is installing until then (and for updates.INSTALL_WAIT_SECONDS if the installer never does). If it
    failed, Settings opens at General, which says why. A countdown card that came up during the download, or
    another click already installing, isn't a failure: the menu just offers Install update… again.
    Returns the thread, or None when there is nothing to do.
    """
    release = updates.read_state().available()
    if release is None or control.installing is not None or control.card_open:
        return None
    control.set_installing(release.version)

    def run() -> None:
        outcome = updates.Install.FAILED
        try:
            outcome = updates.install(release)
        except Exception:
            log.exception("could not install version %s", release.version)
        if outcome is updates.Install.STARTED:
            control.stop.wait(updates.INSTALL_WAIT_SECONDS)
        control.set_installing(None)
        if outcome is updates.Install.FAILED:
            processes.open_settings("general")

    thread = threading.Thread(target=run, name="update-install", daemon=True)
    thread.start()
    return thread


def save_app_icon() -> Path:
    """The icons Windows shows on notifications (PNG) and on windows run from source (ICO)."""
    folder = app_dir()
    folder.mkdir(parents=True, exist_ok=True)
    image = draw_icon(64)
    image.save(folder / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64)])
    image.save(folder / "app.png")
    return folder / "app.png"


def modern_menus(appearance: str = "auto") -> None:
    """A crisp Windows 11 tray menu that follows the Appearance choice, or the Windows app theme on Auto.

    Without DPI awareness Windows draws the menu at 100% and stretches it on scaled displays.
    """
    per_monitor_dpi()
    menu_theme(appearance)


def menu_theme(appearance: str) -> None:
    """Light, dark, or Windows' own theme for the tray menu, from now on.

    SetPreferredAppMode and FlushMenuThemes (uxtheme ordinals 135 and 136) are undocumented but
    stable since Windows 10 1903; without them menus stay light.
    """
    try:
        uxtheme = ctypes.WinDLL("uxtheme")
        set_preferred_app_mode = uxtheme[135]
        set_preferred_app_mode.argtypes = [ctypes.c_int]
        set_preferred_app_mode(MENU_MODES.get(appearance, MENU_MODES["auto"]))
        uxtheme[136]()  # FlushMenuThemes
    except (AttributeError, OSError):
        pass


def run_tray(settings: Settings, config_error: str | None = None, *, dry_run: bool = False) -> int:
    """Run until Quit; returns the exit code. Settings can pause, resume, and quit through commands.py."""
    modern_menus(settings.appearance)
    control = Control(settings, config_error)
    control.force_dry_run = dry_run
    control.last_start = read_status(status_path()).last_start
    logo = None
    try:
        logo = save_app_icon()
        register_app_id(logo)
    except OSError as error:
        log.warning("could not register for notifications (%s)", type(error).__name__)
    state_lock = threading.Lock()
    state, saved_pause = load_state()
    if saved_pause is not None:
        control.set_pause(saved_pause)
    tray: dict[str, pystray.Icon] = {}

    def balloon(title: str, message: str) -> None:
        if "icon" in tray:
            tray["icon"].notify(message, title)

    def on_notification(action: str, problem: Problem) -> None:
        if action == "retry":
            retry_launch(state_lock, state, control, problem.key, dry_run=control.settings.dry_run)
        elif action == "logs":
            processes.open_log_folder()
        elif action == "config":
            processes.open_config_file()
        elif action in ("settings", "open"):
            processes.open_settings(problem.section)

    reporter = Reporter(control, Notifier(on_notification, balloon, logo=logo))
    worker = threading.Thread(
        target=run_loop,
        args=(settings, control, state_lock, state, reporter),
        name="granola-autostart",
        daemon=False,
    )
    worker.start()
    if not settings.calendar_urls() and not settings.setup_complete and config_error is None:
        processes.open_setup()

    def pause_until(until: datetime | None, label: str) -> None:
        control.set_pause(until)
        with state_lock:
            _save_state(state, control)
        log.info(label)

    def pause_hour() -> None:
        until = datetime.now().astimezone() + timedelta(hours=1)
        pause_until(until, f"Paused until {until:%H:%M}")

    def pause_today() -> None:
        pause_until(end_of_today(datetime.now().astimezone()), "Paused until tomorrow")

    def resume() -> None:
        pause_until(None, "Resumed")

    def switch_notes(on: bool) -> None:
        """Turn Autostart notes on or off. config.json gets only that key: save_settings would write the
        watcher's whole copy, which may not have caught up with a change made in Settings a moment ago."""
        control.set_notes(on)
        state = "on" if on else "off"
        try:
            update_config("autostart_notes", on)
        except (OSError, ConfigError):
            log.exception("could not save Autostart notes to config.json; it is %s until config.json changes or this app restarts", state)
            return
        log.info("Autostart notes turned %s", state)

    quitting = threading.Event()

    def quit_app() -> None:
        log.info("quitting")
        quitting.set()
        control.halt()
        tray["icon"].stop()

    def refresh(view: TrayView) -> None:
        icon = tray["icon"]
        icon.icon = draw_icon(64, view.kind, badge=view.update is not None)
        icon.title = view.tooltip
        icon.update_menu()

    def ready(icon: pystray.Icon) -> None:
        # Settings' Quit must work even if showing the icon fails.
        handlers = {"pause-hour": pause_hour, "pause-today": pause_today, "resume": resume, "quit": quit_app}
        threading.Thread(target=commands.listen, args=(handlers, control.stop), name="settings-commands", daemon=True).start()
        icon.visible = True
        control.on_view = refresh
        refresh(control.view)

    def card_up() -> bool:
        return control.card_open or processes.card_showing()

    def is_off() -> bool:
        # The switch itself, which Turn off changes at once, and not the view, which the next tick redraws.
        return not control.settings.autostart_notes

    # Left-clicking the icon runs the first default item: Fix in Settings... when something is wrong, else Settings...
    menu = pystray.Menu(
        pystray.MenuItem(lambda _item: control.view.header, lambda *_: None, enabled=False),
        pystray.MenuItem(
            lambda _item: "Install update after the countdown" if card_up() else "Install update…",
            lambda *_: start_install(control),
            enabled=lambda _item: control.installing is None and not card_up(),
            visible=lambda _item: control.view.update is not None,
        ),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Fix in Settings…",
            lambda *_: processes.open_settings(control.view.problem_section or "calendars"),
            default=True,
            visible=lambda _item: control.view.kind == "attention",
        ),
        pystray.MenuItem("Resume", lambda *_: resume(), visible=lambda _item: control.view.paused),
        pystray.MenuItem(
            "Pause for 1 hour", lambda *_: pause_hour(), visible=lambda _item: not control.view.paused and not is_off()
        ),
        pystray.MenuItem("Pause for today", lambda *_: pause_today(), visible=lambda _item: not is_off()),
        pystray.MenuItem("Turn off", lambda *_: switch_notes(False), visible=lambda _item: not is_off()),
        pystray.MenuItem("Turn on", lambda *_: switch_notes(True), visible=lambda _item: is_off()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            "Settings…",
            lambda *_: processes.open_settings("calendars"),
            default=True,
            visible=lambda _item: control.view.kind != "attention",
        ),
        pystray.MenuItem("Open log folder", lambda *_: processes.open_log_folder()),
        pystray.MenuItem("About", lambda *_: processes.open_settings("general")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda *_: quit_app()),
    )
    try:
        tray["icon"] = pystray.Icon(
            "granola-autostart", draw_icon(64, control.view.kind, badge=control.view.update is not None), control.view.tooltip, menu=menu
        )
        tray["icon"].run(setup=ready)
    finally:
        # Even when the tray fails, the loop stops, so the process exits and the supervisor starts it again.
        control.halt()
        worker.join(timeout=control.settings.countdown_seconds + 15)
    return processes.QUIT_EXIT_CODE if quitting.is_set() else 0


def _call_state(enabled: bool | None, call: Call | None) -> str:
    if not enabled:
        return "off"
    if call is None:
        return "on, not in a call"
    details = [call.session_id]
    if call.age_seconds is not None:
        details.append(f"started {call.age_seconds:.0f}s ago")
    if call.title:
        details.append(f'"{call.title}"')
    return f"on, in a call ({', '.join(details)})"


def print_check(settings: Settings, config_error: str | None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if config_error:
        print(f"config: {config_error} (using defaults)")
    now = datetime.now().astimezone()
    present = is_present(settings.idle_seconds)
    print(f"present: {present} (idle {idle_seconds():.0f}s, limit {settings.idle_seconds}s)")
    in_calls = {call.provider: call for call in CallWatch().calls(settings.calls)}
    for provider in CALL_PROVIDERS:
        print(f"{provider}: {_call_state(settings.calls.get(provider), in_calls.get(provider))}")
    print(f"granola running: {granola_running()}")
    if not settings.calendar_urls():
        print("calendar: no calendar feed in config")
        return
    if not settings.enabled_urls():
        print("calendar: every calendar in config is switched off")
        return
    # Calendars that are off aren't downloaded, but still lend the address the watcher last saved on them.
    saved = {feed.id: feed.owner for feed in read_status(status_path()).feeds}
    owners = {calendar.url: saved.get(feed_id(calendar.url)) for calendar in settings.distinct_calendars()}
    loaded = load_feeds(now, settings, owners)
    for index, _url, error in loaded.failures:
        print(f"calendar feed {index}: failed ({describe_error(error)})")
    # loaded.events is in the order of the Calendars list, so a meeting on two calendars keeps the higher one's copy.
    events = merge_events(*loaded.events.values())
    upcoming = [event for event in events if event.end > now][:8]
    if not upcoming:
        print("calendar: no upcoming events in the feed")
        return
    print("calendar:")
    for event in upcoming:
        print(f"  {event.start:%Y-%m-%d %H:%M}  {event.summary}{'  (skipped)' if event.skipped else ''}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Start Granola when a meeting begins. With no mode, run the watcher.",
        epilog="The other modes: --settings [--section=calendars|calls|notes|general], --setup, --stop, and --notice --title=TITLE.",
    )
    modes = parser.add_mutually_exclusive_group()
    for flag, help_text in (
        ("--install", "start at every sign-in, and start now"),
        ("--uninstall", "remove the sign-in entry and stop the watcher"),
        ("--check", "print presence, calls, and calendar status"),
        ("--preview-notice", "show the countdown once"),
    ):
        modes.add_argument(flag, dest="mode", action="store_const", const=flag[2:], help=help_text)
    parser.add_argument("--dry-run", action="store_true", help="show countdowns but do not launch Granola")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.mode is None and not processes.hold_mutex(processes.WATCHER_MUTEX):
        if sys.stdout is not None:
            print("another watcher is already running; quit it from its tray icon first")
        return 0
    logger = setup_logging("log.txt" if args.mode is None else "cli-log.txt")
    settings, config_error = load_or_defaults()
    if config_error:
        logger.error("%s; using defaults until it is fixed", config_error)
    if args.dry_run:
        settings.dry_run = True
    if args.mode == "preview-notice":
        from .notice import show_notice

        started = show_notice("Design review", settings.countdown_seconds)
        print("would start" if started is not None else "cancelled")
        return 0
    if args.mode == "check":
        print_check(settings, config_error)
        return 1 if config_error else 0
    if args.mode == "uninstall":
        processes.uninstall()
        logger.info("granola autostart removed")
        return 0
    if args.mode == "install":
        processes.install()
        logger.info("granola autostart installed")
        print("installed: it starts at every sign-in")
        return 0
    logger.info("granola autostart %s running (dry_run=%s)", __version__, settings.dry_run)
    return run_tray(settings, config_error, dry_run=args.dry_run)
