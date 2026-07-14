"""Phase-3B.3a A5 — the per-recipe orchestrator: discover -> enumerate -> order across the frozen scope's
catalogs, classify the result by candidate-local-first precedence, compute the ground_template differential,
and build the replay envelope. Read-only, deterministic."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable
from datetime import datetime

from featuregen.overlay.upload.bridge_candidates import BRIDGE_DERIVATION_VERSION
from featuregen.overlay.upload.catalog_realizations import REALIZATION_DERIVATION_VERSION
from featuregen.overlay.upload.need_metadata import NEED_METADATA_VERSION
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import (
    APPLICABILITY_MAPPING_VERSION,
    CONCEPT_REGISTRY_VERSION,
    PLANNER_VERSION,
    REASON_CODE_REGISTRY_VERSION,
    RECIPE_REGISTRY_VERSION,
    BindingPlanningResultV1,
    BindingPlanV1,
    BoundingMetricsV1,
    CatalogScopeV1,
    GroundTemplateDiffOutcome,
    GroundTemplateDiffV1,
    PlannerReplayEnvelopeV1,
    PlanResolutionStatus,
    ReasonCode,
    ReplayStrength,
)
from featuregen.overlay.upload.planner.enumerate import enumerate_single_catalog_plans
from featuregen.overlay.upload.planner.order import order_plans
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION
from featuregen.overlay.upload.templates import Template, ground_template

_FAILURE_PRECEDENCE = (PlanResolutionStatus.safety_rejected, PlanResolutionStatus.bounded_out,
                       PlanResolutionStatus.partially_resolved, PlanResolutionStatus.unresolved)


def _envelope(scope: CatalogScopeV1, recipe_id: str, target_entity: str | None) -> PlannerReplayEnvelopeV1:
    material = (f"{PLANNER_VERSION}|{scope.scope_id}|{recipe_id}|{target_entity or ''}|{NEED_METADATA_VERSION}"
               f"|{GRAPH_VERSION}|{REALIZATION_DERIVATION_VERSION}|{RECIPE_REGISTRY_VERSION}"
               f"|{APPLICABILITY_MAPPING_VERSION}|{CONCEPT_REGISTRY_VERSION}")
    return PlannerReplayEnvelopeV1(
        planner_version=PLANNER_VERSION, reason_code_registry_version=REASON_CODE_REGISTRY_VERSION,
        applicability_mapping_version=APPLICABILITY_MAPPING_VERSION, recipe_registry_version=RECIPE_REGISTRY_VERSION,
        need_metadata_version=NEED_METADATA_VERSION, graph_version=GRAPH_VERSION,
        realization_derivation_version=REALIZATION_DERIVATION_VERSION,
        bridge_derivation_version=BRIDGE_DERIVATION_VERSION, concept_registry_version=CONCEPT_REGISTRY_VERSION,
        catalog_scope=scope, replay_strength=ReplayStrength.conditional,
        planner_input_hash="ph_" + hashlib.sha256(material.encode()).hexdigest()[:24])


def _differential(conn, template, plans, scope, roles, now) -> GroundTemplateDiffV1:
    for src in scope.authorized_catalog_sources:
        gf = ground_template(conn, template, catalog_source=src, roles=roles)
        if gf is None:
            continue
        live_refs = tuple(sorted(ref for _s, ref in gf.derives_pairs))
        for p in plans:
            if p.resolution_status is PlanResolutionStatus.resolved and p.catalog_source == src:
                plan_refs = tuple(sorted(b.bound_object_ref for b in p.ingredient_bindings))
                if set(live_refs).issubset(plan_refs):
                    outcome = (GroundTemplateDiffOutcome.live_binding_present_and_ranked_first
                               if p.preference_rank == 0
                               else GroundTemplateDiffOutcome.live_binding_present_but_ranked_lower)
                    return GroundTemplateDiffV1(outcome, live_refs, p.plan_id)
        return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_binding_absent_unexpectedly, live_refs, None)
    return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_path_had_no_binding, (), None)


def plan_bindings(conn, *, template: Template, target_entity: str | None, scope: CatalogScopeV1,
                  roles: Iterable[str] = (), now: datetime) -> BindingPlanningResultV1:
    roles = tuple(roles)
    envelope = _envelope(scope, template.id, target_entity)
    if not scope.authorized_catalog_sources:
        return _empty_result(template.id, target_entity, scope, envelope,
                             PlanResolutionStatus.not_applicable, ReasonCode.no_authorized_catalog)

    all_plans: list[BindingPlanV1] = []
    cols_trunc = combos_trunc = plans_trunc = False
    total_cols = total_combos = 0
    for src in scope.authorized_catalog_sources:
        disc = discover_ingredient_candidates(conn, template, src, roles=roles)
        cols_trunc |= disc.candidate_columns_truncated
        total_cols += disc.total_candidate_columns_considered
        en = enumerate_single_catalog_plans(template, src, target_entity, disc)
        combos_trunc |= en.combinations_truncated
        plans_trunc |= en.plans_truncated
        total_combos += en.total_combinations_explored
        all_plans.extend(en.plans)

    ordered = order_plans(all_plans)
    resolved = [p for p in ordered.plans if p.resolution_status is PlanResolutionStatus.resolved]
    bounding = BoundingMetricsV1(cols_trunc, combos_trunc, plans_trunc, scope.catalog_consideration_truncated,
                                 total_cols, total_combos, len(ordered.plans))
    diff = _differential(conn, template, ordered.plans, scope, roles, now)

    if resolved:
        status = PlanResolutionStatus.resolved
        selected = resolved[0].plan_id
        reasons = ((ReasonCode.selected_best_single_catalog,)
                   + ((ReasonCode.ambiguous_multiple_equal_plans,) if ordered.ambiguous else ()))
        primary = ReasonCode.selected_best_single_catalog
    else:
        selected = None
        status, primary = _classify_failure(ordered.plans, bounding)
        reasons = (primary,) if primary else ()

    return BindingPlanningResultV1(
        run_id=None, recipe_id=template.id, target_entity=target_entity, catalog_scope_id=scope.scope_id,
        selected_plan_id=selected, candidate_plans=ordered.plans, result_status=status,
        primary_reason_code=primary, reason_codes=reasons, bounding=bounding, ground_template_diff=diff,
        replay_envelope=envelope)


def _classify_failure(plans, bounding):
    present = {p.resolution_status for p in plans}
    if bounding.combinations_truncated or bounding.plans_truncated:
        return PlanResolutionStatus.bounded_out, ReasonCode.bounded_out_max_combinations
    if PlanResolutionStatus.partially_resolved in present:
        return PlanResolutionStatus.partially_resolved, ReasonCode.missing_required_need
    return PlanResolutionStatus.unresolved, ReasonCode.no_role_compatible_column


def _empty_result(recipe_id, target_entity, scope, envelope, status, reason):
    return BindingPlanningResultV1(
        run_id=None, recipe_id=recipe_id, target_entity=target_entity, catalog_scope_id=scope.scope_id,
        selected_plan_id=None, candidate_plans=(), result_status=status, primary_reason_code=reason,
        reason_codes=(reason,),
        bounding=BoundingMetricsV1(False, False, False, scope.catalog_consideration_truncated, 0, 0, 0),
        ground_template_diff=GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None),
        replay_envelope=envelope)
