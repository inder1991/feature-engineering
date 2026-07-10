import pytest

from featuregen.events.registry import event_registry
from featuregen.overlay.facts import register_overlay_event_types


@pytest.fixture(autouse=True)
def _register_overlay_event_types():
    # The EVENT registry is reset per test by the root harness; the preview/ingest tests that call
    # ingest_upload directly need the overlay fact schemas registered (same as overlay/conftest.py).
    register_overlay_event_types(event_registry())
