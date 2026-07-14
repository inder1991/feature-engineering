"""Phase-3B.3a A4 — deterministic total ordering + preference ranks + ambiguity. Never incidental order."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanV1,
    BindingQuality,
    PlanResolutionStatus,
)

_STATUS_RANK = {PlanResolutionStatus.resolved: 0, PlanResolutionStatus.partially_resolved: 1}
_QUALITY_RANK = {BindingQuality.grain_and_role_fit: 0, BindingQuality.exact_concept: 1,
                 BindingQuality.entity_tagged: 2, BindingQuality.weak: 3}


@dataclass(frozen=True, slots=True)
class OrderedPlansV1:
    plans: tuple[BindingPlanV1, ...]
    ambiguous: bool


def _first_ref(p: BindingPlanV1) -> str:
    return min((b.bound_object_ref for b in p.ingredient_bindings), default="")


def _agg_quality(p: BindingPlanV1) -> int:
    return max((_QUALITY_RANK[b.binding_quality] for b in p.ingredient_bindings), default=99)


def _key(p: BindingPlanV1) -> tuple:
    return (_STATUS_RANK.get(p.resolution_status, 9), -len(p.ingredient_bindings), _agg_quality(p),
            p.catalog_source, _first_ref(p), p.recipe_id, p.plan_id)


def _tie_key(p: BindingPlanV1) -> tuple:
    k = _key(p)
    return k[:-1]   # everything except the plan_id tiebreak


def order_plans(plans: Sequence[BindingPlanV1]) -> OrderedPlansV1:
    ordered = sorted(plans, key=_key)
    ranked = tuple(replace(p, preference_rank=i,
                           preference_reasons=(f"status={p.resolution_status}",
                                               f"bindings={len(p.ingredient_bindings)}",
                                               f"quality={_agg_quality(p)}", f"catalog={p.catalog_source}"))
                   for i, p in enumerate(ordered))
    resolved = [p for p in ranked if p.resolution_status is PlanResolutionStatus.resolved]
    ambiguous = any(_tie_key(a) == _tie_key(b) for a, b in zip(resolved, resolved[1:]))
    return OrderedPlansV1(plans=ranked, ambiguous=ambiguous)
