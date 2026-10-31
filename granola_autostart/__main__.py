"""Entry point for every mode: ``python -m granola_autostart`` from source, and GranolaAutostart.exe when packaged.

``--notice`` shows the countdown card without importing the watcher, so the card opens quickly.
``--supervise`` runs the supervisor, which stays up all day, so it loads only the standard library.
``--settings`` and ``--setup`` open the Settings window. Everything else (the watcher, ``--stop``,
``--check``, ``--install``) is watcher.py's command line.
"""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:
    # Started by file path, as a source checkout's sign-in entry and child processes are:
    # put the checkout on the path in place of this folder, so the package imports as a package.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)


def _log_failure(file_name: str, message: str) -> None:
    """Nothing shows an error under pythonw or in the packaged exe, so the reason goes to a log, if it can."""
    try:
        from granola_autostart.settings import setup_logging

        setup_logging(file_name).exception(message)
    except Exception:
        pass


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--notice":
        # The watcher only sees the exit code: 2 is a card that couldn't run (a build missing part of
        # Tk or Pillow, say), never a Cancel, which is 1.
        try:
            from granola_autostart import notice

            return notice.main(sys.argv[2:])
        except Exception:
            _log_failure("notice-log.txt", "the countdown card failed")
            return 2
    if mode == "--supervise":
        from granola_autostart import processes
        from granola_autostart.settings import setup_logging

        setup_logging("supervisor-log.txt")
        return processes.supervise()
    if mode in ("--settings", "--setup"):
        from granola_autostart import settings_app

        return settings_app.main(sys.argv[2:] if mode == "--settings" else sys.argv[1:])
    try:
        from granola_autostart import watcher

        return watcher.main()
    except Exception:
        # Logged where Settings' "Restarting after a problem" points; the supervisor starts it again.
        _log_failure("log.txt", "the watcher stopped")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
