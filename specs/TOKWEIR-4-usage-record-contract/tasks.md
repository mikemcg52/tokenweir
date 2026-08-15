---
description: "Task list for TOKWEIR-4 — versioned usage-record contract"
---

# Tasks: Versioned usage-record contract

**Input**: Design documents from `/specs/TOKWEIR-4-usage-record-contract/`
**Prerequisites**: plan.md, spec.md
**Jira**: TOKWEIR-4

**Tests**: Included and non-optional. The story's acceptance is stated in terms of behaviour
("a record round-trips serialize/deserialize", "`pricing_mode` distinguishes …"), which is only
demonstrable by tests, and `tokenweir` is a published library whose contract other components pin.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story from spec.md the task serves

## Path Conventions

Single project, `src/` layout: `src/tokenweir/`, `tests/`, `schema/` at repository root.

---

## Phase 1: Foundational (Blocking Prerequisites)

**Purpose**: The enumerated type and the record's invariants — every other phase references them.

- [x] T001 [US3] Add a `PricingMode` string-enum to `src/tokenweir/contract.py` with exactly two
      members whose wire values are `api_metered` and `subscription` (FR-009).
- [x] T002 [US3] Add normalization on `UsageRecord` construction in `src/tokenweir/contract.py`:
      accept a `PricingMode` or its wire string, store the enum member, and raise `ValueError`
      listing the permitted values for anything else; `None` stays valid (FR-010, US3 scenarios 2–4).
- [x] T003 [US1] Add required-field validation to `UsageRecord.__post_init__` in
      `src/tokenweir/contract.py`: `request_id`, `app_id`, `endpoint`, `model`, `status` must be
      non-blank strings (whitespace-only counts as blank), error names the field (FR-002, FR-008).
- [x] T004 [US1] Add numeric validation to `UsageRecord.__post_init__` in
      `src/tokenweir/contract.py`: the four token counts must be non-negative `int`s with `bool`
      explicitly rejected; `latency_ms` must be a non-negative `int` or `None` (FR-012, FR-013).
- [x] T005 [US1] Validate `schema_version` is a positive `int`, and confirm it defaults from
      `SCHEMA_VERSION` when not supplied, in `src/tokenweir/contract.py` (FR-003).

**Checkpoint**: The record type enforces its own invariants; serialization can rely on them.

---

## Phase 2: User Story 2 — JSON round-trip (Priority: P1) 🎯 MVP

**Goal**: Lossless serialization to and from JSON and dicts, with forward compatibility.

**Independent Test**: Serialize a fully-populated record to JSON, deserialize it, assert equality.

- [x] T006 [US2] Make `to_dict()` in `src/tokenweir/contract.py` emit `pricing_mode` as its plain
      wire string (or `null` when unset), so the dict form is JSON-ready (FR-011).
- [x] T007 [US2] Add `to_json()` to `UsageRecord` in `src/tokenweir/contract.py`, serializing the
      dict form to a JSON string (FR-004).
- [x] T008 [US2] Add `from_json()` classmethod to `UsageRecord` in `src/tokenweir/contract.py`,
      parsing a JSON string (or bytes) and delegating to `from_dict` (FR-004).
- [x] T009 [US2] Make `from_dict()` in `src/tokenweir/contract.py` strict about required fields —
      raise `ValueError` naming any missing required field instead of a bare `TypeError` — while
      keeping the existing behaviour of ignoring unknown keys (FR-006, FR-008).
- [x] T010 [US2] Ensure `from_dict()` preserves the payload's `schema_version` rather than
      substituting the reading library's own (FR-007).
- [x] T011 [P] [US2] Write `tests/test_serialization.py`: full-fidelity JSON round-trip, dict
      round-trip, unset-optionals round-trip, unknown-field tolerance, `schema_version` preservation,
      missing-required-field rejection (SC-001, SC-003).

**Checkpoint**: The story's named round-trip acceptance criterion is demonstrable.

---

## Phase 3: User Story 3 — pricing_mode tests (Priority: P1)

**Goal**: Prove the two modes, and only those two, are the contract.

**Independent Test**: Assert the enumeration, its wire strings, normalization, and rejection.

- [x] T012 [P] [US3] Write `tests/test_pricing_mode.py`: exactly two members with wire values
      `api_metered` and `subscription`; string input normalizes to the enum; unrecognized value
      raises with the permitted values named; `None` is valid; serializes to the wire string
      (SC-002, US3 scenarios 1–5).

---

## Phase 4: User Story 1 — record construction tests (Priority: P1)

**Goal**: Prove the record's invariants and defaults.

- [x] T013 [P] [US1] Write construction tests in `tests/test_contract.py` (extending, not replacing,
      the scaffold's smoke tests): minimal record is valid with token counts defaulting to `0` and
      `schema_version` stamped; blank/whitespace required field rejected with the field named;
      negative and `bool` token counts rejected; negative `latency_ms` rejected (US1 scenarios 1–3).

---

## Phase 5: User Story 4 — published schema (Priority: P2)

**Goal**: A machine-readable, versioned schema for non-Python consumers, guarded against drift.

- [x] T014 [US4] Add `usage_record_json_schema()` to `src/tokenweir/contract.py` returning a JSON
      Schema dict describing every field with its JSON type, the required-field list, the enumerated
      `pricing_mode` values, and the contract version (FR-014).
- [x] T015 [US4] Generate and check in `schema/usage-record.v1.json` from that function (FR-015).
- [x] T016 [P] [US4] Write `tests/test_schema.py`: the generated schema's properties match the
      record's fields exactly (no field added to one without the other), required list is correct,
      `pricing_mode` enum is correct, and the checked-in file byte-matches the generated schema —
      the drift guard (SC-004).

---

## Phase 6: User Story 5 — provider neutrality (Priority: P2)

**Goal**: Prove no Anthropic-shaped assumption has crept into the core.

- [x] T017 [P] [US5] Add provider-neutrality tests to `tests/test_contract.py`: records for a Claude
      model, a local Llama/Ollama identifier and an OpenAI-style identifier are all valid and store
      `model` verbatim (SC-005, US5 scenario 1).
- [x] T018 [P] [US5] Add a dependency-hygiene test asserting `src/tokenweir/contract.py` imports only
      standard-library modules (FR-017, SC-005, US5 scenario 2).

---

## Phase 7: Public surface and docs

- [x] T019 Export `PricingMode` and `usage_record_json_schema` from `src/tokenweir/__init__.py` and
      add them to `__all__`.
- [x] T020 Update `src/tokenweir/contract.py` module docstring to state the compatibility rules:
      unknown fields ignored, payload `schema_version` preserved, `SCHEMA_VERSION` bumped in lockstep
      with any field change (FR-018).
- [x] T021 Update `README.md` to document the contract — the two pricing modes, the JSON form, and
      the published `schema/usage-record.v1.json` for non-Python consumers.

---

## Phase 8: Verification

- [x] T022 Run the authoritative test command from `/workspace/.mado/project.yaml`
      (`CI=true /workspace/repo/.venv/bin/pytest`, passing exit codes `0` and `5`) and confirm green,
      including the five pre-existing scaffold tests (SC-006).
- [x] T023 Run `ruff check .` — the repo configures ruff in `pyproject.toml`, so the new code must
      satisfy it.

---

## Dependencies

- Phase 1 (T001–T005) blocks everything: serialization, schema and tests all depend on the
  normalized `pricing_mode` and the record's invariants.
- T006 → T007 (the JSON form is the dict form encoded).
- T009/T010 depend on T003 (required-field validation is what `from_dict` reuses).
- T014 depends on T001–T005 (the schema describes the finished shape).
- T015 depends on T014; T016 depends on T015.
- T019–T021 depend on the surface being settled (T014).
- Phase 8 depends on everything.

## Parallel opportunities

T011, T012, T013, T016, T017, T018 are all separate test files or independent test functions and can
be written in parallel once their subject phases land.


---

## Phase 9: Review-driven fixes (added during the review→fix loop)

Filed after the fact so `tasks.md` reflects what the branch actually contains.

- [x] T024 [US4] Align the published schema with the library on blank strings: shared explicit
      ECMA-262 whitespace class in `src/tokenweir/contract.py`, replacing `str.strip()` and `\S`
      (FR-019, FR-024).
- [x] T025 [US2] Normalize integral JSON numbers (`100.0` -> `100`) on deserialization in
      `src/tokenweir/contract.py`, keeping non-integral values rejected (FR-022).
- [x] T026 [US1] Freeze `UsageRecord` so a validated record cannot be mutated into an invalid one
      (FR-023).
- [x] T027 [US4] Build the schema's nested fragments per call so the returned document shares no
      state with the module or with itself (FR-020).
- [x] T028 [US2] Guard `from_dict` against a non-mapping and `from_json` against a non-string
      payload, both raising `ValueError` (FR-021).
- [x] T029 [US4] Require `schema_version` in the published schema's `required` list, and document
      that the schema may be stricter than the reader but never looser.
- [x] T030 Document the two construction error types (`TypeError` on omission, `ValueError` on an
      invalid value) in the module docstring and `README.md`.
- [x] T031 Stop exporting `NON_BLANK_PATTERN` from the package's public surface.
- [x] T032 Remove the stale duplicate `NON_BLANK_PATTERN = r"\S"` that shadowed the shared pattern
      and made the ECMA-aligned class dead code.

- [x] T033 [US4] Validate `workload`, `parent_request_id`, `queue` and `ts` as optional strings so
      the library cannot emit a record its own published schema rejects (FR-025), with a test that
      records the library accepts serialize to schema-valid JSON.
- [x] T034 [US4] Widen the fresh-document guard to the schema's list-valued members (`required`,
      `pricing_mode.enum`) and to numeric fragments, which the first version did not cover (FR-020).
- [x] T035 [US4] Make the jsonschema-backed happy path validate a fully-populated payload as well as
      a minimal one, so the optional fields are actually exercised.
- [x] T036 [US1] Switch the shared blank-character class from ECMA-262's whitespace set to the
      union with Python's, so the shared rule never loosened the library (FR-024).
- [x] T037 Rename `NON_BLANK_PATTERN` to `_NON_BLANK_PATTERN` — a regex is an implementation
      detail, not public API for a library others will pin.
- [x] T038 Add a field-set fingerprint test tying `SCHEMA_VERSION` to the record's fields, so adding
      a field forces a deliberate version decision (FR-018).
- [x] T039 Assert the missing field is named in the omitted-required-argument test (US1 scenario 2).

- [x] T040 [US1] Pin `_NON_BLANK_PATTERN` to its exact literal and assert it contains no regex
      shorthand class, so swapping the explicit class back to `\\s` cannot pass silently (FR-024).
- [x] T041 [US1] Add a node-gated test evaluating the published pattern under real ECMA-262
      semantics and comparing it to Python's and to the library's own accept/reject set (FR-024).
- [x] T042 [US5] Add mixed-case and whitespace-bearing identifiers to the verbatim-storage tests for
      `model`, `app_id`, `endpoint`, `status` and `workload`, so a stray `.lower()`/`.strip()` in
      `__post_init__` fails the suite (FR-016).
- [x] T043 Annotate `from_dict` as taking a `Mapping[str, Any]`, matching its runtime guard.
- [x] T044 Document the >2**53 float precision caveat on `_coerce_wire_integers`, and record the
      "optional strings are type-checked but not blank-checked" choice in spec.md's Assumptions.
- [x] T045 Amend plan.md design decision 8, which still described the superseded `\\S` pattern.
- [x] T046 Cover `bytearray` as well as `bytes` in the `from_json` acceptance test.

- [x] T047 [US4] Pin every declared schema constraint property by property, and assert `null` is
      permitted exactly where the library permits `None`, so an FR-025 schema-side regression fails
      under the authoritative install rather than only on a dev install.
- [x] T048 [US4] Assert the required-string properties carry no extra constraints (a stray
      `maxLength`/`format` would reject values the library accepts).
- [x] T049 Amend the "compensated" claim in spec.md and plan.md to name FR-025 and to say plainly
      which half of it CI cannot cover; note that the node-gated ECMA check is opportunistic.
- [x] T050 Soften the spec's claim that the story's field list matches the scaffold's — `ts` is a
      scaffold field the story does not name.
- [x] T051 Document in `README.md` that optional strings are not blank-checked, that
      `schema_version` must be positive, and that strings are never normalized.
