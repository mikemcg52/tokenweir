# Implementation Plan: Emitter client + Sink/Source interfaces + AMQP adapter

**Branch**: `TOKWEIR-6-emitter-client-sink-source-amqp` | **Date**: 2026-08-19 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/TOKWEIR-6-emitter-client-sink-source-amqp/spec.md`

## Summary

Deliver the three pieces ADR-0001 Pillar 2 promises and `main` does not yet have: a **buffered
emitter client** that makes "fire-and-forget, off the critical path" a property of the library
rather than of every adapter; a **direct (broker-less) sink** over the existing `Source`; and the
**AMQP adapter** under the already-declared `tokenweir[amqp]` extra. The `Sink` and `Source`
protocols are used as they stand — the only interface work is an *optional* `BatchSink` extension
so a batch-capable sink (the direct one) keeps `PostgresSource`'s one-transaction-per-batch
guarantee instead of degrading to a transaction per record.

The shape is a bounded buffer plus one background worker thread. The calling thread does an
append and returns; the worker drains in batches and delivers. Failures are dropped and counted,
never retried and never raised. That is a deliberately small design: every additional mechanism
(retry, persistence, backpressure) trades away the property the story is actually buying.

## Technical Context

**Language/Version**: Python 3.11+ (`requires-python = ">=3.11"`)
**Primary Dependencies**: none in core — stdlib only (`threading`, `collections.deque`, `logging`,
`json`, `time`). `pika>=1.3` under the existing `amqp` extra, imported only inside the function
that opens a connection.
**Storage**: N/A here. The direct sink writes through an existing `Source`; `PostgresSource` and
the migrations are TOKWEIR-5's and are not touched.
**Testing**: pytest, per `/workspace/.mado/project.yaml` — `/workspace/repo/.venv/bin/pytest`,
`CI=true`, passing exit codes `[0, 5]`. New tests run with no broker and no database.
**Target Platform**: Linux (homelab k3s, EKS cloud-edge) and developer macOS.
**Project Type**: Library (single package, `src/` layout).
**Performance Goals**: the emitting call is an append under a lock — microseconds, and constant
with respect to buffer depth, sink latency and broker health. No per-emit allocation beyond the
append itself.
**Constraints**: bounded memory (buffer bound is the only growth term); no thread that outlives the
interpreter; no import-time third-party dependency in any core module (enforced by the existing
AST sweep in `tests/test_contract.py`).
**Scale/Scope**: one metered call per record; homelab volume is low thousands/day, and the buffer
bound defaults well above any realistic burst between flushes.

## Constitution Check

`.specify/memory/constitution.md` in this repository is still the **unfilled speckit template** —
every principle is a `[PRINCIPLE_N_NAME]` placeholder. There are therefore no ratified gates to
check against, and this plan does not invent any. Recorded here rather than passed over silently,
because "Constitution Check: PASS" against a template would be a false statement.

The project's real, written-down constraints live in `docs/adr-0001-token-weir.md` and are treated
as the gate instead:

| ADR-0001 | Gate | How this plan satisfies it |
|---|---|---|
| Pillar 1 — OSS-first | No homelab-private detail in the library | Adapter is generic AMQP; no hostnames, no credentials, no MADO specifics |
| Pillar 2 — Transport-agnostic, dependency-light core | No transport import in core; emission fire-and-forget | `pika` deferred into the connect path; buffered client makes the guarantee structural |
| Pillar 3 — Provider-neutral | No provider knowledge | This story touches no field semantics |
| Pillar 4 — Both pricing modes | Contract unchanged | Not touched |
| Pillar 5 — tokenweir owns the schema | No DDL here | Direct sink writes through `Source`; no SQL added |

## Project Structure

### Documentation (this feature)

```text
specs/TOKWEIR-6-emitter-client-sink-source-amqp/
├── spec.md              # Phase -1 output
├── plan.md              # This file
└── tasks.md             # Phase 2 output (/speckit.tasks)
```

No `research.md`, `data-model.md`, `contracts/` or `quickstart.md`: there is nothing to research
(the transport and the extra were both decided in ADR-0001 and `pyproject.toml`), no new persisted
entity (`UsageRecord` is unchanged), and the public surface is documented in `README.md` where an
adopter will actually look — matching how TOKWEIR-4, TOKWEIR-5 and TOKWEIR-15 were planned in this
repository.

### Source Code (repository root)

```text
src/tokenweir/
├── __init__.py          # + BufferedEmitter, DirectSink, BatchSink, EmitterStats exports
├── contract.py          # untouched
├── sink.py              # + BatchSink protocol, + DirectSink
├── emitter.py           # NEW — BufferedEmitter, EmitterStats
├── _ratelimit.py        # NEW — RateLimitedWarner, private, shared by the three emit-side modules
├── amqp.py              # NEW — AMQPSink, message_for(); pika deferred
├── source.py            # untouched
├── postgres.py          # untouched
└── migrations/          # untouched

tests/
├── test_emitter.py      # NEW — buffering, bounds, threads, lifecycle, counters
├── test_direct_sink.py  # NEW — broker-less path, batch atomicity, failure containment
└── test_amqp.py         # NEW — mapping without pika, publish against a channel double
```

**Structure Decision**: The existing single-package `src/` layout, extended by two new modules.
`emitter.py` and `amqp.py` are new top-level modules rather than a subpackage because the package
is flat today and `tokenweir.amqp` mirrors `tokenweir.postgres` — an adapter reached by its own
import path, absent from `tokenweir.__all__`, exactly as the store is (FR-027). `DirectSink` and
the `BatchSink` protocol go in `sink.py` because that is where sinks and the `Sink` protocol
already live; `sink.py` importing `source.py` introduces no cycle (`source.py` imports only
`contract`).

## Key design decisions

Recorded here because each is a trade the reviewer should be able to check against the spec rather
than reverse-engineer from the diff.

1. **One worker thread, daemon, per client.** A thread pool would reorder records and multiply
   broker connections for no gain — delivery is I/O-bound on one connection. Daemon so metering
   can never hold the interpreter open (FR-012); the cost, stated rather than hidden, is that
   records buffered at a hard exit are lost. `close()` (and the context manager) is the supported
   way to not lose them, and an `atexit` hook gives a best-effort bounded flush for callers who
   forget.

2. **A plain `list` under a `threading.Condition`, not `queue.Queue`.** `Queue` has no "drop the
   newest when full" mode — `put_nowait` raises `Full`, which would mean an exception on the metered
   path to be caught per call, and `put` blocks, which is worse. A sequence under a `Condition`
   gives the drop decision, batch draining, and the flush predicate in one lock. Drop-newest rather
   than drop-oldest: the records already accepted were accepted, and evicting them to make room for
   a newer one converts a bounded loss into an unbounded reshuffling of which records survive.

   *This plan first specified a `deque`, and the code shipped a `list`.* The deciding difference is
   how a **batch** leaves the buffer: `list` takes one `self._buffer[:size]` slice and one
   `del self._buffer[:size]` under the lock, where a `deque` needs a `popleft()` loop. Both are
   O(batch), but the slice holds the lock for one bytecode-level operation instead of *n*, and the
   lock here is the one every emitting thread contends for. `deque`'s advantage — O(1) `popleft`
   and `appendleft` — buys nothing when records only ever enter at one end and leave in slices.

3. **No retry (FR-005).** ADR-0001 says "swallows failures". A retry in front of a down broker
   fills the bounded buffer and turns record loss into *more* record loss plus latency; a poison
   record retried forever blocks every record behind it. Reconnection — the thing that actually
   recovers — belongs to the adapter and happens on the next batch (FR-024).

4. **Batch delivery via an optional protocol (FR-018).** `Sink.emit` is per-record and stays that
   way; a sink may additionally offer `emit_batch`. The client prefers it when present. This is
   what lets the direct path make one `Source.write` call per batch and keep the atomicity
   `PostgresSource` documents, without forcing every adapter to grow a method.

5. **The client is itself a `Sink` (FR-014).** So `emit_usage(client, fields)` composes with
   TOKWEIR-15's guarded seam with no adapter in between, and the record-construction guard and the
   delivery guard are reachable through one call on a request path.

6. **Rate-limited logging (FR-008).** A broken broker with a per-record `WARNING` produces one log
   line per metered request — the same "library taking an output decision" problem the
   `NullHandler` in `__init__.py` was added for. First occurrence logs; subsequent ones are counted
   and logged on a decaying/periodic basis with the suppressed count.

7. **`pika` deferred to construction, not module-level (FR-020).** Keeping it out of module import
   is required, not stylistic: `tests/test_contract.py` sweeps every module in the package with an
   AST check and fails an import-time third-party import. The same sweep also requires a deferred
   import to correspond to a declared extra — which `amqp = ["pika>=1.3"]` already does. It also
   makes `message_for()` testable with no `pika` installed (FR-025), the same rationale
   `postgres.py` gives for `row_for()`.

   **Where it stops being deferred is the load-bearing part**, and this plan originally got it
   wrong by saying "into the connect path". See the amendment on FR-020: deferring past construction
   puts the import on the publish path, which may not raise, turning a missing driver into a silent
   permanent drop rather than an error. The rule is "defer to the last point that can still report
   failure", and that is `AMQPSink.__init__`.

8. **The rate limiter lives in its own private module, `tokenweir/_ratelimit.py`.** It was planned
   as a helper inside `emitter.py`, but `DirectSink` (in `sink.py`) and `AMQPSink` need the same
   thing, and the alternatives are both worse: three copies, or two modules reaching into a third's
   privates. A leading-underscore module name says it is not API while letting the class inside have
   an ordinary name. It is stdlib-only, so the AST sweep covers it at no cost.

## Test approach

Every new test runs with no broker, no database and no `pika`, because that is the environment the
project's authoritative test command actually runs in:

- **AMQP** — the pure `message_for()` mapping directly; publishing against a recording channel
  double asserting exchange, routing key, body and `delivery_mode`. Constructing a sink needs a
  `pika` module object (see decision 7), so the publish tests get one of two ways: `properties=`,
  the documented escape hatch, which needs no `pika` in any form; or a `fake_pika` fixture that
  injects a minimal module into `sys.modules`, which is the only way to cover `from_url` and the
  default properties at all. The missing-driver path uses `sys.modules["pika"] = None`, the same
  technique `test_postgres_source.py` uses for psycopg.
- **Direct** — `MemorySource`, plus a raising source for containment and a counting source for
  "one `write` per batch, not one per record".
- **Client** — deterministic tests over a synchronous drain where possible (drive the worker's
  batch step directly) plus a small number of real-thread tests with generous timeouts and no
  `sleep`-based synchronization: `threading.Event` and the client's own `flush(timeout=…)` are the
  synchronization points, so the suite does not become flaky on a loaded CI box.
- **Dependency-light** — a subprocess import assertion for `pika`, mirroring the existing psycopg
  one, so the result is independent of whether `pika` is installed in the running environment.

## Complexity Tracking

No constitution violations to justify — see Constitution Check above for why the gate is ADR-0001
rather than the unfilled constitution template. The one piece of genuine added complexity, the
`BatchSink` optional protocol, is justified in Key design decision 4 and is additive: no existing
sink changes.
