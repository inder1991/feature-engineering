"""BR-19 — the RBWM pack: 9 retail-product behaviour recipes.

The primacy-loss leaf gets its reviewed primaries (salary anchoring ceasing, operating-balance
share falling); card revolve, mortgage prepayment and early settlement are lifecycle-correct
behaviour reads; activation and digital adoption are engagement facts; and the vulnerability
indicator computes ONLY under its privacy/purpose policy — a special-category fact is policy
first, number second.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes._shared import (
    dim,
    entity,
    event_ts,
    event_window,
    formula,
    measure,
    policy_input,
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

PRIMACY = "customer.relationship_attrition.primacy_loss"
CHURN = "customer.relationship_attrition.churn"
ENGAGEMENT = "customer.engagement"
EARLY_WARNING = "credit.early_warning"

SALARY_DESIGNATION = "active_state:primary-salary-designation"
VULNERABILITY_PRIVACY = "privacy_purpose:vulnerability-assessment"
RBWM_CCY = "currency_conversion:foundation-base-currency"


def _cust(source: str):
    return entity("customer", "customer_id", source)


RBWM_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="salary_anchoring_ceased_flag", revision=1, family="rbwm",
        primary_objective=PRIMACY,
        business_definition=("Whether an account holding the governed primary-salary "
                             "designation stopped receiving salary credits inside the window "
                             "while remaining open — the primacy-loss event, read against "
                             "the designation, never inferred from one missed month."),
        decision_context="primacy-loss detection",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="salary_anchoring_ceased_flag", display_label="Salary anchoring ceased",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="accounts with no designation record return null",
            empty_population_policy="no designated account returns null — nothing anchored, "
                                    "nothing to lose"),
        operands=(_cust("transaction"),
                  dim("account", "account_id", "transaction"),
                  policy_input("designation", "account_status", "transaction",
                               policy=SALARY_DESIGNATION),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="salary credits on the designated account, cadence-compared across the "
                     "window halves",
            excluded="cessation inferred from one missed cadence period",
            policy_refs=(SALARY_DESIGNATION,)),
        formula=formula("rbwm", "salary_anchoring_ceased_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="operating_balance_share_trend", revision=1, family="rbwm",
        primary_objective=PRIMACY,
        business_definition=("OLS slope of the CASA operating balance's share of the "
                             "customer's total relationship balance — the operating share "
                             "falling while total holds is money moving its HOME elsewhere."),
        decision_context="primacy erosion",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="operating_balance_share_trend", display_label="Operating-share trend",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            unit_policy="share points per day",
            null_input_policy="days missing either side are excluded from the fit",
            empty_population_policy="fewer than two valid days returns null"),
        operands=(_cust("account_snapshot"),
                  dim("account", "account_id", "account_snapshot"),
                  measure("balance", "monetary_stock", "account_snapshot"),
                  dim("account_class", "account_type", "account_snapshot"),
                  event_ts("account_snapshot", role="as_of_date", concept="as_of_date")),
        source_grain="account_snapshot", output_grain="customer",
        temporal=snapshot_window("latest snapshots at each day's cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="CASA balances against total relationship balance at the same as-of",
            excluded="mixed-as-of shares",
            policy_refs=(RBWM_CCY,)),
        formula=formula("rbwm", "operating_balance_share_trend", "slope")),
    RecipeDefinitionV2(
        recipe_id="card_revolve_share", revision=1, family="rbwm",
        primary_objective=EARLY_WARNING,
        business_definition=("Billing cycles in the window where a balance revolved past the "
                             "due date, as a share of cycles — read from the schedule "
                             "(due date, minimum due) and payments."),
        decision_context="revolve behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="card_revolve_share", display_label="Revolve share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="cycles missing schedule fields are excluded and surface as "
                              "a gap",
            empty_population_policy="no billing cycles returns null",
            zero_denominator_policy="zero cycles returns null"),
        operands=(_cust("billing_period"),
                  event_ts("billing_period", role="due_date", concept="due_date"),
                  measure("minimum_due", "minimum_due_amount", "billing_period"),
                  measure("paid", "monetary_flow", "billing_period",
                          economic_role="loan_repayment")),
        source_grain="billing_period", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("rbwm", "card_revolve_share", "share")),
    RecipeDefinitionV2(
        recipe_id="mortgage_prepayment_share", revision=1, family="rbwm",
        primary_objective=CHURN,
        business_definition=("Principal paid above schedule as a share of scheduled "
                             "principal — prepayment behaviour, the refinance precursor, "
                             "read from the schedule and the governed allocation."),
        decision_context="prepayment/refinance behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="mortgage_prepayment_share", display_label="Prepayment share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="periods missing schedule fields are excluded and surface as "
                              "a gap",
            empty_population_policy="no scheduled principal returns null",
            zero_denominator_policy="zero scheduled principal returns null"),
        operands=(_cust("installment_schedule"),
                  event_ts("installment_schedule", role="due_date", concept="due_date"),
                  measure("scheduled", "scheduled_amount", "installment_schedule"),
                  measure("paid", "monetary_flow", "installment_schedule",
                          economic_role="loan_repayment"),
                  policy_input("allocation", "payment_allocation", "installment_schedule",
                               policy="allocation:payment-application-order")),
        source_grain="installment_schedule", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled principal and allocated payments",
            excluded="fee allocations counted as principal",
            policy_refs=("allocation:payment-application-order", RBWM_CCY)),
        formula=formula("rbwm", "mortgage_prepayment_share", "share")),
    RecipeDefinitionV2(
        recipe_id="loan_early_settlement_flag", revision=1, family="rbwm",
        primary_objective=CHURN,
        business_definition=("Whether a loan settled in FULL before its contractual maturity "
                             "inside the window — the closure event against the contract."),
        decision_context="early-settlement behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="loan_early_settlement_flag", display_label="Early settlement",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="closures with no maturity to compare are excluded",
            empty_population_policy="no closures returns false — a real answer"),
        operands=(_cust("closure_event"),
                  event_ts("closure_event"),
                  dim("contractual_maturity", "maturity_date", "closure_event")),
        source_grain="closure_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("rbwm", "loan_early_settlement_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="first_90_day_active_days", revision=1, family="rbwm",
        primary_objective=ENGAGEMENT,
        business_definition=("Active days in the first 90 days after origination — the "
                             "activation window anchored on the ORIGINATION date, not the "
                             "cutoff."),
        decision_context="activation measurement",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="first_90_day_active_days", display_label="First-90-day active days",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="a null origination date returns null",
            empty_population_policy="no activity in the activation window returns zero"),
        operands=(_cust("transaction"),
                  dim("origination", "origination_date", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("rbwm", "first_90_day_active_days", "count")),
    RecipeDefinitionV2(
        recipe_id="digital_adoption_share", revision=1, family="rbwm",
        primary_objective=ENGAGEMENT,
        business_definition=("Digital-channel activity as a share of all channel activity "
                             "over the window."),
        decision_context="digital migration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="digital_adoption_share", display_label="Digital adoption",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown channel are excluded from both sides",
            empty_population_policy="no channel activity returns null",
            zero_denominator_policy="zero activity returns null"),
        operands=(_cust("transaction"),
                  dim("channel", "channel", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("rbwm", "digital_adoption_share", "share")),
    RecipeDefinitionV2(
        recipe_id="service_failure_count", revision=1, family="rbwm",
        primary_objective=CHURN,
        business_definition=("Service interactions whose outcome was a FAILURE over the "
                             "window — the churn-precursor service signal."),
        decision_context="service failure volume",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="service_failure_count", display_label="Service failures",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="interactions with no recorded outcome are excluded",
            empty_population_policy="no interactions returns zero"),
        operands=(_cust("service_event"),
                  status("outcome", "contact_outcome", "service_event"),
                  event_ts("service_event")),
        source_grain="service_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("rbwm", "service_failure_count", "count")),
    RecipeDefinitionV2(
        recipe_id="vulnerability_indicator_flag", revision=1, family="rbwm",
        primary_objective=CHURN,
        business_definition=("Whether a REVIEWED vulnerability indicator is recorded for the "
                             "customer — a special-category fact computed ONLY under the "
                             "governed privacy/purpose policy, never derived from behaviour."),
        decision_context="vulnerability (privacy- and purpose-gated)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="vulnerability_indicator_flag", display_label="Vulnerability recorded",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="customers outside the permitted purpose return null — no "
                              "purpose, no number",
            empty_population_policy="no policy coverage returns null"),
        operands=(_cust("customer_flag_snapshot"),
                  status("indicator", "vulnerability_flag", "customer_flag_snapshot"),
                  policy_input("purpose", "consent_status", "customer_flag_snapshot",
                               policy=VULNERABILITY_PRIVACY)),
        source_grain="customer_flag_snapshot", output_grain="customer",
        temporal=snapshot_window("the recorded indicator at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recorded indicators under a permitted purpose",
            excluded="vulnerability inferred from behaviour; use outside the policy",
            policy_refs=(VULNERABILITY_PRIVACY,)),
        formula=formula("rbwm", "vulnerability_indicator_flag", "flag")),
)
