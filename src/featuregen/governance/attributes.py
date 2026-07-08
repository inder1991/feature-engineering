from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from psycopg.types.json import Json

# SP-0 feature_versions vocabulary — a MINTED version is at least gauntlet-passed, so the ladder starts
# at DESIGN-CHECKED (the feature_versions DDL CHECK in 0060 matches this). This is DISTINCT from the
# overlay feature/contract vocabulary, which additionally has UNVERIFIED for direct registration and is
# enforced by the migration-0973 CHECK on those tables — do NOT add UNVERIFIED here (it would allow an
# unverified *mint*, contradicting the semantics and mismatching the 0060 CHECK).
VERIFICATION_STAMPS: tuple[str, ...] = ("DESIGN-CHECKED", "DATA-CHECKED", "USEFULNESS-CHECKED")
APPROVAL_TYPES: tuple[str, ...] = ("EXPERIMENTAL", "PRODUCTION")


class GovernanceAttributeError(Exception):
    """Raised when feature-version governance attributes are not well-formed (§3.8)."""


@dataclass(frozen=True, slots=True)
class GovernanceAttributes:
    """Typed §3.8 governance slots on a feature_version. SP-0 owns the slots; the values/
    thresholds (risk-tier meaning, use-case matrices, required stamp) are policy (SP-9/10/12)."""

    feature_version_id: str
    feature_id: str
    produced_by_run: str
    verification_stamp: str  # DESIGN-CHECKED | DATA-CHECKED | USEFULNESS-CHECKED (a mint is never UNVERIFIED)
    risk_tier: str  # free string; ordering/ceiling is policy
    approval_type: str  # EXPERIMENTAL | PRODUCTION
    base_feature_version_id: str | None = None
    approved_use_cases: tuple[str, ...] = ()
    blocked_use_cases: tuple[str, ...] = ()
    required_artifact_refs: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: str | None = None
    conditions: tuple[str, ...] = ()
    expires_at: datetime | None = None
    max_uses: int | None = None
    reviewed_evidence_refs: tuple[str, ...] = ()
    immutable: bool = True


def validate_governance_attributes(attrs: GovernanceAttributes) -> None:
    for name in ("feature_version_id", "feature_id", "produced_by_run", "risk_tier"):
        if not getattr(attrs, name):
            raise GovernanceAttributeError(f"{name} is required")
    if attrs.verification_stamp not in VERIFICATION_STAMPS:
        raise GovernanceAttributeError(
            f"verification_stamp {attrs.verification_stamp!r} not in {VERIFICATION_STAMPS}"
        )
    if attrs.approval_type not in APPROVAL_TYPES:
        raise GovernanceAttributeError(
            f"approval_type {attrs.approval_type!r} not in {APPROVAL_TYPES}"
        )
    if attrs.max_uses is not None and attrs.max_uses <= 0:
        raise GovernanceAttributeError("max_uses must be None or a positive integer")


def to_feature_version_row(attrs: GovernanceAttributes, *, content_hash: str) -> dict[str, object]:
    approval = {
        "conditions": list(attrs.conditions),
        "expires_at": attrs.expires_at.isoformat() if attrs.expires_at else None,
        "max_uses": attrs.max_uses,
        "reviewed_evidence_refs": list(attrs.reviewed_evidence_refs),
    }
    return {
        "feature_version_id": attrs.feature_version_id,
        "feature_id": attrs.feature_id,
        "produced_by_run": attrs.produced_by_run,
        "base_feature_version_id": attrs.base_feature_version_id,
        "verification_stamp": attrs.verification_stamp,
        "risk_tier": attrs.risk_tier,
        "approval_type": attrs.approval_type,
        "approved_use_cases": list(attrs.approved_use_cases),
        "blocked_use_cases": list(attrs.blocked_use_cases),
        "required_artifact_refs": Json(dict(attrs.required_artifact_refs)),
        "dsl_operation_catalog_version": attrs.dsl_operation_catalog_version,
        "approval": Json(approval),
        "expires_at": attrs.expires_at,
        "content_hash": content_hash,
        "immutable": attrs.immutable,
    }


def from_feature_version_row(row: Mapping[str, object]) -> GovernanceAttributes:
    approval = dict(row.get("approval") or {})
    return GovernanceAttributes(
        feature_version_id=str(row["feature_version_id"]),
        feature_id=str(row["feature_id"]),
        produced_by_run=str(row["produced_by_run"]),
        base_feature_version_id=row.get("base_feature_version_id") or None,  # type: ignore[arg-type]
        verification_stamp=str(row["verification_stamp"]),
        risk_tier=str(row["risk_tier"]),
        approval_type=str(row["approval_type"]),
        approved_use_cases=tuple(row.get("approved_use_cases") or ()),
        blocked_use_cases=tuple(row.get("blocked_use_cases") or ()),
        required_artifact_refs=dict(row.get("required_artifact_refs") or {}),
        dsl_operation_catalog_version=row.get("dsl_operation_catalog_version") or None,  # type: ignore[arg-type]
        conditions=tuple(approval.get("conditions") or ()),
        expires_at=row.get("expires_at") or None,  # type: ignore[arg-type]
        max_uses=approval.get("max_uses"),
        reviewed_evidence_refs=tuple(approval.get("reviewed_evidence_refs") or ()),
        immutable=bool(row["immutable"]),
    )
