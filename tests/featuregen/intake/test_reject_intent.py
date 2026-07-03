import pytest

import featuregen.intake.events as ev
from featuregen.aggregates._append import append
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.contracts import Command, IdentityEnvelope
from featuregen.events.registry import event_registry
from featuregen.intake.commands import reject_intent
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.state import FeatureContractStatus, fold_feature_contract_state
from featuregen.intake.store import append_feature_contract_event, load_feature_contract


@pytest.fixture(autouse=True)
def _register(_reset_registry):
    # RUN_CREATED/RUN_REJECTED (phase-06) + the twelve SP-2 feature_contract schemas, into the
    # per-test event-registry singleton (root `_reset_registry` swaps a fresh empty one in first).
    register_phase06_event_schemas()
    register_sp2_event_types(event_registry())


_SERVICE = IdentityEnvelope(subject="service:intake-agent", actor_kind="service", authenticated=True,
                            auth_method="mtls", role_claims=("intake-agent",))
_REQUESTER = IdentityEnvelope(subject="user:raj", actor_kind="human", authenticated=True,
                              auth_method="sso", role_claims=("data_scientist",))


def _open_run_and_contract(db, run_id="run_1", request_id="req_1"):
    append(db, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
           payload={"run_id": run_id, "request_id": request_id}, actor=_SERVICE,
           run_id=run_id, request_id=request_id, expected_version=0)
    append_feature_contract_event(db, run_id=run_id, type=ev.INTENT_SUBMITTED,
                    payload={"run_id": run_id, "request_id": request_id, "requester": "user:raj",
                             "intake_mode": "definition", "raw_input_ref": "blob_raw1",
                             "raw_input_classification": "clean", "catalog_version": "bdc-2026.06"},
                    actor=_SERVICE, expected_version=0)


def _cmd(run_id, **args):
    return Command(action="reject_intent", aggregate="feature_contract", aggregate_id=run_id,
                   args={"run_id": run_id, **args}, actor=_SERVICE, idempotency_key=f"rk_{run_id}")


def test_reject_intent_out_of_scope_terminates_contract_and_run(db):
    _open_run_and_contract(db)
    res = reject_intent(db, _cmd("run_1", classification="OUT_OF_SCOPE",
                                 reason="not a banking feature", catalog_version="bdc-2026.06"))
    assert res.accepted is True

    st = fold_feature_contract_state(load_feature_contract(db, "run_1"))
    assert st.status is FeatureContractStatus.OUT_OF_SCOPE
    assert st.rejection_reason == "not a banking feature"
    assert st.catalog_version == "bdc-2026.06"

    fc_types = [e.type for e in load_feature_contract(db, "run_1")]
    assert ev.INTENT_REJECTED in fc_types
    assert run_is_terminal(db, "run_1") is True
    run_types = [e.type for e in __import__("featuregen.events.store", fromlist=["load_stream"])
                 .load_stream(db, "run", "run_1")]
    assert "RUN_REJECTED" in run_types


def test_reject_intent_rejects_bad_classification(db):
    _open_run_and_contract(db)
    res = reject_intent(db, _cmd("run_1", classification="AMBIGUOUS_CLARIFY", catalog_version="v1"))
    assert res.accepted is False
    assert "classification must be one of" in res.denied_reason
    # nothing appended, run untouched
    assert ev.INTENT_REJECTED not in [e.type for e in load_feature_contract(db, "run_1")]
    assert run_is_terminal(db, "run_1") is False


def test_reject_intent_no_regression_after_confirmed(db):
    _open_run_and_contract(db)
    append_feature_contract_event(db, run_id="run_1", type=ev.MINIMUM_CONTRACT_VALIDATED,
                    payload={"run_id": "run_1"}, actor=_SERVICE)
    append_feature_contract_event(db, run_id="run_1", type=ev.CONTRACT_CONFIRMED,
                    payload={"run_id": "run_1", "confirmed_doc_id": "doc_conf1"}, actor=_REQUESTER)
    res = reject_intent(db, _cmd("run_1", classification="OUT_OF_SCOPE", catalog_version="v1"))
    assert res.accepted is False
    assert "already terminal" in res.denied_reason
    # the CONFIRMED fold is intact; run never rejected
    st = fold_feature_contract_state(load_feature_contract(db, "run_1"))
    assert st.status is FeatureContractStatus.CONFIRMED
    assert run_is_terminal(db, "run_1") is False


def test_reject_intent_prohibited_data_class_records_matched_class(db):
    _open_run_and_contract(db)
    res = reject_intent(db, _cmd("run_1", classification="PROHIBITED_DATA_CLASS",
                                 matched_class="protected_attribute", catalog_version="bdc-2026.06"))
    assert res.accepted is True
    st = fold_feature_contract_state(load_feature_contract(db, "run_1"))
    assert st.status is FeatureContractStatus.PROHIBITED_DATA_CLASS
    assert st.matched_class == "protected_attribute"


def test_reject_intent_denies_stale_on_concurrent_advance(db, monkeypatch):
    # X4 (SP-1 capstone C2): a concurrent feature_contract transition lands between the fold and the
    # CAS append → append_feature_contract_event raises ConcurrencyError → deny `stale`, and SP-0's
    # RUN_REJECTED is NEVER driven (the run stays live).
    import featuregen.intake.commands as cmds
    from featuregen.contracts import ConcurrencyError

    _open_run_and_contract(db)

    def _stale_append(*a, **k):
        raise ConcurrencyError("stream advanced")

    monkeypatch.setattr(cmds, "append_feature_contract_event", _stale_append)
    res = reject_intent(db, _cmd("run_1", classification="OUT_OF_SCOPE", catalog_version="v1"))
    assert res.accepted is False
    assert "stale" in res.denied_reason
    assert run_is_terminal(db, "run_1") is False


def test_reject_intent_denies_before_fc_append_when_run_terminal_out_of_band(db):
    # Deny-before-append atomicity: the run is made terminal OUT-OF-BAND (RUN_WITHDRAWN writes NO fc
    # event) while the FC is still non-terminal. reject_intent must deny "run already terminal" BEFORE
    # appending INTENT_REJECTED — otherwise the fc terminal is orphaned on a differently-terminal run
    # (reject_command would deny, and the paired RUN_REJECTED would never fire).
    _open_run_and_contract(db)
    append(db, aggregate="run", aggregate_id="run_1", type="RUN_WITHDRAWN",
           payload={"run_id": "run_1", "reason": "requester withdrew"}, actor=_SERVICE, run_id="run_1")

    res = reject_intent(db, _cmd("run_1", classification="OUT_OF_SCOPE", catalog_version="v1"))
    assert res.accepted is False
    assert res.denied_reason == "run already terminal"
    # NO orphaned fc terminal: INTENT_REJECTED was never appended, the FC fold is untouched.
    assert ev.INTENT_REJECTED not in [e.type for e in load_feature_contract(db, "run_1")]
    st = fold_feature_contract_state(load_feature_contract(db, "run_1"))
    assert st.status is FeatureContractStatus.NEEDS_CLARIFICATION
