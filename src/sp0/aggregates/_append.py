from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from sp0.contracts import (
    DbConn, EventEnvelope, IdentityEnvelope, NewEvent, ProvenanceEnvelope,
)
from sp0.events.store import append_event, load_stream

PRODUCING_COMPONENT = "sp0-aggregates@0.1.0"

# §3.7 stage/artifact enum member (owned by Phase 02). Phase-06 events are governance/lifecycle
# events, not artifact producers, so their ProvenanceEnvelope.artifact_type is the governance
# record artifact — NOT the event-type name. (ProvenanceEnvelope.artifact_type "matches the §3.7
# stage/artifact enum casing"; an event type like "VERSION_MINTED" is NOT a §3.7 enum member.)
GOVERNANCE_ARTIFACT_TYPE = "APPROVAL_RECORD"


def current_version(conn: DbConn, aggregate: str, aggregate_id: str) -> int:
    stream = load_stream(conn, aggregate, aggregate_id)
    return stream[-1].stream_version if stream else 0


def table_version_for(conn: DbConn, aggregate: str, aggregate_id: str) -> int:
    if aggregate == "run":
        row = conn.execute(
            "SELECT table_version FROM run_workflow_state WHERE run_id = %s",
            (aggregate_id,),
        ).fetchone()
        if row is not None:
            return int(row[0])
    return 1


def provenance_for(artifact_type: str = GOVERNANCE_ARTIFACT_TYPE, **extra: Any) -> ProvenanceEnvelope:
    """Build a ProvenanceEnvelope. `artifact_type` MUST be a §3.7 stage/artifact enum value
    (defaults to the governance record artifact for lifecycle events); never pass an event-type
    name here."""
    return ProvenanceEnvelope(
        artifact_type=artifact_type, schema_version=1,
        producing_component=PRODUCING_COMPONENT, **extra,
    )


def identity_dict(actor: IdentityEnvelope) -> dict:
    return asdict(actor)


def append(
    conn: DbConn, *, aggregate: str, aggregate_id: str, type: str,
    payload: Mapping[str, Any], actor: IdentityEnvelope,
    provenance: Optional[ProvenanceEnvelope] = None,
    request_id: Optional[str] = None, feature_id: Optional[str] = None,
    run_id: Optional[str] = None, caused_by: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> EventEnvelope:
    if expected_version is None:
        expected_version = current_version(conn, aggregate, aggregate_id)
    new_event = NewEvent(
        aggregate=aggregate, aggregate_id=aggregate_id, type=type, schema_version=1,
        payload=dict(payload), actor=actor,
        provenance=provenance or provenance_for(),  # §3.7 artifact_type, NOT the event-type name
        request_id=request_id, feature_id=feature_id, run_id=run_id,
        caused_by=caused_by, occurred_at=datetime.now(timezone.utc),
    )
    return append_event(
        conn, new_event, expected_version=expected_version,
        table_version=table_version_for(conn, aggregate, aggregate_id),
    )
