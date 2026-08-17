"""S9 — on-demand sandbox verification: the attempt, its staging path, and three-way staleness.

**Verification runs with NO publication capability, and that is enforced by absence.** §0.3 gives
Verify three requirements — the exact sealed artifact, execution permission, environment
compatibility — and publication capability is not among them. So nothing here takes one, stores one
or reads one: not a nullable column, not a boolean defaulting to false. A field would eventually be
read as "may publish", which is exactly the attestation verification is defined to run without.

**Two attempts do not share a staging path.** ``attempt`` is part of the execution identity (C-D13)
because the existing staging root is GENERATION-scoped: without it a second verification of the same
generation writes over the first and "the exact staging output" names two different things. The
uniqueness is the database's, not this module's — a path collision is refused rather than discovered
when the second run overwrites the first's rows.

**Staleness is THREE-WAY, and the third value is the point.**
:func:`staleness_of` answers ``STALE`` / ``CURRENT`` / ``NEITHER``:

* a comparable ``OBSERVED`` input that changed ⇒ **stale**;
* an identical observation ⇒ **current**;
* ``UNPINNED`` ⇒ **neither**, because nothing was pinned and no content comparison can say whether it
  moved. Forcing it into a boolean makes both answers lies: "current" claims a check nobody could
  run, "stale" claims a change nobody observed. Such an output is labelled **unverifiable** and is
  never claimed current or stale on content.

**``PINNED`` is never a claim without enforced reads.** "Pinned" means the run could only have read
what it pinned; without enforcement it is a description of intent, and a staleness answer computed
from intent is an answer about a promise. Refused here by name and by database CHECK, because one of
the two being absent is how the other gets removed.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from featuregen.contracts.db import DbConn
from featuregen.overlay.upload.verification_revisions import (
    RetentionStateV1,
    VerificationExecutionIdentityV1,
    VerifiedOutputRevisionV1,
    stale_against,
)

__all__ = [
    "ObservationStrengthV1",
    "StagingPathCollision",
    "StalenessV1",
    "UnenforcedPinnedReads",
    "VerificationLabelV1",
    "label_for",
    "record_verification_attempt",
    "record_verified_output",
    "set_retention_state",
    "staleness_of",
]


class ObservationStrengthV1(StrEnum):
    """How strongly a verification's INPUTS were tied to what it read.

    Three values because staleness has three answers, and the mapping is one-to-one: pinning
    decides what a later content comparison is even able to say.
    """

    #: The run could only have read what it pinned — and the reads were ENFORCED, not merely
    #: intended. Never recorded without enforcement.
    PINNED = "pinned"
    #: The inputs were observed at run time and are comparable afterwards.
    OBSERVED = "observed"
    #: Nothing was pinned. No content comparison can say whether the inputs moved.
    UNPINNED = "unpinned"


class StalenessV1(StrEnum):
    """The three-way answer. ``NEITHER`` is not "unknown yet" — it is "not decidable on content"."""

    STALE = "stale"
    CURRENT = "current"
    NEITHER = "neither"

    @property
    def is_unverifiable(self) -> bool:
        """Whether this output must be labelled unverifiable rather than current or stale."""
        return self is StalenessV1.NEITHER


class StagingPathCollision(Exception):
    """Two attempts would write to one staging path — refused, never overwritten."""


class UnenforcedPinnedReads(ValueError):
    """An output claims ``PINNED`` strength without enforced reads.

    Named because the remedy is specific: enforce the reads, or record the honest strength. Both are
    work on the run, and neither is "record it anyway".
    """


def record_verification_attempt(
    conn: DbConn,
    identity: VerificationExecutionIdentityV1,
    *,
    sealed_artifact_id: str,
    staging_root: str,
    started_at: str,
) -> str:
    """Append a verification attempt and return its execution hash.

    Takes no publication capability and stores none — see the module docstring. The staging path is
    DERIVED from the identity rather than accepted, so two attempts cannot be handed the same one by
    a caller that computed it differently.

    Raises:
        StagingPathCollision: another attempt already holds this path. Refused rather than
            overwritten, because the first attempt's output is what somebody may already be reading.
        ValueError: no sealed artifact. A verification not tied to one verifies whatever happened to
            be rendered.
    """
    if not sealed_artifact_id.strip():
        raise ValueError(
            "a verification attempt must name the sealed artifact it verifies: §0.3 asks for THE "
            "EXACT artifact, and an attempt naming none verifies whatever happened to be rendered")

    path = identity.staging_path(staging_root)
    holder = conn.execute(
        "SELECT execution_hash FROM verification_attempt WHERE staging_path = %s",
        (path,)).fetchone()
    if holder is not None and holder[0] != identity.execution_hash:
        raise StagingPathCollision(
            f"attempt {identity.attempt} of {identity.generation_authorization_revision_id} would "
            f"write to {path}, which execution {holder[0]} already holds: two attempts sharing a "
            f"path make 'the exact staging output' name two different things, and the first one's "
            f"output may already be being read")

    conn.execute(
        "INSERT INTO verification_attempt (execution_hash, "
        "generation_authorization_revision_id, check_set_hash, inventory_observation_id, "
        "attempt, run_parameters, staging_path, sealed_artifact_id, started_at) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s) "
        "ON CONFLICT (execution_hash) DO NOTHING",
        (identity.execution_hash, identity.generation_authorization_revision_id,
         identity.check_set_hash, identity.inventory_observation_id, identity.attempt,
         json.dumps([list(pair) for pair in sorted(identity.run_parameters)]), path,
         sealed_artifact_id, started_at))
    return identity.execution_hash


def record_verified_output(
    conn: DbConn, verified: VerifiedOutputRevisionV1, *, reads_enforced: bool,
) -> str:
    """Append a PASSED verification's output. Returns the revision id.

    Raises:
        UnenforcedPinnedReads: the output claims ``PINNED`` without enforced reads. "Pinned" means
            the run could only have read what it pinned, so without enforcement it describes an
            intention — and a staleness answer computed from it would be about a promise.
    """
    if (verified.input_observation_strength == ObservationStrengthV1.PINNED
            and not reads_enforced):
        raise UnenforcedPinnedReads(
            f"{verified.revision_id} claims PINNED input observation with reads that were not "
            f"enforced: pinned means the run COULD ONLY have read what it pinned, and without "
            f"enforcement that is a description of intent rather than of what happened")

    conn.execute(
        "INSERT INTO verified_output_revision (revision_id, execution_hash, check_set_hash, "
        "validator_versions, pinned_policy_hashes, input_observation_strength, reads_enforced, "
        "retention_state) VALUES (%s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (verified.revision_id, verified.execution_hash, verified.check_set_hash,
         json.dumps([list(pair) for pair in sorted(verified.validator_versions)]),
         json.dumps(sorted(verified.pinned_policy_hashes)),
         verified.input_observation_strength, reads_enforced,
         verified.retention_state.value))
    return verified.revision_id


def set_retention_state(
    conn: DbConn, revision_id: str, state: RetentionStateV1,
) -> None:
    """Move an output through ``live → marked_orphan → quarantined → swept``.

    The ONE field that legitimately moves on a verified output, reusing ``runtime/blob_gc``'s
    lifecycle verbatim rather than inventing a second one. The database permits an update that
    changes only this column and refuses every other edit, so retention cannot become a route to
    restating what was verified.
    """
    conn.execute(
        "UPDATE verified_output_revision SET retention_state = %s WHERE revision_id = %s",
        (state.value, revision_id))


def staleness_of(
    verified: VerifiedOutputRevisionV1,
    *,
    current_policy_hashes: Sequence[str],
) -> tuple[StalenessV1, tuple[str, ...]]:
    """The three-way answer, and WHICH policies drifted.

    Returns the drifted policies rather than only a verdict, because an operator deciding whether to
    re-verify needs to know which policy moved: a currency conversion changing is a different
    conversation from a status policy changing.

    ``UNPINNED`` returns ``(NEITHER, ())`` even when policies have drifted — not because nothing
    moved, but because nothing was pinned, so no content comparison can attribute the movement to
    this output. Reporting it as stale would claim an observation nobody made.
    """
    if verified.input_observation_strength == ObservationStrengthV1.UNPINNED:
        return StalenessV1.NEITHER, ()
    drifted = stale_against(verified, current_policy_hashes)
    return (StalenessV1.STALE if drifted else StalenessV1.CURRENT), drifted


@dataclass(frozen=True, slots=True)
class VerificationLabelV1:
    """What a surface may say about a verified output, and never more than it can prove."""

    staleness: StalenessV1
    drifted_policies: tuple[str, ...]

    @property
    def label(self) -> str:
        """The word a surface shows. ``unverifiable`` for ``NEITHER`` — never current, never stale.

        A single place, so two surfaces cannot describe one output differently: the whole reason
        staleness is three-way is that the third case has no honest two-word answer.
        """
        if self.staleness is StalenessV1.NEITHER:
            return "unverifiable"
        return self.staleness.value


def label_for(
    verified: VerifiedOutputRevisionV1, *, current_policy_hashes: Sequence[str],
) -> VerificationLabelV1:
    """:func:`staleness_of`, as the label a surface may show."""
    staleness, drifted = staleness_of(
        verified, current_policy_hashes=current_policy_hashes)
    return VerificationLabelV1(staleness=staleness, drifted_policies=drifted)
