from __future__ import annotations


def _cols(db, table):
    return {r[0] for r in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,)).fetchall()}


def test_1002_creates_both_activation_tables(db):
    assert {"evaluation_id", "telemetry_window", "population_report", "gold_set_result",
            "stability_result", "layer_b_labels", "version_vector", "result", "content_hash",
            "evaluated_at"} <= _cols(db, "enablement_evaluation")
    assert {"decision_id", "evaluation_id", "deployment_id", "decision", "decided_by", "reason",
            "decided_at", "supersedes_decision_id"} <= _cols(db, "live_activation_decision")


def test_1002_result_and_decision_checks(db):
    db.execute("INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
               " gold_set_result, stability_result, version_vector, result, content_hash) VALUES"
               " ('e1','{}','{}','{}','{}','{}','PASS','h')")
    import pytest
    with pytest.raises(Exception):
        db.execute("INSERT INTO enablement_evaluation (evaluation_id, telemetry_window, population_report,"
                   " gold_set_result, stability_result, version_vector, result, content_hash) VALUES"
                   " ('e2','{}','{}','{}','{}','{}','MAYBE','h')")   # bad result enum


def test_1002_layer_b_labels_nullable(db):
    row = db.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name="
                     "'enablement_evaluation' AND column_name='layer_b_labels'").fetchone()
    assert row[0] == "YES"
