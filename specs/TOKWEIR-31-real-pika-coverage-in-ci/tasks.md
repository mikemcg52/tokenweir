# Tasks: Make the authoritative run admit what it did not prove

**Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)
**Jira**: TOKWEIR-31 (Story), `Relates` to TOKWEIR-6

Ordering note: the disclosure record comes first because the terminal note, the README markers and
the consistency check are all rendered from or keyed to it.

## Phase 1: Setup

- [x] **T001** Record the pre-change baseline of the authoritative command
      (`CI=true .venv/bin/pytest`) so the story's own claims about it are measured, not recalled.
      *Done: 814 passed, 81 skipped, exit 0; `import pika` raises `ModuleNotFoundError`.*
- [x] **T002** Confirm `/etc/mado/projects.yaml` is unreachable from the pod and that no
      registry-editing CLI is present, since the choice of option 3 rests on it.
      *Done: no such directory; only `mado-phase` and `mado-notify` on PATH.*

## Phase 2: The disclosure record

- [x] **T003** Add `OPTIONAL_DRIVERS` to `tests/conftest.py`: module name → the claim its absence
      forfeits. Entries for `pika` (FR-022 against the real driver), `psycopg` (the real-store
      suite, naming `pgserver`/`TOKENWEIR_TEST_DSN`), `pglast` (migration SQL against the server's
      own parser) and `build` (SC-001, the migrations reaching a wheel). (FR-043)
- [x] **T004** Add the absence probe: import each key, treat any exception as absent, return the
      missing ones in the record's order. (FR-043, FR-046)
- [x] **T005** Add the pure renderer that turns a set of missing drivers into the disclosure lines —
      a header naming what the run did not prove, then one line per driver. Empty output when
      nothing is missing. (FR-044, FR-045)

## Phase 3: The terminal note

- [x] **T006** Add `pytest_terminal_summary` to `tests/conftest.py`, writing the rendered lines
      through the terminal reporter. No return value, no exit-status effect, no dependence on
      results or on which tests were selected. (FR-046, FR-047)
- [x] **T007** Verify by running the authoritative command: the note appears, names `pika` and
      FR-022, and the exit status and pass/skip counts are otherwise unchanged from T001.
      (SC-036, SC-037, SC-040)

## Phase 4: The written-down decision

- [x] **T008** Rewrite `README.md` "Develop": what a bare `pip install -e .` omits, what each
      omission forfeits, that this is accepted deliberately, and why — the core is transport-free by
      contract, so the drivers cannot be required. (FR-048)
- [x] **T009** Add the registry hand-off to the same section: the change that would close the gap
      for whoever owns `/etc/mado/projects.yaml`, so the ticket's options 1 and 2 stay actionable by
      the party that can act on them. (FR-049)

## Phase 5: Tests

- [x] **T010** New `tests/test_optional_drivers.py`. The renderer: names every missing driver and
      its forfeited claim; `pika`'s line names FR-022; produces nothing when none are missing.
      (FR-043, FR-044, FR-045, SC-039)
- [x] **T011** The probe: a module that raises a non-`ImportError` on import counts as absent and
      does not propagate. (FR-046)
- [x] **T012** The hook end to end in a **subprocess** — not `pytester`, which would mean
      registering that plugin for every run: the note reaches the output of a real run, and that
      run's exit status is unaffected. (FR-046, FR-047)
- [x] **T013** The bidirectional consistency check: the set of modules gated by `importorskip`
      across `tests/` equals the record's keys. (FR-051)
- [x] **T014** *The detector itself*, per `test_repo_hygiene.py`'s convention: with a stubbed record
      missing an entry the check fails, and with a stubbed record carrying an entry nothing gates on
      it fails. Without these, a check that always passes is indistinguishable from one that works.
      (SC-038)
- [x] **T015** README marker guards, parametrized, skipping with no source tree. (FR-050, FR-054)
- [x] **T016** The dependency guard: `pika` appears in no runtime dependency and in no extra beyond
      `amqp` and `dev`; a core install pulls in no transport or database driver. Read from
      `pyproject.toml`, skipping where absent. (FR-052, SC-041)
- [x] **T017** Confirm `tests/test_amqp.py` is unchanged and the two FR-022 tests still skip cleanly
      with `pika` absent. (FR-053)

## Phase 6: Verification

- [x] **T018** Authoritative command green in the bare venv, with the note present, and the counts
      reconciled against T001 plus the tests this story adds. (SC-036, SC-037, SC-040)
- [x] **T019** A `.[dev]` venv: no disclosure line for `pika`, and both FR-022 tests execute and
      pass. This is the environment whose absence let the gap exist. (SC-039, FR-045, FR-053)
- [x] **T020** Negative checks run for real, not asserted: delete a README marker and observe the
      failure; add an unlisted `importorskip` and observe the failure; then revert both. (SC-038)
- [x] **T021** `ruff check .` clean.

## Notes from the run

- **T003 found a fifth driver rather than four.** The record was drafted with `pika`, `psycopg`,
  `pglast` and `build`; T013's consistency check immediately failed on `jsonschema`, which
  `test_schema.py` gates the same way and which is equally absent from the authoritative install.
  It is the check's first catch and the clearest argument for having written it.
- **T013 also caught this story's own test file twice** — first the fixture that spells out a gate
  literally, then the comment explaining why the fixture must not. Both are recorded in
  `test_optional_drivers.py`; the fixture now composes the call text at runtime.
- **T019 measured, not assumed.** Bare venv: 856 passed / 81 skipped, note lists five drivers.
  Plus `pika` and `jsonschema`: 880 passed / 57 skipped, both FR-022 tests execute and pass against
  pika 1.4.4, and those two entries drop out of the note. Full `.[dev]`: 934 passed / 3 skipped and
  **no note at all**.
- **T020 was run rather than asserted.** Removing a README marker, adding an undisclosed gate, and
  leaving a stale entry each turned the suite red with a message naming the fix; all three were
  reverted and the suite returned to 856 / 81.

## Phase 7 (fix round 1): what review 1 found

- [x] **T022** *(Med-1)* Test `missing_optional_drivers` itself, against real modules and not only
      stubs. The predicate that decides whether an entry is missing had no test that would fail if it
      broke: rewriting it to disclose every entry left the file green and printed "pika is not
      installed" on a machine with pika 1.4.4. Four tests added; the mutation now fails three.
      (FR-045, SC-038, SC-039)
- [x] **T023** *(Med-2)* Judge the Postgres entry on the condition its own fixtures impose —
      psycopg **and** a DSN or `pgserver` — not on importability. On a machine with the driver and no
      server the note went silent while 43 tests carried on skipping, which is this story's own bug
      one entry over. Entries now carry a `label` as well as a claim, because "psycopg is not
      installed" is the wrong sentence when psycopg is installed. Verified with a stub psycopg on
      `PYTHONPATH`: the note still reports Postgres. (FR-043a)
- [x] **T024** *(Low-1)* Reword the header. It said "A green result above does not cover the
      following" — false on the red and interrupted runs the note deliberately fires on, and wrong
      about "above" even on a passing run, since the note precedes the summary line. Now claims
      nothing about the result, and a test asserts it does not.
- [x] **T025** *(Low-2)* Restate SC-037 with the measured number (9 lines against `-ra`'s 43, which
      is 4.8× and not an order of magnitude) and give the test an absolute ceiling, since one line
      per entry is satisfied at any size.
- [x] **T026** *(Low-3)* Reconcile plan.md and T012 with the subprocess implementation that shipped.
- [x] **T027** *(Low-4, Low-5)* Replace the regex scanner with an AST walk: it cannot be fooled by a
      gate that is merely written about in a docstring, comment or fixture — the trap that caught
      this story's own test file twice — and it recurses into subdirectories. Tests for all three.
- [x] **T028** *(Low-6)* Catch `SystemExit` in the probe, and amend FR-046 to say `KeyboardInterrupt`
      deliberately propagates rather than leaving `except Exception` looking like an oversight.
