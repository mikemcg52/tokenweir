"""The AMQP adapter — the homelab's transport, shipped as an optional extra.

ADR-0001 Pillar 2 keeps the wire out of the core and puts it here: *"Transport
lives in swappable adapters shipped as optional extras (e.g. ``tokenweir[amqp]``)
so the core stays dependency-light (no ``pika`` compiled in) … Consequence:
homelab keeps RabbitMQ (gateway → AMQP → writer, as today)."* This module is that
adapter. The broker-less half of the same sentence is
:class:`~tokenweir.sink.DirectSink`.

**No driver at import time.** Nothing here imports ``pika`` when the module is
imported; the import is deferred into :meth:`AMQPSink.from_url`, the one place
that opens a connection itself, and into the one call that builds pika's message
properties. That is the same rule — and the same reasoning —
:mod:`tokenweir.migrations` follows for psycopg: a bare ``pip install tokenweir``
must carry no transport library, and the record-to-message mapping below has to be
testable where no AMQP library exists at all, which is the environment this
project's automated suite actually runs in.

**Publishing is deliberately plain.** ``basic_publish`` is a blocking call and
this class makes no attempt to hide that, because hiding it is
:class:`~tokenweir.emitter.BufferedEmitter`'s job. Putting the buffering in the
client rather than in every adapter is what lets an adapter be this simple and
still be safe on a metered request path — so the supported way to use this is::

    from tokenweir import BufferedEmitter, emit_usage
    from tokenweir.amqp import AMQPSink

    with BufferedEmitter(AMQPSink.from_url("amqp://guest:guest@rabbit/")) as emitter:
        emit_usage(emitter, fields)

Using an ``AMQPSink`` bare, straight from a request path, satisfies "never raises"
and violates "off the critical path". It is supported — a consumer or a batch job
off that path may well want it — but it is not the metered-path shape.

**No batch capability, on purpose.** AMQP has no batch publish; an ``emit_batch``
here would be a loop wearing a costume. Leaving :class:`~tokenweir.sink.BatchSink`
unimplemented gets the emitter's per-record fallback, which is the same loop
without the pretence that something atomic is happening.

**Declaring the topology is not this adapter's job.** Exchanges, queues, bindings
and their durability are deployment state with a lifecycle longer than any
process, and a library that declared them on connect would silently own them —
and fail confusingly the day its arguments disagreed with what is deployed. This
publishes to what exists.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional, Tuple

from tokenweir._ratelimit import RateLimitedWarner
from tokenweir.contract import UsageRecord

__all__ = [
    "CONTENT_TYPE",
    "DEFAULT_EXCHANGE",
    "DEFAULT_ROUTING_KEY",
    "PERSISTENT_DELIVERY_MODE",
    "AMQPSink",
    "message_for",
    "publish_properties",
]

_logger = logging.getLogger(__name__)

#: The default exchange. The empty string is AMQP's own default direct exchange,
#: which routes to the queue named by the routing key — the one topology that
#: needs no declaration to work, so a caller who has created a queue and nothing
#: else can publish immediately.
DEFAULT_EXCHANGE = ""

#: Where records go when the caller says nothing. Named for the payload rather
#: than for any consumer, because the consumer is a deployment's choice.
DEFAULT_ROUTING_KEY = "tokenweir.usage"

#: The wire form is JSON — that part is the contract (see
#: :mod:`tokenweir.contract`); AMQP is not.
CONTENT_TYPE = "application/json"

#: AMQP's ``delivery_mode`` for a persistent message. Metering that evaporates on
#: a broker restart would make the broker path pointless: the reason to accept a
#: broker at all is that it survives a consumer outage.
PERSISTENT_DELIVERY_MODE = 2

_PUBLISH_FAILED = (
    "tokenweir: usage record not published — the AMQP channel failed; no metering "
    "for this call"
)
_RECONNECT_FAILED = (
    "tokenweir: could not re-establish the AMQP connection; records will be dropped "
    "until the broker is reachable"
)
_CLOSE_FAILED = "tokenweir: the AMQP connection raised while closing; releasing it anyway"
_NOT_A_RECORD = (
    "tokenweir: refusing to publish a non-UsageRecord; no metering for this call"
)
_SINK_CLOSED = (
    "tokenweir: refusing to publish through a closed AMQP sink; no metering for this call"
)


def _import_pika() -> Any:
    """Import ``pika``, or raise an :class:`ImportError` that names the extra.

    Deferred rather than module-level so that importing this module — and using
    :func:`message_for` — costs nothing and needs nothing. A bare
    ``ModuleNotFoundError: pika`` would leave a caller to work out both what to
    install and whether they even need it.
    """
    try:
        import pika
    except ImportError as exc:
        raise ImportError(
            "tokenweir needs pika to speak AMQP; install it with "
            "`pip install 'tokenweir[amqp]'`, or pass an existing channel to "
            "AMQPSink instead — nothing else in tokenweir requires an AMQP library"
        ) from exc
    return pika


def message_for(record: UsageRecord) -> bytes:
    """Map one record to its message body: the record's JSON wire form, UTF-8.

    Pure: no broker, no driver, no clock, no connection. That is what makes it
    testable in an environment with none of them — the same property, and the same
    motivation, as :func:`tokenweir.postgres.row_for`.

    The encoding is compact and key-sorted. Compact because a metering payload is
    paid for on every metered call, and sorted so the bytes for a given record are
    identical from run to run, which is what lets a test assert on them and a
    reader diff two captured messages. Neither is part of the record contract:
    :meth:`~tokenweir.contract.UsageRecord.from_json` is indifferent to key order.

    Raises:
        TypeError: ``record`` is not a :class:`~tokenweir.contract.UsageRecord`.
            Checked rather than duck-typed for the reason ``row_for`` checks it:
            the guarded emit seam returns ``None`` on a construction drop, and a
            ``None`` reaching a broker as the four bytes ``null`` would be exactly
            the "crashes into garbage" outcome that seam exists to prevent.
    """
    if not isinstance(record, UsageRecord):
        raise TypeError(f"expected a UsageRecord; got {type(record).__name__}")
    return record.to_json(separators=(",", ":"), sort_keys=True).encode("utf-8")


def publish_properties() -> dict:
    """The message properties every record is published with, as plain kwargs.

    Separated from the pika object built out of them so that *what* the adapter
    asks for is assertable without an AMQP library installed, while the object
    itself stays pika's to construct.
    """
    return {
        "content_type": CONTENT_TYPE,
        "delivery_mode": PERSISTENT_DELIVERY_MODE,
    }


class AMQPSink:
    """A :class:`~tokenweir.sink.Sink` that publishes records to an AMQP broker.

    Takes a **channel**, not a URL: connection parameters, TLS, heartbeats,
    credentials rotation and connection pooling are the deployment's business, and
    taking a channel is what keeps this module importable with no ``pika``
    installed. :meth:`from_url` is the convenience path for a caller that wants
    none of that.

    Ownership follows :class:`~tokenweir.postgres.PostgresSource`'s rule exactly: a
    channel passed in stays the caller's and :meth:`close` leaves it open; one
    opened by :meth:`from_url` is closed by :meth:`close`.

    **Not thread-safe**, and deliberately not made so. An AMQP channel is not safe
    to share between threads — that is pika's constraint, not one this class could
    lift, and a lock here would protect only the counters while leaving the actual
    hazard in place, which is worse than saying so. Give each thread its own sink,
    or put a single :class:`~tokenweir.emitter.BufferedEmitter` in front: its worker
    is one thread, which is the shape this adapter is built for.

    Ownership also decides **reconnection**. A connection this sink opened, it may
    re-open: on a publish failure it drops the connection and re-establishes on the
    *next* attempt — not by retrying inside the current one, which would put an
    unbounded wait on whatever thread is delivering. A channel that was handed in
    is not reconnected, because this object does not know how it was made and
    guessing would replace the caller's TLS context, credentials or pooling with
    its own; a caller who supplies a channel owns its liveness.

    Args:
        channel: anything with ``basic_publish``. Not type-checked — a pika
            channel, a wrapper, or a test double are all legitimate.
        exchange: defaults to AMQP's default direct exchange.
        routing_key: defaults to :data:`DEFAULT_ROUTING_KEY`.
        connection: the connection behind ``channel``, if this sink should be able
            to close it. Set by :meth:`from_url`.
        owns_connection: whether :meth:`close` should close ``connection``.
        warn_interval: seconds between warning lines for a repeating failure, so an
            unreachable broker cannot produce one log line per metered call.
    """

    def __init__(
        self,
        channel: Any,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        routing_key: str = DEFAULT_ROUTING_KEY,
        connection: Any = None,
        owns_connection: bool = False,
        warn_interval: float = 60.0,
    ) -> None:
        self._channel = channel
        self._connection = connection
        self._owns_connection = owns_connection
        self.exchange = exchange
        self.routing_key = routing_key
        self._closed = False
        self._published = 0
        self._dropped = 0
        self._properties: Any = None
        self._reconnect: Optional[Callable[[], Tuple[Any, Any]]] = None
        self._warner = RateLimitedWarner(_logger, interval=warn_interval)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        routing_key: str = DEFAULT_ROUTING_KEY,
        warn_interval: float = 60.0,
    ) -> "AMQPSink":
        """Open a pika ``BlockingConnection`` from an AMQP URL and own it.

        Raises here, rather than degrading to drops, because opening a connection
        is wiring-time work: a caller who cannot reach the broker at start-up
        wants to know at start-up, and there is no metered request in scope to
        protect. Once the sink exists, every later failure — including losing this
        connection — is a counted drop.

        Raises:
            ImportError: ``pika`` is not installed, naming the extra.
            Exception: whatever pika raises if the broker is unreachable.
        """

        def connect() -> Tuple[Any, Any]:
            pika = _import_pika()
            connection = pika.BlockingConnection(pika.URLParameters(url))
            return connection, connection.channel()

        connection, channel = connect()
        sink = cls(
            channel,
            exchange=exchange,
            routing_key=routing_key,
            connection=connection,
            owns_connection=True,
            warn_interval=warn_interval,
        )
        sink._reconnect = connect
        return sink

    @property
    def channel(self) -> Any:
        """The underlying channel, for a caller that must reach past this."""
        return self._channel

    @property
    def published(self) -> int:
        """Records handed to the broker without error."""
        return self._published

    @property
    def dropped(self) -> int:
        """Records lost — refused, unpublishable, or emitted after :meth:`close`."""
        return self._dropped

    def emit(self, record: UsageRecord) -> None:
        """Publish one record. Never raises, per the ``Sink`` contract.

        Not batch-capable: see the module docstring. The
        :class:`~tokenweir.emitter.BufferedEmitter` fallback calls this per record,
        which is what an AMQP publish is anyway.
        """
        try:
            body = message_for(record)
        except Exception:
            self._dropped += 1
            self._warner.warn(_NOT_A_RECORD, exc_info=False)
            return

        if self._closed:
            self._dropped += 1
            self._warner.warn(_SINK_CLOSED, exc_info=False)
            return

        channel = self._live_channel()
        if channel is None:
            self._dropped += 1
            return

        try:
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=self.routing_key,
                body=body,
                properties=self._basic_properties(),
            )
        except Exception:
            self._dropped += 1
            self._invalidate()
            self._warner.warn(_PUBLISH_FAILED)
            return
        self._published += 1

    def close(self) -> None:
        """Release resources. Safe to call more than once, and never raises.

        A no-op for a borrowed channel. A connection this sink opened is closed
        here — guarded, because a broker that has already gone away routinely makes
        a close raise, and a shutdown path is the worst place to turn that into an
        exception.
        """
        if self._closed:
            return
        self._closed = True
        self._release()

    # --- internals ---------------------------------------------------------

    def _basic_properties(self) -> Any:
        """pika's ``BasicProperties`` for :func:`publish_properties`, built once.

        Built lazily rather than in ``__init__`` so that constructing a sink over a
        caller-supplied channel needs no ``pika`` import of its own, and cached
        because the value never varies and a metered path should not rebuild it per
        record.
        """
        if self._properties is None:
            self._properties = _import_pika().BasicProperties(**publish_properties())
        return self._properties

    def _live_channel(self) -> Any:
        """The channel to publish on, reconnecting if this sink owns the connection."""
        if self._channel is not None:
            return self._channel
        if self._reconnect is None:
            # A borrowed channel that has been invalidated, or no channel at all.
            # Not ours to re-make; the caller owns its liveness.
            return None
        try:
            self._connection, self._channel = self._reconnect()
        except Exception:
            self._warner.warn(_RECONNECT_FAILED)
            return None
        return self._channel

    def _invalidate(self) -> None:
        """Drop a failed connection so the next publish re-establishes it.

        Only for a connection this sink owns: re-making a caller's channel would
        substitute this module's idea of how to connect for theirs. A borrowed
        channel is left exactly as it was, so a caller who repairs it out of band
        sees publishing resume with no cooperation from here.
        """
        if not self._owns_connection:
            return
        self._release()
        self._channel = None
        self._connection = None

    def _release(self) -> None:
        """Close an owned connection, swallowing whatever it says about it."""
        if not self._owns_connection or self._connection is None:
            return
        try:
            self._connection.close()
        except Exception:
            self._warner.warn(_CLOSE_FAILED)
