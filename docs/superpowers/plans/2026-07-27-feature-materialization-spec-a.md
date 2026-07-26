# Spec A — Executable Materialization Vertical Slice: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a governed `TypedFormulaV1` into a runnable Kedro/PySpark project that computes several features at once on Hadoop/Hive and publishes them as one complete, atomically-visible feature-group partition — with a submit → validate → classify → regenerate loop.

**Architecture:** A new `src/featuregen/materialize/` package compiles a Child-1 formula through planner intent → physical IR → output contract → group plan → computation plan, then **renders** a Kedro project. Render-only: the generated project *is* the execution path, so there is no interpreter to drift from. The platform never computes a feature; all compute happens in the data plane via generated code.

**Tech Stack:** Python 3.11 · Kedro + PySpark (generated code only) · HDFS Parquet · Hive · Postgres control plane · `pytest` + `pytest-postgresql`.

**Spec:** `docs/superpowers/specs/2026-07-27-feature-materialization-spec-a-design.md` (§0–§N). Read the section a task cites before implementing it.

## Global Constraints

Every task's requirements implicitly include these.

- **Frozen slotted dataclasses + `StrEnum`** — `@dataclass(frozen=True, slots=True)`. **NOT pydantic.**
- **Every new hash is RFC 8785 (JCS) + sha256**, via the single helper built in Task 1, which wraps `featuregen.formula._jcs.dumps`. No second canonicalization scheme anywhere.
- **Hashes cover identity fields only.** Provenance (ids, timestamps, actors) and **live observations** (current watermark, arrival time, job status) stay OUT of every hash.
- **Render-only.** Nothing in `src/` computes a feature value. No `pyspark` import in `src/featuregen/materialize/` — PySpark appears only inside *rendered text*.
- **The control plane never reads feature data.** It generates code and ingests small manifests/reports.
- **Findings and manifests carry counts, types and locations — never data values.**
- **Fail closed.** Any ungoverned or unresolvable input yields a typed refusal, never a guess or a default.
- **No new LLM call.** Spec A is deterministic end to end.
- **Publication target is derived, never passed in:** absent `formula_binding_hash`/`deployment_binding_hash` ⇒ `sandbox_feature.<group>`.
- **`INSERT OVERWRITE` is forbidden** as a publication mechanism.
- Timezones validate through `zoneinfo.ZoneInfo`.
- Commit trailer on EVERY commit: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

**Test command** (all tasks):
```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
```

---

## File Structure

```
src/featuregen/materialize/
  __init__.py
  canonical.py      T1  the ONE JCS+sha256 hasher for this package
  identity.py       T1  MaterializationIdentity + namespace derivation (§B)
  intent.py         T3  FormulaPlannerIntentV1 (A1)
  grain_path.py     T4  VERIFIED grain-path resolution (§H)
  ir.py             T5  FormulaExecutionIRV1 + physical read set + ir_hash (A2)
  contract.py       T6  MaterializationContractV1 + derivation + monotonic override (§C)
  group_plan.py     T7  FeatureGroupPlanV1 + completeness gate (§D)
  compute_plan.py   T8  ComputationPlanV1 sharing rules (§E)
  render/
    __init__.py     T9  render_project() entry point + generated_project_hash
    scaffold.py     T9  pyproject/catalog.yml/parameters.yml/GENERATED.json
    nodes_pit.py    T10 rendered PIT projection + spine nodes (§G)
    nodes_calc.py   T11 rendered per-feature calc + assemble
    nodes_gate.py   T11 rendered validation gates (§I) + run manifest (§J)
    hooks.py        T11 rendered MetricsHook + ProvenanceHook
    publish.py      T12 rendered publish node + GroupPublisher selection
  publish.py        T12 GroupPublisher seam (control-plane side: choice + refusal)
  validation.py     T13 ValidationReportV1, findings, classification (§N)
  submit.py         T14 PipelineSubmitter seam + LocalClusterSubmitter
src/featuregen/db/migrations/1021_materialization_control_plane.sql   T2

tests/featuregen/materialize/…  (mirrors the above)
tests/featuregen/materialize/goldens/…       T9-T11 golden rendered files
tests/featuregen/materialize/fixtures.py     T3  shared formula/IR builders
```

---

### Task 1: Package skeleton, the one hasher, and identity-derived namespace

Spec §B. This is the foundation every later hash and the publication target depend on.

**Files:**
- Create: `src/featuregen/materialize/__init__.py`, `src/featuregen/materialize/canonical.py`, `src/featuregen/materialize/identity.py`
- Test: `tests/featuregen/materialize/test_canonical.py`, `tests/featuregen/materialize/test_identity.py`

**Interfaces:**
- Consumes: `featuregen.formula._jcs.dumps` (vendored RFC 8785, already on main).
- Produces: `materialize_hash(payload: Mapping[str, Any]) -> str`; `PublicationTarget` (StrEnum); `MaterializationIdentity` (frozen); `MaterializationIdentity.publication_target` property; `derive_namespace(identity) -> str`.

- [ ] **Step 1: Write the failing hasher tests**

```python
# tests/featuregen/materialize/test_canonical.py
import pytest
from featuregen.materialize.canonical import materialize_hash


def test_hash_is_key_order_independent():
    assert materialize_hash({"a": 1, "b": 2}) == materialize_hash({"b": 2, "a": 1})


def test_hash_is_sha256_hex():
    h = materialize_hash({"a": 1})
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


def test_hash_distinguishes_values():
    assert materialize_hash({"a": 1}) != materialize_hash({"a": 2})


def test_hash_rejects_non_mapping():
    with pytest.raises(TypeError):
        materialize_hash([1, 2])  # type: ignore[arg-type]
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize/test_canonical.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.materialize'`

- [ ] **Step 3: Implement the hasher**

```python
# src/featuregen/materialize/canonical.py
"""The ONE canonicalizer for every hash this package mints (spec Global Constraints).

Wraps Child-1's vendored RFC 8785 (JCS) serializer so materialization hashes are
byte-comparable with ``formula_content_hash``. There is deliberately no second
canonicalization scheme in this package.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from featuregen.formula._jcs import dumps as _jcs_dumps

__all__ = ["materialize_hash"]


def materialize_hash(payload: Mapping[str, Any]) -> str:
    """``sha256(JCS(payload))`` hex digest. Key order is irrelevant by construction."""
    if not isinstance(payload, Mapping):
        raise TypeError(f"materialize_hash expects a mapping, got {type(payload).__name__}")
    return hashlib.sha256(_jcs_dumps(dict(payload))).hexdigest()
```

- [ ] **Step 4: Run to verify pass**

Run: `PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize/test_canonical.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Write the failing identity tests**

The namespace must be a *function* of which identities exist — not a flag.

```python
# tests/featuregen/materialize/test_identity.py
import pytest
from featuregen.materialize.identity import (
    MaterializationIdentity, PublicationTarget, derive_namespace,
)

_H = "a" * 64


def _identity(**kw):
    base = dict(
        formula_content_hash=_H, ir_hash=_H, materialization_contract_hash=_H,
        group_plan_hash=_H, generated_project_hash=_H,
        formula_binding_hash=None, deployment_binding_hash=None,
    )
    base.update(kw)
    return MaterializationIdentity(**base)


def test_missing_child2_hashes_force_sandbox():
    assert _identity().publication_target is PublicationTarget.SANDBOX
    assert derive_namespace(_identity()) == "sandbox_feature"


def test_both_child2_hashes_present_allow_production():
    ident = _identity(formula_binding_hash=_H, deployment_binding_hash=_H)
    assert ident.publication_target is PublicationTarget.PRODUCTION
    assert derive_namespace(ident) == "feature"


@pytest.mark.parametrize("present", ["formula_binding_hash", "deployment_binding_hash"])
def test_one_child2_hash_is_still_sandbox(present):
    # Partial identity must NOT unlock production — this is the fail-closed half.
    assert _identity(**{present: _H}).publication_target is PublicationTarget.SANDBOX


def test_sandbox_execution_hash_excludes_live_observations():
    ident = _identity()
    a = ident.sandbox_execution_hash(
        environment_id="hdfc-local", resolved_parameter_values={"business_dt": "2026-07-27"},
        business_dt="2026-07-27", input_snapshot_ids=("banking.transactions/2026-07-27",),
        compiler_version=1, renderer_version=1)
    b = ident.sandbox_execution_hash(
        environment_id="hdfc-local", resolved_parameter_values={"business_dt": "2026-07-27"},
        business_dt="2026-07-27", input_snapshot_ids=("banking.transactions/2026-07-27",),
        compiler_version=1, renderer_version=1)
    assert a == b  # same inputs -> same identity, no clock/run-id leakage


def test_production_execution_hash_refused_without_child2():
    with pytest.raises(ValueError, match="deployment_binding_hash"):
        _identity().production_execution_hash(
            environment_id="x", resolved_parameter_values={}, business_dt="2026-07-27",
            input_snapshot_ids=(), compiler_version=1, renderer_version=1)
```

- [ ] **Step 6: Run to verify failure**

Expected: FAIL — `ModuleNotFoundError: ... materialize.identity`

- [ ] **Step 7: Implement identity + namespace derivation**

```python
# src/featuregen/materialize/identity.py
"""Spec §B — identity model and the namespace that is DERIVED from its completeness.

The production namespace is unreachable without Child-2's ``formula_binding_hash`` and
``deployment_binding_hash``: there is no flag to forget and no reviewer to rely on.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.materialize.canonical import materialize_hash

__all__ = ["PublicationTarget", "MaterializationIdentity", "derive_namespace"]

SANDBOX_NAMESPACE = "sandbox_feature"
PRODUCTION_NAMESPACE = "feature"


class PublicationTarget(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


@dataclass(frozen=True, slots=True)
class MaterializationIdentity:
    """Every identity in play for one group render. Child-2's two are ``None`` in Spec A."""

    formula_content_hash: str
    ir_hash: str
    materialization_contract_hash: str
    group_plan_hash: str
    generated_project_hash: str
    formula_binding_hash: str | None = None
    deployment_binding_hash: str | None = None

    @property
    def publication_target(self) -> PublicationTarget:
        if self.formula_binding_hash and self.deployment_binding_hash:
            return PublicationTarget.PRODUCTION
        return PublicationTarget.SANDBOX

    def _run_payload(self, *, environment_id: str,
                     resolved_parameter_values: Mapping[str, Any], business_dt: str,
                     input_snapshot_ids: Sequence[str], compiler_version: int,
                     renderer_version: int) -> dict[str, Any]:
        return {
            "ir_hash": self.ir_hash,
            "materialization_contract_hash": self.materialization_contract_hash,
            "group_plan_hash": self.group_plan_hash,
            "generated_project_hash": self.generated_project_hash,
            "environment_id": environment_id,
            "resolved_parameter_values": dict(resolved_parameter_values),
            "business_dt": business_dt,
            "input_snapshot_ids": sorted(input_snapshot_ids),
            "compiler_version": compiler_version,
            "renderer_version": renderer_version,
        }

    def sandbox_execution_hash(self, **kw: Any) -> str:
        """The reduced run identity Spec A can honestly compute (§B).

        MUST NEVER be recorded under the name ``execution_hash`` — the parent's definition
        requires ``deployment_binding_hash``, which does not exist yet.
        """
        return materialize_hash(self._run_payload(**kw))

    def production_execution_hash(self, **kw: Any) -> str:
        """The parent's ``execution_hash``. Refuses until Child-2 supplies its inputs."""
        if not self.deployment_binding_hash:
            raise ValueError(
                "production execution_hash requires deployment_binding_hash (Child-2); "
                "use sandbox_execution_hash")
        payload = self._run_payload(**kw)
        payload["deployment_binding_hash"] = self.deployment_binding_hash
        payload["formula_binding_hash"] = self.formula_binding_hash
        return materialize_hash(payload)


def derive_namespace(identity: MaterializationIdentity) -> str:
    """The Hive namespace, derived from identity completeness. The ONLY place this is decided."""
    if identity.publication_target is PublicationTarget.PRODUCTION:
        return PRODUCTION_NAMESPACE
    return SANDBOX_NAMESPACE
```

Also create `src/featuregen/materialize/__init__.py` containing only a module docstring, and `tests/featuregen/materialize/__init__.py` is **not** needed (the repo uses `--import-mode=importlib`).

- [ ] **Step 8: Run to verify pass**

Expected: PASS (9 passed across both files)

- [ ] **Step 9: Commit**

```bash
git add src/featuregen/materialize tests/featuregen/materialize
git commit -m "feat(materialize): JCS hasher + identity-derived publication namespace"
```

---

### Task 2: Migration 1021 — control-plane tables

Spec §J, §N. The control plane records what it **generated**, what came back from **validation**, and what a run **reported**. Three append-only tables.

**Files:**
- Create: `src/featuregen/db/migrations/1021_materialization_control_plane.sql`
- Test: `tests/featuregen/materialize/test_migration_1021.py`

**Interfaces:**
- Produces: tables `materialization_generation`, `pipeline_validation_report`, `materialization_run_manifest`.

**Note:** `1020` is the current maximum. Do **not** renumber existing migrations.

- [ ] **Step 1: Write the failing migration test**

```python
# tests/featuregen/materialize/test_migration_1021.py
import pytest
from psycopg import errors


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        (table,)).fetchall()}


def test_generation_table_shape(db):
    cols = _cols(db, "materialization_generation")
    assert {"generation_id", "group_plan_hash", "generated_project_hash",
            "materialization_contract_hash", "publication_target", "created_at"} <= cols


def test_generation_is_write_once(db):
    db.execute(
        "INSERT INTO materialization_generation (generation_id, group_plan_hash, "
        "generated_project_hash, materialization_contract_hash, publication_target) "
        "VALUES ('gen_1', 'g', 'p', 'c', 'sandbox')")
    with pytest.raises(errors.RaiseException):
        db.execute("UPDATE materialization_generation SET group_plan_hash = 'x'")


def test_validation_report_rejects_unknown_level(db):
    db.execute(
        "INSERT INTO materialization_generation (generation_id, group_plan_hash, "
        "generated_project_hash, materialization_contract_hash, publication_target) "
        "VALUES ('gen_2', 'g', 'p', 'c', 'sandbox')")
    with pytest.raises(errors.CheckViolation):
        db.execute(
            "INSERT INTO pipeline_validation_report (report_id, generation_id, level, status) "
            "VALUES ('rep_1', 'gen_2', 'L9', 'passed')")


def test_run_manifest_status_is_closed(db):
    with pytest.raises(errors.CheckViolation):
        db.execute(
            "INSERT INTO materialization_run_manifest (run_id, group_plan_hash, business_dt, "
            "status) VALUES ('run_1', 'g', '2026-07-27', 'whatever')")
```

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `relation "materialization_generation" does not exist`

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/1021_materialization_control_plane.sql
-- Spec A §J/§N control plane. THREE append-only records: what we generated, what validation
-- said, what a run reported. No feature data is ever stored here (spec Global Constraints).

CREATE TABLE IF NOT EXISTS materialization_generation (
    generation_id                 text        PRIMARY KEY,
    group_plan_hash               text        NOT NULL,
    generated_project_hash        text        NOT NULL,
    materialization_contract_hash text        NOT NULL,
    -- Derived from identity completeness (§B); Spec A can only ever write 'sandbox'.
    publication_target            text        NOT NULL CHECK (publication_target IN ('sandbox','production')),
    formula_content_hashes        jsonb       NOT NULL DEFAULT '[]',
    renderer_version              integer     NOT NULL DEFAULT 1,
    created_at                    timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION materialization_generation_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'materialization_generation is write-once: % not allowed on generation_id=%',
        TG_OP, COALESCE(OLD.generation_id, NEW.generation_id);
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE TRIGGER materialization_generation_no_mutation
    BEFORE UPDATE OR DELETE ON materialization_generation
    FOR EACH ROW EXECUTE FUNCTION materialization_generation_write_once();

CREATE TABLE IF NOT EXISTS pipeline_validation_report (
    report_id       text        PRIMARY KEY,
    generation_id   text        NOT NULL REFERENCES materialization_generation(generation_id),
    level           text        NOT NULL CHECK (level IN ('L0','L1','L2')),
    status          text        NOT NULL CHECK (status IN ('passed','failed','error')),
    environment_id  text        NULL,
    -- Findings carry counts/types/locations only — NEVER data values (§N egress rule).
    findings        jsonb       NOT NULL DEFAULT '[]',
    started_at      timestamptz NULL,
    finished_at     timestamptz NULL,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pipeline_validation_report_gen_idx
    ON pipeline_validation_report (generation_id, level);

CREATE TABLE IF NOT EXISTS materialization_run_manifest (
    run_id                      text        PRIMARY KEY,
    group_plan_hash             text        NOT NULL,
    sandbox_execution_hash      text        NULL,
    business_dt                 date        NOT NULL,
    expected_feature_columns    jsonb       NOT NULL DEFAULT '[]',
    staged_row_count            bigint      NULL,
    published_row_count         bigint      NULL,
    schema_hash                 text        NULL,
    key_uniqueness_result       text        NULL,
    required_column_result      text        NULL,
    publication_location        text        NULL,
    started_at                  timestamptz NULL,
    published_at                timestamptz NULL,
    status                      text        NOT NULL
        CHECK (status IN ('running','validated','published','rejected','failed')),
    created_at                  timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 4: Run to verify pass**

Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/1021_materialization_control_plane.sql tests/featuregen/materialize/test_migration_1021.py
git commit -m "feat(materialize): migration 1021 — materialization control-plane tables"
```

---

### Task 3: `FormulaPlannerIntentV1` — logical requirements with exact ref preservation

Spec §A1. Translates a formula into requirements **without resolving anything physical**, and must never substitute a similar column.

**Files:**
- Create: `src/featuregen/materialize/intent.py`, `tests/featuregen/materialize/fixtures.py`
- Test: `tests/featuregen/materialize/test_intent.py`

**Interfaces:**
- Consumes: from `featuregen.formula.schema` — `TypedFormulaV1`, `Grain`, `WindowPolicy`, `AggregateExpression`, `UnaryBody`, `RatioBody`, `DiffBody`, `FilterNode`, `FilterPredicate`, `FilterBool`, `AggregateFunction`, `LogicalRef`.
- Produces: `RefRole` (StrEnum: `MEASURE`, `FILTER`, `EVENT_TIME`, `GRAIN_KEY`, `SOURCE_TABLE`); `RequiredRef` (frozen: `logical_ref: str`, `role: RefRole`, `expr_path: str | None`); `FormulaPlannerIntentV1` (frozen); `build_planner_intent(formula: TypedFormulaV1) -> FormulaPlannerIntentV1`.
- The `expr_path` values are Child-1's body paths, reused verbatim: `body.expr`, `body.numerator`, `body.denominator`, `body.minuend`, `body.subtrahend`.

- [ ] **Step 1: Write the shared fixtures**

```python
# tests/featuregen/materialize/fixtures.py
"""Hand-authored formula fixtures for the materialize package.

Deliberately NOT derived from src/ helpers — a fixture built by the code under test asserts
only that the code agrees with itself (the Child-1 lesson).
"""
from __future__ import annotations

from featuregen.formula.schema import (
    AggregateExpression, AggregateFunction, DecimalPolicy, EmptyWindowResult, FilterPredicate,
    FilterPredicateOp, FinalOperation, Grain, Inclusivity, NullInput, OverflowBehavior,
    RoundingMode, SourceRelation, TypedFormulaV1, TypedLiteral, LiteralType, UnaryBody,
    WindowBasis, WindowPolicy, FormulaOutputPolicyV1, AdditivityClass,
)

SRC = "hdfc"
TXN = f"{SRC}::banking.transactions"
AMOUNT = f"{TXN}.amount"
TXN_DATE = f"{TXN}.transaction_date"
POSTED_AT = f"{TXN}.posted_at"
TXN_TYPE = f"{TXN}.transaction_type"
ACCOUNT_ID = f"{TXN}.account_id"
CIF_ID = f"{SRC}::banking.accounts.cif_id"


def debit_filter() -> FilterPredicate:
    return FilterPredicate(
        kind="predicate", op=FilterPredicateOp.EQUAL, left=TXN_TYPE,
        right_literal=TypedLiteral(literal_type=LiteralType.STRING, value="debit"),
        right_set=(), right_ref=None, right_param=None)


def window_30d() -> WindowPolicy:
    return WindowPolicy(
        event_time_ref=TXN_DATE, basis=WindowBasis.TRAILING, length=30,
        unit="day", start_inclusive=Inclusivity.INCLUSIVE,
        end_inclusive=Inclusivity.INCLUSIVE, timezone="Asia/Dubai",
        empty_window=EmptyWindowResult.ZERO, null_input=NullInput.IGNORE)


def total_debit_amount_30d() -> TypedFormulaV1:
    """SUM(amount) WHERE transaction_type='debit' over a trailing 30d window, per CIF."""
    return TypedFormulaV1(
        formula_schema_version=1, operation_grammar_version=1, output_policy_version=1,
        canonicalization_version=1,
        grain=Grain(entity="customer", keys=(CIF_ID,)),
        body=UnaryBody(expr=AggregateExpression(
            aggregation=AggregateFunction.SUM, operand=AMOUNT,
            source_relation=SourceRelation(table_ref=TXN), filter=debit_filter(),
            window=window_30d())),
        parameters=(),
        decimal=DecimalPolicy(precision=18, scale=2, rounding=RoundingMode.HALF_UP,
                              overflow=OverflowBehavior.ERROR),
        output=FormulaOutputPolicyV1(output_type="numeric", unit=None, currency=None,
                                     output_additivity=AdditivityClass.ADDITIVE,
                                     external_type_required=False))
```

> **Check the real field names before writing this.** Open `src/featuregen/formula/schema.py` and match `FilterPredicate`, `SourceRelation`, `WindowPolicy` and `TypedFormulaV1` field-for-field; adjust the fixture if a name differs. A fixture that does not construct is the first thing to fix.

- [ ] **Step 2: Write the failing intent tests**

```python
# tests/featuregen/materialize/test_intent.py
from featuregen.materialize.intent import RefRole, build_planner_intent
from tests.featuregen.materialize.fixtures import (
    AMOUNT, CIF_ID, TXN, TXN_DATE, TXN_TYPE, total_debit_amount_30d,
)


def _refs(intent, role):
    return {r.logical_ref for r in intent.required_refs if r.role is role}


def test_measure_filter_event_time_and_grain_are_all_required():
    intent = build_planner_intent(total_debit_amount_30d())
    assert _refs(intent, RefRole.MEASURE) == {AMOUNT}
    assert _refs(intent, RefRole.FILTER) == {TXN_TYPE}
    assert _refs(intent, RefRole.EVENT_TIME) == {TXN_DATE}
    assert _refs(intent, RefRole.GRAIN_KEY) == {CIF_ID}
    assert _refs(intent, RefRole.SOURCE_TABLE) == {TXN}


def test_refs_are_preserved_byte_exactly():
    intent = build_planner_intent(total_debit_amount_30d())
    # No normalization, no lower-casing, no substitution of a "similar" column.
    assert AMOUNT in {r.logical_ref for r in intent.required_refs}


def test_expr_paths_use_child1_body_path_vocabulary():
    intent = build_planner_intent(total_debit_amount_30d())
    paths = {r.expr_path for r in intent.required_refs if r.expr_path}
    assert paths == {"body.expr"}


def test_intent_resolves_nothing_physical():
    intent = build_planner_intent(total_debit_amount_30d())
    # A physical table/column would be a leak of the NEXT stage's job.
    assert not hasattr(intent, "physical_read_set")
    for ref in intent.required_refs:
        assert ref.logical_ref.startswith("hdfc::")


def test_grain_key_order_is_semantic():
    intent = build_planner_intent(total_debit_amount_30d())
    assert intent.grain_keys == (CIF_ID,)
```

- [ ] **Step 3: Run to verify failure**

Expected: FAIL — `ModuleNotFoundError: ... materialize.intent`

- [ ] **Step 4: Implement `intent.py`**

```python
# src/featuregen/materialize/intent.py
"""Spec §A1 — the logical requirements list.

Preserves ``logical_ref`` strings byte-exactly and resolves nothing physical. The
requirements say "flour, 500g, do not substitute corn flour"; picking the warehouse shelf
is :mod:`featuregen.materialize.ir`'s job.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.formula.schema import (
    AggregateExpression, DiffBody, FilterBool, FilterNode, FilterPredicate, RatioBody,
    TypedFormulaV1, UnaryBody,
)

__all__ = ["RefRole", "RequiredRef", "FormulaPlannerIntentV1", "build_planner_intent"]


class RefRole(StrEnum):
    MEASURE = "measure"
    FILTER = "filter"
    EVENT_TIME = "event_time"
    GRAIN_KEY = "grain_key"
    SOURCE_TABLE = "source_table"


@dataclass(frozen=True, slots=True)
class RequiredRef:
    logical_ref: str
    role: RefRole
    expr_path: str | None = None


@dataclass(frozen=True, slots=True)
class FormulaPlannerIntentV1:
    entity: str
    grain_keys: tuple[str, ...]          # ORDER IS SEMANTIC
    required_refs: tuple[RequiredRef, ...]
    formula_content_hash: str


def _body_expressions(body: object) -> tuple[tuple[str, AggregateExpression], ...]:
    """Child-1's body-path vocabulary, reused verbatim so downstream keys match."""
    if isinstance(body, UnaryBody):
        return (("body.expr", body.expr),)
    if isinstance(body, RatioBody):
        return (("body.numerator", body.numerator), ("body.denominator", body.denominator))
    if isinstance(body, DiffBody):
        return (("body.minuend", body.minuend), ("body.subtrahend", body.subtrahend))
    raise TypeError(f"unknown formula body: {type(body).__name__}")


def _filter_refs(node: FilterNode | None) -> tuple[str, ...]:
    if node is None:
        return ()
    if isinstance(node, FilterPredicate):
        refs = [node.left]
        if getattr(node, "right_ref", None):
            refs.append(node.right_ref)  # type: ignore[arg-type]
        return tuple(refs)
    if isinstance(node, FilterBool):
        out: list[str] = []
        for child in node.operands:
            out.extend(_filter_refs(child))
        return tuple(out)
    raise TypeError(f"unknown filter node: {type(node).__name__}")


def build_planner_intent(formula: TypedFormulaV1) -> FormulaPlannerIntentV1:
    from featuregen.formula.canonical import formula_content_hash

    refs: list[RequiredRef] = [
        RequiredRef(logical_ref=key, role=RefRole.GRAIN_KEY) for key in formula.grain.keys
    ]
    for path, expr in _body_expressions(formula.body):
        refs.append(RequiredRef(expr.source_relation.table_ref, RefRole.SOURCE_TABLE, path))
        if expr.operand is not None:  # None IFF COUNT_ROWS
            refs.append(RequiredRef(expr.operand, RefRole.MEASURE, path))
        refs.append(RequiredRef(expr.window.event_time_ref, RefRole.EVENT_TIME, path))
        for fref in _filter_refs(expr.filter):
            refs.append(RequiredRef(fref, RefRole.FILTER, path))

    seen: set[tuple[str, RefRole, str | None]] = set()
    unique: list[RequiredRef] = []
    for r in refs:  # stable de-dup; order is deterministic for hashing downstream
        key = (r.logical_ref, r.role, r.expr_path)
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return FormulaPlannerIntentV1(
        entity=formula.grain.entity, grain_keys=tuple(formula.grain.keys),
        required_refs=tuple(unique), formula_content_hash=formula_content_hash(formula))
```

- [ ] **Step 5: Run to verify pass**

Expected: PASS (5 passed)

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/materialize/intent.py tests/featuregen/materialize/fixtures.py tests/featuregen/materialize/test_intent.py
git commit -m "feat(materialize): FormulaPlannerIntentV1 with exact logical-ref preservation"
```

---

### Task 4: VERIFIED grain-path resolution

Spec §H. `transactions` has `account_id`; the grain is `cif_id`. Reaching it is allowed **only** through VERIFIED governed bridges, and fails closed otherwise.

**Files:**
- Create: `src/featuregen/materialize/grain_path.py`
- Test: `tests/featuregen/materialize/test_grain_path.py`

**Interfaces:**
- Consumes: `featuregen.overlay.upload.bridge_projection.active_bridges(conn) -> tuple[ActiveBridgeV1, ...]` (VERIFIED-only, ordered; fields `fact_key, entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref` where an object ref is `"{schema}.{table}.{column}"`); `featuregen.overlay.upload.object_ref.parse_ref(logical_ref) -> (source, schema, table, column | None)`.
- Produces: `GrainHop` (frozen: `from_ref: str`, `to_ref: str`, `fact_key: str`); `GrainPath` (frozen: `hops: tuple[GrainHop, ...]`, `direct: bool`); `GrainPathUngoverned` (frozen: `reason: str = "GRAIN_PATH_NOT_GOVERNED"`, `missing_from: str`, `missing_to: str`); `resolve_grain_path(conn, *, table_ref: str, grain_key_ref: str, table_columns: frozenset[str]) -> GrainPath | GrainPathUngoverned`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_grain_path.py
from featuregen.materialize.grain_path import (
    GrainPath, GrainPathUngoverned, resolve_grain_path,
)

TXN = "hdfc::banking.transactions"
CIF_IN_TXN = "hdfc::banking.transactions.cif_id"
CIF_IN_ACCOUNTS = "hdfc::banking.accounts.cif_id"


def _seed_verified_bridge(db, *, entity, left, right):
    """Insert directly into the PROJECTION of VERIFIED bridges (what active_bridges reads)."""
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, "
        "left_object_ref, right_catalog_source, right_object_ref, status) "
        "VALUES (%s, %s, 'hdfc', %s, 'hdfc', %s, 'VERIFIED')",
        (f"bridge:{left}:{right}", entity, left, right))


def test_grain_key_present_in_table_needs_no_hop(db):
    result = resolve_grain_path(
        db, table_ref=TXN, grain_key_ref=CIF_IN_TXN,
        table_columns=frozenset({"amount", "cif_id", "posted_at"}))
    assert isinstance(result, GrainPath)
    assert result.direct is True and result.hops == ()


def test_verified_bridge_reaches_the_grain(db):
    _seed_verified_bridge(db, entity="customer",
                          left="banking.transactions.account_id",
                          right="banking.accounts.account_id")
    result = resolve_grain_path(
        db, table_ref=TXN, grain_key_ref=CIF_IN_ACCOUNTS,
        table_columns=frozenset({"amount", "account_id", "posted_at"}))
    assert isinstance(result, GrainPath)
    assert result.direct is False
    assert result.hops[0].from_ref == "banking.transactions.account_id"
    assert result.hops[0].to_ref == "banking.accounts.account_id"


def test_no_verified_bridge_fails_closed(db):
    result = resolve_grain_path(
        db, table_ref=TXN, grain_key_ref=CIF_IN_ACCOUNTS,
        table_columns=frozenset({"amount", "account_id"}))
    assert isinstance(result, GrainPathUngoverned)
    assert result.reason == "GRAIN_PATH_NOT_GOVERNED"


def test_an_unverified_bridge_is_not_a_path(db):
    db.execute(
        "INSERT INTO entity_bridge_edge (fact_key, entity_id, left_catalog_source, "
        "left_object_ref, right_catalog_source, right_object_ref, status) "
        "VALUES ('b1', 'customer', 'hdfc', 'banking.transactions.account_id', "
        "'hdfc', 'banking.accounts.account_id', 'PROPOSED')")
    result = resolve_grain_path(
        db, table_ref=TXN, grain_key_ref=CIF_IN_ACCOUNTS,
        table_columns=frozenset({"account_id"}))
    assert isinstance(result, GrainPathUngoverned)  # PROPOSED is not operational
```

> Confirm `entity_bridge_edge`'s real column list first (`grep -n "entity_bridge_edge" src/featuregen/db/migrations/*.sql`) and adjust the seeding INSERTs to match. Seed through the projection table because that is exactly what `active_bridges` reads.

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `ModuleNotFoundError: ... materialize.grain_path`

- [ ] **Step 3: Implement `grain_path.py`**

```python
# src/featuregen/materialize/grain_path.py
"""Spec §H — reach the grain through VERIFIED governed bridges ONLY.

An aggregate source frequently lacks the grain key (``transactions`` has ``account_id``,
the grain is ``cif_id``). One hop is supported in this slice; anything else fails closed
with ``GRAIN_PATH_NOT_GOVERNED``. This module NEVER infers or hard-codes a join.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.object_ref import parse_ref

__all__ = ["GrainHop", "GrainPath", "GrainPathUngoverned", "resolve_grain_path"]

GRAIN_PATH_NOT_GOVERNED = "GRAIN_PATH_NOT_GOVERNED"


@dataclass(frozen=True, slots=True)
class GrainHop:
    from_ref: str          # "{schema}.{table}.{column}" on the aggregate side
    to_ref: str            # "{schema}.{table}.{column}" on the grain side
    fact_key: str          # the VERIFIED bridge that authorises this hop


@dataclass(frozen=True, slots=True)
class GrainPath:
    hops: tuple[GrainHop, ...]
    direct: bool


@dataclass(frozen=True, slots=True)
class GrainPathUngoverned:
    missing_from: str
    missing_to: str
    reason: str = GRAIN_PATH_NOT_GOVERNED


def _object_path(logical_ref: str) -> str:
    """``hdfc::banking.accounts.cif_id`` -> ``banking.accounts.cif_id`` (bridge-edge form)."""
    _source, schema, table, column = parse_ref(logical_ref)
    return f"{schema}.{table}.{column}" if column else f"{schema}.{table}"


def resolve_grain_path(conn, *, table_ref: str, grain_key_ref: str,
                       table_columns: frozenset[str]) -> GrainPath | GrainPathUngoverned:
    """How ``table_ref`` reaches ``grain_key_ref``. Fails closed when no VERIFIED path exists."""
    _s, _sc, _t, grain_column = parse_ref(grain_key_ref)
    if grain_column and grain_column in table_columns:
        return GrainPath(hops=(), direct=True)

    aggregate_table = _object_path(table_ref)          # "banking.transactions"
    grain_object = _object_path(grain_key_ref)         # "banking.accounts.cif_id"
    grain_table = grain_object.rsplit(".", 1)[0]       # "banking.accounts"

    for bridge in active_bridges(conn):                # VERIFIED-only by construction
        left, right = bridge.left_object_ref, bridge.right_object_ref
        for near, far in ((left, right), (right, left)):
            if near.startswith(f"{aggregate_table}.") and far.startswith(f"{grain_table}."):
                return GrainPath(
                    hops=(GrainHop(from_ref=near, to_ref=far, fact_key=bridge.fact_key),),
                    direct=False)

    return GrainPathUngoverned(missing_from=aggregate_table, missing_to=grain_object)
```

- [ ] **Step 4: Run to verify pass**

Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/grain_path.py tests/featuregen/materialize/test_grain_path.py
git commit -m "feat(materialize): VERIFIED-only grain-path resolution, fail closed"
```

---

### Task 5: `FormulaExecutionIRV1` — physical read set, PIT columns, `ir_hash`

Spec §A2. The single compiled plan both the contract derivation and the renderer consume. **The read set must be complete** — operands, filter columns, event-time columns, join keys *and* bridge tables — because §C derives sensitivity from it.

**Files:**
- Create: `src/featuregen/materialize/ir.py`
- Test: `tests/featuregen/materialize/test_ir.py`

**Interfaces:**
- Consumes: `build_planner_intent` (T3); `resolve_grain_path` (T4); `materialize_hash` (T1); `featuregen.overlay.facts.AVAILABILITY_TIME`, `.GRAIN`; `featuregen.overlay.store.load_fact`; `featuregen.overlay.upload.operational_facts.read_operational_value`.
- Produces: `PhysicalRef` (frozen: `logical_ref`, `schema`, `table`, `column | None`, `role: RefRole`, `expr_path: str | None`); `PitSpec` (frozen: `event_time_column`, `availability_column`, `availability_basis`, `lag_hours: float | None`, `window_basis`, `window_length`, `window_unit`, `start_inclusive`, `end_inclusive`, `timezone`); `FormulaExecutionIRV1` (frozen); `IrUngoverned` (frozen: `reason`, `detail`); `compile_ir(conn, formula) -> FormulaExecutionIRV1 | IrUngoverned`; `ir_hash(ir) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_ir.py
import pytest
from featuregen.materialize.ir import FormulaExecutionIRV1, IrUngoverned, compile_ir, ir_hash
from tests.featuregen.materialize.fixtures import AMOUNT, TXN_TYPE, total_debit_amount_30d


def test_read_set_includes_filter_and_event_time_not_just_the_operand(db, seeded_catalog):
    ir = compile_ir(db, total_debit_amount_30d())
    assert isinstance(ir, FormulaExecutionIRV1)
    refs = {r.logical_ref for r in ir.physical_read_set}
    assert AMOUNT in refs          # operand
    assert TXN_TYPE in refs        # filter column — §C derives sensitivity from these too
    assert any(r.column == "transaction_date" for r in ir.physical_read_set)


def test_pit_comes_from_the_governed_availability_fact(db, seeded_catalog):
    ir = compile_ir(db, total_debit_amount_30d())
    assert ir.pit.availability_column == "posted_at"
    assert ir.pit.availability_basis == "posted_at"


def test_missing_availability_fact_fails_closed(db, seeded_catalog_without_availability):
    result = compile_ir(db, total_debit_amount_30d())
    assert isinstance(result, IrUngoverned)
    assert result.reason == "AVAILABILITY_TIME_NOT_GOVERNED"


def test_ungoverned_grain_path_fails_closed(db, seeded_catalog_without_bridge):
    result = compile_ir(db, total_debit_amount_30d())
    assert isinstance(result, IrUngoverned)
    assert result.reason == "GRAIN_PATH_NOT_GOVERNED"


def test_ir_hash_is_stable_and_excludes_provenance(db, seeded_catalog):
    ir = compile_ir(db, total_debit_amount_30d())
    assert ir_hash(ir) == ir_hash(ir)
    payload = ir.identity_payload()
    for banned in ("compiled_at", "compiled_by", "run_id", "generation_id"):
        assert banned not in payload


def test_output_policy_is_carried_never_rederived(db, seeded_catalog):
    formula = total_debit_amount_30d()
    ir = compile_ir(db, formula)
    assert ir.output_policy == formula.output      # byte-identical, Child-1 is authoritative
```

`seeded_catalog` and its two negative variants are fixtures this task also writes, in `tests/featuregen/materialize/conftest.py`. Seed governed facts through the **real** commands (the pattern in `tests/featuregen/formula/c1_fixtures.py`: evidence → decision → projection), not flat inserts.

- [ ] **Step 2: Run to verify failure**

Expected: FAIL — `ModuleNotFoundError: ... materialize.ir`

- [ ] **Step 3: Implement `ir.py`**

Structure (write the full module; the load-bearing parts are shown):

```python
# src/featuregen/materialize/ir.py  (excerpt — the identity and fail-closed core)
AVAILABILITY_TIME_NOT_GOVERNED = "AVAILABILITY_TIME_NOT_GOVERNED"


@dataclass(frozen=True, slots=True)
class FormulaExecutionIRV1:
    formula_content_hash: str
    entity: str
    grain_keys: tuple[str, ...]
    physical_read_set: tuple[PhysicalRef, ...]     # COMPLETE: operands+filters+event time+join keys+bridges
    grain_paths: tuple[tuple[str, GrainPath], ...]  # (expr_path, path)
    pit: PitSpec
    catalog_state_stamp: tuple[tuple[str, str], ...]  # (fact_key, fact_event_id) pairs, sorted
    operation: Mapping[str, Any]                    # aggregation/final op/filter tree/decimal/null policy
    output_policy: FormulaOutputPolicyV1            # CARRIED from Child-1, never re-derived

    def identity_payload(self) -> dict[str, Any]:
        """Identity fields ONLY — no provenance, no timestamps, no run ids."""
        return {
            "formula_content_hash": self.formula_content_hash,
            "entity": self.entity,
            "grain_keys": list(self.grain_keys),
            "physical_read_set": [
                {"logical_ref": r.logical_ref, "schema": r.schema, "table": r.table,
                 "column": r.column, "role": str(r.role), "expr_path": r.expr_path}
                for r in sorted(self.physical_read_set,
                                key=lambda r: (r.logical_ref, str(r.role), r.expr_path or ""))],
            "grain_paths": [[p, {"direct": g.direct,
                                 "hops": [{"from": h.from_ref, "to": h.to_ref} for h in g.hops]}]
                            for p, g in self.grain_paths],
            "pit": asdict(self.pit),
            "catalog_state_stamp": [list(x) for x in self.catalog_state_stamp],
            "operation": dict(self.operation),
            "output_policy": asdict(self.output_policy),
        }


def ir_hash(ir: FormulaExecutionIRV1) -> str:
    return materialize_hash(ir.identity_payload())
```

`compile_ir` must, in order: build the intent (T3); read the `GRAIN` and `AVAILABILITY_TIME` governed facts for each expression's source table and return `IrUngoverned(AVAILABILITY_TIME_NOT_GOVERNED, …)` if absent; resolve each expression's grain path (T4) and return `IrUngoverned(GRAIN_PATH_NOT_GOVERNED, …)` if ungoverned; expand the read set to include every bridge hop's columns; and carry `formula.output` through untouched.

- [ ] **Step 4: Run to verify pass**

Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/ir.py tests/featuregen/materialize/test_ir.py tests/featuregen/materialize/conftest.py
git commit -m "feat(materialize): FormulaExecutionIRV1 — complete read set, governed PIT, ir_hash"
```

---

### Task 6: `MaterializationContractV1` — derived vs declared, monotonic override

Spec §C. The contract hash is the **group key**. Derivation runs over the **IR's physical read set** — join keys and bridge tables included — not the formula's operands.

**Files:**
- Create: `src/featuregen/materialize/contract.py`
- Test: `tests/featuregen/materialize/test_contract.py`

**Interfaces:**
- Consumes: `FormulaExecutionIRV1` (T5); `materialize_hash` (T1); `featuregen.overlay.upload.column_authority.read_column_facts`.
- Produces: `CadenceDecl` (frozen: `period`, `timezone`, `business_date_cutoff`, `trigger`); `SensitivityClass`/`AccessClass`/`RetentionClass`/`AvailabilityClass` (StrEnums, ordered strictest-last); `ContractOverrides` (frozen, all optional); `MaterializationContractV1` (frozen); `OverrideRefused` (Exception); `derive_contract(conn, ir, *, cadence, availability_class, overrides=None) -> MaterializationContractV1`; `contract_hash(contract) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_contract.py
import pytest
from featuregen.materialize.contract import (
    AvailabilityClass, CadenceDecl, ContractOverrides, OverrideRefused, SensitivityClass,
    contract_hash, derive_contract,
)

DAILY = CadenceDecl(period="daily", timezone="Asia/Dubai",
                    business_date_cutoff="23:59", trigger="scheduled")


def test_sensitivity_is_derived_from_the_whole_read_set_not_just_operands(db, ir_with_restricted_join_key):
    c = derive_contract(db, ir_with_restricted_join_key, cadence=DAILY,
                        availability_class=AvailabilityClass.T_PLUS_1)
    # The restricted column is a JOIN KEY, not an operand — it must still restrict the group.
    assert c.sensitivity_class is SensitivityClass.RESTRICTED


def test_override_may_only_go_stricter(db, plain_ir):
    c = derive_contract(db, plain_ir, cadence=DAILY,
                        availability_class=AvailabilityClass.T_PLUS_3,
                        overrides=ContractOverrides(availability_class=AvailabilityClass.T_PLUS_4))
    assert c.availability_class is AvailabilityClass.T_PLUS_4


def test_override_may_not_loosen(db, plain_ir):
    with pytest.raises(OverrideRefused, match="availability"):
        derive_contract(db, plain_ir, cadence=DAILY,
                        availability_class=AvailabilityClass.T_PLUS_3,
                        overrides=ContractOverrides(availability_class=AvailabilityClass.T_PLUS_1))


def test_invalid_timezone_is_refused(db, plain_ir):
    with pytest.raises(ValueError, match="timezone"):
        CadenceDecl(period="daily", timezone="Mars/Olympus",
                    business_date_cutoff="23:59", trigger="scheduled")


def test_window_length_is_not_cadence(db, ir_30d, ir_90d):
    a = derive_contract(db, ir_30d, cadence=DAILY, availability_class=AvailabilityClass.T_PLUS_1)
    b = derive_contract(db, ir_90d, cadence=DAILY, availability_class=AvailabilityClass.T_PLUS_1)
    # 30d and 90d trailing windows are BOTH daily -> same group.
    assert contract_hash(a) == contract_hash(b)


def test_hash_excludes_live_observations(db, plain_ir):
    c = derive_contract(db, plain_ir, cadence=DAILY, availability_class=AvailabilityClass.T_PLUS_1)
    payload = c.identity_payload()
    for banned in ("current_watermark", "actual_arrival_at", "job_status", "run_id", "observed_at"):
        assert banned not in payload


def test_dependencies_ready_trigger_is_refused_in_this_slice():
    with pytest.raises(ValueError, match="dependencies_ready"):
        CadenceDecl(period="daily", timezone="Asia/Dubai",
                    business_date_cutoff="23:59", trigger="dependencies_ready")
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError: ... materialize.contract`

- [ ] **Step 3: Implement `contract.py`**

Key requirements, all test-covered above:

```python
# src/featuregen/materialize/contract.py  (excerpt — the monotonic rule and the hash payload)
_STRICTNESS: dict[type, tuple] = {
    SensitivityClass: (SensitivityClass.PUBLIC, SensitivityClass.INTERNAL, SensitivityClass.RESTRICTED),
    AvailabilityClass: (AvailabilityClass.T_PLUS_1, AvailabilityClass.T_PLUS_2,
                        AvailabilityClass.T_PLUS_3, AvailabilityClass.T_PLUS_4),
}


def _apply_override(field: str, derived, requested):
    """Monotonic: stricter/later accepted, looser/earlier REFUSED (§C)."""
    if requested is None:
        return derived
    order = _STRICTNESS[type(derived)]
    if order.index(requested) < order.index(derived):
        raise OverrideRefused(
            f"{field}: cannot loosen derived {derived} to {requested}; overrides are monotonic")
    return requested
```

`CadenceDecl.__post_init__` validates `timezone` through `zoneinfo.ZoneInfo` and refuses `trigger="dependencies_ready"` with a message naming it (deferred — needs the source-delivery SLA). `identity_payload()` includes entity, ordered keys, pit semantics, the four classes, cadence, publication policy, backfill boundary/isolation key and every policy version — and **nothing** observational.

- [ ] **Step 4: Run to verify pass** — Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/contract.py tests/featuregen/materialize/test_contract.py
git commit -m "feat(materialize): MaterializationContractV1 — derived classes, monotonic overrides"
```

---

### Task 7: `FeatureGroupPlanV1` and the completeness gate

Spec §D. The contract says what *may* travel together; the plan says what *is* in this shipment. The completeness gate is what stops a two-column table shipping when three were promised.

**Files:**
- Create: `src/featuregen/materialize/group_plan.py`
- Test: `tests/featuregen/materialize/test_group_plan.py`

**Interfaces:**
- Consumes: `MaterializationContractV1`, `contract_hash` (T6); `FormulaExecutionIRV1`, `ir_hash` (T5); `materialize_hash` (T1).
- Produces: `PlannedFeature` (frozen: `intent_feature_name`, `column_name`, `column_type`, `formula_content_hash`, `ir_hash`); `FeatureGroupPlanV1` (frozen: `materialization_contract_hash`, `features: tuple[PlannedFeature, ...]`, `key_columns: tuple[str, ...]`, `business_dt_column: str = "business_dt"`); `GroupPlanError` (Exception); `build_group_plan(contract, entries) -> FeatureGroupPlanV1`; `group_plan_hash(plan) -> str`; `expected_schema(plan) -> tuple[tuple[str, str], ...]`; `check_completeness(plan, observed_schema) -> tuple[CompletenessFinding, ...]`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_group_plan.py
import pytest
from featuregen.materialize.group_plan import (
    GroupPlanError, build_group_plan, check_completeness, expected_schema, group_plan_hash,
)


def test_expected_schema_is_keys_then_business_dt_then_features(three_feature_plan):
    assert expected_schema(three_feature_plan)[:2] == (
        ("cif_id", "string"), ("business_dt", "date"))


def test_adding_a_feature_changes_plan_hash_not_contract_hash(contract, two_entries, three_entries):
    a, b = build_group_plan(contract, two_entries), build_group_plan(contract, three_entries)
    assert a.materialization_contract_hash == b.materialization_contract_hash
    assert group_plan_hash(a) != group_plan_hash(b)


def test_colliding_normalized_names_are_a_plan_error(contract, colliding_entries):
    with pytest.raises(GroupPlanError, match="collide"):
        build_group_plan(contract, colliding_entries)


def test_missing_required_column_is_a_completeness_failure(three_feature_plan):
    observed = (("cif_id", "string"), ("business_dt", "date"),
                ("total_debit_amount_30d", "decimal(18,2)"))
    findings = check_completeness(three_feature_plan, observed)
    assert any(f.code == "MISSING_FEATURE_COLUMN" for f in findings)


def test_extra_column_is_also_a_failure(three_feature_plan, full_observed_schema):
    findings = check_completeness(
        three_feature_plan, full_observed_schema + (("surprise", "string"),))
    assert any(f.code == "UNEXPECTED_COLUMN" for f in findings)


def test_wrong_type_is_a_failure(three_feature_plan, full_observed_schema):
    broken = tuple(("total_debit_amount_30d", "string") if c == "total_debit_amount_30d"
                   else (c, t) for c, t in full_observed_schema)
    findings = check_completeness(three_feature_plan, broken)
    assert any(f.code == "WRONG_COLUMN_TYPE" for f in findings)


def test_a_complete_matching_schema_yields_no_findings(three_feature_plan, full_observed_schema):
    assert check_completeness(three_feature_plan, full_observed_schema) == ()
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `group_plan.py`**

`intent_feature_name` comes from the Child-1 authoring intent and is normalized to a valid Hive identifier (`[a-z_][a-z0-9_]*`); a collision after normalization raises `GroupPlanError` — never a silent overwrite. Record both `intent_feature_name` and `formula_content_hash` so Child-2 can attach a governed `feature_id` later without renaming a published column.

- [ ] **Step 4: Run to verify pass** — Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/group_plan.py tests/featuregen/materialize/test_group_plan.py
git commit -m "feat(materialize): FeatureGroupPlanV1 + completeness gate"
```

---

### Task 8: `ComputationPlanV1` — the sharing rules

Spec §E. Computation grouping is **independent** of materialization grouping. Features sharing a source table *and* availability basis share one projection covering the **maximum** window; feature-specific filters stay with the feature.

**Files:**
- Create: `src/featuregen/materialize/compute_plan.py`
- Test: `tests/featuregen/materialize/test_compute_plan.py`

**Interfaces:**
- Consumes: `FormulaExecutionIRV1` (T5), `FeatureGroupPlanV1` (T7).
- Produces: `SharedProjection` (frozen: `projection_id`, `schema`, `table`, `availability_basis`, `columns: tuple[str, ...]`, `max_window_days: int`, `shared_filters: tuple[Mapping, ...]`, `consumer_feature_names: tuple[str, ...]`); `ComputationPlanV1` (frozen: `projections`, `spine_keys`, `per_feature: Mapping[str, str]`); `build_computation_plan(irs: Mapping[str, FormulaExecutionIRV1], plan) -> ComputationPlanV1`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_compute_plan.py
from featuregen.materialize.compute_plan import build_computation_plan


def test_same_table_and_basis_share_one_projection(ir_30d, ir_90d, three_feature_plan):
    cp = build_computation_plan({"total_debit_amount_30d": ir_30d,
                                 "distinct_merchant_count_90d": ir_90d}, three_feature_plan)
    assert len(cp.projections) == 1


def test_shared_projection_keeps_the_MAXIMUM_window(ir_30d, ir_90d, three_feature_plan):
    cp = build_computation_plan({"total_debit_amount_30d": ir_30d,
                                 "distinct_merchant_count_90d": ir_90d}, three_feature_plan)
    assert cp.projections[0].max_window_days == 90   # 90 covers 30


def test_feature_specific_filters_are_NOT_shared(ir_30d, ir_90d, three_feature_plan):
    cp = build_computation_plan({"total_debit_amount_30d": ir_30d,       # debit-only
                                 "distinct_merchant_count_90d": ir_90d}, three_feature_plan)
    assert cp.projections[0].shared_filters == ()   # debit filter belongs to the feature node


def test_a_filter_every_consumer_shares_IS_shared(ir_30d_debit, ir_90d_debit, three_feature_plan):
    cp = build_computation_plan({"a": ir_30d_debit, "b": ir_90d_debit}, three_feature_plan)
    assert len(cp.projections[0].shared_filters) == 1


def test_different_availability_basis_does_not_share(ir_posted, ir_ingested, three_feature_plan):
    cp = build_computation_plan({"a": ir_posted, "b": ir_ingested}, three_feature_plan)
    assert len(cp.projections) == 2


def test_projection_columns_are_the_union_of_read_sets(ir_30d, ir_90d, three_feature_plan):
    cp = build_computation_plan({"a": ir_30d, "b": ir_90d}, three_feature_plan)
    cols = set(cp.projections[0].columns)
    assert {"amount", "transaction_date", "posted_at"} <= cols


def test_every_feature_maps_to_exactly_one_projection(ir_30d, ir_90d, three_feature_plan):
    cp = build_computation_plan({"a": ir_30d, "b": ir_90d}, three_feature_plan)
    assert set(cp.per_feature) == {"a", "b"}
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `compute_plan.py`** — group by `(schema, table, availability_basis)`; `max_window_days` is the max over consumers (converting `WindowUnit` to days); `shared_filters` is the **intersection** of consumers' canonicalized filter trees (empty when they differ).

- [ ] **Step 4: Run to verify pass** — Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/compute_plan.py tests/featuregen/materialize/test_compute_plan.py
git commit -m "feat(materialize): ComputationPlanV1 — shared projections, max window, no shared feature filters"
```

---

### Task 9: Renderer — project scaffold, catalog, parameters, `generated_project_hash`

Spec §F. The first half of the renderer: everything except node bodies. Golden-file tested.

**Files:**
- Create: `src/featuregen/materialize/render/__init__.py`, `src/featuregen/materialize/render/scaffold.py`
- Create: `tests/featuregen/materialize/goldens/` (committed expected files)
- Test: `tests/featuregen/materialize/test_render_scaffold.py`

**Interfaces:**
- Consumes: `ComputationPlanV1` (T8), `FeatureGroupPlanV1` (T7), `MaterializationContractV1` (T6), `MaterializationIdentity`/`derive_namespace` (T1).
- Produces: `RenderedProject` (frozen: `files: Mapping[str, str]`, `generated_project_hash: str`); `render_scaffold(...) -> Mapping[str, str]`; `project_hash(files: Mapping[str, str]) -> str`; `RENDERER_VERSION = 1`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_render_scaffold.py
from pathlib import Path
from featuregen.materialize.render.scaffold import project_hash, render_scaffold

GOLDENS = Path(__file__).parent / "goldens"


def test_catalog_declares_only_read_set_columns(scaffold_args):
    files = render_scaffold(**scaffold_args)
    catalog = files["conf/base/catalog.yml"]
    assert "banking.transactions" in catalog
    assert "unrelated_table" not in catalog


def test_publication_target_is_sandbox_in_the_catalog(scaffold_args):
    files = render_scaffold(**scaffold_args)
    assert "sandbox_feature.cif_daily" in files["conf/base/catalog.yml"]
    assert "\nfeature.cif_daily" not in files["conf/base/catalog.yml"]


def test_storage_locations_are_config_not_hardcoded_in_nodes(scaffold_args):
    files = render_scaffold(**scaffold_args)
    assert "hdfs://" in files["conf/base/catalog.yml"]


def test_generated_json_carries_every_identity(scaffold_args):
    files = render_scaffold(**scaffold_args)
    import json
    ident = json.loads(files["GENERATED.json"])
    for key in ("ir_hashes", "materialization_contract_hash", "group_plan_hash",
                "renderer_version", "publication_target"):
        assert key in ident


def test_project_hash_changes_when_any_file_changes(scaffold_args):
    files = dict(render_scaffold(**scaffold_args))
    before = project_hash(files)
    files["conf/base/parameters.yml"] += "\n# edited\n"
    assert project_hash(files) != before


def test_render_is_deterministic(scaffold_args):
    assert render_scaffold(**scaffold_args) == render_scaffold(**scaffold_args)


def test_scaffold_matches_goldens(scaffold_args):
    files = render_scaffold(**scaffold_args)
    for rel in ("conf/base/catalog.yml", "conf/base/parameters.yml", "GENERATED.json"):
        expected = (GOLDENS / rel).read_text()
        assert files[rel] == expected, f"{rel} drifted from its golden"
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError: ... render.scaffold`

- [ ] **Step 3: Implement `scaffold.py`** and write the goldens

`project_hash(files)` is `materialize_hash({"files": {path: sha256(text) for …}})` over the sorted paths. Every generated file starts with a header comment naming `formula_content_hash`, `ir_hash`, `group_plan_hash` and `RENDERER_VERSION`. The target table name comes **only** from `derive_namespace(identity)` — never a parameter.

Generate the goldens once by running the renderer, then **read them** and confirm by hand that the catalog names only read-set tables and the target is `sandbox_feature.*` before committing.

- [ ] **Step 4: Run to verify pass** — Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/render tests/featuregen/materialize/test_render_scaffold.py tests/featuregen/materialize/goldens
git commit -m "feat(materialize): render project scaffold + generated_project_hash"
```

---

### Task 10: Renderer — PIT projection and spine nodes

Spec §G. **The correctness core.** A look-ahead feature is wrong, not merely unhardened.

**Files:**
- Create: `src/featuregen/materialize/render/nodes_pit.py`
- Test: `tests/featuregen/materialize/test_render_pit.py`

**Interfaces:**
- Consumes: `SharedProjection`, `ComputationPlanV1` (T8); `PitSpec` (T5).
- Produces: `render_pit_projection(projection, *, pit) -> str`; `render_spine(*, key_columns, entity_source) -> str`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_render_pit.py
from featuregen.materialize.render.nodes_pit import render_pit_projection, render_spine


def test_availability_gate_uses_the_governed_column(shared_projection, pit_posted_at):
    src = render_pit_projection(shared_projection, pit=pit_posted_at)
    assert "posted_at" in src
    assert "<=" in src            # rows known AFTER the cutoff must be excluded


def test_event_time_plus_lag_basis_renders_the_lag(shared_projection, pit_lagged):
    src = render_pit_projection(shared_projection, pit=pit_lagged)
    assert "6" in src             # lag_hours from the governed fact


def test_window_inclusivity_is_honoured_exactly(shared_projection, pit_exclusive_start):
    src = render_pit_projection(shared_projection, pit=pit_exclusive_start)
    assert ">" in src and ">=" not in src.split("transaction_date")[1][:20]


def test_projection_selects_only_read_set_columns(shared_projection, pit_posted_at):
    src = render_pit_projection(shared_projection, pit=pit_posted_at)
    assert "select(" in src
    assert "*" not in src         # never SELECT *


def test_spine_produces_one_row_per_key_and_date(spine_args):
    src = render_spine(**spine_args)
    assert "dropDuplicates" in src or "distinct" in src
    assert "business_dt" in src


def test_rendered_source_is_valid_python(shared_projection, pit_posted_at):
    import ast
    ast.parse(render_pit_projection(shared_projection, pit=pit_posted_at))
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `nodes_pit.py`**

The rendered projection must, in this order: read only the read-set columns; filter `availability_column <= business_dt_cutoff` (adding `lag_hours` when `basis == "event_time_plus_lag"`); filter the event-time column to the maximum window with the exact inclusivity from `PitSpec`; apply only `shared_filters`. `ast.parse` on the output is a required test — rendered text that does not parse is the cheapest possible bug to catch.

- [ ] **Step 4: Run to verify pass** — Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/render/nodes_pit.py tests/featuregen/materialize/test_render_pit.py
git commit -m "feat(materialize): render PIT projection + entity-date spine (§G)"
```

---

### Task 11: Renderer — per-feature calc, assembly, validation gates, manifest, hooks

Spec §I, §J, §F. Each feature aggregates independently; assembly LEFT-JOINs onto the spine; the gates block publication.

**Files:**
- Create: `src/featuregen/materialize/render/nodes_calc.py`, `nodes_gate.py`, `hooks.py`
- Test: `tests/featuregen/materialize/test_render_calc.py`, `test_render_gate.py`

**Interfaces:**
- Produces: `render_feature_node(feature, ir, projection_id) -> str`; `render_assemble(plan) -> str`; `render_validate(plan) -> str`; `render_manifest_writer(plan) -> str`; `render_hooks(identity) -> str`.

- [ ] **Step 1: Write the failing calc/assembly tests**

```python
# tests/featuregen/materialize/test_render_calc.py
import ast
from featuregen.materialize.render.nodes_calc import render_assemble, render_feature_node


def test_feature_node_applies_its_OWN_filter(planned_feature, ir_30d):
    src = render_feature_node(planned_feature, ir_30d, projection_id="p0")
    assert "debit" in src            # feature-specific filter lives HERE, not in the projection


def test_feature_node_groups_by_the_grain_keys(planned_feature, ir_30d):
    src = render_feature_node(planned_feature, ir_30d, projection_id="p0")
    assert "groupBy" in src and "cif_id" in src


def test_feature_node_writes_its_own_staging_output(planned_feature, ir_30d):
    src = render_feature_node(planned_feature, ir_30d, projection_id="p0")
    assert "staging" in src          # independent output: a sibling failure cannot corrupt it


def test_assembly_left_joins_onto_the_spine(three_feature_plan):
    src = render_assemble(three_feature_plan)
    assert '"left"' in src or "how=\"left\"" in src


def test_assembly_covers_every_planned_feature(three_feature_plan):
    src = render_assemble(three_feature_plan)
    for f in three_feature_plan.features:
        assert f.column_name in src


def test_rendered_calc_and_assembly_parse(planned_feature, ir_30d, three_feature_plan):
    ast.parse(render_feature_node(planned_feature, ir_30d, projection_id="p0"))
    ast.parse(render_assemble(three_feature_plan))
```

- [ ] **Step 2: Write the failing gate tests**

```python
# tests/featuregen/materialize/test_render_gate.py
import ast
from featuregen.materialize.render.nodes_gate import render_manifest_writer, render_validate

REQUIRED_GATES = ("KEY_NOT_UNIQUE", "MISSING_FEATURE_COLUMN", "UNEXPECTED_COLUMN",
                  "WRONG_COLUMN_TYPE", "INCOMPLETE_COMPUTATION", "SCHEMA_HASH_MISMATCH",
                  "FORBIDDEN_NUMERIC", "PROJECT_INTEGRITY")


def test_every_spec_I_gate_is_rendered(three_feature_plan):
    src = render_validate(three_feature_plan)
    for code in REQUIRED_GATES:
        assert code in src, f"gate {code} missing from rendered validation"


def test_validation_failure_prevents_publication(three_feature_plan):
    src = render_validate(three_feature_plan)
    assert "raise" in src            # a failed gate must stop the pipeline, not warn


def test_manifest_carries_no_data_values(three_feature_plan):
    src = render_manifest_writer(three_feature_plan)
    assert "count" in src
    for leak in ("collect()", "take(", "head("):
        assert leak not in src       # never pull rows into the manifest


def test_rendered_gate_and_manifest_parse(three_feature_plan):
    ast.parse(render_validate(three_feature_plan))
    ast.parse(render_manifest_writer(three_feature_plan))
```

- [ ] **Step 3: Run both to verify failure** — Expected: `ModuleNotFoundError`

- [ ] **Step 4: Implement the three render modules**

`render_hooks` emits `MetricsHook` (per-node wall time and row counts) and `ProvenanceHook` (stamps the §B identity block on every written dataset). Hooks capture metrics only — profiling is Spec B and is not a hook.

- [ ] **Step 5: Run to verify pass** — Expected: PASS (10 passed)

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/materialize/render/nodes_calc.py src/featuregen/materialize/render/nodes_gate.py src/featuregen/materialize/render/hooks.py tests/featuregen/materialize/test_render_calc.py tests/featuregen/materialize/test_render_gate.py
git commit -m "feat(materialize): render feature calc, assembly, §I gates, manifest, hooks"
```

---

### Task 12: `GroupPublisher` — atomic visibility, mechanism PROVEN not assumed

Spec §K. Atomic reader visibility is an **invariant**; the mechanism is metastore-specific and must be demonstrated, not claimed. `INSERT OVERWRITE` is forbidden.

**Files:**
- Create: `src/featuregen/materialize/publish.py`, `src/featuregen/materialize/render/publish.py`
- Test: `tests/featuregen/materialize/test_publish.py`, `tests/featuregen/materialize/test_publish_atomicity.py`

**Interfaces:**
- Produces: `PublishMechanism` (StrEnum: `LOCATION_SWAP`, `POINTER_VIEW`); `PublisherRefused` (Exception); `GroupPublisher` (Protocol: `mechanism`, `render_publish_node(plan, target) -> str`); `select_publisher(mechanism) -> GroupPublisher`; `render_publish(plan, *, target, mechanism) -> str`.

- [ ] **Step 1: Write the failing publisher tests**

```python
# tests/featuregen/materialize/test_publish.py
import pytest
from featuregen.materialize.publish import PublishMechanism, PublisherRefused, select_publisher
from featuregen.materialize.render.publish import render_publish


def test_insert_overwrite_is_refused(three_feature_plan):
    with pytest.raises(PublisherRefused, match="INSERT OVERWRITE"):
        render_publish(three_feature_plan, target="sandbox_feature.cif_daily",
                       mechanism="insert_overwrite")  # type: ignore[arg-type]


def test_rendered_publish_never_emits_insert_overwrite(three_feature_plan):
    src = render_publish(three_feature_plan, target="sandbox_feature.cif_daily",
                         mechanism=PublishMechanism.LOCATION_SWAP)
    assert "INSERT OVERWRITE" not in src.upper()


def test_publish_targets_the_derived_namespace(three_feature_plan):
    src = render_publish(three_feature_plan, target="sandbox_feature.cif_daily",
                         mechanism=PublishMechanism.LOCATION_SWAP)
    assert "sandbox_feature.cif_daily" in src


def test_publish_runs_only_after_validation(three_feature_plan):
    src = render_publish(three_feature_plan, target="sandbox_feature.cif_daily",
                         mechanism=PublishMechanism.LOCATION_SWAP)
    assert "validated" in src        # the node consumes the validation result
```

- [ ] **Step 2: Write the atomicity PROOF test (marked, needs a cluster)**

```python
# tests/featuregen/materialize/test_publish_atomicity.py
"""Spec §K — PROVE the publication mechanism gives atomic reader visibility.

Marked `cluster`: requires a real metastore. This test is the ONLY evidence that the
chosen mechanism satisfies the invariant. If it cannot pass, the design must change —
not the claim.
"""
import pytest

pytestmark = pytest.mark.cluster


def test_concurrent_reader_never_observes_a_partial_partition(hive_cluster, staged_group):
    """A reader polling throughout a swap sees ONLY complete states."""
    observations = hive_cluster.poll_while(
        lambda: hive_cluster.read_schema_and_count("sandbox_feature.cif_daily"),
        during=lambda: hive_cluster.publish(staged_group))
    complete_old = (staged_group.old_schema, staged_group.old_count)
    complete_new = (staged_group.new_schema, staged_group.new_count)
    assert observations, "reader observed nothing — test is vacuous"
    for obs in observations:
        assert obs in (complete_old, complete_new), f"partial state observed: {obs}"
```

Add `cluster` to `[tool.pytest.ini_options] markers` in `pyproject.toml` and to the default `-m` expression so it is deselected by default: `addopts = "--import-mode=importlib -m 'not eval and not cluster'"`.

- [ ] **Step 3: Run to verify failure** — Expected: `ModuleNotFoundError`; the atomicity test deselected by default

- [ ] **Step 4: Implement `publish.py` and `render/publish.py`**

`render_publish` raises `PublisherRefused` for any mechanism not in `PublishMechanism`, and the message must name `INSERT OVERWRITE` when that is what was requested. Document in the module docstring that `LOCATION_SWAP` is **unproven until the marked test passes on the target cluster**.

- [ ] **Step 5: Run to verify pass** — Expected: PASS (4 passed, 1 deselected)

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/materialize/publish.py src/featuregen/materialize/render/publish.py tests/featuregen/materialize/test_publish.py tests/featuregen/materialize/test_publish_atomicity.py pyproject.toml
git commit -m "feat(materialize): GroupPublisher seam + atomicity proof test; reject INSERT OVERWRITE"
```

---

### Task 13: Validation loop — report, findings, classification, L0/L1/L2

Spec §N. Layered so a wrong column name surfaces in seconds. **Classification is the deliverable** — "it failed" routes nowhere.

**Files:**
- Create: `src/featuregen/materialize/validation.py`
- Test: `tests/featuregen/materialize/test_validation.py`

**Interfaces:**
- Produces: `ValidationLevel` (StrEnum `L0`/`L1`/`L2`); `FindingClass` (StrEnum `RENDERER_DEFECT`, `GOVERNED_FACT_MISMATCH`, `ENVIRONMENT_OR_DATA`, `UNCLASSIFIED`); `ValidationFinding` (frozen: `code`, `severity`, `classification`, `location`, `expected`, `observed`, `count`); `ValidationReportV1` (frozen); `run_l0(project: RenderedProject) -> ValidationReportV1`; `run_l1(conn_or_metastore, ir, project) -> ValidationReportV1`; `classify(code, *, expected, observed) -> FindingClass`; `may_regenerate(report) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/materialize/test_validation.py
import pytest
from featuregen.materialize.validation import (
    FindingClass, ValidationLevel, classify, may_regenerate, run_l0,
)


def test_l0_detects_unparseable_rendered_code(broken_render):
    report = run_l0(broken_render)
    assert report.status == "failed"
    assert report.findings[0].classification is FindingClass.RENDERER_DEFECT


def test_l0_passes_a_good_project(good_render):
    assert run_l0(good_render).status == "passed"


def test_l0_detects_a_hand_edited_project(edited_render):
    report = run_l0(edited_render)
    assert any(f.code == "PROJECT_HASH_MISMATCH" for f in report.findings)


def test_type_contradiction_is_a_governed_fact_mismatch():
    # The catalog said decimal; Hive says string. The CODE is right; the CATALOG is wrong.
    assert classify("COLUMN_TYPE_MISMATCH", expected="decimal(18,2)",
                    observed="string") is FindingClass.GOVERNED_FACT_MISMATCH


def test_missing_partition_is_environmental_not_a_renderer_defect():
    assert classify("PARTITION_ABSENT", expected="2026-07-27",
                    observed=None) is FindingClass.ENVIRONMENT_OR_DATA


def test_permission_denied_is_environmental():
    assert classify("READ_DENIED", expected="readable",
                    observed="denied") is FindingClass.ENVIRONMENT_OR_DATA


def test_unknown_code_fails_closed_as_unclassified():
    # MUST NOT default to environmental — that would blame the cluster for our bug.
    assert classify("SOMETHING_NEW", expected=None,
                    observed=None) is FindingClass.UNCLASSIFIED


def test_governed_fact_mismatch_blocks_regeneration(mismatch_report):
    assert may_regenerate(mismatch_report) is False


def test_renderer_defect_permits_regeneration(renderer_defect_report):
    assert may_regenerate(renderer_defect_report) is True


def test_findings_never_carry_data_values(l2_duplicate_key_report):
    # "3 duplicate grain keys" is allowed; the offending cif_id values are NOT.
    for f in l2_duplicate_key_report.findings:
        assert f.count == 3
        blob = f"{f.location}{f.expected}{f.observed}"
        assert "1001" not in blob


def test_report_records_its_level_and_project_hash(good_render):
    report = run_l0(good_render)
    assert report.level is ValidationLevel.L0
    assert report.generated_project_hash == good_render.generated_project_hash
```

- [ ] **Step 2: Run to verify failure** — Expected: `ModuleNotFoundError`

- [ ] **Step 3: Implement `validation.py`**

`run_l0` requires no cluster: `ast.parse` every rendered `.py`, parse the YAML, and re-check `project_hash(files) == generated_project_hash`. `run_l1` reads metastore metadata **only** — never data. `classify` maps a closed code vocabulary to buckets and returns `UNCLASSIFIED` for anything unknown; there is no `else: return ENVIRONMENT_OR_DATA`. `may_regenerate` returns `False` if any finding is `GOVERNED_FACT_MISMATCH` or `UNCLASSIFIED`.

- [ ] **Step 4: Run to verify pass** — Expected: PASS (11 passed)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/materialize/validation.py tests/featuregen/materialize/test_validation.py
git commit -m "feat(materialize): validation loop — L0/L1 + fail-closed finding classification"
```

---

### Task 14: `LocalClusterSubmitter`, the end-to-end pipeline, and the Spark-local test tier

Spec §N submission, §L tier 2. Wires the whole chain and adds the opt-in tier that actually executes generated code.

**Files:**
- Create: `src/featuregen/materialize/submit.py`, `src/featuregen/materialize/pipeline.py`
- Test: `tests/featuregen/materialize/test_submit.py`, `tests/featuregen/materialize/test_spark_local.py`

**Interfaces:**
- Produces: `PipelineSubmitter` (Protocol: `submit(project, *, level, environment_id) -> ValidationReportV1`); `LocalClusterSubmitter`; `SubmissionError`; `generate_group(conn, *, formulas, cadence, availability_class, overrides=None) -> GenerationResult` (frozen: `project: RenderedProject`, `identity`, `plan`, `contract`, `generation_id`).

- [ ] **Step 1: Write the failing submitter tests**

```python
# tests/featuregen/materialize/test_submit.py
import pytest
from featuregen.materialize.submit import LocalClusterSubmitter, SubmissionError
from featuregen.materialize.validation import ValidationLevel


def test_l2_is_not_run_unless_explicitly_requested(good_render, fake_runner):
    sub = LocalClusterSubmitter(runner=fake_runner)
    sub.submit(good_render, level=ValidationLevel.L1, environment_id="hdfc-local")
    assert fake_runner.spark_jobs == []      # L1 reads metadata only — no Spark job


def test_l2_on_demand_submits_a_spark_job(good_render, fake_runner):
    sub = LocalClusterSubmitter(runner=fake_runner)
    sub.submit(good_render, level=ValidationLevel.L2, environment_id="hdfc-local")
    assert len(fake_runner.spark_jobs) == 1


def test_unreachable_cluster_is_error_not_invented_findings(good_render, dead_runner):
    sub = LocalClusterSubmitter(runner=dead_runner)
    report = sub.submit(good_render, level=ValidationLevel.L1, environment_id="hdfc-local")
    assert report.status == "error" and report.findings == ()


def test_validation_results_do_not_carry_across_project_hashes(good_render, other_render, fake_runner):
    sub = LocalClusterSubmitter(runner=fake_runner)
    a = sub.submit(good_render, level=ValidationLevel.L0, environment_id="e")
    b = sub.submit(other_render, level=ValidationLevel.L0, environment_id="e")
    assert a.generated_project_hash != b.generated_project_hash
```

- [ ] **Step 2: Write the Spark-local execution tier (marked)**

```python
# tests/featuregen/materialize/test_spark_local.py
"""Spec §L tier 2 — actually EXECUTE the generated pipeline on tiny hand-authored fixtures.

Marked `spark`: needs pyspark locally, deselected by default. This is the only tier that
proves the generated code computes the right number.
"""
import pytest

pytestmark = pytest.mark.spark


def test_total_debit_amount_30d_matches_a_hand_computed_value(spark_local, tiny_fixture):
    out = spark_local.run_generated(tiny_fixture, business_dt="2026-07-27")
    assert out.value("1001", "total_debit_amount_30d") == 5500


def test_a_row_posted_after_the_cutoff_is_excluded(spark_local, look_ahead_fixture):
    """A txn dated 2026-07-01 but posted 2026-07-05 must NOT affect business_dt=2026-07-03."""
    early = spark_local.run_generated(look_ahead_fixture, business_dt="2026-07-03")
    later = spark_local.run_generated(look_ahead_fixture, business_dt="2026-07-06")
    assert early.value("1001", "total_debit_amount_30d") == 0
    assert later.value("1001", "total_debit_amount_30d") == 250


def test_exactly_one_row_per_grain_and_date(spark_local, tiny_fixture):
    out = spark_local.run_generated(tiny_fixture, business_dt="2026-07-27")
    assert out.row_count() == out.distinct_key_count()


def test_an_entity_with_no_transactions_still_appears(spark_local, tiny_fixture):
    out = spark_local.run_generated(tiny_fixture, business_dt="2026-07-27")
    assert out.has_key("1099")     # spine guarantees presence; value is null/zero per policy


def test_a_duplicate_key_group_is_rejected_by_the_gate(spark_local, duplicate_key_fixture):
    with pytest.raises(Exception, match="KEY_NOT_UNIQUE"):
        spark_local.run_generated(duplicate_key_fixture, business_dt="2026-07-27")
```

Add `spark` to the markers and the default deselect: `-m 'not eval and not cluster and not spark'`.

- [ ] **Step 3: Run to verify failure** — Expected: `ModuleNotFoundError: ... materialize.submit`; marked tests deselected

- [ ] **Step 4: Implement `submit.py` and `pipeline.py`**

`generate_group` runs the full chain (T3→T9/T10/T11/T12), writes one `materialization_generation` row (T2), and returns the `RenderedProject` plus identity. `LocalClusterSubmitter` takes an injectable `runner` (so the unit tests need no cluster) and shells out to `spark-submit` in production use; a submission that cannot reach the cluster returns `status="error"` with **zero** findings — never invented ones.

- [ ] **Step 5: Run to verify pass** — Expected: PASS (4 passed, marked tiers deselected)

- [ ] **Step 6: Run the whole package plus a regression sweep**

```bash
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/materialize -p no:cacheprovider -q
PYTHONPATH=src .venv/bin/python -m pytest tests/featuregen/formula tests/featuregen/db -p no:cacheprovider -q
```
Expected: the materialize package green; formula + db unchanged from their pre-task counts.

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/materialize/submit.py src/featuregen/materialize/pipeline.py tests/featuregen/materialize/test_submit.py tests/featuregen/materialize/test_spark_local.py pyproject.toml
git commit -m "feat(materialize): LocalClusterSubmitter + end-to-end generation + Spark-local tier"
```

---

## Self-Review

**Spec coverage.** §0 prerequisite/sandbox → T1 (namespace derivation) + T9 (catalog target). §A1 → T3. §A2 → T5. §B → T1. §C → T6. §D → T7. §E → T8. §F → T9/T10/T11. §G → T10 + T14 look-ahead test. §H → T4. §I → T11 + T14 gate test. §J → T2 + T11. §K → T12. §L → T9 goldens (tier 1), T14 (tier 2), T12 atomicity + manual run (tier 3). §M → the fail-closed paths in T4/T5/T6/T12/T13. §N → T13 + T14.

**Known gap, deliberate:** the spec's control-plane *ingest* of `ValidationReportV1`/`RunManifestV1` into the T2 tables is exercised only through `generate_group`'s generation row. If you want reports and manifests persisted and queryable, add it to T13/T14 rather than assuming it.

**Placeholder scan.** No "TBD"/"handle errors appropriately"/"write tests for the above". Two tasks (T5, T7) show excerpts rather than whole modules — their signatures, hash payloads and every test are complete, and the excerpt boundary is stated.

**Type consistency.** `expr_path` uses Child-1's `body.expr`/`body.numerator`/… vocabulary in T3, T5 and T8. `materialize_hash` (T1) is the only hasher. `generated_project_hash` is produced by `project_hash` (T9) and consumed by T13's integrity check. `ValidationReportV1` is produced in T13 and returned by `PipelineSubmitter.submit` in T14. `FindingClass.UNCLASSIFIED` is the fail-closed default in T13 and blocks regeneration via `may_regenerate`.

**Fixture warning for implementers.** T3's `fixtures.py` is written from `src/featuregen/formula/schema.py` as it exists on main. Check the real field names before trusting the fixture, and fix the fixture, not the schema.
