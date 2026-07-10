"""Phase-3A Task 3A.6 — feasibility spike (TESTS ONLY). Prove the not-yet-active contracts can represent
REAL production upload shapes (graph.JoinEdge / entity.EntityBridge) BEFORE 3B commits to them. The
transforms live here, not in production: 3A never populates or traverses catalog realizations or bridges.
Using the real types (not stand-ins) is the point — a field rename in production must fail this test."""
from __future__ import annotations

from featuregen.overlay.upload.entity import EntityBridge  # real production type
from featuregen.overlay.upload.graph import JoinEdge  # real production type
from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    Cardinality,
    CatalogEntityRelationshipV1,
    EntityBridgeV1,
    GraphEdgeAuthority,
    RelationshipStatus,
    validate_catalog_relationship,
    validate_entity_bridge,
)

_CARDINALITY = {"N:1": Cardinality.MANY_TO_ONE, "1:1": Cardinality.ONE_TO_ONE,
                "1:N": Cardinality.ONE_TO_MANY, "N:N": Cardinality.MANY_TO_MANY}


def catalog_relationship_from_join_edge(
    edge: JoinEdge, *, catalog_source: str, relationship_id: str, adapter_id: str,
    from_entity: str, to_entity: str,
) -> CatalogEntityRelationshipV1:
    """A real WITHIN-catalog JoinEdge → a CatalogEntityRelationshipV1 (physical realization). Endpoint→
    entity resolution (``from_entity``/``to_entity``) + binding to a global relationship_id is 3B's job;
    here they are SUPPLIED to prove the CONTRACT carries the join's physical facts + resolved entities
    from the REAL JoinEdge type."""
    return CatalogEntityRelationshipV1(
        realization_id=f"{catalog_source}:{edge.from_ref}->{edge.to_ref}",
        relationship_id=relationship_id, catalog_source=catalog_source,
        from_object_ref=edge.from_ref, to_object_ref=edge.to_ref,
        resolved_from_entity=from_entity, resolved_to_entity=to_entity,
        declared_cardinality=_CARDINALITY[edge.cardinality or "N:1"], adapter_id=adapter_id,
        authority=GraphEdgeAuthority.CATALOG_DECLARED, status=RelationshipStatus.ACTIVE)


def bridge_v1_from_entity_bridge(
    bridge: EntityBridge, *, left_catalog: str, right_catalog: str, bridge_id: str,
) -> EntityBridgeV1:
    return EntityBridgeV1(
        bridge_id=bridge_id, entity_id=bridge.entity, left_catalog_source=left_catalog,
        left_object_ref=bridge.from_ref, right_catalog_source=right_catalog,
        right_object_ref=bridge.to_ref, authority=GraphEdgeAuthority.ENTITY_BRIDGE,
        status=RelationshipStatus.ACTIVE)


def test_real_join_edges_map_to_valid_catalog_realizations():
    # Within-catalog roll-up realizations (a cross-catalog SAME-entity join is a bridge, tested below).
    cases = [
        (JoinEdge(from_ref="accounts.account_id", to_ref="accounts.customer_id",
                  cardinality="N:1", resolved=True), "account", "customer"),
        (JoinEdge(from_ref="cards.card_account_id", to_ref="cards.customer_id",
                  cardinality="N:1", resolved=True), "card_account", "customer"),
        (JoinEdge(from_ref="facilities.facility_id", to_ref="facilities.obligor_id",
                  cardinality="N:1", resolved=True), "facility", "obligor"),
    ]
    for i, (edge, from_entity, to_entity) in enumerate(cases):
        real = catalog_relationship_from_join_edge(
            edge, catalog_source="core", relationship_id=f"rel_{i}", adapter_id="core_adapter",
            from_entity=from_entity, to_entity=to_entity)
        validate_catalog_relationship(real, known=known_entities())   # the contract is self-consistent
        assert real.from_object_ref == edge.from_ref
        assert real.resolved_from_entity == from_entity and real.resolved_to_entity == to_entity
        assert real.declared_cardinality is Cardinality.MANY_TO_ONE


def test_real_entity_bridge_maps_to_valid_bridge_v1():
    b = EntityBridge(entity="account", from_ref="transactions.account_id",
                     to_ref="accounts.account_id")
    v1 = bridge_v1_from_entity_bridge(b, left_catalog="payments", right_catalog="core", bridge_id="b1")
    validate_entity_bridge(v1, known=known_entities())
    assert v1.entity_id == "account"
    assert v1.left_object_ref == "transactions.account_id"
    assert v1.authority is GraphEdgeAuthority.ENTITY_BRIDGE
