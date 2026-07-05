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


@pytest.fixture(autouse=True)
def _reset_counters():
    """`counters` is a process-global singleton; reset it around each test so leaked-connection /
    metric counts from one test never bleed into another (e.g. the leaked-cap-halt test)."""
    from featuregen.runtime.observability import counters

    counters.reset()
    yield
    counters.reset()


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


def test_run_worker_once_emits_depth_and_lag_gauges(db, seeded_pipeline) -> None:
    """Each tick must publish queue-depth and per-projection lag gauges so a health endpoint can
    see backlog/staleness, not just counters (SP-0.5 round-2)."""
    from featuregen.runtime.observability import counters
    from featuregen.runtime.worker import run_worker_once

    reg, projections = seeded_pipeline
    counters.reset()
    run_worker_once(db, reg, projections, owner="w1", now=_now())
    gauges = counters.snapshot()["gauges"]
    assert "queue.depth" in gauges
    assert any(k.startswith("projection.lag.") for k in gauges)


def test_run_worker_once_halts_claiming_past_leaked_conn_cap(db, seeded_pipeline) -> None:
    """Once abandoned (leaked) handler connections exceed the cap, the worker stops claiming new
    work and surfaces it LOUD, so a wedged-handler leak is bounded, not unbounded (SP-0.5 r2)."""
    from featuregen.runtime.observability import counters
    from featuregen.runtime.worker import run_worker_once

    reg, projections = seeded_pipeline
    counters.reset()
    counters.incr("dispatch.leaked_connections", 100)  # simulate many leaked connections
    tick = run_worker_once(db, reg, projections, owner="w1", now=_now(), leaked_conn_cap=10)

    assert tick.queue_processed == 0  # claiming halted despite a ready queue item
    assert counters.snapshot()["counters"].get("worker.leaked_cap_halt", 0) >= 1


def test_relay_publisher_from_env_wires_route_policy(db, monkeypatch, tmp_path) -> None:
    """The production worker path must CONFIGURE the outbox route policy via a JSON routes file (per
    the plan) + a route-required list, else the policy is unreachable in production (SP-0.5 r2 #5)."""
    import json as _json

    from featuregen.runtime.outbox import OutboxMessage, UnroutedOutboxTopic
    from featuregen.runtime.worker import _relay_publisher_from_env

    routes_file = tmp_path / "routes.json"
    routes_file.write_text(_json.dumps({"STEP_TRIGGER": "h"}))
    monkeypatch.setenv("FEATUREGEN_RELAY_ROUTES", str(routes_file))
    monkeypatch.setenv("FEATUREGEN_RELAY_REQUIRED", "MUST_ROUTE")
    publish = _relay_publisher_from_env()

    with pytest.raises(UnroutedOutboxTopic):  # route-required + unrouted -> loud
        publish(db, OutboxMessage("m1", "p", "MUST_ROUTE", {}, "e1"))
    publish(db, OutboxMessage("m2", "run:r", "STEP_TRIGGER", {}, "e2"))  # configured -> enqueues
    assert db.execute("SELECT handler FROM queue WHERE message_id='m2'").fetchone()[0] == "h"
    publish(db, OutboxMessage("m3", "p", "SOME_EVENT", {}, "e3"))  # unrouted non-required -> drains
    assert db.execute("SELECT count(*) FROM queue WHERE message_id='m3'").fetchone()[0] == 0


def test_relay_publisher_fails_loud_on_missing_routes_file(monkeypatch) -> None:
    # A set-but-unreadable FEATUREGEN_RELAY_ROUTES must fail LOUD, not silently load an empty map
    # (SP-0.5 r2 review #5 — a deployment following the plan would otherwise route nothing).
    from featuregen.runtime.worker import _relay_publisher_from_env

    monkeypatch.setenv("FEATUREGEN_RELAY_ROUTES", "/nonexistent/relay-routes.json")
    with pytest.raises(FileNotFoundError):
        _relay_publisher_from_env()


def test_relay_publisher_programmatic_override(db) -> None:
    from featuregen.runtime.outbox import OutboxMessage
    from featuregen.runtime.worker import _relay_publisher_from_env

    publish = _relay_publisher_from_env({"STEP_TRIGGER": "h2"})  # overrides the env file
    publish(db, OutboxMessage("mo", "run:r", "STEP_TRIGGER", {}, "eo"))
    assert db.execute("SELECT handler FROM queue WHERE message_id='mo'").fetchone()[0] == "h2"


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


def test_control_signal_survives_process_one_first_then_poller_parks(db) -> None:
    """Multi-worker control-signal race guard. Worker B's process_one, running BEFORE worker A's
    control poller, must NOT claim or DLQ a runtime.auto_park row (claim_one excludes
    CONTROL_SIGNAL_HANDLERS). The row stays 'ready'; worker A's control poller then parks the run
    (RUN_PARKED) — the safety action is never defeated on any worker ordering."""
    from featuregen.runtime.dispatch import process_one
    from featuregen.runtime.queue import enqueue
    from featuregen.runtime.worker import compose, drain_control_signals

    reg, _projections = compose(db)
    enqueue(
        db,
        message_id="cb:runB:hard",
        partition_key="run:runB",
        handler="runtime.auto_park",
        payload={"run_id": "runB", "reason": "cost_ceiling"},
    )

    # Worker B drains the general queue FIRST. The only ready row is a control signal, which
    # claim_one excludes, so process_one is idle — it does not steal or DLQ the park.
    outcome = process_one(db, reg, owner="workerB")
    assert outcome.status == "idle"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id = 'cb:runB:hard'")
        assert cur.fetchone()[0] == "ready"  # NOT stolen, NOT DLQ'd

    # Worker A's control poller then owns and parks the run.
    parked = drain_control_signals(db)
    assert parked == 1
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE run_id = 'runB' AND type = 'RUN_PARKED'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT status FROM queue WHERE message_id = 'cb:runB:hard'")
        assert cur.fetchone()[0] == "done"


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


def test_advisory_lock_sites_work_on_autocommit_connection(
    autocommit_worker_conn, actor, prov
) -> None:
    """append_event and record_security_event take a TRANSACTION-scoped advisory lock; on the
    daemon's AUTOCOMMIT connection the lock is only held if it runs inside a transaction (a bare
    lock statement self-commits and releases it before the read/insert it guards). Both now open a
    real transaction on autocommit — verify they produce correct, chained results there (SP-0.5
    round-2 advisory-lock autocommit safety). NOTE: this exercises the autocommit path for
    no-regression; the concurrency race the lock prevents is not itself deterministically unit-tested."""
    from featuregen.contracts import NewEvent
    from featuregen.events import append_event
    from featuregen.security.audit import record_security_event, verify_chain

    ac = autocommit_worker_conn
    # append_event on autocommit: two appends produce ascending, chained global_seq (no gap/fork).
    e1 = append_event(
        ac,
        NewEvent(aggregate="run", aggregate_id="run_al", run_id="run_al", type="STEP_TRIGGER",
                 schema_version=1, payload={}, actor=actor, provenance=prov),
        expected_version=0, table_version=1,
    )
    e2 = append_event(
        ac,
        NewEvent(aggregate="run", aggregate_id="run_al", run_id="run_al", type="STEP_TRIGGER",
                 schema_version=1, payload={}, actor=actor, provenance=prov),
        expected_version=1, table_version=1,
    )
    assert e2.global_seq > e1.global_seq

    # record_security_event on autocommit: two appends produce a valid, linked hash chain.
    record_security_event(ac, event_type="COMMAND_DENIED", actor=actor,
                          attempted_action="activate", decision="denied", reason="r1")
    record_security_event(ac, event_type="COMMAND_DENIED", actor=actor,
                          attempted_action="deprecate", decision="denied", reason="r2")
    assert verify_chain(ac) is True


def test_run_worker_once_fires_a_due_timer_on_autocommit(autocommit_worker_conn) -> None:
    """fire_timer holds a SELECT ... FOR UPDATE across the enqueue + mark-fired statements; on the
    AUTOCOMMIT daemon connection that lock releases at statement end unless the tick wraps it in a
    transaction. Prove the wrapping works for the TIMER stage (not only run_projection): a due timer
    fires (timers_fired >= 1) with no stage error on the real daemon connection."""
    from datetime import timedelta

    from featuregen.contracts import NewTimer
    from featuregen.runtime.timers import schedule_timer
    from featuregen.runtime.worker import compose, run_worker_once

    ac = autocommit_worker_conn
    reg, projections = compose(ac)
    now = _now()
    schedule_timer(
        ac,
        "run",
        "run_tmr1",
        NewTimer(
            kind="reminder",
            fire_at=now - timedelta(seconds=5),  # due in the past
            idempotency_key="tmr:run_tmr1:reminder",
            payload={},
        ),
    )

    tick = run_worker_once(ac, reg, projections, owner="w1", now=now)

    assert tick.errors == 0
    assert tick.timers_fired >= 1


def test_run_worker_once_fires_a_due_overlay_expiry_on_autocommit(autocommit_worker_conn) -> None:
    """fire_due_overlay_expiries also holds a FOR UPDATE lock across statements; prove the tick runs
    it durably on the AUTOCOMMIT daemon connection. A due overlay_expiry timer expires a VERIFIED
    fact (overlay_expiries >= 1) with no stage error — covering the overlay stage, not just
    run_projection."""
    from dataclasses import asdict
    from datetime import timedelta

    from tests.featuregen._helpers import mint_test_identity

    from featuregen.overlay.catalog import (
        CatalogObject,
        FixtureCatalog,
        _clear_catalog_adapter,
        register_catalog_adapter,
    )
    from featuregen.overlay.expiry import schedule_expiry
    from featuregen.overlay.facts import OVERLAY_FACT_CONFIRMED, OVERLAY_FACT_PROPOSED
    from featuregen.overlay.identity import (
        CatalogObjectRef,
        display_object_ref,
        fact_key,
        proposal_fingerprint,
    )
    from featuregen.overlay.store import append_overlay_event
    from featuregen.runtime.worker import compose, run_worker_once

    ac = autocommit_worker_conn
    reg, projections = compose(ac)  # registers overlay event schemas so the appends validate
    now = _now()

    ref = CatalogObjectRef(
        catalog_source="pg:core",
        object_kind="table",
        schema="core",
        table="customers",
        column=None,
    )
    key = fact_key(ref, "grain", None)
    value = {"columns": ["customer_id"], "is_unique": True}
    proposer = mint_test_identity(subject="user:proposer", role_claims=("data_owner",))
    proposed = append_overlay_event(
        ac,
        fact_key=key,
        type=OVERLAY_FACT_PROPOSED,
        actor=proposer,
        expected_version=0,
        payload={
            "catalog_object_ref": asdict(ref),
            "object_ref": display_object_ref(ref),
            "fact_type": "grain",
            "use_case": None,
            "proposed_value": value,
            "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": proposer.subject,
        },
    )
    owner = mint_test_identity(subject="user:owner-a", role_claims=("data_owner",))
    confirmed = append_overlay_event(
        ac,
        fact_key=key,
        type=OVERLAY_FACT_CONFIRMED,
        actor=owner,
        payload={
            "value": value,
            "confirmers": [{"subject": "user:owner-a", "role": "data_owner"}],
            "expires_at": (now + timedelta(days=30)).isoformat(),
            "confirms_event_id": proposed.event_id,
        },
    )
    adapter = FixtureCatalog(catalog_source="pg:core")
    adapter.add_object(
        CatalogObject(
            object_ref=display_object_ref(ref),
            object_kind="table",
            schema="core",
            table="customers",
            column=None,
            data_type=None,
            native_oid="oid-cust",
        )
    )
    adapter.set_owner(ref, "user:owner-a")
    register_catalog_adapter(adapter)  # process-global; cleared below so it never leaks
    try:
        # confirm_fact would have armed this timer; arm it directly, due in the past.
        schedule_expiry(ac, key, confirmed.event_id, now - timedelta(seconds=5))
        tick = run_worker_once(ac, reg, projections, owner="w1", now=now)
    finally:
        _clear_catalog_adapter()

    assert tick.errors == 0
    assert tick.overlay_expiries >= 1


def test_run_forever_runs_one_tick_then_exits_on_shutdown(
    _throwaway_autocommit_db, monkeypatch
) -> None:
    """run_forever opens ONE autocommit connection, composes, and loops run_worker_once until the
    shutdown_event is set. Cover the loop/shutdown path: a monkeypatched tick sets the event after a
    single pass, so the loop runs exactly one tick then exits cleanly (connection closed in finally,
    no signal handlers installed because an event was injected)."""
    import threading

    import featuregen.runtime.worker as worker
    from featuregen.runtime.worker import WorkerTick, run_forever

    shutdown = threading.Event()
    calls = {"n": 0}

    def _fake_tick(conn, registry, projections, *, owner, now, **_kw):
        calls["n"] += 1
        shutdown.set()  # ask the loop to stop after this single tick
        return WorkerTick(0, 0, 0, 0, 0, (0, 0), 0, 0)

    monkeypatch.setattr(worker, "run_worker_once", _fake_tick)

    run_forever(_throwaway_autocommit_db, interval=0.0, shutdown_event=shutdown, owner="w-test")

    assert calls["n"] == 1  # exactly one tick ran, then the loop exited cleanly on the event


def test_run_forever_survives_a_fatal_tick_and_counts_it(
    _throwaway_autocommit_db, monkeypatch
) -> None:
    """M6: a `run_worker_once` that RAISES must NOT propagate out of `run_forever` — the loop's
    fatal-tick backstop catches it, increments `worker.tick.fatal`, and still exits cleanly on the
    shutdown_event. A refactor deleting the try/except would let the exception escape and fail this
    test (the finding: the backstop was untested)."""
    import threading

    import featuregen.runtime.worker as worker
    from featuregen.runtime.observability import counters
    from featuregen.runtime.worker import run_forever

    counters.reset()
    shutdown = threading.Event()
    calls = {"n": 0}

    def _boom_tick(conn, registry, projections, *, owner, now, **_kw):
        calls["n"] += 1
        shutdown.set()  # stop the loop after this single (failing) tick
        raise RuntimeError("tick exploded")

    monkeypatch.setattr(worker, "run_worker_once", _boom_tick)

    # No exception must escape: if it did, this call would raise and fail the test.
    run_forever(_throwaway_autocommit_db, interval=0.0, shutdown_event=shutdown, owner="w-fatal")

    assert calls["n"] == 1  # the tick ran and raised, but the daemon loop survived it
    assert counters.snapshot()["counters"].get("worker.tick.fatal", 0) >= 1


def test_run_forever_installs_signal_handlers_when_no_event(
    _throwaway_autocommit_db, monkeypatch
) -> None:
    """M6: with `shutdown_event=None`, `run_forever` must install SIGINT/SIGTERM handlers so a real
    signal triggers graceful shutdown. Assert the handlers CHANGED (then restore them). Hermetic: a
    fake tick raises a BaseException to unwind the loop instead of delivering a real signal."""
    import signal

    import featuregen.runtime.worker as worker
    from featuregen.runtime.worker import run_forever

    class _StopLoop(BaseException):
        """Not an Exception, so run_forever's fatal-tick backstop won't catch it — it unwinds
        the loop cleanly through the finally (conn.close)."""

    orig_int = signal.getsignal(signal.SIGINT)
    orig_term = signal.getsignal(signal.SIGTERM)
    seen: dict[str, object] = {}

    def _capture_then_stop(conn, registry, projections, *, owner, now, **_kw):
        # By now run_forever must have installed its handlers (shutdown_event was None).
        seen["int"] = signal.getsignal(signal.SIGINT)
        seen["term"] = signal.getsignal(signal.SIGTERM)
        raise _StopLoop

    monkeypatch.setattr(worker, "run_worker_once", _capture_then_stop)
    try:
        with pytest.raises(_StopLoop):
            run_forever(_throwaway_autocommit_db, interval=0.0, shutdown_event=None, owner="w-sig")
    finally:
        signal.signal(signal.SIGINT, orig_int)
        signal.signal(signal.SIGTERM, orig_term)

    assert seen["int"] is not orig_int  # a SIGINT handler was installed
    assert seen["term"] is not orig_term  # a SIGTERM handler was installed
    assert callable(seen["int"]) and callable(seen["term"])


def test_migrate_subcommand_applies_migrations(_dsn) -> None:
    """`python -m featuregen migrate` applies migrations (idempotently, against the already-set-up
    test DB) — the production migration runner the Task-9 review flagged as missing."""
    from featuregen.__main__ import main

    assert main(["migrate", "--dsn", _dsn]) == 0


def test_run_drift_scan_skips_and_cadence_gates(db):
    # SP-1.5 Task 4: the drift stage skips (0) with no adapter/config, runs when due, and skips
    # within the scan interval (not every tick).
    from datetime import UTC, datetime, timedelta

    from featuregen.overlay.catalog import (
        FixtureCatalog,
        _clear_catalog_adapter,
        register_catalog_adapter,
    )
    from featuregen.overlay.catalog_changes import drift_watermark
    from featuregen.overlay.config import (
        OverlayConfig,
        _clear_overlay_config,
        register_overlay_config,
    )
    from featuregen.runtime.worker import _run_drift_scan

    now = datetime(2026, 6, 1, tzinfo=UTC)
    assert _run_drift_scan(db, now=now) == 0  # no adapter -> skip-loud

    register_catalog_adapter(FixtureCatalog(catalog_source="pg:core"))
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    ))
    try:
        _run_drift_scan(db, now=now)  # first run (no watermark) -> runs
        assert drift_watermark(db, "pg:core") == now
        _run_drift_scan(db, now=now + timedelta(minutes=5))  # within interval -> skip
        assert drift_watermark(db, "pg:core") == now
        later = now + timedelta(minutes=20)  # past interval -> runs again
        _run_drift_scan(db, now=later)
        assert drift_watermark(db, "pg:core") == later
    finally:
        _clear_catalog_adapter()
        _clear_overlay_config()


def test_run_drift_scan_skips_when_overlay_projection_lags(db):
    # deep-dive BLOCKER #1: a drift scan while the overlay projection LAGS would find zero dependents
    # for a just-confirmed fact and launder the drop. It must skip (watermark un-advanced) until the
    # projection catches up.
    from datetime import UTC, datetime, timedelta

    from featuregen.contracts import IdentityEnvelope
    from featuregen.overlay import facts
    from featuregen.overlay.catalog import (
        FixtureCatalog,
        _clear_catalog_adapter,
        register_catalog_adapter,
    )
    from featuregen.overlay.catalog_changes import drift_watermark
    from featuregen.overlay.config import (
        OverlayConfig,
        _clear_overlay_config,
        register_overlay_config,
    )
    from featuregen.overlay.bootstrap import register_overlay
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.overlay.store import append_overlay_event
    from featuregen.projections.runner import run_projection
    from featuregen.runtime.handlers import HandlerRegistry
    from featuregen.runtime.worker import _run_drift_scan

    now = datetime(2026, 6, 1, tzinfo=UTC)
    register_overlay(HandlerRegistry())  # register overlay event schemas so the append validates
    register_catalog_adapter(FixtureCatalog(catalog_source="pg:core"))
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.0, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(minutes=60),
        profiler_require_restricted_role=False,
    ))
    try:
        # Append an overlay event but do NOT run the projection -> the overlay projection now lags.
        append_overlay_event(
            db, fact_key="fk-lag", type=facts.OVERLAY_FACT_PROPOSED,
            actor=IdentityEnvelope(subject="user:o", actor_kind="human", authenticated=True,
                                   auth_method="oidc", role_claims=("data_owner",)),
            expected_version=0,
            payload={"catalog_object_ref": {"catalog_source": "pg:core", "object_kind": "table",
                                            "schema": "public", "table": "t"},
                     "object_ref": "public.t", "fact_type": "grain",
                     "proposed_value": {"columns": ["id"], "is_unique": True},
                     "proposal_fingerprint": "fp", "proposed_by": "user:o"},
        )
        assert _run_drift_scan(db, now=now) == 0          # projection lags -> skip
        assert drift_watermark(db, "pg:core") is None     # watermark NOT advanced

        run_projection(db, OverlayProjection())           # projection catches up
        _run_drift_scan(db, now=now)
        assert drift_watermark(db, "pg:core") == now      # now the scan runs + advances
    finally:
        _clear_catalog_adapter()
        _clear_overlay_config()
