"""Hypothesis-driven feature-contract flow over HTTP.

Stateless: the frontend carries the discovered options / draft as JSON between steps, and the SERVER
re-validates (the deterministic MCV re-runs at author + confirm), so a tampered payload can never govern
a leaky / stale / ungrounded contract. Safety kwargs (roles, target_ref, server clock) are always
threaded — omitting them would silently downgrade safety (review root-cause A).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    get_llm,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient, compute_input_hash
from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
from featuregen.overlay.upload.contract.gate1 import (
    build_considered_set,
    chosen_feature,
    gate1_choice,
    intent_target_ref,
    persist_intent,
    record_gate1_choice,
)
from featuregen.overlay.upload.contract.govern import (
    Contract,
    ContractValidationError,
    confirm_contract,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.contract.intake import (
    IntentValidationError,
    redact_free_text,
    submit_intent,
)
from featuregen.overlay.upload.contract.review import author_contract
from featuregen.overlay.upload.contract.scope_records import record_recognition_attempt
from featuregen.overlay.upload.taxonomy.recognition import RecognitionStatus
from featuregen.overlay.upload.taxonomy.recognizer import recognize
from featuregen.overlay.upload.taxonomy.use_cases import use_case

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
            derives_pairs=tuple((p[0], p[1]) for p in self.derives_pairs),  # each is a (source, ref) pair
            join_path=tuple(self.join_path))


class ConsideredSetIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    definition: str = ""
    objective: str = Field(min_length=1)
    catalog_source: str | None = None
    entity: str | None = None
    target_ref: str | None = None
    feedback: str | None = None   # whole-round human guidance: a feedback round re-runs the considered
    #                               set under this instruction, minting a FRESH governable intent


class DraftReqIn(BaseModel):
    intent_id: str
    chosen_source: str            # "anchor" | "alternative"
    chosen_option_id: str         # the chosen feature's name (from the considered set)
    why: str = ""


class RecognitionIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    objective: str = ""           # optional prediction goal; redacted before it can reach the LLM


# ---- routes -------------------------------------------------------------------------------------
@router.post("/contract/considered-set", dependencies=[Depends(require_feature_generate)])
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
        roles=identity.role_claims, target_ref=body.target_ref, objective=body.objective,
        feedback=body.feedback, now=datetime.now(UTC))
    return {"intent_id": intent.intent_id, "anchor": cs.anchor,
            "alternatives": cs.alternatives, "recommendation": cs.recommendation,
            "rejections": cs.rejections}


@router.post("/contract/recognitions", dependencies=[Depends(require_feature_generate)])
def recognitions(body: RecognitionIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Phase-1B Gate #1 recognition: classify the objective's governed use-case scope from the
    REDACTED hypothesis/goal (recognition NEVER sees catalog columns) and persist an append-only
    recognition attempt — BEFORE any generation run exists. Decoupled from generation: no
    ``generation_run_id`` is minted here and no recipe/applicability count is returned (applicability
    owns any recipe count, computed later once the human commits to generate). FAIL-OPEN: ``recognize``
    never raises, so a provider failure/refusal folds to ``status='technical_failure'`` at HTTP 200 —
    recognition never blocks generation and never 5xxs."""
    try:
        intent = submit_intent(hypothesis=body.hypothesis, actor=identity.subject)
        redacted_goal = redact_free_text(body.objective) if body.objective else None
    except IntentValidationError as e:   # a free-text field that cannot be safely redacted -> denial
        raise HTTPException(status_code=422, detail=str(e)) from e
    # Idempotent intent: submit_intent mints a fresh id each call, so reuse the EARLIEST intent already
    # recorded for this exact (hypothesis, mode) — re-recognising the same objective is free and never
    # forks the immutable intent. persist_intent is itself ON CONFLICT (intent_id) DO NOTHING.
    prior = conn.execute(
        "SELECT intent_id FROM contract_intent WHERE hypothesis = %s AND intake_mode = %s "
        "ORDER BY created_at ASC LIMIT 1",
        (intent.hypothesis, intent.intake_mode)).fetchone()
    if prior is not None:
        intent = replace(intent, intent_id=prior[0])
    persist_intent(conn, intent)

    input_hash = compute_input_hash({"hypothesis": intent.redacted_hypothesis, "goal": redacted_goal})
    result = recognize(conn, client, redacted_hypothesis=intent.redacted_hypothesis,
                       redacted_goal=redacted_goal, actor=identity)
    recognition_id = record_recognition_attempt(
        conn, intent_id=intent.intent_id, input_hash=input_hash, result=result,
        actor=identity.subject)
    # Fail-open asymmetry: unscoped / technical_failure -> full grounding downstream (recognition never
    # narrows on doubt). The recipe count is NOT here — applicability computes it after generate.
    unscoped = result.status in (RecognitionStatus.UNSCOPED, RecognitionStatus.TECHNICAL_FAILURE)
    candidates = [{
        "use_case_id": c.use_case_id,
        "display_name": (uc.display_name if (uc := use_case(c.use_case_id)) else c.use_case_id),
        "relationship": c.relationship,
        "confidence": c.confidence,
        "evidence_spans": list(c.evidence_spans),
    } for c in result.candidates]
    return {"intent_id": intent.intent_id, "recognition_id": recognition_id,
            "status": result.status.value, "unscoped": unscoped, "candidates": candidates}


@router.post("/contract/draft", dependencies=[Depends(require_feature_generate)])
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


@router.get("/contracts", dependencies=[Depends(require_feature_read)])
def list_governed_contracts(conn: _Conn, identity: _Identity, limit: int = 50) -> list[dict]:
    return list_contracts(conn, limit=limit)


@router.get("/contracts/{contract_id}", dependencies=[Depends(require_feature_read)])
def get_governed_contract(contract_id: str, conn: _Conn, identity: _Identity) -> dict:
    c = get_contract_detail(conn, contract_id)
    if c is None:
        raise HTTPException(status_code=404, detail=f"unknown contract {contract_id!r}")
    return c


@router.post("/contract/confirm", dependencies=[Depends(require_feature_generate)])
def confirm(body: DraftIn, conn: _Conn, identity: _Identity) -> Contract:
    """The human gate — the GOVERNING write. Server-stateful, no client trust (closes the two BLOCKERs
    at the write, not just at /draft):
      * intent_id is REQUIRED; a missing/forged one is rejected (no fall back to a client target_ref);
      * the draft must correspond to the human's RECORDED Gate #1 choice reconstructed from the
        server-persisted considered set — a feature never offered/chosen cannot be governed;
      * target_ref is read SERVER-side from the intent with NO client fallback, so the leakage gate
        cannot be disabled by omitting it.
    Then confirm_contract re-runs the deterministic MCV and registers a versioned, drift-linked contract."""
    if not body.intent_id:
        raise HTTPException(status_code=422, detail="intent_id is required to govern a contract")
    choice = gate1_choice(conn, body.intent_id)
    if choice is None:
        raise HTTPException(status_code=422,
                            detail="no Gate #1 choice recorded for this intent — draft it first")
    chosen = chosen_feature(conn, body.intent_id, choice["chosen_source"], choice["chosen_option_id"])
    if chosen is None:
        raise HTTPException(status_code=422,
                            detail="the chosen feature is not in the recorded considered set")
    draft = body.to_draft()
    if (draft.feature_name != chosen.name
            or frozenset(draft.derives_pairs) != frozenset(chosen.derives_pairs)
            or (draft.aggregation or "") != (chosen.aggregation or "")):
        raise HTTPException(status_code=422, detail="the draft does not match the chosen feature")
    target = intent_target_ref(conn, body.intent_id)   # SERVER truth — never the client body
    try:
        return confirm_contract(conn, draft, actor=identity.subject, now=datetime.now(UTC),
                                target_ref=target, intent_id=body.intent_id)
    except ContractValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except psycopg.errors.UniqueViolation as e:   # concurrent double-confirm -> conflict, not 500
        raise HTTPException(status_code=409,
                            detail="a contract version conflict occurred; re-fetch and retry") from e
