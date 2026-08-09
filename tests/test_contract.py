"""Smoke tests for the tokenweir core seams.

Green baseline for the TOKWEIR-1 extraction: proves the package imports, the
contract round-trips, and the emit/write interfaces behave to contract.
"""

from tokenweir import (
    SCHEMA_VERSION,
    MemorySource,
    NullSink,
    Sink,
    Source,
    UsageRecord,
)


def _record(**overrides) -> UsageRecord:
    base = dict(
        request_id="req-1",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-sonnet-4",
        status="ok",
        input_tokens=10,
        output_tokens=20,
    )
    base.update(overrides)
    return UsageRecord(**base)


def test_record_roundtrips_through_dict():
    rec = _record(workload="review", latency_ms=1234)
    restored = UsageRecord.from_dict(rec.to_dict())
    assert restored == rec
    assert restored.schema_version == SCHEMA_VERSION


def test_from_dict_ignores_unknown_keys():
    payload = _record().to_dict()
    payload["some_future_field"] = "ignored"
    rec = UsageRecord.from_dict(payload)
    assert rec.request_id == "req-1"


def test_raw_token_counts_default_to_zero():
    rec = UsageRecord(
        request_id="r", app_id="a", endpoint="/e", model="m", status="ok"
    )
    assert rec.cache_read_input_tokens == 0
    assert rec.output_tokens == 0


def test_null_sink_never_raises_and_satisfies_protocol():
    sink = NullSink()
    assert isinstance(sink, Sink)
    sink.emit(_record())  # must not raise
    sink.close()


def test_memory_source_persists_and_satisfies_protocol():
    src = MemorySource()
    assert isinstance(src, Source)
    written = src.write([_record(request_id="a"), _record(request_id="b")])
    assert written == 2
    assert [r.request_id for r in src.records] == ["a", "b"]
