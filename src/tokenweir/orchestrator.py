"""The orchestrator's side of attribution — the phase taxonomy and the env block (TOKWEIR-8).

ADR-0001 Pillar 4 settles *where* attribution comes from:

    Phase/issue context: hooks inherit Claude Code's process environment, so MADO's
    orchestrator injects ``MADO_ISSUE_KEY``, ``MADO_PHASE``, ``MADO_STREAM_ID``,
    ``MADO_PRICING_MODE`` per iteration; the hook reads them onto the record.
    Attribution comes from the orchestrator, not the model.

TOKWEIR-7 built the reading half: :func:`tokenweir.claude_code.attribution_from_env`
takes those four variables out of the environment and puts them on the record. This
module is the other half — what the orchestrator is supposed to *put there*, and what
the labels are allowed to say.

Why a phase needs a taxonomy at all
-----------------------------------

``MADO_PHASE`` lands in the record's ``queue`` field, which is free text. Without a
defined vocabulary ``review``, ``Review``, ``1st review``, ``review 1`` and ``review-1``
are five different strings for one phase of one run, and a report that groups by phase
shows five lanes where the work had one. The story's acceptance is stated in exactly
those terms — *"phase labels match the orchestrator lifecycle"* — and matching needs
something to match against.

Where the vocabulary comes from
-------------------------------

The lifecycle is MADO's, not this library's, so the kinds are taken from MADO's own
record of it rather than invented here. ``mado-phase``, the stamper the
``/mado-implement`` loop calls at every phase boundary, documents its permitted values:

    ``--phase PHASE   one of: implementation, review, fix``

Those are the kinds. The loop (implement → review → fix → review → …) is what makes a
kind recur, and the story's own examples — *"1st review, 1st bug fix"* — are a kind plus
which occurrence of it. Kind and occurrence are therefore the whole taxonomy:

    ``implementation``, ``review-1``, ``fix-1``, ``review-2``, …

**Closed at the kinds, open at the occurrence.** Three kinds because three are what the
orchestrator names; any positive occurrence because the loop's fix-round cap is
configurable, and a taxonomy that stopped at "3rd review" would be wrong the first time
somebody passed ``--fix-rounds 8``.

Producers raise, consumers tolerate
-----------------------------------

The asymmetry between :func:`attribution_env` (raises on a value it cannot use) and
:func:`normalize_phase` (never raises, never discards) is deliberate. A bad value in a
producer is a bug that should surface in the orchestrator's own tests, where it can be
fixed. A bad value arriving at the hook is a fact about the world that already happened,
and the hook's design contract is that it cannot fail a session or lose a turn's
metering — so an unrecognized phase is carried through as written rather than dropped.

**Nothing here imports the hook**, and nothing here imports outside the standard library
except :mod:`tokenweir.contract`, for the one enum both ends must agree on. The
orchestrator runs in another codebase and another cluster namespace; it should be able to
depend on this contract without inheriting a capture path or a transport driver. The
dependency runs one way — :mod:`tokenweir.claude_code` imports this module, never the
reverse.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Mapping, Optional, Union

from tokenweir.contract import PricingMode

__all__ = [
    "ATTRIBUTION_ENV",
    "PhaseKind",
    "attribution_env",
    "is_canonical_phase",
    "normalize_phase",
    "phase_label",
]


class PhaseKind(str, Enum):
    """One stage of the orchestrator's loop (FR-001).

    A ``str`` enum, like :class:`~tokenweir.contract.PricingMode`, so a member *is*
    its wire spelling: a label composes by concatenation and a member compares equal
    to the string a record carries.

    - :attr:`IMPLEMENTATION` — writing the change. Happens once per run.
    - :attr:`REVIEW` — an independent review pass. Recurs; there is always exactly
      one more review than there are fix rounds.
    - :attr:`FIX` — a fix round answering the review before it. Recurs.
    """

    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    FIX = "fix"

    @classmethod
    def coerce(cls, value: Union["PhaseKind", str, None]) -> Optional["PhaseKind"]:
        """Normalize a member or its wire string to a member; ``None`` passes through.

        Aliases are honoured here rather than only in :func:`normalize_phase`, so a
        producer calling :func:`phase_label` with ``"bug fix"`` gets the same answer
        the hook would derive from the same string.

        Raises:
            ValueError: if the value is neither ``None`` nor a recognizable kind.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            resolved = _KIND_BY_WORD.get(_collapse(value).lower())
            if resolved is not None:
                return resolved
        permitted = ", ".join(repr(member.value) for member in cls)
        raise ValueError(f"phase kind must be one of {permitted}, or None; got {value!r}")


#: Every accepted spelling of a kind, mapped to the kind it names.
#:
#: The aliases are not decoration. ``bug fix`` is the story's own wording ("1st bug
#: fix"), and ``implement`` is what a hand-written stamp tends to say. Recognizing them
#: is what keeps a human-written phase out of the unrecognized path, where it would
#: survive as a second spelling of a lane that already has one.
_KIND_BY_WORD: dict[str, PhaseKind] = {
    "implementation": PhaseKind.IMPLEMENTATION,
    "implement": PhaseKind.IMPLEMENTATION,
    "review": PhaseKind.REVIEW,
    "fix": PhaseKind.FIX,
    "bug fix": PhaseKind.FIX,
    "bugfix": PhaseKind.FIX,
}

#: The variable names, spelled once (FR-008).
#:
#: Keyed by the argument name :func:`attribution_env` takes, so a caller reading the
#: mapping sees which of its own values ends up where. Both ends of the contract resolve
#: a name from here — the producer builds the block from it, and the hook reads through
#: it — which is what stops a typo at one end from producing records that are silently
#: unattributed rather than an error at either.
ATTRIBUTION_ENV: Mapping[str, str] = {
    "issue_key": "MADO_ISSUE_KEY",
    "phase": "MADO_PHASE",
    "stream_id": "MADO_STREAM_ID",
    "pricing_mode": "MADO_PRICING_MODE",
}

#: A canonical label: a kind, optionally followed by ``-`` and a positive occurrence.
#: Anchored, and ``[1-9]\d*`` rather than ``\d+`` so ``review-0`` and ``review-01`` are
#: not canonical — one is not an occurrence and the other is a second spelling of one.
_CANONICAL_RE = re.compile(
    r"^(?:%s)(?:-[1-9]\d*)?$" % "|".join(member.value for member in PhaseKind)
)

#: A written phase: a kind-ish word, with an occurrence before or after it.
#: ``1st review`` and ``review 2`` are both common ways to say the same thing, so both
#: are read; anything else falls through to the unrecognized path.
_LEADING_ORDINAL_RE = re.compile(r"^(\d+)(st|nd|rd|th)\s+(.*)$", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"^(.*?)[\s-]+(\d+)$")


def _collapse(value: str) -> str:
    """Fold the separators a phase gets written with, without changing its words.

    ``_`` and ``#`` become spaces and runs of whitespace become one, so
    ``review_1``, ``Review #1`` and ``review  1`` are the same input by the time
    anything looks at them. A hyphen is deliberately *not* folded here: it is the
    canonical separator, and turning ``review-1`` into ``review 1`` at this stage
    would mean the canonical form had to be re-derived rather than recognized.
    """
    return re.sub(r"\s+", " ", value.replace("_", " ").replace("#", " ")).strip()


def _english_ordinal(digits: str, suffix: str) -> Optional[int]:
    """The number an English ordinal names, or ``None`` if it is not one.

    The suffix is *validated*, not stripped. ``11th`` is 11 and ``11st`` is not an
    ordinal at all — a stripper would read both as 11 and quietly accept a typo as a
    phase occurrence, which is exactly the kind of near-miss this taxonomy exists to
    keep out of a report.
    """
    try:
        number = int(digits)
    except ValueError:  # pragma: no cover - the regex only matches digits
        return None
    if number <= 0:
        return None
    if 11 <= number % 100 <= 13:
        expected = "th"
    else:
        expected = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return number if suffix.lower() == expected else None


def phase_label(
    kind: Union[PhaseKind, str], occurrence: Optional[int] = None
) -> str:
    """The canonical label for a phase (FR-002).

    ``phase_label("review", 2)`` is ``'review-2'``; ``phase_label("implementation")``
    is ``'implementation'``.

    The occurrence is optional because *absent* and *first* are different claims. An
    orchestrator that does not count its review passes should say ``review``; writing
    ``review-1`` on its behalf would make an unknown look like a fact, and a report
    reading it back could not tell which it was.

    Args:
        kind: a :class:`PhaseKind`, or any spelling :meth:`PhaseKind.coerce` accepts.
        occurrence: which pass through a recurring kind this is, counting from 1.

    Raises:
        ValueError: if the kind is not recognized, or the occurrence is present and is
            not a positive whole number (FR-007).
    """
    resolved = PhaseKind.coerce(kind)
    if resolved is None:
        raise ValueError("phase kind is required; got None")
    if occurrence is None:
        return resolved.value
    # bool is an int subclass, and `phase_label("review", True)` meaning 'review-1' is
    # a coincidence of Python's type lattice rather than something a caller meant.
    if isinstance(occurrence, bool) or not isinstance(occurrence, int):
        raise ValueError(
            f"phase occurrence must be a positive integer or None; "
            f"got {type(occurrence).__name__} {occurrence!r}"
        )
    if occurrence < 1:
        raise ValueError(f"phase occurrence must be 1 or greater; got {occurrence!r}")
    return f"{resolved.value}-{occurrence}"


def normalize_phase(value: Optional[str]) -> Optional[str]:
    """Map a written phase onto its canonical label, losing nothing (FR-003 – FR-006).

    Never raises and never discards. Three outcomes, and the caller can tell them apart
    with :func:`is_canonical_phase`:

    - ``None``, empty or whitespace-only → ``None``. Unset and blank mean "no phase",
      which is the reading TOKWEIR-7 already gives them (FR-022 there); a blank must
      not become a label.
    - A phase this taxonomy recognizes → its canonical label. ``1st review``,
      ``review 1``, ``Review #1``, ``REVIEW-1`` and ``review_1`` all become
      ``'review-1'``.
    - Anything else → the value with its separators folded, unchanged otherwise.

    That last case is a deliberate refusal to be clever. The ACP lifecycle may grow a
    phase before this library hears about it, and a record carrying ``deploy`` is worth
    more than a record carrying nothing — being wrong about the vocabulary should cost
    a non-canonical row in a report, not a lost attribution.
    """
    if value is None:
        return None
    collapsed = _collapse(value)
    if not collapsed:
        return None

    lowered = collapsed.lower()

    direct = _KIND_BY_WORD.get(lowered)
    if direct is not None:
        return direct.value

    leading = _LEADING_ORDINAL_RE.match(lowered)
    if leading is not None:
        number = _english_ordinal(leading.group(1), leading.group(2))
        if number is not None:
            kind = _KIND_BY_WORD.get(leading.group(3).strip())
            if kind is not None:
                return f"{kind.value}-{number}"

    trailing = _TRAILING_NUMBER_RE.match(lowered)
    if trailing is not None:
        kind = _KIND_BY_WORD.get(trailing.group(1).strip())
        # `int()` rather than the raw digits: `review-01` is the same occurrence as
        # `review-1` and must not survive as a second spelling of it.
        if kind is not None and (number := int(trailing.group(2))) >= 1:
            return f"{kind.value}-{number}"

    return collapsed


def is_canonical_phase(value: Optional[str]) -> bool:
    """Whether a value is already a label this taxonomy defines (FR-005).

    This is how a caller distinguishes :func:`normalize_phase`'s two non-``None``
    outcomes — a recognized phase from a preserved unknown — without the function
    having to return a pair for the sake of the rarer case. The hook asks it to decide
    whether to log a diagnostic; a report could ask it to flag a row.

    ``None`` is not canonical, but it is not wrong either: it is the absence of a
    phase, which is a legitimate state and one nobody needs warning about.
    """
    return isinstance(value, str) and bool(_CANONICAL_RE.match(value))


def _required_text(name: str, value: Optional[str]) -> str:
    """A present-but-unusable string is a producer bug; an absent one is not (FR-012, FR-013).

    ``None`` exports ``''``, which the hook already reads back as "unknown" — a caller
    that genuinely has no stream id says so. A blank string is different: it is a value
    that was computed and came out empty, and exporting it would attribute a record to
    whitespace while looking, at every later stage, exactly like a record that was
    attributed properly.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError(
            f"{name} must be a string or None; got {type(value).__name__} {value!r}"
        )
    if not value.strip():
        raise ValueError(f"{name} was given as blank; pass None to leave it unknown")
    return value.strip()


def attribution_env(
    *,
    issue_key: Optional[str],
    phase: Union[PhaseKind, str, None] = None,
    stream_id: Optional[str] = None,
    pricing_mode: Union[PricingMode, str, None] = PricingMode.SUBSCRIPTION,
) -> dict[str, str]:
    """The environment block the orchestrator exports for one iteration (FR-009 – FR-013).

    Hand it what the iteration knows and export what comes back into the environment
    Claude Code inherits, before the turn::

        env.update(attribution_env(issue_key="TOKWEIR-8", phase=phase_label("fix", 2),
                                   stream_id=stream_id))

    **All four variables, every call.** A block that omitted the values it had nothing
    to say about would leave the *previous* iteration's phase standing during this one,
    and the resulting record would be well-formed, plausible, and wrong — which is the
    failure this story exists to prevent. Exporting ``''`` is how the block says
    "unknown", and it is the spelling TOKWEIR-7's reader already treats as unset.

    The phase is canonicalized on the way out (FR-011), so an orchestrator cannot
    export a non-canonical label by accident; a phase this taxonomy does not recognize
    is passed through, on the same reasoning as :func:`normalize_phase`.

    Args:
        issue_key: the issue this iteration is working. Keyword-required rather than
            defaulted: an iteration that cannot say what it is working on has nothing
            to attribute, and a caller that truly has no key should have to write
            ``None`` and mean it.
        phase: a :class:`PhaseKind`, a canonical label, or any spelling
            :func:`normalize_phase` reads.
        stream_id: the stream, so an iteration's turns roll up to it.
        pricing_mode: defaults to :attr:`PricingMode.SUBSCRIPTION`, the mode this
            capture path exists for.

    Raises:
        ValueError: on a value that is present but unusable — a blank issue key or
            stream id, a blank phase, or a pricing mode that is not a mode. The hook
            tolerates such values because it may not fail a session; a producer is a
            program with a bug, and telling it so is the whole point.
    """
    mode = PricingMode.coerce(pricing_mode)

    if isinstance(phase, str) and not phase.strip():
        raise ValueError("phase was given as blank; pass None to leave it unknown")
    label = phase.value if isinstance(phase, PhaseKind) else normalize_phase(phase)

    return {
        ATTRIBUTION_ENV["issue_key"]: _required_text("issue_key", issue_key),
        ATTRIBUTION_ENV["phase"]: label or "",
        ATTRIBUTION_ENV["stream_id"]: _required_text("stream_id", stream_id),
        ATTRIBUTION_ENV["pricing_mode"]: mode.value if mode is not None else "",
    }
