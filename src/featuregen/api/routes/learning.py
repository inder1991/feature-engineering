"""Read-only learning-gap route — the consumer the recorder never had.

`GET /learning/gaps` exposes :func:`open_gaps`: the unresolved things an analysis could not proceed
without, most-blocking first, each with the subjects it concerns, the action a human would take, and
— where the answer is a closed vocabulary — the options being chosen between.

Release 3 required recording learning evidence rather than deferring it to Release 5, "otherwise the
first working question's evidence is discarded". The recording half was built and wired; `open_gaps`
had no caller outside tests, so a refused analysis stored an actionable gap somewhere no human would
ever look. Storing evidence nobody can read discards it just as effectively as never recording it.

**Pure read.** The queue is DERIVED — a gap is open while no resolution event references it — so
there is nothing here to mutate and no state to keep in step. Resolving a gap is a separate,
authority-bearing action that does not belong on a read route; :func:`resolve_gap` exists and is
still unwired, deliberately, because who may decide a business policy is a governance question, not
a routing one.

**Gated on ``catalog:read``.** The queue says which decision blocks which question — a work backlog,
not a privileged secret. ``governance:confirm`` is held by ``platform_admin`` alone
(``identity/permissions.py``), so gating on it would hide the backlog from the feature engineers and
data owners who would act on it.

**Not read-scoped, and why that is stated rather than assumed.** Subjects here are PHYSICAL addresses
minted by the data-agent path (``catalog::database::schema::table.column``), not catalog
``object_ref`` values, so the catalog's sensitivity read-scoping seam does not apply to them
unchanged. They are structural addresses and carry no data values. Mapping them onto the catalog's
scoped universe is a real piece of work and is left undone visibly, rather than papered over with a
filter that looks like scoping and is not.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.data_agent.learning import open_gaps

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


@router.get("/learning/gaps", dependencies=[Depends(require_catalog_read)])
def learning_gaps(conn: _Conn) -> dict:
    """Open ontology gaps, ordered by how many distinct questions each one blocks.

    That ordering is the prioritisation signal the release asked for, and the reason gap identity is
    ``(code, subject_refs)`` with the request deliberately excluded: two questions waiting on one
    undecided relationship is ONE thing to decide with TWO callers waiting, which is a stronger case
    for deciding it than two unrelated gaps.
    """
    return {"gaps": [asdict(gap) for gap in open_gaps(conn)]}
