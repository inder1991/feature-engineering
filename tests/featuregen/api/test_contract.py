"""The Gate-#1 → draft → confirm route contract, walked over the ONE engine.

The E4 cutover (2026-08-14) deleted the free-form physical-column generator, so the convenient
LLM-proposed candidate these tests used to draft (``avg_balance_90d``, invented from a scripted
``overlay.feature.recommend`` response) no longer exists — and no mode, flag or catalog shape
brings it back. Every test below that needs an option to draft now takes the SAME route a human
does: a confirmed scope over one catalog, the semantic engine's recipe card, and the activation
blockers cleared through their real surfaces. That ritual is the E0 walkthrough's and lives in
:func:`tests.featuregen.api.test_binding_confirmation.governed_ready_round`, which this file
imports rather than restages — one fixture, not three.

The subjects under test are unchanged — pointer conflicts, forged intent ids, tamper detection,
stale-plan mapping, the 360 view — only the way an option is obtained is.
"""
from dataclasses import replace

from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv
from tests.featuregen.api.test_binding_confirmation import (
    CATALOG,
    governed_ready_round,
    seal_for_real,
)
from tests.featuregen.api.test_e2e_walkthrough import (
    CHURN,
    HYPOTHESIS,
    TARGET,
    _cib,
    _fake,
)

import featuregen.api.routes.contract as contract_routes

#: The hero the human drafts: a churn recipe that binds on the walkthrough catalog and whose
#: authored temporal contract compiles, so the activation fold can say yes once its blockers clear.
HERO = "complaint_count"


def _governed(make_client, conn, monkeypatch):
    """A client on a catalog and a recipe that can actually reach a governed contract."""
    seal_for_real(monkeypatch)
    _cib(conn)
    return make_client(_fake())


def _round(client) -> tuple[dict, dict]:
    """One generation round whose hero card is genuinely draftable: generate, clear every named
    blocker through its real surface (concept funnel → recipe reviews → confirmed unit of
    analysis), regenerate under the same intent."""
    return governed_ready_round(client)


def _hero(body: dict) -> dict:
    """The hero's served card — matched on its origin-neutral recipe id, never on position."""
    cards = [f for s in body["alternatives"] for f in s["features"]
             if (f.get("source_definition_id") or "").split("@")[0] == HERO]
    assert cards, [f.get("source_definition_id")
                   for s in body["alternatives"] for f in s["features"]]
    return cards[0]


def _draft(client, body: dict, card: dict, why: str = "best fit"):
    return client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"], "why": why}, headers=AUTH)


def test_considered_set_returns_anchor_and_alternatives(make_client, conn, monkeypatch):
    """Gate #1 still answers with an anchor, alternatives and an advisory recommendation — all
    three now sourced from the ONE engine. The anchor is the analyst's own definition EXTRACTED
    as an abstract intent and bound by the shared binder (server-assigned ``user_defined``,
    never read from model output); the alternatives are engine-bound cards."""
    client = _governed(make_client, conn, monkeypatch)
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS,
        "definition": "days since the customer's most recent complaint",
        "objective": "predict churn", "catalog_source": CATALOG, "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()

    assert body["intent_id"]
    assert body["anchor"]["generation_source"] == "user_defined"
    assert body["anchor"]["input_role_bindings"], "the SHARED binder chose the anchor's columns"
    assert _hero(body)["generation_source"] == "recipe"
    assert body["recommendation"]["recommended_lens"] == "monetary"
    # Every served card came from the engine — there is no lens left that could serve anything
    # else (the free-form generator is not scripted, so a dispatch would have raised).
    assert {s["lens"] for s in body["alternatives"]} <= {"engine", "actionable"}


def test_blank_hypothesis_is_422(make_client):
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/contract/considered-set", json={
        "hypothesis": "", "objective": "x", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422


def test_entity_only_request_is_refused_before_it_can_be_empty(make_client, conn, monkeypatch):
    """E4: a request with no ``catalog_source`` is refused with a typed 422. The engine plans
    over ONE frozen catalog context, and the free-form path that used to fill an entity-only
    page is deleted — so the only two possible answers are a refusal that names the missing
    input or a silently empty page. This is the refusal."""
    client = _governed(make_client, conn, monkeypatch)
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "SEMANTIC_REQUIRES_CATALOG_SOURCE"


def test_draft_then_confirm_registers_contract(make_client, conn, monkeypatch):
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    # draft the human's CHOSEN option (reconstructed server-side from the considered set)
    dr = _draft(client, body, card)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    assert draft["definition"].startswith("Count of complaint events")
    assert dr.json()["unresolved"] == []
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    assert cr.json()["version"] == 1
    assert cr.json()["feature_id"].startswith("feat")


def test_draft_rejects_a_choice_not_in_the_considered_set_422(make_client, conn, monkeypatch):
    # BLOCKER 1: a feature that was never offered cannot be drafted
    client = _governed(make_client, conn, monkeypatch)
    body, _card = _round(client)
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "never_offered",
        "expected_generation_run_id": body["generation_run_id"], "why": ""}, headers=AUTH)
    assert res.status_code == 422


def test_confirm_maps_pointer_conflict_to_409(make_client, conn, monkeypatch):
    """M-a: a ``ContractPointerConflict`` raised by ``confirm_contract`` (the pointer CAS lost a race)
    maps to HTTP 409 at the route, never escaping as an uncaught 500."""
    import featuregen.api.routes.contract as contract_routes
    from featuregen.overlay.upload.contract.govern import ContractPointerConflict

    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = _draft(client, body, card)
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]

    def _raise_conflict(*args, **kwargs):
        raise ContractPointerConflict("simulated pointer CAS loss")
    monkeypatch.setattr(contract_routes, "confirm_contract", _raise_conflict)

    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 409, cr.text
    assert "pointer conflict" in cr.json()["detail"]


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


def test_feature_360_shows_hypothesis_lineage_and_stamp(make_client, conn, monkeypatch):
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = _draft(client, body, card, why="fit")
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    fid = client.post("/contract/confirm", json=draft, headers=AUTH).json()["feature_id"]
    # click the feature -> the 360 view carries the hypothesis it was born from
    view = client.get(f"/features/{fid}", headers=AUTH).json()
    assert view["hypothesis"]["hypothesis"].startswith("long-tenured customers churn")
    assert view["contract"]["definition"]              # the governed narrative
    # governed via confirm_contract => the CONTRACT row earns DESIGN-CHECKED…
    assert view["contract"]["verification"] == "DESIGN-CHECKED"
    # …while the FEATURE's effective verification stays UNVERIFIED, because an engine recipe card
    # carries its outstanding runtime data checks with it (here: grain uniqueness). The deleted
    # free-form candidate declared none and so read DESIGN-CHECKED on both rows; the honest
    # post-cutover answer keeps "the design was reviewed" and "the data was checked" apart.
    assert view["verification"] == "UNVERIFIED"
    assert view["contract"]["effective_validation_status"] == "needs_external_validation"
    assert view["derives_from"]                         # lineage present


def test_confirm_requires_intent_id_no_bare_draft_can_govern(make_client):
    # BLOCKER: a fully client-supplied draft with NO intent_id cannot govern (no provenance, and its
    # leakage target could be omitted). It must be rejected before any governing write.
    client = make_client(_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    bare = {"feature_name": "x", "definition": "d", "grain_table": "accounts", "aggregation": "avg_90d",
            "as_of_column": "posted_at", "derives_from": ["public.accounts.balance"],
            "derives_pairs": [["deposits", "public.accounts.balance"]], "join_path": []}
    assert client.post("/contract/confirm", json=bare, headers=AUTH).status_code == 422


def test_confirm_rejects_a_forged_intent_id(make_client, conn, monkeypatch):
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = _draft(client, body, card, why="")
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = "forged_intent_does_not_exist"
    assert client.post("/contract/confirm", json=draft, headers=AUTH).status_code == 422


def test_confirm_rejects_a_draft_tampered_off_the_chosen_feature(make_client, conn, monkeypatch):
    # BLOCKER: even with a valid intent_id, the confirmed draft must MATCH the human's recorded choice.
    # Tampering the derives (here, to add the target column) is rejected — it doesn't match the chosen set.
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = _draft(client, body, card, why="")
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = dr.json()["intent_id"]
    draft["derives_from"] = [*draft["derives_from"], TARGET]
    draft["derives_pairs"] = [*draft["derives_pairs"], [CATALOG, TARGET]]
    assert client.post("/contract/confirm", json=draft, headers=AUTH).status_code == 422


# ── 3C.2a Task 6: the draft/confirm freshness-recheck route contract (409/422 fail-closed) ─────────────
def _stale_envelope():
    from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1
    return PlanEnvelopeV1(
        recipe_id="r", physical_plan_id="bp_1", generation_run_id="run", catalog_sources=(CATALOG,),
        ordered_path=(f"{CATALOG}:direct_catalog:",), contract_id="c1",
        contract_resolution_status="resolved", contract_reason_codes=(),
        catalog_fingerprint={CATALOG: "fp"}, compiler_version={"plan_contract": "1.0.0"},
        input_stamps=({"catalog_source": CATALOG, "compiler_input_fingerprint": "fp",
                       "head_seq": 1, "projection_checkpoint": 1},))


def test_draft_route_maps_stale_plan_to_409(make_client, conn, monkeypatch):
    # a governed feature whose pinned plan drifted → StalePlan → HTTP 409 (regenerate), never a draft.
    from featuregen.overlay.upload.contract.author import StalePlan
    from featuregen.overlay.upload.planner.contracts import ReplayFreshness
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)

    def _raise(*a, **k):
        raise StalePlan(ReplayFreshness.drifted, "bp_x")

    monkeypatch.setattr("featuregen.api.routes.contract.draft_contract", _raise)
    res = _draft(client, body, card, why="")
    assert res.status_code == 409, res.text


def test_draft_route_maps_cross_catalog_without_envelope_to_422(make_client, conn, monkeypatch):
    # a cross-catalog feature that reached drafting with no governed envelope → fail-closed 422.
    from featuregen.overlay.upload.contract.author import CrossCatalogPlanRequired
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)

    def _raise(*a, **k):
        raise CrossCatalogPlanRequired("cross-catalog feature has no governed plan envelope")

    monkeypatch.setattr("featuregen.api.routes.contract.draft_contract", _raise)
    res = _draft(client, body, card, why="")
    assert res.status_code == 422, res.text


def test_confirm_route_rechecks_freshness_and_maps_stale_to_409(make_client, conn, monkeypatch):
    # the GOVERNING write re-runs the freshness recheck against the SERVER-reconstructed chosen feature's
    # envelope (never the client body); a plan that drifted between draft and confirm → 409, never finalize.
    from featuregen.overlay.upload.feature_assist import FeatureIdea
    from featuregen.overlay.upload.planner.contracts import ReplayFreshness
    client = _governed(make_client, conn, monkeypatch)
    body, card = _round(client)
    dr = _draft(client, body, card, why="")
    assert dr.status_code == 200, dr.text
    draft = dr.json()["draft"]
    draft["intent_id"] = body["intent_id"]

    def _governed_chosen(*a, **k):
        return FeatureIdea(
            name=draft["feature_name"], description="", derives_from=draft["derives_from"],
            aggregation=draft["aggregation"], grain_table=draft["grain_table"],
            derives_pairs=tuple(tuple(p) for p in draft["derives_pairs"]),
            plan_envelope=_stale_envelope(), origin="governed_planner",
            path_authority="governed_cross_catalog")

    original = contract_routes.recorded_gate1_draft_choice

    def _governed_recorded(*args, **kwargs):
        recorded = original(*args, **kwargs)
        return replace(recorded, feature=_governed_chosen()) if recorded is not None else None

    monkeypatch.setattr(
        "featuregen.api.routes.contract.recorded_gate1_draft_choice",
        _governed_recorded,
    )
    monkeypatch.setattr("featuregen.api.routes.contract.recheck_plan_freshness",
                        lambda *a, **k: ReplayFreshness.drifted)
    res = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert res.status_code == 409, res.text
