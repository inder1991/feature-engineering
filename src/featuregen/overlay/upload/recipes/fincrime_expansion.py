"""BR-21 Priority C — the financial-crime expansion pack: 8 recipes, the last coverage gaps.

Each typology leaf gets an honest atomic primitive answering a named decision — never a
composite score wearing a recipe: CNP share is the card-fraud exposure surface; the
account-takeover shape is a credential change followed by a new-payee payment, two named
event reads; APP-scam value flows to first-time payees under the verified payee registry;
synthetic identity reads the thin-file flag WITH bureau knowledge time; the structuring leaf
gets sub-threshold cash PERSISTENCE (days, beside the smurfing count); sanctions and screening
get their CONTROL-STATE facts (open hits pending disposition, screening coverage); and
correspondent banking gets its due-diligence review state. The mule-account and TBML leaves
stay INTENTIONALLY EMPTY — declared future by the taxonomy owner, untouched here.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    LeakageSpecV2,
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
    status,
)
from featuregen.overlay.upload.recipes.retail import _WINDOW

CARD_FRAUD = "fraud.card_fraud"
ATO = "fraud.account_takeover"
APP_SCAM = "fraud.app_scam"
SYNTHETIC = "fraud.synthetic_id"
STRUCTURING = "aml_cft.structuring"
SANCTIONS = "aml_cft.sanctions"
SCREENING = "aml_cft.screening"
CORRESPONDENT = "aml_cft.correspondent"

FRAUD_ELIGIBLE = "eligible_status:fraud-eligible-events"
AML_ELIGIBLE = "eligible_status:aml-posted-events"
REPORTING_THRESHOLD = "threshold:aml-reporting-jurisdictional"
BUREAU_KNOWLEDGE = "business_calendar:bureau-pull-knowledge-time"

#: Alert/disposition facts arrive late and border their labels — near-label, time-lagged.
_ALERT_USE = LeakageSpecV2(
    classification="near_label",
    permitted_stages=("monitoring", "investigation_support"),
    prohibited_stages=("origination", "sar_prediction"))


FINCRIME_EXPANSION_RECIPES: tuple[RecipeDefinitionV2, ...] = (
    RecipeDefinitionV2(
        recipe_id="cnp_transaction_share", revision=1, family="fincrime_expansion",
        primary_objective=CARD_FRAUD,
        business_definition=("Card-not-present transactions as a share of the card's posted "
                             "activity over the window — the card-fraud exposure surface, "
                             "read from the channel/entry-mode dimension."),
        decision_context="how exposed is this card to CNP fraud",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="cnp_transaction_share", display_label="CNP share",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="rows with unknown entry mode are excluded from the "
                              "numerator, kept in the denominator",
            empty_population_policy="no card activity returns null",
            zero_denominator_policy="zero activity returns null"),
        operands=(entity("card", "card_id", "transaction"),
                  dim("channel", "channel", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="card",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="posted card transactions with entry-mode identity",
            excluded="reversed and technical events",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=formula("fincrime", "cnp_transaction_share", "share")),
    RecipeDefinitionV2(
        recipe_id="credential_change_then_payment_flag", revision=1,
        family="fincrime_expansion",
        primary_objective=ATO,
        business_definition=("Whether a credential/contact-detail change was followed inside "
                             "the window by a payment to a first-time payee — the "
                             "account-takeover shape as two NAMED event reads, never a "
                             "composite score."),
        decision_context="does this account show the takeover sequence",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="credential_change_then_payment_flag",
            display_label="Credential change then new payee",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="events with unknown kind are excluded",
            empty_population_policy="no session history returns null — unknown, not safe"),
        operands=(entity("account", "account_id", "session_event"),
                  dim("event_kind", "event_type", "session_event"),
                  dim("payee", "beneficiary_id", "session_event", required=False),
                  event_ts("session_event")),
        source_grain="session_event", output_grain="account",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="credential-change and payment events in sequence",
            excluded="either event alone (the sequence IS the signal)",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=formula("fincrime", "credential_change_then_payment_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="authorized_push_new_payee_amount", revision=1,
        family="fincrime_expansion",
        primary_objective=APP_SCAM,
        business_definition=("Authorized push-payment value to FIRST-TIME payees over the "
                             "window — the APP-scam value surface, payee novelty on the "
                             "verified registry, never the bank."),
        decision_context="how much authorized value is flowing to new payees",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="authorized_push_new_payee_amount",
            display_label="New-payee push value",
            output_type="numeric", additivity="additive", unit_kind="monetary",
            unit_policy="base currency units",
            currency_policy="currency_conversion:foundation-base-currency",
            null_input_policy="payments with no payee identity are excluded and surface as "
                              "a gap",
            empty_population_policy="no push payments returns zero",
            aggregation_over_entity="sum across accounts",
            aggregation_over_time="sum over disjoint windows"),
        operands=(entity("customer", "customer_id", "transaction"),
                  measure("amount", "monetary_flow", "transaction"),
                  dim("payee", "beneficiary_id", "transaction"),
                  dim("rail", "payment_rail", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="customer-authorized push payments to registry-identified payees",
            excluded="payments identified only by beneficiary bank; unauthorized (card) "
                     "fraud — a different typology",
            policy_refs=(FRAUD_ELIGIBLE,)),
        formula=formula("fincrime", "authorized_push_new_payee_amount", "sum")),
    RecipeDefinitionV2(
        recipe_id="thin_file_rapid_acquisition_flag", revision=1,
        family="fincrime_expansion",
        primary_objective=SYNTHETIC,
        business_definition=("Whether a THIN-FILE customer (bureau file below the governed "
                             "depth, as known at pull) acquired multiple products inside the "
                             "window — the synthetic-identity shape: a real person's history "
                             "is hard to fake, a shopping spree is not."),
        decision_context="does this new identity look manufactured",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="thin_file_rapid_acquisition_flag",
            display_label="Thin file, rapid acquisition",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="customers with no bureau pull return null — unknown, never "
                              "assumed thick",
            empty_population_policy="no bureau coverage returns null"),
        operands=(entity("customer", "customer_id", "bureau_pull"),
                  status("thin_file", "thin_file_flag", "bureau_pull"),
                  dim("knowledge_ts", "system_time", "bureau_pull"),
                  event_ts("bureau_pull", role="pull_ts")),
        source_grain="bureau_pull", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="pull_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="bureau pulls known at or before the cutoff, with product-acquisition "
                     "events",
            excluded="pulls learned after the cutoff",
            policy_refs=(BUREAU_KNOWLEDGE,)),
        formula=formula("fincrime", "thin_file_rapid_acquisition_flag", "flag")),
    RecipeDefinitionV2(
        recipe_id="sub_threshold_cash_day_count", revision=1, family="fincrime_expansion",
        primary_objective=STRUCTURING,
        business_definition=("Days in the window with sub-threshold cash activity — "
                             "structuring PERSISTENCE, beside the smurfing count: a pattern "
                             "held for weeks is a different fact from one busy afternoon."),
        decision_context="is sub-threshold cash behaviour persistent",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sub_threshold_cash_day_count",
            display_label="Sub-threshold cash days",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="rows with unknown channel/instrument are excluded",
            empty_population_policy="no cash activity returns zero"),
        operands=(entity("customer", "customer_id", "transaction"),
                  measure("amount", "monetary_flow", "transaction"),
                  dim("channel", "channel", "transaction"),
                  dim("instrument", "instrument_type", "transaction"),
                  policy_input("reporting_threshold", "limit", "transaction",
                               policy=REPORTING_THRESHOLD),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="cash-channel/instrument activity against the threshold effective at "
                     "each event date",
            excluded="non-cash channels; thresholds read across jurisdictions",
            policy_refs=(AML_ELIGIBLE, REPORTING_THRESHOLD)),
        formula=formula("fincrime", "sub_threshold_cash_day_count", "count")),
    RecipeDefinitionV2(
        recipe_id="sanctions_hit_pending_count", revision=1, family="fincrime_expansion",
        primary_objective=SANCTIONS,
        business_definition=("Open sanctions hits PENDING disposition at the cutoff, as "
                             "known then — the sanctions control's queue state, read through "
                             "knowledge time."),
        decision_context="what sanctions exposure is sitting undispositioned",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="sanctions_hit_pending_count", display_label="Pending sanctions hits",
            output_type="integer", additivity="additive", unit_kind="count",
            null_input_policy="hits with unknown knowledge time are excluded",
            empty_population_policy="no screening coverage returns null — absence of feed "
                                    "is not zero hits"),
        operands=(entity("customer", "customer_id", "alert_event"),
                  status("hit", "sanctions_hit_flag", "alert_event"),
                  dim("knowledge_ts", "system_time", "alert_event"),
                  event_ts("alert_event")),
        source_grain="alert_event", output_grain="customer",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="event_ts",
                                knowledge_time_role="knowledge_ts",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        leakage=_ALERT_USE,
        formula=formula("fincrime", "sanctions_hit_pending_count", "count")),
    RecipeDefinitionV2(
        recipe_id="screening_coverage_share", revision=1, family="fincrime_expansion",
        primary_objective=SCREENING,
        business_definition=("Counterparties actually SCREENED as a share of counterparties "
                             "transacted with — the screening control's coverage, a "
                             "different fact from what screening found."),
        decision_context="is the screening control actually covering the book",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="screening_coverage_share", display_label="Screening coverage",
            output_type="numeric", additivity="non_additive", unit_kind="ratio",
            valid_range="[0, 1]",
            null_input_policy="counterparties with unknown screening state count as "
                              "UNSCREENED, never dropped",
            empty_population_policy="no counterparties returns null",
            zero_denominator_policy="zero counterparties returns null"),
        operands=(entity("customer", "customer_id", "transaction"),
                  dim("counterparty", "customer_id", "transaction"),
                  status("screened", "watchlist_hit_flag", "transaction"),
                  event_ts("transaction")),
        source_grain="transaction", output_grain="customer",
        temporal=event_window(),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="transacted counterparties and their screening state",
            excluded="unknown-state counterparties counted as screened",
            policy_refs=(AML_ELIGIBLE,)),
        formula=formula("fincrime", "screening_coverage_share", "share")),
    RecipeDefinitionV2(
        recipe_id="correspondent_dd_overdue_flag", revision=1, family="fincrime_expansion",
        primary_objective=CORRESPONDENT,
        business_definition=("Whether the respondent bank's correspondent due-diligence "
                             "review is past its due date at the cutoff — the correspondent "
                             "relationship's CDD state."),
        decision_context="is this correspondent relationship's DD current",
        computation_kind="deterministic_formula",
        output=OutputSpecV2(
            output_id="correspondent_dd_overdue_flag", display_label="Correspondent DD overdue",
            output_type="boolean", additivity="non_additive", unit_kind="count",
            null_input_policy="respondents with no review schedule return null — unknown, "
                              "never assumed current",
            empty_population_policy="no DD schedule coverage returns null"),
        operands=(entity("respondent_bank", "bank_bic", "dd_review_schedule"),
                  dim("dd_state", "kyc_document", "dd_review_schedule"),
                  event_ts("dd_review_schedule", role="review_due", concept="due_date")),
        source_grain="dd_review_schedule", output_grain="respondent_bank",
        temporal=TemporalSpecV2(anchor_kind="event", event_time_role="review_due",
                                window_basis="trailing", window_unit="days",
                                window_parameter="window", cutoff_inclusivity="inclusive"),
        readiness="FORMULA_BLOCKED", parameters=(_WINDOW,),
        eligibility=EligibilitySpecV2(
            included="scheduled correspondent DD reviews and due dates",
            excluded="DD currency assumed with no schedule"),
        formula=formula("fincrime", "correspondent_dd_overdue_flag", "flag")),
)
