# Implementation Plan: Reconcile a live gateway database with tokenweir's owned schema

**Branch**: `TOKWEIR-10-reconcile-live-gateway-schema` | **Date**: 2026-08-23 | **Spec**: [spec.md](./spec.md)
**Jira**: TOKWEIR-10

## Summary

Add `tokenweir.reconcile` — a plan-then-apply path that brings a database the AI Gateway already
owns up to the schema tokenweir's migrations define, without losing a row and without guessing.
It computes its target by **running the shipped migrations into a rolled-back scratch schema and
introspecting the result**, so there is no second description of the schema to rot. It classifies
every difference as something it can do safely or something a human must decide, and it refuses to
apply anything at all while one of the latter is outstanding.

Plus the two honesty fixes the story implies: the adopted-versions warning names the remedy, and
the README stops telling operators that adoption is the end of the job.

Out of reach on this branch, and recorded as such in spec.md § Scope: everything in the
`ai-gateway` repository, which is not checked out in this pod.

## Technical Context

**Language/Version**: Python ≥ 3.11 (`requires-python` in `pyproject.toml`)
**Primary Dependencies**: none added. The core stays dependency-light (ADR-0001 Pillar 2);
`tokenweir.reconcile` imports no driver at module level, exactly as `tokenweir.migrations` and
`tokenweir.postgres` do not.
**Storage**: PostgreSQL. Verified against the embedded PostgreSQL `pgserver` starts for the suite.
**Testing**: `pytest`. Authoritative command from `/workspace/.mado/project.yaml`:
`CI=true /workspace/repo/.venv/bin/pytest`, over a venv built by `pip install -e . pytest`.
The real-Postgres additions gate on the existing `conftest.py` fixtures and skip under that
install, like every other store test.
**Target Platform**: a library. The consumer is a deploy step or an operator at a shell.
**Project Type**: single Python package, `src/` layout.
**Performance Goals**: none stated. The reference build runs six short DDL scripts; the diff is a
handful of catalog queries.
**Constraints**: no data loss, ever; one transaction for the apply; plan-by-default; no second
source of truth for the schema.
**Scale/Scope**: one live database today (`ai_gateway_metrics`), one new module, one new CLI
subcommand, one new test file.

## Constitution Check

`.specify/memory/constitution.md` in this repository is **the unfilled template** — every principle
is a `[PRINCIPLE_N_NAME]` placeholder. There is no ratified constitution to check against, and
inventing one to satisfy this gate would be worse than saying so.

The constraints this repository actually holds itself to are written down elsewhere, and this plan
is checked against them instead:

| Source | Constraint | How this plan meets it |
|---|---|---|
| ADR-0001 Pillar 2 | the core carries no transport or driver dependency | `tokenweir.reconcile` imports `psycopg` nowhere; it takes a DB-API connection, like `migrations.apply` |
| ADR-0001 Pillar 5 | tokenweir owns the schema | the reconciler's target is derived from the shipped migrations and nowhere else (FR-004) |
| `README.md` "the rules the extraction preserved" | forward-only, no `DROP` without operator review, idempotent, atomic | reconciliation is not a migration and does not touch the forward set; `--apply` is the operator review; re-running is a no-op; one transaction |
| `tests/conftest.py` | correctness against a real Postgres, never a mocked one | every behavioural claim here is asserted against a server; the no-server case skips |
| `tests/conftest.py` `OPTIONAL_DRIVERS` | a run must say what it did not prove | new tests gate through the existing `psycopg` entry, whose claim already covers "the migrations were not exercised against a server" |

**Complexity flag, raised deliberately**: the reference-schema-by-rollback technique (FR-004) is
the most surprising thing in this plan, and it is doing real work — see spec.md § Where the target
schema comes from. It was verified against a real server before this plan was written: the scratch
schema does not survive the rollback, `CREATE OR REPLACE VIEW` genuinely cannot reshape a view's
columns, and `pg_depend` genuinely reports dependent views. Those three facts are what the design
rests on, and none of them is taken on trust.

## Project Structure

### Documentation (this feature)

```
specs/TOKWEIR-10-reconcile-live-gateway-schema/
├── spec.md      # what and why
├── plan.md      # this file
└── tasks.md     # the ordered work
```

### Source Code (repository root)

```
src/tokenweir/
├── migrations/
│   ├── __init__.py      # MODIFIED — FR-025, the adopted-versions warning names the remedy
│   ├── __main__.py      # MODIFIED — FR-022/023/024, the `reconcile` subcommand
│   └── sql/             # UNTOUCHED — the forward set is not where this belongs (spec.md)
└── reconcile.py         # NEW — the whole feature

tests/
└── test_reconcile.py    # NEW — real-Postgres, gated by the existing fixtures

README.md                # MODIFIED — FR-026, SC-006
```

`reconcile.py` sits beside `migrations/` rather than inside it. Inside, it would read as part of
the forward-migration machinery, which is precisely the confusion spec.md spends four paragraphs
dispelling. The CLI subcommand lives in `migrations/__main__.py` anyway, because that is the
command an operator already has in their deploy notes and a second entry point would be a worse
answer than an odd import.

## Design

### The four pieces

**1. Introspection — `_snapshot(connection, schema)`.** One function, used for both sides of the
diff, which is what makes the comparison meaningful: the live schema and the reference schema are
described by the same code, so a difference in the output is a difference in the database.

It reads `pg_attribute`/`pg_attrdef` (not `information_schema.columns`) because
`format_type(atttypid, atttypmod)` gives `numeric(18,10)` where `information_schema` gives
`numeric` — and a rate column silently reconciled to the wrong precision is a money bug. It also
reads `attidentity`, which is how the story's `BIGSERIAL` → `GENERATED ALWAYS AS IDENTITY`
difference is visible at all (an identity column has no `column_default`).

Captured per relation: kind (table/view), and per column: name, `format_type`, `attnotnull`,
default expression, identity. Per table: the primary-key column list and the index definitions
(`pg_get_indexdef`, with the schema qualifier normalised away so two schemas compare equal).

**2. The reference — `reference_snapshot(connection)`.** Creates `tokenweir_ref_<uuid4[:12]>`,
`SET LOCAL search_path`, executes each `Migration.sql` from `discover()`, snapshots it, and
**rolls back**. Verified: the schema does not survive the rollback. Refuses autocommit (FR-005),
where the rollback would be a no-op and the scratch schema would be left behind.

The `search_path` is set with `SET LOCAL`, so it dies with the transaction and cannot leak into
the caller's session. The migrations' own `DO $$ … $$` grant blocks no-op without
`tokenweir.reader_role`, which is what we want: the reference is about shape, not permissions.

**3. The diff — `plan(connection, baseline_effective_from=None)`.** Walks the reference's relations
against the live ones and emits `Discrepancy` records. The rules are FR-009 … FR-017; the ordering
is execution order, because `apply` just runs the statements in sequence.

The two structural cases get their own handling rather than falling out of a generic column diff:

- *`schema_version` missing* (FR-012) is a generic missing-column case except for the backfill.
  Generic missing-column resolution is: `ADD COLUMN … [DEFAULT …]`, and where the reference says
  `NOT NULL` and the reference has no default, `ADD COLUMN … DEFAULT <backfill> NOT NULL` followed
  by `ALTER COLUMN … DROP DEFAULT`. That produces exactly migration 001's column. The backfill value
  for `schema_version` is `1`, and it is the *only* column for which a backfill value is known;
  any other missing `NOT NULL`-without-default column is `MANUAL`, because inventing a value for
  existing rows is the thing this repository refuses to do everywhere else.
- *the rate card* (FR-013/FR-014) is not a column diff at all. It is detected by "the live
  `model_pricing_rates` has no `effective_from`", and resolved as: add the column with the
  baseline as default, `SET NOT NULL`, drop the default, drop the old primary key, add
  `(model, effective_from)`. Guarded first by a **data** query — `SELECT model FROM
  model_pricing_rates GROUP BY model HAVING COUNT(*) > 1` — whose non-empty result makes the whole
  thing `MANUAL` with the models named (FR-014).

The view (FR-015) compares only the column *set and order*, not the definition text: two SQL
strings that differ by whitespace produce the same view, and pinning the text would make this
brittle in the direction that cries wolf. A mismatch resolves to `DROP VIEW` + migration 005's
SQL, unless `pg_depend` reports a dependent, which makes it `MANUAL`.

**4. `apply(connection, plan)`.** Re-derives the plan (FR-020), compares it to the one handed in,
refuses on any `MANUAL` (FR-019), then executes every statement of every discrepancy in one
transaction and commits. Any exception rolls back the lot.

### What is deliberately *not* built

- **No repair of `schema_migrations` rows.** Adoption already handles that table (TOKWEIR-5) and
  re-writing another tool's history is not this tool's business. The reconciler makes the *objects*
  true; the rows already say those versions are applied, and after reconciliation they are right.
- **No `--force`, no partial apply, no "fix what you can".** FR-019 exists because the failure mode
  of a reconciler is finishing.
- **No dropping of anything the gateway owns.** FR-009 and FR-016. The one drop in the whole
  feature is the rollup *view*, which holds no data, and it is `MANUAL` the moment anything leans
  on it.
- **No conversion of `id` to an identity column.** The story says keep it (A-002). The snapshot
  *sees* the difference — it must, or it could not be excluded on purpose — and the diff exempts
  `gateway_usage.id` explicitly, with the story quoted at the exemption so it reads as a decision
  rather than an oversight.

### Testing strategy

`tests/test_reconcile.py`, against a real server through the existing `scratch_schema` fixture.
The load-bearing fixture is `legacy_gateway_schema`: a database built to the shape the story and
the hand-off comment describe — `gateway_usage` with `id BIGSERIAL` and **no** `schema_version`,
`model_pricing_rates` keyed `(model, pricing_mode)` with no `effective_from`, a
`schema_migrations` with no `checksum` recording versions 1–6, and a gateway-shaped
`gateway_usage_daily`.

That fixture is the reconstruction spec.md § "The one thing this branch cannot prove" is honest
about. It is written in one place, commented as a reconstruction, and every test that needs a
legacy database uses it — so when somebody finally has the dump, there is exactly one thing to
correct.

The pair of assertions that matter most, and that the spec makes SC-003:

```python
with pytest.raises(psycopg.errors.UndefinedColumn):
    PostgresSource(legacy_connection).write([record])     # before
...
assert PostgresSource(reconciled_connection).write([record]) == 1   # after
```

Asserting the *break* as well as the fix is what stops this test from passing on a database that
never had the problem.

## Complexity Tracking

| Thing | Why it is here | The simpler thing, and why it was rejected |
|---|---|---|
| Reference schema built by rollback | one source of truth for the schema (FR-004) | a Python description of the expected tables — a second source of truth, which this repo already refuses for the 005/006 shared expression |
| `pg_attribute` + `format_type` instead of `information_schema` | `numeric(18,10)` vs `numeric`; identity columns are invisible to `column_default` | `information_schema.columns` — loses exactly the two distinctions the story turns on |
| `AUTOMATIC` / `MANUAL` with no third class | FR-008 | a "probably fine" class; every discrepancy would drift into it |
| Re-deriving the plan inside `apply` | FR-020 | trusting the plan handed in — which was true when a human read it, not now |
