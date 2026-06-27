## Phase 04: Durable runtime I — atomic boundary, outbox, queue, idempotency

**Goal:** Implement the §5.1 single atomic-step transaction (append events + insert documents + record processed-message ledger + insert outbox, one DB tx, OCC rollback), the §5.2 transactional outbox + leased relay (publish-then-mark-sent, DLQ, stuck-message reclaim, backpressure depth), the §5.2 worker queue (`SELECT … FOR UPDATE SKIP LOCKED`, partitioned by aggregate key, one in-flight lease per partition), the §5.3 idempotent step dispatcher + processed-message ledger (keyed by message id, pruned by `global_seq` watermark), and §5.7 automatic crash recovery — all against the shared contract, consumed by Phases 05/06.

### Boundary notes (what this phase does and does NOT do)

- **The atomic boundary `commit_step` persists the four participants whose tables exist by this phase and whose semantics this phase owns:** domain events (via Phase 01 `append_event`, OCC), a frozen document (INSERT into Phase 02's `documents` — Phase 02 owns the write-once trigger + DAG/`derived_from` validation; this phase only inserts the row respecting the contract columns), the processed-message ledger (`processed_messages`, owned here), and outbox messages (`outbox`, owned here). The two remaining §5.1 participants — **`timers`** and **`external_commands`** — have tables, CAS-on-task-version, dispatcher, and stale-result semantics owned by **Phase 05**. So `commit_step` **raises** if a `HandlerResult` carries `timers` or `external_commands` (nothing is ever silently dropped); **Phase 05 extends `commit_step`** to insert them when it creates those tables. This is the same intentional cross-phase extension pattern the overview calls out (Phase 05 hooks into Phase 04's step). **Explicit divergence note:** this phase's scope line names "upsert timers" as a one-tx participant of the §5.1 boundary, but the `timers` table is owned by and does not exist until Phase 05 (per the overview's phase-ownership table). We therefore defer ONLY the *persistence* of timers/external-commands to Phase 05 while keeping the boundary's all-or-nothing contract intact: `commit_step` refuses (raises) rather than partially honoring a step it cannot atomically complete. The single-tx append-events + insert-docs + record-ledger + insert-outbox boundary is fully implemented and tested here.
- **`commit_step` does NOT open/commit a transaction.** Per the shared contract ("every function that mutates participates in the caller's open transaction"), it runs inside the caller's open tx. The dispatcher (`process_one`) owns the transaction/savepoint structure so an OCC failure rolls back only the step writes.
- **Outbox messages are derived, not handler-supplied.** `HandlerResult` has no outbox field; the mechanism emits exactly **one outbox row per committed event** (`message_id = event_id`, `partition_key` = aggregate key, `topic = event.type`). Fan-out to multiple topics/consumers is a later concern; one row per event is the MVP and is idempotent (`ON CONFLICT (message_id) DO NOTHING`).
- **Routing (topic → handler) is policy, not mechanism.** The relay's `publish` callable is the integration seam: `make_queue_publisher(route)` enqueues a worker-queue row only for topics present in the caller-supplied route map. SP-1+ owns the real route map; this phase ships the mechanism + a test route.
- **Worker-queue handlers are run-scoped.** Per the handler contract ("MUST NOT emit feature-/request-stream events, write outside its `run_id`"), `process_one` builds a `HandlerContext` for the **run** aggregate. Feature-/request-stream events still get outbox rows (for the relay / other consumers) but are not dispatched to step handlers here.
- **`HandlerContext.documents` loading is injected.** The contract types it `Mapping[str, NewDocument]` keyed by stage; resolving "current primary per stage" is Phase 02's `stage_primary` projection. To stay decoupled, the dispatcher takes a `document_loader` callable (default returns `{}`); Phase 02/SP-1+ wires the real loader. This phase does not read mutable projections to build guards (it only loads frozen inputs via the injected loader).

### File structure

```
src/sp0/migrations/
    0040_runtime_core.sql           # DDL: outbox, queue, processed_messages (verbatim from contract)
src/sp0/runtime/
    __init__.py                     # package marker
    backoff.py                      # compute_backoff — shared by outbox relay + worker queue
    ledger.py                       # processed_messages: is_processed/record_processed/prune/watermark
    outbox.py                       # OutboxMessage, insert/derive, leased relay, DLQ, reclaim, depth, queue publisher
    queue.py                        # QueueClaim, enqueue, SKIP-LOCKED claim_one, complete/fail/reclaim
    step.py                         # commit_step (the §5.1 atomic boundary), StepCommit, document insert
    handlers.py                     # HandlerRegistry (register/get; re-registration is an error)
    dispatch.py                     # process_one (idempotent), HandlerTimeout, recover_stuck (§5.7)
tests/sp0/runtime/
    conftest.py                     # registers test event types into event_registry; actor/prov/seed helpers
    test_schema.py                  # DDL shape + constraints (status checks, one-inflight-per-partition)
    test_backoff.py                 # deterministic doubling, cap, jitter bounds
    test_ledger.py                  # is/record/duplicate-PK; prune by min checkpoint watermark
    test_outbox.py                  # derive-from-events, partition key, relay publish-then-sent, DLQ, reclaim, depth
    test_queue.py                   # enqueue idempotent, SKIP-LOCKED claim, partition exclusion, complete/fail/reclaim
    test_step.py                    # OCC chaining, doc insert, outbox fan, ledger row; timers/external guard; rollback
    test_dispatch.py                # end-to-end claim→handle→commit; duplicate skip; retryable/permanent; timeout; recovery
```

**Consumed from earlier phases / shared contract** (import, never redefine):
- `sp0.contracts`: `EventEnvelope`, `NewEvent`, `IdentityEnvelope`, `ProvenanceEnvelope`, `ConcurrencyError`, `Handler`, `HandlerResult`, `HandlerContext`, `Disposition`, `NewDocument`, `NewExternalCommand`, `NewTimer`. *(These dataclasses/Protocols are the shared "Core interfaces" block, seeded verbatim in `src/sp0/contracts/` by Phase 01. This phase imports them and is **authoritative for the concrete runtime behaviour** of `Handler`, `HandlerResult`, `HandlerContext`, `Disposition`, `NewExternalCommand`, `NewTimer` — it does not redefine their shapes.)*
- `sp0.event_store` (Phase 01): `append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope`; `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None) -> list[EventEnvelope]`; the module-level `event_registry` instance `append_event` validates against (exposes `register_schema(type_name, schema_version, json_schema, owner, *, status="active")`). *If Phase 01 placed these at a different import path, reconcile the path only — do not redefine the symbols.*
- `src/sp0/migrations/*.sql` (Phase 01: `global_seq_seq`, `events`, `projection_checkpoints`; Phase 02: `documents` + write-once trigger). Applied by the `db` fixture.
- `tests/sp0/conftest.py` (Phase 01): the `db` fixture — a psycopg connection to a fresh database with every `src/sp0/migrations/*.sql` applied (per-test transaction, rolled back after each test). Inherited by `tests/sp0/runtime/`.

> **INTEGRATION CONTRACT ADDENDUM — `event_registry` instance (raise to the overview if Phase 01 disagrees):** the shared contract's Phase-01 "Key Produces" names the event `SchemaRegistry` + `register_schema`/`register_upcaster`/`upcast`/`snapshot_version`, but it does NOT name the *instance* that `append_event` validates against. Every downstream phase's tests need that instance to register test event types. **This phase therefore requires Phase 01 to expose its event-registry singleton as `sp0.event_store.event_registry`** — the object whose `register_schema(type_name, schema_version, json_schema, owner, *, status="active")` matches the contract. This is the single reconciliation point: if Phase 01 exposes it under a different name/path, change ONLY the import in `tests/sp0/runtime/conftest.py` — do not fork the registry, and do not depend on any registry method beyond the contract's `register_schema`. (Per the overview's "Notes for phase authors", if Phase 01 cannot provide this seam, raise it back to the overview rather than diverging.)

**This phase is authoritative for** these contract symbols' concrete behaviour (shapes are the contract verbatim): `Handler`, `HandlerResult`, `HandlerContext`, `Disposition`, `NewExternalCommand`, `NewTimer`; plus the new runtime symbols listed under each task's **Produces**.

---

## Task 1 — DDL migration: outbox, queue, processed_messages

**Files:**
- Create: `src/sp0/migrations/0040_runtime_core.sql`
- Test: `tests/sp0/runtime/test_schema.py`

**Interfaces:**
- Consumes: `db` fixture (Phase 01) — applies all `src/sp0/migrations/*.sql`; `events` table (Phase 01) for the `outbox.caused_by_event` / `processed_messages.result_event_id` FKs.
- Produces: tables `outbox`, `queue`, `processed_messages` (+ indexes) exactly as the shared contract declares, including `queue_one_inflight_per_partition` (partial unique on `partition_key WHERE status='leased'`) and `processed_messages` PK on `message_id`.

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_schema.py`:

```python
from __future__ import annotations

import psycopg
import pytest


def _columns(db, table: str) -> set[str]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            (table,),
        )
        return {r[0] for r in cur.fetchall()}


def test_outbox_has_contract_columns(db) -> None:
    assert {
        "id", "message_id", "partition_key", "topic", "payload", "caused_by_event",
        "status", "lease_owner", "lease_expires_at", "attempts", "max_attempts",
        "next_attempt_at", "last_error", "created_at", "sent_at",
    } <= _columns(db, "outbox")


def test_queue_has_contract_columns(db) -> None:
    assert {
        "id", "message_id", "partition_key", "handler", "payload", "status",
        "lease_owner", "lease_expires_at", "attempts", "max_attempts",
        "available_at", "priority", "last_error", "created_at",
    } <= _columns(db, "queue")


def test_processed_messages_has_contract_columns(db) -> None:
    assert {
        "message_id", "aggregate", "aggregate_id", "result_event_id",
        "processed_seq", "processed_at",
    } <= _columns(db, "processed_messages")


def test_message_id_unique_on_outbox(db) -> None:
    ins = (
        "INSERT INTO outbox (message_id, partition_key, topic, payload) "
        "VALUES ('m1', 'run:r1', 'T', '{}'::jsonb)"
    )
    with db.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)


def test_outbox_status_check_rejects_unknown(db) -> None:
    with db.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status) "
            "VALUES ('m2', 'run:r1', 'T', '{}'::jsonb, 'bogus')"
        )


def test_queue_one_inflight_per_partition(db) -> None:
    base = (
        "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
        "lease_owner, lease_expires_at) VALUES (%s, 'run:r1', 'h', '{}'::jsonb, "
        "'leased', 'w1', now())"
    )
    with db.cursor() as cur:
        cur.execute(base, ("q1",))
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(base, ("q2",))


def test_processed_messages_pk_is_message_id(db) -> None:
    ins = (
        "INSERT INTO processed_messages (message_id, aggregate, aggregate_id, "
        "processed_seq) VALUES ('m3', 'run', 'r1', 5)"
    )
    with db.cursor() as cur:
        cur.execute(ins)
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(ins)
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_schema.py -q`. Expected: `psycopg.errors.UndefinedTable: relation "outbox" does not exist` (the migration file does not exist yet).

3. **Write minimal implementation** — `src/sp0/migrations/0040_runtime_core.sql` (verbatim from the shared contract):

```sql
-- Phase 04: durable runtime I — transactional outbox, worker queue, idempotency ledger.

-- outbox — transactional outbox + leased relay (§5.2). Partitioned by aggregate key.
CREATE TABLE outbox (
    id               bigserial   PRIMARY KEY,
    message_id       text        NOT NULL UNIQUE,                 -- consumer idempotency key
    partition_key    text        NOT NULL,                        -- 'run:...' | 'feature:...' | 'request:...'
    topic            text        NOT NULL,
    payload          jsonb       NOT NULL,
    caused_by_event  text        NULL REFERENCES events(event_id),
    status           text        NOT NULL DEFAULT 'pending'
                         CHECK (status IN ('pending','leased','sent','dead')),
    lease_owner      text        NULL,
    lease_expires_at timestamptz NULL,
    attempts         integer     NOT NULL DEFAULT 0,
    max_attempts     integer     NOT NULL DEFAULT 12,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    last_error       text        NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz NULL
);
CREATE INDEX outbox_dispatch_idx  ON outbox (status, next_attempt_at) WHERE status IN ('pending','leased');
CREATE INDEX outbox_partition_idx ON outbox (partition_key, id);

-- queue — worker queue, claimed via SELECT ... FOR UPDATE SKIP LOCKED (§5.2).
CREATE TABLE queue (
    id               bigserial   PRIMARY KEY,
    message_id       text        NOT NULL UNIQUE,                 -- idempotency
    partition_key    text        NOT NULL,                        -- aggregate key
    handler          text        NOT NULL,                        -- registered step-handler name
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'ready'
                         CHECK (status IN ('ready','leased','done','dead')),
    lease_owner      text        NULL,
    lease_expires_at timestamptz NULL,
    attempts         integer     NOT NULL DEFAULT 0,
    max_attempts     integer     NOT NULL DEFAULT 12,
    available_at     timestamptz NOT NULL DEFAULT now(),
    priority         integer     NOT NULL DEFAULT 100,
    last_error       text        NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queue_claim_idx ON queue (priority, available_at, id) WHERE status = 'ready';
-- per-aggregate serialization: only one in-flight lease per partition
CREATE UNIQUE INDEX queue_one_inflight_per_partition ON queue (partition_key) WHERE status = 'leased';
-- Worker claim pattern:
--   SELECT * FROM queue
--    WHERE status='ready' AND available_at <= now()
--      AND partition_key NOT IN (SELECT partition_key FROM queue WHERE status='leased')
--    ORDER BY priority, available_at, id
--    FOR UPDATE SKIP LOCKED LIMIT 1;

-- processed_messages — idempotency ledger (§5.3). Pruned by global_seq watermark.
CREATE TABLE processed_messages (
    message_id      text        PRIMARY KEY,
    aggregate       text        NOT NULL,
    aggregate_id    text        NOT NULL,
    result_event_id text        NULL REFERENCES events(event_id),
    processed_seq   bigint      NOT NULL,                         -- global_seq at processing time
    processed_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX processed_messages_prune_idx ON processed_messages (processed_seq);
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_schema.py -q`. Expected: 7 passed.

5. **Commit:**

```
git add src/sp0/migrations/0040_runtime_core.sql tests/sp0/runtime/test_schema.py
git commit -m "SP-0 Phase 04: outbox, queue, processed_messages (DDL)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2 — Backoff utility (shared by relay + queue)

**Files:**
- Create: `src/sp0/runtime/__init__.py`
- Create: `src/sp0/runtime/backoff.py`
- Test: `tests/sp0/runtime/test_backoff.py`

**Interfaces:**
- Consumes: nothing (pure stdlib).
- Produces: `compute_backoff(attempts: int, *, base_seconds: float = 1.0, cap_seconds: float = 3600.0, jitter: float = 0.5) -> float` — exponential backoff (`base * 2**(attempts-1)`), capped at `cap_seconds`, with symmetric ±`jitter` fraction; deterministic when `jitter=0.0`.

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_backoff.py`:

```python
from __future__ import annotations

from sp0.runtime.backoff import compute_backoff


def test_doubling_without_jitter() -> None:
    assert compute_backoff(1, base_seconds=2.0, jitter=0.0) == 2.0
    assert compute_backoff(2, base_seconds=2.0, jitter=0.0) == 4.0
    assert compute_backoff(3, base_seconds=2.0, jitter=0.0) == 8.0


def test_cap_applied() -> None:
    assert compute_backoff(40, base_seconds=1.0, cap_seconds=60.0, jitter=0.0) == 60.0


def test_floor_on_zero_or_negative_attempts() -> None:
    # treated as the first attempt
    assert compute_backoff(0, base_seconds=1.0, jitter=0.0) == 1.0
    assert compute_backoff(-5, base_seconds=1.0, jitter=0.0) == 1.0


def test_jitter_stays_within_bounds_and_nonnegative() -> None:
    for _ in range(200):
        v = compute_backoff(3, base_seconds=2.0, cap_seconds=100.0, jitter=0.5)
        # raw=8.0; ±50% => [4.0, 12.0]; never negative
        assert 0.0 <= v <= 12.0
        assert v >= 4.0
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_backoff.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.backoff'`.

3. **Write minimal implementation** — first `src/sp0/runtime/__init__.py`:

```python
"""SP-0 Phase 04: durable runtime I — atomic boundary, outbox, queue, idempotency (§5.1–5.3, §5.7)."""
```

Then `src/sp0/runtime/backoff.py`:

```python
from __future__ import annotations

import random


def compute_backoff(
    attempts: int,
    *,
    base_seconds: float = 1.0,
    cap_seconds: float = 3600.0,
    jitter: float = 0.5,
) -> float:
    """Exponential backoff with cap + symmetric jitter (§5.6 delivery retry).

    attempts < 1 is treated as the first attempt. Deterministic when jitter == 0.0.
    """
    n = attempts if attempts >= 1 else 1
    raw = min(base_seconds * (2 ** (n - 1)), cap_seconds)
    if jitter <= 0.0:
        return raw
    delta = raw * jitter
    return max(0.0, raw + random.uniform(-delta, delta))
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_backoff.py -q`. Expected: 4 passed.

5. **Commit:**

```
git add src/sp0/runtime/__init__.py src/sp0/runtime/backoff.py tests/sp0/runtime/test_backoff.py
git commit -m "SP-0 Phase 04: shared exponential-backoff utility

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3 — Processed-message ledger (idempotency + watermark pruning)

**Files:**
- Create: `src/sp0/runtime/ledger.py`
- Create: `tests/sp0/runtime/conftest.py`
- Test: `tests/sp0/runtime/test_ledger.py`

**Interfaces:**
- Consumes: `db` fixture; `projection_checkpoints` (Phase 01) for the prune watermark; `processed_messages` (Task 1).
- Produces:
  - `is_processed(conn, message_id: str) -> bool`
  - `record_processed(conn, *, message_id: str, aggregate: str, aggregate_id: str, result_event_id: str | None, processed_seq: int) -> None`
  - `processed_watermark(conn) -> int` — `MIN(checkpoint_seq)` across `projection_checkpoints` (0 if none)
  - `prune_processed_messages(conn) -> int` — deletes rows with `processed_seq < watermark`; returns deleted count

### TDD steps

1. **Write the failing test** — first the shared fixtures `tests/sp0/runtime/conftest.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from sp0.event_store import append_event, event_registry

_PERMISSIVE = {"type": "object"}


@pytest.fixture(scope="session", autouse=True)
def _register_runtime_test_event_types():
    # Requires the Phase-01 event-registry singleton at sp0.event_store.event_registry
    # (see the INTEGRATION CONTRACT ADDENDUM in the phase plan). We register once per session;
    # a duplicate registration (the singleton may persist across modules) is tolerated, but we
    # re-confirm the type is registered via validate() so a REAL integration break (wrong name,
    # wrong signature) is surfaced rather than swallowed.
    for type_name in ("STEP_TRIGGER", "STEP_DONE", "STEP_NEXT"):
        try:
            event_registry.register_schema(
                type_name, 1, _PERMISSIVE, owner="sp0-runtime-tests"
            )
        except Exception:  # noqa: BLE001 — only an idempotent re-registration is acceptable
            event_registry.validate(type_name, 1, {})  # re-raises if NOT actually registered
    yield


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
```

Then `tests/sp0/runtime/test_ledger.py`:

```python
from __future__ import annotations

import psycopg
import pytest

from sp0.runtime.ledger import (
    is_processed,
    processed_watermark,
    prune_processed_messages,
    record_processed,
)


def test_record_then_is_processed(db) -> None:
    assert is_processed(db, "m1") is False
    record_processed(
        db, message_id="m1", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=10,
    )
    assert is_processed(db, "m1") is True


def test_duplicate_record_violates_pk(db) -> None:
    record_processed(
        db, message_id="m2", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=10,
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        record_processed(
            db, message_id="m2", aggregate="run", aggregate_id="r1",
            result_event_id=None, processed_seq=11,
        )


def test_watermark_zero_when_no_projections(db) -> None:
    assert processed_watermark(db) == 0


def test_prune_deletes_below_min_checkpoint(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq, head_seq) "
            "VALUES ('p_a', 100, 100), ('p_b', 60, 200)"
        )
    record_processed(
        db, message_id="old", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=50,
    )
    record_processed(
        db, message_id="keep", aggregate="run", aggregate_id="r1",
        result_event_id=None, processed_seq=70,
    )
    # watermark = min(100, 60) = 60 -> only processed_seq < 60 is pruned
    assert processed_watermark(db) == 60
    assert prune_processed_messages(db) == 1
    assert is_processed(db, "old") is False
    assert is_processed(db, "keep") is True
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_ledger.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.ledger'`.

3. **Write minimal implementation** — `src/sp0/runtime/ledger.py`:

```python
from __future__ import annotations

from typing import Optional

import psycopg


def is_processed(conn: psycopg.Connection, message_id: str) -> bool:
    """True if this message id has already produced its one effect (§5.3)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM processed_messages WHERE message_id = %s", (message_id,)
        )
        return cur.fetchone() is not None


def record_processed(
    conn: psycopg.Connection,
    *,
    message_id: str,
    aggregate: str,
    aggregate_id: str,
    result_event_id: Optional[str],
    processed_seq: int,
) -> None:
    """Record that message_id was processed at global_seq=processed_seq (§5.3).
    The PRIMARY KEY on message_id makes a concurrent duplicate roll back the tx."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_messages "
            "(message_id, aggregate, aggregate_id, result_event_id, processed_seq) "
            "VALUES (%s, %s, %s, %s, %s)",
            (message_id, aggregate, aggregate_id, result_event_id, processed_seq),
        )


def processed_watermark(conn: psycopg.Connection) -> int:
    """Min applied checkpoint across all projections; ledger rows at/above this are
    still needed for in-flight projection replay (§5.3). 0 when no projections exist."""
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MIN(checkpoint_seq), 0) FROM projection_checkpoints")
        return int(cur.fetchone()[0])


def prune_processed_messages(conn: psycopg.Connection) -> int:
    """Delete ledger rows below the watermark; returns the number deleted."""
    watermark = processed_watermark(conn)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM processed_messages WHERE processed_seq < %s", (watermark,)
        )
        return cur.rowcount
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_ledger.py -q`. Expected: 4 passed.

5. **Commit:**

```
git add src/sp0/runtime/ledger.py tests/sp0/runtime/conftest.py tests/sp0/runtime/test_ledger.py
git commit -m "SP-0 Phase 04: processed-message idempotency ledger + watermark prune

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4 — Outbox writer + derive-one-row-per-event + partition key

**Files:**
- Create: `src/sp0/runtime/outbox.py`
- Test: `tests/sp0/runtime/test_outbox.py` (this task adds the writer/derive tests; Task 5 adds relay tests to the same file)

**Interfaces:**
- Consumes: `EventEnvelope` (contract); `outbox` (Task 1); `db` + `seed_run_event` (conftest).
- Produces:
  - `OutboxMessage` — `@dataclass(frozen=True, slots=True)` with `message_id: str`, `partition_key: str`, `topic: str`, `payload: Mapping[str, Any]`, `caused_by_event: str | None = None`.
  - `partition_key_for(event: EventEnvelope) -> str` — `run:{run_id}` | `feature:{feature_id}` | `request:{request_id}` (§5.2 aggregate-key partitioning).
  - `outbox_messages_for_events(events: Iterable[EventEnvelope]) -> tuple[OutboxMessage, ...]` — one row per event, `message_id = event_id`.
  - `insert_outbox_message(conn, msg: OutboxMessage) -> int` — idempotent (`ON CONFLICT (message_id) DO NOTHING`); returns the outbox row id.

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_outbox.py`:

```python
from __future__ import annotations

import pytest

from sp0.runtime.outbox import (
    OutboxMessage,
    insert_outbox_message,
    outbox_messages_for_events,
    partition_key_for,
)


def test_partition_key_per_aggregate(db, seed_run_event) -> None:
    ev = seed_run_event("run_p1")
    assert partition_key_for(ev) == "run:run_p1"


def test_derive_one_message_per_event(db, seed_run_event) -> None:
    ev = seed_run_event("run_d1", type="STEP_TRIGGER")
    msgs = outbox_messages_for_events([ev])
    assert len(msgs) == 1
    m = msgs[0]
    assert m.message_id == ev.event_id
    assert m.partition_key == "run:run_d1"
    assert m.topic == "STEP_TRIGGER"
    assert m.caused_by_event == ev.event_id
    assert m.payload["event_id"] == ev.event_id
    assert m.payload["run_id"] == "run_d1"


def test_insert_is_idempotent_on_message_id(db, seed_run_event) -> None:
    ev = seed_run_event("run_i1")
    (m,) = outbox_messages_for_events([ev])
    first = insert_outbox_message(db, m)
    second = insert_outbox_message(db, m)  # duplicate publish -> same row
    assert first == second
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox WHERE message_id = %s", (m.message_id,))
        assert cur.fetchone()[0] == 1


def test_partition_key_for_unknown_aggregate_raises() -> None:
    class _Fake:
        aggregate = "bogus"
        run_id = feature_id = request_id = aggregate_id = "x"

    with pytest.raises(ValueError):
        partition_key_for(_Fake())  # type: ignore[arg-type]
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_outbox.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.outbox'`.

3. **Write minimal implementation** — `src/sp0/runtime/outbox.py` (this task's portion; Task 5 appends the relay functions to the same file):

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import psycopg
from psycopg.types.json import Json

from sp0.contracts import EventEnvelope


@dataclass(frozen=True, slots=True)
class OutboxMessage:
    """A to-be-published transactional-outbox message (§5.2)."""
    message_id: str
    partition_key: str
    topic: str
    payload: Mapping[str, Any]
    caused_by_event: str | None = None


def partition_key_for(event: EventEnvelope) -> str:
    """Aggregate-key partition (§5.2): feature-/request-stream events (run_id null)
    still get per-aggregate ordering."""
    if event.aggregate == "run":
        return f"run:{event.run_id or event.aggregate_id}"
    if event.aggregate == "feature":
        return f"feature:{event.feature_id or event.aggregate_id}"
    if event.aggregate == "request":
        return f"request:{event.request_id or event.aggregate_id}"
    raise ValueError(f"unknown aggregate {event.aggregate!r}")


def outbox_messages_for_events(
    events: Iterable[EventEnvelope],
) -> tuple[OutboxMessage, ...]:
    """One outbox row per committed event; message_id = event_id (idempotency key)."""
    out: list[OutboxMessage] = []
    for e in events:
        out.append(
            OutboxMessage(
                message_id=e.event_id,
                partition_key=partition_key_for(e),
                topic=e.type,
                payload={
                    "event_id": e.event_id,
                    "aggregate": e.aggregate,
                    "aggregate_id": e.aggregate_id,
                    "run_id": e.run_id,
                    "feature_id": e.feature_id,
                    "request_id": e.request_id,
                    "type": e.type,
                    "global_seq": e.global_seq,
                    "stream_version": e.stream_version,
                },
                caused_by_event=e.event_id,
            )
        )
    return tuple(out)


def insert_outbox_message(conn: psycopg.Connection, msg: OutboxMessage) -> int:
    """Insert one outbox row inside the caller's open tx; idempotent on message_id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, caused_by_event) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (msg.message_id, msg.partition_key, msg.topic, Json(msg.payload), msg.caused_by_event),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute("SELECT id FROM outbox WHERE message_id = %s", (msg.message_id,))
        return int(cur.fetchone()[0])
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_outbox.py -q`. Expected: 4 passed.

5. **Commit:**

```
git add src/sp0/runtime/outbox.py tests/sp0/runtime/test_outbox.py
git commit -m "SP-0 Phase 04: outbox writer + derive-per-event + aggregate-key partitioning

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5 — Leased relay: publish-then-mark-sent, DLQ, stuck-message reclaim, backpressure

**Files:**
- Modify: `src/sp0/runtime/outbox.py` (append relay functions)
- Modify: `tests/sp0/runtime/test_outbox.py` (append relay tests)

**Interfaces:**
- Consumes: `compute_backoff` (Task 2); `enqueue` (Task 6 — for `make_queue_publisher`); `outbox` (Task 1).
- Produces:
  - `relay_publish_batch(conn, publish: Callable[[psycopg.Connection, OutboxMessage], None], *, owner: str, lease_seconds: int = 30, batch: int = 100) -> int` — the classic three-step leased relay (§5.2): **(1)** lease a batch of `pending` rows (`FOR UPDATE SKIP LOCKED`) in its **own** transaction and commit so the lease is durable; **(2)** call `publish` for each leased row outside that transaction; **(3)** mark `sent` in its own transaction. A crash between (2) and (3) leaves the row `leased` → `reclaim_stuck_outbox` returns it to `pending` → a harmless at-least-once duplicate (§5.3). On a `publish` exception, backs off (`pending` + `next_attempt_at`) or routes to DLQ (`status='dead'`) once `attempts >= max_attempts`; on a `BackpressureError` (Task 6), leaves the row `pending` with a delay and **no** attempt bump (durable waiting, not a failure). Returns the count marked `sent`.
  - `reclaim_stuck_outbox(conn) -> int` — resets `leased` rows whose lease expired back to `pending` (§5.2 stuck-message detection / §5.7 crash recovery); returns count. Meaningful precisely because `relay_publish_batch` commits the lease in its own transaction.
  - `outbox_pending_depth(conn) -> int` — count of `pending`+`leased` rows (relay-side backlog signal, §5.2).
  - `make_queue_publisher(route: Mapping[str, str], *, max_partition_depth: int | None = None) -> Callable[[psycopg.Connection, OutboxMessage], None]` — a `publish` that enqueues a worker-queue row for topics present in `route` (topic → handler name), skipping others. When `max_partition_depth` is set, it is **admission control / §5.2 backpressure**: if the target partition already holds that many `ready`+`leased` queue items (`queue_depth`), it raises `BackpressureError` so the relay leaves the outbox row durably `pending` (durable waiting) until the worker queue drains — bounding per-partition work-in-progress without dropping or failing work.

> **Relay transaction model (why this differs from the §5.1 step boundary):** the relay is a **background daemon, not a §5.1 step participant**, so it OWNS its transaction structure rather than running inside a caller's open tx. Each `with conn.transaction()` block in `relay_publish_batch` is a durable **COMMIT** when the relay runs on its own dedicated autocommit connection (production), and a **SAVEPOINT** under the per-test transactional `db` fixture (so the tests observe the same state machine and roll back cleanly). This is what makes `leased` status durable and `reclaim_stuck_outbox` meaningful, realizing publish-then-mark-sent with at-least-once redelivery. The contract's "every mutating function participates in the caller's open transaction" governs the step-boundary mutators (`commit_step` and friends); it does not constrain this daemon.

### TDD steps

1. **Write the failing test** — append to `tests/sp0/runtime/test_outbox.py`:

```python
from sp0.runtime.outbox import (
    make_queue_publisher,
    outbox_pending_depth,
    reclaim_stuck_outbox,
    relay_publish_batch,
)
from sp0.runtime.queue import enqueue


def _seed_pending(db, message_id: str, topic: str = "STEP_TRIGGER") -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload) "
            "VALUES (%s, 'run:r1', %s, '{}'::jsonb)",
            (message_id, topic),
        )


def test_relay_publishes_then_marks_sent(db) -> None:
    _seed_pending(db, "rp1")
    published: list[str] = []

    def publish(conn, msg) -> None:
        published.append(msg.message_id)

    assert relay_publish_batch(db, publish, owner="relay1") == 1
    assert published == ["rp1"]
    with db.cursor() as cur:
        cur.execute("SELECT status, sent_at FROM outbox WHERE message_id = 'rp1'")
        status, sent_at = cur.fetchone()
    assert status == "sent"
    assert sent_at is not None


def test_relay_backoff_on_publish_failure(db) -> None:
    _seed_pending(db, "rp2")

    def publish(conn, msg) -> None:
        raise RuntimeError("downstream down")

    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, last_error FROM outbox WHERE message_id = 'rp2'"
        )
        status, attempts, last_error = cur.fetchone()
    assert status == "pending"
    assert attempts == 1
    assert "downstream down" in last_error


def test_relay_routes_to_dlq_at_max_attempts(db) -> None:
    _seed_pending(db, "rp3")
    with db.cursor() as cur:
        cur.execute("UPDATE outbox SET attempts = max_attempts - 1 WHERE message_id = 'rp3'")

    def publish(conn, msg) -> None:
        raise RuntimeError("still down")

    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute("SELECT status FROM outbox WHERE message_id = 'rp3'")
        assert cur.fetchone()[0] == "dead"


def test_reclaim_stuck_outbox(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('rs1', 'run:r1', 'T', '{}'::jsonb, "
            "'leased', 'dead-relay', now() - interval '1 minute')"
        )
    assert reclaim_stuck_outbox(db) == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM outbox WHERE message_id = 'rs1'")
        status, owner = cur.fetchone()
    assert status == "pending"
    assert owner is None


def test_pending_depth_counts_pending_and_leased(db) -> None:
    _seed_pending(db, "pd1")
    _seed_pending(db, "pd2")
    assert outbox_pending_depth(db) == 2


def test_make_queue_publisher_enqueues_routed_topics_only(db) -> None:
    _seed_pending(db, "qp1", topic="STEP_TRIGGER")
    _seed_pending(db, "qp2", topic="UNROUTED")
    publish = make_queue_publisher({"STEP_TRIGGER": "my_handler"})
    assert relay_publish_batch(db, publish, owner="relay1") == 2  # both marked sent
    with db.cursor() as cur:
        cur.execute("SELECT message_id, handler FROM queue ORDER BY message_id")
        rows = cur.fetchall()
    assert rows == [("qp1", "my_handler")]  # qp2 unrouted -> no queue row


def test_backpressure_holds_outbox_pending_without_failing(db) -> None:
    _seed_pending(db, "bp1", topic="STEP_TRIGGER")  # partition run:r1
    # saturate the run:r1 worker-queue partition up to the admission limit
    enqueue(db, message_id="bp_pre", partition_key="run:r1", handler="h", payload={})
    publish = make_queue_publisher({"STEP_TRIGGER": "h"}, max_partition_depth=1)
    # nothing is published while the partition is at capacity -> durable waiting
    assert relay_publish_batch(db, publish, owner="relay1") == 0
    with db.cursor() as cur:
        cur.execute("SELECT status, attempts FROM outbox WHERE message_id='bp1'")
        status, attempts = cur.fetchone()
    assert status == "pending"  # held durably, not failed
    assert attempts == 0        # backpressure is NOT a failure: no attempt bump, no DLQ
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id='bp1'")
        assert cur.fetchone()[0] == 0  # not enqueued while saturated
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_outbox.py -q`. Expected: `ImportError: cannot import name 'relay_publish_batch' from 'sp0.runtime.outbox'`.

3. **Write minimal implementation** — append to `src/sp0/runtime/outbox.py`. First extend the imports at the top of the file:

```python
from typing import Callable

from psycopg.rows import dict_row

from sp0.runtime.backoff import compute_backoff
from sp0.runtime.queue import BackpressureError, enqueue, queue_depth
```

Then append the relay functions:

```python
def relay_publish_batch(
    conn: psycopg.Connection,
    publish: Callable[[psycopg.Connection, OutboxMessage], None],
    *,
    owner: str,
    lease_seconds: int = 30,
    batch: int = 100,
) -> int:
    """Three-step leased relay (§5.2). The relay is a BACKGROUND DAEMON, not a §5.1 step
    participant: it OWNS its transactions. Each `with conn.transaction()` below is a durable
    COMMIT when the relay runs on its own autocommit connection (production) and a SAVEPOINT
    under the per-test transactional `db` fixture.

      Step 1 (own tx): lease a batch of `pending` rows (`FOR UPDATE SKIP LOCKED`) and COMMIT,
        so the lease is durable — a relay crash leaves a 'stuck' leased row that
        reclaim_stuck_outbox returns to 'pending'.
      Step 2 (no tx): call `publish` for each leased row (the external side effect).
      Step 3 (own tx): mark the row 'sent' and COMMIT. A crash between Step 2 and Step 3
        leaves the row 'leased' -> reclaimed -> re-published: a harmless at-least-once
        duplicate (§5.3).

    Publish failures back off ('pending') or route to DLQ ('dead') once attempts are
    exhausted; a BackpressureError is durable waiting ('pending', short delay, NO attempt
    bump, NO DLQ)."""
    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "UPDATE outbox SET status='leased', lease_owner=%s, "
                "lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE id IN (SELECT id FROM outbox WHERE status='pending' AND next_attempt_at <= now() "
                "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s) RETURNING *",
                (owner, lease_seconds, batch),
            )
            leased = cur.fetchall()

    sent = 0
    for row in leased:
        msg = OutboxMessage(
            message_id=row["message_id"],
            partition_key=row["partition_key"],
            topic=row["topic"],
            payload=row["payload"],
            caused_by_event=row["caused_by_event"],
        )
        try:
            publish(conn, msg)
        except BackpressureError as bp:
            # Durable waiting (§5.2): downstream is saturated. Return the row to 'pending'
            # with a delay WITHOUT bumping attempts or DLQ'ing — it is not a failure.
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE outbox SET status='pending', last_error=%s, lease_owner=NULL, "
                        "lease_expires_at=NULL, next_attempt_at = now() + make_interval(secs => %s) "
                        "WHERE id=%s",
                        (str(bp), lease_seconds, row["id"]),
                    )
            continue
        except Exception as exc:  # noqa: BLE001 — failure classification is intentional
            attempts = row["attempts"] + 1
            with conn.transaction():
                with conn.cursor() as cur:
                    if attempts >= row["max_attempts"]:
                        cur.execute(
                            "UPDATE outbox SET status='dead', attempts=%s, last_error=%s, "
                            "lease_owner=NULL, lease_expires_at=NULL WHERE id=%s",
                            (attempts, str(exc), row["id"]),
                        )
                    else:
                        delay = compute_backoff(attempts, jitter=0.0)
                        cur.execute(
                            "UPDATE outbox SET status='pending', attempts=%s, last_error=%s, "
                            "lease_owner=NULL, lease_expires_at=NULL, "
                            "next_attempt_at = now() + make_interval(secs => %s) WHERE id=%s",
                            (attempts, str(exc), delay, row["id"]),
                        )
            continue
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE outbox SET status='sent', sent_at=now() WHERE id=%s", (row["id"],)
                )
        sent += 1
    return sent


def reclaim_stuck_outbox(conn: psycopg.Connection) -> int:
    """Return expired-lease rows to 'pending' (§5.2 stuck detection / §5.7 recovery)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE outbox SET status='pending', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='leased' AND lease_expires_at < now()"
        )
        return cur.rowcount


def outbox_pending_depth(conn: psycopg.Connection) -> int:
    """Backlog (pending+leased) — a backpressure signal for the relay (§5.2)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM outbox WHERE status IN ('pending', 'leased')")
        return int(cur.fetchone()[0])


def make_queue_publisher(
    route: Mapping[str, str],
    *,
    max_partition_depth: int | None = None,
) -> Callable[[psycopg.Connection, OutboxMessage], None]:
    """Build a `publish` that turns a routed outbox topic into a worker-queue row. When
    `max_partition_depth` is set, it is admission control (§5.2 backpressure): if the target
    partition already holds that many `ready`+`leased` queue items, it raises BackpressureError
    so the relay leaves the outbox row durably `pending` (durable waiting) until the worker
    queue drains — bounding per-partition backlog without dropping or failing work."""

    def publish(conn: psycopg.Connection, msg: OutboxMessage) -> None:
        handler = route.get(msg.topic)
        if handler is None:
            return  # topic has no internal step handler; nothing to enqueue
        if max_partition_depth is not None and (
            queue_depth(conn, partition_key=msg.partition_key) >= max_partition_depth
        ):
            raise BackpressureError(
                f"partition {msg.partition_key!r} at capacity ({max_partition_depth})"
            )
        enqueue(
            conn,
            message_id=msg.message_id,
            partition_key=msg.partition_key,
            handler=handler,
            payload=msg.payload,
        )

    return publish
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_outbox.py -q`. Expected: 11 passed (4 from Task 4 + 7 here).

5. **Commit:**

```
git add src/sp0/runtime/outbox.py tests/sp0/runtime/test_outbox.py
git commit -m "SP-0 Phase 04: leased relay (publish-then-sent, DLQ, reclaim, backpressure)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6 — Worker queue: SKIP-LOCKED claim, one-in-flight-per-partition, complete/fail/reclaim

**Files:**
- Create: `src/sp0/runtime/queue.py`
- Test: `tests/sp0/runtime/test_queue.py`

**Interfaces:**
- Consumes: `compute_backoff` (Task 2); `queue` (Task 1).
- Produces:
  - `QueueClaim` — `@dataclass(frozen=True, slots=True)` with `id: int`, `message_id: str`, `partition_key: str`, `handler: str`, `payload: Mapping[str, Any]`, `attempts: int`, `max_attempts: int`.
  - `enqueue(conn, *, message_id, partition_key, handler, payload, available_at=None, priority=100) -> int` — idempotent on `message_id`; returns row id.
  - `claim_one(conn, *, owner: str, lease_seconds: int = 30) -> QueueClaim | None` — the contract's `FOR UPDATE SKIP LOCKED` claim, excluding partitions with an in-flight lease; atomically leases + bumps `attempts`. Runs the claim in a SAVEPOINT: on the concurrent-claimer race where two workers slip past the partition-exclusion subquery before either commits, the loser's `queue_one_inflight_per_partition` `UniqueViolation` is caught and `claim_one` returns `None` (treated as "nothing claimed"), never aborting the caller's outer transaction.
  - `complete(conn, queue_id: int) -> None` — `status='done'`.
  - `fail_retryable(conn, queue_id: int, *, error: str) -> None` — back to `ready` with backoff `available_at`; DLQ (`dead`) once `attempts >= max_attempts` (§5.6).
  - `fail_permanent(conn, queue_id: int, *, error: str) -> None` — `status='dead'` (deterministic failure skips delivery retry, §5.6).
  - `reclaim_stuck_queue(conn) -> int` — expired leases back to `ready` (§5.7 crash recovery).
  - `queue_depth(conn, *, partition_key: str | None = None) -> int` — count of `ready`+`leased` rows (optionally for a single partition); the depth signal the relay's admission control consults for §5.2 backpressure.
  - `BackpressureError(RuntimeError)` — raised by admission control (e.g. the queue publisher in Task 5) when a partition is at capacity; the relay treats it as durable waiting (leave the outbox row `pending`, no attempt bump, no DLQ), not a failure.

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_queue.py`:

```python
from __future__ import annotations

from sp0.runtime.queue import (
    claim_one,
    complete,
    enqueue,
    fail_permanent,
    fail_retryable,
    queue_depth,
    reclaim_stuck_queue,
)


def test_enqueue_idempotent_on_message_id(db) -> None:
    a = enqueue(db, message_id="e1", partition_key="run:r1", handler="h", payload={})
    b = enqueue(db, message_id="e1", partition_key="run:r1", handler="h", payload={})
    assert a == b
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id = 'e1'")
        assert cur.fetchone()[0] == 1


def test_claim_leases_ready_row_and_bumps_attempts(db) -> None:
    enqueue(db, message_id="c1", partition_key="run:r1", handler="h", payload={"k": 1})
    claim = claim_one(db, owner="w1")
    assert claim is not None
    assert claim.message_id == "c1"
    assert claim.handler == "h"
    assert claim.payload == {"k": 1}
    assert claim.attempts == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE message_id = 'c1'")
        status, owner = cur.fetchone()
    assert status == "leased"
    assert owner == "w1"


def test_claim_skips_partition_with_inflight_lease(db) -> None:
    # partition run:r1 already has an in-flight lease; only run:r2 is claimable
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('busy', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'w0', now() + interval '1 minute')"
        )
    enqueue(db, message_id="ready_same", partition_key="run:r1", handler="h", payload={})
    enqueue(db, message_id="ready_other", partition_key="run:r2", handler="h", payload={})
    claim = claim_one(db, owner="w1")
    assert claim is not None
    assert claim.message_id == "ready_other"  # run:r1 is blocked by the in-flight lease


def test_claim_returns_none_when_empty(db) -> None:
    assert claim_one(db, owner="w1") is None


def test_complete_sets_done(db) -> None:
    qid = enqueue(db, message_id="d1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    complete(db, qid)
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE id = %s", (qid,))
        status, owner = cur.fetchone()
    assert status == "done"
    assert owner is None


def test_fail_retryable_reschedules(db) -> None:
    qid = enqueue(db, message_id="r1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    fail_retryable(db, qid, error="boom")
    with db.cursor() as cur:
        cur.execute("SELECT status, last_error FROM queue WHERE id = %s", (qid,))
        status, err = cur.fetchone()
    assert status == "ready"
    assert err == "boom"


def test_fail_retryable_dlqs_at_max_attempts(db) -> None:
    qid = enqueue(db, message_id="r2", partition_key="run:r1", handler="h", payload={})
    with db.cursor() as cur:
        cur.execute("UPDATE queue SET attempts = max_attempts WHERE id = %s", (qid,))
    fail_retryable(db, qid, error="exhausted")
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE id = %s", (qid,))
        assert cur.fetchone()[0] == "dead"


def test_fail_permanent_dlqs(db) -> None:
    qid = enqueue(db, message_id="p1", partition_key="run:r1", handler="h", payload={})
    claim_one(db, owner="w1")
    fail_permanent(db, qid, error="deterministic")
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE id = %s", (qid,))
        assert cur.fetchone()[0] == "dead"


def test_reclaim_stuck_queue(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('stuck', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'dead-w', now() - interval '1 minute')"
        )
    assert reclaim_stuck_queue(db) == 1
    with db.cursor() as cur:
        cur.execute("SELECT status, lease_owner FROM queue WHERE message_id = 'stuck'")
        status, owner = cur.fetchone()
    assert status == "ready"
    assert owner is None


def test_queue_depth_counts_ready_and_leased(db) -> None:
    enqueue(db, message_id="qd1", partition_key="run:r1", handler="h", payload={})
    enqueue(db, message_id="qd2", partition_key="run:r2", handler="h", payload={})
    claim_one(db, owner="w1")  # leases one row; leased rows still count toward depth
    assert queue_depth(db) == 2
    assert queue_depth(db, partition_key="run:r1") == 1
    assert queue_depth(db, partition_key="run:nope") == 0
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_queue.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.queue'`.

3. **Write minimal implementation** — `src/sp0/runtime/queue.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from sp0.runtime.backoff import compute_backoff


class BackpressureError(RuntimeError):
    """Admission control signal (§5.2): a partition is at capacity. Raised by the queue
    publisher; the relay treats it as durable waiting (leave the outbox row pending, no attempt
    bump, no DLQ), never as a delivery failure."""


@dataclass(frozen=True, slots=True)
class QueueClaim:
    """A leased worker-queue item (§5.2)."""
    id: int
    message_id: str
    partition_key: str
    handler: str
    payload: Mapping[str, Any]
    attempts: int
    max_attempts: int


def enqueue(
    conn: psycopg.Connection,
    *,
    message_id: str,
    partition_key: str,
    handler: str,
    payload: Mapping[str, Any],
    available_at: Optional[datetime] = None,
    priority: int = 100,
) -> int:
    """Insert a 'ready' work item; idempotent on message_id. Returns the row id."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, available_at, priority) "
            "VALUES (%s, %s, %s, %s, COALESCE(%s, now()), %s) "
            "ON CONFLICT (message_id) DO NOTHING RETURNING id",
            (message_id, partition_key, handler, Json(payload), available_at, priority),
        )
        row = cur.fetchone()
        if row is not None:
            return int(row[0])
        cur.execute("SELECT id FROM queue WHERE message_id = %s", (message_id,))
        return int(cur.fetchone()[0])


def claim_one(
    conn: psycopg.Connection, *, owner: str, lease_seconds: int = 30
) -> Optional[QueueClaim]:
    """Claim one ready item via FOR UPDATE SKIP LOCKED, excluding partitions that already
    have an in-flight lease (per-aggregate serialization, §5.2). Bumps attempts atomically.

    Concurrent-claimer race: two workers can both pass the partition-exclusion subquery before
    either commits its lease; `queue_one_inflight_per_partition` then rejects the loser with a
    UniqueViolation. We run the claim in a SAVEPOINT and translate that violation into
    "nothing claimed" (return None) so it never aborts the caller's outer transaction."""
    row = None
    try:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "WITH c AS ("
                    "  SELECT id FROM queue"
                    "   WHERE status='ready' AND available_at <= now()"
                    "     AND partition_key NOT IN (SELECT partition_key FROM queue WHERE status='leased')"
                    "   ORDER BY priority, available_at, id"
                    "   FOR UPDATE SKIP LOCKED LIMIT 1"
                    ") "
                    "UPDATE queue q SET status='leased', lease_owner=%s, "
                    "  lease_expires_at = now() + make_interval(secs => %s), attempts = q.attempts + 1 "
                    "FROM c WHERE q.id = c.id RETURNING q.*",
                    (owner, lease_seconds),
                )
                row = cur.fetchone()
    except psycopg.errors.UniqueViolation:
        return None  # lost the per-partition in-flight race; nothing claimed this round
    if row is None:
        return None
    return QueueClaim(
        id=row["id"],
        message_id=row["message_id"],
        partition_key=row["partition_key"],
        handler=row["handler"],
        payload=row["payload"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
    )


def complete(conn: psycopg.Connection, queue_id: int) -> None:
    """Mark a claimed item done and release its lease."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='done', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE id=%s",
            (queue_id,),
        )


def fail_retryable(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Transient failure: reschedule with backoff, or DLQ once attempts hit the budget (§5.6)."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT attempts, max_attempts FROM queue WHERE id=%s", (queue_id,))
        row = cur.fetchone()
        if row["attempts"] >= row["max_attempts"]:
            cur.execute(
                "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL WHERE id=%s",
                (error, queue_id),
            )
        else:
            delay = compute_backoff(row["attempts"], jitter=0.0)
            cur.execute(
                "UPDATE queue SET status='ready', last_error=%s, lease_owner=NULL, "
                "lease_expires_at=NULL, available_at = now() + make_interval(secs => %s) "
                "WHERE id=%s",
                (error, delay, queue_id),
            )


def fail_permanent(conn: psycopg.Connection, queue_id: int, *, error: str) -> None:
    """Deterministic failure: skip delivery retry, route to DLQ (§5.6)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='dead', last_error=%s, lease_owner=NULL, "
            "lease_expires_at=NULL WHERE id=%s",
            (error, queue_id),
        )


def reclaim_stuck_queue(conn: psycopg.Connection) -> int:
    """Return expired-lease items to 'ready' so a crashed worker's items resume (§5.7)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE queue SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='leased' AND lease_expires_at < now()"
        )
        return cur.rowcount


def queue_depth(
    conn: psycopg.Connection, *, partition_key: Optional[str] = None
) -> int:
    """In-flight backlog (ready+leased), globally or for one partition. The relay's admission
    control consults this for §5.2 backpressure (bound per-partition work-in-progress)."""
    with conn.cursor() as cur:
        if partition_key is None:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased')"
            )
        else:
            cur.execute(
                "SELECT count(*) FROM queue WHERE status IN ('ready', 'leased') "
                "AND partition_key = %s",
                (partition_key,),
            )
        return int(cur.fetchone()[0])
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_queue.py -q`. Expected: 10 passed (9 enqueue/claim/complete/fail/reclaim cases + `test_queue_depth_counts_ready_and_leased`).

5. **Commit:**

```
git add src/sp0/runtime/queue.py tests/sp0/runtime/test_queue.py
git commit -m "SP-0 Phase 04: worker queue (SKIP-LOCKED claim, per-partition serialization, retry/DLQ/reclaim)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7 — Atomic step boundary: `commit_step` (events + document + ledger + outbox)

**Files:**
- Create: `src/sp0/runtime/step.py`
- Test: `tests/sp0/runtime/test_step.py`

**Interfaces:**
- Consumes: `append_event`, `ConcurrencyError` (Phase 01); `documents` table (Phase 02 — write-once trigger + DAG/`derived_from` validation enforced there); `HandlerContext`, `HandlerResult`, `NewDocument`, `EventEnvelope` (contract); `record_processed` (Task 3); `outbox_messages_for_events`, `insert_outbox_message` (Task 4); `global_seq_seq` (Phase 01).
- Produces:
  - `StepCommit` — `@dataclass(frozen=True, slots=True)` with `appended_event_ids: tuple[str, ...]`, `document_id: str | None`, `outbox_message_ids: tuple[str, ...]`, `processed_seq: int`.
  - `commit_step(conn, ctx: HandlerContext, result: HandlerResult, *, message_id: str, expected_version: int, table_version: int) -> StepCommit` — the §5.1 atomic boundary **inside the caller's open tx**: chained-OCC appends of `result.new_events`, optional `result.document` insert, one outbox row per appended event, and the processed-message ledger row. **Raises `RuntimeError` if `result.timers` or `result.external_commands` is non-empty** (Phase 05 extends this; nothing is silently dropped). Propagates `ConcurrencyError` so the caller's savepoint rolls back the whole step.
  - `gen_id(prefix: str) -> str` — prefixed unique id (`doc_…`) used for minted document ids.

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_step.py`:

```python
from __future__ import annotations

import psycopg
import pytest

from sp0.contracts import (
    ConcurrencyError,
    Disposition,
    HandlerContext,
    HandlerResult,
    NewDocument,
    NewEvent,
    NewTimer,
)
from datetime import datetime, timezone
from sp0.runtime.step import commit_step


def _next_event(ctx, actor, prov, *, type="STEP_DONE", payload=None) -> NewEvent:
    return NewEvent(
        aggregate="run",
        aggregate_id=ctx.run_id,
        run_id=ctx.run_id,
        type=type,
        schema_version=1,
        payload=payload or {},
        actor=actor,
        provenance=prov,
    )


def _ctx(db, trigger) -> HandlerContext:
    return HandlerContext(
        run_id=trigger.run_id, triggering_event=trigger, documents={}, conn=db
    )


def test_commit_step_appends_event_outbox_and_ledger(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s1", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert len(sc.appended_event_ids) == 1
    assert sc.document_id is None
    # one outbox row per appended event
    with db.cursor() as cur:
        cur.execute("SELECT topic FROM outbox WHERE message_id = %s", (sc.appended_event_ids[0],))
        assert cur.fetchone()[0] == "STEP_DONE"
        cur.execute("SELECT processed_seq FROM processed_messages WHERE message_id = %s",
                    (trigger.event_id,))
        assert cur.fetchone()[0] == sc.processed_seq


def test_commit_step_inserts_document(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s2", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    doc = NewDocument(
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:abc",
        body_classification="governance-retained",
        provenance=prov,
    )
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        document=doc,
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert sc.document_id is not None
    with db.cursor() as cur:
        cur.execute("SELECT stage, run_id, branch_role FROM documents WHERE doc_id = %s",
                    (sc.document_id,))
        stage, run_id, role = cur.fetchone()
    assert (stage, run_id, role) == ("CANDIDATE_SQL", "run_s2", "candidate")


def test_commit_step_chains_occ_for_multiple_events(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s3", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(
            _next_event(ctx, actor, prov, type="STEP_DONE"),
            _next_event(ctx, actor, prov, type="STEP_NEXT"),
        ),
    )
    sc = commit_step(
        db, ctx, result,
        message_id=trigger.event_id,
        expected_version=trigger.stream_version,
        table_version=trigger.table_version,
    )
    assert len(sc.appended_event_ids) == 2
    with db.cursor() as cur:
        cur.execute(
            "SELECT stream_version FROM events WHERE run_id='run_s3' ORDER BY stream_version"
        )
        versions = [r[0] for r in cur.fetchall()]
    assert versions == [1, 2, 3]  # trigger=1, then 2, 3


def test_commit_step_raises_on_timers(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s4", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        timers=(NewTimer(kind="sla", fire_at=datetime.now(timezone.utc), idempotency_key="t1"),),
    )
    with pytest.raises(RuntimeError):
        commit_step(
            db, ctx, result,
            message_id=trigger.event_id,
            expected_version=trigger.stream_version,
            table_version=trigger.table_version,
        )


def test_commit_step_stale_expected_version_raises_concurrency(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_s5", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    result = HandlerResult(
        disposition=Disposition.OK, new_events=(_next_event(ctx, actor, prov),)
    )
    # expected_version 0 is stale (stream is already at version 1)
    with pytest.raises(ConcurrencyError):
        commit_step(
            db, ctx, result,
            message_id=trigger.event_id,
            expected_version=0,
            table_version=trigger.table_version,
        )


def test_commit_step_rolls_back_all_writes_when_document_insert_fails(
    db, seed_run_event, actor, prov
) -> None:
    """Atomicity / no-orphan invariant (§5.1): if a participant fails AFTER an event was
    appended, the WHOLE step rolls back. The event appends, then the document INSERT violates
    documents_reject_reason_present (branch_role='rejected' with no reject_reason); the
    per-step savepoint (as `process_one` uses it) must leave NO event, outbox row, or ledger
    row behind."""
    trigger = seed_run_event("run_atomic", type="STEP_TRIGGER")
    ctx = _ctx(db, trigger)
    bad_doc = NewDocument(
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="rejected",          # CHECK requires a reject_reason
        content_hash="sha256:abc",
        body_classification="governance-retained",
        provenance=prov,
        reject_reason=None,              # -> documents_reject_reason_present violation
    )
    result = HandlerResult(
        disposition=Disposition.OK,
        new_events=(_next_event(ctx, actor, prov),),
        document=bad_doc,
    )
    with pytest.raises(psycopg.errors.CheckViolation):
        with db.transaction():           # mirrors process_one's per-step savepoint
            commit_step(
                db, ctx, result,
                message_id=trigger.event_id,
                expected_version=trigger.stream_version,
                table_version=trigger.table_version,
            )
    with db.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id='run_atomic' AND type='STEP_DONE'"
        )
        assert cur.fetchone()[0] == 0    # the appended event was rolled back
        cur.execute("SELECT count(*) FROM outbox WHERE partition_key='run:run_atomic'")
        assert cur.fetchone()[0] == 0    # no orphan outbox row
        cur.execute(
            "SELECT count(*) FROM processed_messages WHERE message_id=%s", (trigger.event_id,)
        )
        assert cur.fetchone()[0] == 0    # no ledger row
```

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_step.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.step'`.

3. **Write minimal implementation** — `src/sp0/runtime/step.py`:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass
from uuid import uuid4

import psycopg
from psycopg.types.json import Json

from sp0.contracts import HandlerContext, HandlerResult, NewDocument
from sp0.event_store import append_event
from sp0.runtime.ledger import record_processed
from sp0.runtime.outbox import insert_outbox_message, outbox_messages_for_events


@dataclass(frozen=True, slots=True)
class StepCommit:
    """Outcome of one atomic step (§5.1)."""
    appended_event_ids: tuple[str, ...]
    document_id: str | None
    outbox_message_ids: tuple[str, ...]
    processed_seq: int


def gen_id(prefix: str) -> str:
    """Prefixed unique id (ULID-style slot; uuid4 hex is a fine stand-in)."""
    return f"{prefix}_{uuid4().hex}"


def _jsonb(envelope) -> Json:
    return Json(asdict(envelope))


def _insert_document(conn: psycopg.Connection, ctx: HandlerContext, doc: NewDocument) -> str:
    doc_id = gen_id("doc")
    te = ctx.triggering_event
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO documents "
            "(doc_id, request_id, feature_id, run_id, stage, schema_version, branch_role, "
            " derived_from, supersedes, body_ref, content_hash, body_classification, "
            " actor, provenance, reject_reason) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                doc_id, te.request_id, te.feature_id, ctx.run_id, doc.stage, doc.schema_version,
                doc.branch_role, list(doc.derived_from), list(doc.supersedes), doc.body_ref,
                doc.content_hash, doc.body_classification, _jsonb(te.actor),
                _jsonb(doc.provenance), doc.reject_reason,
            ),
        )
    return doc_id


def commit_step(
    conn: psycopg.Connection,
    ctx: HandlerContext,
    result: HandlerResult,
    *,
    message_id: str,
    expected_version: int,
    table_version: int,
) -> StepCommit:
    """The §5.1 atomic boundary, inside the caller's open tx: append events (chained OCC),
    insert one frozen document, write one outbox row per event, record the ledger row."""
    if result.timers or result.external_commands:
        raise RuntimeError(
            "commit_step: timers/external_commands persistence is added by Phase 05 "
            "(§5.4/§5.5); not supported in Phase 04"
        )

    te = ctx.triggering_event
    version = expected_version
    appended = []
    for new_event in result.new_events:
        env = append_event(
            conn, new_event, expected_version=version, table_version=table_version
        )
        appended.append(env)
        version = env.stream_version

    document_id = (
        _insert_document(conn, ctx, result.document)
        if result.document is not None
        else None
    )

    outbox_ids: list[str] = []
    for msg in outbox_messages_for_events(appended):
        insert_outbox_message(conn, msg)
        outbox_ids.append(msg.message_id)

    if appended:
        processed_seq = max(env.global_seq for env in appended)
        result_event_id: str | None = appended[-1].event_id
    else:
        with conn.cursor() as cur:
            cur.execute("SELECT last_value FROM global_seq_seq")
            processed_seq = int(cur.fetchone()[0])
        result_event_id = None

    record_processed(
        conn,
        message_id=message_id,
        aggregate=te.aggregate,
        aggregate_id=te.aggregate_id,
        result_event_id=result_event_id,
        processed_seq=processed_seq,
    )

    return StepCommit(
        appended_event_ids=tuple(env.event_id for env in appended),
        document_id=document_id,
        outbox_message_ids=tuple(outbox_ids),
        processed_seq=processed_seq,
    )
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_step.py -q`. Expected: 6 passed.

5. **Commit:**

```
git add src/sp0/runtime/step.py tests/sp0/runtime/test_step.py
git commit -m "SP-0 Phase 04: atomic step boundary commit_step (events+doc+outbox+ledger, chained OCC)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 8 — Handler registry + idempotent dispatcher + crash recovery

**Files:**
- Create: `src/sp0/runtime/handlers.py`
- Create: `src/sp0/runtime/dispatch.py`
- Test: `tests/sp0/runtime/test_dispatch.py`

**Interfaces:**
- Consumes: `Handler`, `HandlerResult`, `HandlerContext`, `Disposition`, `ConcurrencyError` (contract); `load_stream` (Phase 01); `claim_one`, `complete`, `fail_retryable`, `fail_permanent`, `reclaim_stuck_queue`, `QueueClaim` (Task 6); `reclaim_stuck_outbox` (Task 5); `is_processed` (Task 3); `commit_step` (Task 7).
- Produces:
  - `HandlerRegistry` — `register(handler: Handler) -> None` (re-registration of a name is a `ValueError`, mirroring §10 versioned registration), `get(name: str) -> Handler`.
  - `HandlerTimeout(Exception)` — raised when a handler exceeds `handler.timeout_seconds`.
  - `ProcessOutcome` — `@dataclass(frozen=True, slots=True)` with `status: str` (`"idle"|"ok"|"duplicate"|"retryable"|"permanent"`), `message_id: str | None`, `queue_id: int | None`.
  - `process_one(conn, registry: HandlerRegistry, *, owner: str, document_loader: Callable[[psycopg.Connection, str], Mapping[str, NewDocument]] = ...) -> ProcessOutcome` — claim → ledger-skip duplicates → build run `HandlerContext` → run handler under its timeout → dispatch on `HandlerResult.disposition` (OK ⇒ `commit_step` in a savepoint + `complete`; OCC ⇒ retry; RETRYABLE ⇒ `fail_retryable`; PERMANENT ⇒ `fail_permanent`).
  - `recover_stuck(conn) -> tuple[int, int]` — `(reclaim_stuck_queue, reclaim_stuck_outbox)` (§5.7 automatic crash recovery).

### TDD steps

1. **Write the failing test** — `tests/sp0/runtime/test_dispatch.py`:

```python
from __future__ import annotations

import time

from sp0.contracts import Disposition, HandlerContext, HandlerResult, NewEvent
from sp0.runtime.dispatch import (
    HandlerRegistry,
    ProcessOutcome,
    process_one,
    recover_stuck,
)
from sp0.runtime.outbox import (
    insert_outbox_message,
    make_queue_publisher,
    outbox_messages_for_events,
    relay_publish_batch,
)
from sp0.runtime.queue import enqueue


class _Handler:
    """A run-scoped step handler emitting one STEP_DONE event."""

    name = "advance"
    version = 1
    timeout_seconds = 5.0

    def __init__(self, actor, prov, disposition=Disposition.OK, error=None):
        self._actor, self._prov = actor, prov
        self._disposition, self._error = disposition, error

    def handle(self, ctx: HandlerContext) -> HandlerResult:
        ev = NewEvent(
            aggregate="run", aggregate_id=ctx.run_id, run_id=ctx.run_id,
            type="STEP_DONE", schema_version=1, payload={}, actor=self._actor,
            provenance=self._prov,
        )
        return HandlerResult(
            disposition=self._disposition,
            new_events=(ev,) if self._disposition == Disposition.OK else (),
            error=self._error,
        )


class _SlowHandler(_Handler):
    name = "slow"
    timeout_seconds = 0.05

    def handle(self, ctx):
        time.sleep(0.3)
        return super().handle(ctx)


def _pipe_trigger_to_queue(db, trigger) -> None:
    """Mirror the real path: derive outbox row from the trigger, relay -> queue."""
    for msg in outbox_messages_for_events([trigger]):
        insert_outbox_message(db, msg)
    relay_publish_batch(db, make_queue_publisher({"STEP_TRIGGER": "advance"}), owner="relay1")


def test_end_to_end_claim_handle_commit(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_e2e", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "ok"
    with db.cursor() as cur:
        cur.execute("SELECT type FROM events WHERE run_id='run_e2e' ORDER BY stream_version")
        assert [r[0] for r in cur.fetchall()] == ["STEP_TRIGGER", "STEP_DONE"]
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "done"


def test_idle_when_queue_empty(db) -> None:
    reg = HandlerRegistry()
    assert process_one(db, reg, owner="w1").status == "idle"


def test_duplicate_message_is_skipped(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_dup", type="STEP_TRIGGER")
    # mark already-processed so the dispatcher must no-op
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO processed_messages (message_id, aggregate, aggregate_id, processed_seq) "
            "VALUES (%s, 'run', 'run_dup', 1)",
            (trigger.event_id,),
        )
    enqueue(db, message_id=trigger.event_id, partition_key="run:run_dup",
            handler="advance", payload={"event_id": trigger.event_id, "run_id": "run_dup"})
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "duplicate"
    with db.cursor() as cur:
        cur.execute("SELECT count(*) FROM events WHERE run_id='run_dup' AND type='STEP_DONE'")
        assert cur.fetchone()[0] == 0  # no second effect


def test_retryable_reschedules(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_retry", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov, disposition=Disposition.RETRYABLE, error="transient"))
    assert process_one(db, reg, owner="w1").status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"


def test_permanent_dlqs(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_perm", type="STEP_TRIGGER")
    _pipe_trigger_to_queue(db, trigger)
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov, disposition=Disposition.PERMANENT, error="bad input"))
    assert process_one(db, reg, owner="w1").status == "permanent"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "dead"


def test_timeout_is_retryable(db, seed_run_event, actor, prov) -> None:
    trigger = seed_run_event("run_to", type="STEP_TRIGGER")
    for msg in outbox_messages_for_events([trigger]):
        insert_outbox_message(db, msg)
    relay_publish_batch(db, make_queue_publisher({"STEP_TRIGGER": "slow"}), owner="relay1")
    reg = HandlerRegistry()
    reg.register(_SlowHandler(actor, prov))
    assert process_one(db, reg, owner="w1").status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"


def test_register_rejects_duplicate_name(actor, prov) -> None:
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    import pytest

    with pytest.raises(ValueError):
        reg.register(_Handler(actor, prov))


def test_recover_stuck_reclaims_queue_and_outbox(db) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('q_stuck', 'run:r1', 'h', '{}'::jsonb, "
            "'leased', 'dead', now() - interval '1 minute')"
        )
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, status, "
            "lease_owner, lease_expires_at) VALUES ('o_stuck', 'run:r1', 'T', '{}'::jsonb, "
            "'leased', 'dead', now() - interval '1 minute')"
        )
    assert recover_stuck(db) == (1, 1)


def test_occ_conflict_reschedules_without_partial_writes(db, seed_run_event, actor, prov) -> None:
    """A REAL OCC conflict (the run stream advanced after the step was triggered) must roll the
    step back inside its savepoint — no STEP_DONE event, no outbox row, no ledger row — and
    reschedule the message (status='ready'). This exercises process_one's
    `except ConcurrencyError ⇒ fail_retryable` branch and verifies the no-partial-writes
    invariant (§5.1)."""
    trigger = seed_run_event("run_occ", type="STEP_TRIGGER")  # stream_version 1
    _pipe_trigger_to_queue(db, trigger)
    # concurrently advance the run stream so the step's expected_version (1) is now stale
    seed_run_event("run_occ", type="STEP_NEXT", expected_version=1)  # stream_version 2
    reg = HandlerRegistry()
    reg.register(_Handler(actor, prov))
    outcome = process_one(db, reg, owner="w1")
    assert outcome.status == "retryable"
    with db.cursor() as cur:
        cur.execute("SELECT status FROM queue WHERE message_id=%s", (trigger.event_id,))
        assert cur.fetchone()[0] == "ready"  # rescheduled, not lost
        cur.execute(
            "SELECT count(*) FROM events WHERE run_id='run_occ' AND type='STEP_DONE'"
        )
        assert cur.fetchone()[0] == 0  # no partial event from the rolled-back step
        cur.execute(
            "SELECT count(*) FROM processed_messages WHERE message_id=%s", (trigger.event_id,)
        )
        assert cur.fetchone()[0] == 0  # no ledger row
```

> **Note (handler connection in tests):** `process_one`'s default `handler_conn_factory` opens a dedicated connection via `psycopg.connect(conn.info.dsn)`. The test handlers do no I/O on `ctx.conn`, so if the Phase-01 `db` fixture's DSN is not directly re-connectable in your environment, pass `handler_conn_factory=lambda c: c` to `process_one` in these tests — it stays safe here precisely because the test handlers never touch `ctx.conn`. Production keeps the isolating default.

2. **Run it, expect FAIL** — `python -m pytest tests/sp0/runtime/test_dispatch.py -q`. Expected: `ModuleNotFoundError: No module named 'sp0.runtime.dispatch'`.

3. **Write minimal implementation** — first `src/sp0/runtime/handlers.py`:

```python
from __future__ import annotations

from sp0.contracts import Handler


class HandlerRegistry:
    """Name -> step Handler. Re-registering a name is a load-time error (§10)."""

    def __init__(self) -> None:
        self._by_name: dict[str, Handler] = {}

    def register(self, handler: Handler) -> None:
        name = handler.name
        if name in self._by_name:
            raise ValueError(f"handler {name!r} already registered")
        self._by_name[name] = handler

    def get(self, name: str) -> Handler:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(f"no handler registered: {name!r}") from None
```

Then `src/sp0/runtime/dispatch.py`:

```python
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Mapping

import psycopg

from sp0.contracts import (
    ConcurrencyError,
    Disposition,
    Handler,
    HandlerContext,
    HandlerResult,
    NewDocument,
)
from sp0.event_store import load_stream
from sp0.runtime.handlers import HandlerRegistry
from sp0.runtime.ledger import is_processed
from sp0.runtime.outbox import reclaim_stuck_outbox
from sp0.runtime.queue import (
    QueueClaim,
    claim_one,
    complete,
    fail_permanent,
    fail_retryable,
    reclaim_stuck_queue,
)
from sp0.runtime.step import commit_step


class HandlerTimeout(Exception):
    """Raised when a handler exceeds its per-invocation timeout (=> delivery retry, §5.6)."""


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: str  # "idle" | "ok" | "duplicate" | "retryable" | "permanent"
    message_id: str | None
    queue_id: int | None


def _default_document_loader(
    conn: psycopg.Connection, run_id: str
) -> Mapping[str, NewDocument]:
    return {}


def _open_handler_conn(conn: psycopg.Connection) -> psycopg.Connection:
    """A dedicated, autocommit connection handed to the handler as ctx.conn, ISOLATED from
    the dispatcher's transactional `conn`. Isolation is mandatory: CPython cannot kill a
    running thread, so a timed-out handler must never share the dispatcher's connection (the
    dispatcher keeps using it to reschedule/fail the message — concurrent use of one psycopg
    connection from two threads is unsafe). Override via
    process_one(..., handler_conn_factory=...) if the deployment DSN needs extra credentials."""
    handler_conn = psycopg.connect(conn.info.dsn)
    handler_conn.autocommit = True
    return handler_conn


def _run_with_timeout(handler: Handler, ctx: HandlerContext) -> HandlerResult:
    """Run handler.handle(ctx) under a hard wall-clock timeout WITHOUT blocking on a wedged
    handler. The handler runs on a daemon thread joined for handler.timeout_seconds; we never
    use a ThreadPoolExecutor (whose context-manager __exit__ runs shutdown(wait=True), which
    would BLOCK until the handler returns and defeat the timeout). On breach we raise
    HandlerTimeout and ABANDON the thread; because ctx.conn is a dedicated connection (see
    _open_handler_conn), the abandoned thread cannot corrupt the dispatcher's step transaction.
    A permanently wedged handler leaks its dedicated connection until the worker is restarted,
    and its message is redelivered (then DLQ'd after max_attempts) — the honest limit of
    cooperative timeouts in CPython (a thread cannot be force-killed)."""
    box: dict[str, object] = {}

    def _target() -> None:
        try:
            box["result"] = handler.handle(ctx)
        except BaseException as exc:  # noqa: BLE001 — re-raised on the dispatcher thread
            box["error"] = exc

    thread = threading.Thread(
        target=_target, name=f"handler:{handler.name}", daemon=True
    )
    thread.start()
    thread.join(timeout=handler.timeout_seconds)
    if thread.is_alive():
        raise HandlerTimeout(
            f"handler {handler.name!r} exceeded {handler.timeout_seconds}s"
        )
    if "error" in box:
        raise box["error"]  # type: ignore[misc]
    return box["result"]  # type: ignore[return-value]


def _build_context(
    conn: psycopg.Connection,
    claim: QueueClaim,
    document_loader: Callable[[psycopg.Connection, str], Mapping[str, NewDocument]],
    *,
    handler_conn: psycopg.Connection,
) -> HandlerContext:
    payload = claim.payload
    run_id = payload.get("run_id") or payload.get("aggregate_id")
    event_id = payload["event_id"]
    # The dispatcher's `conn` (not handler_conn) resolves the triggering event so it sees the
    # step's in-flight writes; the handler only ever gets the isolated handler_conn.
    stream = load_stream(conn, "run", run_id)
    triggering = next((e for e in stream if e.event_id == event_id), None)
    if triggering is None:
        raise KeyError(f"triggering event {event_id!r} not found in run {run_id!r}")
    return HandlerContext(
        run_id=run_id,
        triggering_event=triggering,
        documents=document_loader(conn, run_id),
        conn=handler_conn,  # dedicated, isolated from the dispatcher tx
    )


def process_one(
    conn: psycopg.Connection,
    registry: HandlerRegistry,
    *,
    owner: str,
    document_loader: Callable[
        [psycopg.Connection, str], Mapping[str, NewDocument]
    ] = _default_document_loader,
    handler_conn_factory: Callable[
        [psycopg.Connection], psycopg.Connection
    ] = _open_handler_conn,
) -> ProcessOutcome:
    """Claim one queue item and drive it forward idempotently (§5.3). Runs in one outer tx;
    the OK path commits the step inside a savepoint so a real OCC conflict rolls back ONLY the
    step writes (no partial events/docs/outbox/ledger) and reschedules the message.

    OCC basis: `expected_version` is the TRIGGERING event's stream_version (the version the
    step was scheduled against), NOT a freshly-read head — so a concurrent advance of the run
    stream after the step was triggered is correctly detected as a conflict (§5.1 OCC)."""
    with conn.transaction():
        claim = claim_one(conn, owner=owner)
        if claim is None:
            return ProcessOutcome(status="idle", message_id=None, queue_id=None)

        if is_processed(conn, claim.message_id):
            complete(conn, claim.id)
            return ProcessOutcome(
                status="duplicate", message_id=claim.message_id, queue_id=claim.id
            )

        handler = registry.get(claim.handler)
        handler_conn = handler_conn_factory(conn)
        timed_out = False
        try:
            ctx = _build_context(
                conn, claim, document_loader, handler_conn=handler_conn
            )

            try:
                result = _run_with_timeout(handler, ctx)
            except HandlerTimeout as exc:
                timed_out = True
                fail_retryable(conn, claim.id, error=str(exc))
                return ProcessOutcome(
                    status="retryable", message_id=claim.message_id, queue_id=claim.id
                )

            if result.disposition == Disposition.OK:
                try:
                    with conn.transaction():
                        commit_step(
                            conn, ctx, result,
                            message_id=claim.message_id,
                            expected_version=ctx.triggering_event.stream_version,
                            table_version=ctx.triggering_event.table_version,
                        )
                except ConcurrencyError as exc:
                    fail_retryable(conn, claim.id, error=f"OCC: {exc}")
                    return ProcessOutcome(
                        status="retryable", message_id=claim.message_id, queue_id=claim.id
                    )
                complete(conn, claim.id)
                return ProcessOutcome(
                    status="ok", message_id=claim.message_id, queue_id=claim.id
                )

            if result.disposition == Disposition.RETRYABLE:
                fail_retryable(conn, claim.id, error=result.error or "retryable")
                return ProcessOutcome(
                    status="retryable", message_id=claim.message_id, queue_id=claim.id
                )

            fail_permanent(conn, claim.id, error=result.error or "permanent")
            return ProcessOutcome(
                status="permanent", message_id=claim.message_id, queue_id=claim.id
            )
        finally:
            # Close the dedicated handler connection only if the handler actually returned. On
            # timeout the abandoned thread may still hold it, so we deliberately leak it rather
            # than close a connection another thread might be mid-query on.
            if not timed_out:
                handler_conn.close()


def recover_stuck(conn: psycopg.Connection) -> tuple[int, int]:
    """Reclaim expired queue + outbox leases after a crash (§5.7). Returns (queue, outbox)."""
    return (reclaim_stuck_queue(conn), reclaim_stuck_outbox(conn))
```

4. **Run tests, expect PASS** — `python -m pytest tests/sp0/runtime/test_dispatch.py -q`. Expected: 9 passed.

5. **Commit:**

```
git add src/sp0/runtime/handlers.py src/sp0/runtime/dispatch.py tests/sp0/runtime/test_dispatch.py
git commit -m "SP-0 Phase 04: handler registry + idempotent dispatcher + crash recovery

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Phase 04 §12 test-coverage checklist

The spec §12 cases this phase owns (mechanism-under-test) are realized by the tasks above; a reviewer gate should confirm each:

- **Idempotency — duplicate message/outbox-publish → one effect:** `test_dispatch.py::test_duplicate_message_is_skipped` (ledger skip, no second event); `test_outbox.py::test_insert_is_idempotent_on_message_id` + `test_queue.py::test_enqueue_idempotent_on_message_id` (`ON CONFLICT DO NOTHING`).
- **Atomicity — OCC/failure rolls back the whole step (no orphan events/docs/outbox/ledger):** `test_step.py::test_commit_step_stale_expected_version_raises_concurrency` (stale OCC ⇒ `ConcurrencyError`); `test_step.py::test_commit_step_rolls_back_all_writes_when_document_insert_fails` (a failure AFTER a successful append rolls back the event + outbox + ledger via the savepoint — the no-orphan invariant has a real failing→pass cycle); `test_dispatch.py::test_occ_conflict_reschedules_without_partial_writes` forces a REAL OCC conflict so `process_one`'s `except ConcurrencyError ⇒ fail_retryable` savepoint branch runs and leaves no partial writes.
- **Queue partitioning — feature-/request-level events get per-aggregate ordering:** `test_outbox.py::test_partition_key_per_aggregate` + `partition_key_for` covers `run:`/`feature:`/`request:`; `test_queue.py::test_claim_skips_partition_with_inflight_lease` enforces one in-flight per partition.
- **Retries — transient backoff vs permanent skip:** `test_dispatch.py::test_retryable_reschedules` / `::test_permanent_dlqs`; `test_queue.py::test_fail_retryable_reschedules` / `::test_fail_retryable_dlqs_at_max_attempts`; `test_backoff.py`.
- **Outbox relay — publish-then-mark-sent (own-tx three-step), DLQ, stuck detection, backpressure:** `test_outbox.py::test_relay_publishes_then_marks_sent` / `::test_relay_backoff_on_publish_failure` / `::test_relay_routes_to_dlq_at_max_attempts` / `::test_reclaim_stuck_outbox` / `::test_pending_depth_counts_pending_and_leased` (depth signal) / `::test_backpressure_holds_outbox_pending_without_failing` (admission control: a saturated partition leaves the outbox row durably `pending` — durable waiting — with no attempt bump or DLQ).
- **Crash/recovery (§5.7):** `test_dispatch.py::test_recover_stuck_reclaims_queue_and_outbox`; `test_queue.py::test_reclaim_stuck_queue`; `test_outbox.py::test_reclaim_stuck_outbox`.
- **Handler timeout ⇒ delivery retry (non-blocking, connection-isolated):** `test_dispatch.py::test_timeout_is_retryable`. The dispatcher runs each handler on a daemon thread joined with a wall-clock timeout (no blocking `ThreadPoolExecutor` shutdown that would wait for the wedged handler), and hands the handler a DEDICATED connection (`_open_handler_conn`), not the dispatcher's transactional one, so a timed-out, un-killable thread cannot concurrently touch the connection the dispatcher uses to reschedule the message.
- **Versioned handler registration (§10):** `test_dispatch.py::test_register_rejects_duplicate_name`.

**Deferred to Phase 05 (noted, not silently skipped):** durable-timer ladder + overdue fire on recovery + business calendar; external-command dispatcher + stale-result guard + result caching + honest exactly-once caveat; cost-budget breaker auto-park; blob-GC. `commit_step` raises on `timers`/`external_commands` until Phase 05 wires their persistence (`test_step.py::test_commit_step_raises_on_timers`).


