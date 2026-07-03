from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, NewExternalCommand

_log = logging.getLogger("featuregen.external_commands")


class HighCostWithoutDedup(Exception):
    """A high-cost integration was recorded without a dedup guarantee or job handle (§5.4)."""


def record_external_command(
    conn: DbConn,
    cmd: NewExternalCommand,
    *,
    command_id: str,
    run_id: str | None = None,
    require_dedup: frozenset[str] = frozenset({"sandbox"}),
) -> str:
    """Record a side-effecting command in the caller's §5.1 transaction (status='pending').
    Idempotent on idempotency_key (result caching: a duplicate returns the ORIGINAL
    command_id). High-cost integrations in `require_dedup` MUST carry dedup_supported or a
    job_handle, else HighCostWithoutDedup — no false exactly-once claim (§5.4)."""
    if cmd.integration in require_dedup and not cmd.dedup_supported and cmd.job_handle is None:
        raise HighCostWithoutDedup(
            f"{cmd.integration} requires dedup_supported or job_handle (§5.4)"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO external_commands
                (command_id, idempotency_key, run_id, integration, request_payload,
                 expected_run_id, expected_stream_version, expected_task_id,
                 job_handle, dedup_supported)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING command_id
            """,
            (
                command_id,
                cmd.idempotency_key,
                run_id,
                cmd.integration,
                Jsonb(dict(cmd.request_payload)),
                cmd.expected_run_id,
                cmd.expected_stream_version,
                cmd.expected_task_id,
                cmd.job_handle,
                cmd.dedup_supported,
            ),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "SELECT command_id FROM external_commands WHERE idempotency_key = %s",
            (cmd.idempotency_key,),
        )
        return cur.fetchone()[0]


@dataclass(frozen=True, slots=True)
class IntegrationResult:
    ok: bool
    result: Mapping[str, Any]
    cost_units: Decimal | None = None
    job_handle: str | None = None
    permanent: bool = False  # deterministic failure => skip delivery retry (§5.6)


@runtime_checkable
class IntegrationCaller(Protocol):
    integration: str

    def invoke(self, request_payload: Mapping[str, Any]) -> IntegrationResult: ...
    def reconcile(self, job_handle: str) -> IntegrationResult | None: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    command_id: str
    status: str  # succeeded|failed|pending|dispatched
    reinvoked: bool = False
    residual_duplicate_risk: bool = False
    reconciled: bool = False


def _flag_residual(result: Mapping[str, Any], residual: bool) -> dict:
    out = dict(result)
    if residual:
        out["_residual_duplicate_risk"] = True
    return out


def dispatch_command(
    conn: DbConn, command_id: str, caller: IntegrationCaller, *, now: datetime
) -> DispatchOutcome:
    """Execute ONE pending/dispatched external command crash-safely in THREE steps (§5.4),
    so a death between the external call and the result write can never silently re-invoke a
    side effect:

      (1) CLAIM — lock the row, mark it 'dispatched' (attempts+1) and COMMIT that transaction
          on its own, making the claim durable BEFORE any external work.
      (2) CALL — invoke (or, on recovery, reconcile) the external system OUTSIDE any open DB
          transaction. A crash here leaves the row durably 'dispatched', not 'pending'.
      (3) FINALIZE — re-lock and write the result ('succeeded'/'failed', or back to 'pending'
          for a retryable failure) in a SECOND committed transaction.

    Recovery of a row already 'dispatched' (claim committed, never finalized) NEVER blindly
    re-invokes: if a job_handle exists it is RECONCILED (no re-invoke); else if the integration
    honors the idempotency key (dedup_supported) it is safe to re-invoke; else the
    residual-duplicate risk is logged and persisted honestly (no false dedup claim)."""
    # --- Step 1: claim + mark 'dispatched', then COMMIT on its own ---------------------
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_payload, job_handle, dedup_supported, status "
            "FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            conn.rollback()
            raise KeyError(command_id)
        payload, job_handle, dedup_supported, status = row
        if status in ("succeeded", "stale_ignored", "failed"):
            conn.rollback()
            return DispatchOutcome(command_id, status)
        if status == "pending":
            cur.execute(
                "UPDATE external_commands SET status='dispatched', dispatched_at=%s, "
                "attempts=attempts+1 WHERE command_id=%s",
                (now, command_id),
            )
    conn.commit()  # claim is now durable BEFORE the external side effect

    # --- Step 2: external call OUTSIDE any DB transaction ------------------------------
    # `status` is the value read at claim time: 'pending' => first dispatch (fresh invoke);
    # 'dispatched' => recovery of a claimed-but-unfinalized command.
    reconciled = residual = reinvoked = False
    if status == "dispatched":
        if job_handle is not None:
            res = caller.reconcile(job_handle)
            reconciled = True
            if res is None:
                # Not resolvable yet — leave durably 'dispatched' for a later sweep.
                return DispatchOutcome(command_id, "dispatched", reconciled=True)
        elif dedup_supported:
            res = caller.invoke(payload)
            reinvoked = True
        else:
            residual = True
            _log.warning(
                "residual-duplicate risk: re-invoking %s (no job_handle; idempotency key "
                "not honored by %s) — accepted risk flagged, no false dedup claim",
                command_id,
                caller.integration,
            )
            res = caller.invoke(payload)
            reinvoked = True
    else:  # 'pending' -> claimed above; first invocation
        res = caller.invoke(payload)

    # --- Step 3: finalize the result in a SECOND committed transaction -----------------
    return _finalize(
        conn, command_id, res, now,
        reinvoked=reinvoked, residual=residual, reconciled=reconciled,
    )


def _finalize(
    conn: DbConn,
    command_id: str,
    res: IntegrationResult,
    now: datetime,
    *,
    reinvoked: bool,
    residual: bool,
    reconciled: bool,
) -> DispatchOutcome:
    """Write the external call's result in its OWN committed transaction (§5.4 Step 3): re-lock the
    row, honor a concurrent finalize if one already landed, else persist succeeded/failed/pending.
    Shared by dispatch_command (first dispatch + recovery) and invoke_claimed_external."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        frow = cur.fetchone()
        if frow is None:
            conn.rollback()
            raise KeyError(command_id)
        if frow[0] in ("succeeded", "failed", "stale_ignored"):
            # A concurrent dispatcher already finalized this command; honor it.
            conn.rollback()
            return DispatchOutcome(command_id, frow[0], reinvoked, residual, reconciled)

        if res.ok:
            cur.execute(
                "UPDATE external_commands SET status='succeeded', result=%s, cost_units=%s, "
                "completed_at=%s, job_handle=COALESCE(%s, job_handle) WHERE command_id=%s",
                (
                    Jsonb(_flag_residual(res.result, residual)),
                    res.cost_units,
                    now,
                    res.job_handle,
                    command_id,
                ),
            )
            outcome = DispatchOutcome(command_id, "succeeded", reinvoked, residual, reconciled)
        elif res.permanent:
            cur.execute(
                "UPDATE external_commands SET status='failed', result=%s, completed_at=%s "
                "WHERE command_id=%s",
                (Jsonb(dict(res.result)), now, command_id),
            )
            outcome = DispatchOutcome(command_id, "failed", reinvoked, residual, reconciled)
        else:
            cur.execute(
                "UPDATE external_commands SET status='pending' WHERE command_id=%s",
                (command_id,),
            )
            outcome = DispatchOutcome(command_id, "pending", reinvoked, residual, reconciled)
    conn.commit()
    return outcome


@dataclass(frozen=True, slots=True)
class ResultAcceptance:
    command_id: str
    accepted: bool
    stale: bool
    cached: bool = False


def accept_result(
    conn: DbConn,
    command_id: str,
    *,
    current_run_id: str | None,
    current_stream_version: int | None,
    current_task_id: str | None,
) -> ResultAcceptance:
    """Stale-result acceptance guard (§5.4). The result is APPLIED only if the run/task it
    was issued against has not moved on: expected_run_id == current_run_id AND (no
    expected_stream_version OR current has not advanced past it) AND (no expected_task_id OR
    == current). Otherwise it is accepted-and-IGNORED as stale (status='stale_ignored') —
    never blindly applied to a moved-on run. Idempotent: a command already routed to
    stale/applied returns cached."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_run_id, expected_stream_version, expected_task_id, status, "
            "result_event_id FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(command_id)
        exp_run, exp_sv, exp_task, status, result_event_id = row
        if status == "stale_ignored":
            return ResultAcceptance(command_id, accepted=False, stale=True, cached=True)
        if result_event_id is not None:
            return ResultAcceptance(command_id, accepted=True, stale=False, cached=True)
        stale = False
        if (
            exp_run is not None
            and exp_run != current_run_id
            or (
                exp_sv is not None
                and current_stream_version is not None
                and current_stream_version > exp_sv
            )
            or exp_task is not None
            and exp_task != current_task_id
        ):
            stale = True
        if stale:
            cur.execute(
                "UPDATE external_commands SET status='stale_ignored', completed_at=now() "
                "WHERE command_id=%s",
                (command_id,),
            )
            return ResultAcceptance(command_id, accepted=False, stale=True)
    return ResultAcceptance(command_id, accepted=True, stale=False)


# --- Worker wiring: caller registry + first-dispatch claim + crash-recovery sweep ----------

_CALLERS: dict[str, IntegrationCaller] = {}


def register_integration_caller(caller: IntegrationCaller) -> None:
    """Register the IntegrationCaller that CAN execute an integration's external commands. The
    worker only claims commands whose integration is registered (fail-closed); an unregistered
    integration's rows are never invoked, only counted (SP-0.5 round-2). Idempotent — last wins."""
    _CALLERS[caller.integration] = caller


def current_integration_callers() -> dict[str, IntegrationCaller]:
    """Snapshot of registered integration -> caller; its keys gate the claim queries so an
    un-callable integration is never claimed."""
    return dict(_CALLERS)


@dataclass(frozen=True, slots=True)
class ClaimedExternal:
    command_id: str
    integration: str
    payload: Mapping[str, Any]
    job_handle: str | None
    dedup_supported: bool


def claim_next_pending(
    conn: DbConn, registered_integrations, *, now: datetime
) -> ClaimedExternal | None:
    """Atomically claim ONE pending external command whose integration is registered (mark it
    'dispatched', attempts+1) with FOR UPDATE SKIP LOCKED, so two concurrent workers never hand
    the same row to a fresh invoke. Returns None if nothing is claimable. FIRST-dispatch only — the
    claimed row is invoked via invoke_claimed_external, NOT dispatch_command's recovery branch."""
    reg = list(registered_integrations)
    if not reg:
        return None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE external_commands SET status='dispatched', dispatched_at=%s, "
            "attempts=attempts+1 WHERE command_id = (SELECT command_id FROM external_commands "
            "WHERE status='pending' AND integration = ANY(%s) "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1) "
            "RETURNING command_id, integration, request_payload, job_handle, dedup_supported",
            (now, reg),
        )
        row = cur.fetchone()
    conn.commit()  # claim durable BEFORE the external call (mirrors dispatch_command Step 1)
    if row is None:
        return None
    return ClaimedExternal(row[0], row[1], row[2], row[3], row[4])


def invoke_claimed_external(
    conn: DbConn, claimed: ClaimedExternal, caller: IntegrationCaller, *, now: datetime
) -> DispatchOutcome:
    """CALL + FINALIZE a row claim_next_pending already claimed as a FIRST dispatch — a known-fresh
    invoke, so it never takes dispatch_command's recovery branch (no false residual-risk flag)."""
    res = caller.invoke(claimed.payload)
    return _finalize(
        conn, claimed.command_id, res, now, reinvoked=False, residual=False, reconciled=False
    )


def claim_stale_dispatched(
    conn: DbConn, registered_integrations, *, stale_after_seconds: float, now: datetime
) -> list[tuple[str, str]]:
    """Find external commands stuck in 'dispatched' (a worker died after claiming, before
    finalizing) older than `stale_after_seconds` — CRASH RECOVERY. Returns [(command_id,
    integration)] for registered integrations only, FOR UPDATE SKIP LOCKED so two workers do not
    both sweep the same row. The caller routes each through dispatch_command, whose 'dispatched' ->
    recover path (reconcile / dedup-safe re-invoke / honest residual flag) is idempotency-safe."""
    reg = list(registered_integrations)
    if not reg:
        return []
    cutoff = now - timedelta(seconds=stale_after_seconds)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT command_id, integration FROM external_commands "
            "WHERE status='dispatched' AND integration = ANY(%s) AND dispatched_at < %s "
            "ORDER BY dispatched_at FOR UPDATE SKIP LOCKED",
            (reg, cutoff),
        )
        rows = cur.fetchall()
    conn.rollback()  # release row locks; dispatch_command re-locks each in its own Step 1
    return [(r[0], r[1]) for r in rows]


def pending_unhandled_count(conn: DbConn, registered_integrations) -> int:
    """Count pending rows whose integration has NO registered caller — surfaced as a gauge so an
    operator registers the missing caller (rows are never lost, just currently un-invokable)."""
    reg = list(registered_integrations)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM external_commands "
            "WHERE status='pending' AND NOT (integration = ANY(%s))",
            (reg,),
        )
        return int(cur.fetchone()[0])
