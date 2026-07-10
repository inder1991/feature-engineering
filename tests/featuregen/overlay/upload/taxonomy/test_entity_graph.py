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


def test_deprecated_def_with_bad_endpoint_does_not_block_build():
    # Active-only validation: a DEPRECATED def is archived (skipped BEFORE validation), so an unknown
    # endpoint on it must NOT break the build.
    g = build_entity_graph(
        (_e("bad", "account", "not_an_entity", status=RelationshipStatus.DEPRECATED),),
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


from featuregen.overlay.upload.taxonomy.entity_graph import (
    ENTITY_GRAPH,
    resolve_entity_compatibility,
)
from featuregen.overlay.upload.taxonomy.entity_relationships import EntityCompatibility


def test_unknown_entities_never_exact():
    # THE closed-vocab guard: two identical out-of-vocab strings must NOT be EXACT.
    r = resolve_entity_compatibility("not_an_entity", "not_an_entity", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.UNKNOWN
    assert "unknown_source_entity" in r.reason_codes
    assert resolve_entity_compatibility("account", "not_an_entity", ENTITY_GRAPH).reason_codes \
        == ("unknown_target_entity",)
    assert resolve_entity_compatibility("", "", ENTITY_GRAPH).status is EntityCompatibility.UNKNOWN


def test_exact_when_known_source_equals_target():
    r = resolve_entity_compatibility("customer", "customer", ENTITY_GRAPH)
    assert r.status is EntityCompatibility.EXACT
    assert r.paths == () and r.graph_version == ENTITY_GRAPH.version


def test_derivable_direct_and_transitive():
    direct = resolve_entity_compatibility("account", "customer", ENTITY_GRAPH)
    assert direct.status is EntityCompatibility.DERIVABLE
    assert [h.relationship_id for h in direct.paths[0].hops] == ["account_to_customer"]
    assert direct.paths[0].hops[0].relationship_version == "1.0.0"
    assert direct.paths_truncated is False
    trans = resolve_entity_compatibility("transaction", "customer", ENTITY_GRAPH)
    assert [h.to_entity for h in trans.paths[0].hops] == ["account", "customer"]


def test_unknown_when_no_path():
    assert resolve_entity_compatibility("customer", "account", ENTITY_GRAPH).status \
        is EntityCompatibility.UNKNOWN


def test_seed_never_emits_ambiguous():
    ents = ("customer", "account", "card_account", "transaction", "facility", "obligor", "policy")
    for s in ents:
        for t in ents:
            assert resolve_entity_compatibility(s, t, ENTITY_GRAPH).status \
                is not EntityCompatibility.AMBIGUOUS


def test_ambiguous_two_paths_is_not_truncated():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer")),
        version="1.0.0", known=KNOWN)
    r = resolve_entity_compatibility("transaction", "customer", g)
    assert r.status is EntityCompatibility.AMBIGUOUS
    assert len(r.paths) == 2 and r.paths_truncated is False   # exactly two — nothing truncated


def test_ambiguous_three_paths_is_truncated():
    g = build_entity_graph(
        (_e("t_a", "transaction", "account"), _e("a_c", "account", "customer"),
         _e("t_ca", "transaction", "card_account"), _e("ca_c", "card_account", "customer"),
         _e("t_p", "transaction", "policy"), _e("p_c", "policy", "customer")),
        version="1.0.0", known=KNOWN)
    r = resolve_entity_compatibility("transaction", "customer", g)
    assert r.status is EntityCompatibility.AMBIGUOUS
    assert len(r.paths) == 2 and r.paths_truncated is True    # visible capped; a third path exists


def _unsafe_graph_for_test(edges: dict) -> object:
    # Direct construction bypasses build_entity_graph invariants — TEST-ONLY, to exercise traversal
    # defense against a malformed graph the builder would have rejected. Production graphs come only
    # from build_entity_graph.
    from types import MappingProxyType

    from featuregen.overlay.upload.taxonomy.entity_graph import EntityGraph
    return EntityGraph(version="1.0.0", known_entities=KNOWN, _adjacency=MappingProxyType(edges))


def test_traversal_visited_guard_defends_a_malformed_cyclic_graph():
    cyclic = _unsafe_graph_for_test({
        "account": (_e("a_c", "account", "customer"),),
        "customer": (_e("c_a", "customer", "account"),)})
    assert resolve_entity_compatibility("account", "customer", cyclic).status \
        is EntityCompatibility.DERIVABLE
