# Feature Specification: Untrack the committed coverage artifact

**Feature Branch**: `TOKWEIR-12-untrack-coverage-artifact` (branched from
`TOKWEIR-4-usage-record-contract`, which is not yet merged)
**Created**: 2026-08-09
**Status**: Draft
**Jira**: TOKWEIR-12 (Bug) — "Remove committed .coverage artifact and add it to .gitignore",
`Relates` to TOKWEIR-4
**Input**: A 53 KB `.coverage` SQLite binary is tracked in the repo. It was swept in by a
`git add -A` in commit `caffcbd` (TOKWEIR-4's last fix commit) after a coverage run left it in the
working tree. `.gitignore` was rewritten on that same branch to cover `__pycache__/`, `*.py[cod]`,
`*.egg-info/`, `.pytest_cache/`, `.ruff_cache/` and `.venv/` — but not `.coverage`, so this recurs
every time anyone runs coverage.

**Acceptance (from the story):** `.coverage` is untracked, `git status` is clean after a coverage
run, and no coverage artifact is tracked.

## Context

This is a small, contained defect, and the spec is deliberately short to match. Two things make it
worth more than a one-line change:

1. **It recurs.** The underlying cause is a missing ignore rule, not the one stray file. Deleting
   the file without closing the rule gap means the next coverage run re-stages it.
2. **`tokenweir` is the intended first open-source release** (ADR-0001 Pillar 1). A tracked build
   artifact carrying absolute developer paths (`/workspace/repo/src/tokenweir/*.py`) is noise a
   public repo should not ship. No secrets are involved.

The project's own MADO registration (`mado/config.yaml`) declares `coverage.tool: pytest-cov` with
`format: xml`, so a MADO coverage run produces `coverage.xml` as well as `.coverage` — the ignore
rules must cover what this project actually generates, not just the file that happened to leak.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - The artifact leaves the repository (Priority: P1)

A developer clones `tokenweir` and finds no coverage data in the tree.

**Why this priority**: It is the defect. Everything else here exists to stop it coming back.

**Independent Test**: `git ls-files` lists no coverage artifact.

**Acceptance Scenarios**:

1. **Given** the repository at this branch's tip, **When** the tracked files are listed, **Then**
   `.coverage` is not among them.
2. **Given** a working copy where a developer has already run coverage, **When** `.coverage` is
   untracked, **Then** their local file is left on disk — untracking removes it from version
   control, not from the developer's machine.

---

### User Story 2 - Running coverage no longer dirties the tree (Priority: P1)

A developer or reviewer runs coverage, then `git status`, and sees nothing to commit.

**Why this priority**: This is the story's own acceptance wording, and the property that stops the
bug recurring. Without it the next `git add -A` re-commits the artifact.

**Independent Test**: Generate the artifacts a coverage run produces, then check `git status`.

**Acceptance Scenarios**:

1. **Given** a clean tree, **When** a coverage run produces `.coverage`, **Then** `git status`
   reports nothing to commit.
2. **Given** a clean tree, **When** coverage is run in parallel mode producing `.coverage.<suffix>`
   files, **Then** `git status` reports nothing to commit.
3. **Given** a clean tree, **When** an HTML report is written to `htmlcov/`, **Then** `git status`
   reports nothing to commit.
4. **Given** a clean tree, **When** an XML report is written to `coverage.xml` — the format
   `mado/config.yaml` declares for this project — **Then** `git status` reports nothing to commit.

---

### User Story 3 - The repository stays free of build artifacts (Priority: P2)

A future change that re-commits a coverage artifact fails the test suite rather than reaching
`main` unnoticed.

**Why this priority**: P2 because the ignore rules are the primary defence; this is the guard that
catches a `git add -f`, a rule someone deletes, or the same mistake in a different artifact. It is
what makes the story's third acceptance clause ("no coverage artifact is tracked") a standing
property rather than a one-time observation.

**Independent Test**: The test suite fails if a coverage artifact is tracked.

**Acceptance Scenarios**:

1. **Given** the test suite, **When** any coverage artifact is tracked by git, **Then** a test
   fails and names the offending path.
2. **Given** an environment without git, or a source tree extracted from an sdist with no
   repository, **When** the suite runs, **Then** that test skips rather than errors — it is a
   repository-hygiene check, and its absence must not turn an ordinary install red.

### Edge Cases

- **The file exists locally but is no longer tracked**: expected and correct. `git rm --cached`
  removes the index entry only.
- **Another checkout pulls this change**: git deletes their copy of `.coverage`. Acceptable — it is
  regenerable data, not source.
- **The blob remains in history.** Untracking removes the file from the tip, not from the commits
  that already contain it — `caffcbd` still holds the 53 KB blob, so a `git clone` continues to
  carry it and the absolute developer paths inside it. This story's acceptance is about the index
  ("`.coverage` is untracked … no coverage artifact is tracked"), which untracking satisfies, and
  scrubbing history is a materially different operation: `caffcbd` is **already pushed to
  `origin/TOKWEIR-4-usage-record-contract`**, so removing it means rewriting published history and
  force-pushing a branch that has already been through six reviews. That is the developer's call,
  not this run's, and it is deliberately **not** done here. It is recorded rather than decided by
  omission, and filed as a follow-up, because the moment it is cheapest is before the branch merges
  and long before the repository is made public — after a public release it needs `git filter-repo`
  and coordination with everyone who has cloned.
- **A parallel-mode data file (`.coverage.hostname.12345.xyz`)**: covered by the `.coverage.*`
  rule, which is a distinct pattern from `.coverage` and needed separately.
- **A directory literally named `coverage/`**: not covered, and deliberately so — no tool this
  project uses writes one, and a broad rule risks masking a real source directory.
- **The test running inside the pod's `/workspace/repo`**: it must ask git about the repository the
  test file lives in, not the process's current working directory, so it behaves the same however
  pytest is invoked.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `.coverage` MUST NOT be tracked in the repository.
- **FR-002**: Untracking MUST NOT delete a developer's local copy of the file.
- **FR-003**: `.gitignore` MUST ignore the coverage artifacts this project can produce:
  `.coverage`, parallel-mode `.coverage.*` data files, `htmlcov/`, and `coverage.xml`.
- **FR-004**: After a coverage run, `git status` MUST report a clean tree.
- **FR-005**: The test suite MUST fail if any coverage artifact is tracked, naming the path.
- **FR-006**: That test MUST skip — not error or fail — where git is unavailable or the source tree
  is not a git repository.
- **FR-007**: The change MUST NOT alter `tokenweir`'s runtime behaviour, its public API, or the
  published schema. It touches repository hygiene only.

### Key Entities

- **Coverage artifact**: regenerable output of a coverage run — the `.coverage` SQLite data file,
  its parallel-mode siblings, the `htmlcov/` HTML report, and the `coverage.xml` report.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `git ls-files` returns no coverage artifact.
- **SC-002**: Running a coverage pass and then `git status --porcelain` produces no output.
- **SC-003**: The full test suite passes via the authoritative command in
  `/workspace/.mado/project.yaml`, with the same count as before this change plus the new guard.
- **SC-004**: No file under `src/tokenweir/` or `schema/` is modified — the published contract from
  TOKWEIR-4 is untouched.

## Assumptions

- **The branch is based on unmerged work.** `TOKWEIR-12-untrack-coverage-artifact` branches from
  `TOKWEIR-4-usage-record-contract` at the developer's explicit instruction, because the fix is to
  a commit on that branch and the two will merge together. A reviewer diffing against `main` will
  therefore see all of TOKWEIR-4 as well; only the TOKWEIR-12 commits are under review here.
- **The local `.coverage` file is left in place.** `git rm --cached` is the right instrument;
  deleting the developer's working file would be a side effect the story does not ask for.
- **Scope is repository hygiene, not the coverage toolchain.** `pytest-cov` is named in
  `mado/config.yaml` but is not declared in `pyproject.toml`'s `[dev]` extras, so `pytest --cov`
  does not work from a plain dev install. That is a real gap, but it is a different problem from
  the one this story states, and adding a dependency is not "untrack an artifact". Raised in the
  report rather than fixed here.
- **`.gitignore` gains only coverage rules.** The file already covers Python bytecode, build
  metadata, tool caches and virtualenvs from TOKWEIR-4. Broadening it further is not this story.
