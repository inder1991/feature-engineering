from types import SimpleNamespace

import featuregen.intake.events as ev
from featuregen.contracts import IdentityEnvelope
from featuregen.intake.commands import (
    guard_advance,
    open_fields_empty,
)
from featuregen.intake.state import (
    FeatureContractStatus,
    actor_is_request_owner,
    confirmer_is_requester_human,
    fold_feature_contract_state,
)


def _evt(type_, **payload):
    return SimpleNamespace(type=type_, payload=payload, event_id="evt_x", stream_version=1)


_OWNER = IdentityEnvelope(subject="user:raj", actor_kind="human", authenticated=True,
                          auth_method="sso", role_claims=("data_scientist",))
_OTHER = IdentityEnvelope(subject="user:mallory", actor_kind="human", authenticated=True,
                          auth_method="sso", role_claims=("data_scientist",))
_SERVICE = IdentityEnvelope(subject="service:intake-agent", actor_kind="service", authenticated=True,
                            auth_method="mtls", role_claims=("intake-agent",))


def _draft(open_fields):
    return fold_feature_contract_state([
        _evt(ev.INTENT_SUBMITTED, run_id="run_1", requester="user:raj", intake_mode="definition"),
        _evt(ev.DRAFT_CONTRACT_PRODUCED, run_id="run_1", draft_doc_id="doc_draft1",
             open_fields=list(open_fields)),
    ])


def test_is_terminal_and_open_fields_empty():
    confirmed = fold_feature_contract_state([
        _evt(ev.INTENT_SUBMITTED, run_id="run_1", requester="user:raj", intake_mode="definition"),
        _evt(ev.DRAFT_CONTRACT_PRODUCED, run_id="run_1", open_fields=[]),
        _evt(ev.CONTRACT_CONFIRMED, run_id="run_1", confirmed_doc_id="doc_conf1"),
    ])
    assert confirmed.is_terminal is True                 # the P2 FeatureContractState property
    assert open_fields_empty(_draft([])) is True
    assert open_fields_empty(_draft(["filters.declined_status_encoding"])) is False


def test_owner_guards():
    st = _draft([])
    assert actor_is_request_owner(st, _OWNER) is True    # imported from P2 state.py (R4)
    assert actor_is_request_owner(st, _OTHER) is False
    # confirmer_is_requester_human = owner AND actor_kind == human (a service can never confirm).
    assert confirmer_is_requester_human(st, _OWNER) is True
    assert confirmer_is_requester_human(st, _SERVICE) is False


def test_confirmer_is_requester_human_non_owner_human_denied():
    # The deny path that distinguishes confirmer_is_requester_human from a plain actor_kind=="human"
    # check: a HUMAN who is NOT the request owner (a different data scientist) is still denied.
    st = _draft([])
    assert _OTHER.actor_kind == "human"
    assert actor_is_request_owner(st, _OTHER) is False
    assert confirmer_is_requester_human(st, _OTHER) is False


def test_guard_advance_none_terminal_and_illegal():
    empty = fold_feature_contract_state([])
    assert guard_advance(empty, (FeatureContractStatus.NEEDS_CLARIFICATION,)) is not None  # no contract

    draft = _draft([])
    assert guard_advance(draft, (FeatureContractStatus.NEEDS_CLARIFICATION,)) is None       # OK

    # illegal advance from the wrong status
    assert guard_advance(draft, (FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED,)) is not None

    rejected = fold_feature_contract_state([
        _evt(ev.INTENT_SUBMITTED, run_id="run_1", requester="user:raj", intake_mode="definition"),
        _evt(ev.INTENT_REJECTED, run_id="run_1", classification="OUT_OF_SCOPE", catalog_version="v1"),
    ])
    # no-regression: a terminal fold refuses any advance
    assert guard_advance(rejected, (FeatureContractStatus.NEEDS_CLARIFICATION,
                                    FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED)) is not None
