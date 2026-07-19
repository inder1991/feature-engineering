# Phase 3C.2a — Live Governed Cross-Catalog Flip Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Behind a deployment-scoped, activation-gated flag, make the governed deterministic cross-catalog planner the ONLY source of customer-visible cross-catalog features — surfacing resolved governed recipes, rejecting unresolved ones and cross-catalog LLM ideas, drafting exclusively from a server-persisted governed plan envelope, and never invoking the permissive `find_cross_catalog_path`.

**Architecture:** One env flag (`FEATUREGEN_INTENT_LIVE_CROSS_CATALOG`) gated by a durable activation interlock (two append-only records binding an APPROVE decision to a persisted PASS evaluation + a code-only version vector, keyed by `FEATUREGEN_DEPLOYMENT_ID`). Flag-on, the governed planner runs in `build_considered_set`'s entity-scoped branch; each governed option carries structured provenance + a persisted plan envelope; draft/confirm rebind to the envelope and recheck freshness via `ReplayFreshness`. Flag-off is byte-identical.

**Tech Stack:** Python 3.11, FastAPI, psycopg 3, Postgres (one append-only migration). Reuses `plan_bindings`/`BindingPlanningResultV1`/`PlannerReplayEnvelopeV1`, `replay.ReplayFreshness`, the 3C.1 `gate_operate`/`shadow_report` harness, `require_confirmer`.

## Global Constraints

- **The invariant:** in an activation-enabled deployment, EVERY customer-visible cross-catalog feature has a governed physical plan. No two governance standards coexist.
- **Behaviour-neutral flag-off:** with `FEATUREGEN_INTENT_LIVE_CROSS_CATALOG` unset, the considered-set/draft/confirm responses are BYTE-IDENTICAL to today; no new query, no readiness check, no dispatch change. The flag is read ONLY at the route boundary (the builder/planner stay pure).
- **Fail-closed:** flag-on but not activation-approved (missing / revoked / superseded / version-vector mismatch) → a readiness error BEFORE any LLM or planner dispatch; NEVER a legacy fallback. Drift → regenerate (never substitute). Missing/tampered plan identity → reject. A governed rejection NEVER falls back to `find_cross_catalog_path`.
- **Authority is a structured field, not a lens:** keep the semantic `lens`; enforce authority from `origin`/`path_authority`/`physical_plan_id` + the persisted envelope, never the lens name.
- **Version vector = CODE versions only.** The graph/catalog fingerprint is EXCLUDED (catalog churn is a per-plan `ReplayFreshness` concern). No signing / no `authority_sign_gate` dependency.
- **F4 preserved:** the cross-catalog result is a contract DEFINITION, never an attested cross-catalog `approved_join`.
- **WORM:** the two activation tables are append-only (migration additive; REVOKE UPDATE/DELETE mirroring 0971). Contracts `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum`. Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Next free migration number is **1002** (max on the branch is 1001). 27 pre-existing `passc/` ruff errors are unrelated — ignore; only changed files must be clean.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `src/featuregen/db/migrations/1002_live_activation.sql` | `enablement_evaluation` + `live_activation_decision` (append-only) | 1 |
| `src/featuregen/overlay/upload/contract/live_activation.py` (create) | version vector, deployment id, the enabled predicate, readiness error, store readers/writers | 2 |
| `src/featuregen/api/routes/gate.py` (modify) | authority-only: persist an evaluation + record a decision | 3 |
| `src/featuregen/overlay/upload/planner/plan_envelope.py` (create) | `PlanEnvelopeV1` + `plan_envelope_from_result` + `recheck_plan_freshness` | 4 |
| `src/featuregen/overlay/upload/contract/author.py` (modify) | `FeatureIdea` gains envelope/provenance; draft rebinds to the envelope | 4, 6 |
| `src/featuregen/overlay/upload/contract/gate1.py` (modify) | serialize/deserialize envelope on the snapshot; governed lens in the entity branch | 4, 5 |
| `src/featuregen/api/routes/contract.py` (modify) | readiness gate on the scoped route; freshness recheck at confirm | 5, 6 |
| `tests/...` | per-task tests + the acceptance suite + the `find_cross_catalog_path`-raises test | all, 7 |

---

## Task 1: Activation tables (migration 1002)

**Files:**
- Create: `src/featuregen/db/migrations/1002_live_activation.sql`
- Test: `tests/featuregen/db/test_migration_1002.py`

**Interfaces:**
- Produces: tables `enablement_evaluation(evaluation_id text PK, telemetry_window jsonb, population_report jsonb, gold_set_result jsonb, stability_result jsonb, layer_b_labels jsonb NULL, version_vector jsonb, result text CHECK IN ('PASS','FAIL'), content_hash text, evaluated_at timestamptz)` and `live_activation_decision(decision_id text PK, evaluation_id text FK, deployment_id text, decision text CHECK IN ('APPROVE','REVOKE'), decided_by text, reason text, decided_at timestamptz, supersedes_decision_id text NULL)`.

- [ ] **Step 1: Write the failing migration test**

`tests/featuregen/db/test_migration_1002.py`:

```python
from __future__ import annotations


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)).fetchall()}


def test_1002_creates_both_activation_tables(db):
    assert {"evaluation_id", "telemetry_window", "population_report", "gold_set_result",
            "stability_result", "layer_b_labels", "version_vector", "result", "content_hash",
            "evaluated_at"} <= _cols(db, "enablement_evaluation")
    assert {"decision_id", "evaluation_id", "deployment_id", "decision", "decided_by", "reason",
            "decided_at", "supersedes_decision_id"} <= _cols(db, "live_activation_decision")


def test_1002_result_and_decision_checks(db):
    db.execute("INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
               " gold_set_result, stability_result, version_vector, result, content_hash) VALUES"
               " ('e1','{}','{}','{}','{}','{}','PASS','h')")
    import pytest
    with pytest.raises(Exception):
        db.execute("INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
                   " gold_set_result, stability_result, version_vector, result, content_hash) VALUES"
                   " ('e2','{}','{}','{}','{}','{}','MAYBE','h')")   # bad result enum


def test_1002_layer_b_labels_nullable(db):
    row = db.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name="
                     "'enablement_evaluation' AND column_name='layer_b_labels'").fetchone()
    assert row[0] == "YES"
```

- [ ] **Step 2: Run it, verify it fails**

Run: `uv run pytest tests/featuregen/db/test_migration_1002.py -q` → FAIL (tables absent).

- [ ] **Step 3: Write the migration**

`src/featuregen/db/migrations/1002_live_activation.sql`:

```sql
-- src/featuregen/db/migrations/1002_live_activation.sql
-- Phase 3C.2a live-activation interlock (append-only, no signing). enablement_evaluation is a
-- persisted, content-hashed run of the 3C.1 machine gate (server-assembled from trusted sources).
-- live_activation_decision is the human APPROVE/REVOKE bound to one evaluation + this deployment.
-- WORM: both are write-once (INSERT only); UPDATE/DELETE/TRUNCATE revoked from featuregen_app
-- (mirror 0971). Approval is permitted only over a result='PASS' evaluation (enforced in code).
CREATE TABLE IF NOT EXISTS enablement_evaluation (
    evaluation_id     text        PRIMARY KEY,
    telemetry_window  jsonb       NOT NULL,
    population_report jsonb       NOT NULL,
    gold_set_result   jsonb       NOT NULL,
    stability_result  jsonb       NOT NULL,
    layer_b_labels    jsonb       NULL,
    version_vector    jsonb       NOT NULL,
    result            text        NOT NULL CHECK (result IN ('PASS', 'FAIL')),
    content_hash      text        NOT NULL,
    evaluated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS live_activation_decision (
    decision_id            text        PRIMARY KEY,
    evaluation_id          text        NOT NULL REFERENCES enablement_evaluation(evaluation_id),
    deployment_id          text        NOT NULL,
    decision               text        NOT NULL CHECK (decision IN ('APPROVE', 'REVOKE')),
    decided_by             text        NOT NULL,
    reason                 text        NOT NULL DEFAULT '',
    decided_at             timestamptz NOT NULL DEFAULT now(),
    supersedes_decision_id text        NULL REFERENCES live_activation_decision(decision_id)
);
CREATE INDEX IF NOT EXISTS live_activation_by_deployment
    ON live_activation_decision (deployment_id, decided_at DESC);

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'featuregen_app') THEN
        REVOKE UPDATE, DELETE, TRUNCATE ON enablement_evaluation FROM featuregen_app;
        REVOKE UPDATE, DELETE, TRUNCATE ON live_activation_decision FROM featuregen_app;
    END IF;
END $$;
```

- [ ] **Step 4: Run the test, verify it passes**

Run: `uv run pytest tests/featuregen/db/test_migration_1002.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(3c2a): activation tables — enablement_evaluation + live_activation_decision (task 1)"
```

---

## Task 2: `live_activation.py` — version vector, deployment id, the enabled predicate

**Files:**
- Create: `src/featuregen/overlay/upload/contract/live_activation.py`
- Test: `tests/featuregen/overlay/upload/contract/test_live_activation.py`

**Interfaces:**
- Consumes: the Task-1 tables; the version constants (`PLANNER_VERSION`, `PLAN_CONTRACT_VERSION`, `PHYSICAL_PLAN_VERSION`, `AGGREGATION_RULE_VERSION`, `ADDITIVITY_RULE_VERSION`, `TEMPORAL_RULE_VERSION`, `SAFETY_EVALUATOR_VERSION`, `REASON_CODE_REGISTRY_VERSION`, `RECIPE_REGISTRY_VERSION` from `planner.contracts`; `EVALUATOR_VERSION` from `planner.shadow_report`; `GOLD_SET_VERSION` from `planner.contract_gold`).
- Produces: `LiveActivationNotReady(RuntimeError)`; `deployment_id() -> str`; `current_version_vector() -> dict[str, str]`; `record_evaluation(conn, *, telemetry_window, population_report, gold_set_result, stability_result, result, evaluated_at) -> str` (evaluation_id); `record_decision(conn, *, evaluation_id, decision, decided_by, reason, decided_at, supersedes_decision_id=None) -> str`; `is_live_cross_catalog_enabled(conn) -> bool`; `require_live_ready(conn) -> None` (raises `LiveActivationNotReady` unless enabled).

- [ ] **Step 1: Write the failing tests**

`tests/featuregen/overlay/upload/contract/test_live_activation.py`:

```python
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.contract.live_activation import (
    LiveActivationNotReady,
    current_version_vector,
    is_live_cross_catalog_enabled,
    record_decision,
    record_evaluation,
    require_live_ready,
)

_NOW = datetime(2026, 7, 18, tzinfo=UTC)


def _approved(db, *, result="PASS", vv=None):
    eid = record_evaluation(db, telemetry_window={"cohort": "c"}, population_report={},
                            gold_set_result={}, stability_result={}, result=result, evaluated_at=_NOW)
    if vv is not None:   # force a stored vector that differs from current, for the mismatch test
        db.execute("UPDATE enablement_evaluation SET version_vector = %s WHERE evaluation_id = %s",
                   (__import__("json").dumps(vv), eid))
    return record_decision(db, evaluation_id=eid, decision="APPROVE", decided_by="admin",
                           reason="go", decided_at=_NOW)


def test_flag_off_is_disabled(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", raising=False)
    _approved(db)
    assert is_live_cross_catalog_enabled(db) is False
    with pytest.raises(LiveActivationNotReady):
        require_live_ready(db)


def test_flag_on_without_approval_is_disabled(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    assert is_live_cross_catalog_enabled(db) is False   # no decision at all


def test_flag_on_with_matching_pass_approval_is_enabled(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db)
    assert is_live_cross_catalog_enabled(db) is True
    require_live_ready(db)   # no raise


def test_approval_over_a_fail_is_rejected(db, monkeypatch):
    with pytest.raises(ValueError):
        _approved(db, result="FAIL")   # record_decision APPROVE over a FAIL evaluation → refused


def test_version_vector_mismatch_disables(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db, vv={"planner": "0.0.0-stale"})   # stored vector != current
    assert is_live_cross_catalog_enabled(db) is False


def test_revoke_supersedes_approval(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    eid = record_evaluation(db, telemetry_window={}, population_report={}, gold_set_result={},
                            stability_result={}, result="PASS", evaluated_at=_NOW)
    dec = record_decision(db, evaluation_id=eid, decision="APPROVE", decided_by="a", reason="", decided_at=_NOW)
    assert is_live_cross_catalog_enabled(db) is True
    record_decision(db, evaluation_id=eid, decision="REVOKE", decided_by="a", reason="stop",
                    decided_at=_NOW, supersedes_decision_id=dec)
    assert is_live_cross_catalog_enabled(db) is False


def test_wrong_deployment_does_not_inherit_approval(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    _approved(db)                                 # decision recorded for d1
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d2")
    assert is_live_cross_catalog_enabled(db) is False


def test_version_vector_is_code_only_no_graph_fingerprint():
    vv = current_version_vector()
    assert "planner" in vv and "gate_evaluator" in vv and "gold_set" in vv
    assert not any("fingerprint" in k or "graph" in k for k in vv)   # graph/catalog state excluded
```

- [ ] **Step 2: Run them, verify they fail**

Run: `uv run pytest tests/featuregen/overlay/upload/contract/test_live_activation.py -q` → FAIL (module absent).

- [ ] **Step 3: Implement `live_activation.py`**

`src/featuregen/overlay/upload/contract/live_activation.py`:

```python
"""Phase-3C.2a — the deployment-scoped live-activation interlock (no signing). The flag is necessary
but not sufficient: live governed cross-catalog is enabled only when an APPROVE decision (latest,
non-revoked) for THIS deployment references a persisted PASS evaluation whose CODE version vector still
matches the current one. Catalog/graph state is deliberately NOT in the vector (per-plan ReplayFreshness
handles churn). Read only at the route boundary."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any

from featuregen.overlay.upload.planner.contract_gold import GOLD_SET_VERSION
from featuregen.overlay.upload.planner.contracts import (
    ADDITIVITY_RULE_VERSION,
    AGGREGATION_RULE_VERSION,
    PHYSICAL_PLAN_VERSION,
    PLAN_CONTRACT_VERSION,
    PLANNER_VERSION,
    REASON_CODE_REGISTRY_VERSION,
    RECIPE_REGISTRY_VERSION,
    SAFETY_EVALUATOR_VERSION,
    TEMPORAL_RULE_VERSION,
)
from featuregen.overlay.upload.planner.shadow_report import EVALUATOR_VERSION


class LiveActivationNotReady(RuntimeError):
    """Flag on but no matching non-revoked PASS approval for this deployment + version vector."""


def deployment_id() -> str:
    return os.environ.get("FEATUREGEN_DEPLOYMENT_ID", "unset")


def _flag_on() -> bool:
    return os.environ.get("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "0") == "1"


def current_version_vector() -> dict[str, str]:
    """CODE versions only — a change to any invalidates a prior approval. No graph/catalog fingerprint."""
    return {
        "planner": PLANNER_VERSION, "plan_contract": PLAN_CONTRACT_VERSION,
        "physical_plan": PHYSICAL_PLAN_VERSION, "aggregation_rule": AGGREGATION_RULE_VERSION,
        "additivity_rule": ADDITIVITY_RULE_VERSION, "temporal_rule": TEMPORAL_RULE_VERSION,
        "safety_evaluator": SAFETY_EVALUATOR_VERSION, "reason_code_registry": REASON_CODE_REGISTRY_VERSION,
        "recipe_registry": RECIPE_REGISTRY_VERSION, "gate_evaluator": EVALUATOR_VERSION,
        "gold_set": GOLD_SET_VERSION,
    }


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def record_evaluation(conn, *, telemetry_window: dict, population_report: dict, gold_set_result: dict,
                      stability_result: dict, result: str, evaluated_at: datetime) -> str:
    """Persist a content-hashed evaluation (server-assembled; the version vector is the CURRENT one)."""
    if result not in ("PASS", "FAIL"):
        raise ValueError(f"result must be PASS|FAIL, got {result!r}")
    vv = current_version_vector()
    eid = f"eval_{uuid.uuid4().hex[:16]}"
    material = {"telemetry_window": telemetry_window, "population_report": population_report,
                "gold_set_result": gold_set_result, "stability_result": stability_result,
                "version_vector": vv, "result": result}
    content_hash = hashlib.sha256(_canonical(material).encode()).hexdigest()
    conn.execute(
        "INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
        " gold_set_result, stability_result, version_vector, result, content_hash, evaluated_at)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (eid, _canonical(telemetry_window), _canonical(population_report), _canonical(gold_set_result),
         _canonical(stability_result), _canonical(vv), result, content_hash, evaluated_at))
    return eid


def record_decision(conn, *, evaluation_id: str, decision: str, decided_by: str, reason: str,
                    decided_at: datetime, supersedes_decision_id: str | None = None) -> str:
    """Record an APPROVE/REVOKE. APPROVE is permitted ONLY over a persisted result='PASS' evaluation."""
    if decision not in ("APPROVE", "REVOKE"):
        raise ValueError(f"decision must be APPROVE|REVOKE, got {decision!r}")
    row = conn.execute("SELECT result FROM enablement_evaluation WHERE evaluation_id = %s",
                       (evaluation_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown evaluation_id {evaluation_id!r}")
    if decision == "APPROVE" and row[0] != "PASS":
        raise ValueError("APPROVE is only permitted over a PASS evaluation")
    did = f"dec_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO live_activation_decision (decision_id, evaluation_id, deployment_id, decision,"
        " decided_by, reason, decided_at, supersedes_decision_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (did, evaluation_id, deployment_id(), decision, decided_by, reason, decided_at,
         supersedes_decision_id))
    return did


def is_live_cross_catalog_enabled(conn) -> bool:
    """flag ∧ (latest decision for this deployment is APPROVE) ∧ (its evaluation is PASS) ∧ (that
    evaluation's stored version vector == the current one). Fail-closed on anything else."""
    if not _flag_on():
        return False
    row = conn.execute(
        "SELECT d.decision, e.result, e.version_vector FROM live_activation_decision d"
        " JOIN enablement_evaluation e ON e.evaluation_id = d.evaluation_id"
        " WHERE d.deployment_id = %s ORDER BY d.decided_at DESC, d.decision_id DESC LIMIT 1",
        (deployment_id(),)).fetchone()
    if row is None:
        return False
    decision, result, stored_vv = row
    return (decision == "APPROVE" and result == "PASS" and stored_vv == current_version_vector())


def require_live_ready(conn) -> None:
    if not is_live_cross_catalog_enabled(conn):
        raise LiveActivationNotReady(
            "live cross-catalog is flagged on but not activation-approved for this deployment/version "
            "(missing / revoked / superseded / version-vector mismatch)")
```

Note: `stored_vv` comes back from psycopg as a Python dict (jsonb) — compare directly to `current_version_vector()`.

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/contract/test_live_activation.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

Run: `uv run ruff check src/featuregen/overlay/upload/contract/live_activation.py && uv run mypy src/featuregen/overlay/upload/contract/live_activation.py`

```bash
git add -A && git commit -m "feat(3c2a): live-activation interlock — version vector + enabled predicate (task 2)"
```

---

## Task 3: Authority endpoints — persist evaluation + record decision

**Files:**
- Modify: `src/featuregen/api/routes/gate.py`
- Test: `tests/featuregen/api/test_gate_routes.py` (extend)

**Interfaces:**
- Consumes: `live_activation.record_evaluation`/`record_decision`; the 3C.1 harness (`select_window`, `run_gold_suite`, `run_double_compile`, `run_drift_checks`, `build_population_report`, `evaluate_machine_gate`).
- Produces: `POST /gate/enablement-evaluation` (body `{cohort, since, until}`) → runs the harness, persists an `enablement_evaluation`, returns `{evaluation_id, result, ...}`; `POST /gate/activation-decision` (body `{evaluation_id, decision, reason, supersedes_decision_id?}`) → records a decision, returns `{decision_id}`. Both `require_confirmer`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/featuregen/api/test_gate_routes.py`:

```python
def test_persist_evaluation_and_approve_enables(client, admin_headers, db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_PRODUCER_COMMIT", "sha-a")
    monkeypatch.setenv("FEATUREGEN_INTENT_LIVE_CROSS_CATALOG", "1")
    monkeypatch.setenv("FEATUREGEN_DEPLOYMENT_ID", "d1")
    # persist an evaluation over an empty window → result FAIL (fail-closed), and confirm APPROVE is refused
    ev = client.post("/gate/enablement-evaluation", json={"cohort": "ghost",
                     "since": "2026-07-18T00:00:00Z", "until": "2026-07-19T00:00:00Z"}, headers=admin_headers)
    assert ev.status_code == 200 and ev.json()["result"] == "FAIL"
    bad = client.post("/gate/activation-decision", json={"evaluation_id": ev.json()["evaluation_id"],
                      "decision": "APPROVE", "reason": "x"}, headers=admin_headers)
    assert bad.status_code == 422   # APPROVE over a FAIL is refused server-side


def test_activation_endpoints_require_platform_admin(client, non_admin_headers):
    assert client.post("/gate/enablement-evaluation", json={"cohort": "c", "since":
        "2026-07-18T00:00:00Z", "until": "2026-07-19T00:00:00Z"}, headers=non_admin_headers).status_code == 403
    assert client.post("/gate/activation-decision", json={"evaluation_id": "e", "decision": "REVOKE",
                       "reason": "x"}, headers=non_admin_headers).status_code == 403
```

- [ ] **Step 2: Run them, verify they fail** (`404`/`405` — routes absent).

- [ ] **Step 3: Implement the endpoints** in `src/featuregen/api/routes/gate.py`

Add the request models + routes (reuse the existing `_evaluate` machine-harness body from Task-5 3C.1 by factoring it into a helper `_run_machine_gate(conn, cohort, since, until)` that returns `(report, gold, stability, drift, verdict, coverage)`; call it from both `/gate/evaluate` and the new endpoint):

```python
from datetime import UTC, datetime

from featuregen.overlay.upload.contract.live_activation import record_decision, record_evaluation


class EnablementEvalIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


class ActivationDecisionIn(BaseModel):
    evaluation_id: str
    decision: str
    reason: str = ""
    supersedes_decision_id: str | None = None


@router.post("/gate/enablement-evaluation", dependencies=[Depends(require_confirmer)])
def persist_evaluation(body: EnablementEvalIn, conn: _Conn) -> dict:
    since = body.since if body.since.tzinfo else body.since.replace(tzinfo=UTC)
    until = body.until if body.until.tzinfo else body.until.replace(tzinfo=UTC)
    window, report, gold, stability, drift, verdict = _run_machine_gate(conn, body.cohort, since, until)
    result = "PASS" if verdict.passed else "FAIL"
    evaluation_id = record_evaluation(
        conn, telemetry_window={"cohort": body.cohort, "since": since.isoformat(),
                                "until": until.isoformat(), "coverage": {
                                    "dispatched_in_range": window.coverage.dispatched_in_range,
                                    "qualifying": window.coverage.qualifying,
                                    "excluded": window.coverage.excluded}},
        population_report={"denominator": report.denominator, "numerator": report.numerator,
                           "headline_by_primary": report.headline_by_primary,
                           "breakdown_by_category": report.breakdown_by_category,
                           "recipe_outcome_matrix": report.recipe_outcome_matrix},
        gold_set_result={"passed": gold.passed, "false_resolves": list(gold.false_resolves)},
        stability_result={"stable": stability.stable, "compared": stability.compared},
        result=result, evaluated_at=datetime.now(UTC))
    return {"evaluation_id": evaluation_id, "result": result, "reasons": list(verdict.reasons)}


@router.post("/gate/activation-decision", dependencies=[Depends(require_confirmer)])
def activation_decision(body: ActivationDecisionIn, conn: _Conn, identity: _Identity) -> dict:
    try:
        did = record_decision(conn, evaluation_id=body.evaluation_id, decision=body.decision,
                              decided_by=identity.subject, reason=body.reason,
                              decided_at=datetime.now(UTC),
                              supersedes_decision_id=body.supersedes_decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"decision_id": did}
```

`_Identity` and `HTTPException` are already imported in the 3C.1 gate route module; add `from fastapi import HTTPException` if absent, and `_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]` (mirror `governance.py`).

- [ ] **Step 4: Run the tests, verify they pass**

Run: `uv run pytest tests/featuregen/api/test_gate_routes.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

Run: `uv run ruff check src/featuregen/api/routes/gate.py && uv run mypy src/featuregen/api/routes/gate.py`

```bash
git add -A && git commit -m "feat(3c2a): authority endpoints — persist evaluation + activation decision (task 3)"
```

---

## Task 4: The plan envelope + carry-forward on the snapshot

**Files:**
- Create: `src/featuregen/overlay/upload/planner/plan_envelope.py`
- Modify: `src/featuregen/overlay/upload/contract/author.py` (`FeatureIdea` +3 fields), `src/featuregen/overlay/upload/contract/gate1.py` (`_idea_json`/`_idea_from_json`)
- Test: `tests/featuregen/overlay/upload/planner/test_plan_envelope.py`

**Interfaces:**
- Consumes: `BindingPlanningResultV1`, `BindingPlanV1` (`physical_plan_id`, `contract_id`, `contract_resolution_status`, `contract_reason_codes`, `participating_catalogs`, `path_segments`, `audit_envelope`), `replay.StoredEvidenceV1`/`read_current_evidence`/`compare`/`ReplayFreshness`, `fingerprint._VERSIONS`.
- Produces: `PlanEnvelopeV1` (frozen dataclass with the 11 spec fields) + `to_json()`/`from_json(d)`; `plan_envelope_from_result(result: BindingPlanningResultV1) -> PlanEnvelopeV1 | None`; `recheck_plan_freshness(conn, envelope: PlanEnvelopeV1, roles=()) -> ReplayFreshness`. `FeatureIdea` gains `plan_envelope: PlanEnvelopeV1 | None = None`, `origin: str = "llm"`, `path_authority: str = "single_or_llm"`.

- [ ] **Step 1: Write the failing test**

`tests/featuregen/overlay/upload/planner/test_plan_envelope.py`:

```python
from __future__ import annotations

from featuregen.overlay.upload.planner.contracts import ContractResolutionStatus, ReplayStrength
from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1


def _env():
    return PlanEnvelopeV1(
        recipe_id="r", physical_plan_id="pplan_1", generation_run_id="run", catalog_sources=("a", "b"),
        ordered_path=("a.t1->b.t2",), contract_id="c1",
        contract_resolution_status="resolved", contract_reason_codes=(),
        catalog_fingerprint={"a": "fpa", "b": "fpb"}, compiler_version={"plan_contract": "1.0.0"},
        input_stamps=({"catalog_source": "a", "compiler_input_fingerprint": "fpa", "head_seq": 3,
                       "projection_checkpoint": 5},))


def test_envelope_json_roundtrips():
    e = _env()
    assert PlanEnvelopeV1.from_json(e.to_json()) == e


def test_from_json_is_total_and_frozen():
    e = _env()
    import dataclasses
    assert dataclasses.is_dataclass(e) and getattr(type(e), "__slots__", None) is not None
```

(Add a DB test `test_plan_envelope_from_result_and_freshness` that seeds a cross-catalog fixture — reuse `test_shadow_capture._cross_seed` + `_txn_template` — runs `plan_bindings` with compile on, projects the envelope, and asserts a re-read is `ReplayFreshness.current` while a graph rebuild makes it `drifted`.)

- [ ] **Step 2: Run it, verify it fails** (module absent).

- [ ] **Step 3: Implement `plan_envelope.py`**

```python
"""Phase-3C.2a — the governed plan envelope: the server-persisted carry-forward that binds a chosen
considered-set option to its exact governed physical plan, so drafting never recomputes a permissive
path. Freshness is rechecked per-plan via ReplayFreshness (catalog churn is NOT an activation concern)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    CatalogStateStampV1,
    PLAN_CONTRACT_VERSION,
    ReplayFreshness,
)
from featuregen.overlay.upload.planner.fingerprint import _VERSIONS
from featuregen.overlay.upload.planner.replay import (
    StoredEvidenceV1,
    compare,
    read_current_evidence,
)


@dataclass(frozen=True, slots=True)
class PlanEnvelopeV1:
    recipe_id: str
    physical_plan_id: str
    generation_run_id: str | None
    catalog_sources: tuple[str, ...]
    ordered_path: tuple[str, ...]
    contract_id: str | None
    contract_resolution_status: str
    contract_reason_codes: tuple[str, ...]
    catalog_fingerprint: dict[str, str]
    compiler_version: dict[str, str]
    input_stamps: tuple[dict[str, Any], ...]   # serialized CatalogStateStampV1 set (the freshness source)

    def to_json(self) -> dict:
        return {
            "recipe_id": self.recipe_id, "physical_plan_id": self.physical_plan_id,
            "generation_run_id": self.generation_run_id, "catalog_sources": list(self.catalog_sources),
            "ordered_path": list(self.ordered_path), "contract_id": self.contract_id,
            "contract_resolution_status": self.contract_resolution_status,
            "contract_reason_codes": list(self.contract_reason_codes),
            "catalog_fingerprint": dict(self.catalog_fingerprint),
            "compiler_version": dict(self.compiler_version),
            "input_stamps": [dict(s) for s in self.input_stamps]}

    @staticmethod
    def from_json(d: dict) -> "PlanEnvelopeV1":
        return PlanEnvelopeV1(
            recipe_id=d["recipe_id"], physical_plan_id=d["physical_plan_id"],
            generation_run_id=d.get("generation_run_id"),
            catalog_sources=tuple(d.get("catalog_sources", [])),
            ordered_path=tuple(d.get("ordered_path", [])), contract_id=d.get("contract_id"),
            contract_resolution_status=d["contract_resolution_status"],
            contract_reason_codes=tuple(d.get("contract_reason_codes", [])),
            catalog_fingerprint=dict(d.get("catalog_fingerprint", {})),
            compiler_version=dict(d.get("compiler_version", {})),
            input_stamps=tuple(dict(s) for s in d.get("input_stamps", [])))


def _ordered_path(plan) -> tuple[str, ...]:
    return tuple(f"{seg.catalog_source}:{seg.segment_kind}:{seg.realization_ref or seg.bridge_fact_key or ''}"
                for seg in plan.path_segments)


def plan_envelope_from_result(result: BindingPlanningResultV1) -> PlanEnvelopeV1 | None:
    """Project the SELECTED governed contract plan into an envelope. None when the run has no selected
    contract plan (nothing governed to carry)."""
    pid = result.selected_contract_physical_plan_id
    if pid is None:
        return None
    plan = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
    if plan is None:
        return None
    stamps = plan.audit_envelope.catalog_state_stamps if plan.audit_envelope is not None else ()
    return PlanEnvelopeV1(
        recipe_id=result.recipe_id, physical_plan_id=plan.physical_plan_id,
        generation_run_id=result.run_id, catalog_sources=tuple(plan.participating_catalogs),
        ordered_path=_ordered_path(plan), contract_id=plan.contract_id,
        contract_resolution_status=str(plan.contract_resolution_status),
        contract_reason_codes=tuple(str(c) for c in plan.contract_reason_codes),
        catalog_fingerprint={s.catalog_source: s.compiler_input_fingerprint for s in stamps},
        compiler_version={"plan_contract": PLAN_CONTRACT_VERSION},
        input_stamps=tuple({"catalog_source": s.catalog_source,
                            "compiler_input_fingerprint": s.compiler_input_fingerprint,
                            "head_seq": s.head_seq, "projection_checkpoint": s.projection_checkpoint}
                           for s in stamps))


def recheck_plan_freshness(conn, envelope: PlanEnvelopeV1, roles=()) -> ReplayFreshness:
    """Compare the envelope's pinned per-catalog stamps to the CURRENT catalog state. Anything but
    `current` (drifted / incompatible / unverifiable) means the plan must be regenerated, not substituted."""
    stamps = tuple(
        CatalogStateStampV1(catalog_source=s["catalog_source"], head_seq=int(s["head_seq"]),
                            resolved_at="", compiler_input_fingerprint=s["compiler_input_fingerprint"],
                            projection_checkpoint=int(s.get("projection_checkpoint", 0)))
        for s in envelope.input_stamps)
    stored = StoredEvidenceV1.from_stamps(stamps, _VERSIONS)
    return compare(stored, read_current_evidence(conn, stored, roles))
```

(Verify the exact `CatalogStateStampV1` constructor signature against `contracts.py` and adjust the field names if they differ — the plan's Task-4 implementer must read `CatalogStateStampV1` before writing `recheck_plan_freshness`.)

- [ ] **Step 4: Extend `FeatureIdea` + snapshot (de)serialization**

In `contract/author.py`, add to `FeatureIdea` (after `derives_pairs`): `plan_envelope: PlanEnvelopeV1 | None = None`, `origin: str = "llm"`, `path_authority: str = "single_or_llm"` (import `PlanEnvelopeV1`). In `gate1.py::_idea_json`, add `"origin": f.origin`, `"path_authority": f.path_authority`, and `"plan_envelope": f.plan_envelope.to_json() if f.plan_envelope else None`. In `_idea_from_json`, restore them: `origin=d.get("origin", "llm")`, `path_authority=d.get("path_authority", "single_or_llm")`, `plan_envelope=PlanEnvelopeV1.from_json(d["plan_envelope"]) if d.get("plan_envelope") else None`.

- [ ] **Step 5: Run tests, verify they pass; gates; commit**

Run: `uv run pytest tests/featuregen/overlay/upload/planner/test_plan_envelope.py tests/featuregen/overlay/upload/contract -q` + ruff + mypy on the changed files.

```bash
git add -A && git commit -m "feat(3c2a): plan envelope + snapshot carry-forward + FeatureIdea provenance (task 4)"
```

---

## Task 5: Governed lens in `build_considered_set` (flag-on entity branch)

**Files:**
- Modify: `src/featuregen/overlay/upload/contract/gate1.py` (entity-scoped branch), `src/featuregen/api/routes/contract.py` (readiness gate)
- Test: `tests/featuregen/overlay/upload/contract/test_gate1_governed_lens.py`, `tests/featuregen/api/test_contract_live_cross_catalog.py`

**Interfaces:**
- Consumes: `live_activation.is_live_cross_catalog_enabled`/`require_live_ready`; `plan_bindings` (via a per-run scope + eligible recipe set); `plan_envelope_from_result`; `PlanEnvelopeV1`.
- Produces: on a flag-on-enabled entity-scoped run, `build_considered_set` returns a `FeatureSet` of governed options (each `FeatureIdea` with `origin="governed_planner"`, `path_authority="governed_cross_catalog"`, a `plan_envelope`) + rejections for unresolved plans and for cross-catalog LLM features (`GOVERNED_CROSS_CATALOG_PLAN_REQUIRED`). Reason-string constant `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED = "governed_cross_catalog_plan_required"`.

- [ ] **Step 1: Write the failing tests** — cover: (a) flag-off entity-scoped run is byte-identical (no governed options, no readiness query); (b) flag-on-enabled → resolved governed plans appear as options with `origin="governed_planner"` + `path_authority="governed_cross_catalog"` + a non-None `plan_envelope`; (c) unresolved governed plans appear in `rejections`; (d) an LLM feature whose `derives_pairs` span >1 catalog appears in `rejections` with `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED` and NOT in any surfaced `FeatureSet`; (e) a single-catalog LLM feature is unchanged; (f) flag-on-but-not-approved → the route raises the readiness error (`LiveActivationNotReady` → HTTP 503) before any dispatch. (Write concrete assertions mirroring the existing `test_contract_*` route tests; use the `admin`/identity fixtures and seed a cross-catalog fixture via `test_shadow_capture._cross_seed`.)

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.**
  - In `contract/gate1.py`, add a helper `_governed_cross_catalog_lens(conn, intent, applicability, roles, now) -> tuple[list[FeatureIdea], list[dict]]` that: resolves the run scope, calls `plan_bindings` for each eligible recipe, and for each result: if it has a selected resolved contract plan → build a `FeatureIdea` (name from the recipe/template, `derives_pairs` from the plan's read-set, `origin="governed_planner"`, `path_authority="governed_cross_catalog"`, `plan_envelope=plan_envelope_from_result(result)`); else → a rejection dict `{lens:"governed", reason: <primary reason code>, recipe_id}`. Then, in the **entity-scoped branch** (`catalog_source is None`), when live is enabled, (i) append the governed `FeatureSet(lens="templates", features=governed_ideas)` [authority carried on the ideas, NOT the lens], (ii) extend `rejections` with the governed rejections, and (iii) filter the LLM `alternatives`: any feature whose `derives_pairs` span >1 catalog_source is REMOVED from its FeatureSet and added to `rejections` with `GOVERNED_CROSS_CATALOG_PLAN_REQUIRED`.
  - In `contract.py::_scoped_considered_set`, BEFORE building the considered set, if `body.catalog_source is None and _flag_on()`: call `require_live_ready(conn)`; on `LiveActivationNotReady` return HTTP 503 (readiness error) BEFORE any LLM/planner dispatch. Pass an `is_live` boolean into `build_considered_set` so the builder runs the governed lens only when enabled. (`_flag_on` is read only here — the builder receives the resolved boolean.)

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Gates + commit** (`feat(3c2a): governed cross-catalog lens + LLM cross-catalog rejection + readiness gate (task 5)`).

---

## Task 6: Draft/confirm rebinding + freshness recheck

**Files:**
- Modify: `src/featuregen/overlay/upload/contract/author.py` (`draft_contract`, `_join_path`), `src/featuregen/api/routes/contract.py` (draft + confirm rechecks)
- Test: `tests/featuregen/overlay/upload/contract/test_draft_rebinding.py`

**Interfaces:**
- Consumes: `FeatureIdea.plan_envelope`; `recheck_plan_freshness`; `ReplayFreshness`.
- Produces: `draft_contract` uses `feature.plan_envelope.ordered_path` when present (never `find_cross_catalog_path`); a stale-plan signal (`StalePlan` exception or a typed result) when freshness ≠ current.

- [ ] **Step 1: Write the failing tests** — (a) a chosen governed feature (with envelope) drafts a join path EXACTLY equal to `envelope.ordered_path`, and `_join_path`/`find_cross_catalog_path` is not consulted; (b) if `recheck_plan_freshness` returns `drifted`, the draft raises/returns a stale-plan result requiring regeneration (no substitute path); (c) a cross-catalog feature (derives_pairs span >1 catalog) with NO envelope is rejected at draft (fail-closed), even if it entered the snapshot; (d) single-catalog features draft exactly as before.

- [ ] **Step 2: Run, verify fail.**

- [ ] **Step 3: Implement.**
  - In `author.py::draft_contract`: if `feature.plan_envelope is not None`, set the draft's join path from `feature.plan_envelope.ordered_path` (as the same `steps` dict shape `_join_path` returns) and DO NOT call `_join_path`. Add a freshness recheck: `fresh = recheck_plan_freshness(conn, feature.plan_envelope, roles)`; if `fresh is not ReplayFreshness.current` → raise `StalePlan` (new typed exception) so the route returns a regenerate-required result. If `feature.plan_envelope is None` but the feature spans >1 catalog → raise a fail-closed error (a cross-catalog feature without a governed envelope must never draft a permissive path when a governed feature could).
  - In `contract.py`: the `/contract/draft` route catches `StalePlan` → HTTP 409 "plan stale, regenerate". The `/contract/confirm` route re-runs the freshness recheck against the drafted contract's envelope before `confirm_contract`; a stale plan at confirm → 409, never a silent finalize.

- [ ] **Step 4: Run, verify pass.**

- [ ] **Step 5: Gates + commit** (`feat(3c2a): draft/confirm rebind to the governed envelope + freshness recheck (task 6)`).

---

## Task 7: Both-boundary enforcement + the `find_cross_catalog_path`-raises test + acceptance suite

**Files:**
- Test: `tests/featuregen/api/test_contract_live_cross_catalog.py` (extend), `tests/featuregen/overlay/upload/contract/test_no_permissive_path_when_live.py`
- Modify: `.env.example`

**Interfaces:** consumes the full chain from Tasks 1-6.

- [ ] **Step 1: The `find_cross_catalog_path`-raises structural test.** Monkeypatch `contract.author.find_cross_catalog_path` (and `entity.find_cross_catalog_path`) with a function that RAISES, enable live activation, and drive the full flag-on cross-catalog flow: considered-set (entity-scoped) → draft (governed feature) → confirm. Assert each path either SUCCEEDS or FAILS CLOSED (readiness/stale/rejected) WITHOUT the raising function ever being invoked. This is the structural guarantee that no flag-on cross-catalog path touches the permissive implementation.

```python
def test_find_cross_catalog_path_never_invoked_when_live(client, admin_headers, db, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("find_cross_catalog_path must never run while live cross-catalog is on")
    monkeypatch.setattr("featuregen.overlay.upload.contract.author.find_cross_catalog_path", _boom)
    # ... enable activation (persist PASS eval + APPROVE), seed a cross-catalog catalog set,
    #     run considered-set → draft → confirm; assert no AssertionError propagates from _boom.
```

- [ ] **Step 2: The spec §9 acceptance suite** — one test per §9 item (1 flag-off byte-identical; 2 resolved governed surface with `path_authority`; 3 unresolved→rejections; 4 cross-catalog LLM can't reach drafting; 5 draft path == envelope; 6 drift→regenerate; 7 missing/tampered identity fails closed; 8 the raises-test above; 9 activation prerequisite: readiness error / APPROVE-over-FAIL refused / wrong-deployment no inherit). Reuse the fixtures from Tasks 3/5/6.

- [ ] **Step 3: Document env vars** in `.env.example`:

```
# 3C.2a live governed cross-catalog (the first customer-facing flip). NECESSARY BUT NOT SUFFICIENT:
# also requires a matching non-revoked PASS activation decision for this deployment + version vector.
# FEATUREGEN_INTENT_LIVE_CROSS_CATALOG=1
# A stable per-deployment identity; activation approval is keyed by deployment_id + version vector so a
# copied env / shared DB cannot inherit another deployment's approval.
# FEATUREGEN_DEPLOYMENT_ID=production-eu
```

- [ ] **Step 4: Full behaviour-neutral verification.** `uv run pytest tests/featuregen/ tests/db/ -q` (all pass / 1 pre-existing skip — the flag-off considered-set/draft/confirm responses are byte-identical). `uv run ruff check` + `uv run mypy` on all changed src. Frontend unaffected (no UI in 3C.2a; `path_authority` rides the existing response for a later UI task).

- [ ] **Step 5: Commit** (`test(3c2a): acceptance suite + no-permissive-path-when-live guarantee + behaviour-neutral (task 7)`).

---

## Notes for the executor

- **Read before writing:** Task 4 must read `CatalogStateStampV1`'s real constructor + `BindingPlanV1`'s `path_segments`/`audit_envelope`/`participating_catalogs` fields; Task 5 must read `build_considered_set`'s entity branch (`gate1.py:220`) + how `plan_bindings` is invoked in `run_shadow_planner` (scope resolution + per-recipe loop) to reuse that wiring; Task 6 must read `draft_contract`/`_join_path` (`author.py`) + the `/contract/draft` + `/contract/confirm` routes.
- **Behaviour-neutrality is the tripwire:** if any flag-off `test_contract_*` response changes, STOP — the governed lens + readiness gate must be strictly behind `is_live` (resolved at the route) and the builder must not query activation when the flag is off.
- **Fail-closed is the invariant to protect:** every cross-catalog path with the flag on must reach a governed plan or an explicit closed outcome (readiness / rejection / stale). A test that lets a cross-catalog feature draft without a governed envelope is a bug.
- **Model split** ([[prefers-opus-for-subagents]]): Fable implementers/fixers, Opus reviews; set the model explicitly per dispatch.
```
