import pytest

import featuregen.intake.events as ev
from featuregen.aggregates._append import append
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.contracts import Command, IdentityEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import load_stream
from featuregen.intake.commands import withdraw_intent
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.store import append_feature_contract_event, load_feature_contract


@pytest.fixture(autouse=True)
def _register(_reset_registry):
    register_phase06_event_schemas()
    register_sp2_event_types(event_registry())


_SERVICE = IdentityEnvelope(subject="service:intake-agent", actor_kind="service", authenticated=True,
                            auth_method="mtls", role_claims=("intake-agent",))
_REQUESTER = IdentityEnvelope(subject="user:raj", actor_kind="human", authenticated=True,
                              auth_method="sso", role_claims=("data_scientist",))
_OTHER = IdentityEnvelope(subject="user:mallory", actor_kind="human", authenticated=True,
                          auth_method="sso", role_claims=("data_scientist",))


def _open(db, run_id="run_1", request_id="req_1"):
    append(db, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
           payload={"run_id": run_id, "request_id": request_id}, actor=_SERVICE,
           run_id=run_id, request_id=request_id, expected_version=0)
    append_feature_contract_event(db, run_id=run_id, type=ev.INTENT_SUBMITTED,
                    payload={"run_id": run_id, "request_id": request_id, "requester": "user:raj",
                             "intake_mode": "definition", "raw_input_ref": "blob_raw1",
                             "raw_input_classification": "clean"}, actor=_REQUESTER, expected_version=0)


def _cmd(actor, run_id="run_1", **args):
    return Command(action="withdraw_intent", aggregate="run", aggregate_id=run_id,
                   args={"run_id": run_id, **args}, actor=actor, idempotency_key=f"wk_{run_id}")


def test_owner_withdraws_run(db):
    _open(db)
    res = withdraw_intent(db, _cmd(_REQUESTER, reason="changed my mind"))
    assert res.accepted is True
    assert run_is_terminal(db, "run_1") is True
    assert "RUN_WITHDRAWN" in [e.type for e in load_stream(db, "run", "run_1")]
    # withdrawal is run-level: NO feature_contract event is emitted
    assert ev.INTENT_REJECTED not in [e.type for e in load_feature_contract(db, "run_1")]


def test_non_owner_withdraw_denied_and_audited(db):
    _open(db)
    before = db.execute("SELECT count(*) FROM security_audit").fetchone()[0]
    res = withdraw_intent(db, _cmd(_OTHER))
    assert res.accepted is False
    assert "not the request owner" in res.denied_reason
    assert run_is_terminal(db, "run_1") is False
    after = db.execute("SELECT count(*) FROM security_audit").fetchone()[0]
    assert after == before + 1  # denial routed to the security-audit stream


def test_service_cannot_withdraw(db):
    _open(db)
    res = withdraw_intent(db, _cmd(_SERVICE))
    assert res.accepted is False
    assert "human" in res.denied_reason


def test_withdraw_denies_stale_on_concurrent_advance(db, monkeypatch):
    # X4 (SP-1 capstone C2): a concurrent feature_contract transition lands AFTER the owner/terminal
    # gate but BEFORE the delegated run withdraw — the refold-before-append sees an advanced head and
    # denies `stale`; RUN_WITHDRAWN is NEVER driven (the run stays live).
    import featuregen.intake.commands as cmds

    _open(db)
    real_load = cmds.load_feature_contract
    calls = {"n": 0}

    def _racing_load(conn, run_id):
        calls["n"] += 1
        if calls["n"] == 2:  # a concurrent transition slips in between the gate and the refold check
            append_feature_contract_event(conn, run_id=run_id, type=ev.DRAFT_CONTRACT_PRODUCED,
                            payload={"run_id": run_id, "draft_doc_id": "doc_draft1", "open_fields": []},
                            actor=_SERVICE)
        return real_load(conn, run_id)

    monkeypatch.setattr(cmds, "load_feature_contract", _racing_load)
    res = withdraw_intent(db, _cmd(_REQUESTER))
    assert res.accepted is False
    assert "stale" in res.denied_reason
    assert run_is_terminal(db, "run_1") is False
