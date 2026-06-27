from __future__ import annotations

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events.registry import event_registry
from sp0.events.store import append_event


def _idv() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("ds",),
    )


def _prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
    )


def _new(run_id: str, payload: dict) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_STARTED",
        schema_version=1,
        payload=payload,
        actor=_idv(),
        provenance=_prov(),
        run_id=run_id,
    )


def test_append_allocates_seq_and_stream_version(conn):
    event_registry().register_schema(
        "RUN_STARTED", 1, {"type": "object"}, owner="sp0"
    )
    env = append_event(conn, _new("run_a", {}), expected_version=0, table_version=1)
    assert env.stream_version == 1
    assert env.global_seq >= 1
    assert env.event_id.startswith("evt_")
    assert env.run_id == "run_a"


def test_append_increments_stream_version_and_global_seq(conn):
    event_registry().register_schema(
        "RUN_STARTED", 1, {"type": "object"}, owner="sp0"
    )
    first = append_event(conn, _new("run_b", {}), expected_version=0, table_version=1)
    second = append_event(conn, _new("run_b", {}), expected_version=1, table_version=1)
    assert second.stream_version == 2
    assert second.global_seq > first.global_seq


def test_append_validates_payload_against_registry(conn):
    import pytest

    from sp0.contracts import SchemaValidationError

    event_registry().register_schema(
        "RUN_STARTED",
        1,
        {"type": "object", "required": ["needed"], "properties": {"needed": {"type": "string"}}},
        owner="sp0",
    )
    with pytest.raises(SchemaValidationError):
        append_event(conn, _new("run_c", {}), expected_version=0, table_version=1)


def test_append_blocks_writes_to_deprecated_schema(conn):
    import pytest

    from sp0.contracts import SchemaValidationError

    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="sp0")
    event_registry().set_status("RUN_STARTED", 1, "deprecated")
    with pytest.raises(SchemaValidationError):
        append_event(conn, _new("run_d", {}), expected_version=0, table_version=1)
