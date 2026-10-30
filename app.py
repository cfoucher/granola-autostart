"""Entry point of the packaged GranolaAutostart.exe.

``--notice`` shows the countdown card without importing the watcher, so the card opens quickly.
``--settings`` and ``--setup`` open the Settings window. Everything else (the watcher, ``--supervise``,
``--stop``, ``--check``) is watcher.py's command line.
"""

from __future__ import annotations

import sys


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--notice":
        import notice

        return notice.main(sys.argv[2:])
    if mode in ("--settings", "--setup"):
        import settings_app

        return settings_app.main(sys.argv[2:] if mode == "--settings" else sys.argv[1:])
    import watcher

    return watcher.main()


if __name__ == "__main__":
    raise SystemExit(main())
