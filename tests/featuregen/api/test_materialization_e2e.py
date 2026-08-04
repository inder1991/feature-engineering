"""Phase G T11 — THE ACCEPTANCE TEST: a governed feature becomes a build-verified project.

Every task before this one shipped a component with its own suite. **Nothing proved they compose.**
This is the one file that drives the whole of G-1 through the surfaces a deployment actually has:

    POST /materialization-runs  ->  queue row  ->  the worker's own tick  ->  the fenced lane
    ->  resolve -> Gate 1 -> compile -> Gate 2 -> plan -> bind -> publisher -> render -> seal
    ->  L0  ->  the control plane  ->  GET /materialization-runs/{id}

**The plan's §5 criterion, and G-1's honest version of it.** §5 asks for "trigger -> generated
project -> validated -> (gated) submitted -> published". Submission is G-2 and publication does not
exist (plan §3.5), so the successful outcome asserted here is the terminal ``PUBLICATION_REFUSED``
carrying ``CAPABILITY_UNPROVEN`` — a compilation that was sound, an artifact that was sealed, a build
that was PROVED, and a publication the platform refuses to claim. The test asserts that terminal as
success and separately asserts that ``PUBLISHED`` was never appended anywhere.

**What is real, and what is not.** The catalog is seeded through Task 2/3's fixtures (``build_graph``
plus the governed field-resolution lifecycle); the feature's authoring run is written by the REAL
1022 orchestrator; the trigger is the shipped FastAPI route through ``TestClient``; the drain is
``run_worker_once`` — the production worker stage, reading Task 9's flag and resolving Task 7's lane
configuration from the ENVIRONMENT (``lane_config_from_env``), not from an injected config object;
every store is the real one. **The single injection is the L0 seam** (``chain.run_l0``, exactly as
Tasks 6-9's suites inject it) so that this suite needs no kedro/pyspark venv — and even that seam is
not free-floating: :func:`~tests.featuregen.materialize.test_chain._verdict` reads the sealed lock off
the tree it is handed, so a chain that never wrote a project could not obtain a verdict at all. And
the artifact's identity is re-derived HERE from the bytes on disk (``generated_project_hash`` over
the walked tree), so the seam cannot be the only thing that ever looks at the project.

**Three harness substitutions are INHERITED from ``tests/featuregen/api/conftest.py``, and they are
named so that "nothing else is injected" is precise rather than a slogan.** ``FEATUREGEN_AUTH_STUB=1``
enables the ``X-User``/``X-Roles`` header identity — OFF in production, where only a Bearer session
authenticates — which is what lets a test state the role claims ``record_request`` freezes and Gate 2
is then judged against; and ``get_conn``/``get_feature_gen_conn`` are overridden to yield the suite's
one rolled-back connection, so the route, the worker tick and the assertions all read a single
transaction. Neither substitutes a materialization component: the routes, the lane, the chain, the
stores and the renderers below them are the shipped ones.

**Why the three probes are here.** A green happy path proves the flow reaches a terminal; it does not
prove any particular gate was consulted on the way. Three cases make the green non-vacuous, and each
is the permanent form of one row of the task's mutation table:

* a member whose authoring run never happened -> the run ends at ``resolve`` and the plane stays
  EMPTY (a governed refusal before the seal records nothing but the request's own lifecycle);
* one column tagged ``pii`` against a requester holding no ``pii_reader`` -> Gate 2 refuses the whole
  group, and no contract, plan, binding, artifact or project exists afterwards;
* a deployment that configures NO L0 interpreter -> the project is still sealed on disk, and the run
  terminates ``RUN_FAILED`` with the build unproven. The injected L0 verdict is still installed in
  that test and is never consulted, which is what shows the happy path's ``PASSED`` comes from the
  chain asking, rather than from reaching the end of the chain.

**The two drains, and why they differ.** The acceptance test uses ``run_worker_once`` because the
tick is what a deployment runs. The refusal probes call ``process_materialization_once`` — the same
consumer the stage calls, one level down — because a governed refusal BEFORE the seal has nowhere
durable to record its stage or its code (``materialization_run_event.generation_id`` is a foreign key
to a generation that does not exist yet), so the lane's returned outcome is the only place the stage
and the code can be read. That is a finding about the record, not a preference about harnesses, and
it is stated in the task report.
"""
from __future__ import annotations

import dataclasses
import datetime as _datetime
import pathlib
import sys
from typing import Any

import pytest
import yaml
from tests.featuregen.api.test_materialization_runs import _body
from tests.featuregen.materialize.fixtures import authored_formula
from tests.featuregen.materialize.test_chain import (
    _FEATURE,
    _GROUP,
    _authored,
    _inject_l0,
    _seed,
)
from tests.featuregen.materialize.test_ir import INVENTORY, _tag
from tests.featuregen.materialize.test_resolve import (  # noqa: F401 — `no_dsn` is autouse
    _seed_work_item,
    no_dsn,
)

from featuregen.api.routes.materialization_runs import REQUEST_ID_HEADER
from featuregen.formula.canonical import formula_content_hash
from featuregen.materialize.codes import (
    CompilationRefusalCode,
    PublicationRefusalCode,
)
from featuregen.materialize.compile.chain import FIRST_RUN_EVENT_SEQ, ChainStage
from featuregen.materialize.control_plane import (
    RunEventKind,
    RunStatus,
    fold_run_status,
    published_generation_ids,
    read_compiled_artifact,
    read_group_binding,
    read_plan_revisions,
    read_run_events,
)
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    generated_project_hash,
    read_lock,
)
from featuregen.materialize.inventory import ClusterInventoryV1, load_inventory
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    MATERIALIZATION_HANDLER,
    process_materialization_once,
)
from featuregen.materialize.request_store import (
    MaterializationRequestV1,
    RequestLifecycle,
    read_request,
)
from featuregen.materialize.validation import (
    ValidationLevel,
    ValidationStatus,
    read_validation_reports,
)
from featuregen.runtime.handlers import HandlerRegistry
from featuregen.runtime.observability import counters
from featuregen.runtime.worker import WorkerTick, run_worker_once

_PATH = "/materialization-runs"
_NOW = _datetime.datetime(2026, 8, 4, 12, 0, tzinfo=_datetime.UTC)
_PACKAGE = f"sandbox_feature_{_GROUP}"

#: The lane's four environment variables. Named once so a test that unsets one is unmistakable.
_PROJECT_ROOT_ENV = "FEATUREGEN_MATERIALIZE_PROJECT_ROOT"
_INVENTORY_ENV = "FEATUREGEN_MATERIALIZE_INVENTORY"
_L0_PYTHON_ENV = "FEATUREGEN_MATERIALIZE_L0_PYTHON"
_L0_TIMEOUT_ENV = "FEATUREGEN_MATERIALIZE_L0_TIMEOUT_SECONDS"

#: Every table a compile writes. Asserted as a whole rather than one count at a time, so a stage that
#: started writing a record the trigger must not produce is caught by name rather than by silence.
_PLANE_TABLES = (
    "materialization_generation",
    "materialization_run_event",
    "materialization_compiled_artifact",
    "pipeline_validation_report",
    "group_binding",
    "group_plan_revision",
)

_EMPTY_PLANE = dict.fromkeys(_PLANE_TABLES, 0)


# ── the deployment: the environment a worker reads, and the inventory file it points at ──────────


def _inventory_document(inventory: ClusterInventoryV1) -> dict[str, Any]:
    """The suite's typed inventory as the YAML document ``load_inventory`` reads.

    Written from the object rather than hand-authored beside it: the partition mapping is serialized
    through its OWN ``identity_payload`` (the same mapping the loader's field readers consume), so a
    variant that gains a field cannot leave this fixture describing an environment nobody declared.
    :func:`test_the_deployments_inventory_file_IS_the_suites_inventory` pins the round trip.
    """
    return {
        "environment_id": inventory.environment_id,
        "captured_at": inventory.captured_at,
        "engine_versions": dataclasses.asdict(inventory.engine_versions),
        "logical_schema_map": dict(inventory.logical_schema_map),
        "tables": {
            key: {
                "partition_columns": (
                    None if layout.partition_columns is None
                    else [[name, kind] for name, kind in layout.partition_columns]),
                "partition_mapping": (
                    None if layout.partition_mapping is None
                    else layout.partition_mapping.identity_payload()),
                "columns": [[name, kind] for name, kind in layout.columns],
                "location": layout.location,
                "rewritten_in_place": layout.rewritten_in_place,
            }
            for key, layout in inventory.tables.items()
        },
    }


def _inventory_file(directory: pathlib.Path) -> pathlib.Path:
    path = directory / "hdfc-local-inventory.yml"
    path.write_text(yaml.safe_dump(_inventory_document(INVENTORY), sort_keys=False),
                    encoding="utf-8")
    return path


@pytest.fixture
def deployment(monkeypatch, tmp_path):
    """A deployment that RUNS materialization, configured the way a real one is: environment
    variables. Nothing here is an injected object — ``lane_config_from_env`` resolves all of it, so
    the assembler and the clock the compile uses are the lane's own production defaults.

    Returns the project root, which starts EMPTY so "no project on disk" is a readable directory
    listing rather than the absence of a path that was never created.
    """
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")
    monkeypatch.setenv(_PROJECT_ROOT_ENV, str(root))
    monkeypatch.setenv(_INVENTORY_ENV, str(_inventory_file(tmp_path)))
    monkeypatch.setenv(_L0_PYTHON_ENV, sys.executable)
    monkeypatch.setenv(_L0_TIMEOUT_ENV, "60")
    return root


@pytest.fixture
def l0_passes(monkeypatch):
    """THE ONE INJECTION. Task 6's seam, injected exactly as Tasks 6-9's suites inject it, so no
    test here needs a kedro+pyspark interpreter. The verdict is derived from the sealed lock of the
    tree the chain hands it — a run that rendered nothing could not obtain one."""
    _inject_l0(monkeypatch)


@pytest.fixture
def governed_feature(conn, monkeypatch):
    """The governed catalog and ONE feature whose authoring run the REAL 1022 orchestrator wrote."""
    _seed(conn)
    return _authored(conn, monkeypatch, suffix="e2e")


# ── reading the record ───────────────────────────────────────────────────────────────────────────


def _plane_counts(conn) -> dict[str, int]:
    return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in _PLANE_TABLES}


def _queue_row(conn, request_id: str):
    return conn.execute("SELECT status, handler FROM queue WHERE message_id = %s",
                        (f"materialize:{request_id}",)).fetchone()


def _stored(conn, request_id: str) -> MaterializationRequestV1:
    """The durable request row, or a failure that NAMES the missing row. ``read_request`` returns
    ``None`` for a request nobody recorded, and reading an attribute off that would report the whole
    of this flow's failure as an ``AttributeError`` on the line that happened to touch it first."""
    request = read_request(conn, request_id=request_id)
    assert request is not None, f"no materialization request {request_id!r} was recorded"
    return request


def _stage_errors() -> int:
    """The worker's own count of faults in the materialization stage. Read as a DELTA rather than
    reset, because ``counters`` is a process global and resetting it here would erase whatever the
    rest of the session is counting."""
    return counters.snapshot()["counters"].get("worker.stage_error.materialization", 0)


def _tick(conn) -> WorkerTick:
    """ONE pass of the production worker — the stage that reads the flag and drains the lane."""
    return run_worker_once(conn, HandlerRegistry(), [], owner="e2e", now=_NOW)


def _trigger(client, headers, work_item_ids, *, key: str) -> str:
    """POST the trigger and return the request id, having checked the 202 says what it means."""
    posted = client.post(_PATH, json=_body(work_item_ids, key=key), headers=headers)
    assert posted.status_code == 202, posted.text
    body = posted.json()
    assert body["lifecycle_state"] == RequestLifecycle.REQUESTED.value
    assert body["duplicate"] is False
    assert body["work_item_ids"] == sorted(work_item_ids)
    request_id = str(body["request_id"])
    assert posted.headers[REQUEST_ID_HEADER] == request_id
    return request_id


# ── the deployment's inventory file is the suite's inventory, proved rather than assumed ─────────


def test_the_deployments_inventory_file_IS_the_suites_inventory(tmp_path) -> None:
    """The environment variable points at a YAML file, and a fixture that wrote a DIFFERENT
    environment would compile a different artifact while every assertion below stayed green."""
    assert load_inventory(_inventory_file(tmp_path)) == INVENTORY


# ── THE acceptance test ──────────────────────────────────────────────────────────────────────────


def test_a_governed_feature_becomes_a_BUILD_VERIFIED_project_over_HTTP(
        client, conn, admin_headers, governed_feature, deployment, l0_passes) -> None:
    """G-1, end to end, through the real surfaces — and the record it must leave behind.

    Read as five claims, in order: the trigger PROMISES work and does none of it; one worker tick
    does all of it; the artifact on disk is sealed and carries the identity Gate 1 verified; the
    plane holds the generation, the compiled bodies, the L0 proof and exactly ONE terminal event;
    and the status route answers with that outcome rather than a second one stored somewhere.
    """
    # ── 1. the trigger ────────────────────────────────────────────────────────────────────────────
    request_id = _trigger(client, admin_headers, [governed_feature], key="acceptance-happy")

    # ── 2. NOTHING has been compiled. The route is a producer; the worker is the only compiler. ───
    assert _plane_counts(conn) == _EMPTY_PLANE
    assert _queue_row(conn, request_id) == ("ready", MATERIALIZATION_HANDLER)
    queued = _stored(conn, request_id)
    assert (queued.lifecycle_state, queued.generation_id, queued.run_id) == \
        (RequestLifecycle.REQUESTED, None, None)
    assert list(deployment.iterdir()) == []

    # ── 3. one tick of the production worker ──────────────────────────────────────────────────────
    faults = _stage_errors()
    tick = _tick(conn)
    assert tick.materialization_processed == 1
    assert _stage_errors() == faults, "the materialization stage faulted; the tick swallowed it"

    stored = _stored(conn, request_id)
    assert stored.lifecycle_state is RequestLifecycle.COMMITTED
    assert stored.generation_id is not None
    assert stored.run_id is not None
    assert _queue_row(conn, request_id) == ("done", MATERIALIZATION_HANDLER)

    # ── 4. the artifact: a SEALED project on disk, carrying the identity the gates decided ────────
    project = deployment / stored.generation_id
    assert (project / GENERATED_LOCK_FILENAME).is_file()
    assert (project / "conf" / "base" / "catalog.yml").is_file()
    identity = read_lock((project / GENERATED_LOCK_FILENAME).read_text())

    nodes_py = (project / "src" / _PACKAGE / "pipelines" / "materialize" / "nodes.py").read_text()
    assert "def build_spine(" in nodes_py
    assert f"def calculate_{_FEATURE}(" in nodes_py
    assert "def gate_and_publish(" in nodes_py
    assert "renders this body" not in nodes_py          # the REAL renderers, not a placeholder

    # THE BYTES ARE THE ONES THE LOCK NAMES — re-derived here from the tree, not taken from the lock.
    # Without this the injected L0 seam reads only `GENERATED.lock`, so a `materialize_to` that wrote
    # different bytes in any file outside the four names asserted above would leave the lock, the
    # generation row and the report perfectly self-consistent about an artifact nobody validated.
    # `generated_project_hash` is a pure function over a path->text mapping and EXCLUDES the lock
    # whether or not it is present (`identity.py:321`), so it answers the same value for the files
    # that were sealed and for the complete project on disk. What still needs a real interpreter —
    # that those bytes IMPORT and construct a pipeline — is `l0_gate.py`'s claim, and it makes it.
    assert generated_project_hash({
        path.relative_to(project).as_posix(): path.read_text(encoding="utf-8")
        for path in project.rglob("*") if path.is_file()
    }) == identity.generated_project_hash

    # Gate 1's verified hash IS the artifact's identity: `admit_artifacts` checks the supplied
    # formula's content hash against the immutable authoring trace, and `build_compilation_identity`
    # seals THAT value into the lock. A chain that skipped admission could not produce this tuple.
    assert identity.compilation.formula_content_hashes == \
        (formula_content_hash(authored_formula(_FEATURE)),)
    assert len(identity.compilation.ir_hashes) == 1

    # ── 5. the lineage ────────────────────────────────────────────────────────────────────────────
    generation = conn.execute(
        "SELECT logical_group_name, materialization_contract_hash, group_plan_hash, "
        "generated_project_hash FROM materialization_generation WHERE generation_id = %s",
        (stored.generation_id,)).fetchone()
    assert generation == (_GROUP, identity.compilation.materialization_contract_hash,
                          identity.compilation.group_plan_hash, identity.generated_project_hash)

    # §3.6 / migration 1054: the BODIES behind two of those three hashes. `CompiledArtifactV1`
    # re-derives both digests from what the database returned, so a row that reached this table by
    # any other means cannot be read back at all.
    artifact = read_compiled_artifact(conn, generation_id=stored.generation_id)
    assert artifact is not None, "migration 1054's compile-side evidence was never recorded"
    assert (artifact.group_plan_hash, artifact.contract_hash) == \
        (identity.compilation.group_plan_hash, identity.compilation.materialization_contract_hash)
    assert artifact.group_plan["logical_group_name"] == _GROUP
    assert [feature["column_name"] for feature in artifact.group_plan["features"]] == [_FEATURE]
    assert artifact.group_plan["materialization_contract_hash"] == artifact.contract_hash

    # §11.2's L0 — the durable proof, read back rather than taken from the object this process held.
    (report,) = read_validation_reports(conn, generation_id=stored.generation_id)
    assert (report.level, report.status, report.findings) == \
        (ValidationLevel.L0, ValidationStatus.PASSED, ())
    assert report.generated_project_hash == identity.generated_project_hash
    assert report.group_plan_hash == identity.compilation.group_plan_hash
    assert report.environment_id == INVENTORY.environment_id

    # §10.1's two records: the binding the logical name now resolves to, and this generation's
    # revision of the packing list.
    binding = read_group_binding(conn, _GROUP)
    assert binding is not None
    assert binding.materialization_contract_hash == \
        identity.compilation.materialization_contract_hash
    assert [revision.generation_id for revision in read_plan_revisions(conn, binding.binding_id)] \
        == [stored.generation_id]

    # ── 6. exactly ONE terminal event, and it is the honest one ───────────────────────────────────
    events = read_run_events(conn, stored.run_id)
    assert [(event.seq, event.event_kind, event.generation_id) for event in events] == \
        [(FIRST_RUN_EVENT_SEQ, RunEventKind.PUBLICATION_REFUSED, stored.generation_id)]
    assert (events[0].detail or "").startswith(f"{PublicationRefusalCode.CAPABILITY_UNPROVEN.value}: ")
    assert fold_run_status(events) is RunStatus.REFUSED

    # PUBLISHED was never appended — not for this run, and not anywhere in the plane.
    assert published_generation_ids(conn) == frozenset()
    assert conn.execute(
        "SELECT count(*) FROM materialization_run_event WHERE event_kind = %s",
        (RunEventKind.PUBLISHED.value,)).fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM materialization_run_manifest").fetchone()[0] == 0

    # ── 7. the status route answers with that outcome, folded — never a second stored status ──────
    fetched = client.get(f"{_PATH}/{request_id}", headers=admin_headers)
    assert fetched.status_code == 200, fetched.text
    detail = fetched.json()
    assert detail["lifecycle_state"] == RequestLifecycle.COMMITTED.value
    assert detail["terminal"] is True
    assert detail["run_status"] == RunStatus.REFUSED.value
    assert detail["run_status_reason"] is None
    assert (detail["generation_id"], detail["run_id"]) == (stored.generation_id, stored.run_id)
    assert detail["logical_group_name"] == _GROUP
    assert detail["requested_by"] == "user:priya"
    assert detail["authorized_roles"] == ["platform-admin"]


# ── the probes that keep the green above from being vacuous ──────────────────────────────────────


def test_a_member_whose_authoring_run_never_HAPPENED_ends_the_run_at_RESOLVE(
        client, conn, admin_headers, deployment, l0_passes) -> None:
    """The resolution seam, consulted. The work item is a real ``recipe_formula_shadow_work_item``
    row — the route's pre-flight passes on it — and the authoring run its id DERIVES was never
    opened, so there is no manifest and nothing to restore.

    The second half of the claim is what the plane must NOT hold: a refusal before the project is
    sealed writes no generation, and therefore no run event, no report, no artifact, no binding and
    no revision. The request's own lifecycle is the whole record, and the status route says so.
    """
    work_item_id = _seed_work_item(conn, _FEATURE, "unauthored")

    request_id = _trigger(client, admin_headers, [work_item_id], key="acceptance-resolve")
    outcome = process_materialization_once(conn, owner="e2e:materialize")

    assert outcome.status == "refused"
    assert outcome.stopped_at == ChainStage.RESOLVE.value
    assert (outcome.detail or "").startswith(
        f"{CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE.value}: ")
    assert (outcome.generation_id, outcome.run_id) == (None, None)

    stored = _stored(conn, request_id)
    assert stored.lifecycle_state is RequestLifecycle.FAILED
    assert (stored.generation_id, stored.run_id) == (None, None)
    assert _plane_counts(conn) == _EMPTY_PLANE
    assert list(deployment.iterdir()) == []

    detail = client.get(f"{_PATH}/{request_id}", headers=admin_headers).json()
    assert detail["lifecycle_state"] == RequestLifecycle.FAILED.value
    assert detail["terminal"] is True
    assert detail["run_status"] is None
    assert "records nothing in the control plane" in detail["run_status_reason"]


def test_a_DENIED_READ_ends_the_run_at_GATE_2_and_no_artifact_exists(
        client, conn, admin_headers, governed_feature, deployment, l0_passes) -> None:
    """Gate 2, consulted, under the REQUEST's own role snapshot. One column of the group's complete
    physical read set is tagged ``pii``; the trigger's identity holds ``platform-admin`` and no
    ``pii_reader``, and ``record_request`` froze exactly those claims onto the request.

    Gate 2 is group-wide, so one hidden element refuses the whole compilation — and everything
    downstream of it (the contract, the plan, the binding, the project) must therefore not exist.
    """
    _tag(conn, "transactions", "dr_cr_flag", "pii")

    request_id = _trigger(client, admin_headers, [governed_feature], key="acceptance-gate2")
    outcome = process_materialization_once(conn, owner="e2e:materialize")

    assert outcome.status == "refused"
    assert outcome.stopped_at == ChainStage.AUTHORIZE.value
    assert (outcome.detail or "").startswith(
        f"{CompilationRefusalCode.READ_SCOPE_INSUFFICIENT.value}: ")

    stored = _stored(conn, request_id)
    assert stored.lifecycle_state is RequestLifecycle.FAILED
    assert stored.generation_id is None
    assert _plane_counts(conn) == _EMPTY_PLANE
    assert list(deployment.iterdir()) == []


def test_a_deployment_with_NO_L0_INTERPRETER_never_reaches_the_G1_terminal(
        client, conn, monkeypatch, admin_headers, governed_feature, deployment,
        l0_passes) -> None:
    """The G-1 terminal rests on a BUILD PROOF, not on reaching the end of the chain.

    Everything is identical to the acceptance test except two environment variables: this deployment
    configures no interpreter. The injected L0 verdict is still installed and is never consulted —
    ``_prove_the_build`` constructs the ``error`` report itself when ``l0 is None`` — which is
    exactly what shows the happy path's ``PASSED`` comes from the chain ASKING.

    The project is still sealed on disk and the plane still holds the generation, the compiled
    bodies and the report: a run whose build could not be proved is recorded whole, because the
    artifact is what an operator opens to see why. What changes is the terminal.
    """
    monkeypatch.delenv(_L0_PYTHON_ENV)
    monkeypatch.delenv(_L0_TIMEOUT_ENV)

    request_id = _trigger(client, admin_headers, [governed_feature], key="acceptance-no-l0")
    assert _tick(conn).materialization_processed == 1

    stored = _stored(conn, request_id)
    assert stored.lifecycle_state is RequestLifecycle.FAILED
    assert stored.generation_id is not None and stored.run_id is not None
    assert (deployment / stored.generation_id / GENERATED_LOCK_FILENAME).is_file()

    (report,) = read_validation_reports(conn, generation_id=stored.generation_id)
    assert (report.level, report.status, report.findings) == \
        (ValidationLevel.L0, ValidationStatus.ERROR, ())

    events = read_run_events(conn, stored.run_id)
    assert [event.event_kind for event in events] == [RunEventKind.RUN_FAILED]
    assert "build is UNPROVEN" in (events[0].detail or "")
    assert fold_run_status(events) is RunStatus.FAILED
    assert published_generation_ids(conn) == frozenset()

    detail = client.get(f"{_PATH}/{request_id}", headers=admin_headers).json()
    assert detail["run_status"] == RunStatus.FAILED.value
    assert detail["lifecycle_state"] == RequestLifecycle.FAILED.value
