"""The coverage ratchet: the tests run under coverage, and the total may never fall below coverage-floor.txt.
The floor only ever moves up, to whatever coverage reaches.

    python tools/coverage_ratchet.py            # fails below the floor, and says when the floor could rise
    python tools/coverage_ratchet.py --update   # also raises the floor to today's coverage (or sets the first one)
    python tools/coverage_ratchet.py --strict   # also fails when coverage rose but the floor did not (PRs, pre-push)

Failing tests fail it too. The pre-push hook (tools/pre_push.py), CI, and build.ps1 all run it.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from decimal import ROUND_DOWN, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLOOR_FILE = "coverage-floor.txt"
UPDATE = "python tools/coverage_ratchet.py --update"


def truncate(percent: float) -> Decimal:
    """Two decimals, rounded down, so the floor never claims more than was measured. repr, not the float
    itself, so 74.13 stays 74.13 rather than becoming 74.12999…"""
    return Decimal(repr(percent)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def read_floor(root: Path) -> Decimal | None:
    path = root / FLOOR_FILE
    return Decimal(path.read_text().strip()) if path.exists() else None


def write_floor(root: Path, value: Decimal) -> None:
    # LF on Windows too, as .gitattributes stores it.
    (root / FLOOR_FILE).write_text(f"{value}\n", newline="\n")


def verdict(measured: Decimal, floor: Decimal, strict: bool) -> tuple[bool, str]:
    if measured < floor:
        return False, f"Coverage {measured}% is below the floor {floor}% in {FLOOR_FILE}: test the lines listed above."
    if measured == floor:
        return True, f"Coverage {measured}% holds the floor."
    if strict:
        return False, f"Coverage rose to {measured}% but {FLOOR_FILE} says {floor}: run {UPDATE} and commit it."
    return True, f"Coverage {measured}% is above the floor {floor}%: {UPDATE} raises it."


def measure(root: Path) -> Decimal | None:
    """Runs the tests in root under coverage and prints what they miss. None when a test fails.
    The data stays in root/.coverage, where CI's diff-coverage step reads it."""
    coverage = [sys.executable, "-m", "coverage"]
    if subprocess.run([*coverage, "run", "-m", "pytest", "-q"], cwd=root).returncode != 0:
        return None
    subprocess.run([*coverage, "report"], cwd=root, check=True)
    with tempfile.TemporaryDirectory() as folder:
        report = Path(folder) / "coverage.json"
        subprocess.run([*coverage, "json", "-q", "-o", str(report)], cwd=root, check=True)
        return truncate(json.loads(report.read_text())["totals"]["percent_covered"])


def markdown_table(root: Path) -> str:
    command = [sys.executable, "-m", "coverage", "report", "--format=markdown"]
    return subprocess.run(command, cwd=root, capture_output=True, text=True).stdout


def summarize(root: Path, message: str) -> None:
    """The result and the table of files with missed lines, on the GitHub Actions run's summary page."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as out:
        out.write(f"### Coverage\n\n{message}\n\n{markdown_table(root)}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tests with coverage, held to coverage-floor.txt.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="raise the floor when coverage rose")
    mode.add_argument("--strict", action="store_true", help="fail when coverage rose but the floor was not raised")
    parser.add_argument("--root", type=Path, default=ROOT, help="the checkout to test (default: this one)")
    args = parser.parse_args(argv)

    floor = read_floor(args.root)
    if floor is None and not args.update:
        print(f"No {FLOOR_FILE}: {UPDATE} sets the floor at today's coverage.", file=sys.stderr)
        return 1
    measured = measure(args.root)
    if measured is None:
        print("Tests failed, so coverage was not checked.", file=sys.stderr)
        return 1
    if args.update and (floor is None or measured > floor):
        write_floor(args.root, measured)
        ok = True
        message = f"Coverage floor raised from {floor}% to {measured}%." if floor else f"Coverage floor set at {measured}%."
    else:
        ok, message = verdict(measured, floor, args.strict)
    print(message, file=sys.stdout if ok else sys.stderr)
    summarize(args.root, message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
