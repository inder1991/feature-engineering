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

from tests.featuregen.overlay.upload.passc.conftest import (
    _confirm_join,
    _expire_join,
    _propose_join,
    _reject_join,
)

from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.entity import find_cross_catalog_path
from featuregen.overlay.upload.feature_assist import route_strategies
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import build_join_ref
from featuregen.overlay.upload.passc.projection import project_confirmed_joins

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


# ── The reverse projector ────────────────────────────────────────────────────────────────────────


def _verified_projected(conn, *, admin1, admin2):
    """Drive the full governed flow: propose -> dual confirm -> project. Returns the ref."""
    ref = build_join_ref(_strong_evidence(), "src")
    _propose_join(conn, ref)
    _confirm_join(conn, ref, admin1=admin1, admin2=admin2)
    project_confirmed_joins(conn, source="src", pairs=[ref])
    return ref


def test_verified_join_projects_one_operational_edge_that_traverses(
        passc_conn, human_admin_1, human_admin_2):
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)
    key = fact_key(ref, "approved_join")
    confirmed_id = fold_overlay_state(load_fact(passc_conn, key)).confirmed_event_id

    rows = _edge_rows(passc_conn)
    assert set(rows) == {(_FROM, _TO)}, "exactly ONE edge, PUBLIC graph scope, confirmed direction"
    edge = rows[(_FROM, _TO)]
    assert edge["cardinality"] == "N:1" and edge["authority"] == "operational"
    assert edge["fact_key"] == key and edge["status"] == "VERIFIED"
    assert edge["event_id"] == confirmed_id and edge["event_id"] is not None
    assert edge["updated_at"] is not None

    assert find_join_path(passc_conn, "src", "transactions", "customers") \
        == [JoinStep(_FROM, _TO, "N:1")]


def test_confirmed_direction_reversing_declared_edge_leaves_one_row(
        passc_conn, human_admin_1, human_admin_2):
    # The upload declared the join in the OPPOSITE orientation (display-only under the governed
    # seam). The confirmed fact points transactions -> customers: the projector must delete the
    # reversed declared row and leave EXACTLY ONE operational edge — no stale duplicate that
    # find_join_path could traverse with an inverted fan.
    _edge(passc_conn, _TO, _FROM, cardinality="1:N", authority="display_only")
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)

    rows = _edge_rows(passc_conn)
    assert set(rows) == {(_FROM, _TO)}
    assert rows[(_FROM, _TO)]["authority"] == "operational"
    assert rows[(_FROM, _TO)]["fact_key"] == fact_key(ref, "approved_join")


def test_projector_never_demotes_declared_edge(passc_conn):
    # THE flag-off byte-for-byte guarantee: a DRAFT (non-VERIFIED) fact for the same column pair
    # must not touch a file-declared operational edge — its fact_key is NULL, and only
    # fact-LINKED edges are the projector's to demote.
    _edge(passc_conn, _FROM, _TO)
    before = _edge_rows(passc_conn)
    ref = build_join_ref(_strong_evidence(), "src")
    _propose_join(passc_conn, ref)                      # DRAFT — never confirmed

    project_confirmed_joins(passc_conn, source="src", pairs=[ref])

    assert _edge_rows(passc_conn) == before             # byte-for-byte: untouched
    assert find_join_path(passc_conn, "src", "transactions", "customers") \
        == [JoinStep(_FROM, _TO, "N:1")]


def test_projector_demotion_is_scoped_to_the_column_pair(
        passc_conn, human_admin_1, human_admin_2):
    # Edges are COLUMN-keyed: demoting the cif_id pair must not touch (a) a governed edge on a
    # DIFFERENT column pair between the same tables, nor (b) a declared edge on a third pair.
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)
    other_gov = ("public.transactions.branch_id", "public.customers.branch_id")
    other_decl = ("public.transactions.acct_no", "public.customers.acct_no")
    _edge(passc_conn, *other_gov, link_key="other-key", link_status="VERIFIED")
    _edge(passc_conn, *other_decl)

    _expire_join(passc_conn, ref)                       # VERIFIED -> REVERIFY
    project_confirmed_joins(passc_conn, source="src", pairs=[ref])

    rows = _edge_rows(passc_conn)
    demoted = rows[(_FROM, _TO)]
    assert demoted["authority"] == "display_only"
    # The projector CLEARS the fact links on demotion (the edge reverts to a plain display row).
    assert demoted["fact_key"] is None and demoted["event_id"] is None
    assert demoted["status"] is None and demoted["updated_at"] is not None
    # Scope-safety: the other pairs are untouched.
    assert rows[other_gov]["authority"] == "operational"
    assert rows[other_gov]["fact_key"] == "other-key"
    assert rows[other_decl]["authority"] == "operational"
    assert rows[other_decl]["fact_key"] is None


def test_projector_is_idempotent(passc_conn, human_admin_1, human_admin_2):
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)
    first = _edge_rows(passc_conn)
    project_confirmed_joins(passc_conn, source="src", pairs=[ref, ref])   # re-run, duplicate pair
    again = _edge_rows(passc_conn)
    assert set(again) == set(first) == {(_FROM, _TO)}
    assert again[(_FROM, _TO)]["fact_key"] == first[(_FROM, _TO)]["fact_key"]


# ── The async demotion hook (no re-ingest, no projector run) ─────────────────────────────────────


def test_expiry_hook_demotes_edge_without_reingest(passc_conn, human_admin_1, human_admin_2):
    # The ingest-latency closer: the fact expires (VERIFIED -> REVERIFY via the PRODUCTION
    # fire_due_overlay_expiries poller) and the edge stops traversing IMMEDIATELY — no re-upload,
    # no project_confirmed_joins run.
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)
    assert find_join_path(passc_conn, "src", "transactions", "customers") is not None

    _expire_join(passc_conn, ref)

    edge = _edge_rows(passc_conn)[(_FROM, _TO)]
    assert edge["authority"] == "display_only" and edge["status"] == "REVERIFY"
    assert edge["fact_key"] == fact_key(ref, "approved_join")   # link KEPT (audit/re-project)
    assert edge["updated_at"] is not None
    assert find_join_path(passc_conn, "src", "transactions", "customers") is None


def test_reject_hook_stamps_rejected_status(passc_conn, human_admin_1, human_admin_2):
    # A REVERIFY fact is awaiting confirmation again, so a human may REJECT it outright — the
    # hook re-stamps the already-demoted edge with the terminal status.
    ref = _verified_projected(passc_conn, admin1=human_admin_1, admin2=human_admin_2)
    _expire_join(passc_conn, ref)
    _reject_join(passc_conn, ref, admin=human_admin_1)

    edge = _edge_rows(passc_conn)[(_FROM, _TO)]
    assert edge["authority"] == "display_only" and edge["status"] == "REJECTED"
    assert find_join_path(passc_conn, "src", "transactions", "customers") is None


def test_pre_verified_reject_is_a_noop_on_edges(passc_conn, human_admin_1):
    # A DRAFT that never verified has no projected edge; rejecting it must not create or touch
    # any graph_edge row (the hook's UPDATE simply matches nothing).
    _edge(passc_conn, _FROM, _TO)                       # unrelated declared edge stays untouched
    before = _edge_rows(passc_conn)
    ref = build_join_ref(_strong_evidence(), "src")
    _propose_join(passc_conn, ref)
    _reject_join(passc_conn, ref, admin=human_admin_1)
    assert _edge_rows(passc_conn) == before
