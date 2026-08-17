"""The writer's mapping, statement and transactional protocol (TOKWEIR-5).

Everything here runs with **no database and no driver**. That is deliberate:
`row_for`/`rows_for` and `INSERT_SQL` were factored out as pure values precisely
so the parts of the writer that can be checked without a server are checked on a
bare install, where the real-Postgres suite skips.

What is *not* here is whether Postgres accepts the statement or stores the values
faithfully. That is `test_postgres_integration.py`, against a real server, with no
database mocking — the project's established pattern. The fake connection below
is a protocol double for statement sequencing and nothing else.
"""

import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from tokenweir import PricingMode, UsageRecord
from tokenweir.postgres import (
    COLUMNS,
    INSERT_SQL,
    USAGE_TABLE,
    PostgresSource,
    row_for,
    rows_for,
)
from tokenweir.source import Source


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


class RecordingCursor:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def execute(self, sql, params=None):
        self._connection.events.append(("execute", sql, params))

    def executemany(self, sql, rows):
        self._connection.events.append(("executemany", sql, list(rows)))
        if self._connection.fail:
            raise RuntimeError("simulated store failure")


class RecordingConnection:
    def __init__(self, fail=False):
        self.events = []
        self.fail = fail
        self.closed = False

    def cursor(self):
        return RecordingCursor(self)

    def commit(self):
        self.events.append(("commit", None, None))

    def rollback(self):
        self.events.append(("rollback", None, None))

    def close(self):
        self.closed = True


# --- The statement (FR-019, FR-021) -------------------------------------------


def test_the_insert_targets_the_usage_table_with_every_column():
    assert INSERT_SQL.startswith(f"INSERT INTO {USAGE_TABLE} (")
    for column in COLUMNS:
        assert column in INSERT_SQL
    assert INSERT_SQL.count("%s") == len(COLUMNS)


def test_the_timestamp_falls_back_to_the_servers_clock_not_the_clients():
    """The column is NOT NULL DEFAULT now(), and a default applies when a column
    is *omitted* — not when NULL is passed for it. Coalescing in the statement is
    what makes a record with no timestamp get one clock's reading rather than
    whichever consumer host happened to write it."""
    assert "COALESCE(%s::timestamptz, now())" in INSERT_SQL
    assert INSERT_SQL.count("COALESCE(") == 1


def test_the_table_name_is_still_gateway_usage():
    """Renaming would force a data migration on a live database — the exact risk
    ADR-0001 Pillar 5 names — and TOKWEIR-10's acceptance is "no regression in
    gateway_usage contents"."""
    assert USAGE_TABLE == "gateway_usage"


# --- The mapping (FR-021) -----------------------------------------------------


def test_every_field_reaches_its_column():
    record = make_record(
        request_id="req-42",
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
    )
    row = dict(zip(COLUMNS, row_for(record)))

    assert row["schema_version"] == 1
    assert row["request_id"] == "req-42"
    assert row["app_id"] == "gateway"
    assert row["endpoint"] == "/chat"
    assert row["model"] == "qwen3:14b"
    assert row["status"] == "error"
    assert row["workload"] == "review"
    assert row["queue"] == "chat"
    assert row["parent_request_id"] == "req-parent"
    assert row["input_tokens"] == 11
    assert row["output_tokens"] == 22
    assert row["cache_creation_input_tokens"] == 33
    assert row["cache_read_input_tokens"] == 44
    assert row["latency_ms"] == 555
    assert row["ts"] == datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)


def test_pricing_mode_is_stored_as_its_wire_string_not_a_python_repr():
    """The column is TEXT and the contract's wire form is the plain string; an
    enum's `repr` in the database would be a Python detail leaking into a store
    other languages read."""
    row = dict(zip(COLUMNS, row_for(make_record(pricing_mode=PricingMode.SUBSCRIPTION))))
    assert row["pricing_mode"] == "subscription"
    assert isinstance(row["pricing_mode"], str)


def test_an_unset_pricing_mode_is_null():
    row = dict(zip(COLUMNS, row_for(make_record())))
    assert row["pricing_mode"] is None


def test_optional_fields_left_unset_become_nulls_not_empty_strings():
    row = dict(zip(COLUMNS, row_for(make_record())))
    assert row["workload"] is None
    assert row["queue"] is None
    assert row["parent_request_id"] is None
    assert row["latency_ms"] is None


def test_zero_counts_are_stored_as_zero_not_null():
    """Zero and unknown are different facts about a call."""
    row = dict(zip(COLUMNS, row_for(make_record())))
    for column in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        assert row[column] == 0


@pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
def test_an_absent_or_blank_timestamp_defers_to_the_server(blank):
    """Blank is legal on the contract — only identity fields are blank-checked —
    so treating `""` as an error while treating `None` as a default would invent a
    distinction the producer never agreed to."""
    row = dict(zip(COLUMNS, row_for(make_record(ts=blank))))
    assert row["ts"] is None


def test_a_naive_timestamp_is_read_as_utc():
    """The contract documents `ts` as ISO-8601 UTC. Letting the server apply its
    own TimeZone setting instead would make the stored instant depend on a
    configuration the producer cannot see."""
    row = dict(zip(COLUMNS, row_for(make_record(ts="2026-08-15T12:30:00"))))
    assert row["ts"] == datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)


def test_an_offset_timestamp_keeps_the_instant_it_denotes():
    row = dict(zip(COLUMNS, row_for(make_record(ts="2026-08-15T08:30:00-04:00"))))
    assert row["ts"] == datetime(
        2026, 8, 15, 8, 30, tzinfo=timezone(timedelta(hours=-4))
    )
    assert row["ts"] == datetime(2026, 8, 15, 12, 30, tzinfo=timezone.utc)


def test_an_unparsable_timestamp_names_the_record():
    """The caller is a consumer holding a batch; "one of these is malformed" is
    not an actionable message."""
    record = make_record(request_id="req-bad", ts="last tuesday")
    with pytest.raises(ValueError, match="req-bad"):
        row_for(record)


def test_a_non_record_is_refused_rather_than_written_as_nulls():
    """The guarded emit seam returns `None` on a drop. A `None` arriving here and
    being written as a row of NULLs is exactly the "crashes into garbage" outcome
    that seam exists to prevent."""
    for value in (None, {"request_id": "req-1"}, "req-1", 42):
        with pytest.raises(TypeError):
            row_for(value)


def test_a_batch_is_validated_whole_or_not_at_all():
    good = make_record(request_id="req-good")
    bad = make_record(request_id="req-bad", ts="not a timestamp")
    with pytest.raises(ValueError, match="req-bad"):
        rows_for([good, bad, good])


def test_rows_for_preserves_order():
    records = [make_record(request_id=f"req-{i}") for i in range(5)]
    rows = rows_for(records)
    index = COLUMNS.index("request_id")
    assert [row[index] for row in rows] == [f"req-{i}" for i in range(5)]


# --- write() (FR-015 … FR-020) ------------------------------------------------


def test_a_batch_is_one_executemany_in_one_transaction():
    """The property a broker consumer needs: it can ack after `write` returns and
    know that either every record is durable or none is."""
    conn = RecordingConnection()
    source = PostgresSource(conn)
    written = source.write([make_record(request_id=f"req-{i}") for i in range(3)])

    assert written == 3
    kinds = [kind for kind, _, _ in conn.events]
    assert kinds == ["executemany", "commit"]
    _, sql, rows = conn.events[0]
    assert sql == INSERT_SQL
    assert len(rows) == 3


def test_an_empty_batch_costs_nothing():
    """A consumer's flush timer firing on an idle broker must not open a
    transaction — that is one pointless round trip per tick, forever."""
    conn = RecordingConnection()
    assert PostgresSource(conn).write([]) == 0
    assert conn.events == []


def test_an_unwritable_record_fails_before_a_transaction_is_opened():
    """So a single poison record cannot half-write the batch around it."""
    conn = RecordingConnection()
    source = PostgresSource(conn)
    with pytest.raises(ValueError, match="req-bad"):
        source.write(
            [make_record(), make_record(request_id="req-bad", ts="nope"), make_record()]
        )
    assert conn.events == []


def test_a_store_failure_rolls_back_and_propagates():
    """Deliberately the opposite of `Sink.emit`. This side is off the critical
    path, and a swallowed failure here is silent data loss; a caller that can
    retry needs to know it must."""
    conn = RecordingConnection(fail=True)
    with pytest.raises(RuntimeError, match="simulated store failure"):
        PostgresSource(conn).write([make_record()])
    assert [kind for kind, _, _ in conn.events] == ["executemany", "rollback"]


def test_a_failing_rollback_does_not_mask_the_real_failure():
    """Rolling back can itself fail when the connection is gone. The original
    failure is the one the caller must see."""

    class BrokenRollback(RecordingConnection):
        def rollback(self):
            raise RuntimeError("connection is gone")

    conn = BrokenRollback(fail=True)
    with pytest.raises(RuntimeError, match="simulated store failure"):
        PostgresSource(conn).write([make_record()])


def test_it_satisfies_the_source_protocol():
    assert isinstance(PostgresSource(RecordingConnection()), Source)


def test_a_generator_of_records_is_accepted():
    """`Source.write` takes an Iterable; a consumer draining a buffer naturally
    has one."""
    conn = RecordingConnection()
    written = PostgresSource(conn).write(make_record() for _ in range(2))
    assert written == 2


# --- The connection it will accept --------------------------------------------


def test_an_autocommit_connection_is_refused():
    """FR-016's "one transaction per batch" is not a property of this class alone
    — an autocommit connection makes every row durable on its own, so a failure
    partway leaves a partial batch and the consumer's ack becomes a lie. The
    docstring said so and nothing enforced it; a silent loss of atomicity shows up
    as occasional partial data long after anyone would connect it to this."""
    conn = RecordingConnection()
    conn.autocommit = True

    with pytest.raises(ValueError, match="autocommit"):
        PostgresSource(conn)


def test_a_connection_with_no_notion_of_autocommit_is_accepted():
    """`autocommit` is psycopg's attribute, not DB-API's. A connection that does
    not have one is not in autocommit mode, and must not be refused for lacking a
    property it was never obliged to have."""
    conn = RecordingConnection()
    assert not hasattr(conn, "autocommit")
    assert PostgresSource(conn).write([make_record()]) == 1


# --- Ownership and close() ----------------------------------------------------


def test_a_borrowed_connection_is_left_open():
    """Closing something handed to us would surprise whoever else holds it —
    including a pool, for which "closed" and "returned" are not the same thing."""
    conn = RecordingConnection()
    source = PostgresSource(conn)
    source.close()
    assert conn.closed is False


def test_an_owned_connection_is_closed():
    conn = RecordingConnection()
    source = PostgresSource(conn, owns_connection=True)
    source.close()
    assert conn.closed is True


def test_close_is_safe_to_call_more_than_once():
    """Required by the `Source` protocol."""

    class CountingConnection(RecordingConnection):
        closes = 0

        def close(self):
            type(self).closes += 1

    conn = CountingConnection()
    source = PostgresSource(conn, owns_connection=True)
    source.close()
    source.close()
    assert CountingConnection.closes == 1


def test_the_connection_is_reachable_for_a_caller_that_needs_it():
    conn = RecordingConnection()
    assert PostgresSource(conn).connection is conn


# --- Dependency-light core (FR-022, FR-023, SC-016) ---------------------------


def test_importing_the_store_modules_does_not_import_a_driver():
    """ADR-0001 Pillar 2: an installation that only emits must not compile in a
    database library. Asserted in a subprocess against `sys.modules`, which is
    true whether or not psycopg happens to be installed here — the alternative,
    uninstalling it, would make this test depend on the environment it is meant
    to be independent of."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tokenweir, tokenweir.migrations, tokenweir.postgres;"
            "assert 'psycopg' not in sys.modules, sorted(sys.modules);"
            "assert 'psycopg2' not in sys.modules;"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_connecting_without_the_driver_names_the_extra(monkeypatch):
    """A bare `ModuleNotFoundError: psycopg` leaves a caller to work out what to
    install and whether they even need to."""
    from tokenweir import migrations

    # `sys.modules[name] = None` is the documented way to make `import name`
    # raise ImportError, so this exercises the real path rather than a stub of it.
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError, match=r"tokenweir\[postgres\]"):
        migrations.connect("postgresql:///nowhere")


def test_from_dsn_without_the_driver_names_the_extra(monkeypatch):
    monkeypatch.setitem(sys.modules, "psycopg", None)
    with pytest.raises(ImportError, match=r"tokenweir\[postgres\]"):
        PostgresSource.from_dsn("postgresql:///nowhere")


def test_the_package_namespace_is_unchanged():
    """The store is reached by its own import path, mirroring how transport
    adapters are, so `import tokenweir` never drags in store code."""
    import tokenweir

    assert "PostgresSource" not in tokenweir.__all__
    assert "migrations" not in tokenweir.__all__
