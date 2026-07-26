from __future__ import annotations

import psycopg
import pytest

from featuregen.formula.control import (
    LeaseFence,
    LeaseFenceLost,
    RecoveryRequiresReconciliation,
)
from featuregen.formula.replay_trace import (
    append_event,
    load_verified_checkpoint,
    open_authoring_run,
    run_status,
)
from featuregen.runtime.queue import enqueue_checked


def test_trace_is_ordered_terminal_and_write_once(db) -> None:
    run_id = open_authoring_run(
        db,
        intent_hash="ih",
        versions={"orchestrator": 1},
        actor={"subject": "user:test"},
    )
    assert run_status(db, run_id) == "incomplete"
    append_event(
        db,
        run_id,
        "validation_result",
        seq=0,
        idempotency_key=f"{run_id}:0",
        payload={"status": "ok"},
    )
    append_event(
        db,
        run_id,
        "completed",
        seq=1,
        idempotency_key=f"{run_id}:terminal",
        payload={"disposition": "RESOLVED"},
    )
    assert run_status(db, run_id) == "completed"

    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        append_event(
            db,
            run_id,
            "validation_result",
            seq=2,
            idempotency_key=f"{run_id}:2",
            payload={},
        )
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute(
            "UPDATE formula_authoring_run SET intent_hash = 'tampered' "
            "WHERE authoring_run_id = %s",
            (run_id,),
        )


def test_identical_event_retry_is_idempotent_but_changed_payload_is_rejected(db) -> None:
    run_id = open_authoring_run(
        db, intent_hash="ih", versions={"orchestrator": 1}, actor=None)
    kwargs = {
        "seq": 0,
        "idempotency_key": f"{run_id}:author:0",
        "payload": {"output_hash": "abc"},
    }
    append_event(db, run_id, "author_turn", **kwargs)
    append_event(db, run_id, "author_turn", **kwargs)
    assert db.execute(
        "SELECT count(*) FROM formula_authoring_trace_event WHERE authoring_run_id = %s",
        (run_id,),
    ).fetchone()[0] == 1
    with pytest.raises(ValueError, match="idempotency conflict"):
        append_event(
            db,
            run_id,
            "author_turn",
            seq=0,
            idempotency_key=f"{run_id}:author:0",
            payload={"output_hash": "different"},
        )


def test_exact_terminal_retry_is_idempotent_but_changed_terminal_is_rejected(db) -> None:
    run_id = open_authoring_run(
        db, intent_hash="ih", versions={"orchestrator": 1}, actor=None)
    values = {
        "seq": 0,
        "idempotency_key": f"{run_id}:terminal",
        "payload": {"disposition": "TECHNICAL_FAILURE"},
        "stage": "TERMINAL",
    }
    append_event(db, run_id, "failed", **values)
    append_event(db, run_id, "failed", **values)
    assert db.execute(
        "SELECT count(*) FROM formula_authoring_trace_event WHERE authoring_run_id=%s",
        (run_id,),
    ).fetchone()[0] == 1
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        append_event(
            db,
            run_id,
            "failed",
            **{**values, "payload": {"disposition": "RESOLVED"}},
        )


def test_expired_worker_fence_cannot_open_run_or_append_trace(db) -> None:
    queue_id = enqueue_checked(
        db,
        message_id="trace-fence",
        partition_key="trace-fence",
        handler="recipe_formula_shadow.author.v1",
        payload={"work_item_id": "work"},
    )
    db.execute(
        "UPDATE queue SET status='leased',lease_owner='worker-a',lease_fence=7,"
        "lease_expires_at=now() + interval '10 minutes' WHERE id=%s",
        (queue_id,),
    )
    fence = LeaseFence(queue_id, "worker-a", 7)
    run_id = open_authoring_run(
        db,
        intent_hash="ih",
        versions={"orchestrator": 1},
        actor=None,
        lease_fence=fence,
    )
    append_event(
        db,
        run_id,
        "validation_result",
        seq=0,
        idempotency_key=f"{run_id}:validation",
        payload={"status": "ok"},
        stage="EXPECTATION_VALIDATED",
        lease_fence=fence,
    )
    db.execute(
        "UPDATE queue SET lease_expires_at=now() - interval '1 second' WHERE id=%s",
        (queue_id,),
    )
    with pytest.raises(LeaseFenceLost):
        append_event(
            db,
            run_id,
            "completed",
            seq=1,
            idempotency_key=f"{run_id}:terminal",
            payload={"status": "done"},
            stage="TERMINAL",
            lease_fence=fence,
        )
    with pytest.raises(LeaseFenceLost):
        open_authoring_run(
            db,
            intent_hash="other",
            versions={"orchestrator": 1},
            actor=None,
            authoring_run_id="far_expired",
            lease_fence=fence,
        )


def test_checkpoint_rejects_noncontiguous_or_hash_invalid_trace(db) -> None:
    run_id = open_authoring_run(
        db, intent_hash="ih", versions={"orchestrator": 1}, actor=None)
    db.execute(
        "INSERT INTO formula_authoring_trace_event "
        "(authoring_run_id,seq,kind,idempotency_key,payload,stage,payload_hash) "
        "VALUES (%s,1,'failed',%s,'{}'::jsonb,'TERMINAL','wrong-hash')",
        (run_id, f"{run_id}:terminal"),
    )
    with pytest.raises(RecoveryRequiresReconciliation):
        load_verified_checkpoint(
            db,
            run_id,
            intent_hash="ih",
            versions={"orchestrator": 1},
        )


def test_checkpoint_rejects_unreconciled_predispatch_record(db) -> None:
    run_id = open_authoring_run(
        db, intent_hash="ih", versions={"orchestrator": 1}, actor=None)
    db.execute(
        "INSERT INTO llm_dispatch "
        "(dispatch_ref,logical_call_ref,attempt_no,stage,task,input_hash,redacted_input,"
        "authoring_run_id,physical_request_hash,canonical_turn_input_hash,"
        "provider_contract_hash,prompt_content_hash,schema_content_hash) "
        "VALUES ('disp_unreconciled','logical-unreconciled',1,'formula','formula.author',"
        "'input','{}'::jsonb,%s,'physical','canonical','contract','prompt','schema')",
        (run_id,),
    )
    with pytest.raises(
        RecoveryRequiresReconciliation, match="ambiguous provider dispatch"
    ):
        load_verified_checkpoint(
            db,
            run_id,
            intent_hash="ih",
            versions={"orchestrator": 1},
        )
