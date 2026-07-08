from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse


def _fake() -> FakeLLM:
    return FakeLLM(script={
        # upload runs ingest enrichment first (must be scripted or upload 500s)
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_90d", "description": "avg balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_90d",
            "grain_table": "accounts"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits the hypothesis"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average 90-day end-of-day ledger balance per account."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def test_considered_set_returns_anchor_and_alternatives(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "definition": "90-day average balance per account",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["intent_id"]
    assert body["anchor"]["name"] == "avg_balance_90d"
    assert any(f["name"] == "avg_balance_90d"
               for s in body["alternatives"] for f in s["features"])
    assert body["recommendation"]["recommended_lens"] == "monetary"


def test_blank_hypothesis_is_422(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/considered-set", json={
        "hypothesis": "", "objective": "x", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422


def _intent_id(client) -> str:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "definition": "90-day average balance per account",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    return res.json()["intent_id"]


def test_draft_then_confirm_registers_contract(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    # draft the human's CHOSEN option (reconstructed server-side from the considered set)
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    assert draft["definition"].startswith("Average")
    assert dr.json()["unresolved"] == []
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200
    assert cr.json()["version"] == 1
    assert cr.json()["feature_id"].startswith("feat")


def test_draft_rejects_a_choice_not_in_the_considered_set_422(make_client):
    # BLOCKER 1: a feature that was never offered cannot be drafted
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    res = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "alternative",
        "chosen_option_id": "never_offered", "why": ""}, headers=AUTH)
    assert res.status_code == 422


def test_confirm_rejects_a_leaky_draft_422(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    leaky = {"feature_name": "x", "definition": "d", "grain_table": "accounts",
             "aggregation": "avg_90d", "as_of_column": "posted_at",
             "derives_from": ["public.accounts.balance"],
             "target_ref": "public.accounts.balance",   # derives the target -> leaks
             "derives_pairs": [["deposits", "public.accounts.balance"]], "join_path": []}
    res = client.post("/contract/confirm", json=leaky, headers=AUTH)
    assert res.status_code == 422


def test_feature_360_shows_hypothesis_lineage_and_stamp(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": "fit"}, headers=AUTH)
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    fid = client.post("/contract/confirm", json=draft, headers=AUTH).json()["feature_id"]
    # click the feature -> the 360 view carries the hypothesis it was born from
    body = client.get(f"/features/{fid}", headers=AUTH).json()
    assert body["hypothesis"]["hypothesis"].startswith("customers churn")
    assert body["contract"]["definition"]              # the governed narrative
    # governed via confirm_contract => BOTH the feature row and the contract row EARN DESIGN-CHECKED
    assert body["verification"] == "DESIGN-CHECKED"
    assert body["contract"]["verification"] == "DESIGN-CHECKED"
    assert body["derives_from"]                         # lineage present


def test_confirm_requires_intent_id_no_bare_draft_can_govern(make_client):
    # BLOCKER: a fully client-supplied draft with NO intent_id cannot govern (no provenance, and its
    # leakage target could be omitted). It must be rejected before any governing write.
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    bare = {"feature_name": "x", "definition": "d", "grain_table": "accounts", "aggregation": "avg_90d",
            "as_of_column": "posted_at", "derives_from": ["public.accounts.balance"],
            "derives_pairs": [["deposits", "public.accounts.balance"]], "join_path": []}
    assert client.post("/contract/confirm", json=bare, headers=AUTH).status_code == 422


def test_confirm_rejects_a_forged_intent_id(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={"intent_id": intent_id, "chosen_source": "anchor",
                     "chosen_option_id": "avg_balance_90d", "why": ""}, headers=AUTH)
    draft = dr.json()["draft"]
    draft["intent_id"] = "forged_intent_does_not_exist"
    assert client.post("/contract/confirm", json=draft, headers=AUTH).status_code == 422


def test_confirm_rejects_a_draft_tampered_off_the_chosen_feature(make_client):
    # BLOCKER: even with a valid intent_id, the confirmed draft must MATCH the human's recorded choice.
    # Tampering the derives (here, to add the target column) is rejected — it doesn't match the chosen set.
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = client.post("/contract/draft", json={"intent_id": intent_id, "chosen_source": "anchor",
                     "chosen_option_id": "avg_balance_90d", "why": ""}, headers=AUTH)
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    draft["derives_from"] = [*draft["derives_from"], "public.accounts.churned"]
    draft["derives_pairs"] = [*draft["derives_pairs"], ["deposits", "public.accounts.churned"]]
    assert client.post("/contract/confirm", json=draft, headers=AUTH).status_code == 422
