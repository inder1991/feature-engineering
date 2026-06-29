# Contract-Driven Feature Engineering Platform

A **vendor-neutral reference architecture** for a banking-grade feature engineering platform that turns a data scientist's natural-language intent into a **versioned, monitored, governed production feature** — safely.

> **Status:** Design / reference architecture. This repository currently contains the architecture specification and build roadmap. No implementation yet.

---

## The idea in one line

> **The LLM suggests and structures. The platform validates and enforces. The human confirms business meaning. The registry governs the production lifecycle.**

No single actor is trusted to do another's job. That separation is the source of the platform's safety.

The platform is explicitly **not** `free text → LLM → SQL → feature store` (unsafe). Instead, every feature flows through confirmed contracts, deterministic gates, sandbox proof, evaluation, and human approval before it can reach production.

## What it does

A data scientist provides intent in one of two modes:

- **Hypothesis-driven** — *"customers with irregular salary credits are more likely to churn."* The platform **generates** several candidate feature definitions, scores them, and presents ranked options.
- **Definition-driven** — *"stddev of days between SALARY transactions over 90 days, per customer × as_of_date."* The platform **translates** it faithfully.

Both converge on the same safety floor:

```
Intent
  → Draft contract            (LLM structures it; not executable)
  → Confirmed contract        (Human Gate #1 resolves ambiguity)
  → Mapped contract           (grounded to real, allowed data via catalog + overlay)
  → Validated plan + path      (deterministic gates; Implementation Router picks the drafter)
  → Sandbox-proven artifact    (runs on masked/sampled data; data-quality checks)
  → Evaluated feature          (model-free scoring; leakage, fairness, overfitting guard)
  → Approved feature           (Human Gate #2, four-eyes / segregation of duties)
  → Registered + monitored     (immutable version; batch-materialized; lifecycle-managed)
```

## Architecture at a glance — seven layers

| Layer | Responsibility |
|---|---|
| **0 — Metadata foundation** | Existing catalog **+ Metadata Overlay** (fills the ML-specific gaps: availability time, grain, SCD, joins, policy tags) |
| **1 — Intake** | Two intake modes; LLM normalizes intent into a draft contract |
| **2 — Contract control** | Human Gate #1; ambiguity scoring; doubt router; confirmed contract |
| **3 — Grounding** | Policy-aware schema mapper; entity/grain; point-in-time + SCD mapping |
| **4 — Validation + routing** | Deterministic validation packs; Implementation Router (safest path first) |
| **5 — Compilation + sandbox** | Compiler / candidate SQL; sandbox on safe data; data-quality checks |
| **6 — Evaluation + risk** | Model-free scoring; semantic-leakage, fairness, overfitting guard |
| **7 — Approval + lifecycle** | Human approval; Registry (governance) ≠ Store (values); monitoring, drift, deprecation |

## Key design decisions

- **Three implementation paths, one safety floor.** A DSL compiler (low risk), LLM candidate SQL (medium), or human-authored SQL (controlled) may *draft* a feature — but all pass through the identical gates. The **Implementation Router** defaults to the safest path.
- **Metadata Overlay with a hard floor.** The platform augments the existing catalog with the ML facts it lacks; a feature cannot proceed if a required fact exists nowhere. Data facts are confirmed by the **data owner**, policy facts by **Compliance**.
- **Batch-only serving.** Features are computed on a schedule and materialized; serving is a lookup. Sub-second real-time is out of scope for this revision.
- **Leakage is two problems.** *Temporal* leakage is a deterministic hard gate; *semantic/proxy* leakage is an evaluation-plus-human concern. "PASSED" never means "leakage-free."
- **Model-free scoring.** Candidates are ranked with Information Value / Weight of Evidence (no trained model required), with an out-of-time **search-overfitting guard** to reject lucky winners.
- **Staged verification stamp.** Every feature is honestly marked `Design-checked → Data-checked → Usefulness-checked`; production promotion requires **Usefulness-checked**.
- **Full, framework-aware governance.** Model-risk hooks, explainability as a registered artifact, mechanically-enforced segregation of duties, and audit + data-snapshot reproducibility.

## Repository contents

```
docs/architecture/
├── 2026-06-27-feature-engineering-platform-design.md     # the reference architecture (15 sections)
├── 2026-06-27-feature-engineering-platform-roadmap.md    # build decomposition (SP-0 … SP-12, 4 phases)
└── 2026-06-27-sp0-foundations-design.md                  # SP-0 sub-project spec (the backbone)
```

- **[Reference architecture →](docs/architecture/2026-06-27-feature-engineering-platform-design.md)** — full design: layers, contract schemas, validation packs, state machine, governance, generation & scoring.
- **[Build roadmap →](docs/architecture/2026-06-27-feature-engineering-platform-roadmap.md)** — the platform split into independently-buildable sub-projects.
- **[SP-0 Foundations spec →](docs/architecture/2026-06-27-sp0-foundations-design.md)** — the backbone: feature aggregate, event-sourced store, immutable document chain, state machine, durable runtime, identity/SoD.

## Build roadmap (summary)

Vertical-slice-first: lay a thin backbone, prove one feature type end-to-end, then thicken.

| Phase | Focus | Sub-projects |
|---|---|---|
| **A — Foundations** | Contract + state machine + workflow runtime; Metadata Overlay | SP-0, SP-1 |
| **B — Vertical slice / MVP** | One feature type, Path 1, end-to-end happy path | SP-2 … SP-5 |
| **C — Coverage** | LLM-SQL path, full validation, critique service, generation engine | SP-6, SP-7, SP-8, SP-12 |
| **D — Hardening** | Governance, lifecycle/monitoring, human-SQL path + security | SP-9, SP-10, SP-11 |

Each sub-project gets its own `brainstorm (spec) → plan → implementation` cycle. Recommended starting point: **SP-0 (Foundations)**.

## Status & next steps

This is a design-stage repository. **SP-0 (Foundations)** now has its own [design spec](docs/architecture/2026-06-27-sp0-foundations-design.md); the next step is its implementation plan, or brainstorming **SP-1 (Metadata Overlay)** in parallel.
