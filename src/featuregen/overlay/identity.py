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
    catalogs. Bridge identity is UNORDERED — (left, right) and (right, left) denote the same bridge.

    The endpoints are canonicalized ON CONSTRUCTION, so the ref itself carries one orientation and
    every identity derived FROM it agrees: ``fact_key``, ``display_object_ref``, the ``asdict(ref)``
    stored on ``OVERLAY_FACT_PROPOSED.payload``, and the value a caller builds out of ``left_ref`` /
    ``right_ref``. It used to be canonicalized only inside ``fact_key``, which let the same bridge
    hold ONE key and TWO of everything else — most damagingly two ``proposal_fingerprint``s, so a
    human's REJECT (sticky by fingerprint) did not stop the swapped orientation from being proposed
    onto the same fact and folding it back to an AVAILABLE state. A rejection is the one governance
    decision that must be durable, and canonicalizing at the type is what makes it so.
    """
    entity_id: str
    left_ref: CatalogObjectRef
    right_ref: CatalogObjectRef

    def __post_init__(self) -> None:
        left, right = canonical_bridge_endpoints(self.left_ref, self.right_ref)
        if left is not self.left_ref:
            # frozen dataclass: the same escape hatch dataclasses' own __init__ uses
            object.__setattr__(self, "left_ref", left)
            object.__setattr__(self, "right_ref", right)


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


#: The fields of a catalog object ref, in the order they are compared. Same order as `_ref_tuple`,
#: so the endpoint ordering below and the `fact_key` digest read the endpoints identically.
_ENDPOINT_FIELDS = ("catalog_source", "object_kind", "schema", "table", "column")


def _endpoint_order_key(parts) -> tuple[str, ...]:
    """The ONE total order over bridge endpoints.

    A missing part sorts as ``""`` rather than ``None``: a bare ``sorted`` raises comparing ``None``
    against a string, which would make ordering an endpoint that names no column a crash rather than
    a decision. In practice the FIRST part decides every legal bridge — the write gate requires two
    DISTINCT catalog sources — but the rule is total anyway so nothing downstream has to rely on
    that."""
    return tuple("" if p is None else _norm(str(p)) or "" for p in parts)


def canonical_bridge_endpoints(
    left: CatalogObjectRef, right: CatalogObjectRef
) -> tuple[CatalogObjectRef, CatalogObjectRef]:
    """The two endpoints of a bridge in canonical order — the single ordering rule every derivation
    of bridge identity uses (``EntityBridgeRef``, ``fact_key``, ``proposal_fingerprint``, the
    candidate ledger, the verified-edge projection). Returns the arguments UNCHANGED (identity, not
    a copy) when they are already canonical, so a caller can cheaply test whether a swap happened."""
    if (_endpoint_order_key(_ref_tuple(left)) <= _endpoint_order_key(_ref_tuple(right))):
        return left, right
    return right, left


def canonical_bridge_value(value: Mapping) -> Mapping:
    """A bridge's ``proposed_value`` with its endpoints in canonical order; anything else unchanged.

    Shape-detected on the exact ``entity_id`` + ``left_ref`` + ``right_ref`` triple — the same
    sniff ``_ref_from_payload`` uses, and the only fact value with that shape (an ``approved_join``
    is DIRECTIONAL and names ``from_ref`` / ``to_ref``, so it is never reordered). A value already
    in canonical order is returned untouched, so every fingerprint recorded before this existed
    still matches: only the orientation that was producing a SECOND identity is re-keyed."""
    if not isinstance(value, Mapping) or "entity_id" not in value:
        return value
    left, right = value.get("left_ref"), value.get("right_ref")
    if not (isinstance(left, Mapping) and isinstance(right, Mapping)):
        return value
    left_key = _endpoint_order_key(left.get(f) for f in _ENDPOINT_FIELDS)
    right_key = _endpoint_order_key(right.get(f) for f in _ENDPOINT_FIELDS)
    if left_key <= right_key:
        return value
    return dict(value) | {"left_ref": right, "right_ref": left}


def fact_key(
    ref: CatalogObjectRef | ApprovedJoinRef | EntityBridgeRef,
    fact_type: str,
    use_case: str | None = None,
) -> str:
    """Stable sha256 hex over the normalized identity tuple (§3.1). For an ApprovedJoinRef the
    column pairs are sorted AS UNITS (never the two column lists independently) so distinct joins
    can never alias."""
    if isinstance(ref, EntityBridgeRef):
        left, right = canonical_bridge_endpoints(ref.left_ref, ref.right_ref)
        endpoints = [_ref_tuple(left), _ref_tuple(right)]
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
    value yields a new fingerprint.

    "Canonical" now means canonical for the FACT TYPE, not merely for JSON: a bridge's endpoints are
    an unordered pair, so they are ordered here by the same rule ``fact_key`` orders them by. Naming
    a rejected bridge the other way round is not a materially different value, and before this it
    produced a second fingerprint under the same key — which is precisely what the sticky-reject
    guard compares, so a human's NO could be undone by the next upload."""
    canonical = {
        "value": dict(canonical_bridge_value(proposed_value)),
        "profile_version": profile_version,
        "thresholds": dict(thresholds) if thresholds is not None else None,
    }
    return _digest(canonical)
