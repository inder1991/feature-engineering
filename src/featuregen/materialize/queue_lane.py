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

**THE LEASE, and why it is sized rather than renewed.** The one stage of a compile whose duration is
open-ended is the L0 build proof, and it runs INSIDE the chain's commit transaction
(``chain._commit``) — a transaction that has already UPDATEd ``materialization_request`` to
``running``, so it holds a row lock on the exact row a renewal would have to touch. A renewer on a
second connection would block on that lock until the transaction committed, i.e. precisely through
the window a renewal exists to cover; a renewer on THIS connection cannot run at all, because the
connection is inside the subprocess call. (Renewal is available during the EARLIER stages, which are
outside that transaction — but those are the bounded ones, so it would be available exactly where it
is not needed.) Mid-compile renewal is therefore not a policy this lane declined.

What IS available is a bound. The one unbounded-looking part of a compile is the build proof, and
its duration is configured (``L0Interpreter.timeout_seconds``, no default, by Task 6's decision), so
:attr:`MaterializationLaneConfig.lease_seconds` is that timeout plus a compile budget. Raise the L0
timeout and the lease rises with it — structurally, not by somebody remembering to. Both leases are
sized from that one number: the queue row's (so no second worker reclaims a live job) and the
request's (so §3.3's reconciler does not adopt one).

And if a lease expires anyway, nothing silent happens. ``accept_request`` and ``advance_lifecycle``
are conditional UPDATEs naming the states they are legal from, so a reconciler that tried to adopt a
live request would block on the chain's row lock and then match no row — a loud refusal, not a
double write. The lane adds the third guard: it drives a request it did not itself accept ONLY when
that request is ``accepted`` with a demonstrably EXPIRED lease, which proves no run evidence exists
(:func:`_adoptable`); anything else is refused to plan §3.3's reconciler.

**A LEASE IS RELEASED WHEN A WORKER GIVES UP.** A retryable failure that left the request leased
would be a retry that could never succeed — the redelivery would read a live claim and refuse it. So
:func:`_retryable` expires this worker's lease, and the next delivery adopts it. That, plus adoption,
is what makes "retryable" a true word in this lane rather than a label on a dead end.

**AND NONE OF IT RUNS UNLESS THE DEPLOYMENT SAYS SO.** :func:`materialization_enabled` (T9) is the
switch, default OFF, read by the worker stage before the claim. Off, this lane costs a deployment
nothing at all — not a query, not a counter, not a log line. See that function for why the switch
belongs above the claim rather than inside it.
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
    ChainStage,
    CompiledGroup,
    L0Interpreter,
    NodeAssembler,
    RunExecution,
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
from featuregen.materialize.metastore_sql import (
    MetastoreEndpoint,
    MetastoreSession,
    SqlMetastoreAdapter,
)
from featuregen.materialize.publish import PublicationSwap, PublishMechanism
from featuregen.materialize.publish_sql import SqlPublicationSwap
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
from featuregen.materialize.submit import LocalClusterSubmitter, PipelineSubmitter
from featuregen.materialize.validation import MetastoreMetadata
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
    "MATERIALIZATION_ENV_VARS",
    "MATERIALIZATION_FLAG",
    "MATERIALIZATION_HANDLER",
    "MaterializationJobV1",
    "MaterializationLaneConfig",
    "MaterializationLaneOutcome",
    "decode_job",
    "encode_job",
    "enqueue_materialization",
    "lane_config_from_env",
    "materialization_enabled",
    "materialization_message_id",
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

#: The prefix a request's message id is built from, exported so §3.3's reconciler can correlate the
#: two records IN SQL (``queue.message_id = %s || materialization_request.request_id``) rather than
#: by fetching every candidate's id and interpolating it. One definition, two readers.
MATERIALIZATION_MESSAGE_PREFIX = f"{_NAMESPACE}:"


def materialization_message_id(request_id: str) -> str:
    """The queue message id one request's job is frozen under — ONE definition of the derivation.

    A request is one message: this is the idempotency unit ``enqueue_checked`` refuses to see reused
    for materially different work, and it is how §3.3's reconciler asks "can this request's message
    still be delivered?". Spelled once because two spellings of a derived id are two ids, and the
    second one silently finds nothing.
    """
    return f"{MATERIALIZATION_MESSAGE_PREFIX}{request_id}"

#: The payload's shape version. Named and checked rather than assumed: the payload IS this lane's
#: frozen work item, so a worker running older code must refuse a job it cannot read whole rather
#: than compile a group from the half it understands.
PAYLOAD_VERSION = 1

#: The lease a job is claimed under BEFORE the deployment's compile budget is known — long enough to
#: read a request row and resolve a configuration, and no longer, so a worker that dies in that
#: window releases the job promptly.
_BOOTSTRAP_LEASE_SECONDS = 60.0

#: The lease a worker leaves behind when it GIVES UP a request it had claimed — long enough to be a
#: positive duration (``request_store`` refuses zero: "a lease must have a positive duration"), short
#: enough that the redelivery which follows a backoff finds it expired and adoptable. Releasing a
#: lease by expiring it is the only release migration 1053 permits: an ``accepted`` row must HAVE
#: one, so "nobody is working this" cannot be spelled as a null.
_RELEASED_LEASE_SECONDS = 0.001

#: Everything a compile does APART from the build proof: resolve, admit, compile every expression,
#: authorize, derive the contract, plan, bind, render, seal and write the tree. Bounded by the DB
#: and the local filesystem rather than by configuration, so it is a stated budget rather than a
#: derived one — and it is stated ONCE, here.
COMPILE_BUDGET_SECONDS = 600.0

#: G-2's three stages, read as a SET rather than spelled out at the fold, so a stage added to
#: ``ChainStage`` and forgotten here fails the test that names this membership rather than being
#: silently reported as a governed refusal.
_G2_STAGES = frozenset({ChainStage.PREPARE_RUN, ChainStage.VALIDATE_L1, ChainStage.SUBMIT})

_PROJECT_ROOT_ENV = "FEATUREGEN_MATERIALIZE_PROJECT_ROOT"
_INVENTORY_ENV = "FEATUREGEN_MATERIALIZE_INVENTORY"
_L0_PYTHON_ENV = "FEATUREGEN_MATERIALIZE_L0_PYTHON"
_L0_TIMEOUT_ENV = "FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS"

# ── G-2/G-3: the EXECUTION block, all of it or none of it ────────────────────────────────────────
#
# Five values reach the metastore endpoint and three describe the run's execution. They are eight
# separate variables rather than one connection string on purpose, and the reason is
# ``data_agent.connection``'s: *"A field that could hold a secret eventually holds one, and then it
# is in a log, a JSON column, an error message or an LLM prompt."* A DSN has a password slot; these
# do not. The credential, when the mechanism needs one, is resolved at the point of use.
_METASTORE_ENGINE_ENV = "FEATUREGEN_MATERIALIZE_METASTORE_ENGINE"
_METASTORE_HOST_ENV = "FEATUREGEN_MATERIALIZE_METASTORE_HOST"
_METASTORE_PORT_ENV = "FEATUREGEN_MATERIALIZE_METASTORE_PORT"
_METASTORE_AUTH_ENV = "FEATUREGEN_MATERIALIZE_METASTORE_AUTH"
_METASTORE_PRINCIPAL_ENV = "FEATUREGEN_MATERIALIZE_METASTORE_PRINCIPAL"
_STAGING_BASE_ENV = "FEATUREGEN_MATERIALIZE_STAGING_BASE"
_SUBMIT_PYTHON_ENV = "FEATUREGEN_MATERIALIZE_SUBMIT_PYTHON"
_SUBMIT_TIMEOUT_ENV = "FEATUREGEN_MATERIALIZE_SUBMIT_TIMEOUT_SECONDS"

#: The execution block, in the order an operator reads them. ALL of them or NONE of them: a
#: deployment that stated half of these has not chosen a posture, it has made a mistake, and
#: ``RunExecution`` would refuse the half anyway (``execution_for``'s "ALL FIVE or ``None``").
_EXECUTION_ENV_VARS = (
    _METASTORE_ENGINE_ENV, _METASTORE_HOST_ENV, _METASTORE_PORT_ENV, _METASTORE_AUTH_ENV,
    _METASTORE_PRINCIPAL_ENV, _STAGING_BASE_ENV, _SUBMIT_PYTHON_ENV, _SUBMIT_TIMEOUT_ENV)

#: **THE KILL SWITCH.** One env gate for the whole of materialization, default OFF — see
#: :func:`materialization_enabled`.
MATERIALIZATION_FLAG = "FEATUREGEN_MATERIALIZE_ENABLED"

#: Every variable a deployment sets for this lane, in ONE list. A test asserts each member is
#: documented in ``.env.example`` and ``deploy/kind/k8s/20-backend.yaml``, so a sixth variable added
#: here without telling the two files a deployer actually edits fails CI rather than a deployment.
MATERIALIZATION_ENV_VARS = (
    MATERIALIZATION_FLAG, _PROJECT_ROOT_ENV, _INVENTORY_ENV, _L0_PYTHON_ENV, _L0_TIMEOUT_ENV,
    *_EXECUTION_ENV_VARS)

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
    #: The run's business date (``YYYY-MM-DD``), or ``None`` for "this trigger asked for no run".
    #: G-2's ``prepare_run`` resolves every partition from it, so it is a per-run DECLARATION and
    #: cannot come from the deployment: a worker-wide date would run every group at whatever day the
    #: worker was configured with. ``None`` is not a default that means "today" — it is the honest
    #: statement that the trigger asked for a compilation and not for an execution, and the chain
    #: records it as an unprepared run rather than picking a date on the caller's behalf.
    business_dt: str | None = None


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
        "business_dt": job.business_dt,
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
        # `.get` with no version bump: an OPTIONAL key added to a payload version is readable by
        # both spellings — a job frozen before D1 decodes as `business_dt=None`, which is exactly
        # what it asked for (a compilation, no run), and one frozen after it is unreadable by no
        # worker. Bumping PAYLOAD_VERSION would instead dead-letter every in-flight job.
        business_dt=(None if material.get("business_dt") is None
                     else _text(material.get("business_dt"), where="job business_dt")),
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

    Partitioned by the LOGICAL GROUP, not the request, because §10.1 writes one ``group_binding``
    per logical name and publication is atomic per group.

    **What that partition key actually delivers, exactly.** ``queue_one_inflight_per_partition``
    (``runtime/ddl.py:52``) is a UNIQUE partial index on ``(partition_key) WHERE status='leased'``,
    so while one job of a group is leased the database itself refuses a second — the claim's
    ``NOT IN`` subquery is an optimization and the index is the guarantee. What it does NOT deliver
    is mutual exclusion across a lease EXPIRY: once ``reclaim_stuck_queue`` returns an overrun row
    to ``ready``, a different request for the same group is claimable and can compile concurrently
    with the first. That case degrades LOUDLY rather than silently — the two runs have different
    derived generation ids, so they collide on ``record_group_binding``'s unique logical name and
    one transaction rolls back whole — but it is a collision, not an exclusion, and the sizing of
    the lease is what keeps it rare.
    """
    if job.request_id != request.request_id:
        raise ValueError(
            f"the job names request {job.request_id!r} and the recorded request is "
            f"{request.request_id!r}: the message id is derived from the request, so enqueuing "
            f"these together would freeze one request's declarations under another's identity")
    return enqueue_checked(
        conn,
        message_id=materialization_message_id(request.request_id),
        partition_key=f"{_NAMESPACE}:{request.logical_group_name}",
        handler=MATERIALIZATION_HANDLER,
        payload=encode_job(job),
        priority=priority,
    )


# ── the kill switch ──────────────────────────────────────────────────────────────────────────────


def materialization_enabled() -> bool:
    """Whether this deployment runs materialization at all. Default **OFF**.

    **THE ONE PUBLIC DEFINITION.** Task 8's route consults this rather than re-reading the variable
    (``feature_assist``'s RF-C3 rule, for the reason that rule exists: two readings of one switch are
    two switches, and the second one is the one nobody flips).

    **Why it is a kill switch and not just a feature gate.** ``run_worker_once`` drives every stage
    on one connection, in order, in one thread. A materialization compile therefore holds the relay,
    the timers, the projections, the drift scan and the ingestion sweep for its whole duration —
    :data:`COMPILE_BUDGET_SECONDS` plus the deployment's configured L0 timeout, in the worst case.
    An operator who needs that to stop has exactly two other options today: stop the worker (which
    also stops everything the compile was blocking) or drop the queue rows (which destroys governed
    work). This is the third.

    **Read from the environment on EVERY call**, never captured at import and never cached. The
    stage reads it once per tick, before the claim, so the first tick after this process's
    environment says otherwise obeys — and a compile already in flight is never interrupted (a
    leased message cannot be un-claimed, and the chain's writes are one transaction).

    **What that buys, precisely.** A container's environment is fixed at start, so on Kubernetes
    flipping the ConfigMap means restarting that pod. The point is what it comes back as: a worker
    doing everything EXCEPT materialization, rather than no worker at all. And because an undrained
    job stays a durable ``ready`` queue row, nothing is lost in between — the complementary lever
    that needs no restart is the queue itself (pushing those rows' ``available_at`` forward pauses
    the drain from psql, since :func:`claim_materialization` only takes rows that are due).

    **The truthy set is the platform's**, copied verbatim from
    ``overlay/upload/feature_assist.py:feature_context_enabled`` — ``.strip().lower()`` into
    ``{"1", "true", "yes", "on"}``. It is a strict superset of the ``== "1"`` form the older flags
    (``OVERLAY_PASS_C``, ``FEATUREGEN_INTENT_LIVE_CROSS_CATALOG``) use, so the documented ``"0"`` /
    ``"1"`` values mean the same thing under either reading. Anything unrecognised — a typo, a
    truncated write — is OFF, which for a kill switch is the safe side of every ambiguity.
    """
    return os.environ.get(MATERIALIZATION_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


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

    **``metastore``/``submitter``/``swap``/``staging_base`` are G-2 and G-3's half of the same
    posture** and follow
    the same rule: ``None`` is an OUTCOME — an unprepared run, terminating ``RUN_FAILED`` at
    ``ChainStage.PREPARE_RUN`` — never a skipped stage. They are separate fields here and one frozen
    :class:`~featuregen.materialize.compile.chain.RunExecution` at the chain, because a deployment
    supplies them one at a time while the chain must be handed either a complete seam or ``None``:
    :meth:`execution_for` is the ONE place that fold happens, so no caller can assemble a half one.

    **SUCCESSOR 2 moved the boundary.** These four used to be unfillable from a deployment — the
    adapters are objects, not strings, and no implementation of either seam existed — so
    :func:`lane_config_from_env` produced ``metastore=None`` and every deployed run was honestly
    unprepared. It now builds all four from the eight-variable EXECUTION block
    (:func:`_execution_from_env`) when a deployment states it, and still produces ``None`` when it
    does not: unset remains an outcome, and the run it drives is still recorded as unprepared.
    """

    inventory: ClusterInventoryV1
    project_root: str
    l0: L0Interpreter | None
    assemble_nodes: NodeAssembler = _wired_assembler
    clock: Callable[[], str] = _utc_now_iso
    compile_budget_seconds: float = COMPILE_BUDGET_SECONDS
    #: G-2's metastore seam — partitions, columns and read scope, metadata only.
    #: ``metastore_sql.SqlMetastoreAdapter`` is the implementation a configured deployment gets;
    #: ``inventory.MetastoreInventoryAdapter`` is a different seam (capture's three questions).
    metastore: MetastoreMetadata | None = None
    #: G-2's submitter. ``LocalClusterSubmitter`` is the one implementation (``submit.py:169``).
    submitter: PipelineSubmitter | None = None
    #: G-3's publish seam: the metastore write and the reader-visible pointer switch, the one act
    #: §10.3's probe proves. ``publish_sql.SqlPublicationSwap`` is the implementation, and it shares
    #: the metastore adapter's session — one transport, so the two cannot see two worlds.
    swap: PublicationSwap | None = None
    #: §9's staging root base, under which ``staging_root_for`` derives a per-generation path.
    staging_base: str | None = None

    def execution_for(self, job: MaterializationJobV1) -> RunExecution | None:
        """The chain's G-2 seam, or ``None`` when this deployment and this job cannot execute a run.

        Five values, four from the deployment and one — the business date — from the JOB, because a
        business date is a per-run declaration and a deployment-wide one would run every group at
        whatever date the worker was configured with. ALL FIVE or ``None``: a half-configured seam
        has no honest partial behaviour, and ``RunExecution`` would refuse it anyway.
        """
        if (self.metastore is None or self.submitter is None or self.swap is None
                or self.staging_base is None or job.business_dt is None):
            return None
        return RunExecution(metastore=self.metastore, submitter=self.submitter, swap=self.swap,
                            business_dt=job.business_dt, staging_base=self.staging_base)

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
    execution = _execution_from_env()
    return MaterializationLaneConfig(
        inventory=load_inventory(inventory_path),
        project_root=project_root,
        l0=None if not python_executable else L0Interpreter(
            python_executable=python_executable,
            timeout_seconds=_positive_float(timeout, _L0_TIMEOUT_ENV)),
        **execution,
    )


def _execution_from_env() -> dict[str, Any]:
    """G-2/G-3's seams from the environment — all of them, or an honestly unconfigured deployment.

    **Unset is an OUTCOME and not a missing setting**, which is ``l0=None``'s rule two stages later
    (``RunExecution``: *"``execution=None`` is a POSTURE"*): the returned mapping is empty, every
    seam stays ``None``, and every run this lane drives is recorded as honestly unprepared at
    ``PREPARE_RUN``. Half-set is neither, so it raises and names the variables that are missing —
    a deployment that stated a metastore host and no staging base has not chosen a posture.

    The two adapters share ONE :class:`~featuregen.materialize.metastore_sql.MetastoreSession`.
    That is the whole reason they are built here together rather than in two places: L1 asks the
    environment which partitions exist and the swap then makes a generation visible in it, and two
    sessions could answer from two worlds.
    """
    stated = {name: os.environ.get(name, "").strip() for name in _EXECUTION_ENV_VARS}
    if not any(stated.values()):
        return {}
    missing = [name for name, value in stated.items() if not value]
    if missing:
        raise ValueError(
            f"the materialization EXECUTION block is half configured: {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} unset while "
            f"{', '.join(name for name, value in stated.items() if value)} "
            f"{'is' if len(missing) == len(stated) - 1 else 'are'} set. All eight or none: "
            f"`RunExecution` refuses a half-configured seam, and a deployment that cannot execute "
            f"a run says so by setting none of them — every run is then recorded as unprepared, "
            f"which is an outcome rather than a failure")

    session = MetastoreSession(MetastoreEndpoint(
        engine=stated[_METASTORE_ENGINE_ENV],
        host=stated[_METASTORE_HOST_ENV],
        port=_whole_number(stated[_METASTORE_PORT_ENV], _METASTORE_PORT_ENV),
        auth_mechanism=stated[_METASTORE_AUTH_ENV],
        principal=stated[_METASTORE_PRINCIPAL_ENV]))
    return {
        "metastore": SqlMetastoreAdapter(session),
        "swap": SqlPublicationSwap(session),
        "submitter": LocalClusterSubmitter(
            python_executable=stated[_SUBMIT_PYTHON_ENV],
            timeout_seconds=_positive_float(stated[_SUBMIT_TIMEOUT_ENV], _SUBMIT_TIMEOUT_ENV)),
        "staging_base": stated[_STAGING_BASE_ENV],
    }


def _whole_number(raw: str, variable: str) -> int:
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"{variable} is {raw!r}, which is not a port number") from exc


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

    THE ORDER OF THE FIRST FOUR STEPS IS THE RECOVERABILITY DESIGN, and each one is placed where it
    is because of what a failure there leaves behind:

    1. **claim** under a short bootstrap lease — nothing else is known yet.
    2. **decode** the payload. A payload nothing can read will never read, so this is permanent —
       and it happens before any request is touched, because a garbled payload may not even name a
       real request.
    3. **resolve the configuration** — BEFORE accepting, and classified RETRYABLE. A deployment
       missing ``FEATUREGEN_MATERIALIZE_*`` is an operator's five-second fix, and a request left at
       ``requested`` behind a live queue row still has a durable owner that will drive it once the
       variable is set. Accepting first and then failing on configuration would burn every arriving
       request into a TERMINAL ``failed`` that no fix can recover.
    4. **size the queue lease** to the now-known budget, before any request write. A lost fence
       here means another worker owns the row and this one has changed nothing at all.

    Only then is the request read and claimed.
    """
    claim = claim_materialization(conn, owner=owner, lease_seconds=_BOOTSTRAP_LEASE_SECONDS)
    if claim is None:
        return MaterializationLaneOutcome("idle")

    request_id: str | None = None
    accepted = False
    try:
        job = decode_job(claim.payload)
        request_id = job.request_id
    except (ValueError, TypeError) as exc:
        return _deterministic_failure(conn, claim, None, accepted=False, status="failed",
                                      error=f"{type(exc).__name__}: {exc}")
    try:
        resolved = lane_config_from_env() if config is None else config
    except (ValueError, OSError) as exc:
        # NOT deterministic in the sense that matters: the job is fine and the catalog is fine, and
        # the next attempt runs against whatever the operator sets. `OSError` is included because
        # `load_inventory` deliberately does not translate "the operator pointed at a path that is
        # not there" into a malformed declaration.
        return _retryable(conn, claim, request_id, accepted=False,
                          error=f"the materialization lane is not configured: {exc}")
    if not renew_materialization(conn, claim, lease_seconds=resolved.lease_seconds):
        return _stale(claim, request_id)

    try:
        request = read_request(conn, request_id=request_id)
        if request is None:
            raise ValueError(
                f"no materialization request {request_id!r}: the request row is minted BEFORE the "
                f"job is enqueued, so a job naming none can only be a caller that assembled the "
                f"trigger wrongly — and no retry will make the row appear")
        if request.lifecycle_state is RequestLifecycle.REQUESTED:
            accept_request(conn, request_id=request_id, lease_seconds=resolved.lease_seconds)
            accepted = True
        elif _adoptable(request):
            _adopt(conn, request, lease_seconds=resolved.lease_seconds)
            accepted = True
        elif not request.lifecycle_state.is_terminal():
            return _unclaimable(conn, claim, request)

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
            execution=resolved.execution_for(job),
            clock=resolved.clock,
            contract_overrides=job.contract_overrides,
        )
    except _DETERMINISTIC as exc:
        return _deterministic_failure(conn, claim, request_id, accepted=accepted,
                                      status="failed", error=f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001 — the lane's backstop, mirroring the formula lane
        # NOT deterministic: this attempt failed for a reason another one might not, and
        # terminalizing the request here would record a verdict about a feature from a dropped
        # connection.
        return _retryable(conn, claim, request_id, accepted=accepted,
                          error=f"{type(exc).__name__}: {exc}")

    return _recorded(conn, claim, outcome)


def _adoptable(request: MaterializationRequestV1) -> bool:
    """Whether this worker may take over a request somebody else claimed.

    ``accepted`` with an EXPIRED lease, and nothing else. The two halves both matter.

    *Why ``accepted`` is safe to adopt without reading run evidence.* Every write the chain makes is
    inside ``_commit``'s single transaction, which moves the request ``accepted → running →
    committed|failed`` and commits all of it or none. So a durably-visible ``accepted`` request
    PROVES no generation, no compiled artifact, no validation report and no run event exist —
    there is nothing for plan §3.3's evidence-reading reconciler to read, and adoption is not a
    verdict about a run. It is the retry the queue was always going to perform.

    *Why the lease must have expired.* A live lease is the only evidence available that another
    worker is still working, and it is the same evidence ``expired_requests`` uses. A request whose
    lease is live stays :func:`_unclaimable`.

    ``running`` is deliberately NOT adoptable. It is unreachable durably in G-1 (no reader ever
    observes it — it exists only inside the chain's transaction), so a ``running`` row that somehow
    appears is a state this code cannot account for, and it is exactly the case where plane evidence
    might exist. That one is the reconciler's.

    And if this rule is ever wrong — two workers driving one request — the failure is LOUD, never
    silent: the generation id is a pure function of the request id, so the second compile collides
    on ``materialization_generation``'s primary key and its whole transaction rolls back. It can
    never append a second terminal event.
    """
    return (request.lifecycle_state is RequestLifecycle.ACCEPTED
            and request.lease_expires_at is not None
            and request.lease_expires_at < _datetime.datetime.now(tz=_datetime.UTC))


def _adopt(conn: psycopg.Connection, request: MaterializationRequestV1, *,
           lease_seconds: float) -> None:
    """Take over an abandoned claim by taking over its LEASE — ``renew_lease``'s one real use.

    There is no ``accepted → accepted`` edge and no second door into ``accepted``
    (``advance_lifecycle`` refuses that target by design), which is correct: acceptance is the
    original claim and it happened once. What changes hands is the lease, and ``renew_lease`` is
    legal from exactly the two states that hold one.
    """
    renew_lease(conn, request_id=request.request_id, lease_seconds=lease_seconds)
    counters.incr("materialize.lane.adopted")
    log("materialize.lane.adopted", level="warning", request_id=request.request_id,
        expired_at=str(request.lease_expires_at))


def _retryable(
    conn: psycopg.Connection,
    claim: MaterializationQueueClaim,
    request_id: str | None,
    *,
    accepted: bool,
    error: str,
) -> MaterializationLaneOutcome:
    """A transient failure — reschedule the message, and RELEASE the request so the retry can work.

    The release is the whole point. A redelivered message re-reads the request, and a request left
    ``accepted`` under a full-size lease reads as "a worker is on it": :func:`_adoptable` refuses
    it, :func:`_unclaimable` dead-letters the delivery, and the retry this branch exists to arrange
    can never succeed. So a worker that is giving up expires its own lease.

    Expiring it, rather than clearing it: migration 1053 CHECKs that an ``accepted`` row HAS a lease
    ("'accepted' MEANS claimed-and-leased"), and a lease-less accepted row would be non-terminal and
    invisible to ``expired_requests`` — the exact loss that table exists to prevent. An expired
    lease says precisely the true thing: this request was claimed, and nobody is working it now.

    The queue write goes FIRST and gates the release: if the fence has moved, another worker owns
    both the row and the request, and releasing a lease it just took would be this worker reaching
    into somebody else's claim.

    When the retry BUDGET is gone the message is dead-lettered by ``fail_materialization``, and then
    there is no owner left at all — so the request is failed rather than left adoptable by a
    redelivery that will never come.

    Both writes go through :func:`_still_ours`, for the reason its sibling states: ``renew_lease``
    and ``advance_lifecycle`` both refuse a terminal request with a ``ValueError``, so a concurrent
    adopter that finished first would turn a legible retryable failure into an uncaught exception
    out of the handler.

    The status reports WHAT HAPPENED, which is not the same as what was attempted: when the budget
    is gone and this call never held the request, the message is dead and the request is untouched —
    ``dead_letter``, not ``failed``.

    The terminalizing write is NARROWED to ``accepted``, the state :func:`_still_ours` just re-read
    and required. Without ``expected_from`` the UPDATE matches every state ``failed`` is legal from,
    ``running`` included — so a request that moved on between that re-read and this write would be
    failed on evidence gathered about a different state. It is unreachable in G-1 (``running``
    exists only inside ``_commit``'s transaction, which holds this row's lock) and durable the
    moment G-2's ``prepare_run`` lands; ``reconcile.py:541`` closed the same hole for the same
    reason, and this is its twin.
    """
    exhausted = claim.attempts >= claim.max_attempts
    ended = False
    ours = (_still_ours(conn, request_id, accepted=accepted)
            if fail_materialization(conn, claim, error=error, permanent=False) else None)
    if ours is not None:
        if exhausted:
            advance_lifecycle(conn, request_id=ours, to_state=RequestLifecycle.FAILED,
                              expected_from=RequestLifecycle.ACCEPTED)
            ended = True
        else:
            renew_lease(conn, request_id=ours, lease_seconds=_RELEASED_LEASE_SECONDS)
    if not exhausted:
        status = "retryable"
    else:
        status = "failed" if ended else "dead_letter"
    counters.incr(f"materialize.lane.{status}")
    log("materialize.lane.retryable", level="warning", status=status, request_id=request_id,
        error=error, attempts=claim.attempts, max_attempts=claim.max_attempts,
        exhausted=exhausted)
    return MaterializationLaneOutcome(status, request_id, detail=error)


def _still_ours(conn: psycopg.Connection, request_id: str | None, *,
                accepted: bool) -> str | None:
    """The request id when this call may still write that request's lifecycle, else ``None``.

    The ONE rule, in one place — and it returns the id rather than a boolean so the narrowing is the
    type checker's too, not just a fact about the call order.

    Two conditions, both load-bearing. It must be a request this call itself accepted or adopted
    (one somebody else holds is not this worker's to terminalize), and it must STILL be ``accepted``
    when re-read: "this call raised" and "the chain recorded a terminal" are not mutually exclusive
    — a fault after ``_commit``'s transaction closes is both, and so is losing a race to an adopter.
    Writing a lifecycle from either state raises a SECOND exception out of an error path, replacing
    a legible failure with a stage fault that says nothing about the job.
    """
    if not accepted or request_id is None:
        return None
    current = read_request(conn, request_id=request_id)
    return (request_id if current is not None
            and current.lifecycle_state is RequestLifecycle.ACCEPTED else None)


def _recorded(
    conn: psycopg.Connection, claim: MaterializationQueueClaim, outcome: CompiledGroup,
) -> MaterializationLaneOutcome:
    """The chain returned, so it has already recorded everything — the lane only closes the row.

    A governed refusal is NOT a queue failure: the message was processed and its verdict is durable
    on the request (and, once a generation exists, on the plane). Re-delivering it could only
    produce a replay.

    ``run_failed`` is D1's fourth member, and it exists for ``build_unproven``'s reason one stage
    later: a run that stopped at ``PREPARE_RUN``, ``VALIDATE_L1`` or ``SUBMIT`` was compiled,
    rendered, sealed AND build-verified, and then could not be prepared, could not be validated
    against the environment, or ran and failed. Folding those into ``refused`` would tell an
    operator the catalog refused their feature when the cluster is what did not answer — and
    ``CompiledGroup.refusal`` is often ``None`` on those paths (an unconfigured execution seam and a
    failed submission are not governed verdicts at all), so ``detail`` would be empty too. The
    chain's ``l1_report`` and ``submission`` are where the three are told apart.

    ``build_unproven`` and ``refused`` are separate members because they are opposite facts about
    one run. A run that stopped at :attr:`ChainStage.VALIDATE_L0` was compiled, rendered and sealed
    and then FAILED to build (or was never proved to) — there is no governed refusal in it at all,
    and ``CompiledGroup.refusal`` is ``None``. A ``refused`` run's verdict is a
    :class:`MaterializationRefused` from a governed stage. Folding the first into the second would
    tell an operator the catalog refused their feature when the project simply does not import; the
    recorded ``ValidationReportV1`` is where "does not build" and "was never proven" are told apart.
    """
    if outcome.replayed:
        status = "replayed"
    elif outcome.lifecycle_state is RequestLifecycle.COMMITTED:
        status = "completed"
    elif outcome.stopped_at is ChainStage.VALIDATE_L0:
        status = "build_unproven"
    elif outcome.stopped_at in _G2_STAGES:
        status = "run_failed"
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

    The state is RE-READ rather than assumed (:func:`_still_ours`), and the write is skipped unless
    it is still ``accepted`` — then NARROWED to that same state, so the re-read is a precondition of
    the UPDATE rather than merely a check that preceded it. See :func:`_retryable` for the argument;
    it is ``reconcile.py:541``'s, applied to the lane.
    """
    ours = _still_ours(conn, request_id, accepted=accepted)
    if ours is not None:
        advance_lifecycle(conn, request_id=ours, to_state=RequestLifecycle.FAILED,
                          expected_from=RequestLifecycle.ACCEPTED)
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
