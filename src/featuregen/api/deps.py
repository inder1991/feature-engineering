"""Request-scoped dependencies: DB transaction, stub session auth, fail-closed LLM gate."""

from __future__ import annotations

from collections.abc import Iterator

import psycopg
from fastapi import Header, HTTPException, Request

from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient


def get_conn() -> Iterator[psycopg.Connection]:
    """One connection + transaction per request: commit on success, rollback on any error.
    Ingest stays all-or-nothing; reads never leave dangling transactions. Tests override this."""
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
    x_user: str | None = Header(default=None),
    x_roles: str = Header(default=""),
) -> IdentityEnvelope:
    """Stub session auth (spec build-step 1): subject + roles from headers until the real IdP
    lands. This dependency is the M6 seam — swap it for real session resolution without touching
    any endpoint. Roles must never be accepted from request params or bodies."""
    if not x_user:
        raise HTTPException(status_code=401, detail="missing X-User header (stub auth)")
    roles = tuple(r.strip() for r in x_roles.split(",") if r.strip())
    return IdentityEnvelope(
        subject=x_user,
        actor_kind="human",
        authenticated=True,
        auth_method="stub",
        role_claims=roles,
    )


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
