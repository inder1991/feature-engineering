from dataclasses import dataclass

from tests.featuregen._helpers import mint_test_service_identity

from featuregen.intake import events
from featuregen.intake.state import (
    FeatureContractState,
    FeatureContractStatus,
    actor_is_request_owner,
    fold_feature_contract_state,
)
from featuregen.intake.store import append_feature_contract_event, load_feature_contract


@dataclass
class _Actor:
    subject: str


@dataclass
class _Evt:
    type: str
    event_id: str
    payload: dict
    actor: _Actor | None = None  # SP-0 EventEnvelope.actor (IdentityEnvelope with .subject)
    # R2/N4 — the typed EventEnvelope id columns production emitters populate (ids ride here, NOT the
    # payload). Default None so the existing payload-carrying fixtures still exercise the fallback path.
    request_id: str | None = None
    run_id: str | None = None
    aggregate_id: str | None = None


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


# ── N4: the fold reads request_id / run_id from the event ENVELOPE, not the payload (R2) ──────────────
def test_fold_reads_request_id_and_run_id_from_the_envelope_not_the_payload(conn):
    # The ROOT-cause regression (N4): production emitters (append_feature_contract_event / submit_intent)
    # keep id fields OFF the payload — they ride the typed EventEnvelope columns (request_id / run_id /
    # aggregate_id, R2). Build a real feature_contract stream exactly the way production does (ids on the
    # seam kwargs, a payload that carries ONLY the semantic fields) and prove the fold surfaces the REAL
    # ids, NOT None. This assertion FAILS before the fix (the fold read p.get("request_id") → None).
    actor = mint_test_service_identity(
        subject="service:intake-agent", role_claims=["intake-agent"], attestation="sig"
    )
    append_feature_contract_event(
        conn,
        run_id="run_env01",
        request_id="req_env01",
        type=events.INTENT_SUBMITTED,
        payload={  # R2 — NO request_id / run_id keys; only the semantic fields
            "intake_mode": "definition",
            "raw_input_ref": "blob_env01",
            "raw_input_classification": "clean",
        },
        actor=actor,
        expected_version=0,
    )
    st = fold_feature_contract_state(load_feature_contract(conn, "run_env01"))
    assert st.request_id == "req_env01"  # from the envelope, NOT None
    assert st.run_id == "run_env01"  # envelope run_id (== aggregate_id, X3), NOT None
    assert st.request_id is not None and st.run_id is not None
    assert st.intake_mode == "definition"
    assert st.requester == "service:intake-agent"  # R4 — still the event actor.subject


def test_fold_prefers_envelope_ids_over_payload_and_falls_back_to_payload():
    # Precedence contract: the ENVELOPE typed columns win; a stray payload id is only a defensive
    # fallback. An INTENT_SUBMITTED carrying its ids ONLY on the envelope (as production does — no ids in
    # the payload) still folds to non-None ids; a legacy event with ids ONLY in the payload still resolves.
    envelope_only = _Evt(
        events.INTENT_SUBMITTED,
        "evt_env",
        {"intake_mode": "definition"},  # NO request_id / run_id in the payload
        actor=_Actor("user:raj"),
        request_id="req_env",
        run_id="run_env",
        aggregate_id="run_env",
    )
    st = fold_feature_contract_state([envelope_only])
    assert st.request_id == "req_env"
    assert st.run_id == "run_env"

    # envelope columns take precedence over any (legacy) payload ids
    both = _Evt(
        events.INTENT_SUBMITTED,
        "evt_both",
        {"request_id": "req_payload", "run_id": "run_payload", "intake_mode": "definition"},
        actor=_Actor("user:raj"),
        request_id="req_env",
        run_id="run_env",
    )
    st_both = fold_feature_contract_state([both])
    assert st_both.request_id == "req_env"
    assert st_both.run_id == "run_env"

    # legacy synthetic event with ids ONLY in the payload → payload fallback still resolves them
    legacy = _Evt(
        events.INTENT_SUBMITTED,
        "evt_legacy",
        {"request_id": "req_payload", "run_id": "run_payload", "intake_mode": "definition"},
        actor=_Actor("user:raj"),
    )
    st_legacy = fold_feature_contract_state([legacy])
    assert st_legacy.request_id == "req_payload"
    assert st_legacy.run_id == "run_payload"
