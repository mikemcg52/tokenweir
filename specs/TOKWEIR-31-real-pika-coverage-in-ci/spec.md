# Feature Specification: Make the authoritative run admit what it did not prove

**Feature Branch**: `TOKWEIR-31-real-pika-coverage-in-ci`, cut from `main` at `06c12bb`.
Unlike TOKWEIR-30 and TOKWEIR-15, this one needs no parent branch: the tests it is about are
already merged, and the change is to the README, `tests/conftest.py` and the suite that guards
them.

**Created**: 2026-08-20
**Status**: Draft
**Jira**: TOKWEIR-31 (Story), `Relates` to TOKWEIR-6
**Input**: The Jira story, fetched with `getJiraIssue` on 2026-08-20 and quoted verbatim below.
It was filed by the TOKWEIR-6 run itself, as a deferred Med from that story's terminal review.
Quoted in full because the reviewer subagent has no Jira write access and only intermittent read
access, and this quote is the acceptance text it grades fidelity against.

> **Gap.** `tests/test_amqp.py::test_publish_properties_construct_a_real_pika_basicproperties` and
> `::test_a_real_pika_sink_publishes_persistent_json_to_a_channel_double` are gated by
> `pytest.importorskip("pika")`. `pika>=1.3` is declared in the `dev` extra, but the MADO
> registry's install step for this project is `pip install -e . pytest` — no extras. Both tests
> therefore **skip** under the command the project calls authoritative.
>
> The consequence: under CI, FR-022 ("persistent messages with a JSON content type") is proven
> only against `FakeProperties`, a dict wrapper that accepts any kwargs at all. Nothing in the
> authoritative run would notice if `publish_properties()` started emitting a key
> `pika.BasicProperties` rejects.
>
> **Verified working when the driver is present.** Both tests pass with real pika 1.4.4
> installed — this is a signal gap, not a defect. The behaviour under test is correct today.
>
> **Options, in rough order of preference:**
>
> 1. Add `pika` to this project's install commands in `/etc/mado/projects.yaml` so the
>    registry-driven run exercises it. Cheapest, and makes the authoritative command match the
>    dev experience.
> 2. Change the install step to `pip install -e '.[dev]'`. Broader, and would also un-skip the
>    `pglast` / `build` suites — worth considering on its own merits, but a bigger change than
>    this finding needs.
> 3. Leave it and document the limitation explicitly in `README.md` under "Develop", so nobody
>    reads a green CI run as covering FR-022 against the real driver.
>
> Note this is consistent with the repo's existing convention — the real-Postgres suite skips the
> same way for the same reason — so option 3 is defensible. The point of this ticket is that the
> choice should be deliberate and written down rather than incidental.
>
> Files: `/etc/mado/projects.yaml` (registry), `pyproject.toml`, `README.md`, `tests/test_amqp.py`.

## Context

### The gap, reproduced on this branch

The authoritative command was run before anything was changed — `CI=true
/workspace/repo/.venv/bin/pytest`, from a venv built by the registry's own install step
(`pip install -e . pytest`):

```
814 passed, 81 skipped in 5.74s     exit 0
python -c "import pika"  ->  ModuleNotFoundError: No module named 'pika'
```

Eighty-one skips, and the summary line names none of them. Both FR-022 real-driver assertions are
in there. The run is green, and nothing about it tells a reader which claims it did not test.

### Why the choice fell to option 3

Options 1 and 2 are the ticket's stated preferences, and both edit
`/etc/mado/projects.yaml` — a file on the MADO VM. From a stream pod it does not exist:

```
$ ls /etc/mado/
ls: cannot access '/etc/mado/': No such file or directory
```

There is no `mado register` CLI in the pod either; only `mado-phase` and `mado-notify`. So the
registry is not this branch's to change, and a story that claimed to have taken option 1 would be
claiming an edit it could not make. Option 3 is the one this repository can actually execute, and
the ticket calls it defensible on its own merits: the real-Postgres suite has skipped the same way
for the same reason since TOKWEIR-5, and treating the AMQP driver differently would be the
inconsistency.

**The registry options are not dropped, they are handed over.** Written down in `README.md` where
whoever owns the registry can act on them — see FR-050. Choosing option 3 here settles what *this
repository* does; it does not forbid someone later adding `pika` to the install step, and if they
do, the mechanism below goes quiet by itself because the driver is then present.

### What is actually wrong, stated precisely

Not the skip. The skip is correct and must stay — the core is transport-free by contract
(ADR-0001 Pillar 2), so a `pip install -e .` legitimately has no `pika`, and a test that cannot be
evaluated must skip rather than turn an ordinary install red. That rule is already written down in
`tests/conftest.py` and `test_repo_hygiene.py`.

What is wrong is that **the skip is silent and the green is therefore overstated**. A run that
proves 814 things and declines to prove 2 reports only the 814. The reader supplies the missing
half of the sentence themselves, and they supply "…and everything else passed", because that is
what a green run normally means.

So the fix is not to run more tests. It is to make the run say what it did not do.

### Why the README alone is not enough

Option 3 as written says "document the limitation in `README.md`". That is necessary and it is
where the *decision* belongs. But the failure mode the ticket names is *"nobody reads a green CI
run as covering FR-022 against the real driver"* — and the person at risk of that misreading is
looking at a CI log, not at the README. A note in a file they are not reading does not reach them.

So the decision goes in the README, and a one-line statement of the same fact goes in the run's
own output, where the misreading happens. The two must agree, and both are guarded by tests.

### Why not `-ra`

The obvious cheap answer is to turn on pytest's skip-reason summary. It was measured on this
branch and rejected: `pytest -q -ra` emits **43 `SKIPPED` lines**, because pytest groups them by
source line, not by reason — the long "no Postgres configured" reason repeats forty times and the
two `pika` lines sit in the middle of it. That is not a signal; it is the same silence with more
scrolling, and it would grow noisier every time a store test is added. What is wanted is one
sentence naming the forfeited claim, not a transcript of every skip.

### Scope: the whole optional-driver family, not `pika` alone

The ticket is about `pika` and FR-022. But `pglast`, `build`, and `psycopg`/`pgserver` are absent
from the same install for exactly the same reason and forfeit coverage the same way — the ticket's
own note says so. A mechanism that named only `pika` would have to be extended the first time
anyone asked the same question about the migration-SQL suite, and a README section that documented
only `pika` would be a half-truth about what a bare install omits.

So the mechanism covers the family and `pika`/FR-022 is the entry the ticket is about. This is a
deliberate widening of the ticket's letter to match the ticket's purpose; it adds one table, not a
second design.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A green CI run states which claims it did not test (Priority: P1)

Someone reads the output of the authoritative test run — in a MADO push, a terminal, a CI log —
and decides from it whether the AMQP adapter's FR-022 behaviour is covered.

**Why this priority**: This is the misreading the ticket exists to prevent, and it is the only part
of the change that reaches the person at the moment they form the wrong belief.

**Independent Test**: Run the authoritative command in an environment with no `pika` and read the
output. Deliverable on its own: the note appears whether or not the README is touched.

**Acceptance Scenarios**:

1. **Given** a venv built by the registry's install step (`pip install -e . pytest`, no extras),
   **When** the authoritative test command runs to completion, **Then** its output contains a note
   naming `pika` as absent and stating that FR-022 was not checked against the real driver.
2. **Given** that same run, **When** it finishes, **Then** its exit status is unchanged from what
   it would have been without the note — a forfeited claim is a reporting fact, not a failure.
3. **Given** an environment where `pika` is installed, **When** the suite runs, **Then** no note
   about `pika` appears, because there is nothing to disclose.
4. **Given** an environment where every optional driver is installed (`pip install -e '.[dev]'`
   plus a Postgres), **When** the suite runs, **Then** no disclosure note appears at all.

### User Story 2 - The decision is written down where decisions live (Priority: P1)

A developer, or a future reviewer, wants to know whether the FR-022 skip is a considered choice or
an oversight — which is the exact question this ticket was filed to settle.

**Why this priority**: "The choice should be deliberate and written down" is the ticket's own
statement of what done means. Without it the change is a clever log line and no decision.

**Independent Test**: Read `README.md` under "Develop" with no other context and answer: what does
a bare install not cover, and was that on purpose?

**Acceptance Scenarios**:

1. **Given** `README.md`, **When** a reader reaches the "Develop" section, **Then** it names which
   optional drivers a bare `pip install -e .` omits and what coverage each omission forfeits.
2. **Given** that section, **When** the reader looks for the rationale, **Then** it states that the
   omission is accepted deliberately and why (the core is transport-free by contract, so the
   drivers cannot be required).
3. **Given** that section, **When** whoever owns the MADO registry reads it, **Then** it tells them
   what to change to close the gap on their side, so the ticket's preferred options remain
   available rather than being lost with this decision.

### User Story 3 - The written-down decision cannot rot (Priority: P2)

Six months on, someone edits the README, or adds an optional-driver skip to the suite, and does not
know either of these was load-bearing.

**Why this priority**: The repository already holds this rule — `test_the_readme_documents_the
_store` and `test_the_readme_keeps_the_load_bearing_warnings` exist because "a MUST nobody checks
is one refactor away from being gone". A disclosure that silently stops being true is worse than
none, because it is still being trusted.

**Independent Test**: Delete the README paragraph, or add a new `importorskip` to the suite that
the disclosure table does not know about, and confirm the suite goes red.

**Acceptance Scenarios**:

1. **Given** the guarded README markers, **When** one is removed from `README.md`, **Then** a test
   fails naming the missing marker.
2. **Given** the disclosure table, **When** a test file gains an `importorskip` for a module the
   table does not list, **Then** a test fails, so the table is extended rather than quietly
   outgrown.
3. **Given** the disclosure table, **When** it lists a module that nothing in the suite actually
   gates on, **Then** a test fails, so a stale entry cannot go on claiming a forfeit that no longer
   exists.
4. **Given** the tests are run against an installed distribution with no source tree, **When** the
   README checks cannot find a README, **Then** they skip rather than fail — the rule already held
   for every other documentation check here.

### Edge Cases

- **The suite is run with `-p no:cacheprovider`, `-q`, `-x`, or under `xdist`.** The note is a
  report about the environment, not about results, so it must not depend on how the run was
  invoked or on any test having executed.
- **The whole run fails or is interrupted.** The note is about what the environment could not
  prove; a red run's reader needs it no less. It must not be suppressed by failures, but it must
  also never be mistaken for the cause of them.
- **A driver is importable but broken** (present, raises on import). It counts as absent for
  disclosure purposes — that is what the gated tests themselves will do — and the note must not
  itself raise.
- **`pytest` is run from a directory other than the repo root.** The README checks resolve the
  repository from the test file's own location, never the process CWD, exactly as
  `test_repo_hygiene.py` does.
- **Someone later adds `pika` to the registry install step** (the ticket's option 1). Nothing here
  needs undoing: the note disappears because the driver is present, and the README paragraph
  describes a bare install, which is still accurate.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-043**: A completed run of the test suite MUST report, in its own terminal output, every
  optional driver that is absent from the environment, and for each one MUST name the coverage that
  its absence forfeits. `pika` MUST be one of these entries and its forfeited coverage MUST name
  FR-022 and the fact that the check was against a test double rather than the real driver.
- **FR-043a** *(added in fix round 1, from review 1's Med-2)*: An entry MUST be judged by the
  condition its own tests are gated on, which is not always "does this module import". The
  real-Postgres entry MUST be reported missing unless psycopg is importable **and** either
  `$TOKENWEIR_TEST_DSN` is set or `pgserver` is importable — the same condition `tests/conftest.py`'s
  fixtures impose. Deciding it on the import alone made the largest forfeit in the suite disappear
  from a green run on any machine with the driver and no server, which is this story's own bug one
  entry over. An entry's wording MUST remain true in that case, so it MUST NOT be phrased as "X is
  not installed" where X may well be installed.

  *One residual is accepted (review 2, Low-1).* `pgserver` that imports but fails to **start** still
  goes unreported: seeing that would mean starting a database from inside a reporting hook on every
  run, which costs more than the rare under-report — and that case already carries a loud skip reason
  naming the failure. The condition above is therefore the fixtures' condition as far as it can be
  known without side effects, not beyond.
- **FR-044**: The report MUST be a small, fixed number of lines that names the forfeited claims —
  not a per-test enumeration. `pytest -ra` is explicitly rejected as the mechanism (see Context).
- **FR-045**: The report MUST NOT appear for a driver that is present, and MUST NOT appear at all
  when every optional driver is present.
- **FR-046a** *(added in fix round 2, from review 2's Med-2)*: The guarantee in FR-046 MUST hold for
  the whole report, not only for the import probe. Any failure while deciding what is missing or
  rendering it MUST be contained and reported as one line, leaving the run's exit status alone. The
  original text was satisfied by a guarded probe while an unguarded predicate elsewhere produced an
  `INTERNALERROR` and a non-zero exit on a run whose tests had all passed — and the `psycopg` entry
  is already such a predicate, so this was one entry from being live. A report about the environment
  is never the reason a run fails.
- **FR-046**: The report MUST NOT alter the run's exit status, MUST NOT fail or error any test, and
  MUST NOT raise for an import that fails in any ordinary way — including one that raises something
  other than `ImportError`, and including `SystemExit`, which a module calling `sys.exit()` at import
  time really does produce and which does not inherit from `Exception`.

  *Amended in fix round 1, from review 1's Low-6.* The original wording ("MUST NOT raise even if a
  driver's import raises something other than `ImportError`") reads literally as `except
  BaseException`, which would swallow `KeyboardInterrupt`. That is the wrong trade: finishing a
  report is not worth ignoring Ctrl-C, and a run the operator asked to stop must stop. So
  `KeyboardInterrupt` deliberately propagates, and this is written down rather than left looking
  like an oversight.
- **FR-047**: The report MUST be produced regardless of the run's outcome, of which tests were
  selected, and of the reporting flags in use.
- **FR-048**: `README.md` under "Develop" MUST state which optional drivers a bare
  `pip install -e .` omits and what each omission forfeits, MUST state that this is accepted
  deliberately, and MUST give the reason — that the core is dependency-light and transport-free by
  contract (ADR-0001 Pillar 2), so the drivers cannot be made mandatory.
- **FR-049**: `README.md` MUST record, for whoever owns the MADO registry, the change that would
  close the gap on their side, so the ticket's options 1 and 2 remain actionable by the party who
  can act on them.
- **FR-050**: The load-bearing sentences of FR-048 and FR-049 MUST be guarded by tests keyed on
  marker strings, following the existing convention, so the prose stays free to change around them
  but cannot disappear. *Strengthened in fix round 2, from review 2's Med-1.* Each marker MUST be
  unique to the section it guards: two of the original six matched prose elsewhere in `README.md`
  that predates this story, so the sentences they were named for could be deleted with the suite
  green. The guard MUST also cover what each omission *forfeits*, not merely that the driver is
  named — the entire "what goes unchecked" column could be replaced with placeholder text and
  nothing noticed.
- **FR-051**: The set of drivers in the disclosure MUST be checked against the set the test suite
  actually gates on, in both directions: a gated module missing from the disclosure MUST fail, and
  a disclosed module nothing gates on MUST fail. The record MUST be able to hold an entry gated by
  something other than an `importorskip` without that check calling it stale — otherwise FR-043a's
  fix has nowhere to live. The check MUST NOT be confused by a gate that is merely *written about*
  in a docstring, a comment or a test fixture, and MUST see gates in subdirectories of `tests/`.
- **FR-052**: `pika` MUST remain absent from the project's runtime dependencies and from every
  extra other than `amqp` and `dev`; this change MUST NOT make any driver a requirement of a core
  install.
- **FR-053**: The two FR-022 real-driver tests MUST continue to skip — not fail, not be deleted,
  not be rewritten against the double — when `pika` is absent, and MUST continue to pass when it is
  present.
- **FR-054**: Documentation checks MUST skip rather than fail when run against an installed
  distribution with no source tree, per the existing rule.

### Key Entities

- **Optional driver disclosure** — the repository's single record of which importable third-party
  **Python packages** the suite gates on, and what claim goes unproven when each is missing. External
  binaries the suite also gates on — `node` for the ECMA-262 check, `git` for the hygiene checks —
  are deliberately out of scope: a binary has no import to probe, both are present everywhere this
  project runs, and widening to them is a separate concern. *(Boundary named explicitly in fix round
  2, from review 2's Low-2, so the note's silence about them is known rather than accidental.)* It is what the
  terminal note is rendered from and what the consistency check in FR-051 is checked against, so
  the log, the check and the README cannot disagree.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-036**: Running the authoritative test command in a bare-install environment produces output
  from which a reader can name, without opening any other file, the claim the run did not test. The
  baseline for this is the run recorded in Context: green, with the fact absent from the output.
- **SC-037**: The disclosure adds a small, bounded number of lines to the run's output: one per
  entry plus a fixed header and footer, and an absolute ceiling besides, so it cannot grow into the
  wall of text `-ra` was rejected for. *Restated in fix round 1, from review 1's Low-2* — the
  original claimed "an order of magnitude fewer" than `-ra`'s measured 43, and the note is 9, which
  is 4.8×. The number was wrong, and the check backing it (one line per entry) was satisfied at any
  size, so it is now bounded absolutely as well.
- **SC-038**: Removing any load-bearing README sentence, adding an unlisted gated module, listing a
  module nothing gates on, **or breaking the predicate that decides whether an entry is missing**
  each turns the suite red. Verified by making each change and observing the failure, not by
  asserting the test exists. The last of these was added in fix round 1: review 1 showed that
  rewriting the predicate to disclose every entry — so a machine with pika 1.4.4 printed "pika is
  not installed" — left the whole suite green.
- **SC-039**: In an environment with every optional driver present, the run's output is free of any
  disclosure line, and the two FR-022 tests execute and pass.
- **SC-040**: The run's pass/fail outcome and exit status in a bare-install environment are
  unchanged from the recorded baseline of 814 passed / 81 skipped / exit 0, apart from the tests
  this story adds.
- **SC-041**: A core install (`pip install -e .`) still pulls in no transport or database driver —
  checked against the dependency declarations, not merely asserted.

## Assumptions

- **The registry stays as it is for the life of this story.** The authoritative install remains
  `pip install -e . pytest`. If someone later takes option 1 or 2, nothing here breaks; the
  disclosure simply goes quiet, which is the correct behaviour and is covered by SC-039.
- **`/etc/mado/projects.yaml` is genuinely out of reach, not merely unmounted by accident.** Checked
  directly (see Context). If it turns out to be reachable through some path not evident in the pod,
  option 1 would be preferable to option 3 and this story should be revisited — but it is not
  reachable from here, and guessing at a remote edit is worse than handing the option over in
  writing.
- **The forfeited-coverage descriptions are written by hand and are prose.** No mechanism can
  derive "FR-022 is unproven" from the absence of a module, so FR-051 checks that the *set* of
  modules is right and leaves the descriptions to review. This is a known and accepted limit: it
  catches the drift that happens by accident and not the entry that was always wrong.
- **`pgserver` and `psycopg` are treated as one entry from the reader's point of view**, since
  either alone is not enough to run the real-store suite and the existing skip reason already
  explains both.
