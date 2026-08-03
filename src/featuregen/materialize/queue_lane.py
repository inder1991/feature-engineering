"""Phase G §3.1 — the FENCED QUEUE LANE: the compile chain becomes runnable work.

**The hole this closes.** ``compile_feature_group`` is a library function with no caller anywhere in
``src/`` — every stage of the chain exists and nothing runs it. This module is the consumer that
does, and the producer a trigger surface calls to ask for one.

**Why a dedicated lane and not the generic handler registry.** ``process_one`` builds a
``HandlerContext`` from ``payload["event_id"]`` over a ``run`` aggregate stream
(``runtime/dispatch.py:111-133``) and dead-letters anything without one. A materialization job has
no run stream — the run's identity is Phase G's own ``materialization_request`` row (migration
1053), not an event-sourced aggregate. ``recipe_formula_shadow`` hit exactly this and solved it with
a dedicated fenced lane; this is that pattern, and where it had to be adapted the adaptation is
argued below rather than left as a difference somebody notices later.

**WHERE THE PATTERN WAS ADAPTED, and why.**

* *The queue row IS the frozen work item.* The formula lane freezes its inputs into a durable,
  content-hashed ``recipe_formula_shadow_work_item`` row and puts only an opaque id on the queue.
  Phase G may add no migration (1053/1054 are used, 1055 is reserved for G-3, 1056 needs an
  off-branch reservation), so there is no such table to write. What remains is
  ``enqueue_checked``'s ``payload_hash``: the payload carries the job and the queue row's content
  hash refuses one message id reused for materially different work — the same guarantee, held by
  the row the work item would have pointed at.
* *No outbox hop.* The formula lane publishes to a topic the relay routes. Here the producer runs
  in the SAME transaction that mints the request row, so the request and its work item commit
  together and there is no boundary for an outbox to bridge; adding a second reserved relay route
  would change shared relay behaviour (``route_required`` dead-letters an unrouted topic) for every
  deployment, to buy nothing.
* *No mid-work lease renewal.* Argued under "THE LEASE" below.

**THE CHAIN OWNS THE LIFECYCLE.** ``compile_feature_group`` advances the request itself — to
``failed`` on a governed refusal, and through ``running`` to ``committed``/``failed`` inside the one
transaction that records the generation, the artifact, the L0 verdict and the terminal event. This
handler therefore writes the lifecycle in exactly two places, both of them states the chain
declined to reach: ``accept_request`` before it is called (acceptance is the CLAIM, and the chain
requires it rather than granting it), and a terminal ``failed`` when the chain RAISED instead of
returning — a raise records nothing, so a request left ``accepted`` would be invisible work nobody
could account for. On every returning path the handler reads the chain's outcome and writes no
lifecycle at all.

**THE LEASE, and why it is sized rather than renewed.** A compile now includes an L0 subprocess that
runs INSIDE the chain's commit transaction (``chain._commit``), and that transaction has already
UPDATEd ``materialization_request`` to ``running`` — so it holds a row lock on the exact row a
renewal would have to touch. A renewer on a second connection would block on that lock until the
transaction committed, i.e. precisely through the window a renewal exists to cover; a renewer on
THIS connection cannot run at all, because the connection is inside the subprocess call. Mid-compile
renewal is therefore not a policy this lane declined — it is structurally unavailable.

What IS available is a bound. The one unbounded-looking part of a compile is the build proof, and
its duration is configured (``L0Interpreter.timeout_seconds``, no default, by Task 6's decision), so
:attr:`MaterializationLaneConfig.lease_seconds` is that timeout plus a compile budget. Raise the L0
timeout and the lease rises with it — structurally, not by somebody remembering to. Both leases are
sized from that one number: the queue row's (so no second worker reclaims a live job) and the
request's (so §3.3's reconciler does not adopt one).

And if a lease expires anyway, nothing silent happens. ``accept_request`` and ``advance_lifecycle``
are conditional UPDATEs naming the states they are legal from, so a reconciler that tried to adopt a
live request would block on the chain's row lock and then match no row — a loud refusal, not a
double write. The lane adds the third guard: it refuses to drive a request it did not itself accept.

**PublishStepMissing is classified, not crashed.** It is a statement about the PLATFORM — an
operator ingesting one passing probe attestation flips every run of this chain into it — so it fails
the request with the exception's own text on the queue row's ``last_error`` and dead-letters the
message rather than retrying a state no retry can change.
"""
from __future__ import annotations

import dataclasses
import datetime as _datetime
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Any

import psycopg

from featuregen.materialize.admission import FeatureNamePlanError
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.compile.chain import (
    CompiledGroup,
    L0Interpreter,
    NodeAssembler,
    PublishStepMissing,
    compile_feature_group,
)
from featuregen.materialize.compile.wiring import assemble_nodes as _wired_assembler
from featuregen.materialize.contract import (
    AvailabilityPromiseKind,
    AvailabilityPromiseV1,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractOverrides,
)
from featuregen.materialize.inventory import ClusterInventoryV1, load_inventory
from featuregen.materialize.publish import PublishMechanism
from featuregen.materialize.request_store import (
    MaterializationRequestV1,
    RequestLifecycle,
    accept_request,
    advance_lifecycle,
    read_request,
    renew_lease,
)
from featuregen.materialize.spine import (
    SNAPSHOT_POLICY_TYPES,
    PopulationSemantics,
    SnapshotPolicy,
    SnapshotPolicyKind,
    SpineSourceDeclarationV1,
)
from featuregen.runtime.observability import counters, log
from featuregen.runtime.queue import (
    MaterializationQueueClaim,
    claim_materialization,
    complete_materialization,
    enqueue_checked,
    fail_materialization,
    renew_materialization,
)

__all__ = [
    "MATERIALIZATION_HANDLER",
    "MaterializationJobV1",
    "MaterializationLaneConfig",
    "MaterializationLaneOutcome",
    "decode_job",
    "encode_job",
    "enqueue_materialization",
    "lane_config_from_env",
    "process_materialization_once",
]

#: The queue handler name. ``runtime/queue.py`` spells the same string into
#: ``MATERIALIZATION_QUEUE_HANDLERS`` (it cannot import this module — this one imports it), and a
#: test asserts the two agree, so a rename cannot silently leave ``claim_one`` free to claim a
#: governed request and dead-letter it for a missing run stream.
MATERIALIZATION_HANDLER = "materialization.compile.v1"

#: One namespace for both the message id (per REQUEST — the idempotency unit) and the partition key
#: (per GROUP — the serialization unit, because publication is atomic per group and §10.1 writes one
#: binding per logical name).
_NAMESPACE = "materialize"

#: The payload's shape version. Named and checked rather than assumed: the payload IS this lane's
#: frozen work item, so a worker running older code must refuse a job it cannot read whole rather
#: than compile a group from the half it understands.
PAYLOAD_VERSION = 1

#: The lease a job is claimed under BEFORE the deployment's compile budget is known — long enough to
#: read a request row and resolve a configuration, and no longer, so a worker that dies in that
#: window releases the job promptly.
_BOOTSTRAP_LEASE_SECONDS = 60.0

#: Everything a compile does APART from the build proof: resolve, admit, compile every expression,
#: authorize, derive the contract, plan, bind, render, seal and write the tree. Bounded by the DB
#: and the local filesystem rather than by configuration, so it is a stated budget rather than a
#: derived one — and it is stated ONCE, here.
COMPILE_BUDGET_SECONDS = 600.0

_PROJECT_ROOT_ENV = "FEATUREGEN_MATERIALIZE_PROJECT_ROOT"
_INVENTORY_ENV = "FEATUREGEN_MATERIALIZE_INVENTORY"
_L0_PYTHON_ENV = "FEATUREGEN_MATERIALIZE_L0_PYTHON"
_L0_TIMEOUT_ENV = "FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS"

#: Failures that are DETERMINISTIC: the same job, the same catalog and the same configuration would
#: produce them again, so the request is failed and the message dead-lettered instead of retried
#: against a value that will not change. ``ValueError``/``TypeError``/``KeyError`` are the chain's
#: own "this call was assembled wrongly" family (§14 has no code for it); ``FeatureNamePlanError``
#: is a plan error; a ``MaterializationRefused`` that ESCAPES rather than being returned is still a
#: governed verdict. Everything else — a dropped connection, a full disk — is retryable.
_DETERMINISTIC = (ValueError, TypeError, KeyError, FeatureNamePlanError, MaterializationRefused)


def _utc_now_iso() -> str:
    """The chain's ``clock``: an offset-aware ISO 8601 instant. The plane mints no timestamps."""
    return _datetime.datetime.now(tz=_datetime.UTC).isoformat()


# ── the frozen job ───────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MaterializationJobV1:
    """What was asked for, frozen at trigger time — the queue payload, typed.

    Everything here is a DECLARATION the platform does not derive and cannot re-read later:
    §4's population, §5's cadence and availability promise, §5.4's tightenings, the live column list
    of the published table, and the group's members (nothing maps a logical group to its work items
    yet — Task 2's §15.2). Re-reading any of them at claim time would let a run compile against a
    world that changed after somebody asked for it.

    What is NOT here: the logical group name and the authorized roles, both of which live on the
    request row and are read by the chain itself — a second copy would be a second answer.
    """

    request_id: str
    work_item_ids: tuple[str, ...]
    spine_declaration: SpineSourceDeclarationV1 | None
    cadence: CadenceDecl
    availability_promise: AvailabilityPromiseV1
    mechanism: PublishMechanism
    published_schema: tuple[str, ...] | None
    contract_overrides: ContractOverrides | None = None


def encode_job(job: MaterializationJobV1) -> dict[str, Any]:
    """The job as plain JSON, ready for the queue payload."""
    return {
        "version": PAYLOAD_VERSION,
        "request_id": job.request_id,
        "work_item_ids": list(job.work_item_ids),
        "spine_declaration": (None if job.spine_declaration is None
                              else _encode_spine(job.spine_declaration)),
        "cadence": _plain(job.cadence),
        "availability_promise": _plain(job.availability_promise),
        "mechanism": job.mechanism.value,
        "published_schema": (None if job.published_schema is None
                             else list(job.published_schema)),
        "contract_overrides": (None if job.contract_overrides is None
                               else _plain(job.contract_overrides)),
    }


def decode_job(payload: Mapping[str, Any]) -> MaterializationJobV1:
    """The job a worker reads back, or ``ValueError``.

    Every refusal is a ``ValueError`` for the reason ``request_store`` gives: §14's closed
    vocabularies answer governed questions, and "this payload is not a job" is not one of them.
    The lane classifies it as deterministic, so a payload nothing can read is dead-lettered with its
    reason rather than retried until the attempt budget runs out.
    """
    material = _mapping(payload, where="materialization job payload")
    version = material.get("version")
    if version != PAYLOAD_VERSION:
        raise ValueError(
            f"materialization job payload declares version {version!r} and this worker reads "
            f"{PAYLOAD_VERSION}: the payload is this lane's frozen work item, so a job it cannot "
            f"read WHOLE must be refused rather than compiled from the half it understands")
    spine = material.get("spine_declaration")
    overrides = material.get("contract_overrides")
    schema = material.get("published_schema")
    return MaterializationJobV1(
        request_id=_text(material.get("request_id"), where="job request_id"),
        work_item_ids=tuple(_texts(material.get("work_item_ids"), where="job work_item_ids")),
        spine_declaration=None if spine is None else _decode_spine(spine),
        cadence=_decode_cadence(material.get("cadence")),
        availability_promise=_decode_promise(material.get("availability_promise")),
        mechanism=PublishMechanism(_text(material.get("mechanism"), where="job mechanism")),
        published_schema=None if schema is None else tuple(
            _texts(schema, where="job published_schema")),
        contract_overrides=None if overrides is None else _decode_overrides(overrides),
    )


def _encode_spine(declaration: SpineSourceDeclarationV1) -> dict[str, Any]:
    """The declaration, with the snapshot policy's DISCRIMINATOR restored.

    ``kind`` is a ``ClassVar`` on each policy variant, so ``dataclasses.asdict`` drops it — and
    without it the four variants are indistinguishable mappings on the way back. It is written from
    the variant's own class attribute rather than from a second mapping here.
    """
    material = _plain(declaration)
    material["snapshot_policy"] = {
        **_plain(declaration.snapshot_policy),
        "kind": declaration.snapshot_policy.kind.value,
    }
    return material


def _decode_spine(material: Any) -> SpineSourceDeclarationV1:
    node = _mapping(material, where="job spine_declaration")
    return SpineSourceDeclarationV1(
        entity=_text(node.get("entity"), where="spine entity"),
        source_table_ref=_text(node.get("source_table_ref"), where="spine source_table_ref"),
        ordered_key_refs=tuple(_texts(node.get("ordered_key_refs"),
                                      where="spine ordered_key_refs")),
        population_semantics=PopulationSemantics(
            _text(node.get("population_semantics"), where="spine population_semantics")),
        availability_ref=node.get("availability_ref"),
        snapshot_policy=_decode_policy(node.get("snapshot_policy")),
        declared_by=_rebuild(_identity_envelope_type(), node.get("declared_by"),
                             where="spine declared_by"),
        declaration_reason=_text(node.get("declaration_reason"),
                                 where="spine declaration_reason"),
        declaration_version=int(node.get("declaration_version", 0)),
        recorded_at=_text(node.get("recorded_at"), where="spine recorded_at"),
        declaration_record_id=_text(node.get("declaration_record_id"),
                                    where="spine declaration_record_id"),
    )


def _decode_policy(material: Any) -> SnapshotPolicy:
    node = dict(_mapping(material, where="spine snapshot_policy"))
    kind = node.pop("kind", None)
    try:
        variant = SNAPSHOT_POLICY_TYPES[SnapshotPolicyKind(kind)]
    except ValueError as exc:
        raise ValueError(
            f"spine snapshot_policy names kind {kind!r}, which is not one of "
            f"{[k.value for k in SnapshotPolicyKind]}: §4.2's union is CLOSED, and the default a "
            f"missing variant would fall into is 'read the whole table'") from exc
    return _rebuild(variant, node, where=f"spine snapshot_policy {kind}")


def _decode_cadence(material: Any) -> CadenceDecl:
    node = _mapping(material, where="job cadence")
    return CadenceDecl(
        period=CadencePeriod(_text(node.get("period"), where="cadence period")),
        timezone=_text(node.get("timezone"), where="cadence timezone"),
        business_date_cutoff=_text(node.get("business_date_cutoff"),
                                   where="cadence business_date_cutoff"),
        trigger=CadenceTrigger(_text(node.get("trigger"), where="cadence trigger")))


def _decode_promise(material: Any) -> AvailabilityPromiseV1:
    node = _mapping(material, where="job availability_promise")
    return AvailabilityPromiseV1(
        kind=AvailabilityPromiseKind(_text(node.get("kind"), where="promise kind")),
        calendar_days=int(node.get("calendar_days", 0)),
        plus_minutes=int(node.get("plus_minutes", 0)))


def _decode_overrides(material: Any) -> ContractOverrides:
    node = _mapping(material, where="job contract_overrides")
    promise = node.get("availability_promise")
    return ContractOverrides(
        sensitivity_class=node.get("sensitivity_class"),
        access_requirements=tuple(_texts(node.get("access_requirements") or (),
                                         where="override access_requirements")),
        availability_promise=None if promise is None else _decode_promise(promise))


def _identity_envelope_type() -> type:
    """``IdentityEnvelope`` — resolved here rather than imported at module scope so that this
    package's "never import the platform's identity machinery" posture is not weakened into a
    dependency: the envelope is carried as PROVENANCE on the declaration (``spine.py`` excludes it
    from ``identity_payload``), never as authority. Gate 2's roles are the request row's snapshot,
    read by the chain itself."""
    from featuregen.contracts.envelopes import IdentityEnvelope

    return IdentityEnvelope


def _plain(value: Any) -> Any:
    """A dataclass tree as JSON-native material: enums by value, tuples as lists."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _plain(dataclasses.asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _rebuild(cls: type, material: Any, *, where: str) -> Any:
    """One plain dataclass from its own field names — no second description of its shape.

    A list becomes a tuple (every collection field in the types this rebuilds is a tuple), an
    unknown key is refused rather than dropped, and a missing required one is a ``ValueError``
    naming the type: silently defaulting a field of a frozen declaration would let a payload written
    by newer code compile as a DIFFERENT declaration under the same request id.
    """
    node = _mapping(material, where=where)
    names = {field.name for field in dataclasses.fields(cls)}
    unknown = sorted(set(node) - names)
    if unknown:
        raise ValueError(
            f"{where} carries {unknown} which {cls.__name__} does not declare: a key nothing reads "
            f"was either meant to change the job or is a payload from another shape")
    try:
        return cls(**{key: tuple(item) if isinstance(item, list) else item
                      for key, item in node.items()})
    except TypeError as exc:
        raise ValueError(f"{where} cannot be read back as a {cls.__name__}: {exc}") from exc


def _mapping(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} is {type(value).__name__}, and a mapping is required")
    return value


def _text(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} is {value!r}: a non-blank string is required")
    return value


def _texts(value: Any, *, where: str) -> list[str]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ValueError(f"{where} is {value!r}: a sequence of strings is required")
    return [_text(item, where=f"{where} member") for item in value]


# ── the producer ─────────────────────────────────────────────────────────────────────────────────


def enqueue_materialization(
    conn: psycopg.Connection,
    request: MaterializationRequestV1,
    *,
    job: MaterializationJobV1,
    priority: int = 100,
) -> int:
    """Freeze the job onto the queue, in the caller's transaction. Returns the queue row id.

    Takes the RECORDED request rather than an id so the group name it partitions by is the stored
    one — a caller-supplied group could serialize a job against the wrong group's runs.

    Idempotent on the request: one request is one message id, and ``enqueue_checked`` refuses that
    id reused for materially different work (``QueueIdempotencyConflict``). That refusal is the
    guarantee the formula lane gets from a content-hashed work-item row, held here by the queue row
    itself — this lane may add no table.

    Partitioned by the LOGICAL GROUP, not the request: ``claim_materialization`` skips a partition
    that already has an in-flight lease, so two requests for one group compile one at a time.
    §10.1 writes one binding per logical name and publication is atomic per group, so running them
    concurrently would have them race for the same binding row.
    """
    if job.request_id != request.request_id:
        raise ValueError(
            f"the job names request {job.request_id!r} and the recorded request is "
            f"{request.request_id!r}: the message id is derived from the request, so enqueuing "
            f"these together would freeze one request's declarations under another's identity")
    return enqueue_checked(
        conn,
        message_id=f"{_NAMESPACE}:{request.request_id}",
        partition_key=f"{_NAMESPACE}:{request.logical_group_name}",
        handler=MATERIALIZATION_HANDLER,
        payload=encode_job(job),
        priority=priority,
    )


# ── the deployment's half of the configuration ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MaterializationLaneConfig:
    """What the DEPLOYMENT decides, resolved at this boundary and never inside the chain.

    Task 6 gave ``compile_feature_group`` no default interpreter and no default timeout on purpose:
    "whether a run's build is proved is not a thing to inherit by omission", and a library that read
    ``FEATUREGEN_L0_PYTHON`` for itself would make every deployment's build proof depend on a
    variable no record names. This is the object that owns those answers.

    ``l0=None`` is a POSTURE, not a missing setting: the chain records the same ``error`` report an
    interpreter that could not be launched would, and the run terminates ``RUN_FAILED``. It is what
    a deployment with no kedro environment honestly is.
    """

    inventory: ClusterInventoryV1
    project_root: str
    l0: L0Interpreter | None
    assemble_nodes: NodeAssembler = _wired_assembler
    clock: Callable[[], str] = _utc_now_iso
    compile_budget_seconds: float = COMPILE_BUDGET_SECONDS

    @property
    def lease_seconds(self) -> float:
        """How long one compile may hold its leases — the build proof's own bound, plus everything
        else a compile does. Derived rather than configured, so a deployment that raises the L0
        timeout cannot forget to raise the lease and have the reconciler adopt its live runs."""
        return self.compile_budget_seconds + (0.0 if self.l0 is None else self.l0.timeout_seconds)


def lane_config_from_env() -> MaterializationLaneConfig:
    """The lane's configuration from the environment, or ``ValueError`` naming the variable.

    Read at the boundary and only when there is a job to run, so an unconfigured deployment costs
    one idle claim query per tick and never a stage error — and a deployment that IS misconfigured
    finds out against a request, with the variable named, rather than through a worker that will
    not boot.
    """
    project_root = os.environ.get(_PROJECT_ROOT_ENV, "").strip()
    if not project_root:
        raise ValueError(
            f"{_PROJECT_ROOT_ENV} is not set: it is the directory a sealed project is written "
            f"under, and there is no safe default — a compile writes a tree per generation")
    inventory_path = os.environ.get(_INVENTORY_ENV, "").strip()
    if not inventory_path:
        raise ValueError(
            f"{_INVENTORY_ENV} is not set: §0's captured cluster facts are a typed input every "
            f"compilation reads (environment id, engine versions, table layouts), and they live in "
            f"a YAML file a human wrote — there is nothing to infer them from")
    python_executable = os.environ.get(_L0_PYTHON_ENV, "").strip()
    timeout = os.environ.get(_L0_TIMEOUT_ENV, "").strip()
    if timeout and not python_executable:
        raise ValueError(
            f"{_L0_TIMEOUT_ENV} is set and {_L0_PYTHON_ENV} is not: a bound on a build proof that "
            f"cannot be attempted is half a configuration, and the half that is missing is the one "
            f"that decides whether any run of this deployment is ever proven to build")
    return MaterializationLaneConfig(
        inventory=load_inventory(inventory_path),
        project_root=project_root,
        l0=None if not python_executable else L0Interpreter(
            python_executable=python_executable,
            timeout_seconds=_positive_float(timeout, _L0_TIMEOUT_ENV)),
    )


def _positive_float(raw: str, variable: str) -> float:
    if not raw:
        raise ValueError(
            f"{variable} is not set and {_L0_PYTHON_ENV} is: a build proof's bound is a decision, "
            f"and inheriting one by omission is how a caller ends up not having made it")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{variable} is {raw!r}, which is not a number of seconds") from exc
    if value <= 0:
        raise ValueError(
            f"{variable} is {raw!r}: a non-positive bound cannot let any probe finish, so every "
            f"run would record 'the build was never proven'")
    return value


# ── the consumer ─────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MaterializationLaneOutcome:
    """What one pass of the lane did. ``status`` is the vocabulary a worker stage counts by."""

    status: str
    request_id: str | None = None
    generation_id: str | None = None
    run_id: str | None = None
    stopped_at: str | None = None
    detail: str | None = None


def process_materialization_once(
    conn: psycopg.Connection,
    *,
    owner: str,
    config: MaterializationLaneConfig | None = None,
) -> MaterializationLaneOutcome:
    """Claim one materialization job, compile it, and record what happened.

    ``config`` defaults to :func:`lane_config_from_env` and is resolved only AFTER a claim, so a
    deployment that configures nothing pays one query per tick.
    """
    claim = claim_materialization(conn, owner=owner, lease_seconds=_BOOTSTRAP_LEASE_SECONDS)
    if claim is None:
        return MaterializationLaneOutcome("idle")

    request_id: str | None = None
    accepted = False
    try:
        job = decode_job(claim.payload)
        request_id = job.request_id
        request = read_request(conn, request_id=request_id)
        if request is None:
            raise ValueError(
                f"no materialization request {request_id!r}: the request row is minted BEFORE the "
                f"job is enqueued, so a job naming none can only be a caller that assembled the "
                f"trigger wrongly — and no retry will make the row appear")
        if request.lifecycle_state is RequestLifecycle.REQUESTED:
            accept_request(conn, request_id=request_id,
                           lease_seconds=_BOOTSTRAP_LEASE_SECONDS)
            accepted = True
        elif not request.lifecycle_state.is_terminal():
            return _unclaimable(conn, claim, request)

        resolved = lane_config_from_env() if config is None else config
        if accepted:
            # Both leases sized ONCE, now that the budget is known — see "THE LEASE" above. The
            # queue lease first: if its fence has already moved, this worker is not the owner and
            # must not extend a request lease on another worker's behalf.
            if not renew_materialization(conn, claim, lease_seconds=resolved.lease_seconds):
                return _stale(claim, request_id)
            renew_lease(conn, request_id=request_id, lease_seconds=resolved.lease_seconds)

        outcome = compile_feature_group(
            conn,
            request_id=request_id,
            work_item_ids=job.work_item_ids,
            inventory=resolved.inventory,
            spine_declaration=job.spine_declaration,
            cadence=job.cadence,
            availability_promise=job.availability_promise,
            mechanism=job.mechanism,
            published_schema=job.published_schema,
            assemble_nodes=resolved.assemble_nodes,
            project_root=resolved.project_root,
            l0=resolved.l0,
            clock=resolved.clock,
            contract_overrides=job.contract_overrides,
        )
    except PublishStepMissing as exc:
        return _deterministic_failure(conn, claim, request_id, accepted=accepted,
                                      status="publish_step_missing", error=str(exc))
    except _DETERMINISTIC as exc:
        return _deterministic_failure(conn, claim, request_id, accepted=accepted,
                                      status="failed", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — the lane's backstop, mirroring the formula lane
        # NOT deterministic, so the request keeps its lease and its state: this attempt failed for
        # a reason another one might not, and terminalizing it here would record a verdict about a
        # feature from a dropped connection.
        fail_materialization(conn, claim, error=f"{type(exc).__name__}: {exc}", permanent=False)
        counters.incr("materialize.lane.retryable")
        log("materialize.lane.retryable", level="warning", request_id=request_id,
            error=repr(exc))
        return MaterializationLaneOutcome("retryable", request_id, detail=repr(exc))

    return _recorded(conn, claim, outcome)


def _recorded(
    conn: psycopg.Connection, claim: MaterializationQueueClaim, outcome: CompiledGroup,
) -> MaterializationLaneOutcome:
    """The chain returned, so it has already recorded everything — the lane only closes the row.

    A governed refusal is NOT a queue failure: the message was processed and its verdict is durable
    on the request (and, once a generation exists, on the plane). Re-delivering it could only
    produce a replay.
    """
    if outcome.replayed:
        status = "replayed"
    elif outcome.lifecycle_state is RequestLifecycle.COMMITTED:
        status = "completed"
    else:
        status = "refused"
    reported = MaterializationLaneOutcome(
        status, outcome.request_id, generation_id=outcome.generation_id, run_id=outcome.run_id,
        stopped_at=None if outcome.stopped_at is None else outcome.stopped_at.value,
        detail=None if outcome.refusal is None else
        f"{outcome.refusal.code.value}: {outcome.refusal.detail}")
    if not complete_materialization(conn, claim):
        # Everything above is committed; only the queue row was lost to a newer fence. The
        # redelivery this leaves behind replays rather than re-runs, which is exactly what Task 3's
        # derived ids and terminal-request short-circuit exist for.
        return _stale(claim, outcome.request_id)
    counters.incr(f"materialize.lane.{status}")
    log("materialize.lane.recorded", status=status, request_id=outcome.request_id,
        generation_id=outcome.generation_id, run_id=outcome.run_id, stopped_at=reported.stopped_at)
    return reported


def _deterministic_failure(
    conn: psycopg.Connection,
    claim: MaterializationQueueClaim,
    request_id: str | None,
    *,
    accepted: bool,
    status: str,
    error: str,
) -> MaterializationLaneOutcome:
    """The chain RAISED, so nothing recorded the outcome — the lane must, or the work is invisible.

    This is the one lifecycle write the handler owns and the chain does not: a raise leaves the
    request ``accepted`` with a lease that will simply expire, which reads as "a worker is on it"
    forever. Only a request this call itself accepted is failed — one somebody else holds is not
    this worker's to terminalize.

    The state is RE-READ rather than assumed, and the write is skipped unless it is still
    ``accepted``. "The chain raised" and "the chain recorded a terminal" are not mutually exclusive
    in general — a failure after ``_commit``'s transaction closes would be both — and advancing a
    request the chain already terminalized would raise a SECOND exception out of the error path,
    turning a legible failure into a stage fault that says nothing about the job.
    """
    if accepted and request_id is not None:
        current = read_request(conn, request_id=request_id)
        if current is not None and current.lifecycle_state is RequestLifecycle.ACCEPTED:
            advance_lifecycle(conn, request_id=request_id, to_state=RequestLifecycle.FAILED)
    fail_materialization(conn, claim, error=error, permanent=True)
    counters.incr(f"materialize.lane.{status}")
    log("materialize.lane.failed", level="error", status=status, request_id=request_id,
        error=error)
    return MaterializationLaneOutcome(status, request_id, detail=error)


def _unclaimable(
    conn: psycopg.Connection, claim: MaterializationQueueClaim, request: MaterializationRequestV1,
) -> MaterializationLaneOutcome:
    """A request somebody else claimed — refused, and left exactly as it is.

    Re-driving it would mint a second compilation under one durable identity; failing it would be
    this worker deciding, with no evidence, that the first attempt died. Plan §3.3 puts that
    decision in a reconciler that reads the run's own evidence, and until that exists the honest
    state is a non-terminal request with an expiring lease — which is precisely what
    ``expired_requests`` is the query for.
    """
    error = (
        f"materialization request {request.request_id!r} is "
        f"{request.lifecycle_state.value!r}: another worker claimed it, so this delivery cannot be "
        f"driven without minting a second generation under one durable identity. Adopting a "
        f"claimed request is the reconciler's decision (plan §3.3), made from the run's evidence")
    fail_materialization(conn, claim, error=error, permanent=True)
    counters.incr("materialize.lane.unclaimable")
    log("materialize.lane.unclaimable", level="warning", request_id=request.request_id,
        lifecycle_state=request.lifecycle_state.value)
    return MaterializationLaneOutcome("unclaimable", request.request_id, detail=error)


def _stale(
    claim: MaterializationQueueClaim, request_id: str | None,
) -> MaterializationLaneOutcome:
    """This worker's lease was reclaimed and a newer fence owns the row. Nothing is written: every
    terminal write in this lane is fence-guarded, so the loser of a fence race writes nothing at
    all rather than half of something."""
    counters.incr("materialize.lane.stale_lease")
    log("materialize.lane.stale_lease", level="warning", request_id=request_id,
        queue_id=claim.id, lease_fence=claim.lease_fence)
    return MaterializationLaneOutcome("stale_lease", request_id)
