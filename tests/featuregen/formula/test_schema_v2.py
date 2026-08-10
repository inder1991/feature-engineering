"""BR-6 increment 1 — the v2 schema and THE version dispatch, with v1 frozen by literal manifest.

The first test in this file is the plan's first step, mechanized: every Formula-v1 gold fixture's
pinned hash is FROZEN here as a literal — if adding v2 (or anything else, ever) moves one v1
byte, this fails before any reviewer has to notice. Then the v2 gate: version pins are DATA
(validated equal to the v2 constants, never inferred), avg/min/max join the vocabulary with the
same operand discipline, and the dispatch reads the declared version field and nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from featuregen.formula.parse_v2 import parse_proposal_v2, parse_versioned
from featuregen.formula.schema import SchemaError, TypedFormulaProposalV1
from featuregen.formula.schema_v2 import TypedFormulaProposalV2

_GOLD_V1 = Path(__file__).parent / "gold_fixtures"
_GOLD_V2 = Path(__file__).parent / "gold_v2"

# The Formula-v1 freeze (BR-6 step 1): every v1 gold fixture's pinned formula hash, as a LITERAL.
# Breaking this is breaking Formula-v1 immutability — plan invariant 3; there is no legitimate
# same-commit regeneration of THIS table short of a reviewed v1 re-versioning decision.
_V1_FROZEN_HASHES = {
    "01_sum_txn_amt_90d.json": "c9fdf481fea83671892f1b34f0b1780165c1d19a414e4462ffd7bad899c6bec9",
    "02_count_rows_txns_90d.json": "2a513d1199e31a849cd310f39fe9920ed6430e0a54417aa4790b7237f17cc58f",
    "03_count_non_null_merchant_90d.json": "ef08b156270bd5cb62f095877376ea9b568902acfe0a1d41873d83031cbec8a9",
    "04_count_distinct_merchant_90d.json": "f09abe419986f926ed2ba8b668959f4cfe92d77ae08d4ee931323d46a5903c84",
    "05_ratio_posted_amount_share_90d.json": "4ddc8c054d89dc50da2e30eba51de670c414a47ae28c88efeaf1a8d264af9fd5",
    "06_difference_amount_minus_fee_90d.json": "4da62c58f40a2d35def39799581ba1431d3256b1df45dd15daf7cf0f6091441e",
    "07_multi_source_is_unsupported.json": None,
    "08_over_deep_filter_is_invalid.json": None,
    "09_avg_is_unsupported_operation.json": None,
    "10_blocking_critic_wrong_slot.json": None,
    "11_ungoverned_type_needs_external_validation.json": None,
}


def test_every_formula_v1_gold_hash_is_frozen():
    fixtures = {p.name: json.loads(p.read_text()) for p in sorted(_GOLD_V1.glob("*.json"))}
    assert set(fixtures) == set(_V1_FROZEN_HASHES), \
        "the v1 gold corpus itself changed shape — v1 is frozen"
    for name, doc in fixtures.items():
        assert doc.get("expected_formula_hash") == _V1_FROZEN_HASHES[name], \
            f"{name}: the pinned v1 hash moved — Formula-v1 immutability is plan invariant 3"


def _ok_fixture(name: str) -> dict:
    return json.loads((_GOLD_V2 / name).read_text())["proposal"]


def test_the_new_group_parses_and_avg_graduates_from_v1_unsupported():
    """v1's gold corpus literally pins `avg` as an unsupported operation (fixture 09); under v2
    it is a first-class aggregate with the same operand discipline as sum."""
    proposal = parse_proposal_v2(_ok_fixture("01_avg_txn_amt_90d.json"))
    assert isinstance(proposal, TypedFormulaProposalV2)
    assert proposal.body.expr.aggregation.value == "avg"
    for name in ("02_max_balance_90d.json", "03_min_balance_90d.json",
                 "04_sum_txn_amt_90d_v2.json"):
        parse_proposal_v2(_ok_fixture(name))


def test_operand_discipline_holds_for_the_new_group():
    bad = json.loads((_GOLD_V2 / "05_avg_without_operand_invalid.json").read_text())
    assert bad["expected"] == "schema_error"
    with pytest.raises(SchemaError):
        parse_proposal_v2(bad["proposal"])
    with_operand_on_count_rows = _ok_fixture("01_avg_txn_amt_90d.json")
    with_operand_on_count_rows["body"]["expr"]["aggregation"] = "count_rows"
    with pytest.raises(SchemaError, match="count_rows carries no operand"):
        parse_proposal_v2(with_operand_on_count_rows)


def test_version_pins_are_data_not_inference():
    claimed_v1 = _ok_fixture("04_sum_txn_amt_90d_v2.json")
    claimed_v1["formula_schema_version"] = 1
    with pytest.raises(SchemaError):
        parse_proposal_v2(claimed_v1)   # the v2 parser refuses a v1 claim outright


def test_the_dispatch_reads_the_declared_version_and_nothing_else():
    v2 = parse_versioned(_ok_fixture("04_sum_txn_amt_90d_v2.json"))
    assert isinstance(v2, TypedFormulaProposalV2)
    v1_doc = json.loads((_GOLD_V1 / "01_sum_txn_amt_90d.json").read_text())["proposal"]
    v1 = parse_versioned(v1_doc)
    assert isinstance(v1, TypedFormulaProposalV1)
    # a version-less body is refused — never sniffed, however v1-shaped it looks
    unversioned = dict(v1_doc)
    unversioned.pop("formula_schema_version")
    with pytest.raises(SchemaError, match="never inferred from body shape"):
        parse_versioned(unversioned)
    with pytest.raises(SchemaError, match="never inferred"):
        parse_versioned({**v1_doc, "formula_schema_version": 3})


def test_the_distributional_group_parses_and_the_argument_is_disciplined():
    """Increment 2: recency / stddev / percentile / median. The aggregate argument is REQUIRED
    for percentile (p strictly inside (0,100)), FORBIDDEN everywhere else — a parameterized
    aggregate is declared, never smuggled into a label."""
    for name in ("07_recency_last_txn_90d.json", "08_stddev_txn_amt_90d.json",
                 "09_percentile_p95_txn_amt_90d.json", "10_median_txn_amt_90d.json"):
        parse_proposal_v2(_ok_fixture(name))
    p95 = parse_proposal_v2(_ok_fixture("09_percentile_p95_txn_amt_90d.json"))
    assert p95.body.expr.aggregation_argument == 95

    bare = json.loads((_GOLD_V2 / "11_percentile_without_argument_invalid.json").read_text())
    with pytest.raises(SchemaError, match="strictly between 0 and 100"):
        parse_proposal_v2(bare["proposal"])
    smuggled = json.loads((_GOLD_V2 / "12_argument_on_sum_invalid.json").read_text())
    with pytest.raises(SchemaError, match="takes no argument"):
        parse_proposal_v2(smuggled["proposal"])
    edge = _ok_fixture("09_percentile_p95_txn_amt_90d.json")
    edge["body"]["expr"]["aggregation_argument"] = 100
    with pytest.raises(SchemaError, match="strictly between"):
        parse_proposal_v2(edge)


def test_the_at_cutoff_group_and_the_rule_table_are_total():
    """Increment 3: last_known / first_known / zscore parse with the standard operand discipline,
    the rule table is TOTAL over the enum (a member without a rule cannot ship), and additivity is
    a view over it — last_known is honestly SEMI-additive, like the balances it reads."""
    from featuregen.formula.operations_v2 import OPERATION_RULES
    from featuregen.formula.schema import AdditivityClass
    from featuregen.formula.schema_v2 import AGGREGATE_ADDITIVITY_V2, AggregateFunctionV2

    for name in ("13_last_known_balance_at_cutoff.json", "14_first_known_balance_in_window.json",
                 "15_zscore_txn_amt_90d.json"):
        parse_proposal_v2(_ok_fixture(name))
    assert set(OPERATION_RULES) == set(AggregateFunctionV2), \
        "every enum member has a rule row — totality is the table's contract"
    assert AGGREGATE_ADDITIVITY_V2[AggregateFunctionV2.LAST_KNOWN] is AdditivityClass.SEMI_ADDITIVE
    assert AGGREGATE_ADDITIVITY_V2[AggregateFunctionV2.ZSCORE] is AdditivityClass.NON_ADDITIVE
    assert all(OPERATION_RULES[agg].order_sensitive
               for agg in (AggregateFunctionV2.LAST_KNOWN, AggregateFunctionV2.FIRST_KNOWN,
                           AggregateFunctionV2.RECENCY, AggregateFunctionV2.ZSCORE)), \
        "the at-cutoff group is meaningless without the event clock — capability reads this"
    bare = _ok_fixture("13_last_known_balance_at_cutoff.json")
    bare["body"]["expr"]["operand"] = None
    with pytest.raises(SchemaError, match="requires an operand"):
        parse_proposal_v2(bare)


def test_lag_and_delta_are_body_compositions_and_the_offset_is_disciplined():
    """Increment 4: LAG = the same aggregate at offset 1; DELTA = a difference body over offsets
    0 and 1. One new identity-bearing window field, zero new operations — and a negative offset
    (a future window in disguise) or an offset beyond the cap refuses."""
    lag = parse_proposal_v2(_ok_fixture("16_lag_prev_period_sum.json"))
    assert lag.body.expr.window.offset_periods == 1
    delta = parse_proposal_v2(_ok_fixture("17_delta_sum_current_minus_prev.json"))
    assert (delta.body.minuend.window.offset_periods,
            delta.body.subtrahend.window.offset_periods) == (0, 1)

    negative = json.loads((_GOLD_V2 / "19_negative_offset_invalid.json").read_text())
    with pytest.raises(SchemaError):
        parse_proposal_v2(negative["proposal"])
    beyond = _ok_fixture("16_lag_prev_period_sum.json")
    beyond["body"]["expr"]["window"]["offset_periods"] = 13
    with pytest.raises(SchemaError):
        parse_proposal_v2(beyond)


def test_date_diff_avg_demands_its_second_column_and_nothing_else_may_carry_one():
    diff = parse_proposal_v2(_ok_fixture("18_date_diff_avg_due_to_paid.json"))
    assert diff.body.expr.second_operand == "authored::public.txns.due_dt"
    smuggled = json.loads((_GOLD_V2 / "20_second_operand_on_sum_invalid.json").read_text())
    with pytest.raises(SchemaError, match="takes no second column"):
        parse_proposal_v2(smuggled["proposal"])
    bare = _ok_fixture("18_date_diff_avg_due_to_paid.json")
    bare["body"]["expr"].pop("second_operand")
    with pytest.raises(SchemaError, match="requires its second column"):
        parse_proposal_v2(bare)


def test_the_trend_and_condition_group():
    """Increment 5: slope (a trend needs a quantity — operand required, order-sensitive, RATE
    result), streak_periods and any_match (the FILTER is the condition, so no operand — like
    count_rows; a flag is a boolean, never a smuggled count)."""
    from featuregen.formula.operations_v2 import operation_rule
    from featuregen.formula.schema_v2 import AggregateFunctionV2

    slope = parse_proposal_v2(_ok_fixture("21_slope_amount_90d.json"))
    assert slope.body.expr.aggregation is AggregateFunctionV2.SLOPE
    streak = parse_proposal_v2(_ok_fixture("22_streak_salary_periods.json"))
    assert streak.body.expr.operand is None and streak.body.expr.filter is not None
    parse_proposal_v2(_ok_fixture("23_any_match_dispute_flag.json"))

    assert operation_rule(AggregateFunctionV2.SLOPE).result_kind == "rate"
    assert operation_rule(AggregateFunctionV2.SLOPE).order_sensitive
    assert operation_rule(AggregateFunctionV2.ANY_MATCH).result_kind == "flag"
    assert not operation_rule(AggregateFunctionV2.STREAK_PERIODS).operand_required

    bare = json.loads((_GOLD_V2 / "24_slope_without_operand_invalid.json").read_text())
    with pytest.raises(SchemaError, match="requires an operand"):
        parse_proposal_v2(bare["proposal"])
    smuggled = _ok_fixture("23_any_match_dispute_flag.json")
    smuggled["body"]["expr"]["operand"] = "authored::public.txns.txn_amt"
    with pytest.raises(SchemaError, match="carries no operand"):
        parse_proposal_v2(smuggled)


def test_the_concentration_group_and_the_optional_second_operand():
    """Increment 6: hhi / top_share — the operand is the GROUPING dimension, the second operand
    the optional weighting measure. Count-based and amount-weighted variants are two identities,
    honestly; and `optional` never weakens `forbidden` elsewhere (sum still refuses one)."""
    from featuregen.formula.canonical_v2 import proposal_content_hash_v2

    weighted = parse_proposal_v2(_ok_fixture("25_hhi_counterparty_amount_weighted.json"))
    count_based = parse_proposal_v2(_ok_fixture("26_top_share_merchant_count_based.json"))
    assert weighted.body.expr.second_operand is not None
    assert count_based.body.expr.second_operand is None
    # dropping the weighting measure is a DIFFERENT feature — a different hash
    unweighted = _ok_fixture("25_hhi_counterparty_amount_weighted.json")
    unweighted["body"]["expr"].pop("second_operand")
    assert (proposal_content_hash_v2(parse_proposal_v2(unweighted))
            != proposal_content_hash_v2(weighted))
    # `optional` is scoped to the concentration ops — sum still refuses a second column
    smuggled = _ok_fixture("04_sum_txn_amt_90d_v2.json")
    smuggled["body"]["expr"]["second_operand"] = "authored::public.txns.txn_dt"
    with pytest.raises(SchemaError, match="takes no second column"):
        parse_proposal_v2(smuggled)


def test_the_future_horizon_carries_contractual_sums_and_refuses_observed_history():
    """Increment 7: future_horizon reads FORWARD — (cutoff, cutoff+L] — and with an offset it is
    the maturity LADDER BUCKET (lag pointed forward). Order-sensitive operations refuse it: they
    read observed history, and a future horizon has none."""
    runoff = parse_proposal_v2(_ok_fixture("27_future_maturity_runoff_sum.json"))
    assert runoff.body.expr.window.basis.value == "future_horizon"
    bucket = parse_proposal_v2(_ok_fixture("28_future_ladder_bucket_offset1.json"))
    assert bucket.body.expr.window.offset_periods == 1

    doomed = json.loads((_GOLD_V2 / "29_last_known_over_future_invalid.json").read_text())
    with pytest.raises(SchemaError, match="future horizon has none"):
        parse_proposal_v2(doomed["proposal"])
    trend_over_future = _ok_fixture("27_future_maturity_runoff_sum.json")
    trend_over_future["body"]["expr"]["aggregation"] = "slope"
    with pytest.raises(SchemaError, match="future horizon has none"):
        parse_proposal_v2(trend_over_future)


def test_the_br18_exemplar_is_fully_expressible_and_authorities_are_identity():
    """Increment 8: the plan's canonical exemplar (posted_debit_amount) parses end to end with
    every governed policy it names — status, direction, reversal, currency — plus the
    account→customer rollup carrying its allocation policy. The authorities are IDENTITY: the
    same computation with and without a reversal policy is two formulas; a vacuous block (every
    ref blank) is a lie and refuses."""
    from featuregen.formula.canonical_v2 import proposal_content_hash_v2

    exemplar = parse_proposal_v2(_ok_fixture("30_posted_debit_amount_exemplar.json"))
    refs = exemplar.body.expr.authority_refs
    assert refs is not None and refs.reversal_policy_ref == "policy:reversal-neutralizes-original"
    rollup = parse_proposal_v2(_ok_fixture("31_customer_rollup_with_allocation.json"))
    assert rollup.allocation_policy_ref == "policy:joint-account-equal-split"

    stripped = _ok_fixture("30_posted_debit_amount_exemplar.json")
    stripped["body"]["expr"].pop("authority_refs")
    assert (proposal_content_hash_v2(parse_proposal_v2(stripped))
            != proposal_content_hash_v2(exemplar)), \
        "a formula that declares no policies is a DIFFERENT formula — honestly so"

    vacuous = json.loads((_GOLD_V2 / "32_vacuous_authority_block_invalid.json").read_text())
    with pytest.raises(SchemaError, match="omit the block instead"):
        parse_proposal_v2(vacuous["proposal"])
