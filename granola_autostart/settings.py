"""Local config, state, and log paths, and writing those files."""

from __future__ import annotations

import dataclasses
import json
import logging
import math
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .secret_box import protect, unprotect

log = logging.getLogger("granola_autostart")
DEFAULT_IGNORE = ("focus time", "out of office", "lunch")
# Which calls start notes. Zoom only, for new installs and upgrades alike: Teams and Meet are opt-in.
CALL_PROVIDERS = ("zoom", "teams", "meet")
DEFAULT_CALLS = {"zoom": True, "teams": False, "meet": False}
# Appearance: the app theme for Settings, setup, the countdown card, and the tray menu. Auto follows Windows.
APPEARANCES = ("auto", "light", "dark")
MIN_CALENDAR_REFRESH_SECONDS = 60
# Older configs, and hand edits, hold addresses in plain text under these keys.
PLAIN_ADDRESS_KEYS = ("ics_url", "ics_urls")
# Before calendars had settings of their own, encrypted addresses were one flat list under this key.
FLAT_ADDRESS_KEY = "ics_urls_protected"
REPLACE_ATTEMPTS = 5
# write_json's temporary files, "config.json.<process id>.<thread id>.tmp", and how long one has to be left
# before it is taken for the leftover of a save that never finished.
_TEMPORARY = re.compile(r"[\w-]+\.json\.[0-9]+\.[0-9]+\.tmp")
STALE_TEMPORARY_SECONDS = 24 * 60 * 60
_written: dict[Path, str] = {}


class ConfigError(Exception):
    """config.json could not be used. The message never includes file contents."""


def app_dir() -> Path:
    override = os.environ.get("GRANOLA_AUTOSTART_HOME")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise RuntimeError("LOCALAPPDATA is not set")
    return Path(local) / "granola-autostart"


def config_path() -> Path:
    return app_dir() / "config.json"


def state_path() -> Path:
    return app_dir() / "state.json"


def log_path(name: str = "log.txt") -> Path:
    return app_dir() / name


def status_path() -> Path:
    return app_dir() / "status.json"


def update_path() -> Path:
    return app_dir() / "update.json"


def write_json(path: Path, data, *, skip_unchanged: bool = False) -> None:
    """Replace the file in one step, so a reader never sees half of it.

    ``skip_unchanged`` saves a write when this process last wrote the same content, for files that
    only this process writes. Replacing fails while another process has the file open for a moment,
    so it tries a few times. Each process and thread writes through a temporary file of its own, so the
    tray and Settings saving the same file at once can't mix their text.
    """
    text = json.dumps(data, indent=2) + "\n"
    if skip_unchanged and _written.get(path) == text and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        for attempt in range(1, REPLACE_ATTEMPTS + 1):
            try:
                temporary.replace(path)
                break
            except PermissionError:
                if attempt == REPLACE_ATTEMPTS:
                    raise
                time.sleep(0.05 * attempt)
    except BaseException:
        # A save that fails leaves no temporary file behind.
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    _written[path] = text


def sweep_stale_temporaries() -> None:
    """Delete the temporary files of saves that never finished. A process stopped in the middle of one (an update
    stops the app with Stop-Process -Force) can't clean up after itself. Only write_json's own file names go,
    and only after a day, so a save that is under way in another process is never touched."""
    cutoff = time.time() - STALE_TEMPORARY_SECONDS
    try:
        paths = list(app_dir().iterdir())
    except OSError:  # no folder yet
        return
    for path in paths:
        if not _TEMPORARY.fullmatch(path.name):
            continue
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                log.info("deleted %s, left by a save that never finished", path.name)
        except OSError as error:
            log.warning("could not delete %s (%s)", path.name, type(error).__name__)


@dataclass(frozen=True)
class Calendar:
    """One calendar's address, whether it starts notes, and whose declines it skips."""

    url: str
    enabled: bool = True
    # None is automatic: the calendar's own address if its name is one, otherwise every address the app
    # knows, plus Settings.user_email. A list is exactly those addresses, so aliases work, and an empty one
    # checks no declines at all.
    declined_by: tuple[str, ...] | None = None


def clean_addresses(values, *, lower: bool = False) -> tuple[str, ...] | None:
    """Declined addresses without blanks or repeats (compared ignoring case), or None when none are left.
    ``lower`` writes each one in lower case, as declines are compared."""
    found: dict[str, str] = {}
    for value in values or ():
        address = (value or "").strip()
        if address:
            found.setdefault(address.casefold(), address.casefold() if lower else address)
    return tuple(found.values()) or None


def clean_declined_by(values) -> tuple[str, ...] | None:
    """A calendar's ``declined_by`` from a list that was saved or sent: no list (None) is automatic, and a list is
    exactly its addresses, an empty one included, which checks no declines."""
    return None if values is None else clean_addresses(values) or ()


@dataclass
class Settings:
    calendars: tuple[Calendar, ...] = ()
    # The old extra address for declines. Automatic calendars still read it, but nothing in Settings sets it now.
    user_email: str = ""
    calls: dict[str, bool] = field(default_factory=lambda: dict(DEFAULT_CALLS))
    autostart_notes: bool = True  # the app's on/off: off lasts through restarts
    appearance: str = "auto"
    idle_seconds: int = 300
    countdown_seconds: int = 10
    # Zoom's names, kept for config.json, though they now cover a call on any provider: how far ahead a call
    # counts as its meeting starting early, and how recently a call must have begun to get a card.
    zoom_lookahead_minutes: int = 10
    fresh_zoom_seconds: int = 120
    ignore_title_substrings: tuple[str, ...] = DEFAULT_IGNORE
    poll_seconds: float = 5
    calendar_refresh_seconds: int = 300
    dry_run: bool = False
    setup_complete: bool = False

    def distinct_calendars(self) -> list[Calendar]:
        """The calendars with blank addresses dropped and each address once. The first entry for an address wins."""
        found: dict[str, Calendar] = {}
        for calendar in self.calendars:
            url = calendar.url.strip()
            if url and url not in found:
                found[url] = dataclasses.replace(calendar, url=url)
        return list(found.values())

    def calendar_urls(self) -> list[str]:
        """Every calendar's address, switched on or off."""
        return [calendar.url for calendar in self.distinct_calendars()]

    def enabled_urls(self) -> list[str]:
        """The addresses of the calendars that are switched on, which are the ones downloaded."""
        return [calendar.url for calendar in self.distinct_calendars() if calendar.enabled]

    def to_json(self) -> dict:
        """The file form. Calendar addresses are encrypted for this Windows user."""
        return {
            "calendars": [
                {
                    "address_protected": protect(calendar.url),
                    "enabled": calendar.enabled,
                    "declined_by": None if calendar.declined_by is None else list(calendar.declined_by),
                }
                for calendar in self.distinct_calendars()
            ],
            "user_email": self.user_email,
            "calls": {provider: bool(self.calls.get(provider, DEFAULT_CALLS[provider])) for provider in CALL_PROVIDERS},
            "autostart_notes": self.autostart_notes,
            "appearance": self.appearance,
            "idle_seconds": self.idle_seconds,
            "countdown_seconds": self.countdown_seconds,
            "zoom_lookahead_minutes": self.zoom_lookahead_minutes,
            "fresh_zoom_seconds": self.fresh_zoom_seconds,
            "ignore_title_substrings": list(self.ignore_title_substrings),
            "poll_seconds": self.poll_seconds,
            "calendar_refresh_seconds": self.calendar_refresh_seconds,
            "dry_run": self.dry_run,
            "setup_complete": self.setup_complete,
        }


def save_settings(settings: Settings) -> None:
    """Write config.json in one step, keeping any keys this version does not know about."""
    path = config_path()
    try:
        raw = _read_raw(path) if path.exists() else {}
    except ConfigError:
        raw = {}
    for key in (*PLAIN_ADDRESS_KEYS, FLAT_ADDRESS_KEY):
        raw.pop(key, None)
    raw.update(settings.to_json())
    write_json(path, raw)


def update_config(key: str, value) -> None:
    """Set one key of config.json to ``value`` and keep everything else as it is on disk now.

    The tray's Turn off uses it, because save_settings would write the watcher's whole copy of the settings,
    which may not have caught up with a change made in Settings a moment ago. A file that can't be read
    raises ConfigError and is left alone: someone may be in the middle of fixing it.
    """
    path = config_path()
    raw = _read_raw(path) if path.exists() else {}
    raw[key] = value
    write_json(path, raw)


def saved_flag(name: str, default: bool) -> bool:
    """One true/false setting as saved, without loading the rest. ``default`` when the file can't give it."""
    try:
        return _flag(_read_raw(config_path()), name, default=default)
    except (ConfigError, RuntimeError):  # RuntimeError: no LOCALAPPDATA
        return default


def saved_appearance() -> str:
    """The Appearance choice as saved. The countdown card and the title bar need only this, so it reads that one
    key: nothing is decrypted, and a file that is missing or broken means Auto."""
    try:
        return _appearance(_read_raw(config_path()))
    except (ConfigError, RuntimeError):  # RuntimeError: no LOCALAPPDATA
        return "auto"


def load_or_defaults() -> tuple[Settings, str | None]:
    """The saved settings, or the defaults and why config.json couldn't be used."""
    try:
        return load_settings(), None
    except ConfigError as error:
        return Settings(), str(error)


def load_settings() -> Settings:
    path = config_path()
    if not path.exists():
        settings = Settings()
        save_settings(settings)
        return settings
    raw = _read_raw(path)
    # An address in the old flat list, or typed in plain, becomes a calendar that is on and automatic, as it behaved before.
    flat = tuple(Calendar(_decrypt(token, FLAT_ADDRESS_KEY)) for token in _text_list(raw, FLAT_ADDRESS_KEY, ()))
    plain = tuple(url for url in (_text(raw, "ics_url"), *_text_list(raw, "ics_urls", ())) if url)
    defaults = Settings()
    settings = Settings(
        calendars=(*_calendars(raw), *flat, *(Calendar(url) for url in plain)),
        user_email=_text(raw, "user_email"),
        calls=_calls(raw),
        autostart_notes=_flag(raw, "autostart_notes", default=True),
        appearance=_appearance(raw),
        idle_seconds=_number(raw, "idle_seconds", defaults.idle_seconds, int, minimum=10),
        countdown_seconds=_number(raw, "countdown_seconds", defaults.countdown_seconds, int, minimum=1),
        zoom_lookahead_minutes=_number(raw, "zoom_lookahead_minutes", defaults.zoom_lookahead_minutes, int, minimum=0),
        fresh_zoom_seconds=_number(raw, "fresh_zoom_seconds", defaults.fresh_zoom_seconds, int, minimum=0),
        ignore_title_substrings=_text_list(raw, "ignore_title_substrings", DEFAULT_IGNORE),
        poll_seconds=_number(raw, "poll_seconds", defaults.poll_seconds, float, minimum=1),
        calendar_refresh_seconds=_number(
            raw,
            "calendar_refresh_seconds",
            defaults.calendar_refresh_seconds,
            int,
            minimum=MIN_CALENDAR_REFRESH_SECONDS,
        ),
        dry_run=_flag(raw, "dry_run"),
        setup_complete=_flag(raw, "setup_complete"),
    )
    settings.calendars = tuple(settings.distinct_calendars())
    if plain:
        # Addresses typed into the file by hand, or kept from before encryption, are encrypted now.
        try:
            save_settings(settings)
        except OSError as error:
            log.warning("could not encrypt calendar addresses (%s)", type(error).__name__)
    return settings


def _read_raw(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as error:
        raise ConfigError(f"config.json could not be read ({type(error).__name__})") from None
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise ConfigError(f"config.json is not valid JSON at line {error.lineno}, column {error.colno}: {error.msg}") from None
    if not isinstance(raw, dict):
        raise ConfigError("config.json must contain a JSON object")
    return raw


def _calendars(raw: dict) -> tuple[Calendar, ...]:
    value = raw.get("calendars")
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("config.json: calendars must be a list")
    return tuple(_calendar(entry, f"calendars[{number}]") for number, entry in enumerate(value, start=1))


def _calendar(entry, where: str) -> Calendar:
    if not isinstance(entry, dict):
        raise ConfigError(f"config.json: {where} must be an object")
    token = entry.get("address_protected")
    if not isinstance(token, str) or not token.strip():
        raise ConfigError(f"config.json: {where}.address_protected must be the encrypted address")
    declined = entry.get("declined_by")
    if declined is not None and (not isinstance(declined, list) or not all(isinstance(item, str) for item in declined)):
        raise ConfigError(f"config.json: {where}.declined_by must be null or a list of text values")
    return Calendar(
        _decrypt(token.strip(), "calendars"),
        enabled=_flag(entry, "enabled", default=True, label=f"{where}.enabled"),
        declined_by=clean_declined_by(declined),
    )


def _decrypt(token: str, key: str) -> str:
    try:
        return unprotect(token)
    except ValueError:
        raise ConfigError(
            "config.json: a calendar address could not be decrypted, because another Windows user or PC saved it. "
            f"Open config.json and delete the {key} list, then add your calendars again in Settings"
        ) from None


def _text(raw: dict, name: str) -> str:
    value = raw.get(name)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"config.json: {name} must be text")
    return value.strip()


def _text_list(raw: dict, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in raw:
        return default
    value = raw[name]
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"config.json: {name} must be a list of text values")
    return tuple(item.strip() for item in value if item.strip())


def _number(raw: dict, name: str, default, kind, *, minimum):
    # null means "use the default". (For text it means empty, and for a list, none.)
    value = default if raw.get(name) is None else raw[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"config.json: {name} must be a number")
    return max(kind(minimum), kind(value))


def _flag(raw: dict, name: str, *, default: bool = False, label: str | None = None) -> bool:
    value = raw.get(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    raise ConfigError(f"config.json: {label or name} must be true or false")


def _calls(raw: dict) -> dict[str, bool]:
    """Each call provider's switch. One that is missing keeps its default, and one this version doesn't know is ignored."""
    value = raw.get("calls")
    if value is None:
        return dict(DEFAULT_CALLS)
    if not isinstance(value, dict):
        raise ConfigError('config.json: calls must be a set of switches, like {"zoom": true, "teams": false}')
    return {
        provider: _flag(value, provider, default=DEFAULT_CALLS[provider], label=f"calls.{provider}")
        for provider in CALL_PROVIDERS
    }


def _appearance(raw: dict) -> str:
    """Auto, Light, or Dark. A missing value, or one this version doesn't know, means Auto."""
    value = raw.get("appearance")
    choice = value.strip().casefold() if isinstance(value, str) else ""
    return choice if choice in APPEARANCES else "auto"


def setup_logging(file_name: str = "log.txt") -> logging.Logger:
    """Each process kind gets its own file so log rotation never fights over an open file."""
    app_dir().mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("granola_autostart")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_path(file_name), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    if sys.stderr is not None:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)
    return logger
