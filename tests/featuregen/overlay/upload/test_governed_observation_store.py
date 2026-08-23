"""S1B-1 — the governed observation / bridge-demand / review stores and the telemetry outbox.

What these tests pin, in the order the brief states them: referential refusal (FK), the
intent-lineage trigger's ``IS NOT DISTINCT FROM`` semantics, mode-inclusive idempotency, demand
identity material, append-only physics on all three 1120 tables (and the outbox's deliberate
exception), the outbox lease lifecycle, and the rollups' aggregate-THEN-limit discipline.
"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from featuregen.overlay.upload.governed_observation_store import (
    DEMAND_VERDICT_QUEUES,
    claim_telemetry_work,
    complete_telemetry_work,
    enqueue_governed_telemetry,
    observation_queues,
    record_bridge_demand,
    record_planning_observations,
    resolution_summary,
)
from featuregen.overlay.upload.planner.contracts import PLANNER_VERSION
from featuregen.overlay.upload.taxonomy.entity_registry import GRAPH_VERSION

# ── seeding helpers ────────────────────────────────────────────────────────────────────────────


def _seed_run(conn, suffix: str, *, with_intent: bool = True) -> tuple[str | None, str]:
    """A (contract_intent, feature_generation_run) pair; ``with_intent=False`` leaves the run's
    intent genuinely absent, which is the only shape the lineage trigger lets NULL through."""
    intent_id = f"s1b1_int_{suffix}" if with_intent else None
    run_id = f"s1b1_run_{suffix}"
    if intent_id is not None:
        conn.execute(
            "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
            "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)", (run_id, intent_id))
    return intent_id, run_id


def _observation(**overrides) -> dict:
    row = {
        "definition_origin": "recipe_v2",
        "canonical_definition_id": "recipe:txn_count_30d",
        "recipe_id": "txn_count_30d",
        "governed_variant_id": "gvar_" + "a" * 64,
        "planning_request_hash": "p" * 64,
        "parameter_binding_hash": "",
        "physical_plan_content_hash": "c" * 64,
        "selected_physical_plan_id": "bp_1",
        "contract_id": None,
        "primary_objective": "risk",
        "target_entity": "party",
        "anchor_catalog_source": "core_banking",
        "resolution_status": "resolved",
        "reason_codes": [],
        "participating_catalogs": ["core_banking"],
        "hop_count": 1,
        "bridge_count": 0,
        "authority_floor_status": "met",
        "safety_status": "clear",
        "readiness": "ready",
        "intent_corroborated": True,
        "param_divergence": [],
        "catalog_scope_material": {"catalogs": ["core_banking"], "target_entity": "party"},
        "metadata_snapshot_id": None,
        "compiler_input_fingerprint": "f" * 64,
    }
    row.update(overrides)
    return row


def _rejection(**overrides) -> dict:
    rejection = {
        "verdict": "unsanctioned_bridge",
        "recipe_revision_hash": "r" * 64,
        "relationship_id": "party_owns_account",
        "relationship_version": "1.0.0",
        "from_entity": "party",
        "to_entity": "account",
        "position_catalog": "core_banking",
        "position_table_ref": "core_banking.party",
        "hop_index": 0,
        "realizers": [{"table_ref": "core_banking.party_account"}],
        "near_side_key_refs": [{"column": "party_id"}],
        "to_endpoint_hint": "cards.account_id",
    }
    rejection.update(overrides)
    return rejection


# ── FK refusal ─────────────────────────────────────────────────────────────────────────────────


def test_observation_naming_a_nonexistent_run_refuses(conn) -> None:
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            record_planning_observations(
                conn, generation_run_id="s1b1_run_absent", intent_id=None,
                observation_mode="live", rows=[_observation()])


def test_demand_naming_a_nonexistent_observation_refuses(conn) -> None:
    intent_id, run_id = _seed_run(conn, "demand_fk")
    record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                 observation_mode="live", rows=[_observation()])
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            record_bridge_demand(conn, observation_id="gpo_absent", rejections=[_rejection()])


# ── the intent-lineage trigger ─────────────────────────────────────────────────────────────────


def test_matching_intent_inserts(conn) -> None:
    intent_id, run_id = _seed_run(conn, "lineage_ok")
    ids = record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                       observation_mode="live", rows=[_observation()])
    assert len(ids) == 1 and ids[0].startswith("gpo_")


def test_omitted_columns_fall_back_to_the_schema_defaults(conn) -> None:
    """The store's defaults must BE 1120's defaults — an observation that omits the optional half
    is a complete row, not a half-written one."""
    intent_id, run_id = _seed_run(conn, "defaults")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[{"definition_origin": "llm_intent", "canonical_definition_id": "intent:abc",
               "governed_variant_id": "gvar_" + "d" * 64, "planning_request_hash": "p" * 64,
               "target_entity": "party", "resolution_status": "unresolved"}])[0]
    row = conn.execute(
        "SELECT recipe_id, parameter_binding_hash, physical_plan_content_hash, contract_id, "
        "       reason_codes, participating_catalogs, param_divergence, catalog_scope_material, "
        "       hop_count, bridge_count, intent_corroborated, metadata_snapshot_id "
        "  FROM governed_planning_observation WHERE observation_id = %s",
        (observation_id,)).fetchone()
    assert row == (None, "", "", None, [], [], [], {}, 0, 0, False, None)


def test_an_unknown_column_refuses_rather_than_being_dropped(conn) -> None:
    intent_id, run_id = _seed_run(conn, "unknown_column")
    with pytest.raises(ValueError, match="unknown governed_planning_observation column"):
        record_planning_observations(
            conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
            rows=[_observation(scope_id="scope_that_rebinds_per_ingest")])


def test_a_blank_mandatory_column_refuses(conn) -> None:
    intent_id, run_id = _seed_run(conn, "blank_mandatory")
    with pytest.raises(ValueError, match="requires non-blank"):
        record_planning_observations(
            conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
            rows=[_observation(governed_variant_id="")])


def test_an_unknown_observation_mode_refuses(conn) -> None:
    intent_id, run_id = _seed_run(conn, "unknown_mode")
    with pytest.raises(ValueError, match="observation_mode"):
        record_planning_observations(
            conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="shadow",
            rows=[_observation()])


def test_review_events_chain_by_supersession(conn) -> None:
    """1060's idiom: 'current' is the newest event, and a changed mind names what it replaced."""
    intent_id, run_id = _seed_run(conn, "review_chain")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    conn.execute(
        "INSERT INTO governed_plan_review_event (event_id, observation_id, reviewer, "
        "reviewer_role, decision) VALUES ('gre_1', %s, 'sme', 'data_steward', 'changes_required')",
        (observation_id,))
    conn.execute(
        "INSERT INTO governed_plan_review_event (event_id, observation_id, reviewer, "
        "reviewer_role, decision, supersedes_event_id) "
        "VALUES ('gre_2', %s, 'sme', 'data_steward', 'approved', 'gre_1')", (observation_id,))
    assert conn.execute(
        "SELECT decision FROM governed_plan_review_event WHERE observation_id = %s "
        "ORDER BY recorded_at DESC, event_id DESC LIMIT 1", (observation_id,)).fetchone()[0] \
        == "approved"
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO governed_plan_review_event (event_id, observation_id, reviewer, "
                "reviewer_role, decision) VALUES ('gre_3', %s, 'sme', 'data_steward', 'retired')",
                (observation_id,))


def test_mismatched_intent_refuses(conn) -> None:
    _seed_run(conn, "lineage_a")
    other_intent, _ = _seed_run(conn, "lineage_b")
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with conn.transaction():
            record_planning_observations(
                conn, generation_run_id="s1b1_run_lineage_a", intent_id=other_intent,
                observation_mode="live", rows=[_observation()])
    assert "intent" in str(excinfo.value)


def test_null_intent_against_a_run_that_has_one_refuses(conn) -> None:
    _, run_id = _seed_run(conn, "lineage_null_vs_value")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.transaction():
            record_planning_observations(conn, generation_run_id=run_id, intent_id=None,
                                         observation_mode="live", rows=[_observation()])


def test_a_run_with_no_intent_accepts_only_null(conn) -> None:
    _, run_id = _seed_run(conn, "lineage_no_intent", with_intent=False)
    ids = record_planning_observations(conn, generation_run_id=run_id, intent_id=None,
                                       observation_mode="live", rows=[_observation()])
    assert len(ids) == 1
    other_intent, _ = _seed_run(conn, "lineage_no_intent_other")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.transaction():
            record_planning_observations(
                conn, generation_run_id=run_id, intent_id=other_intent,
                observation_mode="live", rows=[_observation(governed_variant_id="gvar_" + "b" * 64)])


def test_the_outbox_carries_the_same_lineage_trigger(conn) -> None:
    _, run_id = _seed_run(conn, "outbox_lineage")
    other_intent, _ = _seed_run(conn, "outbox_lineage_other")
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.transaction():
            enqueue_governed_telemetry(conn, generation_run_id=run_id, intent_id=other_intent,
                                       frozen_inputs={"target_entity": "party"})


# ── mode-inclusive idempotency ─────────────────────────────────────────────────────────────────


def test_same_variant_in_both_modes_is_two_rows_and_a_replay_is_one(conn) -> None:
    intent_id, run_id = _seed_run(conn, "idem")
    live = record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                        observation_mode="live", rows=[_observation()])
    telemetry = record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                             observation_mode="telemetry", rows=[_observation()])
    assert live[0] != telemetry[0]
    replay = record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                          observation_mode="live", rows=[_observation()])
    assert replay == live, "a replay within one mode must resolve to the SAME row, not a new one"
    total = conn.execute(
        "SELECT count(*) FROM governed_planning_observation WHERE generation_run_id = %s",
        (run_id,)).fetchone()[0]
    assert total == 2


# ── demand identity ────────────────────────────────────────────────────────────────────────────


def test_position_table_ref_moves_the_demand_identity(conn) -> None:
    intent_id, run_id = _seed_run(conn, "demand_identity")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    demand_ids = record_bridge_demand(conn, observation_id=observation_id, rejections=[
        _rejection(position_table_ref="core_banking.party"),
        _rejection(position_table_ref="core_banking.party_v2"),
    ])
    assert len(demand_ids) == 2 and demand_ids[0] != demand_ids[1]
    hashes = [r[0] for r in conn.execute(
        "SELECT demand_identity_hash FROM bridge_demand_observation WHERE observation_id = %s "
        "ORDER BY demand_identity_hash", (observation_id,)).fetchall()]
    assert len(set(hashes)) == 2
    assert all(len(h) == 64 for h in hashes), "the FULL sha256 hex, never a prefix"

    expected = hashlib.sha256("|".join((
        "r" * 64, "party_owns_account", "1.0.0", "party", "account", "core_banking",
        "core_banking.party", "0", "unsanctioned_bridge", GRAPH_VERSION, PLANNER_VERSION,
    )).encode()).hexdigest()
    assert expected in hashes


def test_the_endpoint_hint_never_enters_identity(conn) -> None:
    intent_id, run_id = _seed_run(conn, "hint_free")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    record_bridge_demand(conn, observation_id=observation_id,
                         rejections=[_rejection(to_endpoint_hint="cards.account_id")])
    record_bridge_demand(conn, observation_id=observation_id,
                         rejections=[_rejection(to_endpoint_hint="loans.account_id")])
    rows = conn.execute(
        "SELECT count(*) FROM bridge_demand_observation WHERE observation_id = %s",
        (observation_id,)).fetchone()[0]
    assert rows == 1, "an advisory hint must not fork the demand's identity"


def test_capacity_verdict_uses_the_reduced_material_and_routes_to_planner_capacity(conn) -> None:
    intent_id, run_id = _seed_run(conn, "capacity")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="bounded_out")])[0]
    record_bridge_demand(conn, observation_id=observation_id, rejections=[
        _rejection(verdict="bounded_out_max_bridges"),
        # the same capacity verdict with DIFFERENT hop fields is the SAME demand: capacity has no
        # unmet hop by design, so the hop fields are not identity material.
        _rejection(verdict="bounded_out_max_bridges", relationship_id="other",
                   position_table_ref="core_banking.other", hop_index=7),
    ])
    rows = conn.execute(
        "SELECT demand_queue, demand_identity_hash, relationship_id, position_table_ref, hop_index "
        "FROM bridge_demand_observation WHERE observation_id = %s", (observation_id,)).fetchall()
    assert len(rows) == 1
    queue, identity_hash, relationship_id, table_ref, hop_index = rows[0]
    assert queue == "planner_capacity"
    assert (relationship_id, table_ref, hop_index) == ("", "", -1), "hop fields stay at defaults"
    assert identity_hash == hashlib.sha256("|".join((
        "r" * 64, "bounded_out_max_bridges", "core_banking", GRAPH_VERSION, PLANNER_VERSION,
    )).encode()).hexdigest()


def test_queue_routing_covers_every_demand_verdict(conn) -> None:
    assert DEMAND_VERDICT_QUEUES == {
        "unsanctioned_bridge": "bridge_demand",
        "missing_realization": "realization_gap",
        "bounded_out_max_bridges": "planner_capacity",
        "bounded_out_max_frontier_states": "planner_capacity",
    }


def test_a_non_demand_verdict_yields_no_row_and_the_check_refuses_it_in_sql(conn) -> None:
    intent_id, run_id = _seed_run(conn, "unknown_verdict")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    # the STORE filters by the closed verdict set: a shape it does not recognise yields no row
    assert record_bridge_demand(conn, observation_id=observation_id,
                                rejections=[_rejection(verdict="concept_mismatch")]) == ()
    # and were one ever to reach SQL, the CHECK — not a silent skip — is what refuses it
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO bridge_demand_observation (demand_id, observation_id, demand_queue, "
                "demand_identity_hash, recipe_revision_hash, verdict) "
                "VALUES ('bdo_x', %s, 'bridge_demand', 'h', 'r', 'concept_mismatch')",
                (observation_id,))


def test_the_queue_must_match_the_verdict_in_sql(conn) -> None:
    intent_id, run_id = _seed_run(conn, "routing_check")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO bridge_demand_observation (demand_id, observation_id, demand_queue, "
                "demand_identity_hash, recipe_revision_hash, verdict) "
                "VALUES ('bdo_y', %s, 'planner_capacity', 'h', 'r', 'unsanctioned_bridge')",
                (observation_id,))


# ── append-only physics ────────────────────────────────────────────────────────────────────────


def _seed_all_three(conn, suffix: str) -> tuple[str, str, str]:
    intent_id, run_id = _seed_run(conn, suffix)
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    demand_id = record_bridge_demand(conn, observation_id=observation_id,
                                     rejections=[_rejection()])[0]
    event_id = "gre_seed_" + suffix
    conn.execute(
        "INSERT INTO governed_plan_review_event (event_id, observation_id, reviewer, "
        "reviewer_role, decision, rationale) VALUES (%s, %s, 'sme', 'data_steward', 'approved', 'ok')",
        (event_id, observation_id))
    return observation_id, demand_id, event_id


@pytest.mark.parametrize("table", ["governed_planning_observation", "bridge_demand_observation",
                                   "governed_plan_review_event"])
def test_update_and_delete_refuse_on_every_1120_table(conn, table: str) -> None:
    _seed_all_three(conn, "appendonly_" + hashlib.sha256(table.encode()).hexdigest()[:8])
    for statement in (f"UPDATE {table} SET recorded_at = now()", f"DELETE FROM {table}"):
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            with conn.transaction():
                conn.execute(statement)
        assert "append-only" in str(excinfo.value) and table in str(excinfo.value)


@pytest.mark.parametrize("statement", [
    "TRUNCATE governed_planning_observation CASCADE",   # CASCADE: two children reference it
    "TRUNCATE bridge_demand_observation",
    "TRUNCATE governed_plan_review_event",
])
def test_truncate_refuses_on_every_1120_table(conn, statement: str) -> None:
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with conn.transaction():
            conn.execute(statement)
    assert "append-only" in str(excinfo.value)


def test_the_outbox_accepts_status_updates(conn) -> None:
    """1121's deliberate exception: status/lease are worker coordination state, not the record."""
    _, run_id = _seed_run(conn, "outbox_mutable", with_intent=False)
    work_item_id = enqueue_governed_telemetry(conn, generation_run_id=run_id, intent_id=None,
                                              frozen_inputs={"target_entity": "party"})
    conn.execute("UPDATE governed_telemetry_outbox SET status = 'failed' WHERE work_item_id = %s",
                 (work_item_id,))
    assert conn.execute("SELECT status FROM governed_telemetry_outbox WHERE work_item_id = %s",
                        (work_item_id,)).fetchone()[0] == "failed"


# ── the outbox lease lifecycle ─────────────────────────────────────────────────────────────────


def test_lease_lifecycle(conn) -> None:
    intent_id, run_id = _seed_run(conn, "lease")
    work_item_id = enqueue_governed_telemetry(
        conn, generation_run_id=run_id, intent_id=intent_id,
        frozen_inputs={"target_entity": "party", "anchor_catalog_source": "core_banking"})
    queued = conn.execute(
        "SELECT status, attempt_count, lease_owner FROM governed_telemetry_outbox "
        "WHERE work_item_id = %s", (work_item_id,)).fetchone()
    assert queued == ("queued", 0, "")

    claimed = claim_telemetry_work(conn, owner="worker-a", lease_seconds=300)
    assert claimed is not None
    assert claimed["work_item_id"] == work_item_id
    assert claimed["status"] == "leased"
    assert claimed["lease_owner"] == "worker-a"
    assert claimed["attempt_count"] == 1
    assert claimed["lease_expires_at"] is not None
    assert claimed["frozen_inputs"]["target_entity"] == "party"

    assert claim_telemetry_work(conn, owner="worker-b") is None, "a live lease is exclusive"

    # expiry: a row already leased with a lease in the past is reclaimable
    conn.execute(
        "UPDATE governed_telemetry_outbox SET lease_expires_at = now() - interval '1 hour' "
        "WHERE work_item_id = %s", (work_item_id,))
    reclaimed = claim_telemetry_work(conn, owner="worker-b")
    assert reclaimed is not None
    assert reclaimed["work_item_id"] == work_item_id
    assert reclaimed["lease_owner"] == "worker-b"
    assert reclaimed["attempt_count"] == 2, "a reclaim is another attempt, counted"

    complete_telemetry_work(conn, work_item_id=work_item_id, ok=True)
    done = conn.execute(
        "SELECT status, completed_at, last_error, lease_owner FROM governed_telemetry_outbox "
        "WHERE work_item_id = %s", (work_item_id,)).fetchone()
    assert done[0] == "done" and done[1] is not None and done[2] == "" and done[3] == ""
    assert claim_telemetry_work(conn, owner="worker-c") is None


def test_completing_with_a_failure_records_the_error(conn) -> None:
    _, run_id = _seed_run(conn, "lease_fail", with_intent=False)
    work_item_id = enqueue_governed_telemetry(conn, generation_run_id=run_id, intent_id=None,
                                              frozen_inputs={})
    claim_telemetry_work(conn, owner="worker-a")
    complete_telemetry_work(conn, work_item_id=work_item_id, ok=False, error="planner exploded")
    row = conn.execute("SELECT status, last_error FROM governed_telemetry_outbox "
                       "WHERE work_item_id = %s", (work_item_id,)).fetchone()
    assert row == ("failed", "planner exploded")


# ── the rollups ────────────────────────────────────────────────────────────────────────────────


def _seed_demand_group(conn, suffix: str, *, relationship_id: str, mode: str = "live",
                       table_ref: str = "core_banking.party", recorded_at=None,
                       intent_id: str | None = None, run_id: str | None = None) -> str:
    """One observation plus one bridge demand. ``recorded_at`` is supplied at INSERT time (never
    by a later UPDATE — the table is append-only and stays that way for its own tests)."""
    if run_id is None:
        intent_id, run_id = _seed_run(conn, suffix)
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode=mode,
        rows=[_observation(resolution_status="unresolved",
                           governed_variant_id="gvar_" + hashlib.sha256(
                               suffix.encode()).hexdigest())])[0]
    if recorded_at is None:
        record_bridge_demand(conn, observation_id=observation_id, rejections=[
            _rejection(relationship_id=relationship_id, position_table_ref=table_ref)])
        return observation_id
    conn.execute(
        "INSERT INTO bridge_demand_observation (demand_id, observation_id, demand_queue, "
        "demand_identity_hash, recipe_revision_hash, relationship_id, relationship_version, "
        "from_entity, to_entity, position_catalog, position_table_ref, hop_index, verdict, "
        "realizers, near_side_key_refs, recorded_at) "
        "VALUES (%s, %s, 'bridge_demand', %s, %s, %s, '1.0.0', 'party', 'account', "
        "'core_banking', %s, 0, 'unsanctioned_bridge', "
        "'[{\"table_ref\": \"core_banking.party_account\"}]'::jsonb, "
        "'[{\"column\": \"party_id\"}]'::jsonb, %s)",
        (f"bdo_seed_{suffix}", observation_id, hashlib.sha256(suffix.encode()).hexdigest(),
         "r" * 64, relationship_id, table_ref, recorded_at))
    return observation_id


def test_rollup_aggregates_the_full_population_then_limits(conn) -> None:
    for index in range(5):
        _seed_demand_group(conn, f"rollup_{index}", relationship_id=f"rel_{index:02d}")
    page1 = observation_queues(conn, limit=2)
    groups1 = page1["queues"]["bridge_demand"]
    assert len(groups1) == 2
    assert [g["relationship_id"] for g in groups1] == ["rel_00", "rel_01"]
    assert page1["next_cursor"] is not None
    assert isinstance(page1["as_of"], datetime), "as_of is echoed, resolved when not supplied"

    seen = list(groups1)
    cursor = page1["next_cursor"]
    while cursor is not None:
        page = observation_queues(conn, limit=2, cursor=cursor)
        seen.extend(page["queues"]["bridge_demand"])
        cursor = page["next_cursor"]
    assert [g["relationship_id"] for g in seen] == [f"rel_{i:02d}" for i in range(5)]
    # a group absent from page 1 still carries its own correct totals
    assert all(g["observations"] == 1 for g in seen)
    assert seen[4]["nested"][0]["position_table_ref"] == "core_banking.party"


def test_rollup_counts_intents_and_runs_separately(conn) -> None:
    """The retry-gaming pin: five runs of ONE intent is one intent's demand, not five."""
    intent_id = "s1b1_int_retry"
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    for index in range(5):
        run_id = f"s1b1_run_retry_{index}"
        conn.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
                     "VALUES (%s, %s, '{}'::jsonb)", (run_id, intent_id))
        _seed_demand_group(conn, f"retry_{index}", relationship_id="rel_retry",
                           intent_id=intent_id, run_id=run_id)
    group = observation_queues(conn)["queues"]["bridge_demand"][0]
    assert group["distinct_intents"] == 1
    assert group["distinct_runs"] == 5
    assert group["observations"] == 5


def test_rollup_splits_recent_from_historical_and_counts_per_mode(conn) -> None:
    now = datetime.now(UTC)
    _seed_demand_group(conn, "split_recent", relationship_id="rel_split",
                       recorded_at=now - timedelta(days=1))
    _seed_demand_group(conn, "split_old", relationship_id="rel_split", mode="telemetry",
                       recorded_at=now - timedelta(days=30))
    group = observation_queues(conn)["queues"]["bridge_demand"][0]
    assert group["observations"] == 2
    assert group["recent_observations"] == 1
    assert group["historical_observations"] == 1
    assert group["live_observations"] == 1
    assert group["telemetry_observations"] == 1
    assert group["suggested_endpoints"], "realizers + near-side key refs, sampled"
    assert len(group["suggested_endpoints"]) <= 5


def test_rollup_honours_as_of(conn) -> None:
    now = datetime.now(UTC)
    _seed_demand_group(conn, "asof_old", relationship_id="rel_asof",
                       recorded_at=now - timedelta(days=10))
    _seed_demand_group(conn, "asof_new", relationship_id="rel_asof",
                       recorded_at=now - timedelta(days=1))
    as_of = now - timedelta(days=5)
    result = observation_queues(conn, as_of=as_of)
    assert result["as_of"] == as_of
    assert result["queues"]["bridge_demand"][0]["observations"] == 1


def test_rollup_separates_the_three_queues(conn) -> None:
    intent_id, run_id = _seed_run(conn, "three_queues")
    observation_id = record_planning_observations(
        conn, generation_run_id=run_id, intent_id=intent_id, observation_mode="live",
        rows=[_observation(resolution_status="unresolved")])[0]
    record_bridge_demand(conn, observation_id=observation_id, rejections=[
        _rejection(verdict="unsanctioned_bridge"),
        _rejection(verdict="missing_realization"),
        _rejection(verdict="bounded_out_max_frontier_states"),
    ])
    queues = observation_queues(conn)["queues"]
    assert set(queues) == {"bridge_demand", "realization_gap", "planner_capacity"}
    assert len(queues["bridge_demand"]) == 1
    assert len(queues["realization_gap"]) == 1
    assert len(queues["planner_capacity"]) == 1


def test_resolution_summary_reports_per_origin_and_per_anchor_rates(conn) -> None:
    intent_id, run_id = _seed_run(conn, "summary")
    rows = [
        _observation(definition_origin="recipe_v2", resolution_status="resolved",
                     anchor_catalog_source="core_banking",
                     governed_variant_id="gvar_" + "1" * 64),
        _observation(definition_origin="recipe_v2", resolution_status="unresolved",
                     anchor_catalog_source="core_banking",
                     governed_variant_id="gvar_" + "2" * 64),
        _observation(definition_origin="llm_intent", resolution_status="resolved",
                     anchor_catalog_source="cards", recipe_id=None,
                     canonical_definition_id="intent:abc",
                     governed_variant_id="gvar_" + "3" * 64),
        _observation(definition_origin="llm_intent", resolution_status="bounded_out",
                     anchor_catalog_source="cards", recipe_id=None,
                     canonical_definition_id="intent:abc",
                     governed_variant_id="gvar_" + "4" * 64),
        _observation(definition_origin="llm_intent", resolution_status="unresolved",
                     anchor_catalog_source="cards", recipe_id=None,
                     canonical_definition_id="intent:abc",
                     governed_variant_id="gvar_" + "5" * 64),
    ]
    record_planning_observations(conn, generation_run_id=run_id, intent_id=intent_id,
                                 observation_mode="live", rows=rows)
    summary = resolution_summary(conn)
    by_origin = {r["definition_origin"]: r for r in summary["by_origin"]}
    assert by_origin["recipe_v2"]["observations"] == 2
    assert by_origin["recipe_v2"]["resolved"] == 1
    assert by_origin["recipe_v2"]["resolution_rate"] == pytest.approx(0.5)
    assert by_origin["llm_intent"]["observations"] == 3
    assert by_origin["llm_intent"]["resolution_rate"] == pytest.approx(1 / 3)

    counts = {(r["definition_origin"], r["resolution_status"]): r["observations"]
              for r in summary["by_origin_status"]}
    assert counts[("recipe_v2", "resolved")] == 1
    assert counts[("recipe_v2", "unresolved")] == 1
    assert counts[("llm_intent", "bounded_out")] == 1

    by_anchor = {r["anchor_catalog_source"]: r for r in summary["by_anchor_catalog"]}
    assert by_anchor["core_banking"]["resolution_rate"] == pytest.approx(0.5)
    assert by_anchor["cards"]["resolution_rate"] == pytest.approx(1 / 3)
    assert summary["totals"]["observations"] == 5
    assert summary["totals"]["resolved"] == 2
