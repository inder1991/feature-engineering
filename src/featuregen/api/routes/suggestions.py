"""P4 v1 — the read-only suggested-features route.

``GET /catalog/{catalog_source}/tables/{table}/suggestions`` exposes
:func:`overlay.upload.suggestions.suggest_features_for_table`: every template candidate this catalog
can ground on the table, grouped by entity, with the gauntlet's own statuses. NO hypothesis, NO
intent, NO LLM — and no verb other than GET, because v1 WRITES NOTHING: there is deliberately no
surface here from which a suggestion could be accepted, dismissed or governed.

Gated by ``catalog:read`` (``require_catalog_read``), like its sibling ``/catalog/...`` reads.

Why not ``feature:read``: this surface exists so a CURATOR can see what still needs curating on a
table they own — its empty states are data-owner to-dos ("these columns carry no business concept",
"this table has no confirmed as-of column"). ``data_owner`` holds ``catalog:read`` but deliberately
NOT ``feature:read``, a boundary pinned by ``test_data_owner_can_upload_but_not_read_features_or_
generate`` (``GET /features`` must 403 for them). Granting ``feature:read`` to reach this one page
would also hand over the feature registry, the contract reads and the lineage features layer — far
more than the problem needs. So the narrower change is here, on the route.

The trade, stated honestly: derived feature content is now readable via a ``catalog:read`` route.
That is defensible — these suggestions are DERIVED FROM the catalog and rendered on a catalog table
page, and nothing here can accept, dismiss or govern anything — but it does mean the route no longer
gates purely on content class. Revisit if a finer ``feature:suggest:read`` permission is ever added.

Read-scope is separate and mandatory: ``roles=identity.role_claims`` comes from the authenticated
session (never the request), so a column the caller may not see is not a grounding candidate and
cannot be suggested."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query
from psycopg import sql

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.join_path import MAX_HOPS_CEILING, MAX_HOPS_DEFAULT
from featuregen.overlay.upload.suggestions import suggest_features_for_table

logger = logging.getLogger(__name__)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# One request grounds the WHOLE template registry (~157 recipes) against THIS TABLE's columns, so its
# cost is O(recipes × table columns) — constant in the number of tables the catalog holds. It used to
# ground catalog-wide and filter, which scaled with catalog WIDTH (a 3-table/19-column fixture issued
# ~1.7k SELECTs for one page; the same page is ~0.4k per-table). It is still hundreds of statements on
# a wide TABLE, and a slow one must fail FAST (a 500 the caller sees) rather than pin a connection for
# minutes. 30s is well above any measured run here and well below any sane client timeout; change this
# one constant to move it.
SUGGESTIONS_STATEMENT_TIMEOUT_MS = 30_000


@router.get("/catalog/{catalog_source}/tables/{table}/suggestions",
            dependencies=[Depends(require_catalog_read)])
def table_suggestions(
    catalog_source: str, table: str, conn: _Conn, identity: _Identity,
    max_hops: Annotated[int, Query(ge=1, le=MAX_HOPS_CEILING)] = MAX_HOPS_DEFAULT,
) -> dict:
    """This table's suggested features. A table with no suggestions returns the honest empty payload
    — that is a catalog-readiness fact, not a server error — and one this catalog does not hold is
    reported distinctly as ``table_known: false``, never as "no concepts".

    ``max_hops`` is the EXPLICIT opt-in for a wider join neighbourhood. Its default is the capped
    one an automatic page load gets (``MAX_HOPS_DEFAULT``); FastAPI's own bounds refuse anything
    above ``MAX_HOPS_CEILING`` with a 422, so a request cannot ask the server for an unbounded walk.
    Raising it changes which tables are ELIGIBLE to widen into, never how many are admitted — the
    table cap and column budget still apply — so the page's cost stays bounded either way. Choosing
    WHICH deeper join path to follow is a governed, explicit act that wants its own picker; that UI
    is DEFERRED, and this parameter is the surface it will use."""
    # SET LOCAL is transaction-scoped (get_conn owns the txn), so the bound dies with the request.
    # SET takes no bound parameters, so the literal is composed — the profiler's own precedent.
    conn.execute(sql.SQL("SET LOCAL statement_timeout = {}")
                 .format(sql.Literal(SUGGESTIONS_STATEMENT_TIMEOUT_MS)))
    started = time.monotonic()
    out = suggest_features_for_table(conn, catalog_source=catalog_source, table=table,
                                     roles=identity.role_claims, max_hops=max_hops)
    logger.info("suggestions for %s.%s took %.3fs (known=%s, suggested=%s, hops=%s, "
                "neighbours=%s/%s)", catalog_source, table, time.monotonic() - started,
                out["table_known"], out["summary"]["suggested"], max_hops,
                out["neighbourhood"]["tables_considered"], out["neighbourhood"]["tables_available"])
    return out
