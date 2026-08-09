"""Source — the write side of the pipeline.

A ``Source`` is the consumer end: it receives :class:`~tokenweir.contract.UsageRecord`
batches and persists them to a store (Postgres today, per the extracted
usage-writer). Unlike the emit side, the write side *may* fail and retry — it runs
off the request's critical path, so durability matters more than latency here.

Concrete stores (Postgres, etc.) are adapters; the core defines the interface
plus an in-memory implementation for tests.
"""

from __future__ import annotations

from typing import Iterable, Protocol, runtime_checkable

from tokenweir.contract import UsageRecord


@runtime_checkable
class Source(Protocol):
    """Write-side interface: persist a batch of records to a store."""

    def write(self, records: Iterable[UsageRecord]) -> int:
        """Persist records. Returns the count written. May raise on store failure
        so the caller can retry (this side is off the critical path)."""
        ...

    def close(self) -> None:
        """Flush and release resources. Safe to call more than once."""
        ...


class MemorySource:
    """An in-memory Source for tests and local runs."""

    def __init__(self) -> None:
        self.records: list[UsageRecord] = []

    def write(self, records: Iterable[UsageRecord]) -> int:  # noqa: D102
        batch = list(records)
        self.records.extend(batch)
        return len(batch)

    def close(self) -> None:  # noqa: D102
        return None
