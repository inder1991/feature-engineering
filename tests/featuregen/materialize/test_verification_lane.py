"""§9.0 — the sandbox verification worker, driven for real against the queue and the lifecycle.

▲ The case worth reading first is `test_THE_MISSING_SUBSTRATE_IS_A_POSTURE_not_a_verdict`: a
deployment that cannot execute must say so as a PLATFORM fact, never as a claim about the artifact
— and never as a quiet pass.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.action_authorization import ActionV1, authorize_action
from featuregen.materialize.action_decision import ActionRequestV1, decide
from featuregen.materialize.verification_lane import (
    SandboxExecutionV1,
    enqueue_verification,
    process_verification_once,
    reconcile_abandoned_verifications,
    verification_evidence_pins,
)
from featuregen.overlay.upload.verification_request_store import (
    VerificationStatusV1,
    advance_verification,
    request_verification,
)

ENV = "hdfc-local"
ARTIFACT = "art-vl-1"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_VERIFICATION_V2_ENABLED", "1")


def _decided(db, request_id: str, artifact: str = ARTIFACT) -> str:
    authorization = authorize_action(
        db, action=ActionV1.EXECUTE_SANDBOX, resource_identity_hash=artifact,
        actor_subject="user:sam", environment_id=ENV)
    decision_id, _ = decide(
        db, ActionRequestV1(
            action=ActionV1.EXECUTE_SANDBOX, resource_identity_hash=artifact,
            evidence_pins=verification_evidence_pins(
                sealed_artifact_id=artifact, environment_id=ENV)),
        authorization_id=authorization.authorization_id)
    db.execute("UPDATE verification_request SET action_decision_revision_id = %s "
               "WHERE request_id = %s", (decision_id, request_id))
    return decision_id


def _requested(db, *, request_id="vfr-1", artifact=ARTIFACT, decided=True) -> str:
    rid, created = request_verification(
        db, request_id=request_id, sealed_artifact_id=artifact, environment_id=ENV,
        requested_by="user:sam", requested_at="t")
    assert created
    if decided:
        _decided(db, rid, artifact)
    enqueue_verification(db, request_id=rid, sealed_artifact_id=artifact, environment_id=ENV)
    return rid


def _status(db, request_id):
    return db.execute("SELECT status, failure_reason, findings FROM verification_request "
                      "WHERE request_id = %s", (request_id,)).fetchone()


def _inject(monkeypatch, executor):
    import featuregen.materialize.verification_lane as lane

    monkeypatch.setattr(lane, "_EXECUTOR", executor)


# ══ THE POSTURE ════════════════════════════════════════════════════════════════════════════════
def test_THE_MISSING_SUBSTRATE_IS_A_POSTURE_not_a_verdict(db):
    """▲ The shipped executor is None — step 0b's substrate is not configured. FAILED (platform),
    never REFUSED (product): absence of a cluster says nothing about the artifact. And never a
    quiet pass, which is how "no execution seam" becomes green evidence."""
    rid = _requested(db)

    outcome = process_verification_once(db, owner="w1")

    assert outcome.status == "failed"
    status, reason, _findings = _status(db, rid)
    assert status == "FAILED"
    assert "substrate" in reason and "posture" in reason


# ══ THE MEASUREMENT ════════════════════════════════════════════════════════════════════════════
def test_A_PASSING_RUN_RECORDS_THE_OUTPUT_REVISION_and_stops_before_publication(db, monkeypatch):
    """▲ Publication binds to a THING: the measured output, content-addressed. And the worker STOPS
    there — publication is a separate act with its own decision, and a worker that published what
    it verified would be the conflation §2 names."""
    rid = _requested(db)
    _inject(monkeypatch, lambda conn, **kw: SandboxExecutionV1(
        output_manifest_hash="sha256:rows", row_count=42, findings=(), passed=True))

    outcome = process_verification_once(db, owner="w1")

    assert outcome.status == "passed"
    status, _reason, _f = _status(db, rid)
    assert status == "PASSED"
    output = db.execute(
        "SELECT output_manifest_hash, row_count FROM sandbox_output_revision "
        "WHERE request_id = %s", (rid,)).fetchone()
    assert output == ("sha256:rows", 42)
    # Nothing published: the publications surface is untouched by this lane.
    assert db.execute("SELECT count(*) FROM queue WHERE handler LIKE '%publish%'").fetchone()[0] == 0


def test_A_REFUSING_RUN_NAMES_ITS_FINDINGS(db, monkeypatch):
    """REFUSED is a PRODUCT verdict and 1094's CHECK requires it to carry findings — a refusal that
    names nothing is not actionable."""
    rid = _requested(db)
    _inject(monkeypatch, lambda conn, **kw: SandboxExecutionV1(
        output_manifest_hash="sha256:rows", row_count=0,
        findings=({"code": "ROW_COUNT_MISMATCH", "detail": "expected one row per customer"},),
        passed=False))

    outcome = process_verification_once(db, owner="w1")

    assert outcome.status == "refused"
    status, _reason, findings = _status(db, rid)
    assert status == "REFUSED"
    assert findings[0]["code"] == "ROW_COUNT_MISMATCH"


def test_AN_EXECUTOR_CRASH_IS_FAILED_with_the_error_recorded(db, monkeypatch):
    def _boom(conn, **kw):
        raise RuntimeError("thrift connection dropped mid-run")

    rid = _requested(db)
    _inject(monkeypatch, _boom)

    assert process_verification_once(db, owner="w1").status == "failed"
    status, reason, _f = _status(db, rid)
    assert status == "FAILED" and "thrift" in reason


# ══ THE SECOND LOOK ════════════════════════════════════════════════════════════════════════════
def test_A_REQUEST_WITH_NO_DECISION_REFUSES_AS_A_BYPASS(db, monkeypatch):
    """The same §8.2 gate as generation: work submitted straight to the queue was never answered
    about, so it refuses at the worker — REFUSED with the code named, not FAILED."""
    rid = _requested(db, decided=False)
    _inject(monkeypatch, lambda conn, **kw: SandboxExecutionV1(
        output_manifest_hash="x", row_count=1, findings=(), passed=True))

    outcome = process_verification_once(db, owner="w1")

    assert outcome.status == "refused"
    status, _reason, findings = _status(db, rid)
    assert status == "REFUSED"
    assert findings[0]["code"] == "ACTION_DECISION_MISSING"


# ══ REDELIVERY AND THE RECONCILER ══════════════════════════════════════════════════════════════
def test_A_REDELIVERY_OF_FINISHED_WORK_COMPLETES_without_a_second_run(db, monkeypatch):
    calls = []
    rid = _requested(db)
    _inject(monkeypatch, lambda conn, **kw: calls.append(1) or SandboxExecutionV1(
        output_manifest_hash="sha256:rows", row_count=1, findings=(), passed=True))

    assert process_verification_once(db, owner="w1").status == "passed"
    # Redeliver the same message: back to ready, claimed again.
    db.execute("UPDATE queue SET status='ready' WHERE message_id = %s",
               (f"verification:{rid}",))

    outcome = process_verification_once(db, owner="w2")

    assert outcome.status == "unclaimable"
    assert "already PASSED" in outcome.detail
    assert len(calls) == 1, "a recorded verdict is never bought twice"


def test_AN_ABANDONED_REQUEST_IS_RECONCILED_but_a_released_one_is_left_alone(db):
    """§9.0.1: the predicate is an UNREACHABLE message, never merely an unleased one — a request
    awaiting redelivery is byte-for-byte identical on the weaker test, and terminalizing it makes
    the redelivery report "already done" for a run that never happened."""
    dead = _requested(db, request_id="vfr-dead", artifact="art-vl-dead")
    advance_verification(db, dead, VerificationStatusV1.CLAIMED)
    db.execute("UPDATE queue SET status='dead' WHERE message_id = %s",
               (f"verification:{dead}",))

    released = _requested(db, request_id="vfr-released", artifact="art-vl-rel")
    advance_verification(db, released, VerificationStatusV1.CLAIMED)
    db.execute("UPDATE queue SET status='ready' WHERE message_id = %s",
               (f"verification:{released}",))

    judged = reconcile_abandoned_verifications(db)

    assert judged == (dead,)
    assert _status(db, dead)[0] == "FAILED"
    assert _status(db, released)[0] == "CLAIMED"


# ══ THE READ PATH — v2-first, found orphaned by the run-spine session's mapping ═════════════════
def test_THE_GET_READS_THE_V2_REQUEST_the_lane_actually_writes(db):
    """§9.0's rewrite left GET /verifications selecting `verification_attempt` — a table the v2
    lane never writes — so the workspace's poll 404'd on every v2 request for ever. The read is
    now v2-first: the request row, server-owned stage words, and the SANDBOX OUTPUT REVISION
    under the key the screen renders, with the v1 mechanics as honest nulls."""
    from featuregen.api.routes.feature_execution import verification_result

    rid = _requested(db, request_id="vfr-read", artifact="art-vl-read")
    body = verification_result(rid, db)
    assert body["request_id"] == rid
    assert body["status"] == "REQUESTED"
    assert body["stage_label"] == "Queued — the durable worker will execute it"
    assert (body["attempt"], body["staging_path"], body["verified_output"]) == (None, None, None)

    advance_verification(db, rid, VerificationStatusV1.CLAIMED)
    advance_verification(db, rid, VerificationStatusV1.RUNNING)
    db.execute(
        "INSERT INTO sandbox_output_revision (output_revision_id, request_id, "
        "sealed_artifact_id, environment_id, output_manifest_hash, row_count) "
        "VALUES ('sor-read', %s, 'art-vl-read', %s, 'sha256:m', 42)", (rid, ENV))
    advance_verification(db, rid, VerificationStatusV1.PASSED, execution_hash="sor-read")
    body = verification_result(rid, db)
    assert body["terminal"] is True
    assert body["verified_output"] == {
        "revision_id": "sor-read", "output_manifest_hash": "sha256:m", "row_count": 42}


def test_PUBLISH_SANDBOX_finally_SEES_a_v2_verification(db):
    """`evaluate_publish_sandbox` joined only the dead v1 tables, so a v2-lane verification could
    never satisfy publication. The v2 branch: a PASSED request's content-addressed output IS the
    exact output — no staging path exists or is needed — and a non-PASSED request still refuses."""
    from featuregen.materialize.evaluate_execution import evaluate_publish_sandbox
    from featuregen.overlay.upload import semantic_eligibility_reasons as R
    from featuregen.overlay.upload.verification_store import StalenessV1

    rid = _requested(db, request_id="vfr-pub", artifact="art-vl-pub")
    advance_verification(db, rid, VerificationStatusV1.CLAIMED)
    advance_verification(db, rid, VerificationStatusV1.RUNNING)
    db.execute(
        "INSERT INTO sandbox_output_revision (output_revision_id, request_id, "
        "sealed_artifact_id, environment_id, output_manifest_hash, row_count) "
        "VALUES ('sor-pub', %s, 'art-vl-pub', %s, 'sha256:m', 5)", (rid, ENV))
    advance_verification(db, rid, VerificationStatusV1.PASSED, execution_hash="sor-pub")

    verdict = evaluate_publish_sandbox(
        db, verified_output_revision_id="sor-pub", staging_path=None,
        staleness=StalenessV1.NEITHER, publication_permitted=True,
        capability_attestation="cap-1", activation_blockers=())
    assert R.VERIFICATION_NOT_CURRENT not in verdict.blockers

    # A NON-passed v2 request's output must still refuse — PASSED is the currency.
    rid2 = _requested(db, request_id="vfr-pub2", artifact="art-vl-pub2")
    advance_verification(db, rid2, VerificationStatusV1.CLAIMED)
    advance_verification(db, rid2, VerificationStatusV1.RUNNING)
    advance_verification(db, rid2, VerificationStatusV1.FAILED, failure_reason="boom")
    db.execute(
        "INSERT INTO sandbox_output_revision (output_revision_id, request_id, "
        "sealed_artifact_id, environment_id, output_manifest_hash, row_count) "
        "VALUES ('sor-pub2', %s, 'art-vl-pub2', %s, 'sha256:m2', 5)", (rid2, ENV))
    verdict = evaluate_publish_sandbox(
        db, verified_output_revision_id="sor-pub2", staging_path=None,
        staleness=StalenessV1.NEITHER, publication_permitted=True,
        capability_attestation="cap-1", activation_blockers=())
    assert R.VERIFICATION_NOT_CURRENT in verdict.blockers

    # And a LEGACY id with no staging path keeps the old hard refusal — nothing loosened.
    with pytest.raises(ValueError, match="no staging path"):
        evaluate_publish_sandbox(
            db, verified_output_revision_id="vor-legacy", staging_path=None,
            staleness=StalenessV1.NEITHER, publication_permitted=True,
            capability_attestation="cap-1", activation_blockers=())
