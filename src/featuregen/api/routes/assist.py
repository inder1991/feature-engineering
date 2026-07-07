"""LLM feature-assist — proposals only. Nothing here mutates state; registering a feature is a
separate explicit POST /features (suggestion-then-confirm, spec guardrail)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm, require_feature_generate
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.feature_assist import (
    LeakageWarning,
    Recipe,
    feature_recipe,
    leakage_check,
    recommend_feature_sets_report,
    recommend_features_report,
    recommend_set,
    refine_idea,
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
    entity: str | None = None
    target_ref: str | None = None
    # The round's prediction goal. Optional; when present the model revises against the goal the
    # candidate was generated for, not the instruction alone.
    objective: str | None = None


class RecipeIn(BaseModel):
    query: str = Field(min_length=1)
    catalog_source: str


class LeakageIn(BaseModel):
    derives_from: list[str]
    target_ref: str


@router.post("/features/recommend", dependencies=[Depends(require_feature_generate)])
def recommend(
    body: RecommendIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    # The gauntlet's target-leakage gate runs only when target_ref is passed and its freshness gate
    # only when `now` is; over HTTP we ALWAYS pass the server clock (and forward optional
    # target_ref/entity) so those gates are ON — omitting them would silently downgrade safety
    # (review root-cause A). `actor=identity` so every llm_call this round records is attributed
    # to the HUMAN who asked, not the fallback service enrichment identity.
    report = recommend_features_report(conn, body.objective, client,
                                       catalog_source=body.catalog_source,
                                       roles=identity.role_claims,
                                       target_ref=body.target_ref, entity=body.entity,
                                       feedback=body.feedback, now=datetime.now(UTC),
                                       actor=identity)
    # Rejections are shown to the human, never hidden: {"name", "reason", "code"} per candidate.
    return {"proposals": report.ideas, "rejections": report.rejections}


@router.post("/features/refine", dependencies=[Depends(require_feature_generate)])
def refine(
    body: RefineIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    """One human-directed revision of one candidate. Both outcomes are 200: a gauntlet rejection of
    the revision is data the reviewer acts on, not a server error. The revision stays a proposal;
    registration remains the separate explicit POST /features confirm."""
    revised, rejection = refine_idea(conn, body.candidate.model_dump(), body.instruction, client,
                                     catalog_source=body.catalog_source,
                                     roles=identity.role_claims, entity=body.entity,
                                     target_ref=body.target_ref, now=datetime.now(UTC),
                                     objective=body.objective, actor=identity)
    if revised is not None:
        return {"revised": revised}
    rej = rejection or {}
    return {"rejected": {"reason": str(rej.get("reason", "")), "code": str(rej.get("code", ""))}}


@router.post("/features/recommend-sets", dependencies=[Depends(require_feature_generate)])
def recommend_sets(
    body: RecommendIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict:
    """One validated set per applicable strategy lens, plus the ADVISORY set recommendation (a
    fit/coverage judgment, honestly caveated — never a performance claim) and the aggregated
    gauntlet rejections. Same safety posture as /features/recommend: server clock always on."""
    report = recommend_feature_sets_report(conn, body.objective, client,
                                           catalog_source=body.catalog_source,
                                           roles=identity.role_claims,
                                           target_ref=body.target_ref, entity=body.entity,
                                           feedback=body.feedback, now=datetime.now(UTC),
                                           actor=identity)
    # No recommendation over nothing: when every set came back empty there is nothing to advise on,
    # and we do not spend an LLM call to say so.
    recommendation = (recommend_set(conn, report.sets, body.objective, client, actor=identity)
                      if any(s.features for s in report.sets) else None)
    return {"sets": report.sets, "recommendation": recommendation,
            "rejections": report.rejections}


@router.post("/features/recipe", dependencies=[Depends(require_feature_generate)])
def recipe(
    body: RecipeIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> Recipe:
    return feature_recipe(conn, body.query, client,
                          catalog_source=body.catalog_source, roles=identity.role_claims,
                          actor=identity)


@router.post("/features/leakage-check", dependencies=[Depends(require_feature_generate)])
def leakage(
    body: LeakageIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict[str, list[LeakageWarning]]:
    return {"warnings": leakage_check(conn, body.derives_from, body.target_ref, client,
                                      actor=identity)}
