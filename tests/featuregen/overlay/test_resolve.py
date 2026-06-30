from datetime import UTC, datetime

import pytest
from psycopg.types.json import Json

from featuregen.overlay.catalog import CatalogFact
from featuregen.overlay.identity import CatalogObjectRef, display_object_ref, fact_key
from featuregen.overlay.resolve import ResolvedFact, resolve_fact

_REF = CatalogObjectRef(
    catalog_source="enterprise",
    object_kind="column",
    schema="risk",
    table="loans",
    column="origination_ts",
)


class _StubCatalog:
    """Minimal CatalogAdapter: resolve_fact only ever calls get_fact()."""

    def __init__(self, fact: CatalogFact | None = None):
        self._fact = fact

    def list_objects(self):
        return []

    def get_fact(self, ref, fact_type, use_case=None):
        return self._fact

    def owner_of(self, ref):
        return None

    def fingerprint(self):
        return {}


def _seed_state(
    db,
    key,
    *,
    status,
    fact_type="availability_time",
    value=None,
    use_case=None,
    confirmers=None,
    confirmed_at=None,
    expires_at=None,
    prior_value=None,
    confirmed_event_id=None,
    updated_seq=1,
):
    with db.cursor() as cur:
        cur.execute(
            """
            INSERT INTO overlay_fact_state
              (fact_key, object_ref, fact_type, use_case, status, value, confirmers,
               confirmed_at, expires_at, prior_value, confirmed_event_id, updated_seq)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                key,
                display_object_ref(_REF),
                fact_type,
                use_case,
                status,
                Json(value) if value is not None else None,
                Json(confirmers if confirmers is not None else []),
                confirmed_at,
                expires_at,
                Json(prior_value) if prior_value is not None else None,
                confirmed_event_id,
                updated_seq,
            ),
        )


def test_authoritative_catalog_beats_overlay(db):
    key = fact_key(_REF, "availability_time")
    # An overlay VERIFIED row exists, but an authoritative catalog fact must win.
    _seed_state(
        db, key, status="VERIFIED", value={"column": "origination_ts"},
        confirmed_event_id="evt_overlay",
    )
    adapter = _StubCatalog(
        CatalogFact(value={"column": "as_of_date", "basis": "posted_at"}, authoritative=True)
    )

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert isinstance(resolved, ResolvedFact)
    assert resolved.source == "catalog"
    assert resolved.status == "VERIFIED"
    assert resolved.value == {"column": "as_of_date", "basis": "posted_at"}
    assert resolved.catalog_object == display_object_ref(_REF)
    assert resolved.reason_if_missing is None
    assert resolved.prior_value is None


def test_malformed_authoritative_catalog_not_served_as_verified(db):
    # A pluggable catalog claims authority but returns a value that violates the
    # availability_time schema (missing required "basis"). It must NOT be served as VERIFIED.
    adapter = _StubCatalog(CatalogFact(value={"bogus": "x"}, authoritative=True))

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.status != "VERIFIED"
    assert resolved.value is None
    assert resolved.reason_if_missing == "catalog_value_invalid"


def test_malformed_authoritative_catalog_does_not_fall_through_to_overlay(db):
    # Catalog precedence: the malformed authoritative catalog fact must fail closed and must
    # NOT be masked by a stale overlay VERIFIED value for the same fact_key.
    key = fact_key(_REF, "availability_time")
    _seed_state(
        db, key, status="VERIFIED", value={"column": "origination_ts", "basis": "posted_at"},
        confirmed_event_id="evt_overlay_stale",
    )
    adapter = _StubCatalog(CatalogFact(value={"bogus": "x"}, authoritative=True))

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "catalog"
    assert resolved.status != "VERIFIED"
    assert resolved.value is None
    assert resolved.reason_if_missing == "catalog_value_invalid"


def test_missing_is_fail_closed_with_reason(db):
    adapter = _StubCatalog(None)  # catalog has no ML fact, no overlay row seeded

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "missing"
    assert resolved.status == "missing"
    assert resolved.value is None
    assert resolved.reason_if_missing == "no_confirmed_fact"
    assert resolved.confirmed_by == ()
    assert resolved.prior_value is None


@pytest.mark.parametrize("status", ["DRAFT", "PARTIALLY_CONFIRMED", "REJECTED"])
def test_non_verified_overlay_blocks(db, status):
    key = fact_key(_REF, "availability_time")
    # Even with a value present on the row, a non-VERIFIED status must not be served.
    _seed_state(db, key, status=status, value={"column": "origination_ts"})
    adapter = _StubCatalog(None)

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "overlay"
    assert resolved.status == status
    assert resolved.value is None
    assert resolved.reason_if_missing is not None
    assert resolved.prior_value is None


def test_overlay_verified_fill(db):
    key = fact_key(_REF, "availability_time")
    confirmed = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    expires = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    _seed_state(
        db, key, status="VERIFIED", value={"column": "origination_ts"},
        confirmers=[{"subject": "u_owner", "role": "data_owner"}],
        confirmed_at=confirmed, expires_at=expires, confirmed_event_id="evt_conf1",
    )
    adapter = _StubCatalog(None)  # no authoritative catalog fact -> overlay fills

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "overlay"
    assert resolved.status == "VERIFIED"
    assert resolved.value == {"column": "origination_ts"}
    assert resolved.confirmed_by == ("u_owner",)
    assert resolved.confirmed_at == confirmed.isoformat()
    assert resolved.expires_at == expires.isoformat()
    assert resolved.provenance == {"confirmed_event_id": "evt_conf1"}
    assert resolved.reason_if_missing is None
    assert resolved.prior_value is None


def test_non_authoritative_catalog_uses_overlay(db):
    key = fact_key(_REF, "availability_time")
    _seed_state(
        db, key, status="VERIFIED", value={"column": "origination_ts"},
        confirmed_event_id="evt_conf2",
    )
    # An ML fact: information_schema/catalog is NOT authoritative -> overlay must be used.
    adapter = _StubCatalog(CatalogFact(value={"column": "as_of_date"}, authoritative=False))

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "overlay"
    assert resolved.status == "VERIFIED"
    assert resolved.value == {"column": "origination_ts"}


@pytest.mark.parametrize("status", ["REVERIFY", "STALE"])
def test_reverify_and_stale_return_prior_value(db, status):
    key = fact_key(_REF, "availability_time")
    # Fact under re-verification: no current value, but the last VERIFIED value is context.
    _seed_state(
        db, key, status=status, value=None,
        prior_value={"column": "origination_ts"}, confirmed_event_id="evt_prior",
    )
    adapter = _StubCatalog(None)

    resolved = resolve_fact(db, adapter, _REF, "availability_time")

    assert resolved.source == "overlay"
    assert resolved.status == status
    assert resolved.value is None  # never usable
    assert resolved.prior_value == {"column": "origination_ts"}
    assert resolved.reason_if_missing is not None
