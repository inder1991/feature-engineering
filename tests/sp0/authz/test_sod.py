from psycopg.types.json import Json

from sp0.authz.policy import authorize_command, seed_authz_policy
from sp0.authz.sod import (
    gather_gate_responders,
    resolve_run_author,
    three_party_disjoint,
    two_party_ok,
)
from sp0.contracts.commands import Command
from sp0.identity.build import build_human_identity


def _seed_run(db, run_id, author_subject):
    db.execute(
        """
        INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                            type, schema_version, table_version, actor, payload, provenance,
                            occurred_at)
        VALUES (%s,'run',%s,1,%s,'RUN_CREATED',1,1,%s,'{}'::jsonb,'{}'::jsonb, now())
        """,
        ("evt_" + run_id, run_id, run_id, Json({"subject": author_subject})),
    )


def _open_and_answer_iv(db, run_id, validator_subject):
    db.execute(
        """
        INSERT INTO human_tasks (task_id, run_id, gate, eligible_assignees, allowed_responses)
        VALUES ('task_iv', %s, 'INDEPENDENT_VALIDATION', '{"role":"validator"}'::jsonb,
                '{validate}')
        """,
        (run_id,),
    )
    db.execute(
        "INSERT INTO human_task_responses (task_id, subject, response, answered_seq) "
        "VALUES ('task_iv', %s, 'validate', 1)",
        (validator_subject,),
    )


def test_pure_helpers():
    assert two_party_ok("user:a", "user:b") is True
    assert two_party_ok("user:a", "user:a") is False
    assert three_party_disjoint("user:a", {"user:b"}, "user:c") is True
    assert three_party_disjoint("user:a", {"user:b"}, "user:b") is False
    assert three_party_disjoint("user:a", {"user:a"}, "user:c") is False


def test_resolvers(db):
    _seed_run(db, "run_1", "user:author")
    _open_and_answer_iv(db, "run_1", "user:val")
    assert resolve_run_author(db, "run_1") == "user:author"
    assert gather_gate_responders(db, "INDEPENDENT_VALIDATION", run_id="run_1") == {"user:val"}


def test_delegated_validator_is_counted_in_three_party(db):
    """A validator who answers INDEPENDENT_VALIDATION via a delegate (on_behalf_of) must be
    counted under the EFFECTIVE authority (coalesce(on_behalf_of, subject)), so the same person
    cannot then grant FINAL_APPROVAL. Before the fix, gather_gate_responders keyed on r.subject
    (the delegate) and missed the validator authority entirely."""
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    db.execute(
        """
        INSERT INTO human_tasks (task_id, run_id, gate, eligible_assignees, allowed_responses)
        VALUES ('task_iv', %s, 'INDEPENDENT_VALIDATION', '{"role":"validator"}'::jsonb,
                '{validate}')
        """,
        ("run_1",),
    )
    db.execute(
        "INSERT INTO human_task_responses (task_id, subject, on_behalf_of, response, answered_seq)"
        " VALUES ('task_iv', %s, %s, 'validate', 1)",
        ("user:delegate", "user:val"),
    )
    # The effective validator (the authority), not the delegate, is the counted responder.
    assert gather_gate_responders(db, "INDEPENDENT_VALIDATION", run_id="run_1") == {"user:val"}

    val_as_approver = build_human_identity(subject="user:val", role_claims=["approver"])
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "FINAL_APPROVAL", "task_id": "task_fa"},
        actor=val_as_approver, idempotency_key="i_deleg",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "validator" in decision.reason


def test_final_approval_blocks_requester_self_approval(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    author_as_approver = build_human_identity(
        subject="user:author", role_claims=["approver"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "FINAL_APPROVAL", "task_id": "task_fa"},
        actor=author_as_approver, idempotency_key="i1",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "four-eyes" in decision.reason


def test_three_party_blocks_validator_as_approver(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    _open_and_answer_iv(db, "run_1", "user:val")
    validator_as_approver = build_human_identity(
        subject="user:val", role_claims=["approver"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "FINAL_APPROVAL", "task_id": "task_fa"},
        actor=validator_as_approver, idempotency_key="i2",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "validator" in decision.reason


def test_independent_validation_blocks_author_as_validator(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    author_as_validator = build_human_identity(
        subject="user:author", role_claims=["validator"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "INDEPENDENT_VALIDATION", "task_id": "task_iv2"},
        actor=author_as_validator, idempotency_key="i3",
    )
    assert authorize_command(db, cmd).allowed is False


def test_retier_self_request_is_denied(db):
    seed_authz_policy(db)
    rel = build_human_identity(subject="user:rel", role_claims=["release"])
    cmd = Command(
        action="retier", aggregate="feature", aggregate_id="feature_1",
        args={"feature_version_id": "fv_1", "new_risk_tier": "low",
              "requested_by": "user:rel"},
        actor=rel, idempotency_key="i5",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "four-eyes" in decision.reason


def test_retier_without_requester_is_denied(db):
    seed_authz_policy(db)
    rel = build_human_identity(subject="user:rel", role_claims=["release"])
    cmd = Command(
        action="retier", aggregate="feature", aggregate_id="feature_1",
        args={"feature_version_id": "fv_1", "new_risk_tier": "low"},
        actor=rel, idempotency_key="i6",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "dual-controlled" in decision.reason


def test_retier_two_party_is_allowed(db):
    seed_authz_policy(db)
    rel = build_human_identity(subject="user:rel", role_claims=["release"])
    cmd = Command(
        action="retier", aggregate="feature", aggregate_id="feature_1",
        args={"feature_version_id": "fv_1", "new_risk_tier": "low",
              "requested_by": "user:requester"},
        actor=rel, idempotency_key="i7",
    )
    assert authorize_command(db, cmd).allowed is True


def test_compliance_sensitive_activate_needs_four_eyes(db):
    seed_authz_policy(db)
    rel = build_human_identity(subject="user:rel", role_claims=["release"])
    cmd = Command(
        action="activate", aggregate="feature", aggregate_id="feature_1",
        args={"compliance_sensitive": True, "requested_by": "user:rel"},
        actor=rel, idempotency_key="i4",
    )
    assert authorize_command(db, cmd).allowed is False
