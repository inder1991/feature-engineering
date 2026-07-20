"""Phase-3C.2a — the deployment-scoped live-activation interlock (no signing). The flag is necessary
but not sufficient: live governed cross-catalog is enabled only when an APPROVE decision (latest,
non-revoked) for THIS deployment references a persisted PASS evaluation whose CODE version vector still
matches the current one. Catalog/graph state is deliberately NOT in the vector (per-plan ReplayFreshness
handles churn). Read only at the route boundary."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from featuregen.config import get_settings
from featuregen.overlay.upload.planner.cause import CATEGORY_MAP_VERSION
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
from featuregen.overlay.upload.planner.strata import STRATA_VERSION

logger = logging.getLogger(__name__)

# H1c — the umbrella rejection reason. A candidate whose SELECTED inputs span more than one
# catalog_source may be governed ONLY while cross-catalog grounding is genuinely enabled (governed plan
# envelope + this interlock + a valid signed 3C gate artifact). Any doubt → refuse with this reason.
CROSS_CATALOG_GROUNDING_NOT_ENABLED = "CROSS_CATALOG_GROUNDING_NOT_ENABLED"

# F2 — the ISO-8601 expiry keys the signed 3C artifact may carry (either name); enforced if present.
_ARTIFACT_EXPIRY_KEYS = ("expires_at", "valid_until")


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
    else: explicitly superseded decisions are excluded outright (supersession only counts WITHIN
    the same deployment — a foreign deployment's row can never neutralize this one's REVOKE), an
    unconfigured deployment id never honors an approval, and a decided_at tie resolves
    REVOKE-first so an ambiguous ordering can never resurrect a revoked approval."""
    if not _flag_on():
        return False
    dep = deployment_id()
    if not dep or dep == "unset":   # unconfigured deployments must not share 'unset' approvals
        return False
    row = conn.execute(
        "SELECT d.decision, e.result, e.version_vector FROM live_activation_decision d"
        " JOIN enablement_evaluation e ON e.evaluation_id = d.evaluation_id"
        " WHERE d.deployment_id = %s"
        " AND NOT EXISTS (SELECT 1 FROM live_activation_decision s"
        "                 WHERE s.supersedes_decision_id = d.decision_id"
        "                 AND s.deployment_id = d.deployment_id)"
        " ORDER BY d.decided_at DESC, (d.decision = 'REVOKE') DESC, d.decision_id DESC LIMIT 1",
        (dep,)).fetchone()
    if row is None:
        return False
    decision, result, stored_vv = row
    return (decision == "APPROVE" and result == "PASS" and stored_vv == current_version_vector())


def require_live_ready(conn) -> None:
    if not is_live_cross_catalog_enabled(conn):
        raise LiveActivationNotReady(
            "live cross-catalog is flagged on but not activation-approved for this deployment/version "
            "(missing / revoked / superseded / version-vector mismatch)")


def _expected_gate_versions() -> dict[str, str]:
    """The CURRENT running-code versions the signed 3C artifact must match — the gate evaluator, the gold
    set, the Layer-A category map, and the strata definition. A signature-valid but STALE artifact
    (produced under superseded gate logic) fails closed on any mismatch (F2)."""
    return {"evaluator_version": EVALUATOR_VERSION, "gold_set_version": GOLD_SET_VERSION,
            "category_map_version": CATEGORY_MAP_VERSION, "strata_version": STRATA_VERSION}


def _artifact_content_enforced(artifact_path: str) -> bool:
    """F2 — after the detached signature verifies, ENFORCE the signed CONTENT (fail-closed on any doubt):
    the gate must have PASSED (``gate_passed is True``); the gate/gold-set/category-map/strata versions
    must match the CURRENT running code (a stale artifact is refused); and any embedded expiry
    (``expires_at``/``valid_until``, ISO-8601) must be in the future. Signature validity ALONE is not
    sufficient — a signed-but-failed / signed-but-stale / signed-but-expired artifact is REJECTED."""
    try:
        material = json.loads(Path(artifact_path).read_bytes())
    except (OSError, ValueError) as exc:
        logger.warning("3C gate artifact %s unreadable/malformed after signature verify (%s) — failing "
                       "closed", artifact_path, exc)
        return False
    if material.get("gate_passed") is not True:
        logger.warning("3C gate artifact %s did not PASS (gate_passed=%r) — refusing cross-catalog",
                       artifact_path, material.get("gate_passed"))
        return False
    for field_name, want in _expected_gate_versions().items():
        got = material.get(field_name)
        if got != want:
            logger.warning("3C gate artifact %s version mismatch: %s=%r, expected %r — failing closed",
                           artifact_path, field_name, got, want)
            return False
    raw_expiry = next((material[k] for k in _ARTIFACT_EXPIRY_KEYS if material.get(k) is not None), None)
    if raw_expiry is not None:
        try:
            expires = datetime.fromisoformat(str(raw_expiry))
        except ValueError:
            logger.warning("3C gate artifact %s has an unparseable expiry %r — failing closed",
                           artifact_path, raw_expiry)
            return False
        if datetime.now(expires.tzinfo) >= expires:
            logger.warning("3C gate artifact %s expired at %s — failing closed", artifact_path, raw_expiry)
            return False
    return True


def signed_gate_artifact_valid() -> bool:
    """H1c THIRD prong — a VALID + ENFORCED signed 3C enablement-gate artifact. The detached ed25519
    signature is verified with ``planner.signing.verify_report_file`` (the artifact's canonical bytes at
    ``FEATUREGEN_INTENT_GATE_ARTIFACT`` must carry a ``.sig`` sidecar verifying against the trusted public
    key ``FEATUREGEN_INTENT_GATE_PUBLIC_KEY``); then (F2) the signed CONTENT is ENFORCED via
    :func:`_artifact_content_enforced` — signature validity ALONE is NOT sufficient: ``gate_passed`` must
    be true, the gate/gold-set/category-map/strata versions must match the running code, and any embedded
    expiry must be in the future. Fail-CLOSED whenever the gate is DEPLOYED: a configured public key with a
    missing / tampered / wrong-key / unreadable / FAILED / STALE / EXPIRED artifact → False.

    DEPLOYMENT POSTURE (explicit + logged): enforcement is GATED ON A PUBLIC KEY BEING CONFIGURED. When NO
    public key is configured this deployment has not opted into signed-gate enforcement, so the prong is
    INERT (returns True — the durable live-activation interlock alone gates), keeping the flag-off path
    byte-identical and the current production posture unchanged. When a key IS configured the artifact is
    fully enforced. The chosen posture is LOGGED on every call so the inert path is never silent. Reuses
    the existing verifier; it is NEVER rebuilt here."""
    if not get_settings().intent_gate_public_key:
        logger.info("3C signed-gate enforcement NOT deployed (no FEATUREGEN_INTENT_GATE_PUBLIC_KEY) — "
                    "prong inert; the live-activation interlock alone gates cross-catalog grounding")
        return True   # signed-gate enforcement not deployed → prong inert (activation interlock governs)
    artifact_path = os.environ.get("FEATUREGEN_INTENT_GATE_ARTIFACT")
    if not artifact_path:
        logger.warning("3C signed-gate enforcement IS deployed (public key configured) but no artifact "
                       "path (FEATUREGEN_INTENT_GATE_ARTIFACT) is set — failing closed")
        return False  # a trusted key IS configured but there is no artifact to verify → fail closed
    from featuregen.overlay.upload.planner.signing import verify_report_file
    if not verify_report_file(artifact_path):   # ALL signature failure modes → False (fail-closed)
        logger.warning("3C gate artifact %s failed signature verification — failing closed", artifact_path)
        return False
    return _artifact_content_enforced(artifact_path)   # F2 — enforce gate_passed / versions / expiry


def cross_catalog_grounding_enabled(conn) -> bool:
    """H1c — the FULL runtime cross-catalog-grounding interlock the governing write consults for a
    MULTI-catalog contract: the durable live-activation interlock (flag + persisted PASS enablement +
    APPROVE decision + version vector — :func:`is_live_cross_catalog_enabled`) AND a valid signed 3C gate
    artifact (:func:`signed_gate_artifact_valid`). BOTH must hold; fail closed on any doubt. The candidate
    ALSO carrying a governed ``plan_envelope`` is asserted at the call site — this answers only "is live
    cross-catalog grounding enabled for THIS deployment right now"."""
    return is_live_cross_catalog_enabled(conn) and signed_gate_artifact_valid()
