"""The derived-label contract: a label is a RULE, refused at construction when malformed."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetContractError,
    TargetHeaderV1,
)


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
                grain_ref="public.bo_cib_customer.cust_num",
                as_of_ref="public.bo_cib_customer.business_dt",
                window_days=90, label_type="binary", operator=">=", threshold=1.0)
    return TargetHeaderV1(**{**base, **over})


def test_a_well_formed_binary_header_is_accepted():
    h = _header()
    assert (h.name, h.window_days, h.direction) == ("tgt_npe_90d", 90, "forward")


def test_the_name_must_carry_the_tgt_prefix():
    """The prefix is the owner's decision and it is what makes a label recognisable in a
    registry it shares with nothing else."""
    with pytest.raises(TargetContractError, match="name"):
        _header(name="npe_90d")


def test_direction_is_always_forward_and_a_backward_rule_is_REFUSED():
    """A rule that reads backward from the as-of date is a FEATURE. Correcting it silently
    would hide the confusion; the refusal is the point."""
    with pytest.raises(TargetContractError, match="forward"):
        _header(direction="backward")


def test_the_anchor_catalog_is_mandatory():
    """`graph_node.object_ref` is only `public.{table}.{column}` — a bare ref does not identify
    a column (M3), which is why `_column_meta` scopes every lookup to a pair."""
    with pytest.raises(TargetContractError, match="anchor_catalog"):
        _header(anchor_catalog="")


def test_a_binary_label_REQUIRES_operator_and_threshold():
    with pytest.raises(TargetContractError, match="binary"):
        _header(operator=None, threshold=None)


def test_an_unrecognised_operator_is_reported_as_such():
    """"Requires an operator" misdirects when one WAS supplied and is simply not a comparison."""
    with pytest.raises(TargetContractError, match="operator"):
        _header(operator="~=")


def test_a_count_label_FORBIDS_operator_and_threshold():
    """`count` measures; it does not threshold. Carrying both is the field pair most likely to
    be filled in inconsistently, so it is checked rather than trusted."""
    with pytest.raises(TargetContractError, match="count"):
        _header(label_type="count", operator=">=", threshold=1.0)


def test_a_count_label_without_a_threshold_is_accepted():
    assert _header(label_type="count", operator=None, threshold=None).label_type == "count"


def test_the_window_must_be_positive():
    with pytest.raises(TargetContractError, match="window_days"):
        _header(window_days=0)


# ══ the state_change shape ═══════════════════════════════════════════════════════════════════════

def _state(**over) -> StateChangeRuleV1:
    base = dict(header=_header(),
                column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
                from_values=("Performing",), to_values=("Non-performing",))
    return StateChangeRuleV1(**{**base, **over})


def test_a_well_formed_state_change_rule_is_accepted():
    r = _state()
    assert r.column_ref.endswith("cust_perf_nonperf_flg")
    assert r.population_filter == "from_values"


def test_a_value_in_BOTH_from_and_to_is_incoherent():
    """If Performing is both the starting state and the outcome, the rule asks whether a
    customer changed from a state to itself. Silently always-0; caught here instead."""
    with pytest.raises(TargetContractError, match="both"):
        _state(from_values=("Performing",), to_values=("Performing", "Non-performing"))


def test_from_and_to_values_are_both_mandatory():
    """Empty values are how a label becomes silently always-0 — nothing matches."""
    with pytest.raises(TargetContractError, match="from_values"):
        _state(from_values=())


def test_a_state_change_label_must_be_binary():
    """A state either changed or it did not; counting a change is a different rule shape."""
    with pytest.raises(TargetContractError, match="binary"):
        _state(header=_header(label_type="count", operator=None, threshold=None))


def test_the_watched_column_cannot_BE_the_anchor_date():
    """Comparing a date against itself observes nothing."""
    with pytest.raises(TargetContractError, match="same column"):
        _state(column_ref="public.bo_cib_customer.business_dt")


# ══ the event_window shape ═══════════════════════════════════════════════════════════════════════

def _event(**over) -> EventWindowRuleV1:
    base = dict(header=_header(name="tgt_fx_new_60d", window_days=60),
                event_catalog="ftr",
                event_table="public.comp_financial_tran_repos_dly",
                event_date_ref="public.comp_financial_tran_repos_dly.pstd_date",
                join_left="public.bo_cib_customer.cust_num",
                join_right="public.comp_financial_tran_repos_dly.cif_id",
                event_filter="tran_crncy <> counter_party_tran_crncy",
                aggregate="count")
    return EventWindowRuleV1(**{**base, **over})


def test_a_well_formed_event_window_rule_is_accepted():
    assert _event().aggregate == "count"


def test_the_default_population_is_the_WHOLE_population():
    """Explicit, because it is the degenerate choice: on "who will do FX" it means customers
    already trading FX dominate and the model restates last month."""
    assert (_event().population_having, _event().population_lookback_days) == ("any", 0)


def test_excluding_prior_activity_REQUIRES_a_lookback():
    """"Who will START doing FX" is meaningless without saying how far back "not currently" looks."""
    with pytest.raises(TargetContractError, match="lookback"):
        _event(population_having="none", population_lookback_days=0)


def test_a_new_to_activity_population_is_accepted():
    r = _event(population_having="none", population_lookback_days=180)
    assert r.population_lookback_days == 180


def test_the_event_catalog_is_mandatory():
    """The event side is routinely a DIFFERENT catalog from the anchor — that is what makes this
    shape cross-catalog, and why it cannot be left implied."""
    with pytest.raises(TargetContractError, match="event_catalog"):
        _event(event_catalog="")


def test_a_sum_aggregate_REQUIRES_a_measure():
    with pytest.raises(TargetContractError, match="measure_ref"):
        _event(header=_header(name="tgt_fx_volume_60d", label_type="amount",
                              operator=None, threshold=None),
               aggregate="sum")


def test_a_count_aggregate_FORBIDS_a_measure():
    with pytest.raises(TargetContractError, match="measure_ref"):
        _event(measure_ref="public.comp_financial_tran_repos_dly.tran_amt_aed")
