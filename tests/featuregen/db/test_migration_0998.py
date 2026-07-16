from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

# 0998 adds the Phase-3B.4 shadow-telemetry store (dispatch manifest + run_result + plan_observation),
# append-only/WORM with a full CHECK surface: every enum column is constrained, JSON columns are
# jsonb_typeof='object', run_result carries count-consistency + incomplete_reason-scope CHECKs, and
# plan_observation carries the is_compiled cross-field guards. Migrations are applied once per session;
# the `conn` fixture rolls each test's writes back. WORM REVOKE is a no-op under the superuser test
# cluster (a superuser bypasses grants), so these tests assert the migration ran + the CHECKs bite,
# not the privilege enforcement (which relies on prod running under the non-superuser featuregen_app role).


def _dispatch(conn, run_id: str = "grun_1") -> None:
    conn.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash, "
        "expected_count, invocation_predicate, compile_flag, telemetry_flag, applicability_version, "
        "producer_commit, compiler_versions, compiler_versions_hash, payload_schema_version) "
        "VALUES (%s, %s, 'rh', 1, 'entity_scoped', true, true, '1.0.0', 'abc', %s, 'vh', '1.0.0')",
        (run_id, ["r1"], Jsonb({"planner": "1.0.0"})))


def _run_result(conn, *, run_id: str = "grun_1", recipe_id: str = "r1", planner_outcome: str = "compiled",
                compile_status: str = "complete", incomplete_reason: str | None = None,
                path_resolved_eligible: int = 2, compiled_count: int = 2, skipped_count: int = 0,
                capture_status: str = "persisted", contract_result_status: str | None = "resolved",
                bounding=None) -> None:
    conn.execute(
        "INSERT INTO planner_shadow_run_result (generation_run_id, recipe_id, planner_outcome, "
        "compile_status, incomplete_reason, path_resolved_eligible, compiled_count, skipped_count, "
        "capture_status, contract_result_status, bounding, payload_schema_version) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, '1.0.0')",
        (run_id, recipe_id, planner_outcome, compile_status, incomplete_reason, path_resolved_eligible,
         compiled_count, skipped_count, capture_status, contract_result_status,
         Jsonb(bounding if bounding is not None else {"plans_truncated": False})))


def _observation(conn, *, physical_plan_id: str = "bp_1", path_resolution_status: str = "source_to_target_resolved",
                 is_compiled: bool = True, contract_id: str | None = "cc_1",
                 contract_input_hash: str | None = "cih", contract_resolution_status: str | None = "resolved",
                 declaration_status: str | None = "resolved", tier: str = "tier_2_one_bridge",
                 declarations=None, replay_stamp=None) -> None:
    dec = Jsonb(declarations) if declarations is not None else (Jsonb({"k": 1}) if is_compiled else None)
    stamp = Jsonb(replay_stamp) if replay_stamp is not None else (Jsonb({"s": 1}) if is_compiled else None)
    conn.execute(
        "INSERT INTO planner_shadow_plan_observation (generation_run_id, recipe_id, physical_plan_id, "
        "path_resolution_status, is_compiled, contract_id, contract_input_hash, contract_resolution_status, "
        "declaration_status, contract_reason_codes, bridge_count, tier, preference_rank, declarations, "
        "declarations_output_hash, replay_stamp, payload_schema_version) "
        "VALUES ('grun_1', 'r1', %s, %s, %s, %s, %s, %s, %s, '{}', 1, %s, 0, %s, 'oh', %s, '1.0.0')",
        (physical_plan_id, path_resolution_status, is_compiled, contract_id, contract_input_hash,
         contract_resolution_status, declaration_status, tier, dec, stamp))


def _rejected(conn, insert, /, *args, **kwargs) -> None:
    conn.execute("SELECT 1")
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        insert(conn, *args, **kwargs)


def test_migration_created_the_three_tables(conn) -> None:
    for t in ("planner_shadow_dispatch", "planner_shadow_run_result", "planner_shadow_plan_observation"):
        assert conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0] is not None


def test_valid_full_chain_accepted(conn) -> None:
    _dispatch(conn)
    _run_result(conn)
    _observation(conn)


# ── run_result CHECKs ──
def test_count_mismatch_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, compiled_count=1, skipped_count=0, path_resolved_eligible=2)


def test_incomplete_reason_set_but_status_not_incomplete_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, compile_status="complete", incomplete_reason="budget_time")


def test_incomplete_status_without_reason_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, compile_status="incomplete", incomplete_reason=None)


def test_bad_planner_outcome_enum_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, planner_outcome="banana")


def test_bad_compile_status_enum_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, compile_status="halfway")


def test_bad_contract_result_status_enum_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, contract_result_status="mostly_resolved")


def test_non_object_bounding_json_rejected(conn) -> None:
    _dispatch(conn)
    _rejected(conn, _run_result, bounding=[1, 2, 3])   # a JSON array, not an object


# ── plan_observation CHECKs ──
def _obs_ctx(conn) -> None:
    _dispatch(conn)
    _run_result(conn)


def test_is_compiled_with_null_hash_rejected(conn) -> None:
    _obs_ctx(conn)
    _rejected(conn, _observation, is_compiled=True, contract_input_hash=None)


def test_uncompiled_with_contract_id_rejected(conn) -> None:
    _obs_ctx(conn)
    # is_compiled False must have NULL contract_id (and NULL hash/stamp via the other guards)
    _rejected(conn, _observation, is_compiled=False, contract_id="cc_x",
              contract_input_hash=None, replay_stamp=None)


def test_bad_path_resolution_status_enum_rejected(conn) -> None:
    _obs_ctx(conn)
    _rejected(conn, _observation, path_resolution_status="sideways")


def test_bad_tier_enum_rejected(conn) -> None:
    _obs_ctx(conn)
    _rejected(conn, _observation, tier="tier_9")


def test_bad_contract_resolution_status_enum_rejected(conn) -> None:
    _obs_ctx(conn)
    _rejected(conn, _observation, contract_resolution_status="kind_of_resolved")


def test_non_object_declarations_json_rejected(conn) -> None:
    _obs_ctx(conn)
    _rejected(conn, _observation, declarations=[1, 2])   # array, not object


def test_uncompiled_observation_accepted(conn) -> None:
    # a tier-1 / rejected candidate: is_compiled False with all compile-only fields NULL is valid
    _obs_ctx(conn)
    _observation(conn, physical_plan_id="bp_tier1", path_resolution_status="ingredient_binding_only",
                 is_compiled=False, contract_id=None, contract_input_hash=None,
                 contract_resolution_status=None, declaration_status=None, tier="tier_1_single_catalog",
                 declarations=None, replay_stamp=None)
