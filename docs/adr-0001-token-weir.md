# ADR-0001: Extract usage metering into Token-Weir, a standalone provider-neutral component

**Status:** Proposed
**Date:** 2026-08-07
**Deciders:** Mike
**Affected:** AI Gateway, MADO (token/cost tracking — MADO-216), new Token-Weir project

## Context

MADO is moving from a personal tool toward a product, and two roadmap items depend on knowing how much a unit of work costs: per-issue / per-phase token & cost tracking (MADO-216) and per-project review agents (MADO-217), both of which run Claude and want their consumption attributed.

The observability pipeline that would answer this already exists inside the **AI Gateway** (usage record → RabbitMQ `usage` queue → usage-writer → `gateway_usage` in Postgres → `gateway_usage_daily` rollup → pricing at report time). But the AI Gateway is **homelab-resident** and does two jobs at once: it routes to the local NVIDIA/Jetson fleet *and* fronts the Anthropic API. Its local-model role does not travel to the cloud.

This collides with the planned **AWS/EKS deployment** (MADO-218). The moment MADO runs outside the homelab, "reuse the gateway for usage tracking" stops being free, because the gateway is entangled with homelab-only concerns. It also collides with the **open-sourcing** ambition: the compliance-review differentiator only works if a regulated customer can run MADO — and its metering — inside their own tenant, never routing Claude traffic through someone's house.

Two additional forces:

- **MADO also runs Claude Code against a Claude Max subscription**, not only a per-token API key. Under a flat-rate subscription there is no per-call dollar, and — established via Claude Code docs (see Pillar 4) — subscription (OAuth) auth cannot be metered by API interception the way an API key can.
- **Metering is not the moat.** Usage metering is generically useful and non-differentiating; the moat is the orchestration and the compliance/review agents. That makes metering a good *first open-source artifact* and a clean open-core boundary.

## Decision

Extract the usage/observability pipeline out of the AI Gateway into a **standalone, provider-neutral, transport-agnostic component — "Token-Weir" (package `tokenweir`)** — in its own repository and its own Jira/software project. The AI Gateway and a new **MADO cloud-edge** (a Claude-only proxy deployable in EKS) both consume it as a versioned dependency. Token-Weir is the intended first open-source release and the open-core boundary (metering open; compliance/review agents and hosted MADO proprietary).

Token-Weir is created by **extracting the working gateway code, not greenfielding** — preserving the correctness already earned there (real-Postgres tests, the `DATE_TRUNC` STABLE-vs-IMMUTABLE fix, `is_priced`/`BOOL_AND` handling, off-critical-path logging).

## Options Considered

### Option A: Reach back to the homelab gateway over Tailscale
Cloud stream pods point `ANTHROPIC_BASE_URL` at the existing homelab gateway across the Tailscale subnet route; nothing is extracted.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low (uses what exists) |
| Cost | Low upfront |
| Scalability | Poor — hairpins cloud traffic through a home connection |
| Team familiarity | High |

**Pros:** Fastest path to per-issue/phase numbers for a demo; full reuse.
**Cons:** Cloud workloads depend on home-internet uptime; routes production/customer traffic through a residence; requires the deferred gateway-auth work (currently trust-the-network); cannot be offered to a customer in their own tenant. Acceptable as a trial crutch, not as a product.

### Option B: Extract a standalone metering component (chosen)
Pull the pipeline into `tokenweir`; gateway and MADO cloud-edge both depend on it; transport-agnostic; provider-neutral core.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (extraction + interface design + schema-ownership move) |
| Cost | Moderate one-time refactor |
| Scalability | Strong — deployable in-tenant, homelab or cloud, broker or broker-less |
| Team familiarity | High (same code, re-homed) |

**Pros:** Cleanly separates observability (portable) from routing (homelab-bound); deployable in a customer tenant; is the open-core boundary in code; makes the EKS move and the gateway independent.
**Cons:** Real refactor; schema/migration ownership must move; risk of premature over-abstraction on the transport boundary.

### Option C: Bypass the gateway in the cloud (OTel-only)
Leave the gateway purely homelab; in EKS, capture usage from Claude Code's OpenTelemetry export and ship to a separate store.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| Cost | Low–moderate |
| Scalability | OK |
| Team familiarity | Medium |

**Pros:** Simple; no interception; works under both auth modes.
**Cons:** Two divergent usage mechanisms (homelab vs cloud) to maintain; loses the unified contract and the `/v1/messages` tagging path; OTel is batched (~60s) and grouped by API request, not by iteration. Better as a *fallback source adapter* than as the architecture.

## Trade-off Analysis

Option A optimizes for today and mortgages tomorrow; it can't cross the tenant boundary the compliance play requires, so it's demo-only. Option C is really a capture-source choice, not an architecture — and Token-Weir can absorb it as one adapter rather than adopting it wholesale. Option B costs a real refactor now but is the only one that makes the three goals (EKS, in-tenant metering, open-core) mutually compatible, and it lets Option A and Option C both survive as *deployment/adapter choices under one component*: homelab keeps RabbitMQ; the cloud-edge can go broker-less; OTel becomes a fallback source. The decisive factor is the compliance/open-core direction, which forces metering to be co-deployable in-tenant regardless — that is Option B.

## Design Pillars

### Pillar 1 — Standalone extraction and open-core boundary
`tokenweir` is its own repo and Jira project. AI Gateway becomes "router + first consumer"; the MADO cloud-edge is a second consumer; external OSS users are the third. Metering is open; compliance/review agents and hosted MADO stay proprietary.

### Pillar 2 — Transport-agnostic core
The core knows nothing about the wire. It defines a serializable, versioned **usage record contract**, a `Sink` interface (emit side) and a `Source` interface (write side). Transport lives in swappable adapters shipped as optional extras (e.g. `tokenweir[amqp]`) so the core stays dependency-light (no `pika` compiled in); advanced users implement their own. Emission is **fire-and-forget / off the critical path** by contract — buffers, returns immediately, swallows failures — preserving the gateway's invariant that a logging outage cannot affect availability. Consequence: homelab keeps RabbitMQ (gateway → AMQP → writer, as today); the EKS cloud-edge can drop the broker entirely and write direct/in-process.

### Pillar 3 — Provider-neutral core, Claude adapters first
The record and the rate card are keyed by model, not hardwired to Anthropic — the gateway already meters the local Ollama/Jetson fleet alongside Claude, so provider-neutrality is existing behavior, not aspiration. What is provider-specific lives in **source adapters**. Claude / Claude Code adapters ship first; the public description is scoped to Claude until more adapters are tested. Other providers = additional adapters, not a core rewrite. The name and core stay neutral to avoid a future rename.

### Pillar 4 — Dual capture modes by auth (the subscription pillar)
Capture mechanism differs by how Claude Code is authenticated; both feed the *same* contract with a `pricing_mode` tag.

- **API-metered (`pricing_mode=api_metered`):** interception via the Anthropic-compatible `/v1/messages` edge (AIGWAY spec 003). Rate-card cost derivable at report time.
- **Subscription / Max (`pricing_mode=subscription`):** cannot be metered by interception — subscription is OAuth-based and not a base-URL-swappable API path. Capture instead from Claude Code's own record, via a **deterministic hook**, not an LLM skill/agent (a skill would burn tokens into the very window being measured and can't reliably read its own counts).

  Grounded mechanics (Claude Code docs, verified 2026-08-07):
  - **Trigger:** the `Stop` hook — fires after Claude finishes a response turn, the natural per-iteration boundary. Configure `exit 0` / non-blocking (never `exit 2`) and a short `timeout` (e.g. 30s), optionally `async: true`, so a network failure never blocks the session — this *is* the off-critical-path guarantee.
  - **Data:** the hook receives `transcript_path` on stdin; assistant messages in the session JSONL carry `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` (plus `ephemeral_5m/1h` cache fields). The hook sums the delta since the last emit (a turn may span multiple assistant messages) and calls the `tokenweir` emitter.
  - **Phase/issue context:** hooks inherit Claude Code's process environment, so MADO's orchestrator injects `MADO_ISSUE_KEY`, `MADO_PHASE`, `MADO_STREAM_ID`, `MADO_PRICING_MODE` per iteration; the hook reads them onto the record. Attribution comes from the orchestrator, not the model.
  - **Fallback source:** Claude Code's OTel `claude_code.token.usage` metric (works under both auth modes) is a viable secondary adapter where an OTLP collector exists, accepting coarser (~60s, per-API-request) granularity.

  `Unverified:` docs don't explicitly confirm token-count parity between subscription and API-key transcripts — the single check is to diff a Max-session transcript against an API-key session on the same model/context. Do this before relying on subscription numbers.

  **Scope now:** store raw tokens/requests per iteration under `pricing_mode=subscription`; **defer** %-of-limit / capacity modeling (Max tiers and weekly caps are volatile). Cost/capacity views are computed at report time, consistent with "raw stored, dollars derived, provider invoice authoritative."

### Pillar 5 — Schema and migration ownership
Ownership of `gateway_usage` and its forward-only migrations (001–006) moves into Token-Weir. The AI Gateway stops owning the schema and pins a `tokenweir` version. This is the highest-risk part of the extraction: migrations remain forward-only with no DROP-without-review, and consumers must not diverge their schema. Whoever owns the repo owns the schema.

## Consequences

**Easier:** running MADO (and its metering) in EKS or a customer tenant; a clean open-core release; independent evolution of gateway routing vs. usage accounting; adding new providers or capture sources as adapters; attributing both API and Max usage through one contract.

**Harder / new work:** a real extraction refactor; defining and versioning the public record contract and Sink/Source interfaces; moving schema/migration ownership; building the subscription `Stop`-hook adapter and the orchestrator env injection; standing up a broker-less cloud writer path.

**To revisit:** the `Unverified` subscription-transcript parity check; whether the cloud-edge writer co-deploys with or separates from the store; %-of-limit capacity modeling once the Max limit landscape settles; OSS license choice (permissive vs. AGPL) — deferred to release time, not blocking this ADR.

## Action Items

1. [x] Create the new Token-Weir Jira/software project — done, key `TOKWEIR`.
2. [x] File the extraction epic + stories in the new project (contract, emitter client, writer+migrations, transport adapters, subscription hook adapter).
3. [ ] Rewrite MADO-216 story 1: target the cloud-edge / `tokenweir` consumer instead of assuming the homelab gateway; add the reconciliation note and the AIGWAY-003 dependency.
4. [ ] Refactor AI Gateway to consume `tokenweir` (move schema/migrations out; pin a version).
5. [ ] Build the subscription capture adapter: `Stop` hook → transcript delta → `tokenweir` emitter; orchestrator injects `MADO_*` env per iteration.
6. [ ] Verify subscription vs API-key transcript token parity (the `Unverified` check) before trusting Max numbers.
7. [ ] Defer: %-of-limit capacity model; OSS license selection.

### Landed in the library so far

Recorded here because Pillar 2 is the pillar most easily claimed and least easily
checked, and these are the commits that make it true rather than stated:

- **TOKWEIR-4** — the versioned `UsageRecord` contract and its published JSON Schema.
- **TOKWEIR-15** — the guarded seam (`build_record` / `emit_record` / `emit_usage`),
  which keeps record *construction* off a metered request's critical path.
- **TOKWEIR-5** — schema ownership: the migrations, `PostgresSource`, and the
  report-time pricing view.
- **TOKWEIR-6** — `BufferedEmitter`, the client that actually discharges the
  fire-and-forget contract; `DirectSink`, the broker-less path this ADR names as
  Pillar 2's consequence; and `tokenweir.amqp`, the homelab's transport, under the
  `tokenweir[amqp]` extra with no `pika` in the core.

Still outstanding on the consumer side: item 4 (the gateway consumes `tokenweir`)
and item 5 (the subscription `Stop`-hook adapter). Both now have a client to adopt
rather than an interface to re-implement.
