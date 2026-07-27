"""P4 v1 — the read-only suggested-features route.

``GET /catalog/{catalog_source}/tables/{table}/suggestions`` exposes
:func:`overlay.upload.suggestions.suggest_features_for_table`: every template candidate this catalog
can ground on the table, grouped by entity, with the gauntlet's own statuses. NO hypothesis, NO
intent, NO LLM — and no verb other than GET, because v1 WRITES NOTHING: there is deliberately no
surface here from which a suggestion could be accepted, dismissed or governed.

Gated by ``feature:read`` (``require_feature_read``). The payload is feature-generation output — the
same class of content the assist proposal routes gate on ``feature:generate`` — not raw catalog
metadata, so this takes the most conservative EXISTING read guard rather than the ``catalog:read``
its sibling ``/catalog/...`` reads use: ``feature:read``'s roles are a strict subset (it drops
``data_owner``, who publishes the catalog but does not build features). Read-scope is separate and
mandatory: ``roles=identity.role_claims`` comes from the authenticated session (never the request),
so a column the caller may not see is not a grounding candidate and cannot be suggested."""

from __future__ import annotations

import logging
import time
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from psycopg import sql

from featuregen.api.deps import get_conn, get_identity, require_feature_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.suggestions import suggest_features_for_table

logger = logging.getLogger(__name__)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# One request grounds the WHOLE template registry against the catalog, so its cost scales with
# catalog WIDTH, not with the table: a 2-table/12-column fixture already issues ~1.7k SELECTs. That
# is the engine's shape and this read-only route does not get to change it — but a wide catalog must
# fail FAST (a 500 the caller sees) rather than pin a connection for minutes. 30s is well above any
# measured run here and well below any sane client timeout; change this one constant to move it.
SUGGESTIONS_STATEMENT_TIMEOUT_MS = 30_000


@router.get("/catalog/{catalog_source}/tables/{table}/suggestions",
            dependencies=[Depends(require_feature_read)])
def table_suggestions(catalog_source: str, table: str, conn: _Conn, identity: _Identity) -> dict:
    """This table's suggested features. A table with no suggestions returns the honest empty payload
    — that is a catalog-readiness fact, not a server error — and one this catalog does not hold is
    reported distinctly as ``table_known: false``, never as "no concepts"."""
    # SET LOCAL is transaction-scoped (get_conn owns the txn), so the bound dies with the request.
    # SET takes no bound parameters, so the literal is composed — the profiler's own precedent.
    conn.execute(sql.SQL("SET LOCAL statement_timeout = {}")
                 .format(sql.Literal(SUGGESTIONS_STATEMENT_TIMEOUT_MS)))
    started = time.monotonic()
    out = suggest_features_for_table(conn, catalog_source=catalog_source, table=table,
                                     roles=identity.role_claims)
    logger.info("suggestions for %s.%s took %.3fs (known=%s, suggested=%s)", catalog_source, table,
                time.monotonic() - started, out["table_known"], out["summary"]["suggested"])
    return out
