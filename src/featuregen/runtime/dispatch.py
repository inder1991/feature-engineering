from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import psycopg

from featuregen.contracts import (
    ConcurrencyError,
    Disposition,
    Handler,
    HandlerContext,
    HandlerResult,
    NewDocument,
)
from featuregen.events.store import load_stream
from featuregen.runtime.handlers import HandlerRegistry
from featuregen.runtime.ledger import is_processed
from featuregen.runtime.outbox import reclaim_stuck_outbox
from featuregen.runtime.queue import (
    QueueClaim,
    claim_one,
    complete,
    fail_permanent,
    fail_retryable,
    reclaim_stuck_queue,
)
from featuregen.runtime.step import commit_step


class HandlerTimeout(Exception):
    """Raised when a handler exceeds its per-invocation timeout (=> delivery retry, §5.6)."""


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: str  # "idle" | "ok" | "duplicate" | "retryable" | "permanent"
    message_id: str | None
    queue_id: int | None


def _default_document_loader(conn: psycopg.Connection, run_id: str) -> Mapping[str, NewDocument]:
    return {}


def _open_handler_conn(conn: psycopg.Connection) -> psycopg.Connection:
    """A dedicated, autocommit connection handed to the handler as ctx.read_conn (READ-ONLY: a
    handler loads streams/documents for its decisions but MUST NOT write through it — every
    mutation is declared in the returned HandlerResult and applied by commit_step inside the
    step tx). ISOLATED from the dispatcher's transactional `conn`. Isolation is mandatory:
    CPython cannot kill a running thread, so a timed-out handler must never share the
    dispatcher's connection (the dispatcher keeps using it to reschedule/fail the message —
    concurrent use of one psycopg connection from two threads is unsafe). Override via
    process_one(..., handler_conn_factory=...) if the deployment DSN needs extra credentials."""
    # READ-ONLY at the session level: any write a handler attempts through ctx.read_conn fails
    # fast (psycopg ReadOnlySqlTransaction) instead of silently committing outside the §5.1 step
    # boundary. Kept autocommit so the handler's reads do not hold an open transaction on this
    # isolated connection.
    handler_conn = psycopg.connect(conn.info.dsn, options="-c default_transaction_read_only=on")
    handler_conn.autocommit = True
    return handler_conn


def _run_with_timeout(handler: Handler, ctx: HandlerContext) -> HandlerResult:
    """Run handler.handle(ctx) under a hard wall-clock timeout WITHOUT blocking on a wedged
    handler. The handler runs on a daemon thread joined for handler.timeout_seconds; we never
    use a ThreadPoolExecutor (whose context-manager __exit__ runs shutdown(wait=True), which
    would BLOCK until the handler returns and defeat the timeout). On breach we raise
    HandlerTimeout and ABANDON the thread; because ctx.read_conn is a dedicated connection (see
    _open_handler_conn), the abandoned thread cannot corrupt the dispatcher's step transaction.
    A permanently wedged handler leaks its dedicated connection until the worker is restarted,
    and its message is redelivered (then DLQ'd after max_attempts) — the honest limit of
    cooperative timeouts in CPython (a thread cannot be force-killed)."""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["result"] = handler.handle(ctx)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the dispatcher thread
            box["error"] = exc

    thread = threading.Thread(target=_target, name=f"handler:{handler.name}", daemon=True)
    thread.start()
    thread.join(timeout=handler.timeout_seconds)
    if thread.is_alive():
        raise HandlerTimeout(f"handler {handler.name!r} exceeded {handler.timeout_seconds}s")
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]


def _build_context(
    conn: psycopg.Connection,
    claim: QueueClaim,
    document_loader: Callable[[psycopg.Connection, str], Mapping[str, NewDocument]],
    *,
    handler_conn: psycopg.Connection,
) -> HandlerContext:
    payload = claim.payload
    run_id = payload.get("run_id") or payload.get("aggregate_id")
    event_id = payload["event_id"]
    # The dispatcher's `conn` (not handler_conn) resolves the triggering event so it sees the
    # step's in-flight writes; the handler only ever gets the isolated, read-only handler_conn
    # (as ctx.read_conn).
    stream = load_stream(conn, "run", run_id)
    triggering = next((e for e in stream if e.event_id == event_id), None)
    if triggering is None:
        raise KeyError(f"triggering event {event_id!r} not found in run {run_id!r}")
    return HandlerContext(
        run_id=run_id,
        triggering_event=triggering,
        documents=document_loader(conn, run_id),
        read_conn=handler_conn,  # dedicated, READ-ONLY, isolated from the dispatcher tx
    )


def process_one(
    conn: psycopg.Connection,
    registry: HandlerRegistry,
    *,
    owner: str,
    document_loader: Callable[
        [psycopg.Connection, str], Mapping[str, NewDocument]
    ] = _default_document_loader,
    handler_conn_factory: Callable[[psycopg.Connection], psycopg.Connection] = _open_handler_conn,
) -> ProcessOutcome:
    """Claim one queue item and drive it forward idempotently (§5.3). Runs in one outer tx;
    the OK path commits the step inside a savepoint so a real OCC conflict rolls back ONLY the
    step writes (no partial events/docs/outbox/ledger) and reschedules the message.

    OCC basis: `expected_version` is the TRIGGERING event's stream_version (the version the
    step was scheduled against), NOT a freshly-read head — so a concurrent advance of the run
    stream after the step was triggered is correctly detected as a conflict (§5.1 OCC)."""
    with conn.transaction():
        claim = claim_one(conn, owner=owner)
        if claim is None:
            return ProcessOutcome(status="idle", message_id=None, queue_id=None)

        if is_processed(conn, claim.message_id):
            complete(conn, claim.id)
            return ProcessOutcome(
                status="duplicate", message_id=claim.message_id, queue_id=claim.id
            )

        # Resolve the handler under the poison guard: an unknown handler name is a
        # DETERMINISTIC failure (never retryable) — route straight to DLQ (review BLOCKER #2).
        try:
            handler = registry.get(claim.handler)
        except KeyError as exc:
            fail_permanent(conn, claim.id, error=str(exc))
            return ProcessOutcome(
                status="permanent", message_id=claim.message_id, queue_id=claim.id
            )

        handler_conn = handler_conn_factory(conn)
        timed_out = False
        try:
            ctx = _build_context(conn, claim, document_loader, handler_conn=handler_conn)

            try:
                result = _run_with_timeout(handler, ctx)
            except HandlerTimeout as exc:
                timed_out = True
                fail_retryable(conn, claim.id, error=str(exc))
                return ProcessOutcome(
                    status="retryable", message_id=claim.message_id, queue_id=claim.id
                )

            if result.disposition == Disposition.OK:
                try:
                    with conn.transaction():
                        commit_step(
                            conn,
                            ctx,
                            result,
                            message_id=claim.message_id,
                            expected_version=ctx.triggering_event.stream_version,
                            table_version=ctx.triggering_event.table_version,
                        )
                except ConcurrencyError as exc:
                    fail_retryable(conn, claim.id, error=f"OCC: {exc}")
                    return ProcessOutcome(
                        status="retryable", message_id=claim.message_id, queue_id=claim.id
                    )
                complete(conn, claim.id)
                return ProcessOutcome(status="ok", message_id=claim.message_id, queue_id=claim.id)

            if result.disposition == Disposition.RETRYABLE:
                fail_retryable(conn, claim.id, error=result.error or "retryable")
                return ProcessOutcome(
                    status="retryable", message_id=claim.message_id, queue_id=claim.id
                )

            fail_permanent(conn, claim.id, error=result.error or "permanent")
            return ProcessOutcome(
                status="permanent", message_id=claim.message_id, queue_id=claim.id
            )
        except Exception as exc:  # noqa: BLE001 — the poison backstop
            # A handler that raised (or _build_context that could not resolve the triggering
            # event) is treated as a TRANSIENT delivery failure: bump attempts + backoff, DLQ
            # at the budget. This is what prevents the infinite poison-retry (review BLOCKER #2).
            # ConcurrencyError is handled above; HandlerTimeout returned above; anything here is
            # an unexpected fault, retried a bounded number of times then DLQ'd.
            fail_retryable(conn, claim.id, error=f"handler fault: {exc!r}")
            return ProcessOutcome(
                status="retryable", message_id=claim.message_id, queue_id=claim.id
            )
        finally:
            # Close the dedicated handler connection only if the handler actually returned. On
            # timeout the abandoned thread may still hold it, so we deliberately leak it rather
            # than close a connection another thread might be mid-query on.
            if not timed_out:
                handler_conn.close()


def recover_stuck(conn: psycopg.Connection) -> tuple[int, int]:
    """Reclaim expired queue + outbox leases after a crash (§5.7). Returns (queue, outbox)."""
    return (reclaim_stuck_queue(conn), reclaim_stuck_outbox(conn))
