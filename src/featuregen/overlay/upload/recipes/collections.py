"""BR-12 — the collections pack: 10 legacy templates → 14 atomic V2 recipes.

The corrections, structural: behaviour runs at CONTRACT (facility) grain with the customer
rollup a separate recipe; promises read the governed promise concepts (amount, due date,
outcome) — never a scheduled-amount stand-in; right-party contact reads contact ATTEMPT and
OUTCOME events, never the money spent chasing (the cost proxy is dead); cure, re-age and
roll-forward compare STATE AT WINDOW START AND END; the hardship lifecycle is its own event
read; recovery's denominator is the defaulted-balance/EAD snapshot, never a moving balance; and
recovery, write-off and cure OUTCOMES are leakage-classified ``outcome`` with origination and
default prediction prohibited — a pre-default model can never eat a post-default answer.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OperandSpecV2,
    OutputSpecV2,
    RecipeDefinitionV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipes.credit import (
    CREDIT_CCY,
    NEAR_LABEL_USE,
    _as_of,
    _event_ts,
    _facility,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

COLLECTIONS_SELF_CURE = "credit.collections.self_cure"
COLLECTIONS_WORKOUT = "credit.collections.workout"
COLLECTIONS_HARDSHIP = "credit.collections.hardship"
OBLIGOR_MONITORING = "credit.monitoring.obligor"

POST_DEFAULT_POP = "eligible_status:post-default-population"
RECOVERY_CCY = "currency_conversion:collections-recovery-currency"
PLAN_ALLOC = "allocation:payment-application-order"
CONTRACT_ROLLUP = "allocation:contract-to-customer-collections"

#: Post-default OUTCOMES: valid for collections and recovery analytics, leakage for anything
#: that predicts default — the plan's "never enter pre-default models", as a declaration.
POST_DEFAULT_ONLY = LeakageSpecV2(
    classification="outcome",
    permitted_stages=("collections", "recovery_modelling"),
    prohibited_stages=("origination", "default_prediction"))


def _formula(output_id: str, result_class: str) -> FormulaReferenceV2:
    return FormulaReferenceV2(formula_schema_version="formula-v2",
                              expectation_ref=f"collections:{output_id}",
                              result_class=result_class)


def _event_window(role: str = "event_ts") -> TemporalSpecV2:
    return TemporalSpecV2(anchor_kind="event", event_time_role=role,
                          window_basis="trailing", window_unit="days",
                          window_parameter="window", cutoff_inclusivity="inclusive")


def _promise_operands() -> tuple[OperandSpecV2, ...]:
    return (
        _facility("promise_event"),
        OperandSpecV2(role="promised_amount", concept="promise_amount",
                      operand_class="measure", allowed_source_grains=("promise_event",),
                      unit_expectation="monetary"),
        OperandSpecV2(role="promise_due", concept="promise_due_date",
                      operand_class="event_timestamp",
                      allowed_source_grains=("promise_event",)),
        OperandSpecV2(role="outcome", concept="promise_outcome", operand_class="status",
                      allowed_source_grains=("promise_event",)),
    )


_STATE_WINDOW = TemporalSpecV2(
    anchor_kind="as_of", business_effective_role="as_of_date",
    window_basis="trailing", window_unit="days", window_parameter="window",
    cutoff_inclusivity="inclusive",
    snapshot_policy="state read at the window START and at the cutoff — the transition is the "
                    "comparison of those two reads, never an any-time-in-window scan")


COLLECTIONS_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    # ── promises: kept share and amount-collected share (correction 2) ──────────────────────────
    RecipeDefinitionV2(
        recipe_id="promise_kept_share", revision=1, family="collections",
        primary_objective=COLLECTIONS_SELF_CURE,
        business_definition=(
            "Promises with a KEPT outcome divided by promises falling due in the window — the "
            "outcome read from the governed promise record, never inferred from payments."),
        decision_context="promise reliability",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="promise_kept_share", display_label="Promises kept",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="promises with no recorded outcome are excluded and surface as "
                              "a data gap",
            empty_population_policy="no promises due in the window returns null",
            zero_denominator_policy="zero promises due returns null"),
        operands=_promise_operands(),
        source_grain="promise_event", output_grain="facility",
        temporal=_event_window("promise_due"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=_formula("promise_kept_share", "share"),
        replaces_legacy_ids=("promise_to_pay_adherence",)),
    RecipeDefinitionV2(
        recipe_id="promise_amount_collected_share", revision=1, family="collections",
        primary_objective=COLLECTIONS_SELF_CURE,
        business_definition=(
            "Amount collected against promises due in the window divided by the amount "
            "PROMISED — a broken promise partially paid counts its partial payment, which is "
            "exactly why this is a separate output from the kept/broken count."),
        decision_context="promise cash effectiveness",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="promise_amount_collected_share",
            display_label="Promised amount collected",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="promises missing the promised amount are excluded",
            empty_population_policy="no promises due in the window returns null",
            zero_denominator_policy="a zero promised amount returns null"),
        operands=(*_promise_operands(),
                  OperandSpecV2(role="paid", concept="monetary_flow", operand_class="measure",
                                allowed_source_grains=("promise_event",),
                                unit_expectation="monetary",
                                economic_role="loan_repayment")),
        source_grain="promise_event", output_grain="facility",
        temporal=_event_window("promise_due"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="payments allocated to the promise under the governed application order",
            excluded="payments allocated to fees counted as promise performance",
            policy_refs=(PLAN_ALLOC, RECOVERY_CCY)),
        formula=_formula("promise_amount_collected_share", "share"),
        replaces_legacy_ids=("promise_to_pay_adherence",)),

    # ── payment-plan adherence over the arrangement schedule (correction 3) ─────────────────────
    RecipeDefinitionV2(
        recipe_id="plan_installments_met_streak", revision=1, family="collections",
        primary_objective=COLLECTIONS_SELF_CURE,
        business_definition=(
            "Consecutive arrangement installments met in full under the governed payment "
            "allocation — read from the PLAN's schedule (due date, amount), never from raw "
            "payment flows."),
        decision_context="arrangement adherence",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="plan_installments_met_streak", display_label="Plan installments met",
            output_type="integer", additivity="non_additive", unit_kind="count",
            null_input_policy="installments missing schedule fields end the streak and "
                              "surface as a data gap",
            empty_population_policy="no arrangement installments in the window returns null"),
        operands=(_facility("arrangement_schedule"),
                  OperandSpecV2(role="due_date", concept="due_date",
                                operand_class="event_timestamp",
                                allowed_source_grains=("arrangement_schedule",)),
                  OperandSpecV2(role="amount_due", concept="scheduled_amount",
                                operand_class="measure",
                                allowed_source_grains=("arrangement_schedule",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="allocation", concept="payment_allocation",
                                operand_class="policy_input",
                                allowed_source_grains=("arrangement_schedule",),
                                status_policy_ref=PLAN_ALLOC)),
        source_grain="arrangement_schedule", output_grain="facility",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="due_date",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="arrangement installments with due date, amount and allocation declared",
            excluded="unscheduled payments; fee allocations counted as installment "
                     "performance",
            policy_refs=(PLAN_ALLOC,)),
        formula=_formula("plan_installments_met_streak", "extremum"),
        replaces_legacy_ids=("payment_plan_adherence",)),

    # ── cure / re-age and roll-forward: state at start vs end (corrections 5) ───────────────────
    RecipeDefinitionV2(
        recipe_id="cured_in_window_flag", revision=1, family="collections",
        primary_objective=COLLECTIONS_SELF_CURE,
        business_definition=(
            "Whether the contract's delinquency state at the cutoff is CURRENT while its state "
            "at the window start was delinquent — a cure is the comparison of two reads, so a "
            "cure followed by re-default inside the window honestly reads NOT cured."),
        decision_context="cure analytics — an OUTCOME, prohibited pre-default",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cured_in_window_flag", display_label="Cured in window",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="a missing state at either end returns null",
            empty_population_policy="no known state returns null"),
        operands=(_facility("facility_day_snapshot"),
                  OperandSpecV2(role="bucket", concept="delinquency_bucket",
                                operand_class="status",
                                allowed_source_grains=("facility_day_snapshot",)),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_STATE_WINDOW,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        formula=_formula("cured_in_window_flag", "flag"),
        replaces_legacy_ids=("cure_reage_dynamics",)),
    RecipeDefinitionV2(
        recipe_id="rolled_forward_flag", revision=1, family="collections",
        primary_objective=OBLIGOR_MONITORING,
        business_definition=(
            "Whether the delinquency bucket at the cutoff is WORSE than at the window start — "
            "roll-forward as a two-read comparison."),
        decision_context="roll-rate monitoring — near-label",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="rolled_forward_flag", display_label="Rolled forward",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="a missing state at either end returns null",
            empty_population_policy="no known state returns null"),
        operands=(_facility("facility_day_snapshot"),
                  OperandSpecV2(role="bucket", concept="delinquency_bucket",
                                operand_class="status",
                                allowed_source_grains=("facility_day_snapshot",)),
                  _as_of("facility_day_snapshot")),
        source_grain="facility_day_snapshot", output_grain="facility",
        temporal=_STATE_WINDOW,
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        formula=_formula("rolled_forward_flag", "flag"),
        replaces_legacy_ids=("roll_forward_severity",)),

    # ── contact effectiveness: attempts and RPC rate, never cost (correction 4) ─────────────────
    RecipeDefinitionV2(
        recipe_id="contact_attempt_count", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition="Count of outbound contact ATTEMPTS on the contract in the window.",
        decision_context="collections activity volume",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="contact_attempt_count", display_label="Contact attempts",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="events with unknown kind are excluded",
            empty_population_policy="no attempts returns zero"),
        operands=(_facility("contact_event"),
                  OperandSpecV2(role="attempt", concept="contact_attempt_event",
                                operand_class="dimension",
                                allowed_source_grains=("contact_event",)),
                  _event_ts("contact_event")),
        source_grain="contact_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=_formula("contact_attempt_count", "count"),
        replaces_legacy_ids=("right_party_contact_intensity",)),
    RecipeDefinitionV2(
        recipe_id="right_party_contact_rate", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "Contacts whose outcome reached the RIGHT PARTY divided by contact attempts in the "
            "window — read from contact outcome events; the money spent chasing "
            "(cost_to_collect) is a different fact and never a proxy for this one."),
        decision_context="collections contact effectiveness",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="right_party_contact_rate", display_label="Right-party contact rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="attempts with no recorded outcome count as not-reached, "
                              "never dropped",
            empty_population_policy="no attempts in the window returns null",
            zero_denominator_policy="zero attempts returns null"),
        operands=(_facility("contact_event"),
                  OperandSpecV2(role="attempt", concept="contact_attempt_event",
                                operand_class="dimension",
                                allowed_source_grains=("contact_event",)),
                  OperandSpecV2(role="outcome", concept="contact_outcome",
                                operand_class="status",
                                allowed_source_grains=("contact_event",)),
                  OperandSpecV2(role="rpc", concept="right_party_contact_flag",
                                operand_class="status",
                                allowed_source_grains=("contact_event",)),
                  _event_ts("contact_event")),
        source_grain="contact_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=_formula("right_party_contact_rate", "ratio"),
        replaces_legacy_ids=("right_party_contact_intensity",)),

    # ── time in collection, contract grain + the separate customer rollup (correction 1) ────────
    RecipeDefinitionV2(
        recipe_id="days_in_collection", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "Days between the contract's entry into collection (its status change event) and "
            "the cutoff."),
        decision_context="workout ageing",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="days_in_collection", display_label="Days in collection",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="contracts with no recorded entry event return null",
            empty_population_policy="not in collection returns null — zero would read as "
                                    "'entered today'"),
        operands=(_facility("collection_status_event"),
                  OperandSpecV2(role="status", concept="account_status",
                                operand_class="status",
                                allowed_source_grains=("collection_status_event",)),
                  _event_ts("collection_status_event")),
        source_grain="collection_status_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        formula=_formula("days_in_collection", "recency"),
        replaces_legacy_ids=("days_in_collection",)),
    RecipeDefinitionV2(
        recipe_id="customer_worst_days_in_collection", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "The WORST days-in-collection across a customer's contracts — the separate "
            "customer rollup the plan orders, correct at contract grain FIRST and rolled up "
            "under the governed contract-to-customer policy."),
        decision_context="customer-level workout view",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="customer_worst_days_in_collection",
            display_label="Worst days in collection (customer)",
            output_type="numeric", additivity="non_additive", unit_kind="duration_days",
            null_input_policy="contracts with no recorded entry event are excluded",
            empty_population_policy="no contracts in collection returns null"),
        operands=(
            OperandSpecV2(role="customer", concept="customer_id", operand_class="entity_key",
                          allowed_source_grains=("collection_status_event",)),
            _facility("collection_status_event"),
            OperandSpecV2(role="status", concept="account_status", operand_class="status",
                          allowed_source_grains=("collection_status_event",)),
            _event_ts("collection_status_event")),
        source_grain="collection_status_event", output_grain="customer",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="every contract of the customer currently in collection",
            excluded="contracts of other household members — the rollup key is the customer",
            policy_refs=(CONTRACT_ROLLUP,)),
        formula=_formula("customer_worst_days_in_collection", "extremum"),
        replaces_legacy_ids=("days_in_collection",)),

    # ── hardship lifecycle (correction 6) ───────────────────────────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="hardship_arrangement_in_window", revision=1, family="collections",
        primary_objective=COLLECTIONS_HARDSHIP,
        business_definition=(
            "Whether a hardship ARRANGEMENT became effective in the window — one stage of the "
            "request → assessment → arrangement → outcome lifecycle, read from lifecycle "
            "events, never inferred from payment shapes."),
        decision_context="hardship monitoring — near-label",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="hardship_arrangement_in_window",
            display_label="Hardship arrangement in window",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="events with unknown lifecycle stage are excluded",
            empty_population_policy="no hardship events returns false"),
        operands=(_facility("hardship_event"),
                  OperandSpecV2(role="stage", concept="lifecycle_state",
                                operand_class="status",
                                allowed_source_grains=("hardship_event",)),
                  OperandSpecV2(role="event_kind", concept="event_type",
                                operand_class="dimension",
                                allowed_source_grains=("hardship_event",)),
                  _event_ts("hardship_event")),
        source_grain="hardship_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=NEAR_LABEL_USE,
        formula=_formula("hardship_arrangement_in_window", "flag"),
        replaces_legacy_ids=("hardship_forbearance_in_collection",)),

    # ── cost efficiency (kept as cost — never a contact proxy) ──────────────────────────────────
    RecipeDefinitionV2(
        recipe_id="cost_to_collect_ratio", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "Collections/workout cost booked against the contract in the window divided by the "
            "defaulted-balance snapshot — cost EFFICIENCY, a fact about money spent; contact "
            "effectiveness lives in the contact recipes."),
        decision_context="workout cost efficiency",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cost_to_collect_ratio", display_label="Cost to collect",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            null_input_policy="null cost rows are excluded per the source policy",
            empty_population_policy="no cost in the window returns zero",
            zero_denominator_policy="a zero defaulted balance returns null"),
        operands=(_facility("workout_cost_event"),
                  OperandSpecV2(role="cost", concept="cost_to_collect",
                                operand_class="measure",
                                allowed_source_grains=("workout_cost_event",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="defaulted_balance", concept="ead",
                                operand_class="measure",
                                allowed_source_grains=("workout_cost_event",),
                                unit_expectation="monetary",
                                economic_role="defaulted_balance_snapshot"),
                  _event_ts("workout_cost_event")),
        source_grain="workout_cost_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        eligibility=EligibilitySpecV2(
            included="workout costs booked against the defaulted contract",
            excluded="pre-default servicing cost",
            policy_refs=(POST_DEFAULT_POP, CREDIT_CCY)),
        formula=_formula("cost_to_collect_ratio", "ratio"),
        replaces_legacy_ids=("cost_to_collect_ratio",)),

    # ── recovery and write-off: post-default only, snapshot denominator (corrections 7-9) ───────
    RecipeDefinitionV2(
        recipe_id="recovery_rate", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "Post-default recoveries collected in the window divided by the DEFAULTED-BALANCE "
            "SNAPSHOT taken at default — the denominator is frozen at default, so recoveries "
            "in any later period divide by the same number; multi-currency recoveries convert "
            "under the governed recovery-currency policy."),
        decision_context="recovery analytics — an OUTCOME, prohibited pre-default",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="recovery_rate", display_label="Recovery rate",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="recoveries with no default linkage are excluded",
            empty_population_policy="no recoveries in the window returns zero — a real "
                                    "answer post-default",
            zero_denominator_policy="a zero defaulted balance returns null"),
        operands=(_facility("recovery_event"),
                  OperandSpecV2(role="recovery", concept="recovery_amount",
                                operand_class="measure",
                                allowed_source_grains=("recovery_event",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="defaulted_balance", concept="ead",
                                operand_class="measure",
                                allowed_source_grains=("recovery_event",),
                                unit_expectation="monetary",
                                economic_role="defaulted_balance_snapshot"),
                  _event_ts("recovery_event")),
        source_grain="recovery_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        eligibility=EligibilitySpecV2(
            included="post-default recovery flows linked to the default event",
            excluded="pre-default payments; recoveries counted against a moving balance",
            policy_refs=(POST_DEFAULT_POP, RECOVERY_CCY)),
        formula=_formula("recovery_rate", "ratio"),
        replaces_legacy_ids=("recovery_rate",)),
    RecipeDefinitionV2(
        recipe_id="write_off_amount_sum", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition="Write-off amounts charged against the contract in the window.",
        decision_context="write-off sizing — an OUTCOME, prohibited pre-default",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="write_off_amount_sum", display_label="Write-off amount",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units", currency_policy=CREDIT_CCY,
            null_input_policy="null amounts are excluded per the source policy",
            empty_population_policy="no write-offs returns zero",
            aggregation_over_entity="sum across contracts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(_facility("write_off_event"),
                  OperandSpecV2(role="write_off", concept="write_off_amount",
                                operand_class="measure",
                                allowed_source_grains=("write_off_event",),
                                unit_expectation="monetary"),
                  _event_ts("write_off_event")),
        source_grain="write_off_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        eligibility=EligibilitySpecV2(
            included="write-off events on the defaulted contract",
            excluded="provisions (an expectation is not a charge-off)",
            policy_refs=(POST_DEFAULT_POP, CREDIT_CCY)),
        formula=_formula("write_off_amount_sum", "sum"),
        replaces_legacy_ids=("write_off_severity",)),
    RecipeDefinitionV2(
        recipe_id="write_off_severity_share", revision=1, family="collections",
        primary_objective=COLLECTIONS_WORKOUT,
        business_definition=(
            "Write-off amount divided by the defaulted-balance snapshot — severity as a share "
            "of what was owed at default."),
        decision_context="severity analytics — an OUTCOME, prohibited pre-default",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="write_off_severity_share", display_label="Write-off severity",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, ~]",
            null_input_policy="write-offs with no default linkage are excluded",
            empty_population_policy="no write-offs in the window returns null",
            zero_denominator_policy="a zero defaulted balance returns null"),
        operands=(_facility("write_off_event"),
                  OperandSpecV2(role="write_off", concept="write_off_amount",
                                operand_class="measure",
                                allowed_source_grains=("write_off_event",),
                                unit_expectation="monetary"),
                  OperandSpecV2(role="defaulted_balance", concept="ead",
                                operand_class="measure",
                                allowed_source_grains=("write_off_event",),
                                unit_expectation="monetary",
                                economic_role="defaulted_balance_snapshot"),
                  _event_ts("write_off_event")),
        source_grain="write_off_event", output_grain="facility",
        temporal=_event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=POST_DEFAULT_ONLY,
        eligibility=EligibilitySpecV2(
            included="write-off events linked to the default's balance snapshot",
            excluded="severities computed against a moving balance",
            policy_refs=(POST_DEFAULT_POP, CREDIT_CCY)),
        formula=_formula("write_off_severity_share", "ratio"),
        replaces_legacy_ids=("write_off_severity",)),
)
