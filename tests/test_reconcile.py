"""Reconciling a gateway-shaped database with tokenweir's schema (TOKWEIR-10).

Against a **real Postgres**, like every other correctness test in this project and
for the same reason: what is asserted here is what a server does with DDL against
a table that has rows in it, and a mock of a database only proves the code calls
the mock. These skip with a message naming `TOKENWEIR_TEST_DSN` when no server is
available — see `tests/conftest.py`, and note that `pip install -e '.[dev]'` is
enough, because `pgserver` ships the binaries.

**`LEGACY_SQL` below is a reconstruction, and that is the one thing to know about
this file.** The story says the authoritative comparison is against a dump of the
live `ai_gateway_metrics`, and there is no such dump reachable from the pod this
was written in — the same environment fact the TOKWEIR-5 hand-off recorded, which
is why tokenweir's own migrations 001-006 are a reconstruction too. What is here
is built from the story's three known deltas and that comment, in **one place**,
so that whoever finally has the dump has exactly one thing to correct.

The design is what narrows the gap rather than the fixture: `tokenweir.reconcile`
introspects the database in front of it and refuses what it does not recognise, so
being wrong about the legacy shape produces a refusal on the real database rather
than a wrong reconciliation. Several tests below deliberately deform `LEGACY_SQL`
in ways nobody has claimed the live database has — an extra column, a duplicated
rate, a dependent view — because the shapes the reconciler has never seen are
exactly the ones worth knowing it will not damage.
"""

import datetime
import uuid

import pytest

from tokenweir import UsageRecord
from tokenweir.migrations import apply as apply_migrations
from tokenweir.postgres import PostgresSource
from tokenweir.reconcile import (
    REFERENCE_SCHEMA_PREFIX,
    ManualResolutionError,
    PlanStaleError,
    ReferenceSchemaError,
    Resolution,
    _index_name,
    _normalise_baseline,
    _snapshot,
    _view_migration_sql,
    apply,
    plan,
    reference_snapshot,
)

#: The AI Gateway's `ai_gateway_metrics`, as the story and the TOKWEIR-5 hand-off
#: describe it. A **reconstruction** — see the module docstring. The three known
#: deltas it encodes, and which each line is here for:
#:
#: 1. `gateway_usage` has **no** `schema_version`, and `id` is `BIGSERIAL` rather
#:    than `GENERATED ALWAYS AS IDENTITY`.
#: 2. `model_pricing_rates` is current-valued: keyed `(model, pricing_mode)`, with
#:    no `effective_from`.
#: 3. `schema_migrations` has no `checksum` column and already records 1-6, which
#:    is what makes `migrations.apply` a silent no-op against this database.
#:
#: It also has the gateway's own rollup view (narrower than tokenweir's, and
#: without `pricing_mode` in the GROUP BY, which is hand-off note 2), and its own
#: index names, which is what makes "indexes are compared by shape, not by name"
#: testable rather than theoretical.
LEGACY_SQL = """
CREATE TABLE gateway_usage (
    id                          BIGSERIAL PRIMARY KEY,
    request_id                  TEXT        NOT NULL,
    app_id                      TEXT        NOT NULL,
    endpoint                    TEXT        NOT NULL,
    model                       TEXT        NOT NULL,
    status                      TEXT        NOT NULL,
    workload                    TEXT,
    queue                       TEXT,
    input_tokens                BIGINT      NOT NULL DEFAULT 0,
    output_tokens               BIGINT      NOT NULL DEFAULT 0,
    cache_creation_input_tokens BIGINT      NOT NULL DEFAULT 0,
    cache_read_input_tokens     BIGINT      NOT NULL DEFAULT 0,
    latency_ms                  BIGINT,
    pricing_mode                TEXT,
    ts                          TIMESTAMPTZ NOT NULL DEFAULT now(),
    parent_request_id           TEXT
);

-- The gateway's own index names, deliberately not tokenweir's.
CREATE INDEX idx_gateway_usage_ts ON gateway_usage (ts);
CREATE INDEX idx_gateway_usage_app_ts ON gateway_usage (app_id, ts);
CREATE INDEX idx_gateway_usage_parent
    ON gateway_usage (parent_request_id) WHERE parent_request_id IS NOT NULL;

CREATE TABLE model_pricing_rates (
    model                         TEXT            NOT NULL,
    pricing_mode                  TEXT            NOT NULL,
    input_cost_usd_per_mtok       NUMERIC(18, 10) NOT NULL,
    output_cost_usd_per_mtok      NUMERIC(18, 10) NOT NULL,
    cache_write_cost_usd_per_mtok NUMERIC(18, 10),
    cache_read_cost_usd_per_mtok  NUMERIC(18, 10),
    source                        TEXT,
    loaded_at                     TIMESTAMPTZ     NOT NULL DEFAULT now(),
    PRIMARY KEY (model, pricing_mode)
);

CREATE VIEW gateway_usage_daily AS
SELECT
    u.app_id,
    (u.ts AT TIME ZONE INTERVAL '0')::DATE AS usage_day,
    u.model,
    COUNT(*)                    AS calls,
    SUM(u.input_tokens)::BIGINT AS input_tokens,
    SUM(u.output_tokens)::BIGINT AS output_tokens
FROM gateway_usage u
GROUP BY 1, 2, 3;

CREATE TABLE schema_migrations (
    version    INTEGER     PRIMARY KEY,
    name       TEXT        NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations (version, name) VALUES
    (1, 'gateway_usage'), (2, 'parent_request_id'), (3, 'model_pricing_rates'),
    (4, 'reader_grant'), (5, 'gateway_usage_daily'), (6, 'gateway_usage_app_day_index');
"""

#: Two usage rows and one rate, enough to price. The second row has a NULL
#: `pricing_mode` — a row written before that column meant anything, which the
#: rollup must treat as ordinary API usage rather than drop.
LEGACY_ROWS = """
INSERT INTO gateway_usage
    (request_id, app_id, endpoint, model, status, input_tokens, output_tokens,
     pricing_mode, ts)
VALUES
    ('req-1', 'mado', '/v1/messages', 'claude-opus-5', 'ok', 1000, 500, 'api',
     '2026-08-01T12:00:00Z'),
    ('req-2', 'mado', '/v1/messages', 'claude-opus-5', 'ok', 2000, 100, NULL,
     '2026-08-01T13:00:00Z');

INSERT INTO model_pricing_rates
    (model, pricing_mode, input_cost_usd_per_mtok, output_cost_usd_per_mtok, source)
VALUES ('claude-opus-5', 'api', 15, 75, 'vendor list');
"""

BASELINE = "2024-01-01"

OWNED = ("gateway_usage", "model_pricing_rates", "gateway_usage_daily")

#: SC-001's third deviation, named. Reconciling adds a column with `ALTER TABLE …
#: ADD COLUMN`, which puts it at the end of the table rather than in the position
#: migration 001 or 003 gives it. These are the two the story's known deltas add;
#: the view is rebuilt wholesale from 005, so its columns land in the migration's
#: own order and it appends nothing.
APPENDED_BY_RECONCILIATION = {
    "gateway_usage": ("schema_version",),
    "model_pricing_rates": ("effective_from",),
    "gateway_usage_daily": (),
}


def execute(connection, sql):
    with connection.cursor() as cursor:
        cursor.execute(sql)
    connection.commit()


def fetch(connection, sql, params=None):
    with connection.cursor() as cursor:
        cursor.execute(sql, params)
        return cursor.fetchall()


@pytest.fixture
def legacy(connection):
    """A database shaped like the one the AI Gateway has been migrating."""
    execute(connection, LEGACY_SQL)
    return connection


@pytest.fixture
def legacy_with_rows(legacy):
    execute(legacy, LEGACY_ROWS)
    return legacy


def descriptions(result):
    return " | ".join(d.description for d in result.discrepancies)


def find(result, relation, needle):
    """The one discrepancy about `relation` whose description contains `needle`."""
    matches = [
        d
        for d in result.discrepancies
        if d.relation == relation and needle in d.description
    ]
    assert len(matches) == 1, (
        f"expected exactly one {relation} discrepancy mentioning {needle!r}, got "
        f"{len(matches)}: {descriptions(result)}"
    )
    return matches[0]


# --- User story 1: tell me what is wrong with this database -------------------


def test_the_plan_names_the_three_known_deltas(legacy_with_rows):
    """The story's own list, each one found on a database that has it.

    Written against the deltas by name rather than against a count, so that a
    reconciler which found three *different* things would fail here rather than
    pass on arithmetic.
    """
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    schema_version = find(result, "gateway_usage", "schema_version")
    assert schema_version.resolution is Resolution.AUTOMATIC
    assert "back-filled to 1" in schema_version.description

    rate_card = find(result, "model_pricing_rates", "current-valued")
    assert rate_card.resolution is Resolution.AUTOMATIC
    assert "effective_from" in rate_card.description

    rollup = find(result, "gateway_usage_daily", "rollup view differs")
    assert rollup.resolution is Resolution.AUTOMATIC
    # The gateway's view has no pricing_mode and no cost columns; tokenweir's has
    # both, which is hand-off note 2 stated as a schema difference.
    assert "pricing_mode" in rollup.description
    assert "est_cost_usd" in rollup.description


def test_a_database_tokenweir_migrated_itself_has_nothing_to_reconcile(migrated):
    """The other end of the same comparison, and the one that keeps it honest.

    A reconciler that reported differences here would be describing its own
    introspection rather than the database — the reference schema and the live
    schema are built from the same six files.
    """
    result = plan(migrated, baseline_effective_from=BASELINE)
    assert result.is_empty, result.render()
    assert result.observations == ()


def test_planning_changes_nothing_at_all(legacy_with_rows, scratch_schema):
    """FR-002. The reference schema is built on this very connection; if the
    rollback were not real, the operator's first read-only look at production
    would leave a schema behind in it."""
    _, schema_name = scratch_schema
    before = _snapshot(legacy_with_rows, OWNED)
    rows_before = fetch(legacy_with_rows, "SELECT * FROM gateway_usage ORDER BY id")

    plan(legacy_with_rows, baseline_effective_from=BASELINE)

    assert _snapshot(legacy_with_rows, OWNED) == before
    assert fetch(legacy_with_rows, "SELECT * FROM gateway_usage ORDER BY id") == rows_before

    leftovers = fetch(
        legacy_with_rows,
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s",
        (f"{REFERENCE_SCHEMA_PREFIX}%",),
    )
    assert leftovers == [], f"reference schema survived the rollback: {leftovers}"


def test_a_column_the_gateway_owns_is_reported_and_never_dropped(legacy_with_rows):
    """FR-009. The hand-off comment's warning, inverted into a guarantee: a column
    tokenweir does not know about is the gateway's, and the reconciler says so and
    leaves it."""
    execute(legacy_with_rows, "ALTER TABLE gateway_usage ADD COLUMN tenant_id TEXT")

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    noted = [o for o in result.observations if "tenant_id" in o.description]
    assert len(noted) == 1
    assert "kept and left alone" in noted[0].description
    assert not any("tenant_id" in d.description for d in result.discrepancies)
    assert not any("tenant_id" in s for s in result.statements)
    assert "DROP COLUMN" not in " ".join(result.statements).upper()


def test_an_extra_not_null_column_on_the_usage_table_is_a_decision(legacy_with_rows):
    """FR-010. `PostgresSource`'s INSERT does not name it, so every batch would
    fail — a reconciliation that reported success here would have handed back a
    database that cannot be written to."""
    execute(
        legacy_with_rows,
        "ALTER TABLE gateway_usage ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'x'; "
        "ALTER TABLE gateway_usage ALTER COLUMN tenant_id DROP DEFAULT",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    blocker = find(result, "gateway_usage", "tenant_id")
    assert blocker.resolution is Resolution.MANUAL
    assert "PostgresSource" in blocker.remedy
    assert "will not drop it" in blocker.remedy


def test_an_extra_not_null_column_on_a_table_nothing_writes_is_only_a_note(
    legacy_with_rows,
):
    """The other half of the same rule, and the reason it is scoped to the writer's
    table rather than stated generally.

    `model_pricing_rates.pricing_mode` is NOT NULL on the live database — it was
    half the old primary key — and it survives the restructure. Nothing in
    tokenweir writes to that table, so blocking on it would refuse the *ordinary*
    case: the reconciliation the story is actually asking for would never run.
    """
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    assert not any(
        d.relation == "model_pricing_rates" and "pricing_mode" in d.description
        for d in result.discrepancies
    ), descriptions(result)
    noted = [
        o
        for o in result.observations
        if o.relation == "model_pricing_rates" and "pricing_mode" in o.description
    ]
    assert len(noted) == 1
    assert "must still supply it" in noted[0].description


def test_indexes_are_matched_by_shape_not_by_name(legacy_with_rows):
    """The gateway named its indexes itself. An index over the same columns under
    another name is the same index, and creating a second one beside it would cost
    write throughput to serve nothing.

    Migration 006's functional index is genuinely absent from the legacy shape —
    it was tokenweir's fix — so this asserts both directions at once.
    """
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    missing = [d for d in result.discrepancies if "index missing" in d.description]
    assert len(missing) == 1, descriptions(result)
    # The day-bucketed expression from 005/006, not the raw `ts` column the legacy
    # database already indexes under another name.
    assert "date" in missing[0].description
    assert "app_id" in missing[0].description


def test_an_index_the_gateway_owns_is_reported_and_left_alone(legacy_with_rows):
    """FR-016's other half. The legacy indexes are named differently from
    tokenweir's and cover the same columns, so they are matched by shape — and the
    ones that cover something else are still the gateway's."""
    execute(legacy_with_rows, "CREATE INDEX gateway_usage_status_idx ON gateway_usage (status)")

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    noted = [o for o in result.observations if "gateway_usage_status_idx" in o.description]
    assert len(noted) == 1
    assert "left in place" in noted[0].description
    assert "DROP INDEX" not in " ".join(result.statements).upper()


def test_an_index_squatting_on_a_shipped_index_name_is_a_decision(legacy_with_rows):
    """Index names are unique per schema, and the gateway named its own.

    Review 1's Med-1: this was planned AUTOMATIC, and the statement is
    `pg_get_indexdef`'s — tokenweir's name, no IF NOT EXISTS — so Postgres answered
    `DuplicateTable` and the operator got a raw driver error out of a plan that had
    told them every difference was resolvable. Nothing was corrupted; the promise
    the plan makes was.
    """
    execute(
        legacy_with_rows,
        "CREATE INDEX gateway_usage_app_day_idx ON gateway_usage (endpoint)",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    collision = find(result, "gateway_usage", "gateway_usage_app_day_idx exists")
    assert collision.resolution is Resolution.MANUAL
    assert collision.statements == ()
    assert "will not drop yours" in collision.remedy

    # And the whole point of classifying it: nothing runs.
    with pytest.raises(ManualResolutionError):
        apply(legacy_with_rows, baseline_effective_from=BASELINE)


# --- Constraints (review 2, High-1) -------------------------------------------


def test_the_rate_cards_non_negative_check_survives_reconciliation(
    legacy_with_rows, psycopg_module
):
    """Migration 003's `CHECK (… >= 0)` is the guard that stops a negative price
    entering the rate card. `Relation` modelled columns, keys and indexes and not
    constraints, so a reconciled table did not have it — and `plan()` then printed
    "this database already matches the schema tokenweir owns", which is a false
    claim about a money guard made by the one sentence an operator reads before
    pointing the gateway at the database.

    Red before the fix in three separate ways: the plan was empty, the constraint
    was absent, and the negative insert succeeded.
    """
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)
    missing = find(result, "model_pricing_rates", "costs_non_negative")
    assert missing.resolution is Resolution.AUTOMATIC
    assert "existing rows satisfy it" in missing.description

    apply(legacy_with_rows, baseline_effective_from=BASELINE)

    with pytest.raises(psycopg_module.errors.CheckViolation):
        execute(
            legacy_with_rows,
            "INSERT INTO model_pricing_rates (model, pricing_mode, effective_from, "
            "input_cost_usd_per_mtok, output_cost_usd_per_mtok) "
            "VALUES ('negative', 'api', DATE '2024-01-01', -5, 1)",
        )
    legacy_with_rows.rollback()

    assert plan(legacy_with_rows, baseline_effective_from=BASELINE).is_empty


def test_a_constraint_existing_rows_already_violate_is_a_decision(legacy_with_rows):
    """`ADD CONSTRAINT … CHECK` validates the rows already there, so classifying it
    AUTOMATIC without asking the data would be a plan that promises to resolve every
    difference and then dies on one — the same mistake the index-name collision was.

    And what it finds is worth finding: a price tokenweir's schema calls impossible
    is already stored.
    """
    execute(
        legacy_with_rows,
        "INSERT INTO model_pricing_rates (model, pricing_mode, "
        "input_cost_usd_per_mtok, output_cost_usd_per_mtok) "
        "VALUES ('claude-haiku-4-5', 'api', -1, 1)",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    blocked = find(result, "model_pricing_rates", "existing rows violate it")
    assert blocked.resolution is Resolution.MANUAL
    assert blocked.statements == ()
    assert "bill anything" in blocked.remedy

    with pytest.raises(ManualResolutionError):
        apply(legacy_with_rows, baseline_effective_from=BASELINE)


def test_a_null_in_a_checked_column_is_not_a_violation(legacy_with_rows):
    """A CHECK is satisfied when its expression is TRUE **or NULL**. The probe uses
    `NOT (body)`, whose NULL is not TRUE, so a nullable cache rate left unset does
    not read as a row that violates the guard — which would have made the ordinary
    rate card MANUAL and refused the reconciliation this story is for."""
    assert fetch(
        legacy_with_rows,
        "SELECT cache_read_cost_usd_per_mtok FROM model_pricing_rates",
    ) == [(None,)]

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    assert find(
        result, "model_pricing_rates", "costs_non_negative"
    ).resolution is Resolution.AUTOMATIC


def test_a_constraint_the_gateway_owns_is_reported_and_kept(legacy_with_rows):
    """FR-009's treatment, applied to constraints: it is the gateway's rule about
    the gateway's data, and this module does not remove it."""
    execute(
        legacy_with_rows,
        "ALTER TABLE gateway_usage ADD CONSTRAINT gateway_usage_status_known "
        "CHECK (status IN ('ok', 'error'))",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    noted = [o for o in result.observations if "gateway_usage_status_known" in o.description]
    assert len(noted) == 1
    assert "left in place" in noted[0].description
    assert "DROP CONSTRAINT gateway_usage_status_known" not in " ".join(result.statements)


def test_a_constraint_squatting_on_a_shipped_name_is_a_decision(legacy_with_rows):
    """Constraint names are unique per table, so `ADD CONSTRAINT` would fail on the
    duplicate — the index-name collision one relation over."""
    execute(
        legacy_with_rows,
        "ALTER TABLE model_pricing_rates "
        "ADD CONSTRAINT model_pricing_rates_costs_non_negative CHECK (model <> '')",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    collision = find(result, "model_pricing_rates", "says something else")
    assert collision.resolution is Resolution.MANUAL
    assert collision.statements == ()


def test_a_check_about_columns_the_table_lacks_does_not_crash_the_plan(legacy):
    """Review 3's High-1, and the sharpest kind of finding: the tool failed in
    exactly the way the spec says its design prevents.

    A CHECK is probed by running its own expression against the live rows, and
    migration 003's guard names both cache-cost columns. A rate card without them —
    which A-001 explicitly declines to rule out, because nobody has seen the live
    schema — turned the read-only command into a raw `UndefinedColumn` from the
    driver, out of `plan()`, leaving the connection in an aborted transaction.

    spec.md: "a tool that reads the real thing and refuses what it does not
    recognise does not [break]". It refused nothing.
    """
    execute(
        legacy,
        "ALTER TABLE model_pricing_rates "
        "DROP COLUMN cache_write_cost_usd_per_mtok, "
        "DROP COLUMN cache_read_cost_usd_per_mtok",
    )

    result = plan(legacy, baseline_effective_from=BASELINE)

    deferred = find(result, "model_pricing_rates", "cannot be checked yet")
    assert deferred.resolution is Resolution.MANUAL
    assert "cache_write_cost_usd_per_mtok" in deferred.description
    assert deferred.statements == ()
    # The columns it is waiting on are in this same plan, which is what makes
    # "resolve them first, then run it again" advice rather than a brush-off.
    assert find(result, "model_pricing_rates", "cache_write_cost_usd_per_mtok (numeric")

    # And having refused, it left a connection somebody can still use.
    assert fetch(legacy, "SELECT 1") == [(1,)]


def test_a_failed_plan_leaves_a_usable_connection(legacy, monkeypatch):
    """The other half of High-1. Every probe runs in one transaction, so an error
    from any of them aborts it — and returning without a rollback hands back a
    connection that rejects the caller's *next* statement with "current transaction
    is aborted", an error about `plan()` reported wherever the caller happens to be.
    """
    import tokenweir.reconcile as reconcile_module

    def explode(*args, **kwargs):
        with legacy.cursor() as cursor:
            cursor.execute("SELECT 1 / 0")

    monkeypatch.setattr(reconcile_module, "_compare_indexes", explode)

    with pytest.raises(Exception):
        plan(legacy, baseline_effective_from=BASELINE)

    assert fetch(legacy, "SELECT 1") == [(1,)]


def test_nullability_is_not_reported_twice_as_a_missing_constraint(legacy):
    """Review 3's Med-1, guarded where it can be guarded on this server.

    PostgreSQL 18 catalogues NOT NULL in `pg_constraint` as `contype = 'n'`. Under
    the deny-list this used to use (`contype <> 'p'`), every not-null column the
    live table lacked would arrive as an unparseable "missing constraint", go
    MANUAL, and FR-019 would block the whole reconciliation — on `schema_version`
    and `effective_from`, the two columns this story exists to add.

    The symptom cannot be reproduced on the embedded PostgreSQL 16 this suite runs
    against, so **this test does not prove the PG 18 behaviour** and should not be
    counted as covering it. What it does check is the property the allow-list
    exists to preserve on any version: nullability is reported as a column
    difference and by nothing else. On 18 the same assertion would fail if
    `contype = 'n'` rows leaked in.
    """
    # Deliberately *not* asserted here: `"n" not in COMPARED_CONSTRAINT_TYPES`,
    # which review 5 pointed out is a literal compared against itself and would
    # stay green if the constant were referenced nowhere. What follows is the real
    # claim, and it is the one this server can actually answer.
    result = plan(legacy, baseline_effective_from=BASELINE)
    constraint_talk = [
        d.description for d in result.discrepancies if "constraint" in d.description
    ]
    assert not any("NOT NULL" in description for description in constraint_talk), (
        constraint_talk
    )


def test_a_generated_column_is_a_decision_not_an_invisible_match(legacy):
    """Low-2. A generated column has no `column_default` and its type says nothing,
    so without `attgenerated` a live `GENERATED ALWAYS AS (…) STORED` introspected
    identically to tokenweir's ordinary column — an empty plan, and a writer whose
    every INSERT is rejected for supplying a value to it."""
    execute(legacy, "ALTER TABLE gateway_usage DROP COLUMN latency_ms")
    execute(
        legacy,
        "ALTER TABLE gateway_usage ADD COLUMN latency_ms BIGINT "
        "GENERATED ALWAYS AS (input_tokens + output_tokens) STORED",
    )

    result = plan(legacy, baseline_effective_from=BASELINE)

    generated = find(result, "gateway_usage", "latency_ms is a generated column")
    assert generated.resolution is Resolution.MANUAL
    assert generated.statements == ()


def test_an_identity_column_is_a_decision_not_an_invisible_match(legacy_with_rows):
    """Review 5's High-1, and the same hole as the generated-column one above.

    `Column.identity` has been captured since the first commit and its docstring
    says it exists so `BIGSERIAL` versus `GENERATED ALWAYS AS IDENTITY` "is
    invisible to anything that only reads defaults" — and then nothing compared it,
    so it made nothing visible. An identity column has no `column_default` and an
    ordinary type, so it matched on every field the diff *did* compare, while
    rejecting every INSERT the writer makes.

    A field captured and never read is worse than one never captured: it reads as
    coverage. `plan()` printed "this database already matches the schema tokenweir
    owns" about a database that cannot be written to — FR-017's named wrong answer,
    and the story's own phrase for what it exists to prevent.
    """
    execute(
        legacy_with_rows,
        "ALTER TABLE gateway_usage "
        "ADD COLUMN schema_version INTEGER NOT NULL GENERATED ALWAYS AS IDENTITY",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    mismatch = find(result, "gateway_usage", "schema_version is an identity column")
    assert mismatch.resolution is Resolution.MANUAL
    assert mismatch.statements == ()
    assert "DROP IDENTITY" in mismatch.remedy


def test_the_exempt_id_column_stays_exempt(legacy_with_rows):
    """The other side of High-1's fix. `gateway_usage.id` is `BIGSERIAL` on the
    live table and an identity column in tokenweir's schema — the one identity
    difference the story says to keep — so comparing identity everywhere must not
    start reporting it."""
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    assert not any("id is" in d.description for d in result.discrepancies), (
        descriptions(result)
    )
    apply(legacy_with_rows, baseline_effective_from=BASELINE)
    assert plan(legacy_with_rows, baseline_effective_from=BASELINE).is_empty


def test_a_column_defaulting_to_now_is_not_back_filled_with_this_moment(
    legacy_with_rows,
):
    """Review 5's Med-1. `ADD COLUMN … DEFAULT now()` stamps every pre-existing row
    with the instant of the reconciliation — a fact about this run, stored as
    though it were a fact about the row.

    `ts` is the day bucket the rollup groups on, so the whole history would price
    into today: an SC-004 failure delivered by a plan that called itself AUTOMATIC
    and exited 0. It is the objection FR-013 already makes about the rate card's
    baseline, and refusing to invent an `effective_from` while silently inventing a
    `ts` is not a position worth holding.
    """
    execute(legacy_with_rows, "DROP VIEW gateway_usage_daily")
    execute(legacy_with_rows, "ALTER TABLE gateway_usage DROP COLUMN ts")

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    refused = find(result, "gateway_usage", "ts (timestamp with time zone) is missing")
    assert refused.resolution is Resolution.MANUAL
    assert refused.statements == ()
    assert "not a constant" in refused.description

    with pytest.raises(ManualResolutionError):
        apply(legacy_with_rows, baseline_effective_from=BASELINE)


def test_a_constant_default_is_still_back_filled(legacy_with_rows):
    """The allow-list has to let the ordinary case through, or FR-011b would refuse
    every column migration 001 gives a `DEFAULT 0`. Asserted beside its refusal so
    the two cannot drift into one answer."""
    execute(
        legacy_with_rows, "ALTER TABLE gateway_usage DROP COLUMN cache_read_input_tokens"
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    added = find(result, "gateway_usage", "cache_read_input_tokens (bigint) is missing")
    assert added.resolution is Resolution.AUTOMATIC

    apply(legacy_with_rows, baseline_effective_from=BASELINE)
    assert fetch(
        legacy_with_rows, "SELECT DISTINCT cache_read_input_tokens FROM gateway_usage"
    ) == [(0,)]


# --- The classification branches, one deformation each (review 3, Med-3) -------
#
# Six branches that had code and no test. Each emits SQL or a refusal that had
# never been executed against a server, and two of them (`SET DEFAULT`, the
# primary-key mismatch) sit squarely inside SC-001's "same … defaults … primary
# keys". Written as one parametrised test because the shape is identical: deform
# `LEGACY_SQL` in one way, assert the classification and the statement.


@pytest.mark.parametrize(
    "deformation,needle,expected,statement_fragment",
    [
        pytest.param(
            "ALTER TABLE gateway_usage DROP COLUMN cache_creation_input_tokens",
            "cache_creation_input_tokens (bigint) is missing",
            Resolution.AUTOMATIC,
            "ADD COLUMN \"cache_creation_input_tokens\" bigint DEFAULT 0 NOT NULL",
            id="missing-column-with-a-default",
        ),
        pytest.param(
            "ALTER TABLE gateway_usage DROP COLUMN queue",
            "queue (text) is missing",
            Resolution.AUTOMATIC,
            "ADD COLUMN \"queue\" text",
            id="missing-nullable-column",
        ),
        pytest.param(
            "ALTER TABLE gateway_usage DROP COLUMN status",
            "status (text) is missing, and it is NOT NULL with no default",
            Resolution.MANUAL,
            None,
            id="missing-not-null-column-with-no-backfill",
        ),
        pytest.param(
            "UPDATE gateway_usage SET workload = ''; "
            "ALTER TABLE gateway_usage ALTER COLUMN workload SET NOT NULL",
            "workload is NOT NULL; tokenweir's is nullable",
            Resolution.MANUAL,
            None,
            id="live-stricter-than-tokenweir",
        ),
        pytest.param(
            "ALTER TABLE gateway_usage ALTER COLUMN input_tokens SET DEFAULT 5",
            "input_tokens defaults to 5",
            Resolution.AUTOMATIC,
            "SET DEFAULT 0",
            id="default-mismatch",
        ),
        pytest.param(
            "ALTER TABLE gateway_usage DROP CONSTRAINT gateway_usage_pkey; "
            "ALTER TABLE gateway_usage ADD PRIMARY KEY (request_id)",
            "primary key is (request_id)",
            Resolution.MANUAL,
            None,
            id="primary-key-mismatch-outside-the-rate-card",
        ),
    ],
)
def test_each_classification_branch(
    legacy_with_rows, deformation, needle, expected, statement_fragment
):
    execute(legacy_with_rows, deformation)

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    found = find(result, "gateway_usage", needle)
    assert found.resolution is expected
    if statement_fragment is None:
        assert found.statements == ()
        assert found.remedy, "a MANUAL discrepancy an operator cannot act on is a crash"
    else:
        assert any(statement_fragment in s for s in found.statements), found.statements
        # And the statement runs. A branch whose SQL has never touched a server is
        # a guess about syntax, which is what five of these were.
        apply(legacy_with_rows, baseline_effective_from=BASELINE)
        assert plan(legacy_with_rows, baseline_effective_from=BASELINE).is_empty


def test_a_rate_card_missing_a_key_column_is_refused_not_crashed_into(legacy):
    """Review 4's Low-3, and the same class review 3 fixed one path over: every
    probe on the rate-card path names a column, and a table that has not got it
    turned the read-only command into a driver error rather than a refusal."""
    execute(legacy, "DROP VIEW gateway_usage_daily")
    execute(legacy, "ALTER TABLE model_pricing_rates DROP COLUMN model CASCADE")

    result = plan(legacy, baseline_effective_from=BASELINE)

    refused = find(result, "model_pricing_rates", "has not got the column")
    assert refused.resolution is Resolution.MANUAL
    assert refused.statements == ()
    assert fetch(legacy, "SELECT 1") == [(1,)]


def test_a_rate_card_with_a_null_in_the_new_key_is_refused(legacy_with_rows):
    """`ADD PRIMARY KEY` rejects a NULL in a key column, so AUTOMATIC would be a
    plan that promises resolvable and dies — FR-016a's rationale, one relation over.

    Impossible while the table keeps `PRIMARY KEY (model, pricing_mode)`, since a
    key column is NOT NULL. Reachable on one that lost its key along the way, which
    is the database class this module exists for.
    """
    execute(
        legacy_with_rows,
        "ALTER TABLE model_pricing_rates DROP CONSTRAINT model_pricing_rates_pkey; "
        "ALTER TABLE model_pricing_rates ALTER COLUMN model DROP NOT NULL; "
        "INSERT INTO model_pricing_rates (model, pricing_mode, "
        "input_cost_usd_per_mtok, output_cost_usd_per_mtok) VALUES (NULL, 'api', 1, 1)",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    refused = find(result, "model_pricing_rates", "cannot be re-keyed: model holds NULLs")
    assert refused.resolution is Resolution.MANUAL
    assert refused.statements == ()

    # The generic column diff reaches the same rows by another route — tokenweir's
    # `model` is NOT NULL and this one is not. Both are true and both are MANUAL;
    # they are not duplicates of each other, because fixing the nullability is not
    # the same decision as deciding what a rate with no model means.
    assert find(result, "model_pricing_rates", "column model is nullable and holds NULLs")

    with pytest.raises(ManualResolutionError):
        apply(legacy_with_rows, baseline_effective_from=BASELINE)


def test_planning_refuses_an_autocommit_connection(scratch_schema):
    """FR-005. Under autocommit the reference schema's rollback does nothing, and
    a scratch schema would be left in the operator's database by the command whose
    whole promise is that it changes nothing."""
    connect, _ = scratch_schema
    connection = connect()
    connection.autocommit = True

    with pytest.raises(ValueError, match="autocommit"):
        plan(connection)


def test_a_reference_that_cannot_be_built_is_an_error_not_an_empty_plan(
    legacy_with_rows, psycopg_module, postgres_dsn, scratch_schema
):
    """FR-017 names the wrong answer: "MUST NOT report 'no differences' for any
    reason other than having found none."

    The reference schema needs `CREATE` on the database. Without it there is no
    target to compare against, and the failure mode worth guarding is not a crash —
    it is a reconciler that swallows the error and reports a clean database to an
    operator about to point the gateway at it.
    """
    _, schema_name = scratch_schema
    role = f"tokenweir_nocreate_{uuid.uuid4().hex[:8]}"

    admin = psycopg_module.connect(postgres_dsn)
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE ROLE "{role}" LOGIN')
        cursor.execute(f'GRANT USAGE ON SCHEMA "{schema_name}" TO "{role}"')
        cursor.execute(
            f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema_name}" TO "{role}"'
        )
    try:
        restricted = psycopg_module.connect(postgres_dsn, user=role)
        try:
            with restricted.cursor() as cursor:
                cursor.execute(f'SET search_path TO "{schema_name}"')
            restricted.commit()

            with pytest.raises(ReferenceSchemaError, match="CREATE SCHEMA"):
                plan(restricted, baseline_effective_from=BASELINE)
        finally:
            restricted.close()

        leftovers = fetch(
            legacy_with_rows,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name LIKE %s",
            (f"{REFERENCE_SCHEMA_PREFIX}%",),
        )
        assert leftovers == []
    finally:
        with admin.cursor() as cursor:
            cursor.execute(
                f'REVOKE ALL ON ALL TABLES IN SCHEMA "{schema_name}" FROM "{role}"'
            )
            cursor.execute(f'REVOKE ALL ON SCHEMA "{schema_name}" FROM "{role}"')
            cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def test_a_reference_build_that_fails_mid_way_leaves_no_schema_behind(
    legacy, monkeypatch
):
    """FR-006 says the scratch schema must be removed "even when the reference
    build fails". The test above fails at `CREATE SCHEMA` itself, so the schema
    never exists and the rollback it asserts has nothing to undo — the branch was
    covered in name only. This one fails after the schema is created and two
    migrations are in it."""
    import tokenweir.reconcile as reconcile_module

    real_discover = reconcile_module.discover

    def half_a_schema():
        shipped = real_discover()
        broken = type(shipped[-1])(
            version=99, name="broken", sql="CREATE TABLE nonsense (bad_type NOT_A_TYPE)"
        )
        return shipped[:2] + (broken,)

    monkeypatch.setattr(reconcile_module, "discover", half_a_schema)

    with pytest.raises(ReferenceSchemaError):
        plan(legacy, baseline_effective_from=BASELINE)

    monkeypatch.undo()
    assert fetch(
        legacy,
        "SELECT schema_name FROM information_schema.schemata WHERE schema_name LIKE %s",
        (f"{REFERENCE_SCHEMA_PREFIX}%",),
    ) == []
    assert fetch(legacy, "SELECT 1") == [(1,)]


def test_a_missing_table_says_to_apply_rather_than_reconcile(connection):
    """An empty database is not a reconciliation. Emitting CREATE TABLE from here
    would produce a schema with no `schema_migrations` rows describing it."""
    result = plan(connection)

    usage = find(result, "gateway_usage", "absent")
    assert usage.resolution is Resolution.MANUAL
    assert "none of" in usage.remedy
    assert "migrations apply" in usage.remedy


def test_a_partially_migrated_database_is_not_sent_to_a_command_that_does_nothing(
    legacy,
):
    """Review 2's Med-1. The remedy for an absent table said to run `apply` — which
    on a database whose `schema_migrations` records 1-6 returns `()` and creates
    nothing, because that silent no-op is this module's entire reason for existing.

    The empty-database test below asserts the *other* remedy, and it was the only
    one there was: it uses the one database where the advice happened to be true.
    """
    execute(legacy, "DROP TABLE model_pricing_rates CASCADE")

    result = plan(legacy, baseline_effective_from=BASELINE)

    absent = find(result, "model_pricing_rates", "absent")
    assert absent.resolution is Resolution.MANUAL
    assert "Do not reach for `apply`" in absent.remedy
    assert "003_model_pricing_rates.sql" in absent.remedy

    # And the advice it replaced is provably useless here.
    assert apply_migrations(legacy) == ()
    assert fetch(legacy, "SELECT to_regclass('model_pricing_rates')") == [(None,)]


def test_an_absent_rollup_view_is_simply_created(legacy):
    """A gateway that never had a rollup. There is nothing to drop and nothing can
    depend on it, so this is the easy case and must not be treated as the hard
    one."""
    execute(legacy, "DROP VIEW gateway_usage_daily")

    result = plan(legacy, baseline_effective_from=BASELINE)

    view = find(result, "gateway_usage_daily", "absent")
    assert view.resolution is Resolution.AUTOMATIC
    assert "DROP VIEW" not in " ".join(view.statements).upper()


def test_the_reference_schema_is_what_the_migrations_build(connection):
    """FR-004, asserted rather than assumed: the reference is not a description of
    the schema kept beside the migrations, it is the migrations."""
    reference = reference_snapshot(connection)

    assert {r.name for r in reference.relations} == set(OWNED)
    usage = reference.get("gateway_usage")
    assert usage.column("schema_version").not_null
    # `format_type`, not `information_schema.data_type` — the precision is the
    # point, and losing it is a money bug nobody would see.
    rates = reference.get("model_pricing_rates")
    assert rates.column("input_cost_usd_per_mtok").type == "numeric(18,10)"
    assert rates.primary_key == ("model", "effective_from")


# --- User story 2: bring it over without losing a row -------------------------


def test_every_pre_existing_row_survives_unchanged(legacy_with_rows):
    """SC-002, column by column rather than by count. A reconciliation that
    rewrote a row would keep the count."""
    before = fetch(
        legacy_with_rows,
        "SELECT id, request_id, app_id, endpoint, model, status, workload, queue, "
        "input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, latency_ms, pricing_mode, ts, parent_request_id "
        "FROM gateway_usage ORDER BY id",
    )
    rates_before = fetch(
        legacy_with_rows,
        "SELECT model, pricing_mode, input_cost_usd_per_mtok, "
        "output_cost_usd_per_mtok, source FROM model_pricing_rates ORDER BY model",
    )

    apply(legacy_with_rows, baseline_effective_from=BASELINE)

    after = fetch(
        legacy_with_rows,
        "SELECT id, request_id, app_id, endpoint, model, status, workload, queue, "
        "input_tokens, output_tokens, cache_creation_input_tokens, "
        "cache_read_input_tokens, latency_ms, pricing_mode, ts, parent_request_id "
        "FROM gateway_usage ORDER BY id",
    )
    assert after == before
    assert fetch(
        legacy_with_rows,
        "SELECT model, pricing_mode, input_cost_usd_per_mtok, "
        "output_cost_usd_per_mtok, source FROM model_pricing_rates ORDER BY model",
    ) == rates_before

    assert fetch(legacy_with_rows, "SELECT DISTINCT schema_version FROM gateway_usage") == [(1,)]
    assert fetch(
        legacy_with_rows, "SELECT DISTINCT effective_from FROM model_pricing_rates"
    ) == [(datetime.date(2024, 1, 1),)]


def test_the_reconciled_schema_matches_one_tokenweir_built_itself(
    legacy_with_rows, scratch_schema, psycopg_module, postgres_dsn
):
    """SC-001, and the strongest claim in this file.

    Not "the columns I remembered to check": the whole introspection of both
    databases, compared. **Four** deviations are subtracted by name and nothing
    else is:

    1. `gateway_usage.id` stays `BIGSERIAL` rather than becoming an identity
       column, because the story says to keep it.
    2. The gateway's extra `pricing_mode` on the rate card stays, because this
       module does not drop data.
    3. Column **order** differs: an added column lands at the end of the table
       rather than in the migration's position. Nothing reads column position and
       reordering would mean rewriting the table, so this is a deviation rather
       than a defect — but it is one, and review 1 found this docstring claiming
       there were two while silently dropping it by comparing dicts.
    4. **Index and constraint names** differ where the gateway named its own.
       Both are matched by shape and by definition respectively, so an index doing
       tokenweir's job under the gateway's name satisfies the requirement and is
       left alone — creating a duplicate beside it would cost write throughput to
       serve nothing. Review 3 found this one the same way review 1 found the
       third: claimed absent by a docstring, and invisible to the comparison
       underneath it.

    `identity` is compared along with type, nullability and default, so the `id`
    exception is the only place `BIGSERIAL` versus `GENERATED ALWAYS AS IDENTITY`
    is allowed to differ.
    """
    apply(legacy_with_rows, baseline_effective_from=BASELINE)
    reconciled = _snapshot(legacy_with_rows, OWNED)

    native = reference_snapshot(legacy_with_rows)

    for name in OWNED:
        got, want = reconciled.get(name), native.get(name)
        assert got is not None and want is not None
        assert got.kind == want.kind, name
        assert got.primary_key == want.primary_key, name

        got_columns = {
            c.name: c
            for c in got.columns
            if not (name == "gateway_usage" and c.name == "id")
            and not (name == "model_pricing_rates" and c.name == "pricing_mode")
        }
        want_columns = {
            c.name: c for c in want.columns if not (name == "gateway_usage" and c.name == "id")
        }
        assert got_columns.keys() == want_columns.keys(), name

        # Deviation 3, subtracted **by name** rather than by the dicts above —
        # which are keyed by column name and so are order-blind by construction,
        # which is exactly what SC-001 forbids: "the assertion MUST subtract each by
        # name rather than by comparing in a way that cannot see it". Review 4 found
        # this criterion being satisfied by the one mechanism it was written to rule
        # out.
        #
        # The claim is narrow: the two orders differ *only* by the columns this
        # reconciliation added having been appended. A column that moved for any
        # other reason fails here, which is what makes this an assertion rather than
        # an exemption.
        got_order = [c.name for c in got.columns if c.name in got_columns]
        native_order = [c.name for c in want.columns if c.name in want_columns]
        appended = APPENDED_BY_RECONCILIATION[name]
        assert got_order == [c for c in native_order if c not in appended] + list(
            appended
        ), f"{name}: {got_order} vs {native_order}"
        if appended:
            assert got_order != native_order, (
                f"{name}: the ordering deviation has stopped happening — if adding a "
                "column no longer moves it to the end, SC-001 and the README should "
                "stop saying it does"
            )
        for column, expected in want_columns.items():
            actual = got_columns[column]
            assert (
                actual.type,
                actual.not_null,
                actual.default,
                actual.identity,
            ) == (
                expected.type,
                expected.not_null,
                expected.default,
                expected.identity,
            ), f"{name}.{column}"

        # Every index tokenweir wants is present by shape. The gateway's own extra
        # ones are still there and are not asserted away.
        want_shapes = {shape for shape, _ in want.indexes}
        got_shapes = {shape for shape, _ in got.indexes}
        assert want_shapes <= got_shapes, name

        # Deviation 4, asserted rather than hidden by the subset above. Indexes are
        # matched by shape, so an index the gateway named itself satisfies
        # tokenweir's requirement and keeps its own name — `idx_gateway_usage_ts`
        # where a fresh database has `gateway_usage_ts_idx`. Review 3 found this
        # deviation real, undocumented, and invisible to a `<=` comparison that
        # cannot see names at all. Named here, and in the README, so a fourth one
        # cannot arrive the same way.
        if name == "gateway_usage":
            served_by_a_gateway_name = {
                _index_name(definition)
                for shape, definition in got.indexes
                if shape in want_shapes and _index_name(definition).startswith("idx_")
            }
            assert served_by_a_gateway_name, (
                "the legacy fixture names its indexes differently on purpose; if "
                "none survived, indexes are no longer being matched by shape"
            )

        # Likewise every constraint. Absent from this assertion until review 2, and
        # invisible to it: the comparison is written in terms of `_snapshot`, and
        # `_snapshot` did not model constraints — so implementation and test agreed
        # with each other and disagreed with FR-001.
        assert {c.definition for c in want.constraints} <= {
            c.definition for c in got.constraints
        }, name


def test_an_empty_legacy_table_reconciles_to_the_right_shape(legacy):
    """Low-8. Back-fills and key swaps on zero rows still have to produce the right
    *shape* — a reconciler exercised only against populated tables can pass while
    adding a NOT NULL column the wrong way, because with no rows there is nothing
    for a missing back-fill to fail on."""
    apply(legacy, baseline_effective_from=BASELINE)

    reconciled = _snapshot(legacy, OWNED)
    native = reference_snapshot(legacy)

    usage = reconciled.get("gateway_usage").column("schema_version")
    assert (usage.not_null, usage.default) == (True, None)
    assert reconciled.get("model_pricing_rates").primary_key == (
        native.get("model_pricing_rates").primary_key
    )
    assert plan(legacy, baseline_effective_from=BASELINE).is_empty


def test_the_writer_fails_before_reconciliation_and_works_after(
    legacy, psycopg_module
):
    """SC-003, both halves.

    Asserting only the fix would pass just as happily against a database that never
    had the problem — and "the writer is broken and nothing says so" is the failure
    the whole story is about, so it is worth reproducing before curing.
    """
    record = UsageRecord(
        request_id="req-new",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-opus-5",
        status="ok",
        input_tokens=10,
    )

    with pytest.raises(psycopg_module.errors.UndefinedColumn):
        PostgresSource(legacy).write([record])
    legacy.rollback()

    apply(legacy, baseline_effective_from=BASELINE)

    assert PostgresSource(legacy).write([record]) == 1
    assert fetch(
        legacy,
        "SELECT schema_version FROM gateway_usage WHERE request_id = 'req-new'",
    ) == [(1,)]


def test_pre_existing_rows_still_price(legacy_with_rows):
    """SC-004. The story's third acceptance clause, arithmetic and all.

    1000 input @ $15/Mtok + 500 output @ $75/Mtok = 0.015 + 0.0375 = 0.0525
    2000 input @ $15/Mtok + 100 output @ $75/Mtok = 0.030 + 0.0075 = 0.0375

    Two groups rather than one, because tokenweir's rollup groups by
    `pricing_mode` and these rows disagree about it — which is hand-off note 2's
    "returns more rows than it used to", asserted rather than described.
    """
    apply(legacy_with_rows, baseline_effective_from=BASELINE)

    rollup = fetch(
        legacy_with_rows,
        "SELECT pricing_mode, calls, is_priced, est_cost_usd FROM gateway_usage_daily "
        "ORDER BY pricing_mode NULLS LAST",
    )
    assert [(r[0], r[1], r[2]) for r in rollup] == [("api", 1, True), (None, 1, True)]
    assert [float(r[3]) for r in rollup] == [0.0525, 0.0375]


def test_reconciling_a_reconciled_database_does_nothing(legacy_with_rows):
    """FR-021. The property that makes this safe to leave in a deploy script."""
    apply(legacy_with_rows, baseline_effective_from=BASELINE)
    after_first = _snapshot(legacy_with_rows, OWNED)

    second = plan(legacy_with_rows, baseline_effective_from=BASELINE)
    assert second.is_empty, second.render()

    assert apply(legacy_with_rows, baseline_effective_from=BASELINE).is_empty
    assert _snapshot(legacy_with_rows, OWNED) == after_first


def test_a_failure_part_way_through_leaves_the_database_alone(
    legacy_with_rows, monkeypatch
):
    """FR-018. The rate-card restructure is four statements; a database left
    between the second and the third has neither key working and reports itself
    reconciled next time nobody looks."""
    before = _snapshot(legacy_with_rows, OWNED)
    rows_before = fetch(legacy_with_rows, "SELECT * FROM gateway_usage ORDER BY id")

    real_plan = plan(legacy_with_rows, baseline_effective_from=BASELINE)
    assert len(real_plan.statements) > 3

    # Re-derivation inside `apply` is what runs, so the failure has to come from
    # the database rather than from a doctored plan: a statement that is valid to
    # parse and impossible to execute, injected after the real ones.
    import tokenweir.reconcile as reconcile_module

    original = reconcile_module.ReconciliationPlan.statements.fget
    monkeypatch.setattr(
        reconcile_module.ReconciliationPlan,
        "statements",
        property(lambda self: original(self) + ("SELECT 1 / 0",)),
    )

    with pytest.raises(Exception):
        apply(legacy_with_rows, baseline_effective_from=BASELINE)
    legacy_with_rows.rollback()

    assert _snapshot(legacy_with_rows, OWNED) == before
    assert fetch(legacy_with_rows, "SELECT * FROM gateway_usage ORDER BY id") == rows_before


def test_applying_a_plan_the_database_has_outgrown_is_refused(legacy_with_rows):
    """FR-020. A plan a human read ten minutes ago is not evidence about the
    database now."""
    stale = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    execute(
        legacy_with_rows,
        "ALTER TABLE gateway_usage ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1",
    )

    with pytest.raises(PlanStaleError, match="no longer matches"):
        apply(legacy_with_rows, stale)


def test_a_baseline_that_contradicts_the_plan_is_refused(legacy_with_rows):
    """Applying a plan nobody read, under a date nobody saw, is how a rate card
    quietly restates history."""
    read_by_a_human = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    with pytest.raises(ValueError, match="disagrees with the plan"):
        apply(legacy_with_rows, read_by_a_human, baseline_effective_from="2020-05-05")


# --- User story 3: refuse what a human has to decide --------------------------


def test_two_rates_for_one_model_cannot_be_re_keyed_and_says_which(legacy_with_rows):
    """FR-014, and the sharpest edge in the story.

    The old key is `(model, pricing_mode)`; the new one is `(model,
    effective_from)` and tokenweir's rate card has no `pricing_mode` at all. Two
    rows for one model collapse onto one key and one of them has nowhere to go.
    Which one survives is a decision about money.
    """
    execute(
        legacy_with_rows,
        "INSERT INTO model_pricing_rates "
        "(model, pricing_mode, input_cost_usd_per_mtok, output_cost_usd_per_mtok) "
        "VALUES ('claude-opus-5', 'subscription', 0, 0)",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    collision = find(result, "model_pricing_rates", "cannot be re-keyed")
    assert collision.resolution is Resolution.MANUAL
    assert "claude-opus-5" in collision.description
    assert collision.statements == ()


def test_a_solo_subscription_rate_cannot_silently_become_the_api_rate(legacy_with_rows):
    """The High the terminal review found: `_rate_card_duplicates` only fires on
    *more than one* row per model, so a model whose only row is
    `pricing_mode = 'subscription'` walked past it. tokenweir's rollup joins the
    rate card to usage by `model` alone — `pricing_mode` does not survive the
    restructure — so that lone row would become the model's only rate, and
    every API-metered call for it would price at a subscription rate (no
    per-call dollar by construction): a confidently wrong `est_cost_usd`, not
    the blank migration 005 returns for usage it cannot price.
    """
    execute(
        legacy_with_rows,
        "INSERT INTO model_pricing_rates "
        "(model, pricing_mode, input_cost_usd_per_mtok, output_cost_usd_per_mtok) "
        "VALUES ('claude-haiku-4-5', 'subscription', 0, 0)",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    collision = find(result, "model_pricing_rates", "cannot be re-keyed")
    assert collision.resolution is Resolution.MANUAL
    assert "claude-haiku-4-5" in collision.description
    assert collision.statements == ()

    # claude-opus-5's own row (from `legacy_with_rows`) is an ordinary,
    # non-subscription, duplicate-free rate and must not be implicated.
    assert "claude-opus-5" not in collision.description


def test_one_decision_outstanding_blocks_everything_including_the_easy_parts(
    legacy_with_rows,
):
    """FR-019 / SC-005. The failure mode of a reconciler is not stopping."""
    execute(
        legacy_with_rows,
        "INSERT INTO model_pricing_rates "
        "(model, pricing_mode, input_cost_usd_per_mtok, output_cost_usd_per_mtok) "
        "VALUES ('claude-opus-5', 'subscription', 0, 0)",
    )
    before = _snapshot(legacy_with_rows, OWNED)

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)
    assert result.automatic, "this test is worthless if there was nothing to skip"

    with pytest.raises(ManualResolutionError) as raised:
        apply(legacy_with_rows, baseline_effective_from=BASELINE)
    legacy_with_rows.rollback()

    assert "Nothing was applied" in str(raised.value)
    assert _snapshot(legacy_with_rows, OWNED) == before


def test_the_rate_card_will_not_be_re_dated_without_being_told_a_date(legacy_with_rows):
    """FR-013. There is no default, because both available defaults are claims
    about history that only the operator can make."""
    result = plan(legacy_with_rows)

    rate_card = find(result, "model_pricing_rates", "current-valued")
    assert rate_card.resolution is Resolution.MANUAL
    assert "--baseline-effective-from" in rate_card.remedy
    assert "-infinity" in rate_card.remedy

    with pytest.raises(ManualResolutionError):
        apply(legacy_with_rows)


def test_a_view_something_else_depends_on_is_not_replaced_silently(legacy_with_rows):
    """FR-015. `CREATE OR REPLACE VIEW` cannot change a column set, so the rollup
    has to be dropped and recreated — and dropping it takes the dependent with it
    under CASCADE, or fails without. Neither is this tool's call."""
    execute(
        legacy_with_rows,
        "CREATE VIEW gateway_usage_monthly AS "
        "SELECT app_id, model, SUM(calls) AS calls FROM gateway_usage_daily "
        "GROUP BY 1, 2",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    view = find(result, "gateway_usage_daily", "depend on it")
    assert view.resolution is Resolution.MANUAL
    assert "gateway_usage_monthly" in view.description
    assert "pricing_mode" in view.remedy  # hand-off note 2, where it is needed


def test_the_grants_the_view_drop_discards_are_named(
    legacy_with_rows, psycopg_module, postgres_dsn
):
    """Review 1's Med-4. `DROP VIEW` takes the view's ACL with it, and migration
    005 re-issues the grant only when `tokenweir.reader_role` is set — which
    reconcile never sets. A reporting role loses SELECT on the rollup with nothing
    said.

    Reported, not fixed: re-granting is one statement an operator can read, and
    guessing which roles *should* have access is not something this module knows.
    """
    role = f"tokenweir_reader_{uuid.uuid4().hex[:8]}"
    admin = psycopg_module.connect(postgres_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE ROLE "{role}"')
        execute(legacy_with_rows, f'GRANT SELECT ON gateway_usage_daily TO "{role}"')

        result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

        warned = [
            o
            for o in result.observations
            if o.relation == "gateway_usage_daily" and role in o.description
        ]
        assert len(warned) == 1, [o.description for o in result.observations]
        assert "re-granting" in warned[0].description
        assert "--reader-role does not reach this" in warned[0].description

        # And the loss it warns about is real, which is what makes the warning
        # worth having rather than defensive noise.
        apply(legacy_with_rows, baseline_effective_from=BASELINE)
        assert fetch(
            legacy_with_rows,
            "SELECT has_table_privilege(%s, 'gateway_usage_daily', 'SELECT')",
            (role,),
        ) == [(False,)]
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP OWNED BY "{role}"')
            cursor.execute(f'DROP ROLE IF EXISTS "{role}"')
        admin.close()


def test_the_grant_warning_is_about_this_view_and_not_a_namesake(
    legacy_with_rows, psycopg_module, postgres_dsn
):
    """Review 2's Med-2. The lookup ran `information_schema.role_table_grants WHERE
    table_name = %s` with no schema predicate, so a `gateway_usage_daily` in an
    unrelated schema put *its* grantees into this database's warning — naming roles
    that will lose nothing.

    `pg_class.relacl` by oid answers both halves: it is scoped to the relation
    actually resolved, and it is the ACL itself rather than a view of it filtered by
    which roles the caller happens to be a member of.
    """
    bystander = f"tokenweir_bystander_{uuid.uuid4().hex[:8]}"
    elsewhere = f"tokenweir_other_{uuid.uuid4().hex[:8]}"
    admin = psycopg_module.connect(postgres_dsn)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(f'CREATE ROLE "{bystander}"')
            cursor.execute(f'CREATE SCHEMA "{elsewhere}"')
            cursor.execute(f'CREATE TABLE "{elsewhere}".gateway_usage_daily (a INT)')
            cursor.execute(
                f'GRANT USAGE ON SCHEMA "{elsewhere}" TO "{bystander}"'
            )
            cursor.execute(
                f'GRANT SELECT ON "{elsewhere}".gateway_usage_daily TO "{bystander}"'
            )

        result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

        assert not any(
            bystander in o.description for o in result.observations
        ), [o.description for o in result.observations]
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{elsewhere}" CASCADE')
            cursor.execute(f'DROP OWNED BY "{bystander}"')
            cursor.execute(f'DROP ROLE IF EXISTS "{bystander}"')
        admin.close()


def test_the_rendered_plan_carries_the_whole_statement(legacy_with_rows):
    """Review 2's Med-3. Every statement went through `_one_line(…, limit=160)`, so
    migration 005's six thousand characters were printed as a hundred and sixty and
    an ellipsis — the one statement that drops and rebuilds a relation was the one
    an operator could not read.

    The plan-first design rests on the plan being readable before it is run. A
    renderer that elides the interesting statement makes "read the plan" advice
    nobody can take.
    """
    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)
    rendered = result.render()

    assert len(_view_migration_sql("gateway_usage_daily")) > 1000, (
        "this test is about a statement too long to squash"
    )
    # The parts an operator has to see to judge the rebuild: the grouping key that
    # changes the view's shape, and the guard that decides what stays unpriced.
    for fragment in ("BOOL_AND", "u.pricing_mode,", "est_cost_usd", "LEFT JOIN LATERAL"):
        assert fragment in rendered, f"the rendered plan elides {fragment!r}"

    # The general form, so this does not become a list of fragments somebody
    # remembered: every statement, whole, line for line.
    unindented = "\n".join(line.strip() for line in rendered.splitlines())
    for statement in result.statements:
        for line in statement.strip("\n").splitlines():
            assert line.strip() in unindented, f"the rendered plan elides {line.strip()!r}"

    # Descriptions are still summaries and may still be elided — the "…" in the
    # constraint's one-line description is one, and it is deliberate. What must not
    # be elided is the SQL, which is what the loop above pins.


def test_a_table_squatting_on_the_view_name_is_not_replaced(legacy):
    """It may hold rows, and replacing it would destroy them."""
    execute(legacy, "DROP VIEW gateway_usage_daily")
    execute(legacy, "CREATE TABLE gateway_usage_daily (whatever TEXT)")

    result = plan(legacy, baseline_effective_from=BASELINE)

    squatter = find(result, "gateway_usage_daily", "not a view")
    assert squatter.resolution is Resolution.MANUAL
    assert squatter.statements == ()


def test_a_narrowed_column_type_is_a_decision_not_a_conversion(legacy_with_rows):
    """FR-011. A widening may be safe; a narrowing truncates. Which this is
    depends on the data, and the tool does not look."""
    # `latency_ms` rather than a token count: the legacy rollup selects those, and
    # Postgres refuses to retype a column a view depends on. The narrowing being
    # tested is the same one either way — bigint down to integer, which truncates.
    execute(
        legacy_with_rows,
        "ALTER TABLE gateway_usage ALTER COLUMN latency_ms TYPE INTEGER",
    )

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    mismatch = find(result, "gateway_usage", "latency_ms is integer")
    assert mismatch.resolution is Resolution.MANUAL
    assert mismatch.statements == ()


def test_a_nullable_column_holding_nulls_is_a_decision(legacy_with_rows):
    """The data decides, so the data is consulted. `SET NOT NULL` on a column with
    NULLs in it fails; picking a value for those rows is not the tool's to do."""
    execute(legacy_with_rows, "ALTER TABLE gateway_usage ALTER COLUMN status DROP NOT NULL")
    execute(legacy_with_rows, "UPDATE gateway_usage SET status = NULL WHERE request_id = 'req-1'")

    result = plan(legacy_with_rows, baseline_effective_from=BASELINE)

    blocked = find(result, "gateway_usage", "status is nullable and holds NULLs")
    assert blocked.resolution is Resolution.MANUAL

    execute(legacy_with_rows, "UPDATE gateway_usage SET status = 'ok' WHERE status IS NULL")
    fixable = find(
        plan(legacy_with_rows, baseline_effective_from=BASELINE),
        "gateway_usage",
        "status is nullable",
    )
    assert fixable.resolution is Resolution.AUTOMATIC
    assert "SET NOT NULL" in fixable.statements[0]


# --- The baseline, without a database -----------------------------------------


@pytest.mark.parametrize(
    "given,expected",
    [
        ("2024-01-01", "2024-01-01"),
        ("  2024-01-01  ", "2024-01-01"),
        ("-infinity", "-infinity"),
        ("-INFINITY", "-infinity"),
        (None, None),
    ],
)
def test_the_baseline_is_normalised(given, expected):
    assert _normalise_baseline(given) == expected


@pytest.mark.parametrize("given", ["not-a-date", "2024-13-01", "01/01/2024", ""])
def test_a_baseline_that_is_not_a_date_is_refused(given):
    """A rate card back-filled to a date nobody meant is a silent restatement of
    every historical cost, so this is refused rather than coerced."""
    with pytest.raises(ValueError, match="ISO date"):
        _normalise_baseline(given)


@pytest.mark.parametrize("given", ["infinity", "INFINITY", " Infinity "])
def test_a_positive_infinity_baseline_is_refused_by_name(given):
    """`DATE 'infinity'` is valid SQL and is the semantic opposite of what the flag
    is for. Migration 005 joins a rate on `effective_from <= usage_day`, so a card
    dated at the end of time is in force on no day that has happened.

    Review 1 found this accepted, planned AUTOMATIC and applied: the run exited 0
    having left every pre-existing row `is_priced = false`, which is exactly what
    the story's third acceptance clause forbids. The parametrisation next to this
    one covered `-infinity` and `-INFINITY` and agreed with the code rather than
    with the spec.
    """
    with pytest.raises(ValueError, match="in force on no day"):
        _normalise_baseline(given)


def test_the_negative_infinity_baseline_prices_every_pre_existing_row(
    legacy_with_rows,
):
    """The value that *is* offered, doing what it is offered for.

    The counterpart to the test above, and the reason it is not enough to assert
    the refusal: 'these were always the rates' has to actually price the history,
    or the flag would be documented and useless.
    """
    apply(legacy_with_rows, baseline_effective_from="-infinity")

    rollup = fetch(
        legacy_with_rows,
        "SELECT is_priced, est_cost_usd FROM gateway_usage_daily "
        "ORDER BY pricing_mode NULLS LAST",
    )
    assert [r[0] for r in rollup] == [True, True]
    assert [float(r[1]) for r in rollup] == [0.0525, 0.0375]
