"""S1B-4 — ``GET /governance/bridge-demand``: the three demand queues plus the resolution summary.

The read side of the governed observation ledger, and the first consumer of it. Everything here is
a DERIVATION over rows the planner lanes wrote; the route decides nothing, writes nothing, and
gates exactly like the entity-bridge queue beside it (``require_confirmer`` — the raw
``platform-admin`` role CLAIM, not the functional ``platform_admin`` bundle).

Four things this suite exists to pin:

* **the claim boundary** — the functional bundle is refused, the raw claim is served;
* **the payload is the SHAPE the panel renders** — nested position rows, mode and recency splits,
  distinct intents vs distinct runs, and ``suggested_endpoints`` as FLAT catalog-qualified strings;
* **the counts are honest about what they count** — ``demand_rows`` is demand rows, and one
  observation carrying two different gaps files two of them;
* **pagination round-trips** — the aggregate is computed over everything and only then paged, so a
  group on page 2 carries the same totals it would have carried on page 1.

Seeding drives the REAL store writers on the suite's rolled-back ``conn`` — the same connection the
TestClient's ``get_conn`` override serves. NEVER ``conn.commit()``: a committed API test leaks its
rows into every later suite.
"""
from __future__ import annotations

import pytest
from tests.featuregen.api._helpers import AUTH

from featuregen.overlay.upload.governed_observation_store import (
    record_bridge_demand,
    record_planning_observations,
)

ADMIN = {"X-User": "gov", "X-Roles": "platform-admin"}


def _run(conn, suffix: str) -> tuple[str, str]:
    intent_id, run_id = f"s1b4_int_{suffix}", f"s1b4_run_{suffix}"
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    conn.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
                 "VALUES (%s, %s, '{}'::jsonb)", (run_id, intent_id))
    return intent_id, run_id


def _observation(**overrides) -> dict:
    row = {
        "definition_origin": "recipe_v2",
        "canonical_definition_id": "posted_transaction_average_amount",
        "governed_variant_id": "gvar_" + "a" * 64,
        "planning_request_hash": "p" * 64,
        "physical_plan_content_hash": "unresolved",
        "target_entity": "account",
        "anchor_catalog_source": "ops",
        "resolution_status": "unsanctioned_bridge",
    }
    row.update(overrides)
    return row


def _hop(**overrides) -> dict:
    demand = {
        "verdict": "unsanctioned_bridge",
        "recipe_revision_hash": "r" * 64,
        "relationship_id": "transaction_to_account",
        "relationship_version": "1.0.0",
        "from_entity": "transaction",
        "to_entity": "account",
        "position_catalog": "ops",
        "position_table_ref": "public.transactions",
        "hop_index": 0,
        "realizers": [{"catalog_source": "rev", "to_object_ref": "public.accounts",
                       "from_key_ref": "public.transactions.account_id",
                       "to_key_ref": "public.accounts.account_id"}],
        "near_side_key_refs": ["public.transactions.account_id"],
    }
    demand.update(overrides)
    return demand


def _seed(conn, suffix: str, *, mode: str = "live", demands=None, **observation) -> str:
    intent_id, run_id = _run(conn, suffix)
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode=mode,
        rows=[_observation(governed_variant_id=f"gvar_{suffix}", **observation)])[0]
    if demands:
        record_bridge_demand(conn, observation_id=observation_id, rejections=demands)
    return observation_id


# ── the claim boundary ────────────────────────────────────────────────────────────────────────


def test_the_functional_bundle_is_refused(client):
    """``AUTH`` carries the FUNCTIONAL ``platform_admin`` role, not the raw ``platform-admin``
    claim ``require_confirmer`` gates on — the same boundary every governance route enforces."""
    assert client.get("/governance/bridge-demand", headers=AUTH).status_code == 403


def test_the_raw_platform_admin_claim_is_served(client):
    response = client.get("/governance/bridge-demand", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert set(body["queues"]) == {"bridge_demand", "realization_gap", "planner_capacity"}
    # an empty ledger is an empty answer, never an invented one
    assert all(body["queues"][queue] == [] for queue in body["queues"])
    assert body["resolution_summary"]["totals"] == {"observations": 0, "resolved": 0,
                                                    "resolution_rate": 0.0}


# ── the payload the panel renders ─────────────────────────────────────────────────────────────


def test_a_seeded_demand_surfaces_with_its_nested_position_row(client, conn):
    _seed(conn, "live_one", demands=[_hop()])
    _seed(conn, "tele_one", mode="telemetry", demands=[_hop()])

    body = client.get("/governance/bridge-demand", headers=ADMIN).json()
    (group,) = body["queues"]["bridge_demand"]
    assert (group["relationship_id"], group["from_entity"], group["to_entity"]) == (
        "transaction_to_account", "transaction", "account")
    assert group["demand_rows"] == 2
    # two runs of two intents — the retry-gaming denominators the panel shows beside the count
    assert (group["distinct_intents"], group["distinct_runs"]) == (2, 2)
    assert (group["live_observations"], group["telemetry_observations"]) == (1, 1)
    assert group["recent_observations"] == 2 and group["historical_observations"] == 0
    # ONE shape: flat, catalog-qualified refs — the far realizer, then the near-side anchor
    assert group["suggested_endpoints"] == ["rev::public.accounts.account_id",
                                            "ops::public.transactions.account_id"]

    (nested,) = group["nested"]
    assert (nested["position_catalog"], nested["position_table_ref"]) == ("ops",
                                                                          "public.transactions")
    assert nested["demand_rows"] == 2


def test_one_observation_with_two_gaps_files_into_two_queues(client, conn):
    """S1B-3's co-occurrence ruling, visible on the surface a human reads: an unmet HOP (sanction a
    crossing) and a contract-level realization gap (build or attach one for a crossing that IS
    sanctioned) are different work for different people, so they are two rows in two queues — and
    ``demand_rows`` counting two of them is exactly why it is not called "observations"."""
    _seed(conn, "cooccur", demands=[
        _hop(),
        _hop(verdict="missing_realization", relationship_id="transaction_to_account",
             realizers=[{"catalog_source": "rev", "to_key_ref": "public.accounts.account_id",
                         "realization_state": "no_revision_exists"}]),
    ])
    body = client.get("/governance/bridge-demand", headers=ADMIN).json()
    assert body["queues"]["bridge_demand"][0]["demand_rows"] == 1
    assert body["queues"]["realization_gap"][0]["demand_rows"] == 1
    # ...and the realization row carries the state that says which work item it is
    assert body["queues"]["realization_gap"][0]["suggested_endpoints"] == [
        "rev::public.accounts.account_id", "ops::public.transactions.account_id"]


def test_capacity_demand_lands_in_the_planner_capacity_queue(client, conn):
    _seed(conn, "capacity", demands=[{"verdict": "bounded_out_max_frontier_states",
                                      "recipe_revision_hash": "r" * 64,
                                      "anchor_catalog_source": "ops"}])
    body = client.get("/governance/bridge-demand", headers=ADMIN).json()
    assert body["queues"]["bridge_demand"] == []
    (group,) = body["queues"]["planner_capacity"]
    # a capacity row's hop identity is reduced by ruling — the demand is the BUDGET, not a hop
    assert (group["relationship_id"], group["from_entity"], group["to_entity"]) == ("", "", "")
    assert group["demand_rows"] == 1


def test_the_resolution_summary_rides_along(client, conn):
    _seed(conn, "resolved_one", resolution_status="resolved",
          physical_plan_content_hash="c" * 64)
    _seed(conn, "refused_one", demands=[_hop()])

    summary = client.get("/governance/bridge-demand", headers=ADMIN).json()["resolution_summary"]
    assert summary["totals"] == {"observations": 2, "resolved": 1, "resolution_rate": 0.5}
    assert summary["resolved_statuses"] == ["resolved"]
    by_origin = {row["definition_origin"]: row for row in summary["by_origin"]}
    assert by_origin["recipe_v2"]["resolution_rate"] == 0.5
    assert {row["anchor_catalog_source"] for row in summary["by_anchor_catalog"]} == {"ops"}


# ── pagination ────────────────────────────────────────────────────────────────────────────────


def test_cursor_pagination_round_trips(client, conn):
    for index in range(3):
        _seed(conn, f"page_{index}", demands=[_hop(relationship_id=f"rel_{index:02d}")])

    first = client.get("/governance/bridge-demand?limit=2", headers=ADMIN).json()
    assert [g["relationship_id"] for g in first["queues"]["bridge_demand"]] == ["rel_00", "rel_01"]
    assert first["next_cursor"]

    second = client.get(f"/governance/bridge-demand?limit=2&cursor={first['next_cursor']}",
                        headers=ADMIN).json()
    assert [g["relationship_id"] for g in second["queues"]["bridge_demand"]] == ["rel_02"]
    assert second["next_cursor"] is None
    assert second["cursor"] == first["next_cursor"]     # echoed, so a client can prove its place


def test_an_unusable_cursor_is_a_client_error_not_a_500(client):
    response = client.get("/governance/bridge-demand?cursor=not-a-cursor", headers=ADMIN)
    assert response.status_code == 400


@pytest.mark.parametrize("limit", [0, 101])
def test_the_limit_is_clamped_by_the_route(client, limit):
    """A read this wide is a governance surface, not a bulk export: the bound is the route's, so no
    caller can ask the aggregate for an unbounded page."""
    response = client.get(f"/governance/bridge-demand?limit={limit}", headers=ADMIN)
    assert response.status_code == 422
