import pytest

from sp0.aggregates.bootstrap import register_phase06_event_schemas


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
