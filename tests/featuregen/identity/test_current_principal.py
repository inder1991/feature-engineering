from __future__ import annotations

from datetime import UTC, datetime

from featuregen.identity.current_principal import (
    PrincipalResolutionStatus,
    resolve_current_principal,
)
from featuregen.identity.local_session import (
    add_user_to_group,
    create_group,
    create_user,
    set_user_disabled,
)

NOW = datetime(2026, 7, 26, tzinfo=UTC)


def _user(db):
    user_id = create_user(db, "alice", "long-test-password")
    group_id = create_group(db, "analysts", ("catalog_read", "pii_reader"))
    assert add_user_to_group(db, user_id, group_id)
    return user_id, group_id


def test_local_worker_resolves_current_roles_without_a_bearer_token(db):
    _user(db)
    result = resolve_current_principal(db, "user:alice", None, NOW)
    assert result.status is PrincipalResolutionStatus.CURRENT
    assert result.principal is not None
    assert result.principal.authenticated
    assert result.principal.subject == "user:alice"
    assert result.principal.groups == ("analysts",)
    assert result.principal.role_claims == ("catalog_read", "pii_reader")


def test_local_worker_observes_disable_and_role_revocation(db):
    user_id, group_id = _user(db)
    db.execute(
        "DELETE FROM app_group_role WHERE group_id = %s AND role = 'pii_reader'",
        (group_id,),
    )
    current = resolve_current_principal(db, "user:alice", None, NOW)
    assert current.principal is not None
    assert current.principal.role_claims == ("catalog_read",)
    assert set_user_disabled(db, user_id, True)
    revoked = resolve_current_principal(db, "user:alice", None, NOW)
    assert revoked.status is PrincipalResolutionStatus.REVOKED
    assert revoked.principal is None


def test_worker_does_not_substitute_service_or_unverifiable_tenant(db):
    service = resolve_current_principal(db, "service:worker", None, NOW)
    assert service.status is PrincipalResolutionStatus.UNVERIFIABLE
    _user(db)
    tenant = resolve_current_principal(db, "user:alice", "tenant-a", NOW)
    assert tenant.status is PrincipalResolutionStatus.UNVERIFIABLE
