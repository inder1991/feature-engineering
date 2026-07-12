"""SPECIALIZED_FACT bridge: land a CONFIRMED (VERIFIED) grain/availability fact onto graph_node.
Modeled on field_resolution._resolve_sensitivity — computes outside the generic resolver and writes
dedicated graph_node columns. The load-bearing truth is the fact stream; this is its projection."""
from __future__ import annotations

from datetime import datetime

from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.upload_catalog import table_ref


def project_table_facts_for_ref(conn, *, source: str, table: str,
                                now: datetime | None = None) -> None:
    """Project the CURRENT verified grain/availability for ONE table onto graph_node — IDEMPOTENTLY.

    CRITICAL: clears every prior is_grain/is_as_of + fact-event-id on this table's columns FIRST,
    then applies only what resolve_fact currently serves (VERIFIED). Without the clear, a grain that
    changed columns, expired, was rejected, or was replaced on re-verify would leave STALE true flags
    on old columns — a silent correctness rot. Set-only projection is not rebuild-safe; clear-then-set
    is. This single-table entry point is also what a future confirm-time hook calls (there is no
    confirm API today; see the scope boundary)."""
    adapter = current_catalog_adapter()
    # 1. Clear this table's specialized-fact projection (rebuild-safe reset).
    conn.execute(
        "UPDATE graph_node SET is_grain = false, grain_fact_event_id = NULL, "
        "is_as_of = false, availability_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (source, table))
    ref = table_ref(source, table)
    # 2. Apply the CONFIRMED grain (VERIFIED only; PROPOSED/absent -> value None -> nothing set).
    # `now` MUST be forwarded: resolve_fact's expiry + drift-freshness guards compare against it,
    # and ingest threads ONE clock basis end-to-end — resolving on the real clock here would
    # fail-close (drift-stale) any fact whose watermark was attested under an injected ingest
    # clock, clearing a just-declared grain right after build_graph set it.
    grain = resolve_fact(conn, adapter, ref, "grain", now=now)
    if grain and grain.value is not None:
        cols = grain.value.get("columns", [])
        # ResolvedFact has NO confirmed_event_id attribute; a VERIFIED overlay fact carries it in
        # .provenance['confirmed_event_id'] (resolve.py _overlay_verified). getattr(...) would silently
        # write NULL — read provenance so the audit-link column is actually populated.
        conn.execute(
            "UPDATE graph_node SET is_grain = true, grain_fact_event_id = %s "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND column_name = ANY(%s)",
            ((grain.provenance or {}).get("confirmed_event_id"), source, table, list(cols)))
    # 3. Apply the CONFIRMED availability.
    avail = resolve_fact(conn, adapter, ref, "availability_time", now=now)
    if avail and avail.value is not None:
        col = avail.value.get("column")
        conn.execute(
            "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND column_name = %s",
            ((avail.provenance or {}).get("confirmed_event_id"), source, table, col))


def project_table_facts(conn, *, source: str, tables, now: datetime | None = None) -> None:
    """Project every table's confirmed grain/availability. Idempotent per table (clear-then-set)."""
    for table in tables:
        project_table_facts_for_ref(conn, source=source, table=table, now=now)
