"""C1 — the engine capability registry is TRUE, not aspirational.

The advertisement's whole job is to be believed at activation time, so each of its claims is
proved against the renderer itself: every advertised aggregation is rendered into a calculation
node and EXECUTED through ``fake_spark.run_rendered``, the converse direction pins that nothing
renderable goes unadvertised, and the two boolean advertisements are held to the render package's
actual source. A hand-typed capability set would pass none of the first three by accident — and
the last test refuses one structurally.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import pathlib

import pytest
from tests.featuregen.materialize import fake_spark
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_render_nodes_compute import (
    BUSINESS_DT,
    MANIFEST,
    PROJECTION,
    STAGING,
    _debit,
    _spine_rows,
    compiled,  # noqa: F401 — fixture re-export
    feature,  # noqa: F401 — fixture re-export
    lock_tree,  # noqa: F401 — fixture re-export
)

from featuregen.formula.schema import AggregateFunction, EmptyWindowResult, NullInput
from featuregen.formula.schema_v2 import AggregateFunctionV2
from featuregen.materialize import engine_capability
from featuregen.materialize.engine_capability import (
    KEDRO_PYSPARK_ENGINE,
    engine_capability_for,
)
from featuregen.materialize.expression_ir import RefRole
from featuregen.materialize.render import nodes_compute


def _with_aggregation(ir, aggregation: AggregateFunctionV2):
    """The compiled SUM IR, re-aggregated — everything else stays exactly what was compiled.

    ``COUNT_ROWS`` additionally sheds the OPERAND role from its read set, because Child-1's
    grammar gives it no operand at all and the renderer refuses a read set that names one.
    """
    expression = ir.expressions[0]
    if aggregation is AggregateFunctionV2.COUNT_ROWS:
        stripped = tuple(
            dataclasses.replace(
                ref, roles=tuple(role for role in ref.roles if role is not RefRole.OPERAND))
            for ref in expression.physical_read_set)
        expression = dataclasses.replace(expression, physical_read_set=stripped)
    expression = dataclasses.replace(expression, aggregation=aggregation)
    return dataclasses.replace(ir, expressions=(expression,))


def _staged_values(compiled, feature, lock_tree, aggregation):  # noqa: F811
    node = nodes_compute.render_calculation_node(
        _with_aggregation(compiled[0].irs[0], aggregation), feature, compiled[1],
        empty_window={"body.expr": EmptyWindowResult.NULL},
        null_input={"body.expr": NullInput.IGNORE},
        projection_datasets={"body.expr": PROJECTION}, spine_dataset="primary_spine",
        staging_dataset=STAGING, manifest_dataset=MANIFEST)
    run_node = fake_spark.run_rendered(node.source, node.func_name, module_file=lock_tree)
    projection = [
        _debit("C1", 10), _debit("C1", 10), _debit("C1", None),
        _debit("C2", 7),
    ]
    frame, _manifest = run_node(
        fake_spark.DataFrame(projection),
        fake_spark.DataFrame(_spine_rows("C1", "C2"), columns=["cif_id", "business_dt"]),
        BUSINESS_DT, "gen-0001", "run-0001", "exec-0001", "hdfs://nn/staging/gen-0001")
    return {row["cif_id"]: row[SUM_30D] for row in frame.rows}


#: What each advertised aggregation must ANSWER over the same four projection rows — two 10s and a
#: NULL for C1 (``null_input=IGNORE``), one 7 for C2. Four different numbers out of one input is
#: what tells the four rendering paths apart; a dispatch that wired two members to the same Spark
#: call would collide on at least one row here.
# Keyed on V2's members, like the renderer's own dispatch. A V1-keyed map answered a V2 lookup
# anyway — the two enums hash equal — so keying it either way "worked", which is exactly why the
# one that matches the code under test is the one to use.
_EXPECTED = {
    AggregateFunctionV2.SUM: {"C1": 20, "C2": 7},
    AggregateFunctionV2.COUNT_NON_NULL: {"C1": 2, "C2": 1},
    AggregateFunctionV2.COUNT_DISTINCT: {"C1": 1, "C2": 1},
    AggregateFunctionV2.COUNT_ROWS: {"C1": 3, "C2": 1},
}


@pytest.mark.parametrize("aggregation", sorted(KEDRO_PYSPARK_ENGINE.supported_aggregations))
def test_every_advertised_aggregation_has_a_rendering_path(
        compiled, feature, lock_tree, aggregation):  # noqa: F811
    """Render each advertised member and RUN it — the advertisement is executable, not prose."""
    member = AggregateFunctionV2(aggregation)
    values = _staged_values(compiled, feature, lock_tree, member)
    expected = _EXPECTED[member]
    assert {cif: (value if value is None else int(value))
            for cif, value in values.items()} == expected, member


def test_every_renderable_aggregation_is_advertised():
    """The converse — the pair is exhaustive in both directions, member for member."""
    renderable = {member.value for member in nodes_compute.renderable_aggregations()}
    assert renderable == KEDRO_PYSPARK_ENGINE.supported_aggregations
    # And the renderer's own self-description covers Child-1's v1 vocabulary exactly: every v1
    # member renders. The v2-only vocabulary is NOT advertised — that gap is the whole point of
    # the engine arm (`unsupported_engine`), so pin a representative slice of it.
    assert renderable == {member.value for member in AggregateFunction}
    for v2_only in ("avg", "min", "max", "percentile", "last_known", "slope"):
        assert AggregateFunctionV2(v2_only).value not in renderable


def test_window_offset_and_future_horizon_advertisements_match_the_renderer():
    """The booleans are held to the render package's SOURCE, not to anyone's intention.

    Today neither concept appears anywhere under ``materialize/render/`` and both advertisements
    are False. The day offset (or horizon) rendering lands, this test fails — which is the
    designed prompt to revisit the advertisement, instead of the capability staying silently
    unadvertised (or worse, advertised on faith).
    """
    render_dir = pathlib.Path(inspect.getfile(nodes_compute)).parent
    sources = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(render_dir.glob("*.py")))
    renders_offsets = "offset_periods" in sources
    renders_horizons = "future_horizon" in sources
    assert KEDRO_PYSPARK_ENGINE.supports_window_offset is renders_offsets is False
    assert KEDRO_PYSPARK_ENGINE.supports_future_horizon is renders_horizons is False


def test_no_engine_capability_is_asserted_by_hand():
    """The constant is BUILT — structurally. A hand-typed aggregation set cannot parse as one.

    The AST of ``engine_capability.py`` must show ``supported_aggregations`` given as a call over
    the renderer's self-description, and the module must contain no aggregation-name string at
    all — the only way a member gets in is by having a rendering.
    """
    tree = ast.parse(pathlib.Path(inspect.getfile(engine_capability)).read_text(encoding="utf-8"))
    assignment = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "KEDRO_PYSPARK_ENGINE"
                for target in node.targets))
    supported = next(keyword.value for keyword in assignment.value.keywords
                     if keyword.arg == "supported_aggregations")
    assert isinstance(supported, ast.Call), ast.dump(supported)
    aggregation_names = {member.value for member in AggregateFunctionV2}
    hand_typed = [node.value for node in ast.walk(tree)
                  if isinstance(node, ast.Constant) and node.value in aggregation_names]
    assert hand_typed == []


def test_an_unknown_engine_resolves_to_None_never_a_default():
    assert engine_capability_for("kedro-pyspark") is KEDRO_PYSPARK_ENGINE
    assert engine_capability_for("dbt-duckdb") is None
    assert engine_capability_for("") is None
