"""Phase-3B.4 D7 — the FIXED stratum registry (F18).

The 3C statistical bound (§10) is computed PER STRATUM, not over the pooled population — a 1% risk
bound on ``topology_or_model`` rejections says nothing about ``missing_authoring`` ones. So the
population must be partitioned into non-overlapping strata, and that partition must be VERSIONED and
DETERMINISTIC so a signed gate artifact pins the exact frame it was computed over.

``stratum_of`` is a TOTAL, deterministic function over ``(tier × family × primary-dimension)``:
  * ``tier``   — the plan's ``PlanTier`` (single-catalog / one-bridge / multi-bridge): risk grows with
    structural distance.
  * ``family`` — the recipe family (``Template.family``): the semantic shape of the feature.
  * ``dimension`` — the OUTCOME class: ``resolved`` for a clean contract, else the Layer-A
    ``ReasonCategory`` (D5) of the primary reason — so rejections of the same KIND stratify together.

Every observation maps to EXACTLY ONE stratum (non-overlapping by construction). The registry is the
versioned function itself, not an enumerated table (families are open); ``STRATA_VERSION`` is signed
into the gate artifact so a partition change invalidates a prior bound.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.planner.cause import category_of
from featuregen.overlay.upload.planner.contracts import (
    ContractResolutionStatus,
    ReasonCode,
)

STRATA_VERSION = "1.0.0"

_RESOLVED = str(ContractResolutionStatus.resolved)


@dataclass(frozen=True, slots=True)
class StratumId:
    """A single non-overlapping stratum of the sampling frame. ``key`` is the stable, version-stamped
    identity used in the signed gate artifact."""

    tier: str
    family: str
    dimension: str

    @property
    def key(self) -> str:
        return f"{STRATA_VERSION}:{self.tier}:{self.family}:{self.dimension}"


def dimension_of(contract_resolution_status: str, primary_reason_code: str | None) -> str:
    """The OUTCOME dimension: ``resolved`` for a clean contract, else the Layer-A category of the
    primary reason (D5 — exhaustive over the ReasonCode registry, so a mapped code always resolves;
    an unknown string falls to ``operationally_unmeasured``). A rejected contract with no primary
    reason is ``unclassified`` (kept distinct — never silently folded into ``resolved``)."""
    if contract_resolution_status == _RESOLVED:
        return "resolved"
    if primary_reason_code:
        try:
            category = category_of(ReasonCode(primary_reason_code))
        except ValueError:
            return "operationally_unmeasured"    # a reason string outside the registry
        return str(category) if category is not None else "operationally_unmeasured"
    return "unclassified"


def stratum_of(*, tier: str, family: str, contract_resolution_status: str,
               primary_reason_code: str | None) -> StratumId:
    """The TOTAL, deterministic, non-overlapping partition function (F18). Every observation with a
    tier, a family, and a resolution outcome maps to exactly one ``StratumId``."""
    return StratumId(tier=tier, family=family,
                     dimension=dimension_of(contract_resolution_status, primary_reason_code))
