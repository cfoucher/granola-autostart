"""Local config, state, and log paths."""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .secret_box import protect, unprotect

DEFAULT_IGNORE = ("focus time", "out of office", "lunch")
MIN_CALENDAR_REFRESH_SECONDS = 60
# Older configs, and hand edits, hold addresses in plain text under these keys.
PLAIN_ADDRESS_KEYS = ("ics_url", "ics_urls")


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


@dataclass
class Settings:
    ics_url: str = ""
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
        for url in (self.ics_url, *self.ics_urls):
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
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


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
            "config.json: a calendar address could not be decrypted. It was saved by another Windows user or PC, "
            "so add that calendar again in Settings."
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
    value = raw.get(name, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"config.json: {name} must be a number")
    return max(kind(minimum), kind(value))


def _flag(raw: dict, name: str) -> bool:
    value = raw.get(name, False)
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
