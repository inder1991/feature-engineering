"""BR-15 — the Islamic-banking pack: 8 legacy templates → 8 atomic V2 recipes.

Structural corrections: every recipe names its CONTRACT TYPE and Sharia governance reference
as policy inputs; profit-rate semantics replace interest terminology throughout (a profit rate
is a profit rate — the concepts and words say so); profit-sharing reads the pool, the sharing
ratio and the ACTUAL distributed rate; Murabaha behaviour reads the installment schedule
(due_date, scheduled_amount, payment_allocation — the old admission closed); purification and
prohibited-income classification are governed policy; Takaful contributions and claims are
participant-fund facts.
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
    policy_input,
    snapshot_window,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

BANKING = "islamic.banking"
SHARIA = "islamic.sharia_compliance"

SHARIA_BOARD = "privacy_purpose:sharia-board-governance"
CONTRACT_TYPE = "active_state:islamic-contract-structure"
PURIFICATION = "risk_corridor:purification-prohibited-income"
ISL_CCY = "currency_conversion:islamic-base-currency"


ISLAMIC_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="profit_rate_exposure", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition=(
            "Balance repricing to the PROFIT-RATE benchmark within the bucket, per contract "
            "type — profit-rate semantics, never conventional-lending terminology."),
        decision_context="profit-rate risk",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="profit_rate_exposure", display_label="Profit-rate exposure",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ISL_CCY,
            null_input_policy="positions with no repricing date are excluded",
            empty_population_policy="an empty bucket returns zero",
            aggregation_over_entity="sum within the bucket",
            aggregation_over_time="latest snapshot only"),
        operands=(entity("account", "account_id", "islamic_position_snapshot"),
                  measure("balance", "monetary_stock", "islamic_position_snapshot"),
                  policy_input("contract", "product_type", "islamic_position_snapshot",
                               policy=CONTRACT_TYPE),
                  as_of("islamic_position_snapshot")),
        source_grain="islamic_position_snapshot", output_grain="book_bucket",
        temporal=snapshot_window("latest position snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="positions with a declared Islamic contract structure",
            excluded="conventional-lending-terminology reads of profit-rate facts",
            policy_refs=(CONTRACT_TYPE, ISL_CCY)),
        formula=formula("islamic", "profit_rate_exposure", "sum"),
        replaces_legacy_ids=("profit_rate_exposure",)),
    RecipeDefinitionV2(
        recipe_id="profit_sharing_split_behaviour", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition=(
            "The ACTUAL distributed profit rate against the pool's declared sharing ratio — "
            "pool, ratio and actual rate are three declared operands."),
        decision_context="Mudarabah distribution behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="profit_sharing_split_behaviour", display_label="Profit-share fidelity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="periods missing pool, ratio or actual rate return null and "
                              "surface the gap",
            empty_population_policy="no distributions returns null",
            zero_denominator_policy="a zero declared ratio returns null"),
        operands=(entity("account", "account_id", "profit_distribution"),
                  measure("actual_rate", "profit_rate", "profit_distribution", unit="rate"),
                  measure("sharing_ratio", "profit_share_ratio", "profit_distribution",
                          unit="rate"),
                  event_ts("profit_distribution")),
        source_grain="profit_distribution", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="distributions with pool, declared ratio and actual rate",
            excluded="benchmark substitution for the actual rate",
            policy_refs=(SHARIA_BOARD,)),
        formula=formula("islamic", "profit_sharing_split_behaviour", "ratio"),
        replaces_legacy_ids=("profit_sharing_split_behaviour",)),
    RecipeDefinitionV2(
        recipe_id="purification_ratio", revision=1, family="islamic",
        primary_objective=SHARIA,
        business_definition=(
            "Purification amount divided by gross income under the governed purification "
            "and prohibited-income policy — a governance fact, computed as declared."),
        decision_context="purification governance",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="purification_ratio", display_label="Purification ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="periods with no classification are excluded and surface as "
                              "a gap",
            empty_population_policy="no income returns null",
            zero_denominator_policy="zero income returns null"),
        operands=(entity("portfolio", "portfolio_id", "income_classification"),
                  measure("purification", "purification_amount", "income_classification"),
                  measure("gross_income", "monetary_flow", "income_classification"),
                  event_ts("income_classification")),
        source_grain="income_classification", output_grain="portfolio",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="income classified under the purification policy",
            excluded="unclassified income assumed pure",
            policy_refs=(PURIFICATION, SHARIA_BOARD)),
        formula=formula("islamic", "purification_ratio", "ratio"),
        replaces_legacy_ids=("purification_ratio",)),
    RecipeDefinitionV2(
        recipe_id="prohibited_activity_exposure_share", revision=1, family="islamic",
        primary_objective=SHARIA,
        business_definition=(
            "Exposure to activities classified PROHIBITED by the governed classification, "
            "as a share of total exposure."),
        decision_context="Sharia screening",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="prohibited_activity_exposure_share",
            display_label="Prohibited-activity share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="unclassified exposures are excluded from the numerator, kept "
                              "in the denominator",
            empty_population_policy="no exposure returns null",
            zero_denominator_policy="zero exposure returns null"),
        operands=(entity("portfolio", "portfolio_id", "exposure_snapshot"),
                  measure("exposure", "prohibited_activity_exposure", "exposure_snapshot"),
                  as_of("exposure_snapshot")),
        source_grain="exposure_snapshot", output_grain="portfolio",
        temporal=snapshot_window("latest exposure snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="exposures under the prohibited-activity classification",
            excluded="name-pattern guesses at prohibition",
            policy_refs=(PURIFICATION, ISL_CCY)),
        formula=formula("islamic", "prohibited_activity_exposure_share", "share"),
        replaces_legacy_ids=("prohibited_activity_exposure_share",)),
    RecipeDefinitionV2(
        recipe_id="sukuk_concentration", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition="Concentration (HHI) of sukuk holdings across issuers.",
        decision_context="sukuk concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sukuk_concentration", display_label="Sukuk concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="holdings with no issuer identity are excluded",
            empty_population_policy="no sukuk holdings returns null",
            zero_denominator_policy="zero holdings returns null"),
        operands=(entity("portfolio", "portfolio_id", "holding_snapshot"),
                  dim("issuer", "lei", "holding_snapshot"),
                  measure("holding", "sukuk", "holding_snapshot"),
                  as_of("holding_snapshot")),
        source_grain="holding_snapshot", output_grain="portfolio",
        temporal=snapshot_window("latest holding snapshot at or before the cutoff"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="sukuk holdings with issuer identity",
            excluded="conventional bonds",
            policy_refs=(ISL_CCY,)),
        formula=formula("islamic", "sukuk_concentration", "share"),
        replaces_legacy_ids=("sukuk_concentration",)),
    RecipeDefinitionV2(
        recipe_id="takaful_contribution_behaviour", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition=(
            "Takaful contributions collected in the window — a PARTICIPANT-FUND fact, "
            "beside (never merged with) the fund's claims."),
        decision_context="Takaful persistency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="takaful_contribution_behaviour", display_label="Takaful contributions",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ISL_CCY,
            null_input_policy="null contributions are excluded per the source policy",
            empty_population_policy="no contributions returns zero",
            aggregation_over_entity="sum within the participant fund",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("policy", "policy_id", "takaful_event"),
                  measure("contribution", "takaful_contribution", "takaful_event"),
                  event_ts("takaful_event")),
        source_grain="takaful_event", output_grain="policy",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="participant contributions to the Takaful fund",
            excluded="claims (the fund's other side, its own fact)",
            policy_refs=(SHARIA_BOARD, ISL_CCY)),
        formula=formula("islamic", "takaful_contribution_behaviour", "sum"),
        replaces_legacy_ids=("takaful_contribution_behaviour",)),
    RecipeDefinitionV2(
        recipe_id="islamic_deposit_beta", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition=(
            "Change in the ACTUAL distributed profit rate divided by the change in the "
            "profit-rate benchmark — the Islamic twin of deposit beta, in profit-rate "
            "words, with both rates as distinct operands."),
        decision_context="profit-rate repricing behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="islamic_deposit_beta", display_label="Profit-rate beta",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="periods missing either rate are excluded from the fit",
            empty_population_policy="no benchmark movement returns null",
            zero_denominator_policy="zero benchmark change returns null"),
        operands=(entity("account", "account_id", "profit_distribution"),
                  measure("actual_rate", "profit_rate", "profit_distribution", unit="rate"),
                  measure("benchmark", "benchmark_rate", "profit_distribution", unit="rate"),
                  event_ts("profit_distribution")),
        source_grain="profit_distribution", output_grain="book",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="distributions with actual and benchmark profit rates",
            excluded="conventional-rate terminology for profit-rate facts",
            policy_refs=(CONTRACT_TYPE,)),
        formula=formula("islamic", "islamic_deposit_beta", "ratio"),
        replaces_legacy_ids=("islamic_deposit_beta",)),
    RecipeDefinitionV2(
        recipe_id="murabaha_installment_behaviour", revision=1, family="islamic",
        primary_objective=BANKING,
        business_definition=(
            "Murabaha installments met in full under the schedule's principal/profit split "
            "and payment allocation — due_date, scheduled_amount and allocation declared "
            "(the old 'no dedicated Murabaha concept' admission closed by the schedule "
            "concepts)."),
        decision_context="Murabaha payment behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="murabaha_installment_behaviour",
            display_label="Murabaha installments met",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="installments missing schedule fields are excluded and "
                              "surface as a gap",
            empty_population_policy="no installments due returns null"),
        operands=(entity("account", "account_id", "murabaha_schedule"),
                  event_ts("murabaha_schedule", role="due_date", concept="due_date"),
                  measure("amount_due", "scheduled_amount", "murabaha_schedule"),
                  policy_input("allocation", "payment_allocation", "murabaha_schedule",
                               policy="allocation:murabaha-principal-profit-split"),
                  policy_input("contract", "product_type", "murabaha_schedule",
                               policy=CONTRACT_TYPE)),
        source_grain="murabaha_schedule", output_grain="account",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled Murabaha installments with the principal/profit split",
            excluded="payments allocated outside the declared order",
            policy_refs=(CONTRACT_TYPE, "allocation:murabaha-principal-profit-split")),
        formula=formula("islamic", "murabaha_installment_behaviour", "count"),
        replaces_legacy_ids=("murabaha_installment_behaviour",)),
)
