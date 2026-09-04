"""The rule as runnable SQL — the derivation logic, not just the specification.

Option B stands: the platform does not EXECUTE this. It emits it, so the person who registered the
label can read the logic that will produce their training data, and hand it to whatever runs it.

These tests pin MEANING, not formatting: the population bound, the forward window, the censoring
rule, the sampling frame, and the two escaping surfaces. A label whose SQL is subtly wrong is worse
than one with no SQL at all, because the wrongness arrives dressed as an artifact.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetHeaderV1,
)
from featuregen.overlay.upload.target_sql import SqlRenderError, compile_target_sql

_ANCHOR = "public.bo_cib_customer"
_GRAIN = f"{_ANCHOR}.cust_num"
_ASOF = f"{_ANCHOR}.business_dt"
_FLAG = f"{_ANCHOR}.cust_perf_nonperf_flg"


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_90d", entity="customer", anchor_catalog="cib",
                grain_ref=_GRAIN, as_of_ref=_ASOF, window_days=90,
                as_of_frequency="monthly", label_type="binary",
                operator=">=", threshold=1)
    return TargetHeaderV1(**{**base, **over})


def _state(**over) -> StateChangeRuleV1:
    base = dict(header=_header(), column_ref=_FLAG,
                from_values=("Performing",), to_values=("Non-performing",))
    return StateChangeRuleV1(**{**base, **over})


def _event(**over) -> EventWindowRuleV1:
    base = dict(
        header=_header(name="tgt_fx_active_90d"),
        event_catalog="ftr", event_table="comp_financial_tran_repos_dly",
        event_date_ref="public.comp_financial_tran_repos_dly.pstd_date",
        join_left=_GRAIN, join_right="public.comp_financial_tran_repos_dly.cust_num",
        aggregate="count")
    return EventWindowRuleV1(**{**base, **over})


# ══ the forward window — the one inverted property ═══════════════════════════════════════════════

def test_the_window_reads_STRICTLY_FORWARD_of_the_as_of_date():
    """The property that gives labels their own lane. `>` not `>=`: a row observed AT the as-of
    date is the feature's to read, and admitting it makes the label partly a copy of its input."""
    sql = compile_target_sql(_state())
    assert ">  p.as_of_date" in sql
    assert ">= p.as_of_date" not in sql, "inclusive of the as-of date is a FEATURE's window"
    assert "<= p.as_of_date + INTERVAL '90 days'" in sql


def test_the_window_is_never_rendered_BACKWARD():
    """A backward window is a feature. The contract already refuses `direction`, but the renderer
    is the last place the mistake could be reintroduced silently."""
    sql = compile_target_sql(_state())
    assert "- INTERVAL '90 days'" not in sql.split("prior_activity")[0]


# ══ censoring ════════════════════════════════════════════════════════════════════════════════════

def test_require_full_window_BOUNDS_the_population_by_observable_history():
    """Without this every recent row is labelled 0 — "did not happen" when the truth is "cannot
    see" — and the model learns that recent customers are safe, which is exactly backwards."""
    sql = compile_target_sql(_state())
    assert "max_as_of" in sql
    assert "<= o.max_as_of" in sql


def test_switching_censoring_OFF_removes_the_bound_and_says_so():
    """A survival design handles censoring itself. It must be deliberate and visible."""
    sql = compile_target_sql(_state(header=_header(require_full_window=False)))
    assert "<= o.max_as_of" not in sql
    assert "CENSORING OFF" in sql


# ══ the sampling frame ═══════════════════════════════════════════════════════════════════════════

def test_the_as_of_frequency_picks_the_LAST_date_in_each_period():
    """Two teams sampling "the same" label at different frequencies get different training sets.
    The frequency is in the rule, so it must be in the SQL."""
    sql = compile_target_sql(_state())
    assert "date_trunc('month'" in sql
    assert "ROW_NUMBER() OVER" in sql


def test_daily_sampling_takes_every_as_of_date_without_a_window_function():
    sql = compile_target_sql(_state(header=_header(as_of_frequency="daily")))
    assert "ROW_NUMBER() OVER" not in sql


def test_single_sampling_takes_only_the_latest_as_of_date():
    sql = compile_target_sql(_state(header=_header(as_of_frequency="single")))
    assert "MAX(" in sql and "ROW_NUMBER() OVER" not in sql


# ══ state_change ═════════════════════════════════════════════════════════════════════════════════

def test_the_population_EXCLUDES_rows_that_already_have_the_outcome():
    """Omitting this is the most common way to build a silently broken label: a customer already
    non-performing on 1 January is not a candidate for becoming non-performing."""
    sql = compile_target_sql(_state())
    population = sql.split("outcome AS")[0]
    assert "IN ('Performing')" in population


def test_population_filter_all_keeps_everyone():
    sql = compile_target_sql(_state(population_filter="all"))
    assert "IN ('Performing')" not in sql.split("outcome AS")[0]


def test_a_NULL_at_the_as_of_date_drops_the_row_by_default():
    """A NULL means eligibility cannot be determined; including it invents an answer."""
    assert "IS NOT NULL" in compile_target_sql(_state())


def test_at_least_once_FALSE_reads_the_state_at_the_END_of_the_window():
    """"ended non-performing" and "was ever non-performing" are different labels, and rendering
    both the same way would make the flag decorative."""
    once = compile_target_sql(_state())
    final = compile_target_sql(_state(at_least_once=False))
    assert once != final
    assert "last_in_window" in final


# ══ event_window ═════════════════════════════════════════════════════════════════════════════════

def test_an_event_rule_joins_the_EVENT_side_in_its_own_catalog():
    sql = compile_target_sql(_event())
    assert "comp_financial_tran_repos_dly" in sql
    assert "ftr" in sql


def test_population_having_none_excludes_PRIOR_activity_in_the_lookback():
    """"who will START" versus "who will do it at all" — `any` silently yields the degenerate
    label, so `none` must actually restrict the population."""
    sql = compile_target_sql(_event(population_having="none",
                                    population_lookback_days=180))
    assert "prior_activity" in sql
    assert "- INTERVAL '180 days'" in sql
    assert "IS NULL" in sql


def test_population_having_any_adds_no_prior_activity_stage():
    assert "prior_activity" not in compile_target_sql(_event())


def test_a_sum_aggregate_adds_the_MEASURE_not_the_rows():
    sql = compile_target_sql(_event(
        aggregate="sum", measure_ref="public.comp_financial_tran_repos_dly.tran_amt",
        header=_header(name="tgt_fx_amount_90d", label_type="amount",
                       operator=None, threshold=None)))
    assert "SUM(" in sql and '"tran_amt"' in sql


def test_event_filters_are_ANDed_into_BOTH_the_outcome_and_the_lookback():
    """A lookback that ignores the filters excludes people for activity the label never counts —
    "has not traded FX" would drop anyone who made any payment at all."""
    sql = compile_target_sql(_event(
        population_having="none", population_lookback_days=180,
        event_filters=(EventFilterV1(
            column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
            op="!=", value="AED"),)))
    assert sql.count("<> 'AED'") == 2


def test_a_list_filter_renders_as_IN():
    sql = compile_target_sql(_event(event_filters=(EventFilterV1(
        column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
        op="in", values=("USD", "EUR")),)))
    assert "IN ('USD', 'EUR')" in sql


def test_a_value_ref_filter_compares_two_COLUMNS_and_quotes_neither_as_a_literal():
    sql = compile_target_sql(_event(event_filters=(EventFilterV1(
        column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
        op="!=", value_ref="public.comp_financial_tran_repos_dly.base_crncy"),)))
    assert '"base_crncy"' in sql
    assert "'base_crncy'" not in sql


# ══ the label column ═════════════════════════════════════════════════════════════════════════════

def test_a_binary_label_applies_the_OPERATOR_and_THRESHOLD():
    sql = compile_target_sql(_event(header=_header(name="tgt_fx_5plus_90d", operator=">=",
                                                   threshold=5)))
    assert ">= 5" in sql


def test_a_count_label_emits_the_MEASURE_and_never_thresholds_it():
    sql = compile_target_sql(_event(header=_header(
        name="tgt_fx_count_90d", label_type="count", operator=None, threshold=None)))
    assert "CASE WHEN" not in sql.rsplit("SELECT", 1)[1]


def test_the_label_column_is_NAMED_for_the_target():
    assert '"tgt_npe_90d"' in compile_target_sql(_state())


# ══ escaping — the two surfaces ══════════════════════════════════════════════════════════════════

def test_a_quote_in_a_VALUE_is_escaped_not_interpolated():
    """Values are human-entered and stored. Doubling is the whole defence, and it must be tested
    rather than assumed."""
    sql = compile_target_sql(_state(to_values=("O'Brien",)))
    assert "'O''Brien'" in sql


def test_an_IDENTIFIER_that_is_not_a_plain_name_is_REFUSED_not_quoted_around():
    """Refs come from the catalog, so this should be unreachable — which is exactly why it must
    fail loudly if it ever is reached, rather than emitting something that parses."""
    with pytest.raises(SqlRenderError, match="identifier"):
        compile_target_sql(_state(column_ref='public.t."; DROP TABLE x --'))


def test_a_NUL_byte_in_a_value_is_REFUSED():
    with pytest.raises(SqlRenderError, match="NUL"):
        compile_target_sql(_state(to_values=("bad\x00value",)))


# ══ honesty about what the platform does not know ════════════════════════════════════════════════

def test_the_header_names_the_CATALOGS_because_the_platform_has_no_physical_mapping():
    """The registry records a catalog, never a database. Emitting a confident three-part name would
    silently read the wrong table; naming the binding makes the consumer supply what only they
    know."""
    sql = compile_target_sql(_event())
    assert "Source bindings" in sql
    assert "cib" in sql and "ftr" in sql


def test_the_sentence_travels_WITH_the_sql():
    """The SQL is checked by whoever runs it, who was not necessarily in the room when it was
    approved. The statement of meaning must arrive with it."""
    assert "one row per customer" in compile_target_sql(_state())


# ══ rehydration ══════════════════════════════════════════════════════════════════════════════════

from featuregen.overlay.upload.target_contract import (  # noqa: E402
    canonical_target,
    target_from_canonical,
)


def test_a_stored_rule_can_be_read_BACK_into_the_type_that_validates_it():
    """The registry stores rules as jsonb. Without this they are a write-only record: nothing can
    render, re-describe, or re-check a label once it has been registered."""
    for rule in (_state(), _event()):
        assert canonical_target(target_from_canonical(canonical_target(rule))) == \
            canonical_target(rule)


def test_rehydration_restores_TUPLES_not_lists():
    """`asdict` flattens tuples to lists, and a list where the contract declares a tuple compares
    unequal through the content hash — the same defect that made `near_duplicates` unable to fire."""
    rule = target_from_canonical(canonical_target(_state()))
    assert isinstance(rule.from_values, tuple)


def test_rehydration_restores_nested_EVENT_FILTERS():
    rule = target_from_canonical(canonical_target(_event(event_filters=(EventFilterV1(
        column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
        op="in", values=("USD", "EUR")),))))
    assert rule.event_filters[0].values == ("USD", "EUR")


def test_a_stored_rule_that_is_INVALID_is_refused_on_the_way_back_in():
    """Rehydration goes through the constructors, so a hand-edited row cannot become a rule the
    contract would never have accepted."""
    body = canonical_target(_state())
    body["header"]["direction"] = "backward"
    with pytest.raises(Exception, match="forward"):
        target_from_canonical(body)


def test_a_rehydrated_rule_compiles_to_the_SAME_sql():
    assert compile_target_sql(target_from_canonical(canonical_target(_state()))) == \
        compile_target_sql(_state())


# ══ label_type and aggregate must cohere ═════════════════════════════════════════════════════════

def test_a_COUNT_label_cannot_ride_a_SUM_aggregate():
    """"The count of transactions" reported from SUM(amount) is a number wearing the wrong name.
    Only a binary label genuinely chooses its aggregate — count and amount ARE the choice."""
    with pytest.raises(Exception, match="count label"):
        _event(aggregate="sum",
               measure_ref="public.comp_financial_tran_repos_dly.tran_amt",
               header=_header(name="tgt_x_90d", label_type="count",
                              operator=None, threshold=None))


def test_an_AMOUNT_label_cannot_ride_a_COUNT_aggregate():
    with pytest.raises(Exception, match="amount label"):
        _event(aggregate="count",
               header=_header(name="tgt_x_90d", label_type="amount",
                              operator=None, threshold=None))


def test_a_BINARY_label_may_choose_either_aggregate():
    compile_target_sql(_event())                      # count
    compile_target_sql(_event(
        aggregate="sum", measure_ref="public.comp_financial_tran_repos_dly.tran_amt"))
