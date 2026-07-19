# Phase 3C.2b-i — Governed LLM Cross-Catalog Adapter (Shadow) — Design

**Status:** approved for planning
**Date:** 2026-07-19
**Branch (to create):** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` off `origin/main` (`8636b4d` or later)
**Predecessors:** 3C.2a (`live_activation.py`, plan envelope, `_governed_cross_catalog_options`, `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED`, migration 1002); Slice 3 feature-generation (heavily reworked `feature_assist.py`, contract validation tri-state migration 1003).
**Next free migration:** 1004.

---

## 1. Purpose and scope

3C.2b as a whole makes LLM-proposed **cross-catalog** feature ideas *governable* (compiled through the deterministic planner) rather than merely rejected, and then removes the permissive `entity.find_cross_catalog_path`. It is split shadow-first:

- **3C.2b-i (this spec):** build the deterministic `FeatureIdea → PinnedPlannerIntentV1 → plan_bindings → contract` adapter and run it **in shadow** — log-only, measured against an adversarial gold set. Zero customer-visible change. Nothing surfaces.
- **3C.2b-ii (later):** once the adapter is proven, wire it live into the considered set (compile-and-surface / reject) and remove `find_cross_catalog_path`.

This slice exists to answer one question with evidence before any live flip: **does the adapter govern the feature the LLM actually proposed** — the right columns, the right operation, the right grain, under real column→concept authority — with zero false resolves?

### 1.1 What 3C.2b-i does NOT do

- It does **not** modify the alternatives or rejections returned to users. The 3C.2a blanket `_reject_cross_catalog_llm` rejection still fires for every cross-catalog LLM idea. The adapter runs alongside it, log-only.
- It does **not** remove or alter `find_cross_catalog_path`.
- It does **not** make any additional LLM call. It consumes ideas already produced by the existing deterministic feature gauntlet.
- It does **not** surface a plan, an option, or a contract to any user or API response.
- It does **not** promote the global `concept` field policy to OPERATIONAL, and does not change any existing concept/taxonomy consumer.

## 2. Non-negotiables (invariants)

1. **No inference latitude in governed output.** Column-name and grain-name heuristics may produce *shadow diagnostics* only; a heuristic result can never be a `RESOLVED_*` outcome and never contributes to the enablement pass rate.
2. **Authority, not display.** A governed operand's concept is resolved from raw lifecycle-active **evidence** (source-attested or human-confirmed), never from `graph_node.concept` (discovery hint only) and never from the always-null `concept.load_bearing_value_hash`.
3. **Pins bypass discovery.** A pinned computation operand's candidate is *constructed* from the exact source-qualified graph node plus its authoritative concept binding — never selected by `c.concept == need.concept`. When no pins are supplied, existing discovery is byte-identical.
4. **Operand preservation is proof.** A compiled plan that did not bind every computation operand to exactly its pinned `(catalog_source, object_ref)` is `OPERAND_BINDING_NOT_PRESERVED`, not a resolve.
5. **Fail-closed.** Any missing, ambiguous, conflicting, stale, unregistered, or untypeable input rejects normalization. The adapter never guesses.
6. **Technical ≠ semantic.** DB / infrastructure / read failures are recorded as *technical* outcomes in an isolated savepoint, never as a semantic rejection and never as a resolve.
7. **F4 preserved.** The adapter output is a contract *definition* with a governed physical plan; it is never an attested cross-catalog `approved_join`.
8. **Behaviour-neutral.** Log-only; trivially byte-identical to the surfaced path because nothing surfaces. No signing. No data plane.

## 3. The authority seam (why a dedicated resolver is required)

On `origin/main`, `field_policies.py` defines `concept` with `_recommendation(...)` → `influence_max = RECOMMENDATION`. `resolve_field_authority` (`overlay/field_authority.py:255`) short-circuits any non-OPERATIONAL field: `return FieldResolution(display, None, "influence_not_operational")`. Therefore every production concept decision has `display_value_hash = <concept>` and `load_bearing_value_hash = NULL`, locked by `test_field_resolution.py::test_decision_event_per_field_carries_both_effective_values`. The `operational_rule = _SOURCE_OR_HUMAN` on `concept` documents a *possible future promotion*; it mints no authority today.

Consequence: a verifier against `concept.load_bearing_value_hash` would reject every real operand. The adapter must therefore resolve its **own** planner-specific authority over the raw evidence via a dedicated resolver, and (if a durable read surface is wanted) write a **distinct** `planner_concept_binding` decision rather than overloading the advisory concept decision as operational.

## 4. Architecture and data flow

The shadow pipeline, per idea (each idea in its own savepoint):

```
FeatureIdea (cross-catalog, span > 1 catalog_source)
  │
  ├─ 1. Rebind exact source-qualified operand identities  (derives_pairs → (catalog_source, object_ref) graph nodes)
  ├─ 2. Resolve governed concept binding per operand      (resolve_planner_concept_binding — authority FIRST)
  ├─ 3. Registry check + display diagnostic               (authoritative_concept ∈ CONCEPT_REGISTRY; vs graph_node.concept → DISPLAY_CONCEPT_MISMATCH diag)
  ├─ 4. Resolve computation roles + target grain          (operation→operand-shape matrix; grain from confirmed scope, not grain_table)
  ├─ 5. Normalize operation through closed vocabulary      (alias table; compiler-facing declarations)
  ├─ 6. Build PinnedPlannerIntentV1                        (synthetic Template + explicit synthetic Needs + pins)
  ├─ 7. plan_bindings(..., pins=...)                       (pinned candidates bypass display-concept discovery)
  ├─ 8. Assert operands preserved                          (every computation operand == its pin, else OPERAND_BINDING_NOT_PRESERVED)
  ├─ 9. Compile contract                                   (existing contract-compile pass)
  └─ 10. Persist shadow outcome + reason codes             (migration 1004; authoritative + diagnostic separately)
```

## 5. Components and interfaces

### 5.1 `planner/concept_authority.py` — the authority resolver

```
resolve_planner_concept_binding(conn, logical_ref)
    -> PlannerConceptBinding | ConceptAuthorityRejection
```

`PlannerConceptBinding = @dataclass(frozen=True, slots=True)` with:
`authoritative_concept: str`, `authority: ConceptAuthority` (`HUMAN_CONFIRMED | SOURCE_ATTESTED`), `evidence_ids: tuple[str, ...]`, `evidence_set_hash: str`, `value_hash: str`.

It does **not** take an `expected_concept`. It resolves authority first; comparison happens afterward in the caller (§5.2 step 3). Rationale: passing an expected value would let the untrusted display concept steer which authoritative value is sought.

Behaviour:
1. Read lifecycle-**active** concept evidence: `read_active_field_evidence(conn, logical_ref, "concept")` (`overlay/field_evidence.py:133`), viewed via `to_view`.
2. Consider **only** `(producer=SOURCE, strength=ATTESTED)` and `(producer=HUMAN, strength=CONFIRMED)`. Ignore LLM/source-proposed/taxonomy for authority (they may still be recorded as lower-authority diagnostics).
3. Apply `active_disqualifiers_for(conn, logical_ref, "concept")` — a pending revalidation / conflict disqualifier rejects (`CONCEPT_AUTHORITY_CONFLICT` when a conflict disqualifier is active).
4. Precedence ladder (exact):

   | Active accepted evidence | Outcome |
   |---|---|
   | one agreed human-confirmed value | `HUMAN_CONFIRMED` (that value) |
   | conflicting human-confirmed values | reject `CONCEPT_AUTHORITY_CONFLICT` |
   | no human, one agreed source-attested value | `SOURCE_ATTESTED` (that value) |
   | conflicting source-attested values | reject `CONCEPT_AUTHORITY_CONFLICT` |
   | lower-authority disagreement with the winner | recorded as a diagnostic; higher authority wins |

   "Agreed" = all accepted rows at the winning authority carry the same `proposed_value_hash`.
5. Missing vs stale (requires reading beyond active evidence, since active excludes stale/superseded):
   - No SOURCE/HUMAN concept evidence in **any** lifecycle → reject `CONCEPT_AUTHORITY_MISSING`.
   - SOURCE/HUMAN concept evidence exists but **none is `ACTIVE`** (all `STALE`/`SUPERSEDED`) → reject `CONCEPT_EVIDENCE_STALE`.
   - The plan adds/uses a history read over `field_evidence` filtered by `(logical_ref, "concept")` across all `EvidenceLifecycle` values for this distinction.
6. Read/DB failure → raise; the caller's per-idea savepoint records a **technical** outcome, never a semantic rejection.

### 5.2 `planner/pinned_intent.py` — normalization to a pinned intent

Types (all `@dataclass(frozen=True, slots=True)` / lowercase-snake `StrEnum`):

- `ComputationRole(StrEnum)`: `MEASURE | GROUPING | TIME | GRAIN`.
- `PlannerJoinRole` re-uses the existing `JoinRole` axis (`MEASURE | SOURCE_ENTITY_KEY | INTERMEDIATE_ENTITY_KEY | TIME`). The two axes are **independent**: multiple `JoinRole.MEASURE` needs are legitimate.
- `PinnedOperand`: `slot_id: str` (e.g. `measure_0`), `computation_role: ComputationRole`, `join_role: JoinRole`, `catalog_source: str`, `object_ref: str`, `concept: str` (authoritative), `authority: ConceptAuthority`, `evidence_ids: tuple[str, ...]`.
- `PinnedPlannerIntentV1`: `synthetic_template_id: str` (derived from the normalized-intent hash), `target_entity: str`, `target_grain_ref: str`, `operation: OperationSpec`, `operands: tuple[PinnedOperand, ...]`, `catalog_sources: tuple[str, ...]`, `normalized_intent_hash: str`, `adapter_input_hash: str`.

```
normalize_feature_idea(conn, idea, *, scope, roles, now)
    -> PinnedPlannerIntentV1 | NormalizationRejection
```

Steps 1–6 of §4. Each failure returns a typed `NormalizationRejection` carrying a disposition (§8). No step may fall through to a heuristic default.

**Synthetic Template construction.** `plan_bindings` consumes a `Template` with typed `Need`s. Because the synthetic `template_id` is **absent** from static `RESOLVED_NEED_METADATA`, discovery falls back to `need.*` fields (`candidates.py`), so each synthetic `Need` MUST carry explicit `role` (unique slot id), `concept` (the authoritative concept), `join_role`, `temporal_role`, and `allowed_source_grains`. Template-level `aggregation`, `additivity`, `pit`, and window params come from the closed operation registry (§7), never from placeholders.

### 5.3 Pins in the planner — bypass, not filter

Thread an optional `pins: Mapping[str, tuple[str, str]] | None` (`need.role → (catalog_source, object_ref)`) through `plan_bindings` → `discover_ingredient_candidates`.

- When `pins` is `None`: existing discovery path is **byte-identical**.
- When a need is pinned: construct its single `IngredientCandidateV1` directly from the exact `(catalog_source, object_ref)` graph node plus the authoritative concept binding — do **not** enter the `c.concept == need.concept` scan. Grain and safety checks (`object_grain`, `evaluate_binding_safety`) still apply to the pinned column; a pinned column that is unsafe or grain-incompatible yields a normal planner rejection for that plan.
- Non-computation needs (join keys, bridge columns) are unpinned and discovered normally; they may enter only the physical read set, never a computation-operand slot.

**Operand-preservation assertion** (§4 step 8): after planning, for the selected physical plan, assert every computation-operand slot binds exactly its pinned pair. Any deviation → `OPERAND_BINDING_NOT_PRESERVED` (a technical/logic failure of the pin mechanism, recorded, never a resolve).

### 5.4 `planner/llm_shadow.py` — the shadow entry + gate hook

`run_shadow_planner` iterates *registered recipe IDs* and cannot take LLM ideas, so this slice adds a separate log-only entry:

```
run_llm_cross_catalog_shadow(conn, *, ideas, scope, roles, now, generation_run_id, ...) -> None
```

**Hook point.** In `gate1.build_considered_set`, immediately **before** `alternatives, cross_catalog_rejections = _reject_cross_catalog_llm(alternatives)` (`gate1.py:404`):
- Select the alternatives whose derives span **> 1 distinct `catalog_source`** (same span computation `_reject_cross_catalog_llm` uses).
- These have already passed the deterministic feature gauntlet (`feature_assist._vet`, `gate1.py:404` is downstream of generation).
- For each, run the adapter + planner + compile in an **isolated savepoint**; write shadow rows. Catch and record DB/infra failures as technical outcomes; never propagate into the request transaction; never alter `alternatives`.
- Gated behind a telemetry flag (e.g. `FEATUREGEN_LLM_XCAT_SHADOW`, default-off) so it is inert until enabled, exactly as prior shadow harnesses.

### 5.5 `planner/llm_shadow_store.py` + migration 1004

A dedicated store (do not overload the recipe shadow store). See §10.

## 6. Operation → operand-shape matrix and closed vocabulary

The operation registry is a **closed** contract. A small alias table maps prose to a canonical operation; unknown or compound prose rejects (`OPERATION_UNRECOGNIZED`).

```
"average","mean"            -> AVG
"count distinct","nunique"  -> COUNT_DISTINCT
"sum","total"               -> SUM        (etc.)
```

Deterministic operand-shape matrix (missing / extra / ambiguous operands reject with `OPERAND_SHAPE_INVALID`):

| Operation | Required computation operands |
|---|---|
| `SUM`,`AVG`,`MIN`,`MAX`,`STDDEV` | exactly 1 `MEASURE` |
| `RATIO`,`DIFFERENCE` | exactly 2 `MEASURE` |
| `COUNT`,`COUNT_DISTINCT` | exactly 1 counted operand |
| `RECENCY` | exactly 1 `TIME` |
| `TREND` | exactly 1 `MEASURE` + 1 `TIME` + typed `window` |

Arbitrary grouping is **unsupported** in this slice; grouping is the governed **target grain**, not a free operand. Windowed operations require a typed `window` and `time_ref`; these are never inferred from aggregation text (`WINDOW_REQUIRED_UNSPECIFIED` if absent). Each registry entry supplies every compiler-facing declaration: aggregation function, output additivity, window parameters, temporal requirements.

## 7. Grain authority

Authority order (missing or conflicting → reject `GRAIN_UNRESOLVED`):
1. Confirmed scope `target_entity` (the authoritative anchor).
2. Governed grain fact / source-qualified `grain_ref` for that entity.
3. LLM `grain_table` used **only** as a consistency check; a bare table name is never authoritative (ambiguous across catalogs; an entity-scoped cross-catalog run need not have one grain table). A grain_table that contradicts the governed grain rejects.

## 8. Dispositions

**Resolved (authoritative, per-operand authority carried on each `PinnedOperand`):** an idea resolves only if it fully normalizes, plans, preserves operands, and compiles.

**Rejections (semantic, fail-closed):**
`CONCEPT_AUTHORITY_MISSING`, `CONCEPT_AUTHORITY_CONFLICT`, `CONCEPT_EVIDENCE_STALE`, `CONCEPT_NOT_IN_REGISTRY`, `OPERAND_SHAPE_INVALID`, `OPERATION_UNRECOGNIZED`, `WINDOW_REQUIRED_UNSPECIFIED`, `GRAIN_UNRESOLVED`, `UNRESOLVABLE_COLUMN` (a `derives_pair` that maps to no graph node), plus the planner's own no-plan classifications when a fully-normalized intent still fails to plan.

**Technical (never semantic, never resolve):** `OPERAND_BINDING_NOT_PRESERVED`, `TECHNICAL_FAILURE` (DB/infra/read).

**Diagnostics (recorded, never a resolve, never counted toward pass rate):** `DISPLAY_CONCEPT_MISMATCH` (authoritative concept ≠ `graph_node.concept`), lower-authority disagreement, and any `diagnostic_candidate` produced by column-name / grain-name heuristics.

## 9. Telemetry and enablement gate

**Per-plan cohorts** (for resolved plans):
- `RESOLVED_HUMAN_CONFIRMED` — **every** computation operand is human-confirmed.
- `RESOLVED_INCLUDES_SOURCE_ATTESTED` — at least one operand relies on source attestation.

Report false-normalization and resolution rates **separately** per cohort. Cohort resolution rates are **descriptive only** — 3C.2b-ii may choose to activate the human-confirmed cohort first without hardcoding that restriction into the adapter's permanent authority model.

**The enablement gate does NOT gate on resolution rate.** It requires, over the evaluation window on the adversarial gold set:
1. **Zero false resolves** (no idea resolved to a plan that does not faithfully govern what was proposed).
2. **100% operand preservation** on all resolves.
3. **Deterministic replay** (same inputs → identical normalized-intent hash, pins, outcome).
4. **No unexplained rejection category** (every outcome maps to a defined disposition).
5. **No technical failures** over the window.

## 10. Migration 1004 (`1004_llm_cross_catalog_shadow.sql`)

Append-only shadow table. Persists, per idea:
- Generation run id, LLM call / lens, stable idea ordinal.
- `adapter_input_hash` **and** `normalized_intent_hash` (separately).
- `synthetic_template_id` (derived from the normalized-intent hash).
- Per-operand: computation role, planner join role, pin `(catalog_source, object_ref)`, authority class. (jsonb array; hashes/ids only.)
- Evidence ids / evidence-set hash and active disqualifiers.
- Versions: planner, adapter, operation-policy, concept-registry, compiler.
- Selected physical plan id and contract verdict.
- **Authoritative outcome** and **diagnostic outcome** in separate columns; rejection reason codes.
- **No** unredacted hypothesis, rationale, or other free-form text. The table stores identities, hashes, enums, and provenance ids only. It makes **no** claim that the concept load-bearing hash supplied authority.

New version constants: `ADAPTER_VERSION`, `OPERATION_POLICY_VERSION` (alongside reused `PLANNER_VERSION`, `PLAN_CONTRACT_VERSION`/`PHYSICAL_PLAN_VERSION`, concept-registry version, compiler version).

## 11. Testing

- **Adversarial gold set** of cross-catalog `FeatureIdea` shapes: valid single/two-measure/trend/recency; unresolvable column; unregistered authoritative concept; conflicting human evidence; source-only vs human-confirmed; stale-only evidence; display≠authoritative; grain contradiction; unknown/compound operation; missing window; a crafted idea whose display concept would mislead discovery (proves pin bypass).
- **Property tests:** operand preservation on every resolve; determinism/replay of the normalized-intent hash; no active-evidence path ever consults `graph_node.concept` for authority; `pins=None` discovery byte-identical to pre-change (golden comparison).
- **Isolation:** an injected DB error in one idea records `TECHNICAL_FAILURE` and does not poison the request transaction or the other ideas.
- **Behaviour-neutrality:** with the shadow flag off, `build_considered_set` output and the recipe shadow path are byte-identical to `origin/main`.

## 12. Reused surfaces (no new governance system)

`read_active_field_evidence` / `to_view` / `active_disqualifiers_for` (evidence); `EvidenceProducer` / `AssertionStrength` / `EvidenceLifecycle` (enums); `CONCEPT_REGISTRY` (`overlay/upload/concepts.py`); `plan_bindings` / `discover_ingredient_candidates` / `enumerate_single_catalog_plans` / `order_plans` / the contract-compile pass (planner); `_envelope` / `BindingPlanningResultV1` (plan carriers); `write_run_and_plans`-style two-phase store pattern; `gate1.build_considered_set` hook at the `_reject_cross_catalog_llm` boundary.
