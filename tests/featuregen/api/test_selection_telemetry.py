"""TASK 7 phase 1 — selection telemetry over HTTP: the north-star metric is a query from day zero.

The report joins what was already durable (every Gate-1 choice row carries its considered snapshot
+ the chosen option) — no new capture. ORIGIN rides every row (owner decision). The E4 cutover
(2026-08-14) narrowed what the origins can BE: ``llm_freeform`` is gone with the free-form
generator, so the live vocabulary is ``recipe`` (a registry recipe bound by the engine),
``llm_intent`` (an abstract intent the model proposed and the SAME binder grounded) and
``user_defined`` (the analyst's own definition through that same binder). The column still answers
the question it was built for — which source the human actually picks — over the origins that
still exist. Zero rounds is the honest zero report, never a 404.
"""
from tests.featuregen.api._helpers import AUTH

# The governed round is built ONCE, in test_contract: a confirmed churn scope over a catalog whose
# concepts are human-confirmed, and a hero recipe reviewed at its current revision. Reusing it here
# keeps this file about the REPORT rather than about re-staging the workflow that produces a choice.
from tests.featuregen.api.test_contract import HERO, _governed, _round


def test_zero_rounds_is_the_honest_zero_report(client):
    res = client.get("/contracts/selection-telemetry", headers=AUTH)
    assert res.status_code == 200
    assert res.json() == {"rounds": 0, "by_origin": {}, "rows": []}


def test_a_recorded_gate1_choice_lands_with_its_origin(make_client, conn, monkeypatch):
    """A real round: the human picks the engine's recipe card and the report attributes the
    choice to ``recipe`` — with the recipe id on the row, which is exactly the join the
    two-engine question used to need and now names the ONE engine's registry entry."""
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"],
        "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200, dr.text

    report = client.get("/contracts/selection-telemetry", headers=AUTH).json()
    assert report["rounds"] == 1
    assert report["by_origin"]["recipe"]["chosen"] == 1
    picked = next(r for r in report["rows"] if r["feature_name"] == card["name"]
                  and r["generation_source"] == "recipe")
    assert (picked["offered"], picked["chosen"]) == (1, 1)
    assert (picked["recipe_id"] or "").split("@")[0] == HERO
    # every offered-but-unchosen candidate is a row too — rate needs the denominator
    assert all(r["chosen"] <= r["offered"] for r in report["rows"])
    # And the retired origin never appears: nothing can produce it any more.
    assert "llm_freeform" not in report["by_origin"]


def test_the_report_is_read_gated(client):
    assert client.get("/contracts/selection-telemetry").status_code in (401, 403)
