"""C-C10a — the closed pilot operator graph.

The gate is *"the vocabulary is closed and each node's identity payload is pinned"*, so the tests
that matter are the ones a future change would trip: the kind→payload map is a TOTAL function over
the enum, a payload cannot be paired with the wrong kind, and identity does not depend on the order
a builder happened to append nodes in.
"""
from __future__ import annotations

import pytest

from featuregen.formula.schema import DecimalPolicy, OverflowBehavior, RoundingMode
from featuregen.formula.schema_v2 import AggregateFunctionV2
from featuregen.formula.schema_v3 import SelectionKind, SemanticRowSelectionV1
from featuregen.materialize.expression_ir import AvailabilityBasis, PitSpec
from featuregen.materialize.operator_graph_v2 import (
    _PAYLOAD_TYPE,
    AggregateV2,
    AsOfFxJoinV2,
    DecimalMultiplicationV2,
    DuplicateRateGateV2,
    EligibleStatusFilterV2,
    GovernedScanV2,
    GroupAssemblyV2,
    MissingRateGateV2,
    OperatorGraphV2,
    OperatorKindV2,
    OperatorNodeV2,
    PitAvailabilityFilterV2,
    QuoteInversionV2,
    SemanticSelectionV2,
    SpineLeftJoinV2,
    graph_hash_v2,
)

TXN = "bank::public.txns"
PIT = PitSpec(
    event_time_ref=f"{TXN}.booking_ts", availability_ref=f"{TXN}.load_ts",
    availability_basis=AvailabilityBasis.POSTED_AT, availability_lag_hours=None,
    window_basis="as_of_cutoff", window_length=30, window_unit="days",
    window_start_inclusive="exclusive", window_end_inclusive="inclusive", window_timezone="Asia/Dubai")
DEBIT = SemanticRowSelectionV1(
    kind=SelectionKind.TRANSACTION_DIRECTION, role="direction", semantic_value="debit")


def _scan() -> OperatorNodeV2:
    return OperatorNodeV2(
        kind=OperatorKindV2.GOVERNED_SCAN,
        payload=GovernedScanV2(table_ref=TXN, column_refs=(
            f"{TXN}.acct_id", f"{TXN}.txn_amt", f"{TXN}.booking_ts")))


def _pilot() -> OperatorGraphV2:
    """The D1 pilot: `posted_debit_amount_30d` over a fixed-AED source, landed on the population."""
    scan = _scan()
    pit = OperatorNodeV2(kind=OperatorKindV2.PIT_AVAILABILITY_FILTER,
                         payload=PitAvailabilityFilterV2(pit=PIT), inputs=(scan.node_id,))
    select = OperatorNodeV2(kind=OperatorKindV2.SEMANTIC_SELECTION,
                            payload=SemanticSelectionV2(selection=DEBIT), inputs=(pit.node_id,))
    eligible = OperatorNodeV2(
        kind=OperatorKindV2.ELIGIBLE_STATUS_FILTER,
        payload=EligibleStatusFilterV2(status_policy_ref="eligible_status:foundation-posted-events"),
        inputs=(select.node_id,))
    aggregate = OperatorNodeV2(
        kind=OperatorKindV2.AGGREGATE,
        payload=AggregateV2(function=AggregateFunctionV2.SUM, operand_ref=f"{TXN}.txn_amt",
                            grain_key_refs=(f"{TXN}.acct_id",)),
        inputs=(eligible.node_id,))
    landed = OperatorNodeV2(
        kind=OperatorKindV2.SPINE_LEFT_JOIN,
        payload=SpineLeftJoinV2(spine_table_ref="bank::public.account_population",
                                key_refs=("bank::public.account_population.acct_id",),
                                empty_value="0"),
        inputs=(aggregate.node_id,))
    assembled = OperatorNodeV2(
        kind=OperatorKindV2.GROUP_ASSEMBLY,
        payload=GroupAssemblyV2(column_names=("posted_debit_amount_30d",)),
        inputs=(landed.node_id,))
    return OperatorGraphV2(nodes=(scan, pit, select, eligible, aggregate, landed, assembled))


# ══ the vocabulary is CLOSED ═════════════════════════════════════════════════════════════════════
def test_the_vocabulary_is_the_fourteen_the_graph_needs_to_describe_a_WHOLE_feature():
    """C-C10a froze thirteen. FINAL_COMBINE is the fourteenth, and its absence was a gap.

    With thirteen the graph could describe every step of a feature EXCEPT the one that produces its
    value: a ratio's two aggregates had nowhere to be divided. So the graph could not claim to be
    the executable form of ANY feature, not merely of exotic ones — which is why the V2-only plan
    rules it a derived view and adds this node rather than leaving it implied.

    It is also where capability per final operation hangs: `ratio` and `signed_sum` are different
    renderer abilities, and without a kind there was nowhere to record which one an engine has.
    """
    assert [kind.value for kind in OperatorKindV2] == [
        "governed_scan", "pit_availability_filter", "semantic_selection", "eligible_status_filter",
        "linked_reversal_survivor", "as_of_fx_join", "duplicate_rate_gate", "missing_rate_gate",
        "quote_inversion", "decimal_multiplication", "aggregate", "spine_left_join",
        "group_assembly", "final_combine"]
    assert len(OperatorKindV2) == 14


def test_every_kind_has_exactly_one_payload_type():
    """A TOTAL function over the enum. A kind with no payload type could never be constructed, and
    would look like a supported operator to anything reading the enum."""
    assert set(_PAYLOAD_TYPE) == set(OperatorKindV2)
    assert len(set(_PAYLOAD_TYPE.values())) == len(OperatorKindV2), "no payload type is shared"


def test_a_payload_cannot_be_paired_with_the_WRONG_kind():
    """The kind and the payload are one decision — a node whose payload disagreed with its kind
    would render as neither."""
    with pytest.raises(TypeError, match="takes a AggregateV2 payload"):
        OperatorNodeV2(kind=OperatorKindV2.AGGREGATE,
                       payload=GovernedScanV2(table_ref=TXN, column_refs=(f"{TXN}.a",)))


def test_every_payload_pins_an_identity():
    """"each node's identity payload is pinned" — every one of the thirteen answers, and answers a
    JSON-canonicalizable dict rather than a repr."""
    for kind, payload_type in _PAYLOAD_TYPE.items():
        assert hasattr(payload_type, "identity_payload"), kind


# ══ node identity ════════════════════════════════════════════════════════════════════════════════
def test_node_ids_are_CONTENT_derived_not_counted():
    """Two structurally identical nodes with identical inputs ARE one node."""
    assert _scan().node_id == _scan().node_id
    assert _scan().node_id.startswith("governed_scan:")


def test_a_different_payload_is_a_different_node():
    other = OperatorNodeV2(kind=OperatorKindV2.GOVERNED_SCAN,
                           payload=GovernedScanV2(table_ref=TXN, column_refs=(f"{TXN}.acct_id",)))
    assert other.node_id != _scan().node_id


def test_INPUT_ORDER_is_identity_bearing():
    """For a join the sides are not interchangeable — an unordered input set would give a left join
    and a right join the same identity."""
    payload = SemanticSelectionV2(selection=DEBIT)
    left = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, payload, inputs=("a", "b"))
    right = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, payload, inputs=("b", "a"))
    assert left.node_id != right.node_id


def test_the_selection_stays_SEMANTIC_in_the_node_identity():
    """`"debit"`, never the pilot ledger's `"D"` — resolving it is C-C8's, and a graph that resolved
    it here would bake one ledger's encoding into every feature's identity."""
    node = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, SemanticSelectionV2(DEBIT))
    assert node.identity_payload()["payload"] == {
        "kind": "transaction_direction", "role": "direction", "semantic_value": "debit"}


# ══ graph validation ═════════════════════════════════════════════════════════════════════════════
def test_the_pilot_graph_builds_and_terminates_at_group_assembly():
    graph = _pilot()
    assert graph.terminal.kind is OperatorKindV2.GROUP_ASSEMBLY
    assert len(graph.nodes) == 6 + 1


def test_a_dangling_input_is_refused():
    node = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, SemanticSelectionV2(DEBIT),
                          inputs=("governed_scan:doesnotexist",))
    with pytest.raises(ValueError, match="not a node in this graph"):
        OperatorGraphV2(nodes=(node,))


def test_two_terminals_are_refused():
    """A feature computes ONE thing; a second terminal is a branch nobody publishes."""
    scan = _scan()
    a = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, SemanticSelectionV2(DEBIT),
                       inputs=(scan.node_id,))
    b = OperatorNodeV2(OperatorKindV2.ELIGIBLE_STATUS_FILTER,
                       EligibleStatusFilterV2(status_policy_ref="eligible_status:x"),
                       inputs=(scan.node_id,))
    with pytest.raises(ValueError, match="2 terminal nodes"):
        OperatorGraphV2(nodes=(scan, a, b))


def test_a_cycle_is_STRUCTURALLY_unreachable_while_ids_are_derived():
    """Why the acyclicity guard has no refusal test: it has no reachable refusal.

    ``node_id`` is derived FROM the inputs, so a cycle needs a node whose id depends on an id that
    depends on its own — a SHA-256 fixed point. This test pins the property the guard depends on,
    so that making ``node_id`` assignable (or restoring ids from a persisted graph) fails HERE and
    sends the author to the guard, rather than silently turning dead code into load-bearing code
    nobody re-read.
    """
    import dataclasses

    fields = {f.name for f in dataclasses.fields(OperatorNodeV2)}
    assert "node_id" not in fields, "node_id must stay DERIVED, not assignable"
    scan = _scan()
    downstream = OperatorNodeV2(OperatorKindV2.SEMANTIC_SELECTION, SemanticSelectionV2(DEBIT),
                                inputs=(scan.node_id,))
    assert scan.node_id not in downstream.inputs[1:]
    assert downstream.node_id != scan.node_id


def test_an_empty_graph_is_refused():
    with pytest.raises(ValueError, match="computes nothing"):
        OperatorGraphV2(nodes=())


def test_the_same_operator_added_twice_is_refused():
    scan = _scan()
    assembled = OperatorNodeV2(OperatorKindV2.GROUP_ASSEMBLY, GroupAssemblyV2(("f",)),
                               inputs=(scan.node_id,))
    with pytest.raises(ValueError, match="one node, not two"):
        OperatorGraphV2(nodes=(scan, scan, assembled))


def test_the_graph_hash_does_not_depend_on_APPEND_order():
    """The graph is defined by its edges. Two builders producing the same edges must produce the
    same hash — the defect Task 6 found one level down, where ordering by the sequence in which
    tables happened to RESOLVE silently changed the hash."""
    pilot = _pilot()
    shuffled = OperatorGraphV2(nodes=tuple(reversed(pilot.nodes)))
    assert graph_hash_v2(shuffled) == graph_hash_v2(pilot)


# ══ what C-C10's subgraph requirements will ASK (the point of the vocabulary) ═════════════════════
def test_the_fixed_AED_pilot_contains_NO_fx_nodes():
    """D3's sequencing, made checkable: the first execution is fixed-AED, so the base-currency
    bypass is the ABSENCE of the FX nodes rather than a fourteenth operator."""
    kinds = _pilot().kinds
    assert not kinds & {OperatorKindV2.AS_OF_FX_JOIN, OperatorKindV2.DUPLICATE_RATE_GATE,
                        OperatorKindV2.MISSING_RATE_GATE, OperatorKindV2.QUOTE_INVERSION}
    assert OperatorKindV2.SEMANTIC_SELECTION in kinds


def test_an_fx_subgraph_is_expressible_with_both_gates():
    """The two gates C-C10 requires exist as nodes, so "delete the duplicate-rate gate" is a
    question the topology can answer."""
    scan = _scan()
    joined = OperatorNodeV2(
        kind=OperatorKindV2.AS_OF_FX_JOIN,
        payload=AsOfFxJoinV2(currency_conversion_ref="currency_conversion:foundation-base-currency",
                             rate_table_ref="bank::public.fx_rates",
                             as_of_ref="bank::public.fx_rates.effective_ts",
                             rate_column_ref="bank::public.fx_rates.rate"),
        inputs=(scan.node_id,))
    duplicate = OperatorNodeV2(OperatorKindV2.DUPLICATE_RATE_GATE,
                               DuplicateRateGateV2(rate_key_refs=("bank::public.fx_rates.ccy",)),
                               inputs=(joined.node_id,))
    missing = OperatorNodeV2(OperatorKindV2.MISSING_RATE_GATE, MissingRateGateV2(),
                             inputs=(duplicate.node_id,))
    multiplied = OperatorNodeV2(
        OperatorKindV2.DECIMAL_MULTIPLICATION,
        DecimalMultiplicationV2(
            decimal=DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_EVEN,
                                  overflow=OverflowBehavior.ERROR),
            round_per_row=False),
        inputs=(missing.node_id,))
    graph = OperatorGraphV2(nodes=(scan, joined, duplicate, missing, multiplied))
    assert OperatorKindV2.DUPLICATE_RATE_GATE in graph.kinds
    assert OperatorKindV2.MISSING_RATE_GATE in graph.kinds
    assert graph.terminal.kind is OperatorKindV2.DECIMAL_MULTIPLICATION


def test_the_missing_rate_gate_refuses_rather_than_emitting_null():
    """D3, stated in the vocabulary: a left join would emit NULL and a sum would skip the row —
    the silent omission that must never happen to a USD row."""
    assert MissingRateGateV2().refusal_code == "FX_RATE_MISSING"


# ══ payload-level refusals ═══════════════════════════════════════════════════════════════════════
def test_count_rows_refuses_an_operand_and_the_others_require_one():
    keys = ("bank::public.txns.acct_id",)
    with pytest.raises(ValueError, match="count_rows takes no operand"):
        AggregateV2(AggregateFunctionV2.COUNT_ROWS, operand_ref="x", grain_key_refs=keys)
    with pytest.raises(ValueError, match="needs an operand ref"):
        AggregateV2(AggregateFunctionV2.SUM, operand_ref=None, grain_key_refs=keys)
    assert AggregateV2(AggregateFunctionV2.COUNT_ROWS, None, keys).operand_ref is None


def test_a_quote_inversion_to_ITSELF_is_refused():
    """The identity rate is the base-currency BYPASS, which is the absence of these nodes."""
    with pytest.raises(ValueError, match="base-currency BYPASS"):
        QuoteInversionV2(from_currency="AED", to_currency="AED")


def test_a_blank_governed_ref_is_refused():
    with pytest.raises(ValueError, match="is blank"):
        EligibleStatusFilterV2(status_policy_ref="   ")


def test_a_scan_naming_the_same_column_twice_is_refused():
    with pytest.raises(ValueError, match="names the same ref twice"):
        GovernedScanV2(table_ref=TXN, column_refs=(f"{TXN}.a", f"{TXN}.a"))


def test_the_rounding_SITE_is_part_of_the_identity():
    """Rounding per row and rounding after the sum give different numbers, so which one happened
    has to be a fact the graph states."""
    decimal = DecimalPolicy(precision=38, scale=2, rounding=RoundingMode.HALF_EVEN,
                            overflow=OverflowBehavior.ERROR)
    per_row = OperatorNodeV2(OperatorKindV2.DECIMAL_MULTIPLICATION,
                             DecimalMultiplicationV2(decimal, round_per_row=True))
    at_end = OperatorNodeV2(OperatorKindV2.DECIMAL_MULTIPLICATION,
                            DecimalMultiplicationV2(decimal, round_per_row=False))
    assert per_row.node_id != at_end.node_id
