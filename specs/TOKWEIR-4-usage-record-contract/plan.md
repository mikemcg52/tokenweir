# Implementation Plan: Versioned usage-record contract

**Branch**: `TOKWEIR-4-usage-record-contract` | **Date**: 2026-08-09 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/TOKWEIR-4-usage-record-contract/spec.md`
**Jira**: TOKWEIR-4 (epic TOKWEIR-1)

## Summary

Turn the extraction scaffold's `UsageRecord` skeleton into the project's actual published contract:
keep the existing field set, add an enumerated `PricingMode`, add construction-time validation, add
lossless JSON serialization alongside the existing dict form, and publish a versioned JSON Schema
both as a function and as a checked-in file guarded against drift.

The whole change lands in one module (`src/tokenweir/contract.py`) plus its package export surface,
a new checked-in schema artifact, and tests. Nothing about transport, storage or providers is
touched, which is the point: this is the seam every other TOKWEIR story will build against.

## Technical Context

**Language/Version**: Python 3.11+ (`requires-python = ">=3.11"`; the pod runs 3.12.3)
**Primary Dependencies**: None — the core is dependency-light by contract (ADR-0001 Pillar 2).
Implementation uses only `dataclasses`, `enum`, `json`, `typing` from the standard library.
**Storage**: N/A for this story — persistence is the `Source`/writer story, not the contract.
**Testing**: pytest, via the authoritative command in `/workspace/.mado/project.yaml`
(`/workspace/repo/.venv/bin/pytest`, `CI=true`, pass exit codes `0` and `5`).
**Target Platform**: Library, published to PyPI as `tokenweir`; consumed by the AI Gateway, the
MADO cloud-edge, and external OSS users.
**Project Type**: Single Python library (`src/` layout).
**Performance Goals**: None meaningful at this layer — record construction and (de)serialization are
per-model-call operations, dwarfed by the model call itself. Correctness and fidelity are the goals.
**Constraints**: Zero third-party imports in the core; no provider-specific logic; JSON form is part
of the public contract, so field names and wire values are stable and version-gated.
**Scale/Scope**: One dataclass, one enum, one schema function, one checked-in schema file. ~5 source
files touched.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` is an **unfilled template** — every principle is still a
`[PRINCIPLE_N_NAME]` placeholder — so it imposes no project-specific gates on this story. Recorded
here rather than silently skipped: the gate is vacuous, not passed on the merits.

The binding constraints for this work therefore come from **ADR-0001**, and both are checked:

| ADR-0001 pillar | Applies how | Status |
|---|---|---|
| Pillar 2 — transport-agnostic, dependency-light core | Contract module imports stdlib only; no `pika`, no HTTP client, no serialization library | PASS — enforced by a test asserting the module's third-party import set is empty |
| Pillar 3 — provider-neutral, keyed by model | `model` is an opaque string; no provider enum, prefix, or normalization | PASS — enforced by a test building records for several providers' identifiers |
| Pillar 4 — dual capture modes by auth | `pricing_mode` tags which mode produced the record | PASS — implemented as the `PricingMode` enum |
| Pillar 5 — schema ownership lives here | `SCHEMA_VERSION` + published JSON Schema live in this repo | PASS — schema artifact checked in with a drift test |

## Project Structure

### Documentation (this feature)

```text
specs/TOKWEIR-4-usage-record-contract/
├── spec.md              # Feature specification
├── plan.md              # This file
└── tasks.md             # Task breakdown (/speckit.tasks output)
```

No `research.md` — there is nothing to research; the field set is given by the story and the
existing gateway schema. No `data-model.md` — the data model *is* `contract.py` plus the published
JSON Schema, and duplicating it into a spec document would create a third thing to keep in sync.
No `contracts/` directory — the machine-readable contract is checked in at `schema/` where consumers
can actually find it, rather than buried under `specs/`.

### Source Code (repository root)

```text
src/tokenweir/
├── __init__.py          # Public export surface — add PricingMode + schema accessor
├── contract.py          # THE change: PricingMode, validation, JSON ser/de, schema function
├── sink.py              # Untouched (emit-side interface)
└── source.py            # Untouched (write-side interface)

schema/
└── usage-record.v1.json # NEW — published JSON Schema for non-Python consumers

tests/
├── test_contract.py     # Existing scaffold smoke tests — extended, intent preserved
├── test_pricing_mode.py # NEW — PricingMode enumeration + normalization + rejection
├── test_serialization.py# NEW — JSON/dict round-trip, forward compat, required-field rejection
└── test_schema.py       # NEW — published schema shape + checked-in file drift guard
```

**Structure Decision**: Single-project `src/` layout, already established by the scaffold. Tests are
split by concern rather than piled into `test_contract.py` so a reviewer can see which acceptance
clause each file answers; `test_contract.py` keeps its existing smoke tests so the extraction
baseline stays visibly green.

The new `schema/` directory sits at the repository root, not under `src/`, because it is a published
artifact for *other languages* — burying it inside the Python package would make it awkward to fetch
for exactly the consumers it exists to serve.

## Design decisions

Decisions worth stating because a reviewer would otherwise have to reverse-engineer them:

1. **Validation in `__post_init__`, raising `ValueError`.** A record with a blank `app_id` or a
   negative token count is unattributable garbage; emitting it silently is worse than failing. This
   deliberately does **not** conflict with ADR-0001's off-critical-path guarantee, which constrains
   `Sink.emit` (the emit path), not record construction. `Sink.emit` still must never raise, and this
   story does not change that.

2. **`bool` explicitly rejected as a token count.** Python's `bool` is a subclass of `int`, so a
   naive `isinstance(x, int)` check accepts `True` as a token count of 1. That is a silent
   data-corruption path in a metering library, so it is closed explicitly.

3. **`pricing_mode` is a `str`-valued enum accepting either form on the way in.** Producers in other
   codebases will naturally hand over the wire string; consumers want a typed value. Normalizing at
   the boundary means there is exactly one representation inside the process and exactly one on the
   wire.

4. **`from_dict` preserves the payload's `schema_version`.** The scaffold already behaved this way
   incidentally (it is a known field); the plan makes it explicit and tested, because a consumer that
   cannot tell what version it received cannot make a compatibility decision.

5. **The JSON Schema is generated by a function, and the checked-in file is asserted to match.**
   Hand-maintaining a parallel JSON file is how schema drift happens. Generating it and testing the
   artifact against the generator gives non-Python consumers a stable file without a second source of
   truth.

6. **`SCHEMA_VERSION` stays 1.** The scaffold declared version 1 with this same field set and has no
   pinned external consumers; completing the contract at version 1 is correct. Bumping would falsely
   imply a wire-incompatible change that consumers must migrate for.

7. **The published schema pins `schema_version` with `const`, and is therefore deliberately stricter
   than the Python type.** `from_dict` accepts and preserves a newer version (FR-007) so a Python
   consumer can decide for itself what to do; `usage-record.v1.json` refuses it. This is not a
   contradiction — the file *is* the description of version 1 records, and a v2 record is described
   by `usage-record.v2.json`. Relaxing it to `minimum: 1` was considered and rejected: it would make
   the v1 document silently accept a v3 record whose fields it does not describe, which is a worse
   answer to the only question a validator is asked. The spec's deferral of a rejection *policy*
   (Assumptions) still holds — the schema states what a v1 record is; it does not tell a consumer
   what to do with a v2 one. Pinned by a test so the divergence stays a decision, not an accident.

8. **The schema's `pattern` mirrors the library's "blank" rule.** `_validate_required_str` treats
   whitespace-only as blank, so the required string properties carry a `pattern` alongside
   `minLength: 1`. Without it a non-Python producer could follow the published schema and still emit
   `{"app_id": "  "}` — a record this library refuses. A test asserts the two rules agree value by
   value, so they cannot drift apart. *(Superseded in part by decision 13: the pattern started as
   `\S` and is now an explicit shared character class, because `\S` does not mean the same thing to
   Python and to ECMA-262. The requirement here is unchanged; only the expression is.)*

9. **`jsonschema` is a `[dev]` extra, never a runtime dependency.** Validating payloads against the
   published schema needs an engine; the core must not grow one (Pillar 2). The validation tests
   `importorskip` it, so they run on a dev install and skip under the MADO stream plan's
   `pip install -e . pytest`. The structural and pattern-agreement tests are dependency-free and
   always run, so the alignment is guarded even where the engine is absent.

10. **`REQUIRED_FIELDS` and `TOKEN_COUNT_FIELDS` are exported** beyond the minimum the task list
    named. They are the single source of truth that construction validation, deserialization, the
    published schema and the tests all read from, and downstream TOKWEIR stories (the writer, the
    emitter) will need the same lists. Both are tuples, so exporting them commits to names rather
    than to mutable state. `NON_BLANK_PATTERN` is deliberately **not** exported — it is an
    implementation detail of the schema generator, and a published library should not commit to a
    regex as public API.

11. **Integral JSON numbers are normalized on the way in (`100.0` → `100`).** JSON has one number
    type; JSON Schema defines `"type": "integer"` as any number with zero fractional part, and a
    JavaScript producer has no other way to write an integer. Without this, a payload that validates
    against the published schema would be refused by the library — the exact divergence FR-019
    forbids, and the one the whitespace fix missed because it generalized from an example rather
    than the rule. Only exactly-integral floats convert; `100.5`, `NaN` and `Infinity` still fail,
    so nothing is silently truncated. Python construction stays strict, because there `100.0` is a
    caller-side type error rather than a wire encoding.

12. **The record is frozen.** Validation runs once at construction, so a mutable record could be
    edited into an invalid state and then serialized (a reviewer demonstrated exactly that). A
    metering record is a value, and immutability also makes it safe to hand to a buffering,
    fire-and-forget `Sink` without the caller and the emit path sharing state. Callers build
    variants with `dataclasses.replace`, which re-runs validation. There are no pinned consumers at
    version `0.0.0`, so this is the cheapest moment to make the commitment.

13. **Blankness is defined by an explicit character class, not `\S` — and the class is the *union*
    of Python's and ECMA-262's whitespace sets.** JSON Schema `pattern` is ECMA-262, whose
    whitespace set differs from Python's: Python treats U+001C–U+001F and U+0085 as whitespace,
    ECMA-262 does not; ECMA-262 treats U+FEFF as whitespace, Python does not. With `\S` on both
    sides the two engines disagree, so `str.strip()` and `\S` were replaced by one class spelled out
    in escapes both engines parse identically.

    Three options existed once the class was explicit: ECMA's set, Python's set, or the union.
    ECMA's set alone was tried first and rejected on review — it *loosened* the library, making
    `app_id="\x1c"` newly valid. Python's set alone cannot be expressed to an ECMA validator's
    satisfaction without the same explicit spelling, and would leave U+FEFF disagreeing. The union
    is the only choice that both makes every engine agree and leaves the library no less strict than
    it was before the change; U+FEFF additionally becomes blank, which is a tightening. Verified
    across all 1,114,112 code points and, by a reviewer, against V8. Tests pin each divergent point.

14. **Agreement is guarded in both directions, not just schema → library.** FR-019 stops the schema
    accepting what the library refuses. The reverse — the library emitting what its own schema
    rejects — turned out to be the live gap: `workload`, `parent_request_id`, `queue` and `ts` were
    typed `Optional[str]` but never validated, so `workload=123` constructed happily and serialized
    to a document a Go consumer would reject. They are now validated (FR-025). A parametrized test
    asserts records the library accepts serialize to schema-valid JSON; a 4,000-case randomized
    sweep in each direction found zero disagreements.

15. **`.specify/feature.json` is committed on purpose.** The branch follows MADO's
    `<STORY-KEY>-<desc>` convention, which speckit's scripts do not recognize (they expect
    `NNN-name`), so without this file `setup-plan.sh` and `check-prerequisites.sh` resolve the wrong
    feature directory or fail their branch check. It is one line, it is per-branch, and it is what
    makes the speckit spine reproducible on this branch — worth the churn.

16. **`jsonschema` is a `[dev]` extra and its tests skip in CI — accepted, with compensation on both
    sides of the agreement.** See the spec's Assumptions. Dependency-free tests carry the substance
    in the environment that actually gates the merge:

    - schema → library (FR-019, FR-024): the numeric and blank-rule agreement tests, plus the
      pattern pinned to its exact literal;
    - library → schema (FR-025): every declared constraint pinned property by property, and a
      `null`-permitted-exactly-where-`None`-is check. Verified by mutation under the authoritative
      install — dropping `null` from `latency_ms` or `pricing_mode`, or adding a `maxLength` to the
      required strings, each fails there even with the artifact regenerated.

    What no dependency-free test can do is validate a real payload end-to-end; that half stays
    dev-install-only. Fully closing it needs the MADO registry entry to install dev extras — outside
    this repository.

## Phasing

| Phase | What | Why this order |
|---|---|---|
| 1 | `PricingMode` enum + normalization | Everything else references it |
| 2 | Field validation in `__post_init__` | Establishes the record's invariants before serialization relies on them |
| 3 | JSON serialization (`to_json`/`from_json`) + strict `from_dict` | The named acceptance criterion; depends on 1 and 2 |
| 4 | Published JSON Schema function + checked-in artifact | Describes the finished shape, so it goes last |
| 5 | Docs (`README.md`, module docstrings) | Reflects the settled surface |

## Risks

- **Tightening `from_dict` is a behaviour change** for the scaffold's existing callers: it previously
  accepted any partial payload and would raise a bare `TypeError` on missing required arguments.
  There are no external consumers yet (version `0.0.0`, unpublished), so this is the cheapest moment
  to make it strict. Mitigated by keeping the existing tests' intent intact.
- **Over-abstraction on the schema surface** — ADR-0001 explicitly names "premature over-abstraction
  on the transport boundary" as a risk of the extraction. Guarded by scope: no adapter, registry, or
  plugin surface is introduced here, only the record and its schema.
