"""Phase 3C.2b-i-A · Task 4 — governed endpoint revalidation (spec §2/§3.1).

A path endpoint (source / intermediate / landing) is GOVERNED only when a VERIFIED ``grain`` fact
proves its grain. The cross-catalog frontier derives grain/keys from advisory ``graph_node.concept``
/``is_grain`` (spec §2, confirmed by the Task-1 reuse spike) — this authority layer supersedes that
by re-reading the grain from the governed fact stream.

``governed_endpoint`` resolves the table's grain fact via the merged-view read API
(:func:`resolve_fact`, so the catalog adapter is consulted first, then the VERIFIED overlay fact),
qualifies its short grain columns to the table ref, validates each is a real column of that table in
``graph_node`` (membership against ``column_name``), and returns a ``GovernedEndpointV1`` keyed on
the DETERMINISTIC ``grain_fact_key`` (ref+type; never a per-event id — finding #8). No VERIFIED
grain fact (advisory ``is_grain`` alone, or nothing) -> ``None`` (endpoint ungoverned). FAIL CLOSED.
"""
from __future__ import annotations

from datetime import datetime

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.planner.multisource_contracts import GovernedEndpointV1

_SCHEMA_SEP = "."


def _table_object_ref(catalog: str, table_ref: str) -> CatalogObjectRef:
    """Rebuild the table ``CatalogObjectRef`` from ``catalog`` + the dotted ``schema.table`` object
    ref. Built identically to ``upload_catalog.table_ref`` so ``resolve_fact``'s ``fact_key`` matches
    the one the governance write path keyed the grain fact on (``fact_key`` is deterministic over the
    normalized identity tuple)."""
    schema, _, table = table_ref.rpartition(_SCHEMA_SEP)
    return CatalogObjectRef(catalog_source=catalog, object_kind="table",
                            schema=schema, table=table, column=None)


def governed_endpoint(conn, adapter: CatalogAdapter, *, catalog: str, table_ref: str,
                      now: datetime) -> GovernedEndpointV1 | None:
    """Revalidate a table endpoint's grain against a VERIFIED ``grain`` fact (spec §3.1).

    Returns a ``GovernedEndpointV1`` iff a VERIFIED grain fact exists AND every one of its short
    grain columns is a real column of ``table_ref`` in ``graph_node``; otherwise ``None`` — advisory
    ``is_grain`` alone, or no fact, does NOT govern the endpoint. ``grain_key_refs`` are the fact's
    short columns qualified to ``table_ref``; ``grain_fact_key`` is the deterministic fact key
    (ref+type), NOT a per-event id.
    """
    ref = _table_object_ref(catalog, table_ref)
    grain = resolve_fact(conn, adapter, ref, "grain", now=now)
    # Fail closed: resolve_fact serves a value ONLY on VERIFIED — every blocked / missing /
    # advisory-only state yields value=None, so a non-None value IS the VERIFIED grain fact.
    if grain.value is None:
        return None
    columns = grain.value.get("columns") or []
    if not columns:
        return None
    # Membership: each short grain column must be a real column of THIS table in graph_node
    # (validated against column_name, scoped to catalog + table). A grain column the physical graph
    # lacks means the endpoint is untrustworthy -> fail closed.
    schema, _, table = table_ref.rpartition(_SCHEMA_SEP)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM graph_node "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
            (catalog, table))
        real_columns = {row[0] for row in cur.fetchall()}
    qualified: list[str] = []
    for col in columns:
        if col not in real_columns:
            return None
        qualified.append(f"{table_ref}{_SCHEMA_SEP}{col}")
    return GovernedEndpointV1(
        catalog=catalog, table_ref=table_ref, grain_key_refs=tuple(qualified),
        grain_fact_key=fact_key(ref, "grain"))
