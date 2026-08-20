"""What a run did not prove, and the record that says so (TOKWEIR-31).

`tests/conftest.py` ends every run by naming what this environment could not
supply and the claim each absence forfeits. These tests are why that note can be
trusted.

The gap that prompted it: `test_amqp.py`'s two real-`pika` tests are gated by
`importorskip`, and this project's authoritative install is `pip install -e .
pytest` — no extras — so under CI FR-022 was proven only against `FakeProperties`,
a dict wrapper that accepts any keyword at all. The run reported "81 skipped" and
went green. Nothing was wrong with the code; what was wrong was that the run
overstated itself.

Three properties matter here, and the middle one is the one review 1 found
missing:

- the note must be **honest about the suite**, which the bidirectional
  consistency check is for. A driver the suite gates on but the record omits is a
  forfeit nobody is told about — the original bug, one file over. A driver the
  record lists but nothing gates on is a claimed forfeit that no longer exists.
- the note must be **honest about the environment**. Every "is this missing?"
  decision needs a test that fails when the predicate breaks, or a mutant that
  discloses drivers which are in fact installed sails through green — which is
  what happened to the first draft, and it printed "pika is not installed" on a
  machine with pika 1.4.4.
- the note must be **harmless**, which the subprocess runs are for. It is a
  report about the environment, not a result: it may not touch the exit status,
  may not depend on which tests ran, and must still appear when the run went red.

Every check that inspects the source tree skips where there is none — an sdist
install has no `README.md` and no `tests/` to scan — the same rule
`test_repo_hygiene.py` and `test_migration_sql.py` already hold.
"""

import ast
import importlib.util
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent


def _load_conftest():
    """The module under test, in whichever import mode pytest is using.

    A plain `from conftest import ...` works only under the default `prepend`
    mode, which puts `tests/` on `sys.path`; under `--import-mode=importlib` this
    file was the one thing on the branch that failed to collect (review 2, Low-4).
    The fallback loads it by path.

    The *module object* is what gets returned and what the tests monkeypatch,
    rather than the string `"conftest"`: under the fallback there is no
    `sys.modules["conftest"]` to resolve such a target against, and patching a
    second copy of the module would silently patch nothing.
    """
    try:
        import conftest

        return conftest
    except ImportError:
        spec = importlib.util.spec_from_file_location(
            "_tokenweir_conftest_under_test", TESTS_DIR / "conftest.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


conftest = _load_conftest()

OPTIONAL_DRIVERS = conftest.OPTIONAL_DRIVERS
OptionalDriver = conftest.OptionalDriver
_driver_is_importable = conftest._driver_is_importable
_importable_driver = conftest._importable_driver
_postgres_suite_can_run = conftest._postgres_suite_can_run
disclosure_lines = conftest.disclosure_lines
missing_optional_drivers = conftest.missing_optional_drivers

#: A stand-in record, so the renderer's behaviour is checked against something
#: fixed rather than against whatever this machine happens to have installed.
#: Without it, "names every missing driver" would be untestable on a complete
#: environment and "says nothing when none are missing" untestable on a bare one.
STUB_DRIVERS = {
    "alpha": OptionalDriver(
        label="alpha is not installed",
        claim="alpha's claim went unchecked",
        gate="alpha",
        available=lambda: False,
    ),
    "beta": OptionalDriver(
        label="beta is not installed",
        claim="beta's claim went unchecked",
        gate="beta",
        available=lambda: True,
    ),
}


# --- The renderer -------------------------------------------------------------


def test_the_note_names_every_missing_driver_and_what_it_costs():
    rendered = "\n".join(disclosure_lines(list(STUB_DRIVERS), STUB_DRIVERS))

    for name, driver in STUB_DRIVERS.items():
        assert driver.label in rendered, f"{name} is missing but the note does not say so"
        assert driver.claim in rendered, f"{name}'s forfeited claim is not stated"


def test_a_present_driver_is_not_disclosed():
    """Half the value of the note is that it goes quiet. A note that lists a
    driver the environment has would train its reader to ignore it."""
    rendered = "\n".join(disclosure_lines(["alpha"], STUB_DRIVERS))

    assert "alpha" in rendered
    assert "beta" not in rendered


def test_a_complete_environment_produces_no_note_at_all():
    """Not an empty banner, not a "nothing missing" line — nothing. This is the
    state the note asks the reader to reach, and it must be silent."""
    assert disclosure_lines([], STUB_DRIVERS) == []


def test_the_note_claims_nothing_about_the_result():
    """It fires on failing and interrupted runs too, so it may not describe the
    run as green. The first draft opened with "A green result above does not
    cover the following" — false on a red run, and wrong about "above" even on a
    passing one, since the note precedes the summary line."""
    rendered = "\n".join(disclosure_lines(list(STUB_DRIVERS), STUB_DRIVERS)).lower()

    assert "green" not in rendered
    assert "above" not in rendered


def test_the_note_stays_short():
    """One line per entry plus a fixed header and footer, and an absolute ceiling
    besides. `pytest -ra` was the obvious alternative and was rejected for
    emitting 43 lines against this suite — pytest groups skips by source line, so
    the long "no Postgres configured" reason repeats forty times and buries the
    two pika ones. The ceiling is what keeps that comparison true: the formula
    alone is satisfied at any size, so it would still pass with forty drivers and
    forty-three lines."""
    lines = disclosure_lines(list(OPTIONAL_DRIVERS), OPTIONAL_DRIVERS)

    assert len(lines) == len(OPTIONAL_DRIVERS) + 3
    assert len(lines) <= 12, (
        f"the note has grown to {len(lines)} lines; at this size it is becoming the wall of text "
        "`-ra` was rejected for. Group the entries or shorten them rather than raising this."
    )


def test_the_pika_entry_names_the_requirement_it_forfeits():
    """The entry this story exists for. "pika is not installed" alone would leave
    the reader to work out what that costs, which is the position they were in
    before."""
    assert "pika" in OPTIONAL_DRIVERS
    assert "FR-022" in OPTIONAL_DRIVERS["pika"].claim


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_every_entry_states_a_consequence_not_just_a_name(name):
    driver = OPTIONAL_DRIVERS[name]

    assert len(driver.claim) > 40, f"{name}'s entry is too terse to tell anyone anything"
    assert driver.claim.strip() == driver.claim
    assert driver.label.strip() == driver.label


# --- The probe ----------------------------------------------------------------


def test_a_module_that_is_not_there_counts_as_absent():
    assert not _driver_is_importable("definitely_absent_alpha")


def test_the_standard_library_counts_as_present():
    assert _driver_is_importable("json")


def test_a_module_that_explodes_on_import_counts_as_absent(tmp_path, monkeypatch):
    """`except (Exception, SystemExit)`, not `except ImportError`. A package that
    is installed but raises on import is exactly as unable to prove FR-022 as a
    missing one, and this runs inside a reporting hook that must never raise."""
    module = tmp_path / "explodes_on_import.py"
    module.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert not _driver_is_importable("explodes_on_import")


def test_a_module_that_exits_on_import_counts_as_absent(tmp_path, monkeypatch):
    """`SystemExit` does not inherit from `Exception`, so it would have escaped
    the hook and taken the run's exit status with it."""
    (tmp_path / "exits_on_import.py").write_text("raise SystemExit(2)\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert not _driver_is_importable("exits_on_import")


def test_a_keyboard_interrupt_during_import_still_propagates(tmp_path, monkeypatch):
    """Deliberately not swallowed. Finishing a report is not worth ignoring
    Ctrl-C, and FR-046 was amended in fix round 1 to say so rather than leaving
    `except BaseException` to look like an oversight."""
    module = tmp_path / "interrupted_on_import.py"
    module.write_text("raise KeyboardInterrupt\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    with pytest.raises(KeyboardInterrupt):
        _driver_is_importable("interrupted_on_import")


# --- Which entries this environment cannot satisfy ----------------------------
#
# Review 1's Med-1: everything above hands `disclosure_lines` a ready-made list
# of missing names, so `missing_optional_drivers` itself was never exercised.
# Breaking it to `return list(drivers)` — disclosing drivers that are installed —
# left the whole file green, and on a machine with pika 1.4.4 the mutant printed
# "pika is not installed". These are the tests that fail on it.


def test_a_driver_that_is_present_is_not_reported_missing():
    assert missing_optional_drivers(STUB_DRIVERS) == ["alpha"]


def test_presence_is_decided_by_asking_the_record_not_by_listing_it():
    """The mutation that motivated this: with every entry available, the answer
    must be empty rather than "all of them"."""
    everything_present = {
        name: OptionalDriver(
            label=driver.label, claim=driver.claim, gate=driver.gate, available=lambda: True
        )
        for name, driver in STUB_DRIVERS.items()
    }

    assert missing_optional_drivers(everything_present) == []


def test_a_real_importable_module_is_not_reported_missing():
    """Against the genuine predicate rather than a stub, so a broken
    `_driver_is_importable` cannot hide behind a lambda. `json` is always there."""
    record = {"json": _importable_driver("json", "a claim that is never actually forfeited here")}

    assert missing_optional_drivers(record) == []


def test_a_real_absent_module_is_reported_missing():
    record = {
        "definitely_absent_alpha": _importable_driver(
            "definitely_absent_alpha", "a claim forfeited because this module does not exist"
        )
    }

    assert missing_optional_drivers(record) == ["definitely_absent_alpha"]


def test_missing_drivers_come_back_in_the_records_order():
    """So the note's most important entry — pika, the one this story is about —
    stays at the top rather than moving with the environment."""
    absent = {
        name: OptionalDriver(
            label=driver.label, claim=driver.claim, gate=driver.gate, available=lambda: False
        )
        for name, driver in STUB_DRIVERS.items()
    }

    assert missing_optional_drivers(absent) == list(STUB_DRIVERS)


# --- Postgres is not disclosed on an import (review 1, Med-2) -----------------
#
# The real-store suite needs psycopg *and* a server. Deciding this entry on
# `import psycopg` alone made the note go quiet on a machine that had the driver
# and no database, while forty-three tests carried on skipping — this story's own
# bug, one entry over. Reachable without contrivance: `pip install -e '.[postgres]'`,
# or `.[dev]` on Windows, where the `pgserver` marker excludes it.


def test_postgres_needs_more_than_the_driver(monkeypatch):
    monkeypatch.delenv("TOKENWEIR_TEST_DSN", raising=False)
    monkeypatch.setattr(
        conftest, "_driver_is_importable", lambda name: name == "psycopg"
    )

    assert not _postgres_suite_can_run(), (
        "psycopg alone was treated as enough, so a machine with the driver and no server "
        "would be told nothing while the whole real-store suite skipped"
    )


def test_postgres_is_available_with_a_driver_and_a_configured_dsn(monkeypatch):
    monkeypatch.setenv("TOKENWEIR_TEST_DSN", "postgresql://example/scratch")
    monkeypatch.setattr(conftest, "_driver_is_importable", lambda name: name == "psycopg")

    assert _postgres_suite_can_run()


def test_postgres_is_available_with_a_driver_and_an_embedded_server(monkeypatch):
    monkeypatch.delenv("TOKENWEIR_TEST_DSN", raising=False)
    monkeypatch.setattr(
        conftest, "_driver_is_importable", lambda name: name in {"psycopg", "pgserver"}
    )

    assert _postgres_suite_can_run()


def test_postgres_without_the_driver_is_never_available(monkeypatch):
    """A DSN is no use without psycopg, and the fixtures skip in that case too —
    this predicate has to agree with them or the note describes a different suite
    from the one that ran."""
    monkeypatch.setenv("TOKENWEIR_TEST_DSN", "postgresql://example/scratch")
    monkeypatch.setattr(conftest, "_driver_is_importable", lambda name: False)

    assert not _postgres_suite_can_run()


def test_the_postgres_entry_does_not_claim_the_driver_is_missing():
    """Its label has to survive the case where psycopg is installed and the
    server is not, which is the whole point of Med-2."""
    assert "not installed" not in OPTIONAL_DRIVERS["psycopg"].label


# --- The record against the suite, in both directions -------------------------
#
# The check that keeps the note honest as the suite grows. It found `jsonschema`
# during this story: the first draft of the record listed four drivers, the suite
# gated on five, and the note would have shipped silently understating itself.


def _gated_modules(directory: Path = TESTS_DIR) -> set[str]:
    """Every module name the suite gates on with `importorskip`.

    Parsed, not grepped. A regex over the source text also matched the *fixtures*
    in this very file — the sample module written to a temp directory, and then
    the comment explaining why the sample was a problem — and reported them as
    real gates pointing at `OPTIONAL_DRIVERS`. Review 1 rightly called that a
    maintenance trap: no test file could ever contain the literal text again.

    An AST walk has no such trap. A call is a `Call` node; the same characters
    inside a string literal or a docstring are not, so fixtures and documentation
    examples are free to spell gates out in full. It is also strictly more
    accurate than the regex it replaces.
    """
    found = set()
    for path in sorted(directory.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            function = node.func
            name = (
                function.attr
                if isinstance(function, ast.Attribute)
                else function.id
                if isinstance(function, ast.Name)
                else None
            )
            if name != "importorskip":
                continue
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.add(first.value)
    return found


def _gated_in_record() -> set[str]:
    """The entries claiming to be gated by `importorskip`.

    Entries with `gate=None` are gated some other way and are deliberately out of
    scope for the comparison — without that escape hatch the record could not
    hold one, which is exactly the corner Med-2's fix would have been painted
    into.
    """
    return {driver.gate for driver in OPTIONAL_DRIVERS.values() if driver.gate is not None}


# There is deliberately no "skip if there are no test sources" guard on the two
# checks below, and its absence is the point. An earlier draft had one; review 2
# showed it could never fire — `TESTS_DIR` is the directory this very file lives
# in, so if this test is running, the sources it scans are right there. A guard
# that cannot fire is worse than none: it reads as protection and provides none.
# The README and pyproject checks below are a different matter and do skip, since
# those files really can be absent from an installed distribution.


def test_every_gated_driver_is_disclosed():
    """The original bug, one file over: a forfeit nobody is told about."""
    undisclosed = _gated_modules() - _gated_in_record()

    assert not undisclosed, (
        f"{sorted(undisclosed)} are gated by importorskip but absent from OPTIONAL_DRIVERS in "
        "tests/conftest.py, so a run without them would not say what it did not prove. Add an "
        "entry naming the claim each absence forfeits, and a row to README.md under 'Develop'."
    )


def test_every_disclosed_driver_is_actually_gated():
    """The other direction. A record that only ever grows becomes a list of
    historical claims, and the note starts reporting forfeits that no longer
    exist."""
    stale = _gated_in_record() - _gated_modules()

    assert not stale, (
        f"{sorted(stale)} are disclosed in OPTIONAL_DRIVERS but nothing in tests/ gates on them "
        "any more. Remove the entries and the matching README.md rows."
    )


# --- The detector itself ------------------------------------------------------
#
# Per `test_repo_hygiene.py`'s convention. Without these, stubbing `_gated_modules`
# to `return set()` leaves both checks above green forever: on a repository that
# is already consistent, a check that works and a check that always passes are
# indistinguishable.


def test_an_undisclosed_gate_is_actually_detected(monkeypatch):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_gated_modules",
        lambda directory=TESTS_DIR: _gated_in_record() | {"newly_gated_driver"},
    )

    with pytest.raises(AssertionError, match="newly_gated_driver"):
        test_every_gated_driver_is_disclosed()


def test_a_stale_disclosure_is_actually_detected(monkeypatch):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_gated_modules",
        lambda directory=TESTS_DIR: _gated_in_record() - {"pika"},
    )

    with pytest.raises(AssertionError, match="pika"):
        test_every_disclosed_driver_is_actually_gated()


def test_an_entry_gated_some_other_way_is_not_called_stale():
    """The escape hatch Med-2 needed. An entry with `gate=None` is disclosed but
    is not claimed to be an `importorskip`, so the consistency check must leave it
    alone rather than demanding it be removed."""
    otherwise_gated = OptionalDriver(
        label="no network was available",
        claim="a claim gated by something other than an import",
        gate=None,
        available=lambda: False,
    )

    assert otherwise_gated.gate not in _gated_in_record()


def test_the_scanner_reads_real_importorskip_calls(tmp_path):
    """The shapes this suite actually uses, including the multi-line one
    `conftest.py` and `test_migration_sql.py` are written in."""
    (tmp_path / "test_sample.py").write_text(
        textwrap.dedent(
            '''
            import pytest

            def one():
                pytest.importorskip("inline_driver", reason="...")

            def two():
                return pytest.importorskip(
                    "wrapped_driver",
                    reason="over several lines",
                )

            def three():
                return pytest.importorskip('single_quoted_driver')

            def four():
                from pytest import importorskip
                return importorskip("bare_name_driver")
            '''
        ),
        encoding="utf-8",
    )

    assert _gated_modules(tmp_path) == {
        "inline_driver",
        "wrapped_driver",
        "single_quoted_driver",
        "bare_name_driver",
    }


def test_the_scanner_ignores_gates_that_are_only_talked_about(tmp_path):
    """The trap the AST walk removes. A docstring, a comment and a string being
    written to a file all contain the characters of a gate and are not gates —
    the regex this replaced reported all three, and this file tripped it twice
    during implementation."""
    (tmp_path / "test_prose.py").write_text(
        textwrap.dedent(
            '''
            """Gate a test on a driver with pytest.importorskip("documented_driver")."""

            # pytest.importorskip("commented_driver") is how the others do it.

            SAMPLE = \'\'\'
            pytest.importorskip("fixture_driver")
            \'\'\'
            '''
        ),
        encoding="utf-8",
    )

    assert _gated_modules(tmp_path) == set()


def test_the_scanner_looks_inside_subdirectories(tmp_path):
    """`glob` rather than `rglob` would make a gate under `tests/<subpkg>/`
    invisible to both directions of the check. None exists today; the check
    should not quietly stop working the day one does."""
    nested = tmp_path / "integration"
    nested.mkdir()
    (nested / "test_nested.py").write_text(
        'import pytest\n\ndef test_x():\n    pytest.importorskip("nested_driver")\n',
        encoding="utf-8",
    )

    assert "nested_driver" in _gated_modules(tmp_path)


def test_the_scanner_finds_the_gate_this_story_was_filed_about():
    """Anchored on a real call rather than only on synthetic ones, so the scanner
    cannot pass its own fixtures while missing the suite."""

    assert "pika" in _gated_modules()


# --- The hook in a real run ---------------------------------------------------
#
# A subprocess rather than pytest's `pytester` fixture, which would mean
# registering that plugin for every run of this suite. The temporary conftest
# loads the real one by path under a distinct module name — importing it as
# `conftest` would collide with the temporary directory's own.

_CONFTEST_TEMPLATE = '''
import importlib.util

_spec = importlib.util.spec_from_file_location("_tokenweir_conftest", {path!r})
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)

pytest_terminal_summary = _module.pytest_terminal_summary
'''


def _write_conftest(directory: Path) -> None:
    (directory / "conftest.py").write_text(
        _CONFTEST_TEMPLATE.format(path=str(TESTS_DIR / "conftest.py")), encoding="utf-8"
    )


def _run_pytest_in(directory: Path, *extra: str) -> subprocess.CompletedProcess:
    _write_conftest(directory)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *extra, str(directory)],
        capture_output=True,
        text=True,
        cwd=str(directory),
        timeout=120,
    )


def _absent_entry() -> str:
    missing = missing_optional_drivers()
    if not missing:
        pytest.skip("this environment can satisfy every entry; there is no note to observe")
    return OPTIONAL_DRIVERS[missing[0]].label


def test_the_note_reaches_the_output_of_a_passing_run(tmp_path):
    label = _absent_entry()
    (tmp_path / "test_passes.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    result = _run_pytest_in(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Whatever this run reported" in result.stdout, result.stdout
    assert label in result.stdout, result.stdout


def test_a_red_run_still_says_what_it_did_not_prove(tmp_path):
    """The reader of a failing run needs this no less, and a note that vanished
    exactly when the output mattered most would be worse than none. This also
    pins the half of FR-046 that matters: the note rides along with an exit
    status it did not cause."""
    label = _absent_entry()
    (tmp_path / "test_fails.py").write_text(
        "def test_not_ok():\n    assert False\n", encoding="utf-8"
    )

    result = _run_pytest_in(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Whatever this run reported" in result.stdout, result.stdout
    assert label in result.stdout, result.stdout


def test_the_note_does_not_depend_on_any_test_having_run(tmp_path):
    """Absence is a fact about the environment, decided by asking the record
    rather than by watching which tests skipped. A skip census would report
    nothing here — and nothing under `-k`, `-x` or a collection error either."""
    _absent_entry()
    (tmp_path / "test_none_selected.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    result = _run_pytest_in(tmp_path, "-k", "matches_nothing_at_all")

    assert "Whatever this run reported" in result.stdout, result.stdout


# --- The README carries the decision ------------------------------------------


#: Marker strings only, so the prose stays free to change around them. Each one
#: carries part of the *decision* — that it is deliberate, why it has to be, and
#: what the person who can close the gap should do — rather than merely describing
#: the situation. The same treatment `test_the_readme_documents_the_store` gives
#: the store and `test_the_readme_keeps_the_load_bearing_warnings` gives FR-022's
#: unknown-key drop.
README_MARKERS = (
    "What a bare install does not cover",   # the section exists
    "This is accepted, not overlooked.",    # deliberate, not an oversight
    # FR-048's mandated reason. The bare string "ADR-0001 Pillar 2" was used here
    # until fix round 2 and pinned nothing: it appears three times in this README,
    # so the whole rationale paragraph could be deleted with the suite still green.
    "transport-free by contract (ADR-0001 Pillar 2)",
    "cannot be read as covering FR-022",    # the misreading it exists to prevent
    "/etc/mado/projects.yaml",              # option 1, handed to the registry's owner
    # Likewise: "pip install -e '.[dev]'" appears three times, including twice in
    # prose that predates this story, so it did not guard option 2's hand-off.
    "to close all of them, make the step",
)


def _readme_text() -> str:
    readme = REPO_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("no README.md (e.g. an installed distribution)")
    return readme.read_text(encoding="utf-8")


@pytest.mark.parametrize("marker", README_MARKERS)
def test_the_readme_records_the_decision(marker):
    assert marker in _readme_text(), (
        f"README.md no longer records {marker!r} — the choice to accept this coverage gap has to "
        "stay written down, or it reverts to looking like an oversight."
    )


def _develop_section() -> str:
    return _readme_text().split("## Develop")[-1]


def _table_row_for(name: str) -> str:
    """The row of the "what a bare install omits" table that covers `name`."""
    rows = [
        line
        for line in _develop_section().splitlines()
        if line.startswith("|") and f"`{name}`" in line
    ]
    assert len(rows) == 1, (
        f"expected exactly one README table row mentioning {name}, found {len(rows)}. The table "
        "and tests/conftest.py's record have to line up one for one."
    )
    return rows[0]


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_the_readme_lists_every_disclosed_driver(name):
    """The table and the note are rendered from different places and must agree.
    A driver in the note but not the table sends its reader to a document that
    does not mention it."""
    assert f"`{name}`" in _develop_section(), (
        f"README.md's 'Develop' section does not mention {name}, which tests/conftest.py "
        "discloses. The table and the run's own note have to agree."
    )


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_the_readme_says_what_each_omission_forfeits(name):
    """Naming the driver is half of FR-048; the other half is saying what its
    absence costs, and until fix round 2 nothing guarded it. Review 2 replaced
    every "What goes unchecked" cell in the table with "TBD" and the suite stayed
    green — the column the section exists for could be gutted in silence."""
    cells = [cell.strip() for cell in _table_row_for(name).strip().strip("|").split("|")]

    assert len(cells) == 2, f"the {name} row is not a two-column table row: {cells}"
    assert len(cells[1]) > 60, (
        f"README.md's table says nothing substantive about what losing {name} costs: "
        f"{cells[1]!r}. FR-048 requires the forfeited coverage to be named, not just the driver."
    )


# --- The core stays dependency-light ------------------------------------------


def _pyproject() -> dict:
    path = REPO_ROOT / "pyproject.toml"
    if not path.is_file():
        pytest.skip("no pyproject.toml (e.g. an installed distribution)")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def test_the_core_requires_no_driver_at_all():
    """ADR-0001 Pillar 2, and the reason this story documents the gap instead of
    closing it. The moment `dependencies` is non-empty, "install the extra" stops
    being the honest answer and this whole record is the wrong shape."""
    assert _pyproject()["project"]["dependencies"] == []


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_no_disclosed_driver_is_a_runtime_dependency(name):
    dependencies = _pyproject()["project"]["dependencies"]

    assert not any(name in spec for spec in dependencies), (
        f"{name} became a runtime dependency; it is disclosed as optional, so either the "
        "disclosure or the dependency is wrong."
    )


def test_pika_stays_in_the_amqp_and_dev_extras_and_nowhere_else():
    """The specific promise this story makes: the fix for the FR-022 blind spot is
    documentation, not a new dependency. `amqp` is where a consumer who wants the
    adapter gets it; `dev` is where the tests get it."""
    extras = _pyproject()["project"]["optional-dependencies"]
    carrying_pika = {
        extra for extra, specs in extras.items() if any("pika" in spec for spec in specs)
    }

    assert carrying_pika == {"amqp", "dev"}, carrying_pika


def test_the_amqp_adapter_still_imports_without_pika():
    """The property the skip exists to protect. If this ever failed, disclosing
    the gap would be beside the point — the core would already be broken for
    anyone without a broker library."""
    assert importlib.util.find_spec("tokenweir.amqp") is not None

    import tokenweir.amqp  # noqa: F401


# --- The report can never be the reason a run fails (review 2, Med-2) ---------


def test_a_predicate_that_raises_does_not_take_the_run_down(tmp_path):
    """Only the import probe was guarded, so any *other* predicate that raised
    escaped into pytest as an INTERNALERROR with a non-zero exit — on a run whose
    tests had all passed. The `psycopg` entry is already a non-import predicate,
    so this was one entry away from being live rather than hypothetical.

    Run in a subprocess against a record whose predicate raises, because what is
    being asserted is the exit status of a real pytest invocation.
    """
    (tmp_path / "test_passes.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text(
        textwrap.dedent(
            f'''
            import importlib.util

            _spec = importlib.util.spec_from_file_location(
                "_tokenweir_conftest", {str(TESTS_DIR / "conftest.py")!r}
            )
            _module = importlib.util.module_from_spec(_spec)
            _spec.loader.exec_module(_module)


            def _explode():
                raise ValueError("the predicate is broken")


            _module.OPTIONAL_DRIVERS = {{
                "broken": _module.OptionalDriver(
                    label="broken is not installed",
                    claim="a claim whose availability predicate raises",
                    gate=None,
                    available=_explode,
                )
            }}

            pytest_terminal_summary = _module.pytest_terminal_summary
            '''
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )

    assert result.returncode == 0, (
        "a broken disclosure predicate changed the run's verdict:\n" + result.stdout + result.stderr
    )
    assert "INTERNALERROR" not in result.stdout + result.stderr
    assert "could not report what this run did not prove" in result.stdout, result.stdout


# --- Documentation checks skip rather than fail (FR-054, review 2 Med-3) ------
#
# `test_repo_hygiene.py` asserts its own skips with `pytest.raises(pytest.skip
# .Exception)` for exactly this reason, and plan.md claimed this file followed
# that convention while nothing here did. A check that is supposed to skip on an
# installed distribution and instead fails turns an ordinary install red, which
# is the thing FR-006 forbids and this whole story is careful about elsewhere.


def test_the_readme_check_skips_when_there_is_no_readme(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(pytest.skip.Exception):
        _readme_text()


def test_the_pyproject_check_skips_when_there_is_no_pyproject(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(pytest.skip.Exception):
        _pyproject()


def test_the_readme_markers_skip_rather_than_fail_off_a_source_tree(tmp_path, monkeypatch):
    """The parametrized guard itself, not just its helper — it is the one a
    consumer running the installed tests would actually hit."""
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(pytest.skip.Exception):
        test_the_readme_records_the_decision(README_MARKERS[0])


def test_the_dependency_guard_skips_rather_than_fails_off_a_source_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "REPO_ROOT", tmp_path)

    with pytest.raises(pytest.skip.Exception):
        test_the_core_requires_no_driver_at_all()
