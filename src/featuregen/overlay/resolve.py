"""Merged-view read API: the single resolver SP-2+ consumers call (design §7.1).

Precedence: authoritative catalog fact -> VERIFIED overlay fact -> missing.
Fail-closed: a usable value is returned ONLY when status == VERIFIED; every other
status blocks (value=None) with a reason_if_missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC

from psycopg.rows import dict_row

from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    display_object_ref,
    fact_key,
)

_REASON_MISSING = "no_confirmed_fact"
_REASON_CATALOG_INVALID = "catalog_value_invalid"
_REASON_BY_STATUS = {
    "DRAFT": "draft_unconfirmed",
    "PARTIALLY_CONFIRMED": "partial_confirmation_pending",
    "REJECTED": "rejected",
    "REVERIFY": "reverification_required",
    "STALE": "stale_catalog_change",
}


@dataclass(frozen=True, slots=True)
class ResolvedFact:
    value: object | None
    status: str
    source: str  # 'catalog' | 'overlay' | 'missing'
    catalog_object: str
    fact_type: str
    use_case: str | None
    provenance: Mapping | None
    confirmed_by: tuple[str, ...]
    confirmed_at: str | None
    expires_at: str | None
    reason_if_missing: str | None
    prior_value: object | None


def _iso(value):
    # timestamptz columns yield tz-aware datetimes in the session timezone; canonicalize to
    # UTC so the read API emits a deterministic ISO string regardless of the DB session tz.
    return value.astimezone(UTC).isoformat() if value is not None else None


def resolve_fact(
    conn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef | ApprovedJoinRef,
    fact_type: str,
    use_case: str | None = None,
) -> ResolvedFact:
    key = fact_key(ref, fact_type, use_case)
    obj = display_object_ref(ref)

    # 1) Authoritative catalog fact wins (catalog beats overlay only where authoritative=True).
    # approved_join is overlay-only — a catalog is never authoritative for a relation, and the
    # CatalogAdapter.get_fact protocol takes a CatalogObjectRef, so skip catalog precedence (finding 7).
    catalog_fact = None if fact_type == "approved_join" else adapter.get_fact(ref, fact_type, use_case)
    if catalog_fact is not None and catalog_fact.authoritative:
        # Trust boundary: a pluggable CatalogAdapter is a public extension point, so its
        # authoritative value must clear the SAME per-type schema every overlay write enforces
        # (facts.validate_fact_value) before we stamp it VERIFIED. On failure FAIL CLOSED and do
        # NOT fall through to the overlay: catalog precedence means the catalog owns this fact, and
        # serving a stale overlay value would mask the catalog corruption. (TypeError/ValueError
        # cover a non-Mapping value, since validate_fact_value does dict(value).)
        try:
            validate_fact_value(fact_type, catalog_fact.value, use_case)
        except (FactValidationError, TypeError, ValueError):
            return ResolvedFact(
                value=None,
                status="missing",
                source="catalog",
                catalog_object=obj,
                fact_type=fact_type,
                use_case=use_case,
                provenance={"catalog_source": getattr(ref, "catalog_source", None)},
                confirmed_by=(),
                confirmed_at=None,
                expires_at=None,
                reason_if_missing=_REASON_CATALOG_INVALID,
                prior_value=None,
            )
        return ResolvedFact(
            value=catalog_fact.value,
            status="VERIFIED",
            source="catalog",
            catalog_object=obj,
            fact_type=fact_type,
            use_case=use_case,
            provenance={"catalog_source": getattr(ref, "catalog_source", None)},
            confirmed_by=(),
            confirmed_at=None,
            expires_at=None,
            reason_if_missing=None,
            prior_value=None,
        )

    # 2) Overlay merged-view read model (hot table maintained by OverlayProjection).
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT status, value, confirmers, confirmed_at, expires_at,
                   prior_value, confirmed_event_id
            FROM overlay_fact_state
            WHERE fact_key = %s
            """,
            (key,),
        )
        row = cur.fetchone()

    # 3) Nothing confirmed -> missing (fail-closed; routes to first-time confirmation).
    if row is None:
        return ResolvedFact(
            value=None,
            status="missing",
            source="missing",
            catalog_object=obj,
            fact_type=fact_type,
            use_case=use_case,
            provenance=None,
            confirmed_by=(),
            confirmed_at=None,
            expires_at=None,
            reason_if_missing=_REASON_MISSING,
            prior_value=None,
        )

    # VERIFIED overlay entry -> usable value (the only servable overlay status).
    status = row["status"]
    if status == "VERIFIED":
        confirmers = row["confirmers"] or []
        return ResolvedFact(
            value=row["value"],
            status="VERIFIED",
            source="overlay",
            catalog_object=obj,
            fact_type=fact_type,
            use_case=use_case,
            provenance={"confirmed_event_id": row["confirmed_event_id"]},
            confirmed_by=tuple(c["subject"] for c in confirmers),
            confirmed_at=_iso(row["confirmed_at"]),
            expires_at=_iso(row["expires_at"]),
            reason_if_missing=None,
            prior_value=None,
        )

    # Non-VERIFIED overlay entry -> blocked (fail-closed). REVERIFY/STALE surface the
    # last VERIFIED value as read-only context (design §7.1); all others carry no value.
    prior_value = row["prior_value"] if status in ("REVERIFY", "STALE") else None
    return ResolvedFact(
        value=None,
        status=status,
        source="overlay",
        catalog_object=obj,
        fact_type=fact_type,
        use_case=use_case,
        provenance=None,
        confirmed_by=(),
        confirmed_at=None,
        expires_at=None,
        reason_if_missing=_REASON_BY_STATUS.get(status, _REASON_MISSING),
        prior_value=prior_value,
    )
