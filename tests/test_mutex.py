from granola_autostart.watcher import create_mutex


def test_second_mutex_with_the_same_name_is_already_owned():
    first, first_error = create_mutex("Local\\GranolaAutostartMutexTest")
    _second, second_error = create_mutex("Local\\GranolaAutostartMutexTest")
    assert first
    assert first_error == 0
    assert second_error == 183
