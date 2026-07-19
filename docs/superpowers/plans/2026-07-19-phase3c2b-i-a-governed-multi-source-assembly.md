# Phase 3C.2b-i-A — Governed Multi-Source Operand Assembly (Shadow) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the planner a shadow-only capability to combine operands originating in different catalogs into one governed computation at one exact physical grain, proven against a partitioned gold set.

**Architecture:** New sibling contracts + a multi-operand assembly engine (independent governed VERIFIED-bridge path per operand → exact physical-landing convergence → per-path aggregation/temporal proofs → one compile-end union freshness check → final join + expression → new `compile_multi_source_contract`), driven by an authored synthetic gold set through a flag-gated shadow harness with an `0999`-style manifest/reconciliation store. Nothing surfaces; single-source planning is byte-identical when the flag is off.

**Tech Stack:** Python 3.12, `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum` (NOT pydantic), psycopg, pytest. All under `src/featuregen/overlay/upload/planner/`.

**Spec:** `docs/superpowers/specs/2026-07-19-phase3c2b-i-a-governed-multi-source-assembly-design.md`

## Global Constraints

- **Shadow-only:** log/measure, never surface; no data plane; no signing. (Spec shared invariants 1, 7.)
- **F4 preserved:** output is a contract definition with a governed physical plan, never an attested cross-catalog `approved_join`.
- **Fail-closed:** missing/ambiguous/conflicting/ungoverned/lossy input rejects; never guess.
- **Authority, not display:** structural grain/key comes from governed grain/key facts (`resolve_fact`), never `graph_node.concept`/`is_grain`.
- **Preservation is proof:** a resolve preserves every operand, its semantic slot, and the operation; "compiled" alone is not proof.
- **Technical ≠ semantic ≠ capture-incomplete:** DB/infra = technical; budget truncation = capture-incomplete; neither is a semantic reject and neither is a resolve.
- **Behaviour-neutral:** flag off ⇒ single-source `plan_bindings`/`enumerate_single_catalog_plans`/`_assemble_rollups`/`compile_contract` byte-identical to `origin/main`.
- **Types:** `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`. Version every policy.
- **A ALONE:** do not implement any B (adapter/worker/hook/concept-authority) surface here.
- **Commit trailer:** every commit ends with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Reused surfaces (read before use — origin/main):** `planner/contracts.py` (version + `MAX_*` constants, `BindingPlanningResultV1`, `BoundingMetricsV1`, `PlannerReplayEnvelopeV1`, `AggregationFunction`, `PlanResolutionStatus`, `ReasonCode`, `SegmentKind`, `BindingPathSegmentV1`); `planner/assembly.py` (`_State`, `_Position`, `_AUTHORITY_RANK`, active-bridge frontier); `catalog_realizations.py` (`object_grain:99`, `key_entity:111` — both advisory; `resolve_fact`-backed grain fact); `resolve.py`/`resolve_fact` (governed fact read; `grain.value["columns"]`, `provenance["confirmed_event_id"]`); `planner/declarations.py` (`compile_contract:937`, `build_compiler_context`, `CompileBudget`, per-check fns, freshness `:816`); `planner/shadow_store.py` (`write_dispatch`, `write_run_and_plans`, `reconcile`) + `db/migrations/0999_planner_shadow_store.sql`; `planner/contract_eval.py`/`contract_gold.py` (gold evaluator pattern).

---

### Task 0: Prerequisite — rebase, migration check, flag constant

**Files:**
- Modify: `src/featuregen/overlay/upload/planner/contracts.py` (append version + flag constants)

- [ ] **Step 1: Rebase the branch onto current origin/main**

```bash
git fetch origin main
git -c rebase.autostash=false rebase origin/main   # branch is docs-only; expect a clean replay
git log --oneline -5   # confirm the 3 spec commits sit on top of origin/main HEAD
```
Expected: clean rebase (spec commits are docs-only). If the working tree is dirty with unrelated WIP, do NOT `git add -A`; the rebase autostash is disabled so a dirty conflict aborts safely — resolve by leaving that WIP untouched.

- [ ] **Step 2: Confirm migration 1005 is free**

```bash
git ls-files 'src/featuregen/db/migrations/1005_*.sql'   # expect: no output
ls src/featuregen/db/migrations/ | tail -4
```
Expected: no `1005_*` file. If taken, use the next free number and update every `1005_` reference in this plan.

- [ ] **Step 3: Add version + flag constants**

In `contracts.py`, after the existing `*_VERSION` block, append:
```python
# 3C.2b-i-A — governed multi-source operand assembly (shadow).
MULTISOURCE_ASSEMBLY_VERSION = "3c2bia.1.0.0"
OPERATION_POLICY_VERSION = "3c2bia.op.1.0.0"
MULTISOURCE_ASSEMBLY_SHADOW_FLAG = "FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW"
# multi-source bounds (§8): per-operand path cap, cross-operand combination cap, whole-plan states.
MAX_PATHS_PER_OPERAND = 8
MAX_OPERAND_COMBINATIONS = 256
MAX_MULTISOURCE_STATES_EXPANDED = 1024
```

- [ ] **Step 4: Verify import + constant access**

Run: `python -c "from featuregen.overlay.upload.planner.contracts import MULTISOURCE_ASSEMBLY_VERSION, MULTISOURCE_ASSEMBLY_SHADOW_FLAG, MAX_PATHS_PER_OPERAND; print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/planner/contracts.py
git commit -m "feat(3c2bia): version + bound constants for multi-source assembly

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 1: Multi-source contracts (typed data)

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_contracts.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_contracts.py`

**Interfaces:**
- Consumes: `contracts.py` (`PlanResolutionStatus`, `ContractResolutionStatus`, `ReasonCode`, `BindingPathSegmentV1`, `PlannerReplayEnvelopeV1`).
- Produces: `SemanticRole`, `PathAggregation`, `FinalOperation`, `PathStrategyV1`, `GovernedSourceBindingV1`, `OperandSlotV1`, `FinalExpressionV1`, `MultiSourcePlannerIntentV1`, `PhysicalLandingV1`, `OperandPathV1`, `MultiSourceBindingPlanV1`, `MultiSourceBoundingMetricsV1`, `MultiSourcePlanningResultV1`, `MultiSourceReason` (StrEnum of A's dispositions).

- [ ] **Step 1: Write the failing test**

```python
# test_multisource_contracts.py
from featuregen.overlay.upload.planner.multisource_contracts import (
    SemanticRole, PathAggregation, FinalOperation, PathStrategyV1, OperandSlotV1,
    GovernedSourceBindingV1, FinalExpressionV1, MultiSourcePlannerIntentV1, PhysicalLandingV1)

def _ratio_intent():
    num = OperandSlotV1(slot_id="op_0", semantic_role=SemanticRole.numerator,
        catalog_source="core_banking", object_ref="public.transactions.amount",
        authoritative_concept="monetary_flow",
        path_strategy=PathStrategyV1(aggregation=PathAggregation.avg, output_type="decimal",
                                     output_additivity="non_additive", external_type_required=False),
        source_binding=GovernedSourceBindingV1(source_grain_entity="transaction",
                                               source_key_ref="public.transactions.customer_id",
                                               grain_fact_event_id="evt-1", key_fact_event_id="evt-2"))
    den = OperandSlotV1(slot_id="op_1", semantic_role=SemanticRole.denominator,
        catalog_source="wealth", object_ref="public.accounts.balance",
        authoritative_concept="monetary_stock",
        path_strategy=PathStrategyV1(aggregation=PathAggregation.take_latest, output_type="decimal",
                                     output_additivity="semi_additive", external_type_required=False),
        source_binding=GovernedSourceBindingV1(source_grain_entity="account",
                                               source_key_ref="public.accounts.customer_id",
                                               grain_fact_event_id="evt-3", key_fact_event_id="evt-4"))
    return MultiSourcePlannerIntentV1(target_entity="customer", operands=(num, den),
        final_expression=FinalExpressionV1(operation=FinalOperation.ratio,
            ordered_slot_ids=("op_0", "op_1"), time_slot_id=None, window=None,
            output_additivity="non_additive"),
        operation_policy_version="3c2bia.op.1.0.0")

def test_intent_is_frozen_and_slotted():
    intent = _ratio_intent()
    assert intent.final_expression.operation is FinalOperation.ratio
    import dataclasses, pytest
    with pytest.raises(dataclasses.FrozenInstanceError):
        intent.operands = ()

def test_physical_landing_supports_composite_grain():
    land = PhysicalLandingV1(catalog="core_banking", table_ref="public.customer",
                             grain_key_refs=("public.customer.customer_id", "public.customer.region"))
    assert len(land.grain_key_refs) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/planner/test_multisource_contracts.py -v`
Expected: FAIL — `ModuleNotFoundError: multisource_contracts`.

- [ ] **Step 3: Write the contracts module**

```python
# multisource_contracts.py
"""3C.2b-i-A typed contracts (shadow). Frozen slotted dataclasses + lowercase-snake StrEnums.
Siblings to BindingPlanV1/BindingPlanningResultV1 — single-source planning is untouched."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import StrEnum
from featuregen.overlay.upload.planner.contracts import (
    BindingPathSegmentV1, ContractResolutionStatus, PlanResolutionStatus, PlannerReplayEnvelopeV1)


class SemanticRole(StrEnum):
    measure = "measure"; time = "time"; counted = "counted"
    numerator = "numerator"; denominator = "denominator"
    minuend = "minuend"; subtrahend = "subtrahend"


class PathAggregation(StrEnum):
    avg = "avg"; sum = "sum"; min = "min"; max = "max"; stddev = "stddev"
    take_latest = "take_latest"; count = "count"; count_distinct = "count_distinct"


class FinalOperation(StrEnum):
    identity = "identity"; count = "count"; count_distinct = "count_distinct"
    recency = "recency"; trend = "trend"; ratio = "ratio"; difference = "difference"


class MultiSourceReason(StrEnum):
    # semantic
    operand_shape_invalid = "operand_shape_invalid"
    unverified_crossing_required = "unverified_crossing_required"
    realization_endpoint_ungoverned = "realization_endpoint_ungoverned"
    no_common_physical_grain = "no_common_physical_grain"
    ambiguous_physical_grain = "ambiguous_physical_grain"
    aggregation_unsafe_on_path = "aggregation_unsafe_on_path"
    temporal_paths_incompatible = "temporal_paths_incompatible"
    source_binding_ungoverned = "source_binding_ungoverned"
    resolved = "resolved"
    # technical / capture
    operand_or_slot_not_preserved = "operand_or_slot_not_preserved"
    technical_failure = "technical_failure"
    budget_truncated = "budget_truncated"


@dataclass(frozen=True, slots=True)
class PathStrategyV1:
    aggregation: PathAggregation
    output_type: str
    output_additivity: str            # "additive"|"semi_additive"|"non_additive"|"n/a"
    external_type_required: bool = False


@dataclass(frozen=True, slots=True)
class GovernedSourceBindingV1:
    source_grain_entity: str
    source_key_ref: str
    grain_fact_event_id: str          # the VERIFIED grain fact backing source_grain_entity
    key_fact_event_id: str            # the VERIFIED key fact backing source_key_ref


@dataclass(frozen=True, slots=True)
class OperandSlotV1:
    slot_id: str
    semantic_role: SemanticRole
    catalog_source: str
    object_ref: str
    authoritative_concept: str
    path_strategy: PathStrategyV1
    source_binding: GovernedSourceBindingV1


@dataclass(frozen=True, slots=True)
class FinalExpressionV1:
    operation: FinalOperation
    ordered_slot_ids: tuple[str, ...]     # order-sensitive ops rely on this order
    time_slot_id: str | None              # references a TIME operand slot; never a raw time_ref
    window: str | None
    output_additivity: str


@dataclass(frozen=True, slots=True)
class MultiSourcePlannerIntentV1:
    target_entity: str
    operands: tuple[OperandSlotV1, ...]
    final_expression: FinalExpressionV1
    operation_policy_version: str


@dataclass(frozen=True, slots=True)
class PhysicalLandingV1:
    catalog: str
    table_ref: str
    grain_key_refs: tuple[str, ...]       # multi-column grains: join on EVERY key


@dataclass(frozen=True, slots=True)
class OperandPathV1:
    slot_id: str
    semantic_role: SemanticRole
    catalog_source: str
    object_ref: str
    path_segments: tuple[BindingPathSegmentV1, ...]
    source_to_landing_key_map: tuple[tuple[str, str], ...]   # (source_key_ref, landing_key_ref) — A-derived
    path_strategy: PathStrategyV1
    pit_treatment: str


@dataclass(frozen=True, slots=True)
class MultiSourceBoundingMetricsV1:
    paths_per_operand_truncated: bool
    operand_combinations_truncated: bool
    states_truncated: bool
    total_states_expanded: int


@dataclass(frozen=True, slots=True)
class MultiSourceBindingPlanV1:
    plan_id: str
    physical_landing: PhysicalLandingV1
    operand_paths: tuple[OperandPathV1, ...]
    final_expression: FinalExpressionV1
    physical_read_set: tuple[str, ...]
    resolution_status: PlanResolutionStatus
    reason_codes: tuple[MultiSourceReason, ...]
    contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled
    contract_id: str | None = None        # this plan's OWN declaration identity (never a selected id)


@dataclass(frozen=True, slots=True)
class MultiSourcePlanningResultV1:
    run_id: str | None
    target_entity: str
    candidate_plans: tuple[MultiSourceBindingPlanV1, ...]
    selected_plan_id: str | None
    result_status: PlanResolutionStatus
    primary_reason_code: MultiSourceReason | None
    reason_codes: tuple[MultiSourceReason, ...]
    bounding: MultiSourceBoundingMetricsV1
    replay_envelope: PlannerReplayEnvelopeV1
    contract_result_status: ContractResolutionStatus = ContractResolutionStatus.not_compiled
    selected_contract_plan_id: str | None = None
    selected_contract_id: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/featuregen/overlay/upload/planner/test_multisource_contracts.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/planner/multisource_contracts.py tests/featuregen/overlay/upload/planner/test_multisource_contracts.py
git commit -m "feat(3c2bia): multi-source assembly typed contracts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Operation → slot → path-strategy matrix + shape validation

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_operation.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_operation.py`

**Interfaces:**
- Consumes: Task 1 types.
- Produces: `OPERATION_MATRIX` (mapping `FinalOperation` → required `(SemanticRole, count, allowed PathAggregation set)` spec + window requirement + whether ordered), `validate_operation_shape(intent) -> MultiSourceReason | None`.

- [ ] **Step 1: Write the failing test**

```python
from featuregen.overlay.upload.planner.multisource_operation import validate_operation_shape
from featuregen.overlay.upload.planner.multisource_contracts import MultiSourceReason
# reuse _ratio_intent() helper (copy into this test module)

def test_valid_ratio_shape_ok():
    assert validate_operation_shape(_ratio_intent()) is None

def test_identity_requires_measure_not_counted():
    intent = _identity_intent_with_counted()   # IDENTITY over a COUNTED slot
    assert validate_operation_shape(intent) is MultiSourceReason.operand_shape_invalid

def test_trend_requires_window_and_time_slot():
    intent = _trend_intent_without_window()
    assert validate_operation_shape(intent) is MultiSourceReason.operand_shape_invalid

def test_count_distinct_requires_counted_operand():
    assert validate_operation_shape(_count_distinct_intent()) is None
```

- [ ] **Step 2: Run to verify fail**

Run: `pytest tests/featuregen/overlay/upload/planner/test_multisource_operation.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the matrix + validator**

```python
# multisource_operation.py
"""The CLOSED operation→slot→path-strategy matrix (spec §3). Total; any mismatch → operand_shape_invalid."""
from __future__ import annotations
from dataclasses import dataclass
from featuregen.overlay.upload.planner.multisource_contracts import (
    FinalOperation, MultiSourcePlannerIntentV1, MultiSourceReason, PathAggregation, SemanticRole)

_MEASURE_AGG = frozenset({PathAggregation.avg, PathAggregation.sum, PathAggregation.min,
                          PathAggregation.max, PathAggregation.stddev})

@dataclass(frozen=True, slots=True)
class _SlotSpec:
    role: SemanticRole
    allowed: frozenset          # allowed PathAggregation for this slot

@dataclass(frozen=True, slots=True)
class _OpSpec:
    slots: tuple[_SlotSpec, ...]
    requires_window: bool
    requires_time_slot: bool
    order_sensitive: bool

OPERATION_MATRIX: dict[FinalOperation, _OpSpec] = {
    FinalOperation.identity: _OpSpec((_SlotSpec(SemanticRole.measure, _MEASURE_AGG),), False, False, False),
    FinalOperation.count: _OpSpec((_SlotSpec(SemanticRole.counted, frozenset({PathAggregation.count})),), False, False, False),
    FinalOperation.count_distinct: _OpSpec((_SlotSpec(SemanticRole.counted, frozenset({PathAggregation.count_distinct})),), False, False, False),
    FinalOperation.recency: _OpSpec((_SlotSpec(SemanticRole.time, frozenset({PathAggregation.take_latest})),), False, True, False),
    FinalOperation.trend: _OpSpec((_SlotSpec(SemanticRole.measure, _MEASURE_AGG),
                                   _SlotSpec(SemanticRole.time, frozenset({PathAggregation.take_latest}))), True, True, False),
    FinalOperation.ratio: _OpSpec((_SlotSpec(SemanticRole.numerator, _MEASURE_AGG),
                                   _SlotSpec(SemanticRole.denominator, _MEASURE_AGG)), False, False, True),
    FinalOperation.difference: _OpSpec((_SlotSpec(SemanticRole.minuend, _MEASURE_AGG),
                                        _SlotSpec(SemanticRole.subtrahend, _MEASURE_AGG)), False, False, True),
}

def validate_operation_shape(intent: MultiSourcePlannerIntentV1) -> MultiSourceReason | None:
    spec = OPERATION_MATRIX.get(intent.final_expression.operation)
    if spec is None:
        return MultiSourceReason.operand_shape_invalid
    want = sorted((s.role for s in spec.slots), key=str)
    have = sorted((o.semantic_role for o in intent.operands), key=str)
    if want != have:
        return MultiSourceReason.operand_shape_invalid
    by_role = {}
    for o in intent.operands:
        by_role.setdefault(o.semantic_role, []).append(o)
    for s in spec.slots:
        matches = by_role.get(s.role, [])
        if len(matches) != 1 or matches[0].path_strategy.aggregation not in s.allowed:
            return MultiSourceReason.operand_shape_invalid
    fe = intent.final_expression
    if spec.requires_window and not fe.window:
        return MultiSourceReason.operand_shape_invalid
    if spec.requires_time_slot and fe.time_slot_id is None:
        return MultiSourceReason.operand_shape_invalid
    if spec.order_sensitive and len(set(fe.ordered_slot_ids)) != len(spec.slots):
        return MultiSourceReason.operand_shape_invalid
    # every ordered_slot_id and time_slot_id must reference a real slot
    ids = {o.slot_id for o in intent.operands}
    if not set(fe.ordered_slot_ids).issubset(ids) or (fe.time_slot_id and fe.time_slot_id not in ids):
        return MultiSourceReason.operand_shape_invalid
    return None
```

- [ ] **Step 4: Run to verify pass** (write the `_identity_intent_with_counted`/`_trend_intent_without_window`/`_count_distinct_intent` helpers in the test)

Run: `pytest tests/featuregen/overlay/upload/planner/test_multisource_operation.py -v`
Expected: PASS.

- [ ] **Step 5: Commit** (`feat(3c2bia): closed operation→slot→path-strategy matrix + shape validation`)

---

### Task 3: GovernedRealizationV2 — endpoint revalidation against governed facts

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_realizations.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_realizations.py`

**Interfaces:**
- Consumes: `resolve.py`/`resolve_fact` (governed fact read), `catalog_realizations.py` (for the advisory baseline to reject).
- Produces: `governed_grain_key_refs(conn, catalog, table_ref, *, now) -> tuple[str, ...] | None`; `governed_key_entity(conn, catalog, column_ref, *, now) -> tuple[str, str] | None` (entity, key_fact_event_id); `revalidate_endpoint(conn, catalog, table_ref, *, now) -> GovernedRealizationV2 | None`.

- [ ] **Step 1: Write the failing test** (DB fixture: a table with a VERIFIED grain fact vs one whose grain is only advisory `is_grain`)

```python
def test_governed_grain_from_verified_fact_only(db_with_verified_grain):
    keys = governed_grain_key_refs(db_with_verified_grain, "core_banking", "public.customer", now=NOW)
    assert keys == ("public.customer.customer_id",)

def test_advisory_is_grain_without_fact_is_none(db_with_advisory_is_grain_only):
    assert governed_grain_key_refs(db_with_advisory_is_grain_only, "core_banking", "public.customer", now=NOW) is None

def test_revalidate_endpoint_rejects_ungoverned(db_with_advisory_is_grain_only):
    assert revalidate_endpoint(db_with_advisory_is_grain_only, "core_banking", "public.customer", now=NOW) is None
```

- [ ] **Step 2: Run to verify fail.** Run: `pytest .../test_multisource_realizations.py -v` → FAIL (module missing).

- [ ] **Step 3: Implement** — read the governed grain fact via `resolve_fact(conn, adapter, table_ref, "grain", now=now)`; return `tuple(grain.value["columns"])` **only** when `grain.value is not None` and the fact is VERIFIED (carries `provenance["confirmed_event_id"]`); else `None`. `GovernedRealizationV2` is a frozen dataclass `{catalog, table_ref, grain_key_refs, grain_fact_event_id}`. `revalidate_endpoint` returns it only when grain keys are governed. Mirror the `resolve_fact` call site pattern used in `table_fact_projection.py` (read that file first).

- [ ] **Step 4: Run to verify pass.** Expected: PASS.
- [ ] **Step 5: Commit** (`feat(3c2bia): GovernedRealizationV2 endpoint revalidation vs governed grain/key facts`)

---

### Task 4: Multi-operand path enumeration + convergence + ranking

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_assembly.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_assembly.py`

**Interfaces:**
- Consumes: `assembly.py` (`_State`/`_Position`/`_AUTHORITY_RANK`, active-bridge frontier — read fully first), Task 1 types, Task 3 `revalidate_endpoint`, `contracts.MAX_PATHS_PER_OPERAND`/`MAX_OPERAND_COMBINATIONS`/`MAX_MULTISOURCE_STATES_EXPANDED`.
- Produces: `enumerate_operand_paths(conn, operand, target_entity, *, scope, roles, now) -> tuple[_OperandPathCandidate, ...]`; `converge(operand_path_sets) -> tuple[list[_LandedCombination], MultiSourceBoundingMetricsV1]` (exact convergence on `PhysicalLandingV1` incl. `grain_key_refs`, deterministic ranking, bounded).

- [ ] **Step 1: Write the failing test** — two operands in different catalogs, each reachable to `customer` via one VERIFIED bridge to a common `public.customer` table with `grain_key_refs=("public.customer.customer_id",)`; assert `converge` returns exactly one landed combination at that landing; a second test where the two operands share no reachable common landing → empty + no crash; a third where two landings tie → recorded ambiguity (empty selection, flag set).

- [ ] **Step 2: Run → FAIL.**

- [ ] **Step 3: Implement.** Per operand: run the single-source frontier from the operand's pinned source position, but (a) pin the operand column, (b) require every crossing VERIFIED, (c) require each endpoint to pass `revalidate_endpoint` (Task 3), capping at `MAX_PATHS_PER_OPERAND`. Each path ends at a `PhysicalLandingV1` (catalog, table_ref, **governed** grain_key_refs). `converge`: intersect the per-operand landing sets on the full `PhysicalLandingV1` identity (catalog+table+grain_key_refs); cap the cross-operand product at `MAX_OPERAND_COMBINATIONS`; rank landings by `_AUTHORITY_RANK` → fewest total crossings → stable identity order; if the top rank ties across distinct landings, record `ambiguous_physical_grain` (no selection). Record truncations on `MultiSourceBoundingMetricsV1`. This step does NOT compile or check aggregation/temporal (Tasks 5–6).

- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): multi-operand path enumeration + exact physical convergence + ranking`)

---

### Task 5: Per-path pure aggregation + temporal checks

**Files:**
- Modify: `src/featuregen/overlay/upload/planner/multisource_assembly.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_checks.py`

**Interfaces:**
- Consumes: the additivity/aggregation evaluator + temporal-rule evaluator (find them in `declarations.py`; read before use), Task 1 types.
- Produces: `check_path_aggregation(operand, path) -> MultiSourceReason | None`; `check_paths_temporal(operand_paths) -> MultiSourceReason | None` (per-path validity + cross-path as-of consistency). **Pure** — no freshness, no clock-dependent graph reads (freshness is Task 6's union check).

- [ ] **Step 1: Failing test** — a non-additive measure with a `sum` strategy across a fan-out path → `aggregation_unsafe_on_path`; a `take_latest` on a semi-additive stock → ok; two operand paths with incompatible as-of semantics → `temporal_paths_incompatible`; compatible → `None`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** reusing the existing additivity/aggregation classification + temporal-rule functions per path; the cross-path temporal check compares each path's PIT treatment for as-of consistency at the landing.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): pure per-path aggregation + temporal compatibility checks`)

---

### Task 6: compile_multi_source_contract + compile-end union freshness

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_compile.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_compile.py`

**Interfaces:**
- Consumes: `declarations.py` (`build_compiler_context`, `CompileBudget`, the single-path connectivity/safety/aggregation/temporal checks, the freshness check `:816`, `make_contract_id`), Task 1 types.
- Produces: `MultiSourceContractSpecV1` (injected declarations: per-operation aggregation function, output additivity, window, temporal requirements); `compile_multi_source_contract(conn, ctx, plan, spec, *, base_envelope) -> MultiSourceBindingPlanV1`.

- [ ] **Step 1: Failing test** — a plan whose two paths are individually fresh but reference catalogs whose union fails one freshness watermark → `contract_result_status` reflects the union freshness failure; a fully-consistent plan → `resolved` with a stable `contract_id` (identity deterministic across two runs with the same inputs).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** For each `OperandPathV1`: run the existing single-path connectivity/safety/aggregation/temporal declaration checks (reuse, do not reimplement). Then ONE union freshness/consistency check over the union of catalogs/realizations/bridges/structural fact ids (extend the single-plan freshness at `declarations.py:816` to the union — read it first). Then final-combination checks (final expression well-typed at the landing grain; `output_additivity` coherent with the per-path outputs). Compute `contract_id` via a deterministic identity over landing + paths + strategies + final expression + versions (mirror `make_contract_id`). Declarations come from `spec` (production `build_compiler_context` supplies an empty agg-declaration registry — inject here). Return the plan with `contract_result_status`/`contract_id` set.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): compile_multi_source_contract + compile-end union freshness`)

---

### Task 7: Result orchestration — plan_multi_source

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_plan.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_plan.py`

**Interfaces:**
- Consumes: Tasks 2, 4, 5, 6, `_envelope`/`PlannerReplayEnvelopeV1` from `plan.py`/`contracts.py`.
- Produces: `plan_multi_source(conn, *, intent, scope, roles, now, ctx=None, budget=None) -> MultiSourcePlanningResultV1`.

- [ ] **Step 1: Failing test** — the valid RATIO intent resolves: `result_status == resolved`, one selected candidate landing all operands, `selected_plan_id` set, preservation holds; a shape-invalid intent → `operand_shape_invalid`, no candidates; an intent whose operand needs an unverified crossing → `unverified_crossing_required`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the orchestration in spec §5 order: `validate_operation_shape` (Task 2) → `enumerate_operand_paths`+`converge` (Task 4) → per-path aggregation/temporal (Task 5) → final join + **preservation assertion** (every operand + slot survives once; else `operand_or_slot_not_preserved`) → `compile_multi_source_contract` (Task 6) → select best candidate → assemble `MultiSourcePlanningResultV1` with bounds + replay envelope. Fail-closed at every step; a raised DB error is NOT caught here (the harness savepoint classifies it technical).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): plan_multi_source orchestration → MultiSourcePlanningResultV1`)

---

### Task 8: Migration 1005 + shadow store

**Files:**
- Create: `src/featuregen/db/migrations/1005_multisource_assembly_shadow.sql`
- Create: `src/featuregen/overlay/upload/planner/multisource_shadow_store.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_shadow_store.py`

**Interfaces:**
- Consumes: `shadow_store.py` (`write_dispatch`/`write_run_and_plans`/`reconcile` patterns — read first), `db/migrations/0999_planner_shadow_store.sql` (schema pattern).
- Produces: `write_manifest(conn, rec)`; `write_intent_result(conn, run_result, observations)` (two-phase); `reconcile(conn, run_id) -> ReconcileResultV1`; row dataclasses carrying **separate axes**: `semantic_outcome`, `compile_completeness`, `technical_status`, `capture_status`.

- [ ] **Step 1: Failing test** — write a manifest with an expected set of 2 intents; write one intent result; `reconcile` reports the missing one; a second write to the same `(run_id, intent_id)` is idempotent; the row records the four separate axes.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** Migration `1005` mirrors `0999`: `multisource_assembly_shadow_dispatch` (PK `run_id`, expected_intent_ids jsonb, versions jsonb, append-only), `multisource_assembly_shadow_intent_result` (PK `(run_id, intent_id)`, columns for `semantic_outcome`, `compile_completeness`, `technical_status`, `capture_status`, `normalized_intent_hash`, `selected_plan_id`, `physical_landing` jsonb, reason_codes jsonb), `multisource_assembly_shadow_operand_obs` (PK `(run_id, intent_id, slot_id)`, pin/role/path_strategy/crossings/endpoint-fact-ids/source-binding-provenance). Append-only (no UPDATE/DELETE). Store fns mirror `shadow_store.py` exactly (idempotent manifest; two-phase; reconcile = every manifest intent has a result row).
- [ ] **Step 4: Run → PASS** (run the migration in the test DB fixture first).
- [ ] **Step 5: Commit** (`feat(3c2bia): migration 1005 + multi-source shadow store (manifest/reconciliation, separated axes)`)

---

### Task 9: Shadow harness — runnable entrypoint + fixture/persistence boundary

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_shadow.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_shadow.py`

**Interfaces:**
- Consumes: Task 7 `plan_multi_source`, Task 8 store, `CompileBudget`, `MULTISOURCE_ASSEMBLY_SHADOW_FLAG`.
- Produces: `run_multisource_assembly_shadow(conn, *, intents, run_id, roles, now, monotonic=time.monotonic) -> tuple[MultiSourcePlanningResultV1, ...]`.

- [ ] **Step 1: Failing test** — a run over 2 gold intents writes the manifest FIRST, plans each in a per-intent savepoint, two-phase-writes results, reconciles clean; an injected DB error in one intent records `technical_status=technical_failure` and does not poison the others or the manifest; a run over a budget-exhausting set records `capture_status` truncation. Verify results persist even though gold fixtures are set up inside a transaction (persist on a boundary outside the fixture rollback — see Step 3).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** mirroring `shadow.py::run_shadow_planner`: write manifest first (before any planning); own the mutable `CompileBudget` across intents; per-intent `with conn.transaction():` savepoint isolates DB errors → `technical_failure`; two-phase store write caught so the manifest survives; budget truncation → `budget_truncated` capture status. **Fixture/persistence boundary:** the runnable entrypoint accepts the connection and commits shadow rows on it; tests that build gold fixtures inside a rollback-only transaction must pass a *separate* committed connection (or commit before teardown) so `reconcile` is meaningful — document this in the entrypoint docstring and assert it in the test. Flag read happens in the CALLER, never here (harness stays pure), matching `shadow.py`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): multi-source shadow harness entrypoint + fixture/persistence boundary`)

---

### Task 10: Partitioned gold set + assembly gate

**Files:**
- Create: `src/featuregen/overlay/upload/planner/multisource_gold.py`
- Create: `src/featuregen/overlay/upload/planner/multisource_gate.py`
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_gate.py`

**Interfaces:**
- Consumes: `contract_gold.py`/`contract_eval.py` (evaluator pattern — read first), Tasks 7/9.
- Produces: `CORRECTNESS_GOLD` (immutable expected outcomes; positive must-resolve + negative exact-code), `FAULT_CONTROLS` (injected-fault cases), `evaluate_assembly_gate(conn, ...) -> AssemblyGateResultV1` (pass iff the spec §10 criteria hold over the correctness population; fault controls pass only when exactly classified and are EXCLUDED from the clean population).

- [ ] **Step 1: Failing test** — the gate PASSES on a correct implementation over the correctness population (positive cases resolved with exact expected landing/paths/slots/expression; negatives exact-code); the gate FAILS if any positive case does not resolve (reject-all cannot pass); a fault-control case passes when exactly classified and is not counted in the clean population; the gate FAILS on a technical failure in the clean population.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement.** `CORRECTNESS_GOLD` = the spec §11 correctness cases with immutable `expected` outcomes (landing incl. `grain_key_refs`, per-slot `path_strategy`, `final_expression`, reason code). `FAULT_CONTROLS` = injected DB error + budget truncation, `expected` = exact technical/capture classification. `evaluate_assembly_gate`: run the harness over the correctness gold; require positive coverage (≥1 must-resolve), zero operand substitution/loss, zero unverified crossings / ungoverned endpoints in resolves, one-grain landing, correct per-path aggregation/temporal, deterministic identity (run twice, compare), complete reconciliation, no technical/truncation in the clean population; separately assert each fault control is exactly classified. Encode the minimum-distinct-authoritative-shapes requirement as a coverage assertion.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** (`feat(3c2bia): partitioned gold set + assembly gate`)

---

### Task 11: Behaviour-neutrality golden test

**Files:**
- Test: `tests/featuregen/overlay/upload/planner/test_multisource_behaviour_neutral.py`

- [ ] **Step 1: Write the test** — with `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW` unset, assert a representative single-source `plan_bindings` run produces byte-identical `BindingPlanningResultV1` (same `plan_id`s, `selected_plan_id`, reason codes, bounds) to a captured golden from `origin/main`; assert no `multisource_*` table is written on a normal (non-shadow) considered-set path; assert importing the new modules has no import-time side effects on the single-source path.
- [ ] **Step 2: Run → PASS** (the new code is additive; if it fails, a new module has a global side effect — fix it).
- [ ] **Step 3: Commit** (`test(3c2bia): behaviour-neutrality — single-source path byte-identical, no shadow writes when flag off`)

---

## Self-Review

**Spec coverage:** §2 contracts → Task 1; §3 matrix → Task 2; §4 GovernedRealizationV2 → Task 3; §5 steps 1–3 (enumeration/convergence) → Task 4; §5 steps 4–5 (per-path checks) → Task 5; §5 step 8 + §6 compiler + union freshness → Task 6; §5 steps 6–7 + orchestration → Task 7; §7 store + migration 1005 → Task 8; §7 entrypoint/fixture boundary → Task 9; §10–11 gold + gate → Task 10; §12 behaviour-neutrality → Task 11; §8 enumeration bounds → Task 4 (+ constants Task 0). Dispositions (§9) → Task 1 (`MultiSourceReason`), exercised across Tasks 4–7.

**Placeholder scan:** Tasks 3–10 describe algorithms and reference existing functions by exact path/name rather than inlining every line — each names the file to read first and the exact functions to reuse, with concrete test assertions and the new signatures fully typed. Tasks 1–2 carry complete code. This is the appropriate granularity for reuse-heavy planner work; no "TBD"/"add error handling"/"similar to Task N".

**Type consistency:** `MultiSourceReason`/`SemanticRole`/`PathAggregation`/`FinalOperation` and all `*V1` dataclasses are defined in Task 1 and consumed unchanged in Tasks 2–11; `PhysicalLandingV1.grain_key_refs`, `OperandPathV1.source_to_landing_key_map`, and `MultiSourceBindingPlanV1.contract_id` (own, not selected) are used consistently. `plan_multi_source`/`compile_multi_source_contract`/`run_multisource_assembly_shadow` signatures match across producing and consuming tasks.
