from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, NewExternalCommand

_log = logging.getLogger("sp0.external_commands")


class HighCostWithoutDedup(Exception):
    """A high-cost integration was recorded without a dedup guarantee or job handle (§5.4)."""


def record_external_command(
    conn: DbConn,
    cmd: NewExternalCommand,
    *,
    command_id: str,
    run_id: Optional[str] = None,
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
            (command_id, cmd.idempotency_key, run_id, cmd.integration,
             Jsonb(dict(cmd.request_payload)), cmd.expected_run_id,
             cmd.expected_stream_version, cmd.expected_task_id, cmd.job_handle,
             cmd.dedup_supported),
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
    cost_units: Optional[Decimal] = None
    job_handle: Optional[str] = None
    permanent: bool = False        # deterministic failure => skip delivery retry (§5.6)


@runtime_checkable
class IntegrationCaller(Protocol):
    integration: str
    def invoke(self, request_payload: Mapping[str, Any]) -> IntegrationResult: ...
    def reconcile(self, job_handle: str) -> Optional[IntegrationResult]: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    command_id: str
    status: str                    # succeeded|failed|pending|dispatched
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
    """Execute ONE pending/dispatched external command (§5.4). On recovery of a command
    already 'dispatched': if a job_handle exists, reconcile (no re-invoke); else if the
    integration does NOT honor the idempotency key (dedup_supported=False), re-invoke and
    FLAG the residual-duplicate risk honestly (logged + persisted in result) — never a false
    dedup claim. Retryable failures stay 'pending'; permanent failures go to 'failed'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_payload, job_handle, dedup_supported, status "
            "FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(command_id)
        payload, job_handle, dedup_supported, status = row
        if status in ("succeeded", "stale_ignored", "failed"):
            return DispatchOutcome(command_id, status)

        reconciled = residual = reinvoked = False
        if status == "dispatched":
            if job_handle is not None:
                res = caller.reconcile(job_handle)
                reconciled = True
                if res is None:
                    return DispatchOutcome(command_id, "dispatched", reconciled=True)
            else:
                if not dedup_supported:
                    residual = True
                    _log.warning(
                        "residual-duplicate risk: re-invoking %s (idempotency not honored)",
                        command_id,
                    )
                res = caller.invoke(payload)
                reinvoked = True
        else:  # pending
            cur.execute(
                "UPDATE external_commands SET status='dispatched', dispatched_at=%s, "
                "attempts=attempts+1 WHERE command_id=%s",
                (now, command_id),
            )
            res = caller.invoke(payload)

        if res.ok:
            cur.execute(
                "UPDATE external_commands SET status='succeeded', result=%s, cost_units=%s, "
                "completed_at=%s, job_handle=COALESCE(%s, job_handle) WHERE command_id=%s",
                (Jsonb(_flag_residual(res.result, residual)), res.cost_units, now,
                 res.job_handle, command_id),
            )
            return DispatchOutcome(command_id, "succeeded", reinvoked, residual, reconciled)
        if res.permanent:
            cur.execute(
                "UPDATE external_commands SET status='failed', result=%s, completed_at=%s "
                "WHERE command_id=%s",
                (Jsonb(dict(res.result)), now, command_id),
            )
            return DispatchOutcome(command_id, "failed", reinvoked, residual, reconciled)
        cur.execute(
            "UPDATE external_commands SET status='pending' WHERE command_id=%s", (command_id,)
        )
        return DispatchOutcome(command_id, "pending", reinvoked, residual, reconciled)
