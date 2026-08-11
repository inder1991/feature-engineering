"""BR-11 — the cross-sell pack: 10 legacy templates → 12 atomic V2 recipes + 2 model features.

The corrections, structural: whitespace requires an effective-dated ELIGIBLE product universe;
next-best-product propensity and CLV projection are MODEL outputs (registered ModelFeatureSpecs,
never deterministic formulas wearing prose); descriptive campaign response is separated from
predictive uplift and requires treatment evidence; internal penetration is NAMED internal
penetration — share of wallet without an external/modelled denominator stays conceptual;
household rollups require verified effective-dated membership and a governed allocation; and
tenure-only upsell scoring survives only as a conceptual pattern until a reviewed suitability
policy exists.
"""
from __future__ import annotations

from featuregen.overlay.upload.model_feature_contract import ModelFeatureSpecV1
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.retail import (
    _WINDOW,
    ACTIVE_HOLDING,
    BASE_CCY,
    ELIGIBLE_POSTED,
    JOINT_ALLOC,
    REVERSALS,
    _event_ts,
    _status,
)

NBA = "customer.cross_sell.next_best_action"
WHITESPACE = "customer.cross_sell.whitespace"
WALLET = "customer.cross_sell.share_of_wallet"
CAMPAIGN = "customer.campaign"
SEGMENTATION = "customer.segmentation"
CHURN = "customer.relationship_attrition.churn"

ELIGIBLE_UNIVERSE = "active_state:eligible-product-universe"
HOUSEHOLD_ALLOC = "allocation:verified-household-membership"
SUITABILITY = "privacy_purpose:product-suitability-and-consent"


def _customer(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _holding_operands() -> tuple[OperandSpecV2, ...]:
    return (
        _customer("product_holding_interval"),
        OperandSpecV2(role="holding", concept="product_holding", operand_class="dimension",
                      allowed_source_grains=("product_holding_interval",)),
        OperandSpecV2(role="valid_interval", concept="valid_time",
                      operand_class="as_of_timestamp",
                      allowed_source_grains=("product_holding_interval",)),
    )


_EFFECTIVE_HOLDINGS = TemporalSpecV2(
    anchor_kind="effective_interval", business_effective_role="valid_interval",
    window_unit="none", cutoff_inclusivity="inclusive")


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"cross_sell:{output_id}",
                              result_class=result_class)


def _event_window() -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


#: BR-7A reclassifications — the two legacy recipes that were predictions wearing recipe prose.
#: Registered UNVERSIONED (model_version="") — MODEL_SPEC_BLOCKED, honestly: no model is
#: registered yet; the spec exists so the prediction has a governed home, not to claim readiness.
CROSS_SELL_MODEL_FEATURES: tuple[ModelFeatureSpecV1, ...] = (
    ModelFeatureSpecV1(
        model_feature_id="nbp_propensity", revision=1,
        model_family="propensity", model_ref="models/next-best-product",
        model_version="", owner="retail-analytics",
        prediction_grain="customer", prediction_timestamp_role="scoring_cutoff",
        training_data_cutoff_policy="features strictly before the label window",
        inference_knowledge_time_policy="inputs as known at the scoring cutoff",
        target_definition="customer takes the recommended product within the outcome window",
        outcome_window_days=90,
        input_feature_set_revision="pending-first-registered-pack",
        permitted_purposes=("cross_sell_targeting",),
        score_type="probability",
        fallback_policy="no score — never a heuristic substitute"),
    ModelFeatureSpecV1(
        model_feature_id="clv_projection", revision=1,
        model_family="projection", model_ref="models/customer-lifetime-value",
        model_version="", owner="retail-analytics",
        prediction_grain="customer", prediction_timestamp_role="scoring_cutoff",
        training_data_cutoff_policy="features strictly before the projection horizon",
        inference_knowledge_time_policy="inputs as known at the scoring cutoff",
        target_definition="net customer revenue over the projection horizon",
        outcome_window_days=365,
        input_feature_set_revision="pending-first-registered-pack",
        permitted_purposes=("relationship_planning",),
        score_type="amount",
        fallback_policy="historical_product_revenue is the deterministic fallback view"),
)


CROSS_SELL_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="channel_adoption_depth", revision=1, family="cross_sell",
        primary_objective=NBA, supporting_objectives=(CHURN,),
        business_definition=(
            "Distinct count of servicing channels the customer used through eligible posted "
            "activity over the window."),
        decision_context="digital engagement depth",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="channel_adoption_depth", display_label="Channel adoption depth",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown channel are excluded per policy",
            empty_population_policy="no eligible activity returns zero"),
        operands=(_customer("transaction"),
                  OperandSpecV2(role="channel", concept="channel", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts(), _status()),
        source_grain="transaction", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="eligible posted customer-initiated activity",
            excluded="failed, reversed and technical events",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS)),
        formula=_formula("channel_adoption_depth", "distinct_count"),
        replaces_legacy_ids=("channel_adoption_depth",)),

    RecipeDefinitionV2(
        recipe_id="whitespace_product_gap", revision=1, family="cross_sell",
        primary_objective=WHITESPACE,
        business_definition=(
            "Count of products in the customer's effective-dated ELIGIBLE product universe not "
            "held as an active holding at the cutoff — a gap against what the customer COULD "
            "hold, never against a segment stereotype alone."),
        decision_context="whitespace targeting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="whitespace_product_gap", display_label="Whitespace product gap",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="products with unknown eligibility are excluded, never assumed "
                              "eligible",
            empty_population_policy="an empty eligible universe returns null — no universe, "
                                    "no gap"),
        operands=(*_holding_operands(),
                  OperandSpecV2(role="eligible_universe", concept="product_id",
                                operand_class="policy_input",
                                allowed_source_grains=("eligible_product_universe",),
                                status_policy_ref=ELIGIBLE_UNIVERSE)),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=_EFFECTIVE_HOLDINGS,
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="products effective-dated eligible for this customer at the cutoff",
            excluded="products the customer is ineligible for; holdings outside their "
                     "effective interval",
            policy_refs=(ACTIVE_HOLDING, ELIGIBLE_UNIVERSE)),
        formula=_formula("whitespace_product_gap", "count"),
        replaces_legacy_ids=("product_gap_whitespace",)),

    RecipeDefinitionV2(
        recipe_id="next_best_product_propensity", revision=1, family="cross_sell",
        primary_objective=NBA,
        business_definition=(
            "The probability the customer takes a recommended product — a governed MODEL "
            "prediction, visibly separate from every deterministic feature on this page."),
        decision_context="next-best-action ranking",
        computation_kind="governed_model_output",
        model_feature_ref="nbp_propensity",
        output=OutputSpecV2(
            output_id="next_best_product_propensity",
            display_label="Next-best-product propensity",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            valid_range="[0, 1]",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(_customer("model_score"),),
        source_grain="model_score", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        eligibility=EligibilitySpecV2(
            included="customers inside the model's declared population",
            excluded="customers failing suitability, consent or exclusion policies",
            policy_refs=(SUITABILITY,)),
        replaces_legacy_ids=("next_best_product_propensity",)),

    RecipeDefinitionV2(
        recipe_id="product_breadth_growth", revision=1, family="cross_sell",
        primary_objective=NBA, supporting_objectives=(CHURN,),
        business_definition=(
            "Active product breadth at the cutoff minus active breadth one window earlier, "
            "both read from effective-dated holdings."),
        decision_context="relationship deepening trend",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="product_breadth_growth", display_label="Product breadth growth",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="holdings with unknown validity are excluded per policy",
            empty_population_policy="no holdings at either cutoff returns zero minus zero"),
        operands=_holding_operands(),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=TemporalSpecV2(
            anchor_kind="effective_interval", business_effective_role="valid_interval",
            window_basis="trailing", window_unit="days", window_parameter="window",
            cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="holdings active under the governed active-state policy",
            excluded="closed, lapsed and pipeline holdings",
            policy_refs=(ACTIVE_HOLDING,)),
        formula=_formula("product_breadth_growth", "count"),
        replaces_legacy_ids=("relationship_deepening_breadth",)),

    RecipeDefinitionV2(
        recipe_id="campaign_response_rate", revision=1, family="cross_sell",
        primary_objective=CAMPAIGN,
        business_definition=(
            "Responses divided by campaign TREATMENT events (contact or impression) for the "
            "customer over the window — descriptive response, requiring recorded treatment; "
            "never predictive uplift, which needs a control group and a model."),
        decision_context="campaign effectiveness reporting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="campaign_response_rate", display_label="Campaign response rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="events with unknown campaign linkage are excluded per policy",
            empty_population_policy="no treatment events returns null — a response without "
                                    "treatment is attribution, not a rate",
            zero_denominator_policy="zero treatments returns null, never infinity"),
        operands=(_customer("campaign_event"),
                  OperandSpecV2(role="campaign", concept="campaign_id",
                                operand_class="dimension",
                                allowed_source_grains=("campaign_event",)),
                  OperandSpecV2(role="event_kind", concept="event_type",
                                operand_class="status",
                                allowed_source_grains=("campaign_event",)),
                  _event_ts("campaign_event")),
        source_grain="campaign_event", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="treatment (contact/impression) and response events with campaign linkage",
            excluded="responses with no prior recorded treatment in the window"),
        formula=_formula("campaign_response_rate", "ratio"),
        replaces_legacy_ids=("campaign_response_recency",)),
    RecipeDefinitionV2(
        recipe_id="campaign_response_recency_days", revision=1, family="cross_sell",
        primary_objective=CAMPAIGN,
        business_definition="Days since the customer's last campaign response at the cutoff.",
        decision_context="engagement recency for contact planning",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="campaign_response_recency_days",
            display_label="Campaign response recency",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="events with unknown campaign linkage are excluded per policy",
            empty_population_policy="no responses in the window returns null"),
        operands=(_customer("campaign_event"),
                  OperandSpecV2(role="event_kind", concept="event_type",
                                operand_class="status",
                                allowed_source_grains=("campaign_event",)),
                  _event_ts("campaign_event")),
        source_grain="campaign_event", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="response events with campaign linkage",
            excluded="treatments without responses (they are the denominator of the rate, "
                     "not a recency)"),
        formula=_formula("campaign_response_recency_days", "recency"),
        replaces_legacy_ids=("campaign_response_recency",)),

    RecipeDefinitionV2(
        recipe_id="historical_product_revenue", revision=1, family="cross_sell",
        primary_objective=NBA, supporting_objectives=(WALLET,),
        business_definition=(
            "Sum of recognized customer revenue for ONE product line over the window, in base "
            "currency — the deterministic history CLV models train on, never a projection."),
        decision_context="realized relationship value",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="historical_product_revenue", display_label="Historical product revenue",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across a customer's accounts under " + JOINT_ALLOC,
            aggregation_over_time="sum over disjoint windows"),
        operands=(_customer("revenue_event"),
                  OperandSpecV2(role="revenue", concept="monetary_flow", operand_class="measure",
                                allowed_source_grains=("revenue_event",),
                                unit_expectation="monetary",
                                economic_role="recognized_customer_revenue"),
                  OperandSpecV2(role="product", concept="product_id", operand_class="dimension",
                                allowed_source_grains=("revenue_event",)),
                  _event_ts("revenue_event")),
        source_grain="revenue_event", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recognized revenue events attributable to the customer and product",
            excluded="reversed and unrecognized revenue",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, BASE_CCY, JOINT_ALLOC)),
        formula=_formula("historical_product_revenue", "sum"),
        replaces_legacy_ids=("clv_revenue_trajectory",)),
    RecipeDefinitionV2(
        recipe_id="clv_projected_value", revision=1, family="cross_sell",
        primary_objective=NBA,
        business_definition=(
            "Projected customer lifetime value over the horizon — a governed MODEL forecast, "
            "not a formula; historical_product_revenue is its deterministic sibling."),
        decision_context="relationship planning",
        computation_kind="governed_model_output",
        model_feature_ref="clv_projection",
        output=OutputSpecV2(
            output_id="clv_projected_value", display_label="Projected lifetime value",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — model output",
            empty_population_policy="not applicable — model output"),
        operands=(_customer("model_score"),),
        source_grain="model_score", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        replaces_legacy_ids=("clv_revenue_trajectory",)),

    RecipeDefinitionV2(
        recipe_id="internal_penetration_share", revision=1, family="cross_sell",
        primary_objective=WALLET,
        business_definition=(
            "The customer's active holdings as a share of their effective-dated ELIGIBLE "
            "product universe — named for what it is: INTERNAL penetration of the bank's own "
            "shelf, never share of a wallet nobody measured."),
        decision_context="internal penetration reporting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="internal_penetration_share", display_label="Internal penetration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="products with unknown eligibility are excluded, never assumed",
            empty_population_policy="an empty eligible universe returns null",
            zero_denominator_policy="an empty eligible universe returns null, never infinity"),
        operands=(*_holding_operands(),
                  OperandSpecV2(role="eligible_universe", concept="product_id",
                                operand_class="policy_input",
                                allowed_source_grains=("eligible_product_universe",),
                                status_policy_ref=ELIGIBLE_UNIVERSE)),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=_EFFECTIVE_HOLDINGS,
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="active holdings and the effective-dated eligible universe",
            excluded="ineligible products; holdings outside their effective interval",
            policy_refs=(ACTIVE_HOLDING, ELIGIBLE_UNIVERSE)),
        formula=_formula("internal_penetration_share", "share"),
        replaces_legacy_ids=("share_of_wallet_growth",)),
    RecipeDefinitionV2(
        recipe_id="share_of_wallet", revision=1, family="cross_sell",
        primary_objective=WALLET,
        business_definition=(
            "The bank's share of the customer's TOTAL financial wallet — held value over total "
            "wallet value across all providers."),
        decision_context="true wallet-share strategy",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "The denominator is the customer's total wallet across ALL providers, and no "
            "external or modelled total-wallet estimate exists in any governed catalog — "
            "internal_penetration_share is the honest computable neighbour, and presenting it "
            "as share of wallet would overstate primacy for every multi-banked customer."),
        output=OutputSpecV2(
            output_id="share_of_wallet", display_label="Share of wallet",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern",
            zero_denominator_policy="not applicable — conceptual pattern"),
        operands=(_customer("product_holding_interval"),),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=_EFFECTIVE_HOLDINGS,
        readiness="CONCEPTUAL_ONLY",
        replaces_legacy_ids=("share_of_wallet_growth",)),

    RecipeDefinitionV2(
        recipe_id="segment_relative_penetration", revision=1, family="cross_sell",
        primary_objective=SEGMENTATION, supporting_objectives=(NBA,),
        business_definition=(
            "The customer's active product breadth divided by their segment's median active "
            "breadth at the same cutoff — under-penetration relative to peers, from "
            "effective-dated holdings on both sides."),
        decision_context="segment-relative targeting",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="segment_relative_penetration",
            display_label="Segment-relative penetration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="customers with no segment assignment return null",
            empty_population_policy="a segment with no members' holdings returns null",
            zero_denominator_policy="a zero segment median returns null, never infinity"),
        operands=(*_holding_operands(),
                  OperandSpecV2(role="segment", concept="segment", operand_class="dimension",
                                allowed_source_grains=("product_holding_interval",))),
        source_grain="product_holding_interval", output_grain="customer",
        temporal=_EFFECTIVE_HOLDINGS,
        readiness="FORMULA_BLOCKED",
        eligibility=EligibilitySpecV2(
            included="active holdings under the governed active-state policy, both sides "
                     "of the ratio",
            excluded="closed, lapsed and pipeline holdings",
            policy_refs=(ACTIVE_HOLDING,)),
        formula=_formula("segment_relative_penetration", "ratio"),
        replaces_legacy_ids=("segment_relative_penetration",)),

    RecipeDefinitionV2(
        recipe_id="household_relationship_value", revision=1, family="cross_sell",
        primary_objective=NBA,
        business_definition=(
            "Sum of recognized revenue across VERIFIED household members over the window, in "
            "base currency, under the governed household allocation — membership is an "
            "effective-dated verified fact, never a surname-and-address guess."),
        decision_context="household-level relationship value",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="household_relationship_value",
            display_label="Household relationship value",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=BASE_CCY,
            null_input_policy="null amounts are excluded per the reviewed source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum across verified members under " + HOUSEHOLD_ALLOC,
            aggregation_over_time="sum over disjoint windows"),
        operands=(
            OperandSpecV2(role="household", concept="household_id", operand_class="entity_key",
                          allowed_source_grains=("revenue_event",),
                          relationship_requirement="effective-dated VERIFIED household "
                                                   "membership for every included customer"),
            OperandSpecV2(role="revenue", concept="monetary_flow", operand_class="measure",
                          allowed_source_grains=("revenue_event",),
                          unit_expectation="monetary",
                          economic_role="recognized_customer_revenue"),
            _event_ts("revenue_event"),
        ),
        source_grain="revenue_event", output_grain="household",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="revenue events of verified household members inside their membership "
                     "interval",
            excluded="unverified or lapsed memberships",
            policy_refs=(ELIGIBLE_POSTED, REVERSALS, BASE_CCY, HOUSEHOLD_ALLOC)),
        formula=_formula("household_relationship_value", "sum"),
        replaces_legacy_ids=("household_relationship_value",)),

    RecipeDefinitionV2(
        recipe_id="tenure_upsell_readiness", revision=1, family="cross_sell",
        primary_objective=NBA,
        business_definition=(
            "Whether relationship tenure indicates readiness for an upsell conversation."),
        decision_context="contact planning",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "Tenure alone does not make an upsell suitable; without a reviewed suitability "
            "policy attached (eligibility, affordability, consent), a tenure-scored readiness "
            "number is a sales heuristic presented as analysis. tenure_days remains the "
            "deterministic atom."),
        output=OutputSpecV2(
            output_id="tenure_upsell_readiness", display_label="Tenure upsell readiness",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_customer("customer"),),
        source_grain="customer", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="as_of", window_unit="none"),
        readiness="CONCEPTUAL_ONLY",
        replaces_legacy_ids=("tenure_upsell_readiness",)),
)
