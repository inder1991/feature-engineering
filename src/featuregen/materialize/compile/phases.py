"""C-D0 — the four phases of the generate → verify → publish chain, with DURABLE boundaries.

**Why not one transaction with four phase objects.** That was the tempting answer and it is the
wrong one: it cannot support generated code remaining unverified indefinitely with verification run
on demand, which is already a requirement. Worse, the "all-or-nothing" it would preserve is not
real. ``chain.py`` today wraps everything in one ``with conn.transaction()``, and inside it:

* a Hadoop submission cannot be rolled back by PostgreSQL;
* ``os.replace`` cannot be undone if the database commit later fails;
* the code itself acknowledges orphaned staged output.

So the single transaction buys atomicity over the database rows only, while presenting itself as
atomicity over the whole act. Four phases with honest, separate durability boundaries are both more
truthful and more capable.

**The boundaries** (product owner's decision, 2026-08-16):

1. :class:`GenerateArtifact` — compile, render, run L0, persist the sealed artifact. **No Hadoop
   submission, no publisher selection, no publication.**
2. :class:`RequestVerification` — append an on-demand verification request and commit.
3. :class:`ExecuteVerification` — claim an attempt in a SHORT transaction, run Hadoop OUTSIDE any
   transaction, persist the immutable result in ANOTHER transaction.
4. :class:`PublishVerifiedOutput` — validate the exact current verification, the environment and the
   expected active revision, then publish under CAS and record the result.

**This eliminates the publisher-refusal bug structurally.** Publisher selection belongs only to
phase 4, so an L0 failure in phase 1 cannot discard a publication verdict — there is no publication
verdict yet to discard. (Until the extraction lands, ``chain.py`` carries an interim fix:
``_RunAttempt.publication_refusal`` preserves the verdict wherever the run stopped.)

**A synchronous facade may call all four consecutively. It must not wrap them in one transaction** —
:func:`run_all_phases` exists to make that composition available without making it a rollback unit,
and :data:`PHASE_SIDE_EFFECTS` is what a test uses to prove no phase reaches another's.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

__all__ = [
    "PHASE_SIDE_EFFECTS",
    "ChainPhase",
    "ExecuteVerification",
    "GenerateArtifact",
    "PhaseOutcomeV1",
    "PublishVerifiedOutput",
    "RequestVerification",
    "SideEffect",
    "owns",
    "run_all_phases",
]


class ChainPhase(StrEnum):
    """The four, in the only order they can run."""

    GENERATE_ARTIFACT = "generate_artifact"
    REQUEST_VERIFICATION = "request_verification"
    EXECUTE_VERIFICATION = "execute_verification"
    PUBLISH_VERIFIED_OUTPUT = "publish_verified_output"


class SideEffect(StrEnum):
    """The classes of effect a phase may have on the world.

    Enumerated so "no one of them can reach another's side effects" is a checkable statement rather
    than a claim. A boolean "is separated" would be satisfied by four Protocols with empty bodies,
    which would prove nothing at all.
    """

    SEAL_ARTIFACT = "seal_artifact"                    # write the sealed project tree
    RECORD_GENERATION = "record_generation"            # persist generation + plan + binding rows
    RUN_L0 = "run_l0"                                  # local build proof
    APPEND_VERIFICATION_REQUEST = "append_verification_request"
    CLAIM_VERIFICATION_ATTEMPT = "claim_verification_attempt"
    SUBMIT_TO_CLUSTER = "submit_to_cluster"            # Hadoop — NOT rollback-able
    RECORD_VERIFICATION_RESULT = "record_verification_result"
    SELECT_PUBLISHER = "select_publisher"
    SWAP_ACTIVE_REVISION = "swap_active_revision"      # the CAS
    RECORD_PUBLICATION = "record_publication"


#: WHICH effects each phase owns. Disjoint by construction, and a test proves the partition.
#:
#: ``SELECT_PUBLISHER`` sits in phase 4 and nowhere else — that placement is what makes the
#: publisher-refusal bug unrepresentable rather than merely fixed, because a phase that has not
#: selected a publisher cannot discard the selection's verdict.
PHASE_SIDE_EFFECTS: Mapping[ChainPhase, frozenset[SideEffect]] = {
    ChainPhase.GENERATE_ARTIFACT: frozenset({
        SideEffect.SEAL_ARTIFACT, SideEffect.RECORD_GENERATION, SideEffect.RUN_L0}),
    ChainPhase.REQUEST_VERIFICATION: frozenset({SideEffect.APPEND_VERIFICATION_REQUEST}),
    ChainPhase.EXECUTE_VERIFICATION: frozenset({
        SideEffect.CLAIM_VERIFICATION_ATTEMPT, SideEffect.SUBMIT_TO_CLUSTER,
        SideEffect.RECORD_VERIFICATION_RESULT}),
    ChainPhase.PUBLISH_VERIFIED_OUTPUT: frozenset({
        SideEffect.SELECT_PUBLISHER, SideEffect.SWAP_ACTIVE_REVISION,
        SideEffect.RECORD_PUBLICATION}),
}

#: Effects that CANNOT be rolled back by a database transaction. Naming them is the honest half of
#: the boundary argument: phase 3 runs the cluster submission outside any transaction precisely
#: because wrapping it in one would claim a rollback that does not exist.
NON_TRANSACTIONAL: frozenset[SideEffect] = frozenset({
    SideEffect.SUBMIT_TO_CLUSTER, SideEffect.SEAL_ARTIFACT, SideEffect.SWAP_ACTIVE_REVISION})


@dataclass(frozen=True, slots=True)
class PhaseOutcomeV1:
    """What one phase produced, and whether the next may run.

    ``refusal_code`` is per-PHASE. A later phase never overwrites an earlier phase's verdict,
    because they are separate records — which is the structural form of the fix for the bug where
    an L0 failure erased the publisher's answer.
    """

    phase: ChainPhase
    succeeded: bool
    refusal_code: str
    detail: str
    durable_reference: str

    def __post_init__(self) -> None:
        if self.succeeded and self.refusal_code:
            raise ValueError(
                f"{self.phase.value} succeeded while carrying refusal_code "
                f"{self.refusal_code!r}: a caller reading `succeeded` would advance past a phase "
                f"that refused")
        if not self.succeeded and not self.refusal_code.strip():
            raise ValueError(
                f"{self.phase.value} refused with no code: the next phase cannot be told why it "
                f"is not running, and a refusal is indistinguishable from a crash")
        if self.succeeded and not self.durable_reference.strip():
            raise ValueError(
                f"{self.phase.value} succeeded with no durable reference. Each phase COMMITS; a "
                f"success nothing can name afterwards is a success the next phase cannot resume "
                f"from, which is the whole point of separating the boundaries")


class GenerateArtifact(Protocol):
    """Phase 1. Compile, render, run L0, persist the sealed artifact — and stop.

    No cluster submission, no publisher selection, no publication. The artifact may sit unverified
    indefinitely, which is a requirement rather than an accident: verification is on demand.
    """

    def generate_artifact(
        self, *, generation_authorization_revision_id: str,
    ) -> PhaseOutcomeV1: ...


class RequestVerification(Protocol):
    """Phase 2. Append an on-demand verification request, and COMMIT.

    Separate from phase 3 because asking is not running: a request is durable the moment it is
    made, so a crash before execution leaves a queued request rather than nothing.
    """

    def request_verification(
        self, *, sealed_artifact_hash: str, requested_by: str,
    ) -> PhaseOutcomeV1: ...


class ExecuteVerification(Protocol):
    """Phase 3. Claim in a short transaction · run OUTSIDE any transaction · persist in another.

    Three boundaries, not one, because the middle step is a Hadoop submission that PostgreSQL
    cannot roll back. Holding a transaction open across it would hold a lock for the length of a
    cluster job and still not make it atomic.
    """

    def execute_verification(
        self, *, verification_request_id: str, attempt: int,
    ) -> PhaseOutcomeV1: ...


class PublishVerifiedOutput(Protocol):
    """Phase 4. Validate the exact current verification, environment and expected active revision,
    then publish under CAS and record the result.

    Publisher selection lives HERE and only here.
    """

    def publish_verified_output(
        self, *, verified_output_revision_id: str, environment_id: str,
        expected_active_revision_id: str | None,
    ) -> PhaseOutcomeV1: ...


def run_all_phases(
    generate: GenerateArtifact,
    request: RequestVerification,
    execute: ExecuteVerification,
    publish: PublishVerifiedOutput,
    *,
    generation_authorization_revision_id: str,
    sealed_artifact_hash: str,
    requested_by: str,
    environment_id: str,
    expected_active_revision_id: str | None,
    attempt: int = 1,
) -> tuple[PhaseOutcomeV1, ...]:
    """A synchronous facade calling all four consecutively — and NOT wrapping them.

    It takes no connection and opens no transaction, which is the point: composing the phases must
    stay available without turning them back into one rollback unit. Each phase commits its own
    work, so this returns everything that DID happen up to a refusal rather than unwinding it.
    """
    outcomes: list[PhaseOutcomeV1] = []

    first = generate.generate_artifact(
        generation_authorization_revision_id=generation_authorization_revision_id)
    outcomes.append(first)
    if not first.succeeded:
        return tuple(outcomes)

    second = request.request_verification(
        sealed_artifact_hash=sealed_artifact_hash, requested_by=requested_by)
    outcomes.append(second)
    if not second.succeeded:
        return tuple(outcomes)

    third = execute.execute_verification(
        verification_request_id=second.durable_reference, attempt=attempt)
    outcomes.append(third)
    if not third.succeeded:
        return tuple(outcomes)

    outcomes.append(publish.publish_verified_output(
        verified_output_revision_id=third.durable_reference, environment_id=environment_id,
        expected_active_revision_id=expected_active_revision_id))
    return tuple(outcomes)


def owns(phase: ChainPhase, effect: SideEffect) -> bool:
    """Whether ``phase`` may perform ``effect``.

    The question a reviewer actually asks, answered in one place rather than by each caller
    comparing sets.
    """
    return effect in PHASE_SIDE_EFFECTS[phase]
