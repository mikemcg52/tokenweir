"""The subscription capture adapter — a Claude Code ``Stop`` hook (TOKWEIR-7).

ADR-0001 Pillar 4 splits capture by how Claude Code is authenticated. An API key
is metered by interception at the Anthropic-compatible ``/v1/messages`` edge. A
**Claude Max subscription cannot be**: the auth is OAuth and there is no
base-URL-swappable API path, so no proxy ever sees the traffic. The ADR's answer
is to capture from Claude Code's *own* record instead::

    Trigger: the `Stop` hook — fires after Claude finishes a response turn, the
    natural per-iteration boundary. Configure `exit 0` / non-blocking (never
    `exit 2`) and a short `timeout` (e.g. 30s) […] so a network failure never
    blocks the session — this *is* the off-critical-path guarantee.

This module is that hook. It reads the hook payload on stdin, works out how many
tokens the turn that just ended actually consumed, and hands one
:class:`~tokenweir.contract.UsageRecord` to the ordinary emitter with
``pricing_mode=subscription``.

**Deterministic, not a skill.** The ADR is explicit that this must not be an LLM
skill or agent — *"that would burn tokens into the very window being measured and
can't reliably read its own counts"*. This is a plain Python process with no model
call in it and no way to make one. For the same reason attribution comes from the
**environment** the orchestrator injected, never from the model or from anything
the model wrote into the transcript.

**No third-party import, at all.** Everything here is stdlib plus ``tokenweir``
itself, so the module lives in the core package rather than behind an extra and a
bare ``pip install tokenweir`` can run the hook. A transport is imported only if
the environment selects one, inside the branch that selects it — the same rule
:mod:`tokenweir.amqp` and :mod:`tokenweir.migrations` already follow.

Why the delta needs remembered state
------------------------------------

A transcript is append-only and holds the **whole session**, not one turn. Summing
it gives a session total. The turn's number is always a difference against
something remembered from the previous invocation — and *what* is remembered is
the decision that matters.

What is remembered here is the **cumulative total already emitted**, not a file
offset and not a set of message ids. A cumulative baseline is self-correcting: if
one invocation never ran, or ran and could not emit, the tokens it would have
reported are not lost — they turn up in the next turn's delta, which is a *late*
record rather than a missing one. A file cursor is not self-correcting; it
advances whether or not the emit that went with it succeeded, which silently
deletes a turn's tokens forever. The cost, stated rather than hidden: a turn whose
emit is refused is merged into the next turn's record. Late and coarse beats gone.

The baseline is therefore advanced **after** the record reaches the emitter, never
before (FR-012).

Double counting is the real hazard
----------------------------------

One assistant API response can appear as **several** lines in the transcript, each
repeating the same ``message.usage`` object. Summing lines over-counts a turn, and
the over-count is invisible because every line is individually well-formed. So the
scan de-duplicates on ``message.id``: usage belongs to one API response, and one
response has one id however many lines mention it.

That set is per-scan and never persisted — the cumulative baseline is what crosses
invocations, so the state file stays four integers rather than growing with the
session.

Two guarantees that the session is never disturbed, and one that is not ours
----------------------------------------------------------------------------

They are separate because they fail separately:

- :func:`main` catches ``Exception`` and returns ``0`` on every path. Covers a bug
  in this module. It deliberately does **not** catch ``BaseException``: a
  ``KeyboardInterrupt`` means the process is being torn down, and swallowing that
  would be worse than the failure.
- A **bounded emitter close**. Covers a sink that accepted the record and cannot
  deliver it. :meth:`~tokenweir.emitter.BufferedEmitter.close` already implements
  the bound; this module only has to choose a small number.

The third case — nothing raises and nothing returns, a connect into a black hole —
is bounded by **Claude Code's own hook ``timeout``**, which the documented
``settings.json`` fragment sets to 30 seconds. That is deliberately not
re-implemented here. ADR-0001 and the story both name the host's timeout as the
mechanism, and a second timer inside the hook would be a competing bound with its
own failure modes: an alarm is one-shot, every guard in this module catches
``Exception``, and the obvious implementation is therefore absorbed by its own
error handling and silently spent.

There is a real argument on the other side — a synchronous ``Stop`` hook that
wedges delays the next turn by the whole of the host's timeout, and giving up at
ten seconds would cost the developer twenty fewer — but it is a separate question
from capturing usage, and it belongs to whoever takes it up deliberately rather
than to this story.

**Nothing is ever written to stdout.** Claude Code parses a hook's stdout, so a
stray ``print`` is a way for a metering adapter to change the session's behaviour.
Diagnostics go to this module's logger and nowhere else.

Be clear about what that costs, because the package fits the ``tokenweir`` logger
with a ``NullHandler``: under a Claude Code session that has configured no logging
for this library, the hook's diagnostics reach **nobody**. That is the package's
standing decision — where output goes belongs to whoever embeds the library — and
this module keeps to it rather than carving out an exception with a private
stderr knob. The consequence is that a *persistently* misconfigured hook is quiet,
and the operator-facing answer to that is the store: a stream that reports no
usage is the signal, not a log line nobody collected.

Are the numbers it reads real? Yes — measured, not assumed
---------------------------------------------------------

This module only ever claimed to report a transcript *faithfully*. Whether a Max
transcript's counts are denominated in the same token units the API reports and
bills on is a **separate** claim, and ADR-0001 kept it open as an ``Unverified``
item for exactly that reason.

TOKWEIR-9 measured it. **Parity holds and no correction factor is needed**, so
nothing here adjusts, scales or annotates the counts it reads — which is why this
note is documentation rather than code. Against the provider's own tokenizer, a
transcript's ``output_tokens`` satisfies ``tokens(text) + 2`` exactly, on 12
responses spanning 181–1206 tokens with zero variance; the API's own reporting
satisfies the same rule with its thinking term added. A *constant* difference with
slope 1 is request framing, not a scale factor — a scaled count would hold the
ratio constant and grow the difference instead.

Three things about that result bear on this module directly:

- **The four fields this module reads** (:data:`_COUNT_FIELDS`) exist on both sides
  under identical names. Nothing it depends on is renamed or absent under
  subscription auth.
- **Nothing in ``usage`` marks subscription traffic.** ``service_tier`` reads
  ``standard`` on a Max subscription, the same as on an API key. So
  ``pricing_mode`` must come from the environment the orchestrator injected — as it
  does — and could never have been inferred from the transcript.
- **The result is point-in-time.** ADR-0001 notes the Max landscape is volatile; the
  measurement holds for ``claude-opus-5`` on Claude Code 2.1.263 as of 2026-09-10.
  It is not a permanent property of the format, and re-running
  ``python -m tokenweir.parity`` after a Claude Code, model or tier change is the
  intended way to renew it. :mod:`tokenweir.parity`'s offline checks need no
  credential and are the cheap early warning.

Method, numbers and the four things the measurement deliberately does **not**
establish — chiefly that it says nothing about what a subscription is *billed*, and
that the input side rests on corroboration rather than direct measurement — are in
``docs/parity-subscription-vs-api.md``.

Installing it
-------------

``pip install tokenweir`` ships the console script ``tokenweir-claude-code-hook``;
``python -m tokenweir.claude_code`` is the same entry point. See the README for
the ``settings.json`` fragment and the full environment-variable table.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Callable, Mapping, Optional, Tuple

from tokenweir.contract import PricingMode, UsageRecord
from tokenweir.emitter import BufferedEmitter
from tokenweir.orchestrator import ATTRIBUTION_ENV, is_canonical_phase, normalize_phase
from tokenweir.sink import NullSink, Sink, emit_usage

#: What this module offers a caller. The baseline machinery — ``read_baseline``,
#: ``write_baseline``, ``state_path_for``, ``turn_delta`` — is deliberately absent:
#: the spec calls that state an implementation detail of this producer, and a
#: published name is a promise to keep it. It stays importable for the tests, which
#: are inside the project and may know more than a consumer does.
__all__ = [
    "DEFAULT_APP_ID",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "HookInput",
    "ScanResult",
    "SinkChoice",
    "TokenTotals",
    "attribution_from_env",
    "build_fields",
    "main",
    "read_hook_input",
    "run",
    "scan_transcript",
    "select_sink",
]

_logger = logging.getLogger(__name__)

#: ``app_id`` when nothing overrides it. A **constant**, not the stream id: a
#: per-app rollup (``gateway_usage_app_ts_idx``) would fragment into one bucket
#: per stream if the stream id went here, which is the opposite of what an
#: ``app_id`` is for. The stream is carried as ``parent_request_id`` instead.
DEFAULT_APP_ID = "claude-code"

#: ``endpoint`` when nothing overrides it. Deliberately *not* ``/v1/messages``: a
#: turn is an aggregate of several API calls, and labelling the aggregate with a
#: single-call endpoint would let a report blend the two under one key.
DEFAULT_ENDPOINT = "claude-code/stop-hook"

#: ``model`` when the transcript names none. The field is required and non-blank
#: by contract, and a record whose model is unknown is worth more than no record.
DEFAULT_MODEL = "unknown"

#: Seconds :meth:`BufferedEmitter.close` may spend flushing. Smaller than the
#: client's own 5s default: this process exists for one record and must not sit
#: on a dead broker.
DEFAULT_CLOSE_TIMEOUT = 3.0

#: The four contract token fields, in the order they appear on the record.
_COUNT_FIELDS: Tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


# --- diagnostics -----------------------------------------------------------


def _note(message: str, *, exc_info: bool = False) -> None:
    """Record a diagnostic. Never raises, never touches stdout.

    The logger is the only channel, and that is a decision rather than an
    omission. The package attaches a ``NullHandler`` to the ``tokenweir`` logger
    so that an application which configured no logging is not written to — where
    output goes is the embedding application's call, not the library's, and
    ``tokenweir/__init__.py`` sets out the reasoning. A hook is embedded in Claude
    Code, so a private stderr knob here would be this module overriding that
    decision on the package's behalf.

    Guarded, because a hostile logging configuration must not become the thing
    that breaks a hook whose entire purpose is to be unable to break anything.
    """
    try:
        _logger.warning(message, exc_info=exc_info)
    except Exception:  # pragma: no cover - a hostile logging configuration
        pass


def _env(name: str) -> Optional[str]:
    """An environment value, with unset and blank treated alike as absent.

    Blank matters as much as unset (FR-022). An orchestrator that exports
    ``MADO_PHASE=""`` for a run with no phase is saying "no phase", and writing
    ``''`` into an attribution column says something different — it says the field
    was populated. The optional fields are nullable precisely so that "unknown"
    has a representation, and it is ``None``.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


# --- the four counts -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """The four contract token counts as one immutable value.

    A value type rather than four loose integers because every operation this
    module performs on them is performed on all four at once — summing a
    transcript, differencing against a baseline, asking whether the totals went
    backwards. Written out four times, those are four chances to transpose a field
    name, and a transposed cache field is invisible in a record that still looks
    well-formed.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    def __add__(self, other: "TokenTotals") -> "TokenTotals":
        return TokenTotals(
            *(getattr(self, f) + getattr(other, f) for f in _COUNT_FIELDS)
        )

    def minus(self, other: "TokenTotals") -> "TokenTotals":
        """Per-field difference. Only meaningful when :meth:`covers` is true."""
        return TokenTotals(
            *(getattr(self, f) - getattr(other, f) for f in _COUNT_FIELDS)
        )

    def covers(self, other: "TokenTotals") -> bool:
        """Whether every field is at least ``other``'s.

        The question "did the transcript regress" (FR-014), asked once. A
        *partial* regression — one field lower, three higher — is still a
        regression: it cannot happen to an append-only file, so it means the file
        at that path is no longer the one the baseline describes.
        """
        return all(getattr(self, f) >= getattr(other, f) for f in _COUNT_FIELDS)

    @property
    def is_zero(self) -> bool:
        """Whether nothing at all was counted."""
        return not any(getattr(self, f) for f in _COUNT_FIELDS)

    def as_dict(self) -> dict[str, int]:
        """The counts keyed by their contract field names."""
        return {f: getattr(self, f) for f in _COUNT_FIELDS}

    @classmethod
    def from_mapping(cls, data: Any) -> "TokenTotals":
        """Read the four counts out of an untrusted mapping.

        Every field is optional and every value is coerced (FR-008): a missing,
        non-integer or negative count becomes zero. The contract would refuse a
        negative anyway, and refusing the whole record over one malformed field
        would lose three good counts to punish one bad one.
        """
        if not isinstance(data, Mapping):
            return cls()
        return cls(*(_coerce_count(data.get(f)) for f in _COUNT_FIELDS))


def _coerce_count(value: Any) -> int:
    """A non-negative integer, or zero. ``bool`` is not an integer here.

    Integral floats are accepted for the reason
    :func:`tokenweir.contract._coerce_wire_integers` accepts them: JSON has one
    number type, so a producer may legitimately write ``100.0`` where it means
    ``100``. ``bool`` is excluded exactly as the contract excludes it — ``True``
    is not one token.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        if value >= 0 and value.is_integer():
            return int(value)
        return 0
    return 0


# --- reading the transcript ------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What one pass over a transcript found.

    ``totals`` is the session's de-duplicated cumulative usage — not a turn's. The
    turn is :func:`turn_delta` of this against the remembered baseline.

    The three ``last_*`` fields describe the most recent counted response, which is
    what the record is stamped from: its model, its id (the record's identity) and
    its timestamp.

    ``readable`` is what tells the caller *why* ``totals`` might be low. It is
    ``False`` when the file could not be opened at all, or when reading it ended
    in an exception before reaching EOF — a transient failure (``EMFILE`` under a
    busy orchestrator, a permission blip, the file mid-rotation) that says nothing
    about the transcript's actual contents. It is ``True`` whenever the read ran to
    completion, even over zero matching lines: an empty or genuinely short
    transcript is a fact about the file, not a failure to read one, and the two
    must not be confused. A transcript that could not be read has ``totals`` of
    zero for a reason that has nothing to do with the session, and treating that
    zero as ground truth is exactly the defect this field exists to prevent.
    """

    totals: TokenTotals = TokenTotals()
    counted: int = 0
    last_model: Optional[str] = None
    last_message_id: Optional[str] = None
    last_timestamp: Optional[str] = None
    readable: bool = True


def _text(value: Any) -> Optional[str]:
    """A non-blank, stripped string, or ``None``. Anything else is noise.

    The stripped value is what is returned, not the original. Testing one and
    returning the other is the kind of asymmetry that reads as an oversight and
    eventually becomes one — a path or a model id with surrounding whitespace
    would propagate into a record, while :func:`_env` strips the equivalent
    value from the environment. One rule for both.
    """
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def scan_transcript(path: Any) -> ScanResult:
    """Sum a Claude Code transcript's usage, counting each API response once.

    The file is read **line by line** (FR-009): a long session's transcript is
    large, and holding it in memory to add up four integers would make the hook's
    footprint a function of how long the developer has been working.

    Every line is untrusted (FR-007). Blank lines, lines that are not JSON, lines
    that are not objects, entries with no usage and entries whose usage is not a
    mapping are all skipped, and the scan **continues** — abandoning the file at
    the first oddity would throw away a whole session over one bad line, and the
    last line of a transcript being read while it is written is routinely a
    half-written fragment.

    De-duplication is on ``message.id`` (FR-006) and on nothing else. An entry with
    no id is counted on its own: it cannot be shown to be a duplicate of anything,
    and under-counting a turn is the worse error of the two available. An earlier
    draft fell back to the entry's own ``uuid``, which is unique per *line* rather
    than per response — so it de-duplicated nothing that ``message.id`` had not
    already caught, while widening what counts as an identity. It is gone.

    Where two entries *do* share an id, the **first** is counted and
    the rest are skipped — the case this exists for is one response repeated
    verbatim, so first and last are the same value; a genuine disagreement between
    two entries claiming one response id would be a transcript-format change, and
    picking a winner is not something to guess at silently.

    Note the asymmetry in what carries forward across entries, which is deliberate.
    ``last_model`` and ``last_timestamp`` keep the last *known* value, because a
    later entry that names no model has not stopped the turn from running on one.
    ``last_message_id`` does **not**: it becomes the record's ``request_id``, which
    must identify *this* turn, and inheriting the previous turn's id would hand two
    distinct turns the same identity — the precise thing FR-019 forbids, and worse
    than the uuid4 fallback because a consumer collapsing duplicates would delete a
    real turn.

    Entries are not filtered by ``type``. The question this asks is "does this
    entry carry usage", which is the property that matters and the one least likely
    to be invalidated by a transcript-format change. An ``isSidechain`` entry
    *would* be counted by this rule if the file handed to this function held one —
    but in current Claude Code versions it never does: a subagent's usage is
    written to sibling ``<session>/subagents/*.jsonl`` files, not inline in the
    transcript at ``transcript_path``, and this function is only ever given the
    latter. **Subagent token usage is therefore not currently captured**, contrary
    to an earlier claim here. Measured against a live session directory: every
    ``isSidechain`` entry in the main transcript was ``false``; every entry with
    ``isSidechain: true`` and a ``usage`` object lived in a ``subagents/`` file this
    function never opens. Reading those files is real, separate work — a Stop hook
    fires once per main-thread turn, and knowing which subagent files (and how much
    of each) belong to *this* turn needs its own tracking, not a side effect of
    scanning one path — and is not done here.

    Returns:
        A :class:`ScanResult`. A missing or unreadable file yields an empty one
        rather than raising — every failure here is "no metering for this turn" —
        with :attr:`ScanResult.readable` set to ``False`` so the caller can tell
        that emptiness apart from a transcript that genuinely has nothing new.
    """
    seen: set[str] = set()
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        _note(f"transcript could not be opened; no metering for this turn: {path!r}")
        return ScanResult(readable=False)

    totals = TokenTotals()
    counted = 0
    last_model = None
    last_message_id = None
    last_timestamp = None
    readable = True
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

                totals = totals + TokenTotals.from_mapping(usage)
                counted += 1
                last_model = _text(message.get("model")) or last_model
                last_message_id = identity
                last_timestamp = _text(entry.get("timestamp")) or last_timestamp
    except Exception:
        # A read error part-way through. What was counted before it is still
        # true, and returning it is better than discarding a whole session
        # because the tail was unreadable — but the read did not reach EOF, so
        # `totals` is not the transcript's actual cumulative total and must not
        # be trusted as one by the caller.
        _note("transcript read ended early; counting what was read", exc_info=True)
        readable = False

    return ScanResult(
        totals=totals,
        counted=counted,
        last_model=last_model,
        last_message_id=last_message_id,
        last_timestamp=last_timestamp,
        readable=readable,
    )


# --- the remembered baseline -----------------------------------------------


def _state_dir() -> Path:
    """Where baselines live. Overridable, and otherwise a conventional cache path."""
    override = _env("TOKENWEIR_HOOK_STATE_DIR")
    if override:
        return Path(override)
    xdg = _env("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "tokenweir" / "claude-code-hook"


def state_path_for(transcript_path: Any) -> Path:
    """The baseline file for one transcript.

    Keyed by a hash of the **resolved** transcript path (FR-011), so two concurrent
    sessions never consume each other's deltas, and so the key does not depend on
    whether the hook was handed a relative or absolute path. Hashed rather than
    escaped because a transcript path is arbitrary and a filename is not.
    """
    raw = os.fspath(transcript_path)
    try:
        resolved = os.fspath(Path(raw).resolve())
    except Exception:  # pragma: no cover - resolve is total on a plain string
        resolved = raw
    digest = hashlib.sha256(resolved.encode("utf-8", "surrogatepass")).hexdigest()[:32]
    return _state_dir() / f"{digest}.json"


def read_baseline(path: Any) -> TokenTotals:
    """The cumulative totals already emitted for a transcript.

    Absent, unreadable, not JSON, not an object, or holding nonsense: all are
    treated as an empty baseline (FR-015). That is the safe direction — an empty
    baseline over-counts one turn (the session's tokens land in one record) where
    a *guessed* baseline would silently under-count every turn after it, and
    under-counting is the failure nobody notices.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return TokenTotals()
    except Exception:
        _note(f"baseline unreadable; treating as empty: {path!r}")
        return TokenTotals()
    if not isinstance(data, Mapping):
        _note(f"baseline is not an object; treating as empty: {path!r}")
        return TokenTotals()
    return TokenTotals.from_mapping(data.get("cumulative"))


def write_baseline(path: Any, totals: TokenTotals, *, transcript: Any = None) -> bool:
    """Persist the baseline atomically. Returns whether it was written.

    Atomic via a temporary file in the **same directory** and :func:`os.replace`
    (FR-016). Written in place, an interrupted write — the process being killed by
    the host's hook timeout, a full disk — leaves a truncated file that every later
    invocation
    reads as corrupt, so one interruption would cost every subsequent turn its
    baseline. The temp file is in the same directory because ``os.replace`` is only
    atomic within a filesystem.

    Returns ``False`` rather than raising when the directory cannot be created or
    written (FR-017): losing the baseline costs accuracy, and must not cost this
    turn its record.

    Be precise about that cost, because the reassuring version of the sentence is
    wrong. A baseline lost **once** over-counts exactly one turn — the session's
    tokens land in one record — and everything after it is correct again. A state
    directory that is **persistently** unwritable never recovers: every turn
    re-reports the whole session from zero, so the over-count compounds for as
    long as the condition lasts.

    And the compounding case is **not** self-describing, which is worth stating
    plainly because the reassuring version of *this* sentence is wrong too. Only a
    transcript that is not growing produces identical re-reports a consumer could
    collapse: with a live session the identity is the last response's id, which
    changes every turn, so the re-reports arrive as distinct records with
    monotonically inflating counts and nothing marks them as re-reports at all.

    So a persistently unwritable state directory is real, silent inflation. Each
    failure is noted on the logger, and that is all this function can do about it —
    the alternative is losing the turn, which FR-017 rules out. It is a broken
    deployment, and the honest statement is that it must be fixed rather than
    tolerated.
    """
    payload = {
        "schema": 1,
        "transcript_path": os.fspath(transcript) if transcript is not None else None,
        "cumulative": totals.as_dict(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target = Path(path)
    tmp_name = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            prefix=target.name + ".",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_name = tmp.name
            json.dump(payload, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, target)
        return True
    except Exception:
        _note(
            "baseline not written; this turn will be re-reported until it can be "
            f"stored: {target!r}"
        )
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except Exception:
                pass
        return False


def turn_delta(current: TokenTotals, baseline: TokenTotals) -> Optional[TokenTotals]:
    """This turn's counts, or ``None`` if the transcript regressed.

    ``None`` means the file at that path is not the one the baseline describes — it
    was replaced, truncated, or a session was resumed into a fresh file at the same
    name. There is no honest number for that transition: the difference would be
    negative (which the contract refuses, correctly) and the raw total would
    re-count a session already metered. The caller resets the baseline and emits
    nothing, absorbing one transition.
    """
    if not current.covers(baseline):
        return None
    return current.minus(baseline)


# --- the hook's input ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HookInput:
    """The parts of Claude Code's stdin payload this hook uses."""

    transcript_path: Optional[str] = None
    session_id: Optional[str] = None


def read_hook_input(stream: IO[str]) -> HookInput:
    """Parse the hook payload from a stream. Never raises (FR-001, FR-002).

    Empty input, input that is not JSON, JSON that is not an object, and an object
    with no usable ``transcript_path`` all produce an empty :class:`HookInput`,
    which the caller turns into "no record, exit 0". A hook that cannot tell which
    transcript it was called about has nothing to meter, and that is a quiet
    no-op rather than an error.
    """
    try:
        raw = stream.read()
    except Exception:
        _note("hook input could not be read")
        return HookInput()
    if not raw or not raw.strip():
        return HookInput()
    try:
        payload = json.loads(raw)
    except Exception:
        _note("hook input is not JSON; no metering for this turn")
        return HookInput()
    if not isinstance(payload, Mapping):
        _note("hook input is not a JSON object; no metering for this turn")
        return HookInput()
    return HookInput(
        transcript_path=_text(payload.get("transcript_path")),
        session_id=_text(payload.get("session_id")),
    )


# --- the record ------------------------------------------------------------


def attribution_from_env() -> dict[str, Any]:
    """Read the orchestrator's attribution out of the environment (FR-021).

    ADR-0001 Pillar 4: *"hooks inherit Claude Code's process environment, so MADO's
    orchestrator injects ``MADO_ISSUE_KEY``, ``MADO_PHASE``, ``MADO_STREAM_ID``,
    ``MADO_PRICING_MODE`` per iteration; the hook reads them onto the record.
    Attribution comes from the orchestrator, not the model."* This function is the
    only place attribution comes from, which is what makes that sentence true here.

    The mapping onto contract fields, with the one compromise named:

    - ``MADO_ISSUE_KEY`` → ``workload``. The unit of work.
    - ``MADO_STREAM_ID`` → ``parent_request_id``. The column's documented purpose
      is to *"roll them back up to the request the user actually made"*, and a
      stream is exactly that relation to its turns.
    - ``MADO_PHASE`` → ``queue``. **This one is a compromise, not a fit.** The v1
      contract has no phase field, and adding one is a ``SCHEMA_VERSION`` bump plus
      a migration plus every consumer — Pillar 5 work this story does not own.
      ``queue`` is nullable, unconstrained, and the nearest available sense of
      "which lane did this go through". It is written down here, in the spec's
      Assumptions and in the README so that a future v2 can move it deliberately
      rather than discover it.

    The phase is read through :func:`tokenweir.orchestrator.normalize_phase`
    (TOKWEIR-8 FR-015), so ``1st review`` and ``review_1`` reach the record as the
    one label ``review-1`` whether the orchestrator was written before this
    taxonomy existed or a phase was stamped by hand. That module defines the
    vocabulary; this one does not restate it. A phase outside the taxonomy is
    **kept** and noted (FR-016) — the hook may not lose a turn's attribution over a
    label it does not know, any more than it may over a pricing mode it does not
    know.

    ``pricing_mode`` defaults to ``subscription`` — that is the whole point of this
    capture path — and an unrecognized ``MADO_PRICING_MODE`` falls **back** to it
    rather than dropping the record (FR-018). A misconfigured environment variable
    losing a turn's metering would be the tail wagging the dog.
    """
    mode: PricingMode = PricingMode.SUBSCRIPTION
    declared = _env(ATTRIBUTION_ENV["pricing_mode"])
    if declared is not None:
        try:
            coerced = PricingMode.coerce(declared)
        except ValueError:
            _note(
                f"{ATTRIBUTION_ENV['pricing_mode']}={declared!r} is not a recognized "
                "mode; recording as subscription"
            )
        else:
            if coerced is not None:
                mode = coerced

    written = _env(ATTRIBUTION_ENV["phase"])
    phase = normalize_phase(written)
    if written is not None and phase is None:
        # The value had content and folded to nothing — it was made only of the
        # separators the taxonomy folds (`_`, `#`). That is "no phase" by the same rule
        # the producer refuses to export it under, so the field is left unset; but it
        # is *said*, because the likeliest way to get here is a template that expanded
        # to nothing (`${KIND}_${N}` with neither set), and an orchestrator quietly
        # recording no phase for every turn is exactly the failure nobody notices
        # (review 5, Med-2). `_env` has already ruled out unset and whitespace-only,
        # so this cannot fire on an ordinary unattributed run.
        _note(
            f"{ATTRIBUTION_ENV['phase']}={written!r} is made only of separators and "
            "folds to nothing; recording no phase"
        )
    elif phase is not None and not is_canonical_phase(phase):
        # The value as read, not as normalized: an operator debugging this goes
        # looking for the string in their orchestrator's configuration, and
        # `'deploy step'` appears nowhere in a config that says `deploy_step`
        # (review 1, Low-3). "As read" rather than "as exported" because `_env` has
        # already trimmed the ends — a phase exported with surrounding spaces is
        # reported without them (review 4, Low-4). The middle, which is the part that
        # differs after folding, is untouched.
        _note(
            f"{ATTRIBUTION_ENV['phase']}={written!r} is not a phase in the "
            "orchestrator taxonomy; recording it as given"
        )
    return {
        "workload": _env(ATTRIBUTION_ENV["issue_key"]),
        "queue": phase,
        "parent_request_id": _env(ATTRIBUTION_ENV["stream_id"]),
        "pricing_mode": mode,
    }


def _normalize_ts(value: Optional[str]) -> str:
    """A UTC ISO-8601 timestamp, preferring the turn's own (FR-023).

    The transcript's timestamp is when the work actually happened; the hook's
    clock is when the hook got round to looking. They differ by however long the
    turn took, and the record should say the former.

    Anything unparseable falls back to now rather than being passed through: the
    store's column is a ``TIMESTAMPTZ``, and a string that is not a timestamp
    would fail the whole batch it travels in.
    """
    if value:
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).isoformat()
    return datetime.now(timezone.utc).isoformat()


def build_fields(
    delta: TokenTotals,
    scan: ScanResult,
    hook_input: HookInput,
) -> dict[str, Any]:
    """Assemble the record's fields. Pure: reads the environment, touches nothing else.

    Identity (FR-019) is the last counted response's ``message.id`` — unique per
    API response and therefore per turn, and *traceable*: a row in the store can be
    found again in the transcript that produced it.

    It is deliberately **stable, not fresh per emission**. A turn re-reported
    because its record could not be stored keeps its id, so the redelivery arrives
    as the same record rather than as a new turn — which is what lets a report
    collapse it, and why the store deliberately puts no unique constraint on
    ``request_id`` (``001_gateway_usage.sql``): an append-only log expecting
    at-least-once delivery treats an identical row arriving twice as an ordinary
    event, and the id is the key that remedy needs.

    Its reach is narrower than it first appears, and the limit belongs here rather
    than in a footnote. It collapses redeliveries of *one* turn. It does **not**
    rescue the case where the baseline itself cannot be stored on a live session:
    there each re-report covers a longer span and ends on a different response, so
    the ids differ and the records are genuinely different records. That case is
    unrecoverable by any choice of identifier and is a broken deployment; see
    :func:`write_baseline`.

    Distinct turns never collide, which is the property FR-019 actually protects:
    a turn's id is its last response's id, and no two responses share one.

    The fallbacks exist so that a transcript with no usable ids still produces a
    record rather than none: the contract requires ``request_id`` to be non-blank,
    and a uuid4 is a worse identifier than a message id but an infinitely better
    one than a dropped turn. Uniqueness is preserved in that case too — a fresh
    uuid4 cannot collide — at the cost of the stability above, which is
    unavailable when the transcript names nothing to be stable about.

    ``model`` is stored verbatim (ADR-0001 Pillar 3 — nothing is inferred from it)
    and falls back to :data:`DEFAULT_MODEL`, because the alternative to an unknown
    model is a record the contract refuses.

    ``latency_ms`` is deliberately absent. The transcript's timestamps describe
    gaps between writes, which is not the latency of a model call, and a plausible
    invented number is worse than an omitted optional field.
    """
    if scan.last_message_id is not None:
        request_id = scan.last_message_id
    elif hook_input.session_id is not None:
        request_id = f"{hook_input.session_id}:{uuid.uuid4().hex}"
    else:
        request_id = f"claude-code:{uuid.uuid4().hex}"

    fields: dict[str, Any] = {
        "request_id": request_id,
        "app_id": _env("TOKENWEIR_APP_ID") or DEFAULT_APP_ID,
        "endpoint": _env("TOKENWEIR_ENDPOINT") or DEFAULT_ENDPOINT,
        "model": scan.last_model or DEFAULT_MODEL,
        "status": "ok",
        "ts": _normalize_ts(scan.last_timestamp),
    }
    fields.update(delta.as_dict())
    fields.update(attribution_from_env())
    return fields


# --- transport selection ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class SinkChoice:
    """A sink, and whether it is actually metering anything.

    The second field is the point. A *configured* transport that could not be
    constructed — a dead broker, a missing driver — degrades to
    :class:`~tokenweir.sink.NullSink` so the hook cannot fail, and a ``NullSink``
    accepts every record and stores none. Without this flag that is
    indistinguishable from the deliberately unconfigured case, and the difference
    decides whether the baseline may advance: an unconfigured hook is discarding
    records **by choice** and should not hold a baseline for ever, while a broken
    transport is losing records the deployment expected to keep.

    Attributes:
        sink: where records go, possibly a ``NullSink`` standing in for a
            transport that could not be built.
        degraded: ``True`` when a transport *was* configured and could not be
            constructed. The turn is then never treated as kept.
        drop_counter: the name of an attribute on ``sink`` that counts records the
            sink is **certain** it lost, or ``None`` for a sink that offers no such
            promise.

            Named here, by whoever built the sink, rather than sniffed off the
            object at use time. That was the first design and it was wrong: the
            published :class:`~tokenweir.sink.Sink` protocol declares only ``emit``
            and ``close``, so an attribute called ``dropped`` on a stranger's sink
            is not a contract — it might count drops of other records on a sink
            shared with something else, or lifetime drops across reconnects, and
            reading it as "this record was lost" would pin the baseline and
            re-report the session for ever. The party that constructed the sink is
            the only one that knows what its counters mean, so the name travels
            with the choice.
    """

    sink: Sink
    degraded: bool = False
    drop_counter: Optional[str] = None


def _dropped_count(choice: SinkChoice) -> Optional[int]:
    """How many records this sink says it has **lost**, if it promised to say.

    A negative signal, and the direction is the whole design. The obvious version
    of this function asks a success counter — ``DirectSink.written``,
    ``AMQPSink.published`` — and it is wrong in both directions:

    - It over-claims. ``AMQPSink.published`` counts frames handed to the socket,
      not records a broker stored. This adapter does not enable publisher confirms,
      and ``amqp.py`` says so in its own words: *"a publish to a nonexistent
      exchange returns normally, and the broker's ``channel.close`` arrives a round
      trip later"*. A live connection to a missing exchange increments
      ``published`` and stores nothing.
    - It under-claims, which is worse. Any sink exposing an integer named
      ``written`` that it does not increment per record — a perfectly conforming
      third-party sink — would look permanently undelivered, so the baseline would
      never advance and every turn would re-report the whole session.

    Asking what was **dropped** has neither failure. A counted drop is a fact the
    adapter is certain of: :class:`~tokenweir.sink.DirectSink` counts one when the
    store raised, :class:`~tokenweir.amqp.AMQPSink` when the publish failed or the
    channel was not live. It is never a guess. And a sink that offers no drop
    counter simply is not consulted, rather than being assumed to have failed.

    What this deliberately does **not** promise: that a record not counted as
    dropped reached a store. Nothing inside this process can know that over a
    transport without confirms. The claim is narrower and true — *the transport did
    not tell us it lost this record* — and the residual is written down in the
    README rather than dressed up.

    Only a counter the :class:`SinkChoice` **named** is read. Nothing is inferred
    from the shape of the object: see :attr:`SinkChoice.drop_counter` for why an
    attribute named ``dropped`` on an arbitrary sink is not a promise about this
    record.

    Returns:
        The counter, or ``None`` when none was named or it is not an integer.
    """
    if choice.drop_counter is None:
        return None
    value = getattr(choice.sink, choice.drop_counter, None)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def select_sink() -> SinkChoice:
    """Choose a sink from the environment (FR-030, FR-031).

    ``TOKENWEIR_AMQP_URL`` selects the homelab's broker path; ``TOKENWEIR_DSN``
    selects the broker-less direct path; neither selects :class:`NullSink`. With
    **both** set the broker wins, deliberately and not by accident of ordering: the
    broker is the durable path, so a deployment that has configured one has said
    where records should survive an outage, and quietly writing past it to the
    store would discard that.

    Both transport imports are **inside** the branch that selects them, so
    importing this module — or running an unconfigured hook — never touches
    ``pika`` or ``psycopg``. That is the same rule :mod:`tokenweir.amqp` follows
    for its own driver, and it is what lets this module live in a core package
    that is dependency-free by contract.

    A sink whose *construction* fails — a malformed URL, a missing driver, a
    broker that is not there — degrades to :class:`NullSink` and is returned
    **marked degraded**. Construction is normally the one place in the emit path
    where raising is correct, because it is wiring time and somebody is watching.
    Here nobody is: the caller is a hook whose contract is that it cannot disturb
    the session. But the turn must not then be treated as metered — a ``NullSink``
    accepts everything and stores nothing, so without the mark the hook would
    advance its baseline over a record that went nowhere, which is the loss
    :func:`run` is arranged to prevent.

    :class:`NullSink` as the *unconfigured* default is deliberate and is a
    different case. An unconfigured hook is a no-op, not an error; a developer who
    installs the hook before standing up a broker gets silence rather than a stream
    of failures, and its baseline advances because it is discarding records by
    choice rather than by failure.
    """
    url = _env("TOKENWEIR_AMQP_URL")
    if url is not None:
        try:
            from tokenweir.amqp import AMQPSink

            return SinkChoice(AMQPSink.from_url(url), drop_counter="dropped")
        except Exception:
            _note(
                "AMQP sink could not be constructed; this turn is not metered and "
                "its tokens carry into the next"
            )
            return SinkChoice(NullSink(), degraded=True)

    dsn = _env("TOKENWEIR_DSN")
    if dsn is not None:
        try:
            from tokenweir.postgres import PostgresSource
            from tokenweir.sink import DirectSink

            return SinkChoice(
                DirectSink(PostgresSource.from_dsn(dsn), owns_source=True),
                drop_counter="dropped",
            )
        except Exception:
            _note(
                "direct sink could not be constructed; this turn is not metered and "
                "its tokens carry into the next"
            )
            return SinkChoice(NullSink(), degraded=True)

    return SinkChoice(NullSink())


# --- the pipeline ----------------------------------------------------------


def run(
    stream: IO[str],
    sink_factory: Optional[Callable[[], SinkChoice]] = None,
    *,
    close_timeout: float = DEFAULT_CLOSE_TIMEOUT,
) -> Optional[UsageRecord]:
    """Read, count, emit, and only then advance the baseline.

    The order is the requirement (FR-012), not an implementation detail: the
    baseline is written **after** :func:`~tokenweir.sink.emit_usage` has taken the
    record, so a turn the emitter refuses is carried into the next turn's delta
    rather than deleted. A baseline advanced first would make every failed emit a
    permanent, silent loss.

    "Taken the record" is deliberately the *strong* reading: the baseline advances
    only once the record was actually **stored**, not merely accepted. This process
    is short-lived and emits exactly one record, so it can afford what a metered
    request path cannot — it closes the emitter, which flushes within the bounded
    timeout, and then asks what happened.

    **What it asks, and what that can honestly mean.**
    :attr:`~tokenweir.emitter.EmitterStats.delivered` alone is not enough, and
    trusting it was a real defect: it counts records the sink took without raising,
    and a conforming ``Sink`` may not raise — so
    :class:`~tokenweir.sink.DirectSink` over a dead store and
    :class:`~tokenweir.amqp.AMQPSink` against a dead broker both catch their own
    failure, count a drop, and return normally. Both looked delivered. The tokens
    would have been dropped by the sink and dropped again by the advancing
    baseline: a silent, permanent loss with both shipped transports.

    So the emitter's acceptance is combined with the transport's own **drop**
    counter (:func:`_dropped_count`) — a negative signal, deliberately. A counted
    drop is a fact the adapter is certain of; a success counter is not, because
    ``AMQPSink.published`` counts frames handed to a socket over a transport with
    no publisher confirms. Asking "did you lose it?" is answerable. Asking "did you
    store it?" is not, from inside this process.

    The claim is therefore narrower than "stored", and stating it exactly is the
    point: **the record was accepted and the transport did not report losing it.**
    A broker that is down, or a store that raised, carries the turn's tokens
    forward until it recovers. A broker that is *up* but misconfigured — a missing
    exchange — accepts the frame, reports no drop, and the tokens are lost; nothing
    in this process can see that without publisher confirms, and the README says so
    rather than implying otherwise.

    Only a counter the :class:`SinkChoice` named is consulted — the adapters this
    library builds declare theirs in :func:`select_sink`, and a caller supplying
    its own factory says so or does not. A sink that named none is simply believed,
    which is what stops a conforming third-party sink from being assumed to have
    failed for ever. A *configured* transport that could not be built never counts
    (see :class:`SinkChoice`); an unconfigured hook does, because it is discarding
    by choice rather than by failure.

    That flush is bounded, not unbounded, so this stays inside the hook's own
    budget (FR-027, FR-028) — the wait is the same one ``close`` already promises.

    The bound has a cost worth naming rather than discovering. A sink still
    publishing when ``close_timeout`` expires, which then *succeeds*, has the
    record while the baseline stays put — so the next turn re-reports it. The
    window is ``close_timeout`` (3 seconds by default) and it is narrow, but it is
    not zero.

    And the re-report is **not** a collapsible duplicate, for the same reason the
    unwritable-baseline case is not: the next turn covers a longer span and ends on
    a different response, so it carries a different ``request_id`` and a larger
    count. The store ends up holding both, and their sum overstates the session.

    The run still errs this way deliberately, because the alternative is worse:
    assuming success on a timeout turns the same window into a silent **loss**, and
    an overstatement is at least visible against the transcript it came from, while
    a turn that vanished leaves nothing behind to check against.

    Emission goes through the buffered client and the guarded seam rather than a
    bare ``sink.emit`` (FR-029). Both guarantees already exist in the library; the
    fourth reimplementation of a guarantee is where it stops being one.

    **The sink is built late and closed here.** ``sink_factory`` is not called at
    all unless there is something to emit, so a turn that added no tokens — a
    common outcome for a ``Stop`` hook — opens no broker connection and can wedge
    on none. Whatever it returns is then closed, as a consequence of closing the
    emitter wrapped around it (:meth:`BufferedEmitter.close` closes its sink, from
    the worker thread, which is what keeps sink access single-threaded). That is
    the right lifecycle for a hook — one record, then exit — but it is a side
    effect on the factory's object, so it is stated rather than discovered.

    Args:
        stream: the hook payload, normally ``sys.stdin``.
        sink_factory: builds the sink, and is called **only if there is something
            to emit**. Defaults to :func:`select_sink`, resolved at call time.
        close_timeout: how long the emitter may spend flushing.

    Returns:
        The emitted record, or ``None`` when there was nothing to emit (no
        transcript, no new tokens, a replaced transcript) or the record was not
        stored.
    """
    hook_input = read_hook_input(stream)
    if hook_input.transcript_path is None:
        return None

    scan = scan_transcript(hook_input.transcript_path)
    state_path = state_path_for(hook_input.transcript_path)
    baseline = read_baseline(state_path)

    delta = turn_delta(scan.totals, baseline)
    if delta is None:
        if not scan.readable:
            # The file could not be opened, or reading it ended in an
            # exception — a transient failure that says nothing about the
            # transcript's actual contents. `scan.totals` is not a trustworthy
            # cumulative total here, so it must not overwrite a baseline that
            # is: re-anchoring on it would be the same silent reset FR-014
            # exists to prevent, one step removed. Leave the baseline exactly
            # where it is and try again next turn.
            _note("transcript unreadable this turn; leaving baseline untouched, no record")
            return None
        # The transcript at this path is not the one the baseline describes.
        # Re-anchor and emit nothing (FR-014).
        _note("transcript regressed below its baseline; re-anchoring, no record")
        write_baseline(state_path, scan.totals, transcript=hook_input.transcript_path)
        return None

    if delta.is_zero:
        # Nothing new. A zero-token record would inflate the request count while
        # adding no tokens, which is worse than no row at all (FR-013).
        return None

    fields = build_fields(delta, scan, hook_input)

    # Resolved here rather than as a default argument: a default is bound when
    # this function is *defined*, so `sink_factory=select_sink` would capture the
    # original and quietly ignore anyone who replaced the module attribute
    # afterwards — including this project's own tests, which is how the trap was
    # found rather than shipped.
    choice = (sink_factory or select_sink)()
    sink = choice.sink
    dropped_before = _dropped_count(choice)

    emitter = BufferedEmitter(sink, close_timeout=close_timeout)
    try:
        record = emit_usage(emitter, fields)
    finally:
        # Closing flushes within `close_timeout` and is what makes the counters
        # below meaningful. In a `finally` so that a guarded seam that somehow
        # raised still leaves no worker thread behind.
        emitter.close()

    # Accepted at the seam, and the transport did not report losing it.
    kept = record is not None and emitter.stats().delivered >= 1

    dropped_after = _dropped_count(choice)
    if dropped_before is not None and dropped_after is not None:
        if dropped_after > dropped_before:
            # The adapter is certain it lost this record: the store raised, or the
            # publish failed. This is the case `EmitterStats.delivered` cannot see,
            # because a conforming sink may not raise and so returns normally after
            # counting its own drop.
            kept = False

    if choice.degraded:
        # A transport was configured and could not be built. The record went into a
        # stand-in that keeps nothing and counts nothing, so no counter would say
        # so; the flag is the only thing that knows.
        kept = False

    if not kept:
        # Refused at the seam (a malformed record, a sink that raised), reported
        # lost by the transport, or handed to a stand-in for a transport that could
        # not be constructed. The baseline stays where it is, so these tokens are
        # reported next turn instead of vanishing.
        _note("record was not kept; its tokens carry into the next turn")
        return None

    write_baseline(state_path, scan.totals, transcript=hook_input.transcript_path)
    return record


# --- the entry point -------------------------------------------------------


def main(argv: Optional[list[str]] = None) -> int:
    """The hook. Always returns ``0``; never writes to stdout.

    ``0`` on every path is not defensiveness, it is the contract (FR-025).
    ADR-0001 Pillar 4 requires ``exit 0`` / non-blocking and explicitly forbids
    ``exit 2``, which is Claude Code's *blocking* status — a metering adapter that
    returned it could stop a session. There is no failure of this hook important
    enough to be worth that.

    ``Exception`` is caught; ``BaseException`` is not. ``KeyboardInterrupt`` and
    ``SystemExit`` mean the process is being torn down, and a metering guard that
    swallowed them would be a worse bug than the one it fixes — the same line
    :mod:`tokenweir.sink` and :mod:`tokenweir.emitter` draw.

    **What bounds a wedged hook is Claude Code's own ``timeout``**, which the
    documented ``settings.json`` fragment sets, and not anything here. That is the
    story's own answer — *"exit 0, short timeout (~30s), optionally `async: true`"*
    — and it is the right one: a hook that re-implemented the timeout would be a
    second mechanism for a bound the host already applies, with its own failure
    modes to get wrong.
    """
    del argv  # the hook takes no arguments; the payload arrives on stdin
    try:
        run(sys.stdin)
    except Exception:
        _note("hook failed; exiting 0 so the session is untouched", exc_info=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
