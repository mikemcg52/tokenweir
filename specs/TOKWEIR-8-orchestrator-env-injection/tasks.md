# Tasks: Orchestrator env injection for phase/issue context

**Feature**: `TOKWEIR-8-orchestrator-env-injection`
**Spec**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md)

`[P]` marks tasks that touch disjoint files and could run in parallel. The chain here is mostly
sequential — the hook and the docs both depend on the new module existing.

## Phase 1 — The contract module

- [x] **T001** Create `src/tokenweir/orchestrator.py` with the module docstring: what the
  orchestrator injects, why attribution comes from it and not the model (ADR-0001 Pillar 4), where
  the lifecycle vocabulary comes from (`mado-phase --phase`), and the stdlib-only rule (FR-014).
- [x] **T002** Define `PhaseKind` (`implementation`, `review`, `fix`) as a `str` Enum with a
  tolerant `coerce`, mirroring `PricingMode` (FR-001).
- [x] **T003** Implement `phase_label(kind, occurrence=None)` — canonical `kind` / `kind-N`,
  raising on a non-positive or non-integral occurrence and on an unknown kind (FR-002, FR-007).
- [x] **T004** Implement the parser behind `normalize_phase`: separator folding (`_`, `#`,
  whitespace runs), leading English ordinals with suffix validation, trailing bare numbers, and
  the `bug fix`/`bugfix`/`implement` aliases (FR-003).
- [x] **T005** Implement `normalize_phase(value)` — `None`/blank → `None` (FR-006); recognized →
  canonical label; unrecognized → whitespace-collapsed original (FR-005) — and
  `is_canonical_phase(value)` so a caller can tell the two apart.
- [x] **T006** Define `ATTRIBUTION_ENV` as the single spelling of the four variable names
  (FR-008), and implement `attribution_env(...)`: always four keys, `None` → `""`, blank or
  otherwise unusable → raise, phase canonicalized, pricing mode coerced (FR-009 – FR-013).
- [x] **T007** Export the new public names from `src/tokenweir/__init__.py` `__all__`.

## Phase 2 — The consumer side

- [x] **T008** In `src/tokenweir/claude_code.py`, route `MADO_PHASE` through `normalize_phase` in
  `attribution_from_env()` and note a diagnostic for a non-canonical label, leaving every other
  field, the mapping, and the unset/blank rule untouched (FR-015 – FR-017). Update the function's
  docstring to point at the taxonomy rather than restating it.

## Phase 3 — Tests

- [x] **T009** [P] `tests/test_orchestrator.py`: taxonomy and normalization tables — the SC-001
  spellings collapse to one label, aliases, bare kinds keep no occurrence, blank/None → `None`,
  unrecognized preserved, ordinal edge cases (`11th` yes, `11st`/`1th`/`0th` no).
- [x] **T010** [P] `tests/test_orchestrator.py`: `phase_label` and `attribution_env` producer
  rules — four keys always, `None` → `""`, blank raises, bad occurrence raises, unknown pricing
  mode raises, phase canonicalized on the way out.
- [x] **T011** [P] `tests/test_orchestrator.py`: SC-006 — the module imports nothing outside the
  standard library and does not import `tokenweir.claude_code`.
- [x] **T012** Round-trip test (SC-003, the story's acceptance): build a block for a given issue
  key and phase, install it in the environment, call `attribution_from_env()`, and assert
  `workload` and `queue`. Include the "2nd fix" case from User Story 3.
- [x] **T013** Hook tests: a non-canonical `MADO_PHASE` reaches the record canonical; an
  unrecognized one reaches the record intact and is noted (SC-004); existing hook expectations
  unchanged.

## Phase 4 — Documentation

- [x] **T014** README: extend the attribution section with the taxonomy (kinds, label grammar,
  accepted spellings), what the orchestrator must export and when — every variable, every
  iteration, before the turn — and state that the orchestrator-side change lives in the `mado`
  repo and is not delivered here (FR-018 – FR-020).

## Phase 5 — Verification

- [x] **T015** Run the authoritative suite: `CI=true .venv/bin/pytest` from `/workspace/repo`, per
  `/workspace/.mado/project.yaml`. Baseline before this change: 1007 passed, 141 skipped.

## Added after review

- [x] **T016** (review 1) Reject a kind with an impossible occurrence at the producer,
  where FR-012 always said it belonged; the parser now reports that case as its own
  outcome so both ends read one answer.
- [x] **T017** (review 1) Route the hook's four variable names through `ATTRIBUTION_ENV`,
  and assert by AST that it spells none of them itself — SC-005 was documented but not
  held.
- [x] **T018** (review 2) Extend the same guard to the signed spellings (`review -1`,
  `fix -0`), which reached neither the producer's check nor the consumer's parse.
- [x] **T019** (review 2) `MappingProxyType` on `ATTRIBUTION_ENV`; document and test the
  whitespace strip on the issue key and stream id.

## Explicit non-goals

Not tasks, listed so their absence is a decision rather than an oversight:

- The `mado`-repo orchestrator change that actually exports the block.
- A first-class `phase` field in the wire contract (`SCHEMA_VERSION` bump, Pillar 5).
- Backfilling phase labels already written to the store.
