"""BR-15 — the insurance pack: 10 legacy templates → 12 atomic V2 recipes + 2 model features.

Structural corrections: policy state, coverage period and the claim lifecycle are declared
operands; written, earned and collected premium are THREE recipes; the claims family splits
count, paid, reserve and loss ratio with CORRECT additivity (frequency additive, severity and
ratios never); lapse analysis names its competing-risk censoring policy; reinsurance
recoverable reads the treaty lifecycle; mortality/morbidity loading and claims-fraud scores
are MODEL outputs (registered specs); the BR-10 concepts close the old admissions —
policy_loan_balance sized against surrender_value, customer_income for needs analysis.
"""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_contract import ModelFeatureSpecV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes._shared import (
    as_of,
    dim,
    entity,
    event_ts,
    event_window,
    formula,
    measure,
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

SURRENDER = "insurance.lapse.surrender"
PERSISTENCY = "insurance.lapse.persistency"
CLAIMS_FRAUD = "insurance.claims.claims_fraud"
CLAIMS_COST = "insurance.actuarial.claims_cost_modelling"
MORTALITY = "insurance.underwriting.mortality_morbidity_risk_assessment"
REINSURANCE = "insurance.reinsurance"
BANCASSURANCE = "insurance.bancassurance"

PREMIUM_BASIS = "business_calendar:premium-earning-basis"
CENSORING = "risk_corridor:lapse-competing-risk-censoring"
TREATY = "allocation:reinsurance-treaty-terms"
INS_CCY = "currency_conversion:insurance-base-currency"

#: The BR-15 model reclassifications — actuarial loadings and fraud scores are predictions.
INSURANCE_MODEL_FEATURES: tuple[ModelFeatureSpecV1, ...] = (
    ModelFeatureSpecV1(
        model_feature_id="mortality_morbidity_loading", revision=1,
        model_family="loading", model_ref="models/mortality-morbidity",
        model_version="", owner="actuarial",
        prediction_grain="policy", prediction_timestamp_role="underwriting_cutoff",
        training_data_cutoff_policy="experience strictly before the underwriting cutoff",
        inference_knowledge_time_policy="declarations as known at underwriting",
        target_definition="mortality/morbidity loading over the policy term",
        outcome_window_days=365,
        input_feature_set_revision="pending-first-registered-table",
        score_type="rate",
        fallback_policy="standard table loading — never an unreviewed adjustment"),
    ModelFeatureSpecV1(
        model_feature_id="claims_fraud_score", revision=1,
        model_family="typology_score", model_ref="models/claims-fraud",
        model_version="", owner="claims-analytics",
        prediction_grain="claim", prediction_timestamp_role="scoring_cutoff",
        training_data_cutoff_policy="claims strictly before the label window",
        inference_knowledge_time_policy="claim facts as known at scoring",
        target_definition="claim confirmed fraudulent within the outcome window",
        outcome_window_days=180,
        input_feature_set_revision="pending-first-registered-pack",
        score_type="probability",
        fallback_policy="no score — route to manual triage, never a heuristic"),
)


def _policy(source: str) -> tuple:
    return (entity("policy", "policy_id", source),
            status("policy_state", "lifecycle_state", source))


INSURANCE_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── premium: written / earned / collected are three facts ───────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="written_premium_sum", revision=1, family="insurance",
        primary_objective=PERSISTENCY,
        business_definition="Premium WRITTEN in the window — contract inception amounts.",
        decision_context="premium production",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="written_premium_sum", display_label="Written premium",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=INS_CCY,
            null_input_policy="null premium rows are excluded per the source policy",
            empty_population_policy="no written premium returns zero",
            aggregation_over_entity="sum across policies",
            aggregation_over_time="sum over disjoint windows"),
        operands=(*_policy("premium_event"),
                  measure("premium", "premium", "premium_event"),
                  event_ts("premium_event")),
        source_grain="premium_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="written premium under the earning basis",
            excluded="earned or collected premium (different facts, own recipes)",
            policy_refs=(PREMIUM_BASIS, INS_CCY)),
        formula=formula("insurance", "written_premium_sum", "sum"),
        replaces_legacy_ids=("premium_payment_irregularity",)),
    RecipeDefinitionV2(
        recipe_id="collected_premium_sum", revision=1, family="insurance",
        primary_objective=PERSISTENCY,
        business_definition="Premium COLLECTED (cash received) in the window.",
        decision_context="collection health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="collected_premium_sum", display_label="Collected premium",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=INS_CCY,
            null_input_policy="null rows are excluded per the source policy",
            empty_population_policy="no collections returns zero",
            aggregation_over_entity="sum across policies",
            aggregation_over_time="sum over disjoint windows"),
        operands=(*_policy("premium_event"),
                  measure("collected", "monetary_flow", "premium_event"),
                  event_ts("premium_event")),
        source_grain="premium_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="cash premium receipts",
            excluded="written or earned premium (accrual facts)",
            policy_refs=(PREMIUM_BASIS, INS_CCY)),
        formula=formula("insurance", "collected_premium_sum", "sum"),
        replaces_legacy_ids=("premium_payment_irregularity",)),
    RecipeDefinitionV2(
        recipe_id="missed_premium_streak", revision=1, family="insurance",
        primary_objective=SURRENDER,
        business_definition=(
            "Consecutive premium due dates with no collected premium — read from the "
            "schedule (due_date), against collections; the pre-lapse behaviour signal."),
        decision_context="lapse early warning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="missed_premium_streak", display_label="Missed premium streak",
            output_type="integer", additivity="non_additive", unit_kind="count",
            null_input_policy="periods with no schedule end the streak and surface as a gap",
            empty_population_policy="no due dates in the window returns null"),
        operands=(*_policy("premium_schedule"),
                  event_ts("premium_schedule", role="due_date", concept="due_date"),
                  measure("collected", "monetary_flow", "premium_schedule")),
        source_grain="premium_schedule", output_grain="policy",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled dues against collections",
            excluded="unscheduled top-ups",
            policy_refs=(PREMIUM_BASIS,)),
        formula=formula("insurance", "missed_premium_streak", "extremum"),
        replaces_legacy_ids=("missed_premium_streak",)),

    RecipeDefinitionV2(
        recipe_id="surrender_value_trajectory", revision=1, family="insurance",
        primary_objective=SURRENDER,
        business_definition=(
            "OLS slope of surrender value over the window's policy snapshots — lapse "
            "analysis names its censoring policy: death, maturity and claim exits are "
            "competing risks, never lapses."),
        decision_context="surrender trajectory",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="surrender_value_trajectory", display_label="Surrender value slope",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="base currency units per day", currency_policy=INS_CCY,
            null_input_policy="days with no snapshot use the latest prior snapshot",
            empty_population_policy="fewer than two snapshot days returns null"),
        operands=(*_policy("policy_snapshot"),
                  measure("surrender_value", "surrender_value", "policy_snapshot"),
                  as_of("policy_snapshot")),
        source_grain="policy_snapshot", output_grain="policy",
        temporal=snapshot_window("latest policy snapshot at or before each day's cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="in-force policies under the censoring policy",
            excluded="death/maturity/claim exits counted as lapses",
            policy_refs=(CENSORING, INS_CCY)),
        formula=formula("insurance", "surrender_value_trajectory", "slope"),
        replaces_legacy_ids=("surrender_value_trajectory",)),
    RecipeDefinitionV2(
        recipe_id="policy_loan_utilisation", revision=1, family="insurance",
        primary_objective=SURRENDER,
        business_definition=(
            "Policy loan balance divided by surrender value at the cutoff — the pre-lapse "
            "signal, on its own governed concepts (the old admission closed)."),
        decision_context="pre-lapse loan pressure",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="policy_loan_utilisation", display_label="Policy loan utilization",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="policies missing either side return null",
            empty_population_policy="no loan returns zero utilization",
            zero_denominator_policy="a zero surrender value returns null"),
        operands=(*_policy("policy_snapshot"),
                  measure("loan_balance", "policy_loan_balance", "policy_snapshot"),
                  measure("surrender_value", "surrender_value", "policy_snapshot"),
                  as_of("policy_snapshot")),
        source_grain="policy_snapshot", output_grain="policy",
        temporal=snapshot_window("latest policy snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="in-force policies with both sides at the same as-of",
            excluded="mixed-as-of pairs",
            policy_refs=(INS_CCY,)),
        formula=formula("insurance", "policy_loan_utilisation", "ratio"),
        replaces_legacy_ids=("policy_loan_utilisation",)),

    # ── the claims family: count / paid / reserve / loss ratio, correct additivity ──────────────
    RecipeDefinitionV2(
        recipe_id="claim_count", revision=1, family="insurance",
        primary_objective=CLAIMS_COST,
        business_definition="Claims OPENED in the window (frequency — additive).",
        decision_context="claims frequency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="claim_count", display_label="Claim count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with no claim identity are excluded",
            empty_population_policy="no claims returns zero"),
        operands=(*_policy("claim_event"),
                  dim("claim", "claim_id", "claim_event"),
                  status("claim_state", "claim_status", "claim_event"),
                  event_ts("claim_event")),
        source_grain="claim_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("insurance", "claim_count", "distinct_count"),
        replaces_legacy_ids=("claims_frequency_severity",)),
    RecipeDefinitionV2(
        recipe_id="claim_paid_amount_sum", revision=1, family="insurance",
        primary_objective=CLAIMS_COST,
        business_definition="Amounts PAID on claims in the window (the flow beside reserves).",
        decision_context="claims cash cost",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="claim_paid_amount_sum", display_label="Claims paid",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=INS_CCY,
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="no payments returns zero",
            aggregation_over_entity="sum across claims",
            aggregation_over_time="sum over disjoint windows"),
        operands=(*_policy("claim_event"),
                  dim("claim", "claim_id", "claim_event"),
                  measure("paid", "claim_paid_amount", "claim_event"),
                  event_ts("claim_event")),
        source_grain="claim_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="claim payments",
            excluded="reserve movements (a stock change, not a payment)",
            policy_refs=(INS_CCY,)),
        formula=formula("insurance", "claim_paid_amount_sum", "sum"),
        replaces_legacy_ids=("claims_frequency_severity",)),
    RecipeDefinitionV2(
        recipe_id="loss_ratio", revision=1, family="insurance",
        primary_objective=CLAIMS_COST,
        business_definition=(
            "Incurred claims (paid + reserve movement) divided by EARNED premium over the "
            "window — the earning basis governs the denominator; never additive."),
        decision_context="underwriting performance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="loss_ratio", display_label="Loss ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="claims with no incurred amount are excluded and surface as "
                              "a gap",
            empty_population_policy="no earned premium returns null",
            zero_denominator_policy="zero earned premium returns null"),
        operands=(*_policy("claim_event"),
                  measure("incurred", "claim_paid_amount", "claim_event"),
                  measure("reserve", "claim_reserve", "claim_event"),
                  measure("earned_premium", "premium", "claim_event"),
                  event_ts("claim_event")),
        source_grain="claim_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="incurred amounts against EARNED premium under the earning basis",
            excluded="written or collected premium in the denominator",
            policy_refs=(PREMIUM_BASIS, INS_CCY)),
        formula=formula("insurance", "loss_ratio", "ratio"),
        replaces_legacy_ids=("claims_frequency_severity",)),

    RecipeDefinitionV2(
        recipe_id="reinsurance_recoverable_concentration", revision=1, family="insurance",
        primary_objective=REINSURANCE,
        business_definition=(
            "Concentration of reinsurance recoverables across reinsurers, under the treaty "
            "lifecycle (attachment, limit, recoverable state)."),
        decision_context="reinsurer concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="reinsurance_recoverable_concentration",
            display_label="Recoverable concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="recoverables with no reinsurer identity are excluded",
            empty_population_policy="no recoverables returns null",
            zero_denominator_policy="zero recoverables returns null"),
        operands=(entity("reinsurer", "lei", "recoverable_snapshot"),
                  measure("recoverable", "reinsurance_recoverable", "recoverable_snapshot"),
                  as_of("recoverable_snapshot")),
        source_grain="recoverable_snapshot", output_grain="book",
        temporal=snapshot_window("latest recoverable snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recoverables under effective treaties",
            excluded="lapsed treaties",
            policy_refs=(TREATY, INS_CCY)),
        formula=formula("insurance", "reinsurance_recoverable_concentration", "share"),
        replaces_legacy_ids=("reinsurance_recoverable_concentration",)),
    RecipeDefinitionV2(
        recipe_id="sum_assured_adequacy", revision=1, family="insurance",
        primary_objective=BANCASSURANCE,
        business_definition=(
            "Sum assured divided by the customer's governed income (customer_income — the "
            "old 'no income concept' admission closed) — the needs-analysis ratio."),
        decision_context="coverage adequacy",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sum_assured_adequacy", display_label="Coverage adequacy",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="customers with no governed income return null",
            empty_population_policy="no coverage returns null",
            zero_denominator_policy="zero income returns null"),
        operands=(entity("customer", "customer_id", "policy_snapshot"),
                  measure("sum_assured", "sum_assured", "policy_snapshot"),
                  measure("income", "customer_income", "policy_snapshot"),
                  as_of("policy_snapshot")),
        source_grain="policy_snapshot", output_grain="customer",
        temporal=snapshot_window("latest snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="in-force coverage against governed income",
            excluded="salary-credit proxies with no income evidence",
            policy_refs=(INS_CCY,)),
        formula=formula("insurance", "sum_assured_adequacy", "ratio"),
        replaces_legacy_ids=("sum_assured_adequacy",)),
    RecipeDefinitionV2(
        recipe_id="claims_fraud_typology", revision=1, family="insurance",
        primary_objective=CLAIMS_FRAUD,
        business_definition=(
            "The claim's fraud-typology score — a governed MODEL output, never a "
            "deterministic recipe wearing a score."),
        decision_context="claims triage",
        computation_kind="governed_model_output",
        model_feature_ref="claims_fraud_score",
        output=OutputSpecV2(
            output_id="claims_fraud_typology", display_label="Claims fraud score",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            valid_range="[0, 1]",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(entity("claim", "claim_id", "model_score"),),
        source_grain="model_score", output_grain="claim",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        replaces_legacy_ids=("claims_fraud_typology",)),
    RecipeDefinitionV2(
        recipe_id="mortality_morbidity_loading", revision=1, family="insurance",
        primary_objective=MORTALITY,
        business_definition=(
            "The policy's mortality/morbidity loading — an actuarial MODEL output with its "
            "registered table/model provenance."),
        decision_context="underwriting loading",
        computation_kind="governed_model_output",
        model_feature_ref="mortality_morbidity_loading",
        output=OutputSpecV2(
            output_id="mortality_morbidity_loading", display_label="Mortality loading",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(entity("policy", "policy_id", "model_score"),),
        source_grain="model_score", output_grain="policy",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        replaces_legacy_ids=("mortality_morbidity_loading",)),
    RecipeDefinitionV2(
        recipe_id="bancassurance_cross_hold", revision=1, family="insurance",
        primary_objective=BANCASSURANCE,
        business_definition=(
            "Whether the banking customer holds an ACTIVE insurance product, from "
            "effective-dated holdings (product_holding — the old admission closed)."),
        decision_context="bancassurance penetration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="bancassurance_cross_hold", display_label="Insurance cross-hold",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="holdings with unknown validity are excluded per policy",
            empty_population_policy="no holdings returns false — a real answer"),
        operands=(entity("customer", "customer_id", "product_holding_interval"),
                  dim("holding", "product_holding", "product_holding_interval"),
                  event_ts("product_holding_interval", role="valid_interval",
                           concept="valid_time")),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="insurance holdings effective at the cutoff",
            excluded="lapsed policies",
            policy_refs=("active_state:effective-dated-holdings",)),
        formula=formula("insurance", "bancassurance_cross_hold", "flag"),
        replaces_legacy_ids=("bancassurance_cross_hold",)),
)
