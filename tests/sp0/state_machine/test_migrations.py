from __future__ import annotations

import uuid

import pytest

from sp0.contracts import (
    ConcurrencyError,
    IdentityEnvelope,
    NewEvent,
    ProvenanceEnvelope,
)
from sp0.events.store import append_event, load_stream
from sp0.state_machine.guards import InMemoryPredicateRegistry
from sp0.state_machine.migrations import (
    MigrationError,
    migrate_feature_lifecycle_version,
    migrate_workflow_version,
)
from sp0.state_machine.transition_table import Transition, install_transition_table

ACTOR = IdentityEnvelope(
    subject="user:test",
    actor_kind="human",
    authenticated=True,
    auth_method="oidc",
    role_claims=(),
)
PROV = ProvenanceEnvelope(
    artifact_type="APPROVAL_RECORD",
    schema_version=1,
    producing_component="sp0-test@0.0.0",
)


def _draft_transition(table_version: int) -> Transition:
    return Transition(
        table_version=table_version,
        from_state="DRAFT",
        to_state="CONFIRMED_CONTRACT",
        trigger="CONFIRM",
        guard_expr=None,
        guard_inputs={},
        precedence=100,
        on_success={"to": "CONFIRMED_CONTRACT", "emits": "CONTRACT_CONFIRMED"},
        on_guard_fail=None,
    )


def _seed_run(conn, *, table_version: int) -> str:
    run_id = f"run_{uuid.uuid4().hex}"
    append_event(
        conn,
        NewEvent(
            aggregate="run",
            aggregate_id=run_id,
            type="SM_TEST_SEED",
            schema_version=1,
            payload={},
            actor=ACTOR,
            provenance=PROV,
            run_id=run_id,
        ),
        expected_version=0,
        table_version=table_version,
    )
    return run_id


def test_migrate_workflow_version_appends_audited_event(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)

    result = migrate_workflow_version(
        conn, run_id,
        to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )

    assert result.from_table_version == 1
    assert result.to_table_version == 2
    assert result.event.type == "WORKFLOW_VERSION_MIGRATED"
    assert result.event.table_version == 2
    assert result.event.payload == {
        "from_table_version": 1,
        "to_table_version": 2,
        "current_state": "DRAFT",
    }


def test_earlier_events_keep_old_table_version(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    migrate_workflow_version(
        conn, run_id, to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )
    stream = load_stream(conn, "run", run_id)
    assert [e.table_version for e in stream] == [1, 2]


def test_downgrade_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=2)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=1, current_state="DRAFT", expected_version=1,
            actor=ACTOR, provenance=PROV,
        )


def test_unknown_target_version_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="DRAFT", expected_version=1,
            actor=ACTOR, provenance=PROV,
        )


def test_stranded_state_rejected(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(MigrationError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="STATE_NOT_IN_V2",
            expected_version=1, actor=ACTOR, provenance=PROV,
        )


def test_occ_conflict_raises(conn) -> None:
    install_transition_table(conn, "run", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "run", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    run_id = _seed_run(conn, table_version=1)
    with pytest.raises(ConcurrencyError):
        migrate_workflow_version(
            conn, run_id, to_table_version=2, current_state="DRAFT",
            expected_version=99, actor=ACTOR, provenance=PROV,
        )


def test_migrate_feature_lifecycle_version(conn) -> None:
    install_transition_table(conn, "feature", 1, [_draft_transition(1)], InMemoryPredicateRegistry())
    install_transition_table(conn, "feature", 2, [_draft_transition(2)], InMemoryPredicateRegistry())
    feature_id = f"feat_{uuid.uuid4().hex}"
    append_event(
        conn,
        NewEvent(
            aggregate="feature", aggregate_id=feature_id, type="SM_TEST_SEED",
            schema_version=1, payload={}, actor=ACTOR, provenance=PROV,
            feature_id=feature_id,
        ),
        expected_version=0,
        table_version=1,
    )
    result = migrate_feature_lifecycle_version(
        conn, feature_id, to_table_version=2, current_state="DRAFT", expected_version=1,
        actor=ACTOR, provenance=PROV,
    )
    assert result.event.type == "FEATURE_LIFECYCLE_VERSION_MIGRATED"
    assert result.event.table_version == 2
    assert result.event.feature_id == feature_id
