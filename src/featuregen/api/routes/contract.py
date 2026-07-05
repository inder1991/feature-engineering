"""Hypothesis-driven feature-contract flow over HTTP.

Stateless: the frontend carries the discovered options / draft as JSON between steps, and the SERVER
re-validates (the deterministic MCV re-runs at author + confirm), so a tampered payload can never govern
a leaky / stale / ungrounded contract. Safety kwargs (roles, target_ref, server clock) are always
threaded — omitting them would silently downgrade safety (review root-cause A).
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
from featuregen.overlay.upload.contract.gate1 import build_considered_set
from featuregen.overlay.upload.contract.govern import (
    Contract,
    ContractValidationError,
    confirm_contract,
)
from featuregen.overlay.upload.contract.intake import IntentValidationError, submit_intent
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.feature_assist import FeatureIdea

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


# ---- I/O models mirroring the domain dataclasses (JSON carries state between steps) --------------
class FeatureIn(BaseModel):
    name: str
    description: str = ""
    derives_from: list[str]
    aggregation: str | None = None
    grain_table: str | None = None
    derives_pairs: list[tuple[str, str]] = []

    def to_idea(self) -> FeatureIdea:
        return FeatureIdea(
            name=self.name, description=self.description, derives_from=self.derives_from,
            aggregation=self.aggregation, grain_table=self.grain_table,
            derives_pairs=tuple(tuple(p) for p in self.derives_pairs))


class DraftIn(BaseModel):
    feature_name: str
    definition: str
    grain_table: str | None = None
    aggregation: str | None = None
    as_of_column: str | None = None
    derives_from: list[str]
    target_ref: str | None = None
    derives_pairs: list[tuple[str, str]] = []
    join_path: list[dict] = []

    def to_draft(self) -> ContractDraft:
        return ContractDraft(
            feature_name=self.feature_name, definition=self.definition, grain_table=self.grain_table,
            aggregation=self.aggregation, as_of_column=self.as_of_column,
            derives_from=self.derives_from, target_ref=self.target_ref,
            derives_pairs=tuple(tuple(p) for p in self.derives_pairs), join_path=tuple(self.join_path))


class ConsideredSetIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    definition: str = ""
    objective: str = Field(min_length=1)
    catalog_source: str | None = None
    entity: str | None = None
    target_ref: str | None = None


class DraftReqIn(BaseModel):
    feature: FeatureIn
    target_ref: str | None = None


# ---- routes -------------------------------------------------------------------------------------
@router.post("/contract/considered-set")
def considered_set(body: ConsideredSetIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Intake (mandatory hypothesis + optional definition, redacted) → the validated considered set:
    the anchor (from the definition) + generated alternatives + an advisory recommendation. Persists
    the intent. Every option shown has passed the gauntlet."""
    try:
        intent = submit_intent(hypothesis=body.hypothesis, definition=body.definition,
                               actor=identity.subject)
    except IntentValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    cs = build_considered_set(
        conn, intent, client, entity=body.entity, catalog_source=body.catalog_source,
        roles=identity.role_claims, target_ref=body.target_ref, now=datetime.now(UTC))
    return {"intent_id": intent.intent_id, "anchor": cs.anchor,
            "alternatives": cs.alternatives, "recommendation": cs.recommendation}


@router.post("/contract/draft")
def draft(body: DraftReqIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Author a contract draft for the chosen feature + run the bounded critique→refine loop (MCV each
    pass). Returns the draft + any unresolved MCV reasons — read-scoped, target carried on the draft."""
    feature = body.feature.to_idea()
    d = draft_contract(conn, feature, client, roles=identity.role_claims,
                       target_ref=body.target_ref, actor=identity)
    d, unresolved = author_contract(conn, d, client, now=datetime.now(UTC), actor=identity)
    return {"draft": d, "unresolved": unresolved}


@router.post("/contract/confirm")
def confirm(body: DraftIn, conn: _Conn, identity: _Identity) -> Contract:
    """The human gate: re-runs the MCV server-side and refuses to govern a leaky / stale / ungrounded /
    empty draft; otherwise registers a versioned, drift-linked contract."""
    try:
        return confirm_contract(conn, body.to_draft(), actor=identity.subject, now=datetime.now(UTC))
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
