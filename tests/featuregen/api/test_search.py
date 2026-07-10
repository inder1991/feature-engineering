from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, PII_AUTH, upload_csv

# A second source, so OR-within-a-facet and exclude-own-facet counts are observable across sources.
CARDS_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
cards,card_holders,id,integer,y,,card holder key,,,,,,,Cardholder
cards,card_holders,balance,numeric,,,current card balance,,,,semi_additive,dollars,USD,Cardholder
"""

# Two columns that match the same query token EQUALLY (via definition), one grain one not — so the
# grain rank boost is the only tiebreaker and is observable end-to-end through the route.
BOOST_CSV = """\
source,table,column,type,is_grain,as_of,definition,sensitivity,joins_to,cardinality,additivity,unit,currency,entity
metrics,m,alpha,numeric,y,,shared widget token,,,,,,,
metrics,m,beta,numeric,,,shared widget token,,,,,,,
"""


def _bucket(facets, name, value):
    return next((b["count"] for b in facets[name] if b["value"] == value), None)


def test_search_returns_context_rich_hits(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.get("/search", params={"q": "balance"}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"hits", "facets", "total"}       # the new wire shape
    hit = next(h for h in body["hits"] if h["object_ref"] == "public.accounts.balance")
    assert hit["table"] == "accounts"
    assert hit["definition"] == "end-of-day ledger balance"
    assert hit["additivity"] == "semi_additive"
    assert hit["unit"] == "dollars"
    assert hit["currency"] == "USD"
    assert hit["entity"] == "Account"
    assert hit["is_grain"] is False


def test_empty_query_browses_all_read_scoped_fresh_rows(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.get("/search", headers=AUTH).json()          # no q at all
    refs = {h["object_ref"] for h in body["hits"]}
    # every fresh, read-scoped node is browsable (tables + columns); the pii email is withheld.
    assert "public.accounts.balance" in refs
    assert "public.accounts" in refs                            # table node too
    assert "public.customers.email" not in refs                 # pii hidden without the role
    assert body["total"] == len(body["hits"]) == 11
    # facets come back for the browse (no term needed).
    assert _bucket(body["facets"], "source", "deposits") == 11
    assert set(body["facets"]) == {"source", "domain", "sensitivity", "additivity",
                                   "entity", "kind", "grain", "as_of"}


def test_text_query_ranks_with_grain_boost(client):
    upload_csv(client, "metrics", BOOST_CSV)
    hits = client.get("/search", params={"q": "widget"}, headers=AUTH).json()["hits"]
    assert [h["column"] for h in hits] == ["alpha", "beta"]      # grain 'alpha' outranks 'beta'
    assert hits[0]["score"] > hits[1]["score"]                   # purely the grain boost
    # hits are always score-ordered, non-increasing.
    scores = [h["score"] for h in hits]
    assert scores == sorted(scores, reverse=True)


def test_and_across_or_within(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    # OR within source: both sources appear.
    body = client.get("/search", params={"source": ["deposits", "cards"]}, headers=AUTH).json()
    sources = {h["catalog_source"] for h in body["hits"]}
    assert sources == {"deposits", "cards"}
    # AND across groups: (source in deposits,cards) AND additivity = semi_additive.
    body = client.get("/search", params={"source": ["deposits", "cards"],
                                         "additivity": "semi_additive"}, headers=AUTH).json()
    assert body["total"] == 2
    assert {h["object_ref"] for h in body["hits"]} == {
        "public.accounts.balance", "public.card_holders.balance"}
    assert all(h["additivity"] == "semi_additive" for h in body["hits"])


def test_facet_buckets_exclude_own_facet(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    body = client.get("/search", params={"source": "deposits"}, headers=AUTH).json()
    facets = body["facets"]
    # The source facet ignores its OWN selection: cards is still listed at its full count.
    assert _bucket(facets, "source", "deposits") == 11
    assert _bucket(facets, "source", "cards") == 3
    # A DIFFERENT facet reflects the source=deposits filter: cards' entity 'Cardholder' is gone.
    assert _bucket(facets, "entity", "Cardholder") is None
    assert _bucket(facets, "entity", "Account") == 3
    assert body["total"] == 11


def test_read_scope_gates_sensitivity_facet_and_filter(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    # Without pii_reader: no 'pii' bucket, and asking for sensitivity=pii returns nothing.
    body = client.get("/search", headers=AUTH).json()
    assert _bucket(body["facets"], "sensitivity", "pii") is None
    gated = client.get("/search", params={"sensitivity": "pii"}, headers=AUTH).json()
    assert gated["hits"] == [] and gated["total"] == 0
    # With pii_reader: the bucket appears and the filter returns the row.
    body = client.get("/search", headers=PII_AUTH).json()
    assert _bucket(body["facets"], "sensitivity", "pii") == 1
    seen = client.get("/search", params={"sensitivity": "pii"}, headers=PII_AUTH).json()
    assert [h["object_ref"] for h in seen["hits"]] == ["public.customers.email"]


def test_hides_pii_without_role_shows_with(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    assert client.get("/search", params={"q": "email"}, headers=AUTH).json()["hits"] == []
    hits = client.get("/search", params={"q": "email"}, headers=PII_AUTH).json()["hits"]
    assert [h["object_ref"] for h in hits] == ["public.customers.email"]


def test_stale_source_absent_from_hits_and_facets(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    conn.execute(
        "UPDATE overlay_drift_watermark "
        "SET last_completed_at = last_completed_at - interval '3 days' "
        "WHERE catalog_source = %s", ("cards",))
    body = client.get("/search", headers=AUTH).json()
    assert {h["catalog_source"] for h in body["hits"]} == {"deposits"}   # cards withheld
    assert _bucket(body["facets"], "source", "cards") is None            # and absent from counts
    assert _bucket(body["facets"], "source", "deposits") == 11


def test_fails_closed_on_stale_source(client, conn):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    conn.execute(
        "UPDATE overlay_drift_watermark "
        "SET last_completed_at = last_completed_at - interval '3 days' "
        "WHERE catalog_source = %s", ("deposits",))
    res = client.get("/search", params={"q": "balance"}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["hits"] == [] and body["total"] == 0                     # absent, never a 500


def test_total_can_exceed_limit(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    upload_csv(client, "cards", CARDS_CSV)
    body = client.get("/search", params={"limit": 5}, headers=AUTH).json()
    assert len(body["hits"]) == 5
    assert body["total"] == 14                                          # total ignores the limit


def test_grain_and_asof_flag_filters(client):
    upload_csv(client, "deposits", DEPOSITS_CSV)
    grain = client.get("/search", params={"grain": "true"}, headers=AUTH).json()
    assert grain["hits"] and all(h["is_grain"] for h in grain["hits"])
    assert grain["total"] == _bucket(grain["facets"], "grain", "true") == 3
    as_of = client.get("/search", params={"as_of": "true"}, headers=AUTH).json()
    assert as_of["hits"] and all(h["is_as_of"] for h in as_of["hits"])
    assert as_of["total"] == 1


def test_search_requires_auth(client):
    assert client.get("/search", params={"q": "balance"}).status_code == 401


def test_search_limit_validated(client):
    assert client.get("/search", params={"q": "x", "limit": 0}, headers=AUTH).status_code == 422
    assert client.get("/search", params={"q": "x", "limit": 500}, headers=AUTH).status_code == 422
