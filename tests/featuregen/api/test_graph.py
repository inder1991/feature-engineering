from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def test_column_joins_resolved_edge(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    edges = client.get("/columns/public.transactions.account_id/joins",
                       params={"source": "deposits"}, headers=AUTH).json()
    assert edges == [{"from_ref": "public.transactions.account_id",
                      "to_ref": "public.accounts.id", "cardinality": "N:1", "resolved": True}]


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
