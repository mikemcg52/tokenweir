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

Fields may be passed as keywords, as a mapping, or as both — the mapping first,
keyword overrides on top. That is how you stamp a value you only learn after the
metered call returns, without a second, unguarded validating call:

```python
elapsed_ms = ...                          # known only once the call returns
emit_usage(sink, base_fields, latency_ms=elapsed_ms, ts=stamp)
```

**Pass the mapping, don't splat it.** `emit_usage(sink, **mapping)` unpacks in
*your* frame, before any tokenweir code runs, so a mapping that came from JSON or
generic code and picked up a non-string key raises `TypeError: keywords must be
strings` into the request you were metering. `emit_usage(sink, mapping)` unpacks
inside the guard, where it is an ordinary drop.

**The mapping must contain only record fields.** These calls construct a
`UsageRecord`, so an unrecognized key is a drop — every time, not occasionally.
That is the opposite of `UsageRecord.from_dict`, which ignores unknown fields on
purpose so a record from a newer producer still reads. The distinction matters
most for a payload you did not build yourself: if a wire payload gains a field,
handing it straight to `emit_usage` meters *nothing at all*, and with no logging
configured the return value is the only thing that says so. Use `from_dict` to
read a wire payload; use these calls with a mapping of fields you assembled.

The two halves are exposed for a caller that must hold a record between the
steps — a batching emitter builds now and emits later:

```python
from tokenweir import build_record, emit_record

record = build_record(fields)            # None if the values were invalid
...
if record is not None:
    emit_record(sink, record)            # False if the sink raised
```

| Call | On success | On failure |
|---|---|---|
| `build_record(fields, **overrides)` | the `UsageRecord` | `None` |
| `emit_record(sink, record)` | `True` | `False` |
| `emit_usage(sink, fields, **overrides)` | the `UsageRecord` | `None` |

`emit_usage` is exactly `build_record` followed by `emit_record`, so there is one
implementation of each guarantee rather than two.

Every named parameter of these calls is **positional-only** — pass them
positionally, as above. Any parameter reachable by keyword is a name a producer's
own field could collide with during argument binding, which happens before the
function body and so outside every guard. The cost is that `emit_usage(sink=…)`
raises `TypeError` at binding rather than being a supported call shape; it fails
on the first call, so it cannot survive to production.

Note that `dataclasses.replace` **re-runs validation and raises**, so it is the
wrong way to stamp a record on a metered path — build once with the stamp merged,
as above. Off that path it remains the right tool.

A record is refused before the sink sees it if it is not a `UsageRecord` — so the
careless composition, without the `is not None` check, drops rather than
persisting `None`.

**Drops are never silent.** Each one logs a `WARNING` on the `tokenweir.sink`
logger carrying the original exception where one was caught — which already names
the offending field; refusing a non-record is a rejection with no exception to
carry —
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
> then, a consumer constructing records inline should call `emit_usage` itself —
> and should watch the return value, since a producer that gets a field name
> wrong drops *every* record, not some of them.

## The store — schema, migrations and the writer

`tokenweir` **owns** the usage schema. That ownership moved here from the AI
Gateway (ADR-0001 Pillar 5): the gateway pins a `tokenweir` version and no longer
carries DDL, and anything else that reads or writes `gateway_usage` gets its
schema from the same place. The table keeps its name — renaming it would force a
data migration on a live database, and the consumers' contract is that their
existing rows and rollups do not change.

```bash
pip install 'tokenweir[postgres]'
python -m tokenweir.migrations apply --dsn "$TOKENWEIR_DSN"
```

Six forward-only migrations ship **inside the package** — unlike
[`schema/usage-record.v1.json`](#versioning), which is a repository artifact, these
travel in the wheel, because the library has to be able to apply them:

| | |
|---|---|
| `001_gateway_usage` | the append-only usage log; one row per metered unit of work |
| `002_parent_request_id` | attribution for fan-out calls |
| `003_model_pricing_rates` | the effective-dated rate card |
| `004_reader_grant` | `SELECT` for a reporting role |
| `005_gateway_usage_daily` | the daily rollup, and the only place a dollar is produced |
| `006_gateway_usage_app_day_index` | the functional index the rollup needs |

Everything in `tokenweir.migrations` takes a **DB-API connection**, not a DSN, so
a deployment keeps its own pooling, credentials and driver — `connect()` is a
convenience, not a requirement:

```python
from tokenweir.migrations import apply, status

apply(connection, reader_role="metrics_reader")   # returns what it applied
applied, outstanding = status(connection)
```

The rules the extraction preserved, as behaviour rather than convention:

- **Forward-only.** No down-migrations, and a released migration is immutable — a
  change is a new version. The runner records each migration's checksum and
  refuses to run against a database whose applied migrations no longer match the
  shipped files, so an edit is a loud failure rather than a silent disagreement.
- **No `DROP` without operator review.** SQL containing `DROP`, `TRUNCATE` or
  `DELETE FROM` is refused unless the call passes `allow_destructive=True`. It is
  a per-call argument on purpose: the decision belongs where a reviewer reads it.
- **Idempotent and atomic.** Applying an up-to-date database does nothing; each
  migration commits together with its `schema_migrations` row, so a failure leaves
  neither; concurrent runs are serialized by an advisory lock, which every entry
  point takes — `status` and `apply` racing on a fresh database is the ordinary
  case, since whichever arrives first is the one that creates `schema_migrations`.
- **A database ahead of the library is refused** rather than migrated on top of.

#### Taking over a database the gateway already migrated

That is the ordinary case, not an exotic one: the first database `tokenweir` is
pointed at is the one the AI Gateway has been migrating all along. Its
`schema_migrations` predates this runner and records a version without a checksum,
so the runner **adopts** it — it brings the table up to shape and treats the rows
already there as applied, rather than re-running migrations against objects that
exist. Those adopted rows keep a NULL checksum and are reported as unverifiable
(a warning names the versions), because inventing a checksum for a migration
somebody else ran would be a claim this library cannot make.

Nothing is re-applied and nothing is dropped. What the runner does from then on is
ordinary: new migrations get real checksums, and drift detection applies to those.

#### Granting a reader role later

`reader_role` only takes effect on the run that **applies** migrations 004 and
005 — they are the migrations that issue the grants, and a migration already
recorded as applied is never re-run. Passing `reader_role` to an
already-migrated database therefore grants nothing and says nothing, which is
worth knowing before you go looking for the permission you thought you set.

To add a reader after the fact, grant it directly — it is one statement, and it is
not a schema change:

```sql
GRANT SELECT ON gateway_usage, model_pricing_rates, gateway_usage_daily
    TO metrics_reader;
```

#### When a released migration really was edited

The checksum covers the whole file, comments included, so editing even a comment
in a released migration makes every deployed database refuse to migrate. That is
the intended strictness — but if you need to *look* at such a database, `status()`
does not verify checksums by default, and `pending()`/`apply()` take
`verify_checksums=False`. The fix remains a new migration; the escape hatch is for
diagnosis, not for making the disagreement go away.

### Writing records

`PostgresSource` is the writer — a `Source`, so it is the mirror of a `Sink`:

```python
from tokenweir.postgres import PostgresSource

source = PostgresSource(connection)    # refuses an autocommit connection
written = source.write(batch)          # one transaction; returns the row count
```

**`Source.write` may raise, and that is the point.** `Sink.emit` must never raise
because it sits on the metered request's critical path; `write` runs in a consumer,
off that path, where a swallowed failure is silent data loss and a caller that can
retry needs to know it must. The two halves of the pipeline have opposite failure
rules on purpose, and each rule is wrong on the other side.

A batch is validated **whole, before any statement is sent**, so one unwritable
record cannot half-write the batch around it — and because the batch is one
transaction, a consumer can ack after `write` returns knowing that either all of
it is durable or none of it is. Batching *policy* (how many, how long to wait, what
to do with a redelivery) belongs to the consumer that owns the broker, not here.

A record with no `ts` is stamped by the **database**, not the client, so records
written from different hosts share one clock.

### Reading cost

`gateway_usage` stores raw counts and **no cost column, ever**. Dollars are derived
at report time by `gateway_usage_daily`, which joins the rate in force on the usage
day, and which answers "I cannot tell you" rather than guessing:

- `est_cost_usd` is **NULL for the whole group** unless *every* call in it priced
  (`BOOL_AND`). A partial sum is always too low and, read on its own, looks exactly
  like a complete one.
- **Subscription usage is never priced.** Under a flat-rate Max subscription there
  is no per-call dollar, so multiplying those tokens by an API rate card would
  manufacture a figure nobody is billed. `pricing_mode` is one of the grouping
  columns, so flat-rate usage does not blank out API-metered usage that genuinely
  has a cost.
- **A call that used cache tokens the rate card does not price is unpriced**, even
  though its input and output rates are present. The cache rate columns are
  nullable because not every model has cache pricing; treating a missing one as
  zero would quietly report cached usage as free, which is the one direction an
  estimate must never err in.
- **A row with no `pricing_mode` is priced.** Only `subscription` is excluded, and
  the comparison is `IS DISTINCT FROM`, so a NULL — a row written before the column
  meant anything — is treated as ordinary API usage rather than silently dropped
  from every cost figure.
- Rate columns are named `*_usd_per_mtok`. The unit is in the name because a rate
  card loaded against the wrong one is a thousand-fold error with nothing in the
  data to reveal it.

### Running the store tests

The correctness tests for all of the above run against a **real Postgres** — this
project does not mock a database, because a mocked one only proves the code calls
the mock. There are two ways to give them one, and installing the dev extra is
enough for the second:

```bash
pip install -e '.[dev]'          # brings pgserver: the suite starts its own server
pytest

createdb tokenweir_scratch       # or point it at a database you chose
TOKENWEIR_TEST_DSN=postgresql:///tokenweir_scratch pytest
```

`pgserver` ships the PostgreSQL binaries in its wheel, so the embedded path needs
no root, no `apt` and no Docker. `TOKENWEIR_TEST_DSN` wins when set — a developer
who names a particular server means it. With neither, the suite **skips** with a
message naming the variable, so installing only the core never turns red.

Each test creates and drops its own schema, so the DSN may point at any scratch
database without the suite colliding with an existing `gateway_usage`. What runs
without a database — the migration set's shape, the two preserved fixes, the
writer's mapping, and a syntax check against Postgres's own parser — runs always.
Run the real-Postgres suite before believing a change to the runner or the SQL:
the concurrency and adoption behaviours are only observable against a server.

## Develop

```bash
pip install -e '.[dev]'
pytest
ruff check .
```

Apache-2.0. Metering is intentionally the open-core boundary (ADR-0002).
