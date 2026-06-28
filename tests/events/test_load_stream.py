from __future__ import annotations

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events.registry import (
    event_registry,
    load_registry_snapshot,
    persist_registry_snapshot,
)
from sp0.events.store import append_event, load_stream


def _new(run_id: str, type_: str, payload: dict) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type=type_,
        schema_version=1,
        payload=payload,
        actor=IdentityEnvelope(
            subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=run_id,
    )


def test_load_stream_orders_by_stream_version(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    append_event(conn, _new("r1", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r1", "E", {"n": 2}), expected_version=1, table_version=1)
    stream = load_stream(conn, "run", "r1")
    assert [e.stream_version for e in stream] == [1, 2]
    assert [e.payload["n"] for e in stream] == [1, 2]


def test_load_stream_upto_seq_filters(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    a = append_event(conn, _new("r2", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r2", "E", {"n": 2}), expected_version=1, table_version=1)
    stream = load_stream(conn, "run", "r2", upto_seq=a.global_seq)
    assert [e.payload["n"] for e in stream] == [1]


def test_load_stream_upcasts_to_expected_version(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    event_registry().register_schema("E", 2, {"type": "object"}, owner="o")
    event_registry().register_upcaster("E", 1, 2, lambda b: {**b, "added_v2": True})
    append_event(conn, _new("r3", "E", {"n": 1}), expected_version=0, table_version=1)
    stream = load_stream(conn, "run", "r3", expected={"E": 2})
    assert stream[0].schema_version == 2
    assert stream[0].payload == {"n": 1, "added_v2": True}


def test_load_stream_only_returns_requested_aggregate(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    append_event(conn, _new("r4", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r5", "E", {"n": 9}), expected_version=0, table_version=1)
    assert [e.run_id for e in load_stream(conn, "run", "r4")] == ["r4"]


def test_load_stream_upcasts_using_a_pinned_snapshot(conn):
    # §3.3 determinism end-to-end: a v1 event is written, the registry is snapshotted, and a
    # later replay resolves the PINNED snapshot id back to {type: version} to drive upcast-on-read
    # — i.e. the write-side snapshot is actually consumed, not merely stamped into provenance.
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    event_registry().register_schema("E", 2, {"type": "object"}, owner="o")
    event_registry().register_upcaster("E", 1, 2, lambda b: {**b, "added_v2": True})
    append_event(conn, _new("r6", "E", {"n": 1}), expected_version=0, table_version=1)

    snapshot_id = persist_registry_snapshot(conn, event_registry())  # pins {"E": 2}
    expected = load_registry_snapshot(conn, snapshot_id)             # read path resolves it back
    assert expected == {"E": 2}

    stream = load_stream(conn, "run", "r6", expected=expected)
    assert stream[0].schema_version == 2
    assert stream[0].payload == {"n": 1, "added_v2": True}
