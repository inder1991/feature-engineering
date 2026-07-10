"""Phase-3A Task 3A.1 — the entity-relationship contracts + structural validators.

Only EntityRelationshipDefinitionV1 is an active graph edge in 3A; the other three contracts are
defined + STRUCTURALLY validated here (no global-registry cross-check — that is 3B) so 3B builds against
stable, self-consistent types."""
from __future__ import annotations

from collections import Counter

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_registry import (
    ENTITY_RELATIONSHIPS_V1,
    GRAPH_VERSION,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    EntityCompatibility,
    EntityRelationshipDefinitionV1,
    EntityRelationshipProposalV1,
    GraphEdgeAuthority,
    RelationshipProposalStatus,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
    validate_catalog_relationship,
    validate_entity_bridge,
    validate_relationship_definition,
    validate_relationship_proposal,
)

KNOWN = known_entities()


def _defn(**overrides) -> EntityRelationshipDefinitionV1:
    base = dict(
        relationship_id="account_to_customer", from_entity="account", to_entity="customer",
        relationship_type=RelationshipType.ROLLUP, cardinality=Cardinality.MANY_TO_ONE,
        traversal_direction=TraversalDirection.FORWARD, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=RelationshipStatus.ACTIVE,
        version="1.0.0")
    base.update(overrides)
    return EntityRelationshipDefinitionV1(**base)


def test_ambiguous_member_present_incompatible_absent():
    assert EntityCompatibility.AMBIGUOUS.value == "ambiguous"
    assert not hasattr(EntityCompatibility, "INCOMPATIBLE")   # hard reject deferred to 3D


def test_valid_definition_passes():
    validate_relationship_definition(_defn(), known=KNOWN)


def test_dangling_endpoint_rejected():
    with pytest.raises(ValueError, match="unknown entity"):
        validate_relationship_definition(_defn(to_entity="not_an_entity"), known=KNOWN)


def test_all_self_edges_rejected():
    # No identity exception: the EXACT short-circuit handles entity identity; a self roll-up is redundant.
    with pytest.raises(ValueError, match="self-relationship"):
        validate_relationship_definition(_defn(from_entity="customer", to_entity="customer"), known=KNOWN)
    with pytest.raises(ValueError, match="self-relationship"):
        validate_relationship_definition(
            _defn(from_entity="customer", to_entity="customer",
                  relationship_type=RelationshipType.IDENTITY), known=KNOWN)


def test_rollup_must_be_forward():
    with pytest.raises(ValueError, match="forward"):
        validate_relationship_definition(_defn(traversal_direction=TraversalDirection.BOTH), known=KNOWN)


def test_aggregation_required_must_have_strategy():
    with pytest.raises(ValueError, match="aggregation"):
        validate_relationship_definition(
            _defn(aggregation_required=True, aggregation_strategy=AggregationStrategy.NOT_APPLICABLE),
            known=KNOWN)
    with pytest.raises(ValueError, match="aggregation"):
        validate_relationship_definition(
            _defn(aggregation_required=False, aggregation_strategy=AggregationStrategy.RECIPE_DECLARED),
            known=KNOWN)


def test_invalid_version_rejected():
    with pytest.raises(ValueError, match="version"):
        validate_relationship_definition(_defn(version="v1"), known=KNOWN)


def _catalog(**overrides) -> CatalogEntityRelationshipV1:
    base = dict(
        realization_id="core_accounts:accounts.account_id->accounts.customer_id",
        relationship_id="account_to_customer", catalog_source="core_accounts",
        from_object_ref="accounts.account_id", to_object_ref="accounts.customer_id",
        resolved_from_entity="account", resolved_to_entity="customer",
        declared_cardinality=Cardinality.MANY_TO_ONE, adapter_id="core_banking_adapter",
        authority=GraphEdgeAuthority.CATALOG_DECLARED, status=RelationshipStatus.ACTIVE)
    base.update(overrides)
    return CatalogEntityRelationshipV1(**base)


def test_catalog_relationship_validation():
    validate_catalog_relationship(_catalog(), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):
        validate_catalog_relationship(_catalog(catalog_source=""), known=KNOWN)
    with pytest.raises(ValueError, match="empty"):        # whitespace-only is empty
        validate_catalog_relationship(_catalog(adapter_id="   "), known=KNOWN)
    with pytest.raises(ValueError, match="identical"):
        validate_catalog_relationship(_catalog(to_object_ref="accounts.account_id"), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_catalog_relationship(_catalog(resolved_to_entity="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="authority"):
        validate_catalog_relationship(_catalog(authority=GraphEdgeAuthority.ENTITY_BRIDGE), known=KNOWN)


def _bridge(**overrides) -> EntityBridgeV1:
    base = dict(
        bridge_id="b1", entity_id="account", left_catalog_source="payments",
        left_object_ref="transactions.account_id", right_catalog_source="core_accounts",
        right_object_ref="accounts.account_id", authority=GraphEdgeAuthority.ENTITY_BRIDGE,
        status=RelationshipStatus.ACTIVE)
    base.update(overrides)
    return EntityBridgeV1(**base)


def test_entity_bridge_validation():
    validate_entity_bridge(_bridge(), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_entity_bridge(_bridge(entity_id="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="distinct catalog"):
        validate_entity_bridge(_bridge(right_catalog_source="payments"), known=KNOWN)
    with pytest.raises(ValueError, match="authority"):
        validate_entity_bridge(_bridge(authority=GraphEdgeAuthority.CATALOG_DECLARED), known=KNOWN)


def _proposal(**overrides) -> EntityRelationshipProposalV1:
    base = dict(
        proposal_id="p1", proposed_from_entity="account", proposed_to_entity="customer",
        proposed_cardinality=Cardinality.MANY_TO_ONE, evidence_refs=("edge:1",),
        source_catalog="core_accounts", inferred_by="join_inspector",
        status=RelationshipProposalStatus.PENDING)
    base.update(overrides)
    return EntityRelationshipProposalV1(**base)


def test_relationship_proposal_validation():
    validate_relationship_proposal(_proposal(), known=KNOWN)
    with pytest.raises(ValueError, match="unknown entity"):
        validate_relationship_proposal(_proposal(proposed_to_entity="not_an_entity"), known=KNOWN)
    with pytest.raises(ValueError, match="self-relationship proposal"):
        validate_relationship_proposal(_proposal(proposed_to_entity="account"), known=KNOWN)
    with pytest.raises(ValueError, match="evidence"):
        validate_relationship_proposal(_proposal(evidence_refs=()), known=KNOWN)


def test_registry_is_exactly_the_five_seed_rollups_and_valid():
    edges = {(d.from_entity, d.to_entity) for d in ENTITY_RELATIONSHIPS_V1}
    assert edges == {
        ("account", "customer"), ("card_account", "customer"), ("transaction", "account"),
        ("facility", "obligor"), ("policy", "customer")}
    for d in ENTITY_RELATIONSHIPS_V1:
        validate_relationship_definition(d, known=KNOWN)
        assert d.aggregation_required is True
        assert d.aggregation_strategy is AggregationStrategy.RECIPE_DECLARED  # never a blanket agg list
    assert GRAPH_VERSION == "1.0.0"


def test_registry_out_degree_at_most_one():
    # Out-degree <=1 prevents branching; acyclicity is enforced by the builder (Task 3).
    out_degree = Counter(
        d.from_entity for d in ENTITY_RELATIONSHIPS_V1 if d.status is RelationshipStatus.ACTIVE)
    assert all(n <= 1 for n in out_degree.values())


def test_registry_relationship_ids_unique():
    ids = [d.relationship_id for d in ENTITY_RELATIONSHIPS_V1]
    assert len(ids) == len(set(ids))
