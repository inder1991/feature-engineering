from __future__ import annotations

import pytest


@pytest.fixture
def db(conn):
    """Alias the repo-root `conn` fixture (a real PG connection whose writes are
    rolled back on teardown) under the name the Phase-06 aggregates briefs use.
    The shared migration harness applies the canonical migrations once per session
    via the `_dsn` fixture in the root conftest."""
    return conn
