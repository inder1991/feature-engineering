from __future__ import annotations

import psycopg
import pytest


def _intent(conn, intent_id: str) -> None:
    conn.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, redacted_hypothesis, intake_mode, actor) "
        "VALUES (%s, 'h', 'h', 'hypothesis', '\"tester\"'::jsonb)",
        (intent_id,),
    )


def _run(conn, run_id: str, intent_id: str) -> None:
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)",
        (run_id, intent_id),
    )


def test_r5_authority_foreign_keys_are_installed(conn) -> None:
    expected = {
        "confirmed_generation_scope_intent_fk",
        "confirmed_generation_scope_run_fk",
        "contract_considered_revision_run_fk",
        "contract_considered_revision_snapshot_fk",
        "recipe_formula_shadow_manifest_intent_fk",
        "recipe_formula_shadow_manifest_revision_fk",
        "recipe_formula_shadow_observation_intent_fk",
        "recipe_formula_shadow_observation_revision_fk",
        "recipe_formula_shadow_observation_snapshot_fk",
        "recipe_formula_shadow_observation_authoring_run_fk",
        "recipe_formula_shadow_work_item_intent_fk",
        "recipe_formula_shadow_work_item_revision_fk",
        "recipe_formula_shadow_work_item_snapshot_fk",
    }
    rows = conn.execute(
        "SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
        (list(expected),),
    ).fetchall()
    assert {row[0] for row in rows} == expected


def test_r5_audit_refuses_cross_intent_considered_revision(conn) -> None:
    _intent(conn, "r5_intent_a")
    _intent(conn, "r5_intent_b")
    _run(conn, "r5_run_a", "r5_intent_a")

    with pytest.raises(psycopg.errors.RaiseException, match="considered revisions"):
        with conn.transaction():
            conn.execute(
                "INSERT INTO contract_considered_revision "
                "(considered_revision_id, intent_id, generation_run_id, considered_json, "
                "considered_content_hash, canonicalization_version) "
                "VALUES ('r5_bad_revision', 'r5_intent_b', 'r5_run_a', '{}'::jsonb, 'h', 'v1')"
            )
            conn.execute("SELECT featuregen_assert_generation_lineage_integrity()")


def test_r5_deferred_trigger_refuses_cross_intent_recognition(conn) -> None:
    _intent(conn, "r5_scope_intent_a")
    _intent(conn, "r5_scope_intent_b")
    _run(conn, "r5_scope_run", "r5_scope_intent_a")
    conn.execute(
        "INSERT INTO intent_recognition_attempt "
        "(recognition_id, intent_id, input_hash, status, taxonomy_version, "
        "applicability_mapping_version, recognizer_model_id, prompt_version, "
        "recipe_registry_version, created_by) "
        "VALUES ('r5_cross_recognition', 'r5_scope_intent_b', 'h', 'unscoped', "
        "'t', 'a', 'm', 'p', 'r', '{}'::jsonb)"
    )

    with pytest.raises(psycopg.errors.RaiseException, match="inconsistent generation lineage"):
        with conn.transaction():
            conn.execute(
                "INSERT INTO confirmed_generation_scope "
                "(scope_id, intent_id, generation_run_id, recognition_id, expansion, "
                "scope_mode, confirmation_source, confirmed_by) "
                "VALUES ('r5_bad_scope', 'r5_scope_intent_a', 'r5_scope_run', "
                "'r5_cross_recognition', 'exact', 'scoped', 'user_confirmed', 'tester')"
            )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")


def test_r5_deferred_fk_refuses_orphan_snapshot(conn) -> None:
    _intent(conn, "r5_snapshot_intent")
    _run(conn, "r5_snapshot_run", "r5_snapshot_intent")

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with conn.transaction():
            conn.execute(
                "INSERT INTO contract_considered_revision "
                "(considered_revision_id, intent_id, generation_run_id, metadata_snapshot_id, "
                "considered_json, considered_content_hash, canonicalization_version) "
                "VALUES ('r5_orphan_snapshot_revision', 'r5_snapshot_intent', "
                "'r5_snapshot_run', 'missing_snapshot', '{}'::jsonb, 'h', 'v1')"
            )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
