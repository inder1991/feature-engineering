from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn


@dataclass(frozen=True, slots=True)
class RepairLoopState:
    attempts_made: int
    max_attempts: int
    exhausted: bool
    rearm_seq: int = 0  # global_seq baseline of the current loop EPISODE (last re-arm)


def evaluate_repair_loop(
    conn: DbConn,
    run_id: str,
    *,
    max_attempts: int,
    attempt_event_types: Sequence[str],
    rearm_event_types: Sequence[str] = ("MANUAL_RETRY",),
) -> RepairLoopState:
    """Business repair loop (§5.6): count attempt events on the run stream SINCE the last
    re-arm (`manual_retry`). exhausted => route to a human via route_repair_exhaustion.
    `manual_retry` re-arms by advancing the baseline (rearm_seq) to its global_seq, so
    attempts after it start at zero and a fresh exhaustion can route again."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(global_seq), 0) FROM events WHERE run_id = %s AND type = ANY(%s)",
            (run_id, list(rearm_event_types)),
        )
        baseline = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id = %s AND type = ANY(%s) AND global_seq > %s",
            (run_id, list(attempt_event_types), baseline),
        )
        attempts_made = cur.fetchone()[0]
    return RepairLoopState(attempts_made, max_attempts, attempts_made >= max_attempts, baseline)


def route_repair_exhaustion(
    conn: DbConn, run_id: str, state: RepairLoopState, *, aggregate: str = "run"
) -> bool:
    """On exhaustion route the run to a human (§5.6: 'exhaustion → human'). Enqueues exactly
    ONE idempotent 'runtime.repair_exhausted' work message onto the Phase 04 queue; a
    downstream handler opens the human task / failure gate (this phase never calls open_task
    directly, mirroring the cost breaker's trip_cost_breaker). Idempotent PER EPISODE: the
    message_id embeds state.rearm_seq, so a `manual_retry` re-arm (new baseline) lets a fresh
    exhaustion route again. Returns True if a new message was enqueued; a no-op (returns
    False) when the loop is not exhausted or the episode already routed."""
    if not state.exhausted:
        return False
    message_id = f"repair-exhausted:{run_id}:{state.rearm_seq}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'runtime.repair_exhausted', %s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (
                message_id,
                f"{aggregate}:{run_id}",
                Jsonb(
                    {
                        "run_id": run_id,
                        "reason": "repair_exhausted",
                        "attempts_made": state.attempts_made,
                        "max_attempts": state.max_attempts,
                    }
                ),
            ),
        )
        return cur.rowcount == 1
