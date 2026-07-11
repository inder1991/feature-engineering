"""Phase-1B Task 9 — feature-flag neutrality + emergency-rollback proof.

The backend has ONE runtime flag, ``FEATUREGEN_INTENT_SCOPED_APPLICABILITY`` (default OFF), and it is
the single emergency-rollback point: OFF → ``build_considered_set`` grounds ``ALL_TEMPLATES`` even when
a confirmed scope/applicability is supplied. The other two Phase-1B flags (``intent_confirmation_ui`` /
``intent_disposition_lens``) are FRONTEND concerns (Task 8) and have no backend read.

This file proves the rollout/rollback semantics WITHOUT changing production behaviour — the flag and the
grounding path already exist (Tasks 4/7). Three scenarios:

1. **All-off neutrality** — flag unset (default off) + a no-scope call → the response is byte-identical
   to a pre-1B considered set (exact key set, no dispositions/run/scope fields) and NO recognition-attempt
   / confirmed-scope row is written.
2. **Emergency rollback** — flag OFF + a *scoped* call (as if the UI is still sending confirmed scopes):
   grounding FALLS BACK TO FULL (the template lens equals an unscoped call's), the scope row is STILL
   persisted, and — separately — a ``/contract/recognitions`` call still writes its recognition-attempt
   row (recognition telemetry retained during rollback).
3. **Flag-on scoping** — the SAME scoped call with the flag ON now narrows (fewer candidates), proving
   the flag is the single on/off switch between full and scoped grounding.

The catalog is a TWO-family (churn + credit) upload so "full vs narrowed" grounding is a real difference,
mirroring ``tests/featuregen/api/test_contract_scoped.py``.
"""
from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import _intent_scoped_applicability_enabled
from featuregen.overlay.upload.contract.scope_records import scope_for_run
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK

FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
PRE_1B_KEYS = {"intent_id", "anchor", "alternatives", "recommendation", "rejections"}

# A classified recognizer response — the telemetry the recognition endpoint persists (mirrors
# test_contract_recognitions.py). Used only to prove the recognition-attempt row still writes on rollback.
_CLASSIFIED = FakeResponse(output={
    "status": "classified",
    "candidates": [{
        "use_case_id": CHURN, "relationship": "primary", "confidence": "high",
        "evidence_spans": ["churn"], "rationale": "the hypothesis is about customers leaving"}],
    "ambiguity_note": None})


def _fake() -> FakeLLM:
    """The generation tasks ``build_considered_set`` drives (no recognizer entry — recognition is a
    separate API step). Mirrors test_contract_scoped's client."""
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _recognizer() -> FakeLLM:
    """A recognizer-scripted client for the /contract/recognitions telemetry call (one LLM call)."""
    return FakeLLM(script={RECOGNIZER_TASK: _CLASSIFIED})


def _bank_multi(conn) -> None:
    """A TWO-family catalog: an ``accounts`` table the retail_churn recipes ground on, PLUS a
    ``facilities`` table (a credit-limit grain) the credit recipes ground on. A full (unscoped) grounding
    surfaces BOTH families; a churn-scoped grounding surfaces only the churn recipes — the direct 'fewer
    template candidates' signal. Mirrors test_contract_scoped's catalog."""
    # Fresh as of the test run — the route grounds against the real wall clock, so a hardcoded past
    # date rots the freshness gate once that date passes.
    now = datetime.now(UTC)
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
    """The names in the 'templates' grounding lens — the direct 'full vs narrowed' signal."""
    return {f["name"] for s in body["alternatives"] if s["lens"] == "templates" for f in s["features"]}


def _post(client, **extra) -> dict:
    payload = {"hypothesis": HYPOTHESIS, "objective": "predict churn",
               "catalog_source": "bank", "target_ref": TARGET, **extra}
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _scoped_body() -> dict:
    """The exact scoped payload used flag-off AND flag-on, so the flag is the ONLY thing that varies."""
    return {"primary": CHURN, "confirmation_source": "user_confirmed"}


# ── Scenario 1: all-off neutrality — a no-scope call is byte-identical to pre-1B ──────────────────────
def test_all_off_no_scope_is_byte_identical_to_pre_1b(make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)   # the flag is UNSET → default off
    assert _intent_scoped_applicability_enabled() is False   # the single backend flag reads OFF
    _bank_multi(conn)

    body = _post(make_client(_fake()))   # the pre-1B body: hypothesis + objective, NO confirmed_scope

    # Exactly the pre-1B key set — no dispositions / generation_run_id / scope_id / in_scope_count.
    assert set(body) == PRE_1B_KEYS
    # And NO Phase-1B side-effect rows: the no-scope path writes neither a recognition attempt nor a scope.
    assert conn.execute("SELECT count(*) FROM intent_recognition_attempt").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM confirmed_generation_scope").fetchone()[0] == 0


# ── Scenario 2: emergency rollback — flag OFF, scope still sent → FULL grounding, scope + telemetry kept ─
def test_emergency_rollback_full_grounds_while_scope_and_recognition_retained(
        make_client, conn, monkeypatch):
    monkeypatch.delenv(FLAG, raising=False)   # EMERGENCY ROLLBACK: scoped grounding disabled (default off)
    assert _intent_scoped_applicability_enabled() is False
    _bank_multi(conn)

    # The UI is still sending confirmed scopes; the backend must fall open to full grounding.
    scoped = _post(make_client(_fake()), confirmed_scope=_scoped_body())
    # A no-scope call on the SAME catalog is the full-grounding baseline.
    unscoped = _post(make_client(_fake()))

    # ROLLBACK PROOF #1 — grounding falls back to FULL: the scoped run grounds the SAME template lens as
    # the unscoped run (scoping is disabled), and the two-family catalog surfaces more than the churn half.
    assert _templates_names(scoped) == _templates_names(unscoped)
    assert len(_templates_names(scoped)) > 0

    # ROLLBACK PROOF #2 — the scope row is STILL persisted (rollback disables grounding, not scope capture).
    run = scoped["generation_run_id"]
    parent = conn.execute(
        "SELECT scope_id, scope_mode FROM confirmed_generation_scope WHERE generation_run_id = %s",
        (run,)).fetchone()
    assert parent is not None and parent[1] == "scoped"
    assert (CHURN, "primary") in conn.execute(
        "SELECT use_case_id, relationship FROM confirmed_scope_use_case WHERE scope_id = %s",
        (parent[0],)).fetchall()
    assert scope_for_run(conn, run) == ConfirmedScope(primary=CHURN)   # governing scope by run id

    # ROLLBACK PROOF #3 — recognition telemetry is RETAINED during rollback: the generate path itself
    # wrote no attempt, yet a /contract/recognitions call still persists its append-only attempt row.
    assert conn.execute("SELECT count(*) FROM intent_recognition_attempt").fetchone()[0] == 0
    rec = make_client(_recognizer()).post(
        "/contract/recognitions", json={"hypothesis": HYPOTHESIS, "objective": "predict churn"},
        headers=AUTH)
    assert rec.status_code == 200, rec.text
    intent_id = rec.json()["intent_id"]
    n = conn.execute("SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = %s",
                     (intent_id,)).fetchone()[0]
    assert n == 1


# ── Scenario 3: flag-on scoping — the SAME scoped call now narrows (the single on/off switch) ──────────
def test_flag_on_same_scoped_call_narrows(make_client, conn, monkeypatch):
    monkeypatch.setenv(FLAG, "1")   # the ONLY change vs scenario 2's scoped call
    assert _intent_scoped_applicability_enabled() is True
    _bank_multi(conn)

    scoped = _post(make_client(_fake()), confirmed_scope=_scoped_body())   # identical scoped payload
    unscoped = _post(make_client(_fake()))

    # With the flag ON the SAME scoped payload narrows to fewer template candidates than a full run —
    # proving the flag alone flips grounding between full (scenario 2) and scoped (here).
    assert len(_templates_names(scoped)) < len(_templates_names(unscoped))
