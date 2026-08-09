"""Repository hygiene (TOKWEIR-12).

These assert a property of *the repository*, not of the library: no coverage
artifact is tracked, and the ignore rules that keep it that way are in force.

A 53 KB `.coverage` SQLite binary was committed by a `git add -A` after a coverage
run, because `.gitignore` covered bytecode, build metadata, tool caches and
virtualenvs but not coverage output. Deleting the file without closing that gap
would have left the next `git add -A` free to re-commit it, so the rules and this
guard are the actual fix; removing the file was only the cleanup.

`tokenweir` is the intended first open-source release (ADR-0001 Pillar 1), which
is why a regenerable build artifact carrying absolute developer paths does not
belong in the tree.

Everything here skips where git is unavailable or the source tree is not a
repository — an sdist install has no `.git`, and a hygiene check that cannot be
evaluated must not turn an ordinary install red.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

#: The repository is resolved from this file's own location, never from the
#: process CWD — otherwise the result would depend on where pytest was invoked
#: from, and a stray copy of the tree elsewhere on disk could be inspected by
#: mistake.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Paths a coverage run can produce. Each is a distinct pattern: `.coverage` does
#: not match `.coverage.*`, which is what coverage writes per-process in parallel
#: mode. `coverage.xml` is here because mado/config.yaml declares
#: `coverage.format: xml` for this project.
COVERAGE_ARTIFACTS = (
    ".coverage",
    ".coverage.somehost.12345.678901",
    "htmlcov/index.html",
    "coverage.xml",
)

_GIT = shutil.which("git")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_GIT, "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
    )


def _require_git_repo() -> None:
    if _GIT is None:
        pytest.skip("git is not available; repository hygiene cannot be checked")
    result = _git("rev-parse", "--is-inside-work-tree")
    if result.returncode != 0 or result.stdout.strip() != "true":
        pytest.skip(f"{REPO_ROOT} is not a git repository (e.g. an sdist install)")


def _tracked_files() -> list[str]:
    result = _git("ls-files", "-z")
    assert result.returncode == 0, f"git ls-files failed: {result.stderr}"
    return [path for path in result.stdout.split("\0") if path]


def _looks_like_a_coverage_artifact(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name == ".coverage"
        or name.startswith(".coverage.")
        or name == "coverage.xml"
        or "htmlcov" in path.split("/")[:-1]
    )


def test_no_coverage_artifact_is_tracked():
    """The defect itself: a tracked coverage artifact must fail the suite.

    Checks what git *tracks*, not what is on disk — `.coverage` may legitimately
    exist locally after a coverage run, and that is fine. Being in the index is
    the bug.
    """
    _require_git_repo()

    offenders = sorted(p for p in _tracked_files() if _looks_like_a_coverage_artifact(p))
    assert offenders == [], (
        "coverage artifact(s) are tracked by git: "
        + ", ".join(offenders)
        + " — run `git rm --cached <path>`; they are regenerable build output"
    )


def test_the_sweep_actually_sees_the_repository():
    """Guards the check above: if `git ls-files` returned nothing, it would pass
    vacuously and protect nothing."""
    _require_git_repo()

    tracked = set(_tracked_files())
    assert {"pyproject.toml", "README.md", ".gitignore"} <= tracked


@pytest.mark.parametrize("artifact", COVERAGE_ARTIFACTS)
def test_coverage_artifacts_are_ignored(artifact):
    """The rules, not just the cleanup — this is what stops the bug recurring.

    `git check-ignore` evaluates the ignore rules against a pathname whether or
    not it exists, so this covers every artifact a coverage run can leave behind
    without having to generate them.
    """
    _require_git_repo()

    result = _git("check-ignore", "-q", artifact)
    assert result.returncode == 0, (
        f"{artifact} is not ignored; a coverage run would leave the tree dirty "
        "and the next `git add -A` would commit it"
    )


def test_source_and_schema_are_not_ignored():
    """The converse: an over-broad rule that masked real source would be worse
    than the bug it fixed."""
    _require_git_repo()

    for path in ("src/tokenweir/contract.py", "schema/usage-record.v1.json", "README.md"):
        result = _git("check-ignore", "-q", path)
        assert result.returncode != 0, f"{path} is ignored but must not be"
