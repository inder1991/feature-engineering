"""Phase-1B Task 7 — POST /contract/considered-set mints the run, persists the confirmed scope, scopes
grounding and attaches a disposition lens.

Exercises the integration across ``gate1.build_considered_set`` (Part A: it exposes the grounded /
rejected template ids) and the ``considered_set`` route (Part B: confirmed-scope path). The canonical
linkage is proved end to end — the route mints ``generation_run_id``, persists the scope BEFORE the
builder, and ``scope_for_run(run)`` reconstructs the governing scope by run id. ``broaden`` is the same
path re-called with ``unscoped=true``, a NEW run, and ``supersedes_scope_id``. The no-scope path stays
byte-identical to pre-1B.
"""
from datetime import datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.scope_records import scope_for_run
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import (
    ConfirmedScope,
    applicability_result,
)
from featuregen.overlay.upload.taxonomy.recognition import APPLICABILITY_MAPPING_VERSION
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK
from featuregen.overlay.upload.templates import ALL_TEMPLATES

FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
# A churn recipe that binds on the catalog below (eligible) + a credit/fraud recipe that is out of a
# churn scope (out_of_scope). Mirrors tests/.../contract/test_gate1_scoped.py.
CHURN_RECIPE = "balance_trend"
CREDIT_RECIPE = "credit_utilisation"
FRAUD_RECIPE = "txn_velocity_spike"


def _fake() -> FakeLLM:
    """The generation tasks build_considered_set drives (no recognizer entry — recognition is a
    separate API step). Mirrors test_gate1_scoped's client."""
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _bank_multi(conn) -> None:
    """A TWO-family catalog: an ``accounts`` table the retail_churn recipes ground on, PLUS a
    ``facilities`` table (a credit-limit grain) the credit recipes ground on. So a full (unscoped)
    grounding surfaces BOTH families, while a churn-scoped grounding surfaces only the churn recipes —
    the direct, non-trivial 'fewer template candidates' signal. Mirrors test_gate1_scoped's churn
    catalog for the accounts half."""
    from datetime import UTC, datetime
    now = datetime(2026, 7, 10, tzinfo=UTC)
    catalog = [
        # ── accounts → the retail_churn recipes ──
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
        # ── facilities → the credit-utilisation (limit) recipes: a NON-churn family, out of scope for a
        #    churn narrowing but grounded under a full/unscoped run ──
        (CanonicalRow("bank", "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("bank", "facilities", "drawn", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
        (CanonicalRow("bank", "facilities", "asof2", "timestamp", as_of=True), "as_of_date"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "bank", rows, concepts=concepts)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (now, now))


def _templates_names(body: dict) -> set[str]:
    return {f["name"] for s in body["alternatives"] if s["lens"] == "templates" for f in s["features"]}


def _disposition(body: dict, recipe_id: str) -> dict | None:
    return next((d for d in body["dispositions"] if d["recipe_id"] == recipe_id), None)


def _post(client, **extra) -> dict:
    payload = {"hypothesis": HYPOTHESIS, "objective": "predict churn",
               "catalog_source": "bank", "target_ref": TARGET, **extra}
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


# ── scoped: mint run + persist scope BEFORE builder + narrowed grounding + disposition lens ───────────
def test_scoped_call_narrows_grounding_and_returns_dispositions(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")   # scoped grounding on → grounding narrows to the eligible subset
    _bank_multi(conn)
    client = make_client(_fake())

    scoped = _post(client, confirmed_scope={"primary": CHURN, "confirmation_source": "user_confirmed"})
    # A no-scope call on the SAME catalog grounds the whole registry → the scoped run grounds fewer.
    unscoped = _post(make_client(_fake()))

    assert len(_templates_names(scoped)) < len(_templates_names(unscoped)), (
        "scoped grounding must surface fewer template candidates than the full registry")

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

    # in_scope_count is APPLICABILITY-owned (not recognition).
    expected = applicability_result(ConfirmedScope(primary=CHURN)).eligible_ids
    assert scoped["in_scope_count"] == len(expected)
    assert scoped["in_scope_count"] < len(ALL_TEMPLATES)

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

    # Broaden fails open to FULL grounding: every recipe is eligible-by-applicability (none out of scope).
    assert broadened["in_scope_count"] == len(ALL_TEMPLATES)
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
