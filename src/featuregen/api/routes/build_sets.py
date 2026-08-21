"""§0.10 step 3 — the user-reachable surface for declaring a build set and asking to build it.

**The hole this closes.** `record_build_set` and `request_generation` are library functions with no
caller anywhere in `src/`, and `generation_lane` consumes a queue nothing produces onto. A person
could not ask for a build; every test that exercised the store constructed its rows itself. These
are the three endpoints that make a build something a person can start and watch.

**A ROUTE MUST NOT RUN THE CHAIN**, which is `materialization_runs.py`'s rule and holds identically
here: `get_conn` holds ONE connection and ONE transaction open for the whole request, and a
generation restores, admits, compiles, renders and seals. So these are PRODUCERS and READERS — they
record, they enqueue, they read back. The generation itself happens in the worker, where the lane
owns its own claim, its own lease and its own fence.

**The request and its work item commit TOGETHER.** `request_generation` and `enqueue_generation` run
in the same route transaction, deliberately: a request with no queue row is work nobody will ever
pick up, and a queue row naming a rolled-back request is a job that can only fail. There is no
outbox hop between them because there is no boundary for one to bridge.

**`202`, not `201`.** A generation request is ACCEPTED, not completed — the artifact does not exist
when this returns, and a `201` would say a thing was created that a caller could then fetch.

**404 for a flag-off deployment**, byte-for-byte Starlette's own body, exactly as the sibling
routes do: a `503` says "this exists and is unwell"; the flag says this deployment does not run V2
generation at all.

▲ **WHAT THIS SURFACE CANNOT YET CARRY, stated rather than defaulted.** `GenerationJobV2` freezes
five declarations a generation cannot derive: the POPULATION (`spine_declaration`), the CADENCE, the
AVAILABILITY PROMISE, the OPERAND FACTS, and the POLICY REALIZATION IDS. This body carries none of
them, so a build requested here queues with all five absent and the lane refuses it at the
population stage — by name, with the reason, and having recorded nothing.

That refusal is the correct behaviour and it is not the finished behaviour. The alternative was to
invent values, and every one of them decides a published number: a defaulted spine picks whose rows
the features are computed for, a defaulted promise decides which day's data is considered available,
and defaulted operand facts let a monetary sum cross currencies. A route that filled them in would
be choosing what gets published on behalf of whoever clicked the button.

The reason they are not here yet is a DESIGN question, not an omission of typing: a population is
declared once per build set rather than per attempt, and `build_set_revision.declaration_json`
already holds an untyped payload that is the natural home for it. Giving that payload a type is the
next task on this surface, and until it lands the API can declare a set, queue an attempt, and
report a truthful refusal — which is the whole of what it claims to do.
"""
from __future__ import annotations

from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from featuregen.aggregates.ids import mint_id
from featuregen.api.deps import (
    get_conn,
    get_identity,
    require_feature_generate,
    require_feature_read,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.materialize.generation_authorization import load_generation_authorization
from featuregen.materialize.generation_lane import (
    GenerationJobV2,
    enqueue_generation,
    generation_enabled,
)
from featuregen.overlay.upload.build_set_store import (
    read_build_set,
    read_request,
    record_build_set,
    request_generation,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.queue import QueueIdempotencyConflict


def require_generation_enabled() -> None:
    """The deployment's switch, consulted BEFORE anything else on every route here.

    The same function object the worker stage calls, over the same truthy set — a route that
    re-implemented the check would 404 for a deployment whose worker was happily draining.
    """
    if not generation_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(dependencies=[Depends(require_generation_enabled)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


def _now(conn: psycopg.Connection) -> str:
    """The DATABASE's clock, not this process's. Two API replicas with drifting clocks would
    otherwise stamp one lifecycle with times that go backwards."""
    return conn.execute("SELECT now()").fetchone()[0].isoformat()


class BuildSetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_reading_revision_id: str = Field(min_length=1)
    #: ORDERED. The order a person picked features in is a fact about the build — it decides the
    #: published table's column order — so this is a list and never a set.
    selection_revision_ids: list[str] = Field(min_length=1)
    declaration: dict[str, Any] = Field(default_factory=dict)


class GenerationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    build_set_revision_id: str = Field(min_length=1)
    generation_authorization_revision_id: str = Field(min_length=1)
    physical_type_policy: str = Field(min_length=1)
    #: Per feature, and possibly `null` — "had no rows" and "summed to zero" are different published
    #: answers, so there is no default and `compile_generation_v2` refuses a mapping that does not
    #: describe exactly the members being built.
    empty_values: dict[str, str | None]
    engine_id: str = Field(min_length=1)
    roles: list[str] = Field(default_factory=list)


# ── declare ──────────────────────────────────────────────────────────────────────────────────────
@router.post("/build-sets", status_code=201,
             dependencies=[Depends(require_feature_generate)])
def declare_build_set(
    body: BuildSetIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Record a build set — *"build these features, together, against this target"*.

    `201` here and `202` on the generation below, and the difference is real: a build set IS created
    by this call and a caller can fetch it back. Nothing is queued.

    **Idempotent on CONTENT.** Declaring the same members against the same target twice returns the
    existing set with `created: false`, because minting a second identical set would split its
    attempts across two roots and make "how did this build go" a question with two answers.
    """
    try:
        revision_id, created = record_build_set(
            conn,
            revision_id=mint_id("bs"),
            target_reading_revision_id=body.target_reading_revision_id,
            selection_revision_ids=body.selection_revision_ids,
            declaration=body.declaration,
            declared_by=identity.subject,
            declared_at=_now(conn))
    except ValueError as exc:
        # 422: an empty set, or one naming a feature twice. Both are caller errors whose message
        # names exactly what is wrong, and neither is a governed verdict about any feature.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    counters.incr("featuregen.build_set.declared")
    log("featuregen.build_set.declared", revision_id=revision_id, created=created,
        members=len(body.selection_revision_ids))
    return {
        "build_set_revision_id": revision_id,
        "created": created,
        "selection_revision_ids": list(body.selection_revision_ids),
        "detail": ("the build set was recorded" if created else
                   "this exact build was already declared; its attempts belong to that set"),
    }


# ── ask to build ─────────────────────────────────────────────────────────────────────────────────
@router.post("/build-sets/generations", status_code=202,
             dependencies=[Depends(require_feature_generate)])
def request_build(
    body: GenerationIn, conn: _Conn, identity: _Identity,
) -> dict[str, Any]:
    """Start an attempt at a build set, or return the LIVE one.

    **The double-click answer is `created: false`, not a second compile.** Idempotency is on the
    WORK — the build set and the environment — rather than on a caller-supplied key, because a
    client minting a fresh key per click would defeat a key-based guard and a generation costs real
    compute. A retry after a FAILURE is still allowed: the guard protects against double-clicks, not
    against recovery.

    **The environment is read off the AUTHORIZATION, never taken from the body.** An authorization
    names what a generation is authorized FOR, environment included; letting a caller supply a
    second one would let a build authorized for one cluster be requested against another, and the
    composite foreign key would then refuse the write with a message about keys rather than about
    permission.
    """
    authorization = load_generation_authorization(
        conn, body.generation_authorization_revision_id)
    if authorization is None:
        raise HTTPException(
            status_code=404,
            detail=f"no generation authorization {body.generation_authorization_revision_id!r}: "
                   f"a build is requested against an approval, and there is no such approval")
    if authorization.build_set_revision_id != body.build_set_revision_id:
        # 409 rather than 422: both halves exist and are individually valid, and what is wrong is
        # the RELATION between them. An approval for a different set does not permit this build.
        raise HTTPException(
            status_code=409,
            detail=f"authorization {body.generation_authorization_revision_id} approves build set "
                   f"{authorization.build_set_revision_id!r}, not "
                   f"{body.build_set_revision_id!r}: an approval permits a specific build")

    if read_build_set(conn, body.build_set_revision_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"no build set {body.build_set_revision_id!r}: its membership is what would be "
                   f"built, so there is nothing to build")

    request_id, created = request_generation(
        conn,
        request_id=mint_id("gen"),
        build_set_revision_id=body.build_set_revision_id,
        environment_id=authorization.environment_id,
        requested_by=identity.subject,
        requested_at=_now(conn),
        generation_authorization_revision_id=body.generation_authorization_revision_id)

    if created:
        # SAME TRANSACTION as the request above. See the module docstring: a request with no queue
        # row is work nobody will ever pick up.
        now = _now(conn)
        try:
            enqueue_generation(
                conn,
                job=GenerationJobV2(
                    request_id=request_id,
                    # ABSENT, NOT DEFAULTED — see the module docstring. Each of these decides a
                    # published number, and the lane refuses a job missing them by name rather than
                    # generating one against values this route invented.
                    spine_declaration=None,
                    cadence=None,
                    availability_promise=None,
                    physical_type_policy=body.physical_type_policy,
                    empty_values=dict(body.empty_values),
                    operand_facts={},
                    engine_id=body.engine_id,
                    roles=tuple(body.roles),
                    compiled_at=now,
                    sealed_at=now),
                environment_id=authorization.environment_id,
                logical_group_name=authorization.logical_group_name)
        except QueueIdempotencyConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    counters.incr("featuregen.generation.requested")
    log("featuregen.generation.requested", request_id=request_id, created=created,
        build_set_revision_id=body.build_set_revision_id,
        environment_id=authorization.environment_id)
    return {
        "request_id": request_id,
        "created": created,
        "environment_id": authorization.environment_id,
        "logical_group_name": authorization.logical_group_name,
        "detail": ("the build was queued" if created else
                   "an attempt at this build set is already in flight in this environment"),
    }


# ── watch ────────────────────────────────────────────────────────────────────────────────────────
@router.get("/build-sets/generations/{request_id}",
            dependencies=[Depends(require_feature_read)])
def read_generation(request_id: str, conn: _Conn) -> dict[str, Any]:
    """One attempt as stored: where it got to, and what it produced or refused.

    `stage_label` is SERVER-OWNED. A screen that mapped statuses to words itself would be a second
    vocabulary, and the two would describe one status with two different sentences the first time
    either changed.
    """
    request = read_request(conn, request_id)
    if request is None:
        raise HTTPException(status_code=404, detail=f"no generation request {request_id!r}")

    return {
        "request_id": request.request_id,
        "build_set_revision_id": request.build_set_revision_id,
        "environment_id": request.environment_id,
        "generation_authorization_revision_id": request.generation_authorization_revision_id,
        "status": request.status.value,
        "stage_label": request.stage_label,
        "sealed_artifact_id": request.sealed_artifact_id,
        # EVERY refusal, not the fact that there were some: fixing one of four is four round trips.
        "refusals": [dict(refusal) for refusal in request.refusals],
        "failure_reason": request.failure_reason,
    }
