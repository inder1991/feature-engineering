from __future__ import annotations

from typing import Optional

from psycopg.types.json import Jsonb

from sp0.contracts import (
    DbConn,
    EventEnvelope,
    IdentityEnvelope,
    NewEvent,
    ProjectionApplyError,
    ProvenanceEnvelope,
)
from sp0.events.registry import event_registry

PRIMARY_SELECTED = "PRIMARY_SELECTED"
PRIMARY_SELECTED_SCHEMA_VERSION = 1
PRIMARY_SELECTED_JSON_SCHEMA = {
    "type": "object",
    "required": ["doc_id", "stage"],
    "properties": {
        "doc_id": {"type": "string"},
        "stage": {"type": "string"},
    },
    "additionalProperties": False,
}


def register_primary_selected(conn: DbConn) -> None:
    """Register PRIMARY_SELECTED in the event registry so append_event validation
    passes, and ensure the StagePrimaryProjection checkpoint row exists so the Phase
    01 `run_projection` runner can consume it (§3.6). Both inserts are idempotent."""
    conn.execute(
        """
        INSERT INTO event_type_registry
            (type_name, schema_version, json_schema, owner, status)
        VALUES (%s, %s, %s, 'sp0', 'active')
        ON CONFLICT (type_name, schema_version) DO NOTHING
        """,
        (PRIMARY_SELECTED, PRIMARY_SELECTED_SCHEMA_VERSION,
         Jsonb(PRIMARY_SELECTED_JSON_SCHEMA)),
    )
    # append_event validates against the in-memory event_registry() singleton (the DB
    # event_type_registry table is the durable record); register there too so writes pass.
    event_registry().register_schema(
        PRIMARY_SELECTED,
        PRIMARY_SELECTED_SCHEMA_VERSION,
        PRIMARY_SELECTED_JSON_SCHEMA,
        owner="sp0",
        status="active",
    )
    # Phase 02 owns creation of its own checkpoint row (idempotent). This makes the
    # projection-runner path self-sufficient regardless of whether Phase 01's
    # run_projection self-initializes a missing checkpoint row (see Prerequisites).
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) "
        "VALUES ('stage_primary') ON CONFLICT (projection_name) DO NOTHING"
    )


def new_primary_selected(
    *,
    run_id: str,
    stage: str,
    doc_id: str,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
    caused_by: Optional[str] = None,
) -> NewEvent:
    """Canonical PRIMARY_SELECTED builder (§3.4). Promotion is an event, never an in-place flip."""
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type=PRIMARY_SELECTED,
        schema_version=PRIMARY_SELECTED_SCHEMA_VERSION,
        payload={"doc_id": doc_id, "stage": stage},
        actor=actor,
        provenance=provenance,
        run_id=run_id,
        caused_by=caused_by,
    )


class StagePrimaryProjection:
    """Fail-closed projection of PRIMARY_SELECTED into stage_primary (§3.4)."""

    name = "stage_primary"
    is_analytics = False

    def apply(self, conn: DbConn, event: EventEnvelope) -> None:
        if event.type != PRIMARY_SELECTED:
            return
        run_id = event.run_id
        doc_id = event.payload["doc_id"]
        stage = event.payload["stage"]
        known = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id=%s AND run_id=%s AND stage=%s",
            (doc_id, run_id, stage),
        ).fetchone()
        if known is None:
            raise ProjectionApplyError(
                "run", run_id or "",
                f"PRIMARY_SELECTED references unknown doc {doc_id} for ({run_id},{stage})",
            )
        conn.execute(
            """
            INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, stage) DO UPDATE
                SET doc_id = EXCLUDED.doc_id,
                    selected_seq = EXCLUDED.selected_seq,
                    selected_at = now()
                WHERE stage_primary.selected_seq < EXCLUDED.selected_seq
            """,
            (run_id, stage, doc_id, event.global_seq),
        )

    def reset(self, conn: DbConn) -> None:
        conn.execute("TRUNCATE stage_primary")


def current_primary(conn: DbConn, run_id: str, stage: str) -> Optional[str]:
    """The live primary doc_id for (run_id, stage), or None (§3.4)."""
    row = conn.execute(
        "SELECT doc_id FROM stage_primary WHERE run_id=%s AND stage=%s",
        (run_id, stage),
    ).fetchone()
    return row[0] if row else None
