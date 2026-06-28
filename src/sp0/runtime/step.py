from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from uuid import uuid4

import psycopg

from sp0.contracts import HandlerContext, HandlerResult, NewDocument, NewEvent
from sp0.documents.store import append_document
from sp0.events.store import append_event
from sp0.runtime.ledger import record_processed
from sp0.runtime.outbox import insert_outbox_message, outbox_messages_for_events


@dataclass(frozen=True, slots=True)
class StepCommit:
    """Outcome of one atomic step (§5.1)."""
    appended_event_ids: tuple[str, ...]
    document_id: str | None
    outbox_message_ids: tuple[str, ...]
    processed_seq: int


def gen_id(prefix: str) -> str:
    """Prefixed unique id (ULID-style slot; uuid4 hex is a fine stand-in)."""
    return f"{prefix}_{uuid4().hex}"


def _insert_document(conn: psycopg.Connection, ctx: HandlerContext, doc: NewDocument) -> str:
    """Create the frozen document through Phase 02's VALIDATED append_document (§5.1) — NOT a
    raw INSERT — so DAG/acyclicity/supersedes/derived_from/schema-lifecycle/blob + structural
    validation all run inside the step tx. The id is the handler-supplied doc.doc_id (minted via
    ctx.new_doc_id()); append_document uses it and returns it."""
    te = ctx.triggering_event
    return append_document(
        conn,
        doc,
        run_id=ctx.run_id,
        feature_id=te.feature_id,
        request_id=te.request_id,
        actor=te.actor,
    )


def _validate_event_doc_refs(
    conn: psycopg.Connection,
    events: Iterable[NewEvent],
    *,
    document_id: str | None,
) -> None:
    """Any new-document reference an event payload carries (payload['document_id']) MUST resolve
    to the document this step just created (document_id) or to an already-committed document
    (§5.1). Runs AFTER the document INSERT (so the supplied doc_id is visible) and BEFORE
    appending events, so an event can never be committed referencing a doc that does not exist."""
    for new_event in events:
        ref = new_event.payload.get("document_id")
        if ref is None or ref == document_id:
            continue
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM documents WHERE doc_id = %s", (ref,))
            if cur.fetchone() is None:
                raise ValueError(
                    f"event references new document_id {ref!r} that is neither this step's "
                    f"new document nor an already-committed document"
                )


def commit_step(
    conn: psycopg.Connection,
    ctx: HandlerContext,
    result: HandlerResult,
    *,
    message_id: str,
    expected_version: int,
    table_version: int,
) -> StepCommit:
    """The §5.1 atomic boundary, inside the caller's open tx: create one frozen document FIRST
    (via Phase 02's validated append_document, so its DAG/structural checks run and any emitted
    event can reference it), validate the events' new-document references, append the events
    (chained OCC), write one outbox row per event, and record the ledger row."""
    if result.timers or result.external_commands:
        raise RuntimeError(
            "commit_step: timers/external_commands persistence is added by Phase 05 "
            "(§5.4/§5.5); not supported in Phase 04"
        )
    if result.activations:
        raise RuntimeError(
            "commit_step: cross-aggregate activations are applied by Phase 06's commit-path "
            "extension (§6); not supported in Phase 04"
        )

    te = ctx.triggering_event

    # Document FIRST: append_document runs DAG/acyclicity/supersedes/derived_from/
    # schema-lifecycle/blob + structural validation inside this tx, and lets the emitted events
    # reference the doc by its handler-supplied id.
    document_id = (
        _insert_document(conn, ctx, result.document)
        if result.document is not None
        else None
    )
    _validate_event_doc_refs(conn, result.new_events, document_id=document_id)

    version = expected_version
    appended = []
    for new_event in result.new_events:
        env = append_event(
            conn, new_event, expected_version=version, table_version=table_version
        )
        appended.append(env)
        version = env.stream_version

    outbox_ids: list[str] = []
    for msg in outbox_messages_for_events(appended):
        insert_outbox_message(conn, msg)
        outbox_ids.append(msg.message_id)

    if appended:
        processed_seq = max(env.global_seq for env in appended)
        result_event_id: str | None = appended[-1].event_id
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT last_value FROM global_seq_seq")
            processed_seq = int(cur.fetchone()[0])
        result_event_id = None

    record_processed(
        conn,
        message_id=message_id,
        aggregate=te.aggregate,
        aggregate_id=te.aggregate_id,
        result_event_id=result_event_id,
        processed_seq=processed_seq,
    )

    return StepCommit(
        appended_event_ids=tuple(env.event_id for env in appended),
        document_id=document_id,
        outbox_message_ids=tuple(outbox_ids),
        processed_seq=processed_seq,
    )
