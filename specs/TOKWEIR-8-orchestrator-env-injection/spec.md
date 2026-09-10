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
   **Then** the record still carries the value — separators folded, words untouched — rather than
   losing it, and a diagnostic notes that the label is not in the taxonomy, naming the value as
   the orchestrator exported it.

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
  this library hears about it). The value is preserved and noted — never dropped and never coerced
  into a kind it is not. Losing the attribution would be a worse outcome than carrying an
  unrecognized one.

  "Preserved" is bounded, and the bound is part of the requirement rather than an accident of the
  implementation: separators are folded in the preserved value too, so `deploy_step` is kept as
  `deploy step`. Folding in one direction is what stops an *unknown* phase splitting into as many
  lanes as it has spellings — the same reason the taxonomy exists at all. The diagnostic names the
  value **as read**, not as exported — `_env` has already trimmed surrounding whitespace before the
  hook ever sees it, so a phase exported with leading or trailing spaces is reported without them.
  (Corrected here: TOKWEIR-58, filed by the terminal review, found this bullet still claiming "as
  exported" — see `claude_code.py`'s `attribution_from_env`, whose diagnostic comment already said
  "as read" and explained why.) An operator can still find the value in their own configuration;
  only the ends, which folding does not touch anyway, are not reproduced verbatim.
- **An occurrence of zero.** Not a real occurrence; the label is rejected at the producer
  (`review-0`, `review 0`, `fix-0`, `0th fix`) and left as written at the consumer.
- **A negative number where an occurrence would go** (`review -1`). Not an occurrence and not a
  shape this taxonomy reads at all, so it is an unrecognized phase at both ends: preserved by the
  consumer, passed through by the producer. Distinguishing it from `review - 3` — occurrence 3,
  written with spaces — is not something a separator rule can do reliably, and the attempt is what
  review 4 found misclassifying the positive case.
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
  **separators folded** — the same folding a recognized value gets, so `deploy_step` and
  `deploy step` are one unknown phase rather than two — and never `None`, so no attribution is
  lost. The result MUST be distinguishable by the caller from a recognized label. (Reworded after
  review 3, Low-3: this said "with its whitespace normalized", which did not license the folding
  the Edge Cases describe and the code performs.)

  Two bounds on "never `None`", both deliberate. A value that folds to **nothing** is empty, not
  unrecognized, and FR-006 governs it — with FR-016's diagnostic covering the loss. And **case is
  not folded** in a preserved value: `Deploy` stays `Deploy`. Folding separators is repairing a
  spelling of the same word; folding case would be editing what an unknown phase is called, and
  the further this library goes rewriting vocabulary it does not define, the less "preserved"
  means. (Both stated after review 5, Low-1 and Med-2, which found them true of the code and
  absent from the requirement.)
- **FR-006**: Normalization MUST treat `None`, the empty string, a whitespace-only string and a
  string made only of folded separators (`_`, `#`) alike, returning `None` — "no phase" is not a
  label. The producer MUST reject all of the non-`None` cases by the *same* definition of empty
  (FR-012): two definitions of blank leave a gap the width of the difference between them, and a
  phase falls through it. (Extended after review 3, Med-2, which found exactly that gap.)
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
  blank stream id, a phase whose occurrence is not positive **in a shape the taxonomy reads**
  (`review-0`, `review 0`, `fix-0`, `0th fix`), an unrecognized pricing mode — by raising, rather
  than silently exporting it. A producer is a program with a bug to fix; the consumer's tolerance
  (FR-005) is for values that already exist in the world.

  The bound is load-bearing and was added after review 4. A *sign-bearing* spelling — `review -1`,
  `review - 3`, `review- 3` — is an unrecognized phase, not an occurrence, and passes through like
  any other. The rule it replaces tried to refuse those too and could not do it consistently: the
  same regex matched `review - 3`, which names occurrence **3**, so one string in three spacings
  had three outcomes and the middle one raised "occurrence must be 1 or greater" about a positive
  number.
- **FR-013**: The builder MUST accept the absence of a value (`None`) as distinct from a bad
  value, and export it as the empty string, which the hook already reads as "unknown".
- **FR-014**: The module carrying FR-001 to FR-013 MUST import nothing outside the standard
  library **except `tokenweir.contract`**, which is itself stdlib-only and holds the one enum both
  ends must agree on, and MUST NOT import the hook — so the orchestrator can depend on the
  contract without depending on the capture path or on any transport driver. (Revised after
  review 1, Low-2: the original wording forbade the import the design always intended to make,
  and the enforcing test had the exception written into it.)

**The consumer side**

- **FR-015**: The hook MUST normalize `MADO_PHASE` through FR-003 before the value reaches the
  record, so a record written by an orchestrator that predates this contract still carries a
  canonical label.
- **FR-016**: The hook MUST note a diagnostic when the phase it read is not a recognized label,
  and MUST still emit the record carrying that phase. It MUST also note one when a phase that had
  content folded away to nothing (`MADO_PHASE=_`), where there is no phase left to carry: before
  this story that value reached the record as `'_'`, and turning visible junk into an invisible
  absence is a regression unless something says so. The diagnostic MUST NOT fire for an unset or
  blank phase, which is the ordinary unattributed run. (Extended after review 5, Med-2.)
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
- **SC-002**: A report grouping a run's records by phase yields at most one row per *recognized*
  phase occurrence of that run — the five spellings of SC-001 do not become five rows. Unrecognized
  phases are exempt by construction: FR-005 requires preserving them, so two spellings of a phase
  this taxonomy does not know remain two rows until the taxonomy learns it. (Bounded after review
  4, Low-2, which observed that the original wording promised something FR-005 forbids.)
- **SC-003**: 100% of records produced with an injected block carry the issue key and the phase
  label that went into it, verified end to end from builder to record.
- **SC-004**: A phase outside the taxonomy loses no attribution: the record still carries the
  value, in 100% of cases.
- **SC-005**: The variable names appear exactly once as a **code literal** in the library;
  producer and consumer both resolve them from that one definition. Prose is exempt and
  deliberately so — a docstring cannot drift into reading the wrong variable, and only code
  references can. (Reworded after review 3, Low-6, to say what the enforcing test checks.)
- **SC-006**: The contract module imports nothing beyond the standard library and
  `tokenweir.contract`, so the orchestrator can adopt it without inheriting a transport dependency
  — verified by reading the module's own imports rather than `sys.modules`.

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

## Revision log

### Review 1 (2026-09-08) — the producer half of FR-012 was missing

The reviewer found that `attribution_env` exported a phase naming a real kind with an impossible
occurrence (`review-0`, `review 0`, `0th fix`) instead of raising, while the README promised the
opposite to whoever wires the `mado` side. FR-012 and the Edge Cases had always said "rejected at
the producer and left as written at the consumer"; only the consumer half existed.

The fix was to enforce it rather than to amend the requirement, because the way to produce such a
label in the wild is an off-by-one round counter — a live producer bug exporting exactly the split
report lane this taxonomy exists to prevent. It needed the parser to distinguish three outcomes
rather than two, so `_parse_phase` now reports *kind with a bad occurrence* as its own answer, and
the two ends read the same parse: the producer raises on it, the consumer keeps it.

Two related defects came out of the same restructuring:

- The trailing-number separator class `[\s-]+` swallowed a minus sign, so `review -3` normalized
  to `review-3` — a canonical label for a phase that never happened, which `is_canonical_phase`
  then vouched for, so nothing warned. The separator is now one hyphen or one run of spaces, and
  the kind is matched without stripping what is left.
- `attribution_env(phase=7)` raised `AttributeError` from inside `_collapse` rather than the
  documented `ValueError`.

### Review 1 — SC-005 was asserted rather than held

The four variable names were literals in two places: `ATTRIBUTION_ENV` and, by hand, the hook —
while the module's own comment claimed "both ends of the contract resolve a name from here". The
hook now reads through `ATTRIBUTION_ENV`, and a test asserts it spells none of the names itself.
The claim is checked rather than repeated.

### Review 1 — the deferred half, restated

The reviewer confirmed independently that `/workspace` holds only `token-weir`, that the deferral
of the orchestrator-side export to the `mado` repo is legitimate and documented, and that the Jira
acceptance clause ("emitted records carry the correct issue key and phase **for the iteration**")
is therefore not satisfiable on this branch alone. Nothing about that changed in this round; it is
recorded here so the run's report and this spec say the same thing.

The reviewer could not check whether a follow-up existed (`mado-jira` reads by key, and the
reviewer cannot write to Jira by design), so this round checked and filed it: **MADO-419**,
*"Orchestrator: export the MADO_* attribution block into stream pods per iteration"*, linked to
TOKWEIR-8 with a `Relates` link. It is not a duplicate of MADO-235, which covers the
`X-App-ID` / `X-Workload` **header** path for API-metered capture at the gateway — a different
mechanism for the same attribution. TOKWEIR-8 should not be closed as delivering end-to-end
attribution while MADO-419 is open.

### Review 2 (2026-09-08) — the same rule, the spelling it did not cover

Review 1's fix for `review -3` made the trailing-number parser refuse a sign, which was right
for the consumer and left a hole at the producer: `review -1` and `fix -0` now matched no shape
at all, so they never reached the `bad_occurrence` branch and were exported — while FR-012, the
Edge Cases and the README all said a non-positive occurrence on a known kind raises. The two
fixes had been made one round apart and had not been read against each other.

The reviewer offered both resolutions: narrow the promise, or widen the guard. The guard was
widened, because the two spellings come from the same producer bug — `f"{kind} {n}"` with a
counter running backwards — and a rule that catches `n == 0` but not `n == -1` is not a rule
anyone can hold in their head. It is still **one** guard: `_parse_phase` reports the case, the
producer raises on it and the consumer keeps the string, exactly as for `review-0`.

The bound is now tested from the other side too: a sign only makes a phase a producer bug when
the kind is one the taxonomy knows. `deploy -1` passes through, because the lifecycle is MADO's
to extend and this library does not legislate for it.

Also from review 2: `ATTRIBUTION_ENV` is a `MappingProxyType` (a mutable dict re-exported at
package level is one assignment from redirecting both ends at once — SC-005's drift arriving
through the mechanism meant to prevent it); the whitespace strip on the issue key and stream id
is documented and tested rather than merely happening; `tasks.md` is ticked, per the convention
every other spec in this repo follows; and the module says plainly that its stdlib-only guarantee
is about *this module*, since importing it still initializes the package.

The reviewer's second Med is a status finding rather than a defect, and this round discharged it
where it will actually be read: a comment on TOKWEIR-8 itself, naming what the branch delivers,
what it does not, and MADO-419 as the other half. It is not enough for the deferral to be true
and documented in a repo — the person closing the issue is looking at Jira.

### Review 3 (2026-09-08) — one phase, two definitions of empty

`_collapse` folds `_` and `#` to nothing, but the producer's blank check used `str.strip()`,
which does not. The gap between the two definitions was exactly the separator characters:
`MADO_PHASE=_` passed the producer's check, normalized to `None`, and was exported as `''` — so
a phase disappeared at *both* ends, and because the hook only warns about a phase it can see, it
disappeared silently. That is the one outcome this module says must never happen, and it was
reachable from a one-character typo.

The fix is one definition rather than a new guard: the producer now judges an empty phase with
`_collapse`, the same function the reader judges it by. A separator-only phase is "no phase" —
the same claim as `""` in different characters — so the producer refuses it and tells the caller
to pass `None`, and the reader treats it as unset without a diagnostic. Both ends are tested on
the same inputs.

Three amendments to this document, listed because a requirement edited to fit the code is worth
a reader knowing about:

- **FR-005** said "with its whitespace normalized", which never licensed the separator folding
  the Edge Cases describe and the code performs. Reworded to match. The round-1 entry above
  amended the Edge Case and did not list the amendment; this does.
- **FR-006** now names separator-only strings and, more importantly, requires both ends to use
  one definition of empty — the property whose absence was review 3's Med-2.
- **FR-014 and SC-006** were reworded in round 1 to permit the `tokenweir.contract` import that
  `plan.md` always intended; **SC-005** is reworded here to say "code literal", which is what its
  enforcing test checks and what can actually drift.

Review 3's remaining Med is the deferral, unchanged and reported for the third time: the story's
"for the iteration" clause needs MADO-419.

### Review 4 (2026-09-08) — the guard that cost more than it caught

Round 2 added a regex so the producer would refuse a negative occurrence. Round 4's reviewer found
what it bought: `- ?` also matched `review - 3`, which names occurrence **3**, so the producer
raised *"occurrence must be 1 or greater"* about a positive number — and `review-3`, `review - 3`
and `review- 3` had three different outcomes, the middle one the only loud failure. The boundary
was arbitrary, which is the one thing round 2 claimed it was buying.

It is cut rather than repaired. Teaching the regex to tell `- 3` from `-3` only moves the boundary
to `review- 3`, and each move adds a case nobody can predict from the rule. Now every sign-bearing
spelling is uniformly an unrecognized phase at both ends, and the occurrences FR-012 names —
`review-0`, `review 0`, `fix-0`, `0th fix` — still raise, through the parsers FR-003 requires
anyway. FR-012 and the Edge Cases are amended to say exactly that, since the previous wording is
what asked for the guard in the first place.

Worth recording plainly: **review 2 asked for this guard and review 4 asked for it to go.** Both
were right about the thing they were looking at — a negative occurrence really did slip through,
and the fix really did misread a positive one. What settles it is that the spec's own list of
rejected occurrences can be honoured without the guard, and cannot be honoured *consistently* with
it.

### Review 4 — two recommendations declined, with reasons

Review 4 also read the negative-occurrence guard, the version-skew paragraph and the SC-005 AST
test as scope the story never asked for. The guard is cut, above. The other two are kept, and the
disagreement is recorded here rather than resolved by churn:

- **The version-skew paragraph** exists because review 3 asked for it (Low-5), against US4's own
  statement of purpose: the `mado` engineer should learn "what happens if they get it wrong". Two
  reviewers, opposite readings; the paragraph is three sentences and answers a question the
  deferred half will actually raise. It is trimmed rather than removed, and the same note is on
  MADO-419, where the producer engineer will meet it.
- **The SC-005 AST test** exists because review 2 found SC-005 claimed and not held. Review 4
  argues the round-trip test already catches divergence, and it does catch a *renamed* variable —
  but not a re-introduced hard-coded literal, which is the shape SC-005 names and the shape the
  hook was in before round 1. The test is 20 lines and there is repo precedent for structural
  assertions (`test_contract.py`, `test_optional_drivers.py`). Kept.

Review 4's concrete complaint about those tests is fixed rather than argued with: they read the
source tree, so like `_readme()` they now **skip** where there is none instead of raising
`FileNotFoundError` in a tests-without-source install.

### Review 5 (2026-09-08) — the check that was enforcing half a rule, and the drop nobody was told about

**The import-isolation test was blind to relative imports.** `node.level == 0` skipped every
`from .x import y`, so SC-006 — the criterion the spec calls "verified by reading the module's own
imports" — was enforced for one import style out of two. The reviewer got
`from .postgres import *` past a green suite. A relative import now resolves to its
`tokenweir.` name and goes through the same assertion; the same mutation fails now.

**A separator-only phase was being dropped silently.** Round 3 made `_` and `#` fold to nothing at
both ends, which is right and is what FR-006 says. What it did not notice is that the *reader* then
had a third way to end up with no phase, and unlike the other two it followed a value that had
content: on `main`, `MADO_PHASE=_` reached the record as `'_'`. Round 3 turned visible junk into an
invisible absence, on the only live path, with nothing logged — and the likeliest way to produce
`_` is a template that expanded to nothing, which is the fault an operator most needs to hear
about and least likely to spot in an empty column.

Fixed both ways the reviewer offered, because they answer different questions: the hook now notes
the fold (FR-016 extended to require it, and to require silence on the ordinary unattributed run,
which is tested), and the README says plainly that `_`/`#` count as blank at both ends. The
producer's refusal was already there; what was missing was the reader admitting to it.

This is the round's lesson worth keeping: a change that makes two ends agree can still be a
regression at one of them. Round 3 checked that the producer and the consumer now shared a
definition, and did not check what the consumer had done *before*.

Low findings closed with it: `pricing_mode` joins the "`None` is unknown, not an error" test table
(FR-013's third field had no test); the README no longer claims the diagnostic names the value "as
you exported it", since `_env` trims the ends first; and FR-005 now states that case is
deliberately not folded in a preserved value.

Review 5's remaining Med is the deferral, reported for the fifth time and confirmed adequate by
the reviewer's own independent check of every disclosure. The only action left on it belongs to
whoever closes TOKWEIR-8: close it on the contract, not on end-to-end attribution.
