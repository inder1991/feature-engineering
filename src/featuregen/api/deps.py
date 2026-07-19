"""Request-scoped dependencies: DB transaction, stub session auth, fail-closed LLM gate."""

from __future__ import annotations

import logging
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
from featuregen.security.audit import record_security_event

logger = logging.getLogger(__name__)


def _write_denied_audit(conn, identity: IdentityEnvelope, action: str) -> None:
    """Write one ACCESS_DENIED row to the tamper-evident security_audit chain (separated out so it can
    be unit-tested against a normal connection)."""
    record_security_event(conn, event_type="ACCESS_DENIED", actor=identity,
                          attempted_action=action, decision="denied", reason=action)


def audit_access_denied(identity: IdentityEnvelope, action: str) -> None:
    """Record a denied access attempt on the tamper-evident chain, so blocked/probing attempts are
    EVIDENCE, not a silent 403 (examiner proof + insider-threat + forensics). Written on a SEPARATE
    connection because the 403 rolls the request transaction back — an audit on it would be lost. Only
    in real-auth mode (stub OFF = production): with the dev stub on it is skipped, both because it is a
    production control and because a separate committing connection would pollute the rolled-back test
    DB. Best-effort — an audit failure never turns a correct 403 into a 500."""
    if _auth_stub_enabled():
        return
    dsn = get_settings().dsn
    if not dsn:
        return
    try:
        with psycopg.connect(dsn) as conn:   # its own tx, committed on exit — survives the 403 rollback
            _write_denied_audit(conn, identity, action)
    except Exception:  # noqa: BLE001 — never let an audit failure mask the (correct) denial
        logger.warning("failed to record ACCESS_DENIED for %s", action, exc_info=True)


def require_permission(permission: str):
    """A route dependency that 403s unless the caller's roles grant `permission`. Roles come from the
    real Bearer session (prod) or the header stub (dev, stub-enabled) — the same source as read-scope,
    so this is stub-compatible; production self-granting is blocked by the stub being OFF, and iam:manage
    additionally requires an authenticated principal (see require_admin)."""

    def _dep(request: Request,
             identity: Annotated[IdentityEnvelope, Depends(get_identity)]) -> IdentityEnvelope:
        if not has_permission(identity.role_claims, permission):
            audit_access_denied(identity, f"{permission} on {request.method} {request.url.path}")
            raise HTTPException(status_code=403, detail=f"missing permission: {permission}")
        return identity

    return _dep


# Prebuilt route-level guards (used as `dependencies=[Depends(...)]` — the route still injects the
# identity separately for read-scope, and FastAPI caches get_identity so it resolves once per request).
require_catalog_read = require_permission(CATALOG_READ)
require_catalog_write = require_permission(CATALOG_WRITE)
require_feature_read = require_permission(FEATURE_READ)
require_feature_generate = require_permission(FEATURE_GENERATE)


def require_confirmer(
    request: Request,
    identity: Annotated[IdentityEnvelope, Depends(get_identity)],
) -> IdentityEnvelope:
    """Governance confirmer gate: the caller must carry the raw `platform-admin` role CLAIM — the exact
    claim the overlay's dual-owner confirm authorizes on (join_confirmation.py:68). Deliberately NOT the
    `platform_admin` permission bundle, to avoid a route-passes-but-overlay-denies mismatch."""
    if "platform-admin" not in identity.role_claims:
        audit_access_denied(identity, f"platform-admin claim on {request.method} {request.url.path}")
        raise HTTPException(status_code=403, detail="requires the platform-admin role")
    return identity


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


def get_feature_gen_conn() -> Iterator[psycopg.Connection]:
    """Like :func:`get_conn` (one connection + transaction per request, commit on success / rollback on
    error / close in finally, 503 when no DSN) but the feature-generation workflow reads committed catalog
    state under ``REPEATABLE READ`` so the C0 metadata snapshot is torn-free — every read in the request
    transaction sees ONE consistent catalog view.

    The fresh connection is pinned to ``REPEATABLE READ`` BEFORE any query is issued, so the isolation
    boundary is established before the handler's first SQL (in psycopg3 the level applies at the next
    transaction start, which is that first statement). A read that somehow precedes the boundary, or a
    mid-transaction isolation change, must surface as a SERVER error (psycopg raises), never silently
    degrade to ``READ COMMITTED``. Routes depend on this with ``scope="function"`` so the commit runs
    before the response is sent. Tests override this."""
    dsn = get_settings().dsn
    if not dsn:
        raise HTTPException(status_code=503, detail="FEATUREGEN_DSN is not configured")
    conn = psycopg.connect(dsn)
    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ  # applies at the next tx start
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
