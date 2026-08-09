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

Everything here skips where the source tree is not the root of a git repository —
an sdist install has no `.git`, and a tree vendored *inside* someone else's
checkout would otherwise be measured against that repository's index and rules. A
hygiene check that cannot be evaluated must not turn an ordinary install red.
"""

import shutil
import subprocess
import sys
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

#: Paths that must never be mistaken for build output. A rule broad enough to
#: catch the artifacts but also these would be worse than the bug it fixed.
NON_ARTIFACTS = (
    "src/tokenweir/contract.py",
    "schema/usage-record.v1.json",
    "README.md",
    "docs/coverage-notes.md",
    "src/tokenweir/coverage_helpers.py",
)

_GIT = shutil.which("git")


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_GIT, "-C", str(REPO_ROOT), *args],
        capture_output=True,
        text=True,
    )


def _require_git_repo() -> None:
    """Skip unless REPO_ROOT is itself the root of a git repository.

    Checking `--is-inside-work-tree` is not enough: it is also true when the tree
    has been vendored into some *other* repository, and there `git ls-files`
    returns nothing and the ignore rules consulted are the outer repo's — so the
    checks below would fail rather than skip, which is exactly what FR-006
    forbids.
    """
    if _GIT is None:
        pytest.skip("git is not available; repository hygiene cannot be checked")

    result = _git("rev-parse", "--show-toplevel")
    if result.returncode != 0:
        pytest.skip(f"{REPO_ROOT} is not a git repository (e.g. an sdist install)")

    toplevel = Path(result.stdout.strip()).resolve()
    if toplevel != REPO_ROOT:
        pytest.skip(
            f"{REPO_ROOT} is not the root of its git repository (git reports "
            f"{toplevel}) — e.g. vendored inside another checkout"
        )


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


# --- The detector itself ------------------------------------------------------
#
# Without these, stubbing `_looks_like_a_coverage_artifact` to `return False`
# leaves the whole file green: on a clean repository the tracked-file sweep below
# never sees a positive case, so the predicate is only ever exercised on paths it
# should reject. These also keep the predicate and COVERAGE_ARTIFACTS from
# drifting apart.


@pytest.mark.parametrize("path", COVERAGE_ARTIFACTS)
def test_detector_recognizes_every_coverage_artifact(path):
    assert _looks_like_a_coverage_artifact(path)


@pytest.mark.parametrize(
    "path",
    [
        "nested/dir/.coverage",
        "nested/dir/coverage.xml",
        "htmlcov/status.json",
        "reports/htmlcov/index.html",
    ],
)
def test_detector_recognizes_artifacts_in_subdirectories(path):
    assert _looks_like_a_coverage_artifact(path)


@pytest.mark.parametrize("path", NON_ARTIFACTS)
def test_detector_leaves_real_source_alone(path):
    # "coverage" appearing in a filename is not enough — only the actual output
    # shapes count.
    assert not _looks_like_a_coverage_artifact(path)


# --- The repository state -----------------------------------------------------


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


# --- The ignore rules ---------------------------------------------------------
#
# `--no-index` is essential in both directions. Without it `git check-ignore`
# consults the index and reports every *tracked* path as not-ignored whatever the
# rules say — which would make the negative test below unable to fail, and would
# make the positive test's failure message wrong for a tracked artifact.


def _check_ignore(path: str) -> subprocess.CompletedProcess:
    return _git("check-ignore", "-v", "--no-index", path)


@pytest.mark.parametrize("artifact", COVERAGE_ARTIFACTS)
def test_coverage_artifacts_are_ignored(artifact):
    """The rules, not just the cleanup — this is what stops the bug recurring.

    `git check-ignore` evaluates the ignore rules against a pathname whether or
    not it exists, so this covers every artifact a coverage run can leave behind
    without having to generate them.

    The match must come from the repository's own root `.gitignore`. coverage.py
    writes an `htmlcov/.gitignore` containing `*` when it generates an HTML
    report, so once that directory exists on disk a weaker assertion would pass
    on the strength of coverage's own file rather than ours.
    """
    _require_git_repo()

    result = _check_ignore(artifact)
    assert result.returncode == 0, (
        f"{artifact} is not ignored; a coverage run would leave the tree dirty "
        "and the next `git add -A` would commit it"
    )

    source = result.stdout.split(":", 1)[0]
    assert source == ".gitignore", (
        f"{artifact} is ignored by {source!r}, not the repository's own "
        ".gitignore — the rule this project controls is missing"
    )


@pytest.mark.parametrize("path", NON_ARTIFACTS[:3])
def test_source_and_schema_are_not_ignored(path):
    """The converse: an over-broad rule that masked real source would be worse
    than the bug it fixed."""
    _require_git_repo()

    result = _check_ignore(path)
    assert result.returncode != 0, f"{path} is ignored but must not be: {result.stdout.strip()}"


# --- The skip behaviour itself (FR-006) ---------------------------------------


def test_missing_git_skips_rather_than_failing(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_GIT", None)
    with pytest.raises(pytest.skip.Exception):
        _require_git_repo()


def test_non_repository_skips_rather_than_failing(monkeypatch):
    def not_a_repo(*args: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, returncode=128, stdout="", stderr="fatal")

    monkeypatch.setattr(sys.modules[__name__], "_git", not_a_repo)
    with pytest.raises(pytest.skip.Exception):
        _require_git_repo()


def test_tree_vendored_inside_another_repository_skips(monkeypatch):
    """The FR-006 case that a bare `--is-inside-work-tree` check gets wrong.

    In a tree copied into someone else's checkout, `git ls-files` returns nothing
    and the ignore rules are theirs — so the checks above would fail rather than
    skip.
    """

    def outer_repo(*args: str) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            args, returncode=0, stdout="/somewhere/else\n", stderr=""
        )

    monkeypatch.setattr(sys.modules[__name__], "_git", outer_repo)
    with pytest.raises(pytest.skip.Exception):
        _require_git_repo()
