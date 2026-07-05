"""LLM feature-assist — proposals only. Nothing here mutates state; registering a feature is a
separate explicit POST /features (suggestion-then-confirm, spec guardrail)."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity, get_llm
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.feature_assist import (
    FeatureIdea,
    LeakageWarning,
    Recipe,
    feature_recipe,
    leakage_check,
    recommend_features,
)

router = APIRouter()


class RecommendIn(BaseModel):
    objective: str = Field(min_length=1)
    catalog_source: str | None = None


class RecipeIn(BaseModel):
    query: str = Field(min_length=1)
    catalog_source: str


class LeakageIn(BaseModel):
    derives_from: list[str]
    target_ref: str


@router.post("/features/recommend")
def recommend(
    body: RecommendIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict[str, list[FeatureIdea]]:
    ideas = recommend_features(conn, body.objective, client,
                               catalog_source=body.catalog_source, roles=identity.role_claims)
    return {"proposals": ideas}


@router.post("/features/recipe")
def recipe(
    body: RecipeIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> Recipe:
    return feature_recipe(conn, body.query, client,
                          catalog_source=body.catalog_source, roles=identity.role_claims)


@router.post("/features/leakage-check")
def leakage(
    body: LeakageIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
    client: Annotated[LLMClient, Depends(get_llm)],
) -> dict[str, list[LeakageWarning]]:
    return {"warnings": leakage_check(conn, body.derives_from, body.target_ref, client)}
