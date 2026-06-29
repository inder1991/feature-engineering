from __future__ import annotations

import pytest

from featuregen.contracts import ConcurrencyError, IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event


def _new(run_id: str) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_STARTED",
        schema_version=1,
        payload={},
        actor=IdentityEnvelope(
            subject="user:raj",
            actor_kind="human",
            authenticated=True,
            auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=run_id,
    )


def test_stale_expected_version_raises_concurrency_error(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="featuregen")
    append_event(conn, _new("run_x"), expected_version=0, table_version=1)
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_x"), expected_version=0, table_version=1)


def test_ahead_of_head_expected_version_raises_concurrency_error(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="featuregen")
    append_event(conn, _new("run_z"), expected_version=0, table_version=1)
    # expected_version GREATER than the current head (1) must NOT silently insert a
    # stream_version gap; it must raise ConcurrencyError.
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_z"), expected_version=5, table_version=1)


def test_connection_usable_after_conflict_and_correct_retry_succeeds(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="featuregen")
    append_event(conn, _new("run_y"), expected_version=0, table_version=1)
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_y"), expected_version=0, table_version=1)
    # the conflict did not poison the transaction: a correct retry still succeeds.
    retried = append_event(conn, _new("run_y"), expected_version=1, table_version=1)
    assert retried.stream_version == 2
