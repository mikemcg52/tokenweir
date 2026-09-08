"""The Claude Code Stop hook (TOKWEIR-7).

The story's acceptance is two claims, and neither can be demonstrated by reading
the code:

> a Max-authenticated Claude Code turn produces exactly one usage record with
> correct token deltas; a forced emitter failure does not block the next turn.

So the tests are grouped the way the spec's user stories are — `counts`,
`never_disturbs`, `attribution`, `packaging` — and each group is runnable on its
own with `pytest -k`.

**Everything goes through the module's real entry points against real files.**
There is no monkeypatching of `open`, no stubbing of the parser, no asserting that
a mock was called. The one substitution is the **sink**, which is the seam the
library publishes for exactly this purpose: a `Sink` is what a producer is
supposed to be told to hand records to, so a recording one is the honest test
double rather than a stand-in for the code under test.

The environment is scrubbed for every test. The hook reads eleven environment
variables and this suite runs inside a MADO stream pod, where several of them are
*genuinely set* — a test that asserted a record's default `workload` would pass on
a laptop and fail in the pod that this code is written for, or worse, the reverse.
"""

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from tokenweir import PricingMode, UsageRecord
from tokenweir.claude_code import (
    DEFAULT_APP_ID,
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    SinkChoice,
    TokenTotals,
    build_fields,
    main,
    read_baseline,
    read_hook_input,
    run,
    scan_transcript,
    select_sink,
    state_path_for,
    turn_delta,
    write_baseline,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- doubles ---------------------------------------------------------------


class RecordingSink:
    """A `Sink` that keeps what it is given. The suite's window onto the record."""

    def __init__(self):
        self.records = []
        self.closed = False

    def emit(self, record):
        self.records.append(record)

    def close(self):
        self.closed = True


class RaisingSink:
    """A sink that violates its own contract on every call.

    Non-conforming on purpose: the story's second acceptance clause is about a
    *forced* emitter failure, and a sink that politely returns `None` would not
    force anything. `Sink.emit` says implementations must not raise; this is what
    the guarded seam exists to absorb when one does anyway.
    """

    def __init__(self):
        self.attempts = 0

    def emit(self, record):
        self.attempts += 1
        raise RuntimeError("forced emitter failure")

    def close(self):
        pass


class HangingSink:
    """A sink whose delivery never completes — a dead broker that accepted the TCP.

    The released event is not decoration: without it the emitter's daemon worker
    stays parked in `emit` for the rest of the session. It is a daemon so it cannot
    hold the interpreter open, but a test that leaves threads wedged behind it is
    the kind of thing that makes an unrelated suite flaky later.
    """

    def __init__(self):
        self.released = threading.Event()
        self.entered = threading.Event()

    def emit(self, record):
        self.entered.set()
        self.released.wait(30)

    def close(self):
        pass


# --- fixtures --------------------------------------------------------------


#: Every environment variable the hook reads. Kept here rather than exported from
#: the module: a published list is a promise to a consumer, and the only party that
#: needs one is this fixture.
HOOK_ENVIRONMENT = (
    "TOKENWEIR_AMQP_URL",
    "TOKENWEIR_DSN",
    "TOKENWEIR_APP_ID",
    "TOKENWEIR_ENDPOINT",
    "TOKENWEIR_HOOK_STATE_DIR",
    "XDG_STATE_HOME",
    "MADO_ISSUE_KEY",
    "MADO_PHASE",
    "MADO_STREAM_ID",
    "MADO_PRICING_MODE",
)


@pytest.fixture(autouse=True)
def scrubbed_environment(tmp_path, monkeypatch):
    """Every variable the hook reads, unset — then the state directory pointed
    somewhere disposable.

    Autouse because the danger is a variable the *pod* set, not one a test set: an
    inherited `MADO_ISSUE_KEY` would make the "no attribution" tests assert the
    opposite of what they claim — passing on a laptop and failing in the very pod
    this code is written for, or the reverse.
    """
    for name in HOOK_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    state_dir = tmp_path / "hook-state"
    monkeypatch.setenv("TOKENWEIR_HOOK_STATE_DIR", str(state_dir))
    return state_dir


def transcript_entry(
    *,
    message_id=None,
    usage=None,
    model="claude-opus-5",
    timestamp="2026-09-08T00:10:00.000Z",
    entry_type="assistant",
    **extra,
):
    """One assistant transcript entry, shaped as Claude Code writes them."""
    entry = {
        "type": entry_type,
        "uuid": f"entry-{message_id or 'x'}",
        "timestamp": timestamp,
        "message": {"id": message_id, "model": model, "usage": usage or {}},
    }
    entry.update(extra)
    return entry


def usage(inp=0, out=0, creation=0, read=0):
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_creation_input_tokens": creation,
        "cache_read_input_tokens": read,
    }


class Transcript(os.PathLike):
    """A transcript file with an `append(*entries)` that adds JSONL lines.

    A wrapper rather than a `Path` subclass with an extra method: `Path` uses
    `__slots__`, so the obvious `path.append = ...` fails at runtime. Implementing
    `__fspath__` keeps it usable everywhere the module takes a path.
    """

    def __init__(self, path: Path):
        self.path = path

    def __fspath__(self) -> str:
        return str(self.path)

    def __str__(self) -> str:
        return str(self.path)

    def append(self, *entries):
        with self.path.open("a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")
        return self.path

    def open(self, *args, **kwargs):
        return self.path.open(*args, **kwargs)

    def write_text(self, *args, **kwargs):
        return self.path.write_text(*args, **kwargs)


@pytest.fixture
def transcript(tmp_path):
    """An empty transcript file, ready to be appended to."""
    path = tmp_path / "session.jsonl"
    path.touch()
    return Transcript(path)


def hook_stdin(transcript_path, session_id="sess-1"):
    """The stdin payload Claude Code hands a Stop hook."""
    return io.StringIO(
        json.dumps(
            {
                "session_id": session_id,
                "transcript_path": str(transcript_path),
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            }
        )
    )


def into(sink, *, degraded: bool = False):
    """A `sink_factory` for `run` that hands back a prepared sink.

    `run` builds its sink late — nothing is constructed for a turn with nothing to
    emit — so it takes a factory rather than a sink. Tests want a sink they can
    inspect afterwards, which is what this closes over.
    """
    return lambda: SinkChoice(sink, degraded=degraded)


def counts_of(record: UsageRecord) -> tuple:
    return (
        record.input_tokens,
        record.output_tokens,
        record.cache_creation_input_tokens,
        record.cache_read_input_tokens,
    )


# ===========================================================================
# User Story 1 — one correct record per turn
# ===========================================================================


def test_counts_a_turn_spanning_several_messages_as_one_record(transcript):
    """SC-001. The ADR's own caveat — "a turn may span multiple assistant
    messages" — is the case, and it must still be *one* record."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(100, 10, 5, 0)),
        transcript_entry(message_id="msg_b", usage=usage(120, 25, 0, 50)),
        transcript_entry(message_id="msg_c", usage=usage(130, 40, 0, 90)),
    )
    sink = RecordingSink()

    record = run(hook_stdin(transcript), into(sink))

    assert len(sink.records) == 1
    assert record is sink.records[0]
    assert counts_of(record) == (350, 75, 5, 140)


def test_counts_the_second_turn_as_a_delta_not_a_total(transcript):
    """SC-002. The whole reason state exists: a transcript holds the session, so
    the second turn's record must not re-count the first turn's tokens."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(100, 10)),
        transcript_entry(message_id="msg_b", usage=usage(120, 25)),
    )
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    transcript.append(
        transcript_entry(message_id="msg_c", usage=usage(200, 30)),
        transcript_entry(message_id="msg_d", usage=usage(300, 40)),
    )
    run(hook_stdin(transcript), into(sink))

    assert len(sink.records) == 2
    assert counts_of(sink.records[0]) == (220, 35, 0, 0)
    assert counts_of(sink.records[1]) == (500, 70, 0, 0)
    # And the two together are the session total — nothing lost, nothing doubled.
    assert sum(r.input_tokens for r in sink.records) == 720


def test_counts_one_api_response_once_however_many_lines_mention_it(transcript):
    """FR-006. The silent over-count: Claude Code can write several entries for
    one API response, each repeating the *same* usage object. Summing lines
    inflates the turn, and every line is individually well-formed, so nothing
    about the result looks wrong."""
    repeated = usage(1000, 200, 50, 10)
    transcript.append(
        *[transcript_entry(message_id="msg_same", usage=repeated) for _ in range(4)]
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[0]) == (1000, 200, 50, 10)


def test_counts_sidechain_subagent_entries(transcript):
    """A subagent's tokens come out of the same subscription window. Excluding
    them would under-report precisely the runs this exists to measure."""
    transcript.append(
        transcript_entry(message_id="msg_main", usage=usage(100, 10)),
        transcript_entry(
            message_id="msg_sub", usage=usage(400, 60), isSidechain=True
        ),
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[0]) == (500, 70, 0, 0)


def test_counts_nothing_and_emits_nothing_when_the_turn_added_no_tokens(transcript):
    """FR-013. A zero-token record inflates the request count while adding no
    tokens — worse than no row."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    assert run(hook_stdin(transcript), into(sink)) is None
    assert len(sink.records) == 1


def test_counts_are_re_anchored_when_the_transcript_is_replaced(transcript):
    """FR-014. A resumed session can write a *fresh* file at the same path. The
    difference is not negative tokens; the baseline is stale."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(5000, 900)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    transcript.write_text("", encoding="utf-8")
    transcript.append(transcript_entry(message_id="msg_new", usage=usage(10, 2)))

    assert run(hook_stdin(transcript), into(sink)) is None
    assert len(sink.records) == 1, "a replaced transcript must not re-count a session"

    # Re-anchored, so the *next* turn is a correct delta against the new file.
    transcript.append(transcript_entry(message_id="msg_next", usage=usage(40, 8)))
    run(hook_stdin(transcript), into(sink))
    assert counts_of(sink.records[1]) == (40, 8, 0, 0)


def test_counts_survive_lines_that_are_not_usable(transcript):
    """FR-007. Abandoning the file at the first oddity would throw away a whole
    session over one bad line — and the last line of a transcript being read
    while it is written is routinely a half-written fragment."""
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write("\n")
        handle.write("not json at all\n")
        handle.write(json.dumps(["a list, not an object"]) + "\n")
        handle.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        handle.write(
            json.dumps({"message": {"id": "m", "usage": "not a mapping"}}) + "\n"
        )
        handle.write(json.dumps(transcript_entry(message_id="ok", usage=usage(7, 3))) + "\n")
        handle.write('{"message": {"id": "trunc", "usa')  # a torn final line

    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[0]) == (7, 3, 0, 0)


@pytest.mark.parametrize(
    "bad", [None, -5, "120", True, False, 3.7, {"nested": 1}, [1]]
)
def test_counts_coerce_unusable_token_values_to_zero(transcript, bad):
    """FR-008. Refusing the whole record over one malformed field would lose
    three good counts to punish one bad one."""
    transcript.append(
        transcript_entry(
            message_id="msg_a",
            usage={"input_tokens": bad, "output_tokens": 11},
        )
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[0]) == (0, 11, 0, 0)


def test_counts_accept_an_integral_float_as_the_integer_it_means(transcript):
    """JSON has one number type; `100.0` from a non-Python producer means 100 —
    which is exactly what the contract's own wire coercion already allows."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage={"input_tokens": 100.0})
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].input_tokens == 100


def test_counts_are_stamped_subscription(transcript):
    """The story names the mode explicitly, and it is the reason this capture
    path exists at all."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].pricing_mode is PricingMode.SUBSCRIPTION


def test_counts_produce_a_record_that_validates_against_the_published_schema(
    transcript,
):
    """SC-006. Both capture modes feed the *same* contract; a record this producer
    builds must be indistinguishable in kind from one the gateway builds."""
    jsonschema = pytest.importorskip(
        "jsonschema", reason="JSON Schema engine is a [dev] extra"
    )
    from tokenweir import usage_record_json_schema

    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1, 2, 3)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    jsonschema.validate(
        instance=sink.records[0].to_dict(), schema=usage_record_json_schema()
    )


def test_counts_carry_the_turns_own_timestamp_normalized_to_utc(transcript):
    """FR-023. The transcript's timestamp is when the work happened; the hook's
    clock is when the hook got round to looking."""
    transcript.append(
        transcript_entry(
            message_id="msg_a", usage=usage(10, 1), timestamp="2026-09-08T00:10:00.000Z"
        )
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].ts == "2026-09-08T00:10:00+00:00"


def test_counts_fall_back_to_now_for_an_unparseable_timestamp(transcript):
    """The store's column is a TIMESTAMPTZ: passing a non-timestamp through would
    fail the whole batch it travels in."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(10, 1), timestamp="yesterday")
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    from datetime import datetime

    parsed = datetime.fromisoformat(sink.records[0].ts)
    assert parsed.tzinfo is not None


def test_counts_identify_the_record_by_the_last_counted_response(transcript):
    """FR-019. Unique per turn, and traceable: a row in the store can be found
    again in the transcript that produced it."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(10, 1)),
        transcript_entry(message_id="msg_b", usage=usage(20, 2)),
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].request_id == "msg_b"


def test_counts_still_produce_a_record_when_no_message_id_is_present(transcript):
    """A transcript with no usable ids must still produce a record: `request_id`
    is required and non-blank, and a uuid is a worse identifier than a message id
    but an infinitely better one than a dropped turn."""
    entry = transcript_entry(message_id=None, usage=usage(10, 1))
    entry.pop("uuid")
    transcript.append(entry)
    sink = RecordingSink()

    record = run(hook_stdin(transcript), into(sink))

    assert record is not None
    assert record.request_id.startswith("sess-1:")


def test_counts_name_the_model_verbatim(transcript):
    """ADR-0001 Pillar 3: the model is an opaque identifier, stored as written."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(1, 1), model="claude-haiku-4-5")
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].model == "claude-haiku-4-5"


def test_counts_fall_back_to_a_placeholder_when_no_model_is_named(transcript):
    """FR-020. `model` is required and non-blank by contract, so the alternative
    to an unknown model is no record at all."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(1, 1), model=None))
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].model == DEFAULT_MODEL


def test_counts_carry_the_last_known_model_forward_within_a_turn(transcript):
    """A later entry that names no model does not erase what the turn was run on
    — "unknown" would be a less true answer than the model actually in use."""
    transcript.append(
        transcript_entry(message_id="msg_a", usage=usage(1, 1), model="claude-opus-5"),
        transcript_entry(message_id="msg_b", usage=usage(1, 1), model=None),
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].model == "claude-opus-5"


def test_counts_default_app_id_and_endpoint_and_honour_overrides(
    transcript, monkeypatch
):
    """FR-024. `endpoint` is deliberately not `/v1/messages`: a turn is an
    aggregate of several calls, and one key for both would blend them."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(1, 1)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))
    assert sink.records[0].app_id == DEFAULT_APP_ID
    assert sink.records[0].endpoint == DEFAULT_ENDPOINT
    assert sink.records[0].endpoint != "/v1/messages"

    monkeypatch.setenv("TOKENWEIR_APP_ID", "mado")
    monkeypatch.setenv("TOKENWEIR_ENDPOINT", "custom/endpoint")
    transcript.append(transcript_entry(message_id="msg_b", usage=usage(1, 1)))
    run(hook_stdin(transcript), into(sink))
    assert sink.records[1].app_id == "mado"
    assert sink.records[1].endpoint == "custom/endpoint"


def test_counts_leave_latency_unset(transcript):
    """The transcript's timestamps are gaps between writes, not call latency. A
    plausible invented number is worse than an omitted optional field."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(1, 1)))
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].latency_ms is None


def test_counts_are_kept_per_transcript(tmp_path):
    """FR-011. Two concurrent sessions must not consume each other's deltas."""
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    for path, tokens in ((first, 100), (second, 700)):
        path.write_text(
            json.dumps(transcript_entry(message_id=f"m-{tokens}", usage=usage(tokens)))
            + "\n",
            encoding="utf-8",
        )

    sink = RecordingSink()
    run(hook_stdin(first, session_id="s1"), into(sink))
    run(hook_stdin(second, session_id="s2"), into(sink))

    assert [r.input_tokens for r in sink.records] == [100, 700]
    assert state_path_for(first) != state_path_for(second)


def test_counts_carry_into_the_next_turn_when_the_emit_is_refused(transcript):
    """FR-012 — the reason the baseline is cumulative and written *last*.

    A file cursor advanced before the emit would have deleted this turn's tokens
    permanently, and nothing anywhere would have said so."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    raising = RaisingSink()
    assert run(hook_stdin(transcript), into(raising)) is None
    assert raising.attempts >= 1, "the sink was actually asked to take the record"

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (120, 12, 0, 0), (
        "the refused turn's tokens must be carried, not dropped"
    )


def test_counts_carry_forward_when_the_record_is_accepted_but_never_delivered(
    transcript,
):
    """The subtler half of FR-012, and the one a buffered emitter makes easy to
    get wrong.

    `BufferedEmitter.emit` accepts and returns — by contract, it cannot tell the
    caller that the broker is dead. A producer that took acceptance for success
    would advance its baseline for a record nothing ever stored, which is the loss
    the cumulative baseline exists to prevent, reintroduced one layer down. This
    hook emits one record and exits, so it flushes and asks."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    hanging = HangingSink()
    try:
        assert run(hook_stdin(transcript), into(hanging), close_timeout=0.2) is None
    finally:
        hanging.released.set()

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (120, 12, 0, 0), (
        "tokens accepted but never delivered must carry, not vanish"
    )


def test_counts_advance_the_baseline_once_delivery_succeeds(transcript):
    """The other side of the same rule: a delivered record must *not* be
    re-reported, or a working broker would double-count every turn."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[1]) == (20, 2, 0, 0)


def test_counts_carry_forward_when_a_conforming_sink_drops_without_raising(
    transcript,
):
    """FR-012 against the shape every *real* adapter has, which is the shape that
    breaks a naive implementation.

    `Sink.emit` MUST NOT raise, so a conforming adapter catches its own transport
    failure and counts a drop: `DirectSink` on an unreachable store, `AMQPSink` on
    a failed publish. Both return normally. `BufferedEmitter` therefore counts them
    `delivered`, and a producer that trusted that number would advance its baseline
    over a record nothing stored — a silent, permanent loss with *both* shipped
    transports, invisible to any test whose only failing sink raises.

    So this one does what an adapter does: takes the record, keeps nothing, tells
    nobody.
    """

    class ConformingDropSink:
        """Never raises, never stores, and counts what it stored — like the real ones."""

        def __init__(self):
            self.written = 0
            self.dropped = 0

        def emit(self, record):
            self.dropped += 1

        def close(self):
            pass

    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    dropping = ConformingDropSink()
    assert run(hook_stdin(transcript), into(dropping)) is None
    assert dropping.dropped == 1, "the sink was actually offered the record"

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (120, 12, 0, 0), (
        "a sink that accepted and stored nothing must not advance the baseline"
    )


def test_counts_carry_forward_through_the_librarys_own_direct_sink(transcript):
    """The same property, asserted against `DirectSink` itself rather than a
    double — because the claim being made is about the adapters this library
    ships, and a double is only evidence about the double."""
    from tokenweir.sink import DirectSink

    class UnreachableStore:
        def write(self, records):
            raise RuntimeError("store unreachable")

        def close(self):
            pass

    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    dead = DirectSink(UnreachableStore())
    assert run(hook_stdin(transcript), into(dead)) is None
    assert dead.written == 0 and dead.dropped == 1

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (120, 12, 0, 0)


def test_counts_advance_the_baseline_when_a_conforming_sink_really_stores(
    transcript,
):
    """The other side: a working store must not be made to re-report every turn.
    A check that never advances is as wrong as one that always does."""
    from tokenweir.sink import DirectSink
    from tokenweir.source import MemorySource

    store = MemorySource()
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    run(hook_stdin(transcript), into(DirectSink(store)))

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    second = MemorySource()
    run(hook_stdin(transcript), into(DirectSink(second)))

    assert [r.input_tokens for r in store.records] == [100]
    assert [r.input_tokens for r in second.records] == [20]


def test_counts_carry_forward_when_a_configured_transport_cannot_be_built(
    transcript, monkeypatch
):
    """A transport that was *configured* and could not be constructed degrades to
    a no-op sink so the hook cannot fail — and a no-op sink accepts everything.
    Treating that as metered would discard the turn of every session started
    before its broker was up."""
    monkeypatch.setenv("TOKENWEIR_AMQP_URL", "amqp://nowhere.invalid:5672/")
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))

    choice = select_sink()
    assert choice.degraded is True, "a configured-but-unbuildable transport is degraded"

    assert run(hook_stdin(transcript), lambda: choice) is None

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (120, 12, 0, 0)


def test_counts_advance_for_an_unconfigured_hook_that_discards_by_choice(
    transcript,
):
    """The case that must *not* be caught by the rule above. An unconfigured hook
    is discarding by choice, and holding its baseline for ever would make the
    first turn after a transport is configured report the entire session."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    assert run(hook_stdin(transcript), select_sink) is not None

    transcript.append(transcript_entry(message_id="msg_b", usage=usage(20, 2)))
    recording = RecordingSink()
    run(hook_stdin(transcript), into(recording))

    assert counts_of(recording.records[0]) == (20, 2, 0, 0)


def test_the_sink_is_not_built_when_there_is_nothing_to_emit(transcript):
    """A `Stop` hook fires on every turn, and plenty of turns add no tokens.
    Opening a broker connection to discover that is a connection per turn, and a
    connection that can wedge on a turn with nothing at stake."""
    built = []

    def factory():
        built.append(True)
        return SinkChoice(RecordingSink())

    # Nothing in the transcript at all.
    assert run(hook_stdin(transcript), factory) is None
    assert built == []

    # And nothing *new* in it.
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))
    assert run(hook_stdin(transcript), factory) is None
    assert built == [], "a zero-delta turn built a sink"


# ===========================================================================
# User Story 2 — a failure never disturbs the session
# ===========================================================================


def _drive_main(monkeypatch, payload, sink, *, degraded=False):
    """Run the real entry point with a chosen sink and a chosen stdin."""
    monkeypatch.setattr(sys, "stdin", payload)
    monkeypatch.setattr(
        "tokenweir.claude_code.select_sink", into(sink, degraded=degraded)
    )
    return main()


def test_never_disturbs_the_session_when_the_sink_raises(
    transcript, monkeypatch, capsys
):
    """SC-003, SC-004 — the story's second acceptance clause, exactly."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))

    status = _drive_main(monkeypatch, hook_stdin(transcript), RaisingSink())

    assert status == 0
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "   ",
        "not json",
        json.dumps([1, 2, 3]),
        json.dumps({}),
        json.dumps({"transcript_path": ""}),
        json.dumps({"transcript_path": 17}),
        json.dumps({"transcript_path": "/nonexistent/nowhere.jsonl"}),
    ],
    ids=[
        "empty",
        "whitespace",
        "not-json",
        "json-array",
        "no-transcript-path",
        "blank-path",
        "non-string-path",
        "missing-file",
    ],
)
def test_never_disturbs_the_session_on_unusable_input(payload, monkeypatch, capsys):
    """FR-002. A hook that cannot tell which transcript it was called about has
    nothing to meter, and that is a quiet no-op rather than an error."""
    sink = RecordingSink()

    status = _drive_main(monkeypatch, io.StringIO(payload), sink)

    assert status == 0
    assert sink.records == []
    assert capsys.readouterr().out == ""


def test_never_disturbs_the_session_when_the_state_directory_is_unwritable(
    transcript, tmp_path, monkeypatch, capsys
):
    """FR-017. An unwritable cache costs accuracy on the *next* turn; it must not
    cost this turn its record."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("TOKENWEIR_HOOK_STATE_DIR", str(blocked / "state"))
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    try:
        status = _drive_main(monkeypatch, hook_stdin(transcript), sink)
    finally:
        blocked.chmod(0o700)

    assert status == 0
    assert len(sink.records) == 1
    assert capsys.readouterr().out == ""


def test_never_disturbs_the_session_when_the_transcript_is_a_directory(
    tmp_path, monkeypatch, capsys
):
    """A `transcript_path` that cannot be read as a file at all."""
    sink = RecordingSink()

    status = _drive_main(
        monkeypatch, hook_stdin(tmp_path), sink
    )

    assert status == 0
    assert sink.records == []
    assert capsys.readouterr().out == ""


def test_never_disturbs_the_session_when_the_pipeline_itself_fails(
    transcript, monkeypatch, capsys
):
    """FR-025/FR-026. Covers the bug nobody anticipated — the whole reason `main`
    has a blanket handler rather than a list of expected failures."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))

    def explode(*args, **kwargs):
        raise RuntimeError("an unanticipated bug in the hook itself")

    monkeypatch.setattr("tokenweir.claude_code.run", explode)

    status = _drive_main(monkeypatch, hook_stdin(transcript), RecordingSink())

    assert status == 0
    assert status != 2, "exit 2 is Claude Code's blocking status and is forbidden"
    assert capsys.readouterr().out == ""


def test_never_disturbs_the_session_when_sink_construction_fails(
    transcript, monkeypatch, capsys
):
    """FR-030. Construction is normally where raising is right — but here nobody
    is watching, so it degrades to a no-op sink."""
    monkeypatch.setenv("TOKENWEIR_AMQP_URL", "amqp://nowhere.invalid:5672/")
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    monkeypatch.setattr(sys, "stdin", hook_stdin(transcript))

    status = main()

    assert status == 0
    assert capsys.readouterr().out == ""


def test_never_disturbs_the_session_when_delivery_hangs(transcript):
    """FR-028, SC-005. A sink that accepted the record and cannot deliver it must
    cost a bounded wait, not an unbounded one."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = HangingSink()

    started = time.monotonic()
    try:
        run(hook_stdin(transcript), into(sink), close_timeout=0.2)
        elapsed = time.monotonic() - started
    finally:
        sink.released.set()

    assert elapsed < 5, f"a hanging sink held the hook for {elapsed:.1f}s"


def test_never_disturbs_the_session_without_a_session_id(transcript):
    """FR-003 — "and MUST work without it". Claude Code supplies `session_id`, but
    the hook's contract does not require it, and the fallback identity branch is
    only reachable when it is absent."""
    entry = transcript_entry(message_id=None, usage=usage(10, 1))
    entry.pop("uuid")  # no `message.id` *and* no entry uuid: nothing to be stable about
    transcript.append(entry)
    payload = io.StringIO(json.dumps({"transcript_path": str(transcript)}))
    sink = RecordingSink()

    record = run(payload, into(sink))

    assert record is not None
    assert record.request_id.startswith("claude-code:")


def test_never_disturbs_the_session_with_a_corrupt_baseline(transcript, monkeypatch):
    """FR-015. Corrupt state over-counts one turn; a *guessed* baseline would
    silently under-count every turn after it, which nobody would notice."""
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    state = state_path_for(transcript)
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{not json", encoding="utf-8")
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert counts_of(sink.records[0]) == (100, 10, 0, 0)


def test_never_disturbs_the_session_and_writes_the_baseline_atomically(
    tmp_path, transcript, monkeypatch
):
    """FR-016, asserted as a *mechanism* rather than as an outcome.

    An interrupted in-place write leaves a truncated file that every later
    invocation reads as corrupt — one interruption costing every subsequent turn
    its baseline. Round-tripping a value proves nothing about that: a plain
    `write_text` round-trips just as well. So this asserts what a non-atomic
    implementation cannot do — the target is only ever reached by `os.replace`,
    from a temporary file in the target's own directory, since `os.replace` is
    atomic only within a filesystem.
    """
    state = state_path_for(transcript)
    replacements = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        replacements.append((Path(src), Path(dst)))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("tokenweir.claude_code.os.replace", spy)

    assert write_baseline(state, TokenTotals(1, 2, 3, 4), transcript=transcript)

    assert read_baseline(state) == TokenTotals(1, 2, 3, 4)
    assert len(replacements) == 1, "the target was not reached by an atomic replace"
    source, destination = replacements[0]
    assert destination == state
    assert source.parent == state.parent, (
        "os.replace is atomic only within a filesystem, so the temporary file "
        "must live in the target's own directory"
    )
    assert not list(state.parent.glob("*.tmp")), "no temporary file left behind"


def test_never_disturbs_the_session_when_the_baseline_write_is_interrupted(
    transcript, monkeypatch
):
    """The consequence FR-016 exists for: an interruption must leave the *previous*
    baseline intact, not a truncated file that reads as corrupt forever."""
    state = state_path_for(transcript)
    assert write_baseline(state, TokenTotals(11, 22, 33, 44), transcript=transcript)

    def interrupted(src, dst, *args, **kwargs):
        raise OSError("interrupted before the rename landed")

    monkeypatch.setattr("tokenweir.claude_code.os.replace", interrupted)
    assert write_baseline(state, TokenTotals(99, 99, 99, 99), transcript=transcript) is False

    monkeypatch.undo()
    assert read_baseline(state) == TokenTotals(11, 22, 33, 44)
    assert not list(state.parent.glob("*.tmp")), "the partial write was cleaned up"


# ===========================================================================
# User Story 3 — attribution from the environment
# ===========================================================================


def test_attribution_comes_from_the_orchestrators_environment(
    transcript, monkeypatch
):
    """SC-007. ADR-0001 Pillar 4: "Attribution comes from the orchestrator, not
    the model." """
    monkeypatch.setenv("MADO_ISSUE_KEY", "TOKWEIR-7")
    monkeypatch.setenv("MADO_PHASE", "review")
    monkeypatch.setenv("MADO_STREAM_ID", "stream-42")
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    record = sink.records[0]
    assert record.workload == "TOKWEIR-7"
    assert record.queue == "review"
    assert record.parent_request_id == "stream-42"


def test_attribution_rolls_a_streams_turns_up_under_one_parent(
    transcript, monkeypatch
):
    """The `parent_request_id` column exists to "roll them back up to the request
    the user actually made" — which is what a stream is to its turns."""
    monkeypatch.setenv("MADO_STREAM_ID", "stream-42")
    sink = RecordingSink()
    for n in range(3):
        transcript.append(
            transcript_entry(message_id=f"msg_{n}", usage=usage(10 * (n + 1)))
        )
        run(hook_stdin(transcript), into(sink))

    assert len(sink.records) == 3
    assert {r.parent_request_id for r in sink.records} == {"stream-42"}


@pytest.mark.parametrize("value", [None, "", "   ", "\t\n"])
def test_attribution_treats_blank_and_unset_alike(transcript, monkeypatch, value):
    """FR-022. `MADO_PHASE=""` says "no phase"; writing `''` into the column says
    the field was populated, which is a different claim."""
    for name in ("MADO_ISSUE_KEY", "MADO_PHASE", "MADO_STREAM_ID"):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    record = run(hook_stdin(transcript), into(sink))

    assert record is not None, "a laptop with no orchestrator still gets a record"
    assert record.workload is None
    assert record.queue is None
    assert record.parent_request_id is None


def test_attribution_falls_back_to_subscription_for_an_unrecognized_mode(
    transcript, monkeypatch
):
    """FR-018. A misconfigured environment variable losing a turn's metering
    would be the tail wagging the dog."""
    monkeypatch.setenv("MADO_PRICING_MODE", "flat_rate_probably")
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    record = run(hook_stdin(transcript), into(sink))

    assert record is not None
    assert record.pricing_mode is PricingMode.SUBSCRIPTION


def test_attribution_honours_a_recognized_pricing_mode_override(
    transcript, monkeypatch
):
    """The variable is in ADR-0001's list, so it has to actually do something."""
    monkeypatch.setenv("MADO_PRICING_MODE", "api_metered")
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    assert sink.records[0].pricing_mode is PricingMode.API_METERED


def test_attribution_never_reads_anything_the_model_wrote(transcript, monkeypatch):
    """The point of Pillar 4's env injection: a transcript is model output, and a
    producer that trusted it would let the model choose its own attribution."""
    monkeypatch.setenv("MADO_ISSUE_KEY", "TOKWEIR-7")
    transcript.append(
        transcript_entry(
            message_id="msg_a",
            usage=usage(10, 1),
            workload="MADE-UP-BY-THE-MODEL",
            queue="also-made-up",
            app_id="impersonated",
        )
    )
    sink = RecordingSink()

    run(hook_stdin(transcript), into(sink))

    record = sink.records[0]
    assert record.workload == "TOKWEIR-7"
    assert record.queue is None
    assert record.app_id == DEFAULT_APP_ID


# ===========================================================================
# User Story 4 — installable and documented
# ===========================================================================


def test_packaging_resolves_the_console_script_entry_point():
    """FR-032. Claude Code's `settings.json` names a command, so the hook has to
    *be* one — and a declaration sitting in `pyproject.toml` is a different claim
    from an entry point the installed distribution can actually hand back. This
    asserts the second one, through the metadata a console script is built from."""
    from importlib.metadata import entry_points

    scripts = {
        ep.name: ep
        for ep in entry_points(group="console_scripts")
        if ep.name == "tokenweir-claude-code-hook"
    }
    assert scripts, "the hook's console script is not installed"

    resolved = scripts["tokenweir-claude-code-hook"].load()
    assert callable(resolved)
    assert resolved is main


def test_packaging_keeps_the_module_free_of_transport_imports():
    """FR-031, SC-008. The core stays dependency-light by contract, and this
    module lives *in* the core — so it must not be the thing that drags a driver
    in. Checked in a subprocess: this session has already imported plenty."""
    import subprocess

    code = (
        "import sys; import tokenweir.claude_code; "
        "assert 'pika' not in sys.modules, 'pika was imported'; "
        "assert 'psycopg' not in sys.modules, 'psycopg was imported'; "
        "print('clean')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_packaging_runs_as_a_module_and_exits_zero(transcript):
    """FR-032, and the belt-and-braces version of SC-003: the *real process*,
    with a real broken configuration, really exits 0."""
    import subprocess

    transcript.append(transcript_entry(message_id="msg_a", usage=usage(10, 1)))
    env = dict(os.environ)
    env["TOKENWEIR_AMQP_URL"] = "amqp://nowhere.invalid:5672/"
    payload = json.dumps(
        {"session_id": "s", "transcript_path": str(transcript), "hook_event_name": "Stop"}
    )

    result = subprocess.run(
        [sys.executable, "-m", "tokenweir.claude_code"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )

    assert result.returncode == 0
    assert result.stdout == "", "a hook's stdout is parsed by Claude Code"


def test_packaging_documents_a_non_blocking_settings_fragment():
    """FR-033. ADR-0001 forbids `exit 2` and asks for a short timeout; the
    documented fragment is what a reader will actually paste."""
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "tokenweir-claude-code-hook" in readme
    assert '"Stop"' in readme
    assert '"timeout"' in readme


def test_packaging_reads_the_transcript_incrementally(transcript, monkeypatch):
    """FR-009, asserted as a mechanism. A session's transcript grows all day, and
    a hook whose memory is a function of how long the developer has been working
    is a hook that gets killed on the longest sessions — the ones worth metering
    most.

    Round-tripping counts cannot catch a regression here: `handle.read().split()`
    produces identical numbers. So the file handle itself refuses to be read
    whole."""
    transcript.append(
        *[transcript_entry(message_id=f"m{n}", usage=usage(1)) for n in range(200)]
    )

    class IterationOnly:
        """A handle that yields lines but refuses to be materialized."""

        def __init__(self, handle):
            self._handle = handle

        def __iter__(self):
            return iter(self._handle)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._handle.close()
            return False

        def close(self):
            self._handle.close()

        def read(self, *args, **kwargs):
            raise AssertionError("scan_transcript materialized the whole transcript")

        def readlines(self, *args, **kwargs):
            raise AssertionError("scan_transcript materialized the whole transcript")

    real_open = open
    target = str(transcript)

    def guarded_open(file, *args, **kwargs):
        handle = real_open(file, *args, **kwargs)
        return IterationOnly(handle) if str(file) == target else handle

    monkeypatch.setattr("builtins.open", guarded_open)

    result = scan_transcript(transcript)

    assert result.counted == 200
    assert result.totals.input_tokens == 200


def test_counts_give_distinct_turns_distinct_request_ids(transcript):
    """FR-019's actual protection: two different turns must never share an id."""
    sink = RecordingSink()
    for n in range(4):
        transcript.append(
            transcript_entry(message_id=f"msg_{n}", usage=usage(10 * (n + 1)))
        )
        run(hook_stdin(transcript), into(sink))

    ids = [r.request_id for r in sink.records]
    assert len(ids) == 4
    assert len(set(ids)) == 4, f"distinct turns shared an id: {ids}"


def test_counts_do_not_inherit_a_previous_turns_id_for_an_unidentifiable_entry(
    transcript,
):
    """FR-019, on the path the scan's own fallback creates.

    An entry carrying neither `message.id` nor `uuid` is counted (it cannot be
    shown to be a duplicate). If the *last* such entry also supplied the record's
    identity by inheritance, a second turn would be stamped with the first turn's
    id — two distinct turns sharing an identity, which is worse than the uuid
    fallback: a consumer collapsing duplicate ids, which is exactly the remedy
    FR-019 promises, would delete the second turn's tokens outright.
    """

    def anonymous(tokens):
        entry = transcript_entry(message_id=None, usage=usage(tokens))
        entry.pop("uuid")
        return entry

    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    sink = RecordingSink()
    run(hook_stdin(transcript), into(sink))

    transcript.append(anonymous(50))
    run(hook_stdin(transcript), into(sink))

    assert len(sink.records) == 2
    assert counts_of(sink.records[1]) == (50, 0, 0, 0)
    assert sink.records[1].request_id != sink.records[0].request_id, (
        "a new turn inherited the previous turn's request_id"
    )


def test_counts_re_report_a_turn_under_the_same_id_when_the_baseline_is_lost(
    transcript, tmp_path, monkeypatch
):
    """The other half of FR-019, and the honest reading of an unwritable state
    directory (FR-017).

    The hook must still emit — a metering cache is not a reason to lose a turn —
    so the same turn is re-reported until the baseline can be stored. Those
    re-reports are exact duplicates and they carry the **same** `request_id`,
    deliberately: a fresh id per emission would make them look like distinct turns
    and turn a visible duplicate into invisible inflation. The store puts no unique
    constraint on the column precisely so a consumer can collapse them.
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("TOKENWEIR_HOOK_STATE_DIR", str(blocked / "state"))
    transcript.append(transcript_entry(message_id="msg_a", usage=usage(100, 10)))
    sink = RecordingSink()

    try:
        # A static transcript: the same turn, re-reported.
        for _ in range(2):
            run(hook_stdin(transcript), into(sink))
    finally:
        blocked.chmod(0o700)

    assert len(sink.records) == 2, "an unwritable cache must not cost a turn its record"
    assert {r.request_id for r in sink.records} == {"msg_a"}
    assert {counts_of(r) for r in sink.records} == {(100, 10, 0, 0)}


def test_counts_inflate_silently_when_the_baseline_cannot_be_stored_on_a_live_session(
    transcript, tmp_path, monkeypatch
):
    """The honest version of the case above, and the one that actually happens.

    On a *growing* transcript the re-reports are not duplicates of anything. Each
    covers a longer span and ends on a different response, so the ids differ and
    the counts climb — three turns of 100 report 100, 300, 600. No choice of
    identifier rescues this, and nothing marks the records as re-reports.

    This test exists to stop the reassuring version of the story being told again:
    it pins the real behaviour so that any claim about collapsible duplicates has
    to be reconciled with it. A persistently unwritable state directory is a broken
    deployment, not a tolerated one.
    """
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    blocked.chmod(0o500)
    monkeypatch.setenv("TOKENWEIR_HOOK_STATE_DIR", str(blocked / "state"))
    sink = RecordingSink()

    try:
        for n in range(3):
            transcript.append(
                transcript_entry(message_id=f"msg_{n}", usage=usage(100))
            )
            run(hook_stdin(transcript), into(sink))
    finally:
        blocked.chmod(0o700)

    assert [r.request_id for r in sink.records] == ["msg_0", "msg_1", "msg_2"]
    assert [r.input_tokens for r in sink.records] == [100, 200, 300]
    assert sum(r.input_tokens for r in sink.records) == 600, (
        "the true session total is 300; this is the inflation, recorded as real"
    )
    assert len({r.request_id for r in sink.records}) == 3, (
        "these are not collapsible duplicates and must not be described as such"
    )


def test_select_sink_uses_the_broker_path_when_an_amqp_url_is_configured(
    monkeypatch,
):
    """FR-030's first branch — the homelab's production transport.

    Without this, replacing the whole AMQP branch with `return NullSink()` leaves
    the suite green: the only other AMQP test asserts the *failure* degradation,
    which passes trivially in an environment with no `pika` because `from_url`
    raises there anyway. Monkeypatching the factory is what makes the branch
    observable without a driver or a broker."""
    import tokenweir.amqp

    class StubSink:
        def emit(self, record):
            pass

        def close(self):
            pass

    stub = StubSink()
    monkeypatch.setattr(
        tokenweir.amqp.AMQPSink,
        "from_url",
        classmethod(lambda cls, url, **kwargs: stub),
    )
    monkeypatch.setenv("TOKENWEIR_AMQP_URL", "amqp://broker.example/")

    assert select_sink().sink is stub
    assert select_sink().degraded is False


def test_select_sink_prefers_the_broker_when_both_transports_are_configured(
    monkeypatch,
):
    """A deployment that configured a broker said where records should survive an
    outage; writing past it to the store would quietly discard that."""
    import tokenweir.amqp
    import tokenweir.postgres

    class StubSink:
        def emit(self, record):
            pass

        def close(self):
            pass

    stub = StubSink()
    monkeypatch.setattr(
        tokenweir.amqp.AMQPSink, "from_url", classmethod(lambda cls, url, **kw: stub)
    )
    monkeypatch.setattr(
        tokenweir.postgres.PostgresSource,
        "from_dsn",
        classmethod(lambda cls, dsn, **kw: pytest.fail("the store was chosen over the broker")),
    )
    monkeypatch.setenv("TOKENWEIR_AMQP_URL", "amqp://broker.example/")
    monkeypatch.setenv("TOKENWEIR_DSN", "postgresql://example/db")

    assert select_sink().sink is stub
    assert select_sink().degraded is False


def test_select_sink_uses_the_direct_path_when_a_dsn_is_configured(monkeypatch):
    """FR-030's second branch. Asserted without a driver or a server: the question
    is which sink the environment selects, and `PostgresSource.from_dsn` is the
    seam where the driver would be needed."""
    import tokenweir.postgres
    from tokenweir.sink import DirectSink

    class FakeSource:
        def write(self, records):
            return len(records)

        def close(self):
            pass

    monkeypatch.setattr(
        tokenweir.postgres.PostgresSource,
        "from_dsn",
        classmethod(lambda cls, dsn, **kwargs: FakeSource()),
    )
    monkeypatch.setenv("TOKENWEIR_DSN", "postgresql://example/db")

    choice = select_sink()

    assert isinstance(choice.sink, DirectSink)
    assert isinstance(choice.sink.source, FakeSource)
    assert choice.degraded is False


# ===========================================================================
# The small pieces, tested directly
# ===========================================================================


def test_token_totals_regression_is_detected_per_field():
    """A *partial* regression — one field lower, three higher — cannot happen to
    an append-only file, so it means the file was replaced."""
    baseline = TokenTotals(10, 10, 10, 10)
    assert TokenTotals(10, 10, 10, 10).covers(baseline)
    assert TokenTotals(11, 11, 11, 11).covers(baseline)
    assert not TokenTotals(11, 11, 11, 9).covers(baseline)
    assert turn_delta(TokenTotals(11, 11, 11, 9), baseline) is None
    assert turn_delta(TokenTotals(15, 12, 10, 10), baseline) == TokenTotals(5, 2, 0, 0)


def test_read_hook_input_extracts_what_the_hook_needs():
    payload = json.dumps(
        {"session_id": "s-1", "transcript_path": "/tmp/t.jsonl", "extra": "ignored"}
    )
    parsed = read_hook_input(io.StringIO(payload))
    assert parsed.transcript_path == "/tmp/t.jsonl"
    assert parsed.session_id == "s-1"


def test_scan_of_a_missing_transcript_is_empty_rather_than_an_error(tmp_path):
    result = scan_transcript(tmp_path / "does-not-exist.jsonl")
    assert result.totals == TokenTotals()
    assert result.counted == 0


def test_build_fields_produces_a_constructible_record(transcript):
    """The fields the module assembles must satisfy the contract's own
    validation — which is the check that catches a required field going blank."""
    from tokenweir.claude_code import HookInput

    fields = build_fields(
        TokenTotals(1, 2, 3, 4),
        scan_transcript(transcript),
        HookInput(transcript_path=str(transcript), session_id="s"),
    )
    record = UsageRecord(**fields)
    assert counts_of(record) == (1, 2, 3, 4)


def test_select_sink_defaults_to_a_no_op():
    """An unconfigured hook is a no-op, not an error."""
    from tokenweir.sink import NullSink

    choice = select_sink()
    assert isinstance(choice.sink, NullSink)
    assert choice.degraded is False, (
        "an unconfigured hook is discarding by choice, not by failure"
    )
