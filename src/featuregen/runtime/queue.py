from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from featuregen.runtime.backoff import compute_backoff
from featuregen.runtime.observability import counters, log


def _record_dead_letter(queue_id: int, *, error: str, reason: str) -> None:
    """Emit a dedicated DLQ metric + log when a message transitions to status='dead' (m5). Captures
    BOTH DLQ paths — poison-permanent (fail_permanent) and retry-exhausted (fail_retryable at the
    attempt budget) — and is deliberately SEPARATE from the transient `queue.fail` backoff counter."""
    counters.incr("queue.dlq")
    log("queue.dead_letter", level="error", queue_id=queue_id, reason=reason, error=error)

# Control-plane queue handlers owned by the dedicated control-signal poller
# (`drain_control_signals`), NOT by the general `process_one` consumer. These carry no run
# event_id, so process_one's HandlerContext path would DLQ them for a missing handler — and across
# workers process_one could claim one BEFORE the control poller, silently converting a safety park
# into an unknown-handler DLQ. This is the SINGLE SOURCE OF TRUTH that keeps the two consumers
# exactly complementary: `claim_one` EXCLUDES these (below) and `drain_control_signals` claims
# ONLY these, both under FOR UPDATE SKIP LOCKED so neither double-processes across workers.
CONTROL_SIGNAL_HANDLERS = frozenset({"runtime.auto_park", "runtime.repair_exhausted"})
FORMULA_SHADOW_QUEUE_HANDLERS = frozenset({"recipe_formula_shadow.author.v1"})
# Phase G §3.1 — the materialization compile lane. Same reason as the two sets above: a
# materialization job carries no run-stream `event_id`, so `process_one`'s `_build_context`
# (`dispatch.py:111-133`) would dead-letter it as an unresolvable triggering event. The name is
# spelled here rather than imported from `featuregen.materialize.queue_lane` because that module
# imports THIS one; `test_queue_lane.py` asserts the two spellings agree, so a rename cannot
# silently leave `claim_one` free to steal a governed request.
MATERIALIZATION_QUEUE_HANDLERS = frozenset({"materialization.compile.v1"})
# The per-candidate "Draft formula" lane. Dedicated for the same reason as the three sets above —
# the message carries no run-stream `event_id`, so `process_one` would dead-letter it — and for one
# more that is specific to this lane: `process_one` runs its handler INSIDE `with
# conn.transaction()`, and a draft is two provider calls plus validation plus admission. Riding the
# general lane would therefore hold one database transaction open across both models, which is the
# exact shape the async design exists to prevent. Here the handler owns its own connection and
# commits between every stage. Spelled here rather than imported from the route module for
# `MATERIALIZATION_QUEUE_HANDLERS`'s reason: that module imports this one, and a test asserts the
# two spellings agree so a rename cannot leave `claim_one` free to steal a draft.
FORMULA_DRAFT_QUEUE_HANDLERS = frozenset({"formula_draft.author.v1"})
# §0.10 step 3 — the V2 GENERATION lane, which drives a `generation_request` from a declared build
# set to a sealed artifact. Dedicated for the same reason as the four sets above: the message
# carries no run-stream `event_id`, so `process_one` would dead-letter it. It is a SEPARATE set from
# `MATERIALIZATION_QUEUE_HANDLERS` rather than a second handler inside it because the two lanes
# drive different lifecycles over different tables — V1's `materialization_request` and V2's
# `generation_request` — and one shared set would let either consumer claim the other's work and
# then fail to find the row it expects. Spelled here rather than imported for that set's reason:
# `materialize/generation_lane.py` imports THIS module, and a test asserts the two spellings agree.
GENERATION_QUEUE_HANDLERS = frozenset({"generation.v2.build"})
_DEDICATED_HANDLERS = (
    CONTROL_SIGNAL_HANDLERS | FORMULA_SHADOW_QUEUE_HANDLERS | MATERIALIZATION_QUEUE_HANDLERS
    | FORMULA_DRAFT_QUEUE_HANDLERS | GENERATION_QUEUE_HANDLERS)


class BackpressureError(RuntimeError):
    """Admission control signal (§5.2): a partition is at capacity. Raised by the queue
    publisher; the relay treats it as durable waiting (leave the outbox row pending, no attempt
    bump, no DLQ), never as a delivery failure."""


class QueueIdempotencyConflict(RuntimeError):
    """One queue message id was reused for materially different work."""


def _payload_hash(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class QueueClaim:
    """A leased worker-queue item (§5.2)."""

    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


@dataclass(frozen=True, slots=True)
class FormulaQueueClaim:
    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_fence: int


def enqueue(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: datetime | None = None,
    priority: int = 100,
) -> int:
    """Insert a 'ready' work item; idempotent on message_id. Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, available_at, priority) "
            "VALUES (%s, %s, %s, %s, COALESCE(%s, now()), %s) "
            "ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id, partition_key, handler, Json(payload), available_at, priority),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute("SELECT id FROM queue WHERE message_id = %s", (message_id,))
        return int(cur.fetchone()[0])


def enqueue_checked(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: datetime | None = None,
    priority: int = 100,
) -> int:
    payload_hash = _payload_hash(payload)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue "
            "(message_id, partition_key, handler, payload, payload_hash, available_at, priority) "
            "VALUES (%s, %s, %s, %s, %s, COALESCE(%s, now()), %s) "
            "ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (
                message_id,
                partition_key,
                handler,
                Json(payload),
                payload_hash,
                available_at,
                priority,
            ),
        )
        inserted = cur.fetchone()
        if inserted is not None:
            return int(inserted[0])
        cur.execute(
            "SELECT id, partition_key, handler, payload_hash, payload "
            "FROM queue WHERE message_id = %s",
            (message_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise QueueIdempotencyConflict("queue id disappeared during checked insert")
    stored_hash = row[3] or _payload_hash(row[4])
    if (
        row[1] != partition_key
        or row[2] != handler
        or stored_hash != payload_hash
    ):
        raise QueueIdempotencyConflict(
            f"queue message {message_id!r} conflicts with its stored identity")
    return int(row[0])


def claim_one(
    conn: psycopg.Connection, *, owner: str, lease_seconds: int = 30
) -> QueueClaim | None:
    """Claim one ready item via FOR UPDATE SKIP LOCKED, excluding partitions that already
    have an in-flight lease (per-aggregate serialization, §5.2). Bumps attempts atomically.

    Control-signal rows (`CONTROL_SIGNAL_HANDLERS`) are EXCLUDED so this — and thus process_one —
    can NEVER claim an `auto_park` / `repair_exhausted` row on any worker: they are owned by
    `drain_control_signals`. Without this, a multi-worker race lets a general consumer claim a
    control signal first, hit an unknown handler, and DLQ a safety park (the run is never parked).

    Concurrent-claimer race: two workers can both pass the partition-exclusion subquery before
    either commits its lease; `queue_one_inflight_per_partition` then rejects the loser with a
    UniqueViolation. We run the claim in a SAVEPOINT and translate that violation into
    "nothing claimed" (return None) so it never aborts the caller's outer transaction."""
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    "  SELECT id FROM queue"
                    "   WHERE status='ready' AND available_at <= now()"
                    "     AND handler <> ALL(%s)"
                    "     AND partition_key NOT IN (SELECT partition_key FROM queue WHERE status='leased')"
                    "   ORDER BY priority, available_at, id"
                    "   FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    "  lease_expires_at = now() + make_interval(secs => %s), attempts = q.attempts + 1 "
                    "FROM c WHERE q.id = c.id RETURNING q.*",
                    (list(_DEDICATED_HANDLERS), owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None  # lost the per-partition in-flight race; nothing claimed this round
    if row is None:
        return None
    return QueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def claim_recipe_formula_shadow(
    conn: psycopg.Connection,
    *,
    owner: str,
    lease_seconds: int = 300,
) -> FormulaQueueClaim | None:
    """Lease only the dedicated formula route with a monotonically increasing fence."""
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    " SELECT id FROM queue"
                    " WHERE status='ready' AND available_at <= now()"
                    "   AND handler = ANY(%s)"
                    "   AND partition_key NOT IN "
                    "       (SELECT partition_key FROM queue WHERE status='leased')"
                    " ORDER BY priority, available_at, id"
                    " FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    " lease_expires_at=now() + make_interval(secs => %s), "
                    " attempts=q.attempts + 1, lease_fence=q.lease_fence + 1 "
                    "FROM c WHERE q.id=c.id RETURNING q.*",
                    (list(FORMULA_SHADOW_QUEUE_HANDLERS), owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None
    if row is None:
        return None
    return FormulaQueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_fence=row["lease_fence"],
    )


def renew_recipe_formula_shadow(
    conn: psycopg.Connection,
    claim: FormulaQueueClaim,
    *,
    lease_seconds: int = 300,
) -> bool:
    row = conn.execute(
        "UPDATE queue SET lease_expires_at=now() + make_interval(secs => %s) "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (lease_seconds, claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def complete_recipe_formula_shadow(
    conn: psycopg.Connection, claim: FormulaQueueClaim
) -> bool:
    row = conn.execute(
        "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def fail_recipe_formula_shadow(
    conn: psycopg.Connection,
    claim: FormulaQueueClaim,
    *,
    error: str,
    permanent: bool,
) -> bool:
    status = "dead" if permanent or claim.attempts >= claim.max_attempts else "ready"
    row = conn.execute(
        "UPDATE queue SET status=%s, last_error=%s, lease_owner=NULL, lease_expires_at=NULL, "
        "available_at=CASE WHEN %s='ready' THEN now() + make_interval(secs => %s) "
        "ELSE available_at END "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() RETURNING id",
        (
            status,
            error,
            status,
            compute_backoff(claim.attempts),
            claim.id,
            claim.lease_owner,
            claim.lease_fence,
        ),
    ).fetchone()
    if row is not None and status == "dead":
        _record_dead_letter(
            claim.id,
            error=error,
            reason="formula_permanent" if permanent else "formula_retry_exhausted",
        )
    return row is not None


@dataclass(frozen=True, slots=True)
class MaterializationQueueClaim:
    """A leased materialization job (Phase G §3.1).

    Field-for-field the same shape as :class:`FormulaQueueClaim` and deliberately a SEPARATE type:
    the two lanes' terminal writes are fence-guarded UPDATEs keyed on ``(id, lease_owner,
    lease_fence)``, and a shared type would let one lane's claim be handed to the other lane's
    completion by nothing worse than a typo. Reshaping the existing type into a shared one is not
    available either — `runtime/queue.py` is concurrently owned, and this lane is additive.
    """

    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_fence: int


def claim_materialization(
    conn: psycopg.Connection,
    *,
    owner: str,
    lease_seconds: float,
) -> MaterializationQueueClaim | None:
    """Lease one materialization job with a monotonically increasing fence.

    ``lease_seconds`` has NO default, unlike the formula lane's 300s. A compile's duration is
    dominated by the L0 build proof, whose bound is the deployment's configured
    ``L0Interpreter.timeout_seconds`` — so the only honest lease is one derived from that
    configuration, and a default here would be a second answer that nothing keeps in step with it.
    The lane claims with a short bootstrap lease and sizes it once, the moment the budget is known.

    The partition exclusion is what stops two compiles of one logical group: the lane keys the
    partition on the group name, and publication is atomic per group.
    """
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    " SELECT id FROM queue"
                    " WHERE status='ready' AND available_at <= now()"
                    "   AND handler = ANY(%s)"
                    "   AND partition_key NOT IN "
                    "       (SELECT partition_key FROM queue WHERE status='leased')"
                    " ORDER BY priority, available_at, id"
                    " FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    " lease_expires_at=now() + make_interval(secs => %s), "
                    " attempts=q.attempts + 1, lease_fence=q.lease_fence + 1 "
                    "FROM c WHERE q.id=c.id RETURNING q.*",
                    (list(MATERIALIZATION_QUEUE_HANDLERS), owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None
    if row is None:
        return None
    return MaterializationQueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_fence=row["lease_fence"],
    )


def renew_materialization(
    conn: psycopg.Connection,
    claim: MaterializationQueueClaim,
    *,
    lease_seconds: float,
) -> bool:
    """Extend a held materialization lease, refusing a stale fence.

    The lane calls this EXACTLY once, immediately after a claim, to replace the bootstrap lease
    with one sized from the deployment's compile budget — never during a compile. See
    ``materialize/queue_lane.py`` for why a mid-compile renewal is not merely unnecessary here but
    structurally impossible: the build proof runs inside the chain's commit transaction, which
    holds a row lock on the very request a renewal would have to touch.
    """
    row = conn.execute(
        "UPDATE queue SET lease_expires_at=now() + make_interval(secs => %s) "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (lease_seconds, claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def complete_materialization(
    conn: psycopg.Connection, claim: MaterializationQueueClaim
) -> bool:
    """Mark a materialization job done, refusing a stale fence."""
    row = conn.execute(
        "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def fail_materialization(
    conn: psycopg.Connection,
    claim: MaterializationQueueClaim,
    *,
    error: str,
    permanent: bool,
) -> bool:
    """Fail a materialization job — retry with backoff, or dead-letter — refusing a stale fence."""
    status = "dead" if permanent or claim.attempts >= claim.max_attempts else "ready"
    row = conn.execute(
        "UPDATE queue SET status=%s, last_error=%s, lease_owner=NULL, lease_expires_at=NULL, "
        "available_at=CASE WHEN %s='ready' THEN now() + make_interval(secs => %s) "
        "ELSE available_at END "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() RETURNING id",
        (
            status,
            error,
            status,
            compute_backoff(claim.attempts),
            claim.id,
            claim.lease_owner,
            claim.lease_fence,
        ),
    ).fetchone()
    if row is not None and status == "dead":
        _record_dead_letter(
            claim.id,
            error=error,
            reason="materialization_permanent" if permanent else "materialization_retry_exhausted",
        )
    return row is not None


@dataclass(frozen=True, slots=True)
class GenerationQueueClaim:
    """A leased V2 generation job (§0.10 step 3).

    A SEPARATE type from the other lanes' claims, for the reason
    :class:`MaterializationQueueClaim` records: every terminal write below is a fence-guarded UPDATE
    keyed on ``(id, lease_owner, lease_fence)``, and one shared type would let this lane's claim be
    handed to another lane's completion by nothing worse than a typo.
    """

    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_fence: int


def claim_generation(
    conn: psycopg.Connection,
    *,
    owner: str,
    lease_seconds: float = 300,
) -> GenerationQueueClaim | None:
    """Lease one V2 generation job with a monotonically increasing fence.

    ``lease_seconds`` HAS a default here, unlike :func:`claim_materialization`, and the difference
    is a property of the work rather than a difference of opinion. A V1 compile's duration is
    dominated by the L0 build proof — a configured subprocess timeout — so the only honest lease
    there is one derived from that configuration. A V2 generation compiles, renders and seals: it
    runs no subprocess, calls no provider, and its cost is bounded by the size of the build set. A
    default is therefore a statement about the work, not a number nothing keeps in step.

    The partition exclusion is what stops two generations of one logical group in one environment:
    the lane keys the partition on exactly that pair, and sealing is atomic per group.
    """
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    " SELECT id FROM queue"
                    " WHERE status='ready' AND available_at <= now()"
                    "   AND handler = ANY(%s)"
                    "   AND partition_key NOT IN "
                    "       (SELECT partition_key FROM queue WHERE status='leased')"
                    " ORDER BY priority, available_at, id"
                    " FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    " lease_expires_at=now() + make_interval(secs => %s), "
                    " attempts=q.attempts + 1, lease_fence=q.lease_fence + 1 "
                    "FROM c WHERE q.id=c.id RETURNING q.*",
                    (list(GENERATION_QUEUE_HANDLERS), owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None
    if row is None:
        return None
    return GenerationQueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_fence=row["lease_fence"],
    )


def renew_generation(
    conn: psycopg.Connection,
    claim: GenerationQueueClaim,
    *,
    lease_seconds: float = 300,
) -> bool:
    """Extend a held generation lease, refusing a stale fence."""
    row = conn.execute(
        "UPDATE queue SET lease_expires_at=now() + make_interval(secs => %s) "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (lease_seconds, claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def complete_generation(conn: psycopg.Connection, claim: GenerationQueueClaim) -> bool:
    """Mark a generation job done, refusing a stale fence."""
    row = conn.execute(
        "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def fail_generation(
    conn: psycopg.Connection,
    claim: GenerationQueueClaim,
    *,
    error: str,
    permanent: bool,
) -> bool:
    """Fail a generation job — retry with backoff, or dead-letter — refusing a stale fence."""
    status = "dead" if permanent or claim.attempts >= claim.max_attempts else "ready"
    row = conn.execute(
        "UPDATE queue SET status=%s, last_error=%s, lease_owner=NULL, lease_expires_at=NULL, "
        "available_at=CASE WHEN %s='ready' THEN now() + make_interval(secs => %s) "
        "ELSE available_at END "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() RETURNING id",
        (status, error, status, compute_backoff(claim.attempts),
         claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    if row is not None and status == "dead":
        _record_dead_letter(
            claim.id, error=error,
            reason="generation_permanent" if permanent else "generation_retry_exhausted")
    return row is not None


def complete(conn: psycopg.Connection, queue_id: int) -> None:
    """Mark a claimed item done and release its lease."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL WHERE id=%s",
            (queue_id,),
        )


def fail_retryable(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Transient failure: reschedule with backoff, or DLQ once attempts hit the budget (§5.6)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT attempts, max_attempts FROM queue WHERE id=%s", (queue_id,))
        row = cur.fetchone()
        if row["attempts"] >= row["max_attempts"]:
            cur.execute(
                "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL WHERE id=%s",
                (error, queue_id),
            )
            _record_dead_letter(queue_id, error=error, reason="retry_exhausted")
        else:
            delay = compute_backoff(row["attempts"])  # default jitter=0.5 (review MINOR #23)
            cur.execute(
                "UPDATE queue SET status='ready', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL, available_at = now() + make_interval(secs => %s) "
                "WHERE id=%s",
                (error, delay, queue_id),
            )


def fail_permanent(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Deterministic failure: skip delivery retry, route to DLQ (§5.6)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
            "lease_expires_at=NULL WHERE id=%s",
            (error, queue_id),
        )
    _record_dead_letter(queue_id, error=error, reason="poison_permanent")


def reclaim_stuck_queue(conn: psycopg.Connection) -> int:
    """Return expired-lease items to 'ready' so a crashed worker's items resume (§5.7)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='leased' AND lease_expires_at < now()"
        )
        return cur.rowcount


def queue_depth(conn: psycopg.Connection, *, partition_key: str | None = None) -> int:
    """In-flight backlog (ready+leased), globally or for one partition. The relay's admission
    control consults this for §5.2 backpressure (bound per-partition work-in-progress)."""
    with conn.cursor() as cur:
        if partition_key is None:
            cur.execute("SELECT count(*) FROM queue WHERE status IN ('ready', 'leased')")
        else:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased') "
                "AND partition_key = %s",
                (partition_key,),
            )
        return int(cur.fetchone()[0])


@dataclass(frozen=True, slots=True)
class FormulaDraftQueueClaim:
    """A leased "Draft formula" job.

    A SEPARATE type from :class:`FormulaQueueClaim` and :class:`MaterializationQueueClaim` for the
    reason the latter records: every terminal write below is a fence-guarded UPDATE keyed on
    ``(id, lease_owner, lease_fence)``, and one shared type would let this lane's claim be handed to
    another lane's completion by nothing worse than a typo.
    """

    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int
    lease_owner: str
    lease_fence: int


def claim_formula_draft(
    conn: psycopg.Connection,
    *,
    owner: str,
    lease_seconds: float = 600,
) -> FormulaDraftQueueClaim | None:
    """Lease one draft job with a monotonically increasing fence.

    The default lease is longer than the shadow lane's because the work is two provider calls in
    series rather than one, and a lease that expires mid-draft would let a second worker start the
    same authoring — which is a second bill, not merely a second attempt.
    """
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    " SELECT id FROM queue"
                    " WHERE status='ready' AND available_at <= now()"
                    "   AND handler = ANY(%s)"
                    "   AND partition_key NOT IN "
                    "       (SELECT partition_key FROM queue WHERE status='leased')"
                    " ORDER BY priority, available_at, id"
                    " FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    " lease_expires_at=now() + make_interval(secs => %s), "
                    " attempts=q.attempts + 1, lease_fence=q.lease_fence + 1 "
                    "FROM c WHERE q.id=c.id RETURNING q.*",
                    (list(FORMULA_DRAFT_QUEUE_HANDLERS), owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None
    if row is None:
        return None
    return FormulaDraftQueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        lease_owner=row["lease_owner"],
        lease_fence=row["lease_fence"],
    )


def renew_formula_draft(
    conn: psycopg.Connection,
    claim: FormulaDraftQueueClaim,
    *,
    lease_seconds: float = 600,
) -> bool:
    """Extend a held draft lease, refusing a stale fence.

    Called BETWEEN stages, never during one: each stage commits before the next begins, so there is
    always a moment with no open transaction in which to renew. That is what makes a long draft
    safe without a long lease.
    """
    row = conn.execute(
        "UPDATE queue SET lease_expires_at=now() + make_interval(secs => %s) "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (lease_seconds, claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def complete_formula_draft(
    conn: psycopg.Connection, claim: FormulaDraftQueueClaim
) -> bool:
    """Mark a draft job done, refusing a stale fence."""
    row = conn.execute(
        "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() "
        "RETURNING id",
        (claim.id, claim.lease_owner, claim.lease_fence),
    ).fetchone()
    return row is not None


def fail_formula_draft(
    conn: psycopg.Connection,
    claim: FormulaDraftQueueClaim,
    *,
    error: str,
    permanent: bool,
) -> bool:
    """Fail a draft job, retryably or for good, refusing a stale fence.

    RETRY IS NOT FREE HERE, which is why the worker sets ``permanent`` for anything it can name. A
    retryable failure re-runs the authoring stage and buys the answer again; a message that
    exhausted its attempts against a deterministic fault would have bought it `max_attempts` times.
    """
    status = "dead" if permanent or claim.attempts >= claim.max_attempts else "ready"
    row = conn.execute(
        "UPDATE queue SET status=%s, last_error=%s, lease_owner=NULL, lease_expires_at=NULL, "
        "available_at=CASE WHEN %s='ready' THEN now() + make_interval(secs => %s) "
        "ELSE available_at END "
        "WHERE id=%s AND status='leased' AND lease_owner=%s AND lease_fence=%s "
        "AND lease_expires_at > now() RETURNING id",
        (
            status,
            error,
            status,
            compute_backoff(claim.attempts),
            claim.id,
            claim.lease_owner,
            claim.lease_fence,
        ),
    ).fetchone()
    if row is not None and status == "dead":
        _record_dead_letter(
            claim.id,
            error=error,
            reason="formula_draft_permanent" if permanent else "formula_draft_retry_exhausted",
        )
    return row is not None
