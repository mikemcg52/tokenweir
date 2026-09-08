# Feature Specification: Stop-hook usage emitter (transcript delta → tokenweir)

**Feature Branch**: `TOKWEIR-7-stop-hook-usage-emitter`
**Created**: 2026-09-08
**Status**: Draft — revised during implementation; see *Revision log* at the end.
**Jira**: TOKWEIR-7 (Story) — "Stop-hook usage emitter (transcript delta → tokenweir)"
**Input**: The Jira story, read with `mado-jira read TOKWEIR-7` on 2026-09-08 against the
`default` account (`https://bostoncio-cto.atlassian.net`) and quoted verbatim below rather than
reconstructed from the branch name. **Check it rather than trusting it**: `mado-jira read` resolves
from any pod on this project, and every review of this branch has re-read the story and confirmed
the quote. An earlier draft of this line claimed a reviewer could not reach the account, which was
false — and a sentence telling a reviewer the primary source is unreachable is exactly the sentence
that stops an implementer-revised spec from being checked against it.

> Build a Claude Code Stop hook that, on each response turn, reads `transcript_path` from stdin,
> sums the `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens,
> cache_read_input_tokens}` delta since the last emit, and calls the tokenweir emitter with
> `pricing_mode=subscription`. Configure non-blocking: exit 0, short timeout (~30s), optionally
> `async: true` — a failure must never disrupt the session.
>
> **Acceptance:** a Max-authenticated Claude Code turn produces exactly one usage record with
> correct token deltas; a forced emitter failure does not block the next turn.

Parent epic **TOKWEIR-2** (read the same way, same account, elisions marked `[…]`), quoted for the
attribution requirement it adds:

> Goal: Capture granular per-iteration usage when Claude Code runs on a Claude Max subscription,
> where API interception isn't possible (OAuth, not a base-URL-swappable API path).
> Approach (ADR-0001, Pillar 4): a deterministic Claude Code Stop hook (not an LLM skill/agent —
> that would burn tokens into the measured window). […] Phase/issue context comes from
> orchestrator-injected env vars, not the model.
> Scope now: store raw tokens/requests per iteration; defer %-of-limit / capacity modeling.
> **Acceptance:** a Max-authenticated stream emits one usage record per iteration, tagged with
> issue + phase, landing in the tokenweir store.

## Context

ADR-0001 Pillar 4 is the whole motivation. Under a Claude Max subscription the auth is OAuth and
there is no base-URL-swappable API path, so the interception that produces `api_metered` records
cannot see the traffic at all. The ADR's answer is to capture from Claude Code's *own* record:

> **Trigger:** the `Stop` hook — fires after Claude finishes a response turn, the natural
> per-iteration boundary. Configure `exit 0` / non-blocking (never `exit 2`) and a short `timeout`
> (e.g. 30s), optionally `async: true`, so a network failure never blocks the session — this *is*
> the off-critical-path guarantee.
>
> **Data:** the hook receives `transcript_path` on stdin; assistant messages in the session JSONL
> carry `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens,
> cache_read_input_tokens}`. The hook sums the delta since the last emit (a turn may span multiple
> assistant messages) and calls the `tokenweir` emitter.

ADR-0001 implementation checklist item 5 — *"Build the subscription capture adapter: `Stop` hook →
transcript delta → `tokenweir` emitter"* — is this story, and it is the last unbuilt half of the
dual-capture design.

### What already exists, and what is therefore not this story's work

| Piece | Where | State before this story |
|---|---|---|
| `UsageRecord`, `PricingMode.SUBSCRIPTION` | `tokenweir/contract.py` (TOKWEIR-4) | **Done.** The mode this hook stamps already exists and is already validated. |
| Guarded seam `build_record` / `emit_record` / `emit_usage` | `tokenweir/sink.py` (TOKWEIR-15) | **Done.** Construction failures are already drops rather than raises. |
| `BufferedEmitter` — the fire-and-forget client | `tokenweir/emitter.py` (TOKWEIR-6) | **Done**, including a *bounded* close. |
| `AMQPSink`, `DirectSink`, `PostgresSource` | `tokenweir/{amqp,sink,postgres}.py` | **Done.** The transports a record can leave by. |
| `gateway_usage` BIGINT token columns | `migrations/sql/001` | **Done**, and 001 names this hook as the reason: *"a producer that sums a turn's messages (the subscription Stop hook) […] is exactly where a count creeps past 2^31"*. |
| **A producer that reads a transcript** | — | **Missing.** Nothing in the library has ever read a Claude Code transcript. |
| **Turn-delta accounting across invocations** | — | **Missing.** Nothing remembers what was already metered. |
| **A hook entry point** | — | **Missing.** No console script, nothing Claude Code's `settings.json` can name. |

So this story is the producer side only. It **consumes** the emit-side contract; it does not
change it. A record this hook builds must be indistinguishable in kind from one the gateway builds
— that is what "both capture modes feed the same contract" means.

### Why the delta needs state, and why the state is cumulative

A transcript is append-only and holds the **whole session**, not one turn. Summing it gives a
session total, not a turn. The turn's number is therefore always a difference against something
remembered from the previous invocation.

The remembered thing is the **cumulative total already emitted**, not a file offset and not a set
of message identifiers:

- A cumulative baseline is self-correcting. If one invocation of the hook never ran (the process
  was killed, the machine slept, the timeout fired), the tokens it would have reported are not
  lost — they appear in the next turn's delta, which is a late record rather than a missing one.
- A file offset is not. It advances whether or not the emit that went with it succeeded, so a
  failed emit silently deletes a turn's tokens from the record forever.

The cost of the cumulative baseline is stated rather than hidden: a turn whose emit fails is
**merged into the next turn's record** rather than reported separately. Under a design whose first
rule is "never disrupt the session", a slightly coarse record beats a lost one.

"Emit fails" is read strictly. A buffered emitter *accepting* a record is not the same as a store
holding it, and treating acceptance as success would advance the baseline for a record a dead
broker later dropped — reintroducing exactly the loss this shape exists to prevent. The hook emits
one record and then exits, so it can afford to flush and ask what actually happened; a metered
request path could not, which is why the emitter's own contract stops at acceptance.

### Double counting is the real hazard, and it is not hypothetical

One assistant API response can appear as **several** lines in the transcript — Claude Code writes
an entry per content block boundary, and each carries the same `message.usage` object. Summing
lines therefore over-counts a turn, sometimes by a large factor, and the over-count is invisible
because every line is individually well-formed.

Deduplication by the API message id (`message.id`) is what makes the sum correct: usage belongs to
one API response, and one response has one id however many transcript lines mention it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A Max-authenticated turn produces exactly one correct usage record (Priority: P1)

A developer (or MADO's orchestrator) runs Claude Code on a Max subscription with the hook
installed. Claude finishes a response turn. One usage record appears in the tokenweir store
carrying that turn's token counts and nothing else's, tagged `pricing_mode=subscription`.

**Why this priority**: It is the story's first acceptance clause and the epic's whole purpose.
Without it there is no subscription capture at all.

**Independent Test**: Feed the hook a synthetic transcript and a synthetic stdin payload, point it
at a recording sink, and assert one record with the expected counts. No Claude Max session and no
broker are needed to test it, which is what makes it testable at all in CI.

**Acceptance Scenarios**:

1. **Given** a transcript whose turn holds three assistant messages with usage
   `(100/10/5/0)`, `(120/25/0/50)` and `(130/40/0/90)`, **When** the hook runs for the first time,
   **Then** exactly one record is emitted carrying the sums `input=350, output=75,
   cache_creation=5, cache_read=140`.
2. **Given** that first invocation has completed, **When** two further assistant messages are
   appended and the hook runs again, **Then** exactly one further record is emitted carrying only
   the two new messages' tokens — the first turn's are not counted twice.
3. **Given** a turn in which one API response was written to the transcript as four separate
   lines sharing one `message.id`, **When** the hook runs, **Then** that response's usage is
   counted **once**.
4. **Given** any transcript, **When** a record is emitted, **Then** its `pricing_mode` is
   `subscription`.

---

### User Story 2 - A forced emitter failure does not block or disturb the next turn (Priority: P1)

The broker is down, the DSN is wrong, the transcript is truncated mid-write, stdin is garbage, the
state directory is read-only. In every case Claude Code's next turn starts on time and the session
shows no error.

**Why this priority**: The story's second acceptance clause, and ADR-0001 Pillar 2's invariant
applied to this producer — *"a logging outage cannot affect availability"*. A hook that can fail a
session is worse than no hook.

**Independent Test**: Drive the hook's entry point with a sink whose `emit` raises, with a missing
transcript, with malformed stdin, and with an unwritable state directory; assert exit status 0 and
an empty stdout every time.

**Acceptance Scenarios**:

1. **Given** a sink that raises on every emit, **When** the hook runs, **Then** it exits `0` and
   writes nothing to stdout.
2. **Given** stdin that is not JSON at all, **When** the hook runs, **Then** it exits `0`.
3. **Given** a `transcript_path` that does not exist, **When** the hook runs, **Then** it exits
   `0` and emits nothing.
4. **Given** a transcript whose final line is a half-written JSON fragment (the file is being
   appended to as it is read), **When** the hook runs, **Then** the intact lines are still counted
   and the hook exits `0`.
5. **Given** a state directory that cannot be written, **When** the hook runs, **Then** it still
   emits the record and exits `0`.
6. **Given** a sink whose delivery hangs indefinitely, **When** the hook runs, **Then** it returns
   within the emitter's bounded close rather than waiting on the sink. *(Revised — see the
   revision log; the original demanded a time budget the hook no longer implements.)*

---

### User Story 3 - Records are attributed to issue and phase from the environment (Priority: P1)

MADO's orchestrator exports `MADO_ISSUE_KEY`, `MADO_PHASE`, `MADO_STREAM_ID` and
`MADO_PRICING_MODE` before launching Claude Code. Hooks inherit the process environment, so the
record carries the attribution without the model being asked for it.

**Why this priority**: The epic's acceptance says "tagged with issue + phase". A record that
cannot be attributed to a piece of work cannot answer the question the epic exists to ask.

**Independent Test**: Set the variables in the hook's environment, run it, and read the resulting
record's attribution fields. Unset them and confirm the record is still emitted.

**Acceptance Scenarios**:

1. **Given** `MADO_ISSUE_KEY=TOKWEIR-7` and `MADO_PHASE=review`, **When** a record is emitted,
   **Then** the record carries the issue key and the phase in its attribution fields.
2. **Given** `MADO_STREAM_ID=stream-42`, **When** several turns are metered, **Then** every one of
   their records carries `stream-42` as the parent, so the stream's turns roll up.
3. **Given** none of the `MADO_*` variables are set (a developer's own laptop), **When** the hook
   runs, **Then** a record is still emitted, with the attribution fields absent rather than blank.
4. **Given** `MADO_PRICING_MODE` is set to a value the contract does not recognize, **When** the
   hook runs, **Then** the record is still emitted, stamped `subscription`.

---

### User Story 4 - Installing the hook is a documented, copy-pasteable step (Priority: P2)

Someone wiring a new stream pod needs to know what to put in `settings.json`, what environment the
hook reads, and how to tell whether it is working.

**Why this priority**: An adapter nobody can install captures nothing. Lower than P1 because the
capability is what is being built; the documentation makes it reachable.

**Independent Test**: Follow the README section on a clean checkout and confirm the hook runs.

**Acceptance Scenarios**:

1. **Given** the README, **When** a reader copies the `settings.json` fragment, **Then** it names
   the shipped entry point, sets `timeout` to a value at or under 30 seconds, and does not use a
   blocking exit code.
2. **Given** the README, **When** a reader wants to know where records go, **Then** the transport
   environment variables and the no-transport default are documented.

---

### Edge Cases

- **A turn that added no new usage** (the hook fires but the transcript gained nothing new —
  a `/clear`, an interrupted turn, a duplicate Stop event): no record is emitted. A zero-token
  record is noise that would inflate the request count without adding a single token.
- **A transcript replaced at the same path** (a session resumed into a fresh file, or the file
  truncated): the recomputed cumulative total is *lower* than the remembered baseline. The
  difference is not negative tokens; the baseline is stale. Reset the baseline to what the file
  now says and emit nothing for that transition.
- **A state file that is corrupt or unreadable**: treated as absent. The turn is over-counted once
  (the whole session's tokens land in one record) rather than lost, and the next turn is correct
  again.
- **Two hooks racing on one transcript**: the state write must not leave a half-written file that
  poisons every later invocation.
- **A turn spanning more than one model** (a plan-mode draft on a cheaper model, finalized on the
  primary one, both inline in the same transcript): the counts are summed across models and the
  record names one model — see Assumptions. (An earlier version of this bullet used a subagent as
  the example; that was wrong for the reason the Revision log's subagent-capture entry explains,
  and the example is corrected rather than the mechanism, which was never at issue.)
- **A very large transcript**: the file is read in a streaming fashion; the hook must not need to
  hold the whole session in memory to add up four integers.
- **`transcript_path` pointing outside the session** (absent from stdin, empty, a directory): no
  record, exit 0.
- **The usage object holding non-integer or negative values**: those values are refused rather
  than propagated into a record the contract would reject anyway.

## Requirements *(mandatory)*

### Functional Requirements

**Reading the hook's input**

- **FR-001**: The hook MUST read a JSON object from stdin and take `transcript_path` from it.
- **FR-002**: The hook MUST tolerate stdin that is empty, not JSON, not an object, or missing
  `transcript_path`, by emitting nothing and exiting `0`.
- **FR-003**: The hook MUST use `session_id` from the same stdin payload when it is present, for
  record identity, and MUST work without it.
- **FR-004**: The hook MUST NOT write anything to stdout. Claude Code parses a hook's stdout;
  diagnostics belong on stderr.

**Summing the turn**

- **FR-005**: The hook MUST sum `message.usage.input_tokens`, `.output_tokens`,
  `.cache_creation_input_tokens` and `.cache_read_input_tokens` across the transcript's entries.
  The test applied is **"does this entry carry a usage object"**, not "is this entry typed
  `assistant`" — the former is the property that actually matters and the one least likely to be
  invalidated by a transcript-format change. Two independent reviews checked the two against a live
  Claude Code transcript in this pod and found them to select the same lines; that transcript is
  session data and is **not** checked in, so no test in this repo exercises real transcript output
  (see the verification gap noted in the revision log).
- **FR-006**: The hook MUST count each distinct API response exactly once, keyed on `message.id`,
  even when the transcript holds several entries carrying that id. Where two entries share an id,
  the **first** is counted and the rest skipped: the case this exists for is one response repeated
  verbatim, so first and last are the same value, and a genuine disagreement between two entries
  claiming one response id would be a format change rather than something to pick a winner for.
- **FR-007**: The hook MUST skip transcript lines that are blank, are not JSON, are not JSON
  objects, carry no usage, or carry a usage object that is not a mapping — and MUST continue
  reading the rest of the file rather than abandoning it.
- **FR-008**: The hook MUST treat a usage value that is missing, non-integer or negative as zero
  for that field, rather than propagating it into a record.
- **FR-009**: The hook MUST read the transcript incrementally rather than materializing the whole
  file in memory.

**The delta across invocations**

- **FR-010**: The hook MUST persist, per transcript, the cumulative token totals it has already
  emitted, and MUST report each turn as the difference between the current cumulative totals and
  that baseline.
- **FR-011**: The hook MUST keep one baseline per transcript path, so two concurrent Claude Code
  sessions do not consume each other's deltas.
- **FR-012**: The hook MUST advance the persisted baseline **only after** the record was actually
  **stored** — not merely accepted — so that a turn refused at the seam, or accepted and then lost
  against a dead transport, is carried into the next turn rather than dropped. The test MUST be
  **acceptance plus the absence of a reported drop** — `EmitterStats.delivered` together with the
  transport's own `dropped` counter — and MUST NOT be a success counter. `EmitterStats.delivered`
  alone is insufficient (a conforming `Sink` may not raise, so both shipped adapters catch their
  own transport failure, count a drop and return normally). A success counter is unsound in both
  directions: `AMQPSink.published` counts frames handed to a socket over a transport with no
  publisher confirms, and any conforming sink that keeps no such counter would look permanently
  failed. A sink exposing no `dropped` counter MUST NOT be consulted, and its acceptance stands.

  **The limit of the claim MUST be documented**: a live broker with a missing exchange accepts the
  frame and reports no drop, so those tokens are lost and nothing in the hook can see it. The
  requirement is "the transport did not report losing it", not "a store holds it".
- **FR-012a**: A transport that was **configured** and could not be constructed MUST NOT count as
  stored. It degrades to a discarding sink so the hook cannot fail, and a discarding sink accepts
  everything — so without this the first turns of every session started before its broker was up
  would be deleted. An **unconfigured** hook is the opposite case and MUST advance: it is
  discarding by choice, and holding the baseline would make the first turn after a transport is
  configured report the whole session.
- **FR-012b**: The sink MUST NOT be constructed when there is nothing to emit. A `Stop` hook fires
  on every turn and many add no tokens; opening a transport to discover that is a connection per
  turn, and one that can wedge with nothing at stake.
- **FR-013**: The hook MUST emit nothing when every token delta is zero.
- **FR-014**: The hook MUST treat a cumulative total below the stored baseline as a replaced
  transcript: reset the baseline to the current totals and emit nothing.
- **FR-015**: The hook MUST treat an absent, unreadable or corrupt state file as an empty
  baseline.
- **FR-016**: The hook MUST write the state file atomically, so that an interrupted write cannot
  leave a partial file in its place.
- **FR-017**: A state directory that cannot be created or written MUST NOT prevent the record from
  being emitted, and each failure MUST be logged. The **cost** is that the turn is re-reported
  until the baseline can be stored: a single failure over-counts one turn, and a persistent one
  re-reports the whole session every turn. That cost MUST be documented accurately wherever it is
  described — an understated one ("it costs one turn") reads as a bounded loss and is not, and so
  does a claim that the re-reports are collapsible duplicates. They are only duplicates while the
  transcript is **static**; on a live session each re-report ends on a different response, so the
  ids differ, the counts climb, and nothing marks them as re-reports. The documentation MUST say
  that, and a test MUST pin it.

**The record**

- **FR-018**: Every emitted record MUST carry `pricing_mode=subscription` by default, overridable
  only to another mode the contract recognizes; an unrecognized `MADO_PRICING_MODE` MUST fall back
  to `subscription` rather than dropping the record.
- **FR-019**: Every emitted record MUST carry a non-blank `request_id` that identifies the turn:
  two **distinct** turns MUST NOT share one. It MUST be **stable** rather than fresh per
  emission — if the same turn is re-reported (because its baseline could not be stored), the
  re-report MUST carry the same id, so that a duplicate is recognizable as a duplicate rather
  than appearing as a new turn. The store deliberately puts no unique constraint on the column
  (`001_gateway_usage.sql`) because it is an append-only log expecting at-least-once delivery;
  this requirement is what makes that tolerance usable. A turn whose last counted entry carries no
  identity MUST fall back to a fresh identifier rather than inherit the previous turn's — an
  inherited id hands two distinct turns one identity, which is worse than an unstable one, because
  the consumer-side remedy above would then delete a real turn.
- **FR-020**: The record MUST carry `model` taken from the most recent counted assistant message,
  and MUST fall back to a non-blank placeholder when the transcript names none — a record the
  contract would refuse is worse than one whose model is unknown.
- **FR-021**: The record MUST take its attribution from the environment and never from the
  transcript's content: `MADO_ISSUE_KEY` → workload, `MADO_PHASE` → queue, `MADO_STREAM_ID` →
  parent request id.
- **FR-022**: An attribution variable that is unset **or blank** MUST leave the corresponding
  field absent (`None`), never blank.
- **FR-023**: The record MUST carry a UTC ISO-8601 timestamp, preferring the counted turn's own
  last timestamp over the hook's wall clock.
- **FR-024**: `app_id` and `endpoint` MUST be non-blank, defaulted, and overridable by
  environment variable.

**Never disturbing the session**

- **FR-025**: The hook MUST exit `0` on every path, including every unexpected exception. It MUST
  NEVER exit `2` (Claude Code's blocking status).
- **FR-026**: No exception raised by transcript reading, state handling, record construction,
  sink construction or emission may escape the hook.
- **FR-027**: The hook's total run time MUST be bounded by Claude Code's own hook `timeout`, which
  the documented `settings.json` fragment sets. The hook MUST NOT implement a competing timer of
  its own: the story's non-blocking clause names the host's timeout as the mechanism, and a second
  bound inside the hook is scope this story does not carry. *(Revised after review 2, which found
  the self-imposed budget to be over-scope. The case for giving up early rather than being killed —
  a synchronous hook that wedges costs the developer the whole 30 seconds — is real and is recorded
  in the run's report as a follow-up, not built here.)*
- **FR-028**: The hook MUST bound the emitter's flush at exit, so an unreachable broker delays the
  process by a bounded interval rather than indefinitely.
- **FR-029**: Emission MUST go through the library's guarded seam and the buffered client, not
  through a bare sink call — the guarantees are already implemented and MUST NOT be re-derived
  here.

**Transport and packaging**

- **FR-030**: The hook MUST select its sink from the environment: an AMQP URL when one is given, a
  Postgres DSN when one is given, and a no-op sink when neither is. With **both** given the broker
  MUST win — a deployment that configured one has said where records should survive an outage, and
  writing past it to the store would discard that. Each branch MUST be tested positively, not only
  through its failure path.
- **FR-031**: Importing the hook's module MUST NOT import `pika` or `psycopg`. The core stays
  dependency-light (ADR-0001 Pillar 2); a transport is imported only when the environment selects
  it.
- **FR-032**: The hook MUST be installable as a console script so `settings.json` can name it, and
  MUST also be runnable as `python -m`.
- **FR-033**: The README MUST document installation, the `settings.json` fragment, every
  environment variable the hook reads, and where records go when no transport is configured.

### Key Entities

- **Hook input**: the JSON object Claude Code writes to the hook's stdin. Carries
  `transcript_path` and `session_id`.
- **Transcript entry**: one line of the session JSONL. The ones that matter carry
  `message.id`, `message.model`, `message.usage` and a timestamp.
- **Turn delta**: the four token counts attributable to the turn just finished — the difference
  between the transcript's current de-duplicated cumulative totals and the persisted baseline.
- **Baseline state**: the per-transcript record of cumulative totals already emitted. Not part of
  the published contract; an implementation detail of this producer.
- **Usage record**: the existing `UsageRecord`. Unchanged by this story.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A turn containing *n* assistant messages produces exactly **one** usage record whose
  four token counts equal the sum over those messages' distinct API responses.
- **SC-002**: Across *k* consecutive turns, the sum of the *k* records' token counts equals the
  transcript's de-duplicated session total — no turn is double-counted and none is lost.
- **SC-003**: 100% of hook invocations exit `0`, including those where the sink raises, the
  transcript is missing or malformed, stdin is garbage, or the state directory is unwritable.
- **SC-004**: The hook writes zero bytes to stdout on every path.
- **SC-005**: With a sink that never completes, the hook still returns within the emitter's close
  timeout — measured in seconds, and well below Claude Code's configured `timeout`.
- **SC-006**: Every emitted record validates against the published v1 usage-record schema and
  carries `pricing_mode=subscription`.
- **SC-007**: With the `MADO_*` variables set, 100% of records carry the issue key and phase; with
  them unset, 100% of records are still emitted.
- **SC-008**: A bare install of the core (no extras) can import the hook module and run it
  end-to-end against a no-op sink.
- **SC-009**: A reader following the README can install the hook and see a record produced,
  without reading the source.

## Assumptions

Recorded because the story did not specify them and a reasonable default was chosen.

- **Transcript shape.** Assistant entries carry `message.usage` with the four named fields, and
  `message.id` identifies the API response. This matches ADR-0001's "verified 2026-08-07" reading
  of the Claude Code docs. The parser treats every field as optional and every line as untrusted,
  so a shape change degrades to under-counting rather than to a crash.
- **`ephemeral_5m` / `ephemeral_1h` cache fields are not stored.** The v1 contract has four token
  fields and this story does not change the contract. They are subsumed by
  `cache_creation_input_tokens`, which is what the contract has a place for.
- **Sidechain (subagent) entries would be counted if the transcript held one inline** — the
  selection rule is "carries usage", not `type` or `isSidechain`. **This was believed to mean
  subagent usage is captured; it does not.** In current Claude Code versions a subagent's usage is
  written to sibling `<session>/subagents/*.jsonl` files, never inline at `transcript_path`, so the
  case this bullet describes does not occur and subagent tokens are not currently metered by this
  hook. See the Revision log's subagent-capture entry.
- **One record per turn even when a turn spans several models.** The story's acceptance says
  "exactly one usage record"; splitting per model would produce several. The counts are summed and
  `model` names the most recent counted message. A per-model breakdown is a contract question
  (v2), deliberately out of scope here.
- **`MADO_PHASE` is carried in the record's `queue` field.** The v1 contract has no phase field and
  adding one is a schema-version change this story does not own. `queue` is the closest available
  attribution slot and is nullable and unconstrained. This is a documented compromise, not a
  natural fit, and it is the item most likely to be revisited when the contract next moves.
- **`MADO_STREAM_ID` is carried as `parent_request_id`.** Its documented purpose — *"roll them
  back up to the request the user actually made"* — is exactly the relation a stream has to its
  turns.
- **`endpoint` defaults to a hook-specific label, not `/v1/messages`.** A turn is an aggregate of
  several API calls; labelling it with a single-call endpoint would let a report blend aggregates
  with individual calls under one key.
- **`latency_ms` is left unset.** The transcript's timestamps describe wall-clock gaps between
  writes, which is not the latency of a model call, and inventing a number is worse than omitting
  an optional field.
- **State lives under a cache directory keyed by transcript path**, honouring an explicit override
  environment variable. It is disposable: losing it costs one over-counted turn, never a lost one.
- **A bounded flush can produce a duplicate, and the run errs that way on purpose.** The baseline
  advances only if the emitter reports the record delivered within `close_timeout`. A sink that is
  still publishing when that expires, and then *succeeds*, gets the record while the baseline stays
  put — so the next turn re-reports it. The window is the close timeout (3 seconds by default) and
  it is narrow, but it is not zero. That re-report is **not** a collapsible duplicate: it covers a
  longer span and ends on a different response, so it carries a different `request_id` and a larger
  count, and the store holds both. Erring the other way — assuming success on a timeout — would
  turn the same window into a silent **loss**, which is worse: an overstatement can be checked
  against the transcript that produced it, and a turn that vanished leaves nothing to check.
- **The `Unverified` parity check stands.** ADR-0001 records that subscription-vs-API-key token
  parity is unconfirmed. This story captures what the transcript reports; it does not verify that
  the transcript's numbers match an API-key session's, which the ADR keeps as its own item.

## Revision log

This document was written before the implementation and **edited twice while the branch was open**.
That is worth stating plainly rather than leaving to `git log`: a criteria document revised by its
own implementer is not an independent authority over the code, and a reader grading the branch
should know which clauses were written after the fact and why.

| Round | Clause | Change | Why |
|---|---|---|---|
| Fix 1 | FR-012 | "handed to the emitter" → "reported delivered" | The weaker wording let a buffered emitter's *acceptance* count as success, which is the loss the cumulative baseline exists to prevent, one layer down. |
| Fix 1 | FR-019 | "unique to the turn" → stable, non-colliding, with the reasoning | The original was ambiguous between "identifies the turn" and "distinct per emission", and the two want opposite implementations. |
| Fix 1 | FR-017 | Added the accuracy obligation | Review 1 found the documented cost understated. |
| Fix 2 | FR-027 | "MUST bound its own run time" → "MUST NOT implement a competing timer" | Review 2 found the self-imposed budget to be scope the story does not carry; the story assigns the bound to Claude Code's own `timeout`. **This is a reversal, not a clarification.** |
| Fix 2 | SC-005 | Rewritten around the emitter's close timeout | Followed FR-027. |
| Fix 2 | FR-005, FR-006, FR-030 | Added the decisions the code had made silently | Selection is on "carries usage" not `type`; duplicate ids resolve take-first; the broker wins over a DSN. |
| Fix 3 | US2 scenario 6 | Reconciled with FR-027 | Missed in fix 2 — it still demanded the removed budget. |
| Fix 3 | FR-012, +FR-012a, +FR-012b | Named *how* "stored" is measured; added the degraded-transport and lazy-construction rules | Review 3 found FR-012 unmet by both shipped transports: `EmitterStats.delivered` means "did not raise", and a conforming sink never raises. |
| Fix 3 | FR-017 | Added the live-session clause | Review 3 found the "collapsible duplicates" remedy does not exist once the transcript grows. |
| Fix 4 | FR-012 | Success counter → **acceptance plus no reported drop**, with the limit of the claim made a requirement | Review 4 showed the success counter unsound both ways: `AMQPSink.published` counts socket frames (no publisher confirms — `amqp.py` documents this), and a conforming sink keeping no such counter would look permanently failed. |
| Fix 4 | Assumptions (close-timeout window) | Struck the "duplicates share an id" defence | Review 4 found the same live-session error review 3 had corrected elsewhere, left standing here. |
| Fix 5 | Header | Struck the claim that a reviewer cannot reach the Jira account | It was false, and it discouraged the one check that keeps this document honest. |
| Fix 5 | FR-006 | De-duplication is on `message.id` **only** | The entry-`uuid` fallback de-duplicated nothing `message.id` had not already caught, while widening what counts as an identity, and no test held it. |
| Fix 6 | `ScanResult`, `run` | Added `ScanResult.readable`; `run` no longer re-anchors the baseline on an unreadable transcript | Terminal review, High (H1). An unopenable file and a genuinely fresh one both scanned to empty totals, and `run` could not tell them apart — a transient `EMFILE`/permission blip zeroed the baseline exactly as a real rotation would, and the next successful read re-reported the whole session as one turn. |
| Fix 6 | `scan_transcript` docstring, Edge Cases, Assumptions | Corrected the "sidechain entries are counted" claim | Terminal review, High (H2). The claim was measured against a real session directory and found false: subagent usage lives in sibling `subagents/*.jsonl` files never handed to this hook, not inline in the main transcript. The code was not wrong — it would count such an entry if one existed — the claim and its test's fixture shape were. See the subsection below. |

The story itself (quoted at the top, re-read from Jira each round) is unchanged throughout and is
the scope ceiling these revisions were measured against.

### A verification gap, stated rather than closed

The story's first acceptance clause — *"a Max-authenticated Claude Code turn produces exactly one
usage record with correct token deltas"* — is exercised in this repo only against **synthetic**
transcripts built by the test helper, which encodes the same format assumption the parser makes.

Two independent reviews did drive the hook against live Claude Code transcripts in this pod. Both
reported exactly one record with counts matching their own de-duplicated recount, and both found
de-duplication doing substantial work: 50 of 82 usage-bearing ids on more than one line in the
first, 74 of 124 in the second, with a naive line-sum over-counting `cache_read` by ~80% in each.
The second also checked the selection rule against real output — every usage-bearing entry was
typed `assistant` and carried a `message.id`, and no id's usage objects disagreed across lines.

None of that is in CI. Those transcripts are developers' own session data and are not checked in.

The check that would close this is one redacted real transcript committed as a fixture with an
expected-total assertion. It is deliberately not done here: redacting session content reliably is
its own piece of work, and doing it carelessly is how a test fixture ends up carrying somebody's
conversation into a public repository. **Treat AC 1 as verified in a pod and unverified in CI.**

### A scope disagreement, recorded rather than resolved

Reviews 4 and 5 both hold that the "was it kept?" check — `SinkChoice.degraded`, the named drop
counter, and the `kept` computation in `run` — is scope TOKWEIR-7 does not carry, since the story
says only *"calls the tokenweir emitter"* and the emitter's published contract stops at acceptance.
Review 4 asked for it to be cut; review 5 restated it as a Med and conceded that the *ordering*
(advance the baseline after the emit, not before) is in scope.

It was kept, and the reasoning belongs in the record rather than in a commit message nobody will
find:

- Review 3 demonstrated, with a repro against this library's own `DirectSink`, that advancing on
  the emitter's number **silently deletes a turn** whenever the store is unreachable —
  `EmitterStats.delivered` means "the sink did not raise", and a conforming `Sink` may not raise.
  Cutting the check reintroduces a defect that was found, reproduced and fixed on this branch.
- The epic's acceptance is that records **land in the tokenweir store**. A capture path that loses
  a turn to a routine broker restart does not meet it.
- What review 4 *was* right about is the mechanism, and that was changed: a success counter is
  unsound both ways, so the question became "did the transport report losing it", the residual is
  documented, and after review 5 the counter is named by whoever built the sink instead of being
  sniffed off an arbitrary object.

A reviewer who still considers the layer out of scope is not wrong to; it is a judgement about
where this story ends, and the counter-argument is that deleting it knowingly ships a data-loss
path. **The follow-up that would settle it is publisher confirms in `AMQPSink`**, which would make
the answer authoritative instead of merely honest, and which is genuinely a separate story.

### The subagent-capture claim was false, and is corrected here

Fix round 5 shipped a docstring, an Edge Case bullet, an Assumption and a test all asserting the
same thing: sidechain (subagent) entries are inline in the transcript, tagged `isSidechain: true`,
and are therefore summed into the turn's usage. The terminal review checked this against a live
Claude Code session directory and found it false — 162 distinct subagent message ids, none of them
in the main transcript file — and the fix-round cap was reached before it could be corrected on
this branch.

The check was repeated independently while fixing it, against a different real session directory on
the reviewing machine: the main transcript held 111 `isSidechain` entries, **all `false`**; every
entry with `isSidechain: true` and a `usage` object lived in a sibling `subagents/agent-*.jsonl`
file that `scan_transcript` is never handed. Two independent measurements against two different real
sessions agree.

**What was actually wrong, precisely.** `scan_transcript`'s selection rule — "does this entry carry
usage", not `type` or `isSidechain` — was never the defect; it would sum an inline sidechain entry
correctly if one existed. What was wrong is the belief that one ever does. The shipped test,
`test_counts_sidechain_subagent_entries`, fabricated exactly the entry shape the review found does
not occur, so it measured the selection rule against a fixture reality doesn't produce and called
that "subagent usage is captured" — a claim no fixture built from real data could have supported.

**What this fix does, and does not, do.** The docstring, the Edge Case bullet and the Assumption
above are corrected to say plainly that subagent tokens are not currently metered. The misleading
test is kept — the selection rule is still correctly mechanical — but retitled and re-documented so
it cannot be read as evidence of a capability that does not exist. **Actually capturing subagent
usage is not done here.** It needs its own design: discovering which `subagents/*.jsonl` files
belong to *this* turn (a Stop hook fires once per main-thread turn, not once per subagent), reading
files that may still be open for writing on the same schedule this module already handles for the
main transcript, and its own de-duplication and baseline state per subagent file. That is real,
separate work, named here as the follow-up the terminal review already suggested rather than
attempted as part of a claim-correction fix.
