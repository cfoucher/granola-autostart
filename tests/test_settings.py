import json
import os
import re
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from granola_autostart.secret_box import protect, unprotect
from granola_autostart.settings import (
    REPLACE_ATTEMPTS,
    STALE_TEMPORARY_SECONDS,
    Calendar,
    ConfigError,
    Settings,
    clean_addresses,
    config_path,
    load_settings,
    saved_appearance,
    saved_flag,
    save_settings,
    sweep_stale_temporaries,
    update_config,
    write_json,
)


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
    assert "ics_url" not in saved and "ics_urls" not in saved and "ics_urls_protected" not in saved
    assert [entry["enabled"] for entry in saved["calendars"]] == [True, True]
    assert saved["a_key_from_the_future"] == 1
    reloaded = load_settings()
    assert reloaded.calendar_urls() == ["https://calendar.example/secret-a.ics", "https://calendar.example/secret-b.ics"]
    assert reloaded.user_email == "conrad@example.com"


def test_saving_round_trips_every_setting():
    settings = load_settings()
    assert settings.setup_complete is False
    settings.calendars = (Calendar("https://calendar.example/c.ics"),)
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
    assert "ics_urls" not in saved and "ics_urls_protected" not in saved
    assert [unprotect(entry["address_protected"]) for entry in saved["calendars"]] == expected
    assert load_settings().calendar_urls() == expected


WORK = "https://calendar.example/work.ics"
WATCH = "https://calendar.example/watch.ics"


def test_the_old_flat_list_becomes_calendars_that_are_on_and_automatic_until_the_first_save(tmp_path):
    config = {
        "ics_urls_protected": [protect(WORK), protect(WATCH)],
        "user_email": "conrad@example.com",
        "countdown_seconds": 20,
        "a_key_from_the_future": 1,
    }
    write_config(tmp_path, json.dumps(config))
    settings = load_settings()
    assert settings.calendars == (Calendar(WORK, True, None), Calendar(WATCH, True, None))
    assert settings.user_email == "conrad@example.com"
    assert "ics_urls_protected" in json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))  # loading alone doesn't rewrite it
    save_settings(settings)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert "ics_urls_protected" not in saved
    assert [(unprotect(entry["address_protected"]), entry["enabled"], entry["declined_by"]) for entry in saved["calendars"]] == [
        (WORK, True, None),
        (WATCH, True, None),
    ]
    assert (saved["user_email"], saved["countdown_seconds"], saved["a_key_from_the_future"]) == ("conrad@example.com", 20, 1)
    assert load_settings() == settings


def test_a_calendar_removed_after_the_upgrade_does_not_come_back_from_the_old_list(tmp_path):
    write_config(tmp_path, json.dumps({"ics_urls_protected": [protect(WORK), protect(WATCH)]}))
    settings = load_settings()
    save_settings(Settings(calendars=(settings.calendars[0],)))
    assert load_settings().calendar_urls() == [WORK]


def test_calendars_keep_their_switch_and_declined_addresses(tmp_path):
    settings = Settings(
        calendars=(Calendar(WORK, True, ("me@example.com", "alias@example.ca")), Calendar(WATCH, False, None)),
        user_email="old@example.com",
    )
    save_settings(settings)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert [(entry["enabled"], entry["declined_by"]) for entry in saved["calendars"]] == [(True, ["me@example.com", "alias@example.ca"]), (False, None)]
    assert WORK not in (tmp_path / "config.json").read_text(encoding="utf-8")
    assert load_settings() == settings
    assert settings.calendar_urls() == [WORK, WATCH] and settings.enabled_urls() == [WORK]


def test_the_same_address_listed_more_than_once_is_one_calendar_and_the_first_entry_wins(tmp_path):
    config = {
        "calendars": [
            {"address_protected": protect(WORK), "enabled": False, "declined_by": ["me@example.com"]},
            {"address_protected": protect(WATCH)},
            {"address_protected": protect(f" {WORK} ")},
        ],
        "ics_urls_protected": [protect(WATCH)],
        "ics_urls": [WORK, WATCH],
    }
    write_config(tmp_path, json.dumps(config))
    assert load_settings().calendars == (Calendar(WORK, False, ("me@example.com",)), Calendar(WATCH, True, None))
    assert len(json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["calendars"]) == 2


def test_missing_switches_and_lists_mean_on_and_automatic_and_an_empty_list_is_automatic(tmp_path):
    entries = [
        {"address_protected": protect(WORK)},
        {"address_protected": protect(WATCH), "enabled": None, "declined_by": []},
        {"address_protected": protect("https://calendar.example/home.ics"), "enabled": " False ", "declined_by": ["", "  "]},
    ]
    write_config(tmp_path, json.dumps({"calendars": entries}))
    assert [(calendar.enabled, calendar.declined_by) for calendar in load_settings().calendars] == [(True, None), (True, None), (False, None)]


def test_declined_addresses_are_trimmed_and_a_repeat_ignoring_case_is_dropped(tmp_path):
    entry = {"address_protected": protect(WORK), "declined_by": [" Me@Example.com ", "me@example.com", "alias@example.ca"]}
    write_config(tmp_path, json.dumps({"calendars": [entry]}))
    assert load_settings().calendars[0].declined_by == ("Me@Example.com", "alias@example.ca")
    assert clean_addresses(None) is None and clean_addresses([" "]) is None


def test_addresses_can_also_come_out_in_lower_case_without_blanks_or_repeats():
    assert clean_addresses([" Me@Example.com ", None, "", "me@EXAMPLE.com", "Alias@Example.ca"], lower=True) == ("me@example.com", "alias@example.ca")
    assert clean_addresses([" Me@Example.com ", "me@EXAMPLE.com"]) == ("Me@Example.com",)  # as written, by default
    assert clean_addresses([None, " "], lower=True) is None


@pytest.mark.parametrize(
    ("fields", "named"),
    [
        ({"address_protected": 5}, "calendars[1].address_protected"),
        ({"address_protected": "  "}, "calendars[1].address_protected"),
        ({"declined_by": "me@example.com"}, "calendars[1].declined_by must be null or a list of text values"),
        ({"declined_by": ["me@example.com", 5]}, "calendars[1].declined_by"),
        ({"enabled": "yes"}, "calendars[1].enabled must be true or false"),
        ({"enabled": 0}, "calendars[1].enabled"),
    ],
)
def test_a_malformed_calendar_is_a_config_error_that_says_which_and_never_shows_its_address(fields, named, tmp_path):
    write_config(tmp_path, json.dumps({"calendars": [{"address_protected": protect(WORK), **fields}]}))
    with pytest.raises(ConfigError, match=re.escape(named)) as error:
        load_settings()
    assert "work.ics" not in str(error.value)


def test_calendars_that_are_not_a_list_of_objects_with_an_address_are_a_config_error(tmp_path):
    for calendars, named in (
        ("work", "calendars must be a list"),
        (["work"], "calendars[1] must be an object"),
        ([{"address_protected": protect(WORK)}, {"enabled": True}], "calendars[2].address_protected"),
    ):
        write_config(tmp_path, json.dumps({"calendars": calendars}))
        with pytest.raises(ConfigError, match=re.escape(named)):
            load_settings()
    write_config(tmp_path, json.dumps({"calendars": None}))
    assert load_settings().calendars == ()


def test_a_calendar_encrypted_elsewhere_is_a_config_error_naming_the_list_to_clear(tmp_path):
    write_config(tmp_path, json.dumps({"calendars": [{"address_protected": "bm90IGEgcmVhbCBibG9i"}]}))
    with pytest.raises(ConfigError, match="delete the calendars list") as error:
        load_settings()
    assert "decrypted" in str(error.value)
    write_config(tmp_path, json.dumps({"ics_urls_protected": ["bm90IGEgcmVhbCBibG9i"]}))
    with pytest.raises(ConfigError, match="delete the ics_urls_protected list"):
        load_settings()


def test_a_first_run_writes_an_empty_calendar_list():
    assert load_settings().calendars == ()
    assert json.loads(config_path().read_text(encoding="utf-8"))["calendars"] == []


def test_calendar_refresh_has_a_floor(tmp_path):
    write_config(tmp_path, json.dumps({"calendar_refresh_seconds": 5}))
    assert load_settings().calendar_refresh_seconds == 60


def test_null_numbers_and_flags_mean_the_defaults(tmp_path):
    write_config(tmp_path, json.dumps({"idle_seconds": None, "poll_seconds": None, "dry_run": None, "setup_complete": None}))
    loaded = load_settings()
    assert (loaded.idle_seconds, loaded.poll_seconds, loaded.dry_run, loaded.setup_complete) == (300, 5, False, False)


def test_calls_default_to_zoom_only_for_a_new_install_and_for_an_upgrade_alike(tmp_path):
    assert Settings().calls == {"zoom": True, "teams": False, "meet": False}
    assert load_settings().calls == {"zoom": True, "teams": False, "meet": False}  # no config.json yet
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["calls"] == {"zoom": True, "teams": False, "meet": False}
    write_config(tmp_path, json.dumps({"ics_urls_protected": [], "zoom_lookahead_minutes": 7}))  # a config from before calls
    upgraded = load_settings()
    assert upgraded.calls == {"zoom": True, "teams": False, "meet": False} and upgraded.zoom_lookahead_minutes == 7
    write_config(tmp_path, json.dumps({"calls": None}))
    assert load_settings().calls == {"zoom": True, "teams": False, "meet": False}


def test_the_calls_in_config_json_are_loaded_and_saved(tmp_path):
    write_config(tmp_path, json.dumps({"calls": {"zoom": False, "teams": True, "meet": True}}))
    settings = load_settings()
    assert settings.calls == {"zoom": False, "teams": True, "meet": True}
    settings.calls["meet"] = False
    save_settings(settings)
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["calls"] == {"zoom": False, "teams": True, "meet": False}
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": False}


def test_a_provider_missing_from_calls_keeps_its_default_and_an_unknown_one_is_ignored(tmp_path):
    write_config(tmp_path, json.dumps({"calls": {"teams": True, "webex": True, "zoom": None}}))
    settings = load_settings()
    assert settings.calls == {"zoom": True, "teams": True, "meet": False}
    save_settings(settings)
    assert "webex" not in json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["calls"]


def test_a_call_switch_written_as_text_is_read_like_other_flags(tmp_path):
    write_config(tmp_path, json.dumps({"calls": {"zoom": " FALSE ", "teams": "true"}}))
    assert load_settings().calls == {"zoom": False, "teams": True, "meet": False}


def test_a_call_switch_that_is_not_true_or_false_is_a_config_error(tmp_path):
    for provider, value in (("teams", "yes"), ("meet", 1), ("zoom", ["on"]), ("zoom", "")):
        write_config(tmp_path, json.dumps({"calls": {provider: value}}))
        with pytest.raises(ConfigError, match=f"calls.{provider} must be true or false"):
            load_settings()


def test_calls_that_are_not_a_set_of_switches_are_a_config_error(tmp_path):
    for value in (True, "zoom", ["zoom"], 3):
        write_config(tmp_path, json.dumps({"calls": value}))
        with pytest.raises(ConfigError, match="calls must be a set of switches"):
            load_settings()


def test_autostart_notes_is_on_for_a_new_install_and_for_an_upgrade_alike(tmp_path):
    assert Settings().autostart_notes is True
    assert load_settings().autostart_notes is True  # no config.json yet
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["autostart_notes"] is True
    write_config(tmp_path, json.dumps({"ics_urls_protected": []}))  # a config from before the switch
    assert load_settings().autostart_notes is True
    write_config(tmp_path, json.dumps({"autostart_notes": None}))
    assert load_settings().autostart_notes is True


def test_autostart_notes_off_is_loaded_and_saved_and_written_as_text_like_other_flags(tmp_path):
    write_config(tmp_path, json.dumps({"autostart_notes": False}))
    settings = load_settings()
    assert settings.autostart_notes is False
    save_settings(settings)
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["autostart_notes"] is False
    write_config(tmp_path, json.dumps({"autostart_notes": " FALSE "}))
    assert load_settings().autostart_notes is False


def test_autostart_notes_that_is_not_true_or_false_is_a_config_error(tmp_path):
    for value in ("yes", 1, ["on"], ""):
        write_config(tmp_path, json.dumps({"autostart_notes": value}))
        with pytest.raises(ConfigError, match="autostart_notes must be true or false"):
            load_settings()


def test_appearance_is_auto_unless_the_file_says_light_or_dark(tmp_path):
    assert Settings().appearance == "auto"
    assert load_settings().appearance == "auto"  # no config.json yet
    for value in (None, "", "sepia", 3, True, ["dark"], "autumn"):
        write_config(tmp_path, json.dumps({"appearance": value}))
        assert load_settings().appearance == "auto"
    for value, appearance in (("dark", "dark"), (" Light ", "light"), ("AUTO", "auto")):
        write_config(tmp_path, json.dumps({"appearance": value}))
        assert load_settings().appearance == appearance


def test_appearance_is_saved_in_the_file_under_its_own_name(tmp_path):
    settings = load_settings()
    settings.appearance = "dark"
    save_settings(settings)
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))["appearance"] == "dark"
    assert load_settings().appearance == "dark"


def test_updating_one_key_keeps_every_other_key_as_it_is_on_disk(tmp_path):
    config = {
        "ics_urls_protected": [protect("https://calendar.example/a.ics")],
        "ics_url": "https://calendar.example/plain.ics",  # not encrypted yet: only a load does that
        "countdown_seconds": 20,
        "a_key_from_the_future": {"nested": [1, 2]},
    }
    write_config(tmp_path, json.dumps(config))
    update_config("autostart_notes", False)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert saved == {**config, "autostart_notes": False}
    update_config("autostart_notes", True)
    update_config("appearance", "dark")
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {**config, "autostart_notes": True, "appearance": "dark"}


def test_updating_a_key_writes_the_file_when_there_is_none(tmp_path):
    update_config("autostart_notes", False)
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"autostart_notes": False}
    assert load_settings().autostart_notes is False


def test_a_file_that_cannot_be_read_is_never_written_over_by_an_update(tmp_path):
    for text in ("{ broken", "[1, 2]"):
        write_config(tmp_path, text)
        with pytest.raises(ConfigError):
            update_config("autostart_notes", False)
        assert (tmp_path / "config.json").read_text(encoding="utf-8") == text


def test_one_saved_flag_is_read_alone_and_falls_back_to_its_default(tmp_path, monkeypatch):
    assert saved_flag("autostart_notes", True) is True  # no file
    write_config(tmp_path, json.dumps({"autostart_notes": False, "idle_seconds": "not a number"}))
    assert saved_flag("autostart_notes", True) is False  # nothing else in the file has to load
    write_config(tmp_path, json.dumps({"autostart_notes": "false"}))
    assert saved_flag("autostart_notes", True) is False
    for text in (json.dumps({"autostart_notes": "maybe"}), "{ broken", "[]"):
        write_config(tmp_path, text)
        assert saved_flag("autostart_notes", True) is True and saved_flag("autostart_notes", False) is False
    monkeypatch.delenv("GRANOLA_AUTOSTART_HOME")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert saved_flag("autostart_notes", True) is True  # no folder to look in


def test_the_saved_appearance_is_read_alone_and_is_auto_when_the_file_cannot_give_it(tmp_path, monkeypatch):
    assert saved_appearance() == "auto"  # no file
    write_config(tmp_path, json.dumps({"appearance": "dark", "calls": 5, "idle_seconds": "not a number"}))
    assert saved_appearance() == "dark"  # nothing else in the file has to load, or be decrypted
    for text in ("{ broken", "[]", json.dumps({"appearance": "sepia"})):
        write_config(tmp_path, text)
        assert saved_appearance() == "auto"
    write_config(tmp_path, json.dumps({"appearance": "dark"}))
    monkeypatch.delenv("GRANOLA_AUTOSTART_HOME")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert saved_appearance() == "auto"


def test_the_other_zoom_settings_keep_their_names_in_the_file(tmp_path):
    write_config(tmp_path, json.dumps({"zoom_lookahead_minutes": 3, "fresh_zoom_seconds": 45}))
    settings = load_settings()
    assert (settings.zoom_lookahead_minutes, settings.fresh_zoom_seconds) == (3, 45)
    save_settings(settings)
    saved = json.loads((tmp_path / "config.json").read_text(encoding="utf-8"))
    assert (saved["zoom_lookahead_minutes"], saved["fresh_zoom_seconds"]) == (3, 45)


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


def test_a_save_writes_through_a_temporary_file_named_for_its_process_and_thread(tmp_path, monkeypatch):
    seen = []
    real_replace = Path.replace

    def replace(self, target):
        seen.append(self.name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    write_json(tmp_path / "config.json", {"a": 1})
    here = f"config.json.{os.getpid()}.{threading.get_ident()}.tmp"
    assert seen == [here]
    other = []
    thread = threading.Thread(target=lambda: (write_json(tmp_path / "config.json", {"a": 2}), other.extend(seen)))
    thread.start()
    thread.join(10)
    assert other[-1].startswith(f"config.json.{os.getpid()}.") and other[-1] != here  # another thread's own file
    assert list(tmp_path.glob("*.tmp")) == []
    assert json.loads((tmp_path / "config.json").read_text(encoding="utf-8")) == {"a": 2}


def test_two_processes_saving_the_same_file_at_once_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    # What another process would write through, started while this one is between writing and replacing.
    path = tmp_path / "config.json"
    real_replace = Path.replace
    rival = []

    def replace(self, target):
        if not rival:
            rival.append(self.with_name(self.name.replace(f".{os.getpid()}.", ".999999.")))
            rival[0].write_text('{"from": "the other process"}', encoding="utf-8")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    write_json(path, {"from": "this process"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"from": "this process"}
    assert json.loads(rival[0].read_text(encoding="utf-8")) == {"from": "the other process"}  # not overwritten, nor taken


def test_a_save_that_fails_leaves_no_temporary_file_behind(tmp_path, replaces):
    replaces.blocked = [PermissionError() for _ in range(REPLACE_ATTEMPTS)]
    with pytest.raises(PermissionError):
        write_json(tmp_path / "config.json", {"a": 1})
    assert list(tmp_path.iterdir()) == []


def test_a_temporary_file_that_cannot_be_deleted_does_not_hide_why_the_save_failed(tmp_path, replaces, monkeypatch):
    replaces.blocked = [PermissionError("held open") for _ in range(REPLACE_ATTEMPTS)]

    def stuck(self, missing_ok=False):
        raise PermissionError("also held open")

    monkeypatch.setattr(Path, "unlink", stuck)
    with pytest.raises(PermissionError, match="^held open$"):
        write_json(tmp_path / "config.json", {"a": 1})


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


# Temporary files a save left behind


def leave(folder: Path, name: str, age_seconds: float) -> Path:
    path = folder / name
    path.write_text("{", encoding="utf-8")
    then = time.time() - age_seconds
    os.utime(path, (then, then))
    return path


def test_the_leftovers_of_saves_that_never_finished_are_swept_and_nothing_else(tmp_path):
    old = STALE_TEMPORARY_SECONDS + 60
    gone = [
        leave(tmp_path, "config.json.4120.9876.tmp", old),
        leave(tmp_path, "state.json.77.5.tmp", old),
        leave(tmp_path, "update.json.8.13200.tmp", old),
    ]
    kept = [
        leave(tmp_path, "status.json.4120.9876.tmp", 5),  # a save under way, maybe in another process
        leave(tmp_path, "status.json.4120.9876.tmp.bak", old),
        leave(tmp_path, "config.json", old),
        leave(tmp_path, "log.txt.4120.9876.tmp", old),  # not a file write_json writes
        leave(tmp_path, "config.json.4120.tmp", old),
        leave(tmp_path, "config.json.x.9876.tmp", old),
        leave(tmp_path, "notes.tmp", old),
    ]
    (tmp_path / "updates").mkdir()
    leave(tmp_path / "updates", "config.json.1.2.tmp", old)  # only the app's own folder
    folder_like = tmp_path / "state.json.1.1.tmp"
    folder_like.mkdir()
    os.utime(folder_like, (time.time() - old,) * 2)
    sweep_stale_temporaries()
    assert not any(path.exists() for path in gone)
    assert all(path.exists() for path in kept) and folder_like.is_dir()
    assert (tmp_path / "updates" / "config.json.1.2.tmp").exists()


def test_sweeping_with_no_app_folder_yet_does_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("GRANOLA_AUTOSTART_HOME", str(tmp_path / "not-yet"))
    sweep_stale_temporaries()
    assert not (tmp_path / "not-yet").exists()


def test_a_leftover_that_cannot_be_deleted_is_logged_and_the_rest_still_go(tmp_path, monkeypatch, caplog):
    old = STALE_TEMPORARY_SECONDS + 60
    stuck = leave(tmp_path, "config.json.1.1.tmp", old)
    other = leave(tmp_path, "state.json.2.2.tmp", old)
    real_unlink = Path.unlink

    def unlink(path, missing_ok=False):
        if path.name == stuck.name:
            raise PermissionError("held open")
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    sweep_stale_temporaries()
    assert stuck.exists() and not other.exists()
    assert "could not delete config.json.1.1.tmp (PermissionError)" in caplog.text


def test_the_temporary_file_a_save_writes_through_is_what_the_sweep_looks_for(tmp_path, monkeypatch):
    seen = []
    real_replace = Path.replace
    monkeypatch.setattr(Path, "replace", lambda self, target: seen.append(self) or real_replace(self, target))
    write_json(tmp_path / "update.json", {"a": 1})
    [temporary] = seen
    old = time.time() - STALE_TEMPORARY_SECONDS - 60
    temporary.write_text("{", encoding="utf-8")  # as if the save had stopped here
    os.utime(temporary, (old, old))
    sweep_stale_temporaries()
    assert not temporary.exists() and (tmp_path / "update.json").exists()
