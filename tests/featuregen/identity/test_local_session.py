from datetime import datetime, timedelta, timezone

from featuregen.identity.local_session import (
    add_user_to_group,
    create_group,
    create_user,
    hash_password,
    login,
    logout,
    resolve_session,
    verify_password,
)

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def test_password_hash_roundtrip():
    h = hash_password("s3cret")
    assert verify_password("s3cret", h)
    assert not verify_password("wrong", h)
    assert not verify_password("s3cret", "garbage$not$a$hash")


def test_login_mints_session_resolving_to_authenticated_identity_with_group_roles(db):
    uid = create_user(db, "alice", "pw")
    gid = create_group(db, "analysts", roles=("data_owner", "pii_reader"))
    add_user_to_group(db, uid, gid)
    token = login(db, "alice", "pw", now=NOW)
    assert token
    ident = resolve_session(db, token, now=NOW)
    assert ident is not None
    assert ident.authenticated is True            # minted through the trust capability, not asserted
    assert ident.subject == "user:alice"
    assert ident.auth_method == "password"
    assert set(ident.role_claims) == {"data_owner", "pii_reader"}   # roles come from the group
    assert "analysts" in ident.groups


def test_bad_credentials_and_unknown_user_do_not_mint(db):
    create_user(db, "bob", "right")
    assert login(db, "bob", "wrong", now=NOW) is None
    assert login(db, "nobody", "x", now=NOW) is None


def test_expired_session_does_not_resolve(db):
    create_user(db, "carol", "pw")
    token = login(db, "carol", "pw", now=NOW, ttl=timedelta(hours=1))
    assert resolve_session(db, token, now=NOW + timedelta(minutes=30)) is not None
    assert resolve_session(db, token, now=NOW + timedelta(hours=2)) is None


def test_logout_and_bogus_token_do_not_resolve(db):
    create_user(db, "dave", "pw")
    token = login(db, "dave", "pw", now=NOW)
    assert resolve_session(db, token, now=NOW) is not None
    logout(db, token)
    assert resolve_session(db, token, now=NOW) is None
    assert resolve_session(db, "bogus", now=NOW) is None
    assert resolve_session(db, None, now=NOW) is None


def test_unknown_user_verify_is_full_cost_no_timing_oracle():
    # the dummy hash the unknown-user path verifies against must use the SAME round count as real
    # hashes, so "no such user" and "wrong password" take the same time (no enumeration oracle).
    from featuregen.identity.local_session import _DUMMY_HASH, _PBKDF2_ROUNDS
    assert _DUMMY_HASH.startswith(f"pbkdf2_sha256${_PBKDF2_ROUNDS}$")


def test_last_admin_guard(db):
    from featuregen.identity.local_session import (
        add_user_to_group,
        create_group,
        create_user,
        is_last_admin,
    )
    a = create_user(db, "a", "pw")
    g = create_group(db, "admins", roles=("admin",))
    add_user_to_group(db, a, g)
    assert is_last_admin(db, a) is True                 # only admin
    b = create_user(db, "b", "pw")
    add_user_to_group(db, b, g)
    assert is_last_admin(db, a) is False                # a second admin exists
    assert is_last_admin(db, create_user(db, "c", "pw")) is False   # a non-admin is never "last admin"
