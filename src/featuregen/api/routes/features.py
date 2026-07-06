from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn, get_identity
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.contract.govern import feature_detail
from featuregen.overlay.upload.features import (
    FeatureFreshness,
    FeatureSpec,
    consumers_of_feature,
    feature_freshness,
    features_affected_by,
    features_for_consumer,
    list_features,
    register_consumer,
    register_feature,
)

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class ConsumerIn(BaseModel):
    model_ref: str = Field(min_length=1)
    purpose: str = ""
    environment: str = "dev"


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
    try:
        return {"feature_id": register_feature(conn, spec)}
    except psycopg.errors.UniqueViolation as exc:   # feature.name is unique (0970)
        raise HTTPException(status_code=409, detail=f"a feature named {body.name!r} already exists") from exc


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


# ---- registry read surface (the catalog was write-only) -----------------------------------------
@router.get("/features")
def list_registered_features(conn: _Conn, identity: _Identity, limit: int = 50) -> list[dict]:
    return list_features(conn, limit=limit)


@router.get("/features/{feature_id}")
def get_registered_feature(feature_id: str, conn: _Conn, identity: _Identity) -> dict:
    """Feature 360: definition + verification + lineage + the HYPOTHESIS it was born from + consumers."""
    feat = feature_detail(conn, feature_id, roles=identity.role_claims)
    if feat is None:
        raise HTTPException(status_code=404, detail=f"unknown feature {feature_id!r}")
    return feat


# ---- model <-> feature consumer registration (SP-14) --------------------------------------------
@router.post("/features/{feature_id}/consumers")
def add_consumer(feature_id: str, body: ConsumerIn, conn: _Conn, identity: _Identity) -> dict:
    cid = register_consumer(conn, model_ref=body.model_ref, feature_id=feature_id,
                            purpose=body.purpose, environment=body.environment,
                            actor=identity.subject)
    if cid is None:
        raise HTTPException(status_code=404, detail=f"unknown feature {feature_id!r}")
    return {"consumer_id": cid}


@router.get("/features/{feature_id}/consumers")
def list_feature_consumers(feature_id: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return consumers_of_feature(conn, feature_id)


@router.get("/consumers/{model_ref}/features")
def list_consumer_features(model_ref: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return features_for_consumer(conn, model_ref)
