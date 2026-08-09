# ADR-0002: Open/closed IP boundary — MADO & Token-Weir open, review/compliance agents proprietary

**Status:** Proposed
**Date:** 2026-08-07
**Deciders:** Mike
**Related:** ADR-0001 (Token-Weir extraction); MADO-217 (review agents)

## Context

MADO is being positioned not as a standalone product but as the credibility engine for an **AI coaching / consulting services** business — helping companies that struggle to get value from AI in their software-development orgs set up and customize MADO, or teaching them how to think about modern agentic development. The product window for MADO-the-product looks small: large AI vendors are likely to ship MADO-like orchestration soon, and broader tools already exist in the wild (e.g. Yegge's GasTown). Competing on breadth is a losing game; the durable asset is demonstrated judgment, not the orchestrator code.

That argues for openness — but not uniformly. Two classes of asset behave differently:

- **MADO (orchestration) and Token-Weir (metering)** are impressive to watch and hard to rebuild quickly; showing them builds credibility and costs nothing to give away. Breadth and polish are the value.
- **The adversarial review + security/compliance agents** are the opposite: compact, and once the mechanism is seen a client can stand up a crude equivalent in their own Claude session the same day. Here, *the demo is the leak*. This layer embodies the expertise clients would otherwise pay for.

The prior IP discussion already resolved: no patent (doesn't serve a services model, won't stop incumbents, forecloses openness); permissive licensing for adoption; trademark the brand as the one asset worth registering.

## Decision

Adopt a **three-tier IP structure**:

1. **Open (Apache-2.0), shown freely** — MADO orchestrator and Token-Weir (`tokenweir`). The marketing funnel and credibility. Apache-2.0 for maximum enterprise-friendly adoption and its express patent grant.
2. **Proprietary / trade-secret / NDA-gated** — the adversarial review and security/compliance agents: their prompts, review rubrics, compliance rule-sets, and the calibration of what to flag. Kept in a **separate private repository and Jira project**, never in the open repos. Revealed only under NDA and, ideally, only within a paid engagement.
3. **The service** — setup, environment-specific tuning, and teaching. The revenue, and the thing that is genuinely un-copyable.

No patent. Trademark the **MADO** and **Token-Weir** names — in a services + open-source model the brand is the protectable asset.

## Options Considered

### Option A: Open everything; execution is the moat
Open-source MADO, Token-Weir, *and* the review/compliance agents; rely on calibration and expertise being hard to copy.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Low |
| Adoption | Highest |
| Protection of differentiator | Weak |
| Fit for solo operator | Poor |

**Pros:** Maximum reach and goodwill; simplest to maintain.
**Cons:** Gives away the compact, copyable layer that clients would pay for; a solo operator can't out-execute at scale to compensate; arms competing consultancies with the good version, not just the concept.

### Option B: Open-core at metering vs. agents (ADR-0001's first framing)
Open the metering; keep *all* agents proprietary, bundled as one closed product tier.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium |
| Adoption | Medium (MADO itself less open) |
| Protection of differentiator | Medium |
| Fit for solo operator | Medium |

**Pros:** Clear closed tier.
**Cons:** Closes more of MADO than the credibility goal needs; MADO's breadth is exactly what you *want* to show. Over-protects the orchestrator, under-leverages it as a funnel.

### Option C: MADO + Token-Weir open; review/compliance closed (chosen)
Draw the line at the review/compliance layer specifically.

| Dimension | Assessment |
|-----------|------------|
| Complexity | Medium (open/closed boundary to maintain) |
| Adoption | High (MADO fully open) |
| Protection of differentiator | Strong (the copyable-on-sight layer stays hidden) |
| Fit for solo operator | Strong |

**Pros:** Shows off the impressive, hard-to-copy orchestration for credibility; protects the compact, easy-to-copy expertise; matches the services business model exactly.
**Cons:** Requires disciplined separation so the closed layer never leaks through the open repo.

## Trade-off Analysis

The boundary is drawn by *copyability-on-sight*, not by component type. MADO and Token-Weir survive being shown because rebuilding them is real work; the review/compliance agents do not, because seeing the mechanism is most of the recipe. Option A optimizes adoption at the cost of the only thing clients pay for; Option B protects the wrong layer and blunts the funnel; Option C matches protection to where value actually leaks. The decisive fact is the services model: revenue comes from expertise and trust, so openness is a marketing cost worth paying on MADO, and secrecy is a value-preservation move worth making on the review layer.

## What the NDA does and doesn't do

An NDA legally bars a signer from using *your specific materials* — rubrics, rule-sets, prompts — and is worth having before any disclosure. It does **not** stop a client from independently building a generic adversarial-review agent; the concept is obvious and un-protectable. Real protection is trade secret + access control: don't show the good version until under contract, and deliver it as a configured/hosted capability rather than copyable source. NDA is the floor; "only inside a paid engagement" is the ceiling.

## Architectural boundary (leak prevention)

Because the review agents plug into MADO and MADO is public:

- Open MADO exposes only a **generic review-agent extension point / interface** — enough to show the hook exists and that the OSS is extensible.
- The actual security/compliance **packs live in the private repo**, injected at deploy/config time.
- Any examples or tests in the open repo stay **toy-grade** — a realistic sample rubric committed to a public repo leaks the method as surely as a live demo.
- Confidential materials are marked as such, access-controlled, and kept out of any client-accessible repo.

## Consequences

**Easier:** free, high-credibility marketing via MADO; a clean story for prospects (show MADO, describe the review value by outcomes, demo the review layer only under NDA); the build-it-themselves segment self-selects out but was already captured as a lead.

**Harder / new work:** maintaining the open/closed separation as a solo operator; a second private repo + Jira project; discipline to keep the open repo's examples non-revealing; NDA in hand before any review-layer disclosure.

**To revisit:** whether a regulated-vertical product ever emerges from repeated client demand (services-first will reveal it; permissive OSS keeps a later commercial layer open); trademark filing timing.

## Action Items

1. [ ] Reframe **MADO-217** to just the open, generic review-agent extension point; move the proprietary packs out.
2. [ ] Create the private Jira project + private repo for the review/compliance IP (admin — needs a project key from Mike); build its backlog.
3. [ ] Apply **Apache-2.0** to MADO and `tokenweir` at publish; confirm no proprietary material in either repo (incl. examples/tests).
4. [ ] Prepare an NDA (mutual) to have ready before showing the review/compliance tooling to any prospect.
5. [ ] Trademark the **MADO** and **Token-Weir** names.
6. [ ] Defer: regulated-vertical productization decision.
