"""Phase-3B.3a A5 — the log-only shadow entry. Resolves the scope ONCE, plans each eligible recipe,
logs the result. Never alters the considered set. A planner error is isolated per recipe."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    BoundingMetricsV1,
    GroundTemplateDiffOutcome,
    GroundTemplateDiffV1,
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.plan import _envelope, plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Template

logger = logging.getLogger(__name__)


def run_shadow_planner(conn, *, eligible_recipe_ids: frozenset[str], target_entity: str | None,
                       roles: Iterable[str] = (), run_id: str | None, now: datetime,
                       templates: tuple[Template, ...] | None = None) -> tuple[BindingPlanningResultV1, ...]:
    roles = tuple(roles)
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target_entity, now=now)
    by_id = {t.id: t for t in (templates if templates is not None else ALL_TEMPLATES)}
    results: list[BindingPlanningResultV1] = []
    for rid in sorted(eligible_recipe_ids):
        tmpl = by_id.get(rid)
        if tmpl is None:
            continue
        try:
            result = plan_bindings(conn, template=tmpl, target_entity=target_entity, scope=scope,
                                   roles=roles, now=now)
            result = dataclasses.replace(result, run_id=run_id)
        except Exception:   # planner failure is isolated per recipe; never touches the response
            logger.exception("shadow planner internal error for recipe %s", rid)
            result = BindingPlanningResultV1(
                run_id=run_id, recipe_id=rid, target_entity=target_entity, catalog_scope_id=scope.scope_id,
                selected_plan_id=None, candidate_plans=(), result_status=PlanResolutionStatus.internal_error,
                primary_reason_code=ReasonCode.planner_internal_error,
                reason_codes=(ReasonCode.planner_internal_error,),
                bounding=BoundingMetricsV1(False, False, False, False, 0, 0, 0),   # zero — nothing was explored
                ground_template_diff=GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None),
                replay_envelope=_envelope(scope, rid, target_entity))
        logger.info("shadow_binding_plan recipe=%s status=%s selected=%s scope=%s",
                    result.recipe_id, result.result_status, result.selected_plan_id, scope.scope_id)
        results.append(result)
    return tuple(results)
