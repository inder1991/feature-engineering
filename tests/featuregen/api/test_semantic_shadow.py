"""SE-7 part 2 — semantic_shadow: the V2 lens observes beside the template lens, invisibly.

Three properties, each load-bearing for the rollout: the shadow CHANGES NOTHING a client sees
(same response schema, same template-lens candidates); it actually RUNS (the divergence log is
the Tranche-2 metric source); and a shadow failure is swallowed under its savepoint — the
user's request succeeds identically. `legacy` (the frozen default) never touches the lens.
"""
from __future__ import annotations

import pytest

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


def _comparable(body: dict) -> tuple:
    """The response minus per-run minted ids — what must be mode-invariant."""
    lens_names = {s["lens"]: sorted(f["name"] for f in s["features"])
                  for s in body["alternatives"]}
    return (sorted(body.keys()), lens_names,
            sorted(d["recipe_id"] for d in body.get("dispositions", [])))


def test_shadow_mode_changes_nothing_a_client_sees_and_logs_the_divergence(
        make_client, conn, monkeypatch, caplog):
    _bank(conn)
    legacy_body = _post(make_client(llm_client=_fake()))

    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_shadow")
    with caplog.at_level("INFO", logger="featuregen.overlay.upload.contract.gate1"):
        shadow_body = _post(make_client(llm_client=_fake()))

    assert _comparable(shadow_body) == _comparable(legacy_body)
    shadow_lines = [r.message for r in caplog.records if r.message.startswith("semantic-shadow:")]
    assert len(shadow_lines) == 1
    assert "eligible=" in shadow_lines[0] and "| legacy grounded=" in shadow_lines[0]


def test_legacy_default_never_touches_the_lens(make_client, conn, monkeypatch):
    _bank(conn)

    def boom(*a, **k):  # pragma: no cover - would fail the test if reached
        raise AssertionError("the V2 lens must not run in legacy mode")

    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_planning_lens.v2_recipe_candidates", boom)
    _post(make_client(llm_client=_fake()))          # default mode: no env var set


def test_a_shadow_failure_is_swallowed_and_the_response_is_unchanged(
        make_client, conn, monkeypatch, caplog):
    _bank(conn)
    baseline = _post(make_client(llm_client=_fake()))

    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_shadow")
    monkeypatch.setattr(
        "featuregen.overlay.upload.recipe_planning_lens.v2_recipe_candidates",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("shadow store on fire")))
    with caplog.at_level("ERROR", logger="featuregen.overlay.upload.contract.gate1"):
        body = _post(make_client(llm_client=_fake()))

    assert _comparable(body) == _comparable(baseline)
    assert any("semantic-shadow comparison failed" in r.message for r in caplog.records)
