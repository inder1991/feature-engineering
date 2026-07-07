"""User / group / role administration. All endpoints require the iam:manage permission (access_admin /
platform_admin) AND an authenticated principal, EXCEPT the one-time first-run /admin/bootstrap (works
only while the user table is empty)."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from featuregen.api.deps import audit_access_denied, get_conn, get_identity
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.local_session import (
    add_user_to_group,
    bootstrap_admin,
    create_group,
    create_user,
    delete_group,
    delete_user,
    enabled_admin_count,
    grant_role,
    is_last_admin,
    list_groups,
    list_users,
    remove_user_from_group,
    revoke_role,
    set_password,
    set_user_disabled,
)
from featuregen.identity.permissions import IAM_MANAGE, has_permission

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


def require_admin(request: Request,
                  identity: Annotated[IdentityEnvelope, Depends(get_identity)]) -> IdentityEnvelope:
    # The crown jewel: require a PROVEN principal with the iam:manage permission (access_admin /
    # platform_admin), not just self-asserted roles. An unauthenticated stub identity must NOT reach
    # the admin control plane even if the stub is on — defense-in-depth on top of stub-off-in-prod.
    if not (identity.authenticated and has_permission(identity.role_claims, IAM_MANAGE)):
        audit_access_denied(identity, f"{IAM_MANAGE} on {request.method} {request.url.path}")
        raise HTTPException(status_code=403, detail="iam:manage permission required")
    return identity


_Admin = Annotated[IdentityEnvelope, Depends(require_admin)]


class BootstrapIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)


class UserIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=8)


class PasswordIn(BaseModel):
    password: str = Field(min_length=8)


class GroupIn(BaseModel):
    name: str = Field(min_length=1)
    roles: list[str] = []


class RoleIn(BaseModel):
    role: str = Field(min_length=1)


class MemberIn(BaseModel):
    user_id: str = Field(min_length=1)


# ---- bootstrap (first run only) -----------------------------------------------------------------
@router.post("/admin/bootstrap")
def bootstrap(body: BootstrapIn, conn: _Conn) -> dict:
    try:
        uid = bootstrap_admin(conn, body.username, body.password)
    except psycopg.errors.UniqueViolation as exc:   # e.g. a stale 'admins' group / username race
        raise HTTPException(status_code=409, detail="bootstrap conflict; retry") from exc
    if uid is None:
        raise HTTPException(status_code=409, detail="users already exist; bootstrap is first-run only")
    return {"user_id": uid}


# ---- users --------------------------------------------------------------------------------------
@router.get("/admin/users")
def get_users(conn: _Conn, admin: _Admin) -> list[dict]:
    return list_users(conn)


@router.post("/admin/users")
def post_user(body: UserIn, conn: _Conn, admin: _Admin) -> dict:
    try:
        return {"user_id": create_user(conn, body.username, body.password)}
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="username already exists") from exc


@router.post("/admin/users/{user_id}/disable")
def disable_user(user_id: str, conn: _Conn, admin: _Admin) -> dict:
    if is_last_admin(conn, user_id):
        raise HTTPException(status_code=409, detail="cannot disable the last admin")
    if not set_user_disabled(conn, user_id, True):
        raise HTTPException(status_code=404, detail="no such user")
    return {"disabled": True}


@router.post("/admin/users/{user_id}/enable")
def enable_user(user_id: str, conn: _Conn, admin: _Admin) -> dict:
    if not set_user_disabled(conn, user_id, False):
        raise HTTPException(status_code=404, detail="no such user")
    return {"disabled": False}


@router.post("/admin/users/{user_id}/password")
def reset_password(user_id: str, body: PasswordIn, conn: _Conn, admin: _Admin) -> dict:
    if not set_password(conn, user_id, body.password):
        raise HTTPException(status_code=404, detail="no such user")
    return {"ok": True}


@router.delete("/admin/users/{user_id}")
def remove_user(user_id: str, conn: _Conn, admin: _Admin) -> dict:
    if is_last_admin(conn, user_id):
        raise HTTPException(status_code=409, detail="cannot delete the last admin")
    if not delete_user(conn, user_id):
        raise HTTPException(status_code=404, detail="no such user")
    return {"deleted": True}


# ---- groups / roles / membership ----------------------------------------------------------------
@router.get("/admin/groups")
def get_groups(conn: _Conn, admin: _Admin) -> list[dict]:
    return list_groups(conn)


@router.post("/admin/groups")
def post_group(body: GroupIn, conn: _Conn, admin: _Admin) -> dict:
    try:
        return {"group_id": create_group(conn, body.name, tuple(body.roles))}
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail="group name already exists") from exc


@router.delete("/admin/groups/{group_id}")
def remove_group(group_id: str, conn: _Conn, admin: _Admin) -> dict:
    if not delete_group(conn, group_id):
        raise HTTPException(status_code=404, detail="no such group")
    if enabled_admin_count(conn) == 0:   # raise -> request tx rolls the delete back
        raise HTTPException(status_code=409, detail="that group holds the last admin; cannot delete it")
    return {"deleted": True}


@router.post("/admin/groups/{group_id}/roles")
def add_role(group_id: str, body: RoleIn, conn: _Conn, admin: _Admin) -> dict:
    if not grant_role(conn, group_id, body.role):
        raise HTTPException(status_code=404, detail="no such group")
    return {"ok": True}


@router.delete("/admin/groups/{group_id}/roles/{role}")
def remove_role(group_id: str, role: str, conn: _Conn, admin: _Admin) -> dict:
    revoke_role(conn, group_id, role)
    if enabled_admin_count(conn) == 0:   # revoking 'admin' from the last admin-granting group -> rollback
        raise HTTPException(status_code=409, detail="that would revoke the last admin role")
    return {"ok": True}


@router.post("/admin/groups/{group_id}/members")
def add_member(group_id: str, body: MemberIn, conn: _Conn, admin: _Admin) -> dict:
    if not add_user_to_group(conn, body.user_id, group_id):
        raise HTTPException(status_code=404, detail="no such user or group")
    return {"ok": True}


@router.delete("/admin/groups/{group_id}/members/{user_id}")
def remove_member(group_id: str, user_id: str, conn: _Conn, admin: _Admin) -> dict:
    remove_user_from_group(conn, user_id, group_id)
    if enabled_admin_count(conn) == 0:   # removing the last admin from the admin group -> rollback
        raise HTTPException(status_code=409, detail="that would remove the last admin")
    return {"ok": True}
