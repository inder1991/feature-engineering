"""The SP-2 append seam for the `feature_contract` aggregate (mirrors SP-1's overlay/store.py).
Never INSERTs into `events` directly (Global Constraint) — it rides SP-0's OCC/provenance/global_seq
helper. `aggregate_id == feature_contract_id == run_id` (R1 — one contract per run); the seam passes
`run_id=run_id` explicitly so the run_id mirror column is ALWAYS populated (NON-NULL, = run_id) for
correlation (X3), and request_id optionally rides as an additional correlation mirror (consumed by
the get_contract read model, §13). New streams open at expected_version=0; every FC event type MUST
be registered (register_sp2_event_types) first."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.aggregates._append import append
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.store import load_stream


def append_feature_contract_event(
    conn: DbConn,
    *,
    run_id: str,
    type: str,
    payload: Mapping[str, Any],
    actor: IdentityEnvelope,
    request_id: str | None = None,
    provenance: ProvenanceEnvelope | None = None,
    expected_version: int | None = None,
    caused_by: str | None = None,
) -> EventEnvelope:
    """Append one feature_contract event via the SP-0 OCC helper (R1 — mirrors SP-1's
    append_overlay_event). One contract per run, so the seam sets
    `aggregate_id == feature_contract_id == run_id` and passes `run_id=run_id` explicitly — the
    run_id mirror column is ALWAYS populated (NON-NULL) for correlation (X3); `request_id` optionally
    rides as an additional correlation mirror (consumed by the get_contract read model, §13). Raises
    ConcurrencyError if the stream is not exactly at `expected_version` (callers that fold FC state to
    decide MUST pass the folded head_version — X4), and SchemaValidationError if `type` is
    unregistered or the payload fails its schema (fail-closed, before any INSERT)."""
    return append(
        conn,
        aggregate="feature_contract",
        aggregate_id=run_id,
        feature_contract_id=run_id,
        type=type,
        payload=payload,
        actor=actor,
        run_id=run_id,
        request_id=request_id,
        provenance=provenance,
        expected_version=expected_version,
        caused_by=caused_by,
    )


def load_feature_contract(conn: DbConn, run_id: str) -> list[EventEnvelope]:
    """Load the full feature_contract event stream (stream_version ASC) — the input to
    fold_feature_contract_state (P8). `feature_contract_id == run_id` (R1)."""
    return load_stream(conn, "feature_contract", run_id)
