from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import (
    IntegrationResult,
    claim_next_pending,
    claim_stale_dispatched,
    dispatch_command,
    invoke_claimed_external,
    pending_unhandled_count,
    record_external_command,
    register_integration_caller,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clear_callers():
    # The caller registry is a process-global; clear it around each test so registrations do not
    # bleed between tests.
    from featuregen.runtime import external_commands

    external_commands._CALLERS.clear()
    yield
    external_commands._CALLERS.clear()


def _record(conn, key, *, integration="llm", dedup=False, handle=None):
    cmd = NewExternalCommand(
        integration=integration,
        idempotency_key=key,
        request_payload={"p": 1},
        dedup_supported=dedup,
        job_handle=handle,
    )
    return record_external_command(
        conn, cmd, command_id=f"cmd_{key}", run_id="run_1", require_dedup=frozenset()
    )


def _status(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM external_commands WHERE command_id=%s", (cid,))
        return cur.fetchone()[0]


def test_claim_and_invoke_dispatches_a_pending_command(autocommit_worker_conn, recording_caller):
    conn = autocommit_worker_conn
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 7}, Decimal("1.5")))
    register_integration_caller(caller)
    cid = _record(conn, "ok")

    claimed = claim_next_pending(conn, ["llm"], now=NOW)
    assert claimed is not None and claimed.command_id == cid
    assert _status(conn, cid) == "dispatched"  # claimed before the call (crash-safe)

    out = invoke_claimed_external(conn, claimed, caller, now=NOW)
    # fresh invoke, no false residual-duplicate flag
    assert out.status == "succeeded" and out.residual_duplicate_risk is False
    assert _status(conn, cid) == "succeeded"
    assert caller.invoke_calls == 1


def test_claim_next_pending_skips_unregistered_integration(autocommit_worker_conn):
    conn = autocommit_worker_conn
    _record(conn, "unh", integration="unregistered")

    # Only 'llm' is registered -> the 'unregistered' row is never claimed, just counted.
    assert claim_next_pending(conn, ["llm"], now=NOW) is None
    assert pending_unhandled_count(conn, ["llm"]) == 1
    assert _status(conn, "cmd_unh") == "pending"  # untouched, never lost


def test_claim_stale_dispatched_recovers_a_crashed_dispatch(autocommit_worker_conn, recording_caller):
    conn = autocommit_worker_conn
    # A crash left the row 'dispatched' with a job_handle, long ago.
    cid = _record(conn, "crashed", handle="job-42")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE external_commands SET status='dispatched', dispatched_at=%s WHERE command_id=%s",
            (NOW - timedelta(hours=1), cid),
        )

    stale = claim_stale_dispatched(conn, ["llm"], stale_after_seconds=60, now=NOW, limit=10)
    assert stale == [(cid, "llm")]
    # Atomic exclusion: the row's dispatched_at was re-stamped past the cutoff, so an immediately
    # concurrent sweep does NOT pick it up again (no double recovery / double invoke).
    assert claim_stale_dispatched(conn, ["llm"], stale_after_seconds=60, now=NOW, limit=10) == []

    # Route through dispatch_command's recovery branch: job_handle -> reconcile, no re-invoke.
    caller = recording_caller(reconcile_result=IntegrationResult(True, {"reconciled": True}))
    register_integration_caller(caller)
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.reconciled is True and caller.invoke_calls == 0  # recovered, not re-invoked
    assert _status(conn, cid) == "succeeded"


def test_claim_stale_dispatched_ignores_fresh_dispatched_rows(autocommit_worker_conn):
    conn = autocommit_worker_conn
    cid = _record(conn, "fresh")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE external_commands SET status='dispatched', dispatched_at=%s WHERE command_id=%s",
            (NOW, cid),  # just dispatched -> not stale yet
        )
    assert claim_stale_dispatched(conn, ["llm"], stale_after_seconds=60, now=NOW, limit=10) == []


def test_finalize_is_exactly_once_under_a_repeat_recovery(autocommit_worker_conn, recording_caller):
    # If the same command is recovered twice (e.g. a duplicate sweep), the cost must be counted
    # ONCE: the first finalize wins the FOR-UPDATE transaction, the second honors 'succeeded' and
    # does NOT re-record cost (SP-0.5 round-2 review — no durable-budget double-count).
    conn = autocommit_worker_conn
    conn.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "cost_units) VALUES ('run_1','req_1','DRAFT',1,0)"
    )
    caller = recording_caller(reconcile_result=IntegrationResult(True, {"ok": 1}, Decimal("4.0")))
    register_integration_caller(caller)
    cid = _record(conn, "twice", handle="job-x")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE external_commands SET status='dispatched', dispatched_at=%s WHERE command_id=%s",
            (NOW - timedelta(hours=1), cid),
        )

    dispatch_command(conn, cid, caller, now=NOW)  # first recovery -> succeeded, cost recorded once
    dispatch_command(conn, cid, caller, now=NOW)  # second -> honors 'succeeded', no re-record

    cost = conn.execute(
        "SELECT cost_units FROM run_workflow_state WHERE run_id='run_1'"
    ).fetchone()[0]
    assert cost == Decimal("4.0")  # counted exactly once, not 8.0


def test_external_command_cost_rolls_into_run_budget(autocommit_worker_conn, recording_caller):
    # A succeeded external command's cost must roll into the run's durable §5.6 budget so the cost
    # breaker sees external spend, not only the external_commands row (SP-0.5 round-2).
    conn = autocommit_worker_conn
    conn.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "cost_units) VALUES ('run_1','req_1','DRAFT',1,0)"
    )
    caller = recording_caller(invoke_result=IntegrationResult(True, {"ok": 1}, Decimal("3.5")))
    register_integration_caller(caller)
    cid = _record(conn, "costed")  # run_id='run_1'

    claimed = claim_next_pending(conn, ["llm"], now=NOW)
    invoke_claimed_external(conn, claimed, caller, now=NOW)

    cost = conn.execute(
        "SELECT cost_units FROM run_workflow_state WHERE run_id='run_1'"
    ).fetchone()[0]
    assert cost == Decimal("3.5")
    assert _status(conn, cid) == "succeeded"


def test_retryable_external_command_backs_off_not_hot_looped(autocommit_worker_conn, recording_caller):
    # A retryable failure must set a FUTURE next_attempt_at so the row is NOT immediately
    # re-claimable — otherwise one failing command is re-invoked up to `batch` times per tick
    # (SP-0.5 round-2 review, finding 3).
    conn = autocommit_worker_conn
    caller = recording_caller(invoke_result=IntegrationResult(False, {}, permanent=False))
    register_integration_caller(caller)
    cid = _record(conn, "retry")

    claimed = claim_next_pending(conn, ["llm"], now=NOW)
    invoke_claimed_external(conn, claimed, caller, now=NOW)
    assert _status(conn, cid) == "pending"  # retryable -> back to pending

    # Not immediately re-claimable at the same instant (backoff window is in the future).
    assert claim_next_pending(conn, ["llm"], now=NOW) is None
    # After the backoff window elapses, it IS claimable again.
    assert claim_next_pending(conn, ["llm"], now=NOW + timedelta(hours=2)) is not None


def _seed_run(conn, run_id="run_1", cost=0):
    conn.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "cost_units) VALUES (%s,'req_1','DRAFT',1,%s)",
        (run_id, cost),
    )


def test_external_cost_over_ceiling_trips_breaker_and_auto_parks(
    autocommit_worker_conn, recording_caller, monkeypatch
):
    # A succeeded external command whose cost pushes the run over the per-run ceiling must TRIP the
    # §5.6 cost breaker (enqueue runtime.auto_park), not just record spend (SP-0.5 round-2 review).
    monkeypatch.setenv("FEATUREGEN_COST_PER_RUN", "5")
    conn = autocommit_worker_conn
    _seed_run(conn)
    caller = recording_caller(invoke_result=IntegrationResult(True, {"ok": 1}, Decimal("6.0")))
    register_integration_caller(caller)
    cid = _record(conn, "over")

    invoke_claimed_external(conn, claim_next_pending(conn, ["llm"], now=NOW), caller, now=NOW)

    assert _status(conn, cid) == "succeeded"
    row = conn.execute(
        "SELECT handler, payload FROM queue WHERE message_id = 'cost-breaker:run_1:per_run'"
    ).fetchone()
    assert row is not None and row[0] == "runtime.auto_park"
    assert row[1]["reason"] == "cost_ceiling" and row[1]["ceiling"] == "per_run"


def test_external_cost_under_ceiling_does_not_trip(
    autocommit_worker_conn, recording_caller, monkeypatch
):
    monkeypatch.setenv("FEATUREGEN_COST_PER_RUN", "100")
    conn = autocommit_worker_conn
    _seed_run(conn)
    caller = recording_caller(invoke_result=IntegrationResult(True, {"ok": 1}, Decimal("6.0")))
    register_integration_caller(caller)
    _record(conn, "under")

    invoke_claimed_external(conn, claim_next_pending(conn, ["llm"], now=NOW), caller, now=NOW)

    assert conn.execute(
        "SELECT count(*) FROM queue WHERE handler = 'runtime.auto_park'"
    ).fetchone()[0] == 0


def test_run_worker_once_dispatches_external_commands(autocommit_worker_conn, recording_caller):
    # End-to-end wiring: a registered caller + a pending external command -> the worker's external
    # stage claims + invokes it on the real autocommit daemon connection (SP-0.5 round-2).
    from featuregen.runtime.worker import compose, run_worker_once

    conn = autocommit_worker_conn
    caller = recording_caller(invoke_result=IntegrationResult(True, {"ok": 1}, Decimal("2.0")))
    register_integration_caller(caller)
    reg, projections = compose(conn)
    cid = _record(conn, "stage")

    tick = run_worker_once(conn, reg, projections, owner="w1", now=NOW)

    assert tick.external_dispatched >= 1
    assert _status(conn, cid) == "succeeded"
    assert caller.invoke_calls == 1
