"""Spec A §2's ORCHESTRATOR — the chain that turns a governed feature into a sealed project.

The module ``compile/__init__.py`` reserved and did not build ("§2's orchestrator is not built, and
inventing a shell for it would be a second place the chain is described"), recorded as DEFERRED-WORK
A.24. It lands in this SIBLING module rather than in the package's ``__init__`` because
``identity.py:52`` imports that package: a chain there would close ``compile → identity → compile``,
and the failure would surface as an ``ImportError`` in whichever module happened to be imported
first. This file may import ``identity`` freely; ``compile/__init__.py`` stays free of imports.

WHAT THIS CHAIN COVERS. Phase G §4 stages the programme, and this is G-1: stages 1 through SEAL,
plus §11.2's **L0**. ``prepare_run``, ``run_l1`` and ``submit`` are G-2, which is designed but
**not approved**; publication is G-3 and does not exist at all. Nothing here reaches past
:func:`~featuregen.materialize.validation.run_l0`, which launches a *local* interpreter and touches
no cluster.

THE TRUTHFUL TERMINAL, and why it is not a bug to be worked around. ``select_publisher`` decides
publication capability from recorded attestations, and the only thing that can produce one is
``probe_publication_capability`` — *named* at ``publish.py:326`` and absent from the repository. So
every G-1 run reaches ``CAPABILITY_UNPROVEN``, and that verdict is exactly right: the project WAS
compiled, rendered and sealed, and publication genuinely is unproven. The run therefore terminates
``PUBLICATION_REFUSED`` carrying that code.

WHAT THAT TERMINAL MEANS, EXACTLY — and the two it must never be swapped for. There are three
outcomes once a project is sealed, one terminal each, and they are not interchangeable:

* L0 ``passed`` → ``PUBLICATION_REFUSED (CAPABILITY_UNPROVEN)``. *Compiled, rendered, sealed, and
  PROVEN to import and construct its pipeline in a separate interpreter; publication unproven.*
  This is the only terminal in this module that folds to a non-failed status
  (``RunStatus.REFUSED``), and it is legitimate only because a build proof was obtained.
* L0 ``failed`` → ``RUN_FAILED``, request ``failed``. *The build was proved NOT to work.* The
  findings and their §11.2 classification — who fixes it — are in the report and in the event
  detail.
* L0 ``error`` → ``RUN_FAILED``, request ``failed``. *The build was never proven.* §11.2's
  ``error`` is "the validation could not run", carries **zero** findings, and is neither a pass nor
  a failure of the artifact. It reaches the same terminal as a failure for one reason: the terminal
  above is the only alternative this module has, and it would claim a proof this run does not hold.

**L0 is always ATTEMPTED and its absence is an OUTCOME, never a skip** (the Task 6 decision).
Whether an interpreter is usable cannot be known before using it — a path that exists may be
non-executable, the wrong architecture, or missing ``kedro`` — so availability cannot be a
precondition, only a result. ``run_l0`` already says so: a launch failure, a timeout and a probe
that printed no verdict are one answer, ``status="error"`` with no findings. A deployment that
configures no interpreter at all (``l0=None``) is the same non-answer stated earlier, and is
recorded as the same ``error`` report rather than as a fourth vocabulary word meaning "skipped".
A skipped gate that left ``PUBLICATION_REFUSED`` behind would be indistinguishable from a passed
one, on a plane that can never retract it.

**This module must never append** ``RunEventKind.PUBLISHED`` (plan §3.5). The plane is append-only,
with a partial unique index admitting one terminal event per run and migration 1044's BEFORE-INSERT
ordering trigger refusing anything after it — so a publication claim, once written, can never be
retracted by any code path that exists. A test reads this module's AST and asserts that
``PUBLICATION_REFUSED`` and ``RUN_FAILED`` are the only two event kinds it names.

WHY ``RUN_FAILED`` AND NOT ``GATES_FAILED``, against the two kinds' documented meanings.
``GATES_FAILED`` is "a §9 gate failed — the group is rejected and the previous partition stays
untouched"; it folds to ``RunStatus.REJECTED`` and its place in the sequence is after
``COMPUTATION_COMPLETED``, because §9's gates run *inside* the submitted pipeline over output that
has been staged. In G-1 nothing is submitted, nothing is computed and no partition exists, so that
kind would assert a computation that never happened and a gate that never ran — a second claim the
plane could not retract. ``RUN_FAILED`` is documented as "the run failed **outside** the gates —
execution, submission or the environment", which is exactly where a build verification sits: before
any run, and (for ``error``) squarely in the environment.

WHERE THE RECORD LIVES, and why a pre-render refusal writes nothing to the plane.
``MaterializationGeneration`` requires ``generated_project_hash``, which does not exist until the
project is sealed; ``materialization_run_event.generation_id`` is a foreign key to that row. So a
run refused before rendering has, by construction, nowhere in the control plane to be recorded —
and it does not need one, because Phase G §3.2's ``materialization_request`` row (migration 1053)
was minted before any work began. Such a run advances the request to ``failed`` and returns the
stage's own refusal; a run that reaches the plane advances it to ``committed``, because its evidence
is there.

REFUSALS ARE EVIDENCE. Each stage's contract is honoured exactly as that stage wrote it — some
RETURN a :class:`~featuregen.materialize.codes.MaterializationRefused`, two RAISE it — and in every
case the object the stage produced is the object this chain records and returns. It is never
re-raised with a code of this module's choosing, because that is how one stage's verdict quietly
becomes another's. Call-assembly errors (``ValueError``, ``TypeError``,
``FeatureNamePlanError``, ``SchemaError``) are NOT caught: §14's vocabulary has no member for "this
code assembled the call wrongly", and typing one into it would tell an operator the catalog refused
their feature when in fact the caller is broken.
"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypeVar

from featuregen.contracts.db import DbConn
from featuregen.materialize.admission import AdmittedFeature, admit_artifacts
from featuregen.materialize.binding import GroupContractBinding, bind_group, plan_revision
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    CadenceDecl,
    ContractOverrides,
    MaterializationContractV1,
    derive_group_contract,
)
from featuregen.materialize.control_plane import (
    MaterializationGeneration,
    MaterializationRunEvent,
    RunEventKind,
    append_run_event,
    read_group_binding,
    read_run_events,
    record_compiled_artifact,
    record_generation,
    record_group_binding,
    record_plan_revision,
)
from featuregen.materialize.group_plan import (
    FeatureGroupPlanV1,
    PlannedFeature,
    build_group_plan,
    group_plan_hash,
)
from featuregen.materialize.identity import SealedProject
from featuregen.materialize.inputs import PhysicalInputRequirement, derive_requirement
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.ir import (
    AuthorizedCompilation,
    FormulaExecutionIRV1,
    authorize_compilation,
    compile_ir,
    ir_hash,
)
from featuregen.materialize.physical_types import PhysicalType, resolve_physical_type
from featuregen.materialize.publish import PublisherSelection, PublishMechanism, select_publisher
from featuregen.materialize.render.project import (
    ProjectDatasets,
    RenderedNode,
    materialize_to,
    project_datasets,
    render_project,
)
from featuregen.materialize.request_store import (
    MaterializationRequestV1,
    RequestLifecycle,
    advance_lifecycle,
    read_request,
)
from featuregen.materialize.resolve import resolve_feature_inputs
from featuregen.materialize.spine import SpineSourceDeclarationV1
from featuregen.materialize.validation import (
    ValidationLevel,
    ValidationReportV1,
    ValidationStatus,
    read_validation_reports,
    record_validation_report,
    run_l0,
)

__all__ = [
    "FIRST_RUN_EVENT_SEQ",
    "ChainStage",
    "CompiledGroup",
    "L0Interpreter",
    "NodeAssembler",
    "NodeAssemblyInputs",
    "PublishStepMissing",
    "compile_feature_group",
]


#: The ``seq`` of a run's FIRST appended event, and the base of the only numbering convention this
#: plane has: **seq starts at 0 and increases by one per appended event, with a single writer per
#: run held by the request's lease** (Phase G §3.2).
#:
#: It is stated as a constant because the convention was written down nowhere and cannot be
#: recovered from the code that enforces it. ``append_run_event`` deliberately does not compute
#: ``max(seq) + 1`` — that read-modify-write is a race two appenders can both win — so ``seq`` is
#: the caller's, and migration 1044's ``materialization_run_event_ordered`` trigger refuses any
#: value that does not strictly extend the run.
#:
#: **A G-1 run is CLOSED at this seq.** It appends exactly one event and that event is terminal, so
#: 1044's trigger refuses every later insert on the same run — G-2's ``RUN_PREPARED`` /
#: ``RUN_SUBMITTED`` / … cannot continue from it and will need their own request and their own run.
#: The increment convention above governs a run that is still open, which in G-1 never happens.
FIRST_RUN_EVENT_SEQ = 0


class PublishStepMissing(RuntimeError):
    """``select_publisher`` proved a mechanism, and there is no publish step to honour it.

    Exported and named so a queue lane can classify it and fail the request with a reason, because
    its trigger is a legitimate operational act rather than a code change: ``record_attestation``
    is callable today, so ingesting one passing probe result for the target environment flips every
    run of this chain from the truthful ``CAPABILITY_UNPROVEN`` terminal into this state.

    It is a ``RuntimeError`` rather than a governed refusal because it is a statement about the
    PLATFORM, not about the feature: §14's closed vocabulary has no member for "the step that would
    have acted on this verdict has not been built", and inventing one would tell an operator the
    catalog refused their feature.
    """


class ChainStage(StrEnum):
    """The stages that can END a run, in the order the chain runs them.

    A caller reads :attr:`CompiledGroup.stopped_at` to learn WHICH stage stopped a run without
    parsing any string. Only stages that can produce a VERDICT are members: ``build_group_plan``,
    ``render_project`` and ``materialize_to`` have no refusal path at all — they raise, and a raise
    is a defect in this code rather than a verdict about a feature.

    A verdict is not always a :class:`~featuregen.materialize.codes.MaterializationRefused`.
    :attr:`VALIDATE_L0` produces a :class:`~featuregen.materialize.validation.ValidationReportV1`
    instead, which is why :attr:`CompiledGroup.refusal` is ``None`` when it is the stage that
    stopped a run and :attr:`CompiledGroup.validation_report` carries the evidence.
    """

    #: The resolution seam — the run has no terminal trace event, cannot be replayed, did not
    #: resolve, or the durable intent is not the one it was opened for.
    RESOLVE = "resolve"
    #: Gate 1 — §1.2's six checks against the immutable authoring trace.
    ADMIT = "admit"
    #: §2's per-feature compilation — the spine, the grain path, every body expression.
    COMPILE = "compile_ir"
    #: Gate 2 — the GROUP's complete physical read set (existence first, then read scope).
    AUTHORIZE = "authorize_compilation"
    #: §6's published column type for one feature.
    PHYSICAL_TYPE = "resolve_physical_type"
    #: §5 — classification and the one materialization contract the group publishes under.
    CONTRACT = "derive_group_contract"
    #: §10.1 — the logical name already resolves to a different contract or a different table.
    BIND = "bind_group"
    #: §3.3 — the declared population's table could not be resolved to a physical input.
    SPINE_INPUT = "derive_requirement"
    #: §10.3 — publication capability. In G-1 this is where every run that PROVED its build ends.
    PUBLISHER = "select_publisher"
    #: §11.2's L0 — the sealed project imports and constructs its pipeline in another interpreter.
    #: Last because it is the last thing a run does, and it is the one stage whose verdict is a
    #: report rather than a refusal. A run stops HERE when the build failed or was never proven.
    VALIDATE_L0 = "run_l0"


@dataclass(frozen=True, slots=True)
class L0Interpreter:
    """The interpreter §11.2's L0 imports the generated project in — the CALLER's configuration.

    It is a parameter of :func:`compile_feature_group` and not an environment read, because this
    module is a library: a chain that read ``FEATUREGEN_L0_PYTHON`` for itself would make every
    deployment's build proof depend on a variable no record names, and the trigger surface is what
    owns configuration. ``run_l0`` takes the same view — it has no default interpreter, "so nothing
    can silently validate the artifact against the validator's own environment"
    (``validation.py:580``), and this platform's own interpreter has neither ``kedro`` nor
    ``pyspark``.

    ``env`` is overlaid on the process environment for the probe, and the trap it exists for is
    stated in ``run_l0``: ``PYSPARK_PYTHON`` and ``PYSPARK_DRIVER_PYTHON`` must BOTH name the same
    interpreter, or Spark launches workers on the system Python.

    ``timeout_seconds`` has no default for the reason ``published_schema`` and ``spine_declaration``
    have none: a bound on how long a build proof may take is a decision, and inheriting one by
    omission is how a caller ends up not having made it. After it, the probe is *unreachable* —
    ``status="error"`` — not failing.
    """

    python_executable: str
    timeout_seconds: float
    env: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.python_executable, str) or not self.python_executable.strip():
            raise ValueError(
                f"the L0 interpreter is blank ({self.python_executable!r}): 'no interpreter is "
                f"configured' is stated by passing l0=None, which records an 'error' report; a "
                f"blank string is a third state that would be launched and fail as the artifact's "
                f"fault")
        if not isinstance(self.timeout_seconds, int | float) or \
                isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            raise ValueError(
                f"the L0 timeout is {self.timeout_seconds!r}: a non-positive bound cannot let any "
                f"probe finish, so every run would record 'the build was never proven'")


@dataclass(frozen=True, slots=True)
class NodeAssemblyInputs:
    """Everything the Kedro node assembly (Task 4) needs, and nothing it can decide for itself.

    ``datasets`` comes from :func:`~featuregen.materialize.render.project.project_datasets`, which
    is exposed for exactly this reason: a node that named its own datasets would be writing storage
    locations into source, and ``render_project`` re-derives the same names and refuses any wiring
    that does not close.

    ``admitted`` is keyed by feature name and is here because the compute renderers need the
    FORMULA, not only the IR: ``render_calculation_node``'s ``empty_window``/``null_input`` come
    from the formula's ``WindowPolicy``, which ``PitSpec`` deliberately excludes.
    """

    authorized: AuthorizedCompilation
    plan: FeatureGroupPlanV1
    contract: MaterializationContractV1
    admitted: Mapping[str, AdmittedFeature]
    spine_input: PhysicalInputRequirement
    datasets: ProjectDatasets


class NodeAssembler(Protocol):
    """The ONE seam this chain does not implement — Task 4 owns it.

    Nothing in ``src/`` assembles a Kedro node sequence today; the only reference wiring is a test
    fixture. It is a parameter rather than a call so that this module does not become the second
    place the wiring is described, and it takes a single frozen argument so Task 4 can be written
    against a signature that will not move.
    """

    def __call__(self, inputs: NodeAssemblyInputs, /) -> Sequence[RenderedNode]:
        ...


@dataclass(frozen=True, slots=True)
class CompiledGroup:
    """What one run of the chain produced — as far as it got, and what stopped it.

    The artifact fields are ``None`` for a stage the run never reached, which is how a caller reads
    how far it got; ``stopped_at`` is how it reads why it stopped, without parsing a string.
    ``refusal`` is the refusing stage's OWN object, never a copy and never re-typed.
    """

    request_id: str
    logical_group_name: str
    #: The stage whose verdict ended the run. ``None`` only on a replay (see :attr:`replayed`).
    stopped_at: ChainStage | None
    #: The refusing stage's own object — ``None`` when the stage that stopped the run does not
    #: produce one. :attr:`ChainStage.VALIDATE_L0` is the case: its verdict is
    #: :attr:`validation_report`, and a caller must read ``stopped_at`` rather than infer a
    #: successful run from ``refusal is None``.
    refusal: MaterializationRefused | None
    terminal_event: RunEventKind | None
    lifecycle_state: RequestLifecycle
    generation_id: str | None
    run_id: str | None
    materialization_contract_hash: str | None
    group_plan_hash: str | None
    generated_project_hash: str | None
    project_root: str | None
    #: §11.2's L0 verdict for this generation, present on EVERY run that sealed a project — the one
    #: field that says whether the terminal above rests on a build proof. ``None`` means no project
    #: was rendered, never "it built": a report is a claim about an artifact that exists.
    #:
    #: On a replay it is READ BACK from ``pipeline_validation_report`` rather than left ``None``.
    #: Unlike :attr:`stopped_at`, it is durable, so reporting it is a reading and not an invention.
    validation_report: ValidationReportV1 | None = None
    #: True when the request was already terminal and this call did no work. The durable record
    #: does not say which stage stopped a run or carry the refusing object, so both are ``None``
    #: rather than inferred — a replay reports what was recorded, never a reconstruction of it.
    replayed: bool = False


def compile_feature_group(
    conn: DbConn,
    *,
    request_id: str,
    work_item_ids: Sequence[str],
    inventory: ClusterInventoryV1,
    spine_declaration: SpineSourceDeclarationV1 | None,
    cadence: CadenceDecl,
    availability_promise: AvailabilityPromiseV1,
    mechanism: PublishMechanism,
    published_schema: Sequence[str] | None,
    assemble_nodes: NodeAssembler,
    project_root: str | os.PathLike[str],
    l0: L0Interpreter | None,
    clock: Callable[[], str],
    contract_overrides: ContractOverrides | None = None,
) -> CompiledGroup:
    """Compile the group ``request_id`` names into a sealed project on disk, or stop and say where.

    The request row (migration 1053) is the durable anchor: it names the logical group, records the
    roles the requester held, and — once a generation exists — carries the ``generation_id`` and
    ``run_id`` this call derives. It must be ``accepted`` (a worker holds its lease) on entry;
    a terminal request is REPLAYED rather than re-run, which is what keeps a re-delivered queue job
    from minting a second generation or a second terminal event.

    **Gate 2's roles are the request's own snapshot**, read here rather than passed. They are not a
    parameter for the same reason the group name is not: a second source for a governed input is a
    second answer, and the only legal value a caller could supply is the one already recorded.
    ``request_store.py:182-184`` settles which value that is — a run is judged against the scope its
    requester *actually held*, not against whatever anyone holds by the time the work is claimed —
    so there is no legitimate call that narrows or widens it either.

    Args:
        request_id: the durable request. Its ``logical_group_name`` is the group's identity and its
            ``authorized_roles`` are Gate 2's — this function takes neither as a parameter.
        work_item_ids: the group's members, as ``recipe_formula_shadow_work_item`` ids. Nothing maps
            a logical group to its members yet, so the caller supplies them (Task 2's §15.2).
        inventory: the captured cluster facts (§0). ``environment_id`` and ``engine_versions`` are
            read off it, so the artifact cannot be rendered for one environment and pinned to
            another's versions.
        spine_declaration: §4's declared population. Keyword-only with no default, mirroring
            ``compile_ir``: "no spine" must be stated, never inherited by omission.
        cadence, availability_promise: §5's two DECLARATIONS. The platform derives neither.
        mechanism: the publish mechanism §10.3 is asked about. Only ``VERSIONED_POINTER`` can be
            rendered today (``render/publish.py:62``).
        published_schema: the live column list of the currently published table, ``None`` for "no
            table yet". No default, exactly as ``select_publisher`` has none — omitting it would
            silently inherit the lenient answer. G-1 has no metastore seam, so the caller states it.
        assemble_nodes: Task 4's seam (:class:`NodeAssembler`).
        project_root: the directory the sealed project is written UNDER. The project itself lands in
            ``project_root/<generation_id>``, because ``materialize_to`` refuses a non-empty
            directory and two generations of one group must not share a tree.
        l0: the interpreter §11.2's L0 imports the sealed project in, or ``None`` for "this
            deployment has none". No default: whether a run's build is proved is not a thing to
            inherit by omission. ``None`` does not skip the check — it records the same
            ``status="error"`` report an interpreter that could not be launched would, and the run
            terminates ``RUN_FAILED`` (see the module docstring for the argument).
        clock: an offset-aware ISO 8601 instant, matching ``run_l0``/``run_l1``'s ``clock``. The
            plane mints no timestamps; every ``created_at`` and ``occurred_at`` here is this one.
        contract_overrides: §5's declared tightenings, if any.

    Returns:
        A :class:`CompiledGroup`. In G-1 a run that gets all the way through stops at
        :attr:`ChainStage.PUBLISHER` with ``CAPABILITY_UNPROVEN``, a sealed project on disk and a
        PASSED L0 report; one whose build failed or was never proven stops at
        :attr:`ChainStage.VALIDATE_L0` with the report that says which.

    Raises:
        ValueError: the request does not exist or is not ``accepted`` — and every ``ValueError`` the
            stages themselves raise, which are calls assembled wrongly rather than governed
            verdicts.
        featuregen.materialize.admission.FeatureNamePlanError: two members normalize to one Hive
            identifier, or a name cannot be one at all. A plan error, not a §14 code.
        PublishStepMissing: ``select_publisher`` RETURNED a selection and there is no publish step
            to honour it (plan §3.5).
    """
    request = _claim(conn, request_id)
    if request.lifecycle_state.is_terminal():
        return _replayed(conn, request)

    roles = request.authorized_roles
    stop = _Stop(conn, request)
    resolved = _governed(stop, ChainStage.RESOLVE,
                         lambda: resolve_feature_inputs(conn, work_item_ids=work_item_ids))
    if isinstance(resolved, CompiledGroup):
        return resolved

    admitted = _governed(stop, ChainStage.ADMIT,
                         lambda: admit_artifacts(conn, [item.input for item in resolved]))
    if isinstance(admitted, CompiledGroup):
        return admitted
    by_name = {feature.feature_name: feature for feature in admitted}

    irs: list[FormulaExecutionIRV1] = []
    for feature in admitted:
        compiled = compile_ir(conn, feature, roles=roles, spine_decl=spine_declaration,
                              inventory=inventory)
        if isinstance(compiled, MaterializationRefused):
            return stop.refused(ChainStage.COMPILE, compiled)
        irs.append(compiled)

    authorized = authorize_compilation(conn, irs, irs[0].spine, roles=roles)
    if isinstance(authorized, MaterializationRefused):
        return stop.refused(ChainStage.AUTHORIZE, authorized)

    planned: list[PlannedFeature] = []
    for ir in authorized.irs:
        physical = resolve_physical_type(
            by_name[ir.feature_name].formula,
            operand_types={e.expr_path: e.operand_type for e in ir.expressions})
        if isinstance(physical, MaterializationRefused):
            return stop.refused(ChainStage.PHYSICAL_TYPE, physical)
        planned.append(_planned(ir, physical))

    group = derive_group_contract(conn, authorized, cadence=cadence,
                                  availability_promise=availability_promise,
                                  overrides=contract_overrides)
    if isinstance(group, MaterializationRefused):
        return stop.refused(ChainStage.CONTRACT, group)

    plan = build_group_plan(group, planned, logical_group_name=request.logical_group_name)

    # §10.1 BEFORE rendering: `GROUP_BINDING_CONFLICT` is the one refusal that says "do not render
    # for this name at all", and a project rendered for a name already bound to another contract is
    # an artifact that would publish over a table its reader could not tell had changed meaning.
    existing = read_group_binding(conn, plan.logical_group_name)
    binding = bind_group(plan, binding_id=_binding_id(plan.logical_group_name), existing=existing)
    if isinstance(binding, MaterializationRefused):
        return stop.refused(ChainStage.BIND, binding)

    spine_input = derive_requirement(conn, inventory,
                                     table_ref=authorized.spine.source_table_ref)
    if isinstance(spine_input, MaterializationRefused):
        return stop.refused(ChainStage.SPINE_INPUT, spine_input)

    # §10.3 BEFORE rendering: the selection is rendered INTO `conf/base/catalog.yml`, so it cannot
    # be a post-render step. Its refusal does NOT stop the chain — the compilation is sound and the
    # project is worth having; what is unproven is publication, and `render_project` with no
    # selection renders the fail-closed entry that cannot publish over anything.
    selection = select_publisher(conn, environment_id=inventory.environment_id,
                                 engine_versions=inventory.engine_versions, mechanism=mechanism,
                                 group_plan=plan, published_schema=published_schema)
    if isinstance(selection, PublisherSelection):
        raise PublishStepMissing(
            f"select_publisher proved {mechanism.value} for {inventory.environment_id!r} "
            f"(attestation {selection.capability_attestation_id}), and this chain has no publish "
            f"step to honour it: the metastore write, the active-revision record and the pointer "
            f"swap are all G-3 (plan §3.5, DEFERRED-WORK A.26). Rendering a publishing artifact "
            f"nothing will ever publish — or recording a terminal this run did not reach — would "
            f"both be claims the append-only plane could never retract")

    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    sealed = render_project(
        authorized, plan, environment_id=inventory.environment_id,
        engine_versions=inventory.engine_versions, spine_input=spine_input,
        nodes=tuple(assemble_nodes(NodeAssemblyInputs(
            authorized=authorized, plan=plan, contract=group.contract, admitted=by_name,
            spine_input=spine_input, datasets=datasets))))

    return _commit(conn, request, plan=plan, contract=group.contract, binding=binding,
                   existing=existing, project=sealed, refusal=selection, project_root=project_root,
                   environment_id=inventory.environment_id, l0=l0, clock=clock)


# ── the durable record ───────────────────────────────────────────────────────────────────────────


def _commit(
    conn: DbConn,
    request: MaterializationRequestV1,
    *,
    plan: FeatureGroupPlanV1,
    contract: MaterializationContractV1,
    binding: GroupContractBinding,
    existing: GroupContractBinding | None,
    project: SealedProject,
    refusal: MaterializationRefused,
    project_root: str | os.PathLike[str],
    environment_id: str,
    l0: L0Interpreter | None,
    clock: Callable[[], str],
) -> CompiledGroup:
    """Record the generation, the §10.1 records, §3.6's compiled artifact, L0's verdict and the
    terminal that verdict earns, and write the tree — ALL OR NOTHING.

    THE TRANSACTION IS THE POINT, and ordering alone was not enough. The runtime this chain is
    driven from is AUTOCOMMIT by contract (``runtime/worker.py:638``: "Autocommit is required: each
    stage owns its own ``with conn.transaction()``"; ``runtime/dispatch.py:79`` sets it on the
    handler connection), so without an explicit transaction every statement below commits on its
    own. A tree write that then failed — a full disk, a permission, or ``materialize_to``'s
    non-empty-directory refusal — would leave the plane holding a committed request, a generation
    row carrying a ``generated_project_hash``, and an **unretractable terminal event** for a project
    that was never written; migration 1044's one-terminal trigger means nothing can ever supersede
    it. A mid-block failure (the ``group_binding`` unique violation, say) would leave a committed
    generation and a ``running`` request with no terminal at all.

    So the whole block — including the tree write — is one transaction. Opened unconditionally
    rather than only on autocommit: nested inside a caller's transaction psycopg makes it a
    SAVEPOINT, which is exactly the semantics wanted here (roll back this chain's writes, leave the
    caller's alone) and is what makes the guarantee testable at all.

    ORDER still matters inside it, and L0 changed it. The generation row comes first because every
    other plane record takes a foreign key to it — ``materialization_compiled_artifact`` and
    ``pipeline_validation_report`` included. The tree used to be written last; it now has to be
    written before the terminal, because L0 validates a project ON DISK and the terminal is chosen
    from its verdict. What stays last is the ``os.replace`` that makes the tree VISIBLE at the
    project's own path: L0 runs over the staging sibling, so a failure anywhere in this block —
    including at the validation write — still leaves nothing at ``root`` for a rollback to be
    unable to clean up.

    ``RUNNING`` is a stepping stone here, and its documented meaning ("a run was prepared") is not
    yet true in G-1 — ``prepare_run`` is G-2. It is passed through because ``accepted → committed``
    is not a legal edge (``request_store.LEGAL_LIFECYCLE_TRANSITIONS``) and because it is the only
    state that stamps ``generation_id``/``run_id`` onto the request. Inside one transaction no
    reader ever observes it.

    **A FAILING L0 IS NOT A ROLLBACK.** The generation, the artifact, the report, the ``RUN_FAILED``
    terminal and the tree are all committed, and the request lands ``failed`` rather than
    ``committed``. Rolling back would destroy the only evidence of *why* the build failed, and the
    project is what an operator opens to see it; the transaction exists so that a run is recorded
    ONCE and WHOLE, not so that bad news is discarded. Only a raised exception rolls back.

    The terminal event is ``PUBLICATION_REFUSED`` carrying ``select_publisher``'s own code when the
    build was proved, and ``RUN_FAILED`` when it was not. See the module docstring for what each
    means, why ``GATES_FAILED`` is not the second one, and why ``PUBLISHED`` can never be either.
    """
    generation_id = _generation_id(request.request_id)
    run_id = _run_id(request.request_id)
    created_at = clock()
    root = pathlib.Path(project_root) / generation_id
    staging = root.parent / f".{root.name}.partial"

    shutil.rmtree(staging, ignore_errors=True)
    try:
        with conn.transaction():
            record_generation(conn, MaterializationGeneration(
                generation_id=generation_id,
                logical_group_name=plan.logical_group_name,
                materialization_contract_hash=plan.materialization_contract_hash,
                group_plan_hash=group_plan_hash(plan),
                generated_project_hash=project.identity.generated_project_hash,
                created_at=created_at))
            advance_lifecycle(conn, request_id=request.request_id,
                              to_state=RequestLifecycle.RUNNING, generation_id=generation_id,
                              run_id=run_id)

            # `bind_group` RETURNS the existing binding unchanged when the contract still agrees,
            # and `record_group_binding` is a UniqueViolation on a second write for one logical
            # name — so the binding is written exactly when it was minted here.
            if existing is None:
                record_group_binding(conn, binding)
            record_plan_revision(conn, plan_revision(plan, binding, generation_id=generation_id,
                                                     created_at=created_at))
            # §3.6: the BODIES behind two of the generation's three hashes — the packing list §9
            # gates against and the contract the group publishes under. (The third, the project
            # hash, names the tree, which is written below and kept whole.) Without this row a crash
            # after render leaves a generation nobody can audit: three digests of objects nothing
            # kept.
            record_compiled_artifact(conn, generation_id=generation_id, group_plan=plan,
                                     contract=contract)

            materialize_to(project, staging)
            report = _prove_the_build(staging, project=project, l0=l0,
                                      generation_id=generation_id, environment_id=environment_id,
                                      report_id=_report_id(request.request_id), clock=clock)
            record_validation_report(conn, report)
            built = report.status is ValidationStatus.PASSED

            append_run_event(conn, MaterializationRunEvent(
                run_id=run_id, seq=FIRST_RUN_EVENT_SEQ, generation_id=generation_id,
                event_kind=(RunEventKind.PUBLICATION_REFUSED if built
                            else RunEventKind.RUN_FAILED),
                occurred_at=created_at,
                detail=(f"{refusal.code.value}: {refusal.detail}" if built
                        else _unproven_detail(report, configured=l0 is not None))))
            moved = advance_lifecycle(
                conn, request_id=request.request_id,
                to_state=RequestLifecycle.COMMITTED if built else RequestLifecycle.FAILED)
            _publish_tree(staging, root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return CompiledGroup(
        request_id=request.request_id, logical_group_name=request.logical_group_name,
        stopped_at=ChainStage.PUBLISHER if built else ChainStage.VALIDATE_L0,
        refusal=refusal if built else None,
        terminal_event=(RunEventKind.PUBLICATION_REFUSED if built else RunEventKind.RUN_FAILED),
        lifecycle_state=moved.lifecycle_state,
        generation_id=generation_id, run_id=run_id,
        materialization_contract_hash=plan.materialization_contract_hash,
        group_plan_hash=group_plan_hash(plan),
        generated_project_hash=project.identity.generated_project_hash, project_root=str(root),
        validation_report=report)


def _prove_the_build(root: pathlib.Path, *, project: SealedProject, l0: L0Interpreter | None,
                     generation_id: str, environment_id: str, report_id: str,
                     clock: Callable[[], str]) -> ValidationReportV1:
    """§11.2's L0 over the materialized project — or the honest non-answer when it cannot be run.

    ``l0 is None`` says this deployment configures no interpreter. That is not a reason to skip the
    check and it is not a fourth verdict: it is the SAME thing ``run_l0`` reports when the
    interpreter it was given could not be launched, timed out, or printed no verdict —
    ``status="error"``, **zero** findings, "the validation could not run". Constructing it here
    rather than launching something is the whole of the difference; inventing an interpreter (this
    process's own, say) would import a project that needs ``kedro`` into one that has none and
    report ``PROJECT_DOES_NOT_BUILD``, blaming the renderer for the environment.

    The report is built off the SEALED identity rather than off the tree, because the two hashes it
    carries are claims about the artifact this generation names, and a directory that failed to
    hash to its lock is a finding rather than a new identity.
    """
    if l0 is None:
        started_at = clock()
        return ValidationReportV1(
            report_id=report_id, generation_id=generation_id, run_id=None,
            generated_project_hash=project.identity.generated_project_hash,
            group_plan_hash=project.identity.compilation.group_plan_hash,
            level=ValidationLevel.L0, environment_id=environment_id,
            status=ValidationStatus.ERROR, started_at=started_at, finished_at=clock(),
            findings=())
    return run_l0(root, generation_id=generation_id, environment_id=environment_id,
                  report_id=report_id, python_executable=l0.python_executable, clock=clock,
                  env=l0.env, timeout_seconds=l0.timeout_seconds)


def _unproven_detail(report: ValidationReportV1, *, configured: bool) -> str:
    """The ``RUN_FAILED`` event's operator-facing text: what L0 saw, and WHO fixes it.

    §14's rule for a detail — counts, types, codes and locations, never data values — and §11.2's
    rule that a finding's CLASS is what routes a decision. A reader of the append-only stream gets
    the codes, their classes and the report id to open; the findings themselves stay in
    ``pipeline_validation_report``, where ``read_validation_reports`` returns them intact.

    ``error`` prints no findings because it HAS none, and says which of the two silences it is:
    a deployment that configured no interpreter is an operator's act, while an interpreter that did
    not answer is the environment's — different people fix them.
    """
    if report.status is ValidationStatus.ERROR:
        because = ("no L0 interpreter is configured" if not configured else
                   "the configured interpreter did not answer (launch failure, timeout, or no "
                   "verdict printed)")
        return (f"L0 could not run, so this project's build is UNPROVEN ({report.report_id}): "
                f"{because}")
    seen = "; ".join(f"{finding.code.value}({finding.classification.value}) x{finding.count}"
                     for finding in report.findings)
    return f"L0 failed, so this project does not build ({report.report_id}): {seen}"


def _publish_tree(staging: pathlib.Path, root: pathlib.Path) -> None:
    """Make the staged tree VISIBLE at the project's own path — complete, or not at all.

    A filesystem takes no part in the transaction above, so a half-written tree is the one piece of
    state a rollback cannot remove — and it would be permanent damage rather than litter: the
    generation id is a pure function of the request id, so ``root`` never changes for this request,
    and ``materialize_to`` refuses a non-empty directory (``render/project.py:1294``). A partial
    tree would poison every re-drive of that request forever.

    So the project is written to a sibling (and validated there) and moved into place with one
    ``os.replace``, which is atomic on a POSIX filesystem and — because ``rename`` onto a non-empty
    directory fails — cannot silently overwrite an earlier generation's tree either. ``_commit``'s
    own ``except`` removes the partial on any failure.
    """
    root.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, root)


class _Stop:
    """How a governed refusal becomes a recorded, returned outcome — in ONE place.

    A refusal before the project is sealed has nowhere in the control plane to go (see the module
    docstring), so what it records is the request's own terminal state. Recording it here rather
    than at each stage is what makes "every governed refusal is recorded and returned, never
    swallowed" a property of the chain rather than of nine call sites.
    """

    __slots__ = ("conn", "request")

    def __init__(self, conn: DbConn, request: MaterializationRequestV1) -> None:
        self.conn = conn
        self.request = request

    def refused(self, stage: ChainStage, refusal: MaterializationRefused) -> CompiledGroup:
        moved = advance_lifecycle(self.conn, request_id=self.request.request_id,
                                  to_state=RequestLifecycle.FAILED)
        return CompiledGroup(
            request_id=self.request.request_id,
            logical_group_name=self.request.logical_group_name,
            stopped_at=stage, refusal=refusal, terminal_event=None,
            lifecycle_state=moved.lifecycle_state, generation_id=None, run_id=None,
            materialization_contract_hash=None, group_plan_hash=None,
            generated_project_hash=None, project_root=None)


_Produced = TypeVar("_Produced")


def _governed(stop: _Stop, stage: ChainStage,
              call: Callable[[], _Produced]) -> _Produced | CompiledGroup:
    """Run a stage that RAISES its refusal, and turn that raise into a recorded outcome.

    Only two stages do — ``resolve_feature_inputs`` and ``admit_artifacts``, both of which refuse a
    whole batch rather than a member. Everything else in the chain RETURNS its verdict and is
    handled with an ``isinstance`` check at the call site, because pretending the two contracts are
    one would mean a ``try`` wide enough to catch a refusal raised by something else entirely.

    ``MaterializationRefused`` alone is caught: ``FeatureNamePlanError`` and ``ValueError`` travel
    straight through, which is the whole distinction §14 exists to draw.
    """
    try:
        return call()
    except MaterializationRefused as refusal:
        return stop.refused(stage, refusal)


def _replayed(conn: DbConn, request: MaterializationRequestV1) -> CompiledGroup:
    """A terminal request, reported from what was RECORDED — no work, no second generation.

    Idempotency is structural rather than defensive: the ids are derived from the request id, the
    request row stamps ``generation_id``/``run_id`` write-once, and a terminal request cannot be
    advanced again. Together those mean a re-delivered queue job cannot reach the append at all —
    which matters because a second terminal event is a ``UniqueViolation`` on a plane whose
    append-only triggers leave no repair path.

    ``stopped_at`` and ``refusal`` are ``None`` because neither is durable. Inferring a stage from
    the terminal event would be an invention about a decision nothing recorded, and
    :attr:`CompiledGroup.replayed` is how a caller tells this answer from a fresh one.

    The L0 report IS durable, so it is read back rather than dropped: a re-delivered queue job that
    lost the build proof would leave a caller with a terminal and no way to see what stands behind
    it. That is a reading of ``pipeline_validation_report``, not a reconstruction — which is exactly
    why the two fields above stay ``None`` and this one does not.
    """
    events = read_run_events(conn, request.run_id) if request.run_id else ()
    return CompiledGroup(
        request_id=request.request_id, logical_group_name=request.logical_group_name,
        stopped_at=None, refusal=None,
        terminal_event=events[-1].event_kind if events and events[-1].is_terminal() else None,
        lifecycle_state=request.lifecycle_state, generation_id=request.generation_id,
        run_id=request.run_id, materialization_contract_hash=None, group_plan_hash=None,
        generated_project_hash=None, project_root=None,
        validation_report=_recorded_l0(conn, request.generation_id), replayed=True)


def _recorded_l0(conn: DbConn, generation_id: str | None) -> ValidationReportV1 | None:
    """The NEWEST recorded L0 report for a generation, or ``None`` when there is not one.

    ``read_validation_reports`` is the package's reader and returns oldest-first, so the last L0 row
    is the newest — the same "newest report of each level" rule
    :func:`~featuregen.materialize.validation.may_regenerate_for` applies, and for the same reason:
    a re-validation supersedes the verdict it re-checked. G-1 records exactly one, and a request
    that never reached a generation has none to read.
    """
    if generation_id is None:
        return None
    l0_reports = [report for report in read_validation_reports(conn, generation_id=generation_id)
                  if report.level is ValidationLevel.L0]
    return l0_reports[-1] if l0_reports else None


def _claim(conn: DbConn, request_id: str) -> MaterializationRequestV1:
    """The request this call is for, proved to be one this caller may compile.

    ``accepted`` is required rather than granted here: acceptance is the CLAIM on the work and it
    carries the lease, which is the single-writer guarantee an append-only event stream has no
    other way to get. Granting it here would let two workers hold one run.
    """
    request = read_request(conn, request_id=request_id)
    if request is None:
        raise ValueError(
            f"no materialization request {request_id!r}: the request row is minted before any work "
            f"begins, so a missing one means this call is compiling something nobody asked for")
    if request.lifecycle_state.is_terminal():
        return request
    if request.lifecycle_state is not RequestLifecycle.ACCEPTED:
        raise ValueError(
            f"materialization request {request_id!r} is "
            f"{request.lifecycle_state.value!r}, not 'accepted': the lease granted by acceptance is "
            f"what makes this the single writer for the run, and compiling without one would put "
            f"two writers on an append-only event stream that has no repair path")
    return request


# ── derived values ───────────────────────────────────────────────────────────────────────────────


def _planned(ir: FormulaExecutionIRV1, physical: PhysicalType) -> PlannedFeature:
    """One row of the packing list. ``ir_hash`` is re-derived rather than carried: §9's gates
    compare every staging manifest against the PLAN's value, and ``build_compilation_identity`` is
    the one place that checks the plan's value against the IR it names."""
    return PlannedFeature(column_name=ir.feature_name, ir_hash=ir_hash(ir),
                          physical_type=physical)


def _derived_id(prefix: str, *, purpose: str, material: str) -> str:
    """A stable id, DERIVED so that re-entry cannot mint a second one.

    A random id would make every re-entry a new generation, which would make the request row's
    write-once ``generation_id`` unable to be the thing that stops a second compilation. The purpose
    is inside the digest so two ids derived from one request cannot collide.
    """
    digest = hashlib.sha256(f"{purpose}:{material}".encode()).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _generation_id(request_id: str) -> str:
    return _derived_id("gen", purpose="materialization_generation", material=request_id)


def _run_id(request_id: str) -> str:
    return _derived_id("mrun", purpose="materialization_run", material=request_id)


def _report_id(request_id: str) -> str:
    """``run_l0`` mints no ids (``validation.py:576``), and ``pipeline_validation_report.report_id``
    is a primary key — so a re-entry that reached the write with a random id would append a SECOND
    verdict for one generation instead of colliding, which on an append-only table with no repair
    path is the difference between a loud failure and two build proofs nobody can order."""
    return _derived_id("l0rep", purpose="l0_validation_report", material=request_id)


def _binding_id(logical_group_name: str) -> str:
    """Derived from the GROUP, not the request: §10.1 writes one binding per logical name, ever,
    and an id derived per request would be a second binding_id for a row that already exists."""
    return _derived_id("gbind", purpose="group_binding", material=logical_group_name)
