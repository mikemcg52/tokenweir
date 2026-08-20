"""What a run did not prove, and the record that says so (TOKWEIR-31).

`tests/conftest.py` ends every run by naming the optional drivers it could not
import and the claim each absence forfeits. These tests are why that note can be
trusted.

The gap that prompted it: `test_amqp.py`'s two real-`pika` tests are gated by
`importorskip`, and this project's authoritative install is `pip install -e .
pytest` — no extras — so under CI FR-022 was proven only against `FakeProperties`,
a dict wrapper that accepts any keyword at all. The run reported "81 skipped" and
went green. Nothing was wrong with the code; what was wrong was that the run
overstated itself.

Two properties matter here and they pull in opposite directions:

- the note must be **honest**, which is what the bidirectional consistency check
  below is for. A driver the suite gates on but the record omits is a forfeit
  nobody is told about — the original bug, one file over. A driver the record
  lists but nothing gates on is a claimed forfeit that no longer exists.
- the note must be **harmless**, which is what the subprocess runs are for. It is
  a report about the environment, not a result, so it may not touch the exit
  status, may not depend on which tests ran, and must still appear when the run
  itself went red.

Every check that inspects the source tree skips where there is none — an sdist
install has no `README.md` and no `tests/` to scan — the same rule
`test_repo_hygiene.py` and `test_migration_sql.py` already hold.
"""

import importlib.util
import re
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest
from conftest import (
    OPTIONAL_DRIVERS,
    _driver_is_importable,
    disclosure_lines,
    missing_optional_drivers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = Path(__file__).resolve().parent

#: A stand-in record, so the renderer's behaviour is checked against something
#: fixed rather than against whatever this machine happens to have installed.
#: Without it, "names every missing driver" would be untestable on a complete
#: environment and "says nothing when none are missing" untestable on a bare one.
STUB_DRIVERS = {
    "definitely_absent_alpha": "alpha's claim went unchecked",
    "definitely_absent_beta": "beta's claim went unchecked",
}


# --- The renderer -------------------------------------------------------------


def test_the_note_names_every_missing_driver_and_what_it_costs():
    lines = disclosure_lines(list(STUB_DRIVERS), STUB_DRIVERS)
    rendered = "\n".join(lines)

    for name, claim in STUB_DRIVERS.items():
        assert name in rendered, f"{name} is missing but the note does not say so"
        assert claim in rendered, f"{name}'s forfeited claim is not stated"


def test_a_present_driver_is_not_disclosed():
    """Half the value of the note is that it goes quiet. A note that lists a
    driver the environment has would train its reader to ignore it."""
    lines = disclosure_lines(["definitely_absent_beta"], STUB_DRIVERS)
    rendered = "\n".join(lines)

    assert "definitely_absent_beta" in rendered
    assert "definitely_absent_alpha" not in rendered


def test_a_complete_environment_produces_no_note_at_all():
    """Not an empty banner, not a "nothing missing" line — nothing. This is the
    state the note is asking the reader to reach, and it must be silent."""
    assert disclosure_lines([], STUB_DRIVERS) == []


def test_the_note_stays_short():
    """One line per driver plus a fixed header and footer. `pytest -ra` was the
    obvious alternative and was rejected for emitting 43 lines against this
    suite — pytest groups skips by source line, so the long "no Postgres
    configured" reason repeats forty times and buries the two pika ones."""
    lines = disclosure_lines(list(OPTIONAL_DRIVERS), OPTIONAL_DRIVERS)

    assert len(lines) == len(OPTIONAL_DRIVERS) + 3


def test_the_pika_entry_names_the_requirement_it_forfeits():
    """The entry this story exists for. "pika is not installed" alone would leave
    the reader to work out what that costs, which is the position they were in
    before."""
    assert "pika" in OPTIONAL_DRIVERS
    assert "FR-022" in OPTIONAL_DRIVERS["pika"]


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_every_entry_states_a_consequence_not_just_a_name(name):
    claim = OPTIONAL_DRIVERS[name]

    assert len(claim) > 40, f"{name}'s entry is too terse to tell anyone anything: {claim!r}"
    assert claim.strip() == claim


# --- The probe ----------------------------------------------------------------


def test_a_module_that_is_not_there_counts_as_absent():
    assert not _driver_is_importable("definitely_absent_alpha")


def test_the_standard_library_counts_as_present():
    assert _driver_is_importable("json")


def test_a_module_that_explodes_on_import_counts_as_absent(tmp_path, monkeypatch):
    """`except Exception`, not `except ImportError`. A package that is installed
    but raises on import is exactly as unable to prove FR-022 as a missing one,
    and this runs inside a reporting hook that must never raise."""
    module = tmp_path / "explodes_on_import.py"
    module.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    assert not _driver_is_importable("explodes_on_import")


def test_missing_drivers_come_back_in_the_records_order():
    """So the note's most important entry — pika, the one this story is about —
    stays at the top rather than moving with the environment."""
    assert missing_optional_drivers(STUB_DRIVERS) == list(STUB_DRIVERS)


# --- The record against the suite, in both directions -------------------------
#
# The check that keeps the note honest as the suite grows. It found `jsonschema`
# during this story: the first draft of the record listed four drivers, the suite
# gated on five, and the note would have shipped silently understating itself.

_IMPORTORSKIP = re.compile(r"""importorskip\(\s*["']([A-Za-z_][A-Za-z0-9_]*)["']""")


def _gated_modules(directory: Path = TESTS_DIR) -> set[str]:
    """Every module name the suite gates on with `importorskip`.

    A text scan, which is a real limitation and a deliberate one: a
    dynamically-constructed module name would be missed. Every gate in this suite
    is a literal, the job here is to catch the ordinary accident of adding a gate
    and forgetting the record, and a heavier mechanism would cost more than the
    drift it prevents.
    """
    found = set()
    for path in sorted(directory.glob("*.py")):
        found.update(_IMPORTORSKIP.findall(path.read_text(encoding="utf-8")))
    return found


def _require_source_tree() -> None:
    if not TESTS_DIR.is_dir() or not any(TESTS_DIR.glob("test_*.py")):
        pytest.skip("no test sources to scan (e.g. an installed distribution)")


def test_every_gated_driver_is_disclosed():
    """The original bug, one file over: a forfeit nobody is told about."""
    _require_source_tree()
    undisclosed = _gated_modules() - set(OPTIONAL_DRIVERS)

    assert not undisclosed, (
        f"{sorted(undisclosed)} are gated by importorskip but absent from OPTIONAL_DRIVERS in "
        "tests/conftest.py, so a run without them would not say what it did not prove. Add an "
        "entry naming the claim each absence forfeits, and a row to README.md under 'Develop'."
    )


def test_every_disclosed_driver_is_actually_gated():
    """The other direction. A record that only ever grows becomes a list of
    historical claims, and the note starts reporting forfeits that no longer
    exist."""
    _require_source_tree()
    stale = set(OPTIONAL_DRIVERS) - _gated_modules()

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
        lambda directory=TESTS_DIR: set(OPTIONAL_DRIVERS) | {"newly_gated_driver"},
    )

    with pytest.raises(AssertionError, match="newly_gated_driver"):
        test_every_gated_driver_is_disclosed()


def test_a_stale_disclosure_is_actually_detected(monkeypatch):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_gated_modules",
        lambda directory=TESTS_DIR: set(OPTIONAL_DRIVERS) - {"pika"},
    )

    with pytest.raises(AssertionError, match="pika"):
        test_every_disclosed_driver_is_actually_gated()


#: The gate's name, kept out of the fixture source below as a literal call.
#:
#: `_gated_modules` scans every `tests/*.py`, including this one, so a fixture
#: spelling the call out literally would be picked up as a real gate, and
#: `test_every_gated_driver_is_disclosed` would demand an `OPTIONAL_DRIVERS` entry
#: for a driver that does not exist. (Both the first draft of the fixture and the
#: first draft of this very comment tripped it — the check is doing its job.)
#: The fixture is data *describing* gates, not gates, and composing the call text
#: at runtime is what says so. Nothing below may spell it out either.
_GATE = "importorskip"


def test_the_scanner_reads_real_importorskip_calls(tmp_path):
    """The regex against the shapes this suite actually uses, including the
    multi-line one `conftest.py` and `test_migration_sql.py` are written in."""
    (tmp_path / "test_sample.py").write_text(
        textwrap.dedent(
            f'''
            import pytest

            def one():
                pytest.{_GATE}("inline_driver", reason="...")

            def two():
                mod = pytest.{_GATE}(
                    "wrapped_driver",
                    reason="over several lines",
                )
                return mod

            def three():
                return pytest.{_GATE}('single_quoted_driver')
            '''
        ),
        encoding="utf-8",
    )

    assert _gated_modules(tmp_path) == {
        "inline_driver",
        "wrapped_driver",
        "single_quoted_driver",
    }


def test_the_scanner_finds_the_gate_this_story_was_filed_about():
    """Anchored on a real call rather than only on synthetic ones, so the regex
    cannot pass its own fixtures while missing the suite."""
    _require_source_tree()

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


def _run_pytest_in(directory: Path) -> subprocess.CompletedProcess:
    (directory / "conftest.py").write_text(
        _CONFTEST_TEMPLATE.format(path=str(TESTS_DIR / "conftest.py")), encoding="utf-8"
    )
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(directory)],
        capture_output=True,
        text=True,
        cwd=str(directory),
        timeout=120,
    )


def _absent_driver() -> str:
    missing = missing_optional_drivers()
    if not missing:
        pytest.skip("every optional driver is installed; there is no note to observe")
    return missing[0]


def test_the_note_reaches_the_output_of_a_passing_run(tmp_path):
    driver = _absent_driver()
    (tmp_path / "test_passes.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    result = _run_pytest_in(tmp_path)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Not proven by this run" in result.stdout, result.stdout
    assert driver in result.stdout, result.stdout


def test_a_red_run_still_says_what_it_did_not_prove(tmp_path):
    """The reader of a failing run needs this no less, and a note that vanished
    exactly when the output mattered most would be worse than none. This also
    pins the half of FR-046 that matters: the note rides along with an exit
    status it did not cause."""
    driver = _absent_driver()
    (tmp_path / "test_fails.py").write_text(
        "def test_not_ok():\n    assert False\n", encoding="utf-8"
    )

    result = _run_pytest_in(tmp_path)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Not proven by this run" in result.stdout, result.stdout
    assert driver in result.stdout, result.stdout


def test_the_note_does_not_depend_on_any_test_having_run(tmp_path):
    """Absence is a fact about the environment, decided by importing rather than
    by watching which tests skipped. A skip census would report nothing here —
    and nothing under `-k`, `-x` or a collection error either."""
    _absent_driver()
    (tmp_path / "test_none_selected.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "conftest.py").write_text(
        _CONFTEST_TEMPLATE.format(path=str(TESTS_DIR / "conftest.py")), encoding="utf-8"
    )

    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-k", "matches_nothing_at_all", str(tmp_path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        timeout=120,
    )

    assert "Not proven by this run" in result.stdout, result.stdout


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
    "ADR-0001 Pillar 2",                    # and *why* it has to be
    "cannot be read as covering FR-022",   # the misreading it exists to prevent
    "/etc/mado/projects.yaml",              # the hand-off to whoever owns the registry
    "pip install -e '.[dev]'",              # what closes all of it
)


@pytest.mark.parametrize("marker", README_MARKERS)
def test_the_readme_records_the_decision(marker):
    readme = REPO_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("no README.md (e.g. an installed distribution)")

    assert marker in readme.read_text(encoding="utf-8"), (
        f"README.md no longer records {marker!r} — the choice to accept this coverage gap has to "
        "stay written down, or it reverts to looking like an oversight."
    )


@pytest.mark.parametrize("name", sorted(OPTIONAL_DRIVERS))
def test_the_readme_lists_every_disclosed_driver(name):
    """The table and the note are rendered from different places and must agree.
    A driver in the note but not the table sends its reader to a document that
    does not mention it."""
    readme = REPO_ROOT / "README.md"
    if not readme.is_file():
        pytest.skip("no README.md (e.g. an installed distribution)")

    section = readme.read_text(encoding="utf-8").split("## Develop")[-1]

    assert f"`{name}`" in section, (
        f"README.md's 'Develop' section does not mention {name}, which tests/conftest.py "
        "discloses. The table and the run's own note have to agree."
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
