"""SE-4 step 10 — the banking confusion battery: every known bad binding class, refused by name.

Each case is a REAL confusion from the plan's §8 walkthrough or the audit's defect classes —
the plausible-but-wrong bindings the legacy pipeline accepted. The fold must refuse each one
deterministically, without an LLM, with the honest status: a DIFFERENT MEANING is
``not_applicable`` (no action fixes it), a KNOWN contradiction is ``blocked``, and an
unproven-but-plausible binding stays ``provisional`` with the named action.

Two confusions in the plan's list need the SE-8 dataset axes (event-vs-snapshot TABLE shape;
current-vs-lifecycle history) and are covered here only in their column-level half — the
docstrings say so, because a test that silently covers less than its name is how gaps hide.
"""
from __future__ import annotations

from featuregen.overlay.upload.column_capabilities import ColumnCapabilityV1
from featuregen.overlay.upload.concept_operand_classes import OPERAND_CLASS_MAP_VERSION
from featuregen.overlay.upload.feature_planning_contracts import RequiredOperandV1
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.semantic_eligibility import evaluate_operand


def cap(**over) -> ColumnCapabilityV1:
    base = dict(
        object_ref="public.t.c", table="t", column="c",
        declared_type="numeric", type_family="numeric", is_grain=False, is_as_of=False,
        concept="monetary_flow", concept_authority="human/confirmed",
        identifier_namespace=None, identifier_like=False,
        possible_operand_classes=("measure",),
        operand_class_map_version=OPERAND_CLASS_MAP_VERSION,
        entity=None, entity_authority="absent",
        additivity=None, additivity_authority="absent",
        currency=None, currency_authority="absent",
        economic_role=None, economic_role_authority="absent",
        missing_context=("dataset_profile_absent", "relationship_state_absent",
                         "use_policy_absent"),
        retrieval_text="")
    base.update(over)
    return ColumnCapabilityV1(**base)


def op(**over) -> RequiredOperandV1:
    base = dict(role="probe", concept="monetary_flow", operand_class="measure")
    base.update(over)
    return RequiredOperandV1(**base)


# ── §8.2.8: a consent-modification timestamp is not customer activity ───────────────────────────

def test_consent_modification_never_serves_a_transaction_event_role():
    """`cust_cnsnt_mod_dt` means consent changed — a different MEANING, not a lesser event
    timestamp. (The deeper case — a consent date WRONGLY classified event_timestamp — is the
    business-event-subtype axis, SE-8; until then the confirmation funnel is the defense.)"""
    verdict = evaluate_operand(
        op(role="event_ts", concept="event_timestamp", operand_class="event_timestamp"),
        cap(object_ref="public.customers.cust_cnsnt_mod_dt", concept="consent_status",
            declared_type="timestamp", type_family="temporal",
            possible_operand_classes=("policy_input",)))
    assert verdict.status == "not_applicable"
    assert verdict.reason_codes == (R.CONCEPT_MISMATCH,)


# ── §8.2.9: KYC completion is not customer tenure/origination ───────────────────────────────────

def test_kyc_completion_never_serves_an_origination_role():
    verdict = evaluate_operand(
        op(role="origination", concept="origination_date", operand_class="event_timestamp"),
        cap(object_ref="public.customers.cust_kyc_complete_dt", concept="kyc_document",
            declared_type="date", type_family="temporal",
            possible_operand_classes=("dimension",)))
    assert verdict.status == "not_applicable"


# ── §8.2.10: a package code is not product holding/breadth ──────────────────────────────────────

def test_a_package_code_never_serves_a_product_holding_role():
    verdict = evaluate_operand(
        op(role="holding", concept="product_holding", operand_class="dimension"),
        cap(object_ref="public.customers.cust_smart_cust_pkg_cd", concept="product_type",
            declared_type="varchar(10)", type_family="text",
            possible_operand_classes=("dimension", "policy_input")))
    assert verdict.status == "not_applicable"


# ── balance versus flow: a stock is not a flow, whatever the column is named ────────────────────

def test_a_balance_stock_never_serves_a_flow_operand():
    verdict = evaluate_operand(
        op(role="amount", concept="monetary_flow"),
        cap(object_ref="public.accounts.balance", concept="monetary_stock",
            possible_operand_classes=("measure",)))
    assert verdict.status == "not_applicable"


# ── limit versus exposure: a limit is not drawn exposure; a stock without the governed role
#    is not drawn exposure EITHER — the two halves of the audit's worst class ──────────────────

def test_a_limit_never_serves_a_drawn_exposure_operand():
    verdict = evaluate_operand(
        op(role="drawn", concept="monetary_stock", economic_role="drawn_credit_exposure"),
        cap(object_ref="public.facilities.credit_limit", concept="limit",
            possible_operand_classes=("measure", "policy_input")))
    assert verdict.status == "not_applicable"                 # different concept entirely


def test_a_deposit_balance_is_blocked_for_drawn_exposure_without_the_governed_role():
    verdict = evaluate_operand(
        op(role="drawn", concept="monetary_stock", economic_role="drawn_credit_exposure"),
        cap(object_ref="public.accounts.balance", concept="monetary_stock",
            possible_operand_classes=("measure",)))           # right concept, unproven role
    assert verdict.status == "blocked"
    assert R.ECONOMIC_ROLE_UNPROVEN in verdict.reason_codes
    assert "human confirms" in verdict.resolution


# ── authorization versus settlement: two lifecycle stages, never interchangeable ────────────────

def test_authorization_status_never_serves_a_settlement_operand():
    verdict = evaluate_operand(
        op(role="settled", concept="settlement_status", operand_class="status"),
        cap(object_ref="public.payments.auth_status", concept="authorization_status",
            declared_type="varchar(12)", type_family="text",
            possible_operand_classes=("status",)))
    assert verdict.status == "not_applicable"


# ── snapshot date as event anchor: the column-level half ───────────────────────────────────────

def test_an_as_of_date_never_anchors_an_event_window():
    """The column half of "a snapshot cannot support an event window": an as-of date's concept
    is not an event timestamp. (The TABLE half — an event-classified column on a
    current-only snapshot — needs SE-8's dataset axes and is deliberately not claimed here.)"""
    verdict = evaluate_operand(
        op(role="event_ts", concept="event_timestamp", operand_class="event_timestamp"),
        cap(object_ref="public.customers.business_dt", concept="as_of_date",
            declared_type="date", type_family="temporal", is_as_of=True,
            possible_operand_classes=("as_of_timestamp", "event_timestamp")))
    assert verdict.status == "not_applicable"


# ── one bureau date is not bureau-event velocity (§3's cust_num neighborhood) ───────────────────

def test_a_single_snapshot_date_offered_as_a_measure_is_structurally_refused():
    verdict = evaluate_operand(
        op(role="inquiries", concept="bureau_inquiry"),
        cap(object_ref="public.customers.bureau_chk_dt", concept="bureau_inquiry",
            declared_type="date", type_family="temporal",
            possible_operand_classes=("measure",)))
    assert verdict.status == "blocked"                        # right meaning, impossible shape
    assert R.TYPE_INCOMPATIBLE in verdict.reason_codes
