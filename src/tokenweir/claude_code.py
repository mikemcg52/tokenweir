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

Three separate guarantees that the session is never disturbed
-------------------------------------------------------------

They are separate because they fail separately:

- :func:`main` catches ``Exception`` and returns ``0`` on every path. Covers a bug
  in this module. It deliberately does **not** catch ``BaseException``: a
  ``KeyboardInterrupt`` means the process is being torn down, and swallowing that
  would be worse than the failure.
- A **self-imposed time budget** (``SIGALRM``). Covers the case where nothing
  raises and nothing returns — a DNS lookup into a black hole, a connect to a dead
  broker. Claude Code's own ``timeout`` would eventually kill the process, but
  being killed is the worse outcome: it is louder in the session and it skips the
  state write.
- A **bounded emitter close**. Covers a sink that accepted the record and cannot
  deliver it. :meth:`~tokenweir.emitter.BufferedEmitter.close` already implements
  the bound; this module only has to choose a small number.

**Nothing is ever written to stdout.** Claude Code parses a hook's stdout, so a
stray ``print`` is a way for a metering adapter to change the session's behaviour.
Diagnostics go to this module's logger — which the package fits with a
``NullHandler`` — and, when ``TOKENWEIR_HOOK_DEBUG`` is set, to stderr.

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
import signal
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Mapping, Optional, Tuple

from tokenweir.contract import PricingMode, UsageRecord
from tokenweir.emitter import BufferedEmitter
from tokenweir.sink import NullSink, Sink, emit_usage

__all__ = [
    "DEFAULT_APP_ID",
    "DEFAULT_ENDPOINT",
    "DEFAULT_MODEL",
    "ENVIRONMENT_VARIABLES",
    "HookInput",
    "ScanResult",
    "TokenTotals",
    "attribution_from_env",
    "build_fields",
    "main",
    "read_baseline",
    "read_hook_input",
    "run",
    "scan_transcript",
    "select_sink",
    "state_path_for",
    "turn_delta",
    "write_baseline",
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

#: Seconds the whole hook is allowed to take before it gives up and exits 0.
#: Well inside the ~30s hook timeout ADR-0001 recommends, because being killed by
#: Claude Code is a worse outcome than returning early.
DEFAULT_HOOK_TIMEOUT = 10.0

#: Seconds :meth:`BufferedEmitter.close` may spend flushing. Smaller than the
#: client's own 5s default: this process exists for one record and must not sit
#: on a dead broker.
DEFAULT_CLOSE_TIMEOUT = 3.0

#: Every environment variable this module reads, with a one-line meaning. Public
#: because the README documents these and a test checks the two agree — a knob
#: added here without a line in the README fails the suite rather than quietly
#: becoming folklore.
ENVIRONMENT_VARIABLES: dict[str, str] = {
    "TOKENWEIR_AMQP_URL": "AMQP URL; selects the broker path.",
    "TOKENWEIR_DSN": "Postgres DSN; selects the broker-less direct path.",
    "TOKENWEIR_APP_ID": f"Overrides app_id (default {DEFAULT_APP_ID!r}).",
    "TOKENWEIR_ENDPOINT": f"Overrides endpoint (default {DEFAULT_ENDPOINT!r}).",
    "TOKENWEIR_HOOK_STATE_DIR": "Where the per-transcript baseline is kept.",
    "TOKENWEIR_HOOK_TIMEOUT": f"Total time budget in seconds (default {DEFAULT_HOOK_TIMEOUT}).",
    "TOKENWEIR_HOOK_DEBUG": "When set, also write diagnostics to stderr.",
    "MADO_ISSUE_KEY": "Issue being worked; recorded as workload.",
    "MADO_PHASE": "Phase of the run; recorded as queue.",
    "MADO_STREAM_ID": "Stream; recorded as parent_request_id.",
    "MADO_PRICING_MODE": "Overrides pricing_mode; anything unrecognized falls back.",
}

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

    The logger is the primary channel and the package attaches a ``NullHandler``
    to it, so an application that configured no logging is not written to — the
    same decision ``tokenweir/__init__.py`` documents. ``TOKENWEIR_HOOK_DEBUG``
    adds stderr, which is where a hook's diagnostics are visible in Claude Code's
    debug output.

    Both writes are guarded: a hostile logging configuration, or a closed stderr,
    must not become the thing that breaks a hook whose entire purpose is to be
    unable to break anything.
    """
    try:
        _logger.warning(message, exc_info=exc_info)
    except Exception:  # pragma: no cover - a hostile logging configuration
        pass
    try:
        if os.environ.get("TOKENWEIR_HOOK_DEBUG"):
            print(f"tokenweir-claude-code-hook: {message}", file=sys.stderr)
    except Exception:  # pragma: no cover - a closed stderr
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
    """

    totals: TokenTotals = TokenTotals()
    counted: int = 0
    last_model: Optional[str] = None
    last_message_id: Optional[str] = None
    last_timestamp: Optional[str] = None


def _text(value: Any) -> Optional[str]:
    """A non-blank string, or ``None``. Anything else in the transcript is noise."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return value
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

    De-duplication is on ``message.id`` (FR-006), falling back to the entry's own
    ``uuid``. An entry with neither is counted on its own: it cannot be shown to be
    a duplicate of anything, and under-counting a turn is the worse error of the
    two available.

    Entries are not filtered by ``type``. The question this asks is "does this
    entry carry usage", which is the property that matters and the one least likely
    to be invalidated by a transcript-format change. Sidechain (subagent) entries
    are therefore counted, deliberately: they spend the same subscription budget in
    the same window, and excluding them would under-report precisely the runs this
    exists to measure.

    Returns:
        A :class:`ScanResult`. A missing or unreadable file yields an empty one
        rather than raising — every failure here is "no metering for this turn".
    """
    result = ScanResult()
    seen: set[str] = set()
    try:
        handle = open(path, "r", encoding="utf-8", errors="replace")
    except Exception:
        _note(f"transcript could not be opened; no metering for this turn: {path!r}")
        return result

    totals = TokenTotals()
    counted = 0
    last_model = None
    last_message_id = None
    last_timestamp = None
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

                identity = _text(message.get("id")) or _text(entry.get("uuid"))
                if identity is not None:
                    if identity in seen:
                        continue
                    seen.add(identity)

                totals = totals + TokenTotals.from_mapping(usage)
                counted += 1
                last_model = _text(message.get("model")) or last_model
                last_message_id = identity or last_message_id
                last_timestamp = _text(entry.get("timestamp")) or last_timestamp
    except Exception:
        # A read error part-way through. What was counted before it is still
        # true, and returning it is better than discarding a whole session
        # because the tail was unreadable.
        _note("transcript read ended early; counting what was read", exc_info=True)

    return ScanResult(
        totals=totals,
        counted=counted,
        last_model=last_model,
        last_message_id=last_message_id,
        last_timestamp=last_timestamp,
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
    (FR-016). Written in place, an interrupted write — the time budget firing, the
    process being killed — leaves a truncated file that every later invocation
    reads as corrupt, so one interruption would cost every subsequent turn its
    baseline. The temp file is in the same directory because ``os.replace`` is only
    atomic within a filesystem.

    Returns ``False`` rather than raising when the directory cannot be created or
    written (FR-017): an unwritable cache costs accuracy on the *next* turn, and
    must not cost this turn its record.
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
        _note(f"baseline not written; next turn may over-count: {target!r}")
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

    ``pricing_mode`` defaults to ``subscription`` — that is the whole point of this
    capture path — and an unrecognized ``MADO_PRICING_MODE`` falls **back** to it
    rather than dropping the record (FR-018). A misconfigured environment variable
    losing a turn's metering would be the tail wagging the dog.
    """
    mode: PricingMode = PricingMode.SUBSCRIPTION
    declared = _env("MADO_PRICING_MODE")
    if declared is not None:
        try:
            coerced = PricingMode.coerce(declared)
        except ValueError:
            _note(
                f"MADO_PRICING_MODE={declared!r} is not a recognized mode; "
                "recording as subscription"
            )
        else:
            if coerced is not None:
                mode = coerced
    return {
        "workload": _env("MADO_ISSUE_KEY"),
        "queue": _env("MADO_PHASE"),
        "parent_request_id": _env("MADO_STREAM_ID"),
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

    Identity (FR-019) is the last counted response's ``message.id``, which is
    unique per API response and therefore per turn, and which is *traceable* — a
    row in the store can be found again in the transcript that produced it. The
    fallbacks exist so that a transcript with no usable ids still produces a
    record rather than none: the contract requires ``request_id`` to be non-blank,
    and a uuid4 is a worse identifier than a message id but an infinitely better
    one than a dropped turn.

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


def select_sink() -> Sink:
    """Choose a sink from the environment (FR-030, FR-031).

    ``TOKENWEIR_AMQP_URL`` selects the homelab's broker path; ``TOKENWEIR_DSN``
    selects the broker-less direct path; neither selects :class:`NullSink`.

    Both transport imports are **inside** the branch that selects them, so
    importing this module — or running an unconfigured hook — never touches
    ``pika`` or ``psycopg``. That is the same rule :mod:`tokenweir.amqp` follows
    for its own driver, and it is what lets this module live in a core package
    that is dependency-free by contract.

    A sink whose *construction* fails — a malformed URL, a missing driver, a
    broker that is not there — degrades to :class:`NullSink`. Construction is
    normally the one place in the emit path where raising is correct, because it
    is wiring time and somebody is watching. Here nobody is: the caller is a hook
    whose contract is that it cannot disturb the session, so the failure is noted
    and metering is skipped for this turn.

    :class:`NullSink` as the default is deliberate. An unconfigured hook is a
    no-op, not an error; a developer who installs the hook before standing up a
    broker gets silence rather than a stream of failures.
    """
    url = _env("TOKENWEIR_AMQP_URL")
    if url is not None:
        try:
            from tokenweir.amqp import AMQPSink

            return AMQPSink.from_url(url)
        except Exception:
            _note("AMQP sink could not be constructed; no metering for this turn")
            return NullSink()

    dsn = _env("TOKENWEIR_DSN")
    if dsn is not None:
        try:
            from tokenweir.postgres import PostgresSource
            from tokenweir.sink import DirectSink

            return DirectSink(PostgresSource.from_dsn(dsn), owns_source=True)
        except Exception:
            _note("direct sink could not be constructed; no metering for this turn")
            return NullSink()

    return NullSink()


# --- the pipeline ----------------------------------------------------------


def run(
    stream: IO[str], sink: Sink, *, close_timeout: float = DEFAULT_CLOSE_TIMEOUT
) -> Optional[UsageRecord]:
    """Read, count, emit, and only then advance the baseline.

    The order is the requirement (FR-012), not an implementation detail: the
    baseline is written **after** :func:`~tokenweir.sink.emit_usage` has taken the
    record, so a turn the emitter refuses is carried into the next turn's delta
    rather than deleted. A baseline advanced first would make every failed emit a
    permanent, silent loss.

    "Taken the record" is deliberately the *strong* reading: the baseline advances
    only once the emitter reports the record **delivered**, not merely accepted.
    :class:`~tokenweir.emitter.BufferedEmitter` is fire-and-forget by contract, so
    a record it accepted may still be dropped later against a dead broker — and
    accepting that as good enough would quietly reintroduce the loss this design
    exists to prevent, since the baseline would advance for a record nothing ever
    stored. This process is short-lived and emits exactly one record, so it can
    afford what a metered request path cannot: it closes the emitter, which flushes
    within the bounded timeout, and reads :meth:`BufferedEmitter.stats` to find out
    what actually happened. A broker that is down therefore carries the turn's
    tokens forward until it comes back, rather than losing them.

    That flush is bounded, not unbounded, so this stays inside the hook's own
    budget (FR-027, FR-028) — the wait is the same one ``close`` already promises.

    Emission goes through the buffered client and the guarded seam rather than a
    bare ``sink.emit`` (FR-029). Both guarantees already exist in the library; the
    fourth reimplementation of a guarantee is where it stops being one.

    Returns:
        The emitted record, or ``None`` when there was nothing to emit (no
        transcript, no new tokens, a replaced transcript) or the emit was refused.
    """
    hook_input = read_hook_input(stream)
    if hook_input.transcript_path is None:
        return None

    scan = scan_transcript(hook_input.transcript_path)
    state_path = state_path_for(hook_input.transcript_path)
    baseline = read_baseline(state_path)

    delta = turn_delta(scan.totals, baseline)
    if delta is None:
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

    emitter = BufferedEmitter(sink, close_timeout=close_timeout)
    try:
        record = emit_usage(emitter, fields)
    finally:
        # Closing flushes within `close_timeout` and is what turns "accepted" into
        # a knowable "delivered" below. In a `finally` so that a guarded seam that
        # somehow raised still leaves no worker thread behind.
        emitter.close()

    delivered = emitter.stats().delivered

    if record is None or delivered < 1:
        # Refused at the seam (a malformed record, a sink that raised) or accepted
        # and then not delivered (a dead broker). Either way the baseline stays
        # where it is, so these tokens are reported next turn instead of vanishing.
        _note("record was not delivered; its tokens carry into the next turn")
        return None

    write_baseline(state_path, scan.totals, transcript=hook_input.transcript_path)
    return record


# --- the time budget -------------------------------------------------------


class _BudgetExpiredError(Exception):
    """Raised on the main thread when the hook's own time budget runs out."""


def _budget_seconds() -> float:
    """The configured budget, falling back to the default for anything unusable."""
    raw = _env("TOKENWEIR_HOOK_TIMEOUT")
    if raw is None:
        return DEFAULT_HOOK_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        _note(f"TOKENWEIR_HOOK_TIMEOUT={raw!r} is not a number; using the default")
        return DEFAULT_HOOK_TIMEOUT
    if value != value or value <= 0 or value == float("inf"):
        _note(f"TOKENWEIR_HOOK_TIMEOUT={raw!r} is not a usable budget; using the default")
        return DEFAULT_HOOK_TIMEOUT
    return value


def _arm_budget(seconds: float) -> Optional[Any]:
    """Arm the alarm, or return ``None`` where it cannot be armed (FR-027).

    ``SIGALRM`` exists on POSIX and only on the main thread. Where either is
    untrue the budget is simply not armed — degrading to no budget is correct,
    since the alternative (refusing to run) would trade a bounded risk for a
    certain failure. Everything else in :func:`main` still holds.
    """
    if not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"):
        return None
    if threading.current_thread() is not threading.main_thread():
        return None

    def _fire(signum: int, frame: Any) -> None:
        raise _BudgetExpiredError(f"hook exceeded its {seconds}s budget")

    try:
        previous = signal.signal(signal.SIGALRM, _fire)
        signal.setitimer(signal.ITIMER_REAL, seconds)
        return previous
    except Exception:  # pragma: no cover - platform-dependent
        return None


def _disarm_budget(previous: Optional[Any]) -> None:
    """Cancel the alarm and put the previous handler back. Never raises."""
    try:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
        if previous is not None:
            signal.signal(signal.SIGALRM, previous)
    except Exception:  # pragma: no cover - platform-dependent
        pass


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
    :mod:`tokenweir.sink` and :mod:`tokenweir.emitter` draw. The budget's own
    exception is an ordinary ``Exception`` and lands in the same handler.
    """
    del argv  # the hook takes no arguments; the payload arrives on stdin
    previous = _arm_budget(_budget_seconds())
    try:
        sink = select_sink()
        try:
            run(sys.stdin, sink)
        finally:
            # `BufferedEmitter.close` already closed it — this is for the paths
            # that never reached the emitter (no transcript, no new tokens), where
            # a constructed sink would otherwise hold a connection until the
            # process exits.
            try:
                sink.close()
            except Exception:
                pass
    except _BudgetExpiredError:
        _note("hook exceeded its time budget; exiting without disturbing the session")
    except Exception:
        _note("hook failed; exiting 0 so the session is untouched", exc_info=True)
    finally:
        _disarm_budget(previous)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised as a subprocess
    raise SystemExit(main())
