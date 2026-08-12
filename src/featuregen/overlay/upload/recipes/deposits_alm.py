"""BR-14 — the Deposits/ALM pack: 12 legacy templates → 14 atomic V2 recipes.

The corrections, structural: the deposit-backed HQLA feature is RETIRED — a liability is never
HQLA; its replacements are the liability's LCR OUTFLOW contribution and a separate asset-side
HQLA buffer recipe; LCR and NSFR contributions read the regulatory classification factors
(runoff factor, stable/less-stable, insured status, operational status, counterparty class /
ASF factor, residual maturity, funding type) as governed policy inputs; deposit beta CANNOT
compute without the ACTUAL paid customer rate beside the benchmark, with reset dates and lags
declared; the repricing gap runs at book/bucket grain, never customer grain; the maturity
ladder and the contractual maturity profile are CONTRACTUAL-FUTURE anchored with their
future-horizon policy declared (the BR-4/BR-6 twin, closing the audited temporal gap); early
withdrawal reads the closure/break event against origination and contractual maturity; and
lagged net interest flow carries its SIGN AUTHORITY (income and expense as declared economic
roles under a governed direction policy — the audit's degrade note, closed).
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

RUNOFF = "treasury_alm.deposit_runoff_forecasting"
NIM = "treasury_alm.net_interest_margin"
IRRBB = "treasury_alm.net_interest_margin"

LCR_CLASSIFICATION = "risk_corridor:lcr-runoff-classification"
NSFR_CLASSIFICATION = "risk_corridor:nsfr-asf-classification"
HQLA_CLASSIFICATION = "risk_corridor:hqla-asset-classification"
RUNOFF_SCENARIO = "risk_corridor:runoff-scenario-policy"
ALM_BASE_CCY = "currency_conversion:alm-base-currency"
ALM_SIGN = "direction_sign:alm-income-expense-signs"


def _account(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="account", concept="account_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _as_of(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="as_of_date", concept="as_of_date",
                         operand_class="as_of_timestamp", allowed_source_grains=(source,))


def _balance(source: str, role: str = "balance") -> OperandSpecV2:
    return OperandSpecV2(role=role, concept="monetary_stock", operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation="monetary")


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"alm:{output_id}",
                              result_class=result_class)


_DEPOSIT_SNAPSHOT = TemporalSpecV2(
    anchor_kind="as_of", business_effective_role="as_of_date",
    window_basis="trailing", window_unit="days", window_parameter="window",
    cutoff_inclusivity="inclusive",
    snapshot_policy="latest-known deposit snapshot at or before the cutoff; a deposit appears "
                    "in exactly one snapshot per as-of")

_FUTURE_LADDER = TemporalSpecV2(
    anchor_kind="contractual_future", business_effective_role="as_of_date",
    window_basis="future_horizon", window_unit="days", window_parameter="window",
    cutoff_inclusivity="exclusive",
    future_horizon_policy="contract terms knowable AT the cutoff: (cutoff, cutoff+window] "
                          "reads contractual maturities only, never behavioural forecasts")


def _lcr_classification_operands() -> tuple[OperandSpecV2, ...]:
    return (
        OperandSpecV2(role="runoff_class", concept="customer_risk_rating",
                      operand_class="policy_input",
                      allowed_source_grains=("deposit_snapshot",),
                      status_policy_ref=LCR_CLASSIFICATION),
        OperandSpecV2(role="insured_status", concept="lifecycle_state",
                      operand_class="status",
                      allowed_source_grains=("deposit_snapshot",)),
    )


DEPOSITS_ALM_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="nmd_stickiness", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Share of non-maturity deposit balance classified STABLE under the LCR "
            "stable/less-stable classification at the cutoff."),
        decision_context="NMD behavioural stability",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="nmd_stickiness", display_label="NMD stable share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="deposits with no classification are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no NMD balance returns null",
            zero_denominator_policy="zero NMD balance returns null"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"), *_lcr_classification_operands()),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="non-maturity deposits under the classification effective at the as-of",
            excluded="classification read outside its effective period",
            policy_refs=(LCR_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("nmd_stickiness", "share"),
        replaces_legacy_ids=("nmd_stickiness",)),

    # ── the HQLA retirement: a liability is never HQLA (corrections 1-3) ────────────────────────
    RecipeDefinitionV2(
        recipe_id="liability_cash_outflow_contribution", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "The deposit's LCR CASH OUTFLOW contribution: balance × the regulatory runoff "
            "factor for its classification (stable/less-stable, insured/uninsured, "
            "operational status, counterparty class) — the liability side of LCR, which is "
            "what a deposit actually contributes."),
        decision_context="LCR outflow attribution",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="liability_cash_outflow_contribution",
            display_label="LCR outflow contribution",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="deposits with no runoff classification are excluded and "
                              "surface as a data gap",
            empty_population_policy="no deposits returns zero",
            aggregation_over_entity="sum across deposits within the book",
            aggregation_over_time="latest snapshot only — never summed across as-ofs"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"), *_lcr_classification_operands()),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="deposits with the full LCR classification set effective at the as-of",
            excluded="THE RETIRED CLAIM: the deposit itself counted as HQLA — a liability is "
                     "never a high-quality liquid asset",
            policy_refs=(LCR_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("liability_cash_outflow_contribution", "sum"),
        replaces_legacy_ids=("hqla_eligibility_contribution", "lcr_outflow_weight")),
    RecipeDefinitionV2(
        recipe_id="asset_hqla_buffer", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "The ASSET-side HQLA buffer: qualifying asset balances × their HQLA-level "
            "haircuts under the governed classification — the numerator of LCR, on the side "
            "of the balance sheet where HQLA actually lives."),
        decision_context="HQLA buffer measurement",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="asset_hqla_buffer", display_label="HQLA buffer",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="assets with no HQLA classification are excluded",
            empty_population_policy="no qualifying assets returns zero",
            aggregation_over_entity="sum across qualifying assets",
            aggregation_over_time="latest snapshot only"),
        operands=(
            OperandSpecV2(role="asset", concept="instrument_id", operand_class="entity_key",
                          allowed_source_grains=("asset_snapshot",)),
            _balance("asset_snapshot", role="asset_value"),
            _as_of("asset_snapshot"),
            OperandSpecV2(role="hqla_class", concept="hqla", operand_class="policy_input",
                          allowed_source_grains=("asset_snapshot",),
                          status_policy_ref=HQLA_CLASSIFICATION)),
        source_grain="asset_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="assets under the HQLA classification effective at the as-of",
            excluded="liabilities of any kind",
            policy_refs=(HQLA_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("asset_hqla_buffer", "sum"),
        replaces_legacy_ids=("hqla_eligibility_contribution",)),

    RecipeDefinitionV2(
        recipe_id="nsfr_asf_contribution", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "The funding item's NSFR available-stable-funding contribution: balance × the ASF "
            "factor for its funding type, residual maturity and counterparty classification."),
        decision_context="NSFR attribution",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="nsfr_asf_contribution", display_label="NSFR ASF contribution",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="items with no ASF classification are excluded and surface as "
                              "a data gap",
            empty_population_policy="no funding items returns zero",
            aggregation_over_entity="sum across funding items",
            aggregation_over_time="latest snapshot only"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"),
                  OperandSpecV2(role="asf_class", concept="nsfr", operand_class="policy_input",
                                allowed_source_grains=("deposit_snapshot",),
                                status_policy_ref=NSFR_CLASSIFICATION),
                  OperandSpecV2(role="residual_maturity", concept="maturity_date",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("deposit_snapshot",))),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="funding items with ASF factor, residual maturity, funding type and "
                     "counterparty classification",
            excluded="items missing any classification input",
            policy_refs=(NSFR_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("nsfr_asf_contribution", "sum"),
        replaces_legacy_ids=("nsfr_asf_contribution",)),

    # ── deposit beta needs the ACTUAL paid rate (corrections 5-6) ───────────────────────────────
    RecipeDefinitionV2(
        recipe_id="deposit_beta", revision=1, family="deposits_alm",
        primary_objective=NIM,
        business_definition=(
            "Change in the ACTUAL paid deposit rate divided by the change in the benchmark "
            "rate over the window, with reset dates and lags declared — the benchmark alone "
            "cannot compute a beta, and the two rates are two DISTINCT operands."),
        decision_context="deposit repricing behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="deposit_beta", display_label="Deposit beta",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="periods missing either rate are excluded from the fit",
            empty_population_policy="no benchmark movement in the window returns null",
            zero_denominator_policy="zero benchmark change returns null"),
        operands=(_account("rate_reset_event"),
                  OperandSpecV2(role="customer_rate", concept="monetary_rate",
                                operand_class="measure",
                                allowed_source_grains=("rate_reset_event",),
                                economic_role="paid_deposit_rate",
                                distinct_binding_group="beta_rates"),
                  OperandSpecV2(role="benchmark_rate", concept="benchmark_rate",
                                operand_class="measure",
                                allowed_source_grains=("rate_reset_event",),
                                distinct_binding_group="beta_rates"),
                  OperandSpecV2(role="reset_date", concept="effective_date",
                                operand_class="event_timestamp",
                                allowed_source_grains=("rate_reset_event",))),
        source_grain="rate_reset_event", output_grain="book",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="reset_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="rate resets carrying BOTH the paid customer rate and the benchmark, "
                     "with reset dates and lags",
            excluded="benchmark-only rows — a beta without the paid rate is not a beta"),
        formula=_formula("deposit_beta", "ratio"),
        replaces_legacy_ids=("deposit_beta",)),

    # ── repricing gap at book/bucket grain (correction 7) ───────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="repricing_gap_exposure", revision=1, family="deposits_alm",
        primary_objective=IRRBB,
        business_definition=(
            "Rate-sensitive assets minus rate-sensitive liabilities repricing in the bucket, "
            "at BOOK/BUCKET grain — a repricing gap is a balance-sheet fact, not a customer "
            "attribute."),
        decision_context="IRRBB repricing gap",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="repricing_gap_exposure", display_label="Repricing gap",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="positions with no repricing date are excluded and surface as "
                              "a data gap",
            empty_population_policy="an empty bucket returns zero",
            aggregation_over_entity="sum within the book and bucket",
            aggregation_over_time="latest snapshot only"),
        operands=(
            OperandSpecV2(role="book", concept="book_id", operand_class="entity_key",
                          allowed_source_grains=("position_snapshot",)),
            _balance("position_snapshot", role="position_value"),
            OperandSpecV2(role="repricing_date", concept="effective_date",
                          operand_class="as_of_timestamp",
                          allowed_source_grains=("position_snapshot",)),
            _as_of("position_snapshot")),
        source_grain="position_snapshot", output_grain="book_bucket",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="rate-sensitive positions bucketed by repricing date",
            excluded="customer-grain aggregation of a balance-sheet fact",
            policy_refs=(ALM_BASE_CCY,)),
        formula=_formula("repricing_gap_exposure", "sum"),
        replaces_legacy_ids=("repricing_gap_exposure",)),

    RecipeDefinitionV2(
        recipe_id="hot_money_share", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Share of deposit balance classified LESS-STABLE / rate-sensitive under the LCR "
            "classification at the cutoff."),
        decision_context="funding fragility",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="hot_money_share", display_label="Hot-money share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="deposits with no classification are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no deposit balance returns null",
            zero_denominator_policy="zero deposit balance returns null"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"), *_lcr_classification_operands()),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="deposits under the classification effective at the as-of",
            excluded="classification read outside its effective period",
            policy_refs=(LCR_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("hot_money_share", "share"),
        replaces_legacy_ids=("hot_money_share",)),
    RecipeDefinitionV2(
        recipe_id="rate_sensitive_concentration", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Concentration (HHI) of rate-sensitive deposit balance across counterparty "
            "classes at the cutoff."),
        decision_context="funding concentration",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rate_sensitive_concentration",
            display_label="Rate-sensitive concentration",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="deposits with no counterparty class are excluded",
            empty_population_policy="no rate-sensitive balance returns null",
            zero_denominator_policy="zero rate-sensitive balance returns null"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"),
                  OperandSpecV2(role="counterparty_class", concept="segment",
                                operand_class="dimension",
                                allowed_source_grains=("deposit_snapshot",))),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_DEPOSIT_SNAPSHOT,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="rate-sensitive deposits with a counterparty class",
            excluded="classification read outside its effective period",
            policy_refs=(LCR_CLASSIFICATION, ALM_BASE_CCY)),
        formula=_formula("rate_sensitive_concentration", "share"),
        replaces_legacy_ids=("rate_sensitive_concentration",)),

    # ── the contractual-future twins (corrections 8-10) ─────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="maturity_ladder_runoff", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Contractual balance maturing in the FORWARD bucket (cutoff, cutoff+window] under "
            "the governed runoff scenario — effective maturity, balance and scenario policy "
            "all declared; the bucket reads contract terms knowable AT the cutoff."),
        decision_context="maturity ladder",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="maturity_ladder_runoff", display_label="Maturity ladder runoff",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="deposits with no contractual maturity are excluded from the "
                              "ladder and surface as a data gap",
            empty_population_policy="an empty bucket returns zero",
            aggregation_over_entity="sum within the bucket; a deposit appears in exactly ONE "
                                    "bucket per as-of",
            aggregation_over_time="never summed across as-ofs"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"),
                  OperandSpecV2(role="contractual_maturity", concept="maturity_date",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("deposit_snapshot",)),
                  OperandSpecV2(role="scenario", concept="scenario_id",
                                operand_class="policy_input",
                                allowed_source_grains=("deposit_snapshot",),
                                status_policy_ref=RUNOFF_SCENARIO)),
        source_grain="deposit_snapshot", output_grain="book_bucket",
        temporal=_FUTURE_LADDER,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="deposits with a contractual maturity, under the governed scenario",
            excluded="behavioural runoff forecasts (a model's job, not a ladder's)",
            policy_refs=(RUNOFF_SCENARIO, ALM_BASE_CCY)),
        formula=_formula("maturity_ladder_runoff", "sum"),
        replaces_legacy_ids=("maturity_ladder_runoff",)),
    RecipeDefinitionV2(
        recipe_id="contractual_deposit_maturity_profile", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Share of term-deposit balance whose contractual maturity falls inside the "
            "forward bucket — the profile version of the ladder, with the SAME "
            "contractual-future temporal policy, completed."),
        decision_context="maturity profile",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="contractual_deposit_maturity_profile",
            display_label="Contractual maturity share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="deposits with no contractual maturity are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no term balance returns null",
            zero_denominator_policy="zero term balance returns null"),
        operands=(_account("deposit_snapshot"), _balance("deposit_snapshot"),
                  _as_of("deposit_snapshot"),
                  OperandSpecV2(role="contractual_maturity", concept="maturity_date",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("deposit_snapshot",))),
        source_grain="deposit_snapshot", output_grain="book",
        temporal=_FUTURE_LADDER,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="term deposits with contractual maturities knowable at the cutoff",
            excluded="behavioural assumptions in a contractual profile",
            policy_refs=(ALM_BASE_CCY,)),
        formula=_formula("contractual_deposit_maturity_profile", "share"),
        replaces_legacy_ids=("contractual_deposit_maturity_profile",)),

    RecipeDefinitionV2(
        recipe_id="early_withdrawal_break", revision=1, family="deposits_alm",
        primary_objective=RUNOFF,
        business_definition=(
            "Count of term deposits BROKEN before contractual maturity in the window — the "
            "closure/break EVENT read against origination and contractual maturity, with the "
            "notice period its own governed concept."),
        decision_context="break behaviour",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="early_withdrawal_break", display_label="Early withdrawal breaks",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="closures with no maturity to compare are excluded and surface "
                              "as a data gap",
            empty_population_policy="no closures returns zero"),
        operands=(_account("closure_event"),
                  OperandSpecV2(role="closure_ts", concept="event_timestamp",
                                operand_class="event_timestamp",
                                allowed_source_grains=("closure_event",)),
                  OperandSpecV2(role="origination", concept="origination_date",
                                operand_class="as_of_timestamp",
                                allowed_source_grains=("closure_event",)),
                  OperandSpecV2(role="contractual_maturity", concept="maturity_date",
                                operand_class="dimension",
                                allowed_source_grains=("closure_event",)),
                  OperandSpecV2(role="notice_term", concept="notice_period",
                                operand_class="dimension", required=False,
                                allowed_source_grains=("closure_event",))),
        source_grain="closure_event", output_grain="book",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="closure_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="closure events before contractual maturity",
            excluded="maturities at term (a matured deposit is not a break)"),
        formula=_formula("early_withdrawal_break", "count"),
        replaces_legacy_ids=("early_withdrawal_break",)),

    # ── net interest flow with SIGN AUTHORITY (correction 11) ───────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="lagged_net_interest_flow", revision=1, family="deposits_alm",
        primary_objective=NIM,
        business_definition=(
            "Interest income minus interest expense over the window, each an operand with a "
            "DECLARED economic role, combined under the governed income/expense sign policy — "
            "the audit's 'missing sign authority' degrade, closed."),
        decision_context="net interest flow",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="lagged_net_interest_flow", display_label="Net interest flow",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=ALM_BASE_CCY,
            null_input_policy="null flows are excluded per the source policy",
            empty_population_policy="an empty window returns zero",
            aggregation_over_entity="sum within the book",
            aggregation_over_time="sum over disjoint windows"),
        operands=(
            OperandSpecV2(role="book", concept="book_id", operand_class="entity_key",
                          allowed_source_grains=("interest_event",)),
            OperandSpecV2(role="income", concept="interest_income", operand_class="measure",
                          allowed_source_grains=("interest_event",),
                          unit_expectation="monetary",
                          sign_direction_expectation="per the governed sign policy",
                          distinct_binding_group="interest_legs"),
            OperandSpecV2(role="expense", concept="interest_expense", operand_class="measure",
                          allowed_source_grains=("interest_event",),
                          unit_expectation="monetary",
                          sign_direction_expectation="per the governed sign policy",
                          distinct_binding_group="interest_legs"),
            OperandSpecV2(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp",
                          allowed_source_grains=("interest_event",))),
        source_grain="interest_event", output_grain="book",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="recognized interest flows under the governed sign policy",
            excluded="flows whose sign convention is undeclared",
            policy_refs=(ALM_SIGN, ALM_BASE_CCY)),
        formula=_formula("lagged_net_interest_flow", "sum"),
        replaces_legacy_ids=("lagged_net_interest_flow",)),
)
