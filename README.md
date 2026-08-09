# token-weir

Provider-neutral, transport-agnostic **usage & cost metering** — a standalone,
open-source component extracted from the AI Gateway's observability pipeline
(see `docs/adr-0001-token-weir.md`).

The core defines three seams and nothing about the wire:

- **`tokenweir.contract`** — a versioned, serializable `UsageRecord`. Raw token
  counts are stored; cost is computed at report time.
- **`tokenweir.sink`** — the emit side (`Sink`). Fire-and-forget, off the
  critical path; a metering outage never affects the metered system.
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
non-blank; token counts default to `0` and must be non-negative integers. An
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

## Develop

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

Apache-2.0. Metering is intentionally the open-core boundary (ADR-0002).
