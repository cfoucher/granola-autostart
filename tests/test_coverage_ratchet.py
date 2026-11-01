"""The coverage ratchet and the pre-push hook's reading of what is being pushed. Coverage itself is faked:
these tests check the decisions, not a nested test run."""

from decimal import Decimal

import pytest

import coverage_ratchet
import pre_push
from coverage_ratchet import FLOOR_FILE, read_floor, truncate, verdict, write_floor

SHA = "a" * 40
OTHER = "b" * 40
ZERO = "0" * 40


def test_truncate_rounds_down_to_two_decimals():
    assert truncate(74.129) == Decimal("74.12")
    assert truncate(74.13) == Decimal("74.13")  # not 74.12 from the float's binary expansion
    assert truncate(100.0) == Decimal("100.00")


def test_the_floor_round_trips_with_lf(tmp_path):
    assert read_floor(tmp_path) is None
    write_floor(tmp_path, Decimal("71.91"))
    assert (tmp_path / FLOOR_FILE).read_bytes() == b"71.91\n"
    assert read_floor(tmp_path) == Decimal("71.91")


@pytest.mark.parametrize(
    ("measured", "strict", "ok", "says"),
    [
        ("71.90", False, False, "below the floor"),
        ("71.90", True, False, "below the floor"),
        ("71.91", False, True, "holds the floor"),
        ("71.91", True, True, "holds the floor"),
        ("72.00", False, True, "--update raises it"),
        ("72.00", True, False, "rose to 72.00%"),
    ],
)
def test_verdict(measured, strict, ok, says):
    passed, message = verdict(Decimal(measured), Decimal("71.91"), strict)
    assert passed is ok
    assert says in message


@pytest.fixture
def measured(monkeypatch):
    """What the next coverage run measures; None is a failing test."""
    result = {"value": Decimal("72.00")}
    monkeypatch.setattr(coverage_ratchet, "measure", lambda root: result["value"])
    return result


def run(tmp_path, *args):
    return coverage_ratchet.main([*args, "--root", str(tmp_path)])


def test_update_raises_the_floor_but_never_lowers_it(tmp_path, measured):
    write_floor(tmp_path, Decimal("71.91"))
    assert run(tmp_path, "--update") == 0
    assert read_floor(tmp_path) == Decimal("72.00")

    measured["value"] = Decimal("71.00")
    assert run(tmp_path, "--update") == 1
    assert read_floor(tmp_path) == Decimal("72.00")


def test_update_sets_the_first_floor(tmp_path, measured):
    assert run(tmp_path) == 1  # no floor yet, and only --update may set one
    assert run(tmp_path, "--update") == 0
    assert read_floor(tmp_path) == Decimal("72.00")


def test_strict_fails_on_a_rise_the_floor_missed(tmp_path, measured):
    write_floor(tmp_path, Decimal("71.91"))
    assert run(tmp_path) == 0
    assert run(tmp_path, "--strict") == 1
    assert read_floor(tmp_path) == Decimal("71.91")


def test_failing_tests_fail_the_ratchet(tmp_path, measured):
    write_floor(tmp_path, Decimal("71.91"))
    measured["value"] = None
    assert run(tmp_path) == 1


def test_the_result_goes_on_the_actions_summary(tmp_path, measured, monkeypatch):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(coverage_ratchet, "markdown_table", lambda root: "| table |")
    write_floor(tmp_path, Decimal("72.00"))
    assert run(tmp_path) == 0
    assert "holds the floor" in summary.read_text()
    assert "| table |" in summary.read_text()


def test_a_push_to_main_is_refused():
    assert pre_push.pushed_commits([f"refs/heads/fix {SHA} refs/heads/main {OTHER}\n"]) == ([SHA], True)
    # Deleting main is refused too.
    assert pre_push.pushed_commits([f"(delete) {ZERO} refs/heads/main {OTHER}\n"]) == ([], True)


def test_branches_and_tags_are_checked_once_each():
    lines = [
        f"refs/heads/fix {SHA} refs/heads/fix {ZERO}\n",
        f"refs/tags/v1.2.0 {SHA} refs/tags/v1.2.0 {ZERO}\n",
        f"refs/heads/other {OTHER} refs/heads/other {SHA}\n",
        f"(delete) {ZERO} refs/heads/old {OTHER}\n",
        "\n",
    ]
    assert pre_push.pushed_commits(lines) == ([SHA, OTHER], False)
