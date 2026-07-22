"""The general dependency index (§8): which catalog objects a fact's value REFERENCES — an
entity_bridge, like an approved_join, references TWO sources (both endpoints are indexed).

This is a DOMAIN concept shared by two callers that must agree exactly: the OverlayProjection (which
persists the reverse index into overlay_fact_dependency) and command-time referent validation
(_lifecycle.referent_gap). It lives here — not as a private helper on the projection — so validation
does not import a read-model internal (SP-1.5 review #8 decoupling).
"""
from __future__ import annotations

from collections.abc import Mapping

from featuregen.overlay import facts


def table_obj(ref: Mapping) -> str:
    """Dotted `schema.table` for a structured CatalogObjectRef dict (no column)."""
    return ".".join(p for p in [ref["schema"], ref["table"]] if p)


def fact_dependencies(
    object_ref: str, fact_type: str, value: Mapping, catalog_source: str
) -> set[tuple[str, str]]:
    """(catalog_source, ref_object) pairs a fact's value references, each referent qualified by ITS
    OWN source. For the four object-keyed facts every referent (the keyed object plus grain.columns /
    availability_time.column / scd valid_from+valid_to) lives in the fact's single `catalog_source`.
    For an approved_join the keyed `object_ref` is the synthetic "from -> to" display string which
    must NEVER be parsed; instead read the STRUCTURED value — `value['from_ref']`/`value['to_ref']`
    (each carrying its OWN catalog_source) and each `value['column_pairs']` pair — and index BOTH
    tables and ALL paired columns on both sides UNDER THEIR RESPECTIVE SOURCES (a cross-catalog join's
    to-side must be tracked under the to-catalog, or its drift-staling and the read-time freshness
    guard both fail open). A drop/rename/type-change to ANY referent stales the dependent fact."""
    if fact_type == facts.ENTITY_BRIDGE:
        lr, rr = value["left_ref"], value["right_ref"]
        l_src, r_src = lr["catalog_source"], rr["catalog_source"]
        l_obj, r_obj = table_obj(lr), table_obj(rr)
        # both endpoints, each under its OWN catalog_source: the table AND the identifier column, so a
        # drop/rename/retype of either endpoint stales the bridge. A bridge is unordered — indexing both
        # sides symmetrically is correct.
        return {
            (l_src, l_obj), (r_src, r_obj),
            (l_src, f"{l_obj}.{lr['column']}"), (r_src, f"{r_obj}.{rr['column']}"),
        }
    if fact_type == facts.APPROVED_JOIN:
        fr, tr = value["from_ref"], value["to_ref"]
        from_src, to_src = fr["catalog_source"], tr["catalog_source"]
        from_obj, to_obj = table_obj(fr), table_obj(tr)
        deps: set[tuple[str, str]] = {(from_src, from_obj), (to_src, to_obj)}
        for pair in value.get("column_pairs", []):
            deps.add((from_src, f"{from_obj}.{pair['from_col']}"))
            deps.add((to_src, f"{to_obj}.{pair['to_col']}"))
        return deps
    if fact_type == facts.CURRENCY_BINDING:
        # The subject measure column (the keyed object_ref) PLUS the target currency column — both
        # under the fact's single catalog_source (the write gate forces same source/schema/table). A
        # drop/rename/retype of EITHER stales the binding (mirrors approved_join indexing endpoint
        # columns). The currency target lives in `value['currency_column']` (a CatalogObjectRef).
        cc = value["currency_column"]
        return {
            (catalog_source, object_ref),
            (catalog_source, f"{table_obj(cc)}.{cc['column']}"),
        }
    # entity_assignment falls through to the single-object default below: its only catalog referent is
    # the subject column itself (`entity_id` names a governed-vocabulary entity, not a catalog object).
    deps = {(catalog_source, object_ref)}
    if fact_type == facts.GRAIN:
        deps |= {(catalog_source, f"{object_ref}.{c}") for c in value.get("columns", [])}
    elif fact_type == facts.AVAILABILITY_TIME:
        deps.add((catalog_source, f"{object_ref}.{value['column']}"))
    elif fact_type == facts.SCD_EFFECTIVE_DATING:
        deps.add((catalog_source, f"{object_ref}.{value['valid_from']}"))
        if value.get("valid_to"):
            deps.add((catalog_source, f"{object_ref}.{value['valid_to']}"))
    return deps
