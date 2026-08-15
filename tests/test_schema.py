"""The published JSON Schema (TOKWEIR-4, US4).

The story's acceptance opens with "contract published as a typed, versioned schema".
Python consumers get the dataclass; everyone else gets this document. The last test
is the drift guard: the checked-in artifact cannot silently fall out of step with the
code that generates it.
"""

import json
import re
import shutil
import subprocess
from dataclasses import fields
from pathlib import Path

import pytest

from tokenweir import (
    REQUIRED_FIELDS,
    SCHEMA_VERSION,
    PricingMode,
    UsageRecord,
    usage_record_json_schema,
)
from tokenweir.contract import (
    _NON_BLANK_PATTERN,
    _OPTIONAL_STR_FIELDS,
    TOKEN_COUNT_FIELDS,
)

SCHEMA_FILE = Path(__file__).resolve().parents[1] / "schema" / "usage-record.v1.json"


def _payload(**overrides) -> dict:
    """A fully-populated payload — every optional field set, so a validation
    happy-path test actually exercises them rather than only their nulls."""
    base = UsageRecord(
        request_id="req-1",
        app_id="mado",
        endpoint="/v1/messages",
        model="claude-sonnet-4",
        status="ok",
        workload="review",
        parent_request_id="req-0",
        queue="usage",
        input_tokens=1200,
        output_tokens=340,
        cache_creation_input_tokens=90,
        cache_read_input_tokens=17,
        latency_ms=1234,
        pricing_mode=PricingMode.API_METERED,
        ts="2026-08-09T12:00:00Z",
    ).to_dict()
    base.update(overrides)
    return base


def _minimal_payload(**overrides) -> dict:
    """The other extreme: every optional left unset, so the nulls are covered too."""
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
    # schema_version is required on the wire even though from_dict defaults it:
    # the published document may be stricter than the reader, never looser.
    assert usage_record_json_schema()["required"] == [
        "schema_version",
        *REQUIRED_FIELDS,
    ]


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
    assert prop["pattern"] == _NON_BLANK_PATTERN


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
        {"app_id": "   "},  # whitespace-only is blank in both the schema and the library
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
    # generator or any sibling property within the same document. Covers the
    # list-valued members too, not just the nested dicts.
    first = usage_record_json_schema()
    first["properties"]["workload"]["description"] = "POISONED"
    first["required"].append("POISONED")
    first["properties"]["pricing_mode"]["enum"].append("POISONED")
    first["properties"]["input_tokens"]["minimum"] = -99

    second = usage_record_json_schema()
    assert "description" not in second["properties"]["workload"]
    assert "POISONED" not in second["required"]
    assert "POISONED" not in second["properties"]["pricing_mode"]["enum"]
    assert second["properties"]["input_tokens"]["minimum"] == 0

    # ...and no two properties within one document share a fragment.
    assert "description" not in first["properties"]["queue"]
    assert first["properties"]["output_tokens"]["minimum"] == 0


# --- Numeric agreement: JSON has one number type -----------------------------
#
# JSON Schema's "type": "integer" accepts any number with zero fractional part,
# so a JavaScript producer writing 100.0 is emitting the integer 100. The library
# must read it, or the schema would accept records this library refuses (FR-019).
# These are dependency-free on purpose: FR-019's substance stays verified in CI,
# where the jsonschema-backed tests above are skipped.


@pytest.mark.parametrize("name", [*TOKEN_COUNT_FIELDS, "latency_ms", "schema_version"])
def test_integral_json_floats_are_accepted_and_normalized_to_int(name):
    value = 1.0 if name == "schema_version" else 12.0
    rec = UsageRecord.from_dict(_payload(**{name: value}))

    read_back = getattr(rec, name)
    assert read_back == int(value)
    assert type(read_back) is int, f"{name} must normalize to int, got {type(read_back)}"


@pytest.mark.parametrize("name", [*TOKEN_COUNT_FIELDS, "latency_ms"])
def test_integral_json_floats_survive_a_json_round_trip(name):
    payload = json.dumps(_payload(**{name: 7.0}))
    assert getattr(UsageRecord.from_json(payload), name) == 7


@pytest.mark.parametrize("name", [*TOKEN_COUNT_FIELDS, "latency_ms"])
@pytest.mark.parametrize("bad", [12.5, -0.5, float("nan"), float("inf")])
def test_non_integral_numbers_are_still_rejected(name, bad):
    # A genuinely fractional count is a producer bug - normalize the integral
    # case, never silently truncate the fractional one.
    with pytest.raises(ValueError):
        UsageRecord.from_dict(_payload(**{name: bad}))


@pytest.mark.parametrize("name", [*TOKEN_COUNT_FIELDS, "latency_ms"])
def test_direct_construction_still_requires_a_real_int(name):
    # The wire tolerance is a deserialization concern. In Python, 12.0 where an
    # int belongs is a caller-side type error and stays loud.
    with pytest.raises(ValueError):
        UsageRecord(
            request_id="r", app_id="a", endpoint="/e", model="m", status="ok",
            **{name: 12.0},
        )


# --- The blank rule is engine-independent ------------------------------------


@pytest.mark.parametrize(
    "char, is_blank",
    [
        ("\u00a0", True),   # NBSP - whitespace to both Python and ECMA-262
        ("\u2028", True),   # LINE SEPARATOR
        ("\u3000", True),   # IDEOGRAPHIC SPACE
        ("\ufeff", True),   # BOM - ECMA-262 whitespace, but NOT Python's
        ("\u001c", True),   # FILE SEPARATOR - Python whitespace, but NOT ECMA's
        ("\u0085", True),   # NEL - Python whitespace, but NOT ECMA's
        ("x", False),
        ("\u200b", False),  # ZERO WIDTH SPACE - blank to neither
    ],
)
def test_blankness_is_the_union_of_python_and_ecma_whitespace(char, is_blank):
    """The library and the published schema must agree, character for character.

    The listed code points are exactly where Python and ECMA-262 disagree about
    whitespace, which is why blankness is decided by an explicit shared class
    rather than by `str.strip()` on one side and `\\S` on the other. The class is
    the *union*, so adopting a shared rule never made the library less strict:
    U+001C-U+001F and U+0085 stay blank, and U+FEFF becomes blank too.
    """
    schema_says_blank = not re.search(_NON_BLANK_PATTERN, char)
    assert schema_says_blank is is_blank

    try:
        UsageRecord(
            request_id="r", app_id=char, endpoint="/e", model="m", status="ok"
        )
        library_says_blank = False
    except ValueError:
        library_says_blank = True

    assert library_says_blank is is_blank


# --- The other direction: the library must not emit schema-invalid records ----
#
# FR-019 guards schema -> library (a conforming producer is never refused).
# This guards library -> schema: a record this library accepts must serialize to
# a document its own published schema validates. A Go or JS consumer validating
# incoming records would otherwise reject what the reference producer emits.


@pytest.mark.parametrize("name", _OPTIONAL_STR_FIELDS)
@pytest.mark.parametrize("bad", [123, 3.5, True, ["x"], {"a": 1}, object()])
def test_optional_string_fields_reject_non_strings(name, bad):
    with pytest.raises(ValueError) as excinfo:
        UsageRecord(
            request_id="r", app_id="a", endpoint="/e", model="m", status="ok",
            **{name: bad},
        )
    assert name in str(excinfo.value)


@pytest.mark.parametrize("name", _OPTIONAL_STR_FIELDS)
def test_optional_string_fields_accept_a_string_or_none(name):
    for value in ("x", "", None):
        rec = UsageRecord(
            request_id="r", app_id="a", endpoint="/e", model="m", status="ok",
            **{name: value},
        )
        assert getattr(rec, name) == value


def test_schema_types_exactly_the_optional_string_fields_as_nullable_strings():
    props = usage_record_json_schema()["properties"]
    # pricing_mode is also a nullable string, but an enum-constrained one — it is
    # validated by PricingMode, not by the plain optional-string rule.
    nullable = {
        name
        for name, prop in props.items()
        if prop.get("type") == ["string", "null"] and "enum" not in prop
    }
    assert nullable == set(_OPTIONAL_STR_FIELDS)


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"workload": "review"},
        {"workload": ""},
        {"ts": "2026-08-09T12:00:00Z"},
        {"pricing_mode": PricingMode.API_METERED},
        {"pricing_mode": PricingMode.SUBSCRIPTION},
        {"input_tokens": 0, "output_tokens": 10**18},
        {"latency_ms": 0},
        {"model": "llama3.1:70b"},
        {"status": "error"},
        {"queue": "usage", "parent_request_id": "req-0"},
    ],
)
def test_any_record_the_library_accepts_serializes_to_schema_valid_json(kwargs):
    jsonschema = _validator()
    fields = {
        "request_id": "r", "app_id": "a", "endpoint": "/e",
        "model": "m", "status": "ok",
    }
    fields.update(kwargs)
    rec = UsageRecord(**fields)
    jsonschema.validate(
        instance=json.loads(rec.to_json()), schema=usage_record_json_schema()
    )


def test_minimal_and_full_payloads_both_validate():
    jsonschema = _validator()
    schema = usage_record_json_schema()
    jsonschema.validate(instance=_minimal_payload(), schema=schema)
    jsonschema.validate(instance=_payload(), schema=schema)


# --- FR-018: SCHEMA_VERSION must move when the field set moves ----------------


def test_schema_version_is_pinned_to_the_field_set():
    """Adding or removing a field must force a deliberate SCHEMA_VERSION decision.

    The drift guard already forces the published artifact to be regenerated, but
    nothing forced the *version* to be reconsidered. This fingerprint does: change
    the record's fields and this test fails until someone updates it and decides
    whether the version bumps.
    """
    from dataclasses import fields as dataclass_fields

    fingerprint = sorted(f.name for f in dataclass_fields(UsageRecord))
    assert fingerprint == sorted(
        [
            "schema_version",
            "request_id",
            "parent_request_id",
            "app_id",
            "workload",
            "endpoint",
            "model",
            "queue",
            "status",
            "latency_ms",
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
            "pricing_mode",
            "ts",
        ]
    ), "record fields changed - bump SCHEMA_VERSION (or justify not doing so)"
    assert SCHEMA_VERSION == 1


# --- FR-024: pin the shared pattern, and check it under a real ECMA engine ----


EXPECTED_NON_BLANK_PATTERN = (
    r"[^\t\n\v\f\r\u001c-\u001f \u0085\u00a0\u1680"
    r"\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
)


def test_non_blank_pattern_is_pinned_to_its_exact_literal():
    """The published pattern is part of the wire contract — pin it, don't infer it.

    Without this, swapping the explicit class back to a shorthand like `[^\\s]`
    passes every other test in the suite (both sides would be evaluated by Python's
    engine) while silently telling JavaScript and Go consumers that U+001C and
    U+0085 are acceptable values, which this library refuses.
    """
    assert _NON_BLANK_PATTERN == EXPECTED_NON_BLANK_PATTERN


@pytest.mark.parametrize(
    "shorthand", [r"\s", r"\S", r"\w", r"\W", r"\d", r"\D", r"\p{", r"\P{", "[:"]
)
def test_non_blank_pattern_uses_no_shorthand_character_class(shorthand):
    # Shorthands are the failure mode: their meaning differs between Python and
    # ECMA-262, which is the whole reason the class is spelled out.
    assert shorthand not in _NON_BLANK_PATTERN


def test_published_schema_carries_the_pinned_pattern():
    props = usage_record_json_schema()["properties"]
    for name in REQUIRED_FIELDS:
        assert props[name]["pattern"] == EXPECTED_NON_BLANK_PATTERN


_NODE = shutil.which("node")


@pytest.mark.skipif(_NODE is None, reason="needs a node runtime for ECMA-262 semantics")
def test_pattern_means_the_same_thing_under_ecma_262():
    """FR-024's actual claim: a JS/Go validator and this library agree.

    Every other blankness test evaluates the pattern with Python's `re` on both
    sides, so it cannot detect a Python-vs-ECMA divergence. This one runs the
    published pattern through V8.
    """
    code_points = list(range(0, 0x2100)) + [
        0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0x3001, 0xFEFE, 0xFEFF, 0xFF00, 0x1F600
    ]

    script = (
        "const re = new RegExp(process.argv[1]);"
        "const cps = JSON.parse(process.argv[2]);"
        "const blank = cps.filter(cp => !re.test(String.fromCodePoint(cp)));"
        "console.log(JSON.stringify(blank));"
    )
    result = subprocess.run(
        [_NODE, "-e", script, _NON_BLANK_PATTERN, json.dumps(code_points)],
        capture_output=True, text=True, check=True,
    )
    ecma_blank = set(json.loads(result.stdout))
    python_blank = {cp for cp in code_points if not re.search(_NON_BLANK_PATTERN, chr(cp))}

    assert ecma_blank == python_blank, (
        "published pattern means different things to Python and ECMA-262 at: "
        f"{sorted(hex(cp) for cp in ecma_blank ^ python_blank)}"
    )

    # And it is the set the library actually enforces.
    library_blank = set()
    for cp in code_points:
        try:
            UsageRecord(
                request_id="r", app_id=chr(cp), endpoint="/e", model="m", status="ok"
            )
        except ValueError:
            library_blank.add(cp)
    assert library_blank == ecma_blank


# --- FR-025, without a JSON Schema engine ------------------------------------
#
# The tests above that validate real payloads are jsonschema-gated, so they skip
# under the MADO stream plan's install. These pin the *declaration* itself, so a
# schema-side regression — dropping "null" from a nullable type, adding a stray
# maxLength — fails in the environment that actually gates the merge.


_NULLABLE_STRING = {"type": ["string", "null"]}
_REQUIRED_STRING = {
    "type": "string",
    "minLength": 1,
    "pattern": EXPECTED_NON_BLANK_PATTERN,
}
_TOKEN_COUNT = {"type": "integer", "minimum": 0, "default": 0}

EXPECTED_CONSTRAINTS = {
    "schema_version": {"type": "integer", "const": SCHEMA_VERSION},
    "request_id": _REQUIRED_STRING,
    "app_id": _REQUIRED_STRING,
    "endpoint": _REQUIRED_STRING,
    "model": _REQUIRED_STRING,
    "status": _REQUIRED_STRING,
    "workload": _NULLABLE_STRING,
    "parent_request_id": _NULLABLE_STRING,
    "queue": _NULLABLE_STRING,
    "input_tokens": _TOKEN_COUNT,
    "output_tokens": _TOKEN_COUNT,
    "cache_creation_input_tokens": _TOKEN_COUNT,
    "cache_read_input_tokens": _TOKEN_COUNT,
    "latency_ms": {"type": ["integer", "null"], "minimum": 0},
    "pricing_mode": {
        "type": ["string", "null"],
        "enum": ["api_metered", "subscription", None],
    },
    "ts": _NULLABLE_STRING,
}


def test_schema_declares_exactly_the_constraints_the_library_enforces():
    """Pin every declared constraint, so no engine is needed to catch a drift.

    `description` is excluded — prose may change freely; constraints may not.
    """
    props = usage_record_json_schema()["properties"]
    actual = {
        name: {k: v for k, v in prop.items() if k != "description"}
        for name, prop in props.items()
    }
    assert actual == EXPECTED_CONSTRAINTS


@pytest.mark.parametrize("name", sorted(EXPECTED_CONSTRAINTS))
def test_schema_permits_null_exactly_where_the_library_permits_none(name):
    """The FR-025 rule stated directly, rather than as a literal pin.

    If the library accepts `None` for a field, the schema must allow `null` for
    it — otherwise a record the library builds fails its own published schema.
    And the converse: allowing `null` for a field the library requires would let
    a conforming producer emit a record the library refuses (FR-019).
    """
    declared = usage_record_json_schema()["properties"][name]["type"]
    schema_allows_null = "null" in (
        declared if isinstance(declared, list) else [declared]
    )

    fields = {
        "request_id": "r", "app_id": "a", "endpoint": "/e",
        "model": "m", "status": "ok",
    }
    fields[name] = None
    try:
        UsageRecord(**fields)
        library_allows_none = True
    except ValueError:
        library_allows_none = False

    assert schema_allows_null == library_allows_none, (
        f"{name}: schema null={schema_allows_null} but library None="
        f"{library_allows_none}"
    )


def test_required_string_properties_carry_no_extra_constraints():
    # A stray maxLength/format would reject values the library accepts (FR-025)
    # without any engine-backed test noticing under the authoritative install.
    props = usage_record_json_schema()["properties"]
    for name in REQUIRED_FIELDS:
        keys = set(props[name]) - {"description"}
        assert keys == {"type", "minLength", "pattern"}, (
            f"{name} declares unexpected constraint(s): {sorted(keys)}"
        )
