from __future__ import annotations

import time

from featuregen.contracts import Disposition, HandlerContext, HandlerResult, NewEvent
from featuregen.runtime.dispatch import (
    HandlerRegistry,
    ProcessOutcome,
    process_one,
    recover_stuck,
)
from featuregen.runtime.outbox import (
    insert_outbox_message,
    make_queue_publisher,
    outbox_messages_for_events,
    relay_publish_batch,
)
from featuregen.runtime.queue import enqueue


class _Handler:
    """A run-scoped step handler emitting one STEP_DONE event."""

    name = "advance"
    version = 1
    timeout_seconds = 5.0

    def __init__(self, actor, prov, disposition=Disposition.OK, error=None):
        self._actor, self._prov = actor, prov
        self._disposition, self._error = disposition, error

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        ev = NewEvent(
            aggregate="run", aggregate_id=ctx.run_id, run_id=ctx.run_id,
            type="STEP_DONE", schema_version=1, payload={}, actor=self._actor,
            provenance=self._prov,
        )
        return HandlerResult(
            disposition=self._disposition,
            new_events=(ev,) if self._disposition == Disposition.OK else (),
            error=self._error,
        )


class _SlowHandler(_Handler):
    name = "slow"
    timeout_seconds = 0.05

    def handle(self, ctx):
        time.sleep(0.3)
        return super().handle(ctx)


def _pipe_trigger_to_queue(db, trigger) -> None:
    """Mirror the real path: derive outbox row from the trigger, relay -> queue."""
    for msg in outbox_messages_for_events([trigger]):
        insert_outbox_message(db, msg)
    relay_publish_batch(db, make_queue_publisher({"STEP_TRIGGER": "advance"}), owner="relay1")


def test_end_to_end_claim_handle_commit(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_e2e", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "ok"
    with db.cursor() as cur:
        cur.execute("SELECT type FROM events WHERE run_id='run_e2e' ORDER BY stream_version")
        assert [r[0] for r in cur.fetchall()] == ["STEP_TRIGGER", "STEP_DONE"]
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "done"


def test_idle_when_queue_empty(db) -> None:
    reg = HandlerRegistry()
    assert process_one(db, reg, owner="w1").status == "idle"


def test_duplicate_message_is_skipped(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_dup", type="STEP_TRIGGER")
    # mark already-processed so the dispatcher must no-op
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_messages (message_id, aggregate, aggregate_id, processed_seq) "
            "VALUES (%s, 'run', 'run_dup', 1)",
            (trigger.event_id,),
        )
    enqueue(db, message_id=trigger.event_id, partition_key="run:run_dup",
            handler="advance", payload={"event_id": trigger.event_id, "run_id": "run_dup"})
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "duplicate"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE run_id='run_dup' AND type='STEP_DONE'")
        assert cur.fetchone()[0] == 0  # no second effect


def test_retryable_reschedules(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_retry", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov, disposition=Disposition.RETRYABLE, error="transient"))
    assert process_one(db, reg, owner="w1").status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"


def test_permanent_dlqs(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_perm", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov, disposition=Disposition.PERMANENT, error="bad input"))
    assert process_one(db, reg, owner="w1").status == "permanent"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "dead"


def test_timeout_is_retryable(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_to", type="STEP_TRIGGER")
    for msg in outbox_messages_for_events([trigger]):
        insert_outbox_message(db, msg)
    relay_publish_batch(db, make_queue_publisher({"STEP_TRIGGER": "slow"}), owner="relay1")
    reg = HandlerRegistry()
    reg.register(_SlowHandler(actor, prov))
    assert process_one(db, reg, owner="w1").status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"


def test_register_rejects_duplicate_name(actor, prov) -> None:
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    import pytest

    with pytest.raises(ValueError):
        reg.register(_Handler(actor, prov))


def test_recover_stuck_reclaims_queue_and_outbox(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('q_stuck', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'dead', now() - interval '1 minute')"
        )
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('o_stuck', 'run:r1', 'T', '{}'::jsonb, "
            "'leased', 'dead', now() - interval '1 minute')"
        )
    assert recover_stuck(db) == (1, 1)


class _ReadConnWriterHandler(_Handler):
    """A misbehaving handler that tries to WRITE through ctx.read_conn (forbidden, §5.1)."""

    name = "advance"  # routed from STEP_TRIGGER by _pipe_trigger_to_queue

    def handle(self, ctx):
        with ctx.read_conn.cursor() as cur:
            cur.execute("CREATE TABLE handler_illegal_write (x int)")
        return super().handle(ctx)


def test_handler_write_through_read_conn_fails_fast(db, seed_run_event, actor, prov) -> None:
    """ctx.read_conn is opened READ-ONLY (§5.1): a handler that writes through it must fail fast
    (psycopg ReadOnlySqlTransaction) and persist NOTHING — every mutation must go through the
    returned HandlerResult and commit_step inside the step tx, never the read connection."""
    import psycopg
    import pytest

    trigger = seed_run_event("run_ro", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_ReadConnWriterHandler(actor, prov))
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        process_one(db, reg, owner="w1")

    # The illegal write never persisted (checked on an independent connection).
    db.rollback()
    with psycopg.connect(db.info.dsn) as probe:
        with probe.cursor() as cur:
            cur.execute("SELECT to_regclass('handler_illegal_write')")
            assert cur.fetchone()[0] is None


def test_occ_conflict_reschedules_without_partial_writes(db, seed_run_event, actor, prov) -> None:
    """A REAL OCC conflict (the run stream advanced after the step was triggered) must roll the
    step back inside its savepoint — no STEP_DONE event, no outbox row, no ledger row — and
    reschedule the message (status='ready'). This exercises process_one's
    `except ConcurrencyError ⇒ fail_retryable` branch and verifies the no-partial-writes
    invariant (§5.1)."""
    trigger = seed_run_event("run_occ", type="STEP_TRIGGER")  # stream_version 1
    _pipe_trigger_to_queue(db, trigger)
    # concurrently advance the run stream so the step's expected_version (1) is now stale
    seed_run_event("run_occ", type="STEP_NEXT", expected_version=1)  # stream_version 2
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"  # rescheduled, not lost
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id='run_occ' AND type='STEP_DONE'"
        )
        assert cur.fetchone()[0] == 0  # no partial event from the rolled-back step
        cur.execute(
            "SELECT count(*) FROM processed_messages WHERE message_id=%s", (trigger.event_id,)
        )
        assert cur.fetchone()[0] == 0  # no ledger row
