from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts import GuardInputs, GuardPredicate, PredicateRegistry
from featuregen.governance.attributes import VERIFICATION_STAMPS


@dataclass(frozen=True, slots=True)
class _VerificationStampSatisfies:
    name: str = "verification_stamp_satisfies"
    declared_inputs: tuple[str, ...] = ("verification_stamp", "required_stamp")

    def __call__(self, inputs: GuardInputs) -> bool:
        return VERIFICATION_STAMPS.index(inputs["verification_stamp"]) >= VERIFICATION_STAMPS.index(
            inputs["required_stamp"]
        )


@dataclass(frozen=True, slots=True)
class _ApprovalTypeIs:
    name: str = "approval_type_is"
    declared_inputs: tuple[str, ...] = ("approval_type", "expected_approval_type")

    def __call__(self, inputs: GuardInputs) -> bool:
        return inputs["approval_type"] == inputs["expected_approval_type"]


@dataclass(frozen=True, slots=True)
class _UseCaseNotBlocked:
    name: str = "use_case_not_blocked"
    declared_inputs: tuple[str, ...] = ("target_use_case", "blocked_use_cases")

    def __call__(self, inputs: GuardInputs) -> bool:
        return inputs["target_use_case"] not in tuple(inputs["blocked_use_cases"])


@dataclass(frozen=True, slots=True)
class _RequiredArtifactPresent:
    name: str = "required_artifact_present"
    declared_inputs: tuple[str, ...] = ("required_artifact_refs", "artifact_name")

    def __call__(self, inputs: GuardInputs) -> bool:
        refs = inputs["required_artifact_refs"]
        name = inputs["artifact_name"]
        return name in refs and bool(refs[name])


@dataclass(frozen=True, slots=True)
class _RiskTierWithinCeiling:
    name: str = "risk_tier_within_ceiling"
    declared_inputs: tuple[str, ...] = ("risk_tier_rank", "ceiling_rank")

    def __call__(self, inputs: GuardInputs) -> bool:
        return int(inputs["risk_tier_rank"]) <= int(inputs["ceiling_rank"])


@dataclass(frozen=True, slots=True)
class _ApprovalNotExpired:
    name: str = "approval_not_expired"
    declared_inputs: tuple[str, ...] = ("expires_at", "as_of")

    def __call__(self, inputs: GuardInputs) -> bool:
        expires_at = inputs["expires_at"]
        return expires_at is None or inputs["as_of"] <= expires_at


@dataclass(frozen=True, slots=True)
class _MaxUsesNotExceeded:
    name: str = "max_uses_not_exceeded"
    declared_inputs: tuple[str, ...] = ("max_uses", "uses_count")

    def __call__(self, inputs: GuardInputs) -> bool:
        max_uses = inputs["max_uses"]
        return max_uses is None or int(inputs["uses_count"]) < int(max_uses)


verification_stamp_satisfies = _VerificationStampSatisfies()
approval_type_is = _ApprovalTypeIs()
use_case_not_blocked = _UseCaseNotBlocked()
required_artifact_present = _RequiredArtifactPresent()
risk_tier_within_ceiling = _RiskTierWithinCeiling()
approval_not_expired = _ApprovalNotExpired()
max_uses_not_exceeded = _MaxUsesNotExceeded()

GOVERNANCE_PREDICATES: tuple[GuardPredicate, ...] = (
    verification_stamp_satisfies,
    approval_type_is,
    use_case_not_blocked,
    required_artifact_present,
    risk_tier_within_ceiling,
    approval_not_expired,
    max_uses_not_exceeded,
)


def register_governance_predicates(registry: PredicateRegistry) -> None:
    for predicate in GOVERNANCE_PREDICATES:
        registry.register(predicate)
