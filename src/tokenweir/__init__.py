"""token-weir — provider-neutral, transport-agnostic usage & cost metering.

Extraction target for TOKWEIR-1 (see docs ADR-0001). This package defines the
stable seams the gateway's usage pipeline is being pulled into:

- ``contract``  — the versioned, serializable usage-record contract
- ``sink``      — the emit-side interface (fire-and-forget, off the critical path)
- ``source``    — the write-side interface (persists records to a store)

Transport adapters (e.g. AMQP) live behind optional extras (``tokenweir[amqp]``)
so the core carries no wire dependencies.
"""

from tokenweir.contract import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    TOKEN_COUNT_FIELDS,
    PricingMode,
    UsageRecord,
    usage_record_json_schema,
)
from tokenweir.sink import NullSink, Sink
from tokenweir.source import MemorySource, Source

__all__ = [
    "SCHEMA_VERSION",
    "REQUIRED_FIELDS",
    "TOKEN_COUNT_FIELDS",
    "UsageRecord",
    "PricingMode",
    "usage_record_json_schema",
    "Sink",
    "NullSink",
    "Source",
    "MemorySource",
]

__version__ = "0.0.0"
