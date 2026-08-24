# Feature Specification: Reconcile a live gateway database with tokenweir's owned schema

**Feature Branch**: `TOKWEIR-10-reconcile-live-gateway-schema`, cut from `main` at `2d5ad4e`.

**Created**: 2026-08-23
**Status**: Draft
**Jira**: TOKWEIR-10 (Story), child of epic TOKWEIR-3, `Relates` to TOKWEIR-5
**Input**: The Jira story, fetched with `getJiraIssue` on 2026-08-23 and quoted verbatim below,
together with its single comment (the TOKWEIR-5 hand-off). Quoted in full because the reviewer
subagent has only intermittent Jira read access and no write access, and this text is the
acceptance the review grades fidelity against.

> Refactor the AI Gateway to depend on `tokenweir`: remove the embedded usage-writer and
> migrations, pin a tokenweir version, and emit via the tokenweir client (AMQP sink, homelab path
> unchanged). No regression in `gateway_usage` contents or the daily rollup.
>
> ## Database reconciliation — required, NOT point-and-run
>
> TOKWEIR-5 **redesigned** the schema rather than copying it, so tokenweir's migrations diverge
> from the live `ai_gateway_metrics` database and cannot simply be run against it. The migrations
> are `CREATE TABLE IF NOT EXISTS`, so pointed at the existing DB they **skip the live tables
> silently** and never apply the differences — leaving the writer and rollup broken with no error.
> (The live DB was also hand-created before AI Gateway's own migration runner existed, so it may
> not even match AI Gateway's `001` — the authoritative comparison is against a dump of the live
> database, not against either repo's migration files.)
>
> Known deltas (from diffing `tokenweir/src/tokenweir/migrations/sql/` against
> `ai-gateway/migrations/ai_gateway_metrics/`):
>
> * `gateway_usage` gains a `schema_version INTEGER NOT NULL` column the live table lacks; the
>   tokenweir writer emits it, so an unreconciled live table would reject the write. (Canonical
>   `id` also moves `BIGSERIAL` → `GENERATED ALWAYS AS IDENTITY`; keep the existing column on the
>   live table — only relevant if rows are ever rebuilt, where restoring old ids needs
>   `OVERRIDING SYSTEM VALUE` plus a sequence reset.)
> * `model_pricing_rates` is restructured from current-valued `PRIMARY KEY (model, pricing_mode)`
>   to effective-dated `(model, effective_from)`. Reconciling is real data migration — add
>   `effective_from`, change the PK, and backfill existing rows to a baseline effective date — not
>   a column add.
> * The live `schema_migrations` predates tokenweir's `checksum` column; the runner must **adopt**
>   the existing table rather than collide with it.
>
> ## Acceptance
>
> * Gateway runs on tokenweir; usage records and rollups match pre-refactor behavior; the gateway
>   repo no longer owns the schema.
> * A reconciliation migration path brings the live `ai_gateway_metrics` to tokenweir's owned
>   schema **with no data loss**, verified against a **copy/dump of the live database** before
>   production is touched.
> * After reconciliation, the tokenweir writer (emitting `schema_version`) and the effective-dated
>   daily rollup work correctly against the live data, and every pre-existing row is preserved and
>   still prices.

And the hand-off comment, from Mike McGonagle on 2026-08-17:

> Hand-off from TOKWEIR-5 (schema ownership moved into `tokenweir`). Three things that repo's spec
> folder knows and this issue did not — recorded here so they are not discovered during the
> refactor.
>
> **1. Diff the schema before pointing the gateway at it.** There was no `ai-gateway` checkout in
> the pod, so migrations 001–006 were _reconstructed_ from ADR-0001 and from `tokenweir.contract`'s
> v1 field set, not copied from the gateway's files. A test pins `gateway_usage`'s columns to the
> contract so the two cannot drift silently, but nothing has compared them to the live
> `ai_gateway_metrics` schema. Do that comparison first; a column the gateway has and tokenweir
> does not would be dropped on write with nothing to say so.
>
> **2. The rollup's shape changed:** `pricing_mode` is now a GROUP BY key in
> `gateway_usage_daily`. Deliberate — it stops flat-rate subscription usage from blanking out
> API-metered usage that genuinely has a cost — but it means any existing gateway query against
> that view returns more rows than it used to. Audit those queries as part of this story.
>
> **3. The runner adopts the gateway's existing** `schema_migrations`. It has no `checksum` column
> (that is tokenweir's addition), so on first run tokenweir adds the column, records the rows
> already there as applied, and reports their checksums as unverifiable rather than as drift.
> Nothing is re-applied or dropped. Two things follow:
>
> * `apply()` refuses a connection in **autocommit** mode. […]
> * `reader_role` only takes effect on the run that _applies_ 004/005. […]
>
> Verified against a real PostgreSQL 16 on the TOKWEIR-5 branch, including the concurrent-deploy
> and adoption paths.

---

## Scope — what this branch can and cannot deliver

**This is stated first because it is the most important thing about this spec, and because a
reviewer grading fidelity against the Jira text needs to know which clauses were reachable.**

The story spans **two repositories**. This stream pod contains exactly one:

```
/workspace/repo   → github.com/mikemcg52/tokenweir   (this repo)
```

There is **no `ai-gateway` checkout in the pod**, and no live `ai_gateway_metrics` database
reachable from it. That is the same environment fact the TOKWEIR-5 hand-off comment opens with —
it has not changed.

So the acceptance clauses split:

| Acceptance clause | Repo it lives in | This branch |
|---|---|---|
| Gateway pins tokenweir, drops its embedded writer and migrations, emits via the tokenweir client | `ai-gateway` | **Not deliverable here.** No checkout. |
| Gateway's operator queries against `gateway_usage_daily` audited for the new `pricing_mode` grouping (hand-off note 2) | `ai-gateway` | **Not deliverable here.** No checkout. |
| A reconciliation path brings a live gateway database to tokenweir's owned schema **with no data loss** | `tokenweir` | **Delivered.** |
| After reconciliation the writer (emitting `schema_version`) and the effective-dated rollup work against the live data, every pre-existing row preserved and still pricing | `tokenweir` | **Delivered**, proven against a real PostgreSQL over a database built to the legacy shape. |

The reconciliation is tokenweir's own work under any reading — tokenweir owns the schema
(ADR-0001 Pillar 5), so the tool that reconciles a database *to* that schema belongs beside the
migrations that define it. It is also the story's largest written section, and the one the story
insists is "required, NOT point-and-run". That is what this branch builds.

**What the gateway-side half will need from this branch when it is done**: a command it can run,
a plan it can read before running it, and a refusal it can trust when the reconciliation needs a
human. Those are FR-001 … FR-024 below.

### The one thing this branch cannot prove, said out loud

The story's second acceptance clause ends "**verified against a copy/dump of the live
database**". No such dump is reachable from this pod. What is verified here is the reconciliation
against a database **built to the legacy shape described by the story and the hand-off comment** —
which is a reconstruction, exactly as tokenweir's own migrations 001–006 were a reconstruction.

That is a real gap and it is not closed by anything on this branch. It is *narrowed*, deliberately,
by the central design decision below: the tool **introspects the database in front of it** rather
than assuming a shape. A reconstruction that guessed wrong about the live schema would produce a
tool that breaks on the real thing; a tool that reads the real thing and refuses what it does not
recognise does not. The residual is recorded as SC-006 and in `README.md`, so an operator running
this against production knows the dump comparison is still theirs to do.

## Context

### The failure this exists to prevent

Point tokenweir's migrator at the live `ai_gateway_metrics` today and it **succeeds, changes
nothing, and leaves a broken database**. Three mechanisms compound:

1. The live `schema_migrations` already records versions 1…6 (the gateway's). `_ensure_state_table`
   adopts that table by design (TOKWEIR-5, FR-038/FR-042) and `applied_versions` reports
   `{1: None, …, 6: None}`. Every shipped migration is therefore *already applied* and `apply()`
   returns `()`.
2. Even without the adoption, every shipped migration is `CREATE TABLE IF NOT EXISTS` /
   `CREATE INDEX IF NOT EXISTS` / `CREATE OR REPLACE VIEW`. Against existing objects they are
   no-ops by construction — that idempotence is a feature everywhere except here.
3. Nothing anywhere compares what `schema_migrations` *claims* is applied against what the
   database actually *contains*. Adoption takes the gateway's rows at their word.

The result is a database that reports itself fully migrated and:

* rejects **every** write from `PostgresSource`, because `tokenweir/postgres.py` names
  `schema_version` in its INSERT column list (`COLUMNS`, line 56) and the live `gateway_usage`
  has no such column — `UndefinedColumn` on the first batch;
* carries the gateway's old current-valued rate card, so `gateway_usage_daily` — if it is
  tokenweir's at all, which it is not, since 005 was skipped — cannot do its effective-dated
  lookup;
* answers `python -m tokenweir.migrations status` with `pending: (none)`.

"Broken with no error" is the story's phrase for it, and it is accurate.

### Why this is not migration `007`

The obvious shape — one more forward migration — is wrong here, for four independent reasons, and
each of them would have to be defeated for it to be right:

1. **A fresh database does not need it.** Migrations are forward-only and never re-run; a `007`
   that reconciles gateway-shaped objects is a permanent no-op on every database that ran
   001–006. Dead weight in the shipped set forever.
2. **It cannot be applied to the database that needs it.** The live database records 1…6 as
   applied and adoption honours that, so `apply()` would reach `007` — but `007` would be the
   *only* thing that ran, against a database whose 001–006 objects were never actually created by
   tokenweir. A reconciliation is not a migration on top of the six; it is what makes the six
   true.
3. **It would break the ordinary deploy.** Swapping `model_pricing_rates`' primary key needs
   `ALTER TABLE … DROP CONSTRAINT`, and `destructive_statements()` matches `\bDROP\b`. Every
   routine `python -m tokenweir.migrations apply` on every deployment would start demanding
   `--allow-destructive`, which is exactly the flag whose meaning is "an operator reviewed this
   specific data loss". Spending it on the common path destroys it.
4. **It needs an argument a migration cannot take.** Backfilling `effective_from` requires a
   baseline date. There is no correct default (see FR-013); a `.sql` file cannot ask.

So reconciliation is a **separate, operator-invoked path**: `tokenweir.reconcile`, plus a
`reconcile` subcommand on the existing CLI. It plans by default and applies only when told to,
which is what "NOT point-and-run" means as behaviour rather than as a warning in a ticket.

### Where the target schema comes from

The tool needs to know what tokenweir's schema *is* in order to say how a database differs from
it. Two ways to know that, and the choice is load-bearing:

* **Write it down** — a Python description of the expected tables, columns, indexes and view.
  Rejected: it is a second source of truth for the schema, it rots the first time somebody edits a
  `.sql` file, and this repository has an explicit habit of refusing exactly that (005 and 006
  share an expression and a test compares them rather than trusting a convention).
* **Build it and look at it** — create a scratch schema on the connection in front of us, execute
  the shipped migration SQL into it, introspect the result, and **roll the whole thing back**.

The second is what FR-004 requires. Postgres DDL is transactional, so the reference schema exists
only inside an aborted transaction and leaves nothing behind — not even on a crash. It cannot
drift from the migrations because it *is* the migrations, and it is the same technique
`tests/conftest.py` already uses to give the suite a schema of its own.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Tell me what is wrong with this database (Priority: P1)

An operator about to point the gateway at tokenweir runs the reconciler against a **restored dump**
of `ai_gateway_metrics` and is shown, before anything is changed, every way that database differs
from the schema tokenweir owns — each difference labelled with whether the tool can fix it or a
human must.

**Why this priority**: it is the whole of "NOT point-and-run", it is the step the story says to do
*first*, and it is useful on its own — an operator who never runs `--apply` still learns what the
silent-skip was hiding. It is also the only part that can be run against production safely, being
read-only.

**Independent Test**: build a database to the legacy gateway shape, run `reconcile` with no
`--apply`, and assert the plan names the missing `schema_version` column, the current-valued rate
card, and the view mismatch — and that the database is byte-for-byte unchanged afterwards.

**Acceptance Scenarios**:

1. **Given** a database with the legacy `gateway_usage` (no `schema_version`), **When** the
   operator runs `reconcile`, **Then** the plan lists a resolvable discrepancy naming the column,
   the exact SQL that would add it, and the fact that existing rows will be backfilled to
   `schema_version = 1`.
2. **Given** a database already reconciled or freshly migrated by tokenweir, **When** the operator
   runs `reconcile`, **Then** the plan is empty and the command says so in one line.
3. **Given** any database at all, **When** `reconcile` runs without `--apply`, **Then** no
   committed statement has changed it — the reference schema and every probe are rolled back.
4. **Given** a database whose `gateway_usage` carries a column tokenweir does not know about,
   **When** the plan is produced, **Then** that column is reported and **never** proposed for
   removal — and if it is `NOT NULL` with no default, the discrepancy is marked as one a human
   must resolve, because the tokenweir writer's INSERT would fail on it.

### User Story 2 - Bring the database to tokenweir's schema without losing a row (Priority: P1)

The same operator, having read the plan, runs it with `--apply`. Every discrepancy the tool called
resolvable is resolved, in **one transaction**, and every pre-existing row survives with its values
intact and still prices through the rollup.

**Why this priority**: it is the story's second and third acceptance clauses. P1 alongside story 1
rather than below it because a plan nobody can execute does not deliver the story.

**Independent Test**: build the legacy database, insert usage rows and rate-card rows, apply, then
assert (a) row-for-row equality of the pre-existing usage data before and after, (b) the schema now
matches a tokenweir-native one under the same introspection the diff uses, (c) `PostgresSource`
can write, and (d) `gateway_usage_daily` prices the old rows.

**Acceptance Scenarios**:

1. **Given** a legacy database holding usage rows, **When** `--apply` runs, **Then** every row is
   still present with every original column value unchanged, and `schema_version` is `1` on all of
   them.
2. **Given** the reconciled database, **When** `PostgresSource.write` is called with a batch,
   **Then** it succeeds — the same call that raised `UndefinedColumn` before.
3. **Given** the reconciled database and a rate card backfilled to the operator's baseline date,
   **When** `gateway_usage_daily` is read, **Then** the pre-existing rows price, and they price to
   the same figures the gateway's current-valued card would have produced for them.
4. **Given** a reconciliation that has already run, **When** it runs again, **Then** the plan is
   empty and nothing is executed — idempotent, like everything else in this package.
5. **Given** a failure part-way through applying, **When** the transaction aborts, **Then** the
   database is exactly as it was — no half-reconciled schema that reports itself done.

### User Story 3 - Refuse what a human has to decide (Priority: P2)

Some differences cannot be resolved without a decision that is not the tool's to make. The tool
names them, refuses to apply **anything** while one is outstanding, and says what the operator must
do.

**Why this priority**: P2 because stories 1 and 2 deliver the story's acceptance; but this is what
makes the delivered thing safe to point at production, and it is the concrete form of "no data
loss" — the failure mode of a reconciler is not that it stops, it is that it carries on.

**Independent Test**: build a legacy rate card holding two rows for one model (`api` and
`subscription` pricing modes), run `--apply`, and assert it refuses, names the model, and changed
nothing.

**Acceptance Scenarios**:

1. **Given** a legacy `model_pricing_rates` with two rows for the same model under different
   `pricing_mode` values, **When** reconciliation is planned, **Then** it reports that collapsing
   them to the effective-dated key `(model, effective_from)` would collide, names the models, and
   marks the discrepancy as one only a human can resolve. tokenweir's rate card has no
   `pricing_mode`: one of those rows has nowhere to go, and choosing which is not a default.
2. **Given** a plan containing any human-only discrepancy, **When** `--apply` is passed, **Then**
   the command refuses **without executing any part of the plan**, including the parts it could
   have done.
3. **Given** a rate-card restructure in the plan, **When** `--apply` is passed with no
   `--baseline-effective-from`, **Then** the command refuses and explains that the date decides
   which historical usage the existing rates are taken to have covered.
4. **Given** a `gateway_usage_daily` that another view depends on, **When** the plan would replace
   it, **Then** the dependency is reported and the replacement is human-only — dropping it would
   take the dependent with it.

### Edge Cases

- **A database that is neither legacy nor native** — half-reconciled by hand, or a gateway
  deployment that ran only some of its own migrations. The diff is computed from what is *there*,
  never from what `schema_migrations` claims, so this is the ordinary case rather than a special
  one.
- **`schema_migrations` records a version tokenweir does not ship.** This turns out to be
  unreachable *for the reconciler*, and the reason is FR-003: `reconcile` never reads that table,
  so it has no opinion about what it records. `migrations.apply` and `status` still refuse with
  `UnknownAppliedVersionError`, which is correct for them and unchanged. Recorded here rather than
  quietly dropped, because "the edge case does not arise" and "the edge case was forgotten" look
  identical in a spec that says nothing.
- **No `gateway_usage` at all** — the reconciler is pointed at an empty database. That is not a
  reconciliation; the plan says so and directs the operator to `apply`.
- **An empty legacy table.** Backfills and PK swaps on zero rows must still produce the right
  *shape*; a reconciler tested only against populated tables can pass while adding a `NOT NULL`
  column the wrong way.
- **A view that is absent rather than mismatched** (the gateway never had a rollup). Create it;
  there is nothing to drop and nothing to depend on it.
- **Reference-schema construction fails** — no `CREATE SCHEMA` privilege on the target. The tool
  cannot compute a diff without it and must say precisely that, rather than reporting an empty
  plan, which is the one wrong answer available.
- **An autocommit connection.** The reference schema is built and rolled back; under autocommit
  the rollback is a lie and the scratch schema would survive. Refused, exactly as `apply()`
  refuses it and for a related reason.

## Requirements *(mandatory)*

### Functional Requirements

**Computing the difference**

- **FR-001**: The system MUST expose `tokenweir.reconcile.plan(connection, …)` returning a
  `ReconciliationPlan` — an ordered, structured description of every way the connected database's
  relevant objects differ from the schema tokenweir's migrations produce.
- **FR-002**: `plan()` MUST be read-only with respect to the connected database: it MUST leave no
  committed change of any kind, including the reference schema it builds.
- **FR-003**: The diff MUST be computed from the database's **actual catalog contents**
  (`information_schema` / `pg_catalog`), never from `schema_migrations`. The story's failure is a
  database whose recorded state and real state disagree.
- **FR-004**: The target schema MUST be obtained by executing the shipped migration SQL into a
  uniquely-named scratch schema inside a transaction that is then **rolled back** — not from a
  hand-written description of the expected objects. There MUST be exactly one statement of what
  tokenweir's schema is, and it MUST be `src/tokenweir/migrations/sql/`.
- **FR-005**: `plan()` MUST refuse a connection in autocommit mode, naming the reason (the
  reference schema's rollback would not roll back).
- **FR-006**: The scratch schema MUST be removed even when the reference build fails, and its name
  MUST NOT be predictable enough to collide with a concurrent run.

**What the plan contains**

- **FR-007**: Each discrepancy MUST carry: the object it concerns, a one-line human-readable
  description, a resolution class, and — when resolvable — the exact SQL statements that would
  resolve it.
- **FR-008**: The resolution class MUST be one of **`AUTOMATIC`** (the tool can resolve it with no
  data loss and no decision) or **`MANUAL`** (a human must decide). There MUST NOT be a third,
  softer class: a discrepancy the tool is unsure about is `MANUAL`.
- **FR-009**: A column present in the database and absent from tokenweir's schema MUST be reported
  and MUST NEVER be proposed for removal, whatever else is true of it. It is the gateway's data.
  Unless FR-010 applies it MUST be reported as an **`Observation`** rather than a `Discrepancy`:
  it is still there after a successful reconciliation, so counting it as a discrepancy would mean
  a reconciled database never produces an empty plan, and FR-021's "run it again and it does
  nothing" is the property that makes this safe to leave in a deploy script.
- **FR-010**: Such an extra column MUST be classified `MANUAL` when it is `NOT NULL` with no
  default **and it is on the table tokenweir's writer inserts into**
  (`tokenweir.postgres.USAGE_TABLE`), because `PostgresSource`'s INSERT does not name it and every
  write would fail. The rule MUST be tied to that constant rather than restated, so it follows the
  writer if the writer moves.

  It MUST NOT extend to other tables, and that boundary is load-bearing rather than cautious: the
  live `model_pricing_rates.pricing_mode` is `NOT NULL` with no default — it was half the old
  primary key — and it survives the restructure. tokenweir writes nothing to that table, so a rule
  stated more broadly than its reason would classify the canonical case as `MANUAL` and refuse the
  very reconciliation this story exists to perform. On such a table the column is an `Observation`
  saying inserts of the operator's own must still supply it.
- **FR-011**: A column whose type differs from tokenweir's MUST be `MANUAL`. A widening might be
  safe and a narrowing is not, and distinguishing them is a decision.
- **FR-012**: The plan MUST report, as `AUTOMATIC`, a missing `gateway_usage.schema_version`,
  resolving it by adding the column with a default of `1`, backfilling existing rows, setting
  `NOT NULL`, and then **dropping the default** so the reconciled column matches the one migration
  001 creates.
- **FR-013**: The plan MUST report, as `AUTOMATIC` *given a baseline date*, a `model_pricing_rates`
  that is current-valued rather than effective-dated, resolving it by adding `effective_from`,
  backfilling every existing row to the operator's baseline, and replacing the primary key with
  `(model, effective_from)`. Without a baseline date the discrepancy MUST be reported and MUST NOT
  be applied: the date decides which historical usage the existing rates are taken to have covered,
  and no default for it is honest. `-infinity` MUST be accepted and means "these were always the
  rates"; it MUST be documented as `--baseline-effective-from=-infinity`, because argparse reads a
  leading dash as an option and the spaced form fails. Positive `infinity` MUST be **refused**: it
  is valid SQL, it is in force on no day that has happened, and accepting it produced a run that
  reported `AUTOMATIC`, exited `0`, and left every pre-existing row unpriced — the direct negation
  of SC-004. The refusal MUST name `-infinity` as the thing probably meant, because the obvious
  recovery from the argparse error is to drop the dash.
- **FR-014**: When the existing rate card holds more than one row per `model` (the current-valued
  key is `(model, pricing_mode)`), the restructure MUST be `MANUAL` and MUST name the offending
  models. tokenweir's rate card has no `pricing_mode` column; one of those rows has no
  representation, and picking one is a decision about money.
- **FR-015**: A `gateway_usage_daily` whose column set differs from tokenweir's MUST be resolved by
  dropping and recreating it from migration 005 — `CREATE OR REPLACE VIEW` cannot change a view's
  column set. It MUST be `MANUAL` if any other object depends on the view.
- **FR-015a**: That drop takes the view's **ACL** with it, and migration 005 re-issues the grant
  only when `tokenweir.reader_role` is set, which reconciliation never does. The plan MUST report
  an `Observation` naming the roles that will lose `SELECT`. It MUST NOT re-grant: which roles
  *should* have access is not something this module knows, and reconciliation is not the moment to
  start managing permissions. ("The view holds no data" was the original justification for
  permitting this one drop; it was true of rows and false of privileges.)
- **FR-016**: Missing indexes MUST be `AUTOMATIC`. Extra indexes MUST be reported as
  `Observation`s and never dropped.
- **FR-016b**: Constraints other than the primary key MUST be compared, matched by **definition**
  (`pg_get_constraintdef`) rather than by name, since the gateway named its own. A reference
  constraint the live table lacks MUST be `AUTOMATIC` only when the existing rows are checked and
  satisfy it — `ADD CONSTRAINT … CHECK` validates what is already there, so classifying it
  otherwise is a plan that promises to resolve every difference and then dies on one. Rows that
  violate it, a definition the tool cannot check, or a name collision MUST all be `MANUAL`. Extra
  constraints MUST get FR-009's treatment: reported, never dropped.

  This was missing entirely from the first implementation, and the omission is worth recording
  rather than quietly repairing. `Relation` modelled columns, keys and indexes; migration 003's
  `CHECK (… >= 0)` — the guard that stops a negative price entering the rate card — was therefore
  absent from a reconciled table while `plan()` printed *"this database already matches the schema
  tokenweir owns"*. Both FR-001 and FR-017 were breached, and the SC-001 test could not see it
  because it is written in terms of the same snapshot that did not model constraints: the
  implementation and its test agreed with each other and disagreed with the requirement.
- **FR-016a**: An index whose **name** matches one tokenweir ships but whose shape differs MUST be
  `MANUAL`. Index names are unique per schema and the gateway named its own; the resolution
  statement is `pg_get_indexdef`'s, so it carries tokenweir's name and no `IF NOT EXISTS`, and
  `AUTOMATIC` would be a plan that cannot execute. Nothing is corrupted when it fails — the
  transaction rolls back — but "every difference here is resolvable" is the one promise the plan
  makes.
- **FR-017**: An empty plan MUST be distinguishable from a plan that could not be computed. The
  tool MUST NOT report "no differences" for any reason other than having found none.

**Applying it**

- **FR-018**: `tokenweir.reconcile.apply(connection, plan, …)` MUST execute the whole plan in **one
  transaction**, so a failure leaves the database exactly as it was.
- **FR-019**: `apply()` MUST refuse, before executing anything, if the plan contains any `MANUAL`
  discrepancy — including the `AUTOMATIC` parts it could have done. A partially reconciled database
  is the state this whole feature exists to prevent.
- **FR-020**: `apply()` MUST re-derive the plan against the live connection immediately before
  executing, and refuse if it no longer matches the plan it was given. A plan read by a human ten
  minutes ago is not evidence about the database now.
- **FR-021**: Reconciling an already-reconciled database MUST be a no-op producing an empty plan.

**The command line**

- **FR-022**: `python -m tokenweir.migrations reconcile` MUST print the plan and change nothing.
  `--apply` MUST be required to execute it. The default MUST be the safe one.
- **FR-023**: `--baseline-effective-from DATE` MUST supply FR-013's baseline. `--dsn`,
  `--reader-role` and `--verbose` MUST behave exactly as they do on the existing subcommands,
  including their environment fallbacks and either-side placement.
- **FR-024**: Exit codes MUST match the existing CLI: `0` success (including "nothing to do"), `1`
  a refusal or database error rendered as a message rather than a traceback, `2` a usage error.
  A refusal under FR-019 MUST exit `1`.

**Not silently succeeding elsewhere**

- **FR-025**: The warning `_check_applied` already emits for adopted, checksum-less versions MUST
  name the reconciler as the remedy. It is the one moment tokenweir knows it is looking at a
  database somebody else migrated, and it currently ends by reassuring the reader.
- **FR-026**: `README.md`'s "Taking over a database the gateway already migrated" MUST stop
  implying adoption is sufficient. It currently ends "Nothing is re-applied and nothing is
  dropped … What the runner does from then on is ordinary", which is true and, read by an
  operator, wrong.

### Key Entities

- **`Discrepancy`** — one difference between the live database and tokenweir's schema: the object,
  a description, a `Resolution` class, and the statements that would resolve it (empty when
  `MANUAL`).
- **`Observation`** — something true of this database that needs no action and must be said
  anyway: the extra columns and indexes the gateway owns, which are kept. Separate from
  `Discrepancy` because it survives reconciliation, and a plan that still listed it afterwards
  could never be empty (FR-009, FR-021).
- **`ReconciliationPlan`** — the ordered discrepancies, the observations, and the baseline it was
  planned with, with `is_empty` / `manual` / `automatic` views over the discrepancies. Ordered
  because the statements have to run in a workable order — tables before the view that selects
  from them, the column before the backfill.
- **`Resolution`** — `AUTOMATIC` | `MANUAL`. Two values, deliberately (FR-008). Note that this is
  not a third class by another name: an `Observation` carries no resolution because there is
  nothing to resolve.
- **Reference schema** — the transient, rolled-back schema built from the shipped migrations that
  the diff compares against (FR-004).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Against a database built to the legacy gateway shape and populated with usage rows,
  `reconcile --apply` produces a schema that introspects **identically** to one produced by running
  tokenweir's migrations on an empty database — same columns, types, nullability, defaults,
  primary keys and indexes on `gateway_usage` and `model_pricing_rates`, same column set on
  `gateway_usage_daily` — modulo **three** documented deviations, and nothing else: the `id`
  exception (`BIGSERIAL` is kept, per the story), any extra gateway columns (reported and kept),
  and column **order**, since an added column lands at the end of the table rather than in the
  migration's position. Nothing reads column position and reordering would mean rewriting the
  table, so the third is a deviation rather than a defect — but it is one, and the assertion must
  subtract it by name rather than by comparing in a way that cannot see it.
- **SC-002**: Every pre-existing row is present after reconciliation with every original column
  value unchanged, verified column-by-column and not by count alone.
- **SC-003**: `PostgresSource.write` succeeds against the reconciled database and fails against the
  same database before reconciliation — both asserted, so the test proves the fix rather than
  assuming the break.
- **SC-004**: Pre-existing usage rows price through `gateway_usage_daily` after reconciliation, to
  the figures the gateway's current-valued card would have produced for them.
- **SC-005**: With any `MANUAL` discrepancy present, `--apply` changes nothing at all — asserted by
  comparing a full introspection snapshot before and after, on a database that also has
  `AUTOMATIC` discrepancies it could have resolved. (A test where there was nothing to skip would
  assert nothing.)
- **SC-006**: The residual this branch cannot close — that the legacy shape is a reconstruction and
  not a dump of the live `ai_gateway_metrics` — is stated in `README.md` where an operator will
  read it before running the tool, not only in this spec.
- **SC-007**: The authoritative test command (`/workspace/.mado/project.yaml`) stays green, and the
  real-Postgres additions skip cleanly under the bare install exactly as the existing store tests
  do, with `tests/conftest.py`'s disclosure note unchanged in meaning.

## Additions beyond the requirements

Three things were built that no requirement above asks for. Each is small, each is defended, and
each is listed here so it reads as a decision rather than as accretion nobody noticed:

- **`apply()` refuses a `baseline_effective_from` that disagrees with the plan it was handed.**
  FR-020's staleness check already covers the case that matters. This covers a narrower one — a
  caller applying a plan a human read under a *different* date — where the failure is silent and
  the subject is money.
- **The rendered plan flags statements that remove a relation.** FR-007 asks for the statements;
  it does not ask for them to be annotated. The annotation exists because `--apply` is the operator
  review, and a review where the one destructive statement has to be spotted by reading SQL
  carefully is not one.
- **A test asserts the README still names the three ways a reconciled database differs from a
  fresh one.** SC-006 requires only the reconstruction residual. This guards the neighbouring
  claims, by anchor rather than by sentence, following the rule TOKWEIR-31 set for exactly this.

## Known limits, recorded rather than left to be rediscovered

- **`--reader-role` is accepted by `reconcile` and does nothing.** It comes from the shared option
  group FR-023 requires, and reconciliation never sets `tokenweir.reader_role` (FR-015a). Named in
  the README and in the grant observation, so an operator who reads is told; the flag still parses.
- **The rollup view is compared by column set and order, not by definition text.** A live view
  whose columns happen to match tokenweir's but whose body does not is reported as no difference.
  FR-015 asks only for the column-set case, and pinning the text would cry wolf on every reformat —
  but the residual is real, and this is where it is written down.
- **`apply()`'s re-derivation is not atomic with its execution.** `plan()` rolls back, which is
  what makes it read-only, so the statements run in a new transaction holding none of the
  comparison's locks. A change landing in that gap is an error and a rollback, not a corrupted
  database. No advisory lock is taken: that lock serialises two *migrators*, and two operators
  reconciling one database at once is not a case anybody has.

## Assumptions

- **A-001**: The legacy shape is taken from the story's three known deltas and the hand-off
  comment. Where they are silent (e.g. whether the gateway's rate card had provenance columns) the
  tool's introspection makes the question moot — it reconciles what it finds. Where it *cannot* be
  made moot, the tool refuses rather than guesses.
- **A-002**: The gateway's `id` stays `BIGSERIAL` on a reconciled live table. The story says so
  explicitly, and the conversion is a table rewrite for a property that matters only if rows are
  ever rebuilt.
- **A-003**: The operator runs this against a restored dump first. The tool cannot enforce that;
  the documentation says it, and the plan-by-default behaviour makes the safe order the easy one.
- **A-004**: `pricing_mode` remaining on a reconciled `model_pricing_rates` as an extra column is
  acceptable. It is the gateway's data, FR-009 forbids dropping it, and tokenweir's rollup does not
  read it.
