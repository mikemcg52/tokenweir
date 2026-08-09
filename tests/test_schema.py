"""The published JSON Schema (TOKWEIR-4, US4).

The story's acceptance opens with "contract published as a typed, versioned schema".
Python consumers get the dataclass; everyone else gets this document. The last test
is the drift guard: the checked-in artifact cannot silently fall out of step with the
code that generates it.
"""

import json
import re
from dataclasses import fields
from pathlib import Path

import pytest

from tokenweir import (
    NON_BLANK_PATTERN,
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    PricingMode,
    UsageRecord,
    usage_record_json_schema,
)

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schema" / "usage-record.v1.json"


def _payload(**overrides) -> dict:
    base = UsageRecord(
        request_id="req-1",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-sonnet-4",
        status="ok",
    ).to_dict()
    base.update(overrides)
    return base


def test_schema_describes_exactly_the_records_fields():
    schema = usage_record_json_schema()
    assert set(schema["properties"]) == {f.name for f in fields(UsageRecord)}


def test_schema_marks_the_required_fields_required():
    assert usage_record_json_schema()["required"] == list(REQUIRED_FIELDS)


def test_schema_states_the_contract_version():
    schema = usage_record_json_schema()
    assert schema["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert f"v{SCHEMA_VERSION}" in schema["$id"]


def test_schema_enumerates_the_pricing_modes():
    enum_values = usage_record_json_schema()["properties"]["pricing_mode"]["enum"]
    assert set(enum_values) == {"api_metered", "subscription", None}
    assert {m.value for m in PricingMode} == {"api_metered", "subscription"}


def test_schema_permits_unknown_properties_for_forward_compatibility():
    assert usage_record_json_schema()["additionalProperties"] is True


def test_token_counts_are_non_negative_integers_in_the_schema():
    props = usage_record_json_schema()["properties"]
    for name in (
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ):
        assert props[name]["type"] == "integer"
        assert props[name]["minimum"] == 0


def test_checked_in_schema_file_matches_the_generated_schema():
    """Drift guard — regenerate schema/usage-record.v1.json when the record changes."""
    assert SCHEMA_FILE.exists(), f"missing published schema artifact: {SCHEMA_FILE}"

    expected = usage_record_json_schema()
    on_disk = json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))
    assert on_disk == expected, (
        "schema/usage-record.v1.json is out of date; regenerate it from "
        "tokenweir.contract.usage_record_json_schema()"
    )

    # Canonical formatting, so regeneration is deterministic and diffs stay readable.
    assert SCHEMA_FILE.read_text(encoding="utf-8") == json.dumps(expected, indent=2) + "\n"


# --- The schema must agree with the library about what "blank" means ---------
#
# A non-Python producer follows the published schema. If the schema accepted a
# value the library refuses, that producer could emit records this consumer drops.


@pytest.mark.parametrize("name", REQUIRED_FIELDS)
def test_required_string_properties_reject_blank_in_the_schema(name):
    prop = usage_record_json_schema()["properties"][name]
    assert prop["type"] == "string"
    assert prop["minLength"] == 1
    assert prop["pattern"] == NON_BLANK_PATTERN


@pytest.mark.parametrize("name", REQUIRED_FIELDS)
@pytest.mark.parametrize(
    "value, acceptable",
    [("ok", True), ("a b", True), (" padded ", True), ("", False), ("   ", False), ("\t\n", False)],
)
def test_schema_pattern_matches_the_librarys_blank_rule(name, value, acceptable):
    """The schema's `pattern` and `_validate_required_str` must agree exactly."""
    prop = usage_record_json_schema()["properties"][name]

    # JSON Schema `pattern` is an unanchored search, so re.search is the
    # faithful evaluation of it.
    schema_accepts = bool(re.search(prop["pattern"], value)) and len(value) >= prop["minLength"]

    try:
        UsageRecord(**{**{f: "x" for f in REQUIRED_FIELDS}, name: value})
        library_accepts = True
    except ValueError:
        library_accepts = False

    assert schema_accepts == library_accepts == acceptable


# --- Real validation, when a JSON Schema engine is available -----------------
#
# `jsonschema` is a [dev] extra, not a runtime dependency — the core stays
# dependency-light (ADR-0001 Pillar 2). These skip on the MADO stream plan's
# install (`pip install -e . pytest`), which deliberately omits dev extras.


def _validator():
    jsonschema = pytest.importorskip(
        "jsonschema", reason="JSON Schema engine is a [dev] extra"
    )
    return jsonschema


def test_a_well_formed_record_validates_against_the_published_schema():
    jsonschema = _validator()
    jsonschema.validate(instance=_payload(), schema=usage_record_json_schema())


@pytest.mark.parametrize(
    "bad",
    [
        {"app_id": "   "},          # whitespace-only — the Med-2 divergence
        {"app_id": ""},
        {"request_id": None},
        {"input_tokens": -1},
        {"latency_ms": -5},
        {"pricing_mode": "free_tier"},
    ],
)
def test_invalid_payloads_are_rejected_by_the_published_schema(bad):
    jsonschema = _validator()
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=_payload(**bad), schema=usage_record_json_schema())


def test_missing_required_field_is_rejected_by_the_published_schema():
    jsonschema = _validator()
    payload = _payload()
    del payload["app_id"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=payload, schema=usage_record_json_schema())


def test_unknown_properties_still_validate():
    # Forward compatibility: a v1 record that picked up an unknown field is valid.
    jsonschema = _validator()
    jsonschema.validate(
        instance=_payload(some_future_field="ignored"),
        schema=usage_record_json_schema(),
    )


def test_v1_schema_is_a_strict_v1_validator_by_design():
    """`const` is intentional: a v2 record is described by its own schema file.

    This is deliberately stricter than `from_dict`, which accepts and preserves a
    newer version so a Python consumer can decide for itself. Both behaviours are
    intended; this test pins the schema half so the divergence stays a decision
    rather than an accident.
    """
    jsonschema = _validator()
    newer = _payload(schema_version=SCHEMA_VERSION + 1)

    # The Python contract reads it happily...
    assert UsageRecord.from_dict(newer).schema_version == SCHEMA_VERSION + 1

    # ...while the v1 schema declines to call it a v1 record.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(instance=newer, schema=usage_record_json_schema())


def test_generated_schema_shares_no_state_between_calls():
    # Callers may annotate the returned document; doing so must not corrupt the
    # generator or any sibling property within the same document.
    first = usage_record_json_schema()
    first["properties"]["workload"]["description"] = "POISONED"

    second = usage_record_json_schema()
    assert "description" not in second["properties"]["workload"]
    assert "description" not in first["properties"]["queue"]
