"""Phase-3A Tasks 3A.3/3A.4 — the immutable, cycle-rejecting graph builder + bounded traversal."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.taxonomy.dimensions import known_entities
from featuregen.overlay.upload.taxonomy.entity_graph import build_entity_graph
from featuregen.overlay.upload.taxonomy.entity_relationships import (
    AggregationStrategy,
    Cardinality,
    EntityRelationshipDefinitionV1,
    RelationshipStatus,
    RelationshipType,
    TraversalDirection,
)

KNOWN = known_entities()


def _e(rid, a, b, *, status=RelationshipStatus.ACTIVE,
       direction=TraversalDirection.FORWARD) -> EntityRelationshipDefinitionV1:
    return EntityRelationshipDefinitionV1(
        relationship_id=rid, from_entity=a, to_entity=b, relationship_type=RelationshipType.ROLLUP,
        cardinality=Cardinality.MANY_TO_ONE, traversal_direction=direction, aggregation_required=True,
        aggregation_strategy=AggregationStrategy.RECIPE_DECLARED, status=status, version="1.0.0")


def test_build_indexes_active_outgoing_edges_and_carries_vocab():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer")),
        version="1.0.0", known=KNOWN)
    assert g.version == "1.0.0"
    assert g.known_entities == KNOWN
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["t_a"]
    assert g.outgoing("customer") == ()


def test_inactive_edges_excluded():
    g = build_entity_graph(
        (_e("a_c", "account", "customer", status=RelationshipStatus.DEPRECATED),),
        version="1.0.0", known=KNOWN)
    assert g.outgoing("account") == ()


def test_outgoing_sorted_by_relationship_id():
    g = build_entity_graph(
        (_e("z2", "transaction", "account"), _e("a2", "transaction", "obligor")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["a2", "z2"]


def test_converging_dag_is_not_treated_as_cycle():
    # transaction -> account -> customer AND transaction -> card_account -> customer share the descendant
    # `customer`; a converging DAG is acyclic and MUST build (the AMBIGUOUS test depends on this).
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer")),
        version="1.0.0", known=KNOWN)
    assert [d.relationship_id for d in g.outgoing("transaction")] == ["t_a", "t_ca"]


def test_duplicate_relationship_id_rejected():
    with pytest.raises(ValueError, match="duplicate.*id"):
        build_entity_graph(
            (_e("dup", "account", "customer"), _e("dup", "transaction", "account")),
            version="1.0.0", known=KNOWN)


def test_duplicate_semantic_edge_rejected():
    # same (from, to, type, direction) with different ids -> a duplicate declaration, NOT ambiguity
    with pytest.raises(ValueError, match="duplicate semantic edge"):
        build_entity_graph(
            (_e("a_c_v1", "account", "customer"), _e("a_c_dupe", "account", "customer")),
            version="1.0.0", known=KNOWN)


def test_semantic_cycle_rejected():
    with pytest.raises(ValueError, match="cycle"):
        build_entity_graph(
            (_e("a_c", "account", "customer"), _e("c_a", "customer", "account")),
            version="1.0.0", known=KNOWN)


def test_non_forward_active_edge_rejected():
    with pytest.raises(ValueError, match="forward"):
        build_entity_graph(
            (_e("bad", "account", "customer", direction=TraversalDirection.BOTH),),
            version="1.0.0", known=KNOWN)


def test_builder_validates_endpoints():
    with pytest.raises(ValueError, match="unknown entity"):
        build_entity_graph((_e("bad", "account", "not_an_entity"),), version="1.0.0", known=KNOWN)


def test_invalid_graph_version_rejected():
    with pytest.raises(ValueError, match="version"):
        build_entity_graph((_e("a_c", "account", "customer"),), version="v1", known=KNOWN)
