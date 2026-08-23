"""Spec A Task 12 — §7's complete runnable project, and the things a parse check cannot see.

**A file that parses is not a file that runs.** `ast.parse` proves syntax and nothing else, and
every test below that calls it says so in its own name or docstring. What L0 (§11.2, Task 15) does —
install the project, import it, build the Kedro pipeline object — is the thing that proves the
project works, and it is deliberately a later task. Nothing here may be read as that proof.

**Golden files are only as good as the goldens.** They are committed, but they are the WEAKEST test
in this file, because one changed byte fails them all and tells you nothing about which property
moved. So every property §7 and the plan name is asserted on its own — the catalog's tables, the
publication target, the pins, the hooks, the launch story, the wiring — and the goldens sit on top
as a change detector.

**The catalog assertion is the discriminating one.** The fixture inventory declares
`banking.accounts` and the fixture catalog contains it, and NO worked feature reads it. A renderer
that emitted "every table in the inventory", or "every table in the catalog", passes a laxer test
and fails `test_the_catalog_names_ONLY_the_read_set_and_the_spine`.

Fixtures come from `test_ir` / `test_group_plan` rather than being re-declared: a second definition
of the governed catalog is a second chance to disagree about what was compiled, and a golden
rendered from a group nobody really compiles is a golden of nothing.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import os
import pathlib
import re

import pytest
import yaml
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    CADENCE,
    COUNT_90D,
    GROUP,
    RATIO_90D,
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_ir import (
    CUSTOMERS,
    INVENTORY,
    _admitted,
    _compile,
    _ok,
)

from featuregen.materialize import binding, render
from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.group_plan import PlannedFeature, build_group_plan
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    SealedProject,
    generated_project_hash,
    read_lock,
)
from featuregen.materialize.inputs import derive_requirement
from featuregen.materialize.inventory import EngineVersions
from featuregen.materialize.ir import authorize_compilation, ir_hash
from featuregen.materialize.physical_types import PhysicalType, resolve_physical_type
from featuregen.materialize.publish import PublisherSelection, PublishMechanism
from featuregen.materialize.render.project import (
    PIPELINE_NAME,
    REQUIRED_RUN_PARAMETERS,
    ProjectDatasets,
    RenderedNode,
    _check_wiring,
    materialize_to,
    project_datasets,
    render_project,
)

_ROLES = ("feature_engineer",)
ENVIRONMENT = "hdfc-local"
PACKAGE = f"sandbox_feature_{GROUP}"
SOURCE_ROOT = f"src/{PACKAGE}"
GOLDENS = pathlib.Path(__file__).parent / "goldens" / "cif_daily"

#: §7's file list, verbatim, plus the two conf files the launch story needs. A set rather than a
#: subset check would break the moment a later task adds a file; what must never happen is one of
#: these going missing, which is what a project that "renders" and cannot run looks like.
REQUIRED_FILES = (
    "pyproject.toml",
    "requirements.lock",
    "README.md",
    GENERATED_LOCK_FILENAME,
    "conf/base/catalog.yml",
    "conf/base/parameters.yml",
    "conf/base/logging.yml",
    f"{SOURCE_ROOT}/__init__.py",
    f"{SOURCE_ROOT}/settings.py",
    f"{SOURCE_ROOT}/pipeline_registry.py",
    f"{SOURCE_ROOT}/hooks.py",
    f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/__init__.py",
    f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/nodes.py",
    f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/pipeline.py",
)


# ── the real compilation, and stub nodes that stand in for Tasks 13/14 ───────────────────────────


def _compiled(db, *names):
    """Compile the named worked features, authorize the group, and plan them — all real."""
    irs = [_ok(_compile(db, name)) for name in names]
    authorized = authorize_compilation(db, irs, irs[0].spine, roles=_ROLES)
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    features = []
    for ir in irs:
        resolved = resolve_physical_type(
            _admitted(ir.feature_name).formula,
            operand_types={e.expr_path: e.operand_type for e in ir.expressions})
        assert isinstance(resolved, PhysicalType), resolved
        features.append(PlannedFeature(column_name=ir.feature_name, ir_hash=ir_hash(ir),
                                       physical_type=resolved))
    plan = build_group_plan(group, tuple(features), logical_group_name=GROUP)
    spine_input = derive_requirement(db, INVENTORY, table_ref=CUSTOMERS)
    return authorized, plan, spine_input


def _stub(name, func, inputs, outputs, *, imports=(), tags=()) -> RenderedNode:
    """A node whose BODY is a placeholder and whose WIRING is real.

    Task 12 owns the wiring, the catalog and the shell; Tasks 13 and 14 own every line of compute.
    These stubs exist so the assembly can be tested without inventing that compute here — which is
    exactly what a renderer that shipped its own placeholder compute would have done permanently.
    """
    body = ", ".join(f"arg{index}" for index in range(len(inputs))) or ""
    returns = "None" if len(outputs) == 1 else ", ".join("None" for _ in outputs)
    return RenderedNode(
        name=name, func_name=func, inputs=tuple(inputs), outputs=tuple(outputs),
        imports=tuple(imports), tags=tuple(tags),
        source=f'def {func}({body}):\n    """Task 13/14 renders this body."""\n    return {returns}\n')


def _nodes(datasets: ProjectDatasets, *,
           spine_table: str = "banking.customers") -> tuple[RenderedNode, ...]:
    """A complete, closed wiring over `datasets` — the shape Tasks 13 and 14 must produce."""
    customers = datasets.raw[spine_table]
    transactions = datasets.raw["banking.transactions"]
    nodes = [_stub("spine", "build_spine", [customers], [datasets.spine], tags=["primary"],
                   imports=("from pyspark.sql import DataFrame",))]
    for (column, expr_path), name in sorted(datasets.projections.items()):
        nodes.append(_stub(
            f"project_{column}_{expr_path.replace('.', '_')}",
            f"project_{column}_{expr_path.replace('.', '_')}",
            [transactions, datasets.spine], [name], tags=["intermediate"],
            imports=("from pyspark.sql import DataFrame",)))
    for column, staged in sorted(datasets.staging.items()):
        sources = [name for (feature, _path), name in sorted(datasets.projections.items())
                   if feature == column]
        nodes.append(_stub(f"calculate_{column}", f"calculate_{column}",
                           [*sources, datasets.spine],
                           [staged, datasets.manifests[column]], tags=["feature_staging"]))
    nodes.append(_stub(
        "assemble", "assemble",
        [datasets.spine, *sorted(datasets.staging.values()),
         *sorted(datasets.manifests.values())],
        [datasets.assembled], tags=["feature_staging"]))
    nodes.append(_stub("gate_and_publish", "gate_and_publish", [datasets.assembled],
                       [datasets.published], tags=["feature"]))
    return tuple(nodes)


def test_cross_catalog_projection_cannot_bypass_the_precomputation_gate() -> None:
    datasets = ProjectDatasets(
        raw={"banking.dimension": "raw_dimension"},
        join_gates={"brr-1": "validated_dimension"},
        spine="spine",
        projections={("feature", "body.expr"): "projection"},
        staging={"feature": "staged"},
        manifests={"feature": "manifest"},
        assembled="assembled",
        published="published",
    )
    common = [
        _stub("bridge_gate", "bridge_gate", ["raw_dimension"], ["validated_dimension"]),
        _stub("spine", "spine", ["raw_dimension"], ["spine"]),
        _stub("calculation", "calculation", ["projection", "spine"], ["staged", "manifest"]),
        _stub("assembly", "assembly", ["staged", "manifest"], ["assembled"]),
        _stub("publish", "publish", ["assembled"], ["published"]),
    ]
    bypass = _stub("projection", "projection", ["raw_dimension"], ["projection"])
    with pytest.raises(ValueError, match="gate output.*not consumed"):
        _check_wiring(datasets, (*common, bypass))

    downstream = _stub("projection", "projection", ["validated_dimension"], ["projection"])
    _check_wiring(datasets, (*common, downstream))


def _render(db, *names, engine_versions=None, environment_id=ENVIRONMENT,
            publisher_selection=None) -> SealedProject:
    authorized, plan, spine_input = _compiled(db, *(names or (SUM_30D, COUNT_90D, RATIO_90D)))
    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    return render_project(
        authorized, plan, environment_id=environment_id,
        engine_versions=engine_versions or fixtures.ENGINE_VERSIONS,
        spine_input=spine_input, nodes=_nodes(datasets),
        publisher_selection=publisher_selection)


def _selection(*, environment_id=ENVIRONMENT, engine_versions=None) -> PublisherSelection:
    """§10.3's evidence, as `select_publisher` returns it (Task 16). Built directly here because
    what these tests are about is what the RENDERER does with one, not how one is obtained."""
    return PublisherSelection(
        environment_id=environment_id, mechanism=PublishMechanism.VERSIONED_POINTER,
        capability_attestation_id="att-t16", adds_feature=False,
        engine_versions=engine_versions or fixtures.ENGINE_VERSIONS)


@pytest.fixture
def project(catalog, db) -> SealedProject:  # noqa: F811
    return _render(db)


def _python_files(project: SealedProject) -> dict[str, str]:
    return {path: text for path, text in project.files.items() if path.endswith(".py")}


def _yaml_of(project: SealedProject, path: str):
    return yaml.safe_load(project.files[path])


# ══ the file set ═════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("path", REQUIRED_FILES)
def test_every_file_section_7_requires_is_emitted(project: SealedProject, path: str) -> None:
    """§7 lists what makes the project complete. A missing settings.py or pipeline_registry.py is
    not a partial project — Kedro cannot start one at all."""
    assert path in project.files, sorted(project.files)
    assert project.files[path].strip(), f"{path} is empty"


def test_materialize_to_writes_a_REAL_directory(project: SealedProject, tmp_path) -> None:
    root = materialize_to(project, tmp_path / "generated")
    written = {str(p.relative_to(root)): p.read_text(encoding="utf-8")
               for p in root.rglob("*") if p.is_file()}
    assert written == dict(project.files)
    assert (root / GENERATED_LOCK_FILENAME).exists()


def test_materialize_to_refuses_a_directory_that_already_holds_something(
        project: SealedProject, tmp_path) -> None:
    """A file left from an earlier render is part of the project on disk and not part of the project
    the lock was computed over, so L0 would report a mismatch nobody introduced in this render."""
    (tmp_path / "stale.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        materialize_to(project, tmp_path)


def test_materialize_to_refuses_anything_that_is_not_a_SEALED_project(tmp_path) -> None:
    with pytest.raises(TypeError):
        materialize_to({"README.md": "# hi\n"}, tmp_path)  # type: ignore[arg-type]


# ══ the source parses — and that is ALL a parse proves ═══════════════════════════════════════════


def test_every_rendered_python_file_PARSES(project: SealedProject) -> None:
    """Syntax only. This test is not evidence that the project imports, that Kedro can configure
    it, or that the pipeline object builds — L0 (§11.2) is what establishes those, on a project
    installed into a real environment."""
    files = _python_files(project)
    assert len(files) >= 7, sorted(files)
    for path, text in sorted(files.items()):
        ast.parse(text)  # raises SyntaxError with the path in the message


def test_the_nodes_module_DEFINES_every_function_the_pipeline_wires(project: SealedProject) -> None:
    """The failure this catches imports cleanly right up to the line that does not exist."""
    nodes = ast.parse(project.files[f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/nodes.py"])
    defined = {statement.name for statement in nodes.body
               if isinstance(statement, ast.FunctionDef)}
    pipeline = ast.parse(project.files[f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/pipeline.py"])
    imported = {alias.name for statement in ast.walk(pipeline)
                if isinstance(statement, ast.ImportFrom) and statement.module.endswith(".nodes")
                for alias in statement.names}
    assert imported and imported == defined


def _node_calls(project: SealedProject) -> list[dict[str, ast.expr]]:
    pipeline = ast.parse(project.files[f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/pipeline.py"])
    return [{keyword.arg: keyword.value for keyword in call.keywords}
            for call in ast.walk(pipeline)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            and call.func.id == "node"]


def test_the_pipeline_wires_EXPLICIT_inputs_and_outputs(project: SealedProject) -> None:
    """Kedro would infer an order from the signatures either way. What an explicit wiring adds is
    that the order is readable from the artifact rather than from the run that executed it."""
    calls = _node_calls(project)
    assert len(calls) >= 5, calls
    for call in calls:
        assert {"func", "inputs", "outputs", "name"} <= set(call), sorted(call)
        assert not isinstance(call["inputs"], ast.Constant) or call["inputs"].value is not None
        assert call["outputs"] is not None


def test_every_wired_dataset_is_one_the_CATALOG_declares(project: SealedProject) -> None:
    """A name the catalog does not declare becomes an in-memory dataset: the run reports success
    and the output is gone when it ends."""
    declared = set(_yaml_of(project, "conf/base/catalog.yml"))
    wired: set[str] = set()
    for call in _node_calls(project):
        for slot in ("inputs", "outputs"):
            wired |= set(_literals(call[slot]))
    assert wired and wired <= declared, sorted(wired - declared)


def _literals(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.List):
        return [element.value for element in node.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)]
    return []


# ══ the wiring check — every way a project can parse and still be wrong ══════════════════════════


def _rewire(db, mutate) -> None:
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    nodes = mutate(list(_nodes(datasets)), datasets)
    render_project(authorized, plan, environment_id=ENVIRONMENT,
                   engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input, nodes=nodes)


def test_a_node_writing_an_UNDECLARED_dataset_is_refused(catalog, db) -> None:  # noqa: F811
    def mutate(nodes, datasets):
        nodes[-1] = _stub("gate_and_publish", "gate_and_publish", [datasets.assembled],
                          ["somewhere_else"])
        return nodes
    with pytest.raises(ValueError, match="does not declare"):
        _rewire(db, mutate)


def test_a_node_writing_a_RAW_governed_source_is_refused(catalog, db) -> None:  # noqa: F811
    """Gate 2 authorized this group to READ its physical read set. Writing back into it is a
    different act nobody governed."""
    def mutate(nodes, datasets):
        nodes.append(_stub("backfill", "backfill", [datasets.spine],
                           [datasets.raw["banking.transactions"]]))
        return nodes
    with pytest.raises(ValueError, match="raw governed source"):
        _rewire(db, mutate)


def test_TWO_nodes_writing_one_dataset_are_refused(catalog, db) -> None:  # noqa: F811
    def mutate(nodes, datasets):
        nodes.append(_stub("assemble_again", "assemble_again", [datasets.spine],
                           [datasets.assembled]))
        return nodes
    with pytest.raises(ValueError, match="both write"):
        _rewire(db, mutate)


def test_a_declared_dataset_NOBODY_writes_is_refused(catalog, db) -> None:  # noqa: F811
    """A staging output nobody produces is a §9 completeness failure discovered a whole run later,
    on the cluster."""
    def mutate(nodes, datasets):
        return [node for node in nodes if not node.name.startswith("calculate_")]
    with pytest.raises(ValueError, match="written by no node"):
        _rewire(db, mutate)


def test_a_governed_source_NOBODY_reads_is_refused(catalog, db) -> None:  # noqa: F811
    """The other direction of the same false claim: a catalog entry for a table the project does
    not read states a read it does not perform."""
    def mutate(nodes, datasets):
        return [_stub(node.name, node.func_name,
                      [name for name in node.inputs if name != datasets.raw["banking.customers"]],
                      list(node.outputs))
                for node in nodes]
    with pytest.raises(ValueError, match="read by no node"):
        _rewire(db, mutate)


def test_a_node_reading_an_UNDECLARED_dataset_is_refused(catalog, db) -> None:  # noqa: F811
    def mutate(nodes, datasets):
        nodes[0] = _stub("spine", "build_spine", ["raw_banking__unknown"], [datasets.spine])
        return nodes
    with pytest.raises(ValueError, match="does not declare"):
        _rewire(db, mutate)


def test_a_pipeline_that_publishes_NOTHING_is_refused(catalog, db) -> None:  # noqa: F811
    def mutate(nodes, datasets):
        return [node for node in nodes if node.name != "gate_and_publish"] + [
            _stub("gate", "gate", [datasets.assembled], [datasets.staging[SUM_30D]])]
    with pytest.raises(ValueError):
        _rewire(db, mutate)


def test_two_nodes_of_one_NAME_are_refused(catalog, db) -> None:  # noqa: F811
    def mutate(nodes, datasets):
        nodes.append(_stub("spine", "spine_twice", [datasets.assembled], [datasets.published]))
        return nodes
    with pytest.raises(ValueError, match="node names"):
        _rewire(db, mutate)


def test_a_project_with_NO_nodes_is_refused(catalog, db) -> None:  # noqa: F811
    """It builds, imports, runs, and publishes nothing: the one failure a parse check cannot see."""
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    with pytest.raises(ValueError, match="no nodes"):
        render_project(authorized, plan, environment_id=ENVIRONMENT,
                       engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input, nodes=())


# ══ RenderedNode — the node contract Tasks 13 and 14 must satisfy ════════════════════════════════


def test_a_node_whose_source_defines_ANOTHER_function_is_refused() -> None:
    """The pipeline imports the function BY NAME, so the mismatch is an ImportError on the cluster
    with the wiring perfectly intact."""
    with pytest.raises(ValueError, match="defines"):
        RenderedNode(name="n", func_name="assemble", inputs=("a",), outputs=("b",),
                     source="def assembel(a):\n    return a\n")


def test_an_import_hidden_INSIDE_a_node_source_is_refused() -> None:
    """Imports are declared on the node so the rendered module can de-duplicate and sort them; one
    in the body would be emitted once per node that needed it."""
    with pytest.raises(ValueError, match="imports inside its source"):
        RenderedNode(name="n", func_name="f", inputs=("a",), outputs=("b",),
                     source="from pyspark.sql import DataFrame\n\n\ndef f(a):\n    return a\n")


def test_a_declared_import_that_is_not_an_import_is_refused() -> None:
    with pytest.raises(ValueError, match="and it is not one"):
        RenderedNode(name="n", func_name="f", inputs=("a",), outputs=("b",),
                     imports=("DataFrame = 1",), source="def f(a):\n    return a\n")


def test_a_node_that_writes_NOTHING_is_refused() -> None:
    with pytest.raises(ValueError, match="no output"):
        RenderedNode(name="n", func_name="f", inputs=("a",), outputs=(),
                     source="def f(a):\n    return a\n")


def test_a_node_naming_one_dataset_TWICE_is_refused() -> None:
    """Kedro binds a node's datasets positionally, so a repeated name is an argument nobody can
    tell from another."""
    with pytest.raises(ValueError, match="more than once"):
        RenderedNode(name="n", func_name="f", inputs=("a", "a"), outputs=("b",),
                     source="def f(x, y):\n    return x\n")


def test_a_node_source_that_does_not_parse_is_refused() -> None:
    with pytest.raises(ValueError, match="not valid Python"):
        RenderedNode(name="n", func_name="f", inputs=("a",), outputs=("b",),
                     source="def f(a)\n    return a\n")


def test_node_imports_are_DE_DUPLICATED_and_sorted(project: SealedProject) -> None:
    """Two nodes needing one import must not emit it twice, and the order they happened to be built
    in must not change the rendered bytes."""
    nodes = project.files[f"{SOURCE_ROOT}/pipelines/{PIPELINE_NAME}/nodes.py"]
    assert nodes.count("from pyspark.sql import DataFrame") == 1


# ══ settings, hooks, and §7's launch story ═══════════════════════════════════════════════════════


def test_settings_registers_BOTH_hooks(project: SealedProject) -> None:
    """Neither is decoration: `SparkSessionHook` is the session `kedro run` runs inside, and
    `RunParametersHook` is §11.1's refusal to run on values nobody prepared."""
    settings = ast.parse(project.files[f"{SOURCE_ROOT}/settings.py"])
    hooks = [statement for statement in settings.body
             if isinstance(statement, ast.Assign)
             and any(isinstance(t, ast.Name) and t.id == "HOOKS" for t in statement.targets)]
    assert len(hooks) == 1, "HOOKS is assigned once or not at all"
    registered = {call.func.id for call in ast.walk(hooks[0].value)
                  if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
    assert registered == {"SparkSessionHook", "RunParametersHook"}

    imported = {alias.name for statement in ast.walk(settings)
                if isinstance(statement, ast.ImportFrom) and statement.module == f"{PACKAGE}.hooks"
                for alias in statement.names}
    assert imported == registered

    defined = {statement.name for statement in ast.parse(project.files[f"{SOURCE_ROOT}/hooks.py"]).body
               if isinstance(statement, ast.ClassDef)}
    assert registered <= defined


def test_the_run_parameter_hook_carries_the_CLOSED_gate_code(project: SealedProject) -> None:
    """§14's vocabulary is closed, and the rendered text must carry the enum's value rather than a
    hand-spelled string that would stop matching the day the enum moved."""
    hooks = project.files[f"{SOURCE_ROOT}/hooks.py"]
    assert repr(ValidationGateCode.RUN_PARAMETERS_MISSING.value) in hooks
    for name in REQUIRED_RUN_PARAMETERS:
        assert repr(name) in hooks, name


def test_the_hook_refuses_UNEXPECTED_parameters_as_well_as_missing_ones(
        project: SealedProject) -> None:
    """A parameter nobody planned for is a value the pipeline may read and nobody prepared."""
    hooks = project.files[f"{SOURCE_ROOT}/hooks.py"]
    assert "unexpected" in hooks and "missing" in hooks
    assert "raise RuntimeError" in hooks


def test_the_hook_reads_both_kedro_run_param_keys(project: SealedProject) -> None:
    """kedro 0.19.x passes the dict as `extra_params`; kedro 1.x as `runtime_params`. The artifact
    pins 0.19.9 (goldens/cif_daily/requirements.lock), so reading only the 1.x key means every run
    refuses RUN_PARAMETERS_MISSING."""
    hooks = project.files[f"{SOURCE_ROOT}/hooks.py"]
    assert 'get("runtime_params")' in hooks
    assert 'get("extra_params")' in hooks


def test_spark_yml_pins_ansi_off(project: SealedProject) -> None:
    """The emitted gates observe LEGACY cast semantics: the OVERFLOW_VIOLATION gate reads the NULL
    an out-of-range cast produces, and under ANSI mode (pyspark 4.x's default) that cast raises a
    raw SparkArithmeticException outside the closed gate vocabulary — so the artifact pins the
    setting rather than inheriting whichever default the cluster runs."""
    spark_yml = project.files["conf/base/spark.yml"]
    assert 'spark.sql.ansi.enabled: "false"' in spark_yml


def test_the_readme_states_the_kedro_run_versus_spark_submit_distinction(
        project: SealedProject) -> None:
    """§7: `kedro run` inside a Spark session configured by a Kedro hook, with `spark-submit` used
    only to PLACE that run on the cluster."""
    readme = project.files["README.md"]
    assert "kedro run" in readme and "spark-submit" in readme
    assert "SparkSessionHook" in readme
    assert re.search(r"`spark-submit` does one thing: it PLACES that run on the\s+cluster",
                     readme), readme
    assert re.search(r"```\s*\nkedro run --pipeline materialize", readme), readme


def test_the_readme_names_the_target_and_NOT_the_project_hash(project: SealedProject) -> None:
    readme = project.files["README.md"]
    assert f"{binding.SANDBOX_NAMESPACE}.{GROUP}" in readme
    assert project.identity.generated_project_hash not in readme


# ══ pinned dependency versions come from the inventory ═══════════════════════════════════════════


def test_the_pins_come_from_ClusterInventoryV1_engine_versions(catalog, db) -> None:  # noqa: F811
    """An unpinned dependency resolves to whatever the index offers on the day, so a project that
    "ran last week" is not the project that runs today."""
    versions = fixtures.ENGINE_VERSIONS
    project = _render(db, SUM_30D, engine_versions=versions)
    lock = project.files["requirements.lock"]
    assert f"kedro=={versions.kedro}" in lock
    assert f"kedro-datasets=={versions.kedro_datasets}" in lock
    assert f"pyspark=={versions.pyspark}" in lock

    pyproject = project.files["pyproject.toml"]
    assert f'"kedro=={versions.kedro}"' in pyproject
    assert f'"pyspark=={versions.pyspark}"' in pyproject
    assert f'requires-python = "=={versions.python}"' in pyproject


def test_a_DIFFERENT_inventory_renders_different_pins(catalog, db) -> None:  # noqa: F811
    """The pins are read, not spelled: a renderer with the versions baked in passes every assertion
    above and this one fails."""
    other = EngineVersions(hive="4.0.0", spark="3.4.1", metastore="4.0.0", python="3.10.14",
                           java="17.0.10", pyspark="3.4.1", kedro="0.19.5",
                           kedro_datasets="3.0.1")
    a = _render(db, SUM_30D, engine_versions=fixtures.ENGINE_VERSIONS)
    b = _render(db, SUM_30D, engine_versions=other)
    assert "pyspark==3.4.1" in b.files["requirements.lock"]
    assert a.files["requirements.lock"] != b.files["requirements.lock"]
    assert a.identity.generated_project_hash != b.identity.generated_project_hash


def test_EVERY_requirement_is_pinned_with_a_double_equals(catalog, db) -> None:  # noqa: F811
    """A `>=` or a bare name is the same defect as a missing version, spelled differently."""
    lock = _render(db, SUM_30D).files["requirements.lock"]
    requirements = [line for line in lock.splitlines()
                    if line.strip() and not line.startswith("#")]
    assert requirements
    for line in requirements:
        assert "==" in line, line
        assert not re.search(r"[<>~!]|\*", line), line


def test_the_cluster_engines_are_recorded_even_though_nothing_installs_them(catalog, db) -> None:  # noqa: F811
    """Hive, the metastore and the JVM are what the project is rendered FOR; §10.3's attestation is
    keyed on them, so an artifact that did not say which it was built against could be published
    against an environment nobody proved."""
    versions = fixtures.ENGINE_VERSIONS
    readme = _render(db, SUM_30D).files["README.md"]
    for value in (versions.hive, versions.spark, versions.metastore, versions.java):
        assert value in readme, value


# ══ the catalog — locations, and only the locations this group was authorized ════════════════════


def _hive_tables(project: SealedProject) -> set[str]:
    catalog_yaml = _yaml_of(project, "conf/base/catalog.yml")
    return {f"{entry['database']}.{entry['table']}" for entry in catalog_yaml.values()
            if entry["type"] == "spark.SparkHiveDataset"}


def test_the_catalog_names_ONLY_the_read_set_and_the_spine(project: SealedProject) -> None:
    """`banking.accounts` is in the inventory AND in the seeded catalog, and no worked feature reads
    it. A renderer that emitted "every table it knows about" passes a laxer test and fails here."""
    assert _hive_tables(project) == {
        "banking.transactions",         # every expression's source relation
        "banking.customers",            # the declared population (§4)
        f"{binding.SANDBOX_NAMESPACE}.{GROUP}",   # the publication target
    }


def test_the_catalog_is_the_ONLY_place_a_storage_location_appears(project: SealedProject) -> None:
    """A path or table name written into node source could not be reviewed with the rest of the
    environment's configuration, and nothing reading that source could tell it apart from a
    computed value."""
    # LOCATIONS, not names: `raw_banking__transactions` is a catalog dataset name and belongs in the
    # wiring; `banking.transactions` is where the rows live and does not. `staging_root` likewise is
    # a run-parameter KEY the hook checks for — its VALUE, the interpolation below, is the location.
    locations = ("banking.", "hdfs://", "/warehouse/", ".db/", "${runtime_params:",
                 "feature_staging/", f"{binding.SANDBOX_NAMESPACE}.{GROUP}")
    for path, text in sorted(_python_files(project).items()):
        for location in locations:
            assert location not in text, f"{path} spells the storage location {location!r}"


def test_the_publication_target_is_sandbox_feature_and_is_DERIVED(catalog, db, monkeypatch) -> None:  # noqa: F811
    """Not a parameter, not a template variable, not an argument: `physical_target_for` is the only
    route, and moving the binding's namespace moves this answer with it."""
    assert f"{binding.SANDBOX_NAMESPACE}.{GROUP}" in _hive_tables(_render(db, SUM_30D))
    monkeypatch.setattr(binding, "SANDBOX_NAMESPACE", "elsewhere")
    assert f"elsewhere.{GROUP}" in _hive_tables(_render(db, SUM_30D))


def test_the_publication_target_is_NOT_parameterized(project: SealedProject) -> None:
    """A `${...}` in the target is a way for a run to write somewhere nobody bound (§10.1)."""
    entries = _yaml_of(project, "conf/base/catalog.yml")
    published = entries[f"feature_{GROUP}"]
    assert "${" not in published["database"] and "${" not in published["table"]
    assert published["database"] == binding.SANDBOX_NAMESPACE
    for name in REQUIRED_RUN_PARAMETERS:
        assert "target" not in name and "table" not in name, name


def test_the_published_target_CANNOT_replace_an_existing_table(project: SealedProject) -> None:
    """§10 bans `INSERT OVERWRITE`, and §10.3 forbids selecting any publish mechanism before the
    executable probe proves it — so an artifact rendered WITHOUT a selection must be unable to
    publish over anything.

    Task 16 did not relax this; it made the target generation-scoped instead, and a render that
    supplies a `publisher_selection` gets that entry (below). The `project` fixture deliberately
    supplies none, so this stays the default and the default stays fail-closed.
    """
    entries = _yaml_of(project, "conf/base/catalog.yml")
    assert entries[f"feature_{GROUP}"]["write_mode"] == "errorifexists"
    assert {entry.get("write_mode") for entry in entries.values()} <= {None, "errorifexists"}
    assert {entry.get("save_args", {}).get("mode") for entry in entries.values()} \
        <= {None, "errorifexists"}
    # Prose may explain WHY §10 bans it; no SETTING may decide how one behaves.
    spark = _yaml_of(project, "conf/base/spark.yml")
    assert [key for key in spark if "overwrite" in key.lower()] == []


# ── §10.3: the publication entry a SELECTION renders (Task 16) ───────────────────────────────────


def test_a_selection_makes_the_target_GENERATION_SCOPED(catalog, db) -> None:  # noqa: F811
    """The blocker A.15 recorded — *a rendered project cannot run twice against the same target* —
    lifts here, and not by loosening a write mode. `staging_root` resolves to
    `<base>/<generation_id>` (§11.1), so each generation writes somewhere that did not exist."""
    entries = _yaml_of(_render(db, publisher_selection=_selection()), "conf/base/catalog.yml")
    published = entries[f"feature_{GROUP}"]
    assert published["filepath"].startswith("${runtime_params:staging_root}/")
    assert published["save_args"]["mode"] == "errorifexists"
    assert "write_mode" not in published


def test_the_selection_entry_still_bans_every_replacing_write(catalog, db) -> None:  # noqa: F811
    project = _render(db, publisher_selection=_selection())
    catalog_text = project.files["conf/base/catalog.yml"]
    assert "INSERT OVERWRITE" not in catalog_text.upper()
    entries = _yaml_of(project, "conf/base/catalog.yml")
    assert {entry.get("write_mode") for entry in entries.values()} <= {None, "errorifexists"}
    assert {entry.get("save_args", {}).get("mode") for entry in entries.values()} \
        <= {None, "errorifexists"}


def test_the_selection_entry_introduces_NO_new_run_parameter(catalog, db) -> None:  # noqa: F811
    """The rendered hooks refuse a run carrying an unexpected runtime parameter, so a publication
    entry interpolating one nobody prepared would fail every run at `before_pipeline_run`."""
    text = _render(db, publisher_selection=_selection()).files["conf/base/catalog.yml"]
    assert set(re.findall(r"\$\{runtime_params:([a-z_]+)\}", text)) <= set(REQUIRED_RUN_PARAMETERS)


def test_the_selection_changes_ONLY_the_publication_entry(catalog, db) -> None:  # noqa: F811
    """Every other file — the nodes, the wiring, the pins, the lock's inputs — is untouched, so a
    project rendered with evidence is the same artifact plus a different place to land."""
    without = _render(db)
    with_selection = _render(db, publisher_selection=_selection())
    differing = [path for path in without.files
                 if without.files[path] != with_selection.files[path]]
    assert sorted(differing) == ["GENERATED.lock", "conf/base/catalog.yml"], differing
    plain = _yaml_of(without, "conf/base/catalog.yml")
    attested = _yaml_of(with_selection, "conf/base/catalog.yml")
    assert set(plain) == set(attested)
    assert {name: entry for name, entry in plain.items() if name != f"feature_{GROUP}"} \
        == {name: entry for name, entry in attested.items() if name != f"feature_{GROUP}"}
    assert attested[f"feature_{GROUP}"]["metadata"]["kedro-viz"]["layer"] == "feature"


def test_a_selection_for_ANOTHER_environment_is_refused(catalog, db) -> None:  # noqa: F811
    """A capability attestation is for the EXACT environment (§10.3). Carrying one across would
    publish here on a capability somebody probed somewhere else."""
    with pytest.raises(ValueError, match="environment"):
        _render(db, publisher_selection=_selection(environment_id="hdfc-uat"))


def test_a_selection_probed_on_OTHER_engine_versions_is_refused(catalog, db) -> None:  # noqa: F811
    other = dataclasses.replace(fixtures.ENGINE_VERSIONS, spark="3.4.1")
    with pytest.raises(ValueError, match="spark"):
        _render(db, publisher_selection=_selection(engine_versions=other))


def test_a_bare_MECHANISM_cannot_be_passed_where_a_selection_belongs(catalog, db) -> None:  # noqa: F811
    """§10.3: the renderer consumes a selection and never a mechanism."""
    assert "mechanism" not in inspect.signature(render_project).parameters
    with pytest.raises(TypeError, match="PublisherSelection"):
        _render(db, publisher_selection=PublishMechanism.VERSIONED_POINTER)


def test_OMITTING_the_selection_yields_LESS_capability_not_more(catalog, db) -> None:  # noqa: F811
    """The default must be the fail-closed artifact, so a caller cannot obtain a publishing project
    by forgetting an argument."""
    published = _yaml_of(_render(db), "conf/base/catalog.yml")[f"feature_{GROUP}"]
    assert published["write_mode"] == "errorifexists"
    assert published["type"] == "spark.SparkHiveDataset"


def test_every_catalog_entry_declares_one_of_section_7s_LAYERS(project: SealedProject) -> None:
    entries = _yaml_of(project, "conf/base/catalog.yml")
    layers = {name: entry["metadata"]["kedro-viz"]["layer"] for name, entry in entries.items()}
    assert set(layers.values()) == {"raw", "intermediate", "primary", "feature_staging", "feature"}
    assert layers[f"feature_{GROUP}"] == "feature"
    assert "model_input" not in set(layers.values()), "model_input is Spec C"


def test_each_feature_stages_INDEPENDENTLY(project: SealedProject) -> None:
    """§7: no scan sharing in this slice. One staging output and one manifest per feature.

    The three name FAMILIES are disjoint by prefix — `feature_staging_<column>`,
    `feature_manifest__<column>`, `feature_group__assembled` — so no filter here has to carve one
    family out of another, which is exactly the collision the old `feature_staging_manifest_`
    prefix invited (a feature legitimately named `manifest_x` staged onto another feature's
    manifest).
    """
    entries = _yaml_of(project, "conf/base/catalog.yml")
    staged = [name for name in entries if name.startswith("feature_staging_")]
    manifests = [name for name in entries if name.startswith("feature_manifest__")]
    assert len(staged) == 3 and len(manifests) == 3


def test_a_feature_named_manifest_x_RENDERS(catalog, db) -> None:  # noqa: F811
    """`manifest_<x>` and `assembled` are legitimate hive column names, and the group also stages a
    feature named `x`. Under the old prefixes, `x`'s manifest dataset was
    `feature_staging_manifest_x` — the SAME name as `manifest_x`'s staging dataset — and `assembled`
    staged onto the fixed assembled dataset, so `_unique` refused a group nobody misnamed. The
    `feature_manifest__`/`feature_group__` families cannot collide with any
    `feature_staging_<hive_identifier>`: they differ from it at the character after `feature_`.
    """
    authorized, plan, spine_input = _compiled(db, SUM_30D, COUNT_90D, RATIO_90D)
    renames = {SUM_30D: "x", COUNT_90D: "manifest_x", RATIO_90D: "assembled"}
    irs = tuple(dataclasses.replace(ir, feature_name=renames[ir.feature_name])
                for ir in authorized.irs)
    authorized = dataclasses.replace(authorized, irs=irs)
    hashes = {ir.feature_name: ir_hash(ir) for ir in irs}
    plan = dataclasses.replace(plan, features=tuple(
        dataclasses.replace(feature, column_name=renames[feature.column_name],
                            ir_hash=hashes[renames[feature.column_name]])
        for feature in plan.features))

    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    assert datasets.staging["manifest_x"] == "feature_staging_manifest_x"
    assert datasets.manifests["x"] == "feature_manifest__x"
    assert datasets.staging["assembled"] == "feature_staging_assembled"
    assert datasets.assembled == "feature_group__assembled"

    project = render_project(authorized, plan, environment_id=ENVIRONMENT,
                             engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
                             nodes=_nodes(datasets))
    entries = _yaml_of(project, "conf/base/catalog.yml")
    assert {"feature_staging_manifest_x", "feature_manifest__x",
            "feature_staging_assembled", "feature_group__assembled"} <= set(entries)


def test_a_dotted_schema_addresses_the_right_table(catalog, db) -> None:  # noqa: F811
    """`split(".", 1)` on `edp.raw.customers` yields table `raw.customers` — a table that does not
    exist. The LAST dot separates schema from table: a dotted schema is real (a catalog-qualified
    namespace), a dotted table is not."""
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    dotted = dataclasses.replace(spine_input, schema="edp.raw")
    datasets = project_datasets(authorized, plan, spine_input=dotted)
    assert "edp.raw.customers" in datasets.raw

    project = render_project(authorized, plan, environment_id=ENVIRONMENT,
                             engine_versions=fixtures.ENGINE_VERSIONS, spine_input=dotted,
                             nodes=_nodes(datasets, spine_table="edp.raw.customers"))
    entry = _yaml_of(project, "conf/base/catalog.yml")[datasets.raw["edp.raw.customers"]]
    assert entry["database"] == "edp.raw"
    assert entry["table"] == "customers"


def test_a_dotted_publication_namespace_addresses_the_right_table(catalog, db, monkeypatch) -> None:  # noqa: F811
    """The published target's split has the same defect in the other entry: a catalog-qualified
    sandbox namespace must stay the database, whole."""
    monkeypatch.setattr(binding, "SANDBOX_NAMESPACE", "lake.sandbox_feature")
    published = _yaml_of(_render(db, SUM_30D), "conf/base/catalog.yml")[f"feature_{GROUP}"]
    assert published["database"] == "lake.sandbox_feature"
    assert published["table"] == GROUP


def test_a_control_character_in_a_schema_cannot_change_the_rendered_location(catalog, db) -> None:  # noqa: F811
    """Two defects at once, and both must be closed: the `database:` scalar must ESCAPE the
    newline (or the value silently folds to a different name), and the `# comment` line above it
    must not let the newline END the comment (or the remainder becomes YAML nobody wrote)."""
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    hostile = dataclasses.replace(spine_input, schema="edp\nraw")
    datasets = project_datasets(authorized, plan, spine_input=hostile)

    project = render_project(authorized, plan, environment_id=ENVIRONMENT,
                             engine_versions=fixtures.ENGINE_VERSIONS, spine_input=hostile,
                             nodes=_nodes(datasets, spine_table="edp\nraw.customers"))
    entries = _yaml_of(project, "conf/base/catalog.yml")  # a raw newline fails the parse itself
    entry = entries[datasets.raw["edp\nraw.customers"]]
    assert entry["database"] == "edp\nraw"
    assert entry["table"] == "customers"


# ══ parameters — and what must never become one ══════════════════════════════════════════════════


def test_the_parameters_file_declares_NOTHING(project: SealedProject) -> None:
    """A default is what lets a run proceed on a value run preparation never resolved."""
    assert _yaml_of(project, "conf/base/parameters.yml") in (None, {})


def test_the_required_run_parameters_are_the_run_scoped_values_and_nothing_else() -> None:
    """Pinned with `==`: a value added here is a value the rendered hook will start demanding, and
    a value removed is one a run could then omit."""
    assert set(REQUIRED_RUN_PARAMETERS) == {
        "business_dt", "generation_id", "input_snapshots", "run_id", "sandbox_execution_hash",
        "staging_root"}
    assert list(REQUIRED_RUN_PARAMETERS) == sorted(REQUIRED_RUN_PARAMETERS)


# ══ identity — §7's two phases, in the rendered bytes ════════════════════════════════════════════


def test_the_project_hash_is_in_the_LOCK_and_in_no_other_file(project: SealedProject) -> None:
    """A TEST property, not a runtime gate (A.13): `seal_project` refuses a renderer-supplied lock,
    which is the reachable half, and a seal-time scan of a single-pass render would be dead code.
    This is the assertion that keeps the self-reference from returning by another door.

    The first assertion is the control: a scan that could not find the hash anywhere would pass the
    real check vacuously."""
    digest = project.identity.generated_project_hash
    assert digest in project.files[GENERATED_LOCK_FILENAME], "the scan cannot find a present value"
    for path, text in sorted(project.files.items()):
        if path == GENERATED_LOCK_FILENAME:
            continue
        assert digest not in text, path


def test_the_lock_states_the_hash_of_the_OTHER_files(project: SealedProject) -> None:
    """L0 recomputes over a COMPLETE project, so the excluded file must be excluded either way."""
    recomputed = generated_project_hash(project.files)
    assert recomputed == project.identity.generated_project_hash
    assert read_lock(project.files[GENERATED_LOCK_FILENAME]) == project.identity


def test_the_rendered_files_embed_the_COMPILATION_identity(project: SealedProject) -> None:
    """§7: rendered files embed the compilation identity only. Read back with `literal_eval`, so a
    literal that is valid-looking text and invalid Python fails here rather than at import."""
    module = ast.parse(project.files[f"{SOURCE_ROOT}/__init__.py"])
    embedded = [statement.value for statement in module.body
                if isinstance(statement, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == "COMPILATION_IDENTITY"
                        for t in statement.targets)]
    assert len(embedded) == 1
    assert ast.literal_eval(embedded[0]) == project.identity.compilation.identity_payload()


def test_no_rendered_file_embeds_a_RUN_time_value(project: SealedProject) -> None:
    """There is no business date at render time and there must be no room for one: a project that
    changed per business date would be a daily rebuild wearing a hash-identified artifact's name."""
    for path, text in sorted(project.files.items()):
        for literal in ("2026-07-27", "business_dt=", "load_dt=2026"):
            assert literal not in text, f"{path} carries {literal!r}"


# ══ determinism ══════════════════════════════════════════════════════════════════════════════════


def test_rendering_the_same_compilation_twice_is_BYTE_identical(catalog, db) -> None:  # noqa: F811
    """`generated_project_hash` must identify what the project IS, not the day it was built."""
    first, second = _render(db), _render(db)
    assert dict(first.files) == dict(second.files)
    assert first.identity == second.identity


def test_the_ORDER_the_features_compiled_in_does_not_change_the_bytes(catalog, db) -> None:  # noqa: F811
    """Everything is sorted before it is rendered, so a caller's iteration order cannot fork the
    artifact's identity."""
    forward = _render(db, SUM_30D, COUNT_90D, RATIO_90D)
    backward = _render(db, RATIO_90D, COUNT_90D, SUM_30D)
    assert dict(forward.files) == dict(backward.files)


def test_the_environment_id_is_recorded_and_moves_the_bytes(catalog, db) -> None:  # noqa: F811
    a = _render(db, SUM_30D, environment_id="hdfc-local")
    b = _render(db, SUM_30D, environment_id="hdfc-uat")
    assert "hdfc-uat" in b.files["README.md"]
    assert a.identity.generated_project_hash != b.identity.generated_project_hash


# ══ what rendering refuses ═══════════════════════════════════════════════════════════════════════


def test_rendering_requires_GATE_2s_token(catalog, db) -> None:  # noqa: F811
    """§1.3: one denied element anywhere refuses the whole compilation, and no contract, group plan
    or PROJECT is produced. A bare list of IRs never showed the group's read set was authorized."""
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    with pytest.raises(TypeError, match="Gate 2's authorization token"):
        render_project(list(authorized.irs), plan, environment_id=ENVIRONMENT,  # type: ignore[arg-type]
                       engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
                       nodes=_nodes(project_datasets(authorized, plan, spine_input=spine_input)))


def test_a_spine_input_for_ANOTHER_table_is_refused(catalog, db) -> None:  # noqa: F811
    """Every landing key would come from a population §4 never declared."""
    authorized, plan, _spine = _compiled(db, SUM_30D)
    wrong = derive_requirement(db, INVENTORY, table_ref=fixtures.TABLE_REF)
    with pytest.raises(ValueError, match="declared population"):
        project_datasets(authorized, plan, spine_input=wrong)


def test_loose_engine_versions_are_refused(catalog, db) -> None:  # noqa: F811
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    with pytest.raises(TypeError, match="EngineVersions"):
        render_project(authorized, plan, environment_id=ENVIRONMENT,
                       engine_versions={"kedro": "0.19.9"},  # type: ignore[arg-type]
                       spine_input=spine_input,
                       nodes=_nodes(project_datasets(authorized, plan, spine_input=spine_input)))


def test_a_blank_environment_is_refused(catalog, db) -> None:  # noqa: F811
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    with pytest.raises(ValueError, match="environment"):
        render_project(authorized, plan, environment_id="  ",
                       engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
                       nodes=_nodes(project_datasets(authorized, plan, spine_input=spine_input)))


# ══ the renderer imports nothing it renders ══════════════════════════════════════════════════════


def test_the_render_package_imports_neither_pyspark_nor_kedro() -> None:
    """Render-only. Both appear ONLY inside the strings this package emits, which is what lets the
    compiler run anywhere while the artifact it emits runs on the cluster."""
    for path in sorted(pathlib.Path(render.__file__).parent.glob("*.py")):
        parsed = ast.parse(path.read_text(encoding="utf-8"))
        modules = {node.names[0].name for node in ast.walk(parsed) if isinstance(node, ast.Import)}
        modules |= {node.module for node in ast.walk(parsed)
                    if isinstance(node, ast.ImportFrom) and node.module}
        assert not [m for m in modules if m.split(".")[0] in ("pyspark", "kedro")], path


def test_RENDERER_VERSION_is_owned_here_and_no_call_site_can_spell_one() -> None:
    """A.13's open item. A literal at a call site would freeze the version while the renderer moved
    underneath it, and the one thing the execution hash exists to detect is exactly that."""
    assert isinstance(render.RENDERER_VERSION, str) and render.RENDERER_VERSION.strip()
    source_root = pathlib.Path(render.__file__).parent.parent.parent
    offenders = [path for path in source_root.rglob("*.py")
                 if "renderer_version=" in path.read_text(encoding="utf-8")]
    assert offenders == [], offenders


# ══ goldens — the weakest test here, and last for that reason ════════════════════════════════════


def test_the_rendered_project_matches_its_GOLDENS(project: SealedProject) -> None:
    """A change detector, nothing more. One changed byte fails every golden at once and names no
    property, which is why every property above is asserted on its own.

    `GENERATED.lock` is deliberately NOT a golden: it holds the hash of the other files, so it would
    have to be regenerated for any change at all and would never be read.
    """
    actual = {path: text for path, text in project.files.items()
              if path != GENERATED_LOCK_FILENAME}
    if not GOLDENS.is_dir():
        if os.environ.get("UPDATE_GOLDENS") != "1":
            pytest.fail(f"golden dir {GOLDENS} is missing — if this is an intended renderer "
                        f"change, regenerate with UPDATE_GOLDENS=1 and REVIEW the diff")
        for path, text in actual.items():
            target = GOLDENS / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    expected = {str(path.relative_to(GOLDENS)): path.read_text(encoding="utf-8")
                for path in GOLDENS.rglob("*") if path.is_file()}
    assert set(actual) == set(expected)
    for path in sorted(expected):
        assert actual[path] == expected[path], path
