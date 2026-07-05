from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.contracts.db import DbConn
from featuregen.contracts.errors import ConcurrencyError
from featuregen.idgen import mint_id
from featuregen.overlay.authority import resolve_authority
from featuregen.overlay.facts import OVERLAY_FACT_STALED
from featuregen.overlay.identity import _ref_from_payload
from featuregen.overlay.projection import dependents_of
from featuregen.overlay.reverify_tasks import open_reverify_task
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.runtime.observability import counters


def drift_watermark(conn: DbConn, catalog_source: str) -> datetime | None:
    """The last_completed_at of the most recent SUCCESSFUL drift scan for `catalog_source`, or None
    if none has completed (SP-1.5 Task 4). Read by Task 5's read-time drift-freshness guard."""
    row = conn.execute(
        "SELECT last_completed_at FROM overlay_drift_watermark WHERE catalog_source = %s",
        (catalog_source,),
    ).fetchone()
    return row[0] if row else None


def drift_head_seq(conn: DbConn, catalog_source: str) -> int | None:
    """The global_seq the catalog_source's last drift scan advanced to (SP-1.5 review #2). resolve_fact
    fails closed until the overlay projection checkpoint reaches this, so a just-drifted fact's STALE
    is applied to the read model before it can be served."""
    row = conn.execute(
        "SELECT head_seq FROM overlay_drift_watermark WHERE catalog_source = %s",
        (catalog_source,),
    ).fetchone()
    return row[0] if row else None


def _write_watermark(conn: DbConn, catalog_source: str, now: datetime) -> None:
    # head_seq = the global_seq at drift completion (after this scan's OVERLAY_FACT_STALED appends).
    # The read-time guard requires the overlay projection checkpoint to reach it before serving.
    head_seq = conn.execute("SELECT COALESCE(max(global_seq), 0) FROM events").fetchone()[0]
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, %s, %s) ON CONFLICT (catalog_source) DO UPDATE "
        "SET last_completed_at = EXCLUDED.last_completed_at, last_run_id = EXCLUDED.last_run_id, "
        "head_seq = EXCLUDED.head_seq",
        (catalog_source, now, mint_id("drift"), head_seq),
    )


@dataclass(frozen=True, slots=True)
class Change:
    catalog_source: str
    object_ref: str
    kind: str  # "add" | "drop" | "type_change" | "rename"
    native_oid: str | None = None
    renamed_to: str | None = None


def _type_fingerprint(obj) -> str:
    """Stable fingerprint of the structural shape that, if changed, stales dependents:
    object kind + declared data type. A column's type change (text→varchar) flips this;
    a rename does not (the type is unchanged), so renames are detected by oid, not here."""
    return hashlib.sha256(f"{obj.object_kind}|{obj.data_type}".encode()).hexdigest()


def _load_snapshot(conn: DbConn, catalog_source: str) -> dict[str, dict]:
    """The prior snapshot for ONE catalog_source (SP-1.5 Task 2 — never mixes catalogs)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT object_ref, native_oid, type_fingerprint FROM overlay_catalog_object "
            "WHERE catalog_source = %s",
            (catalog_source,),
        )
        return {
            r[0]: {"native_oid": r[1], "type_fingerprint": r[2]} for r in cur.fetchall()
        }


def _save_snapshot(conn: DbConn, catalog_source: str, current) -> None:
    refs = list(current)
    with conn.cursor() as cur:
        for ref, obj in current.items():
            cur.execute(
                """
                INSERT INTO overlay_catalog_object
                    (catalog_source, object_ref, native_oid, columns_fingerprint,
                     type_fingerprint, updated_at)
                VALUES (%s, %s, %s, %s, %s, now())
                ON CONFLICT (catalog_source, object_ref) DO UPDATE
                   SET native_oid = EXCLUDED.native_oid,
                       columns_fingerprint = EXCLUDED.columns_fingerprint,
                       type_fingerprint = EXCLUDED.type_fingerprint,
                       updated_at = now()
                """,
                (catalog_source, ref, obj.native_oid, "", _type_fingerprint(obj)),
            )
        # Catalog-SCOPED anti-join (SP-1.5 §7): only this catalog's stale rows are pruned — another
        # catalog's snapshot is never touched. Scoped to one source, object_ref is a plain text[]
        # so `<> ALL` is a robust contract (F7's composite-key concern does not arise here).
        if refs:
            cur.execute(
                "DELETE FROM overlay_catalog_object "
                "WHERE catalog_source = %s AND object_ref <> ALL(%s)",
                (catalog_source, refs),
            )
        else:
            cur.execute(
                "DELETE FROM overlay_catalog_object WHERE catalog_source = %s", (catalog_source,)
            )


def detect_catalog_changes(
    conn: DbConn, adapter, *, actor, now: datetime | None = None, open_reverify: bool = True
) -> list[Change]:
    """Snapshot adapter.fingerprint() into overlay_catalog_object and diff it against the
    prior snapshot (§8). Because fact_key is name-based, a rename always yields a NEW key:
    the old object is STALEd and the renamed object is onboarded afresh; the stable native
    oid is used only to LABEL the change as a rename (renamed_to). For every drop /
    type-change / rename(old side), each dependent fact found via the general dependency
    index is STALEd (CAS on confirmed_event_id) + gets a re-verify task. Returns all
    detected changes; the snapshot is advanced to `current` at the end."""
    csource = adapter.catalog_source
    current = adapter.fingerprint()
    prior = _load_snapshot(conn, csource)
    cur_refs, prior_refs = set(current), set(prior)
    added, dropped = cur_refs - prior_refs, prior_refs - cur_refs

    dropped_by_oid = {prior[r]["native_oid"]: r for r in dropped if prior[r]["native_oid"]}
    added_by_oid = {current[r].native_oid: r for r in added if current[r].native_oid}
    renamed = {old: added_by_oid[oid] for oid, old in dropped_by_oid.items() if oid in added_by_oid}
    renamed_new = set(renamed.values())

    changes: list[Change] = []
    for old, new in renamed.items():
        changes.append(Change(csource, old, "rename", prior[old]["native_oid"], renamed_to=new))
    for r in dropped:
        if r not in renamed:
            changes.append(Change(csource, r, "drop", prior[r]["native_oid"]))
    for r in added:
        if r not in renamed_new:
            changes.append(Change(csource, r, "add", current[r].native_oid))
    for r in cur_refs & prior_refs:
        if _type_fingerprint(current[r]) != prior[r]["type_fingerprint"]:
            changes.append(Change(csource, r, "type_change", current[r].native_oid))

    try:
        for ch in changes:
            if ch.kind in ("drop", "type_change", "rename"):
                _stale_dependents(conn, adapter, ch, actor=actor, open_reverify=open_reverify)
    except ConcurrencyError:
        # A concurrent confirm conflicted with a dependent stale (review #7). Return WITHOUT
        # advancing the snapshot or watermark so the next scan re-detects + re-stales (idempotent:
        # an already-STALE fact is a CAS no-op). Any partial STALED appends from this scan commit
        # harmlessly; the un-advanced snapshot is what guarantees the change is not laundered.
        # Skip-LOUD (review #11): count the truncated scan so a re-detect loop is observable, not
        # silently indistinguishable from a completed scan.
        counters.incr("overlay.drift.truncated_concurrent_confirm")
        return changes

    _save_snapshot(conn, csource, current)
    # Atomic completion (SP-1.5 Task 4): the watermark advances in the SAME transaction as the
    # snapshot advance + dependent-staling above, so a crash before commit re-detects the drift next
    # run (never laundered). This is what Task 5's read-time freshness guard attests to.
    _write_watermark(conn, csource, now or datetime.now(UTC))
    return changes


def _stale_one(
    conn: DbConn, adapter, fact_key: str, *, change_ref: str, actor, open_reverify: bool = True
) -> str | None:
    """STALE one dependent fact (VERIFIED → STALE) targeting its current confirmed_event_id.
    CAS no-op when the fact has already advanced (not VERIFIED — already STALE/REVERIFY/
    REJECTED, or a concurrent confirm bumps the stream → ConcurrencyError). On success
    append OVERLAY_FACT_STALED + open the re-verify task(s) (one task PER side for an
    approved_join). Returns the event id or None."""
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
        # A concurrent confirm bumped the stream mid-scan. Do NOT swallow-and-continue (review #7):
        # re-raise so detect_catalog_changes leaves the snapshot + watermark UN-advanced and the
        # change is re-detected next scan, instead of laundering it (the fact would stay VERIFIED
        # referencing a dropped/retyped object until TTL).
        raise
    if open_reverify:
        # Governance path: route the stale to the data owner(s). The upload-catalog ingest
        # (no owners) passes open_reverify=False — the fact still STALEs via the append above,
        # but no owner task is opened.
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


def _stale_dependents(
    conn: DbConn, adapter, change: Change, *, actor, open_reverify: bool = True
) -> None:
    """For every fact whose value references the changed object (general dependency index,
    both sides of an approved_join), STALE it. ref_object is the changed object's
    display_object_ref string — the same key the projection records in
    overlay_fact_dependency."""
    change_ref = f"{change.kind}:{change.object_ref}"
    for fact_key in dependents_of(conn, change.catalog_source, change.object_ref):
        _stale_one(conn, adapter, fact_key, change_ref=change_ref, actor=actor,
                   open_reverify=open_reverify)
