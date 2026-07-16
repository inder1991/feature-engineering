# Phase 3B.3c — Contract Resolvability Classifier Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). **C4 (per-ingredient aggregation validation) + C5 (composition) are the algorithmic core.** The whole layer is shadow/log-only/compute-only; the durable store is 3B.4.

**Goal:** Classify each 3B.3b `source_to_target_resolved` plan as a complete feature contract — or, honestly, exactly why not — by computing per-ingredient aggregation, temporal, universal-safety, and freshness declarations onto the in-memory plan. Validate, never fabricate; the only sound derivation is additive→SUM.

**Architecture:** New `planner/declarations.py` (a pure compiler + a batched `CompilerContext`) invoked as a post-assembly pass from `plan.py::plan_bindings`. Three orthogonal status axes; a `physical_plan_id`/`contract_id` split. Log-only, compute-only, no store, no migration.

**Tech Stack:** Python 3.11 (frozen dataclasses, StrEnum), PostgreSQL (read-only), pytest (`db` fixture). `uv run pytest/ruff/mypy`.

**Spec:** `docs/superpowers/specs/2026-07-16-phase3b3c-contract-completion-design.md` (v2).

## Global Constraints (every task's requirements include these)

- **Compute-only / shadow / behaviour-neutral / no migration / no store.** The compiler returns enriched plan objects; nothing is persisted (that's 3B.4). Consumed only by `run_shadow_planner`; the live grounding path and `api/routes/contract.py` response are untouched; per-recipe savepoint isolation preserved; F4 preserved. Full `tests/featuregen` stays green.
- **Validate, never fabricate.** The ONLY sound aggregation derivation is `additive → SUM`. Every other function must be recipe-declared; undeclared → honest `unresolved_*` with a precise reason. 3B.3c builds no authoring surface.
- **Three orthogonal axes.** `resolution_status` (ingredient, 3B.3a) and `path_resolution_status` (path, 3B.3b) are UNCHANGED. NEW `contract_resolution_status` (3B.3c) never overloads `resolved`; non-`source_to_target_resolved` plans get `not_compiled`.
- **Identity split.** `physical_plan_id` (renamed from `plan_id`) is immutable through compilation and is the ranking tie-break. `contract_id` is minted over declaration material only — timestamps/freshness/stamps EXCLUDED. Reason codes canonically ordered (enum order) + deduped so `contract_id` is stable.
- **Universal-safety only** (leakage_anchor + protected/special), separate from PII/authorization; `not_evaluated` is structural (no `_Col`), never treated as `safe`.
- **Freshness** uses `config.drift_freshness_sla` (+ its policy version), the `overlay_checkpoint ≥ drift_head_seq` projection-lag guard, and end-of-compile stamp revalidation.
- **Batched + bounded + kill-switched.** One `CompilerContext` per run (realizations/bridges/columns/stamps/config loaded once); a per-run compile budget + deadline; an env kill-switch passed in from the route (planner stays pure — no `os.environ` in the planner).
- **Convention:** frozen `@dataclass(frozen=True, slots=True)`; lowercase-snake `StrEnum`; ruff (`collections.abc`, E402 top-of-file); no import cycles (`declarations.py` may import `contracts`/`safety`/`catalog_realizations`/`bridge_projection`/`catalog_changes`/`templates`/`need_metadata`/`taxonomy`/`overlay.config`, NOT the route). Branch `feature/phase3b3c-contract-completion`; harness default commit trailer.

## Reused interfaces (verified)
- `IngredientBindingV1(recipe_id, need_role, concept, required_grains, join_role, temporal_role, bound_catalog_source, bound_object_ref, actual_source_grain, binding_quality, safety, reason_codes)`.
- `BindingPathSegmentV1(segment_kind, catalog_source, from_entity, to_entity, realization_ref, bridge_fact_key, cardinality, direction, reason_codes)` — C1 adds `relationship_id`, `relationship_version`.
- `CatalogEntityRelationshipV1(realization_id, relationship_id, relationship_version?, catalog_source, from_object_ref, from_object_grain, to_object_ref, to_object_grain, from_key_ref, from_key_entity, to_key_ref, to_key_entity, declared_cardinality, authority, status, reversed_authoring)`; `derive_catalog_realizations(conn, catalog).realizations`. `to_key_ref` = the realization's GROUP-BY key.
- `Cardinality` = `one_to_one|one_to_many|many_to_one|many_to_many`.
- `_Col(catalog_source, object_ref, table, column, data_type, is_grain, is_as_of, concept, entity, additivity, sensitivity, currency)` + `_load_columns(conn, catalog_source, roles) -> list[_Col]` (read-scoped) + `_safe_to_bind(col) -> bool`; `evaluate_binding_safety(col: _Col) -> BindingSafety` (returns only safe|unsafe; unknown-concept→safe). `concept(name).additivity`.
- `table_of(object_ref) -> str`.
- Freshness: `from featuregen.overlay.catalog_changes import drift_watermark, drift_head_seq`; `from featuregen.projections.runner import _checkpoint_seq` (`_checkpoint_seq(conn,"overlay")`); `OverlayConfig.drift_freshness_sla: timedelta` (loader in `overlay/config.py`).
- `CatalogStateStampV1(catalog_source, head_seq, last_completed_at, stamp_kind=drift_watermark)`; `PlannerReplayEnvelopeV1` + `_envelope(conn, scope, recipe_id, target_entity)`; `ReplayStrength`.
- `RESOLVED_NEED_METADATA[template.id]` → per-need `temporal_role`; `TemporalRole`. `template.params` (allowed-value tuples, first=default); `template.additivity` (OUTPUT).
- Flag pattern: `os.environ.get("FEATUREGEN_INTENT_CONTRACT_COMPILE","0")=="1"` (route-side).

## File Structure

| File | Responsibility |
|---|---|
| `planner/contracts.py` (MODIFY) — C1 | axes/vocabularies; `plan_id`→`physical_plan_id`; `contract_id` material; extend plan/segment/envelope/result |
| `planner/assembly.py`,`enumerate.py`,`order.py` (MODIFY) — C1 | rename ranking-key `plan_id`→`physical_plan_id`; assembly emits `relationship_id`/version |
| `planner/safety.py` (MODIFY) — C6 | compiler-facing safety wrapper for arbitrary physical columns |
| `planner/declarations.py` (CREATE) — C2–C7 | `CompilerContext` + the seven checks + `compile_contract` |
| `planner/plan.py` (MODIFY) — C8 | wire the batched compile pass + result roll-up + guards |
| `api/routes/contract.py` (MODIFY) — C8 | read the kill-switch env flag, pass `compile_contracts` into `run_shadow_planner` |
| Tests | `test_declarations.py` (new) + updates to `test_contracts/plan/assembly/enumerate/order` |

---

### Task C1: Axes, vocabularies, identity split, contract fields

**Files:** Modify `contracts.py`, `assembly.py`, `enumerate.py`, `order.py`; Test `test_contracts.py` (+ update `test_assembly/enumerate/order/plan` for the rename).

**Interfaces produced:** `ContractResolutionStatus`; `AggregationFunction`, `AggregationValidation`, `AdditivityClass`, `AdditivitySource`, `AggregationAxisKind`; `IngredientAggregationV1`, `HopAggregationV1`, `TemporalDeclarationV1`, `WindowSpecV1`, `ParamBindingV1`, `ColumnRole`, `PhysicalColumnReadV1`, `PhysicalReadSetV1`, `ReplayFreshness`; `BindingPlanV1.physical_plan_id` (renamed) + `contract_id`/`contract_resolution_status`/`hop_aggregations`/`temporal_declaration`/`physical_read_set`/`resolved_at_compilation`; `BindingPathSegmentV1.relationship_id`/`relationship_version`; `PlannerReplayEnvelopeV1` additions + `stamp_consistency`; `BindingPlanningResultV1.contract_result_status`; `make_contract_id(...)`; `ReasonCode` additions; `PLAN_CONTRACT_VERSION` bump.

- [ ] **Step 1: Write failing tests** (`test_contracts.py` append):

```python
def test_new_contract_axis_and_enums():
    assert set(c.ContractResolutionStatus) >= {
        c.ContractResolutionStatus.resolved, c.ContractResolutionStatus.not_compiled,
        c.ContractResolutionStatus.unresolved_ingredient_connectivity,
        c.ContractResolutionStatus.unresolved_aggregation_declaration,
        c.ContractResolutionStatus.unresolved_temporal_declaration,
        c.ContractResolutionStatus.unresolved_safety_evaluation,
        c.ContractResolutionStatus.safety_rejected,
        c.ContractResolutionStatus.unresolved_freshness}
    assert c.AggregationFunction.sum == "sum"
    assert {v.value for v in c.AggregationValidation} == {"sound","incompatible","undeclared","inputs_missing"}
    for r in ("ingredient_not_connected_to_path","aggregation_strategy_missing",
              "aggregation_incompatible_with_additivity","aggregation_weight_missing",
              "aggregation_components_missing","aggregation_axis_unsupported",
              "aggregation_composition_unsupported","semi_additive_temporal_strategy_missing",
              "temporal_anchor_missing","temporal_anchor_ambiguous","additivity_source_conflict",
              "safety_evaluation_incomplete","freshness_stamp_unavailable","participating_catalog_stale",
              "projection_lagging","catalog_mutated_during_compile","compile_budget_exhausted"):
        assert r in {x.value for x in c.ReasonCode}

def test_physical_plan_id_rename_and_contract_id_excludes_time():
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    p = c.make_binding_plan(recipe_id="t", target_entity="cust", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    assert p.physical_plan_id.startswith("bp_")
    assert p.contract_resolution_status is c.ContractResolutionStatus.not_compiled  # default until compiled
    # contract_id is stable across two mints with different timestamps
    cid1 = c.make_contract_id(p, resolved_at_compilation=_dt(2026,1,1))
    cid2 = c.make_contract_id(p, resolved_at_compilation=_dt(2099,9,9))
    assert cid1 == cid2 and cid1.startswith("cc_")
```
(`_dt` = a timezone-aware datetime helper in the test.)

- [ ] **Step 2: Run red** — `uv run pytest tests/featuregen/overlay/upload/planner/test_contracts.py -q` → FAIL.

- [ ] **Step 3: Implement** in `contracts.py`:
  - Add enums: `ContractResolutionStatus` (8 members per spec §2); `AggregationFunction` (`sum,count,min,max,last_as_of,first_as_of,average_over_period,max_over_period,weighted_average,recomputed_ratio,none`); `AggregationValidation` (`sound,incompatible,undeclared,inputs_missing`); `AdditivityClass` (`additive,semi_additive,non_additive,not_applicable,unknown`); `AdditivitySource` (`uploaded_column,concept,unknown`); `AggregationAxisKind` (`entity,time`); `ColumnRole` (`ingredient,temporal_anchor,join_key,bridge_key,aggregation_weight,aggregation_component,filter,partition`); `ReplayFreshness` (`current,drifted,unverifiable`); `StampConsistency` (`consistent,unverifiable`).
  - Add dataclasses (all frozen+slots): `WindowSpecV1(length:int|None, unit:str|None, boundary:str|None, inclusive:bool)`; `ParamBindingV1(values:tuple[tuple[str,str],...], is_representative:bool)`; `TemporalDeclarationV1(pit_anchor:TemporalRole|None, anchor_binding:str|None, window:WindowSpecV1|None, param_binding:ParamBindingV1, time_axis_aggregating:bool, reason_codes:tuple[ReasonCode,...])`; `IngredientAggregationV1(need_role, bound_object_ref, additivity:AdditivityClass, additivity_source:AdditivitySource, physical_cardinality:Cardinality, axis:AggregationAxisKind, declared_function:AggregationFunction|None, validation:AggregationValidation, missing_inputs:tuple[str,...], reason_codes)`; `HopAggregationV1(semantic_hop_index:int, from_entity, to_entity, physical_cardinality:Cardinality, grouping_keys:tuple[str,...], ingredient_stages:tuple[IngredientAggregationV1,...])`; `PhysicalColumnReadV1(object_ref, catalog_source, roles:tuple[ColumnRole,...], safety:BindingSafety, reason_codes)`; `PhysicalReadSetV1(columns:tuple[PhysicalColumnReadV1,...])`.
  - Extend `BindingPathSegmentV1` with `relationship_id: str|None = None`, `relationship_version: str|None = None`.
  - **Rename** `BindingPlanV1.plan_id` → `physical_plan_id` (keep it first field). Add fields: `contract_id: str|None = None`, `contract_resolution_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled`, `hop_aggregations: tuple[HopAggregationV1,...] = ()`, `temporal_declaration: TemporalDeclarationV1|None = None`, `physical_read_set: PhysicalReadSetV1|None = None`, `resolved_at_compilation: datetime|None = None`. In `make_binding_plan`, rename the minted field to `physical_plan_id=` (keep the `"bp_"` material exactly — the rename must NOT change the hash).
  - Extend `PlannerReplayEnvelopeV1` with: `aggregation_rule_version`, `additivity_rule_version`, `temporal_rule_version`, `safety_evaluator_version`, `drift_freshness_sla_version`, `authz_role_claims: tuple[str,...]`, `recipe_content_hash: str`, `catalog_state_stamps: tuple[CatalogStateStampV1,...]`, `stamp_consistency: StampConsistency`. Use `ReplayStrength.audit_only` (ALREADY defined) for compiled plans' envelopes. The segment `relationship_id`/`relationship_version` are sourced from the semantic hop (`EntityRelationshipRefV1.relationship_id`/`relationship_version`) at assembly-emission time — NOT from the catalog realization.
  - Extend `BindingPlanningResultV1` with `contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled` (the roll-up; separate from `result_status`).
  - Add `ReasonCode` members (all 17 from the test) + version constants `AGGREGATION_RULE_VERSION`, `ADDITIVITY_RULE_VERSION`, `TEMPORAL_RULE_VERSION`, `SAFETY_EVALUATOR_VERSION`, `DRIFT_FRESHNESS_SLA_VERSION="1.0.0"`. Bump `PLAN_CONTRACT_VERSION` to `"3b3c.1.0.0"`.
  - Add `make_contract_id(plan, *, resolved_at_compilation) -> str`: material = `physical_plan_id · sorted per-ingredient (need_role, additivity, declared_function, validation) · temporal signature · composition/contract_resolution_status · AGGREGATION_RULE_VERSION · ADDITIVITY_RULE_VERSION · TEMPORAL_RULE_VERSION · SAFETY_EVALUATOR_VERSION · PLAN_CONTRACT_VERSION`. **Excludes** `resolved_at_compilation`, freshness, stamps. `"cc_" + sha256(...)[:16]`. Reason codes sorted by enum order + deduped in the material.

- [ ] **Step 4: Rename the ranking-key call sites** — `assembly.py:559` (`p.plan_id`→`p.physical_plan_id`), `enumerate.py:98`, `order.py:37`, `plan.py:89`/`:138`/`:219-220` (dedup + selection). `selected_plan_id`/`planner_matched_plan_id`/`GroundTemplateDiffV1` continue to hold the `physical_plan_id` value (selection is by physical id). Update `test_assembly/enumerate/order/plan` assertions referencing `.plan_id`.

- [ ] **Step 5: Run + gates + commit** — `uv run pytest tests/featuregen/overlay/upload/planner/ -q`; `ruff check`; `mypy`. Commit `feat(3b3c): contract axis + vocabularies + physical_plan_id/contract_id split (task c1)`.

---

### Task C2: Ingredient connectivity

**Files:** Modify `declarations.py` (create with the `CompilerContext` + this check); Test `test_declarations.py`.

**Interfaces produced:** `CompilerContext` (batched, immutable); `check_connectivity(ctx, plan) -> ConnectivityResult(connected: bool, disconnected_roles: tuple[str,...])`.

- [ ] **Step 1: Write failing DB tests** — a plan whose two ingredients sit on different transaction-grain tables where the path only rolls up one → `connected is False`, the other role reported; a plan whose ingredients are all on path/co-located tables → `connected is True`. *[fixtures reuse `CanonicalRow`+`build_graph`.]*

- [ ] **Step 2–4: Implement.** `CompilerContext` frozen dataclass built once per run (§ C8): `realizations_by_catalog`, `active_bridges`, `columns_by_catalog: dict[str, dict[str,_Col]]` (from `_load_columns`), `catalog_head_seq_at_start: dict[str,int]`, `catalog_stamps`, `config`, `roles`, `now`. `check_connectivity`: gather the path's tables — `table_of` of each segment endpoint (`realization_ref`→realization to/from object_ref; `bridge_fact_key`→bridge endpoint refs) + the source-key binding's table; a `co_located` table = same `(catalog, table)` as the source-key binding. For each `IngredientBindingV1`, `table_of(bound_object_ref)` must be a path table or co-located, else record the `need_role`. Return `connected = not disconnected_roles`. Pure (reads only ctx). Reason `ingredient_not_connected_to_path`.

- [ ] **Step 5: gates + commit** (`feat(3b3c): compiler context + ingredient connectivity (task c2)`).

---

### Task C3: Temporal declaration (representative params)

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Interfaces produced:** `compile_temporal(ctx, plan, template) -> TemporalDeclarationV1`.

- [ ] Tests: an as-of roll-up with no bound as-of column → `temporal_anchor_missing`; a `window`/`window_min` param bound representatively → `WindowSpecV1` populated, `param_binding.is_representative`; conflicting `valid_from`+`valid_to` anchors → `temporal_anchor_ambiguous`; `time_axis_aggregating` true when the representative recipe windows the measure.
- [ ] Implement: representative bind = each `template.params` key → first allowed value (`ParamBindingV1(is_representative=True)`). Parse the window param into `WindowSpecV1` (length/unit/boundary/inclusive; handle `window` and `window_min`). PIT anchor from `RESOLVED_NEED_METADATA[template.id]` temporal roles + which need bound it (`IngredientBindingV1.temporal_role`/`bound_object_ref`). Multi-anchor consistency. Runs FIRST (its `time_axis_aggregating` feeds C4). Reason codes on the declaration.
- [ ] gates + commit (`feat(3b3c): temporal declaration on representative params (task c3)`).

---

### Task C4: Per-ingredient aggregation + additivity validation (algorithmic core)

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Interfaces produced:** `resolve_additivity(ctx, binding) -> tuple[AdditivityClass, AdditivitySource]`; `compile_aggregation(ctx, plan, template, temporal) -> tuple[HopAggregationV1, ...]`.

- [ ] **Step 1: Write failing tests** — the six §4 bank-example outcomes verbatim (additive+undeclared→`sound` SUM; non_additive+declared SUM→`incompatible`; non_additive+undeclared→`undeclared`/`aggregation_strategy_missing`; non_additive+weighted_average+weight-unbound→`inputs_missing`/`aggregation_weight_missing`+`missing_inputs`; semi_additive entity roll-up single-PIT→`sound`; semi_additive across-window→`undeclared`/`semi_additive_temporal_strategy_missing`) + additivity-source precedence (uploaded-column beats concept) + conflict→`additivity_source_conflict` + physical cardinality taken from the REALIZATION not the semantic hop.

- [ ] **Step 2–4: Implement.**
  - `resolve_additivity`: `col = ctx.columns_by_catalog[binding.bound_catalog_source].get(binding.bound_object_ref)`; if `col.additivity` set → `(AdditivityClass(col.additivity), uploaded_column)`; elif `concept(binding.concept).additivity` set → `(that, concept)`; else `(unknown, unknown)`. If uploaded and concept both present AND differ → caller flags `additivity_source_conflict`.
  - `compile_aggregation`: for each hop segment that requires aggregation, `physical_cardinality` = the realization's `declared_cardinality` (looked up in `ctx.realizations_by_catalog` by `realization_ref`); `grouping_keys` = the realization's `to_key_ref`. For each ingredient bound at/upstream of this hop, one `IngredientAggregationV1` via the §4 validation matrix (using `temporal.time_axis_aggregating` for the semi-additive single-PIT vs across-time split; `declared_function` from the recipe — today `None` for all except where a future authoring layer sets it, so most are `undeclared`). Deterministic (sorted by `need_role`).

- [ ] **Step 5: gates + commit** (`feat(3b3c): per-ingredient aggregation + additivity validation (task c4)`).

---

### Task C5: Cross-hop composition

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Interfaces produced:** `check_composition(hop_aggregations) -> CompositionResult(composable: bool, reason_codes)`.

- [ ] Tests: `SUM∘SUM` additive across two hops → composable; a non-additive intermediate (`average_over_period` at hop 1) re-aggregated at hop 2 with no surviving weight → `aggregation_composition_unsupported`; grouping/placement mismatch across a bridge → unsupported. (The `SUM(interest)/SUM(principal)` single-hop counter-case classifies per-ingredient `sound`, NOT here.)
- [ ] Implement the conservative fail-closed guard (spec §4.2): per ingredient across hops, only provable-sound chains pass (`SUM∘SUM`; same-axis re-group with surviving grouping key); anything it can't PROVE composable → `aggregation_composition_unsupported`. No expression algebra.
- [ ] gates + commit (`feat(3b3c): cross-hop composition guard (task c5)`).

---

### Task C6: Physical-read set + universal-safety staging

**Files:** Modify `safety.py` (add the wrapper) + `declarations.py`; Test `test_declarations.py` + `test_safety.py`.

**Interfaces produced:** `safety_of_ref(ctx, catalog_source, object_ref) -> BindingSafety` (wrapper: found `_Col`→`evaluate_binding_safety`; missing→`not_evaluated`); `build_physical_read_set(ctx, plan) -> PhysicalReadSetV1`; `stage_safety(read_set) -> tuple[BindingSafety, tuple[ReasonCode,...]]`.

- [ ] Tests: an unsafe (leakage-anchor) JOIN-KEY column → fold `unsafe` (proves non-ingredient coverage); a bridge key with no loaded `_Col` → `not_evaluated`; a PII column visible under read-scope → NOT unsafe (universal-safety ≠ authorization); a column with two roles (ingredient+join_key) → both roles recorded; fold matrix (any unsafe→unsafe; none unsafe+any not_evaluated→not_evaluated; all safe→safe).
- [ ] Implement: `safety_of_ref` wrapper in `safety.py`. `build_physical_read_set`: union of ingredient `bound_object_ref`s (role `ingredient`, +`join_key`/`temporal_anchor` where the binding's `join_role`/`temporal_role` says so) + each segment's realization `from_key_ref`/`to_key_ref` (role `join_key`) + bridge endpoint refs (role `bridge_key`); merge duplicate object_refs into one multi-role `PhysicalColumnReadV1`; per-column `safety` via `safety_of_ref`; per-column reason codes (the concept's block for unsafe; `safety_evaluation_incomplete` for not_evaluated). `stage_safety` = the fold.
- [ ] gates + commit (`feat(3b3c): physical-read set + universal-safety staging (task c6)`).

---

### Task C7: Freshness + transactional consistency + audit envelope

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Interfaces produced:** `compile_freshness(ctx, plan) -> FreshnessResult(status, reason_codes, stamps, stamp_consistency)`; `audit_envelope(ctx, plan, template, base_envelope) -> PlannerReplayEnvelopeV1`.

- [ ] Tests: a participating catalog with a stale watermark (`now - wm > drift_freshness_sla`) → `participating_catalog_stale`; projection behind (`_checkpoint_seq < drift_head_seq`) → `projection_lagging`; a catalog whose `drift_head_seq` advanced between ctx-start and compile-end → `catalog_mutated_during_compile` + `stamp_consistency=unverifiable`; envelope pins the version set + stamps + `replay_strength=audit_only`.
- [ ] Implement: for each participating catalog (`plan.participating_catalogs`): `wm = drift_watermark`; stale if `wm is None or (now-wm) > ctx.config.drift_freshness_sla`; projection-lag if `_checkpoint_seq(conn,"overlay") < drift_head_seq(conn, src)`; **revalidation:** compare `drift_head_seq` now vs `ctx.catalog_head_seq_at_start[src]` — advanced → `catalog_mutated_during_compile`, `stamp_consistency=unverifiable`. Stamp each catalog (`CatalogStateStampV1`). `audit_envelope` extends the base with the §9 version set + `recipe_content_hash` + `authz_role_claims=ctx.roles` + stamps + `stamp_consistency`, `replay_strength=audit_only`.
- [ ] gates + commit (`feat(3b3c): freshness + transactional consistency + audit envelope (task c7)`).

---

### Task C8: Precedence, `compile_contract`, wire the batched pass + guards

**Files:** Modify `declarations.py`, `plan.py`, `api/routes/contract.py`; Test `test_declarations.py`, `test_plan.py`, route test.

**Interfaces produced:** `compile_contract(ctx, plan, template) -> BindingPlanV1` (enriched); `build_compiler_context(conn, scope, roles, now, budget) -> CompilerContext`; `plan_bindings(..., compile_ctx: CompilerContext | None = None)`; `run_shadow_planner(..., compile_contracts: bool = False)`.

- [ ] **Step 1: Write failing tests** — the §14 acceptance/precedence/identity/determinism/kill-switch/behaviour-neutral cases: precedence picks connectivity/safety as primary with all reason codes preserved+sorted+deduped; compilation does NOT change `physical_plan_id` and the ranked selection is unchanged; compile twice → identical `contract_id`+reasons+stamps; `compile_contracts=False` → every plan `not_compiled`, zero extra reads; budget exhausted → remaining `not_compiled` + `compile_budget_exhausted`; full-suite behaviour-neutral (route response + considered-set API byte-identical).

- [ ] **Step 2–4: Implement.**
  - `compile_contract`: run C2→C7 in the spec §2/§10 order; set `contract_resolution_status` by the §10 precedence; collect+sort+dedup reason codes; mint `contract_id`; set `resolved_at_compilation=ctx.now`; `dataclasses.replace` the plan with all declaration fields + the audit envelope. Only for `path_resolution_status == source_to_target_resolved`; else leave `not_compiled`.
  - `build_compiler_context`: batch-load realizations per authorized catalog, `active_bridges`, `_load_columns` per catalog, `drift_head_seq` per catalog (start snapshot), config, roles, now; carry a mutable-free budget (max plans + deadline as data).
  - `plan_bindings`: accept `compile_ctx`; when present, after assembling candidate_plans, compile each `source_to_target_resolved` plan (respecting budget → `compile_budget_exhausted` marker on the rest); compute the result-level `contract_result_status` roll-up (the selected plan's contract status, or `not_compiled`). When `compile_ctx is None` → unchanged behaviour (all `not_compiled`). The tier-1 `result_status`/`selected_plan_id` decision is UNCHANGED (candidate-local-first).
  - `run_shadow_planner`: accept `compile_contracts`; build ONE `CompilerContext` per run when true; pass to each `plan_bindings`; log the contract roll-up in the summary line.
  - `contract.py`: read `os.environ.get("FEATUREGEN_INTENT_CONTRACT_COMPILE","0")=="1"`, pass as `compile_contracts=` to `run_shadow_planner`. No other route change.

- [ ] **Step 5: Behaviour-neutral proof** — `uv run pytest tests/featuregen/ -q` green (prior total + new); route/considered-set tests byte-identical; a `resolved` tier-1 plan's ingredient-level selection unchanged. gates + commit (`feat(3b3c): precedence + compile_contract + batched wire + guards (task c8)`).

---

## Exit criteria mapping

| Spec (v2) | Task |
|---|---|
| Three axes + `not_compiled` totality | C1 |
| physical_plan_id/contract_id split, time excluded | C1 |
| Ingredient connectivity | C2 |
| Temporal on representative params, typed window | C3 |
| Per-ingredient aggregation, physical cardinality, additivity source+conflict, validate-not-derive | C4 |
| Cross-hop composition (conservative) | C5 |
| Physical-read set, universal-safety, structural not_evaluated, multi-role | C6 |
| Freshness (SLA + projection-lag + revalidation), audit_only envelope, self-contained segments | C1(segments)+C7 |
| Precedence + all-reason-code preservation + determinism | C8 |
| Kill-switch, batched context, budget/timeout, timing | C8 |
| Compute-only, store deferred to 3B.4 | (whole plan) |
| 21 mandatory tests | C2–C8 |

## Self-Review

**Spec coverage:** every v2 section maps to a task (table above); the 21 tests are distributed C2–C8. ✅
**Placeholder scan:** C1 carries complete contract code; C2–C8 carry exact signatures + the validation matrices + the mandatory tests as the behavioural contract (C4/C5 are algorithmic — implementer writes against the tests, deepest review). Deliberate, flagged. ✅
**Type consistency:** `physical_plan_id` renamed once (C1) and threaded through all ranking sites; `contract_id`/`contract_resolution_status` minted in C8 via `make_contract_id` (C1); `CompilerContext` built in C8, consumed by C2–C7; `AdditivityClass`/`AggregationValidation`/`Cardinality` flow C4→C5; `PhysicalReadSetV1` C6→C8. ✅
**Executor notes:** (1) C1's `plan_id`→`physical_plan_id` rename must keep the `"bp_"` material identical (rename only the field, not the hash) so 3B.3a/b plan_ids are unchanged — only the *name* changes. (2) The planner stays pure — the env flag is read ONLY in `contract.py` and passed down; no `os.environ` in `planner/`. (3) C4/C5 are the core; most real recipes classify `undeclared`→`unresolved_aggregation_declaration` by design (Option A) — that is correct, not a bug.
