"""LLM feature-assist — proposals only. Nothing here mutates state; registering a feature is a
separate explicit POST /features (suggestion-then-confirm, spec guardrail)."""
from __future__ import annotations

from datetime import UTC, datetime
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
    feature_context_enabled,
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


def _compatibility_only_guard() -> None:
    """SE-11 step 5 — the bypass audit's verdict on the direct feature routes: they skip typed
    intent, confirmed scope, dispositions, and the semantic engine, so they are COMPATIBILITY-
    ONLY. In enforced semantic mode no public endpoint may remain a bypass around eligibility —
    the refusal is typed and points at the governed pipeline. In legacy/shadow they serve
    unchanged, marked, so no client discovers the retirement by surprise at cutover."""
    from featuregen.overlay.upload.recipe_rollout import RecipeRolloutConfig

    if RecipeRolloutConfig.from_env().semantic_planning == "semantic_v1":
        raise HTTPException(status_code=409, detail={
            "code": "SEMANTIC_ENFORCED_USE_CONTRACT_PIPELINE",
            "message": "direct feature routes are compatibility-only; use "
                       "POST /contract/considered-set (typed intent + confirmed scope + "
                       "semantic eligibility)",
        })


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
    _compatibility_only_guard()
    report = recommend_features_report(conn, body.objective, client,
                                       catalog_source=body.catalog_source,
                                       roles=identity.role_claims,
                                       target_ref=body.target_ref, entity=body.entity,
                                       feedback=body.feedback, now=datetime.now(UTC),
                                       actor=identity)
    # Rejections are shown to the human, never hidden: {"name", "reason", "code"} per candidate.
    feature_context = feature_context_enabled()
    return {"proposals": [serialize_feature_idea(i, feature_context=feature_context)
                          for i in report.ideas],
            "rejections": report.rejections,
            "compatibility_only": True}


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

    B9: under semantic_v1 the revision goes through the ENGINE — the model revises the
    MEANING (one audited intent call seeded with the candidate + the instruction), the shared
    binder re-binds from scratch, the gauntlet re-validates. The revised card is a PREVIEW:
    save-idea works on it; GOVERNING it requires a whole-round regenerate, which mints the
    fresh run + superseding revision the governed flow demands (SE-10 step 9)."""
    from featuregen.overlay.upload.recipe_rollout import RecipeRolloutConfig

    if RecipeRolloutConfig.from_env().semantic_planning == "semantic_v1":
        if body.catalog_source is None:
            raise HTTPException(status_code=422, detail={
                "code": "SEMANTIC_REQUIRES_CATALOG_SOURCE",
                "message": "semantic refine plans over one catalog — name a catalog_source"})
        return _refine_as_intent_revision(conn, body, client, identity)
    _compatibility_only_guard()
    revised, rejection = refine_idea(conn, body.candidate.model_dump(), body.instruction, client,
                                     catalog_source=body.catalog_source,
                                     roles=identity.role_claims, entity=body.entity,
                                     target_ref=body.target_ref, now=datetime.now(UTC),
                                     objective=body.objective, actor=identity)
    feature_context = feature_context_enabled()
    if revised is not None:
        return {"revised": serialize_feature_idea(revised, feature_context=feature_context),
                "compatibility_only": True}
    rej = rejection or {}
    return {"rejected": {"reason": str(rej.get("reason", "")), "code": str(rej.get("code", ""))},
            "compatibility_only": True}


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
    from featuregen.overlay.upload.taxonomy.use_cases import selectable_leaves

    from featuregen.overlay.field_evidence import canonical_hash

    candidates, rejections = llm_intent_candidates(
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
    """One validated set per applicable strategy lens, plus the ADVISORY set recommendation (a
    fit/coverage judgment, honestly caveated — never a performance claim) and the aggregated
    gauntlet rejections. Same safety posture as /features/recommend: server clock always on."""
    _compatibility_only_guard()
    report = recommend_feature_sets_report(conn, body.objective, client,
                                           catalog_source=body.catalog_source,
                                           roles=identity.role_claims,
                                           target_ref=body.target_ref, entity=body.entity,
                                           feedback=body.feedback, now=datetime.now(UTC),
                                           actor=identity)
    feature_context = feature_context_enabled()
    sets = [{"lens": s.lens,
             "features": [serialize_feature_idea(f, feature_context=feature_context)
                          for f in s.features]}
            for s in report.sets]
    # No recommendation over nothing: when every set came back empty there is nothing to advise on,
    # and we do not spend an LLM call to say so.
    recommendation = (recommend_set(conn, report.sets, body.objective, client, actor=identity)
                      if any(s.features for s in report.sets) else None)
    return {"sets": sets, "recommendation": recommendation, "rejections": report.rejections,
            "compatibility_only": True}


@router.post("/features/recipe", dependencies=[Depends(require_feature_generate)])
def recipe(
    body: RecipeIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> Recipe:
    _compatibility_only_guard()
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
