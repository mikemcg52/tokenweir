# Tasks: Verify subscription vs API-key transcript token parity (spike)

**Spec**: `spec.md` · **Plan**: `plan.md` · **Branch**: `TOKWEIR-9-subscription-api-token-parity`

Ordering rule: everything that works without a credential comes first, so the credential gate (T14)
is reached with the maximum amount of the run already banked.

## Phase 1 — Transcript extraction (offline)

- [x] **T01** `src/tokenweir/parity.py`: module docstring stating what the module is for, that it is
  a spike harness, and that it holds ADR-0001 Pillar 2 (stdlib only). [FR-010]
- [x] **T02** `Turn` dataclass (`message_id`, `model`, `timestamp`, `usage`, `output_text`,
  `has_unaccounted_blocks`) — frozen, slots, matching the house style. [FR-001]
- [x] **T03** `iter_turns(path)`: per-turn extraction, de-duplicating on `message.id`, reusing
  `claude_code._text` and `TokenTotals.from_mapping`; assembles output text from all text content
  blocks and flags turns carrying blocks whose tokens are unrecoverable. Skips malformed lines and usage-less entries;
  yields nothing for an unreadable file rather than raising. [FR-001, FR-002]
- [x] **T04** `tests/test_parity.py`: `iter_turns` — dedup on repeated id, malformed line skipped,
  usage-less entry skipped, multi-block text assembly, `has_unaccounted_blocks` set for a `tool_use`
  turn, unreadable path yields empty. Synthetic transcripts in `tmp_path` only. [FR-008]

## Phase 2 — Comparison and consistency (offline)

- [x] **T05** `FieldComparison` dataclass + `compare_usage(reference, observed)`: per-field value,
  difference, ratio (guarded against a zero reference), status in
  `parity` / `offset` / `absent`; total over the four billing fields; one-sided extra fields
  surfaced. [FR-003, FR-004, FR-005]
- [x] **T06** Tests for `compare_usage`: agreement, offset with correct difference and ratio, absent
  on either side never `parity`, extra field surfaced, zero reference does not divide. [FR-003..FR-005]
- [x] **T07** `CheckResult` + `consistency_checks(turns)` — Probe C's three identities as independent
  predicates: `cache_creation` sub-fields sum to `cache_creation_input_tokens`; `iterations[]` sums
  to the turn's top-level billing fields; cache reads advance monotonically. Each returns its own
  result; absent inputs give `not_applicable`. [FR-006, FR-007]
- [x] **T08** Tests for `consistency_checks`: each identity passes on consistent input, fails on a
  planted violation, and reports `not_applicable` on missing input. [FR-006, FR-007]

## Phase 3 — Credential and probe plumbing

- [x] **T09** `credential(env, path)`: `ANTHROPIC_API_KEY` preferred, else file; `.strip()` applied;
  blank and absent both "not configured". Never echoed. [FR-020, FR-022, FR-023]
- [x] **T10** `ApiProbe`: stdlib `urllib.request`, `anthropic-version` header, `count_tokens` and
  `messages` methods; model explicit and required, never defaulted; result records model + request
  kind alongside usage; failures raise a message the harness composes itself; no unbounded retry;
  injectable opener for tests. [FR-011..FR-014]
- [x] **T11** Tests: no credential → skipped not failed; injected failing opener → composed error,
  single attempt; **the key appears in no raised message and no rendered report**; model omitted →
  rejected rather than defaulted. [FR-012, FR-014, FR-015, FR-021]
- [x] **T12** `main(argv)`: runs the probes it can, prints a report naming which ran and which were
  skipped and why; `--credential-file`, explicit `--model`, `--transcript`. [FR-040, FR-041]
- [x] **T13** Hygiene test in `tests/test_repo_hygiene.py`: no tracked file contains an
  API-key-shaped string. [FR-024]

## Phase 4 — Execute the measurement

- [x] **T14** Run Probe C against a real Max transcript in this pod; record the numbers. Needs no
  credential.
- [x] **T15** **Credential gate.** With the authorized credential present: run Probe A over a
  population of eligible turns (headline) and Probe B as one small controlled call. Record model,
  date, Claude Code version, transcript measured. If the credential is absent, **stop and
  escalate** — FR-036 forbids writing a verdict for an unmeasured probe.

  **Changed in execution, recorded rather than dropped silently:** this task planned to report
  tool-bearing turns *separately* alongside the headline. That was abandoned once the data showed
  why it could not mean anything — a `tool_use` block's tokens cannot be re-tokenized from the
  transcript at all, so a "separate report" would have been a column of unmeasurable rows. Excluded
  turns are instead counted in aggregate, by reason, and the report's totals are made to close
  (eligible + excluded == turns). The finding states the exclusion and its cause as a limit.

  **Also added in execution:** Probe A′ (the API-side control, gated behind `--control`) and the
  envelope derivation — both now described in `spec.md`. See T22/T23 below.

## Phase 5 — The finding

- [x] **T16** `docs/parity-subscription-vs-api.md`: conditions, per-probe results, one explicit
  verdict (parity / correction factor / characterized difference), measured-vs-inferred separated,
  limits stated, point-in-time notice. [FR-030..FR-032, FR-036]
- [x] **T17** `docs/adr-0001-token-weir.md`: update item 6 and the Pillar 4 `Unverified:` note to the
  outcome — no further than the measurement supports. [FR-033]
- [x] **T18** `src/tokenweir/claude_code.py`: the emitter-side note — a correction factor or handling
  note if the counts differ, or a recorded "parity verified, when, under what conditions" if they
  agree. Docs/constants only; no change to reading or emitting behaviour. [FR-034, FR-035]
- [x] **T19** `README.md`: the harness command and where the finding lives, per `CLAUDE.md`'s
  keep-docs-current rule.

## Phase 6 — Added during the review loop

- [x] **T22** Probe A′: the API-side control, and the PARITY / DIFFERENCE / NO VERDICT verdict the
  harness prints for itself. Gated behind an opt-in `--control` flag (default off) since it is the
  only expensive probe. [FR-030, FR-040]
- [x] **T23** `ApiProbe.envelope`: derive the `count_tokens` framing constant by doubling, so Probe A
  reports a bare-text comparison rather than a raw offset a reader cannot interpret. [FR-040]
- [x] **T24** Make the report's turn accounting close, and each of the eligibility rule's four
  conjuncts individually load-bearing under mutation. [FR-041]

## Phase 7 — Gate

- [x] **T20** Full suite green: `CI=true /workspace/repo/.venv/bin/pytest` from `/workspace/repo`
  (pass codes 0, 5), plus `ruff`.
- [x] **T21** Commit, then the independent reviewer; fix High/Med and re-review to a terminal review.

## Traceability

| FR | Tasks |
|---|---|
| FR-001, FR-002 | T02, T03, T04 |
| FR-003..FR-005 | T05, T06 |
| FR-006, FR-007 | T07, T08 |
| FR-008 | T04, T06, T08 |
| FR-010..FR-014 | T01, T10, T11 |
| FR-015 | T11 |
| FR-020..FR-023 | T09, T11 |
| FR-024 | T13 |
| FR-030..FR-032 | T16 |
| FR-033 | T17 |
| FR-034, FR-035 | T18 |
| FR-036 | T15, T16 |
| FR-040, FR-041 | T12, T22, T23, T24 |
| FR-030 (the verdict itself) | T16, T22 |
