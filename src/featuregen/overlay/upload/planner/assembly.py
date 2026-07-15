"""Phase-3B.3b — cross-catalog assembly: eligibility, source-entity resolution, semantic paths, the
physical-transition physics, and the bounded frontier search. Read-only, deterministic."""
from __future__ import annotations

from dataclasses import dataclass

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
    BindingPathSegmentV1,
    CatalogScopeV1,
    ReasonCode,
    SegmentKind,
)
from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityCompatibility,
    EntityRelationshipRefV1,
    EntitySemanticPathV1,
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
    ``bridge_fact_key`` for B/reposition) so downstream plan_id material stays unambiguous.
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
