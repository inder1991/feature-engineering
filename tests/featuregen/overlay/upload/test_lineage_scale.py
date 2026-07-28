"""A neighbourhood is the columns that PARTICIPATE, not every column of every table it touches.

Opening `cust_num` returned 188 nodes: the two tables expand to all 186 of their visible columns.
The screen showed "88 COLUMNS" and "+90 more columns" — the UI collapsing a payload it could not
draw. Nothing there is wrong, it is just not a neighbourhood; browsing a table's columns is the
asset screen's job.

Kept: the anchor, anything on a real edge (a join key, a bridge endpoint, a feature source), and the
grain / as-of columns — those define the table's identity and its time axis, they are few, and the
table card renders their badges.

Dropped: every column that neither anchors the view nor participates in a relationship. Their
`contains` edges go with them, so the graph never draws an edge to a node that is not there.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.lineage import lineage_graph

_NOW = datetime(2026, 7, 29, tzinfo=UTC)
_FRESH = timedelta(days=3650)
_ACTOR = IdentityEnvelope(subject="o", actor_kind="human", authenticated=True,
                          auth_method="oidc", role_claims=("data_owner",))


@pytest.fixture
def wide(db):
    """A wide table (60 columns) with one linked column — the real shape."""
    rows = [CanonicalRow(source="cib", table="cust", column=f"c{i:02d}", type="text")
            for i in range(60)]
    rows.append(CanonicalRow(source="cib", table="cust", column="cust_num", type="text",
                             is_grain=True))
    ingest_upload(db, "cib", rows, actor=_ACTOR, now=_NOW)
    ingest_upload(db, "ftr", [CanonicalRow(source="ftr", table="txn", column="cif_id", type="text"),
                              *[CanonicalRow(source="ftr", table="txn", column=f"f{i:02d}",
                                             type="text") for i in range(40)]],
                  actor=_ACTOR, now=_NOW)
    ev = {"entity_id": "customer", "type_basis": "declared", "candidate_id": "c1",
          "left_is_grain": True, "right_is_grain": False, "data_type_family": "text",
          "derivation_version": "1.0.0"}
    db.execute(
        "INSERT INTO entity_bridge_candidate_evidence (entity_id, left_catalog_source, "
        " left_object_ref, right_catalog_source, right_object_ref, candidate_id, fact_key, "
        " data_type_family, evidence_json, derivation_version) "
        "VALUES ('customer','cib','public.cust.cust_num','ftr','public.txn.cif_id','c1','fk-1',"
        " 'text',%s,'1.0.0')", (json.dumps(ev),))
    return db


def _graph(db):
    return lineage_graph(db, "cib", "public.cust.cust_num", now=_NOW, fresh_within=_FRESH, depth=2)


def _cols(g):
    return {n["object_ref"] for n in g["nodes"] if n["kind"] == "column"}


def test_the_neighbourhood_is_small_enough_to_read(wide):
    """101 columns exist across the two tables; the picture must not try to draw them."""
    g = _graph(wide)
    assert len(g["nodes"]) < 12, sorted(n["id"] for n in g["nodes"])


def test_the_anchor_is_kept(wide):
    assert "public.cust.cust_num" in _cols(_graph(wide))


def test_the_far_end_of_the_link_is_kept(wide):
    """An edge to a node that is not drawn is not a graph."""
    assert "public.txn.cif_id" in _cols(_graph(wide))


def test_an_unrelated_column_is_dropped(wide):
    cols = _cols(_graph(wide))
    assert "public.cust.c07" not in cols
    assert "public.txn.f11" not in cols


def test_both_tables_are_still_drawn(wide):
    tables = {n["object_ref"] for n in _graph(wide)["nodes"] if n["kind"] == "table"}
    assert tables == {"public.cust", "public.txn"}


def test_no_edge_points_at_a_dropped_node(wide):
    """The invariant that makes pruning safe."""
    g = _graph(wide)
    ids = {n["id"] for n in g["nodes"]}
    dangling = [e for e in g["edges"] if e["from"] not in ids or e["to"] not in ids]
    assert dangling == [], dangling


def test_the_link_edge_survives_the_prune(wide):
    assert any(e.get("kind") == "entity_bridge" for e in _graph(wide)["edges"])
