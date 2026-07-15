from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def test_column_joins_resolved_edge(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    edges = client.get("/columns/public.transactions.account_id/joins",
                       params={"source": "deposits"}, headers=AUTH).json()
    assert edges == [{"from_ref": "public.transactions.account_id",
                      "to_ref": "public.accounts.id", "cardinality": "N:1", "resolved": True,
                      # #10: authority state rides along so a client can tell an operational
                      # edge (traversable by /join-path) from a display-only pending one.
                      "authority": "operational", "approved_join_status": None}]


def test_column_joins_pending_target_unresolved(client):
    csv_text = ("source,table,column,type,joins_to,cardinality\n"
                "gl,entries,entry_id,integer,,\n"
                "gl,entries,batch_id,integer,batches.batch_id,N:1\n")
    upload_csv(client, "gl", csv_text)
    edges = client.get("/columns/public.entries.batch_id/joins",
                       params={"source": "gl"}, headers=AUTH).json()
    assert len(edges) == 1
    assert edges[0]["resolved"] is False   # cross-source / not-yet-loaded target


def test_join_path_steps_oriented_to_traversal(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    steps = client.get("/join-path", params={
        "source": "deposits", "from": "transactions", "to": "customers"}, headers=AUTH).json()
    assert [s["cardinality"] for s in steps] == ["N:1", "N:1"]   # fan-in both hops (M7)
    assert steps[0]["from_ref"] == "public.transactions.account_id"
    assert steps[-1]["to_ref"] == "public.customers.cust_id"


def test_join_path_unreachable_null_same_table_empty(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert client.get("/join-path", params={
        "source": "deposits", "from": "transactions", "to": "nowhere"},
        headers=AUTH).json() is None
    assert client.get("/join-path", params={
        "source": "deposits", "from": "accounts", "to": "accounts"}, headers=AUTH).json() == []


def test_column_joins_read_scopes_a_sensitive_target(client):
    from tests.featuregen.api._helpers import PII_AUTH
    csv_text = ("source,table,column,type,joins_to,cardinality,sensitivity\n"
                "bank,orders,cust_ref,integer,customers.secret_id,N:1,\n"
                "bank,customers,secret_id,integer,,,pii\n")
    upload_csv(client, "bank", csv_text)
    url = "/columns/public.orders.cust_ref/joins"
    # data_owner (no pii_reader): the edge to the pii column is WITHHELD (can't walk the graph to it)
    assert client.get(url, params={"source": "bank"}, headers=AUTH).json() == []
    # pii_reader: the edge is visible
    edges = client.get(url, params={"source": "bank"}, headers=PII_AUTH).json()
    assert any(e["to_ref"] == "public.customers.secret_id" for e in edges)
