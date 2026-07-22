"""Delivery C4 Task 2 — the feature-contract validation STATE PROJECTION (event -> state fold).

C4-T1 (migration 1009) made contract validation an APPEND-ONLY EVENT lifecycle:
``feature_contract_validation_event`` (write-once, own per-table ``seq`` IDENTITY ordering) is the
authority; ``feature_contract_validation_state`` (1 row / contract, rebuildable) is the derived read
model. This module folds the events into that state row.

Unlike ``OverlayProjection`` (which the shared ``projections/runner.run_projection`` drives over the
GLOBAL ``events``/``global_seq`` stream), this is a CUSTOM projection over a DEDICATED table ordered
by that table's own ``seq``. It therefore has its own ``catch_up``/``reset``/``rebuild`` loop, but
it REUSES the framework's supporting tables + helpers where sensible:

* ``projection_checkpoints`` — one ``projection_name='feature_contract_validation'`` row whose
  ``checkpoint_seq`` is the last validation-event ``seq`` folded (reusing runner's
  ``_ensure_checkpoint`` / ``_checkpoint_seq``); ``head_seq`` = max validation-event ``seq``.
* ``projection_degraded`` — a poison event marks the affected contract here (adapted insert: our
  poison event lives in the dedicated table, NOT in ``events``, so ``poison_event_id`` — an FK to
  ``events(event_id)`` — must be NULL; the seq lands in ``poison_seq``).
* ``projection_skips`` — the durable, idempotent (``ON CONFLICT DO NOTHING``) fail-open skip ledger.

Poison handling (``catch_up``) is FAIL-OPEN-BUT-AUDITED, per the C4-T2 brief: a poison event does
not halt the whole projection — its contract is marked degraded AND recorded in the skip ledger AND
skipped past, so one malformed event never corrupts other contracts' state and never disappears
silently.

The effective-state fold is a PURE function of (a contract's event prefix, its blocking
requirements). Recomputing from the prefix on each apply makes reset==rebuild==live-catch-up
produce byte-identical state (the load-bearing projection invariant), and makes the sequence guard
trivial: a replayed or out-of-order-lower ``seq`` (<= the state's ``applied_seq``) is a no-op.

This task does NOT emit events (C4-T3 emits ASSESSED at confirm; Delivery I emits the signed
EXTERNAL_PASSED/FAILED). Callers here fold whatever the authority log already holds.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from psycopg.rows import dict_row

from featuregen.contracts import DbConn
from featuregen.contracts.errors import ProjectionApplyError
from featuregen.projections.runner import _checkpoint_seq, _ensure_checkpoint
from featuregen.runtime.observability import counters

PROJECTION_NAME = "feature_contract_validation"
AGGREGATE = "feature_contract"

# validation_status vocabulary (mirrors the 1009 CHECK on feature_contract_validation_state).
_VALIDATION_STATUSES = ("design_checked", "needs_external_validation", "rejected")
# effective_verification vocabulary (USEFULNESS-CHECKED is a later stage; C4 never emits it).
_UNVERIFIED = "UNVERIFIED"
_DESIGN_CHECKED = "DESIGN-CHECKED"
_DATA_CHECKED = "DATA-CHECKED"

_EVENT_TYPES = ("ASSESSED", "EXTERNAL_PASSED", "EXTERNAL_FAILED", "INVALIDATED", "SUPERSEDED")


# --------------------------------------------------------------------------------------------------
# The pure fold: (event prefix in seq order, blocking requirement ids) -> (status, verification).
# --------------------------------------------------------------------------------------------------
def _fold_effective_state(
    events: Sequence[Mapping], blocking_req_ids: frozenset[str], *, contract_id: str
) -> tuple[str, str, bool]:
    """Fold a contract's validation events (ascending ``seq``) into ``(validation_status,
    effective_verification, superseded)``. The LATEST-seq event governs (a later INVALIDATED demotes
    a prior DATA-CHECKED); ``blocking_req_ids`` is the set of the contract's blocking requirements
    (from ``feature_validation_requirement``), the requirements the DATA-CHECKED promotion depends
    on.

    MF-4: SUPERSEDED is TERMINAL. Once a newer contract version has retired this one, the fold
    freezes the row as history — ``superseded`` is set True, the effective stamp is demoted to
    UNVERIFIED, and NO later (late/redelivered) EXTERNAL_PASSED/ASSESSED/INVALIDATED can re-promote
    it. Because the fold is a PURE function of the ordered event prefix, this stickiness needs no
    persisted flag to be correct on replay — the returned ``superseded`` is carried into the state
    row only so the read surface can report WHY a retired version reads UNVERIFIED.

    Raises ``ProjectionApplyError`` on a poison event (non-object payload, or an ASSESSED that
    declares a validation_status outside the vocabulary) so ``catch_up`` marks it degraded + skips.
    """
    status = "needs_external_validation"
    verification = _UNVERIFIED
    passed: set[str] = set()  # requirement_ids with a CURRENT external pass (reset each assessment)
    superseded = False        # MF-4: TERMINAL once a SUPERSEDED event is seen

    for event in events:
        etype = event["event_type"]
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise ProjectionApplyError(
                AGGREGATE, contract_id,
                f"malformed payload (not a JSON object) on seq={event['seq']}")

        if superseded and etype != "SUPERSEDED":
            # MF-4: retired version — every later event is a no-op for the state (the malformed-
            # payload poison check above still runs, so corruption is never masked). This is the
            # stickiness that stops a late EXTERNAL_PASSED resurrecting a live DATA-CHECKED.
            continue

        if etype == "ASSESSED":
            passed = set()  # a fresh deterministic assessment opens a new external-check epoch
            declared = payload.get("validation_status")
            if declared is not None and declared not in _VALIDATION_STATUSES:
                raise ProjectionApplyError(
                    AGGREGATE, contract_id, f"unknown validation_status {declared!r}")
            if declared == "rejected" or payload.get("hard_reject") is True:
                # A blocking deterministic-negative — the assessment itself hard-rejects.
                status, verification = "rejected", _UNVERIFIED
            elif blocking_req_ids:
                # A blocking requirement exists and is UNRESOLVED at assessment time.
                status, verification = "needs_external_validation", _UNVERIFIED
            else:
                # All deterministic checks passed AND no blocking requirement.
                status, verification = "design_checked", _DESIGN_CHECKED

        elif etype == "EXTERNAL_PASSED":
            req = payload.get("requirement_id")
            if req is not None:
                passed.add(req)
            # DATA-CHECKED requires EVERY blocking requirement to have a current pass, and the
            # assessment must not be rejected.
            if status != "rejected" and blocking_req_ids and passed >= set(blocking_req_ids):
                status, verification = "design_checked", _DATA_CHECKED

        elif etype == "EXTERNAL_FAILED":
            req = payload.get("requirement_id")
            if req is not None:
                passed.discard(req)
            status, verification = "rejected", _UNVERIFIED  # never DATA-CHECKED on a failed blocker

        elif etype == "INVALIDATED":
            # Catalog/fingerprint drift retired the assessment: demote the effective stamp and put
            # the contract back into a needs-(re)validation limbo; a prior DATA-CHECKED is gone.
            passed = set()
            status, verification = "needs_external_validation", _UNVERIFIED

        elif etype == "SUPERSEDED":
            # A newer contract version replaced this one. Retain the row as history
            # (validation_status kept) but demote the effective stamp AND set the terminal
            # ``superseded`` marker — from here the fold ignores any later event (gate above), so a
            # redelivered EXTERNAL_PASSED can never resurrect this retired version's live stamp.
            superseded = True
            verification = _UNVERIFIED

        else:
            # Defensive: the 1009 CHECK forbids inserting an unknown event_type, so this is only
            # reachable via a raw/corrupt row — treat as poison.
            raise ProjectionApplyError(AGGREGATE, contract_id, f"unknown event_type {etype!r}")

    return status, verification, superseded


# --------------------------------------------------------------------------------------------------
# Reads over the dedicated stream + supporting tables.
# --------------------------------------------------------------------------------------------------
def _blocking_requirement_ids(conn: DbConn, contract_id: str) -> frozenset[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT requirement_id FROM feature_validation_requirement "
            "WHERE contract_id = %s AND blocking = true",
            (contract_id,))
        return frozenset(r[0] for r in cur.fetchall())


def _event_prefix(conn: DbConn, contract_id: str, up_to_seq: int) -> list[dict]:
    """This contract's events with ``seq <= up_to_seq`` in ascending ``seq`` order. Deterministic:
    the log is append-only, so the same prefix is read during live catch-up and a full rebuild."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_id, contract_id, seq, event_type, payload "
            "FROM feature_contract_validation_event "
            "WHERE contract_id = %s AND seq <= %s ORDER BY seq ASC",
            (contract_id, up_to_seq))
        return cur.fetchall()


def _applied_seq(conn: DbConn, contract_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT applied_seq FROM feature_contract_validation_state WHERE contract_id = %s",
            (contract_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def _head_seq(conn: DbConn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT max(seq) FROM feature_contract_validation_event")
        return cur.fetchone()[0] or 0


# --------------------------------------------------------------------------------------------------
# The projection API: lock_checkpoint / apply_event / catch_up / reset / rebuild.
# --------------------------------------------------------------------------------------------------
def lock_checkpoint(conn: DbConn) -> None:
    """MF-1: serialize the WHOLE emit+fold of validation events across concurrent confirms.

    A validation event's ``seq`` is a per-table IDENTITY assigned AT INSERT and visible to other
    sessions only at COMMIT. Under READ COMMITTED two concurrent ``confirm_contract`` calls would
    each read the SAME ``projection_checkpoints`` checkpoint with a plain SELECT, fold only their
    OWN uncommitted event, and advance the checkpoint — so one confirm's committed event is
    PERMANENTLY skipped (never folded; ``is_read_ready`` then serves a stale stamp as ready) or the
    checkpoint regresses below head.

    The fix is to take this projection's ``projection_checkpoints`` row FOR UPDATE *BEFORE* any
    event is INSERTed in the confirm transaction. The row-lock is held to end-of-transaction, so a
    second concurrent confirm BLOCKS here until the first commits, then reads the advanced checkpoint
    and folds its own (necessarily higher-seq) event over the committed prefix — seqs are ASSIGNED
    and FOLDED in the same lock order. A FOR UPDATE inside ``catch_up`` alone does NOT fix this: by
    the time ``catch_up`` runs the seq is already assigned at INSERT. The checkpoint row is seeded
    (``INSERT ... ON CONFLICT DO NOTHING`` via ``_ensure_checkpoint``) so the lock finds a row.
    """
    _ensure_checkpoint(conn, PROJECTION_NAME, is_analytics=True)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints "
            "WHERE projection_name = %s FOR UPDATE",
            (PROJECTION_NAME,))
        cur.fetchone()


def apply_event(conn: DbConn, event_row: Mapping) -> bool:
    """Fold ONE validation event into its contract's ``feature_contract_validation_state`` row
    (UPSERT), advancing ``applied_seq`` to this event's ``seq``.

    SEQUENCE GUARD: an event whose ``seq <= state.applied_seq`` is a no-op (idempotent replay + safe
    against an out-of-order lower seq). Otherwise the effective state is RECOMPUTED from the
    contract's full event prefix (``seq <= this seq``) so the result is order-independent and
    rebuild-identical.

    Returns True if the state row was written, False if guarded out. Raises ``ProjectionApplyError``
    on a poison event (the fold rejects a malformed payload) — ``catch_up`` catches it; a direct
    caller sees it.
    """
    contract_id = event_row["contract_id"]
    seq = int(event_row["seq"])
    if seq <= _applied_seq(conn, contract_id):
        return False  # already folded (or superseded by a newer seq) — never regress

    events = _event_prefix(conn, contract_id, seq)
    blocking = _blocking_requirement_ids(conn, contract_id)
    status, verification, superseded = _fold_effective_state(
        events, blocking, contract_id=contract_id)

    conn.execute(
        """
        INSERT INTO feature_contract_validation_state
            (contract_id, validation_status, effective_verification, superseded, applied_seq,
             updated_at)
        VALUES (%s, %s, %s, %s, %s, now())
        ON CONFLICT (contract_id) DO UPDATE SET
            validation_status = EXCLUDED.validation_status,
            effective_verification = EXCLUDED.effective_verification,
            superseded = EXCLUDED.superseded,
            applied_seq = EXCLUDED.applied_seq,
            updated_at = now()
        WHERE feature_contract_validation_state.applied_seq < EXCLUDED.applied_seq
        """,
        (contract_id, status, verification, superseded, seq))
    return True


def catch_up(conn: DbConn, *, batch: int = 500) -> int:
    """Fold every event with ``seq > checkpoint_seq`` in ascending ``seq`` order, advancing the
    ``projection_checkpoints`` checkpoint to the last event seen. Returns the count applied.

    Fail-open-but-audited on a poison event (C4-T2 brief): roll the event's partial writes back to a
    savepoint, mark its contract degraded (``projection_degraded``), record the skip
    (``projection_skips`` + the ``projection.skip`` counter), and advance PAST it — so a malformed
    event neither halts the projection nor corrupts other contracts.
    """
    _ensure_checkpoint(conn, PROJECTION_NAME, is_analytics=True)
    checkpoint = _checkpoint_seq(conn, PROJECTION_NAME)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT event_id, contract_id, seq, event_type, payload "
            "FROM feature_contract_validation_event WHERE seq > %s ORDER BY seq ASC LIMIT %s",
            (checkpoint, batch))
        rows = cur.fetchall()

    applied = 0
    last_seq = checkpoint
    for row in rows:
        try:
            with conn.transaction():  # savepoint: discard a poison event's partial writes
                if apply_event(conn, row):
                    applied += 1
        except ProjectionApplyError as exc:
            # MF-2: ONLY a SIGNALLED poison event (malformed/unknown payload, raised by the fold) is
            # fail-open-but-audited — mark degraded, record the skip, advance PAST it. Any OTHER
            # exception (a transient deadlock / lock-timeout) is NOT poison: let it PROPAGATE so the
            # confirm transaction aborts and the event is retried on the next attempt, instead of
            # being silently skipped past (which would serve the pre-event stale stamp as ready).
            _mark_degraded(conn, row["contract_id"], reason=str(exc)[:500], seq=int(row["seq"]))
            conn.execute(
                "INSERT INTO projection_skips (projection_name, event_global_seq, reason) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (PROJECTION_NAME, int(row["seq"]), str(exc)[:500]))
            counters.incr("projection.skip")
        last_seq = int(row["seq"])

    head = _head_seq(conn)
    conn.execute(
        # MF-1 (defense-in-depth): advance the checkpoint MONOTONICALLY. GREATEST never lets a
        # concurrent/late catch_up regress the checkpoint below a higher committed watermark; the
        # FOR-UPDATE lock taken in lock_checkpoint before the emit is the real serializer, this just
        # guards the checkpoint value itself. head_seq stays freshly recorded regardless.
        "UPDATE projection_checkpoints "
        "SET checkpoint_seq = GREATEST(checkpoint_seq, %s), head_seq = %s, updated_at = now() "
        "WHERE projection_name = %s",
        (last_seq, head, PROJECTION_NAME))
    return applied


def _mark_degraded(conn: DbConn, contract_id: str, *, reason: str, seq: int) -> None:
    """Record the affected contract in the generic ``projection_degraded`` ledger. Idempotent per
    contract (PK (projection_name, aggregate, aggregate_id)). ``poison_event_id`` stays NULL: our
    poison event lives in the dedicated validation-event table, not in ``events`` (whose
    ``event_id`` the column FKs), so the auditable ``seq`` is carried in ``poison_seq`` instead."""
    conn.execute(
        """
        INSERT INTO projection_degraded
            (projection_name, aggregate, aggregate_id, reason, poison_event_id, poison_seq)
        VALUES (%s, %s, %s, %s, NULL, %s)
        ON CONFLICT (projection_name, aggregate, aggregate_id)
        DO UPDATE SET reason = EXCLUDED.reason, poison_seq = EXCLUDED.poison_seq,
                      degraded_at = now()
        """,
        (PROJECTION_NAME, AGGREGATE, contract_id, reason, seq))


def reset(conn: DbConn) -> None:
    """Clear the derived state + this projection's checkpoint/skip/degraded rows so a rebuild starts
    from zero and reproduces state IDENTICALLY. The event log (the authority) is untouched."""
    conn.execute("TRUNCATE feature_contract_validation_state")
    conn.execute("DELETE FROM projection_skips WHERE projection_name = %s", (PROJECTION_NAME,))
    conn.execute("DELETE FROM projection_degraded WHERE projection_name = %s", (PROJECTION_NAME,))
    _ensure_checkpoint(conn, PROJECTION_NAME, is_analytics=True)
    conn.execute(
        "UPDATE projection_checkpoints SET checkpoint_seq = 0, head_seq = 0, updated_at = now() "
        "WHERE projection_name = %s",
        (PROJECTION_NAME,))


def rebuild(conn: DbConn) -> None:
    """``reset()`` then deterministically replay ALL events from ``seq``=0. A rebuild reproduces the
    exact state a live ``catch_up`` produced — the load-bearing projection invariant."""
    reset(conn)
    while catch_up(conn) > 0:
        pass


def read_state(conn: DbConn, contract_id: str) -> dict | None:
    """The current ``feature_contract_validation_state`` row for a contract, or None."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM feature_contract_validation_state WHERE contract_id = %s",
            (contract_id,))
        return cur.fetchone()


def is_read_ready(conn: DbConn) -> bool:
    """Per-read FAIL-CLOSED health gate for the validation STATE read model. Returns False when the
    projection is DEGRADED or LAGGED — the caller must then serve UNVERIFIED/unavailable and NEVER
    fall back to the legacy 1003 stamp, so a lagged/degraded projection can never serve a stale
    DATA-CHECKED/design_checked.

    Reuses the C4-T2 detection primitives (never re-implementing them):
      * DEGRADED — ANY poison marker for this projection in ``projection_degraded`` (the same ledger
        ``_mark_degraded`` writes). Projection-wide: a poisoned read model is untrustworthy for every
        contract, so the whole read fails closed (mirrors ``check_projection_readiness``).
      * LAGGED — the projection ``checkpoint_seq`` sits below the max event ``seq``. The lag is
        measured over the DEDICATED ``feature_contract_validation_event`` stream's own seq space
        (via this module's ``_head_seq``), NOT the GLOBAL ``events``/``global_seq`` head that
        ``projections.runner.projection_lag`` uses — this custom projection folds its own table.
    """
    degraded = conn.execute(
        "SELECT 1 FROM projection_degraded WHERE projection_name = %s LIMIT 1",
        (PROJECTION_NAME,)).fetchone() is not None
    if degraded:
        return False
    return _checkpoint_seq(conn, PROJECTION_NAME) >= _head_seq(conn)


class FeatureContractValidationProjection:
    """Thin object adapter over the module functions, exposing the framework's ``name`` /
    ``is_analytics`` identity for registration. ``is_analytics=True`` reflects the fail-OPEN poison
    handling (advance past + record a skip), not a routing through ``run_projection`` — this
    custom projection consumes the dedicated ``feature_contract_validation_event`` stream via
    ``catch_up``."""

    name = PROJECTION_NAME
    is_analytics = True

    def apply_event(self, conn: DbConn, event_row: Mapping) -> bool:
        return apply_event(conn, event_row)

    def catch_up(self, conn: DbConn, *, batch: int = 500) -> int:
        return catch_up(conn, batch=batch)

    def reset(self, conn: DbConn) -> None:
        reset(conn)

    def rebuild(self, conn: DbConn) -> None:
        rebuild(conn)
