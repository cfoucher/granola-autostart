"""Local config, state, and log paths."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_IGNORE = ("focus time", "out of office", "lunch")


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


def log_path() -> Path:
    return app_dir() / "log.txt"


@dataclass
class Settings:
    ics_url: str = ""
    user_email: str = ""
    idle_seconds: int = 300
    countdown_seconds: int = 10
    zoom_lookahead_minutes: int = 10
    fresh_zoom_seconds: int = 120
    ignore_title_substrings: tuple[str, ...] = DEFAULT_IGNORE
    poll_seconds: float = 5
    calendar_refresh_seconds: int = 60
    dry_run: bool = False

    def to_json(self) -> dict:
        return {
            "ics_url": self.ics_url,
            "user_email": self.user_email,
            "idle_seconds": self.idle_seconds,
            "countdown_seconds": self.countdown_seconds,
            "zoom_lookahead_minutes": self.zoom_lookahead_minutes,
            "fresh_zoom_seconds": self.fresh_zoom_seconds,
            "ignore_title_substrings": list(self.ignore_title_substrings),
            "poll_seconds": self.poll_seconds,
            "calendar_refresh_seconds": self.calendar_refresh_seconds,
            "dry_run": self.dry_run,
        }


def load_settings() -> Settings:
    path = config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        settings = Settings()
        path.write_text(json.dumps(settings.to_json(), indent=2) + "\n", encoding="utf-8")
        return settings
    raw = json.loads(path.read_text(encoding="utf-8"))
    ignore = raw.get("ignore_title_substrings", list(DEFAULT_IGNORE))
    return Settings(
        ics_url=str(raw.get("ics_url") or "").strip(),
        user_email=str(raw.get("user_email") or "").strip(),
        idle_seconds=int(raw.get("idle_seconds", 300)),
        countdown_seconds=int(raw.get("countdown_seconds", 10)),
        zoom_lookahead_minutes=int(raw.get("zoom_lookahead_minutes", 10)),
        fresh_zoom_seconds=int(raw.get("fresh_zoom_seconds", 120)),
        ignore_title_substrings=tuple(str(item) for item in ignore),
        poll_seconds=float(raw.get("poll_seconds", 5)),
        calendar_refresh_seconds=int(raw.get("calendar_refresh_seconds", 60)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def setup_logging() -> logging.Logger:
    app_dir().mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("granola_autostart")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = RotatingFileHandler(log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    return logger
