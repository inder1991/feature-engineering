"""Delivery H1b — Gate-1 role-binding confirmation over HTTP.

The /contract/draft response exposes the exact role bindings (role / column-ref / source / authority /
warnings) + a deterministic ``binding_hash``; /contract/confirm carries the hash the client saw and
FAILS CLOSED (409) if the server-authoritative bindings drifted since draft (a column retyped, a fact
retired/expired, an authority changed) — the role-binding analog of the plan-staleness 409, over the
SAME reconciled inputs H2b persists as ``contract_input_column`` rows. Confirm mints its OWN durable
requirement ids (a client-supplied id/"passed" is ignored) and writes ONLY the contract's rows — never
global catalog ``field_evidence`` / fact authority.

These drive the REAL upload → considered-set → draft → confirm route flow (the gate lives in the route),
mutating the shared rolled-back conn between draft and confirm to simulate drift.
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
            "recommended_lens": "monetary", "reasoning": "fits the hypothesis"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Average 90-day end-of-day ledger balance per account."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def _intent_id(client) -> str:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "definition": "90-day average balance per account",
        "objective": "predict churn", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()["intent_id"]


def _draft(client, intent_id: str):
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "anchor",
        "chosen_option_id": "avg_balance_90d", "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    return dr.json()


def _confirm_body(draft_resp: dict, intent_id: str, **overrides) -> dict:
    body = dict(draft_resp["draft"])
    body["intent_id"] = intent_id
    body["expected_binding_hash"] = draft_resp["binding_hash"]
    body.update(overrides)
    return body


def _contract_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM contract").fetchone()[0]


# ── TEST 1 — the draft exposes per-binding role/ref/source/authority/warnings + a binding_hash; a
#             confirm with the MATCHING hash succeeds. ──────────────────────────────────────────────
def test_draft_exposes_bindings_and_hash_and_confirm_matches(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)

    assert dr["binding_hash"], "draft must expose a binding_hash"
    bindings = dr["bindings"]
    assert bindings, "draft must expose the confirmed role bindings"
    for b in bindings:
        assert set(b) == {"role", "ref", "source", "authority", "warnings"}
    roles = {b["role"] for b in bindings}
    # derives + grain + as_of all surface as role bindings (the deposits accounts anchor).
    assert {"derives", "grain", "as_of"} <= roles
    assert any(b["role"] == "derives" and b["ref"] == "public.accounts.balance" for b in bindings)

    cr = client.post("/contract/confirm", json=_confirm_body(dr, intent_id), headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1


# ── TEST 2 — 409 on drift: a binding's underlying column is RETYPED between draft and confirm, so the
#             server binding_hash changes; the confirm with the OLD hash 409s and finalizes nothing. ──
def test_confirm_409_when_binding_column_retyped(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)
    before = _contract_count(conn)

    # retype the derives column IN PLACE (a drifted binding) — no considered-set/choice change.
    conn.execute("UPDATE graph_node SET declared_type = 'text', data_type = 'text' "
                 "WHERE catalog_source = 'deposits' AND object_ref = 'public.accounts.balance'")

    cr = client.post("/contract/confirm", json=_confirm_body(dr, intent_id), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "bindings changed" in cr.json()["detail"]
    assert _contract_count(conn) == before, "a drifted binding set must finalize NO contract"


# ── TEST 3 — confirm-time revalidation: a referenced fact EXPIRES/becomes unauthorized between draft
#             and confirm → fail closed (409), never a promoted stamp over a drifted fact. ──────────
def test_confirm_revalidation_fails_closed_on_expired_fact(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)
    before = _contract_count(conn)

    # the as_of FACT is retired/expired (its governing is_as_of flag is projected away) between draft and
    # confirm — the as_of binding's state signature moves, so the binding_hash no longer matches.
    conn.execute("UPDATE graph_node SET is_as_of = false "
                 "WHERE catalog_source = 'deposits' AND object_ref = 'public.accounts.posted_at'")

    cr = client.post("/contract/confirm", json=_confirm_body(dr, intent_id), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert _contract_count(conn) == before, "an expired fact must not finalize a contract"


def test_confirm_revalidation_fails_closed_on_unauthorized_fact(make_client, conn):
    # a referenced column becoming read-scope RESTRICTED (a sensitivity tag = C1 authority axis) between
    # draft and confirm moves the binding's authority signature → the confirm 409s (fail closed).
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)
    before = _contract_count(conn)

    conn.execute("UPDATE graph_node SET sensitivity = 'restricted' "
                 "WHERE catalog_source = 'deposits' AND object_ref = 'public.accounts.balance'")

    cr = client.post("/contract/confirm", json=_confirm_body(dr, intent_id), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert _contract_count(conn) == before


# ── TEST 4 — a client-supplied requirement_id / "passed" is IGNORED; the server mints its OWN durable
#             requirement ids and never trusts a client "passed". ───────────────────────────────────
def test_client_supplied_requirement_and_passed_are_ignored(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)

    body = _confirm_body(dr, intent_id,
                         requirement_id="req_forged_by_client",
                         passed=True,
                         requirements=[{"requirement_id": "req_forged_by_client",
                                        "code": "TYPE_IS_NUMERIC", "passed": True}])
    cr = client.post("/contract/confirm", json=body, headers=AUTH)
    assert cr.status_code == 200, cr.text
    contract_id = cr.json()["contract_id"]

    # every persisted requirement id is SERVER-minted (req_*) — never the client's forged id.
    rows = conn.execute(
        "SELECT requirement_id FROM feature_validation_requirement WHERE contract_id = %s",
        (contract_id,)).fetchall()
    ids = [r[0] for r in rows]
    assert "req_forged_by_client" not in ids
    assert all(rid.startswith("req") for rid in ids)
    # no EXTERNAL_PASSED was fabricated from the client "passed" — the stamp reflects real validation only.
    passed_events = conn.execute(
        "SELECT count(*) FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'EXTERNAL_PASSED'", (contract_id,)).fetchone()[0]
    assert passed_events == 0


# ── TEST 5 — SCOPED: a confirm writes only the contract's rows; it mutates NO global catalog
#             field_evidence / graph_node / graph_edge authority. ───────────────────────────────────
def test_confirm_writes_no_global_field_or_fact_authority(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)

    def _counts():
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("field_evidence", "field_decision_event", "graph_node", "graph_edge")}

    global_before = _counts()
    inputs_before = conn.execute("SELECT count(*) FROM contract_input_column").fetchone()[0]

    cr = client.post("/contract/confirm", json=_confirm_body(dr, intent_id), headers=AUTH)
    assert cr.status_code == 200, cr.text

    assert _counts() == global_before, "confirm must not write global field/fact authority rows"
    # ...but it DID write the contract-scoped role-binding lineage (proves the write happened, scoped).
    inputs_after = conn.execute("SELECT count(*) FROM contract_input_column").fetchone()[0]
    assert inputs_after > inputs_before


# ── TEST 6 — legacy degradation: a confirm body with NO expected_binding_hash still succeeds (the gate
#             is required going forward but never breaks a pre-H1b client). ─────────────────────────
def test_legacy_confirm_without_expected_hash_still_succeeds(make_client, conn):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    intent_id = _intent_id(client)
    dr = _draft(client, intent_id)

    body = dict(dr["draft"])
    body["intent_id"] = intent_id
    body.pop("expected_binding_hash", None)   # a pre-H1b client sends none
    cr = client.post("/contract/confirm", json=body, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1
