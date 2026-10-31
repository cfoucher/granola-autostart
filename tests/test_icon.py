from granola_autostart.icon import DOTS, GREEN, draw_icon


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
