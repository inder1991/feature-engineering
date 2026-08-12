"""BR-19 — the customer pack: 13 relationship recipes + 2 model features.

Customer features are built FROM the validated account/product primitives, never re-derived
from generic columns: every rollup names its accounts and its allocation rule (the acceptance:
a customer feature can explain exactly which accounts and allocation rules contributed), the
household count requires VERIFIED membership, contactability quality computes only under the
privacy-purpose policy, and the churn/uplift scores are governed MODEL outputs with registered
specs — deterministic history on one side, prediction on the other, never blended.

Deliberately NOT re-authored: customer_relationship_tenure (retail's tenure_days IS the
customer-grain tenure), channel diversity (cross-sell's channel_adoption_depth).
"""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_contract import ModelFeatureSpecV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    OperandSpecV2,
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
    policy_input,
    snapshot_window,
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

CHURN = "customer.relationship_attrition.churn"
CLV = "customer.clv"
ENGAGEMENT = "customer.engagement"
CAMPAIGN = "customer.campaign"

JOINT_ALLOC = "allocation:joint-account-full-attribution"
HOUSEHOLD_ALLOC = "allocation:verified-household-membership"
CONTACT_PRIVACY = "privacy_purpose:contactability-quality"
CUST_CCY = "currency_conversion:foundation-base-currency"

#: BR-19's model-governance references: churn and uplift are predictions with governed homes.
CUSTOMER_MODEL_FEATURES: tuple[ModelFeatureSpecV1, ...] = (
    ModelFeatureSpecV1(
        model_feature_id="churn_probability", revision=1,
        model_family="probability_of_event", model_ref="models/customer-churn",
        model_version="", owner="retail-analytics",
        prediction_grain="customer", prediction_timestamp_role="scoring_cutoff",
        training_data_cutoff_policy="features strictly before the label window",
        inference_knowledge_time_policy="inputs as known at the scoring cutoff",
        target_definition="customer closes their primary relationship within the window",
        outcome_window_days=90,
        input_feature_set_revision="pending-first-registered-pack",
        score_type="probability",
        fallback_policy="no score — never a heuristic substitute"),
    ModelFeatureSpecV1(
        model_feature_id="campaign_uplift", revision=1,
        model_family="uplift", model_ref="models/campaign-uplift",
        model_version="", owner="retail-analytics",
        prediction_grain="customer", prediction_timestamp_role="scoring_cutoff",
        training_data_cutoff_policy="treatment/control strictly before the outcome window",
        inference_knowledge_time_policy="inputs as known at the scoring cutoff",
        target_definition="incremental response attributable to treatment, over control",
        outcome_window_days=30,
        input_feature_set_revision="pending-first-registered-pack",
        score_type="score",
        fallback_policy="descriptive response rate — never uplift without a control group"),
)


def _customer(source: str) -> OperandSpecV2:
    return entity("customer", "customer_id", source)


def _count(output_id: str, label: str) -> OutputSpecV2:
    return OutputSpecV2(
        output_id=output_id, display_label=label,
        output_type="integer", additivity="additive", unit_kind="count",
        null_input_policy="rows with unknown state are excluded",
        empty_population_policy="an empty window returns zero")


CUSTOMER_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="active_account_count", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("Count of the customer's accounts in the governed ACTIVE state "
                             "at the cutoff, by account class."),
        decision_context="relationship footprint",
        computation_kind="deterministic_formula",
        output=_count("active_account_count", "Active accounts"),
        operands=(_customer("account_snapshot"),
                  dim("account", "account_id", "account_snapshot"),
                  status("account_state", "account_status", "account_snapshot",
                         policy="active_state:dormancy-definition"),
                  dim("account_class", "account_type", "account_snapshot"),
                  as_of("account_snapshot")),
        source_grain="account_snapshot", output_grain="customer",
        temporal=snapshot_window("account states at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "active_account_count", "distinct_count"),
    ),
    RecipeDefinitionV2(
        recipe_id="active_product_family_count", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("Distinct product FAMILIES among the customer's active "
                            "effective-dated holdings at the cutoff."),
        decision_context="relationship breadth by family",
        computation_kind="deterministic_formula",
        output=_count("active_product_family_count", "Active product families"),
        operands=(_customer("product_holding_interval"),
                  dim("holding", "product_holding", "product_holding_interval"),
                  dim("family", "product_type", "product_holding_interval"),
                  event_ts("product_holding_interval", role="valid_interval",
                           concept="valid_time")),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="holdings effective and active at the cutoff",
            excluded="lapsed and pipeline holdings",
            policy_refs=("active_state:effective-dated-holdings",)),
        formula=formula("customer", "active_product_family_count", "distinct_count"),
    ),
    RecipeDefinitionV2(
        recipe_id="relationship_balance", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("Sum of the customer's account end-of-day balances under the "
                             "governed joint-account allocation — the contributing accounts "
                             "and the allocation rule are the recipe's own operands, so the "
                             "number can always explain itself."),
        decision_context="relationship value (balance side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="relationship_balance", display_label="Relationship balance",
            output_type="numeric", additivity="semi_additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CUST_CCY,
            null_input_policy="accounts with no known snapshot use the latest prior snapshot",
            empty_population_policy="no accounts returns null",
            aggregation_over_entity="sum across the customer's accounts under the named "
                                    "allocation rule — every contributing account listable",
            aggregation_over_time="latest snapshot only"),
        operands=(_customer("account_snapshot"),
                  dim("account", "account_id", "account_snapshot"),
                  measure("balance", "monetary_stock", "account_snapshot"),
                  policy_input("allocation", "ownership_percentage", "account_snapshot",
                               policy=JOINT_ALLOC),
                  as_of("account_snapshot")),
        source_grain="account_snapshot", output_grain="customer",
        temporal=snapshot_window("latest snapshots at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="the customer's accounts under the joint-account allocation",
            excluded="unallocated joint balances double-counted per holder",
            policy_refs=(JOINT_ALLOC, CUST_CCY)),
        formula=formula("customer", "relationship_balance", "snapshot"),
    ),
    RecipeDefinitionV2(
        recipe_id="relationship_revenue", revision=1, family="customer",
        primary_objective=CLV,
        business_definition=("Recognized revenue across the customer's relationship over the "
                             "window under the governed allocation — the REALIZED value the "
                             "CLV projection trains on; the deterministic side of customer "
                             "lifetime value."),
        decision_context="realized relationship value (the CLV foundation)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="relationship_revenue", display_label="Relationship revenue",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CUST_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across accounts under the named allocation rule",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_customer("revenue_event"),
                  dim("account", "account_id", "revenue_event"),
                  measure("revenue", "monetary_flow", "revenue_event",
                          economic_role="recognized_customer_revenue"),
                  policy_input("allocation", "ownership_percentage", "revenue_event",
                               policy=JOINT_ALLOC),
                  event_ts("revenue_event")),
        source_grain="revenue_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recognized revenue under the joint-account allocation",
            excluded="reversed and unrecognized revenue",
            policy_refs=(JOINT_ALLOC, CUST_CCY)),
        formula=formula("customer", "relationship_revenue", "sum"),
    ),
    RecipeDefinitionV2(
        recipe_id="channel_active_day_count", revision=1, family="customer",
        primary_objective=ENGAGEMENT,
        business_definition="Days in the window with activity on the chosen channel.",
        decision_context="channel engagement",
        computation_kind="deterministic_formula",
        output=_count("channel_active_day_count", "Channel-active days"),
        operands=(_customer("transaction"),
                  dim("channel", "channel", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "channel_active_day_count", "count"),
    ),
    RecipeDefinitionV2(
        recipe_id="customer_activity_recency", revision=1, family="customer",
        primary_objective=ENGAGEMENT,
        business_definition=("Days since the customer's last eligible activity across ALL "
                             "their accounts — the freshest account wins (a minimum across "
                             "accounts, never a sum)."),
        decision_context="relationship recency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="customer_activity_recency", display_label="Customer recency",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="accounts with no eligible activity are excluded",
            empty_population_policy="no activity in the window returns null"),
        operands=(_customer("transaction"),
                  dim("account", "account_id", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "customer_activity_recency", "recency"),
    ),
    RecipeDefinitionV2(
        recipe_id="service_interaction_count", revision=1, family="customer",
        primary_objective=ENGAGEMENT,
        business_definition="Service interactions over the window, by recorded outcome.",
        decision_context="service load",
        computation_kind="deterministic_formula",
        output=_count("service_interaction_count", "Service interactions"),
        operands=(_customer("service_event"),
                  dim("outcome", "contact_outcome", "service_event"),
                  event_ts("service_event")),
        source_grain="service_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "service_interaction_count", "count"),
    ),
    RecipeDefinitionV2(
        recipe_id="complaint_count", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition="Complaints raised over the window.",
        decision_context="dissatisfaction volume",
        computation_kind="deterministic_formula",
        output=_count("complaint_count", "Complaints"),
        operands=(_customer("complaint_event"),
                  dim("complaint", "complaint_event", "complaint_event"),
                  event_ts("complaint_event")),
        source_grain="complaint_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "complaint_count", "count"),
    ),
    RecipeDefinitionV2(
        recipe_id="unresolved_complaint_count", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition="Complaints still OPEN at the cutoff.",
        decision_context="unresolved dissatisfaction",
        computation_kind="deterministic_formula",
        output=_count("unresolved_complaint_count", "Open complaints"),
        operands=(_customer("complaint_event"),
                  dim("complaint", "complaint_event", "complaint_event"),
                  status("state", "lifecycle_state", "complaint_event"),
                  event_ts("complaint_event")),
        source_grain="complaint_event", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "unresolved_complaint_count", "count"),
    ),
    RecipeDefinitionV2(
        recipe_id="complaint_resolution_days", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("Average days from complaint raised to resolved, for "
                             "complaints resolved in the window."),
        decision_context="service recovery speed",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="complaint_resolution_days", display_label="Resolution days",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="complaints missing either timestamp are excluded",
            empty_population_policy="no resolutions in the window returns null"),
        operands=(_customer("complaint_event"),
                  dim("complaint", "complaint_event", "complaint_event"),
                  event_ts("complaint_event", role="raised_ts", group="complaint_times"),
                  event_ts("complaint_event", role="resolved_ts", group="complaint_times")),
        source_grain="complaint_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="resolved_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=formula("customer", "complaint_resolution_days", "ratio"),
    ),
    RecipeDefinitionV2(
        recipe_id="verified_household_member_count", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("Count of VERIFIED household members effective at the cutoff — "
                             "membership is an effective-dated verified fact, never a "
                             "surname-and-address guess."),
        decision_context="household context",
        computation_kind="deterministic_formula",
        output=_count("verified_household_member_count", "Household members"),
        operands=(entity("household", "household_id", "household_membership"),
                  dim("member", "customer_id", "household_membership"),
                  event_ts("household_membership", role="valid_interval",
                           concept="valid_time")),
        source_grain="household_membership", output_grain="household",
        temporal=TemporalSpecV2(anchor_kind="effective_interval",
                                business_effective_role="valid_interval",
                                window_unit="none", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="verified memberships effective at the cutoff",
            excluded="unverified or lapsed memberships",
            policy_refs=(HOUSEHOLD_ALLOC,)),
        formula=formula("customer", "verified_household_member_count", "distinct_count"),
    ),
    RecipeDefinitionV2(
        recipe_id="contactability_quality_share", revision=1, family="customer",
        primary_objective=ENGAGEMENT,
        business_definition=("Share of the customer's contact channels verified reachable — "
                             "computed ONLY under the privacy-purpose policy; no permitted "
                             "purpose, no number."),
        decision_context="contactability (privacy-gated)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="contactability_quality_share", display_label="Contactability",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="channels with unknown verification are excluded",
            empty_population_policy="no recorded channels returns null",
            zero_denominator_policy="zero channels returns null"),
        operands=(_customer("contact_channel_snapshot"),
                  status("verification", "lifecycle_state", "contact_channel_snapshot"),
                  policy_input("purpose", "consent_status", "contact_channel_snapshot",
                               policy=CONTACT_PRIVACY),
                  as_of("contact_channel_snapshot")),
        source_grain="contact_channel_snapshot", output_grain="customer",
        temporal=snapshot_window("channel states at the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="contact channels under a permitted purpose",
            excluded="contactability computed without the privacy policy",
            policy_refs=(CONTACT_PRIVACY,)),
        formula=formula("customer", "contactability_quality_share", "share"),
    ),

    # ── the predictions: governed model outputs, never blended with history ─────────────────────
    RecipeDefinitionV2(
        recipe_id="churn_probability", revision=1, family="customer",
        primary_objective=CHURN,
        business_definition=("The probability the customer churns within the outcome window "
                             "— a governed MODEL output, visibly separate from every "
                             "deterministic history feature."),
        decision_context="retention targeting",
        computation_kind="governed_model_output",
        model_feature_ref="churn_probability",
        output=OutputSpecV2(
            output_id="churn_probability", display_label="Churn probability",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            valid_range="[0, 1]",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(_customer("model_score"),),
        source_grain="model_score", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
    ),
    RecipeDefinitionV2(
        recipe_id="campaign_uplift_score", revision=1, family="customer",
        primary_objective=CAMPAIGN,
        business_definition=("Incremental response attributable to treatment — an UPLIFT "
                             "model output requiring treatment AND control records; the "
                             "descriptive response rate is its deterministic sibling."),
        decision_context="treatment-effect targeting",
        computation_kind="governed_model_output",
        model_feature_ref="campaign_uplift",
        output=OutputSpecV2(
            output_id="campaign_uplift_score", display_label="Campaign uplift",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(_customer("model_score"),),
        source_grain="model_score", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
    ),
)
