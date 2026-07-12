"""Phase-3A — entity-relationship contracts, enums, result types, and structural validators.

The GLOBAL semantic relationship (:class:`EntityRelationshipDefinitionV1`) is the ONLY edge class the
3A graph traverses. The catalog-realization / entity-bridge / proposal contracts are defined + validated
here so Phase 3B builds against stable, self-consistent types — 3A never populates or traverses them.
Validators are pure and structural (a bridge's entity is known, a realization's refs differ, …); the
cross-check of a realization against the global registry is a Phase-3B concern.

``EntityCompatibility`` lives here (not in ``ranking_signals``) so the graph resolver and the ranking
adapter both import it without a cycle; ``ranking_signals`` re-exports it for its existing callers."""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def is_semver(value: str) -> bool:
    """Shared N.N.N check (used by the definition validator AND the graph builder)."""
    return bool(_SEMVER.match(value))


class EntityCompatibility(StrEnum):
    """Soft grain fit of a recipe to a confirmed ``target_entity`` (Phase-2B semantics, now graph-backed).
    ``EXACT`` grain == target; ``DERIVABLE`` a single roll-up path reaches it; ``AMBIGUOUS`` several
    distinct paths do (reserved — the 3A seed forest never emits it); ``UNKNOWN`` no target / no path /
    out-of-vocabulary. No ``INCOMPATIBLE`` — a hard entity reject is deferred to Phase 3D."""

    EXACT = "exact"
    DERIVABLE = "derivable"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class RelationshipType(StrEnum):
    ROLLUP = "rollup"
    PARENT_CHILD = "parent_child"
    OWNERSHIP = "ownership"
    MEMBERSHIP = "membership"
    IDENTITY = "identity"       # reserved for bridge/realization use; NOT a valid global self-edge


class Cardinality(StrEnum):
    ONE_TO_ONE = "one_to_one"
    ONE_TO_MANY = "one_to_many"
    MANY_TO_ONE = "many_to_one"
    MANY_TO_MANY = "many_to_many"


class TraversalDirection(StrEnum):
    FORWARD = "forward"
    REVERSE = "reverse"          # reserved for 3B; a non-FORWARD ACTIVE edge is rejected in 3A
    BOTH = "both"               # reserved for 3B


class AggregationStrategy(StrEnum):
    """WHO/whether an aggregation is declared for a roll-up. 3A carries the strategy only — the actual
    function (avg/sum/window) is a Phase-3B recipe concern, so the relationship never over-declares a
    measure-specific aggregation."""

    NOT_APPLICABLE = "not_applicable"    # a non-aggregating relationship (e.g. 1:1)
    RECIPE_DECLARED = "recipe_declared"  # aggregation required; the recipe declares the function (3B)


class RelationshipStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"


class RelationshipProposalStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class GraphEdgeAuthority(StrEnum):
    GLOBAL_ENTITY_MODEL = "global_entity_model"
    CATALOG_DECLARED = "catalog_declared"
    ENTITY_BRIDGE = "entity_bridge"


class RealizationAuthority(StrEnum):
    """The authority behind a catalog realization. ``APPROVED_JOIN`` = an attested approved_join fact;
    ``DECLARED_JOIN`` = an uploaded ``graph_edge`` join (what existing single-catalog grounding uses);
    ``INFERRED_JOIN`` = metadata-inferred. Stamped in 3B; which levels are VALID-capable is enforced in
    3C — 3B never blocks on it."""

    APPROVED_JOIN = "approved_join"
    DECLARED_JOIN = "declared_join"
    INFERRED_JOIN = "inferred_join"


@dataclass(frozen=True, slots=True)
class EntityRelationshipDefinitionV1:
    """A GLOBAL semantic entity relationship — the only edge class the 3A graph traverses. Answers 'is
    grain ``from_entity`` semantically derivable into ``to_entity``?' and states WHETHER a roll-up needs
    aggregation (``aggregation_required``) and who declares it (``aggregation_strategy``). It carries NO
    physical column mapping and NO measure-specific aggregation list."""

    relationship_id: str
    from_entity: str
    to_entity: str
    relationship_type: RelationshipType
    cardinality: Cardinality
    traversal_direction: TraversalDirection
    aggregation_required: bool
    aggregation_strategy: AggregationStrategy
    status: RelationshipStatus
    version: str


@dataclass(frozen=True, slots=True)
class CatalogEntityRelationshipV1:
    """How one catalog physically realizes a global relationship. The semantic hop it realizes is
    ``from_object_grain -> to_object_grain`` (each = the entity of its table's is_grain column), realized
    by the join KEY (``from_key_ref``/``to_key_ref`` + their entities). Object grain and join-key entity
    are DISTINCT (a join on ``customer_id`` can realize ``account -> customer``). Derived from declared
    joins in Phase 3B.2A; nothing populated it in 3A (safe to extend).

    ``declared_cardinality`` is in the PHYSICAL (authored) orientation — the fanout of
    ``from_object_grain -> to_object_grain``, matching the stored key refs. ``reversed_authoring=True``
    means that physical orientation is the REVERSE of ``relationship_id``'s canonical orientation (so the
    canonical relationship's own cardinality is ``invert_cardinality(declared_cardinality)``)."""

    realization_id: str
    relationship_id: str
    catalog_source: str
    from_object_ref: str
    from_object_grain: str
    to_object_ref: str
    to_object_grain: str
    from_key_ref: str
    from_key_entity: str
    to_key_ref: str
    to_key_entity: str
    declared_cardinality: Cardinality
    authority: RealizationAuthority = RealizationAuthority.DECLARED_JOIN
    status: RelationshipStatus = RelationshipStatus.ACTIVE
    reversed_authoring: bool = False


@dataclass(frozen=True, slots=True)
class EntityBridgeV1:
    """CONTRACT ONLY in 3A. A sanctioned cross-catalog identity link: two catalog-local representations
    of the SAME entity. Bridge IDENTITY is UNORDERED — ``(A:x ↔ B:y)`` and ``(B:y ↔ A:x)`` denote the
    same bridge; Phase 3B canonicalizes endpoints for duplicate detection. Governed activation is Phase
    3B (today bridges are computed permissively)."""

    bridge_id: str
    entity_id: str
    left_catalog_source: str
    left_object_ref: str
    right_catalog_source: str
    right_object_ref: str
    authority: GraphEdgeAuthority = GraphEdgeAuthority.ENTITY_BRIDGE
    status: RelationshipStatus = RelationshipStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class EntityRelationshipProposalV1:
    """CONTRACT ONLY in 3A, and NEVER traversed. A metadata-derived candidate — evidence, not truth.
    ``ACCEPTED`` status does NOT make it traversable; promotion is a Phase-3B governance step."""

    proposal_id: str
    proposed_from_entity: str
    proposed_to_entity: str
    proposed_cardinality: Cardinality
    evidence_refs: tuple[str, ...]
    source_catalog: str
    inferred_by: str
    status: RelationshipProposalStatus


@dataclass(frozen=True, slots=True)
class EntityRelationshipRefV1:
    """One hop in a resolved semantic path: the relationship (with the version traversed) + its roll-up
    aggregation semantics."""

    relationship_id: str
    relationship_version: str
    from_entity: str
    to_entity: str
    cardinality: Cardinality
    aggregation_required: bool
    aggregation_strategy: AggregationStrategy


@dataclass(frozen=True, slots=True)
class EntitySemanticPathV1:
    hops: tuple[EntityRelationshipRefV1, ...]


@dataclass(frozen=True, slots=True)
class EntityCompatibilityResultV1:
    """``paths`` is ``()`` for EXACT/UNKNOWN, one for DERIVABLE, ≥2 for AMBIGUOUS. ``paths_truncated`` is
    True when path enumeration hit its bound (≥2 paths — enough to classify). ``graph_version`` stamps the
    registry composition the result came from."""

    status: EntityCompatibility
    source_entity: str
    target_entity: str
    paths: tuple[EntitySemanticPathV1, ...]
    reason_codes: tuple[str, ...]
    graph_version: str
    paths_truncated: bool = False


def _nonempty(**fields: str) -> None:
    for name, value in fields.items():
        if not value or not value.strip():
            raise ValueError(f"empty {name}")


def validate_relationship_definition(
    defn: EntityRelationshipDefinitionV1, *, known: frozenset[str]) -> None:
    """Structural guard over ONE global semantic definition. Raises ``ValueError`` on: an endpoint outside
    the closed vocabulary; ANY self-edge (the EXACT short-circuit handles identity — a self roll-up is
    redundant); a non-``FORWARD`` traversal direction (3A supports forward semantic edges only); an
    aggregation_required/strategy mismatch; a non-semver ``version``. Duplicate ids/edges and cycles are
    graph-build concerns."""
    _nonempty(relationship_id=defn.relationship_id, from_entity=defn.from_entity,
              to_entity=defn.to_entity, version=defn.version)
    if defn.from_entity not in known:
        raise ValueError(f"unknown entity: {defn.from_entity!r}")
    if defn.to_entity not in known:
        raise ValueError(f"unknown entity: {defn.to_entity!r}")
    if defn.from_entity == defn.to_entity:
        raise ValueError(f"self-relationship not allowed: {defn.from_entity!r}")
    if defn.traversal_direction is not TraversalDirection.FORWARD:
        raise ValueError("only forward active semantic edges are supported in 3A")
    required = defn.aggregation_required
    applicable = defn.aggregation_strategy is not AggregationStrategy.NOT_APPLICABLE
    if required != applicable:
        raise ValueError("aggregation_required must match a non-NOT_APPLICABLE aggregation_strategy")
    if not is_semver(defn.version):
        raise ValueError(f"invalid version: {defn.version!r} (expected N.N.N)")


def validate_catalog_relationship(real: CatalogEntityRelationshipV1, *, known: frozenset[str]) -> None:
    """Structural guard: non-empty refs, distinct object endpoints, and every resolved entity (both
    object grains + both key entities) in the closed vocabulary. It does NOT cross-check the realization
    against the global relationship (that is the derivation's job in 3B.2A)."""
    _nonempty(realization_id=real.realization_id, relationship_id=real.relationship_id,
              catalog_source=real.catalog_source, from_object_ref=real.from_object_ref,
              to_object_ref=real.to_object_ref, from_key_ref=real.from_key_ref,
              to_key_ref=real.to_key_ref)
    if real.from_object_ref == real.to_object_ref:
        raise ValueError("catalog realization object endpoints are identical")
    for label, ent in (("from_object_grain", real.from_object_grain),
                       ("to_object_grain", real.to_object_grain),
                       ("from_key_entity", real.from_key_entity),
                       ("to_key_entity", real.to_key_entity)):
        if ent not in known:
            raise ValueError(f"unknown entity ({label}): {ent!r}")


def validate_entity_bridge(bridge: EntityBridgeV1, *, known: frozenset[str]) -> None:
    _nonempty(bridge_id=bridge.bridge_id, entity_id=bridge.entity_id,
              left_catalog_source=bridge.left_catalog_source,
              right_catalog_source=bridge.right_catalog_source,
              left_object_ref=bridge.left_object_ref, right_object_ref=bridge.right_object_ref)
    if bridge.entity_id not in known:
        raise ValueError(f"unknown entity: {bridge.entity_id!r}")
    if bridge.left_catalog_source == bridge.right_catalog_source:
        raise ValueError("a bridge must span two distinct catalog sources")
    if bridge.authority is not GraphEdgeAuthority.ENTITY_BRIDGE:
        raise ValueError("bridge authority must be ENTITY_BRIDGE")


def validate_relationship_proposal(
    prop: EntityRelationshipProposalV1, *, known: frozenset[str]) -> None:
    _nonempty(proposal_id=prop.proposal_id, source_catalog=prop.source_catalog,
              inferred_by=prop.inferred_by, proposed_from_entity=prop.proposed_from_entity,
              proposed_to_entity=prop.proposed_to_entity)
    if prop.proposed_from_entity not in known:
        raise ValueError(f"unknown entity: {prop.proposed_from_entity!r}")
    if prop.proposed_to_entity not in known:
        raise ValueError(f"unknown entity: {prop.proposed_to_entity!r}")
    if prop.proposed_from_entity == prop.proposed_to_entity:
        raise ValueError("self-relationship proposal is not allowed; use an entity-bridge proposal")
    if not prop.evidence_refs:
        raise ValueError("a proposal needs at least one evidence ref")
