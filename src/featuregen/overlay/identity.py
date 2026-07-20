from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CatalogObjectRef:
    catalog_source: str
    object_kind: str
    schema: str
    table: str
    column: str | None = None


@dataclass(frozen=True, slots=True)
class ColumnPair:
    from_col: str
    to_col: str


@dataclass(frozen=True, slots=True)
class ApprovedJoinRef:
    from_ref: CatalogObjectRef
    to_ref: CatalogObjectRef
    column_pairs: tuple[ColumnPair, ...]
    cardinality: str


@dataclass(frozen=True, slots=True)
class EntityBridgeRef:
    """A cross-catalog entity bridge: the SAME entity_id via an identifier column in two DISTINCT
    catalogs. Bridge identity is UNORDERED — (left, right) and (right, left) denote the same bridge, so
    fact_key canonicalizes the endpoints."""
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef


def _ref_from_payload(d):
    """Rebuild the typed ref stored on OVERLAY_FACT_PROPOSED.payload['catalog_object_ref']
    (an asdict() of CatalogObjectRef, or of ApprovedJoinRef for approved_join). Shared decoder
    used by both freshness pollers (fire_due_overlay_expiries / detect_catalog_changes)."""
    if "entity_id" in d and "left_ref" in d and "right_ref" in d:
        return EntityBridgeRef(entity_id=d["entity_id"],
                               left_ref=CatalogObjectRef(**d["left_ref"]),
                               right_ref=CatalogObjectRef(**d["right_ref"]))
    if "column_pairs" in d:
        return ApprovedJoinRef(
            from_ref=CatalogObjectRef(**d["from_ref"]),
            to_ref=CatalogObjectRef(**d["to_ref"]),
            column_pairs=tuple(ColumnPair(**p) for p in d["column_pairs"]),
            cardinality=d["cardinality"],
        )
    return CatalogObjectRef(**d)


def _norm(value: str | None) -> str | None:
    return value.strip().lower() if value is not None else None


def _ref_tuple(ref: CatalogObjectRef) -> list[str | None]:
    return [
        _norm(ref.catalog_source),
        _norm(ref.object_kind),
        _norm(ref.schema),
        _norm(ref.table),
        _norm(ref.column),
    ]


def _digest(canonical: object) -> str:
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fact_key(
    ref: CatalogObjectRef | ApprovedJoinRef | EntityBridgeRef,
    fact_type: str,
    use_case: str | None = None,
) -> str:
    """Stable sha256 hex over the normalized identity tuple (§3.1). For an ApprovedJoinRef the
    column pairs are sorted AS UNITS (never the two column lists independently) so distinct joins
    can never alias."""
    if isinstance(ref, EntityBridgeRef):
        endpoints = sorted([_ref_tuple(ref.left_ref), _ref_tuple(ref.right_ref)])
        bridge_canonical = {"kind": "bridge", "entity_id": _norm(ref.entity_id),
                            "endpoints": endpoints, "fact_type": _norm(fact_type),
                            "use_case": _norm(use_case)}
        return _digest(bridge_canonical)
    if isinstance(ref, ApprovedJoinRef):
        pairs = sorted([_norm(p.from_col), _norm(p.to_col)] for p in ref.column_pairs)
        canonical = {
            "kind": "relation",
            "from": _ref_tuple(ref.from_ref),
            "to": _ref_tuple(ref.to_ref),
            "cardinality": _norm(ref.cardinality),
            "column_pairs": pairs,
            "fact_type": _norm(fact_type),
            "use_case": _norm(use_case),
        }
    else:
        canonical = {
            "kind": "object",
            "ref": _ref_tuple(ref),
            "fact_type": _norm(fact_type),
            "use_case": _norm(use_case),
        }
    return _digest(canonical)


def display_object_ref(ref: CatalogObjectRef | ApprovedJoinRef | EntityBridgeRef) -> str:
    """Human-readable dotted reference carried alongside the hashed key for display/audit (§3.1)."""
    if isinstance(ref, EntityBridgeRef):
        # unordered bridge — '<->' (a join's '->' is directional)
        return (f"{ref.entity_id}: {display_object_ref(ref.left_ref)}"
                f" <-> {display_object_ref(ref.right_ref)}")
    if isinstance(ref, ApprovedJoinRef):
        return f"{display_object_ref(ref.from_ref)} -> {display_object_ref(ref.to_ref)}"
    parts = [ref.schema, ref.table]
    if ref.column:
        parts.append(ref.column)
    return ".".join(parts)


def _bridge_write_error(ref, value) -> str | None:
    if not isinstance(ref, EntityBridgeRef):
        return "entity_bridge requires an EntityBridgeRef"
    if _norm(ref.left_ref.catalog_source) == _norm(ref.right_ref.catalog_source):
        return ("entity_bridge requires two distinct catalog sources "
                f"(left={ref.left_ref.catalog_source}, right={ref.right_ref.catalog_source})")
    value_ref = _ref_from_payload(value)
    if not isinstance(value_ref, EntityBridgeRef):
        return "entity_bridge proposed_value is not a bridge ref"
    if fact_key(value_ref, "entity_bridge") != fact_key(ref, "entity_bridge"):
        return "entity_bridge proposed_value does not match ref"
    return None


def _entity_assignment_write_error(ref, value: Mapping) -> str | None:
    """Write gate for entity_assignment (Delivery E): the subject must be an identifier-eligible
    COLUMN ref and `entity_id` must be a member of the closed `known_entities()` vocabulary. The
    JSON schema already forbids a target ref / any extra key (additionalProperties False), so the
    two concerns left here are the column-ness of the subject and the entity being known."""
    if not isinstance(ref, CatalogObjectRef):
        return "entity_assignment requires a CatalogObjectRef"
    if not ref.column:
        return "entity_assignment subject must be a column (ref carries no column)"
    # Lazy import: overlay.identity -> overlay.upload.taxonomy at module load would cycle (mirrors
    # confirmation_commands' lazy join_referents import across the overlay/upload boundary).
    from featuregen.overlay.upload.taxonomy.dimensions import known_entities

    entity_id = value.get("entity_id")
    if entity_id not in known_entities():
        return f"entity_assignment entity_id {entity_id!r} is not a known entity"
    return None


def _currency_binding_write_error(ref, value: Mapping) -> str | None:
    """Write gate for currency_binding (Delivery E): the subject measure must be a COLUMN ref and the
    target currency column must be a well-formed CatalogObjectRef referencing a concrete column in the
    SAME source/schema/table as the measure — no cross-source / cross-schema / cross-table binding
    through this path. The value must match the fact subject (same table). The JSON schema already
    forbids any free value beyond `currency_column`."""
    if not isinstance(ref, CatalogObjectRef):
        return "currency_binding requires a CatalogObjectRef"
    if not ref.column:
        return "currency_binding subject (measure) must be a column (ref carries no column)"
    cc = value.get("currency_column")
    if not isinstance(cc, Mapping):
        return "currency_binding value.currency_column must be a CatalogObjectRef"
    if not cc.get("column"):
        return "currency_binding currency_column must reference a concrete column"
    if (
        _norm(cc.get("catalog_source")) != _norm(ref.catalog_source)
        or _norm(cc.get("schema")) != _norm(ref.schema)
        or _norm(cc.get("table")) != _norm(ref.table)
    ):
        return (
            "currency_binding target currency column must be in the same source/schema/table as the "
            f"measure ({ref.schema}.{ref.table})"
        )
    return None


def join_write_error(ref, fact_type: str, value: Mapping, use_case: str | None = None) -> str | None:
    """Write-path integrity gate for governed column-referent facts (SP-1.5 review fix; extended for
    Delivery E). Returns a rejection reason, or None when the write is well-formed:
      * approved_join — F4 (cross-catalog joins DISALLOWED in SP-1.5) + ref/value consistency
        (authority + fact_key derive from `ref` while the stored value is what consumers read; reject
        a proposed_value describing a DIFFERENT join than `ref`).
      * entity_bridge — cross-catalog required + ref/value consistency.
      * entity_assignment / currency_binding (Delivery E) — subject column-ness, `entity_id` ∈
        `known_entities()`, and same-source/schema/table currency target (no cross-schema binding).
    Called by every write entry point (propose_fact / confirm_fact / enter_fact)."""
    if fact_type == "entity_bridge":
        return _bridge_write_error(ref, value)
    if fact_type == "entity_assignment":
        return _entity_assignment_write_error(ref, value)
    if fact_type == "currency_binding":
        return _currency_binding_write_error(ref, value)
    if fact_type != "approved_join":
        return None
    if not isinstance(ref, ApprovedJoinRef):
        return "approved_join requires an ApprovedJoinRef"
    # Compare NORMALIZED sources (review #8): fact_key/_ref_tuple treat catalog_source as
    # case/whitespace-insensitive, so a raw != here would falsely reject a same-source join whose
    # two endpoints differ only in casing.
    if _norm(ref.from_ref.catalog_source) != _norm(ref.to_ref.catalog_source):
        return (
            "cross-catalog approved_join disallowed in SP-1.5 "
            f"(from={ref.from_ref.catalog_source}, to={ref.to_ref.catalog_source})"
        )
    try:
        value_ref = ApprovedJoinRef(
            from_ref=CatalogObjectRef(**value["from_ref"]),
            to_ref=CatalogObjectRef(**value["to_ref"]),
            column_pairs=tuple(ColumnPair(**p) for p in value["column_pairs"]),
            cardinality=value["cardinality"],
        )
    except (KeyError, TypeError):
        return "approved_join proposed_value is not a well-formed join"
    if fact_key(value_ref, "approved_join") != fact_key(ref, "approved_join"):
        return "approved_join proposed_value does not match ref (from/to/column_pairs/cardinality)"
    return None


def proposal_fingerprint(
    proposed_value: Mapping,
    *,
    profile_version: str | None = None,
    thresholds: Mapping | None = None,
) -> str:
    """Stable hash over (canonical proposed_value + profiler version + thresholds) — NOT the
    evidence id/timestamp (§3.4/§5). Drives REJECTED-stickiness dedup; only a materially different
    value yields a new fingerprint."""
    canonical = {
        "value": dict(proposed_value),
        "profile_version": profile_version,
        "thresholds": dict(thresholds) if thresholds is not None else None,
    }
    return _digest(canonical)
