"""Phase 3C.2b-i-B · Task 6 — the refined computation-role policy.

Once T5 resolves a column's authoritative ``concept``, B must decide what COMPUTATION ROLE that
concept can play: MEASURE (an aggregatable numeric operand), TIME (a governed time anchor), COUNTED
(an entity identifier for count/count-distinct), or an explicit rejection. This is a PURE, versioned
policy over the ``Concept`` record — no DB, no data plane. It is TOTAL over every concept ``group``
(all 19, enumerated below — no silent fall-through).

THE TRAP (confirmed against the registry): ``group`` alone is NOT sufficient for MEASURE.
``impairment_stage`` (group=accounting, additivity="n/a", an ordinal) and
``green_flag``/``sharia_compliant_flag``/``deforestation_flag`` (group=esg, additivity="n/a",
boolean flags) would be wrongly promoted to MEASURE by a naive group->MEASURE table. Likewise
``group == "temporal"`` over-includes: 6 of 20 temporal concepts (``duration_tenure``, ``vintage``,
``tenor``, ``business_day_convention``, ``settlement_cycle``, ``effective_maturity``) carry
``pit_role == "none"`` and are NOT time anchors. So: MEASURE gates on ``additivity``, TIME gates on
``pit_role`` — NOT on ``group`` alone.

Reuses (does NOT redefine) A's ``SemanticRole`` (``multisource_contracts.py``) and B's
``ROLE_POLICY_VERSION``/``BDisposition.role_not_aggregatable`` (``b_dispositions.py``)."""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.concepts import Concept
from featuregen.overlay.upload.planner.b_dispositions import ROLE_POLICY_VERSION, BDisposition
from featuregen.overlay.upload.planner.multisource_contracts import SemanticRole

__all__ = [
    "ROLE_POLICY_VERSION",
    "MEASURE_ELIGIBLE_GROUPS",
    "TIME_ANCHOR_PIT_ROLES",
    "NON_COMPUTATIONAL_GROUPS",
    "RolePolicyReason",
    "RolePolicyRejection",
    "computation_role",
    "reason_to_b_disposition",
]

# ---------------------------------------------------------------------------
# The closed vocabularies (confirmed exhaustive against the registry) — module constants so the
# enumeration is auditable and the totality test can reference them directly.
# ---------------------------------------------------------------------------

# The 6 groups whose concepts are candidate numeric measures — gated further by ``additivity``.
MEASURE_ELIGIBLE_GROUPS: frozenset[str] = frozenset({
    "monetary", "quantity_risk", "accounting", "regulatory_capital", "esg", "crypto",
})

# The 6 accepted time-anchor pit_role values; "none" is the sentinel for "not a time anchor".
TIME_ANCHOR_PIT_ROLES: frozenset[str] = frozenset({
    "as_of", "effective", "event", "maturity", "valid_time", "system_time",
})

# The 11 remaining groups that are non-computational regardless of additivity — the group
# enumeration is closed; an incidental additivity value must never promote one of these to MEASURE.
NON_COMPUTATIONAL_GROUPS: frozenset[str] = frozenset({
    "categorical", "geographic", "flag", "sensitive", "text", "label", "behavioural",
    "network", "bitemporal", "currency", "eligibility",
})


class RolePolicyReason(StrEnum):
    """The fine-grained rejection reason (kept for telemetry) — all fold to the single coarse
    ``BDisposition.role_not_aggregatable`` bucket via ``reason_to_b_disposition``."""
    additivity_not_asserted = "additivity_not_asserted"
    temporal_not_anchor = "temporal_not_anchor"
    identifier_without_entity_link = "identifier_without_entity_link"
    group_not_computational = "group_not_computational"
    unknown_group = "unknown_group"


@dataclass(frozen=True, slots=True)
class RolePolicyRejection:
    reason: RolePolicyReason


def reason_to_b_disposition(reason: RolePolicyReason) -> BDisposition:
    """Coarse fold: every ``RolePolicyReason`` maps to the single ``role_not_aggregatable``
    disposition. The fine reason is kept on ``RolePolicyRejection`` for telemetry."""
    return BDisposition.role_not_aggregatable


def computation_role(concept: Concept) -> SemanticRole | RolePolicyRejection:
    """Decide the computation role a resolved concept may play. TOTAL, ordered, fail-closed —
    every concept ``group`` is matched by exactly one branch below; an unrecognised group (registry
    drift) falls through to the totality guard and rejects rather than raising or returning None."""
    g = concept.group

    # 1. MEASURE-eligible group: gates on additivity, NOT group alone (the trap).
    if g in MEASURE_ELIGIBLE_GROUPS:
        if concept.additivity != "n/a":
            return SemanticRole.measure
        return RolePolicyRejection(RolePolicyReason.additivity_not_asserted)

    # 2. Temporal: gates on pit_role, NOT group alone (the trap). pit_role="none" is NEVER time;
    #    it falls through to the same additivity check MEASURE-eligible groups use.
    if g == "temporal":
        if concept.pit_role in TIME_ANCHOR_PIT_ROLES:
            return SemanticRole.time
        if concept.additivity != "n/a":
            return SemanticRole.measure
        return RolePolicyRejection(RolePolicyReason.temporal_not_anchor)

    # 3. Identifier: COUNTED iff it links to a governed entity.
    if g == "identifier":
        if concept.entity_link is not None:
            return SemanticRole.counted
        return RolePolicyRejection(RolePolicyReason.identifier_without_entity_link)

    # 4. Every other closed-vocabulary group is non-computational regardless of additivity.
    if g in NON_COMPUTATIONAL_GROUPS:
        return RolePolicyRejection(RolePolicyReason.group_not_computational)

    # 5. Totality guard: registry drift adding a new, unrecognised group. Never raise, never None.
    return RolePolicyRejection(RolePolicyReason.unknown_group)
