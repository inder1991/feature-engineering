"""The lineage graph must show a derived cross-catalog link.

Clicking Graph on `cust_num` drew no link to FTR, even though the link is derived, persisted, and
shown on the asset screen. The lineage builder DOES have cross-catalog expansion — but it finds
partners by matching `graph_node.entity`:

    WHERE kind = 'column' AND entity = %s AND catalog_source <> %s

and `graph_node.entity` is NULL on all 237 columns, because nothing populates it (the same bootstrap
deadlock that leaves entity_assignment with zero candidates). So the expansion has never produced an
edge on a source that does not declare entities in its file — which is every real source so far.

The links themselves are not in doubt: they are in the bridge ledger. The graph should read them.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.lineage import lineage_graph

_NOW = datetime(2026, 7, 29, tzinfo=UTC)
_FRESH = timedelta(days=3650)
_ACTOR = IdentityEnvelope(subject="o", actor_kind="human", authenticated=True,
                          auth_method="oidc", role_claims=("data_owner",))


@pytest.fixture
def two_catalogs(db):
    ingest_upload(db, "cib", [CanonicalRow(source="cib", table="bo_cib_customer",
                                           column="cust_num", type="text")],
                  actor=_ACTOR, now=_NOW)
    ingest_upload(db, "ftr", [CanonicalRow(source="ftr", table="tran_repos",
                                           column="cif_id", type="text")],
                  actor=_ACTOR, now=_NOW)
    ev = {"entity_id": "customer", "type_basis": "declared", "candidate_id": "c1",
          "left_is_grain": True, "right_is_grain": False, "data_type_family": "text",
          "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES ('customer','cib','public.bo_cib_customer.cust_num','ftr',"
        " 'public.tran_repos.cif_id','c1','fk-1','text',%s,'1.0.0')", (json.dumps(ev),))
    govern_bridge_fact(db, "fk-1", entity="customer", left_source="cib",
                       left_ref="public.bo_cib_customer.cust_num", right_source="ftr",
                       right_ref="public.tran_repos.cif_id")
    return db


def _graph(db, source="cib", ref="public.bo_cib_customer.cust_num"):
    return lineage_graph(db, source, ref, now=_NOW, fresh_within=_FRESH, depth=2)


def test_the_derived_link_appears_as_an_edge(two_catalogs):
    """THE report: clicking Graph showed no link even though one exists."""
    g = _graph(two_catalogs)
    bridges = [e for e in g["edges"] if e.get("kind") == "entity_bridge"]
    assert bridges, [e.get("kind") for e in g["edges"]]


def test_the_other_catalogs_table_is_a_NODE_on_the_graph(two_catalogs):
    """An edge to nowhere is not a graph — the partner table must be drawn."""
    g = _graph(two_catalogs)
    assert any(n["catalog_source"] == "ftr" for n in g["nodes"]), g["nodes"]


def test_an_unreviewed_link_has_separate_review_and_execution_axes(two_catalogs):
    """Confirmation annotates, it does not gate; endpoint, review and safety stay separate."""
    g = _graph(two_catalogs)
    bridge = next(e for e in g["edges"] if e.get("kind") == "entity_bridge")
    assert "resolved" not in bridge
    assert bridge["endpoint_resolved"] is True
    assert bridge["link_review_status"] == "unreviewed"
    assert bridge["realization_safety_status"] == "not_evaluated"
    assert bridge["execution_eligible"] is False
    assert bridge["trust_kind"] == "governed_identifier_link"


def test_it_works_from_the_OTHER_side_too(two_catalogs):
    """A link is symmetric; opening the FTR column must find the same hop."""
    g = _graph(two_catalogs, source="ftr", ref="public.tran_repos.cif_id")
    assert any(e.get("kind") == "entity_bridge" for e in g["edges"])


def test_a_hidden_partner_column_never_leaks_through_a_link(two_catalogs):
    """Read-scope is not weakened by adding a new edge source: a caller who cannot see the partner
    column must not learn it exists via the bridge."""
    two_catalogs.execute(
        "UPDATE graph_node SET sensitivity = 'pii' WHERE catalog_source = 'ftr' "
        "AND column_name = 'cif_id'")
    g = _graph(two_catalogs)   # roles=() — no pii_reader
    assert not any(e.get("kind") == "entity_bridge" for e in g["edges"])
    assert "cif_id" not in json.dumps(g)


def test_two_near_columns_linking_to_the_SAME_far_column_both_draw(two_catalogs):
    """The dedupe key was the FAR column alone, so a second link into the same far column was
    silently dropped — 6 edges drawn for 9 real links on the live catalog. `cust_pref_branch_cd`
    and `cust_prim_branch_cd` both reach `tran_branch_sol_id`; both are real and both must draw."""
    db = two_catalogs
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               " data_type) VALUES ('cib','public.bo_cib_customer.cust_alt','column',"
               " 'bo_cib_customer','cust_alt','text')")
    ev = {"entity_id": "customer", "type_basis": "declared", "candidate_id": "c2",
          "left_is_grain": False, "right_is_grain": False, "data_type_family": "text",
          "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES ('customer','cib','public.bo_cib_customer.cust_alt','ftr',"
        " 'public.tran_repos.cif_id','c2','fk-2','text',%s,'1.0.0')", (json.dumps(ev),))
    govern_bridge_fact(db, "fk-2", entity="customer", left_source="cib",
                       left_ref="public.bo_cib_customer.cust_alt", right_source="ftr",
                       right_ref="public.tran_repos.cif_id")
    # Asked of the TABLE: a column anchor now shows only its OWN links, so the two-near-columns
    # case has to be posed at table scope to be visible at all.
    g = lineage_graph(db, "cib", "public.bo_cib_customer", now=_NOW, fresh_within=_FRESH, depth=2)
    bridges = [e for e in g["edges"] if e.get("kind") == "entity_bridge"]
    near = {e["from"] for e in bridges}
    assert len(near) == 2, bridges     # both near columns reach cif_id


def test_the_edge_carries_a_strength_and_a_reason(two_catalogs):
    """The canvas needs to distinguish a grain-backed link from a type-only match. Without this,
    `cust_num <-> cif_id` drew identically to `cust_prim_branch_nm <-> sol_desc` — a name paired
    with a description, which is not a real join. The list could say so; the graph could not."""
    edge = next(e for e in _graph(two_catalogs)["edges"] if e.get("kind") == "entity_bridge")
    assert edge["strength"] >= 10          # cust_num is its table's grain
    assert "key" in edge["why"]


def test_a_type_only_link_ranks_below_a_grain_backed_one(two_catalogs):
    db = two_catalogs
    db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
               " data_type) VALUES ('cib','public.bo_cib_customer.branch_nm','column',"
               " 'bo_cib_customer','branch_nm','text')")
    ev = {"entity_id": "branch", "type_basis": "declared", "candidate_id": "c3",
          "left_is_grain": False, "right_is_grain": False, "data_type_family": "text",
          "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES ('branch','cib','public.bo_cib_customer.branch_nm','ftr','public.tran_repos.cif_id',"
        " 'c3','fk-3','text',%s,'1.0.0')", (json.dumps(ev),))
    govern_bridge_fact(db, "fk-3", entity="branch", left_source="cib",
                       left_ref="public.bo_cib_customer.branch_nm", right_source="ftr",
                       right_ref="public.tran_repos.cif_id")
    g = lineage_graph(db, "cib", "public.bo_cib_customer", now=_NOW, fresh_within=_FRESH, depth=2)
    by_entity = {e["entity_id"]: e for e in g["edges"] if e.get("kind") == "entity_bridge"}
    assert by_entity["customer"]["strength"] > by_entity["branch"]["strength"]
    assert "neither side is a key" in by_entity["branch"]["why"]
