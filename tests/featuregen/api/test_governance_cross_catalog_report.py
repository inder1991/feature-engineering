"""S1C-2 — ``GET /governance/cross-catalog-report``: the wave-1 quality report route.

One confirmer-gated read beside ``/governance/bridge-demand``, mirroring its exact conventions:
the raw ``platform-admin`` claim is served, the functional ``platform_admin`` bundle is refused,
and everything the route returns is a READ-ONLY derivation over the governed observation ledger.

Seeding drives the REAL store writers on the suite's rolled-back ``conn`` — the same connection
the TestClient's ``get_conn`` override serves. NEVER ``conn.commit()``: a committed API test
leaks its rows into every later suite.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH

from featuregen.overlay.upload.feature_metadata_snapshot import ensure_generation_run
from featuregen.overlay.upload.governed_observation_store import record_planning_observations

ADMIN = {"X-User": "gov", "X-Roles": "platform-admin"}


def _seed(conn, suffix: str, **overrides) -> None:
    intent_id, run_id = f"s1c2api_int_{suffix}", f"s1c2api_run_{suffix}"
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    ensure_generation_run(conn, run_id, {}, {}, intent_id=intent_id)
    row = {
        "definition_origin": "recipe_v2",
        "canonical_definition_id": "recipe:rail_txn_count",
        "recipe_id": "rail_txn_count",
        "governed_variant_id": f"gvar_{suffix}",
        "planning_request_hash": "p" * 64,
        "physical_plan_content_hash": "c" * 64,
        "target_entity": "account",
        "anchor_catalog_source": "ops",
        "resolution_status": "resolved",
        "authority_floor_status": "met",
    }
    row.update(overrides)
    record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                 observation_mode="live", rows=[row])


def test_the_functional_bundle_is_refused(client):
    """``AUTH`` carries the FUNCTIONAL ``platform_admin`` role, not the raw ``platform-admin``
    claim ``require_confirmer`` gates on — the same boundary as bridge-demand beside it."""
    assert client.get("/governance/cross-catalog-report", headers=AUTH).status_code == 403


def test_an_empty_ledger_reports_zeroes_never_invented_numbers(client):
    response = client.get("/governance/cross-catalog-report", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["origin_coverage"]["totals"]["observations"] == 0
    assert body["resolution_by_domain"] == []
    assert body["authority_floor"]["pass_rate"] is None            # 0/0 is not 0%, it is nothing
    assert body["worker_latency"]["enqueue_to_complete_seconds"] is None
    assert body["bridge_demand"]["queues"] == {
        queue: {"demand_rows": 0, "distinct_demand_identities": 0}
        for queue in ("bridge_demand", "planner_capacity", "realization_gap")}


def test_the_raw_claim_is_served_over_seeded_content(client, conn):
    _seed(conn, "ok")
    _seed(conn, "refused", resolution_status="unsanctioned_bridge",
          physical_plan_content_hash="unresolved", authority_floor_status="unmet",
          reason_codes=["unsanctioned_bridge"])

    response = client.get("/governance/cross-catalog-report", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["origin_coverage"]["totals"] == {"observations": 2, "resolved": 1,
                                                 "resolution_rate": 0.5}
    domains = {row["bucket"]: row for row in body["resolution_by_domain"]}
    assert domains["payments"]["observations"] == 2
    assert body["authority_floor"]["pass_rate"] == 0.5
    assert body["refusal_taxonomy"]["top"] == [{"reason_code": "unsanctioned_bridge",
                                                "occurrences": 1}]
    assert body["corpus_status"]["entries"] > 0
    # the honesty section always ships, and fan-out-risk lives in it (1120 has no cardinalities)
    metrics = {entry["metric"] for entry in body["not_computable_in_stage_1"]}
    assert "fan_out_risk_distribution" in metrics
    assert "chooser_accuracy" in metrics
