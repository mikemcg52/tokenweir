---
description: "Task list for TOKWEIR-12 — untrack the committed coverage artifact"
---

# Tasks: Untrack the committed coverage artifact

**Input**: Design documents from `/specs/TOKWEIR-12-untrack-coverage-artifact/`
**Prerequisites**: plan.md, spec.md
**Jira**: TOKWEIR-12 (Bug)

**Tests**: One test is included and is not optional. The story's third acceptance clause — "no
coverage artifact is tracked" — is a standing property, and the only way to keep it standing after
the ignore rules are in place is to assert it.

## Format: `[ID] [P?] [Story] Description`

## Path Conventions

Single project, `src/` layout. This story touches only `.gitignore` and `tests/`.

---

## Phase 1: Close the rule gap

**Purpose**: The ignore rules must land before the file is untracked, or the next `git add -A`
re-stages it.

- [x] T001 [US2] Add the coverage artifact rules to `/workspace/repo/.gitignore`: `.coverage`,
      `.coverage.*`, `htmlcov/`, `coverage.xml` — the set this project can actually produce, given
      `mado/config.yaml` declares `coverage.format: xml` (FR-003).

---

## Phase 2: Remove the artifact

- [x] T002 [US1] `git rm --cached .coverage` — remove the index entry while leaving the developer's
      local file on disk (FR-001, FR-002).

---

## Phase 3: Stop it recurring

- [x] T003 [US3] Add `/workspace/repo/tests/test_repo_hygiene.py` asserting that no tracked path
      matches a coverage artifact pattern, naming any offender in the failure message (FR-005).
- [x] T004 [US3] Make that test resolve the repository from the test file's own location, not the
      process CWD, so the result does not depend on where pytest was invoked from.
- [x] T005 [US3] Make that test skip — not fail or error — when git is unavailable or the tree is
      not a git repository, e.g. an sdist install (FR-006).

---

## Phase 4: Verification

- [x] T006 Run the authoritative test command from `/workspace/.mado/project.yaml`
      (`CI=true /workspace/repo/.venv/bin/pytest`, pass exit codes `0` and `5`) and confirm green,
      including every test TOKWEIR-4 added (SC-003).
- [x] T007 Run `ruff check .` — the repo configures ruff in `pyproject.toml`.
- [x] T008 [US2] Prove SC-002 rather than assume it: generate `.coverage`, a parallel-mode
      `.coverage.<suffix>`, `htmlcov/` and `coverage.xml`, then confirm `git status --porcelain` is
      empty.
- [x] T009 Confirm nothing under `src/tokenweir/` or `schema/` is modified by this branch's commits
      (SC-004, FR-007).

---

## Dependencies

- T001 blocks T002 (rules first, or the removal is undone by the next `git add`).
- T003 depends on T002 (it asserts the end state).
- T004 and T005 are properties of the test written in T003.
- Phase 4 depends on everything.

## Parallel opportunities

Essentially none — the change is small and sequential by nature. T003–T005 are one test file.
