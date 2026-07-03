"""The durable-runtime daemon: a bounded, testable worker tick + a graceful run-forever loop + the
composition root that wires the existing `register_*` / `seed_*` functions into the HandlerRegistry
and projection list the tick drives.

Review BLOCKER #3 + MAJOR #11: the durable runtime was LIBRARY-ONLY — nothing in production drove the
queue / relay / timers / projections, there was no entrypoint, and there was effectively no
logging/metrics. `run_worker_once` is ONE non-blocking pass over every runtime stage; `run_forever`
loops it with signal-based graceful shutdown; `__main__.py` exposes `worker` + `migrate` subcommands.

Safe to run only because Task 1 landed the poison-message guard: `process_one` DLQs an unknown/failing
message instead of hot-looping. Each stage here is additionally wrapped so one failing stage increments
a counted, logged error and NEVER stalls the tick.
"""

from __future__ import annotations

import os
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from featuregen.contracts import Command, Projection
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.expiry import fire_due_overlay_expiries
from featuregen.projections.runner import run_projection
from featuregen.runtime.dispatch import process_one, recover_stuck
from featuregen.runtime.observability import counters, log
from featuregen.runtime.outbox import OutboxMessage, make_queue_publisher, relay_publish_batch
from featuregen.runtime.queue import CONTROL_SIGNAL_HANDLERS, complete, fail_permanent
from featuregen.runtime.timers import fire_timer, poll_due_timers

# Outbox topic -> internal step handler. EMPTY by default: in this SP-0.5 slice the async steps
# (activation, timer commands, repair/cost-breaker parks) enqueue their queue rows DIRECTLY, so no
# event TYPE needs relay->queue fan-out. The relay stage still runs to keep the outbox drained; a
# deployment adds real routes (or swaps in an external-bus publisher) by passing `publish=`.
_DEFAULT_RELAY_ROUTE: dict[str, str] = {}

# Control-plane queue handlers are defined ONCE in runtime/queue.py as CONTROL_SIGNAL_HANDLERS: the
# dedicated control-signal stage below claims exactly those, and claim_one excludes exactly those,
# so the two consumers are complementary and can never drift. `_AUTO_PARK` is the one member the
# stage dispatches specially (park a run); every other member has no wired consumer yet (DLQ'd loud).
_AUTO_PARK = "runtime.auto_park"


@dataclass(frozen=True, slots=True)
class WorkerTick:
    queue_processed: int
    relay_published: int
    timers_fired: int
    overlay_expiries: int
    projections_advanced: int
    reclaimed: tuple[int, int]
    parked: int
    errors: int


def _control_actor():
    """A fail-closed service principal for the control-signal poller's park effect. Mirrors the
    overlay-expiry poller: `build_service_identity` yields an UNAUTHENTICATED machine principal
    (no token), which is safe here because `park_command` records the actor but authorizes nothing
    against it — and it is fail-SAFE (unattested, never forged)."""
    from featuregen.identity.build import build_service_identity

    return build_service_identity(
        subject="service:control-plane",
        role_claims=("worker",),
        attestation="auto-park-poller",
    )


def _park_run(conn: psycopg.Connection, *, run_id: str, actor, payload) -> None:
    """Park a run in response to a `runtime.auto_park` control signal (§5.5 auto-park-if-unanswered /
    §5.6 cost-breaker). Deferred imports keep the daemon free of an import-time dependency on the
    Phase-06 aggregate (mirrors dispatch.py). Idempotent: never re-parks an already-parked run and
    never parks a terminal run (mirrors intake/commands.py's bounded-exhaustion auto-park guard)."""
    from featuregen.aggregates.run_lifecycle import park_command, run_is_terminal
    from featuregen.intake.commands import _run_is_parked

    if run_is_terminal(conn, run_id) or _run_is_parked(conn, run_id):
        return
    park_command(
        conn,
        Command(
            action="park",
            aggregate="run",
            aggregate_id=run_id,
            args={
                "owner": payload.get("owner"),
                "waiting_on_fact": payload.get("waiting_on_fact"),
                "reason": payload.get("reason"),
            },
            actor=actor,
            idempotency_key=f"auto-park:{run_id}:{payload.get('reason', '')}",
        ),
    )


def drain_control_signals(
    conn: psycopg.Connection, *, actor_factory: Callable[[], object] = _control_actor
) -> int:
    """Dedicated poller for `CONTROL_SIGNAL_HANDLERS` (`runtime.auto_park` / `runtime.repair_exhausted`)
    — control-plane messages that are NOT registry step handlers (they carry no run event_id, so
    process_one would DLQ them for a missing handler; design §5.5's "auto-park if unanswered" would
    then never park).

    process_one can NEVER see these rows: `claim_one` EXCLUDES `CONTROL_SIGNAL_HANDLERS` at claim
    time (the same single-source constant this poller claims by), so on any worker ordering a general
    consumer cannot steal a control signal — this stage owns them exclusively:
      * `runtime.auto_park` WITH a run_id (the §5.6 cost-breaker path) -> park the run (RUN_PARKED),
        complete the row. A genuine, registered-command-backed consumer.
      * everything else (`runtime.repair_exhausted`, which needs a Phase-07 human failure gate; or an
        escalation-ladder auto_park rung carrying only a gate_task_id with no run_id) has no wired
        consumer yet -> surface LOUD (counted + logged) and route to the DLQ with an explanatory
        error. NEVER silent. Wiring those correctly is a documented SP-0.5 follow-up.

    Unlike `claim_one`, this poller does NOT apply the per-partition in-flight exclusion, so an
    `auto_park` can be processed while a step for the same run holds a lease. `park_command`'s OCC
    append is the accepted backstop: `_park_run` is idempotent (never re-parks an already-parked or
    terminal run) and OCC-guarded, so a concurrent step and this park cannot both take effect twice.

    `actor_factory` is invoked LAZILY — only when a parkable row exists — so an empty control queue
    never builds a service principal. One transaction (`conn.transaction()`): FOR UPDATE SKIP LOCKED
    holds the row locks until commit, and the park + queue-status write commit atomically (multiple
    control pollers across workers therefore never double-process). Returns the number of runs parked."""
    parked = 0
    actor = None
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT id, message_id, handler, payload FROM queue "
                "WHERE status = 'ready' AND available_at <= now() "
                "AND handler = ANY(%s) "
                "ORDER BY priority, available_at, id FOR UPDATE SKIP LOCKED",
                (list(CONTROL_SIGNAL_HANDLERS),),
            )
            rows = cur.fetchall()
        for row in rows:
            payload = row["payload"] or {}
            run_id = payload.get("run_id")
            if row["handler"] == _AUTO_PARK and run_id:
                if actor is None:
                    actor = actor_factory()
                _park_run(conn, run_id=run_id, actor=actor, payload=payload)
                complete(conn, row["id"])
                counters.incr("control.auto_park.parked")
                log(
                    "control.auto_park.parked",
                    run_id=run_id,
                    reason=payload.get("reason"),
                    message_id=row["message_id"],
                )
                parked += 1
            else:
                counters.incr(f"control.unconsumed.{row['handler']}")
                log(
                    "control.signal.unconsumed",
                    level="warning",
                    handler=row["handler"],
                    run_id=run_id,
                    message_id=row["message_id"],
                    payload=payload,
                )
                fail_permanent(
                    conn,
                    row["id"],
                    error=f"no consumer wired for {row['handler']} (SP-0.5 follow-up)",
                )
    return parked


def _advance_overlay_expiries(conn: psycopg.Connection, *, now: datetime) -> int:
    """Run the overlay-expiry poller, skipping (NOT erroring) when no catalog adapter is wired.

    `fire_due_overlay_expiries` resolves `current_catalog_adapter()` unconditionally, which fails
    closed (RuntimeError) until a deployment injects a catalog adapter. That is a configuration
    state, not a fault, so we probe first and skip with a counter/gauge instead of inflating the
    error count every tick."""
    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.expiry.skipped_no_adapter")
        return 0
    return fire_due_overlay_expiries(conn, now=now)


def _stage(name: str) -> Callable:
    """Wrap a stage callable so a fault increments a counted, logged error and returns a fallback
    instead of propagating — one failing stage never stalls the tick (the poison guard already
    protects process_one; this protects the other stages)."""

    def _decorate(fn: Callable, fallback):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — bounded per-stage guard, by design
            counters.incr(f"worker.stage_error.{name}")
            counters.incr("worker.errors")
            log("worker.stage_error", level="error", stage=name, error=repr(exc))
            return fallback

    return _decorate


def _tx(conn: psycopg.Connection, fn: Callable):
    """Run `fn` inside `with conn.transaction()`. REQUIRED for any stage that uses SAVEPOINT or a
    FOR UPDATE lock that must span statements (`run_projection`, `fire_timer`, the overlay-expiry
    poller): `run_forever` uses an AUTOCOMMIT connection, where a bare SAVEPOINT raises
    (NoActiveSqlTransaction) and a FOR UPDATE lock releases at statement end. `conn.transaction()`
    opens a real BEGIN/COMMIT on autocommit and a nested SAVEPOINT under the per-test transactional
    connection, so the same code path is durable in production and rolled back in tests."""
    with conn.transaction():
        return fn()


def run_worker_once(
    conn: psycopg.Connection,
    registry,
    projections: list[Projection],
    *,
    owner: str,
    now: datetime,
    batch: int = 50,
    publish: Callable[[psycopg.Connection, OutboxMessage], None] | None = None,
) -> WorkerTick:
    """ONE bounded, non-blocking pass over every runtime stage (no sleeps, so unit-testable):

      reclaim stuck leases -> drain control signals (auto-park) -> drain up to `batch` queue items
      via process_one (stop on idle) -> publish a relay batch -> fire due timers -> fire due overlay
      expiries -> advance each projection once.

    Each stage is individually guarded (`_stage`): a fault is counted + logged and the tick continues.
    Returns a `WorkerTick` of per-stage counts for tests + metrics."""
    if publish is None:
        publish = make_queue_publisher(_DEFAULT_RELAY_ROUTE)
    errors_before = counters.snapshot()["counters"].get("worker.errors", 0)

    reclaimed = _stage("recover_stuck")(lambda: recover_stuck(conn), (0, 0))

    # `_control_actor` is passed as a factory so it is built ONLY when a parkable row exists — an
    # empty control queue (the common tick) never mints a service principal.
    parked = _stage("control_signals")(lambda: drain_control_signals(conn), 0)

    def _drain_queue() -> int:
        processed = 0
        for _ in range(batch):
            outcome = process_one(conn, registry, owner=owner)
            if outcome.status == "idle":
                break
            processed += 1
            if outcome.status in ("retryable", "permanent"):
                counters.incr("queue.fail")
        return processed

    queue_processed = _stage("process_one")(_drain_queue, 0)

    relay_published = _stage("relay")(
        lambda: relay_publish_batch(conn, publish, owner=owner), 0
    )

    def _fire_timers() -> int:
        # Lease due timers durably (one atomic UPDATE), then fire each in its OWN transaction so a
        # crash mid-batch leaves the rest leased for reclaim (§5.5) — and so fire_timer's
        # SELECT-FOR-UPDATE + enqueue + mark-fired is atomic on the autocommit daemon connection.
        tids = _tx(conn, lambda: poll_due_timers(
            conn, owner=owner, lease_seconds=30, batch=batch, now=now
        ))
        fired = 0
        for tid in tids:
            if _tx(conn, lambda t=tid: fire_timer(conn, t, now=now).fired):
                fired += 1
        return fired

    timers_fired = _stage("timers")(_fire_timers, 0)

    overlay_expiries = _stage("overlay_expiry")(
        lambda: _tx(conn, lambda: _advance_overlay_expiries(conn, now=now)), 0
    )

    def _advance_projections() -> int:
        advanced = 0
        for projection in projections:
            advanced += _stage(f"projection.{getattr(projection, 'name', '?')}")(
                lambda p=projection: _tx(conn, lambda: run_projection(conn, p)), 0
            )
        return advanced

    projections_advanced = _stage("projections")(_advance_projections, 0)

    errors = counters.snapshot()["counters"].get("worker.errors", 0) - errors_before
    counters.gauge("worker.last_tick.queue_processed", queue_processed)
    return WorkerTick(
        queue_processed=queue_processed,
        relay_published=relay_published,
        timers_fired=timers_fired,
        overlay_expiries=overlay_expiries,
        projections_advanced=projections_advanced,
        reclaimed=reclaimed,
        parked=parked,
        errors=errors,
    )


def _ensure_phase06_commands() -> None:
    """Register the Phase-06 §4.4 command catalog once. `register_phase06_commands` raises on a
    duplicate and the command registry persists across a process, so guard on a sentinel action —
    mirroring the idempotency the SP-1/SP-2 catalogs implement per-action."""
    from featuregen.aggregates.commands import register_phase06_commands
    from featuregen.commands.registry import get_command

    try:
        get_command("create_run")
    except KeyError:
        register_phase06_commands()


def compose(conn: psycopg.Connection) -> tuple[object, list[Projection]]:
    """Composition root: assemble the production HandlerRegistry + projection list the worker drives
    by REUSING the existing registration functions (no handler is reinvented):

      * Phase-06 saga: event schemas, the §4.4 command catalog, and the `activate_version` handler
        (register_phase06_event_schemas + _ensure_phase06_commands + register_phase06_handlers).
      * SP-2 intake: event-type schemas + command catalog (register_sp2) and the DB-backed authz
        rows / contract-content schemas / PRIMARY_SELECTED wiring / checkpoints (seed_sp2_authz).
      * SP-1 overlay: event-type schemas + command catalog (register_overlay) and its authz rows /
        checkpoint (seed_overlay_authz).

    Returns `(registry, [StagePrimaryProjection, OverlayProjection])` — the two runner-driven
    Projection-protocol read models. (The `feature_contract` / `run_workflow_state` read models are
    fold-authoritative, not runner-driven, so they are not in this list.)

    NOT wired here (deployment-injected, deliberately): the command-authz PolicyAuthorizer and the
    overlay catalog adapter. The worker's own effects (step handlers, the timer-command bridge, the
    auto-park stage) are trusted internal paths that do not cross execute_command's authz seam, and
    the catalog adapter is a per-deployment data-source binding."""
    from featuregen.aggregates.activation import register_phase06_handlers
    from featuregen.aggregates.bootstrap import register_phase06_event_schemas
    from featuregen.documents.primary import StagePrimaryProjection
    from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz
    from featuregen.overlay.bootstrap import register_overlay, seed_overlay_authz
    from featuregen.overlay.projection import OverlayProjection
    from featuregen.runtime.handlers import HandlerRegistry

    registry = HandlerRegistry()
    register_phase06_event_schemas()
    _ensure_phase06_commands()
    register_phase06_handlers(registry)
    register_sp2(registry)
    seed_sp2_authz(conn)
    register_overlay(registry)
    seed_overlay_authz(conn)

    projections: list[Projection] = [StagePrimaryProjection(), OverlayProjection()]
    return registry, projections


def _safe_dsn(dsn: str) -> str:
    """Strip any `password=...` token so a DSN is never logged in the clear."""
    return " ".join(p for p in dsn.split() if not p.lower().startswith("password="))


def _install_signal_handlers(shutdown_event: threading.Event) -> None:
    """Set a graceful-shutdown flag on SIGINT / SIGTERM. Best-effort: `signal.signal` only works on
    the main thread, so a non-main-thread caller (or a platform without a signal) silently keeps the
    injected event, which the caller can still set programmatically."""

    def _handler(_signum, _frame) -> None:
        log("worker.signal", signal=_signum)
        shutdown_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):  # not main thread / unsupported — injected event still works
            pass


def run_forever(
    dsn: str,
    *,
    interval: float = 1.0,
    shutdown_event: threading.Event | None = None,
    owner: str | None = None,
) -> None:
    """Open ONE autocommit connection and loop `run_worker_once` until shutdown (SIGINT/SIGTERM set a
    threading.Event). Autocommit is required: each stage owns its own `with conn.transaction()` so a
    stage's writes commit durably (not a per-test savepoint). A tick is fully guarded, then we wait
    `interval` seconds (interruptible via the event) so an empty queue does not busy-spin."""
    owner = owner or f"worker-{os.getpid()}"
    if shutdown_event is None:
        shutdown_event = threading.Event()
        _install_signal_handlers(shutdown_event)

    conn = psycopg.connect(dsn, autocommit=True)
    try:
        registry, projections = compose(conn)
        log("worker.start", dsn=_safe_dsn(dsn), owner=owner, interval=interval)
        while not shutdown_event.is_set():
            now = datetime.now(UTC)
            try:
                tick = run_worker_once(conn, registry, projections, owner=owner, now=now)
                log(
                    "worker.tick",
                    queue_processed=tick.queue_processed,
                    relay_published=tick.relay_published,
                    timers_fired=tick.timers_fired,
                    overlay_expiries=tick.overlay_expiries,
                    projections_advanced=tick.projections_advanced,
                    reclaimed=list(tick.reclaimed),
                    parked=tick.parked,
                    errors=tick.errors,
                )
            except Exception as exc:  # noqa: BLE001 — a tick must never crash the daemon loop
                counters.incr("worker.tick.fatal")
                log("worker.tick.fatal", level="error", error=repr(exc))
            shutdown_event.wait(timeout=interval)
    finally:
        conn.close()
        log("worker.stop", owner=owner, metrics=counters.snapshot())
