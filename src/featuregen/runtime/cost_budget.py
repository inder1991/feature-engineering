from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn


@dataclass(frozen=True, slots=True)
class CostCeilings:
    per_run: Decimal | None = None
    per_request: Decimal | None = None
    max_candidates: int | None = None


class CostConfigError(ValueError):
    """A cost-ceiling env value is malformed (non-numeric, non-finite, or negative)."""


def current_cost_ceilings() -> CostCeilings:
    """Resolve the active §5.6 cost ceilings from config (env). An unset ceiling is None (that
    limit is disabled). A deployment sets FEATUREGEN_COST_PER_RUN / _PER_REQUEST / _MAX_CANDIDATES.

    VALIDATES each value (finite, non-negative) and raises CostConfigError on a malformed one, so a
    typo is caught at STARTUP (run_forever calls this once) rather than deep inside a finalize
    transaction where it would roll back an already-executed external side effect (SP-0.5 r2 review
    #3). NaN is explicitly rejected — a NaN ceiling would make every `>=` comparison silently false."""

    def _dec(name: str) -> Decimal | None:
        raw = os.environ.get(name)
        if not raw:
            return None
        try:
            val = Decimal(raw)
        except ArithmeticError as exc:
            raise CostConfigError(f"{name}={raw!r} is not a valid number") from exc
        if not val.is_finite() or val < 0:
            raise CostConfigError(f"{name}={raw!r} must be finite and non-negative")
        return val

    mc_raw = os.environ.get("FEATUREGEN_MAX_CANDIDATES")
    max_candidates: int | None = None
    if mc_raw:
        try:
            max_candidates = int(mc_raw)
        except ValueError as exc:
            raise CostConfigError(f"FEATUREGEN_MAX_CANDIDATES={mc_raw!r} is not an integer") from exc
        if max_candidates < 0:
            raise CostConfigError(f"FEATUREGEN_MAX_CANDIDATES={mc_raw!r} must be non-negative")
    return CostCeilings(
        per_run=_dec("FEATUREGEN_COST_PER_RUN"),
        per_request=_dec("FEATUREGEN_COST_PER_REQUEST"),
        max_candidates=max_candidates,
    )


@dataclass(frozen=True, slots=True)
class CostBreakerOutcome:
    tripped: bool
    ceiling: str | None = None  # per_run|per_request|max_candidates
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


def check_cost_breaker(conn: DbConn, run_id: str, *, ceilings: CostCeilings) -> CostBreakerOutcome:
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


def trip_cost_breaker(conn: DbConn, run_id: str, *, ceiling: str, aggregate: str = "run") -> bool:
    """Auto-park on ceiling (§5.6): enqueue exactly ONE parking work message (idempotent by a
    deterministic message_id) mirroring the §5.5 ladder's auto-park rung. Returns True if a
    new park message was enqueued, False if the run was already parked for this ceiling."""
    message_id = f"cost-breaker:{run_id}:{ceiling}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'runtime.auto_park', %s) ON CONFLICT (message_id) DO NOTHING",
            (
                message_id,
                f"{aggregate}:{run_id}",
                Jsonb({"run_id": run_id, "reason": "cost_ceiling", "ceiling": ceiling}),
            ),
        )
        return cur.rowcount == 1
