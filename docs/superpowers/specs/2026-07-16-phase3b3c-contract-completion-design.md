# Phase 3B.3c — Contract Resolvability Classifier (declarations · safety · freshness · audit evidence) Design

**Status:** approved design v2 (2026-07-16), reshaped after a 19-finding adversarial spec review (10 Blocker / 7 High / 2 Medium — all accepted). v1's error: it assumed a structured recipe algebra, bound recipe instances, and a durable store that do not exist, and it overloaded `resolved`. v2 re-scopes 3B.3c to a **sound classifier** and moves persistence to 3B.4.
**Predecessors consumed:** 3B.3b `assemble_paths` → a `source_to_target_resolved` `BindingPlanV1` (physical path); 3B.3a tier-1 `IngredientBindingV1`s (each `need_role`, `bound_catalog_source`, `bound_object_ref`, `concept`, `join_role`, `temporal_role`); 3B.2A `CatalogEntityRelationshipV1.declared_cardinality` (physical fan-in); concept/column `additivity`; `_safe_to_bind`; `drift_watermark` + `config.drift_freshness_sla` + the `overlay_checkpoint ≥ head_seq` projection-lag guard.

## 1. What 3B.3c is (v2)

3B.3b proves a **physical path exists**. 3B.3c **classifies whether that path forms a complete, mathematically-honest feature contract, and if not, exactly why** — computing the declarations + safety + freshness + audit evidence *on the returned plan object*. It is a **classifier**, not a compiler that fabricates missing declarations and not a store.

**Governing invariant (contract axis only):**
> A plan's `contract_resolution_status` is `resolved` only when: every ingredient's table is connected to the physical path; every grain transition has a per-ingredient aggregation that is sound for that ingredient's additivity on the actual (physical) aggregation axis; the temporal declaration is bound; every physically-read column is universal-safety `safe`; and every participating catalog was fresh (watermark present, not stale, projection caught up) at compile time.

**Design posture — detect/validate, never fabricate.** The only sound aggregation *derivation* is `additive → SUM`. Every other function must be recipe-declared; today the corpus does not declare per-ingredient functions, so **most cross-catalog plans will classify `unresolved_*` with a precise reason** — that is the honest, intended output, and it is exactly the population 3B.4 measures. 3B.3c does **not** build the authoring surfaces that would move those plans to `resolved`.

**In scope:** the third resolution axis (§2); per-ingredient aggregation validation + composition (§4); ingredient-connectivity (§3); additivity-source resolution (§4.1); temporal validation on *representative* params (§5); universal-safety staging over the physical-read set (§6); compile-time freshness + transactional consistency (§7); identity split (§8); self-contained *audit* evidence (§9); precedence + diagnostic consistency (§10); operational guards (§11).

**Out of scope (explicit):**
- **No durable store, no replay-time reads.** 3B.3c returns the compiled contract on the in-memory plan; the append-only store, the replay-time freshness comparison, and the 3B.4 metric aggregation are **3B.4**.
- **No aggregation/temporal AUTHORING** (weight-ingredient binding, temporal-strategy declaration, numerator/denominator recomposition) and **no bound parameter-instance planning** — a later enrichment phase (moves `unresolved`→`resolved`).
- **No multi-grain / multi-branch** planning (3D); **no enforcement / live path** (3C); **no policy** (Governed Feature Policy). Safety here is universal-safety only.

## 2. Three orthogonal resolution axes (fixes F1, F2)

Do not overload `resolved`. A plan carries three independent statuses:

| axis | field | owner | meaning |
|---|---|---|---|
| ingredient | `resolution_status` | 3B.3a | were the recipe's need columns bound? |
| path | `path_resolution_status` | 3B.3b | does a governed physical source→target path exist? |
| **contract** | **`contract_resolution_status`** (NEW) | **3B.3c** | is the path a complete, sound, safe, fresh executable contract? |

```python
class ContractResolutionStatus(StrEnum):
    resolved = "resolved"
    unresolved_ingredient_connectivity = "unresolved_ingredient_connectivity"
    unresolved_aggregation_declaration = "unresolved_aggregation_declaration"
    unresolved_temporal_declaration = "unresolved_temporal_declaration"
    unresolved_safety_evaluation = "unresolved_safety_evaluation"   # F2: safety-incomplete has a home
    safety_rejected = "safety_rejected"
    unresolved_freshness = "unresolved_freshness"
    not_compiled = "not_compiled"   # tier-1 / non-source_to_target plans the compiler doesn't touch
```

The compile pass runs only on `source_to_target_resolved` plans; every other plan gets `contract_resolution_status = not_compiled` (so the axis is total and never silently absent). The top-level `BindingPlanningResultV1` gains a **contract roll-up** (`contract_result_status` + the selected plan's contract status) *separate* from its ingredient-level `result_status` — the two never alias.

## 3. Ingredient connectivity (fixes F4)

3B.3a binds each need independently and may land ingredients on **different tables**; 3B.3b's path only guarantees the *source-key* table rolls up to the target. So a plan can be `source_to_target_resolved` yet have an ingredient whose table the path never touches. 3B.3c checks connectivity **first among the contract checks**:

> For every `IngredientBindingV1`, `table_of(bound_object_ref)` must be either on the physical path (a realization/bridge segment's endpoint table in that catalog) or co-located with a path table (same catalog + same table as the source-key binding). Otherwise → `unresolved_ingredient_connectivity`, reason `ingredient_not_connected_to_path`, evidence = the disconnected `need_role` + its table + the path tables.

Multi-branch planning that *would* connect them is 3D; here it is an explicit, evidenced rejection.

## 4. Per-ingredient aggregation (fixes F3, F5, F6, F8)

Aggregation is **per (hop × ingredient)**, not one-per-hop. At a single fan-in hop, `average_rate = SUM(interest_amount)/SUM(principal_amount)` needs `SUM` on two different ingredients and a division — one plan-wide op cannot express that.

```python
@dataclass(frozen=True, slots=True)
class IngredientAggregationV1:
    need_role: str                       # the ingredient this stage aggregates
    bound_object_ref: str
    additivity: AdditivityClass          # resolved per §4.1 (with provenance)
    additivity_source: AdditivitySource  # uploaded_column | concept | unknown  (F6 provenance)
    physical_cardinality: Cardinality    # the REALIZATION fan-in at this hop (F8), not the semantic hop
    axis: AggregationAxisKind            # entity | time
    declared_function: AggregationFunction | None   # from the recipe; None when undeclared
    validation: AggregationValidation    # sound | incompatible | undeclared | inputs_missing
    reason_codes: tuple[ReasonCode, ...]

@dataclass(frozen=True, slots=True)
class HopAggregationV1:
    semantic_hop_index: int
    from_entity: str; to_entity: str
    physical_cardinality: Cardinality    # from CatalogEntityRelationshipV1.declared_cardinality
    grouping_keys: tuple[str, ...]       # the realization's to-side key(s) — the GROUP BY
    ingredient_stages: tuple[IngredientAggregationV1, ...]
```

**Validation per ingredient stage (never fabricate a function):**
- `additive` ingredient, fan-in → the one sound derivation `SUM` is `sound`. (A recipe-declared `count/min/max` is also validated `sound`; an undeclared function still resolves via the `SUM` default only because additivity guarantees SUM — `count/min/max` are *different features* and require the recipe to ask for them.)
- `semi_additive`, entity-axis hop, **single-PIT** temporal (§5) → `SUM` is `sound`. Same measure with the recipe aggregating across the **time axis** and no declared temporal strategy → `undeclared`, reason `semi_additive_temporal_strategy_missing`.
- `non_additive` → no sound default. Requires a recipe-declared rule (weighted-average with a bound weight, ratio recomputation with bound numerator+denominator, take-latest, …); today undeclared → `undeclared`, reason `aggregation_strategy_missing`. A declared function that contradicts additivity → `incompatible`, reason `aggregation_incompatible_with_additivity`. A declared function whose inputs aren't bound → `inputs_missing`, reason `aggregation_weight_missing` / `aggregation_components_missing`, with `missing_inputs` recorded.
- `n/a` (non-aggregating measure) that nonetheless sits on a fan-in hop → `incompatible`, reason `aggregation_axis_unsupported`.

Bank examples (verbatim outcomes the classifier must produce):
```
transaction_amount (additive), fan-in, undeclared         → sound      (SUM default)
interest_rate (non_additive),  fan-in, declared SUM        → incompatible (aggregation_incompatible_with_additivity)
interest_rate (non_additive),  fan-in, undeclared          → undeclared   (aggregation_strategy_missing)
interest_rate (non_additive),  declared weighted_average, weight unbound → inputs_missing (aggregation_weight_missing)
balance (semi_additive), entity roll-up at end-of-day       → sound      (SUM)
balance (semi_additive), summed across a 90-day window       → undeclared   (semi_additive_temporal_strategy_missing)
```

### 4.1 Additivity source + provenance (fixes F6)
Three additivity sources exist and can disagree: uploaded column additivity (`canonical`/`graph_node`), concept additivity, template *output* additivity. Per-ingredient precedence:
1. **uploaded-column additivity** if the bound column asserts one → `AdditivitySource.uploaded_column`;
2. else **concept additivity** of the bound column's concept → `AdditivitySource.concept`;
3. else `unknown` → `AdditivityClass.unknown`, which is **not** treated as additive — it forces `undeclared`/`unresolved`, never a silent `SUM`.
`template.additivity` is the **output** target — used only in the composition check (§4.2), never as an ingredient's input additivity. A column whose upload and concept additivity *conflict* → `unresolved_aggregation_declaration`, reason `additivity_source_conflict`, both values in provenance.

### 4.2 Cross-hop composition (fixes F5, F8)
The recipe has **no structured output algebra** in the corpus (Option A), so composition is a **conservative, fail-closed cross-hop guard over the per-ingredient stages** — not an expression evaluator. For each ingredient across a multi-hop path it asks: *is the sound aggregation at hop k re-aggregable by the aggregation at hop k+1?* The provable-sound chains: `SUM∘SUM` (additive), and a same-axis re-group whose grouping key survives. Everything it cannot **prove** composable → `unresolved_aggregation_declaration`, reason `aggregation_composition_unsupported` — the average-of-average case (a non-additive intermediate, e.g. an `average_over_period` output at hop k, re-aggregated at hop k+1 with no surviving weight), a semi-additive balance whose grouping/placement differs before vs after a bridge, or any declared function pair with no declared composition rule. (A path with every stage individually `sound` can still fail composition; conversely `SUM(interest)/SUM(principal)` at a *single* hop is a valid weighted rate and is a per-ingredient concern, not a composition failure.)

## 5. Temporal validation on representative params (fixes F7)

`plan_bindings` receives an **unbound** `Template` (params are allowed-value tuples). 3B.3c compiles against a **representative default-bound instance** (each param's first allowed value) and records that it did so; full parameter-instance planning is out of scope. The window is a **typed** contract, not a bare string:

```python
@dataclass(frozen=True, slots=True)
class TemporalDeclarationV1:
    pit_anchor: TemporalRole | None       # effective as-of/event anchor from RESOLVED_NEED_METADATA
    anchor_binding: str | None            # bound object_ref supplying it, when required
    window: WindowSpecV1 | None           # typed: {length, unit, boundary, inclusive} — NOT str
    param_binding: ParamBindingV1         # {name: chosen_representative_value}, + is_representative=True
    time_axis_aggregating: bool           # does the representative recipe aggregate the measure over time?
    reason_codes: tuple[ReasonCode, ...]
```

Handles both `window` and `window_min` corpus params. A roll-up needing an as-of anchor the bound columns can't supply → `unresolved_temporal_declaration`, reason `temporal_anchor_missing`. Multiple temporal anchors / bitemporal (`valid_from`+`valid_to`) are validated for consistency → reason `temporal_anchor_ambiguous` when they conflict. Temporal is computed **before** aggregation (it decides semi-additive single-PIT vs across-time).

## 6. Universal-safety over the physical-read set (fixes F13, F14, F2)

Safety covers **every physically-read column**, not just ingredients, with **multi-role** provenance (a column may be both `ingredient` and `join_key`). It is **universal-safety only** — leakage-anchor + protected/special — **explicitly separate from PII/authorization** (read-scope), which 3B.3c does not re-gate.

```python
class ColumnRole(StrEnum):
    ingredient; temporal_anchor; join_key; bridge_key
    aggregation_weight; aggregation_component; filter; partition

@dataclass(frozen=True, slots=True)
class PhysicalColumnReadV1:
    object_ref: str; catalog_source: str
    roles: tuple[ColumnRole, ...]         # multi-role
    safety: BindingSafety                 # safe | unsafe | not_evaluated
    reason_codes: tuple[ReasonCode, ...]

@dataclass(frozen=True, slots=True)
class PhysicalReadSetV1:
    columns: tuple[PhysicalColumnReadV1, ...]   # the immutable inventory the contract would read
```

The read-set is derived from ingredient bindings **+ the path segments' realization/bridge key refs** (segments must expose these — §9). `evaluate_binding_safety(col: _Col)` returns only `safe|unsafe` (unknown-concept → `safe`); it is applied through a **stable compiler-facing wrapper**. `not_evaluated` is **structural** — the wrapper returns it when a physical column cannot be resolved to a `_Col` (e.g. a bare bridge/join key with no loaded metadata), never as an `evaluate_binding_safety` result. Fold:
```
any unsafe                     → unsafe        → contract safety_rejected            (reason: the column's block, e.g. leakage_anchor / protected_attribute)
none unsafe, any not_evaluated → not_evaluated → contract unresolved_safety_evaluation (reason: safety_evaluation_incomplete)
all safe                       → safe          → eligible for resolved
```
`not_evaluated` is never treated as `safe`.

## 7. Freshness + transactional consistency (fixes F10, F15)

**Compile-time freshness (part of the plan record):** each participating catalog must have a drift watermark present, be within `config.drift_freshness_sla` (the existing policy — not a hardcoded 24h; its version pinned in the envelope), and have its **overlay projection caught up** (`overlay_checkpoint ≥ drift head_seq`, the same guard `resolve.py` enforces). Any failure → `unresolved_freshness`, reason `freshness_stamp_unavailable` / `participating_catalog_stale` / `projection_lagging`. Catalogs with no watermark are already dropped by scope resolution — 3B.3c treats a *participating* catalog that later lacks a watermark as stale, not silently absent.

**Transactional consistency (F10):** the scope stamps + `active_bridges` are read at the top of `plan_bindings`, but realization/graph/bridge reads happen later; under READ COMMITTED concurrent ingestion could desync them. 3B.3c **revalidates every catalog stamp at compile completion** — if any participating catalog's drift `head_seq` advanced during the compile, the plan's replay evidence is marked `unverifiable` and the plan classifies `unresolved_freshness` (reason `catalog_mutated_during_compile`) rather than emitting an envelope inconsistent with the reads. (A repeatable-read planning context is the stronger alternative and is noted as a 3C upgrade; revalidation is the shadow-safe choice.)

**Replay-time freshness is 3B.4**, not here — see §9.

## 8. Identity: physical_plan_id vs contract_id (fixes F11, F12)

Compilation must **not** mutate the id the ranker used. Split:
- `physical_plan_id` — minted by assembly (3B.3b) over the physical path; the ranking tie-break; **immutable through compilation**. (Rename the current `plan_id` role; ranking + candidate-local-first unchanged.)
- `contract_id` — minted by 3B.3c over the **declaration material only**: `physical_plan_id · per-ingredient (need_role, resolved additivity, declared_function, validation) · temporal declaration · composition outcome · contract_resolution_status · the rule-registry versions`. **Excludes** all timestamps, freshness stamps, and `resolved_at_compilation`, so identical compiles are byte-identical and freshness changes never change the id.

Canonical material is defined explicitly and its components are **sorted + deduped** (§10) so serialization order can't perturb it. `resolved_at_compilation` is a typed `datetime`, carried as evidence, **never hashed**.

## 9. Self-contained AUDIT evidence; store deferred to 3B.4 (fixes F9, F16)

`run_shadow_planner` today only logs a one-line summary and persists nothing. 3B.3c therefore **computes** the compiled contract + evidence onto the returned `BindingPlanV1`; it does **not** persist. The durable append-only store, the 3B.4 metric aggregation, and the replay-time freshness comparison are **3B.4's** contract.

Replay strength is honestly **`audit_only`** (not `watermark_only`): watermarks permit drift *correlation*, never deterministic re-execution, since historical graph state can't be reconstructed. For the evidence to be self-contained for *audit*, the assembled path segments must carry (added this phase): the **semantic `relationship_id` + `relationship_version`** per hop, and the **realization key refs / bridge fact_keys** already present. The replay envelope pins the full version set — recipe + template version + a **recipe content-hash**, `need_metadata_version`, `graph_version`, realization-derivation version, `bridge_derivation_version`, aggregation-rule / additivity-rule / temporal-rule / safety-evaluator versions, `drift_freshness_sla` policy version, planner bounds + ranking version, **authz role claims**, and the per-participating-catalog `CatalogStateStampV1` — with a `stamp_consistency` flag (`consistent` | `unverifiable`, §7).

The replay-time comparison (a pure `ReplayFreshness ∈ {current, drifted, unverifiable}` over a stored plan's stamps vs current state) is **defined as a 3B.4 function**; 3B.3c ships only the compile-time stamps + the immutable record.

## 10. Precedence + diagnostic consistency (fixes F17, F2)

**Deterministic contract-status precedence** (primary = strongest reason it can't execute; **all** reason codes preserved, sorted + deduped):
```
1. unresolved_ingredient_connectivity
2. safety_rejected
3. unresolved_safety_evaluation
4. unresolved_temporal_declaration
5. unresolved_aggregation_declaration
6. unresolved_freshness
7. resolved
```
(Connectivity first: a plan whose ingredients aren't even connected isn't a contract to reason about further. Safety before the declaration checks: a leakage/protected read is the hardest block.)

**Diagnostics:** every reason code is registered in `ReasonCode` (including one for the safety block — reuse/register `blocked_attribute`/`leakage_anchor_read` explicitly rather than referencing an unregistered name). The retained-plan fields (`required_strategy`, `missing_inputs`, provenance) appear in the contract summary too. Reason codes are **canonically ordered (enum order) and deduped** so `contract_id` is stable. Candidate-local-first holds: a failed *unselected* alternative never affects the selected plan, but every issue on the selected contract is preserved.

## 11. Operational guards (fixes F18)

"Behaviour-neutral" means *response-neutral*; latency/txn-duration is real behaviour and the compile pass adds per-plan reads.
- **Dedicated kill switch** for the compile pass (a flag separate from the shadow-planner entry), default off; when off, plans keep `contract_resolution_status = not_compiled`.
- **Batched compiler context** — load realizations + `active_bridges` + catalog stamps **once per run**, pass an immutable context to every plan compile (no per-plan re-query).
- **Per-run compile budget + timeout** — bound total plans compiled + wall-time; on exceed, remaining plans stay `not_compiled` with a recorded `compile_budget_exhausted` marker (never a silent skip).
- **Timing metrics** emitted for the run (compiles, wall-time, budget hits).

## 12. Contracts summary

New: `ContractResolutionStatus`, `IngredientAggregationV1`, `HopAggregationV1`, `AggregationFunction`, `AggregationValidation`, `AdditivityClass`, `AdditivitySource`, `AggregationAxisKind`, `TemporalDeclarationV1`, `WindowSpecV1`, `ParamBindingV1`, `ColumnRole`, `PhysicalColumnReadV1`, `PhysicalReadSetV1`, `ReplayFreshness` (defined, used by 3B.4). Extended: `BindingPlanV1` + `contract_resolution_status` / `hop_aggregations` / `temporal_declaration` / `physical_read_set` / `contract_id` / `resolved_at_compilation` (physical_plan_id = renamed existing); `BindingPathSegmentV1` + `relationship_id` / `relationship_version`; `PlannerReplayEnvelopeV1` + the §9 version set + stamps + `stamp_consistency`; `BindingPlanningResultV1` + the contract roll-up; `ReasonCode` +~15; `PLAN_CONTRACT_VERSION` bump. New module `planner/declarations.py`. Safety wrapper around `evaluate_binding_safety`. New flag for the compile pass.

## 13. Task decomposition

- **C1 — axes + vocabularies + additivity resolution.** `ContractResolutionStatus`; `AggregationFunction`/`AggregationValidation`/`AdditivityClass`/`AdditivitySource`/axis enums; per-ingredient additivity-source precedence (§4.1) + `additivity_source_conflict`; `ReasonCode` additions (all registered); `PLAN_CONTRACT_VERSION` bump; the `physical_plan_id`/`contract_id` split + canonical `contract_id` material (timestamps excluded). Pure/unit.
- **C2 — ingredient connectivity** (`check_connectivity`) — `unresolved_ingredient_connectivity`; DB-backed (multi-table ingredients).
- **C3 — temporal on representative params** (`compile_temporal`) — typed `WindowSpecV1`, `window`/`window_min`, multi-anchor/bitemporal; runs first.
- **C4 — per-ingredient aggregation + additivity validation** (`compile_aggregation`) — physical cardinality from the realization, per-ingredient stages, the §4 validation matrix (never fabricate).
- **C5 — composition check** (`check_composition`) — `aggregation_composition_unsupported`; avg-of-avg / ratio-of-ratios / snapshot-sum-across-time / cross-bridge placement.
- **C6 — physical-read set + universal-safety staging** (`stage_safety`) — read-set from bindings + segment key refs; multi-role; wrapper; structural `not_evaluated`; `safety_rejected` / `unresolved_safety_evaluation`.
- **C7 — freshness + transactional consistency + audit envelope** — `config.drift_freshness_sla` + projection-lag guard + end-of-compile stamp revalidation; self-contained segments (`relationship_id`/version); `audit_only` strength; `stamp_consistency`.
- **C8 — precedence + roll-up + wire the batched compile pass into `plan_bindings` + operational guards** — §10 precedence, all-reason-code preservation, immutable `resolved_at_compilation`, kill-switch flag, batched context, budget/timeout, timing metrics, the result-level contract roll-up, behaviour-neutral proof.

## 14. Mandatory tests (adversarial; expanded per F19)

1. Additive ingredient, fan-in, undeclared → `resolved` (SUM default). 2. Non-additive rate + declared SUM → `aggregation_incompatible_with_additivity`. 3. Non-additive rate, undeclared → `aggregation_strategy_missing` (NOT "incompatible"). 4. Non-additive rate, `weighted_average` declared, weight unbound → `aggregation_weight_missing` + `missing_inputs`. 5. Semi-additive balance, entity roll-up at single PIT → `resolved`. 6. Semi-additive balance across the window → `semi_additive_temporal_strategy_missing`. 7. **Cross-hop average-of-average** (a two-hop path where hop-1's declared aggregation output is non-additive — e.g. `average_over_period` — and hop-2 re-aggregates it with no surviving weight) → `aggregation_composition_unsupported`. Counter-case in the same test: `SUM(interest)/SUM(principal)` at a *single* hop classifies per-ingredient `sound`, NOT a composition failure. 8. **Disconnected same-grain tables** (amount on t1, timestamp on t2) → `ingredient_not_connected_to_path`. 9. **Physical/semantic cardinality disagreement** (realization is 1:N where the semantic hop is N:1) → uses the physical cardinality. 10. Unbound temporal anchor → `unresolved_temporal_declaration`; **`window_min` param** bound representatively; **multiple/bitemporal anchors** → `temporal_anchor_ambiguous`. 11. **Unsafe join-key / bridge-key** column → `safety_rejected` (proves non-ingredient coverage). 12. **Structural `not_evaluated`** (bare key, no `_Col`) → `unresolved_safety_evaluation`, not `safe`. 13. **PII column permitted by read-scope** → NOT safety_rejected (universal-safety ≠ authorization). 14. **Additivity source conflict** (upload vs concept) → `additivity_source_conflict`. 15. Stale participating catalog → `unresolved_freshness`; **projection lag** (`checkpoint < head_seq`) → `projection_lagging`. 16. **Concurrent catalog mutation during compile** (head_seq advances) → `catalog_mutated_during_compile`, `stamp_consistency=unverifiable`. 17. Multi-problem plan → precedence picks connectivity/safety as primary; all reason codes preserved + sorted/deduped. 18. **Identity**: compilation does not change `physical_plan_id` and the ranked selection is unchanged; `contract_id` excludes `resolved_at_compilation` (compile twice → identical `contract_id`, reasons, stamps). 19. **Deterministic reason ordering** under input shuffle. 20. Kill-switch off → all plans `not_compiled`, zero extra reads. 21. Behaviour-neutral: full `tests/featuregen` green; route + live path untouched; a `resolved` tier-1 plan's ingredient-level selection/status unchanged; **considered-set API byte-identical**.

## 15. Phase boundaries

```
3B.3a  find source-grain ingredient candidates
3B.3b  build governed physical source→target paths
3B.3c  CLASSIFY whether the selected path is a sound/temporal/safe/fresh contract (this) — compute-only, honest-unresolved
3B.4   PERSIST + measure (durable store, replay-time freshness, physically_resolved_but_contract_unresolved)
3D+    aggregation/temporal AUTHORING surfaces + bound-instance planning + multi-grain/multi-branch
```
