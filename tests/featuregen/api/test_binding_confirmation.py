"""Delivery H1b — Gate-1 role-binding confirmation over HTTP.

The /contract/draft response exposes the exact role bindings (role / column-ref / source / authority /
warnings) + a deterministic ``binding_hash``; /contract/confirm carries the hash the client saw and
FAILS CLOSED (409) if the server-authoritative bindings drifted since draft. Confirm mints its OWN
durable requirement ids (a client-supplied id/"passed" is ignored) and writes ONLY the contract's rows
— never global catalog ``field_evidence`` / fact authority.

These drive the REAL considered-set → draft → confirm route flow (the gate lives in the route),
mutating the shared rolled-back conn between draft and confirm to simulate drift.

Two things changed with the E4 cutover (2026-08-14):

* **How a draftable option is obtained.** The free-form generator that used to invent
  ``avg_balance_90d`` from a scripted ``overlay.feature.recommend`` response is deleted, so every
  option now comes from the ONE engine — which means a real ``catalog_source`` + ``confirmed_scope``
  and an option whose activation blockers have been cleared through their real surfaces. That ritual
  is the E0 walkthrough's; :func:`governed_ready_round` performs it here and is imported by the other
  API suites that need a genuinely draftable option.
* **Which gate catches a drifted catalog.** Every served option is now a SEMANTIC option with a frozen
  decision row, so the activation fold re-verifies the SEALED metadata snapshot at the governing write
  — earlier than the H1b binding-hash comparator. A column retyped, an as-of fact retired or a
  sensitivity raised between draft and confirm is therefore refused as ACTIVATION_BLOCKED /
  SNAPSHOT_STALE_REGENERATE rather than "bindings changed". The guarantee those tests exist for (fail
  closed, finalize NOTHING) is unchanged and now fires sooner; they assert the refusal that actually
  happens. The binding-hash gate itself is still exercised directly, by a confirm carrying a hash that
  does not match the server's authoritative bindings.
"""
from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_e2e_walkthrough import (
    AUTH_2,
    GOV,
    SOURCE,
    _card,
    _cib,
    _considered_set,
    _fake,
)

#: Re-exported so the suites below (and the sibling API suites) name one fixture, not three.
CATALOG = SOURCE
#: A derives-role binding on the hero candidate — the column the drift mutations move.
DERIVES_REF = "public.accounts.complaint_flag"
AS_OF_REF = "public.accounts.as_of_date"


def seal_for_real(monkeypatch) -> None:
    """The route harness shares ONE READ COMMITTED test transaction, so the C0 catalog seal's isolation
    gate would skip snapshotting and every semantic option would be undraftable (the activation fold
    fails CLOSED on an unverifiable snapshot). Stub the two isolation gates — the same pair the E0
    walkthrough stubs — so generation SEALS FOR REAL and draft/confirm re-read exactly as they do live."""
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._on_repeatable_read", lambda conn: True)
    monkeypatch.setattr(
        "featuregen.overlay.upload.feature_metadata_snapshot._assert_repeatable_read",
        lambda conn: "repeatable read")


def governed_ready_round(client) -> tuple[dict, dict]:
    """Generate, then clear the hero candidate's activation blockers through their REAL surfaces
    (concept funnel → recipe reviews → confirmed unit of analysis) and regenerate under the same
    intent. Returns ``(considered-set body, the complaint_count card)`` — post-cutover the only shape
    a draft can legitimately proceed from, since the engine is the sole source of options and its
    cards are honestly blocked until a human clears each named blocker."""
    round1 = _considered_set(client)

    queue = client.get(f"/governance/concept-confirmations?source={CATALOG}", headers=GOV).json()
    items = [{
        "object_ref": col["object_ref"], "action": "confirm_existing",
        "evidence_id": col["evidence_id"],
        "expected_latest_decision_id": col["latest_decision_id"],
        "expected_evidence_set_hash": col["evidence_set_hash"],
        "expected_policy_version": col["policy_version"],
    } for group in queue["groups"] for col in group["columns"]]
    assert items, "the funnel queue must offer the proposed concepts"
    confirmed = client.post("/governance/concept-confirmations", json={
        "source": CATALOG, "reason": "binding-confirmation fixture — SME confirms the proposals",
        "items": items}, headers=GOV).json()
    assert confirmed["accepted_count"] == len(items), confirmed

    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
    from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles

    hero = v2_recipe_by_id("complaint_count")
    live_hash = canonical_recipe_v2_hash(hero)
    for i, role in enumerate(required_reviewer_roles(hero)):
        rr = client.post("/recipes/complaint_count/reviews", headers=(AUTH, AUTH_2)[i % 2], json={
            "decision": "approved", "reviewer_role": role,
            "reviewed_revision_hash": live_hash,
            "rationale": "binding-confirmation fixture — definition matches the banking meaning"})
        assert rr.status_code == 201, rr.text

    round2 = _considered_set(client, intent_id=round1["intent_id"], uoa=True)
    return round2, _card(round2, "complaint_count")


def _draft(client, body: dict, card: dict) -> dict:
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"], "why": "best fit"}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    return dr.json()


def _confirm_body(draft_resp: dict, intent_id: str, **overrides) -> dict:
    body = dict(draft_resp["draft"])
    body["intent_id"] = intent_id
    body["expected_binding_hash"] = draft_resp["binding_hash"]
    body.update(overrides)
    return body


def _ready(make_client, conn, monkeypatch):
    """The whole prelude: sealed generation, cleared blockers, a real draft in hand."""
    seal_for_real(monkeypatch)
    _cib(conn)
    client = make_client(_fake())
    body, card = governed_ready_round(client)
    return client, body, _draft(client, body, card)


def _contract_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM contract").fetchone()[0]


def _blocker_codes(response) -> set:
    detail = response.json()["detail"]
    assert detail["code"] == "ACTIVATION_BLOCKED", detail
    return {b["code"] for b in detail["blockers"]}


# ── TEST 1 — the draft exposes per-binding role/ref/source/authority/warnings + a binding_hash; a
#             confirm with the MATCHING hash succeeds. ──────────────────────────────────────────────
def test_draft_exposes_bindings_and_hash_and_confirm_matches(make_client, conn, monkeypatch):
    client, body, dr = _ready(make_client, conn, monkeypatch)

    assert dr["binding_hash"], "draft must expose a binding_hash"
    bindings = dr["bindings"]
    assert bindings, "draft must expose the confirmed role bindings"
    for b in bindings:
        assert set(b) == {"role", "ref", "source", "authority", "warnings"}
    roles = {b["role"] for b in bindings}
    # derives + grain + as_of all surface as role bindings (the engine-bound complaint_count card).
    assert {"derives", "grain", "as_of"} <= roles
    assert any(b["role"] == "derives" and b["ref"] == DERIVES_REF for b in bindings)
    assert all(b["source"] == CATALOG for b in bindings)

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1


# ── TEST 2 — 409 on a binding_hash the server does not agree with: the H1b gate itself, reached with
#             the sealed snapshot untouched so nothing earlier fires. ───────────────────────────────
def test_confirm_409_when_the_binding_hash_the_client_saw_no_longer_matches(
        make_client, conn, monkeypatch):
    """A client confirming against a binding set that is not the server's authoritative one is refused
    and finalizes nothing. This is the H1b comparator on its own: no catalog mutation, so the sealed
    snapshot still verifies and the activation fold (which after the E4 cutover would otherwise catch
    a drifted catalog first — see the two tests below) lets the request through to this gate."""
    client, body, dr = _ready(make_client, conn, monkeypatch)
    before = _contract_count(conn)

    stale = _confirm_body(dr, body["intent_id"],
                          expected_binding_hash="0" * len(dr["binding_hash"]))
    cr = client.post("/contract/confirm", json=stale, headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "bindings changed" in cr.json()["detail"]
    assert _contract_count(conn) == before, "a disagreed binding set must finalize NO contract"


# ── TEST 3 — a binding's underlying column is RETYPED between draft and confirm → fail closed. ─────
def test_confirm_fails_closed_when_a_binding_column_is_retyped(make_client, conn, monkeypatch):
    """Retyping a bound column is catalog DRIFT. Post-cutover the governing write re-verifies the
    sealed metadata snapshot before it ever recomputes the binding hash, so the refusal arrives
    earlier and stronger: ACTIVATION_BLOCKED / SNAPSHOT_STALE_REGENERATE, no contract."""
    client, body, dr = _ready(make_client, conn, monkeypatch)
    before = _contract_count(conn)

    conn.execute("UPDATE graph_node SET declared_type = 'text', data_type = 'text' "
                 "WHERE catalog_source = %s AND object_ref = %s", (CATALOG, DERIVES_REF))

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "SNAPSHOT_STALE_REGENERATE" in _blocker_codes(cr)
    assert _contract_count(conn) == before, "a drifted binding set must finalize NO contract"


# ── TEST 4 — confirm-time revalidation: a referenced fact EXPIRES / becomes unauthorized between draft
#             and confirm → fail closed, never a promoted stamp over a drifted fact. ────────────────
def test_confirm_revalidation_fails_closed_on_expired_fact(make_client, conn, monkeypatch):
    """The as-of FACT is retired (its governing is_as_of flag is projected away) between draft and
    confirm. The sealed snapshot no longer describes the world, so the governing write refuses."""
    client, body, dr = _ready(make_client, conn, monkeypatch)
    before = _contract_count(conn)

    conn.execute("UPDATE graph_node SET is_as_of = false "
                 "WHERE catalog_source = %s AND object_ref = %s", (CATALOG, AS_OF_REF))

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "SNAPSHOT_STALE_REGENERATE" in _blocker_codes(cr)
    assert _contract_count(conn) == before, "an expired fact must not finalize a contract"


def test_confirm_revalidation_fails_closed_on_unauthorized_fact(make_client, conn, monkeypatch):
    """A referenced column becoming read-scope RESTRICTED (a sensitivity tag = the C1 authority axis)
    between draft and confirm moves the catalog state the option was sealed against → fail closed."""
    client, body, dr = _ready(make_client, conn, monkeypatch)
    before = _contract_count(conn)

    conn.execute("UPDATE graph_node SET sensitivity = 'restricted' "
                 "WHERE catalog_source = %s AND object_ref = %s", (CATALOG, DERIVES_REF))

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "SNAPSHOT_STALE_REGENERATE" in _blocker_codes(cr)
    assert _contract_count(conn) == before


# ── TEST 5 — a client-supplied requirement_id / "passed" is IGNORED; the server mints its OWN durable
#             requirement ids and never trusts a client "passed". ───────────────────────────────────
def test_client_supplied_requirement_and_passed_are_ignored(make_client, conn, monkeypatch):
    client, body, dr = _ready(make_client, conn, monkeypatch)

    confirm = _confirm_body(dr, body["intent_id"],
                            requirement_id="req_forged_by_client",
                            passed=True,
                            requirements=[{"requirement_id": "req_forged_by_client",
                                           "code": "TYPE_IS_NUMERIC", "passed": True}])
    cr = client.post("/contract/confirm", json=confirm, headers=AUTH)
    assert cr.status_code == 200, cr.text
    contract_id = cr.json()["contract_id"]

    # every persisted requirement id is SERVER-minted (req_*) — never the client's forged id.
    rows = conn.execute(
        "SELECT requirement_id FROM feature_validation_requirement WHERE contract_id = %s",
        (contract_id,)).fetchall()
    ids = [r[0] for r in rows]
    assert ids, "the governed contract records its own validation requirements"
    assert "req_forged_by_client" not in ids
    assert all(rid.startswith("req") for rid in ids)
    # no EXTERNAL_PASSED was fabricated from the client "passed" — the stamp reflects real validation only.
    passed_events = conn.execute(
        "SELECT count(*) FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'EXTERNAL_PASSED'", (contract_id,)).fetchone()[0]
    assert passed_events == 0


# ── TEST 6 — SCOPED: a confirm writes only the contract's rows; it mutates NO global catalog
#             field_evidence / graph_node / graph_edge authority. ───────────────────────────────────
def test_confirm_writes_no_global_field_or_fact_authority(make_client, conn, monkeypatch):
    client, body, dr = _ready(make_client, conn, monkeypatch)

    def _counts():
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("field_evidence", "field_decision_event", "graph_node", "graph_edge")}

    # Snapshotted AFTER the funnel confirmations above: those are a governance surface writing global
    # authority on purpose. What must not move is the CONFIRM's own footprint.
    global_before = _counts()
    inputs_before = conn.execute("SELECT count(*) FROM contract_input_column").fetchone()[0]

    cr = client.post("/contract/confirm", json=_confirm_body(dr, body["intent_id"]), headers=AUTH)
    assert cr.status_code == 200, cr.text

    assert _counts() == global_before, "confirm must not write global field/fact authority rows"
    # ...but it DID write the contract-scoped role-binding lineage (proves the write happened, scoped).
    inputs_after = conn.execute("SELECT count(*) FROM contract_input_column").fetchone()[0]
    assert inputs_after > inputs_before


# ── TEST 7 — legacy degradation: a confirm body with NO expected_binding_hash still succeeds (the gate
#             is required going forward but never breaks a pre-H1b client). ─────────────────────────
def test_legacy_confirm_without_expected_hash_still_succeeds(make_client, conn, monkeypatch):
    client, body, dr = _ready(make_client, conn, monkeypatch)

    confirm = dict(dr["draft"])
    confirm["intent_id"] = body["intent_id"]
    confirm.pop("expected_binding_hash", None)   # a pre-H1b client sends none
    cr = client.post("/contract/confirm", json=confirm, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1
