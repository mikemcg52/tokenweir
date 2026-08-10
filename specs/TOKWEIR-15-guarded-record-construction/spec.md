# Feature Specification: Guarded record construction on the metered request path

**Feature Branch**: `TOKWEIR-4-usage-record-contract` (continued at the developer's explicit
instruction — "implement TOKWEIR-15 off of the existing branch")
**Created**: 2026-08-10
**Status**: Draft
**Jira**: TOKWEIR-15 (Story) — "Emitter/gateway: UsageRecord construction can raise on the
metered request path", `Relates` to TOKWEIR-4
**Input**: Deferred from the TOKWEIR-4 terminal review. TOKWEIR-4 made `UsageRecord` validate at
construction — a blank identity field, a negative or non-integer token count, a non-string
optional, or an unrecognized `pricing_mode` now raises `ValueError`, and an omitted required
argument raises `TypeError`. The extraction scaffold accepted anything. ADR-0001's
off-critical-path guarantee is scoped to `Sink.emit`, which is unchanged and still must never
raise — but **record construction is not `Sink.emit`**. The AI Gateway and the MADO cloud-edge
construct records inline on a request path, so a bad value that previously produced a junk record
now raises into the caller and fails the request being metered.

**Acceptance (from the story):** the emitter and gateway integrations construct records inside a
guard, with a test proving a malformed record cannot fail the request being metered.

## Context

The validation TOKWEIR-4 added is correct and is not in question here. An unattributable record is
a producer-side bug, and silently metering garbage is worse than failing. That decision stands.

What the story identifies is a **gap in where the blast radius stops**. ADR-0001 Pillar 2 promises
that "a metering outage can never affect the availability of the thing being metered", and the
library discharges that promise at exactly one place: the `Sink.emit` contract. Construction sits
*upstream* of that promise and is now the one metering operation on a request path that can throw.
So the guarantee has a hole in it that is invisible from inside `sink.py`.

### What this story can and cannot deliver in this repository

The story's acceptance names two integrations. Neither is code this repository can edit today:

| Integration | Where it lives | State |
|---|---|---|
| Emitter client | this repo, TOKWEIR-6 | In Progress; nothing of it is on this branch — `sink.py` holds the `Sink` protocol and `NullSink` only |
| AI Gateway | the `ai-gateway` repository, TOKWEIR-10 | To Do; out of this repository's reach entirely |

Waiting for both is not a deliverable, and telling each consumer to hand-roll its own `try/except`
is how the guarantee ends up implemented three times and wrong once. What `tokenweir` owns — and
what closes the gap for **both** consumers at once — is the **seam**: a supported, guarded way to
get a record from raw values into a `Sink` that cannot raise into the metered request. TOKWEIR-6
and TOKWEIR-10 then adopt it by calling it, rather than by each re-deriving it.

This is therefore scoped as: *the library provides the guard and proves it holds*. The
consumer-side adoption in `ai-gateway` remains TOKWEIR-10's, and is flagged rather than claimed.

#### Acceptance-clause traceability

The story text above was fetched from Jira with `getJiraIssue` on 2026-08-10 and is quoted verbatim,
not reconstructed from the branch name — this table's left column is the story's own wording.

The story's acceptance has two clauses. Stated separately so that closing TOKWEIR-15 is a decision
someone makes on the record, not one that happens by the story scrolling off a board:

| Clause (verbatim from the Jira story) | Status on this branch |
|---|---|
| "the emitter and gateway integrations construct records inside a guard" | **Not met here, and cannot be.** The gateway is the `ai-gateway` repository (TOKWEIR-10); the emitter client is TOKWEIR-6 and is not on this branch. What is delivered is the guard those integrations call, so each adopts it by calling rather than by re-deriving it. |
| "with a test proving a malformed record cannot fail the request being metered" | **Met.** `tests/test_guarded_emit.py` proves it for every invalid-input class the contract rejects, for a missing required argument, for a field named `sink`, for a sink that raises, and for a logging configuration that raises. |

Closing the story on the seam alone is therefore a judgement the developer owns, not one this run
can make. It is reported as such rather than assumed.

### Why the guard covers emission too

`Sink.emit` MUST NOT raise — that is the protocol's contract. But a contract is a statement about
conforming implementations, and the caller on the request path is the party that gets hurt when an
adapter (a third-party sink, an in-house one, a half-finished AMQP adapter) violates it. The story
is explicit: "the try/except belongs around construction as well as emission, not only around
emission." Guarding emission here does not weaken or replace the `Sink` contract; it stops a
non-conforming implementation from reaching the metered request.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A malformed record cannot fail the request being metered (Priority: P1)

A gateway request completes normally even though the values it tried to meter were invalid.

**Why this priority**: It is the story. Everything else exists to make this reachable and visible.

**Independent Test**: Call the guarded seam with values that make `UsageRecord(...)` raise, and
observe that nothing propagates and the caller continues.

**Acceptance Scenarios**:

1. **Given** a request path metering a call, **When** an identity field is blank (`app_id=""`),
   **Then** no exception reaches the caller and the call reports that nothing was metered.
2. **Given** a request path metering a call, **When** a token count is negative, non-integer, or a
   `bool`, **Then** no exception reaches the caller.
3. **Given** a request path metering a call, **When** `pricing_mode` is an unrecognized string,
   **Then** no exception reaches the caller.
4. **Given** a request path metering a call, **When** a required field is **omitted entirely** —
   the `TypeError` from Python's own signature check, not a `ValueError` — **Then** no exception
   reaches the caller. The guard covers both error types TOKWEIR-4 deliberately kept distinct.
5. **Given** a request path metering a call, **When** an optional field is the wrong type
   (`workload=42`), **Then** no exception reaches the caller.
6. **Given** valid values, **When** the record is metered, **Then** it reaches the sink unchanged
   and equals the record direct construction would have produced — the guard adds no normalization,
   defaulting, or coercion of its own.

---

### User Story 2 - A misbehaving sink cannot fail the request either (Priority: P1)

A request completes even though the sink handed to it violates the `Sink` contract and raises.

**Why this priority**: Equal to US1 — it is the second half of the story's "construction **as well
as** emission" wording, and it is the failure mode ADR-0001 Pillar 2 was written to prevent.

**Independent Test**: Emit through a sink whose `emit` raises, and observe that nothing propagates.

**Acceptance Scenarios**:

1. **Given** a sink whose `emit` raises, **When** a valid record is emitted through the guard,
   **Then** no exception reaches the caller and the call reports that emission failed.
2. **Given** a sink whose `emit` raises, **When** the record itself was valid, **Then** the failure
   is reported as an emission failure, not as a construction failure — the two are distinguishable
   to whoever reads the logs.

---

### User Story 3 - A dropped record is visible, never silent (Priority: P1)

An operator investigating a metering hole finds a log line naming the field that caused the drop.

**Why this priority**: P1, not P2. A guard that swallows failures silently converts a loud
producer-side bug into an undetectable metering gap — which is the failure mode TOKWEIR-4's
validation was added to prevent. Trading a raised exception for a silent drop would undo that
decision rather than protect it. The guard is only defensible if the drop is observable.

**Independent Test**: Trigger a drop with logging captured, and assert a warning was emitted that
identifies the cause.

**Acceptance Scenarios**:

1. **Given** a malformed record, **When** it is dropped, **Then** a `WARNING` is logged on a
   `tokenweir` logger carrying the underlying exception.
2. **Given** a dropped record, **When** the log line is read, **Then** it says metering was skipped
   for that call, so the reader is not left guessing whether the request itself failed.
3. **Given** an application that has configured no logging at all, **When** a record is dropped,
   **Then** the library writes nothing to that application's stdout or stderr. It uses a module
   logger and leaves handler policy to the application — which, for a library, means attaching a
   `NullHandler` to the package logger, because otherwise the standard library's `lastResort`
   handler prints the warning and its traceback to stderr and the library has taken an output
   decision that was not its to take. On a hot metered path with a systematically broken producer
   that is one traceback per request.
4. **Given** that same unconfigured application, **When** a record is dropped, **Then** the drop is
   still observable — through the return value, which reaches the caller regardless of logging
   configuration. "Never silent" is discharged by the return value plus a logged warning the
   application can turn on with one line of `basicConfig`, not by writing to a stream the
   application did not ask for.
5. **Given** a caller that wants to react programmatically, **When** a record is dropped, **Then**
   the return value distinguishes success from a drop, so the caller can increment its own counter
   without parsing logs.

---

### User Story 4 - A consumer can guard construction and emission separately (Priority: P2)

A caller that must stamp or enrich a record between building it and emitting it still gets the
guarantee, without hand-rolling `try/except`.

**Why this priority**: P2 — the single fused call covers the common case, and this covers the
shapes the story's own consumers actually have. The gateway stamps `ts` and computes `latency_ms`
after the call returns; a batching emitter builds now and emits later. If those callers cannot get
the guarantee from the library, they hand-roll it, which is precisely what this story exists to
stop. It is deliberately not a third abstraction — it is the fused call's two halves, exposed.

**Independent Test**: Meter a call whose `latency_ms` is only known after it returns, and emit it
without any validating call outside a guard.

**Acceptance Scenarios**:

1. **Given** malformed values, **When** a record is built under guard, **Then** the result is
   `None` rather than an exception.
2. **Given** a valid record and a raising sink, **When** it is emitted under guard, **Then** the
   result reports failure rather than raising.
3. **Given** the fused call, **When** its behaviour is compared to building-then-emitting, **Then**
   they agree — the fused call is composed of the two halves, not a parallel implementation.
4. **Given** a caller that learns `latency_ms` only after the metered call returns, **When** it
   stamps that value, **Then** it does so through one guarded construction — not by re-validating an
   already-built record, which `dataclasses.replace` would do and which would put a raise site back
   on the request path.
5. **Given** a *bad* late stamp (a negative `latency_ms`), **When** it is metered, **Then** it is a
   drop, not an exception into the request.

### Edge Cases

- **`KeyboardInterrupt` / `SystemExit`.** The guard catches `Exception`, never `BaseException`. A
  metering guard that swallows Ctrl-C or an interpreter shutdown would be a worse bug than the one
  it fixes; those are not "the record was bad".
- **A caller passes an unknown keyword** (`UsageRecord(**{"typo": 1})`). Raises `TypeError`, caught,
  dropped, logged. The metered request is unaffected — which is the whole point — but the log is the
  only signal the producer gets, reinforcing US3.
- **Logging itself raises.** An application can install a broken handler, filter, or an object whose
  `__repr__` throws. The logging call is itself guarded, because "never raises" must survive a
  hostile logging configuration; otherwise the guard has merely moved the throw site.
- **A sink raising `BaseException`** (a `SystemExit` from inside `emit`) propagates, by the same
  rule as above.
- **A record that is valid but semantically wrong** (`app_id="unknown"`): out of scope. The library
  cannot tell, and the guard does not try — it converts crashes into drops, not garbage into truth.
- **The guard is not the only way to build a record.** `UsageRecord(...)` stays public, stays
  validating, and stays raising. Off the request path — a batch import, a test, a migration —
  raising is the correct behaviour and the guard should not be used.
- **Return-value ambiguity.** A caller must not have to distinguish "dropped" from "emitted a record
  that happens to be falsy"; a `UsageRecord` is never falsy, but the seam's contract states the
  distinction explicitly rather than relying on that.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The library MUST provide a guarded way to construct a `UsageRecord` from raw values
  that returns `None` instead of raising when the values are invalid.
- **FR-002**: The library MUST provide a guarded way to emit an existing record to a `Sink` that
  reports failure instead of raising when the sink raises.
- **FR-003**: The library MUST provide a single fused call that does both, for the common
  request-path case, and it MUST be composed of FR-001 and FR-002 rather than reimplementing them.
- **FR-004**: The guards MUST catch `Exception` and MUST NOT catch `BaseException`.
- **FR-005**: The guards MUST cover both `ValueError` (invalid value) and `TypeError` (missing or
  unknown argument) — the two error types TOKWEIR-4 kept deliberately distinct.
- **FR-006**: Every drop MUST be logged at `WARNING` on a `tokenweir` module logger, stating that
  metering was skipped for that call, and carrying the underlying exception **where one was
  caught**. Refusing a non-`UsageRecord` (FR-018) is a rejection rather than a caught failure, so
  there is no exception to carry, and attaching one would render a misleading `NoneType: None`.
- **FR-007**: Construction drops and emission failures MUST be distinguishable in the logs.
- **FR-008**: The library MUST NOT call `basicConfig`, set a level, install a logging handler that
  emits, or otherwise write to stdout/stderr on the application's behalf. It MUST attach a
  `NullHandler` to the `tokenweir` package logger, which is what makes that true in practice: a
  library logger with no handler falls through to the standard library's `lastResort` handler,
  which writes `WARNING` and above — with the traceback — to stderr.
- **FR-017**: A drop MUST remain observable to a caller that has configured no logging, via the
  return value. FR-008 concerns where a *log* goes; it must not be satisfied by making the drop
  undetectable.
- **FR-009**: The logging call MUST itself be guarded, so a broken logging configuration cannot
  make a guarded call raise.
- **FR-010**: The guarded path MUST produce a record identical to direct construction for valid
  input — no added normalization, defaulting, or coercion.
- **FR-019**: No field name a producer might use may collide with a guarded call's own parameters.
  Every named parameter of a guarded call MUST be positional-only: otherwise a mapping carrying that
  key raises `TypeError` during *argument binding*, before any guard runs.
- **FR-020**: A guarded call MUST accept the field **mapping itself**, not only `**fields`. `**`
  unpacking happens in the caller's frame, so a mapping from JSON, a header dict, or generic code
  that carries a non-string key raises `TypeError: keywords must be strings` before any library code
  runs — a raise site no signature can guard at the splat. Keyword `overrides` MUST compose with the
  mapping (mapping first, overrides on top).
- **FR-021**: Stamping a value learned after the metered call (`latency_ms`, `ts`) MUST NOT require a
  second validating call on the request path. `dataclasses.replace` re-runs validation and raises, so
  it MUST NOT be the documented request-path pattern; building once with the stamp merged (FR-020)
  keeps the single validating call inside the guard.
- **FR-018**: Guarded emission MUST refuse anything that is not a `UsageRecord` rather than hand it
  to the sink. `build_record` returns `None` on a drop, so the naive composition of the two halves
  would otherwise push `None` into a conforming sink — which by contract cannot raise and would
  persist it. The guard converts crashes into drops, never into garbage in the store.
- **FR-011**: `UsageRecord.__init__` MUST keep raising on invalid input; this story adds a guarded
  seam beside it and does not soften the contract.
- **FR-012**: `Sink.emit`'s "MUST NOT raise" contract MUST be unchanged; the emission guard is
  defence against a non-conforming implementation, not a relaxation of the protocol.
- **FR-013**: The new seam MUST be exported from the `tokenweir` package namespace, alongside
  `Sink`, `NullSink`, `UsageRecord` and the rest.
- **FR-014**: The core MUST stay dependency-light (ADR-0001 Pillar 2) — standard library only.
- **FR-015**: The published JSON Schema and the wire contract MUST be unchanged; `SCHEMA_VERSION`
  MUST NOT change. This story adds no field and alters no serialization.
- **FR-016**: `README.md` MUST document the guarded seam and say plainly when to use it (on a
  metered request path) and when not to (off it, where raising is correct).

### Key Entities

- **Guarded construction**: turning raw values into a `UsageRecord`, converting any producer-side
  error into a dropped record plus a log line.
- **Guarded emission**: handing a record to a `Sink`, converting a contract-violating sink's
  exception into a reported failure plus a log line.
- **Drop**: the outcome in which no record is metered for a call and the metered call proceeds
  regardless. Always observable, never silent.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every invalid-input class TOKWEIR-4 rejects — blank identity field, negative
  count, non-integer count, `bool` count, wrong-typed optional, bad `pricing_mode`, bad
  `schema_version`, missing required argument — the guarded path returns a drop and raises nothing.
- **SC-002**: A sink that raises from `emit` cannot propagate an exception through the guarded path.
- **SC-003**: Every drop in SC-001 and SC-002 emits exactly one `WARNING` on a `tokenweir` logger
  that names the underlying exception — **every** case in the matrix, `TypeError` paths included,
  not a representative sample.
- **SC-004**: A guarded call with a logging configuration that raises still does not raise.
- **SC-009**: A subprocess that imports `tokenweir`, configures no logging, and drops a record
  writes nothing to stdout or stderr, and still observes the drop via the return value.
- **SC-010**: `emit_record` handed a non-`UsageRecord` returns `False` and the sink receives
  nothing.
- **SC-011**: A field mapping containing a non-string key, or that is not a mapping at all, is a
  drop when passed as a mapping — and the splatted form is shown to raise, so the reason the mapping
  form exists is pinned rather than asserted.
- **SC-012**: Every named parameter of `build_record` and `emit_usage` is positional-only.
- **SC-013**: A late stamp (`latency_ms` learned after the metered call) is emitted through one
  guarded construction, and a *bad* late stamp is a drop rather than an exception.
- **SC-005**: For valid input, the guarded path's record equals `UsageRecord(**fields)`.
- **SC-006**: The full suite passes via the authoritative command in `/workspace/.mado/project.yaml`
  (`/workspace/repo/.venv/bin/pytest`, `CI=true`, pass codes `0` and `5`), with the pre-existing
  tests unchanged in count and result.
- **SC-007**: `schema/usage-record.v1.json` is byte-identical to its current content, and
  `SCHEMA_VERSION` is still `1`.
- **SC-008**: `ruff check .` is clean.

## Assumptions

- **The branch is the existing one.** Work continues on `TOKWEIR-4-usage-record-contract` at the
  developer's explicit instruction, not on a fresh branch. A reviewer diffing against `main`
  therefore sees TOKWEIR-4 and TOKWEIR-12 as well; only the TOKWEIR-15 commits are under review
  here. The speckit scaffold's `create-new-feature.sh` was not run for the same reason — it
  force-creates an `NNN-`-prefixed branch — and the feature directory follows the repository's
  established `specs/<KEY>-<desc>/` convention instead.
- **The consumer-side adoption is not claimed.** This story lands the guarded seam in `tokenweir`
  and proves it. Making the AI Gateway call it is TOKWEIR-10, in a different repository; making the
  emitter client call it is TOKWEIR-6, not yet on this branch. Reported explicitly rather than left
  implied, because the story's acceptance is phrased in terms of those integrations.
- **The guard is opt-in.** Existing callers of `UsageRecord(...)` are unaffected. Nothing about the
  contract, the schema, or `Sink` changes behaviour; this is additive API surface only.
- **Drops are counted by the caller, not the library.** The return value is the hook. Shipping a
  metrics counter inside `tokenweir` would require picking a metrics library, which the
  dependency-light rule forbids and no requirement here asks for.
- **`ts` and `latency_ms` stay the producer's job.** The guard does not stamp timestamps or measure
  latency. It converts failures into drops; it does not enrich records.
