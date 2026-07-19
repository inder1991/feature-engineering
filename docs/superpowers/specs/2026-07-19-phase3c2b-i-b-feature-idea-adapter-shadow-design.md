# Phase 3C.2b-i-B — FeatureIdea Adapter (Shadow) — Design

**Status:** approved for planning; **implementation gated on A landing** (A's compiler + plan-carrier interfaces + exact-outcome gold gate) and on the concept + structural authority-provisioning dependency
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Depends on:** [3C.2b-i-A](2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md) (`MultiSourcePlannerIntentV1`, the proven assembly planner)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — rebase onto `origin/main` (`d90d457`+) before implementation
**Migration:** `1006` (re-confirm free at build time)

## 1. Purpose

Own the **untrusted → authoritative** conversion: produce an already-authoritative `MultiSourcePlannerIntentV1` for A — or reject fail-closed. Shadow-only, off the request path via a worker.

```
raw LLM proposal (pre-_vet) + request-time candidate identity map
  -> capture safe raw candidate + identity map + authority-state fingerprint + observation origin
  == worker ==
  -> classify catalog span (single | cross | unresolved | ambiguous)
  -> per-operand concept authority (raw evidence)
  -> deterministic roles (versioned policy over group/pit_role/additivity, pit_role-gated TIME)
  -> governed grain + governed time anchor + source-side structural bindings
  -> per-operand path_strategy + final_expression (slot-referenced)
  -> MultiSourcePlannerIntentV1 -> A planner (shadow) -> record outcomes
```

## 2. Capture is request-time; classification is worker-time (findings #6, #7, #8)

Raw LLM proposals carry **bare `derives_from` strings** — `FeatureIdea` gains `(catalog_source, object_ref)` only during recommend/validation (`feature_assist.py:383`) and the LLM `_menu` omits `catalog_source` (`feature_assist.py:166`). So "distinct raw-operand catalog_source" **cannot** be computed at capture. At request time B captures, transactionally with the request write:

- The **safe raw candidate** (pre-`_vet`; §8) — bare derives_from preserved.
- The **request-time candidate identity map** (the `src_of`/menu mapping resolving each bare name → candidate catalog source(s)).
- An **authority-state fingerprint** (finding #8): not just catalog stamps — the relevant **concept evidence set, overlay checkpoint, VERIFIED bridge set, and structural/grain fact ids**, since those change without a catalog rebuild.
- The observation origin (§8) + an outbox message (§7).

`_vet` silently keeps only known operands (`derives = [d for d in ... if d in known]`, `feature_assist.py:474`), so the worker compares raw vs vetted and rejects `PROPOSAL_LOSSY` on any drop/rewrite. The worker then **classifies catalog span** from the identity map: single-catalog / **cross-catalog** (>1 distinct resolved source) / `UNRESOLVED_OPERAND` / `AMBIGUOUS_COLUMN_IDENTITY` (a bare name resolving to >1 catalog). Only cross-catalog proceeds. Before evaluating, the worker **re-checks the authority-state fingerprint**; any mismatch → `AUTHORITY_STATE_DRIFTED` (**capture-incomplete**, never evaluate against newer authority).

## 3. Concept authority (the seam)

`concept` is RECOMMENDATION-capped: `resolve_field_authority` short-circuits non-OPERATIONAL fields (`field_authority.py:255`), so `concept.load_bearing_value_hash` is **always NULL** (locked by `test_field_resolution.py`). B resolves its own planner authority over raw evidence:
```
resolve_planner_concept_binding(conn, logical_ref)
    -> PlannerConceptBinding(authoritative_concept, authority, evidence_ids, evidence_set_hash, value_hash) | Rejection
```
No `expected_concept`. (1) Active evidence via `read_active_field_evidence`, considering **only** `(SOURCE,ATTESTED)`/`(HUMAN,CONFIRMED)`. (2) **Conflict detected by the resolver itself**, not `active_disqualifiers_for` (that returns only `{CONFIRMATION_PENDING_REVALIDATION}`, `field_revalidation.py`; applied for the pending block only). (3) Precedence: one agreed human → `HUMAN_CONFIRMED`; conflicting human → `CONCEPT_AUTHORITY_CONFLICT`; no human + one agreed source → `SOURCE_ATTESTED`; conflicting source → `CONCEPT_AUTHORITY_CONFLICT`; pending disqualifier → `CONCEPT_REVALIDATION_PENDING`; lower-authority disagreement → diagnostic. (4) **Missing/stale/rejected** via `field_evidence` history over all `EvidenceLifecycle`, **only the two accepted pairs**: none in any lifecycle → `CONCEPT_AUTHORITY_MISSING`; exist but none `ACTIVE` (a `STALE`/`SUPERSEDED`) → `CONCEPT_EVIDENCE_STALE`; only `REJECTED` → `CONCEPT_AUTHORITY_MISSING`. (5) Require `∈ CONCEPT_REGISTRY` (`CONCEPT_NOT_IN_REGISTRY`); compare to `graph_node.concept` only for a `DISPLAY_CONCEPT_MISMATCH` diagnostic. (6) Read/DB failure → technical outcome.

### 3.1 Authority-provisioning dependency (findings #5, #9 — tightened)

No production writer attests `concept` today (`LLM/PROPOSED` from `enrich.py:310`; `d90d457` source profiles attest `unit/currency/entity/data_type/...` but **not** `concept`, `source_profile.py`; no human writer). Provisioning rules: a source profile may attest `concept` **only** when the source **explicitly supplies a canonical concept under a governed capability contract** — an LLM-derived mapping must **never** be relabeled `SOURCE/ATTESTED`. And provisioning must cover **structural authority too** (grain/time/key): FTR has no structural fields, so concept alone still leaves grain/time/key mappings unavailable, and B would reject on structural grounds. The dependency therefore covers **both** explicit concept authority **and** structural-authority provisioning, and B's gate requires a **non-vacuous** population for each activatable cohort (§12).

## 4. Deterministic role assignment (findings #9, #10)

`Concept` carries `group/additivity/pit_role/entity_link/...` (`concepts.py`), not "measure/countable" flags. B defines a **versioned `COMPUTATION_ROLE_POLICY`** (`ROLE_POLICY_VERSION`) that is **total over every concept `group`** (enumerated, no `{...}`):
- `monetary`, `quantity_risk`, `accounting`, `regulatory_capital`, `esg`, `crypto` → `MEASURE`-eligible.
- `temporal` → `TIME` **only if it has an accepted `pit_role`** (`as_of|effective|event|maturity|valid_time|system_time`); a temporal-group concept with `pit_role = none` (e.g. `duration_tenure`, `vintage`) is **not** a time anchor — treat as a numeric `MEASURE`/reject per shape, never TIME (finding #10).
- `identifier` (with `entity_link`) → `COUNTED`.
- every other group (`categorical`, `geographic`, `flag`, `sensitive`, `text`, `label`, `behavioural`, `network`, `bitemporal`, `currency`, `eligibility`) → explicit mapping or unmappable → reject.

Match derived roles to the operation shape (A §3). Numeric `path_strategy` sets `external_type_required = true` when the operand's operational type is unknown (finding #10; ties to §9 passthrough). **Order-sensitive ops deferred**: `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING`; ordering never from `derives_pairs`/`measure_refs` order, name, description, rationale.

## 5. Operation grammar + governed time anchor (finding #10)

Exact alias grammar → canonical op + typed `window` (no free-text `trend_90d`; `feature_assist.py:44`); unknown/compound → `OPERATION_UNRECOGNIZED`; windowed op without typed `window` → `WINDOW_REQUIRED_UNSPECIFIED`. Time anchoring does **not** use display `is_as_of` `LIMIT 1` (`feature_assist.py:452`); B **independently resolves a governed time anchor** (accepted `pit_role` + governed structural authority) → else `TIME_ANCHOR_UNGOVERNED`. The final expression references the time slot by `time_slot_id` (A §2.1).

## 6. Governed grain + source-side structural bindings (findings #2, #4)

Grain authority (missing/conflicting → `GRAIN_UNRESOLVED`): confirmed scope `target_entity` → governed source-qualified grain candidates → LLM `grain_table` **consistency check only**. B emits the logical `target_entity`; **A selects the physical landing** — so B provides **source-side** `GovernedSourceBindingV1` per operand (authoritative `source_grain_entity` + `source_key_ref` + provenance), **not** a source→landing key mapping (B can't know the landing). Structural facts come from governed grain/key facts, never `graph_node.concept`/`is_grain`. Ungoverned → `STRUCTURAL_NEED_UNGOVERNED`.

## 7. Worker topology + orchestration (findings #11, #12)

**Worker, off the request path.** Request time inserts only the observation + outbox message (§2) in the request transaction — no planning/compile/LLM — so latency is not customer-visible; `alternatives`/`rejections`/`anchor` untouched. Hook predicate is **is_live-independent**: confirmed scoped run ∧ `catalog_source is None` ∧ non-null confirmed `target_entity` ∧ `FEATUREGEN_LLM_XCAT_SHADOW`. Population = cross-catalog **alternatives + the definition anchor**, source-kind tagged, captured **after all alternatives and the anchor are produced** so the run **manifest is finalized once** with the complete expected set (finding #12). `ConfirmedScope` (distinct from planner `CatalogScopeV1`) + `generation_run_id` are threaded from `_scoped_considered_set` (`contract.py:394`); the **unscoped** route (`contract.py:501`) is **excluded** (no run provenance). The existing outbox (`runtime/outbox.py:78`) gives storage/relay only — B defines the **topic, route, handler**, a **deterministic outbox message identity** (over run + idea-observation id), and **retry handling** (idempotent replay against the append-only store). The worker classifies pre-evaluation authority drift (§2).

## 8. Canonical input + provenance (replay)

`audited_structured_call` returns only the output dict (`enrich_llm.py:611`); `_record_llm_call_durable` discards the call ref (`enrich_llm.py:380`); `FeatureIdea` carries no call/round/candidate index. B requires the **generation seam to thread an observation origin** `{llm_call_ref, lens, round, candidate_index}` and mints a **stable idea-observation id**. Replay uses a **persisted safe canonical raw input** (redacted, structured; captured pre-`_vet`) plus the identity map + authority-state fingerprint — hashes alone cannot replay.

## 9. Slice-3 validation passthrough (finding #13)

`_vet` returns `validation_status` + typed `requirements` (`feature_assist.py:592`), **orthogonal** to governance (a governed plan may still need external type/grain/unit/currency validation — and B's `external_type_required` operands feed this). B persists both and carries them to live serialization in 3C.2b-ii; never collapses the axes.

## 10. Telemetry + store (migration 1006)

No `synthetic_template_id`/"planner role". **Cohort assigned before any later-stage rejection** (denominators): `RESOLVED_HUMAN_CONFIRMED` (all operands human) vs `RESOLVED_INCLUDES_SOURCE_ATTESTED` (≥1 source). **Separate axes**: semantic outcome / compile completeness / technical status / capture status (`BUDGET_TRUNCATED`, `AUTHORITY_STATE_DRIFTED` = capture-incomplete). Store mirrors `0999` (manifest + expected set + two-phase + reconciliation; append-only; idempotent `(run_id, idea_observation_id)`; role/scope fingerprints + authority-state fingerprint + drift class). Persist per idea: observation id + origin; source kind; raw→vetted transformation summary + span classification; `adapter_input_hash` + `normalized_intent_hash` + canonical-raw-input ref + identity-map ref + authority fingerprint; per-operand role + pin + authority class + evidence ids/set hash + disqualifiers + source_binding provenance; `path_strategy` per slot (incl `external_type_required`) + `final_expression`; Slice-3 `validation_status` + `requirements`; versions (adapter, role-policy, operation-policy, concept-registry, planner, compiler); A's `MultiSourcePlanningResultV1` selected id + verdict; authoritative + diagnostic outcomes **separately**; reason codes. No unredacted free-form text.

## 11. Dispositions

**Resolve:** raw captured losslessly → cross-catalog → authority present → normalizes → A resolves → preserved. **Semantic rejects:** `PROPOSAL_LOSSY`, `UNRESOLVED_OPERAND`, `AMBIGUOUS_COLUMN_IDENTITY`, `CONCEPT_AUTHORITY_MISSING`, `CONCEPT_AUTHORITY_CONFLICT`, `CONCEPT_EVIDENCE_STALE`, `CONCEPT_REVALIDATION_PENDING`, `CONCEPT_NOT_IN_REGISTRY`, `OPERATION_UNRECOGNIZED`, `WINDOW_REQUIRED_UNSPECIFIED`, `OPERAND_SHAPE_INVALID`, `OPERAND_ORDER_AUTHORITY_MISSING`, `GRAIN_UNRESOLVED`, `TIME_ANCHOR_UNGOVERNED`, `STRUCTURAL_NEED_UNGOVERNED`, + A's rejects surfaced through. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`, `AUTHORITY_STATE_DRIFTED`. **Diagnostics:** `DISPLAY_CONCEPT_MISMATCH`, lower-authority disagreement, heuristic `diagnostic_candidate`.

## 12. Gate (partitioned — finding #11) + non-vacuous authority

**Correctness gold** (immutable expected outcomes; **positive cases MUST resolve** with the exact expected intent and, end-to-end through A, the exact expected plan) vs **fault-observability controls** (injected DB error, budget truncation, authority drift — pass when exactly classified; excluded from the clean population). Gate over the clean population: zero false resolves; 100% operand + operation preservation; deterministic replay from the persisted canonical raw input + identity map; no unexplained rejection category; no technical failures. Additionally: a **non-vacuous** attested/confirmed population per activatable cohort, and the **minimum distinct authoritative shapes** per cohort, measured against **trusted labels** (the gold's known-correct normalization) so real false-normalization is quantifiable. Resolution rate on real traffic is descriptive; **do not gate on it.**

## 13. Gold set (must include)

**Correctness — positive (must resolve):** single-measure and `TREND` cross-catalog ideas with **real attested/confirmed** concept + governed structural bindings; a composite-grain landing. **Correctness — negative (exact code):** `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING`; lossy proposal → `PROPOSAL_LOSSY`; a bare name resolving to two catalogs → `AMBIGUOUS_COLUMN_IDENTITY`; unresolved operand; unregistered concept; conflicting human evidence; source-only vs human (cohort split); stale-only / rejected-only (distinct); display≠authoritative (diagnostic, pin correct); grain contradiction; `duration_tenure`-as-TIME attempt (must not classify TIME); ungoverned time anchor; unknown/compound op; missing window; ungoverned structural need; pin-bypass. **Fault controls (separate partition):** injected DB error; budget-truncated run; authority-state drift between capture and worker → `AUTHORITY_STATE_DRIFTED`.

## 14. Reused surfaces

`read_active_field_evidence`/`to_view`/`active_disqualifiers_for` + all-lifecycle history read; `EvidenceProducer`/`AssertionStrength`/`EvidenceLifecycle`; `CONCEPT_REGISTRY` + `Concept` fields; the A planner + `MultiSourcePlannerIntentV1`/`MultiSourcePlanningResultV1`; `gate1.build_considered_set` (request-time raw + identity-map + fingerprint capture; shadow-context threading); the generation seam (`recommend_features`/`recommend_feature_sets_report` + `enrich_llm`) extended for observation origin; `runtime/outbox.py` (new topic/route/handler + deterministic message id + retry); the `0999`/`shadow_store.py` manifest+reconciliation pattern.
