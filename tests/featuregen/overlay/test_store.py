import pytest

from featuregen.contracts import ConcurrencyError, IdentityEnvelope, SchemaValidationError
from featuregen.overlay import facts
from featuregen.overlay.facts import FactValidationError
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.store import append_overlay_event, load_fact


def _human():
    return IdentityEnvelope(
        subject="owner_a", actor_kind="human", authenticated=True,
        auth_method="oidc", role_claims=("data_owner",),
    )


def _proposed_payload():
    return {
        "catalog_object_ref": {"catalog_source": "pg:core", "object_kind": "table",
                               "schema": "core", "table": "transactions"},
        "object_ref": "core.transactions", "fact_type": "grain",
        "proposed_value": {"columns": ["id"], "is_unique": True},
        "proposal_fingerprint": "fp1",
        "proposed_by": "owner_a",  # actor subject STRING (pin 11)
    }


def _fk():
    ref = CatalogObjectRef(catalog_source="pg:core", object_kind="table",
                           schema="core", table="transactions")
    return fact_key(ref, "grain")


def test_append_and_load_round_trips(db):
    fk = _fk()
    ev = append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
                              payload=_proposed_payload(), actor=_human(), expected_version=0)
    assert ev.aggregate == "overlay_fact"
    assert ev.aggregate_id == fk
    assert ev.overlay_fact_id == fk
    stream = load_fact(db, fk)
    assert [e.event_id for e in stream] == [ev.event_id]
    assert stream[0].payload["proposal_fingerprint"] == "fp1"


def test_occ_rejects_stale_expected_version(db):
    fk = _fk()
    append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
                         payload=_proposed_payload(), actor=_human(), expected_version=0)
    # A second writer that still believes the stream is empty must be rejected.
    with pytest.raises(ConcurrencyError):
        append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
                             payload=_proposed_payload(), actor=_human(), expected_version=0)


def test_append_rejects_invalid_proposed_value_at_event_boundary(db):
    # F3: grain requires is_unique; the store boundary MUST reject the malformed value rather than
    # persist it (regression: previously only validated proposed_value is an object).
    fk = _fk()
    bad = {
        "catalog_object_ref": {"catalog_source": "pg:core", "object_kind": "table",
                               "schema": "core", "table": "transactions"},
        "object_ref": "core.transactions", "fact_type": "grain",
        "proposed_value": {"columns": ["id"]},  # missing required is_unique
        "proposal_fingerprint": "fp1", "proposed_by": "owner_a",
    }
    with pytest.raises((SchemaValidationError, FactValidationError)):
        append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
                             payload=bad, actor=_human(), expected_version=0)
    assert load_fact(db, fk) == []  # nothing persisted (fail-closed)


def test_append_rejects_invalid_confirmed_value_at_event_boundary(db):
    # F3 CONFIRMED side: a valid grain proposal, then a CONFIRMED whose value drops is_unique must
    # be rejected. fact_type/use_case are resolved from the PROPOSED event (CONFIRMED has none).
    fk = _fk()
    proposed = append_overlay_event(
        db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        payload=_proposed_payload(), actor=_human(), expected_version=0,
    )
    with pytest.raises((SchemaValidationError, FactValidationError)):
        append_overlay_event(
            db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED, actor=_human(),
            payload={
                "value": {"columns": ["id"]},  # missing required is_unique
                "confirmers": [{"subject": "owner_a", "role": "data_owner"}],
                "expires_at": "2026-12-31T00:00:00+00:00",
                "confirms_event_id": proposed.event_id,
            },
        )
    # only the valid PROPOSED persisted; the malformed CONFIRMED never landed (fail-closed)
    assert [e.type for e in load_fact(db, fk)] == [facts.OVERLAY_FACT_PROPOSED]
