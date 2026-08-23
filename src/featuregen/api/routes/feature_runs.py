"""Read-only run spine routes (spec §5/§11/§12). The spine DERIVES; it never stores lifecycle.

404 covers both absence and denial, deliberately: a distinguishable 403 would confirm the run id
exists — a shape leak the read policy exists to prevent.

Both routes are pure reads over the stores that already hold the evidence, so there is no write
endpoint here and nothing in the foundation can change a run's lifecycle through this surface.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from featuregen.api.deps import get_conn, get_identity, require_feature_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import list_runs, run_detail

router = APIRouter(dependencies=[Depends(require_feature_read)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/feature-runs")
def feature_runs_list(conn: _Conn, identity: _Identity,
                      limit: int = 25, cursor: str | None = None) -> dict:
    """One page of the runs this caller may see, grouped by intent WITHIN the page.

    `archived` is neither filtered nor surfaced in the foundation: nothing can set it, because no
    write endpoint exists to.

    The groups carry the intent's RAW hypothesis, because the reader is either its owner or an
    admin — the redacted variants exist for LLM egress, not for owner display.

    Both caller-supplied query values are the route's to police, not the projection's. `limit` is
    CLAMPED rather than refused (0 would page zero rows and then index the empty page; an
    unbounded value would read the table), and a malformed `cursor` is a 422 because it is spliced
    into a `::timestamptz` cast that raises out of the driver — a caller's typo is never a server
    error."""
    try:
        return list_runs(conn, identity, limit=min(max(limit, 1), 100), cursor=cursor)
    except psycopg.DataError as exc:
        # The failed cast aborts the request transaction; `get_conn` rolls it back on the way out.
        # With no cursor there is no caller-supplied value in that query to blame, so a data error
        # is the server's own and must stay a 500 rather than be mislabelled as bad input.
        if cursor is None:
            raise
        raise HTTPException(status_code=422, detail="cursor is not a valid page cursor") from exc


@router.get("/feature-runs/{run_id}")
def feature_run_detail(run_id: str, conn: _Conn, identity: _Identity) -> dict:
    """One run's milestones, authoring axes and honest rail — or 404 for absent AND invisible.

    The projection returns None for both, and the two are mapped to the SAME body here: telling
    them apart would let a probe enumerate other people's run ids."""
    detail = run_detail(conn, identity, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return detail
