"""TOKWEIR-15: guarded record construction on the metered request path.

TOKWEIR-4 made ``UsageRecord`` validate at construction, which is the right call
for a metering contract — but construction is not ``Sink.emit``, and ADR-0001
Pillar 2's off-critical-path guarantee is scoped to ``emit``. A producer building
a record inline on a request path would therefore take a ``ValueError`` into the
request it is metering.

These tests pin the seam that closes that gap: ``build_record``, ``emit_record``
and ``emit_usage`` turn any producer-side failure into a dropped record plus a
logged warning, so a malformed record degrades to "no metering for this call"
rather than failing the call.

The invalid-input matrix below deliberately mirrors the one ``test_contract.py``
asserts *raises*. That pairing is the point: the same input must be loud through
the constructor and quiet through the guard.
"""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

import tokenweir
import tokenweir.sink
from tokenweir import (
    REQUIRED_FIELDS,
    NullSink,
    UsageRecord,
    build_record,
    emit_record,
    emit_usage,
)

VALID_FIELDS = dict(
    request_id="req-1",
    app_id="mado",
    endpoint="/v1/messages",
    model="claude-sonnet-4",
    status="ok",
    input_tokens=10,
    output_tokens=20,
)


def _fields(**overrides):
    """Valid fields with `overrides` applied. A value of ``...`` removes the key,
    which is how the missing-required-argument (``TypeError``) case is built."""
    merged = dict(VALID_FIELDS)
    merged.update(overrides)
    return {k: v for k, v in merged.items() if v is not ...}


class RecordingSink:
    """A conforming Sink that remembers what it was handed."""

    def __init__(self):
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def close(self):
        return None


class RaisingSink:
    """A Sink that violates its own contract by raising from ``emit``.

    ``Sink.emit`` MUST NOT raise; this double exists because the request on the
    critical path should not have to depend on every adapter being correct.
    """

    def __init__(self, exc=None):
        self.exc = exc if exc is not None else RuntimeError("transport down")
        self.calls = 0

    def emit(self, record):
        self.calls += 1
        raise self.exc

    def close(self):
        return None


class ExplodingRepr:
    """A value whose ``repr`` raises ``KeyboardInterrupt``.

    The contract's own error message interpolates ``{value!r}``, so passing this
    as an identity field makes construction raise a ``BaseException`` from inside
    the guarded block — the only realistic way to prove ``BaseException`` is not
    swallowed.
    """

    def __repr__(self):
        raise KeyboardInterrupt


# Every invalid-input class TOKWEIR-4 rejects (SC-001). ``ValueError`` unless
# noted; the last entry is the ``TypeError`` path for an unknown keyword.
MALFORMED_CASES = [
    ("blank_identity", dict(app_id="")),
    ("whitespace_identity", dict(app_id="   ")),
    # Non-ASCII blanks matter: the contract shares one whitespace class with the
    # published schema so a JavaScript producer and this library agree on "blank".
    ("nbsp_only_identity", dict(app_id="\u00a0")),
    ("bom_only_identity", dict(app_id="\ufeff")),
    ("non_string_identity", dict(app_id=42)),
    ("none_identity", dict(request_id=None)),
    ("negative_count", dict(input_tokens=-1)),
    ("fractional_count", dict(input_tokens=1.5)),
    ("bool_count", dict(output_tokens=True)),
    ("string_count", dict(cache_read_input_tokens="10")),
    ("negative_latency", dict(latency_ms=-5)),
    ("wrong_typed_optional", dict(workload=42)),
    ("bad_pricing_mode", dict(pricing_mode="freemium")),
    ("zero_schema_version", dict(schema_version=0)),
    ("string_schema_version", dict(schema_version="1")),
    ("unknown_keyword", dict(not_a_field=1)),  # TypeError
    # A field literally named after the fused call's own parameter. Without
    # `sink` being positional-only this raises during *argument binding*, before
    # any guard runs — the one route by which a producer's data could still reach
    # the metered request.
    ("field_named_sink", dict(sink="oops")),  # TypeError
    ("field_named_self", dict(self="oops")),  # TypeError
]

MALFORMED_IDS = [name for name, _ in MALFORMED_CASES]
MALFORMED_OVERRIDES = [overrides for _, overrides in MALFORMED_CASES]


# --- US1: a malformed record cannot fail the request being metered ------------


@pytest.mark.parametrize("overrides", MALFORMED_OVERRIDES, ids=MALFORMED_IDS)
def test_malformed_fields_are_dropped_not_raised(overrides):
    # The story's core claim: nothing propagates into the metered request.
    sink = RecordingSink()
    assert emit_usage(sink, **_fields(**overrides)) is None


@pytest.mark.parametrize("overrides", MALFORMED_OVERRIDES, ids=MALFORMED_IDS)
def test_malformed_fields_would_raise_without_the_guard(overrides):
    # Pins the pairing: each case above is genuinely invalid, so the guard is
    # doing work rather than the matrix having gone stale. Without this, a case
    # that silently became *valid* would still pass the test above.
    with pytest.raises((ValueError, TypeError)):
        UsageRecord(**_fields(**overrides))


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_missing_required_argument_is_dropped(missing):
    # The TypeError path — Python's own signature check, which TOKWEIR-4 kept
    # deliberately distinct from ValueError. The guard must cover both.
    sink = RecordingSink()
    fields = _fields(**{missing: ...})
    assert emit_usage(sink, **fields) is None
    assert sink.records == []


def test_missing_required_argument_raises_typeerror_unguarded():
    with pytest.raises(TypeError):
        UsageRecord(**_fields(app_id=...))


def test_dropped_record_never_reaches_the_sink():
    sink = RecordingSink()
    emit_usage(sink, **_fields(app_id=""))
    assert sink.records == []


def test_valid_record_is_emitted_unchanged():
    # The guard adds no normalization, defaulting or coercion of its own: what
    # the sink receives is what direct construction would have produced.
    sink = RecordingSink()
    fields = _fields(workload="review", latency_ms=1234, pricing_mode="api_metered")

    returned = emit_usage(sink, **fields)

    assert returned == UsageRecord(**fields)
    assert sink.records == [returned]
    assert sink.records[0] is returned


def test_valid_record_survives_a_no_op_sink():
    assert emit_usage(NullSink(), **_fields()) == UsageRecord(**_fields())


def test_the_exploding_repr_trigger_actually_fires():
    # Guards the test below, which is only meaningful if constructing with an
    # ExplodingRepr really does raise KeyboardInterrupt. That depends on
    # contract.py interpolating {value!r} into its error text — a detail this
    # story does not own. If TOKWEIR-4's messages ever stop rendering the value,
    # this fails loudly instead of letting the FR-004 test pass vacuously.
    with pytest.raises(KeyboardInterrupt):
        UsageRecord(**_fields(app_id=ExplodingRepr()))


def test_base_exception_from_construction_propagates():
    # `except Exception`, never `except BaseException` — a metering guard that
    # swallows Ctrl-C is a worse bug than the one it fixes.
    with pytest.raises(KeyboardInterrupt):
        emit_usage(RecordingSink(), **_fields(app_id=ExplodingRepr()))


def test_a_field_named_sink_cannot_collide_with_the_parameter():
    # Argument binding happens before the function body, so a non-positional-only
    # `sink` parameter would raise TypeError outside every guard. This is the
    # explicit regression test for that escape hatch.
    sink = RecordingSink()
    assert emit_usage(sink, **_fields(sink="oops")) is None
    assert sink.records == []


def test_the_fused_call_takes_its_sink_positionally_only():
    # The mechanism behind the test above, pinned directly: passing the sink by
    # keyword must not be a supported call shape, or the collision returns.
    import inspect

    parameters = inspect.signature(emit_usage).parameters
    assert parameters["sink"].kind is inspect.Parameter.POSITIONAL_ONLY


# --- US2: a misbehaving sink cannot fail the request either -------------------


def test_emit_record_reports_failure_when_the_sink_raises():
    sink = RaisingSink()
    assert emit_record(sink, UsageRecord(**_fields())) is False
    assert sink.calls == 1


def test_emit_record_reports_success_for_a_conforming_sink():
    sink = RecordingSink()
    record = UsageRecord(**_fields())
    assert emit_record(sink, record) is True
    assert sink.records == [record]


def test_emit_usage_swallows_a_raising_sink():
    sink = RaisingSink()
    assert emit_usage(sink, **_fields()) is None
    assert sink.calls == 1


def test_emit_usage_survives_a_sink_that_is_not_a_sink_at_all():
    # A None or misconfigured sink raises AttributeError inside the guard. Still
    # a drop, still not the metered request's problem.
    assert emit_usage(None, **_fields()) is None


def test_a_missing_sink_is_not_described_as_having_raised(caplog):
    # The emission message must not claim the sink raised when the real cause is
    # that there is no sink; exc_info carries the AttributeError either way.
    caplog.set_level(logging.WARNING, logger="tokenweir")
    emit_usage(None, **_fields())
    assert "the sink failed" in caplog.text
    assert "AttributeError" in caplog.text


@pytest.mark.parametrize(
    "not_a_record",
    [None, "req-1", 42, {"app_id": "mado"}, VALID_FIELDS],
    ids=["none", "str", "int", "dict", "fields_dict"],
)
def test_emit_record_refuses_anything_that_is_not_a_record(not_a_record):
    # build_record returns None on a drop, so the naive composition of the two
    # halves would hand None to a conforming sink — which by contract cannot
    # raise and would dutifully persist it. The guard turns crashes into drops,
    # never into garbage in the store.
    sink = RecordingSink()
    assert emit_record(sink, not_a_record) is False
    assert sink.records == []


def test_refusing_a_non_record_logs_without_a_bogus_traceback(caplog):
    caplog.set_level(logging.WARNING, logger="tokenweir")
    assert emit_record(RecordingSink(), None) is False
    records = [r for r in caplog.records if r.name.startswith("tokenweir")]
    assert len(records) == 1
    # No exception was caught here — it is a rejection — so no traceback must be
    # attached, rather than rendering a misleading "NoneType: None".
    # (`logging` passes a falsy `exc_info` through verbatim, so this is `False`
    # rather than `None`; what matters is that nothing renders.)
    assert not records[0].exc_info
    assert "non-UsageRecord" in caplog.text
    assert "NoneType: None" not in caplog.text


def test_the_naive_composition_of_the_halves_cannot_poison_a_sink():
    # The exact shape US4 describes, written the careless way: no `is not None`
    # check between the halves. It must still be safe.
    sink = RecordingSink()
    record = build_record(**_fields(app_id=""))
    assert record is None
    assert emit_record(sink, record) is False
    assert sink.records == []


def test_base_exception_from_the_sink_propagates():
    sink = RaisingSink(exc=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        emit_usage(sink, **_fields())


# --- US3: a dropped record is visible, never silent ---------------------------


def test_construction_drop_logs_one_warning_with_the_cause(caplog):
    # A guard that swallowed failures silently would convert a loud producer-side
    # bug into an undetectable metering hole — undoing TOKWEIR-4 rather than
    # protecting it. The drop has to be observable.
    caplog.set_level(logging.WARNING, logger="tokenweir")

    assert emit_usage(RecordingSink(), **_fields(app_id="")) is None

    records = [r for r in caplog.records if r.name.startswith("tokenweir")]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is not None
    # The contract's own error text names the offending field; the guard does not
    # re-derive it, so the operator sees exactly what validation objected to.
    assert "app_id" in caplog.text


def test_emission_failure_logs_one_warning_with_the_cause(caplog):
    caplog.set_level(logging.WARNING, logger="tokenweir")

    assert emit_usage(RaisingSink(), **_fields()) is None

    records = [r for r in caplog.records if r.name.startswith("tokenweir")]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert "transport down" in caplog.text


def test_construction_and_emission_drops_are_distinguishable(caplog):
    caplog.set_level(logging.WARNING, logger="tokenweir")

    emit_usage(RecordingSink(), **_fields(app_id=""))
    construction_text = caplog.text
    caplog.clear()

    emit_usage(RaisingSink(), **_fields())
    emission_text = caplog.text

    assert construction_text != emission_text
    assert "construction failed" in construction_text
    assert "the sink failed" in emission_text
    # Both say what the operator actually needs to know: the request was fine.
    assert "no metering for this call" in construction_text
    assert "no metering for this call" in emission_text


def test_a_successful_emit_logs_nothing(caplog):
    caplog.set_level(logging.WARNING, logger="tokenweir")
    emit_usage(RecordingSink(), **_fields())
    assert [r for r in caplog.records if r.name.startswith("tokenweir")] == []


@pytest.mark.parametrize("overrides", MALFORMED_OVERRIDES, ids=MALFORMED_IDS)
def test_every_malformed_case_logs_exactly_one_warning(caplog, overrides):
    # SC-003 is "every drop in SC-001", not "a representative sample". Asserting
    # the return value alone would leave a whole error class — the TypeError half
    # of the matrix — free to drop silently.
    caplog.set_level(logging.WARNING, logger="tokenweir")

    assert emit_usage(RecordingSink(), **_fields(**overrides)) is None

    records = [r for r in caplog.records if r.name.startswith("tokenweir")]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info is not None
    assert "construction failed" in caplog.text


@pytest.mark.parametrize("missing", REQUIRED_FIELDS)
def test_every_missing_required_argument_logs_exactly_one_warning(caplog, missing):
    caplog.set_level(logging.WARNING, logger="tokenweir")

    assert emit_usage(RecordingSink(), **_fields(**{missing: ...})) is None

    records = [r for r in caplog.records if r.name.startswith("tokenweir")]
    assert len(records) == 1
    assert records[0].exc_info is not None
    # The TypeError path specifically — the one an assertion on the return value
    # alone would not have noticed going quiet.
    assert records[0].exc_info[0] is TypeError


@pytest.mark.parametrize("name", ["tokenweir", "tokenweir.sink"])
def test_the_library_pins_no_level_and_keeps_propagating(name):
    # Handler policy belongs to the application. A library that calls
    # basicConfig or pins a level takes that decision away from whoever embeds it,
    # and breaking propagation would hide drops from the application's own config.
    logger = logging.getLogger(name)
    assert logger.level == logging.NOTSET
    assert logger.propagate is True


def test_the_package_logger_carries_exactly_one_null_handler():
    # The stdlib idiom for a library. Without it `logging.lastResort` writes every
    # drop — traceback included — to the stderr of an application that configured
    # no logging, which is the library taking an output decision that is not its
    # to take. A NullHandler emits nothing, so it is not "configuring logging" in
    # the sense FR-008 forbids; it is declining to.
    handlers = logging.getLogger("tokenweir").handlers
    assert len(handlers) == 1
    assert type(handlers[0]) is logging.NullHandler
    # The module logger itself stays bare; the package logger is the one place.
    assert logging.getLogger("tokenweir.sink").handlers == []


def test_an_unconfigured_application_gets_no_output_on_stdout_or_stderr():
    # A subprocess so the assertion is about a genuinely unconfigured interpreter,
    # not about whatever pytest's capture and caplog fixtures have installed.
    program = """
import sys
import tokenweir

result = tokenweir.emit_usage(tokenweir.NullSink(), request_id="r", app_id="",
                              endpoint="/e", model="m", status="ok")
# The drop is still observable without any logging configuration at all: the
# return value is the signal that always reaches the caller.
assert result is None, result
sys.exit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(tokenweir.__file__).resolve().parents[2]),
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_an_application_that_configures_logging_still_sees_the_drop():
    # The other half of the trade-off: NullHandler declines to choose a stream,
    # it does not suppress the record. One line of basicConfig and the warning
    # arrives.
    program = """
import logging
import sys
import tokenweir

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
tokenweir.emit_usage(tokenweir.NullSink(), request_id="r", app_id="",
                     endpoint="/e", model="m", status="ok")
"""
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        cwd=str(Path(tokenweir.__file__).resolve().parents[2]),
    )
    assert completed.returncode == 0, completed.stderr
    assert "construction failed" in completed.stderr
    assert "app_id is required and must not be blank" in completed.stderr


def test_a_logger_that_raises_cannot_break_a_guarded_call(monkeypatch):
    # "Never raises" has to survive a hostile logging configuration, or the guard
    # has merely moved the throw site from the contract onto the logger.
    class ExplodingLogger:
        def warning(self, *args, **kwargs):
            raise RuntimeError("broken handler")

    monkeypatch.setattr(tokenweir.sink, "_logger", ExplodingLogger())

    assert build_record(**_fields(app_id="")) is None
    assert emit_record(RaisingSink(), UsageRecord(**_fields())) is False
    assert emit_usage(RecordingSink(), **_fields(app_id="")) is None
    assert emit_usage(RaisingSink(), **_fields()) is None


# --- US4: construction and emission are guardable separately ------------------


def test_build_record_returns_a_record_for_valid_input():
    assert build_record(**_fields()) == UsageRecord(**_fields())


@pytest.mark.parametrize("overrides", MALFORMED_OVERRIDES, ids=MALFORMED_IDS)
def test_build_record_returns_none_for_malformed_input(overrides):
    assert build_record(**_fields(**overrides)) is None


def test_emit_usage_delegates_to_the_two_halves(monkeypatch):
    # FR-003 is a structural claim, not just a behavioural one: the fused call
    # must be *composed of* the halves so there is one implementation of each
    # guarantee. Behavioural agreement tests catch divergence but not
    # duplication — a parallel inline reimplementation would pass them all.
    calls = []

    def fake_build(**fields):
        calls.append(("build", fields))
        return UsageRecord(**fields)

    def fake_emit(sink, record):
        calls.append(("emit", record))
        return True

    monkeypatch.setattr(tokenweir.sink, "build_record", fake_build)
    monkeypatch.setattr(tokenweir.sink, "emit_record", fake_emit)

    returned = tokenweir.sink.emit_usage(RecordingSink(), **_fields())

    assert [name for name, _ in calls] == ["build", "emit"]
    assert calls[0][1] == _fields()
    assert calls[1][1] is returned


def test_emit_usage_does_not_emit_when_the_build_half_drops(monkeypatch):
    monkeypatch.setattr(tokenweir.sink, "build_record", lambda **fields: None)
    monkeypatch.setattr(
        tokenweir.sink,
        "emit_record",
        lambda sink, record: pytest.fail("emit_record must not run after a build drop"),
    )
    assert tokenweir.sink.emit_usage(RecordingSink(), **_fields()) is None


def test_emit_usage_returns_none_when_the_emit_half_fails(monkeypatch):
    monkeypatch.setattr(tokenweir.sink, "emit_record", lambda sink, record: False)
    assert tokenweir.sink.emit_usage(RecordingSink(), **_fields()) is None


def test_build_then_emit_matches_the_fused_call_when_valid():
    # The fused call is a composition of the halves, not a parallel
    # implementation — so the two paths must agree.
    halves_sink, fused_sink = RecordingSink(), RecordingSink()

    record = build_record(**_fields())
    assert emit_record(halves_sink, record) is True
    fused = emit_usage(fused_sink, **_fields())

    assert record == fused
    assert halves_sink.records == fused_sink.records


def test_build_then_emit_matches_the_fused_call_when_malformed():
    sink = RecordingSink()
    assert build_record(**_fields(app_id="")) is None
    assert emit_usage(sink, **_fields(app_id="")) is None
    assert sink.records == []


def test_build_then_emit_matches_the_fused_call_when_the_sink_raises():
    record = build_record(**_fields())
    assert record is not None
    assert emit_record(RaisingSink(), record) is False
    assert emit_usage(RaisingSink(), **_fields()) is None


def test_a_caller_can_stamp_a_record_between_the_halves():
    # The shape the gateway actually has: it computes latency_ms only after the
    # call it is metering returns. If the library did not expose the halves, this
    # caller would hand-roll try/except — the duplication this story prevents.
    import dataclasses

    sink = RecordingSink()
    record = build_record(**_fields())
    stamped = dataclasses.replace(record, latency_ms=1234, ts="2026-08-10T12:00:00Z")

    assert emit_record(sink, stamped) is True
    assert sink.records == [stamped]


# --- Contract non-regression --------------------------------------------------


def test_the_constructor_still_raises():
    # The guard sits beside the contract; it does not soften it. Off a request
    # path — a batch import, a migration — raising is still correct behaviour.
    with pytest.raises(ValueError):
        UsageRecord(**_fields(app_id=""))
    with pytest.raises(ValueError):
        UsageRecord(**_fields(input_tokens=-1))


def test_the_sink_protocol_is_unchanged():
    # emit_record guards against a non-conforming adapter; it does not relax the
    # protocol's "MUST NOT raise" contract, and both doubles still satisfy it
    # structurally.
    from tokenweir import Sink

    assert isinstance(RecordingSink(), Sink)
    assert isinstance(RaisingSink(), Sink)
    assert isinstance(NullSink(), Sink)


def test_the_guarded_seam_is_exported_from_the_package():
    import tokenweir

    for name in ("build_record", "emit_record", "emit_usage"):
        assert name in tokenweir.__all__
        assert getattr(tokenweir, name) is getattr(tokenweir.sink, name)
