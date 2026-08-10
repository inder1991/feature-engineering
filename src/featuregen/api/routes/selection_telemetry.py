"""TASK 7 phase 1 — the selection-telemetry report: what humans actually keep, by origin.

``GET /contracts/selection-telemetry`` serializes
:func:`overlay.upload.contract.selection_telemetry.selection_report` unchanged: selection rate per
candidate identity (the feature NAME carries the parameterisation) and the two-engine totals
(recipe vs llm_freeform vs user_defined). Read-only, ``feature:read``-gated like the other
``/contracts`` reads. Empty catalogs and zero rounds return the honest zero report, never a 404 —
the metric existing at zero is the point.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, require_feature_read
from featuregen.overlay.upload.contract.selection_telemetry import selection_report

router = APIRouter()


@router.get("/contracts/selection-telemetry", dependencies=[Depends(require_feature_read)])
def contracts_selection_telemetry(
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
) -> dict:
    report = selection_report(conn)
    return {
        "rounds": report.rounds,
        "by_origin": report.by_origin,
        "rows": [
            {
                "generation_source": row.generation_source,
                "recipe_id": row.recipe_id,
                "feature_name": row.feature_name,
                "offered": row.offered,
                "chosen": row.chosen,
                "use_cases": list(row.use_cases),
            }
            for row in report.rows
        ],
    }
