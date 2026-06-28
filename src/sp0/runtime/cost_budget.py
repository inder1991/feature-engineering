from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from psycopg.types.json import Jsonb

from sp0.contracts import DbConn


@dataclass(frozen=True, slots=True)
class CostCeilings:
    per_run: Optional[Decimal] = None
    per_request: Optional[Decimal] = None
    max_candidates: Optional[int] = None


@dataclass(frozen=True, slots=True)
class CostBreakerOutcome:
    tripped: bool
    ceiling: Optional[str] = None        # per_run|per_request|max_candidates
    run_cost: Decimal = Decimal(0)
    request_cost: Decimal = Decimal(0)


def record_cost(conn: DbConn, run_id: str, delta: Decimal) -> Decimal:
    """Add to the durable per-run cost counter (run_workflow_state.cost_units, §5.6) and
    return the new total. The caller gates double-counting via the external_command's first
    transition to 'succeeded'."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE run_workflow_state SET cost_units = cost_units + %s, updated_at = now() "
            "WHERE run_id = %s RETURNING cost_units",
            (delta, run_id),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        return row[0]


def request_cost(conn: DbConn, request_id: str) -> Decimal:
    """Per-request cost = SUM of per-run counters across the request's runs (§5.6)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_units), 0) FROM run_workflow_state WHERE request_id = %s",
            (request_id,),
        )
        return cur.fetchone()[0]


def check_cost_breaker(
    conn: DbConn, run_id: str, *, ceilings: CostCeilings
) -> CostBreakerOutcome:
    """Pure read of the durable counters; returns which ceiling (if any) is breached (§5.6).
    Per-run is checked first, then per-request, then candidate count."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, cost_units, candidates_explored "
            "FROM run_workflow_state WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        request_id, run_cost, candidates = row
    req_cost = request_cost(conn, request_id)
    if ceilings.per_run is not None and run_cost >= ceilings.per_run:
        return CostBreakerOutcome(True, "per_run", run_cost, req_cost)
    if ceilings.per_request is not None and req_cost >= ceilings.per_request:
        return CostBreakerOutcome(True, "per_request", run_cost, req_cost)
    if ceilings.max_candidates is not None and candidates >= ceilings.max_candidates:
        return CostBreakerOutcome(True, "max_candidates", run_cost, req_cost)
    return CostBreakerOutcome(False, None, run_cost, req_cost)


def trip_cost_breaker(
    conn: DbConn, run_id: str, *, ceiling: str, aggregate: str = "run"
) -> bool:
    """Auto-park on ceiling (§5.6): enqueue exactly ONE parking work message (idempotent by a
    deterministic message_id) mirroring the §5.5 ladder's auto-park rung. Returns True if a
    new park message was enqueued, False if the run was already parked for this ceiling."""
    message_id = f"cost-breaker:{run_id}:{ceiling}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'runtime.auto_park', %s) ON CONFLICT (message_id) DO NOTHING",
            (message_id, f"{aggregate}:{run_id}",
             Jsonb({"run_id": run_id, "reason": "cost_ceiling", "ceiling": ceiling})),
        )
        return cur.rowcount == 1
