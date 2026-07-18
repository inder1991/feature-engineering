"""Phase-3C.1 — the authority-only gate-operationalization endpoints. Platform-admin only, OFF the
customer path, read-only: the body carries only a batch identifier; every count/verdict is assembled
server-side from the persisted WORM stores (never the request body)."""
from __future__ import annotations

from datetime import datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel

from featuregen.api.deps import get_conn, require_confirmer
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


class EvaluateIn(BaseModel):
    cohort: str
    since: datetime
    until: datetime


@router.post("/gate/evaluate", dependencies=[Depends(require_confirmer)])
def evaluate(body: EvaluateIn, conn: _Conn) -> dict:
    window = select_window(conn, cohort=body.cohort, since=body.since, until=body.until)
    report = build_population_report(conn, window.run_ids)
    gold = run_gold_suite(conn)
    stability = run_double_compile(conn)
    drift = run_drift_checks(conn)
    verdict = evaluate_machine_gate(report=report, gold_report=gold, stability=stability,
                                    drift_ratio=drift)
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


@router.get("/gate/cohorts", dependencies=[Depends(require_confirmer)])
def cohorts(conn: _Conn) -> list[dict]:
    rows = conn.execute(
        "SELECT producer_commit, min(created_at), max(created_at), count(*)"
        " FROM planner_shadow_dispatch WHERE producer_commit <> 'unset'"
        " GROUP BY producer_commit ORDER BY max(created_at) DESC").fetchall()
    return [{"cohort": c, "first_run_at": lo.isoformat(), "last_run_at": hi.isoformat(),
             "run_count": n} for c, lo, hi, n in rows]
