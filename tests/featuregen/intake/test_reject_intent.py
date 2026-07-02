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
