import pytest
from fastapi import HTTPException

from featuregen.api.deps import get_conn, get_identity


def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_identity_requires_user_header():
    with pytest.raises(HTTPException) as exc:
        get_identity(x_user=None, x_roles="")
    assert exc.value.status_code == 401


def test_identity_parses_subject_and_roles():
    ident = get_identity(x_user="ana", x_roles="pii_reader, data_owner")
    assert ident.subject == "ana"
    assert ident.role_claims == ("pii_reader", "data_owner")
    assert ident.authenticated is True
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
