## Phase 01: Event store, schema registry & projections

**Goal:** Build the source-of-truth event store (monotonic `global_seq`, per-aggregate optimistic concurrency), the versioned event-schema registry (total/chained upcasters, backward-compat rule, deprecate/withdraw lifecycle, pinned snapshots), and the projection runner (checkpoint/lag/as-of, fail-closed degraded handling, deterministic rebuild, parallel migration with atomic read-switch) — the foundation every later SP-0 phase appends events into and projects from.

> This phase physically lays down `src/featuregen/contracts/` (the authoritative shared symbol module imported by every phase). Phase 01 is **semantically authoritative** only for the symbols in its "Produces" lists below; the other dataclasses/Protocols are copied **verbatim** from the shared contract as pure declarations so the package imports — later phases implement behavior against them without redefining them.

> **Phase 01 design refinement (degraded handling).** The shared `run_projection` docstring says it marks the affected aggregate degraded "(e.g. `run_workflow_state.degraded=true`)". `run_workflow_state` is owned by a later phase and its `current_state`/`table_version` `NOT NULL` columns require §4 concepts Phase 01 does not have. Phase 01 therefore implements `run_projection` **generically over the `Projection` Protocol** while still honoring the contract's "mark the affected aggregate degraded and stop advancing it":
>
> 1. `run_projection` wraps **every** `projection.apply(event)` in a **SAVEPOINT**. A fail-closed projection's `apply`, on an unappliable event, raises `ProjectionApplyError` carrying `aggregate`/`aggregate_id`/`reason`; any writes its body made before raising (including any table-specific marker of its own) are **discarded** by the runner's `ROLLBACK TO SAVEPOINT`, so **no partial projection state survives** the poison event.
> 2. After rolling back, `run_projection` **records the affected aggregate in a Phase-01-owned generic `projection_degraded` ledger in a separate statement** — using the carried `ProjectionApplyError.aggregate`/`aggregate_id`/`reason` plus the poison event id — so the **only** surviving degraded marker is the one the runner itself writes (the carried payload is load-bearing), independent of which table the projection owns.
> 3. `run_projection` then **halts without advancing past the poison event** (lag grows; downstream command-blocking is enforced by readers of `projection_degraded`).
>
> Analytics projections log-and-skip (their poison `apply` is likewise SAVEPOINT-wrapped, so its partial writes are discarded before the runner continues). This keeps `run_projection` table-agnostic while realizing the §3.6 contract and the shared `run_projection` docstring exactly.

---

## File structure

```
pyproject.toml                              # package metadata, deps, pytest pythonpath=src
src/featuregen/
  __init__.py
  contracts/
    __init__.py                             # re-exports every shared symbol (incl. lazy re-export
                                            #   of append_event/load_stream/run_projection/
                                            #   rebuild_projection/projection_lag via __getattr__)
    errors.py                               # ConcurrencyError, ProjectionApplyError, SchemaValidationError
    db.py                                   # DbConn type alias (psycopg connection/tx handle)
    envelopes.py                            # IdentityEnvelope, ProvenanceEnvelope, EventEnvelope, NewEvent,
                                            #   NewDocument, NewExternalCommand, NewTimer, Disposition,
                                            #   HandlerResult, HandlerContext, Command, CommandResult,
                                            #   GuardOutcome, GateTaskSpec, SignalResult
    protocols.py                            # GuardInputs, Upcaster, Projection, Handler, GuardPredicate,
                                            #   PredicateRegistry, SchemaRegistry
  db/
    __init__.py
    migrations.py                           # MIGRATIONS list + apply_migrations(conn) — Phase 01 DDL
                                            #   (events, registry tables, projection_checkpoints,
                                            #    projection_active_alias, projection_degraded)
  events/
    __init__.py
    serde.py                                # identity/provenance/envelope <-> jsonb
    registry.py                             # EventSchemaRegistry, event_registry(), reset_event_registry(),
                                            #   is_backward_compatible(), assert_evolution_complete(),
                                            #   persist_event_schemas(), persist_registry_snapshot(),
                                            #   load_registry_snapshot(), hydrate_event_registry()
    store.py                                # append_event(), load_stream()
  projections/
    __init__.py
    runner.py                               # run_projection(), rebuild_projection(), projection_lag(),
                                            #   read_as_of(), _ensure_checkpoint(), _head_seq()
    migration.py                            # resolve_projection(), set_alias(), migrate_projection()
tests/
  conftest.py                              # conn fixture (real PG15+), migrations, registry reset
  contracts/test_contract_symbols.py
  contracts/test_core_interface_reexports.py
  db/test_migrations.py
  events/test_serde.py
  events/test_registry_validate.py
  events/test_registry_upcast.py
  events/test_registry_backward_compat.py
  events/test_registry_lifecycle.py
  events/test_registry_snapshot.py
  events/test_registry_hydrate.py
  events/test_append_event.py
  events/test_optimistic_concurrency.py
  events/test_load_stream.py
  projections/test_run_projection.py
  projections/test_fail_closed.py
  projections/test_rebuild.py
  projections/test_migration.py
```

---

### Task 1: Project scaffold + shared contract module

**Files:**
- Create: `pyproject.toml`
- Create: `src/featuregen/__init__.py`
- Create: `src/featuregen/contracts/errors.py`
- Create: `src/featuregen/contracts/db.py`
- Create: `src/featuregen/contracts/envelopes.py`
- Create: `src/featuregen/contracts/protocols.py`
- Create: `src/featuregen/contracts/__init__.py`
- Test: `tests/contracts/test_contract_symbols.py`

**Interfaces:**
- Consumes: nothing (first task).
- Produces: `IdentityEnvelope`, `ProvenanceEnvelope`, `EventEnvelope`, `NewEvent`, `ConcurrencyError`, `ProjectionApplyError`, `SchemaValidationError`, `Disposition`, `Projection`, `SchemaRegistry`, `Upcaster`, `GuardInputs`, `DbConn` (verbatim shared dataclasses / Protocols / exceptions / aliases), plus the unchanged downstream-phase declarations (`NewDocument`, `NewExternalCommand`, `NewTimer`, `HandlerResult`, `HandlerContext`, `Command`, `CommandResult`, `GuardOutcome`, `GateTaskSpec`, `SignalResult`, `Handler`, `GuardPredicate`, `PredicateRegistry`).

- [ ] **Step 1: Write the failing test**

```python
# tests/contracts/test_contract_symbols.py
from __future__ import annotations

from datetime import datetime, timezone

from featuregen.contracts import (
    Command,
    CommandResult,
    ConcurrencyError,
    Disposition,
    EventEnvelope,
    GateTaskSpec,
    GuardOutcome,
    Handler,
    HandlerContext,
    HandlerResult,
    IdentityEnvelope,
    NewDocument,
    NewEvent,
    NewExternalCommand,
    NewTimer,
    PredicateRegistry,
    Projection,
    ProjectionApplyError,
    ProvenanceEnvelope,
    SchemaRegistry,
    SchemaValidationError,
    SignalResult,
)


def _identity() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("data_scientist",),
    )


def _provenance() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=1,
        producing_component="sp0-test@0.1.0",
    )


def test_identity_envelope_is_frozen():
    idv = _identity()
    assert idv.role_claims == ("data_scientist",)
    import dataclasses

    with __import__("pytest").raises(dataclasses.FrozenInstanceError):
        idv.subject = "user:eve"  # type: ignore[misc]


def test_event_envelope_round_constructs():
    now = datetime.now(timezone.utc)
    env = EventEnvelope(
        event_id="evt_1",
        global_seq=1,
        aggregate="run",
        aggregate_id="run_1",
        stream_version=1,
        type="RUN_STARTED",
        schema_version=1,
        table_version=1,
        actor=_identity(),
        payload={"k": "v"},
        provenance=_provenance(),
        occurred_at=now,
        recorded_at=now,
        run_id="run_1",
    )
    assert env.run_id == "run_1"
    assert env.feature_id is None


def test_new_event_defaults():
    ne = NewEvent(
        aggregate="run",
        aggregate_id="run_1",
        type="RUN_STARTED",
        schema_version=1,
        payload={},
        actor=_identity(),
        provenance=_provenance(),
        run_id="run_1",
    )
    assert ne.occurred_at is None
    assert ne.caused_by is None


def test_disposition_values():
    assert Disposition.OK == "ok"
    assert Disposition.RETRYABLE == "retryable"
    assert Disposition.PERMANENT == "permanent"


def test_projection_apply_error_carries_aggregate():
    err = ProjectionApplyError("run", "run_9", "bad event")
    assert err.aggregate == "run"
    assert err.aggregate_id == "run_9"
    assert err.reason == "bad event"


def test_protocols_and_exceptions_importable():
    assert issubclass(ConcurrencyError, Exception)
    assert issubclass(SchemaValidationError, Exception)
    # Protocols import without error and are usable as types.
    for proto in (Projection, Handler, SchemaRegistry, PredicateRegistry):
        assert proto is not None
    # Downstream-phase declarations import without error.
    for sym in (
        NewDocument,
        NewExternalCommand,
        NewTimer,
        HandlerResult,
        HandlerContext,
        Command,
        CommandResult,
        GuardOutcome,
        GateTaskSpec,
        SignalResult,
    ):
        assert sym is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/contracts/test_contract_symbols.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'sp0'`.

- [ ] **Step 3: Write minimal implementation**

```toml
# pyproject.toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "sp0"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "psycopg[binary]>=3.1",
    "jsonschema>=4.21",
    "python-ulid>=2.2",
]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

```python
# src/featuregen/__init__.py
"""SP-0 Foundations."""
```

```python
# src/featuregen/contracts/errors.py
from __future__ import annotations


class ConcurrencyError(Exception):
    """Raised when expected_version != the stream's current stream_version (OCC)."""


class ProjectionApplyError(Exception):
    """Raised by a fail-closed projection that cannot apply an event; carries the
    affected aggregate so the runner can mark it `degraded` and block its commands."""

    def __init__(self, aggregate: str, aggregate_id: str, reason: str) -> None:
        self.aggregate, self.aggregate_id, self.reason = aggregate, aggregate_id, reason
        super().__init__(f"{aggregate}:{aggregate_id}: {reason}")


class SchemaValidationError(Exception):
    """Raised by SchemaRegistry.validate on a schema mismatch."""
```

```python
# src/featuregen/contracts/db.py
from __future__ import annotations

from typing import Any

import psycopg

# The active psycopg connection/transaction handle. Every function that mutates
# participates in the caller's open transaction (the §5.1 atomic boundary).
DbConn = psycopg.Connection[Any]
```

```python
# src/featuregen/contracts/envelopes.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional

from featuregen.contracts.db import DbConn


@dataclass(frozen=True, slots=True)
class IdentityEnvelope:
    """Identity-at-time-of-action for humans and services (§6.1)."""

    subject: str
    actor_kind: str
    authenticated: bool
    auth_method: str
    role_claims: tuple[str, ...]
    groups: tuple[str, ...] = ()
    tenant: Optional[str] = None
    on_behalf_of: Optional[str] = None
    impersonation: Optional[str] = None
    break_glass: bool = False
    source_of_authority: Optional[str] = None
    attestation: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ProvenanceEnvelope:
    """Reproducibility envelope on every event/document (§8)."""

    artifact_type: str
    schema_version: int
    producing_component: str
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: Optional[str] = None
    source_snapshots: tuple[str, ...] = ()
    event_registry_snapshot: Optional[str] = None
    doc_registry_snapshot: Optional[str] = None
    evaluation_dataset_ref: Optional[str] = None
    holdout_partition_spec: Optional[str] = None
    random_seed: Optional[int] = None
    candidates_explored_count: Optional[int] = None
    external_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """A persisted domain event (§3.2). `actor` is the identity field everywhere."""

    event_id: str
    global_seq: int
    aggregate: str
    aggregate_id: str
    stream_version: int
    type: str
    schema_version: int
    table_version: int
    actor: IdentityEnvelope
    payload: Mapping[str, Any]
    provenance: ProvenanceEnvelope
    occurred_at: datetime
    recorded_at: datetime
    request_id: Optional[str] = None
    feature_id: Optional[str] = None
    run_id: Optional[str] = None
    caused_by: Optional[str] = None


@dataclass(frozen=True, slots=True)
class NewEvent:
    """A to-be-appended event; global_seq/event_id/stream_version are allocated on append."""

    aggregate: str
    aggregate_id: str
    type: str
    schema_version: int
    payload: Mapping[str, Any]
    actor: IdentityEnvelope
    provenance: ProvenanceEnvelope
    request_id: Optional[str] = None
    feature_id: Optional[str] = None
    run_id: Optional[str] = None
    caused_by: Optional[str] = None
    occurred_at: Optional[datetime] = None


@dataclass(frozen=True, slots=True)
class NewDocument:
    """A frozen document a handler emits (§3.4). derived_from MUST reference committed docs.
    doc_id is caller-supplied via HandlerContext.new_doc_id(); append_document persists it
    (see Phase 02 / §3.4). Canonical shape lives in the overview (00) and Phase 02."""

    doc_id: str
    stage: str
    schema_version: int
    branch_role: str
    content_hash: str
    body_classification: str
    provenance: ProvenanceEnvelope
    body_ref: Optional[str] = None
    derived_from: tuple[str, ...] = ()
    supersedes: tuple[str, ...] = ()
    reject_reason: Optional[str] = None


class Disposition(str, Enum):
    OK = "ok"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class NewExternalCommand:
    """An external side effect to record in the §5.1 transaction (§5.4)."""

    integration: str
    idempotency_key: str
    request_payload: Mapping[str, Any]
    expected_run_id: Optional[str] = None
    expected_stream_version: Optional[int] = None
    expected_task_id: Optional[str] = None
    job_handle: Optional[str] = None
    dedup_supported: bool = False


@dataclass(frozen=True, slots=True)
class NewTimer:
    """A durable timer to schedule in the §5.1 transaction (§5.5)."""

    kind: str
    fire_at: datetime
    idempotency_key: str
    task_id: Optional[str] = None
    business_calendar: Optional[str] = None
    cas_task_version: Optional[int] = None
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NewActivation:
    """A cross-aggregate feature activation a handler requests (§5.8). Applied by commit_step
    via apply_activation() on the STEP-transaction conn (Phase 06) — never by the handler — so
    the CAS, VERSION_ACTIVATED/ACTIVATION_CONFLICT events, active-map update, and expiry timer
    are atomic with the rest of the step."""

    feature_id: str
    feature_version_id: str
    use_case: str
    base_feature_version_id: Optional[str]
    approval_type: str
    expires_at: Optional[datetime] = None
    provenance: Optional[ProvenanceEnvelope] = None


@dataclass(frozen=True, slots=True)
class HandlerResult:
    """A handler's typed return. Retry/permanent is signalled HERE, never via exceptions.
    Handlers are PURE: ALL effects are declared here and applied atomically by commit_step."""

    disposition: Disposition
    new_events: tuple[NewEvent, ...] = ()
    document: Optional[NewDocument] = None
    external_commands: tuple[NewExternalCommand, ...] = ()
    timers: tuple[NewTimer, ...] = ()
    activations: tuple[NewActivation, ...] = ()
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class HandlerContext:
    run_id: str
    triggering_event: EventEnvelope
    documents: Mapping[str, NewDocument]
    read_conn: "DbConn"  # READ-ONLY (autocommit): load stream/documents only; handlers MUST NOT write

    def new_doc_id(self) -> str:
        """Mint a 'doc_'-prefixed id so the handler can set NewDocument(doc_id=...) and reference
        that exact id in its emitted events; commit_step persists it via append_document.
        (requires: from uuid import uuid4)"""
        return f"doc_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class Command:
    action: str
    aggregate: str
    aggregate_id: Optional[str]
    args: Mapping[str, Any]
    actor: IdentityEnvelope
    idempotency_key: str
    expected_version: Optional[int] = None


@dataclass(frozen=True, slots=True)
class CommandResult:
    accepted: bool
    aggregate_id: str
    produced_event_ids: tuple[str, ...] = ()
    denied_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class GuardOutcome:
    passed: bool
    resolved_inputs: Mapping[str, Any]
    per_predicate: Mapping[str, bool]


@dataclass(frozen=True, slots=True)
class GateTaskSpec:
    gate: str
    required_inputs: tuple[str, ...]
    eligible_assignees: Mapping[str, str]
    allowed_responses: tuple[str, ...]
    run_id: Optional[str] = None
    feature_id: Optional[str] = None
    quorum_required: int = 1
    quorum_of_role: Optional[str] = None
    delegation_allowed: bool = True
    sla: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SignalResult:
    task_id: str
    status: str
    counted: bool
    quorum_met: bool
```

```python
# src/featuregen/contracts/protocols.py
from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol, runtime_checkable

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import (
    EventEnvelope,
    GuardOutcome,
    HandlerContext,
    HandlerResult,
)

GuardInputs = Mapping[str, Any]
Upcaster = Callable[[Mapping[str, Any]], Mapping[str, Any]]


@runtime_checkable
class Projection(Protocol):
    name: str
    is_analytics: bool

    def apply(self, conn: "DbConn", event: EventEnvelope) -> None:
        """Apply ONE event (events arrive in strict global_seq order). State-bearing
        projections raise ProjectionApplyError on an unappliable event."""

    def reset(self, conn: "DbConn") -> None:
        """Truncate this projection's tables for a from-zero rebuild."""


@runtime_checkable
class Handler(Protocol):
    name: str
    version: int
    timeout_seconds: float

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        """IDEMPOTENT (§5.3). MUST NOT emit feature-/request-stream events, write outside its
        run_id, or read mutable projections. Returns events (validated against the registry) and
        optionally one document. Signals retryable/permanent via HandlerResult.disposition."""


@runtime_checkable
class GuardPredicate(Protocol):
    name: str
    declared_inputs: tuple[str, ...]

    def __call__(self, inputs: GuardInputs) -> bool: ...


class PredicateRegistry(Protocol):
    def register(self, predicate: GuardPredicate) -> None: ...

    def get(self, name: str) -> GuardPredicate: ...

    def evaluate(self, guard_expr: str, inputs: GuardInputs) -> GuardOutcome: ...


class SchemaRegistry(Protocol):
    """Implemented twice: an event registry and a document/artifact registry."""

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None: ...

    def register_upcaster(
        self,
        type_name: str,
        from_version: int,
        to_version: int,
        upcaster: Upcaster,
    ) -> None: ...

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None: ...

    def upcast(
        self, type_name: str, body: Mapping[str, Any], from_version: int, to_version: int
    ) -> Mapping[str, Any]: ...

    def snapshot_version(self) -> str: ...
```

```python
# src/featuregen/contracts/__init__.py
from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.contracts.envelopes import (
    Command,
    CommandResult,
    Disposition,
    EventEnvelope,
    GateTaskSpec,
    GuardOutcome,
    HandlerContext,
    HandlerResult,
    IdentityEnvelope,
    NewDocument,
    NewEvent,
    NewExternalCommand,
    NewTimer,
    ProvenanceEnvelope,
    SignalResult,
)
from featuregen.contracts.errors import (
    ConcurrencyError,
    ProjectionApplyError,
    SchemaValidationError,
)
from featuregen.contracts.protocols import (
    GuardInputs,
    GuardPredicate,
    Handler,
    PredicateRegistry,
    Projection,
    SchemaRegistry,
    Upcaster,
)

__all__ = [
    "DbConn",
    "Command",
    "CommandResult",
    "Disposition",
    "EventEnvelope",
    "GateTaskSpec",
    "GuardOutcome",
    "HandlerContext",
    "HandlerResult",
    "IdentityEnvelope",
    "NewDocument",
    "NewEvent",
    "NewExternalCommand",
    "NewTimer",
    "ProvenanceEnvelope",
    "SignalResult",
    "ConcurrencyError",
    "ProjectionApplyError",
    "SchemaValidationError",
    "GuardInputs",
    "GuardPredicate",
    "Handler",
    "PredicateRegistry",
    "Projection",
    "SchemaRegistry",
    "Upcaster",
]
```

Then install dependencies:

```bash
python -m pip install -e ".[dev]"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/contracts/test_contract_symbols.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/featuregen/__init__.py src/featuregen/contracts tests/contracts/test_contract_symbols.py
git commit -m "feat(sp0-01): shared contract module + project scaffold"
```

---

### Task 2: Phase 01 DB migrations + pytest DB harness

**Files:**
- Create: `src/featuregen/db/__init__.py`
- Create: `src/featuregen/db/migrations.py`
- Create: `tests/conftest.py`
- Test: `tests/db/test_migrations.py`

**Interfaces:**
- Consumes: `DbConn` (Task 1).
- Produces: `apply_migrations(conn: DbConn) -> None`; the `global_seq_seq` sequence; tables `events` (with `events_optimistic_concurrency UNIQUE (aggregate, aggregate_id, stream_version)`, `events_global_seq_unique`, `events_aggregate_id_consistent`), `event_type_registry`, `registry_snapshots`, `projection_checkpoints`, `projection_active_alias`, `projection_degraded`; pytest fixtures `conn` (real PG15+ connection in a rolled-back transaction).

> Tests require a reachable PostgreSQL 15+. The DSN is read from env `SP0_TEST_DSN` (default `postgresql:///sp0_test`). Create the database once: `createdb sp0_test`.

- [ ] **Step 1: Write the failing test**

```python
# tests/db/test_migrations.py
from __future__ import annotations


def test_events_table_and_constraints_exist(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('public.events')")
        assert cur.fetchone()[0] == "events"
        cur.execute("SELECT to_regclass('public.event_type_registry')")
        assert cur.fetchone()[0] == "event_type_registry"
        cur.execute("SELECT to_regclass('public.registry_snapshots')")
        assert cur.fetchone()[0] == "registry_snapshots"
        cur.execute("SELECT to_regclass('public.projection_checkpoints')")
        assert cur.fetchone()[0] == "projection_checkpoints"
        cur.execute("SELECT to_regclass('public.projection_active_alias')")
        assert cur.fetchone()[0] == "projection_active_alias"
        cur.execute("SELECT to_regclass('public.projection_degraded')")
        assert cur.fetchone()[0] == "projection_degraded"
        cur.execute(
            "SELECT conname FROM pg_constraint WHERE conname = 'events_optimistic_concurrency'"
        )
        assert cur.fetchone()[0] == "events_optimistic_concurrency"


def test_global_seq_sequence_is_monotonic(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT nextval('global_seq_seq')")
        a = cur.fetchone()[0]
        cur.execute("SELECT nextval('global_seq_seq')")
        b = cur.fetchone()[0]
    assert b > a


def test_aggregate_id_consistency_check_rejects_mismatch(conn):
    import psycopg

    # `events_aggregate_id_consistent` is a non-deferrable CHECK: Postgres raises
    # CheckViolation at the INSERT (execute), NOT at commit. Wrap the INSERT itself in
    # a savepoint so the violation is caught here and the connection stays usable.
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                                        type, schema_version, table_version, actor, payload,
                                        provenance, occurred_at)
                    VALUES ('evt_bad', 'run', 'run_1', 1, 'run_2', 'X', 1, 1, '{}'::jsonb,
                            '{}'::jsonb, '{}'::jsonb, now())
                    """
                )
        raised = False
    except psycopg.errors.CheckViolation:
        # run aggregate with aggregate_id != run_id violates the CHECK at INSERT time.
        raised = True
    assert raised
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/db/test_migrations.py -q`
Expected: FAIL — `fixture 'conn' not found` / `ModuleNotFoundError: No module named 'featuregen.db'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/db/__init__.py
"""SP-0 database migrations."""
```

```python
# src/featuregen/db/migrations.py
from __future__ import annotations

from featuregen.contracts.db import DbConn

GLOBAL_SEQ = """
CREATE SEQUENCE IF NOT EXISTS global_seq_seq AS bigint
    INCREMENT BY 1 START WITH 1 NO CYCLE CACHE 1;
"""

EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id        text        PRIMARY KEY,
    global_seq      bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    aggregate       text        NOT NULL CHECK (aggregate IN ('request','feature','run')),
    aggregate_id    text        NOT NULL,
    stream_version  integer     NOT NULL CHECK (stream_version > 0),
    request_id      text        NULL,
    feature_id      text        NULL,
    run_id          text        NULL,
    type            text        NOT NULL,
    schema_version  integer     NOT NULL,
    table_version   integer     NOT NULL,
    actor           jsonb       NOT NULL,
    payload         jsonb       NOT NULL,
    provenance      jsonb       NOT NULL,
    caused_by       text        NULL REFERENCES events(event_id),
    occurred_at     timestamptz NOT NULL,
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT events_optimistic_concurrency UNIQUE (aggregate, aggregate_id, stream_version),
    CONSTRAINT events_global_seq_unique       UNIQUE (global_seq),
    CONSTRAINT events_aggregate_id_consistent CHECK (
        (aggregate = 'request' AND aggregate_id = request_id) OR
        (aggregate = 'feature' AND aggregate_id = feature_id) OR
        (aggregate = 'run'     AND aggregate_id = run_id)
    )
);
CREATE INDEX IF NOT EXISTS events_stream_idx   ON events (aggregate, aggregate_id, stream_version);
CREATE INDEX IF NOT EXISTS events_global_idx   ON events (global_seq);
CREATE INDEX IF NOT EXISTS events_run_idx      ON events (run_id)     WHERE run_id     IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_feature_idx  ON events (feature_id) WHERE feature_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_request_idx  ON events (request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS events_type_idx     ON events (type);
"""

EVENT_TYPE_REGISTRY = """
CREATE TABLE IF NOT EXISTS event_type_registry (
    type_name      text        NOT NULL,
    schema_version integer     NOT NULL,
    json_schema    jsonb       NOT NULL,
    owner          text        NOT NULL,
    status         text        NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','deprecated','withdrawn')),
    registered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (type_name, schema_version)
);
"""

REGISTRY_SNAPSHOTS = """
CREATE TABLE IF NOT EXISTS registry_snapshots (
    snapshot_id   text        PRIMARY KEY,
    registry      text        NOT NULL CHECK (registry IN ('events','docs')),
    captured_at   timestamptz NOT NULL DEFAULT now(),
    contents      jsonb       NOT NULL
);
"""

PROJECTION_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS projection_checkpoints (
    projection_name text        PRIMARY KEY,
    checkpoint_seq  bigint      NOT NULL DEFAULT 0,
    head_seq        bigint      NOT NULL DEFAULT 0,
    is_analytics    boolean     NOT NULL DEFAULT false,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
"""

# Phase-01-owned supporting table: atomic read-switch alias for parallel
# projection migration (§3.6). Not in the shared core DDL; internal to Phase 01.
PROJECTION_ACTIVE_ALIAS = """
CREATE TABLE IF NOT EXISTS projection_active_alias (
    alias            text        PRIMARY KEY,
    projection_name  text        NOT NULL,
    switched_seq     bigint      NOT NULL DEFAULT 0,
    switched_at      timestamptz NOT NULL DEFAULT now()
);
"""

# Phase-01-owned generic degraded ledger (§3.6). run_projection records the affected
# aggregate here (from ProjectionApplyError.aggregate/aggregate_id/reason) when a
# fail-closed projection cannot apply a poison event, realizing the shared run_projection
# docstring's "mark the affected aggregate degraded and stop advancing it" without
# depending on run_workflow_state (owned by a later phase).
PROJECTION_DEGRADED = """
CREATE TABLE IF NOT EXISTS projection_degraded (
    projection_name text        NOT NULL,
    aggregate       text        NOT NULL,
    aggregate_id    text        NOT NULL,
    reason          text        NOT NULL,
    poison_event_id text        NULL REFERENCES events(event_id),
    poison_seq      bigint      NOT NULL,
    degraded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_name, aggregate, aggregate_id)
);
"""

MIGRATIONS: list[tuple[str, str]] = [
    ("0001_global_seq", GLOBAL_SEQ),
    ("0002_events", EVENTS),
    ("0003_event_type_registry", EVENT_TYPE_REGISTRY),
    ("0004_registry_snapshots", REGISTRY_SNAPSHOTS),
    ("0005_projection_checkpoints", PROJECTION_CHECKPOINTS),
    ("0006_projection_active_alias", PROJECTION_ACTIVE_ALIAS),
    ("0007_projection_degraded", PROJECTION_DEGRADED),
]


def apply_migrations(conn: DbConn) -> None:
    """Create all Phase 01 DDL objects (idempotent)."""
    with conn.cursor() as cur:
        for _name, sql in MIGRATIONS:
            cur.execute(sql)
    conn.commit()
```

```python
# tests/conftest.py
from __future__ import annotations

import os

import psycopg
import pytest

from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import reset_event_registry

DSN = os.environ.get("SP0_TEST_DSN", "postgresql:///sp0_test")


@pytest.fixture(scope="session")
def _migrated() -> bool:
    with psycopg.connect(DSN) as setup_conn:
        apply_migrations(setup_conn)
    return True


@pytest.fixture
def conn(_migrated):
    connection = psycopg.connect(DSN)
    try:
        yield connection
        connection.rollback()
    finally:
        connection.close()


@pytest.fixture(autouse=True)
def _reset_registry():
    reset_event_registry()
    yield
    reset_event_registry()
```

> `tests/conftest.py` imports `reset_event_registry` from `featuregen.events.registry`, created in Task 4. Until then this import fails; that is expected and is resolved by Task 4. To keep Task 2 green in isolation, create the stub now:

```python
# src/featuregen/events/__init__.py
"""SP-0 event store."""
```

```python
# src/featuregen/events/registry.py
from __future__ import annotations


def reset_event_registry() -> None:
    """Placeholder; real registry singleton is added in Task 4."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/db/test_migrations.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db src/featuregen/events/__init__.py src/featuregen/events/registry.py tests/conftest.py tests/db/test_migrations.py
git commit -m "feat(sp0-01): Phase 01 DDL migrations + pytest PG harness"
```

---

### Task 3: Envelope serde (identity / provenance / event row <-> jsonb)

**Files:**
- Create: `src/featuregen/events/serde.py`
- Test: `tests/events/test_serde.py`

**Interfaces:**
- Consumes: `IdentityEnvelope`, `ProvenanceEnvelope`, `EventEnvelope` (Task 1).
- Produces: `identity_to_jsonb(IdentityEnvelope) -> dict`, `identity_from_jsonb(Mapping) -> IdentityEnvelope`, `provenance_to_jsonb(ProvenanceEnvelope) -> dict`, `provenance_from_jsonb(Mapping) -> ProvenanceEnvelope`, `row_to_event(Mapping) -> EventEnvelope`.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_serde.py
from __future__ import annotations

from datetime import datetime, timezone

from featuregen.contracts import EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.serde import (
    identity_from_jsonb,
    identity_to_jsonb,
    provenance_from_jsonb,
    provenance_to_jsonb,
    row_to_event,
)


def test_identity_round_trips_tuples_as_lists():
    idv = IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake", "writer"),
        groups=("g1",),
        attestation="deploy:abc",
    )
    blob = identity_to_jsonb(idv)
    assert blob["role_claims"] == ["intake", "writer"]
    assert identity_from_jsonb(blob) == idv


def test_provenance_round_trips():
    prov = ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=2,
        producing_component="sp2-intake@1.4.0",
        tool_versions={"llm_model": "x"},
        source_snapshots=("delta:core@v1",),
        random_seed=42,
    )
    blob = provenance_to_jsonb(prov)
    assert blob["source_snapshots"] == ["delta:core@v1"]
    assert provenance_from_jsonb(blob) == prov


def test_row_to_event_reconstructs_envelope():
    now = datetime.now(timezone.utc)
    row = {
        "event_id": "evt_1",
        "global_seq": 7,
        "aggregate": "run",
        "aggregate_id": "run_1",
        "stream_version": 3,
        "type": "CONTRACT_CONFIRMED",
        "schema_version": 1,
        "table_version": 12,
        "actor": identity_to_jsonb(
            IdentityEnvelope(
                subject="user:raj",
                actor_kind="human",
                authenticated=True,
                auth_method="oidc",
                role_claims=(),
            )
        ),
        "payload": {"confirmed_contract_ref": "doc_1"},
        "provenance": provenance_to_jsonb(
            ProvenanceEnvelope(
                artifact_type="CONFIRMED_CONTRACT",
                schema_version=1,
                producing_component="c@1",
            )
        ),
        "occurred_at": now,
        "recorded_at": now,
        "request_id": "req_1",
        "feature_id": None,
        "run_id": "run_1",
        "caused_by": None,
    }
    env = row_to_event(row)
    assert isinstance(env, EventEnvelope)
    assert env.global_seq == 7
    assert env.actor.subject == "user:raj"
    assert env.payload["confirmed_contract_ref"] == "doc_1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_serde.py -q`
Expected: FAIL — `ImportError: cannot import name 'identity_from_jsonb' from 'featuregen.events.serde'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/events/serde.py
from __future__ import annotations

from typing import Any, Mapping

from featuregen.contracts import EventEnvelope, IdentityEnvelope, ProvenanceEnvelope


def identity_to_jsonb(idv: IdentityEnvelope) -> dict[str, Any]:
    return {
        "subject": idv.subject,
        "actor_kind": idv.actor_kind,
        "authenticated": idv.authenticated,
        "auth_method": idv.auth_method,
        "role_claims": list(idv.role_claims),
        "groups": list(idv.groups),
        "tenant": idv.tenant,
        "on_behalf_of": idv.on_behalf_of,
        "impersonation": idv.impersonation,
        "break_glass": idv.break_glass,
        "source_of_authority": idv.source_of_authority,
        "attestation": idv.attestation,
    }


def identity_from_jsonb(d: Mapping[str, Any]) -> IdentityEnvelope:
    return IdentityEnvelope(
        subject=d["subject"],
        actor_kind=d["actor_kind"],
        authenticated=d["authenticated"],
        auth_method=d["auth_method"],
        role_claims=tuple(d.get("role_claims", ())),
        groups=tuple(d.get("groups", ())),
        tenant=d.get("tenant"),
        on_behalf_of=d.get("on_behalf_of"),
        impersonation=d.get("impersonation"),
        break_glass=d.get("break_glass", False),
        source_of_authority=d.get("source_of_authority"),
        attestation=d.get("attestation"),
    )


def provenance_to_jsonb(p: ProvenanceEnvelope) -> dict[str, Any]:
    return {
        "artifact_type": p.artifact_type,
        "schema_version": p.schema_version,
        "producing_component": p.producing_component,
        "tool_versions": dict(p.tool_versions),
        "dsl_operation_catalog_version": p.dsl_operation_catalog_version,
        "source_snapshots": list(p.source_snapshots),
        "event_registry_snapshot": p.event_registry_snapshot,
        "doc_registry_snapshot": p.doc_registry_snapshot,
        "evaluation_dataset_ref": p.evaluation_dataset_ref,
        "holdout_partition_spec": p.holdout_partition_spec,
        "random_seed": p.random_seed,
        "candidates_explored_count": p.candidates_explored_count,
        "external_refs": list(p.external_refs),
    }


def provenance_from_jsonb(d: Mapping[str, Any]) -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type=d["artifact_type"],
        schema_version=d["schema_version"],
        producing_component=d["producing_component"],
        tool_versions=dict(d.get("tool_versions", {})),
        dsl_operation_catalog_version=d.get("dsl_operation_catalog_version"),
        source_snapshots=tuple(d.get("source_snapshots", ())),
        event_registry_snapshot=d.get("event_registry_snapshot"),
        doc_registry_snapshot=d.get("doc_registry_snapshot"),
        evaluation_dataset_ref=d.get("evaluation_dataset_ref"),
        holdout_partition_spec=d.get("holdout_partition_spec"),
        random_seed=d.get("random_seed"),
        candidates_explored_count=d.get("candidates_explored_count"),
        external_refs=tuple(d.get("external_refs", ())),
    )


def row_to_event(row: Mapping[str, Any]) -> EventEnvelope:
    return EventEnvelope(
        event_id=row["event_id"],
        global_seq=row["global_seq"],
        aggregate=row["aggregate"],
        aggregate_id=row["aggregate_id"],
        stream_version=row["stream_version"],
        type=row["type"],
        schema_version=row["schema_version"],
        table_version=row["table_version"],
        actor=identity_from_jsonb(row["actor"]),
        payload=row["payload"],
        provenance=provenance_from_jsonb(row["provenance"]),
        occurred_at=row["occurred_at"],
        recorded_at=row["recorded_at"],
        request_id=row["request_id"],
        feature_id=row["feature_id"],
        run_id=row["run_id"],
        caused_by=row["caused_by"],
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_serde.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/serde.py tests/events/test_serde.py
git commit -m "feat(sp0-01): identity/provenance/event jsonb serde"
```

---

### Task 4: Event schema registry — register + validate + singleton

**Files:**
- Modify: `src/featuregen/events/registry.py` (replace the Task 2 stub with the real registry)
- Test: `tests/events/test_registry_validate.py`

**Interfaces:**
- Consumes: `SchemaRegistry` Protocol, `SchemaValidationError` (Task 1).
- Produces: class `EventSchemaRegistry` with `register_schema(type_name, schema_version, json_schema, owner, *, status="active") -> None` and `validate(type_name, schema_version, body) -> None`; module accessors `event_registry() -> EventSchemaRegistry` and `reset_event_registry() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_registry_validate.py
from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import EventSchemaRegistry, event_registry, reset_event_registry

SCHEMA = {
    "type": "object",
    "required": ["confirmed_contract_ref"],
    "properties": {"confirmed_contract_ref": {"type": "string"}},
    "additionalProperties": True,
}


def test_validate_accepts_conforming_payload():
    reg = EventSchemaRegistry()
    reg.register_schema("CONTRACT_CONFIRMED", 1, SCHEMA, owner="sp2")
    reg.validate("CONTRACT_CONFIRMED", 1, {"confirmed_contract_ref": "doc_1"})


def test_validate_rejects_missing_required_field():
    reg = EventSchemaRegistry()
    reg.register_schema("CONTRACT_CONFIRMED", 1, SCHEMA, owner="sp2")
    with pytest.raises(SchemaValidationError):
        reg.validate("CONTRACT_CONFIRMED", 1, {"other": 1})


def test_validate_unknown_type_raises():
    reg = EventSchemaRegistry()
    with pytest.raises(SchemaValidationError):
        reg.validate("NOPE", 1, {})


def test_singleton_is_process_global_and_resettable():
    event_registry().register_schema("X", 1, {"type": "object"}, owner="o")
    assert event_registry() is event_registry()
    reset_event_registry()
    with pytest.raises(SchemaValidationError):
        event_registry().validate("X", 1, {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_registry_validate.py -q`
Expected: FAIL — `ImportError: cannot import name 'EventSchemaRegistry'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/events/registry.py
from __future__ import annotations

from typing import Any, Mapping, Optional

import jsonschema

from featuregen.contracts import SchemaValidationError, Upcaster


class EventSchemaRegistry:
    """Event-type registry (§3.3): versioned JSON schemas, stepwise upcasters,
    deprecate/withdraw lifecycle, pinnable snapshot id."""

    def __init__(self) -> None:
        self._schemas: dict[tuple[str, int], dict[str, Any]] = {}
        self._owners: dict[tuple[str, int], str] = {}
        self._status: dict[tuple[str, int], str] = {}
        self._upcasters: dict[tuple[str, int], Upcaster] = {}

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None:
        key = (type_name, schema_version)
        self._schemas[key] = dict(json_schema)
        self._owners[key] = owner
        self._status[key] = status

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None:
        key = (type_name, schema_version)
        schema = self._schemas.get(key)
        if schema is None:
            raise SchemaValidationError(f"no schema registered for {type_name}@v{schema_version}")
        try:
            jsonschema.validate(instance=dict(body), schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError(
                f"{type_name}@v{schema_version}: {exc.message}"
            ) from exc


_REGISTRY: Optional[EventSchemaRegistry] = None


def event_registry() -> EventSchemaRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = EventSchemaRegistry()
    return _REGISTRY


def reset_event_registry() -> None:
    global _REGISTRY
    _REGISTRY = EventSchemaRegistry()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_registry_validate.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/registry.py tests/events/test_registry_validate.py
git commit -m "feat(sp0-01): event schema registry register+validate+singleton"
```

---

### Task 5: Upcasters — stepwise, total, chained

**Files:**
- Modify: `src/featuregen/events/registry.py` (add `register_upcaster`, `upcast`)
- Test: `tests/events/test_registry_upcast.py`

**Interfaces:**
- Consumes: `Upcaster` alias, `SchemaValidationError` (Task 1); `EventSchemaRegistry` (Task 4).
- Produces: `EventSchemaRegistry.register_upcaster(type_name, from_version, to_version, upcaster) -> None` (requires `to_version == from_version + 1`); `EventSchemaRegistry.upcast(type_name, body, from_version, to_version) -> Mapping` (chains stepwise; a missing step raises `SchemaValidationError`).

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_registry_upcast.py
from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import EventSchemaRegistry


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "added_v2": True})
    reg.register_upcaster("T", 2, 3, lambda b: {**b, "added_v3": 1})
    return reg


def test_upcast_chains_stepwise():
    out = _reg().upcast("T", {"orig": 1}, 1, 3)
    assert out == {"orig": 1, "added_v2": True, "added_v3": 1}


def test_upcast_noop_when_versions_equal():
    out = _reg().upcast("T", {"orig": 1}, 3, 3)
    assert out == {"orig": 1}


def test_upcast_missing_step_is_poison_error():
    reg = EventSchemaRegistry()
    reg.register_upcaster("T", 1, 2, lambda b: b)
    # no 2->3 registered
    with pytest.raises(SchemaValidationError):
        reg.upcast("T", {"x": 1}, 1, 3)


def test_register_upcaster_must_be_stepwise():
    reg = EventSchemaRegistry()
    with pytest.raises(ValueError):
        reg.register_upcaster("T", 1, 3, lambda b: b)


def test_upcast_cannot_downcast():
    with pytest.raises(SchemaValidationError):
        _reg().upcast("T", {"x": 1}, 3, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_registry_upcast.py -q`
Expected: FAIL — `AttributeError: 'EventSchemaRegistry' object has no attribute 'register_upcaster'`.

- [ ] **Step 3: Write minimal implementation**

Add these methods to `EventSchemaRegistry` in `src/featuregen/events/registry.py`:

```python
    def register_upcaster(
        self,
        type_name: str,
        from_version: int,
        to_version: int,
        upcaster: Upcaster,
    ) -> None:
        if to_version != from_version + 1:
            raise ValueError(
                f"upcaster must be stepwise vN->vN+1, got {from_version}->{to_version}"
            )
        self._upcasters[(type_name, from_version)] = upcaster

    def upcast(
        self,
        type_name: str,
        body: Mapping[str, Any],
        from_version: int,
        to_version: int,
    ) -> Mapping[str, Any]:
        if to_version < from_version:
            raise SchemaValidationError(
                f"cannot downcast {type_name} {from_version}->{to_version}"
            )
        current: dict[str, Any] = dict(body)
        version = from_version
        while version < to_version:
            step = self._upcasters.get((type_name, version))
            if step is None:
                raise SchemaValidationError(
                    f"missing upcaster {type_name} {version}->{version + 1}"
                )
            current = dict(step(current))
            version += 1
        return current
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_registry_upcast.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/registry.py tests/events/test_registry_upcast.py
git commit -m "feat(sp0-01): stepwise total chained event upcasters"
```

---

### Task 6: Backward-compatibility rule

**Files:**
- Modify: `src/featuregen/events/registry.py` (add module function `is_backward_compatible`)
- Test: `tests/events/test_registry_backward_compat.py`

**Interfaces:**
- Consumes: `SchemaValidationError` (Task 1); `EventSchemaRegistry` (Task 4), `register_upcaster` (Task 5).
- Produces: `is_backward_compatible(old_schema: Mapping, new_schema: Mapping) -> bool` — True for "add optional field / widen a type / add an enum value"; False for any breaking change (new required field, removed property, narrowed type, removed/added-narrowing enum). Plus `EventSchemaRegistry.assert_evolution_complete() -> None` — the §3.3 "breaking → new schema_version + **mandatory** upcaster" rule, actively enforced: for every type, any consecutive registered version pair `(v, v+k)` that is **not** backward-compatible MUST have a registered stepwise upcaster covering every step `v..v+k`, else raise `SchemaValidationError` (a *load-time* error, not a lazy read-time poison). `is_backward_compatible` is wired into this production path; `assert_evolution_complete` is invoked by `persist_event_schemas` (Task 8) so a breaking schema bump without its upcaster is rejected before it is ever durably recorded or read.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_registry_backward_compat.py
from __future__ import annotations

from featuregen.events.registry import is_backward_compatible

V1 = {
    "type": "object",
    "required": ["a"],
    "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
}


def test_add_optional_field_is_compatible():
    v2 = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "boolean"},
        },
    }
    assert is_backward_compatible(V1, v2) is True


def test_widen_type_is_compatible():
    v2 = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": ["integer", "number"]}},
    }
    assert is_backward_compatible(V1, v2) is True


def test_add_enum_value_is_compatible():
    old = {"type": "object", "properties": {"e": {"enum": ["x"]}}}
    new = {"type": "object", "properties": {"e": {"enum": ["x", "y"]}}}
    assert is_backward_compatible(old, new) is True


def test_new_required_field_is_breaking():
    v2 = {
        "type": "object",
        "required": ["a", "c"],
        "properties": {
            "a": {"type": "string"},
            "b": {"type": "integer"},
            "c": {"type": "string"},
        },
    }
    assert is_backward_compatible(V1, v2) is False


def test_removed_property_is_breaking():
    v2 = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
    assert is_backward_compatible(V1, v2) is False


def test_narrowed_type_is_breaking():
    old = {"type": "object", "properties": {"b": {"type": ["integer", "number"]}}}
    new = {"type": "object", "properties": {"b": {"type": "integer"}}}
    assert is_backward_compatible(old, new) is False


def test_removed_enum_value_is_breaking():
    old = {"type": "object", "properties": {"e": {"enum": ["x", "y"]}}}
    new = {"type": "object", "properties": {"e": {"enum": ["x"]}}}
    assert is_backward_compatible(old, new) is False


# ── assert_evolution_complete: §3.3 breaking-bump => mandatory upcaster (active enforcement)
import pytest  # noqa: E402

from featuregen.contracts import SchemaValidationError  # noqa: E402
from featuregen.events.registry import EventSchemaRegistry  # noqa: E402

_BREAKING_V2 = {
    "type": "object",
    "required": ["a", "c"],  # new required field 'c' breaks old writers
    "properties": {"a": {"type": "string"}, "c": {"type": "string"}},
}


def test_evolution_complete_passes_for_compatible_chain():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema(
        "T", 2,
        {"type": "object", "required": ["a"],
         "properties": {"a": {"type": "string"}, "b": {"type": "integer"},
                        "d": {"type": "boolean"}}},  # add optional field => compatible
        owner="o",
    )
    reg.assert_evolution_complete()  # no upcaster needed; does not raise


def test_evolution_breaking_bump_without_upcaster_raises():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema("T", 2, _BREAKING_V2, owner="o")  # breaking, no 1->2 upcaster
    with pytest.raises(SchemaValidationError):
        reg.assert_evolution_complete()


def test_evolution_breaking_bump_with_upcaster_passes():
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, V1, owner="o")
    reg.register_schema("T", 2, _BREAKING_V2, owner="o")
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "c": "backfilled"})
    reg.assert_evolution_complete()  # mandatory upcaster present => does not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_registry_backward_compat.py -q`
Expected: FAIL — `ImportError: cannot import name 'is_backward_compatible'` (and, once that import resolves, `AttributeError: ... has no attribute 'assert_evolution_complete'`).

- [ ] **Step 3: Write minimal implementation**

Add to `src/featuregen/events/registry.py` (module level, after the class):

```python
def _types_of(spec: Mapping[str, Any]) -> set[str]:
    t = spec.get("type")
    if t is None:
        return set()
    return set(t) if isinstance(t, list) else {t}


def _type_compatible(old_spec: Mapping[str, Any], new_spec: Mapping[str, Any]) -> bool:
    old_types = _types_of(old_spec)
    new_types = _types_of(new_spec)
    if not old_types or not new_types:
        return True  # unconstrained on either side: not a narrowing we track
    return old_types <= new_types  # widening (superset) is compatible


def _enum_compatible(old_spec: Mapping[str, Any], new_spec: Mapping[str, Any]) -> bool:
    old_enum = old_spec.get("enum")
    new_enum = new_spec.get("enum")
    if old_enum is None and new_enum is None:
        return True
    if old_enum is None and new_enum is not None:
        return False  # adding an enum constraint narrows
    if old_enum is not None and new_enum is None:
        return True  # dropping the enum constraint widens
    return set(old_enum) <= set(new_enum)  # adding values is compatible


def is_backward_compatible(old_schema: Mapping[str, Any], new_schema: Mapping[str, Any]) -> bool:
    """§3.3 backward-compat rule: compatible iff the new schema only adds optional
    fields, widens types, or adds enum values; anything else is breaking."""
    old_props: Mapping[str, Any] = old_schema.get("properties", {})
    new_props: Mapping[str, Any] = new_schema.get("properties", {})
    old_required = set(old_schema.get("required", []))
    new_required = set(new_schema.get("required", []))

    if new_required - old_required:
        return False  # a newly-required field breaks old writers
    if set(old_props) - set(new_props):
        return False  # removing a known property breaks old readers
    for name, old_spec in old_props.items():
        new_spec = new_props[name]
        if not _type_compatible(old_spec, new_spec):
            return False
        if not _enum_compatible(old_spec, new_spec):
            return False
    return True
```

Then add this method to `EventSchemaRegistry` (it consumes `is_backward_compatible` defined
above, so place the method after the module functions or reference them lazily inside the body):

```python
    def assert_evolution_complete(self) -> None:
        """§3.3 load-time enforcement: a breaking schema bump REQUIRES a stepwise upcaster.
        For every type, each consecutive registered version pair that is not backward-compatible
        must have a registered upcaster for every step between them; otherwise raise
        SchemaValidationError (a load-time error, never a lazy read-time poison)."""
        by_type: dict[str, list[int]] = {}
        for (type_name, version) in self._schemas:
            by_type.setdefault(type_name, []).append(version)
        for type_name, versions in by_type.items():
            versions.sort()
            for prev, nxt in zip(versions, versions[1:]):
                if is_backward_compatible(self._schemas[(type_name, prev)],
                                          self._schemas[(type_name, nxt)]):
                    continue  # additive bump: no upcaster required
                for step in range(prev, nxt):
                    if (type_name, step) not in self._upcasters:
                        raise SchemaValidationError(
                            f"breaking schema bump {type_name} v{prev}->v{nxt} requires a "
                            f"stepwise upcaster {type_name} v{step}->v{step + 1}"
                        )
```

> `is_backward_compatible` is now load-bearing on a production path: `assert_evolution_complete`
> calls it, and Task 8's `persist_event_schemas` calls `assert_evolution_complete` before any
> durable write — so a breaking bump without its upcaster is rejected at registration/persist
> time, not lazily at read.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_registry_backward_compat.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/registry.py tests/events/test_registry_backward_compat.py
git commit -m "feat(sp0-01): backward-compatibility rule for event schemas"
```

---

### Task 7: Deprecation lifecycle + write-gating

**Files:**
- Modify: `src/featuregen/events/registry.py` (add `set_status`, `assert_writable`)
- Test: `tests/events/test_registry_lifecycle.py`

**Interfaces:**
- Consumes: `SchemaValidationError` (Task 1); `EventSchemaRegistry` (Task 4), `upcast` (Task 5).
- Produces: `EventSchemaRegistry.set_status(type_name, schema_version, status) -> None` (status in `active|deprecated|withdrawn`); `EventSchemaRegistry.assert_writable(type_name, schema_version) -> None` (raises `SchemaValidationError` unless status is `active`). Deprecated/withdrawn versions remain readable/upcastable.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_registry_lifecycle.py
from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import EventSchemaRegistry


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_schema("T", 1, {"type": "object"}, owner="o")
    reg.register_schema("T", 2, {"type": "object"}, owner="o")
    reg.register_upcaster("T", 1, 2, lambda b: {**b, "v2": True})
    return reg


def test_active_version_is_writable():
    _reg().assert_writable("T", 2)


def test_deprecated_version_blocks_new_writes():
    reg = _reg()
    reg.set_status("T", 1, "deprecated")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("T", 1)


def test_withdrawn_version_blocks_writes_but_stays_readable():
    reg = _reg()
    reg.set_status("T", 1, "withdrawn")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("T", 1)
    # in-flight v1 body still upcasts to v2
    assert reg.upcast("T", {"orig": 1}, 1, 2) == {"orig": 1, "v2": True}


def test_set_status_unknown_version_raises():
    with pytest.raises(SchemaValidationError):
        _reg().set_status("T", 99, "deprecated")


def test_assert_writable_unknown_version_raises():
    with pytest.raises(SchemaValidationError):
        _reg().assert_writable("T", 99)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_registry_lifecycle.py -q`
Expected: FAIL — `AttributeError: 'EventSchemaRegistry' object has no attribute 'set_status'`.

- [ ] **Step 3: Write minimal implementation**

Add these methods to `EventSchemaRegistry` in `src/featuregen/events/registry.py`:

```python
    def set_status(self, type_name: str, schema_version: int, status: str) -> None:
        key = (type_name, schema_version)
        if key not in self._status:
            raise SchemaValidationError(f"unknown schema {type_name}@v{schema_version}")
        if status not in ("active", "deprecated", "withdrawn"):
            raise SchemaValidationError(f"invalid status {status!r}")
        self._status[key] = status

    def assert_writable(self, type_name: str, schema_version: int) -> None:
        status = self._status.get((type_name, schema_version))
        if status is None:
            raise SchemaValidationError(f"unknown schema {type_name}@v{schema_version}")
        if status != "active":
            raise SchemaValidationError(
                f"{type_name}@v{schema_version} is {status}; no new writes allowed"
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_registry_lifecycle.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/registry.py tests/events/test_registry_lifecycle.py
git commit -m "feat(sp0-01): schema deprecate/withdraw lifecycle + write-gating"
```

---

### Task 8: Pinnable registry snapshot + durable persistence, hydration & snapshot read-path

**Files:**
- Modify: `src/featuregen/events/registry.py` (add `snapshot_version`, `max_active_versions`, `all_schemas`; module functions `persist_event_schemas`, `persist_registry_snapshot`, `load_registry_snapshot`, `hydrate_event_registry`)
- Test: `tests/events/test_registry_snapshot.py`
- Test: `tests/events/test_registry_hydrate.py`

**Interfaces:**
- Consumes: `DbConn`, `SchemaValidationError` (Task 1); `EventSchemaRegistry` (Task 4), `assert_evolution_complete` (Task 6), `event_registry`/`reset_event_registry` (Task 4); tables `event_type_registry`, `registry_snapshots` (Task 2).
- Produces:
  - `EventSchemaRegistry.snapshot_version() -> str` — a **content-addressed** pinnable id `events@<sha256-16hex>` computed over the `{type_name: max_active_version}` map. Distinct registry states yield distinct ids; identical states yield the **same** id (so a provenance-pinned id always resolves to exactly one `{type: version}` map — the §3.3 replay-determinism guarantee). This replaces the earlier `events@vN` counter, which collided (e.g. `{A@1,A@2}` and `{A@1,B@1}` both → `events@v2`) and let `ON CONFLICT DO UPDATE` overwrite an earlier snapshot's contents.
  - `persist_event_schemas(conn, registry) -> None` — calls `registry.assert_evolution_complete()` first (load-time §3.3 enforcement), then idempotently upserts the in-memory schemas into `event_type_registry`.
  - `persist_registry_snapshot(conn, registry) -> str` — writes `{type_name: max_active_version}` to `registry_snapshots` under the content-addressed id; returns the id.
  - `load_registry_snapshot(conn, snapshot_id) -> dict[str, int]` — **the read path**: resolves a pinned snapshot id back to its `{type_name: schema_version}` map so a replay can drive upcast-on-read deterministically (consumed by `load_stream`'s `expected=` in Task 11). Raises `SchemaValidationError` on an unknown id.
  - `hydrate_event_registry(conn) -> EventSchemaRegistry` — reconstitutes the process-global registry **singleton's schemas** from `event_type_registry` (resets then reloads), so a fresh process can `validate`/`append` without re-declaring every schema by hand. (Upcasters are code and are re-registered at import; hydration restores schemas + status only.)

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_registry_snapshot.py
from __future__ import annotations

import pytest
from psycopg.rows import dict_row

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import (
    EventSchemaRegistry,
    load_registry_snapshot,
    persist_event_schemas,
    persist_registry_snapshot,
)


def _reg() -> EventSchemaRegistry:
    reg = EventSchemaRegistry()
    reg.register_schema("A", 1, {"type": "object"}, owner="o")
    reg.register_schema("A", 2, {"type": "object"}, owner="o")
    reg.register_schema("B", 1, {"type": "object"}, owner="o")
    return reg


def test_snapshot_id_is_content_addressed_and_deterministic():
    sid = _reg().snapshot_version()
    assert sid.startswith("events@")
    # Deterministic: the same registry state yields the same id, every call and across
    # independently-built registries.
    assert _reg().snapshot_version() == sid


def test_distinct_states_get_distinct_ids_no_collision():
    # {A@1, A@2} (max-active {A:2}) vs {A@1, B@1} (max-active {A:1, B:1}) MUST differ —
    # the exact collision the old len()-based id produced.
    r1 = EventSchemaRegistry()
    r1.register_schema("A", 1, {"type": "object"}, owner="o")
    r1.register_schema("A", 2, {"type": "object"}, owner="o")
    r2 = EventSchemaRegistry()
    r2.register_schema("A", 1, {"type": "object"}, owner="o")
    r2.register_schema("B", 1, {"type": "object"}, owner="o")
    assert r1.snapshot_version() != r2.snapshot_version()


def test_withdrawing_a_version_changes_the_snapshot_id():
    reg = _reg()  # max-active {A:2, B:1}
    before = reg.snapshot_version()
    reg.set_status("A", 2, "withdrawn")  # max-active becomes {A:1, B:1}
    assert reg.snapshot_version() != before


def test_persist_event_schemas_writes_rows(conn):
    persist_event_schemas(conn, _reg())
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM event_type_registry")
        assert cur.fetchone()["n"] == 3


def test_persist_event_schemas_rejects_breaking_bump_without_upcaster(conn):
    reg = EventSchemaRegistry()
    reg.register_schema(
        "T", 1, {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}},
        owner="o",
    )
    reg.register_schema(
        "T", 2,
        {"type": "object", "required": ["a", "b"],
         "properties": {"a": {"type": "string"}, "b": {"type": "string"}}},  # breaking
        owner="o",
    )
    with pytest.raises(SchemaValidationError):
        persist_event_schemas(conn, reg)  # assert_evolution_complete fires before any write
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM event_type_registry WHERE type_name='T'")
        assert cur.fetchone()["n"] == 0  # rejected before durable write


def test_persist_snapshot_records_max_active_version(conn):
    reg = _reg()
    reg.set_status("A", 2, "withdrawn")  # excluded from max-active
    snapshot_id = persist_registry_snapshot(conn, reg)
    assert snapshot_id == reg.snapshot_version()
    assert snapshot_id.startswith("events@")
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT registry, contents FROM registry_snapshots WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
    assert row["registry"] == "events"
    assert row["contents"] == {"A": 1, "B": 1}


def test_snapshot_round_trips_to_type_version_map(conn):
    # The pinned-snapshot READ path: persist then resolve the id back to {type: version}.
    reg = _reg()  # max-active {A:2, B:1}
    snapshot_id = persist_registry_snapshot(conn, reg)
    assert load_registry_snapshot(conn, snapshot_id) == {"A": 2, "B": 1}


def test_load_unknown_snapshot_raises(conn):
    with pytest.raises(SchemaValidationError):
        load_registry_snapshot(conn, "events@deadbeefdeadbeef")
```

```python
# tests/events/test_registry_hydrate.py
from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import (
    EventSchemaRegistry,
    event_registry,
    hydrate_event_registry,
    persist_event_schemas,
    reset_event_registry,
)


def test_hydrate_reconstitutes_schemas_from_db(conn):
    seed = EventSchemaRegistry()
    seed.register_schema(
        "RUN_STARTED", 1,
        {"type": "object", "required": ["x"], "properties": {"x": {"type": "string"}}},
        owner="sp0",
    )
    persist_event_schemas(conn, seed)

    # Simulate a fresh process: the in-memory singleton knows nothing yet.
    reset_event_registry()
    with pytest.raises(SchemaValidationError):
        event_registry().validate("RUN_STARTED", 1, {"x": "ok"})

    # Hydrate from the durable table -> validation works again, constraints intact.
    hydrate_event_registry(conn)
    event_registry().validate("RUN_STARTED", 1, {"x": "ok"})
    with pytest.raises(SchemaValidationError):
        event_registry().validate("RUN_STARTED", 1, {})  # missing required 'x'


def test_hydrate_preserves_status(conn):
    seed = EventSchemaRegistry()
    seed.register_schema("T", 1, {"type": "object"}, owner="o")
    seed.set_status("T", 1, "deprecated")
    persist_event_schemas(conn, seed)

    reset_event_registry()
    hydrate_event_registry(conn)
    with pytest.raises(SchemaValidationError):
        event_registry().assert_writable("T", 1)  # deprecated status survived the round-trip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_registry_snapshot.py tests/events/test_registry_hydrate.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_registry_snapshot'` / `hydrate_event_registry` (and `persist_event_schemas`).

- [ ] **Step 3: Write minimal implementation**

Add `snapshot_version` method to `EventSchemaRegistry`:

```python
    def snapshot_version(self) -> str:
        """Content-addressed pinnable snapshot id over the {type_name: max_active_version} map.
        Identical registry states yield the SAME id; distinct states yield DISTINCT ids — so a
        provenance-pinned id resolves to exactly one {type: version} map (§3.3 determinism)."""
        canonical = _json.dumps(self.max_active_versions(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
        return f"events@{digest}"

    def max_active_versions(self) -> dict[str, int]:
        """{type_name: highest active schema_version} for the snapshot contents."""
        out: dict[str, int] = {}
        for (type_name, version), status in self._status.items():
            if status == "active":
                out[type_name] = max(out.get(type_name, 0), version)
        return out

    def all_schemas(self) -> list[tuple[str, int, dict[str, Any], str, str]]:
        """(type_name, schema_version, json_schema, owner, status) for every registration."""
        return [
            (t, v, self._schemas[(t, v)], self._owners[(t, v)], self._status[(t, v)])
            for (t, v) in self._schemas
        ]
```

Add these imports near the top of `src/featuregen/events/registry.py`:

```python
import hashlib
import json as _json

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from featuregen.contracts.db import DbConn
```

Add module-level functions:

```python
def persist_event_schemas(conn: DbConn, registry: EventSchemaRegistry) -> None:
    """Durably record the in-memory schemas in event_type_registry (idempotent upsert).
    Enforces the §3.3 breaking-bump rule FIRST: assert_evolution_complete() raises before any
    write if a breaking schema bump lacks its mandatory upcaster."""
    registry.assert_evolution_complete()
    with conn.cursor() as cur:
        for type_name, version, json_schema, owner, status in registry.all_schemas():
            cur.execute(
                """
                INSERT INTO event_type_registry
                    (type_name, schema_version, json_schema, owner, status)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (type_name, schema_version)
                DO UPDATE SET json_schema = EXCLUDED.json_schema,
                              owner = EXCLUDED.owner,
                              status = EXCLUDED.status
                """,
                (type_name, version, Jsonb(json_schema), owner, status),
            )


def persist_registry_snapshot(conn: DbConn, registry: EventSchemaRegistry) -> str:
    """Write {type_name: max_active_version} under the content-addressed snapshot id; return it.
    Because the id is derived from the same contents, ON CONFLICT re-writes identical contents
    (no cross-state overwrite)."""
    snapshot_id = registry.snapshot_version()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO registry_snapshots (snapshot_id, registry, contents)
            VALUES (%s, 'events', %s)
            ON CONFLICT (snapshot_id)
            DO UPDATE SET contents = EXCLUDED.contents, captured_at = now()
            """,
            (snapshot_id, Jsonb(registry.max_active_versions())),
        )
    return snapshot_id


def load_registry_snapshot(conn: DbConn, snapshot_id: str) -> dict[str, int]:
    """Resolve a pinned snapshot id back to its {type_name: schema_version} map so a replay can
    drive upcast-on-read deterministically (§3.3/§8). Raises SchemaValidationError if unknown."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT contents FROM registry_snapshots WHERE snapshot_id = %s",
            (snapshot_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise SchemaValidationError(f"unknown registry snapshot {snapshot_id!r}")
    return {str(k): int(v) for k, v in row["contents"].items()}


def hydrate_event_registry(conn: DbConn) -> EventSchemaRegistry:
    """Reconstitute the process-global registry singleton's SCHEMAS from event_type_registry
    (resets then reloads), so a fresh process can validate/append without re-declaring every
    schema by hand. Upcasters are code: they are re-registered at import, not hydrated."""
    reset_event_registry()
    reg = event_registry()
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT type_name, schema_version, json_schema, owner, status "
            "FROM event_type_registry ORDER BY type_name, schema_version"
        )
        rows = cur.fetchall()
    for r in rows:
        reg.register_schema(
            r["type_name"], r["schema_version"], r["json_schema"], r["owner"],
            status=r["status"],
        )
    return reg
```

> `SchemaValidationError` is already imported at the top of `registry.py` (Task 4); `event_registry`
> / `reset_event_registry` are module functions defined in Task 4.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_registry_snapshot.py tests/events/test_registry_hydrate.py -q`
Expected: PASS (10 passed: 8 snapshot + 2 hydrate).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/registry.py tests/events/test_registry_snapshot.py tests/events/test_registry_hydrate.py
git commit -m "feat(sp0-01): content-addressed snapshot id, snapshot read-path, registry hydration"
```

---

### Task 9: append_event — global_seq, stream_version, registry-validated insert

**Files:**
- Create: `src/featuregen/events/store.py`
- Test: `tests/events/test_append_event.py`

**Interfaces:**
- Consumes: `NewEvent`, `EventEnvelope`, `DbConn` (Task 1); `event_registry()` + `validate`/`assert_writable` (Tasks 4/7); serde (Task 3); `events` table + `global_seq_seq` (Task 2).
- Produces: `append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope` (allocates `global_seq`+`event_id`, sets `stream_version = expected_version + 1`, validates payload against the registry, inserts into `events`). The OCC conflict path (`ConcurrencyError` on stale/ahead-of-head `expected_version`, savepoint isolation) is completed in **Task 10**.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_append_event.py
from __future__ import annotations

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event


def _idv() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("ds",),
    )


def _prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
    )


def _new(run_id: str, payload: dict) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_STARTED",
        schema_version=1,
        payload=payload,
        actor=_idv(),
        provenance=_prov(),
        run_id=run_id,
    )


def test_append_allocates_seq_and_stream_version(conn):
    event_registry().register_schema(
        "RUN_STARTED", 1, {"type": "object"}, owner="sp0"
    )
    env = append_event(conn, _new("run_a", {}), expected_version=0, table_version=1)
    assert env.stream_version == 1
    assert env.global_seq >= 1
    assert env.event_id.startswith("evt_")
    assert env.run_id == "run_a"


def test_append_increments_stream_version_and_global_seq(conn):
    event_registry().register_schema(
        "RUN_STARTED", 1, {"type": "object"}, owner="sp0"
    )
    first = append_event(conn, _new("run_b", {}), expected_version=0, table_version=1)
    second = append_event(conn, _new("run_b", {}), expected_version=1, table_version=1)
    assert second.stream_version == 2
    assert second.global_seq > first.global_seq


def test_append_validates_payload_against_registry(conn):
    import pytest

    from featuregen.contracts import SchemaValidationError

    event_registry().register_schema(
        "RUN_STARTED",
        1,
        {"type": "object", "required": ["needed"], "properties": {"needed": {"type": "string"}}},
        owner="sp0",
    )
    with pytest.raises(SchemaValidationError):
        append_event(conn, _new("run_c", {}), expected_version=0, table_version=1)


def test_append_blocks_writes_to_deprecated_schema(conn):
    import pytest

    from featuregen.contracts import SchemaValidationError

    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="sp0")
    event_registry().set_status("RUN_STARTED", 1, "deprecated")
    with pytest.raises(SchemaValidationError):
        append_event(conn, _new("run_d", {}), expected_version=0, table_version=1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_append_event.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.events.store'`.

- [ ] **Step 3: Write minimal implementation**

> This task implements the happy-path append (allocation + registry validation + insert). The
> optimistic-concurrency guard (pre-check + savepoint + `ConcurrencyError`) is added in **Task 10**,
> which is where its failing test drives that behavior in. Task 9 deliberately does **not** catch
> conflicts yet.

```python
# src/featuregen/events/store.py
from __future__ import annotations

import datetime as _dt

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from ulid import ULID

from featuregen.contracts import DbConn, EventEnvelope, NewEvent
from featuregen.events.registry import event_registry
from featuregen.events.serde import identity_to_jsonb, provenance_to_jsonb

_INSERT = """
INSERT INTO events (
    event_id, aggregate, aggregate_id, stream_version,
    request_id, feature_id, run_id, type, schema_version, table_version,
    actor, payload, provenance, caused_by, occurred_at
) VALUES (
    %(event_id)s, %(aggregate)s, %(aggregate_id)s, %(stream_version)s,
    %(request_id)s, %(feature_id)s, %(run_id)s, %(type)s, %(schema_version)s, %(table_version)s,
    %(actor)s, %(payload)s, %(provenance)s, %(caused_by)s, %(occurred_at)s
)
RETURNING global_seq, recorded_at
"""


def append_event(
    conn: DbConn,
    new_event: NewEvent,
    *,
    expected_version: int,
    table_version: int,
) -> EventEnvelope:
    """Append one event inside the caller's OPEN transaction (§5.1). Allocates global_seq +
    event_id and sets stream_version = expected_version + 1. (OCC conflict handling is added
    in Task 10.) Validates payload against the registry before insert."""
    registry = event_registry()
    registry.assert_writable(new_event.type, new_event.schema_version)
    registry.validate(new_event.type, new_event.schema_version, new_event.payload)

    event_id = f"evt_{ULID()}"
    stream_version = expected_version + 1
    occurred_at = new_event.occurred_at or _dt.datetime.now(_dt.timezone.utc)
    payload = dict(new_event.payload)
    params = {
        "event_id": event_id,
        "aggregate": new_event.aggregate,
        "aggregate_id": new_event.aggregate_id,
        "stream_version": stream_version,
        "request_id": new_event.request_id,
        "feature_id": new_event.feature_id,
        "run_id": new_event.run_id,
        "type": new_event.type,
        "schema_version": new_event.schema_version,
        "table_version": table_version,
        "actor": Jsonb(identity_to_jsonb(new_event.actor)),
        "payload": Jsonb(payload),
        "provenance": Jsonb(provenance_to_jsonb(new_event.provenance)),
        "caused_by": new_event.caused_by,
        "occurred_at": occurred_at,
    }
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_INSERT, params)
        row = cur.fetchone()

    return EventEnvelope(
        event_id=event_id,
        global_seq=row["global_seq"],
        aggregate=new_event.aggregate,
        aggregate_id=new_event.aggregate_id,
        stream_version=stream_version,
        type=new_event.type,
        schema_version=new_event.schema_version,
        table_version=table_version,
        actor=new_event.actor,
        payload=payload,
        provenance=new_event.provenance,
        occurred_at=occurred_at,
        recorded_at=row["recorded_at"],
        request_id=new_event.request_id,
        feature_id=new_event.feature_id,
        run_id=new_event.run_id,
        caused_by=new_event.caused_by,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_append_event.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/store.py tests/events/test_append_event.py
git commit -m "feat(sp0-01): append_event with global_seq + registry-validated insert"
```

---

### Task 10: Optimistic concurrency guard (stale, ahead-of-head, concurrent race)

**Files:**
- Modify: `src/featuregen/events/store.py` (add the OCC guard to `append_event`)
- Test: `tests/events/test_optimistic_concurrency.py`

**Interfaces:**
- Consumes: `append_event` (Task 9), `ConcurrencyError` (Task 1); `events` table (Task 2).
- Produces: `append_event` now enforces optimistic concurrency completely — it raises `ConcurrencyError` when `expected_version` does not equal the stream's current head, covering BOTH the **stale** case (`expected_version` < head) AND the **ahead-of-head** case (`expected_version` > head, which would otherwise silently insert a `stream_version` gap). A concurrent racer that loses the `UNIQUE (aggregate, aggregate_id, stream_version)` write is also mapped to `ConcurrencyError` via a savepoint, leaving the connection usable for a correct retry.

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_optimistic_concurrency.py
from __future__ import annotations

import pytest

from featuregen.contracts import ConcurrencyError, IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event


def _new(run_id: str) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type="RUN_STARTED",
        schema_version=1,
        payload={},
        actor=IdentityEnvelope(
            subject="user:raj",
            actor_kind="human",
            authenticated=True,
            auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=run_id,
    )


def test_stale_expected_version_raises_concurrency_error(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="sp0")
    append_event(conn, _new("run_x"), expected_version=0, table_version=1)
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_x"), expected_version=0, table_version=1)


def test_ahead_of_head_expected_version_raises_concurrency_error(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="sp0")
    append_event(conn, _new("run_z"), expected_version=0, table_version=1)
    # expected_version GREATER than the current head (1) must NOT silently insert a
    # stream_version gap; it must raise ConcurrencyError.
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_z"), expected_version=5, table_version=1)


def test_connection_usable_after_conflict_and_correct_retry_succeeds(conn):
    event_registry().register_schema("RUN_STARTED", 1, {"type": "object"}, owner="sp0")
    append_event(conn, _new("run_y"), expected_version=0, table_version=1)
    with pytest.raises(ConcurrencyError):
        append_event(conn, _new("run_y"), expected_version=0, table_version=1)
    # the conflict did not poison the transaction: a correct retry still succeeds.
    retried = append_event(conn, _new("run_y"), expected_version=1, table_version=1)
    assert retried.stream_version == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_optimistic_concurrency.py -q`
Expected: FAIL — Task 9's happy-path `append_event` has no OCC guard, so the stale re-append raises a raw `psycopg.errors.UniqueViolation` (not `ConcurrencyError`) and poisons the transaction, and the ahead-of-head append inserts a `stream_version` gap with no error at all. All three tests fail.

- [ ] **Step 3: Write minimal implementation**

Replace the body of `append_event` in `src/featuregen/events/store.py` with the OCC-complete version (add `from psycopg.errors import UniqueViolation` and `ConcurrencyError` to the imports):

```python
from psycopg.errors import UniqueViolation  # add to imports

from featuregen.contracts import ConcurrencyError, DbConn, EventEnvelope, NewEvent  # add ConcurrencyError


def append_event(
    conn: DbConn,
    new_event: NewEvent,
    *,
    expected_version: int,
    table_version: int,
) -> EventEnvelope:
    """Append one event inside the caller's OPEN transaction (§5.1). Sets
    stream_version = expected_version + 1. Raises ConcurrencyError if the stream is not exactly
    at expected_version (stale OR ahead-of-head), and maps a lost UNIQUE race to ConcurrencyError
    via a savepoint so the caller's transaction stays usable. Validates payload first."""
    registry = event_registry()
    registry.assert_writable(new_event.type, new_event.schema_version)
    registry.validate(new_event.type, new_event.schema_version, new_event.payload)

    # OCC pre-check: the stream must currently be EXACTLY at expected_version. This rejects
    # both stale (current > expected) and ahead-of-head (current < expected, gap) without
    # touching the connection's transaction state.
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT coalesce(max(stream_version), 0) AS v FROM events "
            "WHERE aggregate = %s AND aggregate_id = %s",
            (new_event.aggregate, new_event.aggregate_id),
        )
        current = cur.fetchone()["v"]
    if current != expected_version:
        raise ConcurrencyError(
            f"{new_event.aggregate}:{new_event.aggregate_id} at stream_version {current}, "
            f"expected {expected_version}"
        )

    event_id = f"evt_{ULID()}"
    stream_version = expected_version + 1
    occurred_at = new_event.occurred_at or _dt.datetime.now(_dt.timezone.utc)
    payload = dict(new_event.payload)
    params = {
        "event_id": event_id,
        "aggregate": new_event.aggregate,
        "aggregate_id": new_event.aggregate_id,
        "stream_version": stream_version,
        "request_id": new_event.request_id,
        "feature_id": new_event.feature_id,
        "run_id": new_event.run_id,
        "type": new_event.type,
        "schema_version": new_event.schema_version,
        "table_version": table_version,
        "actor": Jsonb(identity_to_jsonb(new_event.actor)),
        "payload": Jsonb(payload),
        "provenance": Jsonb(provenance_to_jsonb(new_event.provenance)),
        "caused_by": new_event.caused_by,
        "occurred_at": occurred_at,
    }
    try:
        with conn.transaction():  # savepoint: a concurrent racer that wins the UNIQUE keeps
            with conn.cursor(row_factory=dict_row) as cur:  # THIS connection usable
                cur.execute(_INSERT, params)
                row = cur.fetchone()
    except UniqueViolation as exc:
        if "events_optimistic_concurrency" in str(exc):
            raise ConcurrencyError(
                f"{new_event.aggregate}:{new_event.aggregate_id} lost a concurrent append at "
                f"expected_version {expected_version}"
            ) from exc
        raise

    return EventEnvelope(
        event_id=event_id,
        global_seq=row["global_seq"],
        aggregate=new_event.aggregate,
        aggregate_id=new_event.aggregate_id,
        stream_version=stream_version,
        type=new_event.type,
        schema_version=new_event.schema_version,
        table_version=table_version,
        actor=new_event.actor,
        payload=payload,
        provenance=new_event.provenance,
        occurred_at=occurred_at,
        recorded_at=row["recorded_at"],
        request_id=new_event.request_id,
        feature_id=new_event.feature_id,
        run_id=new_event.run_id,
        caused_by=new_event.caused_by,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_optimistic_concurrency.py tests/events/test_append_event.py -q`
Expected: PASS (3 OCC tests + 4 Task 9 append tests = 7 passed; Task 9's happy-path tests still pass with the guard in place).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/store.py tests/events/test_optimistic_concurrency.py
git commit -m "feat(sp0-01): OCC guard — stale + ahead-of-head + concurrent-race ConcurrencyError"
```

---

### Task 11: load_stream — ordered replay, as-of, upcast-on-read

**Files:**
- Modify: `src/featuregen/events/store.py` (add `load_stream`)
- Test: `tests/events/test_load_stream.py`

**Interfaces:**
- Consumes: `EventEnvelope`, `DbConn` (Task 1); `row_to_event` (Task 3); `event_registry().upcast` (Task 5); `persist_registry_snapshot`/`load_registry_snapshot` (Task 8); `events` table (Task 2); `append_event` (Task 9).
- Produces: `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None) -> list[EventEnvelope]` (ordered by `stream_version`; `upto_seq` filters `global_seq <= upto_seq`; `expected={type: schema_version}` upcasts each matching event on read). This task also wires the §3.3 pinned-snapshot determinism guarantee end-to-end: a run resolves its provenance-pinned `event_registry_snapshot` via `load_registry_snapshot` into the `{type: version}` map and passes it as `expected=`, so the write-side snapshot is actually consumed on the read side (not just stamped into provenance).

- [ ] **Step 1: Write the failing test**

```python
# tests/events/test_load_stream.py
from __future__ import annotations

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import (
    event_registry,
    load_registry_snapshot,
    persist_registry_snapshot,
)
from featuregen.events.store import append_event, load_stream


def _new(run_id: str, type_: str, payload: dict) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type=type_,
        schema_version=1,
        payload=payload,
        actor=IdentityEnvelope(
            subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=run_id,
    )


def test_load_stream_orders_by_stream_version(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    append_event(conn, _new("r1", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r1", "E", {"n": 2}), expected_version=1, table_version=1)
    stream = load_stream(conn, "run", "r1")
    assert [e.stream_version for e in stream] == [1, 2]
    assert [e.payload["n"] for e in stream] == [1, 2]


def test_load_stream_upto_seq_filters(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    a = append_event(conn, _new("r2", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r2", "E", {"n": 2}), expected_version=1, table_version=1)
    stream = load_stream(conn, "run", "r2", upto_seq=a.global_seq)
    assert [e.payload["n"] for e in stream] == [1]


def test_load_stream_upcasts_to_expected_version(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    event_registry().register_schema("E", 2, {"type": "object"}, owner="o")
    event_registry().register_upcaster("E", 1, 2, lambda b: {**b, "added_v2": True})
    append_event(conn, _new("r3", "E", {"n": 1}), expected_version=0, table_version=1)
    stream = load_stream(conn, "run", "r3", expected={"E": 2})
    assert stream[0].schema_version == 2
    assert stream[0].payload == {"n": 1, "added_v2": True}


def test_load_stream_only_returns_requested_aggregate(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    append_event(conn, _new("r4", "E", {"n": 1}), expected_version=0, table_version=1)
    append_event(conn, _new("r5", "E", {"n": 9}), expected_version=0, table_version=1)
    assert [e.run_id for e in load_stream(conn, "run", "r4")] == ["r4"]


def test_load_stream_upcasts_using_a_pinned_snapshot(conn):
    # §3.3 determinism end-to-end: a v1 event is written, the registry is snapshotted, and a
    # later replay resolves the PINNED snapshot id back to {type: version} to drive upcast-on-read
    # — i.e. the write-side snapshot is actually consumed, not merely stamped into provenance.
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    event_registry().register_schema("E", 2, {"type": "object"}, owner="o")
    event_registry().register_upcaster("E", 1, 2, lambda b: {**b, "added_v2": True})
    append_event(conn, _new("r6", "E", {"n": 1}), expected_version=0, table_version=1)

    snapshot_id = persist_registry_snapshot(conn, event_registry())  # pins {"E": 2}
    expected = load_registry_snapshot(conn, snapshot_id)             # read path resolves it back
    assert expected == {"E": 2}

    stream = load_stream(conn, "run", "r6", expected=expected)
    assert stream[0].schema_version == 2
    assert stream[0].payload == {"n": 1, "added_v2": True}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/events/test_load_stream.py -q`
Expected: FAIL — `ImportError: cannot import name 'load_stream'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/featuregen/events/store.py` (add `from dataclasses import replace` and `from typing import Mapping, Optional` to the imports, and `from featuregen.events.serde import row_to_event`):

```python
def load_stream(
    conn: DbConn,
    aggregate: str,
    aggregate_id: str,
    *,
    upto_seq: Optional[int] = None,
    expected: Optional[Mapping[str, int]] = None,
) -> list[EventEnvelope]:
    """Load one aggregate instance's stream in stream_version order, upcasting each event
    to the consumer's expected schema_version via the registry (§3.3)."""
    sql = (
        "SELECT * FROM events "
        "WHERE aggregate = %(aggregate)s AND aggregate_id = %(aggregate_id)s"
    )
    params: dict[str, object] = {"aggregate": aggregate, "aggregate_id": aggregate_id}
    if upto_seq is not None:
        sql += " AND global_seq <= %(upto_seq)s"
        params["upto_seq"] = upto_seq
    sql += " ORDER BY stream_version ASC"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    registry = event_registry()
    out: list[EventEnvelope] = []
    for row in rows:
        event = row_to_event(row)
        if expected and event.type in expected:
            target = expected[event.type]
            if target != event.schema_version:
                upcast_payload = registry.upcast(
                    event.type, event.payload, event.schema_version, target
                )
                event = replace(event, payload=dict(upcast_payload), schema_version=target)
        out.append(event)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/events/test_load_stream.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/events/store.py tests/events/test_load_stream.py
git commit -m "feat(sp0-01): load_stream ordered replay + as-of + upcast-on-read"
```

---

### Task 12: Projection runner — apply, checkpoint, lag, as-of

**Files:**
- Create: `src/featuregen/projections/__init__.py`
- Create: `src/featuregen/projections/runner.py`
- Test: `tests/projections/test_run_projection.py`

**Interfaces:**
- Consumes: `Projection` Protocol, `DbConn`, `ProjectionApplyError` (Task 1); `row_to_event` (Task 3); `events` + `projection_checkpoints` (Task 2); `append_event` (Task 9).
- Produces: `run_projection(conn, projection, *, batch=500) -> int` (happy-path apply + checkpoint advance here; the §3.6 fail-closed/analytics branches are completed in Task 13); `projection_lag(conn, name) -> int` (live head − checkpoint); `read_as_of(conn, name) -> int` (the `global_seq` the projection is current as-of); internal `_ensure_checkpoint(conn, name, is_analytics)`, `_head_seq(conn) -> int`.

- [ ] **Step 1: Write the failing test**

```python
# tests/projections/test_run_projection.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import projection_lag, read_as_of, run_projection


class CountingProjection:
    """A simple state-bearing projection that counts events into a temp table."""

    name = "counter"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE counter_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO counter_state (global_seq) VALUES (%s)", (event.global_seq,)
            )


def _seed(conn, n: int) -> None:
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i in range(n):
        append_event(
            conn,
            NewEvent(
                aggregate="run",
                aggregate_id="r",
                type="E",
                schema_version=1,
                payload={"i": i},
                actor=IdentityEnvelope(
                    subject="u", actor_kind="human", authenticated=True,
                    auth_method="oidc", role_claims=(),
                ),
                provenance=ProvenanceEnvelope(
                    artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
                ),
                run_id="r",
            ),
            expected_version=i,
            table_version=1,
        )


def _make_counter_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE counter_state (global_seq bigint)")


def test_run_projection_applies_and_advances_checkpoint(conn):
    _make_counter_table(conn)
    _seed(conn, 3)
    applied = run_projection(conn, CountingProjection())
    assert applied == 3
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM counter_state")
        assert cur.fetchone()["n"] == 3
        cur.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name='counter'")
        assert cur.fetchone()["checkpoint_seq"] > 0


def test_second_run_applies_only_new_events(conn):
    _make_counter_table(conn)
    _seed(conn, 2)
    assert run_projection(conn, CountingProjection()) == 2
    assert run_projection(conn, CountingProjection()) == 0  # nothing new


def test_lag_and_as_of_track_checkpoint(conn):
    _make_counter_table(conn)
    _seed(conn, 2)
    proj = CountingProjection()
    run_projection(conn, proj)
    assert projection_lag(conn, "counter") == 0
    assert read_as_of(conn, "counter") > 0
    # append more without projecting -> lag grows.
    _seed_more = append_event(
        conn,
        NewEvent(
            aggregate="run", aggregate_id="r", type="E", schema_version=1, payload={},
            actor=IdentityEnvelope(
                subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                role_claims=(),
            ),
            provenance=ProvenanceEnvelope(
                artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
            ),
            run_id="r",
        ),
        expected_version=2,
        table_version=1,
    )
    assert projection_lag(conn, "counter") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/projections/test_run_projection.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.projections.runner'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/projections/__init__.py
"""SP-0 projections."""
```

```python
# src/featuregen/projections/runner.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import DbConn, Projection, ProjectionApplyError
from featuregen.events.serde import row_to_event


def _ensure_checkpoint(conn: DbConn, name: str, is_analytics: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_checkpoints (projection_name, is_analytics)
            VALUES (%s, %s)
            ON CONFLICT (projection_name) DO NOTHING
            """,
            (name, is_analytics),
        )


def _head_seq(conn: DbConn) -> int:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT max(global_seq) AS h FROM events")
        row = cur.fetchone()
    return row["h"] or 0


def run_projection(conn: DbConn, projection: Projection, *, batch: int = 500) -> int:
    """Consume events with global_seq > checkpoint_seq in order, calling apply(); advance the
    checkpoint to the last applied event. Returns the count applied.

    NOTE: this Task-12 version handles the happy path only. The §3.6 fail-closed degraded-halt and
    analytics fail-open branches (and the `projection_degraded` marking) are added in Task 13,
    where a failing test drives them in."""
    _ensure_checkpoint(conn, projection.name, projection.is_analytics)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints "
            "WHERE projection_name = %s FOR UPDATE",
            (projection.name,),
        )
        checkpoint = cur.fetchone()["checkpoint_seq"]
        cur.execute(
            "SELECT * FROM events WHERE global_seq > %s ORDER BY global_seq ASC LIMIT %s",
            (checkpoint, batch),
        )
        rows = cur.fetchall()

    applied = 0
    last_seq = checkpoint
    for row in rows:
        event = row_to_event(row)
        projection.apply(conn, event)
        last_seq = event.global_seq
        applied += 1

    head = _head_seq(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projection_checkpoints "
            "SET checkpoint_seq = %s, head_seq = %s, updated_at = now() "
            "WHERE projection_name = %s",
            (last_seq, head, projection.name),
        )
    return applied


def projection_lag(conn: DbConn, name: str) -> int:
    """Live head_seq - checkpoint_seq for the named projection."""
    head = _head_seq(conn)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        return head
    return head - row["checkpoint_seq"]


def read_as_of(conn: DbConn, name: str) -> int:
    """The global_seq the projection's data is current as-of (its checkpoint)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name = %s",
            (name,),
        )
        row = cur.fetchone()
    return 0 if row is None else row["checkpoint_seq"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/projections/test_run_projection.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/projections/__init__.py src/featuregen/projections/runner.py tests/projections/test_run_projection.py
git commit -m "feat(sp0-01): projection runner apply+checkpoint+lag+as-of"
```

---

### Task 13: Fail-closed degraded handling vs analytics fail-open

**Files:**
- Modify: `src/featuregen/projections/runner.py` (add the fail-closed/analytics branches + `_mark_degraded` to `run_projection`)
- Test: `tests/projections/test_fail_closed.py`

**Interfaces:**
- Consumes: `run_projection`, `projection_lag` (Task 12); `ProjectionApplyError` (Task 1); `append_event` (Task 9); `projection_degraded` table (Task 2).
- Produces: the §3.6 degraded semantics on `run_projection` — every `projection.apply` is wrapped in a **SAVEPOINT**; a non-analytics (fail-closed) `ProjectionApplyError` (a) triggers a `ROLLBACK TO SAVEPOINT` that **discards any partial writes the apply body made before raising** (so no partial projection state survives), (b) records the affected aggregate in the Phase-01-owned `projection_degraded` ledger in a **separate statement after the rollback** using the **carried** `ProjectionApplyError.aggregate`/`aggregate_id`/`reason` + the poison event id (so the runner itself realizes "mark the affected aggregate degraded", honoring the shared `run_projection` docstring and making the carried payload load-bearing), and (c) HALTS without advancing past the poison event. An analytics (fail-open) `ProjectionApplyError` is wrapped in a savepoint, skipped, and the projection continues to head. Internal helper `_mark_degraded(conn, projection_name, exc, event)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/projections/test_fail_closed.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import (
    IdentityEnvelope,
    NewEvent,
    ProjectionApplyError,
    ProvenanceEnvelope,
)
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import projection_lag, run_projection


def _append(conn, run_id, version, payload):
    return append_event(
        conn,
        NewEvent(
            aggregate="run", aggregate_id=run_id, type="E", schema_version=1, payload=payload,
            actor=IdentityEnvelope(
                subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                role_claims=(),
            ),
            provenance=ProvenanceEnvelope(
                artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
            ),
            run_id=run_id,
        ),
        expected_version=version,
        table_version=1,
    )


class FailClosedProjection:
    name = "fc"
    is_analytics = False

    def __init__(self, poison_seq: int) -> None:
        self.poison_seq = poison_seq

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE fc_applied")
            cur.execute("TRUNCATE fc_degraded")

    def apply(self, conn, event) -> None:
        if event.global_seq == self.poison_seq:
            # Write PARTIAL projection state (into BOTH temp tables), THEN signal fail-closed.
            # Under the runner's SAVEPOINT wrapping these writes MUST be discarded by ROLLBACK
            # TO SAVEPOINT — only the runner's projection_degraded marker may survive.
            with conn.cursor() as cur:
                cur.execute("INSERT INTO fc_applied (global_seq) VALUES (%s)", (event.global_seq,))
                cur.execute(
                    "INSERT INTO fc_degraded (run_id, reason, at_seq) VALUES (%s, %s, %s)",
                    (event.run_id, "unappliable", event.global_seq),
                )
            raise ProjectionApplyError("run", event.run_id, "unappliable")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO fc_applied (global_seq) VALUES (%s)", (event.global_seq,))


class AnalyticsProjection:
    name = "an"
    is_analytics = True

    def __init__(self, poison_seq: int) -> None:
        self.poison_seq = poison_seq

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE an_applied")

    def apply(self, conn, event) -> None:
        if event.global_seq == self.poison_seq:
            raise ProjectionApplyError("run", event.run_id, "skip me")
        with conn.cursor() as cur:
            cur.execute("INSERT INTO an_applied (global_seq) VALUES (%s)", (event.global_seq,))


def test_fail_closed_halts_and_persists_degraded_marker(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE fc_applied (global_seq bigint)")
        cur.execute("CREATE TEMP TABLE fc_degraded (run_id text, reason text, at_seq bigint)")
    e1 = _append(conn, "r", 0, {})
    poison = _append(conn, "r", 1, {})
    _append(conn, "r", 2, {})  # after the poison event

    proj = FailClosedProjection(poison_seq=poison.global_seq)
    applied = run_projection(conn, proj)
    assert applied == 1  # only the pre-poison event

    with conn.cursor(row_factory=dict_row) as cur:
        # The poison apply wrote PARTIAL state into BOTH fc_applied and fc_degraded before raising;
        # the runner's SAVEPOINT + ROLLBACK TO SAVEPOINT discarded ALL of it — fc_applied holds
        # ONLY the pre-poison event and fc_degraded is empty (NO partial projection state survives).
        cur.execute("SELECT global_seq FROM fc_applied ORDER BY global_seq")
        assert [r["global_seq"] for r in cur.fetchall()] == [e1.global_seq]
        cur.execute("SELECT count(*) AS n FROM fc_degraded")
        assert cur.fetchone()["n"] == 0  # the apply body's own partial marker was rolled back
        # The ONLY surviving degraded record is the one run_projection itself wrote, in a SEPARATE
        # statement AFTER the rollback, into the generic ledger — using the CARRIED
        # ProjectionApplyError payload (aggregate/aggregate_id/reason) + the poison event.
        cur.execute(
            "SELECT aggregate, aggregate_id, reason, poison_seq FROM projection_degraded "
            "WHERE projection_name = 'fc'"
        )
        deg = cur.fetchone()
        assert (deg["aggregate"], deg["aggregate_id"], deg["reason"]) == ("run", "r", "unappliable")
        assert deg["poison_seq"] == poison.global_seq
        cur.execute("SELECT checkpoint_seq FROM projection_checkpoints WHERE projection_name='fc'")
        assert cur.fetchone()["checkpoint_seq"] == e1.global_seq  # did not advance past poison

    # stuck: a second run does not advance (lag stays > 0).
    assert run_projection(conn, proj) == 0
    assert projection_lag(conn, "fc") > 0


def test_analytics_skips_poison_and_continues(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE an_applied (global_seq bigint)")
    a1 = _append(conn, "r", 0, {})
    poison = _append(conn, "r", 1, {})
    a3 = _append(conn, "r", 2, {})

    proj = AnalyticsProjection(poison_seq=poison.global_seq)
    applied = run_projection(conn, proj)
    assert applied == 2  # poison skipped, others applied

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq FROM an_applied ORDER BY global_seq")
        assert [r["global_seq"] for r in cur.fetchall()] == [a1.global_seq, a3.global_seq]
    assert projection_lag(conn, "an") == 0  # advanced to head despite the skip
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/projections/test_fail_closed.py -q`
Expected: FAIL — Task 12's `run_projection` does not catch `ProjectionApplyError`, so the poison `apply` propagates straight out of `run_projection`. `test_fail_closed_halts_and_persists_degraded_marker` errors (uncaught `ProjectionApplyError`, no `projection_degraded` row) and `test_analytics_skips_poison_and_continues` errors (poison not skipped). Both fail.

- [ ] **Step 3: Write minimal implementation**

Add `_mark_degraded` and replace the apply loop in `run_projection` (in `src/featuregen/projections/runner.py`). Every `projection.apply` is wrapped in a `SAVEPOINT proj_apply`. A fail-closed projection raises a *Python* `ProjectionApplyError` (not a SQL error), so the connection's transaction is still valid: the runner issues `ROLLBACK TO SAVEPOINT proj_apply` to discard **any partial writes the apply body made before raising** (no partial projection state survives), then writes the surviving degraded marker to `projection_degraded` in a separate statement:

```python
def _mark_degraded(conn: DbConn, projection_name: str, exc: ProjectionApplyError, event) -> None:
    """Record the affected aggregate in the generic degraded ledger from the CARRIED
    ProjectionApplyError payload (§3.6). Idempotent under re-runs of the same poison event."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_degraded
                (projection_name, aggregate, aggregate_id, reason, poison_event_id, poison_seq)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (projection_name, aggregate, aggregate_id)
            DO UPDATE SET reason = EXCLUDED.reason,
                          poison_event_id = EXCLUDED.poison_event_id,
                          poison_seq = EXCLUDED.poison_seq,
                          degraded_at = now()
            """,
            (projection_name, exc.aggregate, exc.aggregate_id, exc.reason,
             event.event_id, event.global_seq),
        )
```

Replace the `for row in rows:` apply loop in `run_projection` with:

```python
    applied = 0
    last_seq = checkpoint
    for row in rows:
        event = row_to_event(row)
        if projection.is_analytics:
            try:
                with conn.transaction():  # savepoint: discard the poison event's partial writes
                    projection.apply(conn, event)
            except ProjectionApplyError:
                last_seq = event.global_seq  # fail open: record the skip and keep going
                continue
            last_seq = event.global_seq
            applied += 1
        else:
            with conn.cursor() as cur:
                cur.execute("SAVEPOINT proj_apply")
            try:
                projection.apply(conn, event)
            except ProjectionApplyError as exc:
                # Fail-closed (§3.6): discard ANY partial writes the apply body made before it
                # raised (ROLLBACK TO SAVEPOINT), so no partial projection state survives; then
                # mark the affected aggregate degraded from the carried payload in a SEPARATE
                # statement (this marker persists), and HALT without advancing past the poison.
                with conn.cursor() as cur:
                    cur.execute("ROLLBACK TO SAVEPOINT proj_apply")
                _mark_degraded(conn, projection.name, exc, event)
                break
            with conn.cursor() as cur:
                cur.execute("RELEASE SAVEPOINT proj_apply")
            last_seq = event.global_seq
            applied += 1
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/projections/test_fail_closed.py tests/projections/test_run_projection.py -q`
Expected: PASS (2 fail-closed tests + the Task 12 happy-path tests; the simplified runner's happy path is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/projections/runner.py tests/projections/test_fail_closed.py
git commit -m "feat(sp0-01): fail-closed degraded-halt + ledger marking vs analytics fail-open"
```

---

### Task 14: Deterministic rebuild from global_seq=0

**Files:**
- Modify: `src/featuregen/projections/runner.py` (add `rebuild_projection`)
- Test: `tests/projections/test_rebuild.py`

**Interfaces:**
- Consumes: `Projection`, `DbConn` (Task 1); `run_projection`, `_ensure_checkpoint` (Task 12).
- Produces: `rebuild_projection(conn, projection) -> None` (calls `projection.reset(conn)`, resets the checkpoint to 0, then replays from `global_seq=0` to head deterministically).

- [ ] **Step 1: Write the failing test**

```python
# tests/projections/test_rebuild.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.runner import rebuild_projection, run_projection


class SumProjection:
    name = "sum"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE sum_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sum_state (global_seq, n) VALUES (%s, %s)",
                (event.global_seq, event.payload["n"]),
            )


def _seed(conn, values):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i, v in enumerate(values):
        append_event(
            conn,
            NewEvent(
                aggregate="run", aggregate_id="r", type="E", schema_version=1,
                payload={"n": v},
                actor=IdentityEnvelope(
                    subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                    role_claims=(),
                ),
                provenance=ProvenanceEnvelope(
                    artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
                ),
                run_id="r",
            ),
            expected_version=i,
            table_version=1,
        )


def test_rebuild_reproduces_identical_state(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [1, 2, 3])
    proj = SumProjection()
    run_projection(conn, proj)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq, n FROM sum_state ORDER BY global_seq")
        before = cur.fetchall()

    rebuild_projection(conn, proj)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT global_seq, n FROM sum_state ORDER BY global_seq")
        after = cur.fetchall()
        cur.execute("SELECT count(*) AS n FROM sum_state")
        assert cur.fetchone()["n"] == 3  # reset cleared duplicates, replay re-added once
    assert after == before


def test_rebuild_resets_checkpoint_then_replays(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE sum_state (global_seq bigint, n int)")
    _seed(conn, [5, 6])
    proj = SumProjection()
    run_projection(conn, proj)
    rebuild_projection(conn, proj)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT checkpoint_seq, head_seq FROM projection_checkpoints WHERE projection_name='sum'")
        row = cur.fetchone()
    assert row["checkpoint_seq"] == row["head_seq"]  # fully caught up after rebuild
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/projections/test_rebuild.py -q`
Expected: FAIL — `ImportError: cannot import name 'rebuild_projection'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/featuregen/projections/runner.py`:

```python
def rebuild_projection(conn: DbConn, projection: Projection) -> None:
    """reset() then deterministically replay from global_seq=0 (§3.6)."""
    projection.reset(conn)
    _ensure_checkpoint(conn, projection.name, projection.is_analytics)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE projection_checkpoints SET checkpoint_seq = 0, head_seq = 0, updated_at = now() "
            "WHERE projection_name = %s",
            (projection.name,),
        )
    while run_projection(conn, projection) > 0:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/projections/test_rebuild.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/projections/runner.py tests/projections/test_rebuild.py
git commit -m "feat(sp0-01): deterministic projection rebuild from global_seq=0"
```

---

### Task 15: Parallel migration with atomic read-switch

**Files:**
- Create: `src/featuregen/projections/migration.py`
- Test: `tests/projections/test_migration.py`

**Interfaces:**
- Consumes: `Projection`, `DbConn` (Task 1); `rebuild_projection`, `projection_lag` (Tasks 12/14); `projection_active_alias` table (Task 2).
- Produces: `set_alias(conn, alias, projection_name) -> None`; `resolve_projection(conn, alias) -> str` (current projection behind an alias); `migrate_projection(conn, alias, new_projection) -> None` (build the new projection from `global_seq=0` in parallel while the old one still serves reads, then switch the alias atomically once caught up).

- [ ] **Step 1: Write the failing test**

```python
# tests/projections/test_migration.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.projections.migration import migrate_projection, resolve_projection, set_alias
from featuregen.projections.runner import projection_lag, run_projection


class V1Projection:
    name = "report_v1"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE report_v1_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO report_v1_state (n) VALUES (%s)", (event.payload["n"],))


class V2Projection:
    """New shape: stores doubled values."""

    name = "report_v2"
    is_analytics = False

    def reset(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE report_v2_state")

    def apply(self, conn, event) -> None:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO report_v2_state (n2) VALUES (%s)", (event.payload["n"] * 2,))


def _seed(conn, values):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    for i, v in enumerate(values):
        append_event(
            conn,
            NewEvent(
                aggregate="run", aggregate_id="r", type="E", schema_version=1, payload={"n": v},
                actor=IdentityEnvelope(
                    subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
                    role_claims=(),
                ),
                provenance=ProvenanceEnvelope(
                    artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
                ),
                run_id="r",
            ),
            expected_version=i,
            table_version=1,
        )


def test_migrate_builds_in_parallel_then_switches_alias(conn):
    with conn.cursor() as cur:
        cur.execute("CREATE TEMP TABLE report_v1_state (n int)")
        cur.execute("CREATE TEMP TABLE report_v2_state (n2 int)")
    _seed(conn, [1, 2, 3])

    set_alias(conn, "report", "report_v1")
    run_projection(conn, V1Projection())
    assert resolve_projection(conn, "report") == "report_v1"

    migrate_projection(conn, "report", V2Projection())

    # alias switched only after v2 caught up; v1 data still intact (parallel build).
    assert resolve_projection(conn, "report") == "report_v2"
    assert projection_lag(conn, "report_v2") == 0
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT n2 FROM report_v2_state ORDER BY n2")
        assert [r["n2"] for r in cur.fetchall()] == [2, 4, 6]
        cur.execute("SELECT count(*) AS n FROM report_v1_state")
        assert cur.fetchone()["n"] == 3  # old projection untouched during migration


def test_resolve_unknown_alias_returns_alias_itself(conn):
    assert resolve_projection(conn, "missing") == "missing"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/projections/test_migration.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.projections.migration'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/projections/migration.py
from __future__ import annotations

from psycopg.rows import dict_row

from featuregen.contracts import DbConn, Projection
from featuregen.projections.runner import _head_seq, projection_lag, rebuild_projection


def set_alias(conn: DbConn, alias: str, projection_name: str) -> None:
    """Point an alias at a projection (upsert)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO projection_active_alias (alias, projection_name)
            VALUES (%s, %s)
            ON CONFLICT (alias)
            DO UPDATE SET projection_name = EXCLUDED.projection_name, switched_at = now()
            """,
            (alias, projection_name),
        )


def resolve_projection(conn: DbConn, alias: str) -> str:
    """Return the projection currently behind an alias; if none, the alias itself."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT projection_name FROM projection_active_alias WHERE alias = %s",
            (alias,),
        )
        row = cur.fetchone()
    return alias if row is None else row["projection_name"]


def migrate_projection(conn: DbConn, alias: str, new_projection: Projection) -> None:
    """Build new_projection from global_seq=0 in parallel (the old one still serves reads),
    then switch the alias atomically once the new projection has caught up to head (§3.6)."""
    rebuild_projection(conn, new_projection)
    if projection_lag(conn, new_projection.name) != 0:
        raise RuntimeError(
            f"migration aborted: {new_projection.name} not caught up to head"
        )
    head = _head_seq(conn)
    with conn.cursor() as cur:
        # single-statement atomic read-switch.
        cur.execute(
            """
            INSERT INTO projection_active_alias (alias, projection_name, switched_seq, switched_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (alias)
            DO UPDATE SET projection_name = EXCLUDED.projection_name,
                          switched_seq = EXCLUDED.switched_seq,
                          switched_at = now()
            """,
            (alias, new_projection.name, head),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/projections/test_migration.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/projections/migration.py tests/projections/test_migration.py
git commit -m "feat(sp0-01): parallel projection migration + atomic read-switch"
```

---

### Task 16: Re-export core interfaces from `featuregen.contracts` + cross-stream monotonic global_seq + full-suite gate

**Files:**
- Modify: `src/featuregen/contracts/__init__.py` (lazily re-export `append_event`, `load_stream`, `run_projection`, `rebuild_projection`, `projection_lag`)
- Test: `tests/contracts/test_core_interface_reexports.py`
- Test: `tests/events/test_global_seq_monotonic.py`

**Interfaces:**
- Consumes: `append_event`, `load_stream` (Tasks 9–11); `run_projection`, `rebuild_projection`, `projection_lag` (Tasks 12–14); `global_seq_seq` (Task 2).
- Produces: the contract's promise that the core interface functions are importable from `featuregen.contracts` (the overview says these "live in `src/featuregen/contracts/` and are imported by every phase"). They are re-exported **lazily** via a module-level `__getattr__` (PEP 562) so importing `featuregen.contracts` does not eagerly pull in `featuregen.events.*` / `featuregen.projections.*` (which import `featuregen.contracts` themselves) — avoiding a circular import while still letting downstream phases write `from featuregen.contracts import append_event`.
- Regression guard (no new implementation): `global_seq` is strictly increasing and unique across appends in different aggregate streams (guaranteed by Task 2's `global_seq_seq` DEFAULT on `events`).

- [ ] **Step 1: Write the failing test**

The TDD subject of this task is the `featuregen.contracts` re-export. Write the failing test:

```python
# tests/contracts/test_core_interface_reexports.py
from __future__ import annotations


def test_core_interface_functions_importable_from_contracts():
    # The overview declares these AUTHORITATIVE functions live in featuregen.contracts and are
    # imported by every phase. Downstream phases must be able to import them from here.
    from featuregen.contracts import (
        append_event,
        load_stream,
        projection_lag,
        rebuild_projection,
        run_projection,
    )

    for fn in (append_event, load_stream, run_projection, rebuild_projection, projection_lag):
        assert callable(fn)


def test_reexported_functions_are_the_same_objects():
    import featuregen.contracts as contracts
    from featuregen.events.store import append_event as store_append
    from featuregen.projections.runner import run_projection as runner_run

    assert contracts.append_event is store_append
    assert contracts.run_projection is runner_run
```

Also add this cross-stream monotonicity **regression guard** (it passes immediately — the
guarantee is the `global_seq_seq` DEFAULT from Task 2 — so it is a standing invariant check, not
this task's red→green subject):

```python
# tests/events/test_global_seq_monotonic.py
from __future__ import annotations

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event


def _new(agg_id: str) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=agg_id,
        type="E",
        schema_version=1,
        payload={},
        actor=IdentityEnvelope(
            subject="u", actor_kind="human", authenticated=True, auth_method="oidc",
            role_claims=(),
        ),
        provenance=ProvenanceEnvelope(
            artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="t@1"
        ),
        run_id=agg_id,
    )


def test_global_seq_strictly_increases_across_distinct_streams(conn):
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    seqs = [
        append_event(conn, _new("run_p"), expected_version=0, table_version=1).global_seq,
        append_event(conn, _new("run_q"), expected_version=0, table_version=1).global_seq,
        append_event(conn, _new("run_p"), expected_version=1, table_version=1).global_seq,
    ]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == 3  # no duplicates across streams
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/contracts/test_core_interface_reexports.py -q`
Expected: FAIL — `ImportError: cannot import name 'append_event' from 'featuregen.contracts'` (the re-export is not wired yet). The monotonic regression guard already passes (the `global_seq_seq` DEFAULT provides it).

- [ ] **Step 3: Write minimal implementation**

Append a lazy re-export to the BOTTOM of `src/featuregen/contracts/__init__.py`. It MUST be a PEP-562
module `__getattr__` (not a top-level `from featuregen.events.store import ...`): `featuregen.events.*` and
`featuregen.projections.*` import `featuregen.contracts` at module load, so an eager import here would deadlock
the cycle. `__getattr__` defers the import to first attribute access, by which point all modules
are fully initialized:

```python
# ── Core interface functions (overview "Core interfaces"): re-exported so downstream phases can
# `from featuregen.contracts import append_event, ...`. Lazy (PEP 562) to avoid the import cycle with
# featuregen.events.* / featuregen.projections.*, which import THIS module.
_LAZY_EXPORTS = {
    "append_event": ("featuregen.events.store", "append_event"),
    "load_stream": ("featuregen.events.store", "load_stream"),
    "run_projection": ("featuregen.projections.runner", "run_projection"),
    "rebuild_projection": ("featuregen.projections.runner", "rebuild_projection"),
    "projection_lag": ("featuregen.projections.runner", "projection_lag"),
}


def __getattr__(name: str):
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module, attr = target
    return getattr(importlib.import_module(module), attr)


__all__ += list(_LAZY_EXPORTS)
```

- [ ] **Step 4: Run the re-export test, then the full Phase 01 suite**

Run: `python -m pytest tests/contracts/test_core_interface_reexports.py -q`
Expected: PASS (2 passed).

Run: `python -m pytest -q`
Expected: PASS — every Phase 01 test green (contracts, db, events, projections), including the
cross-stream monotonicity guard.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/contracts/__init__.py tests/contracts/test_core_interface_reexports.py tests/events/test_global_seq_monotonic.py
git commit -m "feat(sp0-01): re-export core interfaces from featuregen.contracts + cross-stream global_seq guard"
```

---

## Self-review (spec coverage)

- §3.2 event envelope + per-aggregate OCC (incl. the **ahead-of-head** gap case, not just stale) + monotonic `global_seq` → Tasks 1, 2, 9, 10, 16.
- §3.3 registry: validate, total/chained stepwise upcasters, backward-compat rule **actively enforced** (breaking bump → mandatory upcaster, load-time error via `assert_evolution_complete` wired into `persist_event_schemas`), deprecate/withdraw, content-addressed pinned snapshot (write side) **and** the snapshot read-path (`load_registry_snapshot` driving upcast-on-read) + durable persistence **and** hydration → Tasks 4, 5, 6, 7, 8; upcast-on-read incl. snapshot-pinned replay → Task 11.
- §3.6 projections: checkpoint, lag, as-of, fail-closed degraded (each apply is SAVEPOINT-wrapped; on a fail-closed `ProjectionApplyError` the runner rolls back the apply's partial writes, marks the affected aggregate in `projection_degraded` from the carried payload in a separate statement, then halts) vs analytics fail-open, deterministic rebuild, parallel migration + atomic read-switch → Tasks 12, 13, 14, 15.
- Contract integrity: shared `Handler`/`Projection` Protocols and `HandlerContext` carry the contract's typed signatures verbatim (no `Any` weakening) — Task 1; core interface functions importable from `featuregen.contracts` per the overview — Task 16.
- §12 coverage owned here: optimistic-concurrency conflict incl. ahead-of-head guard (Task 10), schema evolution incl. breaking change caught proactively at persist + deprecated/withdrawn readable + content-addressed pinned snapshot resolvable back to its `{type: version}` map (Tasks 5–8, 11), registry hydration on a fresh process (Task 8), `as-of`/lag reads (Tasks 11, 12), fail-closed/`degraded` ledger handling (Task 13).
