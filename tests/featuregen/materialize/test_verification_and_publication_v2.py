"""Steps 13 and 14 — the verification lifecycle, and the gate that reads it.

`verification_attempt` (1080) records what an attempt DID and has no status, so there is no state in
which a verification has been ASKED FOR and not yet run, and no way to tell "still running" from
"the worker died" from "nothing consumes this table". `generation_request` says exactly that in its
own comment; step 13 is the same lifecycle over verification, and step 14 is the door that asks it.

What these tests hold:

1. **PASSED is EARNED**, never declared: a request cannot jump to it without going through RUNNING.
2. **REFUSED and FAILED are different answers.** One is the feature's problem, one is the
   platform's, and collapsing them sends an operator to change a correct formula.
3. **Publication is keyed on the SEALED ARTIFACT**, so a re-rendered artifact cannot inherit the
   previous one's green tick.
4. **"Still running" is not a weaker no.** The gate refuses it exactly as hard as "never asked",
   while the message still says which it was.
"""
from __future__ import annotations

import pytest

from featuregen.materialize.authorize_publication_v2 import (
    PublicationEvidenceV2,
    authorize_publication_v2,
)
from featuregen.materialize.codes import MaterializationRefused, PublicationRefusalCode
from featuregen.overlay.upload.verification_request_store import (
    InvalidVerificationMove,
    VerificationStatusV1,
    advance_verification,
    current_verification,
    request_verification,
)

ART = "art-pilot-1"
ENV = "hdfc-local"


def _request(db, request_id="vr-1", artifact=ART, environment=ENV):
    return request_verification(
        db, request_id=request_id, sealed_artifact_id=artifact, environment_id=environment,
        requested_by="user:ops", requested_at="2026-08-19T00:00:00Z")


def _pass(db, request_id="vr-1", execution_hash="exec-1"):
    """Take a request all the way to PASSED, one step at a time — the only way there."""
    for step in (VerificationStatusV1.CLAIMED, VerificationStatusV1.RUNNING):
        advance_verification(db, request_id, step)
    return advance_verification(
        db, request_id, VerificationStatusV1.PASSED, execution_hash=execution_hash)


# ══ THE LIFECYCLE ══════════════════════════════════════════════════════════════════════════════
def test_a_verification_is_REQUESTED_before_it_runs(db):
    """The state 1080 has no room for, and the reason this table exists."""
    request_id, created = _request(db)
    assert (request_id, created) == ("vr-1", True)


def test_ASKING_TWICE_IS_ONE_REQUEST(db):
    """A verification costs a cluster run, so a double-click must not buy two.

    Idempotent on the WORK — artifact and environment — rather than on a caller-supplied key: a
    client minting a fresh key per click would defeat a key-based guard entirely.
    """
    _request(db)
    assert _request(db, request_id="vr-2") == ("vr-1", False)


def test_ASKING_AGAIN_AFTER_A_REFUSAL_IS_ALLOWED(db):
    """The guard is against double-clicks, not against re-verifying something that changed."""
    _request(db)
    advance_verification(db, "vr-1", VerificationStatusV1.REFUSED,
                         findings=[{"check": "row_count", "detail": "0 rows"}])
    assert _request(db, request_id="vr-2") == ("vr-2", True)


def test_PASSED_IS_EARNED_not_declared(db):
    """A worker that jumped straight to PASSED would produce a green verification whose history
    says nothing ever ran — precisely the evidence step 14 must not be handed."""
    _request(db)
    with pytest.raises(InvalidVerificationMove, match="cannot move REQUESTED → PASSED"):
        advance_verification(db, "vr-1", VerificationStatusV1.PASSED, execution_hash="exec-1")


def test_A_TERMINAL_REQUEST_DOES_NOT_MOVE(db):
    """A re-verification is a NEW request against the same artifact, never a step backwards."""
    _request(db)
    _pass(db)
    with pytest.raises(InvalidVerificationMove, match="terminal"):
        advance_verification(db, "vr-1", VerificationStatusV1.REFUSED,
                             findings=[{"check": "x", "detail": "y"}])


def test_AN_OUTAGE_AND_A_VERDICT_ARE_DIFFERENT_ANSWERS(db):
    """REFUSED is the feature's problem, FAILED is the platform's. Collapsing them would tell an
    operator their feature is broken when the cluster was down, and they would change a correct
    formula."""
    _request(db)
    advance_verification(db, "vr-1", VerificationStatusV1.FAILED,
                         failure_reason="the cluster was unreachable")
    row = db.execute(
        "SELECT status, failure_reason, jsonb_array_length(findings) "
        "FROM verification_request WHERE request_id = 'vr-1'").fetchone()
    assert row == ("FAILED", "the cluster was unreachable", 0)


def test_a_REFUSAL_MUST_NAME_WHAT_REFUSED_IT(db):
    """Enforced by the database, because a refusal nobody can act on is not one."""
    import psycopg

    _request(db)
    with pytest.raises(psycopg.errors.CheckViolation):
        advance_verification(db, "vr-1", VerificationStatusV1.REFUSED)


# ══ THE GATE ═══════════════════════════════════════════════════════════════════════════════════
def test_A_PASSING_VERIFICATION_AUTHORIZES_PUBLICATION(db):
    _request(db)
    _pass(db)
    evidence = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)

    assert isinstance(evidence, PublicationEvidenceV2), evidence
    assert (evidence.request_id, evidence.execution_hash) == ("vr-1", "exec-1")


def test_the_evidence_NAMES_THE_ATTEMPT_not_a_boolean(db):
    """"This was verified" and "this was verified BY THIS RUN" are different claims, and only the
    second can be audited: a publication record carrying a flag can be traced to nothing."""
    _request(db)
    _pass(db)
    evidence = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)
    assert evidence.execution_hash == "exec-1"


def test_NO_VERIFICATION_AT_ALL_REFUSES(db):
    refusal = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)

    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.VERIFICATION_ABSENT
    assert "none has ever been requested" in refusal.detail


def test_STILL_RUNNING_IS_NOT_A_WEAKER_NO(db):
    """A gate that admitted a promising in-flight run would be publishing on an expectation. The
    message still says which state it is in, because the operator's next move differs."""
    _request(db)
    advance_verification(db, "vr-1", VerificationStatusV1.CLAIMED)
    refusal = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)

    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.VERIFICATION_ABSENT
    assert "wait for it" in refusal.detail


def test_a_REFUSED_verification_sends_the_operator_TO_THE_FINDINGS(db):
    _request(db)
    advance_verification(db, "vr-1", VerificationStatusV1.REFUSED,
                         findings=[{"check": "row_count", "detail": "0 rows"}])
    refusal = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)

    assert isinstance(refusal, MaterializationRefused)
    assert "REFUSED with 1 finding(s)" in refusal.detail


def test_an_OUTAGE_says_RETRY_rather_than_blaming_the_feature(db):
    _request(db)
    advance_verification(db, "vr-1", VerificationStatusV1.FAILED, failure_reason="cluster down")
    refusal = authorize_publication_v2(db, sealed_artifact_id=ART, environment_id=ENV)

    assert "outage rather than a verdict about the feature" in refusal.detail


# ══ THE ARTIFACT IS THE KEY ════════════════════════════════════════════════════════════════════
def test_A_DIFFERENT_ARTIFACT_DOES_NOT_INHERIT_THE_GREEN_TICK(db):
    """The formula may have been re-authored, the policy re-pointed, the renderer moved. A passing
    verdict for the previous artifact says nothing about this one, and nothing downstream would show
    that it had been borrowed."""
    _request(db)
    _pass(db)
    refusal = authorize_publication_v2(db, sealed_artifact_id="art-pilot-2", environment_id=ENV)

    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is PublicationRefusalCode.VERIFICATION_ABSENT


def test_A_DIFFERENT_ENVIRONMENT_DOES_NOT_INHERIT_IT_EITHER(db):
    """Verification is evidence about bytes running SOMEWHERE. The same artifact in another cluster
    is another question, and one nobody has asked."""
    _request(db)
    _pass(db)
    assert current_verification(db, sealed_artifact_id=ART, environment_id="prod") is None


def test_the_gate_REFUSES_AN_UNNAMED_ARTIFACT(db):
    """Without one the question is "has anything ever passed anywhere", and that answer must never
    authorize a publication."""
    with pytest.raises(ValueError, match="never authorize a publication"):
        authorize_publication_v2(db, sealed_artifact_id="  ", environment_id=ENV)
