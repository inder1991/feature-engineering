"""BR-18 — the transaction foundation pack, tranche B: 19 analytical primitives.

Every operation these recipes need already exists in the Formula-v2 grammar (BR-6's 21
operations: median/percentile with the identity-bearing argument, slope, stddev, hhi and
top_share with optional weighting, recency, streaks) — so each recipe is authorable the day its
expectation is reviewed, and every one is honestly FORMULA_BLOCKED until then. Percentile and
category choices are SEMANTIC parameters (p95 and p99 are different meanings, a subscription's
regularity and a rent's regularity are different features) — identity-bearing by the BR-3
variant machinery, never smuggled into labels.

Deliberately NOT re-authored (already exist as atoms elsewhere — one id, one recipe):
salary_credit_regularity (retail's salary_regularity), round_amount_share (AML's
round_amount_ratio), the fraud pack's burst detectors (different windows, different purpose).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OutputSpecV2,
    ParameterSpecV2,
)
from featuregen.overlay.upload.recipes._shared import dim
from featuregen.overlay.upload.recipes.transaction_foundation import (
    TXN_CCY,
    TXN_ELIGIBLE,
    _base_operands,
    _count_output,
    _recipe,
)

PAY_BEHAVIOUR = "payments.behaviour"
ENGAGEMENT = "customer.engagement"
STM = "aml_cft.suspicious_transaction_monitoring"

FX_RATE_AUTHORITY = "currency_conversion:foundation-fx-rate-authority"

_PERCENTILE = ParameterSpecV2(name="percentile", parameter_class="semantic",
                              allowed_values=(90, 95, 99),
                              identity_projection="p={value}",
                              display_projection="p{value}")

_PAYMENT_CATEGORY = ParameterSpecV2(name="payment_category", parameter_class="semantic",
                                    allowed_values=("subscription", "bill", "rent",
                                                    "loan_payment"),
                                    identity_projection="category={value}",
                                    display_projection="{value} payments")


def _score_output(output_id: str, label: str, *, unit: str = "score") -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="numeric", additivity="non_additive", unit_kind=unit,
        currency_policy=TXN_CCY if unit == "monetary" else "",
        null_input_policy="ineligible rows are excluded per the source policy",
        empty_population_policy="too few eligible observations returns null")


def _share_output(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="numeric", additivity="non_additive", unit_kind="ratio",
        valid_range="[0, 1]",
        null_input_policy="ineligible rows are excluded per the source policy",
        empty_population_policy="no eligible activity returns null",
        zero_denominator_policy="a zero denominator returns null")


TRANSACTION_ANALYTICS_RECIPES: tuple = (
    _recipe("transaction_amount_median",
            definition="Median eligible posted amount over the window, in base currency.",
            context="robust ticket size",
            output=_score_output("transaction_amount_median", "Median amount",
                                 unit="monetary"),
            result_class="dispersion",
            operands=_base_operands(with_amount=True)),
    _recipe("transaction_amount_percentile",
            definition=("The chosen upper percentile of eligible posted amounts — p95 and "
                        "p99 are two different features, by identity."),
            context="tail ticket size",
            output=_score_output("transaction_amount_percentile", "Amount percentile",
                                 unit="monetary"),
            result_class="dispersion",
            operands=_base_operands(with_amount=True)),
    _recipe("inter_transaction_gap_average",
            definition="Average days between consecutive eligible posted transactions.",
            context="cadence primitive",
            objective=ENGAGEMENT,
            output=OutputSpecV2(
                output_id="inter_transaction_gap_average", display_label="Average gap",
                output_type="numeric", additivity="non_additive",
                unit_kind="duration_days",
                null_input_policy="ineligible rows are excluded per the source policy",
                empty_population_policy="fewer than two transactions returns null"),
            result_class="ratio",
            operands=_base_operands()),
    _recipe("inter_transaction_gap_percentile",
            definition="The chosen upper percentile of inter-transaction gaps in days.",
            context="cadence tail",
            objective=ENGAGEMENT,
            output=OutputSpecV2(
                output_id="inter_transaction_gap_percentile",
                display_label="Gap percentile",
                output_type="numeric", additivity="non_additive",
                unit_kind="duration_days",
                null_input_policy="ineligible rows are excluded per the source policy",
                empty_population_policy="fewer than two transactions returns null"),
            result_class="dispersion",
            operands=_base_operands()),
    _recipe("transaction_velocity",
            definition="Eligible posted transactions per day over the window.",
            context="activity rate",
            output=OutputSpecV2(
                output_id="transaction_velocity", display_label="Transactions per day",
                output_type="numeric", additivity="non_additive", unit_kind="rate",
                unit_policy="transactions per day",
                null_input_policy="ineligible rows are excluded per the source policy",
                empty_population_policy="an empty window returns zero per day",
                zero_denominator_policy="a zero-day window returns null"),
            result_class="ratio",
            operands=_base_operands()),
    _recipe("transaction_burstiness",
            definition=("Dispersion of daily transaction counts relative to their mean — "
                        "how bursty the activity is, not how much."),
            context="burstiness primitive",
            output=_score_output("transaction_burstiness", "Burstiness"),
            result_class="dispersion",
            operands=_base_operands()),
    _recipe("transaction_count_trend",
            definition="OLS slope of daily eligible posted counts over the window.",
            context="volume trend",
            output=OutputSpecV2(
                output_id="transaction_count_trend", display_label="Count trend",
                output_type="numeric", additivity="non_additive", unit_kind="rate",
                unit_policy="transactions per day per day",
                null_input_policy="days with no eligible transactions count zero",
                empty_population_policy="fewer than two days returns null"),
            result_class="slope",
            operands=_base_operands()),
    _recipe("transaction_amount_trend",
            definition="OLS slope of daily eligible posted amounts over the window.",
            context="value trend",
            output=OutputSpecV2(
                output_id="transaction_amount_trend", display_label="Amount trend",
                output_type="numeric", additivity="non_additive", unit_kind="rate",
                unit_policy="base currency units per day", currency_policy=TXN_CCY,
                null_input_policy="null amounts are excluded per the source policy",
                empty_population_policy="fewer than two days returns null"),
            result_class="slope",
            operands=_base_operands(with_amount=True)),
    _recipe("transaction_amount_volatility",
            definition="Standard deviation of eligible posted amounts over the window.",
            context="value dispersion",
            output=_score_output("transaction_amount_volatility", "Amount volatility",
                                 unit="monetary"),
            result_class="dispersion",
            operands=_base_operands(with_amount=True)),
    _recipe("day_of_week_concentration",
            definition=("Concentration (HHI) of eligible posted activity across weekdays — "
                        "1/7th each is flat; 1.0 is one-day-a-week behaviour."),
            context="weekly seasonality",
            objective=ENGAGEMENT,
            output=_share_output("day_of_week_concentration", "Weekday concentration"),
            result_class="share",
            operands=_base_operands()),
    _recipe("monthly_seasonality_concentration",
            definition="Concentration (HHI) of eligible posted activity across months.",
            context="monthly seasonality",
            objective=ENGAGEMENT,
            output=_share_output("monthly_seasonality_concentration",
                                 "Monthly concentration"),
            result_class="share",
            operands=_base_operands()),
    _recipe("counterparty_concentration_hhi",
            definition=("Concentration (HHI) of eligible posted value across "
                        "counterparties."),
            context="counterparty concentration",
            output=_share_output("counterparty_concentration_hhi",
                                 "Counterparty concentration"),
            result_class="share",
            operands=_base_operands(with_amount=True, extra=(
                dim("counterparty", "customer_id", "transaction"),))),
    _recipe("top_counterparty_share",
            definition="The largest counterparty's share of eligible posted value.",
            context="dominant-counterparty share",
            output=_share_output("top_counterparty_share", "Top counterparty share"),
            result_class="share",
            operands=_base_operands(with_amount=True, extra=(
                dim("counterparty", "customer_id", "transaction"),))),
    _recipe("fan_in_counterparty_count",
            definition=("Distinct counterparties PAYING INTO the account over the window — "
                        "the inbound leg alone, atomic (the AML fan-in×fan-out shape reads "
                        "both legs; this is its inbound primitive)."),
            context="inbound network breadth",
            objective=STM,
            output=_count_output("fan_in_counterparty_count", "Fan-in counterparties"),
            result_class="distinct_count",
            operands=_base_operands(with_direction=True, extra=(
                dim("payer", "customer_id", "transaction"),))),
    _recipe("fan_out_counterparty_count",
            definition="Distinct counterparties PAID from the account over the window.",
            context="outbound network breadth",
            objective=STM,
            output=_count_output("fan_out_counterparty_count", "Fan-out counterparties"),
            result_class="distinct_count",
            operands=_base_operands(with_direction=True, extra=(
                dim("payee", "customer_id", "transaction"),))),
    _recipe("new_counterparty_flag",
            definition=("Whether the window contains a payment to a counterparty never seen "
                        "in the account's prior history."),
            context="novelty primitive",
            output=OutputSpecV2(
                output_id="new_counterparty_flag", display_label="New counterparty",
                output_type="boolean", additivity="non_additive", unit_kind="count",
                null_input_policy="rows with no counterparty identity are excluded",
                empty_population_policy="no prior history returns null — unknown, not new"),
            result_class="flag",
            operands=_base_operands(extra=(
                dim("counterparty", "customer_id", "transaction"),))),
    _recipe("categorized_payment_regularity",
            definition=("Share of cadence periods containing the chosen payment category "
                        "(subscription / bill / rent / loan payment) — each category is its "
                        "own feature, by identity."),
            context="recurring-payment regularity",
            objective=ENGAGEMENT,
            output=_share_output("categorized_payment_regularity", "Payment regularity"),
            result_class="share",
            operands=_base_operands(extra=(
                dim("txn_type", "transaction_type", "transaction"),))),
    _recipe("fx_transaction_share",
            definition=("Foreign-currency transactions as a share of eligible posted "
                        "activity."),
            context="FX usage",
            output=_share_output("fx_transaction_share", "FX share"),
            result_class="share",
            operands=_base_operands(with_amount=True, extra=(
                dim("currency", "currency_code", "transaction"),))),
    _recipe("fx_conversion_spread",
            definition=("Average spread of applied FX rates against the governed reference "
                        "rate at booking time — computable only where the rate authority "
                        "exists; no authority, no spread."),
            context="FX pricing",
            output=_score_output("fx_conversion_spread", "FX spread", unit="rate"),
            result_class="dispersion",
            operands=_base_operands(with_amount=True, extra=(
                dim("currency", "currency_code", "transaction"),
                dim("applied_rate", "fx_conversion_rate", "transaction"))),
            eligibility=EligibilitySpecV2(
                included="FX transactions with the applied rate and the governed reference "
                         "rate at booking time",
                excluded="spreads guessed without the rate authority",
                policy_refs=(TXN_ELIGIBLE, FX_RATE_AUTHORITY))),
)


def _attach_variant_parameters() -> tuple:
    """The two semantic-parameter carriers get their identity-bearing parameter appended —
    done here (dataclasses are frozen) so the tuple above stays declaration-only."""
    from dataclasses import replace

    out = []
    for recipe in TRANSACTION_ANALYTICS_RECIPES:
        if recipe.recipe_id in ("transaction_amount_percentile",
                                "inter_transaction_gap_percentile"):
            out.append(replace(recipe, parameters=(*recipe.parameters, _PERCENTILE)))
        elif recipe.recipe_id == "categorized_payment_regularity":
            out.append(replace(recipe, parameters=(*recipe.parameters, _PAYMENT_CATEGORY)))
        else:
            out.append(recipe)
    return tuple(out)


TRANSACTION_ANALYTICS_RECIPES = _attach_variant_parameters()
