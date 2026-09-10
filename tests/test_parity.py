"""The token-parity harness (TOKWEIR-9).

Every test here runs **offline and with no credential** (FR-008, FR-015). That is
a requirement of the spec, not a convenience: the harness's job is to produce a
measurement, and a suite that needed the network to check its arithmetic would be
unrunnable in exactly the checkout most likely to re-run the measurement later.

The network paths are exercised through an injected opener. The one live call in
this story happened once, deliberately, when the finding was produced — a test
suite is not the place to spend metered budget, and a live call would also make
these assertions depend on a provider's uptime.

Transcripts are synthesized into `tmp_path`. The real transcript in the pod is the
*measurement input*, never a fixture: a test that read a developer's own session
history would pass or fail according to what they happened to have done that week.
"""

import json
import urllib.error
from pathlib import Path

import pytest

from tokenweir.parity import (
    ABSENT,
    FAIL,
    NOT_APPLICABLE,
    OFFSET,
    PARITY,
    PASS,
    ApiProbe,
    ProbeError,
    compare_usage,
    consistency_checks,
    credential,
    iter_turns,
    main,
)

# --- helpers ---------------------------------------------------------------


def write_transcript(path: Path, entries) -> Path:
    """Write JSONL, one entry per line, plus the noise a real log carries."""
    with path.open("w", encoding="utf-8") as handle:
        for entry in entries:
            if isinstance(entry, str):
                handle.write(entry + "\n")
            else:
                handle.write(json.dumps(entry) + "\n")
    return path


def assistant(
    message_id="msg_1",
    *,
    usage=None,
    content=None,
    model="claude-opus-5",
    timestamp="2026-09-10T00:00:00Z",
):
    return {
        "timestamp": timestamp,
        "message": {
            "id": message_id,
            "model": model,
            "usage": usage if usage is not None else {"input_tokens": 1, "output_tokens": 2},
            "content": content if content is not None else [{"type": "text", "text": "hello"}],
        },
    }


def usage(
    input_tokens=2,
    output_tokens=10,
    cache_creation=0,
    cache_read=0,
    *,
    breakdown=None,
    iterations=None,
    extra=None,
):
    out = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
    }
    if breakdown is not None:
        out["cache_creation"] = breakdown
    if iterations is not None:
        out["iterations"] = iterations
    if extra:
        out.update(extra)
    return out


# --- iter_turns ------------------------------------------------------------


class TestIterTurns:
    def test_yields_one_turn_per_response(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [assistant("msg_1"), assistant("msg_2")],
        )
        assert [t.message_id for t in iter_turns(path)] == ["msg_1", "msg_2"]

    def test_deduplicates_on_message_id(self, tmp_path):
        """The hazard `claude_code` documents: one response, several lines.

        Every repeated line is individually well-formed, so a comparison that
        counted them twice would be wrong without looking wrong.
        """
        path = write_transcript(
            tmp_path / "t.jsonl",
            [assistant("msg_1"), assistant("msg_1"), assistant("msg_1")],
        )
        turns = list(iter_turns(path))
        assert len(turns) == 1
        assert turns[0].message_id == "msg_1"

    def test_usage_is_counted_once_across_sibling_lines(self, tmp_path):
        """Usage is taken once per response, not summed over its lines."""
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant("msg_1", usage=usage(output_tokens=500)),
                assistant("msg_1", usage=usage(output_tokens=500)),
            ],
        )
        turns = list(iter_turns(path))
        assert len(turns) == 1
        assert turns[0].usage["output_tokens"] == 500

    def test_content_is_merged_across_sibling_lines(self, tmp_path):
        """The regression test for the bug the real measurement exposed.

        Sibling lines sharing a `message.id` do not *repeat* the content — they
        *divide* it. A real response is written as one line carrying its `text`
        block and another carrying its `tool_use` block, while both carry the whole
        response's aggregate `output_tokens`.

        The first version of this harness kept the first line and dropped the rest,
        so it paired 20 characters of text with the token count for a 37 KB tool
        call, reported a transcript claiming 14,142 output tokens against a
        tokenizer count of 12, and read its own bug as a parity failure.

        Two things must hold, and the second is what actually broke: the text is
        concatenated across every line, and `has_unaccounted_blocks` is true if *any*
        line carried one. Per-line inspection could never set that flag, because
        each individual line holds exactly one kind of block.
        """
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_1",
                    usage=usage(output_tokens=14142),
                    content=[{"type": "text", "text": "Now the test module:"}],
                ),
                assistant(
                    "msg_1",
                    usage=usage(output_tokens=14142),
                    content=[
                        {"type": "tool_use", "id": "tu_1", "name": "Write", "input": {"x": "y"}}
                    ],
                ),
            ],
        )
        turns = list(iter_turns(path))
        assert len(turns) == 1
        assert turns[0].output_text == "Now the test module:"
        assert turns[0].has_unaccounted_blocks is True, (
            "the tool_use sibling was dropped; this turn would be treated as "
            "text-only and wreck Probe A's headline"
        )

    def test_text_from_several_lines_is_concatenated_in_order(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant("msg_1", content=[{"type": "text", "text": "first "}]),
                assistant("msg_1", content=[{"type": "text", "text": "second"}]),
            ],
        )
        turns = list(iter_turns(path))
        assert [t.output_text for t in turns] == ["first second"]

    def test_entries_without_an_id_are_not_merged_together(self, tmp_path):
        """Two unrelated responses that both lack an id must stay two responses."""
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                {"message": {"usage": usage(), "content": [{"type": "text", "text": "a"}]}},
                {"message": {"usage": usage(), "content": [{"type": "text", "text": "b"}]}},
            ],
        )
        turns = list(iter_turns(path))
        assert [t.output_text for t in turns] == ["a", "b"]
        assert [t.message_id for t in turns] == [None, None]

    def test_skips_malformed_and_usageless_entries(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                "{not json",
                "",
                json.dumps([1, 2, 3]),
                {"message": {"id": "no_usage"}},
                {"message": {"id": "bad_usage", "usage": "not a mapping"}},
                {"no_message": True},
                assistant("msg_ok"),
            ],
        )
        assert [t.message_id for t in iter_turns(path)] == ["msg_ok"]

    def test_assembles_text_from_multiple_blocks(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    content=[
                        {"type": "text", "text": "one "},
                        {"type": "text", "text": "two"},
                    ]
                )
            ],
        )
        turn = next(iter(iter_turns(path)))
        assert turn.output_text == "one two"
        assert turn.has_unaccounted_blocks is False

    def test_tool_use_is_unaccounted_but_thinking_is_not(self, tmp_path):
        """The distinction the whole headline rests on.

        A `tool_use` block's tokens count toward `output_tokens` and its content is
        a JSON payload this harness cannot re-tokenize faithfully — so a turn
        carrying one is excluded rather than averaged in.

        A `thinking` block is different: its content *is* recoverable, and the
        provider reports its tokens separately as
        `output_tokens_details.thinking_tokens`. Keeping it accounted is what lets
        that field be checked rather than trusted, and treating it as unaccounted
        would have thrown away the 164 thinking turns this session's transcripts
        actually contain.
        """
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_tool",
                    content=[
                        {"type": "text", "text": "calling"},
                        {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {}},
                    ],
                ),
                assistant(
                    "msg_think",
                    content=[
                        {"type": "thinking", "thinking": "hmm"},
                        {"type": "text", "text": "answer"},
                    ],
                ),
            ],
        )
        turns = list(iter_turns(path))
        assert [t.has_unaccounted_blocks for t in turns] == [True, False]
        assert [t.output_text for t in turns] == ["calling", "answer"]
        assert [t.thinking_text for t in turns] == ["", "hmm"]

    def test_redacted_thinking_is_unaccounted(self, tmp_path):
        """It has tokens and no recoverable content — exactly what the flag is for."""
        path = write_transcript(
            tmp_path / "t.jsonl",
            [assistant(content=[{"type": "redacted_thinking", "data": "opaque"}])],
        )
        turn = next(iter(iter_turns(path)))
        assert turn.has_unaccounted_blocks is True
        assert turn.thinking_text == ""

    def test_thinking_text_merges_across_lines(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant("msg_1", content=[{"type": "thinking", "thinking": "step one "}]),
                assistant("msg_1", content=[{"type": "thinking", "thinking": "step two"}]),
                assistant("msg_1", content=[{"type": "text", "text": "done"}]),
            ],
        )
        turns = list(iter_turns(path))
        assert len(turns) == 1
        assert turns[0].thinking_text == "step one step two"
        assert turns[0].output_text == "done"
        assert turns[0].has_unaccounted_blocks is False

    def test_a_block_whose_content_is_not_a_string_is_unaccounted(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant("msg_a", content=[{"type": "text", "text": {"not": "a string"}}]),
                assistant("msg_b", content=[{"type": "thinking", "thinking": 42}]),
                assistant("msg_c", content=["not a mapping"]),
            ],
        )
        assert [t.has_unaccounted_blocks for t in iter_turns(path)] == [True, True, True]

    def test_thinking_tokens_reads_the_claim_or_zero(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_a",
                    usage=usage(extra={"output_tokens_details": {"thinking_tokens": 25}}),
                ),
                assistant("msg_b", usage=usage(extra={"output_tokens_details": {}})),
                assistant("msg_c", usage=usage()),
                assistant("msg_d", usage=usage(extra={"output_tokens_details": "not a mapping"})),
            ],
        )
        assert [t.thinking_tokens for t in iter_turns(path)] == [25, 0, 0, 0]

    def test_string_content_is_taken_at_face_value(self, tmp_path):
        path = write_transcript(tmp_path / "t.jsonl", [assistant(content="plain string")])
        turn = next(iter(iter_turns(path)))
        assert turn.output_text == "plain string"
        assert turn.has_unaccounted_blocks is False

    def test_unreadable_file_yields_nothing_rather_than_raising(self, tmp_path):
        assert list(iter_turns(tmp_path / "does-not-exist.jsonl")) == []

    def test_carries_model_and_timestamp(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [assistant(model="claude-opus-5", timestamp="2026-09-10T12:00:00Z")],
        )
        turn = next(iter(iter_turns(path)))
        assert turn.model == "claude-opus-5"
        assert turn.timestamp == "2026-09-10T12:00:00Z"


# --- compare_usage ---------------------------------------------------------


class TestCompareUsage:
    def _by_name(self, results):
        return {r.name: r for r in results}

    def test_exact_agreement_is_parity(self):
        results = self._by_name(compare_usage(usage(), usage()))
        for name in ("input_tokens", "output_tokens"):
            assert results[name].status == PARITY
            assert results[name].difference == 0

    def test_difference_reports_delta_and_ratio(self):
        results = self._by_name(
            compare_usage(usage(output_tokens=10), usage(output_tokens=15))
        )
        result = results["output_tokens"]
        assert result.status == OFFSET
        assert result.difference == 5
        assert result.ratio == pytest.approx(1.5)

    def test_absent_on_either_side_is_never_parity(self):
        """Two absences are not an agreement about a count (FR-004).

        Without this, comparing two empty usage objects would report a clean
        sweep of parity — the most misleading output the harness could produce.
        """
        results = self._by_name(compare_usage({}, {}))
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            assert results[name].status == ABSENT

        one_sided = self._by_name(compare_usage({"input_tokens": 5}, {}))
        assert one_sided["input_tokens"].status == ABSENT
        assert one_sided["input_tokens"].reference == 5
        assert one_sided["input_tokens"].observed is None

    def test_billing_fields_always_reported(self):
        """Total over the four fields even when neither side carries them (FR-005)."""
        names = [r.name for r in compare_usage({}, {})]
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        ):
            assert name in names

    def test_one_sided_extra_field_is_surfaced(self):
        results = self._by_name(compare_usage({}, {"service_tier": "standard"}))
        assert "service_tier" in results
        assert results["service_tier"].status == ABSENT
        assert results["service_tier"].observed == "standard"

    def test_non_numeric_fields_compare_by_equality_without_arithmetic(self):
        same = self._by_name(
            compare_usage({"service_tier": "standard"}, {"service_tier": "standard"})
        )
        assert same["service_tier"].status == PARITY
        assert same["service_tier"].difference is None
        assert same["service_tier"].ratio is None

        differs = self._by_name(
            compare_usage({"service_tier": "standard"}, {"service_tier": "priority"})
        )
        assert differs["service_tier"].status == OFFSET

    def test_zero_reference_does_not_divide(self):
        results = self._by_name(
            compare_usage(usage(input_tokens=0), usage(input_tokens=7))
        )
        result = results["input_tokens"]
        assert result.status == OFFSET
        assert result.difference == 7
        assert result.ratio is None

    def test_bool_is_not_treated_as_a_count(self):
        """`True == 1` in Python, and a parity measurement must not manufacture
        agreement out of a type coincidence."""
        results = self._by_name(compare_usage({"input_tokens": 1}, {"input_tokens": True}))
        assert results["input_tokens"].status == OFFSET
        assert results["input_tokens"].difference is None


# --- consistency_checks (Probe C) ------------------------------------------


class TestConsistencyChecks:
    def _by_name(self, turns):
        return {r.name: r for r in consistency_checks(turns)}

    def _turns(self, tmp_path, entries):
        return list(iter_turns(write_transcript(tmp_path / "t.jsonl", entries)))

    def test_cache_creation_sums_pass_and_fail(self, tmp_path):
        good = self._turns(
            tmp_path,
            [
                assistant(
                    "m1",
                    usage=usage(
                        cache_creation=100,
                        breakdown={
                            "ephemeral_5m_input_tokens": 40,
                            "ephemeral_1h_input_tokens": 60,
                        },
                    ),
                )
            ],
        )
        assert self._by_name(good)["cache_creation_sums"].status == PASS

        bad = self._turns(
            tmp_path,
            [
                assistant(
                    "m1",
                    usage=usage(
                        cache_creation=100,
                        breakdown={
                            "ephemeral_5m_input_tokens": 40,
                            "ephemeral_1h_input_tokens": 55,
                        },
                    ),
                )
            ],
        )
        result = self._by_name(bad)["cache_creation_sums"]
        assert result.status == FAIL
        assert result.failures and "m1" in result.failures[0]

    def test_cache_creation_not_applicable_without_breakdown(self, tmp_path):
        turns = self._turns(tmp_path, [assistant("m1", usage=usage())])
        assert self._by_name(turns)["cache_creation_sums"].status == NOT_APPLICABLE

    def test_iterations_sum_pass_and_fail(self, tmp_path):
        good = self._turns(
            tmp_path,
            [
                assistant(
                    "m1",
                    usage=usage(
                        input_tokens=2,
                        output_tokens=30,
                        cache_creation=10,
                        cache_read=90,
                        iterations=[
                            {
                                "input_tokens": 1,
                                "output_tokens": 20,
                                "cache_creation_input_tokens": 4,
                                "cache_read_input_tokens": 40,
                            },
                            {
                                "input_tokens": 1,
                                "output_tokens": 10,
                                "cache_creation_input_tokens": 6,
                                "cache_read_input_tokens": 50,
                            },
                        ],
                    ),
                )
            ],
        )
        assert self._by_name(good)["iterations_sum"].status == PASS

        bad = self._turns(
            tmp_path,
            [
                assistant(
                    "m1",
                    usage=usage(
                        output_tokens=30,
                        iterations=[{"input_tokens": 2, "output_tokens": 29}],
                    ),
                )
            ],
        )
        result = self._by_name(bad)["iterations_sum"]
        assert result.status == FAIL
        assert result.failures

    def test_iterations_not_applicable_without_breakdown(self, tmp_path):
        turns = self._turns(tmp_path, [assistant("m1", usage=usage())])
        assert self._by_name(turns)["iterations_sum"].status == NOT_APPLICABLE

    def test_cache_read_monotonic_pass_and_fail(self, tmp_path):
        rising = self._turns(
            tmp_path,
            [
                assistant("m1", usage=usage(cache_read=100)),
                assistant("m2", usage=usage(cache_read=200)),
                assistant("m3", usage=usage(cache_read=200)),
            ],
        )
        assert self._by_name(rising)["cache_read_monotonic"].status == PASS

        falling = self._turns(
            tmp_path,
            [
                assistant("m1", usage=usage(cache_read=200)),
                assistant("m2", usage=usage(cache_read=100)),
            ],
        )
        result = self._by_name(falling)["cache_read_monotonic"]
        assert result.status == FAIL
        assert "200 -> 100" in result.failures[0]

    def test_cache_read_not_applicable_below_two_turns(self, tmp_path):
        turns = self._turns(tmp_path, [assistant("m1", usage=usage(cache_read=5))])
        assert self._by_name(turns)["cache_read_monotonic"].status == NOT_APPLICABLE

    def test_no_turns_is_not_applicable_everywhere(self):
        """A check that could not run did not pass (FR-007)."""
        for result in consistency_checks([]):
            assert result.status == NOT_APPLICABLE

    def test_each_identity_is_reported_independently(self, tmp_path):
        """One failing identity must not mask or imply the others (FR-006)."""
        turns = self._turns(
            tmp_path,
            [
                assistant(
                    "m1",
                    usage=usage(
                        cache_creation=100,
                        cache_read=500,
                        breakdown={"ephemeral_1h_input_tokens": 1},
                    ),
                ),
                assistant("m2", usage=usage(cache_read=600)),
            ],
        )
        results = self._by_name(turns)
        assert results["cache_creation_sums"].status == FAIL
        assert results["cache_read_monotonic"].status == PASS
        assert results["iterations_sum"].status == NOT_APPLICABLE


# --- the credential --------------------------------------------------------


class TestCredential:
    def test_environment_is_preferred(self, tmp_path):
        path = tmp_path / "key"
        path.write_text("from-file", encoding="utf-8")
        assert credential({"ANTHROPIC_API_KEY": "from-env"}, path) == "from-env"

    def test_file_is_read_and_stripped(self, tmp_path):
        """`printf` and a shell `read` differ by exactly a trailing newline, and a
        newline in an HTTP header fails as if it were an auth problem (FR-022)."""
        path = tmp_path / "key"
        path.write_text("  sk-test-value\n", encoding="utf-8")
        assert credential({}, path) == "sk-test-value"

    def test_blank_and_absent_are_the_same_state(self, tmp_path):
        blank = tmp_path / "blank"
        blank.write_text("   \n", encoding="utf-8")
        assert credential({}, blank) is None
        assert credential({}, tmp_path / "missing") is None
        assert credential({}, None) is None
        assert credential({"ANTHROPIC_API_KEY": "  "}, None) is None

    def test_unreadable_file_is_not_configured_rather_than_an_error(self, tmp_path):
        directory = tmp_path / "a-directory"
        directory.mkdir()
        assert credential({}, directory) is None


# --- the network probes ----------------------------------------------------


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingOpener:
    """Captures the request instead of sending it."""

    def __init__(self, payload=None, raises=None):
        self.payload = payload or {"input_tokens": 7}
        self.raises = raises
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append(request)
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.payload)


SECRET = "unit-test-credential-value"


class TestApiProbe:
    def test_requires_a_credential(self):
        with pytest.raises(ValueError):
            ApiProbe("")
        with pytest.raises(ValueError):
            ApiProbe("   ")

    def test_model_is_never_defaulted(self):
        """"The same model" is half the story's comparison condition (FR-012)."""
        probe = ApiProbe(SECRET, opener=RecordingOpener())
        with pytest.raises(ValueError):
            probe.count_tokens("", "text")
        with pytest.raises(ValueError):
            probe.messages("   ", "text")

    def test_count_tokens_sends_the_text_and_returns_the_count(self):
        opener = RecordingOpener({"input_tokens": 42})
        probe = ApiProbe(SECRET, opener=opener)
        result = probe.count_tokens("claude-opus-5", "some output text")

        assert result.usage["input_tokens"] == 42
        assert result.kind == "count_tokens"
        assert result.model == "claude-opus-5"

        request = opener.calls[0]
        body = json.loads(request.data.decode("utf-8"))
        assert body["model"] == "claude-opus-5"
        assert body["messages"] == [{"role": "user", "content": "some output text"}]
        assert request.full_url.endswith("/v1/messages/count_tokens")

    def test_messages_returns_the_usage_object(self):
        opener = RecordingOpener({"usage": {"input_tokens": 9, "output_tokens": 3}})
        probe = ApiProbe(SECRET, opener=opener)
        result = probe.messages("claude-opus-5", "ping")
        assert result.usage == {"input_tokens": 9, "output_tokens": 3}
        assert result.kind == "messages"

    def test_messages_without_usage_is_an_error(self):
        probe = ApiProbe(SECRET, opener=RecordingOpener({"content": []}))
        with pytest.raises(ProbeError):
            probe.messages("claude-opus-5", "ping")

    def test_sends_the_pinned_api_version(self):
        opener = RecordingOpener()
        ApiProbe(SECRET, opener=opener).count_tokens("claude-opus-5", "t")
        headers = {k.lower(): v for k, v in opener.calls[0].header_items()}
        assert headers["anthropic-version"] == "2023-06-01"

    def test_transport_failure_raises_a_composed_error_once(self):
        """One attempt, no retry loop (FR-014). A measurement that silently
        retried would hide a flaky answer inside a definitive-looking number."""
        opener = RecordingOpener(raises=OSError("network unreachable"))
        probe = ApiProbe(SECRET, opener=opener)
        with pytest.raises(ProbeError) as excinfo:
            probe.count_tokens("claude-opus-5", "t")
        assert "count_tokens" in str(excinfo.value)
        assert len(opener.calls) == 1

    def test_http_error_names_the_status(self):
        error = urllib.error.HTTPError(
            "https://api.anthropic.com/v1/messages", 401, "Unauthorized", {}, None
        )
        probe = ApiProbe(SECRET, opener=RecordingOpener(raises=error))
        with pytest.raises(ProbeError) as excinfo:
            probe.messages("claude-opus-5", "t")
        assert "401" in str(excinfo.value)

    def test_non_json_body_is_an_error(self):
        class BadOpener:
            def __call__(self, request, timeout):
                class R:
                    def read(self):
                        return b"<html>nope</html>"

                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                return R()

        probe = ApiProbe(SECRET, opener=BadOpener())
        with pytest.raises(ProbeError) as excinfo:
            probe.count_tokens("claude-opus-5", "t")
        assert "not JSON" in str(excinfo.value)

    @pytest.mark.parametrize(
        "failure",
        [
            OSError(f"connection refused while sending {SECRET}"),
            urllib.error.HTTPError("https://api.anthropic.com/v1", 403, SECRET, {}, None),
        ],
    )
    def test_the_credential_never_reaches_a_raised_message(self, failure):
        """FR-021, defence in depth.

        The key travels in a header and should never reach an exception string —
        but "should never" is an assumption about code this module does not own,
        and the cost of being wrong is a leaked credential in a transcript.
        """
        probe = ApiProbe(SECRET, opener=RecordingOpener(raises=failure))
        with pytest.raises(ProbeError) as excinfo:
            probe.count_tokens("claude-opus-5", "t")
        assert SECRET not in str(excinfo.value)


# --- the command -----------------------------------------------------------


class TestMain:
    def test_reports_partial_run_when_no_credential(self, tmp_path, capsys, monkeypatch):
        """A partial run must never read as a complete one (FR-041)."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant("m1", usage=usage(cache_read=1)),
                assistant("m2", usage=usage(cache_read=2)),
            ],
        )
        code = main(["--transcript", str(path), "--model", "claude-opus-5"])
        out = capsys.readouterr().out

        assert code == 2
        assert "PARTIAL" in out
        assert "Probe A" in out and "SKIPPED" in out
        assert "Probe C" in out

    def test_empty_transcript_is_reported_not_crashed(self, tmp_path, capsys, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        path = write_transcript(tmp_path / "t.jsonl", [])
        code = main(["--transcript", str(path), "--model", "claude-opus-5"])
        assert code == 1
        assert "nothing to measure" in capsys.readouterr().out

    def test_model_is_required(self, tmp_path):
        with pytest.raises(SystemExit):
            main(["--transcript", str(tmp_path / "t.jsonl")])

    def test_credential_is_not_a_positional_argument(self, tmp_path):
        """FR-020: an argument is visible in `ps` and lands in shell history."""
        with pytest.raises(SystemExit):
            main(["--transcript", str(tmp_path / "t.jsonl"), "--model", "m", SECRET])


class TestThinkingIsNotSilentlyAccounted:
    """The second instance of the harness's central hazard, and its guard.

    Claude Code writes `{"type": "thinking", "thinking": ""}` into a transcript
    while still reporting a non-zero `output_tokens_details.thinking_tokens`: the
    content is stripped, the tokens are real. A block like that must count as
    *unaccounted*, because a turn whose output_tokens covers tokens the harness
    cannot re-tokenize is exactly what the headline has to exclude.

    Four real turns in this session's transcripts claimed 71, 176, 366 and 503
    thinking tokens behind an empty thinking block. Had they reached the headline
    they would each have looked like a several-hundred-token parity failure that was
    nothing of the kind.
    """

    def test_empty_thinking_block_is_unaccounted(self, tmp_path):
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_stripped",
                    usage=usage(extra={"output_tokens_details": {"thinking_tokens": 503}}),
                    content=[
                        {"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "the visible answer"},
                    ],
                )
            ],
        )
        turn = next(iter(iter_turns(path)))
        assert turn.has_unaccounted_blocks is True
        assert turn.thinking_tokens == 503

    def test_a_stripped_thinking_turn_never_reaches_the_headline(
        self, tmp_path, capsys, monkeypatch
    ):
        """End to end: the command must not treat such a turn as text-only.

        Asserted through `main` rather than the predicate alone, because the
        headline filter is where the exclusion actually has to happen — a correct
        flag that the filter ignores would still produce a wrong measurement.
        """
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_clean",
                    usage=usage(cache_read=1),
                    content=[{"type": "text", "text": "plain"}],
                ),
                assistant(
                    "msg_stripped",
                    usage=usage(
                        cache_read=2, extra={"output_tokens_details": {"thinking_tokens": 366}}
                    ),
                    content=[
                        {"type": "thinking", "thinking": ""},
                        {"type": "text", "text": "answer"},
                    ],
                ),
            ],
        )
        turns = list(iter_turns(path))
        eligible = [
            t
            for t in turns
            if t.output_text.strip()
            and not t.has_unaccounted_blocks
            and not t.thinking_text
            and t.thinking_tokens == 0
        ]
        assert [t.message_id for t in eligible] == ["msg_clean"]

        assert main(["--transcript", str(path), "--model", "claude-opus-5"]) == 2
        assert "excluded from the headline" not in capsys.readouterr().err

    def test_thinking_tokens_alone_disqualifies_a_turn(self, tmp_path):
        """Even with no thinking block at all, a non-zero claim is disqualifying.

        This is the belt-and-braces half: the filter asks the provider's own number
        rather than inferring from block shape, so a transcript layout nobody has
        seen yet cannot smuggle unrecoverable tokens into the headline.
        """
        path = write_transcript(
            tmp_path / "t.jsonl",
            [
                assistant(
                    "msg_no_block",
                    usage=usage(extra={"output_tokens_details": {"thinking_tokens": 71}}),
                    content=[{"type": "text", "text": "visible only"}],
                )
            ],
        )
        turn = next(iter(iter_turns(path)))
        assert turn.has_unaccounted_blocks is False, "no block is malformed here"
        assert turn.thinking_tokens == 71, "but tokens were spent out of sight"
