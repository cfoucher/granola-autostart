"""The Spiral icon: the countdown card's green arc around a small spiral, drawn with Pillow.

The spiral is the status light: green while watching, grey when paused, amber when something needs
attention. Off turns the arc and the spiral grey. A blue badge at the top right means an update is ready to
install. This one drawing makes the exe and installer icon, the tray icon, and the icon on notifications. It
follows the 64-unit grid of the design canvas.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw

CREAM = (251, 248, 241, 255)
TRACK = (233, 226, 210, 255)
GREEN = (90, 138, 71, 255)
DEEP_GREEN = (66, 112, 58, 255)
SPIRAL_COLOURS = {
    "watching": DEEP_GREEN,
    "paused": (124, 119, 109, 255),
    "attention": (208, 112, 31, 255),
}
OFF_ARC = (168, 161, 150, 255)
OFF_SPIRAL = (138, 132, 120, 255)
BADGE = (47, 111, 208, 255)  # shown while an update is ready to install
BADGE_RING = (238, 238, 238, 255)
SUPERSAMPLE = 4
# The spiral's radius at its inner end and at its outer end, which is at 12 o'clock.
SPIRAL_INNER = 2.6
SPIRAL_OUTER = 17.5
SPIRAL_TURNS = 2
SPIRAL_WIDTH = 4
SPIRAL_BLOB = SPIRAL_WIDTH * 0.85  # the radius of the blob that starts the spiral
SPIRAL_STEPS = 720


def spiral_points() -> list[tuple[float, float]]:
    """The spiral's centreline on the 64-unit grid, from the inner end clockwise out to 12 o'clock."""
    total = 2 * math.pi * SPIRAL_TURNS
    points = []
    for step in range(SPIRAL_STEPS + 1):
        t = total * step / SPIRAL_STEPS
        r = SPIRAL_INNER + (SPIRAL_OUTER - SPIRAL_INNER) * t / total
        angle = -math.pi / 2 - (total - t)
        points.append((r * math.cos(angle), r * math.sin(angle)))
    # Centre the outline, the line together with the blob at its inner end, not the centreline alone.
    xs = [x for x, _ in points] + [points[0][0] - SPIRAL_BLOB, points[0][0] + SPIRAL_BLOB]
    ys = [y for _, y in points] + [points[0][1] - SPIRAL_BLOB, points[0][1] + SPIRAL_BLOB]
    dx, dy = 32 - (min(xs) + max(xs)) / 2, 32 - (min(ys) + max(ys)) / 2
    return [(x + dx, y + dy) for x, y in points]


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

    def spiral(colour) -> None:
        # Joined thick line segments leave seams, so the stroke is one polygon: the centreline pushed half a
        # width out along its normal going forward, and half a width the other way coming back.
        points = spiral_points()
        left, right = [], []
        for before, here, after in zip([points[0], *points], points, [*points[1:], points[-1]]):
            tx, ty = after[0] - before[0], after[1] - before[1]
            length = math.hypot(tx, ty)
            nx, ny = -ty / length * SPIRAL_WIDTH / 2, tx / length * SPIRAL_WIDTH / 2
            left.append(((here[0] + nx) * unit, (here[1] + ny) * unit))
            right.append(((here[0] - nx) * unit, (here[1] - ny) * unit))
        draw.polygon(left + right[::-1], fill=colour)
        disc(*points[-1], SPIRAL_WIDTH / 2, colour)
        disc(*points[0], SPIRAL_BLOB, colour)

    off = status == "off"
    arc, spiral_colour = (OFF_ARC, OFF_SPIRAL) if off else (GREEN, SPIRAL_COLOURS.get(status, DEEP_GREEN))
    disc(32, 32, 31, CREAM)
    ring(27, 0, 360, 5, TRACK, caps=False)
    ring(27, -90, 180, 5, arc)  # the countdown arc: three quarters, from 12 o'clock clockwise
    spiral(spiral_colour)
    if badge:
        # A circle of radius 11 ringed 4 wide: the ring is centred on the edge, so it covers radius 9 to 13.
        disc(52, 12, 13, BADGE_RING)
        disc(52, 12, 9, BADGE)
    return image.resize((size, size), Image.LANCZOS)
