import json

import pytest

from settings import ConfigError, load_settings


def write_config(tmp_path, monkeypatch, text: str, encoding: str = "utf-8"):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(text, encoding=encoding)


def test_config_saved_with_a_byte_order_mark_loads(tmp_path, monkeypatch):
    write_config(tmp_path, monkeypatch, json.dumps({"ics_url": "https://calendar.example/a.ics"}), encoding="utf-8-sig")
    assert load_settings().ics_url == "https://calendar.example/a.ics"


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
    assert settings.ics_urls == ("https://calendar.example/b.ics",)
    assert settings.ignore_title_substrings == ("Lunch",)


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
