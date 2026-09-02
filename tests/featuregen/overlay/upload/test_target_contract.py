"""The derived-label contract: a label is a RULE, refused at construction when malformed."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetContractError,
    TargetHeaderV1,
    canonical_target,
    describe_target,
    refs_read,
    target_content_hash,
)


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
                grain_ref="public.bo_cib_customer.cust_num",
                as_of_ref="public.bo_cib_customer.business_dt",
                window_days=90, as_of_frequency="monthly", label_type="binary", operator=">=", threshold=1.0)
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
                event_filters=(EventFilterV1(
                    column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
                    op="!=",
                    value_ref="public.comp_financial_tran_repos_dly.counter_party_tran_crncy"),),
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


# ══ lineage and identity ═════════════════════════════════════════════════════════════════════════

def test_refs_read_names_every_column_the_rule_touches():
    """This is the lineage answer — "tran_crncy is being retired, which labels break?" — NOT a
    leakage blocklist. A feature reading the same columns BACKWARD is the method, not a leak."""
    assert refs_read(_event()) == (
        ("cib", "public.bo_cib_customer.business_dt"),
        ("cib", "public.bo_cib_customer.cust_num"),
        ("ftr", "public.comp_financial_tran_repos_dly.cif_id"),
        # BOTH sides of the filter are here. Under the old free-text filter they were invisible,
        # so lineage would have said nothing depended on these columns.
        ("ftr", "public.comp_financial_tran_repos_dly.counter_party_tran_crncy"),
        ("ftr", "public.comp_financial_tran_repos_dly.pstd_date"),
        ("ftr", "public.comp_financial_tran_repos_dly.tran_crncy"),
    )


def test_refs_read_keeps_the_two_SIDES_of_a_join_apart():
    """`join_left` is on the anchor and `join_right` on the event side. Collapsing them to bare
    refs is exactly the M3 defect `_column_meta` exists to avoid."""
    pairs = refs_read(_event())
    assert ("cib", "public.bo_cib_customer.cust_num") in pairs
    assert ("ftr", "public.comp_financial_tran_repos_dly.cif_id") in pairs


def test_refs_read_includes_the_measure_when_there_is_one():
    r = _event(header=_header(name="tgt_fx_volume_60d", label_type="amount",
                              operator=None, threshold=None),
               aggregate="sum",
               measure_ref="public.comp_financial_tran_repos_dly.tran_amt_aed")
    assert ("ftr", "public.comp_financial_tran_repos_dly.tran_amt_aed") in refs_read(r)


def test_refs_read_for_a_state_change_includes_the_watched_column():
    assert ("cib", "public.bo_cib_customer.cust_perf_nonperf_flg") in refs_read(_state())


def test_the_content_hash_is_stable_for_an_identical_rule():
    """Content-addressing is what makes an identical rule authored twice ONE row, and any edit a
    new definition rather than a mutation of one other models are already trained against."""
    assert target_content_hash(_state()) == target_content_hash(_state())


def test_changing_the_window_changes_the_hash():
    other = _state(header=_header(window_days=60))
    assert target_content_hash(_state()) != target_content_hash(other)


def test_the_canonical_body_carries_the_shape():
    assert canonical_target(_state())["shape"] == "state_change"
    assert canonical_target(_event())["shape"] == "event_window"


# ══ the closed filter structure ══════════════════════════════════════════════════════════════════

def test_a_filter_may_compare_a_column_to_a_LITERAL():
    f = EventFilterV1(column_ref="public.t.tran_crncy", op="!=", value="AED")
    assert (f.op, f.value) == ("!=", "AED")


def test_a_filter_may_compare_a_column_to_ANOTHER_COLUMN():
    """The truer FX definition — a conversion actually happened — needs no literal at all, which
    also means no unverifiable value to guess."""
    f = EventFilterV1(column_ref="public.t.tran_crncy", op="!=",
                      value_ref="public.t.counter_party_tran_crncy")
    assert f.value_ref.endswith("counter_party_tran_crncy")


def test_exactly_one_kind_of_right_hand_side_is_required():
    with pytest.raises(TargetContractError, match="exactly one"):
        EventFilterV1(column_ref="public.t.c", op="!=")
    with pytest.raises(TargetContractError, match="exactly one"):
        EventFilterV1(column_ref="public.t.c", op="!=", value="A", value_ref="public.t.d")


def test_an_unrecognised_operator_is_refused():
    """A CLOSED set. This is the whole point of the change — an open operator is an open grammar."""
    with pytest.raises(TargetContractError, match="op"):
        EventFilterV1(column_ref="public.t.c", op="LIKE", value="%AED%")


def test_in_requires_a_LIST_and_scalar_ops_forbid_one():
    assert EventFilterV1(column_ref="public.t.c", op="in", values=("USD", "EUR")).values
    with pytest.raises(TargetContractError, match="values"):
        EventFilterV1(column_ref="public.t.c", op="in", value="USD")
    with pytest.raises(TargetContractError, match="values"):
        EventFilterV1(column_ref="public.t.c", op="!=", values=("USD",))


def test_the_filters_COLUMNS_reach_lineage():
    """The defect the free-text filter had: `tgt_fx_active_90d` reads `tran_crncy`, and
    `refs_read` could not see it, so `target_derives_from` would answer "no labels depend on this
    column" while one silently did."""
    rule = _event(event_filters=(
        EventFilterV1(column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
                      op="!=", value="AED"),))
    assert ("ftr", "public.comp_financial_tran_repos_dly.tran_crncy") in refs_read(rule)


def test_a_column_to_column_filter_puts_BOTH_sides_in_lineage():
    rule = _event()  # the default fixture compares tran_crncy to counter_party_tran_crncy
    pairs = refs_read(rule)
    assert ("ftr", "public.comp_financial_tran_repos_dly.tran_crncy") in pairs
    assert ("ftr", "public.comp_financial_tran_repos_dly.counter_party_tran_crncy") in pairs


# ══ what a training set actually needs ═══════════════════════════════════════════════════════════

def test_the_sampling_frequency_is_MANDATORY():
    """A rule that does not say WHICH as-of dates does not define a dataset. Two teams using "the
    same" label at different frequencies get different training sets, which destroys the
    comparability the registry exists to provide."""
    with pytest.raises(TargetContractError, match="as_of_frequency"):
        _header(as_of_frequency="")


def test_the_sampling_frequency_is_a_closed_vocabulary():
    with pytest.raises(TargetContractError, match="as_of_frequency"):
        _header(as_of_frequency="whenever")


def test_a_full_observation_window_is_REQUIRED_by_default():
    """CENSORING. A customer as-of 15 November with a 90-day window needs data through 13 February.
    If history ends before that, the outcome is UNOBSERVABLE — and a rule that labels it 0 says
    "did not happen" when the truth is "cannot see". Every recent row becomes a false negative and
    the model learns that recent customers are safe, which is exactly backwards."""
    assert _header().require_full_window is True


def test_incomplete_windows_can_be_admitted_DELIBERATELY():
    """Some designs want them (a survival model handling censoring itself). Allowed, but never by
    accident — the default refuses and the exception is on the record."""
    assert _header(require_full_window=False).require_full_window is False


def test_a_state_change_excludes_rows_whose_state_is_UNREADABLE_at_the_as_of_date():
    """A NULL flag at the as-of date means the row's eligibility cannot be determined. Including it
    silently invents an answer; the default drops it."""
    assert _state().exclude_null_at_as_of is True


# ══ the sentence a person actually gives concurrence to ══════════════════════════════════════════

def test_a_state_change_rule_renders_as_one_plain_sentence():
    """A person approving twelve JSON fields is rubber-stamping. This is the statement of MEANING
    they check — deterministic, no model call, so it can never drift from the rule."""
    said = describe_target(_state())
    assert "one row per customer" in said
    assert "cust_perf_nonperf_flg" in said
    assert "Performing" in said and "Non-performing" in said
    assert "90 days" in said


def test_an_event_window_rule_says_what_is_counted_and_over_what():
    said = describe_target(_event())
    assert "60 days" in said
    assert "comp_financial_tran_repos_dly" in said
    assert "at least 1" in said


def test_the_sentence_states_CENSORING_and_the_sampling_frame():
    """The two things a data scientist checks first, and the two the form would otherwise bury."""
    said = describe_target(_state())
    assert "monthly" in said
    assert "full 90 days" in said
