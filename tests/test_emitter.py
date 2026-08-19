"""The buffered emitter client (TOKWEIR-6).

The story's third acceptance clause — "an emit failure never raises into the
caller" — is what most of this module is about, and it is asserted against the
whole failure surface rather than the one obvious case: a sink that raises, one
that blocks, one that is missing entirely, a buffer at its bound, a closed client,
and a value that is not a record.

**No test here sleeps to synchronize.** `flush(timeout=…)` and `close()` are
states the client can report exactly; a sleep is a guess that is both slower than
necessary and flaky on a loaded machine. Where a test needs the worker held still,
it holds it with a `threading.Event` and releases it deliberately.
"""

import logging
import subprocess
import sys
import threading
import time

import pytest

from tokenweir import (
    BufferedEmitter,
    EmitterStats,
    MemorySource,
    Sink,
    UsageRecord,
    emit_usage,
)
from tokenweir.sink import BatchSink, DirectSink

# A generous ceiling for "the worker got there". Every wait is on a real
# condition, so a healthy run never comes near it; it exists so a wedged test
# fails instead of hanging a suite.
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


class RecordingSink:
    """Accepts everything and remembers it. The baseline."""

    def __init__(self):
        self.records = []
        self.closed = 0
        self._lock = threading.Lock()

    def emit(self, record):
        with self._lock:
            self.records.append(record)

    def close(self):
        self.closed += 1


class RecordingBatchSink(RecordingSink):
    """Batch-capable, and records the batch boundaries as well as the records."""

    def __init__(self):
        super().__init__()
        self.batches = []

    def emit_batch(self, records):
        with self._lock:
            batch = list(records)
            self.batches.append(batch)
            self.records.extend(batch)


class RaisingSink:
    """Violates the `Sink` contract on every call, which is the point."""

    def __init__(self, exc=RuntimeError("broker is down")):
        self.exc = exc
        self.calls = 0

    def emit(self, record):
        self.calls += 1
        raise self.exc

    def close(self):
        return None


class BlockingSink:
    """Holds the worker inside `emit` until released."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.records = []

    def emit(self, record):
        self.records.append(record)
        self.entered.set()
        self.release.wait(TIMEOUT)

    def close(self):
        self.release.set()


class SinkThatRaisesOnClose:
    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def close(self):
        raise RuntimeError("the broker went away first")


# --- The contract: emitting never raises, never blocks (FR-002, SC-003) ------


def test_a_record_reaches_the_sink():
    sink = RecordingSink()
    with BufferedEmitter(sink, linger=0.0) as emitter:
        emitter.emit(_record(1))
        assert emitter.flush(timeout=TIMEOUT)
    assert [r.request_id for r in sink.records] == ["req-1"]


def test_a_sink_that_raises_never_reaches_the_caller():
    sink = RaisingSink()
    with BufferedEmitter(sink, linger=0.0) as emitter:
        for n in range(5):
            emitter.emit(_record(n))  # must not raise
        assert emitter.flush(timeout=TIMEOUT)
        stats = emitter.stats()
    assert stats.accepted == 5
    assert stats.failed == 5
    assert stats.delivered == 0
    # FR-005 stated directly rather than inferred: five records, five attempts.
    # A retry loop would satisfy every assertion above — the failure count would
    # still be per record — and only this one says the batch was not tried again.
    assert sink.calls == 5, f"the sink was called {sink.calls} times for 5 records"


def test_a_sink_that_raises_does_not_stop_the_worker():
    """FR-005: a failure is dropped, not fatal. The next record still goes."""

    class FailsOnce:
        def __init__(self):
            self.records = []
            self.first = True

        def emit(self, record):
            if self.first:
                self.first = False
                raise RuntimeError("transient")
            self.records.append(record)

        def close(self):
            return None

    sink = FailsOnce()
    with BufferedEmitter(sink, linger=0.0, batch_size=1) as emitter:
        emitter.emit(_record(1))
        assert emitter.flush(timeout=TIMEOUT)
        emitter.emit(_record(2))
        assert emitter.flush(timeout=TIMEOUT)
        stats = emitter.stats()
    assert [r.request_id for r in sink.records] == ["req-2"]
    assert stats.failed == 1
    assert stats.delivered == 1


def test_a_missing_sink_never_reaches_the_caller():
    """`None` is the shape a misconfigured deployment actually produces."""
    with BufferedEmitter(None, linger=0.0) as emitter:
        emitter.emit(_record(1))
        assert emitter.flush(timeout=TIMEOUT)
        assert emitter.stats().failed == 1


def test_emitting_does_not_wait_for_a_blocking_sink():
    """The whole point of the client: the caller's latency is an append, not the
    sink's. Asserted as a bound on wall-clock while the sink is provably held
    inside `emit`, so it measures the property rather than a fast machine."""
    sink = BlockingSink()
    emitter = BufferedEmitter(sink, linger=0.0, batch_size=1)
    try:
        emitter.emit(_record(0))
        assert sink.entered.wait(TIMEOUT), "the worker never entered the sink"

        started = time.monotonic()
        for n in range(1, 200):
            emitter.emit(_record(n))
        elapsed = time.monotonic() - started

        assert elapsed < 1.0, f"emitting blocked for {elapsed:.3f}s behind the sink"
        assert emitter.stats().accepted == 200
    finally:
        sink.release.set()
        emitter.close()


def test_a_non_record_is_refused_before_the_sink_sees_it():
    """FR-006. `None` is exactly what `build_record` returns on a drop, so the
    careless composition must not put it on the wire."""
    sink = RecordingSink()
    with BufferedEmitter(sink, linger=0.0) as emitter:
        emitter.emit(None)
        emitter.emit("not a record")
        emitter.emit({"request_id": "req-1"})
        assert emitter.flush(timeout=TIMEOUT)
        stats = emitter.stats()
    assert sink.records == []
    assert stats.dropped_not_a_record == 3
    assert stats.accepted == 0


def test_emitting_after_close_is_a_counted_drop_not_a_raise():
    sink = RecordingSink()
    emitter = BufferedEmitter(sink, linger=0.0)
    emitter.close()
    emitter.emit(_record(1))  # must not raise
    stats = emitter.stats()
    assert stats.dropped_closed == 1
    assert sink.records == []


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_base_exception_from_a_sink_is_not_swallowed_into_the_caller():
    """A `BaseException` in the worker must not be caught as if it were a drop —
    but it also must not escape into an emitting thread, because it is not that
    thread's exception. The worker dies — loudly, on `threading.excepthook`, which
    is why this test filters the warning pytest raises about it — and the client
    stays callable rather than deadlocking whoever calls `flush` or `close`."""

    class Interrupting:
        def emit(self, record):
            raise KeyboardInterrupt

        def close(self):
            return None

    emitter = BufferedEmitter(Interrupting(), linger=0.0)
    try:
        emitter.emit(_record(1))
        emitter.flush(timeout=TIMEOUT)
        emitter.emit(_record(2))  # still must not raise into the caller
    finally:
        emitter.close(timeout=1.0)


# --- The bound (FR-003, SC-004) ----------------------------------------------


def test_the_buffer_is_bounded_and_the_excess_is_counted():
    sink = BlockingSink()
    emitter = BufferedEmitter(sink, max_buffer=10, batch_size=1, linger=0.0)
    try:
        emitter.emit(_record(0))
        assert sink.entered.wait(TIMEOUT), "the worker never entered the sink"

        for n in range(1, 101):
            emitter.emit(_record(n))

        stats = emitter.stats()
        assert stats.buffered == 10, "the bound was not enforced"
        assert stats.accepted == 11  # the one in flight plus a full buffer
        assert stats.dropped_buffer_full == 90
        assert stats.dropped == 90
    finally:
        sink.release.set()
        emitter.close()


def test_a_full_buffer_keeps_the_oldest_records():
    """Drop-newest: what was accepted stays accepted. Evicting the oldest would
    turn a bounded, countable loss into an unbounded reshuffle of survivors."""
    sink = BlockingSink()
    emitter = BufferedEmitter(sink, max_buffer=3, batch_size=1, linger=0.0)
    try:
        emitter.emit(_record(0))
        assert sink.entered.wait(TIMEOUT)
        for n in range(1, 10):
            emitter.emit(_record(n))
        sink.release.set()
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        sink.release.set()
        emitter.close()
    assert [r.request_id for r in sink.records] == ["req-0", "req-1", "req-2", "req-3"]


# --- Batching (FR-004, FR-018, SC-006) ---------------------------------------


def test_a_batch_capable_sink_gets_whole_batches():
    sink = RecordingBatchSink()
    emitter = BufferedEmitter(sink, batch_size=100, linger=0.05)
    try:
        for n in range(50):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        emitter.close()
    assert len(sink.records) == 50
    assert sum(len(b) for b in sink.batches) == 50
    assert len(sink.batches) < 50, "records were delivered one at a time"


def test_a_plain_sink_gets_records_one_at_a_time():
    """FR-018's fallback. A sink without the capability is untouched by it."""
    sink = RecordingSink()
    assert not isinstance(sink, BatchSink)
    with BufferedEmitter(sink, linger=0.0) as emitter:
        for n in range(5):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    assert len(sink.records) == 5


def test_batch_size_is_respected():
    sink = RecordingBatchSink()
    emitter = BufferedEmitter(sink, batch_size=7, linger=0.05)
    try:
        for n in range(30):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    finally:
        emitter.close()
    assert all(len(b) <= 7 for b in sink.batches), [len(b) for b in sink.batches]
    assert len(sink.records) == 30


def test_an_idle_client_makes_no_delivery_call():
    """An idle service must not produce broker traffic or store transactions —
    the `linger` timer must not be a heartbeat."""

    class CountingSink:
        def __init__(self):
            self.calls = 0

        def emit(self, record):
            self.calls += 1

        def emit_batch(self, records):
            self.calls += 1

        def close(self):
            return None

    sink = CountingSink()
    with BufferedEmitter(sink, linger=0.01) as emitter:
        assert emitter.flush(timeout=1.0)
        assert emitter.flush(timeout=1.0)
    assert sink.calls == 0


# --- Lifecycle (FR-010, FR-011, FR-012, SC-008, SC-009) ----------------------


def test_close_flushes_what_is_buffered():
    sink = RecordingSink()
    emitter = BufferedEmitter(sink, linger=1.0, batch_size=100)
    for n in range(20):
        emitter.emit(_record(n))
    emitter.close(timeout=TIMEOUT)
    assert len(sink.records) == 20, "close abandoned buffered records"


def test_close_cuts_the_linger_short():
    """Batching must never be the reason a shutdown is slow."""
    sink = RecordingSink()
    emitter = BufferedEmitter(sink, linger=30.0, batch_size=100)
    emitter.emit(_record(1))
    started = time.monotonic()
    emitter.close(timeout=TIMEOUT)
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"close waited {elapsed:.1f}s for the linger"
    assert len(sink.records) == 1


def test_close_is_idempotent_and_closes_the_sink_once():
    sink = RecordingSink()
    emitter = BufferedEmitter(sink, linger=0.0)
    emitter.close()
    emitter.close()
    emitter.close()
    assert sink.closed == 1
    assert emitter.closed


def test_close_survives_a_sink_that_raises_on_close():
    emitter = BufferedEmitter(SinkThatRaisesOnClose(), linger=0.0)
    emitter.emit(_record(1))
    emitter.close(timeout=TIMEOUT)  # must not raise
    assert emitter.closed


def test_the_context_manager_closes():
    sink = RecordingSink()
    with BufferedEmitter(sink, linger=0.0) as emitter:
        emitter.emit(_record(1))
    assert emitter.closed
    assert sink.closed == 1
    assert len(sink.records) == 1


def test_the_context_manager_closes_even_when_the_body_raises():
    sink = RecordingSink()
    emitter = None
    with pytest.raises(ValueError):
        with BufferedEmitter(sink, linger=0.0) as emitter:
            emitter.emit(_record(1))
            raise ValueError("the metered work failed")
    assert emitter.closed
    assert len(sink.records) == 1


def test_close_is_bounded_by_its_timeout_against_a_wedged_sink():
    """SC-009's sibling: a dead broker must not be able to hang a shutdown."""
    sink = BlockingSink()
    emitter = BufferedEmitter(sink, linger=0.0, batch_size=1)
    emitter.emit(_record(0))
    assert sink.entered.wait(TIMEOUT)
    emitter.emit(_record(1))

    started = time.monotonic()
    emitter.close(timeout=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, f"close waited {elapsed:.1f}s on a wedged sink"
    sink.release.set()


def test_a_process_that_forgets_to_close_still_exits():
    """FR-012: the worker is a daemon, so metering can never hold a process open.
    Asserted in a subprocess because the property is about interpreter shutdown."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from tokenweir import BufferedEmitter, NullSink, UsageRecord;"
            "e = BufferedEmitter(NullSink());"
            "e.emit(UsageRecord(request_id='r', app_id='a', endpoint='/e',"
            " model='m', status='ok'));"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_atexit_hook_flushes_what_was_buffered(tmp_path):
    """The daemon worker means a clean exit would otherwise lose the buffer.

    The sink appends to a **file** rather than reporting from a second `atexit`
    handler: handlers run last-registered-first, so a reporting handler added after
    the client would run *before* the flush hook and would report zero however well
    the hook worked. The file is read after the interpreter is gone, which is the
    instant the assertion is actually about.
    """
    landed = tmp_path / "delivered.txt"
    program = (
        "from tokenweir import BufferedEmitter, UsageRecord\n"
        f"path = {str(landed)!r}\n"
        "class FileSink:\n"
        "    def emit(self, record):\n"
        "        with open(path, 'a') as fh:\n"
        "            fh.write(record.request_id + chr(10))\n"
        "    def close(self):\n"
        "        return None\n"
        # A long linger, and no close(): nothing but the atexit hook can deliver
        # this record, so the test cannot pass by accident.
        "emitter = BufferedEmitter(FileSink(), linger=30.0)\n"
        "emitter.emit(UsageRecord(request_id='req-atexit', app_id='a',\n"
        "                         endpoint='/e', model='m', status='ok'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert landed.is_file(), "the buffered record never reached the sink"
    assert landed.read_text().split() == ["req-atexit"]


# --- flush (FR-009) ----------------------------------------------------------


def test_flush_reports_a_timeout_rather_than_claiming_success():
    sink = BlockingSink()
    emitter = BufferedEmitter(sink, linger=0.0, batch_size=1)
    try:
        emitter.emit(_record(0))
        assert sink.entered.wait(TIMEOUT)
        emitter.emit(_record(1))
        assert emitter.flush(timeout=0.2) is False
    finally:
        sink.release.set()
        emitter.close()


def test_flush_on_an_empty_client_returns_immediately():
    with BufferedEmitter(RecordingSink(), linger=30.0) as emitter:
        started = time.monotonic()
        assert emitter.flush(timeout=TIMEOUT) is True
        assert time.monotonic() - started < 5.0


def test_flush_cuts_the_linger_short():
    sink = RecordingSink()
    with BufferedEmitter(sink, linger=30.0, batch_size=100) as emitter:
        emitter.emit(_record(1))
        started = time.monotonic()
        assert emitter.flush(timeout=TIMEOUT) is True
        assert time.monotonic() - started < 5.0
    assert len(sink.records) == 1


# --- Concurrency (FR-013, SC-005) --------------------------------------------


def test_ten_threads_emitting_lose_and_duplicate_nothing():
    sink = RecordingBatchSink()
    threads_count, per_thread = 10, 200
    emitter = BufferedEmitter(
        sink, max_buffer=threads_count * per_thread, batch_size=50, linger=0.01
    )
    barrier = threading.Barrier(threads_count)

    def worker(t: int) -> None:
        barrier.wait(TIMEOUT)
        for n in range(per_thread):
            emitter.emit(_record(0, request_id=f"req-{t}-{n}"))

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(TIMEOUT)
    emitter.close(timeout=TIMEOUT)

    expected = {f"req-{t}-{n}" for t in range(threads_count) for n in range(per_thread)}
    seen = [r.request_id for r in sink.records]
    assert len(seen) == len(expected), f"{len(seen)} delivered, {len(expected)} emitted"
    assert set(seen) == expected
    assert emitter.stats().delivered == len(expected)


# --- Logging (FR-008, SC-010) ------------------------------------------------


def test_a_systematically_failing_sink_does_not_log_once_per_record(caplog):
    with caplog.at_level(logging.WARNING, logger="tokenweir.emitter"):
        with BufferedEmitter(RaisingSink(), linger=0.0, warn_interval=3600.0) as emitter:
            for n in range(200):
                emitter.emit(_record(n))
            assert emitter.flush(timeout=TIMEOUT)
            assert emitter.stats().failed == 200
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert 0 < len(warnings) <= 5, f"{len(warnings)} warnings for 200 failures"


def test_a_drop_is_logged_at_all():
    """Rate limiting must not become silence: the first occurrence gets through."""
    logger = logging.getLogger("tokenweir.emitter")
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            seen.append(record)

    handler = Capture()
    logger.addHandler(handler)
    try:
        with BufferedEmitter(RaisingSink(), linger=0.0) as emitter:
            emitter.emit(_record(1))
            assert emitter.flush(timeout=TIMEOUT)
    finally:
        logger.removeHandler(handler)
    assert seen, "a delivery failure produced no warning at all"


def test_the_warning_reports_how_many_were_suppressed(caplog):
    from tokenweir._ratelimit import RateLimitedWarner

    # One reason, four occurrences. The window is per reason, so the message has to
    # be the *same* one each time — distinct texts are distinct reasons and each
    # gets its own first-occurrence line, which is the behaviour the tests above
    # pin. The clock is injected so this proves a 60-second window in no time at
    # all: a test that slept to prove a rate limit would be slow *and* flaky.
    ticks = iter([0.0, 1.0, 2.0, 100.0])
    warner = RateLimitedWarner(
        logging.getLogger("tokenweir.emitter"), interval=60.0, clock=lambda: next(ticks)
    )
    with caplog.at_level(logging.WARNING, logger="tokenweir.emitter"):
        for _ in range(4):
            warner.warn("the sink failed", exc_info=False)

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2, messages
    assert messages[0] == "the sink failed"
    assert "2 further occurrence(s) suppressed" in messages[1]
    assert messages[1].startswith("the sink failed")


def test_a_logging_handler_that_raises_cannot_reach_the_caller():
    """"Never raises" has to survive a hostile logging configuration, or the guard
    has merely moved the throw site onto the logger."""
    logger = logging.getLogger("tokenweir.emitter")

    class Hostile(logging.Handler):
        def emit(self, record):
            raise RuntimeError("the log pipeline is down too")

    handler = Hostile()
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        with BufferedEmitter(RaisingSink(), linger=0.0) as emitter:
            emitter.emit(_record(1))
            emitter.emit(None)
            assert emitter.flush(timeout=TIMEOUT)
    finally:
        logger.removeHandler(handler)


# --- Composition (FR-014) ----------------------------------------------------


def test_the_client_is_a_sink_and_a_batch_sink():
    with BufferedEmitter(RecordingSink(), linger=0.0) as emitter:
        assert isinstance(emitter, Sink)
        assert isinstance(emitter, BatchSink)


def test_emit_usage_composes_with_the_client():
    """The guarded seam and the buffered client, with nothing in between — the one
    call a metered request path should make."""
    sink = RecordingSink()
    fields = {
        "request_id": "req-1",
        "app_id": "gateway",
        "endpoint": "/v1/messages",
        "model": "claude-opus-5",
        "status": "ok",
    }
    with BufferedEmitter(sink, linger=0.0) as emitter:
        assert emit_usage(emitter, fields, input_tokens=10) is not None
        assert emit_usage(emitter, fields, app_id="") is None  # a construction drop
        assert emitter.flush(timeout=TIMEOUT)
        stats = emitter.stats()
    assert [r.request_id for r in sink.records] == ["req-1"]
    assert stats.accepted == 1
    assert stats.dropped_not_a_record == 0, "the drop happened before the client"


def test_a_client_can_front_another_client():
    inner_sink = RecordingSink()
    with BufferedEmitter(inner_sink, linger=0.0) as inner:
        with BufferedEmitter(inner, linger=0.0) as outer:
            outer.emit(_record(1))
            assert outer.flush(timeout=TIMEOUT)
        assert inner.flush(timeout=TIMEOUT)
    assert len(inner_sink.records) == 1


def test_the_client_delivers_to_a_direct_sink_over_a_source():
    source = MemorySource()
    with BufferedEmitter(DirectSink(source), linger=0.0) as emitter:
        for n in range(10):
            emitter.emit(_record(n))
        assert emitter.flush(timeout=TIMEOUT)
    assert len(source.records) == 10


# --- Construction validation -------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_buffer": 0},
        {"max_buffer": -1},
        {"max_buffer": 1.5},
        {"max_buffer": True},
        {"batch_size": 0},
        {"batch_size": "10"},
        {"linger": -1.0},
        {"linger": float("nan")},
        {"linger": float("inf")},
        {"linger": "soon"},
        {"close_timeout": -1.0},
    ],
)
def test_a_nonsensical_configuration_is_rejected_at_construction(kwargs):
    """Wiring time is the one place in the emit path where raising is right: there
    is no metered request in scope, and a silently-corrected bound would mean the
    client's memory behaviour is not what its arguments say."""
    with pytest.raises(ValueError):
        BufferedEmitter(RecordingSink(), **kwargs)


def test_stats_is_a_consistent_snapshot():
    # Closed rather than leaked: an unclosed client leaves a live thread and a
    # `_LIVE` entry for the atexit hook to reap, and it is the test most likely to
    # be blamed for a hang if the worker ever stops being a daemon.
    with BufferedEmitter(RecordingSink(), linger=0.0) as emitter:
        stats = emitter.stats()
    assert isinstance(stats, EmitterStats)
    assert stats == EmitterStats()
    assert stats.dropped == 0
    assert stats.worker_alive
    with pytest.raises(Exception):
        stats.accepted = 5  # frozen: a snapshot that could be edited is not one


# --- Distinct failure reasons are rate-limited separately (Med #2) -----------


def test_a_delivery_failure_does_not_silence_a_refused_record(caplog):
    """A single window shared across reasons does not merely suppress noise — it
    makes a *new* failure mode invisible for a whole interval because an unrelated
    one warned first, and then reports the suppressed count against whichever
    message gets through next. A count attached to the wrong cause is not a smaller
    truth than silence; it is a falsehood."""
    with caplog.at_level(logging.WARNING, logger="tokenweir.emitter"):
        with BufferedEmitter(RaisingSink(), linger=0.0, warn_interval=3600.0) as emitter:
            emitter.emit(_record(1))  # a delivery failure
            assert emitter.flush(timeout=TIMEOUT)
            emitter.emit(None)  # a refused non-record — a different reason
            assert emitter.flush(timeout=TIMEOUT)

    messages = [r.getMessage() for r in caplog.records]
    assert any("the sink failed" in m for m in messages), messages
    assert any("non-UsageRecord" in m for m in messages), messages


def test_a_suppressed_count_is_never_attributed_to_another_reason(caplog):
    """The sharper half of the same bug: with one window, the three refused records
    below would be counted and then reported on the *delivery failure* line, telling
    an operator the sink failed four times when it failed once."""
    with caplog.at_level(logging.WARNING, logger="tokenweir.emitter"):
        with BufferedEmitter(RaisingSink(), linger=0.0, warn_interval=3600.0) as emitter:
            for _ in range(3):
                emitter.emit(None)
            emitter.emit(_record(1))
            assert emitter.flush(timeout=TIMEOUT)
            stats = emitter.stats()

    assert stats.dropped_not_a_record == 3
    assert stats.failed == 1
    failures = [r.getMessage() for r in caplog.records if "the sink failed" in r.getMessage()]
    assert len(failures) == 1, failures
    assert "suppressed" not in failures[0], (
        "the sink-failure line is carrying another reason's suppressed count"
    )


def test_each_reason_keeps_its_own_suppressed_count():
    from tokenweir._ratelimit import RateLimitedWarner

    warner = RateLimitedWarner(logging.getLogger("tokenweir.emitter"), interval=3600.0)
    for _ in range(3):
        warner.warn("reason A", exc_info=False)
    for _ in range(5):
        warner.warn("reason B", exc_info=False)

    assert warner.suppressed("reason A") == 2
    assert warner.suppressed("reason B") == 4
    assert warner.suppressed() == 6


def test_an_interpolated_message_can_still_be_rate_limited_by_key(caplog):
    """A message carrying varying detail would otherwise look distinct every time
    and defeat the limiting entirely — which is why `key=` exists and why the AMQP
    reconnect warning, the one message in this package that interpolates, uses it."""
    from tokenweir._ratelimit import RateLimitedWarner

    warner = RateLimitedWarner(logging.getLogger("tokenweir.emitter"), interval=3600.0)
    with caplog.at_level(logging.WARNING, logger="tokenweir.emitter"):
        for attempt in range(10):
            warner.warn(f"could not connect (attempt {attempt})", key="reconnect", exc_info=False)
    assert len(caplog.records) == 1, [r.getMessage() for r in caplog.records]


# --- A dead worker says so (Low #4) ------------------------------------------


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_stats_reports_a_worker_killed_by_a_base_exception():
    """`BaseException` from a sink is deliberately not caught, so the worker dies
    and nothing further is ever delivered. Without this flag that state shows only
    as `buffered` climbing while `delivered` does not — a symptom, not a diagnosis,
    and the README calls `stats()` the signal to alert on."""

    class Interrupting:
        def emit(self, record):
            raise KeyboardInterrupt

        def close(self):
            return None

    emitter = BufferedEmitter(Interrupting(), linger=0.0)
    try:
        emitter.emit(_record(0))
        emitter.flush(timeout=TIMEOUT)

        for n in range(1, 20):
            emitter.emit(_record(n))

        stats = emitter.stats()
        assert stats.worker_alive is False, "a dead worker is reported as healthy"
        assert stats.buffered > 0
        assert stats.delivered == 0
    finally:
        emitter.close(timeout=1.0)


def test_a_healthy_client_reports_a_live_worker():
    with BufferedEmitter(RecordingSink(), linger=0.0) as emitter:
        emitter.emit(_record(1))
        assert emitter.flush(timeout=TIMEOUT)
        assert emitter.stats().worker_alive is True
