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
# The record, the probe, the renderer and the hook. One record with three
# consumers — the terminal note below, the README paragraph under "Develop", and
# the consistency check in `tests/test_optional_drivers.py` — because the failure
# being fixed is a log and a document disagreeing about what was tested, and two
# records would reintroduce it one refactor later.

#: Importable module → the claim the suite cannot check without it.
#:
#: Keys are exactly the modules the suite gates on with `pytest.importorskip`,
#: and `test_optional_drivers.py` enforces that equality in both directions: a new
#: gate nobody disclosed fails, and a disclosed driver nothing gates on fails too.
#: One-directional would let this decay into a list of historical claims.
#:
#: Values are prose and are nobody's to derive — no mechanism can infer "FR-022
#: goes unproven" from the absence of a module. So the *set* is checked and the
#: descriptions are left to review, which catches the drift that happens by
#: accident and not the entry that was always wrong.
OPTIONAL_DRIVERS: dict[str, str] = {
    # The entry this story was filed for. `FakeProperties` in test_amqp.py accepts
    # any kwargs at all, so without real pika nothing would notice
    # `publish_properties()` emitting a key `pika.BasicProperties` rejects.
    "pika": (
        "FR-022 (persistent messages with a JSON content type) was checked only against the "
        "test double, not against a real pika.BasicProperties"
    ),
    # Found by the consistency check below rather than by hand, which is the whole
    # argument for having it: `test_schema.py` already carried a comment saying
    # these skip on the stream plan's install, and the disclosure would have
    # shipped without them anyway.
    "jsonschema": (
        "the published schema in schema/usage-record.v1.json was not run against a JSON Schema "
        "engine — payload validity was checked only by this package's own code"
    ),
    # Named for the driver rather than the server: `pgserver` supplies a server
    # when no DSN is set, but neither is any use without psycopg, and the skip
    # reason above already explains both halves to whoever hits it.
    "psycopg": (
        "the real-Postgres suite did not run — the writer, the source and the migrations were "
        f"not exercised against a server (set ${DSN_ENV_VAR}, or install the dev extra for an "
        "embedded one via pgserver)"
    ),
    "pglast": (
        "the migration SQL was not parsed by libpg_query, the server's own parser — only the "
        "checks that need no parser ran"
    ),
    "build": (
        "SC-001 was checked only against the packaging declaration, not by building a wheel and "
        "looking inside it for the migrations"
    ),
}


def _driver_is_importable(module_name: str) -> bool:
    """Whether `module_name` can be imported at all.

    Any exception counts as "no", not just `ImportError`: a package that is
    installed but explodes on import is exactly as unable to prove FR-022 as a
    missing one, and this runs inside a reporting hook that must never raise.
    """
    import importlib

    try:
        importlib.import_module(module_name)
    except Exception:
        return False
    return True


def missing_optional_drivers(drivers: dict[str, str] | None = None) -> list[str]:
    """The absent drivers, in the record's order.

    Absence is decided by importing, not by watching which tests skipped. That is
    deliberate: a skip census depends on which tests were selected, breaks under
    `-k`, `-x` and `xdist`, and reports nothing at all when a run dies early —
    while importability is a fact about the environment that holds whether or not
    a single test ran.
    """
    drivers = OPTIONAL_DRIVERS if drivers is None else drivers
    return [name for name in drivers if not _driver_is_importable(name)]


def disclosure_lines(missing: list[str], drivers: dict[str, str] | None = None) -> list[str]:
    """The note, or nothing at all when the environment is complete.

    Pure, so the note can be tested for drivers that are in fact installed —
    otherwise the "nothing is missing" case would only be checkable on a machine
    that happened to have all four, which is the environment this repository
    demonstrably does not run in.
    """
    drivers = OPTIONAL_DRIVERS if drivers is None else drivers
    if not missing:
        return []

    lines = [
        "Not proven by this run — optional drivers absent from this environment.",
        "A green result above does not cover the following:",
    ]
    lines.extend(f"  {name} is not installed: {drivers[name]}" for name in missing)
    lines.append("  Install them with `pip install -e '.[dev]'`; see README.md under \"Develop\".")
    return lines


def pytest_terminal_summary(terminalreporter) -> None:
    """Say what the run did not test, after saying what it did.

    `pytest_terminal_summary` rather than a print at collection time: it fires once
    per invocation — including a failed or interrupted one, whose reader needs this
    no less — writes through the terminal reporter so it honours `-q` and capture,
    and structurally cannot influence the exit status. That last property is what
    makes "a forfeited claim is a reporting fact, not a failure" true by
    construction rather than by care.
    """
    lines = disclosure_lines(missing_optional_drivers())
    if not lines:
        return

    terminalreporter.write_sep("=", "not proven by this run", yellow=True)
    for line in lines:
        terminalreporter.write_line(line)
