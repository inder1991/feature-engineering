from __future__ import annotations

import psycopg
import pytest

from featuregen.db.migrations import apply_migrations

_EXPECTED_COLS = {
    "llm_call_ref", "feature_contract_id", "run_id", "task", "provider", "model",
    "prompt_id", "prompt_version", "output_schema_id", "output_schema_version",
    "generation_settings", "redaction_version", "input_hash", "redacted_input",
    "input_redaction", "raw_output", "validation_result", "repair_attempts",
    "latency_ms", "cost_metadata", "created_at", "created_by",
}


def test_llm_call_table_exists_with_full_provenance_columns(conn):
    apply_migrations(conn)
    reg = conn.execute("SELECT to_regclass('public.llm_call')").fetchone()[0]
    assert reg is not None
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='llm_call'"
        ).fetchall()
    }
    assert _EXPECTED_COLS <= cols


def test_feature_contract_projection_checkpoint_seeded(conn):
    apply_migrations(conn)
    row = conn.execute(
        "SELECT projection_name, checkpoint_seq, head_seq, is_analytics "
        "FROM projection_checkpoints WHERE projection_name='feature_contract'"
    ).fetchone()
    assert row == ("feature_contract", 0, 0, False)


def test_llm_call_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    n = conn.execute(
        "SELECT count(*) FROM projection_checkpoints WHERE projection_name='feature_contract'"
    ).fetchone()[0]
    assert n == 1  # ON CONFLICT DO NOTHING — re-apply does not duplicate the row
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='llm_call_idem_idx'"
    ).fetchone()
    assert idx is not None


def _insert_llm_call(conn, call_ref: str) -> None:
    conn.execute(
        "INSERT INTO llm_call (llm_call_ref, run_id, task, provider, model, prompt_id, "
        "prompt_version, output_schema_id, output_schema_version, redaction_version, "
        "input_hash, redacted_input, created_by) VALUES "
        "(%s, 'run_wo', 'structure_intent', 'fake', 'fake-1', 'p1', 1, 's1', 1, "
        "'rv1', 'sha256:h', '{}'::jsonb, '{}'::jsonb)",
        (call_ref,),
    )


def test_llm_call_is_write_once_no_update(conn):
    # SENSITIVE / governance-retained record store — physically immutable, mirroring SP-0's
    # documents_write_once / feature_versions_write_once trigger.
    apply_migrations(conn)
    _insert_llm_call(conn, "call_wo_update")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("UPDATE llm_call SET model='x' WHERE llm_call_ref='call_wo_update'")


def test_llm_call_is_write_once_no_delete(conn):
    apply_migrations(conn)
    _insert_llm_call(conn, "call_wo_delete")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"), conn.transaction():
        conn.execute("DELETE FROM llm_call WHERE llm_call_ref='call_wo_delete'")
