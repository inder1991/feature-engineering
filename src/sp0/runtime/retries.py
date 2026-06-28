from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sp0.contracts import DbConn, Disposition


def compute_backoff(
    attempts: int, *, base_seconds: float, cap_seconds: float, rng: random.Random
) -> float:
    """Exponential backoff with FULL jitter (§5.6). `attempts` = number already made (>=1).
    Returns a delay uniformly drawn from [0, min(cap, base * 2**(attempts-1))]."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    window = min(cap_seconds, base_seconds * (2 ** (attempts - 1)))
    return rng.uniform(0.0, window)


def within_budget(
    *,
    attempts: int,
    max_attempts: int,
    started_at: datetime,
    now: datetime,
    max_elapsed_seconds: float,
) -> bool:
    """True if another delivery retry is permitted: under BOTH the attempt budget and the
    max_elapsed_time cap (§5.6)."""
    if attempts >= max_attempts:
        return False
    if (now - started_at).total_seconds() >= max_elapsed_seconds:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _TableSpec:
    table: str
    available_col: str          # 'available_at' (queue) | 'next_attempt_at' (outbox)
    ready_status: str           # 'ready' (queue) | 'pending' (outbox)
    dead_status: str = "dead"


QUEUE_SPEC = _TableSpec("queue", "available_at", "ready")
OUTBOX_SPEC = _TableSpec("outbox", "next_attempt_at", "pending")


def record_delivery_outcome(
    conn: DbConn,
    spec: _TableSpec,
    row_id: int,
    *,
    disposition: Disposition,
    error: Optional[str],
    started_at: datetime,
    now: datetime,
    base_seconds: float,
    cap_seconds: float,
    max_elapsed_seconds: float,
    rng: random.Random,
) -> str:
    """Apply §5.6 delivery-retry semantics to one queue/outbox row. PERMANENT => DLQ
    ('dead') immediately (no retry). RETRYABLE => reschedule with backoff+jitter if still
    within BOTH the per-message attempt budget and max_elapsed_time, else DLQ.
    Disposition.OK is REJECTED with ValueError: OK is not a delivery failure — the caller
    marks the row done/sent and never calls this. Returns the new row status."""
    if disposition is Disposition.OK:
        raise ValueError(
            "record_delivery_outcome handles FAILED deliveries only; Disposition.OK is not "
            "retryable — the caller marks the row done/sent instead (§5.6)."
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT attempts, max_attempts FROM {spec.table} WHERE id = %s FOR UPDATE",
            (row_id,),
        )
        attempts, max_attempts = cur.fetchone()
        attempts += 1
        if disposition is Disposition.PERMANENT or not within_budget(
            attempts=attempts, max_attempts=max_attempts, started_at=started_at, now=now,
            max_elapsed_seconds=max_elapsed_seconds,
        ):
            cur.execute(
                f"UPDATE {spec.table} SET status=%s, attempts=%s, last_error=%s WHERE id=%s",
                (spec.dead_status, attempts, error, row_id),
            )
            return spec.dead_status
        delay = compute_backoff(attempts, base_seconds=base_seconds, cap_seconds=cap_seconds, rng=rng)
        next_at = now + timedelta(seconds=delay)
        cur.execute(
            f"UPDATE {spec.table} SET status=%s, attempts=%s, last_error=%s, "
            f"{spec.available_col}=%s WHERE id=%s",
            (spec.ready_status, attempts, error, next_at, row_id),
        )
        return spec.ready_status
