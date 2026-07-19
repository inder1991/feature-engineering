# Phase 3C.2b-i-B — FeatureIdea Adapter (Shadow) — Design

**Status:** approved for planning; **implementation gated on A landing** (A's compiler + plan-carrier interfaces + A's exact-outcome gold gate) and on the concept-authority provisioning dependency
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Depends on:** [3C.2b-i-A](2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md) (`MultiSourcePlannerIntentV1`, the proven assembly planner)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — rebase onto `origin/main` (`d90d457`+) before implementation
**Migration:** `1006` (re-confirm free at build time)

## 1. Purpose

Own the **untrusted → authoritative** conversion: take an LLM-proposed cross-catalog feature and deterministically produce an already-authoritative `MultiSourcePlannerIntentV1` for A — or reject fail-closed. B does no assembly; A does no authority resolution. Shadow-only, off the request path via a worker.

```
raw LLM proposal (pre-_vet)
  -> capture safe raw candidate + observation origin
  -> per-operand concept authority (raw evidence)
  -> deterministic computation roles (versioned policy over group/pit_role/additivity)
  -> governed grain + governed time anchor
  -> per-operand path_strategy + final_expression + governed structural bindings
  -> MultiSourcePlannerIntentV1  -> A planner (shadow)  -> record outcomes
```

## 2. Capture the raw proposal before the gauntlet (finding #6)

`_vet` keeps only known operands — `derives = [d for d in raw.get("derives_from", []) if d in known]` (`feature_assist.py:474`) — so a proposal with one valid + one invalid operand silently becomes a valid **one-operand** idea; the post-`_vet` `FeatureIdea` cannot prove what the LLM proposed. B captures the **safe raw candidate before `_vet`**, preserves the raw→vetted transformation, and **rejects normalization** (`PROPOSAL_LOSSY`) when any operand was dropped or rewritten. All downstream authority/role work runs on the raw operand set, not the vetted one.

## 3. Concept authority (the seam)

`concept` is a RECOMMENDATION field: `resolve_field_authority` short-circuits non-OPERATIONAL fields (`field_authority.py:255`), so `concept.load_bearing_value_hash` is **always NULL** (locked by `test_field_resolution.py`). B resolves its **own** planner authority over the raw evidence:

```
resolve_planner_concept_binding(conn, logical_ref)
    -> PlannerConceptBinding(authoritative_concept, authority, evidence_ids, evidence_set_hash, value_hash)
    | ConceptAuthorityRejection
```

No `expected_concept` — resolve first, compare after. Behaviour:

1. Active evidence: `read_active_field_evidence(conn, logical_ref, "concept")` via `to_view`; consider **only** `(SOURCE,ATTESTED)` and `(HUMAN,CONFIRMED)`.
2. **Conflict is detected by the resolver itself**, not `active_disqualifiers_for` — that returns **only** `{CONFIRMATION_PENDING_REVALIDATION}` (`field_revalidation.py`). It is applied for the pending block only.
3. Precedence:

   | Active accepted evidence | Outcome |
   |---|---|
   | one agreed human-confirmed | `HUMAN_CONFIRMED` |
   | conflicting human-confirmed | `CONCEPT_AUTHORITY_CONFLICT` |
   | no human, one agreed source-attested | `SOURCE_ATTESTED` |
   | conflicting source-attested | `CONCEPT_AUTHORITY_CONFLICT` |
   | pending-revalidation disqualifier active | `CONCEPT_REVALIDATION_PENDING` |
   | lower-authority disagreement with winner | diagnostic; higher authority wins |
4. **Missing vs stale vs rejected** — read `field_evidence` history across all `EvidenceLifecycle` (`ACTIVE|STALE|REJECTED|SUPERSEDED`), considering **only the two accepted producer-strength pairs**: none in any lifecycle → `CONCEPT_AUTHORITY_MISSING`; exist but none `ACTIVE` with a `STALE`/`SUPERSEDED` → `CONCEPT_EVIDENCE_STALE`; only `REJECTED` → `CONCEPT_AUTHORITY_MISSING`.
5. Require `authoritative_concept ∈ CONCEPT_REGISTRY` (`CONCEPT_NOT_IN_REGISTRY`); compare to `graph_node.concept` only for a `DISPLAY_CONCEPT_MISMATCH` **diagnostic**.
6. Read/DB failure → raise → per-idea technical outcome.

### 3.1 Authority-provisioning dependency (finding #5)

No production writer attests `concept` today: enrichment writes `LLM/PROPOSED` (`enrich.py:310`); the `d90d457` source-capability profiles attest `unit/currency/entity/data_type/...` but **not** `concept` (`source_profile.py`); no human concept-confirmation writer exists. Without provisioning, real B telemetry is uniformly `CONCEPT_AUTHORITY_MISSING`. B therefore declares an **explicit dependency**: before 3C.2b-ii, either extend the source-capability profile to attest `concept` for governed sources, or add a human concept-confirmation writer. B's gate requires a **non-vacuous** attested/confirmed population (§12).

## 4. Deterministic role assignment (finding #9)

The LLM gives unordered operands with no roles; Slice 3 marks every `derives_pair` a measure (`feature_assist.py:594`) — not trusted. `Concept` carries `group/additivity/pit_role/entity_link/...` (`concepts.py:25`) — **not** "measure/quantity/countable" flags. B defines a **versioned `COMPUTATION_ROLE_POLICY`** (`ROLE_POLICY_VERSION`) with **total** mappings over real fields:

- `group ∈ {monetary, quantity_risk, ...}` → `MEASURE`; `group = temporal` **or** `pit_role ≠ none` → `TIME`; `group = identifier` (with `entity_link`) → `COUNTED`; otherwise → **unmappable** (reject).
- Match the multiset of derived roles to the operation's required shape (A §3). Exact unambiguous match → assign; a required role unfilled, an operand unmappable, or two operands indistinguishable for an **order-sensitive** slot → reject.
- **Order-sensitive ops deferred**: `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING`. Ordering is **never** taken from `derives_pairs`/`measure_refs` order, name, description, or rationale. A already models ordered roles, so no planner migration when B later gains a governed operation-signature registry.

## 5. Operation grammar + governed time anchor (finding #10)

Current output carries free-text forms like `trend_90d` and free-text window detection (`feature_assist.py:44`). B defines an **exact alias grammar** → canonical operation + typed `window`; unknown/compound → `OPERATION_UNRECOGNIZED`; windowed op without typed `window` → `WINDOW_REQUIRED_UNSPECIFIED`. Time anchoring does **not** use the display `is_as_of` `LIMIT 1` selection (`feature_assist.py:452`); B **independently resolves a governed time anchor** (an authoritative temporal binding, same authority discipline as concept/structural) → else `TIME_ANCHOR_UNGOVERNED`. B emits per-operand `path_strategy` + the `FinalExpressionV1` (A §2).

## 6. Governed grain + structural bindings

Grain authority (missing/conflicting → `GRAIN_UNRESOLVED`): confirmed scope `target_entity` → governed **source-qualified** grain candidates → LLM `grain_table` as a **consistency check only** (bare table name never authoritative; contradiction rejects). B emits the logical `target_entity`; A selects the physical landing. **All load-bearing structural needs** (source grain, key mapping, time anchor) are resolved from **governed structural facts** and emitted as `GovernedStructuralBindingV1` for A (finding #4 — normal discovery would resolve grain/key from `graph_node.concept`). Only existing **VERIFIED bridge/realization** columns may remain planner-discovered (own authority). Ungoverned structural need → `STRUCTURAL_NEED_UNGOVERNED`.

## 7. Worker topology (finding #11 — chosen: worker, not inline)

B runs **off the request path** via a worker. At request time (inside `build_considered_set`, is_live-**independent** predicate: confirmed scoped run ∧ `catalog_source is None` ∧ non-null confirmed `target_entity` ∧ `FEATUREGEN_LLM_XCAT_SHADOW`), B **transactionally persists** (a) the safe raw observation (§2, §8) and (b) an **outbox message**, in the same transaction as the request write, and **freezes request-time scope/catalog stamps**. The worker later consumes the message, runs authority/role/normalization + A's assembly in shadow, and **classifies pre-evaluation drift** (catalog changed between capture and evaluation). The existing outbox (`runtime/outbox.py:78`) provides storage/relay primitives only — B defines the **topic, route, and handler**. The request path adds only the observation+outbox insert (no planning, no compile, no extra LLM call), so latency is not customer-visible; `alternatives`/`rejections`/`anchor` are never modified.

### 7.1 Population = alternatives AND anchor
The 3C.2a governed branch + `_reject_cross_catalog_llm` are under `elif is_live:` (`gate1.py:396`, skipped when off), and the definition anchor is generated later with its own cross-catalog rejection (`gate1.py:416`). B captures cross-catalog (>1 distinct raw-operand `catalog_source`) **alternatives + anchor** into **one source-kind-tagged observation batch**, is_live-independent, so anchors are not an untested governed path for 3C.2b-ii. `build_considered_set` lacks `generation_run_id`/`ConfirmedScope` (they live in `_scoped_considered_set`, `contract.py:394`); B threads an immutable shadow context `{ConfirmedScope, generation_run_id, roles, now}`. `ConfirmedScope` is **distinct** from planner `CatalogScopeV1`. The **unscoped** route (`contract.py:501`) has no run provenance → **excluded**.

## 8. Canonical input + LLM provenance (replay, finding #9-prov)

`audited_structured_call` returns only the output dict (`enrich_llm.py:611`); `_record_llm_call_durable` discards the call ref (`enrich_llm.py:380`); `FeatureIdea` carries no call/round/candidate index, so a "stable ordinal" is not stable across retries/critic/lenses. B requires the **generation seam to return/thread an observation origin** `{llm_call_ref, lens, round, candidate_index}` and mints a **stable idea-observation id**. For replay, B persists a **safe canonical raw input** (redacted, structured — never raw hypothesis/rationale) captured pre-`_vet`; **hashes alone cannot replay**.

## 9. Slice-3 validation passthrough (finding #13)

`_vet` returns `validation_status` + typed `requirements` (`feature_assist.py:592`). These are **orthogonal** to governance (a governed physical plan may still need external type/grain/unit/currency validation) and must **survive normalization**: B persists them on the observation and carries them through to the eventual live serialization in 3C.2b-ii. B never collapses the two axes.

## 10. Telemetry + store (migration 1006, finding #12)

Drop `synthetic_template_id` and "planner role" (A uses no synthetic template). **Cohort is assigned before any later-stage rejection** so cohorts provide denominators: `RESOLVED_HUMAN_CONFIRMED` (every operand human-confirmed) vs `RESOLVED_INCLUDES_SOURCE_ATTESTED` (≥1 source-attested); report per-cohort false-normalization + resolution rates (descriptive). Telemetry keeps **separate axes**: **semantic outcome** / **compile completeness** / **technical status** / **capture (bounded/truncated) status** — `BUDGET_TRUNCATED` is capture-incomplete, not technical.

Store mirrors `0999` manifest+reconciliation: run manifest + expected observation set + two-phase per-idea writes + reconciliation; append-only; idempotent `(run_id, idea_observation_id)`; role/scope fingerprints + pre-evaluation drift class; diagnostic-code arrays. Persist per idea: idea-observation id + origin `{llm_call_ref, lens, round, candidate_index}`; source kind (alternative|anchor); raw→vetted transformation summary; `adapter_input_hash` **and** `normalized_intent_hash` + the persisted canonical raw input ref; per-operand computation role + pin + authority class + evidence ids/evidence-set hash + disqualifiers + structural-binding provenance; `path_strategy` per slot + `final_expression`; Slice-3 `validation_status` + `requirements`; versions (adapter, role-policy, operation-policy, concept-registry, planner, compiler); the A `MultiSourceBindingPlanV1` id + contract verdict; authoritative + diagnostic outcomes in **separate** columns; reason codes. No unredacted free-form text.

## 11. Dispositions

**Resolve:** raw captured losslessly → normalizes → A resolves → operation + operands preserved. **Rejections (semantic):** `PROPOSAL_LOSSY`, `CONCEPT_AUTHORITY_MISSING`, `CONCEPT_AUTHORITY_CONFLICT`, `CONCEPT_EVIDENCE_STALE`, `CONCEPT_REVALIDATION_PENDING`, `CONCEPT_NOT_IN_REGISTRY`, `UNRESOLVABLE_COLUMN`, `OPERATION_UNRECOGNIZED`, `WINDOW_REQUIRED_UNSPECIFIED`, `OPERAND_SHAPE_INVALID`, `OPERAND_ORDER_AUTHORITY_MISSING`, `GRAIN_UNRESOLVED`, `TIME_ANCHOR_UNGOVERNED`, `STRUCTURAL_NEED_UNGOVERNED`, plus A's assembly rejections surfaced through. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`. **Diagnostics:** `DISPLAY_CONCEPT_MISMATCH`, lower-authority disagreement, heuristic `diagnostic_candidate`.

## 12. Normalization gate (exact-outcome gold + non-vacuous authority)

Each gold case has an **immutable expected outcome**; **positive cases MUST resolve** with the exact expected intent (operands, roles, `path_strategy`, `final_expression`, structural bindings) and, end-to-end through A, the exact expected plan — a reject-everything implementation fails. Then over the window: **zero false resolves**; **100% operand + operation preservation**; **deterministic replay from the persisted canonical raw input**; **no unexplained rejection category**; **no technical failures**; and a **non-vacuous** attested/confirmed population actually exercised (else the authority-provisioning dependency §3.1 is unmet and the gate cannot pass). Resolution rate on real traffic is descriptive; **do not gate on it.**

## 13. Gold set (must include)

**Positive (must resolve):** single-measure and `TREND` cross-catalog ideas with **real attested/confirmed** concept evidence + governed structural bindings. **Negative (exact code):** `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING`; a lossy proposal (valid+invalid operand) → `PROPOSAL_LOSSY`; unresolvable column; unregistered concept; conflicting human evidence; source-only vs human (cohort split); stale-only and rejected-only evidence (distinct); display ≠ authoritative (diagnostic, pin correct); grain contradiction; ungoverned time anchor; unknown/compound operation; missing window; ungoverned structural need; a display-concept-misleading pin-bypass case. **Technical/capture:** injected DB error (isolated technical, reconciliation intact); budget-truncated run.

## 14. Reused surfaces

`read_active_field_evidence`/`to_view`/`active_disqualifiers_for` + a new all-lifecycle history read; `EvidenceProducer`/`AssertionStrength`/`EvidenceLifecycle`; `CONCEPT_REGISTRY` + `Concept` fields (`overlay/upload/concepts.py`); the A planner + `MultiSourcePlannerIntentV1`; `gate1.build_considered_set` (capture hook + shadow-context threading, pre-`_vet` raw capture); the generation seam (`recommend_features`/`recommend_feature_sets_report` + `enrich_llm`) extended to thread observation origin; `runtime/outbox.py` (new topic/route/handler); the `0999`/`shadow_store.py` manifest+reconciliation pattern.
