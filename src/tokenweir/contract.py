"""The usage-record contract — the serializable, versioned unit of metering.

This is the stable data contract every producer emits and every store persists.
It is intentionally a plain dataclass (no pydantic / no third-party deps) so the
core stays dependency-light (ADR-0001 Pillar 2). Raw token counts are stored;
cost is computed at report time, never here.

TOKWEIR-1 extends this from the AI Gateway's ``gateway_usage`` schema — keep
``SCHEMA_VERSION`` in lockstep with any field change.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

SCHEMA_VERSION = 1


@dataclass(slots=True)
class UsageRecord:
    """One metered unit of work (one model call / iteration).

    Field set mirrors the gateway's usage record; extend during the TOKWEIR-1
    extraction rather than inventing a parallel schema.
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
    pricing_mode: Optional[str] = None
    ts: Optional[str] = None  # ISO-8601 UTC; stamped by the producer

    schema_version: int = field(default=SCHEMA_VERSION)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict (JSON/AMQP-ready)."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UsageRecord:
        """Rebuild from a dict, ignoring unknown keys for forward-compatibility."""
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        return cls(**{k: v for k, v in data.items() if k in known})
