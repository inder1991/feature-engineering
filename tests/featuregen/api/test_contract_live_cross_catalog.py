"""Phase-3C.2a Task 5 — POST /contract/considered-set live cross-catalog readiness gate + is_live wiring
— and Task 7: the spec §9 acceptance suite over the full HTTP surface.

Task 5: flag-on-but-not-activation-approved → HTTP 503 BEFORE any LLM/planner dispatch and before any
run/scope is minted (fail-closed, never a legacy fallback). Flag-off / flag-on-approved → the route
threads the resolved ``is_live`` boolean into ``build_considered_set``.

WHAT THE E4 CUTOVER (2026-08-14) DID TO THIS FILE
-------------------------------------------------
Two of this suite's premises are gone, and pretending otherwise would be the only real failure here.

1. **An entity-only request no longer reaches the scoped route's builder at all.** With a
   ``confirmed_scope`` and no ``catalog_source`` the route now answers a typed HTTP 422
   ``SEMANTIC_REQUIRES_CATALOG_SOURCE``: the semantic engine plans over ONE frozen catalog context, and
   an honest refusal beats the silently empty page the deleted free-form generator used to fill. Every
   test here that posted an entity-scoped body now asserts either that refusal or — where the property
   is about the LENS rather than the route — calls ``build_considered_set`` directly with
   ``is_live=True, catalog_source=None, target_entity=…``, which is unchanged. The fail-closed 503
   still fires FIRST (readiness is checked before the refusal), so the activation interlock's
   guarantees are untouched.
2. **There is no ungoverned cross-catalog candidate left to reject.** §9 items 1, 4 and 7 all drove a
   ``cross_llm`` idea out of ``recommend_feature_sets_report`` — the free-form physical-column
   generator, now deleted, not disabled. The invariant those tests guarded (in an enabled deployment
   every customer-visible cross-catalog feature has a governed physical plan) now holds by
   CONSTRUCTION: an entity-scoped run has no ungoverned source, so nothing can arrive without a
   governed plan behind it. Those three tests are deleted rather than rewritten around a candidate the
   system can no longer produce; the guarantee is asserted where it now lives —
   ``test_gate1_governed_lens`` (the builder proposes nothing ungoverned, and the anchor is honestly
   absent), ``test_the_free_form_generator_never_runs_under_semantic_v1``
   (test_semantic_v1_serving), and the draft/confirm refusals in ``test_draft_rebinding`` /
   ``test_contract_h1c_cross_catalog_gate`` for a cross-catalog candidate that arrives by any other
   route.

The §9 acceptance map, as it stands after the cutover:

1. Flag off → the pre-3C.2a considered-set response shape: ``test_all_off_no_scope_is_byte_identical_
   to_pre_1b`` (test_1b_rollout). The cross-catalog DRAFT refusal: ``test_cross_catalog_without_
   envelope_is_rejected_at_draft`` (test_draft_rebinding).
2. Flag on + approved → governed options carry ``path_authority`` + a plan envelope:
   ``test_build_considered_set_surfaces_governed_option_when_live`` /
   ``test_helper_surfaces_resolved_governed_plan_as_option`` (test_gate1_governed_lens) +
   ``test_full_flag_on_cross_catalog_flow_never_invokes_permissive_path``
   (test_no_permissive_path_when_live).
3. Unresolved governed recipes → structured rejections: ``test_s9_item3_unresolved_governed_recipe_
   is_a_structured_rejection`` (this file, now at the builder) + ``test_helper_unresolved_governed_
   plan_becomes_a_rejection`` (test_gate1_governed_lens).
4. Cross-catalog LLM candidates cannot reach drafting — closed by construction (see above).
5. The draft path exactly matches the persisted governed plan's ``ordered_path``:
   ``test_full_flag_on_cross_catalog_flow_never_invokes_permissive_path``
   (test_no_permissive_path_when_live) + ``test_governed_feature_drafts_from_envelope_ordered_path``
   (test_draft_rebinding).
6. Drift → regeneration (409), never fallback:
   ``test_drifted_governed_plan_fails_closed_409_without_permissive_fallback``
   (test_no_permissive_path_when_live) + ``test_governed_feature_with_drifted_plan_raises_stale``
   (test_draft_rebinding) + the route mappings in test_contract.
7. Missing/tampered plan identity fails closed: ``test_cross_catalog_without_envelope_is_rejected_at_
   draft`` (test_draft_rebinding), ``test_draft_route_maps_cross_catalog_without_envelope_to_422``
   (test_contract) and the whole confirm-side suite in test_contract_h1c_cross_catalog_gate.
8. ``find_cross_catalog_path`` never invoked while live: tests/featuregen/overlay/upload/contract/
   test_no_permissive_path_when_live.py.
9. Activation prerequisite (no signing): ``test_flag_on_not_approved_returns_503_before_dispatch`` +
   ``test_s9_item9_wrong_deployment_id_does_not_inherit_approval`` (this file) +
   ``test_persist_evaluation_and_approve_enables`` (test_gate_routes) + test_live_activation.py.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH
from tests.featuregen.api.test_binding_confirmation import governed_ready_round, seal_for_real
from tests.featuregen.api.test_contract_scoped import CHURN, HYPOTHESIS, TARGET, _bank_multi
from tests.featuregen.api.test_e2e_walkthrough import _cib
from tests.featuregen.api.test_e2e_walkthrough import _fake as _cib_llm
from tests.featuregen.overlay.upload.planner.test_plan import _split, _txn_template

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.contract.gate1 import ConsideredSet, DraftChoice
from featuregen.overlay.upload.contract.live_activation import record_decision, record_evaluation
from featuregen.overlay.upload.feature_assist import FeatureIdea

_NOW = datetime(2026, 7, 18, tzinfo=UTC)
FLAG = "FEATUREGEN_INTENT_LIVE_CROSS_CATALOG"
DEP = "FEATUREGEN_DEPLOYMENT_ID"

#: The typed refusal the scoped route now returns for an entity-only (cross-catalog) scope.
SEMANTIC_REQUIRES_CATALOG_SOURCE = "SEMANTIC_REQUIRES_CATALOG_SOURCE"


def _approve(conn) -> None:
    """Record a PASS evaluation + an APPROVE decision for the current deployment (d1)."""
    eid = record_evaluation(conn, telemetry_window={}, population_report={}, gold_set_result={},
                            stability_result={}, result="PASS", evaluated_at=_NOW)
    record_decision(conn, evaluation_id=eid, decision="APPROVE", decided_by="admin", reason="go",
                    decided_at=_NOW)


def _flow_llm() -> FakeLLM:
    """Every LLM task these flows can still dispatch. The free-form generator entry
    (``overlay.feature.recommend``) is deliberately absent: it was deleted in the E4 cutover, so
    scripting it would describe a call that can no longer happen."""
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "advisory"}),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "governed cross-catalog transaction roll-up at account grain"}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def _entity_scoped_body() -> dict:
    """An ENTITY-scoped run: catalog_source OMITTED + a confirmed target_entity. This is the shape the
    scoped route now refuses typed — the semantic engine has no single frozen catalog to plan over."""
    return {"hypothesis": HYPOTHESIS, "objective": "predict churn", "target_ref": TARGET,
            "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                                "target_entity": "customer"}}


def _catalog_scoped_body() -> dict:
    """The same run with the catalog the cutover requires named — the servable shape, and the one the
    is_live wiring tests below use to observe what the route threads into the builder."""
    return {"hypothesis": HYPOTHESIS, "objective": "predict churn", "target_ref": TARGET,
            "catalog_source": "bank",
            "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                                "target_entity": "customer"}}


def _capture_builder(monkeypatch) -> dict:
    """Replace the builder with a recorder so the test observes exactly what the ROUTE resolved."""
    captured: dict = {}

    def _capture(_conn, intent, _client, **kwargs):
        captured.update(kwargs)
        return ConsideredSet(intent.intent_id, None, [], None, [])

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _capture)
    monkeypatch.setattr("featuregen.api.routes.contract.run_shadow_planner", lambda *a, **k: ())
    return captured


def _fresh_now(conn, *sources) -> None:
    """Watermark ``sources`` fresh AS OF THE TEST RUN (the routes ground at the real wall clock, so the
    planner-fixture seeds' hardcoded past date would read as stale) + the applied overlay projection
    checkpoint the compiler's CatalogStateStamp pins (mirrors test_plan._freshness at now)."""
    now = datetime.now(UTC)
    event_head = conn.execute("SELECT COALESCE(max(global_seq), 0) FROM events").fetchone()[0]
    applied_head = max(1, event_head)
    for src in sources:
        conn.execute(
            "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id,"
            " head_seq) VALUES (%s,%s,'t7',%s) ON CONFLICT (catalog_source) DO UPDATE SET"
            " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq",
            (src, now, applied_head))
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq) VALUES ('overlay', %s)"
        " ON CONFLICT (projection_name) DO UPDATE SET checkpoint_seq = EXCLUDED.checkpoint_seq",
        (applied_head,))


# ── fail-closed: flag on but NOT activation-approved → 503 before dispatch, nothing minted ─────────────
def test_flag_on_not_approved_returns_503_before_dispatch(make_client, conn, monkeypatch):
    """The interlock still fires FIRST: readiness is checked before the entity-only refusal, so an
    unapproved deployment is told 503 (fail closed) rather than 422 (name a catalog) — the stronger,
    more accurate answer stays the one the caller gets."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")   # a configured deployment, but NO approval decision recorded
    _bank_multi(conn)

    def _must_not_dispatch(*a, **k):
        raise AssertionError("no LLM/planner dispatch may happen when not activation-approved")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _must_not_dispatch)
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 503, res.text
    # fail-closed BEFORE any run/scope is minted or persisted
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── the cutover's own refusal: an entity-only scope is not servable, whatever the activation state ────
def test_entity_only_scope_is_refused_typed_whatever_the_activation_state(
        make_client, conn, monkeypatch):
    """E4: the scoped route plans over ONE frozen catalog context, so an entity-only (cross-catalog)
    request is refused with a typed 422 — and the refusal is UNCONDITIONAL. Being live-activation
    APPROVED does not buy a cross-catalog page here any more; approval governs whether a governed
    cross-catalog PLAN may be authored, not whether the engine can plan without a catalog. The lens
    itself is still reachable through ``build_considered_set`` and the legacy unscoped route."""
    _bank_multi(conn)
    client = make_client(_flow_llm())

    monkeypatch.delenv(FLAG, raising=False)
    off = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert off.status_code == 422, off.text
    assert off.json()["detail"]["code"] == SEMANTIC_REQUIRES_CATALOG_SOURCE
    assert "catalog_source" in off.json()["detail"]["message"]

    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)                                   # the FULL interlock holds …
    on = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert on.status_code == 422, on.text            # … and the answer is the same refusal
    assert on.json()["detail"]["code"] == SEMANTIC_REQUIRES_CATALOG_SOURCE
    # Nothing was generated on either attempt: the refusal precedes the builder entirely.
    assert conn.execute("SELECT count(*) FROM contract_considered").fetchone()[0] == 0


# ── flag on + approved → 200 and is_live=True + the confirmed target_entity thread into the builder ───
def test_flag_on_approved_threads_is_live_true(make_client, conn, monkeypatch):
    """The route (never the builder) resolves the live interlock and hands the builder the boolean.
    Driven over a CATALOG-scoped body since the cutover: the entity-only body this used to send is
    refused before the builder is reached (see the test above), but the resolution + threading it
    proves is route behaviour and is unchanged for every servable request."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)
    _bank_multi(conn)
    captured = _capture_builder(monkeypatch)

    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_catalog_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    assert captured["is_live"] is True
    assert captured["target_entity"] == "customer"
    assert captured["catalog_source"] == "bank"


# ── flag off → no readiness query, is_live=False threaded, response unchanged ──────────────────────────
def test_flag_off_threads_is_live_false(make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)
    _bank_multi(conn)
    captured = _capture_builder(monkeypatch)

    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_catalog_scoped_body(), headers=AUTH)
    assert res.status_code == 200, res.text
    assert captured["is_live"] is False


# ── §9 item 3: an unresolved governed recipe surfaces as a STRUCTURED rejection ───────────────────────
def test_s9_item3_unresolved_governed_recipe_is_a_structured_rejection(conn):
    """§9 item 3 — flag-on-approved, entity-scoped, but the cross-catalog roll-up CANNOT complete (ops +
    rev with NO verified bridge): the governed recipe appears as a structured rejection carrying its
    recipe_id + primary reason code — never as an option, never a permissive fallback.

    Driven at ``build_considered_set`` since the E4 cutover: the entity-scoped HTTP request this used to
    make is now refused 422 before the builder runs, but the governed cross-catalog LENS itself is
    unchanged and still reachable this way (and through the legacy unscoped route). The lens is the
    subject; the route was only ever the delivery mechanism, and its refusal is asserted above."""
    from featuregen.overlay.upload.contract.gate1 import build_considered_set
    from featuregen.overlay.upload.contract.intake import submit_intent

    _split(conn)                      # ops + rev, NO bridge → the account roll-up cannot complete
    _fresh_now(conn, "ops", "rev")
    intent = submit_intent(hypothesis=HYPOTHESIS, actor="tester")
    cs = build_considered_set(
        conn, intent, _flow_llm(), catalog_source=None, is_live=True, target_entity="account",
        templates=(_txn_template(),), applicability=None, now=datetime.now(UTC))

    assert not any(f.name == "t_roll" for s in cs.alternatives for f in s.features)
    rej = [r for r in cs.rejections
           if r.get("lens") == "governed" and r.get("recipe_id") == "t_roll"]
    assert len(rej) == 1
    assert isinstance(rej[0]["reason"], str) and rej[0]["reason"]   # a structured primary reason code


# ── §9 item 9: a wrong deployment_id does not inherit another deployment's approval ───────────────────
def test_s9_item9_wrong_deployment_id_does_not_inherit_approval(make_client, conn, monkeypatch):
    """§9 item 9 — an approval recorded for deployment d1 does NOT enable a deployment presenting
    d2 (copied env / shared DB): readiness fails closed 503 BEFORE any dispatch, nothing minted."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")
    _approve(conn)                    # PASS + APPROVE recorded under d1
    monkeypatch.setenv(DEP, "d2")     # …but this deployment is d2
    _bank_multi(conn)
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_entity_scoped_body(), headers=AUTH)
    assert res.status_code == 503, res.text
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ═══════════ whole-branch review fixes — the two composition findings the isolated tests missed ═══════════
# FINDING 1: the NON-scoped considered-set path (no confirmed_scope) ALSO enforces the live readiness gate
# — otherwise a flag-on-approved caller POSTing an entity-scoped run with no confirmed_scope could reach
# generation without the interlock. (Its other half — that no UNGOVERNED cross-catalog option survives on
# that path — is now true by construction: the free-form generator that produced them was deleted in the
# E4 cutover, so the filter has nothing left to remove. See test_gate1_governed_lens.)
def _non_scoped_body() -> dict:
    """An ENTITY-scoped run with NO confirmed_scope (catalog_source omitted) — the non-scoped route path."""
    return {"hypothesis": HYPOTHESIS, "objective": "predict churn", "entity": "Customer"}


def test_non_scoped_flag_on_not_approved_returns_503_before_dispatch(make_client, conn, monkeypatch):
    """FINDING 1 — a flag-on-but-NOT-approved NON-scoped entity run fails closed 503 BEFORE any builder
    dispatch (the non-scoped path mirrors the scoped readiness interlock)."""
    monkeypatch.setenv(FLAG, "1")
    monkeypatch.setenv(DEP, "d1")   # configured deployment, but NO approval decision recorded

    def _must_not_dispatch(*a, **k):
        raise AssertionError("no LLM/planner dispatch may happen when not activation-approved")

    monkeypatch.setattr("featuregen.api.routes.contract.build_considered_set", _must_not_dispatch)
    client = make_client(_flow_llm())
    res = client.post("/contract/considered-set", json=_non_scoped_body(), headers=AUTH)
    assert res.status_code == 503, res.text


# FINDING 2: at the GOVERNING write a governed contract's persisted join_path must be RE-DERIVED from the
# SERVER envelope's ordered_path — never the client body (the confirm match-check validates name/derives/
# aggregation but NOT join_path, so a client could otherwise replay a governed feature with a FABRICATED
# bridge that the freshness recheck still passes, defeating "a governed draft path equals the plan's,
# byte-for-byte").
def _fresh_envelope():
    from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1
    return PlanEnvelopeV1(
        recipe_id="r", physical_plan_id="bp_1", generation_run_id="run", catalog_sources=("cib",),
        ordered_path=("cib:direct_catalog:",), contract_id="c1",
        contract_resolution_status="resolved", contract_reason_codes=(),
        catalog_fingerprint={"cib": "fp"}, compiler_version={"plan_contract": "1.0.0"},
        input_stamps=({"catalog_source": "cib", "compiler_input_fingerprint": "fp",
                       "head_seq": 1, "projection_checkpoint": 1},))


def test_confirm_persists_server_envelope_join_path_not_client_forged(
        make_client, conn, monkeypatch):
    """FINDING 2 — a governed confirm whose client body carries a FABRICATED join_path (matching
    name/derives_pairs/aggregation, plan fresh) persists the SERVER envelope's ordered_path-derived path,
    NEVER the client's forged value: the join_path can no longer smuggle an ungoverned bridge.

    The draft it replays is now the engine's own governed-ready candidate (E4: there is no free-form
    anchor to draft), obtained through the same real surfaces the E0 walkthrough uses."""
    from featuregen.overlay.upload.contract.author import _envelope_join_path
    from featuregen.overlay.upload.planner.contracts import ReplayFreshness

    seal_for_real(monkeypatch)
    _cib(conn)
    client = make_client(_cib_llm())
    body, card = governed_ready_round(client)
    dr = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"], "why": ""}, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dict(dr.json()["draft"])
    draft["intent_id"] = body["intent_id"]

    env = _fresh_envelope()

    def _governed_chosen(*a, **k):
        # the server-reconstructed chosen feature is GOVERNED (carries a fresh plan envelope), matching the
        # draft's name/derives_pairs/aggregation so the confirm match-check passes.
        return FeatureIdea(
            name=draft["feature_name"], description="", derives_from=draft["derives_from"],
            aggregation=draft["aggregation"], grain_table=draft["grain_table"],
            derives_pairs=tuple(tuple(p) for p in draft["derives_pairs"]),
            plan_envelope=env, origin="governed_planner", path_authority="governed_cross_catalog")

    monkeypatch.setattr(
        "featuregen.api.routes.contract.recorded_gate1_draft_choice",
        lambda *args, **kwargs: DraftChoice(_governed_chosen(), None, None, None),
    )
    monkeypatch.setattr("featuregen.api.routes.contract.recheck_plan_freshness",
                        lambda *a, **k: ReplayFreshness.current)
    # This test's SUBJECT is the route's join_path server-derivation (routes/contract.py, BEFORE
    # confirm_contract), not the confirm-time plan rebuild — its ``_fresh_envelope`` is a synthetic single-
    # catalog envelope (no target_entity, recipe not in the registry) that is intentionally not rebuildable.
    # Stub the rebuild so the confirm reaches the persist step (H3 I-2 otherwise fail-closes a not-rebuildable
    # plan). ``(None, None)`` = no read-set lineage to persist, exactly as the pre-H3 skip did here.
    monkeypatch.setattr("featuregen.overlay.upload.contract.govern.revalidate_governed_plan",
                        lambda *a, **k: (None, None))
    # the client forges a join_path that does NOT match the server envelope's ordered_path
    forged = [{"kind": "governed_segment", "segment": "FORGED:evil:bridge",
               "catalog_source": "FORGED", "segment_kind": "evil", "ref": "bridge"}]
    draft["join_path"] = forged
    cr = client.post("/contract/confirm", json=draft, headers=AUTH)
    assert cr.status_code == 200, cr.text
    contract_id = cr.json()["contract_id"]
    persisted = conn.execute("SELECT join_path FROM contract WHERE contract_id = %s",
                             (contract_id,)).fetchone()[0]
    assert persisted == list(_envelope_join_path(env.ordered_path))   # the server envelope's path
    assert persisted != forged                                         # never the client's forgery
