from __future__ import annotations

from decimal import Decimal

import pytest

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events import append_event
from featuregen.events.registry import event_registry

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
            registry.register_schema(type_name, 1, _PERMISSIVE, owner="featuregen-runtime-tests")
        except Exception:  # noqa: BLE001 — only an idempotent re-registration is acceptable
            registry.validate(type_name, 1, {})  # re-raises if NOT actually registered


class _RecordingDeleter:
    def __init__(self):
        self.deleted = []

    def delete(self, object_key):
        self.deleted.append(object_key)


class _RecordingAudit:
    def __init__(self):
        self.reports = []

    def record(self, report):
        self.reports.append(report)


@pytest.fixture
def recording_deleter():
    return _RecordingDeleter()


@pytest.fixture
def recording_audit():
    return _RecordingAudit()


class _RecordingCaller:
    integration = "llm"

    def __init__(self, *, invoke_result=None, reconcile_result=None):
        self._invoke_result = invoke_result
        self._reconcile_result = reconcile_result
        self.invoke_calls = 0
        self.reconcile_calls = 0

    def invoke(self, request_payload):
        self.invoke_calls += 1
        return self._invoke_result

    def reconcile(self, job_handle):
        self.reconcile_calls += 1
        return self._reconcile_result


@pytest.fixture
def recording_caller():
    return _RecordingCaller


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
        producing_component="featuregen-runtime-tests@0.0.0",
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


@pytest.fixture
def insert_stub_event():
    """Insert a minimal, schema-valid row into the Phase 01 `events` table (run stream)."""

    def _insert(conn, *, event_id: str, run_id: str, type: str, stream_version: int) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                                    type, schema_version, table_version, actor, payload,
                                    provenance, occurred_at)
                VALUES (%s, 'run', %s, %s, %s, %s, 1, 1,
                        '{}'::jsonb, '{}'::jsonb, '{}'::jsonb, now())
                """,
                (event_id, run_id, stream_version, run_id, type),
            )

    return _insert


@pytest.fixture
def insert_run_state():
    """Insert a row into the Phase 01 `run_workflow_state` projection."""

    def _insert(
        conn,
        *,
        run_id: str,
        request_id: str,
        cost: Decimal = Decimal("0"),
        candidates: int = 0,
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version,
                                                cost_units, candidates_explored)
                VALUES (%s, %s, 'AWAITING', 1, %s, %s)
                """,
                (run_id, request_id, cost, candidates),
            )

    return _insert


@pytest.fixture
def insert_stub_document():
    """Insert a minimal committed row into the Phase 02 `documents` table referencing a blob."""

    def _insert(
        conn,
        *,
        doc_id: str,
        body_ref: str | None,
        classification: str = "governance-retained",
    ) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO documents (doc_id, stage, schema_version, branch_role, body_ref,
                                       content_hash, body_classification, actor, provenance)
                VALUES (%s, 'FEATURE_PLAN', 1, 'candidate', %s,
                        'sha256:stub', %s, '{}'::jsonb, '{}'::jsonb)
                """,
                (doc_id, body_ref, classification),
            )

    return _insert
