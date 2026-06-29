## Phase 06: Aggregates, identifiers, lifecycle commands & activation saga

> REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this phase task-by-task. Each task is TDD: failing test → watch it fail → minimal code → watch it pass → commit.

**Goal:** Implement the four-aggregate identifier model (`request_id`→`feature_id`→`run_id`→`feature_version_id`), the advisory concept-claim reservation, the §4.4 lifecycle command catalog behind a single `execute_command` entrypoint (idempotency + degraded-block + authz seam), and the approval→activation CAS saga (use-case-scoped active map, `ACTIVATION_CONFLICT`, experimental expiry, feature-lifecycle signals, `reopen_as_new_run`, `deprecate` consumer guard, `SOURCE_CHANGED_REVALIDATE`).

### Interfaces consumed from earlier phases (do not redefine)

- **Shared contract module** `src/featuregen/contracts/` (established by Phase 01) re-exports: `DbConn`, `IdentityEnvelope`, `ProvenanceEnvelope`, `NewEvent`, `EventEnvelope`, `ConcurrencyError`, `Command`, `CommandResult`, `Handler`, `HandlerResult`, `HandlerContext`, `Disposition`, `NewTimer`, `NewExternalCommand`, `NewActivation`, `SchemaRegistry`, `SchemaValidationError`. (`HandlerResult.activations: tuple[NewActivation, ...] = ()` and `NewActivation(feature_id, feature_version_id, use_case, base_feature_version_id, approval_type, expires_at=None, provenance=None)` are the contract's cross-aggregate-activation effect; Phase 06 declares them in the saga handler and applies them in `commit_step` — see Task 11.)
- **Event store** (Phase 01), importable as `from featuregen.eventstore import append_event, load_stream, event_registry`:
  - `append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope` — validates payload against `event_registry`, raises `ConcurrencyError` on stale `expected_version`.
  - `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None) -> list[EventEnvelope]`.
  - `event_registry` — the process-wide event `SchemaRegistry` instance that `append_event` validates against; exposes `register_schema(type_name, schema_version, json_schema, owner, *, status="active")` and `validate(type_name, schema_version, body)`. **Cross-phase note:** `event_registry` is a Phase-01 *implementation export*, not one of the shared Core-interface symbols (those define only the `SchemaRegistry` Protocol and `append_event`'s "validates against the event registry" prose). This phase depends on Phase 01 exposing this singleton from `featuregen.eventstore`; the §"Production wiring" note below registers Phase-06 schemas into it so runtime appends validate outside pytest.
- **Handler dispatch + registry** (Phase 04), importable as `from featuregen.runtime.handlers import HandlerRegistry` and `from featuregen.runtime.dispatch import process_one`:
  - `HandlerRegistry.register(handler: Handler) -> None` / `.get(name) -> Handler` — keyed by `Handler.name`; re-registering a name is a `ValueError`.
  - `process_one(conn, registry, *, owner, document_loader=...)` — claims one `queue` row, looks up `registry.get(queue.handler)`, builds a **run-scoped** `HandlerContext` from `payload["run_id"]` + `payload["event_id"]` (loaded from the **run** stream), runs `handler.handle(ctx)`, and on `Disposition.OK` calls `commit_step` (which appends `result.new_events` to the run stream, writes the ledger, and emits outbox rows). The activation saga (Task 11) relies on exactly this: its queue payload carries `run_id`+`event_id` of the run-stream `ACTIVATION_REQUESTED` event, and its handler returns **no run-stream events** — it is PURE with respect to persistence and instead DECLARES the cross-aggregate effect as `HandlerResult.activations`, which `commit_step` applies on the step-transaction connection (Task 11 extends `commit_step` for this).
- **Tables created by other phases (Phase 06 only references them):** `events` + `UNIQUE(aggregate,aggregate_id,stream_version)` (Phase 01); `run_workflow_state` (Phase 01 projection; read for the degraded-block, cleared by `resolve_degraded`); `queue` (Phase 04; the activation saga enqueues a `feature:{feature_id}`-partitioned `activate_version` row); `timers` (Phase 05; the saga schedules `experiment_expiry`, and forced `deprecate` schedules a `business_repair` grace timer).
- **Test fixtures (Phase 01 `tests/conftest.py`):** `db` — a function-scoped psycopg connection in an OPEN transaction, rolled back after each test, with every `src/featuregen/db/migrations/*.sql` applied in lexical order.

---

### File structure (this phase)

```
src/featuregen/
  db/migrations/
    0060_aggregates_lifecycle.sql      # Task 1: feature_versions, feature_active_versions,
                                       #         consumers, concept_claims, command_idempotency (verbatim DDL)
  aggregates/
    __init__.py
    ids.py                             # Task 2: ULID-style id minting + concept-key normalization
    events.py                          # Task 3: Phase-06 event types + register_phase06_event_types()
    bootstrap.py                       # Task 3: idempotent register_phase06_event_schemas() (production path)
    _append.py                         # Task 5: append() helper, current_version(), provenance_for()
    concept_claims.py                  # Task 5: claim_concept() (first-committed wins)
    request_aggregate.py               # Task 5/6/7: create_request, create_run, duplicate_of, select_candidate
    run_lifecycle.py                   # Task 8/9: reject/cancel/withdraw/park/unpark/reopen + fact/source signals + resolve_degraded
    feature_versions.py                # Task 10: mint_feature_version()
    activation.py                      # Task 11: apply_activation (CAS), on_run_approved (saga step 1),
                                       #          request_activation, ActivateVersionHandler, register_phase06_handlers, expiry
    consumers.py                       # Task 12: register/deregister_consumer, supersede, deprecate (+force quiesce/grace), finalize_deprecate, retier
    feature_lifecycle.py               # Task 13: raise_monitoring_alert/require_revalidation/record_outcome
    commands.py                        # Task 14: register_phase06_commands()
  commands/
    __init__.py
    registry.py                        # Task 4: register_command/get_command/clear_registry
    authz_seam.py                      # Task 4: CommandAuthorizer, AuthzDecision, register_command_authorizer
    api.py                             # Task 4: execute_command (idempotency + degraded + authz)
tests/featuregen/
  conftest.py                          # Task 4: session-autouse register Phase-06 event types
  _helpers.py                          # Task 4: make_actor(), make_cmd()
  commands/
    test_execute_command.py            # Task 4
  aggregates/
    test_ids.py                        # Task 2
    test_events.py                     # Task 3
    test_bootstrap.py                  # Task 3
    test_concept_claims.py             # Task 5
    test_request_aggregate.py          # Task 5/6/7
    test_run_lifecycle.py              # Task 8/9
    test_feature_versions.py           # Task 10
    test_activation.py                 # Task 11
    test_consumers.py                  # Task 12
    test_feature_lifecycle.py          # Task 13
    test_phase06_e2e.py                # Task 14
```

---

## Task 1 — Owned-table migration

**Files:**
- Create: `src/featuregen/db/migrations/0060_aggregates_lifecycle.sql`
- Test: `tests/featuregen/aggregates/test_migration_0060.py`

**Interfaces:**
- Consumes: the `db` fixture (Phase 01) which applies all migrations in lexical order.
- Produces (physical): tables `feature_versions`, `feature_active_versions`, `consumers`, `concept_claims`, `command_idempotency` exactly as declared in the shared DDL (do not alter columns/constraints/names).

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_migration_0060.py
import pytest

EXPECTED_TABLES = [
    "feature_versions", "feature_active_versions",
    "consumers", "concept_claims", "command_idempotency",
]

@pytest.mark.parametrize("table", EXPECTED_TABLES)
def test_table_exists(db, table):
    row = db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s",
        (table,),
    ).fetchone()
    assert row is not None, f"missing table {table}"

def test_feature_active_versions_pk_is_feature_id_use_case(db):
    cols = db.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'feature_active_versions'::regclass AND i.indisprimary "
        "ORDER BY a.attname"
    ).fetchall()
    assert [c[0] for c in cols] == ["feature_id", "use_case"]

def test_concept_claims_concept_key_is_pk(db):
    row = db.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
        "WHERE i.indrelid = 'concept_claims'::regclass AND i.indisprimary"
    ).fetchone()
    assert row[0] == "concept_key"


def _seed_feature_version(db):
    db.execute(
        "INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
        "verification_stamp, risk_tier, approval_type, content_hash) "
        "VALUES ('fv_im','feat_im','run_im','DATA-CHECKED','low','PRODUCTION','sha256:1')"
    )


def test_feature_versions_reject_update(db):
    _seed_feature_version(db)
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from feature_versions_no_mutation
        db.execute("UPDATE feature_versions SET risk_tier='high' WHERE feature_version_id='fv_im'")


def test_feature_versions_reject_delete(db):
    _seed_feature_version(db)
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from feature_versions_no_mutation
        db.execute("DELETE FROM feature_versions WHERE feature_version_id='fv_im'")
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_migration_0060.py -q`
   Expected: errors like `psycopg.errors.UndefinedTable: relation "feature_versions" does not exist` (migration not yet present).

3. **Write minimal implementation.** Copy the shared-contract DDL VERBATIM into the migration:

```sql
-- src/featuregen/db/migrations/0060_aggregates_lifecycle.sql
-- Phase 06 owns these tables (declared in the shared contract; columns/constraints verbatim).

CREATE TABLE feature_versions (
    feature_version_id            text        PRIMARY KEY,        -- 'fv_...'
    feature_id                    text        NOT NULL,
    produced_by_run               text        NOT NULL,
    base_feature_version_id       text        NULL REFERENCES feature_versions(feature_version_id),
    verification_stamp            text        NOT NULL
                                      CHECK (verification_stamp IN ('DESIGN-CHECKED','DATA-CHECKED','USEFULNESS-CHECKED')),
    risk_tier                     text        NOT NULL,
    approval_type                 text        NOT NULL CHECK (approval_type IN ('EXPERIMENTAL','PRODUCTION')),
    approved_use_cases            text[]      NOT NULL DEFAULT '{}',
    blocked_use_cases             text[]      NOT NULL DEFAULT '{}',
    required_artifact_refs        jsonb       NOT NULL DEFAULT '{}',
    dsl_operation_catalog_version text        NULL,
    approval                      jsonb       NOT NULL DEFAULT '{}',
    expires_at                    timestamptz NULL,
    content_hash                  text        NOT NULL,
    immutable                     boolean     NOT NULL DEFAULT true,
    created_at                    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX feature_versions_feature_idx ON feature_versions (feature_id);
CREATE INDEX feature_versions_base_idx    ON feature_versions (base_feature_version_id);

-- Physical immutability (no UPDATE/DELETE) — mirrors Phase 02's documents write-once trigger.
-- The `immutable` boolean flag is a convention only; this trigger is the enforcement backstop.
CREATE OR REPLACE FUNCTION feature_versions_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_versions are immutable: % not allowed on feature_version_id=%',
        TG_OP, COALESCE(OLD.feature_version_id, NEW.feature_version_id);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER feature_versions_no_mutation
    BEFORE UPDATE OR DELETE ON feature_versions
    FOR EACH ROW EXECUTE FUNCTION feature_versions_write_once();

CREATE TABLE feature_active_versions (
    feature_id         text        NOT NULL,
    use_case           text        NOT NULL,
    feature_version_id text        NOT NULL REFERENCES feature_versions(feature_version_id),
    activation_state   text        NOT NULL
                           CHECK (activation_state IN ('ACTIVE_EXPERIMENTAL','PRODUCTION','DEPRECATED')),
    activated_seq      bigint      NOT NULL,
    activated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (feature_id, use_case)
);

CREATE TABLE consumers (
    consumer_id        text        PRIMARY KEY,
    feature_id         text        NOT NULL,
    feature_version_id text        NULL REFERENCES feature_versions(feature_version_id),
    consumer_kind      text        NOT NULL CHECK (consumer_kind IN ('model','feature')),
    consumer_ref       text        NOT NULL,
    edge_status        text        NOT NULL DEFAULT 'active' CHECK (edge_status IN ('active','deregistered')),
    registered_by      jsonb       NOT NULL,
    registered_at      timestamptz NOT NULL DEFAULT now(),
    deregistered_at    timestamptz NULL,
    UNIQUE (feature_id, consumer_kind, consumer_ref)
);
CREATE INDEX consumers_feature_active_idx ON consumers (feature_id) WHERE edge_status = 'active';

CREATE TABLE concept_claims (
    concept_key   text        PRIMARY KEY,
    request_id    text        NOT NULL,
    claimed_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE command_idempotency (
    idempotency_key   text        PRIMARY KEY,
    action            text        NOT NULL,
    result            jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_migration_0060.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: owned-table migration (feature_versions, active map, consumers, concept_claims, command_idempotency)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 2 — Identifier minting & concept-key normalization

**Files:**
- Create: `src/featuregen/aggregates/__init__.py` (empty), `src/featuregen/aggregates/ids.py`
- Test: `tests/featuregen/aggregates/test_ids.py`

**Interfaces:**
- Produces: `mint_id(prefix: str) -> str`; `new_request_id()`, `new_feature_id()`, `new_run_id()`, `new_feature_version_id()`, `new_consumer_id()`, `new_command_id()` → prefixed ULID-style ids (`req_`, `feat_`, `run_`, `fv_`, `con_`, `cmd_`); `normalize_concept_key(concept: str) -> str`.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_ids.py
from featuregen.aggregates.ids import (
    mint_id, new_request_id, new_feature_id, new_run_id,
    new_feature_version_id, new_consumer_id, new_command_id,
    normalize_concept_key,
)

def test_prefixes():
    assert new_request_id().startswith("req_")
    assert new_feature_id().startswith("feat_")
    assert new_run_id().startswith("run_")
    assert new_feature_version_id().startswith("fv_")
    assert new_consumer_id().startswith("con_")
    assert new_command_id().startswith("cmd_")

def test_ulid_shape_and_uniqueness():
    ids = {mint_id("x") for _ in range(5000)}
    assert len(ids) == 5000
    body = mint_id("x").split("_", 1)[1]
    assert len(body) == 26
    assert set(body) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")

def test_ids_are_lexicographically_time_sortable():
    import time
    a = mint_id("x"); time.sleep(0.002); b = mint_id("x")
    assert a < b

def test_normalize_concept_key():
    assert normalize_concept_key("  Salary  Irregularity! ") == "salary-irregularity"
    assert normalize_concept_key("Salary irregularity") == normalize_concept_key("salary IRREGULARITY")
    assert normalize_concept_key("churn_risk (v2)") == "churn-risk-v2"
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_ids.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.ids'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/ids.py
from __future__ import annotations

import os
import re
import time

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode(value: int, length: int) -> str:
    out = ""
    for _ in range(length):
        out = _CROCKFORD[value & 31] + out
        value >>= 5
    return out


def _ulid() -> str:
    ts = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    return _encode(ts, 10) + _encode(rand, 16)


def mint_id(prefix: str) -> str:
    return f"{prefix}_{_ulid()}"


def new_request_id() -> str:
    return mint_id("req")


def new_feature_id() -> str:
    return mint_id("feat")


def new_run_id() -> str:
    return mint_id("run")


def new_feature_version_id() -> str:
    return mint_id("fv")


def new_consumer_id() -> str:
    return mint_id("con")


def new_command_id() -> str:
    return mint_id("cmd")


def normalize_concept_key(concept: str) -> str:
    s = concept.strip().lower()
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    s = re.sub(r"\s+", "-", s)
    return s
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_ids.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: ULID-style id minting + concept-key normalization" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 3 — Phase-06 event-type registration

**Files:**
- Create: `src/featuregen/aggregates/events.py`
- Test: `tests/featuregen/aggregates/test_events.py`

**Interfaces:**
- Consumes: `SchemaRegistry.register_schema(type_name, schema_version, json_schema, owner, *, status)` (Phase 01).
- Produces: `EVENT_SCHEMAS: dict[str, dict]` (Phase-06 event type → JSON schema, all `schema_version=1`, owner `"sp0-aggregates"`); `register_phase06_event_types(registry: SchemaRegistry) -> None`.

### Steps

1. **Write the failing test.** Uses a recording fake registry (no DB, no Phase-01 instance) plus a real JSON-schema validation check.

```python
# tests/featuregen/aggregates/test_events.py
import jsonschema
import pytest

from featuregen.aggregates.events import EVENT_SCHEMAS, register_phase06_event_types


class _RecordingRegistry:
    def __init__(self):
        self.registered = {}

    def register_schema(self, type_name, schema_version, json_schema, owner, *, status="active"):
        self.registered[(type_name, schema_version)] = (json_schema, owner, status)


def test_registers_every_type_at_v1_with_owner():
    reg = _RecordingRegistry()
    register_phase06_event_types(reg)
    assert set(EVENT_SCHEMAS) <= {t for (t, v) in reg.registered}
    for (type_name, version), (_schema, owner, status) in reg.registered.items():
        assert version == 1 and owner == "sp0-aggregates" and status == "active"


def test_core_types_present():
    for t in ["REQUEST_CREATED", "CANDIDATE_ADDED", "CANDIDATE_SELECTED", "FEATURE_CREATED",
              "VERSION_MINTED", "VERSION_ACTIVATED", "ACTIVATION_CONFLICT", "ACTIVATION_REQUESTED",
              "VERSION_QUIESCED", "RUN_CREATED", "RUN_REJECTED", "FACT_CONFIRMED_RESUME",
              "SOURCE_CHANGED_REVALIDATE"]:
        assert t in EVENT_SCHEMAS


def test_sample_payload_validates_and_missing_required_fails():
    schema = EVENT_SCHEMAS["VERSION_ACTIVATED"]
    jsonschema.validate(
        {"feature_id": "feat_1", "feature_version_id": "fv_1",
         "use_case": "fraud", "activation_state": "PRODUCTION"},
        schema,
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"feature_id": "feat_1"}, schema)
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_events.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.events'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/events.py
from __future__ import annotations

from featuregen.contracts import SchemaRegistry

OWNER = "sp0-aggregates"


def _obj(required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": {key: {} for key in required},
        "required": required,
        "additionalProperties": True,
    }


EVENT_SCHEMAS: dict[str, dict] = {
    # request stream
    "REQUEST_CREATED": _obj(["request_id", "concept_key"]),
    "CANDIDATE_ADDED": _obj(["request_id", "run_id"]),
    "DUPLICATE_OF": _obj(["request_id"]),
    "CANDIDATE_SELECTED": _obj(["request_id", "selected_run_id", "feature_id"]),
    "CANDIDATE_REJECTED": _obj(["request_id", "run_id"]),
    # feature stream
    "FEATURE_CREATED": _obj(["feature_id", "request_id"]),
    "VERSION_MINTED": _obj(["feature_id", "feature_version_id", "produced_by_run"]),
    "VERSION_ACTIVATED": _obj(["feature_id", "feature_version_id", "use_case", "activation_state"]),
    "ACTIVATION_CONFLICT": _obj(["feature_id", "feature_version_id", "use_case"]),
    "VERSION_SUPERSEDED": _obj(["feature_id", "feature_version_id", "use_case"]),
    "VERSION_QUIESCED": _obj(["feature_id", "feature_version_id", "use_case", "impacted_consumers"]),
    "VERSION_DEPRECATED": _obj(["feature_id", "feature_version_id"]),
    "VERSION_RETIERED": _obj(["feature_id", "feature_version_id", "new_risk_tier"]),
    "VERSION_EXPIRED": _obj(["feature_id", "feature_version_id", "use_case"]),
    "CONSUMER_REGISTERED": _obj(["feature_id", "consumer_id", "consumer_kind", "consumer_ref"]),
    "CONSUMER_DEREGISTERED": _obj(["feature_id", "consumer_id"]),
    "MONITORING_ALERT_RAISED": _obj(["feature_id"]),
    "REVALIDATION_REQUIRED": _obj(["feature_id"]),
    "REVALIDATION_OUTCOME_RECORDED": _obj(["feature_id", "outcome"]),
    # run stream
    "RUN_CREATED": _obj(["run_id"]),
    "RUN_CANCELLED": _obj(["run_id"]),
    "RUN_WITHDRAWN": _obj(["run_id"]),
    "RUN_REJECTED": _obj(["run_id"]),
    "RUN_PARKED": _obj(["run_id"]),
    "RUN_UNPARKED": _obj(["run_id"]),
    "FACT_CONFIRMED_RESUME": _obj(["run_id", "fact_key"]),
    "SOURCE_CHANGED_REVALIDATE": _obj(["run_id", "source_ref"]),
    # saga step 1: emitted on the RUN stream in the run's own tx (§5.8); drives the
    # feature-side activate_version handler. Carries every arg apply_activation needs,
    # because the Phase-04 worker passes only HandlerContext (run_id + this triggering
    # event), never the queue payload, to the handler.
    "ACTIVATION_REQUESTED": _obj(["run_id", "feature_id", "feature_version_id",
                                  "use_case", "approval_type"]),
}


def register_phase06_event_types(registry: SchemaRegistry) -> None:
    for type_name, schema in EVENT_SCHEMAS.items():
        registry.register_schema(type_name, 1, schema, OWNER)
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_events.py -q`

5. **Write the bootstrap failing test.** The §"Production wiring" path must register Phase-06 schemas into the *real* process-wide `event_registry`, idempotently, so that `append_event` validation succeeds at runtime (outside pytest). Without this, every Phase-06 append fails schema validation in production.

```python
# tests/featuregen/aggregates/test_bootstrap.py
from featuregen.eventstore import event_registry
from featuregen.aggregates.events import EVENT_SCHEMAS
from featuregen.aggregates.bootstrap import register_phase06_event_schemas


def test_bootstrap_registers_every_type_into_real_event_registry():
    register_phase06_event_schemas()
    # A valid sample for every Phase-06 type validates against the REAL registry the runtime
    # `append_event` uses — proving the production path registered each schema (an unregistered
    # type would raise here, not validate).
    for type_name, schema in EVENT_SCHEMAS.items():
        sample = {key: "x" for key in schema.get("required", [])}
        event_registry.validate(type_name, 1, sample)  # no SchemaValidationError


def test_bootstrap_is_idempotent():
    # calling twice must not raise (e.g. duplicate-registration error)
    register_phase06_event_schemas()
    register_phase06_event_schemas()
```

6. **Write the bootstrap module.** `register_phase06_event_schemas()` is idempotent (guarded), registers into the shared singleton, and is the call the production process makes at startup.

```python
# src/featuregen/aggregates/bootstrap.py
from __future__ import annotations

from featuregen.eventstore import event_registry
from featuregen.aggregates.events import register_phase06_event_types

_SCHEMAS_REGISTERED = False


def register_phase06_event_schemas() -> None:
    """Idempotently register Phase-06 event schemas into the process-wide `event_registry`
    so runtime `append_event` validation passes outside pytest. Called at process startup
    (see the §"Production wiring" note) and by the test conftest."""
    global _SCHEMAS_REGISTERED
    if _SCHEMAS_REGISTERED:
        return
    register_phase06_event_types(event_registry)
    _SCHEMAS_REGISTERED = True
```

7. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_bootstrap.py -q`

8. **Commit.**
   `git add -A && git commit -m "sp0-06: Phase-06 event-type schemas + register_phase06_event_types + idempotent bootstrap" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 4 — `execute_command` (command registry, authz seam, idempotency, degraded-block)

**Files:**
- Create: `src/featuregen/commands/__init__.py` (empty), `src/featuregen/commands/registry.py`, `src/featuregen/commands/authz_seam.py`, `src/featuregen/commands/api.py`
- Create: `tests/featuregen/conftest.py`, `tests/featuregen/_helpers.py`
- Test: `tests/featuregen/commands/test_execute_command.py`

**Interfaces:**
- Consumes: `Command`, `CommandResult`, `DbConn` (contracts); `command_idempotency`, `run_workflow_state` tables; `event_registry` (Phase 01).
- Produces:
  - `register_command(action: str, handler: CommandHandler) -> None`, `get_command(action) -> CommandHandler`, `clear_registry() -> None`, type alias `CommandHandler = Callable[[DbConn, Command], CommandResult]`.
  - `AuthzDecision(allowed: bool, reason: Optional[str])`; `CommandAuthorizer` Protocol with `authorize(conn, cmd) -> AuthzDecision`; `register_command_authorizer(authorizer) -> None`; `current_authorizer() -> CommandAuthorizer` (default allows all — **Phase 07 plugs in the real authorizer that consults `authz_policy` and writes denials to `security_audit`**).
  - `execute_command(conn: DbConn, cmd: Command) -> CommandResult`.

### Steps

1. **Write the failing test.** (First create the shared helpers + conftest so all later tasks reuse them.)

```python
# tests/featuregen/conftest.py
import pytest

from featuregen.aggregates.bootstrap import register_phase06_event_schemas


@pytest.fixture(scope="session", autouse=True)
def _register_phase06_event_types():
    # Exercise the SAME production bootstrap path the running process uses (Task 3), so the
    # test suite and runtime register schemas identically. Idempotent, so safe at session scope.
    register_phase06_event_schemas()
```

```python
# tests/featuregen/_helpers.py
from featuregen.contracts import Command, IdentityEnvelope
from featuregen.aggregates.ids import mint_id


def make_actor(subject="user:raj", actor_kind="human", roles=("data_scientist",)):
    return IdentityEnvelope(
        subject=subject, actor_kind=actor_kind, authenticated=True,
        auth_method="oidc", role_claims=tuple(roles),
    )


def make_cmd(action, aggregate, aggregate_id, args, *, actor=None, idem=None,
             expected_version=None):
    return Command(
        action=action, aggregate=aggregate, aggregate_id=aggregate_id, args=args,
        actor=actor or make_actor(), idempotency_key=idem or mint_id("idem"),
        expected_version=expected_version,
    )
```

```python
# tests/featuregen/commands/test_execute_command.py
import pytest

from featuregen.contracts import CommandResult
from featuregen.commands.api import execute_command
from featuregen.commands.registry import register_command, clear_registry
from featuregen.commands.authz_seam import (
    AuthzDecision, register_command_authorizer, current_authorizer,
)
from tests.featuregen._helpers import make_cmd


@pytest.fixture(autouse=True)
def _clean_registry():
    clean_authorizer = current_authorizer()
    clear_registry()
    yield
    clear_registry()
    register_command_authorizer(clean_authorizer)


def test_dispatch_routes_to_registered_handler(db):
    def handler(conn, cmd):
        return CommandResult(accepted=True, aggregate_id="agg1", produced_event_ids=("e1",))
    register_command("act", handler)
    res = execute_command(db, make_cmd("act", "run", "agg1", {}))
    assert res.accepted and res.produced_event_ids == ("e1",)


def test_duplicate_idempotency_key_replays_original(db):
    calls = []
    def handler(conn, cmd):
        calls.append(1)
        return CommandResult(accepted=True, aggregate_id="agg1", produced_event_ids=("e1",))
    register_command("act", handler)
    cmd = make_cmd("act", "run", "agg1", {}, idem="k1")
    first = execute_command(db, cmd)
    second = execute_command(db, cmd)
    assert first == second
    assert calls == [1]
    rows = db.execute(
        "SELECT count(*) FROM command_idempotency WHERE idempotency_key = %s", ("k1",)
    ).fetchone()[0]
    assert rows == 1


def test_authz_denial_returns_not_accepted_and_does_not_dispatch(db):
    called = []
    register_command("act", lambda c, m: called.append(1))

    class Deny:
        def authorize(self, conn, cmd):
            return AuthzDecision(allowed=False, reason="not permitted")
    register_command_authorizer(Deny())
    res = execute_command(db, make_cmd("act", "run", "agg1", {}))
    assert res.accepted is False
    assert res.denied_reason == "not permitted"
    assert called == []


def test_degraded_run_is_blocked(db):
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, degraded) "
        "VALUES ('run_deg', 'req_x', 'DRAFT', 1, true)"
    )
    register_command("act", lambda c, m: CommandResult(accepted=True, aggregate_id="run_deg"))
    res = execute_command(db, make_cmd("act", "run", "run_deg", {}))
    assert res.accepted is False
    assert "degraded" in res.denied_reason


def test_denied_command_is_not_cached(db):
    register_command("act", lambda c, m: CommandResult(accepted=True, aggregate_id="agg1"))

    class Deny:
        def authorize(self, conn, cmd):
            return AuthzDecision(allowed=False, reason="nope")
    register_command_authorizer(Deny())
    execute_command(db, make_cmd("act", "run", "agg1", {}, idem="dk"))
    rows = db.execute(
        "SELECT count(*) FROM command_idempotency WHERE idempotency_key = %s", ("dk",)
    ).fetchone()[0]
    assert rows == 0  # denials release the claim; a later legitimate retry can run


def test_accepted_command_stores_final_non_pending_result(db):
    register_command("act", lambda c, m: CommandResult(
        accepted=True, aggregate_id="agg1", produced_event_ids=("e1",)))
    execute_command(db, make_cmd("act", "run", "agg1", {}, idem="fk"))
    stored = db.execute(
        "SELECT result FROM command_idempotency WHERE idempotency_key = %s", ("fk",)
    ).fetchone()[0]
    assert stored.get("_pending") is None  # claim was finalized, not left pending
    assert stored["accepted"] is True and stored["produced_event_ids"] == ["e1"]


def test_replay_does_not_rerun_handler_when_prior_committed(db):
    # Simulate a prior committed winner by pre-inserting a finalized idempotency row.
    db.execute(
        "INSERT INTO command_idempotency (idempotency_key, action, result) VALUES "
        "(%s, %s, %s::jsonb)",
        ("pre", "act",
         '{"accepted": true, "aggregate_id": "agg9", "produced_event_ids": ["x1"], '
         '"denied_reason": null}'),
    )
    calls = []
    register_command("act", lambda c, m: calls.append(1))
    res = execute_command(db, make_cmd("act", "run", "agg9", {}, idem="pre"))
    assert res.accepted and res.aggregate_id == "agg9" and res.produced_event_ids == ("x1",)
    assert calls == []  # handler never invoked; result replayed from the committed claim
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/commands/test_execute_command.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.commands.api'`.

3. **Write minimal implementation.**

```python
# src/featuregen/commands/registry.py
from __future__ import annotations

from typing import Callable, Dict

from featuregen.contracts import Command, CommandResult, DbConn

CommandHandler = Callable[[DbConn, Command], CommandResult]
_REGISTRY: Dict[str, CommandHandler] = {}


def register_command(action: str, handler: CommandHandler) -> None:
    if action in _REGISTRY:
        raise ValueError(f"command already registered: {action}")
    _REGISTRY[action] = handler


def get_command(action: str) -> CommandHandler:
    try:
        return _REGISTRY[action]
    except KeyError:
        raise KeyError(f"no handler registered for action: {action}")


def clear_registry() -> None:
    _REGISTRY.clear()
```

```python
# src/featuregen/commands/authz_seam.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from featuregen.contracts import Command, DbConn


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    allowed: bool
    reason: Optional[str] = None


@runtime_checkable
class CommandAuthorizer(Protocol):
    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        """Decide whether `cmd` is permitted. CONTRACT: an authorizer that DENIES is responsible
        for writing the denial to the `security_audit` stream (tamper-evident, NOT the domain
        stream) — this is how `execute_command` fulfils the contract's "on deny, writes to
        security_audit". Phase 07 plugs in the real `authz_policy`-backed authorizer that does
        this; the Phase-06 default below allows all and writes nothing."""
        ...


class _AllowAllAuthorizer:
    def authorize(self, conn: DbConn, cmd: Command) -> AuthzDecision:
        return AuthzDecision(allowed=True)


_AUTHORIZER: CommandAuthorizer = _AllowAllAuthorizer()


def register_command_authorizer(authorizer: CommandAuthorizer) -> None:
    global _AUTHORIZER
    _AUTHORIZER = authorizer


def current_authorizer() -> CommandAuthorizer:
    return _AUTHORIZER
```

```python
# src/featuregen/commands/api.py
from __future__ import annotations

from typing import Mapping, Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.commands.registry import get_command
from featuregen.commands.authz_seam import current_authorizer

_PENDING = {"_pending": True}


def _serialize(result: CommandResult) -> dict:
    return {
        "accepted": result.accepted,
        "aggregate_id": result.aggregate_id,
        "produced_event_ids": list(result.produced_event_ids),
        "denied_reason": result.denied_reason,
    }


def _deserialize(data: Mapping) -> CommandResult:
    return CommandResult(
        accepted=data["accepted"],
        aggregate_id=data["aggregate_id"],
        produced_event_ids=tuple(data["produced_event_ids"]),
        denied_reason=data.get("denied_reason"),
    )


def _claim(conn: DbConn, key: str, action: str) -> bool:
    """Insert a PENDING claim row. Returns True if we won the claim, False if a row already
    exists. `ON CONFLICT (idempotency_key) DO NOTHING` serializes concurrent same-key
    submitters at the unique PK: the loser BLOCKS on the winner's uncommitted row until the
    winner commits/rolls back, then either sees the finalized result (commit) or wins the
    re-claim (rollback). This closes the concurrent double-submit hole — only one transaction
    ever runs the handler for a given idempotency_key."""
    row = conn.execute(
        "INSERT INTO command_idempotency (idempotency_key, action, result) "
        "VALUES (%s, %s, %s) ON CONFLICT (idempotency_key) DO NOTHING RETURNING idempotency_key",
        (key, action, Jsonb(_PENDING)),
    ).fetchone()
    return row is not None


def _finalize(conn: DbConn, key: str, result: CommandResult) -> None:
    conn.execute(
        "UPDATE command_idempotency SET result = %s WHERE idempotency_key = %s",
        (Jsonb(_serialize(result)), key),
    )


def _release(conn: DbConn, key: str) -> None:
    # Denials / degraded blocks are NOT cached: drop the claim so a later legitimate retry runs.
    conn.execute("DELETE FROM command_idempotency WHERE idempotency_key = %s", (key,))


def _replay(conn: DbConn, key: str) -> Optional[CommandResult]:
    row = conn.execute(
        "SELECT result FROM command_idempotency WHERE idempotency_key = %s", (key,)
    ).fetchone()
    if row is None or row[0].get("_pending"):
        return None
    return _deserialize(row[0])


def _is_degraded(conn: DbConn, cmd: Command) -> bool:
    if cmd.aggregate != "run" or cmd.aggregate_id is None:
        return False
    row = conn.execute(
        "SELECT degraded FROM run_workflow_state WHERE run_id = %s",
        (cmd.aggregate_id,),
    ).fetchone()
    return bool(row and row[0])


def execute_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Single command entrypoint (§4.4/§10). Claim-first idempotency (concurrent-safe),
    authz seam, degraded-block, dispatch.

    Authorization: the active authorizer (Phase 07) decides; **the Phase-07 authorizer is
    responsible for writing denials to `security_audit` (NOT the domain stream)** — that
    fulfils the contract's "on deny, writes to security_audit" for `execute_command`. The
    default Phase-06 seam (allow-all) writes nothing because it never denies."""
    key = cmd.idempotency_key
    owned = _claim(conn, key, cmd.action)
    if not owned:
        prior = _replay(conn, key)
        if prior is not None:
            return prior
        # Winner aborted/released its claim; one takeover attempt.
        owned = _claim(conn, key, cmd.action)
        if not owned:
            prior = _replay(conn, key)
            if prior is not None:
                return prior
            return CommandResult(
                accepted=False, aggregate_id=cmd.aggregate_id or "",
                denied_reason="idempotency claim contended; retry",
            )
    # We own the claim; only now run authz + handler.
    decision = current_authorizer().authorize(conn, cmd)
    if not decision.allowed:
        _release(conn, key)
        return CommandResult(
            accepted=False, aggregate_id=cmd.aggregate_id or "",
            denied_reason=decision.reason,
        )
    if cmd.action != "resolve_degraded" and _is_degraded(conn, cmd):
        _release(conn, key)
        return CommandResult(
            accepted=False, aggregate_id=cmd.aggregate_id or "",
            denied_reason="aggregate is degraded",
        )
    result = get_command(cmd.action)(conn, cmd)
    if result.accepted:
        _finalize(conn, key, result)
    else:
        _release(conn, key)
    return result
```

> **`resolve_degraded` (§4.4):** the `cmd.action != "resolve_degraded"` guard lets the remediation command bypass the degraded-block. Its handler is registered in the catalog (Task 8 implements `resolve_degraded_command`; Task 14 binds it) — so the action is never a dangling reference that would reach `get_command()` and raise `KeyError`.
> **Concurrency note:** the claim-first pattern is the airtight fix for the reviewer's concurrent double-submit concern. Sequential double-submit replays the finalized row; concurrent double-submit serializes on the unique `idempotency_key` so only one tx runs the handler and the loser replays the winner's committed result. Denials/degraded blocks release the claim (not cached), matching the prior "only accepted is recorded" behaviour.

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/commands/test_execute_command.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: execute_command (registry, authz seam, idempotency, degraded-block)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 5 — Append helper, concept claim & `create_request`

**Files:**
- Create: `src/featuregen/aggregates/_append.py`, `src/featuregen/aggregates/concept_claims.py`, `src/featuregen/aggregates/request_aggregate.py`
- Test: `tests/featuregen/aggregates/test_concept_claims.py`, `tests/featuregen/aggregates/test_request_aggregate.py`

**Interfaces:**
- Consumes: `append_event`, `load_stream` (Phase 01); `NewEvent`, `EventEnvelope`, `IdentityEnvelope`, `ProvenanceEnvelope`, `CommandResult` (contracts); `run_workflow_state` (read for `table_version_for`).
- Produces:
  - `_append.append(conn, *, aggregate, aggregate_id, type, payload, actor, provenance=None, request_id=None, feature_id=None, run_id=None, caused_by=None, expected_version=None) -> EventEnvelope`; `current_version(conn, aggregate, aggregate_id) -> int`; `provenance_for(artifact_type, **extra) -> ProvenanceEnvelope`; `identity_dict(actor) -> dict`.
  - `concept_claims.claim_concept(conn, concept_key, request_id) -> Optional[str]` (returns `None` if this request won the claim, else the winning request_id).
  - `request_aggregate.create_request_command(conn, cmd) -> CommandResult`.

> **Concept-claim placement (§2 vs §4.4 reconciliation):** the advisory claim + `DUPLICATE_OF` are placed in `create_request` (not `create_run`), following the §4.4 catalog row `create_request | Q | Open a request; place concept-claim (§2)`. §2's "`create_run`/intake" prose is reconciled to this: `create_request` is the intake entrypoint that mints `request_id`, and the request aggregate is the concept's home. See "Naming reconciliations" at the end of this plan.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_concept_claims.py
from featuregen.aggregates.concept_claims import claim_concept


def test_first_committed_wins(db):
    assert claim_concept(db, "salary-irregularity", "req_1") is None
    assert claim_concept(db, "salary-irregularity", "req_2") == "req_1"
    row = db.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        ("salary-irregularity",),
    ).fetchone()
    assert row[0] == "req_1"
```

```python
# tests/featuregen/aggregates/test_request_aggregate.py
from featuregen.eventstore import load_stream
from featuregen.aggregates.request_aggregate import create_request_command
from tests.featuregen._helpers import make_cmd


def test_create_request_mints_id_and_claims_concept(db):
    res = create_request_command(
        db, make_cmd("create_request", "request", None,
                     {"feature_concept": "Salary Irregularity", "intake_mode": "hypothesis"}))
    assert res.accepted and res.aggregate_id.startswith("req_")
    stream = load_stream(db, "request", res.aggregate_id)
    assert [e.type for e in stream] == ["REQUEST_CREATED"]
    assert stream[0].payload["concept_key"] == "salary-irregularity"
    claim = db.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        ("salary-irregularity",),
    ).fetchone()
    assert claim[0] == res.aggregate_id


def test_second_request_on_same_concept_emits_duplicate_of(db):
    first = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "Churn risk"}))
    second = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "churn   RISK"}))
    types = [e.type for e in load_stream(db, "request", second.aggregate_id)]
    assert types == ["REQUEST_CREATED", "DUPLICATE_OF"]
    dup = load_stream(db, "request", second.aggregate_id)[-1]
    assert dup.payload["duplicate_of_request_id"] == first.aggregate_id
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_concept_claims.py tests/featuregen/aggregates/test_request_aggregate.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates._append'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/_append.py
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

from featuregen.contracts import (
    DbConn, EventEnvelope, IdentityEnvelope, NewEvent, ProvenanceEnvelope,
)
from featuregen.eventstore import append_event, load_stream

PRODUCING_COMPONENT = "sp0-aggregates@0.1.0"

# §3.7 stage/artifact enum member (owned by Phase 02). Phase-06 events are governance/lifecycle
# events, not artifact producers, so their ProvenanceEnvelope.artifact_type is the governance
# record artifact — NOT the event-type name. (ProvenanceEnvelope.artifact_type "matches the §3.7
# stage/artifact enum casing"; an event type like "VERSION_MINTED" is NOT a §3.7 enum member.)
GOVERNANCE_ARTIFACT_TYPE = "APPROVAL_RECORD"


def current_version(conn: DbConn, aggregate: str, aggregate_id: str) -> int:
    stream = load_stream(conn, aggregate, aggregate_id)
    return stream[-1].stream_version if stream else 0


def table_version_for(conn: DbConn, aggregate: str, aggregate_id: str) -> int:
    if aggregate == "run":
        row = conn.execute(
            "SELECT table_version FROM run_workflow_state WHERE run_id = %s",
            (aggregate_id,),
        ).fetchone()
        if row is not None:
            return int(row[0])
    return 1


def provenance_for(artifact_type: str = GOVERNANCE_ARTIFACT_TYPE, **extra: Any) -> ProvenanceEnvelope:
    """Build a ProvenanceEnvelope. `artifact_type` MUST be a §3.7 stage/artifact enum value
    (defaults to the governance record artifact for lifecycle events); never pass an event-type
    name here."""
    return ProvenanceEnvelope(
        artifact_type=artifact_type, schema_version=1,
        producing_component=PRODUCING_COMPONENT, **extra,
    )


def identity_dict(actor: IdentityEnvelope) -> dict:
    return asdict(actor)


def append(
    conn: DbConn, *, aggregate: str, aggregate_id: str, type: str,
    payload: Mapping[str, Any], actor: IdentityEnvelope,
    provenance: Optional[ProvenanceEnvelope] = None,
    request_id: Optional[str] = None, feature_id: Optional[str] = None,
    run_id: Optional[str] = None, caused_by: Optional[str] = None,
    expected_version: Optional[int] = None,
) -> EventEnvelope:
    if expected_version is None:
        expected_version = current_version(conn, aggregate, aggregate_id)
    new_event = NewEvent(
        aggregate=aggregate, aggregate_id=aggregate_id, type=type, schema_version=1,
        payload=dict(payload), actor=actor,
        provenance=provenance or provenance_for(),  # §3.7 artifact_type, NOT the event-type name
        request_id=request_id, feature_id=feature_id, run_id=run_id,
        caused_by=caused_by, occurred_at=datetime.now(timezone.utc),
    )
    return append_event(
        conn, new_event, expected_version=expected_version,
        table_version=table_version_for(conn, aggregate, aggregate_id),
    )
```

```python
# src/featuregen/aggregates/concept_claims.py
from __future__ import annotations

from typing import Optional

from featuregen.contracts import DbConn


def claim_concept(conn: DbConn, concept_key: str, request_id: str) -> Optional[str]:
    won = conn.execute(
        "INSERT INTO concept_claims (concept_key, request_id) VALUES (%s, %s) "
        "ON CONFLICT (concept_key) DO NOTHING RETURNING request_id",
        (concept_key, request_id),
    ).fetchone()
    if won is not None:
        return None
    existing = conn.execute(
        "SELECT request_id FROM concept_claims WHERE concept_key = %s",
        (concept_key,),
    ).fetchone()
    return existing[0] if existing else None
```

```python
# src/featuregen/aggregates/request_aggregate.py
from __future__ import annotations

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.aggregates._append import append
from featuregen.aggregates.concept_claims import claim_concept
from featuregen.aggregates.ids import new_request_id
from featuregen.aggregates.ids import normalize_concept_key


def create_request_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = new_request_id()
    concept_key = normalize_concept_key(cmd.args["feature_concept"])
    created = append(
        conn, aggregate="request", aggregate_id=request_id, type="REQUEST_CREATED",
        payload={"request_id": request_id, "concept_key": concept_key,
                 "intake_mode": cmd.args.get("intake_mode", "hypothesis")},
        actor=cmd.actor, request_id=request_id, expected_version=0,
    )
    produced = [created.event_id]
    winner = claim_concept(conn, concept_key, request_id)
    if winner is not None and winner != request_id:
        dup = append(
            conn, aggregate="request", aggregate_id=request_id, type="DUPLICATE_OF",
            payload={"request_id": request_id, "duplicate_of_request_id": winner,
                     "concept_key": concept_key},
            actor=cmd.actor, request_id=request_id,
        )
        produced.append(dup.event_id)
    return CommandResult(accepted=True, aggregate_id=request_id,
                         produced_event_ids=tuple(produced))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_concept_claims.py tests/featuregen/aggregates/test_request_aggregate.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: append helper, concept-claim reservation, create_request (+DUPLICATE_OF)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 6 — `create_run` (request→run binding) & `duplicate_of`

**Files:**
- Modify: `src/featuregen/aggregates/request_aggregate.py`
- Test: append to `tests/featuregen/aggregates/test_request_aggregate.py`

**Interfaces:**
- Consumes: `append`, `new_run_id` (this phase).
- Produces: `create_run_command(conn, cmd) -> CommandResult` (mints `run_id`, appends `RUN_CREATED` on the run stream at v0 → `DRAFT`, appends `CANDIDATE_ADDED` on the request stream); `duplicate_of_command(conn, cmd) -> CommandResult`.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_request_aggregate.py  (append)
from featuregen.aggregates.request_aggregate import create_run_command, duplicate_of_command


def _open_request(db):
    return create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "x"})).aggregate_id


def test_create_run_links_request(db):
    req = _open_request(db)
    r1 = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req}))
    r2 = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req}))
    assert r1.aggregate_id.startswith("run_") and r2.aggregate_id.startswith("run_")
    run_types = [e.type for e in load_stream(db, "run", r1.aggregate_id)]
    assert run_types == ["RUN_CREATED"]
    added = [e.payload["run_id"] for e in load_stream(db, "request", req) if e.type == "CANDIDATE_ADDED"]
    assert set(added) == {r1.aggregate_id, r2.aggregate_id}


def test_duplicate_of_links_existing_feature(db):
    req = _open_request(db)
    res = duplicate_of_command(
        db, make_cmd("duplicate_of", "request", req,
                     {"duplicate_of_feature_id": "feat_existing"}))
    dup = load_stream(db, "request", req)[-1]
    assert dup.type == "DUPLICATE_OF"
    assert dup.payload["duplicate_of_feature_id"] == "feat_existing"
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_request_aggregate.py -q`
   Expected: `ImportError: cannot import name 'create_run_command'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/request_aggregate.py  (append)
from featuregen.aggregates.ids import new_run_id


def create_run_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.args["request_id"]
    run_id = new_run_id()
    run_event = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
        payload={"run_id": run_id, "request_id": request_id, "reopened_from": None},
        actor=cmd.actor, request_id=request_id, run_id=run_id, expected_version=0,
    )
    added = append(
        conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_ADDED",
        payload={"request_id": request_id, "run_id": run_id},
        actor=cmd.actor, request_id=request_id, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id,
                         produced_event_ids=(run_event.event_id, added.event_id))


def duplicate_of_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.aggregate_id
    dup = append(
        conn, aggregate="request", aggregate_id=request_id, type="DUPLICATE_OF",
        payload={"request_id": request_id,
                 "duplicate_of_request_id": cmd.args.get("duplicate_of_request_id"),
                 "duplicate_of_feature_id": cmd.args.get("duplicate_of_feature_id"),
                 "concept_key": cmd.args.get("concept_key")},
        actor=cmd.actor, request_id=request_id,
    )
    return CommandResult(accepted=True, aggregate_id=request_id,
                         produced_event_ids=(dup.event_id,))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_request_aggregate.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: create_run (request->run binding) + duplicate_of" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 7 — `select_candidate` (mint/bind feature_id, close siblings, 1:n)

**Files:**
- Modify: `src/featuregen/aggregates/request_aggregate.py`
- Test: append to `tests/featuregen/aggregates/test_request_aggregate.py`

**Interfaces:**
- Consumes: `load_stream` (Phase 01); `new_feature_id`, `append`, `provenance_for` (this phase); `run_lifecycle.run_is_terminal` (Task 8 — implemented in this task’s minimal code as a local read until Task 8 exposes it; see note).
- Produces: `select_candidate_command(conn, cmd) -> CommandResult`. `cmd.args["selections"]` is a tuple of `{"run_id": str, "feature_id": Optional[str]}`; each selection with no `feature_id` mints a new feature (`FEATURE_CREATED`), each with one binds to it; every selection emits `CANDIDATE_SELECTED`; every non-selected, non-terminal candidate run is closed (`CANDIDATE_REJECTED` on request + `RUN_REJECTED` on the run).

> Note: this task needs a "is this run already terminal" check. To avoid a forward dependency, define the terminal predicate locally here as `_run_terminal_local`; Task 8 introduces the canonical `run_is_terminal` and Task 8’s step 3 replaces the local helper import. Both read the same `RUN_REJECTED/RUN_CANCELLED/RUN_WITHDRAWN` event set.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_request_aggregate.py  (append)
from featuregen.aggregates.request_aggregate import select_candidate_command


def test_select_candidate_mints_feature_and_closes_siblings(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    res = select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a},), "candidates_explored_count": 7}))
    assert res.accepted
    feature_created = [e for e in load_stream(db, "feature", _feature_of(db, req))]
    # selected run a is bound; sibling b is rejected
    assert any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", b))
    assert not any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", a))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["selected_run_id"] == a
    assert sel.payload["candidates_explored_count"] == 7
    assert sel.provenance.candidates_explored_count == 7
    # provenance.artifact_type is a §3.7 stage/artifact enum value, NOT an event-type name
    assert sel.provenance.artifact_type == "APPROVAL_RECORD"


def _feature_of(db, req):
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    return sel.payload["feature_id"]


def test_select_candidate_binds_existing_feature_no_feature_created(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a, "feature_id": "feat_existing"},)}))
    assert not any(e.type == "FEATURE_CREATED" for e in load_stream(db, "feature", "feat_existing"))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["feature_id"] == "feat_existing"


def test_one_request_yields_multiple_features(db):
    req = _open_request(db)
    a = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    select_candidate_command(
        db, make_cmd("select_candidate", "request", req,
                     {"selections": ({"run_id": a}, {"run_id": b})}))
    features = {e.payload["feature_id"]
               for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"}
    assert len(features) == 2
    for e in load_stream(db, "run", a) + load_stream(db, "run", b):
        assert e.type != "RUN_REJECTED"
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_request_aggregate.py -q`
   Expected: `ImportError: cannot import name 'select_candidate_command'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/request_aggregate.py  (append)
from featuregen.aggregates.ids import new_feature_id
from featuregen.aggregates._append import provenance_for
from featuregen.eventstore import load_stream

_TERMINAL_RUN_TYPES = ("RUN_REJECTED", "RUN_CANCELLED", "RUN_WITHDRAWN")


def _run_terminal_local(conn, run_id) -> bool:
    return any(e.type in _TERMINAL_RUN_TYPES for e in load_stream(conn, "run", run_id))


def select_candidate_command(conn: DbConn, cmd: Command) -> CommandResult:
    request_id = cmd.aggregate_id
    stream = load_stream(conn, "request", request_id)
    all_runs = [e.payload["run_id"] for e in stream if e.type == "CANDIDATE_ADDED"]
    selections = cmd.args["selections"]
    selected_ids = {s["run_id"] for s in selections}
    explored = cmd.args.get("candidates_explored_count", len(all_runs))
    produced: list[str] = []
    for sel in selections:
        run_id = sel["run_id"]
        feature_id = sel.get("feature_id")
        if feature_id is None:
            feature_id = new_feature_id()
            created = append(
                conn, aggregate="feature", aggregate_id=feature_id, type="FEATURE_CREATED",
                payload={"feature_id": feature_id, "request_id": request_id,
                         "concept_key": cmd.args.get("concept_key"), "origin_run_id": run_id},
                actor=cmd.actor, request_id=request_id, feature_id=feature_id,
                run_id=run_id, expected_version=0,
            )
            produced.append(created.event_id)
        chosen = append(
            conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_SELECTED",
            payload={"request_id": request_id, "selected_run_id": run_id,
                     "feature_id": feature_id, "candidates_explored_count": explored},
            actor=cmd.actor,
            provenance=provenance_for(candidates_explored_count=explored),  # artifact_type defaults to §3.7 APPROVAL_RECORD
            request_id=request_id, feature_id=feature_id, run_id=run_id,
        )
        produced.append(chosen.event_id)
    for run_id in all_runs:
        if run_id in selected_ids or _run_terminal_local(conn, run_id):
            continue
        rej_req = append(
            conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_REJECTED",
            payload={"request_id": request_id, "run_id": run_id, "reason": "sibling_closed"},
            actor=cmd.actor, request_id=request_id, run_id=run_id,
        )
        rej_run = append(
            conn, aggregate="run", aggregate_id=run_id, type="RUN_REJECTED",
            payload={"run_id": run_id, "reason": "sibling_closed"},
            actor=cmd.actor, run_id=run_id,
        )
        produced.extend([rej_req.event_id, rej_run.event_id])
    return CommandResult(accepted=True, aggregate_id=request_id,
                         produced_event_ids=tuple(produced))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_request_aggregate.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: select_candidate (mint/bind feature_id, close siblings, 1:n)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 8 — Run lifecycle: reject/cancel/withdraw, park/unpark, `reopen_as_new_run`

**Files:**
- Create: `src/featuregen/aggregates/run_lifecycle.py`
- Modify: `src/featuregen/aggregates/request_aggregate.py` (replace local terminal helper with the canonical one)
- Test: `tests/featuregen/aggregates/test_run_lifecycle.py`

**Interfaces:**
- Consumes: `load_stream` (Phase 01); `append`, `new_run_id` (this phase).
- Produces: `run_is_terminal(conn, run_id) -> bool`; `reject_command`, `cancel_command`, `withdraw_command`, `park_command`, `unpark_command`, `reopen_as_new_run_command`, `resolve_degraded_command` (each `(conn, cmd) -> CommandResult`). `resolve_degraded` (§3.6/§4.4) clears a `degraded` projection entry after remediation; it is the action `execute_command` special-cases past the degraded-block.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_run_lifecycle.py
from featuregen.eventstore import load_stream
from featuregen.aggregates.request_aggregate import create_request_command, create_run_command
from featuregen.aggregates.run_lifecycle import (
    reject_command, cancel_command, withdraw_command,
    park_command, unpark_command, reopen_as_new_run_command, run_is_terminal,
)
from tests.featuregen._helpers import make_cmd


def _new_run(db):
    req = create_request_command(
        db, make_cmd("create_request", "request", None, {"feature_concept": "x"})).aggregate_id
    return create_run_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id, req


def test_reject_makes_run_terminal_and_records_reason(db):
    run, _ = _new_run(db)
    res = reject_command(db, make_cmd("reject", "run", run, {"reason": "leakage"}))
    assert res.accepted
    last = load_stream(db, "run", run)[-1]
    assert last.type == "RUN_REJECTED" and last.payload["reason"] == "leakage"
    assert run_is_terminal(db, run)


def test_second_terminal_command_is_rejected(db):
    run, _ = _new_run(db)
    cancel_command(db, make_cmd("cancel", "run", run, {"reason": "stop"}))
    res = withdraw_command(db, make_cmd("withdraw", "run", run, {"reason": "again"}))
    assert res.accepted is False and "terminal" in res.denied_reason


def test_park_unpark(db):
    run, _ = _new_run(db)
    park_command(db, make_cmd("park", "run", run, {"owner": "user:raj", "waiting_on_fact": "f1"}))
    unpark_command(db, make_cmd("unpark", "run", run, {}))
    types = [e.type for e in load_stream(db, "run", run)]
    assert types[-2:] == ["RUN_PARKED", "RUN_UNPARKED"]


def test_reopen_as_new_run_links_rejected(db):
    run, req = _new_run(db)
    reject_command(db, make_cmd("reject", "run", run, {"reason": "leakage"}))
    res = reopen_as_new_run_command(
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run}))
    assert res.accepted and res.aggregate_id != run
    new_created = load_stream(db, "run", res.aggregate_id)[0]
    assert new_created.payload["reopened_from"] == run
    added = [e.payload["run_id"] for e in load_stream(db, "request", req) if e.type == "CANDIDATE_ADDED"]
    assert res.aggregate_id in added


def test_reopen_rejected_when_source_not_rejected(db):
    run, _ = _new_run(db)
    res = reopen_as_new_run_command(
        db, make_cmd("reopen_as_new_run", "run", run, {"source_run_id": run}))
    assert res.accepted is False and "rejected" in res.denied_reason


def test_resolve_degraded_clears_flag(db):
    from featuregen.aggregates.run_lifecycle import resolve_degraded_command
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "degraded, degraded_reason) VALUES ('run_d', 'req_d', 'DRAFT', 1, true, 'boom')"
    )
    res = resolve_degraded_command(db, make_cmd("resolve_degraded", "run", "run_d", {}))
    assert res.accepted
    row = db.execute(
        "SELECT degraded, degraded_reason FROM run_workflow_state WHERE run_id = 'run_d'"
    ).fetchone()
    assert row[0] is False and row[1] is None
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_run_lifecycle.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.run_lifecycle'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/run_lifecycle.py
from __future__ import annotations

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.eventstore import load_stream
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_run_id

_TERMINAL_RUN_TYPES = ("RUN_REJECTED", "RUN_CANCELLED", "RUN_WITHDRAWN")


def run_is_terminal(conn: DbConn, run_id: str) -> bool:
    return any(e.type in _TERMINAL_RUN_TYPES for e in load_stream(conn, "run", run_id))


def _terminal_command(event_type: str):
    def handler(conn: DbConn, cmd: Command) -> CommandResult:
        run_id = cmd.aggregate_id
        if run_is_terminal(conn, run_id):
            return CommandResult(accepted=False, aggregate_id=run_id,
                                 denied_reason="run already terminal")
        evt = append(
            conn, aggregate="run", aggregate_id=run_id, type=event_type,
            payload={"run_id": run_id, "reason": cmd.args.get("reason")},
            actor=cmd.actor, run_id=run_id,
        )
        return CommandResult(accepted=True, aggregate_id=run_id,
                             produced_event_ids=(evt.event_id,))
    return handler


reject_command = _terminal_command("RUN_REJECTED")
cancel_command = _terminal_command("RUN_CANCELLED")
withdraw_command = _terminal_command("RUN_WITHDRAWN")


def park_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_PARKED",
        payload={"run_id": run_id, "owner": cmd.args.get("owner"),
                 "waiting_on_fact": cmd.args.get("waiting_on_fact")},
        actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


def unpark_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="RUN_UNPARKED",
        payload={"run_id": run_id}, actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))


def reopen_as_new_run_command(conn: DbConn, cmd: Command) -> CommandResult:
    source_run = cmd.args["source_run_id"]
    src_stream = load_stream(conn, "run", source_run)
    if not any(e.type == "RUN_REJECTED" for e in src_stream):
        return CommandResult(accepted=False, aggregate_id=source_run,
                             denied_reason="reopen requires a rejected run")
    request_id = next((e.request_id for e in src_stream if e.type == "RUN_CREATED"), None)
    new_run = new_run_id()
    created = append(
        conn, aggregate="run", aggregate_id=new_run, type="RUN_CREATED",
        payload={"run_id": new_run, "request_id": request_id, "reopened_from": source_run},
        actor=cmd.actor, request_id=request_id, run_id=new_run, expected_version=0,
    )
    produced = [created.event_id]
    if request_id is not None:
        added = append(
            conn, aggregate="request", aggregate_id=request_id, type="CANDIDATE_ADDED",
            payload={"request_id": request_id, "run_id": new_run},
            actor=cmd.actor, request_id=request_id, run_id=new_run,
        )
        produced.append(added.event_id)
    return CommandResult(accepted=True, aggregate_id=new_run, produced_event_ids=tuple(produced))


def resolve_degraded_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Clear a `degraded` projection entry after remediation (§3.6/§4.4). This is a projection
    repair (no domain event): it un-blocks the aggregate's commands. `execute_command`
    special-cases this action so it is NOT itself blocked by the degraded gate. Scope here is the
    `run_workflow_state` sample projection (the only degraded-bearing projection in this phase);
    other aggregates' degraded handling is owned by their projection phases."""
    run_id = cmd.aggregate_id
    conn.execute(
        "UPDATE run_workflow_state SET degraded = false, degraded_reason = NULL, "
        "degraded_event_id = NULL, updated_at = now() WHERE run_id = %s",
        (run_id,),
    )
    return CommandResult(accepted=True, aggregate_id=run_id or "")
```

Then replace the local terminal helper in `request_aggregate.py` with the canonical one:

```python
# src/featuregen/aggregates/request_aggregate.py  (modify: delete _run_terminal_local + _TERMINAL_RUN_TYPES,
#                                           and at top of file add)
from featuregen.aggregates.run_lifecycle import run_is_terminal
```

and in `select_candidate_command` replace `_run_terminal_local(conn, run_id)` with `run_is_terminal(conn, run_id)`.

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_run_lifecycle.py tests/featuregen/aggregates/test_request_aggregate.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: run lifecycle (reject/cancel/withdraw/park/unpark/reopen_as_new_run)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 9 — Inbound run signals: `fact_confirmed_resume` & `SOURCE_CHANGED_REVALIDATE`

**Files:**
- Modify: `src/featuregen/aggregates/run_lifecycle.py`
- Test: append to `tests/featuregen/aggregates/test_run_lifecycle.py`

**Interfaces:**
- Consumes: the `events` table (read parked-on-fact runs by `payload->>'waiting_on_fact'`); `append`, `run_is_terminal` (this phase).
- Produces: `fact_confirmed_resume_command(conn, cmd) -> CommandResult` (fans out `FACT_CONFIRMED_RESUME` + `RUN_UNPARKED` to every run parked on `cmd.args["fact_key"]` and not yet unparked); `source_changed_revalidate_command(conn, cmd) -> CommandResult` (emits `SOURCE_CHANGED_REVALIDATE` for an in-flight run; rejected when terminal).

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_run_lifecycle.py  (append)
from featuregen.aggregates.run_lifecycle import (
    fact_confirmed_resume_command, source_changed_revalidate_command,
)


def test_fact_confirmed_resume_wakes_only_runs_waiting_on_that_fact(db):
    waiting, _ = _new_run(db)
    other, _ = _new_run(db)
    park_command(db, make_cmd("park", "run", waiting, {"owner": "o", "waiting_on_fact": "overlay:123"}))
    park_command(db, make_cmd("park", "run", other, {"owner": "o", "waiting_on_fact": "overlay:999"}))
    res = fact_confirmed_resume_command(
        db, make_cmd("fact_confirmed_resume", "run", None, {"fact_key": "overlay:123"}))
    assert res.accepted
    woken_types = [e.type for e in load_stream(db, "run", waiting)]
    assert "FACT_CONFIRMED_RESUME" in woken_types and woken_types[-1] == "RUN_UNPARKED"
    assert "FACT_CONFIRMED_RESUME" not in [e.type for e in load_stream(db, "run", other)]


def test_source_changed_revalidate_for_in_flight_run(db):
    run, _ = _new_run(db)
    res = source_changed_revalidate_command(
        db, make_cmd("source_changed_revalidate", "run", run,
                     {"source_ref": "tbl.core.txn", "new_snapshot": "snap@42"}))
    assert res.accepted
    last = load_stream(db, "run", run)[-1]
    assert last.type == "SOURCE_CHANGED_REVALIDATE"
    assert last.payload["source_ref"] == "tbl.core.txn"


def test_source_changed_revalidate_rejected_when_terminal(db):
    run, _ = _new_run(db)
    reject_command(db, make_cmd("reject", "run", run, {"reason": "x"}))
    res = source_changed_revalidate_command(
        db, make_cmd("source_changed_revalidate", "run", run, {"source_ref": "t"}))
    assert res.accepted is False and "terminal" in res.denied_reason
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_run_lifecycle.py -q`
   Expected: `ImportError: cannot import name 'fact_confirmed_resume_command'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/run_lifecycle.py  (append)
def _runs_parked_on_fact(conn: DbConn, fact_key: str) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT run_id FROM events "
        "WHERE type = 'RUN_PARKED' AND payload->>'waiting_on_fact' = %s "
        "AND run_id NOT IN ("
        "  SELECT run_id FROM events WHERE type = 'RUN_UNPARKED' AND run_id IS NOT NULL)",
        (fact_key,),
    ).fetchall()
    return [r[0] for r in rows]


def fact_confirmed_resume_command(conn: DbConn, cmd: Command) -> CommandResult:
    fact_key = cmd.args["fact_key"]
    produced: list[str] = []
    for run_id in _runs_parked_on_fact(conn, fact_key):
        resume = append(
            conn, aggregate="run", aggregate_id=run_id, type="FACT_CONFIRMED_RESUME",
            payload={"run_id": run_id, "fact_key": fact_key}, actor=cmd.actor, run_id=run_id,
        )
        unparked = append(
            conn, aggregate="run", aggregate_id=run_id, type="RUN_UNPARKED",
            payload={"run_id": run_id}, actor=cmd.actor, run_id=run_id,
        )
        produced.extend([resume.event_id, unparked.event_id])
    return CommandResult(accepted=True, aggregate_id=cmd.aggregate_id or fact_key,
                         produced_event_ids=tuple(produced))


def source_changed_revalidate_command(conn: DbConn, cmd: Command) -> CommandResult:
    run_id = cmd.aggregate_id
    if run_is_terminal(conn, run_id):
        return CommandResult(accepted=False, aggregate_id=run_id,
                             denied_reason="run is terminal; nothing to revalidate")
    evt = append(
        conn, aggregate="run", aggregate_id=run_id, type="SOURCE_CHANGED_REVALIDATE",
        payload={"run_id": run_id, "source_ref": cmd.args["source_ref"],
                 "new_snapshot": cmd.args.get("new_snapshot")},
        actor=cmd.actor, run_id=run_id,
    )
    return CommandResult(accepted=True, aggregate_id=run_id, produced_event_ids=(evt.event_id,))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_run_lifecycle.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: fact_confirmed_resume (wake parked runs) + SOURCE_CHANGED_REVALIDATE" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 10 — `mint_feature_version` (frozen version in the run transaction)

**Files:**
- Create: `src/featuregen/aggregates/feature_versions.py`
- Test: `tests/featuregen/aggregates/test_feature_versions.py`

**Interfaces:**
- Consumes: `feature_versions` table; `append`, `new_feature_version_id` (this phase); `IdentityEnvelope`, `ProvenanceEnvelope` (contracts).
- Produces: `mint_feature_version(conn, *, feature_id, produced_by_run, verification_stamp, risk_tier, approval_type, approved_use_cases, blocked_use_cases, required_artifact_refs, content_hash, actor, provenance, base_feature_version_id=None, dsl_operation_catalog_version=None, approval=None, expires_at=None) -> str` (inserts the immutable row, emits `VERSION_MINTED` on the feature stream, returns `feature_version_id`).

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_feature_versions.py
from featuregen.eventstore import load_stream
from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.feature_versions import mint_feature_version
from tests.featuregen._helpers import make_actor


def test_mint_freezes_version_and_emits_event(db):
    fv = mint_feature_version(
        db, feature_id="feat_1", produced_by_run="run_1",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="medium",
        approval_type="PRODUCTION", approved_use_cases=("fraud",), blocked_use_cases=("credit",),
        required_artifact_refs={"evaluation_report": "doc_1"},
        content_hash="sha256:abc", actor=make_actor(),
        provenance=provenance_for(),
    )
    assert fv.startswith("fv_")
    row = db.execute(
        "SELECT feature_id, approval_type, immutable, approved_use_cases "
        "FROM feature_versions WHERE feature_version_id = %s", (fv,)).fetchone()
    assert row[0] == "feat_1" and row[1] == "PRODUCTION" and row[2] is True
    assert row[3] == ["fraud"]
    minted = load_stream(db, "feature", "feat_1")[-1]
    assert minted.type == "VERSION_MINTED" and minted.payload["feature_version_id"] == fv


def test_base_version_fk_chain(db):
    base = mint_feature_version(
        db, feature_id="feat_2", produced_by_run="run_a", verification_stamp="DATA-CHECKED",
        risk_tier="low", approval_type="PRODUCTION", approved_use_cases=(), blocked_use_cases=(),
        required_artifact_refs={}, content_hash="sha256:1", actor=make_actor(),
        provenance=provenance_for())
    child = mint_feature_version(
        db, feature_id="feat_2", produced_by_run="run_b", verification_stamp="DATA-CHECKED",
        risk_tier="low", approval_type="PRODUCTION", approved_use_cases=(), blocked_use_cases=(),
        required_artifact_refs={}, content_hash="sha256:2", actor=make_actor(),
        provenance=provenance_for(), base_feature_version_id=base)
    row = db.execute(
        "SELECT base_feature_version_id FROM feature_versions WHERE feature_version_id = %s",
        (child,)).fetchone()
    assert row[0] == base
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_feature_versions.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.feature_versions'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/feature_versions.py
from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping, Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, IdentityEnvelope, ProvenanceEnvelope
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_feature_version_id


def mint_feature_version(
    conn: DbConn, *, feature_id: str, produced_by_run: str, verification_stamp: str,
    risk_tier: str, approval_type: str, approved_use_cases, blocked_use_cases,
    required_artifact_refs: Mapping[str, Any], content_hash: str,
    actor: IdentityEnvelope, provenance: ProvenanceEnvelope,
    base_feature_version_id: Optional[str] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    approval: Optional[Mapping[str, Any]] = None,
    expires_at: Optional[datetime] = None,
) -> str:
    fv_id = new_feature_version_id()
    conn.execute(
        "INSERT INTO feature_versions ("
        "  feature_version_id, feature_id, produced_by_run, base_feature_version_id,"
        "  verification_stamp, risk_tier, approval_type, approved_use_cases, blocked_use_cases,"
        "  required_artifact_refs, dsl_operation_catalog_version, approval, expires_at,"
        "  content_hash, immutable) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, true)",
        (fv_id, feature_id, produced_by_run, base_feature_version_id, verification_stamp,
         risk_tier, approval_type, list(approved_use_cases), list(blocked_use_cases),
         Jsonb(dict(required_artifact_refs)), dsl_operation_catalog_version,
         Jsonb(dict(approval or {})), expires_at, content_hash),
    )
    append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_MINTED",
        payload={"feature_id": feature_id, "feature_version_id": fv_id,
                 "produced_by_run": produced_by_run,
                 "base_feature_version_id": base_feature_version_id},
        actor=actor, provenance=provenance, feature_id=feature_id, run_id=produced_by_run,
    )
    return fv_id
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_feature_versions.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: mint_feature_version (frozen immutable version + VERSION_MINTED)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 11 — Activation saga: CAS, `ACTIVATION_CONFLICT`, saga step 1 + feature-side handler, experimental expiry

**Files:**
- Create: `src/featuregen/aggregates/activation.py`
- Modify: `src/featuregen/runtime/step.py` (extend Phase-04 `commit_step` to apply `result.activations` on the step-transaction conn — §5.8; replaces the Phase-04 deferred guard)
- Test: `tests/featuregen/aggregates/test_activation.py`

**Interfaces:**
- Consumes: `feature_active_versions`, `queue`, `timers` tables; `append`, `mint_id`, `new_feature_version_id`, `mint_feature_version`, `current_version` (this phase); `EventEnvelope.global_seq` (set on `activated_seq`); `Handler`, `HandlerContext`, `HandlerResult`, `Disposition`, `IdentityEnvelope`, `NewActivation` (contracts); Phase-04 `HandlerRegistry` / `process_one` / `commit_step` (the worker that dispatches the saga handler and the step boundary that applies the declared activations).
- Produces:
  - `ActivationResult(activated: bool, conflict: bool, feature_version_id: str, use_case: str, event_id: str)`.
  - `apply_activation(conn, *, feature_id, feature_version_id, use_case, base_feature_version_id, approval_type, actor, expires_at=None, provenance=None) -> ActivationResult` — `SELECT ... FOR UPDATE` on `(feature_id, use_case)`; idempotent if already active at that version; **CAS via `_cas_claim_slot`** (closes the null-base race): the active-map write itself is the atomic gate (insert-if-absent for null base, conditional update for a non-null base), so two concurrent first-activations cannot both win; appends `VERSION_ACTIVATED` only after winning the slot, else appends `ACTIVATION_CONFLICT`; `APPROVED_EXPERIMENTAL` → `ACTIVE_EXPERIMENTAL` + schedules an `experiment_expiry` timer.
  - `_cas_claim_slot(conn, *, feature_id, use_case, new_fv, base, state, activated_seq) -> bool` — the atomic CAS primitive (insert-if-absent / conditional-update); returns whether this caller won the slot.
  - `on_run_approved(conn, *, feature_id, produced_by_run, use_case, approval_type, actor, provenance, verification_stamp, risk_tier, approved_use_cases, blocked_use_cases, required_artifact_refs, content_hash, base_feature_version_id=None, dsl_operation_catalog_version=None, approval=None, expires_at=None) -> SagaStep1Result` — **§5.8 saga step 1, in the run's own transaction**: mints the frozen `feature_version_id` (Task 10) AND enqueues the activation request (`request_activation`). `SagaStep1Result(feature_version_id: str, activation_message_id: str)`.
  - `request_activation(conn, *, feature_id, feature_version_id, use_case, base_feature_version_id, approval_type, produced_by_run, actor, expires_at=None) -> str` — appends `ACTIVATION_REQUESTED` on the **run** stream (carrying every arg the handler needs) and enqueues the activation onto `queue` (partition `feature:{feature_id}`, handler `activate_version`, deterministic `message_id`, payload `{"run_id", "event_id"}` so the Phase-04 worker can rebuild the `HandlerContext`).
  - `ActivateVersionHandler` (a `Handler`: `name="activate_version"`, `version=1`, `timeout_seconds`, `handle(ctx)->HandlerResult`) + module singleton `ACTIVATE_VERSION_HANDLER`; `register_phase06_handlers(registry) -> None` — registers it into Phase-04's `HandlerRegistry`. **§5.8 saga step 2:** the worker dispatches this handler; it reads the activation args from `ctx.triggering_event.payload` (the run-stream `ACTIVATION_REQUESTED`) and returns `HandlerResult(disposition=OK, activations=(NewActivation(...),))` with **no run-stream events**. The handler is PURE — it performs NO writes via `ctx`; `commit_step` applies each `NewActivation` by calling `apply_activation` on the SINGLE step-transaction connection (the dispatcher conn), so the feature-side CAS, the `VERSION_ACTIVATED`/`ACTIVATION_CONFLICT` event, and any expiry timer are atomic with the rest of the step. This handler is the sanctioned cross-aggregate saga executor — the general "handlers must not emit feature-stream events" rule (§5.3/contract) does not apply to its declared activation effect; the activation IS the single feature-aggregate transaction §5.8 mandates.
  - `commit_step` extension (`Modify src/featuregen/runtime/step.py`): a `_apply_activations(conn, ctx, result)` helper invoked inside `commit_step` that applies each `result.activations` entry via `apply_activation(conn, ...)` (actor from `ctx.triggering_event.actor`), replacing the Phase-04 deferred-guard clause for `activations`.
  - `activate_command(conn, cmd) -> CommandResult` — the **synchronous** lifecycle command (`activate`, §4.4); distinct entrypoint from the async `activate_version` handler, both delegating to `apply_activation`.
  - `deactivate_expired_version_command(conn, cmd) -> CommandResult`.

> **Saga trigger (who calls `on_run_approved`).** Phase 06 owns the saga-step-1 *logic* (`on_run_approved`); the *trigger* is the run's `APPROVED_EXPERIMENTAL`/`APPROVED_PRODUCTION` transition, whose `on_success` action (Phase 03 state machine / the approval handler that drives that transition) calls `on_run_approved(conn, ...)` **inside the run's transaction** so the version is frozen and the activation request enqueued atomically with the approval (§5.8 step 1). This phase's `test_saga_step1_mints_version_and_enqueues_in_one_tx` exercises that single-transaction effect directly.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_activation.py
from datetime import datetime, timedelta, timezone

from featuregen.contracts import Disposition, HandlerContext
from featuregen.eventstore import load_stream
from featuregen.runtime.step import commit_step
from featuregen.aggregates._append import current_version, provenance_for
from featuregen.aggregates.feature_versions import mint_feature_version
from featuregen.aggregates.activation import (
    apply_activation, activate_command, request_activation, deactivate_expired_version_command,
    on_run_approved, _cas_claim_slot, ACTIVATE_VERSION_HANDLER,
)
from tests.featuregen._helpers import make_actor, make_cmd


def _mint(db, feature_id, run, base=None, approval="PRODUCTION", expires=None):
    return mint_feature_version(
        db, feature_id=feature_id, produced_by_run=run, verification_stamp="USEFULNESS-CHECKED",
        risk_tier="low", approval_type=approval, approved_use_cases=("fraud",),
        blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:" + run,
        actor=make_actor(), provenance=provenance_for(),
        base_feature_version_id=base, expires_at=expires)


def test_first_activation_from_null_base_succeeds(db):
    v1 = _mint(db, "feat_a", "run1")
    res = apply_activation(db, feature_id="feat_a", feature_version_id=v1, use_case="fraud",
                           base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    assert res.activated and not res.conflict
    row = db.execute("SELECT feature_version_id, activation_state FROM feature_active_versions "
                     "WHERE feature_id='feat_a' AND use_case='fraud'").fetchone()
    assert row[0] == v1 and row[1] == "PRODUCTION"


def test_two_runs_from_v1_later_activation_fails_cas(db):
    v1 = _mint(db, "feat_b", "run1")
    apply_activation(db, feature_id="feat_b", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    v2 = _mint(db, "feat_b", "run2", base=v1)
    v3 = _mint(db, "feat_b", "run3", base=v1)
    ok = apply_activation(db, feature_id="feat_b", feature_version_id=v2, use_case="fraud",
                          base_feature_version_id=v1, approval_type="PRODUCTION", actor=make_actor())
    lose = apply_activation(db, feature_id="feat_b", feature_version_id=v3, use_case="fraud",
                            base_feature_version_id=v1, approval_type="PRODUCTION", actor=make_actor())
    assert ok.activated and lose.conflict
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_b' AND use_case='fraud'").fetchone()
    assert row[0] == v2  # no silent overwrite
    assert load_stream(db, "feature", "feat_b")[-1].type == "ACTIVATION_CONFLICT"


def test_activation_is_idempotent(db):
    v1 = _mint(db, "feat_c", "run1")
    a = apply_activation(db, feature_id="feat_c", feature_version_id=v1, use_case="fraud",
                         base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    b = apply_activation(db, feature_id="feat_c", feature_version_id=v1, use_case="fraud",
                         base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    assert a.activated and b.activated
    activations = [e for e in load_stream(db, "feature", "feat_c") if e.type == "VERSION_ACTIVATED"]
    assert len(activations) == 1


def test_use_case_scoped_coexistence(db):
    v1 = _mint(db, "feat_d", "run1")
    v2 = _mint(db, "feat_d", "run2")
    apply_activation(db, feature_id="feat_d", feature_version_id=v1, use_case="credit",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    apply_activation(db, feature_id="feat_d", feature_version_id=v2, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    rows = dict(db.execute("SELECT use_case, feature_version_id FROM feature_active_versions "
                           "WHERE feature_id='feat_d'").fetchall())
    assert rows == {"credit": v1, "fraud": v2}


def test_experimental_activation_schedules_expiry_timer(db):
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    v1 = _mint(db, "feat_e", "run1", approval="EXPERIMENTAL", expires=exp)
    apply_activation(db, feature_id="feat_e", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="EXPERIMENTAL",
                     actor=make_actor(), expires_at=exp)
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_e' AND use_case='fraud'").fetchone()[0]
    assert state == "ACTIVE_EXPERIMENTAL"
    timer = db.execute("SELECT kind, payload->>'handler' FROM timers "
                       "WHERE aggregate='feature' AND aggregate_id='feat_e'").fetchone()
    assert timer == ("experiment_expiry", "deactivate_expired_version")


def test_request_activation_enqueues_feature_partition_and_appends_run_event(db):
    v1 = _mint(db, "feat_f", "run1")
    mid = request_activation(db, feature_id="feat_f", feature_version_id=v1, use_case="fraud",
                             base_feature_version_id=None, approval_type="PRODUCTION",
                             produced_by_run="run1", actor=make_actor())
    row = db.execute("SELECT partition_key, handler, payload FROM queue WHERE message_id=%s",
                     (mid,)).fetchone()
    assert row[0] == "feature:feat_f" and row[1] == "activate_version"
    # the queue payload lets the Phase-04 worker rebuild HandlerContext from the run stream
    assert row[2]["run_id"] == "run1" and "event_id" in row[2]
    req = load_stream(db, "run", "run1")[-1]
    assert req.type == "ACTIVATION_REQUESTED"
    assert req.payload["feature_version_id"] == v1 and req.payload["use_case"] == "fraud"


def test_cas_claim_slot_first_writer_wins_no_silent_overwrite(db):
    # Two writers that BOTH passed a stale current==base(None) precheck race on the slot.
    # The active-map write is the atomic gate: the first wins, the second loses (no overwrite).
    v1 = _mint(db, "feat_cas", "run1")
    v2 = _mint(db, "feat_cas", "run2")
    won1 = _cas_claim_slot(db, feature_id="feat_cas", use_case="fraud", new_fv=v1,
                           base=None, state="PRODUCTION", activated_seq=1)
    won2 = _cas_claim_slot(db, feature_id="feat_cas", use_case="fraud", new_fv=v2,
                           base=None, state="PRODUCTION", activated_seq=2)
    assert won1 is True and won2 is False
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_cas' AND use_case='fraud'").fetchone()
    assert row[0] == v1  # later null-base writer did NOT silently overwrite the first


def test_saga_step1_mints_version_and_enqueues_in_one_tx(db):
    res = on_run_approved(
        db, feature_id="feat_saga", produced_by_run="run_appr", use_case="fraud",
        approval_type="PRODUCTION", actor=make_actor(), provenance=provenance_for(),
        verification_stamp="USEFULNESS-CHECKED", risk_tier="low", approved_use_cases=("fraud",),
        blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:saga",
        base_feature_version_id=None)
    assert res.feature_version_id.startswith("fv_") and res.activation_message_id
    # version frozen in the run tx (step 1a)
    assert db.execute("SELECT count(*) FROM feature_versions WHERE feature_version_id=%s",
                      (res.feature_version_id,)).fetchone()[0] == 1
    assert load_stream(db, "feature", "feat_saga")[-1].type == "VERSION_MINTED"
    # activation request enqueued + ACTIVATION_REQUESTED on the run stream (step 1b)
    q = db.execute("SELECT partition_key, handler FROM queue WHERE message_id=%s",
                   (res.activation_message_id,)).fetchone()
    assert q == ("feature:feat_saga", "activate_version")
    assert load_stream(db, "run", "run_appr")[-1].type == "ACTIVATION_REQUESTED"


def test_activate_version_handler_executes_feature_side_activation(db):
    # §5.8 saga step 2: the registered handler the Phase-04 worker dispatches. The handler is
    # PURE — it only DECLARES the activation; commit_step applies it on the step-tx conn.
    v1 = _mint(db, "feat_hdl", "run_h")
    request_activation(db, feature_id="feat_hdl", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="PRODUCTION",
                       produced_by_run="run_h", actor=make_actor())
    req = load_stream(db, "run", "run_h")[-1]
    assert req.type == "ACTIVATION_REQUESTED"
    ctx = HandlerContext(run_id="run_h", triggering_event=req, documents={}, conn=db)
    result = ACTIVATE_VERSION_HANDLER.handle(ctx)
    assert result.disposition == Disposition.OK
    assert result.new_events == ()  # no run-stream events; commit_step writes only the ledger
    # handler is pure: it declares the effect and writes NOTHING itself.
    assert len(result.activations) == 1 and result.activations[0].feature_version_id == v1
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_hdl'").fetchone()[0] == 0
    # commit_step applies the declared activation on the step-transaction conn.
    commit_step(db, ctx, result, message_id="msg_hdl",
                expected_version=current_version(db, "run", "run_h"), table_version=1)
    row = db.execute("SELECT feature_version_id, activation_state FROM feature_active_versions "
                     "WHERE feature_id='feat_hdl' AND use_case='fraud'").fetchone()
    assert row == (v1, "PRODUCTION")
    assert load_stream(db, "feature", "feat_hdl")[-1].type == "VERSION_ACTIVATED"


def test_activate_version_handler_is_idempotent(db):
    v1 = _mint(db, "feat_hdl2", "run_h2")
    request_activation(db, feature_id="feat_hdl2", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="PRODUCTION",
                       produced_by_run="run_h2", actor=make_actor())
    req = load_stream(db, "run", "run_h2")[-1]
    ctx = HandlerContext(run_id="run_h2", triggering_event=req, documents={}, conn=db)
    # two deliveries; each goes handler -> commit_step; apply_activation no-ops the second time.
    commit_step(db, ctx, ACTIVATE_VERSION_HANDLER.handle(ctx), message_id="msg_h2a",
                expected_version=current_version(db, "run", "run_h2"), table_version=1)
    commit_step(db, ctx, ACTIVATE_VERSION_HANDLER.handle(ctx), message_id="msg_h2b",
                expected_version=current_version(db, "run", "run_h2"), table_version=1)
    activations = [e for e in load_stream(db, "feature", "feat_hdl2") if e.type == "VERSION_ACTIVATED"]
    assert len(activations) == 1  # idempotent: one effect


def test_activation_is_atomic_with_step_rollback(db):
    # A failure anywhere in the step rolls back the ENTIRE step: no orphan active-map row,
    # no VERSION_ACTIVATED event, no expiry timer. Proves apply_activation ran on the step-tx
    # conn (not an autocommit handler conn).
    exp = datetime.now(timezone.utc) + timedelta(days=30)
    v1 = _mint(db, "feat_atom", "run_atom", approval="EXPERIMENTAL", expires=exp)
    request_activation(db, feature_id="feat_atom", feature_version_id=v1, use_case="fraud",
                       base_feature_version_id=None, approval_type="EXPERIMENTAL",
                       produced_by_run="run_atom", actor=make_actor(), expires_at=exp)
    req = load_stream(db, "run", "run_atom")[-1]
    ctx = HandlerContext(run_id="run_atom", triggering_event=req, documents={}, conn=db)
    result = ACTIVATE_VERSION_HANDLER.handle(ctx)
    try:
        with db.transaction():  # mirrors process_one's per-step savepoint
            commit_step(db, ctx, result, message_id="msg_atom",
                        expected_version=current_version(db, "run", "run_atom"), table_version=1)
            raise RuntimeError("boom: forced failure after commit_step, before savepoint release")
    except RuntimeError:
        pass
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_atom'").fetchone()[0] == 0
    assert [e for e in load_stream(db, "feature", "feat_atom")
            if e.type == "VERSION_ACTIVATED"] == []
    assert db.execute("SELECT count(*) FROM timers WHERE aggregate_id='feat_atom'").fetchone()[0] == 0


def test_deactivate_expired_version_removes_active_entry(db):
    exp = datetime.now(timezone.utc) + timedelta(days=1)
    v1 = _mint(db, "feat_g", "run1", approval="EXPERIMENTAL", expires=exp)
    apply_activation(db, feature_id="feat_g", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="EXPERIMENTAL",
                     actor=make_actor(), expires_at=exp)
    res = deactivate_expired_version_command(
        db, make_cmd("deactivate_expired_version", "feature", "feat_g",
                     {"feature_version_id": v1, "use_case": "fraud"}))
    assert res.accepted
    assert db.execute("SELECT count(*) FROM feature_active_versions "
                      "WHERE feature_id='feat_g'").fetchone()[0] == 0
    assert load_stream(db, "feature", "feat_g")[-1].type == "VERSION_EXPIRED"
    # idempotent second fire
    again = deactivate_expired_version_command(
        db, make_cmd("deactivate_expired_version", "feature", "feat_g",
                     {"feature_version_id": v1, "use_case": "fraud"}))
    assert again.accepted and again.produced_event_ids == ()


def test_activate_command_wraps_apply_activation(db):
    v1 = _mint(db, "feat_h", "run1")
    res = activate_command(db, make_cmd("activate", "feature", "feat_h",
        {"feature_version_id": v1, "use_case": "fraud", "base_feature_version_id": None,
         "approval_type": "PRODUCTION"}))
    assert res.accepted and len(res.produced_event_ids) == 1
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_activation.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.activation'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/activation.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import (
    Command, CommandResult, DbConn, Disposition, Handler, HandlerContext, HandlerResult,
    IdentityEnvelope, NewActivation, ProvenanceEnvelope,
)
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import mint_id
from featuregen.aggregates.feature_versions import mint_feature_version


@dataclass(frozen=True, slots=True)
class ActivationResult:
    activated: bool
    conflict: bool
    feature_version_id: str
    use_case: str
    event_id: str


@dataclass(frozen=True, slots=True)
class SagaStep1Result:
    feature_version_id: str
    activation_message_id: str


def _schedule_expiry_timer(conn: DbConn, feature_id: str, feature_version_id: str,
                           use_case: str, expires_at: datetime) -> None:
    conn.execute(
        "INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, kind, "
        "fire_at, payload) VALUES (%s,%s,'feature',%s,'experiment_expiry',%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (mint_id("tmr"), f"expiry:{feature_version_id}:{use_case}", feature_id, expires_at,
         Jsonb({"handler": "deactivate_expired_version", "feature_id": feature_id,
                "feature_version_id": feature_version_id, "use_case": use_case})),
    )


def _cas_claim_slot(
    conn: DbConn, *, feature_id: str, use_case: str, new_fv: str,
    base: Optional[str], state: str, activated_seq: int,
) -> bool:
    """Atomic CAS on the (feature_id, use_case) active-map slot. Returns True iff this caller
    won the slot. The DB write IS the gate (no read-then-write window):
      - null base  -> INSERT ... ON CONFLICT DO NOTHING: only the first concurrent first-
                      activation inserts; the loser conflicts and returns False (no overwrite).
      - non-null base -> conditional UPDATE guarded by `feature_version_id = base`: succeeds only
                      while the slot still holds the run's base version.
    This closes the null-base race where two concurrent first-activations could both pass a
    stale `current == base (None)` precheck and the later one silently overwrite the first."""
    if base is None:
        row = conn.execute(
            "INSERT INTO feature_active_versions "
            "(feature_id, use_case, feature_version_id, activation_state, activated_seq) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (feature_id, use_case) DO NOTHING "
            "RETURNING feature_version_id",
            (feature_id, use_case, new_fv, state, activated_seq),
        ).fetchone()
        return row is not None
    row = conn.execute(
        "UPDATE feature_active_versions SET feature_version_id=%s, activation_state=%s, "
        "activated_seq=%s, activated_at=now() "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s "
        "RETURNING feature_version_id",
        (new_fv, state, activated_seq, feature_id, use_case, base),
    ).fetchone()
    return row is not None


def apply_activation(
    conn: DbConn, *, feature_id: str, feature_version_id: str, use_case: str,
    base_feature_version_id: Optional[str], approval_type: str, actor: IdentityEnvelope,
    expires_at: Optional[datetime] = None, provenance: Optional[ProvenanceEnvelope] = None,
) -> ActivationResult:
    row = conn.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    current = row[0] if row else None
    if current == feature_version_id:
        return ActivationResult(True, False, feature_version_id, use_case, "")  # idempotent
    if current != base_feature_version_id:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_CONFLICT",
            payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                     "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                     "current_active_version_id": current},
            actor=actor, feature_id=feature_id,
        )
        return ActivationResult(False, True, feature_version_id, use_case, evt.event_id)
    activation_state = "ACTIVE_EXPERIMENTAL" if approval_type == "EXPERIMENTAL" else "PRODUCTION"
    # CAS-claim the slot FIRST (with a transient seq); only the winner appends VERSION_ACTIVATED.
    won = _cas_claim_slot(
        conn, feature_id=feature_id, use_case=use_case, new_fv=feature_version_id,
        base=base_feature_version_id, state=activation_state, activated_seq=0,
    )
    if not won:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_CONFLICT",
            payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                     "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                     "current_active_version_id": None, "reason": "lost_cas_race"},
            actor=actor, feature_id=feature_id,
        )
        return ActivationResult(False, True, feature_version_id, use_case, evt.event_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_ACTIVATED",
        payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                 "use_case": use_case, "base_feature_version_id": base_feature_version_id,
                 "activation_state": activation_state},
        actor=actor, provenance=provenance, feature_id=feature_id,
    )
    conn.execute(
        "UPDATE feature_active_versions SET activated_seq=%s "
        "WHERE feature_id=%s AND use_case=%s",
        (evt.global_seq, feature_id, use_case),
    )
    if activation_state == "ACTIVE_EXPERIMENTAL" and expires_at is not None:
        _schedule_expiry_timer(conn, feature_id, feature_version_id, use_case, expires_at)
    return ActivationResult(True, False, feature_version_id, use_case, evt.event_id)


def activate_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Synchronous lifecycle command `activate` (§4.4) — a separate entrypoint from the async
    `activate_version` saga handler; both delegate to apply_activation."""
    args = cmd.args
    res = apply_activation(
        conn, feature_id=cmd.aggregate_id, feature_version_id=args["feature_version_id"],
        use_case=args["use_case"], base_feature_version_id=args.get("base_feature_version_id"),
        approval_type=args["approval_type"], actor=cmd.actor, expires_at=args.get("expires_at"),
    )
    event_ids = (res.event_id,) if res.event_id else ()
    return CommandResult(accepted=True, aggregate_id=cmd.aggregate_id, produced_event_ids=event_ids)


def request_activation(
    conn: DbConn, *, feature_id: str, feature_version_id: str, use_case: str,
    base_feature_version_id: Optional[str], approval_type: str, produced_by_run: str,
    actor: IdentityEnvelope, expires_at: Optional[datetime] = None,
) -> str:
    """§5.8 saga step 1b (in the run's tx): record ACTIVATION_REQUESTED on the RUN stream
    (carrying every arg the feature-side handler needs, since the Phase-04 worker passes the
    handler only a HandlerContext built from this run-stream event), then enqueue a
    feature-partitioned `activate_version` queue row referencing it."""
    req = append(
        conn, aggregate="run", aggregate_id=produced_by_run, type="ACTIVATION_REQUESTED",
        payload={"run_id": produced_by_run, "feature_id": feature_id,
                 "feature_version_id": feature_version_id, "use_case": use_case,
                 "base_feature_version_id": base_feature_version_id,
                 "approval_type": approval_type,
                 "expires_at": expires_at.isoformat() if expires_at else None},
        actor=actor, run_id=produced_by_run, feature_id=feature_id,
    )
    message_id = f"activate:{feature_version_id}:{use_case}"
    conn.execute(
        "INSERT INTO queue (message_id, partition_key, handler, payload) "
        "VALUES (%s, %s, 'activate_version', %s) ON CONFLICT (message_id) DO NOTHING",
        (message_id, f"feature:{feature_id}",
         Jsonb({"run_id": produced_by_run, "event_id": req.event_id})),
    )
    return message_id


def on_run_approved(
    conn: DbConn, *, feature_id: str, produced_by_run: str, use_case: str, approval_type: str,
    actor: IdentityEnvelope, provenance: ProvenanceEnvelope, verification_stamp: str,
    risk_tier: str, approved_use_cases, blocked_use_cases, required_artifact_refs: Mapping[str, Any],
    content_hash: str, base_feature_version_id: Optional[str] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    approval: Optional[Mapping[str, Any]] = None, expires_at: Optional[datetime] = None,
) -> SagaStep1Result:
    """§5.8 saga step 1, ALL in the run's own transaction: mint the frozen feature_version
    (Task 10) and emit the activation request (request_activation). The run is now terminal; the
    version exists but is not yet active — feature-side activation runs async via the worker."""
    fv_id = mint_feature_version(
        conn, feature_id=feature_id, produced_by_run=produced_by_run,
        verification_stamp=verification_stamp, risk_tier=risk_tier, approval_type=approval_type,
        approved_use_cases=approved_use_cases, blocked_use_cases=blocked_use_cases,
        required_artifact_refs=required_artifact_refs, content_hash=content_hash, actor=actor,
        provenance=provenance, base_feature_version_id=base_feature_version_id,
        dsl_operation_catalog_version=dsl_operation_catalog_version, approval=approval,
        expires_at=expires_at,
    )
    message_id = request_activation(
        conn, feature_id=feature_id, feature_version_id=fv_id, use_case=use_case,
        base_feature_version_id=base_feature_version_id, approval_type=approval_type,
        produced_by_run=produced_by_run, actor=actor, expires_at=expires_at,
    )
    return SagaStep1Result(feature_version_id=fv_id, activation_message_id=message_id)


class ActivateVersionHandler:
    """§5.8 saga step 2 — the feature-side activation step the Phase-04 worker dispatches
    (keyed on `queue.handler == name`). The handler is PURE with respect to persistence: it
    reads the activation args from the run-stream ACTIVATION_REQUESTED triggering event and
    DECLARES the cross-aggregate effect as `HandlerResult.activations` — it performs NO writes
    via `ctx`. `commit_step` applies each NewActivation by calling `apply_activation` on the
    SINGLE step-transaction connection, so the active-map CAS, the feature-stream
    VERSION_ACTIVATED / ACTIVATION_CONFLICT event, and any experiment_expiry timer are atomic
    with the rest of the step (a failure rolls them ALL back — no orphan active-map row, event,
    or timer). The handler returns NO run-stream events. Idempotent: re-delivery re-declares the
    same effect and `apply_activation` no-ops when already active at this version. This is the
    sanctioned cross-aggregate saga executor; the general handler prohibition on feature-stream
    writes (§5.3) does not apply to its declared activation effect."""
    name = "activate_version"
    version = 1
    timeout_seconds = 30.0

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        p = ctx.triggering_event.payload
        expires_at = p.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        return HandlerResult(
            disposition=Disposition.OK,
            activations=(
                NewActivation(
                    feature_id=p["feature_id"],
                    feature_version_id=p["feature_version_id"],
                    use_case=p["use_case"],
                    base_feature_version_id=p.get("base_feature_version_id"),
                    approval_type=p["approval_type"],
                    expires_at=expires_at,
                ),
            ),
        )


ACTIVATE_VERSION_HANDLER: Handler = ActivateVersionHandler()


def register_phase06_handlers(registry) -> None:
    """Register Phase-06 saga handlers into Phase-04's HandlerRegistry (production wiring)."""
    registry.register(ACTIVATE_VERSION_HANDLER)


def deactivate_expired_version_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    feature_version_id = cmd.args["feature_version_id"]
    use_case = cmd.args["use_case"]
    row = conn.execute(
        "SELECT feature_version_id, activation_state FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    if row is None or row[0] != feature_version_id or row[1] != "ACTIVE_EXPERIMENTAL":
        return CommandResult(accepted=True, aggregate_id=feature_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_EXPIRED",
        payload={"feature_id": feature_id, "feature_version_id": feature_version_id,
                 "use_case": use_case},
        actor=cmd.actor, feature_id=feature_id,
    )
    conn.execute(
        "DELETE FROM feature_active_versions WHERE feature_id=%s AND use_case=%s",
        (feature_id, use_case),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))
```

4. **Extend `commit_step` to apply the declared activations (Modify `src/featuregen/runtime/step.py`).**
   The saga handler is pure: it only DECLARES `HandlerResult.activations`. Phase 04 added
   `activations` to `commit_step`'s deferred guard ("applied by Phase 06"); this phase REPLACES
   that deferral with the real cross-aggregate application. `apply_activation` runs on the SAME
   transactional `conn` `commit_step` already uses for events/document/outbox/ledger — never an
   autocommit handler connection — so the active-map CAS, the VERSION_ACTIVATED /
   ACTIVATION_CONFLICT feature-stream event, and any experiment_expiry timer are atomic with the
   step. The activation's `actor` comes from `ctx.triggering_event.actor` (the run-stream
   ACTIVATION_REQUESTED event); the remaining args come from each `NewActivation`.

```python
# src/featuregen/runtime/step.py — Phase 06 cross-aggregate extension of commit_step (§5.8).


def _apply_activations(conn, ctx: HandlerContext, result: HandlerResult) -> None:
    """Apply each declared NewActivation on the STEP-TRANSACTION conn (never a handler conn),
    so the active-map CAS + VERSION_ACTIVATED/ACTIVATION_CONFLICT event + expiry timer are
    atomic with the rest of the step. apply_activation is idempotent (no-ops when already active
    at this version), so re-delivery of the saga message produces exactly one effect."""
    if not result.activations:
        return
    # Deferred import keeps Phase-04's step.py free of an import-time dependency on Phase 06.
    from featuregen.aggregates.activation import apply_activation

    actor = ctx.triggering_event.actor
    for act in result.activations:
        apply_activation(
            conn,
            feature_id=act.feature_id,
            feature_version_id=act.feature_version_id,
            use_case=act.use_case,
            base_feature_version_id=act.base_feature_version_id,
            approval_type=act.approval_type,
            actor=actor,
            expires_at=act.expires_at,
            provenance=act.provenance,
        )
```

   Then make two edits inside `commit_step`'s body:
   - **Drop `activations` from the deferred guard.** Phase 04's guard reads
     `if result.timers or result.external_commands or result.activations:` (Phase 05 already
     removed `timers`/`external_commands` when it wired their persistence); remove the
     `or result.activations` clause so a result carrying activations is no longer refused.
   - **Apply them inside the step.** Immediately after the document insert and BEFORE the
     outbox/ledger section, add:

```python
    # Phase 06 (§5.8): apply cross-aggregate activations on the step-transaction conn.
    _apply_activations(conn, ctx, result)
```

5. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_activation.py tests/featuregen/runtime/test_step.py -q`

6. **Commit.**
   `git add -A && git commit -m "sp0-06: activation saga (pure handler declares activations; commit_step applies them atomically; CAS, ACTIVATION_CONFLICT, use-case map, experimental expiry)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 12 — Consumers, `supersede`, `deprecate` (consumer guard), `retier`

**Files:**
- Create: `src/featuregen/aggregates/consumers.py`
- Test: `tests/featuregen/aggregates/test_consumers.py`

**Interfaces:**
- Consumes: `consumers`, `feature_active_versions`, `feature_versions` tables; `append`, `new_consumer_id`, `identity_dict` (this phase).
- Produces: `register_consumer_command`, `deregister_consumer_command`, `supersede_command`, `deprecate_command`, `finalize_deprecate_command`, `retier_command` (each `(conn, cmd) -> CommandResult`). `deprecate` with active consumers is **blocked** (accepted=False) unless `cmd.args["force_quiesce"]` is true; the **forced** path implements the §4.4-note/§6.3 **impact-analysis + quiesce/grace transition**: it records `VERSION_QUIESCED` (capturing the impacted active-consumer refs), schedules a `business_repair` grace timer that fires `finalize_deprecate`, and leaves the active version `PRODUCTION` during the grace window (consumers get time to migrate). `finalize_deprecate` (grace-timer/operator driven, idempotent) emits `VERSION_DEPRECATED` and flips the active map to `DEPRECATED`. The clean path (no active consumers) deprecates directly. `supersede` activates a new version over the prior active one (CAS on `expected_prior` when provided) and leaves the prior `feature_versions` row immutable. `retier` emits `VERSION_RETIERED` without mutating the immutable `feature_versions` row.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_consumers.py
from featuregen.eventstore import load_stream
from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.feature_versions import mint_feature_version
from featuregen.aggregates.activation import apply_activation
from featuregen.aggregates.consumers import (
    register_consumer_command, deregister_consumer_command,
    supersede_command, deprecate_command, finalize_deprecate_command, retier_command,
)
from tests.featuregen._helpers import make_actor, make_cmd


def _mint(db, feature_id, run, base=None, tier="low"):
    return mint_feature_version(
        db, feature_id=feature_id, produced_by_run=run, verification_stamp="DATA-CHECKED",
        risk_tier=tier, approval_type="PRODUCTION", approved_use_cases=("fraud",),
        blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:" + run,
        actor=make_actor(), provenance=provenance_for(), base_feature_version_id=base)


def test_register_then_deregister_consumer(db):
    v1 = _mint(db, "feat_a", "run1")
    register_consumer_command(db, make_cmd("register_consumer", "feature", "feat_a",
        {"consumer_kind": "model", "consumer_ref": "model:churn", "feature_version_id": v1}))
    active = db.execute("SELECT count(*) FROM consumers WHERE feature_id='feat_a' "
                        "AND edge_status='active'").fetchone()[0]
    assert active == 1
    deregister_consumer_command(db, make_cmd("deregister_consumer", "feature", "feat_a",
        {"consumer_kind": "model", "consumer_ref": "model:churn"}))
    active = db.execute("SELECT count(*) FROM consumers WHERE feature_id='feat_a' "
                        "AND edge_status='active'").fetchone()[0]
    assert active == 0


def test_deprecate_blocked_while_active_consumer_exists(db):
    v1 = _mint(db, "feat_b", "run1")
    apply_activation(db, feature_id="feat_b", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    register_consumer_command(db, make_cmd("register_consumer", "feature", "feat_b",
        {"consumer_kind": "model", "consumer_ref": "model:churn"}))
    blocked = deprecate_command(db, make_cmd("deprecate", "feature", "feat_b",
        {"feature_version_id": v1, "use_case": "fraud"}))
    assert blocked.accepted is False and "consumer" in blocked.denied_reason
    deregister_consumer_command(db, make_cmd("deregister_consumer", "feature", "feat_b",
        {"consumer_kind": "model", "consumer_ref": "model:churn"}))
    ok = deprecate_command(db, make_cmd("deprecate", "feature", "feat_b",
        {"feature_version_id": v1, "use_case": "fraud"}))
    assert ok.accepted
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_b' AND use_case='fraud'").fetchone()[0]
    assert state == "DEPRECATED"


def test_supersede_updates_active_and_keeps_prior_immutable(db):
    v1 = _mint(db, "feat_c", "run1")
    apply_activation(db, feature_id="feat_c", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    v2 = _mint(db, "feat_c", "run2", base=v1)
    res = supersede_command(db, make_cmd("supersede", "feature", "feat_c",
        {"feature_version_id": v2, "use_case": "fraud", "expected_prior": v1}))
    assert res.accepted
    row = db.execute("SELECT feature_version_id FROM feature_active_versions "
                     "WHERE feature_id='feat_c' AND use_case='fraud'").fetchone()
    assert row[0] == v2
    assert db.execute("SELECT immutable FROM feature_versions WHERE feature_version_id=%s",
                      (v1,)).fetchone()[0] is True
    assert load_stream(db, "feature", "feat_c")[-1].type == "VERSION_SUPERSEDED"


def test_retier_emits_event_without_mutating_version(db):
    v1 = _mint(db, "feat_d", "run1", tier="high")
    res = retier_command(db, make_cmd("retier", "feature", "feat_d",
        {"feature_version_id": v1, "new_risk_tier": "low"}))
    assert res.accepted
    assert db.execute("SELECT risk_tier FROM feature_versions WHERE feature_version_id=%s",
                      (v1,)).fetchone()[0] == "high"
    last = load_stream(db, "feature", "feat_d")[-1]
    assert last.type == "VERSION_RETIERED"
    assert last.payload == {**last.payload, "old_risk_tier": "high", "new_risk_tier": "low"}


def test_force_deprecate_quiesces_with_grace_not_immediate_deprecation(db):
    v1 = _mint(db, "feat_e", "run1")
    apply_activation(db, feature_id="feat_e", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    register_consumer_command(db, make_cmd("register_consumer", "feature", "feat_e",
        {"consumer_kind": "model", "consumer_ref": "model:churn"}))
    res = deprecate_command(db, make_cmd("deprecate", "feature", "feat_e",
        {"feature_version_id": v1, "use_case": "fraud", "force_quiesce": True,
         "grace_seconds": 3600}))
    assert res.accepted
    quiesced = load_stream(db, "feature", "feat_e")[-1]
    assert quiesced.type == "VERSION_QUIESCED"
    assert quiesced.payload["impacted_consumers"] == ["model:churn"]  # impact analysis recorded
    # active version is NOT deprecated yet — it is quiescing through the grace window
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_e' AND use_case='fraud'").fetchone()[0]
    assert state == "PRODUCTION"
    timer = db.execute("SELECT kind, payload->>'handler' FROM timers "
                       "WHERE aggregate='feature' AND aggregate_id='feat_e'").fetchone()
    assert timer == ("business_repair", "finalize_deprecate")


def test_finalize_deprecate_completes_after_grace_and_is_idempotent(db):
    v1 = _mint(db, "feat_f", "run1")
    apply_activation(db, feature_id="feat_f", feature_version_id=v1, use_case="fraud",
                     base_feature_version_id=None, approval_type="PRODUCTION", actor=make_actor())
    register_consumer_command(db, make_cmd("register_consumer", "feature", "feat_f",
        {"consumer_kind": "model", "consumer_ref": "model:churn"}))
    deprecate_command(db, make_cmd("deprecate", "feature", "feat_f",
        {"feature_version_id": v1, "use_case": "fraud", "force_quiesce": True}))
    res = finalize_deprecate_command(db, make_cmd("finalize_deprecate", "feature", "feat_f",
        {"feature_version_id": v1, "use_case": "fraud"}))
    assert res.accepted and len(res.produced_event_ids) == 1
    last = load_stream(db, "feature", "feat_f")[-1]
    assert last.type == "VERSION_DEPRECATED" and last.payload["via"] == "quiesce"
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_f' AND use_case='fraud'").fetchone()[0]
    assert state == "DEPRECATED"
    # idempotent second fire: no further event
    again = finalize_deprecate_command(db, make_cmd("finalize_deprecate", "feature", "feat_f",
        {"feature_version_id": v1, "use_case": "fraud"}))
    assert again.accepted and again.produced_event_ids == ()
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_consumers.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.consumers'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/consumers.py
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.aggregates._append import append, identity_dict
from featuregen.aggregates.ids import mint_id, new_consumer_id

_DEFAULT_GRACE_SECONDS = 7 * 24 * 3600


def register_consumer_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    consumer_id = conn.execute(
        "INSERT INTO consumers (consumer_id, feature_id, feature_version_id, consumer_kind, "
        "consumer_ref, registered_by) VALUES (%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (feature_id, consumer_kind, consumer_ref) DO UPDATE SET "
        "edge_status='active', deregistered_at=NULL RETURNING consumer_id",
        (new_consumer_id(), feature_id, args.get("feature_version_id"), args["consumer_kind"],
         args["consumer_ref"], Jsonb(identity_dict(cmd.actor))),
    ).fetchone()[0]
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="CONSUMER_REGISTERED",
        payload={"feature_id": feature_id, "consumer_id": consumer_id,
                 "consumer_kind": args["consumer_kind"], "consumer_ref": args["consumer_ref"]},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def deregister_consumer_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    row = conn.execute(
        "UPDATE consumers SET edge_status='deregistered', deregistered_at=now() "
        "WHERE feature_id=%s AND consumer_kind=%s AND consumer_ref=%s RETURNING consumer_id",
        (feature_id, args["consumer_kind"], args["consumer_ref"]),
    ).fetchone()
    consumer_id = row[0] if row else None
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="CONSUMER_DEREGISTERED",
        payload={"feature_id": feature_id, "consumer_id": consumer_id,
                 "consumer_kind": args["consumer_kind"], "consumer_ref": args["consumer_ref"]},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def supersede_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    new_fv = args["feature_version_id"]
    row = conn.execute(
        "SELECT feature_version_id FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s FOR UPDATE",
        (feature_id, use_case),
    ).fetchone()
    prior = row[0] if row else None
    if args.get("expected_prior") is not None and prior != args["expected_prior"]:
        evt = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="ACTIVATION_CONFLICT",
            payload={"feature_id": feature_id, "feature_version_id": new_fv, "use_case": use_case,
                     "base_feature_version_id": args["expected_prior"],
                     "current_active_version_id": prior},
            actor=cmd.actor, feature_id=feature_id,
        )
        return CommandResult(accepted=True, aggregate_id=feature_id,
                             produced_event_ids=(evt.event_id,))
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_SUPERSEDED",
        payload={"feature_id": feature_id, "feature_version_id": new_fv,
                 "superseded_version_id": prior, "use_case": use_case},
        actor=cmd.actor, feature_id=feature_id,
    )
    conn.execute(
        "INSERT INTO feature_active_versions "
        "(feature_id, use_case, feature_version_id, activation_state, activated_seq) "
        "VALUES (%s,%s,%s,'PRODUCTION',%s) "
        "ON CONFLICT (feature_id, use_case) DO UPDATE SET "
        "feature_version_id=EXCLUDED.feature_version_id, activation_state='PRODUCTION', "
        "activated_seq=EXCLUDED.activated_seq, activated_at=now()",
        (feature_id, use_case, new_fv, evt.global_seq),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def _deprecate_now(conn: DbConn, cmd: Command, *, via: str) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_DEPRECATED",
        payload={"feature_id": feature_id, "feature_version_id": fv, "use_case": use_case,
                 "reason": args.get("reason"), "via": via},
        actor=cmd.actor, feature_id=feature_id,
    )
    conn.execute(
        "UPDATE feature_active_versions SET activation_state='DEPRECATED' "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s",
        (feature_id, use_case, fv),
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def deprecate_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    active_refs = [
        r[0] for r in conn.execute(
            "SELECT consumer_ref FROM consumers WHERE feature_id=%s AND edge_status='active'",
            (feature_id,),
        ).fetchall()
    ]
    if active_refs and not args.get("force_quiesce"):
        return CommandResult(accepted=False, aggregate_id=feature_id,
                             denied_reason=f"deprecate blocked: {len(active_refs)} active consumer(s)")
    if active_refs:  # forced: §4.4-note/§6.3 impact-analysis + quiesce/grace transition
        grace_seconds = int(args.get("grace_seconds", _DEFAULT_GRACE_SECONDS))
        quiesced = append(
            conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_QUIESCED",
            payload={"feature_id": feature_id, "feature_version_id": fv, "use_case": use_case,
                     "impacted_consumers": active_refs, "grace_seconds": grace_seconds,
                     "reason": args.get("reason")},
            actor=cmd.actor, feature_id=feature_id,
        )
        fire_at = datetime.now(timezone.utc) + timedelta(seconds=grace_seconds)
        conn.execute(
            "INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, kind, "
            "fire_at, payload) VALUES (%s,%s,'feature',%s,'business_repair',%s,%s) "
            "ON CONFLICT (idempotency_key) DO NOTHING",
            (mint_id("tmr"), f"quiesce:{fv}:{use_case}", feature_id, fire_at,
             Jsonb({"handler": "finalize_deprecate", "feature_id": feature_id,
                    "feature_version_id": fv, "use_case": use_case})),
        )
        # active version stays PRODUCTION during the grace window; finalize_deprecate completes it.
        return CommandResult(accepted=True, aggregate_id=feature_id,
                             produced_event_ids=(quiesced.event_id,))
    return _deprecate_now(conn, cmd, via="direct")


def finalize_deprecate_command(conn: DbConn, cmd: Command) -> CommandResult:
    """Complete a quiesced deprecation after the grace window (grace-timer or operator driven).
    Idempotent: a no-op once the slot is already DEPRECATED or gone."""
    feature_id = cmd.aggregate_id
    args = cmd.args
    use_case = args["use_case"]
    fv = args["feature_version_id"]
    row = conn.execute(
        "SELECT activation_state FROM feature_active_versions "
        "WHERE feature_id=%s AND use_case=%s AND feature_version_id=%s FOR UPDATE",
        (feature_id, use_case, fv),
    ).fetchone()
    if row is None or row[0] == "DEPRECATED":
        return CommandResult(accepted=True, aggregate_id=feature_id)
    return _deprecate_now(conn, cmd, via="quiesce")


def retier_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    args = cmd.args
    row = conn.execute(
        "SELECT risk_tier FROM feature_versions WHERE feature_version_id=%s",
        (args["feature_version_id"],),
    ).fetchone()
    old_tier = row[0] if row else None
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="VERSION_RETIERED",
        payload={"feature_id": feature_id, "feature_version_id": args["feature_version_id"],
                 "old_risk_tier": old_tier, "new_risk_tier": args["new_risk_tier"]},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_consumers.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: consumers + supersede + deprecate (consumer guard) + retier" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 13 — Feature inbound lifecycle signals & lifecycle states

**Files:**
- Create: `src/featuregen/aggregates/feature_lifecycle.py`
- Test: `tests/featuregen/aggregates/test_feature_lifecycle.py`

**Interfaces:**
- Consumes: `load_stream` (Phase 01); `append`, `new_run_id` (this phase); `feature_active_versions` table.
- Produces: `raise_monitoring_alert_command`, `require_revalidation_command`, `record_revalidation_outcome_command` (each `(conn, cmd) -> CommandResult`). Lifecycle order `PRODUCTION → MONITORING_ALERT → REVALIDATION_REQUIRED → (PRODUCTION|DEPRECATED)` is derived from the feature stream; `require_revalidation` is rejected without a prior `MONITORING_ALERT_RAISED`; `record_revalidation_outcome` is rejected without a prior `REVALIDATION_REQUIRED`; `outcome="requires_change"` mints a new linked run, `outcome="deprecate"` sets the active map `DEPRECATED`.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_feature_lifecycle.py
from featuregen.eventstore import load_stream
from featuregen.aggregates.feature_lifecycle import (
    raise_monitoring_alert_command, require_revalidation_command,
    record_revalidation_outcome_command,
)
from tests.featuregen._helpers import make_cmd, make_actor


def _svc():
    return make_actor(subject="service:monitoring", actor_kind="service", roles=("monitoring",))


def test_revalidated_returns_to_production(db):
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_a",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_a",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    res = record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_a", {"feature_version_id": "fv_1", "outcome": "revalidated"}, actor=_svc()))
    assert res.accepted
    last = load_stream(db, "feature", "feat_a")[-1]
    assert last.type == "REVALIDATION_OUTCOME_RECORDED" and last.payload["outcome"] == "revalidated"


def test_require_revalidation_rejected_without_prior_alert(db):
    res = require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_b",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    assert res.accepted is False and "MONITORING_ALERT" in res.denied_reason


def test_requires_change_spawns_new_run(db):
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_c",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_c",
        {"feature_version_id": "fv_1"}, actor=_svc()))
    res = record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_c", {"feature_version_id": "fv_1", "outcome": "requires_change"}, actor=_svc()))
    outcome = load_stream(db, "feature", "feat_c")[-1]
    new_run = outcome.payload["new_run_id"]
    assert new_run and new_run.startswith("run_")
    created = load_stream(db, "run", new_run)[0]
    assert created.type == "RUN_CREATED" and created.feature_id == "feat_c"


def test_deprecate_outcome_sets_active_map_deprecated(db):
    db.execute("INSERT INTO feature_versions (feature_version_id, feature_id, produced_by_run, "
               "verification_stamp, risk_tier, approval_type, content_hash) "
               "VALUES ('fv_x','feat_d','run_x','DATA-CHECKED','low','PRODUCTION','sha256:1')")
    db.execute("INSERT INTO feature_active_versions (feature_id, use_case, feature_version_id, "
               "activation_state, activated_seq) VALUES ('feat_d','fraud','fv_x','PRODUCTION',1)")
    raise_monitoring_alert_command(db, make_cmd("raise_monitoring_alert", "feature", "feat_d",
        {"feature_version_id": "fv_x"}, actor=_svc()))
    require_revalidation_command(db, make_cmd("require_revalidation", "feature", "feat_d",
        {"feature_version_id": "fv_x"}, actor=_svc()))
    record_revalidation_outcome_command(db, make_cmd("record_revalidation_outcome",
        "feature", "feat_d", {"feature_version_id": "fv_x", "outcome": "deprecate"}, actor=_svc()))
    state = db.execute("SELECT activation_state FROM feature_active_versions "
                       "WHERE feature_id='feat_d'").fetchone()[0]
    assert state == "DEPRECATED"
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_feature_lifecycle.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.feature_lifecycle'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/feature_lifecycle.py
from __future__ import annotations

from typing import Optional

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.eventstore import load_stream
from featuregen.aggregates._append import append
from featuregen.aggregates.ids import new_run_id

_LIFECYCLE_TYPES = ("MONITORING_ALERT_RAISED", "REVALIDATION_REQUIRED",
                    "REVALIDATION_OUTCOME_RECORDED")


def _last_lifecycle(conn: DbConn, feature_id: str) -> Optional[str]:
    for event in reversed(load_stream(conn, "feature", feature_id)):
        if event.type in _LIFECYCLE_TYPES:
            return event.type
    return None


def raise_monitoring_alert_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="MONITORING_ALERT_RAISED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "alert_ref": cmd.args.get("alert_ref")},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def require_revalidation_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    if _last_lifecycle(conn, feature_id) != "MONITORING_ALERT_RAISED":
        return CommandResult(accepted=False, aggregate_id=feature_id,
                             denied_reason="require_revalidation requires a prior MONITORING_ALERT")
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="REVALIDATION_REQUIRED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "reason": cmd.args.get("reason")},
        actor=cmd.actor, feature_id=feature_id,
    )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=(evt.event_id,))


def record_revalidation_outcome_command(conn: DbConn, cmd: Command) -> CommandResult:
    feature_id = cmd.aggregate_id
    outcome = cmd.args["outcome"]
    if _last_lifecycle(conn, feature_id) != "REVALIDATION_REQUIRED":
        return CommandResult(accepted=False, aggregate_id=feature_id,
                             denied_reason="record_revalidation_outcome requires REVALIDATION_REQUIRED")
    produced: list[str] = []
    new_run = None
    if outcome == "requires_change":
        new_run = new_run_id()
        created = append(
            conn, aggregate="run", aggregate_id=new_run, type="RUN_CREATED",
            payload={"run_id": new_run, "request_id": cmd.args.get("request_id"),
                     "feature_id": feature_id, "reopened_from": None, "origin": "revalidation"},
            actor=cmd.actor, request_id=cmd.args.get("request_id"), feature_id=feature_id,
            run_id=new_run, expected_version=0,
        )
        produced.append(created.event_id)
    evt = append(
        conn, aggregate="feature", aggregate_id=feature_id, type="REVALIDATION_OUTCOME_RECORDED",
        payload={"feature_id": feature_id,
                 "feature_version_id": cmd.args.get("feature_version_id"),
                 "outcome": outcome, "new_run_id": new_run},
        actor=cmd.actor, feature_id=feature_id,
    )
    produced.append(evt.event_id)
    if outcome == "deprecate":
        conn.execute(
            "UPDATE feature_active_versions SET activation_state='DEPRECATED' WHERE feature_id=%s",
            (feature_id,),
        )
    return CommandResult(accepted=True, aggregate_id=feature_id, produced_event_ids=tuple(produced))
```

4. **Run tests, expect PASS.**
   `python -m pytest tests/featuregen/aggregates/test_feature_lifecycle.py -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: feature lifecycle signals (monitoring alert -> revalidation -> outcome, spawn new run)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 14 — Wire the command catalog into `execute_command` + end-to-end §12 oracles

**Files:**
- Create: `src/featuregen/aggregates/commands.py`
- Test: `tests/featuregen/aggregates/test_phase06_e2e.py`

**Interfaces:**
- Consumes: `register_command` (Task 4); every `*_command` from Tasks 5–13; `execute_command` (Task 4).
- Produces: `register_phase06_commands() -> None` (binds the canonical §4.4 action vocabulary to its handler): `create_request`, `create_run`, `duplicate_of`, `select_candidate`, `cancel`, `withdraw`, `reject`, `park`, `unpark`, `reopen_as_new_run`, `resolve_degraded`, `fact_confirmed_resume`, `source_changed_revalidate`, `activate`, `supersede`, `deprecate`, `finalize_deprecate`, `retier`, `register_consumer`, `deregister_consumer`, `raise_monitoring_alert`, `require_revalidation`, `record_revalidation_outcome`, `deactivate_expired_version`.
- Also produces `bootstrap_phase06(handler_registry) -> None` (the single **production wiring** entrypoint, appended to `bootstrap.py`): registers event schemas (`register_phase06_event_schemas`, Task 3), the command catalog (`register_phase06_commands`), and the saga handler (`register_phase06_handlers`, Task 11) into the passed-in Phase-04 `HandlerRegistry`.

### Steps

1. **Write the failing test.**

```python
# tests/featuregen/aggregates/test_phase06_e2e.py
import pytest

from featuregen.eventstore import load_stream
from featuregen.commands.api import execute_command
from featuregen.commands.registry import clear_registry
from featuregen.aggregates.commands import register_phase06_commands
from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.feature_versions import mint_feature_version
from tests.featuregen._helpers import make_cmd, make_actor


@pytest.fixture(autouse=True)
def _registered():
    clear_registry()
    register_phase06_commands()
    yield
    clear_registry()


def test_multi_candidate_request_flow_through_execute_command(db):
    req = execute_command(db, make_cmd("create_request", "request", None,
        {"feature_concept": "salary irregularity"})).aggregate_id
    a = execute_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    b = execute_command(db, make_cmd("create_run", "request", req, {"request_id": req})).aggregate_id
    res = execute_command(db, make_cmd("select_candidate", "request", req,
        {"selections": ({"run_id": a},), "candidates_explored_count": 5}))
    assert res.accepted
    assert any(e.type == "RUN_REJECTED" for e in load_stream(db, "run", b))
    sel = [e for e in load_stream(db, "request", req) if e.type == "CANDIDATE_SELECTED"][0]
    assert sel.payload["candidates_explored_count"] == 5


def test_activation_cas_oracle_through_execute_command(db):
    def mint(feature_id, run, base=None):
        return mint_feature_version(
            db, feature_id=feature_id, produced_by_run=run, verification_stamp="USEFULNESS-CHECKED",
            risk_tier="low", approval_type="PRODUCTION", approved_use_cases=("fraud",),
            blocked_use_cases=(), required_artifact_refs={}, content_hash="sha256:" + run,
            actor=make_actor(), provenance=provenance_for(),
            base_feature_version_id=base)
    v1 = mint("feat_z", "r1")
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v1, "use_case": "fraud", "base_feature_version_id": None,
         "approval_type": "PRODUCTION"}))
    v2 = mint("feat_z", "r2", base=v1)
    v3 = mint("feat_z", "r3", base=v1)
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v2, "use_case": "fraud", "base_feature_version_id": v1,
         "approval_type": "PRODUCTION"}))
    execute_command(db, make_cmd("activate", "feature", "feat_z",
        {"feature_version_id": v3, "use_case": "fraud", "base_feature_version_id": v1,
         "approval_type": "PRODUCTION"}))
    active = db.execute("SELECT feature_version_id FROM feature_active_versions "
                        "WHERE feature_id='feat_z' AND use_case='fraud'").fetchone()[0]
    assert active == v2
    assert load_stream(db, "feature", "feat_z")[-1].type == "ACTIVATION_CONFLICT"


def test_command_double_submit_is_idempotent(db):
    cmd = make_cmd("create_request", "request", None, {"feature_concept": "double"}, idem="dup-key")
    first = execute_command(db, cmd)
    second = execute_command(db, cmd)
    assert first == second
    requests = db.execute("SELECT count(*) FROM events WHERE type='REQUEST_CREATED' "
                          "AND aggregate_id=%s", (first.aggregate_id,)).fetchone()[0]
    assert requests == 1


def test_every_catalog_action_is_registered():
    from featuregen.commands.registry import get_command
    for action in [
        "create_request", "create_run", "duplicate_of", "select_candidate", "cancel",
        "withdraw", "reject", "park", "unpark", "reopen_as_new_run", "resolve_degraded",
        "fact_confirmed_resume", "source_changed_revalidate", "activate", "supersede",
        "deprecate", "finalize_deprecate", "retier", "register_consumer", "deregister_consumer",
        "raise_monitoring_alert", "require_revalidation", "record_revalidation_outcome",
        "deactivate_expired_version",
    ]:
        assert callable(get_command(action))


def test_resolve_degraded_unblocks_run_through_execute_command(db):
    db.execute(
        "INSERT INTO run_workflow_state (run_id, request_id, current_state, table_version, "
        "degraded, degraded_reason) VALUES ('run_rd', 'req_rd', 'DRAFT', 1, true, 'boom')"
    )
    # a normal command on a degraded run is blocked...
    blocked = execute_command(db, make_cmd("park", "run", "run_rd", {"owner": "o"}))
    assert blocked.accepted is False and "degraded" in blocked.denied_reason
    # ...resolve_degraded bypasses the block and clears the flag...
    cleared = execute_command(db, make_cmd("resolve_degraded", "run", "run_rd", {}))
    assert cleared.accepted
    # ...and the run accepts commands again.
    ok = execute_command(db, make_cmd("park", "run", "run_rd", {"owner": "o"}))
    assert ok.accepted
```

2. **Run it, expect FAIL.**
   `python -m pytest tests/featuregen/aggregates/test_phase06_e2e.py -q`
   Expected: `ModuleNotFoundError: No module named 'featuregen.aggregates.commands'`.

3. **Write minimal implementation.**

```python
# src/featuregen/aggregates/commands.py
from __future__ import annotations

from featuregen.commands.registry import register_command
from featuregen.aggregates.request_aggregate import (
    create_request_command, create_run_command, duplicate_of_command, select_candidate_command,
)
from featuregen.aggregates.run_lifecycle import (
    reject_command, cancel_command, withdraw_command, park_command, unpark_command,
    reopen_as_new_run_command, resolve_degraded_command,
    fact_confirmed_resume_command, source_changed_revalidate_command,
)
from featuregen.aggregates.activation import (
    activate_command, deactivate_expired_version_command,
)
from featuregen.aggregates.consumers import (
    register_consumer_command, deregister_consumer_command,
    supersede_command, deprecate_command, finalize_deprecate_command, retier_command,
)
from featuregen.aggregates.feature_lifecycle import (
    raise_monitoring_alert_command, require_revalidation_command,
    record_revalidation_outcome_command,
)

_CATALOG = {
    "create_request": create_request_command,
    "create_run": create_run_command,
    "duplicate_of": duplicate_of_command,
    "select_candidate": select_candidate_command,
    "cancel": cancel_command,
    "withdraw": withdraw_command,
    "reject": reject_command,
    "park": park_command,
    "unpark": unpark_command,
    "reopen_as_new_run": reopen_as_new_run_command,
    "resolve_degraded": resolve_degraded_command,
    "fact_confirmed_resume": fact_confirmed_resume_command,
    "source_changed_revalidate": source_changed_revalidate_command,
    "activate": activate_command,
    "supersede": supersede_command,
    "deprecate": deprecate_command,
    "finalize_deprecate": finalize_deprecate_command,
    "retier": retier_command,
    "register_consumer": register_consumer_command,
    "deregister_consumer": deregister_consumer_command,
    "raise_monitoring_alert": raise_monitoring_alert_command,
    "require_revalidation": require_revalidation_command,
    "record_revalidation_outcome": record_revalidation_outcome_command,
    "deactivate_expired_version": deactivate_expired_version_command,
}


def register_phase06_commands() -> None:
    for action, handler in _CATALOG.items():
        register_command(action, handler)
```

**Production wiring (single entrypoint).** A running process is NOT inside pytest, so nothing implicitly registers schemas/handlers — the app must call this at startup (the conftest exercises the schema half of the same path, Task 4):

```python
# src/featuregen/aggregates/bootstrap.py  (append)
from featuregen.aggregates.commands import register_phase06_commands
from featuregen.aggregates.activation import register_phase06_handlers


def bootstrap_phase06(handler_registry) -> None:
    """Single production wiring call: event schemas (so runtime `append_event` validation
    passes) + the §4.4 command catalog + the §5.8 saga handler into Phase-04's HandlerRegistry."""
    register_phase06_event_schemas()      # idempotent (Task 3)
    register_phase06_commands()           # §4.4 catalog
    register_phase06_handlers(handler_registry)  # §5.8 activate_version handler
```

4. **Run tests, expect PASS.** Then run the whole phase suite.
   `python -m pytest tests/featuregen/aggregates/test_phase06_e2e.py -q`
   `python -m pytest tests/sp0 -q`

5. **Commit.**
   `git add -A && git commit -m "sp0-06: register §4.4 command catalog + end-to-end §12 oracles (multi-candidate, activation CAS, idempotency)" -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## §12 coverage map (this phase)

- **Concurrent versions & activation CAS / no silent overwrite:** Task 11 `test_two_runs_from_v1_later_activation_fails_cas` (non-null base) + `test_cas_claim_slot_first_writer_wins_no_silent_overwrite` (the null-base race the CAS now closes); Task 14 `test_activation_cas_oracle_through_execute_command`.
- **Approval→activation saga (mint in run tx; async feature-side step; idempotent; no half-applied):** Task 10 (`mint_feature_version`); Task 11 `test_saga_step1_mints_version_and_enqueues_in_one_tx` (step 1: `on_run_approved` mints + enqueues in one tx), `test_activate_version_handler_executes_feature_side_activation` + `test_activate_version_handler_is_idempotent` (step 2: the pure `ActivateVersionHandler` declares the activation, `commit_step` applies it), and `test_activation_is_atomic_with_step_rollback` (a failure rolls back the whole step — no orphan active-map row, event, or expiry timer).
- **Use-case-scoped & experimental activation + expiry:** Task 11 `test_use_case_scoped_coexistence`, `test_experimental_activation_schedules_expiry_timer`, `test_deactivate_expired_version_removes_active_entry`.
- **Multi-candidate / request aggregate (mint/bind/close siblings, explored count, 1:n); provenance.artifact_type is a §3.7 enum value:** Task 7 (`test_select_candidate_*`, incl. the `artifact_type == "APPROVAL_RECORD"` assertion) + Task 14.
- **Lifecycle (`PRODUCTION→MONITORING_ALERT→REVALIDATION_REQUIRED→…`; revalidation-change spawns run; `fact_confirmed_resume` wakes parked runs; `deprecate` blocked with consumers; forced `deprecate` impact-analysis + quiesce/grace → `finalize_deprecate`; `SOURCE_CHANGED_REVALIDATE`):** Tasks 9, 12 (`test_force_deprecate_quiesces_with_grace_not_immediate_deprecation`, `test_finalize_deprecate_completes_after_grace_and_is_idempotent`), 13.
- **Misc (`park`/`unpark`/`reopen_as_new_run` linked to rejected; `DUPLICATE_OF` first-committed race; command idempotency sequential + concurrent-claim semantics + denials not cached; degraded block + `resolve_degraded` unblock; runtime schema bootstrap):** Tasks 3 (`test_bootstrap_*`), 4 (`test_denied_command_is_not_cached`, `test_accepted_command_stores_final_non_pending_result`, `test_replay_does_not_rerun_handler_when_prior_committed`), 5, 6, 8 (`test_resolve_degraded_clears_flag`), 14 (`test_resolve_degraded_unblocks_run_through_execute_command`).
- **Out of this phase (consumed only):** **authz denial → `security_audit`** and break-glass review — Phase 07 plugs the real authorizer into the Task-4 seam; per the seam contract the *authorizer* writes denials to `security_audit` (this is how `execute_command` fulfils the contract's "on deny, writes to security_audit"). Guard-failure `GUARD_FAILED`/`TRANSITION_REJECTED` and table versioning (Phase 03); timer ladder firing/recovery — incl. the `experiment_expiry` and `business_repair` (quiesce-grace) timers this phase schedules (Phase 05); outbox relay + the queue worker (`process_one`) that dispatches `request_activation`'s queue rows to the **`activate_version`** handler (Phase 04).

### Naming reconciliations (reviewer notes)

- **One saga step, two intentional entrypoints, one handler name.** The async §5.8 feature-side step is the `activate_version` **handler** (`queue.handler == ActivateVersionHandler.name == "activate_version"`, dispatched by Phase-04's `process_one`). The synchronous §4.4 lifecycle command is the `activate` **action**. Both delegate to `apply_activation`; they are deliberately distinct, so the worker's `registry.get(queue.handler)` always resolves.
- **Concept-claim placement (§2 vs §4.4).** §2 prose says "`create_run`/intake places the claim," but the §4.4 catalog row is `create_request | Q | Open a request; place concept-claim (§2)`. This phase follows the **§4.4 catalog**: the claim + `DUPLICATE_OF` live in `create_request_command` (Task 5) — the request is the concept's home and `create_request` is the intake entrypoint that mints `request_id`. (Reconciles the spec's internal §2/§4.4 wording difference.)
