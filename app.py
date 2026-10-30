"""Entry point of the packaged GranolaAutostart.exe.

``--notice`` shows the countdown card without importing the watcher, so the card opens quickly.
Everything else (the watcher, ``--supervise``, ``--stop``, ``--check``) is watcher.py's command line.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--notice":
        import notice

        return notice.main(sys.argv[2:])
    import watcher

    return watcher.main()


if __name__ == "__main__":
    raise SystemExit(main())
