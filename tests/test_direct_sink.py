"""The broker-less path (TOKWEIR-6).

`DirectSink` is the half of ADR-0001 Pillar 2's consequence that has no broker in
it — *"the EKS cloud-edge can drop the broker entirely and write direct/in-process"*.
It sits exactly on the seam where the write side's contract (``Source.write`` **may**
raise, so a consumer can retry) meets the emit side's (``Sink.emit`` may **not**), so
most of what is asserted here is that it converts one into the other without losing
the count.
"""

import logging
import threading

import pytest

from tokenweir import (
    BufferedEmitter,
    DirectSink,
    MemorySource,
    Sink,
    UsageRecord,
    emit_usage,
)
from tokenweir.sink import BatchSink

TIMEOUT = 10.0


def _record(n: int = 0, **overrides) -> UsageRecord:
    fields = {
        "request_id": f"req-{n}",
        "app_id": "tokenweir-tests",
        "endpoint": "/v1/messages",
        "model": "claude-opus-5",
        "status": "ok",
    }
    fields.update(overrides)
    return UsageRecord(**fields)


class CountingSource:
    """A `Source` that remembers how the records arrived, not just that they did."""

    def __init__(self):
        self.records = []
        self.writes = []
        self.closed = 0

    def write(self, records):
        batch = list(records)
        self.writes.append(len(batch))
        self.records.extend(batch)
        return len(batch)

    def close(self):
        self.closed += 1


class RaisingSource:
    def __init__(self, exc=RuntimeError("the database is gone")):
        self.exc = exc
        self.calls = 0

    def write(self, records):
        self.calls += 1
        raise self.exc

    def close(self):
        return None


# --- It is a Sink, and a batch-capable one (FR-015, FR-018) ------------------


def test_it_satisfies_both_protocols():
    sink = DirectSink(MemorySource())
    assert isinstance(sink, Sink)
    assert isinstance(sink, BatchSink)


def test_records_reach_the_source():
    source = MemorySource()
    sink = DirectSink(source)
    sink.emit(_record(1))
    sink.emit(_record(2))
    assert [r.request_id for r in source.records] == ["req-1", "req-2"]
    assert sink.written == 2
    assert sink.dropped == 0


# --- One transaction per batch (FR-017, SC-006) ------------------------------


def test_a_batch_is_one_write_call_not_one_per_record():
    """`PostgresSource.write` puts a whole batch in one transaction. That guarantee
    is only worth having if a batch arrives as a batch."""
    source = CountingSource()
    sink = DirectSink(source)
    sink.emit_batch([_record(n) for n in range(50)])
    assert source.writes == [50], "the batch was taken apart"
    assert len(source.records) == 50


def test_the_emitter_delivers_whole_batches_through_it():
    """End to end: the client's batching policy is what turns per-record emits into
    per-batch transactions on the broker-less path."""
    source = CountingSource()
    emitter = BufferedEmitter(DirectSink(source), batch_size=100, linger=0.05)
    try:
        for n in range(200):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        emitter.close()
    assert sum(source.writes) == 200
    assert len(source.writes) < 200, "one transaction per record"
    assert max(source.writes) <= 100


def test_an_empty_batch_opens_no_transaction():
    """A flush tick with nothing buffered must not be a round trip — on an idle
    service that is one pointless transaction per tick, forever."""
    source = CountingSource()
    DirectSink(source).emit_batch([])
    assert source.writes == []


def test_a_generator_is_materialized_before_the_source_sees_it():
    source = CountingSource()
    DirectSink(source).emit_batch(_record(n) for n in range(5))
    assert source.writes == [5]


# --- Store failures become drops, never raises (FR-016, SC-003) --------------


def test_a_store_failure_never_reaches_the_caller():
    source = RaisingSource()
    sink = DirectSink(source)
    sink.emit(_record(1))  # must not raise
    assert sink.written == 0
    assert sink.dropped == 1


def test_a_failed_batch_is_counted_whole():
    """`rows_for` validates before opening a transaction precisely so a batch fails
    whole rather than in part. The count has to say the same thing."""
    sink = DirectSink(RaisingSource())
    sink.emit_batch([_record(n) for n in range(10)])
    assert sink.dropped == 10
    assert sink.written == 0


def test_a_generator_that_raises_while_draining_is_contained():
    def exploding():
        yield _record(1)
        raise RuntimeError("the producer failed mid-batch")

    sink = DirectSink(MemorySource())
    sink.emit_batch(exploding())  # must not raise
    assert sink.written == 0


def test_a_store_failure_does_not_stop_later_records():
    class FailsOnce:
        def __init__(self):
            self.records = []
            self.first = True

        def write(self, records):
            if self.first:
                self.first = False
                raise RuntimeError("transient")
            batch = list(records)
            self.records.extend(batch)
            return len(batch)

        def close(self):
            return None

    source = FailsOnce()
    sink = DirectSink(source)
    sink.emit(_record(1))
    sink.emit(_record(2))
    assert [r.request_id for r in source.records] == ["req-2"]
    assert sink.dropped == 1
    assert sink.written == 1


def test_a_store_failure_through_the_emitter_never_reaches_the_caller():
    emitter = BufferedEmitter(DirectSink(RaisingSource()), linger=0.0)
    try:
        for n in range(5):
            emitter.emit(_record(n))  # must not raise
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        emitter.close()
    # The sink swallows the store failure, so from the client's side delivery
    # succeeded. That asymmetry is deliberate and worth pinning: a conforming sink
    # reports its own drops, and `DirectSink.dropped` is where they are.
    assert emitter.stats().delivered == 5
    assert emitter.stats().failed == 0


def test_the_sinks_own_counter_is_where_a_swallowed_drop_shows_up():
    sink = DirectSink(RaisingSource())
    emitter = BufferedEmitter(sink, linger=0.0)
    try:
        for n in range(5):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        emitter.close()
    assert sink.dropped == 5
    assert sink.written == 0


def test_a_store_failure_is_logged():
    logger = logging.getLogger("tokenweir.sink")
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record)

    handler = Capture()
    logger.addHandler(handler)
    try:
        DirectSink(RaisingSource()).emit(_record(1))
    finally:
        logger.removeHandler(handler)
    assert seen, "a dropped batch produced no warning"


def test_repeated_store_failures_do_not_log_once_per_record(caplog):
    with caplog.at_level(logging.WARNING, logger="tokenweir.sink"):
        sink = DirectSink(RaisingSource(), warn_interval=3600.0)
        for n in range(200):
            sink.emit(_record(n))
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert 0 < len(warnings) <= 5, f"{len(warnings)} warnings for 200 failures"
    assert sink.dropped == 200


# --- Lifecycle and ownership (FR-023 by analogy) -----------------------------


def test_a_borrowed_source_is_left_open():
    """The rule `PostgresSource` already holds: closing something handed to us
    would surprise whoever else holds it, including a connection pool."""
    source = CountingSource()
    sink = DirectSink(source)
    sink.close()
    assert source.closed == 0


def test_an_owned_source_is_closed():
    source = CountingSource()
    sink = DirectSink(source, owns_source=True)
    sink.close()
    assert source.closed == 1


def test_close_is_idempotent():
    source = CountingSource()
    sink = DirectSink(source, owns_source=True)
    sink.close()
    sink.close()
    sink.close()
    assert source.closed == 1


def test_emitting_after_close_is_a_counted_drop_not_a_write():
    source = CountingSource()
    sink = DirectSink(source)
    sink.close()
    sink.emit(_record(1))  # must not raise
    assert source.writes == []
    assert sink.dropped == 1


def test_the_source_is_reachable_for_a_caller_that_needs_it():
    source = MemorySource()
    assert DirectSink(source).source is source


# --- Concurrency -------------------------------------------------------------


def test_the_counters_survive_concurrent_emits():
    source = MemorySource()
    lock = threading.Lock()

    class LockedSource:
        def write(self, records):
            batch = list(records)
            with lock:
                source.records.extend(batch)
            return len(batch)

        def close(self):
            return None

    sink = DirectSink(LockedSource())
    threads = [
        threading.Thread(target=lambda t=t: [sink.emit(_record(t * 100 + n)) for n in range(100)])
        for t in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
    assert sink.written == 800
    assert len(source.records) == 800


# --- The story's first acceptance clause, broker-less half (SC-001) ----------


def test_a_service_emits_via_the_client_with_a_direct_sink():
    """The end-to-end shape a broker-less deployment actually writes."""
    source = MemorySource()
    fields = {
        "request_id": "req-cloud-edge",
        "app_id": "mado-cloud-edge",
        "endpoint": "/v1/messages",
        "model": "claude-opus-5",
        "status": "ok",
    }
    with BufferedEmitter(DirectSink(source), linger=0.0) as emitter:
        assert emit_usage(emitter, fields, input_tokens=120, output_tokens=34) is not None
        assert emitter.flush(timeout=TIMEOUT)

    assert len(source.records) == 1
    stored = source.records[0]
    assert stored.request_id == "req-cloud-edge"
    assert stored.input_tokens == 120
    assert stored.output_tokens == 34


@pytest.mark.parametrize("value", [None, "not a record", 42, {"request_id": "r"}])
def test_a_non_record_does_not_reach_the_store_through_the_client(value):
    source = CountingSource()
    with BufferedEmitter(DirectSink(source), linger=0.0) as emitter:
        emitter.emit(value)
        assert emitter.flush(timeout=TIMEOUT)
    assert source.writes == []


# --- Non-records are refused, as AMQPSink refuses them (Low #1) --------------


@pytest.mark.parametrize("value", [None, "not a record", 42, {"request_id": "r"}])
def test_a_non_record_never_reaches_the_store(value):
    """`MemorySource` validates nothing, so a `None` from a construction drop used
    to be *stored*. `AMQPSink` refuses the same value at the same seam; the two
    adapters agreeing is the point."""
    source = CountingSource()
    sink = DirectSink(source)
    sink.emit(value)  # must not raise
    assert source.records == []
    assert source.writes == []
    assert sink.dropped == 1


def test_a_non_record_does_not_cost_the_good_records_in_its_batch():
    """Filtered rather than refusing the batch whole — the one place this
    deliberately differs from `rows_for`. That rule exists so a batch is never
    *half* written; a value that could never have been written at all is not the
    same hazard, and losing four good records to one producer `None` would be."""
    source = CountingSource()
    sink = DirectSink(source)
    sink.emit_batch([_record(1), None, _record(2), "junk", _record(3)])
    assert [r.request_id for r in source.records] == ["req-1", "req-2", "req-3"]
    assert source.writes == [3], "the good records were not written as one batch"
    assert sink.dropped == 2
    assert sink.written == 3


def test_a_batch_of_only_non_records_opens_no_transaction():
    source = CountingSource()
    sink = DirectSink(source)
    sink.emit_batch([None, None])
    assert source.writes == []
    assert sink.dropped == 2
