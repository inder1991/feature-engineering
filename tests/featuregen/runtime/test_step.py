from __future__ import annotations

import pytest

from featuregen.contracts import (
    ConcurrencyError,
    Disposition,
    HandlerContext,
    HandlerResult,
    NewActivation,
    NewDocument,
    NewEvent,
    NewExternalCommand,
    NewTimer,
)
from datetime import datetime, timezone
from featuregen.documents.store import DocumentValidationError
from featuregen.runtime.step import commit_step


def _next_event(ctx, actor, prov, *, type="STEP_DONE", payload=None) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=ctx.run_id,
        run_id=ctx.run_id,
        type=type,
        schema_version=1,
        payload=payload or {},
        actor=actor,
        provenance=prov,
    )


def _ctx(db, trigger) -> HandlerContext:
    return HandlerContext(
        run_id=trigger.run_id, triggering_event=trigger, documents={}, read_conn=db
    )


def test_commit_step_appends_event_outbox_and_ledger(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s1", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert len(sc.appended_event_ids) == 1
    assert sc.document_id is None
    # one outbox row per appended event
    with db.cursor() as cur:
        cur.execute("SELECT topic FROM outbox WHERE message_id = %s", (sc.appended_event_ids[0],))
        assert cur.fetchone()[0] == "STEP_DONE"
        cur.execute("SELECT processed_seq FROM processed_messages WHERE message_id = %s",
                    (trigger.event_id,))
        assert cur.fetchone()[0] == sc.processed_seq


def test_commit_step_inserts_document(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s2", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    doc = NewDocument(
        doc_id=ctx.new_doc_id(),
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:abc",
        body_classification="governance-retained",
        provenance=prov,
    )
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        document=doc,
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert sc.document_id is not None
    with db.cursor() as cur:
        cur.execute("SELECT stage, run_id, branch_role FROM documents WHERE doc_id = %s",
                    (sc.document_id,))
        stage, run_id, role = cur.fetchone()
    assert (stage, run_id, role) == ("CANDIDATE_SQL", "run_s2", "candidate")


def test_commit_step_chains_occ_for_multiple_events(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s3", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(
            _next_event(ctx, actor, prov, type="STEP_DONE"),
            _next_event(ctx, actor, prov, type="STEP_NEXT"),
        ),
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert len(sc.appended_event_ids) == 2
    with db.cursor() as cur:
        cur.execute(
            "SELECT stream_version FROM events WHERE run_id='run_s3' ORDER BY stream_version"
        )
        versions = [r[0] for r in cur.fetchall()]
    assert versions == [1, 2, 3]  # trigger=1, then 2, 3


def test_commit_step_persists_timers_and_external_commands_atomically(
    db, seed_run_event, actor, prov
) -> None:
    """§5.1/§5.4/§5.5: a handler that returns BOTH a NewTimer and a NewExternalCommand has
    each persisted in the SAME step transaction as its events (replacing the old Phase-04
    guard that raised)."""
    trigger = seed_run_event("run_s4", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        timers=(NewTimer(kind="sla", fire_at=datetime.now(timezone.utc), idempotency_key="t1"),),
        external_commands=(
            NewExternalCommand(
                integration="metadata_write",
                idempotency_key="x1",
                request_payload={"k": "v"},
                expected_run_id="run_s4",
            ),
        ),
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert len(sc.appended_event_ids) == 1
    assert len(sc.timer_ids) == 1
    assert len(sc.external_command_ids) == 1
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, kind FROM timers WHERE timer_id = %s", (sc.timer_ids[0],)
        )
        assert cur.fetchone() == ("scheduled", "sla")
        cur.execute(
            "SELECT status, integration FROM external_commands WHERE command_id = %s",
            (sc.external_command_ids[0],),
        )
        assert cur.fetchone() == ("pending", "metadata_write")


def test_commit_step_rolls_back_timers_and_external_commands_on_abort(
    db, seed_run_event, actor, prov
) -> None:
    """The timer + external-command rows live in the caller's step transaction, so an abort
    (e.g. an OCC rollback in process_one's per-step savepoint) discards them together with the
    step's events — no orphan timer or pending side effect survives a rolled-back step."""
    trigger = seed_run_event("run_s4r", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        timers=(NewTimer(kind="sla", fire_at=datetime.now(timezone.utc), idempotency_key="t2"),),
        external_commands=(
            NewExternalCommand(
                integration="metadata_write",
                idempotency_key="x2",
                request_payload={"k": "v"},
            ),
        ),
    )

    class _Abort(Exception):
        pass

    with pytest.raises(_Abort):
        with db.transaction():  # mirrors process_one's per-step savepoint
            commit_step(
                db, ctx, result,
                message_id=trigger.event_id,
                expected_version=trigger.stream_version,
                table_version=trigger.table_version,
            )
            raise _Abort  # force the whole step tx to roll back AFTER the writes

    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM timers WHERE idempotency_key = 't2'")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM external_commands WHERE idempotency_key = 'x2'")
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id='run_s4r' AND type='STEP_DONE'"
        )
        assert cur.fetchone()[0] == 0


def test_commit_step_applies_activations(db, seed_run_event, actor, prov) -> None:
    # Phase 06 (§5.8): commit_step now APPLIES each declared NewActivation on the step-tx conn
    # (replacing the Phase-04 deferral guard) via apply_activation — atomic with the rest of the
    # step. The active-map CAS + VERSION_ACTIVATED event land in the same transaction.
    from featuregen.events.registry import event_registry
    from featuregen.aggregates.events import register_phase06_event_types
    from featuregen.aggregates.feature_versions import mint_feature_version

    register_phase06_event_types(event_registry())
    fv = mint_feature_version(
        db, feature_id="feat_step", produced_by_run="run_s4b",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="low", approval_type="PRODUCTION",
        approved_use_cases=("fraud",), blocked_use_cases=(), required_artifact_refs={},
        content_hash="sha256:step", actor=actor, provenance=prov)
    trigger = seed_run_event("run_s4b", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        activations=(
            NewActivation(
                feature_id="feat_step",
                feature_version_id=fv,
                use_case="fraud",
                base_feature_version_id=None,
                approval_type="PRODUCTION",
            ),
        ),
    )
    commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_step' AND use_case='fraud'").fetchone()
    assert row[0] == fv


def test_commit_step_stale_expected_version_raises_concurrency(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s5", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK, new_events=(_next_event(ctx, actor, prov),)
    )
    # expected_version 0 is stale (stream is already at version 1)
    with pytest.raises(ConcurrencyError):
        commit_step(
            db, ctx, result,
            message_id=trigger.event_id,
            expected_version=0,
            table_version=trigger.table_version,
        )


def test_commit_step_rolls_back_all_writes_when_document_insert_fails(
    db, seed_run_event, actor, prov
) -> None:
    """Atomicity / no-orphan invariant (§5.1): commit_step creates the document FIRST via the
    validated append_document path, so a rejected document (branch_role='rejected' with no
    reject_reason -> DocumentValidationError) aborts the WHOLE step before any event is
    appended; the per-step savepoint (as `process_one` uses it) must leave NO event, outbox row,
    or ledger row behind."""
    trigger = seed_run_event("run_atomic", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    bad_doc = NewDocument(
        doc_id=ctx.new_doc_id(),
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="rejected",          # append_document requires a reject_reason
        content_hash="sha256:abc",
        body_classification="governance-retained",
        provenance=prov,
        reject_reason=None,              # -> DocumentValidationError (DB CHECK is the backstop)
    )
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        document=bad_doc,
    )
    with pytest.raises(DocumentValidationError):
        with db.transaction():           # mirrors process_one's per-step savepoint
            commit_step(
                db, ctx, result,
                message_id=trigger.event_id,
                expected_version=trigger.stream_version,
                table_version=trigger.table_version,
            )
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id='run_atomic' AND type='STEP_DONE'"
        )
        assert cur.fetchone()[0] == 0    # no event written (document rejected first)
        cur.execute("SELECT count(*) FROM outbox WHERE partition_key='run:run_atomic'")
        assert cur.fetchone()[0] == 0    # no orphan outbox row
        cur.execute(
            "SELECT count(*) FROM processed_messages WHERE message_id=%s", (trigger.event_id,)
        )
        assert cur.fetchone()[0] == 0    # no ledger row
