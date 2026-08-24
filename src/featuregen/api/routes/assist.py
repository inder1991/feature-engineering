"""LLM feature-assist — proposals only. Nothing here mutates state; registering a feature is a
separate explicit POST /features (suggestion-then-confirm, spec guardrail)."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm, require_feature_generate
from featuregen.api.feature_serialize import serialize_feature_idea
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.feature_assist import (
    LeakageWarning,
    Recipe,
    leakage_check,
)

router = APIRouter()


class RecommendIn(BaseModel):
    objective: str = Field(min_length=1)
    catalog_source: str | None = None
    target_ref: str | None = None
    entity: str | None = None
    # HUMAN guidance for the whole generation round ("more behavioral signals, fewer balance
    # aggregates"). Steers what the model proposes; every candidate still runs the full gauntlet.
    feedback: str | None = None


class CandidateIn(BaseModel):
    """One proposal as the UI holds it — the fields the refine fix-hint needs."""
    name: str = Field(min_length=1)
    description: str = ""
    derives_from: list[str] = Field(default_factory=list)
    aggregation: str | None = None
    grain_table: str | None = None


class RefineIn(BaseModel):
    candidate: CandidateIn
    instruction: str = Field(min_length=1)
    catalog_source: str | None = None
    target_ref: str | None = None
    # ACCEPTED, NOT CONSUMED (E4, 2026-08-14). The engine's refine revises MEANING and the shared
    # binder chooses columns from the frozen catalog context, so neither an entity hint nor the
    # round's prediction goal has anywhere to enter. They stay on the request model because
    # clients already send them and a removed field would look like a rejected request; they are
    # documented as inert rather than quietly read, because a field that is threaded and ignored
    # is the thing this codebase deletes on sight.
    entity: str | None = None
    objective: str | None = None


class RecipeIn(BaseModel):
    query: str = Field(min_length=1)
    catalog_source: str


class LeakageIn(BaseModel):
    derives_from: list[str]
    target_ref: str


def _refuse_bypass() -> None:
    """SE-11 step 5, made unconditional by the E4 cutover — the bypass audit's verdict on the
    direct feature routes: they skip typed intent, confirmed scope, dispositions and the
    semantic engine, so they were COMPATIBILITY-ONLY and are now closed. No public endpoint may
    remain a bypass around eligibility, and there is no longer a mode in which one could serve.

    The ROUTES stay — a 404 would tell a client it had the wrong address, when the truth is that
    the address is right and the answer is "not this way any more". The refusal is typed and
    names where the capability moved."""
    raise HTTPException(status_code=409, detail={
        "code": "SEMANTIC_ENFORCED_USE_CONTRACT_PIPELINE",
        "message": "direct feature routes are retired; use POST /contract/considered-set "
                   "(typed intent + confirmed scope + semantic eligibility)",
    })


@router.post("/features/recommend", dependencies=[Depends(require_feature_generate)])
def recommend(
    body: RecommendIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    """RETIRED (E4). Free-form physical-column proposal was this route's whole content and that
    generator no longer exists; the typed refusal names the pipeline that replaced it."""
    _refuse_bypass()


@router.post("/features/refine", dependencies=[Depends(require_feature_generate)])
def refine(
    body: RefineIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    """One human-directed revision of one candidate. Both outcomes are 200: a gauntlet rejection of
    the revision is data the reviewer acts on, not a server error. The revision stays a proposal;
    registration remains the separate explicit POST /features confirm.

    B9: the revision goes through the ENGINE — the model revises the MEANING (one audited intent
    call seeded with the candidate + the instruction), the shared binder re-binds from scratch,
    the gauntlet re-validates. This is the ONE direct feature route that survives E4's cutover,
    because it is no longer a bypass: it plans through the same engine, over one frozen catalog
    context, and refuses without one. The revised card is a PREVIEW:
    save-idea works on it; GOVERNING it requires a whole-round regenerate, which mints the
    fresh run + superseding revision the governed flow demands (SE-10 step 9)."""
    if body.catalog_source is None:
        raise HTTPException(status_code=422, detail={
            "code": "SEMANTIC_REQUIRES_CATALOG_SOURCE",
            "message": "semantic refine plans over one catalog — name a catalog_source"})
    return _refine_as_intent_revision(conn, body, client, identity)


def _refine_as_intent_revision(conn, body, client, identity) -> dict:
    """B9 — the engine's refine: meaning revised, columns re-chosen by the binder, validation
    re-run. A column-naming instruction cannot smuggle a binding — physical keys are refused
    by the intent parser, and the binder alone assigns refs."""
    from featuregen.overlay.upload.candidate_assembly import assemble_candidates
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
    )
    from featuregen.overlay.upload.recipe_planning_lens import llm_intent_candidates
    from featuregen.overlay.upload.semantic_projection import project_assembled_set

    context = build_generation_semantic_context(
        conn, catalog_source=body.catalog_source, roles=identity.role_claims)
    candidate = body.candidate.model_dump()
    seed = (
        "REVISE the feature below per the analyst's instruction — return ONE revised abstract "
        "intent (meaning only; a deterministic stage assigns physical data).\n"
        f"Current feature: {candidate.get('name', '')} — {candidate.get('description', '')}\n"
        f"Instruction: {body.instruction}")
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

    # `_normalizations`: vocabulary repairs applied to the revision that IS served. Available
    # here on purpose (a repair nobody can see is a silent edit); rendering it is T9's.
    candidates, rejections, _normalizations = llm_intent_candidates(
        conn, client, context=context, scope_leaves=selectable_leaves(),
        redacted_hypothesis=seed, actor=identity,
        confirmed_scope_hash=canonical_hash({"unscoped": True, "route": "refine"}))
    if not candidates:
        first = rejections[0] if rejections else {"code": "INTENT_GENERATION_UNAVAILABLE",
                                                  "detail": "no valid revision returned"}
        return {"rejected": {"reason": str(first.get("detail", "")),
                             "code": str(first.get("code", ""))}}
    projection = project_assembled_set(
        assemble_candidates(list(candidates)),
        catalog_source=body.catalog_source, target_ref=body.target_ref)
    served = projection.ideas or projection.actionable_ideas
    if not served:
        reject = projection.rejections[0] if projection.rejections else {
            "reason": "the revision did not survive validation", "code": "REFUSED"}
        return {"rejected": {"reason": str(reject.get("reason", "")),
                             "code": str(reject.get("code", ""))}}
    return {"revised": serialize_feature_idea(served[0], feature_context=True),
            "regenerate_to_govern": True}


@router.post("/features/recommend-sets", dependencies=[Depends(require_feature_generate)])
def recommend_sets(
    body: RecommendIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    """RETIRED (E4). The multi-lens free-form generator this served is deleted; the considered
    set is now built by the semantic engine behind a confirmed scope."""
    _refuse_bypass()


@router.post("/features/recipe", dependencies=[Depends(require_feature_generate)])
def recipe(
    body: RecipeIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> Recipe:
    """RETIRED (E4) — a free-form NL-to-recipe bypass around typed intent and eligibility."""
    _refuse_bypass()


@router.post("/features/leakage-check", dependencies=[Depends(require_feature_generate)])
def leakage(
    body: LeakageIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict[str, list[LeakageWarning]]:
    return {"warnings": leakage_check(conn, body.derives_from, body.target_ref, client,
                                      actor=identity)}
