"""Phase 3C.2b-i-A · Task 5 — per-operand governed path enumeration (spec §5 steps 1-3, §8).

The first assembly-engine task. It INTEGRATES three pieces that already landed:

* the Task-1 reuse spike (``multisource_reuse``) — the injected single-need ``Template`` +
  hand-built ``_Position`` driven through the EXISTING cross-catalog frontier
  (``semantic_rollup_paths`` -> ``assemble_paths``). Enumeration drives that same engine, but keeps
  EVERY resolved cross-catalog ``BindingPlanV1`` the frontier produces (not just the first, as the
  spike's ``run_operand_rollup`` returns), bounded by ``MAX_PATHS_PER_OPERAND``.
* the Task-4 endpoint check (``multisource_endpoints.governed_endpoint``) — EVERY hop endpoint of a
  resolved plan (source + each intermediate + landing) is revalidated against a VERIFIED ``grain``
  fact (spec §2/§3.2); an ungoverned source is ``source_binding_ungoverned``, an ungoverned
  intermediate/landing is ``realization_endpoint_ungoverned``, and a candidate survives only when ALL
  its hop endpoints are governed.
* the Task-2 typed contracts (``multisource_contracts``) — status/reason fields are typed on the
  unified ``MultiSourceReason`` vocabulary (NOT the single-source ``PlanResolutionStatus``), and
  bounds reuse ``MultiSourceBoundingMetricsV1``.

The frontier does NOT emit the landing (``_mint`` discards the landing ``_Position``); A re-derives
EVERY hop endpoint ``(catalog, table_ref)`` from the resolved plan's ``path_segments`` exactly the
way ``check_connectivity``/``_hop_evidence`` compute execution tables (``declarations.py:191-211``,
``:346-377``): walk the realization/bridge segments in order, tracking the physical target-side
table; the first entry is the source, the last is the landing. FAIL CLOSED: an empty result NEVER a
bare empty tuple — it carries ``no_governed_path`` (no VERIFIED-bridge path at all),
``source_binding_ungoverned`` (the source endpoint is ungoverned or its grain fact_key mismatches the
binding's), or ``realization_endpoint_ungoverned`` (a path exists but an intermediate/landing has no
grain fact).

Read-only over the reused frontier + compiler surfaces; nothing here edits ``assemble_paths`` /
``governed_endpoint`` (the §12 behaviour-neutrality invariant).
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.catalog_realizations import table_of
from featuregen.overlay.upload.planner.assembly import (
    _AUTHORITY_RANK,
    _Position,
    assemble_paths,
    semantic_rollup_paths,
)
from featuregen.overlay.upload.planner.contracts import (
    MAX_MULTISOURCE_STATES_EXPANDED,
    MAX_OPERAND_COMBINATIONS,
    MAX_PATHS_PER_OPERAND,
    AggregationValidation,
    BindingPlanV1,
    BindingQuality,
    BindingSafety,
    CatalogScopeV1,
    HopAggregationV1,
    IngredientBindingV1,
    PlanResolutionStatus,
    ReasonCode,
    SegmentKind,
    TemporalDeclarationV1,
)
from featuregen.overlay.upload.planner.declarations import (
    CompilerContext,
    build_physical_read_set,
    check_connectivity,
    compile_aggregation,
    compile_temporal,
    stage_safety,
)
from featuregen.overlay.upload.planner.multisource_contracts import (
    GovernedEndpointV1,
    MultiSourceBoundingMetricsV1,
    MultiSourceReason,
    OperandSlotV1,
    PathAggregation,
    PhysicalLandingV1,
)
from featuregen.overlay.upload.planner.multisource_endpoints import governed_endpoint
from featuregen.overlay.upload.planner.multisource_reuse import injected_operand_template
from featuregen.overlay.upload.taxonomy.entity_relationships import RealizationAuthority
from featuregen.overlay.upload.templates import Template

# The single-source resolution statuses that count as a governed complete path. ``assemble_paths``
# returns ranked/classified plans; a top-rank tie is normalized to ``resolved_with_ambiguity`` —
# still a governed cross-catalog roll-up, so both are enumerated as candidates.
_RESOLVED_STATUSES = frozenset(
    {PlanResolutionStatus.resolved, PlanResolutionStatus.resolved_with_ambiguity})

_OPERAND_NEED_ROLE = "operand"


def _operand_recipe_id(operand: OperandSlotV1) -> str:
    """The injected recipe id A keys the operand's ``Template``/``agg_declarations`` on — stable per
    slot so enumeration and the Task-7 per-path checks compile the SAME plan identity."""
    return f"ms:{operand.slot_id}"


def _operand_anchor_concept(operand: OperandSlotV1) -> str | None:
    """The ordering-anchor concept A injects as a SECOND temporal need — present ONLY for a
    ``take_latest`` strategy (so ``compile_temporal`` can find + validate the anchor)."""
    strategy = operand.path_strategy
    return (strategy.ordering_anchor_concept
            if strategy.aggregation is PathAggregation.take_latest else None)


def _operand_template(operand: OperandSlotV1) -> Template:
    """The injected single-need ``Template`` for one operand — the ONE builder Task-5 enumeration AND
    the Task-7 per-path checks both call, so the template a path is VALIDATED against is byte-identical
    to the one it was ENUMERATED against (same measure/counted/time need + the second temporal need for
    ``take_latest``, same ``ordering_anchor_concept``). A mismatched template would validate a
    different plan than was enumerated (spec §5 step 5)."""
    return injected_operand_template(
        recipe_id=_operand_recipe_id(operand), need_role=_OPERAND_NEED_ROLE,
        concept=operand.authoritative_concept,
        source_entity=operand.source_binding.source_grain_entity,
        anchor_concept=_operand_anchor_concept(operand))


@dataclass(frozen=True, slots=True)
class OperandPathCandidateV1:
    """One governed path the operand can take to a REVALIDATED physical landing (spec §5 step 3).

    Carries the frontier's own single-source ``binding_plan`` (its ``path_segments`` ARE the
    governed crossings), the landing ``(landing_catalog, landing_table_ref)`` re-derived from those
    segments, and the landing ``GovernedEndpointV1`` (proven by a VERIFIED ``grain`` fact).

    ``governed_endpoints`` is the FULL tuple of revalidated endpoints in path order — the SOURCE,
    each INTERMEDIATE, and the LANDING (spec §2/§3.2: every hop endpoint must have a VERIFIED grain
    fact, not just the landing). ``landing_endpoint`` is exactly ``governed_endpoints[-1]``; the
    source is ``governed_endpoints[0]``. A candidate exists only when ALL its hop endpoints resolved
    to a ``GovernedEndpointV1`` (an ungoverned source is ``source_binding_ungoverned``; an ungoverned
    intermediate/landing is ``realization_endpoint_ungoverned``).

    ``authority_key`` is the CROSS-RUN-COMPARABLE authority tuple (worst realizer authority, total
    crossings, semantic hops) computed from THIS candidate's OWN ``path_segments`` (Task-6 fix #T6) —
    NOT the frontier's ``preference_rank`` (a per-``assemble_paths``-run POSITIONAL index that resets
    to 0 each run and is assigned before ungoverned landings are dropped, so summing it across the
    runs/operands convergence concatenates is not a valid authority order). Convergence ranks and
    ties on THIS tuple instead."""
    binding_plan: BindingPlanV1
    landing_catalog: str
    landing_table_ref: str
    landing_endpoint: GovernedEndpointV1
    authority_key: tuple[int, int, int]
    governed_endpoints: tuple[GovernedEndpointV1, ...]


@dataclass(frozen=True, slots=True)
class OperandEnumerationResultV1:
    """The typed result of enumerating ONE operand's governed paths (finding #7, spec §8). An empty
    ``candidates`` ALWAYS carries a ``status`` reason (never a bare empty tuple); ``bounds`` records
    the ``MAX_PATHS_PER_OPERAND`` truncation. Status/reason are the unified ``MultiSourceReason``."""
    candidates: tuple[OperandPathCandidateV1, ...]
    status: MultiSourceReason
    reason_codes: tuple[MultiSourceReason, ...]
    bounds: MultiSourceBoundingMetricsV1


def _bounds(*, paths_truncated: bool, states_truncated: bool,
            total_states: int) -> MultiSourceBoundingMetricsV1:
    """Per-operand enumeration bounds. Two INDEPENDENT frontier cuts are meaningful at the operand grain:
    the ``MAX_PATHS_PER_OPERAND`` complete-path count cut (``paths_truncated``) and the
    ``MAX_MULTISOURCE_STATES_EXPANDED`` cumulative frontier-state cut (``states_truncated``, M-a) — a
    semantic path can expand many states while yielding few complete plans, so the state bound guards a
    blow-up the path-count bound does not. Combinations/landing-ambiguity bounds belong to the later
    cross-operand convergence step and stay ``False`` here."""
    return MultiSourceBoundingMetricsV1(
        paths_per_operand_truncated=paths_truncated,
        operand_combinations_truncated=False,
        states_truncated=states_truncated,
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


def _rederive_hop_tables(ctx: CompilerContext, plan: BindingPlanV1, *, source_catalog: str,
                         source_table: str) -> tuple[tuple[str, str], ...]:
    """Re-derive EVERY hop endpoint table of the resolved path from ``path_segments``, in path order:
    the SOURCE table, then the target-side execution table of each governed segment (intermediate ...
    landing). The frontier discards the landing ``_Position`` (``assembly.py:389-398``), so A walks
    the segments exactly as ``check_connectivity``/``_hop_evidence`` compute execution tables
    (``declarations.py:191-211``, ``:346-377``): an ``intra_catalog_realization`` segment lands on
    the realization's ``to_object_ref`` (already a TABLE ref); a ``governed_bridge`` segment lands on
    the endpoint table in the SEGMENT's catalog (``cat2`` = the far/target side — ``assembly.py:245``;
    endpoint storage order is unordered). Pure ``semantic_rollup`` announcement segments carry neither
    ref and contribute no endpoint; a segment whose ref the context cannot resolve contributes nothing
    (fail closed). The FIRST entry is always the source; the LAST is the landing (a degenerate
    zero-hop path lands in place, so source == landing)."""
    hops: list[tuple[str, str]] = [(source_catalog, source_table)]
    for seg in plan.path_segments:
        if seg.realization_ref is not None:
            r = next((x for x in ctx.realizations_by_catalog.get(seg.catalog_source, ())
                      if x.realization_id == seg.realization_ref), None)
            if r is not None:
                hops.append((seg.catalog_source, r.to_object_ref))
        elif seg.bridge_fact_key is not None:
            br = next((x for x in ctx.active_bridges if x.fact_key == seg.bridge_fact_key), None)
            if br is None:
                continue
            for cat, col_ref in ((br.left_catalog_source, br.left_object_ref),
                                 (br.right_catalog_source, br.right_object_ref)):
                if cat == seg.catalog_source:
                    hops.append((cat, table_of(col_ref)))
                    break
    return tuple(hops)


def _enumerate_plans(conn, *, source_position: _Position, target_entity: str, scope: CatalogScopeV1,
                     bindings: tuple[IngredientBindingV1, ...], template
                     ) -> tuple[list[BindingPlanV1], bool, bool, int]:
    """Drive the reused frontier (``semantic_rollup_paths`` -> ``assemble_paths``) across every
    governed semantic path from ``source_position`` to ``target_entity`` and collect the RESOLVED
    cross-catalog plans. Returns (plans, paths_truncated, states_truncated, total_states_expanded).

    TWO independent per-operand bounds stop the walk (M-a): the ``MAX_PATHS_PER_OPERAND`` cap on the
    number of KEPT complete plans, and the ``MAX_MULTISOURCE_STATES_EXPANDED`` cap on the CUMULATIVE
    frontier state expansion summed across the semantic paths — a semantic path can expand many states
    while yielding few complete plans, so the state cap guards a blow-up the path-count cap does not.
    Either cap stops further semantic-path expansion (never a hot loop) and sets its own flag."""
    semantic_paths, _status = semantic_rollup_paths(source_position.entity, target_entity)
    plans: list[BindingPlanV1] = []
    total_states = 0
    paths_truncated = False
    states_truncated = False
    for semantic_path in semantic_paths:
        assembly = assemble_paths(
            conn, source_position=source_position, semantic_path=semantic_path, scope=scope,
            ingredient_bindings=bindings, template=template, target_entity=target_entity)
        total_states += assembly.bounding.total_states_expanded
        for plan in assembly.complete:
            if plan.resolution_status in _RESOLVED_STATUSES:
                plans.append(plan)
                if len(plans) > MAX_PATHS_PER_OPERAND:
                    paths_truncated = True    # a further governed path was dropped, not kept
                    break
        if paths_truncated:
            break
        # Cumulative frontier-state cap (M-a): stop expanding further semantic paths once the summed
        # state expansion exceeds the bound — independent of how many complete plans were kept.
        if total_states > MAX_MULTISOURCE_STATES_EXPANDED:
            states_truncated = True
            break
    return plans[:MAX_PATHS_PER_OPERAND], paths_truncated, states_truncated, total_states


def _realizer_authority_ranks(ctx: CompilerContext) -> dict[tuple[str, str], int]:
    """(catalog, realization_id) -> ``_AUTHORITY_RANK`` for every governed realization in the batch-
    loaded context — the SAME governed source the frontier's transitions used (conn-free, §12 read-
    only). Mirrors ``assembly._authority_rank_lookup`` but over ``ctx.realizations_by_catalog`` (the
    frontier ran on the same snapshot), so authority stays comparable ACROSS ``assemble_paths`` runs."""
    lookup: dict[tuple[str, str], int] = {}
    for cat, rels in ctx.realizations_by_catalog.items():
        for r in rels:
            lookup[(cat, r.realization_id)] = _AUTHORITY_RANK[r.authority]
    return lookup


def _authority_key(plan: BindingPlanV1, ranks: dict[tuple[str, str], int]) -> tuple[int, int, int]:
    """The candidate's CROSS-RUN-COMPARABLE authority tuple, from ITS OWN ``path_segments`` (best =
    smallest): ``(worst realizer authority, bridge_count, semantic hops)``. The worst-realizer
    component mirrors ``assembly._rank_key`` exactly — a realizer the governed lookup cannot resolve
    ranks WORST (``INFERRED_JOIN``-level), never APPROVED-best; a pure-bridge path has no realizer
    segment and ranks neutrally at 0 (the ``default=``). Unlike the frontier's per-run ``preference_
    rank`` (a positional index that resets to 0 each ``assemble_paths`` run), every component here is
    derived from the path itself, so it is directly comparable across runs and across operands — a
    valid quantity to SUM across a landing's per-operand best candidates (Task-6 fix #T6)."""
    worst_realizer = max(
        (ranks.get((s.catalog_source, s.realization_ref or ""),
                    _AUTHORITY_RANK[RealizationAuthority.INFERRED_JOIN])
         for s in plan.path_segments
         if s.segment_kind is SegmentKind.intra_catalog_realization),
        default=0)
    hops = sum(1 for s in plan.path_segments if s.segment_kind is SegmentKind.semantic_rollup)
    return (worst_realizer, plan.bridge_count, hops)


def enumerate_operand_paths(
        conn, adapter: CatalogAdapter, ctx: CompilerContext, *, operand: OperandSlotV1,
        target_entity: str, scope: CatalogScopeV1, roles: Iterable[str],
        now: datetime) -> OperandEnumerationResultV1:
    """Enumerate ONE operand's governed paths to a REVALIDATED physical landing (spec §5 steps 2-3).

    Builds the injected single-need ``Template`` (with the second temporal need when the strategy is
    ``take_latest``) + hand-built source ``_Position`` from the operand's ``GovernedSourceBindingV1``,
    drives the reused frontier for every governed path (bounded ``MAX_PATHS_PER_OPERAND``), re-derives
    each resolved plan's EVERY hop endpoint (source + intermediates + landing) from ``path_segments``,
    and revalidates EACH with the Task-4 ``governed_endpoint`` grain-fact check (spec §2/§3.2: every
    hop endpoint must carry a VERIFIED ``grain`` fact, not just the landing). Fail-closed:

      * no VERIFIED-bridge path resolves at all -> ``no_governed_path``;
      * the SOURCE endpoint is ungoverned, OR its governed ``grain_fact_key`` != the binding's claimed
        ``GovernedSourceBindingV1.grain_fact_key`` (the binding claims a grain that isn't the actual
        VERIFIED one) -> ``source_binding_ungoverned`` (A trusts the binding's STRUCTURE but VALIDATES
        its claimed grain fact is a real VERIFIED one, spec §1);
      * an INTERMEDIATE or LANDING endpoint lacks a grain fact -> ``realization_endpoint_ungoverned``.

    A candidate survives only when ALL its hop endpoints are governed; an empty result NEVER a bare
    empty tuple. ``ctx`` is already role-scoped; ``roles`` is carried for signature parity with the
    rest of the assembly engine."""
    del roles   # ctx is already read-scoped by the caller's roles; the frontier reads conn directly
    recipe_id = _operand_recipe_id(operand)
    source_entity = operand.source_binding.source_grain_entity
    source_table = table_of(operand.object_ref)
    source_position = _Position(source_entity, operand.catalog_source, source_table)
    template = _operand_template(operand)
    bindings = _operand_bindings(conn, operand, recipe_id=recipe_id, need_role=_OPERAND_NEED_ROLE)

    plans, paths_truncated, states_truncated, total_states = _enumerate_plans(
        conn, source_position=source_position, target_entity=target_entity, scope=scope,
        bindings=bindings, template=template)
    truncated = paths_truncated or states_truncated
    bounds = _bounds(paths_truncated=paths_truncated, states_truncated=states_truncated,
                     total_states=total_states)

    # No VERIFIED-bridge path resolved at all — the planner only reads VERIFIED bridges, and
    # absence never proves an unverified route exists. Fail closed.
    if not plans:
        return OperandEnumerationResultV1(
            candidates=(), status=MultiSourceReason.no_governed_path,
            reason_codes=(MultiSourceReason.no_governed_path,), bounds=bounds)

    # Source-endpoint governance (spec §2/§3.2, §1 reuse model). The source table is INVARIANT across
    # this operand's plans (it is ``table_of(operand.object_ref)``), so revalidate it ONCE: A trusts
    # the binding's STRUCTURE (columns/grain entity) yet VALIDATES that its CLAIMED grain fact is
    # actually the VERIFIED one. An ungoverned source, OR a governed grain whose deterministic
    # ``grain_fact_key`` != the binding's claimed one, is a ``source_binding_ungoverned`` reject.
    source_endpoint = governed_endpoint(
        conn, adapter, catalog=operand.catalog_source, table_ref=source_table, now=now)
    if (source_endpoint is None
            or source_endpoint.grain_fact_key != operand.source_binding.grain_fact_key):
        return OperandEnumerationResultV1(
            candidates=(), status=MultiSourceReason.source_binding_ungoverned,
            reason_codes=(MultiSourceReason.source_binding_ungoverned,), bounds=bounds)

    authority_ranks = _realizer_authority_ranks(ctx)
    candidates: list[OperandPathCandidateV1] = []
    for plan in plans:
        hop_tables = _rederive_hop_tables(
            ctx, plan, source_catalog=operand.catalog_source, source_table=source_table)
        # hop_tables[0] is the source (revalidated above); revalidate every INTERMEDIATE + LANDING
        # endpoint. An ungoverned intermediate/landing drops the candidate; a candidate survives only
        # when ALL its hop endpoints carry a VERIFIED grain fact (spec §2/§3.2).
        endpoints: list[GovernedEndpointV1] = [source_endpoint]
        ungoverned = False
        for cat, table in hop_tables[1:]:
            endpoint = governed_endpoint(conn, adapter, catalog=cat, table_ref=table, now=now)
            if endpoint is None:
                ungoverned = True   # an intermediate/landing endpoint has no VERIFIED grain fact
                break
            endpoints.append(endpoint)
        if ungoverned:
            continue
        landing_catalog, landing_table_ref = hop_tables[-1]
        candidates.append(OperandPathCandidateV1(
            binding_plan=plan, landing_catalog=landing_catalog,
            landing_table_ref=landing_table_ref, landing_endpoint=endpoints[-1],
            authority_key=_authority_key(plan, authority_ranks),
            governed_endpoints=tuple(endpoints)))

    # Every governed path had an ungoverned INTERMEDIATE or LANDING endpoint (the source WAS governed).
    if not candidates:
        return OperandEnumerationResultV1(
            candidates=(), status=MultiSourceReason.realization_endpoint_ungoverned,
            reason_codes=(MultiSourceReason.realization_endpoint_ungoverned,), bounds=bounds)

    reason_codes = (MultiSourceReason.budget_truncated,) if truncated else ()
    return OperandEnumerationResultV1(
        candidates=tuple(candidates), status=MultiSourceReason.resolved,
        reason_codes=reason_codes, bounds=bounds)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Task 6 — exact physical-landing convergence + deterministic ranking (spec §5 step 4, §8).
#
# Each operand's candidates (Task 5) land on a re-derived physical grain. The SAME operand can reach
# ONE landing by several distinct governed plans: the frontier dedups complete states by
# ``used_bridge_fact_keys``, so distinct bridge-key plans re-derive to the same physical landing
# (Task-5 note). Convergence therefore groups each operand's candidates BY full ``PhysicalLandingV1``
# identity — (catalog, table_ref, composite grain_key_refs) — keeping the best-ranked candidate per
# (operand, landing), then INTERSECTS the per-operand landing sets on that full identity. A landing
# every operand reaches is a common landing; the final join is on EVERY grain key.
#
# Ranking uses the candidate's CROSS-RUN-COMPARABLE ``authority_key`` (Task-6 fix #T6), materialized by
# Task 5 from each candidate's OWN ``path_segments`` (``_AUTHORITY_RANK`` worst realizer -> bridge count
# -> semantic hops) — NOT the frontier's ``preference_rank``. ``preference_rank`` is a per-``assemble_
# paths``-run POSITIONAL index (reset to 0 per semantic-path run, assigned before ungoverned landings
# are dropped); ``_enumerate_plans`` concatenates one run PER semantic path, so summing ``preference_
# rank`` across runs/operands was not a valid authority order — it both hid genuine ties (dropped
# ungoverned siblings shift a surviving candidate's index) and manufactured false ones (two best-in-
# their-own-run landings both index 0, erasing an APPROVED-vs-INFERRED authority difference). The
# authority tuple is derived from the path itself, so it is comparable across runs and operands.
# Convergence stays CONN-FREE by contract (spec §8) and never re-reads the graph for authority (§12
# read-only): it consumes the authority Task 5 already folded onto the candidate. A common landing's
# SEMANTIC rank is (Σ worst realizer authority, Σ bridge_count, Σ hops): authority of the crossings
# first, fewest TOTAL crossings second, fewest hops third. A top-semantic-rank tie across DISTINCT
# landings is ``ambiguous_physical_grain`` (+ ``landing_ambiguous``) — detected BEFORE any stable-
# identity presentation order, so an ambiguity is surfaced, never silently resolved by a tiebreak. No
# common landing -> ``no_common_physical_grain``. The realised work is the INTERSECTION; ``budget_
# truncated`` is recorded ONLY when the materialised common landings genuinely exceed
# ``MAX_OPERAND_COMBINATIONS`` (or an upstream bound truncated), never merely because the theoretical
# product is large. Fail-closed: an empty ``landed_combinations`` ALWAYS carries a reason.
# ═══════════════════════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class LandedCombinationV1:
    """One converged physical landing every operand reaches, carrying the per-operand best-ranked
    candidate AT that landing. ``operand_candidates`` preserves INPUT operand order (convergence keys
    operands positionally — ``OperandEnumerationResultV1`` carries no slot id), so downstream maps a
    candidate back to its operand by index. The final join is on the landing's full ``grain_key_refs``."""
    landing: PhysicalLandingV1
    operand_candidates: tuple[OperandPathCandidateV1, ...]


@dataclass(frozen=True, slots=True)
class ConvergenceResultV1:
    """The typed convergence result (finding #7, spec §8). ``landed_combinations`` is empty UNLESS
    exactly one unambiguous best common landing exists (then length 1 — the per-operand best candidate
    at that landing); an empty result ALWAYS carries a ``status``/``reason_codes`` reason
    (``no_common_physical_grain``/``ambiguous_physical_grain``, plus ``budget_truncated`` when the
    combination space was capped), never a bare empty tuple. ``bounds`` extends the incoming metrics
    with ``landing_ambiguous`` + ``operand_combinations_truncated``."""
    landed_combinations: tuple[LandedCombinationV1, ...]
    status: MultiSourceReason
    reason_codes: tuple[MultiSourceReason, ...]
    bounds: MultiSourceBoundingMetricsV1


def _landing_of(candidate: OperandPathCandidateV1) -> PhysicalLandingV1:
    """The full physical-landing identity of a candidate: its re-derived (catalog, table_ref) plus the
    landing endpoint's composite ``grain_key_refs`` (multi-column grain preserved verbatim, order
    intact) — the identity landings are grouped + intersected on."""
    return PhysicalLandingV1(
        catalog=candidate.landing_catalog, table_ref=candidate.landing_table_ref,
        grain_key_refs=candidate.landing_endpoint.grain_key_refs)


def _candidate_rank(candidate: OperandPathCandidateV1) -> tuple[int, int, int, str]:
    """Per-candidate rank (best = smallest) for choosing the best plan at a given (operand, landing):
    the CROSS-RUN-COMPARABLE ``authority_key`` (worst realizer authority -> total crossings -> hops),
    then the canonical ``physical_plan_id`` for a deterministic final tiebreak. Candidates for one
    landing can come from DIFFERENT ``assemble_paths`` runs (distinct bridge-key plans re-derive to the
    same landing), so the per-run ``preference_rank`` was unsound here too — ``authority_key`` is not."""
    return (*candidate.authority_key, candidate.binding_plan.physical_plan_id)


def _best_per_landing(
        result: OperandEnumerationResultV1) -> dict[PhysicalLandingV1, OperandPathCandidateV1]:
    """Group ONE operand's candidates by full ``PhysicalLandingV1`` identity, keeping the best-ranked
    candidate per landing (distinct bridge-key plans re-derive to the same landing — Task-5 note; the
    frontier dedups complete states by ``used_bridge_fact_keys``)."""
    best: dict[PhysicalLandingV1, OperandPathCandidateV1] = {}
    for candidate in result.candidates:
        landing = _landing_of(candidate)
        incumbent = best.get(landing)
        if incumbent is None or _candidate_rank(candidate) < _candidate_rank(incumbent):
            best[landing] = candidate
    return best


def _landing_semantic_key(operand_bests: tuple[OperandPathCandidateV1, ...]) -> tuple[int, int, int]:
    """The SEMANTIC rank of a common landing (best = smallest), EXCLUDING any stable landing identity
    so a genuine tie surfaces before ordering: the Σ of every operand's best candidate's CROSS-RUN-
    COMPARABLE ``authority_key`` — authority of the crossings first (Σ worst realizer authority),
    fewest TOTAL crossings second (Σ ``bridge_count``), fewest hops third (Σ hops). Each component is
    comparable ACROSS ``assemble_paths`` runs (derived from the candidate's OWN path, not a per-run
    index), so a genuine top-authority tie across DISTINCT landings surfaces as ambiguous and an
    unambiguous best is selected — the per-run ``preference_rank`` reset made summing it unsound (#T6)."""
    return (
        sum(c.authority_key[0] for c in operand_bests),
        sum(c.authority_key[1] for c in operand_bests),
        sum(c.authority_key[2] for c in operand_bests),
    )


def _empty_convergence(reason: MultiSourceReason, bounds: MultiSourceBoundingMetricsV1, *,
                       truncated: bool, ambiguous: bool) -> ConvergenceResultV1:
    """A fail-closed empty convergence: no landed combination, ALWAYS a reason (never a bare empty
    tuple), with ``landing_ambiguous``/``operand_combinations_truncated`` recorded on the bounds."""
    reason_codes = (reason,) + ((MultiSourceReason.budget_truncated,) if truncated else ())
    return ConvergenceResultV1(
        landed_combinations=(), status=reason, reason_codes=reason_codes,
        bounds=replace(bounds, operand_combinations_truncated=truncated,
                       landing_ambiguous=bounds.landing_ambiguous or ambiguous))


def converge(operand_results: Sequence[OperandEnumerationResultV1], *,
             bounds: MultiSourceBoundingMetricsV1) -> ConvergenceResultV1:
    """Converge every operand onto ONE exact physical landing (spec §5 step 4, §8).

    Intersect the per-operand landing sets on full ``PhysicalLandingV1`` identity, rank the common
    landings by each candidate's CROSS-RUN-COMPARABLE ``authority_key`` semantic rank (authority of the
    crossings -> fewest total crossings -> fewest hops), and select the single unambiguous best —
    surfacing a top-semantic-rank tie across DISTINCT landings as ``ambiguous_physical_grain`` (+
    ``landing_ambiguous``) BEFORE any stable-identity tiebreak, and no common landing as
    ``no_common_physical_grain``. Conn-free (spec §8): the ``authority_key`` Task 5 folded onto each
    candidate from its OWN path is reused, never re-read (§12) — the per-run ``preference_rank`` is NOT
    consulted (it is not comparable across the runs convergence concatenates, #T6). Operates only on
    the governed candidates handed in (Task-5 note M11). ``budget_truncated`` is recorded ONLY when the
    materialised common landings genuinely exceed ``MAX_OPERAND_COMBINATIONS`` (or an upstream bound
    truncated), never merely because the theoretical product is large. Fail-closed: an empty
    ``landed_combinations`` always carries a reason."""
    # Per-operand landing -> best candidate. An operand that resolved no candidate contributes an
    # empty set, which fails the intersection closed (no common landing).
    per_operand = [_best_per_landing(r) for r in operand_results]

    if not per_operand:
        return _empty_convergence(MultiSourceReason.no_common_physical_grain, bounds,
                                  truncated=bounds.operand_combinations_truncated, ambiguous=False)
    common = set(per_operand[0])
    for bests in per_operand[1:]:
        common &= set(bests)

    # Cross-operand cap (spec §8), Task-6 minor: the theoretical (one-landing-per-operand) product can
    # be huge, but the REALISED work is the INTERSECTION (<= the smallest per-operand set) — every
    # common landing is fully ranked, NONE dropped. So ``budget_truncated`` is recorded ONLY when the
    # materialised common landings genuinely exceed ``MAX_OPERAND_COMBINATIONS`` (a real over-
    # materialisation) or an upstream bound already truncated — never merely because the theoretical
    # product is large, so a fully-captured run is never falsely tagged capture-incomplete.
    truncated = (bounds.operand_combinations_truncated
                 or len(common) > MAX_OPERAND_COMBINATIONS)

    if not common:
        return _empty_convergence(MultiSourceReason.no_common_physical_grain, bounds,
                                  truncated=truncated, ambiguous=False)

    # Rank the common landings by SEMANTIC key; detect a top-rank tie across distinct landings BEFORE
    # any stable-identity ordering (the ambiguity must surface, not be silently tiebroken).
    keyed: list[tuple[tuple[int, int, int], PhysicalLandingV1,
                      tuple[OperandPathCandidateV1, ...]]] = []
    for landing in common:
        operand_bests = tuple(bests[landing] for bests in per_operand)
        keyed.append((_landing_semantic_key(operand_bests), landing, operand_bests))
    best_key = min(k for k, _l, _c in keyed)
    top = [(landing, operand_bests) for k, landing, operand_bests in keyed if k == best_key]
    if len(top) > 1:
        return _empty_convergence(MultiSourceReason.ambiguous_physical_grain, bounds,
                                  truncated=truncated, ambiguous=True)

    landing, operand_bests = top[0]
    combination = LandedCombinationV1(landing=landing, operand_candidates=operand_bests)
    reason_codes = (MultiSourceReason.budget_truncated,) if truncated else ()
    return ConvergenceResultV1(
        landed_combinations=(combination,), status=MultiSourceReason.resolved,
        reason_codes=reason_codes,
        bounds=replace(bounds, operand_combinations_truncated=truncated,
                       landing_ambiguous=bounds.landing_ambiguous))


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Task 7 — per-path aggregation + temporal checks via REUSE (spec §5 step 5, §1 reuse model).
#
# A validates each converged operand path by DRIVING the existing single-source compiler over the
# operand's OWN governed ``BindingPlanV1`` (the Task-5 ``OperandPathCandidateV1``) — it never
# reimplements aggregation/temporal logic:
#   check_connectivity(ctx, plan).placement -> compile_temporal(ctx, plan, template)
#     -> compile_aggregation(ctx, plan, template, temporal, placement)
# with A's OWN ``CompilerContext`` (a POPULATED ``agg_declarations`` keyed by the injected
# (recipe_id, need_role), so a declared ``take_latest``/``sum`` is validated — not resolved
# ``undeclared``). The template fed to the compiler is REBUILT by ``_operand_template`` — the SAME
# builder Task-5 enumeration used — so a path is validated against byte-identically the template it
# was enumerated against (a mismatched template would validate a DIFFERENT plan).
#
# The three checks map the reused compiler's verdicts onto the unified ``MultiSourceReason`` vocab:
#   * an UNSOUND aggregation stage — OR an unsafe/safety-rejected binding (Task-6 convergence
#     intentionally deferred SAFETY to this step; an unsafe path must NEVER pass) -> the path is
#     ``aggregation_unsafe_on_path``.
#   * per-path PIT treatments individually valid AND mutually as-of-consistent at the common landing,
#     else ``temporal_paths_incompatible``.
#   * a TIME-slot ``take_latest`` operand (RECENCY/TREND) — which ``compile_aggregation`` NEVER
#     validates, because it stages MEASURE join_role only (``declarations.py`` C4) — is validated by
#     A's OWN ordering-anchor check; an unbindable ordering anchor rejects with
#     ``ordering_anchor_missing`` (the multi-source reason), never silently degrading to the
#     single-source ``temporal_anchor_missing`` and passing.
# Pure over ``ctx`` (no new DB read beyond what the reused fns already do).
# ═══════════════════════════════════════════════════════════════════════════════════════════════

# The temporal declaration codes that make a single path's PIT treatment INDIVIDUALLY invalid — an
# anchor problem, not a mere annotation (mirrors ``declarations._TEMPORAL_BLOCKING_CODES``).
_TEMPORAL_BLOCKING_CODES = frozenset(
    {ReasonCode.temporal_anchor_missing, ReasonCode.temporal_anchor_ambiguous})


@dataclass(frozen=True, slots=True)
class ResolvedOperandPathV1:
    """The per-path check INPUT (spec §5 step 5): one operand paired with the governed candidate path
    convergence chose for it. Carries the ``OperandSlotV1`` (so ``_operand_template`` rebuilds the
    injected ``Template`` + reads the ``path_strategy`` deterministically, matching Task-5 enumeration)
    and the Task-5 ``OperandPathCandidateV1`` (the resolved ``BindingPlanV1`` + revalidated landing).
    The reused per-path compiler runs over this."""
    operand: OperandSlotV1
    candidate: OperandPathCandidateV1


def check_operand_path(
        ctx: CompilerContext, operand_path: ResolvedOperandPathV1,
) -> tuple[TemporalDeclarationV1, tuple[HopAggregationV1, ...], MultiSourceReason | None]:
    """Validate ONE operand path by reusing the single-source compiler (spec §5 step 5). Runs
    ``check_connectivity`` -> ``compile_temporal`` -> ``compile_aggregation`` over the operand's OWN
    ``BindingPlanV1`` with the SAME injected ``Template`` enumeration used, and returns the compiled
    ``(temporal, hop_aggregations, reason)``. ``reason`` is:

      * ``aggregation_unsafe_on_path`` when the plan's read set is safety-REJECTED (an unsafe binding —
        Task-6 convergence deferred safety to here, so an unsafe path is NOT let through), OR any
        fan-in aggregation stage is not ``sound`` (e.g. a ``sum`` over a fan-in of a non-additive
        measure -> ``incompatible``);
      * ``None`` when the path's aggregation + temporal declaration are sound.

    Pure over ``ctx`` — the reused checks are conn-free (``build_physical_read_set``/``stage_safety``
    read only the batch-loaded context). ``not_evaluated`` safety (a structural evidence gap on a bare
    bridge/join key the read-scope never loaded) is NOT a safety REJECTION and never fails the path
    here — only a genuine ``unsafe`` verdict does."""
    plan = operand_path.candidate.binding_plan
    template = _operand_template(operand_path.operand)
    placement = check_connectivity(ctx, plan).placement
    temporal = compile_temporal(ctx, plan, template)
    hop_aggregations = compile_aggregation(ctx, plan, template, temporal, placement)

    # Safety (Task-6 deferred it to this step): an unsafe/safety-rejected binding must NEVER pass.
    safety_verdict, _codes = stage_safety(build_physical_read_set(ctx, plan))
    if safety_verdict is BindingSafety.unsafe:
        return temporal, hop_aggregations, MultiSourceReason.aggregation_unsafe_on_path

    # Any UNSOUND aggregation stage on any fan-in hop -> the path's roll-up is unsafe.
    if any(stage.validation is not AggregationValidation.sound
           for hop in hop_aggregations for stage in hop.ingredient_stages):
        return temporal, hop_aggregations, MultiSourceReason.aggregation_unsafe_on_path

    return temporal, hop_aggregations, None


def check_paths_temporal_consistency(
        operand_paths: Sequence[TemporalDeclarationV1]) -> MultiSourceReason | None:
    """Cross-path temporal coherence at the common landing (spec §5 step 5). Each element is one
    operand path's PIT treatment — the ``TemporalDeclarationV1`` ``check_operand_path`` compiled.
    Returns ``temporal_paths_incompatible`` when ANY path's treatment is individually invalid (an
    anchor missing/ambiguous), OR the paths are mutually as-of-INCONSISTENT — more than one DISTINCT
    point-in-time anchor role across the paths (e.g. one ``as_of_time`` snapshot joined with one
    ``event_time`` axis has no shared as-of at the landing). Anchor-free paths (``pit_anchor is None``)
    impose no as-of and combine with anything. ``None`` = every path is individually valid and shares
    at most one as-of treatment. Pure — no ``ctx``/conn (the treatments were already compiled)."""
    for temporal in operand_paths:
        if any(code in _TEMPORAL_BLOCKING_CODES for code in temporal.reason_codes):
            return MultiSourceReason.temporal_paths_incompatible
    distinct_anchors = {t.pit_anchor for t in operand_paths if t.pit_anchor is not None}
    if len(distinct_anchors) > 1:
        return MultiSourceReason.temporal_paths_incompatible
    return None


def check_time_slot_take_latest(
        operand_path: ResolvedOperandPathV1) -> MultiSourceReason | None:
    """A's OWN ordering-anchor validation for a TIME-slot ``take_latest`` operand (RECENCY/TREND, spec
    §4/§5 step 5). ``compile_aggregation`` stages MEASURE join_role ONLY (``declarations.py`` C4), so a
    TIME operand is never validated by the reused aggregation compiler — A must prove its ordering
    anchor here. Returns ``ordering_anchor_missing`` when the strategy is ``take_latest`` but its
    ordering anchor is unbindable — no anchor concept, or no bound temporal need on the resolved plan
    carrying that concept (Task-5 ``_operand_bindings`` binds the anchor ONLY when a same-table column
    tagged with the concept exists). This is the multi-source reason, NOT the single-source
    ``temporal_anchor_missing`` — an unbindable anchor must REJECT the operand, never silently pass.
    ``None`` for a non-``take_latest`` strategy (no ordering anchor to validate) or a bound anchor."""
    strategy = operand_path.operand.path_strategy
    if strategy.aggregation is not PathAggregation.take_latest:
        return None     # only a take_latest slot carries an ordering anchor for A to validate
    anchor_concept = strategy.ordering_anchor_concept
    if not anchor_concept:
        return MultiSourceReason.ordering_anchor_missing    # take_latest requires an anchor concept
    none_temporal = str(TemporalRole.NONE)
    anchor_bound = any(
        b.concept == anchor_concept and b.bound_object_ref
        and b.temporal_role and b.temporal_role != none_temporal
        for b in operand_path.candidate.binding_plan.ingredient_bindings)
    return None if anchor_bound else MultiSourceReason.ordering_anchor_missing
