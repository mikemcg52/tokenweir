# Tasks: Emitter client + Sink/Source interfaces + AMQP adapter

**Input**: [spec.md](./spec.md), [plan.md](./plan.md)
**Jira**: TOKWEIR-6 (Story)
**Branch**: `TOKWEIR-6-emitter-client-sink-source-amqp`

**Tests**: Required. The story's acceptance is three testable claims — both sink paths work, the
core imports without transport libraries, and an emit failure never raises — so the test modules
are the deliverable, not a trailing chore.

**Organization**: Grouped by user story. `[P]` marks tasks that touch disjoint files and could run
in parallel.

---

## Phase 1: Setup

- [x] **T001** Point `.specify/feature.json` at this story's feature directory so the speckit
      scaffold resolves TOKWEIR-6 rather than TOKWEIR-15.

---

## Phase 2: Foundational — the batch seam

**Blocking**: the emitter and the direct sink both depend on these.

- [x] **T002** In `src/tokenweir/sink.py`, add the `BatchSink` protocol (`emit_batch(records)`),
      `runtime_checkable`, documented as an *optional* capability that does not alter `Sink`
      (FR-018).
- [x] **T003** In `src/tokenweir/sink.py`, extend the module docstring: why a batch capability is
      additive rather than a protocol change, and why a sink that has it must still honour
      "never raise from emit" (FR-018).

---

## Phase 3: User Story 1 — a metered service never blocks or fails on metering (P1)

**Goal**: the buffered client. The emitting call appends and returns; nothing about the sink,
broker or network can reach the caller.
**Independent test**: `pytest tests/test_emitter.py`

- [x] **T004** Create `src/tokenweir/emitter.py` with the module docstring stating the contract it
      implements (ADR-0001 Pillar 2), and the bounded-buffer and no-retry policies with their
      rationale (FR-003, FR-005).
- [x] **T005** In `emitter.py`, add `EmitterStats` — a frozen snapshot carrying accepted,
      delivered, dropped-buffer-full, dropped-not-a-record and failed counts (FR-007).
- [x] **T006** In `emitter.py`, add `BufferedEmitter.__init__(sink, *, max_buffer, batch_size,
      linger, close_timeout, warn_interval, name)` with validated arguments, the buffer, the
      `Condition`, and the counters (FR-001, FR-003, FR-004). **Shipped with a `list`, not the
      planned `deque`** — see plan decision 2 for why slice-removal under the lock won.
- [x] **T007** In `emitter.py`, add the worker thread: daemon, started lazily or on construction,
      draining up to `batch_size` under the lock and delivering outside it (FR-001, FR-012).
- [x] **T008** In `emitter.py`, add `emit(record)`: type-check, append, drop-newest when full,
      count, notify the worker, return without waiting; never raise except `BaseException`
      (FR-002, FR-003, FR-006, FR-014).
- [x] **T009** In `emitter.py`, add delivery: prefer `emit_batch` when the sink offers it, else
      per-record `emit`; guard both, count failures, never retry, never let the worker die
      (FR-005, FR-018).
- [x] **T010** Add the rate-limited warning helper so a systematically broken sink cannot log once
      per metered record, reporting the suppressed count (FR-008). **Shipped as its own private
      module, `src/tokenweir/_ratelimit.py`, not inside `emitter.py`** — `DirectSink` and `AMQPSink`
      need the same thing, and the alternatives were three copies or cross-module private access
      (plan decision 8). Review round 1 then made the window **per reason** rather than per warner:
      a shared window silences a new failure mode for a whole interval and misattributes the
      suppressed count to whichever message gets through next.
- [x] **T011** In `emitter.py`, add `flush(timeout=None) -> bool` blocking until drained or timed
      out, reporting which (FR-009).
- [x] **T012** In `emitter.py`, add `close(timeout=…)`: stop, bounded final flush, close the sink
      guarded, idempotent; plus `__enter__`/`__exit__` and the `atexit` best-effort flush that must
      not keep the client alive (FR-010, FR-011, FR-012).
- [x] **T013** In `emitter.py`, make emitting after `close()` a counted drop rather than a raise
      (FR-002, spec Edge Cases).
- [x] **T014** Create `tests/test_emitter.py`: record helper and sink doubles — recording, raising,
      blocking, batch-capable, and one that raises from `close()`.
- [x] **T015** In `tests/test_emitter.py`, test the never-raises matrix: raising sink, blocking
      sink, `None` sink, full buffer, post-close emit, non-record value (SC-003, FR-002, FR-006).
- [x] **T016** In `tests/test_emitter.py`, test the bound: more than `max_buffer` records against a
      stalled sink leaves the buffer at its bound and the excess counted (SC-004).
- [x] **T017** In `tests/test_emitter.py`, test batching: `batch_size` respected, an idle client
      makes no delivery call, `emit_batch` preferred when present and per-record `emit` used when
      not (FR-004, FR-018, SC-006).
- [x] **T018** In `tests/test_emitter.py`, test lifecycle: double `close()`, `close()` on a
      never-started client, `close()` flushes what is buffered, context manager, sink `close()`
      raising (SC-008, FR-010, FR-011).
- [x] **T019** In `tests/test_emitter.py`, test concurrency: 10 threads emitting deliver exactly the
      number emitted, none lost or duplicated (SC-005, FR-013).
- [x] **T020** In `tests/test_emitter.py`, test that a failing sink produces a bounded number of log
      records, not one per emit (SC-010, FR-008).
- [x] **T021** In `tests/test_emitter.py`, test that a process creating a client and exiting without
      closing it terminates, asserted in a subprocess (SC-009, FR-012).
- [x] **T022** In `tests/test_emitter.py`, test that the client satisfies `Sink` and composes with
      `emit_usage(client, fields)` (FR-014).

---

## Phase 4: User Story 3 — the broker-less direct path (P1)

**Goal**: emit through the same client with no broker, onto a `Source`.
**Independent test**: `pytest tests/test_direct_sink.py`

- [x] **T023** In `src/tokenweir/sink.py`, add `DirectSink(source, *, owns_source=False)`
      implementing `emit` and `emit_batch` over `Source.write`, never raising, counting and logging
      store failures (FR-015, FR-016, FR-017).
- [x] **T024** In `sink.py`, give `DirectSink.close()` the same borrowed-vs-owned ownership rule
      `PostgresSource` uses, and make it idempotent (FR-023 by analogy).
- [x] **T025** Create `tests/test_direct_sink.py`: write-through to `MemorySource`, one `write` call
      per batch rather than per record, a raising source contained, `close()` ownership and
      idempotence (SC-006, FR-015, FR-016, FR-017).
- [x] **T026** In `tests/test_direct_sink.py`, test the end-to-end broker-less path: a client over a
      `DirectSink` over a `MemorySource` delivers every emitted record (SC-001, User Story 3).

---

## Phase 5: User Story 2 — the AMQP homelab path (P1)

**Goal**: records reach RabbitMQ as JSON, with `pika` absent from a bare install.
**Independent test**: `pytest tests/test_amqp.py`

- [x] **T027** Create `src/tokenweir/amqp.py` with the module docstring explaining the deferred
      `pika` import, the ownership rule and the reconnect policy (FR-020, FR-023, FR-024).
- [x] **T028** In `amqp.py`, add the pure `message_for(record) -> bytes` mapping and the publish
      properties helper, both usable with no `pika` installed (FR-019, FR-022, FR-025).
- [x] **T029** In `amqp.py`, add `AMQPSink(channel, *, exchange, routing_key, properties, ...)`
      taking a caller-supplied channel, with an `emit` that never raises (FR-019, FR-023).
      **No `emit_batch`, deliberately**: AMQP has no batch publish, so one here would be a loop
      wearing a costume, and leaving it off is what exercises the emitter's per-record fallback
      (FR-018). The task as written said to add one; not writing it is the decision.
- [x] **T030** In `amqp.py`, add `AMQPSink.from_url(...)` and raise an `ImportError` naming
      `tokenweir[amqp]` when the driver is absent (FR-020, FR-021). **The import lands in
      `__init__`, not in `from_url` alone** — deferring past construction puts it on the publish
      path, which may not raise, so a missing driver became a silent permanent drop (review round 1,
      High). See the FR-020 amendment in `spec.md`.
- [x] **T031** In `amqp.py`, add reconnection for a connection the sink **can re-make**:
      re-establish on a later publish attempt, not by retrying inside one (FR-024). Two corrections
      from review round 3, both Med: gating this on *ownership* was wrong, because only `from_url`
      supplies the callable that can reconnect — so `AMQPSink(channel, connection=c,
      owns_connection=True)` closed its connection and was then permanently dead. And attempts are
      now spaced by `reconnect_interval`: a blocking dial per record is the retry loop this design
      refuses, reintroduced one attempt at a time.
- [x] **T032** In `amqp.py`, add `close()` honouring the ownership rule and never raising (FR-023).
- [x] **T033** Create `tests/test_amqp.py`: the mapping tests, which must pass with no `pika`
      installed (SC-007, FR-025).
- [x] **T034** In `tests/test_amqp.py`, test publishing against a recording channel double —
      exchange, routing key, body, persistent delivery mode, JSON content type (FR-019, FR-022).
- [x] **T035** In `tests/test_amqp.py`, test that a publish failure does not raise and is counted,
      and that an owned connection is re-established on the next attempt (FR-024, SC-003).
- [x] **T036** In `tests/test_amqp.py`, test that `from_url` without `pika` names the extra, via
      `sys.modules["pika"] = None` (FR-021).
- [x] **T037** In `tests/test_amqp.py`, test the end-to-end AMQP path: a client over an `AMQPSink`
      over a channel double delivers every emitted record (SC-001, User Story 2).

---

## Phase 6: User Story 4 — the core stays dependency-light (P1)

**Goal**: `pip install tokenweir` carries no transport library and `import tokenweir` imports none.

- [x] **T038** In `tests/test_amqp.py`, assert in a subprocess that importing `tokenweir` and every
      core module leaves `pika` out of `sys.modules` (SC-002, FR-026).
- [x] **T039** In `tests/test_amqp.py`, assert `AMQPSink` is not exported from the `tokenweir`
      namespace, mirroring the store's treatment (FR-027).
- [x] **T040** Extend the AST sweep's anti-vacuity guard in `tests/test_contract.py` to name
      `emitter.py`, `amqp.py` and `_ratelimit.py`. The sweep's `rglob` already covered them, which
      is what this task originally concluded — but the explicit set exists precisely so a glob that
      silently stopped matching would fail rather than make the hygiene check vacuously pass, and
      leaving the newest modules out of it defeated that (review round 1, Med).

---

## Phase 7: Wiring and documentation

- [x] **T041** Export `BufferedEmitter`, `EmitterStats`, `DirectSink` and `BatchSink` from
      `src/tokenweir/__init__.py`, add them to `__all__`, and update the package docstring — while
      leaving `AMQPSink` out of the namespace (FR-027).
- [x] **T042** [P] Update `README.md`: the emitter client, the bounded-buffer drop policy, the
      no-retry policy, the direct sink, the AMQP adapter and the extras table; and retire the
      "Adoption is in progress … the emitter client (TOKWEIR-6)" note now that it has landed
      (FR-028).
- [x] **T043** [P] Update `docs/adr-0001-token-weir.md`'s execution checklist for the transport
      adapters / emitter client item now delivered.

---

## Phase 8: Verification

- [x] **T044** Run the authoritative suite from `/workspace/.mado/project.yaml`
      (`CI=true /workspace/repo/.venv/bin/pytest`, passing exit codes `[0, 5]`) and confirm green.
      Verified in **both** environments: MADO's own (`pip install -e . pytest`, no `pika`) and with
      the `dev` extra's `pika` present, since review round 3 noted FR-022 was asserted only against
      a test double. The two real-driver tests skip in the first and pass in the second, and the
      dependency-light subprocess assertions pass in both — which is stronger evidence with `pika`
      installed than without, because then the import genuinely could happen and does not.
- [x] **T046** Close the three test holes review round 4 found by mutation — the linger deadline,
      the guarded `emit_batch` path, and the WARNING on the two close-related drop reasons — each
      verified by re-running the mutation and watching it fail (FR-004, FR-005, FR-008, FR-018).

- [x] **T045** Run `ruff check` against the new modules for the lint rules
      `pyproject.toml` declares (`E`, `F`, `I`, `N`, `W`, line length 100) — clean.
      **`ruff format` is not this project's gate** and was not applied: 19 files already
      on `main`, `contract.py` among them, would be reformatted by it, so running it here
      would bury the story's diff under a whole-repo reformat. Recorded rather than
      silently skipped.
