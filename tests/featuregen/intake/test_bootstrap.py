from psycopg.rows import dict_row
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity

from featuregen.aggregates._append import append
from featuregen.aggregates.run_lifecycle import run_is_terminal
from featuregen.authz.authorizer import PolicyAuthorizer
from featuregen.authz.policy import authorize_command, seed_authz_policy
from featuregen.commands.api import execute_command
from featuregen.commands.authz_seam import register_command_authorizer
from featuregen.commands.registry import get_command
from featuregen.contracts import Command
from featuregen.events.registry import event_registry
from featuregen.intake.bootstrap import register_sp2, seed_sp2_authz
from featuregen.intake.store import append_feature_contract_event

_SP2_EVENT_TYPES = {
    "INTENT_SUBMITTED",
    "DRAFT_CONTRACT_PRODUCED",
    "CONTRACT_CRITIQUED",
    "FIELD_AUTO_RESOLVED",
    "CLARIFICATION_REQUESTED",
    "CLARIFICATION_ANSWERED",
    "CONTRACT_REFINED",
    "MINIMUM_CONTRACT_VALIDATED",
    "CONTRACT_CONFIRMED",
    "USE_CASE_ONBOARDING_REQUESTED",
    "INTENT_REJECTED",
    "LLM_CALL_RECORDED",
}
_SP2_ACTIONS = {
    "submit_intent",
    "answer_clarification",
    "select_candidate_doc",
    "open_gate1_task",
    "confirm_contract",
    "request_edit",
    "reject_intent",
}


class _Registry:
    """Stand-in HandlerRegistry; SP-2 registers no runtime handlers."""

    def __init__(self):
        self.handlers = {}

    def register(self, handler):
        self.handlers[handler.name] = handler


def test_register_sp2_registers_fc_event_schemas_and_command_catalog():
    register_sp2(_Registry())
    registered = {t for (t, _v, _s, _o, _st) in event_registry().all_schemas()}
    assert _SP2_EVENT_TYPES <= registered
    for action in _SP2_ACTIONS:
        assert callable(get_command(action))
    # idempotent: a second call raises nothing (register_sp2_commands skips already-registered)
    register_sp2(_Registry())


def test_seed_sp2_authz_seeds_the_eight_additive_rows(db):
    seed_sp2_authz(db)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT action, permitted_role, actor_kind FROM authz_policy "
            "WHERE action = ANY(%s) ORDER BY action, permitted_role",
            (sorted(_SP2_ACTIONS),),
        )
        rows = cur.fetchall()
    got = {(r["action"], r["permitted_role"], r["actor_kind"]) for r in rows}
    assert ("submit_intent", "data_scientist", "human") in got
    assert ("submit_intent", "intake-agent", "service") in got
    assert ("answer_clarification", "data_scientist", "human") in got
    assert ("select_candidate_doc", "data_scientist", "human") in got
    assert ("open_gate1_task", "intake-agent", "service") in got
    assert ("confirm_contract", "data_scientist", "human") in got
    assert ("request_edit", "data_scientist", "human") in got
    # the ADDITIVE rejection authority: reject_intent is service-issued, NOT SP-0's validator `reject`
    assert ("reject_intent", "intake-agent", "service") in got
    assert len(got) == 8
    # idempotent
    seed_sp2_authz(db)
    n = db.execute(
        "SELECT count(*) FROM authz_policy WHERE action = ANY(%s)", (sorted(_SP2_ACTIONS),)
    ).fetchone()[0]
    assert n == 8


def test_seed_sp2_authz_registers_contract_schemas_primary_selected_and_checkpoints(db):
    register_sp2(_Registry())  # FC event schemas + command catalog in-memory (PRIMARY_SELECTED + contract schemas are seeded by seed_sp2_authz below)
    seed_sp2_authz(db)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type_name FROM document_type_registry "
            "WHERE type_name IN ('DRAFT_CONTRACT','ASSUMPTION_LEDGER','CONFIRMED_CONTRACT') "
            "AND schema_version=1"
        )
        docs = {r["type_name"] for r in cur.fetchall()}
        assert docs == {"DRAFT_CONTRACT", "ASSUMPTION_LEDGER", "CONFIRMED_CONTRACT"}
        cur.execute(
            "SELECT 1 FROM event_type_registry WHERE type_name='PRIMARY_SELECTED'"
        )
        assert cur.fetchone() is not None
        cur.execute(
            "SELECT projection_name FROM projection_checkpoints "
            "WHERE projection_name IN ('stage_primary','feature_contract')"
        )
        checkpoints = {r["projection_name"] for r in cur.fetchall()}
        assert {"stage_primary", "feature_contract"} <= checkpoints


def test_seeded_rows_admit_the_owner_and_the_service_at_the_authz_layer(db):
    seed_authz_policy(db)  # SP-0 base rows
    seed_sp2_authz(db)
    raj = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))
    svc = mint_test_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="deploy-sig"
    )
    submit = Command("submit_intent", "feature_contract", None, {}, raj, "ik-a")
    reject = Command("reject_intent", "feature_contract", "run_x", {}, svc, "ik-b")
    assert authorize_command(db, submit).allowed is True
    assert authorize_command(db, reject).allowed is True
    # a role that is NOT data_scientist is refused at the authz layer
    analyst = mint_test_identity(subject="user:mallory", role_claims=("analyst",))
    assert authorize_command(db, Command("submit_intent", "feature_contract", None, {}, analyst, "ik-c")).allowed is False


def test_unauthorized_submit_intent_is_denied_and_audited(db):
    register_sp2(_Registry())
    seed_authz_policy(db)
    seed_sp2_authz(db)
    register_command_authorizer(PolicyAuthorizer())
    analyst = mint_test_identity(subject="user:mallory", role_claims=("analyst",))
    res = execute_command(
        db,
        Command(
            "submit_intent",
            "feature_contract",
            None,
            {"request_id": "r1", "intent_text": "x", "intake_mode": "definition"},
            analyst,
            "ik-deny",
        ),
    )
    assert res.accepted is False
    assert res.denied_reason == "no matching authz policy"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE event_type='COMMAND_DENIED' AND attempted_action='submit_intent'"
        )
        assert cur.fetchone()["n"] == 1


# ── withdraw_intent wiring (Task-8.7 review: the requester's abandonment was ORPHANED) ────────────
# register_sp2 + seed_sp2_authz MUST make `withdraw_intent` dispatchable via execute_command by the
# request owner (data_scientist human) and deny a non-owner/service at the authz layer.


def test_register_sp2_wires_withdraw_intent_into_the_command_catalog():
    register_sp2(_Registry())
    assert callable(get_command("withdraw_intent"))


def test_withdraw_intent_row_admits_the_requester_and_denies_the_service(db):
    seed_authz_policy(db)
    seed_sp2_authz(db)
    raj = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))
    svc = mint_test_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="deploy-sig"
    )
    ok = Command("withdraw_intent", "run", "run_w", {"run_id": "run_w"}, raj, "wk-a")
    assert authorize_command(db, ok).allowed is True
    # withdrawal is the requester's own — a service principal is refused at the authz layer
    bad = Command("withdraw_intent", "run", "run_w", {"run_id": "run_w"}, svc, "wk-b")
    assert authorize_command(db, bad).allowed is False


def test_owner_dispatches_withdraw_intent_via_execute_command(db):
    register_sp2(_Registry())
    seed_authz_policy(db)
    seed_sp2_authz(db)
    register_command_authorizer(PolicyAuthorizer())
    raj = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))
    # open an owned run + feature_contract stream (INTENT_SUBMITTED.requester == the owner)
    append(
        db, aggregate="run", aggregate_id="run_w", type="RUN_CREATED",
        payload={"run_id": "run_w", "request_id": "req_w"}, actor=raj,
        run_id="run_w", request_id="req_w", expected_version=0,
    )
    append_feature_contract_event(
        db, run_id="run_w", type="INTENT_SUBMITTED",
        payload={
            "requester": "user:raj", "intake_mode": "definition",
            "raw_input_ref": "blob_w", "raw_input_classification": "clean",
        },
        actor=raj, expected_version=0,
    )
    res = execute_command(
        db,
        Command(
            "withdraw_intent", "run", "run_w",
            {"run_id": "run_w", "reason": "changed my mind"}, raj, "wk-owner",
        ),
    )
    assert res.accepted is True
    assert run_is_terminal(db, "run_w") is True
