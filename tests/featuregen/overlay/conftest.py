import pytest

from featuregen.events.registry import event_registry
from featuregen.overlay.facts import register_overlay_event_types


@pytest.fixture(autouse=True)
def _register_overlay_event_types():
    """Register the 6 OVERLAY_FACT_* event schemas in the process-global event registry for each
    overlay test. Function-scoped so it re-registers after the root autouse `_reset_registry`."""
    register_overlay_event_types(event_registry())
    yield
