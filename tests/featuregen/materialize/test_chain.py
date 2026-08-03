"""Phase G T3 — ``compile/chain.py``: the orchestrator, stages 1 → seal.

**What is real here.** Every stage is the real one, over the real seeded catalog, driven from a real
durable identity: a ``recipe_formula_shadow_work_item`` whose authoring run was written by the REAL
1022 orchestrator. Only the two PROVIDER calls inside authoring are scripted, for the reason
``test_resolve.py`` states (the audited ``llm_call`` rows ``load_verified_checkpoint`` reconciles
exist only under a durable DSN, which this suite deliberately does not have). Nothing in
``chain.py`` itself is stubbed by any test below. The ONE seam Task 4 owns — the Kedro node
assembly — is now the REAL ``compile/wiring.assemble_nodes``; it stays a parameter rather than a
call so the chain does not become the second place the wiring is described, and
``test_the_chain_takes_the_assembly_it_is_GIVEN`` drives a different assembler to show that.

**The catalog is the UNION of two shipped seeds, not a third one.** ``fixtures.seed_materialize_catalog``
is the only seed whose ``build_graph`` route makes ``logical_representation`` a GOVERNED decision,
which is what lets the authoring lane resolve an output policy at all; ``test_ir.seed_catalog`` is
the only one that carries a spine (``customers``) for ``compile_ir`` to land on. Neither alone can
drive this chain, and ``build_graph`` DELETEs the source's whole graph, so they cannot simply both
be called. :func:`_seed` therefore calls the first and then adds the second's spine tables and
governed facts THROUGH ``test_ir``'s own helpers — a union of two definitions, never a third.

**The truthfulness test is the load-bearing one.** ``test_the_chain_can_never_append_PUBLISHED``
reads the module's AST, not its behaviour, because a behavioural test only proves that today's
paths do not lie. Plan §3.5: the plane is append-only with a one-terminal index, so a false
publication claim can never be retracted.
"""
from __future__ import annotations

import ast
import dataclasses
import importlib.util
import inspect
import pathlib
import sys

import psycopg
import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    DECLARATION,
    INVENTORY,
    _col,
    _edge,
    _govern_availability,
    _govern_entity,
    _govern_grain,
    _table_node,
    _tag,
)
from tests.featuregen.materialize.test_render_project import _nodes
from tests.featuregen.materialize.test_resolve import (  # noqa: F401 — `no_dsn` is autouse
    _author_the_run,
    _seed_work_item,
    no_dsn,
)

from featuregen.materialize.admission import FeatureNamePlanError
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import (
    CompilationRefusalCode,
    MaterializationRefused,
    PublicationRefusalCode,
    ValidationFindingCode,
)
from featuregen.materialize.compile import chain
from featuregen.materialize.compile.chain import (
    FIRST_RUN_EVENT_SEQ,
    ChainStage,
    CompiledGroup,
    L0Interpreter,
    compile_feature_group,
)
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
    ContractOverrides,
)
from featuregen.materialize.control_plane import (
    RunEventKind,
    RunStatus,
    published_generation_ids,
    read_compiled_artifact,
    read_group_binding,
    read_plan_revisions,
    read_run_events,
    run_status,
)
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME, read_lock
from featuregen.materialize.publish import (
    ProbeObservation,
    PublishMechanism,
    assess_probe_observations,
    record_attestation,
)
from featuregen.materialize.request_store import (
    RequestLifecycle,
    accept_request,
    read_request,
    record_request,
)
from featuregen.materialize.validation import (
    FindingClass,
    ValidationFinding,
    ValidationLevel,
    ValidationReportV1,
    ValidationStatus,
    read_validation_reports,
)

_FEATURE = "total_debit_amount_30d"
_GROUP = "cif_daily"
_ROLES = ("feature_engineer",)
_ACTOR = "user:asha"
_CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                       business_date_cutoff="00:00", trigger=CadenceTrigger.SCHEDULED)
_PROMISE = AvailabilityPromiseV1(calendar_days=1)


# ── the governed catalog: the union of the two shipped seeds (module docstring) ──────────────────

def _seed(db):
    fixtures.seed_materialize_catalog(db)
    _col(db, "transactions", "acct_id")
    for column in ("account_id", "cif_id"):
        _col(db, "accounts", column)
    for column in ("cif_id", "load_ts", "status_cd", "effective_from", "version_seq"):
        _col(db, "customers", column)
    for table in ("accounts", "customers"):
        _table_node(db, table)
    _edge(db, "public.transactions.acct_id", "public.accounts.account_id", fact_key="ajf-txn-acct")
    _edge(db, "public.accounts.cif_id", "public.customers.cif_id", fact_key="ajf-acct-cust")
    # `build_graph` flags `txn_dt` as the as-of column and links the fact event, but writes no
    # `overlay_fact_state` row, and §8's gate dereferences the fact to learn the BASIS.
    _govern_availability(db, "transactions", "txn_dt")
    _govern_grain(db, "customers", "cif_id")
    _govern_entity(db, "customers", "cif_id")
    _govern_availability(db, "customers", "load_ts", event_id="ovf_evt_asof_cust")
    return db


@pytest.fixture
def catalog(db):
    return _seed(db)


# ── a durable request, and the durable feature identities it names ───────────────────────────────

def _request(db, *, request_id="req-0001", roles=_ROLES, group=_GROUP):
    """A request row in the state the queue lane hands the chain: ``accepted``, holding a lease."""
    record_request(db, request_id=request_id, logical_group_name=group, requested_by=_ACTOR,
                   authorized_roles=roles, idempotency_key=f"key-{request_id}",
                   activation_state={"flag": "on"})
    return accept_request(db, request_id=request_id, lease_seconds=300).request_id


def _authored(db, monkeypatch, name=_FEATURE, suffix="a"):
    work_item_id = _seed_work_item(db, name, suffix)
    _author_the_run(db, monkeypatch, work_item_id, name)
    return work_item_id


# ── the L0 seam: a real interpreter is CONFIGURED, and its VERDICT is what a test drives ─────────
#
# `sys.executable` is a genuine, launchable interpreter that does NOT have kedro — the honest
# statement of what this suite's environment is. Tests that need a particular L0 verdict inject it
# at `chain.run_l0` (as Tasks 3/5 inject `materialize_to`); tests that do not are running the REAL
# `run_l0` against it, which is why `test_the_chain_calls_the_REAL_run_l0_...` is not a mock at all.
# No collected test needs kedro or pyspark: the real-interpreter proof lives in `l0_gate.py`.
_L0 = L0Interpreter(python_executable=sys.executable, timeout_seconds=60.0)


def _verdict(root, *, generation_id, environment_id, report_id, clock,
             status=ValidationStatus.PASSED, findings=()) -> ValidationReportV1:
    """An L0 report for a tree that REALLY EXISTS — its identity is read off the lock on disk.

    Deliberately not a constant. A stub that ignored ``root`` would let the chain hand `run_l0` a
    path that was never materialized and still record a build proof, which is the exact shape of
    lie this task exists to make impossible.
    """
    identity = read_lock((pathlib.Path(root) / GENERATED_LOCK_FILENAME).read_text())
    return ValidationReportV1(
        report_id=report_id, generation_id=generation_id, run_id=None,
        generated_project_hash=identity.generated_project_hash,
        group_plan_hash=identity.compilation.group_plan_hash, level=ValidationLevel.L0,
        environment_id=environment_id, status=status, started_at=clock(), finished_at=clock(),
        findings=tuple(findings))


def _inject_l0(monkeypatch, status=ValidationStatus.PASSED, findings=()):
    def _run_l0(root, *, generation_id, environment_id, report_id, python_executable, clock,
                env=None, timeout_seconds=300.0):
        assert python_executable == _L0.python_executable, "the caller's interpreter, or none"
        return _verdict(root, generation_id=generation_id, environment_id=environment_id,
                        report_id=report_id, clock=clock, status=status, findings=findings)

    monkeypatch.setattr(chain, "run_l0", _run_l0)


@pytest.fixture
def l0_passes(monkeypatch):
    """L0 PASSES — the precondition of every test below whose subject is not L0 itself."""
    _inject_l0(monkeypatch)


@pytest.fixture
def ready(catalog, monkeypatch, l0_passes, tmp_path):
    """One accepted request naming one resolvable feature — the whole durable identity."""
    return _request(catalog), [_authored(catalog, monkeypatch)], tmp_path


def _assemble(inputs) -> tuple:
    """The Task-4 seam — now the REAL assembler, so nothing in this file is stubbed at all.

    It stood in with ``test_render_project``'s stub-bodied wiring while
    ``compile/wiring.assemble_nodes`` did not exist. Passing the real one keeps the chain's own
    tests honest about what a run produces: the node BODIES are the renderers' and the wiring is the
    assembly's, and ``_check_wiring`` refuses either if they do not close.

    ``_nodes`` is still imported and exercised below, in
    ``test_the_chain_takes_the_assembly_it_is_GIVEN``: the seam is a parameter, and a test that only
    ever passed one assembler would not show that."""
    return assemble_nodes(inputs)


def _clock():
    return "2026-08-03T12:00:00+00:00"


def _run(db, request_id, work_item_ids, root, *, overrides=None, published_schema=None,
         spine=DECLARATION, inventory=INVENTORY, assemble=_assemble, l0=_L0) -> CompiledGroup:
    return compile_feature_group(
        db, request_id=request_id, work_item_ids=work_item_ids, inventory=inventory,
        spine_declaration=spine, cadence=_CADENCE, availability_promise=_PROMISE,
        contract_overrides=overrides, mechanism=PublishMechanism.VERSIONED_POINTER,
        published_schema=published_schema, assemble_nodes=assemble, project_root=root, l0=l0,
        clock=_clock)


# ── THE happy path: a governed feature becomes a sealed project ──────────────────────────────────

def test_a_governed_feature_becomes_a_sealed_project_on_disk(ready, catalog) -> None:
    """End to end. The assertions are on the ARTIFACTS and the RECORD, never on "nothing raised"."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    assert outcome.stopped_at is ChainStage.PUBLISHER
    assert outcome.refusal is not None
    assert outcome.refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert outcome.terminal_event is RunEventKind.PUBLICATION_REFUSED
    assert outcome.lifecycle_state is RequestLifecycle.COMMITTED

    # the project exists, and it is SEALED — the lock is the thing only `seal_project` writes
    project = pathlib.Path(outcome.project_root)
    assert (project / GENERATED_LOCK_FILENAME).is_file()
    assert (project / "conf" / "base" / "catalog.yml").is_file()
    identity = read_lock((project / GENERATED_LOCK_FILENAME).read_text())
    assert identity.generated_project_hash == outcome.generated_project_hash
    assert identity.compilation.group_plan_hash == outcome.group_plan_hash
    assert identity.compilation.materialization_contract_hash == \
        outcome.materialization_contract_hash


def test_the_chain_renders_the_REAL_node_bodies_the_assembly_produced(ready, catalog) -> None:
    """The Task-4 seam, closed. The project on disk carries the renderers' own compute — the §4.2
    spine, the §8 projection, the §9 calculation and gates — and not a placeholder."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    nodes_py = (pathlib.Path(outcome.project_root) / "src" / f"sandbox_feature_{_GROUP}" /
                "pipelines" / "materialize" / "nodes.py").read_text()
    assert "def build_spine(" in nodes_py
    assert f"def calculate_{_FEATURE}(" in nodes_py
    assert "def gate_and_publish(" in nodes_py
    assert "renders this body" not in nodes_py


def test_the_chain_takes_the_assembly_it_is_GIVEN(ready, catalog) -> None:
    """`assemble_nodes` is a PARAMETER, not a call: the chain must not become the second place the
    wiring is described. Driving one run through `test_render_project`'s stub wiring is what shows
    the seam is still open — a chain that had quietly hard-coded `compile.wiring` would render the
    real bodies here too."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root,
                   assemble=lambda inputs: _nodes(inputs.datasets))

    nodes_py = (pathlib.Path(outcome.project_root) / "src" / f"sandbox_feature_{_GROUP}" /
                "pipelines" / "materialize" / "nodes.py").read_text()
    assert "Task 13/14 renders this body" in nodes_py


def test_an_unproven_capability_renders_the_FAIL_CLOSED_publication_entry(ready, catalog) -> None:
    """A publication refusal does not stop the chain — the compilation is sound and the project is
    worth having — but the artifact it produces must be unable to publish. `render_project` with no
    selection renders §10.3's fail-closed Hive entry onto the sandbox binding rather than the
    generation-scoped parquet target a proven mechanism would have earned."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    rendered = (pathlib.Path(outcome.project_root) / "conf" / "base" / "catalog.yml").read_text()
    assert 'database: "sandbox_feature"' in rendered
    assert "capability attestation" not in rendered


def test_the_generation_records_the_hashes_the_lock_carries(ready, catalog) -> None:
    """The plane's generation row and the sealed lock must agree, because §9's gates compare a run
    against the plan the generation names and L0 re-derives the project hash off the disk."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    stored = catalog.execute(
        "SELECT logical_group_name, materialization_contract_hash, group_plan_hash, "
        "generated_project_hash, created_at FROM materialization_generation WHERE "
        "generation_id = %s", (outcome.generation_id,)).fetchone()
    assert stored == (_GROUP, outcome.materialization_contract_hash, outcome.group_plan_hash,
                      outcome.generated_project_hash, _clock())


def test_the_binding_and_its_plan_revision_are_recorded(ready, catalog) -> None:
    """§10.1's two records. Without the revision, `current_plan_revision` has nothing to derive
    from and the group's packing-list history starts empty at its first publication."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    binding = read_group_binding(catalog, _GROUP)
    assert binding is not None
    assert binding.materialization_contract_hash == outcome.materialization_contract_hash
    assert binding.physical_target == f"sandbox_feature.{_GROUP}"
    revisions = read_plan_revisions(catalog, binding.binding_id)
    assert [(r.generation_id, r.group_plan_hash) for r in revisions] == [
        (outcome.generation_id, outcome.group_plan_hash)]


def test_what_the_run_INTENDED_TO_READ_survives_the_run(ready, catalog) -> None:
    """§3.6's compile-side retention (migration 1054). Before it, a crash after render left only
    hashes: nobody could answer "which features, which columns, which spine?" from the record, and
    §3.3's reconciler had no compile-side evidence to reconcile against.

    The bodies are asserted to re-derive to the hashes this very run reported — through the
    package's one hasher, over a real plan and a real contract that a real compilation produced."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    stored = read_compiled_artifact(catalog, generation_id=outcome.generation_id)
    assert stored is not None
    assert stored.group_plan_hash == outcome.group_plan_hash
    assert stored.contract_hash == outcome.materialization_contract_hash
    assert materialize_hash(stored.group_plan) == outcome.group_plan_hash
    assert materialize_hash(stored.materialization_contract) == \
        outcome.materialization_contract_hash
    # the PACKING LIST itself — the thing a human could not previously read back at all
    assert stored.group_plan["logical_group_name"] == _GROUP
    assert [feature["column_name"] for feature in stored.group_plan["features"]] == [_FEATURE]


def test_the_run_carries_exactly_one_event_and_it_is_the_truthful_terminal(
        ready, catalog) -> None:
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    events = read_run_events(catalog, outcome.run_id)
    assert [(e.seq, e.event_kind) for e in events] == [
        (FIRST_RUN_EVENT_SEQ, RunEventKind.PUBLICATION_REFUSED)]
    assert PublicationRefusalCode.CAPABILITY_UNPROVEN.value in events[0].detail
    assert events[0].generation_id == outcome.generation_id
    assert run_status(catalog, outcome.run_id) is RunStatus.REFUSED


def test_a_second_binding_for_the_same_group_is_not_written_twice(catalog, monkeypatch, l0_passes,
                                                                  tmp_path) -> None:
    """`record_group_binding` is a UniqueViolation on a second write, so the chain must recognise
    the binding `bind_group` RETURNED UNCHANGED and append only the revision."""
    first = _request(catalog, request_id="req-1")
    items = [_authored(catalog, monkeypatch, suffix="one")]
    one = _run(catalog, first, items, tmp_path / "one")

    second = _request(catalog, request_id="req-2")
    two = _run(catalog, second, items, tmp_path / "two")

    binding = read_group_binding(catalog, _GROUP)
    assert binding is not None
    revisions = read_plan_revisions(catalog, binding.binding_id)
    assert {r.generation_id for r in revisions} == {one.generation_id, two.generation_id}
    assert one.generation_id != two.generation_id


# ── L0: the terminal may never imply a build proof the run does not hold ─────────────────────────

def test_a_PASSING_L0_is_recorded_and_is_what_the_truthful_terminal_now_MEANS(
        ready, catalog) -> None:
    """The G-1 terminal was "compiled, rendered and sealed". With L0 in the chain it is "…and
    proven to BUILD", and that upgrade is only honest if the proof is durable and readable.

    The report is read back through `read_validation_reports` — the recorded rule, not the object
    this process happened to hold — and its two hashes must be the generation's own, or the proof
    would be about some other project."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    reports = read_validation_reports(catalog, generation_id=outcome.generation_id)
    assert [(r.level, r.status, r.findings) for r in reports] == \
        [(ValidationLevel.L0, ValidationStatus.PASSED, ())]
    assert reports[0].generated_project_hash == outcome.generated_project_hash
    assert reports[0].group_plan_hash == outcome.group_plan_hash
    assert reports[0].environment_id == INVENTORY.environment_id
    assert reports[0].run_id is None                  # L0 validates the project before any run
    assert outcome.validation_report == reports[0]
    # …and only THEN the terminal Task 3 records.
    assert outcome.stopped_at is ChainStage.PUBLISHER
    assert outcome.terminal_event is RunEventKind.PUBLICATION_REFUSED
    assert outcome.refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert outcome.lifecycle_state is RequestLifecycle.COMMITTED


@pytest.mark.parametrize("code", [ValidationFindingCode.PROJECT_DOES_NOT_BUILD,
                                  ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE,
                                  ValidationFindingCode.PROJECT_HASH_MISMATCH])
def test_a_FAILING_L0_stops_the_run_and_the_terminal_says_the_BUILD_failed(
        catalog, monkeypatch, tmp_path, code) -> None:
    """A project that does not build must not reach `PUBLICATION_REFUSED`: that terminal folds to
    `RunStatus.REFUSED`, which reads as "everything worked and publication was declined".

    `RUN_FAILED` is the documented "failed outside the gates" kind (`control_plane.py:120`);
    `GATES_FAILED` is §9's, and §9 gates run inside a SUBMITTED run over computed output that in
    G-1 does not exist. The findings and their CLASSIFICATION — who fixes it — must survive in the
    plane's own record and in the event detail a reader of the append-only stream sees."""
    request_id = _request(catalog, request_id=f"req-{code.value.lower()}")
    work_items = [_authored(catalog, monkeypatch, suffix="l0fail")]
    _inject_l0(monkeypatch, status=ValidationStatus.FAILED, findings=(
        ValidationFinding(code=code, location="sandbox_feature_cif_daily.pipeline_registry",
                          expected="importable", observed="ImportError", count=1),))

    outcome = _run(catalog, request_id, work_items, tmp_path)

    assert outcome.stopped_at is ChainStage.VALIDATE_L0
    assert outcome.terminal_event is RunEventKind.RUN_FAILED
    assert outcome.lifecycle_state is RequestLifecycle.FAILED
    assert read_request(catalog, request_id=request_id).lifecycle_state is RequestLifecycle.FAILED
    assert run_status(catalog, outcome.run_id) is RunStatus.FAILED
    assert published_generation_ids(catalog) == frozenset()

    stored = read_validation_reports(catalog, generation_id=outcome.generation_id)
    assert [f.code for f in stored[0].findings] == [code]
    assert stored[0].status is ValidationStatus.FAILED
    assert outcome.validation_report == stored[0]

    # legible from the event stream alone: the code, WHO fixes it, and the report to open
    detail = read_run_events(catalog, outcome.run_id)[0].detail
    assert code.value in detail
    assert stored[0].findings[0].classification.value in detail
    assert stored[0].report_id in detail


def test_a_FAILING_L0_still_leaves_the_project_where_the_RECORD_says_it_is(
        catalog, monkeypatch, tmp_path) -> None:
    """The generation row carries a `generated_project_hash` and the record names a project, so the
    tree must exist — it is the evidence an operator opens to see why it does not build. A failed
    build is a committed, legible outcome, not a rollback."""
    request_id = _request(catalog, request_id="req-l0-evidence")
    work_items = [_authored(catalog, monkeypatch, suffix="l0evidence")]
    _inject_l0(monkeypatch, status=ValidationStatus.FAILED, findings=(
        ValidationFinding(code=ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE,
                          location="p.pipeline_registry", expected="a pipeline with at least one "
                          "node", observed="0 pipelines", count=1),))

    outcome = _run(catalog, request_id, work_items, tmp_path)

    project = pathlib.Path(outcome.project_root)
    assert (project / GENERATED_LOCK_FILENAME).is_file()
    assert read_lock((project / GENERATED_LOCK_FILENAME).read_text()).generated_project_hash == \
        outcome.generated_project_hash
    assert catalog.execute(
        "SELECT generated_project_hash FROM materialization_generation WHERE generation_id = %s",
        (outcome.generation_id,)).fetchone()[0] == outcome.generated_project_hash
    assert not [entry for entry in project.parent.iterdir() if entry.name.startswith(".")]


def test_an_L0_THAT_COULD_NOT_RUN_never_looks_like_one_that_PASSED(
        catalog, monkeypatch, tmp_path) -> None:
    """THE decision, stated as a test. `l0=None` is "this deployment has no interpreter", and the
    run is `RUN_FAILED` with a `status="error"` report carrying ZERO findings — §11.2's own
    vocabulary for "the validation did not run", not an invented notion of "skipped".

    A skipped gate that leaves a `PUBLICATION_REFUSED` terminal behind is indistinguishable from a
    gate that passed, and the plane is append-only: that claim could never be retracted."""
    request_id = _request(catalog, request_id="req-no-interpreter")
    work_items = [_authored(catalog, monkeypatch, suffix="nol0")]

    def _never(*_args, **_kwargs):
        raise AssertionError("nothing may be launched when no interpreter was configured")

    monkeypatch.setattr(chain, "run_l0", _never)

    outcome = _run(catalog, request_id, work_items, tmp_path, l0=None)

    assert outcome.stopped_at is ChainStage.VALIDATE_L0
    assert outcome.terminal_event is RunEventKind.RUN_FAILED
    assert outcome.lifecycle_state is RequestLifecycle.FAILED
    assert outcome.validation_report.status is ValidationStatus.ERROR
    assert outcome.validation_report.findings == ()          # nothing was looked at, so nothing out
    stored = read_validation_reports(catalog, generation_id=outcome.generation_id)
    assert (stored[0].status, stored[0].findings) == (ValidationStatus.ERROR, ())
    assert stored[0].generated_project_hash == outcome.generated_project_hash
    detail = read_run_events(catalog, outcome.run_id)[0].detail
    assert "no L0 interpreter is configured" in detail
    assert PublicationRefusalCode.CAPABILITY_UNPROVEN.value not in detail


def test_the_interpreter_and_the_TIMEOUT_are_the_CALLERS_never_the_environments(
        catalog, monkeypatch, tmp_path) -> None:
    """The chain is a library; the trigger surface owns configuration. A chain that read
    `FEATUREGEN_L0_PYTHON` itself would make every deployment's build proof depend on an
    environment variable no record names.

    It also pins what `run_l0` is handed: a directory that REALLY holds the sealed tree, AT THE
    MOMENT OF THE CALL. That timing is the whole assertion — the tree is validated at the staging
    sibling and only then moved to the project's own path, so a check made after the chain returned
    would look at a path that no longer exists and pass or fail for the wrong reason. A chain that
    handed L0 a path nothing had materialized would raise `ValueError` rather than prove anything,
    and one that validated the tree AFTER moving it would leave the failure path unable to clean
    up; the lock is therefore read inside the seam."""
    seen: dict = {}
    request_id = _request(catalog, request_id="req-config")
    work_items = [_authored(catalog, monkeypatch, suffix="config")]

    def _capture(root, **kwargs):
        lock = pathlib.Path(root) / GENERATED_LOCK_FILENAME
        seen.update(kwargs, root=root, sealed_here=lock.is_file(),
                    hash_on_disk=read_lock(lock.read_text()).generated_project_hash)
        return _verdict(root, generation_id=kwargs["generation_id"],
                        environment_id=kwargs["environment_id"], report_id=kwargs["report_id"],
                        clock=kwargs["clock"])

    monkeypatch.setattr(chain, "run_l0", _capture)
    configured = L0Interpreter(python_executable="/opt/l0/bin/python", timeout_seconds=17.5,
                               env={"PYSPARK_PYTHON": "/opt/l0/bin/python"})

    outcome = _run(catalog, request_id, work_items, tmp_path, l0=configured)

    assert seen["python_executable"] == "/opt/l0/bin/python"
    assert seen["timeout_seconds"] == 17.5
    assert seen["env"] == {"PYSPARK_PYTHON": "/opt/l0/bin/python"}
    assert seen["environment_id"] == INVENTORY.environment_id
    assert seen["generation_id"] == outcome.generation_id
    assert seen["sealed_here"] is True
    assert seen["hash_on_disk"] == outcome.generated_project_hash
    # …and the tree L0 proved is the one the record names, moved into place afterwards.
    assert pathlib.Path(seen["root"]) != pathlib.Path(outcome.project_root)
    assert read_lock((pathlib.Path(outcome.project_root) / GENERATED_LOCK_FILENAME).read_text()
                     ).generated_project_hash == seen["hash_on_disk"]


def test_the_chain_calls_the_REAL_run_l0_and_an_interpreter_without_kedro_proves_NOTHING(
        catalog, monkeypatch, tmp_path) -> None:
    """Nothing is injected here: the chain launches the REAL `run_l0` against `sys.executable`,
    which genuinely does not have kedro, over the REAL rendered project.

    That is the whole point — it proves the chain's L0 call is the module's own function and not a
    seam every test replaces, and it proves a run whose build was not proven cannot reach the
    success-shaped terminal. It needs no venv, which is why it belongs in the collected suite;
    proving that the project DOES build under a real kedro is `l0_gate.py`'s job."""
    if importlib.util.find_spec("kedro") is not None:
        pytest.skip("this interpreter HAS kedro; the subject of this test is one that does not")
    request_id = _request(catalog, request_id="req-real-l0")
    work_items = [_authored(catalog, monkeypatch, suffix="reall0")]

    outcome = _run(catalog, request_id, work_items, tmp_path)

    assert chain.run_l0.__module__ == "featuregen.materialize.validation"
    assert outcome.stopped_at is ChainStage.VALIDATE_L0
    assert outcome.terminal_event is RunEventKind.RUN_FAILED
    assert outcome.validation_report.status is ValidationStatus.FAILED
    assert [f.code for f in outcome.validation_report.findings] == \
        [ValidationFindingCode.PROJECT_DOES_NOT_BUILD]
    assert outcome.validation_report.findings[0].classification is FindingClass.RENDERER_DEFECT


def test_a_replayed_run_reports_the_RECORDED_build_proof(catalog, monkeypatch, tmp_path) -> None:
    """`stopped_at` and `refusal` stay `None` on a replay because neither is durable — the report
    IS, so reading it back is a reading rather than a reconstruction. A re-delivered queue job that
    lost the build proof would invite a caller to re-drive a run whose project never built."""
    request_id = _request(catalog, request_id="req-replay-l0")
    work_items = [_authored(catalog, monkeypatch, suffix="replayl0")]
    _inject_l0(monkeypatch, status=ValidationStatus.FAILED, findings=(
        ValidationFinding(code=ValidationFindingCode.PROJECT_DOES_NOT_BUILD, location="p",
                          expected="importable", observed="SyntaxError", count=1),))
    first = _run(catalog, request_id, work_items, tmp_path)

    again = _run(catalog, request_id, work_items, tmp_path)

    assert again.replayed is True
    assert again.stopped_at is None
    assert again.terminal_event is RunEventKind.RUN_FAILED
    assert again.validation_report == first.validation_report


def test_a_run_refused_before_the_project_exists_carries_NO_build_proof(
        catalog, monkeypatch, tmp_path) -> None:
    """A refusal before rendering has no project to validate, and a report is a claim about one."""
    request_id = _request(catalog, request_id="req-nospine-l0")
    work_item_id = _authored(catalog, monkeypatch, suffix="nospinel0")

    outcome = _run(catalog, request_id, [work_item_id], tmp_path, spine=None)

    assert outcome.stopped_at is ChainStage.COMPILE
    assert outcome.validation_report is None
    assert catalog.execute("SELECT count(*) FROM pipeline_validation_report").fetchone()[0] == 0


@pytest.mark.parametrize("injected",
                         ["record_compiled_artifact", "append_run_event", "materialize_to",
                          "record_validation_report"])
def test_the_commit_is_ALL_OR_NOTHING(ready, catalog, monkeypatch, injected) -> None:
    """The commit block must be atomic, not merely ordered — and on the runtime this chain will be
    driven from, ordering alone is not enough.

    `runtime/worker.py:638` and `runtime/dispatch.py:79` make the handler connection AUTOCOMMIT by
    contract, so without an explicit transaction each write below would land on its own. The three
    injection points are the real failure modes: a mid-block refusal (the `group_binding` unique
    violation and the artifact table's own primary key both have this shape) and a failed TREE
    write, which is the dangerous one — it would otherwise leave the plane holding a committed
    request, a generation carrying a `generated_project_hash`, and an UNRETRACTABLE terminal event
    for a project that was never written. 1044's one-terminal trigger means nothing could ever
    supersede it.

    `record_compiled_artifact` is injected as well as read back elsewhere because it is a WRITE
    ADDED to an existing atomic block: a writer that opened its own transaction (or committed on
    the autocommit connection) would leave a compiled artifact for a generation that was rolled
    back — an FK-orphan claim that a compile happened, on the one table nothing can delete from.

    `record_validation_report` is the same hazard one step later, and its table is append-only too
    (1034's guard): a build proof that survived a rolled-back generation would be a verdict about a
    project the record does not contain. It is also the injection that pins the tree ORDER — L0
    needs a materialized project, so the tree is now written before the terminal is chosen, and it
    is written to the sibling and moved into place LAST so that a failure here still leaves nothing
    at the project's own path."""
    request_id, work_items, root = ready
    monkeypatch.setattr(chain, injected,
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("write refused")))

    with pytest.raises(RuntimeError, match="write refused"):
        _run(catalog, request_id, work_items, root)

    assert catalog.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 0
    assert catalog.execute("SELECT count(*) FROM materialization_run_event").fetchone()[0] == 0
    assert catalog.execute("SELECT count(*) FROM group_binding").fetchone()[0] == 0
    assert catalog.execute(
        "SELECT count(*) FROM materialization_compiled_artifact").fetchone()[0] == 0
    assert catalog.execute("SELECT count(*) FROM pipeline_validation_report").fetchone()[0] == 0
    assert read_request(catalog, request_id=request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED
    assert not list(pathlib.Path(root).iterdir())


def test_a_half_written_tree_never_appears_at_the_projects_own_path(ready, catalog,
                                                                    monkeypatch) -> None:
    """The filesystem takes no part in the transaction, so the tree is written to a sibling and
    moved into place atomically. It matters more than litter: `generation_id` is a pure function of
    the request id, so `project_root/<generation_id>` never changes for this request, and
    `materialize_to` refuses a non-empty directory — a partial tree left there would poison every
    re-drive of that request forever."""
    request_id, work_items, root = ready
    genuine = chain.materialize_to

    def _half_write(project, target):
        genuine(project, target)
        raise RuntimeError("died after writing")

    monkeypatch.setattr(chain, "materialize_to", _half_write)

    with pytest.raises(RuntimeError, match="died after writing"):
        _run(catalog, request_id, work_items, root)

    assert not list(pathlib.Path(root).iterdir())


# ── truthfulness: the chain may never claim publication ──────────────────────────────────────────

def _is_run_event_kind(node: ast.expr) -> bool:
    return isinstance(node, ast.Name) and node.id == "RunEventKind"


def _code_strings(tree: ast.AST) -> set[str]:
    """Every string constant in the module's CODE. Docstrings are excluded — prose must be free to
    name what the code may not, and this module's docstrings say `PUBLISHED` repeatedly in order to
    forbid it."""
    docstrings = {
        id(node.body[0].value) for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)}
    return {node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings}


def test_the_chain_can_never_append_PUBLISHED() -> None:
    """THE test that stops a future well-meaning change from lying.

    Read off the AST rather than the behaviour: a behavioural assertion only proves that today's
    paths do not claim publication, and the whole hazard (plan §3.5) is a path added later.

    It closes FOUR routes to the same unretractable write, not one. The attribute route is the
    obvious one. The STRING route is the likeliest "quick fix" and is the same write —
    `MaterializationRunEvent.__post_init__` COERCES `event_kind` (`control_plane.py:238`), so
    `event_kind="PUBLISHED"` produces the real terminal member and a guard that only inspected
    attribute access would stay green while the plane recorded a lie. Subscript, call and `getattr`
    are the three ways to reach a member through a name the AST cannot resolve.

    Task 6 widened the permitted set by exactly ONE — `RUN_FAILED`, the terminal a run whose build
    was not proven ends on. The assertion stays an EQUALITY over the whole set rather than a
    "`PUBLISHED` is absent" check, so every future widening is a deliberate edit here.
    """
    tree = ast.parse(inspect.getsource(chain))
    members = {member.name for member in RunEventKind}
    permitted = {"PUBLICATION_REFUSED", "RUN_FAILED"}

    attributes = {node.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Attribute) and _is_run_event_kind(node.value)}
    assert attributes == permitted

    assert _code_strings(tree) & members <= permitted

    indirect = [
        ast.dump(node) for node in ast.walk(tree)
        if (isinstance(node, ast.Subscript) and _is_run_event_kind(node.value))
        or (isinstance(node, ast.Call) and _is_run_event_kind(node.func))
        or (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "getattr" and node.args and _is_run_event_kind(node.args[0]))]
    assert indirect == []


def test_no_path_records_a_published_generation(ready, catalog) -> None:
    """The behavioural half. `published_generation_ids` is what §10.1's "current plan" derivation
    reads, so a generation that appeared there would become a group's current plan on the strength
    of a publication that never happened."""
    request_id, work_items, root = ready

    _run(catalog, request_id, work_items, root)

    assert published_generation_ids(catalog) == frozenset()


def test_a_PROVEN_capability_stops_the_chain_rather_than_letting_it_claim_a_publish(
        catalog, monkeypatch, tmp_path) -> None:
    """The one branch that could produce a lie. If an attestation exists, `select_publisher` returns
    a selection — and there is no publish step to honour it (plan §3.5, DEFERRED A.26). Rendering a
    publishing catalog entry for a project nothing will ever publish, and then recording either a
    terminal that did not happen or no terminal at all, are both worse than stopping."""
    request_id = _request(catalog, request_id="req-proven")
    work_items = [_authored(catalog, monkeypatch, suffix="proven")]
    _attest_capability(catalog)

    with pytest.raises(chain.PublishStepMissing, match="publish step"):
        _run(catalog, request_id, work_items, tmp_path)

    assert not list(tmp_path.iterdir())


def _attest_capability(db) -> None:
    """A PASSING attestation that also covers schema evolution, for this environment, mechanism and
    engine versions — ingested through the only door there is (`record_attestation` accepts nothing
    but a probe result, and every field of that result is derived from the observations)."""
    observations = tuple(
        ProbeObservation(reader_id=f"reader-{index}", observed_at=f"2026-08-03T10:00:0{index}+00:00",
                         generation_id=generation, column_names=columns, row_count=7,
                         content_digest=f"digest-{generation}")
        for index, (generation, columns) in enumerate((
            ("g1", ("cif_id",)), ("g1", ("cif_id",)),
            ("g2", ("cif_id", "total_debit_amount_30d")),
            ("g2", ("cif_id", "total_debit_amount_30d")))))
    result = assess_probe_observations(
        observations, probe_id="probe-1", environment_id=INVENTORY.environment_id,
        mechanism=PublishMechanism.VERSIONED_POINTER,
        engine_versions=INVENTORY.engine_versions,
        completed_at="2026-08-03T10:00:09+00:00")
    assert result.passed and result.covers_schema_evolution, result
    record_attestation(db, result)


# ── one test per refusing stage: it stops THERE, records the code, leaves nothing behind ─────────

def _assert_nothing_was_written(db, root, request_id) -> None:
    """A refusal before a generation exists must leave the plane empty and the disk untouched.

    Not a redundant trio: the generation row is what every plane record hangs off (a run event
    cannot exist without one — migration 1034's FK), the request advance is the ONLY durable trace
    such a run leaves, and an orphan directory would be a project no record names."""
    assert db.execute("SELECT count(*) FROM materialization_generation").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM materialization_run_event").fetchone()[0] == 0
    assert read_request(db, request_id=request_id).lifecycle_state is RequestLifecycle.FAILED
    assert not list(pathlib.Path(root).iterdir())


def test_an_unresolvable_member_stops_the_chain_at_RESOLVE(catalog, monkeypatch,
                                                           tmp_path) -> None:
    """Gate 1's first half. The work item is real and its authoring run closed NEEDS_REVIEW, so
    there is no admissible artifact — and the seam names the member, which admission cannot."""
    from featuregen.formula.critic import CriticFinding, CriticFindingCode

    request_id = _request(catalog, request_id="req-review")
    work_item_id = _seed_work_item(catalog, _FEATURE, "review")
    _author_the_run(catalog, monkeypatch, work_item_id, _FEATURE, findings=(
        CriticFinding(code=CriticFindingCode.WINDOW_INTENT_MISMATCH, severity="blocking",
                      operand=None, detail="30d asked, 90d proposed"),))

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.stopped_at is ChainStage.RESOLVE
    assert outcome.refusal.code is CompilationRefusalCode.NOT_RESOLVED
    assert outcome.terminal_event is None
    assert outcome.generation_id is None
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_an_admission_refusal_is_recorded_at_ADMIT(catalog, monkeypatch, tmp_path) -> None:
    """Gate 1's second half, and the refusal is INJECTED — which is itself the finding.

    ``admit_artifacts`` RAISES its six §1.2 verdicts, and this chain must record and return them
    rather than let them escape as an exception. End to end they are unreachable: the resolution
    seam ahead of it reads the SAME write-once 1022 row and `load_verified_checkpoint` re-derives
    every payload hash while restoring, so evidence admission would refuse cannot survive the
    restore. The branch is real regardless — `admit_artifacts` is the governed gate, and a caller
    that hands it an object from anywhere else reaches those checks."""
    request_id = _request(catalog, request_id="req-admit")
    work_item_id = _authored(catalog, monkeypatch, suffix="admit")
    refusal = MaterializationRefused(CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED, "not mine")

    def _raise(*_args, **_kwargs):
        raise refusal

    monkeypatch.setattr(chain, "admit_artifacts", _raise)

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.stopped_at is ChainStage.ADMIT
    assert outcome.refusal is refusal
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_an_undeclared_spine_stops_the_chain_at_COMPILE(catalog, monkeypatch, tmp_path) -> None:
    """§2's first check, and the only per-FEATURE refusal a group can be stopped by. A group with
    no attested population has nothing for its features to land on, so it is answered first."""
    request_id = _request(catalog, request_id="req-nospine")
    work_item_id = _authored(catalog, monkeypatch, suffix="nospine")

    outcome = _run(catalog, request_id, [work_item_id], tmp_path, spine=None)

    assert outcome.stopped_at is ChainStage.COMPILE
    assert outcome.refusal.code is CompilationRefusalCode.SPINE_SOURCE_NOT_DECLARED
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_a_denied_read_stops_the_chain_at_GATE_2(catalog, monkeypatch, tmp_path) -> None:
    """§1.3 is decided over the GROUP's union, and one denied element refuses the whole
    compilation — so no contract, no plan and no project may exist afterwards."""
    request_id = _request(catalog, request_id="req-denied")
    work_item_id = _authored(catalog, monkeypatch, suffix="denied")
    _tag(catalog, "transactions", "dr_cr_flag", "pii")

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.stopped_at is ChainStage.AUTHORIZE
    assert outcome.refusal.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_an_unpublishable_column_type_stops_the_chain_at_PHYSICAL_TYPE(
        catalog, monkeypatch, tmp_path) -> None:
    """§6 refuses `half_even` rounding on a RATIO (DEFERRED A.28): Spark's decimal division rounds
    HALF_UP at the result scale before any explicit rounding call runs, so a declared `half_even` is
    unenforceable. The shipped ratio fixture declares `half_up` for exactly that reason; this run
    authors the mode the engine will not apply — genuinely, through the real orchestrator — and the
    refusal arrives from materialize rather than from authoring."""
    from tests.featuregen.materialize import test_resolve as authoring

    name = "cross_border_value_ratio_90d"
    request_id = _request(catalog, request_id="req-ratio")
    work_item_id = _seed_work_item(catalog, name, "ratio")
    unenforceable = fixtures.raw_proposal(name)
    unenforceable["decimal"]["rounding"] = "half_even"
    monkeypatch.setattr(authoring, "raw_proposal", lambda _name: unenforceable)
    _author_the_run(catalog, monkeypatch, work_item_id, name)

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.stopped_at is ChainStage.PHYSICAL_TYPE
    assert outcome.refusal.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_a_spine_the_inventory_never_captured_stops_the_chain_at_SPINE_INPUT(
        catalog, monkeypatch, tmp_path) -> None:
    """§3.4's "absent is not unpartitioned". The declared population's table is governed and the
    features compile against it, but the environment was never captured holding it — so its
    physical input cannot be resolved and no project may be rendered for the group."""
    request_id = _request(catalog, request_id="req-nospinetable")
    work_item_id = _authored(catalog, monkeypatch, suffix="nospinetable")
    without_customers = dataclasses.replace(INVENTORY, tables={
        name: layout for name, layout in INVENTORY.tables.items() if name != "banking.customers"})

    outcome = _run(catalog, request_id, [work_item_id], tmp_path, inventory=without_customers)

    assert outcome.stopped_at is ChainStage.SPINE_INPUT
    assert outcome.refusal.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_a_prohibited_input_stops_the_chain_at_CONTRACT(catalog, monkeypatch, tmp_path) -> None:
    """§5.2's classification refuses the contract. Driven through the caller-supplied override —
    tightening all the way to the top is a legal declaration and an illegal artifact."""
    request_id = _request(catalog, request_id="req-prohibited")
    work_item_id = _authored(catalog, monkeypatch, suffix="prohibited")

    outcome = _run(catalog, request_id, [work_item_id], tmp_path,
                   overrides=ContractOverrides(sensitivity_class="prohibited"))

    assert outcome.stopped_at is ChainStage.CONTRACT
    assert outcome.refusal.code is CompilationRefusalCode.PROHIBITED_INPUT
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_a_bound_name_that_resolves_to_another_contract_stops_the_chain_at_BIND(
        catalog, monkeypatch, tmp_path) -> None:
    """§10.1's one refusal, and the only one that says "do not render for this name AT ALL" — so
    this is also the test that proves the chain does not render before it has bound."""
    request_id = _request(catalog, request_id="req-bound")
    work_item_id = _authored(catalog, monkeypatch, suffix="bound")
    catalog.execute(
        "INSERT INTO group_binding (binding_id, logical_group_name, "
        "materialization_contract_hash, physical_target) VALUES "
        "('gb-other', %s, 'a-contract-this-group-does-not-carry', %s)",
        (_GROUP, f"sandbox_feature.{_GROUP}"))

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.stopped_at is ChainStage.BIND
    assert outcome.refusal.code is PublicationRefusalCode.GROUP_BINDING_CONFLICT
    _assert_nothing_was_written(catalog, tmp_path, request_id)


def test_the_recorded_refusal_is_the_stages_OWN_object_never_a_re_typed_copy(
        catalog, monkeypatch, tmp_path) -> None:
    """A governed verdict is evidence. Re-raising it as a new object with a chosen code is how a
    chain quietly reclassifies one stage's refusal as another's, so the returned refusal must be
    the very object the stage produced."""
    from featuregen.materialize import ir as ir_module

    request_id = _request(catalog, request_id="req-identity")
    work_item_id = _authored(catalog, monkeypatch, suffix="identity")
    refusal = MaterializationRefused(CompilationRefusalCode.COLUMN_NOT_GOVERNED, "the very object")
    monkeypatch.setattr(chain, "authorize_compilation", lambda *a, **k: refusal)
    assert ir_module.authorize_compilation is not None            # the real one still exists

    outcome = _run(catalog, request_id, [work_item_id], tmp_path)

    assert outcome.refusal is refusal


# ── call-assembly errors are NOT refusals, and must propagate ────────────────────────────────────

def test_two_features_that_normalize_to_one_column_RAISE_rather_than_refuse(
        catalog, monkeypatch, tmp_path) -> None:
    """`FeatureNamePlanError` is a plan error, not a §14 verdict (`admission.py:131`). Catching it
    and reporting a governed code would tell an operator the catalog refused their feature when in
    fact the request named one column twice."""
    request_id = _request(catalog, request_id="req-collide")
    # Two DIFFERENT durable identities — two work items, two authoring runs — naming one recipe.
    # Both resolve and both are genuine; it is the GROUP that is malformed.
    members = [_authored(catalog, monkeypatch, suffix="c1"),
               _authored(catalog, monkeypatch, suffix="c2")]

    with pytest.raises(FeatureNamePlanError):
        _run(catalog, request_id, members, tmp_path)

    assert not list(tmp_path.iterdir())
    assert read_request(catalog, request_id=request_id).lifecycle_state is \
        RequestLifecycle.ACCEPTED


def test_an_empty_group_is_a_caller_error_not_a_refusal(catalog, tmp_path) -> None:
    request_id = _request(catalog, request_id="req-empty")

    with pytest.raises(ValueError, match="at least one work item"):
        _run(catalog, request_id, [], tmp_path)


def test_gate_2_authorizes_under_the_REQUESTS_OWN_role_snapshot(catalog, monkeypatch, l0_passes,
                                                                tmp_path) -> None:
    """The roles are not a parameter, so this is the only thing that can decide Gate 2: the
    snapshot taken when the request was recorded (`request_store.py:182-184` — a run is judged
    against the scope its requester actually held). The same catalog and the same feature refuse
    or authorize purely on what the REQUEST says, which is what proves the snapshot is read."""
    _tag(catalog, "transactions", "dr_cr_flag", "pii")
    denied = _request(catalog, request_id="req-roles-denied", roles=_ROLES)
    allowed = _request(catalog, request_id="req-roles-allowed", roles=(*_ROLES, "pii_reader"))
    items = [_authored(catalog, monkeypatch, suffix="roles")]

    refused = _run(catalog, denied, items, tmp_path / "denied")

    assert refused.stopped_at is ChainStage.AUTHORIZE
    assert refused.refusal.code is CompilationRefusalCode.READ_SCOPE_INSUFFICIENT
    assert _run(catalog, allowed, items, tmp_path / "allowed").stopped_at is ChainStage.PUBLISHER


def test_a_request_nobody_recorded_is_a_caller_error(catalog, monkeypatch, tmp_path) -> None:
    work_item_id = _authored(catalog, monkeypatch, suffix="ghost")

    with pytest.raises(ValueError, match="req-never-recorded"):
        _run(catalog, "req-never-recorded", [work_item_id], tmp_path)


def test_an_unaccepted_request_is_a_caller_error(catalog, monkeypatch, tmp_path) -> None:
    """The lease is the single-writer guarantee for an append-only event stream, and it is granted
    by `accept_request`. Compiling for a request nobody claimed would put two writers on one run."""
    record_request(catalog, request_id="req-unclaimed", logical_group_name=_GROUP,
                   requested_by=_ACTOR, authorized_roles=_ROLES, idempotency_key="key-unclaimed",
                   activation_state={})
    work_item_id = _authored(catalog, monkeypatch, suffix="unclaimed")

    with pytest.raises(ValueError, match="requested"):
        _run(catalog, "req-unclaimed", [work_item_id], tmp_path)


# ── idempotency ──────────────────────────────────────────────────────────────────────────────────

def test_the_same_request_twice_yields_ONE_generation_and_ONE_terminal_event(
        ready, catalog) -> None:
    """The plane is append-only with a one-terminal partial index: a second terminal event is a
    `UniqueViolation` with no repair path, so re-entry must not reach the append at all."""
    request_id, work_items, root = ready
    first = _run(catalog, request_id, work_items, root)

    again = _run(catalog, request_id, work_items, root)

    assert again.replayed is True
    assert again.generation_id == first.generation_id
    assert again.run_id == first.run_id
    assert again.terminal_event is RunEventKind.PUBLICATION_REFUSED
    assert catalog.execute(
        "SELECT count(*) FROM materialization_generation").fetchone()[0] == 1
    assert len(read_run_events(catalog, first.run_id)) == 1


def test_a_replayed_result_does_not_invent_a_stage_it_cannot_know(ready, catalog) -> None:
    """`stopped_at` is not durable. Reporting one on replay would be an invention about a decision
    nothing recorded — and the caller has `replayed` to tell the two apart."""
    request_id, work_items, root = ready
    _run(catalog, request_id, work_items, root)

    again = _run(catalog, request_id, work_items, root)

    assert again.stopped_at is None
    assert again.refusal is None
    assert again.lifecycle_state is RequestLifecycle.COMMITTED


def test_a_refused_request_replays_without_re_running_the_chain(catalog, monkeypatch,
                                                                tmp_path) -> None:
    """A request that failed before a generation existed is terminal too: it must not be re-driven
    into a second compilation under the same durable identity."""
    request_id = _request(catalog, request_id="req-replay-failed")
    work_item_id = _seed_work_item(catalog, _FEATURE, "replayfail")

    first = _run(catalog, request_id, [work_item_id], tmp_path)
    assert first.stopped_at is ChainStage.RESOLVE

    again = _run(catalog, request_id, [work_item_id], tmp_path)

    assert again.replayed is True
    assert again.lifecycle_state is RequestLifecycle.FAILED
    assert again.generation_id is None


# ── the ids are DERIVED from the request, which is what makes re-entry safe ──────────────────────

def test_the_generation_and_run_ids_are_stamped_on_the_request(ready, catalog) -> None:
    """Which compilation a request became, and which run carried it, are answers with exactly one
    value — and `advance_lifecycle` stamps both write-once, so this is what a reconciler reads."""
    request_id, work_items, root = ready

    outcome = _run(catalog, request_id, work_items, root)

    stored = read_request(catalog, request_id=request_id)
    assert stored.generation_id == outcome.generation_id
    assert stored.run_id == outcome.run_id
    assert outcome.generation_id != outcome.run_id


def test_the_derived_generation_id_is_the_SECOND_line_of_defence(ready, catalog) -> None:
    """The ids are DERIVED from the request, not minted, and this is why that matters.

    The replay short-circuit is what normally stops a re-entry, but it is one branch in one
    function. Because the id is a pure function of the request, a compilation that got past it
    would collide on the primary key rather than quietly record a second generation for one
    request — which on an append-only plane with no repair path is the difference between a loud
    failure and a permanently ambiguous record."""
    request_id, work_items, root = ready
    outcome = _run(catalog, request_id, work_items, root)

    assert chain._generation_id(request_id) == outcome.generation_id
    with pytest.raises(psycopg.errors.UniqueViolation):
        catalog.execute(
            "INSERT INTO materialization_generation (generation_id, logical_group_name, "
            "materialization_contract_hash, group_plan_hash, generated_project_hash, created_at) "
            "VALUES (%s, 'other', 'c', 'p', 'g', '2026-08-03T12:00:00+00:00')",
            (chain._generation_id(request_id),))
