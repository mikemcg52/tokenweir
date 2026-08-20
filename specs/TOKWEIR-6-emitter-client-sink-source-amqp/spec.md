# Feature Specification: Emitter client + Sink/Source interfaces + AMQP adapter

**Feature Branch**: `TOKWEIR-6-emitter-client-sink-source-amqp`
**Created**: 2026-08-19
**Status**: Draft
**Jira**: TOKWEIR-6 (Story) — "Emitter client + Sink/Source interfaces + AMQP adapter"
**Input**: The Jira story, fetched with `getJiraIssue` on 2026-08-19 and quoted verbatim below
rather than reconstructed from the branch name. Re-fetched and diffed against the quote during fix
round 3 — identical. Recorded because three independent reviews each flagged it Unverified: the
reviewer subagent has no Jira access, so the quote below is the only story text it can grade
against, and "the implementer wrote the spec on this branch" is a fair thing to want checked.

> Build the buffered emitter client targeting a `Sink` interface, and the writer-side `Source`
> interface. Emission is fire-and-forget / off the critical path (buffers, returns immediately,
> swallows failures). Ship transport adapters as optional extras (`tokenweir[amqp]`) so the core
> stays dependency-light (no `pika` in core); provide the AMQP adapter for the homelab path and
> confirm a broker-less/direct sink works.
>
> **Acceptance:** a service emits via the client with an AMQP sink and with a direct sink; the
> core imports without transport libraries; an emit failure never raises into the caller.

## Context

ADR-0001 Pillar 2 is the whole of this story's motivation: *"Emission is **fire-and-forget / off
the critical path** by contract — buffers, returns immediately, swallows failures — preserving the
gateway's invariant that a logging outage cannot affect availability. Consequence: homelab keeps
RabbitMQ (gateway → AMQP → writer, as today); the EKS cloud-edge can drop the broker entirely and
write direct/in-process."*

Every clause of that sentence is a deliverable here, and none of them exists yet.

### What is already on `main`, and what is therefore not this story's work

The story's title names the `Sink` and `Source` interfaces, but earlier stories landed them:

| Piece | Where | State before this story |
|---|---|---|
| `Sink` protocol, `NullSink` | `tokenweir/sink.py` (TOKWEIR-1 scaffold) | **Done.** Interface + no-op default. |
| Guarded seam `build_record` / `emit_record` / `emit_usage` | `tokenweir/sink.py` (TOKWEIR-15) | **Done.** Keeps *construction* off the critical path. |
| `Source` protocol, `MemorySource` | `tokenweir/source.py` (TOKWEIR-1 scaffold) | **Done.** |
| `PostgresSource` — the real write side | `tokenweir/postgres.py` (TOKWEIR-5) | **Done**, and explicitly deferred batching policy to this story. |
| **Buffered emitter client** | — | **Missing.** Nothing buffers; a caller's `emit` is the sink's `emit`. |
| **AMQP adapter** | — | **Missing.** The `amqp` extra is declared in `pyproject.toml` and nothing implements it. |
| **Direct / broker-less sink** | — | **Missing.** No way to point the emit side at a `Source`. |

So this story is precisely the three missing rows. The interfaces are *confirmed and used* here,
not redefined: an adapter that needed the protocol changed would be evidence the protocol was
wrong, and it is not.

`tokenweir/postgres.py` states the hand-off in its own module docstring — *"Batching policy — how
many records, how long to wait, what to do with a redelivery — belongs to the consumer, which owns
the broker; TOKWEIR-6 has it."* That sentence is why batching policy is in scope below and why
`Source.write`'s one-transaction-per-batch guarantee is the thing the direct path must actually
use, rather than degrading to a transaction per record.

### The gap the buffered client closes

`Sink.emit`'s contract already says "buffer/return immediately, never raise", but every `Sink` an
adopter writes would have to implement buffering *itself* to honour it — an AMQP publish is a
blocking network call, so a `Sink` that publishes inline satisfies "never raises" and violates
"off the critical path". That is the guarantee implemented once per adapter and wrong once.

The buffered emitter client makes buffering a property of **the client**, so a transport adapter
is free to be a plain, blocking, easy-to-write publisher and still be safe to put on a metered
request path. This is the same argument TOKWEIR-15 made for record construction: the library owns
the guarantee, and consumers adopt it by calling rather than by re-deriving it.

### Bounded, not unbounded — and what that costs

A buffer that grows without limit is not a safety mechanism; it is a memory leak that takes down
the metered service when the broker is down, which is the exact failure ADR-0001 Pillar 2 exists to
prevent. The buffer is therefore **bounded**, and a full buffer **drops** rather than blocking the
caller or growing. Dropping metering data is the correct trade: metering is the thing that may be
lost, and the metered system is the thing that may not.

Likewise, delivery is **not retried**. "Swallows failures" is the contract; a retry loop in front
of a broken broker is how a bounded buffer becomes a full one, and head-of-line blocking on a
poison record would stall every record behind it. A failed batch is dropped and counted.

Neither drop is silent: both are counted on the client and logged, and the counts are readable so
an operator can alert on "metering is being dropped" without parsing logs.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A metered service emits without ever blocking or failing on metering (Priority: P1)

A service on a request path calls the emitter client once per metered call. The call appends to a
buffer and returns; a background worker delivers to the configured `Sink`. Whatever the sink, the
broker, or the network does, the metered request is unaffected.

**Why this priority**: This is ADR-0001 Pillar 2 and the story's third acceptance clause. Without
it the other two are decoration.

**Independent Test**: Emit against a sink that raises on every call, that blocks, and that is
absent; assert the caller returns and never sees an exception, and that the metered work completes.

**Acceptance Scenarios**:

1. **Given** a client over a working sink, **When** a service emits a record, **Then** the call
   returns without waiting for delivery and the record is delivered by the worker.
2. **Given** a sink whose `emit` raises every time, **When** a service emits, **Then** nothing is
   raised into the caller, the failure is counted and logged, and the client keeps accepting.
3. **Given** a sink whose `emit` blocks for longer than the metered call, **When** a service emits,
   **Then** the emitting call still returns immediately.
4. **Given** a buffer that is full, **When** a service emits, **Then** the call returns immediately
   having dropped the record, and the drop is counted and logged rather than blocking.

---

### User Story 2 - The homelab path publishes to RabbitMQ (Priority: P1)

The homelab keeps its broker: gateway → AMQP → writer. A service configures the client with the
AMQP sink and records reach RabbitMQ as JSON messages on the configured exchange/routing key.

**Why this priority**: The story's first acceptance clause names it, and it is the deployed
topology today.

**Independent Test**: Drive the adapter against a recording channel double asserting the published
exchange, routing key, body and delivery mode; drive the pure record→message mapping with no `pika`
installed at all.

**Acceptance Scenarios**:

1. **Given** an AMQP sink, **When** a record is emitted, **Then** one persistent JSON message is
   published carrying exactly the record's wire form.
2. **Given** the broker has dropped the connection, **When** the next batch is emitted, **Then**
   the adapter re-establishes its connection rather than failing forever, and a publish that still
   fails does not raise.
3. **Given** `pika` is not installed, **When** the AMQP sink is asked to open its own connection,
   **Then** the error names `tokenweir[amqp]` rather than being a bare `ModuleNotFoundError`.

---

### User Story 3 - The cloud-edge drops the broker and writes direct (Priority: P1)

The EKS cloud-edge has no RabbitMQ. It configures the client with a direct sink over a `Source`
(`PostgresSource` in deployment, `MemorySource` in tests) and gets the same emit-side guarantees
with no broker in the picture.

**Why this priority**: The story's first acceptance clause names it alongside AMQP ("and with a
direct sink"), and ADR-0001 names it as the consequence that makes the core transport-agnostic.

**Independent Test**: Emit through a client over a direct sink onto a `MemorySource`, assert the
records land; make the source raise and assert nothing reaches the caller.

**Acceptance Scenarios**:

1. **Given** a client over a direct sink onto a `Source`, **When** records are emitted, **Then**
   they are written to the source, batched rather than one transaction per record.
2. **Given** the underlying store raises, **When** a record is emitted, **Then** nothing is raised
   into the caller and the failure is counted.

---

### User Story 4 - A bare install carries no transport library (Priority: P1)

`pip install tokenweir` installs no `pika` and no driver, and `import tokenweir` imports neither.
The AMQP adapter is reached by its own import path under the `amqp` extra.

**Why this priority**: The story's second acceptance clause, and ADR-0001 Pillar 2's
dependency-light core.

**Independent Test**: In a subprocess, import the package and every core module and assert no
transport library is in `sys.modules` — true whether or not `pika` happens to be installed in the
running environment.

**Acceptance Scenarios**:

1. **Given** a bare install, **When** `tokenweir` and its modules are imported, **Then** `pika` is
   not in `sys.modules`.
2. **Given** the adapter module is imported, **When** no connection is opened, **Then** `pika` is
   still not imported — the mapping is usable and testable without it.

---

### Edge Cases

- **`close()` called twice** — must be a no-op the second time, per the `Sink` protocol's "safe to
  call more than once". *(This case originally also read "or called on a client that was never
  started". The worker starts unconditionally in `__init__`, so no unstarted client exists to close;
  the criterion is **inapplicable as written** rather than covered, and is recorded that way rather
  than quietly ticked. Deferring the thread start was considered and rejected: it buys a state whose
  only visible effect is that `flush()` blocks until its timeout, which is a footgun, not a
  feature.)*
- **`close()` with records still buffered** — a final flush delivers what is buffered within a
  bounded timeout; it must not hang forever on a dead broker.
- **Emitting after `close()`** — must not raise; the record is dropped and counted, because a
  shutdown race on a request path is exactly when a raise would be most harmful.
- **Interpreter exit without `close()`** — the worker must not keep the process alive.
- **A non-`UsageRecord` handed to the client** — refused before it can reach a sink, as
  `emit_record` already refuses it, so a `None` from a construction drop cannot become a row of
  nulls or a message of `"null"`.
- **Many threads emitting at once** — no record lost, none duplicated, no corrupt buffer state.
- **A sink that raises from `close()`** — must not prevent the client from shutting down.
- **An empty flush tick** — an idle service must not produce broker traffic or store transactions.

## Requirements *(mandatory)*

### Functional Requirements

**The buffered emitter client**

- **FR-001**: The library MUST provide a buffered emitter client that accepts a `Sink` and delivers
  records to it from a background worker, so the calling thread never performs transport I/O.
- **FR-002**: The client's emit call MUST return without waiting for delivery, and MUST NOT raise
  for any sink failure, buffer state, or lifecycle state. A `BaseException` raised **on the calling
  thread** — by the logging handler an application installed, say — MUST still propagate, as it does
  through the guarded seam.

  A sink's `BaseException` is a **different case**, and saying it "matches the guarded seam" would
  be wrong. Through `emit_record` the sink runs on the caller's thread, so its `KeyboardInterrupt`
  reaches the caller. Through this client the sink runs on the worker, and an exception there is not
  the emitting thread's to receive — it belongs to no request in particular. It is not caught
  either: the worker dies rather than pretending a `BaseException` is a drop, and
  `EmitterStats.worker_alive` is what makes that state legible instead of leaving it to be inferred
  from `buffered` climbing while `delivered` does not.
- **FR-003**: The buffer MUST be bounded, with the bound configurable, and MUST drop rather than
  block or grow when full.
- **FR-004**: The client MUST deliver in batches, with batch size and a maximum wait configurable,
  so an idle service produces no traffic and a busy one does not pay per-record overhead.
- **FR-005**: A delivery failure MUST NOT be retried; the batch is dropped and counted. A failure
  MUST NOT stop the worker — the client keeps accepting and delivering subsequent records.
- **FR-006**: The client MUST refuse anything that is not a `UsageRecord`, counted as a distinct
  drop reason, before it can reach a sink.
- **FR-007**: The client MUST expose counters distinguishing at least: accepted, delivered, dropped
  because the buffer was full, dropped because the value was not a record, and failed in delivery.
  Counters MUST be readable while the client is running.
- **FR-008**: Every drop and delivery failure MUST be logged at `WARNING` on the library's logger,
  and the logging MUST be rate-limited so a systematically broken broker cannot produce one log
  line per metered request.
- **FR-009**: The client MUST provide an explicit flush that blocks until the buffer is drained or
  a caller-supplied timeout expires, reporting which happened.
- **FR-010**: `close()` MUST stop the worker, flush within a bounded timeout, close the sink, and be
  safe to call more than once. It MUST NOT raise if the sink's own `close()` raises.
- **FR-011**: The client MUST be usable as a context manager, closing on exit.
- **FR-012**: The worker MUST NOT keep the interpreter alive at exit.
- **FR-013**: The client MUST be safe to call from many threads at once.
- **FR-014**: The client MUST itself satisfy the `Sink` protocol, so it composes with the existing
  guarded seam (`emit_usage(client, fields)`) and can front another client.

**The direct (broker-less) sink**

- **FR-015**: The library MUST provide a sink that adapts a `Source` to the `Sink` interface, so a
  deployment with no broker emits through the same client.
- **FR-016**: The direct sink MUST NOT raise from `emit`, per the `Sink` contract, converting a
  store failure into a counted, logged drop.
- **FR-017**: The direct sink MUST write a batch as one `Source.write` call, preserving the
  one-transaction-per-batch guarantee `PostgresSource` provides, rather than one call per record.

**Batch delivery**

- **FR-018**: The library MUST define an optional batch-capable extension of the `Sink` interface
  and the client MUST use it when the sink provides it, falling back to per-record `emit`
  otherwise. Adding it MUST NOT change the `Sink` protocol, so existing sinks keep working.

**The AMQP adapter**

- **FR-019**: The library MUST provide an AMQP sink under the `amqp` extra that publishes each
  record as its JSON wire form.
- **FR-020**: The adapter MUST NOT import `pika` at module import time. The import MUST happen when
  an `AMQPSink` is **constructed** — no later.

  > **Amended during implementation (fix round 1), and the original is worth keeping visible.**
  > This clause first read *"the import MUST be deferred to the point a connection is opened,
  > matching how `tokenweir.migrations` defers `psycopg`"*. Implemented literally, it was a High
  > finding in review 1: the properties every message carries are a `pika` object, so "as late as
  > possible" put the import on the **publish** path — inside the guard that FR-002 forbids from
  > raising. A missing driver was therefore not an error at all. It was a caught exception, a
  > counted drop, and a permanent 100% loss of metering for the life of the process, in the one code
  > path whose whole job is never to complain.
  >
  > The analogy to `tokenweir.migrations` is what misled the clause. There, the deferred import sits
  > in `connect()` — a function that **may raise**, because opening a connection is wiring-time work.
  > The property worth copying was never "defer as far as possible"; it was "defer to the last point
  > that can still report failure". For this adapter that point is construction.
  >
  > What the original clause was protecting is unchanged and still asserted: a bare
  > `pip install tokenweir` carries no AMQP library, importing `tokenweir.amqp` imports no `pika`,
  > and `message_for` stays pure and testable with none installed (FR-025, FR-026). The cost is
  > that a caller with a channel of their own must pass `properties=` — the one thing the driver is
  > needed for — or install `pika`. That is stated in FR-021's error text and in the README.
- **FR-021**: When `pika` is missing, the raised `ImportError` MUST name `tokenweir[amqp]`.
- **FR-022**: The adapter MUST publish persistent messages with a JSON content type, so a broker
  restart does not discard buffered metering.
- **FR-023**: The adapter MUST accept a caller-supplied channel as well as opening its own from a
  URL, and MUST close only a connection it opened itself — mirroring `PostgresSource`'s ownership
  rule.
- **FR-024**: The adapter MUST re-establish a connection **it is able to re-establish** — one it
  opened itself from a URL — after a connection failure, on a later publish attempt rather than by
  retrying inside one. Attempts MUST be rate-limited (`reconnect_interval`), with the first attempt
  after a lost connection immediate.

  > **Amended during implementation (fix round 3), for the same reason FR-020 was.** This clause
  > first read *"MUST re-establish a connection it owns"*, and "owns" is the wrong test. Ownership
  > decides *closing*; it does not confer the ability to re-open. Only `from_url` supplies the
  > callable that can build a new connection, so the documented public shape
  > `AMQPSink(channel, connection=conn, owns_connection=True)` **owned** a connection it could never
  > re-establish. Implemented literally, the clause made that sink close its connection on the first
  > publish failure and then have nothing to replace it with — permanently dead, and strictly worse
  > than the borrowed case it was modelled on, where the caller can at least repair the channel out
  > of band. The gate is therefore capability, not ownership.
  >
  > The rate limit is not in the original clause at all, and is an addition rather than a
  > correction (review round 3, Med). "On a later attempt rather than inside one" bounds the wait
  > per *publish*, but says nothing about the wait per *record* — and `pika.BlockingConnection`
  > blocks for a full connect timeout, on the emitter's delivery worker. Without spacing, a down
  > broker cost one connect timeout per buffered record, which is the retry loop this design
  > refuses, reassembled out of single attempts.
  >
  > **Still incomplete, and closed by TOKWEIR-30.** The rate limit as shipped here was armed only
  > when a *dial* failed, and cleared whenever one succeeded — so on the path where dials succeed
  > and **publishes** fail (a missing exchange, an access-refused channel) it was armed never and
  > cleared constantly, and the sink opened a connection per record. This clause was also silent on
  > the question that decides it: whether a failed publish counts as losing the connection.
  > TOKWEIR-30 answers it — a connection that had been publishing earns an immediate re-dial, one
  > that never published earns exactly one, and after that the interval applies — and carries the
  > reasoning.
  >
  > **And it bounds the word "immediate" in this clause.** TOKWEIR-30 adds
  > `tokenweir.amqp.MAX_DIALS_PER_INTERVAL`, a hard cap on dials per interval that overrides every
  > immediacy rule above it, including this one: a link that drops and recovers more than
  > `MAX_DIALS_PER_INTERVAL` times inside a single interval has the excess recoveries refused, and
  > its records dropped, until the window turns over. That is deliberate — the cap exists because
  > the immediacy rules infer a broker's intent from a driver whose publish is asynchronous, and an
  > inference is the wrong thing to hang a bound on — but "the first attempt after a lost connection
  > immediate" as written here is now true only up to that cap, and this clause would be promising
  > something the code does not deliver if it did not say so.
- **FR-025**: The record→message mapping MUST be a pure function, testable with no `pika` installed
  and no broker running.

**Dependency-light core**

- **FR-026**: Importing `tokenweir` or any core module MUST NOT import `pika`, asserted in a
  subprocess so the result does not depend on whether `pika` is installed locally.
- **FR-027**: The AMQP adapter MUST NOT be exported from the `tokenweir` package namespace; it is
  reached by its own import path, as the store modules are.

**Documentation**

- **FR-028**: `README.md` MUST document the emitter client, the direct sink and the AMQP adapter,
  including the bounded-buffer drop policy and the no-retry policy, since both are behaviours an
  adopter would otherwise discover only in production.

### Key Entities

- **Buffered emitter client**: owns the buffer, the worker, the batching policy and the counters.
  Is itself a `Sink`. Holds a `Sink`; does not know what transport it is.
- **Batch-capable sink**: the optional `emit_batch` extension a sink may provide when delivering a
  batch is cheaper or more atomic than delivering records one at a time.
- **Direct sink**: adapts a `Source` to the `Sink` interface. The broker-less path.
- **AMQP sink**: publishes a record's JSON to an exchange/routing key. Owns its connection only if
  it opened it.
- **Emitter counters**: accepted / delivered / dropped-full / dropped-not-a-record / failed.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A service emits through the client with an AMQP sink and, separately, with a direct
  sink, and in both cases the records arrive at the far side — the story's first acceptance clause,
  proven by a test per path.
- **SC-002**: `import tokenweir` plus every core module leaves `pika` out of `sys.modules`, checked
  in a subprocess — the story's second acceptance clause.
- **SC-003**: No sink failure mode reaches the caller: a sink that raises, one that blocks, one
  that is `None`, and a full buffer all leave the emitting call returning normally — the story's
  third acceptance clause.
- **SC-004**: With the buffer bound set to *n*, emitting more than *n* records against a stalled
  sink leaves memory bounded and the excess counted as dropped, with no unbounded growth.
- **SC-005**: 10 threads emitting concurrently deliver exactly the number of records emitted, with
  none lost or duplicated.
- **SC-006**: A direct sink delivering a batch of *n* records makes exactly one `Source.write` call,
  not *n*.
- **SC-007**: The AMQP adapter's record→message mapping is exercised by tests that pass with `pika`
  absent.
- **SC-008**: `close()` twice, `close()` on a never-started client, and `emit()` after `close()` all
  complete without raising.
- **SC-009**: A process that creates a client and exits without closing it terminates without
  hanging.
- **SC-010**: A systematically failing sink produces a bounded number of log lines, not one per
  record.
- **SC-011**: The whole suite passes under the project's authoritative test command.

## Assumptions

- The `Sink` and `Source` protocols as landed by TOKWEIR-1/TOKWEIR-15 are correct and are used
  here unchanged. `emit_batch` is added as an *optional* capability a sink may advertise, not as a
  change to `Sink`.
- Threads, not asyncio: the metered callers named in ADR-0001 (the gateway, the MADO cloud-edge, a
  Claude Code `Stop` hook) are synchronous, and a thread-based worker is usable from both sync and
  async callers. An async-native client is out of scope and not required by the story.
- RabbitMQ via `pika` is the AMQP client, as declared by the `amqp` extra already in
  `pyproject.toml`.
- No broker and no Postgres is available to this run's automated suite, so AMQP is proven against a
  recording channel double and the direct path against `MemorySource` — with the real-store suite
  continuing to skip as `tests/conftest.py` already arranges. Testing a *broker client* against a
  double is the right shape regardless: what is being asserted is that the adapter publishes the
  right bytes to the right place, not that RabbitMQ works.
- Exchange, routing key and durability defaults are the adapter's, overridable by the caller;
  declaring the exchange/queue is a deployment concern the adapter does not take over.
