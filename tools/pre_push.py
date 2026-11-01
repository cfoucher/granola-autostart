"""The pre-push hook, which .githooks/pre-push runs. Nothing is pushed straight to main, and every commit being
pushed must pass ruff, its tests, and the coverage ratchet (--strict). Each commit is checked from an export of
exactly that commit, so uncommitted edits neither help nor hurt. git push --no-verify skips all of it.

Set up once per clone:  git config core.hooksPath .githooks
"""

import io
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from pathlib import Path

import coverage_ratchet

PROTECTED = "refs/heads/main"


def pushed_commits(lines: Iterable[str]) -> tuple[list[str], bool]:
    """From git's "<local ref> <local sha> <remote ref> <remote sha>" lines: the commits to check, once each,
    and whether the push touches main. A deleted ref has an all-zero sha and nothing to check."""
    commits: list[str] = []
    to_main = False
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            continue
        _local_ref, local_sha, remote_ref, _remote_sha = fields
        to_main = to_main or remote_ref == PROTECTED
        if local_sha.strip("0") and local_sha not in commits:
            commits.append(local_sha)
    return commits, to_main


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def export(commit: str, into: Path) -> None:
    archive = subprocess.run(["git", "archive", "--format=tar", commit], capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")


def check(commit: str, head: str) -> bool:
    with tempfile.TemporaryDirectory(prefix="granola-pre-push-", ignore_cleanup_errors=True) as folder:
        root = Path(folder)
        export(commit, root)
        floor = coverage_ratchet.read_floor(root)
        if floor is None:
            print(f"{commit[:7]} predates the coverage ratchet; not checked.")
            return True
        print(f"== {commit[:7]}: ruff")
        if subprocess.run([sys.executable, "-m", "ruff", "check"], cwd=root).returncode != 0:
            return False
        print(f"== {commit[:7]}: tests and coverage")
        measured = coverage_ratchet.measure(root)
    if measured is None:
        print("Tests failed.", file=sys.stderr)
        return False
    ok, message = coverage_ratchet.verdict(measured, floor, strict=True)
    if not ok and measured > floor and commit == head and not git("status", "--porcelain", "--", coverage_ratchet.FLOOR_FILE):
        # Do the --update for them; the push still stops, since the commit being pushed has the old floor.
        coverage_ratchet.write_floor(coverage_ratchet.ROOT, measured)
        message = f"Coverage rose from {floor}% to {measured}%. {coverage_ratchet.FLOOR_FILE} now says so: commit it and push again."
    print(message, file=sys.stdout if ok else sys.stderr)
    return ok


def main() -> int:
    commits, to_main = pushed_commits(sys.stdin)
    if to_main:
        print("Changes reach main through a pull request (git push --no-verify to override).", file=sys.stderr)
        return 1
    head = git("rev-parse", "HEAD")
    return 0 if all(check(commit, head) for commit in commits) else 1


if __name__ == "__main__":
    raise SystemExit(main())
