"""Smoke tests for the tokenweir core seams.

Green baseline for the TOKWEIR-1 extraction: proves the package imports, the
contract round-trips, and the emit/write interfaces behave to contract.

TOKWEIR-4 extends this file with the record's construction invariants (US1),
provider neutrality (US5) and the core's dependency hygiene. Serialization,
pricing modes and the published schema have their own files.
"""

import ast
import sys
from pathlib import Path

import pytest

import tokenweir.contract
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


# --- TOKWEIR-4: construction invariants (US1) --------------------------------


def test_minimal_record_is_valid_with_documented_defaults():
    rec = UsageRecord(
        request_id="r", app_id="a", endpoint="/e", model="m", status="ok"
    )
    assert rec.schema_version == SCHEMA_VERSION
    assert rec.input_tokens == 0
    assert rec.output_tokens == 0
    assert rec.cache_creation_input_tokens == 0
    assert rec.cache_read_input_tokens == 0
    assert rec.workload is None
    assert rec.parent_request_id is None
    assert rec.queue is None
    assert rec.latency_ms is None
    assert rec.pricing_mode is None
    assert rec.ts is None


@pytest.mark.parametrize(
    "name", ["request_id", "app_id", "endpoint", "model", "status"]
)
@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_blank_required_field_is_rejected_naming_the_field(name, blank):
    with pytest.raises(ValueError) as excinfo:
        _record(**{name: blank})
    assert name in str(excinfo.value)


@pytest.mark.parametrize(
    "name", ["request_id", "app_id", "endpoint", "model", "status"]
)
def test_non_string_required_field_is_rejected_naming_the_field(name):
    with pytest.raises(ValueError) as excinfo:
        _record(**{name: None})
    assert name in str(excinfo.value)


@pytest.mark.parametrize(
    "name",
    [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ],
)
def test_negative_token_count_is_rejected(name):
    with pytest.raises(ValueError) as excinfo:
        _record(**{name: -1})
    assert name in str(excinfo.value)


@pytest.mark.parametrize(
    "name",
    [
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    ],
)
@pytest.mark.parametrize("bad", [True, False, 1.5, "10", None])
def test_non_integer_token_count_is_rejected(name, bad):
    # bool is a subclass of int, so True must not slip through as a count of 1.
    with pytest.raises(ValueError) as excinfo:
        _record(**{name: bad})
    assert name in str(excinfo.value)


def test_latency_ms_may_be_unset_but_not_negative_or_non_integer():
    assert _record(latency_ms=None).latency_ms is None
    assert _record(latency_ms=0).latency_ms == 0
    with pytest.raises(ValueError):
        _record(latency_ms=-1)
    with pytest.raises(ValueError):
        _record(latency_ms=True)


@pytest.mark.parametrize("bad", [0, -1, "1", 1.0, True])
def test_invalid_schema_version_is_rejected(bad):
    with pytest.raises(ValueError) as excinfo:
        _record(schema_version=bad)
    assert "schema_version" in str(excinfo.value)


def test_very_large_token_counts_are_accepted():
    # Python integers are unbounded and the contract sets no ceiling.
    huge = 10**18
    assert _record(input_tokens=huge).input_tokens == huge


# --- TOKWEIR-4: provider neutrality (US5) ------------------------------------


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5",
        "llama3.1:70b",
        "gpt-4o-mini",
        "mistral-large-latest",
        "qwen2.5-coder:32b",
    ],
)
def test_model_is_stored_verbatim_for_any_provider(model):
    # ADR-0001 Pillar 3: the core is keyed by model, not locked to Anthropic.
    rec = _record(model=model)
    assert rec.model == model
    assert UsageRecord.from_dict(rec.to_dict()).model == model


def _third_party_imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                imported_roots.add(node.module.split(".")[0])

    return {
        name
        for name in imported_roots
        if name not in sys.stdlib_module_names and name != "tokenweir"
    }


def _core_modules():
    package_dir = Path(tokenweir.contract.__file__).parent
    return sorted(package_dir.glob("*.py"))


@pytest.mark.parametrize(
    "module", _core_modules(), ids=lambda p: p.name
)
def test_core_modules_import_only_the_standard_library(module):
    # ADR-0001 Pillar 2: no transport and no provider SDK may enter the core, so
    # `pip install tokenweir` with no extras pulls in nothing (SC-005). Covers
    # every module in the package, not just contract.py.
    third_party = _third_party_imports(module)
    assert third_party == set(), f"{module.name} must stay stdlib-only; found {third_party}"


def test_core_module_sweep_actually_covers_the_package():
    # Guards the parametrization above: if the glob silently matched nothing, the
    # dependency-hygiene check would vacuously "pass".
    names = {p.name for p in _core_modules()}
    assert {"__init__.py", "contract.py", "sink.py", "source.py"} <= names


# --- TOKWEIR-4: the two error types are a documented distinction --------------


def test_omitting_a_required_argument_raises_type_error():
    # Python's own signature check. The required fields deliberately have no
    # sentinel defaults, so type checkers and IDEs catch this before runtime —
    # documented in the contract module docstring and the README.
    with pytest.raises(TypeError):
        UsageRecord(request_id="r", app_id="a", endpoint="/e", model="m")


def test_invalid_value_raises_value_error_not_type_error():
    with pytest.raises(ValueError):
        _record(app_id="")


@pytest.mark.parametrize("payload", [["a"], "a", 3, None, ("a", "b")])
def test_from_dict_rejects_a_non_mapping_payload(payload):
    # At the wire boundary the caller catches one error type, so a non-mapping
    # must not surface as AttributeError.
    with pytest.raises(ValueError):
        UsageRecord.from_dict(payload)
