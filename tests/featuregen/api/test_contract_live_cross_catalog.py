"""Phase-3C.2a Task 5 — POST /contract/considered-set live cross-catalog readiness gate + is_live wiring.

Flag-on-but-not-activation-approved → HTTP 503 BEFORE any LLM/planner dispatch and before any run/scope
is minted (fail-closed, never a legacy fallback). Flag-off / flag-on-approved → the route threads the
resolved ``is_live`` boolean into ``build_considered_set``; flag-off runs no readiness query.
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

from featuregen.overlay.upload.contract.gate1 import ConsideredSet
from featuregen.overlay.upload.contract.live_activation import record_decision, record_evaluation

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
