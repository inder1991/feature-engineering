from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse


def _fake() -> FakeLLM:
    return FakeLLM(script={
        # Uploading via a configured client runs ingest enrichment first (ingest_upload ->
        # enrich_concepts/draft_definitions/classify_domains). FakeLLM raises KeyError on an
        # unscripted task, so these must be present or upload_csv 500s before we ever hit assist.
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance", "public.ghost.col"],
            "aggregation": "avg", "grain_table": "customers"}]}),
        "overlay.feature.recipe": FakeResponse(output={
            "grain_table": "customers", "derives_from": ["public.transactions.amount"],
            "aggregation": "sum", "as_of_column": None, "join_table": "transactions"}),
        "overlay.feature.leakage": FakeResponse(output={"leaks": [
            {"object_ref": "public.accounts.balance", "reason": "target-adjacent"},
            {"object_ref": "public.other.col", "reason": "not in derives_from"}]}),
    })


def _leaky() -> FakeLLM:
    # Same enrichment tasks as _fake (upload runs them first), plus a recommend response whose sole
    # grounded derives-from IS the target column, so the leakage gate must reject it every pass.
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance"],
            "aggregation": "avg", "grain_table": "customers"}]}),
    })


def test_assist_unconfigured_is_503_not_broken(client):
    for path, body in [
        ("/features/recommend", {"objective": "churn"}),
        ("/features/recipe", {"query": "spend", "catalog_source": "deposits"}),
        ("/features/leakage-check", {"derives_from": [], "target_ref": "x"}),
    ]:
        res = client.post(path, json=body, headers=AUTH)
        assert res.status_code == 503, path


def test_recommend_returns_grounded_proposals(make_client):
    client = make_client(llm_client=_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend",
                      json={"objective": "predict churn", "catalog_source": "deposits"},
                      headers=AUTH)
    proposals = res.json()["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["name"] == "avg_balance"
    assert proposals[0]["derives_from"] == ["public.accounts.balance"]   # hallucination dropped


def test_recommend_rejects_leaky_proposal_over_http(make_client):
    # The route always forwards target_ref, so the deterministic gauntlet's leakage gate is ON over
    # HTTP. A proposal deriving from the target column is rejected every pass; the loop retries to
    # its budget with 'avoid' hints (the single-response script just repeats), so it exhausts the
    # budget and returns nothing rather than the leaky feature.
    client = make_client(llm_client=_leaky())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend",
                      json={"objective": "predict churn", "catalog_source": "deposits",
                            "target_ref": "public.accounts.balance"},
                      headers=AUTH)
    assert res.json()["proposals"] == []


def test_recipe_combines_llm_intent_with_deterministic_join_path(make_client):
    client = make_client(llm_client=_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    recipe = client.post("/features/recipe",
                         json={"query": "total spend per customer", "catalog_source": "deposits"},
                         headers=AUTH).json()
    assert recipe["grain_table"] == "customers"
    assert recipe["derives_from"] == ["public.transactions.amount"]
    # customers -> accounts -> transactions traverses both joins in reverse: fan-OUT each hop.
    assert [s["cardinality"] for s in recipe["join_path"]] == ["1:N", "1:N"]


def test_leakage_check_filters_to_used_refs(make_client):
    client = make_client(llm_client=_fake())
    warnings = client.post("/features/leakage-check",
                           json={"derives_from": ["public.accounts.balance"],
                                 "target_ref": "public.labels.churned"},
                           headers=AUTH).json()["warnings"]
    assert warnings == [{"object_ref": "public.accounts.balance", "reason": "target-adjacent"}]
