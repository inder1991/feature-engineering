import pytest

from featuregen.contracts import IdentityEnvelope
from featuregen.governance.replay import ReplayMode
from featuregen.privacy.audit_read import AuditReadDenied, AuditView, read_audit

ACTOR = IdentityEnvelope(
    subject="user:auditor", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("auditor",),
)


def _seed_event(db, run_id):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES (%s,'run',%s,1,%s,'RUN_OPENED',1,1,"
        "'{\"subject\":\"s\",\"actor_kind\":\"service\",\"authenticated\":true,"
        "\"auth_method\":\"workload-identity\",\"role_claims\":[]}'::jsonb, '{}'::jsonb, "
        "'{\"artifact_type\":\"DRAFT_CONTRACT\",\"schema_version\":1,\"producing_component\":\"featuregen@1\"}'::jsonb, now())",
        ("evt_" + run_id, run_id, run_id),
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, conn, *, event_type, actor, attempted_action, decision,
                 reason=None, aggregate=None, aggregate_id=None):
        self.calls.append((event_type, decision, attempted_action))
        return "sec_" + str(len(self.calls))


class _Decision:
    # Duck-types Phase 07's AuthzDecision(allowed, reason); read_audit reads only these.
    def __init__(self, allowed, reason=None):
        self.allowed = allowed
        self.reason = reason


def _allow(conn, cmd):
    assert cmd.action == "read_audit"   # canonical §6.2 action wired through to the authorizer
    return _Decision(True)


def _deny(conn, cmd):
    assert cmd.action == "read_audit"
    return _Decision(False, "no matching authz policy")


def test_authorized_read_returns_labeled_view_and_logs_audit_read(db):
    _seed_event(db, "run_a")
    rec = _Recorder()
    view = read_audit(
        db, run_id="run_a", actor=ACTOR,
        authorize_command=_allow,
        record_security_event=rec,
    )
    assert isinstance(view, AuditView)
    assert view.run_id == "run_a"
    assert view.mode is ReplayMode.FULL
    assert len(view.events) == 1
    assert rec.calls == [("AUDIT_READ", "flagged", "read_audit")]


def test_denied_read_logs_denial_and_raises_without_returning_data(db):
    _seed_event(db, "run_b")
    rec = _Recorder()
    with pytest.raises(AuditReadDenied):
        read_audit(
            db, run_id="run_b", actor=ACTOR,
            authorize_command=_deny,
            record_security_event=rec,
        )
    assert rec.calls == [("AUDIT_READ", "denied", "read_audit")]
