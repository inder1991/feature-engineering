"""Phase-3C.2a Task 5 — POST /contract/considered-set live cross-catalog readiness gate + is_live wiring
— and Task 7: the spec §9 acceptance suite over the full HTTP surface.

Task 5: flag-on-but-not-activation-approved → HTTP 503 BEFORE any LLM/planner dispatch and before any
run/scope is minted (fail-closed, never a legacy fallback). Flag-off / flag-on-approved → the route
threads the resolved ``is_live`` boolean into ``build_considered_set``; flag-off runs no readiness query.

Task 7 — the §9 acceptance map (one entry per spec item; existing Task-3/5/6 tests are REFERENCED, not
duplicated; the new tests here add only the HTTP-surface coverage that was missing):

1. Flag off → existing response + permissive draft path, byte-identical:
   NEW ``test_s9_item1_flag_off_draft_rides_permissive_cross_catalog_path`` (HTTP 200 end to end) +
   ``test_flag_off_threads_is_live_false`` (this file), ``test_flag_off_skips_the_governed_branch_
   entirely`` (test_gate1_governed_lens), ``test_cross_catalog_without_envelope_draws_permissive_path_
   when_not_live`` / ``test_single_catalog_feature_drafts_as_before`` (test_draft_rebinding), and the
   headline check: the FULL suite passes unchanged (behaviour-neutral).
2. Flag on + approved → governed options surface with ``path_authority`` + a plan envelope:
   NEW ``test_full_flag_on_cross_catalog_flow_never_invokes_permissive_path``
   (test_no_permissive_path_when_live — asserts the HTTP response JSON) + builder-level
   ``test_build_considered_set_surfaces_governed_option_when_live`` /
   ``test_helper_surfaces_resolved_governed_plan_as_option`` (test_gate1_governed_lens).
3. Unresolved governed recipes → structured rejections:
   NEW ``test_s9_item3_unresolved_governed_recipe_is_a_structured_rejection`` (HTTP) + builder-level
   ``test_helper_unresolved_governed_plan_becomes_a_rejection`` (test_gate1_governed_lens).
4. Cross-catalog LLM candidates cannot reach drafting:
   NEW ``test_s9_item4_cross_catalog_llm_candidate_cannot_reach_drafting`` (HTTP: rejected at the
   considered-set boundary AND un-draftable) + builder-level ``test_reject_cross_catalog_llm_removes_
   multi_catalog_and_keeps_single`` / ``test_build_considered_set_filters_cross_catalog_llm_when_live``
   + the anchor-drop tests (test_gate1_governed_lens).
5. The draft path exactly matches the persisted governed plan's ``ordered_path``:
   NEW ``test_full_flag_on_cross_catalog_flow_never_invokes_permissive_path``
   (test_no_permissive_path_when_live — over HTTP against the PERSISTED envelope) + author-level
   ``test_governed_feature_drafts_from_envelope_ordered_path`` (test_draft_rebinding).
6. Drift → regeneration (409), never fallback:
   NEW ``test_drifted_governed_plan_fails_closed_409_without_permissive_fallback``
   (test_no_permissive_path_when_live — real drift over HTTP, the permissive fn provably not invoked)
   + ``test_governed_feature_with_drifted_plan_raises_stale`` (test_draft_rebinding) +
   ``test_draft_route_maps_stale_plan_to_409`` / ``test_confirm_route_rechecks_freshness_and_maps_
   stale_to_409`` (test_contract).
7. Missing/tampered plan identity fails closed:
   NEW ``test_s9_item7_cross_catalog_option_without_envelope_cannot_confirm_once_live`` (HTTP confirm)
   + ``test_cross_catalog_without_envelope_is_rejected_at_draft_when_live`` (test_draft_rebinding) +
   ``test_draft_route_maps_cross_catalog_without_envelope_to_422`` (test_contract).
8. ``find_cross_catalog_path`` never invoked while live:
   NEW tests/featuregen/overlay/upload/contract/test_no_permissive_path_when_live.py (the structural
   raises-guarantee over the full considered-set → draft → confirm flow).
9. Activation prerequisite (no signing):
   ``test_flag_on_not_approved_returns_503_before_dispatch`` (this file, 503 before dispatch) +
   ``test_persist_evaluation_and_approve_enables`` (test_gate_routes, APPROVE-over-FAIL → 422) +
   test_live_activation.py (revoke / version-vector mismatch / unset deployment) +
   NEW ``test_s9_item9_wrong_deployment_id_does_not_inherit_approval`` (HTTP: a copied flag+DB under
   another deployment_id still fails closed 503).
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_contract_scoped import (
    CHURN,
    HYPOTHESIS,
    TARGET,
    _bank_multi,
    _fake,
)
from tests.featuregen.overlay.upload.planner.test_plan import _split, _txn_template

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    GOVERNED_CROSS_CATALOG_PLAN_REQUIRED,
    ConsideredSet,
)
from featuregen.overlay.upload.contract.live_activation import record_decision, record_evaluation
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, SetsReport
from featuregen.overlay.upload.graph import build_graph

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
FLAG = "FEATUREGEN_INTENT_LIVE_CROSS_CATALOG"
DEP = "FEATUREGEN_DEPLOYMENT_ID"


def _approve(conn) -> None:
    """Record a PASS evaluation + an APPROVE decision for the current deployment (d1)."""
    eid = record_evaluation(conn, telemetry_window={}, population_report={}, gold_set_result={},
                            stability_result={}, result="PASS", evaluated_at=_NOW)
    record_decision(conn, evaluation_id=eid, decision="APPROVE", decided_by="admin", reason="go",
                    decided_at=_NOW)


def _entity_scoped_body() -> dict:
    """An ENTITY-scoped run: catalog_source OMITTED + a confirmed target_entity → the live branch fires."""
    return {"hypothesis": HYPOTHESIS, "objective": "predict churn", "target_ref": TARGET,
            "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                                "target_entity": "customer"}}


# ── fail-closed: flag on but NOT activation-approved → 503 before dispatch, nothing minted ─────────────
def test_flag_on_not_approved_returns_503_before_dispatch(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")   # a configured deployment, but NO approval decision recorded
    _bank_multi(conn)

    def _must_not_dispatch(*a, **k):
        raise AssertionError("no LLM/planner dispatch may happen when not activation-approved")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _must_not_dispatch)
    client = make_client(_fake())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 503, res.text
    # fail-closed BEFORE any run/scope is minted or persisted
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── flag on + approved → 200 and is_live=True + the confirmed target_entity thread into the builder ───
def test_flag_on_approved_threads_is_live_true(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    _bank_multi(conn)
    captured: dict = {}

    def _capture(_conn, intent, _client, **kwargs):
        captured["is_live"] = kwargs.get("is_live")
        captured["target_entity"] = kwargs.get("target_entity")
        return ConsideredSet(intent.intent_id, None, [], None, [])

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _capture)
    monkeypatch.setattr("featuregen.api.routes.contract.run_shadow_planner", lambda *a, **k: ())
    client = make_client(_fake())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    assert captured["is_live"] is True
    assert captured["target_entity"] == "customer"


# ── flag off → no readiness query, is_live=False threaded, response unchanged ──────────────────────────
def test_flag_off_threads_is_live_false(make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    _bank_multi(conn)
    captured: dict = {}

    def _capture(_conn, intent, _client, **kwargs):
        captured["is_live"] = kwargs.get("is_live")
        return ConsideredSet(intent.intent_id, None, [], None, [])

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _capture)
    monkeypatch.setattr("featuregen.api.routes.contract.run_shadow_planner", lambda *a, **k: ())
    client = make_client(_fake())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    assert captured["is_live"] is False


# ═════════════════════════ Task 7 — the §9 acceptance suite (shared harness) ═════════════════════════
def _flow_llm() -> FakeLLM:
    """Every LLM task the full considered-set → draft → confirm flow can dispatch. The generation
    lens returns NO features (the governed planner / stubbed report is the source under test)."""
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": []}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "advisory"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "governed cross-catalog transaction roll-up at account grain"}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def _fresh_now(conn, *sources) -> None:
    """Watermark ``sources`` fresh AS OF THE TEST RUN (the routes ground at the real wall clock, so the
    planner-fixture seeds' hardcoded past date would read as stale) + the applied overlay projection
    checkpoint the compiler's CatalogStateStamp pins (mirrors test_plan._freshness at now)."""
    now = datetime.now(UTC)
    for src in sources:
        conn.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id,"
            " head_seq) VALUES (%s,%s,'t7',1) ON CONFLICT (catalog_source) DO UPDATE SET"
            " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
            (src, now))
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq) VALUES ('overlay', 1)"
        " ON CONFLICT (projection_name) DO UPDATE SET checkpoint_seq = EXCLUDED.checkpoint_seq")


def _governed_scoped_body() -> dict:
    """An ENTITY-scoped run (catalog_source OMITTED) whose confirmed ``target_entity`` is the ACCOUNT
    grain the planner-fixture recipe (t_roll over _cross_seed) plans toward."""
    return {"hypothesis": HYPOTHESIS, "objective": "predict churn",
            "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                                "target_entity": "account"}}


def _inject_fixture_template(monkeypatch) -> None:
    """Route the REAL ``build_considered_set`` through its dedicated test injection point: the governed
    lens plans over the production recipe registry, but the planner-fixture catalog (_cross_seed)
    grounds none of those recipes — so inject the fixture template (t_roll) as the registry and drop
    the route's applicability so the lens's eligible set falls back to the injected registry. The
    builder, governed lens, planner, envelope + persistence wiring all stay REAL."""
    from featuregen.overlay.upload.contract.gate1 import build_considered_set as _real

    def _wrapped(conn, intent, client, **kwargs):
        kwargs["templates"] = (_txn_template(),)
        kwargs["applicability"] = None
        return _real(conn, intent, client, **kwargs)

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _wrapped)


def _cross_catalog_llm_seed(conn) -> None:
    """Two catalogs bridged ONLY by the shared Customer entity (mirrors test_draft_rebinding's
    ``_ungoverned_cross_feature``) + fresh watermarks — the shape whose flag-off draft authors the
    permissive entity-bridged ``find_cross_catalog_path`` path."""
    build_graph(conn, "deposits", [
        CanonicalRow("deposits", "accounts", "cust_ref", "integer", entity="Customer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric")])
    build_graph(conn, "cards", [
        CanonicalRow("cards", "card_accounts", "cust_id", "integer", entity="Customer"),
        CanonicalRow("cards", "card_accounts", "spend", "numeric")])
    _fresh_now(conn, "deposits", "cards")


def _cross_llm_idea() -> FeatureIdea:
    """A gauntlet-shaped LLM idea whose derives span deposits + cards (no governed plan envelope)."""
    return FeatureIdea("cross_llm", "", ["public.accounts.balance", "public.card_accounts.spend"],
                       "avg", "accounts",
                       derives_pairs=(("deposits", "public.accounts.balance"),
                                      ("cards", "public.card_accounts.spend")))


def _stub_report(monkeypatch, *ideas: FeatureIdea) -> None:
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
        lambda *a, **k: SetsReport(sets=[FeatureSet("monetary", list(ideas))], rejections=[]))


# ── §9 item 1: flag OFF → today's response + the permissive draft path, HTTP 200 ──────────────────────
def test_s9_item1_flag_off_draft_rides_permissive_cross_catalog_path(make_client, conn, monkeypatch):
    """§9 item 1 — flag OFF: the considered-set response carries EXACTLY today's keys (no 3C.2a
    additions) and a cross-catalog choice drafts over HTTP via the permissive entity-bridged
    ``find_cross_catalog_path`` path (HTTP 200) — byte-identical to pre-3C.2a."""
    monkeypatch.delenv(FLAG, raising=False)
    _cross_catalog_llm_seed(conn)
    _stub_report(monkeypatch, _cross_llm_idea())
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set",
                      json={"hypothesis": HYPOTHESIS, "objective": "predict churn"}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    # exactly the pre-3C.2a top-level shape — no readiness/ranking/scope keys appear flag-off
    assert set(body) == {"intent_id", "anchor", "alternatives", "recommendation", "rejections"}
    assert any(f["name"] == "cross_llm" for s in body["alternatives"] for f in s["features"])
    assert not any(r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in body["rejections"])
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "cross_llm", "why": "flag-off permissive"}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    join_path = dr.json()["draft"]["join_path"]
    # the permissive entity-bridged shape: accounts --entity(Customer)--> card_accounts
    assert any(step.get("kind") == "entity" and step.get("via") == "Customer" for step in join_path)


# ── §9 item 3: an unresolved governed recipe surfaces as a STRUCTURED rejection over HTTP ─────────────
def test_s9_item3_unresolved_governed_recipe_is_a_structured_rejection(make_client, conn, monkeypatch):
    """§9 item 3 — flag-on-approved, entity-scoped, but the cross-catalog roll-up CANNOT complete (ops +
    rev with NO verified bridge): the governed recipe appears as a structured rejection carrying its
    recipe_id + primary reason code — never as an option, never a permissive fallback."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    _split(conn)                      # ops + rev, NO bridge → the account roll-up cannot complete
    _fresh_now(conn, "ops", "rev")
    _inject_fixture_template(monkeypatch)
    _stub_report(monkeypatch)         # no LLM noise — the governed lens is the source under test
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_governed_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert not any(f["name"] == "t_roll" for s in body["alternatives"] for f in s["features"])
    rej = [r for r in body["rejections"]
           if r.get("lens") == "governed" and r.get("recipe_id") == "t_roll"]
    assert len(rej) == 1
    assert isinstance(rej[0]["reason"], str) and rej[0]["reason"]   # a structured primary reason code


# ── §9 item 4: a cross-catalog LLM candidate cannot reach drafting ────────────────────────────────────
def test_s9_item4_cross_catalog_llm_candidate_cannot_reach_drafting(make_client, conn, monkeypatch):
    """§9 item 4 — flag-on-approved: a cross-catalog LLM candidate is rejected at the considered-set
    boundary with ``GOVERNED_CROSS_CATALOG_PLAN_REQUIRED`` (single-catalog siblings untouched) and is
    therefore NOT draftable — /contract/draft refuses it as not-in-the-recorded-set (422)."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    single = FeatureIdea("single_llm", "", ["public.t.a"], "sum", None,
                         derives_pairs=(("ops", "public.t.a"),))
    _stub_report(monkeypatch, _cross_llm_idea(), single)
    client = make_client(_flow_llm())
    # confirmed scope WITHOUT a target_entity: the boundary FILTER is the subject (no governed lens run)
    body = {"hypothesis": HYPOTHESIS, "objective": "predict churn",
            "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed"}}
    res = client.post("/contract/considered-set", json=body, headers=AUTH)
    assert res.status_code == 200, res.text
    out = res.json()
    names = {f["name"] for s in out["alternatives"] for f in s["features"]}
    assert "single_llm" in names and "cross_llm" not in names
    assert any(r.get("name") == "cross_llm"
               and r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in out["rejections"])
    dr = client.post("/contract/draft", json={
        "intent_id": out["intent_id"], "chosen_source": "alternative",
        "chosen_option_id": "cross_llm", "why": ""}, headers=AUTH)
    assert dr.status_code == 422, dr.text   # never offered → never draftable


# ── §9 item 7: a cross-catalog option with NO valid envelope fails closed at the governing write ──────
def test_s9_item7_cross_catalog_option_without_envelope_cannot_confirm_once_live(
        make_client, conn, monkeypatch):
    """§9 item 7 — missing plan identity fails closed at CONFIRM: a cross-catalog option drafted
    flag-OFF (permissive, no envelope) cannot be confirmed once the deployment flips live — the
    governing write rejects it (422, regenerate under the governed planner), never a permissive
    confirmation riding a pre-flip draft."""
    monkeypatch.delenv(FLAG, raising=False)
    _cross_catalog_llm_seed(conn)
    _stub_report(monkeypatch, _cross_llm_idea())
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set",
                      json={"hypothesis": HYPOTHESIS, "objective": "predict churn"}, headers=AUTH)
    assert res.status_code == 200, res.text
    intent_id = res.json()["intent_id"]
    dr = client.post("/contract/draft", json={
        "intent_id": intent_id, "chosen_source": "alternative",
        "chosen_option_id": "cross_llm", "why": ""}, headers=AUTH)
    assert dr.status_code == 200, dr.text            # flag-off: the permissive draft still works
    draft = dr.json()["draft"]
    draft["intent_id"] = intent_id
    # the flip lands BETWEEN draft and confirm: flag on + this deployment activation-approved
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 422, cr.text
    assert "governed plan envelope" in cr.json()["detail"]
    assert conn.execute("SELECT count(*) FROM contract").fetchone()[0] == 0   # nothing governed


# ── §9 item 9: a wrong deployment_id does not inherit another deployment's approval ───────────────────
def test_s9_item9_wrong_deployment_id_does_not_inherit_approval(make_client, conn, monkeypatch):
    """§9 item 9 — an approval recorded for deployment d1 does NOT enable a deployment presenting
    d2 (copied env / shared DB): readiness fails closed 503 BEFORE any dispatch, nothing minted."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)                    # PASS + APPROVE recorded under d1
    monkeypatch.setenv(DEP, "d2")     # …but this deployment is d2
    _bank_multi(conn)
    client = make_client(_fake())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 503, res.text
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0
