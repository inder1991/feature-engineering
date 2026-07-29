"""Spec A Task 14b — §9's rendered blocking gates, and the assembly node that feeds them.

**A test that asserts "the gate raised" asserts almost nothing.** There are eighteen members of
``ValidationGateCode`` and every one of them raises the same exception type, so eighteen tests each
asserting ``pytest.raises(RuntimeError)`` would all pass against a node whose only statement was
``raise RuntimeError("boom")``. Every test below therefore asserts the CODE — through
:func:`~featuregen.materialize.render.nodes_gate.gate_code_of`, which reads the leading token the
rendered message is contractually required to carry — and, where the gate names something, that the
detail names the right column or feature. A gate firing for another gate's reason fails here.

**Gates are proven by EXECUTION, not by rendered text.** The rendered source is ``exec``'d against
``fake_spark`` (Task 13a's stand-in) and given real frames, so "the manifest check was rendered as a
comment" and "the check runs and refuses" are different outcomes. What that does NOT establish is
that the project runs on Spark — that is L0's job (§11.2, Task 15), and nothing here may be read as
that proof.

**``PROJECT_INTEGRITY`` is the one gate proven against real files.** The project is materialized to
a real directory and the rendered node is pointed at it, so the gate is measured against the bytes
``seal_project`` hashed rather than against a re-statement of them.

Fixtures are the real compilation's (``test_ir`` → ``test_render_project``), for the reason that
file states: a gate rendered from a plan nobody compiles is a gate over nothing.
"""
from __future__ import annotations

import ast
import json
import pathlib

import pytest
from tests.featuregen.materialize import fake_spark, fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    BUSINESS_DT,
    CADENCE,
    COUNT_90D,
    GENERATION,
    RATIO_90D,
    RUN,
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_render_nodes_compute import _render_all, _wired_nodes
from tests.featuregen.materialize.test_render_project import ENVIRONMENT, _compiled, _stub

from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.group_plan import SYSTEM_COLUMNS, expected_schema
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME
from featuregen.materialize.render.nodes_gate import (
    ASSEMBLY_FUNC_NAME,
    FINDING_SEPARATOR,
    GATE_FUNC_NAME,
    gate_code_of,
    render_assembly_node,
    render_gate_node,
)
from featuregen.materialize.render.project import (
    ProjectDatasets,
    materialize_to,
    project_datasets,
    render_project,
)

EXECUTION_HASH = "e" * 64


# ── the real compilation, and the two nodes under test ───────────────────────────────────────────


@pytest.fixture
def compiled(catalog, db):  # noqa: F811 — `catalog` seeds the governed graph `db` is read through
    """The three worked features, compiled, authorized and planned — all real."""
    return _compiled(db, SUM_30D, COUNT_90D, RATIO_90D)


@pytest.fixture
def plan(compiled):
    return compiled[1]


@pytest.fixture
def datasets(compiled) -> ProjectDatasets:
    authorized, group_plan, spine_input = compiled
    return project_datasets(authorized, group_plan, spine_input=spine_input)


@pytest.fixture
def assembly(plan, datasets):
    return render_assembly_node(
        plan, spine_dataset=datasets.spine, staging_datasets=datasets.staging,
        manifest_datasets=datasets.manifests, assembled_dataset=datasets.assembled)


@pytest.fixture
def gate(plan, datasets):
    return render_gate_node(plan, assembled_dataset=datasets.assembled,
                            published_dataset=datasets.published)


def _nodes(datasets: ProjectDatasets, assembly, gate) -> tuple:
    """A complete closed wiring: Task 13's compute stubbed, Task 14b's two nodes REAL.

    The compute nodes are stubs for the same reason Task 12's are — this file owns the gates, not
    the compute — but the assembly and gate nodes are the rendered articles, so ``render_project``
    checks THEIR wiring against the catalog rather than a placeholder's.
    """
    customers = datasets.raw["banking.customers"]
    transactions = datasets.raw["banking.transactions"]
    nodes = [_stub("spine", "build_spine", [customers], [datasets.spine], tags=["primary"],
                   imports=("from pyspark.sql import DataFrame",))]
    for (column, expr_path), name in sorted(datasets.projections.items()):
        node = f"project_{column}_{expr_path.replace('.', '_')}"
        nodes.append(_stub(node, node, [transactions, datasets.spine], [name],
                           tags=["intermediate"]))
    for column, staged in sorted(datasets.staging.items()):
        sources = [name for (feature, _path), name in sorted(datasets.projections.items())
                   if feature == column]
        nodes.append(_stub(f"calculate_{column}", f"calculate_{column}",
                           [*sources, datasets.spine], [staged, datasets.manifests[column]],
                           tags=["feature_staging"]))
    return (*nodes, assembly, gate)


@pytest.fixture
def project(compiled, datasets, assembly, gate):
    authorized, group_plan, spine_input = compiled
    return render_project(
        authorized, group_plan, environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
        nodes=_nodes(datasets, assembly, gate))


@pytest.fixture
def full_project(compiled, catalog, db, datasets, assembly, gate):  # noqa: F811
    """The WHOLE artifact with nothing stubbed: Task 13's real compute plus Task 14b's real gates.

    The stubbed `project` above is enough to check this task's own nodes, but two claims are about
    the project as a WHOLE — that every gate code in §14's closed table is rendered somewhere, and
    that no generated file states the project hash — and a stub renders neither a gate nor a hash.
    """
    authorized, group_plan, spine_input = compiled
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    wired = _wired_nodes((authorized, group_plan, group.contract, spine_input), datasets)
    return render_project(
        authorized, group_plan, environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=spine_input,
        # `_wired_nodes` ends with that file's stub for THIS task's two nodes; the real ones replace
        # it, which is also what makes `render_project`'s wiring check see the real inputs.
        nodes=(*wired[:-1], assembly, gate))


@pytest.fixture
def materialized(project, tmp_path) -> pathlib.Path:
    """The sealed project written to real files, and the ``nodes.py`` path a node resolves from."""
    return materialize_to(project, tmp_path / "artifact")


# ── inputs the rendered assembly node is given ───────────────────────────────────────────────────


def _columns(plan) -> tuple[str, ...]:
    return tuple(feature.column_name for feature in plan.features)


def _spine(plan, keys=("C1", "C2")) -> fake_spark.DataFrame:
    key = plan.entity_key_columns[0]
    return fake_spark.DataFrame(
        [{key: entity, plan.business_dt_column: BUSINESS_DT} for entity in keys],
        [key, plan.business_dt_column])


def _staged(plan, column: str, values=None) -> fake_spark.DataFrame:
    """One feature's staging output: ``(keys…, business_dt, one feature column)`` and nothing else."""
    key = plan.entity_key_columns[0]
    values = {"C1": 1, "C2": 2} if values is None else values
    return fake_spark.DataFrame(
        [{key: entity, plan.business_dt_column: BUSINESS_DT, column: value}
         for entity, value in values.items()],
        [key, plan.business_dt_column, column])


def _manifest(plan, column: str, **overrides) -> dict:
    feature = next(f for f in plan.features if f.column_name == column)
    manifest = {
        "intent_feature_name": column,
        "ir_hash": feature.ir_hash,
        "generation_id": GENERATION,
        "run_id": RUN,
        "business_dt": BUSINESS_DT,
        "generated_project_hash": "unused-by-the-gate",
        "sandbox_execution_hash": EXECUTION_HASH,
        "output_location": f"/staging/{column}",
        "schema_hash": "0" * 64,
        "row_count": 2,
        "status": "completed",
    }
    manifest.update(overrides)
    return manifest


def _run_assembly(assembly, plan, materialized, *, spine=None, staged=None, manifests=None,
                  generation_id=GENERATION, run_id=RUN, business_dt=BUSINESS_DT):
    """Execute the rendered assembly node against the stand-in, rooted at a REAL project."""
    package = materialized.name
    nodes_py = next(materialized.glob("src/*/pipelines/*/nodes.py"))
    assert package  # the path below is what `_LOCK_DEPTH` resolves from
    function = fake_spark.run_rendered(assembly.source, ASSEMBLY_FUNC_NAME,
                                       module_file=str(nodes_py))
    columns = _columns(plan)
    frames = staged if staged is not None else {c: _staged(plan, c) for c in columns}
    documents = manifests if manifests is not None else {c: _manifest(plan, c) for c in columns}
    return function(spine if spine is not None else _spine(plan),
                    *(frames[column] for column in columns),
                    *(documents[column] for column in columns),
                    business_dt, generation_id, run_id, EXECUTION_HASH)


def _code(exc: pytest.ExceptionInfo) -> ValidationGateCode | None:
    """The code of the gate that fired FIRST — the leading token the message must carry."""
    return gate_code_of(str(exc.value))


def _findings(exc: pytest.ExceptionInfo) -> dict[ValidationGateCode | None, str]:
    """Every finding in the message, keyed by ITS OWN code.

    This is what makes a gate test more than "something raised": a test can require that a
    particular code fired AND that its own detail names the thing that was wrong, so a gate firing
    for another gate's reason — or one gate firing for two — fails rather than passes.
    """
    parts = str(exc.value).split(FINDING_SEPARATOR)
    found = {gate_code_of(part): part for part in parts}
    assert len(found) == len(parts), f"two findings share one code: {parts}"
    return found


# ══ §10.2 — the three system columns, added ONCE ═════════════════════════════════════════════════


def test_assembly_adds_each_system_column_EXACTLY_once(assembly, plan, materialized) -> None:
    """§10.2 — per-feature staging carries none of them, so a second copy would collide silently.

    Counted rather than tested for membership: `in` is satisfied by a frame carrying the column
    twice, which is the exact failure the "added once, at assembly" rule exists to prevent.
    """
    assembled = _run_assembly(assembly, plan, materialized)
    for system in SYSTEM_COLUMNS:
        assert assembled.columns.count(system) == 1, (system, assembled.columns)


def test_the_system_columns_carry_the_RUN_s_values_not_a_rendered_literal(
        assembly, plan, materialized, project) -> None:
    """§10.2 — two of the three cannot be literals, and the third is a run parameter."""
    assembled = _run_assembly(assembly, plan, materialized)
    row = assembled.rows[0]
    assert row["__generation_id"] == GENERATION
    assert row["__sandbox_execution_hash"] == EXECUTION_HASH
    assert row["__generated_project_hash"] == project.identity.generated_project_hash


def test_no_generated_source_file_contains_the_PROJECT_HASH_literal(full_project) -> None:
    """§7 — a file containing the hash OF the files cannot hash to itself.

    Greps every rendered file rather than the assembly node alone: the hazard is a literal
    ANYWHERE in the project, and a test that read one file would miss a README that quoted it.
    """
    project_hash = full_project.identity.generated_project_hash
    assert project_hash, "the sealed project has no hash to look for"
    offenders = [path for path, text in full_project.files.items()
                 if path != GENERATED_LOCK_FILENAME and project_hash in text]
    assert offenders == [], offenders


def test_the_lock_is_the_ONLY_file_that_states_it(project) -> None:
    """The other half of the same rule: it must appear SOMEWHERE, or the node reads nothing."""
    project_hash = project.identity.generated_project_hash
    assert project_hash in project.files[GENERATED_LOCK_FILENAME]


def test_a_staging_frame_that_ALREADY_carries_a_system_column_is_REFUSED(
        assembly, plan, materialized) -> None:
    """`withColumn` REPLACES, so an arriving copy would be overwritten and nothing would say so."""
    column = _columns(plan)[0]
    key = plan.entity_key_columns[0]
    intruder = fake_spark.DataFrame(
        [{key: "C1", plan.business_dt_column: BUSINESS_DT, column: 1, "__generation_id": "other"}],
        [key, plan.business_dt_column, column, "__generation_id"])
    staged = {c: (intruder if c == column else _staged(plan, c)) for c in _columns(plan)}
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, staged=staged)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.UNEXPECTED_COLUMN}
    assert "__generation_id" in findings[ValidationGateCode.UNEXPECTED_COLUMN]
    assert column in findings[ValidationGateCode.UNEXPECTED_COLUMN]


# ══ §8 rule 3 — assembly is a LEFT reduction onto the spine ══════════════════════════════════════


def test_an_entity_with_no_staged_row_still_reaches_the_published_group(
        assembly, plan, materialized) -> None:
    """§8 rule 3 — entities with no source rows stay present. An INNER join loses them silently."""
    columns = _columns(plan)
    staged = {c: _staged(plan, c, {"C1": 1}) for c in columns}
    assembled = _run_assembly(assembly, plan, materialized, staged=staged)
    key = plan.entity_key_columns[0]
    assert sorted(row[key] for row in assembled.rows) == ["C1", "C2"]
    absent = next(row for row in assembled.rows if row[key] == "C2")
    assert absent[columns[0]] is None


def test_the_assembled_group_carries_the_plan_s_columns_in_the_plan_s_ORDER(
        assembly, plan, materialized) -> None:
    """§9's schema-hash gate reads the order, so assembly must produce it rather than repair it."""
    assembled = _run_assembly(assembly, plan, materialized)
    assert assembled.columns == [column.name for column in expected_schema(plan)]


# ══ §9 — the staging-manifest gates, each proven to fire for ITS OWN reason ═══════════════════════


def test_a_complete_run_passes_every_manifest_gate(assembly, plan, materialized) -> None:
    """The control. Without it, every gate test below is satisfied by a node that always raises."""
    assembled = _run_assembly(assembly, plan, materialized)
    assert assembled.count() == 2


def test_a_manifest_naming_a_feature_the_plan_does_not_contain(
        assembly, plan, materialized) -> None:
    """§9 — output for an unplanned feature would be an extra column at assembly.

    TWO gates fire on one renamed manifest and each names its own subject: the planned feature is
    now unstaged (``MISSING_STAGING_MANIFEST``) and the name it was given is not in the plan
    (``UNEXPECTED_COLUMN``). Asserting which finding names which is what proves neither gate is
    reporting the other's evidence.
    """
    column = _columns(plan)[0]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[column] = _manifest(plan, column, intent_feature_name="a_feature_nobody_planned")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.MISSING_STAGING_MANIFEST,
                             ValidationGateCode.UNEXPECTED_COLUMN}
    assert column in findings[ValidationGateCode.MISSING_STAGING_MANIFEST]
    assert "a_feature_nobody_planned" in findings[ValidationGateCode.UNEXPECTED_COLUMN]
    assert "a_feature_nobody_planned" not in findings[
        ValidationGateCode.MISSING_STAGING_MANIFEST]


def test_two_manifests_naming_ONE_feature(assembly, plan, materialized) -> None:
    """§9 — choosing between them would choose which computation published."""
    first, second = _columns(plan)[0], _columns(plan)[1]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[second] = _manifest(plan, second, intent_feature_name=first)
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.DUPLICATE_STAGING_MANIFEST,
                             ValidationGateCode.MISSING_STAGING_MANIFEST}
    assert first in findings[ValidationGateCode.DUPLICATE_STAGING_MANIFEST]
    assert second in findings[ValidationGateCode.MISSING_STAGING_MANIFEST]


@pytest.mark.parametrize("field, value", [("generation_id", "gen-9999"), ("run_id", "run-9999"),
                                          ("business_dt", "2026-01-01")])
def test_a_manifest_bound_to_another_generation_run_or_DATE(
        assembly, plan, materialized, field: str, value: str) -> None:
    """§9 — staging paths are generation-scoped; an older SUCCESSFUL manifest whose ir_hash still
    matches would otherwise publish stale output past every other check."""
    column = _columns(plan)[0]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[column] = _manifest(plan, column, **{field: value})
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.STALE_STAGING_MANIFEST}
    assert column in findings[ValidationGateCode.STALE_STAGING_MANIFEST]
    assert value in findings[ValidationGateCode.STALE_STAGING_MANIFEST]


def test_a_STALE_manifest_is_not_ALSO_graded_on_its_ir_hash(assembly, plan, materialized) -> None:
    """§9 judges staleness first, over the whole staging area: a manifest about another run is not
    evidence about this one, and grading its computation would report a verdict nobody asked for."""
    column = _columns(plan)[0]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[column] = _manifest(plan, column, generation_id="gen-9999", ir_hash="f" * 64,
                                  status="failed")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    assert set(_findings(exc)) == {ValidationGateCode.STALE_STAGING_MANIFEST}


def test_a_manifest_that_records_a_FAILED_computation(assembly, plan, materialized) -> None:
    """§9 — the record EXISTS and says the column was never computed: not a missing manifest."""
    column = _columns(plan)[0]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[column] = _manifest(plan, column, status="failed")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.INCOMPLETE_COMPUTATION}
    assert column in findings[ValidationGateCode.INCOMPLETE_COMPUTATION]


def test_a_manifest_whose_ir_hash_is_not_the_planned_one(assembly, plan, materialized) -> None:
    """§9 — a matching schema cannot show WHICH IR produced the column."""
    column = _columns(plan)[0]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[column] = _manifest(plan, column, ir_hash="f" * 64)
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.IR_HASH_MISMATCH}
    assert column in findings[ValidationGateCode.IR_HASH_MISMATCH]


def test_the_manifest_gates_report_EVERY_broken_feature_not_the_first(
        assembly, plan, materialized) -> None:
    """A gate that stopped early would send an operator round the regenerate/rerun loop once per
    broken feature — the reason ``check_completeness`` returns every failure."""
    first, second = _columns(plan)[0], _columns(plan)[1]
    manifests = {c: _manifest(plan, c) for c in _columns(plan)}
    manifests[first] = _manifest(plan, first, status="failed")
    manifests[second] = _manifest(plan, second, status="failed")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized, manifests=manifests)
    assert first in str(exc.value) and second in str(exc.value)


# ══ §9 — PROJECT_INTEGRITY, measured against the REAL sealed bytes ═══════════════════════════════


def test_an_UNTOUCHED_project_passes_the_integrity_gate(assembly, plan, materialized) -> None:
    """The control the three mutations below need. Without it they pass against a gate that always
    fires, which would also make every other assembly test above unreachable."""
    assert _run_assembly(assembly, plan, materialized).count() == 2


def test_ONE_changed_byte_in_a_generated_file_is_caught(assembly, plan, materialized) -> None:
    """L0 ran on the submitting machine; this gate runs on the cluster, against what arrived."""
    readme = materialized / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized)
    assert set(_findings(exc)) == {ValidationGateCode.PROJECT_INTEGRITY}


def test_a_DELETED_generated_file_is_caught(assembly, plan, materialized) -> None:
    """A hash over "the files that are there" would answer happily for a project missing one."""
    (materialized / "conf" / "base" / "logging.yml").unlink()
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized)
    assert set(_findings(exc)) == {ValidationGateCode.PROJECT_INTEGRITY}


def test_an_ADDED_file_is_caught(assembly, plan, materialized) -> None:
    """A file nobody sealed can shadow a module, so it is a different project."""
    (materialized / "conf" / "base" / "extra.yml").write_text("added: true\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as exc:
        _run_assembly(assembly, plan, materialized)
    assert set(_findings(exc)) == {ValidationGateCode.PROJECT_INTEGRITY}


def test_pythons_OWN_byte_cache_is_not_a_project_change(assembly, plan, materialized) -> None:
    """Importing the project writes `__pycache__`, and a gate that fired on it would refuse every
    run that had ever been imported — which is every run."""
    cache = materialized / "src" / "__pycache__"
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "settings.cpython-313.pyc").write_bytes(b"\x00\x01")
    assert _run_assembly(assembly, plan, materialized).count() == 2


def test_the_editable_install_metadata_directory_is_not_a_project_change(
        assembly, plan, materialized) -> None:
    """L0 installs the project (§11.2); `pip install -e` writes `*.egg-info` INSIDE it."""
    info = materialized / "src" / "sandbox_feature_cif_daily.egg-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "PKG-INFO").write_text("Name: x\n", encoding="utf-8")
    assert _run_assembly(assembly, plan, materialized).count() == 2


# ══ the wiring, and the rules every rendered node obeys ══════════════════════════════════════════


def test_both_nodes_close_the_pipeline_wiring(project, datasets) -> None:
    """`render_project` refuses a wiring that does not close, so rendering one at all is the
    assertion: an undeclared output, an unwritten dataset or a second writer all fail here."""
    assert GENERATED_LOCK_FILENAME in project.files


def test_the_assembly_node_CONSUMES_every_staging_manifest(assembly, datasets) -> None:
    """§9's evidence is the manifests. A node that read only the frames could not tell a column of
    the right shape from the column the planned IR produced."""
    for name in datasets.manifests.values():
        assert name in assembly.inputs
    for name in datasets.staging.values():
        assert name in assembly.inputs
    assert assembly.outputs == (datasets.assembled,)


@pytest.mark.parametrize("call", ["collect(", "take(", "head(", "toPandas("])
def test_no_rendered_node_pulls_feature_data_to_the_DRIVER(full_project, call: str) -> None:
    """The control plane never reads feature data, and a manifest writer that did would carry rows
    out of the cluster on its way to writing counts."""
    for path, text in full_project.files.items():
        if path.endswith(".py"):
            assert call not in text, (path, call)


@pytest.mark.parametrize("node_name", ["assembly", "gate"])
def test_every_rendered_node_parses(request, node_name: str) -> None:
    node = request.getfixturevalue(node_name)
    ast.parse(node.source)


def test_no_storage_location_is_written_into_node_SOURCE(assembly, gate, datasets) -> None:
    """Every location is a catalog entry (§7). A dataset name in a node body would be a location
    that could not be reviewed with the rest of the environment's configuration."""
    for node in (assembly, gate):
        for name in datasets.names():
            assert name not in node.source, (node.name, name)


# ══ the code vocabulary: every member of §14's closed table is RENDERED ═══════════════════════════


@pytest.fixture
def rendered_corpus(full_project, compiled, db) -> str:
    """Everything this compiler can render, not everything ONE artifact contains.

    A single project cannot carry the whole table and it would be wrong to ask it to:
    ``SPINE_NON_DETERMINISTIC`` is rendered only where §4.2's ``LATEST_AVAILABLE_AS_OF`` policy is
    declared, so a group whose population is a current snapshot has no tie to break and no such
    gate. The corpus is therefore the full project PLUS a spine rendered under every snapshot
    policy — which is what "every code is rendered" can honestly mean.
    """
    authorized, group_plan, spine_input = compiled
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    spines = [node.source for node in _render_all(
        (authorized, group_plan, group.contract, spine_input)).values()]
    return "\n".join([*(text for path, text in full_project.files.items()
                        if path.endswith(".py")), *spines])


@pytest.mark.parametrize("code", list(ValidationGateCode), ids=lambda c: c.value)
def test_every_validation_gate_code_is_rendered_somewhere(
        rendered_corpus: str, code: ValidationGateCode) -> None:
    """§9's table is closed and normative: a member nothing renders is a gate that cannot fire.

    Presence is the WEAK half of this file's proof and is stated as such — a code in a comment
    passes here. The strong half is the per-code execution tests, which is why every code this
    task owns has one above and every code Tasks 12/13 own has one in their files.
    """
    assert code.value in rendered_corpus


def test_gate_code_of_reads_the_LEADING_token_and_nothing_else() -> None:
    """The rendered project cannot import `featuregen`, so the code travels as the first token of
    the message. That is a contract, and this is what reads it back into the closed vocabulary."""
    assert gate_code_of("KEY_NOT_UNIQUE: two rows") is ValidationGateCode.KEY_NOT_UNIQUE
    assert gate_code_of("something went wrong") is None
    assert gate_code_of("") is None
    # A code that appears LATER in the message is not the code that fired.
    assert gate_code_of("boom: KEY_NOT_UNIQUE") is None


def test_the_assembly_and_gate_functions_are_named_what_the_pipeline_wires(assembly, gate) -> None:
    assert assembly.func_name == ASSEMBLY_FUNC_NAME
    assert gate.func_name == GATE_FUNC_NAME


def test_the_lock_is_read_at_RUN_time_rather_than_baked_in(assembly) -> None:
    """The assembly node's own statement of §7's rule, asserted on the rendered text because the
    behaviour it produces (`test_the_system_columns_carry_the_RUN_s_values…`) cannot distinguish a
    correct literal from a read."""
    assert GENERATED_LOCK_FILENAME in assembly.source
    assert "read_text" in assembly.source


def test_the_manifest_documents_are_read_as_JSON_shaped_mappings(assembly, plan,
                                                                 materialized) -> None:
    """The rendered calculation node writes a plain dict through a JSON dataset, so the assembly
    node must read plain strings back — an enum comparison would never match."""
    documents = {c: json.loads(json.dumps(_manifest(plan, c))) for c in _columns(plan)}
    assert _run_assembly(assembly, plan, materialized, manifests=documents).count() == 2



def test_a_ONE_feature_group_renders_a_TUPLE_and_not_a_STRING(db, catalog) -> None:  # noqa: F811
    """`('x')` is a string and `('x',)` is a tuple, and the rendered `name not in planned` test
    would read the string character by character — accepting any single letter as a planned
    feature. Rendered from a group that really has one feature, because that is the only shape
    that can produce it."""
    authorized, group_plan, spine_input = _compiled(db, SUM_30D)
    one = project_datasets(authorized, group_plan, spine_input=spine_input)
    node = render_assembly_node(
        group_plan, spine_dataset=one.spine, staging_datasets=one.staging,
        manifest_datasets=one.manifests, assembled_dataset=one.assembled)
    namespace: dict = {}
    exec(compile(node.source, "<rendered>", "exec"), namespace)  # noqa: S102
    body = ast.parse(node.source).body[0]
    literals = {target.id: ast.literal_eval(statement.value)
                for statement in ast.walk(body) if isinstance(statement, ast.Assign)
                for target in statement.targets
                if isinstance(target, ast.Name) and target.id in {"planned", "system_columns"}}
    assert literals["planned"] == (SUM_30D,)
    assert literals["system_columns"] == SYSTEM_COLUMNS


# ══ §9 — the shape gates over the ASSEMBLED group ════════════════════════════════════════════════


def _expected_types(plan) -> dict[str, str]:
    """What Spark would answer for a correctly assembled group, in Spark's own spelling.

    The feature types are the PLAN's, lower-cased: `dtypes` reports `decimal(38,6)` where §6
    resolved `DECIMAL(38,6)`, and a gate that could not see past that would fire on every correct
    run. The landing key and the business date are typed here only because the stand-in refuses to
    invent a type — §9 checks neither, because nothing governed states one at compile time.
    """
    types = {plan.entity_key_columns[0]: "string", plan.business_dt_column: "date"}
    for feature in plan.features:
        types[feature.column_name] = feature.physical_type.sql_type.lower()
    for system in SYSTEM_COLUMNS:
        types[system] = "string"
    return types


def _assembled(plan, *, rows=None, types=None, order=None) -> fake_spark.DataFrame:
    """A group assembled exactly as the plan describes, or as near to it as a test needs."""
    declared = _expected_types(plan)
    declared.update(types or {})
    columns = list(order) if order is not None else [c.name for c in expected_schema(plan)]
    if rows is None:
        rows = [_assembled_row(plan)]
    return fake_spark.DataFrame(
        [{name: row.get(name) for name in columns} for row in rows], columns,
        {name: declared[name] for name in columns if name in declared})


def _assembled_row(plan, **overrides) -> dict:
    row = {plan.entity_key_columns[0]: "C1", plan.business_dt_column: BUSINESS_DT,
           "__generation_id": GENERATION, "__generated_project_hash": "p" * 64,
           "__sandbox_execution_hash": EXECUTION_HASH}
    row.update({feature.column_name: 1 for feature in plan.features})
    row.update(overrides)
    return row


def _run_gate(gate, frame):
    return fake_spark.run_rendered(gate.source, GATE_FUNC_NAME)(frame)


def test_a_group_that_matches_the_plan_passes_EVERY_shape_gate(gate, plan) -> None:
    """The control every gate test below needs, and more: the schema hash the rendered node
    compares against is `expected_schema_hash(plan)`, computed in this process by RFC 8785 and in
    the rendered node by `json.dumps(sort_keys=True)`. Passing here is what proves those two
    canonicalizations agree — a claim no assertion about the rendered text could make."""
    passed = _run_gate(gate, _assembled(plan))
    assert passed.columns == [column.name for column in expected_schema(plan)]


def test_a_DUPLICATE_landing_key(gate, plan) -> None:
    """§9 — a duplicate is a blocking gate, never a de-duplication step."""
    frame = _assembled(plan, rows=[_assembled_row(plan), _assembled_row(plan)])
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, frame)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.KEY_NOT_UNIQUE}
    assert "1" in findings[ValidationGateCode.KEY_NOT_UNIQUE]


def test_a_MISSING_planned_column(gate, plan) -> None:
    """§9 — required columns present. A shorter published row is a different contract."""
    absent = plan.features[0].column_name
    order = [c.name for c in expected_schema(plan) if c.name != absent]
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, _assembled(plan, order=order))
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.MISSING_FEATURE_COLUMN}
    assert absent in findings[ValidationGateCode.MISSING_FEATURE_COLUMN]


def test_a_column_the_plan_does_NOT_contain(gate, plan) -> None:
    """§9 — none extra. A column nobody planned is a column nobody governed."""
    order = [*(c.name for c in expected_schema(plan)), "smuggled_in"]
    frame = _assembled(plan, order=order, types={"smuggled_in": "string"},
                       rows=[_assembled_row(plan, smuggled_in="x")])
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, frame)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.UNEXPECTED_COLUMN}
    assert "smuggled_in" in findings[ValidationGateCode.UNEXPECTED_COLUMN]


def test_a_feature_column_of_the_WRONG_declared_type(gate, plan) -> None:
    """§9 — physical types match §6, including the decimal's precision and scale."""
    column = plan.features[0].column_name
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, _assembled(plan, types={column: "decimal(9,2)"}))
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.WRONG_COLUMN_TYPE}
    detail = findings[ValidationGateCode.WRONG_COLUMN_TYPE]
    assert column in detail and "decimal(9,2)" in detail


def test_binary_floating_point_is_reported_as_FORBIDDEN_and_not_as_a_type_mismatch(
        gate, plan) -> None:
    """Both gates can see a `double` in a DECIMAL column. Only one of them says what is wrong with
    it, so the type gate deliberately stands down: 'this column is a double' and 'this column is
    the wrong decimal' have different fixes, and reporting both would name neither."""
    column = plan.features[0].column_name
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, _assembled(plan, types={column: "double"}))
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.FORBIDDEN_NUMERIC}
    assert column in findings[ValidationGateCode.FORBIDDEN_NUMERIC]


def test_a_NULL_in_a_column_declared_NOT_NULL(gate, plan) -> None:
    """§9 — nullability, judged on the ROWS. Spark widens the schema flag through a left join, so
    the flag says nothing; a NULL landing key reaching the published table says everything."""
    key = plan.entity_key_columns[0]
    frame = _assembled(plan, rows=[_assembled_row(plan), _assembled_row(plan, **{key: None})])
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, frame)
    findings = _findings(exc)
    assert set(findings) == {ValidationGateCode.WRONG_NULLABILITY}
    assert key in findings[ValidationGateCode.WRONG_NULLABILITY]


def test_a_PERMUTED_published_row_is_caught_by_the_schema_hash_ALONE(gate, plan) -> None:
    """The gate that would be dead code if it were redundant. Every name, type and nullability is
    right; only the ORDER moved, which every name-keyed check above is blind to by construction."""
    names = [column.name for column in expected_schema(plan)]
    permuted = [names[1], names[0], *names[2:]]
    with pytest.raises(RuntimeError) as exc:
        _run_gate(gate, _assembled(plan, order=permuted))
    assert set(_findings(exc)) == {ValidationGateCode.SCHEMA_HASH_MISMATCH}


def test_the_gate_reads_the_assembled_group_and_writes_the_publication_target(
        gate, datasets) -> None:
    assert gate.inputs == (datasets.assembled,)
    assert gate.outputs == (datasets.published,)


def test_the_gate_names_no_publish_MECHANISM(gate) -> None:
    """§10.3 — the renderer consumes a `PublisherSelection` or renders no mechanism at all. This
    node renders none: no DDL, no table, no partition swap. The catalog entry is the mechanism, and
    it is fail-closed until Task 16's probe attests one."""
    for banned in ("INSERT OVERWRITE", "ALTER TABLE", "EXCHANGE PARTITION", "saveAsTable",
                   "write.", "SET LOCATION"):
        assert banned not in gate.source, banned


# ══ assembly → gates, end to end ═════════════════════════════════════════════════════════════════


def test_the_assembled_group_the_ASSEMBLY_node_produces_passes_the_GATE_node(
        assembly, gate, plan, materialized) -> None:
    """The seam between the two nodes, executed rather than assumed: the shape one produces is the
    shape the other requires, including the three system columns it stamps and the order it puts
    them in. Two nodes that agreed only on paper would fail here."""
    types = _expected_types(plan)
    key = plan.entity_key_columns[0]
    spine = fake_spark.DataFrame(
        [{key: "C1", plan.business_dt_column: BUSINESS_DT}],
        [key, plan.business_dt_column],
        {key: types[key], plan.business_dt_column: types[plan.business_dt_column]})
    staged = {
        column: fake_spark.DataFrame(
            [{key: "C1", plan.business_dt_column: BUSINESS_DT, column: 1}],
            [key, plan.business_dt_column, column],
            {key: types[key], plan.business_dt_column: types[plan.business_dt_column],
             column: types[column]})
        for column in _columns(plan)}
    assembled = _run_assembly(assembly, plan, materialized, spine=spine, staged=staged)
    assert _run_gate(gate, assembled).columns == [c.name for c in expected_schema(plan)]
