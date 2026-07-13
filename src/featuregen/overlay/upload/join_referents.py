"""Authority-routed referent validation for the confirm path (SP-1.5 sealed-runtime fix, Task 0).

A sealed OverlayConfig arms the SP-1.5 referent gate at the dual-join second confirm and the
single-path STALE/REVERIFY re-confirm. `_lifecycle.referent_gap` resolves referent EXISTENCE
against `adapter.fingerprint()` — correct for a real DB-backed adapter, but the upload flow's only
registered adapter is the connectionless sentinel `UploadContextAdapter`
(catalog_source='upload:context', fingerprint()=={}), which serves NO real source: every real
join's referents fail-closed and no join could reach VERIFIED in a sealed deployment.

`check_referents_exist` routes the check to the authoritative STRUCTURAL source for the current
catalog mode: `graph_node` (the built graph — upload mode's structural truth) under the sentinel,
`referent_gap` (byte-for-byte the existing behavior) for any other adapter. The guard is KEPT —
only where existence is answered from changes. Both use the SAME referent enumeration
(`dependencies.fact_dependencies`), so join FROM+TO endpoints — tables AND all paired columns —
are covered identically with no drift between the two paths.
"""
from __future__ import annotations

from featuregen.overlay._lifecycle import referent_gap
from featuregen.overlay.dependencies import fact_dependencies
from featuregen.overlay.identity import display_object_ref
from featuregen.overlay.upload.upload_catalog import UploadContextAdapter


def _norm_source(source: str) -> str:
    """Normalize a catalog_source the way the upload flow does everywhere (object_ref._norm /
    readiness): strip + lower-case."""
    return source.strip().lower()


def _norm_path(referent: str) -> str:
    """Normalize a dotted display referent ('schema.table[.column]') per-segment (strip + lower),
    matching `object_ref.normalize_ref`'s component rules — unquoted SQL identifiers fold to lower
    case, so 'public.Transactions.CIF_ID' and 'public.transactions.cif_id' are ONE object."""
    return ".".join(seg.strip().lower() for seg in referent.split("."))


def graph_referent_gap(conn, ref, fact_type: str, value) -> str | None:
    """`referent_gap`, resolved against `graph_node` instead of `adapter.fingerprint()`: every
    object/column the fact's value refers to must exist in the built graph UNDER ITS OWN
    catalog_source. Returns a rejection reason for the first missing referent, or None when all are
    present. The referent enumeration is EXACTLY `referent_gap`'s (`fact_dependencies` over the
    display ref), and each referent is matched on the FULL public-flattened `graph_node.object_ref`
    path ('schema.table[.column]', as `build_graph` writes it) — never on a bare column name, so a
    same-named column under a different table or source can never false-pass."""
    for dep_source, referent in fact_dependencies(
        display_object_ref(ref), fact_type, value, getattr(ref, "catalog_source", None)
    ):
        row = conn.execute(
            "SELECT 1 FROM graph_node "
            "WHERE lower(btrim(catalog_source)) = %s AND lower(btrim(object_ref)) = %s LIMIT 1",
            (_norm_source(dep_source), _norm_path(referent)),
        ).fetchone()
        if row is None:
            return f"join referent missing from graph: {dep_source}.{referent}"
    return None


def check_referents_exist(conn, adapter, ref, fact_type: str, value) -> str | None:
    """Dispatch referent validation to the authoritative structural source for the current catalog
    mode: the built graph when the sentinel `UploadContextAdapter` is active (its empty fingerprint
    attests nothing — the graph is upload mode's structural truth), else the ORIGINAL
    `referent_gap` against the adapter's fingerprint (unchanged non-sentinel behavior)."""
    if isinstance(adapter, UploadContextAdapter):
        return graph_referent_gap(conn, ref, fact_type, value)
    return referent_gap(adapter, ref, fact_type, value)
