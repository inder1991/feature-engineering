from __future__ import annotations

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events.registry import event_registry
from sp0.events.store import append_event


def _new(agg_id: str) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=agg_id,
        type="E",
        schema_version=1,
        payload={},
        actor=IdentityEnvelope(
            subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=agg_id,
    )


def test_global_seq_strictly_increases_across_distinct_streams(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    seqs = [
        append_event(conn, _new("run_p"), expected_version=0, table_version=1).global_seq,
        append_event(conn, _new("run_q"), expected_version=0, table_version=1).global_seq,
        append_event(conn, _new("run_p"), expected_version=1, table_version=1).global_seq,
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3  # no duplicates across streams
