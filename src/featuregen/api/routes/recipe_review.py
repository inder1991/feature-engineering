"""BR-23 — the recipe-review routes: read the evidence, record a decision.

``GET /recipes`` is the review queue's read model: every V2 recipe with its validity fold at
its current canonical revision, from ONE store query. ``GET /recipes/{recipe_id}`` serves the
full definition an SME signs off on — the serialized contract dataclass itself, never a
paraphrase. ``GET /recipes/{recipe_id}/reviews`` returns the full immutable event history plus
the validity fold at the recipe's CURRENT canonical revision — all three reads are
``catalog:read`` (the same audience that sees the recipes sees their review state). ``POST``
records one decision and is gated by ``governance:confirm`` with OPTIMISTIC CONCURRENCY: the
caller states the revision hash they reviewed, and a mismatch with the live definition is a
409 — you cannot approve a definition that changed under you. Reviewer identity comes from the
SESSION, never the body: review history is attributable or it is nothing.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from featuregen.api.deps import get_feature_gen_conn, get_identity, require_permission
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES, v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import (
    REVIEW_DECISIONS,
    REVIEWER_ROLES,
    RecipeReviewError,
    record_review_event,
    review_events,
    review_events_all,
)
from featuregen.overlay.upload.recipe_review_validity import (
    ReviewValidityV1,
    by_role_at_revision,
    required_reviewer_roles,
    review_validity,
    reviews_by_role_for_revision,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class ReviewDecisionRequest(BaseModel):
    """One decision. ``reviewed_revision_hash`` is the optimistic-concurrency token — the
    hash the reviewer actually looked at."""

    model_config = ConfigDict(extra="forbid")

    decision: str
    reviewer_role: str
    reviewed_revision_hash: str
    rationale: str = ""
    gold_corpus_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    supersedes_event_id: str | None = None


def _recipe_or_404(recipe_id: str):
    recipe = v2_recipe_by_id(recipe_id)
    if recipe is None:
        raise HTTPException(status_code=404, detail=f"unknown recipe {recipe_id!r}")
    return recipe


# The registry is immutable per process (frozen dataclass constants), so each recipe's canonical
# hash is computed once and remembered — the summary route folds all 317 per request.
_HASH_CACHE: dict[str, str] = {}


def _revision_hash(recipe) -> str:
    cached = _HASH_CACHE.get(recipe.recipe_id)
    if cached is None:
        cached = canonical_recipe_v2_hash(recipe)
        _HASH_CACHE[recipe.recipe_id] = cached
    return cached


def _validity_json(validity: ReviewValidityV1) -> dict:
    return {
        "current": validity.current,
        "required_roles": list(validity.required_roles),
        "approved_roles": list(validity.approved_roles),
        "missing_roles": list(validity.missing_roles),
        "blocking_decisions": list(validity.blocking_decisions),
        "single_identity_violation": validity.single_identity_violation,
    }


@router.get("/recipes", dependencies=[Depends(require_permission("catalog:read"))])
def recipe_review_summary(conn: _Conn) -> dict:
    """The review queue: every V2 recipe with its validity at its current revision."""
    events_by_recipe = review_events_all(conn)
    rows = []
    for recipe in V2_RECIPES:
        revision_hash = _revision_hash(recipe)
        by_role = by_role_at_revision(
            events_by_recipe.get(recipe.recipe_id, ()), revision_hash)
        rows.append({
            "recipe_id": recipe.recipe_id,
            "family": recipe.family,
            "readiness": recipe.readiness,
            "computation_kind": recipe.computation_kind,
            "display_label": recipe.output.display_label,
            "leakage_classification": recipe.leakage.classification,
            "recipe_revision_hash": revision_hash,
            "validity": _validity_json(review_validity(recipe, by_role)),
        })
    return {"recipes": rows, "total": len(rows)}


@router.get("/recipes/{recipe_id}",
            dependencies=[Depends(require_permission("catalog:read"))])
def recipe_detail(recipe_id: str) -> dict:
    """The definition under review, serialized from the contract dataclass itself."""
    recipe = _recipe_or_404(recipe_id)
    return {
        "recipe": asdict(recipe),
        "recipe_revision_hash": _revision_hash(recipe),
        "required_reviewer_roles": list(required_reviewer_roles(recipe)),
    }


@router.get("/recipes/{recipe_id}/reviews",
            dependencies=[Depends(require_permission("catalog:read"))])
def recipe_reviews(recipe_id: str, conn: _Conn) -> dict:
    recipe = _recipe_or_404(recipe_id)
    revision_hash = _revision_hash(recipe)
    by_role = reviews_by_role_for_revision(
        conn, recipe_id=recipe_id, recipe_revision_hash=revision_hash)
    validity = review_validity(recipe, by_role)
    return {
        "recipe_id": recipe_id,
        "recipe_revision_hash": revision_hash,
        "validity": _validity_json(validity),
        "events": [{
            "event_id": e.event_id, "recipe_revision_hash": e.recipe_revision_hash,
            "decision": e.decision, "reviewer": e.reviewer,
            "reviewer_role": e.reviewer_role, "rationale": e.rationale,
            "gold_corpus_refs": list(e.gold_corpus_refs),
            "policy_dependencies": list(e.policy_dependencies),
            "supersedes_event_id": e.supersedes_event_id,
        } for e in review_events(conn, recipe_id)],
    }


@router.post("/recipes/{recipe_id}/reviews", status_code=201)
def record_decision(
    recipe_id: str, body: ReviewDecisionRequest, conn: _Conn,
    identity: Annotated[IdentityEnvelope,
                        Depends(require_permission("governance:confirm"))],
) -> dict:
    recipe = _recipe_or_404(recipe_id)
    if body.decision not in REVIEW_DECISIONS or body.reviewer_role not in REVIEWER_ROLES:
        raise HTTPException(status_code=422, detail="unknown decision or reviewer role")

    live_hash = _revision_hash(recipe)
    if body.reviewed_revision_hash != live_hash:
        raise HTTPException(status_code=409, detail=(
            "the definition changed since you reviewed it: reviewed "
            f"{body.reviewed_revision_hash[:12]}…, live {live_hash[:12]}… — re-review the "
            "current revision"))

    expectation_hash = None
    if recipe.formula is not None:
        from featuregen.overlay.upload.recipe_formula_expectations_v2 import (
            RECIPE_FORMULA_V2_EXPECTATIONS,
        )
        pinned = RECIPE_FORMULA_V2_EXPECTATIONS.get(recipe.formula.expectation_ref)
        expectation_hash = pinned[1] if pinned else None

    try:
        event_id = record_review_event(
            conn, recipe_id=recipe_id, recipe_revision_hash=live_hash,
            decision=body.decision, reviewer=identity.subject,
            reviewer_role=body.reviewer_role,
            output_id=recipe.output.output_id,
            reviewed_primary_objective=recipe.primary_objective,
            reviewed_supporting_objectives=recipe.supporting_objectives,
            formula_expectation_hash=expectation_hash,
            gold_corpus_refs=body.gold_corpus_refs,
            policy_dependencies=tuple(recipe.eligibility.policy_refs),
            permitted_stages=recipe.leakage.permitted_stages,
            prohibited_stages=recipe.leakage.prohibited_stages,
            rationale=body.rationale,
            evidence_refs=body.evidence_refs,
            supersedes_event_id=body.supersedes_event_id)
    except RecipeReviewError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    # No route-level commit: get_conn commits on success (one transaction per request) — a
    # mid-handler commit was this codebase's only one, and it broke request atomicity.
    return {"event_id": event_id, "recipe_revision_hash": live_hash}


__all__ = ["ReviewDecisionRequest", "recipe_detail", "recipe_review_summary",
           "recipe_reviews", "record_decision", "router"]
