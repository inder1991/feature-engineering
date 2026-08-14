"""Phase-1B Task 7 — POST /contract/considered-set mints the run, persists the confirmed scope, scopes
generation and attaches a disposition lens.

The canonical linkage is proved end to end — the route mints ``generation_run_id``, persists the scope
BEFORE the builder, and ``scope_for_run(run)`` reconstructs the governing scope by run id. ``broaden``
is the same path re-called with ``unscoped=true``, a NEW run, and ``supersedes_scope_id``. The
no-scope emergency path stays byte-identical to pre-1B.

The E4 cutover (2026-08-14) changed WHICH UNIVERSE this file is talking about. A request carrying a
confirmed scope is served by the semantic engine, and the disposition lens folds the V2 recipe
registry — the universe that was actually planned — rather than the legacy template registry. So the
recipe ids named below are V2 ids, the in-scope counts are measured against ``V2_RECIPES``, and the
"scoped grounds less than unscoped" signal is read off the scope's own eligible set instead of off two
different pipelines' lens names. The one shape that has NO answer any more is the entity-only
(cross-catalog) request: it is refused typed, before any planning happens at all.
"""
from datetime import datetime

import pytest
from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    Gate1Error,
    select_and_record_gate1_choice,
)
from featuregen.overlay.upload.contract.scope_records import scope_for_run
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_planning_lens import v2_applicability_as_result
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.taxonomy.recognition import APPLICABILITY_MAPPING_VERSION
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK

FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
# A churn recipe that binds on the catalog below (eligible) + a credit and a fraud recipe that are out
# of a churn scope (out_of_scope). All three are V2 registry ids — the disposition universe.
CHURN_RECIPE = "balance_volatility"
CREDIT_RECIPE = "days_past_due_max"
FRAUD_RECIPE = "txn_velocity_spike"


def _fake() -> FakeLLM:
    """The generation tasks the builder still drives (no recognizer entry — recognition is a separate
    API step). Only the ADVISORY set-recommendation pass is scripted: the free-form
    ``overlay.feature.recommend`` generator was DELETED in the E4 cutover, and the intents task is left
    unscripted on purpose — the intent lens fails soft, so the recipe half of the engine serves alone
    and every assertion below stays deterministic."""
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _bank_multi(conn) -> None:
    """A TWO-family catalog: an ``accounts`` table the retail-churn recipes bind on, PLUS a
    ``facilities`` table (a lending grain) the credit recipes bind on. A churn-scoped run therefore
    plans only the churn family while an unscoped one plans both — the direct, non-trivial narrowing
    signal.

    Two shapes here are load-bearing for the E4 engine, and were not for the legacy grounding pass
    that preceded it:

    * ``accounts.account_id`` — the churn balance recipes are keyed per ACCOUNT, so a catalog with
      only a customer key leaves every one of them REQUIRED_OPERAND_MISSING;
    * exactly ONE as-of column and one monetary_stock column in the whole catalog. A second of
      either is a genuine AMBIGUOUS_TIME_BINDING / AMBIGUOUS_MEASURE_BINDING, which the binder
      refuses by design rather than guessing — so the facilities table carries its own
      lending-specific facts (``dpd``, ``sicr_flag``) instead of a duplicate balance and as-of.
    """
    from datetime import UTC, datetime
    # Watermark the catalog as fresh AS OF THE TEST RUN — the route grounds against the real wall clock
    # (datetime.now), so a hardcoded past date would rot the freshness gate once that date passes.
    now = datetime.now(UTC)
    catalog = [
        # ── accounts → the retail_churn recipes ──
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "account_id", "integer", entity="Account"), "account_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
        # ── facilities → the lending recipes: a NON-churn family, out of scope under a churn
        #    narrowing but planned under a full/unscoped run ──
        (CanonicalRow("bank", "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("bank", "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
        (CanonicalRow("bank", "facilities", "dpd_days", "integer"), "dpd"),
        (CanonicalRow("bank", "facilities", "sicr_ind", "boolean"), "sicr_flag"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "bank", rows, concepts=concepts)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (now, now))


def _planned_ids(body: dict) -> set[str]:
    """Every recipe the run actually planned — the in-scope half of the disposition fold. Post-cutover
    this replaces "count the names in the templates lens": a scoped and an unscoped run are now served
    by the SAME engine, so the honest narrowing signal is which recipes were considered at all, not
    which lens name the response happened to carry."""
    return {d["recipe_id"] for d in body["dispositions"] if d["relevance_tier"] is not None}


def _disposition(body: dict, recipe_id: str) -> dict | None:
    return next((d for d in body["dispositions"] if d["recipe_id"] == recipe_id), None)


def _post(client, **extra) -> dict:
    payload = {"hypothesis": HYPOTHESIS, "objective": "predict churn",
               "catalog_source": "bank", "target_ref": TARGET, **extra}
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


# ── scoped: mint run + persist scope BEFORE builder + narrowed planning + disposition lens ────────────
def test_scoped_call_narrows_grounding_and_returns_dispositions(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_multi(conn)
    client = make_client(_fake())

    scoped = _post(client, confirmed_scope={"primary": CHURN, "confirmation_source": "user_confirmed"})
    # A BROADENED run on the SAME catalog plans the whole registry → the churn-scoped run plans a
    # strict subset of it. (Pre-cutover this compared the scoped response's templates lens with an
    # unscoped legacy call's; those are now two different pipelines, so comparing them would prove
    # nothing about narrowing. The scope's own planned set is the honest comparison.)
    broadened = _post(make_client(_fake()),
                      confirmed_scope={"unscoped": True, "confirmation_source": "user_broadened"})

    assert _planned_ids(scoped) < _planned_ids(broadened), (
        "a churn-scoped run must plan strictly fewer recipes than a broadened one")

    # The disposition lens: a churn recipe that bound is ELIGIBLE; credit/fraud recipes are OUT_OF_SCOPE.
    churn = _disposition(scoped, CHURN_RECIPE)
    assert churn is not None and churn["final_disposition"] == "eligible"
    assert churn["relevance_tier"] in ("primary", "supporting")
    assert churn["grounding"]["status"] == "completed" and churn["safety"]["status"] == "completed"
    for out in (CREDIT_RECIPE, FRAUD_RECIPE):
        d = _disposition(scoped, out)
        assert d is not None and d["final_disposition"] == "out_of_scope", out
        assert d["relevance_tier"] is None
        assert d["grounding"]["status"] == "not_evaluated"      # never a bare null downstream

    # in_scope_count is APPLICABILITY-owned (not recognition) — over the V2 universe the engine
    # actually planned, which is what the disposition lens folds after the cutover.
    expected = v2_applicability_as_result(ConfirmedScope(primary=CHURN)).eligible_ids
    assert scoped["in_scope_count"] == len(expected)
    assert scoped["in_scope_count"] < len(V2_RECIPES)

    # The scope was persisted BEFORE the builder: a parent row + a primary child exist for the minted run.
    run = scoped["generation_run_id"]
    assert run and scoped["scope_id"]
    parent = conn.execute(
        "SELECT scope_id, scope_mode FROM confirmed_generation_scope WHERE generation_run_id = %s",
        (run,)).fetchone()
    assert parent is not None and parent[1] == "scoped"
    children = conn.execute(
        "SELECT use_case_id, relationship FROM confirmed_scope_use_case WHERE scope_id = %s",
        (parent[0],)).fetchall()
    assert (CHURN, "primary") in children
    # scope_for_run rebuilds the governing scope BY RUN ID (the canonical linkage).
    assert scope_for_run(conn, run) == ConfirmedScope(primary=CHURN)

    chosen = next(
        feature
        for feature_set in scoped["alternatives"]
        for feature in feature_set["features"]
    )
    with pytest.raises(Gate1Error, match="REGENERATE_FROM_CURRENT_CONSIDERED_SET"):
        select_and_record_gate1_choice(
            conn,
            scoped["intent_id"],
            chosen_source="alternative",
            chosen_option_id=chosen["name"],
            actor="user:tester",
        )
    pinned = select_and_record_gate1_choice(
        conn,
        scoped["intent_id"],
        chosen_source="alternative",
        chosen_option_id=chosen["name"],
        actor="user:tester",
        expected_generation_run_id=run,
    )
    assert pinned is not None
    assert pinned.considered_revision_id
    assert pinned.snapshot_lineage["generation_run_id"] == run


# ── broaden: a NEW unscoped run supersedes the first; both scopes retained + retrievable ──────────────
def test_broaden_supersedes_first_scope_and_full_grounds(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_multi(conn)

    scoped = _post(make_client(_fake()),
                   confirmed_scope={"primary": CHURN, "confirmation_source": "user_confirmed"})
    first_run, first_scope = scoped["generation_run_id"], scoped["scope_id"]

    broadened = _post(make_client(_fake()), confirmed_scope={"unscoped": True,
                      "confirmation_source": "user_broadened"},
                      supersedes_scope_id=first_scope)
    broad_run, broad_scope = broadened["generation_run_id"], broadened["scope_id"]

    # A NEW run was minted, and its scope supersedes the first.
    assert broad_run != first_run and broad_scope != first_scope
    row = conn.execute(
        "SELECT supersedes_scope_id, scope_mode FROM confirmed_generation_scope WHERE scope_id = %s",
        (broad_scope,)).fetchone()
    assert row == (first_scope, "unscoped")

    # Broaden fails open to the FULL universe: every V2 recipe is eligible-by-applicability (none out
    # of scope). The count is the planned registry's size, not the legacy template registry's.
    assert broadened["in_scope_count"] == len(V2_RECIPES)
    assert not any(d["final_disposition"] == "out_of_scope" for d in broadened["dispositions"])

    # Both runs' scopes are retrievable by their own run id (supersession is lineage only).
    assert scope_for_run(conn, first_run) == ConfirmedScope(primary=CHURN)
    assert scope_for_run(conn, broad_run) == ConfirmedScope(primary=None, unscoped=True)


# ── no-scope: byte-identical to the pre-1B considered-set response ────────────────────────────────────
def test_no_scope_call_is_byte_unchanged(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")   # even with the flag on, no confirmed_scope → today's exact path
    _bank_multi(conn)
    client = make_client(_fake())

    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn",
        "catalog_source": "bank", "target_ref": TARGET}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()

    # Exactly the pre-1B keys — no run id, no scope id, no dispositions, no in_scope_count.
    assert set(body) == {"intent_id", "anchor", "alternatives", "recommendation", "rejections"}
    # No scope row written on the no-scope path.
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── an invalid confirmed primary (not a selectable leaf) → 422 ───────────────────────────────────────
def test_invalid_primary_is_422(make_client, conn):
    _bank_multi(conn)
    client = make_client(_fake())
    # 'financial_crime' is a real taxonomy node but a NON-selectable domain parent → rejected.
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": "financial_crime"}}, headers=AUTH)
    assert res.status_code == 422, res.text
    # And a wholly unknown id is likewise rejected before any run/scope is minted.
    res2 = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": "not_a_real_use_case"}}, headers=AUTH)
    assert res2.status_code == 422, res2.text
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── Fix 3: a colliding id set (primary ∈ secondary, or a dup secondary) → 422, not a PK-violation 500 ──
def test_primary_in_secondary_and_dup_secondary_are_422(make_client, conn):
    _bank_multi(conn)
    client = make_client(_fake())
    # CHURN as BOTH primary and secondary would violate the confirmed_scope_use_case PK downstream → 422.
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": CHURN, "secondary": [CHURN]}}, headers=AUTH)
    assert res.status_code == 422, res.text
    # A duplicated secondary is likewise rejected (same PK collision).
    res2 = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"secondary": [CHURN, CHURN]}}, headers=AUTH)
    assert res2.status_code == 422, res2.text
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── Fix 1B: a crafted intent_id belonging to ANOTHER actor → 404 (no run/scope minted) ────────────────
def test_scoped_call_with_foreign_intent_id_is_404(make_client, conn):
    _bank_multi(conn)
    alice = {"X-User": "alice", "X-Roles": "platform_admin"}
    bob = {"X-User": "bob", "X-Roles": "platform_admin"}
    # Actor A mints an intent via the recognition endpoint (persists contract_intent for actor A).
    rec_client = make_client(FakeLLM(script={RECOGNIZER_TASK: FakeResponse(output={
        "status": "unscoped", "candidates": [], "ambiguity_note": None})}))
    rec = rec_client.post("/contract/recognitions", json={"hypothesis": HYPOTHESIS}, headers=alice)
    assert rec.status_code == 200, rec.text
    alice_intent = rec.json()["intent_id"]

    # Actor B tries to confirm a scope against A's intent_id → 404; nothing minted/persisted.
    client = make_client(_fake())
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET, "intent_id": alice_intent,
        "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed"}}, headers=bob)
    assert res.status_code == 404, res.text
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0

    # The SAME actor supplying their OWN intent_id is accepted (legitimate reuse still works).
    ok = make_client(_fake()).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET, "intent_id": alice_intent,
        "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed"}}, headers=alice)
    assert ok.status_code == 200, ok.text
    assert ok.json()["intent_id"] == alice_intent


# ── E4: the entity-only request is refused BEFORE anything can run on the request's connection ────────
def test_entity_only_scope_is_refused_before_any_shadow_dispatch(make_client, conn, monkeypatch):
    """The E4 cutover (2026-08-14) closed the entity-only (cross-catalog) shape at the route: with a
    confirmed scope and no ``catalog_source`` the answer is a typed 422 SEMANTIC_REQUIRES_CATALOG_SOURCE,
    because the semantic engine plans over ONE frozen catalog context and the free-form generator that
    used to fill that page is deleted.

    That refusal is what replaced two savepoint-isolation tests here (3B.3a). They proved that a DB
    fault inside the log-only shadow planner could not poison the request's transaction and silently
    turn its commit into a rollback — a real defect, and a real fix, but the shadow planner only ever
    ran on the entity-scoped branch, which no request can now reach. Rather than keep two tests whose
    subject is unreachable, this one asserts the property that now delivers the same guarantee: the
    request is refused BEFORE any of that machinery is dispatched, so there is nothing to poison.
    A ``run_shadow_planner`` that would explode on the request's connection is installed to prove it
    is never called, and the connection is read afterwards to prove it is still usable.
    """
    _bank_multi(conn)

    def _exploding_shadow(shadow_conn, **_kwargs):   # pragma: no cover - reached only on regression
        shadow_conn.execute("SELECT * FROM a_table_that_does_not_exist")

    monkeypatch.setattr("featuregen.api.routes.contract.run_shadow_planner", _exploding_shadow)

    res = make_client(_fake()).post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed",
                            "target_entity": "customer"}}, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "SEMANTIC_REQUIRES_CATALOG_SOURCE"

    # The connection is still usable: the shadow planner never ran, so nothing aborted the
    # transaction (a poisoned txn raises InFailedSqlTransaction on the next statement).
    assert conn.execute("SELECT 1").fetchone() == (1,)


# ── Fix 5: every disposition stage carries the replay stamps (evaluation_version + evaluated_at) ───────
def test_dispositions_carry_replay_stamps(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")
    _bank_multi(conn)
    scoped = _post(make_client(_fake()),
                   confirmed_scope={"primary": CHURN, "confirmation_source": "user_confirmed"})
    assert scoped["dispositions"]
    for d in scoped["dispositions"]:
        for stage in ("applicability", "grounding", "safety"):
            s = d[stage]
            assert s["evaluation_version"] == APPLICABILITY_MAPPING_VERSION
            assert isinstance(s["evaluated_at"], str) and s["evaluated_at"]
            datetime.fromisoformat(s["evaluated_at"])   # ISO-8601, round-trippable for replay


# ── 3B.3c (C8): the contract-compile kill-switch ──────────────────────────────────────────────────────
# FEATUREGEN_INTENT_CONTRACT_COMPILE was the shadow compile pass's dedicated kill switch, read in the
# route (the planner stays pure — no os.environ below the route) and passed verbatim to
# run_shadow_planner(compile_contracts=...). Its test posted the ENTITY-scoped body, the only shape
# that reaches the shadow planner, and observed which value the route threaded. The E4 cutover
# (2026-08-14) refuses that shape with a typed 422 before the route gets there, so the switch has no
# reachable consumer and no observable behaviour left to assert. The test is deleted rather than
# rewritten around a call that cannot happen; the refusal that replaced it is asserted above, in
# test_entity_only_scope_is_refused_before_any_shadow_dispatch.
