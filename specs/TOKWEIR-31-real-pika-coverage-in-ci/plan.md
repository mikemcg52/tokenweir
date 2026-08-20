# Implementation Plan: Make the authoritative run admit what it did not prove

**Branch**: `TOKWEIR-31-real-pika-coverage-in-ci`
**Spec**: [spec.md](./spec.md)
**Created**: 2026-08-20
**Jira**: TOKWEIR-31 (Story), `Relates` to TOKWEIR-6

## Summary

The two FR-022 real-driver tests skip under the registry's install step, and the run reports that
as part of an undifferentiated "81 skipped". This story does not try to make them run — it cannot,
because the registry file lives on the MADO VM and is not reachable from a stream pod, and because
the core is transport-free by contract so `pika` cannot be made mandatory here.

Instead it takes the ticket's option 3 and makes it actually work: one record of what a bare
install forfeits, rendered into **the run's own output** (where the misreading happens) and into
**`README.md` under "Develop"** (where the decision belongs), with tests that keep the two honest
and keep the record in step with the suite.

The whole change is documentation and reporting. No source file under `src/tokenweir/` is touched,
no behaviour of the library changes, and no dependency is added anywhere.

## Technical Context

**Language**: Python 3.11+
**Testing**: pytest; the authoritative command is `/workspace/repo/.venv/bin/pytest` with
`CI=true`, per `/workspace/.mado/project.yaml`, from a venv built by `pip install -e . pytest`.
**Baseline on this branch**: 814 passed, 81 skipped, exit 0; `import pika` → `ModuleNotFoundError`.
**Constraints**: `pika` stays out of core (ADR-0001 Pillar 2). The gated tests keep skipping, never
failing. The new reporting must not touch exit status.

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled speckit template, as recorded in TOKWEIR-6's
and TOKWEIR-30's plans; ADR-0001's pillars are the gate instead.

- **Pillar 2 (dependency-light, transport-free core)** is the pillar that *causes* this problem and
  the one the change must not weaken. It is why the answer is not "add `pika`". The plan adds
  nothing to `dependencies` or to any extra, and SC-041 checks that from the declarations.
- No other pillar is engaged: no contract, schema, pricing or store behaviour changes.

## Project Structure

```text
tests/conftest.py                  # the disclosure record + the terminal-summary hook
tests/test_optional_drivers.py     # new: the hook, the record, and the README guards
README.md                          # "Develop" — the written-down decision (FR-048, FR-049)
specs/TOKWEIR-31-.../              # spec.md, plan.md, tasks.md
```

**Structure Decision**: The record and the hook go in `tests/conftest.py`, not a new module.
`pytest_terminal_summary` is only collected from `conftest.py`, and `conftest.py` is already this
repository's home for "what this environment can and cannot evaluate" — the Postgres gate and its
skip reason live there for exactly that reason. Putting the AMQP driver's disclosure anywhere else
would split one concept across two files.

The *tests* get their own file rather than joining `test_amqp.py`. What they assert is a property
of the suite and the repository — the same category as `test_repo_hygiene.py`, and deliberately not
scoped to AMQP, since the record covers four drivers.

## Key design decisions

1. **One record, three consumers.** `OPTIONAL_DRIVERS` in `tests/conftest.py` maps an importable
   module name to the claim its absence forfeits. The terminal note renders from it, the README
   guard is keyed to it, and the consistency check in FR-051 measures it against the suite. A single
   source is the point: the failure this story is fixing is a log and a document disagreeing about
   what was proven, and two records would reintroduce it one refactor later.

2. **Absence is decided by import, not by watching skips.** The alternative — hooking report events
   and collecting actual skip outcomes — sounds more truthful and is worse: it depends on which
   tests were selected, breaks under `-k`/`-x`/`xdist`, and reports nothing when the run dies early.
   FR-047 requires the note regardless of the run's shape, and a driver's importability is a fact
   about the environment that holds whether or not a single test ran.

   `except Exception` on the probe, not `except ImportError` — FR-046 says the note must not raise
   even for a package that is installed but explodes on import, and such a package is exactly as
   unable to prove FR-022 as a missing one.

3. **`pytest_terminal_summary`, not a print at collection time.** It runs once, after the results,
   for every invocation including a failed or interrupted one, and it writes through the terminal
   reporter so it respects `-q` and capture. It cannot influence exit status, which is what makes
   FR-046 true by construction rather than by care.

4. **The consistency check is bidirectional, and that is the load-bearing test.** Scanning the test
   sources for `importorskip("...")` and requiring the set to equal the record's keys catches both
   drifts: a new gated driver nobody disclosed, and a disclosed driver nothing gates on any more.
   A one-directional check would let the record quietly become a list of historical claims.

   The scan is over test source text. That is a real limitation — a dynamically-constructed module
   name would be missed — and it is accepted: every gate in this suite is a literal, the check's job
   is to catch the ordinary accident, and a heavier mechanism would cost more than the drift it
   prevents. Recorded here rather than discovered in review.

   `conftest.py`'s own `importorskip` for `psycopg` is inside the scan's reach, which is what makes
   the Postgres entry provable rather than asserted; `pgserver` is gated by a plain `try: import`
   and so is named in the record's Postgres entry as prose rather than as its own key (see the
   spec's last assumption).

5. **Marker-string README guards, not prose assertions.** Straight from
   `test_the_readme_documents_the_store` and `test_the_readme_keeps_the_load_bearing_warnings`:
   assert short, load-bearing substrings so the surrounding prose stays free to change. The markers
   chosen are the ones that carry the *decision* — that it is deliberate, why, and what the registry
   owner can do — not the ones that merely describe.

6. **What is deliberately not done.** No change to `pyproject.toml` (nothing to add or move; the
   `dev` extra already carries `pika` with a comment explaining why). No change to
   `tests/test_amqp.py` — the two tests are correct as written, and FR-053 exists to say so and keep
   a later round from "improving" them. No `-ra`, measured and rejected in the spec's Context.

## Test approach

Four groups, in the repository's existing idiom:

- **The hook's behaviour**, driven through pytest's own `pytester` fixture where a real subprocess
  run is needed, and directly on the rendering function otherwise. Directly is preferred: the
  function that turns "these drivers are missing" into lines is pure, so absence can be simulated
  without an environment that actually lacks them, and the present-driver cases (FR-045, SC-039)
  become testable in an environment where the drivers happen to be installed.
- **The detector itself**, per `test_repo_hygiene.py`'s explicit convention: without tests that
  stub the record and confirm the checks then fail, a check that always passes looks identical to a
  check that works.
- **The README guards**, parametrized over markers, skipping where there is no source tree (FR-054).
- **The dependency declarations** (SC-041), read from `pyproject.toml`, skipping where it is absent
  as `test_contract.py` and `test_migration_sql.py` already do.

Both environments are exercised: the bare venv the registry builds (the authoritative run, where
the note must appear), and a venv with `.[dev]` installed (where it must not, and where the two
FR-022 tests must execute and pass). The second is the only way to check SC-039 and FR-053's
present-driver half, and its absence is what let this gap exist in the first place.

## Complexity Tracking

No constitutional deviations. The one judgement worth flagging for review is the widening from
`pika` alone to the four-driver family, argued in the spec's Context: it is one table rather than a
second mechanism, and a `pika`-only version would be a half-truth about what a bare install omits.
