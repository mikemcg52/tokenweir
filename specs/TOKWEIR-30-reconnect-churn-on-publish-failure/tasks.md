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
- [x] **T005** In `_live_channel`, reset `_connection_published = False` when a dial establishes a
      new connection, so the allowance renews per connection (FR-003).
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

## Phase 5: Verification

- [x] **T018** Mutation-check both halves of the fix: revert the arming condition and revert the
      productive flag, and confirm a test fails each time.
- [x] **T019** Run the authoritative suite (`CI=true /workspace/repo/.venv/bin/pytest`, pass codes
      `[0, 5]`) and `ruff check`.
