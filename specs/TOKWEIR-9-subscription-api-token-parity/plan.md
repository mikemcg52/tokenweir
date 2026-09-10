# Implementation Plan: Verify subscription vs API-key transcript token parity (spike)

**Spec**: `specs/TOKWEIR-9-subscription-api-token-parity/spec.md`
**Branch**: `TOKWEIR-9-subscription-api-token-parity`
**Constitution**: `.specify/memory/constitution.md` is an **unfilled template** in this repo — its
placeholders (`[PRINCIPLE_1_NAME]`, …) were never replaced. It therefore imposes no binding
principle on this plan. The governing constraints are ADR-0001's pillars, which are real and are
honoured explicitly below.

## Technical context

| | |
|---|---|
| Language | Python 3.11+ (`requires-python = ">=3.11"`) |
| Runtime deps | **none** — the core is dependency-light by contract (ADR-0001 Pillar 2) |
| Test runner | `/workspace/repo/.venv/bin/pytest`, `CI=true`, pass codes `[0, 5]` (per `/workspace/.mado/project.yaml`) |
| Lint | `ruff` (dev extra) |
| Touched module | `src/tokenweir/parity.py` (new), `src/tokenweir/claude_code.py` (docs note only) |
| Docs | `docs/parity-subscription-vs-api.md` (new), `docs/adr-0001-token-weir.md` (item 6), `README.md` |

## The governing constraint: no new dependency

ADR-0001 Pillar 2 keeps third-party imports out of the core, and `tokenweir.claude_code` is
explicitly cited in the ADR as holding that line ("it carries no third-party import"). The obvious
way to call the Anthropic API is the `anthropic` SDK, and that is the wrong way here.

`urllib.request` from the standard library sends a JSON POST with headers perfectly well. Two API
endpoints, both simple, no streaming, no retries — the SDK buys nothing that justifies becoming the
first third-party import in the package. **Decision: stdlib only** (FR-010).

## Module design

One new module, `src/tokenweir/parity.py`, split so that everything decidable offline is decidable
offline (FR-008). This is the same seam TOKWEIR-15 established for the emit path — construction off
the metered path — applied to measurement.

```
src/tokenweir/parity.py
├── Turn                      frozen dataclass: message_id, model, timestamp, usage, output_text
├── iter_turns(path)          FR-001/FR-002 — per-turn extraction, de-duplicated on message.id
├── FieldComparison           frozen dataclass: field, reference, observed, difference, ratio, status
├── compare_usage(ref, obs)   FR-003..FR-005 — pure, total over the four billing fields
├── CheckResult               frozen dataclass: name, status (pass/fail/not_applicable), detail
├── consistency_checks(turns) FR-006/FR-007 — Probe C, as independent predicates
├── credential(...)           FR-020..FR-023 — env or file, stripped, blank == absent
├── ApiProbe                  FR-011..FR-014 — count_tokens + messages, stdlib urllib
└── main(argv)                FR-040/FR-041 — the reproducible command
```

### Why extraction is new code rather than reused wholesale

`scan_transcript` returns a `ScanResult` of *cumulative* totals; it deliberately throws away the
per-turn detail and never looks at message content, because the hook needs a session total and a
delta and nothing else. Probe A needs the opposite: each turn's own usage **paired with that turn's
output text**.

So `iter_turns` is a second reader over the same file, and FR-002's requirement is that it not
re-invent the semantics that matter. It reuses `_text` for field hygiene, `TokenTotals.from_mapping`
for count coercion, and — the one that would be a real bug to get wrong — the **de-duplication on
`message.id`**, because a transcript repeats one API response's usage across several lines and
counting a turn twice would corrupt Probe A's per-turn comparison exactly as it would corrupt the
hook's totals. The rule is documented in `claude_code.py` and is restated by reference, not by
guesswork.

### Probe A, precisely

For a turn: send `output_text` to `count_tokens` as a single user message and compare the returned
`input_tokens` to the turn's `output_tokens`.

The asymmetry is deliberate and needs stating, because it is the one place this probe could mislead.
`count_tokens` counts what it is *given as input*; we give it the assistant's output text. The
number it returns is the tokenizer's count of that text. The turn's `output_tokens` is the count the
Max transcript attributes to producing that same text. Same artifact, same tokenizer, two reporters.

Two known confounders, to be measured rather than assumed away:

1. **Message envelope overhead.** `count_tokens` counts a whole message request, so a few tokens of
   role/structure overhead ride along. This is a small constant, and it is *characterized* by
   running the probe across many turns of differing length: a constant offset shows up as a
   difference that does not scale, which is distinguishable from a ratio.
2. **Output text reconstruction.** A turn's output may be several content blocks (text plus
   `tool_use`). `output_tokens` covers all of them; the text we can reconstruct covers the text
   blocks. So a turn with tool calls will under-count on the tokenizer side. **Mitigation**: run the
   probe over text-only turns for the headline comparison, and report tool-bearing turns separately
   rather than mixing them into one average. This is recorded as a limit in the finding (FR-032).

That second point is why Probe A is run over a *population* of turns and reported as a distribution,
not as one number from one turn.

### Probe B and C

Probe B issues one small controlled `messages` call and inventories the `usage` keys, diffing them
against the key set observed in the transcript. It is cheap (a handful of tokens) and it is the only
part of the run that spends output tokens on the API key.

Probe C is pure and needs no credential: `cache_creation` sub-fields summing to
`cache_creation_input_tokens`; `iterations[]` summing to the turn's top-level billing fields; cache
reads advancing monotonically across the session. Each is its own `CheckResult` (FR-006), and each
reports `not_applicable` when its inputs are absent rather than a vacuous pass (FR-007).

## Credential handling

Read from `ANTHROPIC_API_KEY`, else from `--credential-file`. `.strip()` on the file's contents,
because the operator writes it with `printf`/`read` and a stray newline in an HTTP header fails
opaquely (FR-022). Blank and absent collapse to one "not configured" state (FR-023).

The invariant that needs a test, not just care: **the key must not reach any output stream**
(FR-021). `urllib`'s exceptions can carry request context, so the probe catches and re-raises with a
message it composes itself rather than letting an arbitrary exception string propagate. Saved result
files carry model and usage, never headers.

FR-024 gets a hygiene test alongside the existing ones in `tests/test_repo_hygiene.py`: no tracked
file matches an API-key shape.

## Testing strategy

`tests/test_parity.py`, all offline (FR-008):

| Area | What is asserted |
|---|---|
| `iter_turns` | de-duplication on repeated `message.id`; turns with no usage skipped; malformed lines skipped; text assembled from multiple text blocks; unreadable file yields nothing rather than raising |
| `compare_usage` | exact agreement → `parity`; difference → `offset` with correct difference and ratio; field absent on one side → `absent`, never `parity`; a one-sided extra field is surfaced (FR-005); zero reference → no division |
| `consistency_checks` | each identity passes on consistent input, fails on a planted violation, and reports `not_applicable` when its inputs are missing |
| `credential` | env preferred; file read and stripped of trailing newline; blank file == absent; absent == "not configured" |
| `ApiProbe` | with no credential, skipped not failed (FR-015); network error surfaces a composed message and does not retry unboundedly; the key never appears in a raised message or printed report (FR-021) |
| hygiene | no tracked file contains an API-key-shaped string (FR-024) |

Network-touching tests use a fake transport (an injected opener), never a live call: the live call
happens once, deliberately, when the finding is produced.

The real transcript in this pod is the *measurement input*, not a test fixture — tests use small
synthetic transcripts written to `tmp_path`, so they do not depend on a developer's own history.

## Execution order

1. Spec, plan, tasks (this).
2. `parity.py` pure logic + tests → commit. Green with no credential.
3. Probe C executed on the real transcript (no credential needed) → numbers recorded.
4. **Gate**: credential present? Probes A and B executed → numbers recorded. If the credential never
   arrives, the run stops and escalates rather than writing a finding it did not measure (FR-036).
5. Finding document, ADR item 6, emitter note (FR-034/FR-035 branch on the result) → commit.
6. Review → fix loop.

Step 4 is a real gate: FR-036 forbids asserting an unmeasured parity, and steps 5's verdict is not
writable without it.

## Risks

| Risk | Handling |
|---|---|
| Probe A confounded by tool-bearing turns | Text-only turns for the headline; tool turns reported separately (above). |
| Envelope overhead read as a discrepancy | Population across turn lengths distinguishes a constant offset from a ratio. |
| A "verified parity" note outliving its validity | FR-031 forces conditions and a point-in-time statement into the finding. |
| Credential leaking into a commit | FR-021 code path plus FR-024 hygiene test. |
| Spend | Probe B is one small call; Probe A is `count_tokens`, which is not a generation. Bounded and small. |
