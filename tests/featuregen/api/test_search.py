from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, PII_AUTH, upload_csv


def test_search_returns_context_rich_hits(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.get("/search", params={"q": "balance"}, headers=AUTH)
    assert res.status_code == 200
    hit = next(h for h in res.json() if h["object_ref"] == "public.accounts.balance")
    assert hit["table"] == "accounts"
    assert hit["definition"] == "end-of-day ledger balance"
    assert hit["additivity"] == "semi_additive"
    assert hit["unit"] == "dollars"
    assert hit["currency"] == "USD"
    assert hit["entity"] == "Account"
    assert hit["is_grain"] is False


def test_search_hides_pii_without_role_shows_with(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert client.get("/search", params={"q": "email"}, headers=AUTH).json() == []
    hits = client.get("/search", params={"q": "email"}, headers=PII_AUTH).json()
    assert [h["object_ref"] for h in hits] == ["public.customers.email"]


def test_search_fails_closed_on_stale_source(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    conn.execute(
        "UPDATE overlay_drift_watermark "
        "SET last_completed_at = last_completed_at - interval '3 days' "
        "WHERE catalog_source = %s", ("deposits",))
    res = client.get("/search", params={"q": "balance"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json() == []               # absent, never a 500


def test_search_requires_auth(client):
    assert client.get("/search", params={"q": "balance"}).status_code == 401


def test_search_limit_validated(client):
    assert client.get("/search", params={"q": "x", "limit": 0}, headers=AUTH).status_code == 422
    assert client.get("/search", params={"q": "x", "limit": 500}, headers=AUTH).status_code == 422
