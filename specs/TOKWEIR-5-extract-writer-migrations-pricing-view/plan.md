# Implementation Plan: Extract the writer, migrations and pricing view

**Branch**: `TOKWEIR-5-extract-writer-migrations-pricing-view` | **Spec**: [spec.md](./spec.md)
**Jira**: TOKWEIR-5

## Summary

Add two modules to `tokenweir` — `tokenweir.migrations` (six SQL files shipped as package data plus a
runner that applies them) and `tokenweir.postgres` (a `PostgresSource` implementing the existing
`Source` protocol) — so that schema ownership moves here per ADR-0001 Pillar 5 and a consumer can
persist records without owning DDL. Nothing on the emit side, the contract, or the published JSON
Schema changes.

## Technical Context

**Language**: Python 3.11+ (pod runs 3.12.3). **Store**: PostgreSQL.
**Runtime deps**: none added to the core. `psycopg[binary]>=3` becomes the `postgres` extra.
**Testing**: pytest. Real-Postgres correctness tests, DSN-gated; SQL-text and pure-function tests
unconditional. **Lint**: ruff, `line-length = 100`, `select = ["E","F","I","N","W"]`.
**Authoritative test command** (`/workspace/.mado/project.yaml`): `/workspace/repo/.venv/bin/pytest`
from the repo root with `CI=true`; exit codes 0 and 5 pass.

### The two environment facts that shape the design

1. **No `ai-gateway` checkout.** The DDL is reconstructed from ADR-0001 and from
   `tokenweir.contract`'s v1 field set. A test pins the table's columns to the contract's fields so
   the two cannot drift apart unnoticed.
2. **No usable Postgres.** Not installed, not in apt, no root, no Docker; the data VM's 5432 is
   production and off-limits. Everything that can be verified without a server is, and the rest is
   gated behind `TOKENWEIR_TEST_DSN` and skips.

Fact 2 is what makes the SQL-text tests load-bearing rather than decorative: they are the only
mechanism by which the preserved fixes are enforced in the environment the project actually tests in.

## Design decisions

### Connection in, not DSN in

Both the runner and the writer take a **DB-API connection**. That is what lets every behaviour except
the SQL semantics be tested with no driver installed, keeps `psycopg` out of the import graph
(Pillar 2), and leaves pooling and credential handling to the deployment. A `connect(dsn)` helper
exists for the CLI and for callers who want the easy path; it is the only place psycopg is imported,
and its `ImportError` names the extra.

### Migrations as package data

`importlib.resources.files("tokenweir.migrations") / "sql"` — not `Path(__file__)` — so discovery
works from a wheel or a zip. `[tool.setuptools.package-data]` carries the `.sql` files into the
distribution. This is deliberately the opposite treatment from `schema/usage-record.v1.json`, which
README documents as repo-only: a consumer *reads* the JSON Schema, but the library itself must *apply*
the migrations.

### One transaction per migration, advisory lock around the run

Each migration's DDL and its `schema_migrations` insert commit together, so "recorded as applied" and
"actually applied" cannot disagree. A session advisory lock (`pg_advisory_lock`) is taken for the whole
run so two deploys racing do not both try to create the same objects. The lock is Postgres-specific and
acquired through a hook that a non-Postgres connection can decline — the fallback is documented rather
than silent.

### The destructive-statement guard

Comments are stripped (`--` to end of line, `/* … */` including nested-looking cases), then whole-word
matching for `DROP`, `TRUNCATE`, `DELETE FROM`. Word boundaries mean `drop_reason` does not match;
stripping comments means a rationale that mentions dropping does not match. `allow_destructive=True` is
a per-call argument, never a setting, so the opt-in appears at the call site a reviewer reads.

### The rollup

```sql
(u.ts AT TIME ZONE INTERVAL '0')::DATE           -- IMMUTABLE; indexable; same text in view and index
BOOL_AND(rate.model IS NOT NULL AND u.pricing_mode IS DISTINCT FROM 'subscription')  -- is_priced
CASE WHEN <is_priced> THEN SUM(...) ELSE NULL END -- est_cost_usd
```

with the rate resolved by a `LEFT JOIN LATERAL` picking the entry with the greatest `effective_from`
not after the usage day, and `GROUP BY app_id, usage_day, model, pricing_mode`. The `pricing_mode` in
the grouping is the documented divergence from the gateway's view (spec Assumptions).

Rate columns are named `input_cost_usd_per_mtok` and friends. The unit is in the name because a rate
card loaded in the wrong unit is otherwise a silent thousand-fold error in a dollar figure.

### The reader grant

`current_setting('tokenweir.reader_role', true)` inside a `DO` block: the runner issues
`SET LOCAL tokenweir.reader_role = …` when a role is configured, and the block no-ops with a `RAISE
NOTICE` when the setting is empty or the role does not exist. Keeps the SQL plain, keeps the library
free of homelab role names, and keeps the migration idempotent.

## Project Structure

```
src/tokenweir/
├── contract.py                        (unchanged)
├── sink.py                            (unchanged)
├── source.py                          (unchanged — PostgresSource implements this protocol)
├── postgres.py                        NEW  PostgresSource, connect(), INSERT_SQL, row_for()
└── migrations/                        NEW
    ├── __init__.py                         Migration, discover(), apply(), status, guards
    ├── __main__.py                         `python -m tokenweir.migrations apply|status`
    └── sql/
        ├── 001_gateway_usage.sql
        ├── 002_parent_request_id.sql
        ├── 003_model_pricing_rates.sql
        ├── 004_reader_grant.sql
        ├── 005_gateway_usage_daily.sql
        └── 006_gateway_usage_app_day_index.sql

tests/
├── conftest.py                        NEW  DSN gating, schema-per-run isolation
├── test_migrations.py                 NEW  runner semantics (fake connection, no driver)
├── test_migration_sql.py              NEW  the preserved fixes, as text assertions
├── test_postgres_source.py            NEW  mapping + statement + protocol (no driver)
└── test_postgres_integration.py       NEW  real Postgres; skips without a DSN

pyproject.toml                         MODIFIED  postgres extra, package-data, dev extra
README.md                              MODIFIED  the store half
```

## Phases

**Phase 1 — Schema.** Write the six SQL files. Nothing else can be tested until the text exists.

**Phase 2 — SQL guards.** `test_migration_sql.py`. Written against the files immediately, because
these are the tests that hold the story's "keep the fixes" clause and they must fail for the right
reason before anything depends on them.

**Phase 3 — Runner.** `tokenweir/migrations/__init__.py` and its CLI, then `test_migrations.py`
driving it through a recording fake connection.

**Phase 4 — Writer.** `tokenweir/postgres.py`, then `test_postgres_source.py`.

**Phase 5 — Real-Postgres suite.** `conftest.py` and `test_postgres_integration.py`. Verified to
*skip* cleanly here; the developer runs them against a scratch database.

**Phase 6 — Packaging and docs.** `pyproject.toml` extras and package data, `README.md`, and a check
that the SQL is reachable through `importlib.resources`.

## Testing strategy, and its honest limits

| Layer | Runs in this pod? | Covers |
|---|---|---|
| SQL text assertions | Yes | FR-024, FR-025, FR-026, FR-028, FR-010, FR-006 — the preserved fixes |
| Runner semantics (fake connection) | Yes | ordering, idempotence, one-transaction-per-migration, unknown-version detection, the destructive guard |
| Writer mapping and statement | Yes | row mapping, `ts` handling, empty batch, pre-flight validation |
| Import hygiene (subprocess) | Yes | no driver imported at module import |
| **Real-Postgres correctness** | **No — skipped** | the rollup's arithmetic, `BOOL_AND`, effective-dating, transactional rollback, concurrent apply |

The fake connection is a **protocol** double: it pins the order and grouping of statements, never the
behaviour of Postgres. Database *correctness* is asserted only against a real server, which is the
project's established pattern and is why the last row exists at all rather than being replaced with a
mock. The consequence — that the last row is unproven until the developer runs it — is stated in the
spec's acceptance-clause table and in the run report, not buried here.

## Risks

| Risk | Mitigation |
|---|---|
| Reconstructed DDL differs from the live gateway schema | Column set pinned to the contract by test; TOKWEIR-10 gets an explicit "diff against live before pointing the gateway at it" step; called out in the report |
| The rollup's shape change breaks gateway queries | Documented as a deliberate divergence with its reason; flagged for TOKWEIR-10 |
| Real-Postgres suite never actually run | Skip message names the variable; README carries the command; the run report says plainly that the clause is unverified |
| A future edit reintroduces a fixed bug | That is exactly what Phase 2 prevents, in an environment with no database |
