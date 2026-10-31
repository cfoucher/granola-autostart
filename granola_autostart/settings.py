"""Local config, state, and log paths, and writing those files."""

from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .secret_box import protect, unprotect

DEFAULT_IGNORE = ("focus time", "out of office", "lunch")
MIN_CALENDAR_REFRESH_SECONDS = 60
# Older configs, and hand edits, hold addresses in plain text under these keys.
PLAIN_ADDRESS_KEYS = ("ics_url", "ics_urls")
REPLACE_ATTEMPTS = 5
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


def write_json(path: Path, data, *, skip_unchanged: bool = False) -> None:
    """Replace the file in one step, so a reader never sees half of it.

    ``skip_unchanged`` saves a write when this process last wrote the same content, for files that
    only this process writes. Replacing fails while another process has the file open for a moment,
    so it tries a few times.
    """
    text = json.dumps(data, indent=2) + "\n"
    if skip_unchanged and _written.get(path) == text and path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            temporary.replace(path)
            break
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                raise
            time.sleep(0.05 * attempt)
    _written[path] = text


@dataclass
class Settings:
    ics_urls: tuple[str, ...] = ()
    user_email: str = ""
    idle_seconds: int = 300
    countdown_seconds: int = 10
    zoom_lookahead_minutes: int = 10
    fresh_zoom_seconds: int = 120
    ignore_title_substrings: tuple[str, ...] = DEFAULT_IGNORE
    poll_seconds: float = 5
    calendar_refresh_seconds: int = 300
    dry_run: bool = False
    setup_complete: bool = False

    def calendar_urls(self) -> list[str]:
        urls: list[str] = []
        for url in self.ics_urls:
            cleaned = url.strip()
            if cleaned and cleaned not in urls:
                urls.append(cleaned)
        return urls

    def to_json(self) -> dict:
        """The file form. Calendar addresses are encrypted for this Windows user."""
        return {
            "ics_urls_protected": [protect(url) for url in self.calendar_urls()],
            "user_email": self.user_email,
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
    for key in PLAIN_ADDRESS_KEYS:
        raw.pop(key, None)
    raw.update(settings.to_json())
    write_json(path, raw)


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
    try:
        protected = tuple(unprotect(token) for token in _text_list(raw, "ics_urls_protected", ()))
    except ValueError:
        raise ConfigError(
            "config.json: a calendar address could not be decrypted, because another Windows user or PC saved it. "
            "Open config.json and delete the ics_urls_protected list, then add your calendars again in Settings"
        ) from None
    plain = tuple(url for url in (_text(raw, "ics_url"), *_text_list(raw, "ics_urls", ())) if url)
    defaults = Settings()
    settings = Settings(
        ics_urls=protected + plain,
        user_email=_text(raw, "user_email"),
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
    settings.ics_urls = tuple(settings.calendar_urls())
    if plain:
        # Addresses typed into the file by hand, or kept from before encryption, are encrypted now.
        try:
            save_settings(settings)
        except OSError as error:
            logging.getLogger("granola_autostart").warning("could not encrypt calendar addresses (%s)", type(error).__name__)
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


def _flag(raw: dict, name: str) -> bool:
    value = raw.get(name)
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().casefold() in {"true", "false"}:
        return value.strip().casefold() == "true"
    raise ConfigError(f"config.json: {name} must be true or false")


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
