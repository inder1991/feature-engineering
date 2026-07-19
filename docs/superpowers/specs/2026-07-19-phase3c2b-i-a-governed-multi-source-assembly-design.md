# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) — Design

**Status:** approved for planning
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — rebase onto `origin/main` (`d90d457`+) before implementation
**Migration:** `1005` (re-confirm free at build time)

## 1. Purpose

Give the planner a capability it lacks: **combine operands originating in different catalogs into one governed computation at one exact physical grain.** Today `enumerate_single_catalog_plans` requires all needs in one catalog and `_assemble_rollups` moves a single resolved single-catalog computation to a target entity — neither combines cross-catalog operands. *"AVG(transaction_amount) [`core_banking.transactions`] ÷ latest(account_balance) [`wealth.accounts`], per customer"* cannot be planned.

A builds each operand an **independent governed path** over **VERIFIED** bridges, **converges all paths to one exact physical landing** `{catalog, table, grain_key_refs}`, applies each operand's own aggregation, proves per-path correctness + cross-path compatibility, joins per-path outputs at the landing keys, applies the **final expression**, asserts preservation, and compiles. Shadow-only, synthetic-gold-driven. A does **no** concept-authority resolution and **no** structural inference — it trusts its typed input for concept + source-side authority and proves the *assembly* governed.

## 2. Contracts

### 2.1 Input — `MultiSourcePlannerIntentV1`
`@dataclass(frozen=True, slots=True)`; all fields authoritative (from B or hand-authored):
- `target_entity: str` — logical grain the feature is defined at.
- `operands: tuple[OperandSlotV1, ...]`; `final_expression: FinalExpressionV1`; `operation_policy_version: str`.

`OperandSlotV1`:
- `slot_id` (`op_0`…), `semantic_role: SemanticRole` (`MEASURE | TIME | COUNTED | NUMERATOR | DENOMINATOR | MINUEND | SUBTRAHEND`).
- `catalog_source`, `object_ref` — exact pinned identity; `authoritative_concept ∈ CONCEPT_REGISTRY`.
- `path_strategy: PathStrategyV1` — this operand's **own** aggregation to the landing (`AVG|SUM|MIN|MAX|STDDEV|TAKE_LATEST|COUNT|COUNT_DISTINCT`) + declared `output_type` + `output_additivity` + `external_type_required: bool` (finding #10: set when operational type is unknown).
- `source_binding: GovernedSourceBindingV1` — **source-side only** (finding #2): authoritative `source_grain_entity`, `source_key_ref`, and evidence provenance. It does **not** carry a landing key mapping (A doesn't know the landing yet). A derives the source→landing mapping per path (§2.2). Absent/ungoverned → `SOURCE_BINDING_UNGOVERNED`.

`FinalExpressionV1` — combining op + **ordered slot references by `slot_id`** (incl. `time_slot_id`, never a raw `time_ref` — finding #5): `IDENTITY(measure_slot)`, `RECENCY(time_slot)`, `TREND(measure_slot, time_slot, window)`, `COUNT(counted_slot)`, `COUNT_DISTINCT(counted_slot)`, `RATIO(numerator_slot, denominator_slot)`, `DIFFERENCE(minuend_slot, subtrahend_slot)`. `is_order_sensitive` marks RATIO/DIFFERENCE. Carries final `output_additivity` + `window`.

### 2.2 Output — result vs plan (finding #1)
Mirror `BindingPlanningResultV1` (which separates candidates from selected ids/bounds/envelope):

`MultiSourcePlanningResultV1` `@dataclass(frozen=True, slots=True)`: `run_id`, `target_entity`, `candidate_plans: tuple[MultiSourceBindingPlanV1, ...]`, `selected_plan_id: str | None`, `result_status`, `primary_reason_code`, `reason_codes`, `bounding: MultiSourceBoundingMetricsV1`, `replay_envelope`, and the contract axis (`contract_result_status`, `selected_contract_physical_plan_id`, `selected_contract_id`).

`MultiSourceBindingPlanV1` (one candidate, carries **only its own** compile result): `plan_id` (deterministic over landing + paths + strategies + final expression + versions), `physical_landing: PhysicalLandingV1`, `operand_paths: tuple[OperandPathV1, ...]`, `final_expression`, `physical_read_set`, `resolution_status`, `reason_codes`, and its own `contract_result_status`/`contract_id` (**not** a selected id).

`PhysicalLandingV1`: `catalog`, `table_ref`, **`grain_key_refs: tuple[str, ...]`** (finding #4 — grains are multi-column; `resolve_fact("grain").value["columns"]` is a list; join on **every** key).

`OperandPathV1`: `slot_id`, `semantic_role`, pinned `(catalog_source, object_ref)`, ordered `path_segments` (each VERIFIED crossing named), the **A-derived source→landing key mapping**, applied `path_strategy`, per-path PIT treatment, per-path check verdicts.

## 3. Complete operation → slot → path-strategy matrix (finding #5)

Total and closed; missing/extra/ambiguous → `OPERAND_SHAPE_INVALID`.

| `final_expression` | slots (semantic_role) | per-slot `path_strategy.aggregation` |
|---|---|---|
| `IDENTITY` | 1 `MEASURE` | `AVG|SUM|MIN|MAX|STDDEV` |
| `COUNT` | 1 `COUNTED` | `COUNT` |
| `COUNT_DISTINCT` | 1 `COUNTED` | `COUNT_DISTINCT` |
| `RECENCY` | 1 `TIME` | `TAKE_LATEST` |
| `TREND` | 1 `MEASURE` + 1 `TIME` (+ typed `window`) | measure: window agg; **time**: `TAKE_LATEST` (the TREND time slot's strategy is defined, not left open) |
| `RATIO` (ordered) | 1 `NUMERATOR` + 1 `DENOMINATOR` | each its own measure agg |
| `DIFFERENCE` (ordered) | 1 `MINUEND` + 1 `SUBTRAHEND` | each its own measure agg |

Windowed ops require a typed `window`; the time anchor is the `TIME` slot referenced by `time_slot_id`.

## 4. Governed realizations (finding #3)

Existing realization derivation reads table grain + key entity from `graph_node.concept` (`catalog_realizations.py:99`) and builds realizations from those values (`catalog_realizations.py:183`) — **advisory**. A VERIFIED bridge *edge* does not make its realization *endpoints* authoritative. A therefore **revalidates every realization endpoint** (source, intermediate, landing) against **governed grain/key facts** before use — an evidence-backed `GovernedRealizationV2` whose grain/key each cite a VERIFIED grain/key fact event. An endpoint with no governed grain/key fact → the path is unusable (`REALIZATION_ENDPOINT_UNGOVERNED`).

## 5. The assembly steps

Per intent (own savepoint):
1. **Per-operand path enumeration (bounded, §8).** From each pinned operand node to candidate physical landings; operand columns pinned (node + `authoritative_concept`, never display-concept discovery).
2. **VERIFIED-only crossings** with **GovernedRealizationV2 endpoints** (§4). No all-governed path for a required operand → `UNVERIFIED_CROSSING_REQUIRED` / `REALIZATION_ENDPOINT_UNGOVERNED`.
3. **Exact physical convergence.** Select **one** `PhysicalLandingV1` (catalog+table+**grain_key_refs**) every operand path reaches — convergence over the physical position (`_Position` extended to carry the landing grain keys). No common landing → `NO_COMMON_PHYSICAL_GRAIN`; tie after ranking → `AMBIGUOUS_PHYSICAL_GRAIN`. A derives each path's source→landing key mapping here.
4. **Per-path aggregation correctness (pure).** Apply `path_strategy`; validate additivity for the concept + fan-in against the rule evaluators. Unsafe → `AGGREGATION_UNSAFE_ON_PATH`.
5. **Per-path temporal correctness (pure).** Each path's PIT treatment individually valid; cross-path as-of-consistency at the landing. Incompatible → `TEMPORAL_PATHS_INCOMPATIBLE`.
6. **Final join + expression.** Join per-path aggregated outputs on **all** landing `grain_key_refs`; apply `final_expression` (ordered slots preserved).
7. **Preservation assertion.** Every original operand + `semantic_role` slot survives exactly once in the correct slot; final expression matches input. Deviation → `OPERAND_OR_SLOT_NOT_PRESERVED` (technical).
8. **Compile-end union check (finding #6).** Per-path checks above are pure (no freshness); freshness is **one** compile-end consistency check over the **union** of catalogs, realizations, bridges, and structural fact evidence (independent per-path freshness could observe different graph states). Then `compile_multi_source_contract` (§6-below). Resolve requires: one landing all operands reach, per-path + final + union checks pass, `contract_result_status == resolved`, non-null contract plan id, steps 1–7 passed.

## 6. `compile_multi_source_contract` (new)

The existing `compile_contract` takes **one** `BindingPlanV1`+`Template` over one shared path (`declarations.py`); connectivity/aggregation/freshness/fingerprints assume a single path (`declarations.py:167`, `:816`, `fingerprint.py:104`). A sibling:
```
compile_multi_source_contract(conn, ctx, plan: MultiSourceBindingPlanV1, spec: MultiSourceContractSpecV1,
                              *, base_envelope) -> MultiSourceBindingPlanV1
```
**Identity material** (deterministic over landing + paths + strategies + final expression + versions); **per-path declaration checks** reusing the single-path connectivity/safety/aggregation/temporal checks; **one union freshness/consistency check** (§5.8); **final-combination checks** (final expression well-typed at the landing; output additivity coherent); **audit envelope**; **replay hash**. Declarations **injected** from `spec` (production `build_compiler_context` supplies an empty agg-declaration registry). Shared mutable `CompileBudget` across intents.

## 7. Shadow harness + store (migration 1005)

Authored synthetic gold (no gate hook). Flag `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`. **Orchestration (finding #12):** a runnable entrypoint; gold fixtures are set up in a transaction, but results are **persisted on a connection outside the fixture rollback** (or committed to the shadow tables before fixture teardown) so reconciliation is meaningful after fixture rollback; the entrypoint's transaction boundary is explicit. Store follows the `0999` capture-integrity pattern: run manifest + expected intent set + role/scope fingerprints first; per-intent two-phase writes in a savepoint; reconciliation; append-only; idempotent `(run_id, intent_id)`; **separate axes**: semantic outcome / compile completeness / technical status / capture (bounded/truncated) status. Persist per intent: intent id + `normalized_intent_hash`; per-operand slot (role, pin, concept, path_strategy, per-path aggregation/temporal, VERIFIED crossings, GovernedRealizationV2 endpoint fact ids, source_binding provenance, A-derived landing mapping); selected `physical_landing` (incl grain_key_refs); `MultiSourcePlanningResultV1` selected id + candidate ids + bounds + read-set hash; contract verdict; versions; outcome + reason codes. Identities/hashes/enums/provenance only.

## 8. Multi-path enumeration + ranking (finding #8)

The current frontier (`assembly.py:375`) handles one source path and stops at the first completing bridge tier. A needs: **bounded per-operand enumeration**; **bounded path-combination search** across operands; **exact convergence filtering** (retain only landings every operand reaches, matching **grain_key_refs**); **deterministic ranking** (`_AUTHORITY_RANK` → fewest crossings → stable identity order); **explicit ambiguity** (`AMBIGUOUS_PHYSICAL_GRAIN` on ties); **whole-plan limits** (total states/crossings), recorded on `MultiSourceBoundingMetricsV1` and excluding truncated intents from identity comparisons.

## 9. Dispositions

**Resolve:** all steps pass. **Semantic rejects:** `OPERAND_SHAPE_INVALID`, `UNVERIFIED_CROSSING_REQUIRED`, `REALIZATION_ENDPOINT_UNGOVERNED`, `NO_COMMON_PHYSICAL_GRAIN`, `AMBIGUOUS_PHYSICAL_GRAIN`, `AGGREGATION_UNSAFE_ON_PATH`, `TEMPORAL_PATHS_INCOMPATIBLE`, `SOURCE_BINDING_UNGOVERNED`. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`.

## 10. Gold set + gate (partitioned — finding #11, #7)

**Two partitions.** (a) **Correctness gold** — each case an immutable expected outcome; **positive cases MUST resolve** with the exact expected `physical_landing` (incl grain_key_refs), operand paths, per-slot `path_strategy`, and `final_expression`; negative cases reject with the exact code. (b) **Fault-observability controls** — injected DB error / budget truncation; they **pass when exactly classified** (`TECHNICAL_FAILURE`/`BUDGET_TRUNCATED`) and are **excluded from the clean operational population**.

**Gate** over the correctness population: positive coverage mandatory (reject-all fails); zero operand substitution/loss (incl. ordered slots); zero unverified crossings / ungoverned endpoints in a resolve; every resolve lands all operands at one exact physical grain; correct per-path aggregation + temporal; deterministic plan/contract identity; complete manifest reconciliation; **no technical failures or unresolved truncation in the clean population**. Define the **minimum distinct authoritative plan shapes** the correctness population must cover. Resolution rate descriptive.

## 11. Gold cases (must include)

**Correctness — positive:** single-measure roll-up over a VERIFIED bridge; `AVG(x)/latest(y)` `RATIO` (per-path strategy + ordered-role preservation); a `DIFFERENCE`; a `TREND`; a **multi-column-grain landing** (composite `grain_key_refs`, join on all keys); a `COUNT_DISTINCT`. **Correctness — negative:** unverified crossing; realization endpoint with no governed grain/key fact; no common physical landing; tied landings; non-additive-over-fan-out; temporally incompatible paths; ungoverned source binding; concept-collision pin-bypass (wrong column must never substitute). **Fault controls (separate partition):** injected DB error (`TECHNICAL_FAILURE`, reconciliation intact); budget-truncated run.

## 12. Behaviour-neutrality

Flag off → no new path; single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups`/`compile_contract` byte-identical (golden test). New carriers/compiler are additive; nothing surfaces.

## 13. Reused surfaces

`discover_ingredient_candidates` (pinned construction); the frontier `_State`/`_Position`/`_AUTHORITY_RANK` (extended for multi-operand convergence + grain keys); additivity/aggregation/temporal evaluators; VERIFIED `approved_join` + a revalidated `GovernedRealizationV2` over grain/key fact events; `build_compiler_context` pattern + the single-path checks (per-path reuse) + a union freshness check; `BindingPlanningResultV1` shape (mirrored); `CompileBudget`; the `0999`/`shadow_store.py` manifest+reconciliation pattern.
