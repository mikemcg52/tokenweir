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
- **T019 measured, not assumed.** Counts below are as of the latest fix round; the figures recorded
  here after round 1 were left stale and review 2 (Low-5) caught it. Bare venv: **881 passed / 81
  skipped**, note lists five entries. Plus `pika` and `jsonschema`: both FR-022 tests execute and
  pass against pika 1.4.4, and those two entries drop out of the note. Full `.[dev]`: **959 passed /
  3 skipped** and **no note at all**.
- **T020 was run rather than asserted** — though not thoroughly enough the first time. Round 1
  exercised *one* README marker; review 2 (Med-1) showed two of the six were satisfied by prose
  elsewhere in the README and guarded nothing. Fix round 2 runs all six individually, plus the
  table-gutting case.

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

## Phase 8 (fix round 2): what review 2 found

- [x] **T029** *(Med-1)* Replace the two markers that pinned nothing. `"ADR-0001 Pillar 2"` and
      `"pip install -e '.[dev]'"` each appear three times in `README.md`, twice in prose predating
      this story, so FR-048's rationale paragraph and FR-049's option-2 hand-off could both be
      deleted with the suite green. Now keyed on substrings unique to the section, and **all six
      deletions were run** — each fails exactly one test. (FR-050, SC-038)
- [x] **T030** *(Med-1)* Give the table teeth on the forfeit column. `test_the_readme_lists_every
      _disclosed_driver` asserted only that the driver's name appeared somewhere after `## Develop`,
      so every "what goes unchecked" cell could be replaced with "TBD" in silence — the column the
      section exists for. Now each row must be a two-cell row whose second cell is substantive;
      gutting the table fails five tests. (FR-048)
- [x] **T031** *(Med-2)* Guard the whole terminal-summary body, not just the import probe. An
      availability predicate that raised escaped into pytest as an `INTERNALERROR` with a non-zero
      exit on a run whose tests had all passed, and the `psycopg` entry is already a non-import
      predicate — one entry from live. FR-046a added; the code comment claiming the property held
      "by construction" was false and is corrected. (FR-046a)
- [x] **T032** *(Med-3)* Test that the documentation checks skip rather than fail off a source tree,
      in `test_repo_hygiene.py`'s `pytest.raises(pytest.skip.Exception)` idiom that plan.md claimed
      this file already followed. Four added. (FR-054)
- [x] **T033** *(Med-3)* Delete `_require_source_tree()`. It could never fire — `TESTS_DIR` is the
      directory the calling file lives in — so it read as protection and provided none.
- [x] **T034** *(Low-1)* Record the residual: `pgserver` that imports but will not start is still
      unreported, because seeing that would mean starting a database inside a reporting hook.
      Written into FR-043a and the predicate's docstring rather than left to be rediscovered.
- [x] **T035** *(Low-2)* Name the record's boundary — Python packages only, not the `node` and `git`
      binaries the suite also gates on — so the note's silence about them is known, not accidental.
- [x] **T036** *(Low-3)* Correct plan.md: the Postgres entry keeps `gate="psycopg"` and does not use
      the `gate=None` hatch. The plan described the code it almost was.
- [x] **T037** *(Low-4)* Survive `--import-mode=importlib`, under which this file was the only thing
      on the branch that failed to collect. The conftest is imported normally with a load-by-path
      fallback, and the tests patch the module object rather than the string `"conftest"`.
- [x] **T038** *(Low-5)* Refresh the stale counts recorded above.

## Phase 9 (fix round 3): what review 3 found

- [x] **T039** *(Med-1)* Catch `SystemExit` in the hook's guard, not just in the probe. Review 2
      closed this at the probe and review 3 found it one level out: a predicate raising
      `SystemExit(3)` skipped the summary entirely and set the run's exit status to 3, on a run whose
      tests had all passed. The raising-predicate test is now parametrized over both escape routes.
      (FR-046, FR-046a)
- [x] **T040** *(Med-2)* Assert each README marker appears **exactly once**, not merely somewhere.
      FR-050 has required uniqueness since round 2 and nothing enforced it — `marker in text` passes
      on a match anywhere, which is precisely how two markers came to guard nothing in the first
      place. Verified by adding a second occurrence: the guard now fails.
- [x] **T041** *(Low-2)* Check the record-versus-README agreement in the other direction too. A row
      for a driver nobody discloses could sit in the table indefinitely; the record-versus-suite
      check has been bidirectional from the start for exactly this reason.
- [x] **T042** *(Low-3)* Inject a synthetic record into the subprocess tests so they stop skipping on
      a provisioned machine. The three checks FR-047 rests on were unexercised for any developer with
      `.[dev]` installed — a property of the hook should not be provable only on an
      under-provisioned machine. A complete-environment case was added the same way, so the silent
      path is now covered end to end everywhere too.
- [x] **T043** Add a check that every entry naming an `importorskip` gate is judged by that import,
      which holds on any machine. `psycopg` is exempted by name, since being judged on more than its
      gate is the whole point of FR-043a.
- [x] **T044** *(Low-4)* Correct plan.md in four places: the driver count (four → five), the probe's
      `except` clause, and above all the claim that FR-046 held "by construction rather than by
      care". Two reviews found ways out of it; it holds by a guard and a test per escape route.
- [x] **T045** *(Low-5)* Record that the note fires only when `tests/` is collected, so `pytest src/`
      prints nothing and exits 5 — a pass code for this project.
- [x] **T046** *(Low-1)* Note that a same-named directory on `sys.path` (the gitignored `build/`)
      satisfies the probe, and why that is left alone: `importorskip` is fooled identically, so the
      note goes on agreeing with the gate it reports on. A stricter probe would claim a forfeit for a
      check that actually ran, which is the worse failure.
- [x] **T047** *(Low-6)* No action: a dynamically-named gate is invisible to the scan, already
      accepted and recorded in plan.md.
