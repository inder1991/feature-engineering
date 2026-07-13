from __future__ import annotations

import logging
from datetime import datetime

from psycopg.rows import dict_row

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import NewTimer
from featuregen.contracts.errors import ConcurrencyError
from featuregen.identity.build import build_service_identity
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.facts import OVERLAY_FACT_EXPIRED
from featuregen.overlay.identity import _ref_from_payload
from featuregen.overlay.reverify_tasks import open_reverify_task
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.runtime.timers import schedule_timer

logger = logging.getLogger(__name__)


def demote_projected_join_edges(conn: DbConn, fact_key: str, status: str) -> None:
    """FAIL-SOFT async demotion of any graph_edge projected from this `approved_join` (Phase 3A
    Task 8): the fact just left VERIFIED, so a linked operational edge must stop
    feature-construction traversal NOW — not at the next upload's projector run (the
    ingest-latency window). Shared by `reject_fact` and `_apply_expiry`. Savepointed
    (`conn.transaction()`) so a DB fault can neither poison the surrounding command/poller
    transaction nor undo the just-appended lifecycle event; the governed edge filter
    (`approved_join_status='VERIFIED'`) remains the independent second gate if this hook is lost."""
    try:
        with conn.transaction():
            from featuregen.overlay.upload.passc.projection import demote_join_edges
            demote_join_edges(conn, fact_key=fact_key, status=status)
    except Exception:  # noqa: BLE001 — advisory: edge demotion never blocks the command/poller
        logger.warning("approved_join edge demotion failed for %s", fact_key, exc_info=True)


def schedule_expiry(
    conn: DbConn, fact_key: str, confirmed_event_id: str, expires_at: datetime
) -> str:
    """Arm the SP-0 `overlay_expiry` timer on a confirmed fact's stream. The timer
    carries the `confirmed_event_id` in its payload so the `fire_due_overlay_expiries`
    poller can CAS on it. Idempotency-keyed on `(fact_key, confirmed_event_id)` so re-confirming
    the same event is a no-op. The freshness area owns the overlay pollers now:
    `fire_due_overlay_expiries` lives in this file, `detect_catalog_changes` in
    `catalog_changes.py`, and `open_reverify_task` in `reverify_tasks.py`."""
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


def _expiry_target_current(state, confirmed_event_id: str) -> bool:
    """CAS predicate used by the expiry poller: the targeted confirmation is still the live
    one iff the fact is VERIFIED and its confirmed_event_id equals the target. A newer
    FACT_CONFIRMED advances confirmed_event_id, so a stale timer reads False here and the
    timer becomes a no-op (it is still consumed/marked fired)."""
    return (
        state is not None
        and state.status == "VERIFIED"
        and state.confirmed_event_id == confirmed_event_id
    )


def _apply_expiry(conn: DbConn, adapter, *, fact_key: str, confirmed_event_id: str, actor) -> bool:
    """Apply one due overlay_expiry timer's effect transactionally (§8). No-op (CAS) if a
    newer FACT_CONFIRMED has superseded the targeted confirmation. Otherwise append
    OVERLAY_FACT_EXPIRED (VERIFIED → REVERIFY) and open the re-verify task(s) for the resolved
    authority (one task PER side for an approved_join), carrying the target
    confirmed_event_id (prior_value flows through the proposal projection → get_task_proposal).
    Returns True iff OVERLAY_FACT_EXPIRED was appended."""
    stream = load_fact(conn, fact_key)
    if not stream:
        return False
    state = fold_overlay_state(stream)
    if not _expiry_target_current(state, confirmed_event_id):
        return False
    try:
        append_overlay_event(
            conn,
            fact_key=fact_key,
            type=OVERLAY_FACT_EXPIRED,
            payload={"expires_confirmed_event_id": confirmed_event_id},
            actor=actor,
            expected_version=stream[-1].stream_version,
        )
    except ConcurrencyError:
        # a concurrent confirm advanced the stream between fold and append → stale timer
        return False
    if state.fact_type == "approved_join":
        # Async demotion hook (Phase 3A Task 8): EXPIRED folds to REVERIFY — the projected edge
        # (if any) stops traversing immediately, without waiting for a re-ingest.
        demote_projected_join_edges(conn, fact_key, "REVERIFY")
    ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    authority = resolve_authority(conn, adapter, ref, state.fact_type)
    open_reverify_task(
        conn,
        fact_key=fact_key,
        fact_type=state.fact_type,
        target_confirmed_event_id=confirmed_event_id,
        authority=authority,
        actor=actor,
    )
    return True


def fire_due_overlay_renewals(conn: DbConn, *, now: datetime) -> int:
    """Pre-expiry RENEWAL poller (SP-1.5 Task 6): for each single-owner-renewable VERIFIED fact inside
    its renewal grace window with NO OPEN human task, open a re-verify task so an owner can RE-CONFIRM
    *before* expiry — closing the recurring outage window. approved_join is EXCLUDED: a dual-owner
    join cannot renew in place (confirm_fact denies it) so a pre-expiry task would be a dead end — it
    re-verifies via the expiry/reverify flow instead (review #12).

    Every candidate is RE-VALIDATED against the AUTHORITATIVE event stream before a task opens: the
    overlay_fact_state read model LAGS, so a task keyed only on the projection would (a) open a POISON
    task for a just-renewed, now-superseded version (review #9), or (b) be wrongly suppressed after a
    prior task was cancelled without renewing (review #3). Checking the stream — still VERIFIED, still
    within grace — avoids both. Skips (0) when no OverlayConfig is sealed."""
    from featuregen.overlay._lifecycle import within_renewal_grace
    from featuregen.overlay.config import current_overlay_config

    try:
        grace = current_overlay_config().renewal_grace
    except RuntimeError:
        return 0
    actor = build_service_identity(
        subject="service:overlay-renewal",
        role_claims=("overlay",),
        attestation="overlay-renewal-poller",
    )
    adapter = current_catalog_adapter()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT fs.fact_key
            FROM overlay_fact_state fs
            WHERE fs.status = 'VERIFIED' AND fs.expires_at IS NOT NULL
              AND fs.expires_at <= %s AND fs.expires_at > %s
              AND fs.fact_type <> 'approved_join'
              AND NOT EXISTS (
                  SELECT 1 FROM human_tasks ht
                  WHERE ht.fact_key = fs.fact_key AND ht.status = 'open'
              )
            FOR UPDATE OF fs SKIP LOCKED
            """,
            (now + grace, now),
        )
        due = cur.fetchall()
    opened = 0
    for row in due:
        stream = load_fact(conn, row["fact_key"])
        if not stream:
            continue
        state = fold_overlay_state(stream)
        # Authoritative re-check against the STREAM (the read model lags): only prompt a fact that is
        # STILL VERIFIED and within grace at the stream head — a just-renewed fact is already past
        # this version, so its stream fails within_renewal_grace and no poison task is opened.
        if state.status != "VERIFIED" or not within_renewal_grace(state, now):
            continue
        ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
        authority = resolve_authority(conn, adapter, ref, state.fact_type)
        open_reverify_task(
            conn,
            fact_key=row["fact_key"],
            fact_type=state.fact_type,
            target_confirmed_event_id=state.confirmed_event_id,
            authority=authority,
            actor=actor,
        )
        opened += 1
    return opened


def fire_due_overlay_expiries(conn: DbConn, *, now: datetime) -> int:
    """Explicit transactional poller — NOT a HandlerRegistry handler.
    The SP-0 timer runtime can't carry fact_key/confirmed_event_id to an overlay handler
    nor open a gate task, so freshness owns its own driver. SELECT due overlay_expiry timers
    FOR UPDATE SKIP LOCKED (row locks are held by the transaction until commit, so multiple
    pollers never double-process), and for each: read fact_key from the timer's aggregate_id
    and confirmed_event_id from its payload, CAS-apply the expiry (append OVERLAY_FACT_EXPIRED
    + open the re-verify task; no-op if a newer FACT_CONFIRMED superseded the target), and
    mark the timer `fired`. The poller acts as a system service principal and resolves the
    catalog adapter via the single-source accessor. Returns the number of OVERLAY_FACT_EXPIRED
    events emitted; a superseded timer is consumed (marked fired) without emitting."""
    # SP-0.5 BLOCKER #1: build_service_identity is now fail-closed, so this in-process poller's
    # principal is authenticated=False until the service-identity mechanism (mTLS / signed deploy
    # token) is wired at deploy time and verifies it. That is fail-SAFE (an unattested machine
    # principal, not a forged one) and nothing on the expiry path authorizes against it today; the
    # service-verifier wiring is the deferred follow-up (see task-10 report).
    actor = build_service_identity(
        subject="service:overlay-freshness",
        role_claims=("overlay",),
        attestation="overlay-expiry-poller",
    )
    adapter = current_catalog_adapter()
    fired = 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT timer_id, aggregate_id, payload
            FROM timers
            WHERE kind = 'overlay_expiry' AND status = 'scheduled' AND fire_at <= %s
            FOR UPDATE SKIP LOCKED
            """,
            (now,),
        )
        due = cur.fetchall()
    for row in due:
        if _apply_expiry(
            conn,
            adapter,
            fact_key=row["aggregate_id"],
            confirmed_event_id=row["payload"]["confirmed_event_id"],
            actor=actor,
        ):
            fired += 1
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE timers SET status = 'fired' WHERE timer_id = %s",
                (row["timer_id"],),
            )
    return fired
