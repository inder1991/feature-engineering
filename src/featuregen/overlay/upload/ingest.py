from __future__ import annotations

from dataclasses import dataclass
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
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.review_queue import persist_quarantine
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref
from featuregen.projections.runner import run_projection


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


def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None, client=None) -> IngestResult:
    vr = validate_rows(rows, catalog_source)
    if vr.structural_error:
        return IngestResult("rejected", vr.structural_error, 0, 0, len(vr.quarantined))

    upload = UploadCatalog(catalog_source, vr.good)
    brake = large_change_brake(conn, catalog_source, upload)
    if brake.held:
        return IngestResult("held", brake.reason, 0, 0, len(vr.quarantined))

    asserted = 0
    for table, fact_type, value in _table_facts(vr.good):
        if _assert_fact(conn, catalog_source, table, fact_type, value, actor=actor):
            asserted += 1

    run_projection(conn, OverlayProjection())
    changes = detect_catalog_changes(conn, upload, actor=actor, now=now, open_reverify=False)
    run_projection(conn, OverlayProjection())
    staled = sum(1 for c in changes if c.kind in ("drop", "type_change", "rename"))

    concepts = definitions = domains = None
    if client is not None:
        concepts = enrich_concepts(conn, vr.good, client)
        definitions = draft_definitions(conn, vr.good, client)
        domains = classify_domains(conn, vr.good, client)
    build_graph(conn, catalog_source, vr.good, concepts, definitions, domains)
    persist_quarantine(conn, catalog_source, vr.quarantined)
    flagged = (f"first upload of '{catalog_source}' ({len(vr.good)} objects) — review recommended"
               if brake.is_first_upload else None)
    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined), flagged)
