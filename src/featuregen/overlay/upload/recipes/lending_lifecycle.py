"""BR-21 Priority A — the lending-lifecycle pack: 5 recipes, each answering a named decision.

Every recipe here passes the admission rule's structural half: a named end-user decision (the
decision_context), one atomic output, exact grains, a formula path, declared PIT/knowledge
semantics and policy constraints. Affordability reads governed income against committed
outgoings; seasoning is months-on-book from origination; SME underwriting reads business-inflow
stability; mitigation coverage counts only enforceable mitigants; and recovery cash is
POST-DEFAULT ONLY with the outcome fence the collections pack established.
"""
from __future__ import annotations

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
)
from featuregen.overlay.upload.recipes.collections import POST_DEFAULT_ONLY
from featuregen.overlay.upload.recipes.retail import _WINDOW

AFFORDABILITY = "credit.underwriting.affordability"
SEASONING = "credit.underwriting.seasoning"
SME = "credit.underwriting.sme"
MITIGATION = "credit.monitoring.credit_mitigation"
RECOVERIES = "credit.collections.recoveries"

AFFORDABILITY_POLICY = "threshold:affordability-assessment-basis"
MITIGANT_ENFORCEABILITY = "allocation:mitigant-enforceability"
CREDIT_CCY = "currency_conversion:credit-base-currency-same-asof"
POST_DEFAULT_POP = "eligible_status:post-default-population"


LENDING_LIFECYCLE_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="disposable_income_share", revision=1, family="lending_lifecycle",
        primary_objective=AFFORDABILITY,
        business_definition=("Governed income minus committed outgoings, as a share of "
                             "income, at the assessment cutoff — the affordability decision's "
                             "own number, under the governed assessment basis; income is the "
                             "customer_income concept, never a salary-credit guess."),
        decision_context="can this applicant afford the repayment (underwriting decision)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="disposable_income_share", display_label="Disposable-income share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="applicants missing governed income or outgoings return null "
                              "and surface the gap — affordability is never defaulted",
            empty_population_policy="no assessment inputs returns null",
            zero_denominator_policy="zero income returns null"),
        operands=(entity("customer", "customer_id", "affordability_assessment"),
                  measure("income", "customer_income", "affordability_assessment"),
                  measure("outgoings", "monetary_flow", "affordability_assessment",
                          economic_role="committed_outgoings"),
                  as_of("affordability_assessment")),
        source_grain="affordability_assessment", output_grain="customer",
        temporal=snapshot_window("the assessment's declared inputs at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="governed income and committed outgoings under the assessment basis",
            excluded="income inferred from one salary-like credit",
            policy_refs=(AFFORDABILITY_POLICY, CREDIT_CCY)),
        formula=formula("lending", "disposable_income_share", "ratio")),
    RecipeDefinitionV2(
        recipe_id="months_on_book", revision=1, family="lending_lifecycle",
        primary_objective=SEASONING,
        business_definition=("Months between the loan's origination and the cutoff — the "
                             "vintage/seasoning axis every cohort analysis keys on."),
        decision_context="how seasoned is this exposure (vintage analysis)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="months_on_book", display_label="Months on book",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            unit_policy="months",
            null_input_policy="a null origination date returns null",
            empty_population_policy="no origination record returns null"),
        operands=(entity("facility", "facility_id", "facility_record"),
                  event_ts("facility_record", role="originated",
                           concept="origination_date")),
        source_grain="facility_record", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="as_of", business_effective_role="originated",
                                window_unit="none"),
        readiness="FORMULA_BLOCKED",
        formula=formula("lending", "months_on_book", "recency")),
    RecipeDefinitionV2(
        recipe_id="business_inflow_stability", revision=1, family="lending_lifecycle",
        primary_objective=SME,
        business_definition=("Dispersion of the SME's monthly business inflows relative to "
                             "their mean — revenue stability, the SME underwriting decision's "
                             "cash-flow evidence."),
        decision_context="is this SME's revenue stable enough to lend against",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="business_inflow_stability", display_label="Inflow stability",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="months with no eligible inflows count zero, never dropped",
            empty_population_policy="fewer than three months of history returns null — "
                                    "stability needs a baseline"),
        operands=(entity("customer", "customer_id", "transaction"),
                  measure("inflow", "monetary_flow", "transaction"),
                  dim("direction", "debit_credit_indicator", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted business credits under the eligible-status policy",
            excluded="intra-company transfers dressed as revenue; reversed credits",
            policy_refs=("eligible_status:foundation-posted-events",
                         "reversal_correction:foundation-flag-or-code", CREDIT_CCY)),
        formula=formula("lending", "business_inflow_stability", "dispersion")),
    RecipeDefinitionV2(
        recipe_id="mitigation_coverage_share", revision=1, family="lending_lifecycle",
        primary_objective=MITIGATION,
        business_definition=("Enforceable mitigant value (collateral after haircut plus "
                             "unexpired enforceable guarantees) as a share of the obligor's "
                             "exposure, both sides at the same as-of — mitigation the "
                             "monitoring decision can rely on, never face-value comfort."),
        decision_context="how much of this exposure is actually mitigated",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="mitigation_coverage_share", display_label="Mitigation coverage",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="mitigants with unknown enforceability are excluded from the "
                              "numerator, exposure kept in the denominator",
            empty_population_policy="no exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("obligor", "obligor_id", "mitigant_snapshot"),
                  measure("collateral", "collateral_value", "mitigant_snapshot",
                          economic_role="collateral_valuation"),
                  measure("guarantees", "monetary_stock", "mitigant_snapshot"),
                  measure("exposure", "ead", "mitigant_snapshot",
                          economic_role="drawn_credit_exposure"),
                  as_of("mitigant_snapshot")),
        source_grain="mitigant_snapshot", output_grain="obligor",
        temporal=snapshot_window("mitigants effective and enforceable at the as-of"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="enforceable mitigants after haircut, at one as-of",
            excluded="expired guarantees; unenforceable agreements; face values without "
                     "haircut",
            policy_refs=(MITIGANT_ENFORCEABILITY,
                         "allocation:collateral-allocation-haircut-seniority", CREDIT_CCY)),
        formula=formula("lending", "mitigation_coverage_share", "ratio")),
    RecipeDefinitionV2(
        recipe_id="recovery_cash_collected", revision=1, family="lending_lifecycle",
        primary_objective=RECOVERIES,
        business_definition=("Post-default recovery cash collected in the window, in base "
                             "currency — the recoveries decision's flow, POST-DEFAULT ONLY; "
                             "the rate against the frozen defaulted balance is its sibling "
                             "in the collections pack."),
        decision_context="how much is this defaulted book actually returning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="recovery_cash_collected", display_label="Recovery cash",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units",
            currency_policy="currency_conversion:collections-recovery-currency",
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="no recoveries returns zero — a real answer post-default",
            aggregation_over_entity="sum across defaulted contracts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("facility", "facility_id", "recovery_event"),
                  measure("recovery", "recovery_amount", "recovery_event"),
                  event_ts("recovery_event")),
        source_grain="recovery_event", output_grain="facility",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        eligibility=EligibilitySpecV2(
            included="post-default recovery flows linked to the default",
            excluded="pre-default payments",
            policy_refs=(POST_DEFAULT_POP,
                         "currency_conversion:collections-recovery-currency")),
        formula=formula("lending", "recovery_cash_collected", "sum")),
)
