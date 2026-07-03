from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.serde import (
    identity_from_jsonb,
    identity_to_jsonb,
    provenance_from_jsonb,
    provenance_to_jsonb,
    row_to_event,
)


def test_identity_round_trips_tuples_as_lists():
    idv = IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake", "writer"),
        groups=("g1",),
        attestation="deploy:abc",
    )
    blob = identity_to_jsonb(idv)
    assert blob["role_claims"] == ["intake", "writer"]
    assert identity_from_jsonb(blob) == idv


def test_provenance_round_trips():
    prov = ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=2,
        producing_component="sp2-intake@1.4.0",
        tool_versions={"llm_model": "x"},
        source_snapshots=("delta:core@v1",),
        random_seed=42,
    )
    blob = provenance_to_jsonb(prov)
    assert blob["source_snapshots"] == ["delta:core@v1"]
    assert provenance_from_jsonb(blob) == prov


def test_row_to_event_reconstructs_envelope():
    now = datetime.now(UTC)
    row = {
        "event_id": "evt_1",
        "global_seq": 7,
        "aggregate": "run",
        "aggregate_id": "run_1",
        "stream_version": 3,
        "type": "CONTRACT_CONFIRMED",
        "schema_version": 1,
        "table_version": 12,
        "actor": identity_to_jsonb(
            IdentityEnvelope(
                subject="user:raj",
                actor_kind="human",
                authenticated=True,
                auth_method="oidc",
                role_claims=(),
            )
        ),
        "payload": {"confirmed_contract_ref": "doc_1"},
        "provenance": provenance_to_jsonb(
            ProvenanceEnvelope(
                artifact_type="CONFIRMED_CONTRACT",
                schema_version=1,
                producing_component="c@1",
            )
        ),
        "occurred_at": now,
        "recorded_at": now,
        "request_id": "req_1",
        "feature_id": None,
        "run_id": "run_1",
        "overlay_fact_id": None,
        "feature_contract_id": None,
        "caused_by": None,
    }
    env = row_to_event(row)
    assert isinstance(env, EventEnvelope)
    assert env.global_seq == 7
    assert env.actor.subject == "user:raj"
    assert env.payload["confirmed_contract_ref"] == "doc_1"
    assert env.overlay_fact_id is None
    assert env.feature_contract_id is None
