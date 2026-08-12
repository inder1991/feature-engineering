"""BR-13 — the AML pack: 11 legacy templates → 13 atomic V2 recipes.

The corrections, structural: the structuring threshold is a GOVERNED policy carrying
jurisdiction, effective period, currency and instrument — never a hardcoded number; cash is
identified by channel/instrument operands, never the ISO purpose code standing in as a cash
proxy; round-amount detection is base-currency-aware and carries no unrelated purpose-code
operand; corridor risk is an effective-dated governed classification; fan-in/fan-out and
passthrough carry their party-role legs as DISTINCT binding groups (two legs, one physical
column refused — the counterparty legs are never merged); nested correspondent flow runs at an
explicit respondent/correspondent grain; screening splits into exposure, alert and confirmed
match — three facts with three leakage postures; and alert/case/SAR history is near-label AND
time-lagged (the alert's availability time is knowledge time, not its event time).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

STM = "aml_cft.suspicious_transaction_monitoring"

AML_ELIGIBLE = "eligible_status:aml-posted-events"
REPORTING_THRESHOLD = "threshold:aml-reporting-jurisdictional"
CORRIDOR_RISK = "risk_corridor:country-corridor-effective-dated"
VASP_CLASSIFICATION = "risk_corridor:crypto-vasp-classification"
AML_BASE_CCY = "currency_conversion:aml-base-currency"

#: Alert/case/SAR facts border the SAR label and arrive LATE — near-label, and read through
#: knowledge time so an alert filed after the cutoff never informs it.
ALERT_HISTORY_USE = LeakageSpecV2(
    classification="near_label",
    permitted_stages=("monitoring", "investigation_support"),
    prohibited_stages=("origination", "sar_prediction"))


def _customer(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _amount(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation="monetary")


def _event_ts(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="event_ts", concept="event_timestamp",
                         operand_class="event_timestamp", allowed_source_grains=(source,))


def _cash_markers() -> tuple[OperandSpecV2, ...]:
    """Cash is channel + instrument, NEVER the purpose code."""
    return (
        OperandSpecV2(role="channel", concept="channel", operand_class="dimension",
                      allowed_source_grains=("transaction",)),
        OperandSpecV2(role="instrument", concept="instrument_type", operand_class="dimension",
                      allowed_source_grains=("transaction",)),
    )


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"aml:{output_id}",
                              result_class=result_class)


def _days_window(role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


AML_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── structuring under the GOVERNED jurisdictional threshold (correction 1-2) ────────────────
    RecipeDefinitionV2(
        recipe_id="structuring_smurfing", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Count of CASH transactions (channel/instrument-identified) within the governed "
            "tolerance just below the reporting threshold — the threshold is jurisdictional, "
            "effective-dated, currency- and instrument-scoped policy, never a number in a "
            "recipe."),
        decision_context="structuring detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="structuring_smurfing", display_label="Sub-threshold cash count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown channel or instrument are excluded",
            empty_population_policy="no cash activity returns zero"),
        operands=(_customer(), _amount(), _event_ts(), *_cash_markers(),
                  OperandSpecV2(role="reporting_threshold", concept="limit",
                                operand_class="policy_input",
                                allowed_source_grains=("transaction",),
                                status_policy_ref=REPORTING_THRESHOLD)),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="cash-channel/instrument transactions against the threshold effective at "
                     "their event date, in the threshold's currency",
            excluded="non-cash channels; purpose-code-only 'cash'; thresholds read across "
                     "jurisdictions or effective periods",
            policy_refs=(AML_ELIGIBLE, REPORTING_THRESHOLD)),
        formula=_formula("structuring_smurfing", "count"),
        replaces_legacy_ids=("structuring_smurfing",)),
    RecipeDefinitionV2(
        recipe_id="cash_intensity_ratio", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Cash-channel/instrument value divided by total posted value over the window, in "
            "base currency."),
        decision_context="cash-intensity monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cash_intensity_ratio", display_label="Cash intensity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown channel/instrument are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero total value returns null"),
        operands=(_customer(), _amount(), _event_ts(), *_cash_markers()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions; cash identified by channel/instrument",
            excluded="purpose-code-only cash identification",
            policy_refs=(AML_ELIGIBLE, AML_BASE_CCY)),
        formula=_formula("cash_intensity_ratio", "ratio"),
        replaces_legacy_ids=("cash_intensity_ratio",)),

    # ── passthrough and fan-in/fan-out carry DISTINCT party legs (correction 6) ─────────────────
    RecipeDefinitionV2(
        recipe_id="rapid_movement_passthrough", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Outbound value divided by inbound value within the short window — the in-leg and "
            "out-leg counterparties are two DISTINCT operands (one physical column refused), "
            "because money in from A and out to A is not passthrough."),
        decision_context="passthrough detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rapid_movement_passthrough", display_label="Passthrough ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="rows with no counterparty identity are excluded",
            empty_population_policy="no inbound value returns null",
            zero_denominator_policy="zero inbound value returns null"),
        operands=(_customer(), _amount(), _event_ts(),
                  OperandSpecV2(role="direction", concept="debit_credit_indicator",
                                operand_class="direction",
                                allowed_source_grains=("transaction",),
                                status_policy_ref=AML_ELIGIBLE),
                  OperandSpecV2(role="in_counterparty", concept="customer_id",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                distinct_binding_group="passthrough_legs"),
                  OperandSpecV2(role="out_counterparty", concept="customer_id",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                distinct_binding_group="passthrough_legs")),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with distinct in/out counterparty identities",
            excluded="legs merged onto one physical column",
            policy_refs=(AML_ELIGIBLE, AML_BASE_CCY)),
        formula=_formula("rapid_movement_passthrough", "ratio"),
        replaces_legacy_ids=("rapid_movement_passthrough",)),
    RecipeDefinitionV2(
        recipe_id="fan_in_fan_out", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Distinct paying counterparties (fan-in) and distinct paid counterparties "
            "(fan-out) — the two party-role legs are DISTINCT binding operands; a source "
            "whose single counterparty column serves both legs is refused, never merged."),
        decision_context="mule-pattern network shape",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="fan_in_fan_out", display_label="Fan-in × fan-out",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with no counterparty identity are excluded",
            empty_population_policy="no counterparty activity returns zero"),
        operands=(_customer(), _event_ts(),
                  OperandSpecV2(role="payer", concept="customer_id",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                distinct_binding_group="fan_legs"),
                  OperandSpecV2(role="payee", concept="customer_id",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",),
                                distinct_binding_group="fan_legs")),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with party-role-resolved counterparties",
            excluded="legs merged onto one physical column",
            policy_refs=(AML_ELIGIBLE,)),
        formula=_formula("fan_in_fan_out", "distinct_count"),
        replaces_legacy_ids=("fan_in_fan_out",)),

    # ── round amounts, corridor risk, crypto ramps (corrections 3-4, 10) ────────────────────────
    RecipeDefinitionV2(
        recipe_id="round_amount_ratio", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Share of posted transactions whose BASE-CURRENCY amount is round under the "
            "governed rounding definition — roundness is currency-aware (10,000 AED and "
            "10,000 JPY are different facts), and no purpose code is consulted."),
        decision_context="round-amount pattern",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="round_amount_ratio", display_label="Round-amount share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="no posted transactions returns null",
            zero_denominator_policy="zero transactions returns null"),
        operands=(_customer(), _amount(), _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions converted to base currency before the roundness test",
            excluded="purpose-code operands (unrelated to roundness)",
            policy_refs=(AML_ELIGIBLE, AML_BASE_CCY)),
        formula=_formula("round_amount_ratio", "share"),
        replaces_legacy_ids=("round_amount_ratio",)),
    RecipeDefinitionV2(
        recipe_id="high_risk_corridor_exposure", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Share of posted value flowing to corridors classified HIGH RISK by the "
            "effective-dated corridor authority, with transaction direction read from the "
            "governed indicator."),
        decision_context="corridor risk exposure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="high_risk_corridor_exposure", display_label="High-risk corridor share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown corridor are excluded from the numerator, "
                              "kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero total value returns null"),
        operands=(_customer(), _amount(), _event_ts(),
                  OperandSpecV2(role="corridor", concept="corridor", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  OperandSpecV2(role="direction", concept="debit_credit_indicator",
                                operand_class="direction",
                                allowed_source_grains=("transaction",),
                                status_policy_ref=AML_ELIGIBLE),
                  OperandSpecV2(role="risk_classification", concept="customer_risk_rating",
                                operand_class="policy_input",
                                allowed_source_grains=("transaction",),
                                status_policy_ref=CORRIDOR_RISK)),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted value against the corridor classification effective at event date",
            excluded="classifications read outside their effective period",
            policy_refs=(AML_ELIGIBLE, CORRIDOR_RISK, AML_BASE_CCY)),
        formula=_formula("high_risk_corridor_exposure", "share"),
        replaces_legacy_ids=("high_risk_corridor_exposure",)),
    RecipeDefinitionV2(
        recipe_id="nested_correspondent_flow", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Count of nested correspondent payments at the RESPONDENT bank grain — the "
            "respondent and correspondent are explicit BIC operands, and the nested marker is "
            "the message's own flag, so the recipe runs where correspondent data actually "
            "lives."),
        decision_context="nested correspondent monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="nested_correspondent_flow", display_label="Nested correspondent count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="messages missing either bank identity are excluded",
            empty_population_policy="no correspondent traffic returns zero"),
        operands=(
            OperandSpecV2(role="respondent_bank", concept="bank_bic",
                          operand_class="entity_key",
                          allowed_source_grains=("correspondent_payment",),
                          distinct_binding_group="correspondent_banks"),
            OperandSpecV2(role="correspondent_bank", concept="bank_bic",
                          operand_class="dimension",
                          allowed_source_grains=("correspondent_payment",),
                          distinct_binding_group="correspondent_banks"),
            OperandSpecV2(role="nested_marker", concept="nested_correspondent_flag",
                          operand_class="status",
                          allowed_source_grains=("correspondent_payment",)),
            _event_ts("correspondent_payment")),
        source_grain="correspondent_payment", output_grain="respondent_bank",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="correspondent payment messages with both bank identities",
            excluded="retail transactions (a different grain entirely)",
            policy_refs=(AML_ELIGIBLE,)),
        formula=_formula("nested_correspondent_flow", "count"),
        replaces_legacy_ids=("nested_correspondent_flow",)),
    RecipeDefinitionV2(
        recipe_id="crypto_offramp_exposure", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Share of posted value to counterparties classified as crypto on/off-ramps by the "
            "governed VASP classification authority — classification is a dated policy fact, "
            "never a name-pattern guess."),
        decision_context="crypto ramp exposure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="crypto_offramp_exposure", display_label="Crypto ramp share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unclassified counterparties are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero total value returns null"),
        operands=(_customer(), _amount(), _event_ts(),
                  OperandSpecV2(role="vasp_classification", concept="digital_asset",
                                operand_class="policy_input",
                                allowed_source_grains=("transaction",),
                                status_policy_ref=VASP_CLASSIFICATION)),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="value to counterparties under the VASP classification effective at "
                     "event date",
            excluded="name-pattern crypto guesses",
            policy_refs=(AML_ELIGIBLE, VASP_CLASSIFICATION, AML_BASE_CCY)),
        formula=_formula("crypto_offramp_exposure", "share"),
        replaces_legacy_ids=("crypto_offramp_exposure",)),

    RecipeDefinitionV2(
        recipe_id="dormant_reactivation", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Whether an account whose governed state was DORMANT at the window start posted "
            "eligible activity above the governed floor inside the window."),
        decision_context="dormant-reactivation monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="dormant_reactivation", display_label="Dormant reactivation",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="accounts with unknown state at the window start return null",
            empty_population_policy="no state history returns null"),
        operands=(
            OperandSpecV2(role="account", concept="account_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",)),
            OperandSpecV2(role="account_state", concept="account_status",
                          operand_class="status",
                          allowed_source_grains=("transaction",),
                          status_policy_ref="active_state:dormancy-definition"),
            _amount(), _event_ts()),
        source_grain="transaction", output_grain="account",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="accounts dormant under the governed dormancy definition at window start",
            excluded="accounts never dormant; system-only postings as 'activity'",
            policy_refs=(AML_ELIGIBLE, "active_state:dormancy-definition")),
        formula=_formula("dormant_reactivation", "flag"),
        replaces_legacy_ids=("dormant_reactivation",)),

    # ── screening: exposure, alert and confirmed match are THREE facts (correction 7) ───────────
    RecipeDefinitionV2(
        recipe_id="screening_exposure_share", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Share of posted value to counterparties carrying a watchlist or adverse-media "
            "HIT — screening exposure, before any human read it."),
        decision_context="screening exposure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="screening_exposure_share", display_label="Screening exposure",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="unscreened counterparties are excluded from the numerator, "
                              "kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero total value returns null"),
        operands=(_customer(), _amount(), _event_ts(),
                  OperandSpecV2(role="hit", concept="watchlist_hit_flag",
                                operand_class="status",
                                allowed_source_grains=("transaction",))),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted value to screened counterparties",
            excluded="alerts and confirmed matches (separate recipes, separate facts)",
            policy_refs=(AML_ELIGIBLE, AML_BASE_CCY)),
        formula=_formula("screening_exposure_share", "share"),
        replaces_legacy_ids=("screening_exposure",)),
    RecipeDefinitionV2(
        recipe_id="screening_alert_count", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Count of screening ALERTS raised on the customer in the window, as known at the "
            "cutoff — an alert is an analyst queue entry, not a hit and not a match."),
        decision_context="alert history — near-label, time-lagged",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="screening_alert_count", display_label="Screening alerts",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="alerts with unknown knowledge time are excluded",
            empty_population_policy="no alert feed coverage returns null — absence of feed "
                                    "is not zero alerts"),
        operands=(_customer("alert_event"),
                  OperandSpecV2(role="alert", concept="alert_id", operand_class="dimension",
                                allowed_source_grains=("alert_event",)),
                  _event_ts("alert_event"),
                  OperandSpecV2(role="knowledge_ts", concept="system_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("alert_event",))),
        source_grain="alert_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=ALERT_HISTORY_USE,
        formula=_formula("screening_alert_count", "count"),
        replaces_legacy_ids=("screening_exposure",)),
    RecipeDefinitionV2(
        recipe_id="confirmed_match_flag", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Whether a screening match was CONFIRMED by an investigator before the cutoff — "
            "the third and strongest screening fact, read through knowledge time."),
        decision_context="confirmed-match state — near-label, time-lagged",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="confirmed_match_flag", display_label="Confirmed match",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="matches with unknown disposition return null",
            empty_population_policy="no screening history returns null"),
        operands=(_customer("alert_event"),
                  OperandSpecV2(role="disposition", concept="case_id",
                                operand_class="status",
                                allowed_source_grains=("alert_event",)),
                  _event_ts("alert_event"),
                  OperandSpecV2(role="knowledge_ts", concept="system_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("alert_event",))),
        source_grain="alert_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=ALERT_HISTORY_USE,
        formula=_formula("confirmed_match_flag", "flag"),
        replaces_legacy_ids=("screening_exposure",)),
    RecipeDefinitionV2(
        recipe_id="prior_alert_recidivism", revision=1, family="aml",
        primary_objective=STM,
        business_definition=(
            "Count of prior alerts on the customer whose disposition was known BEFORE the "
            "cutoff — recidivism reads history through knowledge time, so an outcome recorded "
            "after the cutoff never informs it."),
        decision_context="alert recidivism — near-label, time-lagged",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="prior_alert_recidivism", display_label="Prior alerts (known)",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="alerts with unknown knowledge time are excluded",
            empty_population_policy="no alert feed coverage returns null"),
        operands=(_customer("alert_event"),
                  OperandSpecV2(role="alert", concept="alert_id", operand_class="dimension",
                                allowed_source_grains=("alert_event",)),
                  _event_ts("alert_event"),
                  OperandSpecV2(role="knowledge_ts", concept="system_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("alert_event",))),
        source_grain="alert_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=ALERT_HISTORY_USE,
        formula=_formula("prior_alert_recidivism", "count"),
        replaces_legacy_ids=("prior_alert_recidivism",)),
)
