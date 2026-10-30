"""Draw the app icon, a green circle with a white centre, as a multi-size .ico.

Usage: python packaging/make_icon.py build/app.ico
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

GREEN = (90, 138, 71, 255)  # #5a8a47, the countdown ring's colour
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def draw(size: int = 256) -> Image.Image:
    scale = 4
    big = size * scale
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    margin = big / 16
    draw.ellipse((margin, margin, big - margin, big - margin), fill=GREEN)
    dot = big * 0.18
    centre = big / 2
    draw.ellipse((centre - dot, centre - dot, centre + dot, centre + dot), fill=(255, 255, 255, 255))
    return image.resize((size, size), Image.LANCZOS)


def main() -> int:
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)
    draw().save(target, sizes=SIZES)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
