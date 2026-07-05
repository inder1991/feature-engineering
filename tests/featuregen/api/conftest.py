from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from featuregen.api.app import create_app
from featuregen.api.deps import get_conn


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
