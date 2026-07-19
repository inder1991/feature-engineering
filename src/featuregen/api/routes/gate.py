"""Phase-3C.1 — the authority-only gate-operationalization endpoints. Platform-admin only, OFF the
customer path, read-only: the body carries only a batch identifier; every count/verdict is assembled
server-side from the persisted WORM stores (never the request body)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.contract.live_activation import record_decision, record_evaluation
from featuregen.overlay.upload.planner.gate_operate import (
    run_double_compile,
    run_drift_checks,
    run_gold_suite,
    select_window,
)
from featuregen.overlay.upload.planner.shadow_report import (
    EVALUATOR_VERSION,
    build_population_report,
    evaluate_machine_gate,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class EvaluateIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


class EnablementEvalIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


class ActivationDecisionIn(BaseModel):
    evaluation_id: str
    decision: str
    reason: str = ""
    supersedes_decision_id: str | None = None


def _run_machine_gate(conn: psycopg.Connection, cohort: str, since: datetime, until: datetime):
    """The 3C.1 machine-gate harness, shared by `/gate/evaluate` and `/gate/enablement-evaluation`
    so the persisted evaluation can never diverge from what the read-only endpoint reports."""
    window = select_window(conn, cohort=cohort, since=since, until=until)
    report = build_population_report(conn, window.run_ids)
    gold = run_gold_suite(conn)
    stability = run_double_compile(conn)
    drift = run_drift_checks(conn)
    verdict = evaluate_machine_gate(report=report, gold_report=gold, stability=stability,
                                    drift_ratio=drift)
    return window, report, gold, stability, drift, verdict


@router.post("/gate/evaluate", dependencies=[Depends(require_confirmer)])
def evaluate(body: EvaluateIn, conn: _Conn) -> dict:
    # The frontend sends date-only strings, which Pydantic coerces to NAIVE midnight; a naive
    # timestamp compared against timestamptz is cast in the PG session timezone (deployment-
    # dependent). Pin naive edges to UTC so the window is reproducible everywhere.
    since = body.since if body.since.tzinfo else body.since.replace(tzinfo=UTC)
    until = body.until if body.until.tzinfo else body.until.replace(tzinfo=UTC)
    window, report, gold, stability, drift, verdict = _run_machine_gate(
        conn, body.cohort, since, until)
    return {
        "verdict": {"passed": verdict.passed, "gate1_capture": verdict.gate1_capture,
                    "gate2a_map": verdict.gate2a_map, "gate3_gold": verdict.gate3_gold,
                    "gate5_stability": verdict.gate5_stability, "gate6_drift": verdict.gate6_drift},
        "reasons": list(verdict.reasons),
        "necessary_not_sufficient": True,
        "coverage": {"dispatched_in_range": window.coverage.dispatched_in_range,
                     "qualifying": window.coverage.qualifying, "excluded": window.coverage.excluded},
        "population": {"denominator": report.denominator, "numerator": report.numerator,
                       "headline_by_primary": report.headline_by_primary,
                       "breakdown_by_category": report.breakdown_by_category,
                       "recipe_outcome_matrix": report.recipe_outcome_matrix},
        "versions": {"evaluator": EVALUATOR_VERSION, "cohort": body.cohort},
    }


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


@router.get("/gate/cohorts", dependencies=[Depends(require_confirmer)])
def cohorts(conn: _Conn) -> list[dict]:
    rows = conn.execute(
        "SELECT producer_commit, min(created_at), max(created_at), count(*)"
        " FROM planner_shadow_dispatch WHERE producer_commit <> 'unset'"
        " GROUP BY producer_commit ORDER BY max(created_at) DESC").fetchall()
    return [{"cohort": c, "first_run_at": lo.isoformat(), "last_run_at": hi.isoformat(),
             "run_count": n} for c, lo, hi, n in rows]
