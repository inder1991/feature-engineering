"""C-C10 — subgraph requirements over the frozen vocabulary, on STUBBED graphs.

The plan's own S7 gate is the centre of this file: *"deleting the duplicate-rate gate refuses"*.
The second-order case matters just as much and is easy to miss — a gate that EXISTS but sits off the
path from the FX join to the terminal satisfies "contains a duplicate-rate gate" and protects
nothing.
"""
from __future__ import annotations

import pytest

from featuregen.formula.schema_leaves import DecimalPolicy, OverflowBehavior, RoundingMode
from featuregen.formula.schema_v2 import AggregateFunctionV2
from featuregen.materialize.operator_graph_v2 import (
    AggregateV2,
    AsOfFxJoinV2,
    DecimalMultiplicationV2,
    DuplicateRateGateV2,
    GovernedScanV2,
    GroupAssemblyV2,
    LinkedReversalSurvivorV2,
    MissingRateGateV2,
    OperatorGraphV2,
    OperatorKindV2,
    OperatorNodeV2,
    QuoteInversionV2,
)
from featuregen.materialize.subgraph_requirements_v2 import (
    DISCONNECTED_OPERATOR,
    FX_CONVERSION,
    LINKED_REVERSAL,
    MISSING_OPERATOR,
    PILOT_REQUIREMENTS,
    RequirementVerdictV2,
    SubgraphRequirementV2,
    check_subgraph_requirements_v2,
)

TXN = "bank::public.txns"
RATES = "bank::public.fx_rates"
DECIMAL = DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_EVEN,
                        overflow=OverflowBehavior.ERROR)


def _scan() -> OperatorNodeV2:
    return OperatorNodeV2(OperatorKindV2.GOVERNED_SCAN,
                          GovernedScanV2(TXN, (f"{TXN}.acct_id", f"{TXN}.txn_amt")))


def _fx_chain(*, duplicate_gate: bool = True, missing_gate: bool = True,
              multiply: bool = True, invert: bool = False):
    """A stubbed FX graph, with any link removable — the mutations C-C10's gate is written against."""
    scan = _scan()
    nodes = [scan]
    tip = OperatorNodeV2(
        OperatorKindV2.AS_OF_FX_JOIN,
        AsOfFxJoinV2(currency_conversion_ref="currency_conversion:foundation-base-currency",
                     rate_table_ref=RATES, as_of_ref=f"{RATES}.effective_ts",
                     rate_column_ref=f"{RATES}.rate"),
        inputs=(scan.node_id,))
    nodes.append(tip)
    if duplicate_gate:
        tip = OperatorNodeV2(OperatorKindV2.DUPLICATE_RATE_GATE,
                             DuplicateRateGateV2((f"{RATES}.ccy",)), inputs=(tip.node_id,))
        nodes.append(tip)
    if missing_gate:
        tip = OperatorNodeV2(OperatorKindV2.MISSING_RATE_GATE, MissingRateGateV2(),
                             inputs=(tip.node_id,))
        nodes.append(tip)
    if invert:
        tip = OperatorNodeV2(OperatorKindV2.QUOTE_INVERSION, QuoteInversionV2("USD", "AED"),
                             inputs=(tip.node_id,))
        nodes.append(tip)
    if multiply:
        tip = OperatorNodeV2(OperatorKindV2.DECIMAL_MULTIPLICATION,
                             DecimalMultiplicationV2(DECIMAL, round_per_row=False),
                             inputs=(tip.node_id,))
        nodes.append(tip)
    aggregate = OperatorNodeV2(
        OperatorKindV2.AGGREGATE,
        AggregateV2(AggregateFunctionV2.SUM, f"{TXN}.txn_amt", (f"{TXN}.acct_id",)),
        inputs=(tip.node_id,))
    assembled = OperatorNodeV2(OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f",)),
                               inputs=(aggregate.node_id,))
    return OperatorGraphV2(nodes=(*nodes, aggregate, assembled))


def _fixed_aed_pilot() -> OperatorGraphV2:
    scan = _scan()
    aggregate = OperatorNodeV2(
        OperatorKindV2.AGGREGATE,
        AggregateV2(AggregateFunctionV2.SUM, f"{TXN}.txn_amt", (f"{TXN}.acct_id",)),
        inputs=(scan.node_id,))
    assembled = OperatorNodeV2(OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f",)),
                               inputs=(aggregate.node_id,))
    return OperatorGraphV2(nodes=(scan, aggregate, assembled))


# ══ the requirement definitions ══════════════════════════════════════════════════════════════════
def test_the_requirements_are_NAMED_CONSTANTS():
    assert {r.name for r in PILOT_REQUIREMENTS} == {"FX_CONVERSION", "LINKED_REVERSAL"}
    assert FX_CONVERSION.triggered_by is OperatorKindV2.AS_OF_FX_JOIN
    assert set(FX_CONVERSION.required) == {
        OperatorKindV2.DUPLICATE_RATE_GATE, OperatorKindV2.MISSING_RATE_GATE,
        OperatorKindV2.DECIMAL_MULTIPLICATION}
    assert FX_CONVERSION.optional == (OperatorKindV2.QUOTE_INVERSION,)


def test_a_requirement_cannot_require_its_own_trigger():
    """A tautology that always passes."""
    with pytest.raises(ValueError, match="tautology"):
        SubgraphRequirementV2(name="X", triggered_by=OperatorKindV2.AS_OF_FX_JOIN,
                              required=(OperatorKindV2.AS_OF_FX_JOIN,))


def test_an_operator_cannot_be_both_required_and_optional():
    with pytest.raises(ValueError, match="both required and optional"):
        SubgraphRequirementV2(name="X", triggered_by=OperatorKindV2.AS_OF_FX_JOIN,
                              required=(OperatorKindV2.QUOTE_INVERSION,),
                              optional=(OperatorKindV2.QUOTE_INVERSION,))


# ══ the base-currency bypass is an ABSENCE ═══════════════════════════════════════════════════════
def test_the_fixed_AED_pilot_triggers_NOTHING():
    """D3's sequencing: no FX join, so the FX requirement never applies — and is NOT reported as a
    pass, which would suggest an FX subgraph was inspected and found sound."""
    verdict = check_subgraph_requirements_v2(_fixed_aed_pilot())
    assert verdict.satisfied
    assert verdict.triggered == ()


# ══ THE S7 GATE ══════════════════════════════════════════════════════════════════════════════════
def test_a_complete_fx_subgraph_is_satisfied():
    verdict = check_subgraph_requirements_v2(_fx_chain())
    assert verdict.satisfied, verdict.findings
    assert verdict.triggered == ("FX_CONVERSION",)


def test_DELETING_THE_DUPLICATE_RATE_GATE_REFUSES():
    """The plan's S7 gate. An as-of join amplifies silently when the rate side offers two rows for
    a key at an instant — a wrong number rather than an error."""
    verdict = check_subgraph_requirements_v2(_fx_chain(duplicate_gate=False))
    assert not verdict.satisfied
    (finding,) = verdict.findings
    assert finding.code == MISSING_OPERATOR
    assert finding.requirement == "FX_CONVERSION"
    assert "duplicate_rate_gate" in finding.detail
    assert "amplifies silently" in finding.detail


def test_deleting_the_missing_rate_gate_refuses():
    """The other half of D3: a missing rate must refuse, never drop the row."""
    verdict = check_subgraph_requirements_v2(_fx_chain(missing_gate=False))
    assert not verdict.satisfied
    assert [f.code for f in verdict.findings] == [MISSING_OPERATOR]
    assert "missing_rate_gate" in verdict.findings[0].detail


def test_deleting_the_multiplication_refuses():
    verdict = check_subgraph_requirements_v2(_fx_chain(multiply=False))
    assert not verdict.satisfied
    assert "decimal_multiplication" in verdict.findings[0].detail


def test_the_quote_inversion_is_OPTIONAL_both_ways():
    """Whether a quote needs inverting is a property of how the rate table publishes, not of the
    conversion — so its absence is not a defect and its presence is not a surprise."""
    assert check_subgraph_requirements_v2(_fx_chain(invert=False)).satisfied
    assert check_subgraph_requirements_v2(_fx_chain(invert=True)).satisfied


# ══ position, not just presence ══════════════════════════════════════════════════════════════════
def test_a_gate_hanging_off_NOTHING_is_already_refused_by_the_graph_type():
    """Worth pinning: the single-terminal rule catches the SIMPLEST disconnection on its own, so
    the requirement check does not need to and the next test covers what it genuinely adds."""
    complete = _fx_chain(duplicate_gate=False)
    stray = OperatorNodeV2(OperatorKindV2.DUPLICATE_RATE_GATE,
                           DuplicateRateGateV2((f"{RATES}.ccy",)), inputs=(_scan().node_id,))
    with pytest.raises(ValueError, match="2 terminal nodes"):
        OperatorGraphV2(nodes=(*complete.nodes, stray))


def test_a_gate_UPSTREAM_OF_THE_TERMINAL_BUT_NOT_OF_THE_JOIN_refuses():
    """The case the graph type CANNOT catch, and the reason position is checked at all.

    The graph contains a duplicate-rate gate, it has exactly one terminal, and the gate is genuinely
    consumed — it just sits on a branch that never sees the joined rows. "Contains the gate" is
    satisfied and nothing is protected.
    """
    complete = _fx_chain(duplicate_gate=False)
    scan = _scan()
    stray = OperatorNodeV2(OperatorKindV2.DUPLICATE_RATE_GATE,
                           DuplicateRateGateV2((f"{RATES}.ccy",)), inputs=(scan.node_id,))
    old_terminal = complete.terminal
    # the assembly consumes BOTH branches, so the graph is single-terminal and fully connected
    assembled = OperatorNodeV2(
        OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f", "g")),
        inputs=(old_terminal.inputs[0], stray.node_id))
    rest = tuple(node for node in complete.nodes if node.node_id != old_terminal.node_id)
    graph = OperatorGraphV2(nodes=(*rest, stray, assembled))
    verdict = check_subgraph_requirements_v2(graph)

    assert OperatorKindV2.DUPLICATE_RATE_GATE in graph.kinds, "the gate really is present"
    assert not verdict.satisfied
    (finding,) = [f for f in verdict.findings if f.code == DISCONNECTED_OPERATOR]
    assert "protects nothing" in finding.detail


# ══ linked reversal — payload facts, enforced at construction ════════════════════════════════════
def test_the_reversal_payload_carries_all_FOUR_required_facts():
    """The vocabulary has ONE reversal operator, so as-of population, linkage, ambiguity gate and
    survivor are payload facts. Writing this requirement is what found two of them missing."""
    payload = LinkedReversalSurvivorV2(
        reversal_policy_ref="reversal_correction:foundation-flag-or-code",
        as_of_population_ref=f"{TXN}.booking_ts", linkage_key_refs=(f"{TXN}.orig_txn_id",))
    assert payload.ambiguity_refusal_code == "REVERSAL_LINK_AMBIGUOUS"
    assert set(payload.identity_payload()) == {
        "reversal_policy_ref", "as_of_population_ref", "linkage_key_refs",
        "ambiguity_refusal_code"}


@pytest.mark.parametrize("missing", ["as_of_population_ref", "linkage_key_refs"])
def test_an_incomplete_reversal_payload_is_refused(missing):
    kwargs = dict(reversal_policy_ref="reversal_correction:foundation-flag-or-code",
                  as_of_population_ref=f"{TXN}.booking_ts",
                  linkage_key_refs=(f"{TXN}.orig_txn_id",))
    kwargs[missing] = "" if missing.endswith("_ref") else ()
    with pytest.raises(ValueError):
        LinkedReversalSurvivorV2(**kwargs)


def test_a_reversal_graph_triggers_the_reversal_requirement():
    scan = _scan()
    survivor = OperatorNodeV2(
        OperatorKindV2.LINKED_REVERSAL_SURVIVOR,
        LinkedReversalSurvivorV2(
            reversal_policy_ref="reversal_correction:foundation-flag-or-code",
            as_of_population_ref=f"{TXN}.booking_ts", linkage_key_refs=(f"{TXN}.orig_txn_id",)),
        inputs=(scan.node_id,))
    aggregate = OperatorNodeV2(
        OperatorKindV2.AGGREGATE,
        AggregateV2(AggregateFunctionV2.SUM, f"{TXN}.txn_amt", (f"{TXN}.acct_id",)),
        inputs=(survivor.node_id,))
    assembled = OperatorNodeV2(OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f",)),
                               inputs=(aggregate.node_id,))
    verdict = check_subgraph_requirements_v2(
        OperatorGraphV2(nodes=(scan, survivor, aggregate, assembled)))
    assert verdict.satisfied
    assert verdict.triggered == ("LINKED_REVERSAL",)


# ══ the verdict cannot lie ═══════════════════════════════════════════════════════════════════════
def test_a_satisfied_verdict_cannot_carry_findings():
    from featuregen.materialize.subgraph_requirements_v2 import RequirementFindingV2

    finding = RequirementFindingV2(requirement="FX_CONVERSION", code=MISSING_OPERATOR, detail="d")
    with pytest.raises(ValueError, match="cannot carry findings"):
        RequirementVerdictV2(satisfied=True, triggered=("FX_CONVERSION",), findings=(finding,))
    with pytest.raises(ValueError, match="nothing to fix"):
        RequirementVerdictV2(satisfied=False, triggered=(), findings=())


def test_LINKED_REVERSAL_requires_no_sibling_nodes_by_design():
    assert LINKED_REVERSAL.required == ()
    assert "payload facts" in LINKED_REVERSAL.rationale
