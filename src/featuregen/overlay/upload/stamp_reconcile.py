"""Table-fact stamp reconciliation (richness Task 5).

The overlay's VERIFIED grain/availability facts are projected onto graph_node as provenance
stamps (`grain_fact_event_id` / `availability_fact_event_id` on the table node and its member
columns — `table_fact_projection.py`). Two paths can leave a VERIFIED fact UN-stamped with no
later backfill:

* an ingest whose `table_fact_projection` stage records ``lagged`` skips the projection AFTER
  `build_graph` already wiped every node — the stamps stay NULL until a NEXT caught-up ingest of
  the same source, which may never come;
* out-of-band mutation (a migration, a manual fix) nulls or retargets a stamp.

`reconcile_table_fact_stamps` DETECTS: it compares every VERIFIED grain/availability fact's
`confirmed_event_id` against the TABLE node's stamp (the one-row-per-fact location the audit
script reads) and returns the disagreements. It never writes. `repair_table_fact_stamps`
REPAIRS: for each drifted fact it re-runs the EXISTING projection
(`project_table_facts_for_ref`), so the restored event ids are re-read from `resolve_fact` —
never fabricated — and a fact `resolve_fact` refuses to serve (drift-stale watermark, expiry) is
left drifted-and-visible rather than force-stamped or, worse, wiped (the c715a16d hazard: an
unguarded clear-then-set on an unservable fact would erase file-declared flags).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay.identity import CatalogObjectRef, _norm, _ref_from_payload
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.table_fact_projection import project_table_facts_for_ref
from featuregen.overlay.upload.upload_catalog import table_ref
from featuregen.projections.runner import projection_lag
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)

# fact_type -> the graph_node stamp column it projects to. Closed internal map — never
# caller-supplied — so interpolating the column name is safe.
_STAMP_COLUMNS = {
    "grain": "grain_fact_event_id",
    "availability_time": "availability_fact_event_id",
}


@dataclass(frozen=True, slots=True)
class StampDrift:
    """One VERIFIED table fact whose graph stamp disagrees with the overlay truth."""
    object_ref: str            # the fact's display ref, e.g. "public.accounts"
    fact_type: str             # "grain" | "availability_time"
    overlay_event_id: str | None   # the fact's confirmed_event_id (the truth)
    stamped_event_id: str | None   # what the table node carries (None = never stamped/wiped)


def _verified_table_facts(conn, *, source: str | None = None):
    """``(source, table, object_ref, fact_type, confirmed_event_id)`` for every VERIFIED
    grain/availability fact — source/table recovered from the DRAFT event's TYPED ref (the
    structural truth; `overlay_fact_state.object_ref` is display-only and source-less). A fact
    whose stream is empty or whose ref will not decode is skipped-loud, never fatal."""
    rows = conn.execute(
        "SELECT fact_key, object_ref, fact_type, confirmed_event_id FROM overlay_fact_state "
        "WHERE status='VERIFIED' AND fact_type IN ('grain','availability_time') "
        "ORDER BY object_ref, fact_type").fetchall()
    want = _norm(source) if source is not None else None
    for key, object_ref, fact_type, confirmed_event_id in rows:
        stream = load_fact(conn, key)
        if not stream:
            counters.incr("overlay.stamp_reconcile.ref_undecodable")
            logger.warning("stamp reconcile: fact %s has no event stream — skipped", key)
            continue
        try:
            ref = _ref_from_payload(stream[0].payload["catalog_object_ref"])
        except Exception:  # noqa: BLE001 — one corrupt payload must not abort the reconcile
            counters.incr("overlay.stamp_reconcile.ref_undecodable")
            logger.warning("stamp reconcile: fact %s ref undecodable — skipped", key,
                           exc_info=True)
            continue
        if not isinstance(ref, CatalogObjectRef) or not ref.catalog_source:
            counters.incr("overlay.stamp_reconcile.ref_undecodable")
            logger.warning("stamp reconcile: fact %s is not a sourced table ref — skipped", key)
            continue
        src = _norm(ref.catalog_source)
        if want is not None and src != want:
            continue
        yield src, _norm(ref.table), object_ref, fact_type, confirmed_event_id


def reconcile_table_fact_stamps(conn, *, source: str | None = None) -> tuple[StampDrift, ...]:
    """Every VERIFIED grain/availability fact whose TABLE-node stamp disagrees with its
    `confirmed_event_id` — NULL and WRONG stamps both count. READ-ONLY. ``source`` scopes the
    check to one catalog (the ingest tail passes the uploading source); ``None`` checks all.
    A fact whose table is absent from the graph (dropped / not yet uploaded) is not drift —
    there is no node to stamp; the drift machinery owns that divergence."""
    drifts: list[StampDrift] = []
    for src, table, object_ref, fact_type, confirmed_event_id in _verified_table_facts(
            conn, source=source):
        col = _STAMP_COLUMNS[fact_type]
        row = conn.execute(
            f"SELECT {col} FROM graph_node WHERE catalog_source = %s AND table_name = %s "
            "AND kind = 'table'", (src, table)).fetchone()
        if row is None:
            continue
        stamped = row[0]
        if stamped != confirmed_event_id:
            drifts.append(StampDrift(object_ref=object_ref, fact_type=fact_type,
                                     overlay_event_id=confirmed_event_id,
                                     stamped_event_id=stamped))
    return tuple(drifts)


def repair_table_fact_stamps(conn, adapter, *, now: datetime) -> tuple[StampDrift, ...]:
    """Re-stamp every drifted table fact by RE-RUNNING the existing projection; returns the drift
    that REMAINS (empty = fully repaired). Per drifted fact: `resolve_fact` is consulted FIRST —
    only a fact it actually serves re-projects (scoped to that fact type), so an unservable
    VERIFIED fact (stale drift watermark, expiry) can never trigger the clear that would wipe a
    file-declared is_grain/is_as_of; it stays drifted and visible instead. Fail-closed under
    projection lag: a stale read model must not drive graph writes."""
    if projection_lag(conn, "overlay") > 0:
        counters.incr("overlay.stamp_reconcile.repair_skipped_projection_lag")
        logger.warning("stamp repair: overlay projection lags — no writes; drift stands until "
                       "the projection catches up")
        return reconcile_table_fact_stamps(conn)
    for drift in reconcile_table_fact_stamps(conn):
        # Re-derive the fact's (source, table) — StampDrift deliberately carries only the display
        # ref, so walk the enumerator for the matching typed identity.
        for src, table, object_ref, fact_type, _confirmed in _verified_table_facts(conn):
            if object_ref != drift.object_ref or fact_type != drift.fact_type:
                continue
            resolved = resolve_fact(conn, adapter, table_ref(src, table), fact_type, now=now)
            if resolved is None or resolved.value is None:
                counters.incr("overlay.stamp_reconcile.repair_unservable")
                logger.warning("stamp repair: VERIFIED %s on %s.%s is not servable "
                               "(watermark/expiry) — left drifted, nothing cleared",
                               fact_type, src, table)
                break
            project_table_facts_for_ref(conn, source=src, table=table,
                                        only_fact_type=fact_type, now=now, adapter=adapter)
            break
    return reconcile_table_fact_stamps(conn)
