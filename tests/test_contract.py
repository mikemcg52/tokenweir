"""Smoke tests for the tokenweir core seams.

Green baseline for the TOKWEIR-1 extraction: proves the package imports, the
contract round-trips, and the emit/write interfaces behave to contract.

TOKWEIR-4 extends this file with the record's construction invariants (US1),
provider neutrality (US5) and the core's dependency hygiene. Serialization,
pricing modes and the published schema have their own files.
"""

import ast
import dataclasses
import json
import re
import sys
import tomllib
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
        # Mixed case and surrounding whitespace: a lower()/strip() sneaking into
        # the core would silently rewrite attribution data, and every all-lowercase
        # identifier above would fail to notice.
        "Meta-Llama-3.1-70B-Instruct",
        "ft:gpt-4o-2024-08-06:acme::AbC123",
        "  spaced-model  ",
        "MiXeD-CaSe-Model",
        "Qwen/Qwen2.5-Coder-32B-Instruct",
    ],
)
def test_model_is_stored_verbatim_for_any_provider(model):
    # ADR-0001 Pillar 3: the core is keyed by model, not locked to Anthropic, and
    # the identifier is opaque — no normalization, prefixing or inference.
    rec = _record(model=model)
    assert rec.model == model
    assert UsageRecord.from_dict(rec.to_dict()).model == model
    assert json.loads(rec.to_json())["model"] == model


@pytest.mark.parametrize(
    "name", ["request_id", "app_id", "endpoint", "model", "status", "workload"]
)
@pytest.mark.parametrize(
    "value",
    [
        "MiXeD-CaSe",
        "  padded  ",
        "UPPER",
        "trailing ",
        " leading",
        "Has Spaces Inside",
    ],
)
def test_string_fields_are_never_normalized(name, value):
    # Attribution data is stored as given. A stray .lower()/.strip() anywhere in
    # __post_init__ would corrupt it silently, and the round trip would hide it.
    rec = _record(**{name: value})
    assert getattr(rec, name) == value
    assert json.loads(rec.to_json())[name] == value
    assert getattr(UsageRecord.from_json(rec.to_json()), name) == value


def _imported_roots(nodes) -> set:
    roots = set()
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                roots.add(node.module.split(".")[0])
    return {
        name for name in roots if name not in sys.stdlib_module_names and name != "tokenweir"
    }


def _deferred_nodes(tree):
    """Nodes inside a function body — they run when called, not when imported."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for descendant in ast.walk(node):
                if descendant is not node:
                    yield descendant


def _import_time_third_party_imports(path: Path) -> set:
    """What a bare `pip install tokenweir` would have to satisfy.

    Everything except function bodies: a class body and a top-level `if`/`try` do
    execute at import time, so their imports count.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deferred = {id(node) for node in _deferred_nodes(tree)}
    return _imported_roots(node for node in ast.walk(tree) if id(node) not in deferred)


def _deferred_third_party_imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return _imported_roots(_deferred_nodes(tree))


def _optional_extra_distributions() -> set:
    """Import-name roots the project declares as **optional** extras.

    `dev` is excluded on purpose: a test-only dependency has no business being
    imported by library code at all, deferred or otherwise.
    """
    # From this test file, not from the package: an editable install and a real
    # one put the package in different places relative to the project root, and
    # only the test file is reliably beside it.
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pyproject.is_file():
        pytest.skip("no pyproject.toml (e.g. an installed distribution)")
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    extras = config["project"]["optional-dependencies"]
    return {
        # "psycopg[binary]>=3" -> "psycopg". Crude, and adequate: these are this
        # project's own extras, not arbitrary requirement strings.
        re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip().replace("-", "_")
        for name, requirements in extras.items()
        if name != "dev"
        for requirement in requirements
    }


def _core_modules():
    # rglob, not glob: since TOKWEIR-5 the package has a subpackage
    # (`tokenweir.migrations`), and a top-level-only sweep would have exempted
    # exactly the new code most likely to reach for a database driver.
    package_dir = Path(tokenweir.contract.__file__).parent
    return sorted(package_dir.rglob("*.py"))


def _module_id(path: Path) -> str:
    # Relative to the package root, so `migrations/__init__.py` is distinguishable
    # from the package's own `__init__.py` in the test id.
    return str(path.relative_to(Path(tokenweir.contract.__file__).parent))


@pytest.mark.parametrize(
    "module", _core_modules(), ids=_module_id
)
def test_core_modules_import_only_the_standard_library(module):
    # ADR-0001 Pillar 2: no transport and no provider SDK may enter the core, so
    # `pip install tokenweir` with no extras pulls in nothing (SC-005). Covers
    # every module in the package, not just contract.py.
    #
    # Scoped to import time since TOKWEIR-5. A driver imported *inside the one
    # function that connects* costs a bare install nothing, and that deferral is
    # how an optional extra is supposed to be reached — `tokenweir.migrations`
    # does it for psycopg and the AMQP adapter will do it for pika. The rule that
    # actually matters is the one below: a deferred import must correspond to a
    # declared extra, so "deferred" cannot become a way to smuggle in a hard
    # dependency that merely fails later.
    third_party = _import_time_third_party_imports(module)
    assert third_party == set(), f"{module.name} must stay stdlib-only; found {third_party}"


@pytest.mark.parametrize("module", _core_modules(), ids=_module_id)
def test_a_deferred_import_must_be_a_declared_optional_extra(module):
    deferred = _deferred_third_party_imports(module)
    declared = _optional_extra_distributions()
    assert deferred <= declared, (
        f"{module.name} defers an import of {sorted(deferred - declared)}, which is "
        "not declared as an optional extra in pyproject.toml — a deferred import "
        "still has to be installable by someone, and an undeclared one is a hard "
        "dependency that fails at the call instead of at install"
    )


def test_the_deferred_import_check_is_not_vacuous():
    # `tokenweir.migrations.connect` imports psycopg inside the function. If that
    # ever stops being true — or the detector stops seeing it — the check above
    # passes on every module for the wrong reason.
    package_dir = Path(tokenweir.contract.__file__).parent
    assert _deferred_third_party_imports(package_dir / "migrations" / "__init__.py") == {
        "psycopg"
    }
    assert "psycopg" in _optional_extra_distributions()


def test_core_module_sweep_actually_covers_the_package():
    # Guards the parametrization above: if the glob silently matched nothing, the
    # dependency-hygiene check would vacuously "pass".
    names = {_module_id(p) for p in _core_modules()}
    assert {
        "__init__.py",
        "contract.py",
        "sink.py",
        "source.py",
        # The store, added by TOKWEIR-5. Listed explicitly because these are the
        # modules whose whole job is to talk to Postgres — they import the driver
        # inside the one function that connects, and this sweep is what keeps it
        # that way.
        "postgres.py",
        str(Path("migrations") / "__init__.py"),
        str(Path("migrations") / "__main__.py"),
        # The emit side, added by TOKWEIR-6. `amqp.py` is here for the same reason
        # `postgres.py` is: its whole job is to talk to a transport, it defers the
        # driver import into the one place that needs it, and this sweep is what
        # keeps it that way. `emitter.py` and `_ratelimit.py` are listed because a
        # glob that silently stopped matching them would make the stdlib-only
        # assertion vacuous for the newest code in the package, which is exactly
        # the code most likely to reach for something.
        "emitter.py",
        "amqp.py",
        "_ratelimit.py",
    } <= names


# --- TOKWEIR-4: the two error types are a documented distinction --------------


def test_omitting_a_required_argument_raises_type_error():
    # Python's own signature check. The required fields deliberately have no
    # sentinel defaults, so type checkers and IDEs catch this before runtime —
    # documented in the contract module docstring and the README.
    with pytest.raises(TypeError) as excinfo:
        UsageRecord(request_id="r", app_id="a", endpoint="/e", model="m")
    # Python's own message names the missing argument, which is the "clear error
    # naming the offending field" the spec asks for.
    assert "status" in str(excinfo.value)


def test_invalid_value_raises_value_error_not_type_error():
    with pytest.raises(ValueError):
        _record(app_id="")


@pytest.mark.parametrize("payload", [["a"], "a", 3, None, ("a", "b")])
def test_from_dict_rejects_a_non_mapping_payload(payload):
    # At the wire boundary the caller catches one error type, so a non-mapping
    # must not surface as AttributeError.
    with pytest.raises(ValueError):
        UsageRecord.from_dict(payload)


# --- TOKWEIR-4 fix round 2: the record is a value, not a mutable buffer -------


def test_record_is_immutable():
    # Validation runs once at construction; if fields could be reassigned, a
    # validated record could be mutated into one that breaks its own contract
    # and then serialized.
    rec = _record()
    with pytest.raises(Exception) as excinfo:
        rec.input_tokens = -5
    assert excinfo.type.__name__ in {"FrozenInstanceError", "AttributeError"}
    assert rec.input_tokens == 10


def test_dataclasses_replace_builds_a_validated_variant():
    rec = _record()
    stamped = dataclasses.replace(rec, ts="2026-08-09T12:00:00Z")
    assert stamped.ts == "2026-08-09T12:00:00Z"
    assert stamped.request_id == rec.request_id
    assert rec.ts is None  # the original is untouched

    # replace re-runs validation rather than bypassing it
    with pytest.raises(ValueError):
        dataclasses.replace(rec, input_tokens=-1)


@pytest.mark.parametrize("payload", [123, None, {"a": 1}, ["a"]])
def test_from_json_rejects_a_non_string_payload_with_value_error(payload):
    # Mirrors the from_dict guard so the wire boundary raises exactly one type.
    with pytest.raises(ValueError):
        UsageRecord.from_json(payload)
