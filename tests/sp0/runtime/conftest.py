from __future__ import annotations

import pytest

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.events import append_event
from sp0.events.registry import event_registry

_PERMISSIVE = {"type": "object"}


@pytest.fixture
def db(conn):
    """Alias the repo-root `conn` fixture (a real PG connection whose writes are
    rolled back on teardown) under the name the Phase-04 runtime briefs use."""
    return conn


@pytest.fixture(autouse=True)
def _register_runtime_test_event_types(_reset_registry):
    """Register the runtime-test event types into the shared event registry so
    append_event can validate them.

    Function-scoped and autouse, depending on Phase 01's function-scoped autouse
    `_reset_registry` (repo-root tests/conftest.py) PURELY to force ordering:
    pytest sets up `_reset_registry` first (replacing the singleton with a fresh,
    EMPTY registry), then this fixture repopulates it. A session-scoped
    registration would be wiped before every schema-dependent test. We register
    each type once per test; we re-confirm via validate() so a REAL integration
    break (wrong name/signature) is surfaced rather than swallowed.
    """
    registry = event_registry()
    for type_name in ("STEP_TRIGGER", "STEP_DONE", "STEP_NEXT"):
        try:
            registry.register_schema(
                type_name, 1, _PERMISSIVE, owner="sp0-runtime-tests"
            )
        except Exception:  # noqa: BLE001 — only an idempotent re-registration is acceptable
            registry.validate(type_name, 1, {})  # re-raises if NOT actually registered


@pytest.fixture
def actor() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="service:test-worker",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("worker",),
    )


@pytest.fixture
def prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=1,
        producing_component="sp0-runtime-tests@0.0.0",
    )


@pytest.fixture
def seed_run_event(db, actor, prov):
    """Append one run-stream event and return its EventEnvelope."""

    def _seed(
        run_id: str,
        *,
        type: str = "STEP_TRIGGER",
        expected_version: int = 0,
        table_version: int = 1,
        payload: dict | None = None,
    ):
        new_event = NewEvent(
            aggregate="run",
            aggregate_id=run_id,
            run_id=run_id,
            type=type,
            schema_version=1,
            payload=payload or {},
            actor=actor,
            provenance=prov,
        )
        return append_event(
            db, new_event, expected_version=expected_version, table_version=table_version
        )

    return _seed
