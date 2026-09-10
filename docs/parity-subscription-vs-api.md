# Subscription vs API-key token parity — the finding (TOKWEIR-9)

**Verdict: parity. No correction factor is needed.**

A Claude Max transcript's `message.usage` counts are true token counts on the same
scale the Anthropic API reports — its `output_tokens` **by direct measurement**, its
input-side fields **by corroboration only**. That distinction is load-bearing and is
spelled out under "What this does and does not establish"; it is stated here so the
verdict cannot be quoted without it.

For `output_tokens`, the relationship between a transcript's count and the provider's
own tokenizer is **exact, affine, and slope-1**, and the *same* relationship holds for
the API's own `output_tokens` on a response it generates itself.

This closes ADR-0001 item 6 and the `Unverified:` note in Pillar 4, within the limits
stated at the bottom — which are real and should be read before this is treated as
permanent.

## Measurement conditions

| | |
|---|---|
| Date | 2026-09-10 |
| Model | `claude-opus-5`, both sides |
| Claude Code | 2.1.263 |
| Auth measured | Claude Max subscription (OAuth) vs. API key (`x-api-key`) |
| API version | `2023-06-01` |
| Transcripts | the 2 **completed** sessions in one stream pod, 283 de-duplicated assistant responses (144 + 139) |
| Headline sample | 12 responses (7 + 5), 181–1206 output tokens (a 6.7× span) |
| Also checked | the in-flight session that produced this finding — reported separately below, since its counts grow while it runs and are not reproducible |
| Harness | `python -m tokenweir.parity` (`src/tokenweir/parity.py`) |

The API-key measurement was explicitly authorized by the developer, in session, as the
subject under test. `/mado-implement`'s guardrail against a human-invoked run reaching
for an API token concerns a run spending metered budget on *its own work*; here the
API call **is** the measurement. The loop itself ran on the interactive Max session
throughout.

## Why the story's literal method cannot work, and what replaces it

The story says to "diff a Max-session transcript against an API-key session on the same
model and context". Taken literally that is not executable, and it is worth saying why
rather than quietly substituting something else.

A Claude Code turn and a bare API call are **not the same request and cannot be made
so**. Claude Code sends a large system prompt, a full tool-schema set, and cached
conversation history whose exact bytes are not a published artifact this repo may
assume. Two requests with different inputs produce different token counts *correctly*,
so a raw difference between them is uninterpretable — it measures the prompt, not the
reporting.

The real transcripts make this concrete: **every** Max turn reports `input_tokens: 2`,
with essentially all context in `cache_read_input_tokens`. Comparing that `2` against a
bare API call's `input_tokens` would "find" a vast discrepancy that is nothing but cache
attribution.

So the comparison is made against an authority both sides share — **the provider's own
tokenizer** (`POST /v1/messages/count_tokens`). The question becomes answerable and
sharp: *are the transcript's numbers real token counts on the provider's scale, or are
they scaled, rounded, bucketed or synthesized?*

## Probe A — output-side parity (decisive)

A transcript records both the assistant's output text and the `output_tokens` it
attributes to producing it. Both describe the same artifact, so they are directly
comparable with no prompt to confound them.

First, the tokenizer's request envelope was measured rather than assumed. For a text `T`,
`count_tokens` counts a whole message request, so `count_tokens(T) = tokens(T) + E`.
Doubling the text gives `count_tokens(T+T) = 2·tokens(T) + E`, hence `E = 2·c₁ − c₂` with
no assumption about how many tokens `T` is. Across four independent texts:

```
len=1303  c1=549  c2=1092  =>  E = 6
len=1359  c1=512  c2=1018  =>  E = 6
len=1744  c1=668  c2=1330  =>  E = 6
len=1899  c1=679  c2=1352  =>  E = 6
```

**Provenance**, since it differs from what a re-run prints: these four came from an
exploratory script written while establishing the method, which derived `E` from four
separate turns to check the constant held. The committed harness derives it **once**,
from the longest eligible turn, because one derivation is what the measurement needs
and each costs two `count_tokens` calls. A re-run therefore prints a single
`count_tokens envelope E = 6` line, not four. Running the committed command against
c301b927 reproduces `E = 6` from its 3248-char turn.

**E = 6**, invariant across all four.

One assumption is buried in that derivation and is worth naming rather than leaving
implicit: it takes `count_tokens(T+T) = 2·tokens(T) + E`, i.e. that tokenization is
**additive across the join**. That holds when `T` ends on a clean token boundary and can
be off by a token or two otherwise, which is why it is derived from long texts and why
two derivations disagreeing would itself be the signal.

**The verdict does not rest on it.** `count_tokens − transcript = 4` exactly on all 12
rows across a 6.7× span, and a constant difference over that range rules out a scale
factor without reference to `E` at all. `E` only converts that 4 into the more
interpretable "+2 against the bare text count"; if the additivity assumption were off by
a token, the `+2` would shift and the parity conclusion would not.

With `E` subtracted, all 12 headline responses:

| chars | transcript `output_tokens` | `count_tokens` | bare text (= ct − 6) | transcript − bare |
|---:|---:|---:|---:|---:|
| 454 | 181 | 185 | 179 | **+2** |
| 486 | 192 | 196 | 190 | **+2** |
| 547 | 194 | 198 | 192 | **+2** |
| 727 | 245 | 249 | 243 | **+2** |
| 730 | 242 | 246 | 240 | **+2** |
| 1303 | 545 | 549 | 543 | **+2** |
| 1359 | 508 | 512 | 506 | **+2** |
| 1734 | 623 | 627 | 621 | **+2** |
| 1744 | 664 | 668 | 662 | **+2** |
| 1801 | 614 | 618 | 612 | **+2** |
| 1899 | 675 | 679 | 673 | **+2** |
| 3248 | 1206 | 1210 | 1204 | **+2** |

```
transcript.output_tokens = tokens(text) + 2        (n = 12, zero variance)
```

**Reproducing this table takes two invocations, not one.** Its 12 rows pool the two
completed sessions — 7 from `c301b927`, 5 from `f603b29b` — and the command takes a
single `--transcript`. Each run also derives its own `E` from its own longest eligible
turn, so the two runs establish the constant independently rather than sharing one
derivation.

**Why this rules out a scale factor.** The *difference* is constant at exactly 2 while
the *ratio* drifts across the range — 0.978 → 0.997 measured as
`transcript ÷ count_tokens`, or 1.0112 → 1.0017 as `transcript ÷ bare text`, which is
the one the harness prints. Either way it moves; the difference does not. A scaled count behaves the opposite
way: the ratio would be constant and the difference would grow with size. A constant
difference with slope 1 is a framing constant, not a correction factor — and it does not
scale with anything, so there is no factor to apply.

## Probe A′ — the API-side control (what makes it parity rather than plausibility)

Probe A alone shows the transcript reports real tokens. It does not by itself show the
API would report the *same* number for the same response. So the identical arithmetic was
run against the API's own generations: prompt it, take the `output_tokens` it reports, and
independently tokenize the text it returned.

The first attempt appeared to disagree by +27 and +21 — because the responses contained
`thinking` blocks whose tokens count toward `output_tokens`. Accounting for
`output_tokens_details.thinking_tokens`:

As printed by the committed harness (`python -m tokenweir.parity … --control`; without
that flag this block reads `SKIPPED`), so these numbers are reproducible rather than
transcribed from a scratch script:

```
api 128, bare text 101, thinking 25, api-(text+thinking) +2
api  39, bare text  18, thinking 19, api-(text+thinking) +2
api  27, bare text   7, thinking 18, api-(text+thinking) +2
api   4, bare text   2, thinking  0, api-(text+thinking) +2

Probe A' result: api = bare_text + thinking +2, CONSTANT across 4 call(s).
==> PARITY. One rule, one constant (+2), both auth modes. No correction factor.
```

```
api.output_tokens = tokens(text) + thinking_tokens + 2        (4 of 4 exact)
```

A generation is not deterministic, and that is useful here rather than a nuisance. The
control was run three times over the course of this story, producing three different sets
of responses — `135 / 43 / 27 / 4`, `128 / 39 / 27 / 4` and `138 / 39 / 28 / 4` output
tokens — and every one yielded **the same `+2`**, including across differing thinking
counts (25, 23, 19, 18, 0). The constant is a property of the reporting, not of the
sample.

The headline transcript sample all report `thinking_tokens: 0`, so `tokens(text) + 2` is
that same rule with its thinking term zero. **One rule, one constant, both auth modes.**
That is parity in the sense the ADR asked for.

## Probe B — field shape

One controlled API call, inventoried against a transcript's usage object:

```
API:        cache_creation, cache_creation_input_tokens, cache_read_input_tokens,
            inference_geo, input_tokens, output_tokens, output_tokens_details,
            service_tier
transcript: cache_creation, cache_creation_input_tokens, cache_read_input_tokens,
            inference_geo, input_tokens, iterations, output_tokens,
            output_tokens_details, server_tool_use, service_tier, speed
```

All four billing fields the hook reads — `input_tokens`, `output_tokens`,
`cache_creation_input_tokens`, `cache_read_input_tokens` — are present on both sides
under identical names. The transcript is a **superset**: it adds `iterations` (the
per-API-call breakdown of a turn), `server_tool_use`, and `speed`. Nothing the hook
depends on is missing, renamed, or restructured.

Two observations worth recording:

- **`service_tier` reads `standard` on the Max subscription**, not a subscription-specific
  value. Nothing in the usage object marks it as subscription traffic, which is exactly
  why `pricing_mode` must be supplied by the emitter (as TOKWEIR-7 does) and cannot be
  inferred from the transcript.
- The per-field numeric values differ wildly between the two (`input_tokens` 16 vs 2,
  `cache_read_input_tokens` 0 vs 381,135). That is the confounded comparison described
  above, reported here only to show what it looks like — it is **not** evidence of
  anything, and must not be read as such.

## Probe C — internal consistency (offline, no credential)

Arithmetic identities that a passed-through count satisfies and a synthesized or bucketed
one generally does not. Across the 2 completed sessions:

| Session | Responses | `cache_creation` sums | `iterations[]` sums | cache reads non-decreasing |
|---|---:|---|---|---|
| c301b927 | 144 | 144/144 PASS | 144/144 PASS | 143/143 PASS |
| f603b29b | 139 | 139/139 PASS | 139/139 PASS | 138/138 PASS |
| **283** | | **PASS** | **PASS** | **PASS** |

The in-flight session that produced this finding was checked too, and is reported
separately because it is **not a reproducible input**: it grows while it runs, so no
fixed count quoted here would survive the next turn. (An earlier draft of this
paragraph quoted one, and the numbers did not even reconcile with each other — 126
turns cannot yield 126 transitions.) What is stable is the shape of the result. Its
two arithmetic identities pass on every response; its monotonicity check **fails**,
on a small number of transitions where `cache_read_input_tokens` drops — for example
233,233 → 230,941 and 236,168 → 234,146.

That is the documented legitimate case, not a defect: `_cache_read_monotonic` says in
as many words that compaction, a context reset or a new cache prefix all shrink what is
read, which is exactly what happens to a long session. It is recorded here because a
reader who re-runs the harness on a long session **will** see that failure, and a
finding that showed three tidy PASSes would leave them thinking something had broken.
It is also why this check is corroboration and never a verdict on its own.

The `iterations[]` result is the most informative: Claude Code makes several API calls per
turn and records each one's usage, and those per-call figures sum **exactly** to the
turn's top-level billing fields on every single response. An aggregate that had been
rounded or re-derived would not.

## Two bugs this measurement found in its own harness

Recorded because both produced large, entirely fake parity failures, and both would have
been easy to publish as findings.

1. **Sibling transcript lines divide a response's content; they do not repeat it.** One
   line carries the `text` block, another the `tool_use` block, and *both* carry the whole
   response's aggregate `output_tokens`. De-duplicating on `message.id` — correct for
   usage — dropped the tool call, so 20 characters of text were compared against the
   token count for a 37 KB payload. This is what produced "transcript 14,142 vs tokenizer
   12" in the first run.
2. **An empty `thinking` block is unaccounted, not accounted-and-zero.** Transcripts carry
   `{"type": "thinking", "thinking": ""}` while still reporting non-zero
   `thinking_tokens`: content stripped, tokens real. Four responses claiming 71, 176, 366
   and 503 thinking tokens would have entered the headline as multi-hundred-token
   "discrepancies".

The general lesson, and the reason the finding is trustworthy: **a discrepancy is a claim
about the measurement before it is a claim about the thing measured.** Both bugs were
caught because the deltas were implausibly large and structured, not noisy.

## What this does and does not establish

**Established.** A Max transcript's `output_tokens` is a true token count on the
provider's scale, related to the underlying text by the same slope-1 rule with the same
`+2` constant as the API's own reporting; the four billing fields exist under identical
names on both sides; and a transcript's internal aggregation is exactly consistent on 283
responses.

**Not established, and not to be inferred:**

- **Nothing about subscription *billing*.** There is no per-call dollar under a flat rate —
  ADR-0001 Pillar 1's premise, and why raw tokens are stored and dollars derived at report
  time. This finding is about counts, not money, and the provider invoice remains
  authoritative.
- **The input side is not directly verified.** Probe A is an output-side test. `input_tokens`,
  `cache_read_input_tokens` and `cache_creation_input_tokens` are supported by Probes B and
  C (identical names; exactly consistent aggregation) but not by a same-artifact tokenizer
  comparison, because the Claude Code request's exact bytes are not reconstructable. The
  input side rests on corroboration, the output side on direct measurement.
- **Most turns are not covered by the headline**, and for two distinct reasons rather
  than one. 12 of 283 responses were eligible. A turn is excluded if it carries a
  `tool_use` block (a JSON payload this harness cannot re-tokenize faithfully) **or** if
  it accounts for thinking tokens it cannot reproduce — either a `thinking` block whose
  content Claude Code stripped, or a non-zero `output_tokens_details.thinking_tokens` with
  no block at all. Both causes are common and they overlap; the harness reports them
  together as "unmeasurable". The excluded turns are **not** suspected — they are
  unmeasurable by this method, which is a different thing.
- **`thinking_tokens` was verified on the API side only.** Max transcripts strip thinking
  content, so the transcript's own `thinking_tokens` could not be checked against the
  tokenizer.

**This is a point-in-time result.** ADR-0001 records that the Max landscape is volatile.
It holds for `claude-opus-5` on Claude Code 2.1.263 on 2026-09-10. Re-run the harness after
a Claude Code upgrade, a model change, or any change in subscription tier before relying
on it again:

```bash
python -m tokenweir.parity \
  --transcript ~/.claude/projects/<project>/<session>.jsonl \
  --model claude-opus-5 \
  --credential-file /path/to/api-key \
  --control        # add this to re-establish the verdict itself
```

`--sample` caps how many eligible turns go to the tokenizer, defaulting to 20 — above
the 12 that were eligible here, so it did not bind. A run it *does* cap says so, in the
same line that reports the turn accounting.

`--control` is off by default and is the harness's only *expensive* probe: four
generations of up to 300 `max_tokens`. A credentialed run without it is **not**
generation-free — Probe B spends one 16-token generation on its field inventory,
unconditionally — but everything else is `count_tokens`. Without `--control` the command
still measures the transcript side and reports whether its offset is constant, which is
the cheap drift check, but it declines to print the parity verdict, because that verdict
rests on the API-side comparison it did not make.

The harness computes and prints the verdict itself, so a re-run does not require this
document to interpret. It derives `E` on the spot, prints each turn's
`transcript − bare_text`, states whether that difference is **CONSTANT** or **VARIES**,
runs the same arithmetic against the API's own generations (Probe A′), and then prints one
of:

- `==> PARITY. One rule, one constant (+2), both auth modes. No correction factor.`
- `==> DIFFERENCE. Transcript constant X vs api constant Y: the gap is the correction to
  carry with the emitter.`
- `==> NO VERDICT: a constant was not established on both sides.`

Exit status carries the same information for a script: `0` measured, `1` nothing to
measure, `2` partial (no credential), `3` a probe was attempted and failed. A failed probe
never exits 0.

Probe C runs with no credential at all and is the cheap early warning: if its two
arithmetic identities ever start failing, the parity claim above should be treated as
lapsed until re-measured. (Its monotonicity check is the exception — a long session
legitimately fails it, as above.)
