# Feature Engineering Platform — Build Roadmap

**Status:** Roadmap (decomposition of the reference architecture into buildable sub-projects)
**Date:** 2026-06-27
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
                │
                ▼
PHASE B — VERTICAL SLICE / MVP  (one feature type, Path 1 only, happy path)
  SP-2  Intake + Clarification (L1–2)
     └─► SP-3  Data Grounding (L3)          [needs SP-0, SP-1]
            └─► SP-4  Validation core + DSL compiler + Sandbox (L4–5)
                   └─► SP-5  Thin Eval + Approval + Registry + Store (L6–7)
                ◆ MILESTONE: a feature goes request → registered → materialized, end to end
                │
                ▼
PHASE C — COVERAGE  (broaden what can be built; parallelizable)
  SP-6  Path 2: LLM candidate SQL + repair loop + SQL validation gate
  SP-7  Full validation suite + semantic/proxy leakage + fairness + overfitting guard
  SP-8  Critique Service formalized (all 5 modes)
  SP-12 Hypothesis-driven Feature Generation (multi-candidate + model-free scoring; bounded/assistive)
                │
                ▼
PHASE D — HARDENING  (make it bank-grade & durable)
  SP-9   Governance: MRM hooks · explainability artifact · SoD · reproducibility
  SP-10  Lifecycle & Monitoring · drift/PSI · change-impact · harvest loop
  SP-11  Path 3 (human-authored SQL) + security/access enforcement (RBAC, exposure)

CROSS-CUTTING (designed in at SP-0, completed in Phase D):
  audit logging · identity/RBAC · segregation of duties
```

## 4. Sub-projects

### Phase A — Foundations (parallel; the backbone)

| ID | Delivers | Depends on | Why now |
|---|---|---|---|
| **SP-0** | Feature Contract data model (draft → confirmed → mapped → plan); the state machine; the durable workflow runtime (persist across days, retry with hard loop limits, human-gate SLA → escalation → auto-park); audit log | — | Everything needs a place to live and a way to advance. Without it there is no spine. |
| **SP-1** | Overlay store; merged-view read API (catalog + overlay); confirmation console; strict confirmation authority (data owner vs Compliance); freshness/expiry | — | Hard dependency for grounding and validation; self-contained behind one query interface, so it builds in parallel with SP-0. |

### Phase B — Vertical slice / MVP (mostly sequential; the proof)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-2** | LLM intake agent; **two intake modes** (hypothesis-driven generation vs definition-driven translation); draft contract; ambiguity + confidence scoring; doubt router; **Human Gate #1** (including the calculation-method choice, picked from scored candidates); confirmed contract | SP-0 |
| **SP-3** | Policy-aware schema mapper; entity/grain resolver; **point-in-time + SCD mapper**; mapped contract; mapping review | SP-0, SP-1 |
| **SP-4** | Core validation packs (schema, type, entity, grain, join, **temporal leakage / PIT**, policy); Implementation Router (Path-1 only); small DSL operation catalog + compiler; sandbox on masked data; feature-type DQ checks | SP-0, SP-1, SP-3 |
| **SP-5** | Thin offline evaluation via **model-free scoring** (IV/WoE); the **staged verification stamp** (Design → Data → Usefulness-checked); **Human Gate #2** with augmented review; final approval; immutable registry entry; batch materialization to the store | SP-4 |

**◆ Milestone (end of SP-5):** a data scientist takes one feature type from free text → clarified → grounded → validated → DSL-compiled → sandboxed → evaluated → approved → registered → materialized. The architecture is proven end-to-end.

### Phase C — Coverage (parallelizable after the milestone)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-6** | Path 2 (LLM candidate SQL); self-critique repair loop (non-gate); SQL validation gate for arbitrary SQL | SP-4, SP-5 |
| **SP-7** | Remaining validation packs (backfill, cost, serving-feasibility, duplication incl. rejected/in-flight); **semantic/proxy leakage detection + fairness gate**; the **search-overfitting guard** (out-of-time re-check) at evaluation | SP-4, SP-5 |
| **SP-8** | Critique Service as one reusable service across all five modes (CONTRACT / MAPPING / LOGIC / CODE / EVALUATION) | SP-2, SP-3, SP-4 |
| **SP-12** | Hypothesis-driven **Feature Generation engine** (bounded, assistive): multi-candidate generation, attempt memory + diversity (islands), model-free scoring, fast resource-cap discard; **proposes only — never bypasses gates** (design §14) | SP-4, SP-5, SP-7 |

### Phase D — Hardening (bank-grade & durable)

| ID | Delivers | Depends on |
|---|---|---|
| **SP-9** | Model-risk/model-inventory hooks; explainability as a registered artifact; four-eyes / segregation-of-duties enforcement; data-snapshot reproducibility | SP-5, cross-cutting threads |
| **SP-10** | Feature Lifecycle Manager; drift / PSI monitoring; Change-Impact Analyzer; revalidation / deprecation; **Path-2 → Path-1 harvest loop** | SP-5, SP-6 |
| **SP-11** | Path 3 (human-authored SQL with friction); RBAC; purpose-based access; policy-aware exposure; row/column-level enforcement | SP-4, SP-9 |

## 5. Sequencing rationale

- **Retire the riskiest unknowns first.** Point-in-time correctness and DSL viability are the two things most likely to invalidate the whole concept; Phase B exercises both immediately on the salary example.
- **The milestone is a real walking skeleton.** After SP-5 the spine is proven; everything afterward *broadens* and *hardens* a working system rather than betting on an unproven one.
- **Two cross-cutting threads cannot be deferred.** Audit logging and identity/SoD must be designed into SP-0 (even minimally) and completed in SP-9 — retrofitting governance at the end is a known failure mode.
- **Phase C and most of Phase D parallelize** once the spine exists; SP-6 / SP-7 / SP-8 can run concurrently across teams.

## 6. Per-sub-project process

Each sub-project is taken individually through:

```
brainstorm (spec)  →  writing-plans (implementation plan)  →  implementation
```

The reference architecture (companion doc) is the shared contract every sub-project spec refers back to.

## 7. Recommended starting point

**SP-0 (Foundations)** — it pins down the Feature Contract data model, the state machine, and the durable workflow runtime that every later spec references. **SP-1 (Metadata Overlay)** is a strong parallel alternative if a self-contained service is preferred as the first build.
