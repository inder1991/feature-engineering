"""SE-11 step 1 — the EXPLICIT response contract: v2 is asked for, v1 never leaks it.

The rollout rule the plan pins: an omitted version preserves the current response byte-shape,
and an old client never infers the version from optional fields — the marker is top-level and
present ONLY when the client asked. The v2 additions are the version marker itself plus the
resolved semantic-planning mode (the step-7 diagnostic); the per-card semantic fields already
ride the v2 card serializer on every path.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"


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
    ]
    build_graph(conn, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _post(client, **extra) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
        **extra,
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def test_v2_is_asked_for_and_carries_the_marker_plus_the_mode(make_client, conn, monkeypatch):
    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_shadow")
    body = _post(make_client(llm_client=_fake()), contract_version=2)
    assert body["contract_version"] == 2
    assert body["semantic_planning_mode"] == "semantic_shadow"


def test_v1_default_never_carries_the_new_keys(make_client, conn):
    """The no-silent-leak pin: an omitted version is TODAY'S response — a frozen old client
    must never see a semantic field appear and be tempted to infer."""
    _bank(conn)
    body = _post(make_client(llm_client=_fake()))
    assert "contract_version" not in body
    assert "semantic_planning_mode" not in body


def test_v2_on_the_emergency_unscoped_path_is_refused(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_CONFIRMATION_REQUIRED", "0")
    _bank(conn)
    res = make_client(llm_client=_fake()).post("/contract/considered-set", json={
        "hypothesis": "h", "objective": "o", "catalog_source": "bank",
        "contract_version": 2,
    }, headers=AUTH)
    assert res.status_code == 422
    assert "confirmed_scope" in res.text
