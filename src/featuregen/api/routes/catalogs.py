"""``GET /catalogs`` — the catalog pick-list every source-keyed surface needed and none had.

Every governance / readiness / semantics route is keyed by ``{source}``, and the Governance Review
screen consequently opened on an empty text input: nothing rendered until the operator guessed a slug
(the live ones are ``cib`` and ``ftr``). This route enumerates the slugs the caller is allowed to know
about, so the screen can offer them.

READ-SCOPED, and the scope is DERIVED. There is no catalog-level visibility in this system; the only
mechanism is column-level (migration 1032's ``visible_requires``). So a catalog is returned iff the
caller can see at least one column in it, and a caller who can see none does not learn it exists — the
listing is a ``GROUP BY`` over read-scoped column rows, so a fully-hidden catalog yields no group at
all rather than a filtered-out row (see :mod:`overlay.upload.catalogs`). Roles come from the
authenticated session, NEVER the request, so this surface cannot be asked for someone else's scope.

Gated by ``catalog:read`` — the same permission as its ``/sources/{source}/...`` peers, since knowing a
catalog exists is strictly less than reading inside it.

The payload is the slug plus scope-honest ``tables`` / ``columns`` counts. Deliberately NOT here: any
display name (no source of truth exists — a UI wanting a readable form can upper-case the slug) and
any pending-governance count (the queue surface owns those; two implementations would drift).
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.catalogs import list_visible_catalogs

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/catalogs", dependencies=[Depends(require_catalog_read)])
def list_catalogs(conn: _Conn, identity: _Identity) -> dict:
    """The catalogs this caller may see: slug + read-scoped table/column counts, slug-sorted.

    Empty list when the caller can see nothing — including when nothing has been uploaded. Never a
    404 and never an error: "no catalogs you may see" and "no catalogs" are deliberately the same
    answer, so the response cannot be used to probe for hidden catalogs.
    """
    catalogs = list_visible_catalogs(conn, roles=identity.role_claims)
    return {"catalogs": [asdict(c) for c in catalogs]}
