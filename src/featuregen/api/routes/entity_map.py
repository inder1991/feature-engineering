"""Ingestion-richness Task 3D — the entity map read route.

``GET /catalog/entity-map`` returns the READ-ONLY entity map
(:func:`overlay.upload.entity_map.build_entity_map`): every entity present in the graph with its
read-scoped per-catalog column counts and sample refs, plus every AVAILABLE cross-catalog link
verbatim from ``available_identifier_links()`` — the one availability truth governance, the planner
and this map all share. Gated by ``catalog:read``.

Read-scope: roles come from the authenticated session (NEVER the request), and only column counts /
sample refs are scoped — the links are the same un-scoped availability truth every existing
consumer of the reader already serves. Assembled under the REPEATABLE READ read connection so the
counts and the links describe one torn-free snapshot, mirroring the asset-detail route.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_feature_gen_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.entity_map import EntityMapV1, build_entity_map

router = APIRouter()

_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]


@router.get("/catalog/entity-map", dependencies=[Depends(require_catalog_read)])
def get_entity_map(
    conn: _RRConn,
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> EntityMapV1:
    return build_entity_map(conn, roles=identity.role_claims)
