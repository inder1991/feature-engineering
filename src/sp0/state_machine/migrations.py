from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sp0.contracts import (
    EventEnvelope,
    IdentityEnvelope,
    NewEvent,
    ProvenanceEnvelope,
)
from sp0.events.store import append_event, load_stream
from sp0.state_machine.event_types import (
    FEATURE_LIFECYCLE_VERSION_MIGRATED,
    WORKFLOW_VERSION_MIGRATED,
)
from sp0.state_machine.transition_table import load_transition_table


class MigrationError(Exception):
    """Raised when a table-version migration is invalid (downgrade, unknown
    target version, or a state that would be stranded by the new table, §4.2)."""


@dataclass(frozen=True, slots=True)
class MigrationResult:
    event: EventEnvelope
    from_table_version: int
    to_table_version: int


def _migrate(
    conn: Any,
    *,
    aggregate: str,
    aggregate_id: str,
    kind: str,
    event_type: str,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    stream = load_stream(conn, aggregate, aggregate_id)
    if not stream:
        raise MigrationError(f"{aggregate} {aggregate_id!r} has no events to migrate")
    from_version = stream[-1].table_version
    if to_table_version <= from_version:
        raise MigrationError(
            f"to_table_version {to_table_version} must be newer than current {from_version}"
        )
    target = load_transition_table(conn, kind, to_table_version)
    if not target.transitions:
        raise MigrationError(
            f"{kind} transition table version {to_table_version} does not exist"
        )
    if current_state not in target.states:
        raise MigrationError(
            f"current_state {current_state!r} not present in {kind} table "
            f"v{to_table_version}; migration would strand the aggregate"
        )
    new_event = NewEvent(
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        type=event_type,
        schema_version=1,
        payload={
            "from_table_version": from_version,
            "to_table_version": to_table_version,
            "current_state": current_state,
        },
        actor=actor,
        provenance=provenance,
        feature_id=aggregate_id if aggregate == "feature" else None,
        run_id=aggregate_id if aggregate == "run" else None,
    )
    event = append_event(
        conn, new_event, expected_version=expected_version, table_version=to_table_version
    )
    return MigrationResult(
        event=event, from_table_version=from_version, to_table_version=to_table_version
    )


def migrate_workflow_version(
    conn: Any,
    run_id: str,
    *,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    return _migrate(
        conn,
        aggregate="run",
        aggregate_id=run_id,
        kind="run",
        event_type=WORKFLOW_VERSION_MIGRATED,
        to_table_version=to_table_version,
        current_state=current_state,
        expected_version=expected_version,
        actor=actor,
        provenance=provenance,
    )


def migrate_feature_lifecycle_version(
    conn: Any,
    feature_id: str,
    *,
    to_table_version: int,
    current_state: str,
    expected_version: int,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
) -> MigrationResult:
    return _migrate(
        conn,
        aggregate="feature",
        aggregate_id=feature_id,
        kind="feature",
        event_type=FEATURE_LIFECYCLE_VERSION_MIGRATED,
        to_table_version=to_table_version,
        current_state=current_state,
        expected_version=expected_version,
        actor=actor,
        provenance=provenance,
    )
