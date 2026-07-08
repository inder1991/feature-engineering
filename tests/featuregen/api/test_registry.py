"""Registry read API + consumer-registration endpoints."""
from featuregen.overlay.upload.features import FeatureSpec, register_feature

from ._helpers import AUTH


def _feat(conn, name="avg_bal"):
    return register_feature(conn, FeatureSpec(name=name, aggregation="avg_90d",
                                              derives_from=(("bank", "public.accounts.balance"),)))


def test_registry_list_and_detail_with_stamp(make_client, conn):
    fid = _feat(conn)
    client = make_client()
    lst = client.get("/features", headers=AUTH)
    assert lst.status_code == 200 and any(f["feature_id"] == fid for f in lst.json())
    det = client.get(f"/features/{fid}", headers=AUTH)
    assert det.status_code == 200 and det.json()["verification"] == "UNVERIFIED"  # direct register
    assert client.get("/features/nope", headers=AUTH).status_code == 404


def test_consumer_registration_endpoints(make_client, conn):
    fid = _feat(conn)
    client = make_client()
    r = client.post(f"/features/{fid}/consumers",
                    json={"model_ref": "churn_v3", "purpose": "churn", "environment": "prod"},
                    headers=AUTH)
    assert r.status_code == 200 and r.json()["consumer_id"]
    cons = client.get(f"/features/{fid}/consumers", headers=AUTH)
    assert cons.status_code == 200 and cons.json()[0]["model_ref"] == "churn_v3"
    feats = client.get("/consumers/churn_v3/features", headers=AUTH)
    assert feats.status_code == 200 and feats.json()[0]["feature_id"] == fid
    assert client.post("/features/nope/consumers", json={"model_ref": "m"},
                       headers=AUTH).status_code == 404


def test_feature_360_has_no_hypothesis_for_a_directly_registered_feature(make_client, conn):
    fid = _feat(conn)
    body = make_client().get(f"/features/{fid}", headers=AUTH).json()
    assert body["hypothesis"] is None and body["contract"] is None   # not born from the hypothesis flow
    assert body["verification"] == "UNVERIFIED"   # directly registered => honestly UNVERIFIED (finding #4)
    assert body["consumers"] == []


def test_feature_360_read_scopes_the_lineage(make_client, conn):
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.graph import build_graph

    from ._helpers import PII_AUTH
    build_graph(conn, "bank", [CanonicalRow("bank", "accounts", "ssn", "text", sensitivity="pii")])
    fid = register_feature(conn, FeatureSpec(name="uses_pii", aggregation="count",
                                             derives_from=(("bank", "public.accounts.ssn"),)))
    # data_owner (no pii_reader): the pii lineage is WITHHELD (can't enumerate where pii lives)
    assert make_client().get(f"/features/{fid}", headers=AUTH).json()["derives_from"] == []
    # pii_reader: sees the lineage
    body = make_client().get(f"/features/{fid}", headers=PII_AUTH).json()
    assert any(d["object_ref"] == "public.accounts.ssn" for d in body["derives_from"])
