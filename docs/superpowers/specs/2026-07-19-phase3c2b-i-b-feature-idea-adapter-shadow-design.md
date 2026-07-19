# Phase 3C.2b-i-B — FeatureIdea Adapter (Shadow) — Design

**Status:** approved for planning (implementation gated on A's assembly gate passing)
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Depends on:** [3C.2b-i-A](2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md) (`MultiSourcePlannerIntentV1`, the proven assembly planner)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow`
**Migration:** `1005` (re-confirm free at build time)

## 1. Purpose

Own the **untrusted → authoritative** conversion. Take an LLM-proposed cross-catalog `FeatureIdea` and deterministically produce an already-authoritative `MultiSourcePlannerIntentV1` for A's proven planner — or reject it fail-closed. B does no assembly; A does no authority resolution. B is **shadow-only**: it observes real LLM ideas, converts, hands to A in shadow, records the outcome, and never alters what users see.

```
FeatureIdea
  -> concept authority        (resolve_planner_concept_binding, per operand)
  -> deterministic roles       (semantic role from governed metadata; no guessing)
  -> governed grain            (confirmed scope target_entity; source-qualified candidates)
  -> OperationSpecV1           (closed vocabulary; declarations)
  -> MultiSourcePlannerIntentV1
  -> A planner (shadow)
  -> record authoritative + diagnostic outcomes
```

## 2. Concept authority (the seam)

On `origin/main`, `concept` is a RECOMMENDATION field: `resolve_field_authority` short-circuits non-OPERATIONAL fields (`field_authority.py:255`) so `concept.load_bearing_value_hash` is **always NULL** (locked by `test_field_resolution.py`). A verifier against it rejects every operand. B resolves its **own** planner authority over the raw evidence:

```
resolve_planner_concept_binding(conn, logical_ref)
    -> PlannerConceptBinding(authoritative_concept, authority, evidence_ids, evidence_set_hash, value_hash)
    | ConceptAuthorityRejection
```

No `expected_concept` input — resolve authority first, compare after. Behaviour:

1. Read lifecycle-**active** concept evidence (`read_active_field_evidence(conn, logical_ref, "concept")`, viewed via `to_view`). Consider **only** `(SOURCE, ATTESTED)` and `(HUMAN, CONFIRMED)`.
2. **Conflict is detected by the resolver itself**, not via `active_disqualifiers_for` — that function returns **only** `{CONFIRMATION_PENDING_REVALIDATION}` (`field_revalidation.py`), no conflict backing. `active_disqualifiers_for` is still applied for the pending-revalidation block; conflict comes from disagreeing accepted values.
3. Precedence ladder:

   | Active accepted evidence | Outcome |
   |---|---|
   | one agreed human-confirmed value | `HUMAN_CONFIRMED` |
   | conflicting human-confirmed values | reject `CONCEPT_AUTHORITY_CONFLICT` |
   | no human, one agreed source-attested | `SOURCE_ATTESTED` |
   | conflicting source-attested values | reject `CONCEPT_AUTHORITY_CONFLICT` |
   | pending-revalidation disqualifier active | reject `CONCEPT_REVALIDATION_PENDING` |
   | lower-authority (LLM/source-proposed) disagreement with the winner | diagnostic only; higher authority wins |

   "Agreed" = all accepted rows at the winning authority share one `proposed_value_hash`.
4. **Missing vs stale vs rejected** (active evidence excludes non-active, so read history over `field_evidence` for `(logical_ref, "concept")` across all `EvidenceLifecycle` — `ACTIVE|STALE|REJECTED|SUPERSEDED`), considering **only the two accepted producer-strength pairs** (source-proposed/LLM rows never turn missing authority into stale):
   - No `(SOURCE,ATTESTED)`/`(HUMAN,CONFIRMED)` row in **any** lifecycle → `CONCEPT_AUTHORITY_MISSING`.
   - Such rows exist but **none `ACTIVE`**, at least one `STALE`/`SUPERSEDED` → `CONCEPT_EVIDENCE_STALE`.
   - Only `REJECTED` such rows (never active/stale) → `CONCEPT_AUTHORITY_MISSING` (a rejected assertion is not stale authority).
5. Then: require `authoritative_concept ∈ CONCEPT_REGISTRY` (else `CONCEPT_NOT_IN_REGISTRY`); compare to `graph_node.concept` only to emit a `DISPLAY_CONCEPT_MISMATCH` **diagnostic**.
6. Read/DB failure → raise; the per-idea savepoint records a **technical** outcome, never a semantic reject.

## 3. Deterministic role assignment (no guessing)

The LLM gives unordered `(catalog_source, object_ref)` operands with no roles; Slice 3 marks every `derives_pair` a measure (`feature_assist.py:594`) — **not trusted**. B assigns each operand's `SemanticRole` **deterministically from governed metadata**:

- Each operand's `authoritative_concept` carries registry classification (temporal/pit → `TIME`; measure/quantity classification → `MEASURE`; countable identity → `COUNTED`).
- Match the multiset of concept-derived roles against the operation's required shape (§4). If they match exactly and unambiguously → assign. If a required role has no operand, an operand has no governed role, or two operands are indistinguishable for an **order-sensitive** slot → reject.
- **Order-sensitive operations are deferred**: `RATIO`/`DIFFERENCE` return `OPERAND_ORDER_AUTHORITY_MISSING`. Ordering is **never** taken from `derives_pairs` order, `measure_refs` order, feature name, description, or LLM rationale. (The concept registry has no feature-algebra role declarations; a later slice adds a governed operation-signature registry or a human-confirmed ordered intent. A's planner already models `NUMERATOR`/`DENOMINATOR`, so no planner migration is needed when B gains authority.)

## 4. Operation normalization (closed)

A bounded alias table maps prose → a canonical operation (`"average"/"mean"→AVG`, `"count distinct"/"nunique"→COUNT_DISTINCT`, …); unknown/compound prose → `OPERATION_UNRECOGNIZED`. Windowed ops require typed `window` + `time_ref` → else `WINDOW_REQUIRED_UNSPECIFIED`. B emits the versioned `OperationSpecV1` (defined in A §2) with the compiler-facing declarations A injects. Supported-for-resolution now: `SUM`,`AVG`,`MIN`,`MAX`,`STDDEV`,`COUNT`,`COUNT_DISTINCT`,`RECENCY`,`TREND` (roles unambiguous from metadata). `RATIO`/`DIFFERENCE` normalize structurally but resolve to `OPERAND_ORDER_AUTHORITY_MISSING`.

## 5. Governed grain

Authority order (missing/conflicting → `GRAIN_UNRESOLVED`): confirmed scope `target_entity` (authoritative anchor) → governed **source-qualified grain candidates** for that entity → LLM `grain_table` as a **consistency check only** (a bare table name is never authoritative; contradiction rejects). The intent carries the logical `target_entity`; A's planner selects the physical landing point. B does **not** emit a singular `target_grain_ref`.

## 6. Pinning ALL load-bearing needs (not just computation operands)

Computation operands are pinned to their exact node + authoritative concept. **Structural needs are also load-bearing**: a source-entity/grain/time key discovered by normal `candidates.py` display-concept match (`c.concept == need.concept`, `candidates.py:51`) would make advisory metadata load-bearing. Therefore B resolves synthetic **source/grain/time** needs from **governed structural facts** and pins or explicitly verifies them; only existing **VERIFIED bridge/realization** columns may remain planner-discovered (those mechanisms carry their own authority). Any structural need without a governed fact → reject (`STRUCTURAL_NEED_UNGOVERNED`).

## 7. Shadow hook (is_live-independent) + scope threading

The 3C.2a governed branch and its `_reject_cross_catalog_llm` call live under `elif is_live:` (`gate1.py:396`), skipped when live grounding is off — hooking there would collect **no** shadow evidence before the flip, defeating shadow-before-live. B's hook is **independent of `is_live`**, with an explicit predicate:

> confirmed scoped run ∧ `catalog_source is None` ∧ non-null confirmed `target_entity` ∧ `FEATUREGEN_LLM_XCAT_SHADOW` enabled.

`build_considered_set` (`gate1.py:341`) has neither `generation_run_id` nor a `ConfirmedScope`; those exist only in `_scoped_considered_set` before the builder call (`contract.py:394`). B threads an **immutable shadow context** `{ConfirmedScope, generation_run_id, roles, now}` from the scoped route into the builder. `ConfirmedScope` is **distinct** from planner `CatalogScopeV1` (confirmed-run provenance vs authorized catalog sources). The **unscoped** route (`contract.py:501`) has no generation run or confirmed scope → **explicitly excluded** from shadow (no run provenance to attribute).

**Population = alternatives AND the definition anchor**, unified. The anchor is generated after the `elif` block with its own cross-catalog rejection (`gate1.py:416`); collecting only alternatives would leave anchors as an untested governed path for 3C.2b-ii. B gathers cross-catalog (>1 distinct operand `catalog_source`) **alternatives + anchor** into **one pre-filter observation batch** (source-kind tagged), after they pass the deterministic feature gauntlet (`feature_assist._vet`) and **before** `_reject_cross_catalog_llm` still runs unchanged for users. B never modifies `alternatives`/`rejections`/`anchor`; no additional LLM call.

## 8. Canonical input + LLM provenance (replay)

`audited_structured_call` returns only the output dict (`enrich_llm.py:611`) and `_record_llm_call_durable` discards the call ref (`enrich_llm.py:380`); `FeatureIdea` carries no call/round/candidate index, so a "stable ordinal" is not stable across retries/critic-replacement/lenses. B requires the **generation seam to return/thread an observation origin** `{llm_call_ref, lens, round, candidate_index}` and mints a **stable idea-observation id**. For replay, B persists a **safe canonical adapter input** (redacted, structured — never raw hypothesis/rationale) *or* double-normalizes against one frozen context during capture; **hashes alone cannot replay an input**.

## 9. Telemetry (cohorts) + store (migration 1005)

**Per-plan cohorts** (resolves): `RESOLVED_HUMAN_CONFIRMED` (every operand human-confirmed) vs `RESOLVED_INCLUDES_SOURCE_ATTESTED` (≥1 source-attested). Report false-normalization and resolution rates **separately** per cohort — descriptive only; 3C.2b-ii may activate the human cohort first **without** hardcoding that into B's permanent model.

Store mirrors the `0999` manifest+reconciliation pattern (finding #8): run manifest + expected observation set + two-phase per-idea writes + reconciliation; append-only; idempotent `(run_id, idea_observation_id)`; role/scope fingerprints; diagnostic-code arrays; bounded/truncated status. Persist per idea: idea-observation id + origin `{llm_call_ref, lens, round, candidate_index}`; source kind (alternative|anchor); `adapter_input_hash` **and** `normalized_intent_hash` (separately) + the persisted canonical input reference; `synthetic_template_id` (from the normalized hash); per-operand computation role + planner role + pin `(catalog_source, object_ref)` + authority class + evidence ids/evidence-set hash + active disqualifiers; versions (adapter, operation-policy, concept-registry, planner, compiler); the A `MultiSourceBindingPlanV1` id + contract verdict; authoritative outcome and diagnostic outcome in **separate** columns; reason codes. **No** unredacted hypothesis/rationale/free-form text.

## 10. Bounds + latency (not customer-visible)

B runs inside the request; synchronous planning/compilation would add seconds/timeouts, so byte-identical output is **not** enough. Define `MAX_IDEAS_PER_RUN`, per-idea operand/catalog bounds, **one shared elapsed-time + compile budget**, deterministic truncation telemetry, and **prefer an outbox/worker boundary** so assembly runs off the request path. If inline, the shared budget hard-caps added latency and truncation is recorded, never silently dropped.

## 11. Dispositions

**Resolve:** normalizes → A resolves (all A steps pass) → operation + operands preserved. **Rejections (semantic):** `CONCEPT_AUTHORITY_MISSING`, `CONCEPT_AUTHORITY_CONFLICT`, `CONCEPT_EVIDENCE_STALE`, `CONCEPT_REVALIDATION_PENDING`, `CONCEPT_NOT_IN_REGISTRY`, `UNRESOLVABLE_COLUMN`, `OPERATION_UNRECOGNIZED`, `WINDOW_REQUIRED_UNSPECIFIED`, `OPERAND_SHAPE_INVALID`, `OPERAND_ORDER_AUTHORITY_MISSING`, `GRAIN_UNRESOLVED`, `STRUCTURAL_NEED_UNGOVERNED`, plus A's assembly rejections surfaced through. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`, `BUDGET_TRUNCATED`. **Diagnostics (never resolve, never gated):** `DISPLAY_CONCEPT_MISMATCH`, lower-authority disagreement, column-name/grain-name heuristic `diagnostic_candidate`.

## 12. Normalization gate

Over the window on the adversarial gold set: **zero false resolves** (no idea resolved to a plan not faithfully governing what was proposed); **100% operand + operation preservation** on resolves; **deterministic replay** (same inputs → identical normalized-intent hash/pins/outcome, replayable from the persisted canonical input); **no unexplained rejection category**; **no technical failures**. Resolution rate is descriptive only. **Do not gate on resolution rate.**

## 13. Gold set (must include)

Valid single-measure and `TREND` cross-catalog ideas; `RATIO`/`DIFFERENCE` (must return `OPERAND_ORDER_AUTHORITY_MISSING`); unresolvable column; unregistered authoritative concept; conflicting human evidence; source-only vs human-confirmed (cohort split); stale-only and rejected-only evidence (distinct outcomes); display ≠ authoritative concept (diagnostic, pin still correct); grain contradiction; unknown/compound operation; missing window; a structural need with no governed fact; an idea whose display concept would mislead discovery (proves pin bypass); an injected DB error (isolated technical failure, reconciliation intact); a run exceeding the idea/latency budget (truncation recorded).

## 14. Reused surfaces

`read_active_field_evidence`/`to_view`/`active_disqualifiers_for` + a new all-lifecycle history read; `EvidenceProducer`/`AssertionStrength`/`EvidenceLifecycle`; `CONCEPT_REGISTRY` (`overlay/upload/concepts.py`); the A planner + `MultiSourcePlannerIntentV1`; `gate1.build_considered_set` (hook + shadow context threading); the generation seam (`recommend_features`/`recommend_feature_sets_report` + `enrich_llm`) extended to thread observation origin; the `0999`/`shadow_store.py` manifest+reconciliation pattern; the `CompileBudget` bound pattern.
