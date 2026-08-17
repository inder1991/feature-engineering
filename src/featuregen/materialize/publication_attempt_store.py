"""S10 — exact-output CAS publication: four outcomes, and an uncertain one that blocks retry.

**Four outcomes, because publication crosses two planes.** A Hive swap and a PostgreSQL transaction
are not one transaction, and there is a real window where the swap succeeds and the transaction later
rolls back. No care inside either plane closes it; the alternative is a distributed transaction this
platform does not have and would not want on the publish path. So an attempt ends ``STARTED`` ·
``SUCCEEDED`` · ``FAILED`` · ``UNKNOWN_RECONCILIATION_REQUIRED``, and the fourth is the honest name
for *the swap may or may not have landed*.

**An uncertain attempt BLOCKS retry until reconciled**, and so does a ``STARTED`` one. Retrying an
attempt that may already have swapped is how a group gets published twice, or published from an
artifact the operator believed had failed. A ``STARTED`` row that never moved is the same
uncertainty reached by crashing rather than by catching, so treating it as retryable would be
assuming that a crash means nothing happened. The block is a partial unique index — at most one
unreconciled blocking attempt per ``(environment, group)`` — so a retry cannot even be RECORDED while
one is outstanding, rather than being refused by a check somebody remembered to run.

**An older verified output over a newer active revision refuses.** ``expected_active_revision_id`` is
the CAS half: the caller names the revision it read, and a publish whose expectation no longer holds
is refused rather than retried here — the caller read a verified output against one active revision,
and a later one may make that output the wrong thing to publish.
:func:`~featuregen.overlay.upload.publication_revisions.check_publish_precondition` owns that rule
and is called rather than restated.

**Capability lives here and nowhere in S9.** §0.3: verification must not require a publication
capability and publication must. The attestation is a required column on this table, and 1080
deliberately has no column for one.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import psycopg

from featuregen.contracts.db import DbConn
from featuregen.overlay.upload.publication_revisions import (
    PublishRequestV1,
    check_publish_precondition,
)

__all__ = [
    "PublicationBlocked",
    "PublicationOutcomeV1",
    "PublicationAttemptV1",
    "blocking_attempt",
    "reconcile_attempt",
    "record_publication_attempt",
    "settle_attempt",
]


class PublicationOutcomeV1(StrEnum):
    """What happened to an attempt. Four, because two of them are certainty and two are not."""

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    #: The swap may or may not have landed. Not an error state — a state of KNOWLEDGE, and the only
    #: honest one when a cross-plane window is interrupted.
    UNKNOWN_RECONCILIATION_REQUIRED = "unknown_reconciliation_required"

    @property
    def blocks_retry(self) -> bool:
        """Whether an unreconciled attempt in this state stops another from being recorded.

        ``STARTED`` blocks for the same reason the uncertain outcome does: nobody knows whether the
        swap landed, and the difference between the two is only how the process ended.
        """
        return self in (PublicationOutcomeV1.STARTED,
                        PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)


class PublicationBlocked(Exception):
    """An unreconciled attempt is outstanding for this group. Named, because the remedy is to
    RECONCILE it — check the published generation marker — never to try again harder."""


@dataclass(frozen=True, slots=True)
class PublicationAttemptV1:
    """One attempt to publish one group in one environment."""

    attempt_id: str
    environment_id: str
    logical_group_name: str
    verified_output_revision_id: str
    sealed_artifact_id: str
    expected_active_revision_id: str | None
    publish_mechanism: str
    capability_attestation: str
    outcome: PublicationOutcomeV1
    detail: str = ""

    def __post_init__(self) -> None:
        for value, what in (
            (self.attempt_id, "attempt_id"), (self.environment_id, "environment_id"),
            (self.logical_group_name, "logical_group_name"),
            (self.verified_output_revision_id, "verified_output_revision_id"),
            (self.sealed_artifact_id, "sealed_artifact_id"),
            (self.publish_mechanism, "publish_mechanism"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a publication attempt with a blank {what} cannot say what it published, "
                    f"where, or from which artifact")
        if not self.capability_attestation.strip():
            raise ValueError(
                "a publication attempt with no capability attestation: publication REQUIRES one "
                "(§0.3) — it is the single thing that distinguishes it from a verification, and an "
                "attempt without it is a publish nobody was entitled to make")


def blocking_attempt(
    conn: DbConn, *, environment_id: str, logical_group_name: str,
) -> tuple[str, PublicationOutcomeV1] | None:
    """The unreconciled attempt standing in the way, or ``None``.

    Asked before recording rather than only enforced by the index, so a caller gets the attempt id
    it must reconcile rather than a constraint violation it has to decode.
    """
    row = conn.execute(
        "SELECT attempt_id, outcome FROM publication_attempt "
        "WHERE environment_id = %s AND logical_group_name = %s AND reconciled_at IS NULL "
        "AND outcome IN ('started', 'unknown_reconciliation_required')",
        (environment_id, logical_group_name)).fetchone()
    return None if row is None else (row[0], PublicationOutcomeV1(row[1]))


def record_publication_attempt(
    conn: DbConn,
    attempt: PublicationAttemptV1,
    *,
    observed_active_revision_id: str | None,
    started_at: str,
) -> str:
    """Check the CAS precondition, refuse if a blocking attempt is outstanding, and record.

    Order matters and is deliberate: the precondition is checked FIRST, because an attempt whose
    expectation no longer holds should leave no row at all — it never began.

    Raises:
        ActiveRevisionConflict: the active revision moved since the caller read it. An older
            verified output over a newer active revision refuses here.
        PublicationBlocked: an unreconciled ``STARTED`` or uncertain attempt is outstanding for this
            group. The remedy is to reconcile it against the published generation marker, never to
            retry.
    """
    check_publish_precondition(
        PublishRequestV1(
            verified_output_revision_id=attempt.verified_output_revision_id,
            expected_active_revision_id=attempt.expected_active_revision_id),
        observed_active_revision_id=observed_active_revision_id)

    blocking = blocking_attempt(conn, environment_id=attempt.environment_id,
                                logical_group_name=attempt.logical_group_name)
    if blocking is not None and blocking[0] != attempt.attempt_id:
        held, outcome = blocking
        raise PublicationBlocked(
            f"attempt {held} for {attempt.logical_group_name!r} in {attempt.environment_id!r} is "
            f"{outcome.value} and unreconciled: nobody knows whether its swap landed, so a retry "
            f"could publish the group twice or publish from an artifact believed to have failed. "
            f"Reconcile it against the published generation marker first")

    try:
        conn.execute(
            "INSERT INTO publication_attempt (attempt_id, environment_id, logical_group_name, "
            "verified_output_revision_id, sealed_artifact_id, expected_active_revision_id, "
            "publish_mechanism, capability_attestation, outcome, detail, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (attempt_id) DO NOTHING",
            (attempt.attempt_id, attempt.environment_id, attempt.logical_group_name,
             attempt.verified_output_revision_id, attempt.sealed_artifact_id,
             attempt.expected_active_revision_id, attempt.publish_mechanism,
             attempt.capability_attestation, attempt.outcome.value, attempt.detail, started_at))
    except psycopg.errors.UniqueViolation as exc:   # the index, reached by a concurrent writer
        raise PublicationBlocked(
            f"another attempt for {attempt.logical_group_name!r} in {attempt.environment_id!r} "
            f"became blocking concurrently: only one unreconciled attempt may exist per group") \
            from exc
    return attempt.attempt_id


def settle_attempt(
    conn: DbConn, attempt_id: str, outcome: PublicationOutcomeV1, *, detail: str = "",
) -> None:
    """Move a ``STARTED`` attempt to one of the three settled outcomes.

    Raises:
        ValueError: the attempt is not ``STARTED``. A settled outcome is a fact about what happened
            to two systems, and moving it would restate that.
    """
    if outcome is PublicationOutcomeV1.STARTED:
        raise ValueError(
            "settle_attempt cannot set STARTED: an attempt starts in that state, and 'settling' it "
            "there would record a transition that did not happen")
    changed = conn.execute(
        "UPDATE publication_attempt SET outcome = %s, detail = %s "
        "WHERE attempt_id = %s AND outcome = 'started'",
        (outcome.value, detail, attempt_id)).rowcount
    if changed != 1:
        raise ValueError(
            f"attempt {attempt_id} is not STARTED (or does not exist): a settled outcome is a fact "
            f"about what happened to two systems, and moving it would restate that")


def reconcile_attempt(
    conn: DbConn,
    attempt_id: str,
    *,
    observed_outcome: PublicationOutcomeV1,
    reconciled_at: str,
) -> None:
    """Record what the published generation marker says an UNCERTAIN attempt actually did.

    This is the only thing that unblocks a group. It takes the OBSERVED outcome — what the marker
    shows — rather than a decision, because the point of reconciliation is to look rather than to
    choose.

    Raises:
        ValueError: the attempt is not uncertain, is already reconciled, or the observed outcome is
            not one of ``SUCCEEDED``/``FAILED``. "Still unknown" is not a reconciliation; it is the
            state the attempt is already in.
    """
    if observed_outcome not in (PublicationOutcomeV1.SUCCEEDED, PublicationOutcomeV1.FAILED):
        raise ValueError(
            f"reconciliation observed {observed_outcome.value}: the marker either shows the "
            f"generation published or it does not, and recording 'still unknown' as a "
            f"reconciliation would unblock a group nobody checked")
    changed = conn.execute(
        "UPDATE publication_attempt SET reconciled_at = %s, reconciled_outcome = %s "
        "WHERE attempt_id = %s AND outcome = 'unknown_reconciliation_required' "
        "AND reconciled_at IS NULL",
        (reconciled_at, observed_outcome.value, attempt_id)).rowcount
    if changed != 1:
        raise ValueError(
            f"attempt {attempt_id} is not an unreconciled uncertain attempt: reconciling a settled "
            f"one would restate a fact the attempt already established, and reconciling twice would "
            f"let the second look override the first")
