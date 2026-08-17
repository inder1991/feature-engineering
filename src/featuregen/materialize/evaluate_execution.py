"""S11 — ``evaluate_verify`` and ``evaluate_publish_sandbox``: §0.3's other two gates.

**These have exactly two callers each, and that is an acceptance clause rather than a style note.**
Verification and publication are USER-TRIGGERED (§0.4: sandbox verification is "user-triggered, tied
to the exact artifact, never interchangeable with a development proof"). If either evaluator were
reachable from a relay route, a timer or a batch, the platform could execute against a cluster — or
make something visible — without anyone asking. So each is called from its own request endpoint and
nowhere else, and an enumeration test over the worker's relay routes, control handlers and timers
asserts the absence.

**Verify's three requirements are §0.3's, and each maps to a named blocker.** The EXACT sealed
artifact (it exists, and its subgraph check did not refuse it), execution permission, and
environment compatibility. The artifact check is not "an artifact exists": a refused one is recorded
with its findings precisely so nothing later mistakes it for a servable one, and verifying it would
produce a passing verification for a computation nobody was willing to seal.

**Verify does NOT ask for a publication capability, and cannot.** The parameter is absent, not
defaulted — §0.3's asymmetry is the requirement, and a capability argument here would eventually be
passed by a caller that had one to hand.

**Publish's requirements include a CURRENT passing verification, and "current" is three-way.** S9's
staleness has a third value, and both non-current answers refuse for different reasons: a STALE
verification vouches for an artifact whose meaning moved; an UNVERIFIABLE one never vouched for
anything on content. Neither is a basis for making something visible, and collapsing them would tell
an operator to re-verify when the honest answer is that this output can never be verified on content.
"""
from __future__ import annotations

from collections.abc import Sequence

from featuregen.contracts.db import DbConn
from featuregen.materialize.seal_v2 import load_sealed_artifact
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.evaluator_contracts import (
    EvaluatorAction,
    EvaluatorVerdictV1,
    carried_blockers,
)
from featuregen.overlay.upload.verification_store import StalenessV1

__all__ = ["evaluate_publish_sandbox", "evaluate_verify"]


def evaluate_verify(
    conn: DbConn,
    *,
    sealed_artifact_hash: str,
    inventory_observation_id: str,
    environment_id: str,
    execution_permitted: bool,
    activation_blockers: Sequence[str] = (),
) -> EvaluatorVerdictV1:
    """§0.3's Verify gate: the exact sealed artifact · execution permission · environment
    compatibility.

    Args:
        sealed_artifact_hash: the artifact id being verified. THE EXACT one — a verification not
            tied to a specific artifact verifies whatever happened to be rendered.
        inventory_observation_id: the environment observation the run would execute against.
            Carried so a caller cannot verify against one cluster and claim another.
        environment_id: the environment that observation belongs to, compared against the one the
            artifact was SEALED for.
        execution_permitted: whether the caller may execute. Passed in rather than derived, because
            read scope is decided by the shipped predicate at Gate 2 and re-deciding it here would
            be a second opinion about a governed question.
        activation_blockers: anything the activation policy already found, carried through
            ``carried_blockers`` — which RAISES on a code with no disposition rather than dropping it.

    Returns:
        An :class:`EvaluatorVerdictV1`. Note the absence: no publication capability is asked for,
        and there is no parameter for one.
    """
    if not sealed_artifact_hash.strip():
        raise ValueError(
            "evaluate_verify was called with no artifact: §0.3 asks for THE EXACT sealed artifact, "
            "and a verification naming none would verify whatever happened to be rendered")
    if not inventory_observation_id.strip():
        raise ValueError(
            "evaluate_verify was called with no inventory observation: a run has to say which "
            "environment it would execute against, or 'environment compatibility' is a check with "
            "nothing on one side of it")

    blockers = list(carried_blockers(tuple(activation_blockers)))

    sealed = load_sealed_artifact(conn, sealed_artifact_hash)
    if sealed is None or not sealed.servable:
        blockers.append(R.ARTIFACT_NOT_SERVABLE)
    elif sealed.environment_id != environment_id:
        # Checked only when the artifact IS servable: reporting both would tell an operator to fix
        # an environment mismatch on an artifact that could not run in either.
        blockers.append(R.ENVIRONMENT_INCOMPATIBLE)

    if not execution_permitted:
        blockers.append(R.EXECUTION_AUTHORITY_UNMET)

    ordered = tuple(sorted(set(blockers)))
    return EvaluatorVerdictV1(action=EvaluatorAction.VERIFY, allowed=not ordered,
                              blockers=ordered)


def evaluate_publish_sandbox(
    conn: DbConn,
    *,
    verified_output_revision_id: str,
    staging_path: str,
    staleness: StalenessV1,
    publication_permitted: bool,
    capability_attestation: str,
    activation_blockers: Sequence[str] = (),
) -> EvaluatorVerdictV1:
    """§0.3's Publish gate: a current passing verification · the exact staging output · publication
    permission and capability.

    Args:
        verified_output_revision_id: the verification being published from. Must exist and be
            SERVABLE — a swept output's bytes are gone and a quarantined one's are pending deletion.
        staging_path: the exact staged output. Carried and checked against the attempt that produced
            it, because "the exact staging output" is the requirement and two attempts deliberately
            do not share a path (S9).
        staleness: S9's three-way answer, passed in rather than recomputed — the store owns what
            "current" means, and a second answer here could publish from a verification the store
            would have called stale.
        publication_permitted: whether the caller may publish.
        capability_attestation: the attestation publication requires and verification must not.

    Returns:
        An :class:`EvaluatorVerdictV1`.
    """
    if not verified_output_revision_id.strip():
        raise ValueError(
            "evaluate_publish_sandbox was called with no verified output: publication rests on a "
            "verification, and one naming none rests on nothing")
    if not staging_path.strip():
        raise ValueError(
            "evaluate_publish_sandbox was called with no staging path: §0.3 asks for THE EXACT "
            "staged output, and two verification attempts deliberately do not share a path")

    blockers = list(carried_blockers(tuple(activation_blockers)))

    row = conn.execute(
        "SELECT v.retention_state, a.staging_path FROM verified_output_revision v "
        "JOIN verification_attempt a ON a.execution_hash = v.execution_hash "
        "WHERE v.revision_id = %s", (verified_output_revision_id,)).fetchone()
    if row is None or row[0] not in ("live", "marked_orphan") or row[1] != staging_path:
        # ONE code for three shapes of the same fact: there is no current passing verification over
        # this exact staged output. Splitting them would name where the caller's belief diverged
        # from the record, which is a debugging question rather than a governed one.
        blockers.append(R.VERIFICATION_NOT_CURRENT)
    elif staleness is not StalenessV1.CURRENT:
        # STALE and NEITHER both refuse, for different reasons the code's own text carries: a stale
        # verification vouches for an artifact whose meaning moved, an unverifiable one never
        # vouched for anything on content.
        blockers.append(R.VERIFICATION_NOT_CURRENT)

    if not publication_permitted:
        blockers.append(R.EXECUTION_AUTHORITY_UNMET)
    if not capability_attestation.strip():
        blockers.append(R.PUBLICATION_CAPABILITY_MISSING)

    ordered = tuple(sorted(set(blockers)))
    return EvaluatorVerdictV1(action=EvaluatorAction.PUBLISH_SANDBOX, allowed=not ordered,
                              blockers=ordered)
