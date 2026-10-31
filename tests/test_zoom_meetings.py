from granola_autostart.zoom_meetings import MAX_TITLE, best_window_title


def test_the_meeting_name_is_the_longest_title_that_is_not_zoom_itself():
    assert best_window_title(["Zoom Meeting", "Roadmap review", "Zoom"]) == "Roadmap review"
    assert best_window_title([" Meeting Chat ", "Roadmap"]) == "Roadmap"


def test_only_zoom_s_own_window_names_mean_no_meeting_name():
    assert best_window_title(["Zoom Workplace", "zoom webinar", "  ", ""]) is None
    assert best_window_title([]) is None


def test_a_very_long_title_is_shortened():
    assert best_window_title(["x" * 500]) == "x" * MAX_TITLE


def test_of_several_meeting_names_the_longest_wins_without_its_spaces():
    assert best_window_title(["Roadmap", "  Roadmap review  ", "Zoom"]) == "Roadmap review"
