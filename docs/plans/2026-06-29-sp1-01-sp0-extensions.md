# SP-1 — Phase 1 — SP-0 extensions (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp1-00-overview.md](2026-06-29-sp1-00-overview.md) (authoritative).

---

This phase ships the **additive, backward-compatible SP-0 extensions** that unblock the rest of SP-1
(design §2.1–§2.3). All DDL changes to existing tables (`events`, `human_tasks`, `timers`) are **new
idempotent `.sql` files** under `src/featuregen/db/migrations/` using `ALTER TABLE … DROP/ADD
CONSTRAINT` and `ADD COLUMN IF NOT EXISTS` — editing a Stage-1 `CREATE TABLE IF NOT EXISTS` string
does **not** alter an existing table. The new files use the `008x_` prefix so they sort after the gate
(`0070`), security (`0071`), and runtime (`050x`) DDL they alter, and before privacy (`081x`). After
each `.sql` file is added it is auto-globbed by `featuregen.db.migrations._sql_file_migrations()` and
applied by the session test fixture's one-time `apply_migrations`.

> **Postgres constraint-name note (load-bearing):** an *unnamed inline* `CHECK` on a single column is
> auto-named `{table}_{column}_check` by PostgreSQL. So SP-0's inline checks are named
> `events_aggregate_check` (on `events.aggregate`), `human_tasks_gate_check` (on `human_tasks.gate`),
> and `timers_kind_check` (on `timers.kind`). The migrations below `DROP CONSTRAINT IF EXISTS` those
> exact names before re-adding the widened constraint under the **same** name (so re-runs are no-ops).

---

### Task 1.1: `events` — `overlay_fact` aggregate + `overlay_fact_id` column threaded through the append path

**Files:**
- Create: `src/featuregen/db/migrations/0080_overlay_events.sql`
- Modify: `src/featuregen/contracts/envelopes.py:34-54` (`EventEnvelope`), `:57-72` (`NewEvent`)
- Modify: `src/featuregen/events/store.py:17-28` (`_INSERT`), `:80-96` (params), `:117-135` (returned envelope)
- Modify: `src/featuregen/events/serde.py:79-98` (`row_to_event`)
- Modify: `src/featuregen/aggregates/_append.py:60-96` (`append` kwarg + `NewEvent` build)
- Modify: `src/featuregen/runtime/outbox.py:27-36` (`partition_key_for`)
- Test: `tests/featuregen/events/test_overlay_events.py`

**Interfaces:**
- Consumes: `append(conn, *, aggregate, aggregate_id, type, payload, actor, provenance=None, request_id=None, feature_id=None, run_id=None, caused_by=None, expected_version=None) -> EventEnvelope` (`featuregen.aggregates._append`); `event_registry().register_schema(type_name, schema_version, json_schema, owner, *, status="active")`; `build_service_identity(*, subject, role_claims, attestation, ...) -> IdentityEnvelope`; `partition_key_for(event: EventEnvelope) -> str` (`featuregen.runtime.outbox`); `apply_migrations(conn)`.
- Produces: `EventEnvelope.overlay_fact_id: str | None`, `NewEvent.overlay_fact_id: str | None`; `append(..., overlay_fact_id=None)` keyword; an `events` table that accepts `aggregate='overlay_fact'` with `aggregate_id == overlay_fact_id` and `request_id/feature_id/run_id IS NULL`; `partition_key_for` returns `f"overlay_fact:{fact_key}"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/events/test_overlay_events.py
from __future__ import annotations

import psycopg
import pytest

from featuregen.aggregates._append import append
from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import event_registry
from featuregen.identity.build import build_service_identity
from featuregen.runtime.outbox import partition_key_for

_FK = "a1b2c3d4e5f6"  # stand-in for a fact_key (sha256 hex) — Phase 1 store layer is key-agnostic


def _overlay_actor():
    return build_service_identity(
        subject="service:overlay-profiler",
        role_claims=["overlay"],
        attestation="signed-deploy-id:overlay@1.0.0",
    )


def _register(type_name="OVERLAY_FACT_TEST"):
    event_registry().register_schema(type_name, 1, {"type": "object"}, "overlay")


def test_overlay_fact_append_succeeds(conn):
    _register()
    env = append(
        conn,
        aggregate="overlay_fact",
        aggregate_id=_FK,
        overlay_fact_id=_FK,
        type="OVERLAY_FACT_TEST",
        payload={"k": "v"},
        actor=_overlay_actor(),
    )
    assert env.aggregate == "overlay_fact"
    assert env.overlay_fact_id == _FK
    assert env.aggregate_id == _FK
    assert env.stream_version == 1
    row = conn.execute(
        "SELECT aggregate, aggregate_id, overlay_fact_id, request_id, feature_id, run_id "
        "FROM events WHERE event_id=%s",
        (env.event_id,),
    ).fetchone()
    assert row == ("overlay_fact", _FK, _FK, None, None, None)


def test_overlay_fact_with_request_id_is_rejected_by_consistency_check(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "overlay_fact_id, request_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'overlay_fact',%s,1,%s,'req_x','OVERLAY_FACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad", _FK, _FK),
        )


def test_request_append_still_passes(conn):
    _register("REQ_TEST")
    env = append(
        conn,
        aggregate="request",
        aggregate_id="req_1",
        request_id="req_1",
        type="REQ_TEST",
        payload={},
        actor=_overlay_actor(),
    )
    assert env.aggregate == "request"
    assert env.request_id == "req_1"
    assert env.overlay_fact_id is None


def test_partition_key_for_overlay_fact(conn):
    _register()
    env = append(
        conn,
        aggregate="overlay_fact",
        aggregate_id=_FK,
        overlay_fact_id=_FK,
        type="OVERLAY_FACT_TEST",
        payload={},
        actor=_overlay_actor(),
    )
    assert partition_key_for(env) == f"overlay_fact:{_FK}"


def test_overlay_events_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    col = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='events' AND column_name='overlay_fact_id'"
    ).fetchone()
    assert col is not None
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='events_aggregate_id_consistent'"
    ).fetchone()
    assert chk is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/events/test_overlay_events.py -v`
Expected: FAIL — `append()` rejects the unexpected `overlay_fact_id` keyword (TypeError) / the `aggregate='overlay_fact'` CHECK violation / no `overlay_fact_id` column.

- [ ] **Step 3: Create the `0080_overlay_events.sql` migration**

```sql
-- src/featuregen/db/migrations/0080_overlay_events.sql
-- SP-1 Phase 1 (design §2.1): additive, backward-compatible extension of SP-0's `events`
-- table to host the `overlay_fact` aggregate. Idempotent: re-running is a clean no-op.

-- 1. Widen the aggregate CHECK to admit 'overlay_fact'. The original inline CHECK on the
--    column is auto-named events_aggregate_check.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_check;
ALTER TABLE events ADD CONSTRAINT events_aggregate_check
    CHECK (aggregate IN ('request','feature','run','overlay_fact'));

-- 2. Typed mirror column for overlay facts (aggregate_id == overlay_fact_id == fact_key).
ALTER TABLE events ADD COLUMN IF NOT EXISTS overlay_fact_id text;

-- 3. Recreate the id-consistency invariant with an explicit overlay branch: for overlay
--    facts the canonical key is overlay_fact_id and the run/feature/request mirrors are NULL.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_id_consistent;
ALTER TABLE events ADD CONSTRAINT events_aggregate_id_consistent CHECK (
    (aggregate = 'request' AND aggregate_id = request_id) OR
    (aggregate = 'feature' AND aggregate_id = feature_id) OR
    (aggregate = 'run'     AND aggregate_id = run_id)     OR
    (aggregate = 'overlay_fact' AND aggregate_id = overlay_fact_id
        AND request_id IS NULL AND feature_id IS NULL AND run_id IS NULL)
);

-- 4. Partial index for per-fact lookups (mirrors events_run_idx / events_feature_idx).
CREATE INDEX IF NOT EXISTS events_overlay_fact_idx
    ON events (overlay_fact_id) WHERE overlay_fact_id IS NOT NULL;
```

- [ ] **Step 4: Thread `overlay_fact_id` through `EventEnvelope` and `NewEvent`**

In `src/featuregen/contracts/envelopes.py`, add the field to `EventEnvelope` (after `run_id`):

```python
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    caused_by: str | None = None
```

And to `NewEvent` (after `run_id`):

```python
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    caused_by: str | None = None
    occurred_at: datetime | None = None
```

- [ ] **Step 5: Thread `overlay_fact_id` through `events/store.py`**

Update `_INSERT` to list the new column (add `overlay_fact_id` to both the column list and the
VALUES list):

```python
_INSERT = """
INSERT INTO events (
    event_id, aggregate, aggregate_id, stream_version,
    request_id, feature_id, run_id, overlay_fact_id, type, schema_version, table_version,
    actor, payload, provenance, caused_by, occurred_at
) VALUES (
    %(event_id)s, %(aggregate)s, %(aggregate_id)s, %(stream_version)s,
    %(request_id)s, %(feature_id)s, %(run_id)s, %(overlay_fact_id)s, %(type)s, %(schema_version)s, %(table_version)s,
    %(actor)s, %(payload)s, %(provenance)s, %(caused_by)s, %(occurred_at)s
)
RETURNING global_seq, recorded_at
"""
```

Add the param (in the `params = {...}` dict, after `"run_id"`):

```python
        "run_id": new_event.run_id,
        "overlay_fact_id": new_event.overlay_fact_id,
```

Add the field to the returned `EventEnvelope(...)` (after `run_id=`):

```python
        run_id=new_event.run_id,
        overlay_fact_id=new_event.overlay_fact_id,
        caused_by=new_event.caused_by,
    )
```

- [ ] **Step 6: Thread `overlay_fact_id` through `events/serde.py::row_to_event`**

Add the field to the constructed `EventEnvelope` (after `run_id=row["run_id"]`):

```python
        run_id=row["run_id"],
        overlay_fact_id=row["overlay_fact_id"],
        caused_by=row["caused_by"],
    )
```

- [ ] **Step 7: Add the `overlay_fact_id` kwarg to `aggregates/_append.py::append`**

Add the keyword to the signature (after `run_id`) and pass it into the `NewEvent`:

```python
def append(
    conn: DbConn,
    *,
    aggregate: str,
    aggregate_id: str,
    type: str,
    payload: Mapping[str, Any],
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope | None = None,
    request_id: str | None = None,
    feature_id: str | None = None,
    run_id: str | None = None,
    overlay_fact_id: str | None = None,
    caused_by: str | None = None,
    expected_version: int | None = None,
) -> EventEnvelope:
    if expected_version is None:
        expected_version = current_version(conn, aggregate, aggregate_id)
    new_event = NewEvent(
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        type=type,
        schema_version=1,
        payload=dict(payload),
        actor=actor,
        provenance=provenance or provenance_for(),  # §3.7 artifact_type, NOT the event-type name
        request_id=request_id,
        feature_id=feature_id,
        run_id=run_id,
        overlay_fact_id=overlay_fact_id,
        caused_by=caused_by,
        occurred_at=datetime.now(UTC),
    )
    return append_event(
        conn,
        new_event,
        expected_version=expected_version,
        table_version=table_version_for(conn, aggregate, aggregate_id),
    )
```

- [ ] **Step 8: Add the `overlay_fact` branch to `runtime/outbox.py::partition_key_for`**

```python
def partition_key_for(event: EventEnvelope) -> str:
    """Aggregate-key partition (§5.2): feature-/request-stream events (run_id null)
    still get per-aggregate ordering."""
    if event.aggregate == "run":
        return f"run:{event.run_id or event.aggregate_id}"
    if event.aggregate == "feature":
        return f"feature:{event.feature_id or event.aggregate_id}"
    if event.aggregate == "request":
        return f"request:{event.request_id or event.aggregate_id}"
    if event.aggregate == "overlay_fact":
        return f"overlay_fact:{event.overlay_fact_id or event.aggregate_id}"
    raise ValueError(f"unknown aggregate {event.aggregate!r}")
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/events/test_overlay_events.py -v`
Expected: PASS (5 passed).

- [ ] **Step 10: Commit**

```bash
git add src/featuregen/db/migrations/0080_overlay_events.sql \
        src/featuregen/contracts/envelopes.py src/featuregen/events/store.py \
        src/featuregen/events/serde.py src/featuregen/aggregates/_append.py \
        src/featuregen/runtime/outbox.py tests/featuregen/events/test_overlay_events.py
git commit -m "feat(overlay): add overlay_fact aggregate + overlay_fact_id to events append path"
```

---

### Task 1.2: `human_tasks` — overlay gate columns + gate enum, `GateTaskSpec.fact_key`, `_task_aggregate`/`open_task` plumbing

**Files:**
- Create: `src/featuregen/db/migrations/0081_overlay_gates.sql`
- Modify: `src/featuregen/contracts/envelopes.py:197-208` (`GateTaskSpec`)
- Modify: `src/featuregen/gates/tasks.py:20-23` (`_task_aggregate`), `:26-70` (`open_task` INSERT)
- Test: `tests/featuregen/gates/test_overlay_gates.py`

**Interfaces:**
- Consumes: `open_task(conn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str`; `GateTaskSpec(gate, required_inputs, eligible_assignees, allowed_responses, run_id=None, feature_id=None, quorum_required=1, quorum_of_role=None, delegation_allowed=True, sla=None)`; `_task_aggregate(run_id, feature_id, fact_key=None) -> tuple[str, str]`; `build_service_identity(...)`; `apply_migrations(conn)`.
- Produces: `GateTaskSpec.fact_key/draft_event_id/target_event_id/evidence_ref: str | None`; `human_tasks` rows carrying those columns; gate CHECK admits `OVERLAY_DATA_OWNER`/`OVERLAY_COMPLIANCE`; `_task_aggregate(..., fact_key=...)` returns `("overlay_fact", fact_key)` when `fact_key` set.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/gates/test_overlay_gates.py
from __future__ import annotations

import psycopg
import pytest

from featuregen.contracts.gates import GateTaskSpec
from featuregen.db.migrations import apply_migrations
from featuregen.gates.tasks import _task_aggregate, open_task
from featuregen.identity.build import build_service_identity


def _svc():
    return build_service_identity(
        subject="service:overlay",
        role_claims=["overlay"],
        attestation="signed-deploy-id:overlay@1.0.0",
    )


def _overlay_spec(**kw):
    base = dict(
        gate="OVERLAY_DATA_OWNER",
        required_inputs=("proposed_value",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "reject"),
        fact_key="a1b2c3",
        draft_event_id="evt_draft",
        target_event_id="evt_draft",
        evidence_ref="eviu_1",
    )
    base.update(kw)
    return GateTaskSpec(**base)


def test_task_aggregate_fact_key_arm():
    assert _task_aggregate(None, None, "fk1") == ("overlay_fact", "fk1")
    assert _task_aggregate("run_1", None) == ("run", "run_1")
    assert _task_aggregate(None, "feat_1") == ("feature", "feat_1")


def test_open_task_with_fact_key_inserts_overlay_columns(db):
    task_id = open_task(db, _overlay_spec(), _svc())
    row = db.execute(
        "SELECT gate, fact_key, draft_event_id, target_event_id, evidence_ref, run_id, feature_id "
        "FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == ("OVERLAY_DATA_OWNER", "a1b2c3", "evt_draft", "evt_draft", "eviu_1", None, None)


def test_gate_check_accepts_overlay_compliance(db):
    task_id = open_task(db, _overlay_spec(gate="OVERLAY_COMPLIANCE"), _svc())
    gate = db.execute(
        "SELECT gate FROM human_tasks WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert gate == "OVERLAY_COMPLIANCE"


def test_gate_check_rejects_unknown_gate(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO human_tasks (task_id, gate, eligible_assignees, allowed_responses) "
            "VALUES ('task_bad','NOT_A_GATE','{}'::jsonb, ARRAY['x'])"
        )


def test_overlay_gates_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='human_tasks'"
        ).fetchall()
    }
    assert {"fact_key", "draft_event_id", "target_event_id", "evidence_ref"} <= cols
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/gates/test_overlay_gates.py -v`
Expected: FAIL — `GateTaskSpec` has no `fact_key` keyword; `_task_aggregate` takes 2 args; gate CHECK rejects `OVERLAY_DATA_OWNER`.

- [ ] **Step 3: Create the `0081_overlay_gates.sql` migration**

```sql
-- src/featuregen/db/migrations/0081_overlay_gates.sql
-- SP-1 Phase 1 (design §2.2): extend SP-0's human-gate model for overlay confirmations.
-- Additive + idempotent.

-- 1. Overlay routing/CAS columns (nullable, like run_id/feature_id).
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS fact_key        text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS draft_event_id  text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS target_event_id text;
ALTER TABLE human_tasks ADD COLUMN IF NOT EXISTS evidence_ref    text;

-- 2. Widen the gate CHECK (auto-named human_tasks_gate_check) with the two overlay gates.
ALTER TABLE human_tasks DROP CONSTRAINT IF EXISTS human_tasks_gate_check;
ALTER TABLE human_tasks ADD CONSTRAINT human_tasks_gate_check CHECK (
    gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
             'INDEPENDENT_VALIDATION','FINAL_APPROVAL',
             'OVERLAY_DATA_OWNER','OVERLAY_COMPLIANCE')
);

-- 3. Partial index for open overlay tasks by fact_key.
CREATE INDEX IF NOT EXISTS human_tasks_fact_idx
    ON human_tasks (fact_key) WHERE status = 'open';
```

- [ ] **Step 4: Add the overlay fields to `GateTaskSpec`**

In `src/featuregen/contracts/envelopes.py`, append the four fields to `GateTaskSpec` (all have
defaults, placed last):

```python
@dataclass(frozen=True, slots=True)
class GateTaskSpec:
    gate: str
    required_inputs: tuple[str, ...]
    eligible_assignees: Mapping[str, str]
    allowed_responses: tuple[str, ...]
    run_id: str | None = None
    feature_id: str | None = None
    quorum_required: int = 1
    quorum_of_role: str | None = None
    delegation_allowed: bool = True
    sla: str | None = None
    fact_key: str | None = None
    draft_event_id: str | None = None
    target_event_id: str | None = None
    evidence_ref: str | None = None
```

- [ ] **Step 5: Add the `fact_key` arm to `_task_aggregate`**

```python
def _task_aggregate(run_id, feature_id, fact_key=None) -> tuple[str, str]:
    if fact_key:
        return "overlay_fact", fact_key
    if run_id:
        return "run", run_id
    return "feature", feature_id
```

- [ ] **Step 6: Thread the overlay columns through `open_task`**

Replace the INSERT and the SLA-ladder `_task_aggregate` call in `open_task`:

```python
def open_task(conn: DbConn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str:
    task_id = mint_id("task")
    conn.execute(
        """
        INSERT INTO human_tasks
            (task_id, task_version, run_id, feature_id, gate, required_inputs,
             eligible_assignees, allowed_responses, quorum_required, quorum_of_role,
             delegation_allowed, sla, status,
             fact_key, draft_event_id, target_event_id, evidence_ref)
        VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s)
        """,
        (
            task_id,
            spec.run_id,
            spec.feature_id,
            spec.gate,
            list(spec.required_inputs),
            Json(dict(spec.eligible_assignees)),
            list(spec.allowed_responses),
            spec.quorum_required,
            spec.quorum_of_role,
            spec.delegation_allowed,
            spec.sla,
            spec.fact_key,
            spec.draft_event_id,
            spec.target_event_id,
            spec.evidence_ref,
        ),
    )
    if spec.sla:
        base = datetime.now(UTC)
        sla = parse_duration(spec.sla)
        agg, agg_id = _task_aggregate(spec.run_id, spec.feature_id, spec.fact_key)
        ladder = {
            "reminder": base + sla / 2,
            "sla": base + sla,
            "escalation": base + sla + sla / 2,
            "auto_park": base + sla * 2,
        }
        for kind, fire_at in ladder.items():
            conn.execute(
                """
                INSERT INTO timers
                    (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind,
                     fire_at, status, cas_task_version)
                VALUES (%s,%s,%s,%s,%s,%s,%s,'scheduled',1)
                """,
                (mint_id("tmr"), f"{task_id}:{kind}", agg, agg_id, task_id, kind, fire_at),
            )
    return task_id
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/gates/test_overlay_gates.py -v`
Expected: PASS (5 passed).

- [ ] **Step 8: Commit**

```bash
git add src/featuregen/db/migrations/0081_overlay_gates.sql \
        src/featuregen/contracts/envelopes.py src/featuregen/gates/tasks.py \
        tests/featuregen/gates/test_overlay_gates.py
git commit -m "feat(overlay): extend human_tasks gate model for overlay confirmations"
```

---

### Task 1.3: `timers` — add `overlay_expiry` kind

**Files:**
- Create: `src/featuregen/db/migrations/0082_overlay_timers.sql`
- Test: `tests/featuregen/runtime/test_overlay_timers.py`

**Interfaces:**
- Consumes: `schedule_timer(conn, aggregate: str, aggregate_id: str, timer: NewTimer) -> str` (`featuregen.runtime.timers`); `NewTimer(kind, fire_at, idempotency_key, task_id=None, business_calendar=None, cas_task_version=None, payload={})`; `apply_migrations(conn)`.
- Produces: `timers.kind` CHECK admits `'overlay_expiry'`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/runtime/test_overlay_timers.py
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import NewTimer
from featuregen.db.migrations import apply_migrations
from featuregen.runtime.timers import schedule_timer


def test_schedule_overlay_expiry_timer_succeeds(conn):
    timer_id = schedule_timer(
        conn,
        "overlay_fact",
        "a1b2c3",
        NewTimer(
            kind="overlay_expiry",
            fire_at=datetime(2026, 12, 1, tzinfo=UTC),
            idempotency_key="a1b2c3:expiry",
        ),
    )
    assert timer_id.startswith("tmr_")
    row = conn.execute(
        "SELECT kind, aggregate, aggregate_id, status FROM timers WHERE timer_id=%s",
        (timer_id,),
    ).fetchone()
    assert row == ("overlay_expiry", "overlay_fact", "a1b2c3", "scheduled")


def test_overlay_timers_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='timers_kind_check'"
    ).fetchone()
    assert chk is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/runtime/test_overlay_timers.py -v`
Expected: FAIL — inserting `kind='overlay_expiry'` violates `timers_kind_check`.

- [ ] **Step 3: Create the `0082_overlay_timers.sql` migration**

```sql
-- src/featuregen/db/migrations/0082_overlay_timers.sql
-- SP-1 Phase 1 (design §2.3): admit the overlay expiry timer kind. Additive + idempotent.
ALTER TABLE timers DROP CONSTRAINT IF EXISTS timers_kind_check;
ALTER TABLE timers ADD CONSTRAINT timers_kind_check CHECK (
    kind IN ('sla','reminder','escalation','auto_park',
             'experiment_expiry','business_repair','cost_breaker','overlay_expiry')
);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/runtime/test_overlay_timers.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0082_overlay_timers.sql \
        tests/featuregen/runtime/test_overlay_timers.py
git commit -m "feat(overlay): add overlay_expiry timer kind"
```

---

### Task 1.4: overlay read/evidence/dependency/fingerprint tables + projection-checkpoint init

**Files:**
- Create: `src/featuregen/db/migrations/0083_overlay_tables.sql`
- Test: `tests/featuregen/db/test_overlay_tables.py`

**Interfaces:**
- Consumes: `apply_migrations(conn)`; `projection_checkpoints(projection_name PK, checkpoint_seq, head_seq, is_analytics, updated_at)` (created by Stage-1 `0005_projection_checkpoints`).
- Produces: tables `overlay_fact_state`, `overlay_proposal`, `overlay_evidence`, `overlay_fact_dependency` (+ `(ref_object)` index), `overlay_catalog_object`; a `projection_checkpoints` row `('overlay')` (seq 0, non-analytics) — consumed by `OverlayProjection` (Phase 2.6) and the readers in Phases 5–7.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/db/test_overlay_tables.py
from __future__ import annotations

from featuregen.db.migrations import apply_migrations

_TABLES = (
    "overlay_fact_state",
    "overlay_proposal",
    "overlay_evidence",
    "overlay_fact_dependency",
    "overlay_catalog_object",
)


def test_overlay_tables_exist(conn):
    apply_migrations(conn)
    for table in _TABLES:
        reg = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()[0]
        assert reg is not None, f"missing table {table}"


def test_overlay_projection_checkpoint_seeded(conn):
    apply_migrations(conn)
    row = conn.execute(
        "SELECT projection_name, checkpoint_seq, head_seq, is_analytics "
        "FROM projection_checkpoints WHERE projection_name='overlay'"
    ).fetchone()
    assert row == ("overlay", 0, 0, False)


def test_overlay_tables_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    n = conn.execute(
        "SELECT count(*) FROM projection_checkpoints WHERE projection_name='overlay'"
    ).fetchone()[0]
    assert n == 1  # ON CONFLICT DO NOTHING — re-apply does not duplicate the row
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='overlay_fact_dependency_ref_idx'"
    ).fetchone()
    assert idx is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/db/test_overlay_tables.py -v`
Expected: FAIL — `to_regclass` returns `None` for the overlay tables; no `'overlay'` checkpoint row.

- [ ] **Step 3: Create the `0083_overlay_tables.sql` migration**

```sql
-- src/featuregen/db/migrations/0083_overlay_tables.sql
-- SP-1 Phase 1: overlay read model, immutable evidence, dependency index, catalog fingerprint
-- snapshot, and projection-checkpoint init. All CREATE … IF NOT EXISTS; checkpoint insert is
-- ON CONFLICT DO NOTHING — fully idempotent.

-- Hot merged-view read model (one row per fact_key).
CREATE TABLE IF NOT EXISTS overlay_fact_state (
    fact_key           text        PRIMARY KEY,
    object_ref         text        NOT NULL,
    fact_type          text        NOT NULL,
    use_case           text        NULL,
    status             text        NOT NULL,
    value              jsonb       NULL,
    confirmers         jsonb       NOT NULL DEFAULT '[]',
    confirmed_at       timestamptz NULL,
    expires_at         timestamptz NULL,
    prior_value        jsonb       NULL,
    confirmed_event_id text        NULL,
    updated_seq        bigint      NOT NULL
);

-- Workflow / task read model (in-flight proposals & re-verifications).
CREATE TABLE IF NOT EXISTS overlay_proposal (
    fact_key             text        PRIMARY KEY,
    status               text        NOT NULL,
    proposed_value       jsonb       NOT NULL,
    proposal_fingerprint text        NOT NULL,
    draft_event_id       text        NOT NULL,
    target_event_id      text        NULL,
    evidence_ref         text        NULL,
    partial_confirmers   jsonb       NOT NULL DEFAULT '[]',
    object_ref           text        NOT NULL,
    fact_type            text        NOT NULL,
    use_case             text        NULL,
    updated_seq          bigint      NOT NULL
);

-- Immutable evidence (written at propose time — NOT a projection; aggregate metrics only).
CREATE TABLE IF NOT EXISTS overlay_evidence (
    evidence_id       text        PRIMARY KEY,
    fact_key          text        NOT NULL,
    table_snapshot_at timestamptz NULL,
    row_count         bigint      NULL,
    sample_size       bigint      NULL,
    profile_version   text        NULL,
    thresholds_used   jsonb       NULL,
    metric_values     jsonb       NULL,
    created_by        jsonb       NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now()
);

-- General dependency index (projection-maintained): which facts reference which objects.
CREATE TABLE IF NOT EXISTS overlay_fact_dependency (
    fact_key   text NOT NULL,
    ref_object text NOT NULL,
    PRIMARY KEY (fact_key, ref_object)
);
CREATE INDEX IF NOT EXISTS overlay_fact_dependency_ref_idx
    ON overlay_fact_dependency (ref_object);

-- Catalog fingerprint snapshot for change detection.
CREATE TABLE IF NOT EXISTS overlay_catalog_object (
    object_ref          text        PRIMARY KEY,
    native_oid          text        NULL,
    columns_fingerprint text        NULL,
    type_fingerprint    text        NULL,
    last_seen_seq       bigint      NULL,
    updated_at          timestamptz NOT NULL DEFAULT now()
);

-- Projection checkpoint for the overlay (non-analytics, fail-closed) projection.
INSERT INTO projection_checkpoints (projection_name) VALUES ('overlay')
ON CONFLICT DO NOTHING;
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/db/test_overlay_tables.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0083_overlay_tables.sql \
        tests/featuregen/db/test_overlay_tables.py
git commit -m "feat(overlay): add overlay read/evidence/dependency tables + projection checkpoint"
```

---

## Phase 1 exit check

- [ ] **Run the whole touched surface + the existing idempotency suite green**

Run: `uv run pytest tests/featuregen/events/test_overlay_events.py tests/featuregen/gates/ tests/featuregen/runtime/test_overlay_timers.py tests/featuregen/db/ -v`
Expected: PASS — all four new migrations apply idempotently and the SP-0 gate/timer tests still pass (no regression from the widened CHECKs).
