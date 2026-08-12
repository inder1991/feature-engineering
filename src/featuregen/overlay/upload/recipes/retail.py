"""BR-11 — the Retail/RBWM churn pack: 12 legacy templates → 23 atomic V2 recipes.

Every plan-ordered correction is a STRUCTURAL fact here, not a note: balance_trend splits into a
raw and a normalized slope; the transaction-frequency halves-ratio and count-slope are two
outputs; net flow and inflow/outflow ratio have different contracts; salary_signal splits four
ways and its confidence half is honestly conceptual; RFM's three atoms are deterministic and the
composite awaits a reviewed scoring policy; mandate cancellation and returned collections are two
different events; and the fuzzy external-own-transfer match survives only as a conceptual pattern
carrying its privacy warning, beside a verified-relationship recipe that can actually execute.

Readiness discipline: every deterministic recipe is FORMULA_BLOCKED — its expectation ref names
the review that has not happened, which is exactly what BR-7's fold reports. Nothing here claims
FORMULA_AUTHORABLE by assertion; the expectation registry grants that, one review at a time.

Policy references use the BR-10 governed kinds (``<kind>:<name>``): the retail posted-status and
reversal policies every filtered recipe shares, the sign policy the flow recipes need, the
base-currency policy the corrections demand, and the joint-account allocation policy every
account→customer rollup declares.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OperandSpecV2,
    OutputSpecV2,
    ParameterSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)

CHURN = "customer.relationship_attrition.churn"
DEPOSIT_ATTRITION = "customer.relationship_attrition.deposit_attrition"

#: The pack's shared governed policy references (BR-10 kinds).
ELIGIBLE_POSTED = "eligible_status:retail-posted-events"
REVERSALS = "reversal_correction:retail-flag-or-code"
SIGN = "direction_sign:retail-signed-by-indicator"
BASE_CCY = "currency_conversion:retail-base-currency-eod"
JOINT_ALLOC = "allocation:joint-account-full-attribution"
ACTIVE_HOLDING = "active_state:effective-dated-holdings"

_ELIGIBLE_ACTIVITY = EligibilitySpecV2(
    included="customer-initiated posted transactions in an eligible status",
    excluded="failed, reversed, technical, closure and system-only events",
    policy_refs=(ELIGIBLE_POSTED, REVERSALS))

_WINDOW = ParameterSpecV2(name="window", parameter_class="operational",
                          allowed_values=(30, 90, 180),
                          identity_projection="window={value}d",
                          display_projection="{value}-day window")


def _account(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="account", concept="account_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _event_ts(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="event_ts", concept="event_timestamp",
                         operand_class="event_timestamp", allowed_source_grains=(source,))


def _amount(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                         allowed_source_grains=(source,),
                         unit_expectation="monetary", currency_expectation="per-row currency",
                         sign_direction_expectation="unsigned amount plus direction authority")


def _direction(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="direction", concept="debit_credit_indicator",
                         operand_class="direction", allowed_source_grains=(source,),
                         status_policy_ref=ELIGIBLE_POSTED)


def _status(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="status", concept="booking_status", operand_class="status",
                         allowed_source_grains=(source,), status_policy_ref=ELIGIBLE_POSTED)


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"retail:{output_id}",
                              result_class=result_class)


def _event_window(*, event_role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=event_role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


_AS_OF_SNAPSHOT = TemporalSpecV2(
    anchor_kind="as_of", business_effective_role="as_of_date",
    window_basis="trailing", window_unit="days", window_parameter="window",
    cutoff_inclusivity="inclusive",
    snapshot_policy="latest-known end-of-day snapshot at or before each day's cutoff")


def _snapshot_operands() -> tuple[OperandSpecV2, ...]:
    return (
        _account("account_day_snapshot"),
        OperandSpecV2(role="balance", concept="monetary_stock", operand_class="measure",
                      allowed_source_grains=("account_day_snapshot",),
                      unit_expectation="monetary",
                      currency_expectation="base currency per " + BASE_CCY),
        OperandSpecV2(role="as_of_date", concept="as_of_date", operand_class="as_of_timestamp",
                      allowed_source_grains=("account_day_snapshot",)),
    )


RETAIL_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── balance_trend → raw slope + normalized slope (correction 1) ─────────────────────────────
    RecipeDefinitionV2(
        recipe_id="balance_slope", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "OLS slope of the account's latest-known end-of-day balance over the window, in base "
            "currency units per day — the direction and speed of balance movement."),
        decision_context="early churn warning; balance runoff detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="balance_slope", display_label="Balance slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day",
            currency_policy=BASE_CCY,
            null_input_policy="days with no known snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null, never zero"),
        operands=_snapshot_operands(),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=_AS_OF_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="latest-known end-of-day balance snapshots",
            excluded="intraday and superseded snapshots",
            policy_refs=(BASE_CCY,)),
        formula=_formula("balance_slope", "slope"),
        replaces_legacy_ids=("balance_trend",)),
    RecipeDefinitionV2(
        recipe_id="normalized_balance_slope", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "Balance slope divided by the window's mean balance — the RELATIVE runoff rate, "
            "comparable across accounts of different size."),
        decision_context="cross-account churn ranking",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="normalized_balance_slope", display_label="Normalized balance slope",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="days with no known snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null",
            zero_denominator_policy="a zero mean balance returns null, never infinity"),
        operands=_snapshot_operands(),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=_AS_OF_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="latest-known end-of-day balance snapshots",
            excluded="intraday and superseded snapshots",
            policy_refs=(BASE_CCY,)),
        formula=_formula("normalized_balance_slope", "ratio"),
        replaces_legacy_ids=("balance_trend",)),

    # ── dormancy with declared eligible activity and explicit cutoff (corrections 3-4) ──────────
    RecipeDefinitionV2(
        recipe_id="dormancy_recency_days", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Days between the cutoff and the last ELIGIBLE customer-initiated activity — "
            "failed, reversed, technical, closure and system-only events never count as "
            "activity."),
        decision_context="dormancy and reactivation targeting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="dormancy_recency_days", display_label="Days since eligible activity",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="rows with null event time are excluded per the source policy",
            empty_population_policy="no eligible activity in the window returns null — "
                                    "'never active' is not 'active window days ago'"),
        operands=(_account(), _event_ts(), _status(),
                  OperandSpecV2(role="event_kind", concept="event_type",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",))),
        source_grain="transaction", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_ELIGIBLE_ACTIVITY,
        formula=_formula("dormancy_recency_days", "recency"),
        replaces_legacy_ids=("dormancy_days",)),

    # ── txn_frequency_trend → count slope + halves ratio (corrections 5-6) ──────────────────────
    RecipeDefinitionV2(
        recipe_id="txn_count_slope", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "OLS slope of weekly eligible posted transaction counts over the window — "
            "transaction frequency's direction of travel."),
        decision_context="engagement trend for churn models",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="txn_count_slope", display_label="Transaction count slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="transactions per week per week",
            null_input_policy="weeks with no eligible transactions count zero",
            empty_population_policy="fewer than two weeks returns null"),
        operands=(_account(), _event_ts(), _status()),
        source_grain="transaction", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_ELIGIBLE_ACTIVITY,
        formula=_formula("txn_count_slope", "slope"),
        replaces_legacy_ids=("txn_frequency_trend",)),
    RecipeDefinitionV2(
        recipe_id="txn_frequency_halves_ratio", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Eligible posted transaction count in the window's recent half divided by the count "
            "in its prior half — a simple before/after engagement comparison."),
        decision_context="engagement drop detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="txn_frequency_halves_ratio",
            display_label="Transaction frequency halves ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="weeks with no eligible transactions count zero",
            empty_population_policy="an empty window returns null",
            zero_denominator_policy="a zero prior-half count returns null, never infinity"),
        operands=(_account(), _event_ts(), _status()),
        source_grain="transaction", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_ELIGIBLE_ACTIVITY,
        formula=_formula("txn_frequency_halves_ratio", "ratio"),
        replaces_legacy_ids=("txn_frequency_trend",)),

    # ── inflow/outflow ratio and net flow are DIFFERENT contracts (corrections 7-8) ─────────────
    RecipeDefinitionV2(
        recipe_id="inflow_outflow_ratio", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "Sum of eligible credit amounts divided by the sum of eligible debit amounts over "
            "the window, read through the governed direction authority — never inferred from "
            "amount signs."),
        decision_context="money-in versus money-out balance of trade",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="inflow_outflow_ratio", display_label="Inflow/outflow ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns null",
            zero_denominator_policy="zero outflow returns null, never infinity"),
        operands=(_account(), _amount(), _direction(), _event_ts()),
        source_grain="transaction", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="eligible posted transactions with a governed direction reading",
            excluded="failed, reversed and technical events; rows whose direction cannot "
                     "be read under the sign policy",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN)),
        formula=_formula("inflow_outflow_ratio", "ratio"),
        replaces_legacy_ids=("inflow_outflow_ratio",)),
    RecipeDefinitionV2(
        recipe_id="net_transaction_flow", revision=1, family="retail_churn",
        primary_objective=DEPOSIT_ATTRITION, supporting_objectives=(CHURN,),
        business_definition=(
            "Signed sum of eligible transaction amounts (credits minus debits) over the window, "
            "in base currency — the net money movement, with the sign supplied by the governed "
            "direction policy."),
        decision_context="deposit attrition sizing",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="net_transaction_flow", display_label="Net transaction flow",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units",
            currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across accounts under " + JOINT_ALLOC,
            aggregation_over_time="sum over disjoint windows"),
        operands=(_account(), _amount(), _direction(), _event_ts()),
        source_grain="transaction", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="eligible posted transactions with a governed direction reading",
            excluded="failed, reversed and technical events",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN, BASE_CCY)),
        formula=_formula("net_transaction_flow", "sum"),
        replaces_legacy_ids=("inflow_outflow_ratio",)),

    # ── days below a GOVERNED threshold (threshold = policy, never a bare number) ───────────────
    RecipeDefinitionV2(
        recipe_id="days_below_threshold", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "Count of snapshot days in the window whose latest-known end-of-day balance sits "
            "below the governed balance-floor threshold (jurisdiction- and currency-scoped)."),
        decision_context="low-balance persistence",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="days_below_threshold", display_label="Days below balance floor",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="days with no known snapshot use the latest prior snapshot",
            empty_population_policy="no known snapshots returns null"),
        operands=_snapshot_operands(),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=_AS_OF_SNAPSHOT,
        readiness="FORMULA_BLOCKED",
        parameters=(_WINDOW,
                    ParameterSpecV2(name="balance_floor", parameter_class="governed_policy",
                                    identity_projection="floor={value}",
                                    display_projection="below {value}",
                                    governed_policy_ref="threshold:retail-balance-floor")),
        eligibility=EligibilitySpecV2(
            included="latest-known end-of-day balance snapshots",
            excluded="intraday and superseded snapshots",
            policy_refs=(BASE_CCY, "threshold:retail-balance-floor")),
        formula=_formula("days_below_threshold", "count"),
        replaces_legacy_ids=("days_below_threshold",)),

    # ── salary_signal → four outputs (corrections 9-10) ─────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="salary_credit_count", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Count of eligible posted CREDIT transactions matching the reviewed salary "
            "definition — credit direction, eligible posted state, stable payer identity and "
            "monthly cadence; never category alone."),
        decision_context="salary presence — the primacy anchor",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="salary_credit_count", display_label="Salary credit count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows failing the salary definition are excluded, not nulled",
            empty_population_policy="an empty window returns zero"),
        operands=(_account(), _amount(), _direction(), _event_ts(), _status(),
                  OperandSpecV2(role="payer", concept="initiating_party",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                relationship_requirement="stable payer identity across the "
                                                         "cadence per the salary policy")),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted credits satisfying the reviewed salary definition",
            excluded="reversed salary-like credits; pension and internal-transfer lookalikes",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN, JOINT_ALLOC)),
        formula=_formula("salary_credit_count", "count"),
        replaces_legacy_ids=("salary_signal",)),
    RecipeDefinitionV2(
        recipe_id="salary_credit_amount", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Sum of eligible salary credit amounts over the window, in base currency, under the "
            "same reviewed salary definition as salary_credit_count."),
        decision_context="income sizing for retention",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="salary_credit_amount", display_label="Salary credit amount",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across a customer's accounts under " + JOINT_ALLOC,
            aggregation_over_time="sum over disjoint windows"),
        operands=(_account(), _amount(), _direction(), _event_ts(), _status(),
                  OperandSpecV2(role="payer", concept="initiating_party",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                relationship_requirement="stable payer identity across the "
                                                         "cadence per the salary policy")),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted credits satisfying the reviewed salary definition",
            excluded="reversed salary-like credits; pension and internal-transfer lookalikes",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN, BASE_CCY, JOINT_ALLOC)),
        formula=_formula("salary_credit_amount", "sum"),
        replaces_legacy_ids=("salary_signal",)),
    RecipeDefinitionV2(
        recipe_id="salary_regularity", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Share of cadence periods in the window containing at least one eligible salary "
            "credit — cadence kept as a fraction of cadence expected."),
        decision_context="salary stability",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="salary_regularity", display_label="Salary regularity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="periods with no eligible credit count as missed, not null",
            empty_population_policy="a window shorter than one cadence period returns null",
            zero_denominator_policy="zero expected periods returns null"),
        operands=(_account(), _direction(), _event_ts(), _status(),
                  OperandSpecV2(role="payer", concept="initiating_party",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                relationship_requirement="stable payer identity across the "
                                                         "cadence per the salary policy")),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted credits satisfying the reviewed salary definition",
            excluded="reversed salary-like credits; pension and internal-transfer lookalikes",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN, JOINT_ALLOC)),
        formula=_formula("salary_regularity", "share"),
        replaces_legacy_ids=("salary_signal",)),
    RecipeDefinitionV2(
        recipe_id="salary_confidence", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "How confidently the credit stream identified as salary IS salary — a weighing of "
            "payer stability, cadence, amount stability and narrative evidence."),
        decision_context="qualifying the other three salary outputs",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "No reviewed deterministic scoring policy exists for combining the evidence; until "
            "one is approved (or a ModelFeatureSpec is registered), any confidence number would "
            "be an unreviewed heuristic presented as a fact."),
        output=OutputSpecV2(
            output_id="salary_confidence", display_label="Salary confidence",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_account(),),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="CONCEPTUAL_ONLY",
        parameters=(_WINDOW,),
        replaces_legacy_ids=("salary_signal",)),

    # ── product breadth from effective-dated ACTIVE holdings (correction 11) ────────────────────
    RecipeDefinitionV2(
        recipe_id="product_breadth_active", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=("customer.cross_sell.next_best_action",),
        business_definition=(
            "Distinct count of the customer's ACTIVE product holdings as of the cutoff, read "
            "from effective-dated holding intervals — never from a product-type column alone."),
        decision_context="relationship depth",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="product_breadth_active", display_label="Active product breadth",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="holdings with unknown validity are excluded per policy",
            empty_population_policy="no active holdings returns zero — a real answer here"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("product_holding_interval",)),
            OperandSpecV2(role="holding", concept="product_holding", operand_class="dimension",
                          allowed_source_grains=("product_holding_interval",)),
            OperandSpecV2(role="valid_interval", concept="valid_time",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("product_holding_interval",)),
        ),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="holdings whose effective interval covers the cutoff and whose state is "
                     "active under the governed active-state policy",
            excluded="closed, lapsed and pipeline holdings",
            policy_refs=(ACTIVE_HOLDING,)),
        formula=_formula("product_breadth_active", "distinct_count"),
        replaces_legacy_ids=("product_breadth",)),

    # ── tenure (kept atomic) ────────────────────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="tenure_days", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition="Days between the customer's origination date and the cutoff.",
        decision_context="lifecycle stage context",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="tenure_days", display_label="Tenure days",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="a null origination date returns null",
            empty_population_policy="no origination record returns null"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("customer",)),
            OperandSpecV2(role="origination", concept="origination_date",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("customer",)),
        ),
        source_grain="customer", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", business_effective_role="origination",
                                window_unit="none"),
        readiness="FORMULA_BLOCKED",
        formula=_formula("tenure_days", "recency"),
        replaces_legacy_ids=("tenure_days",)),

    # ── balance volatility (dispersion result class) ────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="balance_volatility", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "Standard deviation of the latest-known end-of-day balance over the window, in base "
            "currency — how unsettled the balance is."),
        decision_context="behaviour change detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="balance_volatility", display_label="Balance volatility",
            output_type="numeric", additivity="non_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="days with no known snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null"),
        operands=_snapshot_operands(),
        source_grain="account_day_snapshot", output_grain="account",
        temporal=_AS_OF_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="latest-known end-of-day balance snapshots",
            excluded="intraday and superseded snapshots",
            policy_refs=(BASE_CCY,)),
        formula=_formula("balance_volatility", "dispersion"),
        replaces_legacy_ids=("balance_volatility",)),

    # ── RFM → three deterministic atoms + an honestly-conceptual composite (correction 14) ──────
    RecipeDefinitionV2(
        recipe_id="rfm_recency_days", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition="Days since the last eligible posted transaction at the cutoff.",
        decision_context="RFM recency atom",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rfm_recency_days", display_label="Recency (days)",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="rows with null event time are excluded per the source policy",
            empty_population_policy="no eligible transactions in the window returns null"),
        operands=(_account(), _event_ts(), _status()),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_ELIGIBLE_ACTIVITY,
        formula=_formula("rfm_recency_days", "recency"),
        replaces_legacy_ids=("rfm_composite",)),
    RecipeDefinitionV2(
        recipe_id="rfm_frequency_count", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition="Count of eligible posted transactions over the window.",
        decision_context="RFM frequency atom",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rfm_frequency_count", display_label="Frequency (count)",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="ineligible rows are excluded, not nulled",
            empty_population_policy="an empty window returns zero"),
        operands=(_account(), _event_ts(), _status()),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_ELIGIBLE_ACTIVITY,
        formula=_formula("rfm_frequency_count", "count"),
        replaces_legacy_ids=("rfm_composite",)),
    RecipeDefinitionV2(
        recipe_id="rfm_monetary_amount", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Sum of eligible posted transaction amounts over the window, in base currency."),
        decision_context="RFM monetary atom",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rfm_monetary_amount", display_label="Monetary (amount)",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across a customer's accounts under " + JOINT_ALLOC,
            aggregation_over_time="sum over disjoint windows"),
        operands=(_account(), _amount(), _event_ts(), _status()),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="customer-initiated posted transactions in an eligible status",
            excluded="failed, reversed, technical, closure and system-only events",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, BASE_CCY, JOINT_ALLOC)),
        formula=_formula("rfm_monetary_amount", "sum"),
        replaces_legacy_ids=("rfm_composite",)),
    RecipeDefinitionV2(
        recipe_id="rfm_composite_score", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "A single engagement score combining recency, frequency and monetary value."),
        decision_context="ranking convenience over the three RFM atoms",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "Combining the three atoms into one number requires either a reviewed deterministic "
            "scoring policy or a registered ModelFeatureSpec — neither exists yet, and an "
            "unreviewed weighting is not a computation."),
        output=OutputSpecV2(
            output_id="rfm_composite_score", display_label="RFM composite score",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_account(),),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,),
        replaces_legacy_ids=("rfm_composite",)),

    # ── DD mandate cancellation vs returned collections — two EVENTS (correction 15) ────────────
    RecipeDefinitionV2(
        recipe_id="dd_mandate_cancellation_count", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Count of direct-debit MANDATE cancellations over the window — the customer "
            "withdrawing an instruction, read from mandate state changes."),
        decision_context="switching intent",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="dd_mandate_cancellation_count",
            display_label="DD mandate cancellations",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="mandate events with unknown state are excluded per policy",
            empty_population_policy="an empty window returns zero"),
        operands=(
            _account("mandate_event"),
            OperandSpecV2(role="mandate_state", concept="mandate", operand_class="status",
                          allowed_source_grains=("mandate_event",)),
            _event_ts("mandate_event"),
        ),
        source_grain="mandate_event", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="mandate state changes to cancelled",
            excluded="failed or returned COLLECTIONS — a bounced payment is not a cancelled "
                     "mandate (dd_collection_return_count counts those)"),
        formula=_formula("dd_mandate_cancellation_count", "count"),
        replaces_legacy_ids=("dd_cancellation_rate",)),
    RecipeDefinitionV2(
        recipe_id="dd_collection_return_count", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "Count of direct-debit collection RETURNS over the window — the scheme bouncing an "
            "attempted collection (insufficient funds, revoked authority), read from payment "
            "return events."),
        decision_context="payment stress and pre-churn friction",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="dd_collection_return_count", display_label="DD collection returns",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="returns with unknown reason still count; unknown status rows "
                              "are excluded per policy",
            empty_population_policy="an empty window returns zero"),
        operands=(
            _account("payment_return_event"),
            OperandSpecV2(role="return_status", concept="payment_return_status",
                          operand_class="status",
                          allowed_source_grains=("payment_return_event",)),
            _event_ts("payment_return_event"),
        ),
        source_grain="payment_return_event", output_grain="account",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="direct-debit collection return events",
            excluded="mandate cancellations (a withdrawn instruction is not a bounced payment)"),
        formula=_formula("dd_collection_return_count", "count"),
        replaces_legacy_ids=("dd_cancellation_rate",)),

    # ── own-transfer: VERIFIED relationship executes; fuzzy match stays conceptual (corr. 13) ───
    RecipeDefinitionV2(
        recipe_id="own_transfer_outflow_amount", revision=1, family="retail_churn",
        primary_objective=CHURN, supporting_objectives=(DEPOSIT_ATTRITION,),
        business_definition=(
            "Sum of eligible posted debit amounts to beneficiaries VERIFIED as the customer's "
            "own external accounts — money moved to self at a competitor, over a confirmed "
            "own-account relationship, never a name-similarity guess."),
        decision_context="primacy loss — own money leaving",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="own_transfer_outflow_amount",
            display_label="Verified own-transfer outflow",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero"),
        operands=(
            _account(), _amount(), _direction(), _event_ts(),
            OperandSpecV2(role="payee", concept="beneficiary_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",),
                          relationship_requirement="a VERIFIED own-account relationship between "
                                                   "the payee record and the customer"),
        ),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted debits to verified own external accounts",
            excluded="transfers to unverified payees — they are ordinary outflow, not own-money "
                     "movement",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, SIGN, BASE_CCY, JOINT_ALLOC)),
        formula=_formula("own_transfer_outflow_amount", "sum"),
        replaces_legacy_ids=("external_own_transfer_trend",)),
    RecipeDefinitionV2(
        recipe_id="external_own_transfer_pattern", revision=1, family="retail_churn",
        primary_objective=CHURN,
        business_definition=(
            "The PATTERN of transfers that LOOK like own-account movement (matching beneficiary "
            "names, round amounts, regular cadence) where no verified relationship exists."),
        decision_context="own-transfer hypothesis generation only",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "Fuzzy name matching over beneficiary_name is PII processing with a real false-match "
            "rate: two different people share names, and a wrong match moves a customer's money "
            "story onto a stranger. Without a verified own-account relationship this stays a "
            "pattern to investigate, never a number to compute."),
        output=OutputSpecV2(
            output_id="external_own_transfer_pattern",
            display_label="External own-transfer pattern",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_account(),),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,),
        leakage=LeakageSpecV2(classification="standard"),
        replaces_legacy_ids=("external_own_transfer_trend",)),
)
