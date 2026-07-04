"""Merged-view read API: the single resolver SP-2+ consumers call (design §7.1).

Precedence: authoritative catalog fact -> VERIFIED overlay fact -> missing.
Fail-closed: a usable value is returned ONLY when status == VERIFIED; every other
status blocks (value=None) with a reason_if_missing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from psycopg.rows import dict_row

from featuregen.overlay._types import FactStatus, FactType
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import (
    ApprovedJoinRef,
    CatalogObjectRef,
    display_object_ref,
    fact_key,
)
from featuregen.overlay.projection import read_proposal

_REASON_MISSING = "no_confirmed_fact"
_REASON_CATALOG_INVALID = "catalog_value_invalid"
_REASON_EXPIRED = "expired_pending_reverify"  # SP-1.5 Task 3: read-time time-expiry guard
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
    # A folded FactStatus for a real overlay/catalog fact, PLUS the read-only sentinel "missing"
    # (paired with source in {"catalog","missing"}) that resolve stamps when nothing is servable.
    # "missing" is deliberately NOT a member of FactStatus — it is a resolve-read outcome, not a
    # persisted fact status — so it is surfaced explicitly here rather than by widening FactStatus.
    status: FactStatus | Literal["missing"]
    source: str  # 'catalog' | 'overlay' | 'missing'
    catalog_object: str
    fact_type: FactType
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


# Result constructors: one per resolve_fact outcome. They centralize the ResolvedFact
# shape so each branch reads as the decision it makes, not a 12-field literal.


def _catalog_verified(catalog_fact, ref, obj, fact_type: FactType, use_case) -> ResolvedFact:
    # Authoritative catalog value that cleared per-type validation -> servable, VERIFIED.
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


def _catalog_invalid(ref, obj, fact_type: FactType, use_case) -> ResolvedFact:
    # Authoritative catalog value failed validation -> fail closed, do not fall through.
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


def _missing(reason, obj, fact_type: FactType, use_case) -> ResolvedFact:
    # Nothing catalog-authoritative and nothing in flight -> first-time confirmation.
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
        reason_if_missing=reason,
        prior_value=None,
    )


def _overlay_verified(row, obj, fact_type: FactType, use_case) -> ResolvedFact:
    # The only servable overlay status: a CONFIRMED, VERIFIED overlay fact.
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


def _overlay_blocked(
    status: FactStatus, reason, obj, fact_type: FactType, use_case, prior_value=None,
    *, expires_at=None, provenance=None,
) -> ResolvedFact:
    # Any non-VERIFIED overlay state (in-flight proposal or blocked fact state) -> no value. A
    # blocked result carries expires_at + provenance {confirmed_event_id, catalog_source} when known
    # (SP-1.5 Task 3) so SP-3 / operators can distinguish expired / drift-stale / reverify / missing.
    return ResolvedFact(
        value=None,
        status=status,
        source="overlay",
        catalog_object=obj,
        fact_type=fact_type,
        use_case=use_case,
        provenance=provenance,
        confirmed_by=(),
        confirmed_at=None,
        expires_at=expires_at,
        reason_if_missing=reason,
        prior_value=prior_value,
    )


def resolve_fact(
    conn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef | ApprovedJoinRef,
    fact_type: FactType,
    use_case: str | None = None,
    *,
    now: datetime | None = None,
) -> ResolvedFact:
    # `now` is an optional injected clock (SP-1.5 Task 3) — deterministic tests + one clock basis
    # shared with the pollers. Compare expiry in PYTHON (not SQL) to avoid a second clock source.
    now = now or datetime.now(UTC)
    key = fact_key(ref, fact_type, use_case)
    obj = display_object_ref(ref)

    # 1) Authoritative catalog fact wins (catalog beats overlay only where authoritative=True).
    # approved_join is overlay-only — a catalog is never authoritative for a relation, and the
    # CatalogAdapter.get_fact protocol takes a CatalogObjectRef, so skip catalog precedence.
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
            return _catalog_invalid(ref, obj, fact_type, use_case)
        return _catalog_verified(catalog_fact, ref, obj, fact_type, use_case)

    # 2) Overlay merged-view read model (hot table maintained by OverlayProjection).
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT status, value, confirmers, confirmed_at, expires_at,
                   prior_value, confirmed_event_id, catalog_source
            FROM overlay_fact_state
            WHERE fact_key = %s
            """,
            (key,),
        )
        row = cur.fetchone()

    # 3) No overlay_fact_state row -> the fact was never CONFIRMED. A fresh DRAFT /
    # PARTIALLY_CONFIRMED / REJECTED lives only in overlay_proposal (the projection writes
    # overlay_fact_state on CONFIRMED only), so consult it as a diagnostic fallback to report the
    # real workflow status instead of collapsing everything to "missing". This NEVER serves a
    # value (only VERIFIED is usable); fail-closed is preserved on every branch.
    if row is None:
        prop = read_proposal(conn, key)
        if prop is not None and prop["status"] in _REASON_BY_STATUS:
            return _overlay_blocked(
                prop["status"], _REASON_BY_STATUS[prop["status"]], obj, fact_type, use_case
            )
        # Nothing in flight -> missing (fail-closed; routes to first-time confirmation).
        return _missing(_REASON_MISSING, obj, fact_type, use_case)

    # VERIFIED overlay entry -> usable value (the only servable overlay status).
    status = row["status"]
    if status == "VERIFIED":
        # Read-time TIME-EXPIRY guard (SP-1.5 Task 3): between expires_at passing and the async
        # expiry poller STALEing the fact, do NOT serve a past-expiry value — fail closed to
        # REVERIFY, surfacing the current value as read-only prior_value + the expiry context.
        exp = row["expires_at"]
        if exp is not None and exp <= now:
            return _overlay_blocked(
                "REVERIFY", _REASON_EXPIRED, obj, fact_type, use_case,
                prior_value=row["value"], expires_at=_iso(exp),
                provenance={
                    "confirmed_event_id": row["confirmed_event_id"],
                    "catalog_source": row["catalog_source"],
                },
            )
        return _overlay_verified(row, obj, fact_type, use_case)

    # Non-VERIFIED overlay entry -> blocked (fail-closed). REVERIFY/STALE surface the
    # last VERIFIED value as read-only context (design §7.1); all others carry no value.
    prior_value = row["prior_value"] if status in ("REVERIFY", "STALE") else None
    return _overlay_blocked(
        status, _REASON_BY_STATUS.get(status, _REASON_MISSING), obj, fact_type, use_case, prior_value
    )
