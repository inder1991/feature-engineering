"""BR-12 — the credit-risk pack: 16 legacy templates → 20 atomic V2 recipes.

The corrections, structural: every exposure operand names its ECONOMIC ROLE (a deposit balance
cannot ground a drawn-exposure recipe — BR-5's evidence gate enforces what these declare);
facility features run at facility grain; utilization level and trend are two recipes sharing a
same-as-of/same-currency policy; minimum-payment behaviour requires the CONTRACTUAL minimum due
(the percent-of-limit approximation survives only as conceptual); missed/partial payments
require schedule due date, amount due and payment allocation; LTV splits raw/indexed/trend over
effective-dated valuations with haircut, seniority and allocation policies; the near-label
family (DPD, buckets, stage, SICR, forbearance, ECL) carries permitted-use stages — monitoring
and collections may read them, origination and default prediction may not; ECL and stage reads
carry the accounting engine's model provenance policy; and bureau reads declare knowledge time
so a later bureau state never leaks into an earlier cutoff.
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

OBLIGOR_MONITORING = "credit.monitoring.obligor"
EARLY_WARNING = "credit.early_warning"

CREDIT_CCY = "currency_conversion:credit-base-currency-same-asof"
SAME_ASOF = "business_calendar:credit-same-asof-numerator-denominator"
COLLATERAL_POLICY = "allocation:collateral-allocation-haircut-seniority"
PROPERTY_INDEX = "risk_corridor:collateral-valuation-index"
ECL_PROVENANCE = "model_output:ifrs9-ecl-engine"
BUREAU_KNOWLEDGE = "business_calendar:bureau-pull-knowledge-time"
CREDIT_ELIGIBLE = "eligible_status:credit-posted-events"
CREDIT_REVERSALS = "reversal_correction:credit-flag-or-code"

#: The near-label permitted-use contract (the plan's marking, verbatim).
NEAR_LABEL_USE = LeakageSpecV2(
    classification="near_label",
    permitted_stages=("monitoring", "collections"),
    prohibited_stages=("origination", "default_prediction"))


def _facility(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="facility", concept="facility_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _as_of(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="as_of_date", concept="as_of_date",
                         operand_class="as_of_timestamp", allowed_source_grains=(source,))


def _drawn(source: str = "facility_day_snapshot") -> OperandSpecV2:
    return OperandSpecV2(role="drawn", concept="drawn_principal", operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation="monetary",
                         economic_role="drawn_credit_exposure")


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"credit:{output_id}",
                              result_class=result_class)


_FACILITY_SNAPSHOT = TemporalSpecV2(
    anchor_kind="as_of", business_effective_role="as_of_date",
    window_basis="trailing", window_unit="days", window_parameter="window",
    cutoff_inclusivity="inclusive",
    snapshot_policy="latest-known facility snapshot at or before each day's cutoff")

_SNAPSHOT_ELIGIBILITY = EligibilitySpecV2(
    included="latest-known facility snapshots, numerator and denominator at the SAME as-of in "
             "the SAME currency",
    excluded="superseded snapshots; mixed-as-of or mixed-currency pairs",
    policy_refs=(CREDIT_CCY, SAME_ASOF))


def _event_window() -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


def _event_ts(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="event_ts", concept="event_timestamp",
                         operand_class="event_timestamp", allowed_source_grains=(source,))


CREDIT_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── utilization: level and trend are two recipes (corrections 1-4) ──────────────────────────
    RecipeDefinitionV2(
        recipe_id="utilization_level", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING, supporting_objectives=(EARLY_WARNING,),
        business_definition=(
            "Drawn credit exposure divided by the approved limit at the cutoff — numerator and "
            "denominator at the SAME as-of in the SAME currency, each operand carrying its "
            "economic role so a deposit balance can never stand in for drawn exposure."),
        decision_context="facility utilization monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="utilization_level", display_label="Utilization",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="a facility missing either side at the as-of returns null",
            empty_population_policy="no known snapshot returns null",
            zero_denominator_policy="a zero limit returns null, never infinity"),
        operands=(_facility("facility_day_snapshot"), _drawn(),
                  OperandSpecV2(role="limit", concept="limit", operand_class="measure",
                                allowed_source_grains=("facility_day_snapshot",),
                                unit_expectation="monetary",
                                economic_role="approved_credit_limit"),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_SNAPSHOT_ELIGIBILITY,
        formula=_formula("utilization_level", "ratio"),
        replaces_legacy_ids=("credit_utilisation",)),
    RecipeDefinitionV2(
        recipe_id="utilization_trend", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING, supporting_objectives=(OBLIGOR_MONITORING,),
        business_definition=(
            "OLS slope of daily utilization over the window — the drawdown trajectory, over the "
            "same same-as-of/same-currency pairs the level reads."),
        decision_context="drawdown-acceleration early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="utilization_trend", display_label="Utilization trend",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="utilization points per day",
            null_input_policy="days missing either side are excluded from the fit",
            empty_population_policy="fewer than two valid days returns null"),
        operands=(_facility("facility_day_snapshot"), _drawn(),
                  OperandSpecV2(role="limit", concept="limit", operand_class="measure",
                                allowed_source_grains=("facility_day_snapshot",),
                                unit_expectation="monetary",
                                economic_role="approved_credit_limit"),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_SNAPSHOT_ELIGIBILITY,
        formula=_formula("utilization_trend", "slope"),
        replaces_legacy_ids=("credit_utilisation",)),

    RecipeDefinitionV2(
        recipe_id="ead_slope", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "OLS slope of exposure-at-default over the window's facility snapshots — rising "
            "exposure into deteriorating credit is the early-warning shape."),
        decision_context="exposure trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="ead_slope", display_label="EAD slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=CREDIT_CCY,
            null_input_policy="days with no known snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null"),
        operands=(_facility("facility_day_snapshot"),
                  OperandSpecV2(role="ead", concept="ead", operand_class="measure",
                                allowed_source_grains=("facility_day_snapshot",),
                                unit_expectation="monetary",
                                economic_role="exposure_at_default"),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=_SNAPSHOT_ELIGIBILITY,
        formula=_formula("ead_slope", "slope"),
        replaces_legacy_ids=("exposure_trend",)),

    # ── the near-label family: marked with permitted-use stages (correction 10) ─────────────────
    RecipeDefinitionV2(
        recipe_id="days_past_due_max", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition="Worst days-past-due reached in the window (max of daily DPD).",
        decision_context="delinquency monitoring — near-label; not for origination models",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="days_past_due_max", display_label="Worst DPD in window",
            output_type="integer", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="days with no known DPD are excluded",
            empty_population_policy="no known DPD returns null"),
        operands=(_facility("facility_day_snapshot"),
                  OperandSpecV2(role="dpd", concept="dpd", operand_class="measure",
                                allowed_source_grains=("facility_day_snapshot",)),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        formula=_formula("days_past_due_max", "extremum"),
        replaces_legacy_ids=("days_past_due_max",)),
    RecipeDefinitionV2(
        recipe_id="delinquency_bucket_worst", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition="Worst delinquency bucket reached in the window.",
        decision_context="delinquency monitoring — near-label; not for origination models",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="delinquency_bucket_worst", display_label="Worst delinquency bucket",
            output_type="integer", additivity="non_additive", unit_kind="count",
            null_input_policy="days with no known bucket are excluded",
            empty_population_policy="no known bucket returns null"),
        operands=(_facility("facility_day_snapshot"),
                  OperandSpecV2(role="bucket", concept="delinquency_bucket",
                                operand_class="measure",
                                allowed_source_grains=("facility_day_snapshot",)),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        formula=_formula("delinquency_bucket_worst", "extremum"),
        replaces_legacy_ids=("delinquency_bucket_dynamics",)),

    RecipeDefinitionV2(
        recipe_id="repayment_coverage_ratio", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "Sum of repayment flows in the window divided by drawn exposure at the window "
            "start — how much of the balance the period's payments covered."),
        decision_context="repayment capacity monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="repayment_coverage_ratio", display_label="Repayment coverage",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="null repayment amounts are excluded per the source policy",
            empty_population_policy="no payments in the window returns zero coverage",
            zero_denominator_policy="zero drawn exposure returns null"),
        operands=(_facility("repayment_event"),
                  OperandSpecV2(role="repayment", concept="monetary_flow",
                                operand_class="measure",
                                allowed_source_grains=("repayment_event",),
                                unit_expectation="monetary",
                                economic_role="loan_repayment"),
                  _drawn("repayment_event"),
                  _event_ts("repayment_event")),
        source_grain="repayment_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted repayment flows against the facility",
            excluded="reversed payments; fee-only postings",
            policy_refs=(CREDIT_ELIGIBLE, CREDIT_REVERSALS, CREDIT_CCY)),
        formula=_formula("repayment_coverage_ratio", "ratio"),
        replaces_legacy_ids=("payment_ratio",)),

    # ── minimum-payment behaviour needs the CONTRACTUAL minimum (correction 5) ──────────────────
    RecipeDefinitionV2(
        recipe_id="min_payment_only_streak", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Consecutive billing periods where the amount paid matched the CONTRACTUAL minimum "
            "due (within the tolerance policy) — read from the schedule's minimum_due_amount, "
            "never approximated from a percent of limit."),
        decision_context="revolver stress early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="min_payment_only_streak", display_label="Minimum-payment streak",
            output_type="integer", additivity="non_additive", unit_kind="count",
            null_input_policy="periods missing the contractual minimum are excluded and end "
                              "the streak honestly",
            empty_population_policy="no billing periods in the window returns null"),
        operands=(_facility("billing_period"),
                  OperandSpecV2(role="minimum_due", concept="minimum_due_amount",
                                operand_class="measure",
                                allowed_source_grains=("billing_period",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="paid", concept="monetary_flow", operand_class="measure",
                                allowed_source_grains=("billing_period",),
                                unit_expectation="monetary",
                                economic_role="loan_repayment"),
                  _event_ts("billing_period")),
        source_grain="billing_period", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="billing periods with a contractual minimum and posted payments",
            excluded="periods with no schedule",
            policy_refs=(CREDIT_ELIGIBLE, CREDIT_REVERSALS)),
        formula=_formula("min_payment_only_streak", "extremum"),
        replaces_legacy_ids=("min_payment_only_streak",)),
    RecipeDefinitionV2(
        recipe_id="min_payment_pct_of_limit_pattern", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Minimum-payment behaviour approximated as payments near a percent of limit, for "
            "sources with no billing schedule."),
        decision_context="fallback when no schedule exists",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "The plan keeps the percent-of-limit approximation CONCEPTUAL: without the "
            "contractual minimum a threshold guess manufactures streaks, and a schedule-less "
            "source should surface as a data gap, not a fabricated behaviour signal."),
        output=OutputSpecV2(
            output_id="min_payment_pct_of_limit_pattern",
            display_label="Minimum-payment pattern (approximation)",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_facility("billing_period"),),
        source_grain="billing_period", output_grain="facility",
        temporal=_event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,),
        replaces_legacy_ids=("min_payment_only_streak",)),

    # ── missed/partial payments need the schedule (correction 6) ────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="missed_partial_payment_count", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING, supporting_objectives=(EARLY_WARNING,),
        business_definition=(
            "Count of scheduled installments in the window where the amount allocated to the "
            "installment fell short of the scheduled amount — read from the schedule's due "
            "date, amount due and the governed payment allocation; a generic payment flow "
            "cannot establish a missed payment without a schedule."),
        decision_context="payment behaviour monitoring",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="missed_partial_payment_count", display_label="Missed/partial payments",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="installments missing schedule fields are excluded and reported "
                              "as a data gap",
            empty_population_policy="no scheduled installments in the window returns null"),
        operands=(_facility("installment_schedule"),
                  OperandSpecV2(role="due_date", concept="due_date",
                                operand_class="event_timestamp",
                                allowed_source_grains=("installment_schedule",)),
                  OperandSpecV2(role="amount_due", concept="scheduled_amount",
                                operand_class="measure",
                                allowed_source_grains=("installment_schedule",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="allocation", concept="payment_allocation",
                                operand_class="policy_input",
                                allowed_source_grains=("installment_schedule",),
                                status_policy_ref="allocation:payment-application-order"),
                  ),
        source_grain="installment_schedule", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled installments due in the window with a governed allocation",
            excluded="unscheduled payments; fee allocations counted as principal",
            policy_refs=("allocation:payment-application-order", CREDIT_REVERSALS)),
        formula=_formula("missed_partial_payment_count", "count"),
        replaces_legacy_ids=("missed_partial_payment_count",)),

    # ── ECL and stage: recorded ACCOUNTING outputs with model provenance (corrections 10-11) ────
    RecipeDefinitionV2(
        recipe_id="ecl_provision_slope", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "OLS slope of the RECORDED IFRS9 ECL provision over the window — a read of the "
            "accounting engine's output under its model provenance policy, never a computation "
            "of ECL itself."),
        decision_context="provision trajectory — near-label; not for origination models",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="ecl_provision_slope", display_label="ECL provision slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=CREDIT_CCY,
            null_input_policy="days with no recorded provision use the latest prior record",
            empty_population_policy="fewer than two recorded days returns null"),
        operands=(_facility("provision_snapshot"),
                  OperandSpecV2(role="provision", concept="provision_amount",
                                operand_class="measure",
                                allowed_source_grains=("provision_snapshot",),
                                unit_expectation="monetary"),
                  _as_of("provision_snapshot")),
        source_grain="provision_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        eligibility=EligibilitySpecV2(
            included="recorded provisions carrying the ECL engine's model/version provenance",
            excluded="provisions with no model provenance",
            policy_refs=(ECL_PROVENANCE, CREDIT_CCY)),
        formula=_formula("ecl_provision_slope", "slope"),
        replaces_legacy_ids=("ecl_provision_trend",)),
    RecipeDefinitionV2(
        recipe_id="stage_worsened_flag", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "Whether the RECORDED IFRS9 impairment stage at the cutoff is worse than at the "
            "window start — a read of the accounting engine's staging, with its provenance."),
        decision_context="stage migration monitoring — near-label",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="stage_worsened_flag", display_label="Stage worsened",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="a missing stage at either end returns null",
            empty_population_policy="no recorded stages returns null"),
        operands=(_facility("provision_snapshot"),
                  OperandSpecV2(role="stage", concept="impairment_stage",
                                operand_class="status",
                                allowed_source_grains=("provision_snapshot",)),
                  _as_of("provision_snapshot")),
        source_grain="provision_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        eligibility=EligibilitySpecV2(
            included="recorded stages carrying the ECL engine's model/version provenance",
            excluded="stages with no model provenance",
            policy_refs=(ECL_PROVENANCE,)),
        formula=_formula("stage_worsened_flag", "flag"),
        replaces_legacy_ids=("stage_migration",)),

    # ── LTV: raw / indexed / trend over effective-dated valuations (corrections 7-8) ────────────
    RecipeDefinitionV2(
        recipe_id="ltv_raw", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "Drawn exposure divided by the effective-dated collateral valuation, after the "
            "governed haircut, respecting lien seniority and the collateral allocation policy "
            "— the valuation effective AT the cutoff, never a later one."),
        decision_context="collateral coverage",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="ltv_raw", display_label="LTV (raw)",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="a facility with no effective valuation returns null",
            empty_population_policy="no collateral returns null — unsecured is not LTV zero",
            zero_denominator_policy="a zero valuation returns null, never infinity"),
        operands=(_facility("collateral_valuation"), _drawn("collateral_valuation"),
                  OperandSpecV2(role="valuation", concept="collateral_value",
                                operand_class="measure",
                                allowed_source_grains=("collateral_valuation",),
                                unit_expectation="monetary",
                                economic_role="collateral_valuation"),
                  OperandSpecV2(role="valid_interval", concept="valid_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("collateral_valuation",))),
        source_grain="collateral_valuation", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="valuations effective at the cutoff, haircut and seniority applied per "
                     "policy",
            excluded="valuations effective after the cutoff; junior liens counted as senior",
            policy_refs=(COLLATERAL_POLICY, CREDIT_CCY)),
        formula=_formula("ltv_raw", "ratio"),
        replaces_legacy_ids=("loan_to_value",)),
    RecipeDefinitionV2(
        recipe_id="ltv_indexed", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "LTV with the effective-dated valuation rolled forward by the governed valuation "
            "index to the cutoff — an INDEXED estimate, named as one."),
        decision_context="portfolio LTV between revaluations",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="ltv_indexed", display_label="LTV (indexed)",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="a facility with no effective valuation or index reading "
                              "returns null",
            empty_population_policy="no collateral returns null",
            zero_denominator_policy="a zero indexed valuation returns null"),
        operands=(_facility("collateral_valuation"), _drawn("collateral_valuation"),
                  OperandSpecV2(role="valuation", concept="collateral_value",
                                operand_class="measure",
                                allowed_source_grains=("collateral_valuation",),
                                unit_expectation="monetary",
                                economic_role="collateral_valuation"),
                  OperandSpecV2(role="index", concept="benchmark_rate",
                                operand_class="policy_input",
                                allowed_source_grains=("collateral_valuation",),
                                status_policy_ref=PROPERTY_INDEX),
                  OperandSpecV2(role="valid_interval", concept="valid_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("collateral_valuation",))),
        source_grain="collateral_valuation", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="effective valuations indexed by the governed index to the cutoff",
            excluded="valuations effective after the cutoff",
            policy_refs=(COLLATERAL_POLICY, PROPERTY_INDEX, CREDIT_CCY)),
        formula=_formula("ltv_indexed", "ratio"),
        replaces_legacy_ids=("loan_to_value",)),
    RecipeDefinitionV2(
        recipe_id="ltv_trend", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition="OLS slope of raw LTV over the window's effective valuations.",
        decision_context="collateral erosion early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="ltv_trend", display_label="LTV trend",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="LTV points per day",
            null_input_policy="days with no effective valuation are excluded from the fit",
            empty_population_policy="fewer than two valid days returns null"),
        operands=(_facility("collateral_valuation"), _drawn("collateral_valuation"),
                  OperandSpecV2(role="valuation", concept="collateral_value",
                                operand_class="measure",
                                allowed_source_grains=("collateral_valuation",),
                                unit_expectation="monetary",
                                economic_role="collateral_valuation"),
                  OperandSpecV2(role="valid_interval", concept="valid_time",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("collateral_valuation",))),
        source_grain="collateral_valuation", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="effective valuations inside the window",
            excluded="valuations effective after the cutoff",
            policy_refs=(COLLATERAL_POLICY, CREDIT_CCY)),
        formula=_formula("ltv_trend", "slope"),
        replaces_legacy_ids=("loan_to_value",)),

    # ── bureau reads declare knowledge time (correction 12) ─────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="bureau_score_delta", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Latest bureau score minus the earliest in the window — each read AS KNOWN at its "
            "pull, so a later bureau state never informs an earlier cutoff."),
        decision_context="external credit trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="bureau_score_delta", display_label="Bureau score delta",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="pulls with unknown knowledge time are excluded",
            empty_population_policy="fewer than two pulls in the window returns null"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="score", concept="bureau_score", operand_class="measure",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="pull_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="knowledge_ts", concept="system_time",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("bureau_pull",)),
        ),
        source_grain="bureau_pull", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="pull_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="bureau pulls whose knowledge time is at or before the cutoff",
            excluded="pulls learned after the cutoff",
            policy_refs=(BUREAU_KNOWLEDGE,)),
        formula=_formula("bureau_score_delta", "ratio"),
        replaces_legacy_ids=("bureau_score_delta",)),
    RecipeDefinitionV2(
        recipe_id="bureau_inquiry_velocity", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition="Count of HARD bureau inquiries in the window, as known at pull.",
        decision_context="credit-seeking velocity",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="bureau_inquiry_velocity", display_label="Hard inquiry count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="inquiries with unknown kind are excluded (soft never counts)",
            empty_population_policy="no pulls covering the window returns null — absence of "
                                    "data is not zero inquiries"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="inquiry", concept="bureau_inquiry", operand_class="measure",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="pull_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="knowledge_ts", concept="system_time",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("bureau_pull",)),
        ),
        source_grain="bureau_pull", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="pull_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="hard inquiries known at or before the cutoff",
            excluded="soft inquiries; pulls learned after the cutoff",
            policy_refs=(BUREAU_KNOWLEDGE,)),
        formula=_formula("bureau_inquiry_velocity", "count"),
        replaces_legacy_ids=("bureau_inquiry_velocity",)),
    RecipeDefinitionV2(
        recipe_id="new_trade_line_count", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition="Count of NEW bureau tradelines opened in the window, as known "
                            "at pull.",
        decision_context="new-borrowing velocity",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="new_trade_line_count", display_label="New tradelines",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="tradelines with unknown open date are excluded",
            empty_population_policy="no pulls covering the window returns null"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="trade_line", concept="trade_line", operand_class="measure",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="pull_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("bureau_pull",)),
            OperandSpecV2(role="knowledge_ts", concept="system_time",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("bureau_pull",)),
        ),
        source_grain="bureau_pull", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="pull_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="tradelines opened in the window, known at or before the cutoff",
            excluded="pulls learned after the cutoff",
            policy_refs=(BUREAU_KNOWLEDGE,)),
        formula=_formula("new_trade_line_count", "count"),
        replaces_legacy_ids=("new_trade_line_count",)),

    # ── event flags in the near-label family ────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="forbearance_in_window", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition="Whether a forbearance/restructure event occurred in the window.",
        decision_context="forbearance monitoring — near-label",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="forbearance_in_window", display_label="Forbearance in window",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="events with unknown kind are excluded",
            empty_population_policy="no events returns false — a real answer for a flag"),
        operands=(_facility("credit_event"),
                  OperandSpecV2(role="flag", concept="restructured_flag",
                                operand_class="status",
                                allowed_source_grains=("credit_event",)),
                  _event_ts("credit_event")),
        source_grain="credit_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        formula=_formula("forbearance_in_window", "flag"),
        replaces_legacy_ids=("forbearance_in_window",)),
    RecipeDefinitionV2(
        recipe_id="sicr_onset", revision=1, family="credit_risk",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "Whether a recorded SICR trigger fired in the window — a read of the staging "
            "engine's trigger under its provenance policy."),
        decision_context="SICR monitoring — near-label",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sicr_onset", display_label="SICR onset",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="records with no SICR field are excluded",
            empty_population_policy="no records returns null — silence is not 'no trigger'"),
        operands=(_facility("provision_snapshot"),
                  OperandSpecV2(role="sicr", concept="sicr_flag", operand_class="status",
                                allowed_source_grains=("provision_snapshot",)),
                  _as_of("provision_snapshot")),
        source_grain="provision_snapshot", output_grain="facility",
        temporal=_FACILITY_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        eligibility=EligibilitySpecV2(
            included="records carrying the staging engine's provenance",
            excluded="records with no model provenance",
            policy_refs=(ECL_PROVENANCE,)),
        formula=_formula("sicr_onset", "flag"),
        replaces_legacy_ids=("sicr_onset",)),

    # ── covenant headroom with the full declared covenant record (correction 9) ─────────────────
    RecipeDefinitionV2(
        recipe_id="dscr_covenant_headroom", revision=1, family="credit_risk",
        primary_objective=EARLY_WARNING,
        business_definition=(
            "Margin between the covenant's ACTUAL tested value and its threshold, in the "
            "covenant's own unit and direction, at the test date — waiver and cure state "
            "declared, so a waived breach is never a silent pass."),
        decision_context="covenant headroom early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="dscr_covenant_headroom", display_label="Covenant headroom",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="tests missing actual, threshold, direction or unit return "
                              "null and surface the gap",
            empty_population_policy="no covenant tests in the window returns null",
            zero_denominator_policy="a zero threshold returns null"),
        operands=(_facility("covenant_test"),
                  OperandSpecV2(role="actual", concept="covenant", operand_class="measure",
                                allowed_source_grains=("covenant_test",)),
                  OperandSpecV2(role="threshold", concept="covenant",
                                operand_class="policy_input",
                                allowed_source_grains=("covenant_test",),
                                status_policy_ref="threshold:covenant-terms",
                                distinct_binding_group="covenant_sides"),
                  OperandSpecV2(role="actual_side", concept="covenant",
                                operand_class="dimension",
                                allowed_source_grains=("covenant_test",),
                                distinct_binding_group="covenant_sides"),
                  OperandSpecV2(role="waiver_state", concept="lifecycle_state",
                                operand_class="status",
                                allowed_source_grains=("covenant_test",)),
                  OperandSpecV2(role="test_date", concept="event_timestamp",
                                operand_class="event_timestamp",
                                allowed_source_grains=("covenant_test",))),
        source_grain="covenant_test", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="test_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="covenant tests with actual, threshold, direction, unit and waiver/cure "
                     "state declared",
            excluded="tests under an active waiver counted as clean passes",
            policy_refs=("threshold:covenant-terms",)),
        formula=_formula("dscr_covenant_headroom", "ratio"),
        replaces_legacy_ids=("dscr_covenant_headroom",)),
)
