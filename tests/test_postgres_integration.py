"""Correctness against a real Postgres (TOKWEIR-5).

These are the tests the story's third acceptance clause names. They use a real
server and no database mocking, per the project's pattern, and they **skip** with
a message naming `TOKENWEIR_TEST_DSN` when none is configured — see
`tests/conftest.py`.

Read that skip honestly: this project's automated environment has no Postgres and
cannot get one, so in CI this file contributes skips rather than passes. What is
asserted here — the SQL Postgres actually accepts, the rollup's arithmetic, the
transactional behaviour — is asserted **nowhere else**, because faking it would
only assert that the code calls the fake. The compensating control is
`test_migration_sql.py`, which pins the properties that can be read off the text
in an environment with no server; it is not a substitute for this file and is not
offered as one.

To run them:

    createdb tokenweir_scratch
    TOKENWEIR_TEST_DSN=postgresql:///tokenweir_scratch pytest tests/test_postgres_integration.py
"""

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


def test_a_configured_role_is_granted_select(scratch_schema, psycopg_module):
    connect, schema = scratch_schema
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
    created_here = not exists

    try:
        apply(connection, reader_role=role)
        granted = fetch(
            connection,
            "SELECT table_name FROM information_schema.role_table_grants "
            "WHERE grantee = %s AND privilege_type = 'SELECT' AND table_schema = %s",
            (role, schema),
        )
        assert {row[0] for row in granted} >= {
            "gateway_usage",
            "model_pricing_rates",
            "gateway_usage_daily",
        }
    finally:
        if created_here:
            with connection.cursor() as cursor:
                cursor.execute(f'REASSIGN OWNED BY "{role}" TO CURRENT_USER')
                cursor.execute(f'DROP OWNED BY "{role}"')
                cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
            connection.commit()


def test_a_role_that_does_not_exist_is_a_notice_not_a_failure(scratch_schema):
    connect, _ = scratch_schema
    connection = connect()
    assert len(apply(connection, reader_role="no_such_role_anywhere")) == 6
