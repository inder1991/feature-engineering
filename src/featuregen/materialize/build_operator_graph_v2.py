"""Step 7 — the MINIMUM deterministic operator graph for one planned V2 feature.

**The vocabulary existed and nothing produced it.** ``OperatorGraphV2`` and its fourteen operators
have been shipped, validated and hand-built in tests since C-C10a; no function anywhere turns a
compiled feature into one. This is that function, and it comes BEFORE the pilot deliberately: the
pilot is the first thing that can be wrong in an interesting way, and it needs a graph to be wrong
about.

**Minimum means what it says.** This builds the smallest graph that faithfully expresses what the IR
already decided — scan, point-in-time window, declared selections, declared status policy, aggregate,
land on the population, combine, assemble. Everything it cannot express *faithfully* refuses BY NAME
rather than being approximated:

* a **currency conversion**, because §7 rules that the policy realization owns the rate relation and
  the graph carries only its resolved binding. A builder that chose a rate table would be the second
  source of truth that ruling exists to prevent — and it would choose one that looked right;
* a **linked reversal**, because C-C10 requires four facts of that subgraph (as-of population,
  linkage, ambiguity gate, survivor) and they live in the policy payload, which the IR does not
  carry;
* a **multi-relation expression**, because ``GovernedScanV2`` scans one relation and the joins
  between two are operators this vocabulary does not have.

**Nothing is defaulted, including the empty-window value.** ``empty_value`` is a required argument
with no default: the IR does not carry the declared empty-window result (§8.4 records that wiring
reads it off the formula), and a builder that picked ``"0"`` would turn "this account had no
transactions" into "this account transacted zero" for every feature whose author declared NULL.

**Determinism is structural, not promised.** Node ids are content-derived, so the same IR builds the
same graph whatever order this function happens to append nodes in — and two builders producing the
same edges produce the same ``graph_hash_v2``.
"""
from __future__ import annotations

from featuregen.formula.schema_v2 import AggregateFunctionV2
from featuregen.materialize.boundary_v2 import PlannedFormulaExecutionIRV2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import ExpressionExecutionIR, RefRole
from featuregen.materialize.operator_graph_v2 import (
    AggregateV2,
    EligibleStatusFilterV2,
    FinalCombineV2,
    GovernedScanV2,
    GroupAssemblyV2,
    OperatorGraphV2,
    OperatorKindV2,
    OperatorNodeV2,
    PitAvailabilityFilterV2,
    SemanticSelectionV2,
    SpineLeftJoinV2,
)

__all__ = ["build_operator_graph_v2"]

#: Declared policy roles this builder can express, and what it would take to express the rest.
#: Status is a faithful CARRY — `EligibleStatusFilterV2` holds the ref and nothing else, so emitting
#: it invents nothing. The other two need facts that live in the resolved payload, and inventing
#: those is exactly what §7's FX ruling forbids.
_UNBUILDABLE_ROLES = {
    "reversal": ("a linked reversal needs four facts — as-of population, linkage, ambiguity gate "
                 "and survivor rule — and all four live in the policy payload, which the IR does "
                 "not carry. A node built without them would claim a governed reversal and apply "
                 "none of it"),
    "currency_conversion": ("§7 rules that the policy realization owns the rate relation and the "
                            "graph carries only its RESOLVED binding. A builder that picked a rate "
                            "table here would be the second source of truth that ruling removes, "
                            "and it would pick one that looked right"),
}


def build_operator_graph_v2(
    planned: PlannedFormulaExecutionIRV2, *, empty_value: str | None,
) -> OperatorGraphV2 | MaterializationRefused:
    """The operator DAG for ONE planned feature, or a refusal naming what cannot be expressed.

    Args:
        planned: the planned IR — planned rather than bare so the graph is built from the same
            object the gates authorized, never from a re-derivation of it.
        empty_value: what a population key with no in-window rows carries, from the formula's
            declared empty-window result. REQUIRED and possibly ``None`` (meaning NULL): the two are
            different published answers and the caller is the only one holding the declaration.

    Returns:
        An :class:`OperatorGraphV2` whose terminal is the group assembly, or a
        :class:`MaterializationRefused`.
    """
    ir = planned.ir
    nodes: list[OperatorNodeV2] = []
    aggregates: list[OperatorNodeV2] = []

    declared_by_path = {
        policies.expr_path: policies.declared_refs() for policies in ir.policies}
    selections_by_path = {
        selected.expr_path: selected.selections for selected in ir.row_selections}

    for expression in sorted(ir.expressions, key=lambda e: e.expr_path):
        chain = _expression_chain(
            expression,
            grain_keys=ir.grain_keys,
            selections=selections_by_path.get(expression.expr_path, ()),
            declared=declared_by_path.get(expression.expr_path, ()))
        if isinstance(chain, MaterializationRefused):
            return chain
        nodes.extend(chain)
        aggregates.append(chain[-1])

    # ── LAND ON THE DECLARED POPULATION, THEN COMBINE ───────────────────────────────────────────
    # In that order. Landing first gives every population key a row for EVERY term, so a ratio over
    # a key with no in-window rows combines two declared empties rather than silently disappearing;
    # combining first would drop the key before the population was ever consulted.
    landed = OperatorNodeV2(
        kind=OperatorKindV2.SPINE_LEFT_JOIN,
        payload=SpineLeftJoinV2(
            spine_table_ref=ir.spine.source_table_ref,
            key_refs=ir.spine.ordered_key_refs,
            empty_value=empty_value),
        inputs=tuple(node.node_id for node in aggregates))
    combined = OperatorNodeV2(
        kind=OperatorKindV2.FINAL_COMBINE,
        payload=FinalCombineV2(
            final_operation=ir.final_operation,
            term_paths=tuple(sorted(e.expr_path for e in ir.expressions)),
            zero_denominator=(None if ir.zero_denominator is None
                              else str(ir.zero_denominator))),
        inputs=(landed.node_id,))
    assembled = OperatorNodeV2(
        kind=OperatorKindV2.GROUP_ASSEMBLY,
        payload=GroupAssemblyV2(column_names=(ir.feature_name,)),
        inputs=(combined.node_id,))

    return OperatorGraphV2(nodes=(*nodes, landed, combined, assembled))


def _expression_chain(
    expression: ExpressionExecutionIR,
    *,
    grain_keys: tuple[str, ...],
    selections: tuple,
    declared: tuple[tuple[str, str], ...],
) -> list[OperatorNodeV2] | MaterializationRefused:
    """One expression's linear chain, scan first and aggregate last.

    Linear, not a DAG, because every step here narrows the same relation: each operator consumes
    exactly the rows the one before it kept. The branching in a V2 graph is between EXPRESSIONS, and
    it happens at the spine join.
    """
    if expression.join_plan.steps:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{expression.expr_path} reads across {len(expression.join_plan.steps)} governed join "
            f"step(s), and this vocabulary has one scan operator and no join between two scanned "
            f"relations. Expressing it is a vocabulary change, not a building one")

    scanned = _scan_payload(expression)
    if isinstance(scanned, MaterializationRefused):
        return scanned

    chain = [OperatorNodeV2(kind=OperatorKindV2.GOVERNED_SCAN, payload=scanned)]

    # The window is carried VERBATIM. Re-declaring it here would be a second answer to "what is this
    # expression's clock", and §8 rule 1 already assigns that answer to PitSpec.
    chain.append(OperatorNodeV2(
        kind=OperatorKindV2.PIT_AVAILABILITY_FILTER,
        payload=PitAvailabilityFilterV2(pit=expression.pit),
        inputs=(chain[-1].node_id,)))

    for selection in selections:
        chain.append(OperatorNodeV2(
            kind=OperatorKindV2.SEMANTIC_SELECTION,
            payload=SemanticSelectionV2(selection=selection),
            inputs=(chain[-1].node_id,)))

    for role, ref in declared:
        if role in _UNBUILDABLE_ROLES:
            return MaterializationRefused(
                CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
                f"{expression.expr_path} declares {role} policy {ref!r}, which this builder cannot "
                f"express: {_UNBUILDABLE_ROLES[role]}")
        if role == "status":
            chain.append(OperatorNodeV2(
                kind=OperatorKindV2.ELIGIBLE_STATUS_FILTER,
                payload=EligibleStatusFilterV2(status_policy_ref=ref),
                inputs=(chain[-1].node_id,)))
        # `direction` has no operator of its own: it is applied as the SEMANTIC SELECTION already
        # emitted above, and the ref is what resolves that selection to this ledger's encoding.

    aggregation = _aggregate_function(expression)
    operand = _operand_ref(expression)
    if aggregation is AggregateFunctionV2.COUNT_ROWS:
        operand = None                     # AggregateV2 refuses an operand it would not use
    elif operand is None:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{expression.expr_path} aggregates with {aggregation} and its read set names no "
            f"operand column: the aggregate has nothing to compute over")

    chain.append(OperatorNodeV2(
        kind=OperatorKindV2.AGGREGATE,
        payload=AggregateV2(function=aggregation, operand_ref=operand,
                            grain_key_refs=grain_keys),
        inputs=(chain[-1].node_id,)))
    return chain


def _scan_payload(
    expression: ExpressionExecutionIR,
) -> GovernedScanV2 | MaterializationRefused:
    """The one relation this expression scans, and the columns it reads from it, in READ ORDER.

    Order is the read set's own, never sorted: it is what the compiler resolved, and re-ordering it
    would make two compilations of one expression two different graphs.
    """
    relations = [ref.logical_ref for ref in expression.physical_read_set
                 if RefRole.SOURCE_TABLE in ref.roles]
    if len(relations) != 1:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{expression.expr_path} names {len(relations)} scanned relation(s) "
            f"({sorted(relations)}): a governed scan reads exactly one, and a graph that guessed "
            f"which would scan a table the compilation never resolved")

    columns = tuple(dict.fromkeys(
        ref.logical_ref for ref in expression.physical_read_set if ref.column is not None))
    if not columns:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{expression.expr_path} scans {relations[0]} and reads no column from it: a scan with "
            f"no columns produces rows nothing can compute over")
    return GovernedScanV2(table_ref=relations[0], column_refs=columns)


def _aggregate_function(expression: ExpressionExecutionIR) -> AggregateFunctionV2:
    """The expression's aggregate, which is ALREADY V2's — guaranteed by the IR, not re-crossed.

    This used to convert, defensively. It no longer does, because step 8 made
    ``ExpressionExecutionIR`` enforce the vocabulary at construction: a second crossing here would
    be a second place that could answer differently, which is the whole failure being removed.
    """
    return expression.aggregation


def _operand_ref(expression: ExpressionExecutionIR) -> str | None:
    """The column the aggregate computes over, by its RECORDED role.

    Read off ``roles`` rather than guessed from position: the same column is legitimately read as
    the operand and as the window's clock, and a builder picking by order would eventually sum a
    timestamp.
    """
    for ref in expression.physical_read_set:
        if RefRole.OPERAND in ref.roles:
            return ref.logical_ref
    return None
