"""require_confirmer gates on the raw `platform-admin` role CLAIM — the exact claim the overlay's
dual-owner confirm authorizes on (overlay/join_confirmation.py) — NOT the permission bundle."""
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from featuregen.api.deps import get_conn, require_confirmer


def _app():
    app = FastAPI()

    @app.get("/x", dependencies=[Depends(require_confirmer)])
    def _x():
        return {"ok": True}

    # The stub-auth path in get_identity never touches the connection, but FastAPI still resolves
    # the get_conn dependency (503 without a DSN) — stub it out so this test needs no database.
    def _no_conn():
        yield None

    app.dependency_overrides[get_conn] = _no_conn
    return app


def test_confirmer_allows_platform_admin(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    c = TestClient(_app())
    r = c.get("/x", headers={"X-User": "priya", "X-Roles": "platform-admin"})
    assert r.status_code == 200


def test_confirmer_denies_non_admin(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")
    c = TestClient(_app())
    r = c.get("/x", headers={"X-User": "joe", "X-Roles": "catalog_viewer"})
    assert r.status_code == 403
