# Implementation Plan: Orchestrator env injection for phase/issue context

**Branch**: `TOKWEIR-8-orchestrator-env-injection` | **Date**: 2026-09-08 | **Spec**: [spec.md](./spec.md)
**Input**: `specs/TOKWEIR-8-orchestrator-env-injection/spec.md`

## Summary

Publish, from `token-weir`, the contract that MADO's orchestrator injects against: a closed set of
lifecycle phase kinds, a canonical label grammar with an occurrence number, a normalizer that maps
the spellings a human or an older orchestrator would write onto that grammar, and a builder that
returns the four-variable environment block to export per iteration. The Stop hook then normalizes
`MADO_PHASE` through the same function, so a record carries a canonical label whichever end wrote
it. The orchestrator-side export is a change in the `mado` repository and is out of scope here
(spec, "The repo boundary").

## Technical Context

**Language/Version**: Python 3.11+ (the package targets 3.11; the pod's venv is 3.12)
**Primary Dependencies**: none — the new module is stdlib-only by requirement (FR-014)
**Storage**: N/A
**Testing**: pytest (`/workspace/.mado/project.yaml`: `.venv/bin/pytest`, `CI=true`, pass on exit 0 or 5)
**Target Platform**: Linux; the library is imported by the Stop hook in a stream pod and, later, by the orchestrator in `mado-system`
**Project Type**: single library (`src/tokenweir`)
**Performance Goals**: N/A — the work is string normalization on a path that already runs once per turn
**Constraints**: no new third-party import in the core package; no change to `SCHEMA_VERSION`; the hook must remain unable to fail a session
**Scale/Scope**: one new module (~2 public functions + an enum), one edit inside `claude_code.attribution_from_env`, one README section

## Constitution Check

`.specify/memory/constitution.md` in this repo is the **unfilled speckit template** — every
principle is still a `[PRINCIPLE_N_NAME]` placeholder. There is therefore no project constitution
to check against, and this gate passes vacuously rather than by assertion. Recorded here so that a
reader does not mistake silence for compliance. The house rules that *do* bind this change are
written in the code and in TOKWEIR-7's spec: stdlib-only core, no import of a transport at module
scope, and a hook that exits 0 on every path.

## Project Structure

### Documentation (this feature)

```text
specs/TOKWEIR-8-orchestrator-env-injection/
├── spec.md
├── plan.md      # this file
└── tasks.md
```

No `research.md`, `data-model.md` or `contracts/` — there is nothing to research (the lifecycle
vocabulary is read off `mado-phase --help`), the data model is four strings, and the contract is
the module itself rather than a document about it.

### Source Code (repository root)

```text
src/tokenweir/
├── orchestrator.py     # NEW — the taxonomy and the injection contract
├── claude_code.py      # EDIT — normalize MADO_PHASE in attribution_from_env()
└── __init__.py         # EDIT — re-export the new public names

tests/
├── test_orchestrator.py    # NEW — taxonomy, normalization, builder, round trip
└── test_claude_code_hook.py # EDIT — the hook's phase is canonical; unknown is preserved + noted

README.md               # EDIT — taxonomy + what the orchestrator must export
```

## Design

### Why a new module rather than a home in an existing one

`contract.py` is the **wire** contract, shared by every producer including the AI Gateway's
`api_metered` path. The phase taxonomy is a MADO convention about what to *put in* a free-text
field, not a constraint the wire format imposes on everyone, and putting it there would imply
`queue` is now enumerated for all producers. `claude_code.py` is the consumer of the block and
imports the hook's whole world; an orchestrator that wants only the producer half should not have
to import a Stop hook to get it. A separate `orchestrator.py` is what lets FR-014 ("imports
nothing outside the standard library and does not import the hook") be true and stay true.

The dependency direction is one-way: `claude_code` imports `orchestrator`, never the reverse.

### The taxonomy

```python
class PhaseKind(str, Enum):
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    FIX = "fix"
```

`str`-valued to match `PricingMode`, so a label composes by concatenation and a member compares
equal to its wire string.

A **label** is `kind` or `kind-N` (N a positive integer). Two functions carry it:

- `phase_label(kind, occurrence=None) -> str` — the producer's constructor. Raises on a
  non-positive or non-integral occurrence and on an unknown kind (FR-007, FR-012).
- `normalize_phase(value) -> str | None` — the tolerant reader. `None`/blank → `None` (FR-006);
  a recognized phase → its canonical label; anything else → the value with whitespace collapsed
  (FR-005).

FR-005 also requires the caller to be able to *tell* the two apart. Rather than a second return
value or a sentinel, `is_canonical_phase(value) -> bool` answers it directly: the hook asks it to
decide whether to note a diagnostic, and a report could ask it to flag rows. Keeping normalization
single-valued means the common call site stays a one-liner.

Parsing accepts, case-insensitively, with `_`, `#` and runs of whitespace treated as separators:
a leading English ordinal (`1st review`, `22nd fix`), a trailing bare number (`review 2`),
an already-canonical label, and the aliases `bug fix`/`bugfix` → `fix`, `implement` →
`implementation`. English ordinal suffixes are validated, not merely stripped: `11th` is 11 and
`11st` is not an ordinal at all, so it falls through to the unrecognized path rather than being
read as 11.

### The injection block

```python
ATTRIBUTION_ENV = {          # the one place the names are spelled
    "issue_key":   "MADO_ISSUE_KEY",
    "phase":       "MADO_PHASE",
    "stream_id":   "MADO_STREAM_ID",
    "pricing_mode": "MADO_PRICING_MODE",
}

def attribution_env(*, issue_key, phase=None, stream_id=None,
                    pricing_mode=PricingMode.SUBSCRIPTION) -> dict[str, str]: ...
```

Always four keys (FR-010) — a partial block would leave the previous iteration's phase standing
during the next one, which is the exact failure this whole story exists to prevent, and it would
be invisible because the record would still look well-formed.

`issue_key` is keyword-required rather than optional: an iteration that cannot say what it is
working on has nothing to attribute, and a caller that genuinely has no key can pass `None`
explicitly. Blank-but-present raises (FR-012); `None` exports `""`, which TOKWEIR-7's `_env`
already reads back as "unknown" (FR-013).

`pricing_mode` goes through `PricingMode.coerce`, so the producer raises on a mode the consumer
would have silently downgraded to `subscription`.

### The hook edit

`attribution_from_env()` gains one line — `queue` becomes `normalize_phase(_env("MADO_PHASE"))` —
plus a guarded `_note` when `is_canonical_phase` says the label is not in the taxonomy (FR-015,
FR-016). Nothing else in the hook moves: same fields, same mapping, same unset/blank rule
(FR-017). The whole edit sits inside the existing function, which is already the single place
attribution comes from.

### Risks

| Risk | Handling |
|---|---|
| The taxonomy is wrong — the ACP lifecycle names phases `mado-phase` does not | FR-005 preserves unrecognized labels, so being wrong costs a diagnostic and a non-canonical row, not a lost record. Extending the enum later is additive. |
| Normalization mangles a label a human meant literally | Only recognized shapes are rewritten; everything else is returned as written (modulo whitespace). |
| Records already in the store carry old spellings | Out of scope by decision — this story sets the contract going forward. A backfill would need the store, which this library only writes to. Noted in tasks as a non-goal, not silently skipped. |

## Test Strategy

Pure functions with a large input space, so the tests are table-driven:

1. **Taxonomy** — every spelling in SC-001 maps to one label; each kind and alias round-trips;
   bare kinds keep no occurrence; bad occurrences raise.
2. **Ordinals** — `1st/2nd/3rd/4th/11th/21st/22nd` parse; `11st`, `1th`, `0th` do not.
3. **Builder** — all four keys always present; `None` → `""`; blank → raises; phase canonicalized
   on the way out; pricing mode coerced.
4. **Round trip** (SC-003) — build a block, put it in `os.environ` via `monkeypatch`, call
   `attribution_from_env()`, assert the record fields. This is the story's acceptance criterion
   expressed as one test.
5. **Preservation** (SC-004) — an unrecognized phase survives to the record and is noted.
6. **Isolation** (SC-006) — assert the module's imports are stdlib-only, in the style of the
   existing `test_optional_drivers.py` checks, so a future edit cannot quietly add a dependency.
7. **Regression** — the existing hook suite must stay green; the mapping and blank rules are
   unchanged.

## Non-goals

- Changing the `mado` orchestrator (different repository — see spec).
- A `phase` column in the wire contract; that is a `SCHEMA_VERSION` bump and Pillar 5 work.
- Backfilling or rewriting phase labels already in the store.
- Making the hook validate or reject phases — it cannot fail a session, by contract.
