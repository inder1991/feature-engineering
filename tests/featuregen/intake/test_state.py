from dataclasses import dataclass

from featuregen.intake import events
from featuregen.intake.state import (
    FeatureContractState,
    FeatureContractStatus,
    actor_is_request_owner,
    fold_feature_contract_state,
)


@dataclass
class _Actor:
    subject: str


@dataclass
class _Evt:
    type: str
    event_id: str
    payload: dict
    actor: _Actor | None = None  # SP-0 EventEnvelope.actor (IdentityEnvelope with .subject)


def _submitted(eid="evt_sub", subject="user:raj"):
    return _Evt(
        events.INTENT_SUBMITTED,
        eid,
        {
            "request_id": "req_1",
            "run_id": "run_1",
            "intake_mode": "definition",
            "catalog_version": "banking-cat@1",
        },
        actor=_Actor(subject=subject),
    )


def _produced(open_fields=("filters.declined_status_encoding",), candidates=()):
    return _Evt(
        events.DRAFT_CONTRACT_PRODUCED,
        "evt_prod",
        {
            "draft_doc_id": "doc_draft1",
            "assumption_ledger_ref": "doc_led1",
            "open_fields": list(open_fields),
            "candidate_doc_ids": list(candidates),
        },
    )


def test_empty_stream_is_unopened():
    st = fold_feature_contract_state([])
    assert isinstance(st, FeatureContractState)
    assert st.status is None
    assert st.open_fields == ()
    assert not st.is_terminal


def test_submit_then_draft_is_needs_clarification_with_open_fields():
    st = fold_feature_contract_state([_submitted(), _produced()])
    assert st.status is FeatureContractStatus.NEEDS_CLARIFICATION
    assert st.open_fields == ("filters.declined_status_encoding",)
    assert st.request_id == "req_1"
    assert st.run_id == "run_1"
    assert st.intake_mode == "definition"
    assert st.draft_doc_id == "doc_draft1"
    assert st.assumption_ledger_ref == "doc_led1"
    assert st.catalog_version == "banking-cat@1"
    assert st.requester == "user:raj"
    assert not st.mcv_passed


def test_requester_is_the_intent_submitted_event_actor_and_owner_predicate():
    # R4 — the request owner is folded from the INTENT_SUBMITTED event's actor.subject, and the ONE
    # owner predicate P5–P8 call compares an acting principal's .subject to it.
    st = fold_feature_contract_state([_submitted(subject="user:raj")])
    assert st.requester == "user:raj"
    assert actor_is_request_owner(st, _Actor("user:raj"))
    assert not actor_is_request_owner(st, _Actor("user:mallory"))


def test_answering_and_auto_resolving_clears_open_fields():
    answered = _Evt(
        events.CLARIFICATION_ANSWERED, "evt_ans", {"field": "filters.declined_status_encoding"}
    )
    st = fold_feature_contract_state([_submitted(), _produced(), answered])
    assert st.open_fields == ()
    resolved = _Evt(events.FIELD_AUTO_RESOLVED, "evt_ar", {"field": "entity_grain"})
    st2 = fold_feature_contract_state(
        [_submitted(), _produced(open_fields=("entity_grain",)), resolved]
    )
    assert st2.open_fields == ()


def test_mcv_then_confirm_advances_status():
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    st_mcv = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv])
    assert st_mcv.status is FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED
    assert st_mcv.mcv_passed
    conf = _Evt(
        events.CONTRACT_CONFIRMED,
        "evt_conf",
        {"confirmed_doc_id": "doc_conf1", "confirmed_by": "user:raj"},
    )
    st_conf = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv, conf])
    assert st_conf.status is FeatureContractStatus.CONFIRMED
    assert st_conf.is_confirmed
    assert st_conf.confirmed_doc_id == "doc_conf1"
    assert st_conf.confirmed_by == "user:raj"


def test_edit_reopening_a_field_drops_back_from_mcv_to_needs_clarification():
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    refined = _Evt(
        events.CONTRACT_REFINED,
        "evt_ref",
        {"draft_doc_id": "doc_draft2", "open_fields": ["calculation_method"]},
    )
    st = fold_feature_contract_state([_submitted(), _produced(open_fields=()), mcv, refined])
    assert st.status is FeatureContractStatus.NEEDS_CLARIFICATION
    assert st.open_fields == ("calculation_method",)
    assert st.draft_doc_id == "doc_draft2"


def test_intent_rejected_folds_to_the_carried_classification():
    rej = _Evt(
        events.INTENT_REJECTED,
        "evt_rej",
        {
            "classification": "PROHIBITED_DATA_CLASS",
            "matched_class": "protected_attribute",
            "catalog_version": "banking-cat@1",
        },
    )
    st = fold_feature_contract_state([_submitted(), rej])
    assert st.status is FeatureContractStatus.PROHIBITED_DATA_CLASS
    assert st.is_terminal
    assert st.matched_class == "protected_attribute"
    assert st.classification == "PROHIBITED_DATA_CLASS"


def test_onboarding_request_folds_to_the_onboarding_hold_status():
    # NEEDS_USE_CASE_ONBOARDING is a folded feature_contract status (from USE_CASE_ONBOARDING_REQUESTED),
    # NOT a waiting_on_fact park — and it is a non-terminal HOLD, not a terminal reject.
    onb = _Evt(
        events.USE_CASE_ONBOARDING_REQUESTED, "evt_onb", {"catalog_version": "banking-cat@1"}
    )
    st = fold_feature_contract_state([_submitted(), onb])
    assert st.status is FeatureContractStatus.NEEDS_USE_CASE_ONBOARDING
    assert (
        not st.is_terminal
    )  # a hold, not a terminal reject (contrast OUT_OF_SCOPE / PROHIBITED_DATA_CLASS)


def test_no_regression_guard_locks_confirmed_and_terminal_states():
    # a stray re-advance AFTER CONFIRMED must be ignored (mirrors overlay's defensive fold)
    mcv = _Evt(events.MINIMUM_CONTRACT_VALIDATED, "evt_mcv", {})
    conf = _Evt(events.CONTRACT_CONFIRMED, "evt_conf", {"confirmed_doc_id": "doc_conf1"})
    stray_refine = _Evt(events.CONTRACT_REFINED, "evt_ref2", {"open_fields": ["x"]})
    st = fold_feature_contract_state(
        [_submitted(), _produced(open_fields=()), mcv, conf, stray_refine]
    )
    assert st.status is FeatureContractStatus.CONFIRMED
    assert st.open_fields == ()
    # a stray DRAFT after a terminal rejection must not re-open the contract
    rej = _Evt(events.INTENT_REJECTED, "evt_rej", {"classification": "OUT_OF_SCOPE"})
    st2 = fold_feature_contract_state([_submitted(), rej, _produced()])
    assert st2.status is FeatureContractStatus.OUT_OF_SCOPE


def test_llm_call_refs_accrete_even_after_confirmation():
    conf = _Evt(events.CONTRACT_CONFIRMED, "evt_conf", {"confirmed_doc_id": "doc_conf1"})
    llm = _Evt(events.LLM_CALL_RECORDED, "evt_llm", {"llm_call_ref": "llmc_9"})
    st = fold_feature_contract_state([_submitted(), conf, llm])
    assert st.llm_call_refs == ("llmc_9",)
    assert st.status is FeatureContractStatus.CONFIRMED
