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
    ref: CatalogObjectRef | ApprovedJoinRef, fact_type: str, use_case: str | None = None
) -> str:
    """Stable sha256 hex over the normalized identity tuple (§3.1). For an ApprovedJoinRef the
    column pairs are sorted AS UNITS (never the two column lists independently) so distinct joins
    can never alias."""
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


def display_object_ref(ref: CatalogObjectRef | ApprovedJoinRef) -> str:
    """Human-readable dotted reference carried alongside the hashed key for display/audit (§3.1)."""
    if isinstance(ref, ApprovedJoinRef):
        return f"{display_object_ref(ref.from_ref)} -> {display_object_ref(ref.to_ref)}"
    parts = [ref.schema, ref.table]
    if ref.column:
        parts.append(ref.column)
    return ".".join(parts)


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
