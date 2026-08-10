"""TASK 7 phase 1 — selection telemetry over HTTP: the north-star metric is a query from day zero.

The report joins what was already durable (every Gate-1 choice row carries its considered snapshot
+ the chosen option) — no new capture. ORIGIN rides every row (owner decision): recipe vs
llm_freeform vs user_defined is the two-engine answer nobody could give before. Zero rounds is the
honest zero report, never a 404.
"""
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_90d", "description": "avg balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
            "grain_table": "accounts"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average 90-day balance per account."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def test_zero_rounds_is_the_honest_zero_report(client):
    res = client.get("/contracts/selection-telemetry", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"rounds": 0, "by_origin": {}, "rows": []}


def test_a_recorded_gate1_choice_lands_with_its_origin(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    cs = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "definition": "90-day average balance per account",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert cs.status_code == 200
    intent_id = cs.json()["intent_id"]
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "alternative",
        "chosen_option_id": "avg_balance_90d", "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200

    report = client.get("/contracts/selection-telemetry", headers=AUTH).json()
    assert report["rounds"] == 1
    assert report["by_origin"]["llm_freeform"]["chosen"] == 1
    picked = next(r for r in report["rows"] if r["feature_name"] == "avg_balance_90d"
                  and r["generation_source"] == "llm_freeform")
    assert (picked["offered"], picked["chosen"]) == (1, 1)
    assert picked["recipe_id"] is None
    # every offered-but-unchosen candidate is a row too — rate needs the denominator
    assert all(r["chosen"] <= r["offered"] for r in report["rows"])


def test_the_report_is_read_gated(client):
    assert client.get("/contracts/selection-telemetry").status_code in (401, 403)
