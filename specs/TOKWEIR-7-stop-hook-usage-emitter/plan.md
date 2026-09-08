# Implementation Plan: Stop-hook usage emitter (transcript delta → tokenweir)

**Branch**: `TOKWEIR-7-stop-hook-usage-emitter`
**Spec**: [spec.md](./spec.md)
**Jira**: TOKWEIR-7 (Story), parent epic TOKWEIR-2
**Date**: 2026-09-08

## Summary

Add the subscription capture adapter: a deterministic Claude Code `Stop` hook that reads the
session transcript, works out how many tokens the turn just finished actually consumed, and hands
one `UsageRecord` to the existing emitter with `pricing_mode=subscription`.

It is a **producer**, and that is the whole shape of the work. Everything downstream of
`emit_usage` — the guarded seam, the buffered client, the bounded close, the transports, the
store, the schema — was built by TOKWEIR-4/5/6/15 and is used here exactly as published. Nothing
in the contract or the emit path changes. The new code answers three questions the library has
never had to answer before:

1. **What did this turn cost?** — de-duplicated cumulative totals from the transcript, minus a
   remembered baseline.
2. **Whose turn was it?** — attribution read from the orchestrator's environment, never from the
   model.
3. **How do we guarantee we cannot hurt the session?** — exit 0 on every path, a bounded flush,
   nothing on stdout, and the host's own hook `timeout` for the rest.

## Technical Context

**Language/Version**: Python 3.11+ (as declared in `pyproject.toml`).
**Runtime dependencies**: none. The hook uses `json`, `os`, `sys`, `hashlib`, `tempfile`,
`pathlib` — all stdlib — plus `tokenweir` itself. This keeps ADR-0001 Pillar 2's
"core stays dependency-light" true of the new module, and it is why the module can live in the
core package rather than behind an extra.
**Optional dependencies**: `pika` (via `tokenweir[amqp]`) and `psycopg` (via `tokenweir[postgres]`)
are imported only if the environment selects that transport, and only through the existing
adapters, which already defer their imports.
**Storage**: a small JSON state file per transcript under a cache directory. Disposable.
**Testing**: `pytest`, per `/workspace/.mado/project.yaml` — `/workspace/repo/.venv/bin/pytest`.
**Target platform**: Linux (the stream pod) and macOS (a developer's laptop). Nothing in the
module is platform-specific.
**Project type**: single library.
**Performance**: one pass over the transcript per turn, streaming, constant memory in the number
of lines and linear in the number of distinct API responses.
**Constraints**: exit 0 always; nothing on stdout; no `pika`/`psycopg` at import; the run bounded
by the host's own hook timeout rather than by a timer of our own.

## Constitution Check

`.specify/memory/constitution.md` is still the unfilled speckit template — it carries placeholder
principles and no ratified content, so there is nothing project-specific in it to check against.
The operative design authority for this repository is **ADR-0001**, and the plan is checked
against it instead:

| ADR-0001 | Bearing on this plan | Status |
|---|---|---|
| Pillar 2 — emission is fire-and-forget, off the critical path | The hook emits through `BufferedEmitter` + `emit_usage` rather than re-deriving the guarantee. | Held |
| Pillar 2 — core stays dependency-light | The new module imports no third-party package at all. | Held |
| Pillar 3 — provider-neutral | `model` is stored verbatim from the transcript; nothing is inferred from it. | Held |
| Pillar 4 — deterministic hook, not an LLM skill | The hook is a plain Python process. It cannot call a model and has no way to. | Held |
| Pillar 4 — attribution from the orchestrator, not the model | Attribution comes from environment variables only. | Held |
| Pillar 4 — scope now: raw tokens, defer capacity modelling | Raw counts only; no percentages, no limits, no cost. | Held |
| Pillar 5 — schema ownership | No migration, no contract change. The record uses existing columns. | Held |

## Project Structure

### Documentation (this feature)

```
specs/TOKWEIR-7-stop-hook-usage-emitter/
├── spec.md      # the requirement
├── plan.md      # this file
└── tasks.md     # the ordered work
```

### Source Code (repository root)

```
src/tokenweir/
├── claude_code.py          # NEW — the whole adapter: parse, delta, attribute, emit, entry point
└── (unchanged)             # contract.py, sink.py, emitter.py, amqp.py, postgres.py, source.py

tests/
└── test_claude_code_hook.py  # NEW — the story's acceptance, in four groups matching the spec

pyproject.toml              # + [project.scripts] console entry point
README.md                   # + "Subscription capture: the Claude Code Stop hook" section
docs/adr-0001-token-weir.md # checklist item 5 ticked
```

**One module, not a package.** The adapter is ~5 cohesive responsibilities that all exist to serve
one entry point, and splitting it across files would put a package boundary where there is no
seam. If a second capture source arrives (the ADR names the OTel metric as a fallback), *that* is
when a `tokenweir/capture/` package earns its existence.

## Key design decisions

### 1. Cumulative baseline, not a file cursor

The state file stores the four cumulative totals already emitted. Each run recomputes the
transcript's de-duplicated cumulative totals and reports the difference.

The alternative — remembering a byte offset or the last line read — is what the phrase "delta
since the last emit" first suggests, and it is wrong in exactly the case the hook must survive: a
run that reads the file and then fails to emit has advanced its cursor and permanently deleted
that turn's tokens. With a cumulative baseline that is written *after* the emit, the same failure
merges the turn into the next record. Late and coarse beats gone.

### 2. De-duplicate on `message.id`

Usage belongs to one API response. Claude Code may write several transcript entries for one
response, each repeating the same `message.usage` object, so summing entries over-counts. The scan
keeps a set of seen ids and takes each response's usage once; an entry with no usable id falls
back to its own entry `uuid`, and an entry with neither is counted on its own (it cannot be a
duplicate of anything identifiable).

The set is per-scan, not persisted — the cumulative baseline is what crosses invocations, so the
state file stays four integers rather than growing with the session.

### 3. Regression means "the file was replaced", not "negative tokens"

If the recomputed cumulative is below the baseline in any field, the transcript at that path is not
the one the baseline describes. Emitting the difference would be a negative count, which the
contract rejects outright; emitting the whole new total would double-count a resumed session. So
the baseline is reset to the current totals and nothing is emitted. One transition is silently
absorbed, which is the correct price for a case that only arises when the ground truth moved.

### 4. The record's field mapping, and the one uncomfortable slot

| Record field | Source | Note |
|---|---|---|
| `request_id` | last counted `message.id`, else `<session_id>:<uuid4>`, else `claude-code:<uuid4>` | identifies the turn and is traceable back into the transcript; **stable** rather than fresh per emission, so a re-report of the same turn is recognizable as a duplicate (FR-019) |
| `app_id` | `TOKENWEIR_APP_ID`, else `claude-code` | a constant, so per-app rollups do not fragment per stream |
| `endpoint` | `TOKENWEIR_ENDPOINT`, else `claude-code/stop-hook` | a turn is an aggregate; `/v1/messages` would blend it with single calls |
| `model` | most recent counted message's `message.model`, else `unknown` | verbatim, per Pillar 3 |
| `status` | `ok` | the turn completed; the hook fires on `Stop` |
| `workload` | `MADO_ISSUE_KEY` | the unit of work |
| `queue` | `MADO_PHASE` | **the compromise** — see below |
| `parent_request_id` | `MADO_STREAM_ID` | the relation the column was built for |
| token fields | the turn delta | |
| `latency_ms` | unset | transcript timestamps are not call latency |
| `pricing_mode` | `MADO_PRICING_MODE` if the contract recognizes it, else `subscription` | |
| `ts` | last counted entry's timestamp, else now, normalized to UTC ISO-8601 | |

`queue` carrying the phase is the one mapping that is not a natural fit, and the plan says so
rather than letting a reviewer discover it. The v1 contract has no phase field; adding one is a
`SCHEMA_VERSION` bump plus a migration plus every consumer, which is Pillar 5 territory and not
this story's to spend. `queue` is nullable, unconstrained, and semantically the nearest thing to
"which lane did this work go through". It is documented in the module, in the spec's Assumptions
and in the README, so a future v2 can move it deliberately rather than find it.

### 5. Two guarantees that the session is never disturbed, and one that is not ours

They are separate because they fail separately:

- **`main()` catches `Exception` and returns 0.** Covers every bug in the hook's own logic. It
  deliberately does not catch `BaseException`: `KeyboardInterrupt` and `SystemExit` mean the
  process is being torn down and swallowing them would be worse than the failure.
- **A bounded emitter close** (`close_timeout`, default 3s). Covers a sink that accepted the record
  and cannot deliver it. `BufferedEmitter.close` already implements exactly this bound; the hook
  just has to choose a small number rather than the 5s default.

The third case — nothing raises and nothing returns — is bounded by **Claude Code's own hook
`timeout`**, and is deliberately not re-implemented here.

*Revised after review 2.* The first draft added a self-imposed `SIGALRM` budget. It was over-scope:
the story's non-blocking clause names the host's timeout as the mechanism, and the second timer
brought its own failure surface — an alarm is one-shot and every guard in this module catches
`Exception`, so the natural implementation was absorbed by its own error handling and silently
spent, which review 1 found. Fixing that required a `BaseException` subclass, which then leaked a
temp file past the state write's cleanup and falsified four separate documentation claims. The
argument for giving up early rather than being killed is real — a synchronous hook that wedges
costs the developer the whole 30 seconds — and is recorded as a follow-up rather than built here.

### 6. Sink selection is environment-driven, and defaults to nothing

`TOKENWEIR_AMQP_URL` → `AMQPSink.from_url`; else `TOKENWEIR_DSN` → `DirectSink(PostgresSource)`;
else `NullSink`. The imports happen inside the selecting branch, so a core install never touches
`pika` or `psycopg`, and a sink whose construction raises (bad URL, missing driver, dead broker)
degrades to `NullSink` with a stderr note rather than to a failed hook.

`NullSink` as the default is deliberate: an unconfigured hook is a no-op, not an error. It is also
why `--selftest`-style verification is unnecessary — the README tells a reader to point
`TOKENWEIR_DSN` at a scratch database and look.

### 7. Why the emitter at all, for a process that exits immediately

A single-record, short-lived process could call `DirectSink.emit` directly. It should not:
`BufferedEmitter` is where "swallows failures" and "bounded close" are *implemented*, and
`emit_usage` is where "a malformed record is a drop, not a raise" is implemented. Re-deriving
either here would be the fourth copy of a guarantee the library exists to own — the exact argument
`emitter.py`'s own docstring makes.

### 8. What "stored" is measured by — and why not the emitter's own number

*Added at review 3, which found the first answer wrong in a way that lost data with both shipped
transports.*

`EmitterStats.delivered` counts records the sink took **without raising**. That reads like a
delivery signal and is not one, because `Sink.emit` may not raise by contract — so every adapter in
this library catches its own transport failure and returns normally: `DirectSink` on an unreachable
store, `AMQPSink` on a failed publish. Both increment `delivered`, both stored nothing. A baseline
advanced on that number deletes the turn, permanently and silently, in exactly the outage the
carry-forward design exists for.

So the run combines acceptance with the transport's own **drop** counter, sampled either side of
the emit — a negative signal, chosen after review 4 showed the success-counter version unsound in
both directions. `AMQPSink.published` counts frames handed to a socket over a transport with no
publisher confirms (`amqp.py` documents that a publish to a missing exchange returns normally), so
it over-claims; and any conforming sink keeping a `written` it does not increment per record would
look permanently failed, so it under-claims. "Did you lose it?" is answerable; "did you store it?"
is not, from inside this process. The residual — a live broker with a missing exchange — is stated
in the README rather than papered over.

Two consequences follow, and both are requirements now (FR-012a, FR-012b):

- A **configured** transport that could not be constructed degrades to a discarding sink so the
  hook cannot fail — and must not then count as stored, or every session started before its broker
  was up loses its first turns. An **unconfigured** hook is the opposite and must advance: it is
  discarding by choice, and a held baseline would make the first configured turn report everything.
- The sink is built **lazily**, only when there is something to emit. A `Stop` hook fires on every
  turn and many add no tokens; a connection opened to discover that is a connection per turn, and
  one that can wedge with nothing at stake.

## Test approach

`tests/test_claude_code_hook.py`, grouped to match the spec's four user stories, with a helper
that writes synthetic transcripts and a recording sink.

Everything is tested through the module's real entry point with real files in `tmp_path` — no
mocking of `open`, no monkeypatching of the parser. The one substitution is the **sink**, which is
the seam the library already publishes for exactly this purpose.

- **US1 (counts)**: multi-message turn sums; second invocation deltas; duplicate `message.id`
  counted once; sidechain entries counted; `pricing_mode` is `subscription`; the emitted record
  validates against `schema/usage-record.v1.json`.
- **US2 (never disturbs)**: raising sink; hanging sink against the bounded close; a *conforming*
  sink that drops without raising (the shape every real adapter has); a configured transport that
  cannot be constructed; garbage stdin; missing/truncated transcript; unwritable state dir; stdout
  is empty; exit code is 0 in every one of them.
- **US3 (attribution)**: each `MADO_*` variable lands in its field; blank and unset both leave
  `None`; an invalid `MADO_PRICING_MODE` still emits, as `subscription`.
- **US4 (packaging/docs)**: the console script is declared and resolves; importing the module
  pulls in neither `pika` nor `psycopg`; the README documents each environment variable the module
  actually reads (a guard against the documentation rotting away from the code).

Two tests are worth calling out as the ones that would have caught the defects this design is
shaped around: the duplicate-`message.id` test (silent over-counting) and the emit-fails-then-next-
turn-carries-it test (silent under-counting). Neither failure is visible in a record that looks
perfectly well-formed.

## Complexity Tracking

No constitutional deviations to record. The one piece of deliberate complexity is the atomic state
write (FR-016), which exists to close a specific failure the simpler version leaves open: an
interrupted in-place write leaves a truncated file that every later invocation reads as corrupt.

The `SIGALRM` budget was the other, and it was removed at review 2 for being scope the story did
not carry — recorded here rather than quietly dropped, because "we tried this and took it out"
is more useful to the next reader than silence about it.
