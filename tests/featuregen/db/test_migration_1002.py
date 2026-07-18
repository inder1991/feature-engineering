from __future__ import annotations

import psycopg
import pytest


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)).fetchall()}


def _evaluation(db, evaluation_id: str = "e1", *, result: str = "PASS",
                population_report: str = "{}") -> None:
    db.execute(
        "INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
        " gold_set_result, stability_result, version_vector, result, content_hash) VALUES"
        " (%s, '{}', %s, '{}', '{}', '{}', %s, 'h')",
        (evaluation_id, population_report, result))


def _decision(db, decision_id: str = "d1", *, evaluation_id: str = "e1",
              decision: str = "APPROVE", supersedes_decision_id: str | None = None) -> None:
    db.execute(
        "INSERT INTO live_activation_decision (decision_id, evaluation_id, deployment_id,"
        " decision, decided_by, supersedes_decision_id) VALUES (%s, %s, 'dep1', %s, 'admin', %s)",
        (decision_id, evaluation_id, decision, supersedes_decision_id))


def _rejected(db, exc, insert, /, *args, **kwargs) -> None:
    # Mirror test_migration_0999: SELECT 1 forces autobegin so db.transaction() is a SAVEPOINT,
    # not an outermost transaction — each expected rejection rolls back to the savepoint and
    # cannot poison later assertions (nor commit under the rollback-on-teardown fixture).
    db.execute("SELECT 1")
    with pytest.raises(exc), db.transaction():
        insert(db, *args, **kwargs)


def test_1002_creates_both_activation_tables(db):
    assert {"evaluation_id", "telemetry_window", "population_report", "gold_set_result",
            "stability_result", "layer_b_labels", "version_vector", "result", "content_hash",
            "evaluated_at"} <= _cols(db, "enablement_evaluation")
    assert {"decision_id", "evaluation_id", "deployment_id", "decision", "decided_by", "reason",
            "decided_at", "supersedes_decision_id"} <= _cols(db, "live_activation_decision")


def test_1002_result_and_decision_checks(db):
    _evaluation(db, "e1", result="PASS")
    _rejected(db, psycopg.errors.CheckViolation, _evaluation, "e2", result="MAYBE")
    # decision enum CHECK bites too: a valid PASS evaluation satisfies the FK, the enum does not.
    _rejected(db, psycopg.errors.CheckViolation, _decision, "d1", evaluation_id="e1",
              decision="MAYBE")


def test_1002_non_object_jsonb_rejected(db):
    # Content-hashed jsonb columns must be objects (mirror 0999): a JSON array is rejected.
    _rejected(db, psycopg.errors.CheckViolation, _evaluation, "e_arr",
              population_report="[]")


def test_1002_decision_fk_and_nullable_supersedes(db):
    _rejected(db, psycopg.errors.ForeignKeyViolation, _decision, "d_orphan",
              evaluation_id="e_missing")
    _evaluation(db, "e1", result="PASS")
    _decision(db, "d1", evaluation_id="e1", supersedes_decision_id=None)   # nullable, inserts fine


def test_1002_layer_b_labels_nullable(db):
    row = db.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name="
                     "'enablement_evaluation' AND column_name='layer_b_labels'").fetchone()
    assert row[0] == "YES"
