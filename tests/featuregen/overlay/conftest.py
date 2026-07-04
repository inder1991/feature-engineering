import pytest
from tests.featuregen.overlay._helpers import StubCatalog

from featuregen.events.registry import event_registry
from featuregen.overlay.catalog import _clear_catalog_adapter, register_catalog_adapter
from featuregen.overlay.commands import register_overlay_commands
from featuregen.overlay.facts import register_overlay_event_types

__all__ = ["StubCatalog"]


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


@pytest.fixture(autouse=True)
def _reset_overlay_config():
    # OverlayConfig is a process-wide module global (SP-1.5 §3.1); reset it around each test so a
    # config registered in one test never leaks into another that expects fail-closed.
    from featuregen.overlay.config import _clear_overlay_config

    _clear_overlay_config()
    yield
    _clear_overlay_config()


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


def _append_seam_modules():
    """The overlay handler modules that each import `append_overlay_event` from the store. The append
    seam now lives on the OWNING module (CQ9): propose_fact -> proposal_commands, confirm/reject/enter
    -> confirmation_commands, approved_join -> join_confirmation. Patching the name on every one of
    them intercepts an append regardless of which command a test drives."""
    from featuregen.overlay import (
        confirmation_commands,
        join_confirmation,
        proposal_commands,
    )

    return (proposal_commands, confirmation_commands, join_confirmation)


@pytest.fixture
def occ_spy(monkeypatch):
    """Record (type -> expected_version) for every overlay command append (C2/I4 OCC pinning).

    Patches each handler module's `append_overlay_event`, so the spy sees exactly the
    `expected_version` each handler passes (None means the append was NOT version-pinned and
    `append()` would silently recompute the live head — the lost-update defect). The returned dict
    maps each event type to the LAST `expected_version` observed for it."""
    modules = _append_seam_modules()
    real = modules[0].append_overlay_event  # the same store function in every module
    seen: dict[str, int | None] = {}

    def spy(conn, **kw):
        seen[kw.get("type")] = kw.get("expected_version")
        return real(conn, **kw)

    for m in modules:
        monkeypatch.setattr(m, "append_overlay_event", spy)
    return seen


@pytest.fixture
def inject_concurrent_append(monkeypatch):
    """Return an installer(target_type) that simulates a concurrent writer (C2/I4).

    Just before a command handler lands its event of `target_type`, a concurrent writer lands an
    event of the SAME type at the LIVE head (`expected_version=None`). A correctly version-pinned
    handler append then collides with `ConcurrencyError`; the buggy unpinned append would silently
    land one stream_version higher (the lost-update / double-confirm / stranded-join defect)."""
    modules = _append_seam_modules()
    real = modules[0].append_overlay_event  # the same store function in every module

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

        for m in modules:
            monkeypatch.setattr(m, "append_overlay_event", patched)

    return install
