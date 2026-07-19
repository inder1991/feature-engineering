# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) — Design

**Status:** approved for planning (post 6th review — contract + reuse model grounded against code)
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Branch:** implement in a **separate clean worktree** off `origin/main` (`d90d457`+); do not rebase the shared dirty tree.
**Migration:** `1006` (`1005_llm_dispatch_provenance` is taken at `d90d457`; re-confirm at build time).

## 1. Purpose + the reuse model (grounded)

Combine operands originating in different catalogs into one governed computation at one exact physical grain — a capability `plan_bindings` lacks. **Reuse model:** each operand's path to a common landing is an ordinary single-source `BindingPlanV1` produced by the existing frontier, and the existing per-path compiler validates it. A adds four things on top: endpoint governance, physical-landing convergence, final combination, and one union freshness check.

Precise reuse (corrected against code):
- **Path production:** drive `assemble_paths` + `semantic_rollup_paths(source_entity, target_entity)` from a **hand-built `_Position(source_grain_entity, catalog_source, table_of(object_ref))`** taken from the operand's `GovernedSourceBindingV1`. **Not** `_assemble_rollups` — that derives the source from a `SOURCE_ENTITY_KEY` *need* and returns empty for a single-measure injected template (`planner/assembly.py:65-99`).
- **Per-path compile:** `compile_temporal(ctx, plan, template)` then `compile_aggregation(ctx, plan, template, temporal, placement)` where `placement = check_connectivity(ctx, plan).placement` (`declarations.py:167,220`). These accept **injected** templates (F17).
- **Compiler context:** A builds its **own** `CompilerContext` — production `build_compiler_context` hard-codes `agg_declarations={}` (`declarations.py:1046`) and read-scopes columns by `roles` (out-of-scope columns fail safety `:716`). A's context supplies `agg_declarations` keyed by `(injected_recipe_id, need_role)` and `roles` covering every operand/anchor/key column.
- **Landing:** the frontier does **not** emit the landing (`_mint` sets `catalog_source = source`, discards the landing `_Position`; `BindingPlanV1` has no landing field — `assembly.py:389-398`). A **re-derives** the landing `(catalog, table_ref)` from the plan's `path_segments` (as `check_connectivity` computes execution tables, `declarations.py:191-211`) and takes `grain_key_refs` from the landing endpoint's grain fact. A never modifies `assemble_paths` (protects the §12 golden test).

Shadow-only, synthetic-gold-driven. A trusts its typed input for concept + source-side authority; it proves the *assembly* is governed.

## 2. Authority basis (no key fact; crossings are the plan's governed segments)

`FactType` is closed: `grain | availability_time | scd_effective_dating | approved_join | entity_bridge | policy_tag` (`overlay/_types.py`). Consequences, grounded:
- A table's **grain** is proven by a VERIFIED **`grain` fact**, read via `resolve_fact(conn, adapter, table_ref, "grain", now=now)` — grain is a data fact so the sealed-config upload **adapter** is consulted (`resolve.py:210`); `ref` is the **table** `CatalogObjectRef`; value is `{columns, is_unique}` with **short** column names (`facts.py:56-69`).
- **There is no key fact.** Source key columns come from the grain fact's `columns`; crossings are proven by the frontier's own governed segments — **intra-catalog realizations** carry `APPROVED_JOIN`(VERIFIED)/`DECLARED_JOIN`(file-declared)/`INFERRED_JOIN` authority (`catalog_realizations.py:_join_edges` is intra-catalog by construction; `assembly.py:309-310`), and **cross-catalog crossings** are VERIFIED **`entity_bridge`** facts (`bridge_projection.active_bridges` — single-column endpoints, no cardinality field, no `confirmed_event_id` selected).
- A does **not** define a bespoke crossing fact. Crossing governance is exactly what the frontier already enforces (VERIFIED bridges + governed realization authorities). A's *added* governance is **endpoint grain-fact revalidation** (every hop endpoint has a VERIFIED grain fact), because the frontier's grain/key derivation is advisory (`object_grain`/`key_entity` read `graph_node.concept`).

## 3. Contracts (`@dataclass(frozen=True, slots=True)` + `StrEnum`)

### 3.1 Governed endpoint
```
GovernedEndpointV1: catalog, table_ref, grain_key_refs: tuple[str, ...], grain_fact_key
```
`grain_key_refs` = the grain fact's short `columns` qualified to `table_ref` and validated for membership against `graph_node.column_name` (exists + non-null for columns: `0945_graph.sql`, `0997_graph_structural_constraints.sql`). Keyed on **`grain_fact_key`** (deterministic from ref+type), never a per-event id (finding #8). Missing/unverified grain fact → not a `GovernedEndpointV1` (endpoint ungoverned).

### 3.2 Input — `MultiSourcePlannerIntentV1`
- `target_entity`, `operands: tuple[OperandSlotV1, ...]`, `final_expression: FinalExpressionV1`, `operation_policy_version`.

`OperandSlotV1`: `slot_id`, `semantic_role: SemanticRole`, `catalog_source`, `object_ref`, `authoritative_concept`, `path_strategy: PathStrategyV1`, `source_binding: GovernedSourceBindingV1`.

`GovernedSourceBindingV1`: `source_grain_entity`, `source_grain_key_refs: tuple[str, ...]` (composite, qualified), `grain_fact_key`. **No key fact.**

`PathStrategyV1`: `aggregation: PathAggregation`, `output_type`, `output_additivity`, `external_type_required: bool`, **`ordering_anchor_concept: str | None`** — the *concept* of a temporal anchor (accepted `pit_role`) that A injects as a **second bound temporal need** so the reused `compile_temporal` can validate `take_latest` (the anchor must be a bound need, not a bare string — `declarations.py:273-292,450-474`). Required iff `aggregation == take_latest`.

`FinalExpressionV1`: `operation: FinalOperation`, `ordered_slot_ids: tuple[str, ...]`, `time_slot_id: str | None` (references a `TIME` slot), `window: str | None`, `output_additivity`.

### 3.3 Output
`MultiSourcePlanningResultV1` mirrors `BindingPlanningResultV1`: `run_id`, `target_entity`, `candidate_plans`, `selected_plan_id`, `result_status`, `primary_reason_code`, `reason_codes`, `bounding: MultiSourceBoundingMetricsV1`, `replay_envelope: MultiSourceReplayEnvelopeV1`, contract axis (`contract_result_status`, `selected_contract_plan_id`, `selected_contract_id`).

`MultiSourceBindingPlanV1` (own compile result): `plan_id`, `physical_landing: PhysicalLandingV1`, `operand_paths: tuple[OperandPathV1, ...]`, `final_expression`, `physical_read_set: PhysicalReadSetV1` (reuse), `resolution_status`, `reason_codes`, `contract_result_status`, `contract_id`, `declaration_evidence` (per-path `HopAggregationV1`/`TemporalDeclarationV1` + final verdict), `contract_input_hash`, `contract_output_hash`.

`OperandPathV1`: `slot_id`, `semantic_role`, `catalog_source`, `object_ref`, **`binding_plan: BindingPlanV1`** (the frontier's governed plan — its `path_segments` ARE the governed crossings: realization authorities + VERIFIED bridge segments), `governed_endpoints: tuple[GovernedEndpointV1, ...]` (source + each intermediate + landing, revalidated), `path_strategy`, `pit_treatment`. No bespoke `GovernedCrossingV1` (unpopulatable — approved_join is intra-catalog, entity_bridge carries no cardinality/composite-keys/confirmed_event_id). Crossing cardinality is by-construction from the frontier (`declarations.py:364-376`); `confirmed_event_id` for audit is re-queried from `entity_bridge_edge` at store time, never widening `ActiveBridgeV1`.

`PhysicalLandingV1`: `catalog`, `table_ref`, `grain_key_refs: tuple[str, ...]` (composite; join on every key).

`MultiSourceBoundingMetricsV1`: `paths_per_operand_truncated`, `operand_combinations_truncated`, `states_truncated`, `landing_ambiguous`, `total_states_expanded`.

`MultiSourceReplayEnvelopeV1` (finding #8, #11): input fingerprint over target_entity + operand pins + `source_grain_key_refs` + governed endpoint **`grain_fact_key`s** + bridge **`fact_key`s** + versions — all deterministic; no `recipe_id`, no per-event ids.

## 4. Operation → slot → path-strategy matrix + aggregation mapping (findings #1,#3,#10)

`PathAggregation → AggregationFunction` mapping (the reused `compile_aggregation` keys off `AggregationFunction` = `sum|count|min|max|weighted_average|ratio_recompute|take_latest`, `contracts.py:155`): `sum→sum`, `min→min`, `max→max`, `take_latest→take_latest` (natively, soundly validated); `count→count`, `count_distinct→count` (order-safe rule); `avg→` validated via its additive-decomposable components (both additive) with the precise identity carried by A's strategy-hash. **`stddev` is not resolvable initially** (no additive/order-safe analog; validating it as SUM-sound would mislabel) → `UNSUPPORTED_PATH_AGGREGATION` (fail-closed; deferred). The reused validator validates only *coarsely*; A's multi-source `contract_id` (which hashes `path_strategy`) carries the precise operation identity.

Matrix (total, closed; exact role→slot validation — not set membership):

| `final_expression` | slots (role) | allowed per-slot `PathAggregation` |
|---|---|---|
| `IDENTITY` | 1 `MEASURE` | `AVG SUM MIN MAX` |
| `COUNT` | 1 `COUNTED` | `COUNT` |
| `COUNT_DISTINCT` | 1 `COUNTED` | `COUNT_DISTINCT` |
| `RECENCY` | 1 `TIME` | `TAKE_LATEST` (+`ordering_anchor_concept`) |
| `TREND` | 1 `MEASURE` + 1 `TIME` (+`window`) | measure `AVG SUM`; time `TAKE_LATEST` |
| `RATIO` (ordered) | 1 `NUMERATOR` + 1 `DENOMINATOR` | each `AVG SUM MIN MAX TAKE_LATEST` |
| `DIFFERENCE` (ordered) | 1 `MINUEND` + 1 `SUBTRAHEND` | each `AVG SUM MIN MAX TAKE_LATEST` |

Validation: exact multiset of roles; each `ordered_slot_id`/`time_slot_id` references a real, correctly-roled slot; no duplicate `slot_id`; numerator≠denominator (minuend≠subtrahend); window present iff required; `take_latest` ⇒ `ordering_anchor_concept` present. **TIME-slot `take_latest`** (RECENCY/TREND) is validated by **A's own** ordering check — `compile_aggregation` stages MEASURE join_role only (`declarations.py:565`), so it never sees a TIME operand.

## 5. Assembly steps

Per intent (own savepoint): (1) `validate_operation_shape` (§4). (2) Per operand build an injected `Template` — one MEASURE/COUNTED/TIME need for the pinned column + (for `take_latest`) a **second** temporal need for `ordering_anchor_concept` — and run `assemble_paths`/`semantic_rollup_paths` from the hand-built source `_Position` (§1) to enumerate governed paths (bounded `MAX_PATHS_PER_OPERAND`). (3) **Endpoint governance:** revalidate each path endpoint via `GovernedEndpointV1` (grain fact). A required operand with **no governed path** → `NO_GOVERNED_PATH` (the planner only reads VERIFIED bridges; absence never proves an unverified route exists); a governed path whose endpoint lacks a grain fact → `REALIZATION_ENDPOINT_UNGOVERNED`. (4) **Convergence:** select one `PhysicalLandingV1` every operand reaches (landing re-derived from `path_segments`, §1); rank by `_AUTHORITY_RANK`→fewest crossings; **detect a top-semantic-rank tie across distinct landings BEFORE stable ordering** → `AMBIGUOUS_PHYSICAL_GRAIN`/`landing_ambiguous`; no common landing → `NO_COMMON_PHYSICAL_GRAIN`. (5) **Per-path checks (reuse):** `compile_temporal` then `compile_aggregation` per path (with A's own `CompilerContext`, §1). Unsafe → `AGGREGATION_UNSAFE_ON_PATH`; cross-path as-of inconsistency → `TEMPORAL_PATHS_INCOMPATIBLE`. (6) **Final join + expression** on all landing keys; union the per-path `PhysicalReadSetV1`s. (7) **Preservation:** every operand + slot survives once; final expression matches → else `OPERAND_OR_SLOT_NOT_PRESERVED` (technical). (8) **Compile-end union check + mint:** §6.

## 6. `compile_multi_source_contract`

```
compile_multi_source_contract(conn, ctx, plan, spec, *, base_envelope: MultiSourceReplayEnvelopeV1) -> MultiSourceBindingPlanV1
```
Per-path checks reuse `compile_temporal`/`compile_aggregation`/safety over each `OperandPathV1.binding_plan` with A's `CompilerContext` (`placement` from `check_connectivity`). Union freshness: **call** the existing `revalidate_freshness` with a synthetic plan whose `participating_catalogs` = the union of all paths' catalogs (do **not** edit `revalidate_freshness` — it is on the single-source path, §12). Final-combination checks: final expression well-typed at the landing; `output_additivity` coherent. Identity: a multi-source `make_contract_id`-style hash over landing + paths + `path_strategy`s + final expression + versions. `CompileBudget` **decremented per compile**. `confirmed_event_id` (audit only) re-queried from `entity_bridge_edge`.

## 7. Shadow harness + store (migration 1006)

Authored synthetic gold; flag `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`. A runnable admin/CLI entrypoint reads the flag, constructs the **sealed-config upload adapter** and table refs for `resolve_fact` (finding #9), and takes **two connections** (finding #13): `planning_conn` sees the gold fixture transaction; `telemetry_conn` is durable. Sequence: write manifest on `telemetry_conn`; plan each intent on `planning_conn`; retain results in memory; roll back fixtures; persist on `telemetry_conn`; reconcile.

Store (migration 1006, mirrors `0999` + per-candidate rows): `..._dispatch` (PK `run_id`; expected_intent_ids; versions; append-only), `..._intent_result` (PK `(run_id,intent_id)`; four axis columns with CHECK vocabularies — `semantic_outcome`/`compile_completeness`/`technical_status`/`capture_status`; `normalized_intent_hash`; `selected_plan_id`; reason_codes), `..._candidate` (PK `(run_id,intent_id,plan_id)`; `physical_landing`; `contract_input_hash`; `contract_output_hash`; `read_set_hash`; `replay_envelope_hash`; `rank`; `declaration_evidence`), `..._operand_obs` (PK `(run_id,intent_id,plan_id,slot_id)`; pin/role/path_strategy/governed_endpoints/binding_plan segment refs/source_binding). Append-only WORM (REVOKE like `0999`). Writes **read back and compare payload hash** to detect divergent duplicates — never `ON CONFLICT DO NOTHING` on conflicting telemetry. The exact-plan + determinism gate is computable from these rows after the process exits.

## 8. Enumeration + convergence typed results (finding #7)

`enumerate_operand_paths(...) -> OperandEnumerationResultV1{candidates, status, reason_codes, bounds}`; `converge(...) -> ConvergenceResultV1{landed_combinations, status, reason_codes, bounds}` — an empty result always carries a reason (`NO_GOVERNED_PATH`/`REALIZATION_ENDPOINT_UNGOVERNED`/`NO_COMMON_PHYSICAL_GRAIN`/`AMBIGUOUS_PHYSICAL_GRAIN`/`BUDGET_TRUNCATED`), never a bare empty tuple. Bounds `MAX_PATHS_PER_OPERAND`/`MAX_OPERAND_COMBINATIONS`/`MAX_MULTISOURCE_STATES_EXPANDED`; semantic-rank ties detected before stable ordering.

## 9. Dispositions

**Resolve.** **Semantic:** `OPERAND_SHAPE_INVALID`, `UNSUPPORTED_PATH_AGGREGATION`, `ORDERING_ANCHOR_MISSING`, `NO_GOVERNED_PATH`, `REALIZATION_ENDPOINT_UNGOVERNED`, `NO_COMMON_PHYSICAL_GRAIN`, `AMBIGUOUS_PHYSICAL_GRAIN`, `AGGREGATION_UNSAFE_ON_PATH`, `TEMPORAL_PATHS_INCOMPATIBLE`, `SOURCE_BINDING_UNGOVERNED`. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`.

## 10. Gold set + gate (partitioned; findings #8,#14)

**Correctness population** (immutable expected outcomes; positive cases MUST resolve with exact expected landing incl. `grain_key_refs`, per-slot `path_strategy`, `final_expression`; negatives exact-code) vs **fault-observability controls** (injected DB error / budget truncation — pass when exactly classified; excluded from the clean population). Gold **seeds VERIFIED `grain` facts, `entity_bridge` facts, VERIFIED intra-catalog joins, drift watermarks, and projection checkpoints through the real governance write paths** — the resulting **`fact_key`s are deterministic** (derived from ref+type), so double-run determinism keys the replay envelope on `fact_key`s (not per-event ids) and uses **distinct run ids + stable authored fact_keys**. `MULTISOURCE_GOLD_MIN_SHAPES = 6` (identity, ratio-with-take_latest, difference, trend, count_distinct, composite-grain landing — none require stddev). Gate over the clean population: positive coverage mandatory (reject-all fails); zero operand substitution/loss (incl. ordered slots); zero non-governed crossings/endpoints in a resolve; one-grain landing; correct per-path aggregation/temporal; deterministic plan/contract identity; complete reconciliation; no technical/truncation in the clean population.

## 11. Gold cases (must include)

**Correctness — positive:** identity single-measure roll-up; `AVG(x)/latest(y)` RATIO (take_latest denominator + `ordering_anchor_concept`, ordered-role preservation); a DIFFERENCE; a TREND; a composite-grain landing; a COUNT_DISTINCT. **Correctness — negative:** no governed path (`NO_GOVERNED_PATH`); endpoint without a grain fact (`REALIZATION_ENDPOINT_UNGOVERNED`); no common landing; tied landings; non-additive-over-fan-out; temporally incompatible paths; ungoverned source binding; `take_latest` without `ordering_anchor_concept`; a `stddev` operand (`UNSUPPORTED_PATH_AGGREGATION`); concept-collision pin-bypass. **Fault controls:** injected DB error; budget-truncated run.

## 12. Behaviour-neutrality

Flag off → no new path; single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups`/`assemble_paths`/`revalidate_freshness`/`compile_contract` byte-identical (golden test). A **calls** reused functions, never edits them; new carriers/context/store are additive; nothing surfaces.

## 13. Reused surfaces (corrected)

`assemble_paths` + `semantic_rollup_paths` driven from a hand-built `_Position` (not `_assemble_rollups`); `compile_temporal`/`compile_aggregation` + `check_connectivity().placement` over injected templates (F17); **A's own** `CompilerContext` (production one has empty `agg_declarations` + role-scoped columns); `PhysicalReadSetV1`; `resolve_fact` (`grain`) via the sealed-config adapter + table ref, `active_bridges`/`entity_bridge_edge` + intra-catalog realization authorities for crossing governance; `revalidate_freshness` **called** with a union-catalog synthetic plan; `BindingPlanningResultV1` shape (mirrored); `CompileBudget`; the `0999`/`shadow_store.py` manifest+reconciliation pattern (extended: per-candidate rows + payload-hash divergent-duplicate detection).
