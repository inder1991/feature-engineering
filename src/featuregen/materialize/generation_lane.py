"""§0.10 step 3 — the FENCED V2 GENERATION LANE: a declared build set becomes a sealed artifact.

**The hole this closes.** `generate_v2` runs the whole V2 chain and has no caller anywhere in
`src/`. `request_generation` mints a `generation_request` row and nothing consumes it. Between the
two sits every stage the program has built, wired to nothing. This module is the consumer, and the
producer a trigger surface calls to ask for one.

**Why a dedicated lane and not `process_one`.** The general consumer builds a `HandlerContext` from
`payload["event_id"]` over a run aggregate stream and dead-letters anything without one
(`runtime/dispatch.py`). A generation job has no run stream — its identity is the `generation_request`
row (migration 1092), not an event-sourced aggregate. Three lanes already hit this and solved it the
same way; this is that pattern.

**WHERE IT DIFFERS FROM THE V1 MATERIALIZATION LANE, and why.**

* *This lane OWNS the lifecycle.* V1's `compile_feature_group` advances its own request, so the V1
  handler writes the lifecycle in exactly two places. Nothing in the V2 chain touches
  `generation_request` — `compile_generation_v2` and `generate_v2` are pure functions over a
  connection — so every transition is written here, explicitly, and each one is a compare-and-set
  (`advance_request`) that refuses to move a row that changed underneath it.
* *The lease is a default rather than a derivation.* V1's is sized from the configured L0 build-proof
  timeout because that subprocess dominates a compile. A V2 generation runs no subprocess and calls
  no provider: it restores, admits, compiles, gates, renders and seals, all in-process and bounded by
  the size of the build set. A number here is a statement about the work rather than a second answer
  to a configured one.
* *No mid-work renewal.* For the same reason there is nothing to renew across: the work between
  claim and terminal is bounded, and a lease that expires mid-generation means the sizing is wrong,
  which a renewal would hide.

**THE THREE GUARDS, and they fail differently.** The queue's per-partition unique partial index
refuses a second in-flight job for one `(environment, group)`. The `lease_fence` refuses a terminal
write from a worker whose claim was superseded. And `advance_request`'s compare-and-set refuses a
lifecycle move validated against a status that no longer holds — which is the one that catches a
worker whose lease expired while it was still alive and working.

**NOTHING IS RE-READ THAT WAS DECLARED.** The job payload freezes what a person declared at request
time — the population, the cadence, the availability promise, the empty-window answers, the operand
facts. Re-deriving any of them at claim time would let a generation be built against a world that
changed after somebody asked for it. What is NOT frozen is what the durable rows already own: the
build set's membership, the authorization's group and environment and target mode, and the
deployment's cluster facts. A second copy of any of those would be a second answer.

**AND NONE OF IT RUNS UNLESS THE DEPLOYMENT SAYS SO.** :func:`generation_enabled` is the switch,
default OFF, read by the worker stage before the claim — so a deployment that runs no generation
pays nothing at all, not even an idle query.
"""
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg

from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1
from featuregen.materialize.admission_v2 import AdmittedFeatureV2, admit_artifacts_v2
from featuregen.materialize.authoring_provenance import (
    MemberAuthoringInputV1,
    MemberProvenanceRefused,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.compile.chain import NodeAssemblyInputs
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.generate_v2 import GenerationRefused, generate_v2
from featuregen.materialize.inputs import derive_requirement
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.pilot_v2 import CompiledGenerationV2, compile_generation_v2
from featuregen.materialize.render.project import project_datasets
from featuregen.materialize.restore_formula_v3 import restore_build_set_formulas
from featuregen.materialize.spine import validate_spine_declaration
from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    RequestMovedUnderneath,
    advance_request,
    read_build_set,
    read_request,
)
from featuregen.runtime.queue import (
    GenerationQueueClaim,
    claim_generation,
    complete_generation,
    enqueue_checked,
    fail_generation,
)

# Spelled here and asserted equal to `runtime.queue.GENERATION_QUEUE_HANDLERS` by the suite, for
# that module's stated reason: it cannot import this one, and a rename that left the two out of step
# would free the general consumer to claim a governed generation.
GENERATION_HANDLER = "generation.v2.build"

_NAMESPACE = "generation"

#: One request is one message. Derived rather than supplied so a redelivery, a double-click and a
#: retry of the same attempt are one queue row — `enqueue_checked` refuses this id reused for
#: materially different work, which is the guarantee a content-hashed work-item table would give.
GENERATION_MESSAGE_PREFIX = f"{_NAMESPACE}:"

PAYLOAD_VERSION = 1

#: Bounded work, so one number rather than a derivation. See the module docstring.
LEASE_SECONDS = 300.0

GENERATION_FLAG = "FEATUREGEN_GENERATION_V2_ENABLED"

#: Faults that are the JOB's fault rather than the platform's, and will fail the same way on every
#: redelivery. Retrying them burns the attempt budget to reach the same answer later, so they
#: dead-letter immediately with their reason. `MaterializationRefused` is deliberately NOT here: a
#: governed refusal is a PRODUCT answer that terminalizes the request as REFUSED, not a fault.
#: `AttributeError` is here because it is the shape a mis-wired refusal path takes — reading a
#: field the object does not have — and it fails identically on every redelivery. Classifying it as
#: transient would retry a programming error until the attempt budget ran out, which is exactly what
#: happened when `pilot_v2` read `.code` off an `InvalidOutputV2`.
_DETERMINISTIC = (ValueError, TypeError, KeyError, AttributeError)

__all__ = [
    "GENERATION_HANDLER",
    "GenerationJobV2",
    "GenerationLaneOutcome",
    "decode_job",
    "encode_job",
    "enqueue_generation",
    "generation_enabled",
    "generation_inventory_from_env",
    "generation_message_id",
    "process_generation_once",
]


def generation_message_id(request_id: str) -> str:
    """The ONE message id for a request. Derived, never supplied."""
    if not request_id.strip():
        raise ValueError(
            "a generation message id needs its request id: the id is what makes a redelivery the "
            "same message rather than a second generation")
    return f"{GENERATION_MESSAGE_PREFIX}{request_id}"


# ── the frozen job ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GenerationJobV2:
    """What was asked for, frozen at request time — the queue payload, typed.

    Every field is a DECLARATION the platform does not derive and cannot honestly re-read later.
    What is deliberately absent is anything a durable row already owns: the build set's members, the
    authorization's group, environment and target mode, and the deployment's cluster facts. See the
    module docstring.
    """

    request_id: str
    #: The population. `None` is not "pick one" — `validate_spine_declaration` refuses it, and a
    #: lane that filled one in would be choosing whose rows the features are computed for.
    spine_declaration: Any
    cadence: Any
    availability_promise: Any
    physical_type_policy: str
    #: What a population key with NO in-window rows publishes, per feature name. Required per
    #: feature and possibly `None` (meaning NULL): "had no transactions" and "transacted zero" are
    #: different published answers, and `compile_generation_v2` refuses a mapping that does not
    #: describe exactly the features being compiled.
    empty_values: Mapping[str, str | None]
    #: The governed unit and currency of each operand ref, for output authority.
    operand_facts: Mapping[str, OperandFactsV2]
    engine_id: str
    roles: tuple[str, ...] = ()
    target_aliases: tuple[str, ...] = ()
    policy_realization_ids: Mapping[str, str] = ()
    #: The instant recorded on what this generation produces. A per-request DECLARATION rather than
    #: a clock read at claim time: a redelivered job must seal the same bytes under the same
    #: timestamps, and `now()` at claim time would make one request's two deliveries two artifacts.
    compiled_at: str = ""
    sealed_at: str = ""
    #: The request-time action decision this act runs under (migration 1106). `None` decodes from a
    #: payload frozen before the field existed — and the worker REFUSES it (`ACTION_DECISION_MISSING`)
    #: rather than defaulting, because an act with no decision is a queue bypass however it got here.
    action_decision_revision_id: str | None = None


def encode_job(job: GenerationJobV2) -> dict[str, Any]:
    """The job as plain JSON, ready for the queue payload."""
    from featuregen.materialize.queue_lane import _encode_spine, _plain

    return {
        "version": PAYLOAD_VERSION,
        "request_id": job.request_id,
        "spine_declaration": (None if job.spine_declaration is None
                              else _encode_spine(job.spine_declaration)),
        "cadence": _plain(job.cadence),
        "availability_promise": _plain(job.availability_promise),
        "physical_type_policy": job.physical_type_policy,
        "empty_values": dict(job.empty_values),
        "operand_facts": {ref: _plain(facts) for ref, facts in job.operand_facts.items()},
        "engine_id": job.engine_id,
        "roles": list(job.roles),
        "target_aliases": list(job.target_aliases),
        "policy_realization_ids": dict(job.policy_realization_ids or {}),
        "compiled_at": job.compiled_at,
        "sealed_at": job.sealed_at,
        "action_decision_revision_id": job.action_decision_revision_id,
    }


def decode_job(payload: Mapping[str, Any]) -> GenerationJobV2:
    """The job a worker reads back, or ``ValueError``.

    Every refusal is a ``ValueError`` for the V1 lane's reason: §14's closed vocabularies answer
    governed questions, and "this payload is not a job" is not one of them. The lane classifies it
    as deterministic, so a payload nothing can read dead-letters with its reason rather than being
    retried until the attempt budget runs out.
    """
    from featuregen.materialize.queue_lane import (
        _decode_cadence,
        _decode_promise,
        _decode_spine,
        _mapping,
        _rebuild,
        _text,
        _texts,
    )

    material = _mapping(payload, where="generation job payload")
    version = material.get("version")
    if version != PAYLOAD_VERSION:
        raise ValueError(
            f"generation job payload declares version {version!r} and this worker reads "
            f"{PAYLOAD_VERSION}: the payload is this lane's frozen work item, so a job it cannot "
            f"read WHOLE must be refused rather than generated from the half it understands")

    spine = material.get("spine_declaration")
    empty = _mapping(material.get("empty_values"), where="job empty_values")
    facts = _mapping(material.get("operand_facts"), where="job operand_facts")
    realizations = _mapping(material.get("policy_realization_ids") or {},
                            where="job policy_realization_ids")
    return GenerationJobV2(
        request_id=_text(material.get("request_id"), where="job request_id"),
        spine_declaration=None if spine is None else _decode_spine(spine),
        cadence=_decode_cadence(material.get("cadence")),
        availability_promise=_decode_promise(material.get("availability_promise")),
        physical_type_policy=_text(material.get("physical_type_policy"),
                                   where="job physical_type_policy"),
        # The VALUE may legitimately be None — that is "publishes NULL for an empty window" — so it
        # is not put through `_text`. The KEY may not: a blank feature name matches no member.
        empty_values={_text(name, where="job empty_values key"): value
                      for name, value in empty.items()},
        operand_facts={_text(ref, where="job operand_facts key"):
                       _rebuild(OperandFactsV2, node, where=f"operand facts for {ref}")
                       for ref, node in facts.items()},
        engine_id=_text(material.get("engine_id"), where="job engine_id"),
        roles=tuple(_texts(material.get("roles") or [], where="job roles")),
        target_aliases=tuple(_texts(material.get("target_aliases") or [],
                                    where="job target_aliases")),
        policy_realization_ids={_text(ref, where="job policy_realization_ids key"):
                                _text(value, where=f"realization id for {ref}")
                                for ref, value in realizations.items()},
        compiled_at=_text(material.get("compiled_at"), where="job compiled_at"),
        sealed_at=_text(material.get("sealed_at"), where="job sealed_at"),
        # `.get`, not `_text`: absence is a legal DECODE (an old payload) and an illegal ACT — the
        # worker refuses it by name rather than this codec inventing one.
        action_decision_revision_id=material.get("action_decision_revision_id") or None,
    )


def generation_evidence_pins(
    *, build_set_content_hash: str, generation_authorization_revision_id: str,
) -> dict[str, str]:
    """The pins a GENERATE_PREVIEW decision is taken against — ONE definition, per-action.

    ▲ The route's decide and the worker's recheck must build these from the SAME function, or the
    two dictionaries drift apart the first time either changes and the drift check fires on healthy
    jobs — which teaches people to delete it. Owner ruling 2026-08-23 item 4: evidence assemblers
    are per-action and server-owned.
    """
    return {
        "build_set_content_hash": build_set_content_hash,
        "generation_authorization_revision_id": generation_authorization_revision_id,
    }


def authorize_and_decide_generation(
    conn, *, build_set_revision_id: str, build_set_content_hash: str,
    generation_authorization_revision_id: str, environment_id: str, actor_subject: str,
    member_names: tuple[str, ...] = (),
):
    """Mint the server-owned authorization and RECORD the request-time decision. Returns
    ``(decision_id, decision)``.

    ONE definition, called by the route and by every test that enqueues a job, because a fixture
    that hand-rolled its own pins would drift from the worker's recompute the first time either
    changed — and the drift check would then fire on healthy jobs, which teaches people to delete it.

    ▲ The pins are the two facts whose movement would change the answer, both re-readable
    server-side at worker time: the build set's content hash (which since 1101 folds every member's
    pinned formula) and the generation approval it runs under. The ACTOR is not a pin — the decision
    already binds its authorization, and the authorization binds the actor.
    """
    from featuregen.materialize.action_authorization import ActionV1, authorize_action
    from featuregen.materialize.action_decision import ActionRequestV1, decide

    authorization = authorize_action(
        conn, action=ActionV1.GENERATE_PREVIEW,
        resource_identity_hash=build_set_revision_id,
        actor_subject=actor_subject, environment_id=environment_id)
    return decide(
        conn,
        ActionRequestV1(
            action=ActionV1.GENERATE_PREVIEW,
            resource_identity_hash=build_set_revision_id,
            # ▲ Every member BY NAME, clean ones included — a member absent from the record is a
            # member the decision cannot be shown to have covered.
            member_names=member_names,
            evidence_pins=generation_evidence_pins(
                build_set_content_hash=build_set_content_hash,
                generation_authorization_revision_id=generation_authorization_revision_id)),
        authorization_id=authorization.authorization_id)


# ── the producer ─────────────────────────────────────────────────────────────────────────────────


def enqueue_generation(
    conn: psycopg.Connection,
    *,
    job: GenerationJobV2,
    environment_id: str,
    logical_group_name: str,
    priority: int = 100,
) -> int:
    """Freeze the job onto the queue, in the caller's transaction. Returns the queue row id.

    Called in the SAME transaction that mints the request, so the request and its work item commit
    together — there is no window in which a request exists that nothing will ever pick up, and none
    in which a job names a request that was rolled back.

    Partitioned by ``(environment, logical group)``, which is what the in-flight exclusion is FOR:
    sealing is atomic per group per environment, and two concurrent generations of one group would
    race to publish the same columns.

    Raises:
        QueueIdempotencyConflict: this request id was already enqueued with materially different
            work. The message id is derived from the request, so that means one request id was used
            for two different jobs — and generating either would be generating something nobody
            asked for under an id somebody is watching.
    """
    return enqueue_checked(
        conn,
        message_id=generation_message_id(job.request_id),
        partition_key=f"{_NAMESPACE}:{environment_id}:{logical_group_name}",
        handler=GENERATION_HANDLER,
        payload=encode_job(job),
        priority=priority,
    )


# ── the kill switch ──────────────────────────────────────────────────────────────────────────────


def generation_enabled() -> bool:
    """Whether this deployment runs V2 generation at all. Default **OFF**.

    THE ONE PUBLIC DEFINITION, so a route and a worker cannot read two different switches — the
    second of which is the one nobody flips.

    Read ABOVE the claim rather than inside it, so an unconfigured deployment costs nothing: not a
    query, not a counter, not a log line. A switch consulted after the claim would already have
    leased a job it then had to release.
    """
    return os.environ.get(GENERATION_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def generation_inventory_from_env() -> ClusterInventoryV1:
    """The deployment's cluster facts, from the SAME variable the V1 lane reads.

    The same one deliberately. §0's captured cluster facts describe an ENVIRONMENT — its id, its
    engine versions, its table layouts — and a deployment has one environment, not one per lane. A
    second variable would let the two lanes render against different readings of one cluster, and
    the disagreement would surface as an artifact pinning runtimes nobody is running.

    Read at the boundary and only once a job has been claimed, so a deployment that configures no
    generation costs one idle claim query per tick and never a stage error — and one that IS
    misconfigured finds out against a request, with the variable named.
    """
    from featuregen.materialize.inventory import load_inventory
    from featuregen.materialize.queue_lane import _INVENTORY_ENV

    path = os.environ.get(_INVENTORY_ENV, "").strip()
    if not path:
        raise ValueError(
            f"{_INVENTORY_ENV} is not set: §0's captured cluster facts are a typed input every "
            f"generation reads (environment id, engine versions, table layouts), and they live in "
            f"a YAML file a human wrote — there is nothing to infer them from")
    return load_inventory(path)


# ── the consumer ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class GenerationLaneOutcome:
    """What one tick of the lane did. ``status`` is the lane's answer, not the request's."""

    #: "idle" | "generated" | "refused" | "failed" | "retryable" | "unclaimable"
    status: str
    request_id: str | None = None
    artifact_id: str | None = None
    detail: str = ""


def process_generation_once(
    conn: psycopg.Connection,
    *,
    owner: str,
    inventory: ClusterInventoryV1,
    lease_seconds: float = LEASE_SECONDS,
) -> GenerationLaneOutcome:
    """Claim ONE generation job and drive it to a terminal request, or report why not.

    Returns rather than raises on every governed outcome: a refused build is a PRODUCT answer and a
    lane that raised it would be indistinguishable from one that broke.

    The order below is the whole of it, and each step's failure mode is different:

    1. **Claim**, fenced. No claim is idle, and idle costs one query.
    2. **Decode**, deterministically. A payload nothing can read dead-letters with its reason.
    3. **Read the request.** A job naming a request nobody recorded is permanent — a redelivery
       would find the same absence.
    4. **Take the lifecycle**, REQUESTED → CLAIMED, by compare-and-set. Losing that race means
       another worker holds this request, and this one stops without touching it.
    5. **Generate**, CLAIMED → RUNNING first, so a request that is being worked on says so.
    6. **Terminalize**: SUCCEEDED with the artifact, REFUSED with what refused it, or FAILED.

    **THE CALLER COMMITS**, as every other lane in this codebase does — nothing here commits or
    rolls back. One tick is one transaction, so a tick that dies leaves neither a claimed queue row
    nor a half-advanced request: the whole thing is simply undone and the job is delivered again.

    **EVERY TERMINAL LIFECYCLE WRITE IS GATED ON THE QUEUE WRITE**, in that order, which is the V1
    lane's rule and holds for its reason: if the fence has moved, another worker owns both the row
    and the request, and writing the request's outcome would be this worker reaching into somebody
    else's claim to record a result that worker did not produce.
    """
    claim = claim_generation(conn, owner=owner, lease_seconds=lease_seconds)
    if claim is None:
        return GenerationLaneOutcome("idle")

    try:
        job = decode_job(claim.payload)
    except ValueError as exc:
        fail_generation(conn, claim, error=f"undecodable job: {exc}", permanent=True)
        return GenerationLaneOutcome("failed", detail=str(exc))

    request = read_request(conn, job.request_id)
    if request is None:
        fail_generation(
            conn, claim,
            error=f"generation request {job.request_id} does not exist", permanent=True)
        return GenerationLaneOutcome(
            "failed", request_id=job.request_id,
            detail="the job names a request nobody recorded")

    if request.status.is_terminal:
        # A REDELIVERY of work that already finished. Completing the queue row rather than failing
        # it: nothing is wrong, the message simply arrived twice, and re-running would seal a second
        # artifact for one request.
        complete_generation(conn, claim)
        return GenerationLaneOutcome(
            "unclaimable", request_id=job.request_id,
            detail=f"already {request.status.value}")

    # ▲ THE READ IS THE CHEAP CHECK; THE COMPARE-AND-SET BELOW IS THE GUARANTEE. Both are here, and
    # neither is redundant. Without this read, a request another worker already moved to CLAIMED
    # would reach `advance_request` as CLAIMED → CLAIMED — which the LIFECYCLE refuses, as
    # `InvalidStatusMove`, meaning "the caller asked for a step that does not exist". The lane
    # classifies that as deterministic and DEAD-LETTERS the job, so a message whose only problem is
    # that somebody else got there first would be permanently discarded. Distinguishing the two is
    # not cosmetic: one is a bug in this code and the other is the design working.
    if request.status is not GenerationStatusV1.REQUESTED:
        fail_generation(
            conn, claim, permanent=False,
            error=f"request {job.request_id} is {request.status.value}: another worker holds it")
        return GenerationLaneOutcome(
            "unclaimable", request_id=job.request_id,
            detail=f"another worker holds it ({request.status.value})")

    try:
        advance_request(conn, job.request_id, GenerationStatusV1.CLAIMED)
    except RequestMovedUnderneath as exc:
        # The read above was stale — somebody claimed it in between — and the compare-and-set is
        # what caught that. Release the queue row for a later delivery rather than failing it: the
        # work is not wrong, it is not ours.
        fail_generation(conn, claim, error=str(exc), permanent=False)
        return GenerationLaneOutcome("unclaimable", request_id=job.request_id, detail=str(exc))

    try:
        return _drive(conn, claim, job, request, inventory=inventory)
    except _DETERMINISTIC as exc:
        # THE QUEUE WRITE GOES FIRST AND GATES THE LIFECYCLE WRITE, which is the V1 lane's rule and
        # holds for its reason: if the fence has moved, another worker owns both the row and the
        # request, and failing a request it just took would be this worker reaching into somebody
        # else's claim.
        error = f"{type(exc).__name__}: {exc}"
        if fail_generation(conn, claim, error=error, permanent=True):
            _terminalize(conn, job.request_id, GenerationStatusV1.FAILED, failure_reason=error)
        return GenerationLaneOutcome("failed", request_id=job.request_id, detail=str(exc))
    except Exception as exc:                                    # noqa: BLE001 — see below
        # TRANSIENT until proven otherwise. A deployment fault (a dropped connection, a full disk)
        # must leave a request the retry can actually take, so the lifecycle is NOT terminalized
        # here and the queue row goes back to ready with backoff. Terminalizing on an unknown fault
        # would burn a request for a reason that may not exist by the next delivery.
        fail_generation(conn, claim, error=f"{type(exc).__name__}: {exc}", permanent=False)
        return GenerationLaneOutcome("retryable", request_id=job.request_id, detail=str(exc))


def _drive(
    conn: psycopg.Connection,
    claim: GenerationQueueClaim,
    job: GenerationJobV2,
    request,
    *,
    inventory: ClusterInventoryV1,
) -> GenerationLaneOutcome:
    """The generation itself, from a claimed request to a terminal one."""
    from featuregen.materialize.generation_authorization import load_generation_authorization

    authorization = load_generation_authorization(
        conn, request.generation_authorization_revision_id)
    if authorization is None:
        raise ValueError(
            f"request {job.request_id} names authorization "
            f"{request.generation_authorization_revision_id!r} and no such approval exists: the "
            f"composite key makes this unwritable, so finding it means the row was reached another "
            f"way")

    build_set = read_build_set(conn, request.build_set_revision_id)
    if build_set is None:
        raise ValueError(
            f"request {job.request_id} names build set {request.build_set_revision_id!r} and no "
            f"such set exists: its membership is what would be built, so there is nothing to build")

    advance_request(conn, job.request_id, GenerationStatusV1.RUNNING)

    # ── 0. THE SECOND LOOK (§8.2): the request-time decision, re-checked before the act ─────────
    # Same service, second moment. The pins are recomputed from what the DATABASE says now — never
    # from the payload, which is the thing a bypass would have written.
    from featuregen.materialize.action_decision import DecisionDrift, DecisionMissing, recheck

    # ▲ THE ROW IS AUTHORITATIVE, the payload is the fallback (1108). The queue row is the work
    # item; generation_request is the record — and the record must answer "which decision did this
    # attempt run under" even after the message is redelivered, dead-lettered or reaped. The payload
    # copy remains for jobs frozen before 1108's column existed.
    decision_id = request.action_decision_revision_id or job.action_decision_revision_id
    try:
        if decision_id is None:
            raise DecisionMissing(
                f"generation job {job.request_id} carries no action decision: work submitted "
                f"straight to the queue is refused at the worker, not built")
        decision = recheck(conn, decision_id, current_pins=generation_evidence_pins(
            build_set_content_hash=build_set.content_hash,
            generation_authorization_revision_id=request.generation_authorization_revision_id))
    except DecisionMissing as exc:
        return _refuse(conn, claim, job, MaterializationRefused(
            CompilationRefusalCode.ACTION_DECISION_MISSING, str(exc)))
    except DecisionDrift as exc:
        # ▲ REFUSED, never re-decided. Re-evaluating moved evidence usually returns "allowed" again,
        # and the act then proceeds under a verdict nobody was shown.
        return _refuse(conn, claim, job, MaterializationRefused(
            CompilationRefusalCode.ACTION_DECISION_DRIFTED, str(exc)))

    # ── 1. THE MEMBERS, rehydrated from the write-once authoring trace ──────────────────────────
    try:
        # ▲ THE PINS, not the selections. Passing selections made the lane resolve "the newest
        # draft" at build time, so a re-author between the decision and this moment changed what
        # the build meant under an unchanged build-set identity (migration 1101).
        restored = restore_build_set_formulas(
            conn, selection_formula_binding_ids=build_set.selection_formula_binding_ids)
    except MaterializationRefused as refusal:
        return _refuse(conn, claim, job, refusal)

    # ── 2. ADMISSION, whose provenance proof is the anti-forgery check ──────────────────────────
    try:
        admitted = admit_artifacts_v2(
            conn, [item.input for item in restored], engine_id=job.engine_id)
    except MaterializationRefused as refusal:
        return _refuse(conn, claim, job, refusal)

    # ── 3. THE POPULATION ───────────────────────────────────────────────────────────────────────
    spine = validate_spine_declaration(conn, job.spine_declaration, roles=job.roles)
    if isinstance(spine, MaterializationRefused):
        return _refuse(conn, claim, job, spine)

    # ── 4. COMPILE, AUTHORIZE, PLAN, SHAPE ──────────────────────────────────────────────────────
    compiled = compile_generation_v2(
        conn, admitted,
        spine=spine,
        inventory=inventory,
        authorization=authorization,
        cadence=job.cadence,
        availability_promise=job.availability_promise,
        physical_type_policy=job.physical_type_policy,
        empty_values=job.empty_values,
        operand_facts=job.operand_facts,
        logical_group_name=authorization.logical_group_name,
        roles=job.roles,
        target_aliases=job.target_aliases,
        policy_realization_ids=job.policy_realization_ids)
    if isinstance(compiled, MaterializationRefused):
        return _refuse(conn, claim, job, compiled)

    # ── 5. GATE, RECORD, RENDER, SEAL ───────────────────────────────────────────────────────────
    spine_input = derive_requirement(
        conn, inventory, table_ref=spine.source_table_ref)
    nodes = assemble_nodes(NodeAssemblyInputs(
        authorized=compiled.authorized.token,
        plan=compiled.plan,
        contract=compiled.contract,
        admitted={feature.feature_name: feature for feature in admitted},
        spine_input=spine_input,
        datasets=project_datasets(compiled.authorized.token, compiled.plan,
                                  spine_input=spine_input)))

    try:
        produced = generate_v2(
            conn, compiled,
            environment_id=request.environment_id,
            generation_authorization_revision_id=request.generation_authorization_revision_id,
            engine_id=job.engine_id,
            engine_versions=inventory.engine_versions,
            spine_input=spine_input,
            nodes=nodes,
            artifact_id=_artifact_id(job.request_id),
            occurrences_by_member=_occurrences(
                compiled, admitted, environment_id=request.environment_id),
            # ▲ FROM THE DECISION, explicitly — §8.1. The same recorded answer a person was shown
            # at request time, rechecked moments ago; an empty tuple here is that decision's claim
            # that nothing carried, never a default nobody chose.
            activation_blockers=tuple(decision.blockers),
            realizations=(),
            member_provenance=_member_provenance(restored, admitted),
            compiled_at=job.compiled_at,
            sealed_at=job.sealed_at)
    except GenerationRefused as refusal:
        return _refuse_gate(conn, claim, job, refusal)
    except MemberProvenanceRefused as refusal:
        # ▲ A GOVERNED REFUSAL, not a fault. A member whose authoring method cannot be established
        # from its run's evidence is a build that cannot be certified, and a redelivery would reach
        # the same answer from the same immutable trace — so the request terminalizes REFUSED with
        # the reason rather than being retried until the attempt budget runs out. Nothing was
        # sealed: `seal_v2` derives before it stores anything.
        return _refuse(conn, claim, job, MaterializationRefused(
            CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE, str(refusal)))

    if complete_generation(conn, claim):
        _terminalize(conn, job.request_id, GenerationStatusV1.SUCCEEDED,
                     sealed_artifact_id=produced.sealed.artifact_id)
    return GenerationLaneOutcome(
        "generated", request_id=job.request_id, artifact_id=produced.sealed.artifact_id)


def _artifact_id(request_id: str) -> str:
    """DERIVED from the request, so a redelivery seals the same id rather than a second artifact.

    A retry is a NEW request against the same build set and therefore a new artifact, which is
    correct: the two attempts produced different bytes at different times, and collapsing them would
    make "which attempt produced what is running" unanswerable.
    """
    return f"{_NAMESPACE}:{request_id}"


def _member_provenance(
    restored: Sequence[Any],
    admitted: Sequence[AdmittedFeatureV2],
) -> tuple[MemberAuthoringInputV1, ...]:
    """What each member's provenance row is made of — from the two stages that own the halves.

    ▲ **THIS IS THE ONLY PLACE BOTH HALVES EXIST.** `RestoredFormulaV3` knows which SELECTION a
    person made and which DRAFT answered it; `AdmittedFeatureV2` knows the published feature name
    and the formula hash its provenance PROOF verified against the immutable trace. Neither carries
    the other's half, and admission deliberately drops the selection — so a stage below this one
    could only recover it by asking "which draft is current for this selection now", which is a
    different question with a possibly different answer.

    ▲ **AND NOTHING HERE READS `generation_source`.** Where the IDEA came from — recipe, llm_intent,
    user_defined — is not how the FORMULA was written: `formula_draft_worker` always drives the LLM
    author and critic, so a recipe-origin feature is still LLM-authored. The method is not decided
    here at all; `seal_v2` derives it from the run's own evidence.

    Paired POSITIONALLY, because that is what the two stages guarantee: `restore_build_set_formulas`
    preserves the build set's declared order and `admit_artifacts_v2` maps its inputs one for one,
    in order. The run ids are compared anyway — a silent re-pairing would attach one member's
    selection to another member's formula, and every row would still look well-formed.
    """
    provenance: list[MemberAuthoringInputV1] = []
    for item, feature in zip(restored, admitted, strict=True):
        restored_run = item.input.result.authoring_run_id
        if restored_run != feature.authoring_run_id:
            raise ValueError(
                f"selection {item.selection_revision_id} restored authoring run {restored_run!r} "
                f"and the admitted feature {feature.feature_name!r} names {feature.authoring_run_id!r}: "
                f"the two stages have come out of step, and pairing them anyway would file one "
                f"member's selection against another member's formula")
        provenance.append(MemberAuthoringInputV1(
            member_name=feature.feature_name,
            selection_revision_id=item.selection_revision_id,
            formula_draft_id=item.formula_draft_id,
            authoring_run_id=feature.authoring_run_id,
            # THE VERIFIED HASH, not the draft's stored one. Admission recomputed it from the
            # proposal and proved it against the trace's immutable payload; the draft's copy is what
            # a person was shown. `restore_formula` refuses when they disagree, so here they agree —
            # and taking the proven one keeps that true if the restorer's check ever moves.
            formula_content_hash=feature.proposal_content_hash))
    return tuple(provenance)


def _occurrences(
    compiled: CompiledGenerationV2,
    admitted: Sequence[Any],
    *,
    environment_id: str,
) -> Mapping[str, PolicyOccurrenceSetV1]:
    """Each member's policy occurrences, DECLARED by its formula and BOUND to what it compiled to.

    The two halves come from the two places that own them, and neither is re-derived. The
    DECLARATIONS are the proposal's `authority_refs`, read through `body_paths_v2` so the paths are
    the compiler's own. The BINDINGS are the compiled IR's source relations — `derive_policy_
    occurrences` requires them precisely because binding a logical relation to a physical dataset
    is the generation inventory's job, and an occurrence that guessed would name a dataset nobody
    bound.

    `evaluate_generate` gates each member against this set, so a member whose set was assembled
    from a second reading would be gated against occurrences its compilation never had — and an
    empty set passes.
    """
    from featuregen.formula.policy_occurrences import derive_policy_occurrences
    from featuregen.materialize.compile_ir_v2 import body_paths_v2

    proposals = {feature.feature_name: feature.proposal for feature in admitted}
    by_member: dict[str, PolicyOccurrenceSetV1] = {}
    for planned in compiled.authorized.token.ordered_planned():
        name = planned.ir.feature_name
        proposal = proposals.get(name)
        if proposal is None:
            raise ValueError(
                f"the group compiled {name!r} and no admitted artifact supplied it: the policy "
                f"declarations live on the formula, and a member gated against an empty set is a "
                f"member whose policy references were never checked")
        expressions = dict(body_paths_v2(proposal.body))
        by_member[name] = derive_policy_occurrences(
            expressions,
            bound_datasets={expression.expr_path: _bound_dataset(expression)
                            for expression in planned.ir.expressions},
            environment_id=environment_id)
    return by_member


def _bound_dataset(expression) -> str:
    """The ``schema.table`` an expression reads, off its own compiled read set.

    The SOURCE relation specifically — the one relation §2.1 records as a read of its own. A policy
    applies to the rows being aggregated, not to a table reached over a join on the way to them.
    """
    from featuregen.materialize.expression_ir import RefRole

    sources = {f"{ref.schema}.{ref.table}" for ref in expression.physical_read_set
               if RefRole.SOURCE_TABLE in ref.roles}
    if len(sources) != 1:
        raise ValueError(
            f"expression {expression.expr_path!r} names {sorted(sources)} as its source relation: "
            f"a policy occurrence binds to ONE dataset, and neither zero nor two is one")
    return sources.pop()


def _refuse(
    conn: psycopg.Connection,
    claim: GenerationQueueClaim,
    job: GenerationJobV2,
    refusal: MaterializationRefused,
) -> GenerationLaneOutcome:
    """A GOVERNED refusal: the build could not be made from what was asked for.

    The queue row COMPLETES rather than fails. A refusal is a product answer that a redelivery would
    reproduce exactly — the formulas, the policies and the catalog are the same — so retrying it
    would spend the attempt budget to reach the same verdict, and dead-lettering it would file a
    correct answer as a platform fault.
    """
    detail = f"{refusal.code.value}: {refusal}"
    if complete_generation(conn, claim):
        _terminalize(conn, job.request_id, GenerationStatusV1.REFUSED,
                     refusals=[{"code": refusal.code.value, "detail": str(refusal)}])
    return GenerationLaneOutcome("refused", request_id=job.request_id, detail=detail)


def _refuse_gate(
    conn: psycopg.Connection,
    claim: GenerationQueueClaim,
    job: GenerationJobV2,
    refusal: GenerationRefused,
) -> GenerationLaneOutcome:
    """The Generate gate refused, and `generate_v2` guarantees nothing was recorded.

    EVERY blocker is carried, not the fact that there were some: an operator reading this needs to
    know which member and which requirements, because fixing one of four is four round trips.
    """
    if complete_generation(conn, claim):
        _terminalize(
            conn, job.request_id, GenerationStatusV1.REFUSED,
            refusals=[{"code": blocker, "detail": f"member {refusal.member}"}
                      for blocker in refusal.verdict.blockers])
    return GenerationLaneOutcome(
        "refused", request_id=job.request_id,
        detail=f"{refusal.member}: {', '.join(refusal.verdict.blockers)}")


def _terminalize(
    conn: psycopg.Connection,
    request_id: str,
    status: GenerationStatusV1,
    *,
    sealed_artifact_id: str | None = None,
    refusals: Sequence[Mapping[str, str]] = (),
    failure_reason: str | None = None,
) -> None:
    """Write the terminal status, tolerating a request another writer already finished.

    `RequestMovedUnderneath` is swallowed HERE and nowhere else. Everywhere above, losing the race
    means this worker must stop touching a request somebody else owns. Here the work is already
    done — the artifact is sealed or the refusal is decided — and the only question left is who
    records it. Raising would turn a completed generation into a lane fault.
    """
    try:
        advance_request(conn, request_id, status, sealed_artifact_id=sealed_artifact_id,
                        refusals=refusals, failure_reason=failure_reason)
    except RequestMovedUnderneath:
        return
