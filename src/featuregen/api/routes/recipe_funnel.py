"""STEP 0 — the recipe-funnel diagnostic: every template's fate against one catalog.

``GET /catalog/{catalog_source}/recipe-funnel`` serializes
:func:`overlay.upload.templates.recipe_funnel` unchanged: the REAL grounding verdict per template,
EVERY unmet required need with role and concept, the blocked-concept histogram in wire order, and
the grounding stopwatch. Read-only, ``catalog:read``-gated like every ``/catalog/...`` read, with
the session's roles as the read scope — never the request's.

WHY IT EXISTS (router plan, Step 0): the gauntlet's per-recipe reject codes were already on the v2
payload (`collection.rejections`), but the OTHER side of the funnel — the ~134 templates that never
ground, and which concepts starve them — had no surface. That histogram sized every task in the
router plan and was computed by hand in kubectl four times before this endpoint. An unknown catalog
returns the honest answer (nothing grounds; every required need unmet), never a 404: the funnel's
whole point is explaining absence.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends

from featuregen.api.deps import get_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.templates import recipe_funnel

router = APIRouter()


@router.get("/catalog/{catalog_source}/recipe-funnel",
            dependencies=[Depends(require_catalog_read)])
def catalog_recipe_funnel(
    catalog_source: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> dict:
    funnel = recipe_funnel(conn, catalog_source=catalog_source, roles=identity.role_claims)
    return {
        "catalog_source": catalog_source,
        "registry_total": funnel.registry_total,
        "grounded": funnel.grounded,
        "entries": [
            {
                "template_id": e.template_id,
                "status": e.status,
                "reason_codes": list(e.reason_codes),
                "unmet": [{"role": role, "concept": concept} for role, concept in e.unmet],
            }
            for e in funnel.entries
        ],
        "blocked_concepts": [
            {"concept": concept, "blocked": count} for concept, count in funnel.blocked_concepts
        ],
        "elapsed_ms": funnel.elapsed_ms,
    }
