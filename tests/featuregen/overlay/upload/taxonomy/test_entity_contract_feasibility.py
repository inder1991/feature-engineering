"""Phase-3A Task 3A.6 — feasibility spike (TESTS ONLY). Prove the not-yet-active contracts can represent
REAL production upload shapes (entity.EntityBridge) BEFORE 3B commits to them. The transform lives here,
not in production: 3A never populates or traverses entity bridges. Using the real types (not stand-ins) is
the point — a field rename in production must fail this test.

Note: catalog-realization derivation moved to production in Phase 3B.2A (`catalog_realizations.py`)."""
from __future__ import annotations

from featuregen.overlay.upload.entity import EntityBridge  # real production type
from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    EntityBridgeV1,
    GraphEdgeAuthority,
    RelationshipStatus,
    validate_entity_bridge,
)


def bridge_v1_from_entity_bridge(
    bridge: EntityBridge, *, left_catalog: str, right_catalog: str, bridge_id: str,
) -> EntityBridgeV1:
    return EntityBridgeV1(
        bridge_id=bridge_id, entity_id=bridge.entity, left_catalog_source=left_catalog,
        left_object_ref=bridge.from_ref, right_catalog_source=right_catalog,
        right_object_ref=bridge.to_ref, authority=GraphEdgeAuthority.ENTITY_BRIDGE,
        status=RelationshipStatus.ACTIVE)


def test_real_entity_bridge_maps_to_valid_bridge_v1():
    b = EntityBridge(entity="account", from_ref="transactions.account_id",
                     to_ref="accounts.account_id")
    v1 = bridge_v1_from_entity_bridge(b, left_catalog="payments", right_catalog="core", bridge_id="b1")
    validate_entity_bridge(v1, known=known_entities())
    assert v1.entity_id == "account"
    assert v1.left_object_ref == "transactions.account_id"
    assert v1.authority is GraphEdgeAuthority.ENTITY_BRIDGE
