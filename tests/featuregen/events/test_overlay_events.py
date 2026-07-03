from __future__ import annotations

import psycopg
import pytest
from tests.featuregen._helpers import mint_test_service_identity

from featuregen.aggregates._append import append
from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import event_registry
from featuregen.runtime.outbox import partition_key_for

_FK = "a1b2c3d4e5f6"  # stand-in for a fact_key (sha256 hex) — Phase 1 store layer is key-agnostic


def _overlay_actor():
    return mint_test_service_identity(
        subject="service:overlay-profiler",
        role_claims=["overlay"],
        attestation="signed-deploy-id:overlay@1.0.0",
    )


def _register(type_name="OVERLAY_FACT_TEST"):
    event_registry().register_schema(type_name, 1, {"type": "object"}, "overlay")


def test_overlay_fact_append_succeeds(conn):
    _register()
    env = append(
        conn,
        aggregate="overlay_fact",
        aggregate_id=_FK,
        overlay_fact_id=_FK,
        type="OVERLAY_FACT_TEST",
        payload={"k": "v"},
        actor=_overlay_actor(),
    )
    assert env.aggregate == "overlay_fact"
    assert env.overlay_fact_id == _FK
    assert env.aggregate_id == _FK
    assert env.stream_version == 1
    row = conn.execute(
        "SELECT aggregate, aggregate_id, overlay_fact_id, request_id, feature_id, run_id "
        "FROM events WHERE event_id=%s",
        (env.event_id,),
    ).fetchone()
    assert row == ("overlay_fact", _FK, _FK, None, None, None)


def test_overlay_fact_with_request_id_is_rejected_by_consistency_check(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "overlay_fact_id, request_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'overlay_fact',%s,1,%s,'req_x','OVERLAY_FACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad", _FK, _FK),
        )


def test_request_append_still_passes(conn):
    _register("REQ_TEST")
    env = append(
        conn,
        aggregate="request",
        aggregate_id="req_1",
        request_id="req_1",
        type="REQ_TEST",
        payload={},
        actor=_overlay_actor(),
    )
    assert env.aggregate == "request"
    assert env.request_id == "req_1"
    assert env.overlay_fact_id is None


def test_partition_key_for_overlay_fact(conn):
    _register()
    env = append(
        conn,
        aggregate="overlay_fact",
        aggregate_id=_FK,
        overlay_fact_id=_FK,
        type="OVERLAY_FACT_TEST",
        payload={},
        actor=_overlay_actor(),
    )
    assert partition_key_for(env) == f"overlay_fact:{_FK}"


def test_overlay_events_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    col = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='events' AND column_name='overlay_fact_id'"
    ).fetchone()
    assert col is not None
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='events_aggregate_id_consistent'"
    ).fetchone()
    assert chk is not None
