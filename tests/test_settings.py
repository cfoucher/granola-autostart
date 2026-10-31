import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from granola_autostart.secret_box import protect, unprotect
from granola_autostart.settings import REPLACE_ATTEMPTS, ConfigError, load_settings, save_settings, write_json


def write_config(tmp_path, text: str, encoding: str = "utf-8"):
    (tmp_path / "config.json").write_text(text, encoding=encoding)


def test_config_saved_with_a_byte_order_mark_loads(tmp_path):
    write_config(tmp_path, json.dumps({"ics_url": "https://calendar.example/a.ics"}), encoding="utf-8-sig")
    assert load_settings().calendar_urls() == ["https://calendar.example/a.ics"]


def test_plain_addresses_are_encrypted_on_load_and_other_keys_kept(tmp_path):
    config = {
        "ics_url": "https://calendar.example/secret-a.ics",
        "ics_urls": ["https://calendar.example/secret-b.ics"],
        "user_email": "conrad@example.com",
        "a_key_from_the_future": 1,
    }
    write_config(tmp_path, json.dumps(config))
    assert load_settings().calendar_urls() == [
        "https://calendar.example/secret-a.ics",
        "https://calendar.example/secret-b.ics",
    ]
    text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "secret" not in text
    saved = json.loads(text)
    assert "ics_url" not in saved and "ics_urls" not in saved
    assert len(saved["ics_urls_protected"]) == 2
    assert saved["a_key_from_the_future"] == 1
    reloaded = load_settings()
    assert reloaded.calendar_urls() == ["https://calendar.example/secret-a.ics", "https://calendar.example/secret-b.ics"]
    assert reloaded.user_email == "conrad@example.com"


def test_saving_round_trips_every_setting():
    settings = load_settings()
    assert settings.setup_complete is False
    settings.ics_urls = ("https://calendar.example/c.ics",)
    settings.countdown_seconds = 20
    settings.ignore_title_substrings = ("standup",)
    settings.setup_complete = True
    save_settings(settings)
    again = load_settings()
    assert again.calendar_urls() == ["https://calendar.example/c.ics"]
    assert (again.countdown_seconds, again.ignore_title_substrings, again.setup_complete) == (20, ("standup",), True)


def test_an_address_encrypted_elsewhere_is_a_config_error(tmp_path):
    write_config(tmp_path, json.dumps({"ics_urls_protected": ["bm90IGEgcmVhbCBibG9i"]}))
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "decrypted" in str(error.value)


def test_invalid_json_reports_position_without_contents(tmp_path):
    write_config(tmp_path, '{\n  "ics_url": "https://calendar.example/secret.ics",\n}\n')
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "line 2" in str(error.value)
    assert "secret" not in str(error.value)


def test_single_strings_are_accepted_where_lists_are_expected(tmp_path):
    write_config(
        tmp_path,
        json.dumps({"ics_urls": "https://calendar.example/b.ics", "ignore_title_substrings": "Lunch"}),
    )
    settings = load_settings()
    assert settings.calendar_urls() == ["https://calendar.example/b.ics"]
    assert settings.ignore_title_substrings == ("Lunch",)


def test_dpapi_round_trips_and_rejects_tampering():
    token = protect("https://calendar.example/é.ics")
    assert "calendar" not in token
    assert unprotect(token) == "https://calendar.example/é.ics"
    with pytest.raises(ValueError):
        unprotect(token[:-8] + "AAAAAAA=")
    with pytest.raises(ValueError):
        unprotect("not base64!")


def test_wrong_types_raise_a_config_error(tmp_path):
    write_config(tmp_path, json.dumps({"ics_urls": 5}))
    with pytest.raises(ConfigError):
        load_settings()
    write_config(tmp_path, json.dumps({"idle_seconds": "soon"}))
    with pytest.raises(ConfigError):
        load_settings()


def test_flags_accept_true_and_false_written_as_text(tmp_path):
    write_config(tmp_path, json.dumps({"dry_run": " TRUE ", "setup_complete": "false"}))
    settings = load_settings()
    assert (settings.dry_run, settings.setup_complete) == (True, False)
    write_config(tmp_path, json.dumps({"dry_run": "False", "setup_complete": " True "}))
    settings = load_settings()
    assert (settings.dry_run, settings.setup_complete) == (False, True)


def test_a_flag_that_is_not_true_or_false_is_a_config_error(tmp_path):
    for value in ("yes", "1", "", 1):
        write_config(tmp_path, json.dumps({"setup_complete": value}))
        with pytest.raises(ConfigError, match="setup_complete"):
            load_settings()


def test_true_or_false_where_a_number_is_expected_is_a_config_error(tmp_path):
    write_config(tmp_path, json.dumps({"idle_seconds": True}))
    with pytest.raises(ConfigError, match="idle_seconds"):
        load_settings()


def test_an_email_that_is_not_text_is_a_config_error(tmp_path):
    write_config(tmp_path, json.dumps({"user_email": ["conrad@example.com"]}))
    with pytest.raises(ConfigError, match="user_email"):
        load_settings()


def test_an_address_added_by_hand_next_to_encrypted_ones_is_kept_and_encrypted(tmp_path):
    config = {
        "ics_urls_protected": [protect("https://calendar.example/secret-a.ics")],
        "ics_urls": ["https://calendar.example/secret-b.ics"],
    }
    write_config(tmp_path, json.dumps(config))
    expected = ["https://calendar.example/secret-a.ics", "https://calendar.example/secret-b.ics"]
    assert load_settings().calendar_urls() == expected
    text = (tmp_path / "config.json").read_text(encoding="utf-8")
    assert "secret" not in text
    saved = json.loads(text)
    assert "ics_urls" not in saved
    assert [unprotect(token) for token in saved["ics_urls_protected"]] == expected
    assert load_settings().calendar_urls() == expected


def test_calendar_refresh_has_a_floor(tmp_path):
    write_config(tmp_path, json.dumps({"calendar_refresh_seconds": 5}))
    assert load_settings().calendar_refresh_seconds == 60


def test_null_numbers_and_flags_mean_the_defaults(tmp_path):
    write_config(tmp_path, json.dumps({"idle_seconds": None, "poll_seconds": None, "dry_run": None, "setup_complete": None}))
    loaded = load_settings()
    assert (loaded.idle_seconds, loaded.poll_seconds, loaded.dry_run, loaded.setup_complete) == (300, 5, False, False)


@pytest.fixture
def replaces(monkeypatch):
    """Each file write_json puts in place, after the PermissionErrors queued in ``blocked``; nothing really sleeps."""
    real_replace = Path.replace
    record = SimpleNamespace(done=[], blocked=[], sleeps=[])

    def replace(self, target):
        if record.blocked:
            raise record.blocked.pop()
        record.done.append(target)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    monkeypatch.setattr("granola_autostart.settings.time", SimpleNamespace(sleep=record.sleeps.append))
    return record


def test_a_file_held_open_for_a_moment_is_still_replaced(tmp_path, replaces):
    replaces.blocked = [PermissionError(), PermissionError()]
    write_json(tmp_path / "status.json", {"a": 1})
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8")) == {"a": 1}
    assert replaces.sleeps == pytest.approx([0.05, 0.1])


def test_a_file_that_stays_locked_raises_after_the_last_try(tmp_path, replaces):
    replaces.blocked = [PermissionError() for _ in range(REPLACE_ATTEMPTS)]
    with pytest.raises(PermissionError):
        write_json(tmp_path / "status.json", {"a": 1})
    assert replaces.blocked == [] and len(replaces.sleeps) == REPLACE_ATTEMPTS - 1
    assert not (tmp_path / "status.json").exists()


def test_skip_unchanged_skips_only_a_rewrite_of_the_same_file_still_there(tmp_path, replaces):
    path = tmp_path / "status.json"
    write_json(path, {"a": 1}, skip_unchanged=True)
    write_json(path, {"a": 1}, skip_unchanged=True)
    assert len(replaces.done) == 1
    path.unlink()
    write_json(path, {"a": 1}, skip_unchanged=True)
    assert len(replaces.done) == 2 and path.exists()
    write_json(path, {"a": 2}, skip_unchanged=True)
    assert len(replaces.done) == 3
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": 2}
