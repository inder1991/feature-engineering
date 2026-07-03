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

    stale = claim_stale_dispatched(conn, ["llm"], stale_after_seconds=60, now=NOW)
    assert stale == [(cid, "llm")]

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
    assert claim_stale_dispatched(conn, ["llm"], stale_after_seconds=60, now=NOW) == []


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
