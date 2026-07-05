"""Local username/password → session-token authentication (users → groups → roles).

Lives inside `identity/` — a sanctioned trust root — because resolving a valid session mints an
*authenticated* human principal via the private trust capability (SP-0.5 BLOCKER #1). This is the local
auth mode until an OIDC IdP lands; OIDC (`identity/verify.py`) is the other `IdentityVerifier`. Roles are
derived ONLY from the user's group memberships — never from a request header/param. The raw session token
is returned to the client once at login and stored only as a SHA-256 hash (like a password).
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.identity._trust import _TRUST_CAPABILITY
from featuregen.identity.build import build_human_identity

_PBKDF2_ROUNDS = 200_000
DEFAULT_SESSION_TTL = timedelta(hours=8)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_hex, hash_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(rounds))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk.hex(), hash_hex)   # constant-time


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


# ---- provisioning (users / groups / roles) ------------------------------------------------------
def create_user(conn: DbConn, username: str, password: str) -> str:
    user_id = _new_id("usr")
    conn.execute("INSERT INTO app_user (user_id, username, password_hash) VALUES (%s, %s, %s)",
                 (user_id, username, hash_password(password)))
    return user_id


def create_group(conn: DbConn, name: str, roles: tuple[str, ...] = ()) -> str:
    group_id = _new_id("grp")
    conn.execute("INSERT INTO app_group (group_id, name) VALUES (%s, %s)", (group_id, name))
    for role in roles:
        conn.execute("INSERT INTO app_group_role (group_id, role) VALUES (%s, %s) "
                     "ON CONFLICT DO NOTHING", (group_id, role))
    return group_id


def add_user_to_group(conn: DbConn, user_id: str, group_id: str) -> bool:
    """Idempotent. Returns False if the user or group doesn't exist (checked first, so a bad id can't
    abort the transaction on a foreign-key violation)."""
    exists = conn.execute(
        "SELECT (SELECT 1 FROM app_user WHERE user_id = %s), "
        "(SELECT 1 FROM app_group WHERE group_id = %s)", (user_id, group_id)).fetchone()
    if not (exists[0] and exists[1]):
        return False
    conn.execute("INSERT INTO app_user_group (user_id, group_id) VALUES (%s, %s) "
                 "ON CONFLICT DO NOTHING", (user_id, group_id))
    return True


# ---- login + session resolution -----------------------------------------------------------------
def login(conn: DbConn, username: str, password: str, *, now: datetime,
          ttl: timedelta = DEFAULT_SESSION_TTL) -> str | None:
    """Verify credentials and mint a session; returns the RAW token (stored hashed), or None on a bad
    username / password / disabled account. A wrong username still runs a verify to avoid timing leaks."""
    row = conn.execute("SELECT user_id, password_hash, disabled FROM app_user WHERE username = %s",
                       (username,)).fetchone()
    stored = row[1] if row else "pbkdf2_sha256$1$00$00"   # dummy verify on unknown user (timing)
    ok = verify_password(password, stored)
    if row is None or row[2] or not ok:
        return None
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO app_session (token_hash, user_id, expires_at) VALUES (%s, %s, %s)",
                 (_token_hash(token), row[0], now + ttl))
    return token


def resolve_session(conn: DbConn, token: str | None, *, now: datetime) -> IdentityEnvelope | None:
    """A valid, unexpired session token → an AUTHENTICATED human principal whose roles are aggregated
    from the user's group memberships. None if the token is missing / unknown / expired / disabled."""
    if not token:
        return None
    row = conn.execute(
        "SELECT s.user_id, u.username, u.disabled, s.expires_at FROM app_session s "
        "JOIN app_user u ON u.user_id = s.user_id WHERE s.token_hash = %s",
        (_token_hash(token),)).fetchone()
    if row is None or row[2] or row[3] <= now:
        return None
    user_id, username = row[0], row[1]
    memberships = conn.execute(
        "SELECT g.name, gr.role FROM app_user_group ug "
        "JOIN app_group g ON g.group_id = ug.group_id "
        "LEFT JOIN app_group_role gr ON gr.group_id = g.group_id WHERE ug.user_id = %s",
        (user_id,)).fetchall()
    groups = sorted({m[0] for m in memberships})
    roles = sorted({m[1] for m in memberships if m[1]})
    # authenticated=True — minted here because identity/ holds the trust capability (SP-0.5 BLOCKER #1).
    return build_human_identity(subject=f"user:{username}", role_claims=roles,
                                auth_method="password", groups=groups, _capability=_TRUST_CAPABILITY)


def logout(conn: DbConn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM app_session WHERE token_hash = %s", (_token_hash(token),))


# ---- administration (list / mutate users, groups, roles, membership) ----------------------------
def list_users(conn: DbConn) -> list[dict]:
    rows = conn.execute("SELECT user_id, username, disabled FROM app_user ORDER BY username").fetchall()
    return [{"user_id": r[0], "username": r[1], "disabled": r[2]} for r in rows]


def set_user_disabled(conn: DbConn, user_id: str, disabled: bool) -> bool:
    # resolve_session already rejects a disabled user, so existing tokens stop working immediately.
    row = conn.execute("UPDATE app_user SET disabled = %s WHERE user_id = %s RETURNING user_id",
                       (disabled, user_id)).fetchone()
    return row is not None


def set_password(conn: DbConn, user_id: str, password: str) -> bool:
    row = conn.execute("UPDATE app_user SET password_hash = %s WHERE user_id = %s RETURNING user_id",
                       (hash_password(password), user_id)).fetchone()
    if row is not None:
        conn.execute("DELETE FROM app_session WHERE user_id = %s", (user_id,))   # revoke old sessions
    return row is not None


def delete_user(conn: DbConn, user_id: str) -> bool:
    row = conn.execute("DELETE FROM app_user WHERE user_id = %s RETURNING user_id",
                       (user_id,)).fetchone()
    return row is not None   # sessions + memberships cascade (FK ON DELETE CASCADE)


def list_groups(conn: DbConn) -> list[dict]:
    rows = conn.execute(
        "SELECT g.group_id, g.name, "
        "COALESCE(array_agg(gr.role) FILTER (WHERE gr.role IS NOT NULL), '{}') "
        "FROM app_group g LEFT JOIN app_group_role gr ON gr.group_id = g.group_id "
        "GROUP BY g.group_id, g.name ORDER BY g.name").fetchall()
    return [{"group_id": r[0], "name": r[1], "roles": sorted(r[2])} for r in rows]


def grant_role(conn: DbConn, group_id: str, role: str) -> bool:
    if conn.execute("SELECT 1 FROM app_group WHERE group_id = %s", (group_id,)).fetchone() is None:
        return False
    conn.execute("INSERT INTO app_group_role (group_id, role) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                 (group_id, role))
    return True


def revoke_role(conn: DbConn, group_id: str, role: str) -> None:
    conn.execute("DELETE FROM app_group_role WHERE group_id = %s AND role = %s", (group_id, role))


def remove_user_from_group(conn: DbConn, user_id: str, group_id: str) -> None:
    conn.execute("DELETE FROM app_user_group WHERE user_id = %s AND group_id = %s",
                 (user_id, group_id))


def delete_group(conn: DbConn, group_id: str) -> bool:
    row = conn.execute("DELETE FROM app_group WHERE group_id = %s RETURNING group_id",
                       (group_id,)).fetchone()
    return row is not None


def user_count(conn: DbConn) -> int:
    return conn.execute("SELECT count(*) FROM app_user").fetchone()[0]


def bootstrap_admin(conn: DbConn, username: str, password: str, *, admin_role: str = "admin") -> str | None:
    """Create the first admin (user + an 'admins' group granting the admin role) ONLY when there are
    no users yet — a one-time, first-run action. Returns the new user_id, or None if users exist."""
    if user_count(conn) > 0:
        return None
    uid = create_user(conn, username, password)
    gid = create_group(conn, "admins", roles=(admin_role,))
    add_user_to_group(conn, uid, gid)
    return uid
