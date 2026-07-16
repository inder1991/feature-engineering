"""Phase-3B.3b — cross-catalog assembly: eligibility, source-entity resolution, semantic paths, the
physical-transition physics, and the bounded frontier search. Read-only, deterministic."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from featuregen.overlay.upload.binding_roles import JoinRole
from featuregen.overlay.upload.bridge_projection import ActiveBridgeV1, active_bridges
from featuregen.overlay.upload.catalog_realizations import (
    derive_catalog_realizations,
    key_entity,
    object_grain,
    table_of,
)
from featuregen.overlay.upload.need_metadata import ResolvedNeedMetadataV1, derive_need_metadata
from featuregen.overlay.upload.planner.contracts import (
    MAX_BRIDGES_PER_PLAN,
    MAX_PHYSICAL_PATHS_PER_BINDING,
    MAX_REALIZATIONS_PER_HOP,
    MAX_STATES_EXPANDED_PER_BINDING,
    BindingPathSegmentV1,
    BindingPlanV1,
    BindingSafety,
    BoundingMetricsV1,
    CandidateRole,
    CatalogScopeV1,
    IngredientBindingV1,
    PathResolutionStatus,
    PlanResolutionStatus,
    ReasonCode,
    SegmentKind,
    make_binding_plan,
)
from featuregen.overlay.upload.planner.order import _agg_quality
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    CatalogEntityRelationshipV1,
    EntityCompatibility,
    EntityRelationshipRefV1,
    EntitySemanticPathV1,
    RealizationAuthority,
)
from featuregen.overlay.upload.templates import Template


@dataclass(frozen=True, slots=True)
class EligibilityV1:
    eligible: bool
    source_entity: str | None
    reason: ReasonCode | None


def _resolved(template: Template) -> tuple[ResolvedNeedMetadataV1, ...]:
    """The GOVERNED per-need resolution (3B.1) — reuse it; never re-derive source grain from concepts here.
    ``derive_need_metadata`` is the pure function behind the ``RESOLVED_NEED_METADATA`` corpus registry and
    raises ``ValueError`` on an ambiguous anchor (the caller treats that as not-eligible)."""
    return derive_need_metadata(template)


def resolve_source_entity(template: Template) -> str | None:
    """The recipe's single source-grain entity, from the GOVERNED 3B.1 resolution: the sole need resolved to
    ``JoinRole.SOURCE_ENTITY_KEY`` and its single ``allowed_source_grain``. 0-or-many source keys, a source key
    with 0-or-many grains, or an ambiguous anchor -> None (never guessed from whichever catalog bound)."""
    try:
        metas = _resolved(template)
    except ValueError:
        return None
    sources = [m for m in metas if m.join_role is JoinRole.SOURCE_ENTITY_KEY]
    if len(sources) != 1:
        return None
    grains = sources[0].allowed_source_grains
    return grains[0] if len(grains) == 1 else None


def ingredient_eligibility(template: Template) -> EligibilityV1:
    """3B.3b handles SOURCE-GRAIN ingredients only. A recipe with no single governed source grain is SKIPPED
    (eligible=False, reason=None — not a rejection; it stays an ingredient-binding-only tier-1 candidate). A
    REQUIRED need governed to a single grain DIFFERENT from the source (a second entity that would need its own
    roll-up, e.g. a resolved ``INTERMEDIATE_ENTITY_KEY``) -> unsupported_multi_grain_ingredients. Optional needs
    and entity-neutral MEASURE/TIME needs (unconstrained grains) never gate."""
    source = resolve_source_entity(template)
    if source is None:
        return EligibilityV1(False, None, None)
    by_role = {m.role: m for m in _resolved(template)}
    for need in template.needs:
        if need.optional:
            continue
        m = by_role.get(need.role)
        if m is None:
            continue
        grains = m.allowed_source_grains
        if len(grains) == 1 and grains[0] != source:
            return EligibilityV1(False, source, ReasonCode.unsupported_multi_grain_ingredients)
    return EligibilityV1(True, source, None)


def semantic_rollup_paths(source_entity: str, target_entity: str
                          ) -> tuple[tuple[EntitySemanticPathV1, ...], EntityCompatibility]:
    """The governed roll-up paths source->target. EXACT (source==target) -> (); DERIVABLE -> one path;
    AMBIGUOUS -> >=2; UNKNOWN -> ()."""
    res = resolve_entity_compatibility(source_entity, target_entity, ENTITY_GRAPH)
    return res.paths, res.status


# ---------------------------------------------------------------------------------------------
# Task B3 — the physical-transition physics. Three pure, read-only, DETERMINISTIC functions the
# B4 frontier expands: (R) intra-catalog realization, (B) cross-catalog roll-up bridge, and the
# same-entity reposition crossing. The invariant is EXACT physical continuity: a realizer/bridge
# is usable ONLY from the position's current table (+ catalog) — never "the entity exists
# somewhere in the catalog". Crossings are governed-bridge-only (active_bridges = VERIFIED) and
# fail closed on the frozen CatalogScopeV1 (an out-of-scope endpoint disqualifies the bridge;
# inaccessible catalogs are never revealed).
# ---------------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class _Position:
    """The exact physical position of the assembly search: WHICH entity grain we hold, in WHICH
    catalog, on WHICH physical table (a table object_ref, e.g. ``public.transactions``)."""
    entity: str
    catalog: str
    table_ref: str


@dataclass(frozen=True, slots=True)
class _Move:
    """One permitted transition out of a position: where it lands + the exact path segments it
    emits. Every realizer segment carries its distinguishing ref (``realization_ref`` for R,
    ``bridge_fact_key`` for B/reposition) so downstream physical-plan-id material stays unambiguous.
    ``bridge_fact_key`` doubles as the frontier's same-bridge-never-twice cycle key; it is None
    for intra-catalog realizations."""
    next_position: _Position
    segments: tuple[BindingPathSegmentV1, ...]
    bridge_fact_key: str | None = None


def _table_columns(conn, catalog: str, table_ref: str) -> tuple[tuple[str, bool], ...]:
    """The ``(object_ref, is_grain)`` columns of the table at ``table_ref``, deterministically
    ordered. Addresses the table exactly the way ``object_grain`` does: the short ``table_name``
    plus an ``object_ref`` prefix guard."""
    rows = conn.execute(
        "SELECT object_ref, is_grain FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND kind = 'column' AND object_ref LIKE %s ORDER BY object_ref",
        (catalog, table_ref.rsplit(".", 1)[-1], table_ref + ".%")).fetchall()
    return tuple((r[0], bool(r[1])) for r in rows)


def _scoped_bridges(conn, entity_id: str, scope: CatalogScopeV1) -> tuple[ActiveBridgeV1, ...]:
    """The VERIFIED bridges at ``entity_id`` whose BOTH endpoint catalogs are authorized.
    Fail-closed: one out-of-scope endpoint disqualifies the bridge entirely — it is neither
    traversed nor revealed."""
    allowed = set(scope.authorized_catalog_sources)
    return tuple(b for b in active_bridges(conn)
                 if b.entity_id == entity_id
                 and b.left_catalog_source in allowed and b.right_catalog_source in allowed)


def _other_endpoint(bridge: ActiveBridgeV1, catalog: str, column_ref: str) -> tuple[str, str] | None:
    """The bridge endpoint OPPOSITE ``(catalog, column_ref)``. Bridges are UNORDERED/symmetric —
    the current endpoint may be stored left OR right — so both storage orders are normalized.
    None when neither endpoint is the current column (exact continuity: an endpoint on any other
    table/column is unusable from here)."""
    if (bridge.left_catalog_source, bridge.left_object_ref) == (catalog, column_ref):
        return bridge.right_catalog_source, bridge.right_object_ref
    if (bridge.right_catalog_source, bridge.right_object_ref) == (catalog, column_ref):
        return bridge.left_catalog_source, bridge.left_object_ref
    return None


def realize_in_place(conn, pos: _Position, hop: EntityRelationshipRefV1,
                     scope: CatalogScopeV1) -> tuple[_Move, ...]:
    """(R) Realize the semantic hop INSIDE the current catalog: a globally-bound VALID realization
    whose source table is exactly the current table and whose object-grain pair is exactly the hop.
    Emits ``semantic_rollup`` + ``intra_catalog_realization`` (with the realization's unique ref).
    Deterministic: sorted by ``(authority, realization_id)`` — APPROVED_JOIN before DECLARED_JOIN
    before INFERRED_JOIN. ``()`` when nothing matches."""
    if pos.catalog not in scope.authorized_catalog_sources:
        return ()   # fail closed: never derive from an unauthorized catalog
    matches = sorted(
        (r for r in derive_catalog_realizations(conn, pos.catalog).realizations
         if r.from_object_ref == pos.table_ref
         and r.from_object_grain == hop.from_entity and r.to_object_grain == hop.to_entity),
        key=lambda r: (r.authority, r.realization_id))
    return tuple(
        _Move(
            next_position=_Position(hop.to_entity, pos.catalog, r.to_object_ref),
            segments=(
                BindingPathSegmentV1(
                    segment_kind=SegmentKind.semantic_rollup, catalog_source=pos.catalog,
                    from_entity=hop.from_entity, to_entity=hop.to_entity,
                    cardinality=hop.cardinality),
                BindingPathSegmentV1(
                    segment_kind=SegmentKind.intra_catalog_realization, catalog_source=pos.catalog,
                    realization_ref=r.realization_id),
            ))
        for r in matches)


def rollup_bridges(conn, pos: _Position, hop: EntityRelationshipRefV1,
                   scope: CatalogScopeV1) -> tuple[_Move, ...]:
    """(B) Realize the semantic hop by CROSSING catalogs: the current table holds a
    ``hop.to_entity``-keyed FK column, a VERIFIED in-scope bridge at that entity is anchored on
    exactly that column, and the far endpoint's table is genuinely ``hop.to_entity``-grain.
    Emits ``semantic_rollup`` + ``governed_bridge`` (with the bridge's fact_key). Deterministic:
    sorted by ``(far_catalog, far_column_ref, fact_key)``. ``()`` when nothing matches."""
    if pos.entity != hop.from_entity:
        return ()   # self-guard: the physics never realizes a hop from a mismatched position
    if pos.catalog not in scope.authorized_catalog_sources:
        return ()
    bridges = _scoped_bridges(conn, hop.to_entity, scope)
    if not bridges:
        return ()
    keyed: list[tuple[tuple[str, str, str], _Move]] = []
    for col_ref, _is_grain in _table_columns(conn, pos.catalog, pos.table_ref):
        if key_entity(conn, pos.catalog, col_ref) != hop.to_entity:
            continue                        # not an E2-keyed FK on the CURRENT table
        for b in bridges:
            other = _other_endpoint(b, pos.catalog, col_ref)
            if other is None:
                continue                    # not anchored on this exact column (continuity)
            cat2, k2 = other
            far_table = table_of(k2)
            if object_grain(conn, cat2, far_table) != hop.to_entity:
                continue                    # the far table is not genuinely E2-grain
            keyed.append((
                (cat2, k2, b.fact_key),
                _Move(
                    next_position=_Position(hop.to_entity, cat2, far_table),
                    segments=(
                        BindingPathSegmentV1(
                            segment_kind=SegmentKind.semantic_rollup, catalog_source=pos.catalog,
                            from_entity=hop.from_entity, to_entity=hop.to_entity,
                            cardinality=hop.cardinality),
                        BindingPathSegmentV1(
                            segment_kind=SegmentKind.governed_bridge, catalog_source=cat2,
                            from_entity=hop.from_entity, to_entity=hop.to_entity,
                            bridge_fact_key=b.fact_key),
                    ),
                    bridge_fact_key=b.fact_key)))
    keyed.sort(key=lambda kv: kv[0])
    return tuple(m for _, m in keyed)


def reposition_bridges(conn, pos: _Position, scope: CatalogScopeV1) -> tuple[_Move, ...]:
    """Reposition: cross to ANOTHER catalog's table of the SAME entity grain without advancing a
    hop. Anchored on the current table's grain-key column (``is_grain`` AND keyed to
    ``pos.entity``); the far endpoint's table must be the same grain. Emits ONE
    ``governed_bridge`` segment (from_entity == to_entity == pos.entity, with the bridge's
    fact_key). Deterministic: sorted by ``(far_catalog, far_column_ref, fact_key)``. ``()`` when
    nothing matches."""
    if pos.catalog not in scope.authorized_catalog_sources:
        return ()
    bridges = _scoped_bridges(conn, pos.entity, scope)
    if not bridges:
        return ()
    keyed: list[tuple[tuple[str, str, str], _Move]] = []
    for col_ref, is_grain in _table_columns(conn, pos.catalog, pos.table_ref):
        if not is_grain or key_entity(conn, pos.catalog, col_ref) != pos.entity:
            continue                        # only THE grain-key column identifies rows to recross
        for b in bridges:
            other = _other_endpoint(b, pos.catalog, col_ref)
            if other is None:
                continue
            cat2, k2 = other
            far_table = table_of(k2)
            if object_grain(conn, cat2, far_table) != pos.entity:
                continue                    # the far table must hold the SAME grain
            keyed.append((
                (cat2, k2, b.fact_key),
                _Move(
                    next_position=_Position(pos.entity, cat2, far_table),
                    segments=(
                        BindingPathSegmentV1(
                            segment_kind=SegmentKind.governed_bridge, catalog_source=cat2,
                            from_entity=pos.entity, to_entity=pos.entity,
                            bridge_fact_key=b.fact_key),
                    ),
                    bridge_fact_key=b.fact_key)))
    keyed.sort(key=lambda kv: kv[0])
    return tuple(m for _, m in keyed)


# ---------------------------------------------------------------------------------------------
# Task B4 — the bounded frontier search + layered tier search + ranking + ambiguity.
#
# The search is a BFS over states LAYERED BY BRIDGE COUNT: every state at bridge_count=k is
# expanded before any at k+1, and once a tier yields >=1 complete path the tier is finished and
# NO deeper tier is expanded (fewest crossings win; `deeper_tiers_not_explored` records the cut).
# Within a tier the search is EXHAUSTIVE, never greedy — every permitted transition of every
# state is expanded in deterministic order, so a locally-valid realization that dead-ends can
# never prevent a bridge-first path from completing. Fail-closed: a state with no permitted
# transition becomes a first-class REJECTED candidate (missing_realization / unsanctioned_bridge
# / bounded_out_*) — a bridge or realizer segment is NEVER fabricated to force completion. All
# four bounds (bridges per plan, realizations per hop, complete paths, frontier states) are
# enforced and every truncation is recorded on the BoundingMetricsV1.
# ---------------------------------------------------------------------------------------------

_AUTHORITY_RANK = {RealizationAuthority.APPROVED_JOIN: 0, RealizationAuthority.DECLARED_JOIN: 1,
                   RealizationAuthority.INFERRED_JOIN: 2}


@dataclass(frozen=True, slots=True)
class _State:
    """One frontier state: how many semantic hops are realized, the EXACT physical position, the
    executable segments accumulated so far, the crossings spent, the catalogs touched, and the
    bridge facts consumed (the same bridge fact is never reused — cycle prevention)."""
    hop_index: int
    position: _Position
    segments: tuple[BindingPathSegmentV1, ...]
    bridge_count: int
    participating: tuple[str, ...]
    used_bridge_fact_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class AssemblyV1:
    """The per-(source-binding x semantic-path) assembly result: the COMPLETE plans (already
    ranked + classified), the fail-closed REJECTED candidates, and the bounding metrics."""
    complete: tuple[BindingPlanV1, ...]
    rejected: tuple[BindingPlanV1, ...]
    bounding: BoundingMetricsV1


def _plan_safety(bindings: tuple[IngredientBindingV1, ...]) -> BindingSafety:
    """Plan-level safety, fail-closed: safe only when EVERY ingredient binding is safe (an
    unevaluated binding is not presumed safe)."""
    return (BindingSafety.safe if all(b.safety is BindingSafety.safe for b in bindings)
            else BindingSafety.unsafe)


def _child(state: _State, move: _Move, *, advance: bool) -> _State:
    """The successor state of taking ``move``: hop advances for R/roll-up (not reposition), a
    crossing spends a bridge + marks its fact consumed, segments/participation accumulate."""
    used = (state.used_bridge_fact_keys | {move.bridge_fact_key}
            if move.bridge_fact_key is not None else state.used_bridge_fact_keys)
    participating = (state.participating if move.next_position.catalog in state.participating
                     else state.participating + (move.next_position.catalog,))
    return _State(
        hop_index=state.hop_index + (1 if advance else 0),
        position=move.next_position,
        segments=state.segments + move.segments,
        bridge_count=state.bridge_count + (0 if move.bridge_fact_key is None else 1),
        participating=participating,
        used_bridge_fact_keys=used)


def _hop_realizable_elsewhere(
        conn, hop: EntityRelationshipRefV1, scope: CatalogScopeV1, current_catalog: str,
        cache: dict[str, tuple[CatalogEntityRelationshipV1, ...]]) -> bool:
    """The dead-end taxonomy probe: does ANY OTHER authorized catalog hold a VALID realization of
    this hop? True -> the dead end is an UNSANCTIONED crossing (a realizer exists but no VERIFIED
    bridge reaches it); False -> missing_realization. Only in-scope catalogs are consulted —
    an inaccessible catalog is never probed, never revealed."""
    for cat in scope.authorized_catalog_sources:
        if cat == current_catalog:
            continue
        if cat not in cache:
            cache[cat] = derive_catalog_realizations(conn, cat).realizations
        if any(r.from_object_grain == hop.from_entity and r.to_object_grain == hop.to_entity
               for r in cache[cat]):
            return True
    return False


def assemble_paths(conn, *, source_position: _Position, semantic_path: EntitySemanticPathV1,
                   scope: CatalogScopeV1, ingredient_bindings: tuple[IngredientBindingV1, ...],
                   template: Template, target_entity: str) -> AssemblyV1:
    """The bounded frontier search for ONE (source binding x semantic path): realize every hop of
    ``semantic_path`` from ``source_position`` by (R) intra-catalog realization or (B) governed
    roll-up bridge, with same-entity repositions that may unlock a crossing. A state with every
    hop realized AND the target entity held is a COMPLETE executable path (an EXACT zero-hop path
    completes in place — the zero-bridge roll-up). Read-only, pure, deterministic."""
    hops = semantic_path.hops
    prefix = (BindingPathSegmentV1(segment_kind=SegmentKind.direct_catalog,
                                   catalog_source=source_position.catalog),)
    safety = _plan_safety(ingredient_bindings)

    def _mint(state: _State, *, resolution_status: PlanResolutionStatus,
              path_status: PathResolutionStatus, primary: ReasonCode | None,
              role: CandidateRole) -> BindingPlanV1:
        return make_binding_plan(
            recipe_id=template.id, target_entity=target_entity,
            catalog_source=source_position.catalog, ingredient_bindings=ingredient_bindings,
            path_segments=prefix + state.segments, resolution_status=resolution_status,
            path_resolution_status=path_status, primary_reason_code=primary,
            reason_codes=(primary,) if primary is not None else (), safety=safety,
            preference_rank=-1, preference_reasons=(), candidate_role=role)

    start = _State(hop_index=0, position=source_position, segments=(), bridge_count=0,
                   participating=(source_position.catalog,), used_bridge_fact_keys=frozenset())
    # one FIFO queue per bridge tier: levels[k] holds exactly the states that spent k crossings
    levels: tuple[list[_State], ...] = tuple([] for _ in range(MAX_BRIDGES_PER_PLAN + 1))
    levels[0].append(start)
    cursors = [0] * (MAX_BRIDGES_PER_PLAN + 1)
    visited: set[tuple[str, str, str, frozenset[str]]] = set()
    complete: list[BindingPlanV1] = []
    rejected: list[BindingPlanV1] = []
    realization_cache: dict[str, tuple[CatalogEntityRelationshipV1, ...]] = {}
    states_expanded = 0
    bridge_transitions = 0
    realizations_truncated = False
    bridge_transitions_truncated = False
    frontier_states_truncated = False
    paths_truncated = False

    stop = False
    level = 0
    while level <= MAX_BRIDGES_PER_PLAN and not stop:
        queue = levels[level]
        while cursors[level] < len(queue):
            state = queue[cursors[level]]
            cursors[level] += 1
            key = (state.position.entity, state.position.catalog, state.position.table_ref,
                   state.used_bridge_fact_keys)
            if key in visited:
                continue
            visited.add(key)
            if states_expanded >= MAX_STATES_EXPANDED_PER_BINDING:
                frontier_states_truncated = True
                stop = True
                break
            states_expanded += 1

            if state.hop_index == len(hops) and state.position.entity == target_entity:
                if len(complete) >= MAX_PHYSICAL_PATHS_PER_BINDING:
                    paths_truncated = True      # a further complete path was DROPPED, not kept
                    stop = True
                    break
                complete.append(_mint(
                    state, resolution_status=PlanResolutionStatus.resolved,
                    path_status=PathResolutionStatus.source_to_target_resolved, primary=None,
                    role=CandidateRole.rejected))   # provisional; rank_and_classify assigns roles
                continue                            # terminal success — never expanded further

            hop = hops[state.hop_index] if state.hop_index < len(hops) else None
            r_moves: tuple[_Move, ...] = ()
            roll_moves: tuple[_Move, ...] = ()
            if hop is not None:
                r_moves = realize_in_place(conn, state.position, hop, scope)
                if len(r_moves) > MAX_REALIZATIONS_PER_HOP:
                    realizations_truncated = True
                    # ReasonCode.bounded_out_max_realizations_per_hop is RESERVED for this cut. It is
                    # not attached to a candidate: the truncated state CONTINUES on the kept moves (no
                    # reject is minted here), so the cut is surfaced on the bounding metrics instead.
                    r_moves = r_moves[:MAX_REALIZATIONS_PER_HOP]
                roll_moves = rollup_bridges(conn, state.position, hop, scope)
            repo_moves = reposition_bridges(conn, state.position, scope)

            # cycle prevention: the same bridge fact is never crossed twice on one path
            usable_roll = [m for m in roll_moves
                           if m.bridge_fact_key not in state.used_bridge_fact_keys]
            usable_repo = [m for m in repo_moves
                           if m.bridge_fact_key not in state.used_bridge_fact_keys]
            budget_blocked = False
            if (usable_roll or usable_repo) and state.bridge_count >= MAX_BRIDGES_PER_PLAN:
                budget_blocked = True               # a crossing exists but the budget is spent
                bridge_transitions_truncated = True
                usable_roll, usable_repo = [], []

            if not r_moves and not usable_roll and not usable_repo:
                # fail-closed dead end -> a first-class rejected candidate carrying the evidence
                # trail it DID accumulate; a completing segment is NEVER fabricated.
                if budget_blocked:
                    status = PlanResolutionStatus.bounded_out
                    primary = ReasonCode.bounded_out_max_bridges
                elif hop is not None and _hop_realizable_elsewhere(
                        conn, hop, scope, state.position.catalog, realization_cache):
                    status = PlanResolutionStatus.unresolved
                    primary = ReasonCode.unsanctioned_bridge
                else:
                    status = PlanResolutionStatus.unresolved
                    primary = ReasonCode.missing_realization
                rejected.append(_mint(
                    state, resolution_status=status,
                    path_status=PathResolutionStatus.source_to_target_rejected, primary=primary,
                    role=CandidateRole.rejected))
                continue

            for m in r_moves:                       # same tier: the hop advances, no crossing
                queue.append(_child(state, m, advance=True))
            bridge_transitions += len(usable_roll) + len(usable_repo)
            for m in usable_roll:                   # next tier: the hop advances over a crossing
                levels[state.bridge_count + 1].append(_child(state, m, advance=True))
            for m in usable_repo:                   # next tier: same entity, crossing only
                levels[state.bridge_count + 1].append(_child(state, m, advance=False))
        if stop:
            break
        if complete:
            break       # whole-tier completion: this tier resolved; deeper tiers are NOT expanded
        level += 1

    deeper_tiers_not_explored = any(
        cursors[j] < len(levels[j]) for j in range(level + 1, MAX_BRIDGES_PER_PLAN + 1))
    if not complete and frontier_states_truncated:
        # bounded out with nothing complete -> an explicit rejected candidate, never a silent drop
        rejected.append(_mint(
            start, resolution_status=PlanResolutionStatus.bounded_out,
            path_status=PathResolutionStatus.source_to_target_rejected,
            primary=ReasonCode.bounded_out_max_frontier_states, role=CandidateRole.rejected))

    bounding = BoundingMetricsV1(
        candidate_columns_truncated=False, combinations_truncated=False,
        plans_truncated=paths_truncated,
        catalog_consideration_truncated=scope.catalog_consideration_truncated,
        total_candidate_columns_considered=0, total_combinations_explored=0,
        total_plans_preserved=len(complete) + len(rejected),
        realizations_truncated=realizations_truncated,
        bridge_transitions_truncated=bridge_transitions_truncated,
        frontier_states_truncated=frontier_states_truncated,
        deeper_tiers_not_explored=deeper_tiers_not_explored,
        total_states_expanded=states_expanded,
        total_bridge_transitions_explored=bridge_transitions)
    return AssemblyV1(complete=rank_and_classify(conn, tuple(complete)),
                      rejected=tuple(rejected), bounding=bounding)


def _authority_rank_lookup(conn, plans: Sequence[BindingPlanV1]) -> dict[tuple[str, str], int]:
    """(catalog, realization_id) -> authority rank for every realization catalog the plans touch,
    derived from the same governed source the transitions used. Deterministic (catalogs sorted)."""
    cats = sorted({s.catalog_source for p in plans for s in p.path_segments
                   if s.segment_kind is SegmentKind.intra_catalog_realization})
    lookup: dict[tuple[str, str], int] = {}
    for cat in cats:
        for r in derive_catalog_realizations(conn, cat).realizations:
            lookup[(cat, r.realization_id)] = _AUTHORITY_RANK[r.authority]
    return lookup


def _rank_key(p: BindingPlanV1, authority: dict[tuple[str, str], int]) -> tuple[int, int, int, int, int]:
    """The FULL ranking precedence (best-first, physical_plan_id excluded): validity/safety -> bridge_count
    -> ingredient-binding rank (order.py's worst-binding quality) -> semantic-path rank (fewer
    realized hops) -> physical-realization rank (worst realizer authority). Fail-closed: a realizer
    the governed lookup cannot resolve ranks WORST (INFERRED_JOIN-level), never APPROVED-best. A
    pure-bridge path has no realizer segment and ranks neutrally at 0 (the `default=`)."""
    return (
        0 if p.safety is BindingSafety.safe else 1,
        p.bridge_count,
        _agg_quality(p),
        sum(1 for s in p.path_segments if s.segment_kind is SegmentKind.semantic_rollup),
        max((authority.get((s.catalog_source, s.realization_ref or ""),
                           _AUTHORITY_RANK[RealizationAuthority.INFERRED_JOIN])
             for s in p.path_segments
             if s.segment_kind is SegmentKind.intra_catalog_realization), default=0),
    )


def rank_and_classify(conn, complete_plans: Sequence[BindingPlanV1]) -> tuple[BindingPlanV1, ...]:
    """Order the COMPLETE plans by the full precedence with the canonical physical_plan_id tie-break,
    then classify: the single best is ``selected``; a plan tying the best on the FULL key except
    physical_plan_id is an ``equal_rank_alternative`` and the tie marks every tied plan
    ``resolved_with_ambiguity`` (an ambiguous binding is surfaced, never silently picked); a
    strictly lower complete plan is a ``lower_rank_alternative``. Idempotent under re-ranking."""
    if not complete_plans:
        return ()
    authority = _authority_rank_lookup(conn, complete_plans)
    ordered = sorted(complete_plans, key=lambda p: (_rank_key(p, authority), p.physical_plan_id))
    top = _rank_key(ordered[0], authority)
    ambiguous = len(ordered) > 1 and _rank_key(ordered[1], authority) == top
    out: list[BindingPlanV1] = []
    for i, p in enumerate(ordered):
        k = _rank_key(p, authority)
        at_top = k == top
        role = (CandidateRole.selected if i == 0
                else CandidateRole.equal_rank_alternative if at_top
                else CandidateRole.lower_rank_alternative)
        if ambiguous and at_top:
            status = PlanResolutionStatus.resolved_with_ambiguity
        elif p.resolution_status is PlanResolutionStatus.resolved_with_ambiguity:
            status = PlanResolutionStatus.resolved      # normalize on re-rank: no stale ambiguity
        else:
            status = p.resolution_status
        codes = p.reason_codes
        if ambiguous and at_top \
                and ReasonCode.ambiguous_equal_cross_catalog_paths not in codes:
            codes = codes + (ReasonCode.ambiguous_equal_cross_catalog_paths,)
        elif not (ambiguous and at_top):
            codes = tuple(c for c in codes
                          if c is not ReasonCode.ambiguous_equal_cross_catalog_paths)
        out.append(replace(
            p, candidate_role=role, resolution_status=status, reason_codes=codes,
            preference_rank=i,
            preference_reasons=(f"safety={k[0]}", f"bridge_count={k[1]}",
                                f"binding_quality={k[2]}", f"path_hops={k[3]}",
                                f"realizer_authority={k[4]}")))
    return tuple(out)
