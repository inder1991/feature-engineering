from __future__ import annotations

from enum import Enum

# NewDocument is a SHARED contract type (single source of truth). Phase 01 placed the
# canonical frozen/slots dataclass in sp0.contracts.envelopes; this module re-exports that
# SAME class object so `sp0.contracts.documents.NewDocument is sp0.contracts.envelopes.NewDocument`
# (a divergent copy would break isinstance/identity across phases). Phase 02 owns the
# stage/artifact enum + branch-role / classification vocabularies below.
from sp0.contracts.envelopes import NewDocument

__all__ = [
    "NewDocument",
    "Stage",
    "STAGES",
    "BRANCH_ROLES",
    "BODY_CLASSIFICATIONS",
]


class Stage(str, Enum):
    """Normatively published stage/artifact enum (§3.7)."""

    DRAFT_CONTRACT = "DRAFT_CONTRACT"
    ASSUMPTION_LEDGER = "ASSUMPTION_LEDGER"
    CONFIRMED_CONTRACT = "CONFIRMED_CONTRACT"
    MAPPED_CONTRACT = "MAPPED_CONTRACT"
    FEATURE_PLAN = "FEATURE_PLAN"
    CANDIDATE_SQL = "CANDIDATE_SQL"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    SANDBOX_RESULT = "SANDBOX_RESULT"
    DQ_REPORT = "DQ_REPORT"
    EVALUATION_REPORT = "EVALUATION_REPORT"
    RISK_ASSESSMENT = "RISK_ASSESSMENT"
    EXPLAINABILITY = "EXPLAINABILITY"
    MONITORING_SPEC = "MONITORING_SPEC"
    APPROVAL_RECORD = "APPROVAL_RECORD"


STAGES: tuple[str, ...] = tuple(s.value for s in Stage)
BRANCH_ROLES: tuple[str, ...] = ("candidate", "primary", "rejected", "repair")
BODY_CLASSIFICATIONS: tuple[str, ...] = ("pii-erasable", "governance-retained")
