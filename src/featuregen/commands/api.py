from __future__ import annotations

from collections.abc import Mapping

from psycopg.types.json import Jsonb

from featuregen.commands.authz_seam import current_authorizer
from featuregen.commands.registry import get_command
from featuregen.contracts import Command, CommandResult, DbConn

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


def _replay(conn: DbConn, key: str) -> CommandResult | None:
    row = conn.execute(
        "SELECT result FROM command_idempotency WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if row is None or row[0].get("_pending"):
        return None
    return _deserialize(row[0])


def _is_degraded(conn: DbConn, cmd: Command) -> bool:
    # Fail-closed enforcement of the §3.6 projection halt (SP-0.5 round-2 B1). A poison event marks
    # the affected aggregate in projection_degraded (keyed by aggregate + aggregate_id, any
    # projection); a command against a degraded aggregate is blocked. resolve_degraded is
    # special-cased by execute_command so it can clear the marker. (The prior check read
    # run_workflow_state.degraded, a column no production code ever sets — so nothing was blocked.)
    if cmd.aggregate_id is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM projection_degraded WHERE aggregate = %s AND aggregate_id = %s LIMIT 1",
        (cmd.aggregate, cmd.aggregate_id),
    ).fetchone()
    return row is not None


def execute_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Single command entrypoint (§4.4/§10). Claim-first idempotency (concurrent-safe),
    authz seam, degraded-block, dispatch.

    Runs the whole body in ONE transaction (SP-0.5 round-2): a SAVEPOINT when the caller already
    holds a transaction (preserves the `_claim` ON CONFLICT-blocks-the-loser concurrency and every
    existing in-transaction caller), a real transaction on an autocommit connection (so claim +
    handler + finalize are atomic — a handler failure rolls the claim back and a retry re-claims,
    instead of stranding a committed `_pending`). It NEVER calls conn.commit() itself.

    Authorization: the active authorizer (Phase 07) decides; **the Phase-07 authorizer is
    responsible for writing denials to `security_audit` (NOT the domain stream)** — that
    fulfils the contract's "on deny, writes to security_audit" for `execute_command`. The
    default Phase-06 seam (allow-all) writes nothing because it never denies."""
    # On an AUTOCOMMIT connection each statement self-commits, so wrap the whole body in one real
    # transaction (a handler failure then rolls the claim back instead of stranding a committed
    # _pending).
    if conn.autocommit:
        with conn.transaction():
            return _execute_command_body(conn, cmd)
    # On a NON-autocommit connection the caller owns commit, so we must NOT open+commit our own
    # transaction (that would defeat the caller's rollback / test isolation). But we still bracket
    # the body in a SAVEPOINT so a handler exception rolls back OUR claim within execute_command —
    # otherwise a caller that catches the exception then commits would strand a committed _pending
    # claim and block retries (SP-0.5 round-2 review). The savepoint rides the caller's transaction
    # (auto-begun by the first statement); RELEASE on success, ROLLBACK TO on failure, never commit.
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT execute_command")
    try:
        result = _execute_command_body(conn, cmd)
    except BaseException:
        with conn.cursor() as cur:
            cur.execute("ROLLBACK TO SAVEPOINT execute_command")
        raise
    with conn.cursor() as cur:
        cur.execute("RELEASE SAVEPOINT execute_command")
    return result


def _execute_command_body(conn: DbConn, cmd: Command) -> CommandResult:
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
                accepted=False,
                aggregate_id=cmd.aggregate_id or "",
                denied_reason="idempotency claim contended; retry",
            )
    # We own the claim; only now run authz + handler.
    decision = current_authorizer().authorize(conn, cmd)
    if not decision.allowed:
        _release(conn, key)
        return CommandResult(
            accepted=False,
            aggregate_id=cmd.aggregate_id or "",
            denied_reason=decision.reason,
        )
    if cmd.action != "resolve_degraded" and _is_degraded(conn, cmd):
        _release(conn, key)
        return CommandResult(
            accepted=False,
            aggregate_id=cmd.aggregate_id or "",
            denied_reason="aggregate is degraded",
        )
    result = get_command(cmd.action)(conn, cmd)
    if result.accepted:
        _finalize(conn, key, result)
    else:
        _release(conn, key)
    return result
