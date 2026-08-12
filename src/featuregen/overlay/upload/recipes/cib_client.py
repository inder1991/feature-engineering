"""BR-20 — the CIB client pack: profitability with stated methodologies, KYC review state.

The CIB grain vocabulary is EXPLICIT (:data:`CIB_GRAINS`, pinned by test): legal party, obligor,
client group, account, facility, instrument and pool are distinct output grains, never blurred.
Profitability states its methodology or refuses: service cost is a declared economic role;
economic profit / RAROC and the revenue wallet stay CONCEPTUAL until their capital/cost policy
and external denominator are governed — a profitability number with an ungoverned methodology
is an argument, not a feature. KYC periodic-review state gives the kyc leaf its first reviewed
primary.

Already existing as atoms (not re-authored): operating balance average/volatility (account
foundation), product revenue by line (historical_product_revenue), net relationship revenue
(relationship_revenue), penetration (internal_penetration_share), limit utilization/excess
(facility_utilisation_headroom, excess-limit family).
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
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

OBLIGOR = "credit.monitoring.obligor"
KYC = "aml_cft.kyc"
CLV = "customer.clv"

CIB_CCY = "currency_conversion:cib-base-currency"
COST_ALLOCATION = "allocation:service-cost-allocation"

#: The explicit CIB grain vocabulary — every BR-20 recipe's output grain is one of these.
CIB_GRAINS = ("legal_party", "obligor", "legal_group", "account", "facility",
              "instrument", "pool", "client")


CIB_CLIENT_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="direct_service_cost", revision=1, family="cib_client",
        primary_objective=CLV,
        business_definition=("Direct service cost booked to the client over the window under "
                             "the governed cost-allocation policy — the cost side of client "
                             "profitability, its allocation methodology an operand."),
        decision_context="client profitability (cost side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="direct_service_cost", display_label="Direct service cost",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CIB_CCY,
            null_input_policy="cost rows with no allocation basis are excluded and surface "
                              "as a gap",
            empty_population_policy="no booked cost returns zero",
            aggregation_over_entity="sum under the named allocation policy",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("client", "customer_id", "cost_event"),
                  measure("cost", "monetary_flow", "cost_event",
                          economic_role="allocated_service_cost"),
                  policy_input("allocation", "ownership_percentage", "cost_event",
                               policy=COST_ALLOCATION),
                  event_ts("cost_event")),
        source_grain="cost_event", output_grain="client",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="costs booked under the governed allocation",
            excluded="unallocated overheads guessed onto clients",
            policy_refs=(COST_ALLOCATION, CIB_CCY)),
        formula=formula("cib_client", "direct_service_cost", "sum")),
    RecipeDefinitionV2(
        recipe_id="raroc_pattern", revision=1, family="cib_client",
        primary_objective=CLV,
        business_definition=("Risk-adjusted return on capital for the client relationship."),
        decision_context="economic profitability",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "RAROC needs a GOVERNED capital-attribution and cost-allocation methodology — "
            "the plan keeps it blocked until those are reviewed policy; until then any RAROC "
            "is a methodology argument presented as a number."),
        output=OutputSpecV2(
            output_id="raroc_pattern", display_label="RAROC",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(entity("client", "customer_id", "cost_event"),),
        source_grain="cost_event", output_grain="client",
        temporal=event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,)),
    RecipeDefinitionV2(
        recipe_id="revenue_wallet_share_pattern", revision=1, family="cib_client",
        primary_objective=CLV,
        business_definition=("The bank's share of the client's total banking-revenue "
                             "wallet."),
        decision_context="wallet strategy",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "The denominator is the client's spend across ALL banks — no governed external or "
            "estimated wallet denominator exists; relationship_revenue is the honest "
            "computable neighbour, and presenting internal revenue as wallet share would "
            "overstate primacy for every multi-banked client."),
        output=OutputSpecV2(
            output_id="revenue_wallet_share_pattern", display_label="Revenue wallet share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern",
            zero_denominator_policy="not applicable — conceptual pattern"),
        operands=(entity("client", "customer_id", "revenue_event"),),
        source_grain="revenue_event", output_grain="client",
        temporal=event_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,)),
    RecipeDefinitionV2(
        recipe_id="kyc_periodic_review_overdue_flag", revision=1, family="cib_client",
        primary_objective=KYC,
        business_definition=("Whether the client's KYC/CDD periodic review is past its "
                             "refresh due date at the cutoff — onboarding completion and "
                             "review currency as a governed state, never a guess."),
        decision_context="KYC review currency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="kyc_periodic_review_overdue_flag", display_label="KYC review overdue",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="clients with no review schedule return null — unknown, never "
                              "assumed current",
            empty_population_policy="no KYC schedule coverage returns null"),
        operands=(entity("client", "customer_id", "kyc_review_schedule"),
                  dim("kyc_state", "kyc_document", "kyc_review_schedule"),
                  event_ts("kyc_review_schedule", role="refresh_due", concept="due_date")),
        source_grain="kyc_review_schedule", output_grain="client",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="refresh_due",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled reviews and their refresh due dates",
            excluded="review currency assumed with no schedule"),
        formula=formula("cib_client", "kyc_periodic_review_overdue_flag", "flag")),
)
