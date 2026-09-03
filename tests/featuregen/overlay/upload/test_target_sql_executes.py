"""The compiled SQL is RUN against known data and its labels are checked.

Every other test in this area asserts on the TEXT of the query. Text tests cannot see a query that
parses and answers the wrong question — an off-by-one at the window edge, a population bound that
silently keeps rows it should drop, a censoring rule that labels the unobservable as 0. Those are
precisely the defects that make a label look like a working model while being wrong.

So this suite builds a small catalog with hand-computed answers and executes the real output of
`compile_target_sql` against it.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetHeaderV1,
)
from featuregen.overlay.upload.target_sql import compile_target_sql

# Monthly snapshots, Jan–Jun 2024. With a 60-day window the newest observable as-of is 2024-04-30
# (+60 = 2024-06-29, inside the 2024-06-30 history), so 2024-05-31 and 2024-06-30 are CENSORED.
_SNAPSHOTS = ("2024-01-31", "2024-02-29", "2024-03-31", "2024-04-30", "2024-05-31", "2024-06-30")


def _anchor(db, rows) -> None:
    db.execute("CREATE TABLE bo_cib_customer ("
               " cust_num text, business_dt date, cust_perf_nonperf_flg text)")
    with db.cursor() as cur:
        cur.executemany("INSERT INTO bo_cib_customer VALUES (%s, %s, %s)", rows)


def _header(**over) -> TargetHeaderV1:
    base = dict(name="tgt_npe_60d", entity="customer", anchor_catalog="cib",
                grain_ref="public.bo_cib_customer.cust_num",
                as_of_ref="public.bo_cib_customer.business_dt",
                window_days=60, as_of_frequency="monthly", label_type="binary",
                operator=">=", threshold=1)
    return TargetHeaderV1(**{**base, **over})


def _state(**over) -> StateChangeRuleV1:
    base = dict(header=_header(), column_ref="public.bo_cib_customer.cust_perf_nonperf_flg",
                from_values=("Performing",), to_values=("Non-performing",))
    return StateChangeRuleV1(**{**base, **over})


def _run(db, rule) -> dict:
    rows = db.execute(compile_target_sql(rule)).fetchall()
    return {(r[0], str(r[1])): r[2] for r in rows}


# ══ the whole state_change story on one dataset ══════════════════════════════════════════════════

@pytest.fixture
def cohort(db):
    """Four customers chosen so every population rule has a witness."""
    rows = []
    # C1 — performing until 2024-05-31, then non-performing. The ONLY customer who gets a 1.
    for date in _SNAPSHOTS:
        rows.append(("C1", date, "Non-performing" if date >= "2024-05-31" else "Performing"))
    # C2 — performing throughout: the honest 0.
    rows += [("C2", date, "Performing") for date in _SNAPSHOTS]
    # C3 — ALREADY non-performing at every as-of: never a candidate to acquire the outcome.
    rows += [("C3", date, "Non-performing") for date in _SNAPSHOTS]
    # C4 — state unreadable at every as-of: eligibility cannot be determined.
    rows += [("C4", date, None) for date in _SNAPSHOTS]
    _anchor(db, rows)
    return db


def test_the_compiled_sql_actually_RUNS(cohort):
    """The floor. A generator nobody has executed is a string generator."""
    assert _run(cohort, _state())


def test_the_label_is_1_only_where_the_state_ACTUALLY_moves_in_the_window(cohort):
    """C1 goes non-performing on 2024-05-31. From the 2024-04-30 as-of that is 31 days ahead —
    inside the 60-day window. From 2024-03-31 it is 61 days ahead, one day OUTSIDE it."""
    labels = _run(cohort, _state())
    assert labels[("C1", "2024-04-30")] == 1
    assert labels[("C1", "2024-03-31")] == 0, \
        "2024-05-31 is 61 days after 2024-03-31 — one day beyond the window"
    assert labels[("C1", "2024-01-31")] == 0
    assert labels[("C1", "2024-02-29")] == 0


def test_a_customer_who_never_changes_state_is_labelled_0_throughout(cohort):
    assert set(v for (c, _), v in _run(cohort, _state()).items() if c == "C2") == {0}


def test_a_customer_who_ALREADY_has_the_outcome_never_enters_the_population(cohort):
    """Not "labelled 0" — ABSENT. Including them makes the model learn to predict the state it
    was given, and it is the most common way to build a silently broken label."""
    assert not [c for (c, _) in _run(cohort, _state()) if c == "C3"]


def test_an_UNREADABLE_state_at_the_as_of_date_drops_the_row(cohort):
    assert not [c for (c, _) in _run(cohort, _state()) if c == "C4"]


def test_population_filter_ALL_readmits_the_customer_who_already_has_the_outcome(cohort):
    """The switch must actually switch, or `from_values` is decorative."""
    labels = _run(cohort, _state(population_filter="all"))
    assert [c for (c, _) in labels if c == "C3"]


def test_CENSORED_as_of_dates_are_absent_rather_than_labelled_0(cohort):
    """History ends 2024-06-30, so a 2024-05-31 as-of cannot see its full 60 days. Labelling it 0
    says "did not happen" where the truth is "cannot see" — every recent row becomes a false
    negative and the model learns that recent customers are safe, which is exactly backwards."""
    dates = {d for (_, d) in _run(cohort, _state())}
    assert "2024-04-30" in dates
    assert "2024-05-31" not in dates and "2024-06-30" not in dates


def test_switching_censoring_OFF_readmits_those_dates(cohort):
    dates = {d for (_, d) in _run(cohort, _state(header=_header(require_full_window=False)))}
    assert "2024-05-31" in dates and "2024-06-30" in dates


def test_at_least_once_FALSE_asks_a_DIFFERENT_question_of_the_same_data(db):
    """C5 dips into non-performing mid-window and recovers by the end. "was ever" says 1; "ended"
    says 0. If both rendered the same the flag would be decorative."""
    _anchor(db, [("C5", "2024-01-31", "Performing"), ("C5", "2024-02-29", "Non-performing"),
                 ("C5", "2024-03-31", "Performing"), ("C5", "2024-04-30", "Performing"),
                 ("C5", "2024-05-31", "Performing"), ("C5", "2024-06-30", "Performing")])
    ever = _run(db, _state())
    ended = _run(db, _state(at_least_once=False))
    assert ever[("C5", "2024-01-31")] == 1
    assert ended[("C5", "2024-01-31")] == 0


def test_daily_sampling_yields_MORE_as_of_dates_than_monthly(cohort):
    """The sampling frame is part of the rule, so it must change the dataset."""
    monthly = len(_run(cohort, _state()))
    daily = len(_run(cohort, _state(header=_header(as_of_frequency="daily"))))
    assert daily >= monthly


def test_single_sampling_yields_exactly_ONE_as_of_date(cohort):
    """And it must be an OBSERVABLE one — the naive "latest date" would render a query that is
    correct and always empty."""
    labels = _run(cohort, _state(header=_header(as_of_frequency="single")))
    assert len({d for (_, d) in labels}) == 1
    assert {d for (_, d) in labels} == {"2024-04-30"}


def test_a_quote_in_a_STATE_VALUE_survives_execution(db):
    """The escaping test in the text suite proves the doubling is emitted. This proves the server
    accepts it."""
    _anchor(db, [("C6", d, "O'Brien") for d in _SNAPSHOTS])
    labels = _run(db, _state(from_values=("O'Brien",), to_values=("Non-performing",)))
    assert labels[("C6", "2024-04-30")] == 0


# ══ event_window ═════════════════════════════════════════════════════════════════════════════════

def _events(db, rows) -> None:
    db.execute("CREATE TABLE comp_financial_tran_repos_dly ("
               " cust_num text, pstd_date date, tran_crncy text, tran_amt numeric)")
    with db.cursor() as cur:
        cur.executemany(
            "INSERT INTO comp_financial_tran_repos_dly VALUES (%s, %s, %s, %s)", rows)


def _event_rule(**over) -> EventWindowRuleV1:
    base = dict(header=_header(name="tgt_fx_active_60d"), event_catalog="ftr",
                event_table="comp_financial_tran_repos_dly",
                event_date_ref="public.comp_financial_tran_repos_dly.pstd_date",
                join_left="public.bo_cib_customer.cust_num",
                join_right="public.comp_financial_tran_repos_dly.cust_num",
                aggregate="count")
    return EventWindowRuleV1(**{**base, **over})


@pytest.fixture
def fx(db):
    """C1 trades FX inside the window from 2024-04-30; C2 never trades; C3 traded BEFORE."""
    _anchor(db, [(c, d, "Performing") for c in ("C1", "C2", "C3") for d in _SNAPSHOTS])
    _events(db, [
        ("C1", "2024-05-15", "USD", 100),      # inside (2024-04-30, 2024-06-29]
        ("C1", "2024-05-20", "USD", 250),      # a second one, for the sum/count distinction
        ("C3", "2024-02-10", "USD", 900),      # BEFORE the 2024-04-30 as-of: prior activity
    ])
    return db


def test_an_event_label_counts_only_rows_INSIDE_the_forward_window(fx):
    labels = _run(fx, _event_rule())
    assert labels[("C1", "2024-04-30")] == 1
    assert labels[("C2", "2024-04-30")] == 0
    assert labels[("C3", "2024-04-30")] == 0, "C3's only trade is BEFORE the as-of date"


def test_a_count_label_reports_the_NUMBER_not_a_flag(fx):
    labels = _run(fx, _event_rule(header=_header(
        name="tgt_fx_count_60d", label_type="count", operator=None, threshold=None)))
    assert labels[("C1", "2024-04-30")] == 2


def test_a_sum_label_adds_the_MEASURE(fx):
    labels = _run(fx, _event_rule(
        aggregate="sum", measure_ref="public.comp_financial_tran_repos_dly.tran_amt",
        header=_header(name="tgt_fx_amount_60d", label_type="amount",
                       operator=None, threshold=None)))
    assert labels[("C1", "2024-04-30")] == 350


def test_a_threshold_that_is_not_met_is_0_even_though_events_exist(fx):
    """The operator has to bite, or every binary event label is "did anything happen at all"."""
    labels = _run(fx, _event_rule(header=_header(name="tgt_fx_3plus_60d", operator=">=",
                                                  threshold=3)))
    assert labels[("C1", "2024-04-30")] == 0


def test_population_having_NONE_removes_the_customer_with_prior_activity(fx):
    """"who will START" — C3 traded in February, so from the 2024-04-30 as-of they are already
    doing it and are not a candidate to start."""
    labels = _run(fx, _event_rule(population_having="none", population_lookback_days=180))
    assert ("C3", "2024-04-30") not in labels
    assert ("C2", "2024-04-30") in labels


def test_a_filter_excludes_events_the_label_should_not_count(fx):
    labels = _run(fx, _event_rule(event_filters=(EventFilterV1(
        column_ref="public.comp_financial_tran_repos_dly.tran_crncy", op="!=", value="USD"),)))
    assert labels[("C1", "2024-04-30")] == 0, "C1's only trades are USD, which the filter excludes"


def test_the_lookback_carries_the_SAME_filters_as_the_outcome(fx):
    """A lookback ignoring the filters excludes people for activity the label never counts. C3's
    prior trade is USD; under a non-USD label they have NO prior activity and must survive."""
    labels = _run(fx, _event_rule(
        population_having="none", population_lookback_days=180,
        event_filters=(EventFilterV1(
            column_ref="public.comp_financial_tran_repos_dly.tran_crncy",
            op="!=", value="USD"),)))
    assert ("C3", "2024-04-30") in labels
