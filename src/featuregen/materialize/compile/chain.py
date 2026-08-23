"""Spec A §2's ORCHESTRATOR — the chain that turns a governed feature into a sealed project.

The module ``compile/__init__.py`` reserved and did not build ("§2's orchestrator is not built, and
inventing a shell for it would be a second place the chain is described"), recorded as DEFERRED-WORK
A.24. It lands in this SIBLING module rather than in the package's ``__init__`` because
``identity.py:52`` imports that package: a chain there would close ``compile → identity → compile``,
and the failure would surface as an ``ImportError`` in whichever module happened to be imported
first. This file may import ``identity`` freely; ``compile/__init__.py`` stays free of imports.

WHAT THIS CHAIN COVERS. Phase G §4 stages the programme. G-1 is stages 1 through SEAL plus §11.2's
**L0**; **G-2 — ``prepare_run`` → ``run_l1`` → ``submit`` — is composed here as of task D1**, and
**G-3 — the publish step — as of task D3**. Both run only on a build that PASSED L0 under a PROVEN
publication capability. There is no longer any state in which a completed run has nowhere to go:
``PublishStepMissing`` and the writer-side guard that kept an operator from reaching it are both
deleted, because their entire cause was the absence this task closed.

WHY G-2 CANNOT RUN WITHOUT A PROVEN CAPABILITY, which is not an ordering preference.
``prepare_run`` requires a ``capability_attestation_id`` and ``sandbox_execution_hash`` refuses a
blank one ("a defaulted ``capability_attestation_id`` would let an execution be identified without
naming the attestation §10.3 requires", ``identity.py:494``). §11.1 therefore identifies an
execution PARTLY BY the attestation it will publish under, so a run in an environment whose
publication capability is unproven has no execution identity — there is nothing to prepare, and
that is an outcome (``PUBLICATION_REFUSED``, the terminal below) rather than a stage anyone skipped.

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

**WHEN THIS MODULE MAY APPEND** ``RunEventKind.PUBLISHED`` — and the answer is a conjunction, not a
branch. Only when the build PASSED L0, publication capability was PROVEN for this exact environment
at these exact engine versions, the run was prepared, L1 passed against the live metastore, the
pipeline ran to completion, AND the pointer was recorded and swapped. The plane is append-only, with
a partial unique index admitting one terminal event per run and migration 1044's BEFORE-INSERT
ordering trigger refusing anything after it — so a publication claim, once written, can never be
retracted by any code path that exists, which is exactly why it is the last thing this module does
and why every one of those seven conditions is a stage with its own recorded outcome.
``test_the_chain_appends_PUBLISHED_only_after_a_proven_selection_and_a_passed_gate`` is the guard
that replaced the AST test which used to forbid the kind outright.

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

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # ANNOTATION-ONLY, and deliberately not runtime imports. This module sits at the centre
    # of the compile graph and both of these reach back into it transitively; importing them
    # at runtime closes a cycle, and a cycle first triggered from a REQUEST thread deadlocks
    # on the import lock rather than raising — the main thread waits on a lock forever and
    # the suite simply stops. `from __future__ import annotations` keeps the field types as
    # strings, so `NodeAssemblyInputs` needs neither class at runtime.
    from featuregen.materialize.admission_v2 import AdmittedFeatureV2
    from featuregen.materialize.boundary_v2 import AuthorizedCompilationV2

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
from featuregen.materialize.codes import MaterializationRefused, ValidationFindingCode
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
from featuregen.materialize.publish import (
    ActiveRevision,
    PublicationSwap,
    PublisherSelection,
    PublishMechanism,
    publish_generation,
    select_publisher,
)
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
from featuregen.materialize.runprep import (
    prepare_run,
    run_input_requests,
    spine_input_request,
)
from featuregen.materialize.spine import SpineSourceDeclarationV1
from featuregen.materialize.submit import PipelineSubmitter, SubmissionOutcome
from featuregen.materialize.validation import (
    MetastoreMetadata,
    ReadScopeDeclaration,
    ValidationLevel,
    ValidationReportV1,
    ValidationStatus,
    read_validation_reports,
    record_validation_report,
    run_l0,
    run_l1,
)
from featuregen.overlay.upload.bridge_realization import ExecutionTier

__all__ = [
    "FIRST_RUN_EVENT_SEQ",
    "ChainStage",
    "CompiledGroup",
    "L0Interpreter",
    "NodeAssembler",
    "NodeAssemblyInputs",
    "RunExecution",
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

#: The terminals that leave the request ``committed`` rather than ``failed`` — the two whose
#: evidence is ON the plane. Read as a set rather than spelled at the fold, so a third terminal
#: added without deciding which side it falls on fails the test that names this membership.
#: ``PUBLICATION_REFUSED`` is here for the reason §0.0 states: it is G-1's SUCCESS terminal.
_COMMITTING_TERMINALS = frozenset({RunEventKind.PUBLICATION_REFUSED, RunEventKind.PUBLISHED})


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
    #: The one stage whose verdict is a report rather than a refusal. A run stops HERE when the
    #: build failed or was never proven, and the three G-2 stages below run only when it PASSED.
    VALIDATE_L0 = "run_l0"
    #: §11.1's ``prepare_run`` — the run's inputs resolved to exact partitions and its execution
    #: identified. G-2. It stops a run when the deployment configures no execution seam at all, or
    #: when resolution returns a governed refusal.
    PREPARE_RUN = "prepare_run"
    #: §11.2's L1 — metastore METADATA over every IR, every expression, the spine and the exact
    #: partitions preparation resolved. G-2.
    VALIDATE_L1 = "run_l1"
    #: §11.1's submission — the rendered project run with EXACTLY the prepared parameters. G-2.
    SUBMIT = "submit"
    #: §3.5's publish step — the metastore write, migration 1055's active-revision record and the
    #: reader-visible pointer swap. G-3, and the ONE stage whose verdict is neither a refusal nor a
    #: report: a run that reaches it and returns has PUBLISHED, and the plane can never retract it.
    PUBLISH = "publish_generation"


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
class RunExecution:
    """Everything G-2 needs that this library cannot decide — the CALLER's configuration.

    :class:`L0Interpreter`'s argument, one level out: a chain that read the metastore's address or
    the run's business date for itself would make every execution depend on values no record names.
    All four are stated together in ONE object for a reason — each is useless without the others, so
    four independently-nullable parameters would have fifteen half-configured shapes, of which
    exactly zero can run.

    **``execution=None`` is a POSTURE, not a missing setting**, and it is ``l0=None``'s rule applied
    one stage later: it says *this deployment cannot execute a run*, the chain records that as the
    run's outcome (``RUN_FAILED``, stopped at :attr:`ChainStage.PREPARE_RUN`) and never as a skipped
    stage. A skipped G-2 that left a publication terminal behind would be indistinguishable from one
    that ran, on a plane that can never retract it.

    ``metastore`` is a :class:`~featuregen.materialize.validation.MetastoreMetadata`, which extends
    ``MetastorePartitions``: preparation resolves partitions and L1 checks the SAME ones still
    exist, so one adapter answers both and there is nowhere for two to disagree. **No implementation
    of it exists in ``src/`` yet** — capture has its own ``MetastoreInventoryAdapter``
    (``inventory.py:679``), which answers different questions — so every deployment configured from
    the environment today passes ``None`` and every run is honestly unprepared.

    ``business_dt`` is the run's declaration, not the deployment's: it rides the queue payload
    (``MaterializationJobV1.business_dt``) and reaches this object at the lane boundary.
    ``staging_base`` is the deployment's, exactly as ``project_root`` is — ``staging_root_for``
    derives the per-generation path under it so no caller can forget the scoping.

    ``read_scope`` is the deployment's declaration about its engine's AUTHORIZATION MODEL, and it
    is the one field here with a default — deliberately, and the opposite way round from the other
    five. Those five are useless without each other, so ``None`` for any of them means no execution
    at all; this one has a safe value that must be what a caller who says nothing gets, because
    every existing construction of this object predates it and none of them accepted anything.
    :attr:`~featuregen.materialize.validation.ReadScopeDeclaration.ENGINE_ANSWERS` is that value: L1
    fails closed on an unanswerable read scope exactly as it did before the field existed.
    """

    metastore: MetastoreMetadata
    submitter: PipelineSubmitter
    swap: PublicationSwap
    business_dt: str
    staging_base: str
    read_scope: ReadScopeDeclaration = ReadScopeDeclaration.ENGINE_ANSWERS

    def __post_init__(self) -> None:
        if not isinstance(self.business_dt, str) or not self.business_dt.strip():
            raise ValueError(
                f"the run's business_dt is blank ({self.business_dt!r}): every partition the run "
                f"reads is resolved from it, and 'this deployment cannot execute a run' is stated "
                f"by passing execution=None, not by naming a date nothing resolves")
        if not isinstance(self.staging_base, str) or not self.staging_base.strip():
            raise ValueError(
                f"the staging base is blank ({self.staging_base!r}): §9's staging root is derived "
                f"under it per generation, and a blank base would stage every generation of every "
                f"group at one path")
        if not isinstance(self.read_scope, ReadScopeDeclaration):
            raise TypeError(
                f"read_scope must be a ReadScopeDeclaration, got "
                f"{type(self.read_scope).__name__}: a truthy string here would read as a "
                f"declaration nobody made, and this is the field that decides whether an "
                f"unverified read scope stops a run")


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

    #: EITHER language's token and admitted artifacts. Widened rather than duplicated because the
    #: node sequence for a group is ONE concept — a spine, a projection per expression, a
    #: calculation per feature, an assembly and a gate — and two copies of it would drift. What
    #: makes the widening honest rather than a `| V2` nobody checked is that `assemble_nodes` reads
    #: only what both carry: `.irs` (both tokens expose it under that name, deliberately),
    #: `.spine`, and `ExpressionExecutionIR`, which is one shared type. The single genuine
    #: difference — where the FORMULA lives — is crossed in exactly one function,
    #: `wiring._declared_windows`.
    authorized: AuthorizedCompilation | AuthorizedCompilationV2
    plan: FeatureGroupPlanV1
    contract: MaterializationContractV1
    admitted: Mapping[str, AdmittedFeature | AdmittedFeatureV2]
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
    #: The PUBLISHER's verdict, present whenever selection refused — INDEPENDENTLY of where this run
    #: stopped. ``refusal`` names what stopped the run; this names what publication decided, and a
    #: run that stops at L0 still has a publication verdict worth reading. Before this field the
    #: two were fused into one, so a refused publisher plus a failed build reported neither.
    publication_refusal: MaterializationRefused | None
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
    #: §11.2's L1 verdict, present on every run that got past :attr:`ChainStage.PREPARE_RUN`.
    #: ``None`` means the run was never prepared — never "L1 passed".
    l1_report: ValidationReportV1 | None = None
    #: What §11.1's submission did, or ``None`` when nothing was submitted. ``completed=False`` with
    #: ``returncode=None`` is a submission that never STARTED, which is a different fact from one
    #: that ran and failed and routes to different people (``submit.py:60``).
    submission: SubmissionOutcome | None = None
    #: §7's identity of the execution that was prepared, ``None`` when none was. It is the value
    #: that ties a plane record to the parameters the pipeline was actually given.
    sandbox_execution_hash: str | None = None
    #: Migration 1055's pointer, present exactly on a run that PUBLISHED. ``None`` means *"not
    #: published"* — never "published, target unknown" — and any surface reporting it must say so
    #: rather than invent a table name.
    active_revision: ActiveRevision | None = None


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
    execution: RunExecution | None,
    clock: Callable[[], str],
    contract_overrides: ContractOverrides | None = None,
    execution_tier: ExecutionTier = ExecutionTier.PRODUCTION,
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
        execution: G-2's seam (:class:`RunExecution`), or ``None`` for "this deployment cannot
            execute a run". No default, for ``l0``'s reason. ``None`` does not skip G-2 — it is the
            run's OUTCOME: an unprepared run, terminating ``RUN_FAILED`` at
            :attr:`ChainStage.PREPARE_RUN`. G-2 is attempted only on a run whose L0 PASSED and whose
            publication capability was PROVEN, because §11.1 identifies an execution partly by the
            capability attestation and there is no attestation to name without a selection.
        clock: an offset-aware ISO 8601 instant, matching ``run_l0``/``run_l1``'s ``clock``. The
            plane mints no timestamps; every ``created_at`` and ``occurred_at`` here is this one.
        contract_overrides: §5's declared tightenings, if any.
        execution_tier: the bridge-realization APPLICABILITY scope this compilation reads joins at
            (``overlay/upload/bridge_realization.py:81``) — whether a cross-catalog join approved
            only for sandbox data may be used. **It is not a run execution tier** and it names no
            namespace: plan §3.4 decided Phase G introduces none, because the sandbox namespace is
            inside ``sandbox_execution_hash`` and a second one would fork execution identity. Two
            runs of one group at the two tiers seal identically; only which realizations
            ``compile_ir`` can SEE differs. It defaults to ``PRODUCTION`` — the value this chain
            asserted by omission before it was a parameter — because the durable request carries no
            tier of its own, so requiring it here would only move the same literal into the queue
            lane, one caller further from anything that governs it.

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
        # B3/D-4: the envelope admission validated this artifact against is the one compilation is
        # held to — carried on the admitted feature, never re-read from anywhere else.
        compiled = compile_ir(conn, feature, roles=roles, spine_decl=spine_declaration,
                              inventory=inventory, execution_tier=execution_tier,
                              plan_envelope=feature.plan_envelope)
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

    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    sealed = render_project(
        authorized, plan, environment_id=inventory.environment_id,
        engine_versions=inventory.engine_versions, spine_input=spine_input,
        nodes=tuple(assemble_nodes(NodeAssemblyInputs(
            authorized=authorized, plan=plan, contract=group.contract, admitted=by_name,
            spine_input=spine_input, datasets=datasets))))

    return _commit(conn, request, plan=plan, contract=group.contract, binding=binding,
                   existing=existing, project=sealed, selection=selection, authorized=authorized,
                   spine_input=spine_input, project_root=project_root, inventory=inventory,
                   l0=l0, execution=execution, roles=roles, clock=clock)


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
    selection: PublisherSelection | MaterializationRefused,
    authorized: AuthorizedCompilation,
    spine_input: PhysicalInputRequirement,
    project_root: str | os.PathLike[str],
    inventory: ClusterInventoryV1,
    l0: L0Interpreter | None,
    execution: RunExecution | None,
    roles: Sequence[str],
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

    ``RUNNING`` is a stepping stone here, and since D1 its documented meaning ("a run was prepared")
    is true on the one path that reaches ``prepare_run``. It is passed through on every path because
    ``accepted → committed`` is not a legal edge (``request_store.LEGAL_LIFECYCLE_TRANSITIONS``) and
    because it is the only state that stamps ``generation_id``/``run_id`` onto the request. Inside
    one transaction no reader ever observes it.

    **G-2 RUNS INSIDE THIS TRANSACTION**, for the reason L0 does: its verdict chooses the terminal,
    and a terminal chosen from a verdict recorded outside the block could be committed without it.
    The submission is a subprocess against a cluster and is therefore the one step whose effects the
    rollback does NOT reach — it writes into ``staging_root``, which is generation-scoped and
    immutable (``staging_root_for``), so a rolled-back run leaves staged output that no plan
    revision names and that §9's gates would refuse to assemble.

    **A FAILING L0 IS NOT A ROLLBACK.** The generation, the artifact, the report, the ``RUN_FAILED``
    terminal and the tree are all committed, and the request lands ``failed`` rather than
    ``committed``. Rolling back would destroy the only evidence of *why* the build failed, and the
    project is what an operator opens to see it; the transaction exists so that a run is recorded
    ONCE and WHOLE, not so that bad news is discarded. Only a raised exception rolls back.

    The terminal event is ``PUBLICATION_REFUSED`` carrying ``select_publisher``'s own code when the
    build was proved and publication was not, and ``RUN_FAILED`` in every other case — a build that
    failed or was never proved, and every G-2 stage that did not reach an answer.
    :class:`_RunAttempt` is the ONE place that decision is made, so the terminal, the lifecycle and
    ``stopped_at`` cannot disagree. See the module docstring for what each terminal means, why
    ``GATES_FAILED`` is not the second one, and why ``PUBLISHED`` can never be either.
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
                                      generation_id=generation_id,
                                      environment_id=inventory.environment_id,
                                      report_id=_report_id(request.request_id), clock=clock)
            record_validation_report(conn, report)
            built = report.status is ValidationStatus.PASSED

            if not built:
                attempt = _RunAttempt.build_unproven(report, l0=l0, selection=selection)
            elif isinstance(selection, PublisherSelection):
                attempt = _execute_the_run(
                    conn, staging, request=request, project=project, authorized=authorized,
                    spine_input=spine_input, selection=selection, inventory=inventory,
                    execution=execution, roles=roles, generation_id=generation_id, run_id=run_id,
                    plan=plan, binding=binding, clock=clock)
                if attempt.l1_report is not None:
                    record_validation_report(conn, attempt.l1_report)
            else:
                attempt = _RunAttempt.refused_publication(selection)

            append_run_event(conn, MaterializationRunEvent(
                run_id=run_id, seq=FIRST_RUN_EVENT_SEQ, generation_id=generation_id,
                event_kind=attempt.terminal, occurred_at=created_at, detail=attempt.detail))
            moved = advance_lifecycle(
                conn, request_id=request.request_id,
                to_state=(RequestLifecycle.COMMITTED
                          if attempt.terminal in _COMMITTING_TERMINALS
                          else RequestLifecycle.FAILED))
            _publish_tree(staging, root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return CompiledGroup(
        request_id=request.request_id, logical_group_name=request.logical_group_name,
        stopped_at=attempt.stopped_at,
        refusal=attempt.refusal,
        terminal_event=attempt.terminal,
        lifecycle_state=moved.lifecycle_state,
        generation_id=generation_id, run_id=run_id,
        materialization_contract_hash=plan.materialization_contract_hash,
        group_plan_hash=group_plan_hash(plan),
        generated_project_hash=project.identity.generated_project_hash, project_root=str(root),
        publication_refusal=attempt.publication_refusal,
        validation_report=report, l1_report=attempt.l1_report, submission=attempt.submission,
        sandbox_execution_hash=attempt.sandbox_execution_hash,
        active_revision=attempt.active_revision)


@dataclass(frozen=True, slots=True)
class _RunAttempt:
    """What the run reached after its build was proved, and the terminal that reach earns.

    ONE object decides the terminal event, the request's lifecycle and ``stopped_at`` together,
    because they are three readings of one fact and three separate expressions computing them from
    the same booleans is how they come to disagree. There are exactly two terminals available to
    this module — ``PUBLICATION_REFUSED`` (the only one folding to a non-failed status, legitimate
    only on a build proof and a publication verdict) and ``RUN_FAILED`` — and G-3 adds the third.
    """

    stopped_at: ChainStage
    terminal: RunEventKind
    detail: str
    refusal: MaterializationRefused | None = None
    #: The PUBLISHER's verdict, preserved wherever the chain stopped. Separate from ``refusal``
    #: (which names what STOPPED this run) because the two answer different questions: a run
    #: can stop at L0 and still have a publication verdict worth recording. Before this field
    #: that verdict was discarded — `if not built:` is tested first, so a refused publisher
    #: plus a failed build reported nothing and the publication question looked unasked.
    publication_refusal: MaterializationRefused | None = None
    l1_report: ValidationReportV1 | None = None
    submission: SubmissionOutcome | None = None
    sandbox_execution_hash: str | None = None
    active_revision: ActiveRevision | None = None

    @classmethod
    def build_unproven(cls, report: ValidationReportV1, *,
                       l0: L0Interpreter | None,
                       selection: PublisherSelection | MaterializationRefused | None = None,
                       ) -> _RunAttempt:
        """L0 failed or never ran — the run stops there and NOTHING downstream is attempted.

        Not a skip of G-2: a run whose project was not proved to build has no artifact to prepare
        inputs for, and preparing one would resolve partitions for bytes nobody validated.
        """
        return cls(stopped_at=ChainStage.VALIDATE_L0, terminal=RunEventKind.RUN_FAILED,
                   detail=_unproven_detail(report, configured=l0 is not None),
                   publication_refusal=(selection
                                        if isinstance(selection, MaterializationRefused)
                                        else None))

    @classmethod
    def refused_publication(cls, refusal: MaterializationRefused) -> _RunAttempt:
        """G-1's terminal: the build was proved and publication capability was not.

        G-2 is not attempted, and that is an OUTCOME rather than a skip: §11.1 identifies an
        execution partly by the capability attestation it will publish under
        (``prepare_run(capability_attestation_id=…)``, defaulted nowhere — ``identity.py:494``),
        so without a selection there is no attestation to name and therefore no execution to
        identify. The reason is the terminal's own detail, already carrying the refusal code.
        """
        return cls(stopped_at=ChainStage.PUBLISHER, terminal=RunEventKind.PUBLICATION_REFUSED,
                   detail=f"{refusal.code.value}: {refusal.detail}", refusal=refusal,
                   publication_refusal=refusal)

    @classmethod
    def published(cls, revision: ActiveRevision, *, l1_report: ValidationReportV1,
                  submission: SubmissionOutcome, sandbox_execution_hash: str) -> _RunAttempt:
        """The terminal G-1 was forbidden to write and G-3 earns — with the evidence attached.

        The detail names the object a reader can now query, the generation behind it and the
        attestation the swap rested on, because those are the three things an operator asks first
        and the only ones the plane can never re-derive once later probes are ingested.
        """
        return cls(stopped_at=ChainStage.PUBLISH, terminal=RunEventKind.PUBLISHED,
                   detail=(f"published {revision.published_object} at generation "
                           f"{revision.generation_id} (revision seq {revision.seq}, "
                           f"{revision.mechanism.value} under attestation "
                           f"{revision.capability_attestation_id})"
                           f"{_read_scope_note(l1_report)}"),
                   l1_report=l1_report, submission=submission,
                   sandbox_execution_hash=sandbox_execution_hash, active_revision=revision)

    @classmethod
    def failed_at(cls, stage: ChainStage, detail: str, *,
                  refusal: MaterializationRefused | None = None,
                  l1_report: ValidationReportV1 | None = None,
                  submission: SubmissionOutcome | None = None,
                  sandbox_execution_hash: str | None = None) -> _RunAttempt:
        return cls(stopped_at=stage, terminal=RunEventKind.RUN_FAILED, detail=detail,
                   refusal=refusal, l1_report=l1_report, submission=submission,
                   sandbox_execution_hash=sandbox_execution_hash)


def _execute_the_run(
    conn: DbConn,
    staging: pathlib.Path,
    *,
    request: MaterializationRequestV1,
    project: SealedProject,
    authorized: AuthorizedCompilation,
    spine_input: PhysicalInputRequirement,
    selection: PublisherSelection,
    inventory: ClusterInventoryV1,
    execution: RunExecution | None,
    roles: Sequence[str],
    generation_id: str,
    run_id: str,
    plan: FeatureGroupPlanV1,
    binding: GroupContractBinding,
    clock: Callable[[], str],
) -> _RunAttempt:
    """G-2: §11.1's ``prepare_run`` → §11.2's L1 → §11.1's submission, in that order and no other.

    Reached only from a run whose L0 PASSED and whose publication capability was PROVEN. Each stage
    is attempted, and every non-answer is an OUTCOME with a stage attached rather than a skip — the
    rule ``run_l0`` states and this applies three more times.

    THE ORDER IS THE DESIGN. Preparation resolves the exact partitions; L1 then checks *those*
    partitions still exist, that the read set's columns are what the environment declares and that
    these roles may read the tables — which is why L1 takes preparation's snapshots rather than
    re-resolving them. Submission comes last because it is the only step that spends cluster time,
    and both checks above it exist to keep a doomed run from starting.

    A GROUP WITH A CROSS-CATALOG HOP REFUSES HERE, by construction and on purpose. ``prepare_run``
    requires a ``BridgeExecutionAuthorization`` covering the artifact's exact IRs whenever the
    compilation declares realization dependencies (``runprep.py:859``), this chain has no parameter
    carrying one, and so a bridged group stops at :attr:`ChainStage.PREPARE_RUN` with
    ``JOIN_CARDINALITY_UNKNOWN`` rather than executing an unauthorized hop. That is also what makes
    the default ``required_parameters`` correct here: the one name that widens the rendered set is
    ``bridge_predicate_values`` (``render/project.py:1296``), which only a bridged artifact wires,
    and no bridged artifact can reach the submission below.

    ``binding.physical_target`` is the name the swap makes resolve — read from the binding rather
    than re-derived, because §10.1 wrote that name once for this logical group and a second
    derivation here would be a second answer to a question that has one.
    """
    if execution is None:
        return _RunAttempt.failed_at(
            ChainStage.PREPARE_RUN,
            f"this deployment configures no run execution seam, so the run for generation "
            f"{generation_id} was never prepared: G-2 needs a metastore adapter, a pipeline "
            f"submitter, a business date and a staging base (chain.RunExecution), and a run that "
            f"resolved no partitions and started no pipeline has published nothing. The project is "
            f"sealed, build-verified and on disk; publication capability IS proven for "
            f"{selection.environment_id!r} under attestation "
            f"{selection.capability_attestation_id}")

    spine_request = spine_input_request(authorized.spine, spine_input,
                                        business_dt=execution.business_dt)
    if isinstance(spine_request, MaterializationRefused):
        return _RunAttempt.failed_at(
            ChainStage.PREPARE_RUN,
            f"{spine_request.code.value}: {spine_request.detail}", refusal=spine_request)

    prepared = prepare_run(
        project.identity, inventory, execution.metastore, generation_id=generation_id,
        run_id=run_id, business_dt=execution.business_dt,
        requests=(spine_request, *run_input_requests(authorized.irs)),
        staging_base=execution.staging_base,
        capability_attestation_id=selection.capability_attestation_id)
    if isinstance(prepared, MaterializationRefused):
        return _RunAttempt.failed_at(
            ChainStage.PREPARE_RUN, f"{prepared.code.value}: {prepared.detail}", refusal=prepared)

    l1 = run_l1(
        project.identity, prepared.snapshots, irs=authorized.irs, inventory=inventory,
        metastore=execution.metastore, roles=roles, generation_id=generation_id, run_id=run_id,
        report_id=_l1_report_id(request.request_id), clock=clock,
        read_scope=execution.read_scope)
    if l1.status is not ValidationStatus.PASSED:
        return _RunAttempt.failed_at(
            ChainStage.VALIDATE_L1, _l1_detail(l1), l1_report=l1,
            sandbox_execution_hash=prepared.sandbox_execution_hash)

    submitted = execution.submitter.submit(staging, run_parameters=prepared.parameters)
    if not submitted.completed:
        return _RunAttempt.failed_at(
            ChainStage.SUBMIT, _submission_detail(submitted), l1_report=l1, submission=submitted,
            sandbox_execution_hash=prepared.sandbox_execution_hash)

    # G-3. The pointer is RECORDED before the swap is performed and the swap before the terminal is
    # appended, so nothing claims a publication that has not already been written down — see
    # `publish_generation` for why the other order is the one that can produce an unauditable table.
    revision = publish_generation(
        conn, execution.swap, selection=selection, group_plan=plan, generation_id=generation_id,
        run_id=run_id, published_object=binding.physical_target,
        staging_root=str(prepared.parameters["staging_root"]),
        revision_id=_revision_id(request.request_id), activated_at=clock())
    return _RunAttempt.published(
        revision, l1_report=l1, submission=submitted,
        sandbox_execution_hash=prepared.sandbox_execution_hash)


def _read_scope_note(report: ValidationReportV1) -> str:
    """The plain-language acceptance, appended to the terminal a PUBLISHED run writes — or ``""``.

    **WHY THE TERMINAL EVENT AND NOT ONLY THE REPORT.** The acceptance is already durable on
    ``pipeline_validation_report``, per table and typed, and that is the record a later audit reads.
    But the run event is what an operator reads FIRST — it is the one line
    ``GET /materialization-runs/{id}`` and the run screen lead with — and a publication that says
    only "published X at generation Y" reads as a run that passed every check. It did not: one check
    could not be performed, and a deployment accepted that in advance. Saying so here costs one
    sentence and is the difference between a record that is complete and a record that is merely
    true.

    ONLY the PUBLISHED terminal carries it. Every other terminal's detail names the thing that
    stopped the run, and appending an accepted absence to a failure would compete with the headline
    for a reader's attention while the report behind it says the same thing in full.

    ``""`` for a run whose engine answered — which is what makes the sentence mean something when it
    IS there. A note printed on every run is a note nobody reads.
    """
    accepted = [finding for finding in report.findings
                if finding.code is ValidationFindingCode.READ_SCOPE_UNVERIFIED]
    if not accepted:
        return ""
    tables = ", ".join(sorted(finding.location for finding in accepted))
    return (f". READ SCOPE WAS NOT VERIFIED for {len(accepted)} table(s) ({tables}): this "
            f"deployment declares no authorization model, so nobody checked whether the authorized "
            f"roles may read this run's inputs — L1 report {report.report_id} records it per table "
            f"as {ValidationFindingCode.READ_SCOPE_UNVERIFIED.value}")


def _l1_detail(report: ValidationReportV1) -> str:
    """L1's failure, in §14's terms: codes, classes and counts, never a data value.

    Split from ``_unproven_detail`` rather than shared with it because the two silences differ:
    an L0 ``error`` is "the artifact was never imported", while an L1 ``error`` is "the METASTORE
    did not answer" — a cluster fact, and a different person's fix.
    """
    if report.status is ValidationStatus.ERROR:
        return (f"L1 could not run, so this run's inputs are UNVERIFIED ({report.report_id}): the "
                f"metastore did not answer, and a run started on unchecked partitions would read "
                f"whatever is there")
    seen = "; ".join(f"{finding.code.value}({finding.classification.value}) x{finding.count}"
                     for finding in report.findings)
    return (f"L1 failed, so this run's resolved inputs are not what the environment holds "
            f"({report.report_id}): {seen}")


def _submission_detail(outcome: SubmissionOutcome) -> str:
    """A submission that FAILED and one that never STARTED, told apart — ``submit.py:60``'s rule.

    ``returncode is None`` means no process produced a verdict about the pipeline (the interpreter
    could not be launched, or the timeout killed it), and reporting an exit status would be an
    invented observation. Both carry the submitter's own ``detail`` verbatim.
    """
    if outcome.returncode is None:
        return (f"the pipeline was never started, so nothing is known about this run: "
                f"{outcome.detail}")
    return (f"the pipeline ran and failed with returncode {outcome.returncode}: {outcome.detail}")


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

    Raises:
        ValueError: the report describes a DIFFERENT artifact than the one this generation names.
            Today that cannot happen — ``root`` is the tree ``materialize_to(project, …)`` wrote
            four lines above the call — but "cannot happen" is a property of two adjacent lines,
            and the whole of this task is that a build proof must be about the project the record
            names STRUCTURALLY rather than by proximity. A ``ValueError`` rather than a governed
            code, for §14's reason: this is a defect in this module, not a verdict about a feature,
            and it aborts the transaction rather than recording a proof about the wrong bytes.
    """
    if l0 is None:
        started_at = clock()
        report = ValidationReportV1(
            report_id=report_id, generation_id=generation_id, run_id=None,
            generated_project_hash=project.identity.generated_project_hash,
            group_plan_hash=project.identity.compilation.group_plan_hash,
            level=ValidationLevel.L0, environment_id=environment_id,
            status=ValidationStatus.ERROR, started_at=started_at, finished_at=clock(),
            findings=())
    else:
        report = run_l0(root, generation_id=generation_id, environment_id=environment_id,
                        report_id=report_id, python_executable=l0.python_executable, clock=clock,
                        env=l0.env, timeout_seconds=l0.timeout_seconds)

    sealed = project.identity.generated_project_hash
    if report.generated_project_hash != sealed:
        raise ValueError(
            f"L0 reported on {report.generated_project_hash!r} and this generation seals "
            f"{sealed!r}: a build proof is a claim about a SPECIFIC artifact, and one carried over "
            f"from another tree would let the terminal say 'proven to build' about bytes nobody "
            f"validated — which the append-only plane could never retract")
    return report


def _unproven_detail(report: ValidationReportV1, *, configured: bool) -> str:
    """The ``RUN_FAILED`` event's operator-facing text: what L0 saw, and WHO fixes it.

    §14's rule for a detail — counts, types, codes and locations, never data values — and §11.2's
    rule that a finding's CLASS is what routes a decision. A reader of the append-only stream gets
    the codes, their classes and the report id to open; the findings themselves stay in
    ``pipeline_validation_report``, where ``read_validation_reports`` returns them intact.

    ``error`` prints no findings because it HAS none, and says which of the two silences it is:
    a deployment that configured no interpreter is an operator's act, while an interpreter that did
    not answer is the environment's — different people fix them.

    ``ENGINE_VERSION_MISMATCH`` gets its own sentence, because "this project does not build" would
    be a claim nobody made: the probe stops at the pin comparison and never attempts the import, so
    the build is UNPROVEN rather than failed. Telling an operator their project does not build when
    the actual fault is that L0 was pointed at the wrong environment is the same mis-routing
    DEFERRED-WORK A.42 exists to eliminate, one layer up.
    """
    if report.status is ValidationStatus.ERROR:
        because = ("no L0 interpreter is configured" if not configured else
                   "the configured interpreter did not answer (launch failure, timeout, or no "
                   "verdict printed)")
        return (f"L0 could not run, so this project's build is UNPROVEN ({report.report_id}): "
                f"{because}")
    seen = "; ".join(f"{finding.code.value}({finding.classification.value}) x{finding.count}"
                     for finding in report.findings)
    if any(finding.code is ValidationFindingCode.ENGINE_VERSION_MISMATCH
           for finding in report.findings):
        return (f"L0 did not attempt the build, which is therefore UNPROVEN ({report.report_id}): "
                f"the interpreter is not the environment this artifact pins itself to — {seen}")
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

    WHAT IS PUBLISHED IS THE STAGED TREE **PLUS L0'S RESIDUE**, and that is worth stating.
    ``_probe_verdict`` runs the probe with ``cwd`` set to the staging directory and imports the
    project there, so ``__pycache__`` written by that import is inside the tree this function then
    publishes: the directory at ``root`` is NOT byte-identical to ``materialize_to``'s output.
    It is benign today and by design — ``validation.py``'s ``_files_on_disk`` skips ``__pycache__``
    for exactly this reason ("L0's own probe creates the first of them") and the rendered
    ``PROJECT_INTEGRITY`` gate uses the same skip list, so both hashers still see the sealed file
    set. It is recorded because the invariant is narrower than it looks: a future probe with richer
    side effects (a ``.lock``, a log, a compiled extension) would publish those too, and anything
    outside that shared skip list becomes drift the integrity gate reports against the operator.

    No ``mkdir`` here, and that is not an omission. ``staging`` is ``root``'s SIBLING
    (``root.parent / f".{root.name}.partial"``), and ``_commit`` already wrote the whole project
    into it through ``materialize_to``, which mkdirs every file's parent with ``parents=True``
    (``render/project.py:1300``). So ``root.parent`` provably exists by the time this runs — it is
    ``staging``'s parent too, and ``staging`` is populated. A ``mkdir`` here could only re-assert
    what the write it follows already proved.
    """
    os.replace(staging, root)


class _Stop:
    """How a governed refusal becomes a recorded, returned outcome — in ONE place.

    A refusal before the project is sealed has nowhere in the control plane to go (see the module
    docstring), so what it records is the request's own terminal state. Recording it here rather
    than at each stage is what makes "every governed refusal is recorded and returned, never
    swallowed" a property of the chain rather than of nine call sites.

    The write is NARROWED to ``accepted``. Every refusal this class records was reached from a stage
    that ran on an ``accepted`` request — ``_claim`` requires that state and raises otherwise — and
    an unnarrowed UPDATE would also match ``running``, terminalizing a request that had moved on
    against a verdict about the state it left. Unreachable in G-1 (nothing outside ``_commit``'s
    transaction can produce ``running``) and durable the moment G-2's ``prepare_run`` lands, which
    is ``reconcile.py:541``'s argument applied to the chain's own refusal path.
    """

    __slots__ = ("conn", "request")

    def __init__(self, conn: DbConn, request: MaterializationRequestV1) -> None:
        self.conn = conn
        self.request = request

    def refused(self, stage: ChainStage, refusal: MaterializationRefused) -> CompiledGroup:
        moved = advance_lifecycle(self.conn, request_id=self.request.request_id,
                                  to_state=RequestLifecycle.FAILED,
                                  expected_from=RequestLifecycle.ACCEPTED)
        return CompiledGroup(
            request_id=self.request.request_id,
            logical_group_name=self.request.logical_group_name,
            # `publication_refusal` is None and that is TRUTHFUL here: this path records a stage
            # that RAISED its refusal, and publisher selection returns its refusal rather than
            # raising, so publication was never reached and has no verdict to preserve.
            stopped_at=stage, refusal=refusal, publication_refusal=None, terminal_event=None,
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
        # A replay reconstructs from recorded EVENTS, which carry the terminal but not the
        # publisher's own refusal object; None here says "this replay does not know", not "there
        # was no refusal". Reading the terminal event is how a caller learns publication was
        # refused on a replayed run.
        stopped_at=None, refusal=None, publication_refusal=None,
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


def _revision_id(request_id: str) -> str:
    """Migration 1055's primary key, DERIVED for ``_report_id``'s reason: a re-entry that reached
    the write with a random id would append a SECOND pointer for one swap instead of colliding, and
    on an append-only table with no repair path that is the difference between a loud failure and
    two rows claiming to be what a reader is seeing."""
    return _derived_id("frev", purpose="feature_active_revision", material=request_id)


def _l1_report_id(request_id: str) -> str:
    """L1's own derived id. A DIFFERENT purpose from L0's, so one request's two reports cannot
    collide on ``pipeline_validation_report``'s primary key — and derived for L0's reason: a random
    id on a re-entry would append a second verdict instead of colliding."""
    return _derived_id("l1rep", purpose="l1_validation_report", material=request_id)


def _binding_id(logical_group_name: str) -> str:
    """Derived from the GROUP, not the request: §10.1 writes one binding per logical name, ever,
    and an id derived per request would be a second binding_id for a row that already exists."""
    return _derived_id("gbind", purpose="group_binding", material=logical_group_name)
