# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the planner a shadow-only capability to combine operands originating in different catalogs into one governed computation at one exact physical grain, proven against a partitioned gold set.

**Architecture:** Reuse the existing single-source frontier + per-path compiler by expressing each operand's path as an injected-template `BindingPlanV1`; add only endpoint governance (grain facts), physical-landing convergence (composite `grain_key_refs`), final join + expression, and one union freshness check. A **calls** reused functions, never edits them, so single-source planning stays byte-identical when the flag is off. **Task 1 is a spike** that proves the reuse premise against real code before any multi-source contract is built.

**Tech Stack:** Python 3.12, `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum` (NOT pydantic), psycopg, pytest. All under `src/featuregen/overlay/upload/planner/`.

**Spec:** `docs/superpowers/specs/2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md` (6th-review-hardened).

## Global Constraints

- **Shadow-only:** log/measure, never surface; no data plane; no signing.
- **F4:** output is a contract definition with a governed physical plan, never an attested cross-catalog `approved_join`.
- **Fail-closed:** missing/ambiguous/ungoverned/unsupported input rejects; never guess.
- **Authority, not display:** structural grain/keys from governed `grain` facts (`resolve_fact` via the sealed-config adapter), never `graph_node.concept`/`is_grain`. No key fact exists (`FactType` = `grain|availability_time|scd_effective_dating|approved_join|entity_bridge|policy_tag`); crossings are the frontier's governed `path_segments` (intra-catalog `APPROVED_JOIN`/`DECLARED_JOIN`/`INFERRED_JOIN` realizations + VERIFIED `entity_bridge`).
- **Reuse, don't edit:** A calls `assemble_paths`/`semantic_rollup_paths`/`compile_temporal`/`compile_aggregation`/`check_connectivity`/`revalidate_freshness` unchanged; A builds its **own** `CompilerContext` (production `build_compiler_context` has empty `agg_declarations` and role-scoped columns).
- **Determinism keys on `fact_key`s** (deterministic from ref+type), never per-event `confirmed_event_id`.
- **Behaviour-neutral:** flag off ⇒ single-source path byte-identical.
- **Types:** `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`; version every policy.
- **A ALONE:** implement no B (adapter/worker/hook/concept-authority) surface.
- **Migration 1006** (`1005_llm_dispatch_provenance` taken); re-confirm free at build time.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Reused surfaces (read before use — origin/main d90d457):** `planner/contracts.py` (`AggregationFunction:155`, `BindingPlanningResultV1`, `BoundingMetricsV1`, `PlanResolutionStatus`, `ReasonCode`, `BindingPlanV1`, `PhysicalReadSetV1:469`, `MAX_*`, `*_VERSION`); `planner/assembly.py` (`_Position` entity/catalog/table_ref, `assemble_paths(conn,*,source_position,semantic_path,scope,ingredient_bindings,template,target_entity)->AssemblyV1`, `semantic_rollup_paths(source_entity,target_entity)`, `_AUTHORITY_RANK`); `planner/declarations.py` (`CompilerContext` fields, `check_connectivity(ctx,plan).placement:167`, `compile_temporal(ctx,plan,template):241`, `compile_aggregation(ctx,plan,template,temporal,placement):527`, `revalidate_freshness:816`, `build_compiler_context:1046`, `CompileBudget`); `resolve.py` (`resolve_fact(conn,adapter,ref,fact_type,now=)`, adapter consulted for data facts `:210`); `facts.py` (grain value `{columns,is_unique}:56`, entity_bridge schema `:108`); `bridge_projection.py` (`active_bridges:57`, `entity_bridge_edge` for `confirmed_event_id`); `catalog_realizations.py` (`_join_edges` intra-catalog, `object_grain:99`); `templates.py` (`Template`/`Need`); `planner/shadow_store.py` + `db/migrations/0999_planner_shadow_store.sql`; `planner/contract_eval.py`/`contract_gold.py`.

---

### Task 0: Prerequisite — clean worktree, migration check, constants

**Files:** Modify `src/featuregen/overlay/upload/planner/contracts.py`.

- [ ] **Step 1: Build in place on the current branch** (do NOT rebase or merge — the shared tree holds the parallel session's uncommitted WIP, and a merge is blocked by their untracked docs; A's reused planner/facts code is byte-identical between this branch's base and origin/main, verified, so building here is functionally correct). Every implementer stages ONLY its explicit files — never `git add -A` — to preserve the parallel WIP. The final merge-back re-confirms the migration number.
- [ ] **Step 2: Confirm migration 1006 is above origin/main's highest** (origin/main tops at `1005_llm_dispatch_provenance`; A uses `1006`, B `1007`).
```bash
git ls-tree -r --name-only origin/main -- src/featuregen/db/migrations/ | grep -oE "10[0-9][0-9]" | sort -n | tail -1   # expect 1005
```
Expected: `1005`. If higher, bump A's migration above it and update every `1006_` reference here.
- [ ] **Step 3: Add constants** — append to `contracts.py` after the `*_VERSION` block:
```python
# 3C.2b-i-A — governed multi-source operand assembly (shadow).
MULTISOURCE_ASSEMBLY_VERSION = "3c2bia.1.0.0"
OPERATION_POLICY_VERSION = "3c2bia.op.1.0.0"
MULTISOURCE_ASSEMBLY_SHADOW_FLAG = "FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW"
MULTISOURCE_GOLD_MIN_SHAPES = 6
MAX_PATHS_PER_OPERAND = 8
MAX_OPERAND_COMBINATIONS = 256
MAX_MULTISOURCE_STATES_EXPANDED = 1024
```
- [ ] **Step 4: Verify** — `python -c "from featuregen.overlay.upload.planner.contracts import MULTISOURCE_ASSEMBLY_SHADOW_FLAG, MULTISOURCE_GOLD_MIN_SHAPES; print('ok')"` → `ok`.
- [ ] **Step 5: Commit** (`feat(3c2bia): worktree + version/bound constants`).

---

### Task 1: SPIKE — prove the reuse premise against real code

**Goal:** Before building any multi-source contract, demonstrate that an **injected single-need `Template`** + a **hand-built `_Position`** runs through `semantic_rollup_paths` → `assemble_paths` → `check_connectivity` → `compile_temporal` → `compile_aggregation` (using A's **own** `CompilerContext`) and yields a **resolved** cross-catalog single-operand `BindingPlanV1` with an aggregation declaration. If any link fails, STOP and report — the whole design rests on this.

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_reuse.py` (thin helpers: `build_operand_context(...)`, `injected_operand_template(...)`, `run_operand_rollup(...)`)
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_reuse_spike.py`

**Interfaces:**
- Produces: `injected_operand_template(*, recipe_id, need_role, concept, source_entity, anchor_concept=None) -> Template`; `build_operand_context(conn, *, catalogs, roles, now, agg_declarations) -> CompilerContext`; `run_operand_rollup(conn, ctx, *, source_position, target_entity, template, scope, ingredient_bindings) -> BindingPlanV1 | None`.

- [ ] **Step 1: Write the failing test** — a two-catalog fixture where a `monetary_flow` column in catalog A is reachable to entity `customer` via a VERIFIED `entity_bridge`, with a VERIFIED grain fact on the landing:
```python
def test_injected_operand_template_rolls_up_and_compiles(two_catalog_bridged_fixture):
    conn, scope, now = two_catalog_bridged_fixture
    tmpl = injected_operand_template(recipe_id="ms:op_0", need_role="measure_0",
                                     concept="monetary_flow", source_entity="transaction")
    ctx = build_operand_context(conn, catalogs=["core_banking", "wealth"],
                                roles=("feature_engineer",), now=now,
                                agg_declarations=_agg_decls_for("ms:op_0", "measure_0", "sum"))
    plan = run_operand_rollup(conn, ctx, source_position=_Position("transaction", "core_banking",
                              "public.transactions"), target_entity="customer", template=tmpl,
                              scope=scope, ingredient_bindings=_binding_for("measure_0",
                              "core_banking", "public.transactions.amount"))
    assert plan is not None
    assert plan.resolution_status is PlanResolutionStatus.resolved
    conn_res = check_connectivity(ctx, plan)
    temporal = compile_temporal(ctx, plan, tmpl)
    hops = compile_aggregation(ctx, plan, tmpl, temporal, conn_res.placement)
    assert hops  # a fan-in hop produced an aggregation declaration
```
- [ ] **Step 2: Run → FAIL** (`ModuleNotFoundError` / fixtures absent). Build the fixture using the real governance write paths (seed a VERIFIED grain fact + a VERIFIED entity_bridge via `record_field_evidence`/the fact confirm path — read `facts.py`/`bridge_projection.py`/existing planner tests for the seeding helpers first).
- [ ] **Step 3: Implement** the three helpers. `injected_operand_template`: a `Template` with `id=recipe_id`, one required `Need(role=need_role, concept=concept, ...)` (+ a temporal `Need` when `anchor_concept` is set), `source_entity`, `source_entity_need_role`. `build_operand_context`: a `CompilerContext` with `agg_declarations` populated (NOT the empty production builder) and `columns_by_catalog`/`realizations_by_catalog`/`active_bridges`/fingerprints/stamps loaded for the given catalogs+roles. `run_operand_rollup`: `paths, status = semantic_rollup_paths(source_position.entity, target_entity)`; pick the governed path; `assemble_paths(conn, source_position=..., semantic_path=path, scope=..., ingredient_bindings=..., template=..., target_entity=...)`; return the first `resolved` plan from `AssemblyV1.complete`.
- [ ] **Step 4: Run → PASS.** If it cannot be made to pass, STOP: the reuse premise is wrong; report with the exact failure before proceeding.
- [ ] **Step 5: Add a second test** proving the `take_latest` two-need injection: `injected_operand_template(..., anchor_concept="as_of_date")` yields a plan whose `compile_temporal` finds the anchor and `_take_latest` validation passes. Run → PASS.
- [ ] **Step 6: Commit** (`feat(3c2bia): SPIKE — injected-template operand rollup + compile proven against real frontier`).

---

### Task 2: Multi-source contracts

**Files:** Create `src/featuregen/overlay/upload/planner/multisource_contracts.py`; Test `.../test_multisource_contracts.py`.

**Interfaces produced:** `SemanticRole`, `PathAggregation`, `FinalOperation`, `MultiSourceReason` (StrEnums); `PathStrategyV1`, `GovernedSourceBindingV1`, `OperandSlotV1`, `FinalExpressionV1`, `MultiSourcePlannerIntentV1`, `GovernedEndpointV1`, `PhysicalLandingV1`, `OperandPathV1`, `MultiSourceBoundingMetricsV1`, `MultiSourceReplayEnvelopeV1`, `MultiSourceBindingPlanV1`, `MultiSourcePlanningResultV1`; `PATH_AGG_TO_FUNCTION: dict[PathAggregation, AggregationFunction | None]`.

- [ ] **Step 1: Failing test** — construct a RATIO intent with a `take_latest` denominator carrying `ordering_anchor_concept`, assert frozen/slotted; a `PhysicalLandingV1` with two `grain_key_refs`; `PATH_AGG_TO_FUNCTION[PathAggregation.sum] is AggregationFunction.sum` and `PATH_AGG_TO_FUNCTION[PathAggregation.stddev] is None`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the module exactly per spec §3 (all fields as listed: `OperandPathV1.binding_plan: BindingPlanV1`, `governed_endpoints`; `GovernedSourceBindingV1` = `source_grain_entity`/`source_grain_key_refs`/`grain_fact_key`, no key fact; `PathStrategyV1.ordering_anchor_concept: str | None`; `MultiSourceReplayEnvelopeV1` fields over fact_keys; `physical_read_set: PhysicalReadSetV1`; `MultiSourceBoundingMetricsV1.landing_ambiguous`). `PATH_AGG_TO_FUNCTION` = `{sum:sum, min:min, max:max, take_latest:take_latest, count:count, count_distinct:count, avg:None, stddev:None}`. `MultiSourceReason` StrEnum lists every §9 disposition incl. `unsupported_path_aggregation`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): multi-source typed contracts + path-agg mapping`).

---

### Task 3: Operation matrix + shape validation

**Files:** Create `.../multisource_operation.py`; Test `.../test_multisource_operation.py`.

**Interfaces:** `OPERATION_MATRIX`, `validate_operation_shape(intent) -> MultiSourceReason | None`.

- [ ] **Step 1: Failing test** — valid RATIO (take_latest denom + anchor) → `None`; IDENTITY over COUNTED → `operand_shape_invalid`; TREND without window → `operand_shape_invalid`; duplicate `slot_id` → `operand_shape_invalid`; `time_slot_id` pointing at a MEASURE → `operand_shape_invalid`; a `stddev` measure → `unsupported_path_aggregation`; `take_latest` without `ordering_anchor_concept` → `ordering_anchor_missing`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** per spec §4: the closed matrix (allowed `PathAggregation` per slot, window/time requirements, order-sensitivity); **exact role→slot validation** (multiset of roles; each `ordered_slot_id`/`time_slot_id` references a real, correctly-roled, distinct slot; no duplicate ids); `stddev` → `unsupported_path_aggregation`; `take_latest` ⇒ `ordering_anchor_concept` present else `ordering_anchor_missing`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): operation→slot→path-strategy matrix + exact shape validation`).

---

### Task 4: GovernedEndpointV1 — grain-fact endpoint revalidation

**Files:** Create `.../multisource_endpoints.py`; Test `.../test_multisource_endpoints.py`.

**Interfaces:** `governed_endpoint(conn, adapter, *, catalog, table_ref, now) -> GovernedEndpointV1 | None`.

- [ ] **Step 1: Failing test** — a table with a VERIFIED grain fact → `GovernedEndpointV1` whose `grain_key_refs` are the fact's short columns **qualified** to `table_ref` and validated against `graph_node.column_name`, and `grain_fact_key` set; a table with only advisory `is_grain` (no fact) → `None`; a composite grain fact → multi-element `grain_key_refs`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `resolve_fact(conn, adapter, table_ref, "grain", now=now)`; when `grain and grain.value is not None`, qualify each short column to `table_ref` (`f"{table_ref}.{col}"`), verify each exists in `graph_node` (`column_name` membership), and return `GovernedEndpointV1(catalog, table_ref, tuple(qualified), grain.fact_key)`; else `None`. Read `table_fact_projection.py`'s `resolve_fact("grain")` call site first for the adapter/ref pattern.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): GovernedEndpointV1 grain-fact endpoint revalidation`).

---

### Task 5: Per-operand path enumeration (typed result)

**Files:** Create `.../multisource_assembly.py`; Test `.../test_multisource_enumeration.py`.

**Interfaces:** `OperandEnumerationResultV1{candidates, status, reason_codes, bounds}`; `enumerate_operand_paths(conn, adapter, ctx, *, operand, target_entity, scope, roles, now) -> OperandEnumerationResultV1`. Each candidate carries the `BindingPlanV1`, the re-derived landing `(catalog, table_ref)`, and the landing `GovernedEndpointV1`.

- [ ] **Step 1: Failing test** — the bridged operand yields ≥1 candidate whose landing endpoint is governed; an operand reachable only without any VERIFIED bridge → status carries `no_governed_path`; an operand whose landing has no grain fact → `realization_endpoint_ungoverned`; truncation at `MAX_PATHS_PER_OPERAND` sets the bound.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — reuse Task 1's `run_operand_rollup` per governed path (build the injected template incl. the second temporal need when `take_latest`); **re-derive the landing** `(catalog, table_ref)` from the plan's `path_segments` (mirror `check_connectivity`'s execution-table logic); revalidate the landing (Task 4) → drop ungoverned; classify empties with the right reason (never a bare empty tuple); cap at `MAX_PATHS_PER_OPERAND`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): per-operand governed path enumeration (typed result)`).

---

### Task 6: Convergence + ranking

**Files:** Modify `.../multisource_assembly.py`; Test `.../test_multisource_convergence.py`.

**Interfaces:** `ConvergenceResultV1{landed_combinations, status, reason_codes, bounds}`; `converge(operand_results, *, bounds) -> ConvergenceResultV1`.

- [ ] **Step 1: Failing test** — two operands both reaching one `PhysicalLandingV1` (catalog+table+`grain_key_refs`) → one landed combination; operands sharing no landing → `no_common_physical_grain`; two distinct landings tied at the top **semantic** rank → `ambiguous_physical_grain` + `landing_ambiguous` (tie detected BEFORE stable ordering); composite-grain landing preserved.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — intersect per-operand landing sets on full `PhysicalLandingV1` identity; cap the product at `MAX_OPERAND_COMBINATIONS`; rank by `_AUTHORITY_RANK` → fewest total crossings; detect a top-semantic-rank tie across distinct landings and return `ambiguous_physical_grain` **before** applying stable-identity presentation order; record bounds.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): exact physical-landing convergence + deterministic ranking`).

---

### Task 7: Per-path checks via reuse (aggregation + temporal)

**Files:** Modify `.../multisource_assembly.py`; Test `.../test_multisource_checks.py`.

**Interfaces:** `check_operand_path(ctx, operand_path) -> tuple[TemporalDeclarationV1, tuple[HopAggregationV1,...], MultiSourceReason | None]`; `check_paths_temporal_consistency(operand_paths) -> MultiSourceReason | None`; A-owned `check_time_slot_take_latest(operand_path) -> MultiSourceReason | None`.

- [ ] **Step 1: Failing test** — a non-additive measure with `sum` over a fan-in → `aggregation_unsafe_on_path`; `take_latest` measure with its anchor bound → ok; two paths with incompatible as-of semantics → `temporal_paths_incompatible`; a RECENCY TIME-slot `take_latest` validated by A's own check (since `compile_aggregation` stages MEASURE-only).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `check_operand_path` runs `check_connectivity(ctx, plan).placement` → `compile_temporal(ctx, plan, template)` → `compile_aggregation(ctx, plan, template, temporal, placement)` and maps unsafe stages to `aggregation_unsafe_on_path`; `check_paths_temporal_consistency` compares per-path PIT treatments for as-of consistency at the landing; `check_time_slot_take_latest` implements A's own ordering-anchor validation for TIME-slot operands (compile_aggregation never sees them).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): per-path aggregation/temporal checks via reuse + A-owned time-slot check`).

---

### Task 8: Final combination + compile_multi_source_contract + union freshness

**Files:** Create `.../multisource_compile.py`; Test `.../test_multisource_compile.py`.

**Interfaces:** `MultiSourceContractSpecV1`; `compile_multi_source_contract(conn, ctx, plan, spec, *, base_envelope) -> MultiSourceBindingPlanV1`; `union_freshness(conn, ctx, plan) -> ...` (calls `revalidate_freshness` with a union-catalog synthetic plan); a multi-source `make_contract_id`-style hash.

- [ ] **Step 1: Failing test** — a plan whose paths are individually fresh but whose union hits a stale watermark → the union check fails; a consistent plan → `resolved` with a **deterministic** `contract_id` across two runs (distinct run ids, same fact_keys); `CompileBudget.remaining` decremented by 1 per compile.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — per-path declaration checks (reuse Task 7); **union freshness by CALLING** `revalidate_freshness` with a synthetic `BindingPlanV1` whose `participating_catalogs` = the union (do NOT edit `revalidate_freshness`); final-combination well-typedness at the landing + `output_additivity` coherence; `contract_id`/`contract_input_hash`/`contract_output_hash` over landing + paths + `path_strategy`s + final expression + versions; decrement `CompileBudget`; re-query `entity_bridge_edge` for `confirmed_event_id` (audit only).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): compile_multi_source_contract + union freshness (call, don't edit)`).

---

### Task 9: plan_multi_source orchestration

**Files:** Create `.../multisource_plan.py`; Test `.../test_multisource_plan.py`.

**Interfaces:** `plan_multi_source(conn, adapter, *, intent, scope, roles, now, ctx=None, budget=None) -> MultiSourcePlanningResultV1`.

- [ ] **Step 1: Failing test** — the valid RATIO intent resolves (one selected candidate, preservation holds, `selected_plan_id` set); a shape-invalid intent → `operand_shape_invalid`, no candidates; an operand needing an ungoverned endpoint → `realization_endpoint_ungoverned`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the spec §5 order: shape (Task 3) → enumerate per operand (Task 5) → converge (Task 6) → per-path checks (Task 7) → final join + **preservation assertion** (every operand + slot once; else `operand_or_slot_not_preserved`) → `compile_multi_source_contract` (Task 8) → select best → assemble `MultiSourcePlanningResultV1` with bounds + `MultiSourceReplayEnvelopeV1` (keyed on fact_keys). Fail-closed each step; DB errors propagate (harness classifies technical).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): plan_multi_source orchestration`).

---

### Task 10: Migration 1006 + shadow store

**Files:** Create `src/featuregen/db/migrations/1006_multisource_assembly_shadow.sql`; Create `.../multisource_shadow_store.py`; Test `.../test_multisource_shadow_store.py`.

**Interfaces:** `write_manifest(conn, rec)`; `write_intent_result(conn, intent_row, candidate_rows, operand_rows)`; `reconcile(conn, run_id) -> ReconcileResultV1`; row dataclasses with the four axes.

- [ ] **Step 1: Failing test** — manifest with an expected set of 2 intents; write 1 intent result with 2 candidate rows; `reconcile` reports the missing intent; a re-write with the **same** payload hash is idempotent; a re-write with a **different** payload hash raises (divergent-duplicate); the four axis columns persist.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — migration `1006` mirrors `0999` (WORM + REVOKE) with four tables: `multisource_assembly_shadow_dispatch` (PK `run_id`; `expected_intent_ids` jsonb; versions jsonb), `..._intent_result` (PK `(run_id,intent_id)`; `semantic_outcome`/`compile_completeness`/`technical_status`/`capture_status` text with CHECK vocabularies; `normalized_intent_hash`; `selected_plan_id`; `reason_codes` jsonb), `..._candidate` (PK `(run_id,intent_id,plan_id)`; `physical_landing` jsonb; `contract_input_hash`; `contract_output_hash`; `read_set_hash`; `replay_envelope_hash`; `rank` int; `declaration_evidence` jsonb), `..._operand_obs` (PK `(run_id,intent_id,plan_id,slot_id)`; `pin` jsonb; `role`; `path_strategy` jsonb; `governed_endpoints` jsonb; `source_binding` jsonb). Store fns mirror `shadow_store.py`; writes **read back and compare payload hash** (divergent-duplicate → error), not `ON CONFLICT DO NOTHING`.
- [ ] **Step 4: Run → PASS** (apply the migration in the test DB fixture first).
- [ ] **Step 5: Commit** (`feat(3c2bia): migration 1006 + shadow store (per-candidate, divergent-duplicate detection, 4 axes)`).

---

### Task 11: Two-connection shadow harness + CLI entrypoint

**Files:** Create `.../multisource_shadow.py`; Test `.../test_multisource_shadow.py`.

**Interfaces:** `run_multisource_assembly_shadow(*, planning_conn, telemetry_conn, adapter, intents, run_id, roles, now, monotonic=time.monotonic) -> tuple[MultiSourcePlanningResultV1, ...]`; a CLI/admin entrypoint that reads `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`, builds the sealed-config adapter + table refs, and invokes the harness.

- [ ] **Step 1: Failing test** — a run over 2 gold intents writes the manifest on `telemetry_conn` FIRST, plans each on `planning_conn` (fixture txn), retains results in memory, rolls back `planning_conn`, persists on `telemetry_conn`, reconciles clean; an injected DB error in one intent records `technical_status=technical_failure` without poisoning the others or the manifest; a budget-exhausting run records `capture_status` truncation.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** mirroring `shadow.py::run_shadow_planner` but two-connection: `write_manifest(telemetry_conn, ...)` first; own the mutable `CompileBudget`; per-intent `with planning_conn.transaction():` savepoint isolating DB errors → `technical_failure`; collect results in memory; after all intents, roll back the fixture transaction on `planning_conn`; `write_intent_result(telemetry_conn, ...)`; `reconcile(telemetry_conn, run_id)`; budget truncation → `budget_truncated`. The flag is read in the CLI entrypoint, never in the harness. Document the two-connection contract in the entrypoint docstring.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): two-connection shadow harness + CLI entrypoint`).

---

### Task 12: Partitioned gold set + assembly gate

**Files:** Create `.../multisource_gold.py`, `.../multisource_gate.py`; Test `.../test_multisource_gate.py`.

**Interfaces:** `seed_gold(conn)` (via real governance write paths → deterministic fact_keys); `CORRECTNESS_GOLD`, `FAULT_CONTROLS`; `evaluate_assembly_gate(...) -> AssemblyGateResultV1`.

- [ ] **Step 1: Failing test** — the gate PASSES on a correct implementation over the correctness population (each positive resolves to the exact expected landing incl. `grain_key_refs`, per-slot `path_strategy`, `final_expression`); FAILS if any positive does not resolve; a fault-control case passes only when exactly classified and is excluded from the clean population; the gate FAILS on a technical failure in the clean population; the correctness population covers `MULTISOURCE_GOLD_MIN_SHAPES` (6) distinct authoritative shapes; double-run determinism uses distinct `run_id`s + stable authored `fact_key`s.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** — `seed_gold` seeds VERIFIED grain facts + entity_bridge facts + VERIFIED intra-catalog joins + drift watermarks + projection checkpoints **through the real governance write paths** (deterministic `fact_key`s). `CORRECTNESS_GOLD` = spec §11 correctness cases with immutable `expected` outcomes; `FAULT_CONTROLS` = injected DB error + budget truncation with expected exact classification. `evaluate_assembly_gate`: run the harness twice (distinct run ids) over correctness gold; assert positive coverage ≥ `MULTISOURCE_GOLD_MIN_SHAPES`, zero substitution/loss, zero non-governed crossings/endpoints in resolves, one-grain landing, correct per-path aggregation/temporal, identical `contract_id`/`replay_envelope_hash` across runs, complete reconciliation, no technical/truncation in the clean population; separately assert each fault control exactly classified.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): partitioned gold (real-governance-seeded) + assembly gate`).

---

### Task 13: Behaviour-neutrality golden test

**Files:** Test `.../test_multisource_behaviour_neutral.py`.

- [ ] **Step 1: Write the test** — with the flag unset, a representative single-source `plan_bindings` run is byte-identical (`plan_id`s, `selected_plan_id`, reason codes, bounds) to a golden captured from `origin/main`; no `multisource_assembly_shadow_*` table is written on a normal considered-set path; importing every new `multisource_*` module has no import-time side effect; grep-assert that no reused function (`assemble_paths`/`semantic_rollup_paths`/`compile_temporal`/`compile_aggregation`/`revalidate_freshness`/`check_connectivity`) was modified on this branch (`git diff origin/main -- planner/assembly.py planner/declarations.py` touches none of them).
- [ ] **Step 2: Run → PASS** (additive by construction; a failure means a new module has a global side effect or a reused function was edited — fix it).
- [ ] **Step 3: Commit** (`test(3c2bia): behaviour-neutrality — single-source byte-identical, reused fns untouched`).

---

## Self-Review

**Spec coverage:** §1 reuse model → Task 1 (spike) + Tasks 5/7; §2 authority basis → Tasks 4/5/8; §3 contracts → Task 2; §4 matrix + agg mapping → Tasks 2/3; §5 steps → Tasks 5–9; §6 compiler → Task 8; §7 harness/store → Tasks 10/11; §8 typed results → Tasks 5/6; §9 dispositions → Task 2 (`MultiSourceReason`), exercised 5–9; §10–11 gold/gate → Task 12; §12 behaviour-neutrality → Task 13.

**Placeholder scan:** Task 1 (spike) and Task 2 carry full code; Tasks 3–12 give exact new signatures + concrete test assertions + the exact reused functions to call (each names the file to read first). No "TBD"/"add error handling"/"similar to Task N".

**Type consistency:** `MultiSourceReason`/`SemanticRole`/`PathAggregation`/`FinalOperation` + all `*V1` dataclasses defined in Task 2, consumed unchanged in 3–13; `OperandPathV1.binding_plan: BindingPlanV1`, `GovernedSourceBindingV1.grain_fact_key` (no key fact), `PathStrategyV1.ordering_anchor_concept`, `PhysicalLandingV1.grain_key_refs`, `MultiSourceReplayEnvelopeV1` fact-key-keyed — all consistent. `plan_multi_source`/`compile_multi_source_contract`/`run_multisource_assembly_shadow` signatures match across producer/consumer tasks. The spike's `run_operand_rollup`/`build_operand_context`/`injected_operand_template` are reused by Task 5.
