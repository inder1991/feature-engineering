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
        # the upload path's enrichment tasks (same scripts as the sibling contract-flow tests).
        # `monetary` is NOT a registered concept name (the registry holds `monetary_stock`,
        # `monetary_flow`, ...), so every column here lands UNREGISTERED — which is the third
        # tier of the T7 confirm gate: nothing certifies it as a label, and nothing denies it.
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        INTAKE_TICKET_TASK: FakeResponse(output={
            "target_ref": target, "target_window_days": 90,
            "target_type": "binary_classification", "business_domain": [],
            "confidence": "high",
            "runner_up_refs": ["public.transactions.amount"]})})


def _with_concept(concept: str, target: str = _BALANCE) -> FakeLLM:
    """`_fake`, with every column enriched to one REGISTERED concept — the tier under test."""
    fake = _fake(target)
    fake._task_fallback["overlay.enrich.concept"] = [FakeResponse(output={"concept": concept})]
    return fake


#: The one concept that COMMITS: `outcome_label` declares `leakage_anchor=True`.
def _outcome_fake(target: str = _BALANCE) -> FakeLLM:
    return _with_concept("outcome_label", target)


#: NEAR_LABEL — the concept `cust_susp_flg` actually carried on the 2026-08-24 AML run.
def _proxy_fake(target: str = _BALANCE) -> FakeLLM:
    return _with_concept("restriction_status", target)


#: STANDARD — the registry positively declassified it: neither the label nor label-adjacent.
def _standard_fake(target: str = _BALANCE) -> FakeLLM:
    return _with_concept("customer_relationship_status", target)


#: The acknowledgment the confirm gate asks for on ANY non-outcome target. It asserts only what it
#: says — "I know the registry does not certify this as the outcome label" — never a correlation.
_ACK = {"target_not_outcome_acknowledged": True}


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
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
    assert confirm.status_code == 200
    ref, window, ttype, provenance, by = _reading(conn, intent_id)
    assert (ref, window, ttype) == (_BALANCE, 90, "binary_classification")
    assert provenance == "human_confirmed"
    assert by == "user:tester"
    # the leakage gate's existing server-side read sees the signed value — zero extra wiring
    assert intent_target_ref(conn, intent_id) == _BALANCE


def test_a_typed_name_pins_an_OUTCOME_column_and_is_recorded_without_a_click(make_client, conn):
    """shows-doesn't-gate, kept exactly where it is warranted: the column the person typed IS the
    label, so recording it asserts nothing the registry does not already certify."""
    client = make_client(_outcome_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH)
    body = res.json()
    assert body["ticket"]["pinned"] is True
    assert body["ticket"]["target_leakage_class"] == "outcome"
    ref, _, _, provenance, by = _reading(conn, body["intent_id"])
    assert (ref, provenance) == (_BALANCE, "user_typed")
    assert by == "user:tester"


def test_a_typed_name_pinning_a_NON_OUTCOME_column_records_NOTHING_until_acknowledged(
        make_client, conn):
    """FIX ROUND NB-2 — the pin door, closed.

    Typing the column name in prose used to write it durably onto the exact row the leakage gate
    reads, at `user_typed`, with no disclosure — while clicking Confirm on the SAME column was a
    422. Two doors, opposite answers, one user. The ticket still LABELS the pin (the screen loses
    nothing); the acknowledged confirm is now the only way it becomes a record.
    """
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    intent_id = body["intent_id"]
    assert body["ticket"]["pinned"] is True, "the pin still HAPPENS — it is pure code"
    assert body["ticket"]["target_column"] == _BALANCE, "and the screen still shows it"
    assert body["ticket"]["target_is_proxy"] is True, "labelled, not hidden"
    assert _reading(conn, intent_id)[3] is None, \
        "but nothing durable is written — the confirm gate is the one door"
    assert intent_target_ref(conn, intent_id) is None

    client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
    ref, _, _, provenance, _ = _reading(conn, intent_id)
    assert (ref, provenance) == (_BALANCE, "human_confirmed")


def test_intake_never_clobbers_a_signed_reading(make_client, conn):
    """Re-running intake on a confirmed intent is a READ, not a write."""
    client = make_client(_outcome_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    hypothesis = "Predict balance from customer activity."
    intent_id = client.post("/contract/intake", json={
        "hypothesis": hypothesis, "catalog_source": "deposits"},
        headers=AUTH).json()["intent_id"]
    # `amount`'s concept is refused by the concept critic in this fixture, so it lands with NO
    # registered concept — the gate's third tier, and the acknowledgment is owed.
    client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "corrected",
        "target_ref": "public.transactions.amount",
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
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
        "business_domain": ["made_up_domain"], **_ACK}, headers=AUTH)
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
    while the screen shows the new one). A human-signed reading stays untouchable.

    Enriched to `outcome_label` so the pin RECORDS at all — NB-2 closed the pin door for every
    non-outcome column, and this test is about the pin-vs-pin and pin-vs-human precedence."""
    client = make_client(_outcome_fake())
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
    # but once the human signs, intake becomes a pure read (covered end-to-end here too).
    # `amount` lands unregistered in this fixture, so the correction owes the acknowledgment.
    client.post("/contract/intake/target", json={
        "intent_id": first["intent_id"], "decision": "corrected",
        "target_ref": "public.transactions.amount",
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
    client.post("/contract/intake", json={
        "hypothesis": "Predict balance from customer activity.",
        "catalog_source": "deposits"}, headers=AUTH)
    ref, _, _, provenance, _ = _reading(conn, first["intent_id"])
    assert (ref, provenance) == ("public.transactions.amount", "human_confirmed")


# ══ T7 (c) — confirming a non-outcome target is an ACKNOWLEDGED act ══════════════════════════════
#
# The gate is BREADTH-first and ASSERTION-careful, and the two are different questions. It fires on
# every target the registry does not certify as an outcome label — near_label, standard and
# unregistered alike, because none of them warrants a silent commit. But what it SAYS differs per
# tier, because the registry says different things: near_label asserts label-adjacency, standard
# positively denies it, and an unregistered concept asserts nothing at all.

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


def _intent_for(client, hypothesis="Customers likely to be flagged for AML review.") -> str:
    return client.post("/contract/intake", json={
        "hypothesis": hypothesis, "catalog_source": "deposits"},
        headers=AUTH).json()["intent_id"]


def test_the_NEAR_LABEL_refusal_speaks_of_a_proxy_and_quotes_the_registry_s_warrant(
        make_client, conn):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "restriction_status" in detail and "near_label" in detail
    assert "PROXY" in detail, "here the proxy word is EARNED — the registry asserts adjacency"
    assert "BORDER" in detail, "and the registry's own warrant is quoted, not paraphrased"
    assert "target_not_outcome_acknowledged" in detail, \
        "the refusal names the field the client must send"
    assert _reading(conn, intent_id)[3] is None, "nothing was recorded behind the refusal"


def test_the_STANDARD_refusal_claims_NO_correlation_it_only_withholds_certification(
        make_client, conn):
    """FIX ROUND NB-1 — a `standard` concept is one the registry positively DEclassified. Calling
    it "a PROXY for the outcome" asserted a correlation nobody measured; the honest refusal
    withholds certification and stops."""
    client = make_client(_standard_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "customer_relationship_status" in detail and "standard" in detail
    assert "does not certify" in detail
    assert "nothing here asserts it correlates" in detail
    assert "PROXY" not in detail and "proxy" not in detail, \
        "the registry never said this borders the label — the refusal must not say so either"
    assert "target_not_outcome_acknowledged" in detail
    assert _reading(conn, intent_id)[3] is None


def test_the_UNREGISTERED_refusal_says_absence_is_not_an_assertion(make_client, conn):
    client = make_client(_fake())            # `monetary` is not a registered concept name
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422
    detail = res.json()["detail"]
    assert "no registered concept" in detail
    assert "absence is not an assertion" in detail
    assert "PROXY" not in detail and "proxy" not in detail
    assert "target_not_outcome_acknowledged" in detail
    assert _reading(conn, intent_id)[3] is None


def test_an_OUTCOME_target_needs_no_acknowledgment_at_all(make_client, conn):
    client = make_client(_outcome_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    body = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits"}, headers=AUTH)
    assert body.status_code == 200
    assert body.json()["target_leakage_class"] == "outcome"
    assert body.json()["target_is_proxy"] is False
    assert _reading(conn, intent_id)[3] == "human_confirmed"


def test_the_acknowledgment_lets_the_confirmation_through_and_is_disclosed_back(
        make_client, conn):
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    res = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits", **_ACK}, headers=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["target_is_proxy"] is True
    assert body["target_leakage_class"] == "near_label"
    assert body["target_concept"] == "restriction_status"
    ref, _, _, provenance, _ = _reading(conn, intent_id)
    assert (ref, provenance) == (_BALANCE, "human_confirmed")
    # The acknowledgment is OBSERVABLE — a counter and a log line carrying the class. It is NOT a
    # durable record of the disclosure: `contract_intent` has no column that can carry one and this
    # program adds no migrations, so that home stays a ledgered owner item. What actually holds is
    # the refusal above: an unacknowledged non-outcome confirmation cannot be recorded at all.
    assert counters.snapshot()["counters"].get(
        "overlay.intake.target_not_outcome_acknowledged", 0) >= 1


def test_the_acknowledgment_is_an_acknowledgment_never_a_classification(make_client):
    """A client sending the flag on a column the registry does NOT call label-adjacent does not get
    to relabel it — the class in the answer is the server's own derivation."""
    client = make_client(_fake())            # concepts land unregistered: nothing is asserted
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    body = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits", **_ACK}, headers=AUTH).json()
    assert body["target_is_proxy"] is False
    assert body["target_leakage_class"] is None


def test_a_standard_target_is_gated_but_is_never_CALLED_a_proxy_in_the_answer(make_client):
    client = make_client(_standard_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_for(client)
    body = client.post("/contract/intake/target", json={
        "intent_id": intent_id, "decision": "confirmed", "target_ref": _BALANCE,
        "catalog_source": "deposits", **_ACK}, headers=AUTH).json()
    assert body["target_leakage_class"] == "standard"
    assert body["target_is_proxy"] is False, \
        "gated for lack of certification, not for an adjacency nobody asserted"


def test_the_outcome_columns_the_catalog_HOLDS_ride_the_intake_response(make_client):
    """NB-3 over HTTP: the abstention answer names the label that exists, with its one-liner."""
    client = make_client(_proxy_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/contract/intake", json={
        "hypothesis": "Customers likely to be flagged for AML review in the next 90 days.",
        "catalog_source": "deposits"}, headers=AUTH).json()
    # every column here is near_label, so the catalog genuinely holds no label — and says so
    assert body["ticket"]["outcome_candidates"] == []
    assert body["outcome_candidate_details"] == []


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
