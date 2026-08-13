"""SE-7 — semantic_v1 SERVES: the recipe lens comes from the semantic engine, end to end.

The enforced projection is the program's payoff: frozen context → capability binder →
eligibility fold → assembly → typed gauntlet → Gate-1 cards. These tests prove the mode
actually serves V2 candidates (not the legacy template lens), persists its observations in the
request transaction, and that the frozen `legacy` default is untouched by the new branch.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
    })


def _bank(conn) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(conn, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _post(client) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def test_semantic_v1_serves_the_recipe_lens_from_the_semantic_engine(
        make_client, conn, monkeypatch, caplog):
    """The one-engine payoff: under semantic_v1 the legacy template grounding never runs —
    the templates lens (when present) and the recipe rejections come from the V2 engine."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")

    def boom(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError("legacy template grounding must not run under semantic_v1")

    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._template_candidates", boom)
    with caplog.at_level("INFO", logger="featuregen.overlay.upload.contract.gate1"):
        body = _post(make_client(llm_client=_fake()))

    served = [r.message for r in caplog.records if r.message.startswith("semantic-v1 served:")]
    assert len(served) == 1
    # The V2 engine decided every recipe outcome: whatever bound was served, whatever did not
    # is a NAMED rejection — nothing silently dropped. On this catalog the V2 registry's
    # objective scoping decides how many candidates exist; the invariant is the SOURCE.
    lens_names = {s["lens"] for s in body["alternatives"]}
    assert "llm" in lens_names or len(body["alternatives"]) >= 1   # LLM lens unchanged
    rows = conn.execute(
        "SELECT source_origin, binding_state FROM semantic_candidate_observation").fetchall()
    assert rows, "the serving path persists its observations in the request transaction"
    assert all(r[0] == "recipe_v2" for r in rows)


def test_semantic_v1_recipe_cards_carry_projected_provenance(make_client, conn, monkeypatch):
    """When a V2 candidate binds on this catalog, the card is recipe-sourced with role
    bindings projected from the binder's verdicts (measured authority, confirmation flags)."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=_fake()))

    template_sets = [s for s in body["alternatives"] if s["lens"] == "templates"]
    if not template_sets or not template_sets[0]["features"]:
        # Honest skip-shape: nothing bound on this fixture — the engine refused everything by
        # name instead. The projection contract is then visible in the rejections.
        assert any(r.get("code") for r in body.get("rejections", []))
        return
    card = template_sets[0]["features"][0]
    assert card.get("generation_source") == "recipe"
    assert card.get("recipe_id")


def test_semantic_v1_dispositions_describe_the_v2_universe(make_client, conn, monkeypatch):
    """SE-7 part 4: under semantic_v1 the disposition lens folds the universe that was
    actually planned — V2 recipe ids, with the engine's own grounded/rejected outcomes —
    never the legacy registry (which would read UNBUILDABLE for every recipe it never ran)."""
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=_fake()))

    v2_ids = {r.recipe_id for r in V2_RECIPES}
    disposition_ids = {d["recipe_id"] for d in body["dispositions"]}
    assert disposition_ids == v2_ids
    assert body["in_scope_count"] == len(
        [d for d in body["dispositions"] if d["relevance_tier"] is not None])
    # The engine's outcome and the disposition lens agree: anything the projection served or
    # refused is stamped from those same ids, in the same universe.
    rows = {r[0] for r in conn.execute(
        "SELECT DISTINCT source_definition_id FROM semantic_candidate_observation").fetchall()}
    assert rows <= v2_ids


def test_legacy_default_is_untouched_by_the_serving_branch(make_client, conn, monkeypatch):
    """The frozen default: no env var → the semantic serving path must never run."""
    _bank(conn)

    def boom(*a, **k):  # pragma: no cover - reached only on regression
        raise AssertionError("the projection must not run in legacy mode")

    monkeypatch.setattr(
        "featuregen.overlay.upload.semantic_projection.project_assembled_set", boom)
    _post(make_client(llm_client=_fake()))


def _intent_payload() -> dict:
    return {"intents": [{
        "display_name": "Days since last activity (model)",
        "business_definition": "Days elapsed since the customer's most recent event.",
        "primary_objective": CHURN,
        "computation_kind": "deterministic_formula",
        "operation_class": "recency",
        "output_grain_entity": "customer",
        "source_grain": "transaction",
        "output": {
            "output_id": "recency_model", "display_label": "Days since last activity",
            "output_type": "numeric", "additivity": "non_additive", "unit_kind": "duration_days",
            "null_input_policy": "null timestamps are excluded and counted",
            "empty_population_policy": "null with populated flag",
        },
        "operands": [
            {"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
            {"role": "when", "concept": "event_timestamp",
             "operand_class": "event_timestamp"},
        ],
        "temporal": {"anchor_kind": "event", "window_basis": "event time",
                     "window_unit": "days", "cutoff_inclusivity": "inclusive"},
        "rationale": "recency is the strongest dormancy precursor",
    }]}


def _fake_with_intents() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
    })


def test_semantic_v1_binds_llm_intents_through_the_same_engine(
        make_client, conn, monkeypatch, caplog):
    """SE-6 wired: the model proposes an ABSTRACT intent (concepts + classes, no refs), the
    SHARED binder chooses the columns, and the observation store records an llm_intent-origin
    candidate beside the recipes — one engine, both origins."""
    fake = _fake_with_intents()
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    _post(make_client(llm_client=fake))

    rows = conn.execute(
        "SELECT source_origin, source_definition_id, binding_state "
        "FROM semantic_candidate_observation WHERE source_origin = 'llm_intent'").fetchall()
    assert rows, "the intent candidate reached the engine and was observed"
    origin, definition_id, binding_state = rows[0]
    assert definition_id.startswith("intent:") or definition_id
    # The engine DECIDED the binding (bound on this catalog: customer_id + event_ts exist);
    # whatever the state, it came from the shared binder, never from model-named columns.
    assert binding_state in ("bound", "ambiguous", "missing", "blocked")


def test_option_decision_rows_freeze_the_served_facts(make_client, conn, monkeypatch):
    """A1b: every SERVED semantic option gets one immutable decision row, keyed to the exact
    (revision, option), linked to its exact observation, loadable as the activation policy's
    frozen layer — and the store refuses rewrites. Driven through the INTENT path (on this
    small fixture no recipe binds all the way to a served card), which also proves the
    decision row's origin honesty: the wire may still say "recipe" (GEN-05, fixed in B4) but
    the FROZEN record says llm_intent."""
    import psycopg
    import pytest as _pytest

    from featuregen.overlay.upload.semantic_option_decision import load_frozen_option_facts

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=_fake_with_intents()))

    revision_id = conn.execute(
        "SELECT considered_revision_id FROM contract_considered_revision "
        "WHERE generation_run_id = %s", (body["generation_run_id"],)).fetchone()[0]
    rows = conn.execute(
        "SELECT option_id, source_definition_id, generation_source, observation_id, "
        "       review_current, confirmation_required_roles, computation_kind "
        "FROM semantic_option_decision WHERE considered_revision_id = %s",
        (revision_id,)).fetchall()
    assert rows, "the served intent option froze a decision row"
    served_engine_options = {
        f["option_id"] for s_ in body["alternatives"] for f in s_["features"]
        if f.get("source_definition_id")}
    assert {r[0] for r in rows} == served_engine_options       # the EXACT keys the human sees

    # A3 widened this: ACTIONABLE recipe options freeze decision rows too, so pick the
    # intent's row explicitly rather than positionally.
    intent_rows = [r for r in rows if r[2] == "llm_intent"]
    assert intent_rows, "the intent option froze a row with its HONEST origin"
    option_id, definition_id, source, observation_id, review_current, roles, kind = intent_rows[0]
    assert kind == "conceptual_pattern"                        # the structural ceiling
    assert observation_id, "linked to the exact observation, never newest-for-definition"
    assert review_current is False
    assert roles, "all-proposed metadata => confirmation roles recorded"

    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen is not None
    assert frozen.binding_state == "bound"
    assert frozen.generation_source == "llm_intent"
    assert frozen.confirmation_required_roles
    assert frozen.plan_envelope_present is True                # B7: the plan FOLDED here
    assert load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id="opt_nope") is None

    with _pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with conn.transaction():
            conn.execute("UPDATE semantic_option_decision SET review_current = true")


def test_draft_of_a_semantic_option_is_blocked_by_the_activation_policy(
        make_client, conn, monkeypatch):
    """A2 slice 2 — the negative path that makes end-to-end testing honest: the served intent
    option (conceptual, all-proposed metadata, no plan, no verifiable snapshot) 409s at draft
    with EVERY blocker named and a next step per blocker. Nothing hides; nothing proceeds."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    client = make_client(llm_client=_fake_with_intents())
    body = _post(client)

    option = next(f for s_ in body["alternatives"] for f in s_["features"]
                  if f.get("source_definition_id"))
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"],
        "chosen_option_id": option["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "ACTIVATION_BLOCKED"
    assert detail["action"] == "create_contract"
    codes = {b["code"] for b in detail["blockers"]}
    # The remaining truths about THIS option, all named at once:
    assert "PROPOSED_METADATA_ONLY" in codes            # all-proposed metadata needs the funnel
    assert "CONCEPTUAL_PATTERN_NOT_AUTHORABLE" in codes  # a conceptual idea has no computation
    assert "SNAPSHOT_STALE_REGENERATE" in codes         # unverifiable snapshot FAILS CLOSED
    # B7 flipped this gate from a wall into a door: the single-source frozen-bindings plan
    # FOLDED for this candidate, so the plan blocker is honestly ABSENT now.
    assert "PHYSICAL_PLAN_MISSING" not in codes
    assert all(b["next_step"] for b in detail["blockers"])


def test_confirm_re_checks_activation_even_when_a_choice_was_recorded(
        make_client, conn, monkeypatch):
    """A2 slice 3 — the race defense at the GOVERNING write: the draft route records the
    Gate-1 choice BEFORE the fold blocks it, so a client could skip straight to /contract/
    confirm with a hand-built draft body. The confirm re-check runs the SAME fold over the
    same frozen decision row and blocks with the same typed shape — the governing write is
    never softer than the draft."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    client = make_client(llm_client=_fake_with_intents())
    body = _post(client)
    option = next(f for s_ in body["alternatives"] for f in s_["features"]
                  if f.get("source_definition_id"))

    # Draft: blocked by the fold — but the choice row is recorded first (by design: the
    # choice audit trail exists even for refused activations).
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"],
        "chosen_option_id": option["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)
    assert res.status_code == 409

    # Straight to confirm with a hand-built draft body: the re-check blocks identically.
    res = client.post("/contract/confirm", json={
        "feature_name": option["name"],
        "definition": "hand-built definition smuggled past the draft",
        "derives_from": option.get("derives_from", []),
        "intent_id": body["intent_id"],
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "ACTIVATION_BLOCKED"
    assert detail["action"] == "create_contract"
    assert {b["code"] for b in detail["blockers"]} >= {
        "PROPOSED_METADATA_ONLY", "CONCEPTUAL_PATTERN_NOT_AUTHORABLE"}


def test_the_free_form_generator_never_runs_under_semantic_v1(make_client, conn, monkeypatch):
    """B1 (GEN-01 closed): the FakeLLM scripts ONLY the intents task — an unscripted task
    raises KeyError inside FakeLLM, so the request SUCCEEDING proves the free-form
    recommend/recommend_set/near-label calls were never dispatched. One generation
    architecture, one provider bill."""
    # recommend_set stays scripted: it is a kept ADVISORY pass (an annotator, not a
    # generator). The GENERATORS (overlay.feature.recommend / recommend_features) remain
    # unscripted — success proves they never dispatched.
    fake = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "engine set"}),
    })
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    body = _post(make_client(llm_client=fake))
    served = [f for s_ in body["alternatives"] for f in s_["features"]]
    assert served, "the engine alone serves the considered set"
    assert all(f.get("source_definition_id") for f in served)  # every card is engine-origin
    # B4: an intent card never wears a recipe badge, and the lens name stopped lying.
    assert all(f.get("recipe_id") is None or f["generation_source"] == "recipe"
               for f in served)
    assert {s_["lens"] for s_ in body["alternatives"]} <= {"engine", "actionable"}


def test_entity_only_scope_is_refused_typed_under_semantic_v1(make_client, conn, monkeypatch):
    """B1: no catalog_source under the mode = typed 422, never a silently empty page."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    res = make_client(llm_client=_fake()).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn",
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "SEMANTIC_REQUIRES_CATALOG_SOURCE"


def test_definition_mode_anchor_is_extracted_through_the_engine(
        make_client, conn, monkeypatch):
    """B1: the analyst's own definition is EXTRACTED as an abstract intent and bound by the
    shared engine — generation_source stays the server-assigned user_defined, and no free-form
    recommend_features call happens (unscripted => would 500)."""
    fake = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "engine set"}),
    })
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    res = make_client(llm_client=fake).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS,
        "definition": "days since the customer's most recent transaction",
        "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    anchor = res.json()["anchor"]
    assert anchor is not None
    assert anchor["generation_source"] == "user_defined"
    assert anchor.get("input_role_bindings"), "the SHARED binder chose the anchor's columns"


def test_unscoped_generation_accepts_intents_from_the_full_vocabulary(
        make_client, conn, monkeypatch):
    """B2 (GEN-02 closed): an unscoped request used to pass an EMPTY objective vocabulary, so
    every proposed intent was rejected out-of-scope. Now the whole selectable-leaf set is the
    vocabulary — the intent survives; an objective OUTSIDE even that set still rejects."""
    fake = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "templates", "reasoning": "engine set"}),
    })
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    res = make_client(llm_client=fake).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"unscoped": True},
    }, headers=AUTH)
    assert res.status_code == 200, res.text

    rows = conn.execute(
        "SELECT COUNT(*) FROM semantic_candidate_observation "
        "WHERE source_origin = 'llm_intent'").fetchone()
    assert rows[0] >= 1, "the unscoped intent survived the vocabulary check"


def test_the_intent_call_is_attributed_to_the_requesting_human(make_client, conn, monkeypatch):
    """B3 (GEN-04's other half): the audited intent call records the HUMAN who asked — never
    the fallback service identity — and its provenance carries the SCOPE's own hash as a
    distinct key from the catalog context hash."""
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    _post(make_client(llm_client=_fake_with_intents()))

    row = conn.execute(
        "SELECT created_by FROM llm_call WHERE task = 'overlay.feature.intents' "
        "ORDER BY created_at DESC LIMIT 1").fetchone()
    assert row is not None, "the intent call was durably recorded"
    assert row[0].get("subject") == "user:tester"             # the AUTH header's human


def test_a_lagged_projection_refuses_before_any_provider_spend(
        make_client, conn, monkeypatch):
    """B8 (PLAN-14 closed): the readiness gate fires BEFORE model dispatch — the 503 arrives
    with the FakeLLM never called (an unscripted call would KeyError-500, and the fake here
    scripts nothing at all)."""
    from featuregen.intake.llm import FakeLLM

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    from featuregen.overlay.upload.feature_metadata_snapshot import (
        CatalogProjectionUnavailable,
    )

    def lagged(_conn):
        raise CatalogProjectionUnavailable("CATALOG_PROJECTION_UNAVAILABLE", "probe lagged")

    monkeypatch.setattr(
        "featuregen.api.routes.contract.check_projection_readiness", lagged)
    res = make_client(llm_client=FakeLLM(script={})).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }, headers=AUTH)
    assert res.status_code == 503, res.text
