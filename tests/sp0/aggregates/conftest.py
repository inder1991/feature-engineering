from __future__ import annotations

import pytest

from sp0.aggregates.bootstrap import register_phase06_event_schemas


@pytest.fixture(autouse=True)
def _register_phase06_event_types(_reset_registry) -> None:
    """Register Phase-06 event schemas into the live event-registry singleton so
    `append_event` validation passes. Function-scoped and autouse, depending on Phase
    01's `_reset_registry` (repo-root, function-scoped autouse) PURELY to force ordering:
    pytest runs `_reset_registry` first (its pre-yield reset swaps in a fresh, EMPTY
    singleton), then this fixture repopulates that fresh singleton via the SAME production
    bootstrap path the runtime uses (Task 3). A session-scoped registration would be wiped
    by `_reset_registry` before every schema-dependent test."""
    register_phase06_event_schemas()


@pytest.fixture
def db(conn):
    """Alias the repo-root `conn` fixture (a real PG connection whose writes are
    rolled back on teardown) under the name the Phase-06 aggregates briefs use.
    The shared migration harness applies the canonical migrations once per session
    via the `_dsn` fixture in the root conftest."""
    return conn
