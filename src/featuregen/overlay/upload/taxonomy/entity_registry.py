"""Phase-3A — the curated, versioned GLOBAL semantic entity-relationship registry.

Seeded with EXACTLY the five roll-ups Phase-2B's ``_ENTITY_ROLLUP`` expressed — acyclic, each source
out-degree <=1, so the graph is regression-equivalent and never emits ``AMBIGUOUS``. Each roll-up
requires aggregation whose function the RECIPE declares in Phase 3B (``RECIPE_DECLARED``) — the
relationship never over-declares a measure-specific aggregation list. In-code + version-controlled; no
DB in 3A. New relationships that could create a second path for an existing pair are a Phase-3D concern.

These five definitions encode the semantic ASSUMPTIONS already embedded in ``_ENTITY_ROLLUP``; they are
compatibility-preserving DEFAULTS, not proof that every catalog physically realizes the relationship
with the declared ``MANY_TO_ONE`` cardinality. Real-world exceptions (joint accounts, multi-policyholder
policies, facilities with several obligors) are catalog-realization concerns that Phase 3B validates and
FAILS CLOSED on — 3A does not assert these cardinalities as universal banking truths.

NOTE (deferred, Phase-3B/growth): a content fingerprint (sha256 over canonicalized definitions) paired
with ``GRAPH_VERSION`` would catch a definition change made without bumping the version. Omitted for a
five-entry seed; add it when the registry grows."""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)

GRAPH_VERSION = "1.0.0"


def _rollup(relationship_id: str, from_entity: str, to_entity: str) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=relationship_id, from_entity=from_entity, to_entity=to_entity,
        relationship_type=RelationshipType.ROLLUP, cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=RelationshipStatus.ACTIVE,
        version="1.0.0")


ENTITY_RELATIONSHIPS_V1: tuple[EntityRelationshipDefinitionV1, ...] = (
    _rollup("account_to_customer", "account", "customer"),
    _rollup("card_account_to_customer", "card_account", "customer"),
    _rollup("transaction_to_account", "transaction", "account"),
    _rollup("facility_to_obligor", "facility", "obligor"),
    _rollup("policy_to_customer", "policy", "customer"),
)


def global_relationship_for(
    from_entity: str, to_entity: str) -> EntityRelationshipDefinitionV1 | None:
    """The active global relationship for a directed grain pair, or None. Directed: ``account->customer``
    is a relationship; ``customer->account`` is not (the reverse must be handled by the caller)."""
    for d in ENTITY_RELATIONSHIPS_V1:
        if d.status is RelationshipStatus.ACTIVE \
                and d.from_entity == from_entity and d.to_entity == to_entity:
            return d
    return None
