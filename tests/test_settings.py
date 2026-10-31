import json

import pytest

from granola_autostart.secret_box import protect, unprotect
from granola_autostart.settings import ConfigError, load_settings, save_settings


def write_config(tmp_path, monkeypatch, text: str, encoding: str = "utf-8"):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(text, encoding=encoding)


def test_config_saved_with_a_byte_order_mark_loads(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, json.dumps({"ics_url": "https://calendar.example/a.ics"}), encoding="utf-8-sig")
    assert load_settings().calendar_urls() == ["https://calendar.example/a.ics"]


def test_plain_addresses_are_encrypted_on_load_and_other_keys_kept(tmp_path, monkeypatch):
    config = {
        "ics_url": "https://calendar.example/secret-a.ics",
        "ics_urls": ["https://calendar.example/secret-b.ics"],
        "user_email": "conrad@example.com",
        "a_key_from_the_future": 1,
    }
    write_config(tmp_path, monkeypatch, json.dumps(config))
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


def test_saving_round_trips_every_setting(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
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


def test_an_address_encrypted_elsewhere_is_a_config_error(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, json.dumps({"ics_urls_protected": ["bm90IGEgcmVhbCBibG9i"]}))
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "decrypted" in str(error.value)


def test_invalid_json_reports_position_without_contents(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, '{\n  "ics_url": "https://calendar.example/secret.ics",\n}\n')
    with pytest.raises(ConfigError) as error:
        load_settings()
    assert "line 2" in str(error.value)
    assert "secret" not in str(error.value)


def test_single_strings_are_accepted_where_lists_are_expected(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        monkeypatch,
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


def test_wrong_types_raise_a_config_error(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, json.dumps({"ics_urls": 5}))
    with pytest.raises(ConfigError):
        load_settings()
    write_config(tmp_path, monkeypatch, json.dumps({"idle_seconds": "soon"}))
    with pytest.raises(ConfigError):
        load_settings()


def test_calendar_refresh_has_a_floor(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, json.dumps({"calendar_refresh_seconds": 5}))
    assert load_settings().calendar_refresh_seconds == 60
