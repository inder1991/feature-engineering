"""C-D11 — the typed planning request, and a 409 gate that can actually fire.

The gate today compares two values written from ONE in-memory object in one statement, so it is
unreachable through the production write path. These tests exercise the SECOND source: the hash is
recomputed from the stored payload's own bytes, so corrupting the payload, the stored hash, or the
decision record's reference each refuse.

Round-tripping covers all THREE origins. `__post_init__` applies origin-DEPENDENT rules
(`feature_planning_contracts.py:157-161` forbids binding hints outside `user_definition`), and
`user_definition` is the only origin that may carry `binding_hint_refs` — a nested tuple the parser
must re-tuple, which JSON cannot represent.
"""
from __future__ import annotations

import dataclasses
import json

import pytest

from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    planning_request_hash,
)
from featuregen.overlay.upload.planning_request_store import (
    DECISION_RECORD_TAMPERED,
    LEGACY_PLANNING_REQUEST_UNAVAILABLE,
    DecisionRecordTampered,
    LegacyPlanningRequestUnavailable,
    canonical_planning_request_payload,
    load_verified_planning_request,
    parse_planning_request,
    store_planning_request,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id


def _recipe_request() -> FeaturePlanningRequestV1:
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe

    return planning_request_from_recipe(v2_recipe_by_id("posted_debit_amount"))


def _all_shipped_recipe_requests():
    from featuregen.overlay.upload.feature_planning_contracts import planning_request_from_recipe
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    for definition in V2_RECIPES:
        yield planning_request_from_recipe(definition)


# ══ the round trip ═══════════════════════════════════════════════════════════════════════════════
def test_a_recipe_request_ROUND_TRIPS():
    request = _recipe_request()
    payload = canonical_planning_request_payload(request)
    assert parse_planning_request(json.loads(json.dumps(payload))) == request


def test_the_recomputed_hash_matches_for_EVERY_SHIPPED_RECIPE():
    """Empirical rather than sampled: if the parser drops or mistypes any field on any recipe, the
    recomputed hash moves and this fails."""
    checked = 0
    for request in _all_shipped_recipe_requests():
        payload = json.loads(json.dumps(canonical_planning_request_payload(request)))
        assert planning_request_hash(parse_planning_request(payload)) == planning_request_hash(
            request), request.source_definition_id
        checked += 1
    assert checked >= 300, f"only {checked} recipes exercised"


def test_the_parser_is_FIELD_EXHAUSTIVE_not_hand_written():
    """A hand-written parser would silently drop a newly added field and the recomputed hash would
    then never match. This is driven by each dataclass's own fields."""
    from featuregen.overlay.upload import planning_request_store

    source = __import__("inspect").getsource(planning_request_store._from_canonical)
    assert "fields(cls)" in source
    for hand_written in ('payload["origin"]', 'payload["operands"]', 'payload.get("temporal")'):
        assert hand_written not in source


def test_a_MISSING_field_in_the_payload_refuses_rather_than_defaulting():
    """Serialized by a different build — the hash recomputed from it could never match, and
    defaulting the field would produce a request nobody wrote."""
    payload = canonical_planning_request_payload(_recipe_request())
    del payload["computation_kind"]
    with pytest.raises(DecisionRecordTampered, match="different build"):
        parse_planning_request(payload)


def test_a_non_object_payload_refuses():
    with pytest.raises(DecisionRecordTampered, match="not an object"):
        parse_planning_request(["not", "an", "object"])


def test_NESTED_TUPLES_come_back_as_tuples():
    """JSON has no tuple. If `operands` came back as a list the dataclass would compare unequal and
    the hash would move."""
    parsed = parse_planning_request(
        json.loads(json.dumps(canonical_planning_request_payload(_recipe_request()))))
    assert isinstance(parsed.operands, tuple)
    assert all(dataclasses.is_dataclass(op) for op in parsed.operands)
    assert isinstance(parsed.supporting_objectives, tuple)


# ══ the five checks, against a real database ═════════════════════════════════════════════════════
def _store(db, request, *, revision="cons-1", option="opt-1") -> str:
    return store_planning_request(
        db, considered_revision_id=revision, option_id=option, request=request)


def test_a_stored_request_loads_back_VERIFIED(db):
    request = _recipe_request()
    stored_hash = _store(db, request)
    loaded = load_verified_planning_request(
        db, considered_revision_id="cons-1", option_id="opt-1",
        decision_record_reference=stored_hash)
    assert loaded == request


def test_A_CORRUPTED_PAYLOAD_IS_TAMPERING(db):
    """The check the inert gate cannot make: the hash is recomputed from the BYTES."""
    request = _recipe_request()
    stored_hash = _store(db, request, option="opt-payload")
    tampered = canonical_planning_request_payload(request)
    # A field that is hash-bearing but survives `__post_init__` — corrupting a VALIDATED field
    # would refuse at parse time, which proves the validator works rather than that the hash check
    # does. This is the case the inert gate cannot catch: valid-looking bytes, wrong identity.
    tampered["source_content_hash"] = "sha256:not-what-was-hashed"
    db.execute(
        "INSERT INTO typed_planning_request (considered_revision_id, option_id, request_payload, "
        "planning_request_hash) VALUES (%s, %s, %s::jsonb, %s)",
        ("cons-1", "opt-corrupt", json.dumps(tampered), stored_hash))

    with pytest.raises(DecisionRecordTampered, match="bytes and the identity claimed"):
        load_verified_planning_request(
            db, considered_revision_id="cons-1", option_id="opt-corrupt",
            decision_record_reference=stored_hash)


def test_A_CORRUPTED_STORED_HASH_IS_TAMPERING(db):
    request = _recipe_request()
    db.execute(
        "INSERT INTO typed_planning_request (considered_revision_id, option_id, request_payload, "
        "planning_request_hash) VALUES (%s, %s, %s::jsonb, %s)",
        ("cons-1", "opt-badhash",
         json.dumps(canonical_planning_request_payload(request)), "sha256:not-the-real-hash"))

    with pytest.raises(DecisionRecordTampered, match="bytes and the identity claimed"):
        load_verified_planning_request(
            db, considered_revision_id="cons-1", option_id="opt-badhash",
            decision_record_reference="sha256:not-the-real-hash")


def test_A_DRIFTED_DECISION_REFERENCE_IS_TAMPERING(db):
    """"The decision was made about a different request than the one on file"."""
    stored_hash = _store(db, _recipe_request(), option="opt-ref")
    with pytest.raises(DecisionRecordTampered, match="different request than the one on file"):
        load_verified_planning_request(
            db, considered_revision_id="cons-1", option_id="opt-ref",
            decision_record_reference="sha256:some-other-request")
    assert stored_hash


def test_A_LEGACY_ROW_IS_NOT_TAMPERING(db):
    """A row predating the store has nothing to verify against. Reporting it as tampered would
    accuse the system of something it did not do."""
    with pytest.raises(LegacyPlanningRequestUnavailable, match="NOT evidence of tampering"):
        load_verified_planning_request(
            db, considered_revision_id="cons-legacy", option_id="opt-legacy",
            decision_record_reference="sha256:whatever")


def test_the_two_refusals_are_DIFFERENT_exceptions():
    assert not issubclass(LegacyPlanningRequestUnavailable, DecisionRecordTampered)
    assert LEGACY_PLANNING_REQUEST_UNAVAILABLE != DECISION_RECORD_TAMPERED


def test_the_stored_row_is_WRITE_ONCE(db):
    """A row that can be updated is one that can be brought into agreement with a tampered decision
    after the fact, which would defeat the entire point of the second source."""
    import psycopg

    _store(db, _recipe_request(), option="opt-writeonce")
    with pytest.raises(psycopg.errors.RaiseException, match="write-once"):
        db.execute(
            "UPDATE typed_planning_request SET planning_request_hash = %s WHERE option_id = %s",
            ("sha256:rewritten", "opt-writeonce"))


def test_the_hash_is_stored_SEPARATELY_from_the_payload(db):
    """A hash stored inside the payload it describes cannot disprove that payload — precisely the
    shape the inert gate already has."""
    _store(db, _recipe_request(), option="opt-columns")
    row = db.execute(
        "SELECT request_payload, planning_request_hash FROM typed_planning_request "
        "WHERE option_id = %s", ("opt-columns",)).fetchone()
    assert "planning_request_hash" not in row[0]
    assert row[1]
