"""The subscription/API-key token-parity harness — ADR-0001 item 6 (TOKWEIR-9).

ADR-0001 Pillar 4 captures Claude Max usage from Claude Code's own transcript,
because a subscription is OAuth-authenticated and cannot be metered by
interception. The ADR then marks one thing ``Unverified``::

    docs don't explicitly confirm token-count parity between subscription and
    API-key transcripts — the single check is to diff a Max-session transcript
    against an API-key session on the same model/context. Do this before relying
    on subscription numbers.

TOKWEIR-7 shipped the *reading* half and did not close that item: the hook
faithfully reports what a Max transcript says, which is a different claim from
those numbers being denominated in the same units the API reports and bills on.
This module is the measurement that settles it.

Why not a naive two-session diff
--------------------------------

A Claude Code turn and a bare API call are not the same request and cannot be made
so — Claude Code carries a large system prompt, a full tool schema set and cached
history that this repo may not assume the shape of. Two requests with different
inputs produce different counts *correctly*, so subtracting one from the other
proves nothing. Real transcripts make this vivid: every Max turn reports
``input_tokens: 2`` with the context sitting in ``cache_read_input_tokens``, so a
raw comparison against a bare call's ``input_tokens`` would "discover" a
discrepancy that is nothing but cache attribution.

The question is not whether two different requests agree. It is whether the
transcript's numbers **are token counts on the provider's own scale**. That is
testable against an authority both sides share — the provider's tokenizer:

Probe A (decisive)
    A transcript records both the assistant's output text and the ``output_tokens``
    it attributes to producing it. Send that same recorded text to
    ``count_tokens`` and compare. Same artifact, same tokenizer, two reporters.
    Agreement means the transcript reports real tokens; a constant ratio is a
    correction factor; noise is an approximation.

Probe B (corroborating)
    One controlled API-key ``messages`` call, to inventory the ``usage`` object the
    API itself returns and diff its field names against a transcript's.

Probe C (corroborating, offline)
    Arithmetic identities a passed-through count satisfies and a synthesized one
    generally does not — see :func:`consistency_checks`. Needs no credential.

Probe A's two confounders are characterized rather than assumed away.
``count_tokens`` counts a whole message request, so a few tokens of envelope
overhead ride along — a *constant*, which shows up across turns of differing
length as an offset that does not scale, distinguishable from a ratio. And a turn
whose output includes a ``tool_use`` block has ``output_tokens`` covering a JSON
payload this harness cannot re-tokenize faithfully, so such turns are excluded from
the headline rather than averaged in. ``thinking`` blocks *are* recoverable and are
kept separately, which is what lets ``output_tokens_details.thinking_tokens`` be
checked rather than trusted. That is why the probe runs over a *population* of
turns and reports a distribution.

What this can and cannot establish
----------------------------------

It speaks to whether the counts are true tokens on the provider's scale. It says
nothing about what a subscription is *billed* — there is no per-call dollar under
a flat rate, which is Pillar 1's premise and exactly why raw tokens are stored and
dollars derived at report time. And it is **point-in-time**: ADR-0001 notes the Max
landscape is volatile, so the harness exists to be re-run, not just read.

No third-party import, at all
-----------------------------

Everything here is stdlib plus ``tokenweir`` itself, holding the line ADR-0001
Pillar 2 draws and :mod:`tokenweir.claude_code` already keeps. The obvious way to
call the API is the vendor SDK; for two unstreamed JSON POSTs it buys nothing worth
becoming the package's first third-party dependency for.

This is a spike harness, not part of the capture path. It is never imported by the
hook and never runs on a metered request's critical path.
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

# Intra-package reuse, deliberately, and two underscore-prefixed names among them.
# That is a deliberate exception rather than an oversight: FR-002 requires this module
# not re-derive the transcript semantics that already exist — `_text` for field
# hygiene, `TokenTotals.from_mapping` for count coercion, `_COUNT_FIELDS` so the
# harness measures exactly the fields the capture path reads — and importing them is
# the only way to hold that requirement without copying three definitions that would
# then be free to drift. Both modules are inside this package; nothing here is
# re-exported to a consumer.
#
# What FR-002 cannot be discharged by import is the de-duplication rule itself, since
# `scan_transcript` folds it into a cumulative sum this module cannot use — see
# `iter_turns` for why, and for the test that keeps the two from drifting.
from tokenweir.claude_code import _COUNT_FIELDS, TokenTotals, _text

__all__ = [
    "ANTHROPIC_VERSION",
    "BILLING_FIELDS",
    "CheckResult",
    "FieldComparison",
    "ProbeError",
    "ProbeResult",
    "ApiProbe",
    "Turn",
    "compare_usage",
    "consistency_checks",
    "credential",
    "is_headline_eligible",
    "iter_turns",
    "main",
]

#: The four contract token fields. Taken from the hook rather than restated, so
#: the harness measures exactly the fields the capture path actually reads.
BILLING_FIELDS: Tuple[str, ...] = _COUNT_FIELDS

#: The API version header. Pinned, because an unpinned version is a silent
#: behaviour change in a measurement that is meant to be reproducible.
ANTHROPIC_VERSION = "2023-06-01"

_DEFAULT_BASE_URL = "https://api.anthropic.com"

#: Status values shared by :class:`FieldComparison` and :class:`CheckResult`.
PARITY = "parity"
OFFSET = "offset"
ABSENT = "absent"
PASS = "pass"
FAIL = "fail"
NOT_APPLICABLE = "not_applicable"


# --- reading a transcript, one turn at a time ------------------------------


@dataclass(frozen=True, slots=True)
class Turn:
    """One assistant response, with its usage and the text it produced.

    This is the pairing :func:`~tokenweir.claude_code.scan_transcript` throws
    away. That function needs a session total and returns cumulative
    :class:`~tokenweir.claude_code.TokenTotals`; Probe A needs each turn's own
    usage *beside the text of that same turn*, which is the only way to ask the
    tokenizer about the identical artifact.

    ``output_text`` is the response's text **gathered across every transcript line
    that carries it**, not one line's worth — see :func:`iter_turns` for why the
    difference is the whole ballgame.

    ``thinking_text`` is kept **separately** rather than folded into
    ``output_text``, because the two are counted separately by the provider:
    ``output_tokens_details.thinking_tokens`` reports the thinking half on its own.
    Keeping them apart is what lets the harness check that field independently
    instead of taking it on trust.

    ``has_unaccounted_blocks`` is what keeps the headline honest. ``output_tokens``
    covers every block the model emitted, and a ``tool_use`` block's tokens cannot
    be recovered from the transcript — the block is a JSON payload whose
    tokenization this harness has no way to reproduce faithfully. A turn carrying
    one is therefore expected to under-count for a reason that has nothing to do
    with parity, so it is flagged and excluded rather than averaged in. ``text``
    and ``thinking`` are *accounted* blocks: their content is recoverable, so they
    do not set the flag.
    """

    message_id: Optional[str]
    model: Optional[str]
    timestamp: Optional[str]
    usage: Mapping[str, Any]
    output_text: str
    thinking_text: str = ""
    has_unaccounted_blocks: bool = False

    @property
    def thinking_tokens(self) -> int:
        """What the response *claims* it spent thinking, or 0.

        A claim, deliberately named as one: the harness's job is to check it
        against the tokenizer, not to adopt it.
        """
        details = self.usage.get("output_tokens_details")
        if isinstance(details, Mapping):
            value = _numeric(details.get("thinking_tokens"))
            if value is not None:
                return value
        return 0


def _blocks(message: Mapping[str, Any]) -> Tuple[str, str, bool]:
    """Split a message's content into text, thinking, and "something else".

    A ``content`` that is a plain string is Claude Code's older shape and is taken
    at face value. A list is the current one: ``text`` and ``thinking`` blocks
    contribute their recoverable content, and anything else — ``tool_use``,
    ``redacted_thinking``, a block that is not a mapping, a block whose text field
    is not a string — sets the flag instead of silently contributing nothing.

    ``redacted_thinking`` counts as unaccounted on purpose: it has tokens and no
    recoverable content, which is precisely the case the flag exists for.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content, "", False
    if not isinstance(content, Sequence):
        return "", "", False

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    unaccounted = False
    for block in content:
        if not isinstance(block, Mapping):
            unaccounted = True
            continue
        kind = block.get("type")
        if kind == "text":
            value = block.get("text")
            if isinstance(value, str):
                text_parts.append(value)
            else:
                unaccounted = True
        elif kind == "thinking":
            value = block.get("thinking")
            # An **empty** thinking block is unaccounted, not accounted-and-zero.
            # Claude Code's transcripts carry `{"type": "thinking", "thinking": ""}`
            # while still reporting a non-zero
            # `output_tokens_details.thinking_tokens` — the content is stripped, the
            # tokens are real. Treating that as recoverable is what let four turns
            # claiming 71 to 503 thinking tokens present themselves as text-only,
            # which would have corrupted the headline in precisely the way the
            # sibling-line bug did.
            if isinstance(value, str) and value:
                thinking_parts.append(value)
            else:
                unaccounted = True
        else:
            unaccounted = True
    return "".join(text_parts), "".join(thinking_parts), unaccounted


@dataclass
class _Accumulator:
    """One response under construction, gathered across the lines that carry it."""

    model: Optional[str]
    timestamp: Optional[str]
    usage: Mapping[str, Any]
    parts: list[str] = field(default_factory=list)
    thinking_parts: list[str] = field(default_factory=list)
    has_unaccounted_blocks: bool = False


def iter_turns(path: Any) -> Iterator[Turn]:
    """Yield each counted assistant response of a Claude Code transcript.

    One API response can appear as **several** transcript lines. The hook's scan
    (:func:`~tokenweir.claude_code.scan_transcript`) handles that by counting the
    first line for a given ``message.id`` and skipping the rest, which is exactly
    right for its purpose: every line repeats the same ``message.usage``, so
    summing them would over-count the turn.

    For Probe A that rule is right about usage and **wrong about content**, and
    getting it wrong is not subtle — it silently produces enormous fake
    discrepancies. Those sibling lines do not repeat the content; they *divide* it.
    A single response is written as one line carrying its ``text`` block and
    another carrying its ``tool_use`` block, while both carry the whole response's
    aggregate ``output_tokens``. Take the first line's content and pair it with
    that aggregate and you are comparing 20 characters of text against the token
    count for a 37 KB tool call — which is how the first version of this harness
    reported a transcript claiming 14,142 output tokens against a tokenizer count
    of 12, and read it as a parity failure rather than as its own bug.

    So this function accumulates: usage is taken **once** per ``message.id``, text
    is concatenated across **every** line of that id, and
    :attr:`Turn.has_unaccounted_blocks` is true if *any* of them carried a block
    whose tokens cannot be recovered. That flag is what makes the sibling structure
    visible; without the accumulation it never fired at all, because each
    individual line holds exactly one kind of block.

    Entries that are not objects, carry no message, carry no usage, or whose usage
    is not a mapping are skipped — the same tolerance the hook's scan applies,
    because a transcript is a log and a log has noise in it. An entry with no
    ``message.id`` cannot be grouped and stands as its own turn.

    Ordering is first-appearance. Responses are yielded only after the whole file
    has been read, since a response's later lines cannot be known before reaching
    them; only the text is retained, never the large tool-call payloads, so the
    cost is bounded by a session's prose rather than by its transcript. This is a
    spike harness run by hand, not the hook, which must stay streaming and lean.

    An unreadable file yields nothing rather than raising. The harness reports
    "no turns" and the operator can see that for themselves; a traceback out of an
    iterator would be a worse way to say the same thing.

    **On FR-002, which says not to re-derive these semantics.** This function does
    re-derive the filter-and-group loop, because
    :func:`~tokenweir.claude_code.scan_transcript` folds it into a cumulative total
    and offers no way to get per-response detail out. What discharges the requirement
    instead is a test:
    ``tests/test_parity.py::TestAgreementWithTheHooksOwnScan`` pins this function and
    the hook's scan to the **same response count** over a transcript carrying every
    hazard — sibling lines, malformed lines, usage-less entries — so the two cannot
    drift apart unnoticed. The duplication is a deliberate trade with a guard on it,
    not an accident; review 2 of TOKWEIR-9 asked for it to be said here rather than
    only in ``plan.md``.
    """
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return

    order: list[str] = []
    responses: dict[str, _Accumulator] = {}
    anonymous = 0

    try:
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                if not isinstance(entry, Mapping):
                    continue
                message = entry.get("message")
                if not isinstance(message, Mapping):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, Mapping):
                    continue

                identity = _text(message.get("id"))
                if identity is None:
                    # Ungroupable: it stands alone under a key no id can collide
                    # with, rather than being merged into a neighbour it may have
                    # nothing to do with.
                    anonymous += 1
                    key = f"\x00anonymous-{anonymous}"
                else:
                    key = identity

                text, thinking, unaccounted = _blocks(message)
                existing = responses.get(key)
                if existing is None:
                    order.append(key)
                    responses[key] = _Accumulator(
                        model=_text(message.get("model")),
                        timestamp=_text(entry.get("timestamp")),
                        # First occurrence wins. The siblings repeat it, so this is
                        # a choice between identical values — and taking the first
                        # keeps the usage paired with the timestamp beside it.
                        usage=usage,
                        parts=[text] if text else [],
                        thinking_parts=[thinking] if thinking else [],
                        has_unaccounted_blocks=unaccounted,
                    )
                else:
                    if text:
                        existing.parts.append(text)
                    if thinking:
                        existing.thinking_parts.append(thinking)
                    existing.has_unaccounted_blocks = (
                        existing.has_unaccounted_blocks or unaccounted
                    )
    except Exception:
        # A read that ended early. What was gathered before it is still true, and
        # yielding it beats discarding a session because its tail was unreadable.
        pass

    for key in order:
        accumulated = responses[key]
        yield Turn(
            message_id=None if key.startswith("\x00anonymous-") else key,
            model=accumulated.model,
            timestamp=accumulated.timestamp,
            usage=accumulated.usage,
            output_text="".join(accumulated.parts),
            thinking_text="".join(accumulated.thinking_parts),
            has_unaccounted_blocks=accumulated.has_unaccounted_blocks,
        )


# --- comparing two usage objects -------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """One field, as the two sides reported it."""

    name: str
    reference: Any
    observed: Any
    status: str
    difference: Optional[int] = None
    ratio: Optional[float] = None


_MISSING = object()


def _numeric(value: Any) -> Optional[int]:
    """The value as a whole number, or ``None`` if it is not one.

    Negative values pass through unchanged rather than being clamped: this is a
    *comparison* helper, and a negative count is a discrepancy worth reporting
    rather than one worth hiding. (The hook's own ``_coerce_count`` clamps, because
    it is building a record that must satisfy the contract.)

    ``bool`` is excluded on purpose: it is an ``int`` subclass in Python, and a
    ``True`` silently comparing equal to ``1`` is the kind of agreement a parity
    measurement must not manufacture.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _equal(left: Any, right: Any) -> bool:
    """Equality for the values arithmetic cannot be done on.

    Plain ``==`` would be wrong for exactly one pair of types, and wrong in the
    direction that matters: ``True == 1`` in Python, because ``bool`` subclasses
    ``int``. A field reported as ``1`` on one side and ``True`` on the other is a
    *shape* difference between the two sources — precisely the kind of thing this
    harness exists to notice — and ``==`` would file it as perfect agreement.
    Everything else compares as itself; ``"5" == 5`` is already ``False``.
    """
    if isinstance(left, bool) != isinstance(right, bool):
        return False
    return bool(left == right)


def compare_usage(
    reference: Mapping[str, Any],
    observed: Mapping[str, Any],
) -> list[FieldComparison]:
    """Compare two usage mappings field by field.

    Total over :data:`BILLING_FIELDS` — those four are always reported, even when
    neither side carries them, because "the field the hook reads was missing" is
    the single most important thing this comparison can discover and an empty
    result would be the worst possible way to say it. Every other field either
    side carries is reported too, so a Max-only field such as ``service_tier`` is
    surfaced rather than silently dropped (FR-005).

    ``status`` is :data:`ABSENT` whenever the field is missing from either side,
    which is never :data:`PARITY`: two absences are not an agreement about a
    count, and reporting them as one would let a comparison of two empty objects
    read as perfect parity (FR-004).

    ``difference`` and ``ratio`` are filled only when both sides are numeric, and
    ``ratio`` only when the reference is non-zero. Non-numeric fields still get a
    status by equality — ``service_tier`` agreeing or differing is a real finding —
    but no arithmetic is invented for them.
    """
    names: list[str] = list(BILLING_FIELDS)
    for name in list(reference) + list(observed):
        if name not in names:
            names.append(name)

    results: list[FieldComparison] = []
    for name in names:
        ref = reference.get(name, _MISSING)
        obs = observed.get(name, _MISSING)

        if ref is _MISSING or obs is _MISSING:
            results.append(
                FieldComparison(
                    name=name,
                    reference=None if ref is _MISSING else ref,
                    observed=None if obs is _MISSING else obs,
                    status=ABSENT,
                )
            )
            continue

        ref_n, obs_n = _numeric(ref), _numeric(obs)
        if ref_n is None or obs_n is None:
            results.append(
                FieldComparison(
                    name=name,
                    reference=ref,
                    observed=obs,
                    status=PARITY if _equal(ref, obs) else OFFSET,
                )
            )
            continue

        difference = obs_n - ref_n
        ratio = (obs_n / ref_n) if ref_n else None
        results.append(
            FieldComparison(
                name=name,
                reference=ref_n,
                observed=obs_n,
                status=PARITY if difference == 0 else OFFSET,
                difference=difference,
                ratio=ratio,
            )
        )
    return results


# --- Probe C: internal consistency, offline --------------------------------


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One identity, checked over a transcript's turns.

    :data:`NOT_APPLICABLE` is distinct from :data:`PASS` on purpose (FR-007). A
    check whose inputs were absent did not pass; it did not run. Collapsing the
    two would let a transcript that carries none of the fields an identity is
    about report a clean sweep of passes, which is precisely backwards as evidence.
    """

    name: str
    status: str
    detail: str
    checked: int = 0
    failures: Tuple[str, ...] = field(default_factory=tuple)


def _cache_creation_sums(turns: Sequence[Turn]) -> CheckResult:
    """``cache_creation``'s sub-fields must sum to ``cache_creation_input_tokens``."""
    checked = 0
    failures: list[str] = []
    for turn in turns:
        breakdown = turn.usage.get("cache_creation")
        total = _numeric(turn.usage.get("cache_creation_input_tokens"))
        if not isinstance(breakdown, Mapping) or total is None:
            continue
        parts = [_numeric(v) for v in breakdown.values()]
        if any(p is None for p in parts):
            continue
        checked += 1
        summed = sum(p for p in parts if p is not None)
        if summed != total:
            failures.append(
                f"{turn.message_id}: cache_creation sums to {summed}, "
                f"cache_creation_input_tokens is {total}"
            )

    if not checked:
        return CheckResult(
            name="cache_creation_sums",
            status=NOT_APPLICABLE,
            detail="no turn carried both a cache_creation breakdown and a total",
        )
    return CheckResult(
        name="cache_creation_sums",
        status=FAIL if failures else PASS,
        detail=f"{checked - len(failures)}/{checked} turns consistent",
        checked=checked,
        failures=tuple(failures),
    )


def _iterations_sum(turns: Sequence[Turn]) -> CheckResult:
    """A turn's ``iterations[]`` must sum to its top-level billing fields.

    Claude Code makes several API calls per turn and records each one's usage in
    ``iterations``. If the top-level figures are the honest aggregate of those
    calls, the two agree. If they were rounded, bucketed or synthesized, they
    generally do not — which is the point of asking.
    """
    checked = 0
    failures: list[str] = []
    for turn in turns:
        iterations = turn.usage.get("iterations")
        if not isinstance(iterations, Sequence) or isinstance(iterations, (str, bytes)):
            continue
        entries = [e for e in iterations if isinstance(e, Mapping)]
        if not entries:
            continue
        checked += 1
        summed = TokenTotals()
        for entry in entries:
            summed = summed + TokenTotals.from_mapping(entry)
        top = TokenTotals.from_mapping(turn.usage)
        if summed != top:
            failures.append(
                f"{turn.message_id}: iterations sum to {summed.as_dict()}, "
                f"top level is {top.as_dict()}"
            )

    if not checked:
        return CheckResult(
            name="iterations_sum",
            status=NOT_APPLICABLE,
            detail="no turn carried an iterations breakdown",
        )
    return CheckResult(
        name="iterations_sum",
        status=FAIL if failures else PASS,
        detail=f"{checked - len(failures)}/{checked} turns consistent",
        checked=checked,
        failures=tuple(failures),
    )


def _cache_read_monotonic(turns: Sequence[Turn]) -> CheckResult:
    """``cache_read_input_tokens`` should not go backwards within a session.

    A cache read is the context replayed from cache, and context grows as a
    session proceeds, so a pass-through count advances. A **decrease is not
    automatically a defect**: compaction, a context reset or a new cache prefix
    all legitimately shrink what is read. So this check reports where it went
    backwards and how far, and the finding interprets it — it is corroboration,
    not a verdict on its own.
    """
    values: list[Tuple[Optional[str], int]] = []
    for turn in turns:
        value = _numeric(turn.usage.get("cache_read_input_tokens"))
        if value is not None:
            values.append((turn.message_id, value))

    if len(values) < 2:
        return CheckResult(
            name="cache_read_monotonic",
            status=NOT_APPLICABLE,
            detail="fewer than two turns carried cache_read_input_tokens",
        )

    failures = [
        f"{values[i][0]}: {values[i - 1][1]} -> {values[i][1]}"
        for i in range(1, len(values))
        if values[i][1] < values[i - 1][1]
    ]
    steps = len(values) - 1
    return CheckResult(
        name="cache_read_monotonic",
        status=FAIL if failures else PASS,
        detail=f"{steps - len(failures)}/{steps} transitions non-decreasing",
        checked=steps,
        failures=tuple(failures),
    )


def consistency_checks(turns: Iterable[Turn]) -> list[CheckResult]:
    """Run Probe C's identities, each reported independently (FR-006).

    Independently, because "the transcript is internally consistent" is not one
    fact. A failing ``iterations`` sum and a non-monotonic cache read mean
    completely different things, and a single boolean would name neither.
    """
    materialized = list(turns)
    return [
        _cache_creation_sums(materialized),
        _iterations_sum(materialized),
        _cache_read_monotonic(materialized),
    ]


# --- the credential --------------------------------------------------------


def credential(
    env: Optional[Mapping[str, str]] = None,
    path: Any = None,
) -> Optional[str]:
    """The API key, from the environment or a file, or ``None`` if not configured.

    ``ANTHROPIC_API_KEY`` wins over a file so that an operator can override a
    stale file without deleting it.

    Never a command-line argument: an argument is visible in ``ps`` to every
    process on the host and lands in shell history (FR-020). A path is passed
    instead, and the file is read here.

    ``.strip()`` matters more than it looks. The operator writes this file with
    ``printf`` or a shell ``read``, and the difference between the two is exactly
    a trailing newline. A newline inside an HTTP header value fails in a way that
    reads as an auth problem rather than a formatting one, which is a bad hour to
    give someone (FR-022).

    Blank and absent return the same ``None`` (FR-023): an empty file is not a
    configured credential, and treating it as one only moves the failure to the
    server, where the message is worse.
    """
    source = os.environ if env is None else env
    value = source.get("ANTHROPIC_API_KEY")
    if value and value.strip():
        return value.strip()

    if path:
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except Exception:
            return None
        if raw.strip():
            return raw.strip()
    return None


class ProbeError(RuntimeError):
    """A network probe failed. The message is composed here, never inherited.

    Letting an arbitrary exception string propagate is how a secret ends up in a
    log: the harness cannot know what a transport packed into it. So every failure
    is re-described in this module's own words and passed through
    :func:`_scrub` before it is raised (FR-021).
    """


def _scrub(text: str, secret: Optional[str]) -> str:
    """Remove the credential from a string about to be shown to someone.

    Defence in depth. The key travels in a header and should never reach a
    message in the first place — but "should never" is an assumption about code
    this module does not own, and the cost of being wrong is a leaked credential
    in a transcript. The cost of the check is a string replace.
    """
    if not secret:
        return text
    return text.replace(secret, "***")


# --- the network probes ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """A network result, self-describing (FR-013).

    Model and request kind travel with the usage because "the same model" is half
    the story's comparison condition, and a number filed away without the
    conditions that produced it cannot be re-checked later.
    """

    kind: str
    model: str
    usage: Mapping[str, Any]
    text: str = ""
    """The text the response actually produced, for a ``messages`` result.

    Probe A′ needs it: the control is "what the API says it spent, versus what the
    tokenizer makes of what it returned", and that is unanswerable without the
    returned text. Empty for a ``count_tokens`` result, which produces none.
    """


class ApiProbe:
    """The two API calls this spike needs, over ``urllib``.

    An ``opener`` can be injected so the tests exercise every path — success,
    HTTP error, transport error, and the scrubbing — without a live call. The
    live call happens once, deliberately, when the finding is produced.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = _DEFAULT_BASE_URL,
        opener: Optional[Callable[[urllib.request.Request, float], Any]] = None,
        timeout: float = 30.0,
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("api_key is required; call credential() first")
        self._key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._opener = opener or (
            lambda request, timeout: urllib.request.urlopen(request, timeout=timeout)
        )

    def _post(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        """One POST. One attempt.

        No retry loop, deliberately (FR-014). A measurement that silently retried
        would hide a flaky answer inside a number presented as definitive, and the
        operator running a spike is right there to run it again.
        """
        url = f"{self._base_url}{endpoint}"
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "anthropic-version": ANTHROPIC_VERSION,
                "x-api-key": self._key,
            },
            method="POST",
        )
        try:
            with self._opener(request, self._timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise ProbeError(
                _scrub(f"POST {endpoint} failed: HTTP {exc.code}. {detail}".strip(), self._key)
            ) from None
        except Exception as exc:
            raise ProbeError(
                _scrub(f"POST {endpoint} failed: {type(exc).__name__}: {exc}", self._key)
            ) from None

        try:
            parsed = json.loads(body)
        except Exception:
            raise ProbeError(f"POST {endpoint} returned a body that is not JSON") from None
        if not isinstance(parsed, Mapping):
            raise ProbeError(f"POST {endpoint} returned {type(parsed).__name__}, not an object")
        return parsed

    def count_tokens(self, model: str, text: str) -> ProbeResult:
        """Probe A: what the provider's tokenizer makes of this exact text.

        ``model`` is required and never defaulted (FR-012) — the tokenizer is a
        property of the model, and a silent default would answer a question about
        a model nobody chose.
        """
        if not model or not model.strip():
            raise ValueError("model is required and is never defaulted")
        parsed = self._post(
            "/v1/messages/count_tokens",
            {"model": model, "messages": [{"role": "user", "content": text}]},
        )
        return ProbeResult(kind="count_tokens", model=model, usage=parsed)

    def messages(self, model: str, text: str, *, max_tokens: int = 16) -> ProbeResult:
        """One controlled generation — Probe B's field inventory and Probe A′'s control.

        Returns the produced text alongside the usage, because Probe A′ compares the
        two: what the API says it spent, against what the tokenizer makes of what it
        returned. Only ``text`` blocks are collected; ``thinking`` is reported
        separately by ``output_tokens_details.thinking_tokens`` and must not be
        folded in, which is the whole point of the control.

        ``max_tokens`` defaults small — Probe B only needs field names, and these are
        the only output tokens the harness ever spends.
        """
        if not model or not model.strip():
            raise ValueError("model is required and is never defaulted")
        parsed = self._post(
            "/v1/messages",
            {
                "model": model,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": text}],
            },
        )
        usage = parsed.get("usage")
        if not isinstance(usage, Mapping):
            raise ProbeError("POST /v1/messages returned no usage object")
        produced = ""
        content = parsed.get("content")
        if isinstance(content, Sequence) and not isinstance(content, (str, bytes)):
            produced = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, Mapping)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            )
        return ProbeResult(kind="messages", model=model, usage=usage, text=produced)

    def envelope(self, model: str, text: str) -> int:
        """The constant ``count_tokens`` adds for the request it wraps the text in.

        ``count_tokens`` prices a whole message request, so it returns
        ``tokens(text) + E`` for some fixed framing cost ``E``. Probe A needs ``E``
        to recover the bare text count — and guessing it, or reading it off a short
        string whose token count is itself unknown, is how a measurement acquires a
        fudge factor.

        So it is derived instead. Doubling the text gives two equations::

            count_tokens(T)   = tokens(T)     + E
            count_tokens(T+T) = 2 * tokens(T) + E

        and subtracting twice the first from the second leaves ``E = 2*c1 - c2``,
        with **no assumption about how many tokens T is**.

        It does assume one thing, which the finding states rather than buries:
        that tokenization is **additive across the join** — that ``T+T`` costs
        exactly twice ``T``. That holds for a text ending on a clean token boundary
        and can be off by a token or two otherwise, so the caller should derive it
        from a long text and treat a disagreement between two derivations as the
        signal it is. It is also why the *verdict* does not rest on ``E``: a
        difference that stays constant across a wide range of sizes already rules
        out a scale factor, whatever the framing costs.
        """
        if not model or not model.strip():
            raise ValueError("model is required and is never defaulted")
        single = _numeric(self.count_tokens(model, text).usage.get("input_tokens"))
        double = _numeric(self.count_tokens(model, text + text).usage.get("input_tokens"))
        if single is None or double is None:
            raise ProbeError("count_tokens did not return an input_tokens count")
        return 2 * single - double


# --- the reproducible command ----------------------------------------------


#: Probe A′'s control prompts, fixed so a re-run measures the same thing. Short and
#: varied in length on purpose: the control needs a few responses of differing size
#: to show its constant is a constant, and every token here is metered spend.
_CONTROL_PROMPTS: Tuple[Tuple[str, int], ...] = (
    ("Write exactly two sentences about tokenizers. No preamble.", 300),
    ("List five colours, one per line, nothing else.", 150),
    ("Name three prime numbers, comma separated.", 100),
    ("Say the word 'ok' and nothing else.", 20),
)


def is_headline_eligible(turn: Turn) -> bool:
    """Is this response comparable against the tokenizer on equal terms?

    Probe A's headline is only meaningful for a response whose ``output_tokens``
    covers content this harness can re-tokenize in full. Four conditions, and none
    of them is redundant:

    - there is text to compare at all;
    - no **unaccounted** block — a ``tool_use`` payload's tokenization cannot be
      reproduced faithfully from the transcript;
    - no thinking *text*, which is counted separately by the provider;
    - and ``thinking_tokens == 0``, which is the belt-and-braces guard rather than a
      restatement of the third. A response can claim thinking tokens with **no**
      recoverable thinking block at all — Claude Code writes
      ``{"type": "thinking", "thinking": ""}`` and still reports a non-zero count.
      Asking the provider's own number, instead of inferring from block shape, is
      what makes this robust to a transcript layout nobody has seen yet.

    This is a **module-level named predicate rather than a comprehension inside
    :func:`main`** because it is the load-bearing correctness rule of the whole
    measurement, and it has to be assertable on its own. Review 1 of TOKWEIR-9
    demonstrated the cost of the alternative: with the rule inlined, deleting either
    of the last two conditions left all 54 tests passing, because the only test that
    claimed to cover it re-implemented it inline and asserted on its own copy.
    """
    return (
        bool(turn.output_text.strip())
        and not turn.has_unaccounted_blocks
        and not turn.thinking_text
        and turn.thinking_tokens == 0
    )


def _render_checks(results: Sequence[CheckResult]) -> list[str]:
    lines = ["Probe C — internal consistency of the Max transcript (offline)"]
    for result in results:
        lines.append(f"  {result.name}: {result.status.upper()} — {result.detail}")
        for failure in result.failures[:5]:
            lines.append(f"      {failure}")
        if len(result.failures) > 5:
            lines.append(f"      … and {len(result.failures) - 5} more")
    return lines


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    opener: Optional[Callable[[urllib.request.Request, float], Any]] = None,
) -> int:
    """Run the probes that can be run and report which were not (FR-040, FR-041).

    A partial run must never read as a complete one, so a skipped probe is stated
    as skipped, with the reason, rather than simply being absent from the output.

    Exit status is part of that honesty, not decoration:

    ==== ====================================================================
    0    every probe that was asked for ran and produced a measurement
    1    the transcript yielded nothing to measure
    2    partial — no credential, so the probes needing the tokenizer skipped
    3    a probe was attempted and did not produce a measurement
    ==== ====================================================================

    ``3`` exists because this command is meant to be re-run after a Claude Code or
    model change, quite possibly from a script. Returning 0 for a run that measured
    nothing would report success for the exact outcome someone re-running it needs
    to hear about.

    ``--control`` gates Probe A′, the API-side control, **off by default**. It is the
    harness's only *expensive* probe: four generations of up to 300 ``max_tokens``. A
    credentialed run without it is not generation-free — Probe B spends one 16-token
    generation on its field inventory, unconditionally — but everything else is
    ``count_tokens``. The routine reason to re-run this is to check nothing has
    drifted, which Probe A and Probe C answer on their own, so the expensive probe is
    the one you ask for rather than the one you pay for by default. Without it the
    report says what was established on the transcript side and declines to claim the
    parity verdict.

    ``opener`` is injected by the tests so the rendering path — the headline filter,
    the report text, and the guarantee that the credential never reaches it — can be
    exercised without a network call or a metered request.
    """
    parser = argparse.ArgumentParser(
        prog="python -m tokenweir.parity",
        description=(
            "Measure whether a Claude Max transcript's token counts are on the "
            "same scale the Anthropic API reports (ADR-0001 item 6)."
        ),
    )
    parser.add_argument(
        "--transcript", required=True, help="path to a Claude Code .jsonl transcript"
    )
    parser.add_argument(
        "--model",
        required=True,
        help="model for the API probes; never defaulted, it is half the comparison condition",
    )
    parser.add_argument(
        "--credential-file",
        default=None,
        help="file holding the API key (never pass the key itself as an argument)",
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help=(
            "also run Probe A' — the same arithmetic against the API's own generations. "
            "Off by default: it is the harness's only *expensive* probe (4 generations "
            "of up to 300 max_tokens). A credentialed run without it still generates "
            "once, for Probe B's 16-token field inventory; everything else is "
            "count_tokens. Turn it on when re-establishing the parity verdict itself."
        ),
    )
    parser.add_argument(
        "--sample",
        type=int,
        default=20,
        help="how many text-only turns to send to the tokenizer for Probe A",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    turns = list(iter_turns(args.transcript))
    out: list[str] = [
        f"transcript: {args.transcript}",
        f"turns with usage: {len(turns)}",
        f"model (API probes): {args.model}",
        "",
    ]

    if not turns:
        out.append("No turns with usage were read — nothing to measure.")
        print("\n".join(out))
        return 1

    out.extend(_render_checks(consistency_checks(turns)))
    out.append("")

    key = credential(path=args.credential_file)
    if key is None:
        out.extend(
            [
                "Probe A — output-side parity: SKIPPED (no credential configured)",
                "Probe B — API usage field shape: SKIPPED (no credential configured)",
                "",
                "This is a PARTIAL run. Set ANTHROPIC_API_KEY or pass --credential-file",
                "to run the probes that need the provider's tokenizer.",
            ]
        )
        print("\n".join(out))
        return 2

    probe = ApiProbe(key, opener=opener)
    failures = 0

    text_only = [t for t in turns if is_headline_eligible(t)]
    # The **complement** of the predicate, not a restatement of parts of it. Review 2
    # of TOKWEIR-9 caught the earlier version enumerating only two of the four
    # disqualifying conditions, so a turn with no text at all — or with thinking text
    # but a zero thinking count — fell into neither bucket and vanished from a report
    # whose own comment below promises the arithmetic closes. Derived this way,
    # `len(text_only) + len(excluded) == len(turns)` holds by construction.
    excluded = [t for t in turns if not is_headline_eligible(t)]

    # The envelope first: without it every row below reads as a discrepancy of
    # exactly -E, which is what review 1 of TOKWEIR-9 found this command doing —
    # printing "delta -4" twelve times and leaving the operator no way to tell
    # parity from drift.
    envelope: Optional[int] = None
    if text_only:
        longest = max(text_only, key=lambda t: len(t.output_text))
        try:
            envelope = probe.envelope(args.model, longest.output_text)
        except ProbeError as exc:
            out.append(f"count_tokens envelope: FAILED — {exc}")
            failures += 1
    if envelope is not None:
        out.append(
            f"count_tokens envelope E = {envelope} tokens "
            f"(derived as 2*c1-c2 by doubling a {len(longest.output_text)}-char turn; "
            f"bare text count = count_tokens - E)"
        )
    out.append("")

    out.append("Probe A — output-side parity (transcript vs the tokenizer, same text)")
    if not text_only:
        out.append("  no eligible turn was available; headline NOT MEASURED")
        failures += 1
    measured = 0
    offsets: list[int] = []
    ratios: list[float] = []
    for turn in text_only[: args.sample]:
        reported = _numeric(turn.usage.get("output_tokens"))
        try:
            counted = probe.count_tokens(args.model, turn.output_text)
        except ProbeError as exc:
            out.append(f"  {turn.message_id}: FAILED — {exc}")
            failures += 1
            break
        tokenizer = _numeric(counted.usage.get("input_tokens"))
        if reported is None or tokenizer is None:
            out.append(f"  {turn.message_id}: incomparable (a count was absent)")
            continue
        measured += 1
        if envelope is None:
            out.append(
                f"  {turn.message_id}: transcript {reported}, count_tokens {tokenizer} "
                f"(no envelope: bare count unavailable)"
            )
            continue
        bare = tokenizer - envelope
        offset = reported - bare
        offsets.append(offset)
        if bare:
            ratios.append(reported / bare)
        out.append(
            f"  {turn.message_id}: transcript {reported}, count_tokens {tokenizer}, "
            f"bare text {bare}, transcript-bare {offset:+d}"
        )

    # The verdict, computed rather than left to the reader. A *constant* difference
    # with slope 1 is framing; a constant *ratio* with a growing difference is a
    # scale factor. Saying which was observed is the whole point of re-running this.
    if offsets:
        distinct = sorted(set(offsets))
        out.append("")
        if len(distinct) == 1:
            out.append(
                f"  Probe A result: transcript = bare_text {distinct[0]:+d}, "
                f"CONSTANT across {len(offsets)} turn(s) — no scale factor."
            )
        else:
            out.append(
                f"  Probe A result: difference VARIES across {len(offsets)} turn(s): "
                f"{distinct} — investigate before trusting subscription numbers."
            )
        if len(ratios) > 1:
            out.append(
                f"  (ratio transcript/bare spans {min(ratios):.4f}-{max(ratios):.4f}; a "
                f"scale factor would hold this constant and grow the difference instead)"
            )
    # The arithmetic has to close, or a capped run reads as a complete one: with
    # `--sample 3` on a 144-turn transcript, "3 measured" and "137 excluded" leave
    # four eligible turns unaccounted for and invisible (FR-041).
    unrecoverable = sum(1 for t in excluded if t.has_unaccounted_blocks or t.thinking_tokens)
    separately_counted = sum(
        1
        for t in excluded
        if not (t.has_unaccounted_blocks or t.thinking_tokens) and t.thinking_text
    )
    no_text = len(excluded) - unrecoverable - separately_counted
    out.append(
        f"  {measured} of {len(text_only)} eligible turn(s) measured; "
        f"{len(excluded)} excluded ({unrecoverable} unmeasurable — tool_use or "
        f"unrecoverable thinking; {separately_counted} with thinking counted "
        f"separately; {no_text} with no text at all); "
        f"{len(text_only) + len(excluded)} accounted for of {len(turns)} total"
    )
    if text_only and measured < len(text_only) and not failures:
        # Guarded on `failures`: the Probe A loop breaks out on a ProbeError, and
        # without this the shortfall it leaves would be blamed on the sample cap.
        out.append(
            f"  NOTE: {len(text_only) - measured} eligible turn(s) were not measured "
            f"(--sample {args.sample})"
        )
    out.append("")

    # Probe A' — the control that turns "the transcript reports real tokens" into
    # "the transcript reports the same number the API would". Probe A alone cannot
    # do that: it shows the transcript is on the tokenizer's scale, not that the API
    # agrees on the constant. So the identical arithmetic is run against responses
    # the API generates itself, and the two constants are compared.
    #
    # **Opt-in, and off by default.** It is this harness's only *expensive* probe —
    # four generations of up to 300 max_tokens, against count_tokens everywhere else.
    # (Probe B still generates once, 16 tokens, on any credentialed run; review 3 of
    # TOKWEIR-9 caught this comment claiming otherwise directly above the code that
    # does it.) The routine reason to re-run the command is to check that nothing has
    # drifted, which Probe A and Probe C answer on their own. Review 2 made the case:
    # every re-run paying for four generations to re-derive a constant that is already
    # recorded is a standing cost for an occasional need. The verdict below degrades
    # honestly rather than silently when it is off.
    out.append("Probe A' — the same arithmetic on the API's own generations")
    api_offsets: list[int] = []
    if not args.control:
        out.append(
            "  SKIPPED (pass --control to run it; it is the only expensive probe — "
            "4 generations. Probe B below still generates once, 16 tokens.)"
        )
    elif envelope is None:
        out.append("  SKIPPED (no envelope, so a bare count cannot be recovered)")
    else:
        for prompt, budget in _CONTROL_PROMPTS:
            try:
                control = probe.messages(args.model, prompt, max_tokens=budget)
            except ProbeError as exc:
                out.append(f"  FAILED — {exc}")
                failures += 1
                break
            reported = _numeric(control.usage.get("output_tokens"))
            if reported is None or not control.text.strip():
                out.append("  incomparable (no output_tokens, or no text produced)")
                continue
            try:
                counted = probe.count_tokens(args.model, control.text)
            except ProbeError as exc:
                out.append(f"  FAILED — {exc}")
                failures += 1
                break
            tokenizer = _numeric(counted.usage.get("input_tokens"))
            if tokenizer is None:
                out.append("  incomparable (no count returned)")
                continue
            thinking = 0
            details = control.usage.get("output_tokens_details")
            if isinstance(details, Mapping):
                thinking = _numeric(details.get("thinking_tokens")) or 0
            bare = tokenizer - envelope
            offset = reported - bare - thinking
            api_offsets.append(offset)
            out.append(
                f"  api {reported}, bare text {bare}, thinking {thinking}, "
                f"api-(text+thinking) {offset:+d}"
            )

    if api_offsets:
        api_distinct = sorted(set(api_offsets))
        out.append("")
        if len(api_distinct) == 1:
            out.append(
                f"  Probe A' result: api = bare_text + thinking {api_distinct[0]:+d}, "
                f"CONSTANT across {len(api_offsets)} call(s)."
            )
        else:
            out.append(f"  Probe A' result: VARIES: {api_distinct}")

        # The verdict the whole command exists to produce.
        transcript_distinct = sorted(set(offsets))
        if len(api_distinct) == 1 and len(transcript_distinct) == 1:
            if api_distinct == transcript_distinct:
                out.append(
                    f"  ==> PARITY. One rule, one constant ({api_distinct[0]:+d}), both "
                    f"auth modes. No correction factor."
                )
            else:
                out.append(
                    f"  ==> DIFFERENCE. Transcript constant {transcript_distinct[0]:+d} vs "
                    f"api constant {api_distinct[0]:+d}: the gap is the correction to "
                    f"carry with the emitter."
                )
        else:
            out.append(
                "  ==> NO VERDICT: a constant was not established on both sides."
            )
    elif offsets:
        # A' did not run. Say what *was* established and what was not, rather than
        # letting a constant transcript offset read as the full parity verdict.
        transcript_distinct = sorted(set(offsets))
        if len(transcript_distinct) == 1:
            out.append(
                f"  ==> Transcript side only: a constant offset of "
                f"{transcript_distinct[0]:+d} against the tokenizer, so the counts are "
                f"real tokens and unscaled. Whether the API reports the *same* constant "
                f"is the API-side control question — re-run with --control to confirm it, or see "
                f"docs/parity-subscription-vs-api.md for the recorded result."
            )
        else:
            out.append(
                f"  ==> DRIFT on the transcript side: the offset varies "
                f"({transcript_distinct}). Re-run with --control before trusting "
                f"subscription numbers."
            )
    out.append("")

    out.append("Probe B — the API's own usage field shape")
    try:
        control = probe.messages(args.model, "Reply with the single word: ok")
    except ProbeError as exc:
        out.append(f"  FAILED — {exc}")
        failures += 1
    else:
        # The last counted response, named in the output rather than left implicit.
        # Any turn would do — this probe compares the *set of field names*, which is
        # a property of the transcript format and not of one response — but a report
        # that does not say which row it read cannot be checked by its reader.
        reference_turn = turns[-1]
        transcript_usage = reference_turn.usage
        out.append(f"  transcript row compared: {reference_turn.message_id}")
        out.append(f"  API usage keys:        {sorted(control.usage)}")
        out.append(f"  transcript usage keys: {sorted(transcript_usage)}")
        for comparison in compare_usage(control.usage, transcript_usage):
            if comparison.name in BILLING_FIELDS:
                out.append(
                    f"    {comparison.name}: {comparison.status} "
                    f"(api={comparison.reference}, transcript={comparison.observed})"
                )

    print("\n".join(out))
    return 3 if failures else 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
