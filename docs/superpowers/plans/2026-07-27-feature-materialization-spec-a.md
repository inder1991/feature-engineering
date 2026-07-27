# Spec A — Executable Materialization Vertical Slice: Implementation Plan (rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A published `sandbox_feature.cif_daily` partition on the real Hadoop/Hive cluster, computed by generated Kedro/PySpark from three governed formulas, with the numbers proven by execution.

**Definition of done is a live table, not a green suite.** Task 15 is the deliverable; everything before it is scaffolding for it.

**Architecture:** `src/featuregen/materialize/` compiles a governed `ResolvedFeatureInput` through per-expression IR → contract → group plan → a **complete runnable Kedro project**, which is then submitted, validated, executed and published. Render-only: the generated project is the sole execution path.

**Spec:** `docs/superpowers/specs/2026-07-27-feature-materialization-spec-a-design.md` **rev 2** (386 lines, §0–§N). Read the cited section before implementing a task.

> **Rev 2 of this plan.** Rev 1 was rejected by a code-grounded review for twelve defects, four of which would have produced silently wrong feature values. Do not consult rev 1.

## Global Constraints

- **Frozen slotted dataclasses + `StrEnum`** — NOT pydantic.
- **One hasher:** `materialize_hash()` (Task 1), wrapping `featuregen.formula._jcs.dumps` + sha256. Identity fields only — no provenance, no timestamps, no live observations.
- **Reuse governed machinery.** Joins → `classify_join_path`. Sensitivity gating → `allowed_sensitivities`. C1 reads → `read_operational_value`. Never re-implement these.
- **Cardinality is correctness.** A `1:N` step toward the grain multiplies rows and inflates a SUM. Collapse fan-out before joining.
- **No scan sharing in this slice.** Each feature computes independently.
- **Sandbox only.** `derive_namespace()` returns `sandbox_feature` unconditionally; there is no parameter that changes it.
- **Render-only.** No `pyspark` import anywhere in `src/featuregen/materialize/`; PySpark appears only inside rendered text.
- **Manifests/findings carry counts, types, hashes, locations — never data values.**
- **Fail closed** on anything ungoverned, unverified, denied or unresolvable.
- **`INSERT OVERWRITE` is forbidden.**
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

**Test command:**
```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
```

## Verified Child-1 interfaces (use these EXACT names)

Confirmed against `main` on 2026-07-27. Do not guess; these are correct.

```python
TypedLiteral(type: LiteralType, value: str)
FilterPredicate(op: FilterPredicateOp, left: LogicalRef,
                right_literal: TypedLiteral | None = None,
                right_param: ParameterRef | None = None,
                right_set: tuple[TypedLiteral, ...] | None = None)   # `kind` is init=False
FilterBool(op: FilterBoolOp, children: tuple[FilterNode, ...])       # `children`, NOT `operands`
SourceRelation(table_ref: LogicalRef)                                # TABLE ref, no column
Grain(entity: str, keys: tuple[LogicalRef, ...])                     # key ORDER IS SEMANTIC
WindowPolicy(event_time_ref, basis: WindowBasis, length: int, unit: WindowUnit,
             start_inclusive: Inclusivity, end_inclusive: Inclusivity,
             timezone: str, empty_window: EmptyWindowResult, null_input: NullInput)
AggregateExpression(aggregation: AggregateFunction, operand: LogicalRef | None,
                    source_relation: SourceRelation, filter: FilterNode | None,
                    window: WindowPolicy)                            # operand None IFF COUNT_ROWS
AuthoringIntent(name: str, hypothesis: str, target_entity: str,
                target_grain_keys: tuple[str, ...] = ())             # `name` is the feature name
```

**There is no `right_ref` on `FilterPredicate`.** A filter's only column reference is `left`.

Governed machinery:
```python
classify_join_path(conn, catalog_source: str, from_table: str, to_table: str,
                   *, roles: Iterable[str] = ()) -> JoinOutcome
  # JoinOutcome.kind ∈ {OPERATIONAL, UNVERIFIED, DENIED, NO_PATH}
  # .steps: tuple[JoinStep(from_ref, to_ref, cardinality), ...]  — oriented to traversal
read_operational_value(conn, logical_ref: str, field_name: str) -> OperationalValue
read_column_facts(conn, logical_ref: str, field_name: str) -> OperationalColumnFacts
  # -> {value, authority, provenance} ONLY. No sensitivity/access/retention.
allowed_sensitivities(roles) -> set[str]     # featuregen.overlay.upload.read_scope
parse_ref(logical_ref) -> (source, schema, table, column | None)
```

---

## File Structure

```
src/featuregen/materialize/
  canonical.py       T1   materialize_hash()
  admission.py       T2   ResolvedFeatureInput + admission checks + role authorization
  joins.py           T3   JoinPlan over classify_join_path
  spine.py           T4   SpineSpec resolution
  expression_ir.py   T5   ExpressionExecutionIR + PitSpec (per expression)
  ir.py              T6   FormulaExecutionIRV1 + ir_hash
  classify.py        T7   versioned sensitivity/access/retention adapter
  contract.py        T7   MaterializationContractV1
  group_plan.py      T8   FeatureGroupPlanV1 + StagingManifestV1 + completeness
  identity.py        T9   CompilationIdentity / RenderedArtifactIdentity, sandbox namespace
  render/
    project.py       T10  render_project() -> complete runnable directory
    nodes_compute.py T11  spine, PIT projection, calculate_*, fan-out collapse
    nodes_gate.py    T12  assemble (manifest-consuming), §H gates, hooks
    publish.py       T14  publish node
  publish.py         T14  capability attestation + GroupPublisher
  validation.py      T13  ValidationReportV1 + classification + L0/L1/L2
  submit.py          T13  PipelineSubmitter + LocalClusterSubmitter
  control_plane.py   T12  ingest of generation/reports/run events
  pipeline.py        T15  generate_group() end-to-end entry point
src/featuregen/db/migrations/1021_materialization_control_plane.sql   T12
tests/featuregen/materialize/…
tests/featuregen/materialize/fixtures.py     T2  hand-authored formulas (verified fields)
tests/featuregen/materialize/spark_fixtures/ T13 tiny hand-authored data
```

---

### Task 1: `materialize_hash`

**Files:** Create `src/featuregen/materialize/__init__.py`, `canonical.py`; Test `tests/featuregen/materialize/test_canonical.py`

**Produces:** `materialize_hash(payload: Mapping[str, Any]) -> str`

- [ ] **Step 1: Failing test**

```python
import pytest
from featuregen.materialize.canonical import materialize_hash


def test_key_order_irrelevant():
    assert materialize_hash({"a": 1, "b": 2}) == materialize_hash({"b": 2, "a": 1})


def test_sha256_hex():
    h = materialize_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_values_distinguished():
    assert materialize_hash({"a": 1}) != materialize_hash({"a": 2})


def test_rejects_non_mapping():
    with pytest.raises(TypeError):
        materialize_hash([1])  # type: ignore[arg-type]
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError`)
- [ ] **Step 3: Implement**

```python
# src/featuregen/materialize/canonical.py
"""The ONE canonicalizer for every hash this package mints."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from featuregen.formula._jcs import dumps as _jcs_dumps

__all__ = ["materialize_hash"]


def materialize_hash(payload: Mapping[str, Any]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError(f"materialize_hash expects a mapping, got {type(payload).__name__}")
    return hashlib.sha256(_jcs_dumps(dict(payload))).hexdigest()
```

- [ ] **Step 4: Run — expect PASS (4)**
- [ ] **Step 5: Commit** — `feat(materialize): JCS+sha256 hasher`

---

### Task 2: `ResolvedFeatureInput` — the only public input

Spec §0. A bare formula must not be materializable.

**Files:** Create `src/featuregen/materialize/admission.py`, `tests/featuregen/materialize/fixtures.py`; Test `tests/featuregen/materialize/test_admission.py`

**Consumes:** `featuregen.formula.result.AuthoringResult`; `featuregen.formula.turns.AuthoringIntent`; `featuregen.formula.canonical.formula_content_hash`; `featuregen.formula.authoring` (for `authoring_intent_hash`); `allowed_sensitivities`.

**Produces:** `ResolvedFeatureInput` (frozen: `intent`, `result`); `AdmissionRefused(Exception)` with `.code`; `admit(conn, inputs: Sequence[ResolvedFeatureInput], *, roles: Iterable[str]) -> tuple[AdmittedFeature, ...]` where `AdmittedFeature` is frozen `(feature_name: str, formula: TypedFormulaV1, formula_content_hash: str, intent: AuthoringIntent)`.

- [ ] **Step 1: Write the hand-authored fixtures (VERIFIED field names)**

```python
# tests/featuregen/materialize/fixtures.py
"""Hand-authored fixtures. Field names verified against src/featuregen/formula/schema.py."""
from __future__ import annotations

from featuregen.formula.schema import (
    AdditivityClass, AggregateExpression, AggregateFunction, DecimalPolicy, EmptyWindowResult,
    FilterPredicate, FilterPredicateOp, FormulaOutputPolicyV1, Grain, Inclusivity, LiteralType,
    NullInput, OverflowBehavior, RatioBody, RoundingMode, SourceRelation, TypedFormulaV1,
    TypedLiteral, UnaryBody, WindowBasis, WindowPolicy, WindowUnit,
)
from featuregen.formula.turns import AuthoringIntent

SRC = "hdfc"
TXN = f"{SRC}::banking.transactions"
AMOUNT = f"{TXN}.amount"
TXN_DATE = f"{TXN}.transaction_date"
POSTED_AT = f"{TXN}.posted_at"
TXN_TYPE = f"{TXN}.transaction_type"
MERCHANT = f"{TXN}.merchant_id"
IS_CROSS_BORDER = f"{TXN}.is_cross_border"
ACCOUNT_ID = f"{TXN}.account_id"
CIF_ID = f"{SRC}::banking.accounts.cif_id"
CUSTOMERS = f"{SRC}::banking.customers"


def _window(length: int, unit: WindowUnit = WindowUnit.DAY) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=TXN_DATE, basis=WindowBasis.TRAILING, length=length, unit=unit,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.INCLUSIVE,
        timezone="Asia/Dubai", empty_window=EmptyWindowResult.ZERO, null_input=NullInput.IGNORE)


def _eq(left: str, value: str) -> FilterPredicate:
    # NOTE: `kind` is init=False; there is NO right_ref field.
    return FilterPredicate(op=FilterPredicateOp.EQUAL, left=left,
                           right_literal=TypedLiteral(type=LiteralType.STRING, value=value))


def _decimal() -> DecimalPolicy:
    return DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_UP,
                         overflow=OverflowBehavior.ERROR)


def _grain() -> Grain:
    return Grain(entity="customer", keys=(CIF_ID,))


def total_debit_amount_30d() -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=1, operation_grammar_version=1, output_policy_version=1,
        canonicalization_version=1, grain=_grain(),
        body=UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.SUM, operand=AMOUNT,
            source_relation=SourceRelation(table_ref=TXN),
            filter=_eq(TXN_TYPE, "debit"), window=_window(30))),
        parameters=(), decimal=_decimal(),
        output=FormulaOutputPolicyV1(output_type="numeric", unit=None, currency=None,
                                     output_additivity=AdditivityClass.ADDITIVE,
                                     external_type_required=False))


def distinct_merchant_count_90d() -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=1, operation_grammar_version=1, output_policy_version=1,
        canonicalization_version=1, grain=_grain(),
        body=UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.COUNT_DISTINCT, operand=MERCHANT,
            source_relation=SourceRelation(table_ref=TXN), filter=None, window=_window(90))),
        parameters=(), decimal=_decimal(),
        output=FormulaOutputPolicyV1(output_type="numeric", unit=None, currency=None,
                                     output_additivity=AdditivityClass.NON_ADDITIVE,
                                     external_type_required=False))


def cross_border_value_ratio_90d() -> TypedFormulaV1:
    """RATIO — exercises numerator/denominator, zero-denominator policy and rounding."""
    from featuregen.formula.schema import ZeroDenominator
    return TypedFormulaV1(
        formula_schema_version=1, operation_grammar_version=1, output_policy_version=1,
        canonicalization_version=1, grain=_grain(),
        body=RatioBody(
            numerator=AggregateExpression(
                aggregation=AggregateFunction.SUM, operand=AMOUNT,
                source_relation=SourceRelation(table_ref=TXN),
                filter=_eq(IS_CROSS_BORDER, "true"), window=_window(90)),
            denominator=AggregateExpression(
                aggregation=AggregateFunction.SUM, operand=AMOUNT,
                source_relation=SourceRelation(table_ref=TXN), filter=None,
                window=_window(90)),
            zero_denominator=ZeroDenominator.NULL),
        parameters=(), decimal=_decimal(),
        output=FormulaOutputPolicyV1(output_type="decimal", unit=None, currency=None,
                                     output_additivity=AdditivityClass.NON_ADDITIVE,
                                     external_type_required=False))


def intent_for(name: str) -> AuthoringIntent:
    return AuthoringIntent(name=name, hypothesis=f"{name} per customer per day",
                           target_entity="customer", target_grain_keys=(CIF_ID,))
```

> Run `PYTHONPATH=src .venv/bin/python -c "from tests.featuregen.materialize.fixtures import *; total_debit_amount_30d(); distinct_merchant_count_90d(); cross_border_value_ratio_90d()"` **first**. If any field name is wrong, fix the fixture (not the schema) before continuing.

- [ ] **Step 2: Failing admission tests**

```python
import pytest
from featuregen.formula.canonical import formula_content_hash
from featuregen.materialize.admission import AdmissionRefused, ResolvedFeatureInput, admit
from tests.featuregen.materialize.fixtures import intent_for, total_debit_amount_30d


def test_non_resolved_disposition_is_refused(db, needs_review_result):
    with pytest.raises(AdmissionRefused) as e:
        admit(db, [ResolvedFeatureInput(intent_for("f"), needs_review_result)], roles=("admin",))
    assert e.value.code == "NOT_RESOLVED"


def test_missing_candidate_formula_is_refused(db, resolved_result_without_formula):
    with pytest.raises(AdmissionRefused) as e:
        admit(db, [ResolvedFeatureInput(intent_for("f"), resolved_result_without_formula)],
              roles=("admin",))
    assert e.value.code == "NO_CANDIDATE_FORMULA"


def test_formula_hash_disagreement_is_refused(db, tampered_result):
    with pytest.raises(AdmissionRefused) as e:
        admit(db, [ResolvedFeatureInput(intent_for("f"), tampered_result)], roles=("admin",))
    assert e.value.code == "FORMULA_HASH_MISMATCH"


def test_intent_hash_mismatch_is_refused(db, resolved_result, foreign_intent):
    with pytest.raises(AdmissionRefused) as e:
        admit(db, [ResolvedFeatureInput(foreign_intent, resolved_result)], roles=("admin",))
    assert e.value.code == "INTENT_HASH_MISMATCH"


def test_insufficient_roles_refuse_the_whole_compilation(db, resolved_result, restricted_catalog):
    with pytest.raises(AdmissionRefused) as e:
        admit(db, [ResolvedFeatureInput(intent_for("total_debit_amount_30d"), resolved_result)],
              roles=("catalog_viewer",))
    assert e.value.code == "READ_SCOPE_INSUFFICIENT"


def test_admitted_feature_takes_its_name_from_the_INTENT(db, resolved_result, seeded_catalog):
    out = admit(db, [ResolvedFeatureInput(intent_for("total_debit_amount_30d"), resolved_result)],
                roles=("feature_engineer",))
    assert out[0].feature_name == "total_debit_amount_30d"
    assert out[0].formula_content_hash == formula_content_hash(total_debit_amount_30d())


def test_there_is_no_api_accepting_a_bare_formula():
    import inspect
    import featuregen.materialize.admission as m
    for name, fn in inspect.getmembers(m, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = inspect.signature(fn).parameters
        assert "formula" not in params and "formulas" not in params, (
            f"{name} exposes a raw-formula entry point, bypassing the governed gate")
```

- [ ] **Step 3: Run — expect FAIL**
- [ ] **Step 4: Implement `admission.py`** — the five checks in spec §0 order, each raising `AdmissionRefused` with its code. Role authorization collects **every** ref the formula names (operands, filter `left` refs, event-time refs, grain keys, source tables) and compares each element's `graph_node.sensitivity` against `allowed_sensitivities(roles)`; the spine source is added in Task 4 and this check is extended there.
- [ ] **Step 5: Run — expect PASS (7)**
- [ ] **Step 6: Commit** — `feat(materialize): governed ResolvedFeatureInput admission gate`

---

### Task 3: `JoinPlan` over the existing planner

Spec §A3. **Do not hand-roll join resolution.**

**Files:** Create `src/featuregen/materialize/joins.py`; Test `tests/featuregen/materialize/test_joins.py`

**Produces:** `JoinPlanStep` (frozen: `from_ref`, `to_ref`, `cardinality`); `JoinPlan` (frozen: `steps`, `outcome_kind`, `roles_used`, `fans_out: bool`); `JoinRefused` (frozen: `code`, `detail`); `plan_join(conn, *, catalog_source, from_table, to_table, roles) -> JoinPlan | JoinRefused`.

- [ ] **Step 1: Failing tests**

```python
import pytest
from featuregen.materialize.joins import JoinPlan, JoinRefused, plan_join


def test_operational_path_retains_every_step_and_cardinality(db, verified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.accounts", roles=("feature_engineer",))
    assert isinstance(result, JoinPlan)
    assert result.steps and all(s.cardinality for s in result.steps)


def test_fan_out_is_flagged_when_a_step_is_one_to_many(db, one_to_many_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.accounts",
                       to_table="banking.transactions", roles=("feature_engineer",))
    assert isinstance(result, JoinPlan)
    assert result.fans_out is True     # renderer MUST collapse before joining


def test_unverified_path_is_refused(db, unverified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.accounts", roles=("feature_engineer",))
    assert isinstance(result, JoinRefused) and result.code == "JOIN_PATH_NOT_VERIFIED"


def test_denied_by_read_scope_is_refused_distinctly(db, restricted_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.accounts", roles=("catalog_viewer",))
    assert isinstance(result, JoinRefused)
    assert result.code == "JOIN_PATH_DENIED_BY_READ_SCOPE"


def test_no_path_is_refused(db, empty_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.unrelated", roles=("feature_engineer",))
    assert isinstance(result, JoinRefused) and result.code == "GRAIN_PATH_NOT_GOVERNED"


def test_same_table_needs_no_steps(db, verified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.transactions", roles=("feature_engineer",))
    assert isinstance(result, JoinPlan) and result.steps == ()


def test_roles_are_recorded_in_the_plan(db, verified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table="banking.transactions",
                       to_table="banking.accounts", roles=("feature_engineer",))
    assert result.roles_used == ("feature_engineer",)
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement `joins.py`** — a thin adapter that calls `classify_join_path` and maps `JoinOutcome.kind` per spec §A3's table. `fans_out` is `True` when any step's cardinality expands toward the destination. **No BFS, no bridge scanning, no prefix matching in this module.**
- [ ] **Step 4: Run — expect PASS (7)**
- [ ] **Step 5: Commit** — `feat(materialize): JoinPlan adapter over classify_join_path`

---

### Task 4: `SpineSpec` — the governed entity population

Spec §A4. Without this, "every customer appears" is a lie.

**Files:** Create `src/featuregen/materialize/spine.py`; Test `tests/featuregen/materialize/test_spine.py`

**Produces:** `SpineSpec` (frozen, per spec §A4); `SpineRefused` (frozen: `code="SPINE_SOURCE_NOT_GOVERNED"`, `detail`); `resolve_spine(conn, *, entity, grain_keys, roles, business_dt_column="business_dt") -> SpineSpec | SpineRefused`.

- [ ] **Step 1: Failing tests**

```python
import pytest
from featuregen.materialize.spine import SpineRefused, SpineSpec, resolve_spine


def test_governed_entity_source_resolves(db, customers_entity_source):
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("feature_engineer",))
    assert isinstance(out, SpineSpec)
    assert out.source_table_ref == "hdfc::banking.customers"


def test_absent_entity_source_fails_closed(db, no_entity_source):
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("feature_engineer",))
    assert isinstance(out, SpineRefused) and out.code == "SPINE_SOURCE_NOT_GOVERNED"


def test_spine_never_falls_back_to_a_fact_table(db, only_transactions):
    # A spine built from transactions cannot contain a customer with no transactions.
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("feature_engineer",))
    assert isinstance(out, SpineRefused)


def test_spine_keys_needing_a_hop_carry_a_join_plan(db, customers_via_accounts):
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("feature_engineer",))
    assert isinstance(out, SpineSpec) and out.join_plan is not None


def test_spine_source_hidden_by_read_scope_is_refused(db, restricted_customers):
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("catalog_viewer",))
    assert isinstance(out, SpineRefused)


def test_snapshot_identity_is_recorded(db, customers_entity_source):
    out = resolve_spine(db, entity="customer", grain_keys=("hdfc::banking.accounts.cif_id",),
                        roles=("feature_engineer",))
    assert out.snapshot_identity
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement `spine.py`** — resolve the entity's governed source via the `ENTITY_ASSIGNMENT`/`GRAIN` facts for the entity; require the source's grain fact to be `is_unique` over the entity keys; use `plan_join` (Task 3) when spine keys differ from grain keys; refuse otherwise. Extend Task 2's role check to include the spine source.
- [ ] **Step 4: Run — expect PASS (6)**
- [ ] **Step 5: Commit** — `feat(materialize): governed SpineSpec resolution, fail closed`

---

### Task 5: `ExpressionExecutionIR` — PIT per expression

Spec §A2. Each `AggregateExpression` gets its own read set, join plan and `PitSpec`.

**Files:** Create `src/featuregen/materialize/expression_ir.py`; Test `tests/featuregen/materialize/test_expression_ir.py`

**Produces:** `PhysicalRef` (frozen: `logical_ref`, `schema`, `table`, `column | None`, `role`); `PitSpec` (frozen: `event_time_column`, `availability_column`, `availability_basis`, `lag_hours: float | None`, `window_basis`, `window_length`, `window_unit`, `start_inclusive`, `end_inclusive`, `timezone`); `ExpressionExecutionIR` (frozen, per spec §A2); `ExpressionRefused` (frozen: `code`, `detail`); `compile_expression(conn, *, expr_path, expr, grain_keys, roles) -> ExpressionExecutionIR | ExpressionRefused`; `expression_ir_hash(e) -> str`.

- [ ] **Step 1: Failing tests**

```python
import pytest
from featuregen.materialize.expression_ir import (
    ExpressionExecutionIR, ExpressionRefused, compile_expression, expression_ir_hash,
)
from tests.featuregen.materialize.fixtures import (
    AMOUNT, TXN_DATE, TXN_TYPE, cross_border_value_ratio_90d, total_debit_amount_30d,
)


def test_read_set_includes_operand_filter_and_event_time(db, seeded_catalog):
    e = compile_expression(db, expr_path="body.expr",
                           expr=total_debit_amount_30d().body.expr,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    refs = {r.logical_ref for r in e.physical_read_set}
    assert {AMOUNT, TXN_TYPE, TXN_DATE} <= refs


def test_each_ratio_expression_gets_its_OWN_pit(db, seeded_catalog):
    f = cross_border_value_ratio_90d()
    num = compile_expression(db, expr_path="body.numerator", expr=f.body.numerator,
                             grain_keys=("hdfc::banking.accounts.cif_id",),
                             roles=("feature_engineer",))
    den = compile_expression(db, expr_path="body.denominator", expr=f.body.denominator,
                             grain_keys=("hdfc::banking.accounts.cif_id",),
                             roles=("feature_engineer",))
    assert num.expr_path != den.expr_path
    assert num.pit is not den.pit          # independent specs, not a shared object


def test_expressions_with_different_windows_hash_differently(db, seeded_catalog):
    a = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    f = cross_border_value_ratio_90d()
    b = compile_expression(db, expr_path="body.expr", expr=f.body.denominator,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    assert expression_ir_hash(a) != expression_ir_hash(b)


def test_missing_availability_fact_fails_closed(db, catalog_without_availability):
    out = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                             grain_keys=("hdfc::banking.accounts.cif_id",),
                             roles=("feature_engineer",))
    assert isinstance(out, ExpressionRefused)
    assert out.code == "AVAILABILITY_TIME_NOT_GOVERNED"


def test_ungoverned_join_to_the_grain_fails_closed(db, catalog_without_join):
    out = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                             grain_keys=("hdfc::banking.accounts.cif_id",),
                             roles=("feature_engineer",))
    assert isinstance(out, ExpressionRefused)
    assert out.code in {"GRAIN_PATH_NOT_GOVERNED", "JOIN_PATH_NOT_VERIFIED"}


def test_join_keys_and_hops_enter_the_read_set(db, seeded_catalog):
    e = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    # account_id is not an operand or a filter — it is only a join key, and §C classifies over it.
    assert any(r.column == "account_id" for r in e.physical_read_set)


def test_fan_out_flag_is_carried_from_the_join_plan(db, one_to_many_catalog):
    e = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    assert isinstance(e, ExpressionExecutionIR)
    assert e.join_plan.fans_out is True


def test_hash_excludes_provenance(db, seeded_catalog):
    e = compile_expression(db, expr_path="body.expr", expr=total_debit_amount_30d().body.expr,
                           grain_keys=("hdfc::banking.accounts.cif_id",),
                           roles=("feature_engineer",))
    for banned in ("compiled_at", "roles_used", "run_id"):
        assert banned not in e.identity_payload()
```

- [ ] **Step 2: Run — expect FAIL**
- [ ] **Step 3: Implement `expression_ir.py`** — read `AVAILABILITY_TIME` for the expression's source table (fail closed if absent); build `PitSpec` from that fact plus the expression's own `WindowPolicy`; call `plan_join` (Task 3) from the expression's table to each grain key's table; expand the read set with every join step's `from_ref`/`to_ref` column. Filter refs come from `left` **only** — `FilterPredicate` has no `right_ref`.
- [ ] **Step 4: Run — expect PASS (8)**
- [ ] **Step 5: Commit** — `feat(materialize): per-expression IR with its own PIT and join plan`

---

### Task 6: `FormulaExecutionIRV1`

Spec §A3. Assembles expressions + grain + spine + carried output policy.

**Files:** Create `src/featuregen/materialize/ir.py`; Test `tests/featuregen/materialize/test_ir.py`

**Produces:** `FormulaExecutionIRV1` (frozen); `IrRefused` (frozen: `code`, `detail`); `compile_ir(conn, admitted: AdmittedFeature, *, roles) -> FormulaExecutionIRV1 | IrRefused`; `ir_hash(ir) -> str`.

- [ ] **Step 1: Failing tests**

```python
def test_ratio_produces_two_expression_irs(db, seeded_catalog, admitted_ratio):
    ir = compile_ir(db, admitted_ratio, roles=("feature_engineer",))
    assert {e.expr_path for e in ir.expressions} == {"body.numerator", "body.denominator"}


def test_output_policy_is_carried_not_rederived(db, seeded_catalog, admitted_sum):
    ir = compile_ir(db, admitted_sum, roles=("feature_engineer",))
    assert ir.output_policy == admitted_sum.formula.output


def test_spine_is_part_of_the_ir(db, seeded_catalog, admitted_sum):
    ir = compile_ir(db, admitted_sum, roles=("feature_engineer",))
    assert ir.spine.source_table_ref == "hdfc::banking.customers"


def test_any_expression_refusal_refuses_the_whole_ir(db, catalog_without_availability, admitted_ratio):
    out = compile_ir(db, admitted_ratio, roles=("feature_engineer",))
    assert isinstance(out, IrRefused)


def test_spine_refusal_refuses_the_ir(db, no_entity_source, admitted_sum):
    out = compile_ir(db, admitted_sum, roles=("feature_engineer",))
    assert isinstance(out, IrRefused) and out.code == "SPINE_SOURCE_NOT_GOVERNED"


def test_ir_hash_is_stable(db, seeded_catalog, admitted_sum):
    ir = compile_ir(db, admitted_sum, roles=("feature_engineer",))
    assert ir_hash(ir) == ir_hash(compile_ir(db, admitted_sum, roles=("feature_engineer",)))
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): FormulaExecutionIRV1 over per-expression IRs`

---

### Task 7: Classification adapter and `MaterializationContractV1`

Spec §C. **`read_column_facts` cannot supply sensitivity** — this task builds the versioned adapter.

**Files:** Create `src/featuregen/materialize/classify.py`, `contract.py`; Test `tests/featuregen/materialize/test_classify.py`, `test_contract.py`

**Produces:** `CLASSIFICATION_POLICY_VERSION = 1`; `SensitivityClass` (StrEnum ordered `PUBLIC < INTERNAL < CONFIDENTIAL < RESTRICTED < PROHIBITED`); `classify_read_set(conn, refs) -> Classification` (frozen: `sensitivity_class`, `access_requirements: tuple[str, ...]`, `retention_class`, `policy_version`); `CadenceDecl`; `AvailabilityClass`; `ContractOverrides`; `OverrideRefused`; `MaterializationContractV1`; `derive_contract(conn, irs, *, cadence, availability_class, overrides=None) -> MaterializationContractV1`; `contract_hash(c) -> str`.

- [ ] **Step 1: Failing classification tests**

```python
def test_most_restrictive_element_wins(db, mixed_sensitivity_catalog, ir_refs):
    c = classify_read_set(db, ir_refs)
    assert c.sensitivity_class is SensitivityClass.RESTRICTED


def test_a_join_key_can_be_the_most_restrictive_element(db, restricted_join_key_catalog, ir_refs):
    # account_id is neither operand nor filter — classification must still see it.
    assert classify_read_set(db, ir_refs).sensitivity_class is SensitivityClass.RESTRICTED


def test_the_spine_source_is_classified_too(db, restricted_customers_catalog, ir_refs_with_spine):
    assert classify_read_set(db, ir_refs_with_spine).sensitivity_class is SensitivityClass.RESTRICTED


def test_policy_version_is_recorded(db, mixed_sensitivity_catalog, ir_refs):
    assert classify_read_set(db, ir_refs).policy_version == 1


def test_read_scope_tags_map_into_the_effective_vocabulary(db, pii_tagged_catalog, ir_refs):
    # `pii` is a read-scope tag, not a contract class — it must MAP, not appear verbatim.
    c = classify_read_set(db, ir_refs)
    assert c.sensitivity_class in tuple(SensitivityClass)
```

- [ ] **Step 2: Failing contract tests**

```python
def test_30d_and_90d_share_a_contract(db, ir_30d, ir_90d, daily):
    a = derive_contract(db, [ir_30d], cadence=daily, availability_class=AvailabilityClass.T_PLUS_1)
    b = derive_contract(db, [ir_90d], cadence=daily, availability_class=AvailabilityClass.T_PLUS_1)
    assert contract_hash(a) == contract_hash(b)   # window length is NOT contract identity


def test_contract_hash_excludes_calculation_window(db, ir_30d, daily):
    payload = derive_contract(db, [ir_30d], cadence=daily,
                              availability_class=AvailabilityClass.T_PLUS_1).identity_payload()
    assert "window_length" not in str(payload) and "window_unit" not in str(payload)


def test_hash_excludes_live_observations(db, ir_30d, daily):
    payload = derive_contract(db, [ir_30d], cadence=daily,
                              availability_class=AvailabilityClass.T_PLUS_1).identity_payload()
    for banned in ("current_watermark", "actual_arrival_at", "job_status", "run_id"):
        assert banned not in payload


def test_override_may_only_tighten(db, ir_30d, daily):
    c = derive_contract(db, [ir_30d], cadence=daily, availability_class=AvailabilityClass.T_PLUS_3,
                        overrides=ContractOverrides(availability_class=AvailabilityClass.T_PLUS_4))
    assert c.availability_class is AvailabilityClass.T_PLUS_4


def test_override_may_not_loosen(db, ir_30d, daily):
    with pytest.raises(OverrideRefused):
        derive_contract(db, [ir_30d], cadence=daily, availability_class=AvailabilityClass.T_PLUS_3,
                        overrides=ContractOverrides(availability_class=AvailabilityClass.T_PLUS_1))


def test_invalid_timezone_refused():
    with pytest.raises(ValueError, match="timezone"):
        CadenceDecl(period="daily", timezone="Mars/Olympus", business_date_cutoff="23:59",
                    trigger="scheduled")


def test_dependencies_ready_trigger_refused():
    with pytest.raises(ValueError, match="dependencies_ready"):
        CadenceDecl(period="daily", timezone="Asia/Dubai", business_date_cutoff="23:59",
                    trigger="dependencies_ready")


def test_classification_policy_version_is_hashed(db, ir_30d, daily):
    assert "classification_policy_version" in derive_contract(
        db, [ir_30d], cadence=daily,
        availability_class=AvailabilityClass.T_PLUS_1).identity_payload()
```

- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): versioned classification adapter + MaterializationContractV1`

---

### Task 8: `FeatureGroupPlanV1`, `StagingManifestV1`, completeness by manifest

Spec §D.

**Files:** Create `src/featuregen/materialize/group_plan.py`; Test `tests/featuregen/materialize/test_group_plan.py`

**Produces:** `PlannedFeature`; `FeatureGroupPlanV1`; `StagingManifestV1`; `GroupPlanError`; `build_group_plan(contract, entries)`; `group_plan_hash(plan)`; `expected_schema(plan)`; `check_completeness(plan, manifests, observed_schema) -> tuple[CompletenessFinding, ...]`.

- [ ] **Step 1: Failing tests**

```python
def test_missing_manifest_fails_even_when_schema_matches(three_feature_plan, full_schema):
    manifests = [m for m in _all_manifests() if m.intent_feature_name != "distinct_merchant_count_90d"]
    findings = check_completeness(three_feature_plan, manifests, full_schema)
    assert any(f.code == "MISSING_STAGING_MANIFEST" for f in findings)


def test_ir_hash_mismatch_fails_even_when_schema_matches(three_feature_plan, full_schema):
    manifests = _all_manifests()
    manifests[0] = replace(manifests[0], ir_hash="deadbeef")
    findings = check_completeness(three_feature_plan, manifests, full_schema)
    assert any(f.code == "IR_HASH_MISMATCH" for f in findings)
    # THE POINT: schema equality cannot prove which IR computed a column.


def test_failed_status_fails(three_feature_plan, full_schema):
    manifests = _all_manifests()
    manifests[1] = replace(manifests[1], status="failed")
    assert any(f.code == "INCOMPLETE_COMPUTATION"
               for f in check_completeness(three_feature_plan, manifests, full_schema))


def test_missing_column_fails(three_feature_plan):
    findings = check_completeness(three_feature_plan, _all_manifests(),
                                  (("cif_id", "string"), ("business_dt", "date")))
    assert any(f.code == "MISSING_FEATURE_COLUMN" for f in findings)


def test_extra_column_fails(three_feature_plan, full_schema):
    assert any(f.code == "UNEXPECTED_COLUMN" for f in check_completeness(
        three_feature_plan, _all_manifests(), full_schema + (("surprise", "string"),)))


def test_wrong_type_fails(three_feature_plan, full_schema):
    broken = tuple((c, "string") if c == "total_debit_amount_30d" else (c, t)
                   for c, t in full_schema)
    assert any(f.code == "WRONG_COLUMN_TYPE"
               for f in check_completeness(three_feature_plan, _all_manifests(), broken))


def test_complete_group_yields_nothing(three_feature_plan, full_schema):
    assert check_completeness(three_feature_plan, _all_manifests(), full_schema) == ()


def test_adding_a_feature_changes_plan_not_contract(contract, two_entries, three_entries):
    a, b = build_group_plan(contract, two_entries), build_group_plan(contract, three_entries)
    assert a.materialization_contract_hash == b.materialization_contract_hash
    assert group_plan_hash(a) != group_plan_hash(b)


def test_name_collision_is_an_error(contract, colliding_entries):
    with pytest.raises(GroupPlanError, match="collide"):
        build_group_plan(contract, colliding_entries)
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): group plan + completeness proven by staging manifests`

---

### Task 9: Two-phase identity, sandbox-only namespace

Spec §B.

**Files:** Create `src/featuregen/materialize/identity.py`; Test `tests/featuregen/materialize/test_identity.py`

**Produces:** `CompilationIdentity`; `RenderedArtifactIdentity`; `derive_namespace() -> str`; `sandbox_execution_hash(...) -> str`.

- [ ] **Step 1: Failing tests**

```python
def test_namespace_is_always_sandbox():
    assert derive_namespace() == "sandbox_feature"


def test_there_is_no_production_parameter():
    import inspect
    from featuregen.materialize import identity
    assert inspect.signature(identity.derive_namespace).parameters == {}
    src = inspect.getsource(identity)
    assert '"feature"' not in src and "'feature'" not in src


def test_compilation_identity_is_plural(compilation_identity):
    assert isinstance(compilation_identity.formula_content_hashes, tuple)
    assert len(compilation_identity.formula_content_hashes) == 3
    assert len(compilation_identity.ir_hashes) == 3


def test_rendered_identity_is_built_AFTER_rendering(compilation_identity):
    r = RenderedArtifactIdentity(compilation=compilation_identity,
                                 generated_project_hash="a" * 64)
    assert r.compilation is compilation_identity     # no circular dependency


def test_project_hash_is_not_embedded_in_hashed_files(rendered_project):
    for path, text in rendered_project.files.items():
        assert rendered_project.generated_project_hash not in text, (
            f"{path} embeds the hash that covers it — self-referential")


def test_sandbox_execution_hash_is_reproducible(compilation_identity):
    kw = dict(generated_project_hash="a" * 64, environment_id="hdfc-local",
              resolved_parameter_values={"business_dt": "2026-07-27"},
              business_dt="2026-07-27", input_snapshot_ids=("banking.transactions/2026-07-27",),
              compiler_version=1, renderer_version=1, capability_attestation_id="cap_1")
    assert sandbox_execution_hash(compilation_identity, **kw) == \
           sandbox_execution_hash(compilation_identity, **kw)
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): two-phase non-circular identity, sandbox-only`

---

### Task 10: `render_project()` — a complete runnable Kedro project

Spec §F. Not fragments — a directory that runs.

**Files:** Create `src/featuregen/materialize/render/project.py`; Test `tests/featuregen/materialize/test_render_project.py`

**Produces:** `RENDERER_VERSION = 1`; `RenderedProject` (frozen: `files: Mapping[str, str]`, `generated_project_hash`); `render_project(...) -> RenderedProject`; `materialize_to(project, path) -> None`.

- [ ] **Step 1: Failing tests**

```python
REQUIRED = (
    "pyproject.toml", "requirements.lock", "README.md", "GENERATED.lock",
    "conf/base/catalog.yml", "conf/base/parameters.yml", "conf/base/logging.yml",
    "src/featuregen_materialized/__init__.py",
    "src/featuregen_materialized/settings.py",
    "src/featuregen_materialized/pipeline_registry.py",
    "src/featuregen_materialized/hooks.py",
    "src/featuregen_materialized/pipelines/materialize/__init__.py",
    "src/featuregen_materialized/pipelines/materialize/nodes.py",
    "src/featuregen_materialized/pipelines/materialize/pipeline.py",
)


def test_every_required_file_is_emitted(render_args):
    files = render_project(**render_args).files
    for path in REQUIRED:
        assert path in files, f"missing {path} — the project would not run"


def test_pipeline_wires_nodes_with_explicit_inputs_and_outputs(render_args):
    src = render_project(**render_args).files[
        "src/featuregen_materialized/pipelines/materialize/pipeline.py"]
    assert "inputs=" in src and "outputs=" in src and "node(" in src


def test_pipeline_registry_exposes_default(render_args):
    src = render_project(**render_args).files["src/featuregen_materialized/pipeline_registry.py"]
    assert "__default__" in src


def test_settings_registers_the_hooks(render_args):
    src = render_project(**render_args).files["src/featuregen_materialized/settings.py"]
    assert "HOOKS" in src and "MetricsHook" in src and "ProvenanceHook" in src


def test_readme_states_how_to_run_and_the_submit_distinction(render_args):
    readme = render_project(**render_args).files["README.md"]
    assert "kedro run" in readme and "spark-submit" in readme


def test_generated_lock_is_detached_from_the_hashed_files(render_args):
    p = render_project(**render_args)
    import json
    lock = json.loads(p.files["GENERATED.lock"])
    assert lock["generated_project_hash"] == p.generated_project_hash
    # and the hash must be computed over the OTHER files only
    assert "GENERATED.lock" not in _hashed_paths(p)


def test_catalog_names_only_read_set_and_spine_tables(render_args):
    catalog = render_project(**render_args).files["conf/base/catalog.yml"]
    assert "banking.transactions" in catalog and "banking.customers" in catalog
    assert "banking.unrelated" not in catalog


def test_target_is_sandbox_and_not_parameterised(render_args):
    files = render_project(**render_args).files
    assert "sandbox_feature.cif_daily" in files["conf/base/catalog.yml"]
    assert "publication_target" not in files["conf/base/parameters.yml"]


def test_render_is_deterministic(render_args):
    assert render_project(**render_args).files == render_project(**render_args).files


def test_every_rendered_python_file_parses(render_args):
    import ast
    for path, text in render_project(**render_args).files.items():
        if path.endswith(".py"):
            ast.parse(text)


def test_materialize_to_writes_a_real_directory(tmp_path, render_args):
    p = render_project(**render_args)
    materialize_to(p, tmp_path)
    assert (tmp_path / "conf/base/catalog.yml").exists()
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render a complete runnable Kedro project`

---

### Task 11: Rendered compute nodes — spine, PIT projection, per-feature calculation

Spec §G. Correctness core; independent per feature (no sharing).

**Files:** Create `src/featuregen/materialize/render/nodes_compute.py`; Test `tests/featuregen/materialize/test_render_compute.py`

**Produces:** `render_spine_node(spine)`; `render_pit_projection(expr_ir)`; `render_calculate_node(feature, ir)` (writes `StagingManifestV1`).

- [ ] **Step 1: Failing tests**

```python
def test_spine_reads_the_governed_entity_source(spine_spec):
    src = render_spine_node(spine_spec)
    assert "banking.customers" in src and "banking.transactions" not in src


def test_availability_gate_uses_the_governed_column(expr_ir_posted):
    assert "posted_at" in render_pit_projection(expr_ir_posted)


def test_lagged_basis_renders_its_lag(expr_ir_lagged):
    assert "6" in render_pit_projection(expr_ir_lagged)


def test_calendar_window_is_not_converted_to_days(expr_ir_monthly):
    src = render_pit_projection(expr_ir_monthly)
    assert "30" not in src           # a calendar month is NOT 30 days


def test_projection_selects_only_read_set_columns(expr_ir_posted):
    src = render_pit_projection(expr_ir_posted)
    assert "select(" in src and "*" not in src


def test_fan_out_is_collapsed_before_joining(expr_ir_fanout):
    src = render_calculate_node(_feature(), _ir_with(expr_ir_fanout))
    # Aggregate/de-duplicate BEFORE the join, or a 1:N hop inflates the SUM.
    assert "dropDuplicates" in src or src.index("groupBy") < src.index("join")


def test_calculate_writes_a_staging_manifest_with_its_ir_hash(feature, ir):
    src = render_calculate_node(feature, ir)
    assert "StagingManifest" in src or "staging_manifest" in src
    assert ir.ir_hash[:8] in src


def test_each_feature_writes_its_own_staging_output(feature, ir):
    assert "feature_staging" in render_calculate_node(feature, ir)


def test_ratio_renders_both_expressions_and_zero_denominator_policy(ratio_feature, ratio_ir):
    src = render_calculate_node(ratio_feature, ratio_ir)
    assert src.count("groupBy") >= 2
    assert "when" in src              # zero-denominator -> NULL policy


def test_rendered_compute_parses(feature, ir):
    import ast
    ast.parse(render_calculate_node(feature, ir))
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render spine, PIT projection, per-feature calculation`

---

### Task 12: Rendered gates, hooks, and the control plane

Spec §H, §J.

**Files:** Create `src/featuregen/materialize/render/nodes_gate.py`, `src/featuregen/materialize/control_plane.py`, `src/featuregen/db/migrations/1021_materialization_control_plane.sql`; Test `test_render_gate.py`, `test_control_plane.py`, `test_migration_1021.py`

**Produces:** `render_assemble(plan)`; `render_validate(plan)`; `render_hooks(compilation_identity)`; `record_generation(...)`; `ingest_validation_report(...)`; `append_run_event(...)`; `run_status(conn, run_id)`.

- [ ] **Step 1: Failing migration tests** — assert `materialization_generation`, `pipeline_validation_report`, `materialization_run_event` all reject UPDATE, DELETE **and TRUNCATE** (statement-level `BEFORE TRUNCATE … FOR EACH STATEMENT` triggers — a `FOR EACH ROW` trigger does **not** fire on TRUNCATE); `materialization_run_event` has `(run_id, seq)` unique and a closed `event_kind` CHECK; the run FKs to `generation_id`.

- [ ] **Step 2: Failing gate tests**

```python
REQUIRED_GATES = ("KEY_NOT_UNIQUE", "MISSING_FEATURE_COLUMN", "UNEXPECTED_COLUMN",
                  "WRONG_COLUMN_TYPE", "MISSING_STAGING_MANIFEST", "IR_HASH_MISMATCH",
                  "SCHEMA_HASH_MISMATCH", "FORBIDDEN_NUMERIC", "PROJECT_INTEGRITY")


def test_every_gate_is_rendered(three_feature_plan):
    src = render_validate(three_feature_plan)
    for code in REQUIRED_GATES:
        assert code in src


def test_assembly_consumes_staging_manifests(three_feature_plan):
    src = render_assemble(three_feature_plan)
    assert "manifest" in src.lower()


def test_a_failed_gate_raises(three_feature_plan):
    assert "raise" in render_validate(three_feature_plan)


def test_run_status_is_folded_from_events_not_stored(db, run_events):
    assert run_status(db, "run_1") == "published"
```

- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): rendered gates + hooks + append-only control plane`

---

### Task 13: Validation loop and local submitter

Spec §N. **L0 imports the project and builds the Kedro DAG** — it does not merely parse text.

**Files:** Create `src/featuregen/materialize/validation.py`, `submit.py`; Test `test_validation.py`, `test_submit.py`

**Produces:** `ValidationLevel`; `FindingClass`; `ValidationFinding`; `ValidationReportV1`; `run_l0(project) -> ValidationReportV1`; `run_l1(conn, ir, project, *, roles)`; `classify(code, *, expected, observed)`; `may_regenerate(report)`; `PipelineSubmitter`; `LocalClusterSubmitter`.

- [ ] **Step 1: Failing tests**

```python
def test_l0_actually_imports_and_builds_the_kedro_pipeline(good_project, tmp_path):
    report = run_l0(good_project, workdir=tmp_path)
    assert report.status == "passed"
    assert any("pipeline" in f.lower() for f in report.checks_performed)


def test_l0_catches_a_project_that_imports_but_has_no_pipeline(no_pipeline_project, tmp_path):
    # ast.parse would PASS this; only a real import+build catches it.
    report = run_l0(no_pipeline_project, workdir=tmp_path)
    assert report.status == "failed"
    assert report.findings[0].classification is FindingClass.RENDERER_DEFECT


def test_l0_detects_a_hand_edited_project(edited_project, tmp_path):
    assert any(f.code == "PROJECT_HASH_MISMATCH"
               for f in run_l0(edited_project, workdir=tmp_path).findings)


def test_type_contradiction_is_a_governed_fact_mismatch():
    assert classify("COLUMN_TYPE_MISMATCH", expected="decimal(18,2)",
                    observed="string") is FindingClass.GOVERNED_FACT_MISMATCH


def test_missing_partition_is_environmental():
    assert classify("PARTITION_ABSENT", expected="2026-07-27",
                    observed=None) is FindingClass.ENVIRONMENT_OR_DATA


def test_unknown_code_fails_closed():
    assert classify("SOMETHING_NEW", expected=None, observed=None) is FindingClass.UNCLASSIFIED


def test_governed_fact_mismatch_blocks_regeneration(mismatch_report):
    assert may_regenerate(mismatch_report) is False


def test_unclassified_also_blocks_regeneration(unclassified_report):
    assert may_regenerate(unclassified_report) is False


def test_findings_never_carry_data_values(l2_duplicate_report):
    for f in l2_duplicate_report.findings:
        assert "1001" not in f"{f.location}{f.expected}{f.observed}"
        assert f.count == 3


def test_l1_reads_metadata_only(db, good_project, ir, spy_conn):
    run_l1(spy_conn, ir, good_project, roles=("feature_engineer",))
    assert not spy_conn.read_any_data_rows


def test_unreachable_cluster_invents_no_findings(good_project, dead_runner):
    report = LocalClusterSubmitter(runner=dead_runner).submit(
        good_project, level=ValidationLevel.L1, environment_id="e")
    assert report.status == "error" and report.findings == ()


def test_l2_is_not_run_unless_requested(good_project, fake_runner):
    LocalClusterSubmitter(runner=fake_runner).submit(
        good_project, level=ValidationLevel.L1, environment_id="e")
    assert fake_runner.spark_jobs == []
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): validation loop with real L0 import + fail-closed classification`

---

### Task 14: Publication capability attestation and the publisher

Spec §K. **No mechanism is selectable without a passing proof for that environment.**

**Files:** Create `src/featuregen/materialize/publish.py`, `render/publish.py`; Test `test_publish.py`, `test_publish_capability.py`

**Produces:** `PublishMechanism` (StrEnum `VERSIONED_POINTER`, `LOCATION_SWAP`, `EXCHANGE_PARTITION`); `PublicationCapabilityAttestation` (frozen: `environment_id`, `hive_version`, `spark_version`, `metastore_version`, `mechanism`, `passed`, `covers_schema_evolution`, `attested_at`); `CapabilityUnproven(Exception)`; `select_publisher(conn, *, environment_id, mechanism) -> GroupPublisher`; `render_publish(plan, *, mechanism)`.

- [ ] **Step 1: Failing tests**

```python
def test_no_attestation_means_no_publisher(db):
    with pytest.raises(CapabilityUnproven):
        select_publisher(db, environment_id="hdfc-local",
                         mechanism=PublishMechanism.LOCATION_SWAP)


def test_failed_attestation_means_no_publisher(db, failed_attestation):
    with pytest.raises(CapabilityUnproven):
        select_publisher(db, environment_id="hdfc-local",
                         mechanism=PublishMechanism.LOCATION_SWAP)


def test_attestation_for_a_DIFFERENT_environment_does_not_count(db, attestation_for_other_env):
    with pytest.raises(CapabilityUnproven, match="hdfc-local"):
        select_publisher(db, environment_id="hdfc-local",
                         mechanism=PublishMechanism.LOCATION_SWAP)


def test_attestation_not_covering_schema_evolution_is_refused_when_adding_a_feature(
        db, attestation_without_schema_evolution):
    with pytest.raises(CapabilityUnproven, match="schema"):
        select_publisher(db, environment_id="hdfc-local",
                         mechanism=PublishMechanism.LOCATION_SWAP, adds_feature=True)


def test_insert_overwrite_is_not_even_a_mechanism():
    assert not any("OVERWRITE" in m.upper() for m in PublishMechanism)


def test_rendered_publish_never_emits_insert_overwrite(three_feature_plan):
    assert "INSERT OVERWRITE" not in render_publish(
        three_feature_plan, mechanism=PublishMechanism.VERSIONED_POINTER).upper()


def test_target_is_derived_not_passed(three_feature_plan):
    import inspect
    assert "target" not in inspect.signature(render_publish).parameters
```

- [ ] **Step 2: The cluster capability probe (its own runnable command)**

```python
# tests/featuregen/materialize/test_publish_capability.py
"""Spec §K — the probe that PROVES a mechanism on the target cluster.

Run explicitly against the live cluster:
    PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize/test_publish_capability.py \
      --cluster-dsn=... -q
It records a PublicationCapabilityAttestation. Until it passes, publication is refused.
"""

def test_concurrent_reader_sees_only_complete_states(hive_cluster, staged_group):
    obs = hive_cluster.poll_while(
        lambda: hive_cluster.read_generation_marker_and_content("sandbox_feature.cif_daily"),
        during=lambda: hive_cluster.publish(staged_group))
    assert obs, "reader observed nothing — the probe is vacuous"
    for o in obs:
        assert o in (staged_group.complete_old_marker, staged_group.complete_new_marker)


def test_adding_a_feature_is_also_atomic(hive_cluster, group_with_added_feature):
    """Schema evolution: swapping one partition does NOT atomically change table schema."""
    obs = hive_cluster.poll_while(
        lambda: hive_cluster.read_schema_and_marker("sandbox_feature.cif_daily"),
        during=lambda: hive_cluster.publish(group_with_added_feature))
    assert obs
    for o in obs:
        assert o in (group_with_added_feature.old_state, group_with_added_feature.new_state)
```

The probe uses a **generation marker plus a content check**, never schema-and-row-count alone (those can coincide across versions).

- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): publication capability attestation + publisher`

---

### Task 15: LIVE — end-to-end generation, Spark-local proof, and a published cluster partition

Spec §L. **This task is the deliverable.** It is not complete until a real partition is published on the cluster.

**Files:** Create `src/featuregen/materialize/pipeline.py`, `tests/featuregen/materialize/spark_fixtures/`; Test `test_spark_local.py`, `test_live_cluster.py`

**Produces:** `generate_group(conn, inputs: Sequence[ResolvedFeatureInput], *, roles, cadence, availability_class, environment_id) -> GenerationResult`.

- [ ] **Step 1: Write the hand-authored tiny data fixtures**

`spark_fixtures/` holds small CSV/Parquet inputs written by hand: `transactions` (including a row dated `2026-07-01` **posted** `2026-07-05` for the look-ahead case, a customer with several merchants, cross-border and domestic rows, and an account with two customers to exercise a `1:N` hop), `accounts`, `customers` (including customer `1099` with **no** transactions). Every expected value is computed by hand and written in a comment beside the assertion.

- [ ] **Step 2: Failing Spark-local tests (MANDATORY — not marked, not skipped)**

```python
def test_total_debit_amount_30d(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1001", "total_debit_amount_30d") == 5500   # 3000 + 2000 + 500 debits


def test_distinct_merchant_count_90d(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1001", "distinct_merchant_count_90d") == 3  # M1, M2, M3


def test_cross_border_value_ratio_90d(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1001", "cross_border_value_ratio_90d") == Decimal("0.20")  # 1000/5000


def test_zero_denominator_yields_null_per_policy(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1002", "cross_border_value_ratio_90d") is None  # no txns in window


def test_decimal_rounding_is_half_up_at_scale_2(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1003", "cross_border_value_ratio_90d") == Decimal("0.33")  # 1/3


def test_look_ahead_row_is_excluded(spark_run):
    """Dated 2026-07-01, posted 2026-07-05 — invisible on the 3rd, visible on the 6th."""
    assert spark_run(business_dt="2026-07-03").value("1004", "total_debit_amount_30d") == 0
    assert spark_run(business_dt="2026-07-06").value("1004", "total_debit_amount_30d") == 250


def test_empty_window_policy_yields_zero(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1099", "total_debit_amount_30d") == 0


def test_entity_with_no_transactions_still_appears(spark_run):
    assert spark_run(business_dt="2026-07-27").has_key("1099")


def test_exactly_one_row_per_key_and_date(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.row_count() == out.distinct_key_count()


def test_a_one_to_many_hop_does_not_inflate_the_sum(spark_run):
    """Account shared by two customers: the SUM must not double-count."""
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1005", "total_debit_amount_30d") == 400   # NOT 800


def test_duplicate_key_group_is_rejected_by_the_gate(spark_run_broken):
    with pytest.raises(Exception, match="KEY_NOT_UNIQUE"):
        spark_run_broken(business_dt="2026-07-27")


def test_missing_staging_manifest_blocks_publication(spark_run_missing_manifest):
    with pytest.raises(Exception, match="MISSING_STAGING_MANIFEST"):
        spark_run_missing_manifest(business_dt="2026-07-27")
```

Add `pyspark` to the dev dependencies in `pyproject.toml`. These tests run by default.

- [ ] **Step 3: Run — expect FAIL, then implement `generate_group` and fix the renderer until every number is right**

This is where renderer defects surface. Iterate: run → read the failure → fix the *renderer* (never the fixture's expected value) → re-run.

- [ ] **Step 4: The live cluster task**

```python
# tests/featuregen/materialize/test_live_cluster.py
"""The deliverable. Run against the real Hadoop/Hive cluster."""

def test_publication_capability_is_attested(live_cluster, control_plane):
    att = control_plane.attestation(environment_id="hdfc-local")
    assert att.passed and att.covers_schema_evolution


def test_generate_validate_run_and_publish(live_cluster, control_plane, resolved_inputs):
    result = generate_group(control_plane.conn, resolved_inputs, roles=("feature_engineer",),
                            cadence=DAILY, availability_class=AvailabilityClass.T_PLUS_1,
                            environment_id="hdfc-local")
    assert run_l0(result.project, workdir=live_cluster.workdir).status == "passed"
    assert run_l1(control_plane.conn, result.irs[0], result.project,
                  roles=("feature_engineer",)).status == "passed"
    live_cluster.submit_and_run(result.project, business_dt="2026-07-27")
    rows = live_cluster.query(
        "SELECT COUNT(*) FROM sandbox_feature.cif_daily WHERE business_dt='2026-07-27'")
    assert rows[0][0] > 0
    assert control_plane.run_status(result.run_id) == "published"


def test_manifest_and_reports_are_ingested(live_cluster, control_plane, resolved_inputs):
    manifest = control_plane.latest_manifest()
    assert manifest.generation_id and manifest.published_row_count > 0
```

- [ ] **Step 5: Full sweep + commit**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula tests/featuregen/db -p no:cacheprovider -q
git add -- src/featuregen/materialize tests/featuregen/materialize pyproject.toml
git commit -m "feat(materialize): end-to-end generation, Spark-local proof, live cluster publication"
```

**Spec A is not done until `test_live_cluster.py` passes against the real cluster.**

---

## Self-Review

**Spec coverage.** §0→T2 · §A2→T5 · §A3→T3,T6 · §A4→T4 · §B→T9 · §C→T7 · §D→T8 · §E (no sharing)→T11 by construction · §F→T10 · §G→T11,T15 · §H→T12 · §J→T12 · §K→T14 · §L→T10 (goldens), T15 (Spark-local, mandatory), T15 (cluster) · §M→the fail-closed paths in T2–T7,T14 · §N→T13.

**Rev-1 defects, each now owned by a task:** governed input gate→T2 · `classify_join_path` reuse + cardinality→T3 · governed spine→T4 · per-expression IR→T5 · classification adapter + window-out-of-contract→T7 · manifest-based completeness→T8 · non-circular sandbox-only identity→T9 · complete runnable project + real L0→T10,T13 · no scan sharing→T11 · TRUNCATE-blocking append-only control plane→T12 · capability attestation + schema evolution→T14 · mandatory execution with all three features→T15 · verified fixture fields→T2.

**Placeholder scan.** No "TBD"/"handle errors appropriately"/"check the names yourself". Fixture field names are stated as verified and listed at the top; the one instruction to run a construction check first is a *verification* step, not a gap.

**Type consistency.** `materialize_hash` (T1) is the only hasher. `AdmittedFeature` (T2) → `compile_ir` (T6). `JoinPlan` (T3) is consumed by T5 and its `fans_out` drives T11's collapse. `PitSpec` is per `ExpressionExecutionIR` (T5) throughout. `StagingManifestV1` is defined in T8, rendered in T11, consumed in T12. `CompilationIdentity` (T9) is embedded in rendered files; `generated_project_hash` is detached (T10). `ValidationReportV1` (T13) is returned by `LocalClusterSubmitter.submit`.
