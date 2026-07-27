# Spec A — Executable Materialization Vertical Slice: Implementation Plan (rev 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A published `sandbox_feature.<group>` partition on the real Hadoop/Hive cluster, computed by generated Kedro/PySpark from governed formulas, with the numbers proven by execution.

**Done means a live table, not a green suite.** Task 16 is the deliverable.

**Spec:** `docs/superpowers/specs/2026-07-27-feature-materialization-spec-a-design.md` **rev 3**.
**Verified interfaces:** `docs/architecture/2026-07-27-verified-interfaces-materialization.md`.

> **THE RULE THAT MATTERS.** Revisions 1 and 2 of this plan were rejected with 12 and 16 findings; **every defect sat in an API described from memory rather than read**. Before writing code against any interface, confirm it in the verified-interfaces reference. If it is not there, **read the source, add an entry, then implement**. An implementation built on an unverified assumption is a defect even if its tests pass.

## Global Constraints

- **Frozen slotted dataclasses + `StrEnum`** — NOT pydantic.
- **One hasher:** `materialize_hash()` (Task 1). Identity fields only — no provenance, no timestamps, no live observations.
- **Reuse governed machinery.** Joins → `classify_join_path`. Sensitivity → `graph_node` + `safety_floor.SENSITIVITY_ORDER` + `read_scope`. C1 → `read_operational_value`. Actor → `IdentityEnvelope`.
- **Never mint identity.** Thread `IdentityEnvelope` from the request; never construct one with `authenticated=True`.
- **Render-only.** No `pyspark` import in `src/featuregen/materialize/`.
- **Fail closed** with a typed code from spec §14. Unknown ⇒ refusal, never a default.
- **Manifests/findings carry counts, types, hashes, locations — never data values.**
- **Sandbox only.** `derive_namespace()` takes no parameters.
- **`INSERT OVERWRITE` is forbidden.** **No fan-out repair.** **No scan sharing.**
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

**Test command:** `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q`

---

### Task 0: Target-cluster inventory (BLOCKING, discovery — no code)

Spec §0. Produces facts, not software. **Cluster acceptance (Task 16) is blocked until this is complete**; Tasks 1–15 are parameterized on its output and may proceed in parallel.

**Files:** Create `docs/architecture/2026-07-27-hdfc-cluster-inventory.md`

- [ ] **Step 1: Capture table layout** — for `banking.transactions`, `banking.accounts`, `banking.customers`:

```sql
DESCRIBE FORMATTED banking.transactions;
SHOW PARTITIONS banking.transactions;
-- repeat for accounts, customers
```

Record per table: partitioned yes/no · ordered partition columns + types · example partition values covering the acceptance date · physical location · whether historical partitions are rewritten in place.

- [ ] **Step 2: Capture engine versions** (§K's attestation is keyed on this exact triple)

```sql
SELECT version();            -- Hive
-- Spark: spark.version ; metastore: hive.metastore schema version
```

- [ ] **Step 3: Answer the two slice-shaping questions**
  1. **How is account-to-customer ownership modelled?** A single `accounts.cif_id` column is `N:1` toward the grain and fine. A joint-holder bridge table is `1:N` and **refuses `total_debit_amount_30d`** under spec §3.2 — in which case pick a first feature whose traversal is `N:1` and record the substitution here.
  2. **How is a customer snapshot selected for a business date?** This becomes `SpineSourceDeclarationV1.snapshot_policy`.

- [ ] **Step 4: Record `None`-vs-unknown explicitly.** For each table write either "verified unpartitioned" or the partition columns. **Never leave it unstated** — spec §3.3 treats `None` as *verified unpartitioned*, and an unknown recorded as `None` would silently license reading the wrong data.

- [ ] **Step 5: Commit** — `docs: HDFC target-cluster inventory (Spec A Task 0)`

---

### Task 1: `materialize_hash` + package skeleton

**Files:** Create `src/featuregen/materialize/{__init__,canonical}.py`; Test `tests/featuregen/materialize/test_canonical.py`
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

- [ ] **Step 2: Run — FAIL** (`ModuleNotFoundError`)
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

- [ ] **Step 4: Run — PASS (4)** · **Step 5: Commit** — `feat(materialize): JCS+sha256 hasher`

---

### Task 2: Terminal-event reader + Gate 1 admission

Spec §1.2. **`trace.py` has no public event reader — this task adds one.**

**Files:** Modify `src/featuregen/formula/trace.py`; Create `src/featuregen/materialize/admission.py`, `tests/featuregen/materialize/fixtures.py`; Test `tests/featuregen/formula/test_trace_reader.py`, `tests/featuregen/materialize/test_admission.py`

**Consumes (verified):** `AuthoringResult` (`result.py:97`, carries `authoring_run_id`) · terminal payload fields (`authoring.py:393-408`) · `authoring_intent_hash` (`authoring.py:253`) · `_durable_read` (`trace.py:432`) · `formula_content_hash`.

**Produces:** `TerminalEvent` (frozen: `kind`, `payload: Mapping`, `payload_hash: str`); `read_terminal_event(conn, run_id) -> TerminalEvent | None`; `ResolvedFeatureInput` (frozen: `intent`, `result`); `AdmissionRefused(Exception)` with `.code`; `AdmittedFeature` (frozen: `feature_name`, `formula`, `formula_content_hash`, `intent`, `authoring_run_id`); `admit_artifacts(conn, inputs) -> tuple[AdmittedFeature, ...]`.

- [ ] **Step 1: Failing reader test**

```python
# tests/featuregen/formula/test_trace_reader.py
from featuregen.formula.trace import TraceEventKind, append_event, read_terminal_event


def test_no_terminal_event_reads_none(db, open_run):
    append_event(db, open_run, TraceEventKind.STARTED, seq=0, idempotency_key="k0", payload={})
    assert read_terminal_event(db, open_run) is None


def test_terminal_event_returns_payload_and_hash(db, open_run):
    append_event(db, open_run, TraceEventKind.COMPLETED, seq=1, idempotency_key="k1",
                 payload={"authoring_disposition": "RESOLVED"})
    ev = read_terminal_event(db, open_run)
    assert ev.kind == "COMPLETED"
    assert ev.payload["authoring_disposition"] == "RESOLVED"
    assert len(ev.payload_hash) == 64
```

- [ ] **Step 2: Run — FAIL** · **Step 3: Implement `read_terminal_event`** in `trace.py` using `_durable_read`, selecting `kind, payload, payload_hash` where `kind IN (COMPLETED, FAILED)`.
- [ ] **Step 4: Run — PASS (3)**

- [ ] **Step 5: Write the fixtures — verified field names**

```python
# tests/featuregen/materialize/fixtures.py
"""Hand-authored fixtures. Field names verified in the interfaces reference §6.

NOTE the output policies: Child-1 resolves a plain SUM to NON_ADDITIVE without
partition_proof.path_additive, and COUNT_DISTINCT to NON_ADDITIVE with logical type
`integer`. A fixture claiming otherwise is a FORGERY and Gate 1 will reject it.
"""
from __future__ import annotations

from featuregen.formula.schema import (
    AdditivityClass, AggregateExpression, AggregateFunction, DecimalPolicy, EmptyWindowResult,
    FilterPredicate, FilterPredicateOp, FormulaOutputPolicyV1, Grain, Inclusivity, LiteralType,
    NullInput, OverflowBehavior, RoundingMode, SourceRelation, TypedFormulaV1, TypedLiteral,
    UnaryBody, WindowBasis, WindowPolicy, WindowUnit,
)
from featuregen.formula.turns import AuthoringIntent

SRC = "hdfc"
TXN = f"{SRC}::banking.transactions"
AMOUNT, TXN_DATE, POSTED_AT = f"{TXN}.amount", f"{TXN}.transaction_date", f"{TXN}.posted_at"
TXN_TYPE, MERCHANT = f"{TXN}.transaction_type", f"{TXN}.merchant_id"
CIF_ID = f"{SRC}::banking.accounts.cif_id"
CUSTOMERS = f"{SRC}::banking.customers"


def _window(length: int) -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=TXN_DATE, basis=WindowBasis.TRAILING, length=length, unit=WindowUnit.DAY,
        start_inclusive=Inclusivity.INCLUSIVE, end_inclusive=Inclusivity.INCLUSIVE,
        timezone="Asia/Dubai", empty_window=EmptyWindowResult.ZERO, null_input=NullInput.IGNORE)


def _eq(left: str, value: str) -> FilterPredicate:
    # `kind` is init=False; there is NO right_ref field.
    return FilterPredicate(op=FilterPredicateOp.EQUAL, left=left,
                           right_literal=TypedLiteral(type=LiteralType.STRING, value=value))


def total_debit_amount_30d() -> TypedFormulaV1:
    return TypedFormulaV1(
        formula_schema_version=1, operation_grammar_version=1, output_policy_version=1,
        canonicalization_version=1, grain=Grain(entity="customer", keys=(CIF_ID,)),
        body=UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.SUM, operand=AMOUNT,
            source_relation=SourceRelation(table_ref=TXN),
            filter=_eq(TXN_TYPE, "debit"), window=_window(30))),
        parameters=(),
        decimal=DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_UP,
                              overflow=OverflowBehavior.ERROR),
        output=FormulaOutputPolicyV1(
            output_type="numeric", unit=None, currency=None,
            output_additivity=AdditivityClass.NON_ADDITIVE,   # verified: no path_additive proof
            external_type_required=False))


def intent_for(name: str) -> AuthoringIntent:
    return AuthoringIntent(name=name, hypothesis=f"{name} per customer per day",
                           target_entity="customer", target_grain_keys=(CIF_ID,))
```

**Before continuing**, run:
`PYTHONPATH=src .venv/bin/python -c "from tests.featuregen.materialize.fixtures import *; total_debit_amount_30d()"`
If any field name is wrong, fix the fixture **and** correct the interfaces reference.

- [ ] **Step 6: Failing Gate-1 tests**

```python
# tests/featuregen/materialize/test_admission.py
import pytest
from featuregen.materialize.admission import AdmissionRefused, ResolvedFeatureInput, admit_artifacts
from tests.featuregen.materialize.fixtures import intent_for, total_debit_amount_30d


def test_no_terminal_event_is_refused(db, run_without_terminal, result_for):
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), result_for(run_without_terminal))])
    assert e.value.code == "AUTHORING_RUN_INCOMPLETE"


def test_terminal_disposition_not_resolved_is_refused(db, rejected_run, result_for):
    """A REJECTED run still writes a COMPLETED event — only the PAYLOAD says otherwise."""
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), result_for(rejected_run))])
    assert e.value.code == "NOT_RESOLVED"


def test_forged_result_with_a_legitimate_run_is_refused(db, resolved_run, forged_result):
    """The attack: claim RESOLVED, attach any formula, cite a real run id."""
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), forged_result(resolved_run))])
    assert e.value.code == "FORMULA_HASH_MISMATCH"


def test_axes_disagreeing_with_the_terminal_event_are_refused(db, resolved_run, tweaked_axes_result):
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"),
                                                  tweaked_axes_result(resolved_run))])
    assert e.value.code == "AXES_MISMATCH"


def test_tampered_terminal_payload_is_refused(db, tampered_terminal, result_for):
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("f"), result_for(tampered_terminal))])
    assert e.value.code == "TERMINAL_PAYLOAD_TAMPERED"


def test_intent_hash_mismatch_is_refused(db, resolved_run, result_for):
    with pytest.raises(AdmissionRefused) as e:
        admit_artifacts(db, [ResolvedFeatureInput(intent_for("a_different_feature"),
                                                  result_for(resolved_run))])
    assert e.value.code == "INTENT_HASH_MISMATCH"


def test_admitted_feature_name_comes_from_the_intent(db, resolved_run, result_for):
    out = admit_artifacts(db, [ResolvedFeatureInput(intent_for("total_debit_amount_30d"),
                                                    result_for(resolved_run))])
    assert out[0].feature_name == "total_debit_amount_30d"


def test_no_function_accepts_a_bare_formula():
    import inspect
    import featuregen.materialize.admission as m
    for name, fn in inspect.getmembers(m, inspect.isfunction):
        if not name.startswith("_"):
            params = inspect.signature(fn).parameters
            assert "formula" not in params and "formulas" not in params, (
                f"{name} exposes a raw-formula entry point, bypassing the governed gate")
```

- [ ] **Step 7: Run — FAIL** · **Step 8: Implement the six checks in spec §1.2 order** · **Step 9: Run — PASS (8)**
- [ ] **Step 10: Commit** — `feat(materialize): Gate 1 admission against the immutable terminal event`

---

### Task 3: `JoinPlan` — extend the planner, then adapt it

Spec §3.1–3.2. **Two parts: the planner must first be taught to keep authority.**

**Files:** Modify `src/featuregen/overlay/upload/join_path.py`; Create `src/featuregen/materialize/joins.py`; Test `tests/featuregen/overlay/upload/test_join_path_authority.py`, `tests/featuregen/materialize/test_joins.py`

**Produces:** `JoinStep` gains `approved_join_fact_key`, `approved_join_status`, `authority`; `JoinPlanStep`; `JoinPlan` (frozen: `steps`, `outcome_kind`, `roles_used`, `fans_out`); `JoinRefused` (frozen: `code`, `detail`); `plan_join(conn, *, catalog_source, from_table_ref, to_table_ref, roles) -> JoinPlan | JoinRefused`.

- [ ] **Step 1: Failing planner-authority test**

```python
# tests/featuregen/overlay/upload/test_join_path_authority.py
def test_operational_steps_retain_their_approving_fact(db, verified_join_catalog):
    """Verified: clearing.append((from_ref, to_ref, card)) currently DROPS the fact key."""
    outcome = classify_join_path(db, "hdfc", "transactions", "accounts", roles=("feature_engineer",))
    assert outcome.kind == JoinOutcome.OPERATIONAL
    assert outcome.steps[0].approved_join_fact_key is not None
    assert outcome.steps[0].approved_join_status == "VERIFIED"


def test_file_declared_edges_report_their_authority(db, declared_join_catalog):
    outcome = classify_join_path(db, "hdfc", "transactions", "accounts", roles=("feature_engineer",))
    assert outcome.steps[0].approved_join_fact_key is None
    assert outcome.steps[0].authority == "operational"
```

- [ ] **Step 2: Run — FAIL** · **Step 3: Carry `approved_join_fact_key`/`approved_join_status` through the clearing tuple, inside the existing query** (they are already SELECTed at `:105-106`). Do **not** add a second read. · **Step 4: Run — PASS**

- [ ] **Step 5: Failing adapter tests**

```python
# tests/featuregen/materialize/test_joins.py
def test_schema_qualified_refs_are_reduced_to_bare_table_names(db, verified_join_catalog):
    """VERIFIED: _table_of returns parts[1]; a schema-qualified destination never matches."""
    result = plan_join(db, catalog_source="hdfc",
                       from_table_ref="hdfc::banking.transactions",
                       to_table_ref="hdfc::banking.accounts", roles=("feature_engineer",))
    assert isinstance(result, JoinPlan) and result.steps


def test_duplicate_table_name_across_schemas_is_refused(db, ambiguous_catalog):
    result = plan_join(db, catalog_source="hdfc",
                       from_table_ref="hdfc::banking.transactions",
                       to_table_ref="hdfc::archive.transactions", roles=("feature_engineer",))
    assert isinstance(result, JoinRefused) and result.code == "AMBIGUOUS_TABLE_NAME"


def test_authority_survives_into_the_plan(db, verified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table_ref="hdfc::banking.transactions",
                       to_table_ref="hdfc::banking.accounts", roles=("feature_engineer",))
    assert result.steps[0].approved_join_fact_key is not None


def test_fan_out_toward_the_grain_is_REFUSED(db, joint_account_catalog):
    """A 1:N step is refused, never repaired — allocation is a business decision."""
    result = plan_join(db, catalog_source="hdfc", from_table_ref="hdfc::banking.transactions",
                       to_table_ref="hdfc::banking.customers", roles=("feature_engineer",))
    assert isinstance(result, JoinRefused) and result.code == "JOIN_FANOUT_UNSUPPORTED"


def test_no_deduplication_helper_exists_anywhere_in_the_module():
    import inspect
    from featuregen.materialize import joins
    src = inspect.getsource(joins)
    for banned in ("dropDuplicates", "drop_duplicates", "distinct("):
        assert banned not in src, "fan-out must be refused, not repaired"


def test_unverified_denied_and_no_path_map_to_distinct_codes(db, unverified_cat, restricted_cat,
                                                             empty_cat):
    assert plan_join(db, catalog_source="hdfc", from_table_ref="hdfc::banking.transactions",
                     to_table_ref="hdfc::banking.accounts",
                     roles=("feature_engineer",)).code == "JOIN_PATH_NOT_VERIFIED"
    # …restricted_cat -> JOIN_PATH_DENIED_BY_READ_SCOPE ; empty_cat -> GRAIN_PATH_NOT_GOVERNED


def test_same_table_needs_no_steps(db, verified_join_catalog):
    result = plan_join(db, catalog_source="hdfc", from_table_ref="hdfc::banking.transactions",
                       to_table_ref="hdfc::banking.transactions", roles=("feature_engineer",))
    assert isinstance(result, JoinPlan) and result.steps == ()
```

- [ ] **Step 6: Run — FAIL** · **Step 7: Implement the adapter.** Parse with `parse_ref`, pass **bare** table names, keep schema/source separately, refuse ambiguity, map the four outcomes, and refuse any step whose cardinality fans out toward the destination. **No BFS, no bridge scan, no prefix matching in this module.** · **Step 8: Run — PASS (7)**
- [ ] **Step 9: Commit** — `feat(materialize): JoinPlan adapter; planner retains join authority`

---

### Task 4: `SpineSourceDeclarationV1`

Spec §4. Facts validate; they never choose.

**Files:** Create `src/featuregen/materialize/spine.py`; Test `tests/featuregen/materialize/test_spine.py`
**Produces:** `PopulationSemantics` (closed StrEnum); `SnapshotPolicy`; `SpineSourceDeclarationV1`; `SpineRefused`; `validate_spine_declaration(conn, decl, *, roles) -> SpineSpec | SpineRefused`.

- [ ] **Step 1: Failing tests**

```python
def test_facts_validate_but_never_choose(db, two_candidate_customer_tables, declaration_for_customers):
    """kyc_customers ALSO has a unique cif_id; only the DECLARATION picks the master."""
    out = validate_spine_declaration(db, declaration_for_customers, roles=("feature_engineer",))
    assert out.source_table_ref == "hdfc::banking.customers"


def test_no_declaration_is_refused(db):
    with pytest.raises(TypeError):
        validate_spine_declaration(db, None, roles=())  # there is no inference path


def test_facts_may_REJECT_a_declaration(db, declaration_naming_a_non_unique_table):
    out = validate_spine_declaration(db, declaration_naming_a_non_unique_table,
                                     roles=("feature_engineer",))
    assert isinstance(out, SpineRefused) and out.code == "SPINE_DECLARATION_REJECTED_BY_FACTS"


def test_declaration_denied_by_read_scope_is_refused(db, restricted_customers, declaration):
    out = validate_spine_declaration(db, declaration, roles=("catalog_viewer",))
    assert isinstance(out, SpineRefused)


def test_population_semantics_is_a_closed_enum():
    from featuregen.materialize.spine import PopulationSemantics
    assert {p.value for p in PopulationSemantics} == {
        "current_complete_population", "current_active_only", "historical_as_of"}


def test_no_free_text_sql_predicate_is_accepted(db):
    import inspect
    from featuregen.materialize import spine
    fields = {f.name for f in dataclasses.fields(spine.SpineSourceDeclarationV1)}
    assert not any("sql" in f or "predicate" in f or "where" in f for f in fields)


def test_declared_by_is_never_minted_inside_the_module():
    import inspect
    from featuregen.materialize import spine
    assert "authenticated=True" not in inspect.getsource(spine)
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): SpineSourceDeclarationV1, facts validate never choose`

---

### Task 5: Physical input snapshots

Spec §3.3. Parameterized on Task 0; **assumes no partition column.**

**Files:** Create `src/featuregen/materialize/snapshots.py`; Test `test_snapshots.py`
**Produces:** `PartitionSpec`; `PhysicalInputSnapshot`; `resolve_snapshots(inventory, *, table_ref, window, business_dt) -> tuple[PhysicalInputSnapshot, ...] | SnapshotRefused`.

- [ ] **Step 1: Failing tests**

```python
def test_a_90_day_window_resolves_MANY_partitions(daily_partitioned_inventory):
    snaps = resolve_snapshots(daily_partitioned_inventory, table_ref="hdfc::banking.transactions",
                              window=_window(90), business_dt="2026-07-27")
    assert len(snaps[0].partition_specs) == 90       # plural is the whole point


def test_verified_unpartitioned_yields_None_not_empty(unpartitioned_inventory):
    snaps = resolve_snapshots(unpartitioned_inventory, table_ref="hdfc::banking.customers",
                              window=None, business_dt="2026-07-27")
    assert snaps[0].partition_specs is None


def test_unknown_layout_is_REFUSED_not_treated_as_unpartitioned(empty_inventory):
    out = resolve_snapshots(empty_inventory, table_ref="hdfc::banking.transactions",
                            window=_window(30), business_dt="2026-07-27")
    assert isinstance(out, SnapshotRefused) and out.code == "PARTITION_IDENTITY_UNKNOWN"


def test_business_dt_is_never_assumed_to_be_the_partition_column(hourly_partitioned_inventory):
    snaps = resolve_snapshots(hourly_partitioned_inventory, table_ref="hdfc::banking.transactions",
                              window=_window(1), business_dt="2026-07-27")
    cols = {c for s in snaps[0].partition_specs for c, _ in s.columns}
    assert "business_dt" not in cols        # the inventory said the column is `load_hour`
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): plural partition snapshots, unknown refuses`

---

### Task 6: `ExpressionExecutionIR`

Spec §3. One PIT per expression.

**Files:** Create `src/featuregen/materialize/expression_ir.py`; Test `test_expression_ir.py`
**Produces:** `PhysicalRef`; `PitSpec`; `ExpressionExecutionIR`; `ExpressionRefused`; `compile_expression(conn, *, expr_path, expr, grain_keys, roles, inventory) -> … `; `expression_ir_hash(e)`.

- [ ] **Step 1: Failing tests** — read set includes operand + filter `left` + event time + join endpoints (there is **no `right_ref`**) · a ratio's two expressions get independent `PitSpec`s · different windows hash differently · missing `AVAILABILITY_TIME` → `AVAILABILITY_TIME_NOT_GOVERNED` · a fan-out join refuses the expression · `identity_payload()` excludes provenance.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): per-expression IR with its own PIT`

---

### Task 7: `FormulaExecutionIRV1` + Gate 2 authorization

Spec §1.3, §2.

**Files:** Create `src/featuregen/materialize/ir.py`; Test `test_ir.py`
**Produces:** `FormulaExecutionIRV1`; `IrRefused`; `compile_ir(conn, admitted, *, roles, spine_decl, inventory)`; `ir_hash(ir)`; `authorize_read_set(conn, ir, *, roles) -> None | AuthorizationRefused`.

- [ ] **Step 1: Failing tests**

```python
def test_gate_2_covers_join_endpoints_not_just_operands(db, restricted_join_key, ir):
    """account_id is only a join key — Gate 2 must still see it."""
    out = authorize_read_set(db, ir, roles=("feature_engineer",))
    assert out.code == "READ_SCOPE_INSUFFICIENT"


def test_gate_2_covers_the_spine_source(db, restricted_customers, ir):
    assert authorize_read_set(db, ir, roles=("feature_engineer",)).code == "READ_SCOPE_INSUFFICIENT"


def test_gate_2_covers_availability_columns(db, restricted_posted_at, ir):
    assert authorize_read_set(db, ir, roles=("feature_engineer",)).code == "READ_SCOPE_INSUFFICIENT"


def test_one_denied_element_refuses_the_WHOLE_compilation(db, one_restricted_column, two_feature_irs):
    for ir in two_feature_irs:
        assert authorize_read_set(db, ir, roles=("feature_engineer",)) is not None


def test_ratio_produces_two_expression_irs(db, seeded, admitted_ratio):
    assert {e.expr_path for e in compile_ir(...).expressions} == {"body.numerator", "body.denominator"}


def test_output_policy_is_carried_never_rederived(db, seeded, admitted):
    assert compile_ir(...).output_policy == admitted.formula.output
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): FormulaExecutionIRV1 + Gate 2 over the complete read set`

---

### Task 8: Physical type adapter

Spec §6.

**Files:** Create `src/featuregen/materialize/physical_types.py`; Test `test_physical_types.py`
**Produces:** `PHYSICAL_TYPE_POLICY_VERSION = 1`; `PhysicalType` (frozen: `sql_type`, `nullable`); `PhysicalTypeUnsupported`; `resolve_physical_type(formula) -> PhysicalType | PhysicalTypeUnsupported`.

- [ ] **Step 1: Failing tests**

```python
def test_counts_are_bigint(count_distinct_formula):
    assert resolve_physical_type(count_distinct_formula).sql_type == "BIGINT"


def test_sum_uses_decimal_from_the_decimal_policy(sum_formula):
    assert resolve_physical_type(sum_formula).sql_type == "DECIMAL(18,2)"


def test_operation_beats_the_logical_word(count_distinct_formula):
    """Logical output_type is `integer`; the OPERATION decides BIGINT."""
    assert resolve_physical_type(count_distinct_formula).sql_type == "BIGINT"


def test_zero_denominator_null_makes_the_column_nullable(ratio_null_formula):
    assert resolve_physical_type(ratio_null_formula).nullable is True


def test_empty_window_zero_makes_the_column_non_nullable(sum_zero_formula):
    assert resolve_physical_type(sum_zero_formula).nullable is False


def test_precision_above_38_is_unsupported(huge_precision_formula):
    out = resolve_physical_type(huge_precision_formula)
    assert isinstance(out, PhysicalTypeUnsupported) and out.code == "PHYSICAL_TYPE_UNSUPPORTED"


def test_saturate_is_refused_in_this_slice(saturate_formula):
    assert isinstance(resolve_physical_type(saturate_formula), PhysicalTypeUnsupported)


def test_double_is_never_produced():
    import inspect
    from featuregen.materialize import physical_types
    assert "DOUBLE" not in inspect.getsource(physical_types).upper()
```

- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): versioned physical type adapter`

---

### Task 9: Classification + contract per feature + grouping

Spec §5.

**Files:** Create `src/featuregen/materialize/{classify,contract}.py`; Test `test_classify.py`, `test_contract.py`
**Produces:** `CLASSIFICATION_POLICY_VERSION = 1`; `classify_read_set(conn, refs) -> Classification`; `CadenceDecl`; `AvailabilityClass`; `ContractOverrides`; `OverrideRefused`; `MaterializationContractV1`; `derive_contract(conn, ir, *, cadence, availability_class, spine_decl, overrides=None)`; `group_by_contract(contracts) -> …`; `contract_hash(c)`.

- [ ] **Step 1: Failing classification tests**

```python
def test_sensitivity_class_comes_from_effective_restriction(db, confidential_catalog, refs):
    assert classify_read_set(db, refs).sensitivity_class == "confidential"


def test_access_requirements_come_from_the_read_scope_TAGS(db, pii_tagged_catalog, refs):
    assert "pii_reader" in classify_read_set(db, refs).access_requirements


def test_the_two_axes_are_independent(db, pii_but_internal_catalog, refs):
    """A pii-tagged column may still be effective_restriction=internal."""
    c = classify_read_set(db, refs)
    assert c.sensitivity_class == "internal" and "pii_reader" in c.access_requirements


def test_unknown_restriction_fails_closed_to_prohibited(db, garbage_restriction_catalog, refs):
    assert classify_read_set(db, refs).sensitivity_class == "prohibited"


def test_prohibited_input_refuses_materialization(db, prohibited_catalog, refs):
    with pytest.raises(ClassificationRefused) as e:
        classify_read_set(db, refs)
    assert e.value.code == "PROHIBITED_INPUT"


def test_a_join_key_can_be_the_most_restrictive_element(db, restricted_join_key, refs):
    assert classify_read_set(db, refs).sensitivity_class == "restricted"
```

- [ ] **Step 2: Failing contract tests**

```python
def test_a_contract_is_derived_PER_FEATURE(db, public_ir, restricted_ir, daily):
    a = derive_contract(db, public_ir, cadence=daily, ...)
    b = derive_contract(db, restricted_ir, cadence=daily, ...)
    assert contract_hash(a) != contract_hash(b)


def test_mixed_contracts_are_REFUSED_not_unioned(db, public_ir, restricted_ir, daily):
    """A caller must not force a public feature into a restricted group by passing them together."""
    out = group_by_contract([derive_contract(db, public_ir, ...),
                             derive_contract(db, restricted_ir, ...)])
    assert out.code == "MULTIPLE_MATERIALIZATION_CONTRACTS" and len(out.groups) == 2


def test_30d_and_90d_share_a_contract(db, ir_30d, ir_90d, daily):
    assert contract_hash(derive_contract(db, ir_30d, ...)) == \
           contract_hash(derive_contract(db, ir_90d, ...))


def test_contract_hash_excludes_the_calculation_window(db, ir_30d, daily):
    payload = str(derive_contract(db, ir_30d, ...).identity_payload())
    assert "window_length" not in payload and "window_unit" not in payload


def test_hash_excludes_live_observations(db, ir_30d, daily):
    payload = derive_contract(db, ir_30d, ...).identity_payload()
    for banned in ("current_watermark", "actual_arrival_at", "job_status", "run_id"):
        assert banned not in payload


def test_override_may_only_tighten(db, ir_30d, daily):
    with pytest.raises(OverrideRefused):
        derive_contract(db, ir_30d, availability_class=T_PLUS_3,
                        overrides=ContractOverrides(availability_class=T_PLUS_1), ...)


def test_dependencies_ready_trigger_refused():
    with pytest.raises(ValueError, match="dependencies_ready"):
        CadenceDecl(period="daily", timezone="Asia/Dubai", business_date_cutoff="23:59",
                    trigger="dependencies_ready")


def test_both_policy_versions_are_hashed(db, ir_30d, daily):
    p = derive_contract(db, ir_30d, ...).identity_payload()
    assert "classification_policy_version" in p and "physical_type_policy_version" in p
```

- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): classification + per-feature contracts + grouping`

---

### Task 10: Group plan, staging manifest, completeness

Spec §9.

**Files:** Create `src/featuregen/materialize/group_plan.py`; Test `test_group_plan.py`
**Produces:** `PlannedFeature` (incl. resolved `PhysicalType`); `FeatureGroupPlanV1`; `StagingManifestV1` (all binding fields per spec §9); `build_group_plan`; `group_plan_hash`; `expected_schema`; `check_completeness(plan, manifests, observed_schema, *, generation_id, run_id, business_dt)`.

- [ ] **Step 1: Failing tests** — a matching schema with a wrong `ir_hash` still fails (`IR_HASH_MISMATCH`) · a manifest from a **different generation/run/business_dt** is rejected (`STALE_STAGING_MANIFEST`) · missing manifest · `status="failed"` · duplicate manifest · missing/extra/mistyped column · nullability mismatch · adding a feature changes `group_plan_hash` but not `materialization_contract_hash` · name collision is an error.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): group plan + generation-bound staging manifests`

---

### Task 11: Two-phase identity

Spec §7.

**Files:** Create `src/featuregen/materialize/identity.py`; Test `test_identity.py`
**Produces:** `CompilationIdentity`; `RenderedArtifactIdentity`; `derive_namespace()`; `sandbox_execution_hash(...)`.

- [ ] **Step 1: Failing tests** — `derive_namespace()` takes **no parameters** and the module source contains no production literal · identity hashes are **plural** · `RenderedArtifactIdentity` is built after rendering · **`generated_project_hash` appears in `GENERATED.lock` and in no other file**, and the hash is computed over every file *except* the lock · `sandbox_execution_hash` is reproducible and includes the capability attestation id · there is no `production_execution_hash`.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): two-phase sandbox-only identity`

---

### Task 12: Render the complete runnable project

Spec §7.

**Files:** Create `src/featuregen/materialize/render/{__init__,project}.py`; Test `test_render_project.py` + `goldens/`
**Produces:** `RENDERER_VERSION = 1`; `RenderedProject`; `render_project(...)`; `materialize_to(project, path)`.

- [ ] **Step 1: Failing tests** — every required file emitted (`settings.py`, `pipeline_registry.py`, `pipelines/materialize/{nodes,pipeline}.py`, `conf/base/*`, `GENERATED.lock`, `README.md`, `pyproject.toml`, `requirements.lock`) · pipeline wires explicit `inputs=`/`outputs=` · `settings.py` registers both hooks · README states the `kedro run` vs `spark-submit` distinction · catalog names only read-set + spine tables · target is `sandbox_feature.*` and **not** parameterized · every `.py` parses · deterministic · `materialize_to` writes a real directory.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render a complete runnable Kedro project`

---

### Task 13: Render compute nodes

Spec §8.

**Files:** Create `render/nodes_compute.py`; Test `test_render_compute.py`
**Produces:** `render_spine_node(spine)`; `render_pit_projection(expr_ir)`; `render_calculate_node(feature, ir)`.

- [ ] **Step 1: Failing tests** — spine reads the **declared** source, never a fact table · availability gate uses the governed column · `event_time_plus_lag` renders its lag · **calendar windows are not converted to days** · projection selects only read-set columns, never `*` · calculate writes a `StagingManifestV1` carrying its `ir_hash`, generation, run and business_dt · each feature writes its own staging output · rendered overflow behaviour **raises** rather than yielding NULL · rendered rounding is explicit · every rendered node parses.
- [ ] **Step 2–5:** Run/implement/run/commit — `feat(materialize): render spine, PIT projection, per-feature calculation`

---

### Task 14: Render gates + hooks + control plane

Spec §9, §12.

**Files:** Create `render/nodes_gate.py`, `src/featuregen/materialize/control_plane.py`, `src/featuregen/db/migrations/1021_materialization_control_plane.sql`; Test `test_render_gate.py`, `test_control_plane.py`, `test_migration_1021.py`
**Produces:** `render_assemble`; `render_validate`; `render_hooks`; `RunManifestV1`; `record_generation`; `ingest_validation_report`; `append_run_event`; `ingest_run_manifest`; `run_status`.

- [ ] **Step 1: Failing migration tests** — `materialization_generation`, `pipeline_validation_report`, `materialization_run_event`, `materialization_run_manifest` **all** reject UPDATE, DELETE **and TRUNCATE** (statement-level `BEFORE TRUNCATE … FOR EACH STATEMENT`; a `FOR EACH ROW` trigger does not fire on TRUNCATE) · `(run_id, seq)` unique on events · closed `event_kind` CHECK · run manifest FKs to `generation_id` · one terminal manifest per run.
- [ ] **Step 2: Failing gate tests** — every §9 code rendered, including `SPINE_INCOMPLETE` · assembly consumes staging manifests · a failed gate raises · the manifest writer never calls `collect()`/`take()`/`head()` · `run_status` is folded from events, not stored.
- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): rendered gates, hooks, append-only control plane`

---

### Task 15: Capability attestation, publisher, validation loop, submitter

Spec §10, §11.

**Files:** Create `src/featuregen/materialize/{publish,validation,submit}.py`, `render/publish.py`; Test `test_publish.py`, `test_validation.py`, `test_submit.py`
**Produces:** `PublishMechanism`; `PublicationCapabilityAttestation` (with `attestation_id`); `record_attestation`; `PublisherSelection`; `CapabilityUnproven`; `select_publisher(conn, *, environment_id, engine_versions, mechanism, adds_feature) -> PublisherSelection`; `render_publish(plan, *, selection)`; `ValidationLevel`; `FindingClass`; `ValidationReportV1`; `run_l0`; `run_l1`; `classify`; `may_regenerate`; `LocalClusterSubmitter`.

- [ ] **Step 1: Failing publisher tests** — no attestation ⇒ `CapabilityUnproven` · failed attestation ⇒ refused · attestation for a **different environment** ⇒ refused · **engine versions not matching the attestation** ⇒ refused · adding a feature without `covers_schema_evolution` ⇒ refused · `render_publish` takes a **`PublisherSelection`, not a mechanism** (introspect the signature) · no `INSERT OVERWRITE` in any rendered text · the target is derived, not a parameter.
- [ ] **Step 2: Failing validation tests** — **L0 imports the project and builds the Kedro pipeline** (a project that `ast.parse` accepts but has no pipeline must FAIL) · L0 catches a hand-edited project · L1 runs over **all** IRs, all expressions and the spine, and verifies **every resolved partition exists** · L1 reads metadata only · type contradiction ⇒ `GOVERNED_FACT_MISMATCH` · missing partition ⇒ `ENVIRONMENT_OR_DATA` · unknown code ⇒ `UNCLASSIFIED` · both `GOVERNED_FACT_MISMATCH` and `UNCLASSIFIED` block regeneration · findings carry no data values · unreachable cluster ⇒ `status="error"` with zero findings · L2 not run unless requested.
- [ ] **Step 3–6:** Run/implement/run/commit — `feat(materialize): capability-gated publisher + validation loop + submitter`

---

### Task 16: LIVE — Spark-local proof, then a published cluster partition

Spec §13. **The deliverable.** Blocked on Task 0.

**Files:** Create `src/featuregen/materialize/pipeline.py`, `tests/featuregen/materialize/spark_fixtures/`; Test `test_spark_local.py`, `test_live_cluster.py`
**Produces:** `generate_group(conn, inputs, *, roles, cadence, availability_class, spine_decl, environment_id, inventory) -> GenerationResult`

- [ ] **Step 1: Hand-author the tiny fixtures** — `transactions` (a row dated `2026-07-01` **posted** `2026-07-05` for the look-ahead case; several merchants; a value forcing decimal rounding; a value forcing overflow under the declared precision), `accounts`, `customers` (including a customer with **no** transactions). Every expected value computed by hand and written in a comment beside its assertion. Add `pyspark` to dev dependencies.

- [ ] **Step 2: Failing Spark-local tests (MANDATORY, run by default)**

```python
def test_each_first_slice_feature_matches_its_hand_computed_value(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.value("1001", "total_debit_amount_30d") == Decimal("5500.00")  # 3000+2000+500
    # …one assertion per feature in the slice, each with its arithmetic in a comment


def test_look_ahead_row_is_excluded(spark_run):
    """Dated 2026-07-01, posted 2026-07-05 — invisible on the 3rd, visible on the 6th."""
    assert spark_run(business_dt="2026-07-03").value("1004", "total_debit_amount_30d") == 0
    assert spark_run(business_dt="2026-07-06").value("1004", "total_debit_amount_30d") == 250


def test_entity_with_no_transactions_still_appears(spark_run):
    assert spark_run(business_dt="2026-07-27").has_key("1099")


def test_exactly_one_row_per_key_and_date(spark_run):
    out = spark_run(business_dt="2026-07-27")
    assert out.row_count() == out.distinct_key_count()


def test_declared_rounding_is_applied(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1003", "ratio_feature") == Decimal("0.33")


def test_overflow_RAISES_rather_than_yielding_null(spark_run_overflow):
    """Spark's default is NULL on decimal overflow; OverflowBehavior.ERROR must fail."""
    with pytest.raises(Exception, match="overflow|OVERFLOW"):
        spark_run_overflow(business_dt="2026-07-27")


def test_empty_window_policy_yields_zero(spark_run):
    assert spark_run(business_dt="2026-07-27").value("1099", "total_debit_amount_30d") == 0


def test_orphan_grain_key_blocks_a_complete_population_claim(spark_run_orphan):
    with pytest.raises(Exception, match="SPINE_INCOMPLETE"):
        spark_run_orphan(business_dt="2026-07-27")


def test_duplicate_key_group_is_rejected(spark_run_dup):
    with pytest.raises(Exception, match="KEY_NOT_UNIQUE"):
        spark_run_dup(business_dt="2026-07-27")


def test_stale_staging_manifest_blocks_publication(spark_run_stale_manifest):
    with pytest.raises(Exception, match="STALE_STAGING_MANIFEST"):
        spark_run_stale_manifest(business_dt="2026-07-27")
```

- [ ] **Step 3: Run — FAIL. Then iterate: run → read the failure → fix the RENDERER (never the expected value) → re-run**, until every number is right.

- [ ] **Step 4: Failing live-cluster tests**

```python
def test_capability_is_attested_for_this_environment(live, control_plane):
    att = control_plane.attestation(environment_id="hdfc-local")
    assert att.passed and att.covers_schema_evolution
    assert att.hive_version == live.hive_version      # the attestation must match reality


def test_generate_validate_run_and_publish(live, control_plane, resolved_inputs):
    result = generate_group(control_plane.conn, resolved_inputs, ...)
    assert run_l0(result.project, workdir=live.workdir).status == "passed"
    for ir in result.irs:                              # ALL IRs, not just the first
        assert run_l1(control_plane.conn, ir, result.project, roles=(...)).status == "passed"
    live.submit_and_run(result.project, business_dt="2026-07-27")

    published = live.describe("sandbox_feature.cif_daily")
    assert published.schema == expected_schema(result.plan)          # exact schema
    for f in result.plan.features:
        assert f.column_name in published.columns
        assert published.non_null_count(f.column_name) > 0           # not a table of nulls
    assert published.generation_marker == result.generation_id
    assert published.row_count == control_plane.latest_manifest().published_row_count
    assert published.project_hash == result.project.generated_project_hash
    assert control_plane.run_status(result.run_id) == "published"


def test_the_acceptance_fixture_gives_the_SAME_numbers_on_the_cluster(live, acceptance_fixture):
    """The Spark-local proof, re-run on real Hadoop. Same inputs, same expected values."""
    out = live.run_acceptance(acceptance_fixture, business_dt="2026-07-27")
    assert out.value("1001", "total_debit_amount_30d") == Decimal("5500.00")


def test_reports_and_manifest_are_ingested(live, control_plane):
    assert control_plane.latest_manifest().generation_id
    assert control_plane.reports_for(level="L1")
```

- [ ] **Step 5: Full sweep, then commit**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula tests/featuregen/db -p no:cacheprovider -q
git add -- src/featuregen/materialize tests/featuregen/materialize pyproject.toml
git commit -m "feat(materialize): end-to-end generation, Spark-local proof, live cluster publication"
```

**Spec A is not done until `test_live_cluster.py` passes against the real cluster.**

---

## Self-Review

**Spec coverage.** §0→T0 · §1.2→T2 · §1.3→T7 · §3.1–3.2→T3 · §3.3→T5 · §3 IR→T6 · §4→T4 · §5→T9 · §6→T8 · §7 identity→T11, project→T12 · §8→T13, T16 · §9→T10, T14 · §10→T15 · §11→T15 · §12→T14 · §13→T12 (goldens), T16 (execution, cluster) · §14 vocabulary→every refusing task.

**Rev-2 findings, each owned:** forged result→T2 (terminal-event verification) · two authorization gates→T2/T7 · population source→T4 · bare table names + authority retention→T3 · fan-out refusal→T3 · snapshot identity→T5 · classification axes→T9 · fixture forgery→T2 (fixtures carry verified NON_ADDITIVE) · physical types→T8 · contract union→T9 (`MULTIPLE_MATERIALIZATION_CONTRACTS`) · stale staging→T10 · lock circularity→T11 · run manifest→T14 · attestation persistence + `PublisherSelection`→T15 · L1 over all IRs→T15/T16 · live assertions→T16.

**Placeholder scan.** No "TBD"/"handle errors appropriately". Tasks 6, 10, 11, 12, 13 list their discriminating assertions in prose rather than full code blocks — deliberate, because each is a straightforward application of a pattern shown in full in T2/T3/T8/T9, and the assertions name the exact codes and behaviours. Every *novel* mechanism has complete test code.

**Type consistency.** `materialize_hash` (T1) is the sole hasher. `AdmittedFeature` (T2) → `compile_ir` (T7). `JoinPlan` (T3) is consumed by T6. `PhysicalInputSnapshot` (T5) → T6 → `input_snapshot_ids` (T11) → L1 (T15). `PhysicalType` (T8) → `PlannedFeature` (T10) → the type gate (T14). `StagingManifestV1` (T10) is rendered in T13 and verified in T14. `CompilationIdentity` (T11) is embedded in rendered files; `generated_project_hash` lives only in `GENERATED.lock` (T12). `PublisherSelection` (T15) is what `render_publish` consumes.

**Unverified-interface check.** Every API named here appears in `docs/architecture/2026-07-27-verified-interfaces-materialization.md`. Two entries were added while writing this plan (§14: authoring result + terminal payload; and the absence of a public trace reader, which is why T2 adds one). If implementation meets an interface not in that file: **read the source, add the entry, then implement.**
