"""Serialization fidelity (TOKWEIR-4, US2).

The story's named acceptance: "a record round-trips serialize/deserialize". JSON is
part of the contract, so the round-trip is exercised through an actual JSON string,
not only through a dict.
"""

import json

import pytest

from tokenweir import SCHEMA_VERSION, PricingMode, UsageRecord


def _minimal(**overrides) -> UsageRecord:
    base = dict(
        request_id="req-1",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-sonnet-4",
        status="ok",
    )
    base.update(overrides)
    return UsageRecord(**base)


def _fully_populated() -> UsageRecord:
    """Every field set — nothing left on its default, so loss anywhere shows up."""
    return UsageRecord(
        request_id="req-42",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-opus-5",
        status="ok",
        workload="review",
        parent_request_id="req-41",
        queue="usage",
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=90,
        cache_read_input_tokens=17,
        latency_ms=1234,
        pricing_mode=PricingMode.SUBSCRIPTION,
        ts="2026-08-09T12:00:00Z",
        schema_version=SCHEMA_VERSION,
    )


def test_fully_populated_record_round_trips_through_json():
    original = _fully_populated()
    restored = UsageRecord.from_json(original.to_json())
    assert restored == original
    assert restored.pricing_mode is PricingMode.SUBSCRIPTION


def test_fully_populated_record_round_trips_through_dict():
    original = _fully_populated()
    assert UsageRecord.from_dict(original.to_dict()) == original


def test_unset_optionals_survive_as_null_and_rebuild_unset():
    original = _minimal()
    payload = json.loads(original.to_json())

    for name in ("workload", "parent_request_id", "queue", "latency_ms", "pricing_mode", "ts"):
        assert payload[name] is None, f"{name} should serialize as null when unset"

    assert UsageRecord.from_json(original.to_json()) == original


def test_to_json_emits_a_json_object_with_the_contracted_field_names():
    payload = json.loads(_fully_populated().to_json())
    assert payload["model"] == "claude-opus-5"
    assert payload["pricing_mode"] == "subscription"
    assert payload["schema_version"] == SCHEMA_VERSION


@pytest.mark.parametrize("wrap", [lambda b: b, bytearray], ids=["bytes", "bytearray"])
def test_from_json_accepts_bytes_like_payloads(wrap):
    # str, bytes and bytearray — exactly what json.loads itself accepts, so the
    # guard neither narrows nor widens the standard library's contract.
    original = _minimal()
    encoded = original.to_json().encode("utf-8")
    assert UsageRecord.from_json(wrap(encoded)) == original


def test_unknown_fields_from_a_newer_producer_are_ignored():
    payload = _minimal().to_dict()
    payload["ephemeral_5m_input_tokens"] = 5
    payload["some_future_field"] = "ignored"

    rec = UsageRecord.from_dict(payload)
    assert rec.request_id == "req-1"
    assert not hasattr(rec, "some_future_field")


def test_deserialization_preserves_the_payloads_schema_version():
    # A consumer must be able to tell what it actually received, so the reading
    # library must not overwrite the version with its own.
    payload = _minimal().to_dict()
    payload["schema_version"] = SCHEMA_VERSION + 1

    assert UsageRecord.from_dict(payload).schema_version == SCHEMA_VERSION + 1


def test_schema_version_defaults_when_the_payload_omits_it():
    payload = _minimal().to_dict()
    del payload["schema_version"]

    assert UsageRecord.from_dict(payload).schema_version == SCHEMA_VERSION


@pytest.mark.parametrize(
    "missing", ["request_id", "app_id", "endpoint", "model", "status"]
)
def test_payload_missing_a_required_field_is_rejected_naming_it(missing):
    payload = _minimal().to_dict()
    del payload[missing]

    with pytest.raises(ValueError) as excinfo:
        UsageRecord.from_dict(payload)

    assert missing in str(excinfo.value)


def test_from_json_rejects_a_payload_that_is_not_an_object():
    with pytest.raises(ValueError):
        UsageRecord.from_json("[]")


def test_from_json_rejects_malformed_json():
    # json.JSONDecodeError is a ValueError, so callers need only catch one type.
    with pytest.raises(ValueError):
        UsageRecord.from_json("{not json")
