from tests.featuregen._helpers import make_cmd

from featuregen.aggregates.request_aggregate import create_request_command, create_run_command
from featuregen.aggregates.run_lifecycle import (
    cancel_command,
    fact_confirmed_resume_command,
    park_command,
    reject_command,
    reopen_as_new_run_command,
    run_is_terminal,
    source_changed_revalidate_command,
    unpark_command,
    withdraw_command,
)
from featuregen.events.store import load_stream


def _new_run(db):
    req = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "x"})
    ).aggregate_id
    return create_run_command(
        db, make_cmd("create_run", "request", req, {"request_id": req})
    ).aggregate_id, req


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
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run})
    )
    assert res.accepted and res.aggregate_id != run
    new_created = load_stream(db, "run", res.aggregate_id)[0]
    assert new_created.payload["reopened_from"] == run
    added = [
        e.payload["run_id"] for e in load_stream(db, "request", req) if e.type == "CANDIDATE_ADDED"
    ]
    assert res.aggregate_id in added


def test_reopen_rejected_when_source_not_rejected(db):
    run, _ = _new_run(db)
    res = reopen_as_new_run_command(
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run})
    )
    assert res.accepted is False and "rejected" in res.denied_reason


def test_resolve_degraded_clears_marker(db):
    # resolve_degraded deletes the projection_degraded marker the runner writes (the ledger
    # execute_command now enforces, SP-0.5 round-2 B1), not the never-set run_workflow_state column.
    from featuregen.aggregates.run_lifecycle import resolve_degraded_command

    db.execute(
        "INSERT INTO projection_degraded (projection_name, aggregate, aggregate_id, reason, "
        "poison_event_id, poison_seq) VALUES ('run','run','run_d','boom',NULL,1)"
    )
    res = resolve_degraded_command(db, make_cmd("resolve_degraded", "run", "run_d", {}))
    assert res.accepted
    left = db.execute(
        "SELECT count(*) FROM projection_degraded WHERE aggregate_id='run_d'"
    ).fetchone()[0]
    assert left == 0


def test_fact_confirmed_resume_wakes_only_runs_waiting_on_that_fact(db):
    waiting, _ = _new_run(db)
    other, _ = _new_run(db)
    park_command(
        db, make_cmd("park", "run", waiting, {"owner": "o", "waiting_on_fact": "overlay:123"})
    )
    park_command(
        db, make_cmd("park", "run", other, {"owner": "o", "waiting_on_fact": "overlay:999"})
    )
    res = fact_confirmed_resume_command(
        db, make_cmd("fact_confirmed_resume", "run", None, {"fact_key": "overlay:123"})
    )
    assert res.accepted
    woken_types = [e.type for e in load_stream(db, "run", waiting)]
    assert "FACT_CONFIRMED_RESUME" in woken_types and woken_types[-1] == "RUN_UNPARKED"
    assert "FACT_CONFIRMED_RESUME" not in [e.type for e in load_stream(db, "run", other)]


def test_source_changed_revalidate_for_in_flight_run(db):
    run, _ = _new_run(db)
    res = source_changed_revalidate_command(
        db,
        make_cmd(
            "source_changed_revalidate",
            "run",
            run,
            {"source_ref": "tbl.core.txn", "new_snapshot": "snap@42"},
        ),
    )
    assert res.accepted
    last = load_stream(db, "run", run)[-1]
    assert last.type == "SOURCE_CHANGED_REVALIDATE"
    assert last.payload["source_ref"] == "tbl.core.txn"


def test_source_changed_revalidate_rejected_when_terminal(db):
    run, _ = _new_run(db)
    reject_command(db, make_cmd("reject", "run", run, {"reason": "x"}))
    res = source_changed_revalidate_command(
        db, make_cmd("source_changed_revalidate", "run", run, {"source_ref": "t"})
    )
    assert res.accepted is False and "terminal" in res.denied_reason
