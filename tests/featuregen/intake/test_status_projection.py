import pytest

import featuregen.intake.events as ev
from featuregen.aggregates._append import append
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.contracts import IdentityEnvelope
from featuregen.events.registry import event_registry
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.read_model import read_contract_status
from featuregen.intake.state import FeatureContractStatus
from featuregen.intake.store import append_feature_contract_event


@pytest.fixture(autouse=True)
def _register(_reset_registry):
    register_phase06_event_schemas()
    register_sp2_event_types(event_registry())


_SERVICE = IdentityEnvelope(subject="service:intake-agent", actor_kind="service", authenticated=True,
                            auth_method="mtls", role_claims=("intake-agent",))


def test_read_contract_status(db):
    assert read_contract_status(db, "run_absent") is None
    append(db, aggregate="run", aggregate_id="run_1", type="RUN_CREATED",
           payload={"run_id": "run_1", "request_id": "req_1"}, actor=_SERVICE,
           run_id="run_1", request_id="req_1", expected_version=0)
    append_feature_contract_event(db, run_id="run_1", type=ev.INTENT_SUBMITTED,
                    payload={"run_id": "run_1", "requester": "user:raj", "intake_mode": "definition",
                             "raw_input_ref": "blob_raw1", "raw_input_classification": "clean"},
                    actor=_SERVICE, expected_version=0)
    assert read_contract_status(db, "run_1") is FeatureContractStatus.NEEDS_CLARIFICATION
    append_feature_contract_event(db, run_id="run_1", type=ev.USE_CASE_ONBOARDING_REQUESTED,
                    payload={"run_id": "run_1", "catalog_version": "bdc-2026.06"}, actor=_SERVICE)
    assert read_contract_status(db, "run_1") is FeatureContractStatus.NEEDS_USE_CASE_ONBOARDING
