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


def test_shadow_observations_become_append_only_rows(make_client, conn, monkeypatch):
    """SE-10 slice 1: the shadow's per-candidate truth persists as rows — queryable fleet
    metrics under the frozen context's hash — and the store refuses rewrites."""
    import pytest

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_shadow")
    _post(make_client(llm_client=_fake()))

    rows = conn.execute(
        "SELECT source_definition_id, source_origin, binding_state, context_hash, "
        "       generation_run_id, eligibility "
        "FROM semantic_candidate_observation ORDER BY recorded_at").fetchall()
    assert rows, "the shadow run persisted observations"
    definition_ids = {r[0] for r in rows}
    assert all(r[1] == "recipe_v2" for r in rows)
    assert all(r[2] in ("bound", "ambiguous", "missing", "blocked") for r in rows)
    assert len({r[3] for r in rows}) == 1                     # one frozen context per run
    assert all(r[4] != "unattributed" for r in rows), "the scoped route's run id is attributed"
    assert definition_ids                                     # real registry ids
    # Append-only: an observation is what the run saw; rewriting it is a DB error.
    import psycopg

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        with conn.transaction():
            conn.execute("UPDATE semantic_candidate_observation SET binding_state = 'bound'")

def test_shadow_metrics_fold_the_recorded_observations(make_client, conn, monkeypatch):
    """SE-14: the cutover gate's measured inputs come from the rows the shadow actually wrote —
    an empty store reads observation_rows_present=False (which BLOCKS), and a populated one
    counts states and preventions from the recorded verdicts, never from a projection."""
    from featuregen.overlay.upload.semantic_candidate_store import semantic_shadow_metrics

    empty = semantic_shadow_metrics(conn)
    assert empty["observation_rows_present"] is False
    assert empty["observations"] == 0

    _bank(conn)
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_shadow")
    _post(make_client(llm_client=_fake()))

    metrics = semantic_shadow_metrics(conn)
    assert metrics["observation_rows_present"] is True
    assert metrics["observations"] > 0
    assert metrics["generation_runs"] == 1 and metrics["distinct_contexts"] == 1
    assert sum(metrics["candidates_by_binding_state"].values()) == metrics["observations"]
    assert set(metrics["candidates_by_origin"]) == {"recipe_v2"}
    assert metrics["identifier_as_measure_preventions"] >= 0   # counted from verdict codes

