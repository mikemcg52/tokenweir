# Tasks: Extract the writer, migrations and pricing view

**Branch**: `TOKWEIR-5-extract-writer-migrations-pricing-view` | **Spec**: [spec.md](./spec.md) |
**Plan**: [plan.md](./plan.md)

## Phase 1 — Schema (FR-001 … FR-006, FR-024 … FR-030)

- [x] **T001** `sql/001_gateway_usage.sql` — `gateway_usage`, columns matching the v1 contract's field
      set, `ts TIMESTAMPTZ NOT NULL DEFAULT now()`, `schema_version` stored, no cost column; indexes on
      `ts` and `(app_id, ts)`. `IF NOT EXISTS` throughout.
- [x] **T002** `sql/002_parent_request_id.sql` — add `parent_request_id`, partial index where not null.
- [x] **T003** `sql/003_model_pricing_rates.sql` — effective-dated rate card keyed `(model,
      effective_from)`, costs named `*_usd_per_mtok`, cache rates nullable.
- [x] **T004** `sql/004_reader_grant.sql` — `DO` block granting `SELECT` on both tables to
      `current_setting('tokenweir.reader_role', true)`; no-op with a notice when unset or absent.
- [x] **T005** `sql/005_gateway_usage_daily.sql` — the rollup: IMMUTABLE day bucket, `LEFT JOIN LATERAL`
      onto the rate in force, `BOOL_AND` priced flag excluding `subscription`, `est_cost_usd` NULL when
      not fully priced, grouped by app/day/model/pricing_mode; plus its own reader grant.
- [x] **T006** `sql/006_gateway_usage_app_day_index.sql` — functional index on `(app_id, <the same day
      expression, character-for-character>)`.

## Phase 2 — The preserved fixes, as tests that need no database (FR-034, SC-013 … SC-015)

- [x] **T007** `tests/test_migration_sql.py`: the set is 001–006, zero-padded, contiguous, unique.
- [x] **T008** No `DATE_TRUNC` over `ts` anywhere; the day expression is the IMMUTABLE one.
- [x] **T009** The view's day expression and the index's are character-for-character identical
      (extracted from the text, not eyeballed).
- [x] **T010** The rollup's priced flag is `BOOL_AND`, and `subscription` is excluded from it.
- [x] **T011** No destructive statement in any shipped migration (comments stripped first).
- [x] **T012** No cost-valued column on `gateway_usage`; cost appears only in the rate card and the view.
- [x] **T013** `gateway_usage`'s columns are exactly the v1 contract's fields (plus `id`) — read from
      `tokenweir.contract`, so the table and the record cannot drift apart silently.
- [x] **T014** Every migration creating a relation grants the reader role `SELECT` on it.

## Phase 3 — The runner (FR-007 … FR-014)

- [x] **T015** `Migration` (version, name, sql) and `discover()` via `importlib.resources`.
- [x] **T016** `looks_destructive()` — comment-stripping and whole-word matching.
- [x] **T017** `applied_versions(conn)`, creating `schema_migrations` if absent.
- [x] **T018** `pending(conn)`; raise on an applied version the library does not ship (FR-014).
- [x] **T019** `apply(conn, *, reader_role=None, allow_destructive=False)` — advisory lock, ascending
      order, one transaction per migration, `SET LOCAL tokenweir.reader_role` when configured.
- [x] **T020** `connect(dsn)` — the only psycopg import, `ImportError` naming the extra.
- [x] **T021** `__main__.py` — `apply` / `status`, non-zero exit with a message rather than a traceback.
- [x] **T022** `tests/test_migrations.py` — recording fake connection: order, idempotence, rollback on
      failure, unknown applied version, destructive refusal and opt-in, lock taken and released,
      `reader_role` propagation, discovery from package data.

## Phase 4 — The writer (FR-015 … FR-023)

- [x] **T023** `row_for(record)` / `rows_for(records)` — mapping, `ts` handling (absent/blank → server
      default, unparsable → raise), pure and driver-free.
- [x] **T024** `INSERT_SQL` against `gateway_usage`, `COALESCE`-ing the timestamp to `now()`.
- [x] **T025** `PostgresSource(connection)` — `write` (validate-then-one-transaction, returns count,
      raises on store failure, `0` and no statement for an empty batch) and `close`.
- [x] **T026** `tests/test_postgres_source.py` — mapping, empty batch, pre-flight validation before any
      statement, one transaction per batch, rollback on failure, `Source` protocol conformance,
      `close` idempotent.
- [x] **T027** Subprocess test: `tokenweir`, `tokenweir.migrations`, `tokenweir.postgres` all import
      with no psycopg present, and `connect` fails naming the extra.

## Phase 5 — Real Postgres (FR-031 … FR-033)

- [x] **T028** `tests/conftest.py` — `TOKENWEIR_TEST_DSN` gating, skip message naming the variable,
      per-run schema created and dropped.
- [x] **T029** `tests/test_postgres_integration.py` — apply to empty, re-apply, partial-state apply,
      failure rollback, concurrent apply.
- [x] **T030** Round-trip: a written batch reads back field-for-field; `ts` defaulting; batch failure
      leaves nothing.
- [x] **T031** The rollup: fully priced, one-unpriced-call, subscription-with-full-rate-card,
      mid-month reprice, UTC day boundary.
- [x] **T032** Confirm the whole Phase-5 set **skips** cleanly under the authoritative command here.

## Phase 6 — Packaging and docs (FR-002, FR-023, FR-035 … FR-037)

- [x] **T033** `pyproject.toml` — `postgres` extra, psycopg into `dev`, `package-data` for `sql/*.sql`.
- [x] **T034** Test that the SQL is reachable as package data through `importlib.resources`.
- [x] **T035** `README.md` — applying migrations, the writer, the rollup, running the real-Postgres
      tests, and the deliberate `pricing_mode` divergence.
- [x] **T036** Confirm `schema/usage-record.v1.json` and `SCHEMA_VERSION` are untouched and the
      package's exports are unchanged.
- [x] **T037** `ruff check .` clean; full suite green under the authoritative command.

## Added during implementation (spec updated to match)

- [x] **T038** `--dsn` was rejected after the subcommand, which is the form the CLI's own docs showed.
      Fixed with a shared parent parser and `argparse.SUPPRESS` defaults so neither position discards
      the other; `tests/test_migrations_cli.py` pins both (FR-013, SC-022).
- [x] **T039** The migration runner records a checksum per version and refuses a database whose
      applied migrations no longer match the shipped files — FR-004 made mechanical (FR-038, SC-021).
- [x] **T040** Extended `test_contract.py`'s stdlib-only sweep to the new subpackage and split it in
      two: no third-party import at **import time**, and every **deferred** one must name a declared
      optional extra. Found by the existing guard failing on `migrations.connect` (FR-039, SC-023).
- [x] **T041** Syntax-check the SQL and the PL/pgSQL blocks with `pglast` where installed; added to
      the `dev` extra, skipped when absent (FR-040, SC-025).
- [x] **T042** `build/` and `dist/` added to `.gitignore` with a `git check-ignore` guard — building a
      wheel to verify T034 left an uncommittable-but-untracked `build/` in the tree (FR-041, SC-024).

## Fix round 1 (review findings)

- [x] **T043** **High** — the advisory lock was acquired *after* `pending()`, so
      `CREATE TABLE IF NOT EXISTS schema_migrations` ran outside it. `CREATE TABLE IF NOT EXISTS` is
      not atomic against a concurrent creation, so two migrators against a fresh database collided on
      the system catalog (reproduced 5/5). Lock now precedes every read or creation of the state
      table; pinned with no database (statement order) and against a real server (FR-012, SC-006).
- [x] **T044** **High** — `apply` and `status` both died with `UndefinedColumn` against a
      `schema_migrations` that predates the `checksum` column, i.e. the AI Gateway's own — the first
      database TOKWEIR-10 points this at. The runner now adopts such a table: `ADD COLUMN IF NOT
      EXISTS`, pre-existing rows recorded as applied, their absent checksums reported as unverifiable
      rather than as drift (FR-042, SC-026).
- [x] **T045** **Med** — `gateway_usage_columns` read 001 and 002 by name, so a `007` adding a cost
      column would leave the FR-028 and contract-drift guards green. It now scans every shipped
      migration, with an anti-vacuity test that proves the scan reaches a hypothetical 007.
- [x] **T046** **Med** — `reader_role` only takes effect on the run that applies 004/005; configuring
      one later grants nothing, silently. Documented in README with the one-statement remedy, and in
      `apply`'s docstring.
- [x] **T047** **Med** — the spec's "no usable Postgres" premise was false (`pgserver` ships the
      binaries in its wheel; no root, apt or Docker needed), and it is what let T043 ship. Corrected
      in `spec.md` and `plan.md`; `pgserver` added to the `dev` extra and `conftest.py` now starts a
      throwaway server when `TOKENWEIR_TEST_DSN` is unset, still skipping for a core-only install.
- [x] **T048** **Low** — CLI option-position coverage extended to `--reader-role` and `--verbose` on
      both sides of the subcommand, plus `--dsn A status --dsn B` (SC-022).
- [x] **T049** **Low** — the two undocumented pricing rules written down: cache tokens with no cache
      rate ⇒ unpriced, and NULL `pricing_mode` ⇒ priceable (spec US3, README).
- [x] **T050** **Low** — the `verify_checksums=False` escape hatch documented in README; `status()`
      no longer reads `schema_migrations` twice, so both halves of its answer come from one snapshot.

## Fix round 2 (review findings)

- [x] **T051** **High** — T043 fixed `apply` and left the reader path unlocked. `status`,
      `pending` and `applied_versions` all reach `_ensure_state_table`, so a health check's `status`
      against a fresh database still killed a concurrent deploy with a catalog `UniqueViolation`
      (reproduced 3/3 barrier-synchronised). The lock now lives in a `_advisory_lock` context manager
      that every entry point takes; `apply` passes `advisory_lock=False` inward rather than relying on
      Postgres reference-counting re-entry. SC-006 now races `status`+`apply` and `status`+`status`,
      not only two writers (FR-012).
- [x] **T052** **Med** — `BOOL_AND`, the story's named fix, had no behavioural test: swapping in
      `BOOL_OR` left all 27 real-Postgres tests green because every group in the suite was uniformly
      priced or uniformly not, and the two agree on uniform groups. Added the one shape that can tell
      them apart — one priced call and one cache-unpriced call in the same group — and verified the
      mutation now fails (FR-025, SC-027).
- [x] **T053** **Med** — T047 corrected the "no usable Postgres" claim in two places and left it
      standing in five: `plan.md`'s environment facts and the docstrings of four test modules, plus a
      claim that the `pglast` check is skipped here. All corrected; `README`'s advisory-lock line
      narrowed to what is now true.
- [x] **T054** **Low** — the FR-030 grant check read a hardcoded list of three relations, the same
      hole T045 closed for the column scan. Now derived from every `CREATE TABLE`/`VIEW` in the
      shipped SQL, with an anti-vacuity test proving a 007 that forgets its grant is caught.
- [x] **T055** **Low** — `_ensure_state_table` probed nothing and issued three
      `ALTER TABLE … ADD COLUMN IF NOT EXISTS` on every call, each taking `ACCESS EXCLUSIVE` as a
      no-op. It now reads `pg_attribute` first and alters only what is missing, so the steady state
      issues no DDL.
- [x] **T056** **Low** — every CLI test stubbed `connect`, so argparse-through-psycopg-to-a-server was
      never run. Added real-driver integration tests for `apply`, re-`apply`, `status` and a drifted
      database (FR-013, SC-020, SC-022).
- [x] **T057** **Low** — `PostgresSource` documented that the connection must not be in autocommit and
      enforced nothing. It now refuses one, because losing atomicity silently is worse than being
      told (FR-016).
- [x] **T058** **Low** — `status` gained `--verify-checksums`: FR-038 only requires the opt-*out*, but
      the command line was the one place an operator could never see drift.

## Fix round 3 (review findings)

- [x] **T059** **Med** — round 2 made `PostgresSource` refuse an autocommit connection and left the
      runner accepting one, where the consequence is worse. `set_config(..., is_local => true)` scopes
      the reader role to the applying transaction, so under autocommit migrations 004/005 saw no role,
      took their no-op branch, and were recorded as applied — the grant lost permanently, not delayed
      (demonstrated against a real server: `granted=[]` with autocommit, three relations without it).
      It also breaks FR-009's one-transaction-per-migration. `apply` now refuses, and says which two
      guarantees it is protecting (FR-009, FR-016, FR-030).
- [x] **T060** **Low** — the adopted-rows "unverifiable checksum" warning sat *after* the
      `verify_checksums` early return, and `status` defaults to not verifying. The one caller that
      exists to describe an adopted database was the one guaranteed never to mention it. Warning moved
      above the return (FR-042).
- [x] **T061** **Low** — added the no-database counterpart of the reader-path lock test: `status` must
      take the advisory lock before creating `schema_migrations`, mirroring the one `apply` has. The
      unlocked reader shipped past a green no-DB suite once already.
- [x] **T062** **Low** — `TOKENWEIR_DSN` and `TOKENWEIR_READER_ROLE` documented in README with the
      option/environment table; `--verify-checksums` documented alongside.
- [x] **T063** **Low** — SC-020 names `python -m tokenweir.migrations`, and every test called `main()`
      in-process. Added a `subprocess` test of the real entry point, which is the only thing that can
      catch a broken `__main__` guard.
- [x] **T064** **Low** — the reviewer flagged as unverified whether a password-bearing DSN can reach
      stderr through the connect-failure message. Checked five adversarial forms against psycopg:
      libpq quotes the offending *keyword*, never the value. No leak — and now pinned by a test, since
      the message interpolates a driver exception this project does not control.

## Fix round 4 (review findings)

- [x] **T065** **Med** — the destructive guard had a false negative in the dangerous direction, and
      its docstring claimed it could not: `_COMMENT_RE` treated a `--` inside a *string literal* as a
      comment, so `VALUES ('cleanup -- see #12'); DROP TABLE gateway_usage;` scanned clean.
      Comment-stripping is now a left-to-right scan that knows literals from comments (and handles
      Postgres's nesting block comments). Literal *contents* are deliberately kept — `EXECUTE 'DROP
      TABLE …'` in a `DO` block is a real drop, so blanking literals would swap one false negative for
      a worse one. Docstring corrected, both directions pinned by tests (FR-010).
- [x] **T066** **Med** — SC-001 ("discovered from the installed package, no repository present") was
      tested only by proxy: one test read an editable install, the other read the `pyproject.toml`
      declaration. Both stay green if the declaration is right and the build stops honouring it. Now
      builds a wheel into `tmp_path` and asserts the six `sql/*.sql` members are inside it; `build`
      added to the `dev` extra, skipped when absent (SC-031).
- [x] **T067** **Med** — the reviewer could not reach Jira and flagged the spec's verbatim story quote
      as unverified, noting this file was revised during the story and so partly grades itself.
      Fetched TOKWEIR-5: the quote matches the description verbatim. Recorded in `spec.md` along with
      the caveat, since it is a real one. **Left for the human:** ratifying `test_contract.py`'s
      relaxation from "no third-party import anywhere" to "none at import time" — authorised by
      FR-039, which this story wrote. The compensating controls are real, but inheriting a relaxation
      the change itself authorised is a call for the developer, not for the change.
- [x] **T068** **Low** — `PostgresSource`'s autocommit refusal was constructor-only, and `autocommit`
      is a mutable attribute. Extracted to `_reject_autocommit` and re-checked in `write`, so flipping
      it after construction no longer slips past (SC-032).
- [x] **T069** **Low** — `_advisory_lock` commits the connection on entry and exit, which commits
      anything the caller had in flight. Documented on the context manager and in the module
      docstring: the runner owns this connection's transaction boundaries, which is what FR-009 is.
- [x] **T070** **Low** — recorded the deliberate call that `status` still refuses a database *ahead*
      of the library while FR-038/FR-042 both grant an opt-out. tokenweir can describe a drifted or
      adopted database; it cannot describe a version it does not ship. A `status` listing six
      migrations while silently omitting a seventh is worse than a refusal (spec, `_check_applied`).
- [x] **T071** **Low** — FR-029 (the rate unit in the column name) had no no-database assertion; it
      rested on a README marker. Now every cost-valued column in the shipped SQL must end in
      `_usd_per_mtok`, with a guard against the scan finding nothing (SC-033).
- [x] **T072** **Low** — documented that `write` returns the number of rows *sent* rather than a
      server rowcount, and why the two cannot disagree here.
- [x] **T073** Carried the TOKWEIR-10 hand-off — "diff this reconstructed DDL against the live
      `ai_gateway_metrics` schema before pointing the gateway at it" — onto the Jira issue itself,
      rather than leaving it only in this repo's spec folder.
