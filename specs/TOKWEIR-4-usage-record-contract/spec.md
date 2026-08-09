# Feature Specification: Versioned usage-record contract

**Feature Branch**: `TOKWEIR-4-usage-record-contract`
**Created**: 2026-08-09
**Status**: Draft
**Jira**: TOKWEIR-4 (parent epic TOKWEIR-1) — "Define the versioned usage-record contract"
**Input**: Define the serializable, versioned usage-record contract: `schema_version`, `request_id`,
`parent_request_id`, `app_id`, `workload`, `endpoint`, `model`, `queue`, `status`, `latency_ms`,
token counts, `pricing_mode`. Provider-neutral and keyed by model (not Anthropic-locked). JSON is
part of the contract; transport is not.

**Acceptance (from the story):** contract published as a typed, versioned schema; a record
round-trips serialize/deserialize; `pricing_mode` distinguishes `api_metered` vs `subscription`.

## Context

`tokenweir` already carries a skeleton `UsageRecord` dataclass (from the TOKWEIR-1 extraction
scaffold) with the right field names, plus `to_dict`/`from_dict`. This story turns that skeleton
into an actual **contract**: a typed, versioned, JSON-serializable unit with an enumerated
`pricing_mode`, a published machine-readable schema, and documented compatibility rules.

Two design pillars from ADR-0001 bound the work:

- **Pillar 2 — transport-agnostic core.** The contract defines the record and its JSON form and
  nothing about the wire. No transport dependency may enter the core.
- **Pillar 3 — provider-neutral core.** The record is keyed by `model` as an opaque string. No
  Anthropic-specific validation, enumeration, or normalization belongs here.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A producer emits a well-formed, versioned record (Priority: P1)

An instrumented caller (the AI Gateway, the MADO cloud-edge, or an external OSS user) builds a
`UsageRecord` describing one metered unit of work and hands it to a `Sink`. The record carries a
`schema_version` so every downstream consumer knows exactly which contract it is reading.

**Why this priority**: Nothing else in the pipeline exists without a record that producers can
construct correctly and consumers can identify the version of. This is the contract itself.

**Independent Test**: Construct a record with only the required fields, assert the optional fields
take documented defaults and `schema_version` is stamped automatically. Delivers value on its own —
it is the published type other TOKWEIR stories build against.

**Acceptance Scenarios**:

1. **Given** the required identity fields (`request_id`, `app_id`, `endpoint`, `model`, `status`),
   **When** a record is constructed with nothing else, **Then** it is valid, all four token counts
   default to `0`, and `schema_version` equals the library's current `SCHEMA_VERSION`.
2. **Given** a record constructed with a blank or missing required field, **When** construction is
   attempted, **Then** it fails loudly with a clear error naming the offending field rather than
   producing a silently unattributable record.
3. **Given** a negative or non-integer token count, **When** construction is attempted, **Then** it
   fails with a clear error — token counts are the metered quantity and a negative one is nonsense.

---

### User Story 2 - A record round-trips through JSON without loss (Priority: P1)

A record is serialized to JSON by a producer, crosses some boundary the contract deliberately does
not describe (AMQP, HTTP, a file, an in-process call), and is rebuilt by a consumer into a record
equal to the original.

**Why this priority**: The story names JSON as part of the contract, and lossless round-trip is the
named acceptance criterion. A contract whose serialization loses or mangles a field is not a
contract.

**Independent Test**: Serialize a fully-populated record to a JSON string, deserialize it, and
assert equality with the original — no transport, no store, no adapters involved.

**Acceptance Scenarios**:

1. **Given** a fully-populated record (every field set, including all optional attribution fields),
   **When** it is serialized to JSON and deserialized, **Then** the result equals the original,
   field for field, with `pricing_mode` preserved.
2. **Given** a record with optional fields left unset, **When** it round-trips through JSON,
   **Then** the unset fields survive as `null` and rebuild as the same unset values.
3. **Given** a JSON payload produced by a *newer* library version carrying fields this version does
   not know, **When** it is deserialized, **Then** the unknown fields are ignored and the known
   fields are read correctly — forward compatibility, so a schema addition never breaks an older
   consumer.
4. **Given** a JSON payload, **When** it is deserialized, **Then** the `schema_version` in the
   payload is preserved on the resulting record rather than being overwritten with the reading
   library's own version — a consumer must be able to tell what it actually received.

---

### User Story 3 - `pricing_mode` distinguishes API-metered from subscription usage (Priority: P1)

A report-time consumer needs to know whether a record can be costed against a rate card
(`api_metered`) or represents flat-rate subscription consumption (`subscription`) where there is no
per-call dollar. Both capture modes feed the same contract, tagged by this field (ADR-0001 Pillar 4).

**Why this priority**: Named explicitly in the story's acceptance. Without an enumerated value,
`pricing_mode` is a free-text field that every consumer must defensively re-interpret, and typos
silently produce uncostable records.

**Independent Test**: Assert the two modes exist as an enumerated type, that they serialize to the
exact strings `api_metered` and `subscription`, and that an unrecognized value is rejected.

**Acceptance Scenarios**:

1. **Given** the contract, **When** the pricing modes are enumerated, **Then** exactly
   `api_metered` and `subscription` are defined, with those exact wire strings.
2. **Given** a record built with `pricing_mode` supplied as the *string* `"api_metered"`, **When**
   it is constructed, **Then** the value is normalized to the enumerated type, so producers may
   pass either form and consumers always read one.
3. **Given** a record built with an unrecognized `pricing_mode` such as `"free_tier"`, **When**
   construction is attempted, **Then** it fails with an error listing the permitted values.
4. **Given** a record with `pricing_mode` unset, **When** it is constructed, **Then** it is valid
   and the field is `None` — the field is optional, since a producer that does not know its billing
   mode must still be able to emit raw counts.
5. **Given** a record whose `pricing_mode` is set, **When** it is serialized, **Then** the JSON
   carries the plain wire string, not a language-specific enum representation.

---

### User Story 4 - A non-Python consumer reads the published schema (Priority: P2)

A consumer outside Python — or a reviewer checking a change for a breaking field edit — reads a
machine-readable JSON Schema describing the record, versioned in lockstep with `SCHEMA_VERSION`.

**Why this priority**: "Published as a typed, versioned schema" is the story's first acceptance
clause, and `tokenweir` is aimed at open-source consumers who will not all be Python. It is P2 only
because the Python type is what unblocks the other TOKWEIR stories today.

**Independent Test**: Read the published schema, assert it describes exactly the record's fields and
declares the current version — no other component involved.

**Acceptance Scenarios**:

1. **Given** the library, **When** the published JSON Schema is requested, **Then** it lists every
   field of the record with its JSON type, marks the required fields as required, and states the
   contract version.
2. **Given** the schema is also checked into the repository as a file for non-Python consumers,
   **When** the record type changes without the file being regenerated, **Then** the test suite
   fails — the checked-in artifact cannot silently drift from the code.
3. **Given** the published schema, **When** `pricing_mode` is inspected, **Then** its permitted
   values are enumerated as `api_metered` and `subscription`.

---

### User Story 5 - The contract stays provider-neutral (Priority: P2)

A record describes a call to a local Ollama/Jetson model exactly as naturally as a call to Claude.
The gateway already meters both, so provider-neutrality is existing behaviour to preserve, not an
aspiration.

**Why this priority**: A regression here (an Anthropic-shaped validation sneaking in) would be
cheap to introduce and expensive to unwind after external consumers pin the version.

**Independent Test**: Build records for several unrelated providers' model identifiers and assert
all are accepted identically.

**Acceptance Scenarios**:

1. **Given** model identifiers from different providers (e.g. a Claude model, a local Llama model,
   an OpenAI-style identifier), **When** records are built for each, **Then** all are valid and
   `model` is stored verbatim with no normalization, prefixing, or provider inference.
2. **Given** the core contract module, **When** its imports are inspected, **Then** it depends only
   on the Python standard library — no transport and no provider SDK.

### Edge Cases

- **Unknown fields on the wire** (newer producer → older consumer): ignored on deserialization; the
  known fields still read correctly.
- **Missing required fields on the wire** (malformed or truncated payload): rejected with an error
  naming the field, rather than materializing a record with empty identity.
- **A `schema_version` from a newer contract**: preserved as received and readable by the consumer,
  which may then decide what to do. This story does not define a rejection policy for future
  versions — see Assumptions.
- **Whitespace-only required field**: treated as blank, i.e. rejected. `"  "` is not an app id.
  "Whitespace" is a single explicit character class shared by the library and the published schema —
  the union of Python's and ECMA-262's sets — rather than each side using its own shorthand, which
  would let the two disagree at the handful of code points where those sets differ (FR-024).
- **`pricing_mode` supplied as the enum vs. as its wire string**: both accepted, both normalize to
  the same stored value.
- **A very large token count**: accepted; Python integers are unbounded and the contract sets no
  ceiling.
- **Serializing a record built by a future version with extra attributes**: out of scope — this
  library only ever serializes records it constructed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The contract MUST define a typed usage record carrying exactly these fields:
  `schema_version`, `request_id`, `parent_request_id`, `app_id`, `workload`, `endpoint`, `model`,
  `queue`, `status`, `latency_ms`, `input_tokens`, `output_tokens`,
  `cache_creation_input_tokens`, `cache_read_input_tokens`, `pricing_mode`, and `ts`.
- **FR-002**: `request_id`, `app_id`, `endpoint`, `model` and `status` MUST be required and
  non-blank. All other fields MUST be optional, with the four token counts defaulting to `0`.
- **FR-003**: Every record MUST carry a `schema_version`, stamped automatically from the library's
  current `SCHEMA_VERSION` when the producer does not supply one. It MUST be a positive integer —
  a version of `0` or below identifies no contract and MUST be rejected.
- **FR-004**: The contract MUST serialize to and deserialize from JSON losslessly for every field,
  such that a deserialized record equals the record that was serialized.
- **FR-005**: The contract MUST also serialize to and deserialize from a plain dictionary, so
  callers that already hold a JSON-decoded payload need not re-encode it.
- **FR-006**: Deserialization MUST ignore fields it does not recognize (forward compatibility).
- **FR-007**: Deserialization MUST preserve the `schema_version` present in the payload rather than
  substituting the reading library's own version.
- **FR-008**: Deserialization MUST reject a payload missing any required field, with an error
  naming the field.
- **FR-009**: `pricing_mode` MUST be an enumerated type with exactly two members whose wire values
  are the strings `api_metered` and `subscription`.
- **FR-010**: `pricing_mode` MUST accept either the enumerated member or its wire string on
  construction and normalize to the enumerated member; an unrecognized value MUST be rejected with
  an error listing the permitted values.
- **FR-011**: `pricing_mode` MUST serialize to its plain wire string, and MUST serialize to `null`
  when unset.
- **FR-012**: Token counts MUST be rejected if negative or not integers. `bool` MUST NOT be
  accepted as a token count.
- **FR-013**: `latency_ms` MUST be rejected if negative or not an integer, and MAY be unset.
- **FR-014**: The contract MUST publish a machine-readable JSON Schema describing the record,
  including its field types, its required fields, the enumerated `pricing_mode` values, and the
  contract version.
- **FR-015**: The published JSON Schema MUST also exist as a checked-in file for non-Python
  consumers, and the test suite MUST fail if that file drifts from the code-generated schema.
- **FR-019**: The published JSON Schema's constraints MUST agree with the library's own validation,
  so a producer that follows the schema cannot emit a record this library refuses. In particular a
  whitespace-only required field MUST be invalid in both.
- **FR-020**: Generating the published JSON Schema MUST return an independent document on every
  call, sharing no mutable state with the library or with a previously returned document, so a
  consumer may annotate the result safely.
- **FR-021**: Deserialization MUST reject a payload that is not a mapping (or, for JSON, not a
  string/bytes), with a `ValueError` rather than a lower-level attribute or type error, so a caller
  at the wire boundary catches one type.
- **FR-022**: Deserialization MUST accept an integral JSON number where an integer field is
  expected — JSON has a single number type, so `100.0` on the wire denotes the integer `100`, which
  is exactly what the published schema's `"type": "integer"` permits — and MUST normalize it to an
  integer. A non-integral value (`100.5`, `NaN`, `Infinity`) MUST still be rejected, never
  truncated. Direct Python construction remains strict, since there `100.0` is a caller type error.
- **FR-023**: The record MUST be immutable after construction, so a validated record cannot be
  mutated into one that violates its own contract and then serialized.
- **FR-024**: The published JSON Schema's notion of a blank string MUST hold under ECMA-262 regular
  expression semantics (which is what JSON Schema `pattern` specifies), not merely under Python's,
  so that a JavaScript or Go validator and this library agree on which values are blank. The shared
  rule MUST NOT make the library less strict than treating any Python-whitespace-only value as
  blank.
- **FR-025**: The agreement MUST hold in **both** directions. As well as never refusing a record
  that conforms to the published schema (FR-019), the library MUST NOT construct a record that
  serializes to JSON its own published schema rejects — a consumer in another language validating
  incoming records would otherwise reject what the reference producer emits. In particular
  `workload`, `parent_request_id`, `queue` and `ts` MUST be a string or absent, matching the
  `["string", "null"]` the schema declares for them.
- **FR-016**: `model` MUST be stored verbatim as an opaque identifier, with no provider-specific
  validation, normalization, or inference.
- **FR-017**: The contract module MUST depend only on the Python standard library.
- **FR-018**: `SCHEMA_VERSION` MUST be documented as changing in lockstep with any field change to
  the record.

### Key Entities

- **UsageRecord**: One metered unit of work — a single model call or iteration. Carries identity
  (`request_id`, `app_id`, `endpoint`, `model`, `status`), attribution (`parent_request_id`,
  `workload`, `queue`), the raw token counts, and operational metadata (`latency_ms`,
  `pricing_mode`, `ts`), plus the `schema_version` that identifies which contract it obeys. Raw
  counts are stored; cost is derived at report time and never lives on the record.
- **PricingMode**: The billing mode a record was captured under — `api_metered` (interception via
  the Anthropic-compatible edge; costable against a rate card) or `subscription` (flat-rate Max
  capture via the Claude Code `Stop` hook; no per-call dollar exists).
- **Usage-record JSON Schema**: The published, versioned, language-neutral description of the
  record, for consumers that are not Python and for review of breaking changes.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A fully-populated record round-trips through JSON and through a dictionary with
  100% field fidelity — the rebuilt record is equal to the original in both directions.
- **SC-002**: The two pricing modes, and only those two, are accepted; every other value is
  rejected at construction.
- **SC-003**: A payload carrying unknown fields from a future version deserializes successfully,
  and a payload missing a required field is rejected — both demonstrated by tests.
- **SC-004**: The published JSON Schema and the checked-in schema file describe the same set of
  fields as the record type, enforced by a test that fails on drift.
- **SC-005**: The contract module imports nothing outside the Python standard library, so
  installing `tokenweir` with no extras pulls in no transport or provider dependency.
- **SC-006**: The full test suite passes via the project's authoritative test command, and the
  existing tests from the extraction scaffold continue to pass unchanged in intent.

## Assumptions

- **Scope is the contract only.** `Sink` and `Source` already exist as interfaces from the TOKWEIR-1
  scaffold; this story does not change them, and does not build transport adapters, the writer,
  migrations, or the pricing/rate-card view. Those are separate TOKWEIR stories.
- **Validation is loud at construction.** Building an invalid record raises, because an
  unattributable or negative-count record is a producer-side programming error and silently
  emitting garbage is worse than failing fast. This does not weaken ADR-0001's off-critical-path
  guarantee, which constrains `Sink.emit` — the emit path — not record construction. The `Sink`
  contract that `emit` must never raise is unchanged by this story.
- **Two error types at construction, one at the wire boundary.** *Omitting* a required constructor
  argument raises `TypeError` — Python's own signature check, kept because giving the required
  fields sentinel defaults would stop type checkers and IDEs from catching the mistake before
  runtime. Supplying an *invalid value* raises `ValueError`. Deserialization collapses the
  distinction: `from_dict`/`from_json` raise `ValueError` for missing or invalid alike, since a
  consumer parsing untrusted payloads should catch one type.
- **The published schema is a strict validator for its own version.** `schema_version` is pinned
  with `const`, so `usage-record.v1.json` accepts only v1 records while the Python type stays
  forward-tolerant per FR-007. This does not contradict the deferral below: the schema states what
  a v1 record *is*; it does not dictate what a consumer should *do* with a v2 one.
- **`status` stays free-form.** The story does not enumerate statuses and the gateway's values are
  not settled, so `status` is a required non-blank string rather than an enum. Enumerating it later
  would be a schema change requiring a `SCHEMA_VERSION` bump.
- **`ts` stays a string.** It is an ISO-8601 UTC timestamp stamped by the producer, carried
  verbatim. The contract does not parse, validate or generate it in this story; producers own their
  clock. Tightening this is a future change.
- **No rejection policy for future `schema_version` values.** A record from a newer contract is
  preserved and readable; deciding whether a consumer should refuse it belongs with the consumer
  (the writer story), not the contract.
- **`SCHEMA_VERSION` stays at 1.** The scaffold's record already declared version 1 and has no
  external consumers pinned to it yet, so completing the contract at version 1 is correct;
  the fields named in the story are the same fields the scaffold declared.
- **Cache-token granularity is deferred.** ADR-0001 notes Claude Code transcripts also carry
  `ephemeral_5m`/`ephemeral_1h` cache fields. The story's field list does not include them; adding
  them later is a schema addition and a `SCHEMA_VERSION` bump.
- **The project constitution is an unfilled template.** `.specify/memory/constitution.md` still
  contains placeholder principles, so no project-specific constitutional constraints were applied
  beyond ADR-0001's pillars.
- **The published schema may require more of a *payload* than the reader insists on.**
  `schema_version` is in the schema's `required` list even though `from_dict` defaults it when
  absent: every record this library emits carries it, so the published document tells a non-Python
  producer to stamp it, while the reader stays tolerant of an older payload that omits it. This is
  the one deliberate asymmetry, and it does not licence the general case — FR-019 and FR-025
  together require the schema and the library to agree on *values*, in both directions.
- **`schema_version` is the one field where the schema is narrower than the library.** The schema
  pins `const: 1`, while the library will construct a record with any positive version. A record
  built with `schema_version=2` therefore fails the v1 schema — correctly, since it is not a v1
  record, and v2 would ship its own schema file. This is the versioning decision above, not an
  FR-025 breach.
- **Validator-level tests are dev-install-only, a known CI gap.** Validating payloads against the
  published schema needs a JSON Schema engine, and the core must not grow one (ADR-0001 Pillar 2),
  so `jsonschema` is a `[dev]` extra and those tests `importorskip`. The MADO stream plan installs
  `pip install -e . pytest`, so they skip there. The *substance* of FR-019 and FR-024 — the numeric
  and blank-string agreement between schema and library — is therefore also covered by
  dependency-free tests that always run. Closing the gap fully means changing the project's registry
  entry to install dev extras, which is a MADO-side change outside this repository.
