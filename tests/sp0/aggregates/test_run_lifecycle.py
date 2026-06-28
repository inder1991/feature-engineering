from sp0.events.store import load_stream
from sp0.aggregates.request_aggregate import create_request_command, create_run_command
from sp0.aggregates.run_lifecycle import (
    reject_command, cancel_command, withdraw_command,
    park_command, unpark_command, reopen_as_new_run_command, run_is_terminal,
)
from tests.sp0._helpers import make_cmd


def _new_run(db):
    req = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "x"})).aggregate_id
    return create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id, req


def test_reject_makes_run_terminal_and_records_reason(db):
    run, _ = _new_run(db)
    res = reject_command(db, make_cmd("reject", "run", run, {"reason": "leakage"}))
    assert res.accepted
    last = load_stream(db, "run", run)[-1]
    assert last.type == "RUN_REJECTED" and last.payload["reason"] == "leakage"
    assert run_is_terminal(db, run)


def test_second_terminal_command_is_rejected(db):
    run, _ = _new_run(db)
    cancel_command(db, make_cmd("cancel", "run", run, {"reason": "stop"}))
    res = withdraw_command(db, make_cmd("withdraw", "run", run, {"reason": "again"}))
    assert res.accepted is False and "terminal" in res.denied_reason


def test_park_unpark(db):
    run, _ = _new_run(db)
    park_command(db, make_cmd("park", "run", run, {"owner": "user:raj", "waiting_on_fact": "f1"}))
    unpark_command(db, make_cmd("unpark", "run", run, {}))
    types = [e.type for e in load_stream(db, "run", run)]
    assert types[-2:] == ["RUN_PARKED", "RUN_UNPARKED"]


def test_reopen_as_new_run_links_rejected(db):
    run, req = _new_run(db)
    reject_command(db, make_cmd("reject", "run", run, {"reason": "leakage"}))
    res = reopen_as_new_run_command(
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run}))
    assert res.accepted and res.aggregate_id != run
    new_created = load_stream(db, "run", res.aggregate_id)[0]
    assert new_created.payload["reopened_from"] == run
    added = [e.payload["run_id"] for e in load_stream(db, "request", req) if e.type == "CANDIDATE_ADDED"]
    assert res.aggregate_id in added


def test_reopen_rejected_when_source_not_rejected(db):
    run, _ = _new_run(db)
    res = reopen_as_new_run_command(
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run}))
    assert res.accepted is False and "rejected" in res.denied_reason


def test_resolve_degraded_clears_flag(db):
    from sp0.aggregates.run_lifecycle import resolve_degraded_command
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "degraded, degraded_reason) VALUES ('run_d', 'req_d', 'DRAFT', 1, true, 'boom')"
    )
    res = resolve_degraded_command(db, make_cmd("resolve_degraded", "run", "run_d", {}))
    assert res.accepted
    row = db.execute(
        "SELECT degraded, degraded_reason FROM run_workflow_state WHERE run_id = 'run_d'"
    ).fetchone()
    assert row[0] is False and row[1] is None
