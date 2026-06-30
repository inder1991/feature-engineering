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


@pytest.fixture
def occ_spy(monkeypatch):
    """Record (type -> expected_version) for every overlay command append (C2/I4 OCC pinning).

    Patches the module-global `append_overlay_event` the command handlers call, so the spy sees
    exactly the `expected_version` each handler passes (None means the append was NOT version-pinned
    and `append()` would silently recompute the live head — the lost-update defect). The returned
    dict maps each event type to the LAST `expected_version` observed for it."""
    import featuregen.overlay.commands as m

    real = m.append_overlay_event
    seen: dict[str, int | None] = {}

    def spy(conn, **kw):
        seen[kw.get("type")] = kw.get("expected_version")
        return real(conn, **kw)

    monkeypatch.setattr(m, "append_overlay_event", spy)
    return seen


@pytest.fixture
def inject_concurrent_append(monkeypatch):
    """Return an installer(target_type) that simulates a concurrent writer (C2/I4).

    Just before a command handler lands its event of `target_type`, a concurrent writer lands an
    event of the SAME type at the LIVE head (`expected_version=None`). A correctly version-pinned
    handler append then collides with `ConcurrencyError`; the buggy unpinned append would silently
    land one stream_version higher (the lost-update / double-confirm / stranded-join defect)."""
    import featuregen.overlay.commands as m

    real = m.append_overlay_event

    def install(target_type: str):
        state = {"fired": False}

        def patched(conn, **kw):
            if kw.get("type") == target_type and not state["fired"]:
                state["fired"] = True
                real(
                    conn,
                    fact_key=kw["fact_key"],
                    type=kw["type"],
                    payload=kw["payload"],
                    actor=kw["actor"],
                )  # out-of-band concurrent winner at the live head
            return real(conn, **kw)

        monkeypatch.setattr(m, "append_overlay_event", patched)

    return install
