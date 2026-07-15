from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.upload_catalog import UploadCatalog


@dataclass(frozen=True, slots=True)
class BrakeResult:
    held: bool
    reason: str | None
    is_first_upload: bool


def _prior_refs(conn, catalog_source: str) -> set[str]:
    rows = conn.execute(
        "SELECT object_ref FROM overlay_catalog_object WHERE catalog_source = %s",
        (catalog_source,)).fetchall()
    return {r[0] for r in rows}


def large_change_brake(conn, catalog_source: str, upload: UploadCatalog, *,
                       max_removed_frac: float = 0.30, min_removed_abs: int = 5,
                       min_overlap_frac: float = 0.60) -> BrakeResult:
    prior = _prior_refs(conn, catalog_source)
    if not prior:
        return BrakeResult(held=False, reason=None, is_first_upload=True)

    current = set(upload.fingerprint())
    removed = prior - current
    overlap = len(prior & current) / len(prior)

    if len(removed) >= min_removed_abs and len(removed) / len(prior) > max_removed_frac:
        return BrakeResult(True, f"removes {len(removed)}/{len(prior)} objects "
                           f"(> {max_removed_frac:.0%})", False)
    if overlap < min_overlap_frac:
        return BrakeResult(True, f"overlap {overlap:.0%} < {min_overlap_frac:.0%} "
                           "(possible wrong source)", False)
    return BrakeResult(False, None, False)


def resolution_brake(conn, catalog_source: str, new_refs: set[str], *,
                     max_added_frac: float = 0.30, min_added_abs: int = 5) -> BrakeResult:
    """The large-change brake for the QUARANTINE-RESOLUTION path (#4). Resolution only ever ADDS
    objects (an already-present column is refused upstream), so the upload brake's removed/overlap
    conditions can never fire on it — instead the CUMULATIVE additions since the last successful
    upload (graph objects absent from the drift snapshot, plus what this resolution would add) are
    held against the snapshot with the upload brake's own thresholds. A reviewer resolving an
    all-quarantined wrong-source upload row-by-row trips this once the contamination is large
    enough; a single-row fix (a couple of objects) passes. No prior snapshot (the source never
    ingested successfully) mirrors the upload path's is_first_upload pass-through. Fail-closed
    corner: a snapshot left behind by a drift-skip (projection lag) counts its uploads' extra
    objects as additions and may hold a resolution until drift catches up — hold, never launder."""
    prior = _prior_refs(conn, catalog_source)
    if not prior:
        return BrakeResult(held=False, reason=None, is_first_upload=True)
    graph = {r[0] for r in conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s",
        (catalog_source,)).fetchall()}
    added = (graph | set(new_refs)) - prior
    if len(added) >= min_added_abs and len(added) / len(prior) > max_added_frac:
        return BrakeResult(True, f"cumulative quarantine resolutions would add {len(added)} objects "
                           f"to a catalog of {len(prior)} (> {max_added_frac:.0%} — possible "
                           "wrong-source contamination); fix and re-upload the source instead", False)
    return BrakeResult(False, None, False)
