from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv


def _register(client, name="avg_balance"):
    return client.post("/features", json={
        "name": name, "description": "average end-of-day balance per customer",
        "grain_table": "customers", "aggregation": "avg",
        "derives_from": [{"catalog_source": "deposits",
                          "object_ref": "public.accounts.balance"}]}, headers=AUTH)


def test_register_returns_feature_id(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = _register(client)
    assert res.status_code == 200
    assert res.json()["feature_id"].startswith("feat")


def test_freshness_follows_stalest_source(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    feature_id = _register(client).json()["feature_id"]
    assert client.get(f"/features/{feature_id}/freshness", headers=AUTH).json() == {
        "fresh": True, "stale_sources": []}
    conn.execute(
        "UPDATE overlay_drift_watermark "
        "SET last_completed_at = last_completed_at - interval '3 days' "
        "WHERE catalog_source = %s", ("deposits",))
    assert client.get(f"/features/{feature_id}/freshness", headers=AUTH).json() == {
        "fresh": False, "stale_sources": ["deposits"]}


def test_freshness_unknown_feature_404(client):
    assert client.get("/features/feat_nope/freshness", headers=AUTH).status_code == 404


def test_feature_impact_reverse_lineage(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    feature_id = _register(client).json()["feature_id"]
    res = client.get("/columns/public.accounts.balance/feature-impact",
                     params={"source": "deposits"}, headers=AUTH)
    assert res.json() == {"feature_ids": [feature_id]}


def test_register_validates_name(client):
    assert client.post("/features", json={"name": ""}, headers=AUTH).status_code == 422
