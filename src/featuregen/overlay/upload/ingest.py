from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.brake import large_change_brake
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref
from featuregen.projections.runner import run_projection


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str            # "ingested" | "held" | "rejected"
    reason: str | None
    asserted: int
    staled: int
    quarantined: int


def _table_facts(rows: list[CanonicalRow]):
    """Yield (table, fact_type, value) for grain + availability_time facts."""
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    for table, trows in by_table.items():
        grain_cols = [r.column for r in trows if r.is_grain]
        if grain_cols:
            yield table, "grain", {"columns": grain_cols, "is_unique": True}
        as_of = next((r.column for r in trows if r.as_of), None)
        if as_of:
            yield table, "availability_time", {"column": as_of, "basis": "posted_at"}


def _assert_fact(conn, source: str, table: str, fact_type: str, value: dict, *, actor) -> bool:
    fk = fact_key(table_ref(source, table), fact_type)
    if load_fact(conn, fk):        # already asserted (slice: unchanged) -> skip (diff-append)
        return False
    draft = append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=actor, expected_version=0, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": fact_type,
            "proposed_value": value, "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": actor.subject})
    append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=actor, expected_version=1, payload={
            "value": value, "confirmers": [{"subject": actor.subject, "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})
    return True


def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None) -> IngestResult:
    vr = validate_rows(rows)
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

    build_graph(conn, catalog_source, vr.good)
    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined))
