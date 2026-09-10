# Feature Specification: Verify subscription vs API-key transcript token parity (spike)

**Feature Branch**: `TOKWEIR-9-subscription-api-token-parity`
**Created**: 2026-09-10
**Status**: Draft
**Jira**: TOKWEIR-9 (Story) — "Verify subscription vs API-key transcript token parity (spike)"
**Input**: The Jira story, read with `mado-jira read TOKWEIR-9` on 2026-09-10 against the
`default` account (`https://bostoncio-cto.atlassian.net`), quoted verbatim below rather than
reconstructed from the branch name.

> Resolve the ADR-0001 Unverified item: Claude Code docs don't explicitly confirm that
> Max-subscription transcripts report the same token counts as API-key sessions. Diff a
> Max-session transcript against an API-key session on the same model and context; confirm
> message.usage.* parity (or characterize the difference) before trusting subscription numbers.
>
> **Acceptance:** documented finding on parity; if it differs, a correction factor or handling
> note is captured for the Stop-hook emitter.

Parent epic **TOKWEIR-2** (read the same way, same account, elisions marked `[…]`):

> Goal: Capture granular per-iteration usage when Claude Code runs on a Claude Max subscription,
> where API interception isn't possible […] the hook reads the transcript message.usage.* delta
> per turn and calls the tokenweir emitter with pricing_mode=subscription.
>
> **Acceptance:** a Max-authenticated stream emits one usage record per iteration, tagged with
> issue + phase, landing in the tokenweir store.

## Context

This is **ADR-0001 item 6**, the last open item of Pillar 4 and the only one the ADR marks
`Unverified`:

> `Unverified:` docs don't explicitly confirm token-count parity between subscription and API-key
> transcripts — the single check is to diff a Max-session transcript against an API-key session on
> the same model/context. Do this before relying on subscription numbers.

TOKWEIR-7 shipped the capture path: `tokenweir.claude_code` reads a transcript's `message.usage`
and emits one record per turn under `pricing_mode=subscription`. The ADR is explicit that this did
**not** close item 6:

> the hook faithfully reports what a Max transcript says, which is a different claim from those
> numbers agreeing with an API-key session's on the same model and context.

That distinction is the whole story. The hook is a faithful *reader*; nothing has yet established
that what it reads is denominated in the same units as the numbers an invoice is computed from.
Every downstream consumer — the report-time pricing view of TOKWEIR-5, any cost or capacity figure
derived from a `subscription` row — silently assumes it is.

### What the risk actually is

`pricing_mode=subscription` rows are stored raw and priced at report time. If a Max transcript's
counts were scaled, rounded, bucketed, or synthesized rather than passed through, then:

| If Max counts are… | Consequence |
|---|---|
| the same units as API `usage` | Nothing to do; the assumption is sound and now evidenced. |
| systematically offset or scaled | A **correction factor** belongs with the emitter, applied or recorded. |
| approximate or partly absent | A **handling note** must say which fields are trustworthy and which are not. |

The story asks for whichever of those three is true, backed by measurement. It does not presume
parity, and neither does this spec.

### Scope: this is a spike

The deliverable is a **finding**, not a feature. Code exists here only insofar as it is needed to
*produce* the finding and to let someone reproduce it later — which matters more than usual,
because ADR-0001 twice notes that the Max landscape is volatile ("Max tiers and weekly caps are
volatile"). A finding that cannot be re-run is a finding with an expiry date nobody can see.

So the code deliverable is a small, tested comparison harness; the documentation deliverable is the
executed finding; and the emitter deliverable is whatever the measurement warrants (FR-030+).

### The measurement authorization, recorded

`/mado-implement`'s first guardrail forbids a human-invoked run from reaching for an API token.
That guardrail is about a run using metered budget to do *its own work* faster or cheaper. Here the
API-key request **is the measurement the story exists to produce** — it is the subject under test,
not a shortcut — and the loop that produced this spec (implementation, reviews, fixes) ran on the
interactive Max session throughout.

The developer authorized the API-key measurement and its metered spend explicitly, in session, on
2026-09-10, after being told no credential was present in the pod. That authorization is recorded
here because a later reader must be able to tell a sanctioned measurement from a guardrail breach.

**Credential handling** is therefore a first-class requirement, not an implementation detail: see
FR-020..FR-024.

## The experimental design

"The same model and context" is the story's phrase and it hides a real difficulty, which this spec
resolves explicitly rather than papering over.

### Why a naive two-session diff cannot work

A Claude Code Max turn and a bare API call are **not** the same request and cannot be made so. A
Claude Code request carries a large system prompt, a full tool schema set, and conversation history
under cache control; a controlled API call carries none of that unless it is reconstructed exactly,
and Claude Code's system prompt and tool schemas are not published artifacts this repo may assume.
Two runs whose inputs differ produce different token counts *correctly*, so a raw numeric
difference between them proves nothing either way.

Concretely, from real Max transcripts in this pod, every turn reports `input_tokens: 2` with the
context in `cache_read_input_tokens` / `cache_creation_input_tokens`. Comparing that `2` against a
bare API call's `input_tokens` would "find a discrepancy" that is nothing but cache attribution.

### The design that does work: tokenizer ground truth

The question is not whether two different requests produce equal numbers. It is whether the Max
transcript's numbers **are token counts on the provider's own scale** — the same scale the API
reports and bills on.

That is testable against an authority both sides share: the provider's tokenizer, reachable with an
API key via `POST /v1/messages/count_tokens`, plus a control `POST /v1/messages` call whose usage is
by definition the API's own reporting.

Three probes to begin with, each answering one part of the question — a fourth, Probe A′,
was added during the review loop and is described after them:

**Probe A — Output-side parity (the direct test).**
A Max transcript records both the assistant's output text and the `output_tokens` it attributes to
producing that text. Send the *same recorded text* to `count_tokens` and compare. The two are
measuring the same artifact, so they are directly comparable with no context to confound them. If
the Max transcript's `output_tokens` matches the tokenizer's count for its own recorded output,
the transcript's counts are real token counts on the provider's scale. A constant ratio instead of
agreement is a correction factor; noise is an approximation.

This probe is the heart of the finding: it is a genuine same-artifact comparison, not an
apples-to-oranges session diff.

**Probe B — Field shape and semantics.**
Issue one controlled API-key `messages` call and inventory its `usage` object. Compare, field by
field, against the `usage` objects a Max transcript carries: which of the four billing fields are
present, under what names, and what extra fields each side adds. This establishes that the hook's
four fields (`_COUNT_FIELDS` in `tokenweir/claude_code.py`) mean the same thing on both sides, and
surfaces any Max-only field that changes their interpretation.

**Probe C — Internal consistency of the Max record.**
Counts that are passed through satisfy arithmetic identities; counts that are synthesized or
bucketed generally do not. Check, across every turn of a real transcript: that `cache_creation`
sub-fields sum to `cache_creation_input_tokens`; that a turn's `iterations[]` entries sum to its
top-level billing fields; and that cumulative cache reads advance monotonically with context
growth. A violation is evidence against pass-through even if Probe A were to look clean.

**Probe A′ — the API-side control.** Added during the review loop, and recorded here so this
spec describes what was built. Probe A establishes that a transcript's `output_tokens` is a real
token count differing from the tokenizer's measurement of the same text by a constant. It cannot,
alone, establish that the API would report *the same* constant for the same response — and
without that, a constant is only internal consistency, not parity. So the identical arithmetic is
run against responses the API generates itself and the two constants are compared. This is what
makes FR-030's single explicit verdict reachable.

It is the harness's only **expensive** probe (four generations), so it is gated behind an opt-in
`--control` flag, default off: a routine re-run is a drift check, which Probes A and C answer for
the price of `count_tokens` calls plus Probe B's one 16-token generation. Without the flag the
report states what the transcript side established and explicitly declines the parity verdict.

**The envelope derivation.** `count_tokens` prices a whole request, so it returns
`tokens(T) + E`. `E` is derived rather than assumed — by doubling a text, since
`E = 2·count_tokens(T) − count_tokens(T+T)` needs no knowledge of how many tokens `T` is — and
the assumption it does carry (that tokenization is additive across the join) is named in the
finding, along with why the verdict does not depend on it.

Together these answer the story's question without pretending to an identity that the two request
shapes cannot have. **Probe A is decisive on the transcript side, A′ closes it to a parity claim,
and B and C corroborate and bound both.**

### What this design cannot establish, stated plainly

It establishes that the numbers are true token counts on the provider's scale. It does **not**
establish what a Max subscription is *billed* — there is no per-call dollar under a flat rate, which
is Pillar 1's premise and precisely why raw tokens are stored and dollars derived at report time.
It is a **point-in-time** result on the models and Claude Code version measured, and the finding
must say so rather than reading as permanent.

## Requirements

### The comparison harness — pure logic

- **FR-001**: The library MUST provide a way to extract, from a Claude Code transcript, each
  assistant turn's `message.usage` object together with the assistant output text of that same
  turn, keyed by `message.id` so one API response is represented once.
- **FR-002**: Extraction MUST reuse `tokenweir.claude_code`'s existing de-duplication semantics
  rather than re-deriving them: usage belongs to one API response, and a transcript repeats the
  same `message.usage` across lines.
- **FR-003**: The harness MUST provide a pure comparison that, given two usage mappings, reports
  per field: both values, their absolute difference, and their ratio where the reference is
  non-zero.
- **FR-004**: The comparison MUST classify each field's result as `parity` (exact agreement),
  `offset` (a difference), or `absent` (the field is missing on one side), and MUST NOT report
  `parity` for a field absent on either side.
- **FR-005**: The comparison MUST be total over the four billing fields
  (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens`) and
  MUST additionally report any field present on one side only, so a Max-only field is surfaced
  rather than silently dropped.
- **FR-006**: The harness MUST implement Probe C's consistency checks as pure predicates over a
  transcript's parsed usage objects, each independently reportable, so a single failing identity is
  named rather than collapsing into one boolean.
- **FR-007**: A consistency check whose inputs are absent MUST report `not_applicable`, distinctly
  from `pass` and `fail` — a check that could not run is not a check that passed.
- **FR-008**: All pure logic MUST be importable and testable with no network access and no
  credential present.

### The network probe

- **FR-010**: The harness MUST issue its API requests using the standard library only. It MUST NOT
  add a third-party runtime dependency, and MUST NOT introduce a third-party import into
  `tokenweir`'s core — ADR-0001 Pillar 2, which `tokenweir.claude_code` already holds.
- **FR-011**: The probe MUST support `POST /v1/messages/count_tokens` (Probe A) and
  `POST /v1/messages` (Probe B) against the Anthropic API, sending the `anthropic-version` header.
- **FR-012**: The model used for a probe MUST be explicit in the caller's request and recorded in
  the result. The harness MUST NOT default to a model silently, because "the same model" is half
  the story's comparison condition.
- **FR-013**: Every network result MUST record the request's identifying conditions — model, and
  the request kind — alongside the returned usage, so a stored result is self-describing.
- **FR-014**: A network failure MUST surface as a clear error naming what failed. The harness MUST
  NOT fall back to a fabricated or cached number, and MUST NOT retry in an unbounded loop.
- **FR-015**: The probe MUST be skipped, not failed, when no credential is configured, so the
  test suite of a checkout with no credential stays green.

### Credential handling

- **FR-020**: The credential MUST be read from the environment (`ANTHROPIC_API_KEY`) or from a
  file path given by the operator, and MUST NOT be a positional command-line argument — an argument
  is visible in `ps` and in shell history.
- **FR-021**: The credential MUST NOT be written to stdout, stderr, a log line, an emitted record,
  a saved result file, or a committed artifact, on any code path including error paths.
- **FR-022**: A credential read from a file MUST be stripped of trailing whitespace and a trailing
  newline, since a file written by `printf`/`echo` differs by exactly that and a header carrying a
  newline fails opaquely.
- **FR-023**: An absent or blank credential MUST be reported as "not configured" — the same state,
  with the same message — rather than one being an error and the other a silent empty header.
- **FR-024**: No committed file in this repository may contain the credential. A repository-hygiene
  test MUST assert this, alongside the existing `tests/test_repo_hygiene.py` checks.

### The recorded finding

- **FR-030**: The run MUST produce a committed finding document that states, for each probe, the
  conditions measured and the result observed, and reaches one explicit verdict: parity, a
  correction factor, or a characterized difference.
- **FR-031**: The finding MUST record its measurement conditions — date, model(s), Claude Code
  version, and the transcript measured — and MUST state that it is a point-in-time result, given
  ADR-0001's own note that the Max landscape is volatile.
- **FR-032**: The finding MUST distinguish what was measured from what was inferred, and MUST state
  the limits named above: it speaks to token-count scale, not to subscription billing.
- **FR-033**: ADR-0001's open item 6 and its `Unverified` note MUST be updated to reflect the
  outcome, and MUST NOT be marked resolved beyond what the measurement supports.
- **FR-034**: If the measurement shows a difference, a correction factor or handling note MUST be
  captured with the Stop-hook emitter — in `tokenweir.claude_code`, where a future reader of the
  capture path will encounter it — not only in a document elsewhere.
- **FR-035**: If the measurement shows parity, the emitter's documentation MUST record that parity
  was verified, when, and under what conditions, so the next reader is not left re-litigating a
  settled question or over-trusting a stale one.
- **FR-036**: The finding MUST NOT assert that parity was verified for any field or condition the
  run did not actually measure.

### Reproducibility

- **FR-040**: The harness MUST be runnable as a documented command that performs the probes and
  prints a report, so the finding can be re-established after a Claude Code or Max-tier change.
- **FR-041**: The command MUST make clear, in its output, which probes ran and which were skipped
  for want of a credential — a partial run must not read as a complete one.

## Acceptance criteria

The story's acceptance is *"documented finding on parity; if it differs, a correction factor or
handling note is captured for the Stop-hook emitter."* Concretely:

1. A committed finding document reaches an explicit verdict from executed measurements, with its
   conditions and limits recorded (FR-030..FR-032, FR-036).
2. Probe A has been executed against a real Max transcript and the real tokenizer, and its numbers
   appear in the finding.
3. ADR-0001 item 6 reflects the outcome, neither overclaiming nor left silently open (FR-033).
4. The emitter carries the correction factor or the handling note the outcome warrants
   (FR-034/FR-035).
5. The harness's pure logic is covered by tests that pass with no credential and no network
   (FR-008, FR-015).
6. No credential appears in any committed file, asserted by a test (FR-024).

## Out of scope

- **Capacity or %-of-limit modeling.** ADR-0001 defers it explicitly; volatile Max tiers are why.
- **Changing how records are priced.** Dollars are derived at report time (TOKWEIR-5); this spike
  informs that view, it does not rewrite it.
- **Subagent usage capture.** `tokenweir/claude_code.py` documents that a subagent's usage is not
  currently captured; that is a separate gap, not this one.
- **Any change to the emit path.** The hook's reading and emitting behaviour is TOKWEIR-7's and
  stays as it is unless FR-034 requires a note beside it.
