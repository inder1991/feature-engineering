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
from featuregen.overlay.upload.contract.gate1 import (
    build_considered_set,
    chosen_feature,
    intent_target_ref,
    record_gate1_choice,
)
from featuregen.overlay.upload.contract.govern import (
    Contract,
    ContractValidationError,
    confirm_contract,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.contract.intake import IntentValidationError, submit_intent
from featuregen.overlay.upload.contract.review import author_contract

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


# ---- I/O models. The security-critical state (target_ref, the chosen feature) lives SERVER-side,
# keyed by intent_id — the client carries only the transient draft + its intent_id back to confirm. ----
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
    intent_id: str | None = None   # server re-reads target_ref + links the contract via this

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
    intent_id: str
    chosen_source: str            # "anchor" | "alternative"
    chosen_option_id: str         # the chosen feature's name (from the considered set)
    why: str = ""


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
    """Gate #1 → author. The chosen feature is reconstructed from the SERVER-persisted considered set
    (BLOCKER 1 — never an arbitrary client payload); the choice is recorded (audit); the leakage target
    is read SERVER-side (BLOCKER 2). Then draft + the critique→refine loop (MCV each pass)."""
    feature = chosen_feature(conn, body.intent_id, body.chosen_source, body.chosen_option_id)
    if feature is None:
        raise HTTPException(status_code=422,
                            detail="chosen option is not in the recorded considered set for this intent")
    record_gate1_choice(conn, body.intent_id, chosen_source=body.chosen_source,
                        chosen_option_id=body.chosen_option_id, actor=identity.subject, why=body.why)
    target = intent_target_ref(conn, body.intent_id)   # server truth, not client-supplied
    d = draft_contract(conn, feature, client, roles=identity.role_claims, target_ref=target,
                       actor=identity)
    d, unresolved = author_contract(conn, d, client, now=datetime.now(UTC), actor=identity)
    return {"draft": d, "unresolved": unresolved, "intent_id": body.intent_id}


@router.get("/contracts")
def list_governed_contracts(conn: _Conn, identity: _Identity, limit: int = 50) -> list[dict]:
    return list_contracts(conn, limit=limit)


@router.get("/contracts/{contract_id}")
def get_governed_contract(contract_id: str, conn: _Conn, identity: _Identity) -> dict:
    c = get_contract_detail(conn, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown contract {contract_id!r}")
    return c


@router.post("/contract/confirm")
def confirm(body: DraftIn, conn: _Conn, identity: _Identity) -> Contract:
    """The human gate: re-runs the MCV server-side (leakage target re-read from the intent, not trusted
    from the payload) and refuses to govern a leaky / stale / ungrounded / empty draft; otherwise
    registers a versioned, drift-linked contract linked back to its intent."""
    target = intent_target_ref(conn, body.intent_id) if body.intent_id else body.target_ref
    try:
        return confirm_contract(conn, body.to_draft(), actor=identity.subject,
                                now=datetime.now(UTC), target_ref=target, intent_id=body.intent_id)
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except psycopg.errors.UniqueViolation as e:   # concurrent double-confirm -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract version conflict occurred; re-fetch and retry") from e
