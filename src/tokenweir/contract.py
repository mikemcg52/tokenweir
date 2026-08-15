"""The usage-record contract — the serializable, versioned unit of metering.

This is the stable data contract every producer emits and every store persists.
It is intentionally a plain dataclass (no pydantic / no third-party deps) so the
core stays dependency-light (ADR-0001 Pillar 2). Raw token counts are stored;
cost is computed at report time, never here.

**Provider-neutral** (ADR-0001 Pillar 3): ``model`` is an opaque identifier stored
verbatim. Nothing in this module validates, normalizes or infers a provider — a
local Ollama model and a Claude model are described identically.

**JSON is part of the contract; transport is not.** :meth:`UsageRecord.to_json`
and :meth:`UsageRecord.from_json` define the wire form. How those bytes travel —
AMQP, HTTP, a file, an in-process call — is an adapter's business, not this
module's.

Compatibility rules for ``schema_version``:

- ``SCHEMA_VERSION`` changes in lockstep with **any** field change to the record.
- Deserialization **ignores unknown fields**, so a record written by a newer
  producer still reads on an older consumer (forward compatibility).
- Deserialization **preserves the payload's** ``schema_version`` rather than
  substituting this library's, so a consumer can always tell what it received.
- The **published JSON Schema is deliberately stricter** than the Python type: it
  pins ``schema_version`` with ``const``, because ``usage-record.v1.json`` is the
  document that describes *version 1 records* and a version 2 record is described
  by its own file. The Python type stays forward-tolerant so a consumer can read a
  newer record and decide for itself; the schema answers the narrower question
  "is this a v1 record I fully understand?". Both behaviours are intentional and
  they are not in conflict — see :func:`usage_record_json_schema`.

Validation is deliberately loud: constructing a record with a blank identity
field or a negative token count raises :class:`ValueError`, because an
unattributable record is a producer-side bug and silently metering garbage is
worse than failing. This does not weaken ADR-0001's off-critical-path guarantee,
which constrains :meth:`tokenweir.sink.Sink.emit` — the emit path — not record
construction. ``Sink.emit`` still must never raise.

Two distinct error types, by design:

- **Omitting** a required constructor argument raises :class:`TypeError` — that is
  Python's own signature check, and keeping the required fields genuinely required
  (rather than giving them sentinel defaults) is what lets type checkers and IDEs
  catch the mistake before runtime.
- Supplying an **invalid value** raises :class:`ValueError`.

At the wire boundary the distinction disappears: :meth:`UsageRecord.from_dict` and
:meth:`UsageRecord.from_json` raise :class:`ValueError` for a missing *or* invalid
field, so a consumer parsing untrusted payloads catches one type.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, Optional, Union

SCHEMA_VERSION = 1

#: Fields that must be present and non-blank on every record. Single source of
#: truth for construction validation, deserialization, and the published schema.
REQUIRED_FIELDS: tuple[str, ...] = (
    "request_id",
    "app_id",
    "endpoint",
    "model",
    "status",
)

#: The raw token counts. Cost is derived from these at report time, never stored.
TOKEN_COUNT_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: Fields that are a string when present and ``None`` when not. The published
#: schema types them ``["string", "null"]``, so the library must refuse anything
#: else — otherwise it would emit records that fail its own published schema.
#: Unlike :data:`REQUIRED_FIELDS` these are *not* checked for blankness: the
#: schema places no such constraint on them, and the two must agree.
_OPTIONAL_STR_FIELDS: tuple[str, ...] = (
    "workload",
    "parent_request_id",
    "queue",
    "ts",
)

#: Integer-valued fields as they appear on the wire. JSON has a single number
#: type, so a non-Python producer may legitimately write ``100.0`` where it means
#: the integer 100 — JSON Schema's ``"type": "integer"`` accepts exactly that.
#: These are normalized on deserialization; see ``_coerce_wire_integers``.
_WIRE_INTEGER_FIELDS: tuple[str, ...] = (
    *TOKEN_COUNT_FIELDS,
    "latency_ms",
    "schema_version",
)

#: The union of Python's and ECMA-262's whitespace sets, written out explicitly
#: rather than relying on a shorthand class like ``\\s``.
#:
#: JSON Schema ``pattern`` is ECMA-262, whose whitespace set is *not* Python's:
#: Python counts U+001C-U+001F and U+0085, ECMA-262 counts U+FEFF. Using ``\\S`` on
#: both sides lets the two engines disagree, so a JavaScript or Go producer could
#: emit a value this library calls blank (or the reverse). Spelling the class out
#: makes every regex engine agree — that is the point of a cross-language contract.
#:
#: The *union* is used rather than ECMA's set alone so that adopting a shared rule
#: never made the library **less** strict than it was: the five code points Python
#: alone treats as whitespace stay blank, and U+FEFF becomes blank too.
_BLANK_CHARACTERS = (
    "\\t\\n\\v\\f\\r"           # TAB, LF, VT, FF, CR
    "\\u001c-\\u001f"       # FILE/GROUP/RECORD/UNIT SEPARATOR (Python only)
    " "                    # SPACE
    "\\u0085"               # NEXT LINE (Python only)
    "\\u00a0"               # NO-BREAK SPACE
    "\\u1680"               # OGHAM SPACE MARK
    "\\u2000-\\u200a"       # EN QUAD .. HAIR SPACE
    "\\u2028\\u2029"        # LINE SEPARATOR, PARAGRAPH SEPARATOR
    "\\u202f\\u205f\\u3000"  # NARROW NBSP, MEDIUM MATH SPACE, IDEOGRAPHIC SPACE
    "\\ufeff"               # ZERO WIDTH NO-BREAK SPACE / BOM (ECMA-262 only)
)

#: Matches a value containing at least one non-blank character. Shared by
#: :func:`_validate_required_str` and the published schema, so "blank" means the
#: same thing to this library and to a consumer validating against the schema.
#: Private: a regex is an implementation detail, not something a published library
#: should commit to as public API.
_NON_BLANK_PATTERN = f"[^{_BLANK_CHARACTERS}]"

_NON_BLANK_RE = re.compile(_NON_BLANK_PATTERN)


class PricingMode(str, Enum):
    """How the usage described by a record is billed (ADR-0001 Pillar 4).

    Both capture modes feed this same contract; the mode is what tells a
    report-time consumer whether a rate card even applies.

    - :attr:`API_METERED` — captured by interception at the Anthropic-compatible
      ``/v1/messages`` edge. A per-call cost is derivable from a rate card.
    - :attr:`SUBSCRIPTION` — captured from Claude Code's own transcript under a
      flat-rate Max subscription, where **no per-call dollar exists**. Raw counts
      are still meaningful; cost is not.
    """

    API_METERED = "api_metered"
    SUBSCRIPTION = "subscription"

    @classmethod
    def coerce(cls, value: Union["PricingMode", str, None]) -> Optional["PricingMode"]:
        """Normalize a member or its wire string to a member; ``None`` passes through.

        Producers in other codebases naturally hand over the wire string, while
        consumers want a typed value. Normalizing here means there is exactly one
        representation inside the process and exactly one on the wire.

        Raises:
            ValueError: if the value is neither ``None`` nor a permitted mode.
        """
        if value is None:
            return None
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            try:
                return cls(value)
            except ValueError:
                pass
        permitted = ", ".join(repr(member.value) for member in cls)
        raise ValueError(
            f"pricing_mode must be one of {permitted}, or None; got {value!r}"
        )


def _validate_required_str(name: str, value: Any) -> None:
    """Require a non-blank string. Whitespace-only is blank — '  ' is not an app id.

    Blankness is decided by :data:`_NON_BLANK_PATTERN`, the same expression the
    published schema carries, rather than by ``str.strip()`` — Python's notion of
    whitespace differs from ECMA-262's, and the two must agree or a non-Python
    producer could emit a value this library refuses.
    """
    if not isinstance(value, str):
        raise ValueError(
            f"{name} is required and must be a string; got {type(value).__name__} {value!r}"
        )
    if not _NON_BLANK_RE.search(value):
        raise ValueError(f"{name} is required and must not be blank")


def _validate_optional_str(name: str, value: Any) -> None:
    """Require a string or ``None`` — the shape the published schema declares."""
    if value is not None and not isinstance(value, str):
        raise ValueError(
            f"{name} must be a string or None; got {type(value).__name__} {value!r}"
        )


def _coerce_wire_integers(kwargs: dict[str, Any]) -> None:
    """Normalize integral JSON numbers to ``int``, in place.

    JSON has one number type, so ``100.0`` on the wire *is* the integer 100 —
    JSON Schema's ``"type": "integer"`` says so explicitly, and a JavaScript
    producer has no other way to write it. Python's ``int`` and ``float`` are
    distinct, so without this a payload that validates against the published
    schema would be refused by the library (the divergence FR-019 forbids).

    Only exactly-integral floats are converted. ``100.5``, ``NaN`` and ``Infinity``
    are left alone for :meth:`UsageRecord.__post_init__` to reject, so a genuinely
    fractional count still fails loudly rather than being silently truncated.

    Caveat: a float above 2**53 has already lost precision by the time it reaches
    here — ``json.loads`` produced it — so ``1e30`` converts to the nearest
    representable integer, not exactly 10**30. Nothing is truncated, but the value
    is approximate. No realistic token count or latency comes near that magnitude,
    and a producer needing exact large integers should write them as JSON integers,
    which are parsed exactly.
    """
    for name in _WIRE_INTEGER_FIELDS:
        value = kwargs.get(name)
        if isinstance(value, float) and value.is_integer():
            kwargs[name] = int(value)


def _validate_non_negative_int(name: str, value: Any, *, allow_none: bool) -> None:
    """Require a non-negative ``int``, rejecting ``bool``.

    ``bool`` is a subclass of ``int``, so a plain ``isinstance(x, int)`` check
    would silently accept ``True`` as a token count of 1 — a data-corruption path
    in a metering library, so it is closed explicitly.
    """
    if value is None:
        if allow_none:
            return
        raise ValueError(f"{name} must be an integer; got None")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an integer; got {type(value).__name__} {value!r}"
        )
    if value < 0:
        raise ValueError(f"{name} must be non-negative; got {value}")


@dataclass(slots=True, frozen=True)
class UsageRecord:
    """One metered unit of work (one model call / iteration).

    Raw token counts are stored; cost is derived at report time and never lives
    on the record.

    **Immutable.** A record is a value, and validation runs once at construction —
    if the fields could be reassigned afterwards, a validated record could be
    mutated into one that no longer satisfies its own contract and then serialized.
    Immutability also makes a record safe to hand to a buffering, fire-and-forget
    ``Sink`` without the emit path and the caller sharing mutable state. Build a
    variant with :func:`dataclasses.replace`, which re-runs validation.
    """

    request_id: str
    app_id: str
    endpoint: str
    model: str
    status: str

    # Attribution
    workload: Optional[str] = None
    parent_request_id: Optional[str] = None
    queue: Optional[str] = None

    # Raw token counts (cost is derived later, at report time)
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    # Operational
    latency_ms: Optional[int] = None
    pricing_mode: Optional[PricingMode] = None
    ts: Optional[str] = None  # ISO-8601 UTC; stamped by the producer

    schema_version: int = field(default=SCHEMA_VERSION)

    def __post_init__(self) -> None:
        for name in REQUIRED_FIELDS:
            _validate_required_str(name, getattr(self, name))

        for name in _OPTIONAL_STR_FIELDS:
            _validate_optional_str(name, getattr(self, name))

        for name in TOKEN_COUNT_FIELDS:
            _validate_non_negative_int(name, getattr(self, name), allow_none=False)

        _validate_non_negative_int("latency_ms", self.latency_ms, allow_none=True)

        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise ValueError(
                "schema_version must be an integer; got "
                f"{type(self.schema_version).__name__} {self.schema_version!r}"
            )
        if self.schema_version < 1:
            raise ValueError(
                f"schema_version must be >= 1; got {self.schema_version}"
            )

        # object.__setattr__ because the dataclass is frozen; normalizing the
        # value the caller passed is the one write the contract allows.
        object.__setattr__(self, "pricing_mode", PricingMode.coerce(self.pricing_mode))

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain, JSON-ready dict.

        ``pricing_mode`` is emitted as its plain wire string (or ``None``), never
        as a language-specific enum representation.
        """
        data = asdict(self)
        data["pricing_mode"] = (
            self.pricing_mode.value if self.pricing_mode is not None else None
        )
        return data

    def to_json(self, **dumps_kwargs: Any) -> str:
        """Serialize to a JSON string. Extra kwargs go to :func:`json.dumps`."""
        return json.dumps(self.to_dict(), **dumps_kwargs)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> UsageRecord:
        """Rebuild from a dict.

        Unknown keys are ignored so a payload from a newer producer still reads
        (forward compatibility), and the payload's ``schema_version`` is preserved
        rather than replaced with this library's.

        Raises:
            ValueError: if the payload is not a mapping, a required field is
                absent, or any value is invalid.
        """
        if not isinstance(data, Mapping):
            raise ValueError(
                "usage record payload must be a mapping; got "
                f"{type(data).__name__}"
            )

        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}

        missing = [name for name in REQUIRED_FIELDS if name not in kwargs]
        if missing:
            raise ValueError(
                "usage record payload is missing required field(s): "
                + ", ".join(missing)
            )

        _coerce_wire_integers(kwargs)
        return cls(**kwargs)

    @classmethod
    def from_json(cls, payload: Union[str, bytes, bytearray]) -> UsageRecord:
        """Rebuild from a JSON string or bytes.

        Raises:
            ValueError: if the payload is not a JSON string/bytes, does not decode
                to an object, or the record is invalid.
                (:class:`json.JSONDecodeError` is a ``ValueError``.)
        """
        if not isinstance(payload, (str, bytes, bytearray)):
            raise ValueError(
                "usage record JSON must be a string or bytes; got "
                f"{type(payload).__name__}"
            )
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise ValueError(
                "usage record JSON must decode to an object; got "
                f"{type(data).__name__}"
            )
        return cls.from_dict(data)


# --- Published schema -------------------------------------------------------
#
# The record's machine-readable description, for consumers that are not Python
# and for reviewing whether a change breaks the wire contract. Generated from
# this module so there is one source of truth; the checked-in artifact at
# ``schema/usage-record.v1.json`` is asserted by the test suite to match.

def usage_record_json_schema() -> dict[str, Any]:
    """Return the JSON Schema describing a serialized :class:`UsageRecord`.

    A fresh document is built on every call — no nested object is shared between
    calls or with module state, so a caller may annotate or mutate the result
    freely.

    The document describes **this** contract version and only this one:
    ``schema_version`` is pinned with ``const``, and a future version ships its
    own ``usage-record.vN.json``. That is deliberately stricter than
    :meth:`UsageRecord.from_dict`, which accepts and preserves a newer version so
    a Python consumer can decide for itself. The schema answers the narrower
    question "is this a v1 record I fully understand?"; a consumer that wants the
    tolerant behaviour should read the version field rather than validate.

    ``additionalProperties`` stays open because unknown fields are tolerated by
    design (forward compatibility) — a v1 record that has picked up a v2 field
    still validates.
    """

    def nullable_string(**extra: Any) -> dict[str, Any]:
        return {"type": ["string", "null"], **extra}

    def required_string(**extra: Any) -> dict[str, Any]:
        # minLength rejects ""; the pattern rejects "   ". Both mirror
        # _validate_required_str, so the schema and the library agree on "blank".
        return {
            "type": "string",
            "minLength": 1,
            "pattern": _NON_BLANK_PATTERN,
            **extra,
        }

    def token_count() -> dict[str, Any]:
        return {"type": "integer", "minimum": 0, "default": 0}

    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": (
            "https://raw.githubusercontent.com/mikemcg52/tokenweir/main/schema/"
            f"usage-record.v{SCHEMA_VERSION}.json"
        ),
        "title": "tokenweir usage record",
        "description": (
            "One metered unit of work (one model call / iteration). Raw token "
            "counts are stored; cost is derived at report time. Provider-neutral: "
            "'model' is an opaque identifier. This document describes contract "
            f"version {SCHEMA_VERSION}; unknown properties are permitted so that a "
            "record written by a newer producer still reads on an older consumer."
        ),
        "type": "object",
        "properties": {
            "schema_version": {
                "type": "integer",
                "const": SCHEMA_VERSION,
                "description": (
                    "The contract version this record obeys. Pinned: this "
                    "document validates version "
                    f"{SCHEMA_VERSION} records only, and a later version ships "
                    "its own schema file."
                ),
            },
            "request_id": required_string(),
            "app_id": required_string(),
            "endpoint": required_string(),
            "model": required_string(
                description=(
                    "Opaque, provider-neutral model identifier, stored verbatim."
                )
            ),
            "status": required_string(),
            "workload": nullable_string(),
            "parent_request_id": nullable_string(),
            "queue": nullable_string(),
            "input_tokens": token_count(),
            "output_tokens": token_count(),
            "cache_creation_input_tokens": token_count(),
            "cache_read_input_tokens": token_count(),
            "latency_ms": {"type": ["integer", "null"], "minimum": 0},
            "pricing_mode": {
                "type": ["string", "null"],
                "enum": [*(member.value for member in PricingMode), None],
                "description": (
                    "'api_metered' is costable against a rate card; "
                    "'subscription' is flat-rate, where no per-call cost exists."
                ),
            },
            "ts": nullable_string(
                description="ISO-8601 UTC timestamp, stamped by the producer."
            ),
        },
        # Strict in what it describes, lenient in what the reader accepts: every
        # record this library emits carries schema_version, so the published
        # document requires it and a non-Python producer knows to stamp it.
        # from_dict still tolerates its absence (defaulting to SCHEMA_VERSION) —
        # the schema may be stricter than the reader, never looser, which is the
        # direction FR-019 cares about.
        "required": ["schema_version", *REQUIRED_FIELDS],
        "additionalProperties": True,
    }
