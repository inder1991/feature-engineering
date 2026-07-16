from __future__ import annotations

import psycopg
import pytest

# #21: 0993 constrained the graph's closed VOCABULARIES (enum-like CHECKs); 0997 adds the
# STRUCTURAL invariants of what build_graph/add_column_row actually write — kind-dependent
# nullability (a 'column' node names its column, a 'table' node has neither column_name nor
# data_type) and object_ref/edge-ref SHAPE (a dotted path with non-empty segments: >= 2 segments
# for a table ref, >= 3 for a column ref). Deliberately still NO foreign keys (a 'joins' edge may
# reference a not-yet-loaded endpoint — documented design choice) and NO cross-column equality
# (object_ref == 'public.' || table_name ...): the shape checks are widened to ">= N non-empty
# segments" so a pre-dot-quarantine row with a dotted table/column name is never broken by the
# ALTER. Migrations are applied once per session by the root `_dsn` fixture; the `conn` fixture
# rolls each test's writes back.


def _node(conn, object_ref: str, *, kind: str = "column", table_name: str = "txn",
          column_name: str | None = "amount", data_type: str | None = "text") -> None:
    conn.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type) VALUES ('src', %s, %s, %s, %s, %s)",
        (object_ref, kind, table_name, column_name, data_type))


def _edge(conn, from_ref: str, to_ref: str, *, kind: str = "joins") -> None:
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, authority) "
        "VALUES ('src', %s, %s, %s, 'operational')", (kind, from_ref, to_ref))


def _rejected(conn, insert, /, *args, **kwargs) -> None:
    """The insert must fail a CHECK; savepointed so one test can probe several violations.

    The no-op execute first OPENS the test's implicit outer transaction: ``conn.transaction()`` on
    an idle psycopg connection would otherwise be a top-level transaction that COMMITS on a
    no-raise exit, leaking rows past the fixture's teardown rollback (the 0993 pattern)."""
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        insert(conn, *args, **kwargs)


# ── graph_node: kind-dependent nullability ────────────────────────────────────────────────────────


def test_table_node_with_column_name_rejected(conn) -> None:
    _rejected(conn, _node, "public.txn", kind="table", column_name="amount", data_type=None)


def test_table_node_with_data_type_rejected(conn) -> None:
    # build_graph/add_column_row always write a table node's data_type as NULL — a typed table
    # node would be a corrupt row (and would silently shift the source_fingerprint contract).
    _rejected(conn, _node, "public.txn", kind="table", column_name=None, data_type="text")


def test_column_node_without_column_name_rejected(conn) -> None:
    _rejected(conn, _node, "public.txn.amount", column_name=None)
    _rejected(conn, _node, "public.txn.amount2", column_name="")


def test_empty_table_name_rejected(conn) -> None:
    _rejected(conn, _node, "public.txn.amount", table_name="")


# ── graph_node: object_ref shape matches kind ─────────────────────────────────────────────────────


def test_table_ref_must_be_dotted_with_nonempty_segments(conn) -> None:
    _rejected(conn, _node, "publictxn", kind="table", column_name=None, data_type=None)   # no dot
    _rejected(conn, _node, "public.", kind="table", column_name=None, data_type=None)     # empty tail
    _rejected(conn, _node, ".txn", kind="table", column_name=None, data_type=None)        # empty head
    _rejected(conn, _node, "public..txn", kind="table", column_name=None, data_type=None)


def test_column_ref_needs_one_more_segment_than_a_table_ref(conn) -> None:
    _rejected(conn, _node, "public.txn")            # table-shaped (2 segments) on a column node
    _rejected(conn, _node, "amount")                # no dots at all
    _rejected(conn, _node, "public.txn.")           # empty final segment
    _rejected(conn, _node, "public..amount")        # empty middle segment


def test_node_shapes_build_graph_writes_accepted(conn) -> None:
    _node(conn, "public.accounts", kind="table", column_name=None, data_type=None)
    _node(conn, "public.accounts.id", table_name="accounts", column_name="id",
          data_type="integer")
    # Widened on purpose: a legacy pre-dot-quarantine name may contain '.', giving the ref an
    # extra segment — the CHECK admits it (>= 3 non-empty segments) rather than break old data.
    _node(conn, "public.accounts.meta.data", table_name="accounts", column_name="meta.data")


# ── graph_edge: endpoint refs are non-empty dotted paths ──────────────────────────────────────────


def test_edge_refs_must_be_dotted_with_nonempty_segments(conn) -> None:
    _rejected(conn, _edge, "", "public.accounts.id")            # empty from_ref
    _rejected(conn, _edge, "public.txn.acct_id", "accounts")    # undotted to_ref
    _rejected(conn, _edge, "public.txn.", "public.accounts.id")   # empty final segment
    _rejected(conn, _edge, "public.txn.acct_id", ".accounts.id")  # empty head segment


def test_edge_shapes_build_graph_writes_accepted(conn) -> None:
    _edge(conn, "public.accounts", "public.accounts.id", kind="contains")   # table -> column
    _edge(conn, "public.txn.acct_id", "public.accounts.id")                 # column -> column join
