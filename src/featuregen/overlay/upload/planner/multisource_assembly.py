"""Phase 3C.2b-i-A · Task 5 — per-operand governed path enumeration (spec §5 steps 1-3, §8).

The first assembly-engine task. It INTEGRATES three pieces that already landed:

* the Task-1 reuse spike (``multisource_reuse``) — the injected single-need ``Template`` +
  hand-built ``_Position`` driven through the EXISTING cross-catalog frontier
  (``semantic_rollup_paths`` -> ``assemble_paths``). Enumeration drives that same engine, but keeps
  EVERY resolved cross-catalog ``BindingPlanV1`` the frontier produces (not just the first, as the
  spike's ``run_operand_rollup`` returns), bounded by ``MAX_PATHS_PER_OPERAND``.
* the Task-4 endpoint check (``multisource_endpoints.governed_endpoint``) — each resolved plan's
  landing table is revalidated against a VERIFIED ``grain`` fact; an ungoverned landing is dropped.
* the Task-2 typed contracts (``multisource_contracts``) — status/reason fields are typed on the
  unified ``MultiSourceReason`` vocabulary (NOT the single-source ``PlanResolutionStatus``), and
  bounds reuse ``MultiSourceBoundingMetricsV1``.

The frontier does NOT emit the landing (``_mint`` discards the landing ``_Position``); A re-derives
the landing ``(catalog, table_ref)`` from the resolved plan's ``path_segments`` exactly the way
``check_connectivity``/``_hop_evidence`` compute execution tables (``declarations.py:191-211``,
``:346-377``): walk the realization/bridge segments in order, tracking the physical target-side
table, and take the LAST one. FAIL CLOSED: an empty result NEVER a bare empty tuple — it carries
``no_governed_path`` (no VERIFIED-bridge path at all) or ``realization_endpoint_ungoverned`` (a
path exists but its landing has no grain fact).

Read-only over the reused frontier + compiler surfaces; nothing here edits ``assemble_paths`` /
``governed_endpoint`` (the §12 behaviour-neutrality invariant).
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.catalog_realizations import table_of
from featuregen.overlay.upload.planner.assembly import (
    _Position,
    assemble_paths,
    semantic_rollup_paths,
)
from featuregen.overlay.upload.planner.contracts import (
    MAX_PATHS_PER_OPERAND,
    BindingPlanV1,
    BindingQuality,
    BindingSafety,
    CatalogScopeV1,
    IngredientBindingV1,
    PlanResolutionStatus,
)
from featuregen.overlay.upload.planner.declarations import CompilerContext
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    MultiSourceBoundingMetricsV1,
    MultiSourceReason,
    OperandSlotV1,
    PathAggregation,
)
from featuregen.overlay.upload.planner.multisource_endpoints import governed_endpoint
from featuregen.overlay.upload.planner.multisource_reuse import injected_operand_template

# The single-source resolution statuses that count as a governed complete path. ``assemble_paths``
# returns ranked/classified plans; a top-rank tie is normalized to ``resolved_with_ambiguity`` —
# still a governed cross-catalog roll-up, so both are enumerated as candidates.
_RESOLVED_STATUSES = frozenset(
    {PlanResolutionStatus.resolved, PlanResolutionStatus.resolved_with_ambiguity})

_OPERAND_NEED_ROLE = "operand"


@dataclass(frozen=True, slots=True)
class OperandPathCandidateV1:
    """One governed path the operand can take to a REVALIDATED physical landing (spec §5 step 3).

    Carries the frontier's own single-source ``binding_plan`` (its ``path_segments`` ARE the
    governed crossings), the landing ``(landing_catalog, landing_table_ref)`` re-derived from those
    segments, and the landing ``GovernedEndpointV1`` (proven by a VERIFIED ``grain`` fact)."""
    binding_plan: BindingPlanV1
    landing_catalog: str
    landing_table_ref: str
    landing_endpoint: GovernedEndpointV1


@dataclass(frozen=True, slots=True)
class OperandEnumerationResultV1:
    """The typed result of enumerating ONE operand's governed paths (finding #7, spec §8). An empty
    ``candidates`` ALWAYS carries a ``status`` reason (never a bare empty tuple); ``bounds`` records
    the ``MAX_PATHS_PER_OPERAND`` truncation. Status/reason are the unified ``MultiSourceReason``."""
    candidates: tuple[OperandPathCandidateV1, ...]
    status: MultiSourceReason
    reason_codes: tuple[MultiSourceReason, ...]
    bounds: MultiSourceBoundingMetricsV1


def _bounds(*, paths_truncated: bool, total_states: int) -> MultiSourceBoundingMetricsV1:
    """Per-operand enumeration bounds. Only the ``MAX_PATHS_PER_OPERAND`` cut and the states-
    expanded tally are meaningful at the operand grain; combinations/states/landing-ambiguity
    bounds belong to the later cross-operand convergence step and stay ``False`` here."""
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=paths_truncated,
        operand_combinations_truncated=False,
        states_truncated=False,
        landing_ambiguous=False,
        total_states_expanded=total_states)


def _operand_bindings(conn, operand: OperandSlotV1, *, recipe_id: str,
                      need_role: str) -> tuple[IngredientBindingV1, ...]:
    """The pinned-operand ingredient bindings the frontier carries onto the plan: the measure/
    counted/time need bound to the operand's column, plus — for a ``take_latest`` operand — a
    SECOND bound temporal need for the ordering anchor (resolved to a same-table column tagged with
    ``ordering_anchor_concept``) so the reused ``compile_temporal`` can validate it downstream. The
    bindings are safe by construction; they do not affect the path physics (which read the graph
    directly), only what rides onto ``BindingPlanV1``."""
    bindings = [_binding(recipe_id, need_role, operand.catalog_source, operand.object_ref,
                         concept=operand.authoritative_concept, join_role=str(JoinRole.MEASURE))]
    anchor_concept = operand.path_strategy.ordering_anchor_concept
    if operand.path_strategy.aggregation is PathAggregation.take_latest and anchor_concept:
        anchor_ref = _anchor_column_ref(conn, operand, anchor_concept)
        if anchor_ref is not None:
            bindings.append(_binding(
                recipe_id, f"{need_role}_anchor", operand.catalog_source, anchor_ref,
                concept=anchor_concept, join_role=str(JoinRole.TIME),
                temporal_role=str(TemporalRole.AS_OF_TIME)))
    return tuple(bindings)


def _binding(recipe_id: str, need_role: str, catalog: str, object_ref: str, *, concept: str,
             join_role: str, temporal_role: str = "") -> IngredientBindingV1:
    return IngredientBindingV1(
        recipe_id=recipe_id, need_role=need_role, concept=concept, required_grains=(),
        join_role=join_role, temporal_role=temporal_role, bound_catalog_source=catalog,
        bound_object_ref=object_ref, actual_source_grain=None,
        binding_quality=BindingQuality.grain_and_role_fit, safety=BindingSafety.safe,
        reason_codes=())


def _anchor_column_ref(conn, operand: OperandSlotV1, anchor_concept: str) -> str | None:
    """The column object_ref on the operand's SOURCE table tagged with ``anchor_concept`` (the
    ordering anchor is a same-grain temporal column on the source, e.g. an ``as_of`` date). ``None``
    when no such column exists — the anchor stays unbound (a compile-time concern, not enumeration's;
    the path physics do not depend on it)."""
    source_table = table_of(operand.object_ref)
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND kind = 'column' AND concept = %s ORDER BY object_ref LIMIT 1",
        (operand.catalog_source, source_table.rsplit(".", 1)[-1], anchor_concept)).fetchone()
    return row[0] if row is not None else None


def _rederive_landing(ctx: CompilerContext, plan: BindingPlanV1, *, source_catalog: str,
                      source_table: str) -> tuple[str, str]:
    """Re-derive the plan's landing ``(catalog, table_ref)`` from ``path_segments`` — the frontier
    discards the landing ``_Position`` (``assembly.py:389-398``), so the landing is the target-side
    execution table of the LAST hop. Mirrors ``check_connectivity``/``_hop_evidence``: a realization
    segment lands on the realization's ``to_object_ref`` (already a TABLE ref); a bridge segment
    lands on the endpoint table in the SEGMENT's catalog (endpoint storage order is unordered). A
    segment whose ref the context cannot resolve contributes nothing (fail closed). The pre-first-hop
    source position is the fallback (a degenerate zero-hop path lands in place)."""
    landing = (source_catalog, source_table)
    for seg in plan.path_segments:
        if seg.realization_ref is not None:
            r = next((x for x in ctx.realizations_by_catalog.get(seg.catalog_source, ())
                      if x.realization_id == seg.realization_ref), None)
            if r is not None:
                landing = (seg.catalog_source, r.to_object_ref)
        elif seg.bridge_fact_key is not None:
            br = next((x for x in ctx.active_bridges if x.fact_key == seg.bridge_fact_key), None)
            if br is None:
                continue
            for cat, col_ref in ((br.left_catalog_source, br.left_object_ref),
                                 (br.right_catalog_source, br.right_object_ref)):
                if cat == seg.catalog_source:
                    landing = (cat, table_of(col_ref))
                    break
    return landing


def _enumerate_plans(conn, *, source_position: _Position, target_entity: str, scope: CatalogScopeV1,
                     bindings: tuple[IngredientBindingV1, ...], template
                     ) -> tuple[list[BindingPlanV1], bool, int]:
    """Drive the reused frontier (``semantic_rollup_paths`` -> ``assemble_paths``) across every
    governed semantic path from ``source_position`` to ``target_entity`` and collect the RESOLVED
    cross-catalog plans, capped at ``MAX_PATHS_PER_OPERAND``. Returns (plans, truncated,
    total_states_expanded). The cap is enforced across ALL semantic paths (the per-operand budget)."""
    semantic_paths, _status = semantic_rollup_paths(source_position.entity, target_entity)
    plans: list[BindingPlanV1] = []
    total_states = 0
    truncated = False
    for semantic_path in semantic_paths:
        assembly = assemble_paths(
            conn, source_position=source_position, semantic_path=semantic_path, scope=scope,
            ingredient_bindings=bindings, template=template, target_entity=target_entity)
        total_states += assembly.bounding.total_states_expanded
        for plan in assembly.complete:
            if plan.resolution_status in _RESOLVED_STATUSES:
                plans.append(plan)
                if len(plans) > MAX_PATHS_PER_OPERAND:
                    truncated = True    # a further governed path was dropped, not kept
                    break
        if truncated:
            break
    return plans[:MAX_PATHS_PER_OPERAND], truncated, total_states


def enumerate_operand_paths(
        conn, adapter: CatalogAdapter, ctx: CompilerContext, *, operand: OperandSlotV1,
        target_entity: str, scope: CatalogScopeV1, roles: Iterable[str],
        now: datetime) -> OperandEnumerationResultV1:
    """Enumerate ONE operand's governed paths to a REVALIDATED physical landing (spec §5 steps 2-3).

    Builds the injected single-need ``Template`` (with the second temporal need when the strategy is
    ``take_latest``) + hand-built source ``_Position`` from the operand's ``GovernedSourceBindingV1``,
    drives the reused frontier for every governed path (bounded ``MAX_PATHS_PER_OPERAND``), re-derives
    each resolved plan's landing from ``path_segments``, and revalidates the landing with the Task-4
    ``governed_endpoint`` grain-fact check — dropping ungoverned landings. Fail-closed: no
    VERIFIED-bridge path -> ``no_governed_path``; a path whose landing lacks a grain fact ->
    ``realization_endpoint_ungoverned`` (never a bare empty tuple). ``ctx`` is already role-scoped;
    ``roles`` is carried for signature parity with the rest of the assembly engine."""
    del roles   # ctx is already read-scoped by the caller's roles; the frontier reads conn directly
    recipe_id = f"ms:{operand.slot_id}"
    source_entity = operand.source_binding.source_grain_entity
    source_table = table_of(operand.object_ref)
    source_position = _Position(source_entity, operand.catalog_source, source_table)
    anchor_concept = (operand.path_strategy.ordering_anchor_concept
                      if operand.path_strategy.aggregation is PathAggregation.take_latest else None)
    template = injected_operand_template(
        recipe_id=recipe_id, need_role=_OPERAND_NEED_ROLE,
        concept=operand.authoritative_concept, source_entity=source_entity,
        anchor_concept=anchor_concept)
    bindings = _operand_bindings(conn, operand, recipe_id=recipe_id, need_role=_OPERAND_NEED_ROLE)

    plans, truncated, total_states = _enumerate_plans(
        conn, source_position=source_position, target_entity=target_entity, scope=scope,
        bindings=bindings, template=template)
    bounds = _bounds(paths_truncated=truncated, total_states=total_states)

    # No VERIFIED-bridge path resolved at all — the planner only reads VERIFIED bridges, and
    # absence never proves an unverified route exists. Fail closed.
    if not plans:
        return OperandEnumerationResultV1(
            candidates=(), status=MultiSourceReason.no_governed_path,
            reason_codes=(MultiSourceReason.no_governed_path,), bounds=bounds)

    candidates: list[OperandPathCandidateV1] = []
    for plan in plans:
        landing_catalog, landing_table_ref = _rederive_landing(
            ctx, plan, source_catalog=operand.catalog_source, source_table=source_table)
        endpoint = governed_endpoint(
            conn, adapter, catalog=landing_catalog, table_ref=landing_table_ref, now=now)
        if endpoint is None:
            continue    # the landing has no VERIFIED grain fact — ungoverned, dropped
        candidates.append(OperandPathCandidateV1(
            binding_plan=plan, landing_catalog=landing_catalog,
            landing_table_ref=landing_table_ref, landing_endpoint=endpoint))

    # A governed path resolved but no landing was governed by a VERIFIED grain fact.
    if not candidates:
        return OperandEnumerationResultV1(
            candidates=(), status=MultiSourceReason.realization_endpoint_ungoverned,
            reason_codes=(MultiSourceReason.realization_endpoint_ungoverned,), bounds=bounds)

    reason_codes = (MultiSourceReason.budget_truncated,) if truncated else ()
    return OperandEnumerationResultV1(
        candidates=tuple(candidates), status=MultiSourceReason.resolved,
        reason_codes=reason_codes, bounds=bounds)
