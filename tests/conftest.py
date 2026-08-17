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
