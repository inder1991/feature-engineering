# Feature Engineering Platform — Build Roadmap

**Status:** Roadmap (decomposition of the reference architecture into buildable sub-projects)
**Date:** 2026-06-27
**Updated:** 2026-07-02 — added **Phase E** (SP-13..15, serving & consumption) and **Phase F** (SP-16, discovery intelligence — design §17), from the full platform review (`2026-07-02-platform-review-sp0-sp15.md`)
**Companion to:** [`2026-06-27-feature-engineering-platform-design.md`](./2026-06-27-feature-engineering-platform-design.md)

---

## 1. Purpose

The reference architecture describes the full target-state platform (seven layers, ~40 components). This document decomposes it into **independently-buildable sub-projects** with explicit dependencies and a build order. Each sub-project is sized to take its own `spec → plan → implementation` cycle.

## 2. The core sequencing decision: vertical slice first

Building the layers horizontally (Layer 0 → 1 → 2 → …) means building a great deal *before* the end-to-end spine is proven, and building Layer 7 governance for features that do not yet exist. Instead:

> **Lay a thin backbone, drive one feature type all the way through (happy path, Path 1 only), prove the architecture end-to-end, then thicken each layer.**

The salary-irregularity example from the design doc is the chosen first slice precisely because it exercises the hardest correctness problem — point-in-time / availability-time leakage (`posted_at`) — on day one.

## 3. Dependency graph

```
PHASE A — FOUNDATIONS  (build in parallel; everything plugs into these)
  ┌─ SP-0  Contract + State Machine + Durable Workflow + Audit log
  └─ SP-1  Metadata Overlay service (store + merged-view API + console)
     + SP-0.5 / SP-1.5  hardening of the built foundations (2026-07-02 review) —
       authn boundary · DLQ/poison fix · WORM audit · runtime daemons · migrations ·
       observability · KMS · read-time expiry · pre-expiry renewal · group ownership ·
       bulk confirmation        [must land before SP-3 builds on these seams]
     + SP-1b  Domain / Use-Case Catalog service (design §15) — store · versioning ·
       owner/Compliance confirmation · read API       [consumed by SP-3, SP-5, SP-7]
                │
                ▼
PHASE B — VERTICAL SLICE / MVP  (one feature type, Path 1 only, happy path)
  SP-2  Intake + Clarification (L1–2)
     └─► SP-3  Data Grounding (L3)          [needs SP-0, SP-1]
            └─► SP-4  Validation core + DSL compiler + Sandbox (L4–5)
                   └─► SP-5  Thin Eval + Approval + Registry + Store (L6–7)
                ◆ MILESTONE: a feature goes request → registered → materialized, end to end
                  (capped at APPROVED_EXPERIMENTAL until SP-7 + SP-10 — see §4)
                │
                ▼
PHASE C — COVERAGE  (broaden what can be built; parallelizable)
  SP-6  Path 2: LLM candidate SQL + repair loop + SQL validation gate
  SP-7  Full validation suite + semantic/proxy leakage + fairness + overfitting guard
  SP-8  Critique Service formalized (all 5 modes)
  SP-12 Hypothesis-driven Feature Generation (multi-candidate + model-free scoring; bounded/assistive)
  SP-11a Exposure & access enforcement (pulled forward from Phase D, 2026-07-02) —
         purpose-based access · policy-aware exposure · row/column-level ·
         store-read checks                      [gates Wave-2 AML data onboarding]
                │
                ▼
PHASE D — HARDENING  (make it bank-grade & durable)
  SP-9   Governance: MRM hooks · explainability artifact · SoD · reproducibility
  SP-10  Lifecycle & Monitoring · drift/PSI · change-impact · harvest loop
  SP-11b Path 3 (human-authored SQL, with friction) — enforcement half moved to SP-11a (Phase C)

                │
                ▼
PHASE E — SERVING & CONSUMPTION  (the feature-store *product*; added 2026-07-02)
  SP-13  Online serving + streaming (online store · stream compute · on-demand
         transforms · offline/online equivalence testing)
  SP-14  Consumption (PIT training-set service · Python SDK · serving read path ·
         model ↔ feature consumer registration)
  SP-15  Adoption (discovery/search console · brownfield import · dev/UAT/prod
         environments + promotion · features-as-code)
                │
                ▼
PHASE F — DISCOVERY INTELLIGENCE  (design §17; added 2026-07-02)
  SP-16  Signal atlas · multi-fidelity screening funnel · portfolio-aware scoring ·
         semantic data cards · graph/peer feature families · proactive triggers ·
         production outcome loop + preference reranker

CROSS-CUTTING (designed in at SP-0, completed in Phase D):
  audit logging · identity/RBAC · segregation of duties

DESIGN HOOKS RESERVED EARLY (cheap now; make Phases E/F additive, not rewrites):
  dual-target-compilable DSL ops (SP-4) · online-aware registry schema incl.
  serving_mode (SP-5) · training-set service scoped into SP-5 · consumer-
  registration schema (SP-5/SP-9) · single scorer seam + structured rejection
  enum (SP-2/SP-12) · cumulative-comparison ledger (SP-5/SP-7)
```

## 4. Sub-projects

### Phase A — Foundations (parallel; the backbone)

| ID | Delivers | Depends on | Why now |
|---|---|---|---|
| **SP-0** | Feature Contract data model (draft → confirmed → mapped → plan); the state machine; the durable workflow runtime (persist across days, retry with hard loop limits, human-gate SLA → escalation → auto-park); audit log | — | Everything needs a place to live and a way to advance. Without it there is no spine. |
| **SP-1** | Overlay store; merged-view read API (catalog + overlay); confirmation console; strict confirmation authority (data owner vs Compliance); freshness/expiry | — | Hard dependency for grounding and validation; self-contained behind one query interface, so it builds in parallel with SP-0. |
| **SP-0.5 / SP-1.5** *(added 2026-07-02)* | Hardening of the built foundations. **SP-0.5:** real authentication boundary (OIDC/JWKS humans, attested service identity); DLQ/poison-message fix; signed/WORM audit anchoring + events write-once trigger; runtime daemons (worker · relay · timer · projection) with observability; migration framework; KMS implementation behind crypto-shred. **SP-1.5:** read-time expiry enforcement in `resolve_fact`; pre-expiry renewal (no recurring fail-closed outage windows); group ownership + delegation; bulk confirmation + data-class facts; catalog adapter protocol for real catalogs (batched, incremental `changes_since`, multi-catalog keys). | SP-0, SP-1 | The 2026-07-02 review found the built kernels correct but inoperable/forgeable in production; hardening is far cheaper **before** SP-3 consumers freeze the current seams into their assumptions. |
| **SP-1b** *(added 2026-07-02)* | **Domain / Use-Case Catalog service** (design §15): versioned store; domain-owner + Compliance confirmation via SP-0 gates; read API; use-case onboarding workflow (§15.6). | SP-0, SP-1 | Three layers consume it (L3 policy scoping, L6 target/metric, L7 governance guards) and no SP built it — SP-3 and SP-5 cannot be specced against a read-only markdown seed file. |

### Phase B — Vertical slice / MVP (mostly sequential; the proof)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-2** | LLM intake agent; **two intake modes** (hypothesis-driven generation vs definition-driven translation); draft contract; ambiguity + confidence scoring; doubt router; **Human Gate #1** (including the calculation-method choice, picked from scored candidates); confirmed contract | SP-0 |
| **SP-3** | Policy-aware schema mapper; entity/grain resolver; **point-in-time + SCD mapper**; mapped contract; mapping review | SP-0, SP-1 |
| **SP-4** | Core validation packs (schema, type, entity, grain, join, **temporal leakage / PIT**, policy); Implementation Router (Path-1 only); small DSL operation catalog + compiler; sandbox on masked data; feature-type DQ checks | SP-0, SP-1, SP-3 |
| **SP-5** | Thin offline evaluation via **model-free scoring** (IV/WoE); **Label/Target service** *(added 2026-07-02)* — executable, versioned label contracts with PIT-validated label joins and maturity/embargo rules (prerequisite for any honest usefulness score); the **staged verification stamp** (Design → Data → Usefulness-checked); **Human Gate #2** with augmented review; final approval; immutable registry entry (**online-aware schema:** `serving_mode` + freshness contract, per the reserved design hooks); batch materialization to the store | SP-4, SP-1b |

**◆ Milestone (end of SP-5):** a data scientist takes one feature type from free text → clarified → grounded → validated → DSL-compiled → sandboxed → evaluated → approved → registered → materialized. The architecture is proven end-to-end.

**Cap (added 2026-07-02):** milestone features register as **`APPROVED_EXPERIMENTAL`** (a state SP-0 already supports). `PRODUCTION` is reachable only once **SP-7** (overfitting guard, semantic-leakage detection, fairness) and **SP-10** (a *running* monitoring executor) exist — otherwise the milestone would violate the design's own hard gates (§10 "no monitoring spec → no production feature"; §14.5 USEFULNESS-CHECKED requires the §14.4 guard), and the first registered features would surface later as an MRM finding.

### Phase C — Coverage (parallelizable after the milestone)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-6** | Path 2 (LLM candidate SQL); self-critique repair loop (non-gate); SQL validation gate for arbitrary SQL | SP-4, SP-5 |
| **SP-7** | Remaining validation packs (backfill, cost, serving-feasibility, duplication incl. rejected/in-flight); **semantic/proxy leakage detection + fairness gate**; the **search-overfitting guard** (out-of-time re-check) at evaluation | SP-4, SP-5 |
| **SP-8** | Critique Service as one reusable service across all five modes (CONTRACT / MAPPING / LOGIC / CODE / EVALUATION) | SP-2, SP-3, SP-4 |
| **SP-12** | Hypothesis-driven **Feature Generation engine** (bounded, assistive): multi-candidate generation, attempt memory + diversity (islands), model-free scoring, fast resource-cap discard; **proposes only — never bypasses gates** (design §14) | SP-4, SP-5, SP-7 |
| **SP-11a** *(pulled forward from Phase D, 2026-07-02)* | **Exposure & access enforcement:** purpose-based (use-case-scoped) access; policy-aware exposure; row/column-level enforcement with the **enforcement point stated explicitly** (mapper at exposure time + store/serving reads); store-read purpose checks | SP-3, SP-4 |

**Sequencing rule (2026-07-02):** SP-11a must complete **before Wave-2 (AML) data classes are onboarded** — SAR-derived labels carry criminal tipping-off exposure and may not exist in a store without read-side purpose enforcement.

### Phase D — Hardening (bank-grade & durable)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-9** | Model-risk/model-inventory hooks; explainability as a registered artifact; four-eyes / segregation-of-duties enforcement; data-snapshot reproducibility | SP-5, cross-cutting threads |
| **SP-10** | Feature Lifecycle Manager; drift / PSI monitoring; Change-Impact Analyzer; revalidation / deprecation; **Path-2 → Path-1 harvest loop** | SP-5, SP-6 |
| **SP-11b** *(renamed 2026-07-02 — enforcement half moved to SP-11a, Phase C)* | Path 3 (human-authored SQL with friction): named author, recorded justification, senior review; RBAC completion | SP-4, SP-9, SP-11a |

### Phase E — Serving & consumption (the feature-store product; added 2026-07-02)

Phases A–D build the world's best-governed feature *factory*; Phase E is the *product* around it — the half every commercial platform (Tecton/Feast/Hopsworks) leads with. Without it, governed features exist that no model can conveniently consume, no team can find, and no real-time decision can use.

| ID | Delivers | Depends on |
|---|---|---|
| **SP-13** | **Online serving + streaming:** low-latency online store + materialization-to-online path with governance parity; streaming ingestion + governed window aggregations **compiled from the same DSL** (velocity/recency features for fraud & AML); on-demand/request-time transform class (**Path-1 only** — un-vetted generated SQL never reaches a real-time path); single-definition-compiled-to-both with **mandatory offline/online equivalence testing** (design §16) | SP-4 (dual-target DSL hook), SP-5, SP-10 |
| **SP-14** | **Consumption:** PIT-correct training-set generation service (label spine + as-of join, reusing the SP-3 PIT engine; versioned datasets for SR 11-7 independent validation); Python SDK + batch-scoring retrieval; REST registry API; serving read path with **use-case-scoped authorization** (SP-11a enforcement applied at read time); **model ↔ feature consumer registration** (also unblocks SP-9 inventory hooks and SP-10 change-impact/deprecation) | SP-3, SP-5, SP-11a |
| **SP-15** | **Adoption:** discovery/search console (entity/use-case/data-class facets, scores, usage, lineage); **brownfield import** of the existing estate (register-as-is with an honest DESIGN-CHECKED stamp; LLM-assisted SAS/SQL → contract extraction; optional re-derivation into Path 1 via the harvest loop §5.8); dev/UAT/prod environments + promotion reusing Gate-#2 machinery; features-as-code contract files for power users | SP-5, SP-9, SP-14 |

**◆ Milestone (end of SP-14):** a model trains on a platform-generated, PIT-correct, versioned training set and scores in production against the online store. The platform is now a product, not only a pipeline.

### Phase F — Discovery intelligence (design §17; added 2026-07-02)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-16** | **Discovery Intelligence (design §17):** signal atlas (deterministic enumeration over the approved join graph + cheap IV scan); multi-fidelity screening funnel with governed per-hypothesis budgets; portfolio-aware conditional scoring (incremental value vs the registered portfolio); semantic data cards (profiler extension); graph + peer-relative feature families; proactive discovery triggers (new data / model degradation / label arrival); production outcome loop + gate-preference reranker. **Precondition:** cumulative overfitting accounting (§17.1). | SP-7, SP-12, SP-14 (the outcome loop needs live consumption) |

**Staging note:** SP-16's build-time components (atlas, funnel, portfolio scoring, data cards) have no production dependency and may start once SP-5's evaluation machinery exists — only the outcome loop and preference reranker wait for Phase E to be live.

## 5. Sequencing rationale

- **Retire the riskiest unknowns first.** Point-in-time correctness and DSL viability are the two things most likely to invalidate the whole concept; Phase B exercises both immediately on the salary example.
- **The milestone is a real walking skeleton.** After SP-5 the spine is proven; everything afterward *broadens* and *hardens* a working system rather than betting on an unproven one.
- **Two cross-cutting threads cannot be deferred.** Audit logging and identity/SoD must be designed into SP-0 (even minimally) and completed in SP-9 — retrofitting governance at the end is a known failure mode.
- **Phase C and most of Phase D parallelize** once the spine exists; SP-6 / SP-7 / SP-8 can run concurrently across teams.
- **Phase E is where the platform becomes a product.** Governance (Phases A–D) is the durable differentiator, but consumption/serving is the adoption surface — without SP-13/14/15, teams copy registered SQL out of the registry and run it un-audited, and the highest-value real-time workloads stay out of reach. The design hooks (§3) are reserved early precisely so Phase E is additive.
- **Phase F comes last because it must.** The §17 outcome loop learns from production adoption and retained feature importance — signal that only exists once Phase E features are being consumed. Its build-time components (atlas, funnel, portfolio scoring) can begin earlier, against SP-5's evaluation machinery.
- **The milestone honors its own gates.** Phase B/C output is capped at `APPROVED_EXPERIMENTAL`; `PRODUCTION` requires the SP-7 guard and a running SP-10 monitoring executor. Shipping "production" features that don't meet the design's published bar is worse than shipping experimental ones that honestly say so.
- **Enforcement precedes sensitive data.** SP-11a (exposure / row-column / store-read enforcement) moves to Phase C and gates Wave-2 AML onboarding; only Path-3 authoring (SP-11b) remains in hardening.
- **Harden before extending.** SP-0.5/SP-1.5 close the review's blocker-tier findings on the built foundations (self-asserted identity, fail-open reads, no production daemons) before SP-3 freezes those seams into a third consumer.

## 6. Per-sub-project process

Each sub-project is taken individually through:

```
brainstorm (spec)  →  writing-plans (implementation plan)  →  implementation
```

The reference architecture (companion doc) is the shared contract every sub-project spec refers back to.

## 7. Recommended starting point

**SP-0 (Foundations)** — it pins down the Feature Contract data model, the state machine, and the durable workflow runtime that every later spec references. **SP-1 (Metadata Overlay)** is a strong parallel alternative if a self-contained service is preferred as the first build.

> SP-0 now has a written design spec: [`2026-06-27-sp0-foundations-design.md`](./2026-06-27-sp0-foundations-design.md) — immutable staged document chain, event-sourced state + projections, roll-your-own durable runtime (outbox · idempotency · durable timers · bounded retries), and identity + structural SoD.
