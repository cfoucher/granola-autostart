"""Entry point for every mode: ``python -m granola_autostart`` from source, and GranolaAutostart.exe when packaged.

``--notice`` shows the countdown card without importing the watcher, so the card opens quickly.
``--settings`` and ``--setup`` open the Settings window. Everything else (the watcher, ``--supervise``,
``--stop``, ``--check``) is watcher.py's command line.
"""

from __future__ import annotations

import sys
from pathlib import Path

if not __package__:
    # Started by file path, as a source checkout's sign-in entry and child processes are:
    # put the checkout on the path in place of this folder, so the package imports as a package.
    sys.path[0] = str(Path(__file__).resolve().parent.parent)


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "--notice":
        from granola_autostart import notice

        return notice.main(sys.argv[2:])
    if mode in ("--settings", "--setup"):
        from granola_autostart import settings_app

        return settings_app.main(sys.argv[2:] if mode == "--settings" else sys.argv[1:])
    from granola_autostart import watcher

    return watcher.main()


if __name__ == "__main__":
    raise SystemExit(main())
