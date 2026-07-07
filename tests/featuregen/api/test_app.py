from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from featuregen.api.deps import get_conn, get_identity


def _req(auth: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(headers=({"authorization": auth} if auth else {}))


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_identity_requires_user_header():
    with pytest.raises(HTTPException) as exc:
        get_identity(_req(), None, x_user=None, x_roles="")
    assert exc.value.status_code == 401


def test_identity_parses_subject_and_roles_unauthenticated_stub():
    # The stub asserts identity, it does not prove it (SP-0.5 trust boundary): the envelope is
    # authenticated=False until a real verifier proves a token.
    ident = get_identity(_req(), None, x_user="ana", x_roles="pii_reader, data_owner")
    assert ident.subject == "user:ana"
    assert ident.role_claims == ("pii_reader", "data_owner")
    assert ident.authenticated is False
    assert ident.auth_method == "stub"


def test_get_conn_fails_closed_without_dsn(monkeypatch):
    monkeypatch.delenv("FEATUREGEN_DSN", raising=False)
    with pytest.raises(HTTPException) as exc:
        next(get_conn())
    assert exc.value.status_code == 503


def test_get_conn_commits_and_closes(monkeypatch, _dsn):
    monkeypatch.setenv("FEATUREGEN_DSN", _dsn)
    gen = get_conn()
    conn = next(gen)
    assert conn.execute("SELECT 1").fetchone() == (1,)
    assert next(gen, None) is None  # drives the commit + close path
    assert conn.closed


def test_create_app_from_env_wires_claude_only_when_enabled(monkeypatch):
    from featuregen.api.app import create_app_from_env
    from featuregen.intake.llm_claude import ClaudeLLM

    monkeypatch.delenv("FEATUREGEN_LLM_PROVIDER", raising=False)
    assert create_app_from_env().state.llm_client is None
    monkeypatch.setenv("FEATUREGEN_LLM_PROVIDER", "anthropic")
    assert isinstance(create_app_from_env().state.llm_client, ClaudeLLM)


def test_health_reports_degraded_when_schema_is_behind():
    from starlette.testclient import TestClient

    from featuregen.api.app import create_app
    app = create_app()
    with TestClient(app) as c:                       # lifespan runs (sets schema_pending)
        app.state.schema_pending = ["9999_future"]   # simulate drift after startup
        body = c.get("/health").json()
    assert body["status"] == "degraded"
    assert body["pending_migrations"] == 1


def test_metrics_reports_operational_snapshot(client):
    from tests.featuregen.api._helpers import AUTH
    body = client.get("/metrics", headers=AUTH).json()
    assert set(body) >= {"counters", "projection_lag", "degraded_markers",
                         "skipped_events", "pending_migrations"}
    assert "overlay" in body["projection_lag"]
