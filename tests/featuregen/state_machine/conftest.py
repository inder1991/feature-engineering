from __future__ import annotations

import pytest

from featuregen.events.registry import event_registry
from featuregen.state_machine.event_types import register_state_machine_event_types


@pytest.fixture(autouse=True)
def _register_state_machine_event_types(_reset_registry) -> None:
    """Register Phase-03 event types + a seed type into the shared event registry
    so append_event can validate them (Task 7).

    Function-scoped and autouse. It depends on Phase 01's `_reset_registry`
    fixture (defined in the repo-root tests/conftest.py, also function-scoped
    autouse) PURELY to force ordering: pytest sets up `_reset_registry` first
    (running its pre-yield reset_event_registry(), which replaces the singleton
    with a fresh, EMPTY EventSchemaRegistry), and only then runs this fixture,
    which repopulates that fresh singleton. A session-scoped registration here
    would be wiped by `_reset_registry` before every schema-dependent test and
    the SM schemas would be missing. `event_registry()` is the accessor function
    (Phase 01) and must be CALLED to get the live singleton."""
    registry = event_registry()
    register_state_machine_event_types(registry)
    registry.register_schema(
        "SM_TEST_SEED", 1, {"type": "object"}, owner="featuregen-state-machine-test"
    )
