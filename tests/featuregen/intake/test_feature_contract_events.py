from __future__ import annotations

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_service_identity

from featuregen.aggregates._append import append
from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import event_registry
from featuregen.runtime.outbox import partition_key_for

_FC = "fc_test0001"
_RUN = "run_test0001"


def _intake_actor():
    return mint_test_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:intake@1.0.0",
    )


def _register(type_name="FEATURE_CONTRACT_TEST"):
    event_registry().register_schema(type_name, 1, {"type": "object"}, "featuregen-intake")


def test_feature_contract_append_succeeds(conn):
    _register()
    env = append(
        conn,
        aggregate="feature_contract",
        aggregate_id=_FC,
        feature_contract_id=_FC,
        run_id=_RUN,
        request_id="req_1",
        type="FEATURE_CONTRACT_TEST",
        payload={"k": "v"},
        actor=_intake_actor(),
    )
    assert env.aggregate == "feature_contract"
    assert env.feature_contract_id == _FC
    assert env.aggregate_id == _FC
    assert env.run_id == _RUN
    assert env.stream_version == 1
    row = conn.execute(
        "SELECT aggregate, aggregate_id, feature_contract_id, run_id, request_id, feature_id "
        "FROM events WHERE event_id=%s",
        (env.event_id,),
    ).fetchone()
    assert row == ("feature_contract", _FC, _FC, _RUN, "req_1", None)


def test_feature_contract_with_feature_id_is_rejected_by_consistency_check(conn):
    # A contract precedes any feature, so the feature_contract branch mandates feature_id IS NULL.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "feature_contract_id, feature_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'feature_contract',%s,1,%s,'feat_x','FEATURE_CONTRACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad", _FC, _FC),
        )


def test_feature_contract_id_mismatch_is_rejected_by_consistency_check(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "feature_contract_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'feature_contract','fc_A',1,'fc_B','FEATURE_CONTRACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad2",),
        )


def test_feature_contract_with_null_run_id_is_rejected_by_consistency_check(conn):
    # X3: a feature_contract event MUST carry its run_id mirror (aggregate_id == feature_contract_id
    # == run_id) — the consistency CHECK requires run_id NON-NULL even when the id-equality holds.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "feature_contract_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'feature_contract',%s,1,%s,'FEATURE_CONTRACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_null_run", _FC, _FC),
        )


def test_request_and_overlay_appends_still_pass(conn):
    _register("REQ_TEST")
    env = append(
        conn,
        aggregate="request",
        aggregate_id="req_2",
        request_id="req_2",
        type="REQ_TEST",
        payload={},
        actor=_intake_actor(),
    )
    assert env.aggregate == "request"
    assert env.feature_contract_id is None


def test_partition_key_for_feature_contract(conn):
    _register()
    env = append(
        conn,
        aggregate="feature_contract",
        aggregate_id=_FC,
        feature_contract_id=_FC,
        run_id=_RUN,
        type="FEATURE_CONTRACT_TEST",
        payload={},
        actor=_intake_actor(),
    )
    assert partition_key_for(env) == f"feature_contract:{_FC}"


def test_feature_contract_events_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    col = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='events' AND column_name='feature_contract_id'"
    ).fetchone()
    assert col is not None
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='events_aggregate_id_consistent'"
    ).fetchone()
    assert chk is not None
    # Regression: SP-1's overlay_fact aggregate value must survive the rebuild.
    agg = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='events_aggregate_check'"
    ).fetchone()[0]
    assert "overlay_fact" in agg and "feature_contract" in agg
