from __future__ import annotations

from datetime import datetime

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import NewTimer
from featuregen.runtime.timers import schedule_timer


def schedule_expiry(
    conn: DbConn, fact_key: str, confirmed_event_id: str, expires_at: datetime
) -> str:
    """Arm the SP-0 `overlay_expiry` timer on a confirmed fact's stream (decision 5). The timer
    carries the `confirmed_event_id` in its payload so the Phase 7 `fire_due_overlay_expiries`
    poller can CAS on it. Idempotency-keyed on `(fact_key, confirmed_event_id)` so re-confirming
    the same event is a no-op. NOTE: this is the ONLY symbol in `freshness.py` for now — Phase 7
    (Task 7.1) extends THIS file with `fire_due_overlay_expiries`/`detect_catalog_changes`/
    `open_reverify_task`."""
    return schedule_timer(
        conn,
        "overlay_fact",
        fact_key,
        NewTimer(
            kind="overlay_expiry",
            fire_at=expires_at,
            idempotency_key=f"overlay_expiry:{fact_key}:{confirmed_event_id}",
            payload={"confirmed_event_id": confirmed_event_id},
        ),
    )
