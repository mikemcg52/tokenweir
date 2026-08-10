# Implementation Plan: Guarded record construction on the metered request path

**Branch**: `TOKWEIR-4-usage-record-contract` | **Date**: 2026-08-10 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/TOKWEIR-15-guarded-record-construction/spec.md`
**Jira**: TOKWEIR-15 (Story), `Relates` TOKWEIR-4

## Summary

Add a guarded seam to the emit side so that constructing and emitting a `UsageRecord` on a request
path cannot raise into the caller. Three thin public functions in `src/tokenweir/sink.py` —
`build_record`, `emit_record`, `emit_usage` — exported from the package, plus a new test module and
a README section.

Nothing existing changes behaviour: `UsageRecord.__init__` still validates and still raises, the
`Sink` protocol's "MUST NOT raise" contract is unchanged, and `schema/usage-record.v1.json` is
untouched. This is additive API surface only.

## Technical Context

**Language/Version**: Python 3.11+ (pod runs 3.12.3)
**Primary Dependencies**: None added. Standard library `logging` and `typing` only.
**Storage**: N/A
**Testing**: pytest, via `/workspace/.mado/project.yaml`
(`CI=true /workspace/repo/.venv/bin/pytest`, pass exit codes `0` and `5`)
**Target Platform**: Library consumers on a request critical path — the AI Gateway (TOKWEIR-10) and
the MADO cloud-edge (TOKWEIR-11), plus the emitter client (TOKWEIR-6) in this repo.
**Project Type**: Single Python library (`src/` layout)
**Performance Goals**: The guard sits on a request path, so it must add no I/O and no work beyond a
`try`. Python's zero-cost `try` on the success path means the guard costs nothing when it does not
fire; the logging call happens only on the failure path.
**Constraints**: No new dependency (ADR-0001 Pillar 2). No change to the wire contract or the
published schema (FR-015). Must not catch `BaseException` (FR-004). Must not configure logging on
the application's behalf (FR-008).
**Scale/Scope**: One source file extended, one `__init__.py` export block, one new test module, one
README section.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` remains an **unfilled template** (every principle is a
`[PRINCIPLE_N_NAME]` placeholder), so it imposes no project-specific gates. Recorded rather than
silently skipped: the gate is vacuous, not passed on the merits.

The ADR-0001 constraints that bear on this change:

| ADR-0001 pillar | Applies how | Status |
|---|---|---|
| Pillar 2 — transport-agnostic, dependency-light core | The guard must not drag in a metrics or logging library; drops are surfaced by return value plus stdlib `logging` | PASS — stdlib only |
| Pillar 2 — emission is fire-and-forget, off the critical path | This story is the pillar's own gap: the promise was discharged only at `Sink.emit`, and construction sits upstream of it | PASS — this change closes it |
| Pillar 3 — provider-neutral core | The guard inspects no field semantically; it converts failures into drops | PASS — nothing provider-specific added |

## Project Structure

### Documentation (this feature)

```text
specs/TOKWEIR-15-guarded-record-construction/
├── spec.md
├── plan.md    # This file
└── tasks.md
```

### Source Code (repository root)

```text
src/tokenweir/
├── __init__.py     # MODIFIED — export build_record, emit_record, emit_usage
├── contract.py     # UNCHANGED
├── sink.py         # MODIFIED — the guarded seam lives here
└── source.py       # UNCHANGED

tests/
└── test_guarded_emit.py   # NEW — US1-US4

schema/usage-record.v1.json  # UNCHANGED (asserted by the existing suite)
README.md                    # MODIFIED — document the seam
```

**Structure Decision**: The seam lives in `sink.py`, not in a new module. `sink.py` is already "the
emit side of the pipeline" and already imports `contract`; the guard is emit-side machinery that
happens to also cover the construction immediately upstream of an emit. Inventing a fourth
top-level module would add a seam name ADR-0001 does not use, for three functions totalling well
under a hundred lines. If the emitter client (TOKWEIR-6) later grows a substantial client object,
that is the moment to reconsider — not now.

## Design

### The three functions

```python
def build_record(**fields: Any) -> Optional[UsageRecord]:
    """Construct a UsageRecord, or return None if the values are invalid."""

def emit_record(sink: Sink, record: UsageRecord) -> bool:
    """Emit a record; return False if the sink raised."""

def emit_usage(sink: Sink, **fields: Any) -> Optional[UsageRecord]:
    """Build and emit in one call. Returns the record if it was emitted, else None."""
```

Why three and not one: `emit_usage` is the request-path call and covers the common case, but the
gateway stamps `ts` and computes `latency_ms` *after* the call it is metering returns, and a
batching emitter builds now and emits later. Those callers need the two halves separately, and if
the library does not give them the guarantee they will hand-roll `try/except` — the exact
duplication this story exists to prevent. `emit_usage` is implemented as `build_record` +
`emit_record`, so there is one implementation of each guarantee, not two (FR-003).

Why `**fields` rather than a positional record: the whole point is that the *construction* is
inside the guard. A signature taking an already-built `UsageRecord` would put construction back on
the caller's side of the `try`, which is the bug.

### Return-value contract

| Call | Success | Drop |
|---|---|---|
| `build_record` | the `UsageRecord` | `None` |
| `emit_record` | `True` | `False` |
| `emit_usage` | the `UsageRecord` | `None` |

A `UsageRecord` is a frozen dataclass and is never falsy, so `if emit_usage(...) is None` and
`if not emit_usage(...)` agree — but the docstrings state the `None` contract explicitly rather
than leaning on that (spec edge case: return-value ambiguity).

`emit_usage` returns `None` for *either* a construction drop or an emission failure. A caller that
needs to tell them apart uses the two halves; the log lines already distinguish them (FR-007).

### What happens before the guard runs (FR-019, FR-020)

The subtlest class of bug in this story is not inside the `try` — it is everything Python does
*before* entering the function, where no `try` can reach. Two instances, found one review apart, and
worth stating as one idea so a future change does not reintroduce a third:

1. **Parameter collision (FR-019).** A `**fields` mapping carrying the key `"sink"` collides with
   the parameter of the same name and raises `TypeError: got multiple values for argument 'sink'`
   during argument binding. Fixed by making every named parameter of every guarded call
   positional-only, which takes those names out of the keyword namespace so each becomes an ordinary
   unknown field, dropped and logged like any other. `emit_record` takes no `**overrides` and so
   cannot be collided with today; it is positional-only anyway, because one rule for the whole seam
   is what stops the next parameter added to it from quietly reopening the hole.
2. **Non-string keys (FR-020).** `**` unpacking happens in the *caller's* frame, so
   `emit_usage(sink, **mapping)` raises `TypeError: keywords must be strings` before any tokenweir
   code runs, if the mapping came from JSON, a header dict, or generic code. No signature change can
   guard a splat at the call site. Fixed by accepting the **mapping itself** as a positional
   argument, so the unpacking happens inside the guard.

FR-020 is also what makes late stamping safe (see below), so the two problems have one answer:
`build_record(fields, **overrides)` and `emit_usage(sink, fields, **overrides)` take a mapping,
keyword fields, or both — mapping first, keyword overrides applied on top.

### Stamping without a second unguarded call

US4's motivating caller — the gateway, which knows `latency_ms` only once the metered call returns —
must not be told to reach for `dataclasses.replace`. `replace` re-runs `__post_init__`, so it is a
validating call, and on a request path that is precisely the unguarded raise site this story exists
to remove. The `overrides` parameter is the answer: build **once**, after the call, with the stamp
merged in — `emit_usage(sink, base_fields, latency_ms=elapsed_ms)` — so there is one validating call
and it is inside the guard. `replace` remains correct off the request path and is unchanged.

### Refusing a non-record (FR-018)

`emit_record` type-checks before touching the sink. This is not defensive noise: `build_record`
returns `None` on a drop, so the *naive* composition of the two halves the library is asking
consumers to use — build, then emit, without an `is not None` between them — would hand `None` to a
conforming sink. A conforming sink cannot raise, so it would persist it. That converts a producer
bug into garbage in the store, which is the one thing this guard must not do. Refusal logs a
`WARNING` with no `exc_info`, because nothing was caught — there is no traceback, and attaching one
would render a misleading `NoneType: None`.

### Logging

One module logger, `logging.getLogger(__name__)` → `tokenweir.sink`. No level, no `basicConfig`.
Messages:

- construction drop: `"tokenweir: dropping usage record — construction failed; no metering for this call"`
- emission failure: `"tokenweir: usage record not emitted — the sink failed; no metering for this call"`
- refusal: `"tokenweir: refusing to emit a non-UsageRecord; no metering for this call"`

"the sink failed" rather than "the sink raised": the same guard catches a sink that is `None` or
misconfigured, where the truthful cause is an `AttributeError`, not a raising `emit`. `exc_info`
carries the real cause in both cases, and the message stays trivially distinguishable from a
construction drop, which is what FR-007 asks for.

**Handler policy — the `NullHandler` decision (FR-008, FR-017).** The package attaches exactly one
`NullHandler` to the `tokenweir` logger in `__init__.py`. Recorded because the naive reading —
"a library should attach no handler at all" — is wrong and was the first thing tried: a logger with
no handler anywhere in its chain falls through to the standard library's `logging.lastResort`, which
writes `WARNING` and above, traceback included, to `sys.stderr`. An application that configured no
logging would therefore get one traceback per metered request from a systematically broken producer
— the library taking an output decision that belongs to whoever embeds it, which is exactly what
FR-008 forbids. `NullHandler` emits nothing, so attaching it is declining to configure logging
rather than configuring it.

The cost, stated rather than glossed: an application with no logging configuration now sees nothing
in its logs. That is the right default — it is the application's choice, reversed by one line of
`basicConfig` — and it does not make drops unobservable, because the return value reaches the caller
regardless of logging configuration (FR-017). "Never silent" is discharged by the return value plus
an available log, not by writing to a stream nobody asked for.

Both at `WARNING` with `exc_info=True`, so the underlying `ValueError`/`TypeError` — which already
names the offending field, e.g. `app_id is required and must not be blank` — reaches the operator
(FR-006). The library does not re-derive or re-format that message; the contract's own error text is
the best description of what was wrong.

The log call is itself wrapped in a bare `try/except Exception: pass` (FR-009). This is deliberate
paranoia and is called out here so a reviewer does not read it as a swallowed bug: an application
can install a handler, filter, or `__repr__` that raises, and "never raises" has to survive that or
the guard has merely moved the throw site. Nothing else is inside that inner `try` — only the
logging call — so it cannot mask a real failure in the guarded operation.

No record field is interpolated into the message. The contract carries no prompt or payload data, so
there is nothing secret to leak, but formatting an arbitrary caller value is also the cheapest way
to make the logging call itself throw; `exc_info` already carries what the operator needs.

### What is caught

`except Exception`, never `BaseException` (FR-004). This covers both error types TOKWEIR-4 kept
distinct — `ValueError` for an invalid value and `TypeError` for a missing or unknown keyword
argument (FR-005) — without enumerating them, because enumerating would silently re-raise anything
the contract's validation grows later.

`KeyboardInterrupt`, `SystemExit` and `GeneratorExit` propagate. A metering guard that swallows
Ctrl-C is a worse bug than the one being fixed.

## Complexity Tracking

No constitutional violations to justify. The one judgement call worth recording is three public
functions where one would satisfy the literal acceptance; the rationale is in **Design** above, and
the mitigation is that the fused call is a composition of the other two rather than a third code
path.

## Phase Plan

| Phase | Content |
|---|---|
| 0 — Research | None needed. The contract's failure modes are already enumerated and tested by TOKWEIR-4 (`tests/test_contract.py`); this story reuses that enumeration as its invalid-input matrix. |
| 1 — Design | Above. |
| 2 — Tasks | See [tasks.md](./tasks.md). |

## Risks

- **A guard that hides producer bugs.** Mitigated by FR-006/US3: every drop logs at `WARNING` with
  the original exception, and the return value lets a caller count drops. The story's own framing —
  "degrades to no metering for this call" — accepts this trade explicitly.
- **Reviewer reads the nested `try` around logging as sloppiness.** Mitigated by the comment in the
  code and the rationale here.
- **Scope creep into TOKWEIR-6/-10.** The seam is landed and proven here; adopting it in the gateway
  is TOKWEIR-10, in another repository. Stated in the spec's assumptions and to be repeated in the
  run report rather than quietly claimed.
