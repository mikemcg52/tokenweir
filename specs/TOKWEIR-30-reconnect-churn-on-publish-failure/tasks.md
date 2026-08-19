# Tasks: Bound reconnect churn when publishes fail on a healthy connection

**Input**: [spec.md](./spec.md), [plan.md](./plan.md)
**Jira**: TOKWEIR-30 (Bug), `Relates` TOKWEIR-6
**Branch**: `TOKWEIR-30-reconnect-churn-on-publish-failure`, cut from
`TOKWEIR-6-emitter-client-sink-source-amqp` at the developer's instruction

**Tests**: Required, and one of them is the deliverable — a bug with a reproduction in its ticket
must end with that reproduction as a test that fails on the parent commit.

---

## Phase 1: Setup

- [x] **T001** Point `.specify/feature.json` at this story's feature directory.
- [x] **T002** Reproduce the defect on the parent commit and record the numbers, so the fix is
      measured against an observed baseline rather than a described one.

---

## Phase 2: The fix

- [x] **T003** In `src/tokenweir/amqp.py`, add `_connection_published` to `__init__`, initialised
      `False` — including for the connection `from_url` opens, which has published nothing yet
      (FR-003, spec Edge Cases).
- [x] **T004** In `AMQPSink.emit`, set `_connection_published = True` on a successful publish
      (FR-002).
- [x] **T005** Reset `_connection_published` when a connection is discarded, so every connection
      starts unproven and the allowance renews per connection (FR-003). **Shipped in `_invalidate`,
      not in `_live_channel` as this task first said**: `_invalidate` is the only path that nulls
      `_channel`, so a reset in `_live_channel` was a second assignment that could be deleted with
      the whole suite green. One reset point, not two agreeing ones.
- [x] **T006** In `_invalidate`, arm `_next_reconnect_at` only when the connection being discarded
      never published (FR-001, FR-002).
- [x] **T007** Extend the `_invalidate` / `_live_channel` docstrings to state the rule and why it is
      not an exception-classification scheme (plan decision 1).

---

## Phase 3: Tests

- [x] **T008** In `tests/test_amqp.py`, the ticket's reproduction as a test: dials succeed, publishes
      always fail, 50 records inside one interval, assert at most one reconnect (SC-001).
- [x] **T009** Test that the pattern repeats at one dial per interval rather than per record once the
      interval elapses (User Story 1, scenario 2).
- [x] **T010** Test blip recovery: a productive connection that fails re-dials immediately with the
      clock not advanced (SC-002, FR-002).
- [x] **T011** Test that the allowance renews — two blips on two productive connections, both
      immediate (SC-003, FR-003).
- [x] **T012** Test that a connection which never published gets exactly one free dial and then backs
      off (spec Edge Cases).
- [x] **T013** Test that drops are still counted and logged with accurate reasons throughout
      (FR-006).
- [x] **T014** Test `reconnect_interval=0` still attempts on every publish (FR-007).
- [x] **T015** Confirm every existing AMQP test passes **unedited** (SC-004, FR-004, FR-005).

---

## Phase 4: Specs and documentation

- [x] **T016** Amend TOKWEIR-6's FR-024 in place to record the resolved ambiguity, matching the
      visible-original treatment FR-020 and FR-024 already carry there (FR-008).
- [x] **T017** Update `README.md`'s reconnect paragraph if it overstates what the interval bounds
      (FR-009 — no API change, but the prose must match the behaviour).

---

## Phase 5 (fix round 2): the bound made structural

- [x] **T020** Add `MAX_DIALS_PER_INTERVAL`, an unconditional cap on dials per interval, after
      review 2 showed the productivity rule is defeated by pika's asynchronous channel-close: the
      first publish to a missing exchange returns normally, so the rule is handed a phantom success
      every cycle and the fix was a measured no-op (FR-001a).
- [x] **T021** Add a channel double shaped like the real driver — first publish returns, later ones
      raise — and assert the bound against it. Every other double in this suite raises on the first
      call, which is the one shape pika will *not* produce for a 404, so nothing in the suite
      constrained the behaviour that mattered.
- [x] **T022** Pin SC-005's availability cost with a recovering-broker test, and amend the criterion
      for the second time — the first amendment fixed one half of a two-part false claim.

---

## Phase 6: Verification

- [x] **T018** Mutation-check **every** assignment the fix adds — not "both halves", which is what
      this task first said and what the round-1 commit claimed to have done. There are three, and
      the third (`_consecutive_unproductive = 0` on a productive connection) survived until review 1
      pointed at it. Each is now confirmed to fail a test when reverted.
- [x] **T019** Run the authoritative suite (`CI=true /workspace/repo/.venv/bin/pytest`, pass codes
      `[0, 5]`) and `ruff check`.
