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
        ("/features/refine", {"candidate": {"name": "avg_balance"}, "instruction": "tighten it"}),
        ("/features/recommend-sets", {"objective": "churn"}),
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
    assert res.json()["rejections"] == []       # nothing was gauntlet-rejected this round


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
    # Rejection transparency: the human sees the rejected candidate with a machine-readable code.
    assert res.json()["rejections"] == [
        {"name": "avg_balance", "reason": "leaks target", "code": "LEAKAGE"}]


class _Recording:
    """Wraps a scripted FakeLLM and records every request so tests can assert on the wire inputs."""

    def __init__(self, inner):
        self._inner = inner
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return self._inner.call(request)


def test_recommend_forwards_human_feedback_to_every_generation_round(make_client):
    rec = _Recording(_fake())
    client = make_client(llm_client=rec)
    upload_csv(client, "deposits", DEPOSITS_CSV)
    client.post("/features/recommend",
                json={"objective": "predict churn", "catalog_source": "deposits",
                      "feedback": "more behavioral signals, fewer balance aggregates"},
                headers=AUTH)
    gen = [r for r in rec.requests if r.task == "overlay.feature.recommend"]
    assert gen                                   # at least one generation round ran
    assert all(r.inputs["catalog_metadata"]["feedback"]
               == "more behavioral signals, fewer balance aggregates" for r in gen)
    assert all("avoid" in r.inputs["catalog_metadata"] for r in gen)   # machine hints still present


def _refiner() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_30d", "description": "30 day average balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_30d",
            "grain_table": "customers", "rationale": "a shorter window reacts faster"}]}),
    })


def test_refine_returns_revised_candidate(make_client):
    client = make_client(llm_client=_refiner())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/refine", json={
        "candidate": {"name": "avg_balance_90d", "description": "90 day average balance",
                      "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
                      "grain_table": "customers"},
        "instruction": "use a 30 day window", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    revised = res.json()["revised"]
    assert revised["name"] == "avg_balance_30d"
    assert revised["aggregation"] == "avg_30d"
    assert revised["derives_from"] == ["public.accounts.balance"]
    assert revised["verification"] == "DESIGN-CHECKED"   # a revision is still only a proposal


def test_refine_llm_call_is_attributed_to_the_human_caller(make_client, conn):
    # IMPORTANT-3: every assist route threads the caller's IdentityEnvelope into the audited seam,
    # so the llm_call audit row names the HUMAN who asked (user:...), never the fallback service
    # enrichment actor.
    client = make_client(llm_client=_refiner())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/refine", json={
        "candidate": {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
                      "aggregation": "avg_90d"},
        "instruction": "use a 30 day window", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200 and "revised" in res.json()
    rows = conn.execute(
        "SELECT created_by FROM llm_call WHERE task = 'overlay.feature.recommend'").fetchall()
    assert rows                                            # the refine call was recorded
    assert all(r[0]["subject"] == "user:tester" for r in rows)
    assert all(r[0]["actor_kind"] == "human" for r in rows)


def test_refine_forwards_the_objective_to_the_llm_inputs(make_client):
    # Finding 9: the optional round goal in the request body reaches the model's inputs.
    rec = _Recording(_refiner())
    client = make_client(llm_client=rec)
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/refine", json={
        "candidate": {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"]},
        "instruction": "use a 30 day window", "catalog_source": "deposits",
        "objective": "predict churn"}, headers=AUTH)
    assert res.status_code == 200
    calls = [r for r in rec.requests if r.task == "overlay.feature.recommend"]
    assert len(calls) == 1
    assert calls[0].inputs["catalog_metadata"]["objective"] == "predict churn"


def test_refine_gauntlet_rejection_is_data_not_an_error(make_client):
    # The scripted revision derives from the target column -> the gauntlet rejects it. That is a
    # 200 with a structured rejection the human reads, never a 4xx/5xx.
    client = make_client(llm_client=_leaky())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/refine", json={
        "candidate": {"name": "orig", "derives_from": ["public.transactions.amount"]},
        "instruction": "use the balance instead", "catalog_source": "deposits",
        "target_ref": "public.accounts.balance"}, headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"rejected": {"reason": "leaks target", "code": "LEAKAGE"}}


def _multiset() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg",
            "grain_table": "customers"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "temporal",
            "reasoning": "recency signals move earliest for a churn horizon"}),
    })


def test_recommend_sets_returns_sets_and_advisory_recommendation(make_client):
    client = make_client(llm_client=_multiset())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend-sets",
                      json={"objective": "predict churn", "catalog_source": "deposits"},
                      headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    lenses = [s["lens"] for s in body["sets"]]
    # The router picked the lenses from the data's shape: deposits has an as-of column (temporal)
    # and unary always applies.
    assert "unary" in lenses and "temporal" in lenses
    assert all(s["features"][0]["name"] == "avg_balance" for s in body["sets"])
    rec = body["recommendation"]
    assert rec["recommended_lens"] == "temporal"
    assert rec["reasoning"]
    assert "backtest" in rec["caveat"]           # advisory, honestly caveated
    assert body["rejections"] == []


def test_recommend_sets_reports_rejections_and_null_recommendation(make_client):
    # Every lens's only candidate leaks the target: all sets come back empty, the aggregated
    # rejections say why, and there is no advisory recommendation over nothing (null, and no LLM
    # call is spent on one — recommend_set is unscripted here and would error if called).
    client = make_client(llm_client=_leaky())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/features/recommend-sets",
                      json={"objective": "predict churn", "catalog_source": "deposits",
                            "target_ref": "public.accounts.balance"},
                      headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["sets"] and all(s["features"] == [] for s in body["sets"])
    assert body["recommendation"] is None
    assert body["rejections"] == [
        {"name": "avg_balance", "reason": "leaks target", "code": "LEAKAGE"}]


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
