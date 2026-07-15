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


def test_build_graph_writes_concept_into_node_and_search(db):
    from featuregen.overlay.upload.enrich import content_hash
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    concepts = {content_hash(rows[0]): "monetary_amount"}
    build_graph(db, "deposits", rows, concepts)
    concept = db.execute(
        "SELECT concept FROM graph_node WHERE object_ref='public.accounts.balance'").fetchone()[0]
    assert concept == "monetary_amount"
    # 'monetary' now matches the node via the folded concept text.
    hit = db.execute(
        "SELECT count(*) FROM graph_node WHERE object_ref='public.accounts.balance' "
        "AND search_doc @@ plainto_tsquery('english','monetary')").fetchone()[0]
    assert hit == 1


def test_build_graph_folds_drafted_definition_and_domain(db):
    from featuregen.overlay.upload.enrich import content_hash
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]  # blank definition
    build_graph(db, "deposits", rows,
                definitions={content_hash(rows[0]): "the account ledger balance"},
                domains={"accounts": "Deposits"})
    row = db.execute(
        "SELECT definition, domain FROM graph_node WHERE object_ref='public.accounts.bal'"
    ).fetchone()
    assert row[0] == "the account ledger balance"   # drafted def fills the blank
    assert row[1] == "Deposits"
    # both the drafted definition ('ledger') and the domain ('deposits') are searchable
    for term in ("ledger", "deposits"):
        hit = db.execute(
            "SELECT count(*) FROM graph_node WHERE object_ref='public.accounts.bal' "
            "AND search_doc @@ plainto_tsquery('english', %s)", (term,)).fetchone()[0]
        assert hit == 1, term


def test_malformed_joins_to_is_not_written_as_raw_edge(db):
    # A malformed joins_to (here a bare table name, no column) must NOT become a raw 'joins' edge on
    # the ungoverned graph-write path — it would be an operational edge to a garbage/phantom target.
    # Only a parse_join_ref-valid target becomes an edge, at public.<table>.<column> (#5).
    build_graph(db, "core", [
        CanonicalRow("core", "orders", "customer_id", "integer", joins_to="customers"),   # bare, invalid
        CanonicalRow("core", "orders", "ok_fk", "integer", joins_to="dw.customers.id"),   # schema.table.column
        CanonicalRow("core", "orders", "plain_fk", "integer", joins_to="customers.id"),   # table.column
    ])
    joins = db.execute(
        "SELECT from_ref, to_ref FROM graph_edge WHERE catalog_source='core' AND kind='joins' "
        "ORDER BY from_ref").fetchall()
    # The bare 'customers' is skipped; the 3-part target normalizes to public.<table>.<column>.
    assert joins == [
        ("public.orders.ok_fk", "public.customers.id"),
        ("public.orders.plain_fk", "public.customers.id"),
    ]


def test_column_joins_resolved_is_catalog_scoped(db):
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.graph import build_graph, column_joins
    # A has a join to public.customers.id that is NOT loaded in A (cross-source pending)
    build_graph(db, "A", [
        CanonicalRow("A", "accounts", "cust", "integer", joins_to="customers.id", cardinality="N:1")])
    build_graph(db, "B", [CanonicalRow("B", "customers", "id", "integer", is_grain=True)])
    edges = column_joins(db, "A", "public.accounts.cust")
    assert edges and edges[0].resolved is False   # B's node must NOT resolve A's cross-source edge
