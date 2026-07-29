"""Build the GOVERNED FACT behind a cross-catalog bridge fixture.

A row in ``entity_bridge_candidate_evidence`` never exists on its own in production: ``bridge_propose``
writes the ledger only AFTER ``propose_fact`` is accepted, so every ledger row has an ``overlay_fact``
stream behind it and the bridge's lifecycle status is readable. A fixture that inserts the ledger row
alone builds a shape the platform cannot produce — and once availability is decided by an ALLOW-LIST
over that lifecycle (``cross_catalog_links.AVAILABLE_STATUSES``), such a row is correctly unavailable:
there is no governance record to allow.

So fixtures say which lifecycle state they mean, and get the events that state really comes from.
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay import facts, store

_ACTOR = mint_test_identity(subject="user:bridge-proposer", role_claims=("data_owner",))


def _ref(source: str, object_ref: str) -> dict:
    """``schema.table.column`` -> the catalog object ref shape the entity_bridge value schema wants."""
    schema, table, column = object_ref.split(".", 2)
    return {"catalog_source": source, "object_kind": "column", "schema": schema,
            "table": table, "column": column}


def govern_bridge_fact(db, fact_key: str, *, entity: str, left_source: str, left_ref: str,
                       right_source: str, right_ref: str, status: str = "DRAFT") -> str:
    """Drive ``fact_key``'s overlay_fact stream to ``status`` using the events production appends.

    ``status`` is any of DRAFT / PARTIALLY_CONFIRMED / VERIFIED / REJECTED / STALE / REVERIFY. The
    last two are reached the way the platform reaches them: STALE via ``OVERLAY_FACT_STALED`` (drift)
    and REVERIFY via ``OVERLAY_FACT_EXPIRED`` (a lapsed confirmation) — see ``overlay/state.py``.
    """
    value = {"entity_id": entity,
             "left_ref": _ref(left_source, left_ref),
             "right_ref": _ref(right_source, right_ref)}
    proposed = store.append_overlay_event(
        db, fact_key=fact_key, type=facts.OVERLAY_FACT_PROPOSED, actor=_ACTOR,
        payload={"catalog_object_ref": value["left_ref"], "object_ref": left_ref,
                 "fact_type": "entity_bridge", "proposed_value": value,
                 "proposal_fingerprint": f"fp-{fact_key}", "proposed_by": _ACTOR.subject})
    if status == "DRAFT":
        return fact_key
    if status == "PARTIALLY_CONFIRMED":
        store.append_overlay_event(
            db, fact_key=fact_key, type=facts.OVERLAY_FACT_PARTIALLY_CONFIRMED, actor=_ACTOR,
            payload={"by_owner": "user:one", "role": "data_owner",
                     "draft_event_id": proposed.event_id})
        return fact_key
    if status == "REJECTED":
        store.append_overlay_event(
            db, fact_key=fact_key, type=facts.OVERLAY_FACT_REJECTED, actor=_ACTOR,
            payload={"rejected_by": "user:one", "target_event_id": proposed.event_id})
        return fact_key
    confirmed = store.append_overlay_event(
        db, fact_key=fact_key, type=facts.OVERLAY_FACT_CONFIRMED, actor=_ACTOR,
        payload={"value": value, "confirms_event_id": proposed.event_id,
                 "confirmers": [{"subject": "user:one", "role": "data_owner"},
                                {"subject": "user:two", "role": "platform_admin"}]})
    if status == "VERIFIED":
        return fact_key
    if status == "STALE":
        store.append_overlay_event(
            db, fact_key=fact_key, type=facts.OVERLAY_FACT_STALED, actor=_ACTOR,
            payload={"catalog_change_ref": "chg-1",
                     "stales_confirmed_event_id": confirmed.event_id})
        return fact_key
    if status == "REVERIFY":
        store.append_overlay_event(
            db, fact_key=fact_key, type=facts.OVERLAY_FACT_EXPIRED, actor=_ACTOR,
            payload={"expires_confirmed_event_id": confirmed.event_id})
        return fact_key
    raise AssertionError(f"unknown bridge lifecycle status {status!r}")
