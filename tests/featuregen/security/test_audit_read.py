import pytest
from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.security.audit import (
    AuditReadDenied,
    read_security_audit,
    record_security_event,
)


def _seed(db):
    a = mint_test_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="nope",
    )


def test_security_role_can_read_and_read_is_logged(db):
    _seed(db)
    sec = mint_test_identity(subject="user:sec", role_claims=["security"])
    rows = read_security_audit(db, sec)
    types = {r[1] for r in rows}
    assert "COMMAND_DENIED" in types
    assert "AUDIT_READ" in types  # the read logged itself
    logged = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert logged == [("flagged",)]


def test_feature_owner_cannot_read_security_stream(db):
    _seed(db)
    owner = mint_test_identity(subject="user:owner", role_claims=["owner"])
    with pytest.raises(AuditReadDenied):
        read_security_audit(db, owner)
    denied = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert denied == [("denied",)]


def test_unauthenticated_envelope_with_security_role_is_denied(db):
    _seed(db)
    spoofed = IdentityEnvelope(
        subject="user:spoof",
        actor_kind="human",
        authenticated=False,
        auth_method="oidc",
        role_claims=("security",),
    )
    with pytest.raises(AuditReadDenied):
        read_security_audit(db, spoofed)
    decisions = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert decisions == [("denied",)]
