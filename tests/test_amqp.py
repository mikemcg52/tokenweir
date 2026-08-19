"""The AMQP adapter and the dependency-light core (TOKWEIR-6).

Two of the story's three acceptance clauses live here: *"a service emits via the
client with an AMQP sink"* and *"the core imports without transport libraries"*.

**The whole suite runs with no broker and no `pika` package installed**, which is
not a compromise — it is the point. What an adapter test can meaningfully assert is
that the right bytes go to the right exchange with the right properties; that
RabbitMQ works is RabbitMQ's test.

Being precise about *how*, because the earlier version of this docstring was not
and the imprecision hid a defect. `message_for` and `publish_properties` need
nothing at all and are exercised directly. Constructing an `AMQPSink` does need a
`pika` module object, because that is where the message properties come from — so
these tests get one of two ways, each deliberate:

- `properties=` — the documented escape hatch for a caller with a channel double,
  which needs no `pika` in any form;
- the `fake_pika` fixture, which injects a minimal module into `sys.modules` so the
  real deferred-import code runs rather than a stand-in for it. That is the only
  way to cover `from_url` and the default properties at all here.

And `sys.modules["pika"] = None` covers the missing-driver path, which is the one
that has to fail loudly rather than silently.
"""

import json
import logging
import subprocess
import sys
import types

import pytest

import tokenweir
from tokenweir import BufferedEmitter, Sink, UsageRecord, emit_usage
from tokenweir.amqp import (
    CONTENT_TYPE,
    DEFAULT_EXCHANGE,
    DEFAULT_ROUTING_KEY,
    PERSISTENT_DELIVERY_MODE,
    AMQPSink,
    message_for,
    publish_properties,
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


class FakeProperties:
    """Stands in for `pika.BasicProperties`, recording what it was asked for."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __eq__(self, other):
        return isinstance(other, FakeProperties) and self.kwargs == other.kwargs


class RecordingChannel:
    def __init__(self, fail_with=None):
        self.published = []
        self.fail_with = fail_with
        self.closed = 0

    def basic_publish(self, exchange, routing_key, body, properties=None):
        if self.fail_with is not None:
            raise self.fail_with
        self.published.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )

    def close(self):
        self.closed += 1


class RecordingConnection:
    def __init__(self, channel=None, fail_on_close=False):
        self._channel = channel or RecordingChannel()
        self.closed = 0
        self.fail_on_close = fail_on_close

    def channel(self):
        return self._channel

    def close(self):
        self.closed += 1
        if self.fail_on_close:
            raise RuntimeError("the broker went away first")


@pytest.fixture
def fake_pika(monkeypatch):
    """Install a minimal fake `pika`, so the deferred-import path is real code.

    `pika` is genuinely not installed in this project's test environment — that is
    what `test_the_core_imports_without_a_transport_library` is about — so the
    alternative to this fixture is leaving the adapter's default properties and
    `from_url` untested, which is exactly where a transport bug would hide.
    """
    connections = []

    class BlockingConnection:
        def __init__(self, parameters):
            self.parameters = parameters
            self._channel = RecordingChannel()
            self.closed = 0
            connections.append(self)

        def channel(self):
            return self._channel

        def close(self):
            self.closed += 1

    class URLParameters:
        def __init__(self, url):
            self.url = url

    module = types.SimpleNamespace(
        BasicProperties=FakeProperties,
        BlockingConnection=BlockingConnection,
        URLParameters=URLParameters,
    )
    module.connections = connections
    monkeypatch.setitem(sys.modules, "pika", module)
    return module


# --- The pure mapping, with no pika and no broker (FR-025, SC-007) -----------


def test_the_message_is_the_records_json_wire_form():
    record = _record(1, input_tokens=120, output_tokens=34)
    body = message_for(record)
    assert isinstance(body, bytes)
    assert UsageRecord.from_json(body) == record


def test_the_body_is_valid_utf8_json():
    body = message_for(_record(1, model="モデル"))
    assert json.loads(body.decode("utf-8"))["model"] == "モデル"


def test_the_encoding_is_compact_and_stable():
    """Compact because a metering payload is paid for on every metered call;
    stable so two captured messages for the same record are diffable."""
    record = _record(1)
    assert message_for(record) == message_for(record)
    assert b", " not in message_for(record)
    keys = list(json.loads(message_for(record)))
    assert keys == sorted(keys)


def test_a_non_record_is_refused_by_the_mapping():
    """The guarded seam returns `None` on a construction drop; `null` on the wire
    would be the "crashes into garbage" outcome that seam exists to prevent."""
    for value in (None, "not a record", 42, {"request_id": "r"}):
        with pytest.raises(TypeError):
            message_for(value)


def test_the_publish_properties_ask_for_a_persistent_json_message():
    """FR-022. Metering that evaporates on a broker restart would make the broker
    path pointless — surviving a consumer outage is the only reason to accept one."""
    assert publish_properties() == {
        "content_type": CONTENT_TYPE,
        "delivery_mode": PERSISTENT_DELIVERY_MODE,
    }
    assert CONTENT_TYPE == "application/json"
    assert PERSISTENT_DELIVERY_MODE == 2


# --- Publishing against a channel double (FR-019, FR-022) --------------------


def test_it_publishes_to_the_default_exchange_and_routing_key(fake_pika):
    channel = RecordingChannel()
    sink = AMQPSink(channel)
    sink.emit(_record(1))

    assert len(channel.published) == 1
    published = channel.published[0]
    assert published["exchange"] == DEFAULT_EXCHANGE
    assert published["routing_key"] == DEFAULT_ROUTING_KEY
    assert UsageRecord.from_json(published["body"]) == _record(1)
    assert published["properties"] == FakeProperties(**publish_properties())
    assert sink.published == 1


def test_the_exchange_and_routing_key_are_the_callers(fake_pika):
    channel = RecordingChannel()
    sink = AMQPSink(channel, exchange="metering", routing_key="usage.gateway")
    sink.emit(_record(1))
    published = channel.published[0]
    assert published["exchange"] == "metering"
    assert published["routing_key"] == "usage.gateway"


def test_the_properties_object_is_built_once(fake_pika):
    """A metered path should not rebuild an invariant per record."""
    channel = RecordingChannel()
    sink = AMQPSink(channel)
    for n in range(5):
        sink.emit(_record(n))
    properties = [p["properties"] for p in channel.published]
    assert all(p is properties[0] for p in properties)


def test_it_is_a_sink_but_not_a_batch_sink():
    """AMQP has no batch publish; an `emit_batch` here would be a loop wearing a
    costume. Leaving it off is what exercises the emitter's per-record fallback."""
    sink = AMQPSink(RecordingChannel(), properties=FakeProperties())
    assert isinstance(sink, Sink)
    assert not isinstance(sink, BatchSink)


# --- Failures never raise (FR-019, SC-003) -----------------------------------


def test_a_publish_failure_never_reaches_the_caller(fake_pika):
    sink = AMQPSink(RecordingChannel(fail_with=RuntimeError("channel closed")))
    sink.emit(_record(1))  # must not raise
    assert sink.published == 0
    assert sink.dropped == 1


def test_a_non_record_is_refused_before_the_channel_sees_it(fake_pika):
    channel = RecordingChannel()
    sink = AMQPSink(channel)
    sink.emit(None)
    sink.emit("not a record")
    assert channel.published == []
    assert sink.dropped == 2


def test_publishing_after_close_is_a_counted_drop(fake_pika):
    channel = RecordingChannel()
    sink = AMQPSink(channel)
    sink.close()
    sink.emit(_record(1))  # must not raise
    assert channel.published == []
    assert sink.dropped == 1


def test_a_publish_failure_through_the_emitter_never_reaches_the_caller(fake_pika):
    sink = AMQPSink(RecordingChannel(fail_with=RuntimeError("broker down")))
    with BufferedEmitter(sink, linger=0.0) as emitter:
        for n in range(5):
            emitter.emit(_record(n))  # must not raise
        assert emitter.flush(timeout=TIMEOUT)
    assert sink.dropped == 5


# --- Ownership and reconnection (FR-023, FR-024) -----------------------------


def test_a_borrowed_channel_is_left_open(fake_pika):
    channel = RecordingChannel()
    connection = RecordingConnection(channel)
    sink = AMQPSink(channel, connection=connection)
    sink.close()
    assert connection.closed == 0
    assert channel.closed == 0


def test_an_owned_connection_is_closed(fake_pika):
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/")
    connection = fake_pika.connections[0]
    sink.close()
    assert connection.closed == 1


def test_close_is_idempotent(fake_pika):
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/")
    connection = fake_pika.connections[0]
    sink.close()
    sink.close()
    assert connection.closed == 1


def test_close_survives_a_connection_that_raises(fake_pika):
    connection = RecordingConnection(fail_on_close=True)
    sink = AMQPSink(
        connection.channel(), connection=connection, owns_connection=True
    )
    sink.close()  # must not raise
    assert connection.closed == 1


def test_an_owned_connection_is_re_established_on_a_later_attempt(fake_pika):
    """FR-024: recovery is a *later* attempt, not a retry inside the current one —
    an unbounded wait on the delivery thread is the thing being avoided."""
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/")
    first = fake_pika.connections[0]
    first.channel().fail_with = RuntimeError("connection reset")

    sink.emit(_record(1))
    assert sink.dropped == 1
    assert first.closed == 1, "the failed connection was not dropped"

    sink.emit(_record(2))
    assert len(fake_pika.connections) == 2, "no reconnection was attempted"
    second = fake_pika.connections[1]
    assert len(second.channel().published) == 1
    assert sink.published == 1


def test_a_borrowed_channel_is_never_reconnected(fake_pika):
    """Re-making a caller's channel would substitute this module's idea of how to
    connect — TLS, credentials, pooling — for theirs. Their channel, their
    liveness."""
    channel = RecordingChannel(fail_with=RuntimeError("connection reset"))
    connection = RecordingConnection(channel)
    sink = AMQPSink(channel, connection=connection)

    sink.emit(_record(1))
    sink.emit(_record(2))

    assert connection.closed == 0, "a borrowed connection was closed"
    assert fake_pika.connections == [], "a borrowed channel was reconnected"
    assert sink.dropped == 2


def test_a_reconnect_that_also_fails_is_a_drop_not_a_raise(monkeypatch, fake_pika):
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/")
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(1))

    def refuse(parameters):
        raise RuntimeError("the broker is still down")

    monkeypatch.setattr(fake_pika, "BlockingConnection", refuse)
    sink.emit(_record(2))  # must not raise
    assert sink.dropped == 2
    assert sink.published == 0


def test_the_channel_is_reachable_for_a_caller_that_needs_it():
    channel = RecordingChannel()
    assert AMQPSink(channel, properties=FakeProperties()).channel is channel


def test_from_url_passes_the_url_to_pika(fake_pika):
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit:5672/%2Fvhost")
    assert fake_pika.connections[0].parameters.url == (
        "amqp://guest:guest@rabbit:5672/%2Fvhost"
    )
    sink.close()


# --- The missing driver names the extra (FR-021) -----------------------------


def test_from_url_without_pika_names_the_extra(monkeypatch):
    """A bare `ModuleNotFoundError: pika` leaves a caller to work out both what to
    install and whether they even need it. `sys.modules[name] = None` is the
    documented way to make `import name` raise, so this drives the real path."""
    monkeypatch.setitem(sys.modules, "pika", None)
    with pytest.raises(ImportError, match=r"tokenweir\[amqp\]"):
        AMQPSink.from_url("amqp://guest:guest@nowhere/")


def test_constructing_without_pika_fails_loudly_rather_than_dropping_silently(
    monkeypatch,
):
    """The defect this replaces: the driver import used to sit on the *publish*
    path, inside the guard that may not raise. A missing `pika` was therefore not
    an error at all — it was a silent 100% drop, for the life of the process, in
    the one code path whose job is never to complain. Wiring time is where a
    missing dependency has to surface, because it is the last moment anyone is
    watching."""
    monkeypatch.setitem(sys.modules, "pika", None)
    with pytest.raises(ImportError, match=r"tokenweir\[amqp\]"):
        AMQPSink(RecordingChannel())


def test_a_channel_double_can_be_used_with_no_pika_at_all(monkeypatch):
    """The remedy the ImportError advertises has to actually work. It did not
    before: the text said "pass an existing channel instead", and that was the
    exact path that silently dropped everything."""
    monkeypatch.setitem(sys.modules, "pika", None)
    channel = RecordingChannel()
    properties = FakeProperties(**publish_properties())
    sink = AMQPSink(channel, properties=properties)

    sink.emit(_record(1))

    assert len(channel.published) == 1
    assert channel.published[0]["properties"] is properties
    assert sink.published == 1
    assert sink.dropped == 0


def test_the_import_errors_advertised_remedy_is_the_one_that_works(monkeypatch):
    monkeypatch.setitem(sys.modules, "pika", None)
    with pytest.raises(ImportError) as caught:
        AMQPSink(RecordingChannel())
    message = str(caught.value)
    assert "properties=" in message, "the error does not name the working remedy"
    assert "pass an existing channel to AMQPSink instead" not in message


def test_supplied_properties_are_used_verbatim(fake_pika):
    """A caller adding an `app_id` or an `expiration` must not have them replaced
    by the default."""
    channel = RecordingChannel()
    mine = FakeProperties(content_type="application/json", app_id="ai-gateway")
    sink = AMQPSink(channel, properties=mine)
    sink.emit(_record(1))
    assert channel.published[0]["properties"] is mine


def test_from_url_passes_properties_through(fake_pika):
    mine = FakeProperties(app_id="ai-gateway")
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/", properties=mine)
    sink.emit(_record(1))
    assert fake_pika.connections[0].channel().published[0]["properties"] is mine


# --- The dependency-light core (FR-026, FR-027, SC-002) ----------------------


def test_the_core_imports_without_a_transport_library():
    """The story's second acceptance clause. Asserted in a subprocess against
    `sys.modules`, so the result holds whether or not `pika` is installed here —
    the alternative, uninstalling it, would make the test depend on the very
    environment it is meant to be independent of."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tokenweir, tokenweir.contract, tokenweir.sink,"
            " tokenweir.emitter, tokenweir.source, tokenweir.postgres,"
            " tokenweir.migrations;"
            "assert 'pika' not in sys.modules, sorted(sys.modules);"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_importing_the_adapter_does_not_import_pika():
    """FR-020. The mapping has to be usable — and testable — with no AMQP library,
    which is the same rule `tokenweir.migrations` holds for psycopg."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tokenweir.amqp;"
            "assert 'pika' not in sys.modules, sorted(sys.modules);"
            "tokenweir.amqp.message_for(tokenweir.amqp.UsageRecord("
            "request_id='r', app_id='a', endpoint='/e', model='m', status='ok'));"
            "assert 'pika' not in sys.modules;"
            "print('ok')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_adapter_is_not_in_the_package_namespace():
    """FR-027. Reached by its own import path, exactly as the store is, so
    `import tokenweir` can never drag in transport code."""
    assert "AMQPSink" not in tokenweir.__all__
    assert not hasattr(tokenweir, "AMQPSink")
    assert "amqp" not in tokenweir.__all__


def test_the_emit_side_is_in_the_package_namespace():
    """The counterpart: what a caller needs on a metered path is one import."""
    for name in ("BufferedEmitter", "EmitterStats", "DirectSink", "BatchSink"):
        assert name in tokenweir.__all__
        assert hasattr(tokenweir, name)


# --- The story's first acceptance clause, broker half (SC-001) ---------------


def test_a_service_emits_via_the_client_with_an_amqp_sink(fake_pika):
    """The end-to-end shape the homelab deployment actually writes."""
    channel = RecordingChannel()
    fields = {
        "request_id": "req-homelab",
        "app_id": "ai-gateway",
        "endpoint": "/v1/messages",
        "model": "claude-opus-5",
        "status": "ok",
    }
    sink = AMQPSink(channel, exchange="", routing_key="tokenweir.usage")
    with BufferedEmitter(sink, linger=0.0) as emitter:
        assert emit_usage(emitter, fields, input_tokens=120, output_tokens=34) is not None
        assert emitter.flush(timeout=TIMEOUT)

    assert len(channel.published) == 1
    published = channel.published[0]
    assert published["routing_key"] == "tokenweir.usage"
    delivered = UsageRecord.from_json(published["body"])
    assert delivered.request_id == "req-homelab"
    assert delivered.input_tokens == 120
    assert delivered.output_tokens == 34
    assert sink.published == 1


# --- Credentials never reach a log this library writes (Low #7) --------------


def test_a_reconnect_failure_does_not_log_the_urls_credentials(fake_pika, caplog):
    """An AMQP URL conventionally carries `user:password@host`, and a connect
    failure is exactly when a library is tempted to echo what it could not reach.
    `tokenweir.migrations` already set this rule for the Postgres equivalent
    (`_redact` before printing a connect failure); this is the same rule."""
    url = "amqp://gateway:sup3r-s3cret@rabbit:5672/%2Fmetering"
    sink = AMQPSink.from_url(url)
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(1))

    def refuse(parameters):
        raise RuntimeError(f"could not connect to {url}: authentication failed")

    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        import pika  # the fixture's fake

        pika.BlockingConnection = refuse
        sink.emit(_record(2))

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert logged, "the reconnect failure produced no warning at all"
    assert "sup3r-s3cret" not in logged, logged
    assert "gateway:sup3r-s3cret" not in logged, logged
    # What an operator actually needs from the line survives redaction.
    assert "authentication failed" in logged
    assert "rabbit:5672" in logged


@pytest.mark.parametrize(
    ("text", "url", "expected"),
    [
        (
            "cannot reach amqp://u:p@h/",
            "amqp://u:p@h/",
            "cannot reach amqp://***@h/",
        ),
        # No userinfo: nothing to hide, and nothing over-masked.
        ("cannot reach amqp://h/", "amqp://h/", "cannot reach amqp://h/"),
        # A bare user with no password carries no secret.
        ("cannot reach amqp://u@h/", "amqp://u@h/", "cannot reach amqp://u@h/"),
        # The password alone, echoed away from the URL, is masked too.
        ("auth failed for s3cret", "amqp://u:s3cret@h/", "auth failed for ***"),
        ("anything at all", None, "anything at all"),
    ],
)
def test_redaction_masks_credentials_without_mangling_the_rest(text, url, expected):
    from tokenweir.amqp import _redact_url

    assert _redact_url(text, url) == expected


def test_from_url_still_raises_the_drivers_own_exception(fake_pika):
    """The deliberate boundary, matching `tokenweir.migrations.connect`: the
    library propagates the driver's own exception type — the caller supplied the
    URL and may well be catching `pika.exceptions.AMQPConnectionError` — and
    redacts only what it writes to a log itself."""

    class RefusedError(RuntimeError):
        pass

    def refuse(parameters):
        raise RefusedError("connection refused")

    fake_pika.BlockingConnection = refuse
    with pytest.raises(RefusedError):
        AMQPSink.from_url("amqp://u:p@rabbit/")


# --- Distinct failure reasons are rate-limited separately (Med #2) -----------


def test_a_publish_failure_does_not_silence_a_refused_record(fake_pika, caplog):
    sink = AMQPSink(
        RecordingChannel(fail_with=RuntimeError("channel closed")),
        warn_interval=3600.0,
    )
    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        sink.emit(_record(1))  # a publish failure
        sink.emit(None)  # a refused non-record, a different reason entirely

    messages = [r.getMessage() for r in caplog.records]
    assert any("the AMQP channel failed" in m for m in messages), messages
    assert any("non-UsageRecord" in m for m in messages), messages


def test_repeated_reconnect_failures_are_rate_limited_despite_varying_text(
    fake_pika, caplog
):
    """The reconnect warning is the one message in this package that interpolates —
    the exception type and its text go into the line. Rate limiting keys on the
    message by default, so without an explicit `key=` every varying error string is
    a distinct reason and the limiting is defeated entirely: one WARNING per publish
    attempt against a broker that is down, which is precisely the FR-008 failure
    mode. A driver that includes an attempt number, a port or a timestamp in its
    message is not exotic; it is the normal case."""
    # `reconnect_interval=0.0` on purpose: the backoff added for the
    # attempt-per-record defect would otherwise let this pass with a single
    # reconnect attempt, and the thing under test is the *rate limiter's* keying,
    # not the backoff. With the interval closed off, every emit really does try to
    # reconnect and really does produce a distinct error string, which is what
    # makes removing `key=` fail this test.
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", warn_interval=3600.0, reconnect_interval=0.0
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))
    caplog.clear()

    attempts = iter(range(1, 100))

    def refuse(parameters):
        raise RuntimeError(f"connect attempt {next(attempts)} refused at 17:0{next(attempts)}")

    fake_pika.BlockingConnection = refuse

    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        for n in range(1, 40):
            sink.emit(_record(n))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) <= 2, (
        f"{len(warnings)} warnings for 39 reconnect failures — the varying error "
        "text defeated rate limiting"
    )
    assert sink.dropped == 40


def test_a_sink_with_no_channel_drops_loudly_rather_than_silently(caplog):
    """A drop with no counter and no log is the one outcome the whole design is
    arranged to prevent. This branch had the counter and not the log."""
    sink = AMQPSink(None, properties=FakeProperties())
    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        for n in range(5):
            sink.emit(_record(n))  # must not raise

    assert sink.dropped == 5
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "5 records vanished without a single warning"
    assert any("no channel" in m for m in warnings), warnings


def test_an_invalidated_borrowed_channel_keeps_reporting(fake_pika, caplog):
    """A borrowed channel is never reconnected — the caller owns its liveness — so
    the sink must keep saying so rather than going quiet after the first line."""
    channel = RecordingChannel(fail_with=RuntimeError("connection reset"))
    sink = AMQPSink(channel, connection=RecordingConnection(channel), warn_interval=0.0)
    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        for n in range(3):
            sink.emit(_record(n))
    assert sink.dropped == 3
    assert [r for r in caplog.records if r.levelno == logging.WARNING]


# --- Reconnection is gated on being able to, not on owning (Med #1) ----------


def test_a_constructor_owned_connection_still_publishes_after_a_failure(fake_pika):
    """`owns_connection=True` outside `from_url` used to be a death sentence:
    `_invalidate` keyed off ownership, but only `from_url` supplies the callable
    that can re-make a connection, so the sink closed the one it had and then had
    no way to make another. Strictly worse than the borrowed case, where the caller
    can at least repair the channel out of band."""
    channel = RecordingChannel(fail_with=RuntimeError("connection reset"))
    connection = RecordingConnection(channel)
    sink = AMQPSink(channel, connection=connection, owns_connection=True)

    sink.emit(_record(1))
    assert sink.dropped == 1

    # The broker comes back and the caller repairs the channel they own.
    channel.fail_with = None
    sink.emit(_record(2))

    assert sink.published == 1, "the sink was permanently dead after one failure"
    assert len(channel.published) == 1


def test_a_connection_it_cannot_remake_is_not_closed_underneath_the_caller(fake_pika):
    channel = RecordingChannel(fail_with=RuntimeError("connection reset"))
    connection = RecordingConnection(channel)
    sink = AMQPSink(channel, connection=connection, owns_connection=True)
    sink.emit(_record(1))
    assert connection.closed == 0, "a connection with no way to be re-made was closed"
    assert sink.channel is channel


def test_close_still_closes_a_constructor_owned_connection(fake_pika):
    """Ownership still decides *closing*; it just no longer decides reconnecting."""
    channel = RecordingChannel()
    connection = RecordingConnection(channel)
    AMQPSink(channel, connection=connection, owns_connection=True).close()
    assert connection.closed == 1


# --- Reconnect attempts are spaced (Med #2) ----------------------------------


def test_a_down_broker_is_not_re_dialled_once_per_record(fake_pika):
    """`pika.BlockingConnection` blocks for the connect timeout, and this runs on
    the delivery worker — so an attempt per record is the "retry loop in front of a
    broken broker" the module docstring refuses, reintroduced one attempt at a
    time. Against a real down broker it would spend a full connect timeout per
    buffered record while the buffer fills behind it."""
    now = [1000.0]
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", reconnect_interval=30.0, clock=lambda: now[0]
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))  # invalidates

    attempts = []

    def refuse(parameters):
        attempts.append(now[0])
        raise RuntimeError("connection refused")

    fake_pika.BlockingConnection = refuse

    for n in range(1, 51):
        now[0] += 0.1  # 5 seconds of traffic, well inside the 30s interval
        sink.emit(_record(n))

    assert len(attempts) == 1, f"{len(attempts)} connect attempts for 50 records"
    assert sink.dropped == 51


def test_the_first_attempt_after_a_failure_is_immediate(fake_pika):
    """A blip must recover at once. Making a caller wait out an interval to recover
    from a dropped connection would trade a real fault for an invented one."""
    now = [1000.0]
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", reconnect_interval=3600.0, clock=lambda: now[0]
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))

    sink.emit(_record(1))  # no clock movement at all

    assert len(fake_pika.connections) == 2, "the blip was not recovered immediately"
    assert sink.published == 1


def test_the_interval_reopens_once_it_has_elapsed(fake_pika):
    now = [1000.0]
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", reconnect_interval=30.0, clock=lambda: now[0]
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))

    attempts = []

    def refuse(parameters):
        attempts.append(now[0])
        raise RuntimeError("connection refused")

    fake_pika.BlockingConnection = refuse

    sink.emit(_record(1))
    now[0] += 10.0
    sink.emit(_record(2))
    now[0] += 25.0  # now past the interval
    sink.emit(_record(3))

    assert len(attempts) == 2, attempts


def test_a_successful_reconnect_clears_the_interval(fake_pika):
    """Otherwise a recovered broker would still be treated as backing off."""
    now = [1000.0]
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", reconnect_interval=30.0, clock=lambda: now[0]
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("reset")
    sink.emit(_record(0))
    sink.emit(_record(1))  # immediate reconnect, succeeds
    assert sink.published == 1

    fake_pika.connections[1].channel().fail_with = RuntimeError("reset again")
    sink.emit(_record(2))  # fails, invalidates
    sink.emit(_record(3))  # must reconnect immediately again, not be gated
    assert sink.published == 2, "the interval outlived the successful reconnect"


# --- FR-022 against the real driver, not only the double (Low #5) ------------


def test_publish_properties_construct_a_real_pika_basicproperties():
    """Every other publish assertion here goes through `FakeProperties`, so nothing
    proved that real pika accepts these kwargs or keeps their values. Skips where
    `pika` is absent — it is in the `dev` extra and nothing else, so an ordinary
    `pip install -e .` still skips, the same rule `tests/conftest.py` holds for the
    real-Postgres suite."""
    pika = pytest.importorskip("pika", reason="pika is dev-only; install '.[dev]'")

    properties = pika.BasicProperties(**publish_properties())

    assert properties.content_type == CONTENT_TYPE
    assert properties.delivery_mode == PERSISTENT_DELIVERY_MODE


def test_a_real_pika_sink_publishes_persistent_json_to_a_channel_double():
    """The default construction path end to end with the genuine driver: no fake
    module in `sys.modules`, a real `BasicProperties`, and a channel double
    standing in only for the broker."""
    pytest.importorskip("pika", reason="pika is dev-only; install '.[dev]'")

    channel = RecordingChannel()
    sink = AMQPSink(channel, routing_key="tokenweir.usage")
    sink.emit(_record(1, input_tokens=120))

    assert len(channel.published) == 1
    published = channel.published[0]
    assert published["routing_key"] == "tokenweir.usage"
    assert UsageRecord.from_json(published["body"]).input_tokens == 120
    assert published["properties"].delivery_mode == PERSISTENT_DELIVERY_MODE
    assert published["properties"].content_type == CONTENT_TYPE


# --- The drop reason has to be the true one (Low #1) -------------------------


def test_backing_off_does_not_claim_the_sink_is_unrecoverable(fake_pika, caplog):
    """A drop logged with the wrong cause is worse than a bare count. This branch
    told an operator the sink "has no channel to publish on and cannot make one"
    while it was merely inside its reconnect interval and about to recover on its
    own — an accurate counter under a false explanation."""
    now = [1000.0]
    sink = AMQPSink.from_url(
        "amqp://guest:guest@rabbit/", reconnect_interval=30.0, clock=lambda: now[0]
    )
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))

    def refuse(parameters):
        raise RuntimeError("connection refused")

    fake_pika.BlockingConnection = refuse
    sink.emit(_record(1))  # the one real dial attempt; fails and starts the backoff
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        now[0] += 1.0
        sink.emit(_record(2))  # inside the window

    messages = [r.getMessage() for r in caplog.records]
    assert messages, "the drop was silent"
    assert not any("cannot make one" in m for m in messages), messages
    assert any("reconnect interval" in m for m in messages), messages


def test_a_sink_that_truly_cannot_reconnect_still_says_so(caplog):
    sink = AMQPSink(None, properties=FakeProperties())
    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        sink.emit(_record(1))
    assert any("cannot make one" in r.getMessage() for r in caplog.records)


def test_one_drop_produces_one_warning_on_the_dial_path(fake_pika, caplog):
    """`_live_channel` logs the dial failure; `emit` must not log a second line for
    the same lost record."""
    sink = AMQPSink.from_url("amqp://guest:guest@rabbit/", warn_interval=0.0)
    fake_pika.connections[0].channel().fail_with = RuntimeError("connection reset")
    sink.emit(_record(0))
    caplog.clear()

    def refuse(parameters):
        raise RuntimeError("connection refused")

    fake_pika.BlockingConnection = refuse
    with caplog.at_level(logging.WARNING, logger="tokenweir.amqp"):
        sink.emit(_record(1))

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "re-establish" in warnings[0]
