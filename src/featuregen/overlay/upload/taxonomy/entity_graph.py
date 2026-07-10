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

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityRelationshipDefinitionV1,
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
            raise ValueError(f"only FORWARD active edges supported in 3A: {d.relationship_id!r}")
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
