from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Protocol, runtime_checkable

from featuregen.governance.attributes import GovernanceAttributes
from featuregen.governance.predicates import (
    required_artifact_present,
    risk_tier_within_ceiling,
    use_case_not_blocked,
    verification_stamp_satisfies,
)


@dataclass(frozen=True, slots=True)
class GuardFailure:
    """An evaluated §3.8 guard that REJECTED an activation/supersession. Carries the guard name
    plus the resolved inputs + boolean result so the emitted ACTIVATION_BLOCKED event is fully
    auditable (§4.1 symmetric typing & audit)."""
    guard: str
    inputs: Mapping[str, object]
    result: bool = False


@runtime_checkable
class ActivationPolicy(Protocol):
    """The injected policy hook (§3.8): SP-0 owns the guard *mechanism*; the policy supplies the
    *thresholds* (required stamp for production, per-use-case risk ceiling, required artifacts).
    Returns the first GuardFailure or None when the policy-parameterized guards all pass.
    The intrinsic `use_case_not_blocked` guard is enforced by SP-0 independently of any policy."""

    def evaluate(
        self, attrs: GovernanceAttributes, *, use_case: str, approval_type: str
    ) -> Optional[GuardFailure]: ...


@dataclass(frozen=True, slots=True)
class StandardActivationPolicy:
    """A configurable policy that wires the §3.8 predicates to declared thresholds. The default
    instance is PERMISSIVE (no thresholds set) — SP-0 ships the slots/hooks; SP-9/10/12 inject a
    populated policy. Each parameter, when set, activates one parameterized guard:
      - `production_required_stamp` -> verification_stamp_satisfies (only for PRODUCTION promotion)
      - `required_artifacts`        -> required_artifact_present     (only for PRODUCTION promotion)
      - `use_case_risk_ceiling` + `risk_tier_ranks` -> risk_tier_within_ceiling
    """
    production_required_stamp: Optional[str] = None
    required_artifacts: tuple[str, ...] = ()
    use_case_risk_ceiling: Mapping[str, str] = field(default_factory=dict)
    risk_tier_ranks: Mapping[str, int] = field(default_factory=dict)

    def evaluate(
        self, attrs: GovernanceAttributes, *, use_case: str, approval_type: str
    ) -> Optional[GuardFailure]:
        if approval_type == "PRODUCTION" and self.production_required_stamp is not None:
            inputs = {
                "verification_stamp": attrs.verification_stamp,
                "required_stamp": self.production_required_stamp,
            }
            if not verification_stamp_satisfies(inputs):
                return GuardFailure("verification_stamp_satisfies", inputs)
            for artifact_name in self.required_artifacts:
                inputs = {
                    "required_artifact_refs": attrs.required_artifact_refs,
                    "artifact_name": artifact_name,
                }
                if not required_artifact_present(inputs):
                    return GuardFailure("required_artifact_present", inputs)
        ceiling = self.use_case_risk_ceiling.get(use_case)
        if ceiling is not None:
            inputs = {
                "risk_tier_rank": self.risk_tier_ranks[attrs.risk_tier],
                "ceiling_rank": self.risk_tier_ranks[ceiling],
            }
            if not risk_tier_within_ceiling(inputs):
                return GuardFailure("risk_tier_within_ceiling", inputs)
        return None


# Permissive default: parameterized guards are off until a policy is injected. The intrinsic
# use_case_not_blocked guard is enforced regardless (see evaluate_activation_guards).
DEFAULT_ACTIVATION_POLICY = StandardActivationPolicy()


def evaluate_activation_guards(
    attrs: GovernanceAttributes, *, use_case: str, approval_type: str,
    policy: Optional[ActivationPolicy] = None,
) -> Optional[GuardFailure]:
    """Evaluate the §3.8 activation guards for `attrs` activating INTO `use_case`. The intrinsic
    `use_case_not_blocked` guard is always enforced (SP-0-owned, no policy); the policy-
    parameterized guards run via the injected hook. Returns the first GuardFailure, else None."""
    blocked_inputs = {"target_use_case": use_case, "blocked_use_cases": attrs.blocked_use_cases}
    if not use_case_not_blocked(blocked_inputs):
        return GuardFailure("use_case_not_blocked", blocked_inputs)
    active_policy = policy if policy is not None else DEFAULT_ACTIVATION_POLICY
    return active_policy.evaluate(attrs, use_case=use_case, approval_type=approval_type)
