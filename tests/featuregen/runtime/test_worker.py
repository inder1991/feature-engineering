from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.contracts import Disposition, HandlerContext, HandlerResult, NewEvent
from featuregen.runtime.outbox import (
    insert_outbox_message,
    make_queue_publisher,
    outbox_messages_for_events,
    relay_publish_batch,
)


class _AdvanceHandler:
    """A minimal run-scoped step handler emitting one STEP_DONE event, so a worker tick has a
    real, registry-backed handler to drive the seeded STEP_TRIGGER to completion."""

    name = "advance"
    version = 1
    timeout_seconds = 5.0

    def __init__(self, actor, prov):
        self._actor, self._prov = actor, prov

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        ev = NewEvent(
            aggregate="run",
            aggregate_id=ctx.run_id,
            run_id=ctx.run_id,
            type="STEP_DONE",
            schema_version=1,
            payload={},
            actor=self._actor,
            provenance=self._prov,
        )
        return HandlerResult(disposition=Disposition.OK, new_events=(ev,))


@pytest.fixture
def seeded_pipeline(db, actor, prov, seed_run_event):
    """Compose the production registry + projections, add a test `advance` step handler, and seed
    one STEP_TRIGGER -> queue row (mirrors the real outbox->relay->queue path)."""
    from featuregen.runtime.worker import compose

    reg, projections = compose(db)
    reg.register(_AdvanceHandler(actor, prov))
    trigger = seed_run_event("run_worker1", type="STEP_TRIGGER")
    for msg in outbox_messages_for_events([trigger]):
        insert_outbox_message(db, msg)
    relay_publish_batch(db, make_queue_publisher({"STEP_TRIGGER": "advance"}), owner="relay1")
    return reg, projections


def _now() -> datetime:
    return datetime.now(UTC)


def test_run_worker_once_drives_a_queued_step(db, seeded_pipeline) -> None:
    """One worker tick claims + processes a ready queue item, advancing the run — proving the
    daemon actually drives work (review BLOCKER #3). Bounded and non-blocking (no sleeps)."""
    from featuregen.runtime.worker import run_worker_once

    reg, projections = seeded_pipeline
    tick = run_worker_once(db, reg, projections, owner="w1", now=_now())

    assert tick.queue_processed >= 1
    assert tick.errors == 0
    with db.cursor() as cur:
        cur.execute("SELECT type FROM events WHERE run_id='run_worker1' ORDER BY stream_version")
        assert [r[0] for r in cur.fetchall()] == ["STEP_TRIGGER", "STEP_DONE"]


def test_run_worker_once_is_idle_on_empty(db, seeded_pipeline) -> None:
    """After draining to idle, the next tick does no queue work — no busy-spin."""
    from featuregen.runtime.worker import run_worker_once

    reg, projections = seeded_pipeline
    run_worker_once(db, reg, projections, owner="w1", now=_now())
    tick2 = run_worker_once(db, reg, projections, owner="w1", now=_now())

    assert tick2.queue_processed == 0


def test_run_worker_once_survives_a_failing_stage(db, seeded_pipeline, monkeypatch) -> None:
    """A fault in ONE stage increments the error counter and is logged, but never raises out of
    the tick or stalls the remaining stages (the seeded step still processes)."""
    import featuregen.runtime.worker as worker

    def _boom(_conn):
        raise RuntimeError("recover_stuck exploded")

    monkeypatch.setattr(worker, "recover_stuck", _boom)

    reg, projections = seeded_pipeline
    tick = worker.run_worker_once(db, reg, projections, owner="w1", now=_now())

    assert tick.errors >= 1
    assert tick.queue_processed >= 1  # the failing stage did not stall the rest of the tick


def test_run_worker_once_parks_a_cost_breaker_auto_park(db, seeded_pipeline) -> None:
    """A `runtime.auto_park` control signal carrying a run_id (the §5.6 cost-breaker path) is
    consumed by the dedicated control-signal stage and parks the run (RUN_PARKED) — it is NOT
    silently DLQ'd for a missing handler."""
    from featuregen.runtime.queue import enqueue
    from featuregen.runtime.worker import run_worker_once

    enqueue(
        db,
        message_id="cost-breaker:run_park1:hard",
        partition_key="run:run_park1",
        handler="runtime.auto_park",
        payload={"run_id": "run_park1", "reason": "cost_ceiling", "ceiling": "hard"},
    )
    reg, projections = seeded_pipeline
    tick = run_worker_once(db, reg, projections, owner="w1", now=_now())

    assert tick.errors == 0
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE run_id='run_park1' AND type='RUN_PARKED'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM queue WHERE message_id='cost-breaker:run_park1:hard'")
        assert cur.fetchone()[0] == "done"


def test_run_worker_once_surfaces_unconsumed_repair_exhausted(db, seeded_pipeline) -> None:
    """`runtime.repair_exhausted` has no wired consumer yet: it must be surfaced LOUD (counted +
    DLQ'd with an explanatory error), never silently completed or silently DLQ'd."""
    from featuregen.runtime.observability import counters
    from featuregen.runtime.queue import enqueue
    from featuregen.runtime.worker import run_worker_once

    counters.reset()
    enqueue(
        db,
        message_id="repair-exhausted:run_rx1:0",
        partition_key="run:run_rx1",
        handler="runtime.repair_exhausted",
        payload={"run_id": "run_rx1", "reason": "repair_exhausted"},
    )
    reg, projections = seeded_pipeline
    run_worker_once(db, reg, projections, owner="w1", now=_now())

    with db.cursor() as cur:
        cur.execute("SELECT status, last_error FROM queue WHERE message_id='repair-exhausted:run_rx1:0'")
        status, last_error = cur.fetchone()
    assert status == "dead"
    assert "no consumer" in (last_error or "")
    snap = counters.snapshot()
    assert snap["counters"].get("control.unconsumed.runtime.repair_exhausted", 0) >= 1


@pytest.fixture
def autocommit_worker_conn(_dsn):
    """An isolated, throwaway, AUTOCOMMIT connection — the exact connection mode run_forever opens.
    Created on its own database so its committed side effects never leak into the shared test DB."""
    import psycopg

    from featuregen.db.migrations import apply_migrations

    dbname = "fg_worker_ac_test"
    new_dsn = " ".join((f"dbname={dbname}" if p.startswith("dbname=") else p) for p in _dsn.split())

    admin = psycopg.connect(_dsn, autocommit=True)
    try:
        admin.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {dbname}")
    finally:
        admin.close()
    with psycopg.connect(new_dsn) as mconn:
        apply_migrations(mconn)

    ac = psycopg.connect(new_dsn, autocommit=True)
    try:
        yield ac
    finally:
        ac.close()
        admin = psycopg.connect(_dsn, autocommit=True)
        try:
            admin.execute(f"DROP DATABASE IF EXISTS {dbname} WITH (FORCE)")
        finally:
            admin.close()


def test_run_worker_once_advances_projections_on_autocommit(
    autocommit_worker_conn, actor, prov
) -> None:
    """run_forever opens an AUTOCOMMIT connection; `run_projection` uses SAVEPOINT, which raises
    outside a transaction. The tick must run its transaction-requiring stages inside a transaction so
    the projection stage advances (does not error) on the real daemon connection."""
    from featuregen.contracts import NewEvent
    from featuregen.events import append_event
    from featuregen.runtime.worker import compose, run_worker_once

    ac = autocommit_worker_conn
    reg, projections = compose(ac)
    # One committed run event so the projection stage actually reaches SAVEPOINT proj_apply.
    append_event(
        ac,
        NewEvent(
            aggregate="run",
            aggregate_id="run_ac1",
            run_id="run_ac1",
            type="STEP_TRIGGER",
            schema_version=1,
            payload={},
            actor=actor,
            provenance=prov,
        ),
        expected_version=0,
        table_version=1,
    )

    tick = run_worker_once(ac, reg, projections, owner="w1", now=_now())

    assert tick.errors == 0
    assert tick.projections_advanced >= 1  # the seeded event was consumed by the projections


def test_migrate_subcommand_applies_migrations(_dsn) -> None:
    """`python -m featuregen migrate` applies migrations (idempotently, against the already-set-up
    test DB) — the production migration runner the Task-9 review flagged as missing."""
    from featuregen.__main__ import main

    assert main(["migrate", "--dsn", _dsn]) == 0
