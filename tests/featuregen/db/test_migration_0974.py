from __future__ import annotations

import psycopg
import pytest

# Migrations are applied once per session by the root `_dsn` fixture; the `conn` fixture is a real
# PG connection whose writes are rolled back on teardown. These tests query the live catalog
# (information_schema / pg_constraint) and insert minimal rows directly with SQL — they do NOT
# depend on the Task-2 persistence module, which does not exist yet.

_TABLES = (
    "intent_recognition_attempt",
    "confirmed_generation_scope",
    "confirmed_scope_use_case",
)


def _unique_column_sets(conn, table: str) -> set[tuple[str, ...]]:
    """Every UNIQUE constraint on `table`, as a set of ordered (col, ...) tuples."""
    rows = conn.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord)
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
         WHERE t.relname = %s AND c.contype = 'u'
         GROUP BY c.oid
        """,
        (table,),
    ).fetchall()
    return {tuple(r[0]) for r in rows}


def _insert_attempt(conn, *, recognition_id: str, intent_id: str, input_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO intent_recognition_attempt
            (recognition_id, intent_id, input_hash, status, taxonomy_version,
             applicability_mapping_version, recognizer_model_id, prompt_version,
             recipe_registry_version)
        VALUES (%s, %s, %s, 'classified', 'tax_v1', 'map_v1', 'model_v1', 'prompt_v1', 'reg_v1')
        """,
        (recognition_id, intent_id, input_hash),
    )


def _insert_scope(conn, *, scope_id: str, intent_id: str, generation_run_id: str) -> None:
    conn.execute(
        """
        INSERT INTO confirmed_generation_scope
            (scope_id, intent_id, generation_run_id, expansion, scope_mode,
             confirmation_source, confirmed_by)
        VALUES (%s, %s, %s, 'EXACT', 'scoped', 'user_confirmed', 'actor_1')
        """,
        (scope_id, intent_id, generation_run_id),
    )


def test_three_tables_exist(conn) -> None:
    for table in _TABLES:
        reg = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0]
        assert reg is not None, f"missing table {table}"


def test_recognition_attempt_has_no_generation_run_id(conn) -> None:
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            ("intent_recognition_attempt",),
        ).fetchall()
    }
    # Recognition precedes generation — the attempt must not carry a run id.
    assert "generation_run_id" not in cols
    assert {"candidates", "taxonomy_version", "applicability_mapping_version",
            "recognizer_model_id", "prompt_version", "recipe_registry_version"} <= cols


def test_unique_constraints_exist(conn) -> None:
    assert ("intent_id", "input_hash") in _unique_column_sets(conn, "intent_recognition_attempt")
    assert ("generation_run_id",) in _unique_column_sets(conn, "confirmed_generation_scope")


def test_child_fk_to_confirmed_scope_exists(conn) -> None:
    row = conn.execute(
        """
        SELECT 1
          FROM pg_constraint c
          JOIN pg_class child  ON child.oid  = c.conrelid
          JOIN pg_class parent ON parent.oid = c.confrelid
         WHERE c.contype = 'f'
           AND child.relname  = 'confirmed_scope_use_case'
           AND parent.relname = 'confirmed_generation_scope'
        """
    ).fetchone()
    assert row is not None


def test_duplicate_generation_run_id_rejected(conn) -> None:
    _insert_scope(conn, scope_id="scope_a", intent_id="intent_1", generation_run_id="run_1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        # A second scope claiming the same run violates the canonical run->scope linkage.
        _insert_scope(conn, scope_id="scope_b", intent_id="intent_1", generation_run_id="run_1")


def test_duplicate_intent_input_hash_rejected(conn) -> None:
    _insert_attempt(conn, recognition_id="rec_a", intent_id="intent_1", input_hash="hash_1")
    with pytest.raises(psycopg.errors.UniqueViolation):
        # Same intent + redacted input must map to the same idempotent attempt.
        _insert_attempt(conn, recognition_id="rec_b", intent_id="intent_1", input_hash="hash_1")


def test_child_check_rejects_bad_relationship(conn) -> None:
    _insert_scope(conn, scope_id="scope_r", intent_id="intent_1", generation_run_id="run_r")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO confirmed_scope_use_case (scope_id, use_case_id, relationship, origin)
            VALUES ('scope_r', 'uc_1', 'bogus', 'llm_proposed')
            """
        )


def test_child_check_rejects_bad_origin(conn) -> None:
    _insert_scope(conn, scope_id="scope_o", intent_id="intent_1", generation_run_id="run_o")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            """
            INSERT INTO confirmed_scope_use_case (scope_id, use_case_id, relationship, origin)
            VALUES ('scope_o', 'uc_1', 'primary', 'bogus')
            """
        )


def test_child_accepts_valid_row(conn) -> None:
    _insert_scope(conn, scope_id="scope_ok", intent_id="intent_1", generation_run_id="run_ok")
    conn.execute(
        """
        INSERT INTO confirmed_scope_use_case (scope_id, use_case_id, relationship, origin, display_order)
        VALUES ('scope_ok', 'uc_1', 'primary', 'llm_proposed', 0)
        """
    )
    row = conn.execute(
        "SELECT relationship, origin, display_order FROM confirmed_scope_use_case "
        "WHERE scope_id = 'scope_ok' AND use_case_id = 'uc_1'"
    ).fetchone()
    assert row == ("primary", "llm_proposed", 0)
