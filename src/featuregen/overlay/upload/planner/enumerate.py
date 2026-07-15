"""Phase-3B.3a A3 — bounded single-catalog plan enumeration. The cartesian product of ELIGIBLE per-need
candidates into tier-1 BindingPlanV1s, bounded + deterministic. A plan binding every REQUIRED need is
`resolved` (pre-ranking); otherwise `partially_resolved`. Ranking is Task 4."""
from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass

from featuregen.overlay.upload.planner.candidates import CandidateDiscoveryV1
from featuregen.overlay.upload.planner.contracts import (
    MAX_PARTIAL_COMBINATIONS,
    MAX_PLANS_PER_RECIPE,
    PLANNER_VERSION,
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingSafety,
    IngredientBindingV1,
    IngredientCandidateV1,
    PlanResolutionStatus,
    PlanTier,
    ReasonCode,
    SegmentKind,
)
from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class EnumerationV1:
    plans: tuple[BindingPlanV1, ...]
    combinations_truncated: bool
    plans_truncated: bool
    total_combinations_explored: int


def _binding(c: IngredientCandidateV1) -> IngredientBindingV1:
    return IngredientBindingV1(
        recipe_id=c.recipe_id, need_role=c.need_role, concept=c.concept, required_grains=c.required_grains,
        join_role=c.join_role, temporal_role=c.temporal_role, bound_catalog_source=c.catalog_source,
        bound_object_ref=c.object_ref, actual_source_grain=c.actual_source_grain,
        binding_quality=c.binding_quality, safety=c.safety, reason_codes=c.reason_codes)


def _plan_id(recipe_id: str, catalog: str, refs: tuple[str, ...]) -> str:
    material = f"{recipe_id}|{catalog}|{'|'.join(sorted(refs))}|{PlanTier.tier_1_single_catalog}|{PLANNER_VERSION}"
    return "bp_" + hashlib.sha256(material.encode()).hexdigest()[:16]


def enumerate_single_catalog_plans(template: Template, catalog_source: str, target_entity: str | None,
                                   discovery: CandidateDiscoveryV1) -> EnumerationV1:
    required = [n.role for n in template.needs if not n.optional]
    optional = [n.role for n in template.needs if n.optional]
    # one axis per REQUIRED need = its eligible candidates; a required need with no eligible candidate
    # still yields a single partial "axis" (None) so the plan is preserved as partially_resolved.
    # NOTE: bounded-product truncation determinism DEPENDS on candidates.py pre-sorting each need's
    # candidates by object_ref — no per-axis re-sort happens here.
    axes: list[tuple[str, tuple[IngredientCandidateV1 | None, ...]]] = []
    for role in required:
        eligible = tuple(c for c in discovery.candidates.get(role, ()) if c.eligible)
        axes.append((role, eligible if eligible else (None,)))
    # deterministic optional bindings: at most the single best-ordered eligible candidate per optional need
    opt_bindings: list[IngredientCandidateV1] = []
    for role in optional:
        elig = [c for c in discovery.candidates.get(role, ()) if c.eligible]
        if elig:
            opt_bindings.append(sorted(elig, key=lambda c: c.object_ref)[0])

    combos = 1
    for _role, cs in axes:
        combos *= max(1, len(cs))
    combinations_truncated = combos > MAX_PARTIAL_COMBINATIONS

    plans: list[BindingPlanV1] = []
    explored = 0
    for combo in itertools.product(*[cs for _r, cs in axes]):
        if explored >= MAX_PARTIAL_COMBINATIONS:
            combinations_truncated = True
            break
        explored += 1
        bound = [c for c in combo if c is not None] + opt_bindings
        missing_required = any(c is None for c in combo)
        bindings = tuple(_binding(c) for c in sorted(bound, key=lambda c: c.need_role))
        refs = tuple(b.bound_object_ref for b in bindings)
        status = (PlanResolutionStatus.partially_resolved if missing_required
                  else PlanResolutionStatus.resolved)
        reasons = (ReasonCode.missing_required_need,) if missing_required else ()
        plans.append(BindingPlanV1(
            plan_id=_plan_id(template.id, catalog_source, refs), recipe_id=template.id,
            target_entity=target_entity, tier=PlanTier.tier_1_single_catalog, catalog_source=catalog_source,
            ingredient_bindings=bindings,
            path_segments=(BindingPathSegmentV1(segment_kind=SegmentKind.direct_catalog,
                                                catalog_source=catalog_source),),
            resolution_status=status, primary_reason_code=(reasons[0] if reasons else None),
            reason_codes=reasons, safety=BindingSafety.safe, preference_rank=-1, preference_reasons=()))

    plans_truncated = len(plans) > MAX_PLANS_PER_RECIPE
    if plans_truncated:
        plans = sorted(plans, key=lambda p: p.plan_id)[:MAX_PLANS_PER_RECIPE]
    return EnumerationV1(plans=tuple(plans), combinations_truncated=combinations_truncated,
                         plans_truncated=plans_truncated, total_combinations_explored=explored)
