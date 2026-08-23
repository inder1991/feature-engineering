"""BR-13 — the fraud pack: 12 legacy templates → 13 atomic V2 recipes.

The corrections, structural: every recipe DECLARES its payment-lifecycle stage
(:data:`FRAUD_LIFECYCLE_STAGE`) and reads the feed that stage owns — card-testing and decline
patterns read the AUTHORIZATION feed's outcome (authorization_status, a BR-10 concept), booked
behaviour reads the transaction feed, and the two are never interchangeable operands; payee
novelty is calculated on the VERIFIED beneficiary identity (beneficiary_id), never the
beneficiary bank; just-under-limit behaviour reads the GOVERNED effective threshold/control
policy; merchant anomaly names its customer source entity (customer-relative by construction);
impossible-travel stays conceptual (geo distance is outside the formula grammar — a refusal,
not an approximation); and merchant_mcc_diversity keeps its REVIEWED Formula-v1 count-distinct
expectation — the one recipe in this pack that is honestly FORMULA_AUTHORABLE today.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    OperandSpecV2,
    OutputSpecV2,
    ParameterSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

TXN_FRAUD = "fraud.transaction_fraud_detection"
MERCHANT_FRAUD = "fraud.merchant_fraud"

FRAUD_ELIGIBLE = "eligible_status:fraud-eligible-events"
CARD_CONTINUITY = "active_state:card-token-device-continuity"
LIMIT_POLICY = "threshold:authorization-limit-controls"
SMALL_AMOUNT = "threshold:card-testing-small-amount"

#: The plan's first correction, as data a test pins: WHERE in the payment lifecycle each recipe
#: runs. pre_authorization sees only the attempt; post_authorization sees the auth OUTCOME;
#: post_booking sees the ledger. A recipe may never read a later stage's feed than it declares.
FRAUD_LIFECYCLE_STAGE: dict[str, str] = {
    "card_testing_velocity": "post_authorization",
    "auth_decline_streak": "post_authorization",
    "device_sharing_velocity": "pre_authorization",
    "new_device_flag": "pre_authorization",
    "geo_velocity_impossible": "pre_authorization",
    "first_time_payee_high_value": "post_booking",
    "merchant_amount_zscore": "post_booking",
    "merchant_mcc_diversity": "post_booking",
    "txn_velocity_spike": "post_booking",
    "amount_zscore_spike": "post_booking",
    "cross_channel_rail_burst": "post_booking",
    "cross_border_burst": "post_booking",
    "amount_just_under_limit": "post_authorization",
}

_MINUTES_WINDOW = ParameterSpecV2(name="window_minutes", parameter_class="operational",
                                  allowed_values=(15, 60, 240),
                                  identity_projection="window={value}m",
                                  display_projection="{value}-minute window")


def _card(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="card", concept="card_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _customer(source: str) -> OperandSpecV2:
    return OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                         allowed_source_grains=(source,))


def _auth_outcome() -> OperandSpecV2:
    return OperandSpecV2(role="auth_outcome", concept="authorization_status",
                         operand_class="status",
                         allowed_source_grains=("authorization_event",),
                         status_policy_ref=FRAUD_ELIGIBLE)


def _auth_ts() -> OperandSpecV2:
    return OperandSpecV2(role="auth_ts", concept="authorization_timestamp",
                         operand_class="event_timestamp",
                         allowed_source_grains=("authorization_event",))


def _event_ts(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="event_ts", concept="event_timestamp",
                         operand_class="event_timestamp", allowed_source_grains=(source,))


def _amount(source: str = "transaction") -> OperandSpecV2:
    return OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                         allowed_source_grains=(source,), unit_expectation="monetary")


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"fraud:{output_id}",
                              result_class=result_class)


def _minutes_window(role: str) -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="minutes",
                          window_parameter="window_minutes", cutoff_inclusivity="inclusive")


def _days_window(role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


FRAUD_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── the authorization stage: outcomes read from the AUTH feed (corrections 1-2) ─────────────
    RecipeDefinitionV2(
        recipe_id="card_testing_velocity", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Count of DECLINED small-amount authorization attempts on the card in a short "
            "window — card testing reads the authorization OUTCOME at auth time; a settlement "
            "feed cannot see a decline at all."),
        decision_context="card-testing detection (post-authorization)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="card_testing_velocity", display_label="Declined small-auth velocity",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="attempts with unknown outcome are excluded",
            empty_population_policy="no attempts returns zero"),
        operands=(_card("authorization_event"), _auth_outcome(), _auth_ts(),
                  OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                                allowed_source_grains=("authorization_event",),
                                unit_expectation="monetary")),
        source_grain="authorization_event", output_grain="card",
        temporal=_minutes_window("auth_ts"),
        readiness="FORMULA_BLOCKED", parameters=(_MINUTES_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="declined authorizations under the governed small-amount policy",
            excluded="approved authorizations; booked transactions (a different stage)",
            policy_refs=(FRAUD_ELIGIBLE, SMALL_AMOUNT)),
        formula=_formula("card_testing_velocity", "count"),
        replaces_legacy_ids=("card_testing_velocity",)),
    RecipeDefinitionV2(
        recipe_id="auth_decline_streak", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Longest run of consecutive declined authorizations on the card in the window."),
        decision_context="decline-pattern detection (post-authorization)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="auth_decline_streak", display_label="Auth decline streak",
            output_type="integer", additivity="non_additive", unit_kind="count",
            null_input_policy="attempts with unknown outcome end the streak honestly",
            empty_population_policy="no attempts returns null"),
        operands=(_card("authorization_event"), _auth_outcome(), _auth_ts()),
        source_grain="authorization_event", output_grain="card",
        temporal=_minutes_window("auth_ts"),
        readiness="FORMULA_BLOCKED", parameters=(_MINUTES_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="authorization attempts with a recorded outcome",
            excluded="booked transactions (a different stage)",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("auth_decline_streak", "extremum"),
        replaces_legacy_ids=("card_testing_velocity",)),

    # ── device continuity (correction 7) ────────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="device_sharing_velocity", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Distinct accounts seen on the device in the window, under the governed "
            "card/token/device continuity rules — a reissued card or family tablet is "
            "continuity, not sharing."),
        decision_context="device-sharing detection (pre-authorization)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="device_sharing_velocity", display_label="Accounts per device",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with no device fingerprint are excluded",
            empty_population_policy="no device events returns null"),
        operands=(
            OperandSpecV2(role="device", concept="device_fingerprint",
                          operand_class="entity_key",
                          allowed_source_grains=("session_event",)),
            OperandSpecV2(role="account", concept="account_id", operand_class="dimension",
                          allowed_source_grains=("session_event",)),
            _event_ts("session_event")),
        source_grain="session_event", output_grain="device",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="sessions under the continuity policy's identity rules",
            excluded="token rotations and reissues counted as new devices",
            policy_refs=(CARD_CONTINUITY,)),
        formula=_formula("device_sharing_velocity", "distinct_count"),
        replaces_legacy_ids=("device_sharing_velocity",)),
    RecipeDefinitionV2(
        recipe_id="new_device_flag", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Whether the session's device was never seen for this customer before the window, "
            "under the continuity rules."),
        decision_context="new-device signal (pre-authorization)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="new_device_flag", display_label="New device",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="events with no device fingerprint return null",
            empty_population_policy="no session history returns null — unknown, not new"),
        operands=(_customer("session_event"),
                  OperandSpecV2(role="device", concept="device_fingerprint",
                                operand_class="dimension",
                                allowed_source_grains=("session_event",)),
                  _event_ts("session_event")),
        source_grain="session_event", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="sessions under the continuity policy's identity rules",
            excluded="token rotations and reissues counted as new devices",
            policy_refs=(CARD_CONTINUITY,)),
        formula=_formula("new_device_flag", "flag"),
        replaces_legacy_ids=("new_device_flag",)),
    RecipeDefinitionV2(
        recipe_id="geo_velocity_impossible", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Whether two events place the customer at locations impossible to travel between "
            "in the elapsed time."),
        decision_context="impossible-travel signal (pre-authorization)",
        computation_kind="conceptual_pattern",
        conceptual_reason=(
            "Geographic distance over coordinates is outside the formula grammar's capability "
            "— computing it would need a geo engine the platform does not govern yet, and "
            "approximating with country pairs manufactures false impossibilities inside large "
            "countries."),
        output=OutputSpecV2(
            output_id="geo_velocity_impossible", display_label="Impossible travel",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="not applicable — conceptual pattern",
            empty_population_policy="not applicable — conceptual pattern"),
        operands=(_customer("session_event"),),
        source_grain="session_event", output_grain="customer",
        temporal=_days_window(),
        readiness="CONCEPTUAL_ONLY", parameters=(_WINDOW,),
        replaces_legacy_ids=("geo_velocity_impossible",)),

    # ── payee novelty on VERIFIED identity (correction 4) ───────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="first_time_payee_high_value", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Whether a payment above the governed threshold went to a BENEFICIARY IDENTITY "
            "never paid before the window — novelty on the verified payee record "
            "(beneficiary_id), never on the destination bank: one bank hosts thousands of "
            "payees."),
        decision_context="first-time-payee signal (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="first_time_payee_high_value", display_label="First-time payee high value",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="payments with no payee identity are excluded and surface as a "
                              "data gap",
            empty_population_policy="no payment history returns null — unknown, not first"),
        operands=(_customer("transaction"), _amount(), _event_ts(),
                  OperandSpecV2(role="payee", concept="beneficiary_id",
                                operand_class="entity_key",
                                allowed_source_grains=("transaction",),
                                relationship_requirement="the verified payee registry record, "
                                                         "never the beneficiary bank")),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED",
        parameters=(_WINDOW,
                    ParameterSpecV2(name="high_value_floor", parameter_class="governed_policy",
                                    identity_projection="floor={value}",
                                    display_projection="above {value}",
                                    governed_policy_ref="threshold:first-time-payee-value")),
        eligibility=EligibilitySpecV2(
            included="posted payments to registered payees above the governed floor",
            excluded="payments identified only by beneficiary bank",
            policy_refs=(FRAUD_ELIGIBLE, "threshold:first-time-payee-value")),
        formula=_formula("first_time_payee_high_value", "flag"),
        replaces_legacy_ids=("first_time_payee_high_value",)),

    # ── merchant anomaly is CUSTOMER-relative (correction 5) ────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="merchant_amount_zscore", revision=1, family="fraud",
        primary_objective=MERCHANT_FRAUD, supporting_objectives=(TXN_FRAUD,),
        business_definition=(
            "Z-score of the latest amount at the merchant against THIS CUSTOMER's own history "
            "at that merchant — the customer source entity is an operand, so the anomaly is "
            "customer-relative by construction."),
        decision_context="merchant anomaly (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="merchant_amount_zscore", display_label="Merchant amount z-score",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="fewer than two prior amounts returns null"),
        operands=(_customer("transaction"),
                  OperandSpecV2(role="merchant", concept="merchant_id",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _amount(), _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted card transactions with merchant identity",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("merchant_amount_zscore", "dispersion"),
        replaces_legacy_ids=("merchant_risk_anomaly",)),

    # ── the reviewed anchor keeps its Formula-v1 expectation (correction 9) ─────────────────────
    RecipeDefinitionV2(
        recipe_id="merchant_mcc_diversity", revision=1, family="fraud",
        primary_objective=MERCHANT_FRAUD,
        business_definition=(
            "Distinct MCC codes in the customer's posted card activity over the trailing "
            "window ending at the cutoff — the temporal wording the audit flagged, corrected: "
            "the window is anchored on event time, bounded by the declared window parameter."),
        decision_context="merchant-category breadth (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="merchant_mcc_diversity", display_label="MCC diversity",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with no MCC are excluded",
            empty_population_policy="no card activity returns zero"),
        operands=(_customer("transaction"),
                  OperandSpecV2(role="mcc", concept="mcc", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        # The ONE recipe in this pack with a REVIEWED expectation (the registry's Formula-v1
        # count-distinct blueprint, retained verbatim) — honestly authorable today.
        readiness="FORMULA_AUTHORABLE", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted card transactions carrying an MCC",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=FormulaReferenceV2(formula_schema_version="formula-v2",
                                   expectation_ref="merchant_mcc_diversity",
                                   result_class="distinct_count"),
        replaces_legacy_ids=("merchant_mcc_diversity",)),

    # ── booked-behaviour bursts (correction 8: atomic splits) ───────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="txn_velocity_spike", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Posted transaction count in the recent half of the window divided by the count "
            "in the prior half."),
        decision_context="velocity spike (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="txn_velocity_spike", display_label="Velocity spike ratio",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="ineligible rows are excluded, not nulled",
            empty_population_policy="an empty window returns null",
            zero_denominator_policy="a zero prior-half count returns null"),
        operands=(_customer("transaction"), _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions in an eligible status",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("txn_velocity_spike", "ratio"),
        replaces_legacy_ids=("txn_velocity_spike",)),
    RecipeDefinitionV2(
        recipe_id="amount_zscore_spike", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Z-score of the latest posted amount against the customer's window history."),
        decision_context="amount anomaly (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="amount_zscore_spike", display_label="Amount z-score",
            output_type="numeric", additivity="non_additive", unit_kind="score",
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="fewer than two prior amounts returns null"),
        operands=(_customer("transaction"), _amount(), _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_days_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions in an eligible status",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("amount_zscore_spike", "dispersion"),
        replaces_legacy_ids=("amount_zscore_spike",)),
    RecipeDefinitionV2(
        recipe_id="cross_channel_rail_burst", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Distinct payment rails/channels used in a short window — a burst across rails is "
            "the takeover cash-out shape."),
        decision_context="cross-rail burst (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cross_channel_rail_burst", display_label="Distinct rails in burst",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown rail are excluded",
            empty_population_policy="no activity returns zero"),
        operands=(_customer("transaction"),
                  OperandSpecV2(role="rail", concept="payment_rail", operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_minutes_window("event_ts"),
        readiness="FORMULA_BLOCKED", parameters=(_MINUTES_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted transactions in an eligible status",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("cross_channel_rail_burst", "distinct_count"),
        replaces_legacy_ids=("cross_channel_rail_anomaly",)),
    RecipeDefinitionV2(
        recipe_id="cross_border_burst", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Count of posted cross-border transactions in a short window."),
        decision_context="cross-border burst (post-booking)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cross_border_burst", display_label="Cross-border burst count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown country are excluded",
            empty_population_policy="no activity returns zero"),
        operands=(_customer("transaction"),
                  OperandSpecV2(role="country", concept="country_code",
                                operand_class="dimension",
                                allowed_source_grains=("transaction",)),
                  _event_ts()),
        source_grain="transaction", output_grain="customer",
        temporal=_minutes_window("event_ts"),
        readiness="FORMULA_BLOCKED", parameters=(_MINUTES_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted cross-border transactions",
            excluded="domestic transactions; reversed events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=_formula("cross_border_burst", "count"),
        replaces_legacy_ids=("cross_border_burst",)),

    # ── just-under-limit reads the GOVERNED control (correction 6) ──────────────────────────────
    RecipeDefinitionV2(
        recipe_id="amount_just_under_limit", revision=1, family="fraud",
        primary_objective=TXN_FRAUD,
        business_definition=(
            "Count of authorizations within the governed tolerance just below the EFFECTIVE "
            "authorization limit/control — the limit is a dated policy fact, never a guessed "
            "round number."),
        decision_context="limit-probing detection (post-authorization)",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="amount_just_under_limit", display_label="Just-under-limit count",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="attempts with unknown amount are excluded",
            empty_population_policy="no attempts returns zero"),
        operands=(_card("authorization_event"), _auth_outcome(), _auth_ts(),
                  OperandSpecV2(role="amount", concept="monetary_flow", operand_class="measure",
                                allowed_source_grains=("authorization_event",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="limit_control", concept="limit",
                                operand_class="policy_input",
                                allowed_source_grains=("authorization_event",),
                                status_policy_ref=LIMIT_POLICY)),
        source_grain="authorization_event", output_grain="card",
        temporal=_days_window("auth_ts"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="authorizations against the effective dated limit control",
            excluded="attempts compared to an undated or guessed limit",
            policy_refs=(FRAUD_ELIGIBLE, LIMIT_POLICY)),
        formula=_formula("amount_just_under_limit", "count"),
        replaces_legacy_ids=("amount_just_under_limit",)),
)
