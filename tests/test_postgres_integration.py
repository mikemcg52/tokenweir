"""Correctness against a real Postgres (TOKWEIR-5).

These are the tests the story's third acceptance clause names. They use a real
server and no database mocking, per the project's pattern, and they **skip** with
a message naming `TOKENWEIR_TEST_DSN` when none is configured — see
`tests/conftest.py`.

A bare `pip install -e .` skips this file. `pip install -e '.[dev]'` does not:
`pgserver` ships the server binaries in its wheel, so `conftest.py` starts a
throwaway PostgreSQL with no root, no apt and no Docker. An earlier draft of this
story recorded "no usable Postgres" as an environment fact and never tested the
claim; **run this file before believing a change to the runner or the SQL.** Two
of this story's review findings were only visible here — a concurrency defect the
statement-order tests could not see, and a `BOOL_AND` that a string grep declared
covered while `BOOL_OR` passed everything else.

What is asserted here — the SQL Postgres actually accepts, the rollup's
arithmetic, the transactional and concurrent behaviour — is asserted **nowhere
else**, because faking it would only assert that the code calls the fake.
`test_migration_sql.py` is a second line for the bare-install case, not a
substitute for this file, and is not offered as one.

To run them:

    pip install -e '.[dev]' && pytest tests/test_postgres_integration.py
    # or against a server you chose:
    TOKENWEIR_TEST_DSN=postgresql:///tokenweir_scratch pytest tests/test_postgres_integration.py
"""

import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tokenweir import PricingMode, UsageRecord
from tokenweir.migrations import (
    Migration,
    MigrationChecksumError,
    UnknownAppliedVersionError,
    applied_versions,
    apply,
    discover,
    pending,
    status,
)
from tokenweir.postgres import PostgresSource

SHIPPED = discover()


def make_record(**overrides):
    fields = {
        "request_id": "req-1",
        "app_id": "mado",
        "endpoint": "/v1/messages",
        "model": "claude-opus-5",
        "status": "ok",
    }
    fields.update(overrides)
    return UsageRecord(**fields)


def relations(connection, schema):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            (schema,),
        )
        return {row[0] for row in cursor.fetchall()}


def fetch(connection, sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


# --- Applying the schema (US1) ------------------------------------------------


def test_an_empty_database_gets_the_whole_schema(scratch_schema):
    connect, schema = scratch_schema
    connection = connect()

    applied = apply(connection)

    assert [m.version for m in applied] == [1, 2, 3, 4, 5, 6]
    assert {
        "gateway_usage",
        "model_pricing_rates",
        "gateway_usage_daily",
        "schema_migrations",
    } <= relations(connection, schema)


def test_applying_twice_changes_nothing(scratch_schema):
    """Idempotence is what makes running the migrator on every deploy safe."""
    connect, _ = scratch_schema
    connection = connect()

    apply(connection)
    before = fetch(connection, "SELECT version, name, checksum FROM schema_migrations")

    assert apply(connection) == ()
    after = fetch(connection, "SELECT version, name, checksum FROM schema_migrations")
    assert after == before


def test_only_the_missing_migrations_are_applied(scratch_schema, monkeypatch):
    connect, _ = scratch_schema
    connection = connect()

    monkeypatch.setattr("tokenweir.migrations.discover", lambda: SHIPPED[:3])
    apply(connection)
    assert set(applied_versions(connection)) == {1, 2, 3}

    monkeypatch.undo()
    applied = apply(connection)
    assert [m.version for m in applied] == [4, 5, 6]


def test_a_failing_migration_leaves_nothing_behind(scratch_schema, monkeypatch):
    """One transaction per migration: a half-applied version can never be
    recorded as applied."""
    connect, schema = scratch_schema
    connection = connect()

    broken = Migration(
        version=7,
        name="broken",
        sql="CREATE TABLE half_applied (a int); SELECT this_function_does_not_exist();",
    )
    monkeypatch.setattr("tokenweir.migrations.discover", lambda: (*SHIPPED, broken))

    with pytest.raises(Exception):
        apply(connection)

    connection.rollback()
    assert 7 not in applied_versions(connection)
    assert "half_applied" not in relations(connection, schema)
    # Everything before it survived, because each committed on its own.
    assert set(applied_versions(connection)) == {1, 2, 3, 4, 5, 6}


def test_two_migrators_racing_produce_one_set_of_rows(scratch_schema):
    """The advisory lock's whole job. Without it both try to create the same
    objects and one loses in a way that depends on timing."""
    connect, _ = scratch_schema
    first, second = connect(), connect()
    errors = []

    def run(connection):
        try:
            apply(connection)
        except Exception as exc:  # pragma: no cover - a failure here is the finding
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(c,)) for c in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert set(applied_versions(first)) == {1, 2, 3, 4, 5, 6}
    counts = fetch(first, "SELECT version, count(*) FROM schema_migrations GROUP BY version")
    assert all(count == 1 for _, count in counts)


@pytest.mark.parametrize(
    "operations",
    [("status", "apply"), ("status", "status"), ("apply", "status")],
    ids=["status+apply", "status+status", "apply+status"],
)
def test_a_concurrent_reader_does_not_collide_with_a_migrator(scratch_schema, operations):
    """Racing two `apply`s is not enough to pin FR-012.

    On a *fresh* database, whichever call arrives first is the one that creates
    `schema_migrations` — and on a deploying cluster that is as likely to be a
    health check's `status` as the deploy's own `apply`. With the lock held only
    by `apply`, a concurrent `status` made the *apply* die on a catalog
    duplicate-key error, and a `status` that raises is itself the "refused a
    description of the database" outcome FR-038 and FR-042 exist to prevent.

    Barrier-synchronised so both sides reach the create at the same moment; the
    window is small enough that unsynchronised threads miss it.
    """
    connect, _ = scratch_schema
    barrier = threading.Barrier(len(operations))
    errors = []

    def run(operation):
        connection = connect()
        try:
            barrier.wait(timeout=30)
            apply(connection) if operation == "apply" else status(connection)
        except Exception as exc:  # pragma: no cover - a failure here is the finding
            errors.append(f"{operation}: {type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=run, args=(op,)) for op in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []


def test_a_database_ahead_of_the_library_is_refused(migrated):
    with migrated.cursor() as cursor:
        cursor.execute(
            "INSERT INTO schema_migrations (version, name, checksum) VALUES (99, 'future', 'x')"
        )
    migrated.commit()

    with pytest.raises(UnknownAppliedVersionError, match="99"):
        pending(migrated)


def test_an_edited_released_migration_is_refused(migrated):
    with migrated.cursor() as cursor:
        cursor.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    migrated.commit()

    with pytest.raises(MigrationChecksumError, match="001_gateway_usage.sql"):
        pending(migrated)


def gateway_shaped_state_table(connection):
    """Reduce `schema_migrations` to the shape the AI Gateway's runner left it in.

    The gateway recorded a version and a timestamp; `name` and `checksum` are this
    runner's additions (FR-038). Dropping them here is how a test in a pod with no
    gateway checkout gets a genuinely pre-existing state table rather than a
    hand-written approximation of one — the rows above it were applied for real.
    """
    with connection.cursor() as cursor:
        cursor.execute("ALTER TABLE schema_migrations DROP COLUMN checksum")
        cursor.execute("ALTER TABLE schema_migrations DROP COLUMN name")
    connection.commit()


def test_a_state_table_that_predates_the_checksum_column_is_adopted(
    scratch_schema, monkeypatch
):
    """Pillar 5's ownership move points this runner at a database the gateway
    already migrated. Its `schema_migrations` has no `checksum` column, so
    `CREATE TABLE IF NOT EXISTS` no-ops and the next read used to die on
    `UndefinedColumn` — the first thing TOKWEIR-10 would have hit."""
    connect, _ = scratch_schema
    connection = connect()

    monkeypatch.setattr("tokenweir.migrations.discover", lambda: SHIPPED[:3])
    apply(connection)
    monkeypatch.undo()
    gateway_shaped_state_table(connection)

    applied = apply(connection)

    assert [m.version for m in applied] == [4, 5, 6]
    assert set(applied_versions(connection)) == {1, 2, 3, 4, 5, 6}


def test_adopted_rows_keep_a_null_checksum_and_are_not_drift(scratch_schema, monkeypatch):
    """A checksum invented for a row somebody else applied would be a lie about
    what ran. NULL says "applied before this runner recorded checksums", and
    `pending` must read that as unverifiable rather than as an edited migration."""
    connect, _ = scratch_schema
    connection = connect()

    monkeypatch.setattr("tokenweir.migrations.discover", lambda: SHIPPED[:3])
    apply(connection)
    monkeypatch.undo()
    gateway_shaped_state_table(connection)
    apply(connection)

    recorded = dict(fetch(connection, "SELECT version, checksum FROM schema_migrations"))
    assert [version for version, checksum in sorted(recorded.items()) if checksum is None] == [
        1,
        2,
        3,
    ]
    assert recorded[4] == SHIPPED[3].checksum

    # And the adopted rows do not make the next run refuse the database.
    assert pending(connection) == ()
    assert apply(connection) == ()


def test_status_can_still_describe_a_state_table_that_predates_us(
    scratch_schema, monkeypatch
):
    """FR-038's stated intent is that reporting never gets refused. A `status`
    that cannot describe the one database an operator is most likely to point it
    at fails that intent in the case it was written for."""
    connect, _ = scratch_schema
    connection = connect()

    monkeypatch.setattr("tokenweir.migrations.discover", lambda: SHIPPED[:3])
    apply(connection)
    monkeypatch.undo()
    gateway_shaped_state_table(connection)

    done, outstanding = status(connection)

    assert [m.version for m in done] == [1, 2, 3]
    assert [m.version for m in outstanding] == [4, 5, 6]


def test_the_functional_index_exists_and_is_buildable(migrated):
    """`DATE_TRUNC('day', ts)` here would simply fail: over a TIMESTAMPTZ it is
    STABLE, not IMMUTABLE. That failure is the origin of the preserved fix, so
    this test passing *is* the fix being in place."""
    indexes = {
        row[0]
        for row in fetch(
            migrated,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'gateway_usage'",
        )
    }
    assert "gateway_usage_app_day_idx" in indexes


# --- The writer (US2) ---------------------------------------------------------


def test_a_batch_round_trips_field_for_field(migrated):
    records = [
        make_record(
            request_id="req-full",
            app_id="gateway",
            endpoint="/chat",
            model="qwen3:14b",
            status="error",
            workload="review",
            queue="chat",
            parent_request_id="req-parent",
            input_tokens=11,
            output_tokens=22,
            cache_creation_input_tokens=33,
            cache_read_input_tokens=44,
            latency_ms=555,
            pricing_mode=PricingMode.API_METERED,
            ts="2026-08-15T12:30:00Z",
        ),
        make_record(request_id="req-minimal"),
    ]

    assert PostgresSource(migrated).write(records) == 2

    rows = fetch(
        migrated,
        "SELECT request_id, app_id, endpoint, model, status, workload, queue, "
        "parent_request_id, input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, latency_ms, pricing_mode, schema_version, ts "
        "FROM gateway_usage ORDER BY request_id",
    )
    full = rows[0]
    assert full[:8] == (
        "req-full",
        "gateway",
        "/chat",
        "qwen3:14b",
        "error",
        "review",
        "chat",
        "req-parent",
    )
    assert full[8:14] == (11, 22, 33, 44, 555, "api_metered")
    assert full[14] == 1
    assert full[15] == datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)

    minimal = rows[1]
    assert minimal[5:8] == (None, None, None)
    assert minimal[8:12] == (0, 0, 0, 0)
    assert minimal[12:14] == (None, None)


def test_a_record_without_a_timestamp_gets_the_servers_clock(migrated):
    before = fetch(migrated, "SELECT now()")[0][0]
    PostgresSource(migrated).write([make_record(ts=None), make_record(ts="")])
    after = fetch(migrated, "SELECT now()")[0][0]

    stamps = [row[0] for row in fetch(migrated, "SELECT ts FROM gateway_usage")]
    assert len(stamps) == 2
    assert all(before <= stamp <= after for stamp in stamps)


def test_a_failing_batch_leaves_no_rows(migrated):
    """The property a consumer acking after the write depends on."""
    with migrated.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE gateway_usage ADD CONSTRAINT no_boom CHECK (app_id <> 'boom')"
        )
    migrated.commit()

    source = PostgresSource(migrated)
    with pytest.raises(Exception):
        source.write(
            [
                make_record(request_id="req-a"),
                make_record(request_id="req-b", app_id="boom"),
                make_record(request_id="req-c"),
            ]
        )

    migrated.rollback()
    assert fetch(migrated, "SELECT count(*) FROM gateway_usage")[0][0] == 0


def test_a_duplicate_request_id_is_accepted(migrated):
    """An at-least-once broker redelivers, and `/compare` legitimately emits
    several records under one parent. A unique constraint would turn an ordinary
    redelivery into a poison message."""
    PostgresSource(migrated).write([make_record(request_id="req-same")] * 2)
    assert fetch(migrated, "SELECT count(*) FROM gateway_usage")[0][0] == 2


def test_an_empty_batch_touches_nothing(migrated):
    assert PostgresSource(migrated).write([]) == 0
    assert fetch(migrated, "SELECT count(*) FROM gateway_usage")[0][0] == 0


def test_a_token_count_beyond_a_32_bit_integer_is_storable(migrated):
    """The columns are BIGINT because the contract's only bound is non-negative.
    A producer summing a long turn is exactly where a count creeps past 2^31."""
    PostgresSource(migrated).write([make_record(input_tokens=2**31 + 7)])
    assert fetch(migrated, "SELECT input_tokens FROM gateway_usage")[0][0] == 2**31 + 7


# --- The command line, against a real driver (FR-013) -------------------------


def test_the_cli_applies_and_reports_against_a_real_database(
    scratch_schema, postgres_dsn, monkeypatch, capsys
):
    """Every other CLI test stubs `connect`, so the one path an operator actually
    runs — argparse through psycopg to a server — was never exercised end to end.

    `PGOPTIONS` rather than a fixture connection: the CLI opens its own, so the
    scratch schema has to reach it through libpq's own environment.
    """
    from tokenweir.migrations import __main__ as cli

    _, schema = scratch_schema
    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

    assert cli.main(["apply", "--dsn", postgres_dsn]) == 0
    assert "006_gateway_usage_app_day_index.sql" in capsys.readouterr().out

    # Re-applying is a no-op, and `status` describes what happened.
    assert cli.main(["--dsn", postgres_dsn, "apply"]) == 0
    assert "already up to date" in capsys.readouterr().out

    assert cli.main(["status", "--dsn", postgres_dsn, "--verify-checksums"]) == 0
    out = capsys.readouterr().out
    assert "pending:  (none)" in out
    assert "001_gateway_usage.sql" in out


def test_the_cli_reports_a_drifted_database_without_a_traceback(
    scratch_schema, postgres_dsn, monkeypatch, capsys
):
    """FR-013 and FR-038 meeting: the operator asked to be told about drift, and
    what they get is one line and a non-zero exit."""
    from tokenweir.migrations import __main__ as cli

    connect, schema = scratch_schema
    connection = connect()
    apply(connection)
    with connection.cursor() as cursor:
        cursor.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
    connection.commit()

    monkeypatch.setenv("PGOPTIONS", f"-c search_path={schema}")

    assert cli.main(["status", "--dsn", postgres_dsn, "--verify-checksums"]) == 1
    captured = capsys.readouterr()
    assert "001_gateway_usage.sql" in captured.err
    assert "Traceback" not in captured.err


def test_apply_refuses_an_autocommit_connection_rather_than_lose_the_grant(
    scratch_schema, reader_role
):
    """The refusal is not fastidiousness — this is what it prevents.

    `set_config('tokenweir.reader_role', %s, true)` is transaction-scoped. Under
    autocommit that transaction is the `set_config` statement, so 004 and 005 see
    no role, take their no-op branch, and are recorded as applied. The grant is
    not deferred; it is gone, and only the manual GRANT in the README recovers it.
    """
    connect, _ = scratch_schema
    connection = connect()
    connection.autocommit = True

    with pytest.raises(ValueError, match="autocommit"):
        apply(connection, reader_role=reader_role)

    # And the ordinary connection does grant, so the test above is about
    # autocommit rather than about the role never working here.
    connection.autocommit = False
    apply(connection, reader_role=reader_role)
    assert granted_relations(connection, reader_role) == {
        "gateway_usage",
        "gateway_usage_daily",
        "model_pricing_rates",
    }


def test_the_module_entry_point_runs_as_a_command(postgres_dsn, scratch_schema, monkeypatch):
    """SC-020 names `python -m tokenweir.migrations`, and every other CLI test
    calls `main()` in-process — which cannot catch a broken `__main__` guard, a
    bad module name or an import that only fails outside pytest."""
    _, schema = scratch_schema
    env = {**os.environ, "PGOPTIONS": f"-c search_path={schema}"}

    result = subprocess.run(
        [sys.executable, "-m", "tokenweir.migrations", "status", "--dsn", postgres_dsn],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "pending:" in result.stdout
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql://u:{pw}@localhost:1/db",
        "postgresql://u:{pw}@localhost:1/db?bogus=1",
        "password={pw} bogus_key=1",
        "not a dsn {pw}",
        "host=localhost password={pw} dbname=x sslmode=bogusvalue",
    ],
)
def test_a_failing_connection_does_not_echo_the_password(dsn, psycopg_module, capsys):
    """A DSN carries a password and a connect failure is printed to stderr, which
    on a deploy goes to a log somebody else can read. libpq quotes the offending
    *keyword* rather than the value, but that is a property worth pinning rather
    than assuming — the message is built by string interpolation of a driver
    exception this project does not control."""
    from tokenweir.migrations import __main__ as cli

    password = "s3cr3t-do-not-log"

    assert cli.main(["status", "--dsn", dsn.format(pw=password)]) == 1
    captured = capsys.readouterr()
    assert "error:" in captured.err
    assert password not in captured.err
    assert password not in captured.out


# --- Grants: the fixtures the reader-role tests share -------------------------


@pytest.fixture
def reader_role(scratch_schema, psycopg_module):
    """A real Postgres role, dropped afterwards, or a skip.

    Shared rather than inlined because two tests need it now: one asserting the
    grant lands, one asserting it is *not* silently lost. The second is only
    meaningful next to the first.
    """
    connect, _ = scratch_schema
    connection = connect()
    role = "tokenweir_test_reader"

    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        exists = cursor.fetchone() is not None
    if not exists:
        try:
            with connection.cursor() as cursor:
                cursor.execute(f'CREATE ROLE "{role}" NOLOGIN')
            connection.commit()
        except psycopg_module.errors.InsufficientPrivilege:
            connection.rollback()
            pytest.skip("the test role cannot be created; CREATEROLE is not held")

    try:
        yield role
    finally:
        if not exists:
            with connection.cursor() as cursor:
                cursor.execute(f'REASSIGN OWNED BY "{role}" TO CURRENT_USER')
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
            connection.commit()


def granted_relations(connection, role):
    """The relations ``role`` may SELECT from, in the connection's own schema."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_schema()")
        schema = cursor.fetchone()[0]
    return {
        row[0]
        for row in fetch(
            connection,
            "SELECT table_name FROM information_schema.role_table_grants "
            "WHERE grantee = %s AND privilege_type = 'SELECT' AND table_schema = %s",
            (role, schema),
        )
    }


# --- The rollup (US3) ---------------------------------------------------------


def price(connection, model, effective_from, input_rate, output_rate, **cache):
    with connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO model_pricing_rates (model, effective_from, "
            "input_cost_usd_per_mtok, output_cost_usd_per_mtok, "
            "cache_write_cost_usd_per_mtok, cache_read_cost_usd_per_mtok) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (
                model,
                effective_from,
                input_rate,
                output_rate,
                cache.get("cache_write"),
                cache.get("cache_read"),
            ),
        )
    connection.commit()


def rollup(connection):
    return {
        (row[0], str(row[1]), row[2], row[3]): row[4:]
        for row in fetch(
            connection,
            "SELECT app_id, usage_day, model, pricing_mode, is_priced, est_cost_usd, "
            "calls, input_tokens, output_tokens FROM gateway_usage_daily",
        )
    }


def test_a_fully_priced_group_reports_a_cost(migrated):
    price(migrated, "claude-opus-5", "2026-01-01", Decimal("15"), Decimal("75"))
    PostgresSource(migrated).write(
        [
            make_record(input_tokens=1_000_000, output_tokens=0, ts="2026-08-15T01:00:00Z"),
            make_record(input_tokens=0, output_tokens=1_000_000, ts="2026-08-15T02:00:00Z"),
        ]
    )

    key = ("mado", "2026-08-15", "claude-opus-5", None)
    is_priced, est_cost, calls, input_tokens, output_tokens = rollup(migrated)[key]
    assert is_priced is True
    assert est_cost == Decimal("90")
    assert (calls, input_tokens, output_tokens) == (2, 1_000_000, 1_000_000)


def test_one_unpriced_call_blanks_the_whole_group(migrated):
    """`BOOL_AND`, not `BOOL_OR`. A partial sum is always too low and is
    indistinguishable, when read, from a complete one."""
    price(migrated, "claude-opus-5", "2026-08-16", Decimal("15"), Decimal("75"))
    PostgresSource(migrated).write(
        [
            make_record(input_tokens=1_000_000, ts="2026-08-16T01:00:00Z"),
            # Before any rate is in force, so this call has no applicable rate.
            make_record(input_tokens=1_000_000, ts="2026-08-15T01:00:00Z"),
        ]
    )

    unpriced_day = rollup(migrated)[("mado", "2026-08-15", "claude-opus-5", None)]
    assert unpriced_day[0] is False
    assert unpriced_day[1] is None


def test_cache_tokens_without_a_cache_rate_make_a_call_unpriced(migrated):
    """Counting them at zero would understate the bill and look exactly like a
    call that used no cache."""
    price(migrated, "claude-opus-5", "2026-01-01", Decimal("15"), Decimal("75"))
    PostgresSource(migrated).write(
        [make_record(cache_read_input_tokens=1_000, ts="2026-08-15T01:00:00Z")]
    )

    row = rollup(migrated)[("mado", "2026-08-15", "claude-opus-5", None)]
    assert row[0] is False
    assert row[1] is None


def test_a_mixed_group_is_blanked_by_its_one_unpriced_call(migrated):
    """The `BOOL_AND` fix, asserted on the only shape that can distinguish it.

    Every other rollup test builds groups whose calls are *uniformly* priced or
    uniformly not — a missing rate and a `subscription` mode are both constant
    within a group, because the rate is resolved per `(model, usage_day)` and
    `pricing_mode` is a grouping key. On uniform groups `BOOL_AND` and `BOOL_OR`
    agree, so swapping one for the other left the whole real-Postgres suite green
    and only a string grep objected.

    A group can be genuinely mixed only through the cache-token clauses. This is
    that group: two calls, same app/day/model/mode, one priced outright and one
    using cache reads the rate card does not price. Under `BOOL_OR` it reports
    $15 with the cached tokens silently free — a number that is too low and reads
    exactly like a complete one, which is the failure the fix exists to prevent.
    """
    price(migrated, "claude-opus-5", "2026-01-01", Decimal("15"), Decimal("75"))
    PostgresSource(migrated).write(
        [
            make_record(
                request_id="req-priced",
                input_tokens=1_000_000,
                ts="2026-08-15T01:00:00Z",
            ),
            make_record(
                request_id="req-cache",
                cache_read_input_tokens=1_000_000,
                ts="2026-08-15T02:00:00Z",
            ),
        ]
    )

    is_priced, est_cost, calls = rollup(migrated)[
        ("mado", "2026-08-15", "claude-opus-5", None)
    ][:3]

    assert calls == 2, "the two calls must land in one group or this proves nothing"
    assert is_priced is False
    assert est_cost is None


def test_subscription_usage_is_never_priced(migrated):
    """Under a flat-rate Max subscription there is no per-call dollar, however
    complete the rate card is (ADR-0001 Pillar 4)."""
    price(migrated, "claude-opus-5", "2026-01-01", Decimal("15"), Decimal("75"))
    PostgresSource(migrated).write(
        [
            make_record(
                request_id="req-sub",
                input_tokens=1_000_000,
                pricing_mode=PricingMode.SUBSCRIPTION,
                ts="2026-08-15T01:00:00Z",
            ),
            make_record(
                request_id="req-api",
                input_tokens=1_000_000,
                pricing_mode=PricingMode.API_METERED,
                ts="2026-08-15T01:00:00Z",
            ),
        ]
    )

    rows = rollup(migrated)
    subscription = rows[("mado", "2026-08-15", "claude-opus-5", "subscription")]
    metered = rows[("mado", "2026-08-15", "claude-opus-5", "api_metered")]

    assert subscription[0] is False
    assert subscription[1] is None
    # And it did not poison the metered usage, which is why pricing_mode is in
    # the GROUP BY.
    assert metered[0] is True
    assert metered[1] == Decimal("15")


def test_a_repriced_model_does_not_restate_earlier_days(migrated):
    price(migrated, "claude-opus-5", "2026-08-01", Decimal("10"), Decimal("0"))
    price(migrated, "claude-opus-5", "2026-08-15", Decimal("20"), Decimal("0"))
    PostgresSource(migrated).write(
        [
            make_record(request_id="req-early", input_tokens=1_000_000, ts="2026-08-10T00:00:00Z"),
            make_record(request_id="req-late", input_tokens=1_000_000, ts="2026-08-20T00:00:00Z"),
        ]
    )

    rows = rollup(migrated)
    assert rows[("mado", "2026-08-10", "claude-opus-5", None)][1] == Decimal("10")
    assert rows[("mado", "2026-08-20", "claude-opus-5", None)][1] == Decimal("20")


def test_the_day_bucket_is_utc_not_the_sessions_timezone(migrated):
    """Two producers in different zones must agree on which day a call belongs to,
    and a report must not change meaning because a reader's session TimeZone
    differs from the writer's."""
    PostgresSource(migrated).write(
        [
            make_record(request_id="req-late", ts="2026-08-15T23:30:00Z"),
            make_record(request_id="req-early", ts="2026-08-16T00:30:00Z"),
        ]
    )

    with migrated.cursor() as cursor:
        cursor.execute("SET TIME ZONE 'America/New_York'")
    migrated.commit()

    days = {str(row[0]) for row in fetch(migrated, "SELECT usage_day FROM gateway_usage_daily")}
    assert days == {"2026-08-15", "2026-08-16"}


def test_an_unpriced_model_still_appears_in_the_rollup(migrated):
    """LEFT JOIN, not INNER: disappearing would hide the usage as well as the
    cost, and a missing rate card would then look like no traffic."""
    PostgresSource(migrated).write(
        [make_record(model="some-local-model", ts="2026-08-15T01:00:00Z")]
    )
    row = rollup(migrated)[("mado", "2026-08-15", "some-local-model", None)]
    assert row[0] is False
    assert row[1] is None
    assert row[2] == 1


# --- Grants (FR-030) ----------------------------------------------------------


def test_the_grant_migrations_no_op_without_a_configured_role(scratch_schema):
    """A library cannot know a deployment's role names, and failing would make an
    unconfigured database un-migratable."""
    connect, _ = scratch_schema
    connection = connect()
    assert len(apply(connection)) == 6


def test_a_configured_role_is_granted_select(scratch_schema, reader_role):
    connect, _ = scratch_schema
    connection = connect()

    apply(connection, reader_role=reader_role)

    assert granted_relations(connection, reader_role) >= {
        "gateway_usage",
        "model_pricing_rates",
        "gateway_usage_daily",
    }


def test_a_role_that_does_not_exist_is_a_notice_not_a_failure(scratch_schema):
    connect, _ = scratch_schema
    connection = connect()
    assert len(apply(connection, reader_role="no_such_role_anywhere")) == 6
