from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.identity.current_principal import (
    WORKER_IDENTITY_RESOLVER_ENV,
    CurrentPrincipalResolution,
    LocalWorkerIdentityResolver,
    PrincipalResolutionStatus,
    WorkerIdentityResolverUnavailable,
    configured_worker_identity_resolver,
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


# ══ B0a fix round — THE RESOLVER SEAM IS A DEPLOYMENT SETTING, not an edit to the lane ═════════
class _AlwaysCurrent:
    """A deployment adapter standing in for an external IdP: it answers for subjects local IAM
    cannot (`service:`, tenanted, externally-held)."""

    def resolve_current_principal(self, conn, frozen_subject, frozen_tenant, observed_at):
        from featuregen.contracts.envelopes import IdentityEnvelope

        return CurrentPrincipalResolution(
            PrincipalResolutionStatus.CURRENT,
            principal=IdentityEnvelope(
                subject=frozen_subject, actor_kind="service", authenticated=True,
                auth_method="oidc", role_claims=("catalog_read", "pii_reader")))


def test_the_default_authority_is_local_iam(monkeypatch) -> None:
    monkeypatch.delenv(WORKER_IDENTITY_RESOLVER_ENV, raising=False)
    assert isinstance(configured_worker_identity_resolver(), LocalWorkerIdentityResolver)


def test_a_CONFIGURED_ADAPTER_ANSWERS_for_subjects_local_iam_cannot(db, monkeypatch) -> None:
    """▲ THE SEAM, WIRED. Before this it was a keyword argument nobody passed: "inject a resolver"
    meant editing the lane. A deployment whose principals are service accounts now names its
    authority once, and every prong — the draft author, the recipe author, and the generation
    lane's second prong — runs against it."""
    monkeypatch.setenv(WORKER_IDENTITY_RESOLVER_ENV,
                       f"{__name__}:_AlwaysCurrent")

    unconfigured = LocalWorkerIdentityResolver().resolve_current_principal(
        db, "service:pipeline", None, NOW)
    assert unconfigured.status is PrincipalResolutionStatus.UNVERIFIABLE

    resolved = resolve_current_principal(db, "service:pipeline", None, NOW)
    assert resolved.status is PrincipalResolutionStatus.CURRENT
    assert resolved.principal is not None
    assert resolved.principal.role_claims == ("catalog_read", "pii_reader")


def test_a_NAMED_BUT_UNLOADABLE_authority_RAISES_rather_than_falling_back(monkeypatch) -> None:
    """Falling back to local IAM would report every one of that deployment's principals as
    unverifiable — an outage wearing a misconfiguration's clothes. The refusal names the variable."""
    for value in ("not_a_reference", "featuregen.nope:Resolver",
                  "featuregen.identity.current_principal:NoSuchThing",
                  "featuregen.identity.current_principal:CurrentPrincipalResolution"):
        monkeypatch.setenv(WORKER_IDENTITY_RESOLVER_ENV, value)
        with pytest.raises(WorkerIdentityResolverUnavailable) as excinfo:
            configured_worker_identity_resolver()
        assert WORKER_IDENTITY_RESOLVER_ENV in str(excinfo.value)


def test_an_EXPLICIT_resolver_still_wins(db, monkeypatch) -> None:
    """A caller that already holds an authority — a test, or a lane given one — is not overridden
    by the environment."""
    monkeypatch.setenv(WORKER_IDENTITY_RESOLVER_ENV, f"{__name__}:_AlwaysCurrent")
    assert resolve_current_principal(
        db, "service:pipeline", None, NOW,
        resolver=LocalWorkerIdentityResolver()).status is PrincipalResolutionStatus.UNVERIFIABLE


def test_the_FROZEN_SCOPE_RECHECK_runs_against_the_CONFIGURED_authority(db, monkeypatch) -> None:
    """The generation lane passes no resolver ON PURPOSE, so the authority is a deployment setting
    rather than a per-call-site decision. Here a service principal — refused by local IAM — is
    authorized because the deployment named an adapter that can answer for it."""
    from featuregen.contracts.envelopes import IdentityEnvelope
    from featuregen.identity.principal_scope import (
        ScopeAuthorizationRefused,
        authorize_frozen_scope,
        bind_principal_scope,
        ensure_principal_scope_revision,
    )

    revision_id = ensure_principal_scope_revision(db, principal=IdentityEnvelope(
        subject="service:pipeline", actor_kind="service", authenticated=True,
        auth_method="oidc", role_claims=("catalog_read",)))
    binding_id = bind_principal_scope(
        db, revision_id=revision_id, subject_kind="generation_request", subject_id="gen-svc")

    monkeypatch.delenv(WORKER_IDENTITY_RESOLVER_ENV, raising=False)
    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(db, subject_kind="generation_request", subject_id="gen-svc",
                               claimed_binding_id=binding_id, observed_at=NOW)
    assert excinfo.value.code == "PRINCIPAL_NOT_CURRENT"

    monkeypatch.setenv(WORKER_IDENTITY_RESOLVER_ENV, f"{__name__}:_AlwaysCurrent")
    authorized = authorize_frozen_scope(
        db, subject_kind="generation_request", subject_id="gen-svc",
        claimed_binding_id=binding_id, observed_at=NOW)
    assert authorized.role_claims == ("catalog_read",)
