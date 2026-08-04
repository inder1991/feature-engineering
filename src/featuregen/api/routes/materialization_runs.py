"""Phase G §3.1 — the TRIGGER SURFACE: materialization becomes something a person can invoke.

``POST /materialization-runs`` mints the durable request and enqueues the job; ``GET
/materialization-runs/{request_id}`` reports what became of it. Both are flag-gated, both are
``require_confirmer``-gated, and neither runs a compile.

**THE ROUTE MUST NOT RUN THE CHAIN, and that is settled on evidence rather than on taste.**
``get_conn`` holds ONE connection and ONE transaction open for the whole life of a request
(``api/deps.py:100-116``), and a compile is bounded by ``COMPILE_BUDGET_SECONDS`` (600s) plus the
deployment's configured L0 timeout — with ``LocalClusterSubmitter.timeout_seconds`` defaulting to
3600s once G-2 lands. A route that compiled inline would hold a database transaction open for tens
of minutes, and a client disconnect or an app restart would orphan the work with a half-written
project tree behind it. So this surface is a PRODUCER: it validates what is cheap to validate,
freezes the declarations onto the queue in the same transaction that mints the request, and returns
``202``. The worker (``materialize/queue_lane.py``) is the only thing that compiles. The test suite
reads this module's AST and asserts it never names ``compile_feature_group``.

**ONE SWITCH, NOT TWO.** :func:`~featuregen.materialize.queue_lane.materialization_enabled` is
imported and consulted — the same function object the worker stage calls, over the same truthy set
(``{"1", "true", "yes", "on"}`` after ``.strip().lower()``). A route that re-implemented the check as
``== "1"`` would 404 for a deployment that wrote ``FEATUREGEN_MATERIALIZE_ENABLED=true`` while its
worker happily drained the backlog — a split brain whose symptom ("the API says this feature does
not exist") points nowhere near its cause. The switch is also the FIRST dependency of every route
here, ahead of authentication and body validation, so a flag-off deployment opens no connection,
parses no declaration and is indistinguishable from one where these paths were never registered.

**WHY 404 AND NOT 503.** A 503 says "this exists and is unwell"; the flag says "this deployment does
not run materialization at all". The refusal body is Starlette's own ``{"detail": "Not Found"}``,
byte for byte, so the surface leaks nothing about whether the feature is built.

**THE MEMBERS ARE SUPPLIED, BECAUSE NOTHING DURABLE MAPS THEM (Task 2's §15.2, unresolved).** There
is no group→member table anywhere in the tree: ``materialization_request`` carries the group NAME,
``group_binding`` maps that name to a contract hash and a physical target, and
``group_plan_revision`` records a plan HASH — none of them stores membership, and ``work_item_id``
appears in no schema outside ``recipe_formula_shadow_work_item`` itself. ``resolve_feature_inputs``
therefore takes a set of work-item ids, ``compile_feature_group`` says in its own docstring that
"nothing maps a logical group to its members yet, so the caller supplies them", and so does this
route. What it CAN do — and does, before minting anything — is prove that every id names a real
``recipe_formula_shadow_work_item`` row, which is the one durable anchor from which both the
authoring run and the authoring intent are recoverable (``materialize/resolve.py``). Inventing a
mapping table here would need a migration this task may not add, and guessing membership from a
previous generation's plan would compile a membership nobody decided.

**A 202 IS A PROMISE THAT SOMETHING WILL RUN, so it is only made when something will.**
``record_request`` returns the EXISTING row on an idempotency hit (correctly: a retried HTTP call
must not become a second run) and ``enqueue_checked`` returns the EXISTING queue id — including one
the worker has dead-lettered. Composed naively, those two correct behaviours produce a route that
answers ``202`` over a job no worker will ever claim again, forever, silently.
:func:`_refuse_if_nothing_will_run` is the one place that rule lives: a request is accepted only
when it is non-terminal AND its queue row is still claimable. Everything else is a ``409`` that
NAMES what it found and says what to do about it. See that function for why re-arming the dead row
was rejected, and why minting a fresh key on the caller's behalf is worse than either.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.control_plane import fold_run_status, read_run_events
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    PAYLOAD_VERSION,
    MaterializationJobV1,
    decode_job,
    enqueue_materialization,
    materialization_enabled,
)
from featuregen.materialize.request_store import (
    MaterializationRequestV1,
    read_request,
    record_request,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.queue import QueueIdempotencyConflict

#: The 202's id, echoed as a header for the same reason ``X-Ingestion-Run-Id`` is
#: (``uploads.py:157``): a client that lost the body can still name the work it started.
REQUEST_ID_HEADER = "X-Materialization-Request-Id"

#: The id prefix. A materialization request is not the Phase-06 ``req_`` aggregate and must not be
#: confusable with one in a log line.
_REQUEST_PREFIX = "mreq"

#: The queue statuses from which a worker can still reach the job. ``done`` and ``dead`` are the two
#: it cannot: the first was drained, the second was dead-lettered.
_CLAIMABLE = frozenset({"ready", "leased"})

#: The single durable anchor a member id names — the only table from which BOTH the authoring run id
#: and the authoring intent are recoverable (``materialize/resolve.py``). Read-only here.
_MEMBERS_EXIST = (
    "SELECT work_item_id FROM recipe_formula_shadow_work_item WHERE work_item_id = ANY(%s)")


def require_materialization_enabled() -> None:
    """The deployment's switch, consulted BEFORE anything else on every route in this module.

    Flag off is not a degraded mode and not a permission failure: it is a deployment that does not
    run materialization, and the honest HTTP answer is that these paths are not there. The body is
    Starlette's own 404 payload verbatim so the two are indistinguishable.
    """
    if not materialization_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(require_materialization_enabled)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


class MaterializationRunIn(BaseModel):
    """What a trigger declares. Everything here is a DECISION the platform cannot derive.

    The four declaration fields are carried as plain JSON objects and handed to the LANE's own
    :func:`~featuregen.materialize.queue_lane.decode_job` rather than re-described as Pydantic
    models. That is deliberate: the queue payload IS this lane's frozen work item, and a second
    description of its shape at the trigger would be the first thing to drift from the shape the
    worker reads. Validating through the worker's reader means a body this route accepts is, by
    construction, a job that worker can decode whole.
    """

    #: §10.1's publication unit. Publication is atomic per group and ``authorize_compilation``
    #: authorizes a group-wide read set, so the trigger is per-group and never per-feature.
    logical_group_name: str = Field(min_length=1)
    #: The group's members, as ``recipe_formula_shadow_work_item`` ids. SUPPLIED, because nothing
    #: durable maps a group to its members — see the module docstring.
    work_item_ids: list[str]
    #: What stops one retried call from becoming two runs. The caller's, not this route's: a key
    #: minted here would make every retry a new run.
    idempotency_key: str = Field(min_length=1)
    #: §5's two declarations, and §4's population.
    cadence: dict[str, Any]
    availability_promise: dict[str, Any]
    mechanism: str = Field(min_length=1)
    spine_declaration: dict[str, Any] | None = None
    #: The live column list of the currently published table; ``None`` for "no table yet".
    published_schema: list[str] | None = None
    #: §5.4's declared tightenings, if any.
    contract_overrides: dict[str, Any] | None = None


@router.post("/materialization-runs", status_code=202,
             dependencies=[Depends(require_confirmer)])
def trigger_materialization(
    body: MaterializationRunIn, response: Response, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Mint the request, freeze the job onto the queue, and return ``202`` — never compile.

    The pre-flight is everything that is CHEAP and DURABLE, run before the request row is minted so
    a typo leaves nothing behind: the group is well formed, every member names a real work item, and
    the declarations decode as a job. What it deliberately does not do is resolve, admit or
    authorize — those read the whole governed catalog per member and are the compile, not a check on
    it.

    The request row and the queue row are written in the SAME transaction (``get_conn``'s), so there
    is no window in which a request exists with nobody to drive it, and no window in which a queue
    row names a request that is not there.
    """
    members = _well_formed_group(body.work_item_ids)
    _require_every_member_exists(conn, members)
    minted = mint_id(_REQUEST_PREFIX)
    job = _job(minted, members, body)

    recorded = _record(conn, minted, body, members, identity)
    duplicate = recorded.request_id != minted
    if duplicate:
        # The stored request is the one being answered about, and the message id is derived from it.
        job = dataclasses.replace(job, request_id=recorded.request_id)
    _refuse_if_nothing_will_run(recorded)

    try:
        queue_id = enqueue_materialization(conn, recorded, job=job)
    except QueueIdempotencyConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=f"idempotency key {body.idempotency_key!r} already named materialization "
                   f"request {recorded.request_id!r} with DIFFERENT declarations, so this call "
                   f"cannot be answered with it ({exc}). Re-trigger with a fresh idempotency key",
        ) from exc
    _refuse_if_nothing_will_run(recorded, queue_status=_queue_status(conn, queue_id))

    response.headers[REQUEST_ID_HEADER] = recorded.request_id
    counters.incr("materialize.trigger.duplicate" if duplicate else "materialize.trigger.queued")
    log("materialize.trigger.queued", request_id=recorded.request_id,
        logical_group_name=recorded.logical_group_name, members=len(members),
        duplicate=duplicate, queue_id=queue_id)
    return {
        "request_id": recorded.request_id,
        "logical_group_name": recorded.logical_group_name,
        "lifecycle_state": recorded.lifecycle_state.value,
        "work_item_ids": list(members),
        "queue_id": queue_id,
        "duplicate": duplicate,
        "detail": ("this idempotency key already named a materialization request; it is still "
                   "queued and nothing was enqueued a second time" if duplicate else
                   "the materialization request was recorded and its job enqueued; a worker "
                   "compiles it"),
    }


@router.get("/materialization-runs/{request_id}", dependencies=[Depends(require_confirmer)])
def materialization_run_detail(request_id: str, conn: _Conn) -> dict[str, Any]:
    """What became of one request — its coordination state, and the plane's verdict when there is one.

    **THE TWO RECORDS ARE NOT MERGED, because they answer different questions.**
    ``materialization_request`` says what is being ATTEMPTED (who asked, is it claimed, has its lease
    expired); the append-only control plane says what HAPPENED. So ``lifecycle_state`` is read off
    the row and ``run_status`` is FOLDED from the events — never a second status stored anywhere.

    **AND AN EMPTY PLANE IS A NORMAL ANSWER (Task 3's trap).** A refusal before the project is sealed
    has nowhere in the plane to be recorded — ``materialization_run_event.generation_id`` is a
    foreign key to a generation row that only exists once a project has been rendered and hashed — so
    a request can be terminally ``failed`` with no generation and no run event at all. That is the
    ordinary shape of a governed refusal, not a corrupt record. ``fold_run_status`` RAISES on an
    empty stream by design ("reporting one would be an invention about a run nobody recorded"), so
    this route never hands it one: it reads the events first and, when there are none, reports
    ``run_status: null`` alongside a reason that says which silence it is.
    """
    stored = read_request(conn, request_id=request_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="materialization request not found")
    status, reason = _run_verdict(conn, stored)
    return {
        "request_id": stored.request_id,
        "logical_group_name": stored.logical_group_name,
        "requested_by": stored.requested_by,
        "authorized_roles": list(stored.authorized_roles),
        "idempotency_key": stored.idempotency_key,
        "activation_state": dict(stored.activation_state),
        "lifecycle_state": stored.lifecycle_state.value,
        "terminal": stored.lifecycle_state.is_terminal(),
        "generation_id": stored.generation_id,
        "run_id": stored.run_id,
        "run_status": status,
        "run_status_reason": reason,
        "resolved_input_digest": stored.resolved_input_digest,
        "requested_at": stored.requested_at.isoformat(),
        "accepted_at": None if stored.accepted_at is None else stored.accepted_at.isoformat(),
        "lease_expires_at": (None if stored.lease_expires_at is None
                             else stored.lease_expires_at.isoformat()),
        "updated_at": stored.updated_at.isoformat(),
    }


# ── the pre-flight ───────────────────────────────────────────────────────────────────────────────


def _well_formed_group(work_item_ids: list[str]) -> tuple[str, ...]:
    """The group's members, ordered and distinct, or a 422 that names the problem.

    The same two rules ``resolve_feature_inputs`` enforces, applied HERE — where they cost nothing
    and are legible — rather than inside the worker, where the only available outcome is a
    dead-lettered job an operator has to go and read. Ordered ascending because that is the order
    the resolver imposes anyway, so the payload records the membership the compile will see.
    """
    if not work_item_ids:
        raise HTTPException(
            status_code=422,
            detail="a materialization group needs at least one work item: compilation is "
                   "authorized group-wide and there is nothing to authorize for an empty group")
    duplicates = sorted({item for item in work_item_ids if work_item_ids.count(item) > 1})
    if duplicates:
        raise HTTPException(
            status_code=422,
            detail=f"work item(s) {duplicates} appear twice in the group; a duplicate member would "
                   f"resolve to two features occupying one Hive column")
    blank = [item for item in work_item_ids if not item.strip()]
    if blank:
        raise HTTPException(status_code=422,
                            detail="a blank work item id names no durable feature identity")
    return tuple(sorted(work_item_ids))


def _require_every_member_exists(conn: psycopg.Connection, members: tuple[str, ...]) -> None:
    """One existence read — NOT a compile.

    This is the whole of what a trigger can honestly check about membership: that each id names a
    durable ``recipe_formula_shadow_work_item`` row. Whether that row's authoring run terminated,
    resolved and hashes to its manifest is ``resolve_feature_inputs``'s question, and answering it
    here would mean replaying every member's trace inside the request transaction — which is the
    compile this route exists not to run.
    """
    found = {row[0] for row in conn.execute(_MEMBERS_EXIST, (list(members),)).fetchall()}
    missing = sorted(set(members) - found)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"no work item(s) {missing}: a materialization group's members are supplied as "
                   f"recipe_formula_shadow_work_item ids (nothing maps a logical group to its "
                   f"members), and an id naming no row could never be resolved")


def _job(request_id: str, members: tuple[str, ...],
         body: MaterializationRunIn) -> MaterializationJobV1:
    """The declarations as the WORKER's job, validated by the worker's own reader.

    ``decode_job`` is the only description of this payload's shape in the tree, and every refusal it
    raises is a ``ValueError``. Surfacing those as 422 rather than letting them become a
    dead-lettered message a tick later is the difference between a caller who can fix their request
    and an operator who has to read a queue row's ``last_error``.
    """
    try:
        return decode_job({
            "version": PAYLOAD_VERSION,
            "request_id": request_id,
            "work_item_ids": list(members),
            "spine_declaration": body.spine_declaration,
            "cadence": body.cadence,
            "availability_promise": body.availability_promise,
            "mechanism": body.mechanism,
            "published_schema": body.published_schema,
            "contract_overrides": body.contract_overrides,
        })
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=422,
            detail=f"the declarations do not form a materialization job the worker could read: "
                   f"{exc}") from exc


def _record(conn: psycopg.Connection, request_id: str, body: MaterializationRunIn,
            members: tuple[str, ...], identity: IdentityEnvelope) -> MaterializationRequestV1:
    """Mint the request — or return the one this idempotency key already named.

    ``authorized_roles`` is the requester's role CLAIMS, snapshotted here and never re-read: Gate 2
    judges the run against the scope its requester actually held, which is why
    ``compile_feature_group`` takes no roles parameter at all.

    ``resolved_input_digest`` is the group's declared MEMBERSHIP, canonicalized. It is set rather
    than left null because it is one of ``IDEMPOTENT_IDENTITY_FIELDS``: with it, one key reused for
    a different set of features is REFUSED by the store; without it, the store would happily answer
    the second caller with the first caller's request and both would believe their group was queued.

    ``activation_state`` records the switch that was observed at the moment of asking. It is
    evidence about the world the decision was made in, and the store is deliberately opaque to its
    content.
    """
    try:
        return record_request(
            conn,
            request_id=request_id,
            logical_group_name=body.logical_group_name,
            requested_by=identity.subject,
            authorized_roles=identity.role_claims,
            idempotency_key=body.idempotency_key,
            activation_state={"flag": MATERIALIZATION_FLAG, "enabled": True, "surface": "http"},
            resolved_input_digest=materialize_hash({
                "logical_group_name": body.logical_group_name,
                "work_item_ids": list(members)}),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        # `record_request`'s one non-ValueError refusal, and it is deliberately not a 500: under an
        # isolation level stricter than READ COMMITTED the INSERT can conflict with a row this
        # transaction's snapshot cannot yet see. Nothing is wrong with the CALL — another writer
        # holds the key uncommitted — so the honest answer is "try again", not "the server broke".
        raise HTTPException(
            status_code=503, headers={"Retry-After": "1"},
            detail=f"another caller is recording a materialization request under idempotency key "
                   f"{body.idempotency_key!r} and has not committed yet; retry ({exc})") from exc


# ── the one rule about what a 202 means ──────────────────────────────────────────────────────────


def _refuse_if_nothing_will_run(request: MaterializationRequestV1, *,
                                queue_status: str | None = None) -> None:
    """Refuse with 409 unless a worker can still REACH this request. The dead-letter trap, closed.

    Two correct behaviours compose into a wrong answer. ``record_request`` returns the stored row on
    an idempotency hit, because a retried HTTP call must not become a second run. ``enqueue_checked``
    returns the stored queue id, because one message id is one unit of work. So a retry of a request
    whose job was dead-lettered — or whose lifecycle the worker already terminalized — receives the
    ids of work that is over, and a route that answered ``202`` would be promising a run that no
    worker will ever claim. Nothing would surface it: the queue depth would not move, the counters
    would not advance, and the caller would be told their group was accepted.

    **THE DECISION: refuse, name the state, and require a fresh idempotency key.** A 409 is exactly
    what this is — the request conflicts with a durable record of the same name — and the remedy is
    one the caller can act on, because a fresh key mints a fresh request id, which derives a fresh
    message id, which is a fresh queue row (a named test proves that end to end).

    **The two alternatives, and why they were rejected.**

    *Re-arm the dead queue row* (``UPDATE queue SET status='ready', attempts=0``). It would destroy
    the dead-letter record — the operator's only evidence of WHY the job was poison — and it would
    usually not even work: the worker dead-letters a permanent failure and terminalizes the request
    in the same pass, so the re-armed message would find a ``failed`` request, and
    ``compile_feature_group`` REPLAYS a terminal request rather than running it. The route would
    have destroyed evidence to arrange a replay. Worse, re-arming is a write into the fence-guarded
    lifecycle the worker owns, issued from a surface that holds no lease.

    *Mint a fresh key silently on the caller's behalf.* That is the idempotency key doing the
    opposite of its job: the caller's retry — the ordinary case of a lost response — would become a
    second run, which is the exact duplication migration 1053's UNIQUE constraint exists to prevent.

    The check runs TWICE by design, and the order is the point: once before the enqueue (a terminal
    request must not have work pushed at it at all) and once after (only then is the stored queue
    row's status known). ``queue_status=None`` means the queue has not been consulted yet.
    """
    if request.lifecycle_state.is_terminal():
        raise HTTPException(
            status_code=409,
            detail=f"materialization request {request.request_id!r} is already "
                   f"{request.lifecycle_state.value!r} (generation "
                   f"{request.generation_id!r}, run {request.run_id!r}): this idempotency key names "
                   f"work that is OVER, and a re-delivery would replay it rather than run it. "
                   f"Re-trigger with a fresh idempotency key; read GET "
                   f"/materialization-runs/{request.request_id} for what the first attempt decided")
    if queue_status is not None and queue_status not in _CLAIMABLE:
        counters.incr("materialize.trigger.unreachable")
        log("materialize.trigger.unreachable", level="warning", request_id=request.request_id,
            queue_status=queue_status, lifecycle_state=request.lifecycle_state.value)
        raise HTTPException(
            status_code=409,
            detail=f"materialization request {request.request_id!r} already exists and its queued "
                   f"job is {queue_status!r}, so no worker will claim it again — answering 202 "
                   f"would promise a run that cannot happen. The dead-lettered row is left as the "
                   f"worker wrote it (it is the evidence of why the job failed); re-trigger with a "
                   f"fresh idempotency key to mint a new request and a new queue row")


def _queue_status(conn: psycopg.Connection, queue_id: int) -> str:
    """The stored queue row's status — read back rather than assumed, because ``enqueue_checked``
    returns an existing id as readily as a new one and says nothing about its state."""
    row = conn.execute("SELECT status FROM queue WHERE id = %s", (queue_id,)).fetchone()
    if row is None:  # pragma: no cover — the row was written in this same transaction
        raise HTTPException(status_code=500,
                            detail=f"queue row {queue_id} vanished inside its own transaction")
    return str(row[0])


# ── the status fold ──────────────────────────────────────────────────────────────────────────────


def _run_verdict(conn: psycopg.Connection,
                 request: MaterializationRequestV1) -> tuple[str | None, str | None]:
    """``(run_status, reason)`` — the plane's fold, or a plain statement of why there is none.

    Exactly one of the two is ever non-null. A reason is not an error message: "this request never
    reached the plane" is the ordinary durable shape of a pre-render refusal, and of every request
    that has not yet been claimed.
    """
    if request.run_id is None:
        return None, (
            f"no run exists for this request: it is {request.lifecycle_state.value!r}. "
            f"A request that ends before its project is sealed records nothing in the control "
            f"plane — a run event needs a generation, and a generation needs a rendered project — "
            f"so its own lifecycle is the whole record"
            if request.lifecycle_state.is_terminal() else
            f"no run exists yet: this request is {request.lifecycle_state.value!r}, and a run is "
            f"named only once a project has been rendered and sealed")
    events = read_run_events(conn, request.run_id)
    if not events:  # pragma: no cover — a run id is stamped in the same transaction as its event
        return None, (
            f"this request names run {request.run_id!r} and the control plane holds no events for "
            f"it: a status folded from nothing would be an invention about a run nobody recorded")
    return fold_run_status(events).value, None
