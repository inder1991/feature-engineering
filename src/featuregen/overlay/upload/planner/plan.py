"""Phase-3B.3a A5 — the per-recipe orchestrator: discover -> enumerate -> order across the frozen scope's
catalogs, classify the result by candidate-local-first precedence, compute the ground_template differential,
and build the replay envelope. 3B.3b (B5) wires the cross-catalog assembler in as a LOG-ONLY enrichment:
the source->target roll-up plans (complete + fail-closed rejected) are ADDED to candidate_plans and the
governed crossing set is pinned on the envelope, while the tier-1 result classification is byte-identical.
3B.3c (C8) adds the equally log-only contract-compile pass: when the caller supplies a batched
``CompilerContext`` (+ the run-owned ``CompileBudget``), each source->target-resolved plan is compiled
in place and the result carries the contract selection roll-up — tier-1 classification, plan order and
physical ids untouched. Read-only, deterministic."""
from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.bridge_candidates import BRIDGE_DERIVATION_VERSION
from featuregen.overlay.upload.bridge_projection import active_bridges
from featuregen.overlay.upload.catalog_realizations import REALIZATION_DERIVATION_VERSION, table_of
from featuregen.overlay.upload.need_metadata import NEED_METADATA_VERSION
from featuregen.overlay.upload.planner.assembly import (
    _authority_rank_lookup,
    _Position,
    _rank_key,
    assemble_paths,
    ingredient_eligibility,
    semantic_rollup_paths,
)
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import (
    APPLICABILITY_MAPPING_VERSION,
    CONCEPT_REGISTRY_VERSION,
    PLAN_CONTRACT_VERSION,
    PLANNER_VERSION,
    REASON_CODE_REGISTRY_VERSION,
    RECIPE_REGISTRY_VERSION,
    BindingPlanningResultV1,
    BindingPlanV1,
    BoundingMetricsV1,
    CatalogScopeV1,
    ContractResolutionStatus,
    GroundTemplateDiffOutcome,
    GroundTemplateDiffV1,
    PathResolutionStatus,
    PlannerReplayEnvelopeV1,
    PlanResolutionStatus,
    ReasonCode,
    ReplayStrength,
    canonical_reason_codes,
)
from featuregen.overlay.upload.planner.declarations import (
    CompileBudget,
    CompilerContext,
    compile_contract,
)
from featuregen.overlay.upload.planner.enumerate import enumerate_single_catalog_plans
from featuregen.overlay.upload.planner.order import order_plans
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility
from featuregen.overlay.upload.templates import Template, ground_template


def _envelope(conn, scope: CatalogScopeV1, recipe_id: str,
              target_entity: str | None) -> PlannerReplayEnvelopeV1:
    material = (f"{PLANNER_VERSION}|{scope.scope_id}|{recipe_id}|{target_entity or ''}|{NEED_METADATA_VERSION}"
               f"|{GRAPH_VERSION}|{REALIZATION_DERIVATION_VERSION}|{RECIPE_REGISTRY_VERSION}"
               f"|{APPLICABILITY_MAPPING_VERSION}|{CONCEPT_REGISTRY_VERSION}")
    # 3B.3b (B5): pin the EXACT governed crossing set this run could see — the VERIFIED bridges whose
    # BOTH endpoint catalogs are inside the frozen scope. An out-of-scope endpoint keeps the bridge out
    # of the envelope exactly as it is out of the search (fail-closed, never revealed). conn=None is the
    # shadow fallback path (run_shadow_planner's per-recipe except): nothing was planned there, so an
    # empty crossing set is recorded without another DB read that could break per-recipe isolation.
    allowed = set(scope.authorized_catalog_sources)
    bridge_keys: tuple[str, ...] = () if conn is None else tuple(sorted(
        b.fact_key for b in active_bridges(conn)
        if b.left_catalog_source in allowed and b.right_catalog_source in allowed))
    return PlannerReplayEnvelopeV1(
        planner_version=PLANNER_VERSION, reason_code_registry_version=REASON_CODE_REGISTRY_VERSION,
        applicability_mapping_version=APPLICABILITY_MAPPING_VERSION, recipe_registry_version=RECIPE_REGISTRY_VERSION,
        need_metadata_version=NEED_METADATA_VERSION, graph_version=GRAPH_VERSION,
        realization_derivation_version=REALIZATION_DERIVATION_VERSION,
        bridge_derivation_version=BRIDGE_DERIVATION_VERSION, concept_registry_version=CONCEPT_REGISTRY_VERSION,
        catalog_scope=scope, replay_strength=ReplayStrength.conditional,
        planner_input_hash="ph_" + hashlib.sha256(material.encode()).hexdigest()[:24],
        active_bridge_fact_keys=bridge_keys, plan_contract_version=PLAN_CONTRACT_VERSION)


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
                    return GroundTemplateDiffV1(outcome, live_refs, p.physical_plan_id)
        return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_binding_absent_unexpectedly, live_refs, None)
    return GroundTemplateDiffV1(GroundTemplateDiffOutcome.live_path_had_no_binding, (), None)


def plan_bindings(conn, *, template: Template, target_entity: str | None, scope: CatalogScopeV1,
                  roles: Iterable[str] = (), now: datetime,
                  compile_ctx: CompilerContext | None = None,
                  budget: CompileBudget | None = None) -> BindingPlanningResultV1:
    roles = tuple(roles)
    envelope = _envelope(conn, scope, template.id, target_entity)
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
    diff = _differential(conn, template, ordered.plans, scope, roles, now)

    # 3B.3b (B5) — the LOG-ONLY cross-catalog enrichment. Candidate-local-first: the assembler's output
    # only APPENDS to candidate_plans (a rejected roll-up never downgrades a resolved tier-1 result; a
    # resolved roll-up never overrides the tier-1 selection). result_status/selected_plan_id below are
    # computed from the tier-1 plans EXACTLY as before; an assembler DB error propagates to the
    # per-recipe savepoint in run_shadow_planner (never hidden by a bare except).
    asm = _assemble_rollups(conn, template=template, target_entity=target_entity, scope=scope,
                            resolved_plans=resolved)
    candidate_plans = ordered.plans + asm.plans
    bounding = BoundingMetricsV1(cols_trunc, combos_trunc, plans_trunc, scope.catalog_consideration_truncated,
                                 total_cols, total_combos, len(candidate_plans),
                                 realizations_truncated=asm.realizations_truncated,
                                 bridge_transitions_truncated=asm.bridge_transitions_truncated,
                                 frontier_states_truncated=asm.frontier_states_truncated,
                                 deeper_tiers_not_explored=asm.deeper_tiers_not_explored,
                                 total_states_expanded=asm.total_states_expanded,
                                 total_bridge_transitions_explored=asm.total_bridge_transitions_explored)

    if resolved:
        status = PlanResolutionStatus.resolved
        selected = resolved[0].physical_plan_id
        reasons = ((ReasonCode.selected_best_single_catalog,)
                   + ((ReasonCode.ambiguous_multiple_equal_plans,) if ordered.ambiguous else ()))
        primary = ReasonCode.selected_best_single_catalog
    else:
        selected = None
        status, primary = _classify_failure(ordered.plans, bounding)
        reasons = (primary,) if primary else ()

    # 3B.3c (C8) — the SHADOW contract-compile pass + the contract-axis roll-up. Gated on the
    # caller-provided batched context (the route kill-switch): compile_ctx=None is byte-identical
    # to the pre-C8 path — no compile, no roll-up, zero extra reads. Only source_to_target_resolved
    # plans compile; the tier-1 result_status/selected_plan_id decision above is UNTOUCHED
    # (candidate-local-first), and physical ids/order are immutable through compilation.
    contract_status = ContractResolutionStatus.not_compiled
    selected_contract_pid: str | None = None
    selected_contract_id: str | None = None
    if compile_ctx is not None:
        candidate_plans = tuple(
            _compile_or_mark(conn, p, template, compile_ctx, budget, envelope)
            for p in candidate_plans)
        # Contract selection roll-up (F3): the best COMPILED source→target plan by the EXISTING
        # assembly physical ranking key (physical_plan_id tie-break). A budget-skipped or tier-1
        # plan is never eligible — it stayed not_compiled.
        compiled = [p for p in candidate_plans
                    if p.path_resolution_status is PathResolutionStatus.source_to_target_resolved
                    and p.contract_resolution_status is not ContractResolutionStatus.not_compiled]
        if compiled:
            authority = _authority_rank_lookup(conn, compiled)
            best = min(compiled, key=lambda p: (_rank_key(p, authority), p.physical_plan_id))
            contract_status = best.contract_resolution_status
            selected_contract_pid = best.physical_plan_id
            selected_contract_id = best.contract_id

    return BindingPlanningResultV1(
        run_id=None, recipe_id=template.id, target_entity=target_entity, catalog_scope_id=scope.scope_id,
        selected_plan_id=selected, candidate_plans=candidate_plans, result_status=status,
        primary_reason_code=primary, reason_codes=reasons + asm.reason_codes, bounding=bounding,
        ground_template_diff=diff, replay_envelope=envelope,
        contract_result_status=contract_status,
        selected_contract_physical_plan_id=selected_contract_pid,
        selected_contract_id=selected_contract_id)


def _compile_or_mark(conn, plan: BindingPlanV1, template: Template, compile_ctx: CompilerContext,
                     budget: CompileBudget | None,
                     envelope: PlannerReplayEnvelopeV1) -> BindingPlanV1:
    """Compile ONE candidate while the run-owned budget allows it. Non source→target plans pass
    through untouched (never compiled); a plan skipped because the budget (count or the deadline
    over the INJECTED now — deterministic, never wall-clock) is spent stays not_compiled and
    honestly records compile_budget_exhausted — never a silent skip."""
    if plan.path_resolution_status is not PathResolutionStatus.source_to_target_resolved:
        return plan
    if budget is not None and not (budget.remaining > 0 and compile_ctx.now < budget.deadline):
        return replace(plan, contract_reason_codes=canonical_reason_codes(
            plan.contract_reason_codes + (ReasonCode.compile_budget_exhausted,)))
    if budget is not None:
        budget.remaining -= 1
    return compile_contract(conn, compile_ctx, plan, template, base_envelope=envelope)


@dataclass(frozen=True, slots=True)
class _AssemblyRollupsV1:
    """What the 3B.3b assembler contributed to ONE plan_bindings run: the cross-catalog candidate plans
    (complete + fail-closed rejected), result-level reason codes, and the merged assembly bounding —
    flags OR-ed, counts SUMMED across every assemble_paths call."""
    plans: tuple[BindingPlanV1, ...] = ()
    reason_codes: tuple[ReasonCode, ...] = ()
    realizations_truncated: bool = False
    bridge_transitions_truncated: bool = False
    frontier_states_truncated: bool = False
    deeper_tiers_not_explored: bool = False
    total_states_expanded: int = 0
    total_bridge_transitions_explored: int = 0


def _assemble_rollups(conn, *, template: Template, target_entity: str | None, scope: CatalogScopeV1,
                      resolved_plans: Sequence[BindingPlanV1]) -> _AssemblyRollupsV1:
    """Run the 3B.3b assembler for the recipe's source->target roll-up: one bounded frontier search per
    (deduped tier-1 source binding x governed semantic path). Only RESOLVED tier-1 plans supply source
    bindings, so nothing here ever runs on a recipe whose tier-1 outcome was not resolved."""
    elig = ingredient_eligibility(template)
    if elig.reason is ReasonCode.unsupported_multi_grain_ingredients:
        # the recipe cannot do a cross-catalog roll-up at all (a REQUIRED second-entity grain) —
        # recorded on the result, never attempted; the tier-1 classification is otherwise untouched.
        return _AssemblyRollupsV1(reason_codes=(ReasonCode.unsupported_multi_grain_ingredients,))
    if not elig.eligible or elig.source_entity is None or target_entity is None:
        return _AssemblyRollupsV1()     # no single governed source grain (or no target grain): tier-1 stands
    paths, status = semantic_rollup_paths(elig.source_entity, target_entity)
    if status is EntityCompatibility.EXACT:
        return _AssemblyRollupsV1()     # tier-1 already binds AT target grain — the zero-hop case is tier-1's
    if status not in (EntityCompatibility.DERIVABLE, EntityCompatibility.AMBIGUOUS):
        return _AssemblyRollupsV1()     # UNKNOWN: no governed path -> no roll-up to attempt, no reason minted
    reasons = ((ReasonCode.ambiguous_semantic_path,)
               if status is EntityCompatibility.AMBIGUOUS else ())

    source_role = str(JoinRole.SOURCE_ENTITY_KEY)   # bindings store join_role as its string value
    seen_positions: set[tuple[str, str]] = set()
    seen_plan_ids: set[str] = set()
    plans: list[BindingPlanV1] = []
    r_trunc = b_trunc = f_trunc = deeper = False
    states = transitions = 0
    for plan in resolved_plans:                     # deterministic: order_plans rank order
        for sb in plan.ingredient_bindings:
            if sb.join_role != source_role:
                continue
            pos_key = (sb.bound_catalog_source, sb.bound_object_ref)
            if pos_key in seen_positions:           # the same starting position is never searched twice
                continue
            seen_positions.add(pos_key)
            position = _Position(elig.source_entity, sb.bound_catalog_source,
                                 table_of(sb.bound_object_ref))
            # Known shadow-phase limitation for 3B.4 (B4 review M1): the frontier's visited key excludes
            # hop_index, so two distinct complete paths converging on the identical
            # (entity, catalog, table, used-bridges) state keep only the first.
            for path in paths:
                one = assemble_paths(conn, source_position=position, semantic_path=path, scope=scope,
                                     ingredient_bindings=plan.ingredient_bindings, template=template,
                                     target_entity=target_entity)
                r_trunc |= one.bounding.realizations_truncated
                b_trunc |= one.bounding.bridge_transitions_truncated
                f_trunc |= one.bounding.frontier_states_truncated
                deeper |= one.bounding.deeper_tiers_not_explored
                states += one.bounding.total_states_expanded
                transitions += one.bounding.total_bridge_transitions_explored
                for p in one.complete + one.rejected:
                    if p.physical_plan_id not in seen_plan_ids:   # defensive: an identical mint is appended once
                        seen_plan_ids.add(p.physical_plan_id)
                        plans.append(p)
    return _AssemblyRollupsV1(tuple(plans), reasons, r_trunc, b_trunc, f_trunc, deeper,
                              states, transitions)


def _classify_failure(plans, bounding):
    # Precedence: bounded_out > partially_resolved > unresolved. safety_rejected can't occur in tier-1
    # (unsafe columns are filtered pre-enumeration); safety_rejected/staging precedence is a 3B.3c concern.
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
        bounding=BoundingMetricsV1(False, False, False, scope.catalog_consideration_truncated, 0, 0, 0,
                                   realizations_truncated=False, bridge_transitions_truncated=False,
                                   frontier_states_truncated=False, deeper_tiers_not_explored=False,
                                   total_states_expanded=0, total_bridge_transitions_explored=0),
        ground_template_diff=GroundTemplateDiffV1(GroundTemplateDiffOutcome.not_compared, (), None),
        replay_envelope=envelope)
