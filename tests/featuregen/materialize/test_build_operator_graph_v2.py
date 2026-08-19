"""Step 7 — turning a planned V2 feature into its operator graph.

The vocabulary has been shipped, validated and hand-built in tests since C-C10a, and no function
anywhere produced one. This is that function, and it lands BEFORE the pilot because the pilot is the
first thing that can be wrong in an interesting way — and it needs a graph to be wrong about.

What these tests hold:

1. **The pilot's shape is built, end to end**, and the terminal is the group assembly.
2. **The population is consulted BEFORE the terms are combined**, so a key with no in-window rows
   still reaches the combination.
3. **What cannot be expressed faithfully REFUSES BY NAME** — FX above all, because §7 rules the rate
   relation belongs to the policy realization and a builder that chose one would choose plausibly.
4. **The empty-window value is required, never picked**: "no transactions" and "transacted zero" are
   different published answers.
5. **Identity is content-derived**, so the same IR is the same graph.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize.test_chain_v2_s6 import _planned, _v2_ir
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    DECLARATION,
    INVENTORY,
    TXN_AMT,
    _admitted,
    _col,
    _table_node,
    compile_ir,
    seed_catalog,
)

from featuregen.formula.schema_v2 import AggregateFunctionV2, AuthorityRefsV2
from featuregen.materialize.boundary_v2 import PlannedFormulaExecutionIRV2
from featuregen.materialize.build_operator_graph_v2 import build_operator_graph_v2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.operator_graph_v2 import OperatorKindV2, graph_hash_v2


@pytest.fixture
def catalog(db):
    seed_catalog(db)
    for column in ("rate", "quote_dt", "ccy"):
        _col(db, "fx_rates", column)
    _table_node(db, "fx_rates")
    return db


@pytest.fixture
def v1_ir(catalog):
    return compile_ir(catalog, _admitted("total_debit_amount_30d"), roles=_ROLES,
                      spine_decl=DECLARATION, inventory=INVENTORY)


def _build(planned, *, empty_value="0"):
    return build_operator_graph_v2(planned, empty_value=empty_value)


# ══ THE PILOT SHAPE ════════════════════════════════════════════════════════════════════════════
def test_A_SUM_FEATURE_BUILDS_THE_WHOLE_CHAIN(catalog, v1_ir):
    """Scan, restrict to the window, select the declared rows, aggregate, land, combine, assemble."""
    graph = _build(_planned(v1_ir))

    assert not isinstance(graph, MaterializationRefused), graph
    assert graph.kinds == {
        OperatorKindV2.GOVERNED_SCAN,
        OperatorKindV2.PIT_AVAILABILITY_FILTER,
        OperatorKindV2.SEMANTIC_SELECTION,
        OperatorKindV2.AGGREGATE,
        OperatorKindV2.SPINE_LEFT_JOIN,
        OperatorKindV2.FINAL_COMBINE,
        OperatorKindV2.GROUP_ASSEMBLY,
    }
    assert graph.terminal.kind is OperatorKindV2.GROUP_ASSEMBLY


def test_THE_POPULATION_IS_CONSULTED_BEFORE_THE_TERMS_ARE_COMBINED(catalog, v1_ir):
    """Order, asserted on the EDGES rather than on a list — a ratio over a key with no in-window
    rows must combine two declared empties, not disappear before the population was ever read."""
    graph = _build(_planned(v1_ir))

    combine = next(n for n in graph.nodes if n.kind is OperatorKindV2.FINAL_COMBINE)
    landed = next(n for n in graph.nodes if n.kind is OperatorKindV2.SPINE_LEFT_JOIN)
    aggregate = next(n for n in graph.nodes if n.kind is OperatorKindV2.AGGREGATE)

    assert combine.inputs == (landed.node_id,)
    assert landed.inputs == (aggregate.node_id,)


def test_the_AGGREGATE_OPERAND_comes_from_its_RECORDED_ROLE(catalog, v1_ir):
    """Not from position: the same column is legitimately read as the operand and as the window's
    clock, and a builder picking by order would eventually sum a timestamp."""
    graph = _build(_planned(v1_ir))
    aggregate = next(n for n in graph.nodes if n.kind is OperatorKindV2.AGGREGATE)

    assert aggregate.payload.function is AggregateFunctionV2.SUM
    assert aggregate.payload.operand_ref == TXN_AMT


def test_the_SCAN_READS_ONE_RELATION_and_its_columns(catalog, v1_ir):
    graph = _build(_planned(v1_ir))
    scan = next(n for n in graph.nodes if n.kind is OperatorKindV2.GOVERNED_SCAN)

    assert scan.payload.table_ref == "hdfc::public.transactions"
    assert TXN_AMT in scan.payload.column_refs
    # The relation itself is NOT a column read: it carries no column, and a scan listing its own
    # table among its columns would ask the engine for a column that does not exist.
    assert scan.payload.table_ref not in scan.payload.column_refs


# ══ WHAT IT WILL NOT INVENT ════════════════════════════════════════════════════════════════════
def test_A_CURRENCY_CONVERSION_REFUSES(catalog, v1_ir):
    """§7's ruling, enforced where it would be broken. The realization owns the rate relation; a
    builder that picked a rate table would be the second source of truth that ruling removes — and
    it would pick one that looked right, which is why this is a refusal and not a TODO."""
    refused = _build(_planned(v1_ir, converted=True))

    assert isinstance(refused, MaterializationRefused)
    assert "second source of truth" in refused.detail


def test_A_LINKED_REVERSAL_REFUSES(catalog, v1_ir):
    """Four facts are required of that subgraph and all four live in the policy payload, which the
    IR does not carry. A node built without them claims a governed reversal and applies none."""
    ir = _v2_ir(v1_ir, refs=AuthorityRefsV2(
        status_policy_ref="", direction_policy_ref="",
        reversal_policy_ref="reversal:foundation-linked", currency_conversion_ref=""))
    refused = _build(PlannedFormulaExecutionIRV2.plan(
        ir, policy_reads=(_reversal_read(),)))

    assert isinstance(refused, MaterializationRefused)
    assert "ambiguity gate" in refused.detail


def test_a_DECLARED_STATUS_POLICY_IS_CARRIED(catalog, v1_ir):
    """The discriminator for the two refusals above: status IS expressible, because the operator
    holds the ref and nothing else — emitting it invents nothing."""
    ir = _v2_ir(v1_ir, refs=AuthorityRefsV2(
        status_policy_ref="eligible_status:foundation-posted", direction_policy_ref="",
        reversal_policy_ref="", currency_conversion_ref=""))
    graph = _build(PlannedFormulaExecutionIRV2.plan(ir, policy_reads=(_status_read(),)))

    assert not isinstance(graph, MaterializationRefused), graph
    node = next(n for n in graph.nodes if n.kind is OperatorKindV2.ELIGIBLE_STATUS_FILTER)
    assert node.payload.status_policy_ref == "eligible_status:foundation-posted"


def test_a_MULTI_RELATION_EXPRESSION_REFUSES(catalog, v1_ir):
    """A governed scan reads exactly one relation, and the joins between two are operators this
    vocabulary does not have. Guessing which to scan would scan a table nobody resolved."""
    planned = _planned(v1_ir)
    doubled = dataclasses.replace(
        planned.ir.expressions[0],
        physical_read_set=planned.ir.expressions[0].physical_read_set * 2)
    ir = dataclasses.replace(planned.ir, expressions=(doubled,))
    refused = _build(PlannedFormulaExecutionIRV2.plan(ir, policy_reads=()))

    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED
    assert "exactly one" in refused.detail


# ══ THE EMPTY-WINDOW VALUE IS DECLARED, NEVER PICKED ═══════════════════════════════════════════
def test_EMPTY_VALUE_HAS_NO_DEFAULT():
    """"This account had no transactions" and "this account transacted zero" are different
    published answers, and the IR does not carry which one the author declared."""
    with pytest.raises(TypeError):
        build_operator_graph_v2(object())                        # type: ignore[call-arg]


def test_NULL_AND_ZERO_ARE_DIFFERENT_GRAPHS(catalog, v1_ir):
    """So a build that silently defaulted could never be mistaken for one that was declared."""
    zero = _build(_planned(v1_ir), empty_value="0")
    null = _build(_planned(v1_ir), empty_value=None)

    assert graph_hash_v2(zero) != graph_hash_v2(null)


# ══ IDENTITY IS CONTENT-DERIVED ════════════════════════════════════════════════════════════════
def test_THE_SAME_IR_IS_THE_SAME_GRAPH(catalog, v1_ir):
    """Determinism is structural rather than promised: node ids derive from content, so append
    order cannot leak into the hash."""
    planned = _planned(v1_ir)
    assert graph_hash_v2(_build(planned)) == graph_hash_v2(_build(planned))


def _status_read():
    from tests.featuregen.materialize.test_ir import CUSTOMERS

    from featuregen.materialize.boundary_v2 import (
        KnowledgeTimeBasisV2,
        PolicyReadV2,
        TemporalReadV2,
    )

    return PolicyReadV2(
        policy_ref="eligible_status:foundation-posted", role="status",
        logical_ref=f"{CUSTOMERS}.status_cd",
        temporal=TemporalReadV2(basis=KnowledgeTimeBasisV2.EVENT_TIME, declared_promise=None))


def _reversal_read():
    from tests.featuregen.materialize.test_ir import CUSTOMERS

    from featuregen.materialize.boundary_v2 import (
        KnowledgeTimeBasisV2,
        PolicyReadV2,
        TemporalReadV2,
    )

    return PolicyReadV2(
        policy_ref="reversal:foundation-linked", role="reversal",
        logical_ref=f"{CUSTOMERS}.status_cd",
        temporal=TemporalReadV2(basis=KnowledgeTimeBasisV2.EVENT_TIME, declared_promise=None))
