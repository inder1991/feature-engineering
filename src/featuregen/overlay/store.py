from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.aggregates._append import append
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.store import load_stream
from featuregen.overlay.facts import (
    OVERLAY_FACT_CONFIRMED,
    OVERLAY_FACT_PROPOSED,
    validate_fact_value,
)


def _enforce_fact_value_schema(
    conn: DbConn, fact_key: str, type: str, payload: Mapping[str, Any]
) -> None:
    """Integrity boundary: enforce the per-fact-type FACT_VALUE_SCHEMAS at the overlay append
    boundary, NOT just at the command layer. `append_event` only checks the generic OVERLAY_EVENT
    schema (which declares value/proposed_value as a bare object), so a non-validating caller could
    otherwise persist a value that violates its fact type (§3.6 fail-closed). Reuses
    `validate_fact_value` (FACT_VALUE_SCHEMAS + the use_case rule) — no duplicated schema logic.

    PROPOSED carries fact_type/proposed_value/use_case directly. CONFIRMED carries only `value`
    (no fact_type), so fact_type/use_case are resolved from the fact's PROPOSED event — they are
    invariant for a fact_key (the key hashes fact_type+use_case), so any PROPOSED in the stream is
    authoritative (this also covers re-verify CONFIRMED, whose confirms_event_id points at a prior
    CONFIRMED, not a PROPOSED)."""
    if type == OVERLAY_FACT_PROPOSED:
        fact_type = payload.get("fact_type")
        proposed_value = payload.get("proposed_value")
        # If either is missing the generic event schema (run inside append) raises the right error.
        if fact_type is not None and proposed_value is not None:
            validate_fact_value(fact_type, proposed_value, use_case=payload.get("use_case"))
    elif type == OVERLAY_FACT_CONFIRMED:
        value = payload.get("value")
        if value is None:
            return
        proposed = next(
            (e for e in load_stream(conn, "overlay_fact", fact_key)
             if e.type == OVERLAY_FACT_PROPOSED),
            None,
        )
        if proposed is None:
            return  # no proposal in the stream -> cannot resolve fact_type; generic schema applies
        fact_type = proposed.payload.get("fact_type")
        if fact_type is not None:
            validate_fact_value(fact_type, value, use_case=proposed.payload.get("use_case"))


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
    fact_key` (Shared Contract). Never INSERTs into `events` directly (Global Constraint).

    Fail-closed: PROPOSED/CONFIRMED values are validated against their per-fact-type schema BEFORE
    any INSERT, so a malformed value never reaches the event store."""
    _enforce_fact_value_schema(conn, fact_key, type, payload)
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
