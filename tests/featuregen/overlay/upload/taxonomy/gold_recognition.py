"""Gold evaluation set for the use-case recognizer (Phase-1A, Task 4).

Each :class:`GoldCase` is a banking-analyst hypothesis paired with the use-case objective(s) a
domain expert would recognise it as, plus the concrete recipe ids the expert would expect the
recogniser's *scope* to retain. That recipe list is the **false-narrowing ground truth**: the
applicability evaluator (Task 3) maps a recognised scope to in-scope recipe ids, and a gold recipe
that the recognised scope drops is a false narrowing. Recognition accuracy and applicability recall
are measured against this set (Task 5); applicability recall is the Phase-1B gate.

Coverage authored in: every major family at least once (customer, credit, financial_crime
fraud/aml, treasury_alm, markets, payments, securities_services, insurance, asset_management,
islamic, esg, corporate_trade); ``synonym`` (wording that is not the leaf name), ``ambiguous``
(genuinely spans two objectives), ``unscoped`` (exploratory, no prediction target), ``regulated``
(fair-lending / AML-scoped framing) and ``multi_use_case`` (a legitimate primary + secondaries).

``expected_primary`` is a real selectable leaf in ``USE_CASE_REGISTRY`` (``None`` for ``unscoped``);
``expected_relevant_recipes`` are real recipe ids in ``ALL_TEMPLATES`` — each is a recipe whose
governed applicability *primary or secondary* is one of the case's objectives (or a same-family
sibling), so it is a defensible member of the case's in-scope set.

.. warning::

   **AUTHORED, PENDING EXPERT REVIEW.** This is a first-draft gold set authored by an implementer
   acting as a banking SME. It has **not** yet been validated by an independent human expert. Treat
   the labels as provisional until that review lands; the recognizer's measured accuracy against this
   set is only as trustworthy as the labels themselves.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoldCase:
    """One labelled hypothesis. ``category`` is one of ``straightforward | synonym | ambiguous |
    unscoped | regulated | multi_use_case``."""

    id: str
    hypothesis: str
    prediction_goal: str | None
    expected_primary: str | None
    permitted_secondary: tuple[str, ...]
    expected_relevant_recipes: tuple[str, ...]
    category: str


GOLD: tuple[GoldCase, ...] = (
    # ── straightforward: the objective is close to the leaf's own framing ──────────────────────────
    GoldCase(
        id="G01",
        hypothesis=(
            "Retail customers who are winding down their day-to-day activity are likely to close "
            "their primary current account with us within the next quarter."
        ),
        prediction_goal="Probability a customer closes their primary current account in the next 90 days.",
        expected_primary="customer.relationship_attrition.churn",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "balance_trend", "dormancy_days", "txn_frequency_trend",
            "inflow_outflow_ratio", "tenure_days", "rfm_composite",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G02",
        hypothesis=(
            "Among our delinquent unsecured-loan accounts, some are far more likely than others to "
            "respond to a collections treatment and repay what they owe."
        ),
        prediction_goal="Rank delinquent accounts by expected recovery for next month's collections queue.",
        expected_primary="credit.collections.recoveries",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "promise_to_pay_adherence", "payment_plan_adherence", "cure_reage_dynamics",
            "recovery_rate", "write_off_severity", "days_in_collection",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G03",
        hypothesis=(
            "Some card and account transactions arriving at authorisation are fraudulent and should "
            "be scored for a real-time block or step-up."
        ),
        prediction_goal="Probability an individual transaction is fraudulent, scored at authorisation.",
        expected_primary="fraud.transaction_fraud_detection",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "card_testing_velocity", "geo_velocity_impossible", "txn_velocity_spike",
            "amount_zscore_spike", "first_time_payee_high_value", "merchant_risk_anomaly",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G04",
        hypothesis=(
            "For the ALM desk, the behavioural stickiness of our non-maturing deposit base will vary "
            "by segment over the next twelve months."
        ),
        prediction_goal="Forecast behavioural stability (stickiness) of the non-maturing-deposit base by segment.",
        expected_primary="treasury_alm.deposit_stability",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "nmd_stickiness", "deposit_beta", "hot_money_share",
            "maturity_ladder_runoff", "early_withdrawal_break",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G05",
        hypothesis=(
            "The trading desk's book carries portfolio market risk that we need to forecast over the "
            "coming risk horizon."
        ),
        prediction_goal="Forecast portfolio-level market risk (VaR/ES) for the trading book.",
        expected_primary="markets.market_risk.portfolio",
        permitted_secondary=(),
        expected_relevant_recipes=("position_var_risk", "greek_sensitivity_exposure"),
        category="straightforward",
    ),
    GoldCase(
        id="G06",
        hypothesis=(
            "Payment processing quality varies across our rails — some flows show elevated declines, "
            "returns and chargebacks that we should predict and manage."
        ),
        prediction_goal="Predict payment-operations exception rates (declines, returns, chargebacks) by rail.",
        expected_primary="payments.operations",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "rail_volume_value", "authorisation_decline_rate", "chargeback_dispute_rate",
            "return_payment_rate", "settlement_lag",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G07",
        hypothesis=(
            "In the custody book, certain trades are at elevated risk of failing to settle on the "
            "intended settlement date."
        ),
        prediction_goal="Probability a trade fails to settle over the coming settlement cycles.",
        expected_primary="securities_services.custody.settlement_failure_risk",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "matching_break_rate", "pre_settlement_aging", "settlement_fail_rate", "fail_ageing_buckets",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G08",
        hypothesis=(
            "Net flows into our flagship fund share classes (subscriptions less redemptions) will "
            "swing over the next quarter."
        ),
        prediction_goal="Forecast net fund flows by share class for the next quarter.",
        expected_primary="asset_management.redemption.fund_flows",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "net_fund_flow_trend", "share_class_flow_mix", "expense_ratio_competitiveness",
        ),
        category="straightforward",
    ),
    GoldCase(
        id="G09",
        hypothesis=(
            "Obligors on our trade-finance letter-of-credit and guarantee book show varying stress "
            "that affects rollover and utilisation risk."
        ),
        prediction_goal="Assess trade-finance facility risk (rollover / utilisation / covenant headroom).",
        expected_primary="corporate_trade.trade_finance",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "facility_utilisation_headroom", "lc_guarantee_rollover", "invoice_finance_dynamics",
            "covenant_headroom_breach", "cross_product_stress_count",
        ),
        category="straightforward",
    ),

    # ── synonym: banking wording that is NOT the leaf name ──────────────────────────────────────────
    GoldCase(
        id="G10",
        hypothesis=(
            "A segment of our customers are quietly getting ready to leave us for a competitor bank "
            "for their everyday banking."
        ),
        prediction_goal="Probability a customer defects to a competitor for their primary banking relationship.",
        expected_primary="customer.relationship_attrition.churn",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "balance_trend", "dormancy_days", "txn_frequency_trend",
            "inflow_outflow_ratio", "external_own_transfer_trend", "rfm_composite",
        ),
        category="synonym",
    ),
    GoldCase(
        id="G11",
        hypothesis=(
            "Some performing borrowers' repayment capacity is quietly weakening well before they miss "
            "a payment, and we want to catch that deterioration early."
        ),
        prediction_goal="Flag performing exposures with a significant increase in credit risk before default.",
        expected_primary="credit.early_warning",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "credit_utilisation", "days_past_due_max", "payment_ratio",
            "min_payment_only_streak", "stage_migration", "sicr_onset",
        ),
        category="synonym",
    ),
    GoldCase(
        id="G12",
        hypothesis=(
            "Our corporate borrowers differ in how exposed they are to the low-carbon transition and "
            "in the credibility of their decarbonisation trajectory."
        ),
        prediction_goal="Score each corporate borrower's transition / ESG risk profile.",
        expected_primary="esg.scoring",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "emissions_trend_by_scope", "carbon_intensity_trajectory", "financed_emissions_attribution",
            "transition_alignment_gap", "taxonomy_alignment_share", "scope3_value_chain_exposure",
        ),
        category="synonym",
    ),
    GoldCase(
        id="G13",
        hypothesis=(
            "Our Sharia-compliant financing customers behave differently on profit-rate sensitivity "
            "and instalment payments, and we want to model that behaviour."
        ),
        prediction_goal="Model Islamic-financing customer behaviour (profit-rate, instalment, purification).",
        expected_primary="islamic.sharia_compliance",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "profit_rate_exposure", "profit_sharing_split_behaviour", "murabaha_installment_behaviour",
            "takaful_contribution_behaviour", "islamic_deposit_beta", "purification_ratio",
        ),
        category="synonym",
    ),
    GoldCase(
        id="G14",
        hypothesis=(
            "Some protection-policy customers are drifting off cover — paying premiums late or "
            "irregularly — and are unlikely to keep the policy in force."
        ),
        prediction_goal="Probability a policyholder fails to persist (lapses) over the cover period.",
        expected_primary="insurance.lapse.persistency",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "premium_payment_irregularity", "missed_premium_streak", "surrender_value_trajectory",
        ),
        category="synonym",
    ),

    # ── ambiguous: genuinely spans two objectives ──────────────────────────────────────────────────
    GoldCase(
        id="G15",
        hypothesis=(
            "We are seeing unusual payment patterns on a set of accounts — rapid bursts of transfers "
            "kept just under reporting thresholds — and we want to model the risk."
        ),
        prediction_goal="Score accounts showing suspicious just-under-threshold burst activity.",
        # Spans AML structuring/monitoring and real-time fraud velocity; expert would keep both.
        expected_primary="aml_cft.suspicious_transaction_monitoring",
        permitted_secondary=("fraud.transaction_fraud_detection",),
        expected_relevant_recipes=(
            "amount_just_under_limit", "structuring_smurfing", "txn_velocity_spike",
            "cross_border_burst", "cash_intensity_ratio", "fan_in_fan_out",
        ),
        category="ambiguous",
    ),
    GoldCase(
        id="G16",
        hypothesis=(
            "A group of savings customers are steadily shifting balances out to external banks and we "
            "want to get ahead of the deposit outflow."
        ),
        prediction_goal="Predict individual deposit outflow / draining of balances to external banks.",
        # Ambiguous between an individual-customer attrition objective and the ALM deposit-stability view.
        expected_primary="customer.relationship_attrition.deposit_attrition",
        permitted_secondary=("treasury_alm.deposit_stability",),
        expected_relevant_recipes=(
            "balance_trend", "external_own_transfer_trend", "nmd_stickiness",
            "deposit_beta", "hot_money_share",
        ),
        category="ambiguous",
    ),
    GoldCase(
        id="G17",
        hypothesis=(
            "A cluster of mass-affluent customers are reducing their engagement with us — it is "
            "unclear whether they are on their way out or simply under-served and ready for more."
        ),
        prediction_goal="Explain reduced engagement as attrition risk vs cross-sell opportunity.",
        # Genuinely spans churn (they are leaving) and next-best-action (they need the next product).
        expected_primary="customer.relationship_attrition.churn",
        permitted_secondary=("customer.cross_sell.next_best_action",),
        expected_relevant_recipes=(
            "dormancy_days", "txn_frequency_trend", "balance_trend",
            "next_best_product_propensity", "product_gap_whitespace", "tenure_upsell_readiness",
        ),
        category="ambiguous",
    ),

    # ── unscoped: exploratory, no prediction target ────────────────────────────────────────────────
    GoldCase(
        id="G18",
        hypothesis=(
            "We would like to explore our retail current-account transaction data and see what "
            "interesting patterns are in there — nothing specific in mind yet."
        ),
        prediction_goal=None,
        expected_primary=None,
        permitted_secondary=(),
        expected_relevant_recipes=(),
        category="unscoped",
    ),
    GoldCase(
        id="G19",
        hypothesis=(
            "Can you profile our SME lending portfolio and surface anything unusual? We haven't "
            "settled on a prediction target."
        ),
        prediction_goal=None,
        expected_primary=None,
        permitted_secondary=(),
        expected_relevant_recipes=(),
        category="unscoped",
    ),
    GoldCase(
        id="G20",
        hypothesis=(
            "Give us a general view of customer behaviour across products so leadership can decide "
            "what is worth modelling next."
        ),
        prediction_goal=None,
        expected_primary=None,
        permitted_secondary=(),
        expected_relevant_recipes=(),
        category="unscoped",
    ),

    # ── regulated: fair-lending / AML-scoped framing ───────────────────────────────────────────────
    GoldCase(
        id="G21",
        hypothesis=(
            "Under our AML transaction-monitoring obligations we must detect money-laundering "
            "typologies — structuring, layering and rapid pass-through of funds — across the flow."
        ),
        prediction_goal="Flag transaction flow exhibiting money-laundering typologies for SAR triage.",
        expected_primary="aml_cft.suspicious_transaction_monitoring",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "structuring_smurfing", "cash_intensity_ratio", "rapid_movement_passthrough",
            "fan_in_fan_out", "round_amount_ratio", "nested_correspondent_flow",
        ),
        category="regulated",
    ),
    GoldCase(
        id="G22",
        hypothesis=(
            "We need to assess mortgage-applicant affordability at origination in a way that is "
            "defensible under fair-lending / responsible-lending review."
        ),
        prediction_goal="Assess repayment-capacity / affordability of a new mortgage applicant.",
        expected_primary="credit.underwriting.affordability",
        permitted_secondary=(),
        expected_relevant_recipes=("dscr_covenant_headroom", "salary_signal", "payment_ratio"),
        category="regulated",
    ),
    GoldCase(
        id="G23",
        hypothesis=(
            "For sanctions and screening compliance, we must flag customers and payments with "
            "elevated sanctions-evasion or high-risk-corridor exposure."
        ),
        prediction_goal="Score sanctions / high-risk-corridor exposure for screening compliance.",
        expected_primary="aml_cft.sanctions",
        permitted_secondary=(),
        expected_relevant_recipes=("high_risk_corridor_exposure", "screening_exposure"),
        category="regulated",
    ),
    GoldCase(
        id="G26",
        hypothesis=(
            "For mandate-compliance reporting we must monitor investment portfolios approaching a "
            "benchmark tracking-error or mandate breach."
        ),
        prediction_goal="Predict proximity to an investment-mandate / tracking-error breach.",
        expected_primary="asset_management.mandate_compliance",
        permitted_secondary=(),
        expected_relevant_recipes=("tracking_error_breach_proximity", "mandate_breach_proximity"),
        category="regulated",
    ),

    # ── multi_use_case: a legitimate primary plus secondaries ──────────────────────────────────────
    GoldCase(
        id="G24",
        hypothesis=(
            "We want an early-warning view of our corporate lending book that also feeds "
            "facility-limit monitoring and picks up name and sector concentration building up."
        ),
        prediction_goal="Early-warning deterioration score, feeding limit monitoring and concentration watch.",
        expected_primary="credit.early_warning",
        permitted_secondary=("credit.monitoring.limit_management", "portfolio_risk.concentration"),
        expected_relevant_recipes=(
            "credit_utilisation", "exposure_trend", "covenant_headroom_breach",
            "trading_limit_utilisation", "loan_to_value", "group_exposure_aggregation", "book_desk_concentration",
        ),
        category="multi_use_case",
    ),
    GoldCase(
        id="G25",
        hypothesis=(
            "We are being hit by account-takeover attacks that show up as both new-device logins and "
            "anomalous payment velocity, and we want to score them in real time."
        ),
        prediction_goal="Score real-time activity for account-takeover-driven fraudulent payments.",
        expected_primary="fraud.transaction_fraud_detection",
        permitted_secondary=("fraud.account_takeover",),
        expected_relevant_recipes=(
            "new_device_flag", "device_sharing_velocity", "geo_velocity_impossible",
            "txn_velocity_spike", "cross_channel_rail_anomaly",
        ),
        category="multi_use_case",
    ),

    # ── extra synonym: treasury liquidity in analyst wording ("hot money runs first") ──────────────
    GoldCase(
        id="G27",
        hypothesis=(
            "Under a funding stress, we want to know how resilient our liquidity buffer is and which "
            "deposits are hot money likely to run first."
        ),
        prediction_goal="Estimate liquidity resilience / outflow under a stress scenario.",
        expected_primary="treasury_alm.liquidity",
        permitted_secondary=(),
        expected_relevant_recipes=(
            "lcr_outflow_weight", "hqla_eligibility_contribution", "nsfr_asf_contribution",
            "hot_money_share", "deposit_beta",
        ),
        category="synonym",
    ),
)
