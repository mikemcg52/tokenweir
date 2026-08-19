"""The AMQP adapter and the dependency-light core (TOKWEIR-6).

Two of the story's three acceptance clauses live here: *"a service emits via the
client with an AMQP sink"* and *"the core imports without transport libraries"*.

**Everything here runs with no `pika` installed and no broker running**, which is
not a compromise — it is the point. What an adapter test can meaningfully assert
is that the right bytes go to the right exchange with the right properties; that
RabbitMQ works is RabbitMQ's test. The pure mapping is exercised directly, the
publish path against a recording channel, and the two code paths that genuinely
need `pika` (its `BasicProperties`, and `from_url`) against a fake module injected
into `sys.modules` — which exercises the real deferred-import code rather than a
stand-in for it.
"""

import json
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
    sink = AMQPSink(RecordingChannel())
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
    assert AMQPSink(channel).channel is channel


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


def test_publishing_without_pika_is_a_drop_not_a_crash(monkeypatch):
    """A channel implies pika in practice, but a caller who supplies a double must
    still not be able to make the emit path raise."""
    monkeypatch.setitem(sys.modules, "pika", None)
    channel = RecordingChannel()
    sink = AMQPSink(channel)
    sink.emit(_record(1))  # must not raise
    assert channel.published == []
    assert sink.dropped == 1


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
