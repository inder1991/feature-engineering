from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import (
    IntegrationResult,
    dispatch_command,
    record_external_command,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _record(conn, key, *, integration="llm", dedup=False, handle=None, status="pending"):
    cmd = NewExternalCommand(
        integration=integration,
        idempotency_key=key,
        request_payload={"p": 1},
        dedup_supported=dedup,
        job_handle=handle,
    )
    cid = record_external_command(
        conn, cmd, command_id=f"cmd_{key}", run_id="run_1", require_dedup=frozenset()
    )
    if status != "pending":
        with conn.cursor() as cur:
            cur.execute("UPDATE external_commands SET status=%s WHERE command_id=%s", (status, cid))
    return cid


def _row(conn, cid):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, result, cost_units FROM external_commands WHERE command_id=%s", (cid,)
        )
        return cur.fetchone()


def test_pending_success(conn, recording_caller):
    cid = _record(conn, "ok")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 7}, Decimal("1.50")))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    status, result, cost = _row(conn, cid)
    assert status == "succeeded" and result["answer"] == 7 and cost == Decimal("1.50")
    assert caller.invoke_calls == 1


def test_retryable_stays_pending(conn, recording_caller):
    cid = _record(conn, "retry")
    caller = recording_caller(
        invoke_result=IntegrationResult(False, {"err": "503"}, permanent=False)
    )
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "pending"
    assert _row(conn, cid)[0] == "pending"


def test_permanent_fails(conn, recording_caller):
    cid = _record(conn, "perm")
    caller = recording_caller(
        invoke_result=IntegrationResult(False, {"err": "bad input"}, permanent=True)
    )
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "failed"
    assert _row(conn, cid)[0] == "failed"


def test_recovery_reconciles_via_handle(conn, recording_caller):
    cid = _record(conn, "rec", handle="job-9", status="dispatched")
    caller = recording_caller(reconcile_result=IntegrationResult(True, {"answer": 1}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded" and out.reconciled is True
    assert caller.invoke_calls == 0 and caller.reconcile_calls == 1


def test_recovery_no_handle_no_dedup_flags_residual(conn, recording_caller):
    cid = _record(conn, "resid", dedup=False, handle=None, status="dispatched")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 2}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    assert out.reinvoked is True and out.residual_duplicate_risk is True
    assert _row(conn, cid)[1]["_residual_duplicate_risk"] is True
    assert caller.invoke_calls == 1


def test_recovery_dedup_supported_no_residual(conn, recording_caller):
    cid = _record(conn, "safe", dedup=True, handle=None, status="dispatched")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 3}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    assert out.reinvoked is True and out.residual_duplicate_risk is False
    assert "_residual_duplicate_risk" not in _row(conn, cid)[1]


class _CrashingCaller:
    """Performs the external side effect, then crashes BEFORE the result is
    finalized — simulating a dispatcher death between invoke() and the result
    write (§5.4). Recovery must reconcile via the job handle, never re-invoke."""

    integration = "llm"

    def __init__(self, *, reconcile_result):
        self._reconcile_result = reconcile_result
        self.invoke_calls = 0
        self.reconcile_calls = 0
        self.side_effects = 0

    def invoke(self, request_payload):
        self.invoke_calls += 1
        self.side_effects += 1  # the irreversible external effect HAPPENED
        raise RuntimeError("crash after external effect, before finalize")

    def reconcile(self, job_handle):
        self.reconcile_calls += 1
        return self._reconcile_result


def test_crash_between_invoke_and_finalize_does_not_double_invoke(conn):
    # A pending command with a reconcilable job handle.
    cid = _record(conn, "crash", handle="job-crash")

    caller = _CrashingCaller(
        reconcile_result=IntegrationResult(True, {"answer": 99}, job_handle="job-crash")
    )

    # Dispatch: claim+mark-dispatched (committed), then the external call crashes
    # AFTER the side effect but BEFORE the result is finalized.
    with pytest.raises(RuntimeError):
        dispatch_command(conn, cid, caller, now=NOW)
    assert caller.invoke_calls == 1 and caller.side_effects == 1

    # The 'dispatched' claim must have been COMMITTED in its own transaction, so
    # it survives the crash/rollback — recovery can see it (this is the fix: the
    # old single-transaction code rolled the mark back to 'pending').
    conn.rollback()  # discard any open tx; read durably committed state
    assert _row(conn, cid)[0] == "dispatched"

    # Recovery: reconcile via the job handle — NO second invocation.
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded" and out.reconciled is True
    assert caller.invoke_calls == 1 and caller.side_effects == 1  # not re-invoked
    assert caller.reconcile_calls == 1
    status, result, _ = _row(conn, cid)
    assert status == "succeeded" and result["answer"] == 99
