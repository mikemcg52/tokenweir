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
whose output includes ``thinking`` or ``tool_use`` blocks has ``output_tokens``
covering more than the text we can reconstruct, so those turns are reported
separately from the text-only headline rather than averaged in. That is why the
probe runs over a *population* of turns and reports a distribution.

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

# Intra-package reuse, deliberately. FR-002 requires this module not re-derive the
# transcript semantics that already exist: `_text` for field hygiene and
# `TokenTotals.from_mapping` for count coercion. The one that would be a real bug
# to re-invent is de-duplication on `message.id` — a transcript repeats one API
# response's usage across several lines — so that rule is restated by reference
# below rather than guessed at a second time.
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

    ``has_non_text_blocks`` is what keeps the headline honest. ``output_tokens``
    covers every block the model emitted — ``thinking`` and ``tool_use`` included —
    while ``output_text`` can only carry the text ones. A turn with either is
    therefore expected to under-count on the tokenizer side for a reason that has
    nothing to do with parity, so it is flagged here and reported separately
    rather than averaged into the result.
    """

    message_id: Optional[str]
    model: Optional[str]
    timestamp: Optional[str]
    usage: Mapping[str, Any]
    output_text: str
    has_non_text_blocks: bool = False


def _blocks(message: Mapping[str, Any]) -> Tuple[str, bool]:
    """Assemble a message's text and say whether anything else was there.

    A ``content`` that is a plain string is Claude Code's older shape and is taken
    at face value. A list is the current one: ``text`` blocks contribute, and any
    other block type — ``thinking``, ``tool_use``, ``redacted_thinking`` — sets the
    flag without contributing text it cannot supply.
    """
    content = message.get("content")
    if isinstance(content, str):
        return content, False
    if not isinstance(content, Sequence):
        return "", False

    parts: list[str] = []
    other = False
    for block in content:
        if not isinstance(block, Mapping):
            other = True
            continue
        if block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
            else:
                other = True
        else:
            other = True
    return "".join(parts), other


def iter_turns(path: Any) -> Iterator[Turn]:
    """Yield each counted assistant turn of a Claude Code transcript.

    De-duplicates on ``message.id`` for the reason
    :mod:`tokenweir.claude_code` documents at length: one API response can appear
    as several transcript lines, each repeating the same ``message.usage``, and
    every one of those lines is individually well-formed. Counting a turn twice
    would corrupt Probe A's per-turn comparison exactly as it corrupts a session
    total.

    Entries that are not objects, carry no message, carry no usage, or whose usage
    is not a mapping are skipped — the same tolerance the hook's scan applies,
    because a transcript is a log and a log has noise in it.

    An unreadable file yields nothing rather than raising. The harness reports
    "no turns" and the operator can see that for themselves; a traceback out of an
    iterator would be a worse way to say the same thing.
    """
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        return

    seen: set[str] = set()
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
                if identity is not None:
                    if identity in seen:
                        continue
                    seen.add(identity)

                text, other = _blocks(message)
                yield Turn(
                    message_id=identity,
                    model=_text(message.get("model")),
                    timestamp=_text(entry.get("timestamp")),
                    usage=usage,
                    output_text=text,
                    has_non_text_blocks=other,
                )
    except Exception:
        # A read that ended early. What was already yielded is still true; there
        # is simply no more of it. Same judgement as the hook's scan.
        return


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

    @property
    def present_both(self) -> bool:
        return self.status in (PARITY, OFFSET)


_MISSING = object()


def _numeric(value: Any) -> Optional[int]:
    """The value as a whole non-negative count, or ``None`` if it is not one.

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
        """Probe B: one small controlled call, for the API's own ``usage`` shape.

        ``max_tokens`` is deliberately tiny. This call exists to inventory field
        names, not to generate anything, and it spends the only output tokens the
        whole harness spends.
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
        return ProbeResult(kind="messages", model=model, usage=usage)


# --- the reproducible command ----------------------------------------------


def _render_checks(results: Sequence[CheckResult]) -> list[str]:
    lines = ["Probe C — internal consistency of the Max transcript (offline)"]
    for result in results:
        lines.append(f"  {result.name}: {result.status.upper()} — {result.detail}")
        for failure in result.failures[:5]:
            lines.append(f"      {failure}")
        if len(result.failures) > 5:
            lines.append(f"      … and {len(result.failures) - 5} more")
    return lines


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the probes that can be run and report which were not (FR-040, FR-041).

    A partial run must never read as a complete one, so a skipped probe is stated
    as skipped, with the reason, rather than simply being absent from the output.
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

    probe = ApiProbe(key)

    text_only = [t for t in turns if t.output_text.strip() and not t.has_non_text_blocks]
    mixed = [t for t in turns if t.output_text.strip() and t.has_non_text_blocks]

    out.append("Probe A — output-side parity (text-only turns are the headline)")
    if not text_only:
        out.append("  no text-only turn was available; headline NOT MEASURED")
    for turn in text_only[: args.sample]:
        reported = _numeric(turn.usage.get("output_tokens"))
        try:
            counted = probe.count_tokens(args.model, turn.output_text)
        except ProbeError as exc:
            out.append(f"  {turn.message_id}: FAILED — {exc}")
            break
        tokenizer = _numeric(counted.usage.get("input_tokens"))
        if reported is None or tokenizer is None:
            out.append(f"  {turn.message_id}: incomparable (a count was absent)")
            continue
        delta = reported - tokenizer
        out.append(
            f"  {turn.message_id}: transcript {reported}, tokenizer {tokenizer}, "
            f"delta {delta:+d}"
        )
    out.append(
        f"  ({len(mixed)} turn(s) carrying thinking/tool_use blocks "
        "excluded from the headline)"
    )
    out.append("")

    out.append("Probe B — the API's own usage field shape")
    try:
        control = probe.messages(args.model, "Reply with the single word: ok")
    except ProbeError as exc:
        out.append(f"  FAILED — {exc}")
    else:
        transcript_usage = turns[-1].usage
        out.append(f"  API usage keys:        {sorted(control.usage)}")
        out.append(f"  transcript usage keys: {sorted(transcript_usage)}")
        for comparison in compare_usage(control.usage, transcript_usage):
            if comparison.name in BILLING_FIELDS:
                out.append(
                    f"    {comparison.name}: {comparison.status} "
                    f"(api={comparison.reference}, transcript={comparison.observed})"
                )

    print("\n".join(out))
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
