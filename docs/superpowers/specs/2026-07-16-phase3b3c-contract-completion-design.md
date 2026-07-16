# Phase 3B.3c — Contract Completion (declarations · resolvability · safety staging · replay) Design

**Status:** approved design (2026-07-16). Successor to 3B.3b (cross-catalog assembly, merged `bd3e380`).
**Predecessors consumed:** 3B.3b `assemble_paths` → a `source_to_target_resolved` `BindingPlanV1` (the physical path); 3B.1 `RESOLVED_NEED_METADATA` (temporal roles + source grain); 3A `EntitySemanticPathV1` hops (`cardinality`, `aggregation_required`); concept/template `additivity`; `evaluate_binding_safety`; `drift_watermark` + `CatalogStateStampV1`.

## 1. What 3B.3c is (and is not)

3B.3b proves a **physical path exists** ("you can roll transaction grain up to customer grain via this realization and that governed bridge"). A path is not yet a feature contract. **3B.3c compiles the selected path into a complete, mathematically-honest, replayable feature-contract definition, and states plainly whether it is executable or exactly why not.** Still shadow, still log-only, still behaviour-neutral, no migration — it only makes the shadow plan a *real contract* instead of a route, and feeds 3B.4 an honest resolvability signal.

**Governing invariant (the promise of this phase):**
> A plan is `resolved` only when every grain transition carries an explicit aggregation declaration compatible with the measure's additivity profile on the actual aggregation axis; the temporal semantics are declared; every physically-read column is safety-`safe`; and the participating catalogs were fresh at compile time.

**In scope:** contract completion + resolvability classification for the **single-source-grain** plans 3B.3b produces — aggregation declaration (per hop + composition), temporal declaration, safety staging over *every physically-read column*, freshness/replay evidence, and a deterministic resolution-status precedence. **Detect-and-classify only.**

**Explicitly OUT of scope (the boundary that sizes this phase):**
- **No authoring surfaces.** 3B.3c does NOT build the weighted-average weight-ingredient binding, the per-recipe temporal-strategy declaration, or numerator/denominator recomposition. A recipe that *needs* those stays `unresolved` with a precise reason code; standing those authoring surfaces up (moving recipes unresolved→resolved) is a later enrichment phase. This mirrors the steer: "if the recipe provides no weighting basis, the plan remains unresolved."
- **No multi-grain / multi-branch** ingredient planning (that is 3D).
- **No enforcement / live path** (that is 3C). Nothing 3B.3c writes alters candidate generation or any response.
- **No policy.** Safety here is *universal-safety only* (`_safe_to_bind`: leakage / PII / blocked-attribute). Contextual policy is the separate Governed Feature Policy initiative — no policy leak into this phase.

## 2. Compile pipeline (the dependency order)

Aggregation and temporal semantics cannot be compiled independently — for a semi-additive balance the *temporal* declaration determines whether the aggregation is even valid. The compiler runs one deterministic order (work may be shared internally, but the decision model honours the dependency):

1. **Resolve temporal roles + PIT anchor** — from `RESOLVED_NEED_METADATA` temporal roles + the recipe's window/PIT.
2. **Determine the effective aggregation axes** — each physical hop is an *entity axis* roll-up; the recipe window is the *time axis*.
3. **Compile per-hop aggregation declarations** — one `AggregationStepV1` per semantic hop that requires aggregation.
4. **Validate additivity + strategy composition** — per-hop additivity compatibility, then a plan-level composition check across hops.
5. **Stage safety** — universal-safety over every physically-read column, role preserved.
6. **Establish freshness + replay evidence** — compile-time catalog stamps + the full replay version set.
7. **Derive the final resolution status** — by the fixed precedence (§8), preserving all reason codes.

A single new module `planner/declarations.py` holds the compiler; `plan.py` calls it as a **post-assembly compile pass** over each `source_to_target_resolved` plan (the physics in `assembly.py` stays cleanly separated from contract-compilation).

## 3. Additivity: axis-aware, derived (not newly authored)

Additivity exists today as a scalar per measure concept (`additive` / `semi_additive` / `non_additive` / `n/a`), with good coverage (`monetary_flow`=additive, `monetary_stock`/balances=semi_additive, `monetary_rate`=non_additive, …). What's missing is axis-awareness. We get it **by derivation**, using the standard dimensional-modelling meaning of semi-additive (additive across every dimension *except time*):

```python
@dataclass(frozen=True, slots=True)
class AdditivityProfileV1:
    kind: AdditivityKind                     # additive | semi_additive | non_additive | not_applicable
    additive_across: tuple[AggregationAxisKind, ...]      # e.g. (entity,)
    non_additive_across: tuple[AggregationAxisKind, ...]  # e.g. (time,)
    supported_temporal_strategies: tuple[AggregationFunction, ...]  # () when none declared
    source: ProfileSource                    # derived_convention | authored_override  (future)
```

Derivation from the scalar (the default; per-measure `authored_override` is a future enrichment, and the contract is shaped so it slots in without a redesign):

| scalar | `additive_across` | `non_additive_across` | entity-axis roll-up | time-axis roll-up |
|---|---|---|---|---|
| `additive` | (entity, time) | () | valid (SUM/COUNT/MIN/MAX) | valid |
| `semi_additive` | (entity,) | (time,) | valid at a single PIT (SUM) | requires an explicitly-declared temporal strategy |
| `non_additive` | () | (entity, time) | requires weighting / recomputation | requires temporal rule |
| `n/a` | (entity, time) | () | not an aggregating measure | — |

`AggregationAxisKind = {entity, time}`. Because a 3B.3c **hop** is always an entity-axis roll-up and the recipe **window** is the time axis, this table decides hop validity with no new authoring.

## 4. Aggregation function vocabulary + per-hop declaration

The semantic hop's `AggregationStrategy` is deliberately abstract (`NOT_APPLICABLE` / `RECIPE_DECLARED` — "the actual function is a Phase-3B recipe concern"). 3B.3c introduces the concrete function vocabulary:

```python
class AggregationFunction(StrEnum):
    sum = "sum"; count = "count"; min = "min"; max = "max"           # additive-safe
    last_as_of = "last_as_of"; first_as_of = "first_as_of"          # semi-additive temporal
    average_over_period = "average_over_period"; max_over_period = "max_over_period"
    weighted_average = "weighted_average"                            # non-additive (needs a weight ingredient)
    recomputed_ratio = "recomputed_ratio"                            # non-additive (needs numerator+denominator)
    none = "none"
```

One declaration per hop that requires aggregation:

```python
@dataclass(frozen=True, slots=True)
class AggregationStepV1:
    semantic_hop_index: int
    source_entity: str
    target_entity: str
    cardinality: Cardinality                      # the physical fan-in (many_to_one / many_to_many)
    aggregation_required: bool
    axis: AggregationAxisKind                     # entity (a roll-up hop is always the entity axis)
    proposed_strategy: AggregationFunction | None # None when nothing legal is declarable
    additivity_evaluation: AdditivityEvaluation   # compatible | incompatible | incomplete
    supporting_bindings: tuple[str, ...]          # weight/component object_refs, when required
    reason_codes: tuple[ReasonCode, ...]
```

**Per-hop derivation (fails closed by construction):**
- **additive** measure + fan-in → `sum` (the canonical additive roll-up; always valid). `compatible`.
- **semi-additive** measure, entity-axis hop at a single PIT → `sum`. `compatible`. But if the *recipe also aggregates across the time axis* and declares no temporal strategy → `incomplete`, reason `semi_additive_temporal_strategy_missing`.
- **non-additive** measure → cannot silently `sum`/`avg`. Needs `weighted_average` (with a weight ingredient) or `recomputed_ratio` (with numerator+denominator). The recipe binds neither today → `incomplete`, reason `aggregation_weight_missing` / `aggregation_components_missing`. If a strategy *were* proposed but contradicts the additivity → `incompatible`, reason `aggregation_incompatible_with_additivity`. If no strategy is declarable at all → reason `aggregation_strategy_missing`.

Bank examples the derivation must get right:
```
transaction_amount, fan-in transaction→customer, sum            → compatible
interest_rate,      fan-in transaction→customer, sum            → incompatible (aggregation_incompatible_with_additivity)
interest_rate,      required weighted_average, weight missing    → incomplete   (aggregation_weight_missing)
balance,            entity roll-up at end-of-day, sum            → compatible
balance,            summed across a 90-day window (time axis)     → incomplete   (semi_additive_temporal_strategy_missing)
```

**Plan-level composition check.** Do not flatten the path to one aggregation field. For `transaction → account → customer`, each transition has its own step, and the compiler then asks: *are the hop outputs safely composable?* This catches:
- average-of-average without a surviving weight basis,
- ratio-of-ratios,
- summing snapshot balances across time,
- aggregation applied inconsistently before vs after a bridge.
Failure → plan reason `aggregation_composition_unsupported`. (A path can have every hop individually `compatible` yet fail composition — e.g. `weighted_average` at hop 1 whose weight basis doesn't survive hop 2.)

## 5. Temporal declaration

Compiled from `RESOLVED_NEED_METADATA` temporal roles (`event_time` / `as_of_time` / `valid_from` / …) + the recipe's window/PIT:

```python
@dataclass(frozen=True, slots=True)
class TemporalDeclarationV1:
    pit_anchor: TemporalRole | None       # the effective as-of/event anchor
    anchor_binding: str | None            # bound object_ref supplying it, when required
    window: str | None                    # the recipe's trailing-window rule (verbatim from the template)
    time_axis_aggregating: bool           # does the recipe aggregate across the time axis?
    reason_codes: tuple[ReasonCode, ...]
```

If a roll-up needs an as-of anchor the bound columns can't supply → `unresolved_temporal_declaration`, reason `temporal_anchor_missing`. The temporal declaration is computed **first** (step 1) because it decides whether a semi-additive entity roll-up is valid (single-PIT vs across-time).

## 6. Safety staging over every physically-read column

Safety is **not** limited to feature-ingredient columns. The executable contract would read: measure ingredients, event-time / as-of columns, realization join keys, bridge endpoint keys, aggregation weights, ratio numerator/denominator components, and any filter/partition columns. 3B.3c stages universal-safety over **every bound column the contract requires reading**, preserving the column's role:

```python
class ColumnRole(StrEnum):
    ingredient = "ingredient"; temporal_anchor = "temporal_anchor"
    join_key = "join_key"; bridge_key = "bridge_key"
    aggregation_weight = "aggregation_weight"; aggregation_component = "aggregation_component"

@dataclass(frozen=True, slots=True)
class ColumnSafetyV1:
    object_ref: str; catalog_source: str; role: ColumnRole; safety: BindingSafety  # safe|unsafe|not_evaluated
```

`evaluate_binding_safety(col: _Col)` today accepts an ingredient column; 3B.3c puts a **stable compiler-facing wrapper** around it and applies it to every physical column ref where its semantics apply. Plan-level fold:

```
any unsafe                     → unsafe        → resolution safety_rejected
none unsafe, any not_evaluated → not_evaluated → NOT fully resolved (reason safety_evaluation_incomplete)
all safe                       → safe          → eligible for resolved
```

`not_evaluated` is never treated as `safe` (fail-closed). A column whose safety can't be evaluated leaves the plan not-fully-resolved with `safety_evaluation_incomplete`, not pretend-executable.

## 7. Freshness: two separate concepts (auditability)

A persisted plan must **not** silently mutate its original resolution status as catalog state changes — otherwise historical metrics change depending on when they're queried.

- **Compile-time freshness (part of the immutable record).** At compilation: were all participating catalogs' states available and acceptable (drift watermark present, not staled, within `fresh_within`)? If not → `resolution_status = unresolved_freshness`, reason `freshness_stamp_unavailable` / `participating_catalog_stale`. Each participating catalog is stamped with a `CatalogStateStampV1` (`catalog_source, head_seq, last_completed_at, stamp_kind=drift_watermark`) into the replay envelope. The plan records `resolved_at_compilation`.
- **Replay / observation-time freshness (computed on read, never mutates the record).** Later, compare current catalog state to the plan's recorded stamps → a *separate* field:

```python
class ReplayFreshness(StrEnum):
    current = "current"; drifted = "drifted"; unverifiable = "unverifiable"
```

The original plan stays immutable (`resolved`/`unresolved_*` at compilation) but may be observed `invalidated_by_drift`. 3B.3c produces the compile-time status + the stamps; the replay-time comparison is a read-side helper (also used by 3B.4).

## 8. Resolution status + precedence + reason codes

**Status vocabulary — kept compact; diagnostic precision lives in reason codes.** Additions to `PlanResolutionStatus`:
- `unresolved_aggregation_declaration` (broader than "missing" — the compiler may have *found* a strategy and proved it wrong)
- `unresolved_temporal_declaration`
- `unresolved_freshness`
- (`safety_rejected` already exists)

**Reason-code registry additions** (the diagnostic precision):
```
aggregation_strategy_missing            aggregation_incompatible_with_additivity
aggregation_weight_missing              aggregation_components_missing
aggregation_axis_unsupported            aggregation_composition_unsupported
semi_additive_temporal_strategy_missing
temporal_anchor_missing
safety_evaluation_incomplete
freshness_stamp_unavailable             participating_catalog_stale
```

**Deterministic precedence for a plan with multiple problems** (the *primary* status = the strongest reason it can't be treated as executable; **all** reason codes are preserved so the full set answers "what must be fixed"):
```
1. safety_rejected
2. unresolved_temporal_declaration
3. unresolved_aggregation_declaration
4. unresolved_freshness
5. resolved
```
Example:
```
resolution_status = safety_rejected
reason_codes = [blocked_attribute, aggregation_weight_missing, freshness_stamp_unavailable]
```
Candidate-local-first still holds: a failed *unselected* alternative never affects the selected plan; but every issue on the *selected* contract is preserved.

## 9. Preserve the rejected plan completely (fail-closed ≠ discard)

A plan that fails resolution is retained in full for shadow evaluation. The compiled result carries, at minimum:
```
physical_path_status: source_to_target_resolved      # from 3B.3b, unchanged
resolution_status: unresolved_aggregation_declaration
aggregation_steps: [AggregationStepV1, ...]          # per hop, with the failing hop marked
temporal_declaration: TemporalDeclarationV1
column_safety: [ColumnSafetyV1, ...]
required_strategy: weighted_average
missing_inputs: [principal_amount]
reason_codes: [aggregation_incompatible_with_additivity, ...]
compiler_version + rule versions (see §10)
catalog_state_stamps: [CatalogStateStampV1, ...]
resolved_at_compilation
```
This is exactly the population 3B.4 measures. The 3B.4 hand-off metric this phase is designed to feed: **`physically_resolved_but_contract_unresolved`**, broken down by `aggregation | temporal | freshness | safety`.

## 10. Replay envelope completion

`watermark_only` replay strength is honest **only** if it's understood as drift-correlation, not deterministic re-execution — the historical graph state can't be reconstructed from watermarks alone, and the contract must name that accurately. Beyond 3B.3b's `active_bridge_fact_keys` + `plan_contract_version`, 3B.3c pins the full version set into the replay envelope:
- recipe + template version; `need_metadata_version`; semantic-path / `graph_version`;
- realization-derivation version; active-bridge / `bridge_derivation_version`;
- **aggregation-rule registry version; additivity-rule version; temporal-compilation rule version; safety-evaluator version;**
- planner bounds + ranking version; **per-participating-catalog `CatalogStateStampV1`.**

## 11. Where it runs, and behaviour-neutrality

- New module `src/featuregen/overlay/upload/planner/declarations.py` — the pure, read-only compiler (`compile_contract(conn, plan, template, scope, now) -> BindingPlanV1` enriched, plus the axis/additivity/aggregation/temporal/safety/freshness helpers).
- `plan.py::plan_bindings` calls it as a **post-assembly compile pass** on each `source_to_target_resolved` plan (the assembler is untouched). The compiled declarations + resolution status + stamps ride on the existing `BindingPlanV1` (additive fields; bump `PLAN_CONTRACT_VERSION`; the canonical `make_binding_plan` remains the sole constructor and the new declaration fields participate in plan_id material where they change identity).
- **Log-only / shadow / behaviour-neutral / no migration.** Consumed only by `run_shadow_planner`; the live grounding path and the route are untouched; per-recipe savepoint isolation preserved; F4 preserved (a compiled contract is still a *definition*, never an operational join). Full `tests/featuregen` suite stays green.

## 12. Contracts summary (new / extended)

New (in `contracts.py` unless noted): `AdditivityProfileV1`, `AdditivityKind`, `AggregationAxisKind`, `ProfileSource`, `AggregationFunction`, `AggregationStepV1`, `AdditivityEvaluation`, `TemporalDeclarationV1`, `ColumnRole`, `ColumnSafetyV1`, `ReplayFreshness`; `PlanResolutionStatus` +3 members; `ReasonCode` +~11; `PlannerReplayEnvelopeV1` + the version set + `catalog_state_stamps`; `BindingPlanV1` + `aggregation_steps` / `temporal_declaration` / `column_safety` / `resolved_at_compilation`. New module `planner/declarations.py`. Wrapper around `evaluate_binding_safety` for arbitrary physical columns.

## 13. Task decomposition (for the implementation plan)

- **C1 — contracts + vocabularies + additivity derivation.** All new dataclasses/enums; the derived `AdditivityProfileV1` table (§3) with `authored_override`-ready shape; `PlanResolutionStatus`/`ReasonCode` additions; `PLAN_CONTRACT_VERSION` bump. Pure, unit-tested against the additivity table.
- **C2 — temporal declaration** (`compile_temporal`) — first in the pipeline; `unresolved_temporal_declaration` / `temporal_anchor_missing`.
- **C3 — per-hop aggregation declaration + additivity validation** (`compile_aggregation_steps`) — the derivation of §4 per hop, axis-aware via §3, using the temporal result. All aggregation reason codes.
- **C4 — plan-level composition check** — `aggregation_composition_unsupported`; the avg-of-avg / ratio-of-ratios / snapshot-sum-across-time / inconsistent-across-bridge cases.
- **C5 — safety staging over every physically-read column** (`stage_safety`) — the wrapper + role tagging + the fold; `safety_rejected` / `safety_evaluation_incomplete`.
- **C6 — freshness + replay envelope** — compile-time stamps + `unresolved_freshness`; the read-side `ReplayFreshness` helper; the full version set.
- **C7 — resolution precedence + wire the compile pass into `plan_bindings`** — the §8 precedence, all-reason-code preservation, immutable `resolved_at_compilation`, behaviour-neutral proof, and the 3B.4 hand-off metric shape.

## 14. Mandatory tests (adversarial, DB-backed where needed)

1. Additive measure, single fan-in → `resolved`, hop `sum`, `compatible`.
2. Non-additive rate, fan-in, no weight → `unresolved_aggregation_declaration` / `aggregation_incompatible_with_additivity`; physical path preserved.
3. Non-additive rate, `weighted_average` required, weight ingredient missing → `aggregation_weight_missing`; `missing_inputs` recorded.
4. Semi-additive balance, entity roll-up at a single PIT → `resolved` (`sum`).
5. Semi-additive balance, aggregated across the time window, no temporal strategy → `semi_additive_temporal_strategy_missing`.
6. Two-hop path where each hop is individually compatible but composition fails (avg-of-avg) → `aggregation_composition_unsupported`.
7. Temporal anchor required but unbound → `unresolved_temporal_declaration`.
8. A join-key / bridge-key column is unsafe → `safety_rejected` (proves safety covers non-ingredient columns).
9. A column's safety can't be evaluated → `safety_evaluation_incomplete`, not `safe`.
10. A participating catalog stale at compile → `unresolved_freshness`; stamps recorded.
11. Multi-problem plan → precedence picks `safety_rejected` as primary; all reason codes preserved.
12. Replay-time: drift a participating catalog after compile → the immutable record's status is unchanged; `ReplayFreshness.drifted` on read.
13. Behaviour-neutral: full `tests/featuregen` green; route + live path untouched; a `resolved` tier-1 plan's selection/status unchanged.
14. Determinism: compile twice → identical declarations, reason codes, stamps, plan_id.

## 15. Phase boundaries (recap)

```
3B.3a  find source-grain ingredient candidates
3B.3b  build governed physical source→target paths
3B.3c  prove the selected path is a mathematically, temporally, safely & operationally complete contract  ← this
3B.4   measure completeness, failure distributions, readiness (consumes physically_resolved_but_contract_unresolved)
3D     add multi-grain & multi-branch planning; the aggregation/temporal AUTHORING surfaces
```
