"""Shared fixtures — chiefly the gate on the real-Postgres suite (TOKWEIR-5).

The project's pattern is correctness tests against a **real Postgres, never a
mocked one**: a mock of a database asserts that the code calls the mock, which is
not the question anyone has about a schema. TOKWEIR-5 says to keep that pattern,
and these fixtures are how it is kept without making the rest of the suite
depend on a server.

`TOKENWEIR_TEST_DSN` turns the store tests on. Without it they **skip**, naming
the variable, because an environment-dependent test that cannot be evaluated must
not turn an ordinary install red — the same rule `test_repo_hygiene.py` already
holds for a tree with no `.git`.

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
    "gateway_usage is not touched)"
)


def postgres_dsn_or_none() -> str | None:
    return os.environ.get(DSN_ENV_VAR) or None


@pytest.fixture(scope="session")
def postgres_dsn() -> str:
    dsn = postgres_dsn_or_none()
    if not dsn:
        pytest.skip(SKIP_REASON)
    return dsn


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
