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

Validation is deliberately loud: constructing a record with a blank identity
field or a negative token count raises :class:`ValueError`, because an
unattributable record is a producer-side bug and silently metering garbage is
worse than failing. This does not weaken ADR-0001's off-critical-path guarantee,
which constrains :meth:`tokenweir.sink.Sink.emit` — the emit path — not record
construction. ``Sink.emit`` still must never raise.
"""

from __future__ import annotations

import json
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
    """Require a non-blank string. Whitespace-only is blank — '  ' is not an app id."""
    if not isinstance(value, str):
        raise ValueError(
            f"{name} is required and must be a string; got {type(value).__name__} {value!r}"
        )
    if not value.strip():
        raise ValueError(f"{name} is required and must not be blank")


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


@dataclass(slots=True)
class UsageRecord:
    """One metered unit of work (one model call / iteration).

    Raw token counts are stored; cost is derived at report time and never lives
    on the record.
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

        self.pricing_mode = PricingMode.coerce(self.pricing_mode)

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
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        """Rebuild from a dict.

        Unknown keys are ignored so a payload from a newer producer still reads
        (forward compatibility), and the payload's ``schema_version`` is preserved
        rather than replaced with this library's.

        Raises:
            ValueError: if a required field is absent, or any value is invalid.
        """
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in data.items() if k in known}

        missing = [name for name in REQUIRED_FIELDS if name not in kwargs]
        if missing:
            raise ValueError(
                "usage record payload is missing required field(s): "
                + ", ".join(missing)
            )

        return cls(**kwargs)

    @classmethod
    def from_json(cls, payload: Union[str, bytes, bytearray]) -> UsageRecord:
        """Rebuild from a JSON string or bytes.

        Raises:
            ValueError: if the payload is not a JSON object, or the record is
                invalid. (:class:`json.JSONDecodeError` is a ``ValueError``.)
        """
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

_NULLABLE_STRING = {"type": ["string", "null"]}
_NULLABLE_INTEGER = {"type": ["integer", "null"], "minimum": 0}
_TOKEN_COUNT = {"type": "integer", "minimum": 0, "default": 0}


def usage_record_json_schema() -> dict[str, Any]:
    """Return the JSON Schema describing a serialized :class:`UsageRecord`.

    The document describes **this** contract version: ``schema_version`` is
    pinned with ``const``, and a future version ships its own schema file.
    ``additionalProperties`` stays open because unknown fields are tolerated by
    design (forward compatibility).
    """
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://github.com/mikemcg52/tokenweir/schema/usage-record.v{SCHEMA_VERSION}.json",
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
                "description": "The contract version this record obeys.",
            },
            "request_id": {"type": "string", "minLength": 1},
            "app_id": {"type": "string", "minLength": 1},
            "endpoint": {"type": "string", "minLength": 1},
            "model": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "Opaque, provider-neutral model identifier, stored verbatim."
                ),
            },
            "status": {"type": "string", "minLength": 1},
            "workload": _NULLABLE_STRING,
            "parent_request_id": _NULLABLE_STRING,
            "queue": _NULLABLE_STRING,
            "input_tokens": _TOKEN_COUNT,
            "output_tokens": _TOKEN_COUNT,
            "cache_creation_input_tokens": _TOKEN_COUNT,
            "cache_read_input_tokens": _TOKEN_COUNT,
            "latency_ms": _NULLABLE_INTEGER,
            "pricing_mode": {
                "type": ["string", "null"],
                "enum": [*(member.value for member in PricingMode), None],
                "description": (
                    "'api_metered' is costable against a rate card; "
                    "'subscription' is flat-rate, where no per-call cost exists."
                ),
            },
            "ts": {
                **_NULLABLE_STRING,
                "description": "ISO-8601 UTC timestamp, stamped by the producer.",
            },
        },
        "required": list(REQUIRED_FIELDS),
        "additionalProperties": True,
    }
