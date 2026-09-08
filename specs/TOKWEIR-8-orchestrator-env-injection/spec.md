# Feature Specification: Orchestrator env injection for phase/issue context

**Feature Branch**: `TOKWEIR-8-orchestrator-env-injection`
**Created**: 2026-09-08
**Status**: Draft
**Jira**: TOKWEIR-8 (Story) — "Orchestrator env injection for phase/issue context"
**Input**: The Jira story, read with `mado-jira read TOKWEIR-8` on 2026-09-08 against the
`default` account (`https://bostoncio-cto.atlassian.net`), quoted verbatim below rather than
reconstructed from the branch name.

> Have MADO's orchestrator inject MADO_ISSUE_KEY, MADO_PHASE, MADO_STREAM_ID, MADO_PRICING_MODE
> into the stream-pod environment per iteration; the Stop hook reads them (hooks inherit the
> process env) onto the usage record. Attribution comes from the orchestrator, not the model.
> Define the phase taxonomy (1st review, 1st bug fix, …) from the orchestrator's feature-state /
> ACP lifecycle.
>
> **Acceptance:** emitted records carry the correct issue key and phase for the iteration; phase
> labels match the orchestrator lifecycle.

Parent epic **TOKWEIR-2** (read the same way, same account, elisions marked `[…]`):

> Goal: Capture granular per-iteration usage when Claude Code runs on a Claude Max subscription
> […] Phase/issue context comes from orchestrator-injected env vars, not the model.
>
> **Acceptance:** a Max-authenticated stream emits one usage record per iteration, tagged with
> issue + phase, landing in the tokenweir store.

## Context

TOKWEIR-7 built the *reading* half. `tokenweir.claude_code.attribution_from_env()` already takes
`MADO_ISSUE_KEY`, `MADO_PHASE`, `MADO_STREAM_ID` and `MADO_PRICING_MODE` out of the process
environment and puts them on the record, and already treats unset and blank alike (FR-021, FR-022
of that spec). Nothing in this story changes where attribution comes from — that sentence is
already true.

What is missing is everything on the *other* side of the variable:

| Missing piece | Consequence today |
|---|---|
| A defined phase vocabulary | `MADO_PHASE` is free text. `review`, `Review`, `1st review`, `review 1` and `review-1` are five different strings for one phase, and a report that groups by phase sees five lanes. |
| An executable definition of the env block | The orchestrator, in another codebase, has to re-derive four variable names and their value rules from prose. A typo in a name is silent: the hook reads `None` and the record is simply unattributed. |
| A written injection contract | The MADO-side change has no specification to be written against. |

The story's own words name the vocabulary problem — *"Define the phase taxonomy (1st review, 1st
bug fix, …)"* — and its acceptance is stated in terms of it: *"phase labels match the orchestrator
lifecycle"*. Matching requires something to match against, and that something does not exist yet.

### The repo boundary, stated up front

MADO's orchestrator is `services/orchestrator/` in the **`mado`** repository (Python 3.11, k8s
namespace `mado-system`). It is not in this repository and cannot be edited from this stream pod,
whose workspace holds `token-weir` alone.

So this story is delivered in two parts across two repositories, and **this spec covers only the
part that lives here**:

| Part | Repo | This story? |
|---|---|---|
| The phase taxonomy — the labels themselves, their canonical form, and how a written label is normalized to it | `token-weir` | **Yes.** |
| The env-injection contract as *code* the orchestrator calls to build the block it exports | `token-weir` | **Yes.** |
| The hook conforming to the taxonomy when it reads `MADO_PHASE` | `token-weir` | **Yes.** |
| The orchestrator actually exporting the block into the stream pod per iteration | `mado` | **No** — a separate change against the contract this story publishes. |

This is a scope statement, not a scope reduction: the half that is deferred is deferred to the
repository that owns it, and it is a smaller and safer change for being written against a
published contract rather than against prose. The run's report says so explicitly, so nobody reads
a green run here as "MADO now injects the variables".

### Where the lifecycle vocabulary comes from

The story says the taxonomy comes from *"the orchestrator's feature-state / ACP lifecycle"*. The
authoritative, machine-checkable statement of that lifecycle available in the pod is MADO's own
phase stamper, `mado-phase`, whose `--phase` argument documents its permitted values:

```
--phase PHASE   one of: implementation, review, fix
```

Those three are the lifecycle's phase *kinds*, and the loop that drives them
(`/mado-implement`: implement → review → fix → review → …) is what makes a kind recur. The
story's own examples — *"1st review, 1st bug fix"* — are a kind plus which occurrence of it, so
an occurrence number is the second and last dimension the taxonomy needs.

The taxonomy is therefore deliberately **small and closed at the kinds, open at the occurrence**:
three kinds because three are what the orchestrator names, and any occurrence number because the
loop's fix-round cap is configurable and a taxonomy that stopped at "3rd review" would be wrong
the first time somebody passed `--fix-rounds 8`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A phase has exactly one label (Priority: P1)

Someone reporting on token spend groups a project's usage records by phase and gets one row per
phase of the run — not five rows for five spellings of "review".

**Why this priority**: This is the story's acceptance criterion ("phase labels match the
orchestrator lifecycle"), and it is the piece with no existing implementation at all.

**Independent Test**: Write the same phase five ways, normalize each, and observe one label.

**Acceptance Scenarios**:

1. **Given** the phase written as `1st review`, `review 1`, `Review #1`, `REVIEW-1` or
   `review_1`, **When** it is normalized, **Then** every one of them yields the label `review-1`.
2. **Given** the phase written as `1st bug fix` or `bugfix 1`, **When** it is normalized,
   **Then** each yields `fix-1`.
3. **Given** a phase kind written with no occurrence — plain `review` — **When** it is
   normalized, **Then** the label is `review`, with no occurrence number invented.

---

### User Story 2 - The orchestrator builds the env block from a definition, not from prose (Priority: P1)

The engineer wiring the `mado` side asks this library for the environment to inject, passing the
issue key, the phase and the stream id it already holds, and exports what comes back.

**Why this priority**: It is the story's title. A contract expressed only in a README is
re-implemented by hand at the other end, and the failure mode of a hand re-implementation —
a mistyped variable name — produces records that are silently unattributed rather than an error.

**Independent Test**: Call the builder with an issue key, phase and stream id; check the returned
mapping is exactly the four documented variable names with the expected values.

**Acceptance Scenarios**:

1. **Given** an issue key, a phase kind with an occurrence, and a stream id, **When** the
   environment block is built, **Then** it contains `MADO_ISSUE_KEY`, `MADO_PHASE`,
   `MADO_STREAM_ID` and `MADO_PRICING_MODE`, with the phase already in canonical form.
2. **Given** a field the caller has no value for — no stream id, say — **When** the block is
   built, **Then** that variable is exported as the empty string rather than omitted, so an
   exported block always clears a value left over from the previous iteration.
3. **Given** a value that is not usable as attribution — an issue key that is blank — **When**
   the block is built, **Then** the caller is told so rather than shipping a record attributed to
   whitespace.

---

### User Story 3 - A record carries the iteration's issue key and phase (Priority: P1)

The stream pod runs a turn during the second fix round of TOKWEIR-8; the record that lands in the
store says so.

**Why this priority**: The story's first acceptance clause, and the end-to-end property the other
two stories exist to produce.

**Independent Test**: Put the built block into the environment, run the hook's attribution read,
and compare the resulting record fields against the values that went in.

**Acceptance Scenarios**:

1. **Given** an environment built by the injection helper for phase "2nd fix" of `TOKWEIR-8`,
   **When** the hook builds a record, **Then** `workload` is `TOKWEIR-8` and `queue` is `fix-2`.
2. **Given** an orchestrator that exported a phase in a non-canonical spelling — because it was
   written before this contract, or by hand — **When** the hook builds a record, **Then** `queue`
   still holds the canonical label.
3. **Given** a phase this taxonomy does not recognize, **When** the hook builds a record,
   **Then** the record still carries the value as written rather than losing it, and a diagnostic
   notes that the label is not in the taxonomy.

---

### User Story 4 - The MADO-side change has a contract to be written against (Priority: P2)

The engineer making the `mado` change reads one page that says which variables to export, what
their values may be, when to export them, and what happens if they get it wrong.

**Why this priority**: The deferred half of this story cannot be done well without it, and this
repo is where the contract lives.

**Independent Test**: Read the README section and the module docstring; check every variable, the
canonical label grammar and the per-iteration timing are stated.

**Acceptance Scenarios**:

1. **Given** the README, **When** it is read by someone implementing the orchestrator side,
   **Then** it names the four variables, the label grammar, and the requirement to re-export on
   every iteration.

---

### Edge Cases

- **A phase kind that is not in the taxonomy** (the ACP lifecycle grows a `deploy` phase before
  this library hears about it). The value is preserved as written, whitespace-normalized, and
  noted — never dropped and never coerced into a kind it is not. Losing the attribution would be
  a worse outcome than carrying an unrecognized one.
- **An occurrence of zero or a negative number.** Not a real occurrence; the label is rejected at
  the producer and left as written at the consumer.
- **An occurrence written as a huge number.** Accepted — the fix-round cap is configurable and
  nothing here should decide how many rounds are too many.
- **A phase that is unset, or set to the empty string.** Already answered by TOKWEIR-7's FR-022:
  both mean "no phase" and leave the field `None`. This story must not turn a blank into a label.
- **A record whose phase is set but whose issue key is not.** Permitted; the fields are
  independent and both are nullable in the contract.
- **The orchestrator re-exports a block while a previous iteration's variables are still set.**
  Every variable in the block is written on every iteration, so a value never survives into an
  iteration it does not belong to.
- **Ordinal spellings beyond `1st`/`2nd`/`3rd`** — `21st`, `11th`. Handled by the same rule, and
  `11th` is not `11st`.

## Requirements *(mandatory)*

### Functional Requirements

**The taxonomy**

- **FR-001**: The library MUST define the phase kinds of the orchestrator lifecycle as a closed,
  named set: `implementation`, `review`, `fix`.
- **FR-002**: A phase label MUST be either a kind alone (`review`) or a kind and a positive
  occurrence number joined by a hyphen (`review-2`). No other form is canonical.
- **FR-003**: The library MUST provide a normalization that maps a written phase to its canonical
  label, accepting at least: case differences, surrounding and internal whitespace, `_` and `#`
  separators, a leading English ordinal (`1st review`, `2nd fix`), a trailing number
  (`review 2`), and the alias `bug fix`/`bugfix` for `fix`.
- **FR-004**: Normalization MUST NOT invent an occurrence number for a kind written without one,
  and MUST NOT drop one from a kind written with one.
- **FR-005**: Normalization of a value it does not recognize MUST return that value with its
  whitespace normalized rather than `None`, so no attribution is lost, and MUST be distinguishable
  by the caller from a recognized label.
- **FR-006**: Normalization MUST treat `None`, the empty string and a whitespace-only string
  alike, returning `None` — "no phase" is not a label.
- **FR-007**: An occurrence that is zero, negative or not a whole number MUST NOT produce a
  canonical label.

**The injection contract**

- **FR-008**: The library MUST name the four attribution variables — `MADO_ISSUE_KEY`,
  `MADO_PHASE`, `MADO_STREAM_ID`, `MADO_PRICING_MODE` — as data, so that a producer and the hook
  cannot drift apart on a spelling.
- **FR-009**: The library MUST provide a builder that returns the environment block to inject,
  given an issue key, a phase, a stream id and a pricing mode.
- **FR-010**: The builder MUST return a mapping of `str` to `str` containing **all four**
  variables on every call, using the empty string for a value the caller does not have, so that
  exporting the block clears a stale value from a previous iteration.
- **FR-011**: The builder MUST canonicalize the phase it is given (FR-003) so the orchestrator
  cannot export a non-canonical label by accident.
- **FR-012**: The builder MUST reject a value that is present but unusable — a blank issue key, a
  blank stream id, a phase whose occurrence is not positive, an unrecognized pricing mode — by
  raising, rather than silently exporting it. A producer is a program with a bug to fix; the
  consumer's tolerance (FR-005) is for values that already exist in the world.
- **FR-013**: The builder MUST accept the absence of a value (`None`) as distinct from a bad
  value, and export it as the empty string, which the hook already reads as "unknown".
- **FR-014**: The module carrying FR-001 to FR-013 MUST import nothing outside the standard
  library and MUST NOT import the hook, so the orchestrator can depend on the contract without
  depending on the capture path.

**The consumer side**

- **FR-015**: The hook MUST normalize `MADO_PHASE` through FR-003 before the value reaches the
  record, so a record written by an orchestrator that predates this contract still carries a
  canonical label.
- **FR-016**: The hook MUST note a diagnostic when the phase it read is not a recognized label,
  and MUST still emit the record carrying that phase.
- **FR-017**: This story MUST NOT change which record field carries which variable, nor the
  treatment of unset and blank established by TOKWEIR-7.

**Documentation**

- **FR-018**: The README MUST document the taxonomy — the kinds, the label grammar, the accepted
  input spellings — alongside the existing attribution table.
- **FR-019**: The README MUST state what the orchestrator side must do: export all four variables
  on **every** iteration, before the turn, into the environment Claude Code inherits.
- **FR-020**: The documentation MUST say plainly that the orchestrator-side change lives in the
  `mado` repository and is not delivered by this story.

### Key Entities

- **Phase kind**: one stage of the orchestrator's loop — implementation, review, or fix.
- **Phase occurrence**: which pass through a recurring kind this is, counting from 1. Absent when
  the orchestrator does not track it.
- **Phase label**: the canonical string written into a record's `queue` field, formed from a kind
  and an optional occurrence.
- **Attribution block**: the four environment variables the orchestrator exports per iteration,
  and the values they carry.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every documented spelling of a phase — at least the five in User Story 1 — maps to
  a single label, verified by test.
- **SC-002**: A report grouping a run's records by phase yields at most one row per phase
  occurrence of that run, with no row that is a spelling variant of another.
- **SC-003**: 100% of records produced with an injected block carry the issue key and the phase
  label that went into it, verified end to end from builder to record.
- **SC-004**: A phase outside the taxonomy loses no attribution: the record still carries the
  value, in 100% of cases.
- **SC-005**: The variable names appear exactly once as literals in the library; producer and
  consumer both resolve them from that one definition.
- **SC-006**: The contract module has zero non-stdlib imports, so the orchestrator can adopt it
  without inheriting a transport dependency.

## Assumptions

- **The orchestrator-side wiring is deferred to the `mado` repo** and is not implementable from
  this pod. Stated in Context; repeated here because it is the single largest scope judgement in
  this spec.
- **The lifecycle's phase kinds are the three `mado-phase` names.** That tool is MADO's own
  record of its loop's phases and is the best evidence available from inside a stream pod. If the
  ACP feature-state lifecycle names more, FR-005's preserve-and-note rule means records keep
  working while the set is extended — the taxonomy is closed by decision, not by assumption.
- **`queue` remains the field carrying the phase.** Unchanged from TOKWEIR-7, where it is
  recorded as a compromise pending a v2 contract with a real phase field. Defining the taxonomy
  now makes that eventual migration mechanical rather than archaeological.
- **A bare kind means the orchestrator did not say which occurrence**, not "the first". Inventing
  `-1` would make an unknown look like a fact.
- **The canonical separator is a hyphen** and the canonical case is lower. Any consistent choice
  works; this one matches the existing wire values in the contract (`api_metered` uses an
  underscore, but every MADO-side identifier in this project — branch names, stream ids — is
  hyphenated and lowercase).
- **Producers raise, consumers tolerate.** The asymmetry between FR-012 and FR-005 is deliberate:
  a bad value in a producer is a bug that should be found in the orchestrator's own tests, while
  the hook's whole design contract (TOKWEIR-7 FR-025) is that it cannot fail a session.
