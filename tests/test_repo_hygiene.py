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

import re
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

#: Real paths in this repository that must never be ignored. A rule broad enough
#: to catch the artifacts but also these would be worse than the bug it fixed.
REAL_SOURCE_PATHS = (
    "src/tokenweir/contract.py",
    "schema/usage-record.v1.json",
    "README.md",
)

#: Paths the detector must not mistake for build output. Includes the real paths
#: above plus hypothetical ones chosen to be adversarial — "coverage" appearing in
#: a name is not enough to make something an artifact.
NON_ARTIFACTS = (
    *REAL_SOURCE_PATHS,
    "docs/coverage-notes.md",
    "src/tokenweir/coverage_helpers.py",
    "tests/test_coverage_report.py",
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
        # Any path segment, including the last: a directory named htmlcov is what
        # coverage writes, and a plain file of that name is artifact-shaped too.
        or "htmlcov" in path.split("/")
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


@pytest.mark.parametrize("path", REAL_SOURCE_PATHS)
def test_source_and_schema_are_not_ignored(path):
    """The converse: an over-broad rule that masked real source would be worse
    than the bug it fixed.

    Asserts exit code 1 specifically — `git check-ignore` returns 1 for "not
    ignored" and 128 for an error, and a future git that rejected an option here
    would otherwise make this pass for the wrong reason.
    """
    _require_git_repo()

    result = _check_ignore(path)
    assert result.returncode == 1, (
        f"{path} is ignored but must not be: {result.stdout.strip() or result.stderr.strip()}"
    )


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


# --- FR-004 end to end --------------------------------------------------------


def test_generated_coverage_artifacts_do_not_dirty_the_tree(tmp_path):
    """Generate the artifact shapes and confirm git reports nothing new.

    The tests above assert the ignore *rules*; this asserts the property the story
    actually states — "git status is clean after a coverage run". It writes the
    files rather than running coverage, so it needs no coverage tooling (which is
    absent from the [dev] extras) and cannot depend on a coverage run's timing.

    Safety: only paths that do not already exist are created, and only those are
    removed afterwards, so a developer's real .coverage is never touched. The
    assertion looks for the created paths specifically instead of requiring the
    whole tree to be clean, so unrelated work in progress does not fail it.
    """
    _require_git_repo()

    candidates = [
        REPO_ROOT / ".coverage",
        REPO_ROOT / ".coverage.tokweir12selftest.4242.abcdef",
        REPO_ROOT / "coverage.xml",
        REPO_ROOT / "htmlcov" / "index.html",
    ]

    created: list[Path] = []
    created_dirs: list[Path] = []
    try:
        for path in candidates:
            if path.exists():
                continue  # never clobber a real artifact
            if not path.parent.exists():
                path.parent.mkdir(parents=True)
                created_dirs.append(path.parent)
            path.write_text("generated by test_repo_hygiene\n", encoding="utf-8")
            created.append(path)

        assert created, "expected to create at least one artifact to test with"

        status = _git("status", "--porcelain", "--untracked-files=all")
        assert status.returncode == 0, status.stderr

        relative = {str(p.relative_to(REPO_ROOT)) for p in created}
        dirty = sorted(
            line for line in status.stdout.splitlines()
            if any(name in line for name in relative)
        )
        assert dirty == [], (
            "a coverage run dirties the tree; git reports: " + "; ".join(dirty)
        )
    finally:
        for path in created:
            path.unlink(missing_ok=True)
        for directory in created_dirs:
            if not any(directory.iterdir()):
                directory.rmdir()


# --- What the README must not stop saying (TOKWEIR-10) ------------------------


def test_the_readme_documents_reconciling_a_mismatched_database():
    """FR-026. Adoption is not the end of the job, and the README used to imply it
    was — "Nothing is re-applied and nothing is dropped … What the runner does from
    then on is ordinary" is true and, read by an operator pointing `apply` at the
    live gateway database, wrong: every migration is an idempotent no-op against
    objects that already exist, so the run succeeds and changes nothing.

    Asserted rather than left to review because this repository already treats
    README content as testable (`test_optional_drivers.py`), and because the
    section is the only place an operator is told the command exists.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    assert "Reconciling a database that does not match" in readme
    assert "python -m tokenweir.migrations reconcile" in readme
    for claim in (
        # The two classifications, which are the whole contract of the plan.
        "AUTOMATIC",
        "MANUAL",
        # That planning is the default and the safe order is plan-first.
        "restored dump",
        # SC-006: the residual this branch could not close. The legacy shape the
        # suite reconciles against is a reconstruction, and an operator about to
        # run this against production is owed that in the place they will read it.
        "LEGACY_SQL",
        "ai_gateway_metrics",
    ):
        assert claim in readme, f"README no longer says {claim!r}"


def test_the_readme_names_the_ways_a_reconciled_database_differs():
    """A reconciled database is not byte-identical to a fresh one, in **four** ways
    that are decisions rather than defects. Each is named here because each was
    found by a review reading the code rather than the docs — `BIGSERIAL` in the
    story, the kept gateway columns in FR-009, the column ordering that a test
    docstring claimed did not exist, and the surviving index and constraint names
    that the docstring's replacement then miscounted the same way."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")

    # Bounded at the next heading. Splitting on the opening sentence alone ran the
    # "section" to end of file, so `grants` — which occurs three more times in the
    # paragraphs after it — stayed green with the whole bullet list deleted. An
    # anchor satisfied by unrelated text is not an anchor.
    section = readme.split("A reconciled database is not identical to a fresh one")[-1]
    section = section.split("\n#")[0]

    # Anchors, not prose. Following the rule `tests/conftest.py`'s
    # `OptionalDriver.anchors` established for the same reason (TOKWEIR-31): a check
    # against whole English sentences fails on a reword, which trains the next
    # person to delete the check rather than keep the claim. Each of these is an
    # identifier the claim is *about*, and none survives a reword that drops the
    # deviation it names — including the fourth, which had no anchor at all until
    # review 4 counted them.
    for anchor in ("BIGSERIAL", "populate", "order", "matched by what they"):
        assert anchor in section, f"README no longer names {anchor!r}"


# --- No credential is ever committed (TOKWEIR-9, FR-024) ----------------------
#
# TOKWEIR-9 is the one story in this project that handles a live API credential:
# the parity spike measures a Max transcript against the provider's own tokenizer,
# which needs a key. The key is delivered into the pod out-of-band and read from
# the environment or a file outside the tree — but "outside the tree" is a
# convention, and a convention is one `git add -A` away from being untrue.
#
# So the same shape of guard TOKWEIR-12 established for coverage artifacts applies
# here, for a mistake that is very much worse than a 53 KB binary: `tokenweir` is
# intended for open-source release (ADR-0001 Pillar 1), and a key committed to a
# public history is a key that must be rotated, not deleted.

#: An Anthropic API key's published shape: the `sk-ant-` prefix and a long opaque
#: tail. The length floor is what keeps the pattern off ordinary prose — it is
#: matching a secret, not the word "sk-ant-" in a sentence about one.
_API_KEY_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")

#: Files whose contents are not text worth scanning. A key is an ASCII string; a
#: PNG that happens to contain those bytes is not a leaked credential, and reading
#: large binaries to find out is wasted work.
_UNSCANNED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".whl", ".gz"})


def _looks_like_an_api_key(text: str) -> bool:
    return _API_KEY_PATTERN.search(text) is not None


#: The detector's positive cases, **assembled at import time rather than written
#: out**. A literal key-shaped string here would be found by the sweep below and
#: reported as a leak in this very file — and the tempting fix, excluding this file
#: from the sweep, would punch a hole exactly where someone debugging a credential
#: is most likely to paste a real one. Splitting the prefix keeps the tree free of
#: the shape while still exercising the predicate on it.
_KEY_SHAPED = "sk-" + "ant-" + "api03-" + "AbCdEfGhIjKlMnOpQr0123456789"


@pytest.mark.parametrize(
    "text",
    [
        _KEY_SHAPED,
        f'ANTHROPIC_API_KEY="{_KEY_SHAPED}"',
        f"  {_KEY_SHAPED}  ",
        f"export ANTHROPIC_API_KEY={_KEY_SHAPED}",
    ],
)
def test_key_detector_recognizes_a_credential(text):
    """Without these, stubbing the predicate to `return False` leaves the sweep
    below green forever: a clean repository never gives it a positive case. The
    same reasoning as the coverage detector's own tests, above."""
    assert _looks_like_an_api_key(text)


def test_the_key_sweep_would_catch_a_planted_credential(tmp_path):
    """The sweep's own wiring, not just its predicate.

    `test_no_api_credential_is_tracked` passes on a clean repository whether or not
    it actually reads anything, so this plants a key-shaped file, runs the same
    read-and-match step over it, and asserts it is caught. Without this, deleting
    the body of the loop would leave the suite green.
    """
    planted = tmp_path / "leaked.env"
    planted.write_text(f"ANTHROPIC_API_KEY={_KEY_SHAPED}\n", encoding="utf-8")
    assert _looks_like_an_api_key(planted.read_text(encoding="utf-8", errors="replace"))


@pytest.mark.parametrize(
    "text",
    [
        "ANTHROPIC_API_KEY is read from the environment",
        "pass --credential-file rather than the key itself",
        "sk-ant-",
        "sk-ant-short",
        "unit-test-credential-value",
    ],
)
def test_key_detector_leaves_prose_alone(text):
    """The docs, the spec and the tests all *discuss* credentials at length. A
    guard that fired on the discussion would be deleted within a week."""
    assert not _looks_like_an_api_key(text)


def test_no_api_credential_is_tracked():
    """FR-024: no committed file in this repository contains an API key.

    The sweep is over `git ls-files`, so it is asking about the *repository*, not
    about whatever else is lying around the working directory — an untracked
    scratch file holding a key is careless but not published, and this test is
    about what a clone would hand a stranger.
    """
    _require_git_repo()

    offenders = []
    for path in _tracked_files():
        if Path(path).suffix.lower() in _UNSCANNED_SUFFIXES:
            continue
        full = REPO_ROOT / path
        try:
            content = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _looks_like_an_api_key(content):
            offenders.append(path)

    assert not offenders, (
        "An API-key-shaped string is committed in: "
        + ", ".join(offenders)
        + ". Rotate the key — a secret in git history is not fixed by deleting the file."
    )
