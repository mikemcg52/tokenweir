"""Reconciling a database somebody else migrated with the schema tokenweir owns.

ADR-0001 Pillar 5 moved ownership of ``gateway_usage`` into this package, and
:mod:`tokenweir.migrations` is how a *new* database gets that schema. This module
is for the other case, which is the one the AI Gateway is actually in: a database
that already has these tables, created by a tool that is not this one, and that
differs from what tokenweir's migrations produce.

**Why this is not migration 007.** The tempting shape is one more forward
migration. It is wrong four times over:

1. A fresh database does not need it — migrations never re-run, so a
   reconciliation would be a permanent no-op on every database that ran 001–006.
2. It cannot reach the database that does need it. The live ``schema_migrations``
   already records 1…6, adoption honours that (see
   :func:`tokenweir.migrations._ensure_state_table`), and so ``007`` would be the
   only thing that ever ran — on top of six migrations whose objects tokenweir
   never created. Reconciliation is not a step after the six; it is what makes
   the six true.
3. It would spend the destructive-migration gate on the common path. Swapping
   ``model_pricing_rates``' primary key needs ``DROP CONSTRAINT``, and
   :func:`tokenweir.migrations.destructive_statements` matches ``DROP``. Every
   routine ``apply`` everywhere would start demanding ``allow_destructive=True``,
   which is the flag that is supposed to mean "an operator reviewed *this* data
   loss".
4. It needs an argument a ``.sql`` file cannot take: the baseline date the
   existing rate card is backfilled to (see :func:`plan`).

**The shape instead: plan, then apply.** :func:`plan` is read-only and is the
default; :func:`apply` executes what it produced, in one transaction, and refuses
outright while any discrepancy needs a human. The story this implements says the
reconciliation is "required, NOT point-and-run"; this module is that sentence as
behaviour.

**Where the target comes from.** Not from a description of the expected schema
written down here — that would be a second source of truth for something
``src/tokenweir/migrations/sql/`` already defines, and it would rot the first time
somebody edited a migration. :func:`reference_snapshot` instead *builds* the
schema: it creates a scratch schema on the connection in front of it, executes
every shipped migration into it, introspects the result, and rolls the whole
transaction back. Postgres DDL is transactional, so the reference exists only
inside an aborted transaction and leaves nothing behind — the same technique
``tests/conftest.py`` uses to give each test a schema of its own.

**What it will never do.** Drop a column, drop a table, or delete a row. A column
the gateway has and tokenweir does not is the gateway's data; it is reported and
kept. The single ``DROP`` this module can emit is of the rollup *view*, which
holds no data, and even that becomes a human's decision the moment anything
depends on it.

**Transactions belong to this module**, as they do in :mod:`tokenweir.migrations`
and for the same reason: :func:`plan` rolls back when it is done (that is what
makes it read-only) and :func:`apply` commits. Do not interleave other work on the
connection you hand either of them.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Optional, Sequence, Union

from tokenweir.migrations import destructive_statements, discover
from tokenweir.postgres import USAGE_TABLE

__all__ = [
    "Column",
    "Discrepancy",
    "ManualResolutionError",
    "Observation",
    "PlanStaleError",
    "ReconcileError",
    "ReconciliationPlan",
    "Relation",
    "Resolution",
    "Snapshot",
    "apply",
    "plan",
    "reference_snapshot",
]

_logger = logging.getLogger(__name__)

#: The prefix of the transient schema :func:`reference_snapshot` builds in. Named
#: for this package so that one left behind by a crash is identifiable as ours —
#: it should never happen (the transaction is rolled back), but a stray schema
#: called ``ref_a1b2`` would be a mystery and this one is not.
REFERENCE_SCHEMA_PREFIX = "tokenweir_ref_"

#: ``gateway_usage.id`` is exempt from the column comparison, deliberately. The
#: story says so in as many words: the canonical column moved ``BIGSERIAL`` →
#: ``GENERATED ALWAYS AS IDENTITY``, and on a live table you *keep the existing
#: column* — it is "only relevant if rows are ever rebuilt, where restoring old
#: ids needs ``OVERRIDING SYSTEM VALUE`` plus a sequence reset". Converting it is
#: a full table rewrite for a property nothing reads.
#:
#: The snapshot still *records* the difference, and must: an exemption you cannot
#: see is indistinguishable from a comparison that never looked.
EXEMPT_COLUMNS = frozenset({("gateway_usage", "id")})

#: The one column for which a value for pre-existing rows is known rather than
#: invented. Rows already in the gateway's table were written under the v1 usage
#: record, so they are ``schema_version = 1``.
#:
#: Hardcoded ``1`` and **not** ``tokenweir.contract.SCHEMA_VERSION``: those two are
#: the same number today and mean different things. The constant is "what this
#: library emits now"; what belongs on a pre-existing row is "what wrote it", and
#: the day the contract goes to v2 the constant would start back-filling a lie.
BACKFILL_VALUES: dict[tuple[str, str], str] = {
    ("gateway_usage", "schema_version"): "1",
}

#: Relations are compared — and reconciled — in this order: tables before the view
#: that selects from them, because recreating the rollup before its columns exist
#: fails. Derived from the reference rather than hardcoded (see :func:`_ordered`).
_TABLE_KIND = "r"
_VIEW_KIND = "v"


class ReconcileError(RuntimeError):
    """Base class for this module's refusals. All of them are refusals to act."""


class ManualResolutionError(ReconcileError):
    """The plan contains a discrepancy only a human can resolve, so none was applied."""


class PlanStaleError(ReconcileError):
    """The database no longer matches the plan that was handed in."""


class ReferenceSchemaError(ReconcileError):
    """The reference schema could not be built, so no comparison is possible."""


class Resolution(Enum):
    """Who resolves a discrepancy.

    Two values, and the absence of a third is the point. A "probably safe" class
    would collect every case nobody wanted to think about, and the failure mode of
    a reconciler is not stopping — it is carrying on.
    """

    AUTOMATIC = "automatic"
    MANUAL = "manual"


@dataclass(frozen=True)
class Column:
    """One column, described in the terms the comparison actually needs.

    ``type`` comes from ``format_type(atttypid, atttypmod)`` rather than
    ``information_schema.columns.data_type``, which renders ``NUMERIC(18, 10)`` as
    a bare ``numeric``. A rate column reconciled to the wrong precision is a money
    bug that no test comparing ``data_type`` could see.

    ``identity`` is here for the same reason: an identity column has no
    ``column_default``, so ``BIGSERIAL`` versus ``GENERATED ALWAYS AS IDENTITY`` —
    the story's first known delta — is invisible to anything that only reads
    defaults.
    """

    name: str
    type: str
    not_null: bool
    default: Optional[str]
    #: ``''`` for an ordinary column, ``'a'`` for ALWAYS, ``'d'`` for BY DEFAULT.
    identity: str


@dataclass(frozen=True)
class Relation:
    """A table or view as the catalog has it."""

    name: str
    #: ``pg_class.relkind`` — ``'r'`` table, ``'v'`` view.
    kind: str
    columns: tuple[Column, ...]
    #: Primary-key columns in key order; empty when there is no primary key.
    primary_key: tuple[str, ...]
    #: ``(shape, definition)`` per non-constraint index — see :func:`_index_shape`.
    indexes: tuple[tuple[str, str], ...]

    def column(self, name: str) -> Optional[Column]:
        return next((c for c in self.columns if c.name == name), None)

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.columns)


@dataclass(frozen=True)
class Snapshot:
    """Every relation tokenweir owns, as found in one schema."""

    relations: tuple[Relation, ...]

    def get(self, name: str) -> Optional[Relation]:
        return next((r for r in self.relations if r.name == name), None)


@dataclass(frozen=True)
class Discrepancy:
    """One difference that needs resolving, and how.

    ``statements`` is empty for a :attr:`Resolution.MANUAL` discrepancy — not
    because the SQL is unknown but because writing it out would invite somebody to
    run it, and the whole point of the classification is that a person has to
    decide first. ``remedy`` says what the decision is.
    """

    relation: str
    description: str
    resolution: Resolution
    statements: tuple[str, ...] = ()
    remedy: str = ""

    @property
    def is_manual(self) -> bool:
        return self.resolution is Resolution.MANUAL


@dataclass(frozen=True)
class Observation:
    """Something true of this database that needs no action, and must be said anyway.

    An extra column the gateway owns is the case this exists for. It is reported
    because a reader deciding whether to trust the reconciliation needs to know it
    is there, and it is *not* a :class:`Discrepancy` because it will still be there
    afterwards — a reconciled database would otherwise never produce an empty plan,
    and "run it again and it does nothing" is the property that makes this safe to
    put in a deploy script.
    """

    relation: str
    description: str


@dataclass(frozen=True)
class ReconciliationPlan:
    """What is wrong with this database, in the order it would be put right."""

    discrepancies: tuple[Discrepancy, ...] = ()
    observations: tuple[Observation, ...] = ()
    #: The baseline the rate-card restructure was planned with, carried so
    #: :func:`apply` re-derives the same plan rather than a different one.
    baseline_effective_from: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        """No discrepancies. Observations do not count — see :class:`Observation`."""
        return not self.discrepancies

    @property
    def manual(self) -> tuple[Discrepancy, ...]:
        return tuple(d for d in self.discrepancies if d.is_manual)

    @property
    def automatic(self) -> tuple[Discrepancy, ...]:
        return tuple(d for d in self.discrepancies if not d.is_manual)

    @property
    def statements(self) -> tuple[str, ...]:
        """Every statement, in execution order."""
        return tuple(s for d in self.discrepancies for s in d.statements)

    @property
    def signature(self) -> tuple[Any, ...]:
        """What :func:`apply` compares to decide the plan still describes the database.

        Statements and classifications, not observations: an observation changing
        (somebody added a column elsewhere) is not a reason to refuse a
        reconciliation that is still correct.
        """
        return tuple(
            (d.relation, d.description, d.resolution.value, d.statements)
            for d in self.discrepancies
        )

    def render(self) -> str:
        """The plan as an operator reads it."""
        lines: list[str] = []
        if self.is_empty:
            lines.append(
                "No discrepancies: this database already matches the schema tokenweir owns."
            )
        else:
            manual, automatic = self.manual, self.automatic
            lines.append(
                f"{len(self.discrepancies)} discrepanc"
                f"{'y' if len(self.discrepancies) == 1 else 'ies'}: "
                f"{len(automatic)} tokenweir can resolve, {len(manual)} need a decision."
            )
            for discrepancy in self.discrepancies:
                lines.append("")
                marker = "MANUAL   " if discrepancy.is_manual else "AUTOMATIC"
                lines.append(f"  [{marker}] {discrepancy.relation}: {discrepancy.description}")
                if discrepancy.remedy:
                    lines.append(f"             -> {discrepancy.remedy}")
                for statement in discrepancy.statements:
                    lines.append(f"             {_one_line(statement)}")
                    # Named where it is read, not only in the docs. This is the one
                    # place a reconciliation can destroy anything, and an operator
                    # deciding whether to pass --apply should not have to notice a
                    # DROP by reading SQL carefully.
                    if destructive_statements(statement):
                        lines.append(
                            "             ^ drops something (a default, a constraint or "
                            "the rollup view) — read it before you run it. This module "
                            "never drops a column, a table or a row."
                        )
        for observation in self.observations:
            lines.append("")
            lines.append(f"  [note]      {observation.relation}: {observation.description}")
        return "\n".join(lines)


def _one_line(statement: str, limit: int = 160) -> str:
    """A statement squashed onto one line for the rendered plan.

    Whole-line ``--`` comments go first. Migration 005 is a hundred lines of SQL
    behind forty of prose explaining every decision in it; squashed as-is, the
    first 160 characters an operator sees are the prose, and the statement they
    are being asked to approve is not among them.

    Display only, and approximate on purpose: a ``--`` inside a string literal is
    not a comment, and this does not try to know that. Being wrong here shortens a
    rendered line. :func:`tokenweir.migrations.destructive_statements`, which has
    to be right about the same question, does the real scan.
    """
    body = " ".join(
        line for line in statement.splitlines() if not line.strip().startswith("--")
    )
    squashed = " ".join(body.split())
    return squashed if len(squashed) <= limit else squashed[: limit - 1] + "…"


# --- Introspection ------------------------------------------------------------


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


_INDEX_SHAPE_RE = re.compile(
    r"^CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+.*?\s+ON\s+\S+\s+(?P<body>USING\s+.*)$",
    re.IGNORECASE | re.DOTALL,
)


def _index_shape(indexdef: str) -> str:
    """An index reduced to what it *does*, with its name and table dropped.

    Indexes are compared by shape rather than by name because the gateway named
    its own. A legacy ``idx_gateway_usage_ts`` over exactly the columns
    ``gateway_usage_ts_idx`` covers is the same index, and creating a second one
    beside it would add write cost and serve nothing.

    Falls back to the whole normalised definition if the pattern does not match,
    which errs towards reporting a difference that is not one — the safe
    direction, since the resolution is only ever to *create* an index.
    """
    squashed = " ".join(indexdef.split())
    match = _INDEX_SHAPE_RE.match(squashed)
    if not match:
        return squashed
    unique = "UNIQUE " if match.group("unique") else ""
    return f"{unique}{match.group('body')}"


def _strip_schema(sql: str, schema: str) -> str:
    """Remove a schema qualifier so two schemas' definitions compare equal."""
    return sql.replace(f"{_quote(schema)}.", "").replace(f"{schema}.", "")


def _relation_oid(cursor: Any, schema: Optional[str], name: str) -> Optional[int]:
    """The relation's oid, or None. ``schema=None`` resolves through ``search_path``."""
    qualified = name if schema is None else f"{_quote(schema)}.{_quote(name)}"
    cursor.execute("SELECT to_regclass(%s)::oid", (qualified,))
    row = cursor.fetchone()
    return None if row is None or row[0] is None else int(row[0])


def _snapshot(connection: Any, names: Sequence[str], schema: Optional[str] = None) -> Snapshot:
    """Describe ``names`` as the catalog has them.

    One function for both sides of the comparison, which is what makes the
    comparison mean anything: the reference and the live database are described by
    the same code, so a difference in the output is a difference in the database
    and not a difference in how it was read.
    """
    relations: list[Relation] = []
    with connection.cursor() as cursor:
        for name in names:
            oid = _relation_oid(cursor, schema, name)
            if oid is None:
                continue

            cursor.execute(
                "SELECT c.relkind, n.nspname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = %s",
                (oid,),
            )
            kind, namespace = cursor.fetchone()

            cursor.execute(
                "SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull, "
                "       pg_get_expr(d.adbin, d.adrelid), a.attidentity "
                "FROM pg_attribute a "
                "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
                "WHERE a.attrelid = %s AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum",
                (oid,),
            )
            columns = tuple(
                Column(name=r[0], type=r[1], not_null=bool(r[2]), default=r[3], identity=r[4] or "")
                for r in cursor.fetchall()
            )

            cursor.execute(
                "SELECT a.attname "
                "FROM pg_constraint c "
                "JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE "
                "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum "
                "WHERE c.conrelid = %s AND c.contype = 'p' "
                "ORDER BY k.ord",
                (oid,),
            )
            primary_key = tuple(r[0] for r in cursor.fetchall())

            # Constraint-backed indexes are excluded: the primary key is compared
            # as a primary key just above, and counting its implicit index too
            # would report one disagreement twice — once as a key and once as a
            # missing index whose "resolution" is a CREATE INDEX that cannot
            # produce a constraint anyway.
            cursor.execute(
                "SELECT pg_get_indexdef(i.indexrelid) "
                "FROM pg_index i WHERE i.indrelid = %s AND NOT i.indisprimary "
                "  AND NOT EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid = i.indexrelid)",
                (oid,),
            )
            indexes = tuple(
                sorted(
                    (_index_shape(_strip_schema(r[0], namespace)),
                     _strip_schema(r[0], namespace))
                    for r in cursor.fetchall()
                )
            )

            relations.append(
                Relation(
                    name=name,
                    kind=kind,
                    columns=columns,
                    primary_key=primary_key,
                    indexes=indexes,
                )
            )
    return Snapshot(relations=tuple(relations))


def _owned_relation_names() -> tuple[str, ...]:
    """Every relation the shipped migrations create, found by reading them.

    Parsed out of the SQL rather than listed here, so a migration that adds a
    table is compared without anybody remembering to update a constant. The
    patterns are deliberately narrow: this is a list of names, and a name it
    misses is a relation the reconciler silently would not compare — which
    :func:`_view_migration_sql` turns into a loud failure for the one relation
    where silence would matter most.
    """
    names: list[str] = []
    pattern = re.compile(
        r"CREATE\s+(?:TABLE|(?:OR\s+REPLACE\s+)?VIEW)\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z0-9_]*)",
        re.IGNORECASE,
    )
    for migration in discover():
        for name in pattern.findall(migration.sql):
            if name not in names:
                names.append(name)
    return tuple(names)


def _view_migration_sql(view: str) -> str:
    """The shipped SQL that creates ``view``, for recreating it.

    Located by searching the migrations rather than by filename, and a miss is an
    exception rather than a skipped comparison: a renamed migration must break
    this loudly, because the alternative is a reconciler that quietly stops
    checking the rollup.
    """
    pattern = re.compile(
        rf"CREATE\s+(?:OR\s+REPLACE\s+)?VIEW\s+{re.escape(view)}\b", re.IGNORECASE
    )
    for migration in discover():
        if pattern.search(migration.sql):
            return migration.sql
    raise ReferenceSchemaError(
        f"no shipped migration creates the view {view!r}; tokenweir cannot recreate "
        "it, and reconciling a database without being able to is not something this "
        "module will guess at"
    )


def _require_transactional(connection: Any, what: str) -> None:
    if getattr(connection, "autocommit", False):
        raise ValueError(
            f"tokenweir.reconcile.{what} needs a connection that is not in autocommit "
            "mode: the reference schema is built and then rolled back, and under "
            "autocommit that rollback does nothing — the scratch schema would be left "
            "behind and every statement would commit as it ran. Set "
            "connection.autocommit = False."
        )


def reference_snapshot(connection: Any) -> Snapshot:
    """The schema tokenweir's migrations produce, built and then rolled back.

    Creates a uniquely-named scratch schema, executes every shipped migration into
    it, describes the result with the same :func:`_snapshot` used on the live
    database, and rolls the transaction back. Postgres DDL is transactional, so
    nothing survives — verified, not assumed.

    The grant blocks in migrations 004 and 005 no-op here, because
    ``tokenweir.reader_role`` is never set. That is what we want: the reference is
    a statement about shape, and permissions on a schema that is about to cease to
    exist are not a useful thing to compare against.

    Raises:
        ValueError: the connection is in autocommit mode.
        ReferenceSchemaError: the reference could not be built — most often no
            ``CREATE`` privilege on the database. Reported rather than swallowed,
            because the one answer this module must never give is "no differences"
            for any reason other than having found none.
    """
    _require_transactional(connection, "reference_snapshot")

    schema = f"{REFERENCE_SCHEMA_PREFIX}{uuid.uuid4().hex[:12]}"
    names = _owned_relation_names()
    try:
        with connection.cursor() as cursor:
            cursor.execute(f"CREATE SCHEMA {_quote(schema)}")
            # SET LOCAL, so it dies with this transaction and cannot leak into the
            # caller's session even if something below raises.
            cursor.execute(f"SET LOCAL search_path TO {_quote(schema)}")
            for migration in discover():
                cursor.execute(migration.sql)
        snapshot = _snapshot(connection, names, schema=schema)
    except Exception as exc:
        connection.rollback()
        raise ReferenceSchemaError(
            "could not build tokenweir's reference schema on this connection, so "
            f"there is nothing to compare the database against: {exc}. Reconciling "
            "needs permission to CREATE SCHEMA (the schema is created inside a "
            "transaction and rolled back, so nothing is left behind)."
        ) from exc

    connection.rollback()
    return snapshot


# --- The diff -----------------------------------------------------------------


def _ordered(snapshot: Snapshot) -> tuple[Relation, ...]:
    """Tables first, then views, each alphabetically.

    Execution order, not presentation order: the rollup selects from both tables,
    so recreating it before their columns exist fails. Derived from ``relkind``
    rather than hardcoded, so a seventh migration adding a table needs nothing
    here.
    """
    tables = sorted((r for r in snapshot.relations if r.kind == _TABLE_KIND), key=lambda r: r.name)
    views = sorted((r for r in snapshot.relations if r.kind != _TABLE_KIND), key=lambda r: r.name)
    return tuple(tables) + tuple(views)


def _normalise_baseline(value: Union[str, date, None]) -> Optional[str]:
    """Validate the operator's baseline date, or refuse it.

    ``-infinity`` is accepted and is the semantically honest choice for a
    current-valued rate card that was simply always the rate — but it is not the
    default, because "always" is itself a claim about history and the operator is
    the one who knows whether it is true.
    """
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = value.strip()
    if text.lower() in {"-infinity", "infinity"}:
        return text.lower()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise ValueError(
            f"baseline_effective_from must be an ISO date (YYYY-MM-DD) or '-infinity'; "
            f"got {value!r}"
        ) from exc


def _add_column_statements(relation: str, column: Column) -> Optional[tuple[str, ...]]:
    """How to add ``column`` so the result is the column the migration creates.

    ``None`` when there is no way to do it honestly: a ``NOT NULL`` column with no
    default and no known back-fill value cannot be added to a table with rows in
    it without inventing a value for every one of them.
    """
    table, name = _quote(relation), _quote(column.name)
    if column.default is not None:
        # The migration's own default back-fills existing rows, and leaving it in
        # place is correct: it is what the migration would have produced.
        null = " NOT NULL" if column.not_null else ""
        return (f"ALTER TABLE {table} ADD COLUMN {name} {column.type} "
                f"DEFAULT {column.default}{null}",)
    if not column.not_null:
        return (f"ALTER TABLE {table} ADD COLUMN {name} {column.type}",)

    backfill = BACKFILL_VALUES.get((relation, column.name))
    if backfill is None:
        return None
    # DEFAULT back-fills every existing row, then the default goes, because the
    # migration's column has none. Two statements, and the second is not optional:
    # a leftover default would silently supply a value for any future writer that
    # forgot the column, which is the opposite of what NOT NULL is for.
    return (
        f"ALTER TABLE {table} ADD COLUMN {name} {column.type} "
        f"NOT NULL DEFAULT {backfill}",
        f"ALTER TABLE {table} ALTER COLUMN {name} DROP DEFAULT",
    )


def _column_has_nulls(connection: Any, relation: str, column: str) -> bool:
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT EXISTS (SELECT 1 FROM {_quote(relation)} "
            f"WHERE {_quote(column)} IS NULL)"
        )
        return bool(cursor.fetchone()[0])


def _rate_card_duplicates(connection: Any, relation: str) -> tuple[str, ...]:
    """Models with more than one row in a current-valued rate card.

    The old key is ``(model, pricing_mode)``; the new one is
    ``(model, effective_from)`` and tokenweir's rate card has no ``pricing_mode``
    at all. Two rows for one model therefore collapse onto the same new key, and
    one of them has nowhere to go. Which one survives is a decision about money,
    so the tool does not make it.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            f"SELECT model FROM {_quote(relation)} GROUP BY model "
            "HAVING COUNT(*) > 1 ORDER BY model"
        )
        return tuple(r[0] for r in cursor.fetchall())


def _primary_key_constraint_name(connection: Any, relation: str) -> Optional[str]:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.conname FROM pg_constraint c "
            "WHERE c.conrelid = to_regclass(%s) AND c.contype = 'p'",
            (relation,),
        )
        row = cursor.fetchone()
        return None if row is None else row[0]


def _view_dependents(connection: Any, view: str) -> tuple[str, ...]:
    """Relations whose definition selects from ``view``.

    Dropping a view that something else is built on takes the dependent with it
    under ``CASCADE`` and fails without it. Neither is this module's call to make,
    so a dependent turns the replacement into a decision.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT DISTINCT dependent.relname "
            "FROM pg_depend d "
            "JOIN pg_rewrite r ON r.oid = d.objid "
            "JOIN pg_class dependent ON dependent.oid = r.ev_class "
            "WHERE d.refobjid = to_regclass(%s) "
            "  AND d.classid = 'pg_rewrite'::regclass "
            "  AND dependent.oid <> to_regclass(%s) "
            "ORDER BY 1",
            (view, view),
        )
        return tuple(r[0] for r in cursor.fetchall())


def _rate_card_discrepancy(
    connection: Any,
    relation: str,
    reference: Relation,
    live: Relation,
    baseline: Optional[str],
) -> Discrepancy:
    """The current-valued → effective-dated restructure, or the reason it cannot happen.

    This is not a missing column with a primary key beside it; it is one change
    with three parts, and splitting it across a generic column diff and a generic
    key diff would let a plan be half-applied in a way that leaves the table with
    neither key working.
    """
    duplicates = _rate_card_duplicates(connection, relation)
    if duplicates:
        listed = ", ".join(duplicates)
        return Discrepancy(
            relation=relation,
            description=(
                "rate card is current-valued and cannot be re-keyed automatically: "
                f"{len(duplicates)} model(s) have more than one row ({listed})"
            ),
            resolution=Resolution.MANUAL,
            remedy=(
                "tokenweir's rate card is keyed (model, effective_from) and has no "
                "pricing_mode column, so these rows would collide on one key. Decide "
                "which rate each model carries — or give them different effective_from "
                "dates by hand — before reconciling. Nothing here can choose for you: "
                "the answer changes what every historical row costs."
            ),
        )

    if baseline is None:
        return Discrepancy(
            relation=relation,
            description=(
                "rate card is current-valued (no effective_from) and must be "
                "restructured to the effective-dated key (model, effective_from)"
            ),
            resolution=Resolution.MANUAL,
            remedy=(
                "re-run with a baseline date (--baseline-effective-from). It decides "
                "which historical usage the existing rates are taken to have covered, "
                "and the rollup prices every day on or after it at these rates. "
                "'-infinity' is accepted and says 'these were always the rates'; there "
                "is no default, because both answers are claims about history."
            ),
        )

    column = _quote("effective_from")
    table = _quote(relation)
    statements = [
        # DEFAULT back-fills every existing row to the operator's baseline in one
        # statement; NOT NULL is safe in the same breath because of it.
        f"ALTER TABLE {table} ADD COLUMN {column} date NOT NULL DEFAULT DATE '{baseline}'",
        f"ALTER TABLE {table} ALTER COLUMN {column} DROP DEFAULT",
    ]
    existing_key = _primary_key_constraint_name(connection, relation)
    if existing_key is not None:
        statements.append(f"ALTER TABLE {table} DROP CONSTRAINT {_quote(existing_key)}")
    key = ", ".join(_quote(c) for c in reference.primary_key)
    statements.append(f"ALTER TABLE {table} ADD PRIMARY KEY ({key})")
    return Discrepancy(
        relation=relation,
        description=(
            "rate card is current-valued; adding effective_from (back-filled to "
            f"{baseline}) and re-keying to (" + ", ".join(reference.primary_key) + ")"
        ),
        resolution=Resolution.AUTOMATIC,
        statements=tuple(statements),
    )


def _compare_columns(
    connection: Any,
    relation: str,
    reference: Relation,
    live: Relation,
    skip: frozenset,
) -> tuple[list[Discrepancy], list[Observation]]:
    discrepancies: list[Discrepancy] = []
    observations: list[Observation] = []
    table = _quote(relation)

    for column in reference.columns:
        if column.name in skip or (relation, column.name) in EXEMPT_COLUMNS:
            continue
        found = live.column(column.name)
        if found is None:
            statements = _add_column_statements(relation, column)
            if statements is None:
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=(
                            f"column {column.name} ({column.type}) is missing, and is "
                            "NOT NULL with no default"
                        ),
                        resolution=Resolution.MANUAL,
                        remedy=(
                            "existing rows have no value for it and tokenweir will not "
                            "invent one. Add the column yourself with a back-fill you "
                            "can defend, then reconcile again."
                        ),
                    )
                )
            else:
                note = ""
                if (relation, column.name) in BACKFILL_VALUES:
                    note = (
                        f"; existing rows back-filled to "
                        f"{BACKFILL_VALUES[(relation, column.name)]}"
                    )
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=f"column {column.name} ({column.type}) is missing{note}",
                        resolution=Resolution.AUTOMATIC,
                        statements=statements,
                    )
                )
            continue

        if found.type != column.type:
            discrepancies.append(
                Discrepancy(
                    relation=relation,
                    description=(
                        f"column {column.name} is {found.type}, tokenweir's is {column.type}"
                    ),
                    resolution=Resolution.MANUAL,
                    remedy=(
                        "a widening may be safe and a narrowing truncates; which this is "
                        "depends on the data, so it is not converted automatically. "
                        "ALTER … TYPE it yourself once you have checked."
                    ),
                )
            )
            continue

        if column.not_null and not found.not_null:
            if _column_has_nulls(connection, relation, column.name):
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=(
                            f"column {column.name} is nullable and holds NULLs; "
                            "tokenweir's is NOT NULL"
                        ),
                        resolution=Resolution.MANUAL,
                        remedy=(
                            "decide what those rows should say and update them, then "
                            "reconcile again. Choosing a value for existing rows is not "
                            "this tool's to do."
                        ),
                    )
                )
            else:
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=(
                            f"column {column.name} is nullable; tokenweir's is NOT NULL "
                            "(no NULLs present)"
                        ),
                        resolution=Resolution.AUTOMATIC,
                        statements=(
                            f"ALTER TABLE {table} ALTER COLUMN {_quote(column.name)} SET NOT NULL",
                        ),
                    )
                )
        elif found.not_null and not column.not_null:
            discrepancies.append(
                Discrepancy(
                    relation=relation,
                    description=(
                        f"column {column.name} is NOT NULL; tokenweir's is nullable, and "
                        "the writer sends NULL for it"
                    ),
                    resolution=Resolution.MANUAL,
                    remedy=(
                        "relaxing a constraint the gateway chose is not this tool's "
                        "decision, but leaving it means PostgresSource rejects any record "
                        "that omits the field. DROP NOT NULL yourself, or give the column "
                        "a default."
                    ),
                )
            )

        if found.default != column.default:
            if column.default is None:
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=(
                            f"column {column.name} has a default "
                            f"({_one_line(found.default or '', 40)}); tokenweir's has none"
                        ),
                        resolution=Resolution.AUTOMATIC,
                        statements=(
                            f"ALTER TABLE {table} ALTER COLUMN {_quote(column.name)} DROP DEFAULT",
                        ),
                    )
                )
            else:
                discrepancies.append(
                    Discrepancy(
                        relation=relation,
                        description=(
                            f"column {column.name} defaults to "
                            f"{_one_line(found.default or 'nothing', 40)}; tokenweir's "
                            f"defaults to {_one_line(column.default, 40)}"
                        ),
                        resolution=Resolution.AUTOMATIC,
                        statements=(
                            f"ALTER TABLE {table} ALTER COLUMN {_quote(column.name)} "
                            f"SET DEFAULT {column.default}",
                        ),
                    )
                )

    reference_names = set(reference.column_names)
    for column in live.columns:
        if column.name in reference_names or (relation, column.name) in EXEMPT_COLUMNS:
            continue
        # Never proposed for removal, whatever else is true of it. It is the
        # gateway's data and this module does not delete data.
        #
        # Blocking only on the table tokenweir actually inserts into, and the rule
        # is tied to `USAGE_TABLE` rather than restated, so it follows the writer
        # if the writer ever moves. The reason it blocks is specific — `COLUMNS` in
        # `tokenweir.postgres` does not name this column, so every INSERT the
        # writer makes fails on it — and a rule stated more broadly than its reason
        # would refuse the ordinary case: the live rate card's `pricing_mode` is
        # NOT NULL (it was half the old primary key) and nothing in tokenweir
        # writes to that table at all.
        unwritable = column.not_null and column.default is None and not column.identity
        if unwritable and relation == USAGE_TABLE:
            discrepancies.append(
                Discrepancy(
                    relation=relation,
                    description=(
                        f"column {column.name} ({column.type}) is not part of tokenweir's "
                        "schema and is NOT NULL with no default"
                    ),
                    resolution=Resolution.MANUAL,
                    remedy=(
                        "PostgresSource's INSERT does not name this column, so every batch "
                        "it writes would fail on it. Give it a default, make it nullable, "
                        "or keep writing this table with something that supplies it. "
                        "tokenweir will not drop it — it is your data."
                    ),
                )
            )
        elif unwritable:
            observations.append(
                Observation(
                    relation=relation,
                    description=(
                        f"column {column.name} ({column.type}) is not part of tokenweir's "
                        "schema, is NOT NULL with no default, and is kept. Nothing in "
                        "tokenweir writes to this table, so it blocks nothing here — but "
                        "any INSERT of your own must still supply it"
                    ),
                )
            )
        else:
            observations.append(
                Observation(
                    relation=relation,
                    description=(
                        f"column {column.name} ({column.type}) is not part of tokenweir's "
                        "schema; it is kept and left alone, and tokenweir's writer will "
                        "not populate it"
                    ),
                )
            )
    return discrepancies, observations


def _compare_indexes(relation: str, reference: Relation, live: Relation) -> tuple[
    list[Discrepancy], list[Observation]
]:
    discrepancies: list[Discrepancy] = []
    observations: list[Observation] = []
    live_shapes = {shape for shape, _ in live.indexes}
    reference_shapes = {shape for shape, _ in reference.indexes}
    for shape, definition in reference.indexes:
        if shape in live_shapes:
            continue
        discrepancies.append(
            Discrepancy(
                relation=relation,
                description=f"index missing: {_one_line(shape, 80)}",
                resolution=Resolution.AUTOMATIC,
                statements=(definition,),
            )
        )
    for shape, definition in live.indexes:
        if shape not in reference_shapes:
            observations.append(
                Observation(
                    relation=relation,
                    description=(
                        f"index is not part of tokenweir's schema and is left in place: "
                        f"{_one_line(definition, 100)}"
                    ),
                )
            )
    return discrepancies, observations


def _compare_view(
    connection: Any, relation: str, reference: Relation, live: Optional[Relation]
) -> list[Discrepancy]:
    sql = _view_migration_sql(relation)
    if live is None:
        return [
            Discrepancy(
                relation=relation,
                description="rollup view is absent",
                resolution=Resolution.AUTOMATIC,
                statements=(sql,),
            )
        ]

    if live.kind != reference.kind:
        return [
            Discrepancy(
                relation=relation,
                description=(
                    f"{relation} exists but is not a view (relkind {live.kind!r}); "
                    f"tokenweir's is a view"
                ),
                resolution=Resolution.MANUAL,
                remedy=(
                    "a table with this name may hold rows, and replacing it would "
                    "destroy them. Move it aside yourself, then reconcile again."
                ),
            )
        ]

    # Column set *and order*, not the definition text: two SQL strings differing
    # only in whitespace build the same view, and comparing text would cry wolf on
    # every reformat. Order matters because a view's columns are positional.
    if live.column_names == reference.column_names:
        return []

    dependents = _view_dependents(connection, relation)
    if dependents:
        return [
            Discrepancy(
                relation=relation,
                description=(
                    f"rollup view has tokenweir's columns wrong and "
                    f"{len(dependents)} object(s) depend on it ({', '.join(dependents)})"
                ),
                resolution=Resolution.MANUAL,
                remedy=(
                    "the view has to be dropped and recreated — CREATE OR REPLACE cannot "
                    "change a view's column set — and dropping it would take the "
                    "dependents with it. Recreate them against the new shape yourself. "
                    "Note that tokenweir's rollup groups by pricing_mode, so queries "
                    "against it return more rows than the gateway's did."
                ),
            )
        ]

    return [
        Discrepancy(
            relation=relation,
            description=(
                "rollup view differs from tokenweir's: columns are "
                f"({', '.join(live.column_names)}), tokenweir's are "
                f"({', '.join(reference.column_names)})"
            ),
            resolution=Resolution.AUTOMATIC,
            statements=(f"DROP VIEW {_quote(relation)}", sql),
        )
    ]


def plan(
    connection: Any,
    *,
    baseline_effective_from: Union[str, date, None] = None,
) -> ReconciliationPlan:
    """Describe every way this database differs from the schema tokenweir owns.

    Read-only: the reference schema is rolled back, every other statement is a
    ``SELECT``, and the connection is rolled back before returning. Nothing here
    commits.

    The comparison is against the database's **actual catalog contents**, never
    against ``schema_migrations``. That is the whole point — the failure this
    module exists for is a database whose recorded state and real state disagree,
    and asking the record would reproduce it.

    Args:
        baseline_effective_from: the date existing ``model_pricing_rates`` rows are
            taken to have been in force from, when that table has to be
            restructured from current-valued to effective-dated. Without it the
            restructure is reported and refused rather than guessed at — the date
            decides what every historical row costs. An ISO date, a
            :class:`datetime.date`, or ``'-infinity'``.

    Raises:
        ValueError: the connection is in autocommit mode, or the baseline is not a
            date.
        ReferenceSchemaError: tokenweir's schema could not be built for comparison.
    """
    _require_transactional(connection, "plan")
    baseline = _normalise_baseline(baseline_effective_from)

    reference = reference_snapshot(connection)
    names = tuple(r.name for r in _ordered(reference))
    live = _snapshot(connection, names)

    discrepancies: list[Discrepancy] = []
    observations: list[Observation] = []

    for reference_relation in _ordered(reference):
        name = reference_relation.name
        live_relation = live.get(name)

        if reference_relation.kind != _TABLE_KIND:
            discrepancies.extend(
                _compare_view(connection, name, reference_relation, live_relation)
            )
            continue

        if live_relation is None:
            # Not a reconciliation. `apply` creates missing objects and is the
            # right command; saying so beats emitting a CREATE TABLE from here and
            # leaving `schema_migrations` describing a database that no longer
            # exists.
            discrepancies.append(
                Discrepancy(
                    relation=name,
                    description="table is absent from this database",
                    resolution=Resolution.MANUAL,
                    remedy=(
                        "there is nothing here to reconcile. `python -m "
                        "tokenweir.migrations apply` creates tokenweir's schema; "
                        "reconcile only reshapes objects that already exist."
                    ),
                )
            )
            continue

        skip: frozenset = frozenset()
        if name == "model_pricing_rates" and live_relation.column("effective_from") is None:
            rate_card = _rate_card_discrepancy(
                connection, name, reference_relation, live_relation, baseline
            )
            discrepancies.append(rate_card)
            # The restructure owns effective_from and the primary key; letting the
            # generic diff report them again would offer a second, conflicting
            # resolution for the same change.
            skip = frozenset({"effective_from"})

        column_discrepancies, column_observations = _compare_columns(
            connection, name, reference_relation, live_relation, skip
        )
        discrepancies.extend(column_discrepancies)
        observations.extend(column_observations)

        if not skip and live_relation.primary_key != reference_relation.primary_key:
            discrepancies.append(
                Discrepancy(
                    relation=name,
                    description=(
                        f"primary key is ({', '.join(live_relation.primary_key) or 'none'}); "
                        f"tokenweir's is ({', '.join(reference_relation.primary_key)})"
                    ),
                    resolution=Resolution.MANUAL,
                    remedy=(
                        "re-keying a table decides which rows are duplicates of which. "
                        "Check the data, then change the key yourself."
                    ),
                )
            )

        index_discrepancies, index_observations = _compare_indexes(
            name, reference_relation, live_relation
        )
        discrepancies.extend(index_discrepancies)
        observations.extend(index_observations)

    connection.rollback()
    return ReconciliationPlan(
        discrepancies=tuple(discrepancies),
        observations=tuple(observations),
        baseline_effective_from=baseline,
    )


def apply(
    connection: Any,
    existing_plan: Optional[ReconciliationPlan] = None,
    *,
    baseline_effective_from: Union[str, date, None] = None,
) -> ReconciliationPlan:
    """Execute a plan, all of it, in one transaction. Returns what was applied.

    Args:
        existing_plan: the plan a human read. It is **re-derived** against the live
            connection and refused if it no longer matches — a plan read ten
            minutes ago is not evidence about the database now. Omit it to plan and
            apply in one step.
        baseline_effective_from: see :func:`plan`. When ``existing_plan`` is given
            it already carries the baseline it was planned with, and passing a
            different one here is a contradiction rather than an override — the
            plan the human read would not be the plan that ran — so it is refused.

    Raises:
        ManualResolutionError: the plan contains a discrepancy only a human can
            resolve. **Nothing is applied**, including the parts that could have
            been: a half-reconciled database that reports itself done is the exact
            state this module exists to prevent.
        PlanStaleError: the database changed since ``existing_plan`` was made.
        ValueError, ReferenceSchemaError: see :func:`plan`.
    """
    _require_transactional(connection, "apply")

    baseline = _normalise_baseline(baseline_effective_from)
    if existing_plan is not None:
        if baseline is not None and baseline != existing_plan.baseline_effective_from:
            raise ValueError(
                "baseline_effective_from was given to apply() and disagrees with the "
                f"plan's own ({baseline!r} vs "
                f"{existing_plan.baseline_effective_from!r}). Re-plan with the baseline "
                "you mean and read the result; applying a plan nobody read under a "
                "different date is how a rate card ends up restating history quietly."
            )
        baseline = existing_plan.baseline_effective_from
    current = plan(connection, baseline_effective_from=baseline)

    if existing_plan is not None and existing_plan.signature != current.signature:
        raise PlanStaleError(
            "the database no longer matches the plan it was given — something changed "
            "it in between. Nothing was applied; re-plan and read it again before "
            "applying."
        )

    if current.manual:
        detail = "; ".join(f"{d.relation}: {d.description}" for d in current.manual)
        one = len(current.manual) == 1
        raise ManualResolutionError(
            f"refusing to reconcile: {len(current.manual)} "
            f"{'discrepancy needs' if one else 'discrepancies need'} a decision that is "
            f"not tokenweir's to make — {detail}. Nothing was applied, including the "
            f"{len(current.automatic)} that could have been: a partly reconciled "
            "database reports itself done and is not."
        )

    if current.is_empty:
        return current

    try:
        with connection.cursor() as cursor:
            for statement in current.statements:
                cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    for discrepancy in current.discrepancies:
        _logger.info("tokenweir: reconciled %s — %s", discrepancy.relation, discrepancy.description)
    return current
