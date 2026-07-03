import pytest
from tests.featuregen._helpers import install_fake_identity_verifier

from featuregen.aggregates.bootstrap import register_phase06_event_schemas


@pytest.fixture(autouse=True)
def _register_fake_identity_verifier():
    """Ensure a permissive identity verifier is registered for every test (SP-0.5 BLOCKER #1).

    ``build_*_identity`` is fail-closed in production, so tests obtain authenticated principals
    only through a verifier. Importing ``_helpers`` registers one at import time (for module-level
    identity constants); this autouse fixture re-registers it per test so a test that clears or
    swaps the verifier cannot leak that state into the next one."""
    install_fake_identity_verifier()


@pytest.fixture(scope="session", autouse=True)
def _register_phase06_event_types():
    # Exercise the SAME production bootstrap path the running process uses (Task 3), so the
    # test suite and runtime register schemas identically. Idempotent, so safe at session scope.
    register_phase06_event_schemas()


@pytest.fixture
def db(conn):
    """Alias the repo-root `conn` fixture (a real PG connection whose writes are rolled back on
    teardown) under the name the Phase-06 briefs use. Shared across all Phase-06 task suites so
    each task reuses one harness; the canonical migrations are applied once per session via the
    `_dsn` fixture in the root conftest."""
    return conn
