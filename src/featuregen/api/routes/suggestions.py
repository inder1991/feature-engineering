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

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, get_identity, require_feature_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.suggestions import suggest_features_for_table

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/catalog/{catalog_source}/tables/{table}/suggestions",
            dependencies=[Depends(require_feature_read)])
def table_suggestions(catalog_source: str, table: str, conn: _Conn, identity: _Identity) -> dict:
    """This table's suggested features. A table with none (or one this catalog does not hold)
    returns the honest empty payload — that is a catalog-readiness fact, not a server error."""
    return suggest_features_for_table(conn, catalog_source=catalog_source, table=table,
                                      roles=identity.role_claims)
