"""INTAKE BUILD increment 2 — the confirm gate over HTTP: draft reading in, HUMAN decision recorded.

/contract/intake runs the mandatory read (one cached governed call) and returns the DRAFT ticket for
the confirm screen; a literally-typed name is recorded server-side as ``user_typed`` with no click.
/contract/intake/target records the person's answer — the provenance flip to ``human_confirmed`` —
onto the SAME ``contract_intent`` row the leakage gate already reads (``intent_target_ref``), so the
veto downstream runs on the signed value with no further wiring. Author-only; catalog-validated;
never silently edited (off-vocabulary domain tokens are refused, not dropped — a human decision is
recorded verbatim or not at all).
"""
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.contract.gate1 import intent_target_ref
from featuregen.overlay.upload.contract.intake_ticket import INTAKE_TICKET_TASK
from featuregen.runtime.observability import counters

_BALANCE = "public.accounts.balance"
_EMAIL = "public.customers.email"


def _fake(target: str = _BALANCE) -> FakeLLM:
    return FakeLLM(script={
        # the upload path's enrichment tasks (same scripts as the sibling contract-flow tests)
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        INTAKE_TICKET_TASK: FakeResponse(output={
            "target_ref": target, "target_window_days": 90,
            "target_type": "binary_classification", "business_domain": [],
            "confidence": "high",
            "runner_up_refs": ["public.transactions.amount"]})})


def _reading(conn, intent_id: str) -> tuple:
    return conn.execute(
        "SELECT target_ref, target_window_days, target_type, target_provenance, "
        "target_confirmed_by FROM contract_intent WHERE intent_id = %s", (intent_id,)).fetchone()


def test_intake_returns_the_draft_ticket_and_the_confirm_screen_material(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their account activity drops off. Churn = 90 days of inactivity.",
        "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["reason"] == "extracted"
    assert body["ticket"]["target_column"] == _BALANCE
    assert body["ticket"]["pinned"] is False
    assert body["ticket"]["target_window_days"] == 90
    # the confirm screen's one-liner rides along: "I understood your target as X — <summary>"
    assert body["target_detail"]["ref"] == _BALANCE
    assert body["target_detail"]["catalog_source"] == "deposits"


def test_a_fuzzy_draft_is_NOT_a_recorded_decision_until_the_human_signs(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their deposits shrink over time.",
        "catalog_source": "deposits"}, headers=AUTH)
    intent_id = res.json()["intent_id"]
    assert _reading(conn, intent_id)[3] is None, "a model draft never lands as a decision"
    assert intent_target_ref(conn, intent_id) is None

    confirm = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "target_window_days": 90, "target_type": "binary_classification",
        "catalog_source": "deposits"}, headers=AUTH)
    assert confirm.status_code == 200
    ref, window, ttype, provenance, by = _reading(conn, intent_id)
    assert (ref, window, ttype) == (_BALANCE, 90, "binary_classification")
    assert provenance == "human_confirmed"
    assert by == "user:tester"
    # the leakage gate's existing server-side read sees the signed value — zero extra wiring
    assert intent_target_ref(conn, intent_id) == _BALANCE


def test_a_typed_name_pins_and_is_recorded_without_a_click(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH)
    body = res.json()
    assert body["ticket"]["pinned"] is True
    ref, _, _, provenance, by = _reading(conn, body["intent_id"])
    assert (ref, provenance) == (_BALANCE, "user_typed"), \
        "shows-doesn't-gate: the user's own typed name lands server-side with no click"
    assert by == "user:tester"


def test_intake_never_clobbers_a_signed_reading(make_client, conn):
    """Re-running intake on a confirmed intent is a READ, not a write."""
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    hypothesis = "Predict balance from customer activity."
    intent_id = client.post("/contract/intake", json={
        "hypothesis": hypothesis, "catalog_source": "deposits"},
        headers=AUTH).json()["intent_id"]
    client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "corrected",
        "target_ref": "public.transactions.amount",
        "catalog_source": "deposits"}, headers=AUTH)
    again = client.post("/contract/intake", json={
        "hypothesis": hypothesis, "catalog_source": "deposits"}, headers=AUTH)
    assert again.json()["intent_id"] == intent_id, "per-actor idempotent intent"
    ref, _, _, provenance, _ = _reading(conn, intent_id)
    assert (ref, provenance) == ("public.transactions.amount", "human_confirmed"), \
        "the human's correction survives a re-run; the pin recorder must not overwrite it"


def test_exploring_is_a_recorded_no_target_declaration(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "What features exist around customer deposit behaviour?",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "exploring",
        "target_ref": _BALANCE}, headers=AUTH)   # a smuggled ref is forced NULL
    assert res.status_code == 200
    ref, _, _, provenance, _ = _reading(conn, intent_id)
    assert (ref, provenance) == (None, "exploring"), \
        "exploring records the DECLARATION and no target — the veto honestly has nothing to guard"


def test_only_the_author_signs_and_only_real_readable_columns_are_signable(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their deposits shrink over time.",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    other = {"X-User": "other", "X-Roles": "platform_admin"}
    assert client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE},
        headers=other).status_code == 403
    assert client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed",
        "target_ref": "public.accounts.INVENTED"}, headers=AUTH).status_code == 422
    # AUTH carries no pii role — a column the confirmer cannot SEE cannot be their target
    assert client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _EMAIL},
        headers=AUTH).status_code == 422
    assert client.post("/contract/intake/target", json={
        "intent_id": "intent_nope", "decision": "confirmed", "target_ref": _BALANCE},
        headers=AUTH).status_code == 404


def test_off_vocabulary_domains_are_refused_never_silently_edited(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their deposits shrink over time.",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "business_domain": ["made_up_domain"]}, headers=AUTH)
    assert res.status_code == 422
    assert "made_up_domain" in res.json()["detail"]


def test_no_llm_degrades_and_never_blocks(make_client):
    client = make_client()   # no LLM configured at all
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from activity.", "catalog_source": "deposits"},
        headers=AUTH)
    assert res.status_code == 200
    assert res.json()["reason"] == "unavailable"
    assert res.json()["ticket"]["target_column"] == _BALANCE, "the pin is pure code"


def test_the_change_it_menu_rides_the_response_with_its_material(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their deposits shrink over time.",
        "catalog_source": "deposits"}, headers=AUTH)
    body = res.json()
    assert body["ticket"]["runners_up"] == ["public.transactions.amount"]
    assert body["runner_up_details"][0]["ref"] == "public.transactions.amount"


def test_a_new_pin_overwrites_a_prior_pin_but_never_a_human_decision(make_client, conn):
    """Review fix: pins are code-derived from the user's own current text, so a NEW pin may
    replace a PRIOR pin (a rename between sessions must not leave the record on the old column
    while the screen shows the new one). A human-signed reading stays untouchable."""
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    first = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    assert _reading(conn, first["intent_id"])[0] == _BALANCE
    # a DIFFERENT hypothesis pinning a different column is a different intent — so simulate the
    # rename by rewriting the stored pin, then re-running the SAME intake: the fresh pin wins.
    conn.execute("UPDATE contract_intent SET target_ref = %s WHERE intent_id = %s",
                 ("public.accounts.stale_ref", first["intent_id"]))
    again = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    assert again["intent_id"] == first["intent_id"]
    ref, _, _, provenance, _ = _reading(conn, first["intent_id"])
    assert (ref, provenance) == (_BALANCE, "user_typed"), "the new pin replaced the stale one"
    # but once the human signs, intake becomes a pure read (covered end-to-end here too)
    client.post("/contract/intake/target", json={
        "intent_id": first["intent_id"], "decision": "corrected",
        "target_ref": "public.transactions.amount",
        "catalog_source": "deposits"}, headers=AUTH)
    client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH)
    ref, _, _, provenance, _ = _reading(conn, first["intent_id"])
    assert (ref, provenance) == ("public.transactions.amount", "human_confirmed")


# ══ T7 (c) — proxy disclosure: confirming a non-outcome target is an ACKNOWLEDGED act ════════════

def _proxy_fake(target: str = _BALANCE) -> FakeLLM:
    """Every column enriched to `restriction_status` — the concept `cust_susp_flg` actually carried
    on the 2026-08-24 AML run. `near_label=True` in the registry: a compliance CONSEQUENCE, never
    the outcome. Any target here is therefore PROXY-labelled by the server."""
    fake = _fake(target)
    fake._task_fallback["overlay.enrich.concept"] = [
        FakeResponse(output={"concept": "restriction_status"})]
    return fake


def test_the_proposal_LABELS_a_proxy_target_and_abstains_on_it(make_client):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/contract/intake", json={
        "hypothesis": "Customers likely to be flagged for AML review in the next 90 days.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    ticket = body["ticket"]
    assert ticket["target_is_proxy"] is True
    assert ticket["target_leakage_class"] == "near_label"
    assert ticket["target_concept"] == "restriction_status"
    assert ticket["confidence"] == "abstain", "no outcome-family concept -> nothing auto-commits"
    assert ticket["proxy_candidates"][0]["ref"] == _BALANCE
    assert ticket["proxy_candidates"][0]["concept"] == "restriction_status"


def test_confirming_a_proxy_target_WITHOUT_the_acknowledgment_is_a_typed_refusal(make_client, conn):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "Customers likely to be flagged for AML review in the next 90 days.",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "restriction_status" in detail and "near_label" in detail
    assert "target_is_proxy" in detail, "the refusal names the field the client must send"
    assert _reading(conn, intent_id)[3] is None, "nothing was recorded behind the refusal"


def test_the_acknowledgment_lets_the_proxy_confirmation_through_and_is_disclosed_back(
        make_client, conn):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "Customers likely to be flagged for AML review in the next 90 days.",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "target_is_proxy": True, "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["target_is_proxy"] is True
    assert body["target_leakage_class"] == "near_label"
    assert body["target_concept"] == "restriction_status"
    ref, _, _, provenance, _ = _reading(conn, intent_id)
    assert (ref, provenance) == (_BALANCE, "human_confirmed")
    # ...and the disclosure itself is recorded. `contract_intent` has no column that can carry it
    # and this program adds no migrations, so the durable record is the counter + the log line —
    # a stated limit, not a silent omission. The SCHEMA GAP is an owner item.
    assert counters.snapshot()["counters"].get("overlay.intake.target_proxy_disclosed", 0) >= 1


def test_the_server_decides_the_class_the_client_only_acknowledges_it(make_client):
    """A client claiming `target_is_proxy` on a column the registry does NOT call a proxy does not
    get to relabel it — the answer is the server's own derivation."""
    client = make_client(_fake())            # concepts land unregistered: nothing is asserted
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "Customers leave when their deposits shrink over time.",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    body = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "target_is_proxy": True, "catalog_source": "deposits"}, headers=AUTH).json()
    assert body["target_is_proxy"] is False
    assert body["target_leakage_class"] is None


def test_an_exploring_declaration_needs_no_acknowledgment(make_client):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = client.post("/contract/intake", json={
        "hypothesis": "What features exist around AML review?",
        "catalog_source": "deposits"}, headers=AUTH).json()["intent_id"]
    assert client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "exploring"},
        headers=AUTH).status_code == 200, "there is no target to disclose anything about"


def test_the_window_refusal_rides_the_intake_response(make_client):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/contract/intake", json={
        "hypothesis": "Customers flagged for AML review within 30 days of onboarding.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    # the scripted ticket says 90; the objective says 30
    assert body["ticket"]["window_refusal"]["code"] == "WINDOW_CONTRADICTS_GOAL"
    assert body["ticket"]["window_refusal"]["stated_days"] == 30
    assert body["ticket"]["window_refusal"]["ticket_days"] == 90
    assert body["ticket"]["target_window_days"] is None
    assert body["ticket"]["window_source"] == "contradicted"
