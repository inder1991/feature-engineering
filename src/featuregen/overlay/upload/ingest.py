from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import datetime

from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.brake import large_change_brake
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.enrich import classify_domains, draft_definitions, enrich_concepts
from featuregen.overlay.upload.graph import (
    add_column_row,
    build_graph,
    governed_join_proposal,
    governed_joins_enabled,
    parse_join_ref,
)
from featuregen.overlay.upload.review_queue import persist_quarantine
from featuregen.overlay.upload.source_profile import SourceCapabilityProfile
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref
from featuregen.projections.runner import projection_lag, run_projection
from featuregen.runtime.observability import counters

logger = logging.getLogger(__name__)


def _drain_projection(conn) -> None:
    """Run the overlay projection until caught up. A single run_projection caps at 500 events and an
    upload emits 2 per (re)asserted fact, so one pass on a large upload leaves the dependency index
    stale when detect_catalog_changes reads it (false stale / missed drop). Each pass advances the
    checkpoint, so this terminates (a partial batch = caught up or poison-halted)."""
    while run_projection(conn, OverlayProjection()) >= 500:
        pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str            # "ingested" | "held" | "rejected"
    reason: str | None
    asserted: int
    staled: int
    quarantined: int
    flagged: str | None = None   # a soft-gate note (e.g. first upload — review recommended)


def _table_facts(rows: list[CanonicalRow]):
    """Yield (table, fact_type, value) for grain + availability_time facts."""
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    for table, trows in by_table.items():
        grain_cols = [r.column for r in trows if r.is_grain]
        if grain_cols:
            yield table, "grain", {"columns": grain_cols, "is_unique": True}
        as_of_row = next((r for r in trows if r.as_of), None)
        if as_of_row:
            # Use the declared basis when valid; default to posted_at (M8 — no longer hard-coded).
            basis = as_of_row.as_of_basis if as_of_row.as_of_basis in (
                "posted_at", "ingested_at") else "posted_at"
            yield table, "availability_time", {"column": as_of_row.column, "basis": basis}


def _assert_fact(conn, source: str, table: str, fact_type: str, value: dict, *, actor) -> bool:
    """Assert a fact, or RE-assert it when the upload changed its value or it is not currently
    VERIFIED. Skipping only-on-existence (the original bug) served a stale value forever (B1) and
    left a staled fact stuck unservable after the file was fixed (M1). We diff on the value: skip
    only when the stream is already VERIFIED with the identical value."""
    fk = fact_key(table_ref(source, table), fact_type)
    stream = load_fact(conn, fk)
    if stream:
        state = fold_overlay_state(stream)
        if state.status == "VERIFIED" and state.value == value:
            return False   # genuinely unchanged -> skip (cheap re-upload)
    # New fact, a changed value, or a non-VERIFIED (STALE/REVERIFY/REJECTED) stream -> (re)assert.
    base = stream[-1].stream_version if stream else 0
    draft = append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=actor, expected_version=base, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": fact_type,
            "proposed_value": value, "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": actor.subject})
    append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=actor, expected_version=base + 1, payload={
            "value": value, "confirmers": [{"subject": actor.subject, "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})
    return True


def _propose_governed_joins(conn, rows: list[CanonicalRow], *, actor) -> None:
    """Route each declared `joins_to` into the governed approved_join path via `propose_fact`, behind
    OVERLAY_GOVERNED_JOINS=1 (the caller gates on `governed_joins_enabled()`).

    ADVISORY / fail-soft (spec §12.1): this NEVER aborts the upload. A malformed `joins_to` is
    skipped-loud with its parse diagnostic; a `propose_fact` failure is logged and counted.

    ADAPTER-GATED (Phase-1 dependency): `propose_fact` resolves `current_catalog_adapter()`, which the
    UPLOAD request path does not yet register (only the worker/deployment does). When no adapter is
    wired we skip-loud rather than crash — the display-only edge marking (graph.py) still happens, so
    turning the flag on is safe today; the actual proposal dispatch activates once the upload-context
    adapter lands. The flag is default-OFF, so production behaviour is unchanged."""
    # Imported lazily: propose_fact -> proposal_commands resolves the catalog adapter at import-use
    # time, and the pure builder/parser tests must import graph.py without pulling the command stack.
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact

    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.governed_joins.skipped_no_adapter")
        logger.warning("OVERLAY_GOVERNED_JOINS is on but no catalog adapter is registered in the "
                       "upload flow — skipping approved_join proposals (Phase-1: wire the "
                       "upload-context adapter). Display-only edges are still marked.")
        return

    for r in rows:
        if not r.joins_to:
            continue
        ref = governed_join_proposal(r)
        if ref is None:
            counters.incr("overlay.governed_joins.skipped_malformed")
            logger.warning("skipping governed join for %s.%s: %s", r.table, r.column,
                           parse_join_ref(r.joins_to).diagnostic)
            continue
        value = {
            "from_ref": asdict(ref.from_ref),
            "to_ref": asdict(ref.to_ref),
            "column_pairs": [{"from_col": p.from_col, "to_col": p.to_col} for p in ref.column_pairs],
            "cardinality": ref.cardinality,
        }
        try:
            result = propose_fact(conn, Command(
                "propose_fact", "overlay_fact", None,
                {"ref": ref, "fact_type": "approved_join", "proposed_value": value},
                actor, proposal_fingerprint(value)))
        except Exception:  # noqa: BLE001 — advisory: a proposal failure must never fail an upload
            counters.incr("overlay.governed_joins.propose_error")
            logger.warning("advisory governed-join proposal raised for %s.%s -> %s",
                           r.table, r.column, r.joins_to, exc_info=True)
            continue
        if not result.accepted:
            # A deny (e.g. a duplicate of an already-pending/verified join) is expected on re-upload —
            # advisory, not an error. Counted so the seam's activity is observable.
            counters.incr("overlay.governed_joins.propose_denied")
            logger.info("governed-join proposal for %s.%s not accepted: %s", r.table, r.column,
                        result.denied_reason)


def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None, client=None,
                  profile: SourceCapabilityProfile | None = None) -> IngestResult:
    # `profile` (spec §U) makes validation profile-aware: a glossary upload's `type="unknown"` rows
    # pass, while a technical upload (or the default `profile=None`) still requires a real type.
    vr = validate_rows(rows, catalog_source, profile=profile)
    if vr.structural_error:
        return IngestResult("rejected", vr.structural_error, 0, 0, len(vr.quarantined))

    upload = UploadCatalog(catalog_source, vr.good)
    brake = large_change_brake(conn, catalog_source, upload)
    if brake.held:
        # persist the quarantine even when held, so a reviewer can see WHY this upload's rows failed
        # (was: returned before persist_quarantine -> the queue still showed the previous upload).
        persist_quarantine(conn, catalog_source, vr.quarantined)
        logger.warning("upload of %r held by the large-change brake: %s", catalog_source, brake.reason)
        return IngestResult("held", brake.reason, 0, 0, len(vr.quarantined))

    asserted = 0
    for table, fact_type, value in _table_facts(vr.good):
        if _assert_fact(conn, catalog_source, table, fact_type, value, actor=actor):
            asserted += 1

    _drain_projection(conn)   # fully catch up BEFORE the diff reads the dependency index (>500-event uploads)
    if projection_lag(conn, "overlay") > 0:
        # The drain reached a poison-HALT, not head: the dependency index is stale, so drift would
        # stale NOTHING for a just-dropped/changed column yet still advance the snapshot — laundering
        # the change for a full TTL. Skip drift this upload (same guard as the worker); it re-detects
        # once the projection catches up. The upload's facts still assert; the snapshot is NOT advanced.
        counters.incr("overlay.drift.skipped_projection_lag")
        logger.warning("overlay projection lags after ingest of %r — skipping catalog-change detection "
                       "to avoid laundering drift (re-runs when the projection catches up)", catalog_source)
        changes = []
    else:
        changes = detect_catalog_changes(conn, upload, actor=actor, now=now, open_reverify=False)
        _drain_projection(conn)
    staled = sum(1 for c in changes if c.kind in ("drop", "type_change", "rename"))

    concepts = definitions = domains = None
    if client is not None:
        # Three INDEPENDENT advisory failure domains (spec C1): a failure in one task must not
        # discard another's already-computed enrichment. Each degrades search, never the facts.
        try:
            concepts = enrich_concepts(conn, vr.good, client, actor)
        except Exception:  # noqa: BLE001
            logger.warning("advisory concept enrichment failed for %r", catalog_source, exc_info=True)
        try:
            definitions = draft_definitions(conn, vr.good, client, actor, concepts=concepts)
        except Exception:  # noqa: BLE001
            logger.warning("advisory definition enrichment failed for %r", catalog_source, exc_info=True)
        try:
            domains = classify_domains(conn, vr.good, client, actor)
        except Exception:  # noqa: BLE001
            logger.warning("advisory domain enrichment failed for %r", catalog_source, exc_info=True)
    build_graph(conn, catalog_source, vr.good, concepts, definitions, domains)
    if governed_joins_enabled():
        # Governed seam (Task 7 / §12.1): the raw 'joins' edges just written are display-only; route
        # each declared join into the governed approved_join path. Advisory/fail-soft + adapter-gated.
        _propose_governed_joins(conn, vr.good, actor=actor)
    persist_quarantine(conn, catalog_source, vr.quarantined)
    flagged = (f"first upload of '{catalog_source}' ({len(vr.good)} objects) — review recommended"
               if brake.is_first_upload else None)
    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined), flagged)


def _bool(v) -> bool:
    return v is True or (isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"))


def _row_from_raw(raw: dict, catalog_source: str) -> CanonicalRow:
    """Rebuild a CanonicalRow from a quarantine `raw` dict merged with the reviewer's edits."""
    def s(k: str) -> str:
        return str(raw.get(k) or "")
    return CanonicalRow(
        source=s("source") or catalog_source, table=s("table"), column=s("column"), type=s("type"),
        is_grain=_bool(raw.get("is_grain")), as_of=_bool(raw.get("as_of")),
        as_of_basis=s("as_of_basis"), definition=s("definition"), sensitivity=s("sensitivity"),
        joins_to=s("joins_to"), cardinality=s("cardinality"), additivity=s("additivity"),
        unit=s("unit"), currency=s("currency"), entity=s("entity"))


def resolve_quarantine_row(conn, catalog_source: str, row_index: int, edits: dict, *,
                           actor) -> tuple[bool, str]:
    """Apply a reviewer's inline fix to a quarantined row: merge the edits onto the raw row, RE-RUN the
    real deterministic validation (validate_rows — never the client mock), and, if it now passes and its
    column isn't already in the catalog, add it to the source graph + reconcile its table's grain /
    point-in-time facts and drop it from the queue. Returns (resolved, reason).

    LIMITS (holds until the source is re-uploaded — the file stays the source of truth): a resolved
    column is added incrementally, so it is NOT recorded in the drift snapshot and a subsequent
    re-upload of the still-broken file rebuilds the graph WITHOUT it (the resolution is superseded).
    Fix the source file for durability."""
    row = conn.execute(
        "SELECT raw FROM quarantine_row WHERE catalog_source = %s AND row_index = %s",
        (catalog_source, row_index)).fetchone()
    if row is None:
        return False, "no such quarantined row"
    merged = {**row[0], **(edits or {})}
    vr = validate_rows([_row_from_raw(merged, catalog_source)], catalog_source)
    if vr.structural_error or vr.quarantined:
        return False, vr.structural_error or vr.quarantined[0].message   # still invalid — surface why
    good = vr.good[0]
    c_ref = f"public.{good.table}.{good.column}"
    if conn.execute("SELECT 1 FROM graph_node WHERE catalog_source = %s AND object_ref = %s",
                    (catalog_source, c_ref)).fetchone() is not None:
        return False, f"{good.table}.{good.column} is already in the catalog"
    add_column_row(conn, catalog_source, good)
    if good.is_grain:
        # reconcile the table's grain fact with its FULL grain-column set (now incl. the added column),
        # or the uniqueness key stays silently wrong (a grain column added to the graph but not the fact).
        grain_cols = [r[0] for r in conn.execute(
            "SELECT column_name FROM graph_node WHERE catalog_source = %s AND table_name = %s "
            "AND kind = 'column' AND is_grain = true ORDER BY column_name",
            (catalog_source, good.table)).fetchall()]
        _assert_fact(conn, catalog_source, good.table, "grain",
                     {"columns": grain_cols, "is_unique": True}, actor=actor)
    if good.as_of:
        basis = good.as_of_basis if good.as_of_basis in ("posted_at", "ingested_at") else "posted_at"
        _assert_fact(conn, catalog_source, good.table, "availability_time",
                     {"column": good.column, "basis": basis}, actor=actor)
    conn.execute("DELETE FROM quarantine_row WHERE catalog_source = %s AND row_index = %s",
                 (catalog_source, row_index))
    return True, ""


def dismiss_quarantine_row(conn, catalog_source: str, row_index: int) -> bool:
    """Durably drop a quarantined row from the queue (holds until the source is re-uploaded)."""
    row = conn.execute(
        "DELETE FROM quarantine_row WHERE catalog_source = %s AND row_index = %s RETURNING row_index",
        (catalog_source, row_index)).fetchone()
    return row is not None
