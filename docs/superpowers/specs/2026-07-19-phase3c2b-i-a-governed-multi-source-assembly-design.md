# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) — Design

**Status:** approved for planning
**Date:** 2026-07-19
**Parent:** [3C.2b-i decomposition](2026-07-19-phase3c2b-i-governed-llm-cross-catalog-shadow-design.md)
**Branch:** `feature/phase3c2b-i-governed-llm-cross-catalog-shadow` off `origin/main` (`8636b4d`+)
**Migration:** `1004` (re-confirm free at build time)

## 1. Purpose

Give the planner a capability it does not have: **combine operands that originate in different catalogs into one governed computation at a common target grain.** Today `enumerate_single_catalog_plans` requires all needs in one catalog and `_assemble_rollups` moves a single complete single-catalog computation to a target entity — neither combines cross-catalog operands. A cross-catalog feature like *"average transaction amount (`core_banking.transactions`) ÷ account balance (`wealth.accounts`), per customer"* cannot be planned.

A builds each operand an **independent governed path** to a common grain over **VERIFIED** bridges, proves the paths are cardinality/aggregation/temporal-correct and mutually compatible, unions the physical read sets, asserts nothing was substituted or lost, and compiles the operation at the common grain. It is **shadow-only** and driven by a **synthetic gold set** of already-authoritative intents — it does **no** concept-authority resolution (that is B's job); it trusts its typed input and proves the *assembly* is governed.

### 1.1 Contract boundary

A's input is `MultiSourcePlannerIntentV1` — **already authoritative and fully typed** (produced by B in the live system, hand-authored in A's gold set). A never reads `graph_node.concept` for authority, never infers a semantic role, and never invents operand ordering. If a synthetic intent's operands are not reachable to the target grain over VERIFIED bridges, A rejects it — it does not relax the bridge requirement.

## 2. Input contract — `MultiSourcePlannerIntentV1`

`@dataclass(frozen=True, slots=True)`:

- `target_entity: str` — the logical target grain the feature is defined at (authoritative anchor; not source-qualified — the selected paths determine physical landing).
- `operation_spec: OperationSpecV1` — the operation + its typed shape (see §3). Includes window/time_ref for windowed ops.
- `operands: tuple[OperandSlotV1, ...]`.

`OperandSlotV1`:
- `slot_id: str` — unique (`op_0`, `op_1`).
- `semantic_role: SemanticRole` — the operation-algebra role: `MEASURE | TIME | COUNTED | NUMERATOR | DENOMINATOR | MINUEND | SUBTRAHEND | GROUPING` (ordered roles are first-class **now**; see parent "ordered operands").
- `catalog_source: str`, `object_ref: str` — the exact pinned operand identity.
- `authoritative_concept: str` — the concept (trusted as given; from `CONCEPT_REGISTRY`).

`OperationSpecV1` is **versioned** (`OPERATION_POLICY_VERSION`) and carries every compiler-facing declaration: aggregation function, output additivity, window parameters, temporal requirements, and the required operand shape (§3). Its `is_order_sensitive: bool` marks `RATIO`/`DIFFERENCE`.

## 3. Operation → operand-shape matrix

Closed. Missing/extra/ambiguous operands reject `OPERAND_SHAPE_INVALID`.

| Operation | Required operands (semantic role) |
|---|---|
| `SUM`,`AVG`,`MIN`,`MAX`,`STDDEV` | exactly 1 `MEASURE` |
| `COUNT`,`COUNT_DISTINCT` | exactly 1 `COUNTED` |
| `RECENCY` | exactly 1 `TIME` |
| `TREND` | exactly 1 `MEASURE` + 1 `TIME` + typed `window` |
| `RATIO` | exactly 1 `NUMERATOR` + 1 `DENOMINATOR` (order-sensitive) |
| `DIFFERENCE` | exactly 1 `MINUEND` + 1 `SUBTRAHEND` (order-sensitive) |

`GROUPING` is the governed target grain, not a free operand. Windowed operations require typed `window` + `time_ref` (never inferred).

## 4. The eight assembly steps

Per intent (each intent in its own savepoint):

1. **Per-operand governed path.** For each `OperandSlotV1`, build an independent path from its exact `(catalog_source, object_ref)` graph node to `target_entity`. The operand column is pinned (constructed directly from the node + `authoritative_concept`), never discovered by display-concept match.
2. **VERIFIED-only crossings.** Every cross-catalog hop uses a **VERIFIED** governed bridge (`approved_join`) and governed realizations only. Any hop requiring an unverified crossing → the path fails; if any required operand has no all-VERIFIED path → `UNVERIFIED_CROSSING_REQUIRED` (reject).
3. **Common grain.** Every operand path must land at the **same** `target_entity` grain. An operand that cannot reach it → `OPERAND_UNREACHABLE_AT_TARGET_GRAIN` (reject).
4. **Cardinality + aggregation correctness per path.** Each path's fan-in aggregation must be additivity-correct for that operand's concept (reuse the additivity/aggregation-rule evaluators; `AGGREGATION_RULE_VERSION`/`ADDITIVITY_RULE_VERSION`). A non-additive measure aggregated across a fan-out crossing → `AGGREGATION_UNSAFE_ON_PATH` (reject).
5. **Temporal compatibility across paths.** Each path's point-in-time treatment (reuse `TEMPORAL_RULE_VERSION`) must be individually valid **and** mutually as-of-consistent at the common grain. Incompatible as-of semantics between operand paths → `TEMPORAL_PATHS_INCOMPATIBLE` (reject).
6. **Union read sets.** The physical read set is the union of all operand paths' reads (bridge/realization columns included). Join keys and bridge columns enter only the physical read set, never an operand slot.
7. **Preservation assertion.** Assert every original `(catalog_source, object_ref)` operand and its `semantic_role` slot survives into the assembled plan, exactly once, in the correct slot — including ordered roles (numerator ≠ denominator). Any deviation → `OPERAND_OR_SLOT_NOT_PRESERVED` (technical failure, never a resolve).
8. **Compile at common grain.** Build a `CompilerContext` (§6) with the operation's declarations **injected** (production's `build_compiler_context` supplies an empty agg-declaration registry) and compile the operation over the unioned plan at the common grain. Resolve requires: all operands landed at one governed grain, `contract_result_status == resolved`, non-null selected contract plan id, and steps 1–7 passed.

## 5. New plan carrier — `MultiSourceBindingPlanV1`

The current `BindingPlanV1` assumes **one** source catalog, one ingredient set, one shared path; forcing multiple independent operand paths into it would make `catalog_source`, path ordering, and aggregation-placement ambiguous. Introduce:

`MultiSourceBindingPlanV1` `@dataclass(frozen=True, slots=True)`:
- `plan_id: str` (deterministic identity over the operand paths + operation + target grain).
- `target_entity: str`.
- `operand_paths: tuple[OperandPathV1, ...]` — one per operand slot: `slot_id`, `semantic_role`, `pinned (catalog_source, object_ref)`, the ordered `path_segments` (each `VERIFIED` crossing named), per-path aggregation + temporal treatment.
- `physical_read_set: tuple[...]` — the union.
- `operation_spec: OperationSpecV1`.
- `resolution_status`, `reason_codes`, and the compiled `contract_id`/`contract_result_status`/selected contract plan id.

This is a **sibling** to `BindingPlanV1`, not a replacement; single-source planning is untouched and byte-identical.

## 6. Compiler context + declaration injection

Production `build_compiler_context` (`planner/declarations.py`) deliberately supplies an **empty** aggregation-declaration registry. A's shadow builds its own context (batch-loaded once per run: realizations, active governed crossings, read-scoped columns, scope-start fingerprints) and **injects** the operation's aggregation/window/temporal declarations from the versioned `OperationSpecV1`. Compilation is otherwise the existing pass; the compile-end fingerprint recheck is preserved. A mutable shared `CompileBudget` persists across intents in a run (the `MAX_COMPILES_PER_RUN` + real-elapsed-deadline pattern from `shadow.py`).

## 7. Shadow harness + store (migration 1004)

Driven by an **authored synthetic gold set** of `MultiSourcePlannerIntentV1` (not a gate hook — A has no LLM input). Behind a default-off flag (e.g. `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`).

Store follows the `0999` recipe-shadow pattern (capture integrity, finding #8):
- A **run manifest** written first (before assembly) with the **expected intent set** and run fingerprints (roles/scope/policy versions).
- Per-intent **result rows** via a two-phase write; a per-intent savepoint isolates DB errors as `TECHNICAL_FAILURE`.
- **Reconciliation**: manifest expected-set vs written results reveals any dropped write.
- Physical **append-only** enforcement; **idempotent** duplicate-guard on `(run_id, intent_id)`; **role/scope fingerprints**; **diagnostic-code arrays**; **bounded/truncated** capture status. No unredacted free-form text — identities, hashes, enums, provenance ids only.

Persist per intent: intent id + `normalized_intent_hash`; per-operand slot (semantic role, pinned `(catalog_source, object_ref)`, concept, per-path aggregation/temporal treatment, VERIFIED crossings traversed); `MultiSourceBindingPlanV1` id + physical read set hash; contract verdict + selected contract plan id; versions (planner, operation-policy, aggregation/additivity/temporal rule, concept-registry, compiler); authoritative outcome + reason codes; bounded/truncated status.

## 8. Dispositions

**Resolve:** all eight steps pass. **Rejections (semantic):** `OPERAND_SHAPE_INVALID`, `UNVERIFIED_CROSSING_REQUIRED`, `OPERAND_UNREACHABLE_AT_TARGET_GRAIN`, `AGGREGATION_UNSAFE_ON_PATH`, `TEMPORAL_PATHS_INCOMPATIBLE`. **Technical (never resolve/semantic):** `OPERAND_OR_SLOT_NOT_PRESERVED`, `TECHNICAL_FAILURE`, `BUDGET_TRUNCATED`.

## 9. Assembly gate

Over the evaluation window on the adversarial gold set:
1. **Zero operand substitution or loss** — every resolve preserves all operands + semantic slots (incl. ordered).
2. **Zero unverified crossings** — no resolved plan traverses a non-VERIFIED bridge.
3. **Every resolved plan lands all operands at one governed grain.**
4. **Correct per-path aggregation and temporal treatment** — validated against the rule evaluators.
5. **Deterministic plan and contract identity** — same intent → identical `plan_id`/`normalized_intent_hash`/`contract_id`.
6. **Complete manifest reconciliation** — no dropped or duplicated captures.
7. **No unresolved truncation or technical failures** in the gated population.

Resolution *rate* is descriptive only — the gate is correctness, not throughput.

## 10. Gold set (must include)

Valid single-measure roll-up across a VERIFIED bridge; valid `RATIO`/`DIFFERENCE` with authored numerator/denominator (proves ordered-role preservation); an operand reachable only via an **unverified** crossing (must reject); an operand unreachable at the target grain; a non-additive measure aggregated across a fan-out (must reject); temporally incompatible operand paths; a crafted intent whose operand column shares a concept with a *different* column (proves pin bypass — the wrong column must never be substituted); an injected DB error mid-run (isolated `TECHNICAL_FAILURE`, reconciliation intact); a budget-truncated run (excluded from identity comparisons, logged).

## 11. Behaviour-neutrality

With the shadow flag off, no new code path runs; single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups` are byte-identical to `origin/main` (golden comparison in tests). `MultiSourceBindingPlanV1` is additive; nothing surfaces.

## 12. Reused surfaces

`plan_bindings`/`discover_ingredient_candidates` (pinned candidate construction), the additivity/aggregation/temporal rule evaluators, the governed crossing (`approved_join`, VERIFIED) + realization reads, `build_compiler_context` pattern + the contract-compile pass, the `CompileBudget`/`MAX_COMPILES_PER_RUN` budget pattern, and the `0999`/`shadow_store.py` manifest+reconciliation two-phase write pattern.
