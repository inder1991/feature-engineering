"""BR-18 — the account foundation pack: balance-ladder, limit and dormancy primitives."""
from __future__ import annotations

from featuregen.overlay.upload.recipes.account_foundation import ACCOUNT_FOUNDATION_RECIPES

BY_ID = {r.recipe_id: r for r in ACCOUNT_FOUNDATION_RECIPES}


def test_every_account_primitive_stays_at_account_grain():
    assert all(r.output_grain == "account" for r in ACCOUNT_FOUNDATION_RECIPES)
    assert len(ACCOUNT_FOUNDATION_RECIPES) == 20


def test_the_balance_ladder_reads_latest_known_snapshots():
    for rid in ("end_of_day_balance", "average_daily_balance", "minimum_daily_balance",
                "maximum_daily_balance", "maximum_balance_drawdown"):
        r = BY_ID[rid]
        assert "latest-known" in r.temporal.snapshot_policy, rid
        assert r.output.currency_policy.startswith("currency_conversion:"), rid
    assert BY_ID["end_of_day_balance"].output.additivity == "semi_additive"
    assert BY_ID["minimum_daily_balance"].output.additivity == "non_additive"


def test_limit_primitives_read_the_governed_limit_record():
    for rid in ("limit_headroom", "excess_limit_episode_count",
                "excess_limit_episode_max_days"):
        r = BY_ID[rid]
        assert any(op.status_policy_ref.startswith("threshold:account-limit")
                   for op in r.operands), rid
    assert "unlimited account" in BY_ID["limit_headroom"].output.empty_population_policy


def test_interest_paid_and_charged_are_two_recipes_with_two_roles():
    paid, charged = BY_ID["interest_paid_amount"], BY_ID["interest_charged_amount"]
    assert any(op.concept == "interest_income" for op in paid.operands)
    assert any(op.concept == "interest_expense" for op in charged.operands)
    assert "its own recipe" in paid.eligibility.excluded


def test_dormancy_reads_the_governed_definition_never_absence():
    for rid in ("dormant_day_count", "reactivation_flag"):
        r = BY_ID[rid]
        assert any(op.status_policy_ref.startswith("active_state:dormancy")
                   for op in r.operands), rid
    assert "absence alone" in BY_ID["dormant_day_count"].eligibility.excluded


def test_salary_primacy_is_a_governed_designation_never_inferred():
    r = BY_ID["primary_salary_account_flag"]
    assert any(op.status_policy_ref.startswith("active_state:primary-salary")
               for op in r.operands)
    assert "one salary-like credit" in r.eligibility.excluded
    assert "unknown" in r.output.null_input_policy


def test_switch_precursors_stay_conceptual_until_the_event_set_is_reviewed():
    r = BY_ID["account_switch_precursor_pattern"]
    assert r.computation_kind == "conceptual_pattern"
    assert "leakage boundary" in r.conceptual_reason


def test_no_duplicate_of_the_migrated_or_transaction_primitives():
    """The plan's list overlaps already-existing atoms; one id, one recipe — the overlaps are
    deliberately absent here and the registry law would refuse them anyway."""
    for absent in ("balance_slope", "normalized_balance_slope", "balance_volatility",
                   "net_account_flow", "debit_turnover", "account_activity_recency"):
        assert absent not in BY_ID
