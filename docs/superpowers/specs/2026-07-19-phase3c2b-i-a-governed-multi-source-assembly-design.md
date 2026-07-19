# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) — Design

**Status:** approved for planning (post 5th review — contract + reuse model corrected)
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — implement in a **separate clean worktree** off `origin/main` (`d90d457`+); do not rebase the shared dirty tree (finding #15).
**Migration:** `1005` (re-confirm free at build time)

## 1. Purpose + the reuse model (finding #9)

Combine operands originating in different catalogs into one governed computation at one exact physical grain — a capability `plan_bindings` lacks (`enumerate_single_catalog_plans` needs all needs in one catalog; `_assemble_rollups` moves a single resolved single-catalog computation to a target entity).

**Reuse model (the spine of A):** each operand's path to the landing is expressed as an ordinary **single-source `BindingPlanV1` over an injected single-need `Template`** (target = the landing grain). The existing single-operand rollup (`_assemble_rollups`) and per-path compiler (`compile_temporal`, `compile_aggregation`, safety/connectivity, `PhysicalReadSetV1`) already work on **injected templates** (F17 — `compile_temporal` docstring: *"works for INJECTED templates"*). So A does **not** reimplement per-path planning or compilation. A adds exactly four things on top:

1. **Endpoint governance** — every path endpoint's grain/keys revalidated against governed **facts**.
2. **Exact physical-landing convergence** — all operand plans land at one `{catalog, table, grain_key_refs}`.
3. **Final combination** — join the per-path outputs on the landing keys and apply the final expression.
4. **One compile-end union freshness/consistency check** over all paths.

Shadow-only, synthetic-gold-driven. A trusts its typed input for concept + source-side authority; it proves the *assembly* is governed.

## 2. Authority basis (finding #2 — no key fact exists)

`FactType` is closed: `grain | availability_time | scd_effective_dating | approved_join | entity_bridge | policy_tag` (`overlay/_types.py`). **There is no key fact.** So:
- A table's **grain** (its key columns) is proven by a VERIFIED **`grain` fact** (`resolve_fact(..,"grain").value["columns"]` — short column names).
- A **crossing** between two catalogs/tables is proven by a VERIFIED **`approved_join`** or **`entity_bridge`** fact, which proves the endpoint columns *and* the relationship/cardinality.
- There is **no** standalone entity-key fact. A must not invent one (that would be a separate governance design + migration, out of scope for A). `GovernedSourceBindingV1` therefore carries a grain-fact reference for the source grain and relies on approved_join/entity_bridge facts for crossings — never a `key_fact_event_id`.

## 3. Contracts

All `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`.

### 3.1 Governed endpoint + crossing (findings #2, #3, #4, #5)
```
GovernedEndpointV1: catalog, table_ref, grain_key_refs: tuple[str, ...], grain_fact_event_id
```
`grain_key_refs` are **schema/table-qualified object_refs** derived by qualifying the grain fact's short `columns` with the endpoint table and validating membership against `graph_node.column_name` (finding #4 — the fact stores short names; projection compares `column_name`). An endpoint with no VERIFIED grain fact → not a `GovernedEndpointV1` (unusable).
```
GovernedCrossingV1: from_endpoint: GovernedEndpointV1, to_endpoint: GovernedEndpointV1,
                    from_keys: tuple[str, ...], to_keys: tuple[str, ...],
                    cardinality: Cardinality, authority: CrossingAuthority (approved_join|entity_bridge),
                    confirmed_event_id
```
A path is an **ordered `execution_edges: tuple[GovernedCrossingV1, ...]`** (finding #5 — a multi-hop path keeps each hop's key mapping + cardinality + provenance; not a flattened source→landing pair).

### 3.2 Input — `MultiSourcePlannerIntentV1`
- `target_entity: str`; `operands: tuple[OperandSlotV1, ...]`; `final_expression: FinalExpressionV1`; `operation_policy_version: str`.

`OperandSlotV1`: `slot_id`, `semantic_role: SemanticRole`, `catalog_source`, `object_ref`, `authoritative_concept`, `path_strategy: PathStrategyV1`, `source_binding: GovernedSourceBindingV1`.

`GovernedSourceBindingV1` (finding #2): `source_grain_entity`, `source_grain_key_refs: tuple[str, ...]` (composite; qualified), `grain_fact_event_id`. **No `key_fact_event_id`.**

`PathStrategyV1` (findings #1, #10): `aggregation: PathAggregation`, `output_type`, `output_additivity`, `external_type_required: bool`, **`ordering_anchor_ref: str | None`** — a governed temporal column (accepted `pit_role`, authority-checked) **required** when `aggregation == take_latest` (else `ORDERING_ANCHOR_MISSING`); `None` otherwise.

`FinalExpressionV1`: `operation: FinalOperation`, `ordered_slot_ids: tuple[str, ...]` (order-sensitive ops rely on order), `time_slot_id: str | None` (must reference a `TIME` slot), `window: str | None`, `output_additivity`.

### 3.3 Output — result vs plan (finding #1) with full evidence (findings #6, #11, #12)
`MultiSourcePlanningResultV1` (mirrors `BindingPlanningResultV1`): `run_id`, `target_entity`, `candidate_plans: tuple[MultiSourceBindingPlanV1, ...]`, `selected_plan_id`, `result_status`, `primary_reason_code`, `reason_codes`, `bounding: MultiSourceBoundingMetricsV1`, `replay_envelope: MultiSourceReplayEnvelopeV1`, contract axis (`contract_result_status`, `selected_contract_plan_id`, `selected_contract_id`).

`MultiSourceBindingPlanV1` (one candidate; **own** compile result only): `plan_id`, `physical_landing: PhysicalLandingV1`, `operand_paths: tuple[OperandPathV1, ...]`, `final_expression`, `physical_read_set: PhysicalReadSetV1` (**reuse** the source-qualified, role-bearing, safety-carrying type — finding #6), `resolution_status`, `reason_codes`, and its own **declaration/check/audit** fields: `contract_result_status`, `contract_id`, `declaration_evidence` (per-path `HopAggregationV1`/`TemporalDeclarationV1` + final-combination verdict), `contract_input_hash`, `contract_output_hash`.

`PhysicalLandingV1`: `catalog`, `table_ref`, `grain_key_refs: tuple[str, ...]` (composite; join on **every** key).

`OperandPathV1`: `slot_id`, `semantic_role`, `catalog_source`, `object_ref`, the underlying single-source `binding_plan: BindingPlanV1` (the injected-template plan — §1), `execution_edges: tuple[GovernedCrossingV1, ...]`, `path_strategy`, `pit_treatment`.

`MultiSourceBoundingMetricsV1`: `paths_per_operand_truncated`, `operand_combinations_truncated`, `states_truncated`, **`landing_ambiguous`** (finding #7), `total_states_expanded`.

`MultiSourceReplayEnvelopeV1` (finding #11 — `_envelope` needs a recipe_id A doesn't have): a multi-source input fingerprint over target_entity + operand pins + source bindings + governed endpoint/crossing fact ids + versions. Deterministic; no recipe_id.

## 4. Complete operation → slot → path-strategy matrix (findings #1, #5, #10)

Total, closed; exact role→slot references validated (not set membership). Missing/extra/duplicate slot / mis-referenced time/order / unexpected window → `OPERAND_SHAPE_INVALID`.

| `final_expression` | slots (role) | allowed per-slot `path_strategy.aggregation` |
|---|---|---|
| `IDENTITY` | 1 `MEASURE` | `AVG SUM MIN MAX STDDEV` |
| `COUNT` | 1 `COUNTED` | `COUNT` |
| `COUNT_DISTINCT` | 1 `COUNTED` | `COUNT_DISTINCT` |
| `RECENCY` | 1 `TIME` | `TAKE_LATEST` (needs `ordering_anchor_ref`) |
| `TREND` | 1 `MEASURE` + 1 `TIME` (+ `window`) | measure: `AVG SUM`; time: `TAKE_LATEST` |
| `RATIO` (ordered) | 1 `NUMERATOR` + 1 `DENOMINATOR` | each: `AVG SUM MIN MAX STDDEV **TAKE_LATEST**` |
| `DIFFERENCE` (ordered) | 1 `MINUEND` + 1 `SUBTRAHEND` | each: `AVG SUM MIN MAX STDDEV **TAKE_LATEST**` |

RATIO/DIFFERENCE operands **may** be `TAKE_LATEST` (the canonical *"AVG(txn) ÷ latest(balance)"* — a semi-additive stock reduced by latest), each such operand carrying its own `ordering_anchor_ref`. Validation checks: exact multiset of roles; each `ordered_slot_id`/`time_slot_id` references a real, correctly-roled slot; no duplicate `slot_id`; numerator≠denominator (minuend≠subtrahend); window present iff required; `take_latest` ⇒ `ordering_anchor_ref` present.

## 5. Assembly steps

Per intent (own savepoint):
1. **Shape** — `validate_operation_shape` (§4).
2. **Per-operand injected-template rollup** — for each operand, build a single-need injected `Template` (need = pinned operand column + `authoritative_concept`; target = `target_entity`), run the existing single-source rollup to enumerate governed paths to candidate landings (bounded `MAX_PATHS_PER_OPERAND`). Reuses the frontier + `PhysicalReadSetV1`.
3. **Endpoint governance** — for every path, revalidate each endpoint via `GovernedEndpointV1` (grain fact) and each crossing via `GovernedCrossingV1` (approved_join/entity_bridge). Ungoverned endpoint → path dropped; a required operand with **no governed path at all** → `NO_GOVERNED_PATH` (finding #8 — the planner reads only VERIFIED bridges; absence never proves an unverified route exists); a governed path exists but an endpoint lacks a grain fact → `REALIZATION_ENDPOINT_UNGOVERNED`.
4. **Exact physical convergence** — select **one** `PhysicalLandingV1` (catalog+table+**grain_key_refs**) every operand reaches. **Detect semantic-rank ties first** (finding #7): rank by `_AUTHORITY_RANK` → fewest crossings; if the top *semantic* rank ties across distinct landings → `landing_ambiguous`/`AMBIGUOUS_PHYSICAL_GRAIN` **before** the stable-identity presentation order is applied. No common landing → `NO_COMMON_PHYSICAL_GRAIN`.
5. **Per-path pure checks (via reuse)** — each operand path is a `BindingPlanV1`; run `compile_temporal(ctx, plan, injected_template)` then `compile_aggregation(ctx, plan, injected_template, temporal, placement)` (finding #9 — these are the real signatures; A feeds each path through them as an injected-template single-source plan). Aggregation unsafe → `AGGREGATION_UNSAFE_ON_PATH`; cross-path as-of inconsistency → `TEMPORAL_PATHS_INCOMPATIBLE`.
6. **Final join + expression** — join per-path outputs on all landing `grain_key_refs`; apply `final_expression` (ordered slots preserved). Union the per-path `PhysicalReadSetV1`s.
7. **Preservation assertion** — every operand + `semantic_role` slot survives once; final expression matches input. Else `OPERAND_OR_SLOT_NOT_PRESERVED` (technical).
8. **Compile-end union check + mint** — one union freshness/consistency check over the union of catalogs/realizations/bridges/structural fact ids (extend `declarations.py:816` from one plan's catalogs to the union). Mint `contract_id`/hashes/`MultiSourceReplayEnvelopeV1`; **decrement `CompileBudget`** per compile (finding #11). Resolve iff one landing reached by all, per-path + union + final checks pass, `contract_result_status == resolved`, non-null contract id, steps 1–7 passed.

## 6. `compile_multi_source_contract`

```
compile_multi_source_contract(conn, ctx, plan: MultiSourceBindingPlanV1, spec: MultiSourceContractSpecV1,
                              *, base_envelope: MultiSourceReplayEnvelopeV1) -> MultiSourceBindingPlanV1
```
Per-path declaration checks are the reused single-path `compile_temporal`/`compile_aggregation`/safety over each `OperandPathV1.binding_plan`; then the **one** union freshness/consistency check; then final-combination checks (final expression well-typed at the landing; `output_additivity` coherent). Declarations **injected** from `spec` (production `build_compiler_context` supplies an empty agg-declaration registry). Identity via a multi-source `make_contract_id`-style hash over landing + paths + strategies + final expression + versions. Shared mutable `CompileBudget` decremented per compile.

## 7. Shadow harness + store (migration 1005)

Authored synthetic gold (no gate hook). Flag `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`.

**Two-connection design (finding #13):** the runnable admin/CLI entrypoint reads the flag and takes **`planning_conn`** (sees the gold fixture transaction) and **`telemetry_conn`** (durable). Sequence: write the manifest on `telemetry_conn`; plan each intent against `planning_conn` (fixtures); **retain results in memory**; roll back the fixture transaction; **persist results on `telemetry_conn`**; reconcile. A single connection cannot both see rollback-only fixtures and durably retain telemetry.

**Store (findings #12, #8-store):** migration `1005` mirrors `0999` but adds per-**candidate** rows, not just the selected one: `multisource_assembly_shadow_dispatch` (PK `run_id`; expected_intent_ids; versions; append-only), `multisource_assembly_shadow_intent_result` (PK `(run_id,intent_id)`; the **four separate axis columns** with CHECK vocabularies — `semantic_outcome`/`compile_completeness`/`technical_status`/`capture_status`; `normalized_intent_hash`; `selected_plan_id`; reason_codes), `multisource_assembly_shadow_candidate` (PK `(run_id,intent_id,plan_id)`; `physical_landing` jsonb; `contract_input_hash`; `contract_output_hash`; `read_set_hash`; `replay_envelope_hash`; `rank`; `declaration_evidence` jsonb), `multisource_assembly_shadow_operand_obs` (PK `(run_id,intent_id,plan_id,slot_id)`; pin/role/path_strategy/execution_edges/endpoint-fact-ids/source-binding). Append-only (no UPDATE/DELETE). Idempotent writes must **detect divergent duplicates** (compare payload hash; a conflicting re-write is an error, not `ON CONFLICT DO NOTHING`). The exact-plan + determinism gate is computable from these rows after the process exits.

## 8. Multi-path enumeration + bounds (finding #7)

Typed results: `enumerate_operand_paths(...) -> OperandEnumerationResultV1{candidates, status, reason_codes, bounds}`; `converge(...) -> ConvergenceResultV1{landed_combinations, status, reason_codes, bounds}` — an empty result carries a *reason* (`NO_GOVERNED_PATH` / `REALIZATION_ENDPOINT_UNGOVERNED` / `NO_COMMON_PHYSICAL_GRAIN` / `AMBIGUOUS_PHYSICAL_GRAIN` / `BUDGET_TRUNCATED`), never a bare empty tuple. Bounds: `MAX_PATHS_PER_OPERAND`, `MAX_OPERAND_COMBINATIONS`, `MAX_MULTISOURCE_STATES_EXPANDED`; semantic-rank ties detected before stable ordering.

## 9. Dispositions

**Resolve.** **Semantic:** `OPERAND_SHAPE_INVALID`, `ORDERING_ANCHOR_MISSING`, `NO_GOVERNED_PATH`, `REALIZATION_ENDPOINT_UNGOVERNED`, `NO_COMMON_PHYSICAL_GRAIN`, `AMBIGUOUS_PHYSICAL_GRAIN`, `AGGREGATION_UNSAFE_ON_PATH`, `TEMPORAL_PATHS_INCOMPATIBLE`, `SOURCE_BINDING_UNGOVERNED`. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`.

## 10. Gold set + gate (partitioned; findings #11, #14)

**Correctness population** (immutable expected outcomes; positive cases MUST resolve with exact expected landing incl. `grain_key_refs`, per-slot `path_strategy`, `final_expression`; negatives exact-code) vs **fault-observability controls** (injected DB error / budget truncation — pass when exactly classified; excluded from the clean population). Gold **seeds VERIFIED `grain` facts, `approved_join`/`entity_bridge` facts, drift watermarks, and projection checkpoints through the real governance write paths** (not hand-set columns) so endpoints are genuinely governed. **Minimum distinct authoritative shapes = a concrete versioned number** (`MULTISOURCE_GOLD_MIN_SHAPES = 6`: identity, ratio-with-take_latest, difference, trend, count_distinct, composite-grain landing). Double-run determinism uses **distinct run ids + stable authored fact ids**. Gate over the clean population: positive coverage mandatory (reject-all fails); zero operand substitution/loss (incl. ordered slots); zero non-governed crossings/endpoints in a resolve; one-grain landing; correct per-path aggregation/temporal; deterministic plan/contract identity; complete reconciliation; no technical/truncation in the clean population.

## 11. Gold cases (must include)

**Correctness — positive (must resolve):** identity single-measure roll-up; `AVG(x)/latest(y)` RATIO (take_latest denominator + `ordering_anchor_ref`, ordered-role preservation); a DIFFERENCE; a TREND; a **composite-grain landing** (multi-column `grain_key_refs`); a COUNT_DISTINCT. **Correctness — negative (exact code):** no governed path (`NO_GOVERNED_PATH`); endpoint with no grain fact (`REALIZATION_ENDPOINT_UNGOVERNED`); no common landing; tied landings (`AMBIGUOUS_PHYSICAL_GRAIN`); non-additive-over-fan-out; temporally incompatible paths; ungoverned source binding; `take_latest` without `ordering_anchor_ref` (`ORDERING_ANCHOR_MISSING`); concept-collision pin-bypass. **Fault controls (separate partition):** injected DB error (`TECHNICAL_FAILURE`); budget-truncated run.

## 12. Behaviour-neutrality

Flag off → no new path; single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups`/`compile_contract` byte-identical (golden test). New carriers/compiler/store are additive; nothing surfaces.

## 13. Reused surfaces

The single-source frontier/`_assemble_rollups` + per-path compiler (`compile_temporal`/`compile_aggregation`/safety) over **injected templates** (F17); `PhysicalReadSetV1`/`PhysicalColumnReadV1`; `resolve_fact` (`grain` fact) + `approved_join`/`entity_bridge` fact reads for endpoint/crossing governance; `build_compiler_context` + a union freshness extension of `declarations.py:816`; `BindingPlanningResultV1` shape (mirrored); `CompileBudget`; the `0999`/`shadow_store.py` manifest+reconciliation pattern (extended with candidate rows + payload-hash divergent-duplicate detection).
