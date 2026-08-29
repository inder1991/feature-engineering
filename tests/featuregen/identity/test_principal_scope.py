"""B0a — the frozen principal/data-scope revision, its binding, and the two-prong recheck (1133).

What these tests pin, in the order the task states them: append-only physics on BOTH tables
(UPDATE/DELETE/TRUNCATE refuse); content-addressed identity (the same principal + the same scope
always answers the same id, and one row); the claim canonicalization that makes a scope a SET;
one authority per act (a second, different binding refuses); and the recheck's two prongs pinned
SEPARATELY — frozen claims authorize (a scope that has since GROWN still compiles under the frozen
set) and current claims gate (a revoked principal refuses; a narrowed one refuses; a payload
naming the wrong binding refuses).
"""
from __future__ import annotations

from datetime import UTC, datetime

import psycopg
import pytest

from featuregen.contracts import IdentityEnvelope
from featuregen.identity.local_session import (
    add_user_to_group,
    create_group,
    create_user,
    set_user_disabled,
)
from featuregen.identity.principal_scope import (
    PRINCIPAL_NOT_CURRENT,
    PRINCIPAL_SCOPE_BINDING_ID_PREFIX,
    PRINCIPAL_SCOPE_BINDING_MISMATCH,
    PRINCIPAL_SCOPE_ID_PREFIX,
    PRINCIPAL_SCOPE_MISSING,
    PRINCIPAL_SCOPE_NARROWED,
    PRINCIPAL_SCOPE_REFUSAL_CODES,
    PRINCIPAL_SCOPE_SUBJECT_KINDS,
    PrincipalScopeDefect,
    PrincipalScopeRebound,
    PrincipalScopeRevisionV1,
    ScopeAuthorizationRefused,
    authorize_frozen_scope,
    bind_principal_scope,
    ensure_principal_scope_revision,
    load_principal_scope_binding,
    load_principal_scope_revision,
    principal_scope_revision_of,
)

NOW = datetime(2026, 8, 24, tzinfo=UTC)


class _NeverTouched:
    """A probe standing in for a connection: ANY attribute access fails the test. Handing this to
    the store proves a refusal happened BEFORE SQL, not merely before commit."""

    def __getattr__(self, name):  # pragma: no cover - reaching here IS the failure
        raise AssertionError(f"the store touched the connection ({name!r}) before validating")


def _principal(subject="user:alice", roles=("catalog_read", "pii_reader"),
               **overrides) -> IdentityEnvelope:
    fields = dict(subject=subject, actor_kind="human", authenticated=True,
                  auth_method="password", role_claims=roles)
    fields.update(overrides)
    return IdentityEnvelope(**fields)


def _local_user(db, username="alice", roles=("catalog_read", "pii_reader"), group="analysts"):
    user_id = create_user(db, username, "long-test-password")
    group_id = create_group(db, group, roles)
    assert add_user_to_group(db, user_id, group_id)
    return user_id, group_id


def _bound(db, *, subject_id="gen-1", roles=("catalog_read", "pii_reader"),
           subject_kind="generation_request", decision=None) -> str:
    revision_id = ensure_principal_scope_revision(db, principal=_principal(roles=roles))
    return bind_principal_scope(
        db, revision_id=revision_id, subject_kind=subject_kind, subject_id=subject_id,
        action_decision_revision_id=decision)


# ══ the tables exist and are shaped ════════════════════════════════════════════════════════════
def test_the_1133_tables_exist_and_start_empty_shaped(db) -> None:
    assert db.execute(
        "SELECT revision_id, subject, actor_kind, authenticated, auth_method, role_claims, "
        "tenant, on_behalf_of, impersonation, break_glass, content_hash, recorded_at "
        "FROM principal_scope_revision LIMIT 0").fetchone() is None
    assert db.execute(
        "SELECT binding_id, revision_id, subject_kind, subject_id, "
        "action_decision_revision_id, content_hash, bound_at "
        "FROM principal_scope_binding LIMIT 0").fetchone() is None


# ══ content-addressed identity ═════════════════════════════════════════════════════════════════
def test_the_same_principal_and_scope_answer_the_same_id(db) -> None:
    first = ensure_principal_scope_revision(db, principal=_principal())
    second = ensure_principal_scope_revision(db, principal=_principal())
    assert first == second
    assert first.startswith(PRINCIPAL_SCOPE_ID_PREFIX)
    assert db.execute("SELECT count(*) FROM principal_scope_revision").fetchone()[0] == 1


def test_the_id_is_content_derived_never_random(db) -> None:
    revision = principal_scope_revision_of(_principal())
    assert ensure_principal_scope_revision(db, principal=_principal()) == revision.revision_id
    assert revision.revision_id == PRINCIPAL_SCOPE_ID_PREFIX + revision.content_hash


def test_a_different_scope_is_a_different_revision(db) -> None:
    """The whole point: the scope IS part of the identity, so a narrower principal can never be
    mistaken for a broader one."""
    broad = ensure_principal_scope_revision(db, principal=_principal())
    narrow = ensure_principal_scope_revision(
        db, principal=_principal(roles=("catalog_read",)))
    other = ensure_principal_scope_revision(db, principal=_principal(subject="user:bob"))
    assert len({broad, narrow, other}) == 3


def test_claim_order_and_repetition_are_not_identity() -> None:
    """A scope is a SET. Hashing the caller's ordering would fork the identity of an unchanged
    scope."""
    one = principal_scope_revision_of(_principal(roles=("pii_reader", "catalog_read")))
    two = principal_scope_revision_of(
        _principal(roles=("catalog_read", "pii_reader", "catalog_read")))
    assert one.revision_id == two.revision_id
    assert one.role_claims == ("catalog_read", "pii_reader")


def test_the_authentication_METHOD_is_part_of_the_scope_identity() -> None:
    """A stub-asserted identity and a proven one are not the same authority, and a content hash
    that ignored the difference would let the weaker one answer for the stronger."""
    proven = principal_scope_revision_of(_principal())
    asserted = principal_scope_revision_of(
        _principal(authenticated=False, auth_method="stub"))
    assert proven.revision_id != asserted.revision_id


def test_the_round_trip_loads_every_stored_fact(db) -> None:
    revision_id = ensure_principal_scope_revision(
        db, principal=_principal(tenant=None, on_behalf_of="user:bob", break_glass=True))
    loaded = load_principal_scope_revision(db, revision_id)
    assert loaded is not None
    assert loaded.subject == "user:alice"
    assert loaded.role_claims == ("catalog_read", "pii_reader")
    assert loaded.on_behalf_of == "user:bob"
    assert loaded.break_glass is True
    assert loaded.recorded_at is not None


def test_load_of_missing_id_returns_none(db) -> None:
    assert load_principal_scope_revision(db, PRINCIPAL_SCOPE_ID_PREFIX + "0" * 64) is None


@pytest.mark.parametrize("subject", ["", "   ", None, 3])
def test_a_blank_subject_is_refused_before_any_sql(subject) -> None:
    with pytest.raises(PrincipalScopeDefect):
        ensure_principal_scope_revision(
            _NeverTouched(), principal=_principal(subject=subject))


@pytest.mark.parametrize("roles", ["catalog_read", (None,), ("",), (3,)])
def test_claims_that_are_not_claim_strings_are_refused_before_any_sql(roles) -> None:
    with pytest.raises(PrincipalScopeDefect):
        ensure_principal_scope_revision(_NeverTouched(), principal=_principal(roles=roles))


# ══ the binding: one authority per act ═════════════════════════════════════════════════════════
def test_binding_is_idempotent_on_content(db) -> None:
    first = _bound(db)
    second = _bound(db)
    assert first == second
    assert first.startswith(PRINCIPAL_SCOPE_BINDING_ID_PREFIX)
    assert db.execute("SELECT count(*) FROM principal_scope_binding").fetchone()[0] == 1


def test_a_second_DIFFERENT_binding_for_one_act_refuses(db) -> None:
    _bound(db, subject_id="gen-1")
    with pytest.raises(PrincipalScopeRebound) as excinfo:
        _bound(db, subject_id="gen-1", roles=("catalog_read", "pii_reader", "restricted_reader"))
    assert "two answers" in str(excinfo.value)


def test_a_binding_cannot_name_an_unrecorded_authority(db) -> None:
    with pytest.raises(PrincipalScopeDefect) as excinfo:
        bind_principal_scope(
            db, revision_id=PRINCIPAL_SCOPE_ID_PREFIX + "f" * 64,
            subject_kind="generation_request", subject_id="gen-1")
    assert "never recorded" in str(excinfo.value)


@pytest.mark.parametrize("kind", ["build_set", "GENERATION_REQUEST", "", None])
def test_an_unknown_subject_kind_is_refused_before_any_sql(kind) -> None:
    with pytest.raises(PrincipalScopeDefect):
        bind_principal_scope(
            _NeverTouched(), revision_id=PRINCIPAL_SCOPE_ID_PREFIX + "a" * 64,
            subject_kind=kind, subject_id="gen-1")


def test_the_subject_vocabulary_matches_the_migrations_check(db) -> None:
    """One spelling of the closed set: the constant and the DDL, never two."""
    stored = db.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'principal_scope_binding_subject_kind_chk'").fetchone()[0]
    for kind in PRINCIPAL_SCOPE_SUBJECT_KINDS:
        assert f"'{kind}'" in stored


def test_the_binding_loads_by_act(db) -> None:
    binding_id = _bound(db, subject_id="gen-7", decision="dec-1")
    loaded = load_principal_scope_binding(
        db, subject_kind="generation_request", subject_id="gen-7")
    assert loaded is not None
    assert loaded.binding_id == binding_id
    assert loaded.action_decision_revision_id == "dec-1"
    assert load_principal_scope_binding(
        db, subject_kind="generation_request", subject_id="gen-nope") is None


# ══ append-only physics, BOTH tables ═══════════════════════════════════════════════════════════
@pytest.mark.parametrize("table", ["principal_scope_revision", "principal_scope_binding"])
def test_update_and_delete_refuse(db, table) -> None:
    _bound(db)
    for statement in (f"UPDATE {table} SET content_hash = 'x'", f"DELETE FROM {table}"):
        with pytest.raises(psycopg.errors.RaiseException) as excinfo:
            with db.transaction():
                db.execute(statement)
        assert "append-only" in str(excinfo.value)
        assert table in str(excinfo.value)


@pytest.mark.parametrize("table", ["principal_scope_revision", "principal_scope_binding"])
def test_truncate_refuses(db, table) -> None:
    with pytest.raises(psycopg.errors.RaiseException) as excinfo:
        with db.transaction():
            db.execute(f"TRUNCATE {table}")
    assert "append-only" in str(excinfo.value)


def test_the_content_hash_is_unique_under_a_hostile_second_insert(db) -> None:
    """The DB backstop behind the store's ON CONFLICT idiom: one content, one id, one row."""
    revision = principal_scope_revision_of(_principal())
    ensure_principal_scope_revision(db, principal=_principal())
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO principal_scope_revision (revision_id, subject, actor_kind, "
                "authenticated, auth_method, role_claims, break_glass, content_hash) "
                "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                (PRINCIPAL_SCOPE_ID_PREFIX + "b" * 64, "user:mallory", "human", True,
                 "password", '["admin"]', False, revision.content_hash))


# ══ THE RECHECK — two prongs, pinned separately ════════════════════════════════════════════════
def test_PRONG_ONE_the_FROZEN_claims_authorize(db) -> None:
    """What compiles is the frozen set — not what the principal happens to hold now. Here the
    principal has GAINED a claim since; the authorization still carries exactly the frozen two."""
    _local_user(db, roles=("catalog_read", "pii_reader", "restricted_reader"))
    binding_id = _bound(db, roles=("catalog_read", "pii_reader"))

    authorized = authorize_frozen_scope(
        db, subject_kind="generation_request", subject_id="gen-1",
        claimed_binding_id=binding_id, observed_at=NOW)

    assert authorized.role_claims == ("catalog_read", "pii_reader")
    assert authorized.current_role_claims == (
        "catalog_read", "pii_reader", "restricted_reader")


def test_PRONG_TWO_a_REVOKED_principal_does_not_execute_on_frozen_claims(db) -> None:
    user_id, _ = _local_user(db)
    binding_id = _bound(db)
    assert set_user_disabled(db, user_id, True)

    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-1",
            claimed_binding_id=binding_id, observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_NOT_CURRENT


def test_PRONG_TWO_an_UNVERIFIABLE_principal_refuses(db) -> None:
    """Fail-closed, the posture `formula_draft_worker._author` already takes: the platform declines
    to read the catalog on behalf of a principal it cannot vouch for."""
    binding_id = _bound(db)  # no local account for user:alice at all

    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-1",
            claimed_binding_id=binding_id, observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_NOT_CURRENT


def test_PRONG_TWO_a_NARROWED_scope_refuses_rather_than_being_reinterpreted(db) -> None:
    """The frozen authorization is not silently re-read as "whatever is left"."""
    _, group_id = _local_user(db)
    binding_id = _bound(db)
    db.execute("DELETE FROM app_group_role WHERE group_id = %s AND role = 'pii_reader'",
               (group_id,))

    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-1",
            claimed_binding_id=binding_id, observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_SCOPE_NARROWED
    assert "pii_reader" in excinfo.value.detail


def test_a_payload_naming_ANOTHER_acts_binding_refuses(db) -> None:
    _local_user(db)
    _bound(db, subject_id="gen-1")
    other = _bound(db, subject_id="gen-2", roles=("catalog_read",))

    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-1",
            claimed_binding_id=other, observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_SCOPE_BINDING_MISMATCH


def test_a_payload_naming_NO_binding_refuses(db) -> None:
    _local_user(db)
    _bound(db, subject_id="gen-1")

    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-1",
            claimed_binding_id=None, observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_SCOPE_MISSING


def test_an_act_with_NO_recorded_scope_refuses(db) -> None:
    """A work item that reached the queue without ever being authorized is a bypass however it got
    there."""
    with pytest.raises(ScopeAuthorizationRefused) as excinfo:
        authorize_frozen_scope(
            db, subject_kind="generation_request", subject_id="gen-ghost",
            claimed_binding_id="psb_whatever", observed_at=NOW)
    assert excinfo.value.code == PRINCIPAL_SCOPE_MISSING


def test_every_refusal_code_is_a_real_compilation_refusal(db) -> None:
    """The identity module and the compiler vocabulary spell these ONCE. The lane converts by
    value, so a drift would raise rather than refuse under an invented code."""
    from featuregen.materialize.codes import CompilationRefusalCode

    for code in PRINCIPAL_SCOPE_REFUSAL_CODES:
        assert CompilationRefusalCode(code).value == code


def test_a_revision_that_fails_content_verification_is_never_served(db) -> None:
    """Corruption raises rather than answering — the A3 store discipline."""
    from featuregen.identity.principal_scope import PrincipalScopeStoreConflict

    revision = PrincipalScopeRevisionV1(
        subject="user:alice", actor_kind="human", authenticated=True, auth_method="password",
        role_claims=("catalog_read",))
    db.execute(
        "INSERT INTO principal_scope_revision (revision_id, subject, actor_kind, authenticated, "
        "auth_method, role_claims, break_glass, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
        (revision.revision_id, "user:mallory", "human", True, "password", '["admin"]', False,
         revision.content_hash))
    with pytest.raises(PrincipalScopeStoreConflict):
        load_principal_scope_revision(db, revision.revision_id)
