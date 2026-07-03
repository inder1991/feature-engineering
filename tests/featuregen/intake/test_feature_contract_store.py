from __future__ import annotations

import pytest
from tests.featuregen._helpers import mint_test_service_identity

from featuregen.contracts import ConcurrencyError, SchemaValidationError
from featuregen.intake.events import CONTRACT_REFINED, INTENT_SUBMITTED
from featuregen.intake.store import append_feature_contract_event, load_feature_contract

_RUN = "run_store01"  # R1: feature_contract_id == run_id (one contract per run)


def _intake_actor():
    return mint_test_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:intake@1.0.0",
    )


def _intent_payload():
    # R2: emitters put NO id fields in the payload — only the SEMANTIC fields.
    return {
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
    }


def test_open_stream_appends_at_version_0_and_carries_correlation(conn):
    env = append_feature_contract_event(
        conn,
        run_id=_RUN,
        request_id="req_store01",
        type=INTENT_SUBMITTED,
        payload=_intent_payload(),
        actor=_intake_actor(),
        expected_version=0,
    )
    assert env.aggregate == "feature_contract"
    # R1: the seam sets feature_contract_id == aggregate_id == run_id.
    assert env.feature_contract_id == _RUN
    assert env.aggregate_id == _RUN
    assert env.run_id == _RUN
    assert env.request_id == "req_store01"
    assert env.stream_version == 1


def test_load_feature_contract_returns_the_stream_in_order(conn):
    append_feature_contract_event(
        conn, run_id=_RUN, request_id="req_store01",
        type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
        expected_version=0,
    )
    append_feature_contract_event(
        conn, run_id=_RUN,
        type=CONTRACT_REFINED,
        payload={"draft_doc_id": "doc_v2"},
        actor=_intake_actor(), expected_version=1,
    )
    stream = load_feature_contract(conn, _RUN)
    assert [e.type for e in stream] == [INTENT_SUBMITTED, CONTRACT_REFINED]
    assert [e.stream_version for e in stream] == [1, 2]


def test_stale_expected_version_raises_concurrency(conn):
    append_feature_contract_event(
        conn, run_id=_RUN, request_id="req_store01",
        type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
        expected_version=0,
    )
    with pytest.raises(ConcurrencyError):
        append_feature_contract_event(
            conn, run_id=_RUN,
            type=CONTRACT_REFINED,
            payload={"draft_doc_id": "doc_v2"},
            actor=_intake_actor(), expected_version=0,  # stale — stream is already at 1
        )


def test_unregistered_type_fails_closed(conn):
    # Prove registration is load-bearing: an unknown FC event type is refused before any INSERT.
    from featuregen.events.registry import reset_event_registry
    reset_event_registry()  # wipe the autouse registrations for this one assertion
    with pytest.raises(SchemaValidationError):
        append_feature_contract_event(
            conn, run_id=_RUN, request_id="req_store01",
            type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
            expected_version=0,
        )
