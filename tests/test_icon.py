from granola_autostart.icon import BADGE, BADGE_RING, CREAM, DOTS, GREEN, OFF_ARC, OFF_DOT, OFF_MAZE, draw_icon


def near(pixel, colour, tolerance=6) -> bool:
    """Downscaling shifts colours by a step or two."""
    return all(abs(a - b) <= tolerance for a, b in zip(pixel[:3], colour[:3]))


def test_the_goal_dot_shows_the_status():
    for status, colour in DOTS.items():
        icon = draw_icon(64, status)
        assert icon.size == (64, 64)
        assert icon.getpixel((32, 32)) == colour


def test_the_countdown_arc_is_green_and_the_corners_are_clear():
    icon = draw_icon(64)
    # Three o'clock, inside the arc. Downscaling shifts colours by a step or two.
    assert all(abs(a - b) <= 4 for a, b in zip(icon.getpixel((59, 32))[:3], GREEN[:3]))
    assert icon.getpixel((0, 0))[3] == 0


def test_a_blue_badge_with_a_pale_ring_marks_an_update_ready_to_install():
    plain, badged = draw_icon(64, "paused"), draw_icon(64, "paused", badge=True)
    assert plain.getpixel((52, 12)) != BADGE and plain.getpixel((52, 23)) != BADGE_RING
    assert badged.getpixel((52, 12)) == BADGE
    # A circle of radius 11 ringed 4 wide at (52, 12): the ring runs from radius 9 to 13.
    assert all(abs(a - b) <= 12 for a, b in zip(badged.getpixel((52, 23)), BADGE_RING))
    assert badged.getpixel((32, 32)) == DOTS["paused"]
    assert badged.getpixel((0, 0))[3] == 0 and badged.getpixel((63, 0))[3] == 0


def test_the_badge_scales_with_the_icon():
    assert draw_icon(32, badge=True).getpixel((26, 6)) == BADGE


def test_off_greys_the_arc_and_the_labyrinth_and_hollows_the_goal_dot():
    icon = draw_icon(64, "off")
    assert near(icon.getpixel((59, 32)), OFF_ARC)  # the countdown arc, at three o'clock
    assert near(icon.getpixel((32, 49)), OFF_MAZE)  # the outer labyrinth ring, at six o'clock
    assert near(icon.getpixel((36, 32)), OFF_DOT)  # the ring round the dot: radius 2.5 to 5.5
    assert near(icon.getpixel((32, 32)), CREAM)  # and nothing inside it
    assert icon.getpixel((0, 0))[3] == 0
    assert not near(icon.getpixel((59, 32)), GREEN, tolerance=40)


def test_the_other_statuses_keep_the_green_arc_and_a_filled_dot():
    for status in DOTS:
        icon = draw_icon(64, status)
        assert near(icon.getpixel((59, 32)), GREEN, tolerance=4)
        assert icon.getpixel((32, 32)) == DOTS[status]


def test_off_still_carries_the_update_badge():
    assert draw_icon(64, "off", badge=True).getpixel((52, 12)) == BADGE
