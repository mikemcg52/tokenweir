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

Drops are never silent: each one logs a ``WARNING`` on this module's logger
carrying the original exception, and the return value distinguishes a drop from a
success so a caller can count drops without parsing logs. A guard that hid
producer bugs would undo TOKWEIR-4's decision rather than protect it.

Where that signal *goes* is the application's decision, not this library's: the
package attaches a ``NullHandler`` to the ``tokenweir`` logger (see
``tokenweir/__init__.py``) so an application that has configured no logging is not
given a traceback per metered request on its stderr by ``logging.lastResort``.
The return value remains the signal that always reaches the caller regardless of
logging configuration.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Protocol, runtime_checkable

from tokenweir.contract import UsageRecord

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


def build_record(**fields: Any) -> Optional[UsageRecord]:
    """Construct a :class:`~tokenweir.contract.UsageRecord`, or ``None`` if invalid.

    The guarded counterpart to calling ``UsageRecord(**fields)`` directly. Use it
    where a raised exception would reach a request being metered; off that path,
    construct directly and let a producer bug be loud.

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
        return UsageRecord(**fields)
    except Exception:
        _warn_dropped(_CONSTRUCTION_FAILED)
        return None


def emit_record(sink: Sink, record: UsageRecord) -> bool:
    """Emit ``record`` to ``sink``; return ``False`` if the sink raised.

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


def emit_usage(sink: Sink, **fields: Any) -> Optional[UsageRecord]:
    """Build a record from ``fields`` and emit it to ``sink``. Never raises.

    The one call a metered request path makes. It is exactly
    :func:`build_record` followed by :func:`emit_record`, so there is one
    implementation of each guarantee rather than two — a caller that must stamp
    or enrich a record between the two steps (the gateway computes ``latency_ms``
    after the metered call returns; a batching emitter builds now and emits later)
    should use the halves directly and gets the same guarantee.

    ``**fields`` rather than a ready-made record is the point: construction has to
    happen *inside* the guard, or the caller is back to holding the exception.

    Returns:
        The record if it was built and emitted, otherwise ``None``. ``None`` means
        either a construction drop or an emission failure; the two are
        distinguishable in the logs, and a caller that needs to tell them apart in
        code should use the two halves.
    """
    record = build_record(**fields)
    if record is None:
        return None
    if not emit_record(sink, record):
        return None
    return record
