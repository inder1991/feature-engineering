import pytest

from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter, register_catalog_adapter
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.facts import register_overlay_event_types
from featuregen.overlay.identity import display_object_ref


class StubCatalog:
    """In-memory CatalogAdapter test double (stands in for Phase 3's FixtureCatalog so Phase 4
    is independent of its constructor). Owners are keyed on the display object_ref string."""

    def __init__(self) -> None:
        self.owners: dict[str, str] = {}

    def set_owner(self, ref, subject: str) -> None:
        self.owners[display_object_ref(ref)] = subject

    def owner_of(self, ref):
        return self.owners.get(display_object_ref(ref))

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}


@pytest.fixture(autouse=True)
def _register_overlay_commands():
    # Re-register the overlay command catalog before EVERY overlay test (function-scoped). Other test
    # modules call clear_registry(), so a session-scoped single registration could be wiped mid-session
    # under randomized ordering (finding 7). register_overlay_commands() is idempotent (skips already-
    # registered actions), so per-test registration is cheap and order-robust.
    register_overlay_commands()


@pytest.fixture(autouse=True)
def _register_overlay_event_types():
    # The EVENT registry IS reset per test by the root harness, so re-register the overlay event
    # schemas for every overlay test (so `append_event` validation passes).
    register_overlay_event_types(event_registry())


@pytest.fixture
def catalog():
    cat = StubCatalog()
    register_catalog_adapter(cat)  # single-source accessor from overlay/catalog.py
    try:
        yield cat
    finally:
        # The adapter is a process-wide module global (Task 3.3); clear it so it never leaks into a
        # later test that expects current_catalog_adapter() to fail closed (no adapter registered).
        _clear_catalog_adapter()
