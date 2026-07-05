"""Local username/password authentication → a session token (Bearer)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import get_conn
from featuregen.identity.local_session import login, logout

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]


class LoginIn(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LogoutIn(BaseModel):
    token: str


@router.post("/auth/login")
def login_route(body: LoginIn, conn: _Conn) -> dict:
    """Verify credentials, mint a session, return the Bearer token. 401 on bad credentials (no hint
    which of username/password was wrong)."""
    token = login(conn, body.username, body.password, now=datetime.now(UTC))
    if token is None:
        raise HTTPException(status_code=401, detail="invalid username or password")
    return {"token": token, "token_type": "bearer"}


@router.post("/auth/logout")
def logout_route(body: LogoutIn, conn: _Conn) -> dict:
    logout(conn, body.token)
    return {"ok": True}
