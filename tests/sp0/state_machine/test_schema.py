from __future__ import annotations

import psycopg
import pytest


def test_run_transition_table_has_contract_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'run_transition_table'"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {
        "table_version", "from_state", "to_state", "trigger", "guard_expr",
        "guard_inputs", "precedence", "on_success", "on_guard_fail",
    } <= cols


def test_feature_lifecycle_table_inherits_columns(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'feature_lifecycle_table'"
        )
        cols = {r[0] for r in cur.fetchall()}
    assert {
        "table_version", "from_state", "to_state", "trigger", "guard_expr",
        "guard_inputs", "precedence", "on_success", "on_guard_fail",
    } <= cols


def test_feature_lifecycle_table_pk_enforced(conn) -> None:
    ins = (
        "INSERT INTO feature_lifecycle_table "
        "(table_version, from_state, to_state, trigger, precedence, on_success) "
        "VALUES (1, 'A', 'B', 'T', 100, '{}'::jsonb)"
    )
    with conn.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)
