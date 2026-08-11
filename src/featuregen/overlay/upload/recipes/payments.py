"""BR-13 — the payments pack: 10 legacy templates → 13 atomic V2 recipes.

The corrections, structural: the decline rate reads AUTHORIZATION outcome — settlement status
cannot see a decline, and the two are never interchangeable operands; chargebacks carry the
dispute lifecycle (status, reason, original-transaction link); returns carry the scheme return
status and reason; mandate STATE stays separate from collection EXECUTION outcome; the payment
stages (initiation/authorization/booking/clearing/value/settlement) are distinct timestamp
concepts, so settlement lag is a difference of two NAMED stages; merchant economics run at
MERCHANT grain with the fee-basis relationships (interchange, MDR amount vs rate) declared; and
counts, amounts, rates and averages are separate atomic outputs.
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
from featuregen.overlay.upload.recipes.fraud import _auth_outcome, _auth_ts
from featuregen.overlay.upload.recipes.retail import _WINDOW

PAY_BEHAVIOUR = "payments.behaviour"
MERCHANT_ECON = "payments.merchant.interchange"
PAY_OPS = "payments.operations"

PAY_ELIGIBLE = "eligible_status:payments-posted-events"
PAY_BASE_CCY = "currency_conversion:payments-base-currency"
FEE_BASIS = "threshold:scheme-fee-basis"


def _customer(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _merchant(source: str = "acquiring_settlement") -> OperandSpecV2:
    return OperandSpecV2(role="merchant", concept="merchant_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _amount(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation="monetary")


def _event_ts(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="event_ts", concept="event_timestamp",
                         operand_class="event_timestamp", allowed_source_grains=(source,))


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"payments:{output_id}",
                              result_class=result_class)


def _days_window(role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


PAYMENTS_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── rail activity: count and amount are two outputs (correction 8) ──────────────────────────
    RecipeDefinitionV2(
        recipe_id="rail_txn_count", revision=1, family="payments",
        primary_objective=PAY_BEHAVIOUR,
        business_definition="Posted transaction count per payment rail over the window.",
        decision_context="rail mix (count side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rail_txn_count", display_label="Rail transaction count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown rail are excluded",
            empty_population_policy="no activity returns zero"),
        operands=(_customer(),
                  OperandSpecV2(role="rail", concept="payment_rail", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with rail identity",
            excluded="reversed and technical events",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("rail_txn_count", "count"),
        replaces_legacy_ids=("rail_volume_value",)),
    RecipeDefinitionV2(
        recipe_id="rail_txn_amount", revision=1, family="payments",
        primary_objective=PAY_BEHAVIOUR,
        business_definition=(
            "Posted transaction value per payment rail over the window, in base currency."),
        decision_context="rail mix (value side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rail_txn_amount", display_label="Rail transaction amount",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=PAY_BASE_CCY,
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="no activity returns zero",
            aggregation_over_entity="sum across customers",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_customer(), _amount(),
                  OperandSpecV2(role="rail", concept="payment_rail", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with rail identity",
            excluded="reversed and technical events",
            policy_refs=(PAY_ELIGIBLE, PAY_BASE_CCY)),
        formula=_formula("rail_txn_amount", "sum"),
        replaces_legacy_ids=("rail_volume_value",)),
    RecipeDefinitionV2(
        recipe_id="rail_scheme_diversity", revision=1, family="payments",
        primary_objective=PAY_BEHAVIOUR,
        business_definition="Distinct payment rails/schemes used over the window.",
        decision_context="payment-mix breadth",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rail_scheme_diversity", display_label="Rail/scheme diversity",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown rail are excluded",
            empty_population_policy="no activity returns zero"),
        operands=(_customer(),
                  OperandSpecV2(role="rail", concept="payment_rail", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with rail identity",
            excluded="reversed and technical events",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("rail_scheme_diversity", "distinct_count"),
        replaces_legacy_ids=("rail_scheme_diversity",)),
    RecipeDefinitionV2(
        recipe_id="purpose_code_diversity", revision=1, family="payments",
        primary_objective=PAY_BEHAVIOUR,
        business_definition=(
            "Distinct ISO 20022 purpose codes over the window — purpose codes describing "
            "payment PURPOSE, their one legitimate home."),
        decision_context="payment-purpose breadth",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="purpose_code_diversity", display_label="Purpose-code diversity",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with no purpose code are excluded",
            empty_population_policy="no coded activity returns zero"),
        operands=(_customer(),
                  OperandSpecV2(role="purpose", concept="iso20022_purpose_code",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions carrying a purpose code",
            excluded="reversed and technical events",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("purpose_code_diversity", "distinct_count"),
        replaces_legacy_ids=("purpose_code_diversity",)),

    # ── merchant economics at MERCHANT grain with fee-basis declared (corrections 6-7) ──────────
    RecipeDefinitionV2(
        recipe_id="interchange_revenue_sum", revision=1, family="payments",
        primary_objective=MERCHANT_ECON,
        business_definition=(
            "Interchange revenue booked for the merchant's settled volume over the window — "
            "merchant/acquirer grain, under the governed fee basis."),
        decision_context="acquiring economics (interchange side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="interchange_revenue_sum", display_label="Interchange revenue",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=PAY_BASE_CCY,
            null_input_policy="null fee rows are excluded per the source policy",
            empty_population_policy="no settled volume returns zero",
            aggregation_over_entity="sum across merchants",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_merchant(),
                  OperandSpecV2(role="interchange", concept="interchange",
                                operand_class="measure",
                                allowed_source_grains=("acquiring_settlement",),
                                unit_expectation="monetary"),
                  _event_ts("acquiring_settlement")),
        source_grain="acquiring_settlement", output_grain="merchant",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled acquiring records under the governed fee basis",
            excluded="fee rows with no declared basis",
            policy_refs=(PAY_ELIGIBLE, FEE_BASIS, PAY_BASE_CCY)),
        formula=_formula("interchange_revenue_sum", "sum"),
        replaces_legacy_ids=("interchange_revenue",)),
    RecipeDefinitionV2(
        recipe_id="merchant_discount_rate", revision=1, family="payments",
        primary_objective=MERCHANT_ECON,
        business_definition=(
            "MDR amount divided by settled volume for the merchant over the window — the "
            "amount/rate RELATIONSHIP declared, so a stored rate and a computed rate cannot "
            "silently disagree."),
        decision_context="acquiring economics (MDR side)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="merchant_discount_rate", display_label="Merchant discount rate",
            output_type="numeric", additivity="non_additive", unit_kind="rate",
            null_input_policy="null fee or volume rows are excluded per the source policy",
            empty_population_policy="no settled volume returns null",
            zero_denominator_policy="zero settled volume returns null"),
        operands=(_merchant(),
                  OperandSpecV2(role="mdr_amount", concept="merchant_discount_rate",
                                operand_class="measure",
                                allowed_source_grains=("acquiring_settlement",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="settled_volume", concept="monetary_flow",
                                operand_class="measure",
                                allowed_source_grains=("acquiring_settlement",),
                                unit_expectation="monetary"),
                  _event_ts("acquiring_settlement")),
        source_grain="acquiring_settlement", output_grain="merchant",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled acquiring records under the governed fee basis",
            excluded="fee rows with no declared basis",
            policy_refs=(PAY_ELIGIBLE, FEE_BASIS, PAY_BASE_CCY)),
        formula=_formula("merchant_discount_rate", "ratio"),
        replaces_legacy_ids=("merchant_discount_economics",)),

    # ── the decline rate reads AUTHORIZATION, never settlement (correction 1) ───────────────────
    RecipeDefinitionV2(
        recipe_id="authorisation_decline_rate", revision=1, family="payments",
        primary_objective=PAY_OPS,
        business_definition=(
            "Declined authorizations divided by authorization attempts over the window — read "
            "from the AUTHORIZATION feed's outcome at auth time; a settlement row cannot see "
            "a decline, and the two feeds are never interchangeable."),
        decision_context="authorization funnel health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="authorisation_decline_rate", display_label="Auth decline rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="attempts with unknown outcome are excluded",
            empty_population_policy="no attempts returns null",
            zero_denominator_policy="zero attempts returns null"),
        operands=(_merchant("authorization_event"), _auth_outcome(), _auth_ts()),
        source_grain="authorization_event", output_grain="merchant",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="auth_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="authorization attempts with a recorded outcome",
            excluded="settlement rows standing in for authorization outcomes"),
        formula=_formula("authorisation_decline_rate", "ratio"),
        replaces_legacy_ids=("authorisation_decline_rate",)),

    # ── chargebacks and returns carry their lifecycles (corrections 2-3) ────────────────────────
    RecipeDefinitionV2(
        recipe_id="chargeback_rate", revision=1, family="payments",
        primary_objective=MERCHANT_ECON,
        business_definition=(
            "Chargebacks RAISED in the window divided by settled transactions — each "
            "chargeback carries its dispute status, reason code and the ORIGINAL transaction "
            "link, so a dispute raised after the cutoff never counts inside it."),
        decision_context="merchant dispute health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="chargeback_rate", display_label="Chargeback rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="disputes with no original-transaction link are excluded and "
                              "surface as a data gap",
            empty_population_policy="no settled transactions returns null",
            zero_denominator_policy="zero settled transactions returns null"),
        operands=(_merchant("dispute_event"),
                  OperandSpecV2(role="dispute_status", concept="chargeback_status",
                                operand_class="status",
                                allowed_source_grains=("dispute_event",)),
                  OperandSpecV2(role="reason", concept="dispute_reason_code",
                                operand_class="dimension",
                                allowed_source_grains=("dispute_event",)),
                  OperandSpecV2(role="original_txn", concept="original_transaction_id",
                                operand_class="dimension",
                                allowed_source_grains=("dispute_event",)),
                  _event_ts("dispute_event")),
        source_grain="dispute_event", output_grain="merchant",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="disputes raised in the window, linked to their original transactions",
            excluded="disputes raised after the cutoff; refunds (a credit, not a dispute)",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("chargeback_rate", "ratio"),
        replaces_legacy_ids=("chargeback_dispute_rate",)),
    RecipeDefinitionV2(
        recipe_id="return_payment_rate", revision=1, family="payments",
        primary_objective=PAY_OPS,
        business_definition=(
            "Scheme RETURNS divided by collection attempts over the window — each return "
            "carries its return status and scheme reason code, so a mandate cancellation "
            "(no attempt at all) is never counted as a return."),
        decision_context="collection return health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="return_payment_rate", display_label="Return rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="returns with unknown reason still count; unknown-status rows "
                              "are excluded",
            empty_population_policy="no collection attempts returns null",
            zero_denominator_policy="zero attempts returns null"),
        operands=(_customer("payment_return_event"),
                  OperandSpecV2(role="return_status", concept="payment_return_status",
                                operand_class="status",
                                allowed_source_grains=("payment_return_event",)),
                  OperandSpecV2(role="reason", concept="return_reason_code",
                                operand_class="dimension",
                                allowed_source_grains=("payment_return_event",)),
                  _event_ts("payment_return_event")),
        source_grain="payment_return_event", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="collection attempts and their scheme return events",
            excluded="mandate cancellations (mandate STATE, a different fact from a bounced "
                     "collection — the retail pack's mandate recipes own it)",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("return_payment_rate", "ratio"),
        replaces_legacy_ids=("return_payment_rate",)),

    # ── settlement lag is a difference of two NAMED stages (correction 5) ───────────────────────
    RecipeDefinitionV2(
        recipe_id="settlement_lag_avg_days", revision=1, family="payments",
        primary_objective=PAY_OPS,
        business_definition=(
            "Average days between BOOKING and SETTLEMENT per settled payment over the window "
            "— two named stage timestamps subtracted, never 'the two date columns'."),
        decision_context="settlement operations health",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="settlement_lag_avg_days", display_label="Settlement lag (avg days)",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="payments missing either stage timestamp are excluded",
            empty_population_policy="no settled payments returns null"),
        operands=(_customer("settlement_event"),
                  OperandSpecV2(role="booking_ts", concept="booking_date",
                                operand_class="event_timestamp",
                                allowed_source_grains=("settlement_event",),
                                distinct_binding_group="stage_timestamps"),
                  OperandSpecV2(role="settlement_ts", concept="settlement_date",
                                operand_class="event_timestamp",
                                allowed_source_grains=("settlement_event",),
                                distinct_binding_group="stage_timestamps")),
        source_grain="settlement_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="settlement_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="settled payments carrying both stage timestamps",
            excluded="one physical column serving both stages",
            policy_refs=(PAY_ELIGIBLE,)),
        formula=_formula("settlement_lag_avg_days", "ratio"),
        replaces_legacy_ids=("settlement_lag",)),

    RecipeDefinitionV2(
        recipe_id="corridor_cross_border_share", revision=1, family="payments",
        primary_objective=PAY_BEHAVIOUR,
        business_definition=(
            "Cross-border value as a share of total posted value over the window, by "
            "corridor, in base currency."),
        decision_context="cross-border mix",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="corridor_cross_border_share", display_label="Cross-border share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown corridor are excluded from the numerator, "
                              "kept in the denominator",
            empty_population_policy="no posted value returns null",
            zero_denominator_policy="zero total value returns null"),
        operands=(_customer(), _amount(),
                  OperandSpecV2(role="corridor", concept="corridor", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions with corridor identity",
            excluded="reversed and technical events",
            policy_refs=(PAY_ELIGIBLE, PAY_BASE_CCY)),
        formula=_formula("corridor_cross_border_share", "share"),
        replaces_legacy_ids=("corridor_cross_border_share",)),
)
