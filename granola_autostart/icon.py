"""The Labyrinth icon: the countdown card's green arc around a small labyrinth, drawn with Pillow.

The goal dot in the middle is the status light: green while watching, grey when paused, amber when
something needs attention. Off turns the whole icon grey and hollows the dot. A blue badge at the top right
means an update is ready to install. This one drawing makes the exe and installer icon, the tray icon, and the
icon on notifications. It follows the 64-unit grid of the design canvas.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

CREAM = (251, 248, 241, 255)
TRACK = (233, 226, 210, 255)
GREEN = (90, 138, 71, 255)
DEEP_GREEN = (66, 112, 58, 255)
DOTS = {
    "watching": GREEN,
    "paused": (124, 119, 109, 255),
    "attention": (208, 112, 31, 255),
}
# Off: the arc, the labyrinth, and the ring round the hollow centre dot, all grey.
OFF_ARC = (168, 161, 150, 255)
OFF_MAZE = (138, 132, 120, 255)
OFF_DOT = (124, 119, 109, 255)
BADGE = (47, 111, 208, 255)  # shown while an update is ready to install
BADGE_RING = (238, 238, 238, 255)
SUPERSAMPLE = 4


def draw_icon(size: int = 64, status: str = "watching", badge: bool = False) -> Image.Image:
    unit = size * SUPERSAMPLE / 64
    image = Image.new("RGBA", (round(64 * unit), round(64 * unit)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def disc(cx: float, cy: float, r: float, colour) -> None:
        draw.ellipse(((cx - r) * unit, (cy - r) * unit, (cx + r) * unit, (cy + r) * unit), fill=colour)

    def ring(r: float, start: float, end: float, width: float, colour, caps: bool = True) -> None:
        # Pillow strokes inward from the box, so the box sits half a stroke outside the radius.
        outer = r + width / 2
        box = ((32 - outer) * unit, (32 - outer) * unit, (32 + outer) * unit, (32 + outer) * unit)
        draw.arc(box, start, end, fill=colour, width=round(width * unit))
        if caps:
            for angle in (start, end):
                disc(32 + r * math.cos(math.radians(angle)), 32 + r * math.sin(math.radians(angle)), width / 2, colour)

    off = status == "off"
    arc, maze = (OFF_ARC, OFF_MAZE) if off else (GREEN, DEEP_GREEN)
    disc(32, 32, 31, CREAM)
    ring(27, 0, 360, 5, TRACK, caps=False)
    ring(27, -90, 180, 5, arc)  # the countdown arc: three quarters, from 12 o'clock clockwise
    ring(17, -74.3, 254.3, 4, maze)  # outer labyrinth ring, gap at the top
    ring(10, 114.8, 425.2, 4, maze)  # inner ring, gap at the bottom
    draw.line(((42 * unit, 32 * unit), (49 * unit, 32 * unit)), fill=maze, width=round(4 * unit))
    disc(42, 32, 2, maze)
    disc(49, 32, 2, maze)
    if off:
        # A hollow goal dot: a ring of radius 4 stroked 3 wide, so it covers radius 2.5 to 5.5.
        disc(32, 32, 5.5, OFF_DOT)
        disc(32, 32, 2.5, CREAM)
    else:
        disc(32, 32, 4.5, DOTS.get(status, GREEN))  # the goal dot
    if badge:
        # A circle of radius 11 ringed 4 wide: the ring is centred on the edge, so it covers radius 9 to 13.
        disc(52, 12, 13, BADGE_RING)
        disc(52, 12, 9, BADGE)
    return image.resize((size, size), Image.LANCZOS)
