"""Step 14 — publication requires a CURRENT PASSING verification of THIS artifact.

**The two questions publication asks are about different things, and both must be answered.**
``select_publisher`` asks whether this ENVIRONMENT can publish atomically — a probe result about a
cluster and its engine versions. This asks whether THIS ARTIFACT produces the right numbers — a
verification result about the bytes that were rendered. An environment can be perfectly capable of
publishing a feature nobody has ever checked, which is why one gate cannot stand in for the other and
why ``VERIFICATION_ABSENT`` is its own code.

**Keyed on the sealed artifact, never on the feature or the group.** A passing verdict for a previous
artifact of the same feature says nothing about this one: the formula may have been re-authored, the
policy re-pointed, the renderer moved. Letting the question be asked by feature name is exactly how a
re-rendered artifact inherits the old one's green tick, and nothing downstream would show it had.

**"No passing verification" is one answer here, deliberately.** Never asked, still running, and
refused are three different situations for an operator and the same situation for this gate: none of
them is evidence. The refusal says which it was so nobody has to guess, but the DECISION does not
branch on it — a gate that admitted "still running" because it looked promising would be publishing
on an expectation.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.codes import MaterializationRefused, PublicationRefusalCode
from featuregen.overlay.upload.verification_request_store import (
    VerificationStatusV1,
    current_verification,
)

__all__ = ["PublicationEvidenceV2", "authorize_publication_v2"]


@dataclass(frozen=True, slots=True)
class PublicationEvidenceV2:
    """What a publication is standing on: the artifact, and the attempt that verified it.

    Carries the ``execution_hash`` rather than only a boolean, because "this was verified" and
    "this was verified BY THIS RUN" are different claims and only the second can be audited. A
    publication record naming the attempt can be traced to the numbers that were checked; one
    carrying a passed/failed flag can be traced to nothing.
    """

    sealed_artifact_id: str
    environment_id: str
    request_id: str
    execution_hash: str


def authorize_publication_v2(
    conn: DbConn, *, sealed_artifact_id: str, environment_id: str,
) -> PublicationEvidenceV2 | MaterializationRefused:
    """The verification evidence for publishing ``sealed_artifact_id`` here, or the refusal.

    Returns:
        :class:`PublicationEvidenceV2`, or a ``VERIFICATION_ABSENT`` refusal naming what state the
        verification is actually in.
    """
    for value, what in ((sealed_artifact_id, "sealed_artifact_id"),
                        (environment_id, "environment_id")):
        if not value.strip():
            raise ValueError(
                f"authorize_publication_v2 needs a {what}: without one the question is 'has "
                f"anything ever passed anywhere', and the answer to that must never authorize a "
                f"publication")

    passing = current_verification(
        conn, sealed_artifact_id=sealed_artifact_id, environment_id=environment_id)
    if passing is not None:
        request_id, execution_hash = passing
        return PublicationEvidenceV2(
            sealed_artifact_id=sealed_artifact_id, environment_id=environment_id,
            request_id=request_id, execution_hash=execution_hash)

    return MaterializationRefused(
        PublicationRefusalCode.VERIFICATION_ABSENT,
        f"artifact {sealed_artifact_id!r} has no passing verification in {environment_id!r}: "
        f"{_state_of(conn, sealed_artifact_id, environment_id)}. Publishing it would put numbers "
        f"nobody checked in front of a model")


def _state_of(conn: DbConn, sealed_artifact_id: str, environment_id: str) -> str:
    """What the verification is ACTUALLY doing, for the refusal message only.

    Read separately from the decision on purpose. The gate must not branch on this — "still
    running" is not a weaker no — but an operator told only "no passing verification" cannot tell
    whether to wait, to look at findings, or to ask for one in the first place.
    """
    row = conn.execute(
        "SELECT status, jsonb_array_length(findings), failure_reason FROM verification_request "
        "WHERE sealed_artifact_id = %s AND environment_id = %s "
        "ORDER BY updated_at DESC LIMIT 1",
        (sealed_artifact_id, environment_id)).fetchone()
    if row is None:
        return "none has ever been requested — ask for one"

    status, findings, failure_reason = VerificationStatusV1(row[0]), row[1], row[2]
    if status is VerificationStatusV1.REFUSED:
        return f"the most recent one REFUSED with {findings} finding(s) — read them"
    if status is VerificationStatusV1.FAILED:
        return (f"the most recent one FAILED for platform reasons ({failure_reason}) — this is an "
                f"outage rather than a verdict about the feature, so retry it")
    if status is VerificationStatusV1.CANCELLED:
        return "the most recent one was CANCELLED — ask for another"
    return f"the most recent one is {status.value} — wait for it"
