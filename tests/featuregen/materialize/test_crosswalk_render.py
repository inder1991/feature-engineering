"""Release C Task 12 — the generated project for a two-leg crosswalk, and its acceptance.

Every stage here is the SHIPPED one: ``compile_ir`` plans the traversal, Gate 2 authorizes the union
of three tables' read sets across two catalogs, §5/§6/§10 derive the contract, the type and the
plan, ``compile.wiring.assemble_nodes`` builds the node sequence and ``render_project`` seals it.
Nothing is hand-wired, because a hand-wired project proves what the fixture author believed rather
than what the compiler produces.

WHAT THIS SUITE IS ABOUT: two joins that are never collapsed into endpoint equality; a mapping row
filter applied BEFORE the uniqueness/fan-out gate, which is itself before any aggregation; the
population-spine left joins preserved so a zero-event entity survives; every pinned revision in the
project's own metadata; and an honest statement of what a failure actually leaves behind.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import json
import os
import re

import pytest
from tests.featuregen.materialize import crosswalk_fixtures as cf
from tests.featuregen.materialize import fake_spark, fixtures

from featuregen.materialize import identity as identity_module
from featuregen.materialize.codes import MaterializationRefused, ValidationGateCode
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.identity import GENERATED_LOCK_FILENAME, read_lock
from featuregen.materialize.ir import authorize_execution_realizations
from featuregen.materialize.joins import CrosswalkJoinStepV1
from featuregen.materialize.publish import PublisherSelection, PublishMechanism
from featuregen.materialize.render.nodes_compute import render_projection_node
from featuregen.materialize.render.publish import publish_entry_body
from featuregen.materialize.validation import ValidationStatus, run_l0

_ROLES = ("feature_engineer",)


@pytest.fixture(autouse=True)
def crosswalk_flag(monkeypatch):
    for name in ("FEATUREGEN_DATASET_PROFILES", "FEATUREGEN_SOURCE_TEMPORAL_SELECTION",
                 "FEATUREGEN_CROSSWALK_EXECUTION"):
        monkeypatch.setenv(name, "1")
    return monkeypatch


@pytest.fixture
def inputs(db):
    cf.seed_catalog(db)
    return cf.compiled_group(db)


@pytest.fixture
def nodes(inputs):
    return assemble_nodes(inputs)


@pytest.fixture
def projection(nodes):
    return next(node for node in nodes if node.name.startswith("project_"))


@pytest.fixture
def project(inputs):
    return cf.render(inputs)


def _lines(source: str) -> list[str]:
    """Executable lines only — a comment that merely NAMES a gate must not satisfy an assertion."""
    return [line for line in source.splitlines() if line.strip() and not line.strip().startswith("#")]


def _index(source: str, needle: str) -> int:
    lines = _lines(source)
    for position, line in enumerate(lines):
        if needle in line:
            return position
    raise AssertionError(f"{needle!r} is not in the rendered body:\n" + "\n".join(lines))


# ══ two joins, never one ═════════════════════════════════════════════════════════════════════════


def test_the_project_renders_TWO_joins(projection) -> None:
    joins = [line for line in _lines(projection.source) if ".join(" in line]
    assert len(joins) == 2, joins
    assert all("'left'" in line for line in joins), joins


def test_the_two_endpoints_are_NEVER_joined_to_each_other(projection) -> None:
    """The mutation Task 13 names first: render endpoint equality.

    Structurally unreachable rather than merely absent — every join key is an aliased hop key, and
    the mapping table is a frame in its own right on both sides.
    """
    source = projection.source
    assert "acct_xref" in source
    for line in _lines(source):
        if ".join(" not in line:
            continue
        assert "__join_" in line
    # Neither endpoint column ever appears on both sides of one equality.
    assert not re.search(r"F\.col\('acct_no'\)\s*==\s*F\.col\('counter_party_acct_no'\)", source)
    assert not re.search(r"F\.col\('counter_party_acct_no'\)\s*==\s*F\.col\('acct_no'\)", source)


def test_dropping_one_leg_is_refused_rather_than_rendered(inputs) -> None:
    """The 'omit one leg' mutation. A one-step crosswalk plan cannot reach the mapping's target."""
    ir = inputs.authorized.irs[0]
    expression = ir.expressions[0]
    one_leg = dataclasses.replace(
        expression,
        join_plan=dataclasses.replace(
            expression.join_plan, steps=(expression.join_plan.steps[0],)))
    with pytest.raises(ValueError):
        render_projection_node(
            one_leg, inputs.contract, feature_column=cf.FEATURE,
            source_dataset="raw_source", projection_dataset="projected",
            joined_datasets={f"cib::{cf.CIB_SCHEMA}.{cf.MAP_TABLE}": "raw_map",
                             f"ftr::{cf.FTR_SCHEMA}.{cf.FTR_TABLE}": "raw_target"})


# ══ the mapping row filter runs BEFORE the gate, and the gate before any aggregation ═════════════


def test_the_mapping_row_filter_precedes_the_uniqueness_gate_and_the_join(projection) -> None:
    source = projection.source
    filter_line = _index(source, "hop_1_scoped = join_1_acct_xref.where(")
    gate_line = _index(source, "duplicate_1 = ")
    join_line = _index(source, "rows.join(hop_1")
    assert filter_line < gate_line < join_line
    # Both halves of the half-open interval reach the frame before the gate reads it.
    assert _index(source, "valid_to") < gate_line


def test_the_composed_amplification_gate_SPANS_both_legs(projection) -> None:
    """A crosswalk's fan-out verdict describes the PAIR, so the runtime check must too."""
    source = projection.source
    opened = _index(source, "rows_before_crosswalk = rows.count()")
    first_join = _index(source, "rows.join(hop_1")
    second_join = _index(source, "rows.join(hop_2")
    closed = _index(source, "if rows.count() > rows_before_crosswalk:")
    assert opened < first_join < second_join < closed
    assert ValidationGateCode.JOIN_AMPLIFICATION.value in source


def test_the_composed_gate_names_the_execution_revision_it_enforces(inputs, projection) -> None:
    (step,) = {s for ir in inputs.authorized.irs for e in ir.expressions
               for s in e.join_plan.steps if isinstance(s, CrosswalkJoinStepV1)
               and s.arrives_at_mapping}
    assert step.crosswalk_execution_revision_id in projection.source


def test_no_aggregation_happens_before_the_gate(projection, nodes) -> None:
    """The projection AGGREGATES NOTHING — the only groupBy in it is a uniqueness gate — and the
    calculation node that does aggregate consumes the projection's OUTPUT, so the gate cannot be
    reordered after it without rewiring the pipeline."""
    grouped = [line for line in _lines(projection.source) if ".groupBy(" in line]
    assert grouped and all("count()" in line for line in grouped), grouped
    assert ".agg(" not in projection.source

    calculation = next(node for node in nodes if node.name.startswith("calculate_"))
    assert ".agg(" in calculation.source
    assert set(projection.outputs) <= set(calculation.inputs)


# ══ the population spine, and the entity with no events ══════════════════════════════════════════


def test_every_crosswalk_hop_is_a_LEFT_join_and_an_unmatched_row_keeps_a_null_key(
        inputs, projection) -> None:
    execute = fake_spark.run_rendered(projection.source, projection.func_name)
    rows = execute(
        fake_spark.DataFrame([
            {"acct_no": "A1", "opened_dt": _BEFORE, "load_ts": _LONG_AGO},
            {"acct_no": "A404", "opened_dt": _BEFORE, "load_ts": _LONG_AGO},
        ]),
        fake_spark.DataFrame([
            {"acct_no": "A1", "ext_acct_ref": "X1",
             "valid_from": _LONG_AGO, "valid_to": _FAR_FUTURE},
            # A row of the SAME mapping key that the row rule excludes: were the filter applied
            # after the gate, this would be a duplicate key and the gate would refuse.
            {"acct_no": "A1", "ext_acct_ref": "X9",
             "valid_from": _LONG_AGO, "valid_to": _LONG_AGO},
        ]),
        fake_spark.DataFrame([{"counter_party_acct_no": "X1"}]),
        {cf.REPORT_CUTOFF_REF: _CUTOFF},
        _BUSINESS_DT,
    ).rows
    assert [(row["acct_no"], row["counter_party_acct_no"]) for row in rows] == [
        ("A1", "X1"), ("A404", None)]


def test_the_spine_reduction_is_still_a_LEFT_join_so_a_zero_event_entity_survives(
        nodes, projection, tmp_path) -> None:
    """The pilot acceptance, EXTENDED to a crosswalk and EXECUTED rather than read off the text.

    An FTR account that no CIB account maps to through the mapping table stays in the published
    population with a NULL value. That is the whole reason every hop and the spine reduction are
    LEFT: the traversal says which entity a row belongs to, it does not decide which entities
    exist, and an inner join anywhere on this path would silently shrink the population on a
    referential-integrity fact nobody governed.
    """
    calculation = next(node for node in nodes if node.name.startswith("calculate_"))
    joins = [line for line in _lines(calculation.source) if ".join(" in line]
    assert joins and all("how='left'" in line for line in joins), joins

    projected = fake_spark.run_rendered(projection.source, projection.func_name)(
        fake_spark.DataFrame([{"acct_no": "A1", "opened_dt": _BEFORE, "load_ts": _LONG_AGO}]),
        fake_spark.DataFrame([{"acct_no": "A1", "ext_acct_ref": "X1",
                               "valid_from": _LONG_AGO, "valid_to": _FAR_FUTURE}]),
        fake_spark.DataFrame([{"counter_party_acct_no": "X1"}]),
        {cf.REPORT_CUTOFF_REF: _CUTOFF},
        _BUSINESS_DT,
    ).rows

    lock_root = tmp_path / "src" / "pkg" / "pipelines" / "materialize"
    lock_root.mkdir(parents=True)
    (lock_root / "nodes.py").write_text("", encoding="utf-8")
    (tmp_path / GENERATED_LOCK_FILENAME).write_text(
        json.dumps({"compilation": {}, "generated_project_hash": "project-hash-cwx"}),
        encoding="utf-8")
    staged, _manifest = fake_spark.run_rendered(
        calculation.source, calculation.func_name,
        module_file=str(lock_root / "nodes.py"))(
        fake_spark.DataFrame(projected),
        fake_spark.DataFrame(
            # X1 is reachable through the mapping; X404 is the ZERO-EVENT entity.
            [{"counter_party_acct_no": key, "business_dt": dt.date.fromisoformat(_BUSINESS_DT)}
             for key in ("X1", "X404")],
            columns=["counter_party_acct_no", "business_dt"]),
        _BUSINESS_DT, "gen-cwx", "run-cwx", "exec-cwx", str(tmp_path / "staging"))
    landed = {row["counter_party_acct_no"]: row[cf.FEATURE] for row in staged.rows}
    assert landed == {"X1": 1, "X404": None}


# ══ every pinned revision reaches the artifact ═══════════════════════════════════════════════════


def test_every_pinned_revision_lands_in_the_projects_identity(inputs, project) -> None:
    package = f"src/sandbox_feature_{cf.GROUP}/__init__.py"
    rendered = project.files[package]
    admitted = cf.admitted()
    for pinned in (
        admitted.execution.execution_revision_id,
        cf.DEFINITION.revision_id,
        cf.MAP_BINDING.binding_revision_id,
        cf.TEMPORAL_POLICY,
        admitted.execution.composition_observation_revision_id,
    ):
        assert pinned in rendered, pinned
    # The cross-catalog LEG's realization is pinned as an execution dependency too, so the same
    # revalidation a direct bridge gets applies to it.
    assert admitted.execution.target_leg.realization_revision_ids[0] in rendered


def test_the_lock_round_trips_the_crosswalk_pins(project) -> None:
    restored = read_lock(project.files[GENERATED_LOCK_FILENAME])
    assert len(restored.compilation.crosswalk_execution_pins) == 1
    assert restored.compilation.identity_payload() == (
        identity_module.CompilationIdentity(**dataclasses.asdict(
            restored.compilation)).identity_payload())


def test_the_lineage_names_the_row_rule_and_the_measurement_it_was_taken_under(
        projection) -> None:
    """A reader of the generated project must be able to look up WHICH rows the uniqueness was
    proved over without re-deriving a hash."""
    admitted = cf.admitted()
    assert cf.TEMPORAL_POLICY in projection.source
    assert admitted.mapping_row_selection.content_hash in projection.source
    assert admitted.execution.composition_observation_revision_id in projection.source
    for measurement in admitted.execution.leg_measurement_ids:
        assert measurement in projection.source


def test_the_run_parameter_for_the_row_rule_is_declared_everywhere_it_must_be(
        projection, project) -> None:
    assert f"params:{cf.REPORT_CUTOFF_REF}" not in projection.inputs
    assert "params:crosswalk_row_values" in projection.inputs
    assert "crosswalk_row_values" in project.files["conf/base/parameters.yml"]
    assert "crosswalk_row_values" in project.files["README.md"]
    # And the node refuses BEFORE reading rows when the value is absent.
    assert "missing_crosswalk_values" in projection.source


# ══ the project builds ═══════════════════════════════════════════════════════════════════════════


def test_every_rendered_python_file_parses(project) -> None:
    for path, text in sorted(project.files.items()):
        if path.endswith(".py"):
            ast.parse(text, filename=path)


def test_the_l0_build_proof(project, tmp_path) -> None:
    """Phase G's L0 gate, run for real IF an interpreter with kedro + pyspark is configured.

    SKIPPED HONESTLY otherwise, naming the variable: a build proof nobody ran must never be
    reported as one that passed.
    """
    interpreter = (os.environ.get("FEATUREGEN_MATERIALIZE_L0_PYTHON")
                   or os.environ.get("FEATUREGEN_L0_PYTHON"))
    if not interpreter:
        pytest.skip(
            "no L0 environment: set FEATUREGEN_MATERIALIZE_L0_PYTHON to an interpreter carrying "
            "kedro + pyspark (and a JVM). The rendered project is AST-parsed by the test above; "
            "what only L0 can prove is that kedro can CONSTRUCT its pipeline.")
    from featuregen.materialize.render.project import materialize_to

    root = materialize_to(project, tmp_path / "project")
    report = run_l0(
        root, generation_id="gen-cwx-0001", environment_id=cf.ENVIRONMENT,
        report_id="l0-cwx-0001", python_executable=interpreter,
        clock=lambda: "2026-08-04T12:00:00+00:00", timeout_seconds=900)
    assert report.status is ValidationStatus.PASSED, report


# ══ failure semantics — what publish ACTUALLY guarantees ═════════════════════════════════════════


def test_publication_is_refused_and_nothing_claims_otherwise(inputs) -> None:
    """THE HONEST CLAIM, checked against the code rather than restated from a plan.

    The checklist item this replaces reads "a failure leaves the last published feature partition
    unchanged". That sentence is vacuous here and Phase G is why: there is no publish step at all.
    ``probe_publication_capability`` does not exist, so ``select_publisher`` can only ever answer
    ``CAPABILITY_UNPROVEN``, and ``compile.chain`` is forbidden from ever appending
    ``RunEventKind.PUBLISHED`` — its truthful terminal is ``PUBLICATION_REFUSED`` carrying that
    code. Nothing is published, so no published partition exists to be left unchanged.

    What IS guaranteed, and what this asserts instead: the write target is GENERATION-SCOPED.
    ``staging_root`` resolves to ``<base>/<generation_id>``, so a crosswalk run writes where no
    previous generation ever wrote, and ``errorifexists`` refuses to write over its own output. A
    failed run therefore cannot damage another generation's evidence — which is a claim about this
    project's bytes, and is checkable.
    """
    import featuregen.materialize.publish as publish_module

    assert not hasattr(publish_module, "probe_publication_capability")
    body = "\n".join(publish_entry_body(
        inputs.plan, selection=PublisherSelection(
            environment_id=cf.ENVIRONMENT, mechanism=PublishMechanism.VERSIONED_POINTER,
            capability_attestation_id="att-cwx", adds_feature=False,
            engine_versions=fixtures.ENGINE_VERSIONS)))
    assert "${runtime_params:staging_root}/published/" in body
    assert 'mode: "errorifexists"' in body


def test_the_flag_off_project_is_the_one_that_was_always_rendered(db) -> None:
    """Crosswalks are ADDITIVE: with the flag off this group does not compile at all, because its
    grain is unreachable — the same answer as before crosswalks existed, not a degraded project."""
    cf.seed_catalog(db)
    os.environ.pop("FEATUREGEN_CROSSWALK_EXECUTION", None)
    with pytest.raises(AssertionError):
        cf.compiled_group(db)


# ══ the stale-pin doctrine, extended ═════════════════════════════════════════════════════════════


def test_a_stale_leg_realization_refuses_at_compile_revalidation(db, inputs) -> None:
    """``authorize_execution_realizations``' doctrine, reaching a crosswalk leg for free.

    The cross-catalog leg's ``JoinLegPinV1`` names a realization revision and its dependency
    snapshot, ``bridge_realization_dependencies`` collects them, and the final pre-run check
    resolves each BY REVISION against the current pointer. The fixture's leg pins a realization
    this catalog has never held, which is exactly the shape a leg whose pointer advanced after
    compilation takes.
    """
    refused = authorize_execution_realizations(
        db, inputs.authorized, environment_id=cf.ENVIRONMENT)
    assert isinstance(refused, MaterializationRefused)
    assert cf.admitted().execution.target_leg.realization_revision_ids[0] in refused.detail


_BUSINESS_DT = "2026-08-04"
#: Inside the trailing 30-day window, and long since available — so what the execution test
#: measures is the TRAVERSAL and not the point-in-time gates, which have their own suite.
_BEFORE = dt.datetime(2026, 7, 26, 12, 0)
_LONG_AGO = dt.datetime(2000, 1, 1)
#: The value the run binds the mapping row rule's parameter to.
_CUTOFF = dt.datetime(2026, 8, 4, 0, 0)
#: The open interval's high sentinel. NOT ``None``: the pinned row rule's own operator is ``>``
#: (``temporal_resolver._predicates_for``), and a NULL compares to nothing — so a mapping row whose
#: interval is left open with a NULL would be excluded. That is Release B's rule travelling
#: unchanged, which is the point of pinning it, and it is recorded here rather than papered over.
_FAR_FUTURE = dt.datetime(9999, 12, 31)
