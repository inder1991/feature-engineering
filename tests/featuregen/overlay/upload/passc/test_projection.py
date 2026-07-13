"""Task 8 — the reverse projector + governed edge filter + async demotion hook.

The loop-closer: a VERIFIED `approved_join` fact becomes the ONE operational `graph_edge` that
`find_join_path` traverses (public graph scope, fact-linked); anything less than VERIFIED never
traverses. Three safety properties under test:

* **Governed edge filter** (flag-off-safe): a file-declared edge has a NULL
  `approved_join_fact_key` and traverses byte-for-byte; a fact-LINKED edge traverses only while
  `approved_join_status='VERIFIED'` — even inside the ingest-latency window where `authority`
  still says 'operational'.
* **Declared-spare projector**: `project_confirmed_joins` deletes/demotes ONLY fact-linked edges
  for THE candidate's unordered column pair (both orientations); a declared edge (fact_key NULL)
  and any OTHER column pair's edge are never touched.
* **Async demotion hook**: the moment a fact leaves VERIFIED (expiry -> REVERIFY, reject ->
  REJECTED) the linked edge flips to display_only WITHOUT a re-ingest/projector run.
"""
from __future__ import annotations

from featuregen.overlay.upload.entity import find_cross_catalog_path
from featuregen.overlay.upload.feature_assist import route_strategies
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta

_CIF_TERM = "Customer Information File Identifier"

# The candidate's endpoints in PUBLIC graph scope (graph_node.object_ref form, NOT the
# `src::public.…` evidence form) — what the projector must render.
_FROM = "public.transactions.cif_id"
_TO = "public.customers.cif_id"


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _strong_evidence(from_table="transactions", to_table="customers", column="cif_id"):
    """A strong, grain-inferred N:1 candidate: {from_table}.{column} -> {to_table}.{column}."""
    pairs = block_candidates([_c(from_table, column, term_name=_CIF_TERM),
                              _c(to_table, column, term_name=_CIF_TERM, is_grain=True)])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    ev = score(pairs[0], source_snapshot_id="snap-1")
    assert ev.bucket == "strong" and ev.proposed_cardinality == "N:1"
    return ev


def _edge(conn, from_ref, to_ref, *, cardinality="N:1", authority="operational",
          link_key=None, link_status=None, source="src"):
    """Insert a raw `joins` graph_edge row (declared when link_key is None; governed otherwise)."""
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority,"
        " approved_join_fact_key, approved_join_status) VALUES (%s,'joins',%s,%s,%s,%s,%s,%s)",
        (source, from_ref, to_ref, cardinality, authority, link_key, link_status))


def _edge_rows(conn, source="src"):
    """Every `joins` edge for the source: {(from_ref, to_ref): row-dict}."""
    rows = conn.execute(
        "SELECT from_ref, to_ref, cardinality, authority, approved_join_fact_key,"
        " approved_join_event_id, approved_join_status, authority_updated_at"
        " FROM graph_edge WHERE catalog_source = %s AND kind = 'joins'", (source,)).fetchall()
    return {(r[0], r[1]): dict(zip(
        ("cardinality", "authority", "fact_key", "event_id", "status", "updated_at"),
        r[2:], strict=True)) for r in rows}


# ── Governed edge filter (flag-off-safe) ─────────────────────────────────────────────────────────


def test_declared_edge_with_null_link_still_traverses(passc_conn):
    # Flag-off byte-for-byte: a file-declared edge never carries a fact link (fact_key NULL), so
    # the new filter must not change its traversal in ANY reader.
    _edge(passc_conn, _FROM, _TO)
    assert find_join_path(passc_conn, "src", "transactions", "customers") \
        == [JoinStep(_FROM, _TO, "N:1")]
    assert find_cross_catalog_path(passc_conn, "src", "transactions", "src", "customers") \
        is not None
    picks = route_strategies(passc_conn, [{"object_ref": _FROM, "catalog_source": "src"}])
    assert "aggregation" in {name for name, _ in picks}


def test_linked_edge_traverses_only_when_verified(passc_conn):
    # The ingest-latency window: a fact-linked edge whose fact has left VERIFIED but whose
    # authority column was not yet flipped (async hook lost / crashed). The governed filter is the
    # second, independent gate: status != VERIFIED -> NO feature-construction reader traverses.
    _edge(passc_conn, _FROM, _TO, link_key="k1", link_status="VERIFIED")
    assert find_join_path(passc_conn, "src", "transactions", "customers") is not None

    passc_conn.execute(
        "UPDATE graph_edge SET approved_join_status = 'REVERIFY' WHERE approved_join_fact_key='k1'")
    assert find_join_path(passc_conn, "src", "transactions", "customers") is None
    assert find_cross_catalog_path(passc_conn, "src", "transactions", "src", "customers") is None
    picks = route_strategies(passc_conn, [{"object_ref": _FROM, "catalog_source": "src"}])
    assert "aggregation" not in {name for name, _ in picks}
