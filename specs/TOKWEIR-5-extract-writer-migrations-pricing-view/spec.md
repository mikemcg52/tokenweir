# Feature Specification: Extract the writer, migrations and pricing view from the AI Gateway

**Feature Branch**: `TOKWEIR-5-extract-writer-migrations-pricing-view`
**Created**: 2026-08-15
**Status**: Draft
**Jira**: TOKWEIR-5 (Story) — "Extract writer + migrations + pricing view from AI Gateway", child of
epic TOKWEIR-1
**Input** (fetched with `getJiraIssue` on 2026-08-15 and quoted verbatim, not reconstructed from the
branch name):

> Move the writer, `gateway_usage` schema + migrations (001–006), `schema_migrations` runner, and
> the daily rollup / pricing view into `tokenweir`. Ownership of the schema moves here; forward-only,
> no DROP-without-review preserved. Keep the real-Postgres test pattern and the `DATE_TRUNC`
> STABLE/IMMUTABLE and `is_priced`/`BOOL_AND` fixes.
>
> **Acceptance:** tokenweir owns and applies the migrations; writer persists records to Postgres;
> existing correctness tests pass against real Postgres.

## Context

ADR-0001 Pillar 5 calls this "the highest-risk part of the extraction": ownership of `gateway_usage`
and its forward-only migrations moves out of the AI Gateway and into `tokenweir`, and afterwards the
gateway pins a version instead of owning DDL. TOKWEIR-4 landed the record contract and TOKWEIR-15 the
guarded emit seam; `source.py` has carried a `Source` protocol and a `MemorySource` since then, with
its docstring already promising "Postgres today, per the extracted usage-writer". This story is the
promise being kept.

The write side is deliberately unlike the emit side. `Sink.emit` must never raise, because it sits on
the metered request's critical path. `Source.write` **may** raise, because it does not — it runs in a
consumer, off the request path, where durability matters more than latency and a caller that can
retry is better served by a loud failure than by a swallowed one. That asymmetry is already written
into `source.py` and this story implements against it rather than revisiting it.

### What is in this repository, and what is not

Two facts about the environment shape everything below. Both are stated here rather than discovered
by a reader halfway down.

| Thing the story implies | Reality in this pod | Consequence |
|---|---|---|
| An `ai-gateway` checkout whose files are *moved* | **Absent.** The gateway lives in a separate repository on the developer's workstation; this pod has only `tokenweir` (`git remote -v` → `mikemcg52/tokenweir`). | The DDL and writer are **reconstructed from the authoritative written descriptions** — ADR-0001 (which names the pipeline, the ownership move and the two fixes by name) and the v1 record contract in `tokenweir.contract`, which is the in-repo definition of what a record *is*. See Assumptions. |
| A Postgres to test against | **None reachable that may be used.** No server is installed, `postgresql` is not in the pod's apt sources, the account is not root, and there is no Docker. Port 5432 on the data VM answers, but that is the **production** `ai_gateway_metrics` database and this run has neither credentials for it nor any business writing to it. | Real-Postgres tests are **DSN-gated and skip** when no DSN is configured. The acceptance clause "existing correctness tests pass against real Postgres" is therefore *deliverable but not verifiable here*; see Acceptance-clause traceability. |

Neither fact is a reason to skip the story. What it changes is which claims this run may make, and
those are stated as claims rather than assumed.

### Acceptance-clause traceability

The story's acceptance has three clauses. Stated separately so that closing TOKWEIR-5 is a decision
someone makes on the record rather than one that happens by the story scrolling off a board.

| Clause (verbatim from the Jira story) | Status on this branch |
|---|---|
| "tokenweir owns and applies the migrations" | **Met.** The six migrations ship *inside the package* (`tokenweir/migrations/sql/`, wheel-installed, not repo-only), and `tokenweir.migrations.apply()` plus `python -m tokenweir.migrations` apply them and record them in `schema_migrations`. |
| "writer persists records to Postgres" | **Met in code; proven against a real Postgres only where a DSN is configured.** `tokenweir.postgres.PostgresSource` implements `Source` and INSERTs a batch in one transaction. Its SQL and row mapping are pure and fully tested here; the round-trip through a live server is the DSN-gated suite. |
| "existing correctness tests pass against real Postgres" | **Not verifiable in this pod, and not claimed.** There is no usable Postgres (see the table above). The correctness tests are written, are real-Postgres tests with no database mocking, and skip with a message naming the environment variable that turns them on. The developer runs them once against a scratch database; that step is documented in `README.md` and is the story's remaining verification. |

The two named fixes — `DATE_TRUNC` STABLE-vs-IMMUTABLE and `is_priced`/`BOOL_AND` — are a separate
matter, and they are **not** left to the unrunnable suite. Each is pinned by a test that reads the
shipped SQL, so a future edit that reintroduces either bug fails the suite in *this* pod, with no
database present. A preserved fix that only a machine somebody else owns can check is not preserved.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - tokenweir owns and applies the schema (Priority: P1)

An operator points `tokenweir` at an empty database and gets the full `gateway_usage` schema; running
it again changes nothing.

**Why this priority**: It is the ownership move Pillar 5 describes. Until the migrations live and run
here, the gateway still owns the schema and nothing has actually been extracted.

**Independent Test**: Apply the migrations to an empty database, then apply them again, and compare.

**Acceptance Scenarios**:

1. **Given** an empty database, **When** the migrations are applied, **Then** `gateway_usage`,
   `model_pricing_rates`, `gateway_usage_daily` and `schema_migrations` all exist.
2. **Given** an already-migrated database, **When** the migrations are applied again, **Then** nothing
   is applied, nothing errors, and `schema_migrations` is unchanged — idempotence is the property that
   makes running the migrator on every deploy safe.
3. **Given** a database migrated to version 003, **When** the migrator runs, **Then** exactly 004, 005
   and 006 are applied, in that order.
4. **Given** a migration that fails partway, **When** it is applied, **Then** neither its DDL nor its
   `schema_migrations` row survives — one transaction per migration, so a half-applied version can
   never be recorded as applied.
5. **Given** the package installed from a wheel with no repository present, **When** the migrations are
   discovered, **Then** all six are found — they are package data, not repository artifacts. This is
   the opposite of `schema/usage-record.v1.json`, which is deliberately repo-only, and the difference
   is that the library itself must be able to *apply* these.

---

### User Story 2 - The writer persists records to Postgres (Priority: P1)

A consumer hands a batch of `UsageRecord`s to a `Source` and they land in `gateway_usage`, or none of
them do.

**Why this priority**: The other half of the story's acceptance, and the reason the schema exists.

**Independent Test**: Write a batch, read the rows back, and compare against the records.

**Acceptance Scenarios**:

1. **Given** a batch of valid records, **When** it is written, **Then** every field of every record is
   in `gateway_usage` unchanged, and the count returned equals the number of rows.
2. **Given** a batch, **When** the write fails partway, **Then** no row from that batch is visible —
   one transaction per batch, which is what lets a consumer ack only after the commit.
3. **Given** a record with no `ts`, **When** it is written, **Then** the row's timestamp is the
   database's own `now()` rather than a NULL or a client clock reading.
4. **Given** a store failure (the server is down, the table is missing), **When** a write is attempted,
   **Then** `write` raises, because the caller is off the critical path and can retry. This is the
   deliberate opposite of `Sink.emit`, and both behaviours are correct for their side.
5. **Given** an empty batch, **When** it is written, **Then** the result is `0` and no statement is
   sent — a consumer whose 5-second timer fires with nothing buffered must not open a transaction.

---

### User Story 3 - The daily rollup prices honestly (Priority: P1)

An operator reads `gateway_usage_daily` and can tell the difference between "this cost $4.10" and "I
cannot tell you what this cost".

**Why this priority**: It is where the two named fixes live, and a rollup that quietly under-reports
cost is worse than one that reports nothing.

**Independent Test**: Load a rate card covering some models but not others, insert usage for both, and
read the view.

**Acceptance Scenarios**:

1. **Given** a group whose every call has a rate-card entry, **When** the view is read, **Then**
   `is_priced` is true and `est_cost_usd` is the summed cost.
2. **Given** a group where **any** call lacks a rate, **When** the view is read, **Then** `is_priced`
   is false and `est_cost_usd` is NULL for the whole group — `BOOL_AND`, not `BOOL_OR`. A partial
   number would be indistinguishable from a complete one and would be silently too low.
3. **Given** usage under `pricing_mode = 'subscription'`, **When** the view is read, **Then**
   `est_cost_usd` is NULL however complete the rate card is. Under a flat-rate subscription no
   per-call dollar exists (ADR-0001 Pillar 4); inventing one from an API rate card would be a
   fabricated number, which is worse than a blank.
4. **Given** rows spanning a day boundary in UTC, **When** they are grouped, **Then** the day is
   computed with an IMMUTABLE expression, so the same expression can be indexed.
5. **Given** a rate card with several entries for one model, **When** cost is computed, **Then** the
   entry in force on that day is used, not the newest one — repricing a model must not silently
   restate last month.

---

### User Story 4 - The preserved fixes cannot be un-fixed (Priority: P1)

A future contributor who writes `DATE_TRUNC('day', ts)` or `BOOL_OR(is_priced)` fails the suite.

**Why this priority**: P1, not P3. The story's wording is "keep the ... fixes", and a fix kept only as
a comment is a fix waiting to be reverted. This is also the only guard that runs in an environment
with no Postgres — which, per the Context table, is the environment this project's automated test run
actually has.

**Independent Test**: Read the shipped SQL and assert the properties, with no database involved.

**Acceptance Scenarios**:

1. **Given** the shipped SQL, **When** it is scanned, **Then** no day-bucketing expression uses
   `DATE_TRUNC` on the `ts` column, because it is STABLE rather than IMMUTABLE and cannot be indexed —
   without the fix a 30-day query sequential-scans and blows the 2-second SLA past a million rows.
2. **Given** the shipped SQL, **When** the rollup is scanned, **Then** the group-level priced flag is a
   `BOOL_AND`.
3. **Given** the shipped SQL, **When** it is scanned, **Then** no migration contains a destructive
   statement (`DROP`, `TRUNCATE`, `DELETE FROM`, `ALTER ... DROP`) — "no DROP without operator review"
   as a check rather than a convention.
4. **Given** the shipped SQL, **When** it is scanned, **Then** no column stores a cost. Cost is derived
   at report time from raw counts; a stored dollar goes stale the moment a rate card changes and there
   is then no way to tell which rows are stale.
5. **Given** the migration set, **When** it is listed, **Then** versions are zero-padded, contiguous
   from 001, and unique — a migrator that orders lexically is only correct if the names sort the way
   the numbers do.

---

### User Story 5 - Applying migrations does not require a Postgres driver at import time (Priority: P2)

`import tokenweir` still works in an environment with no database libraries at all.

**Why this priority**: P2 — nothing breaks today if it is missed, but ADR-0001 Pillar 2 makes the
dependency-light core a contract rather than a preference, and a top-level `import psycopg` would
break every consumer that only emits.

**Independent Test**: Import the package and the new modules in an interpreter with no psycopg, and
observe that only the call that actually connects fails.

**Acceptance Scenarios**:

1. **Given** an environment with no psycopg, **When** `tokenweir`, `tokenweir.migrations` and
   `tokenweir.postgres` are imported, **Then** all three succeed.
2. **Given** that environment, **When** the connection helper is called, **Then** it raises an error
   naming the extra to install, rather than an unexplained `ModuleNotFoundError`.
3. **Given** that environment, **When** a caller supplies its own DB-API connection, **Then** the
   writer and the migrator work — neither requires psycopg specifically, because both take a
   connection rather than a DSN.

### Edge Cases

- **A record whose `ts` is not a parsable timestamp.** The contract type-checks `ts` but does not
  parse it, so an unparsable string can reach the writer. The whole batch is validated *before* any
  statement is sent, and the write raises without opening a transaction — so a poison record fails
  cheaply and cannot half-write its batch. Making the consumer dead-letter it rather than requeue it
  forever is TOKWEIR-6's problem, and is flagged there rather than silently absorbed here.
- **A `ts` with an offset.** Stored as the instant it denotes; `TIMESTAMPTZ` normalizes, and the day
  bucket is computed in UTC so two producers in different zones agree on which day a call belongs to.
- **A record from a newer `schema_version`.** Stored with its own version, unknown fields dropped.
  The column set is v1's; a v2 field needs migration 007. The version column is what makes that
  detectable afterwards rather than a silent truncation.
- **Duplicate `request_id`.** Not rejected. The store is an append-only usage log, not a registry of
  requests, and `/compare` legitimately emits several records sharing a parent. A unique constraint
  here would turn a redelivery — which an at-least-once broker is *expected* to produce — into a
  poison message.
- **A rate card with no entry effective on the usage day.** Unpriced, so the group is unpriced. That
  is the `BOOL_AND` case and it is the point.
- **Applying migrations concurrently from two processes.** The migrator takes a session-level advisory
  lock, so the second waits and then finds nothing pending. Without it, two deploys racing produce
  either a duplicate-key error or a duplicate `CREATE`.
- **A reader role that does not exist.** The grant migrations no-op with a notice rather than failing.
  `tokenweir` is a library and cannot know a deployment's role names; a hardcoded `GRANT` to a role
  that exists only in the homelab would make the migration set unusable anywhere else.
- **An empty `ts` string** (`""`). Legal per the contract, which blank-checks identity fields only.
  Treated as absent, so the database default applies.

## Requirements *(mandatory)*

### Functional Requirements

**Ownership and the migration set**

- **FR-001**: `tokenweir` MUST ship migrations 001–006 defining `gateway_usage`, `parent_request_id`,
  `model_pricing_rates`, the reader grant, `gateway_usage_daily`, and the app/day index — the same six
  the AI Gateway owned, in the same order.
- **FR-002**: The migrations MUST be **package data**, discovered through `importlib.resources`, so a
  wheel install can apply them. They MUST NOT be repo-only artifacts.
- **FR-003**: The table MUST remain `gateway_usage` and the view `gateway_usage_daily`. Renaming would
  force a data migration on a live database, which is the exact risk Pillar 5 names; TOKWEIR-10's
  acceptance is "no regression in `gateway_usage` contents".
- **FR-004**: Migrations MUST be forward-only: no down-migrations, and no migration may be edited once
  released — a change is a new version.
- **FR-005**: Every migration MUST be individually idempotent (`IF NOT EXISTS` / `CREATE OR REPLACE`),
  so a database in an unexpected state is repairable rather than wedged.
- **FR-006**: Version numbers MUST be zero-padded, contiguous from `001`, and unique, so lexical order
  is numeric order.

**The runner**

- **FR-007**: The runner MUST record applied versions in a `schema_migrations` table it creates itself.
- **FR-008**: The runner MUST apply only pending migrations, in ascending version order.
- **FR-009**: Each migration and its `schema_migrations` row MUST be applied in **one transaction**, so
  a failure leaves neither behind.
- **FR-010**: The runner MUST refuse to apply SQL containing a destructive statement unless the caller
  explicitly opts in, and the opt-in MUST be per-call rather than a setting. This is
  "no DROP without operator review" made mechanical. Detection MUST ignore comments and MUST NOT fire
  on an identifier that merely contains the word (`drop_reason` is not a `DROP`).
- **FR-011**: The runner MUST take a **DB-API connection**, not a DSN, so it works with any driver and
  is testable without one. A DSN-based convenience MUST exist separately.
- **FR-012**: The runner MUST serialize concurrent runs with an advisory lock.
- **FR-013**: The runner MUST be usable as a command (`python -m tokenweir.migrations`) with at least
  `apply` and `status`, mirroring the `scripts/migrate.py` it replaces. Its connection options MUST be
  accepted on **either side of the subcommand** — `... status --dsn X` is the form anyone writes
  first, and argparse rejects it by default. A failure MUST be a message and a non-zero exit, never a
  traceback: the reader is an operator scanning a deploy log, not someone debugging this library.
- **FR-014**: The runner MUST report an applied version that is **not** in the shipped set rather than
  ignoring it — that means the database is ahead of the library, and applying anything on top of it
  blindly is how two consumers' schemas diverge.
- **FR-038**: The runner MUST record each migration's checksum and MUST refuse to run against a
  database whose already-applied migrations no longer match the shipped files. This is what makes
  FR-004's "never edited once released" a property of the system rather than a note: an edit means the
  database and the library disagree about what version N *is*, and re-running cannot resolve that.
  Reporting state (`status`) MUST be able to opt out, since being refused a *description* of a drifted
  database is the opposite of helpful.

**The writer**

- **FR-015**: `PostgresSource` MUST implement the existing `Source` protocol (`write`, `close`) without
  changing it.
- **FR-016**: `write` MUST persist a batch in one transaction and return the number of rows written.
- **FR-017**: `write` MUST raise on store failure. The write side is off the critical path and a
  swallowed failure there is silent data loss; this is the deliberate opposite of `Sink.emit`.
- **FR-018**: `write` MUST validate the whole batch and raise **before** opening a transaction if any
  record cannot be mapped to a row, so an unwritable record cannot half-write its batch.
- **FR-019**: An absent or blank `ts` MUST become the database's `now()`, not a client-side timestamp
  and not NULL.
- **FR-020**: `write` on an empty batch MUST return `0` without contacting the database.
- **FR-021**: The record→row mapping and the INSERT statement MUST be importable and testable without a
  database driver, since that is the only way they are covered in an environment with no Postgres.
- **FR-022**: Neither `tokenweir.postgres` nor `tokenweir.migrations` may import a database driver at
  module import time; the import MUST be deferred to the call that connects, and its failure MUST name
  the extra to install (ADR-0001 Pillar 2).
- **FR-023**: `psycopg` MUST be an optional extra (`tokenweir[postgres]`), never a core dependency.
- **FR-039**: The existing stdlib-only sweep in `test_contract.py` MUST cover the new subpackage, and
  MUST be scoped to **import time** — a driver imported inside the one function that connects costs a
  bare install nothing, and deferring is precisely how an optional extra is meant to be reached (the
  AMQP adapter will need the same). To stop "deferred" becoming a way to smuggle in a hard dependency
  that merely fails later, every deferred third-party import MUST correspond to a **declared optional
  extra** in `pyproject.toml`, and that check MUST itself be guarded against passing vacuously.

**The rollup and pricing**

- **FR-024**: The day bucket MUST use an IMMUTABLE expression — `(ts AT TIME ZONE INTERVAL '0')::DATE`
  — in both the view and the index, and MUST NOT use `DATE_TRUNC` on `ts`. The two must be the *same*
  expression or the index cannot serve the view.
- **FR-025**: The rollup's group-level priced flag MUST be `BOOL_AND` over the per-row flag, and
  `est_cost_usd` MUST be NULL for the whole group when it is false.
- **FR-026**: `est_cost_usd` MUST be NULL for `pricing_mode = 'subscription'` regardless of the rate
  card, per ADR-0001 Pillar 4.
- **FR-027**: The rate card MUST be effective-dated, and cost MUST use the entry in force on the usage
  day.
- **FR-028**: No cost may be stored on `gateway_usage`. Raw counts are stored; dollars are derived at
  report time.
- **FR-029**: The rate card's unit MUST be unambiguous in the **column name**, so a rate card loaded
  against the wrong unit is a visible mismatch rather than a thousand-fold silent error.
- **FR-030**: A migration creating a relation MUST grant the reader role `SELECT` on it, and the grant
  MUST no-op with a notice where the role is not configured or does not exist.

**Tests and documentation**

- **FR-031**: Correctness tests against Postgres MUST use a real Postgres — no mocked database — per
  the project's established pattern.
- **FR-032**: Those tests MUST **skip**, not fail or error, when no test DSN is configured, and the
  skip message MUST name the variable that enables them. An environment-dependent test that cannot be
  evaluated must not turn an ordinary install red; this repository already holds that line in
  `test_repo_hygiene.py` and the same rule applies here.
- **FR-033**: The tests MUST create and drop their own objects in a schema of their own and MUST NOT
  touch a database's existing `gateway_usage`.
- **FR-034**: Each preserved fix (FR-024, FR-025, FR-026, FR-028, FR-010, FR-006) MUST have a test that
  reads the shipped SQL and passes with **no database present**.
- **FR-040**: Where a Postgres *parser* is available without a Postgres *server* (`pglast`, which
  wraps the server's own `libpg_query`), the shipped SQL and its PL/pgSQL blocks MUST be syntax-checked
  by the suite. It is not a substitute for FR-031 — parsing says a statement is well-formed, not that
  it does the right thing — but a typo in DDL that no environment here can execute is otherwise found
  on somebody's deploy. It MUST be test-only and MUST skip when absent.
- **FR-041**: Confirming FR-002 means building a wheel, and setuptools leaves `build/` behind when you
  do. That output MUST be ignored by the repository, for the same reason TOKWEIR-12 gave for coverage
  artifacts, and the rule MUST be checked rather than merely written.
- **FR-035**: `README.md` MUST document applying the migrations, using the writer, reading the rollup,
  and how to run the real-Postgres tests. Its load-bearing warnings MUST be checked by **marker
  string** — not by wording, so the prose stays free to change and the check stays cheap.
- **FR-036**: The contract, its JSON Schema and `SCHEMA_VERSION` MUST be unchanged. This story adds a
  store; it does not touch the wire.
- **FR-037**: `tokenweir`'s existing public exports MUST be unchanged. The new modules are reached by
  their own import paths, mirroring how transport adapters are reached, so that importing the package
  never drags in store code.

### Key Entities

- **Migration**: a numbered, forward-only SQL file plus its recorded application. Identity is its
  version; content is immutable once released.
- **`schema_migrations`**: the record of which versions a database has. The migrator's only state.
- **`gateway_usage`**: the append-only usage log. One row per metered unit of work; raw counts only.
- **`model_pricing_rates`**: the effective-dated rate card, keyed by model and start date.
- **`gateway_usage_daily`**: the derived rollup — counts and tokens per app/day/model/pricing-mode,
  with a cost that is either complete or absent.
- **`PostgresSource`**: the writer. A `Source` that turns a batch of records into one transaction.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All six migrations are discovered from the **installed package**, with no repository
  present, and their versions are `001`–`006` in order.
- **SC-002**: Applying to an empty database creates all four relations; applying again applies nothing
  and leaves `schema_migrations` byte-identical.
- **SC-003**: A migration that raises leaves no trace — neither its objects nor its `schema_migrations`
  row.
- **SC-004**: SQL containing `DROP`, `TRUNCATE`, `DELETE FROM` or `ALTER ... DROP` is refused without
  the explicit opt-in and applied with it; SQL where the word appears only in a comment or inside an
  identifier is **not** refused.
- **SC-005**: A database carrying an applied version the library does not ship raises, naming the
  version.
- **SC-006**: Two migrators run concurrently against one database and produce one set of applied rows.
- **SC-007**: A written batch reads back field-for-field equal to the records, including `NULL`s and
  zeroes.
- **SC-008**: A batch that fails partway leaves zero rows visible.
- **SC-009**: A record with `ts=None` and one with `ts=""` both land with the server's `now()`, and one
  with an unparsable `ts` raises before any transaction is opened.
- **SC-010**: `write([])` returns `0` and issues no statement.
- **SC-011**: `gateway_usage_daily` returns a non-NULL `est_cost_usd` for a fully-priced group, NULL
  for a group with one unpriced call, and NULL for a `subscription` group with a full rate card.
- **SC-012**: A model repriced mid-month costs the earlier days at the earlier rate.
- **SC-013**: The day-bucket expression in the view and the one in the index are identical once the
  table qualifier is removed — an index cannot carry the view's alias, so that is what "identical" can
  mean here. Compared by extracting and normalizing both from the SQL, not by eye.
- **SC-021**: A database whose applied migrations no longer match the shipped files is refused, naming
  the migration; `status` can still describe it.
- **SC-022**: `--dsn` works before and after the subcommand, an explicit flag beats the environment,
  and neither position silently discards the other. A connect failure and a runner refusal each exit
  non-zero with a message and no traceback.
- **SC-023**: No module in the package imports a third-party distribution at import time, and every
  deferred third-party import names a distribution declared in a non-`dev` optional extra.
- **SC-024**: `build/` and `dist/` are ignored by the repository's own `.gitignore`.
- **SC-025**: Every shipped migration, and both PL/pgSQL blocks, parse against Postgres's grammar
  where `pglast` is installed.
- **SC-014**: `DATE_TRUNC` applied to `ts` appears nowhere in the shipped SQL, and the rollup's priced
  flag is a `BOOL_AND`. Both assertions run with no database.
- **SC-015**: No shipped migration contains a destructive statement, and none adds a cost-valued
  column. Both assertions run with no database.
- **SC-016**: A subprocess that imports `tokenweir`, `tokenweir.migrations` and `tokenweir.postgres`
  with no psycopg installed exits cleanly, and the connect helper's error names the extra.
- **SC-017**: The full suite passes via the authoritative command in `/workspace/.mado/project.yaml`
  (`/workspace/repo/.venv/bin/pytest`, `CI=true`, pass codes `0` and `5`), with every pre-existing test
  unchanged in count and result, and every Postgres-dependent test **skipped with a message naming the
  DSN variable** rather than failing.
- **SC-018**: `schema/usage-record.v1.json` is byte-identical to its current content and
  `SCHEMA_VERSION` is still `1`.
- **SC-019**: `ruff check .` is clean.
- **SC-020**: `python -m tokenweir.migrations status` and `apply` both run, and `status` against an
  unreachable database exits non-zero with a message rather than a traceback.

## Assumptions

- **The extraction is reconstructed, not copied — this is the load-bearing assumption of the story.**
  The `ai-gateway` repository is not present in this pod, so the DDL could not be moved file-by-file.
  It is derived from the two authoritative descriptions that *are* here: ADR-0001 (which names the
  pipeline, the six migrations, the ownership move and both fixes) and `tokenweir.contract`, which is
  the in-repo definition of a v1 record and therefore of what the table must hold. The column set is
  the contract's field set; that correspondence is asserted by a test, so a drift between the record
  and the table is caught here rather than at adoption.
  **What this cannot guarantee** is byte-level agreement with the DDL currently live on the data VM —
  column ordering, index names, the rate card's exact columns. TOKWEIR-10 must diff this schema against
  the live one before pointing the gateway at it, and that reconciliation is called out in the plan as
  a first-class step rather than left to be discovered. It is flagged in the run report too.
- **The rollup groups by `pricing_mode`, which the gateway's view may not have.** Required by FR-026:
  without it, one subscription record inside an otherwise API-metered group would either fabricate a
  dollar for flat-rate usage or blank out the cost of usage that genuinely has one. Both are wrong, and
  grouping is the only way neither happens. This is a deliberate, documented divergence, and it changes
  the view's shape — so TOKWEIR-10 must revisit `docs/usage-observability-queries.md` in the gateway
  repository. Recorded here so the decision is reviewable rather than inferred from a diff.
- **The AMQP consume loop is TOKWEIR-6's, not this story's.** The gateway's usage-writer was a service:
  a consumer *and* a persister. The persister is a `Source` and belongs here. The batch-of-100 /
  5-second timer, ack-after-commit, nack-and-requeue, the 1s→60s backoff and the ten-minute `[ALERT]`
  are consumer policy over a broker, and TOKWEIR-6 ("Emitter client + Sink/Source interfaces + AMQP
  adapter") owns the broker. Splitting them the other way would put `pika` behaviour in a story whose
  own ADR forbids `pika` in the core. What this story guarantees is the property that consumer needs:
  one transaction per batch, so ack-after-commit is *possible*.
- **The reader role is configuration, not a constant.** The gateway's `ai_gateway_metrics_reader` is a
  homelab role name; a library that hardcoded it would be unusable in the tenant deployments ADR-0001
  exists to enable. The runner takes the role name and the grant no-ops without it.
- **`schema_version` is stored per row.** A store owned by a versioned contract should be able to
  answer "which version wrote this", especially across the v1→v2 change the contract's own
  compatibility rules anticipate.
- **The speckit scaffold's `create-new-feature.sh` was not run.** It force-creates an `NNN-`-prefixed
  branch (verified: `--dry-run` here yields `001-extract-writer-migrations`), which would abandon the
  `<KEY>-<desc>` branch convention. The feature directory follows this repository's established
  `specs/<KEY>-<desc>/` layout, as TOKWEIR-4, TOKWEIR-12 and TOKWEIR-15 all did.
- **Test isolation is by schema, not by database.** The DSN-gated tests create a uniquely-named schema,
  work inside it, and drop it. A developer can therefore point them at any scratch database without
  the suite colliding with — or destroying — anything already in it.
