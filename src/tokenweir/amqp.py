"""The AMQP adapter — the homelab's transport, shipped as an optional extra.

ADR-0001 Pillar 2 keeps the wire out of the core and puts it here: *"Transport
lives in swappable adapters shipped as optional extras (e.g. ``tokenweir[amqp]``)
so the core stays dependency-light (no ``pika`` compiled in) … Consequence:
homelab keeps RabbitMQ (gateway → AMQP → writer, as today)."* This module is that
adapter. The broker-less half of the same sentence is
:class:`~tokenweir.sink.DirectSink`.

**No driver at import time.** Nothing here imports ``pika`` when the module is
imported. That is the same rule — and the same reasoning —
:mod:`tokenweir.migrations` follows for psycopg: a bare ``pip install tokenweir``
must carry no transport library, and the record-to-message mapping below has to be
testable where no AMQP library exists at all, which is the environment this
project's automated suite actually runs in.

The import happens when an :class:`AMQPSink` is **constructed** — wiring time,
where raising is safe and correct — and not one moment later. Deferring it further,
to the first publish, is a mistake worth naming because it looks like a virtue: the
publish path may not raise, so a missing driver discovered there is not an error at
all, it is a silent 100% drop of every record, for the life of the process, in the
one code path whose whole job is to never complain. If this module needs ``pika``,
it says so while somebody is still watching.

A caller who wants a sink with no ``pika`` at all — a test with a channel double —
passes ``properties=``, which is the only thing the driver was needed for.

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
import re
import time
from typing import Any, Callable, Optional, Tuple

from tokenweir._ratelimit import RateLimitedWarner
from tokenweir.contract import UsageRecord

__all__ = [
    "CONTENT_TYPE",
    "DEFAULT_EXCHANGE",
    "DEFAULT_ROUTING_KEY",
    "MAX_DIALS_PER_INTERVAL",
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
_NO_CHANNEL = (
    "tokenweir: usage record not published — the AMQP sink has no channel to publish "
    "on and cannot make one; no metering for this call"
)
#: A hard cap on dials per ``reconnect_interval``, independent of every judgement
#: the productivity rule below makes. **This, not the productivity rule, is what
#: makes the churn bound a guarantee**, and the reason is worth stating plainly
#: because it took a review to see it.
#:
#: ``pika``'s ``BlockingChannel.basic_publish`` does not wait for the broker unless
#: publisher confirms are on, which this adapter does not enable: ``_flush_output()``
#: is called with no waiters and returns once the socket write is flushed. So a
#: publish to a **nonexistent exchange returns normally**, and the broker's
#: ``channel.close`` arrives a round trip later, surfacing on a *later* publish.
#: The first success on a fresh channel is therefore unfalsifiable — it is the same
#: call whether the exchange exists or not.
#:
#: That is fatal to a rule built only on "has this connection published?", because a
#: broken exchange hands it a fresh "this one works" every cycle. The obvious repair
#: — demand two publishes before trusting a connection — was tried and rejected: it
#: breaks a TOKWEIR-6 guarantee that a connection which published once and then
#: dropped recovers immediately, which is a legitimate property and not one this
#: story is entitled to take away.
#:
#: So the productivity rule stays as the thing that gets the *common* case right and
#: keeps recovery instant, and this cap is what holds when the rule is fooled.
#: Structural rather than inferential: however many times the rule concludes "this
#: one deserves an immediate re-dial", dialling is capped. Three is small enough to
#: bound churn hard and large enough that a genuine recovery burst — lose, recover,
#: lose again — never reaches it.
MAX_DIALS_PER_INTERVAL = 3

_DIAL_CEILING_REACHED = (
    "tokenweir: usage record not published — the AMQP sink has hit its cap of "
    f"{MAX_DIALS_PER_INTERVAL} reconnects per interval and is waiting; the broker "
    "is accepting connections but records are not getting through. No metering "
    "for these calls"
)
# Three distinct reasons the sink is not publishing, and they must not share a
# message. This module has already litigated the point once (`_NO_CHANNEL` used to
# claim "cannot make one" during a backoff): a drop logged with the wrong cause is
# worse than a bare count, because it sends an operator after the wrong problem.
# The two backoff messages both keep the words "reconnect interval" so a reader —
# and the tests that grep for it — can still recognise the state.
_AWAITING_REDIAL = (
    "tokenweir: usage record not published — waiting out the reconnect interval "
    "after a failed connection attempt; no metering for this call"
)
_AWAITING_PUBLISHABLE = (
    "tokenweir: usage record not published — waiting out the reconnect interval "
    "after two connections in a row failed to publish anything; if this persists, "
    "check that the exchange and routing key exist and are permitted. No metering "
    "for these calls"
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
            "`pip install 'tokenweir[amqp]'`. Only tokenweir.amqp needs it — the "
            "core, the contract and the store do not, so a caller that does not "
            "publish to a broker never has to install it. To build a sink over a "
            "channel of your own with no pika present, pass `properties=` to "
            "AMQPSink; that is the only thing the driver is needed for here"
        ) from exc
    return pika


def _redact_url(text: str, url: Optional[str]) -> str:
    """Mask ``url``'s credentials wherever they appear in ``text``.

    An AMQP URL conventionally carries ``user:password@host``, and a connect
    failure is exactly the moment a library is tempted to echo the thing it could
    not connect to into somebody's log. ``tokenweir.migrations`` already
    established the rule for the equivalent Postgres case — redact before printing
    a connect failure — and this is the same rule for the same reason.

    Two things are masked: the whole ``user:password`` userinfo, and the password
    on its own, since a driver may report the credential without the URL around it.

    A URL whose userinfo has **no colon** is left entirely alone. That mirrors
    ``tokenweir.migrations._carries_a_password``: a bare username is not a secret,
    and blanking it would cost an operator the "which identity failed to
    authenticate" half of the message for no gain.

    The bare password is replaced only where it stands as its own token — not where
    it happens to be a substring of a longer word. The naive
    ``text.replace(password, "***")`` is unusable here, and not marginally: a
    one-character password turns ``amqp://`` into ``amq***://`` and the line stops
    being readable at all, while protecting nothing, because a password is only
    legible *as* a credential where it stands alone. This still over-masks in the
    direction the Postgres version chose — a password that is an ordinary English
    word blanks that word out — and what an operator needs ("connection refused",
    "authentication failed", the host and port) survives intact.

    The boundary this does **not** cross: :meth:`AMQPSink.from_url` still lets the
    driver's own exception propagate unredacted, exactly as
    ``tokenweir.migrations.connect`` does for psycopg. The caller supplied the URL
    and is in a position to handle the driver's own exception type; what this
    library must not do is write those credentials to a log itself.
    """
    if not url:
        return text
    # `[^/?]*` rather than `[^/@?]*`, and greedy: it runs to the **last** `@`
    # before the path. With the first `@` instead, a password containing an
    # unencoded one — `amqp://user:p@ss@host/` — split at the wrong place and the
    # tail of the credential (`ss`) reached the log. Such a URL is RFC-3986-invalid
    # (userinfo `@` must be percent-encoded), which is exactly why it is worth
    # handling: a caller who has made that mistake is the one whose password is
    # least likely to survive the round trip intact.
    match = re.match(r"[a-z+]+://([^/?]*)@", url, re.IGNORECASE)
    if not match or not match.group(1):
        return text
    userinfo = match.group(1)
    _, colon, password = userinfo.partition(":")
    if not colon or not password:
        # A bare username carries nothing to hide.
        return text
    redacted = text.replace(userinfo, "***")
    return re.sub(
        rf"(?<![A-Za-z0-9]){re.escape(password)}(?![A-Za-z0-9])", "***", redacted
    )


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

    **``pika`` is needed to construct one of these, and that is on purpose.** The
    message properties are built here, at wiring time, rather than on the first
    publish — see the module docstring: a driver discovered missing on the publish
    path cannot be reported, because that path may not raise, so it would become a
    permanent silent drop instead of an error. Pass ``properties=`` to build a sink
    with no driver present.

    Args:
        channel: anything with ``basic_publish``. Not type-checked — a pika
            channel, a wrapper, or a test double are all legitimate.
        exchange: defaults to AMQP's default direct exchange.
        routing_key: defaults to :data:`DEFAULT_ROUTING_KEY`.
        properties: the properties object attached to every message. Defaults to
            ``pika.BasicProperties(**publish_properties())``, built once here.
            Supply your own to add an ``app_id``, a ``expiration`` or headers — or
            to construct a sink where ``pika`` is not installed.
        connection: the connection behind ``channel``, if this sink should be able
            to close it. Set by :meth:`from_url`.
        owns_connection: whether :meth:`close` should close ``connection``.
        warn_interval: seconds between warning lines for a repeating failure, so an
            unreachable broker cannot produce one log line per metered call.
        reconnect_interval: minimum seconds between reconnect *attempts* once the
            sink has reason to think re-dialling will not help — a dial that
            failed, or two connections in a row that could not publish. Losing a
            connection that *had* been publishing is recovered immediately, and so
            is the first connection that fails to publish; see :meth:`_invalidate`
            for why those two are not the same case. This bounds how often a broken
            broker or a misconfigured exchange is re-dialled, because a blocking
            connect per record is a retry loop by another name. ``0`` attempts on
            every publish.

            **Both immediate cases are still subject to**
            :data:`MAX_DIALS_PER_INTERVAL`, a hard cap on dials per interval that
            applies whatever the reasoning above concludes. It exists because that
            reasoning infers a broker's intent from a driver whose publish is
            asynchronous, and an inference is the wrong thing to hang a bound on;
            see the constant for the detail.
        clock: monotonic seconds source. A **test seam**, not a production knob —
            it exists so the reconnect interval can be driven deterministically
            without sleeping, and no deployment should need to pass it.

    Raises:
        ImportError: ``pika`` is not installed and no ``properties`` were given,
            naming the extra.
    """

    def __init__(
        self,
        channel: Any,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        routing_key: str = DEFAULT_ROUTING_KEY,
        properties: Any = None,
        connection: Any = None,
        owns_connection: bool = False,
        warn_interval: float = 60.0,
        reconnect_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._channel = channel
        self._connection = connection
        self._owns_connection = owns_connection
        self.exchange = exchange
        self.routing_key = routing_key
        self._closed = False
        self._published = 0
        self._dropped = 0
        self._properties = (
            properties
            if properties is not None
            else _import_pika().BasicProperties(**publish_properties())
        )
        self._url: Optional[str] = None
        self._reconnect: Optional[Callable[[], Tuple[Any, Any]]] = None
        self._clock = clock
        self._reconnect_interval = max(0.0, float(reconnect_interval))
        self._next_reconnect_at = 0.0
        # How many records the connection currently held has published, and how
        # many connections in a row have been discarded without proving
        # themselves. Together they decide whether a publish failure earns an
        # immediate re-dial or has to wait out `reconnect_interval` — see
        # `_invalidate` for why the threshold is *more than one*.
        self._connection_publishes = 0
        self._consecutive_unproductive = 0
        # The backstop: dials are capped per interval whatever the productivity
        # bookkeeping concludes. See `_live_channel`.
        self._dial_window_ends = 0.0
        self._dials_in_window = 0
        # Which of the two causes armed `_next_reconnect_at`, so the drop it
        # produces can name the right one. Defaults to the dial case; it is only
        # ever read while the interval is armed, and arming always sets it.
        self._backoff_message = _AWAITING_REDIAL
        self._warner = RateLimitedWarner(_logger, interval=warn_interval)

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        exchange: str = DEFAULT_EXCHANGE,
        routing_key: str = DEFAULT_ROUTING_KEY,
        properties: Any = None,
        warn_interval: float = 60.0,
        reconnect_interval: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> "AMQPSink":
        """Open a pika ``BlockingConnection`` from an AMQP URL and own it.

        Raises here, rather than degrading to drops, because opening a connection
        is wiring-time work: a caller who cannot reach the broker at start-up
        wants to know at start-up, and there is no metered request in scope to
        protect. Once the sink exists, every later failure — including losing this
        connection — is a counted drop.

        Raises:
            ImportError: ``pika`` is not installed, naming the extra.
            Exception: whatever pika raises if the broker is unreachable. Left
                unredacted, and deliberately: the caller supplied ``url``, and
                ``tokenweir.migrations.connect`` sets the same boundary for
                psycopg — the library propagates the driver's own exception type,
                and redacts only what it writes to a log *itself* (see
                :func:`_redact_url`).
        """

        def connect() -> Tuple[Any, Any]:
            pika = _import_pika()
            connection = pika.BlockingConnection(pika.URLParameters(url))
            try:
                return connection, connection.channel()
            except BaseException:
                # A broker that accepts TCP and then refuses to open a channel is
                # a real state, and this closure is also the *reconnect* callable —
                # so without this the sink leaks one socket per reconnect interval
                # for the life of the process. Closing it is guarded because the
                # channel failure is the one worth reporting.
                try:
                    connection.close()
                except Exception:
                    pass
                raise

        connection, channel = connect()
        sink = cls(
            channel,
            exchange=exchange,
            routing_key=routing_key,
            properties=properties,
            connection=connection,
            owns_connection=True,
            warn_interval=warn_interval,
            reconnect_interval=reconnect_interval,
            clock=clock,
        )
        sink._url = url
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

        # `_live_channel` logs its own failure reasons — there are three of them
        # and they are only distinguishable inside it. FR-008 wants every drop
        # logged, but a drop logged with the wrong cause is worse than a bare
        # count: "cannot make one" told an operator their sink was unrecoverable
        # when it was merely inside its backoff window and about to recover on its
        # own. Re-deriving the reason out here is what produced both that lie and a
        # second warning for one lost record.
        channel = self._live_channel()
        if channel is None:
            self._dropped += 1
            return

        try:
            channel.basic_publish(
                exchange=self.exchange,
                routing_key=self.routing_key,
                body=body,
                properties=self._properties,
            )
        except Exception:
            self._dropped += 1
            self._invalidate()
            self._warner.warn(_PUBLISH_FAILED)
            return
        self._published += 1
        self._connection_publishes += 1

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

    def _live_channel(self) -> Any:
        """The channel to publish on, re-establishing it if this sink can.

        **Reconnect attempts are spaced by ``reconnect_interval``**, and that bound
        is the point rather than a refinement. ``pika.BlockingConnection`` blocks
        for the socket's connect timeout, and this runs on the delivery worker — so
        an unbounded attempt-per-record rate is exactly the "retry loop in front of
        a broken broker" this module's docstring says it refuses, reintroduced one
        attempt at a time. Against a broker that is down it would spend a full
        connect timeout per buffered record while the buffer fills behind it.

        **When the spacing applies is decided in** :meth:`_invalidate`, which sets
        ``_next_reconnect_at``; this method only honours it. In short: a connection
        that had been publishing is replaced immediately, a failed dial is spaced,
        and a connection that never published is replaced once and then spaced. The
        reasoning for all three is on :meth:`_invalidate`, and it is worth reading
        before changing either.
        """
        if self._channel is not None:
            return self._channel
        if self._reconnect is None:
            # A channel this sink cannot re-make — borrowed, or absent entirely.
            # Not ours to replace; whoever owns it owns its liveness.
            self._warner.warn(_NO_CHANNEL, exc_info=False)
            return None
        if self._clock() < self._next_reconnect_at:
            # Backing off. Which cause armed it decides what an operator should do
            # about it — wait, or go and look at the broker's topology — so the
            # message is the one whichever arm set (see `_invalidate`).
            self._warner.warn(self._backoff_message, exc_info=False)
            return None
        if not self._may_dial():
            self._warner.warn(_DIAL_CEILING_REACHED, exc_info=False)
            return None
        try:
            self._connection, self._channel = self._reconnect()
        except Exception as exc:
            self._next_reconnect_at = self._clock() + self._reconnect_interval
            self._backoff_message = _AWAITING_REDIAL
            # `exc_info=False`, and the cause rendered by hand: a traceback would
            # carry the driver's own message, and a driver that echoes the URL it
            # could not reach would put `user:password@host` in the log through
            # the one channel `_redact_url` cannot reach. The type and the redacted
            # message are what an operator needs; the credentials are not.
            self._warner.warn(
                f"{_RECONNECT_FAILED}: {type(exc).__name__}: "
                f"{_redact_url(str(exc), self._url)}",
                key=_RECONNECT_FAILED,
                exc_info=False,
            )
            return None
        # Nothing to reset here. `_next_reconnect_at` is only ever read as
        # `clock() < it`, and a dial happens only when that is already false, so
        # clearing it would be writing a value that is by definition already spent
        # — it was kept for one round as "the obvious invariant, stated where a
        # reader looks", and a mutation test showed it could be deleted with the
        # whole suite green, which is what that costs. `_connection_publishes` is
        # cleared by `_invalidate`, the only path that nulls `_channel`, so a
        # connection reached from here always starts unproven: one reset point
        # rather than two that have to agree.
        return self._channel

    def _may_dial(self) -> bool:
        """Whether the per-interval dial cap still has room, counting this dial.

        The backstop described on :data:`MAX_DIALS_PER_INTERVAL`. Everything above
        this is a judgement about *why* a connection failed, inferred from a
        driver whose error semantics this project cannot exercise against a real
        broker. This is the part that does not depend on getting that judgement
        right: however many times the productivity rule concludes "this one
        deserves an immediate re-dial", dialling is capped per interval.

        A zero ``reconnect_interval`` disables it, as it disables the interval
        itself — that setting means "no spacing", and a cap would be spacing.
        """
        if not self._reconnect_interval:
            return True
        now = self._clock()
        if now >= self._dial_window_ends:
            self._dial_window_ends = now + self._reconnect_interval
            self._dials_in_window = 0
        if self._dials_in_window >= MAX_DIALS_PER_INTERVAL:
            return False
        self._dials_in_window += 1
        return True

    def _invalidate(self) -> None:
        """Drop a failed connection so the next publish re-establishes it.

        Gated on being **able to reconnect**, not on owning the connection. Those
        are not the same condition, and keying off ownership was a defect: only
        :meth:`from_url` sets ``_reconnect``, so the documented public combination
        ``AMQPSink(channel, connection=conn, owns_connection=True)`` closed its
        connection, nulled its channel, and then had no way to make another — a
        sink permanently dead after one publish failure, which is strictly worse
        than the borrowed case it was modelled on, since there the caller can at
        least repair the channel out of band.

        A channel this sink cannot re-make is therefore left exactly as it was,
        whoever owns it. Publishing keeps failing, and keeps being counted and
        logged, until the thing behind it is repaired — which is a state a caller
        can act on, unlike silence.

        **Whether the next dial waits** is the question TOKWEIR-6's FR-024 left
        open: does a failed *publish* count as losing the connection? TOKWEIR-30
        answers it, and the answer is not a simple yes or no, because a publish
        failure is genuinely ambiguous evidence:

        - The connection **had been publishing** and then failed. Something outside
          this process broke, re-dialling is what fixes it, and making a caller
          wait out an interval to recover from a blip trades a real fault for an
          invented one. Immediate.
        - The connection **never published**, and neither did the one before it.
          Two fresh connections in a row could not deliver a record, so the fault
          is not in the connection — a bad exchange, a permissions error, a routing
          key that matches nothing. Re-dialling cannot help, and doing it per
          record is a blocking dial per metered call on the emitter's delivery
          worker: the retry loop this module refuses. Armed.
        - The connection **never published** but is the first to fail. This is the
          ambiguous case, and it is why the rule is not simply "did it publish?".
          A connection reset *before* the first publish is an ordinary blip, and
          from a sink's very first record it is indistinguishable from a
          misconfiguration. So it gets **one** free re-dial: if the replacement
          also cannot publish, the ambiguity is resolved and the interval arms.
          One extra dial is a cheap price for not turning every cold-start blip
          into an interval-long outage.

        Deliberately *not* done by classifying the driver's exceptions into
        channel-level and connection-level errors. That is more precise on paper
        and worse here: it would couple this adapter to a pika exception hierarchy
        it otherwise imports nothing from — the module's discipline is that
        ``pika`` appears in exactly one function — and it would need
        broker-specific reply codes to stay accurate, so it would rot silently.
        Counting connections that failed to publish needs no driver knowledge and
        answers the question that actually matters: can re-dialling plausibly help?
        """
        if self._reconnect is None:
            return
        if self._connection_publishes:
            # A connection that had been publishing was lost. That is what
            # re-dialling is for, and the allowance renews here rather than once
            # per sink, so a flaky link that keeps working between drops recovers
            # instantly every time.
            #
            # One publish is taken as evidence even though, without confirms, it is
            # not conclusive — see `MAX_DIALS_PER_INTERVAL` for why that is the
            # right trade and what covers the case where this is wrong.
            self._consecutive_unproductive = 0
        else:
            self._consecutive_unproductive += 1
            if self._consecutive_unproductive > 1:
                self._next_reconnect_at = self._clock() + self._reconnect_interval
                self._backoff_message = _AWAITING_PUBLISHABLE
        self._release()
        self._channel = None
        self._connection = None
        # The single reset point for this counter (see `_live_channel`): a
        # connection discarded here is the only way a new one comes to be dialled,
        # so clearing it now is what makes every fresh connection start unproven.
        self._connection_publishes = 0

    def _release(self) -> None:
        """Close an owned connection, swallowing whatever it says about it."""
        if not self._owns_connection or self._connection is None:
            return
        try:
            self._connection.close()
        except Exception:
            self._warner.warn(_CLOSE_FAILED)
