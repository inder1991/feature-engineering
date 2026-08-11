"""BR-18 — the account foundation pack: 20 atomic account-grain primitives.

Balance-ladder primitives read the latest-known end-of-day snapshot; limit primitives pair the
governed limit record with balances at the SAME as-of; overdraft and excess-limit episodes are
counted against the governed limit, never a guessed zero line; interest paid and charged are
two recipes (two economic roles); dormancy reads the governed dormancy definition; the
primary-salary designation is a GOVERNED policy fact, never inferred from one credit; and the
closure/switch precursor stays CONCEPTUAL until the precursor event set is reviewed — a
pre-outcome feature whose event set nobody reviewed is a leakage guess.

Deliberately NOT re-authored here (the plan's list overlaps recipes that already exist as
migrated atoms — one id, one recipe): balance_slope / normalized_balance_slope /
balance_volatility (retail pack), debit/credit turnover and net account flow
(posted_debit_amount / posted_credit_amount / net_posted_transaction_flow, transaction pack),
fee amount (transaction pack; the RATIO is new here), activity recency
(transaction_recency_days).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes._shared import (
    as_of,
    entity,
    event_ts,
    event_window,
    measure,
    policy_input,
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

DEPOSIT_STABILITY = "treasury_alm.deposit_stability"
LIQUIDITY = "treasury_alm.liquidity"
LIMIT_MGMT = "credit.monitoring.limit_management"
OVERDRAFT = "customer.overdraft_propensity"
NIM = "treasury_alm.net_interest_margin"
CHURN = "customer.relationship_attrition.churn"

ACCT_CCY = "currency_conversion:foundation-base-currency"
ACCT_LIMIT = "threshold:account-limit-record"
DORMANCY = "active_state:dormancy-definition"
SALARY_DESIGNATION = "active_state:primary-salary-designation"

_SNAPSHOT = snapshot_window("latest-known end-of-day snapshot at or before each day's cutoff")

_SNAPSHOT_ELIGIBILITY = EligibilitySpecV2(
    included="latest-known end-of-day balance snapshots",
    excluded="intraday and superseded snapshots",
    policy_refs=(ACCT_CCY,))


def _balance_operands() -> tuple:
    return (entity("account", "account_id", "account_day_snapshot"),
            measure("balance", "monetary_stock", "account_day_snapshot"),
            as_of("account_day_snapshot"))


def _snap(recipe_id: str, *, definition: str, context: str, output: OutputSpecV2,
          result_class: str, objective: str = DEPOSIT_STABILITY,
          operands: tuple = (), eligibility: EligibilitySpecV2 = _SNAPSHOT_ELIGIBILITY,
          ) -> RecipeDefinitionV2:
    return RecipeDefinitionV2(
        recipe_id=recipe_id, revision=1, family="account_foundation",
        primary_objective=objective,
        business_definition=definition, decision_context=context,
        computation_kind="deterministic_formula",
        output=output, operands=operands or _balance_operands(),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=eligibility,
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref=f"foundation:{recipe_id}",
                                   result_class=result_class))


def _money(output_id: str, label: str, *, additivity: str = "semi_additive",
           aggregation: str = "latest over time; sum across accounts within one currency",
           ) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="numeric", additivity=additivity, unit_kind="monetary",
        unit_policy="base currency units", currency_policy=ACCT_CCY,
        null_input_policy="days with no known snapshot use the latest prior snapshot",
        empty_population_policy="no known snapshots returns null",
        aggregation_over_entity=aggregation.split(";")[-1].strip(),
        aggregation_over_time=aggregation.split(";")[0].strip())


def _days(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="integer", additivity="additive", unit_kind="count",
        null_input_policy="days with no known snapshot use the latest prior snapshot",
        empty_population_policy="no known snapshots returns null")


ACCOUNT_FOUNDATION_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── the balance ladder ──────────────────────────────────────────────────────────────────────
    _snap("end_of_day_balance",
          definition="The latest-known end-of-day balance at the cutoff.",
          context="the balance primitive",
          output=_money("end_of_day_balance", "End-of-day balance"),
          result_class="snapshot"),
    _snap("average_daily_balance",
          definition="Mean of daily end-of-day balances over the window.",
          context="balance level primitive",
          output=OutputSpecV2(
              output_id="average_daily_balance", display_label="Average daily balance",
              output_type="numeric", additivity="non_additive", unit_kind="monetary",
              unit_policy="base currency units", currency_policy=ACCT_CCY,
              null_input_policy="days with no known snapshot use the latest prior snapshot",
              empty_population_policy="no known snapshots returns null",
              zero_denominator_policy="a zero-day window returns null"),
          result_class="ratio"),
    _snap("minimum_daily_balance",
          definition="Lowest daily end-of-day balance in the window.",
          context="floor primitive",
          output=_money("minimum_daily_balance", "Minimum daily balance",
                        additivity="non_additive", aggregation="minimum over time; never "
                        "summed"),
          result_class="extremum"),
    _snap("maximum_daily_balance",
          definition="Highest daily end-of-day balance in the window.",
          context="peak primitive",
          output=_money("maximum_daily_balance", "Maximum daily balance",
                        additivity="non_additive", aggregation="maximum over time; never "
                        "summed"),
          result_class="extremum"),
    _snap("maximum_balance_drawdown",
          definition=("Largest peak-to-trough fall of the daily end-of-day balance inside "
                      "the window."),
          context="drawdown primitive",
          output=_money("maximum_balance_drawdown", "Maximum drawdown",
                        additivity="non_additive", aggregation="worst over time; never "
                        "summed"),
          result_class="extremum"),
    _snap("available_balance",
          definition=("The latest-known AVAILABLE balance (ledger balance net of holds and "
                      "unposted authorizations) at the cutoff — a different fact from the "
                      "end-of-day ledger balance."),
          context="liquidity primitive",
          objective=LIQUIDITY,
          output=_money("available_balance", "Available balance"),
          result_class="snapshot",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    measure("available", "monetary_stock", "account_day_snapshot",
                            economic_role="available_balance"),
                    as_of("account_day_snapshot"))),

    # ── limits and overdraft against the GOVERNED limit record ──────────────────────────────────
    _snap("limit_headroom",
          definition=("Available balance plus undrawn limit as a share of the governed "
                      "account limit, both sides at the same as-of."),
          context="limit headroom",
          objective=LIMIT_MGMT,
          output=OutputSpecV2(
              output_id="limit_headroom", display_label="Limit headroom",
              output_type="numeric", additivity="non_additive", unit_kind="ratio",
              valid_range="[0, 1]",
              null_input_policy="accounts missing either side at the as-of return null",
              empty_population_policy="no limit record returns null — an unlimited account "
                                      "has no headroom to measure",
              zero_denominator_policy="a zero limit returns null"),
          result_class="ratio",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    measure("balance", "monetary_stock", "account_day_snapshot"),
                    policy_input("limit", "limit", "account_day_snapshot",
                                 policy=ACCT_LIMIT),
                    as_of("account_day_snapshot")),
          eligibility=EligibilitySpecV2(
              included="balances against the limit record effective at the as-of",
              excluded="usage against an undated or guessed limit",
              policy_refs=(ACCT_LIMIT, ACCT_CCY))),
    _snap("overdraft_day_count",
          definition=("Days in the window with the end-of-day balance below zero (or below "
                      "the governed overdraft line where one is defined)."),
          context="overdraft persistence — the overdraft-propensity input",
          objective=OVERDRAFT,
          output=_days("overdraft_day_count", "Overdraft days"),
          result_class="count"),
    _snap("maximum_overdraft_depth",
          definition="Deepest end-of-day overdraft reached in the window.",
          context="overdraft severity",
          objective=OVERDRAFT,
          output=_money("maximum_overdraft_depth", "Max overdraft depth",
                        additivity="non_additive", aggregation="worst over time; never "
                        "summed"),
          result_class="extremum"),
    _snap("excess_limit_episode_count",
          definition=("Count of distinct episodes where the balance exceeded the governed "
                      "limit — an episode is a maximal run of consecutive excess days."),
          context="limit discipline",
          objective=LIMIT_MGMT,
          output=_days("excess_limit_episode_count", "Excess-limit episodes"),
          result_class="count",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    measure("balance", "monetary_stock", "account_day_snapshot"),
                    policy_input("limit", "limit", "account_day_snapshot",
                                 policy=ACCT_LIMIT),
                    as_of("account_day_snapshot")),
          eligibility=EligibilitySpecV2(
              included="balances against the limit record effective at each day",
              excluded="excess against an undated limit",
              policy_refs=(ACCT_LIMIT, ACCT_CCY))),
    _snap("excess_limit_episode_max_days",
          definition="The longest excess-limit episode's length in days.",
          context="limit discipline (duration side)",
          objective=LIMIT_MGMT,
          output=OutputSpecV2(
              output_id="excess_limit_episode_max_days",
              display_label="Longest excess episode",
              output_type="integer", additivity="non_additive", unit_kind="duration_days",
              null_input_policy="days with no known snapshot use the latest prior snapshot",
              empty_population_policy="no excess episodes returns zero — a real answer"),
          result_class="extremum",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    measure("balance", "monetary_stock", "account_day_snapshot"),
                    policy_input("limit", "limit", "account_day_snapshot",
                                 policy=ACCT_LIMIT),
                    as_of("account_day_snapshot")),
          eligibility=EligibilitySpecV2(
              included="balances against the limit record effective at each day",
              excluded="excess against an undated limit",
              policy_refs=(ACCT_LIMIT, ACCT_CCY))),

    # ── interest and fees ───────────────────────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="interest_paid_amount", revision=1, family="account_foundation",
        primary_objective=NIM,
        business_definition=("Interest PAID to the account (credit interest) over the "
                             "window — the interest_income economic role from the account's "
                             "side."),
        decision_context="deposit economics",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="interest_paid_amount", display_label="Interest paid",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ACCT_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("account", "account_id", "interest_event"),
                  measure("interest", "interest_income", "interest_event"),
                  event_ts("interest_event")),
        source_grain="interest_event", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="credit interest postings",
            excluded="charged interest (its own recipe, its own role)",
            policy_refs=(ACCT_CCY,)),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:interest_paid_amount",
                                   result_class="sum")),
    RecipeDefinitionV2(
        recipe_id="interest_charged_amount", revision=1, family="account_foundation",
        primary_objective=NIM,
        business_definition=("Interest CHARGED to the account (debit/overdraft interest) "
                             "over the window."),
        decision_context="lending economics",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="interest_charged_amount", display_label="Interest charged",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ACCT_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("account", "account_id", "interest_event"),
                  measure("interest", "interest_expense", "interest_event"),
                  event_ts("interest_event")),
        source_grain="interest_event", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="debit interest postings",
            excluded="paid interest (its own recipe, its own role)",
            policy_refs=(ACCT_CCY,)),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:interest_charged_amount",
                                   result_class="sum")),
    RecipeDefinitionV2(
        recipe_id="fee_burden_ratio", revision=1, family="account_foundation",
        primary_objective=CHURN,
        business_definition=("Fees charged divided by posted debit flow over the window — "
                             "how much of the account's activity the fees eat."),
        decision_context="fee-pain signal",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="fee_burden_ratio", display_label="Fee burden",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="no posted debits returns null",
            zero_denominator_policy="zero debit flow returns null"),
        operands=(entity("account", "account_id", "transaction"),
                  measure("fee", "monetary_flow", "transaction",
                          economic_role="fee_charged"),
                  measure("debit_flow", "monetary_flow", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted fees against posted debit flow",
            excluded="reversed fees",
            policy_refs=("eligible_status:foundation-posted-events",
                         "reversal_correction:foundation-flag-or-code", ACCT_CCY)),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:fee_burden_ratio",
                                   result_class="ratio")),

    # ── dormancy, tenure, mandates, salary designation ──────────────────────────────────────────
    _snap("dormant_day_count",
          definition=("Days in the window the account's governed state was DORMANT."),
          context="dormancy persistence",
          objective=CHURN,
          output=_days("dormant_day_count", "Dormant days"),
          result_class="count",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    status("account_state", "account_status", "account_day_snapshot",
                           policy=DORMANCY),
                    as_of("account_day_snapshot")),
          eligibility=EligibilitySpecV2(
              included="daily states under the governed dormancy definition",
              excluded="dormancy inferred from transaction absence alone",
              policy_refs=(DORMANCY,))),
    _snap("reactivation_flag",
          definition=("Whether the account moved from the governed DORMANT state to ACTIVE "
                      "inside the window — a two-read state comparison."),
          context="reactivation signal",
          objective=CHURN,
          output=OutputSpecV2(
              output_id="reactivation_flag", display_label="Reactivated",
              output_type="boolean", additivity="non_additive", unit_kind="count",
              null_input_policy="a missing state at either end returns null",
              empty_population_policy="no state history returns null"),
          result_class="flag",
          operands=(entity("account", "account_id", "account_day_snapshot"),
                    status("account_state", "account_status", "account_day_snapshot",
                           policy=DORMANCY),
                    as_of("account_day_snapshot")),
          eligibility=EligibilitySpecV2(
              included="states under the governed dormancy definition at both reads",
              excluded="reactivation inferred without the dormant prior state",
              policy_refs=(DORMANCY,))),
    RecipeDefinitionV2(
        recipe_id="account_tenure_days", revision=1, family="account_foundation",
        primary_objective=CHURN,
        business_definition="Days between the ACCOUNT's opening date and the cutoff.",
        decision_context="account age primitive (the customer-grain tenure is its own recipe)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="account_tenure_days", display_label="Account tenure",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="a null opening date returns null",
            empty_population_policy="no account record returns null"),
        operands=(entity("account", "account_id", "account"),
                  event_ts("account", role="opened", concept="origination_date")),
        source_grain="account", output_grain="account",
        temporal=TemporalSpecV2(anchor_kind="as_of", business_effective_role="opened",
                                window_unit="none"),
        readiness="FORMULA_BLOCKED",
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:account_tenure_days",
                                   result_class="recency")),
    RecipeDefinitionV2(
        recipe_id="active_mandate_count", revision=1, family="account_foundation",
        primary_objective=CHURN,
        business_definition=("Count of ACTIVE direct-debit/standing-order mandates on the "
                             "account at the cutoff."),
        decision_context="stickiness primitive",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="active_mandate_count", display_label="Active mandates",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="mandates with unknown state are excluded",
            empty_population_policy="no mandates returns zero"),
        operands=(entity("account", "account_id", "mandate_snapshot"),
                  status("mandate_state", "mandate", "mandate_snapshot"),
                  as_of("mandate_snapshot")),
        source_grain="mandate_snapshot", output_grain="account",
        temporal=snapshot_window("mandate states at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:active_mandate_count",
                                   result_class="count")),
    RecipeDefinitionV2(
        recipe_id="primary_salary_account_flag", revision=1, family="account_foundation",
        primary_objective=CHURN,
        business_definition=("Whether this account carries the GOVERNED primary-salary "
                             "designation at the cutoff — a policy fact, never inferred "
                             "from a single credit."),
        decision_context="primacy primitive",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="primary_salary_account_flag", display_label="Primary salary account",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="accounts with no designation record return null — unknown, "
                              "not false",
            empty_population_policy="no designation policy coverage returns null"),
        operands=(entity("account", "account_id", "account_day_snapshot"),
                  policy_input("designation", "account_status", "account_day_snapshot",
                               policy=SALARY_DESIGNATION),
                  as_of("account_day_snapshot")),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=snapshot_window("the designation effective at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="accounts under the governed designation policy",
            excluded="primacy inferred from one salary-like credit",
            policy_refs=(SALARY_DESIGNATION,)),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="foundation:primary_salary_account_flag",
                                   result_class="flag")),
    RecipeDefinitionV2(
        recipe_id="account_switch_precursor_pattern", revision=1,
        family="account_foundation",
        primary_objective=CHURN,
        business_definition=("The PATTERN of pre-outcome closure/switch precursors (mandate "
                             "cancellations, salary redirect, balance sweep) preceding an "
                             "account closure or switch."),
        decision_context="closure early warning (pre-outcome only)",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "The precursor EVENT SET is not reviewed: which events count as precursors — and "
            "which are the outcome itself leaking backwards — is exactly the leakage boundary "
            "a reviewed definition must draw before any number is computed."),
        output=OutputSpecV2(
            output_id="account_switch_precursor_pattern",
            display_label="Switch precursor pattern",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(entity("account", "account_id", "transaction"),),
        source_grain="transaction", output_grain="account",
        temporal=event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,)),
)
