"""Ingestion-richness Task 3D — GET /catalog/entity-map.

Read-only, gated by ``catalog:read``; roles are threaded from the session headers so column counts
and sample refs are read-scoped, while the link set is the same availability truth
``available_identifier_links()`` serves every other consumer.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.events.registry import event_registry
from featuregen.overlay.facts import register_overlay_event_types


def _h(roles: str = "catalog_viewer", user: str = "u") -> dict:
    return {"X-User": user, "X-Roles": roles}


@pytest.fixture
def overlay_env(conn):
    """The OVERLAY_FACT_* event schemas (the root harness resets the registry per test) — the
    governed stream behind every ledger candidate needs them."""
    register_overlay_event_types(event_registry())
    return conn


def _column(conn, source, table, column, *, entity=None, sensitivity=None):
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, entity, sensitivity) VALUES (%s,%s,'column',%s,%s,'text',%s,%s)",
        (source, f"public.{table}.{column}", table, column, entity, sensitivity))


def _candidate(conn, entity, left_col, right_col, *, status="DRAFT"):
    key = f"fk-{left_col}"
    ev = {"entity_id": entity, "type_basis": "declared",
          "candidate_id": f"{left_col}-{right_col}",
          "left_is_grain": False, "right_is_grain": False,
          "data_type_family": "text", "derivation_version": "1.0.0"}
    conn.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES (%s,'cib',%s,'ftr',%s,%s,%s,'text',%s,'1.0.0')",
        (entity, f"public.bo_cib_customer.{left_col}",
         f"public.comp_financial_tran_repos_dly.{right_col}",
         ev["candidate_id"], key, json.dumps(ev)))
    govern_bridge_fact(
        conn, key, entity=entity, left_source="cib",
        left_ref=f"public.bo_cib_customer.{left_col}", right_source="ftr",
        right_ref=f"public.comp_financial_tran_repos_dly.{right_col}", status=status)
    return key


def test_entity_map_requires_catalog_read(client):
    # access_admin holds ONLY iam:manage — no catalog:read -> 403
    assert client.get("/catalog/entity-map", headers=_h(roles="access_admin")).status_code == 403
    # catalog_viewer holds catalog:read -> 200 (an empty map is a valid, honest answer)
    r = client.get("/catalog/entity-map", headers=_h())
    assert r.status_code == 200, r.text
    assert r.json() == {"entities": [], "links": []}


def test_entity_map_returns_nodes_and_available_links(client, conn, overlay_env):
    _column(conn, "cib", "bo_cib_customer", "cust_num", entity="customer")
    _column(conn, "ftr", "comp_financial_tran_repos_dly", "cif_id", entity="customer")
    _candidate(conn, "customer", "cust_num", "cif_id", status="DRAFT")
    _candidate(conn, "branch", "branch_cd", "branch_desc", status="REJECTED")  # unavailable

    r = client.get("/catalog/entity-map", headers=_h())
    assert r.status_code == 200, r.text
    body = r.json()
    (link,) = body["links"]                       # the rejected decoy is absent, not dimmed
    assert link["status"] == "proposed"
    assert link["bridge_fact_key"] == "fk-cust_num"
    assert link["left"]["catalog_source"] == "cib"
    assert link["right"]["catalog_source"] == "ftr"
    by_id = {node["entity_id"]: node for node in body["entities"]}
    assert by_id["customer"]["column_count"] == 2
    assert {c["catalog_source"] for c in by_id["customer"]["catalogs"]} == {"cib", "ftr"}


def test_entity_map_counts_are_read_scoped_from_session_roles(client, conn):
    _column(conn, "cib", "bo_cib_customer", "cust_num", entity="customer")
    _column(conn, "cib", "bo_cib_customer", "cust_name", entity="customer", sensitivity="pii")

    plain = client.get("/catalog/entity-map", headers=_h()).json()
    (node,) = plain["entities"]
    assert node["column_count"] == 1
    assert "cust_name" not in json.dumps(plain)   # neither the count NOR the ref leaks

    lifted = client.get(
        "/catalog/entity-map", headers=_h(roles="catalog_viewer,pii_reader")).json()
    (node_lifted,) = lifted["entities"]
    assert node_lifted["column_count"] == 2
