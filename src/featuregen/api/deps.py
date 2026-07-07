"""Request-scoped dependencies: DB transaction, stub session auth, fail-closed LLM gate."""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated

import psycopg
from fastapi import Depends, Header, HTTPException, Request

from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.build import IdentityError, build_human_identity
from featuregen.identity.local_session import resolve_session
from featuregen.identity.permissions import (
    CATALOG_READ,
    CATALOG_WRITE,
    FEATURE_GENERATE,
    FEATURE_READ,
    has_permission,
)
from featuregen.intake.llm import LLMClient


def require_permission(permission: str):
    """A route dependency that 403s unless the caller's roles grant `permission`. Roles come from the
    real Bearer session (prod) or the header stub (dev, stub-enabled) — the same source as read-scope,
    so this is stub-compatible; production self-granting is blocked by the stub being OFF, and iam:manage
    additionally requires an authenticated principal (see require_admin)."""

    def _dep(identity: Annotated[IdentityEnvelope, Depends(get_identity)]) -> IdentityEnvelope:
        if not has_permission(identity.role_claims, permission):
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return identity

    return _dep


# Prebuilt route-level guards (used as `dependencies=[Depends(...)]` — the route still injects the
# identity separately for read-scope, and FastAPI caches get_identity so it resolves once per request).
require_catalog_read = require_permission(CATALOG_READ)
require_catalog_write = require_permission(CATALOG_WRITE)
require_feature_read = require_permission(FEATURE_READ)
require_feature_generate = require_permission(FEATURE_GENERATE)


def _auth_stub_enabled() -> bool:
    """The header stub (X-User/X-Roles -> authenticated=False) is OFF by default — secure in prod, where
    only a real Bearer session authenticates. Dev/tests set FEATUREGEN_AUTH_STUB=1 to keep it."""
    return os.environ.get("FEATUREGEN_AUTH_STUB", "0") == "1"


def get_conn() -> Iterator[psycopg.Connection]:
    """One connection + transaction per request: commit on success, rollback on any error.
    Ingest stays all-or-nothing; reads never leave dangling transactions. Tests override this.
    Routes must depend on this with ``scope="function"`` so the commit runs before the response is
    sent — a failed commit must surface as a 500, never a silent 200 over pre-commit state."""
    dsn = get_settings().dsn
    if not dsn:
        raise HTTPException(status_code=503, detail="FEATUREGEN_DSN is not configured")
    conn = psycopg.connect(dsn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_identity(
    request: Request,
    conn: Annotated[psycopg.Connection, Depends(get_conn, scope="function")],
    x_user: str | None = Header(default=None),
    x_roles: str = Header(default=""),
) -> IdentityEnvelope:
    """Real auth: an ``Authorization: Bearer <token>`` session (local username/password login), resolved
    to a genuinely ``authenticated=True`` principal whose roles come from the user's GROUPS (never a
    request header/param). If no Bearer token is present, fall back to the header stub — but ONLY when
    ``FEATUREGEN_AUTH_STUB=1`` (dev/tests): the stub *asserts* an identity (``authenticated=False``), it
    does not prove one, and it is OFF in production so roles can't be self-granted via ``X-Roles``.

    OIDC drops in later as a second verifier with no route changes; this is the M6 seam."""
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        ident = resolve_session(conn, auth[7:].strip(), now=datetime.now(UTC))
        if ident is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
        return ident
    if _auth_stub_enabled() and x_user:
        roles = tuple(r.strip() for r in x_roles.split(",") if r.strip())
        try:
            return build_human_identity(subject=f"user:{x_user}", role_claims=roles,
                                        auth_method="stub")
        except IdentityError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
    raise HTTPException(status_code=401, detail="authentication required (Bearer session token)")


def get_llm(request: Request) -> LLMClient:
    """Fail closed: feature-assist requires a configured LLM client (env-gated ClaudeLLM in
    production, a scripted FakeLLM in tests). Absent client -> 503, never a silent fake (D5)."""
    client = get_llm_optional(request)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail="no LLM provider is configured on this deployment "
            "(set FEATUREGEN_LLM_PROVIDER=anthropic to enable feature-assist)",
        )
    return client


def get_llm_optional(request: Request) -> LLMClient | None:
    """The app's optional LLM client (ingest enrichment): None means 'run without enrichment' —
    unlike get_llm, absence is a supported mode here, not an error."""
    return getattr(request.app.state, "llm_client", None)
