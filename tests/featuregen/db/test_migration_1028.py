from __future__ import annotations

import psycopg
import pytest


def _intent_and_run(conn, suffix: str) -> tuple[str, str]:
    intent_id = f"r6_intent_{suffix}"
    run_id = f"r6_run_{suffix}"
    conn.execute(
        "INSERT INTO contract_intent "
        "(intent_id, hypothesis, redacted_hypothesis, intake_mode, actor) "
        "VALUES (%s, 'h', 'h', 'hypothesis', '\"tester\"'::jsonb)",
        (intent_id,),
    )
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb)",
        (run_id, intent_id),
    )
    return intent_id, run_id


def test_feedback_supersession_run_fk_is_installed(conn) -> None:
    row = conn.execute(
        "SELECT condeferrable, condeferred FROM pg_constraint "
        "WHERE conname = 'confirmed_generation_scope_superseded_run_fk'"
    ).fetchone()
    assert row == (True, True)


def test_feedback_supersession_requires_scope_and_run_to_match(conn) -> None:
    intent_id, prior_run = _intent_and_run(conn, "prior")
    current_run = "r6_run_current"
    wrong_run = "r6_run_wrong"
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor) "
        "VALUES (%s, %s, '{}'::jsonb), (%s, %s, '{}'::jsonb)",
        (current_run, intent_id, wrong_run, intent_id),
    )
    conn.execute(
        "INSERT INTO confirmed_generation_scope "
        "(scope_id, intent_id, generation_run_id, expansion, scope_mode, "
        "confirmation_source, confirmed_by) "
        "VALUES ('r6_prior_scope', %s, %s, 'exact', 'scoped', 'user_confirmed', 'tester')",
        (intent_id, prior_run),
    )

    with pytest.raises(psycopg.errors.RaiseException, match="feedback supersession lineage"):
        with conn.transaction():
            conn.execute(
                "INSERT INTO confirmed_generation_scope "
                "(scope_id, intent_id, generation_run_id, supersedes_scope_id, "
                "supersedes_generation_run_id, expansion, scope_mode, "
                "confirmation_source, confirmed_by) "
                "VALUES ('r6_bad_feedback_scope', %s, %s, 'r6_prior_scope', %s, "
                "'exact', 'scoped', 'user_feedback', 'tester')",
                (intent_id, current_run, wrong_run),
            )
            conn.execute("SET CONSTRAINTS ALL IMMEDIATE")
