"""Phase-3A — the immutable global semantic entity graph + bounded compatibility traversal.

Built ONCE from the curated registry. Only active FORWARD :class:`EntityRelationshipDefinitionV1` edges
are indexed. The builder rejects invalid definitions, duplicate ids, duplicate semantic edges, non-
FORWARD active edges, and directed CYCLES (a semantic cycle is a contradictory grain model, not merely a
traversal hazard). Outgoing edges are stored sorted by ``relationship_id`` so traversal is deterministic.
The closed entity vocabulary is carried on the graph so the resolver can fail out-of-vocab entities to
UNKNOWN (never EXACT)."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_registry import (
    ENTITY_RELATIONSHIPS_V1,
    GRAPH_VERSION,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityCompatibility,
    EntityCompatibilityResultV1,
    EntityRelationshipDefinitionV1,
    EntityRelationshipRefV1,
    EntitySemanticPathV1,
    RelationshipStatus,
    TraversalDirection,
    is_semver,
    validate_relationship_definition,
)


@dataclass(frozen=True, slots=True)
class EntityGraph:
    """An immutable adjacency of active semantic relationships + the closed entity vocabulary it was built
    over. ``outgoing(entity)`` returns the entity's active outgoing edges, sorted by ``relationship_id``.
    PRODUCTION GRAPHS MUST come from :func:`build_entity_graph` — direct construction bypasses every
    invariant (cycle/duplicate/direction/vocabulary) and is test-only."""

    version: str
    known_entities: frozenset[str]
    _adjacency: Mapping[str, tuple[EntityRelationshipDefinitionV1, ...]]

    def outgoing(self, entity: str) -> tuple[EntityRelationshipDefinitionV1, ...]:
        return self._adjacency.get(entity, ())


def _reject_cycles(adjacency: Mapping[str, tuple[EntityRelationshipDefinitionV1, ...]]) -> None:
    """Raise ``ValueError`` on any directed cycle among active edges. Recursive three-colour DFS: a node
    on the ACTIVE recursion path (``visiting``) reached again is a back edge → a cycle; a fully-explored
    node (``visited``) reached again is a shared descendant of a converging DAG → NOT a cycle. The curated
    graph is tiny, so recursion depth is trivial."""
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError(f"semantic cycle through {node!r}")
        if node in visited:
            return
        visiting.add(node)
        for edge in adjacency.get(node, ()):
            visit(edge.to_entity)
        visiting.discard(node)
        visited.add(node)

    for node in sorted(adjacency):
        visit(node)


def build_entity_graph(
    defs: tuple[EntityRelationshipDefinitionV1, ...], *, version: str, known: frozenset[str],
) -> EntityGraph:
    """Validate + index only ACTIVE definitions (deprecated ones are archived, neither validated nor
    traversed — a deliberate active-only model). Reject duplicate active ids, duplicate active semantic
    edges ``(from, to, type, direction)``, non-FORWARD active edges, and directed cycles; index by
    ``from_entity`` (sorted). Fails fast at import for the seed."""
    if not is_semver(version):
        raise ValueError(f"invalid graph version: {version!r} (expected N.N.N)")
    seen_ids: set[str] = set()
    seen_semantic: set[tuple[str, str, str, str]] = set()
    by_source: dict[str, list[EntityRelationshipDefinitionV1]] = {}
    for d in defs:
        if d.status is not RelationshipStatus.ACTIVE:
            continue                                    # archived; not validated or indexed in 3A
        validate_relationship_definition(d, known=known)
        if d.traversal_direction is not TraversalDirection.FORWARD:
            raise ValueError(f"only forward active edges supported in 3A: {d.relationship_id!r}")
        if d.relationship_id in seen_ids:
            raise ValueError(f"duplicate active relationship id: {d.relationship_id!r}")
        seen_ids.add(d.relationship_id)
        key = (d.from_entity, d.to_entity, d.relationship_type.value, d.traversal_direction.value)
        if key in seen_semantic:
            raise ValueError(f"duplicate semantic edge: {d.from_entity!r}->{d.to_entity!r}")
        seen_semantic.add(key)
        by_source.setdefault(d.from_entity, []).append(d)
    adjacency = {
        src: tuple(sorted(edges, key=lambda e: e.relationship_id))
        for src, edges in by_source.items()}
    _reject_cycles(adjacency)
    return EntityGraph(
        version=version, known_entities=known, _adjacency=MappingProxyType(adjacency))


def _ref(d: EntityRelationshipDefinitionV1) -> EntityRelationshipRefV1:
    return EntityRelationshipRefV1(
        relationship_id=d.relationship_id, relationship_version=d.version, from_entity=d.from_entity,
        to_entity=d.to_entity, cardinality=d.cardinality, aggregation_required=d.aggregation_required,
        aggregation_strategy=d.aggregation_strategy)


# Enough paths to classify DERIVABLE vs AMBIGUOUS. Not a public knob — no consumer configures it.
_MAX_COMPATIBILITY_PATHS = 2


def _bounded_simple_paths(
    graph: EntityGraph, source: str, target: str, *, limit: int,
) -> list[tuple[EntityRelationshipDefinitionV1, ...]]:
    """Up to ``limit`` simple directed paths ``source → target`` over active forward edges. Cycle-safe via
    a visited set (defense in depth — the builder already rejects cycles); deterministic because outgoing
    edges are pre-sorted. Stops once ``limit`` paths are found."""
    results: list[tuple[EntityRelationshipDefinitionV1, ...]] = []

    def _walk(node: str, path: tuple[EntityRelationshipDefinitionV1, ...], visited: frozenset[str]) -> None:
        if len(results) >= limit:
            return
        if node == target:
            results.append(path)
            return
        for edge in graph.outgoing(node):
            if len(results) >= limit:
                return
            nxt = edge.to_entity
            if nxt in visited:
                continue
            _walk(nxt, (*path, edge), visited | {nxt})

    _walk(source, (), frozenset({source}))
    return results


def _path_identity(path: tuple[EntityRelationshipDefinitionV1, ...]) -> tuple[str, ...]:
    """Two paths are DISTINCT iff their ordered relationship-ids differ. (With simple-path enumeration +
    the builder's duplicate-edge rejection this is already 1:1 with the edge sequence; the dedup is an
    explicit, defensive statement of the equivalence contract.)"""
    return tuple(edge.relationship_id for edge in path)


def resolve_entity_compatibility(
    source: str, target: str, graph: EntityGraph) -> EntityCompatibilityResultV1:
    """Graph-backed grain compatibility. Out-of-vocabulary ``source``/``target`` → UNKNOWN (NEVER EXACT).
    ``source == target`` (both known) → EXACT; exactly one directed path → DERIVABLE; ≥2 distinct paths →
    AMBIGUOUS (surfaced, never a shortest-path pick); no path → UNKNOWN. Enumeration is bounded to
    ``_MAX_COMPATIBILITY_PATHS + 1`` so ``paths_truncated`` reports whether MORE than the visible paths
    exist (exactly two paths → not truncated; three-plus → truncated). Never raises."""
    def _unknown(*codes: str) -> EntityCompatibilityResultV1:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.UNKNOWN, source_entity=source, target_entity=target,
            paths=(), reason_codes=codes, graph_version=graph.version)

    if source not in graph.known_entities:
        return _unknown("unknown_source_entity")
    if target not in graph.known_entities:
        return _unknown("unknown_target_entity")
    if source == target:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.EXACT, source_entity=source, target_entity=target,
            paths=(), reason_codes=(), graph_version=graph.version)
    raw = _bounded_simple_paths(graph, source, target, limit=_MAX_COMPATIBILITY_PATHS + 1)
    seen: set[tuple[str, ...]] = set()
    distinct: list[tuple[EntityRelationshipDefinitionV1, ...]] = []
    for p in raw:
        ident = _path_identity(p)
        if ident in seen:
            continue
        seen.add(ident)
        distinct.append(p)
    truncated = len(distinct) > _MAX_COMPATIBILITY_PATHS
    visible = distinct[:_MAX_COMPATIBILITY_PATHS]
    paths = tuple(EntitySemanticPathV1(hops=tuple(_ref(e) for e in p)) for p in visible)
    if not paths:
        return _unknown("no_entity_path")
    if len(paths) == 1:
        return EntityCompatibilityResultV1(
            status=EntityCompatibility.DERIVABLE, source_entity=source, target_entity=target,
            paths=paths, reason_codes=(), graph_version=graph.version, paths_truncated=False)
    return EntityCompatibilityResultV1(
        status=EntityCompatibility.AMBIGUOUS, source_entity=source, target_entity=target,
        paths=paths, reason_codes=("multiple_entity_paths",), graph_version=graph.version,
        paths_truncated=truncated)


# Built ONCE at import from the curated registry — the single active graph in 3A. Fails fast if the
# registry is malformed (cycle / duplicate / unknown endpoint / bad version).
ENTITY_GRAPH: EntityGraph = build_entity_graph(
    ENTITY_RELATIONSHIPS_V1, version=GRAPH_VERSION, known=known_entities())
