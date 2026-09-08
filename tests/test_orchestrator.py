"""The phase taxonomy and the injection contract (TOKWEIR-8).

These tests are mostly tables, because the thing under test is mostly a vocabulary.
The interesting cases are the three edges the spec argues about:

- **one label per phase** (SC-001) — the point of having a taxonomy at all;
- **nothing is lost** (SC-004) — an unrecognized phase reaches the record anyway,
  because a wrong vocabulary should cost a report a non-canonical row, not cost the
  store an attribution;
- **producers raise where consumers tolerate** — the asymmetry is deliberate and is
  asserted from both sides, since a later "consistency" edit that made them behave
  alike would silently take one of the two properties away.
"""

import ast
import subprocess
import sys
from pathlib import Path

import pytest

from tokenweir.contract import PricingMode
from tokenweir.orchestrator import (
    ATTRIBUTION_ENV,
    PhaseKind,
    attribution_env,
    is_canonical_phase,
    normalize_phase,
    phase_label,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- the taxonomy ----------------------------------------------------------


def test_the_kinds_are_the_lifecycle_the_orchestrator_names():
    """FR-001. `mado-phase --phase` documents "one of: implementation, review, fix",
    and this set is that set. It is closed by decision: a fourth kind is an edit here
    plus a look at whether the orchestrator really emits it, not something a caller
    can introduce by writing a new string."""
    assert {member.value for member in PhaseKind} == {"implementation", "review", "fix"}


@pytest.mark.parametrize(
    "written",
    ["1st review", "review 1", "Review #1", "REVIEW-1", "review_1", "  review  1  "],
)
def test_one_phase_has_one_label(written):
    """SC-001 and US1 AC1. Five spellings of one phase, one label — which is the whole
    reason the taxonomy exists: a report grouping by phase must not see five lanes
    where the run had one."""
    assert normalize_phase(written) == "review-1"


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("1st bug fix", "fix-1"),
        ("bugfix 1", "fix-1"),
        ("bug fix", "fix"),
        ("bugfix", "fix"),
        ("implement", "implementation"),
        ("Implementation", "implementation"),
        ("2nd fix", "fix-2"),
        ("fix-2", "fix-2"),
        ("3rd review", "review-3"),
        ("review 12", "review-12"),
    ],
)
def test_the_aliases_and_forms_the_taxonomy_reads(written, expected):
    """FR-003, US1 AC2. `bug fix` is the story's own wording ("1st bug fix") and
    `implement` is what a hand-written stamp says; both resolve rather than falling
    through to the unrecognized path, where they would survive as second spellings of
    lanes that already exist."""
    assert normalize_phase(written) == expected


@pytest.mark.parametrize("kind", [member.value for member in PhaseKind])
def test_a_bare_kind_keeps_no_occurrence(kind):
    """FR-004, US1 AC3. Absent and first are different claims. Writing `review-1` for
    an orchestrator that does not count its passes would make an unknown look like a
    fact, and nothing downstream could tell which it was."""
    assert normalize_phase(kind) == kind


@pytest.mark.parametrize(
    "blank",
    [
        None,
        "",
        "   ",
        "\t\n",
        " ",
        # Review 3, Med-2: separator-only. `_collapse` folds these to nothing, so they
        # are the same claim as `""` written with different characters — and the two
        # ends must agree about that, or a phase goes missing between them.
        "_",
        "#",
        "__ ##",
        "_ #",
    ],
)
def test_blank_is_no_phase_and_not_a_label(blank):
    """FR-006, and TOKWEIR-7's FR-022 preserved. An orchestrator exporting
    `MADO_PHASE=""` is saying "no phase"; turning that into a label would say the
    field was populated."""
    assert normalize_phase(blank) is None


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("deploy", "deploy"),
        ("deploy step", "deploy step"),
        ("  deploy   step  ", "deploy step"),
        ("deploy_step", "deploy step"),
        ("review-0", "review-0"),
        ("review 0", "review 0"),
        ("11st fix", "11st fix"),
        ("1th fix", "1th fix"),
        ("triage 2", "triage 2"),
        ("review -3", "review -3"),
        ("review - 3", "review - 3"),
        ("0th review", "0th review"),
    ],
)
def test_an_unrecognized_phase_is_kept_as_written(written, expected):
    """FR-005, SC-004. The ACP lifecycle may grow a phase before this library hears
    about it. A record carrying `deploy` is worth more than a record carrying nothing,
    so the value survives with its separators folded and nothing else changed — and it
    is emphatically not coerced into a kind it is not.

    `review-0` is in this list on purpose: it names a real kind and an occurrence that
    does not exist, so it is preserved rather than repaired into `review` or `review-1`,
    either of which would be this library inventing a fact."""
    assert normalize_phase(written) == expected
    assert not is_canonical_phase(normalize_phase(written))


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("4th review", "review-4"),
        ("11th fix", "fix-11"),
        ("12th fix", "fix-12"),
        ("13th fix", "fix-13"),
        ("21st review", "review-21"),
        ("22nd review", "review-22"),
        ("23rd review", "review-23"),
        ("101st fix", "fix-101"),
        ("111th fix", "fix-111"),
    ],
)
def test_english_ordinals_are_read_including_the_teens(written, expected):
    """FR-003. The teens are the case a naive rule gets wrong: 11 takes `th`, not
    `st`, and 21 takes `st` again."""
    assert normalize_phase(written) == expected


@pytest.mark.parametrize("written", ["11st fix", "12nd fix", "13rd fix", "21th fix", "1th fix"])
def test_a_malformed_ordinal_is_not_read_as_a_number(written):
    """FR-003. The suffix is validated, not stripped. A stripper reads `11st` as 11 and
    accepts a typo as an occurrence — a near-miss that lands in a report looking
    exactly like a real phase."""
    assert not is_canonical_phase(normalize_phase(written))


@pytest.mark.parametrize(
    ("canonical", "expected"),
    [
        ("review", True),
        ("review-1", True),
        ("implementation", True),
        ("fix-99", True),
        ("review-0", False),
        ("review-01", False),
        ("Review-1", False),
        ("deploy", False),
        ("", False),
        (None, False),
    ],
)
def test_is_canonical_phase_answers_which_outcome_normalization_produced(canonical, expected):
    """FR-005. This is how a caller tells a recognized label from a preserved unknown
    without normalization having to return a pair for the sake of the rarer case.

    `review-01` is False deliberately: it is a second spelling of `review-1`, and
    admitting it would reintroduce the duplicate lane the taxonomy removes."""
    assert is_canonical_phase(canonical) is expected


# --- phase_label, the producer's constructor -------------------------------


@pytest.mark.parametrize(
    ("kind", "occurrence", "expected"),
    [
        ("review", 2, "review-2"),
        (PhaseKind.FIX, 1, "fix-1"),
        ("bug fix", 3, "fix-3"),
        ("implementation", None, "implementation"),
        (PhaseKind.REVIEW, None, "review"),
    ],
)
def test_phase_label_builds_the_canonical_form(kind, occurrence, expected):
    """FR-002."""
    label = phase_label(kind, occurrence)
    assert label == expected
    assert is_canonical_phase(label)


@pytest.mark.parametrize("occurrence", [0, -1, 1.5, "1", True])
def test_phase_label_rejects_an_occurrence_that_is_not_one(occurrence):
    """FR-007, FR-012. A producer is a program with a bug to fix, so it hears about it.

    `True` is in the list because `bool` is an `int` subclass: `phase_label("review",
    True)` meaning `review-1` would be a coincidence of Python's type lattice rather
    than something a caller meant."""
    with pytest.raises(ValueError):
        phase_label("review", occurrence)


@pytest.mark.parametrize("kind", ["deploy", "", "   ", None, 3])
def test_phase_label_rejects_a_kind_outside_the_taxonomy(kind):
    """FR-012. The tolerance for unknown phases belongs to the reader; a producer
    naming a kind the lifecycle does not have is telling us something is wrong at its
    end, and the round trip is what makes `normalize_phase` free to be forgiving."""
    with pytest.raises(ValueError):
        phase_label(kind)


# --- the injection block ---------------------------------------------------


def test_the_variable_names_are_the_four_the_adr_names():
    """FR-008. Spelled once, here, so a producer and the hook cannot drift apart on a
    name — the drift being silent, since a mistyped variable makes the hook read
    `None` and the record simply comes out unattributed."""
    assert set(ATTRIBUTION_ENV.values()) == {
        "MADO_ISSUE_KEY",
        "MADO_PHASE",
        "MADO_STREAM_ID",
        "MADO_PRICING_MODE",
    }


def test_the_block_carries_every_value_it_was_given():
    """FR-009, US2 AC1."""
    block = attribution_env(
        issue_key="TOKWEIR-8",
        phase=phase_label("fix", 2),
        stream_id="stream-17",
        pricing_mode=PricingMode.SUBSCRIPTION,
    )
    assert block == {
        "MADO_ISSUE_KEY": "TOKWEIR-8",
        "MADO_PHASE": "fix-2",
        "MADO_STREAM_ID": "stream-17",
        "MADO_PRICING_MODE": "subscription",
    }


def test_the_block_always_has_all_four_keys():
    """FR-010, US2 AC2. The one that matters: a block that omitted what it had nothing
    to say about would leave the *previous* iteration's phase standing during this one,
    and the record would be well-formed, plausible and wrong."""
    block = attribution_env(issue_key=None)
    assert set(block) == set(ATTRIBUTION_ENV.values())
    assert block["MADO_ISSUE_KEY"] == ""
    assert block["MADO_PHASE"] == ""
    assert block["MADO_STREAM_ID"] == ""


def test_the_block_exports_every_value_as_a_string():
    """FR-010. `os.environ` accepts nothing else, and a caller doing
    `env.update(attribution_env(...))` should not have to find that out at export time."""
    block = attribution_env(issue_key="TOKWEIR-8", phase=PhaseKind.REVIEW, stream_id=None)
    assert all(isinstance(key, str) and isinstance(value, str) for key, value in block.items())


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("1st review", "review-1"),
        ("bugfix 2", "fix-2"),
        (PhaseKind.IMPLEMENTATION, "implementation"),
    ],
)
def test_the_block_canonicalizes_the_phase_on_the_way_out(written, expected):
    """FR-011. The orchestrator cannot export a non-canonical label by accident, which
    is the cheapest place to enforce the taxonomy — one end, before the value exists."""
    assert attribution_env(issue_key="TOKWEIR-8", phase=written)["MADO_PHASE"] == expected


def test_the_block_passes_an_unrecognized_phase_through():
    """FR-005 at the producer. A kind the lifecycle grew is not a producer bug in the
    way a blank value is: the caller said something specific, and refusing it would
    stop the orchestrator exporting a phase this library simply has not met yet."""
    assert attribution_env(issue_key="TOKWEIR-8", phase="deploy")["MADO_PHASE"] == "deploy"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issue_key", "   "),
        ("stream_id", "  "),
        ("phase", "   "),
        # Review 3, Med-2. The producer judged blankness with `str.strip()` while the
        # reader judged it with `_collapse`, and the gap between the two definitions was
        # exactly the separator characters: `"_"` passed this check, normalized to
        # `None`, and was exported as `''` — a phase lost at both ends, silently, which
        # is the one thing this module says must never happen.
        ("phase", "_"),
        ("phase", "#"),
        ("phase", "__ ##"),
    ],
)
def test_the_block_rejects_a_value_that_is_present_but_blank(field, value):
    """FR-012. Blank-but-present is a value that was computed and came out empty. The
    hook would read it back as "unknown" and nothing would ever say the computation
    failed, so the producer is told instead."""
    kwargs = {"issue_key": "TOKWEIR-8", field: value}
    with pytest.raises(ValueError):
        attribution_env(**kwargs)


def test_the_block_rejects_a_pricing_mode_that_is_not_one():
    """FR-012. The hook falls back to `subscription` for an unrecognized mode because it
    may not lose a turn; a producer that asked for `flat_rate` should hear that no such
    mode exists rather than have it quietly become something else."""
    with pytest.raises(ValueError):
        attribution_env(issue_key="TOKWEIR-8", pricing_mode="flat_rate")


@pytest.mark.parametrize("field", ["issue_key", "stream_id"])
def test_the_block_treats_none_as_unknown_rather_than_as_an_error(field):
    """FR-013. Absent and unusable are different, and only one of them is a bug."""
    kwargs = {"issue_key": "TOKWEIR-8", field: None}
    block = attribution_env(**kwargs)
    assert block[ATTRIBUTION_ENV[field]] == ""


def test_the_block_defaults_to_the_mode_this_capture_path_exists_for():
    """FR-009. Subscription is the whole reason the Stop hook exists (ADR-0001 Pillar
    4); a caller should not have to say so on every iteration."""
    assert attribution_env(issue_key="TOKWEIR-8")["MADO_PRICING_MODE"] == "subscription"


# --- the round trip, which is the story's acceptance -----------------------


@pytest.mark.parametrize(
    ("issue_key", "phase", "stream_id", "expected_phase"),
    [
        ("TOKWEIR-8", phase_label("fix", 2), "stream-17", "fix-2"),
        ("TOKWEIR-8", "1st review", "stream-17", "review-1"),
        ("MADO-256", PhaseKind.IMPLEMENTATION, None, "implementation"),
    ],
)
def test_a_record_carries_the_issue_key_and_phase_that_were_injected(
    monkeypatch, issue_key, phase, stream_id, expected_phase
):
    """SC-003 and US3 AC1/AC2 — the story's acceptance ("emitted records carry the
    correct issue key and phase for the iteration"), end to end across the two halves.

    Imported here rather than at module scope so that the taxonomy's own tests do not
    drag the hook in; this is the one test that is deliberately about both."""
    from tokenweir.claude_code import attribution_from_env

    for name, value in attribution_env(
        issue_key=issue_key, phase=phase, stream_id=stream_id
    ).items():
        monkeypatch.setenv(name, value)

    attribution = attribution_from_env()
    # The builder canonicalizes on the way out, so the block alone would pass with the
    # hook's normalization removed — which is what review 1 said about this test. The
    # hook half is proven by the raw re-export below and by the hook suite's own cases.
    assert attribution["workload"] == issue_key
    assert attribution["queue"] == expected_phase
    assert attribution["parent_request_id"] == stream_id
    assert attribution["pricing_mode"] is PricingMode.SUBSCRIPTION

    raw = phase.value if isinstance(phase, PhaseKind) else phase
    monkeypatch.setenv(ATTRIBUTION_ENV["phase"], raw)
    assert attribution_from_env()["queue"] == expected_phase, (
        "a phase exported without going through the builder — an orchestrator "
        "written before this contract — must still reach the record canonical"
    )


def test_an_exported_block_clears_the_previous_iterations_phase(monkeypatch):
    """FR-010, end to end. The failure this story exists to prevent: iteration N+1's
    record wearing iteration N's phase. Exporting the whole block is what makes it
    impossible, so it is tested against the reader rather than only against the dict."""
    from tokenweir.claude_code import attribution_from_env

    for name, value in attribution_env(
        issue_key="TOKWEIR-8", phase=phase_label("review", 1), stream_id="stream-17"
    ).items():
        monkeypatch.setenv(name, value)
    assert attribution_from_env()["queue"] == "review-1"

    for name, value in attribution_env(issue_key="TOKWEIR-8", stream_id="stream-17").items():
        monkeypatch.setenv(name, value)
    assert attribution_from_env()["queue"] is None


@pytest.mark.parametrize("written", ["review -3", "fix -1", "review - 3"])
def test_a_negative_occurrence_is_not_read_as_a_positive_one(written):
    """Review 1, Med-2. A separator class that swallowed the sign turned `review -3`
    into `review-3` — a canonical label for a phase that never happened, which
    `is_canonical_phase` then vouched for so nothing warned about it. The spec's Edge
    Cases put a negative occurrence on the preserved path, and this is that."""
    normalized = normalize_phase(written)
    assert normalized == _collapse_for_test(written)
    assert not is_canonical_phase(normalized)


def _collapse_for_test(value: str) -> str:
    """What preservation is allowed to change: separators, nothing else."""
    return " ".join(value.replace("_", " ").replace("#", " ").split())


@pytest.mark.parametrize(
    "phase",
    [
        "review-0",
        "review 0",
        "0th fix",
        "fix-0",
        # Review 2, Med-1: the signed spellings reached neither guard. `f"{kind} {n}"`
        # with a counter running backwards is the same producer bug as `n == 0`, and
        # the spec makes no distinction between the two — so neither does the parser.
        "review -1",
        "fix -0",
        "review - 3",
        "review--2",
    ],
)
def test_the_block_rejects_a_kind_with_an_impossible_occurrence(phase):
    """FR-012, review 1 High-1. The spec's Edge Cases: "the label is rejected at the
    producer and left as written at the consumer", and the README promises the same to
    whoever wires the `mado` side.

    This is the case that matters most in practice, because the way to produce it is an
    off-by-one round counter — a live bug in the producer, exporting a label that then
    splits a report lane in exactly the way the taxonomy was built to prevent."""
    with pytest.raises(ValueError, match="occurrence"):
        attribution_env(issue_key="TOKWEIR-8", phase=phase)


@pytest.mark.parametrize("phase", ["review-0", "review 0", "0th fix", "review -1", "fix -0"])
def test_the_consumer_keeps_what_the_producer_refuses(phase):
    """The other half of the same rule, asserted alongside it so a later edit that
    "made them consistent" would have to take one of the two properties away in plain
    sight. The hook may not fail a session over a label, whatever the producer thinks
    of it."""
    assert normalize_phase(phase) is not None
    assert not is_canonical_phase(normalize_phase(phase))


@pytest.mark.parametrize("phase", ["deploy -1", "triage 0", "review +1"])
def test_the_block_passes_a_signed_occurrence_on_an_unknown_kind_through(phase):
    """The bound on the rule above. A sign only makes a phase a *producer* bug when the
    kind is one the taxonomy knows — `deploy -1` is a phase this library has not met,
    spelled however its own orchestrator spells it, and refusing it would be this
    library legislating for a lifecycle it does not define.

    `review +1` is in this list deliberately: it is a positive occurrence in a spelling
    nobody uses, and FR-012 is about occurrences that cannot exist, not about spellings
    we would rather they had not chosen."""
    exported = attribution_env(issue_key="TOKWEIR-8", phase=phase)[ATTRIBUTION_ENV["phase"]]
    assert exported == normalize_phase(phase), "an unknown phase is passed on, not edited"


@pytest.mark.parametrize("phase", [7, 7.0, ["review"], object()])
def test_the_block_rejects_a_phase_that_is_not_a_phase(phase):
    """Review 1, Low-4. The documented failure is `ValueError`; without a guard this
    surfaced as an `AttributeError` about `.replace`, which tells a caller nothing
    about phases."""
    with pytest.raises(ValueError, match="phase"):
        attribution_env(issue_key="TOKWEIR-8", phase=phase)


def test_the_contract_mapping_cannot_be_edited_by_a_caller():
    """Review 2, Low-2. The names are re-exported at package level, so a plain dict
    would be one assignment away from redirecting producer *and* consumer together —
    SC-005's drift arriving through the mechanism meant to prevent it."""
    with pytest.raises(TypeError):
        ATTRIBUTION_ENV["phase"] = "SOMETHING_ELSE"  # type: ignore[index]


@pytest.mark.parametrize("field", ["issue_key", "stream_id"])
def test_the_block_strips_surrounding_whitespace(field):
    """Review 2, Low-3 — behaviour that existed but was neither documented nor tested.
    `' TOKWEIR-8 '` and `'TOKWEIR-8'` are the same issue to a reader and two different
    strings to anything grouping by the column: the duplicate-lane problem the phase
    taxonomy exists to solve, one field over."""
    kwargs = {"issue_key": "TOKWEIR-8", field: "  spaced  "}
    assert attribution_env(**kwargs)[ATTRIBUTION_ENV[field]] == "spaced"


def test_the_hook_resolves_the_variable_names_from_the_contract():
    """SC-005, review 1 Med-1. "Producer **and consumer** both resolve them from that
    one definition" — which was not true when the hook spelled all four by hand, and
    the contract module's docstring said it was.

    Checked against the source rather than behaviour: two independent hard-codings
    agree right up until someone edits one of them, and the round-trip test would then
    fail with no indication of why."""
    source = (REPO_ROOT / "src" / "tokenweir" / "claude_code.py").read_text()
    tree = ast.parse(source)
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in set(ATTRIBUTION_ENV.values())
    }
    assert not literals, (
        f"the hook spells {sorted(literals)} itself; the names live in "
        "ATTRIBUTION_ENV so that producer and consumer cannot drift apart"
    )


# --- isolation -------------------------------------------------------------


def test_the_contract_module_imports_nothing_but_the_stdlib_and_the_contract():
    """SC-006, FR-014. The orchestrator runs in another codebase and another namespace;
    depending on this contract must not mean inheriting a capture path or a transport
    driver. Read from the source rather than from `sys.modules`, so the check sees what
    the module *asks for* — a session that already imported everything cannot hide a
    new import from it."""
    tree = ast.parse((REPO_ROOT / "src" / "tokenweir" / "orchestrator.py").read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module)

    for module in imported:
        root = module.split(".")[0]
        if root == "tokenweir":
            assert module == "tokenweir.contract", (
                f"the contract module imports {module}; only tokenweir.contract is "
                "permitted, and importing the hook would reverse the dependency"
            )
        else:
            assert root in sys.stdlib_module_names, f"{module} is not in the standard library"


def test_importing_the_contract_module_does_not_import_the_hook():
    """FR-014. The dependency runs one way. Checked in a subprocess because this
    session has already imported the hook several times over."""
    code = (
        "import sys; import tokenweir.orchestrator; "
        "assert 'tokenweir.claude_code' not in sys.modules, 'the hook was imported'; "
        "assert 'pika' not in sys.modules and 'psycopg' not in sys.modules; "
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


# --- the documented contract -----------------------------------------------


def _readme() -> str:
    """The README, or a skip. An sdist install has neither it nor `tests/`, which is
    the rule `test_repo_hygiene.py` and `test_optional_drivers.py` already follow."""
    path = REPO_ROOT / "README.md"
    if not path.exists():
        pytest.skip("no README.md in this install; the source tree is not present")
    return path.read_text(encoding="utf-8")


def test_the_readme_documents_the_taxonomy():
    """FR-018. The half of this story that a person consumes rather than imports. The
    canonical label grammar and at least one accepted spelling are checked by content,
    not by heading — a section that named the feature and explained none of it would
    satisfy a grep and none of the requirement."""
    readme = _readme()
    for kind in PhaseKind:
        assert kind.value in readme
    assert "review-1" in readme
    assert "1st review" in readme
    assert "bug fix" in readme


def test_the_readme_states_the_orchestrators_obligation():
    """FR-019. Per iteration, every variable, before the turn — the three parts a
    partial implementation on the `mado` side would get wrong."""
    readme = _readme()
    for name in ATTRIBUTION_ENV.values():
        assert name in readme
    assert "attribution_env" in readme
    assert "every iteration" in readme


def test_the_readme_says_the_orchestrator_change_lives_elsewhere():
    """FR-020. The largest scope judgement in this story, and the one a reader is most
    likely to get wrong in the optimistic direction: nothing here makes MADO export
    anything, and a green suite must not be read as saying otherwise."""
    readme = _readme()
    assert "not in this repository" in readme
    assert "services/orchestrator/" in readme
