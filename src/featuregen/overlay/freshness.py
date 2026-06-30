from __future__ import annotations

import hashlib
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.contracts.errors import ConcurrencyError
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.expiry import (
    _ref_from_payload,
    fire_due_overlay_expiries,
    schedule_expiry,
)
from featuregen.overlay.facts import OVERLAY_FACT_STALED
from featuregen.overlay.projection import dependents_of
from featuregen.overlay.reverify_tasks import open_reverify_task
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact

__all__ = [
    "schedule_expiry",
    "fire_due_overlay_expiries",
    "open_reverify_task",
]


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
