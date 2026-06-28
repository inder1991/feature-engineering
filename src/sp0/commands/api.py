from __future__ import annotations

from typing import Mapping, Optional

from psycopg.types.json import Jsonb

from sp0.contracts import Command, CommandResult, DbConn
from sp0.commands.registry import get_command
from sp0.commands.authz_seam import current_authorizer

_PENDING = {"_pending": True}


def _serialize(result: CommandResult) -> dict:
    return {
        "accepted": result.accepted,
        "aggregate_id": result.aggregate_id,
        "produced_event_ids": list(result.produced_event_ids),
        "denied_reason": result.denied_reason,
    }


def _deserialize(data: Mapping) -> CommandResult:
    return CommandResult(
        accepted=data["accepted"],
        aggregate_id=data["aggregate_id"],
        produced_event_ids=tuple(data["produced_event_ids"]),
        denied_reason=data.get("denied_reason"),
    )


def _claim(conn: DbConn, key: str, action: str) -> bool:
    """Insert a PENDING claim row. Returns True if we won the claim, False if a row already
    exists. `ON CONFLICT (idempotency_key) DO NOTHING` serializes concurrent same-key
    submitters at the unique PK: the loser BLOCKS on the winner's uncommitted row until the
    winner commits/rolls back, then either sees the finalized result (commit) or wins the
    re-claim (rollback). This closes the concurrent double-submit hole — only one transaction
    ever runs the handler for a given idempotency_key."""
    row = conn.execute(
        "INSERT INTO command_idempotency (idempotency_key, action, result) "
        "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key",
        (key, action, Jsonb(_PENDING)),
    ).fetchone()
    return row is not None


def _finalize(conn: DbConn, key: str, result: CommandResult) -> None:
    conn.execute(
        "UPDATE command_idempotency SET result = %s WHERE idempotency_key = %s",
        (Jsonb(_serialize(result)), key),
    )


def _release(conn: DbConn, key: str) -> None:
    # Denials / degraded blocks are NOT cached: drop the claim so a later legitimate retry runs.
    conn.execute("DELETE FROM command_idempotency WHERE idempotency_key = %s", (key,))


def _replay(conn: DbConn, key: str) -> Optional[CommandResult]:
    row = conn.execute(
        "SELECT result FROM command_idempotency WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if row is None or row[0].get("_pending"):
        return None
    return _deserialize(row[0])


def _is_degraded(conn: DbConn, cmd: Command) -> bool:
    if cmd.aggregate != "run" or cmd.aggregate_id is None:
        return False
    row = conn.execute(
        "SELECT degraded FROM run_workflow_state WHERE run_id = %s",
        (cmd.aggregate_id,),
    ).fetchone()
    return bool(row and row[0])


def execute_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Single command entrypoint (§4.4/§10). Claim-first idempotency (concurrent-safe),
    authz seam, degraded-block, dispatch.

    Authorization: the active authorizer (Phase 07) decides; **the Phase-07 authorizer is
    responsible for writing denials to `security_audit` (NOT the domain stream)** — that
    fulfils the contract's "on deny, writes to security_audit" for `execute_command`. The
    default Phase-06 seam (allow-all) writes nothing because it never denies."""
    key = cmd.idempotency_key
    owned = _claim(conn, key, cmd.action)
    if not owned:
        prior = _replay(conn, key)
        if prior is not None:
            return prior
        # Winner aborted/released its claim; one takeover attempt.
        owned = _claim(conn, key, cmd.action)
        if not owned:
            prior = _replay(conn, key)
            if prior is not None:
                return prior
            return CommandResult(
                accepted=False, aggregate_id=cmd.aggregate_id or "",
                denied_reason="idempotency claim contended; retry",
            )
    # We own the claim; only now run authz + handler.
    decision = current_authorizer().authorize(conn, cmd)
    if not decision.allowed:
        _release(conn, key)
        return CommandResult(
            accepted=False, aggregate_id=cmd.aggregate_id or "",
            denied_reason=decision.reason,
        )
    if cmd.action != "resolve_degraded" and _is_degraded(conn, cmd):
        _release(conn, key)
        return CommandResult(
            accepted=False, aggregate_id=cmd.aggregate_id or "",
            denied_reason="aggregate is degraded",
        )
    result = get_command(cmd.action)(conn, cmd)
    if result.accepted:
        _finalize(conn, key, result)
    else:
        _release(conn, key)
    return result
