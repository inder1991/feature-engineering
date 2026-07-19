# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) — Design

**Status:** approved for planning
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` — rebase onto `origin/main` (`d90d457`+) before implementation
**Migration:** `1005` (re-confirm free at build time)

## 1. Purpose

Give the planner a capability it lacks: **combine operands originating in different catalogs into one governed computation at one exact physical grain.** Today `enumerate_single_catalog_plans` requires all needs in one catalog and `_assemble_rollups` moves a single resolved single-catalog computation to a target entity — neither combines cross-catalog operands. A feature like *"AVG(transaction_amount) [`core_banking.transactions`] ÷ latest(account_balance) [`wealth.accounts`], per customer"* cannot be planned.

A builds each operand an **independent governed path** over **VERIFIED** bridges, **converges all paths to one exact physical landing** `{catalog, table, grain_key}`, applies each operand's own aggregation along its path, proves the paths are cardinality/aggregation/temporal-correct and mutually compatible, joins the per-path outputs at the landing key, applies the **final expression**, asserts nothing was substituted or lost, and compiles. Shadow-only, driven by a **synthetic gold set** of already-authoritative intents. A does **no** concept-authority resolution (B's job) and **no** structural inference — it trusts its typed input and proves the *assembly* is governed.

## 2. Input contract — `MultiSourcePlannerIntentV1`

`@dataclass(frozen=True, slots=True)`, all fields authoritative (from B, or hand-authored in gold):

- `target_entity: str` — the logical grain the feature is defined at.
- `operands: tuple[OperandSlotV1, ...]`.
- `final_expression: FinalExpressionV1` — the operation that combines per-operand results (see §3).
- `operation_policy_version: str`.

`OperandSlotV1`:
- `slot_id: str` (`op_0`…), `semantic_role: SemanticRole` (`MEASURE | TIME | COUNTED | NUMERATOR | DENOMINATOR | MINUEND | SUBTRAHEND`).
- `catalog_source: str`, `object_ref: str` — exact pinned operand identity.
- `authoritative_concept: str` — trusted, ∈ `CONCEPT_REGISTRY`.
- `path_strategy: PathStrategyV1` — this operand's **own** aggregation to the landing grain: `aggregation` (e.g. `AVG`, `SUM`, `TAKE_LATEST`, `COUNT_DISTINCT`), plus its declared `output_type` and `output_additivity`. (Separating per-operand aggregation from the final expression is finding #3: for `AVG(x)/latest(y)`, `AVG` and `TAKE_LATEST` are per-path; `RATIO` is final.)
- `structural_binding: GovernedStructuralBindingV1` — the **frozen governed structural projection** for this operand (finding #4): the authoritative source-grain entity, the source→landing key mapping, and its evidence provenance. A does **not** re-derive grain/key from `graph_node.concept`/`is_grain` (advisory; `catalog_realizations.object_grain`/`key_entity` read the display concept). Absent/ungoverned → A rejects.

`FinalExpressionV1` — the combining op + typed shape + ordered slot references: `IDENTITY(measure)`, `RECENCY(time)`, `TREND(measure,time,window)`, `RATIO(numerator,denominator)`, `DIFFERENCE(minuend,subtrahend)`; `is_order_sensitive` marks the last two. Carries the final `output_additivity` and window/`time_ref` where applicable.

## 3. Operation shape (closed)

Missing/extra/ambiguous operands → `OPERAND_SHAPE_INVALID`.

| `final_expression` | Operand slots | Per-operand `path_strategy` |
|---|---|---|
| `IDENTITY` | 1 `MEASURE` | one aggregation |
| `RECENCY` | 1 `TIME` | latest/recency |
| `TREND` | 1 `MEASURE` + 1 `TIME` + typed `window` | measure agg over window |
| `RATIO` (order-sensitive) | 1 `NUMERATOR` + 1 `DENOMINATOR` | each carries its own aggregation |
| `DIFFERENCE` (order-sensitive) | 1 `MINUEND` + 1 `SUBTRAHEND` | each carries its own aggregation |

Windowed ops require typed `window` + `time_ref` (never inferred).

## 4. The assembly steps

Per intent (own savepoint):

1. **Per-operand path enumeration (bounded).** For each operand, enumerate governed paths from its exact pinned `(catalog_source, object_ref)` node to candidate physical landings (§8 gives the enumeration/ranking/limits). Operand columns are pinned — constructed from the node + `authoritative_concept`, never display-concept discovery.
2. **VERIFIED-only crossings.** Every cross-catalog hop uses a **VERIFIED** governed bridge (`approved_join`) and governed realizations only. No all-VERIFIED path for a required operand → `UNVERIFIED_CROSSING_REQUIRED`.
3. **Exact physical convergence.** Select **one** physical landing `{catalog, table, grain_key}` that **every** operand path can reach (exact-convergence filtering over the planner's physical `_State.position`, not just the same logical entity — finding #1). No common physical landing → `NO_COMMON_PHYSICAL_GRAIN`. Ambiguous landings → deterministic ranking (§8); unresolved ambiguity → `AMBIGUOUS_PHYSICAL_GRAIN`.
4. **Per-path aggregation correctness.** Apply each operand's `path_strategy.aggregation`; validate additivity for the operand's concept + the path's fan-in against the rule evaluators (`AGGREGATION_RULE_VERSION`/`ADDITIVITY_RULE_VERSION`). Non-additive measure summed across a fan-out crossing → `AGGREGATION_UNSAFE_ON_PATH`.
5. **Temporal compatibility.** Each path's PIT treatment (`TEMPORAL_RULE_VERSION`) must be individually valid **and** mutually as-of-consistent at the landing. Incompatible → `TEMPORAL_PATHS_INCOMPATIBLE`.
6. **Final join + expression.** Join the per-path aggregated outputs at the landing `grain_key` and apply `final_expression` (ordered slots preserved: numerator ≠ denominator). Join keys/bridge columns enter only the physical read set.
7. **Preservation assertion.** Assert every original `(catalog_source, object_ref)` operand and its `semantic_role` slot survives exactly once in the correct slot, and the final expression matches the input. Deviation → `OPERAND_OR_SLOT_NOT_PRESERVED` (technical).
8. **Compile.** `compile_multi_source_contract` (§6). Resolve requires: a selected physical landing all operands reach, per-path + final checks pass, `contract_result_status == resolved`, non-null selected contract plan id, steps 1–7 passed.

## 5. New plan carrier — `MultiSourceBindingPlanV1`

`BindingPlanV1` assumes one source catalog / one ingredient set / one shared path (`catalog_source`, path ordering, aggregation placement all singular). A **sibling** carrier:

`MultiSourceBindingPlanV1` `@dataclass(frozen=True, slots=True)`:
- `plan_id: str` — deterministic identity over operand paths + landing + per-path strategies + final expression + versions.
- `target_entity: str`; `physical_landing: PhysicalLandingV1 {catalog, table, grain_key}`.
- `operand_paths: tuple[OperandPathV1, ...]` — per slot: `slot_id`, `semantic_role`, pinned `(catalog_source, object_ref)`, ordered `path_segments` (each VERIFIED crossing named), applied `path_strategy`, per-path PIT treatment.
- `final_expression: FinalExpressionV1`; `physical_read_set` (union); `resolution_status`, `reason_codes`, and the compiled `contract_id`/`contract_result_status`/selected contract plan id.

Single-source planning (`BindingPlanV1`) is untouched and byte-identical.

## 6. `compile_multi_source_contract` (new)

The existing `compile_contract(conn, ctx, plan: BindingPlanV1, template: Template, *, base_envelope)` runs declaration checks over **one** shared path and one Template (`declarations.py`); connectivity/aggregation/freshness/fingerprints all assume a single path (`declarations.py:167`, `fingerprint.py:104`). A defines a sibling:

```
compile_multi_source_contract(conn, ctx, plan: MultiSourceBindingPlanV1, spec: MultiSourceContractSpecV1,
                              *, base_envelope) -> MultiSourceBindingPlanV1
```

With: **identity material** (deterministic over landing + paths + strategies + final expression + versions); **per-path declaration checks** (connectivity, safety, aggregation, temporal, freshness per operand path, reusing the single-path checks per path); **final-combination checks** (the final expression is well-typed at the landing grain; output additivity coherent); an **audit envelope**; and a **replay hash**. Declarations are **injected** from `spec` (production `build_compiler_context` supplies an empty agg-declaration registry). A mutable shared `CompileBudget` persists across intents in a run.

## 7. Shadow harness + store (migration 1005)

Driven by an **authored synthetic gold set** (no gate hook — A has no LLM input). Default-off flag `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`. Store follows the `0999` capture-integrity pattern: run manifest + expected intent set + role/scope fingerprints written first; per-intent two-phase result writes in a savepoint (DB error → `TECHNICAL_FAILURE`); reconciliation; append-only; idempotent `(run_id, intent_id)`; diagnostic-code arrays; separate axes for **semantic outcome / compile completeness / technical status / capture (bounded/truncated) status** (finding #12 — `BUDGET_TRUNCATED` is capture-incomplete, not technical). Persist per intent: intent id + `normalized_intent_hash`; per-operand slot (role, pin, concept, path_strategy, per-path aggregation/temporal, VERIFIED crossings, structural-binding provenance); selected `physical_landing`; `MultiSourceBindingPlanV1` id + read-set hash; contract verdict + selected contract plan id; versions (planner, operation-policy, aggregation/additivity/temporal, concept-registry, compiler); outcome + reason codes. Identities/hashes/enums/provenance only — no free-form text.

## 8. Multi-path enumeration + ranking (finding #8)

The current frontier search (`assembly.py:375`) handles one source path and stops at the first completing bridge tier. A needs: **bounded per-operand path enumeration** (cap per operand); **bounded path-combination search** across operands (cap the cartesian frontier); **exact convergence filtering** (retain only landings every operand reaches); **deterministic ranking** (authority rank → fewest crossings → stable identity order — reusing `_AUTHORITY_RANK`); **explicit ambiguity handling** (`AMBIGUOUS_PHYSICAL_GRAIN` when top-ranked landings tie); and **whole-plan limits** (total states/crossings). Every truncation is recorded and excludes the intent from identity comparisons.

## 9. Dispositions

**Resolve:** all steps pass. **Rejections (semantic):** `OPERAND_SHAPE_INVALID`, `UNVERIFIED_CROSSING_REQUIRED`, `NO_COMMON_PHYSICAL_GRAIN`, `AMBIGUOUS_PHYSICAL_GRAIN`, `AGGREGATION_UNSAFE_ON_PATH`, `TEMPORAL_PATHS_INCOMPATIBLE`, `STRUCTURAL_BINDING_UNGOVERNED`. **Technical:** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`. **Capture-incomplete:** `BUDGET_TRUNCATED`.

## 10. Assembly gate (exact-outcome gold)

Each gold case has an **immutable expected outcome** (finding #7). **Positive cases MUST resolve** with the exact expected `physical_landing`, operand paths, slots, `path_strategy` per slot, and `final_expression` — a reject-everything implementation fails. Negative cases must reject with the exact expected code. Then over the window:
1. **Zero operand substitution or loss** (incl. ordered slots).
2. **Zero unverified crossings** in any resolve.
3. **Every resolve lands all operands at one exact physical grain.**
4. **Correct per-path aggregation + temporal treatment** vs the evaluators.
5. **Deterministic plan + contract identity** (same intent → identical `plan_id`/`normalized_intent_hash`/`contract_id`).
6. **Complete manifest reconciliation.**
7. **No unresolved truncation or technical failures** in the gated population.

Resolution *rate* is descriptive; positive gold coverage is mandatory.

## 11. Gold set (must include)

**Positive (must resolve, exact expected plan):** single-measure roll-up across a VERIFIED bridge; `AVG(x)/latest(y)` `RATIO` with authored numerator/denominator (proves per-path strategy + ordered-role preservation); a `DIFFERENCE`; a `TREND`. **Negative (exact code):** operand reachable only via an **unverified** crossing (`UNVERIFIED_CROSSING_REQUIRED`); operands with **no common physical landing** (`NO_COMMON_PHYSICAL_GRAIN`); tied landings (`AMBIGUOUS_PHYSICAL_GRAIN`); non-additive measure across a fan-out (`AGGREGATION_UNSAFE_ON_PATH`); temporally incompatible paths; an operand with an ungoverned structural binding (`STRUCTURAL_BINDING_UNGOVERNED`); a crafted intent whose operand shares a concept with a different column (pin bypass — wrong column must never substitute). **Technical/capture:** injected DB error mid-run (isolated `TECHNICAL_FAILURE`, reconciliation intact); budget-truncated run (excluded from identity comparisons).

## 12. Behaviour-neutrality

Flag off → no new path runs; single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups`/`compile_contract` byte-identical to `origin/main` (golden test). `MultiSourceBindingPlanV1`/`compile_multi_source_contract` are additive; nothing surfaces.

## 13. Reused surfaces

`discover_ingredient_candidates` (pinned candidate construction); the frontier `_State`/`_Position`/`_AUTHORITY_RANK` (extended for multi-operand convergence); the additivity/aggregation/temporal rule evaluators; governed crossing (`approved_join` VERIFIED) + realization reads; `build_compiler_context` pattern + the single-path declaration checks (per-path reuse); the `CompileBudget` bound pattern; the `0999`/`shadow_store.py` manifest+reconciliation two-phase write pattern.
