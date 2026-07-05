"""Entity-resolution API: suggest → list → apply."""
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph

from ._helpers import AUTH


def _fake():
    return FakeLLM(script={"overlay.enrich.entity": FakeResponse(output={"entity": "Customer"})})


def test_entity_suggest_list_and_apply(make_client, conn):
    build_graph(conn, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer"),   # id-like, un-tagged
        CanonicalRow("deposits", "accounts", "balance", "numeric")])   # skipped
    client = make_client(_fake())
    r = client.post("/entity/suggest", json={"catalog_source": "deposits"}, headers=AUTH)
    assert r.status_code == 200 and r.json()["suggested"] == 1
    lst = client.get("/entity/suggestions", params={"catalog_source": "deposits"}, headers=AUTH)
    assert lst.status_code == 200
    hit = lst.json()[0]
    assert hit["column"] == "cust_ref" and hit["suggested_entity"] == "Customer"
    ap = client.post("/entity/apply",
                     json={"catalog_source": "deposits", "object_ref": hit["object_ref"]}, headers=AUTH)
    assert ap.status_code == 200 and ap.json()["applied"] is True
    # applied -> no longer pending; the graph now carries the entity
    assert client.get("/entity/suggestions", params={"catalog_source": "deposits"},
                      headers=AUTH).json() == []
    assert conn.execute(
        "SELECT entity FROM graph_node WHERE catalog_source='deposits' AND object_ref=%s",
        (hit["object_ref"],)).fetchone()[0] == "Customer"


def test_entity_apply_missing_suggestion_404(make_client, conn):
    client = make_client(_fake())
    r = client.post("/entity/apply",
                    json={"catalog_source": "deposits", "object_ref": "public.accounts.nope"},
                    headers=AUTH)
    assert r.status_code == 404
