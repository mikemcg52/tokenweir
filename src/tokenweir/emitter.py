"""The buffered emitter client — the emit side made safe by construction.

ADR-0001 Pillar 2 says emission is *"fire-and-forget / off the critical path by
contract — buffers, returns immediately, swallows failures — preserving the
gateway's invariant that a logging outage cannot affect availability."* The
:class:`~tokenweir.sink.Sink` protocol states that contract; this module is what
actually **discharges** it.

Why it is not the adapter's job
-------------------------------

``Sink.emit`` already says "buffer/return immediately, never raise", so in
principle every adapter could satisfy it alone. In practice that means every
adapter re-implements buffering, a worker thread, a bound and a drop policy —
the guarantee written four times and wrong once. Worse, the natural way to write
an AMQP sink is a blocking ``basic_publish``, which satisfies "never raises" and
violates "off the critical path" without ever looking wrong.

:class:`BufferedEmitter` moves the guarantee into the library, so a transport
adapter is free to be a plain, blocking, obvious publisher and still be safe to
put on a metered request path. It is the same argument TOKWEIR-15 made for record
construction: the library owns the promise, and consumers adopt it by *calling*
rather than by re-deriving it.

It is itself a :class:`~tokenweir.sink.Sink`, so it composes with the guarded seam
with nothing in between::

    from tokenweir import BufferedEmitter, DirectSink, emit_usage

    with BufferedEmitter(DirectSink(source)) as emitter:
        emit_usage(emitter, fields)          # never raises, never blocks

Two policies worth reading before adopting
------------------------------------------

**The buffer is bounded, and a full buffer drops.** An unbounded buffer is not a
safety mechanism; it is a memory leak that takes down the metered service exactly
when the broker is down — the precise failure Pillar 2 exists to prevent. Blocking
the caller instead would be worse still, since that is the critical path. So the
bound is real and the excess is dropped, newest-first: records already accepted
were accepted, and evicting them to make room converts a bounded, countable loss
into an unbounded reshuffling of which records survive.

**Delivery is never retried.** "Swallows failures" is the contract. A retry loop
in front of a down broker fills the bounded buffer and turns record loss into
*more* record loss plus latency, and a poison record retried forever blocks every
record behind it. A failed batch is dropped and counted. What actually recovers
from an outage is **reconnection**, which belongs to the adapter and happens on a
later delivery attempt (see :class:`~tokenweir.amqp.AMQPSink`).

Neither drop is silent. Both are counted on :meth:`BufferedEmitter.stats` — the
signal an operator should alert on, and one that reaches a caller regardless of
logging configuration — and both are logged at ``WARNING``, rate-limited so a
systematically broken sink cannot produce one log line per metered request.

What this deliberately is not
-----------------------------

Not durable. Records live in memory; a hard process exit loses what is buffered.
Closing the client (or using it as a context manager) flushes within a bounded
timeout, and an :mod:`atexit` hook makes a best-effort attempt for callers who
forget — but a metering client that fsynced would be a queue, and the supported
way to survive a consumer outage is the broker, which is what a broker is for.

Not async-native. The metered callers ADR-0001 names are synchronous, and a
thread-based worker is callable from sync and async code alike; ``emit`` never
blocks, so it is safe from a coroutine without a thread-pool hop.
"""

from __future__ import annotations

import atexit
import logging
import threading
import time
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Iterable, List, Optional

from tokenweir._ratelimit import RateLimitedWarner
from tokenweir.contract import UsageRecord
from tokenweir.sink import BatchSink, Sink

__all__ = ["BufferedEmitter", "EmitterStats"]

_logger = logging.getLogger(__name__)

_BUFFER_FULL = (
    "tokenweir: dropping usage record — the emitter buffer is full; the sink is not "
    "keeping up or is failing. No metering for this call"
)
_NOT_A_RECORD = (
    "tokenweir: refusing to buffer a non-UsageRecord; no metering for this call"
)
_EMITTER_CLOSED = (
    "tokenweir: dropping usage record — the emitter is closed; no metering for this call"
)
_DELIVERY_FAILED = (
    "tokenweir: usage records not delivered — the sink failed; no metering for these calls"
)
_WORKER_DIED = (
    "tokenweir: the emitter's delivery worker stopped unexpectedly; buffered records "
    "will not be delivered"
)
_SINK_CLOSE_FAILED = "tokenweir: the sink raised while closing; releasing it anyway"
_FLUSH_TIMED_OUT = (
    "tokenweir: the emitter did not drain within its close timeout; buffered records "
    "may be lost"
)

#: Clients that are still open, so :func:`_close_all_at_exit` can make a
#: best-effort final flush. A :class:`weakref.WeakSet` because the registry must
#: not be the reason a client outlives its owner — an emitter that is garbage
#: has no buffer worth flushing.
_LIVE: "weakref.WeakSet[BufferedEmitter]" = weakref.WeakSet()


def _close_all_at_exit() -> None:
    """Best-effort flush of every open client at interpreter exit.

    Registered because the worker is a daemon thread: the interpreter will not
    wait for it, so without this a clean ``sys.exit`` silently loses whatever was
    buffered. ``atexit`` handlers run *before* daemon threads are torn down,
    which is what makes this work at all.

    It is best-effort by design, and bounded by each client's own close timeout —
    metering must not be able to hang a shutdown. Explicit :meth:`close` (or the
    context manager) remains the supported way to not lose records.
    """
    for emitter in list(_LIVE):
        try:
            emitter.close()
        except Exception:  # pragma: no cover - close is itself guarded
            pass


atexit.register(_close_all_at_exit)


@dataclass(frozen=True, slots=True)
class EmitterStats:
    """A point-in-time snapshot of a client's counters.

    A snapshot rather than live attributes: the counters are read under one lock,
    so the numbers in a snapshot are consistent with each other and an operator
    reporting them is not describing three different instants.

    Attributes:
        accepted: records taken into the buffer.
        delivered: records the sink accepted without raising.
        failed: records lost because delivery raised. Not retried, by contract.
        dropped_buffer_full: records lost because the buffer was at its bound —
            the sink is not keeping up, or is failing.
        dropped_not_a_record: values refused for not being a
            :class:`~tokenweir.contract.UsageRecord`, most often a ``None`` from a
            construction drop.
        dropped_closed: records offered after :meth:`BufferedEmitter.close`.
        buffered: records currently waiting for delivery.
    """

    accepted: int = 0
    delivered: int = 0
    failed: int = 0
    dropped_buffer_full: int = 0
    dropped_not_a_record: int = 0
    dropped_closed: int = 0
    buffered: int = 0

    @property
    def dropped(self) -> int:
        """Every record this client took responsibility for and did not deliver."""
        return (
            self.failed
            + self.dropped_buffer_full
            + self.dropped_not_a_record
            + self.dropped_closed
        )


class BufferedEmitter:
    """A :class:`~tokenweir.sink.Sink` that buffers and delivers from a worker thread.

    Wrap the sink a deployment actually uses — :class:`~tokenweir.amqp.AMQPSink`
    for the homelab's broker, :class:`~tokenweir.sink.DirectSink` for a
    broker-less one — and emit through this. :meth:`emit` appends and returns; a
    single daemon worker drains the buffer in batches and delivers.

    Args:
        sink: where records go. Not type-checked: a sink that turns out to be
            unusable (``None``, misconfigured, non-conforming) must degrade to
            counted drops rather than raise at wiring time in one deployment and
            on a request path in another. The failures are counted and logged.
        max_buffer: the bound. Reaching it drops rather than blocking or growing.
        batch_size: the most records handed to the sink at once.
        linger: how long the worker waits for a partial batch to fill before
            delivering it anyway. ``0`` delivers as soon as anything is buffered —
            lowest latency, most round trips. An explicit :meth:`flush` and
            :meth:`close` both cut the wait short, so this never delays a
            shutdown.
        close_timeout: default bound on :meth:`close`'s final flush, and the bound
            used by the ``atexit`` hook. A sink wedged against a dead broker can
            delay an exit by this much; ``0`` declines to wait at all.
        warn_interval: seconds between warning lines for a repeating failure.
        name: the worker thread's name, so it is identifiable in a stack dump.

    Raises:
        ValueError: for a nonsensical bound, batch size or timeout. Construction
            is wiring-time, not request-time, so this is the one place in the emit
            path where raising is the correct behaviour — the same reasoning
            :class:`~tokenweir.contract.UsageRecord` uses for its own validation.
    """

    def __init__(
        self,
        sink: Sink,
        *,
        max_buffer: int = 10_000,
        batch_size: int = 100,
        linger: float = 0.2,
        close_timeout: float = 5.0,
        warn_interval: float = 60.0,
        name: str = "tokenweir-emitter",
    ) -> None:
        self._max_buffer = _positive_int("max_buffer", max_buffer)
        self._batch_size = _positive_int("batch_size", batch_size)
        self._linger = _non_negative_float("linger", linger)
        self._close_timeout = _non_negative_float("close_timeout", close_timeout)

        self._sink = sink
        # Resolved once: whether the sink can take a batch is a property of the
        # sink's type, and asking per batch would put a `getattr` on the delivery
        # path for an answer that cannot change.
        self._emit_batch: Optional[Callable[[List[UsageRecord]], Any]] = (
            sink.emit_batch if isinstance(sink, BatchSink) else None
        )

        self._condition = threading.Condition()
        self._buffer: List[UsageRecord] = []
        self._in_flight = 0
        self._flush_waiters = 0
        self._stopping = False
        self._closed = False
        self._worker_alive = True

        self._accepted = 0
        self._delivered = 0
        self._failed = 0
        self._dropped_buffer_full = 0
        self._dropped_not_a_record = 0
        self._dropped_closed = 0

        self._warner = RateLimitedWarner(_logger, interval=warn_interval)

        self._worker = threading.Thread(target=self._run, name=name, daemon=True)
        # Daemon: metering must never be the reason a process will not exit
        # (FR-012). The cost — records buffered at a hard exit are lost — is paid
        # back by `close()` and the `atexit` hook above.
        self._worker.start()
        _LIVE.add(self)

    # --- the Sink interface ------------------------------------------------

    def emit(self, record: UsageRecord) -> None:
        """Buffer a record for delivery and return. Never blocks, never raises.

        Returns ``None`` rather than a success flag because that is what
        :class:`~tokenweir.sink.Sink` declares, and this client has to *be* a
        ``Sink`` to compose with :func:`~tokenweir.sink.emit_usage`. The signal a
        caller can act on is :meth:`stats`, which is both richer than a boolean
        and readable without instrumenting every call site.

        ``BaseException`` still propagates, as everywhere else in the emit path: a
        metering guard that swallowed ``KeyboardInterrupt`` would be a worse bug
        than the one it fixes.
        """
        try:
            self._offer(record)
        except Exception:  # pragma: no cover - _offer is already total
            # Belt and braces. `_offer` handles its own failures; if it somehow
            # does not, this is the last thing standing between a metering bug and
            # the request being metered.
            pass

    def emit_batch(self, records: Iterable[UsageRecord]) -> None:
        """Buffer several records. Never blocks, never raises.

        Present so a client can front another client (or any batch-capable
        consumer) without the batch being taken apart and put back together.
        """
        try:
            for record in records:
                self._offer(record)
        except Exception:  # pragma: no cover
            pass

    def close(self, timeout: Optional[float] = None) -> None:
        """Stop the worker, flush what is buffered, and close the sink.

        Safe to call more than once, per the :class:`~tokenweir.sink.Sink`
        protocol, and safe to call from a thread that is emitting. Never raises —
        including when the sink's own ``close`` raises, which is common enough
        during a shutdown against a broker that has already gone away.

        The final flush is **bounded** by ``timeout`` (defaulting to the
        ``close_timeout`` given at construction). A sink wedged against a dead
        broker must not be able to hang a process shutdown, so the wait ends and
        the remaining records are lost — logged, and visible as ``buffered`` in a
        final :meth:`stats`. If the worker is still delivering when that happens,
        the sink is closed underneath it; the resulting failure is guarded and
        counted like any other.

        Args:
            timeout: seconds to wait for the buffer to drain. ``0`` declines to
                wait.
        """
        wait_for = self._close_timeout if timeout is None else max(0.0, float(timeout))

        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._stopping = True
            self._condition.notify_all()

        _LIVE.discard(self)

        worker = self._worker
        if worker.is_alive() and worker is not threading.current_thread():
            worker.join(wait_for)
            if worker.is_alive():
                self._warner.warn(_FLUSH_TIMED_OUT, exc_info=False)

        try:
            close = getattr(self._sink, "close", None)
            if callable(close):
                close()
        except Exception:
            self._warner.warn(_SINK_CLOSE_FAILED)

    # --- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "BufferedEmitter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @property
    def closed(self) -> bool:
        """Whether :meth:`close` has been called."""
        with self._condition:
            return self._closed

    def flush(self, timeout: Optional[float] = None) -> bool:
        """Block until the buffer is drained and nothing is in flight.

        Cuts short the worker's ``linger`` wait, so it does not pay the batching
        delay it exists to avoid on a busy path.

        This is the synchronization point a caller (or a test) should use instead
        of sleeping — "drained" is a state the client can report exactly, and a
        sleep is a guess that is either too slow or flaky.

        Args:
            timeout: seconds to wait. ``None`` waits indefinitely, which is only
                safe when the sink is known to make progress.

        Returns:
            ``True`` if everything was drained, ``False`` if the timeout expired
            first (or the worker is gone and records remain).
        """
        def drained() -> bool:
            # `not self._worker_alive` is a release valve, not a success
            # condition: without it a caller that flushes after the worker has
            # gone would wait out the whole timeout for a drain that can no
            # longer happen. The return value below still reports the truth.
            if not self._worker_alive:
                return True
            return not self._buffer and self._in_flight == 0

        with self._condition:
            self._flush_waiters += 1
            self._condition.notify_all()
            try:
                self._condition.wait_for(drained, timeout)
            finally:
                self._flush_waiters -= 1
            return not self._buffer and self._in_flight == 0

    def stats(self) -> EmitterStats:
        """A consistent snapshot of the counters. Safe to call at any time."""
        with self._condition:
            return EmitterStats(
                accepted=self._accepted,
                delivered=self._delivered,
                failed=self._failed,
                dropped_buffer_full=self._dropped_buffer_full,
                dropped_not_a_record=self._dropped_not_a_record,
                dropped_closed=self._dropped_closed,
                buffered=len(self._buffer),
            )

    # --- internals ---------------------------------------------------------

    def _offer(self, record: object) -> bool:
        """Take a record into the buffer. Total: every path returns, none raises."""
        if not isinstance(record, UsageRecord):
            # Refused before it can reach a sink, for the reason `emit_record`
            # refuses it: `build_record` returns `None` on a construction drop, and
            # the naive composition would otherwise hand that `None` to a
            # conforming sink, which by contract cannot raise and would dutifully
            # deliver it. The guard turns crashes into drops; it must not turn them
            # into garbage on the wire.
            with self._condition:
                self._dropped_not_a_record += 1
            self._warner.warn(_NOT_A_RECORD, exc_info=False)
            return False

        with self._condition:
            if self._closed:
                self._dropped_closed += 1
                warning = _EMITTER_CLOSED
            elif len(self._buffer) >= self._max_buffer:
                self._dropped_buffer_full += 1
                warning = _BUFFER_FULL
            else:
                self._buffer.append(record)
                self._accepted += 1
                self._condition.notify()
                return True
        # Logged outside the lock: a logging handler is arbitrary
        # application code and may be slow, and holding the buffer's lock across
        # it would let a log sink add latency to every other emitting thread.
        self._warner.warn(warning, exc_info=False)
        return False

    def _run(self) -> None:
        """The worker. Drains and delivers until closed and empty."""
        try:
            while True:
                batch = self._next_batch()
                if batch is None:
                    return
                if batch:
                    self._deliver(batch)
                with self._condition:
                    self._in_flight = 0
                    self._condition.notify_all()
        except BaseException:
            # The loop's own failure, not a delivery failure — delivery is guarded
            # in `_deliver`. Recording it is what stops `flush` and `close` from
            # waiting on a thread that is never coming back.
            self._warner.warn(_WORKER_DIED)
            raise
        finally:
            with self._condition:
                self._worker_alive = False
                self._in_flight = 0
                self._condition.notify_all()

    def _next_batch(self) -> Optional[List[UsageRecord]]:
        """Wait for work and take up to ``batch_size`` records.

        Returns ``None`` when the client is closed and the buffer is empty — the
        one exit condition, which is what makes ``close()`` a real flush rather
        than an abandonment.
        """
        with self._condition:
            while not self._buffer and not self._stopping:
                self._condition.wait()
            if not self._buffer:
                return None

            # Linger: let a partial batch fill, so a busy caller pays one delivery
            # per batch rather than one per record. Skipped entirely when someone
            # is waiting on a flush or the client is closing — batching must never
            # be the reason a shutdown is slow.
            if self._linger > 0 and len(self._buffer) < self._batch_size:
                deadline = time.monotonic() + self._linger
                while (
                    len(self._buffer) < self._batch_size
                    and not self._stopping
                    and not self._flush_waiters
                ):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._condition.wait(remaining)

            size = min(len(self._buffer), self._batch_size)
            batch = self._buffer[:size]
            del self._buffer[:size]
            self._in_flight = size
            return batch

    def _deliver(self, batch: List[UsageRecord]) -> None:
        """Hand a batch to the sink. Guarded; a failure never stops the worker.

        The sink is not trusted to honour "never raise from emit" — the party
        harmed by a non-conforming adapter is the deployment that installed it,
        and it should not have to depend on every adapter in the ecosystem being
        correct. That is the same reasoning
        :func:`~tokenweir.sink.emit_record` gives.
        """
        if self._emit_batch is not None:
            try:
                self._emit_batch(batch)
            except Exception:
                self._count_failed(len(batch))
                return
            self._count_delivered(len(batch))
            return

        for record in batch:
            try:
                self._sink.emit(record)
            except Exception:
                self._count_failed(1)
            else:
                self._count_delivered(1)

    def _count_delivered(self, n: int) -> None:
        with self._condition:
            self._delivered += n

    def _count_failed(self, n: int) -> None:
        with self._condition:
            self._failed += n
        self._warner.warn(_DELIVERY_FAILED)


def _positive_int(name: str, value: Any) -> int:
    """Require an ``int`` >= 1, rejecting ``bool`` as :mod:`tokenweir.contract` does."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"{name} must be an integer; got {type(value).__name__} {value!r}"
        )
    if value < 1:
        raise ValueError(f"{name} must be at least 1; got {value}")
    return value


def _non_negative_float(name: str, value: Any) -> float:
    """Require a finite, non-negative number of seconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"{name} must be a number of seconds; got {type(value).__name__} {value!r}"
        )
    value = float(value)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"{name} must be a finite number of seconds; got {value!r}")
    if value < 0:
        raise ValueError(f"{name} must not be negative; got {value}")
    return value
