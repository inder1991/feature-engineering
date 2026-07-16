"""Phase-3B.3a A5 — the log-only shadow entry. Resolves the scope ONCE, plans each eligible recipe,
logs the result. Never alters the considered set. A planner error is isolated per recipe.
3B.3c (C8): when ``compile_contracts`` is on, builds ONE batched ``CompilerContext`` per run and owns
the MUTABLE ``CompileBudget`` that persists ACROSS recipes (F10) — the operational guard that bounds
the compile pass's extra reads. The flag is read in the route, never here (the planner stays pure)."""
from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable
from datetime import datetime, timedelta

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanningResultV1,
    BoundingMetricsV1,
    GroundTemplateDiffOutcome,
    GroundTemplateDiffV1,
    PlanResolutionStatus,
    ReasonCode,
)
from featuregen.overlay.upload.planner.declarations import CompileBudget, build_compiler_context
from featuregen.overlay.upload.planner.plan import _envelope, plan_bindings
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Template

logger = logging.getLogger(__name__)

# C8 run-budget defaults (§11): the compile pass is bounded per RUN — a plan count and a deadline.
# The deadline is derived from the INJECTED `now` (never a wall-clock read in the deterministic
# path), so it bounds runs whose caller-supplied clock advances, and tests can inject a tiny one.
MAX_COMPILES_PER_RUN = 500
COMPILE_BUDGET = timedelta(seconds=30)


def run_shadow_planner(conn, *, eligible_recipe_ids: frozenset[str], target_entity: str | None,
                       roles: Iterable[str] = (), run_id: str | None, now: datetime,
                       templates: tuple[Template, ...] | None = None,
                       compile_contracts: bool = False) -> tuple[BindingPlanningResultV1, ...]:
    roles = tuple(roles)
    scope = resolve_catalog_scope(conn, roles=roles, target_entity=target_entity, now=now)
    compile_ctx = None
    budget: CompileBudget | None = None
    if compile_contracts:
        # ONE immutable context per run (no per-plan re-query) + the run-owned mutable budget,
        # threaded into EVERY plan_bindings call so it persists across recipes (F10).
        compile_ctx = build_compiler_context(conn, scope, roles, now)
        budget = CompileBudget(remaining=MAX_COMPILES_PER_RUN, deadline=now + COMPILE_BUDGET)
    by_id = {t.id: t for t in (templates if templates is not None else ALL_TEMPLATES)}
    results: list[BindingPlanningResultV1] = []
    for rid in sorted(eligible_recipe_ids):
        tmpl = by_id.get(rid)
        if tmpl is None:
            continue
        try:
            with conn.transaction():   # per-recipe savepoint — a DB error here must not poison the request txn
                result = plan_bindings(conn, template=tmpl, target_entity=target_entity, scope=scope,
                                       roles=roles, now=now, compile_ctx=compile_ctx, budget=budget)
            result = dataclasses.replace(result, run_id=run_id)
        except Exception:   # planner failure is isolated per recipe; never touches the response
            logger.exception("shadow planner internal error for recipe %s", rid)
            result = BindingPlanningResultV1(
                run_id=run_id, recipe_id=rid, target_entity=target_entity, catalog_scope_id=scope.scope_id,
                selected_plan_id=None, candidate_plans=(), result_status=PlanResolutionStatus.internal_error,
                primary_reason_code=ReasonCode.planner_internal_error,
                reason_codes=(ReasonCode.planner_internal_error,),
                bounding=BoundingMetricsV1(False, False, False, False, 0, 0, 0,   # zero — nothing was explored
                                           realizations_truncated=False, bridge_transitions_truncated=False,
                                           frontier_states_truncated=False, deeper_tiers_not_explored=False,
                                           total_states_expanded=0, total_bridge_transitions_explored=0),
                ground_template_diff=GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None),
                # conn=None: the fallback envelope records an EMPTY crossing set without another DB read
                # (nothing was planned; a read here could itself fail and break per-recipe isolation).
                replay_envelope=_envelope(None, scope, rid, target_entity))
        logger.info("shadow_binding_plan recipe=%s status=%s selected=%s scope=%s"
                    " contract_status=%s contract_selected=%s",
                    result.recipe_id, result.result_status, result.selected_plan_id, scope.scope_id,
                    result.contract_result_status, result.selected_contract_physical_plan_id)
        results.append(result)
    if budget is not None:
        # The §11 run metric — derived from the budget alone (no wall-clock read): how many plans
        # compiled and whether either bound was hit (a hit means some plan recorded
        # compile_budget_exhausted, or the whole run started past its deadline).
        compiles = MAX_COMPILES_PER_RUN - budget.remaining
        budget_hit = budget.remaining <= 0 or now >= budget.deadline
        logger.info("shadow_contract_compile_run run=%s compiles=%d budget_hit=%s remaining=%d",
                    run_id, compiles, budget_hit, budget.remaining)
    return tuple(results)
