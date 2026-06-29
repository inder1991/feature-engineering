from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, IdentityEnvelope, ProvenanceEnvelope
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_feature_version_id
from featuregen.governance.attributes import GovernanceAttributes, from_feature_version_row

_GOVERNANCE_COLUMNS: tuple[str, ...] = (
    "feature_version_id", "feature_id", "produced_by_run", "base_feature_version_id",
    "verification_stamp", "risk_tier", "approval_type", "approved_use_cases",
    "blocked_use_cases", "required_artifact_refs", "dsl_operation_catalog_version",
    "approval", "expires_at", "immutable",
)


def load_governance_attributes(conn: DbConn, feature_version_id: str) -> GovernanceAttributes:
    """Load the immutable §3.8 governance attributes for a feature_version (read of a frozen,
    write-once row — replay-safe for guard evaluation). Raises KeyError if the version is unknown."""
    row = conn.execute(
        f"SELECT {', '.join(_GOVERNANCE_COLUMNS)} FROM feature_versions WHERE feature_version_id=%s",
        (feature_version_id,),
    ).fetchone()
    if row is None:
        raise KeyError(f"unknown feature_version_id: {feature_version_id!r}")
    return from_feature_version_row(dict(zip(_GOVERNANCE_COLUMNS, row)))


def mint_feature_version(
    conn: DbConn, *, feature_id: str, produced_by_run: str, verification_stamp: str,
    risk_tier: str, approval_type: str, approved_use_cases, blocked_use_cases,
    required_artifact_refs: Mapping[str, Any], content_hash: str,
    actor: IdentityEnvelope, provenance: ProvenanceEnvelope,
    base_feature_version_id: Optional[str] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    approval: Optional[Mapping[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> str:
    fv_id = new_feature_version_id()
    conn.execute(
        "INSERT INTO feature_versions ("
        "  feature_version_id, feature_id, produced_by_run, base_feature_version_id,"
        "  verification_stamp, risk_tier, approval_type, approved_use_cases, blocked_use_cases,"
        "  required_artifact_refs, dsl_operation_catalog_version, approval, expires_at,"
        "  content_hash, immutable) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, true)",
        (fv_id, feature_id, produced_by_run, base_feature_version_id, verification_stamp,
         risk_tier, approval_type, list(approved_use_cases), list(blocked_use_cases),
         Jsonb(dict(required_artifact_refs)), dsl_operation_catalog_version,
         Jsonb(dict(approval or {})), expires_at, content_hash),
    )
    append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_MINTED",
        payload={"feature_id": feature_id, "feature_version_id": fv_id,
                 "produced_by_run": produced_by_run,
                 "base_feature_version_id": base_feature_version_id},
        actor=actor, provenance=provenance, feature_id=feature_id, run_id=produced_by_run,
    )
    return fv_id
