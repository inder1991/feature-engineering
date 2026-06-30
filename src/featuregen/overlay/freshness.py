from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from psycopg.rows import dict_row

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import NewTimer
from featuregen.contracts.errors import ConcurrencyError
from featuregen.identity.build import build_service_identity
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.facts import OVERLAY_FACT_EXPIRED, OVERLAY_FACT_STALED
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, ColumnPair
from featuregen.overlay.projection import dependents_of
from featuregen.overlay.reverify_tasks import open_reverify_task
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.runtime.timers import schedule_timer

__all__ = ["open_reverify_task"]


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


def _ref_from_payload(d):
    """Rebuild the typed ref stored on OVERLAY_FACT_PROPOSED.payload['catalog_object_ref']
    (an asdict() of CatalogObjectRef, or of ApprovedJoinRef for approved_join)."""
    if "column_pairs" in d:
        return ApprovedJoinRef(
            from_ref=CatalogObjectRef(**d["from_ref"]),
            to_ref=CatalogObjectRef(**d["to_ref"]),
            column_pairs=tuple(ColumnPair(**p) for p in d["column_pairs"]),
            cardinality=d["cardinality"],
        )
    return CatalogObjectRef(**d)


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
    authority (one task PER side for an approved_join, pin 19), carrying the target
    confirmed_event_id (prior_value flows through the proposal projection → get_task_proposal,
    Phase 4.6). Returns True iff OVERLAY_FACT_EXPIRED was appended."""
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


def fire_due_overlay_expiries(conn: DbConn, *, now: datetime) -> int:
    """Explicit transactional poller (overview decision 5) — NOT a HandlerRegistry handler.
    The SP-0 timer runtime can't carry fact_key/confirmed_event_id to an overlay handler
    nor open a gate task, so freshness owns its own driver. SELECT due overlay_expiry timers
    FOR UPDATE SKIP LOCKED (row locks are held by the transaction until commit, so multiple
    pollers never double-process), and for each: read fact_key from the timer's aggregate_id
    and confirmed_event_id from its payload, CAS-apply the expiry (append OVERLAY_FACT_EXPIRED
    + open the re-verify task; no-op if a newer FACT_CONFIRMED superseded the target), and
    mark the timer `fired`. The poller acts as a system service principal and resolves the
    catalog adapter via the single-source accessor. Returns the number of OVERLAY_FACT_EXPIRED
    events emitted; a superseded timer is consumed (marked fired) without emitting."""
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


@dataclass(frozen=True, slots=True)
class Change:
    object_ref: str
    kind: str  # "add" | "drop" | "type_change" | "rename"
    native_oid: str | None = None
    renamed_to: str | None = None


def _type_fingerprint(obj) -> str:
    """Stable fingerprint of the structural shape that, if changed, stales dependents:
    object kind + declared data type. A column's type change (text→varchar) flips this;
    a rename does not (the type is unchanged), so renames are detected by oid, not here."""
    return hashlib.sha256(f"{obj.object_kind}|{obj.data_type}".encode()).hexdigest()


def _load_snapshot(conn: DbConn) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_ref, native_oid, type_fingerprint FROM overlay_catalog_object"
        )
        return {
            r[0]: {"native_oid": r[1], "type_fingerprint": r[2]} for r in cur.fetchall()
        }


def _save_snapshot(conn: DbConn, current) -> None:
    refs = list(current)
    with conn.cursor() as cur:
        for ref, obj in current.items():
            cur.execute(
                """
                INSERT INTO overlay_catalog_object
                    (object_ref, native_oid, columns_fingerprint, type_fingerprint, updated_at)
                VALUES (%s, %s, %s, %s, now())
                ON CONFLICT (object_ref) DO UPDATE
                   SET native_oid = EXCLUDED.native_oid,
                       columns_fingerprint = EXCLUDED.columns_fingerprint,
                       type_fingerprint = EXCLUDED.type_fingerprint,
                       updated_at = now()
                """,
                (ref, obj.native_oid, "", _type_fingerprint(obj)),
            )
        if refs:
            cur.execute(
                "DELETE FROM overlay_catalog_object WHERE object_ref <> ALL(%s)", (refs,)
            )
        else:
            cur.execute("DELETE FROM overlay_catalog_object")


def detect_catalog_changes(conn: DbConn, adapter, *, actor) -> list[Change]:
    """Snapshot adapter.fingerprint() into overlay_catalog_object and diff it against the
    prior snapshot (§8). Because fact_key is name-based, a rename always yields a NEW key:
    the old object is STALEd and the renamed object is onboarded afresh; the stable native
    oid is used only to LABEL the change as a rename (renamed_to). For every drop /
    type-change / rename(old side), each dependent fact found via the general dependency
    index is STALEd (CAS on confirmed_event_id) + gets a re-verify task. Returns all
    detected changes; the snapshot is advanced to `current` at the end."""
    current = adapter.fingerprint()
    prior = _load_snapshot(conn)
    cur_refs, prior_refs = set(current), set(prior)
    added, dropped = cur_refs - prior_refs, prior_refs - cur_refs

    dropped_by_oid = {prior[r]["native_oid"]: r for r in dropped if prior[r]["native_oid"]}
    added_by_oid = {current[r].native_oid: r for r in added if current[r].native_oid}
    renamed = {old: added_by_oid[oid] for oid, old in dropped_by_oid.items() if oid in added_by_oid}
    renamed_new = set(renamed.values())

    changes: list[Change] = []
    for old, new in renamed.items():
        changes.append(Change(old, "rename", prior[old]["native_oid"], renamed_to=new))
    for r in dropped:
        if r not in renamed:
            changes.append(Change(r, "drop", prior[r]["native_oid"]))
    for r in added:
        if r not in renamed_new:
            changes.append(Change(r, "add", current[r].native_oid))
    for r in cur_refs & prior_refs:
        if _type_fingerprint(current[r]) != prior[r]["type_fingerprint"]:
            changes.append(Change(r, "type_change", current[r].native_oid))

    for ch in changes:
        if ch.kind in ("drop", "type_change", "rename"):
            _stale_dependents(conn, adapter, ch, actor=actor)

    _save_snapshot(conn, current)
    return changes


def _stale_one(conn: DbConn, adapter, fact_key: str, *, change_ref: str, actor) -> str | None:
    """STALE one dependent fact (VERIFIED → STALE) targeting its current confirmed_event_id.
    CAS no-op when the fact has already advanced (not VERIFIED — already STALE/REVERIFY/
    REJECTED, or a concurrent confirm bumps the stream → ConcurrencyError). On success
    append OVERLAY_FACT_STALED + open the re-verify task(s) (one task PER side for an
    approved_join, pin 19). Returns the event id or None."""
    stream = load_fact(conn, fact_key)
    if not stream:
        return None
    state = fold_overlay_state(stream)
    if state.status != "VERIFIED":
        return None  # CAS: already advanced
    try:
        env = append_overlay_event(
            conn,
            fact_key=fact_key,
            type=OVERLAY_FACT_STALED,
            payload={
                "catalog_change_ref": change_ref,
                "stales_confirmed_event_id": state.confirmed_event_id,
            },
            actor=actor,
            expected_version=stream[-1].stream_version,
        )
    except ConcurrencyError:
        return None  # a concurrent confirm advanced the stream
    ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
    authority = resolve_authority(conn, adapter, ref, state.fact_type)
    open_reverify_task(
        conn,
        fact_key=fact_key,
        fact_type=state.fact_type,
        target_confirmed_event_id=state.confirmed_event_id,
        authority=authority,
        actor=actor,
    )
    return env.event_id


def _stale_dependents(conn: DbConn, adapter, change: Change, *, actor) -> None:
    """For every fact whose value references the changed object (general dependency index,
    both sides of an approved_join), STALE it. ref_object is the changed object's
    display_object_ref string — the same key the projection records in
    overlay_fact_dependency."""
    change_ref = f"{change.kind}:{change.object_ref}"
    for fact_key in dependents_of(conn, change.object_ref):
        _stale_one(conn, adapter, fact_key, change_ref=change_ref, actor=actor)
