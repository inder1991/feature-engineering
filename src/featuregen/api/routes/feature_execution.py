"""S11 — the user-reachable surface for generate, code view, verify and publish.

**These are the ONLY callers of the three evaluators, and that is the acceptance clause.**
Verification and publication are user-triggered by definition (§0.4: sandbox verification is
"user-triggered, tied to the exact artifact, never interchangeable with a development proof"). If
either evaluator were reachable from a relay route, a timer or a batch, the platform could execute
against a cluster — or make something visible — without anyone asking. So each request endpoint
calls its evaluator once, and an enumeration test over the worker's relay routes, control-signal
handlers and timers asserts nothing else can.

**A ROUTE MUST NOT RUN THE CHAIN**, for the reason ``materialization_runs.py`` records at length:
``get_conn`` holds ONE connection and ONE transaction open for the whole request, and a compile is
bounded by ``COMPILE_BUDGET_SECONDS`` plus the cluster's own timeout. So these are PRODUCERS and
READERS — they evaluate (cheap, a handful of queries), record the request, and return. The test
suite reads this module's AST and asserts it never names ``compile_feature_group``, ``seal_v2`` or
any submitter.

**The evaluate endpoints are GETs and change nothing.** "May I generate this?" is a question a
workspace asks to decide whether to enable a button, and answering it must not mint anything — a
POST that recorded an attempt every time a screen rendered would fill the history with things nobody
did.

**Code view serves through the verified path.** ``serve_artifact`` refuses a non-servable artifact
BEFORE fetching a byte and re-derives every digest on the way out, so a code view cannot display an
artifact the subgraph check refused, and cannot display bytes that do not match their manifest. The
alternative — reading the blobs directly — would be a second retrieval path with its own idea of
what verification means.

**404 for a flag-off deployment**, byte-for-byte Starlette's own body, exactly as
``materialization_runs`` does: a 503 says "this exists and is unwell"; the flag says this deployment
does not run materialization at all.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.permissions import FEATURE_GENERATE, has_permission
from featuregen.materialize.evaluate_execution import (
    evaluate_publish_sandbox,
    evaluate_verify,
)
from featuregen.materialize.generation_authorization import (
    GenerationAuthorizationV1,
    record_generation_authorization,
)
from featuregen.materialize.publication_attempt_store import (
    PublicationAttemptV1,
    PublicationBlocked,
    PublicationOutcomeV1,
    blocking_attempt,
    record_publication_attempt,
)
from featuregen.materialize.queue_lane import materialization_enabled
from featuregen.materialize.seal_v2 import (
    ArtifactNotServable,
    load_manifest,
    load_sealed_artifact,
    realization_links_of,
    serve_artifact,
)
from featuregen.overlay.upload.evaluator_contracts import ACTIVATION_BLOCKER_DISPOSITIONS
from featuregen.overlay.upload.publication_revisions import ActiveRevisionConflict
from featuregen.overlay.upload.selection_revisions import TargetModeV1
from featuregen.overlay.upload.verification_revisions import VerificationExecutionIdentityV1
from featuregen.overlay.upload.verification_store import (
    StagingPathCollision,
    StalenessV1,
    record_verification_attempt,
)
from featuregen.runtime.observability import counters, log

#: Echoed as a header for the reason ``X-Materialization-Request-Id`` is: a client that lost the
#: body can still name the work it started.
VERIFICATION_ID_HEADER = "X-Verification-Execution-Hash"
PUBLICATION_ID_HEADER = "X-Publication-Attempt-Id"

_ATTEMPT_PREFIX = "pubatt"

#: Where verification attempts stage their output. A deployment constant rather than a caller field:
#: a caller-chosen root would let two callers stage into each other's tree, and the per-attempt path
#: S9 derives is only unique WITHIN a root.
STAGING_ROOT = "hdfs://nn/staging/featuregen"


def require_materialization_enabled() -> None:
    """The deployment's switch, consulted BEFORE anything else on every route here.

    The same function object the worker stage calls, over the same truthy set — a route that
    re-implemented the check would 404 for a deployment whose worker was happily draining.
    """
    if not materialization_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(require_materialization_enabled)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


def _explained(blockers: tuple[str, ...]) -> list[dict[str, str]]:
    """Each blocker with the reason the disposition table gives it.

    Read from that table rather than restated here, so the workspace and a corpus report explain a
    code the same way. A code with no disposition raises through the lookup — an unexplained code on
    a screen is how a blocker stops being understood.
    """
    return [{"code": code, "reason": ACTIVATION_BLOCKER_DISPOSITIONS[code][1]}
            for code in blockers]


# ── generate ─────────────────────────────────────────────────────────────────────────────────────
class GenerationRequestIn(BaseModel):
    """What a person authorizes when they press Generate.

    ``target_ref`` is ``None`` exactly when the mode is ``exploration`` — an exploration build HAS no
    target, and a prediction without one would be authorized to predict something nobody named. The
    type and the database both refuse the disagreement, so a wire body cannot introduce it.
    """

    model_config = ConfigDict(extra="forbid")

    environment_id: str = Field(min_length=1)
    logical_group_name: str = Field(min_length=1)
    build_set_revision_id: str = Field(min_length=1)
    target_mode: str = Field(min_length=1)
    target_ref: str | None = None


@router.post("/feature-execution/generations", status_code=201,
             dependencies=[Depends(require_feature_generate)])
def authorize_generation(
    body: GenerationRequestIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Mint the GENERATION AUTHORIZATION — what a generation is authorized FOR (invariant 17).

    **This route deliberately does NOT call ``evaluate_generate``, and the reason is structural
    rather than a preference.** That evaluator needs the feature's policy OCCURRENCES and its
    OPERATOR GRAPH, and both are products of a compilation: the graph is not persisted anywhere (S7
    records that explicitly — the sealed artifact stores the verdict, not the graph), and
    occurrences are derived over a bound input set. A route cannot compile — ``get_conn`` holds one
    transaction for the request and a compile is bounded in minutes — so the evaluation happens
    where the compilation does, in the worker, over the objects it already has. Calling a weakened
    version here would produce a second, laxer answer to a governed question.

    What this route CAN do, and does, is record the authorization the whole downstream chain
    references by id: ``verification_attempt`` is keyed on it, ``evaluate_generate`` refuses a blank
    one, and until now nothing minted one.

    ``201``, not ``202``: nothing was queued. An authorization is a durable decision, and the
    generation that spends it is triggered separately.
    """
    try:
        authorization = GenerationAuthorizationV1(
            environment_id=body.environment_id, logical_group_name=body.logical_group_name,
            build_set_revision_id=body.build_set_revision_id,
            target_mode=TargetModeV1(body.target_mode), target_ref=body.target_ref)
    except ValueError as exc:
        # 422 and not 500: a body whose two target fields disagree is a caller error with a
        # message that names exactly which pair is wrong.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    revision_id = record_generation_authorization(
        conn, authorization, authorized_by=identity.subject, authorized_at=_now(conn))
    counters.incr("featuregen.generate.authorized")
    log("featuregen.generate.authorized", revision_id=revision_id,
        logical_group_name=body.logical_group_name, target_mode=body.target_mode)
    return {
        "generation_authorization_revision_id": revision_id,
        "environment_id": authorization.environment_id,
        "logical_group_name": authorization.logical_group_name,
        "target_mode": authorization.target_mode.value,
        "target_ref": authorization.target_ref,
        "detail": "the generation authorization was recorded; it is what a verification names",
    }


# ── verify eligibility ───────────────────────────────────────────────────────────────────────────
@router.get("/feature-execution/{artifact_id}/verify-eligibility",
            dependencies=[Depends(require_feature_read)])
def verify_eligibility(
    artifact_id: str, inventory_observation_id: str,
    conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """May this artifact be verified? A QUESTION — it records nothing.

    A workspace asks this to decide whether to enable a button. Recording an attempt every time a
    screen rendered would fill the history with things nobody did.
    """
    verdict = evaluate_verify(
        conn, sealed_artifact_hash=artifact_id,
        inventory_observation_id=inventory_observation_id,
        execution_permitted=_may_execute(identity),
        # ▲ EXPLICIT-EMPTY IS A CLAIM (§8.1): this route consults no activation fold today, and
        # says so — an omitted argument was indistinguishable from a caller that never asked. The
        # carried set arrives when §7's decision service absorbs these gates (step 6's worker).
        activation_blockers=())
    return {"action": verdict.action.value, "allowed": verdict.allowed,
            "blockers": _explained(verdict.blockers)}


def _may_execute(identity: IdentityEnvelope) -> bool:
    """Whether this caller may execute against a cluster.

    Asked as a PERMISSION, not as a list of role names — the same question the route guard asks, so
    the two cannot disagree. They used to: the routes demanded the raw ``platform-admin`` claim
    while this function accepted ``feature_engineer``, so a feature engineer was rejected by the
    guard before the function that would have authorised them ever ran. Three vocabularies for one
    idea, and the narrowest of them won by accident of ordering.

    Kept as a separate check even though the guard now asks the same thing: it is what produces the
    EXPLAINED refusal (``EXECUTION_AUTHORITY_UNMET``) rather than a bare 403, and a later change
    that widens the guard should not silently widen who may run a cluster job.

    Derived from the claims the request arrived with, and NOT re-derived from the catalog: read
    scope over the group's physical reads is Gate 2's decision, made when the artifact was
    authorised. This is the coarser "may this person run things at all".
    """
    return has_permission(identity.role_claims, FEATURE_GENERATE)


def _may_publish(identity: IdentityEnvelope) -> bool:
    """Whether this caller may publish. STRICTLY narrower than execution: publication makes a number
    visible to everyone downstream, and the two are separate grants for that reason.

    Still expressed as a ROLE rather than a permission because there is no ``feature:publish``
    permission in the catalogue yet, and inventing one here — in a route module — would put a
    second permission vocabulary next to the real one. The narrowing is deliberate and the
    asymmetry is recorded rather than hidden: a feature engineer REACHES this route (the guard is
    ``feature:generate``) and is refused by name, which is the answer they can act on. A bare 403
    from the guard would have told them only that something, somewhere, said no.
    """
    return "platform_admin" in set(identity.role_claims)


# ── code view ────────────────────────────────────────────────────────────────────────────────────
@router.get("/feature-execution/{artifact_id}/code",
            dependencies=[Depends(require_feature_read)])
def artifact_code(artifact_id: str, conn: _Conn) -> dict[str, Any]:
    """The generated project's files, verified on the way out.

    Served through :func:`~featuregen.materialize.seal_v2.serve_artifact`, which refuses a
    non-servable artifact before fetching a byte and re-derives every digest — so a code view cannot
    display an artifact the subgraph check refused, and cannot display bytes that disagree with
    their manifest. Reading the blobs directly would be a second retrieval path with its own idea of
    what verification means.
    """
    sealed = load_sealed_artifact(conn, artifact_id)
    manifest = load_manifest(conn, artifact_id)
    if sealed is None or manifest is None:
        raise HTTPException(status_code=404, detail="sealed artifact not found")
    try:
        files = serve_artifact(conn, sealed, manifest)
    except ArtifactNotServable as exc:
        # 409, not 404: the artifact EXISTS and its findings are the answer. A 404 would send an
        # operator looking for a missing record instead of at the graph that was refused.
        raise HTTPException(
            status_code=409,
            detail=f"{exc}. The subgraph findings are on the artifact and explain what to fix",
        ) from exc

    return {
        "artifact_id": sealed.artifact_id,
        "environment_id": sealed.environment_id,
        "logical_group_name": sealed.logical_group_name,
        "project_digest": sealed.project_digest,
        "files": [{"path": path, "content": text} for path, text in sorted(files.items())],
        # POLICY PROVENANCE, visible (S11's deliverable): which governed realizations produced this
        # number, and which occurrence each answered.
        "policy_realizations": [
            {"revision_id": link.revision_id, "occurrence_hash": link.occurrence_hash}
            for link in realization_links_of(conn, artifact_id)],
    }


# ── verify ───────────────────────────────────────────────────────────────────────────────────────
class VerificationRequestIn(BaseModel):
    """What a person asks for when they press Verify.

    ``extra="forbid"`` for ``materialization_runs``'s reason: a typo must not silently drop a field
    and substitute a default. There is no capability field, and that is the point — §0.3 says
    verification must not require one, so there is nowhere for a caller to supply one.
    """

    model_config = ConfigDict(extra="forbid")

    sealed_artifact_id: str = Field(min_length=1)
    check_set_hash: str = Field(min_length=1)
    inventory_observation_id: str = Field(min_length=1)
    #: `generation_authorization_revision_id` and `environment_id` are NOT fields here, and their
    #: absence is the point. Both are properties OF THE ARTIFACT, recorded when it was sealed, and
    #: both end up inside the verification identity — so accepting them from the request body let
    #: the caller choose two security-relevant values that the server already knows. `extra="forbid"`
    #: means an old client still sending them gets a 422 rather than being quietly ignored.
    #: Counted from 1 by the caller, because the caller is the one who knows this is a RETRY. A
    #: server-chosen next-attempt would make two concurrent clicks two attempts nobody asked for.
    attempt: int = Field(ge=1)


@router.post("/feature-execution/verifications", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def request_verification(
    body: VerificationRequestIn, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Record a verification request. ONE of the two callers of ``evaluate_verify``.

    Records; does not execute. The worker runs the verification, exactly as it runs a compile — a
    route that executed inline would hold this request's transaction open for the length of a
    cluster job.
    """
    verdict = evaluate_verify(
        conn, sealed_artifact_hash=body.sealed_artifact_id,
        inventory_observation_id=body.inventory_observation_id,
        execution_permitted=_may_execute(identity),
        activation_blockers=())   # explicit-empty is a claim — §8.1; see verify_eligibility
    if not verdict.allowed:
        raise HTTPException(
            status_code=409,
            detail={"detail": "verification is not allowed for this artifact",
                    "blockers": _explained(verdict.blockers)})

    # DERIVED FROM THE ARTIFACT, not from the body. The verification identity says which approval
    # the run is being verified under; taking that from the request would let a caller verify one
    # artifact while citing somebody else's approval, and the resulting attempt would look
    # perfectly well-formed. `evaluate_verify` has already established the artifact is servable, so
    # this load succeeds — but it is re-read rather than assumed, because "the gate passed" is not
    # a value.
    sealed = load_sealed_artifact(conn, body.sealed_artifact_id)
    if sealed is None:                                            # pragma: no cover — gate-covered
        raise HTTPException(status_code=409,
                            detail={"detail": "the artifact disappeared between the gate and the "
                                              "record; nothing was written"})

    identity_v1 = VerificationExecutionIdentityV1(
        generation_authorization_revision_id=sealed.generation_authorization_revision_id,
        check_set_hash=body.check_set_hash,
        inventory_observation_id=body.inventory_observation_id,
        attempt=body.attempt)
    try:
        execution_hash = record_verification_attempt(
            conn, identity_v1, sealed_artifact_id=body.sealed_artifact_id,
            staging_root=STAGING_ROOT, started_at=_now(conn))
    except StagingPathCollision as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    response.headers[VERIFICATION_ID_HEADER] = execution_hash
    counters.incr("featuregen.verify.requested")
    log("featuregen.verify.requested", execution_hash=execution_hash,
        artifact_id=body.sealed_artifact_id, attempt=body.attempt)
    return {
        "execution_hash": execution_hash,
        "sealed_artifact_id": body.sealed_artifact_id,
        "attempt": body.attempt,
        "staging_path": identity_v1.staging_path(STAGING_ROOT),
        "detail": "the verification request was recorded; a worker executes it",
    }


@router.get("/feature-execution/verifications/{execution_hash}",
            dependencies=[Depends(require_feature_read)])
def verification_result(execution_hash: str, conn: _Conn) -> dict[str, Any]:
    """What became of one verification — and its THREE-WAY staleness, never a boolean.

    ``label`` is ``current`` / ``stale`` / ``unverifiable``, computed by
    :func:`~featuregen.overlay.upload.verification_store.label_for` so the workspace and every other
    surface use one word for one state. ``unverifiable`` is the honest answer for an ``UNPINNED``
    output: nothing was pinned, so no content comparison can say whether its inputs moved.
    """
    attempt = conn.execute(
        "SELECT sealed_artifact_id, attempt, staging_path, started_at FROM verification_attempt "
        "WHERE execution_hash = %s", (execution_hash,)).fetchone()
    if attempt is None:
        raise HTTPException(status_code=404, detail="verification attempt not found")

    output = conn.execute(
        "SELECT revision_id, input_observation_strength, reads_enforced, retention_state "
        "FROM verified_output_revision WHERE execution_hash = %s", (execution_hash,)).fetchone()
    return {
        "execution_hash": execution_hash,
        "sealed_artifact_id": attempt[0],
        "attempt": attempt[1],
        "staging_path": attempt[2],
        "started_at": attempt[3],
        # None is the ORDINARY answer for a request a worker has not finished — never an error, and
        # never a fabricated "pending" verified output.
        "verified_output": None if output is None else {
            "revision_id": output[0],
            "input_observation_strength": output[1],
            "reads_enforced": output[2],
            "retention_state": output[3],
        },
    }


# ── publish ──────────────────────────────────────────────────────────────────────────────────────
class PublicationRequestIn(BaseModel):
    """What a person asks for when they press Publish.

    ``capability_attestation`` is REQUIRED here and absent from the verification body — §0.3's
    asymmetry expressed at the wire, so the difference between the two actions is visible in what
    each one is even able to say.
    """

    model_config = ConfigDict(extra="forbid")

    verified_output_revision_id: str = Field(min_length=1)
    staging_path: str = Field(min_length=1)
    sealed_artifact_id: str = Field(min_length=1)
    environment_id: str = Field(min_length=1)
    logical_group_name: str = Field(min_length=1)
    publish_mechanism: str = Field(min_length=1)
    capability_attestation: str = Field(min_length=1)
    #: The active revision the caller READ. ``None`` only for a group's first publication.
    expected_active_revision_id: str | None = None
    observed_active_revision_id: str | None = None


@router.post("/feature-execution/publications", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def request_publication(
    body: PublicationRequestIn, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Record a publication attempt. ONE of the two callers of ``evaluate_publish_sandbox``.

    The attempt is recorded as ``STARTED``; the worker performs the swap and settles it. That split
    is what makes the uncertain outcome expressible at all — a route that swapped inline would have
    nowhere to record "the swap may or may not have landed" if it died mid-call.
    """
    staleness = _staleness_of(conn, body.verified_output_revision_id)
    verdict = evaluate_publish_sandbox(
        conn, verified_output_revision_id=body.verified_output_revision_id,
        staging_path=body.staging_path, staleness=staleness,
        publication_permitted=_may_publish(identity),
        capability_attestation=body.capability_attestation,
        activation_blockers=())   # explicit-empty is a claim — §8.1; see verify_eligibility
    if not verdict.allowed:
        raise HTTPException(
            status_code=409,
            detail={"detail": "publication is not allowed for this verified output",
                    "blockers": _explained(verdict.blockers),
                    "staleness": staleness.value})

    attempt = PublicationAttemptV1(
        attempt_id=mint_id(_ATTEMPT_PREFIX), environment_id=body.environment_id,
        logical_group_name=body.logical_group_name,
        verified_output_revision_id=body.verified_output_revision_id,
        sealed_artifact_id=body.sealed_artifact_id,
        expected_active_revision_id=body.expected_active_revision_id,
        publish_mechanism=body.publish_mechanism,
        capability_attestation=body.capability_attestation,
        outcome=PublicationOutcomeV1.STARTED)
    try:
        attempt_id = record_publication_attempt(
            conn, attempt, observed_active_revision_id=body.observed_active_revision_id,
            started_at=_now(conn))
    except ActiveRevisionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PublicationBlocked as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    response.headers[PUBLICATION_ID_HEADER] = attempt_id
    counters.incr("featuregen.publish.requested")
    log("featuregen.publish.requested", attempt_id=attempt_id,
        logical_group_name=body.logical_group_name, environment_id=body.environment_id)
    return {
        "attempt_id": attempt_id,
        "logical_group_name": body.logical_group_name,
        "environment_id": body.environment_id,
        "outcome": PublicationOutcomeV1.STARTED.value,
        "detail": "the publication attempt was recorded; a worker performs the swap and settles it",
    }


@router.get("/feature-execution/publications",
            dependencies=[Depends(require_feature_read)])
def publication_status(
    environment_id: str, logical_group_name: str, conn: _Conn,
) -> dict[str, Any]:
    """Whether a group is blocked, and by which attempt.

    The question a workspace asks before offering Publish: an unreconciled attempt means nobody
    knows whether its swap landed, and the remedy is to reconcile it against the published
    generation marker rather than to try again.
    """
    blocking = blocking_attempt(
        conn, environment_id=environment_id, logical_group_name=logical_group_name)
    return {
        "environment_id": environment_id,
        "logical_group_name": logical_group_name,
        "blocked": blocking is not None,
        "blocking_attempt_id": None if blocking is None else blocking[0],
        "blocking_outcome": None if blocking is None else blocking[1].value,
        "detail": ("nothing is outstanding for this group" if blocking is None else
                   "an unreconciled attempt is outstanding: nobody knows whether its swap landed, "
                   "so it must be reconciled against the published generation marker before a "
                   "retry can be recorded"),
    }


def _staleness_of(conn: psycopg.Connection, verified_output_revision_id: str) -> StalenessV1:
    """S9's three-way answer for one output, from what is stored.

    An output nobody can find is ``NEITHER`` rather than an exception: the evaluator's own
    ``VERIFICATION_NOT_CURRENT`` is the honest refusal for that, and raising here would turn a
    governed refusal into a 500.
    """
    row = conn.execute(
        "SELECT input_observation_strength, pinned_policy_hashes FROM verified_output_revision "
        "WHERE revision_id = %s", (verified_output_revision_id,)).fetchone()
    if row is None:
        return StalenessV1.NEITHER
    if row[0] == "unpinned":
        return StalenessV1.NEITHER

    # THE COMPARISON THIS FUNCTION IS NAMED FOR. An earlier version returned CURRENT for every
    # pinned output without comparing anything — a positive assurance it had not earned, and the
    # worst possible default: policy drift after a verification is EXACTLY the case this answers,
    # and reporting it as current tells an operator the verification still holds when nobody
    # checked. Found by review.
    pinned = frozenset(row[1] or ())
    if not pinned:
        # A pinned output that names no policies cannot be compared against anything. The column is
        # CHECKed non-empty at the schema, so this is unreachable for rows written through the
        # store — it is here because "unreachable" is a claim about today's writers.
        return StalenessV1.NEITHER

    current = frozenset(r[0] for r in conn.execute(
        "SELECT revision_id FROM policy_realization_current").fetchall())
    if not current:
        # NOTHING is recorded as current, so "has it moved?" has no answer — not "no, it has not".
        # NEITHER is the vocabulary's word for undecidable-on-content, and `is_unverifiable` is what
        # the surfaces render. This is the LIVE state today: no production code publishes a policy
        # realization, so the pointer table is empty and every pinned output is honestly
        # unverifiable rather than dishonestly current.
        return StalenessV1.NEITHER

    # A pinned realization that is no longer the current one for its family is drift, and drift is
    # what makes a passed verification untrue. Any single one is enough.
    return StalenessV1.CURRENT if pinned <= current else StalenessV1.STALE


def _now(conn: psycopg.Connection) -> str:
    """The database's clock, as an ISO string.

    The database's rather than the process's, so two application instances cannot disagree about
    when something started — and so a test can freeze it in one place.
    """
    return str(conn.execute("SELECT now()").fetchone()[0])
