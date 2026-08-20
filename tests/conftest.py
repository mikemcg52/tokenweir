"""Shared fixtures — chiefly the gate on the real-Postgres suite (TOKWEIR-5).

The project's pattern is correctness tests against a **real Postgres, never a
mocked one**: a mock of a database asserts that the code calls the mock, which is
not the question anyone has about a schema. TOKWEIR-5 says to keep that pattern,
and these fixtures are how it is kept without making the rest of the suite
depend on a server.

`TOKENWEIR_TEST_DSN` turns the store tests on. Without it, and with no embedded
server available, they **skip**, naming the variable, because an
environment-dependent test that cannot be evaluated must not turn an ordinary
install red — the same rule `test_repo_hygiene.py` already holds for a tree with
no `.git`.

**The embedded fallback.** With no DSN configured, these fixtures will start a
throwaway PostgreSQL of their own if `pgserver` is importable — it ships the
server binaries in the wheel, so it needs no root, no apt and no Docker, which is
what makes it usable in an environment that has none of the three. It is in the
`dev` extra and nothing else, so an ordinary `pip install -e .` still skips.

That fallback exists because the first draft of this story recorded "no usable
Postgres" as an environment fact and left the third acceptance clause unverified —
and a High-severity concurrency defect shipped underneath a correct test that was
never executed. A suite that can start its own server has no such hiding place.

Each test gets its own Postgres **schema**, created and dropped around it, with
`search_path` pointed at it. So the DSN may point at any scratch database: the
suite cannot collide with — or destroy — an existing `gateway_usage`, which
matters because the obvious database to hand it is the one that already has one.

---

**Second concern, same subject: saying out loud what the skips cost (TOKWEIR-31).**

Everything above is about *skipping well*. `OPTIONAL_DRIVERS` below is about the
other half — that a run which skips well still reports a bare "81 skipped", and a
reader takes green to mean "everything passed" because that is what green normally
means.

The case that prompted it: `tests/test_amqp.py`'s two real-`pika` tests are gated
the same way these fixtures are, and the authoritative install for this project is
`pip install -e . pytest` — no extras. So under CI, FR-022 was proven only against
`FakeProperties`, a dict wrapper that accepts any kwargs at all, and nothing in the
run said so.

The skip is not the bug and is not being removed: the core is transport-free by
contract (ADR-0001 Pillar 2), so a plain install legitimately has no `pika`, and a
test that cannot be evaluated must not turn that install red. What is being fixed
is the silence. The run now ends by naming each absent driver and the claim it
therefore did not test.
"""

import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass

import pytest

#: The one switch. Named for this package rather than reusing `DATABASE_URL` so
#: that pointing a test suite at a database is always deliberate — an inherited
#: `DATABASE_URL` in a developer's shell should not silently enlist their
#: application's database into a test run that creates and drops schemas in it.
DSN_ENV_VAR = "TOKENWEIR_TEST_DSN"

SKIP_REASON = (
    f"no Postgres configured: set ${DSN_ENV_VAR} to a scratch database to run the "
    "real-store tests (each test creates and drops its own schema, so an existing "
    "gateway_usage is not touched), or install the dev extra for an embedded one "
    "(`pip install -e '.[dev]'`)"
)


def postgres_dsn_or_none() -> str | None:
    return os.environ.get(DSN_ENV_VAR) or None


def _embedded_dsn(tmp_path_factory) -> str | None:
    """A DSN for a throwaway server, or ``None`` if one cannot be had.

    ``None`` rather than an exception on *any* failure: an embedded server that
    will not start is an environment this suite cannot evaluate, and the rule
    above says such a suite skips with a reason rather than turning an install
    red. The reason names what actually went wrong, so it is not mistaken for the
    ordinary "nothing configured" case.
    """
    try:
        import pgserver
    except ImportError:
        return None

    try:
        server = pgserver.get_server(tmp_path_factory.mktemp("pgserver"))
        return server.get_uri()
    except Exception as exc:  # pragma: no cover - environment-dependent
        pytest.skip(f"embedded Postgres (pgserver) could not be started: {exc!r}")


@pytest.fixture(scope="session")
def postgres_dsn(tmp_path_factory) -> str:
    """A configured database if there is one, else an embedded one, else a skip.

    A configured DSN wins: a developer who points this at a specific server —
    a particular Postgres version, say — means it, and silently substituting a
    different server would answer a question they did not ask.
    """
    dsn = postgres_dsn_or_none()
    if dsn:
        return dsn

    embedded = _embedded_dsn(tmp_path_factory)
    if embedded:
        return embedded

    pytest.skip(SKIP_REASON)


@pytest.fixture(scope="session")
def psycopg_module(postgres_dsn):
    """The driver, or a skip. Ordered after the DSN check on purpose: with no
    database configured the useful message is about the DSN, not about a driver
    the developer would have no reason to install."""
    return pytest.importorskip(
        "psycopg",
        reason=(
            f"${DSN_ENV_VAR} is set but psycopg is not installed; "
            "`pip install -e '.[dev]'` or `pip install 'tokenweir[postgres]'`"
        ),
    )


@pytest.fixture
def scratch_schema(psycopg_module, postgres_dsn):
    """A uniquely-named schema, and a factory for connections pointed at it.

    A factory rather than a single connection because the concurrency test needs
    two, and both must see the same schema.
    """
    schema = f"tokenweir_test_{uuid.uuid4().hex[:12]}"
    connections = []

    def connect():
        connection = psycopg_module.connect(postgres_dsn)
        with connection.cursor() as cursor:
            # Quoted: the name is generated here, but quoting it is the habit that
            # keeps the next person from interpolating one that is not.
            cursor.execute(f'SET search_path TO "{schema}"')
        connection.commit()
        connections.append(connection)
        return connection

    admin = psycopg_module.connect(postgres_dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
    try:
        yield connect, schema
    finally:
        for connection in connections:
            try:
                connection.close()
            except Exception:
                pass
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


@pytest.fixture
def connection(scratch_schema):
    connect, _ = scratch_schema
    return connect()


@pytest.fixture
def migrated(connection):
    """A connection whose schema has every migration applied."""
    from tokenweir.migrations import apply

    apply(connection)
    return connection


# --- What this environment could not prove (TOKWEIR-31) -----------------------
#
# The record, the predicates, the renderer and the hook. One record with three
# consumers — the terminal note below, the README table under "Develop", and the
# consistency check in `tests/test_optional_drivers.py` — because the failure
# being fixed is a log and a document disagreeing about what was tested, and two
# records would reintroduce it one refactor later.


@dataclass(frozen=True)
class OptionalDriver:
    """One thing this environment might lack, and what its absence costs.

    `available` rather than a bare module name because **being importable is not
    always the question**. The real-Postgres suite needs psycopg *and* a server,
    so disclosing it on `import psycopg` alone would go quiet on a machine that
    has the driver and no database — while forty-three tests carried on
    skipping. That is this story's own bug, one entry over, and review 1 caught
    it here before it shipped.

    `gate` is the module the suite gates on with `pytest.importorskip`, and it is
    what `test_optional_drivers.py` matches the suite against in both directions.
    It is `None` for an entry gated some other way, so the record can hold one
    without the consistency check calling it stale.
    """

    #: How the note names the absence. A whole clause, because "psycopg is not
    #: installed" is the wrong sentence when psycopg is installed and the server
    #: is what is missing.
    label: str
    #: What goes unproven. Prose, and nobody's to derive — no mechanism can infer
    #: "FR-022 goes unchecked" from the absence of a module. So the *set* of
    #: entries is checked and the wording is left to review, which catches the
    #: drift that happens by accident and not the entry that was always wrong.
    claim: str
    #: The `importorskip` module the suite gates on, or None if gated otherwise.
    gate: str | None
    #: Whether this environment can prove the claim. Called, never cached: an
    #: environment variable can change between collection and the summary.
    available: Callable[[], bool]
    #: The identifiers this entry's forfeit is *about* — the requirement id, the
    #: file, the library whose absence is the point. Every one must appear both in
    #: `claim` above and in the entry's README row, which is what stops the two
    #: from being gutted independently.
    #:
    #: Prose cannot be checked and is left to review (see `claim`), but these are
    #: not prose. Until fix round 4 the README's "what goes unchecked" column was
    #: guarded only by its *length*, so review 5 replaced every cell with eighty
    #: characters of `TBD` — deleting `FR-022` from the row this story exists for —
    #: and the suite stayed green. A length threshold measures typing, not meaning.
    #:
    #: Defaulted only so the suite's own stub records stay readable; every entry in
    #: `OPTIONAL_DRIVERS` must declare some, and a test asserts exactly that — an
    #: entry with none would opt its row out of the check entirely.
    anchors: tuple[str, ...] = ()


def _driver_is_importable(module_name: str) -> bool:
    """Whether `module_name` can be imported at all.

    Catches `SystemExit` alongside `Exception`: a module that calls `sys.exit()`
    at import time is real, and is exactly as unable to prove FR-022 as a missing
    one. `KeyboardInterrupt` is deliberately **not** caught — this runs inside a
    reporting hook, and swallowing Ctrl-C to finish printing a note would be a
    worse bug than the one being fixed. FR-046 was amended to say so.
    """
    import importlib

    try:
        importlib.import_module(module_name)
    except (Exception, SystemExit):
        return False
    return True

    # One known quirk, deliberately not "fixed" (review 3, Low-1). A same-named
    # *directory* on `sys.path` is an implicit namespace package and imports
    # cleanly, so running `python -m pytest` from the repository root — where a
    # gitignored `build/` may sit — makes the `build` entry look satisfied. That
    # is not a divergence to correct: `pytest.importorskip` imports too and is
    # fooled identically, so the note goes on agreeing with the gate it reports
    # on, and the test then fails loudly rather than passing vacuously. Making
    # the probe stricter than the gate would produce the worse failure — a note
    # claiming a forfeit for a check that actually ran. The authoritative command
    # invokes `.venv/bin/pytest`, which does not put the cwd on `sys.path`.


def _postgres_suite_can_run() -> bool:
    """Exactly the condition `postgres_dsn` and `psycopg_module` impose above.

    Written as one predicate so it cannot drift from the fixtures: the suite runs
    when psycopg is importable **and** there is either a configured DSN or a
    `pgserver` to start one. Any other reading discloses the wrong thing —
    see `OptionalDriver.available`.

    **One residual, deliberately left** (review 2, Low-1). `_embedded_dsn` skips
    when an importable `pgserver` fails to *start*, and this predicate cannot see
    that: it would have to start a server to find out, inside a reporting hook,
    on every run. So an environment where pgserver imports and will not run is
    still told nothing. The trade is one rare under-report against starting a
    database to write a log line, and the rare case is the one already carrying a
    loud skip reason naming the failure. Recorded here and in FR-043a rather than
    left for the next reader to find.
    """
    if not _driver_is_importable("psycopg"):
        return False
    return bool(postgres_dsn_or_none()) or _driver_is_importable("pgserver")


def _importable_driver(
    module_name: str, claim: str, anchors: tuple[str, ...] = ()
) -> OptionalDriver:
    """The ordinary case: gated by `importorskip`, present iff it imports."""
    return OptionalDriver(
        label=f"{module_name} is not installed",
        claim=claim,
        gate=module_name,
        available=lambda: _driver_is_importable(module_name),
        anchors=anchors,
    )


#: Every optional driver the suite gates on, and what its absence forfeits.
#:
#: **Python packages only.** The suite also gates on external binaries — `node`
#: for FR-024's ECMA-262 check, `git` for the repository-hygiene checks — and
#: those are out of this record's scope, so the note is a report on optional
#: drivers rather than an exhaustive census of everything a run skipped. Both are
#: present in every environment this project targets and neither has ever gone
#: missing; widening to them is a separate concern with its own consistency
#: problem (a binary has no import to probe). Named here so the note's silence
#: about them is a known boundary rather than an oversight.
#:
#: Keys line up with the modules gated by `pytest.importorskip`, and
#: `test_optional_drivers.py` enforces that in both directions: a new gate nobody
#: disclosed fails, and a disclosed driver nothing gates on fails too.
#: One-directional would let this decay into a list of historical claims.
OPTIONAL_DRIVERS: dict[str, OptionalDriver] = {
    # The entry this story was filed for. `FakeProperties` in test_amqp.py accepts
    # any kwargs at all, so without real pika nothing would notice
    # `publish_properties()` emitting a key `pika.BasicProperties` rejects.
    "pika": _importable_driver(
        "pika",
        "FR-022 (persistent messages with a JSON content type) was checked only against the "
        "test double, not against a real pika.BasicProperties",
        # Both halves of FR-043's requirement for this entry: the requirement id,
        # and the fact that what stood in for the driver was a double. Review 5
        # (Low-1) found the second half guarded nowhere, so it could have been
        # deleted from the note in silence.
        anchors=("FR-022", "pika.BasicProperties"),
    ),
    # Found by the consistency check rather than by hand, which is the whole
    # argument for having it: `test_schema.py` already carried a comment saying
    # these skip on the stream plan's install, and the disclosure would have
    # shipped without them anyway.
    "jsonschema": _importable_driver(
        "jsonschema",
        "the published schema in schema/usage-record.v1.json was not run against a JSON Schema "
        "engine — payload validity was checked only by this package's own code",
        anchors=("schema/usage-record.v1.json", "JSON Schema"),
    ),
    # Not an `_importable_driver`: the suite needs a server as well as a driver,
    # and disclosing this on the import alone would go silent while the whole
    # real-store suite carried on skipping.
    "psycopg": OptionalDriver(
        label="no Postgres was available",
        claim=(
            "the real-Postgres suite did not run — the writer, the source and the migrations "
            f"were not exercised against a server (set ${DSN_ENV_VAR}, or install the dev extra "
            "for an embedded one via pgserver)"
        ),
        gate="psycopg",
        available=_postgres_suite_can_run,
        # What the forfeit *is*, not what the condition for it is. The condition
        # ($TOKENWEIR_TEST_DSN or pgserver) lives in the README table's first
        # column and in `label`; these anchors hold the second column, which is
        # the one review 5 showed could be filled with placeholder text.
        anchors=("real-Postgres suite", "writer", "migrations"),
    ),
    "pglast": _importable_driver(
        "pglast",
        "the migration SQL was not parsed by libpg_query, the server's own parser — only the "
        "checks that need no parser ran",
        anchors=("libpg_query",),
    ),
    "build": _importable_driver(
        "build",
        "SC-001 was checked only against the packaging declaration, not by building a wheel and "
        "looking inside it for the migrations",
        anchors=("SC-001", "wheel"),
    ),
}


def missing_optional_drivers(drivers: dict[str, OptionalDriver] | None = None) -> list[str]:
    """The keys this environment cannot satisfy, in the record's order.

    Decided by asking each entry, not by watching which tests skipped. That is
    deliberate: a skip census depends on which tests were selected, breaks under
    `-k`, `-x` and `xdist`, and reports nothing at all when a run dies early —
    while what an environment has is a fact that holds whether or not a single
    test ran.
    """
    drivers = OPTIONAL_DRIVERS if drivers is None else drivers
    return [name for name, driver in drivers.items() if not driver.available()]


def disclosure_lines(
    missing: list[str], drivers: dict[str, OptionalDriver] | None = None
) -> list[str]:
    """The note, or nothing at all when the environment is complete.

    Pure, so the note can be tested for drivers that are in fact installed —
    otherwise the "nothing is missing" case would only be checkable on a machine
    that happened to have all five, which is not the machine this repository
    demonstrably runs on.

    The wording claims nothing about the result. An earlier draft opened with "A
    green result above does not cover the following", which is false on a failing
    or interrupted run — and the note fires on those too, deliberately, because
    their reader needs it no less.
    """
    drivers = OPTIONAL_DRIVERS if drivers is None else drivers
    if not missing:
        return []

    lines = [
        "Optional drivers were absent from this environment.",
        "Whatever this run reported, it did not check the following:",
    ]
    lines.extend(f"  {drivers[name].label}: {drivers[name].claim}" for name in missing)
    lines.append("  Install them with `pip install -e '.[dev]'`; see README.md under \"Develop\".")
    return lines


def pytest_terminal_summary(terminalreporter) -> None:
    """Say what the run did not test, after saying what it did.

    `pytest_terminal_summary` rather than a print at collection time: it fires once
    per invocation — including a failed or interrupted one, whose reader needs this
    no less — writes through the terminal reporter so it honours `-q` and capture,
    and cannot fail or error a test.

    **The whole body is guarded**, which review 2 showed was not merely belt and
    braces. An earlier version claimed the no-exit-status-effect property held "by
    construction"; it did not. Only the *import* probe was protected, so any other
    predicate that raised escaped into pytest and produced an `INTERNALERROR` with
    a non-zero exit on a run whose tests had all passed. The `psycopg` entry is
    already a non-import predicate, so that was one entry away from being live.

    A report about the environment must never be the reason a run fails. If this
    cannot render, it says so in one line and gets out of the way — the run's
    verdict is not the disclosure's to change.

    `SystemExit` is caught alongside `Exception` for the same reason the probe
    catches it, and review 3 found it missing here after review 2 added it there:
    a predicate raising `SystemExit(3)` skipped the summary entirely and set the
    run's exit status to 3, on a run whose tests had all passed. Guarding the
    probe and not the report left the hole one level out.

    `KeyboardInterrupt` still propagates, being neither, for the reason FR-046
    records: finishing a report is not worth ignoring Ctrl-C.
    """
    try:
        lines = disclosure_lines(missing_optional_drivers())
        if not lines:
            return

        terminalreporter.write_sep("=", "not proven by this run", yellow=True)
        for line in lines:
            terminalreporter.write_line(line)
    except (Exception, SystemExit) as exc:  # pragma: no cover - exercised via subprocess
        terminalreporter.write_line(
            f"could not report what this run did not prove: {exc!r} "
            "(tests/conftest.py, OPTIONAL_DRIVERS)"
        )
