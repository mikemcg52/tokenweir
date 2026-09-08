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
  off that critical path too, and to
  [`DirectSink`](#the-broker-less-path), the broker-less write-through.
- **`tokenweir.source`** — the write side (`Source`). Persists records to a store.

and one client that makes the emit side's promise true rather than merely stated:

- **`tokenweir.emitter`** — [`BufferedEmitter`](#the-emitter-client). Buffers,
  returns immediately, delivers from a background worker, swallows failures.

Transport lives in optional adapters so the core stays dependency-light:

```bash
pip install tokenweir           # core only
pip install 'tokenweir[amqp]'   # + AMQP adapter (tokenweir.amqp)
pip install 'tokenweir[postgres]'  # + the Postgres store (tokenweir.postgres)
```

An adapter is reached by its own import path and is never exported from the
`tokenweir` namespace, so `import tokenweir` can never drag in a wire or a driver.

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

> **Adoption is in progress.** `tokenweir` ships the seam and, since TOKWEIR-6,
> the [emitter client](#the-emitter-client) that consumes it. The AI Gateway
> (TOKWEIR-10) is still being moved onto both. A consumer constructing records
> inline should call `emit_usage` itself — and should watch the return value, since
> a producer that gets a field name wrong drops *every* record, not some of them.

## The emitter client

`Sink.emit` promises to buffer, return immediately and never raise. `BufferedEmitter`
is what **discharges** that promise, so an adapter does not have to.

That distinction is the whole reason this exists. Without it every adapter
re-implements a buffer, a worker, a bound and a drop policy — the guarantee written
four times and wrong once — and the natural way to write an AMQP sink is a blocking
`basic_publish`, which satisfies "never raises" and violates "off the critical path"
without ever looking wrong.

```python
from tokenweir import BufferedEmitter, emit_usage
from tokenweir.amqp import AMQPSink

with BufferedEmitter(AMQPSink.from_url("amqp://guest:guest@rabbit/")) as emitter:
    emit_usage(emitter, fields)          # appends and returns; never raises
```

The client is itself a `Sink`, so it composes with the guarded seam with nothing in
between: `emit_usage(emitter, fields)` is the one call a metered request path makes,
and both the construction guard and the delivery guard are behind it.

### Two policies to read before adopting

**The buffer is bounded, and a full buffer drops the newest record.** An unbounded
buffer is not a safety mechanism; it is a memory leak that takes down the metered
service exactly when the broker is down — the precise failure the design exists to
prevent. Blocking the caller instead would be worse, since that *is* the critical
path. Records already accepted stay accepted: evicting the oldest to make room would
turn a bounded, countable loss into an unbounded reshuffle of which records survive.

**Delivery is never retried.** A retry loop in front of a down broker fills the
bounded buffer and turns record loss into *more* record loss plus latency, and a
poison record retried forever blocks everything behind it. A failed batch is dropped
and counted. What actually recovers from an outage is reconnection, which belongs to
the adapter and happens on a later delivery attempt.

Neither drop is silent. Both are counted, and both log a rate-limited `WARNING` so a
systematically broken sink cannot produce one log line per metered request. The
window is **per reason**: a chatty broker failure never suppresses a first sighting
of an unrelated one, and a suppressed count is never reported against a cause that
did not produce it.

```python
stats = emitter.stats()
stats.accepted, stats.delivered, stats.failed
stats.dropped_buffer_full, stats.dropped_not_a_record, stats.dropped_closed
stats.dropped_at_close   # abandoned when close() hit its timeout
stats.dropped        # every record the client took on and did not deliver
stats.buffered, stats.worker_alive
```

`stats()` is the signal to alert on: it is a consistent snapshot, richer than a
return value, and it reaches you regardless of logging configuration. Two things it
is worth knowing precisely:

- **`delivered` counts hand-off, not persistence.** It means the sink accepted the
  record without raising. A conforming sink cannot raise, so a sink that swallows
  its own failure — `DirectSink` does exactly this when the store is down — is
  counted as delivered here. On the broker-less path, read `DirectSink.dropped`
  alongside it; on the AMQP path, `AMQPSink.dropped`.
- **`worker_alive` means "the delivery worker is running".** It is `False` after
  any `close()` — that is the normal end state, not a fault. What it diagnoses is a
  client you have *not* closed reporting `False`: that happens only if a sink raised
  a `BaseException`, which is deliberately not caught, and it means the client has
  silently stopped metering. Every other failure is a counted drop and the client
  keeps working.
- **`dropped` includes `dropped_at_close`**, so records abandoned by a `close()`
  that timed out against a wedged sink are inside the one number worth alerting on.

### Knobs

| Argument | Default | What it decides |
|---|---|---|
| `max_buffer` | `10_000` | The bound on records *waiting*. Reaching it drops rather than blocking or growing. A batch already in flight is outside it, so peak retention is `max_buffer + batch_size`. |
| `batch_size` | `100` | The most records handed to the sink at once. |
| `linger` | `0.2` | How long the worker waits for a partial batch to fill. `flush()` and `close()` both cut it short, so it never delays a shutdown. |
| `close_timeout` | `5.0` | Bound on `close()`'s final flush. A wedged sink must not hang a process exit. |
| `warn_interval` | `60.0` | Seconds between warning lines for a repeating failure, **per reason**. |

### Lifecycle

`flush(timeout)` blocks until the buffer is drained and reports whether it got
there — it is the synchronization point to use instead of a sleep. `close()` stops
the worker, flushes within `close_timeout`, closes the sink, is safe to call more
than once, and never raises even if the sink's own `close` does.

The worker is a **daemon** thread, so metering can never be the reason a process
will not exit. The cost, stated rather than hidden: records buffered at a hard exit
are lost. `close()` — or the context manager — is the supported way to not lose
them, and an `atexit` hook makes a bounded best-effort attempt for callers who
forget. That bound is **per client**: *n* forgotten clients whose sinks are all
wedged against a dead broker delay interpreter exit by up to *n* × `close_timeout`.
Exit is still bounded rather than hung, but if you hold many clients, close them.
This client is not durable; surviving a consumer outage is what a broker is for.

## Choosing a transport

ADR-0001 Pillar 2 keeps the wire out of the core, which is what lets the two
deployments differ without the library knowing:

| Deployment | Sink | Why |
|---|---|---|
| homelab | `tokenweir.amqp.AMQPSink` | RabbitMQ is already there: gateway → AMQP → writer, and the broker absorbs a writer outage |
| cloud-edge | `tokenweir.sink.DirectSink` | no broker to run; write in-process to the store |

### The AMQP path

```python
from tokenweir.amqp import AMQPSink

sink = AMQPSink.from_url("amqp://guest:guest@rabbit/", routing_key="tokenweir.usage")
sink = AMQPSink(channel, exchange="metering", routing_key="usage.gateway")
```

Each record is published as its JSON wire form, persistent (`delivery_mode=2`) with
a JSON content type — metering that evaporated on a broker restart would make the
broker path pointless.

`pika` is never imported at module import, so a bare `pip install tokenweir` carries
no AMQP library and the record→message mapping (`message_for`) is usable and testable
with none installed. It *is* imported when a sink is **constructed** — wiring time,
where a missing driver raises an `ImportError` naming `tokenweir[amqp]` while
somebody is still watching.

That last part is deliberate rather than incidental. Deferring the import further,
to the first publish, looks tidier and is a trap: the publish path may not raise, so
a missing driver discovered there is not an error at all — it is a silent 100% drop
of every record, for the life of the process, in the one code path whose whole job
is never to complain.

To build a sink over a channel of your own with no `pika` installed at all, pass
`properties=` — the driver is only needed to construct the message properties:

```python
sink = AMQPSink(channel, properties=my_basic_properties)
```

Nothing this library writes to a log carries the URL's credentials: a reconnect
failure is redacted before it is logged, the same rule `tokenweir.migrations`
applies to a Postgres DSN. `from_url` still raises the driver's own exception
unredacted, because you supplied the URL and may be catching pika's exception type.

**Being able to reconnect decides reconnection** — not ownership, which is a
different question. A sink built by `from_url` knows how to re-dial, so on a publish
failure it drops the connection and re-establishes on the *next* attempt, never by
retrying inside the current one. A channel you hand in is never re-made, whoever
owns it: this object does not know how it was built, and guessing would replace your
TLS context, credentials or pooling with its own. Such a channel is left exactly as
it was, so publishing resumes the moment you repair it, with no cooperation needed
here. `close()` still closes only a connection the sink opened itself.

Reconnect *attempts* are spaced by `reconnect_interval` (default 5s). Without it, a
`BlockingConnection` dial per buffered record would block the emitter's worker for a
full connect timeout each time — the retry loop this design refuses, reassembled out
of single attempts.

When an attempt is free and when it waits comes down to one question: **can
re-dialling plausibly help?**

| What failed | Next attempt |
|---|---|
| A connection that had been publishing | **Immediate.** Something outside the process broke, and re-dialling is exactly what fixes it. Waiting out an interval here would trade a real fault for an invented one. |
| The dial itself | **Spaced.** A broker that refused the connection will refuse the next one too. |
| A publish, on a connection that never published | **One free re-dial, then spaced.** A reset before the first publish looks identical to a bad exchange, so one retry settles it: if a fresh connection also cannot publish, the fault is not the connection. |

**If you are debugging a silent metering outage, start with the cap.** The obvious
guess — that a missing exchange lands in the third row — is wrong, and wrong in a way
worth knowing. `pika` does not wait for the broker on publish unless confirms are
enabled, so the *first* publish to an exchange that does not exist **returns
normally** and the 404 surfaces on the next one. That first apparent success makes
the connection look productive, so it takes **row 1** and is re-dialled immediately;
what stops it is the cap below, not the third row. The third row is for a channel
that fails on its very first publish — a socket already gone, not a topology
mistake.

So the line to look for is the cap's: it names the exchange and routing key as the
thing to check, and the underlying broker error is on the publish-failure warning
beside it (logged with a traceback). The two *waiting* cases log distinct `WARNING`s
naming which one you are in; an immediate recovery logs the ordinary publish failure
and nothing more, because there is nothing to wait for.

Underneath all of it there is a hard cap of `tokenweir.amqp.MAX_DIALS_PER_INTERVAL`
dials per interval, which applies no matter what the rules above conclude — unless
you set `reconnect_interval=0`, which disables the spacing and the cap together and
hands back unbounded per-record dialling. That is a supported setting, not a default
worth reaching for. It is
there because those rules infer *why* a publish failed from whether the connection
had published before, and that inference is not always available: `pika` does not
wait for the broker on publish unless confirms are enabled, so the first publish to
a missing exchange returns normally and the rejection surfaces on the next one. The
cap is what makes the bound a guarantee rather than a good guess. It is a module
constant rather than a constructor argument on purpose — a backstop a caller can
raise is not a backstop.

Backing off has a cost worth knowing: a broker that recovers *during* a window has
those records dropped, where dialling per record would have stumbled into a working
connection. That is the trade — the dialling is the thing that was hurting the
metered service — and the window bounds it.

The allowance renews per *working* connection, so a flaky link that keeps publishing
between drops recovers instantly rather than degrading into a rate-limited one —
up to `MAX_DIALS_PER_INTERVAL` recoveries per interval. A link dropping more often
than that inside one window has the excess refused and its records dropped until the
window turns over, because the cap above overrides every immediacy rule here. That
is the intended order of precedence: a link failing that often is not one the sink
should keep re-dialling.

**Declaring the topology is not the adapter's job.** Exchanges, queues and bindings
outlive any process; a library that declared them would silently own them, and fail
confusingly the day its arguments disagreed with what is deployed.

**`AMQPSink` is not thread-safe, and cannot be made so here.** An AMQP channel is
not safe to share between threads — that is pika's constraint, not one this library
could lift, and a lock around the counters would hide the hazard rather than remove
it. Give each thread its own sink, or put a single `BufferedEmitter` in front: its
worker is one thread, and it is the only thread that ever touches the sink,
including for `close()`. That is the shape this adapter is built for.

### The broker-less path

`DirectSink` adapts a `Source` to the `Sink` interface:

```python
from tokenweir import BufferedEmitter, DirectSink
from tokenweir.postgres import PostgresSource

with BufferedEmitter(DirectSink(PostgresSource(connection))) as emitter:
    emit_usage(emitter, fields)
```

It is **batch-capable on purpose**: a batch stays one `Source.write` call, which is
what preserves `PostgresSource`'s one-transaction-per-batch guarantee instead of
degrading to a transaction per row. That capability is the optional `BatchSink`
protocol — a sink may offer `emit_batch`, and `BufferedEmitter` prefers it when
present and falls back to per-record `emit` when it does not. `AMQPSink`
deliberately does not implement it: AMQP has no batch publish, so an `emit_batch`
there would be a loop wearing a costume.

`DirectSink` sits exactly where the two halves' failure rules meet, so it is the
place where a store failure stops being an exception and becomes a counted, logged
drop (`sink.written`, `sink.dropped`). Be plain about what that means: records that
reach this sink and cannot be stored are **gone**, not queued for retry. A
deployment that needs durability across a store outage wants the broker path.

Ownership follows `PostgresSource`'s rule — a source passed in stays yours and
`close()` leaves it open; pass `owns_source=True` to hand it over.

## Subscription capture — the Claude Code Stop hook

Everything above assumes a producer that can *see* the call it is metering. Under a
**Claude Max subscription** nothing can: the auth is OAuth, there is no
base-URL-swappable API path, and no proxy is ever in the way. ADR-0001 Pillar 4's
answer is to capture from Claude Code's own record instead, with a **deterministic
hook** — not an LLM skill, which would burn tokens into the very window being
measured.

`tokenweir.claude_code` is that hook. On each `Stop` event it reads the session
transcript, works out how many tokens the turn that just ended consumed, and emits
one record with `pricing_mode=subscription`. It is a producer and nothing more: the
guarded seam, the buffered client, the transports and the store are the same ones
the rest of this README describes.

It imports **no third-party package**, so a bare `pip install tokenweir` can run it.

### Install it

```jsonc
// ~/.claude/settings.json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "tokenweir-claude-code-hook",
            "timeout": 30
          }
        ]
      }
    ]
  }
}
```

`python -m tokenweir.claude_code` is the same entry point if you would rather not
rely on the console script being on `PATH`.

The hook **always exits 0** and never writes to stdout. That is not politeness, it is
the contract: ADR-0001 requires non-blocking capture and explicitly forbids `exit 2`
(Claude Code's *blocking* status), because no failure of a metering adapter is worth
stopping a session over. `"async": true` is safe to add if your Claude Code supports it.

### Where records go

| Variable | Effect |
|---|---|
| `TOKENWEIR_AMQP_URL` | Publish over AMQP (the homelab path). Needs `tokenweir[amqp]`. |
| `TOKENWEIR_DSN` | Write straight to Postgres (the broker-less path). Needs `tokenweir[postgres]`. |
| *neither* | `NullSink` — the hook runs and discards. An unconfigured hook is a no-op, not an error. |

With **both** set the broker wins. A deployment that configured one has said where records should
survive an outage, and writing past it to the store would discard that.

A transport that is *configured* but cannot be constructed — a dead broker, a missing driver — does
not fail the hook; it falls back to discarding, and the turn's tokens are carried into the next one
rather than counted as metered. That is the difference between a broken transport and no transport:
an unconfigured hook is discarding by choice and moves on, a broken one is losing records the
deployment expected to keep and holds them.

Neither driver is imported unless the matching variable is set, so an unconfigured
hook touches no transport library at all. A sink that cannot be *constructed* — a bad
URL, a missing driver, a broker that is not there — degrades to the no-op sink with a
logged note rather than failing the hook.

### Attribution, and where it comes from

Hooks inherit Claude Code's process environment, so MADO's orchestrator exports the
context per iteration and the hook reads it onto the record. **Nothing is read from
the transcript's content** — attribution comes from the orchestrator, not from the
model.

| Variable | Record field | Meaning |
|---|---|---|
| `MADO_ISSUE_KEY` | `workload` | The issue being worked. |
| `MADO_PHASE` | `queue` | The phase of the run. See the note below. |
| `MADO_STREAM_ID` | `parent_request_id` | The stream, so its turns roll up. |
| `MADO_PRICING_MODE` | `pricing_mode` | Overrides the default; anything unrecognized falls back to `subscription`. |

Unset and **blank** are treated alike: the field is left `None`, never `''`. An
orchestrator exporting `MADO_PHASE=""` is saying "no phase", and a blank string in the
column would say the field was populated.

> **`queue` carrying the phase is a documented compromise, not a natural fit.** The v1
> contract has no phase field, and adding one is a `SCHEMA_VERSION` bump plus a
> migration plus every consumer — Pillar 5 work. `queue` is nullable, unconstrained,
> and the nearest available sense of "which lane did this go through". It is written
> down here, in the module, and in the story's spec so a future v2 can move it
> deliberately rather than find it.

### The phase taxonomy, and what the orchestrator must export (TOKWEIR-8)

`MADO_PHASE` lands in a free-text column, so without a defined vocabulary `review`,
`Review`, `1st review`, `review 1` and `review-1` are five different strings for one
phase of one run — and a report grouping by phase shows five lanes where the work had
one. `tokenweir.orchestrator` defines the vocabulary both ends use.

**The kinds** are the orchestrator's own, taken from `mado-phase --phase` (*"one of:
implementation, review, fix"*) rather than invented here. The loop — implement → review
→ fix → review → … — is what makes a kind recur, so a label is a kind and, optionally,
which occurrence of it:

```text
implementation        a kind on its own
review-1  fix-1       a kind and a positive occurrence, hyphen-joined
review-12             any occurrence — the fix-round cap is configurable
```

The set of kinds is closed; the occurrence is not. A bare kind means *the orchestrator
did not say which occurrence*, which is not the same claim as "the first" — so nothing
here ever invents a `-1`.

**Reading is forgiving.** `normalize_phase` maps what a human or an older orchestrator
would write onto the canonical label: case, `_` and `#` separators, surrounding
whitespace, a leading English ordinal (`1st review`, `22nd fix`), a trailing number
(`review 2`), and the aliases `bug fix`/`bugfix` → `fix` and `implement` →
`implementation`. Ordinal suffixes are validated rather than stripped, so `11th` is 11
and `11st` is not an ordinal at all. A phase the taxonomy does not recognize is **kept**
and logged — the lifecycle may grow a phase before this library hears about it, and a
record carrying `deploy` is worth more than a record carrying nothing. Separators are
folded in the preserved value too (`deploy_step` is kept as `deploy step`), so an
unknown phase does not split into as many lanes as it has spellings; the log names the
value as you exported it, so you can still find it in your own configuration. Unset and
blank still mean "no phase" and still leave the field `None`.

**Writing is strict.** The producer builds the block from the contract rather than
spelling the variable names by hand, and hears about a value it cannot use:

```python
from tokenweir import attribution_env, phase_label

env.update(attribution_env(
    issue_key="TOKWEIR-8",
    phase=phase_label("fix", 2),      # 'fix-2'
    stream_id=stream_id,
))
```

`attribution_env` returns **all four variables on every call**, using `''` for a value
the caller does not have. That is the load-bearing part: a block that omitted what it
had nothing to say about would leave the previous iteration's phase standing during the
next one, and the resulting record would be well-formed, plausible and wrong. A blank
issue key, a blank stream id, a blank phase, a non-positive occurrence on a known kind
(`review-0`, `review 0`, `0th fix`, `review -1`) or an unrecognized pricing mode raises
instead of being exported. Surrounding whitespace on the issue key and stream id is
stripped rather than exported, since `' TOKWEIR-8 '` and `'TOKWEIR-8'` are one issue to
a reader and two rows to anything grouping by the column. A phase the taxonomy does not
*recognize* is not in that list — `deploy` passes through, because the lifecycle is
MADO's to extend — the hook tolerates such values because it may not
fail a session, while a producer is a program with a bug worth surfacing.

So the orchestrator's obligation is: **export the whole block, on every iteration,
before the turn**, into the environment Claude Code inherits.

**Version skew.** The orchestrator takes an ordinary runtime dependency on this
package, and the two ends are deployed separately — so they can disagree about the
taxonomy. The disagreement is deliberately harmless in the direction it will happen:
a producer whose `tokenweir` knows a kind the reader's does not exports a canonical
label, and the older reader keeps it verbatim and logs it as unrecognized. The record is
never lost and never rewritten; a report just shows one lane it cannot name yet, until
the pod's `tokenweir` catches up. Adding a kind is therefore additive at both ends, and
neither end needs to be upgraded first.

> **The orchestrator-side change is not in this repository.** MADO's orchestrator is
> `services/orchestrator/` in the `mado` repo. TOKWEIR-8 delivers the contract here —
> the taxonomy, the builder, and the hook's conformance to it — and the export itself is
> a separate change over there, tracked as **MADO-419** and written against this
> contract. Until it lands, records from a Max-authenticated stream carry no issue key
> and no phase, however green this repo's suite is.

### Everything else it reads

| Variable | Effect |
|---|---|
| `TOKENWEIR_APP_ID` | Overrides `app_id` (default `claude-code`). |
| `TOKENWEIR_ENDPOINT` | Overrides `endpoint` (default `claude-code/stop-hook`). |
| `TOKENWEIR_HOOK_STATE_DIR` | Where the per-transcript baseline is kept. |
| `XDG_STATE_HOME` | Base for the default state directory when the above is unset (falls back to `~/.local/state`). |

`endpoint` deliberately is **not** `/v1/messages`: a turn is an aggregate of several
API calls, and labelling the aggregate with a single-call endpoint would let a report
blend the two under one key.

### How the delta is computed, and why it is a baseline

A transcript is append-only and holds the **whole session**. The turn's number is the
difference between the transcript's current de-duplicated totals and the cumulative
total the hook has already emitted, which it keeps in a small JSON file per transcript.

Two decisions in that sentence are load-bearing:

- **De-duplicated on `message.id`.** One API response can appear as several transcript
  lines, each repeating the same `usage` object. Summing lines over-counts a turn, and
  every line is individually well-formed, so nothing about the result looks wrong.
- **A cumulative baseline, advanced only once the record is actually *stored*.** A file
  cursor advances whether or not the emit succeeded, which silently deletes a turn's
  tokens forever. A cumulative baseline is self-correcting: a turn that could not be
  stored is merged into the next one. Late and coarse beats gone.

  The test applied is **"was it accepted, and did the transport not report losing
  it?"** — the emitter's acceptance plus the adapter's own `dropped` counter. The
  emitter's number alone is not enough: a conforming `Sink` may not raise, so both
  adapters catch their own transport failure, count a drop, and return normally, and
  reading `delivered` would have meant a dead broker silently deleting every turn.

  It asks about **drops rather than successes**, and that is deliberate. A counted
  drop is a fact the adapter is certain of. A success counter is not:
  `AMQPSink.published` counts frames handed to a socket, and this adapter does not
  enable publisher confirms — so **a live broker with a missing exchange or a wrong
  routing key accepts the frame, reports no drop, and the tokens are lost.** Nothing
  inside the hook can see that. If you need that case covered, the answer is
  publisher confirms in the adapter, not a guess here.

  Asking about drops also means a sink that keeps no such counter is simply believed
  rather than assumed to have failed for ever.

Losing the state file **once** over-counts exactly one turn — the session's tokens land
in one record — and everything after it is correct again. A state directory that is
**persistently** unwritable is a different matter: the hook still emits (it must; a
metering cache is not a reason to lose a turn), but it re-reports the whole session on
every turn for as long as the condition lasts.

**Those re-reports are not collapsible, and it is worth being blunt about that.** On a
static transcript they are identical records sharing a `request_id`. On a *live* session —
the case that actually happens — each re-report covers a longer span and ends on a
different response, so the ids differ and the counts climb: three turns of 100 tokens
report 100, 300 and 600. Nothing marks them as re-reports, and no choice of identifier
would.

So a persistently unwritable state directory is real, silent inflation. Each failure is
logged, and that is the whole of the mitigation — the alternative is losing the turn, which
is worse. It is a broken deployment and needs fixing, not tolerating.

(The stable `request_id` still earns its keep for the case it *does* cover: an
at-least-once transport redelivering one record. The store puts no unique constraint on the
column for exactly that reason, so a report can collapse those.)

One other case falls the same way. The baseline advances only if the record survives the
emitter's close timeout (3 seconds by default); a sink still publishing when that expires, which
then succeeds, gets the record while the baseline stays put, so the next turn re-reports it. That
re-report is **not** a collapsible duplicate either — it covers a longer span, ends on a different
response, and carries a different id — so the store holds both and their sum overstates the
session. The window is narrow, and the run still errs this way on purpose: assuming success on a
timeout turns it into a silent loss, and an overstatement can at least be checked against the
transcript it came from.

A transcript that *regresses* below its baseline is taken as a replaced file: the baseline
is re-anchored and nothing is emitted for that transition.

A turn that added no new tokens emits nothing at all. A zero-token record would inflate
the request count while adding no tokens.

### Two guarantees that it cannot disturb a session, and one that is not its own

They are separate because they fail separately:

| Guarantee | Covers |
|---|---|
| `main` catches `Exception` and returns `0` | A bug in the hook itself. `BaseException` still propagates — a metering guard that swallowed `KeyboardInterrupt` would be the worse bug. |
| A bounded emitter close | A sink that accepted the record and cannot deliver it. |

The third case — nothing raises and nothing returns, a connect into a black hole — is bounded by
**Claude Code's own `timeout`**, which is why the fragment above sets it. The hook deliberately does
not run a competing timer: a second bound inside a process the host already bounds is a failure
surface without a failure it uniquely fixes.

### What it does not do

Raw counts per turn, and nothing else — matching Pillar 4's "scope now". No
percentage-of-limit, no capacity modelling, no cost: Max tiers and weekly caps are
volatile, and cost is derived at report time from the rate card, as everywhere else in
this project.

It also does not verify that a Max transcript's numbers agree with an API-key session's
on the same model and context. ADR-0001 records that parity as `Unverified` and keeps it
as its own item; this hook faithfully reports what the transcript says.

## The store — schema, migrations and the writer

`tokenweir` **owns** the usage schema. That ownership moved here from the AI
Gateway (ADR-0001 Pillar 5): the gateway pins a `tokenweir` version and no longer
carries DDL, and anything else that reads or writes `gateway_usage` gets its
schema from the same place. The table keeps its name — renaming it would force a
data migration on a live database, and the consumers' contract is that their
existing rows and rollups do not change.

```bash
pip install 'tokenweir[postgres]'
python -m tokenweir.migrations apply --dsn postgresql:///gateway
python -m tokenweir.migrations status --verify-checksums
```

On a database somebody else migrated — the AI Gateway's, which is the first one
this was ever pointed at — `apply` is not enough on its own and succeeds anyway.
See [Reconciling a database that does not match](#reconciling-a-database-that-does-not-match).

Connection options are accepted on **either side** of the subcommand, and each
falls back to an environment variable, so a deploy script can set them once:

| Option | Environment variable | |
|---|---|---|
| `--dsn` | `TOKENWEIR_DSN` | the connection string |
| `--reader-role` | `TOKENWEIR_READER_ROLE` | role the grant migrations give `SELECT` to; unset, they no-op with a notice |
| `--verbose` | — | log each migration as it is applied |

An explicit flag beats the environment. `status` also takes `--verify-checksums`,
which is the only way to be *told* about a drifted database rather than shown one;
`apply` takes `--allow-destructive` and `--no-advisory-lock`; and `reconcile` takes
`--apply` and `--baseline-effective-from`, both described
[below](#reconciling-a-database-that-does-not-match).

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

**Adoption is not the end of the job, and this is the part to read twice.** Those
adopted rows say six migrations were applied. They are the *gateway's* rows, about
the gateway's migrations, and nothing has compared them to what the database
actually contains — the schema here was redesigned during the extraction, not
copied, so the two are known to differ. Every migration in this set is
`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE VIEW`, which means that against
objects that already exist they are **no-ops by construction**. Point `apply` at
such a database and it succeeds, changes nothing, reports `pending: (none)` — and
leaves you with a `gateway_usage` that has no `schema_version` column, which the
writer names in every `INSERT`. The first batch fails and nothing before it said a
word.

That is what `reconcile` is for.

#### Reconciling a database that does not match

```bash
python -m tokenweir.migrations reconcile --dsn "$DSN"            # plans; changes nothing
python -m tokenweir.migrations reconcile --dsn "$DSN" --apply \
    --baseline-effective-from 2024-01-01
```

`reconcile` ignores `schema_migrations` entirely and compares the database's
**actual catalog contents** against the schema tokenweir's migrations produce —
which it obtains by building that schema in a scratch schema on the connection and
rolling it back, so there is no second description of the schema to go stale. Every
difference is classified as one of two things and there is no third:

| | |
|---|---|
| `AUTOMATIC` | tokenweir can resolve it with no data loss and no decision |
| `MANUAL` | a human must decide; **nothing at all is applied while one is outstanding** |

Run it against a **restored dump of the database first**, read the plan, and only
then point it at production. Planning is read-only — that is what makes the safe
order the easy one — and `--apply` is the operator review, the same way
`--allow-destructive` is on `apply`.

Three things it will not do:

- **It never drops a column, a table or a row.** A column the gateway has and
  tokenweir does not is the gateway's data: it is reported, kept, and left alone,
  and tokenweir's writer will not populate it. The only `DROP` `reconcile` emits
  that removes a **relation** is of the rollup *view* — and even that becomes a
  decision the moment anything depends on it. (It emits others that remove nothing
  stored: `DROP DEFAULT` after a back-fill, and `DROP CONSTRAINT` on the rate
  card's old primary key.)
- **It will not date your rate card for you.** `model_pricing_rates` moves from
  current-valued `(model, pricing_mode)` to effective-dated `(model,
  effective_from)`, and back-filling the existing rows needs a baseline date that
  decides which historical usage those rates are taken to have covered. There is no
  default: `--baseline-effective-from` is required, and `-infinity` is accepted and
  means "these were always the rates" — write it as
  `--baseline-effective-from=-infinity`, with the equals sign, or argparse reads the
  leading dash as an option. Positive `infinity` is **refused**: it is in force on no
  day that has happened, so every existing row would quietly stop pricing. If two
  rows share a model — the old key
  allowed one per pricing mode, the new schema has no `pricing_mode` — it refuses
  and names them, because one of those rows has nowhere to go and choosing is a
  decision about money.
- **It will not half-finish.** The whole plan runs in one transaction, and a single
  `MANUAL` discrepancy blocks the `AUTOMATIC` ones too. A partly reconciled database
  reports itself done and is not.

Re-running it is a no-op, so it is safe to leave in a deploy script — though the
plan-first order above is the one to use the first time.

**A reconciled database is not identical to a fresh one, in four named ways**, and
none of them is a defect:

- `gateway_usage.id` stays `BIGSERIAL` rather than becoming `GENERATED ALWAYS AS
  IDENTITY`. Converting it is a full table rewrite for a property that only matters
  if rows are ever rebuilt — where restoring the old ids needs `OVERRIDING SYSTEM
  VALUE` and a sequence reset anyway.
- Columns the gateway owns are still there, and tokenweir's writer will not
  populate them.
- Added columns land at the end of the table rather than in the migration's order.
  Column *position* is not something anything here reads, and reordering would mean
  rewriting the table.
- Indexes and constraints the gateway named itself keep those names. Both are
  matched by what they *do* — an index by its shape, a constraint by its definition
  — so one already doing tokenweir's job under another name satisfies the
  requirement and is left alone. Creating a duplicate beside it would cost write
  throughput to serve nothing.

`reconcile` also does not re-issue grants. Dropping and recreating the rollup view
discards its SELECT grants, and `--reader-role` does not reach it — the grant lives
in migration 005, which reconcile executes without a reader role set. The plan names
the roles that will need re-granting; the statement is in "Granting a reader role
later" above.

**One limit, stated because you are about to run this against a real database.**
tokenweir's own migrations were reconstructed during the extraction rather than
copied from the gateway, and the legacy shape the test suite reconciles against is
likewise a reconstruction — `tests/test_reconcile.py`'s `LEGACY_SQL`, which is in
one place precisely so it can be corrected once. **Nothing here has been run
against a dump of the live `ai_gateway_metrics`.** That comparison is still yours to
do, and it is why `reconcile` introspects rather than assumes: a shape it has not
seen produces a refusal it names, not a reconciliation that guesses.

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
to do with a redelivery) belongs to the consumer that owns the broker, not here —
on the [broker-less path](#the-broker-less-path) that consumer is `BufferedEmitter`,
whose `batch_size` and `linger` are exactly that policy.

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

### What a bare install does not cover

Use the `dev` extra above. A plain `pip install -e .` runs the suite too, and it will be **green** —
but it is green over less, and the difference is deliberate rather than accidental.

The core is dependency-light and transport-free by contract (ADR-0001 Pillar 2): `tokenweir` itself
must install with no broker library and no database driver, so the tests that need one **skip**
rather than fail. That is the right behaviour — a test that cannot be evaluated must not turn an
ordinary install red — but it means a bare run silently proves less than it appears to:

| Absent from a bare install | What goes unchecked |
| --- | --- |
| `pika` | **FR-022** — persistent messages with a JSON content type — is checked only against the test double. The double accepts any keyword at all, so it would not notice `publish_properties()` emitting a key that real `pika.BasicProperties` rejects. |
| `jsonschema` | The published `schema/usage-record.v1.json` is never run through a JSON Schema engine; payload validity is checked only by this package's own code. |
| `psycopg`, **and** a server — either `$TOKENWEIR_TEST_DSN` or `pgserver` | The whole real-Postgres suite: the writer, the source and the migrations against an actual server. Concurrency and adoption behaviour are only observable there. Note this one needs both: the driver on its own proves nothing, so the run reports it missing until a server is reachable too. |
| `pglast` | The migration SQL is never parsed by libpg_query, the server's own parser. |
| `build` | SC-001 is checked against the packaging declaration rather than by building a wheel and looking inside it. |

**This is accepted, not overlooked.** The alternative is making a broker library a dependency of a
metering contract that must not have one, which costs more than the coverage is worth. So the suite
says so out loud instead: every run ends by naming what this environment could not supply and the
claim each absence forfeits, so a green CI log
**cannot be read as covering FR-022** against the real driver. The note appears on failing and
interrupted runs too, and never changes a run's exit status — it reports on the environment, not on
the result.

`tests/conftest.py` holds that record and `tests/test_optional_drivers.py` keeps it honest: equal to
the set of drivers the suite actually gates on, in both directions, so it cannot drift as tests are
added or removed.

### If you run this project's CI

The authoritative install for the MADO registry entry is `pip install -e . pytest` — no extras — so
the runs it drives forfeit everything in the table above. To close the `pika` gap, add `pika` to
this project's `install_commands` in `/etc/mado/projects.yaml`; to close all of them, make the step
`pip install -e '.[dev]'`. Neither is editable from inside a stream pod, which is why the limitation
is recorded here rather than fixed there. Nothing needs undoing in this repository if you do: the
disclosure goes quiet on its own once the driver is present.

Apache-2.0. Metering is intentionally the open-core boundary (ADR-0002).
