"""Bounded-exhaustion park check — relocated from `intake/commands.py` when SP-2 intake was retired.

Not SP-2-specific: the durable runtime uses it to avoid re-parking an already-parked SP-0 run (the
auto-park is a direct park outside execute_command, so this idempotency check stops a re-drive from
appending a duplicate RUN_PARKED).
"""
from __future__ import annotations

from featuregen.contracts import DbConn
from featuregen.events.store import load_stream


def run_is_parked(conn: DbConn, run_id: str) -> bool:
    """True iff the SP-0 run is CURRENTLY parked (a RUN_PARKED not since cleared by RUN_UNPARKED)."""
    parked = False
    for e in load_stream(conn, "run", run_id):
        if e.type == "RUN_PARKED":
            parked = True
        elif e.type == "RUN_UNPARKED":
            parked = False
    return parked
