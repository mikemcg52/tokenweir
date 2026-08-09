"""Sink — the emit side of the pipeline.

A ``Sink`` is what instrumented code (the gateway, the MADO cloud-edge, an OSS
caller) hands a :class:`~tokenweir.contract.UsageRecord` to. By contract emission
is **fire-and-forget and off the critical path** (ADR-0001 Pillar 2): ``emit``
must buffer/return immediately and must never raise into the caller — a metering
outage can never affect the availability of the thing being metered.

Transport-specific sinks (AMQP, HTTP, direct-to-store) are adapters shipped as
optional extras; the core only defines the interface plus a no-op default.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from tokenweir.contract import UsageRecord


@runtime_checkable
class Sink(Protocol):
    """Emit-side interface. Implementations MUST NOT raise from ``emit``."""

    def emit(self, record: UsageRecord) -> None:
        """Accept a record for delivery. Fire-and-forget; never blocks on I/O
        in a way that can fail the caller, never raises."""
        ...

    def close(self) -> None:
        """Flush and release resources. Safe to call more than once."""
        ...


class NullSink:
    """A Sink that drops everything. The safe default and a test double."""

    def emit(self, record: UsageRecord) -> None:  # noqa: D102
        return None

    def close(self) -> None:  # noqa: D102
        return None
