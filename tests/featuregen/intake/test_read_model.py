import pytest

import featuregen.intake.events as ev
from featuregen.aggregates._append import append, provenance_for
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.contracts import IdentityEnvelope
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.store import append_document, compute_content_hash
from featuregen.events.registry import event_registry
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.read_model import ContractView, get_contract
from featuregen.intake.store import append_feature_contract_event


@pytest.fixture(autouse=True)
def _register(_reset_registry):
    register_phase06_event_schemas()
    register_sp2_event_types(event_registry())


_SERVICE = IdentityEnvelope(subject="service:intake-agent", actor_kind="service", authenticated=True,
                            auth_method="mtls", role_claims=("intake-agent",))
_REQUESTER = IdentityEnvelope(subject="user:raj", actor_kind="human", authenticated=True,
                              auth_method="sso", role_claims=("data_scientist",))


def _open_run(db, run_id="run_1", request_id="req_1"):
    append(db, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
           payload={"run_id": run_id, "request_id": request_id}, actor=_SERVICE,
           run_id=run_id, request_id=request_id, expected_version=0)


def test_get_contract_none_when_no_contract(db):
    assert get_contract(db, "run_absent") is None


def test_get_contract_draft_is_not_servable(db):
    _open_run(db)
    append_feature_contract_event(db, run_id="run_1", type=ev.INTENT_SUBMITTED,
                    payload={"run_id": "run_1", "request_id": "req_1", "requester": "user:raj",
                             "intake_mode": "definition", "catalog_version": "bdc-2026.06",
                             "raw_input_ref": "blob_raw1", "raw_input_classification": "clean"},
                    actor=_SERVICE, expected_version=0)
    append_feature_contract_event(db, run_id="run_1", type=ev.DRAFT_CONTRACT_PRODUCED,
                    payload={"run_id": "run_1", "draft_doc_id": "doc_draft1",
                             "assumption_ledger_ref": "doc_ledger1",
                             "proposed_feature_name": "declined_card_auth_count_90d",
                             "open_fields": ["filters.declined_status_encoding"],
                             "field_scores": {"filters": {"ambiguity": 0.8, "confidence": 0.4,
                                                          "source": "llm"}},
                             "open_questions": [{"field": "filters.declined_status_encoding",
                                                 "routed_to": "human"}]},
                    actor=_SERVICE)

    view = get_contract(db, "run_1")
    assert isinstance(view, ContractView)
    assert view.stage == "DRAFT_CONTRACT"
    assert view.status == "NEEDS_CLARIFICATION"
    assert view.intake_mode == "definition"
    assert view.draft_doc_id == "doc_draft1"
    assert view.assumption_ledger_ref == "doc_ledger1"
    assert view.feature_name == "declined_card_auth_count_90d"  # from proposed_feature_name
    assert view.open_fields == ("filters.declined_status_encoding",)
    assert view.field_scores["filters"]["ambiguity"] == 0.8
    assert view.open_questions[0]["routed_to"] == "human"
    assert view.terminal_outcome is None
    assert view.reason_if_unavailable is not None  # fail-closed: a Draft is never servable to SP-3


def _emit_confirmed_doc(db, run_id="run_1", doc_id="doc_conf1"):
    body = b'{"feature_name":"declined_card_auth_count_90d","status":"CONFIRMED"}'
    new_doc = NewDocument(
        doc_id=doc_id, stage=Stage.CONFIRMED_CONTRACT.value, schema_version=1, branch_role="primary",
        content_hash=compute_content_hash(body), body_classification="governance-retained",
        provenance=provenance_for(Stage.CONFIRMED_CONTRACT.value), body_ref="blob_conf1",
    )
    append_document(db, new_doc, run_id=run_id, actor=_SERVICE)
    return doc_id


def _seed_confirmed(db, run_id="run_1"):
    _open_run(db, run_id)
    append_feature_contract_event(db, run_id=run_id, type=ev.INTENT_SUBMITTED,
                    payload={"run_id": run_id, "request_id": "req_1", "requester": "user:raj",
                             "intake_mode": "definition", "catalog_version": "bdc-2026.06",
                             "raw_input_ref": "blob_raw1", "raw_input_classification": "clean"},
                    actor=_SERVICE, expected_version=0)
    append_feature_contract_event(db, run_id=run_id, type=ev.DRAFT_CONTRACT_PRODUCED,
                    payload={"run_id": run_id, "draft_doc_id": "doc_draft1", "open_fields": []},
                    actor=_SERVICE)
    append_feature_contract_event(db, run_id=run_id, type=ev.MINIMUM_CONTRACT_VALIDATED,
                    payload={"run_id": run_id}, actor=_SERVICE)
    doc_id = _emit_confirmed_doc(db, run_id)
    append_feature_contract_event(db, run_id=run_id, type=ev.CONTRACT_CONFIRMED,
                    payload={"run_id": run_id, "confirmed_doc_id": doc_id,
                             "feature_name": "declined_card_auth_count_90d",
                             "requires_independent_validation": False, "selected_candidate": None},
                    actor=_REQUESTER)


def test_get_contract_confirmed_is_servable(db):
    _seed_confirmed(db)
    view = get_contract(db, "run_1")
    assert view.stage == "CONFIRMED_CONTRACT"
    assert view.status == "CONFIRMED"
    assert view.confirmed_doc_id == "doc_conf1"
    assert view.requires_independent_validation is False
    assert view.body_ref == "blob_conf1"
    assert view.content_hash is not None
    assert view.terminal_outcome is None
    assert view.reason_if_unavailable is None  # the ONLY servable case
    # R13 dual access: subscript body access alongside attribute access. The frozen body resolves in the
    # P9 E2E (object store bound); here it is fail-soft to None. A non-body key still raises KeyError.
    assert view.run_id == "run_1"                                  # attribute access
    assert view["confirmed"] is None or view["confirmed"]["feature_name"] == "declined_card_auth_count_90d"
    with pytest.raises(KeyError):
        view["nonsense"]


def test_get_contract_withdrawn_run_is_blocked(db):
    _open_run(db, "run_2")
    append_feature_contract_event(db, run_id="run_2", type=ev.INTENT_SUBMITTED,
                    payload={"run_id": "run_2", "request_id": "req_2", "requester": "user:raj",
                             "intake_mode": "definition", "raw_input_ref": "blob_raw2", "raw_input_classification": "clean"},
                    actor=_SERVICE, expected_version=0)
    append(db, aggregate="run", aggregate_id="run_2", type="RUN_WITHDRAWN",
           payload={"run_id": "run_2", "reason": "requester withdrew intent"}, actor=_REQUESTER,
           run_id="run_2")
    view = get_contract(db, "run_2")
    assert view.terminal_outcome == "RUN_WITHDRAWN"
    assert view.reason_if_unavailable == "run terminal: RUN_WITHDRAWN"
