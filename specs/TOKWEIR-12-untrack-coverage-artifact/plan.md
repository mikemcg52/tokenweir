# Implementation Plan: Untrack the committed coverage artifact

**Branch**: `TOKWEIR-12-untrack-coverage-artifact` | **Date**: 2026-08-09 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/TOKWEIR-12-untrack-coverage-artifact/spec.md`
**Jira**: TOKWEIR-12 (Bug), `Relates` TOKWEIR-4

## Summary

Remove `.coverage` from git's index, close the `.gitignore` gap that let it in, and add one test so
the repository cannot silently pick up a coverage artifact again.

Three files: `.gitignore`, a new `tests/test_repo_hygiene.py`, and the index entry itself. Nothing
under `src/` or `schema/` is touched — the TOKWEIR-4 contract is not in scope and must come through
unchanged.

## Technical Context

**Language/Version**: Python 3.11+ (pod runs 3.12.3)
**Primary Dependencies**: None added. The new test uses `subprocess` and `pathlib` from the standard
library to ask git what it tracks.
**Storage**: N/A
**Testing**: pytest, via `/workspace/.mado/project.yaml`
(`CI=true /workspace/repo/.venv/bin/pytest`, pass exit codes `0` and `5`)
**Target Platform**: The repository itself — this is repository hygiene, not library behaviour.
**Project Type**: Single Python library (`src/` layout)
**Performance Goals**: N/A
**Constraints**: No change to runtime behaviour, public API, or the published schema (FR-007). No new
dependency — the core stays dependency-light (ADR-0001 Pillar 2), and a hygiene test must not be the
thing that drags one in.
**Scale/Scope**: One ignore-rule edit, one index removal, one new test file.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

`.specify/memory/constitution.md` remains an **unfilled template** (every principle is a
`[PRINCIPLE_N_NAME]` placeholder), so it imposes no project-specific gates. Recorded rather than
silently skipped: the gate is vacuous, not passed on the merits.

The one ADR-0001 constraint that bears on this change:

| ADR-0001 pillar | Applies how | Status |
|---|---|---|
| Pillar 1 — standalone extraction and open-core boundary | `tokenweir` is the intended first OSS release, so a tracked build artifact carrying absolute developer paths is exactly the noise a public repo should not ship | PASS — this change removes it |
| Pillar 2 — dependency-light core | The regression guard must not introduce a dependency | PASS — stdlib `subprocess` only, and it is a test, not core code |

## Project Structure

### Documentation (this feature)

```text
specs/TOKWEIR-12-untrack-coverage-artifact/
├── spec.md
├── plan.md    # This file
└── tasks.md
```

### Source Code (repository root)

```text
.gitignore                  # + coverage artifact rules
tests/
└── test_repo_hygiene.py    # NEW — the regression guard
```

Nothing else. `src/tokenweir/` and `schema/` are deliberately untouched.

**Structure Decision**: The guard goes in its own `tests/test_repo_hygiene.py` rather than into
`test_contract.py`, because it asserts a property of *the repository*, not of the library. Mixing it
into a contract test file would mislead the next reader about what that file is for — and TOKWEIR-4
already established the convention of one test file per concern.

## Design decisions

1. **`git rm --cached`, not `git rm`.** The developer's local `.coverage` is regenerable data they
   may be mid-way through using; removing the index entry is the whole requirement (FR-002).

2. **Four ignore rules, not one.** `.coverage` does not match `.coverage.*`, so parallel-mode data
   files need their own pattern. `htmlcov/` and `coverage.xml` are the report outputs — and
   `coverage.xml` specifically because `mado/config.yaml` declares `format: xml` for this project,
   so a MADO coverage run produces it. Covering only the file that leaked would leave the same bug
   one flag away.

3. **No `coverage/` rule.** A bare directory name is too broad for a repo that may one day hold a
   legitimately-named source directory, and nothing in this toolchain writes one. Narrow rules that
   match real outputs beat a wide rule that might mask source.

4. **The guard asks git, rather than checking the filesystem.** The defect was a *tracked* file, not
   a present one — `.coverage` exists on disk right now and correctly so. `git ls-files` is
   therefore the only thing that answers the actual question.

5. **The guard resolves the repository from the test file's own location.** Using the process CWD
   would make the result depend on where pytest was invoked from; a previous session in this repo
   was misled by exactly that class of mistake when a stray copy of the tree sat in a scratch
   directory.

6. **The guard skips where git is absent or the tree is not a repository.** An sdist install has no
   `.git`, and a hygiene check failing there would turn an ordinary install red for a property that
   cannot even be evaluated (FR-006). Skip is the honest outcome, not a silent pass — though note
   the trade-off in Risks.

7. **The guard checks a pattern set, not just `.coverage`.** It fails on any tracked path matching
   the coverage artifact shapes, so re-committing `coverage.xml` or `htmlcov/index.html` is caught
   too. That is what makes the story's "no coverage artifact is tracked" a standing property.

## Phasing

| Phase | What | Why this order |
|---|---|---|
| 1 | Add the ignore rules | Must land before untracking, or the very next `git add -A` re-stages the file |
| 2 | `git rm --cached .coverage` | The defect itself |
| 3 | Add the regression guard | Asserts the end state, so it goes after it exists |
| 4 | Verify: full suite, ruff, and a real coverage run leaving a clean tree | Proves SC-002 rather than assuming it |

## Risks

- **A skipped guard is a quiet guard.** Where git is unavailable the test skips, so it protects the
  repository and CI but not an arbitrary consumer environment. That is the correct scope — the
  property is about this repository — but it means the guard's value depends on CI running inside a
  git checkout, which the MADO stream pod does.
- **`git rm --cached` on a file that other checkouts hold** will delete their copy on pull. Called
  out in the spec's Edge Cases; acceptable for regenerable data.
- **This branch is based on unmerged TOKWEIR-4 work**, at the developer's instruction. A reviewer
  diffing `main...HEAD` sees both stories; the TOKWEIR-12 change is the last commits only.
