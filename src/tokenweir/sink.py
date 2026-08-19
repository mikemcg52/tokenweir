"""Sink — the emit side of the pipeline.

A ``Sink`` is what instrumented code (the gateway, the MADO cloud-edge, an OSS
caller) hands a :class:`~tokenweir.contract.UsageRecord` to. By contract emission
is **fire-and-forget and off the critical path** (ADR-0001 Pillar 2): ``emit``
must buffer/return immediately and must never raise into the caller — a metering
outage can never affect the availability of the thing being metered.

Transport-specific sinks (AMQP, HTTP, direct-to-store) are adapters shipped as
optional extras; the core only defines the interface plus a no-op default.

Guarded metering
----------------

ADR-0001 Pillar 2 promises that metering cannot affect the availability of the
metered system, and the ``Sink.emit`` contract is where that promise is
discharged. But **record construction is not** ``Sink.emit``. Since TOKWEIR-4 a
``UsageRecord`` validates at construction and raises on a blank identity field,
a negative count, a wrong-typed optional or an unrecognized ``pricing_mode`` —
and a producer building a record inline on a request path would take that
exception into the request it is metering. That is the gap TOKWEIR-15 closes.

:func:`build_record`, :func:`emit_record` and :func:`emit_usage` are the guarded
seam: they turn any producer-side failure into a **dropped record plus a logged
warning**, so a malformed record degrades to "no metering for this call" rather
than failing the call. They live here, on the emit side, because the guard exists
for the benefit of the emit path — a caller off the request path should keep
using ``UsageRecord(...)`` directly, where raising is the correct behaviour.

Two things this does **not** do:

- It does not soften the contract. :class:`~tokenweir.contract.UsageRecord`
  still validates and still raises; an unattributable record is still a
  producer-side bug. The guard changes who absorbs the bug, not whether it is one.
- It does not relax the ``Sink`` protocol. Implementations still MUST NOT raise
  from ``emit``. :func:`emit_record` guards emission because the party harmed by
  a *non-conforming* adapter is the request on the critical path, and that
  request should not depend on every adapter in the ecosystem being correct.

Drops are never silent: each one logs a ``WARNING`` on this module's logger,
carrying the original exception where one was caught (refusing a non-record is a
rejection rather than a caught failure, so it has none), and the return value
distinguishes a drop from a success so a caller can count drops without parsing
logs. A guard that hid
producer bugs would undo TOKWEIR-4's decision rather than protect it.

Where that signal *goes* is the application's decision, not this library's: the
package attaches a ``NullHandler`` to the ``tokenweir`` logger (see
``tokenweir/__init__.py``) so an application that has configured no logging is not
given a traceback per metered request on its stderr by ``logging.lastResort``.
The return value remains the signal that always reaches the caller regardless of
logging configuration.

Batch delivery, and the broker-less path
----------------------------------------

TOKWEIR-6 adds two things here, neither of which changes :class:`Sink`.

:class:`BatchSink` is an **optional** capability a sink may additionally offer:
``emit_batch`` for the case where delivering a batch is cheaper, or more atomic,
than delivering the records one at a time. It is a separate protocol rather than a
method on :class:`Sink` because the majority of adapters have nothing to gain from
it — an AMQP publish is per-message however you slice it — and growing the
interface every adapter must satisfy in order to serve the minority that benefits
is how a small protocol stops being one. :class:`~tokenweir.emitter.BufferedEmitter`
prefers ``emit_batch`` when a sink has it and falls back to per-record ``emit``
when it does not, so an existing sink keeps working untouched.

:class:`DirectSink` is the **broker-less** path ADR-0001 Pillar 2 names as the
consequence of a transport-agnostic core — *"the EKS cloud-edge can drop the
broker entirely and write direct/in-process"*. It adapts a
:class:`~tokenweir.source.Source` to the :class:`Sink` interface, and it is
batch-capable precisely so that a batch stays one ``Source.write`` call: that is
the one-transaction-per-batch guarantee :class:`~tokenweir.postgres.PostgresSource`
documents, and delivering record-by-record would quietly throw it away in favour of
a transaction per row.

Note the asymmetry it has to absorb. ``Source.write`` *may* raise — deliberately,
because the write side is off the critical path and a caller that can retry needs
to know it must. ``Sink.emit`` may **not**. :class:`DirectSink` sits exactly on that
seam, so it is the place where a store failure stops being an exception and becomes
a counted, logged drop.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from tokenweir._ratelimit import RateLimitedWarner
from tokenweir.contract import UsageRecord
from tokenweir.source import Source

_logger = logging.getLogger(__name__)

_CONSTRUCTION_FAILED = (
    "tokenweir: dropping usage record — construction failed; no metering for this call"
)
# "the sink failed" rather than "the sink raised": the same guard catches a sink
# that is missing or misconfigured (an AttributeError from ``None.emit``), and
# claiming it raised would misdescribe that case. ``exc_info`` carries the real
# cause either way. Still trivially distinguishable from a construction drop,
# which is what FR-007 asks for.
_EMISSION_FAILED = (
    "tokenweir: usage record not emitted — the sink failed; no metering for this call"
)
_NOT_A_RECORD = (
    "tokenweir: refusing to emit a non-UsageRecord; no metering for this call"
)
_SINK_CLOSED = (
    "tokenweir: refusing to emit through a closed sink; no metering for these calls"
)
_DIRECT_WRITE_FAILED = (
    "tokenweir: usage records not written — the store failed; no metering for these calls"
)


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


@runtime_checkable
class BatchSink(Protocol):
    """Optional extension of :class:`Sink` for a sink that can take a whole batch.

    A sink that implements this is still a :class:`Sink` and is still bound by every
    part of that contract — in particular ``emit_batch`` MUST NOT raise, for the same
    reason ``emit`` may not.

    Implement it only when a batch is genuinely better than a loop: fewer round trips,
    or — the case this exists for — one transaction instead of *n*. Where it buys
    nothing, leave it off and get the per-record fallback, which is what
    :class:`~tokenweir.emitter.BufferedEmitter` does when the capability is absent.
    :class:`~tokenweir.amqp.AMQPSink` deliberately does not implement it: AMQP has no
    batch publish, so an ``emit_batch`` there would be a loop wearing a costume.

    ``runtime_checkable`` gives ``isinstance`` a structural check for the method's
    presence, which is the question the emitter actually asks once, at construction.
    """

    def emit_batch(self, records: Iterable[UsageRecord]) -> None:
        """Accept a batch for delivery. Fire-and-forget; never raises.

        The batch is a materialized sequence, not a lazily-consumed iterator, so an
        implementation may traverse it more than once.
        """
        ...


class NullSink:
    """A Sink that drops everything. The safe default and a test double."""

    def emit(self, record: UsageRecord) -> None:  # noqa: D102
        return None

    def close(self) -> None:  # noqa: D102
        return None


class DirectSink:
    """A :class:`Sink` that writes straight to a :class:`~tokenweir.source.Source`.

    The broker-less path (ADR-0001 Pillar 2). A deployment with no RabbitMQ — the EKS
    cloud-edge — points the emitter at one of these over a
    :class:`~tokenweir.postgres.PostgresSource` and gets the same emit-side guarantees
    with no transport in the picture::

        with BufferedEmitter(DirectSink(PostgresSource(conn))) as emitter:
            emit_usage(emitter, fields)

    **It is batch-capable on purpose.** ``PostgresSource.write`` puts a whole batch in
    one transaction, and that guarantee is only worth having if a batch actually
    arrives as a batch — so :meth:`emit_batch` is one ``write`` call, not *n*.
    :meth:`emit` is the degenerate case of a batch of one, and a caller that emits
    record-by-record through this sink genuinely does get a transaction per record;
    putting a :class:`~tokenweir.emitter.BufferedEmitter` in front is what turns that
    into batches, and is the supported way to use this.

    **It converts store failures into drops**, because it is a ``Sink`` and
    ``Sink.emit`` must not raise. That is the opposite of what
    ``Source.write`` does, and both are right: the ``Source`` contract faces a
    consumer that can retry, and this one faces a request that cannot be failed.
    The consequence is worth being plain about — records that reach *this* sink and
    cannot be stored are **gone**, not queued for retry. A deployment that needs
    durability across a store outage wants the broker path, which is what a broker is
    for.

    Failures are counted (:attr:`written`, :attr:`dropped`) and logged, rate-limited so
    an unreachable store does not produce one line per metered call.

    Ownership follows :class:`~tokenweir.postgres.PostgresSource`'s rule: a source
    passed in stays the caller's and :meth:`close` leaves it open; pass
    ``owns_source=True`` to hand it over.
    """

    def __init__(
        self,
        source: Source,
        *,
        owns_source: bool = False,
        warn_interval: float = 60.0,
    ) -> None:
        self._source = source
        self._owns_source = owns_source
        self._closed = False
        self._lock = threading.Lock()
        self._written = 0
        self._dropped = 0
        self._warner = RateLimitedWarner(_logger, interval=warn_interval)

    @property
    def source(self) -> Source:
        """The underlying source, for a caller that must reach past this."""
        return self._source

    @property
    def written(self) -> int:
        """Records this sink has handed to the store successfully."""
        with self._lock:
            return self._written

    @property
    def dropped(self) -> int:
        """Records lost — refused, unstorable, or offered after :meth:`close`."""
        with self._lock:
            return self._dropped

    def emit(self, record: UsageRecord) -> None:
        """Write one record. Never raises."""
        self.emit_batch((record,))

    def emit_batch(self, records: Iterable[UsageRecord]) -> None:
        """Write a batch in a single :meth:`~tokenweir.source.Source.write` call.

        Never raises. Two different rules apply to two different kinds of bad input,
        and the difference is deliberate:

        - A value that is **not a** :class:`~tokenweir.contract.UsageRecord` is
          filtered out and counted, and the rest of the batch is written. It could
          never have been stored, so dropping it costs the good records nothing.
        - A record the **store** cannot take loses the batch **whole**. That is
          ``rows_for``'s documented behaviour and the reason it validates before
          opening a transaction: a partially written batch is worse than a wholly
          dropped one for a consumer that acks on the call returning.
        """
        try:
            offered = list(records)
        except Exception:
            # A generator that raises while being drained. Nothing was written and
            # there is no count to attribute it to, so it is one drop event.
            self._warner.warn(_DIRECT_WRITE_FAILED)
            return
        # Non-records are refused here rather than handed to the store, matching
        # what `AMQPSink` does at the same seam. Both adapters sit downstream of
        # `build_record`, which returns `None` on a construction drop, and a
        # `Source` is under no obligation to notice: `MemorySource` would store the
        # `None`, and `PostgresSource.rows_for` would raise — losing the whole
        # batch around it rather than the one bad value.
        #
        # Filtered rather than refusing the batch whole, which is the one place
        # this deliberately differs from `rows_for`. That rule exists so a batch is
        # never *half* written; dropping a value that could never have been written
        # at all costs the good records nothing, and salvaging them is strictly
        # better than losing them to a producer's `None`.
        batch = [record for record in offered if isinstance(record, UsageRecord)]
        refused = len(offered) - len(batch)
        if refused:
            with self._lock:
                self._dropped += refused
            self._warner.warn(_NOT_A_RECORD, exc_info=False)
        if not batch:
            return
        # The flag is read under the same lock that guards the counters. It was
        # read outside it at first, which left a window where a concurrent
        # `close()` landed between the check and the `write` — harmless, because
        # the failure is caught and counted, but an asymmetry that reads as an
        # oversight rather than a decision, and one that would stop being harmless
        # the moment anything here stopped being guarded.
        with self._lock:
            if self._closed:
                self._dropped += len(batch)
                closed = True
            else:
                closed = False
        if closed:
            self._warner.warn(_SINK_CLOSED, exc_info=False)
            return
        try:
            self._source.write(batch)
        except Exception:
            with self._lock:
                self._dropped += len(batch)
            self._warner.warn(_DIRECT_WRITE_FAILED)
            return
        with self._lock:
            self._written += len(batch)

    def close(self) -> None:
        """Release resources. Safe to call more than once.

        A no-op for a borrowed source, exactly as
        :meth:`~tokenweir.postgres.PostgresSource.close` is for a borrowed connection —
        closing something handed to us would surprise whoever else holds it. The closed
        flag is set before the source is closed, so a ``close`` that raises is not
        retried into a double-close by a caller who calls again.
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_source:
            self._source.close()



def _warn_dropped(message: str, *, exc_info: bool = True) -> None:
    """Log a drop at ``WARNING`` with the active exception, and never raise.

    The nested ``try`` is deliberate, not sloppiness. An application may install a
    handler, a filter, or a value whose ``__repr__`` raises, and "this call never
    raises" has to survive a hostile logging configuration — otherwise the guard
    has merely moved the throw site from the contract onto the logger. Only the
    logging call is inside it, so it cannot mask a failure of the guarded
    operation itself.

    No field value is interpolated into the message: ``exc_info`` already carries
    the contract's own error text, which names the offending field, and formatting
    an arbitrary caller-supplied value is the cheapest way to make the logging
    call throw in the first place.

    ``exc_info`` is a parameter because one drop — refusing a non-record — is a
    rejection rather than a caught failure, and there is no active exception to
    attach. Logging ``exc_info=True`` there would render a misleading
    ``NoneType: None``.
    """
    try:
        _logger.warning(message, exc_info=exc_info)
    except Exception:
        pass


def build_record(
    fields: Optional[Mapping[str, Any]] = None, /, **overrides: Any
) -> Optional[UsageRecord]:
    """Construct a :class:`~tokenweir.contract.UsageRecord`, or ``None`` if invalid.

    The guarded counterpart to calling ``UsageRecord(**fields)`` directly. Use it
    where a raised exception would reach a request being metered; off that path,
    construct directly and let a producer bug be loud.

    Fields may be given as keywords, as a mapping, or as both — a mapping with
    keyword ``overrides`` applied on top, which is how a caller stamps a value it
    only learns after the metered call returns::

        build_record(base_fields, latency_ms=elapsed_ms, ts=stamp)

    Accepting the mapping *itself* rather than only ``**fields`` is not a
    convenience. ``**`` unpacking happens in the **caller's** frame, before this
    function is entered, so ``build_record(**mapping)`` raises
    ``TypeError: keywords must be strings`` into the metered request if the
    mapping came from JSON, a header dict, or any generic code that could put a
    non-string key in it. Passed as a mapping, that unpacking happens inside the
    guard and becomes an ordinary drop.

    ``fields`` is positional-only for the reason given on :func:`emit_usage`: a
    producer whose record has a field named ``fields`` must not collide with this
    parameter during argument binding, where no guard can reach.

    Both of the contract's error types are absorbed — ``ValueError`` for an
    invalid value and ``TypeError`` for a missing or unknown keyword argument.
    ``BaseException`` (``KeyboardInterrupt``, ``SystemExit``) is **not** caught:
    swallowing those would be a worse bug than the one this guards against.

    For valid input the result is exactly what direct construction produces — the
    guard adds no normalization, defaulting or coercion of its own.

    Returns:
        The record, or ``None`` if construction failed (a ``UsageRecord`` is a
        frozen dataclass and never falsy, but callers should test ``is None``).
    """
    try:
        if fields is not None and not isinstance(fields, Mapping):
            # `dict()` duck-types any iterable of pairs, which would quietly make
            # the annotation a lie and silently consume a generator. A mapping is
            # what the signature promises, so anything else is a producer bug and
            # takes the ordinary drop path.
            raise TypeError(
                f"fields must be a mapping; got {type(fields).__name__}"
            )
        merged = dict(fields) if fields is not None else {}
        merged.update(overrides)
        return UsageRecord(**merged)
    except Exception:
        _warn_dropped(_CONSTRUCTION_FAILED)
        return None


def emit_record(sink: Sink, record: object, /) -> bool:
    """Emit ``record`` to ``sink``; return ``False`` if it was refused or the sink raised.

    Both parameters are positional-only, as on the other guarded calls (FR-019).
    No producer data can bind to them here — this function takes no ``**overrides``
    — but keeping one rule for the whole seam is what stops the next parameter
    added to it from quietly reopening the collision hole.

    ``record`` is annotated ``object`` rather than ``UsageRecord`` deliberately.
    FR-018 exists so that ``emit_record(sink, build_record(**fields))`` is safe
    without an intervening ``is not None`` check — but ``build_record`` returns
    ``Optional[UsageRecord]``, so a narrower annotation would make the very
    composition this function is designed to absorb a static type error for an
    adopter running mypy or pyright. The runtime check below is the contract.

    ``Sink.emit`` MUST NOT raise — that contract is unchanged. This guards against
    an implementation that violates it, because the party harmed by a
    non-conforming adapter is the request on the critical path.

    Anything that is not a :class:`~tokenweir.contract.UsageRecord` is refused
    before the sink sees it, and refusing is the whole point: this function is
    half of the build-then-emit pair, and :func:`build_record` returns ``None`` on
    a drop, so the naive composition would otherwise hand ``None`` to a conforming
    sink — which by contract cannot raise and would dutifully persist it. The
    guard turns crashes into drops; it must not turn them into garbage in the
    store.

    ``BaseException`` propagates, as in :func:`build_record`.

    Returns:
        ``True`` if ``emit`` returned normally, ``False`` if the record was
        refused or ``emit`` raised.
    """
    if not isinstance(record, UsageRecord):
        _warn_dropped(_NOT_A_RECORD, exc_info=False)
        return False
    try:
        sink.emit(record)
    except Exception:
        _warn_dropped(_EMISSION_FAILED)
        return False
    return True


def emit_usage(
    sink: Sink, fields: Optional[Mapping[str, Any]] = None, /, **overrides: Any
) -> Optional[UsageRecord]:
    """Build a record from the given fields and emit it to ``sink``.

    Never raises, except for ``BaseException`` from the record's own construction
    or from the sink — see :func:`build_record`.

    The one call a metered request path makes. It is exactly
    :func:`build_record` followed by :func:`emit_record`, so there is one
    implementation of each guarantee rather than two — a caller that must hold a
    record between the two steps (a batching emitter builds now and emits later)
    uses the halves directly and gets the same guarantee. A caller that only needs
    to *stamp* a value it learns late does not need the halves at all::

        emit_usage(sink, base_fields, latency_ms=elapsed_ms, ts=stamp)

    Field values may be given as keywords, as a mapping, or as both; see
    :func:`build_record` for why passing the mapping itself matters.

    Both ``sink`` and ``fields`` are positional-only, and that is load-bearing
    rather than stylistic. Argument binding happens *before* the function body, so
    a ``**overrides`` mapping carrying the key ``"sink"`` or ``"fields"`` would
    otherwise collide with a parameter and raise ``TypeError`` outside every
    guard — a route by which a producer's own data reaches the metered request
    that no ``try`` inside this function could close. Positional-only takes those
    names out of the keyword namespace, so each becomes an ordinary unknown field:
    dropped and logged like any other.

    Returns:
        The record if it was built and emitted, otherwise ``None``. ``None`` means
        either a construction drop or an emission failure; the two are
        distinguishable in the logs, and a caller that needs to tell them apart in
        code should use the two halves.
    """
    record = build_record(fields, **overrides)
    if record is None:
        return None
    if not emit_record(sink, record):
        return None
    return record
