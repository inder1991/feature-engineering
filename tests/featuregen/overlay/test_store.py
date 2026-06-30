import pytest

from featuregen.contracts import ConcurrencyError, IdentityEnvelope
from featuregen.overlay import facts
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
