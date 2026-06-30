from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.aggregates._append import append
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.store import load_stream


def append_overlay_event(
    conn: DbConn,
    *,
    fact_key: str,
    type: str,
    payload: Mapping[str, Any],
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope | None = None,
    expected_version: int | None = None,
    caused_by: str | None = None,
) -> EventEnvelope:
    """Append one overlay_fact event via the SP-0 OCC helper. `aggregate_id == overlay_fact_id ==
    fact_key` (Shared Contract). Never INSERTs into `events` directly (Global Constraint)."""
    return append(
        conn,
        aggregate="overlay_fact",
        aggregate_id=fact_key,
        overlay_fact_id=fact_key,
        type=type,
        payload=payload,
        actor=actor,
        provenance=provenance,
        expected_version=expected_version,
        caused_by=caused_by,
    )


def load_fact(conn: DbConn, fact_key: str) -> list[EventEnvelope]:
    """Load the full overlay_fact event stream (stream_version ASC) for `fact_key`."""
    return load_stream(conn, "overlay_fact", fact_key)
