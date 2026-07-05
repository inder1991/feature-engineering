from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.features import (
    FeatureFreshness,
    FeatureSpec,
    feature_freshness,
    features_affected_by,
    register_feature,
)

router = APIRouter()


class DerivesFromIn(BaseModel):
    catalog_source: str
    object_ref: str


class FeatureSpecIn(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""
    grain_table: str | None = None
    aggregation: str | None = None
    as_of_column: str | None = None
    derives_from: list[DerivesFromIn] = []


@router.post("/features")
def create_feature(
    body: FeatureSpecIn,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> dict[str, str]:
    """Registration is the explicit-confirm step — suggestions only become features here."""
    spec = FeatureSpec(
        name=body.name, description=body.description, grain_table=body.grain_table,
        aggregation=body.aggregation, as_of_column=body.as_of_column,
        derives_from=tuple((d.catalog_source, d.object_ref) for d in body.derives_from))
    return {"feature_id": register_feature(conn, spec)}


@router.get("/features/{feature_id}/freshness")
def freshness(
    feature_id: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> FeatureFreshness:
    row = conn.execute("SELECT 1 FROM feature WHERE feature_id = %s", (feature_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown feature {feature_id!r}")
    return feature_freshness(conn, feature_id, now=datetime.now(UTC))


@router.get("/columns/{object_ref}/feature-impact")
def feature_impact(
    object_ref: str,
    source: str,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> dict[str, list[str]]:
    return {"feature_ids": features_affected_by(conn, source, object_ref)}
