"""Entity resolution — suggest which business entity an id-like column denotes, then confirm/dismiss.

Suggestions are advisory (LLM, metadata-only); a human confirms before a tag is written, because a
wrong entity mis-links catalogs. Confirmed tags survive re-upload (re-applied by build_graph)."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.entity import (
    apply_entity_suggestion,
    dismiss_entity_suggestion,
    list_entity_suggestions,
    suggest_entities,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


class SuggestIn(BaseModel):
    catalog_source: str = Field(min_length=1)


class ResolveIn(BaseModel):
    catalog_source: str = Field(min_length=1)
    object_ref: str = Field(min_length=1)


@router.post("/entity/suggest")
def suggest(body: SuggestIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Generate advisory entity suggestions for this catalog's un-tagged id-like columns (read-scoped)."""
    n = suggest_entities(conn, client, body.catalog_source, roles=identity.role_claims,
                         actor=identity)
    return {"suggested": n}


@router.get("/entity/suggestions")
def suggestions(catalog_source: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return [{"object_ref": s.object_ref, "table": s.table, "column": s.column,
             "suggested_entity": s.suggested_entity}
            for s in list_entity_suggestions(conn, catalog_source)]


@router.post("/entity/apply")
def apply(body: ResolveIn, conn: _Conn, identity: _Identity) -> dict:
    """The human confirms: write the suggested entity onto the column (durable across re-upload)."""
    if not apply_entity_suggestion(conn, body.catalog_source, body.object_ref, actor=identity):
        raise HTTPException(status_code=404, detail="no pending suggestion for that column")
    return {"applied": True}


@router.post("/entity/dismiss")
def dismiss(body: ResolveIn, conn: _Conn, identity: _Identity) -> dict:
    if not dismiss_entity_suggestion(conn, body.catalog_source, body.object_ref):
        raise HTTPException(status_code=404, detail="no pending suggestion for that column")
    return {"dismissed": True}
