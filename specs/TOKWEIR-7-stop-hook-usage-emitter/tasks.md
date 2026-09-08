# Tasks: Stop-hook usage emitter (transcript delta → tokenweir)

**Input**: [spec.md](./spec.md), [plan.md](./plan.md)
**Jira**: TOKWEIR-7 (Story), parent epic TOKWEIR-2
**Branch**: `TOKWEIR-7-stop-hook-usage-emitter`

**Tests**: Required. The story's acceptance is two testable claims — one correct record per turn,
and a forced emitter failure that does not block the next turn — and neither can be demonstrated
by inspection. The test module is the deliverable, not a trailing chore.

**Organization**: Grouped by user story. `[P]` marks tasks touching disjoint files.

---

## Phase 1: Setup

- [x] **T001** Point `.specify/feature.json` at this story's feature directory so the speckit
      scaffold resolves TOKWEIR-7 rather than TOKWEIR-30.

---

## Phase 2: Foundational — the parse/delta core

**Blocking**: every user story below depends on these. Written first and independently testable,
so the entry point has nothing left to invent.

- [x] **T002** Create `src/tokenweir/claude_code.py` with a module docstring stating what the
      adapter is (ADR-0001 Pillar 4), why it is deterministic rather than an LLM skill, why it
      carries no third-party import, and the cumulative-baseline argument from plan decision 1.
- [x] **T003** Add `TokenTotals` — the four counts as a small immutable value with addition and a
      per-field `>=` comparison, so "did the cumulative go backwards" (FR-014) is one expression
      rather than four repeated comparisons.
- [x] **T004** Add `scan_transcript(path)`: stream the JSONL, skip unusable lines (FR-007), read
      `message.usage` with per-field coercion of missing/non-integer/negative to zero (FR-008),
      de-duplicate on `message.id` with an entry-`uuid` fallback (FR-006), and return the
      cumulative totals plus the last counted entry's `model`, `id` and timestamp (FR-005, FR-009).
- [x] **T005** Add the state seam: `state_path_for(transcript_path)` keyed by a hash of the
      resolved path under `TOKENWEIR_HOOK_STATE_DIR` or a cache default (FR-011);
      `read_baseline(path)` treating absent/corrupt/unreadable as empty (FR-015);
      `write_baseline(path, totals)` writing to a temp file in the same directory and `os.replace`
      -ing it into place (FR-016), returning a bool rather than raising when the directory is
      unwritable (FR-017).
- [x] **T006** Add `turn_delta(current, baseline)` returning the per-field difference, `None` when
      the current totals regress below the baseline (FR-014), and zeroes when nothing changed
      (FR-013).

---

## Phase 3: User Story 1 — one correct record per turn (P1)

**Goal**: a turn's tokens, counted once, emitted once, tagged `subscription`.
**Independent test**: `pytest tests/test_claude_code_hook.py -k counts`

- [x] **T007** Add `read_hook_input(stream)`: parse the stdin JSON object, return
      `transcript_path` and `session_id`, and return an empty result for anything unparseable
      (FR-001, FR-002, FR-003).
- [x] **T008** Add `build_fields(...)`: assemble the record's field mapping per plan decision 4 —
      identity, defaults, token counts, `ts` normalization to UTC ISO-8601 with the turn's own
      timestamp preferred (FR-019, FR-020, FR-023, FR-024).
- [x] **T009** Add `run(...)`: the ordered pipeline — read input → scan → baseline → delta → build
      → emit → **then** advance the baseline (FR-012), emitting nothing on a zero delta or a
      regression.
- [x] **T010** Wire emission through `BufferedEmitter` + `emit_usage` with a bounded
      `close_timeout` (FR-028, FR-029). No bare sink call anywhere in the module.
- [x] **T010a** *(added at review 3)* Decide "stored" from the transport's own counter
      (`DirectSink.written` / `AMQPSink.published`), not `EmitterStats.delivered`, which means
      only "did not raise" and is therefore true of every conforming sink that dropped the
      record (FR-012).
- [x] **T010b** *(added at review 3)* Mark a configured-but-unconstructable transport degraded
      so it never counts as stored, and build the sink lazily so a turn with nothing to emit
      opens no connection (FR-012a, FR-012b).
- [x] **T011 [P]** `tests/test_claude_code_hook.py`: a `write_transcript` helper and a recording
      sink; the three-message turn summing to `350/75/5/140` (SC-001).
- [x] **T012 [P]** Test: a second invocation after appending emits only the new messages' tokens,
      and the two records' sums equal the session total (SC-002).
- [x] **T013 [P]** Test: four entries sharing one `message.id` are counted once (FR-006).
- [x] **T014 [P]** Test: a zero delta emits nothing; a regressed transcript emits nothing and
      resets the baseline (FR-013, FR-014).
- [x] **T015 [P]** Test: sidechain entries are counted, and unusable lines are skipped without
      abandoning the rest of the file (FR-007).
- [x] **T016 [P]** Test: the emitted record carries `pricing_mode=subscription` and validates
      against `schema/usage-record.v1.json` (SC-006), skipping if `jsonschema` is absent, as the
      existing schema tests do.

---

## Phase 4: User Story 2 — a failure never disturbs the session (P1)

**Goal**: exit 0 on every path, nothing on stdout, bounded time.
**Independent test**: `pytest tests/test_claude_code_hook.py -k never_disturbs`

- [x] **T017** Add `main(argv=None)`: catch `Exception` (never `BaseException`), return `0` on
      every path, and write diagnostics only to stderr (FR-004, FR-025, FR-026).
- [~] **T018** ~~Arm a self-imposed `SIGALRM` time budget.~~ **Removed at review 2** as scope the
      story does not carry: FR-027 is satisfied by Claude Code's own hook `timeout`, which the
      documented `settings.json` fragment sets. Recorded rather than deleted so the next reader
      knows it was tried and why it went.
- [x] **T019** Add `select_sink()`: `TOKENWEIR_AMQP_URL` → `AMQPSink.from_url`, else
      `TOKENWEIR_DSN` → `DirectSink(PostgresSource)`, else `NullSink`; transport imports inside
      the branch; a construction failure degrades to `NullSink` (FR-030, FR-031).
- [x] **T020 [P]** Test: a sink raising on every emit → exit 0, empty stdout (SC-003, SC-004).
- [x] **T021 [P]** Test: garbage stdin, absent `transcript_path`, a missing transcript file, and a
      transcript whose last line is a half-written fragment → exit 0 each (FR-002, FR-007).
- [x] **T022 [P]** Test: an unwritable state directory still emits the record and exits 0
      (FR-017).
- [x] **T023 [P]** Test: a sink whose `emit` blocks indefinitely → the hook returns within its
      budget (SC-005). Bounded by the test's own generous ceiling so a regression fails rather
      than hangs the suite.
- [x] **T024 [P]** Test: `main` returns 0 — and never 2 — for an internal error raised from inside
      the pipeline (FR-025).

---

## Phase 5: User Story 3 — attribution from the environment (P1)

**Goal**: issue, phase and stream on the record; nothing read from the model.
**Independent test**: `pytest tests/test_claude_code_hook.py -k attribution`

- [x] **T025** Add `attribution_from_env()`: `MADO_ISSUE_KEY` → `workload`, `MADO_PHASE` →
      `queue`, `MADO_STREAM_ID` → `parent_request_id`, treating unset **and blank** alike as
      `None` (FR-021, FR-022); `MADO_PRICING_MODE` coerced through `PricingMode`, falling back to
      `SUBSCRIPTION` on anything unrecognized (FR-018).
- [x] **T026 [P]** Test: the three variables land in their fields (SC-007).
- [x] **T027 [P]** Test: unset and whitespace-only both leave the fields `None`, and the record is
      still emitted (FR-022).
- [x] **T028 [P]** Test: an invalid `MADO_PRICING_MODE` still emits, stamped `subscription`; a
      valid `api_metered` is honoured (FR-018).

---

## Phase 6: User Story 4 — installable and documented (P2)

- [x] **T029** Add `[project.scripts] tokenweir-claude-code-hook = "tokenweir.claude_code:main"`
      to `pyproject.toml`, and an `if __name__ == "__main__"` guard so `python -m
      tokenweir.claude_code` works identically (FR-032).
- [x] **T030** README: a "Subscription capture — the Claude Code Stop hook" section with the
      `settings.json` fragment (`exit 0`, `timeout` ≤ 30s), the full environment-variable table,
      the no-transport default, and the `queue`-carries-phase compromise (FR-033).
- [x] **T031 [P]** Tick ADR-0001 implementation checklist item 5, and note that item 6 (the
      `Unverified` parity check) remains open — this story captures what the transcript says, it
      does not verify the transcript against an API-key session.
- [x] **T032 [P]** Test: importing `tokenweir.claude_code` loads neither `pika` nor `psycopg`
      (FR-031), matching the existing dependency-light guard's approach.
- [x] **T033 [P]** Test: the console-script entry point is declared **and resolves to a callable**
      through the installed distribution's metadata (FR-032) — a declaration that is merely present
      in `pyproject.toml` is not the same claim — and the README documents every environment
      variable the module reads, a guard that fails when the code grows a knob the docs do not
      mention (FR-033).

---

## Phase 7: Verification

- [x] **T034** Run the authoritative test plan from `/workspace/.mado/project.yaml`:
      `CI=true /workspace/repo/.venv/bin/pytest` from the repo root. Green, with the pre-existing
      skip disclosures unchanged.
- [x] **T035** `ruff check src tests` clean at the configured rule set.
