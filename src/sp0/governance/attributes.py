from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

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
    verification_stamp: str                         # DESIGN-CHECKED | DATA-CHECKED | USEFULNESS-CHECKED
    risk_tier: str                                  # free string; ordering/ceiling is policy
    approval_type: str                              # EXPERIMENTAL | PRODUCTION
    base_feature_version_id: Optional[str] = None
    approved_use_cases: tuple[str, ...] = ()
    blocked_use_cases: tuple[str, ...] = ()
    required_artifact_refs: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: Optional[str] = None
    conditions: tuple[str, ...] = ()
    expires_at: Optional[datetime] = None
    max_uses: Optional[int] = None
    reviewed_evidence_refs: tuple[str, ...] = ()
    immutable: bool = True


def validate_governance_attributes(attrs: GovernanceAttributes) -> None:
    for name in ("feature_version_id", "feature_id", "produced_by_run", "risk_tier"):
        if not getattr(attrs, name):
            raise GovernanceAttributeError(f"{name} is required")
    if attrs.verification_stamp not in VERIFICATION_STAMPS:
        raise GovernanceAttributeError(f"verification_stamp {attrs.verification_stamp!r} not in {VERIFICATION_STAMPS}")
    if attrs.approval_type not in APPROVAL_TYPES:
        raise GovernanceAttributeError(f"approval_type {attrs.approval_type!r} not in {APPROVAL_TYPES}")
    if attrs.max_uses is not None and attrs.max_uses <= 0:
        raise GovernanceAttributeError("max_uses must be None or a positive integer")
