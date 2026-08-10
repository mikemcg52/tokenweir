# token-weir

Provider-neutral, transport-agnostic **usage & cost metering** — a standalone,
open-source component extracted from the AI Gateway's observability pipeline
(see `docs/adr-0001-token-weir.md`).

The core defines three seams and nothing about the wire:

- **`tokenweir.contract`** — a versioned, serializable `UsageRecord`. Raw token
  counts are stored; cost is computed at report time.
- **`tokenweir.sink`** — the emit side (`Sink`). Fire-and-forget, off the
  critical path; a metering outage never affects the metered system. Also home to
  the [guarded seam](#metering-on-a-request-path) that keeps record *construction*
  off that critical path too.
- **`tokenweir.source`** — the write side (`Source`). Persists records to a store.

Transport lives in optional adapters so the core stays dependency-light:

```bash
pip install tokenweir           # core only
pip install 'tokenweir[amqp]'   # + AMQP adapter
```

## The usage-record contract

A `UsageRecord` is one metered unit of work — a single model call or iteration.
It is provider-neutral: `model` is an opaque identifier stored verbatim, so a
local Ollama model is described exactly like a Claude one.

```python
from tokenweir import PricingMode, UsageRecord

rec = UsageRecord(
    request_id="req-42",
    app_id="mado",
    endpoint="/v1/messages",
    model="claude-opus-5",
    status="ok",
    workload="review",
    input_tokens=1200,
    output_tokens=340,
    latency_ms=1234,
    pricing_mode=PricingMode.SUBSCRIPTION,
)

wire = rec.to_json()                     # JSON is part of the contract
assert UsageRecord.from_json(wire) == rec  # transport is not
```

`request_id`, `app_id`, `endpoint`, `model` and `status` are required and must be
non-blank. `workload`, `parent_request_id`, `queue` and `ts` are optional strings —
type-checked, but *not* blank-checked, so `""` is legal for them where it is not
for an identity field. Token counts default to `0` and must be non-negative
integers, and `schema_version` must be a positive integer. Strings are stored
verbatim: nothing is lower-cased, trimmed or otherwise normalized. An
unattributable record is a producer-side bug, and failing loudly beats metering
garbage — so construction validates, and does so with two error types by design:

| Mistake | Raises |
|---|---|
| Omitting a required argument | `TypeError` — Python's own signature check. The required fields have no sentinel defaults, so type checkers and IDEs catch it before runtime. |
| Supplying an invalid value (blank, negative, wrong type) | `ValueError` |

At the wire boundary the distinction disappears: `from_dict` and `from_json` raise
`ValueError` for a missing *or* invalid field, so code parsing untrusted payloads
catches one type.

This is separate from `Sink.emit`, which must still never raise into the caller.
Construction raising is safe to do on a request path *because* of the guarded seam
below — the two decisions are meant to be read together: validation stays loud, and
the caller on the critical path is given a supported way not to be hurt by it.

Records are **immutable** — validation runs once, at construction, so the fields
cannot afterwards be mutated into a state that breaks the contract, and a record
handed to a buffering fire-and-forget `Sink` is not shared mutable state. Build a
variant with `dataclasses.replace`, which re-runs validation:

```python
import dataclasses
stamped = dataclasses.replace(rec, ts="2026-08-09T12:00:00Z")
```

Reading from the wire is slightly more forgiving than the Python constructor, and
deliberately so: JSON has a single number type, so `{"input_tokens": 100.0}` means
the integer 100 and `from_dict`/`from_json` normalize it. A genuinely fractional
value like `100.5` is still rejected rather than truncated.

### Pricing modes

`pricing_mode` tells a report-time consumer whether a rate card even applies
(ADR-0001 Pillar 4). It accepts either the enum member or its wire string, and
serializes as the plain string:

| Value | Meaning |
|---|---|
| `api_metered` | Captured by interception at the Anthropic-compatible `/v1/messages` edge. Cost is derivable from a rate card. |
| `subscription` | Captured from Claude Code's transcript under a flat-rate Max subscription. Raw counts are meaningful; no per-call dollar exists. |

It may also be left unset, for a producer that does not know its billing mode.

### Versioning

Every record carries a `schema_version`, and `SCHEMA_VERSION` changes in lockstep
with any field change. Deserialization **ignores unknown fields**, so a record from
a newer producer still reads on an older consumer, and it **preserves the payload's
`schema_version`** rather than substituting the reading library's — a consumer can
always tell what it actually received.

For consumers that are not Python, the contract is published as JSON Schema at
[`schema/usage-record.v1.json`](schema/usage-record.v1.json), generated from
`tokenweir.contract.usage_record_json_schema()`.

Two things to know about it:

- **It is a strict v1 validator, deliberately stricter than the Python type.**
  `schema_version` is pinned with `const: 1`, so a v2 record fails validation
  against the v1 document even though `from_dict` reads it happily. The schema
  answers "is this a v1 record I fully understand?"; a consumer wanting the
  tolerant behaviour should read the version field rather than validate. A future
  version ships its own `usage-record.vN.json`.
- **It is a repository artifact, not part of the wheel.** `pip install tokenweir`
  does not carry it — fetch it from the repo, or generate it in-process by calling
  `usage_record_json_schema()`.

The test suite fails if the checked-in file drifts from the code; regenerate it
with:

```bash
python -c "import json;from tokenweir import usage_record_json_schema as s;\
print(json.dumps(s(), indent=2))" > schema/usage-record.v1.json
```

## Metering on a request path

ADR-0001 Pillar 2 promises that metering can never affect the availability of the
metered system. The `Sink.emit` contract discharges that promise for the *emit*
step — but record **construction** is not `Sink.emit`, and it validates, so a
producer building a record inline on a request path would take a `ValueError`
into the request it is metering.

`tokenweir` therefore ships the guard rather than leaving every consumer to
hand-roll it. A malformed record degrades to **"no metering for this call"**:

```python
from tokenweir import emit_usage

# On the request path. Never raises; returns None if nothing was metered.
emit_usage(
    sink,
    request_id=req.id,
    app_id="mado",
    endpoint="/v1/messages",
    model=resp.model,
    status="ok",
    input_tokens=resp.usage.input_tokens,
    output_tokens=resp.usage.output_tokens,
)
```

The two halves are exposed for callers that must stamp or enrich a record in
between — a gateway computes `latency_ms` only after the metered call returns,
and a batching emitter builds now and emits later:

```python
from tokenweir import build_record, emit_record
import dataclasses

record = build_record(**fields)          # None if the values were invalid
if record is not None:
    record = dataclasses.replace(record, latency_ms=elapsed_ms)
    emit_record(sink, record)            # False if the sink raised
```

| Call | On success | On failure |
|---|---|---|
| `build_record(**fields)` | the `UsageRecord` | `None` |
| `emit_record(sink, record)` | `True` | `False` |
| `emit_usage(sink, **fields)` | the `UsageRecord` | `None` |

`emit_usage` is exactly `build_record` followed by `emit_record`, so there is one
implementation of each guarantee rather than two.

A record is refused before the sink sees it if it is not a `UsageRecord` — so the
careless composition, without the `is not None` check above, drops rather than
persisting `None`.

**Drops are never silent.** Each one logs a `WARNING` on the `tokenweir.sink`
logger carrying the original exception — which already names the offending field —
and the return value lets a caller count drops without parsing logs.

Handler policy stays the application's: the package attaches a `NullHandler` to
the `tokenweir` logger and sets no level, per the standard library's guidance for
libraries. The consequence is worth stating plainly — an application that has
configured no logging at all sees nothing, and gets the warnings with one line of
`logging.basicConfig()`. The alternative was writing a traceback per dropped
record to the stderr of an application that never asked for output. The return
value is the signal that reaches a caller either way.

Two things the guard deliberately does *not* do:

- **It does not soften the contract.** `UsageRecord(...)` still validates and still
  raises. Off a request path — a batch import, a migration, a test — construct
  directly and let a producer bug be loud. That is the correct behaviour there, and
  the guard is the wrong tool.
- **It does not relax the `Sink` protocol.** Implementations still MUST NOT raise
  from `emit`. `emit_record` guards emission because the party harmed by a
  *non-conforming* adapter is the request on the critical path, which should not
  have to depend on every adapter in the ecosystem being correct.

`KeyboardInterrupt` and `SystemExit` are not caught. A metering guard that
swallowed Ctrl-C would be a worse bug than the one it fixes.

> **Adoption is in progress.** `tokenweir` ships the seam; the AI Gateway
> (TOKWEIR-10) and the emitter client (TOKWEIR-6) are being moved onto it. Until
> then, a consumer constructing records inline should call `emit_usage` itself.

## Develop

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

Apache-2.0. Metering is intentionally the open-core boundary (ADR-0002).
