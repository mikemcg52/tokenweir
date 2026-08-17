"""The writer — a :class:`~tokenweir.source.Source` backed by Postgres.

This is the persistence half of the AI Gateway's usage-writer, re-homed per
ADR-0001 Pillar 5. It takes batches of :class:`~tokenweir.contract.UsageRecord`
and puts them in ``gateway_usage``, one transaction per batch.

**Why this may raise, when the emit side may not.** ``Sink.emit`` sits on the
metered request's critical path and must never raise into it; ``Source.write``
sits in a consumer, off that path, where a swallowed failure is silent data loss
and a caller that can retry is better served by a loud one. Both halves of that
asymmetry are deliberate and each is wrong on the other side.

**One transaction per batch** is the property a broker consumer needs: it can ack
after :meth:`PostgresSource.write` returns and know that either every record in
the batch is durable or none of them is. Batching *policy* — how many records, how
long to wait, what to do with a redelivery — belongs to the consumer, which owns
the broker; TOKWEIR-6 has it. What is guaranteed here is that the policy is
implementable.

**No driver at import time.** Nothing in this module imports psycopg; the
connection is supplied by the caller, and :meth:`PostgresSource.from_dsn` — the
one place that opens one itself — imports it on the call. That keeps ADR-0001
Pillar 2's dependency-light core true of an installation that only emits, and it
is what lets the record-to-row mapping below be tested where no database library
exists at all.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from tokenweir.contract import PricingMode, UsageRecord

__all__ = [
    "COLUMNS",
    "INSERT_SQL",
    "USAGE_TABLE",
    "PostgresSource",
    "row_for",
    "rows_for",
]

#: The table the migrations create. Named once here rather than inlined, so the
#: writer and ``001_gateway_usage.sql`` have a single point of correspondence.
#: It stays ``gateway_usage`` even though this is no longer the gateway's:
#: renaming would force a data migration on a live database, and TOKWEIR-10's
#: acceptance is "no regression in gateway_usage contents".
USAGE_TABLE = "gateway_usage"

#: Insert order. Every field of the v1 contract, in the order 001/002 declare
#: them. A test asserts this tuple against ``UsageRecord``'s fields, so a field
#: added to the contract without a migration and a column here fails the suite
#: rather than being silently dropped on write.
COLUMNS: tuple[str, ...] = (
    "schema_version",
    "request_id",
    "app_id",
    "endpoint",
    "model",
    "status",
    "workload",
    "queue",
    "parent_request_id",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "latency_ms",
    "pricing_mode",
    "ts",
)

#: ``ts`` is ``COALESCE(%s::timestamptz, now())`` rather than a plain placeholder:
#: the column is ``NOT NULL DEFAULT now()``, and a default only applies when the
#: column is *omitted*, not when NULL is passed for it. Coalescing in the
#: statement means a record with no timestamp is stamped by the server — one
#: clock — rather than by whichever consumer host happened to write it.
INSERT_SQL = (
    f"INSERT INTO {USAGE_TABLE} ({', '.join(COLUMNS)}) VALUES ("
    + ", ".join(
        "COALESCE(%s::timestamptz, now())" if column == "ts" else "%s"
        for column in COLUMNS
    )
    + ")"
)


def _timestamp_for(record: UsageRecord) -> Optional[datetime]:
    """Parse the record's ``ts``, or ``None`` to let the server stamp it.

    The contract type-checks ``ts`` but does not parse it — it is an opaque
    optional string there — so an unparsable value can legitimately reach the
    writer and has to be dealt with here.

    Absent and blank both mean "no timestamp". Blank is legal on the contract
    (only identity fields are blank-checked), and treating ``""`` as an error
    while treating ``None`` as a default would be a distinction the producer
    never agreed to.

    A naive value is read as UTC, because the contract documents the field as
    "ISO-8601 UTC" — the alternative, letting the server apply its own TimeZone
    setting, would make the stored instant depend on a server configuration the
    producer cannot see.

    Raises:
        ValueError: the string is not an ISO-8601 timestamp. Naming the record is
            deliberate: the caller is a consumer holding a batch, and "one of
            these is malformed" is not an actionable message.
    """
    raw = record.ts
    if raw is None or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(
            f"usage record {record.request_id!r} has an unparsable ts {raw!r}: {exc}"
        ) from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def row_for(record: UsageRecord) -> tuple[Any, ...]:
    """Map one record to an insert row, in :data:`COLUMNS` order.

    Pure: no database, no driver, no clock. That is what makes it testable in an
    environment with neither, which is the environment this project's automated
    suite actually runs in.

    Raises:
        TypeError: ``record`` is not a :class:`~tokenweir.contract.UsageRecord`.
            Checked rather than duck-typed because the guarded emit seam returns
            ``None`` on a drop, and a ``None`` reaching the store as a row of
            NULLs would be exactly the "crashes into garbage" outcome that seam
            was built to avoid.
        ValueError: the record's ``ts`` cannot be parsed.
    """
    if not isinstance(record, UsageRecord):
        raise TypeError(
            f"expected a UsageRecord; got {type(record).__name__}"
        )
    pricing_mode = record.pricing_mode
    return (
        record.schema_version,
        record.request_id,
        record.app_id,
        record.endpoint,
        record.model,
        record.status,
        record.workload,
        record.queue,
        record.parent_request_id,
        record.input_tokens,
        record.output_tokens,
        record.cache_creation_input_tokens,
        record.cache_read_input_tokens,
        record.latency_ms,
        pricing_mode.value if isinstance(pricing_mode, PricingMode) else pricing_mode,
        _timestamp_for(record),
    )


def rows_for(records: Iterable[UsageRecord]) -> list[tuple[Any, ...]]:
    """Map a whole batch, or raise having mapped none of it into the database.

    Materializing and validating the batch up front is the point: it means a
    single unwritable record fails *before* a transaction is opened, so it cannot
    half-write the batch around it. A consumer that wants to salvage the rest of
    a batch containing one poison record can call :func:`row_for` per record and
    decide for itself.
    """
    return [row_for(record) for record in records]


class PostgresSource:
    """A :class:`~tokenweir.source.Source` that persists records to Postgres.

    Takes a **DB-API connection**, not a DSN: pooling, credentials, tracing
    wrappers and driver choice are the deployment's business, and taking a
    connection is what keeps this module importable with no driver installed.
    :meth:`from_dsn` is the convenience path for callers that want none of that.

    The connection must accept ``%s`` placeholders and support ``executemany``
    (psycopg 2 and 3 both do), and must **not** be in autocommit mode — this class
    commits batches itself, and autocommit would make each row durable
    independently, which is precisely the guarantee a consumer acking after the
    batch relies on not being true.

    Ownership: a connection you pass in stays yours, and :meth:`close` leaves it
    open. One opened by :meth:`from_dsn` is closed by :meth:`close`.
    """

    def __init__(self, connection: Any, *, owns_connection: bool = False) -> None:
        # An autocommit connection silently breaks the one-transaction-per-batch
        # guarantee the docstring above promises and that a consumer acking after
        # `write` depends on: each row would become durable on its own, so a
        # mid-batch failure would leave a partial batch behind and the ack would
        # be a lie. Refused rather than documented, because the symptom is
        # occasional partial data long after the decision.
        #
        # `getattr` with a default: `autocommit` is a psycopg attribute, not a
        # DB-API one, and a connection that has no such notion is not in
        # autocommit mode.
        if getattr(connection, "autocommit", False):
            raise ValueError(
                "PostgresSource needs a connection that is not in autocommit "
                "mode: it commits each batch itself, and autocommit would make "
                "rows durable one at a time, so a failure partway through would "
                "leave part of a batch written. Set connection.autocommit = False."
            )
        self._connection = connection
        self._owns_connection = owns_connection
        self._closed = False

    @classmethod
    def from_dsn(cls, dsn: str, **kwargs: Any) -> PostgresSource:
        """Open a psycopg connection and own it.

        Raises:
            ImportError: psycopg is not installed, naming the extra to install.
        """
        from tokenweir.migrations import connect

        return cls(connect(dsn, **kwargs), owns_connection=True)

    @property
    def connection(self) -> Any:
        """The underlying connection, for a caller that must reach past this."""
        return self._connection

    def write(self, records: Iterable[UsageRecord]) -> int:
        """Persist a batch in one transaction; return the number of rows written.

        Raises:
            ValueError, TypeError: a record cannot be mapped to a row. Raised
                before any statement is sent, so nothing is partially written.
                Such a record will never succeed on retry — a consumer should
                dead-letter it rather than requeue it, and distinguishing that
                from a store failure is why these are separate exception types
                from whatever the driver raises.
            Exception: whatever the driver raises on store failure. Deliberately
                not swallowed: this side is off the critical path, and a caller
                that can retry needs to know it must.
        """
        rows = rows_for(records)
        if not rows:
            # A consumer's flush timer firing with an empty buffer must not open a
            # transaction — on an idle broker that is one pointless round trip per
            # tick, forever.
            return 0

        try:
            with self._connection.cursor() as cursor:
                cursor.executemany(INSERT_SQL, rows)
            self._connection.commit()
        except Exception:
            try:
                self._connection.rollback()
            except Exception:
                # Rolling back can itself fail if the connection is gone. The
                # original failure is the one the caller needs; masking it with a
                # rollback error would send them after the wrong problem.
                pass
            raise
        return len(rows)

    def close(self) -> None:
        """Release resources. Safe to call more than once, per the protocol.

        A no-op for a borrowed connection. Closing something handed to us would be
        a surprise to whoever else holds it — including a connection pool, for
        which "closed" and "returned" are not the same thing.
        """
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()
