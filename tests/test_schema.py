"""The published JSON Schema (TOKWEIR-4, US4).

The story's acceptance opens with "contract published as a typed, versioned schema".
Python consumers get the dataclass; everyone else gets this document. The last test
is the drift guard: the checked-in artifact cannot silently fall out of step with the
code that generates it.
"""

import json
from dataclasses import fields
from pathlib import Path

from tokenweir import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    PricingMode,
    UsageRecord,
    usage_record_json_schema,
)

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schema" / "usage-record.v1.json"


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
