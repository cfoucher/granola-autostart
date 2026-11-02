from granola_autostart.icon import BADGE, BADGE_RING, DEEP_GREEN, GREEN, OFF_ARC, OFF_SPIRAL, SPIRAL_COLOURS, draw_icon

BLOB = (34, 31)  # the blob that starts the spiral: centred at (33.86, 31.24) on the 64-unit grid
OUTER_TURN = (34, 47)  # the spiral's outer turn at six o'clock, which is centred at (33.86, 47.61)
OUTER_END = (34, 16)  # the spiral's outer end, at 12 o'clock


def near(pixel, colour, tolerance=6) -> bool:
    """Downscaling shifts colours by a step or two."""
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], colour[:3]))


def test_the_spiral_shows_the_status():
    for status, colour in SPIRAL_COLOURS.items():
        icon = draw_icon(64, status)
        assert icon.size == (64, 64)
        assert icon.getpixel(BLOB) == colour
        assert near(icon.getpixel(OUTER_TURN), colour)
        assert near(icon.getpixel(OUTER_END), colour)


def test_watching_is_a_deeper_green_than_the_arc():
    assert SPIRAL_COLOURS["watching"] == DEEP_GREEN
    assert draw_icon(64).getpixel(BLOB) != GREEN


def test_an_unknown_status_looks_like_watching():
    assert draw_icon(64, "bogus").getpixel(BLOB) == SPIRAL_COLOURS["watching"]


def test_the_countdown_arc_is_green_and_the_corners_are_clear():
    icon = draw_icon(64)
    # Three o'clock, inside the arc. Downscaling shifts colours by a step or two.
    assert near(icon.getpixel((59, 32)), GREEN, tolerance=4)
    assert icon.getpixel((0, 0))[3] == 0


def test_a_blue_badge_with_a_pale_ring_marks_an_update_ready_to_install():
    plain, badged = draw_icon(64, "paused"), draw_icon(64, "paused", badge=True)
    assert plain.getpixel((52, 12)) != BADGE and plain.getpixel((52, 23)) != BADGE_RING
    assert badged.getpixel((52, 12)) == BADGE
    # A circle of radius 11 ringed 4 wide at (52, 12): the ring runs from radius 9 to 13.
    assert all(abs(a - b) <= 12 for a, b in zip(badged.getpixel((52, 23)), BADGE_RING))
    assert badged.getpixel(BLOB) == SPIRAL_COLOURS["paused"]
    assert badged.getpixel((0, 0))[3] == 0 and badged.getpixel((63, 0))[3] == 0


def test_the_badge_scales_with_the_icon():
    assert draw_icon(32, badge=True).getpixel((26, 6)) == BADGE


def test_off_greys_the_arc_and_the_spiral():
    icon = draw_icon(64, "off")
    assert near(icon.getpixel((59, 32)), OFF_ARC)  # the countdown arc, at three o'clock
    assert icon.getpixel(BLOB) == OFF_SPIRAL  # the blob is filled, not a ring
    assert near(icon.getpixel(OUTER_TURN), OFF_SPIRAL)
    assert icon.getpixel((0, 0))[3] == 0
    assert not near(icon.getpixel((59, 32)), GREEN, tolerance=40)


def test_the_other_statuses_keep_the_green_arc():
    for status in SPIRAL_COLOURS:
        assert near(draw_icon(64, status).getpixel((59, 32)), GREEN, tolerance=4)


def test_off_still_carries_the_update_badge():
    assert draw_icon(64, "off", badge=True).getpixel((52, 12)) == BADGE


def test_the_spiral_is_centred_on_the_icon():
    icon = draw_icon(256)
    # The spiral is the only thing drawn in this colour, give or take the edges Lanczos blends.
    spots = [(x, y) for x in range(256) for y in range(256) if near(icon.getpixel((x, y)), DEEP_GREEN)]
    pixels = [(x, y) for x, y in spots if icon.getpixel((x, y))[3] == 255]
    left, right = min(x for x, _ in pixels), max(x for x, _ in pixels) + 1
    top, bottom = min(y for _, y in pixels), max(y for _, y in pixels) + 1
    assert abs((left + right) / 2 - 128) <= 2 and abs((top + bottom) / 2 - 128) <= 2
