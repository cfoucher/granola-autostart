"""Save the Labyrinth app icon as a multi-size .ico for the exe and the installer.

Usage: python packaging/make_icon.py build/app.ico
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from granola_autostart.icon import draw_icon  # noqa: E402

SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]


def main() -> int:
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    draw_icon(256).save(target, sizes=SIZES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
