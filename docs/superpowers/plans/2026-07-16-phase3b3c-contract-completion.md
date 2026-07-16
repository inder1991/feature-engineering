# Phase 3B.3c — Contract Resolvability Classifier Implementation Plan (v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`). **C4 (per-ingredient aggregation) + C5 (composition) are the algorithmic core.** Shadow/log-only/compute-only; the durable store is 3B.4.
>
> **v2 folds a 21-finding plan review (10 Blocker/8 High/3 Medium), all accepted.** The load-bearing fixes: split `PHYSICAL_PLAN_VERSION` (frozen) from the contract version so the rename can't change physical IDs; per-plan `audit_envelope`/`contract_*` fields; a separate identity-bearing `declaration_status` (freshness excluded from `contract_id`); an injectable aggregation-declaration registry (Option A has no authoring source); bridge-rollup cardinality by construction; connectivity returns a placement map; `compile_freshness(conn, …)` as the explicit impure boundary; consistency via `realization_fingerprint` + bridge fingerprint (not `head_seq` alone); a mutable `CompileBudget` owned by `run_shadow_planner`; `derive_need_metadata(template)` (not the static registry).

**Goal:** Classify each 3B.3b `source_to_target_resolved` plan as a complete feature contract — or exactly why not — computing per-ingredient aggregation, temporal, universal-safety, and freshness declarations onto the in-memory plan. Validate, never fabricate.

**Architecture:** New `planner/declarations.py` (batched immutable `CompilerContext` + pure per-plan checks + one impure freshness boundary) invoked as a post-assembly pass from `plan.py::plan_bindings`. Three orthogonal status axes; a `physical_plan_id`/`contract_id` split; a run-owned mutable `CompileBudget`.

**Tech Stack:** Python 3.11 (frozen dataclasses, StrEnum), PostgreSQL (read-only), pytest (`db` fixture). `uv run pytest/ruff/mypy`. **Spec:** `docs/superpowers/specs/2026-07-16-phase3b3c-contract-completion-design.md` (v2).

## Global Constraints

- **Compute-only / shadow / behaviour-neutral / no migration / no store.** Compiler returns enriched plan objects; nothing persisted (3B.4). Consumed only by `run_shadow_planner`; live path + `api/routes/contract.py` response untouched; per-recipe savepoint preserved; F4 preserved; full `tests/featuregen` green.
- **Validate, never fabricate.** The permitted auto-derivations (versioned by `AGGREGATION_RULE_VERSION`) are EXACTLY two: `additive` fan-in → `SUM`; `semi_additive` **entity-axis single-PIT** → `SUM`. Every other function must come from the injected declaration registry (empty in prod → `undeclared` → honest `unresolved_*`).
- **Three orthogonal axes, unchanged predecessors.** `resolution_status` (ingredient) + `path_resolution_status` (path) UNCHANGED. NEW `contract_resolution_status` + `declaration_status`; non-`source_to_target_resolved` plans → `not_compiled`. Contract diagnostics use `contract_primary_reason_code`/`contract_reason_codes` — never the ingredient/path axes' reason fields.
- **Identity.** `physical_plan_id` (renamed from `plan_id`) uses a FROZEN `PHYSICAL_PLAN_VERSION` and is immutable through compilation (the ranking tie-break). `contract_id` hashes `declaration_status` + declaration reason codes + per-ingredient declarations + temporal signature + rule versions — **excludes** freshness status/reasons, timestamps, and stamps. All reason codes canonically ordered (enum order) + deduped.
- **Universal-safety only** (leakage_anchor + protected/special), reason-bearing, separate from PII/authorization; structural `not_evaluated` (no `_Col`) ≠ `safe`.
- **Freshness** is the one impure step: `config.drift_freshness_sla`, the `overlay_checkpoint ≥ drift_head_seq` guard, and **fingerprint** revalidation (`realization_fingerprint` per catalog + a bridge fact-set fingerprint) taken at scope-start and rechecked at compile-end.
- **Batched + bounded + kill-switched.** One immutable `CompilerContext` per run; a mutable `CompileBudget` (remaining count + deadline) owned by `run_shadow_planner`; an env kill-switch read ONLY in the route (planner stays pure — no `os.environ` in `planner/`).
- **Convention:** frozen `@dataclass(frozen=True, slots=True)`; lowercase-snake `StrEnum`; ruff (`collections.abc`, E402); no import cycle (`declarations.py` must not import the route). Branch `feature/phase3b3c-contract-completion`; harness default trailer.

## Reused interfaces (verified)
- `IngredientBindingV1(recipe_id, need_role, concept, required_grains, join_role, temporal_role, bound_catalog_source, bound_object_ref, actual_source_grain, binding_quality, safety, reason_codes)`.
- `BindingPathSegmentV1(segment_kind, catalog_source, from_entity, to_entity, realization_ref, bridge_fact_key, cardinality, direction, reason_codes)` — C1 adds `relationship_id`/`relationship_version` (sourced from the semantic hop `EntityRelationshipRefV1` at assembly emission). Bridge-rollup segments have `bridge_fact_key` and NO `realization_ref`.
- `CatalogEntityRelationshipV1(realization_id, relationship_id, catalog_source, from_object_ref, from_object_grain, to_object_ref, to_object_grain, from_key_ref, from_key_entity, to_key_ref, to_key_entity, declared_cardinality, authority, status, reversed_authoring)`; `derive_catalog_realizations(conn, catalog).realizations`. `to_key_ref` = GROUP-BY key.
- `ActiveBridgeV1(fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref)`; `active_bridges(conn)`.
- `Cardinality` = `one_to_one|one_to_many|many_to_one|many_to_many`.
- `_Col(catalog_source, object_ref, table, column, data_type, is_grain, is_as_of, concept, entity, additivity, sensitivity, currency)` + `_load_columns(conn, catalog_source, roles)`; `_safe_to_bind(col) -> bool`; `concept(name).additivity`. `table_of(object_ref)`.
- Freshness: `drift_watermark(conn, src)`, `drift_head_seq(conn, src)` (`overlay.catalog_changes`); `_checkpoint_seq(conn,"overlay")` (`projections.runner`); `realization_fingerprint(conn, src)` (`catalog_realizations`); `OverlayConfig.drift_freshness_sla` (loader in `overlay/config.py`).
- `derive_need_metadata(template) -> tuple[ResolvedNeedMetadataV1,...]` (pure — works for injected/custom templates; NOT the static `RESOLVED_NEED_METADATA[id]`). `template.params` (allowed-value tuples). `template.additivity` (OUTPUT).
- `make_binding_plan(*, recipe_id, target_entity, catalog_source, ingredient_bindings, path_segments, resolution_status, path_resolution_status, primary_reason_code, reason_codes, safety, preference_rank, preference_reasons, candidate_role)`; physical-id material currently ends `|{PLANNER_VERSION}|{PLAN_CONTRACT_VERSION}` (contracts.py:351). `ReplayStrength.audit_only` exists.
- Flag pattern (route): `os.environ.get("FEATUREGEN_INTENT_CONTRACT_COMPILE","0")=="1"`.

## File Structure

| File | Responsibility |
|---|---|
| `planner/contracts.py` (MODIFY) — C1 | axes/vocabularies; version split; `physical_plan_id`/`contract_id`/`declaration_status`; per-plan `audit_envelope` + `contract_*`; provenance; extend segment/envelope/result |
| `planner/assembly.py`,`enumerate.py`,`order.py` (MODIFY) — C1 | `plan_id`→`physical_plan_id` ranking keys; assembly emits `relationship_id`/version |
| `planner/safety.py` (MODIFY) — C6 | reason-bearing column-safety evaluator + arbitrary-column wrapper |
| `planner/declarations.py` (CREATE) — C2–C7 | `CompilerContext`, `CompileBudget`, the checks, `compile_contract`, `revalidate_freshness` |
| `planner/plan.py` (MODIFY) — C8 | batched compile pass + contract selection roll-up + budget |
| `planner/shadow.py` (MODIFY) — C8 | own the `CompileBudget`, build the context per run, thread `compile_contracts` |
| `api/routes/contract.py` (MODIFY) — C8 | read kill-switch flag, pass `compile_contracts`; `.env.example` doc |
| Tests | `test_declarations.py` (new) + updates to `test_contracts/plan/assembly/enumerate/order/safety` |

---

### Task C1: Axes, versions, identity, per-plan contract fields

**Files:** Modify `contracts.py`, `assembly.py`, `enumerate.py`, `order.py`; Test `test_contracts.py` (+ rename fallout).

**Produces:** `ContractResolutionStatus`, `DeclarationStatus`; `AggregationFunction`, `AggregationValidation`, `AdditivityClass`, `AdditivitySource`, `AggregationAxisKind`, `ColumnRole`, `ReplayFreshness`, `StampConsistency`; `AdditivityProvenanceV1`, `IngredientAggregationV1`, `HopAggregationV1`, `TemporalDeclarationV1`, `WindowSpecV1`, `ParamBindingV1`, `PhysicalColumnReadV1`, `PhysicalReadSetV1`; `to_additivity_class(str|None)`; `make_contract_id(...)`; `PHYSICAL_PLAN_VERSION`, rule versions; extended `BindingPlanV1`/`BindingPathSegmentV1`/`PlannerReplayEnvelopeV1`/`BindingPlanningResultV1`; `ReasonCode` additions; `PLAN_CONTRACT_VERSION` bump.

- [ ] **Step 1: Write failing tests** (`test_contracts.py`):

```python
def test_version_split_keeps_physical_id_stable():
    # PHYSICAL_PLAN_VERSION freezes the physical material; PLAN_CONTRACT_VERSION may bump freely.
    assert c.PHYSICAL_PLAN_VERSION == "3b3b.1.0.0"
    assert c.PLAN_CONTRACT_VERSION == "3b3c.1.0.0"
    seg = c.BindingPathSegmentV1(c.SegmentKind.direct_catalog, "core")
    p = c.make_binding_plan(recipe_id="t", target_entity="cust", catalog_source="core",
        ingredient_bindings=(), path_segments=(seg,),
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.selected)
    # this exact id was recorded from HEAD before the version split — proves the rename+bump didn't move it
    assert p.physical_plan_id == _PINNED_3B3B_ID   # captured in the test from `make_binding_plan` pre-change
    assert p.contract_resolution_status is c.ContractResolutionStatus.not_compiled
    assert p.declaration_status is c.DeclarationStatus.not_compiled
    assert p.audit_envelope is None

def test_contract_id_excludes_freshness_and_time():
    p = _compiled_plan(declaration_status=c.DeclarationStatus.resolved,
                       contract_resolution_status=c.ContractResolutionStatus.unresolved_freshness,
                       contract_reason_codes=(c.ReasonCode.participating_catalog_stale,))
    a = c.make_contract_id(p, resolved_at_compilation=_dt(2026,1,1))
    # same DECLARATIONS, different freshness outcome + time → SAME contract_id
    p2 = _replace(p, contract_resolution_status=c.ContractResolutionStatus.resolved,
                  contract_reason_codes=())
    b = c.make_contract_id(p2, resolved_at_compilation=_dt(2099,9,9))
    assert a == b and a.startswith("cc_")

def test_new_enums_and_reason_codes():
    for r in ("ingredient_not_connected_to_path","aggregation_strategy_missing",
              "aggregation_incompatible_with_additivity","aggregation_weight_missing",
              "aggregation_components_missing","aggregation_axis_unsupported",
              "aggregation_composition_unsupported","semi_additive_temporal_strategy_missing",
              "temporal_anchor_missing","temporal_anchor_ambiguous","additivity_source_conflict",
              "physical_cardinality_unavailable","safety_evaluation_incomplete","leakage_anchor_read",
              "protected_attribute_read","freshness_stamp_unavailable","participating_catalog_stale",
              "projection_lagging","catalog_mutated_during_compile","compile_budget_exhausted"):
        assert r in {x.value for x in c.ReasonCode}
    assert c.to_additivity_class("n/a") is c.AdditivityClass.not_applicable
    assert c.to_additivity_class("garbage") is c.AdditivityClass.unknown   # never raises
    assert c.to_additivity_class("semi_additive") is c.AdditivityClass.semi_additive
```

- [ ] **Step 2: Run red.**

- [ ] **Step 3: Implement** in `contracts.py`:
  - **Version split (F1):** add `PHYSICAL_PLAN_VERSION = "3b3b.1.0.0"`; change `make_binding_plan`'s material tail from `|{PLAN_CONTRACT_VERSION}` to `|{PHYSICAL_PLAN_VERSION}` (byte-identical to today, since today's `PLAN_CONTRACT_VERSION` is `"3b3b.1.0.0"`). Then bump `PLAN_CONTRACT_VERSION = "3b3c.1.0.0"`. Add `AGGREGATION_RULE_VERSION`, `ADDITIVITY_RULE_VERSION`, `TEMPORAL_RULE_VERSION`, `SAFETY_EVALUATOR_VERSION`, `DRIFT_FRESHNESS_SLA_VERSION`, `PLANNER_BOUNDS_VERSION`, `RANKING_VERSION` (each `"1.0.0"`).
  - **Enums:** `DeclarationStatus` (`not_compiled, resolved, unresolved_ingredient_connectivity, unresolved_aggregation_declaration, unresolved_temporal_declaration, unresolved_safety_evaluation, safety_rejected`) — the FRESHNESS-FREE outcome (identity-bearing); `ContractResolutionStatus` = `DeclarationStatus` members **plus** `unresolved_freshness` (the full observed status). Plus `AggregationFunction`, `AggregationValidation` (`sound,incompatible,undeclared,inputs_missing`), `AdditivityClass` (`additive,semi_additive,non_additive,not_applicable,unknown`), `AdditivitySource` (`uploaded_column,concept,unknown`), `AggregationAxisKind` (`entity,time`), `ColumnRole` (8, incl. `filter,partition`), `ReplayFreshness`, `StampConsistency`.
  - **`to_additivity_class(s)`** — normalize: `None/""/"n/a"→not_applicable`; the four known → their class; else `unknown`. Never raises.
  - **Dataclasses:** `AdditivityProvenanceV1(uploaded_value:str|None, concept_value:str|None, selected:AdditivityClass, source:AdditivitySource, conflict:bool)`; `IngredientAggregationV1(need_role, bound_object_ref, additivity:AdditivityClass, provenance:AdditivityProvenanceV1, physical_cardinality:Cardinality|None, axis, declared_function:AggregationFunction|None, validation:AggregationValidation, missing_inputs:tuple[str,...], reason_codes)`; `HopAggregationV1(semantic_hop_index, segment_index, from_entity, to_entity, execution_catalog:str, execution_table:str, physical_cardinality:Cardinality|None, cardinality_source:str, grouping_keys, ingredient_stages)`; `TemporalDeclarationV1(pit_anchor, anchor_binding, window:WindowSpecV1|None, param_binding:ParamBindingV1, time_axis_aggregating, reason_codes)`; `WindowSpecV1(length:int|None, unit:str|None, boundary:str|None, inclusive:bool)`; `ParamBindingV1(values:tuple[tuple[str,str],...], is_representative:bool)`; `PhysicalColumnReadV1(object_ref, catalog_source, roles:tuple[ColumnRole,...], safety:BindingSafety, reason_codes)`; `PhysicalReadSetV1(columns)`.
  - **`BindingPathSegmentV1`** += `relationship_id:str|None=None`, `relationship_version:str|None=None`.
  - **`BindingPlanV1`**: rename `plan_id`→`physical_plan_id` (first field). Add: `contract_id:str|None=None`, `declaration_status:DeclarationStatus=DeclarationStatus.not_compiled`, `contract_resolution_status:ContractResolutionStatus=ContractResolutionStatus.not_compiled`, `contract_primary_reason_code:ReasonCode|None=None`, `contract_reason_codes:tuple[ReasonCode,...]=()`, `hop_aggregations:tuple[HopAggregationV1,...]=()`, `temporal_declaration:TemporalDeclarationV1|None=None`, `physical_read_set:PhysicalReadSetV1|None=None`, `audit_envelope:PlannerReplayEnvelopeV1|None=None`, `resolved_at_compilation:datetime|None=None`. In `make_binding_plan` mint `physical_plan_id=` (material uses `PHYSICAL_PLAN_VERSION`).
  - **`make_contract_id(plan, *, resolved_at_compilation)`**: material = `physical_plan_id · declaration_status · '|'.join(sorted-by-enum-order+deduped declaration reason codes) · per-ingredient sorted (need_role, additivity, declared_function or '', validation) · temporal signature (pit_anchor, window, time_axis_aggregating) · AGGREGATION_RULE_VERSION · ADDITIVITY_RULE_VERSION · TEMPORAL_RULE_VERSION · SAFETY_EVALUATOR_VERSION · PLAN_CONTRACT_VERSION`. **Excludes** `contract_resolution_status`'s freshness delta, freshness reason codes, stamps, and `resolved_at_compilation`. `"cc_"+sha256[:16]`. (Note: hash `declaration_status` + DECLARATION reason codes only; the freshness observation never enters.)
  - **`PlannerReplayEnvelopeV1`** += `aggregation_rule_version, additivity_rule_version, temporal_rule_version, safety_evaluator_version, drift_freshness_sla_version, planner_bounds_version, ranking_version, authz_role_claims:tuple[str,...], recipe_content_hash:str, catalog_state_stamps:tuple[CatalogStateStampV1,...], stamp_consistency:StampConsistency`. Compiled plans set `replay_strength=ReplayStrength.audit_only`.
  - **`BindingPlanningResultV1`** += `contract_result_status:ContractResolutionStatus=not_compiled`, `selected_contract_physical_plan_id:str|None=None`, `selected_contract_id:str|None=None`.
  - **`ReasonCode`** += the 20 in the test.

- [ ] **Step 4: Rename ranking sites** — `assembly.py:559`, `enumerate.py:98`, `order.py:37`, `plan.py:89/138/219-220` (`.plan_id`→`.physical_plan_id`); `selected_plan_id`/`planner_matched_plan_id`/diff hold the physical id. Update `test_assembly/enumerate/order/plan`. **Capture `_PINNED_3B3B_ID`** by running `make_binding_plan` on the pre-change code (or compute the sha) so the stability test is real.

- [ ] **Step 5: gates + commit** (`feat(3b3c): contract/declaration axes + version split + identity + per-plan fields (task c1)`).

---

### Task C2: `CompilerContext` + ingredient connectivity with placement

**Files:** Create `declarations.py`; Test `test_declarations.py`.

**Produces:** `CompilerContext` (immutable, no conn); `CompileBudget` (mutable, run-owned); `check_connectivity(ctx, plan) -> ConnectivityResult(connected, disconnected_roles, placement)` where `placement: dict[str, PathPositionV1]` maps `need_role → (segment_index, catalog, table)`.

- [ ] Tests: two ingredients on different transaction-grain tables, path rolls up only one → `connected False` + the other role; all ingredients on path/co-located tables → `connected True` + a placement entry per role (segment index of the hop whose table holds it). *(DB-backed; reuse `CanonicalRow`+`build_graph`.)*
- [ ] Implement: `CompilerContext(realizations_by_catalog, active_bridges, columns_by_catalog:dict[str,dict[str,_Col]], catalog_fingerprint_at_start:dict[str,str], bridge_fingerprint_at_start:str, catalog_stamps, config, roles, now, agg_declarations:AggregationDeclarationRegistry)` — all frozen; `agg_declarations` is an injectable frozen mapping `(recipe_id, need_role) -> AggregationFunction` (empty in prod). `CompileBudget(remaining:int, deadline:datetime)` is a plain mutable dataclass (NOT frozen), owned by `run_shadow_planner`. `check_connectivity`: path tables from segment endpoints (realization to/from refs via `realization_ref`; bridge endpoint refs via `bridge_fact_key` against `ctx.active_bridges`) + source-key table; co-located = same `(catalog,table)`. Build `placement[need_role] = PathPositionV1(segment_index, catalog, table)` for the hop whose table holds the ingredient (source-key table = the pre-first-hop position, segment_index 0). `ingredient_not_connected_to_path` for the rest.

- [ ] gates + commit (`feat(3b3c): compiler context + connectivity with placement (task c2)`).

---

### Task C3: Temporal declaration (representative params)

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Produces:** `compile_temporal(ctx, plan, template) -> TemporalDeclarationV1`.

- [ ] Tests: as-of roll-up, no bound as-of column → `temporal_anchor_missing`; `window`/`window_min` bound representatively → `WindowSpecV1` populated; **two incompatible event anchors** → `temporal_anchor_ambiguous`; **`valid_from`+`valid_to` together → a VALID bitemporal interval** (NOT ambiguous); `time_axis_aggregating` true when the representative recipe windows the measure. Uses an **injected/custom template** (not in the static registry) to prove `derive_need_metadata` is used.
- [ ] Implement: `metas = derive_need_metadata(template)` (F17 — pure, works for custom templates). Representative bind: each `template.params` key → first allowed value, `ParamBindingV1(is_representative=True)`. Parse window/window_min → `WindowSpecV1`. PIT anchor from the metas' temporal roles + which binding supplies it. Bitemporal (`valid_from`+`valid_to`) = interval, valid; ambiguity ONLY when two anchors are genuinely incompatible (e.g. two distinct event-time anchors). Runs FIRST.
- [ ] gates + commit (`feat(3b3c): temporal declaration on representative params (task c3)`).

---

### Task C4: Per-ingredient aggregation + additivity (algorithmic core)

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Produces:** `resolve_additivity(ctx, binding) -> AdditivityProvenanceV1`; `hop_physical_cardinality(ctx, segment) -> tuple[Cardinality|None, str, tuple[str,...]]` (cardinality, source, grouping_keys); `compile_aggregation(ctx, plan, template, temporal, placement) -> tuple[HopAggregationV1, ...]`.

- [ ] **Step 1: tests** — the six §4 outcomes verbatim (additive+undeclared→`sound` SUM; non_additive+**registry-declared** SUM→`incompatible`; non_additive+undeclared→`undeclared`/`aggregation_strategy_missing`; non_additive+registry-declared `weighted_average`+weight-unbound→`inputs_missing`/`aggregation_weight_missing`+`missing_inputs`; semi_additive entity single-PIT→`sound`; semi_additive across-window→`undeclared`/`semi_additive_temporal_strategy_missing`); additivity precedence (uploaded beats concept) + `additivity_source_conflict` with BOTH values in provenance; **realization** cardinality used for a realized hop; **bridge-rollup hop** (no `realization_ref`) → `physical_cardinality=many_to_one` by construction (grouping key = bridge target grain key), `cardinality_source="bridge_construction"`; a hop with neither realization nor bridge evidence → `physical_cardinality_unavailable`. Declared functions come from `ctx.agg_declarations` (F5).
- [ ] **Step 2–4: Implement.**
  - `resolve_additivity`: `col = ctx.columns_by_catalog[binding.bound_catalog_source].get(binding.bound_object_ref)`; `uploaded = to_additivity_class(col.additivity) if col else None`; `concept_add = to_additivity_class(concept(binding.concept).additivity)`; precedence uploaded→concept→unknown; `conflict = uploaded not in (None, not_applicable) and uploaded != concept_add and concept_add not in (unknown, not_applicable)`. Return `AdditivityProvenanceV1(...)`.
  - `hop_physical_cardinality`: realized hop (`realization_ref`) → the realization's `declared_cardinality`, `to_key_ref` grouping, source `"realization"`; bridge-rollup (`bridge_fact_key`, no `realization_ref`) → `many_to_one` by the FK→grain-key construction, grouping = the bridge's target grain key, source `"bridge_construction"`; else `(None, "unavailable", ())` → `physical_cardinality_unavailable`.
  - `compile_aggregation`: per hop, one `IngredientAggregationV1` per ingredient placed at/upstream of that hop (from C2's `placement`); function from `ctx.agg_declarations.get((recipe_id, need_role))` (else None); validation via the §4 matrix (additive→SUM sound; semi_additive entity single-PIT via `temporal.time_axis_aggregating`; non_additive declared-vs-additivity; `physical_cardinality is None` → the hop's stages carry `physical_cardinality_unavailable`). Deterministic (sorted by need_role).

- [ ] gates + commit (`feat(3b3c): per-ingredient aggregation + additivity + physical/bridge cardinality (task c4)`).

---

### Task C5: Cross-hop composition

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Produces:** `check_composition(hop_aggregations, output_additivity: AdditivityClass) -> CompositionResult(composable, reason_codes)`.

- [ ] Tests: `SUM∘SUM` additive across two hops → composable; a non-additive intermediate (registry-declared `average_over_period` at hop 1) re-aggregated at hop 2, no surviving weight → `aggregation_composition_unsupported`; grouping/placement mismatch across a bridge (execution_catalog/table differ, grouping key doesn't survive) → unsupported; the single-hop `SUM(interest)/SUM(principal)` counter-case is per-ingredient `sound`, NOT here.
- [ ] Implement the conservative fail-closed guard (spec §4.2) using `HopAggregationV1.segment_index`/`execution_catalog`/`execution_table`/`grouping_keys` (from C4) + the passed `output_additivity` (F13): only provable-sound chains pass (`SUM∘SUM`; same-axis re-group with surviving grouping key); everything else → `aggregation_composition_unsupported`. No expression algebra.
- [ ] gates + commit (`feat(3b3c): cross-hop composition guard (task c5)`).

---

### Task C6: Physical-read set + reason-bearing universal-safety

**Files:** Modify `safety.py` + `declarations.py`; Test `test_declarations.py` + `test_safety.py`.

**Produces:** `evaluate_column_safety(col: _Col) -> tuple[BindingSafety, ReasonCode|None]` (reason-bearing, in `safety.py`); `safety_of_ref(ctx, catalog, object_ref) -> tuple[BindingSafety, ReasonCode|None]` (found→evaluate; missing→`not_evaluated`,`safety_evaluation_incomplete`); `build_physical_read_set(ctx, plan) -> PhysicalReadSetV1`; `stage_safety(read_set) -> tuple[BindingSafety, tuple[ReasonCode,...]]`.

- [ ] Tests: leakage-anchor JOIN-KEY column → `unsafe`,`leakage_anchor_read` (proves non-ingredient coverage + reason); protected/special ingredient → `unsafe`,`protected_attribute_read`; bridge key with no `_Col` → `not_evaluated`,`safety_evaluation_incomplete`; PII column visible under read-scope → NOT unsafe (universal ≠ authorization); a column with two roles (ingredient+join_key) merged; the fold matrix.
- [ ] Implement: refactor `safety.py` so `evaluate_column_safety` returns the REASON (leakage_anchor vs protected/special) without duplicating policy — derive from `concept(col.concept)` the same predicates `_safe_to_bind` uses, mapped to `leakage_anchor_read`/`protected_attribute_read`; `evaluate_binding_safety` stays a thin bool wrapper. `safety_of_ref` looks up `ctx.columns_by_catalog`. `build_physical_read_set`: ingredient refs (roles from `join_role`/`temporal_role`) + segment realization `from_key_ref`/`to_key_ref` (join_key) + bridge endpoint refs (bridge_key); merge duplicates → multi-role `PhysicalColumnReadV1`; per-column safety+reasons. `stage_safety` fold: any unsafe→`safety_rejected`; else any not_evaluated→`unresolved_safety_evaluation`; else safe.
- [ ] gates + commit (`feat(3b3c): reason-bearing safety over the physical-read set (task c6)`).

---

### Task C7: Freshness (impure boundary) + consistency + audit envelope

**Files:** Modify `declarations.py`; Test `test_declarations.py`.

**Produces:** `revalidate_freshness(conn, ctx, plan) -> FreshnessResult(status, reason_codes, stamps, stamp_consistency)` (**explicit conn — the impure boundary**, F8); `audit_envelope(ctx, plan, template, base_envelope, stamps, stamp_consistency) -> PlannerReplayEnvelopeV1`.

- [ ] Tests: stale watermark (`now-wm > drift_freshness_sla`) → `participating_catalog_stale`; projection behind (`_checkpoint_seq < drift_head_seq`) → `projection_lagging`; **a catalog whose `realization_fingerprint` OR the bridge fingerprint changed between ctx-start and compile-end** → `catalog_mutated_during_compile` + `stamp_consistency=unverifiable` (F9/F11 — proves head_seq alone isn't the guard; simulate a graph rebuild that doesn't move head_seq); envelope pins the full version set + `recipe_content_hash` (canonical) + sorted/deduped `authz_role_claims` + stamps + `replay_strength=audit_only`.
- [ ] Implement: for each `plan.participating_catalogs`: stale via `drift_watermark` + `ctx.config.drift_freshness_sla`; projection-lag via `_checkpoint_seq(conn,"overlay") < drift_head_seq(conn,src)`; **consistency** via `realization_fingerprint(conn,src) != ctx.catalog_fingerprint_at_start[src]` OR `bridge_fingerprint(conn) != ctx.bridge_fingerprint_at_start` → `unverifiable`. Stamp each catalog. `audit_envelope` extends base with the §9 version set, a canonical `recipe_content_hash` (stable sorted serialization of the template's identity fields), `authz_role_claims=tuple(sorted(set(ctx.roles)))`, stamps, `stamp_consistency`, `replay_strength=audit_only`.
- [ ] gates + commit (`feat(3b3c): freshness impure boundary + fingerprint consistency + audit envelope (task c7)`).

---

### Task C8: Precedence, `compile_contract`, batched wire, budget, selection, guards

**Files:** Modify `declarations.py`, `plan.py`, `shadow.py`, `api/routes/contract.py`; Test `test_declarations.py`, `test_plan.py`, route/shadow tests, `.env.example`.

**Produces:** `compile_contract(conn, ctx, plan, template) -> BindingPlanV1`; `build_compiler_context(conn, scope, roles, now) -> CompilerContext`; `plan_bindings(..., compile_ctx, budget)`; `run_shadow_planner(..., compile_contracts)`.

- [ ] **Step 1: tests** — precedence primary = connectivity→safety_rejected→safety_eval→temporal→aggregation→freshness→resolved, all `contract_reason_codes` preserved+sorted+deduped; **declaration_status vs contract_resolution_status** (a declaration-resolved but stale plan → `declaration_status=resolved`, `contract_resolution_status=unresolved_freshness`, and `contract_id` unchanged vs the fresh compile); compilation does NOT change `physical_plan_id` and the ranked selection is unchanged; **result-level contract selection** across the compiled (source_to_target) plans → `selected_contract_physical_plan_id`/`selected_contract_id`/`contract_result_status` set to the best compiled plan (tier-1 stays `not_compiled` and is NOT chosen); **budget shared across recipes** (two recipes, budget=1 → second recipe's plans `not_compiled`+`compile_budget_exhausted`); `compile_contracts=False` → all `not_compiled`, zero extra reads; timing metric emitted; behaviour-neutral (route/considered-set byte-identical).
- [ ] **Step 2–4: Implement.**
  - `compile_contract(conn, ctx, plan, template)`: run C2 connectivity → C3 temporal → C4 aggregation → C5 composition → C6 safety (pure over ctx) → C7 `revalidate_freshness(conn,...)` (impure). Derive `declaration_status` from {connectivity, aggregation/composition, temporal, safety} by the §10 precedence MINUS freshness; then `contract_resolution_status` = fold `declaration_status` with the freshness result. `contract_primary_reason_code` + sorted/deduped `contract_reason_codes`. `contract_id = make_contract_id(...)` (declaration-only). Set `resolved_at_compilation=ctx.now`, `audit_envelope`. Only for `source_to_target_resolved`.
  - `build_compiler_context`: batch realizations per authorized catalog, `active_bridges`, `_load_columns` per catalog, `realization_fingerprint` per catalog + bridge fingerprint (snapshot), config, roles, now, `agg_declarations` (empty in prod). Immutable.
  - `plan_bindings(..., compile_ctx=None, budget=None)`: when `compile_ctx` present, after building candidate_plans, compile each `source_to_target_resolved` plan while `budget.remaining > 0 and ctx.now < budget.deadline`, decrement `budget.remaining`; over budget → leave `not_compiled` + `compile_budget_exhausted`. Compute the **contract selection roll-up** (F3): rank the compiled plans by the existing physical ranking key restricted to source_to_target plans → best → `selected_contract_*` + `contract_result_status`. Tier-1 `result_status`/`selected_plan_id` UNCHANGED (candidate-local-first).
  - `run_shadow_planner(..., compile_contracts=False)`: build ONE `CompilerContext` when true; own a mutable `CompileBudget(remaining=MAX_COMPILES_PER_RUN, deadline=now+COMPILE_BUDGET)`; pass `compile_ctx`+`budget` into each `plan_bindings` (budget persists across recipes — F10); emit a run timing metric (structured log fields: compiles, wall-time-from-`now`, budget_hit); include the contract roll-up in the summary line.
  - `contract.py`: read `FEATUREGEN_INTENT_CONTRACT_COMPILE`, pass `compile_contracts=`. Add the flag to `.env.example` + a one-line doc.

- [ ] **Step 5: behaviour-neutral proof** — `uv run pytest tests/featuregen/ -q` green; route/considered-set byte-identical; a `resolved` tier-1 plan's ingredient-level selection unchanged. gates + commit (`feat(3b3c): precedence + compile_contract + batched wire + budget + selection + guards (task c8)`).

---

## Exit criteria mapping

| Finding(s) | Task |
|---|---|
| F1 version split; F6 contract diagnostic fields; F7 declaration_status/contract_id; F2 per-plan audit_envelope | C1 |
| F12 connectivity placement | C2 |
| F17 derive_need_metadata + bitemporal | C3 |
| F4 bridge cardinality; F5 declaration registry; F14 additivity normalize; F15 provenance; F16 derivation matrix | C4 |
| F13 composition stage model + output additivity | C5 |
| F18 reason-bearing safety codes | C6 |
| F8 impure freshness boundary; F9/F11 fingerprint consistency; F19 audit canonicalization | C7 |
| F3 contract selection roll-up; F10 run-owned budget; F20 timing + flag doc | C8 |
| F21 cross-task tests | C2–C8 |

## Self-Review

**Coverage:** all 21 findings mapped (table). Predecessor axes untouched; physical IDs provably stable (pinned-id test). ✅
**Placeholder scan:** C1 complete; C2–C8 exact signatures + matrices + tests as the behavioural contract (C4/C5 algorithmic — deepest review). ✅
**Type consistency:** `physical_plan_id` renamed once + threaded; `CompilerContext` (no conn) built C8, consumed C2–C7; freshness alone takes `conn` (C7); `CompileBudget` mutable, run-owned (C8); `placement` (C2)→C4; `AdditivityProvenanceV1` C4; `HopAggregationV1` stage fields C4→C5; `declaration_status`(identity) vs `contract_resolution_status`(observed) split honoured in `make_contract_id`. ✅
**Executor notes:** (1) The physical-id material swap `PLAN_CONTRACT_VERSION→PHYSICAL_PLAN_VERSION` must be byte-identical (today's value is `"3b3b.1.0.0"`); pin an id in the test to prove it. (2) Planner stays pure except `revalidate_freshness(conn,…)`; the env flag lives only in the route. (3) Most real recipes classify `undeclared → unresolved_aggregation_declaration` (Option A) — correct, not a bug. (4) `CompileBudget` is the ONLY mutable object and is owned by `run_shadow_planner`, never inside the immutable context.
