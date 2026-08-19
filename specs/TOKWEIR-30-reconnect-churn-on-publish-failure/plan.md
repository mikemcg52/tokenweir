# Implementation Plan: Bound reconnect churn when publishes fail on a healthy connection

**Branch**: `TOKWEIR-30-reconnect-churn-on-publish-failure` (cut from
`TOKWEIR-6-emitter-client-sink-source-amqp` at `36ae6de`) | **Date**: 2026-08-19 |
**Spec**: [spec.md](./spec.md)

## Summary

One flag and two small edits in `AMQPSink`. Track whether the current connection has ever
successfully published; arm `reconnect_interval` on invalidation **only when it has not**. That
makes a lost-but-working connection recover instantly and a never-working one back off, which is the
distinction the ticket asks to be settled and the one that actually separates a blip from a
misconfiguration.

Everything else in the reconnect path — the dial-failure arm, the borrowed-channel early return, the
three drop reasons, the redaction — is untouched.

## Technical Context

**Language/Version**: Python 3.11+.
**Primary Dependencies**: none added. `pika` remains behind the `amqp` extra and is imported in one
place.
**Testing**: pytest per `/workspace/.mado/project.yaml` — `CI=true /workspace/repo/.venv/bin/pytest`,
pass codes `[0, 5]`. New tests use the existing `fake_pika` fixture and an injected clock; no broker.
**Project Type**: Library (single package, `src/` layout).
**Performance Goals**: the point of the change — bound dials to one per `reconnect_interval` on the
publish-failure path, from one per record.
**Constraints**: no public API change; no new import; every existing AMQP test must pass **unedited**,
since editing one to accommodate this fix would hide a regression rather than reveal it.
**Scale/Scope**: ~10 lines of `amqp.py`, plus tests and two spec amendments.

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled speckit template, as recorded in TOKWEIR-6's
plan; ADR-0001's pillars are the gate instead. This change touches Pillar 2 only (transport
adapter behaviour) and strengthens it — a blocking dial per metered record on the delivery worker is
the emit path costing the metered system something, which is the thing Pillar 2 forbids.

## Project Structure

```text
src/tokenweir/amqp.py    # _invalidate, _live_channel, emit — the only source file
tests/test_amqp.py       # new cases alongside the TOKWEIR-6 reconnect tests
specs/TOKWEIR-30-.../    # spec.md, plan.md, tasks.md
specs/TOKWEIR-6-.../spec.md   # FR-024 amended to record the resolved ambiguity (FR-008)
```

**Structure Decision**: No new module. The bug is a policy error inside one class, and the fix is
state that class already had the natural home for.

## Key design decisions

0. **One free re-dial before the interval arms** — see the FR-002a amendment in `spec.md`. The
   first draft armed on any unproductive connection and broke a TOKWEIR-6 test that was correct;
   "never published" cannot distinguish a reset-before-first-publish from a bad exchange, and one
   retry resolves that for one dial. Recorded here because it is the only place this plan's original
   reasoning was wrong, and the test that caught it is the reason the plan said not to edit tests.

1. **The gate is "was this connection productive?", not "what kind of error was it?".**
   The precise alternative is to classify pika's exceptions — channel-level (`NOT_FOUND`,
   `ACCESS_REFUSED`) versus connection-level — and re-dial only for the latter. Rejected on two
   counts. It couples the adapter to a driver exception hierarchy that this module imports nothing
   from, breaking the discipline that `pika` appears in exactly one function; and it needs
   broker-specific reply codes to stay accurate, so it rots silently when a broker or a driver
   changes. "Did this connection ever publish a record?" needs no driver knowledge, cannot go stale,
   and answers the question that actually matters — whether re-dialling can plausibly help.

2. **Arm on invalidation, not on publish failure.** The flag is consulted in `_invalidate`, which is
   already the one place that decides a connection is finished with. Putting the decision in `emit`
   would spread reconnect policy across two methods, and `emit` has no business knowing about
   intervals.

3. **The allowance renews per productive connection, not per sink.** Both resets live in
   `_invalidate`, which is the single place a connection is discarded: `_connection_published`
   clears there so the next connection starts unproven, and `_consecutive_unproductive` clears there
   when the connection being discarded had published. (An earlier draft of this plan said the resets
   happened in `_live_channel` and "on any successful publish" respectively — behaviourally the
   same, but it would send a reader chasing them to the wrong function.) So a sink that blips, recovers, publishes, and blips again gets an immediate retry both
   times — the right behaviour for a flaky network — while a sink whose connections keep failing to
   publish gets one free dial and then backs off. A once-per-sink allowance would degrade a
   flaky-but-working link into a rate-limited one.

4. **The initial `from_url` connection starts unproductive**, so a service deployed straight into a
   broken exchange is bounded after its one free retry rather than churning indefinitely. It does
   not arm on the *first* failure, per decision 0.

5. **`_next_reconnect_at = 0.0` on a successful dial stays.** It is now redundant, because
   `_invalidate` decides the next gate either way — but removing it would make the reset depend
   entirely on a code path two methods away, and it costs nothing to leave the obvious invariant
   ("a dial that succeeded has spent the previous backoff") stated where a reader looks for it.

## Test approach

- **The defect**: a `fake_pika` whose dials succeed and whose publishes always raise, an injected
  clock, 50 records inside one interval, assert dials ≤ 1. This test fails on the pre-fix code — that
  is checked by running it against the parent commit, not assumed.
- **The property most at risk**: blip recovery. Asserted with the clock **not advanced at all**, so
  a fix that armed the interval unconditionally cannot pass.
- **Renewal**: publish → fail → recover → publish → fail → recover, asserting both recoveries are
  immediate.
- **Regression**: every existing AMQP test runs unchanged. If one needs editing, the fix is wrong.
- **Mutation**: revert each half of the change and confirm a test fails, rather than trusting that
  new tests bite.

## Complexity Tracking

None. One boolean, three assignments, one condition.
