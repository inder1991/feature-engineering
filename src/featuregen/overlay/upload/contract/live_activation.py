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
    """flag ∧ (latest non-superseded decision for this deployment is APPROVE) ∧ (its evaluation is
    PASS) ∧ (that evaluation's stored version vector == the current one). Fail-closed on anything
    else: explicitly superseded decisions are excluded outright, and a decided_at tie resolves
    REVOKE-first so an ambiguous ordering can never resurrect a revoked approval."""
    if not _flag_on():
        return False
    row = conn.execute(
        "SELECT d.decision, e.result, e.version_vector FROM live_activation_decision d"
        " JOIN enablement_evaluation e ON e.evaluation_id = d.evaluation_id"
        " WHERE d.deployment_id = %s"
        " AND NOT EXISTS (SELECT 1 FROM live_activation_decision s"
        "                 WHERE s.supersedes_decision_id = d.decision_id)"
        " ORDER BY d.decided_at DESC, (d.decision = 'REVOKE') DESC, d.decision_id DESC LIMIT 1",
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
