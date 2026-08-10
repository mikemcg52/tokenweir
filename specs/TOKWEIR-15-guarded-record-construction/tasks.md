# Tasks: Guarded record construction on the metered request path

**Input**: [spec.md](./spec.md), [plan.md](./plan.md)
**Jira**: TOKWEIR-15 (Story), `Relates` TOKWEIR-4
**Branch**: `TOKWEIR-4-usage-record-contract` (continued, per the developer's instruction)

**Tests**: Required. The story's acceptance is literally "a test proving a malformed record cannot
fail the request being metered", so the test module is the deliverable, not a trailing chore.

**Organization**: Grouped by user story. `[P]` marks tasks that touch disjoint files and could run
in parallel.

---

## Phase 1: Setup

- [x] **T001** Record the feature directory in `.specify/feature.json` so the speckit scaffold points
      at this story rather than TOKWEIR-12.

---

## Phase 2: Foundational — the seam itself

**Blocking**: every user story below depends on these.

- [x] **T002** In `src/tokenweir/sink.py`, add the module logger (`logging.getLogger(__name__)`) and
      the internal, self-guarded log helper that emits a `WARNING` with `exc_info=True` and cannot
      itself raise (FR-006, FR-008, FR-009).
- [x] **T003** In `src/tokenweir/sink.py`, add `build_record(**fields) -> Optional[UsageRecord]`:
      construct under `except Exception`, log and return `None` on failure (FR-001, FR-004, FR-005,
      FR-010).
- [x] **T004** In `src/tokenweir/sink.py`, add `emit_record(sink, record) -> bool`: emit under
      `except Exception`, log and return `False` on failure (FR-002, FR-004, FR-012).
- [x] **T005** In `src/tokenweir/sink.py`, add `emit_usage(sink, **fields) -> Optional[UsageRecord]`
      composed of T003 + T004 — no third implementation of either guarantee (FR-003).
- [x] **T006** Extend the `sink.py` module docstring to explain why an emit-side module owns a
      construction guard, and that this closes ADR-0001 Pillar 2's gap rather than relaxing the
      `Sink` contract (FR-011, FR-012).
- [x] **T007** Export `build_record`, `emit_record` and `emit_usage` from `src/tokenweir/__init__.py`
      and add them to `__all__` (FR-013).

---

## Phase 3: User Story 1 — a malformed record cannot fail the metered request (P1)

**Goal**: every invalid-input class TOKWEIR-4 rejects becomes a drop, not a raise.
**Independent test**: `pytest tests/test_guarded_emit.py -k malformed`

- [x] **T008** Create `tests/test_guarded_emit.py` with a valid-fields helper mirroring
      `tests/test_contract.py`'s `_record`, and a `RaisingSink` / recording-sink pair of doubles.
- [x] **T009** Parametrized test over the full invalid-input matrix — blank identity field,
      whitespace-only identity field, non-string identity field, negative count, non-integer count,
      `bool` count, wrong-typed optional, bad `pricing_mode`, bad `schema_version`, unknown keyword —
      asserting `emit_usage` returns `None` and raises nothing (SC-001, FR-005).
- [x] **T010** Test that omitting a required argument entirely — the `TypeError` path, distinct from
      every `ValueError` above — is also a drop (US1 scenario 4, FR-005).
- [x] **T011** Test that a dropped record reaches no sink: the recording sink saw zero records.
- [x] **T012** Test the happy path: `emit_usage` returns a record equal to `UsageRecord(**fields)`
      and the sink received that same record — the guard adds no normalization (SC-005, FR-010).
- [x] **T013** Test that `KeyboardInterrupt` from a construction-time hook propagates rather than
      being swallowed, proving `BaseException` is not caught (FR-004).

---

## Phase 4: User Story 2 — a misbehaving sink cannot fail the request either (P1)

**Goal**: a `Sink` that violates its own contract still cannot reach the metered request.
**Independent test**: `pytest tests/test_guarded_emit.py -k sink`

- [x] **T014** Test `emit_record` against a sink whose `emit` raises: returns `False`, raises nothing
      (SC-002, FR-002).
- [x] **T015** Test `emit_usage` against the same raising sink with *valid* fields: returns `None`,
      raises nothing, and the drop is logged as an emission failure — not a construction failure
      (US2 scenario 2, FR-007).
- [x] **T016** Test that a sink raising `BaseException` propagates (FR-004, spec edge case).

---

## Phase 5: User Story 3 — a dropped record is visible, never silent (P1)

**Goal**: no silent metering holes.
**Independent test**: `pytest tests/test_guarded_emit.py -k log`

- [x] **T017** Test via `caplog` that a construction drop emits exactly one `WARNING` on a
      `tokenweir` logger, and that the record carries the underlying exception (SC-003, FR-006).
- [x] **T018** Test that the construction-drop and emission-failure messages are distinguishable
      (FR-007).
- [x] **T019** Test that the library adds no handler and sets no level on the root or `tokenweir`
      loggers — importing and calling must not configure logging for the application (FR-008).
- [x] **T020** Test that a logger which raises on `warning()` does not make a guarded call raise
      (SC-004, FR-009).

---

## Phase 6: User Story 4 — construction and emission guardable separately (P2)

**Goal**: the stamping/batching caller gets the guarantee without hand-rolling it.
**Independent test**: `pytest tests/test_guarded_emit.py -k halves`

- [x] **T021** Test `build_record` alone: `None` for malformed input, a record for valid input, and
      no sink involved (FR-001).
- [x] **T022** Test that build-then-emit and the fused `emit_usage` agree on outcome for the valid
      case, the malformed-input case, and the raising-sink case (US4 scenario 3, FR-003).

---

## Phase 7: Contract non-regression

**Goal**: prove this story changed nothing it was not supposed to.

- [x] **T023** Test that `UsageRecord(...)` still raises on the same invalid inputs the guard now
      swallows — the guard is beside the contract, not a softening of it (FR-011).
- [x] **T024** Confirm `SCHEMA_VERSION` is unchanged and `schema/usage-record.v1.json` is untouched;
      the existing `tests/test_schema.py` drift check carries this (SC-007, FR-015).
- [x] **T025** Confirm the core still imports no third-party module; the existing dependency-hygiene
      test in `tests/test_contract.py` carries this for `contract.py` — extend its scope to `sink.py`
      if it does not already cover it (FR-014).

---

## Phase 8: Documentation & polish

- [x] **T026** [P] Add a README section documenting the guarded seam: the three calls, the return
      contract, the log behaviour, and — explicitly — when *not* to use it (off the request path,
      where raising is correct) (FR-016).
- [x] **T027** [P] Note in the README's contract section that construction raising is safe on a
      request path *because* the guard exists, linking the two decisions rather than leaving the
      reader to reconcile them.
- [x] **T028** Run the authoritative suite: `CI=true /workspace/repo/.venv/bin/pytest` (SC-006).
- [x] **T029** Run `ruff check .` (SC-008).
- [x] **T030** Verify `git status` is clean after the run — the TOKWEIR-12 hygiene guard must stay
      green.

---

---

## Phase 9: Fix round 1 (findings from review 1)

- [x] **T031** *(Med-1)* Attach a `NullHandler` to the `tokenweir` package logger in
      `src/tokenweir/__init__.py`. Without it, `logging.lastResort` writes a traceback per drop to
      the stderr of an application that configured no logging — the output decision FR-008 forbids
      the library from taking. Record the trade-off in plan.md and amend FR-008/US3.3, which
      contradicted each other on stdout vs stderr (FR-008, FR-017).
- [x] **T032** *(Med-1)* Subprocess tests pinning both directions: an unconfigured interpreter gets
      nothing on stdout/stderr and still observes the drop via the return value; an interpreter that
      calls `basicConfig` gets the warning and the cause (SC-009).
- [x] **T033** *(Med-2)* Parametrize the log assertion over the whole malformed matrix **and** over
      `REQUIRED_FIELDS`, asserting exactly one `WARNING` with `exc_info` per case, and that the
      missing-argument cases carry a `TypeError`. Asserting only the return value left the entire
      `TypeError` half free to go quiet (SC-003).
- [x] **T034** *(Med-3)* `emit_record` refuses anything that is not a `UsageRecord` — returns
      `False`, logs without a bogus traceback, and the sink receives nothing. Test the naive
      build-then-emit composition with no `is not None` check (FR-018, SC-010).
- [x] **T035** *(Low-1)* Test that `emit_usage` delegates to `build_record` and `emit_record` rather
      than reimplementing them, so FR-003's structural claim is enforced, not just its behaviour.
- [x] **T036** *(Low-2)* Reword the emission message to "the sink failed" — the same guard catches a
      missing sink, where "raised" is untrue — and test that a `None` sink is described honestly.
- [x] **T037** *(Low-5)* Note in the README that gateway/emitter adoption is still in progress, so a
      reader does not infer the seam is already wired up.
- [x] **T038** Re-run the authoritative suite and `ruff check .`.

**Not fixed, and why:** Low-3 (no test for the README) — the reviewer flagged it for completeness
and documentation tests are not worth the coupling. Low-4 (no throttling on drop logging) — FR-006
*requires* `exc_info`, so per-drop tracebacks are compliant by design; rate-limiting is an
operational concern for the consumers, and is carried to TOKWEIR-6/-10 rather than solved here.

---

## Phase 10: Fix round 2 (findings from review 2)

- [x] **T039** *(Med-1)* Make `emit_usage`'s `sink` positional-only. A `**fields` mapping carrying
      the key `"sink"` collided with the parameter and raised `TypeError` during argument binding,
      before any guard ran — the last route by which a producer's data could reach the metered
      request. Add `field_named_sink` and `field_named_self` to the malformed matrix, a direct
      regression test, and a signature test pinning the mechanism (FR-019).
- [x] **T040** *(Med-2)* Add an acceptance-clause traceability table to spec.md separating the
      clause this branch meets from the one TOKWEIR-6/-10 carry, so closing the story is a decision
      made on the record rather than by default. Carried into the run report and a Jira comment.
- [x] **T041** *(Low-1)* Amend FR-006 to require the underlying exception "where one was caught",
      so the non-record refusal path — which has no exception to carry — no longer contradicts it.
- [x] **T042** *(Low-2)* Qualify `emit_usage`'s "Never raises" docstring with the `BaseException`
      carve-out the two halves already state.
- [x] **T043** *(Low-3)* Widen `emit_record`'s `record` annotation to `object`, so the naive
      composition FR-018 exists to make safe also type-checks for an adopter running mypy/pyright.
- [x] **T044** *(Low-4)* Add a guard test asserting the `ExplodingRepr` trigger genuinely raises
      `KeyboardInterrupt` through the constructor, so the FR-004 construction test cannot pass
      vacuously if TOKWEIR-4's error messages ever stop interpolating the value.
- [x] **T045** Re-run the authoritative suite and `ruff check .`.

---

## Dependencies

- Phase 2 (T002–T007) blocks Phases 3–7.
- T008 blocks T009–T022 (it creates the test module and its doubles).
- Phase 8 runs last.

## Out of scope

- Making the AI Gateway call the seam — TOKWEIR-10, a different repository.
- Making the emitter client call the seam — TOKWEIR-6, not yet on this branch.
- Any metrics counter inside `tokenweir` — the return value is the hook; a metrics dependency would
  breach ADR-0001 Pillar 2.
- Stamping `ts` or measuring `latency_ms` — the producer's job; the guard converts failures into
  drops, it does not enrich records.
