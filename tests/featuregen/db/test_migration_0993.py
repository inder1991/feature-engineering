from __future__ import annotations

import psycopg
import pytest

# Round-3 #19: PostgreSQL enforces the graph's enum-like invariants. graph_node/graph_edge store
# closed vocabularies (node kind/sensitivity; edge kind/cardinality/authority/approved_join_status)
# as text, and 0993 adds CHECK constraints matching EXACTLY what the application writes — so an
# application bug can no longer persist malformed operational state. The endpoints deliberately
# have NO foreign keys (a 'joins' edge may reference a not-yet-loaded target); free-text columns
# (definition, concept, domain, ...) stay unconstrained. Migrations are applied once per session
# by the root `_dsn` fixture; the `conn` fixture rolls each test's writes back.


def _node(conn, object_ref: str, *, kind: str = "column", sensitivity: str | None = None) -> None:
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity) VALUES ('src', %s, %s, 'txn', 'amount', %s)",
        (object_ref, kind, sensitivity))


def _edge(conn, to_ref: str, *, kind: str = "joins", cardinality: str | None = "N:1",
          authority: str = "operational", status: str | None = None) -> None:
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_status) VALUES ('src', %s, 'public.txn.acct_id', %s, %s, %s, %s)",
        (kind, to_ref, cardinality, authority, status))


def _rejected(conn, insert, /, *args, **kwargs) -> None:
    """The insert must fail the CHECK; savepointed so one test can probe several violations.

    The no-op execute first OPENS the test's implicit outer transaction: ``conn.transaction()`` on
    an idle psycopg connection would otherwise be a top-level transaction that COMMITS on a
    no-raise exit, leaking rows past the fixture's teardown rollback."""
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        insert(conn, *args, **kwargs)


# ── graph_node ───────────────────────────────────────────────────────────────────────────────────


def test_node_kind_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _node, "public.txn.a", kind="tabel")
    _rejected(conn, _node, "public.txn.b", kind="")


def test_node_sensitivity_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _node, "public.txn.a", sensitivity="PII")   # app writes lowercase only
    _rejected(conn, _node, "public.txn.b", sensitivity="secret")
    _rejected(conn, _node, "public.txn.c", sensitivity="")      # canonical maps '' -> NULL


def test_node_valid_values_accepted(conn) -> None:
    _node(conn, "public.txn.a", kind="table", sensitivity=None)
    _node(conn, "public.txn.b", kind="column", sensitivity="pii")
    _node(conn, "public.txn.c", kind="column", sensitivity="restricted")


# ── graph_edge ───────────────────────────────────────────────────────────────────────────────────


def test_edge_kind_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _edge, "public.acct.a", kind="join")
    _rejected(conn, _edge, "public.acct.b", kind="entity_bridge")


def test_edge_cardinality_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _edge, "public.acct.a", cardinality="n:1")   # canonical vocab is uppercase
    _rejected(conn, _edge, "public.acct.b", cardinality="M:N")


def test_edge_authority_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _edge, "public.acct.a", authority="governed")
    _rejected(conn, _edge, "public.acct.b", authority="")


def test_edge_approved_join_status_out_of_domain_rejected(conn) -> None:
    _rejected(conn, _edge, "public.acct.a", status="verified")   # folded statuses are uppercase
    _rejected(conn, _edge, "public.acct.b", status="APPROVED")


def test_edge_valid_values_accepted(conn) -> None:
    # The shapes the app actually writes: a plain contains edge; a declared joins edge (nullable
    # cardinality/status); a governed VERIFIED projection; every async-demotion folded status.
    _edge(conn, "public.acct.c0", kind="contains", cardinality=None, status=None)
    _edge(conn, "public.acct.c1", cardinality=None, authority="display_only", status=None)
    for i, (card, status) in enumerate([("N:1", "VERIFIED"), ("1:N", "REJECTED"),
                                        ("1:1", "REVERIFY"), ("N:1", "STALE"),
                                        ("N:1", "DRAFT"), ("N:1", "PARTIALLY_CONFIRMED")]):
        _edge(conn, f"public.acct.v{i}", cardinality=card,
              authority="operational" if status == "VERIFIED" else "display_only", status=status)
