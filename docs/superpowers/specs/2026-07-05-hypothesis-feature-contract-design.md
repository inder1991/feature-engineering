# Hypothesis-Driven Feature Contract — final design
*(reconciles SP-2 intake + the assisted-definition addendum + the feature loop)*

Date: 2026-07-05. Status: BUILT (Phases 1-5 merged) + review-fixed. Supersedes, for the go-forward
product: `docs/architecture/2026-07-02-assisted-definition-design.md` (designed, never built) and the
discovery half of the SP-2 intake layer (`intake/*`, dormant + unwired). Builds on the merged
[feature loop](2026-07-05-feature-engineering-loop-design.md).

**PERSISTENCE DECISION (2026-07-05, M6): RELATIONAL, not event-sourced.** The flow persists to plain
Postgres tables (`contract_intent`, `contract_gate1_choice`, `contract`) + records every LLM call in
`llm_call`. It does NOT emit SP-2 `feature_contract` lifecycle events / reuse `intake/{events,state}`. The
audit a bank needs — who confirmed, the considered set they saw + why, every AI call — is captured
relationally; event-replay parity was judged not worth the complexity. All "event-sourced / reuse
intake/events" language below is superseded by this decision.

## The decision (why this exists)
Three overlapping attempts at the *same idea* — hypothesis-driven, LLM-assisted, human-confirmed feature
definition — now coexist:
- **SP-2 intake** (`intake/*`): built, has unwired-collaborator bugs (left unfixed — the pivot made them
  moot), **dormant**.
- **Assisted-definition addendum** (2026-07-02): mandatory hypothesis + generated scored alternatives +
  advisory critique at Human Gate #1 — **designed, never built**; its "quality half" waited on the
  *SP-12 feature-generation engine*.
- **The feature loop** (built this session): generate-validate-refine + multi-set advisory recommendation
  grounded in a hypothesis — **which IS that SP-12 engine, and already realizes the discovery half of
  both above, in the upload-catalog world.**

**Decision: retire the redundant machinery; SALVAGE the still-valuable governance half onto the loop.**
The loop wins the discovery half; we keep only what it doesn't do — formal hypothesis intake, text
redaction, the Human Gate #1 considered-set audit, and a versioned, event-sourced, drift-linked feature
**contract**.

## Retire / Salvage / Already-built
| Piece | Disposition |
|-------|-------------|
| SP-2 intake *discovery* (candidates, scoring, mcv-as-discovery, doubt_router, critique-as-intake) | **retire** — the loop replaces it |
| Assisted-definition's governed-DB flow assumptions | **retire** — wrong world (no DB, no ownership) |
| SP-2 unwired-collaborator bugs | **deleted with the code** (not fixed) |
| Feature discovery (which features, cross-domain, multi-set, advisory) | **already built** (the loop) |
| Deterministic gauntlet (leakage/freshness/additivity/point-in-time) | **already built** (= the MCV) |
| Mandatory-hypothesis intake + optional anchor definition | **salvage** (build) |
| Text redaction/egress on hypothesis + definition | **salvage** — reuse SP-2 redactor/`assert_llm_safe` |
| Human Gate #1 considered-set audit (anchor + alternatives + choice + who + why) | **salvage** (build) |
| Governed, versioned, **relational** feature **contract** (draft→critique→refine→confirm) | **built** — plain tables (`contract`, `contract_gate1_choice`, `contract_intent`); audit via `llm_call` (M6, not event-sourced) |
| Drift-linked contract freshness/impact | **salvage** — reuse `feature_freshness` / `features_affected_by` |

## Authority model (the spine — unchanged from SP-2 & the loop)
**LLM suggests/critiques → platform validates (deterministic gauntlet) → human confirms → registry
governs.** The AI never decides, never auto-approves, never silently swaps the anchor. **No PII/raw data
reaches the LLM** (metadata-only + the redactor/egress guard on any free text). Every LLM call audited.

## The reconciled lifecycle (end to end)
```
DATA SCIENTIST
  Hypothesis H   ← MANDATORY (the "why")
  Definition D   ← optional  (the "what" — the anchor, if given)
        │ submit_intent   (no H → command-validation DENIAL, resubmit — not a terminal reject)
        ▼
REDACT + CLASSIFY H and D (text)   — reuse SP-2 redactor + assert_llm_safe; intake_mode fixed & immutable
        ▼
DISCOVER (the loop)   — generate-validate-refine, entity-anchored cross-domain, over the catalog;
        │               produces the anchor (from D) + SCORED ALTERNATIVES (from H) + advisory critique.
        │               Every candidate passes the deterministic gauntlet (leak-free/fresh/additivity/PIT).
        ▼
HUMAN GATE #1   — requester sees the anchor (pre-selected) + scored alternatives + advisory notes,
        │           confirms exactly ONE; the CONSIDERED SET + choice + who + (conditionally) why recorded.
        ▼
AUTHOR CONTRACT (LLM)   — DRAFT_CONTRACT_PRODUCED: author the full contract from the chosen option +
        │                  catalog metadata (definition/doc, I/O, grain, as-of, join path, aggregation,
        │                  unit, lineage narrative, assumptions).
        ▼
CRITIQUE → REFINE (LLM loop)   — CONTRACT_CRITIQUED (adversarial) → CONTRACT_REFINED; bounded, code-owned.
        │                          The deterministic MCV runs INSIDE each pass; its failures feed refine
        │                          alongside the LLM critique (symmetric with the feature loop).
        ▼
MCV (code)   — MINIMUM_CONTRACT_VALIDATED = the deterministic gauntlet. No LLM. Also the final gate.
        ▼
CONTRACT_CONFIRMED (human)   — the governing write. Registered, versioned.
        ▼
GOVERN   — drift-linked: contract freshness = its derives-from sources' watermarks; catalog drift stales
           the contract (REVERIFY); the read path fails closed. A feature that KNOWS when its inputs drifted.
```

## The LLM at each node (advisory, audited, human-gated)
| Node | LLM role | grounded in | gate |
|------|----------|-------------|------|
| discover / alternatives | propose the anchor + scored alternatives | catalog graph + H (redacted) | gauntlet |
| advisory critique | note risks on the options | the options + metadata + target | advisory only (cannot block) |
| Gate #1 | none | — | **human confirms one** |
| draft | author the contract | chosen option + column/table defs + grain/as-of/additivity/entity | — |
| critique | adversarial review of the draft | the draft + metadata + target | — |
| refine | fix findings | the critique | re-critique |
| MCV | **none — deterministic** | the gauntlet | reject if unsafe |
| confirm | none | — | **human** |

## No-DB limits + honest scope
- Cross-catalog joins in a contract are **declared/entity-resolved, not value-verified** → the human
  confirms them at Gate #1 / CONTRACT_CONFIRMED.
- **"Which alternative/contract performs best" is a backtest** (downstream), not an LLM claim — the LLM
  authors and critiques the *definition* and scores *fit vs the hypothesis*; it never predicts performance.
- **Human-gated throughout**; LLM advisory at every node; every call audited via the seam.

## Reuses (already built)
The feature loop + the deterministic gauntlet (= MCV), the entity layer + cross-catalog path, the audited
LLM seam (`enrich_llm.audited_structured_call` / `audited_enrich_call`), `feature_freshness` /
`features_affected_by`, and — from SP-2, re-grounded — the **redactor + egress guard**
(`intake/redaction.py`). **NOT** the SP-2 `intake/{events,state,contract}` aggregate — persistence is
relational (`contract_intent`, `contract_gate1_choice`, `contract`) per the M6 decision.

## Open follow-ups (post-review)
- **Join path in the contract** (spec's AUTHOR node): DEFERRED — bundled with the **full B3 refactor**
  (carry `(catalog_source, object_ref)` through `FeatureIdea.derives_from`), since authoring a
  cross-catalog join path needs the same catalog_source threading. Until then the contract carries
  grain/as-of/aggregation/derives-from + the definition narrative; the join path is confirmed at Gate #1
  but not persisted on the contract.
- Other reduced `ContractDraft` fields (io_schema, unit, assumptions) — incremental, low priority.

## Build plan
See `docs/superpowers/plans/2026-07-05-hypothesis-feature-contract-plan.md` — phased:
1. **Intake** — `submit_intent` (mandatory H + optional D) + text redaction/classification.
2. **Gate #1 bridge** — drive the loop from H (+ anchor from D) → considered-set + advisory; record the
   choice (anchor/alternatives/who/why).
3. **Contract authoring** — DRAFT_CONTRACT_PRODUCED via the audited seam, catalog-grounded.
4. **Critique→refine loop + MCV** — bounded LLM critique/refine; MCV = the gauntlet.
5. **Confirm + govern** — CONTRACT_CONFIRMED (human), versioned, drift-linked.
6. **Retire** — delete the superseded SP-2 intake discovery modules + the old assisted-definition assumptions.
