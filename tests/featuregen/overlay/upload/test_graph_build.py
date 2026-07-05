from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph


def test_graph_tables_exist(db):
    # Both tables were created by the migration; a trivial count proves they exist.
    assert db.execute("SELECT count(*) FROM graph_node").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM graph_edge").fetchone()[0] == 0


def test_build_graph_materializes_nodes_edges(db):
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance"),
    ]
    build_graph(db, "deposits", rows)

    n = db.execute("SELECT count(*) FROM graph_node WHERE catalog_source='deposits'").fetchone()[0]
    assert n == 4  # 1 table + 3 columns
    kind = db.execute("SELECT kind FROM graph_node WHERE object_ref='public.accounts'").fetchone()[0]
    assert kind == "table"
    grain = db.execute(
        "SELECT is_grain FROM graph_node WHERE object_ref='public.accounts.id'").fetchone()[0]
    assert grain is True
    edges = db.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source='deposits' AND kind='contains'"
    ).fetchone()[0]
    assert edges == 3


def test_build_graph_is_idempotent_rebuild(db):
    rows_v1 = [CanonicalRow("deposits", "accounts", "id", "integer"),
               CanonicalRow("deposits", "accounts", "old_col", "text")]
    build_graph(db, "deposits", rows_v1)
    rows_v2 = [CanonicalRow("deposits", "accounts", "id", "integer")]
    build_graph(db, "deposits", rows_v2)  # old_col dropped
    refs = {r[0] for r in db.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source='deposits'").fetchall()}
    assert "public.accounts.old_col" not in refs
    assert "public.accounts.id" in refs
