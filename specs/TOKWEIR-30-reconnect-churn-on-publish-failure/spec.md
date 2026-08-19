# Feature Specification: Bound reconnect churn when publishes fail on a healthy connection

**Feature Branch**: `TOKWEIR-30-reconnect-churn-on-publish-failure`, cut from
`TOKWEIR-6-emitter-client-sink-source-amqp` at `36ae6de` **at the developer's explicit
instruction** — *"branch off of the current TOKWEIR-6 branch since the work is related"*. The code
this bug is in exists only on that branch and has not merged, so branching from `main` would have
meant fixing a file that is not there. The same thing was done for TOKWEIR-15 off TOKWEIR-4.

**Created**: 2026-08-19
**Status**: Draft
**Jira**: TOKWEIR-30 (Bug), `Relates` to TOKWEIR-6
**Input**: The Jira bug, fetched with `getJiraIssue` on 2026-08-19. It was filed by the TOKWEIR-6
run itself, as a deferred Med from that story's terminal review.

> **Defect.** `AMQPSink._invalidate()` runs on *any* publish exception, and `_live_channel()` gates
> re-dialling on `_next_reconnect_at`, which is set **only when a dial fails** and reset to `0.0` on
> every dial success. So when the connection succeeds and the *publish* fails — a nonexistent
> exchange, an access-refused channel, any channel-level `NOT_FOUND` — the sink tears down and
> re-dials on **every single record**, indefinitely. `reconnect_interval` never engages, because
> from its point of view every attempt is "the first attempt after a lost connection".
>
> **Suggested resolution.** Set `_next_reconnect_at` in `_invalidate()`, or clear it only after a
> dial is followed by a *successful publish*.
>
> **Spec ambiguity worth settling at the same time.** `spec.md` FR-024 says attempts must be
> rate-limited "with the first attempt after a lost connection immediate". It does not say whether a
> publish failure counts as losing the connection. Whichever way that is resolved, it should be
> written down.

## Context

### Reproduced before anything was changed

The ticket's reproduction was re-run on this branch rather than taken on trust — a fake `pika`
whose dials always succeed and whose `basic_publish` always raises, `reconnect_interval=30.0`, an
injected clock advancing 0.01s per record:

```
records: 50   connections dialled: 50   dropped: 50
```

Fifty records, fifty TCP connects and AMQP handshakes, all inside a thirty-second window that was
supposed to permit one. Against a real broker each of those is a blocking dial on the
`BufferedEmitter`'s single delivery worker, so the buffer behind it fills and drops while the worker
is in `connect()`.

### Why the existing interval does not catch this

`reconnect_interval` was added by TOKWEIR-6 fix round 3 for the **broker-down** case, and it is
armed in exactly one place: the `except` arm of the dial. Its reset is equally narrow — a
*successful dial* clears it. Neither half ever observes a publish. So on the path where dials
succeed and publishes fail, the interval is armed never and cleared constantly, and the code behaves
as though `reconnect_interval` were zero.

The shape of the miss is worth naming, because it is the interesting part: the fix asked "did the
attempt to reach the broker work?" when the question that bounds churn is "did the reconnect
*accomplish* anything?". A dial that succeeds and is immediately thrown away has accomplished
nothing, and repeating it is a retry loop no matter how healthy each individual dial looks.

### The likelier trigger

A misconfigured exchange or a permissions error is a far more common production state than a broker
that is down — it is the normal consequence of a deploy against an environment whose topology was
never declared, which the adapter deliberately does not do for the caller. So the path that churns
is the path more likely to be taken.

### Settling the ambiguity the ticket names

TOKWEIR-6's FR-024 says attempts are rate-limited "with the first attempt after a lost connection
immediate", and does not say whether a failed publish counts as losing the connection. This story
answers: **it depends on whether that connection ever worked.**

- A connection that **published successfully and then failed** has genuinely been lost. Re-dialling
  is the thing that fixes it, and making the caller wait out an interval to recover from a blip
  would trade a real fault for an invented one. Immediate.
- A connection that **never published anything** has not been lost — it was never doing the job.
  Nothing about a fresh connection differs from the one being discarded, so an immediate re-dial
  cannot help and is pure churn. Rate-limited.

That distinction is available without knowing anything about AMQP. The alternative design —
classifying pika's exceptions into channel-level and connection-level errors — is more precise on
paper and worse here: it couples the adapter to a driver exception hierarchy that
`tokenweir.amqp` otherwise imports nothing from (the module's whole discipline is that `pika`
appears in one place), and it would have to track broker-specific reply codes to stay accurate.
"Did this connection ever publish a record?" needs no driver knowledge and cannot go stale.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A misconfigured exchange costs one dial per interval, not one per record (Priority: P1)

A service is deployed with a routing key or exchange that does not exist on the broker. Every
publish fails. The sink drops the records — that part is correct and unchanged — but it must not
open a new connection for each one.

**Why this priority**: It is the defect. Everything else here is protecting behaviour that already
works.

**Independent Test**: Drive many records through a sink whose dials succeed and whose publishes
always fail, with an injected clock inside one interval, and count connections opened.

**Acceptance Scenarios**:

1. **Given** a sink whose publishes always fail, **When** 50 records are emitted inside one
   `reconnect_interval`, **Then** at most one reconnect is attempted.
2. **Given** the same sink, **When** the interval elapses, **Then** exactly one further reconnect is
   attempted, and the pattern repeats at that rate rather than per record.
3. **Given** the same sink, **When** records are dropped, **Then** every drop is still counted and
   logged, rate-limited, exactly as before.

---

### User Story 2 - A genuine connection blip still recovers instantly (Priority: P1)

A connection that has been publishing successfully drops. The next record must re-establish it
immediately, with no interval to wait out.

**Why this priority**: It is the property most easily destroyed by a careless fix. The obvious
one-line change — arm the interval on every invalidation — fixes User Story 1 by breaking this.

**Independent Test**: Publish successfully, fail one publish, and assert the next record reconnects
with the clock not advanced at all.

**Acceptance Scenarios**:

1. **Given** a connection that has published at least one record, **When** a publish then fails,
   **Then** the next record re-dials immediately regardless of the interval.
2. **Given** that immediate re-dial succeeds, **When** the next record is published, **Then** it is
   published on the new connection.
3. **Given** a connection that recovered and published again, **When** it later fails again,
   **Then** that recovery is also immediate — the allowance is per productive connection, not
   once per sink.

---

### User Story 3 - A down broker is unaffected (Priority: P2)

The existing broker-down behaviour — dial fails, interval armed, one dial per interval — must be
untouched, including its logging and its counters.

**Why this priority**: Regression protection for TOKWEIR-6 fix round 3's own fix. It already has
tests; this story must not quietly change what they assert.

**Independent Test**: The TOKWEIR-6 reconnect tests pass unchanged.

**Acceptance Scenarios**:

1. **Given** a broker refusing connections, **When** 50 records are emitted inside one interval,
   **Then** one dial is attempted, as today.
2. **Given** a borrowed channel (no reconnect capability), **When** publishes fail, **Then** nothing
   is dialled or closed, as today.

---

### Edge Cases

- **The very first connection, from `from_url`, never publishes** (deployed straight into a broken
  exchange). It gets one free re-dial — see FR-002a for why the obvious alternative is wrong — and
  the interval arms when that replacement also fails to publish. Churn is bounded from the second
  failure, at a cost of one extra dial.
- **A connection publishes, fails, re-dials immediately, and the new connection's first publish also
  fails.** The productive connection reset the count, so the new one is the first unproductive
  connection and gets its own free retry before the interval arms. The allowance is genuinely per
  productive connection.
- **A dial that fails** is unchanged and is a different path: it arms the interval on the first
  failure, because a dial that could not be made is unambiguous evidence, needing no free retry.
- **`reconnect_interval=0`** must keep meaning "attempt on every publish", for the tests that rely
  on it and for a caller who wants it.
- **A borrowed channel** (`_reconnect is None`) must be untouched: no dialling, no closing, no
  interval.
- **A publish that fails for a non-record reason** (refused before the channel is reached) must not
  invalidate anything, since no connection was used.

## Requirements *(mandatory)*

- **FR-001**: When **two or more consecutive connections** are discarded without either having
  published a record, the reconnect interval MUST be armed, so subsequent records do not each
  trigger a dial.
- **FR-002**: A publish failure on a connection that **has** successfully published at least one
  record MUST NOT arm the interval — the next record re-dials immediately.
- **FR-002a**: The **first** unproductive connection in a run MUST also get an immediate re-dial.
  Only if that replacement is *also* unproductive does the interval arm.

  > **Amended during implementation, before the first review.** FR-001 first read: *"A publish
  > failure on a connection that has **never successfully published** MUST arm the reconnect
  > interval"*, with no FR-002a. Implemented literally, it broke a TOKWEIR-6 test —
  > `test_the_first_attempt_after_a_failure_is_immediate` — and the test was right, which is why it
  > was not edited.
  >
  > The rule conflated two states that "never published" cannot tell apart: a connection **reset
  > before its first publish**, which is an ordinary blip that re-dialling fixes, and a connection
  > facing a **misconfigured exchange**, which re-dialling cannot fix. From a sink's very first
  > record the two are indistinguishable. One free re-dial resolves the ambiguity cheaply — if a
  > fresh connection also cannot publish, the fault is not in the connection — at a cost of exactly
  > one extra dial, versus turning every cold-start blip into an interval-long metering outage.
  >
  > The bound this story exists to deliver is unchanged: one dial per interval instead of one per
  > record, plus a single one-off retry at the start.
- **FR-003**: The state MUST be per connection — "has this connection published" reset on each new
  connection, and the consecutive-unproductive count reset by any successful publish — so the
  immediate-retry allowance is renewed for each productive connection rather than granted once per
  sink.
- **FR-004**: Behaviour when the **dial itself** fails MUST be unchanged: the interval is armed, one
  attempt per interval, with the existing redacted warning.
- **FR-005**: Behaviour for a sink with no reconnect capability (a borrowed channel) MUST be
  unchanged: no dial, no close, no interval.
- **FR-006**: Every dropped record MUST still be counted and logged with an accurate reason,
  rate-limited per reason, exactly as before this change.
- **FR-007**: `reconnect_interval=0` MUST continue to mean "no spacing".
- **FR-008**: The resolution of the ambiguity MUST be written into TOKWEIR-6's FR-024, which is the
  clause that was silent on it, so the two specs do not disagree — the same in-place amendment
  treatment FR-020 and FR-024 already carry there.
- **FR-009**: No public API change. `reconnect_interval`, `clock`, the counters and the log messages
  keep their meanings; a caller upgrading sees only less churn.

### Key Entities

- **Productive connection**: one that has successfully published at least one record. The single
  piece of state this fix adds, and the thing that distinguishes a lost connection from a connection
  that never worked.

## Success Criteria *(mandatory)*

- **SC-001**: 50 records against always-failing publishes inside one interval produce **at most 1**
  reconnect (so at most 2 connections including the one `from_url` opened), down from 50. Asserted
  by a test that fails on the pre-fix code.
- **SC-002**: A blip on a productive connection recovers on the very next record with the clock not
  advanced.
- **SC-003**: The immediate-retry allowance renews per productive connection.
- **SC-004**: Every TOKWEIR-6 AMQP test passes unchanged — no assertion is edited to accommodate
  this fix.
- **SC-005**: Drop counts and warning behaviour are identical before and after for every failure
  mode.
- **SC-006**: The whole suite passes under the project's authoritative command.

## Assumptions

- The fix belongs in `AMQPSink` alone. `BufferedEmitter` and `DirectSink` are untouched; the churn
  is entirely inside the adapter's reconnect policy.
- No new public constructor argument. The distinction is derivable from state the sink already has
  to track, and another knob would be a way of asking the caller to solve this.
- The `dev` extra's `pika` remains test-only and the authoritative command still runs without it, so
  everything here is exercised against the `fake_pika` fixture and an injected clock, as TOKWEIR-6
  established.
