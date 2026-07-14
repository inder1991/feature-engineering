"""SPECIALIZED_FACT bridge: land a CONFIRMED (VERIFIED) grain/availability fact onto graph_node.
Modeled on field_resolution._resolve_sensitivity — computes outside the generic resolver and writes
dedicated graph_node columns. The load-bearing truth is the fact stream; this is its projection."""
from __future__ import annotations

from datetime import datetime

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.upload_catalog import table_ref


def project_table_facts_for_ref(conn, *, source: str, table: str,
                                declared_grain: set[str] | None = None,
                                declared_as_of: set[str] | None = None,
                                only_fact_type: str | None = None,
                                now: datetime | None = None) -> None:
    """Project the CURRENT verified grain/availability for ONE table onto graph_node — IDEMPOTENTLY.

    CRITICAL: clears every prior is_grain/is_as_of + fact-event-id on this table's columns FIRST,
    then applies only what resolve_fact currently serves (VERIFIED). Without the clear, a grain that
    changed columns, expired, was rejected, or was replaced on re-verify would leave STALE true flags
    on old columns — a silent correctness rot. Set-only projection is not rebuild-safe; clear-then-set
    is. This single-table entry point is also what a future confirm-time hook calls (there is no
    confirm API today; see the scope boundary).

    ``declared_grain`` / ``declared_as_of`` are the columns THIS upload declares as grain / as-of (a
    file/source attestation ``build_graph`` just wrote is_grain/is_as_of=true for). The clear SPARES
    them: a file-declared flag is byte-for-byte final (pre-Phase-2 behaviour) and must survive even
    when the governed grain/availability fact drift-STALEs (resolve_fact then serves None). The clear
    still resets NON-declared columns, so a prior-cycle CONFIRMED grain on an undeclared column (the
    bridge's real purpose) still re-projects.

    ``only_fact_type`` (whole-branch review FIX 1) scopes the clear-then-set to ONE fact type:
    "grain" runs only the is_grain clear + grain set; "availability_time" runs only the is_as_of
    clear + availability set. The confirm-time bridge (`project_verified_table_fact`) passes the
    just-confirmed type so a single-fact confirm NEVER touches the other flag — under a stale drift
    watermark resolve_fact refuses to re-serve the untouched fact, so an unscoped clear would wipe
    a file-declared grain/as-of until the next re-upload. ``None`` (the default — the ingest
    re-projection path) keeps the full both-types clear-then-set, byte-for-byte."""
    adapter = current_catalog_adapter()
    declared_grain = declared_grain or set()
    declared_as_of = declared_as_of or set()
    project_grain = only_fact_type in (None, "grain")
    project_as_of = only_fact_type in (None, "availability_time")
    # 1. Clear this table's specialized-fact projection (rebuild-safe reset), EXCLUDING the columns
    #    this upload declares (their file-declared flag is final and must not be wiped by a staled
    #    governed fact). Two scoped UPDATEs because is_grain and is_as_of are independent flags —
    #    each gated on `only_fact_type` so a single-fact confirm never clears the other flag.
    if project_grain:
        conn.execute(
            "UPDATE graph_node SET is_grain = false, grain_fact_event_id = NULL "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND NOT (column_name = ANY(%s))",
            (source, table, list(declared_grain)))
    if project_as_of:
        conn.execute(
            "UPDATE graph_node SET is_as_of = false, availability_fact_event_id = NULL "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND NOT (column_name = ANY(%s))",
            (source, table, list(declared_as_of)))
    ref = table_ref(source, table)
    # 2. Apply the CONFIRMED grain (VERIFIED only; PROPOSED/absent -> value None -> nothing set).
    # `now` MUST be forwarded: resolve_fact's expiry + drift-freshness guards compare against it,
    # and ingest threads ONE clock basis end-to-end — resolving on the real clock here would
    # fail-close (drift-stale) any fact whose watermark was attested under an injected ingest
    # clock, clearing a just-declared grain right after build_graph set it.
    if project_grain:
        grain = resolve_fact(conn, adapter, ref, "grain", now=now)
        if grain and grain.value is not None:
            cols = grain.value.get("columns", [])
            # ResolvedFact has NO confirmed_event_id attribute; a VERIFIED overlay fact carries it
            # in .provenance['confirmed_event_id'] (resolve.py _overlay_verified). getattr(...)
            # would silently write NULL — read provenance so the audit-link column is populated.
            conn.execute(
                "UPDATE graph_node SET is_grain = true, grain_fact_event_id = %s "
                "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
                "AND column_name = ANY(%s)",
                ((grain.provenance or {}).get("confirmed_event_id"), source, table, list(cols)))
    # 3. Apply the CONFIRMED availability.
    if project_as_of:
        avail = resolve_fact(conn, adapter, ref, "availability_time", now=now)
        if avail and avail.value is not None:
            col = avail.value.get("column")
            conn.execute(
                "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
                "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
                "AND column_name = %s",
                ((avail.provenance or {}).get("confirmed_event_id"), source, table, col))


def project_table_facts(conn, *, source: str, tables,
                        declared_grain: dict[str, set[str]] | None = None,
                        declared_as_of: dict[str, set[str]] | None = None,
                        now: datetime | None = None) -> None:
    """Project every table's confirmed grain/availability. Idempotent per table (clear-then-set).

    ``declared_grain`` / ``declared_as_of`` map a table to the columns the current upload declares as
    grain / as-of; those columns' file-declared flags are SPARED from the clear (see
    :func:`project_table_facts_for_ref`)."""
    declared_grain = declared_grain or {}
    declared_as_of = declared_as_of or {}
    for table in tables:
        project_table_facts_for_ref(
            conn, source=source, table=table,
            declared_grain=declared_grain.get(table), declared_as_of=declared_as_of.get(table),
            now=now)


_TABLE_FACT_TYPES = ("grain", "availability_time")

# The governance worklist reads platform-admin governance-queue tasks (grain/availability route
# there because UploadContextAdapter.owner_of -> None). get_task_proposal authorizes on role_claims,
# so the reader MUST hold platform-admin or every read is denied. Subject-less system reader —
# consumed by table_fact_governance.list_open_table_fact_proposals_governance. authenticated=False:
# get_task_proposal authorizes on role_claims (never .authenticated), and a fabricated authenticated
# identity is forbidden outside the sanctioned trust roots (mirrors enrich_llm._ENRICH_ACTOR).
_WORKLIST_READER = IdentityEnvelope(
    subject="system:table-fact-worklist", actor_kind="service", authenticated=False,
    auth_method="internal", role_claims=("platform-admin",))
