from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from featuregen.api.app import create_app
from featuregen.api.deps import get_conn


@pytest.fixture(autouse=True)
def _auth_stub(monkeypatch):
    """API tests authenticate via the X-User/X-Roles stub; enable it (OFF by default in prod). Tests
    that exercise real Bearer login/logout override this back to '0' explicitly."""
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")


@pytest.fixture
def make_client(conn):
    """Build a TestClient whose requests run on the suite's rolled-back connection.

    The get_conn override yields the shared test conn WITHOUT commit/close — the root `conn`
    fixture rolls everything back on teardown, keeping API tests isolated like all others."""
    with ExitStack() as stack:

        def _make(llm_client=None):
            app = create_app(llm_client=llm_client)

            def _test_conn():
                yield conn

            app.dependency_overrides[get_conn] = _test_conn
            return stack.enter_context(TestClient(app))

        yield _make


@pytest.fixture
def client(make_client):
    return make_client()


@pytest.fixture
def admin_headers():
    """Stub identity carrying the raw `platform-admin` role CLAIM — the exact claim
    `require_confirmer` gates on (hyphen, not the functional `platform_admin` role)."""
    return {"X-User": "priya", "X-Roles": "platform-admin"}


@pytest.fixture
def non_admin_headers():
    """Stub identity WITHOUT the confirmer claim (functional read-only role only)."""
    return {"X-User": "v", "X-Roles": "catalog_viewer"}
