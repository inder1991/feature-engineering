"""E1 — the gold corpus runs the REAL serving path: re-expose the API harness fixtures.

The corpus lives under tests/eval (the eval marker gates it), OUTSIDE tests/featuregen, so
the API package's autouse fixtures do not apply — re-declare the two that matter: the fake
identity verifier (fail-closed identity needs a registered verifier) and the X-User auth
stub the API tests authenticate through.
"""
import pytest
from tests.featuregen._helpers import install_fake_identity_verifier
from tests.featuregen.api.conftest import make_client  # noqa: F401 — fixture


@pytest.fixture(autouse=True)
def _register_fake_identity_verifier():
    install_fake_identity_verifier()


@pytest.fixture(autouse=True)
def _auth_stub(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_AUTH_STUB", "1")


@pytest.fixture
def db(conn):
    return conn
