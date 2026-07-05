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
