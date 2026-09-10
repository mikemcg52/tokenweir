# Subscription vs API-key token parity — the finding (TOKWEIR-9)

**Verdict: parity. No correction factor is needed.**

A Claude Max transcript's `message.usage` counts are true token counts on the same
scale the Anthropic API reports. The relationship between a transcript's
`output_tokens` and the provider's own tokenizer is **exact, affine, and slope-1**,
and the *same* relationship holds for the API's own `output_tokens` on a response it
generates itself.

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
| Transcripts | 3 sessions in one stream pod, 373 de-duplicated assistant responses |
| Headline sample | 12 responses, 181–1206 output tokens (a 6.7× span) |
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

**E = 6**, invariant. With that subtracted, all 12 headline responses:

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

**Why this rules out a scale factor.** The *difference* is constant at exactly 2 while
the *ratio* drifts 0.978 → 0.997 across the range. A scaled count behaves the opposite
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

```
out=138  bare_text=111  thinking=25  predicted=111+25+2=138   MATCH
out= 39  bare_text= 18  thinking=19  predicted= 18+19+2= 39   MATCH
out=  4  bare_text=  2  thinking= 0  predicted=  2+ 0+2=  4   MATCH
out= 28  bare_text=  7  thinking=19  predicted=  7+19+2= 28   MATCH
```

```
api.output_tokens = tokens(text) + thinking_tokens + 2        (4 of 4 exact)
```

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
one generally does not. Across 3 independent sessions:

| Session | Responses | `cache_creation` sums | `iterations[]` sums | cache reads non-decreasing |
|---|---:|---|---|---|
| c301b927 | 144 | 144/144 PASS | 144/144 PASS | 143/143 PASS |
| f603b29b | 139 | 139/139 PASS | 139/139 PASS | 138/138 PASS |
| f30a6c83 | 69+ | PASS | PASS | PASS |

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
names on both sides; and a transcript's internal aggregation is exactly consistent on 373
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
- **Turns containing `tool_use` are not covered by the headline.** A tool call's tokens
  cannot be re-tokenized faithfully from the transcript. 12 of 373 responses were
  headline-eligible; the rest were excluded for that reason. The excluded turns are not
  suspected — they are unmeasurable by this method.
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
  --credential-file /path/to/api-key
```

Probe C runs with no credential at all and is the cheap early warning: if its identities
ever start failing, the parity claim above should be treated as lapsed until re-measured.
