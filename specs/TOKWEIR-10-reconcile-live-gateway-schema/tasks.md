# Tasks: Reconcile a live gateway database with tokenweir's owned schema

**Branch**: `TOKWEIR-10-reconcile-live-gateway-schema` | **Spec**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md)

## Format: `[ID] [P?] [Story] Description`

`[P]` = touches a file nothing else in its phase touches, so it can run in parallel.
`[US1]` / `[US2]` / `[US3]` = the user story from spec.md the task serves.

## Path Conventions

Single Python package, `src/` layout. Source under `src/tokenweir/`, tests under `tests/`.

---

## Phase 1: Setup

- [ ] **T001** Confirm the baseline is green before anything changes, using the authoritative
      command from `/workspace/.mado/project.yaml`: `CI=true /workspace/repo/.venv/bin/pytest`
      over a venv built by `python3 -m venv .venv && .venv/bin/pip install -e . pytest -q`.
      Record the count. *(Done during planning: 894 passed, 82 skipped, exit 0.)*
- [ ] **T002** Build a second, throwaway venv with the `dev` extra so the real-Postgres suite
      actually runs during development (`pgserver` starts an embedded server; no root, no Docker).
      This venv is **not** the authoritative one and must not replace it — the authoritative
      install is `pip install -e . pytest` and the store tests must still skip under it (SC-007).

## Phase 2: Foundational (blocking prerequisites)

Everything in Phase 3+ depends on these. No user story is deliverable without the snapshot and the
reference, because the whole feature is a comparison between them.

- [ ] **T003** Create `src/tokenweir/reconcile.py` with the module docstring stating what this is
      and — first — why it is not migration `007` (spec.md § Why this is not migration 007).
      Declare `__all__`, the `Resolution` enum (`AUTOMATIC` | `MANUAL`, and no third value —
      FR-008), the `Discrepancy` dataclass (FR-007) and the `ReconciliationPlan` dataclass with its
      `is_empty` / `automatic` / `manual` views, and the `ReconcileError` hierarchy mirroring
      `MigrationError`'s "every error is a refusal to act" shape.
- [ ] **T004** Implement `_snapshot(connection, schema)` (plan.md § 1): per-relation kind, and per
      column name / `format_type(atttypid, atttypmod)` / `attnotnull` / default expression /
      `attidentity`; per table the primary-key column list and the normalised `pg_get_indexdef`
      set. Read `pg_attribute`, **not** `information_schema.columns` — `numeric(18,10)` and
      identity columns are both invisible to the latter, and they are the two distinctions this
      story turns on.
- [ ] **T005** Implement `reference_snapshot(connection)` (FR-004, FR-005, FR-006): unique scratch
      schema, `SET LOCAL search_path`, execute every `discover()` migration, snapshot, **roll
      back**. Refuse an autocommit connection. Clean up on failure.

## Phase 3: User Story 1 — Tell me what is wrong with this database (P1) 🎯 MVP

**Goal**: an operator can point this at a restored dump and be shown every difference, classified,
with nothing changed.

**Independent test**: legacy database in, plan out, database provably untouched.

### Tests for User Story 1

- [ ] **T006** [P] [US1] `tests/test_reconcile.py`: the `legacy_gateway_schema` fixture — the
      reconstruction of the gateway's shape from the story's three known deltas and the hand-off
      comment (plan.md § Testing strategy). Comment it as a reconstruction and as the **single**
      place to correct when a real dump is available (SC-006).
- [ ] **T007** [P] [US1] Assert `plan()` on a legacy database names the missing `schema_version`,
      the current-valued rate card and the view mismatch (US1 scenario 1).
- [ ] **T008** [P] [US1] Assert `plan()` on a tokenweir-native database is empty (US1 scenario 2,
      FR-021).
- [ ] **T009** [P] [US1] Assert `plan()` leaves the database unchanged — full snapshot before and
      after, plus "no `tokenweir_ref_*` schema survives" (US1 scenario 3, FR-002, FR-006).
- [ ] **T010** [P] [US1] Assert an extra gateway column is reported and never proposed for removal;
      and that an extra `NOT NULL`-without-default column is `MANUAL` (US1 scenario 4, FR-009,
      FR-010).
- [ ] **T011** [P] [US1] Assert `plan()` refuses an autocommit connection (FR-005).

### Implementation for User Story 1

- [ ] **T012** [US1] Implement `plan(connection, *, baseline_effective_from=None)`: the generic
      column diff (missing / extra / type-mismatch → FR-009, FR-010, FR-011), the missing-index
      case (FR-016), and the `gateway_usage.id` exemption with the story quoted at it (A-002).
- [ ] **T013** [US1] Implement the `schema_version` backfill resolution (FR-012): `ADD COLUMN …
      DEFAULT 1 NOT NULL` then `ALTER COLUMN … DROP DEFAULT`, so the reconciled column is exactly
      the one migration 001 creates. Any *other* missing `NOT NULL`-without-default column is
      `MANUAL` — there is no honest backfill value for it.
- [ ] **T014** [US1] Implement the rate-card restructure detection and resolution (FR-013), and the
      collision guard that makes it `MANUAL` (FR-014).
- [ ] **T015** [US1] Implement the view comparison (FR-015): column set and order, not definition
      text; `DROP VIEW` + migration 005 on mismatch; `MANUAL` when `pg_depend` reports a dependent.
- [ ] **T016** [US1] Make "no `gateway_usage` at all" and `UnknownAppliedVersionError` land as
      plain, actionable refusals rather than tracebacks or a misleadingly empty plan (FR-017, spec
      Edge Cases).

**Checkpoint**: `plan()` is usable on its own — read-only, and the most dangerous thing it can do
is tell you something.

## Phase 4: User Story 2 — Bring the database over without losing a row (P1)

**Goal**: the plan executes, atomically, and every pre-existing row survives and still prices.

### Tests for User Story 2

- [ ] **T017** [P] [US2] Row preservation: populate the legacy database, apply, assert every row
      present with every original column value unchanged, column-by-column (SC-002), and
      `schema_version = 1` throughout.
- [ ] **T018** [P] [US2] Schema equivalence: after apply, the live snapshot equals a
      tokenweir-native snapshot, modulo the documented `id` exception and the reported extra
      columns (SC-001).
- [ ] **T019** [P] [US2] The writer, both directions: `PostgresSource.write` raises
      `UndefinedColumn` **before** reconciliation and succeeds after (SC-003). Both halves, so the
      test cannot pass against a database that never had the problem.
- [ ] **T020** [P] [US2] The rollup: pre-existing rows price after reconciliation, to the figures
      the gateway's current-valued card would have produced (SC-004).
- [ ] **T021** [P] [US2] Idempotence: a second run plans empty and executes nothing (FR-021).
- [ ] **T022** [P] [US2] Atomicity: a statement that fails part-way leaves the database exactly as
      it was (US2 scenario 5, FR-018).
- [ ] **T023** [P] [US2] `apply()` refuses a plan that no longer matches the live database
      (FR-020).

### Implementation for User Story 2

- [ ] **T024** [US2] Implement `apply(connection, plan)`: re-derive, compare, refuse on `MANUAL`,
      execute in one transaction, commit; roll back the lot on any exception (FR-018, FR-019,
      FR-020).

**Checkpoint**: the story's second and third acceptance clauses are met for a reconstructed legacy
database.

## Phase 5: User Story 3 — Refuse what a human has to decide (P2)

**Goal**: the tool stops rather than finishes.

### Tests for User Story 3

- [ ] **T025** [P] [US3] Two rows for one model under different `pricing_mode`s: plan marks the
      restructure `MANUAL` and names the models (US3 scenario 1, FR-014).
- [ ] **T026** [P] [US3] `--apply` with any `MANUAL` present changes **nothing at all** — snapshot
      before and after (US3 scenario 2, SC-005, FR-019).
- [ ] **T027** [P] [US3] A rate-card restructure with no `--baseline-effective-from` refuses and
      explains what the date decides (US3 scenario 3, FR-013).
- [ ] **T028** [P] [US3] A dependent view makes the rollup replacement `MANUAL` (US3 scenario 4,
      FR-015).

### Implementation for User Story 3

- [ ] **T029** [US3] Wire the refusals into `apply()` and give each a message naming the object,
      the decision and the remedy. A refusal an operator cannot act on is a traceback with better
      manners.

## Phase 6: The command line

- [ ] **T030** Add the `reconcile` subcommand to `src/tokenweir/migrations/__main__.py` (FR-022,
      FR-023, FR-024): plan-by-default, `--apply` to execute, `--baseline-effective-from`, and the
      existing `--dsn` / `--reader-role` / `--verbose` behaviour including either-side placement
      and environment fallbacks. Exit codes as the module already documents them.
- [ ] **T031** [P] Extend `tests/test_migrations_cli.py`: plan-by-default changes nothing, `--apply`
      applies, a `MANUAL` plan exits `1` with a message and not a traceback, and an unparsable
      `--baseline-effective-from` is a usage error.

## Phase 7: Not silently succeeding elsewhere

- [ ] **T032** FR-025 — `_check_applied`'s adopted-versions warning names the reconciler. It is the
      one moment the library knows it is looking at somebody else's database, and it currently ends
      by reassuring the reader.
- [ ] **T033** FR-026 / SC-006 — `README.md`: "Taking over a database the gateway already migrated"
      stops implying adoption finishes the job; a new subsection documents `reconcile`, the
      plan-first order, and the residual that the legacy shape is a reconstruction rather than a
      dump of the live database.

## Phase 8: Polish & cross-cutting

- [ ] **T034** Run the authoritative command and confirm green with the store tests **still
      skipping** (SC-007), and separately run the full suite against the embedded server so the new
      tests actually execute. Both, and report both — a green authoritative run that skipped
      everything new proves nothing about it.
- [ ] **T035** `ruff check` clean (the repo configures `E`, `F`, `I`, `N`, `W` at line-length 100).
- [ ] **T036** Re-read `tests/conftest.py`'s `OPTIONAL_DRIVERS` disclosure: the new tests gate
      through the existing `psycopg` entry, so no new entry is needed — confirm that
      `test_optional_drivers.py`'s two-directional consistency check still passes and that the
      note's claim still describes what a bare install forfeits.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1** → **Phase 2**: nothing can be verified without a server to verify it against.
- **Phase 2** → **Phases 3–5**: the snapshot and the reference are the feature; both stories are
  comparisons over them.
- **Phase 3** → **Phase 4**: `apply` executes what `plan` produced. There is nothing to apply
  first.
- **Phase 4** → **Phase 5**: the refusals are refusals *to apply*.
- **Phases 3–5** → **Phase 6**: the CLI is a thin call into the module, matching `__main__.py`'s
  existing "deliberately thin" contract.
- **Phase 7** is independent of 3–6 and can be done at any point after Phase 2.

### Within each user story

Tests before implementation within a story where the story's own behaviour is being pinned —
US1's T006 fixture in particular is a prerequisite for everything after it, in both directions.

### Parallel opportunities

- T006–T011 are all in `tests/test_reconcile.py` but are independent test functions after T006
  lands the fixture.
- T017–T023 likewise.
- T031 (`test_migrations_cli.py`) and T033 (`README.md`) touch files nothing else touches.

## Implementation Strategy

**MVP is User Story 1 alone.** A read-only reconciler that tells an operator what the silent skip
was hiding is the single most valuable thing here, it is safe to run against production, and it is
the step the story says to do first. If budget ran out after Phase 3, the branch would still be
worth merging.

**Then US2**, which is the story's acceptance. **Then US3**, which is what makes US2 safe to point
at a real database — and which is where the review should look hardest, because a reconciler's
characteristic failure is not stopping when it should.

## Notes

- The forward migration set (`sql/001`…`006`) is **not touched**. Any diff against it in review is
  a defect: released migrations are immutable and every deployed database's checksums depend on it.
- The authoritative test command is the one in `/workspace/.mado/project.yaml` and nothing else.
  The dev-extra venv exists so the new tests can be *seen* to pass; it does not replace it.
