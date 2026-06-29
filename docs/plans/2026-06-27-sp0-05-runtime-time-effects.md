## Phase 05: Durable runtime II — timers, retries, external effects, cost, blob GC

**Goal:** Implement the time-and-effects half of the durable runtime — durable timers + business-calendar poller + escalation ladder + timer/answer CAS race, external-command outbox + dispatcher (idempotency-key / job-handle reconciliation, honest residual-duplicate caveat, stale-result guard, result caching), delivery-retry vs business-repair-loop classification with `max_elapsed_time`, the per-run/per-request cost-budget circuit breaker, and pre-transaction blob orphan GC (mark-sweep, quarantine, audited) — all built against the shared contract.

> REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Every task is TDD (failing test → watch fail → minimal code → watch pass → commit). Steps use checkbox (`- [ ]`) syntax.

---

### Consumes (shared symbols — do NOT redefine; import only)

From `src/featuregen/contracts/` (declared by the overview; Phase 01 owns the type alias, Phase 04 owns the runtime dataclasses):

```python
from featuregen.contracts import DbConn            # = psycopg.Connection[Any]; the active tx handle (Phase 01)
from featuregen.contracts import NewTimer          # kind, fire_at, idempotency_key, task_id, business_calendar,
                                            #   cas_task_version, payload  (Phase 04)
from featuregen.contracts import NewExternalCommand# integration, idempotency_key, request_payload, expected_run_id,
                                            #   expected_stream_version, expected_task_id, job_handle,
                                            #   dedup_supported  (Phase 04)
from featuregen.contracts import Disposition       # Enum: OK / RETRYABLE / PERMANENT  (Phase 04)
```

Shared DDL this phase **reads** but does not own: `events` (Phase 01), `documents` + `blob_index` (Phase 02), `queue` + `outbox` (Phase 04), `run_workflow_state` (Phase 01). Shared DDL this phase **creates** (verbatim from the overview): `timers`, `external_commands`. **`blob_index` is created by Phase 02** (its `0002_documents.sql`, Task 2 — the overview assigns the `blob_index` schema to Phase 02); this phase only builds the mark-and-sweep GC mechanism over it and does NOT re-create the table. One phase-owned supporting table not in the shared DDL: `business_calendars` (does not redefine any shared symbol).

This phase deliberately does NOT call `append_event`/`open_task`/`submit_human_signal`. Firing a timer or tripping the breaker **enqueues a work message** onto the Phase 04 `queue` (idempotent by `message_id`); downstream phases register the handlers that turn those into domain events / human tasks. The timer/answer CAS reads the gate task version through an **injected resolver** (`resolve_task_version`), so there is **no compile-time / import dependency on Phase 07**. Honest caveat: the *library default* resolver (`_default_task_version`, Task 4) issues a runtime `SELECT … FROM human_tasks` (Phase 07 owns that table), so the **default code path has a runtime dependency on Phase 07**. Callers that run before Phase 07 exists — including every Task-4 test — inject their own `resolve_task_version` to avoid it.

### Test harness assumptions

`tests/conftest.py` (Phase 01) provides a function-scoped `conn` fixture: a `psycopg` connection to a throwaway PostgreSQL 15+ database with every `src/featuregen/db/migrations/*.sql` applied in lexical order, autocommit off, rolled back after each test. Phase 05 migrations use the `05xx_` numeric prefix so they sort AFTER the core tables they reference. Run commands assume the repo root is on `PYTHONPATH` and `pytest` discovers `tests/`.

---

### File structure

```
src/featuregen/
  runtime/
    __init__.py                 # package marker (created by Phase 04; add if running Phase 05 first)
    business_calendar.py        # Task 1  — duration parsing + business-day deadline resolution
    timers.py                   # Tasks 2-4 — schedule_timer, ladder builder, poller, fire+CAS, cancel
    external_commands.py        # Tasks 5-7 — record, dispatcher, stale-result guard, result cache
    retries.py                  # Task 8  — backoff+jitter, max_elapsed budget, delivery-retry → DLQ
    repair_loop.py              # Task 9  — business repair loop (attempt counting, bounded N, re-arm)
    cost_budget.py              # Task 10 — per-run/per-request cost counter + breaker + auto-park
    blob_gc.py                  # Task 11 — register_blob + mark-and-sweep + quarantine + audit
  db/migrations/
    0501_business_calendars.sql # Task 1
    0502_timers.sql             # Task 2  (verbatim shared DDL)
    0503_external_commands.sql  # Task 5  (verbatim shared DDL)
    # blob_index is created by Phase 02 (0002_documents.sql); Phase 05 only builds GC over it
tests/featuregen/runtime/
    conftest.py                 # Task 1  — seed/fake fixtures (events, run_state, documents, callers)
    test_business_calendar.py   # Task 1
    test_timers_schedule.py     # Task 2
    test_timers_poller.py       # Task 3
    test_timers_fire.py         # Task 4
    test_external_commands_record.py    # Task 5
    test_external_commands_dispatch.py  # Task 6
    test_external_commands_stale.py     # Task 7
    test_retries.py             # Task 8
    test_repair_loop.py         # Task 9
    test_cost_budget.py         # Task 10
    test_blob_gc.py             # Task 11
```

---

## Task 1 — Business-calendar deadline resolution

Durable timers are "resolved against a named business calendar" (§5.5). This task creates the phase-owned calendar store, the shared test fixtures, and the deterministic (replay-safe) deadline resolver everything else builds on.

**Files:**
- Create: `src/featuregen/db/migrations/0501_business_calendars.sql`
- Create: `src/featuregen/runtime/business_calendar.py`
- Create: `tests/featuregen/runtime/conftest.py`
- Test: `tests/featuregen/runtime/test_business_calendar.py`

**Interfaces:**
- Consumes: `DbConn` (Phase 01); `conn` test fixture (Phase 01).
- Produces:
  - `parse_duration(spec: str) -> tuple[int, str]`
  - `resolve_business_deadline(conn: DbConn, calendar_name: Optional[str], start: datetime, spec: str) -> datetime`
  - Table `business_calendars(calendar_name PK, timezone, workdays int[], holidays date[], created_at)`
  - Fixtures in `tests/featuregen/runtime/conftest.py`: `insert_stub_event`, `insert_run_state`, `insert_stub_document` (seed helpers); `recording_caller`, `recording_deleter`, `recording_audit` (fakes) — produced incrementally; this task introduces `insert_stub_event`, `insert_run_state`, `insert_stub_document`.

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_business_calendar.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from featuregen.runtime.business_calendar import parse_duration, resolve_business_deadline

UTC = timezone.utc


def _seed_calendar(conn, name="ops", workdays=(1, 2, 3, 4, 5), holidays=()):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO business_calendars (calendar_name, timezone, workdays, holidays) "
            "VALUES (%s, 'UTC', %s, %s)",
            (name, list(workdays), list(holidays)),
        )


def test_parse_duration_units():
    assert parse_duration("7d") == (7, "d")
    assert parse_duration("4h") == (4, "h")
    assert parse_duration("30m") == (30, "m")
    assert parse_duration("45s") == (45, "s")


@pytest.mark.parametrize("bad", ["", "d", "7", "7x", "-3d", "abcd"])
def test_parse_duration_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_wall_clock_units_ignore_calendar(conn):
    _seed_calendar(conn)
    start = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # a Friday
    assert resolve_business_deadline(conn, "ops", start, "4h") == start + timedelta(hours=4)
    assert resolve_business_deadline(conn, "ops", start, "90m") == start + timedelta(minutes=90)


def test_business_days_skip_weekend(conn):
    _seed_calendar(conn)
    friday = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # Fri 2026-06-26
    # 1 business day from Friday -> Monday 2026-06-29 (skip Sat/Sun)
    assert resolve_business_deadline(conn, "ops", friday, "1d").date().isoformat() == "2026-06-29"


def test_business_days_skip_holiday(conn):
    from datetime import date

    _seed_calendar(conn, holidays=(date(2026, 6, 29),))  # Monday is a holiday
    friday = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)
    # 1 business day -> skip Sat, Sun, Mon-holiday -> Tuesday 2026-06-30
    assert resolve_business_deadline(conn, "ops", friday, "1d").date().isoformat() == "2026-06-30"


def test_days_without_calendar_are_calendar_days(conn):
    start = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)
    assert resolve_business_deadline(conn, None, start, "2d") == start + timedelta(days=2)
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_business_calendar.py -q` → fails with `ModuleNotFoundError: No module named 'featuregen.runtime.business_calendar'` (and the `business_calendars` relation does not exist).

- [ ] **(3) Write minimal implementation.**

`src/featuregen/db/migrations/0501_business_calendars.sql`:

```sql
-- business_calendars — phase-05-owned calendar store for §5.5 timer resolution.
-- Not part of the shared DDL; redefines nothing.
CREATE TABLE business_calendars (
    calendar_name text        PRIMARY KEY,
    timezone      text        NOT NULL DEFAULT 'UTC',
    workdays      integer[]   NOT NULL DEFAULT '{1,2,3,4,5}',   -- ISO weekday 1=Mon..7=Sun
    holidays      date[]      NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now()
);
```

`src/featuregen/runtime/business_calendar.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from featuregen.contracts import DbConn

_WALL_CLOCK_SECONDS = {"h": 3600, "m": 60, "s": 1}


def parse_duration(spec: str) -> tuple[int, str]:
    """Parse '7d' / '4h' / '30m' / '45s' into (amount, unit). 'd' = business days when a
    calendar is named, else calendar days; 'h'/'m'/'s' are always wall-clock."""
    spec = spec.strip().lower()
    if len(spec) < 2:
        raise ValueError(f"unparseable duration: {spec!r}")
    unit = spec[-1]
    if unit not in ("d", "h", "m", "s"):
        raise ValueError(f"unknown duration unit in {spec!r}")
    try:
        amount = int(spec[:-1])
    except ValueError as exc:
        raise ValueError(f"unparseable duration amount in {spec!r}") from exc
    if amount < 0:
        raise ValueError(f"negative duration not allowed: {spec!r}")
    return amount, unit


@dataclass(frozen=True, slots=True)
class _Calendar:
    workdays: frozenset[int]
    holidays: frozenset[date]


def _load_calendar(conn: DbConn, name: str) -> _Calendar:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT workdays, holidays FROM business_calendars WHERE calendar_name = %s",
            (name,),
        )
        row = cur.fetchone()
    if row is None:
        raise KeyError(f"unknown business calendar: {name!r}")
    workdays, holidays = row
    return _Calendar(frozenset(workdays), frozenset(holidays))


def _add_business_days(cal: _Calendar, start: datetime, days: int) -> datetime:
    cursor = start
    remaining = days
    while remaining > 0:
        cursor = cursor + timedelta(days=1)
        if cursor.isoweekday() in cal.workdays and cursor.date() not in cal.holidays:
            remaining -= 1
    return cursor


def resolve_business_deadline(
    conn: DbConn, calendar_name: Optional[str], start: datetime, spec: str
) -> datetime:
    """Resolve a duration spec to an absolute fire time (§5.5). Deterministic so timer
    deadlines reproduce on replay. 'd' against a named calendar counts BUSINESS days
    (skipping non-workdays + holidays); without a calendar 'd' is calendar days."""
    amount, unit = parse_duration(spec)
    if unit in _WALL_CLOCK_SECONDS:
        return start + timedelta(seconds=amount * _WALL_CLOCK_SECONDS[unit])
    if calendar_name is None:
        return start + timedelta(days=amount)
    return _add_business_days(_load_calendar(conn, calendar_name), start, amount)
```

`tests/featuregen/runtime/conftest.py` (seed helpers used across this phase):

```python
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import pytest


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
    def _insert(conn, *, run_id: str, request_id: str,
                cost: Decimal = Decimal("0"), candidates: int = 0) -> None:
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
    def _insert(conn, *, doc_id: str, body_ref: Optional[str],
                classification: str = "governance-retained") -> None:
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
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_business_calendar.py -q` → all green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: business-calendar deadline resolution (§5.5)"`

---

## Task 2 — `timers` table, `schedule_timer`, escalation-ladder builder

Create the shared `timers` table (verbatim) and the idempotent insert + the escalation-ladder composer (§5.5). `open_task` (Phase 07) composes the ladder via `build_escalation_ladder` and schedules each returned rung via `schedule_timer` inside the §5.1 atomic step (note `open_task(conn, spec, actor) -> str` returns a task_id, not a HandlerResult — see the shared contract). **Rung ordering:** §5.5 lists the conceptual ladder as "SLA → reminder → escalation → auto-park", but the rungs are returned and fire in chronological *fire-time* order, which is `reminder → sla → escalation → auto_park`: the reminder is a courtesy nudge that deliberately fires BEFORE the SLA deadline (`reminder < sla`). The spec's listing is the conceptual ladder, not the fire sequence.

**Files:**
- Create: `src/featuregen/db/migrations/0502_timers.sql`
- Create: `src/featuregen/runtime/timers.py`
- Test: `tests/featuregen/runtime/test_timers_schedule.py`

**Interfaces:**
- Consumes: `DbConn`, `NewTimer` (contract); `resolve_business_deadline` (Task 1).
- Produces:
  - `schedule_timer(conn: DbConn, aggregate: str, aggregate_id: str, timer: NewTimer) -> str`
  - `build_escalation_ladder(conn: DbConn, *, aggregate: str, aggregate_id: str, task_id: str, task_version: int, opened_at: datetime, sla: str, reminder: str, escalation: str, business_calendar: Optional[str] = None) -> tuple[NewTimer, ...]`
  - Table `timers` (shared DDL).

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_timers_schedule.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone

from featuregen.contracts import NewTimer
from featuregen.runtime.timers import build_escalation_ladder, schedule_timer

UTC = timezone.utc


def _count(conn, sql, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def test_schedule_timer_inserts_row(conn):
    t = NewTimer(kind="sla", fire_at=datetime(2026, 7, 1, tzinfo=UTC),
                 idempotency_key="k1", task_id="task_1")
    tid = schedule_timer(conn, "run", "run_1", t)
    assert tid
    assert _count(conn, "SELECT count(*) FROM timers WHERE idempotency_key='k1'") == 1
    with conn.cursor() as cur:
        cur.execute("SELECT status, aggregate, aggregate_id, task_id FROM timers WHERE timer_id=%s", (tid,))
        assert cur.fetchone() == ("scheduled", "run", "run_1", "task_1")


def test_schedule_timer_idempotent(conn):
    t = NewTimer(kind="reminder", fire_at=datetime(2026, 7, 1, tzinfo=UTC), idempotency_key="dup")
    a = schedule_timer(conn, "run", "run_1", t)
    b = schedule_timer(conn, "run", "run_1", t)
    assert a == b
    assert _count(conn, "SELECT count(*) FROM timers WHERE idempotency_key='dup'") == 1


def test_build_escalation_ladder(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO business_calendars (calendar_name) VALUES ('ops')")
    opened = datetime(2026, 6, 26, 9, 0, tzinfo=UTC)  # Friday
    ladder = build_escalation_ladder(
        conn, aggregate="run", aggregate_id="run_1", task_id="task_9", task_version=3,
        opened_at=opened, sla="2d", reminder="1d", escalation="1d", business_calendar="ops",
    )
    kinds = [t.kind for t in ladder]
    # fire-time order: the reminder fires BEFORE the SLA deadline; §5.5 lists the
    # conceptual ladder as SLA -> reminder -> escalation -> auto-park (see Task 2 intro).
    assert kinds == ["reminder", "sla", "escalation", "auto_park"]
    assert all(t.cas_task_version == 3 for t in ladder)
    assert all(t.idempotency_key.startswith("ladder:task_9:v3:") for t in ladder)
    fire_times = [t.fire_at for t in ladder]
    assert fire_times == sorted(fire_times)  # monotonically increasing rungs
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_timers_schedule.py -q` → `ImportError: cannot import name 'schedule_timer'` (and `relation "timers" does not exist`).

- [ ] **(3) Write minimal implementation.**

`src/featuregen/db/migrations/0502_timers.sql` (verbatim shared DDL):

```sql
CREATE TABLE timers (
    timer_id          text        PRIMARY KEY,
    idempotency_key   text        NOT NULL UNIQUE,
    aggregate         text        NOT NULL,
    aggregate_id      text        NOT NULL,
    task_id           text        NULL,
    kind              text        NOT NULL
                          CHECK (kind IN ('sla','reminder','escalation','auto_park',
                                          'experiment_expiry','business_repair','cost_breaker')),
    fire_at           timestamptz NOT NULL,
    business_calendar text        NULL,
    status            text        NOT NULL DEFAULT 'scheduled'
                          CHECK (status IN ('scheduled','leased','fired','cancelled')),
    lease_owner       text        NULL,
    lease_expires_at  timestamptz NULL,
    cas_task_version  integer     NULL,
    payload           jsonb       NOT NULL DEFAULT '{}',
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX timers_due_idx  ON timers (fire_at) WHERE status = 'scheduled';
CREATE INDEX timers_task_idx ON timers (task_id) WHERE task_id IS NOT NULL;
```

`src/featuregen/runtime/timers.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from psycopg.types.json import Jsonb
from ulid import ULID  # python-ulid (declared by Phase 01); ULID-style id minting

from featuregen.contracts import DbConn, NewTimer
from featuregen.runtime.business_calendar import resolve_business_deadline


def schedule_timer(conn: DbConn, aggregate: str, aggregate_id: str, timer: NewTimer) -> str:
    """Insert one durable timer (status='scheduled'). The PK is a freshly minted ULID-style
    'tmr_…' id (overview id convention); idempotency is enforced by the UNIQUE
    idempotency_key, so a duplicate schedule creates no second row and returns the EXISTING
    timer_id. Used by the §5.1 atomic step (Phase 04) and by the poller for re-arming."""
    timer_id = f"tmr_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO timers (timer_id, idempotency_key, aggregate, aggregate_id, task_id,
                                kind, fire_at, business_calendar, cas_task_version, payload)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING timer_id
            """,
            (timer_id, timer.idempotency_key, aggregate, aggregate_id, timer.task_id,
             timer.kind, timer.fire_at, timer.business_calendar, timer.cas_task_version,
             Jsonb(dict(timer.payload))),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute("SELECT timer_id FROM timers WHERE idempotency_key = %s",
                    (timer.idempotency_key,))
        return cur.fetchone()[0]


def build_escalation_ladder(
    conn: DbConn,
    *,
    aggregate: str,
    aggregate_id: str,
    task_id: str,
    task_version: int,
    opened_at: datetime,
    sla: str,
    reminder: str,
    escalation: str,
    business_calendar: Optional[str] = None,
) -> tuple[NewTimer, ...]:
    """Compose the escalation ladder (§5.5) as durable timers, each CAS-stamped with
    task_version and keyed for idempotency, returned in FIRE-TIME order
    (reminder -> sla -> escalation -> auto_park; the reminder fires before the SLA deadline,
    so chronologically reminder < sla, while §5.5 lists the conceptual order
    SLA -> reminder -> escalation -> auto-park). The caller is open_task (Phase 07), whose
    contract signature is open_task(conn, spec, actor) -> str: it returns a task_id and
    itself schedules each returned rung via schedule_timer inside the §5.1 atomic step (it
    does NOT return timers in a HandlerResult). cancel_timers_for_task voids the unfired
    rungs on answer."""
    sla_at = resolve_business_deadline(conn, business_calendar, opened_at, sla)
    reminder_at = resolve_business_deadline(conn, business_calendar, opened_at, reminder)
    escalation_at = resolve_business_deadline(conn, business_calendar, sla_at, escalation)
    park_at = resolve_business_deadline(conn, business_calendar, escalation_at, escalation)
    rungs = (("reminder", reminder_at), ("sla", sla_at),
             ("escalation", escalation_at), ("auto_park", park_at))
    return tuple(
        NewTimer(
            kind=kind,
            fire_at=fire_at,
            idempotency_key=f"ladder:{task_id}:v{task_version}:{kind}",
            task_id=task_id,
            business_calendar=business_calendar,
            cas_task_version=task_version,
            payload={"gate_task_id": task_id, "rung": kind},
        )
        for kind, fire_at in rungs
    )
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_timers_schedule.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: timers table + schedule_timer + escalation ladder (§5.5)"`

---

## Task 3 — Timer poller (due + overdue claim, lease, SKIP LOCKED)

The poller fires due timers and **overdue** timers on recovery, and reclaims expired leases — without two pollers double-claiming (§5.5).

**Files:**
- Modify: `src/featuregen/runtime/timers.py`
- Test: `tests/featuregen/runtime/test_timers_poller.py`

**Interfaces:**
- Consumes: `DbConn`; `schedule_timer`, `timers` table (Task 2).
- Produces: `poll_due_timers(conn: DbConn, *, owner: str, lease_seconds: int, batch: int, now: datetime) -> list[str]`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_timers_poller.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from featuregen.contracts import NewTimer
from featuregen.runtime.timers import poll_due_timers, schedule_timer

UTC = timezone.utc
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _schedule(conn, key, fire_at, kind="sla"):
    return schedule_timer(conn, "run", "run_1",
                          NewTimer(kind=kind, fire_at=fire_at, idempotency_key=key))


def _status(conn, tid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM timers WHERE timer_id=%s", (tid,))
        return cur.fetchone()[0]


def test_due_timer_claimed(conn):
    tid = _schedule(conn, "due", NOW - timedelta(minutes=1))
    claimed = poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed
    assert _status(conn, tid) == "leased"


def test_future_timer_not_claimed(conn):
    tid = _schedule(conn, "future", NOW + timedelta(hours=1))
    assert poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW) == []
    assert _status(conn, tid) == "scheduled"


def test_overdue_timer_claimed_on_recovery(conn):
    tid = _schedule(conn, "overdue", NOW - timedelta(days=30))
    claimed = poll_due_timers(conn, owner="poller-a", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed


def test_expired_lease_reclaimed(conn):
    tid = _schedule(conn, "stale-lease", NOW - timedelta(minutes=5))
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE timers SET status='leased', lease_owner='dead', lease_expires_at=%s "
            "WHERE timer_id=%s",
            (NOW - timedelta(minutes=1), tid),
        )
    claimed = poll_due_timers(conn, owner="poller-b", lease_seconds=60, batch=10, now=NOW)
    assert tid in claimed
    with conn.cursor() as cur:
        cur.execute("SELECT lease_owner FROM timers WHERE timer_id=%s", (tid,))
        assert cur.fetchone()[0] == "poller-b"
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_timers_poller.py -q` → `ImportError: cannot import name 'poll_due_timers'`.

- [ ] **(3) Write minimal implementation.** Append to `src/featuregen/runtime/timers.py`:

```python
from datetime import timedelta  # add to existing imports at top of timers.py


def poll_due_timers(
    conn: DbConn, *, owner: str, lease_seconds: int, batch: int, now: datetime
) -> list[str]:
    """Claim due AND overdue scheduled timers (fire_at <= now) plus timers whose lease has
    expired, via FOR UPDATE SKIP LOCKED so concurrent pollers never double-claim (§5.5).
    Overdue timers are picked up here regardless of how far past, giving crash-recovery
    catch-up. Returns the claimed timer_ids (status -> 'leased')."""
    lease_until = now + timedelta(seconds=lease_seconds)
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH due AS (
                SELECT timer_id FROM timers
                 WHERE (status = 'scheduled' AND fire_at <= %s)
                    OR (status = 'leased' AND lease_expires_at < %s)
                 ORDER BY fire_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s
            )
            UPDATE timers t
               SET status = 'leased', lease_owner = %s, lease_expires_at = %s
              FROM due
             WHERE t.timer_id = due.timer_id
            RETURNING t.timer_id
            """,
            (now, now, batch, owner, lease_until),
        )
        return [r[0] for r in cur.fetchall()]
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_timers_poller.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: timer poller — due/overdue claim + lease reclaim (§5.5)"`

---

## Task 4 — `fire_timer` (CAS on gate task version, idempotent enqueue) + `cancel_timers_for_task`

Firing applies a timer's effect exactly once by enqueueing one idempotent work message; the timer/answer race is resolved by CAS on the gate task's version plus atomic cancel-on-answer, so a late timer can never escalate an answered/changed gate (§5.5).

**Files:**
- Modify: `src/featuregen/runtime/timers.py`
- Test: `tests/featuregen/runtime/test_timers_fire.py`

**Interfaces:**
- Consumes: `DbConn`; `timers` (Task 2), `queue` (Phase 04).
- Produces:
  - `TaskVersionResolver = Callable[[DbConn, str], Optional[int]]`
  - `TimerFireOutcome(timer_id: str, fired: bool, suppressed_reason: Optional[str])`
  - `fire_timer(conn: DbConn, timer_id: str, *, now: datetime, resolve_task_version: TaskVersionResolver = <default>) -> TimerFireOutcome`
  - `cancel_timers_for_task(conn: DbConn, task_id: str) -> int`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_timers_fire.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from featuregen.contracts import NewTimer
from featuregen.runtime.timers import (
    cancel_timers_for_task,
    fire_timer,
    poll_due_timers,
    schedule_timer,
)

UTC = timezone.utc
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _sched(conn, key, *, kind="escalation", task_id=None, cas=None):
    return schedule_timer(conn, "run", "run_1",
                          NewTimer(kind=kind, fire_at=NOW - timedelta(minutes=1),
                                   idempotency_key=key, task_id=task_id, cas_task_version=cas))


def _queue_count(conn, message_id):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id=%s", (message_id,))
        return cur.fetchone()[0]


def _status(conn, tid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM timers WHERE timer_id=%s", (tid,))
        return cur.fetchone()[0]


def test_fire_enqueues_once_and_is_idempotent(conn):
    tid = _sched(conn, "k-fire")
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out1 = fire_timer(conn, tid, now=NOW)
    assert out1.fired is True
    assert _status(conn, tid) == "fired"
    assert _queue_count(conn, "k-fire") == 1
    # re-fire (e.g. overdue duplicate) -> one effect, no second queue row
    out2 = fire_timer(conn, tid, now=NOW)
    assert out2.fired is False and out2.suppressed_reason == "already_fired"
    assert _queue_count(conn, "k-fire") == 1


def test_cas_match_fires(conn):
    tid = _sched(conn, "k-match", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: 1)
    assert out.fired is True
    assert _queue_count(conn, "k-match") == 1


def test_cas_mismatch_suppressed(conn):
    tid = _sched(conn, "k-mismatch", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: 2)
    assert out.fired is False and out.suppressed_reason == "cas_mismatch"
    assert _status(conn, tid) == "cancelled"
    assert _queue_count(conn, "k-mismatch") == 0


def test_answered_task_suppressed(conn):
    tid = _sched(conn, "k-answered", task_id="task_1", cas=1)
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    out = fire_timer(conn, tid, now=NOW, resolve_task_version=lambda c, t: None)
    assert out.fired is False and out.suppressed_reason == "task_closed"
    assert _queue_count(conn, "k-answered") == 0


def test_cancel_on_answer_voids_unfired_rungs(conn):
    a = _sched(conn, "lad-a", kind="reminder", task_id="task_7", cas=1)
    b = _sched(conn, "lad-b", kind="escalation", task_id="task_7", cas=1)
    n = cancel_timers_for_task(conn, "task_7")
    assert n == 2
    assert _status(conn, a) == "cancelled" and _status(conn, b) == "cancelled"
    # a late fire on a cancelled timer is refused
    out = fire_timer(conn, b, now=NOW, resolve_task_version=lambda c, t: 1)
    assert out.fired is False and out.suppressed_reason == "task_closed"


def test_auto_park_rung_uses_canonical_handler(conn):
    # The ladder's auto_park rung must enqueue the SAME handler the cost breaker uses
    # ('runtime.auto_park', Task 10), NOT 'timer.auto_park' (§5.6 mirrors the §5.5 ladder),
    # so downstream registers ONE park handler for both ladder + cost-ceiling parking.
    tid = _sched(conn, "k-park", kind="auto_park")
    poll_due_timers(conn, owner="p", lease_seconds=60, batch=10, now=NOW)
    assert fire_timer(conn, tid, now=NOW).fired is True
    with conn.cursor() as cur:
        cur.execute("SELECT handler FROM queue WHERE message_id='k-park'")
        assert cur.fetchone()[0] == "runtime.auto_park"
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_timers_fire.py -q` → `ImportError: cannot import name 'fire_timer'`.

- [ ] **(3) Write minimal implementation.** Append to `src/featuregen/runtime/timers.py`:

```python
from psycopg.types.json import Jsonb  # already imported at top; keep one import only

TaskVersionResolver = Callable[[DbConn, str], Optional[int]]


def _default_task_version(conn: DbConn, task_id: str) -> Optional[int]:
    """Read the current task_version of an OPEN gate task. Returns None if the task is
    gone/answered/cancelled, which suppresses a late timer. NOTE: this is the LIBRARY
    DEFAULT only and queries Phase 07's `human_tasks` table — a *runtime* dependency on
    Phase 07. Callers running before Phase 07 exists (and every Task-4 test) inject
    `resolve_task_version` to avoid it; there is no compile-time/import dependency."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT task_version FROM human_tasks WHERE task_id = %s AND status = 'open'",
            (task_id,),
        )
        row = cur.fetchone()
    return None if row is None else row[0]


@dataclass(frozen=True, slots=True)
class TimerFireOutcome:
    timer_id: str
    fired: bool
    suppressed_reason: Optional[str] = None  # already_fired|cas_mismatch|task_closed|not_found


def fire_timer(
    conn: DbConn,
    timer_id: str,
    *,
    now: datetime,
    resolve_task_version: TaskVersionResolver = _default_task_version,
) -> TimerFireOutcome:
    """Apply one timer's effect IDEMPOTENTLY (§5.5). CAS on the gate task version: if the
    timer guards a task whose required_inputs changed (task_version bumped) or that is no
    longer open, the timer is voided ('cancelled') and produces NO effect — a late timer
    cannot escalate an answered/changed gate. Otherwise it enqueues exactly one work message
    keyed by idempotency_key (overdue re-fire => one effect) and marks the timer 'fired'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT idempotency_key, aggregate, aggregate_id, task_id, kind, cas_task_version, status "
            "FROM timers WHERE timer_id = %s FOR UPDATE",
            (timer_id,),
        )
        row = cur.fetchone()
        if row is None:
            return TimerFireOutcome(timer_id, False, "not_found")
        idem, aggregate, aggregate_id, task_id, kind, cas_version, status = row
        if status == "fired":
            return TimerFireOutcome(timer_id, False, "already_fired")
        if status == "cancelled":
            return TimerFireOutcome(timer_id, False, "task_closed")
        if task_id is not None and cas_version is not None:
            current = resolve_task_version(conn, task_id)
            if current is None:
                cur.execute("UPDATE timers SET status='cancelled' WHERE timer_id=%s", (timer_id,))
                return TimerFireOutcome(timer_id, False, "task_closed")
            if current != cas_version:
                cur.execute("UPDATE timers SET status='cancelled' WHERE timer_id=%s", (timer_id,))
                return TimerFireOutcome(timer_id, False, "cas_mismatch")
        # The auto_park rung shares ONE canonical park handler with the cost breaker
        # (trip_cost_breaker, Task 10), which enqueues 'runtime.auto_park' — §5.6 says the
        # breaker "mirrors the §5.5 ladder", so both park effects MUST land on the same
        # handler. Downstream therefore registers a single 'runtime.auto_park' handler; all
        # other rungs route to their per-kind 'timer.<kind>' handler.
        handler = "runtime.auto_park" if kind == "auto_park" else f"timer.{kind}"
        cur.execute(
            """
            INSERT INTO queue (message_id, partition_key, handler, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (message_id) DO NOTHING
            """,
            (idem, f"{aggregate}:{aggregate_id}", handler,
             Jsonb({"timer_id": timer_id, "kind": kind, "task_id": task_id})),
        )
        cur.execute("UPDATE timers SET status='fired' WHERE timer_id=%s", (timer_id,))
    return TimerFireOutcome(timer_id, True)


def cancel_timers_for_task(conn: DbConn, task_id: str) -> int:
    """Atomically void all unfired timers guarding a gate task (cancel-on-answer, §5.5).
    Called in the SAME transaction as the answer (submit_human_signal, Phase 07) so a
    leased-but-not-yet-fired escalation cannot fire after the gate is answered. Returns the
    number of timers cancelled."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE timers SET status='cancelled' "
            "WHERE task_id=%s AND status IN ('scheduled','leased')",
            (task_id,),
        )
        return cur.rowcount
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_timers_fire.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: fire_timer CAS race + idempotent enqueue + cancel-on-answer (§5.5)"`

---

## Task 5 — `external_commands` table + `record_external_command` (idempotent, result caching, high-cost dedup guard)

Record side-effecting calls in the §5.1 transaction with an idempotency key; duplicates return the original command (result caching), and high-cost integrations must carry a dedup guarantee or job handle — no false exactly-once claim (§5.4).

**Files:**
- Create: `src/featuregen/db/migrations/0503_external_commands.sql`
- Create: `src/featuregen/runtime/external_commands.py`
- Test: `tests/featuregen/runtime/test_external_commands_record.py`

**Interfaces:**
- Consumes: `DbConn`, `NewExternalCommand` (contract).
- Produces:
  - `HighCostWithoutDedup(Exception)`
  - `record_external_command(conn: DbConn, cmd: NewExternalCommand, *, command_id: str, run_id: Optional[str] = None, require_dedup: frozenset[str] = frozenset({"sandbox"})) -> str`
  - Table `external_commands` (shared DDL).

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_external_commands_record.py`:

```python
from __future__ import annotations

import pytest

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import HighCostWithoutDedup, record_external_command


def _count(conn, key):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM external_commands WHERE idempotency_key=%s", (key,))
        return cur.fetchone()[0]


def test_record_inserts_pending(conn):
    cmd = NewExternalCommand(integration="llm", idempotency_key="idem-1",
                             request_payload={"prompt": "x"})
    cid = record_external_command(conn, cmd, command_id="cmd_1", run_id="run_1")
    assert cid == "cmd_1"
    with conn.cursor() as cur:
        cur.execute("SELECT status, run_id FROM external_commands WHERE command_id='cmd_1'")
        assert cur.fetchone() == ("pending", "run_1")


def test_record_is_idempotent_caching(conn):
    cmd = NewExternalCommand(integration="llm", idempotency_key="dup",
                             request_payload={"prompt": "x"})
    a = record_external_command(conn, cmd, command_id="cmd_a")
    b = record_external_command(conn, cmd, command_id="cmd_b")  # same idempotency_key
    assert a == b == "cmd_a"
    assert _count(conn, "dup") == 1


def test_high_cost_requires_dedup_or_handle(conn):
    cmd = NewExternalCommand(integration="sandbox", idempotency_key="s1",
                             request_payload={}, dedup_supported=False, job_handle=None)
    with pytest.raises(HighCostWithoutDedup):
        record_external_command(conn, cmd, command_id="cmd_s1")


def test_high_cost_with_job_handle_ok(conn):
    cmd = NewExternalCommand(integration="sandbox", idempotency_key="s2",
                             request_payload={}, job_handle="job-42")
    assert record_external_command(conn, cmd, command_id="cmd_s2") == "cmd_s2"
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_external_commands_record.py -q` → `ModuleNotFoundError: No module named 'featuregen.runtime.external_commands'`.

- [ ] **(3) Write minimal implementation.**

`src/featuregen/db/migrations/0503_external_commands.sql` (verbatim shared DDL):

```sql
CREATE TABLE external_commands (
    command_id              text        PRIMARY KEY,
    idempotency_key         text        NOT NULL UNIQUE,
    run_id                  text        NULL,
    integration             text        NOT NULL,
    request_payload         jsonb       NOT NULL,
    expected_run_id         text        NULL,
    expected_stream_version integer     NULL,
    expected_task_id        text        NULL,
    job_handle              text        NULL,
    dedup_supported         boolean     NOT NULL DEFAULT false,
    status                  text        NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending','dispatched','succeeded','failed','stale_ignored')),
    result                  jsonb       NULL,
    result_event_id         text        NULL REFERENCES events(event_id),
    cost_units              numeric(18,4) NULL,
    attempts                integer     NOT NULL DEFAULT 0,
    dispatched_at           timestamptz NULL,
    completed_at            timestamptz NULL,
    created_at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX external_commands_status_idx ON external_commands (status, created_at);
CREATE INDEX external_commands_run_idx    ON external_commands (run_id) WHERE run_id IS NOT NULL;
```

`src/featuregen/runtime/external_commands.py`:

```python
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Optional, Protocol, runtime_checkable

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn, NewExternalCommand

_log = logging.getLogger("featuregen.external_commands")


class HighCostWithoutDedup(Exception):
    """A high-cost integration was recorded without a dedup guarantee or job handle (§5.4)."""


def record_external_command(
    conn: DbConn,
    cmd: NewExternalCommand,
    *,
    command_id: str,
    run_id: Optional[str] = None,
    require_dedup: frozenset[str] = frozenset({"sandbox"}),
) -> str:
    """Record a side-effecting command in the caller's §5.1 transaction (status='pending').
    Idempotent on idempotency_key (result caching: a duplicate returns the ORIGINAL
    command_id). High-cost integrations in `require_dedup` MUST carry dedup_supported or a
    job_handle, else HighCostWithoutDedup — no false exactly-once claim (§5.4)."""
    if cmd.integration in require_dedup and not cmd.dedup_supported and cmd.job_handle is None:
        raise HighCostWithoutDedup(
            f"{cmd.integration} requires dedup_supported or job_handle (§5.4)"
        )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO external_commands
                (command_id, idempotency_key, run_id, integration, request_payload,
                 expected_run_id, expected_stream_version, expected_task_id,
                 job_handle, dedup_supported)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING command_id
            """,
            (command_id, cmd.idempotency_key, run_id, cmd.integration,
             Jsonb(dict(cmd.request_payload)), cmd.expected_run_id,
             cmd.expected_stream_version, cmd.expected_task_id, cmd.job_handle,
             cmd.dedup_supported),
        )
        row = cur.fetchone()
        if row is not None:
            return row[0]
        cur.execute(
            "SELECT command_id FROM external_commands WHERE idempotency_key = %s",
            (cmd.idempotency_key,),
        )
        return cur.fetchone()[0]
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_external_commands_record.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: external_commands table + idempotent record + dedup guard (§5.4)"`

---

## Task 6 — External-command dispatcher (invoke/reconcile, retry classification, residual-duplicate caveat)

The dispatcher executes pending commands and, on crash-recovery of an already-dispatched command, reconciles via job handle or honestly flags the residual-duplicate risk when the integration cannot dedup — never a false dedup claim (§5.4).

**Files:**
- Modify: `src/featuregen/runtime/external_commands.py`
- Modify: `tests/featuregen/runtime/conftest.py` (add `recording_caller` fixture)
- Test: `tests/featuregen/runtime/test_external_commands_dispatch.py`

**Interfaces:**
- Consumes: `DbConn`; `external_commands` (Task 5).
- Produces:
  - `IntegrationResult(ok: bool, result: Mapping[str, Any], cost_units: Optional[Decimal] = None, job_handle: Optional[str] = None, permanent: bool = False)`
  - `IntegrationCaller(Protocol)` with `integration: str`, `invoke(request_payload) -> IntegrationResult`, `reconcile(job_handle) -> Optional[IntegrationResult]`
  - `DispatchOutcome(command_id, status, reinvoked: bool, residual_duplicate_risk: bool, reconciled: bool)`
  - `dispatch_command(conn: DbConn, command_id: str, caller: IntegrationCaller, *, now: datetime) -> DispatchOutcome`

**TDD steps:**

- [ ] **(1) Write the failing test.** First add the fixture to `tests/featuregen/runtime/conftest.py`. NOTE: the fake stores whatever `IntegrationResult` the test constructs and passes in — it never builds one itself — so conftest deliberately does NOT import `IntegrationResult`. Importing it here would make conftest fail to collect at step (2) and break the entire `tests/featuregen/runtime/` directory (including Task 5's already-green tests); the failing import must live in the test module under construction, not in conftest.

```python
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
```

Then `tests/featuregen/runtime/test_external_commands_dispatch.py`:

```python
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import (
    IntegrationResult,
    dispatch_command,
    record_external_command,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def _record(conn, key, *, integration="llm", dedup=False, handle=None, status="pending"):
    cmd = NewExternalCommand(integration=integration, idempotency_key=key,
                             request_payload={"p": 1}, dedup_supported=dedup, job_handle=handle)
    cid = record_external_command(conn, cmd, command_id=f"cmd_{key}", run_id="run_1",
                                  require_dedup=frozenset())
    if status != "pending":
        with conn.cursor() as cur:
            cur.execute("UPDATE external_commands SET status=%s WHERE command_id=%s", (status, cid))
    return cid


def _row(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT status, result, cost_units FROM external_commands WHERE command_id=%s", (cid,))
        return cur.fetchone()


def test_pending_success(conn, recording_caller):
    cid = _record(conn, "ok")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 7}, Decimal("1.50")))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    status, result, cost = _row(conn, cid)
    assert status == "succeeded" and result["answer"] == 7 and cost == Decimal("1.50")
    assert caller.invoke_calls == 1


def test_retryable_stays_pending(conn, recording_caller):
    cid = _record(conn, "retry")
    caller = recording_caller(invoke_result=IntegrationResult(False, {"err": "503"}, permanent=False))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "pending"
    assert _row(conn, cid)[0] == "pending"


def test_permanent_fails(conn, recording_caller):
    cid = _record(conn, "perm")
    caller = recording_caller(invoke_result=IntegrationResult(False, {"err": "bad input"}, permanent=True))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "failed"
    assert _row(conn, cid)[0] == "failed"


def test_recovery_reconciles_via_handle(conn, recording_caller):
    cid = _record(conn, "rec", handle="job-9", status="dispatched")
    caller = recording_caller(reconcile_result=IntegrationResult(True, {"answer": 1}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded" and out.reconciled is True
    assert caller.invoke_calls == 0 and caller.reconcile_calls == 1


def test_recovery_no_handle_no_dedup_flags_residual(conn, recording_caller):
    cid = _record(conn, "resid", dedup=False, handle=None, status="dispatched")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 2}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    assert out.reinvoked is True and out.residual_duplicate_risk is True
    assert _row(conn, cid)[1]["_residual_duplicate_risk"] is True
    assert caller.invoke_calls == 1


def test_recovery_dedup_supported_no_residual(conn, recording_caller):
    cid = _record(conn, "safe", dedup=True, handle=None, status="dispatched")
    caller = recording_caller(invoke_result=IntegrationResult(True, {"answer": 3}))
    out = dispatch_command(conn, cid, caller, now=NOW)
    assert out.status == "succeeded"
    assert out.reinvoked is True and out.residual_duplicate_risk is False
    assert "_residual_duplicate_risk" not in _row(conn, cid)[1]
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_external_commands_dispatch.py -q` → `ImportError: cannot import name 'IntegrationResult' from 'featuregen.runtime.external_commands'`. (The dispatch test imports `IntegrationResult`, `dispatch_command`, and `record_external_command`; the first two are added in step (3), so collecting THIS module fails on the first missing name, `IntegrationResult`. Because conftest no longer imports anything undefined, Task 5's already-green tests are unaffected.)

- [ ] **(3) Write minimal implementation.** Append to `src/featuregen/runtime/external_commands.py`:

```python
@dataclass(frozen=True, slots=True)
class IntegrationResult:
    ok: bool
    result: Mapping[str, Any]
    cost_units: Optional[Decimal] = None
    job_handle: Optional[str] = None
    permanent: bool = False        # deterministic failure => skip delivery retry (§5.6)


@runtime_checkable
class IntegrationCaller(Protocol):
    integration: str
    def invoke(self, request_payload: Mapping[str, Any]) -> IntegrationResult: ...
    def reconcile(self, job_handle: str) -> Optional[IntegrationResult]: ...


@dataclass(frozen=True, slots=True)
class DispatchOutcome:
    command_id: str
    status: str                    # succeeded|failed|pending|dispatched
    reinvoked: bool = False
    residual_duplicate_risk: bool = False
    reconciled: bool = False


def _flag_residual(result: Mapping[str, Any], residual: bool) -> dict:
    out = dict(result)
    if residual:
        out["_residual_duplicate_risk"] = True
    return out


def dispatch_command(
    conn: DbConn, command_id: str, caller: IntegrationCaller, *, now: datetime
) -> DispatchOutcome:
    """Execute ONE pending/dispatched external command (§5.4). On recovery of a command
    already 'dispatched': if a job_handle exists, reconcile (no re-invoke); else if the
    integration does NOT honor the idempotency key (dedup_supported=False), re-invoke and
    FLAG the residual-duplicate risk honestly (logged + persisted in result) — never a false
    dedup claim. Retryable failures stay 'pending'; permanent failures go to 'failed'."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_payload, job_handle, dedup_supported, status "
            "FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(command_id)
        payload, job_handle, dedup_supported, status = row
        if status in ("succeeded", "stale_ignored", "failed"):
            return DispatchOutcome(command_id, status)

        reconciled = residual = reinvoked = False
        if status == "dispatched":
            if job_handle is not None:
                res = caller.reconcile(job_handle)
                reconciled = True
                if res is None:
                    return DispatchOutcome(command_id, "dispatched", reconciled=True)
            else:
                if not dedup_supported:
                    residual = True
                    _log.warning(
                        "residual-duplicate risk: re-invoking %s (idempotency not honored)",
                        command_id,
                    )
                res = caller.invoke(payload)
                reinvoked = True
        else:  # pending
            cur.execute(
                "UPDATE external_commands SET status='dispatched', dispatched_at=%s, "
                "attempts=attempts+1 WHERE command_id=%s",
                (now, command_id),
            )
            res = caller.invoke(payload)

        if res.ok:
            cur.execute(
                "UPDATE external_commands SET status='succeeded', result=%s, cost_units=%s, "
                "completed_at=%s, job_handle=COALESCE(%s, job_handle) WHERE command_id=%s",
                (Jsonb(_flag_residual(res.result, residual)), res.cost_units, now,
                 res.job_handle, command_id),
            )
            return DispatchOutcome(command_id, "succeeded", reinvoked, residual, reconciled)
        if res.permanent:
            cur.execute(
                "UPDATE external_commands SET status='failed', result=%s, completed_at=%s "
                "WHERE command_id=%s",
                (Jsonb(dict(res.result)), now, command_id),
            )
            return DispatchOutcome(command_id, "failed", reinvoked, residual, reconciled)
        cur.execute(
            "UPDATE external_commands SET status='pending' WHERE command_id=%s", (command_id,)
        )
        return DispatchOutcome(command_id, "pending", reinvoked, residual, reconciled)
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_external_commands_dispatch.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: external-command dispatcher — reconcile/residual caveat + retry classification (§5.4)"`

---

## Task 7 — Stale-result acceptance guard + result caching

A result is applied only if the run/task it was issued against has not moved on; otherwise it is accepted-and-ignored as stale, never blindly applied (§5.4).

**Files:**
- Modify: `src/featuregen/runtime/external_commands.py`
- Test: `tests/featuregen/runtime/test_external_commands_stale.py`

**Interfaces:**
- Consumes: `DbConn`; `external_commands` (Task 5), `events` (Phase 01, for the cached-applied path).
- Produces:
  - `ResultAcceptance(command_id, accepted: bool, stale: bool, cached: bool)`
  - `accept_result(conn: DbConn, command_id: str, *, current_run_id: Optional[str], current_stream_version: Optional[int], current_task_id: Optional[str]) -> ResultAcceptance`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_external_commands_stale.py`:

```python
from __future__ import annotations

from featuregen.contracts import NewExternalCommand
from featuregen.runtime.external_commands import accept_result, record_external_command


def _record(conn, key, **expected):
    cmd = NewExternalCommand(integration="llm", idempotency_key=key, request_payload={},
                             expected_run_id=expected.get("run"),
                             expected_stream_version=expected.get("sv"),
                             expected_task_id=expected.get("task"))
    return record_external_command(conn, cmd, command_id=f"cmd_{key}", require_dedup=frozenset())


def _status(conn, cid):
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM external_commands WHERE command_id=%s", (cid,))
        return cur.fetchone()[0]


def test_accepted_when_on_target(conn):
    cid = _record(conn, "a", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=5,
                        current_task_id=None)
    assert out.accepted is True and out.stale is False


def test_stale_when_run_changed(conn):
    cid = _record(conn, "b", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_2", current_stream_version=5,
                        current_task_id=None)
    assert out.stale is True and out.accepted is False
    assert _status(conn, cid) == "stale_ignored"


def test_stale_when_advanced_past_version(conn):
    cid = _record(conn, "c", run="run_1", sv=5)
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=6,
                        current_task_id=None)
    assert out.stale is True


def test_stale_when_task_changed(conn):
    cid = _record(conn, "d", task="task_1")
    out = accept_result(conn, cid, current_run_id=None, current_stream_version=None,
                        current_task_id="task_2")
    assert out.stale is True


def test_stale_idempotent_cache(conn):
    cid = _record(conn, "e", run="run_1")
    accept_result(conn, cid, current_run_id="run_2", current_stream_version=None,
                  current_task_id=None)
    out = accept_result(conn, cid, current_run_id="run_2", current_stream_version=None,
                        current_task_id=None)
    assert out.stale is True and out.cached is True


def test_applied_result_is_cached(conn, insert_stub_event):
    cid = _record(conn, "f", run="run_1", sv=5)
    insert_stub_event(conn, event_id="evt_res", run_id="run_1", type="LLM_RESULT", stream_version=1)
    with conn.cursor() as cur:
        cur.execute("UPDATE external_commands SET result_event_id='evt_res' WHERE command_id=%s", (cid,))
    out = accept_result(conn, cid, current_run_id="run_1", current_stream_version=5,
                        current_task_id=None)
    assert out.accepted is True and out.cached is True
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_external_commands_stale.py -q` → `ImportError: cannot import name 'accept_result'`.

- [ ] **(3) Write minimal implementation.** Append to `src/featuregen/runtime/external_commands.py`:

```python
@dataclass(frozen=True, slots=True)
class ResultAcceptance:
    command_id: str
    accepted: bool
    stale: bool
    cached: bool = False


def accept_result(
    conn: DbConn,
    command_id: str,
    *,
    current_run_id: Optional[str],
    current_stream_version: Optional[int],
    current_task_id: Optional[str],
) -> ResultAcceptance:
    """Stale-result acceptance guard (§5.4). The result is APPLIED only if the run/task it
    was issued against has not moved on: expected_run_id == current_run_id AND (no
    expected_stream_version OR current has not advanced past it) AND (no expected_task_id OR
    == current). Otherwise it is accepted-and-IGNORED as stale (status='stale_ignored') —
    never blindly applied to a moved-on run. Idempotent: a command already routed to
    stale/applied returns cached."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_run_id, expected_stream_version, expected_task_id, status, "
            "result_event_id FROM external_commands WHERE command_id = %s FOR UPDATE",
            (command_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(command_id)
        exp_run, exp_sv, exp_task, status, result_event_id = row
        if status == "stale_ignored":
            return ResultAcceptance(command_id, accepted=False, stale=True, cached=True)
        if result_event_id is not None:
            return ResultAcceptance(command_id, accepted=True, stale=False, cached=True)
        stale = False
        if exp_run is not None and exp_run != current_run_id:
            stale = True
        elif (exp_sv is not None and current_stream_version is not None
              and current_stream_version > exp_sv):
            stale = True
        elif exp_task is not None and exp_task != current_task_id:
            stale = True
        if stale:
            cur.execute(
                "UPDATE external_commands SET status='stale_ignored', completed_at=now() "
                "WHERE command_id=%s",
                (command_id,),
            )
            return ResultAcceptance(command_id, accepted=False, stale=True)
    return ResultAcceptance(command_id, accepted=True, stale=False)
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_external_commands_stale.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: stale-result acceptance guard + result caching (§5.4)"`

---

## Task 8 — Delivery-retry semantics: backoff+jitter, `max_elapsed_time`, DLQ

Transient delivery failures back off with jitter to a per-message budget and `max_elapsed_time`; permanent (deterministic) failures skip retry and go straight to the DLQ (§5.6). Operates generically over the Phase 04 `queue` and `outbox` rows.

**Files:**
- Create: `src/featuregen/runtime/retries.py`
- Test: `tests/featuregen/runtime/test_retries.py`

**Interfaces:**
- Consumes: `DbConn`, `Disposition` (contract); `queue`, `outbox` (Phase 04).
- Produces:
  - `compute_backoff(attempts: int, *, base_seconds: float, cap_seconds: float, rng: random.Random) -> float`
  - `within_budget(*, attempts: int, max_attempts: int, started_at: datetime, now: datetime, max_elapsed_seconds: float) -> bool`
  - `QUEUE_SPEC`, `OUTBOX_SPEC` (`_TableSpec` instances)
  - `record_delivery_outcome(conn: DbConn, spec: _TableSpec, row_id: int, *, disposition: Disposition, error: Optional[str], started_at: datetime, now: datetime, base_seconds: float, cap_seconds: float, max_elapsed_seconds: float, rng: random.Random) -> str`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_retries.py`:

```python
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest

from featuregen.contracts import Disposition
from featuregen.runtime.retries import (
    OUTBOX_SPEC,
    QUEUE_SPEC,
    compute_backoff,
    record_delivery_outcome,
    within_budget,
)

NOW = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)


def test_compute_backoff_bounded_and_deterministic():
    rng = random.Random(7)
    d = compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, rng=rng)
    assert 0.0 <= d <= 4.0  # window = min(30, 1*2**2) = 4
    assert compute_backoff(3, base_seconds=1.0, cap_seconds=30.0, rng=random.Random(7)) == d


def test_compute_backoff_rejects_zero_attempts():
    with pytest.raises(ValueError):
        compute_backoff(0, base_seconds=1.0, cap_seconds=30.0, rng=random.Random(1))


def test_within_budget_caps():
    assert within_budget(attempts=3, max_attempts=12, started_at=NOW, now=NOW,
                         max_elapsed_seconds=3600) is True
    assert within_budget(attempts=12, max_attempts=12, started_at=NOW, now=NOW,
                         max_elapsed_seconds=3600) is False
    past = NOW - timedelta(hours=2)
    assert within_budget(attempts=1, max_attempts=12, started_at=past, now=NOW,
                         max_elapsed_seconds=3600) is False


def _insert_queue(conn, *, attempts=0, max_attempts=12):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload, attempts, max_attempts) "
            "VALUES ('m1','run:1','h','{}'::jsonb,%s,%s) RETURNING id",
            (attempts, max_attempts),
        )
        return cur.fetchone()[0]


def _insert_outbox(conn, *, attempts=0, max_attempts=12):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO outbox (message_id, partition_key, topic, payload, attempts, max_attempts) "
            "VALUES ('o1','run:1','t','{}'::jsonb,%s,%s) RETURNING id",
            (attempts, max_attempts),
        )
        return cur.fetchone()[0]


def _status(conn, table, row_id):
    with conn.cursor() as cur:
        cur.execute(f"SELECT status FROM {table} WHERE id=%s", (row_id,))
        return cur.fetchone()[0]


def test_ok_disposition_is_rejected(conn):
    rid = _insert_queue(conn)
    with pytest.raises(ValueError):
        record_delivery_outcome(
            conn, QUEUE_SPEC, rid, disposition=Disposition.OK, error=None,
            started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
            rng=random.Random(1),
        )
    assert _status(conn, "queue", rid) == "ready"  # untouched; OK is not a failure


def test_permanent_goes_to_dlq(conn):
    rid = _insert_queue(conn)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.PERMANENT, error="bad",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "dead" and _status(conn, "queue", rid) == "dead"


def test_retryable_within_budget_reschedules(conn):
    rid = _insert_queue(conn, attempts=0)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.RETRYABLE, error="503",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "ready"
    with conn.cursor() as cur:
        cur.execute("SELECT attempts, available_at FROM queue WHERE id=%s", (rid,))
        attempts, available_at = cur.fetchone()
    assert attempts == 1 and available_at >= NOW


def test_retryable_exhausted_goes_to_dlq(conn):
    rid = _insert_queue(conn, attempts=11, max_attempts=12)
    status = record_delivery_outcome(
        conn, QUEUE_SPEC, rid, disposition=Disposition.RETRYABLE, error="503",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "dead"


def test_outbox_uses_next_attempt_at(conn):
    rid = _insert_outbox(conn)
    status = record_delivery_outcome(
        conn, OUTBOX_SPEC, rid, disposition=Disposition.RETRYABLE, error="x",
        started_at=NOW, now=NOW, base_seconds=1, cap_seconds=30, max_elapsed_seconds=3600,
        rng=random.Random(1),
    )
    assert status == "pending"
    with conn.cursor() as cur:
        cur.execute("SELECT next_attempt_at FROM outbox WHERE id=%s", (rid,))
        assert cur.fetchone()[0] >= NOW
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_retries.py -q` → `ModuleNotFoundError: No module named 'featuregen.runtime.retries'`.

- [ ] **(3) Write minimal implementation.** `src/featuregen/runtime/retries.py`:

```python
from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from featuregen.contracts import DbConn, Disposition


def compute_backoff(
    attempts: int, *, base_seconds: float, cap_seconds: float, rng: random.Random
) -> float:
    """Exponential backoff with FULL jitter (§5.6). `attempts` = number already made (>=1).
    Returns a delay uniformly drawn from [0, min(cap, base * 2**(attempts-1))]."""
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    window = min(cap_seconds, base_seconds * (2 ** (attempts - 1)))
    return rng.uniform(0.0, window)


def within_budget(
    *,
    attempts: int,
    max_attempts: int,
    started_at: datetime,
    now: datetime,
    max_elapsed_seconds: float,
) -> bool:
    """True if another delivery retry is permitted: under BOTH the attempt budget and the
    max_elapsed_time cap (§5.6)."""
    if attempts >= max_attempts:
        return False
    if (now - started_at).total_seconds() >= max_elapsed_seconds:
        return False
    return True


@dataclass(frozen=True, slots=True)
class _TableSpec:
    table: str
    available_col: str          # 'available_at' (queue) | 'next_attempt_at' (outbox)
    ready_status: str           # 'ready' (queue) | 'pending' (outbox)
    dead_status: str = "dead"


QUEUE_SPEC = _TableSpec("queue", "available_at", "ready")
OUTBOX_SPEC = _TableSpec("outbox", "next_attempt_at", "pending")


def record_delivery_outcome(
    conn: DbConn,
    spec: _TableSpec,
    row_id: int,
    *,
    disposition: Disposition,
    error: Optional[str],
    started_at: datetime,
    now: datetime,
    base_seconds: float,
    cap_seconds: float,
    max_elapsed_seconds: float,
    rng: random.Random,
) -> str:
    """Apply §5.6 delivery-retry semantics to one queue/outbox row. PERMANENT => DLQ
    ('dead') immediately (no retry). RETRYABLE => reschedule with backoff+jitter if still
    within BOTH the per-message attempt budget and max_elapsed_time, else DLQ.
    Disposition.OK is REJECTED with ValueError: OK is not a delivery failure — the caller
    marks the row done/sent and never calls this. Returns the new row status."""
    if disposition is Disposition.OK:
        raise ValueError(
            "record_delivery_outcome handles FAILED deliveries only; Disposition.OK is not "
            "retryable — the caller marks the row done/sent instead (§5.6)."
        )
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT attempts, max_attempts FROM {spec.table} WHERE id = %s FOR UPDATE",
            (row_id,),
        )
        attempts, max_attempts = cur.fetchone()
        attempts += 1
        if disposition is Disposition.PERMANENT or not within_budget(
            attempts=attempts, max_attempts=max_attempts, started_at=started_at, now=now,
            max_elapsed_seconds=max_elapsed_seconds,
        ):
            cur.execute(
                f"UPDATE {spec.table} SET status=%s, attempts=%s, last_error=%s WHERE id=%s",
                (spec.dead_status, attempts, error, row_id),
            )
            return spec.dead_status
        delay = compute_backoff(attempts, base_seconds=base_seconds, cap_seconds=cap_seconds, rng=rng)
        next_at = now + timedelta(seconds=delay)
        cur.execute(
            f"UPDATE {spec.table} SET status=%s, attempts=%s, last_error=%s, "
            f"{spec.available_col}=%s WHERE id=%s",
            (spec.ready_status, attempts, error, next_at, row_id),
        )
        return spec.ready_status
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_retries.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: delivery-retry backoff/jitter + max_elapsed + DLQ (§5.6)"`

---

## Task 9 — Business repair loop (attempt counting, bounded N, `manual_retry` re-arm)

A valid workflow failure routes to a failure state with bounded `N` attempts (each an attempt event); exhaustion routes to a human and `manual_retry` re-arms the loop (§5.6).

**Files:**
- Create: `src/featuregen/runtime/repair_loop.py`
- Test: `tests/featuregen/runtime/test_repair_loop.py`

**Interfaces:**
- Consumes: `DbConn`; `events` (Phase 01), `queue` (Phase 04); `insert_stub_event` fixture (Task 1).
- Produces:
  - `RepairLoopState(attempts_made: int, max_attempts: int, exhausted: bool, rearm_seq: int)`
  - `evaluate_repair_loop(conn: DbConn, run_id: str, *, max_attempts: int, attempt_event_types: Sequence[str], rearm_event_types: Sequence[str] = ("MANUAL_RETRY",)) -> RepairLoopState`
  - `route_repair_exhaustion(conn: DbConn, run_id: str, state: RepairLoopState, *, aggregate: str = "run") -> bool`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_repair_loop.py`:

```python
from __future__ import annotations

from featuregen.runtime.repair_loop import evaluate_repair_loop, route_repair_exhaustion

ATTEMPT = ("REPAIR_ATTEMPTED",)


def test_no_attempts_not_exhausted(conn):
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 0 and st.exhausted is False


def test_exhausts_at_n(conn, insert_stub_event):
    for i in range(3):
        insert_stub_event(conn, event_id=f"evt_{i}", run_id="run_1",
                          type="REPAIR_ATTEMPTED", stream_version=i + 1)
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 3 and st.exhausted is True


def test_manual_retry_rearms(conn, insert_stub_event):
    insert_stub_event(conn, event_id="e1", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=1)
    insert_stub_event(conn, event_id="e2", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=2)
    insert_stub_event(conn, event_id="e3", run_id="run_1", type="MANUAL_RETRY", stream_version=3)
    insert_stub_event(conn, event_id="e4", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=4)
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.attempts_made == 1 and st.exhausted is False  # only the post-rearm attempt counts


def test_exhaustion_routes_to_human_idempotently(conn, insert_stub_event):
    for i in range(3):
        insert_stub_event(conn, event_id=f"evt_{i}", run_id="run_1",
                          type="REPAIR_ATTEMPTED", stream_version=i + 1)
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.exhausted is True
    # exhaustion -> human: enqueue exactly ONE idempotent routing message (§5.6)
    assert route_repair_exhaustion(conn, "run_1", st) is True
    assert route_repair_exhaustion(conn, "run_1", st) is False  # idempotent per episode
    with conn.cursor() as cur:
        cur.execute(
            "SELECT handler, count(*) FROM queue "
            "WHERE message_id=%s GROUP BY handler",
            (f"repair-exhausted:run_1:{st.rearm_seq}",),
        )
        handler, count = cur.fetchone()
    assert handler == "runtime.repair_exhausted" and count == 1


def test_rearm_allows_fresh_exhaustion_routing(conn, insert_stub_event):
    # First episode exhausts and routes...
    for i in range(3):
        insert_stub_event(conn, event_id=f"a{i}", run_id="run_1",
                          type="REPAIR_ATTEMPTED", stream_version=i + 1)
    st1 = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert route_repair_exhaustion(conn, "run_1", st1) is True
    # ...a manual_retry re-arms; a fresh exhaustion routes again under a NEW episode key.
    insert_stub_event(conn, event_id="mr", run_id="run_1", type="MANUAL_RETRY", stream_version=4)
    for i in range(3):
        insert_stub_event(conn, event_id=f"b{i}", run_id="run_1",
                          type="REPAIR_ATTEMPTED", stream_version=5 + i)
    st2 = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st2.exhausted is True and st2.rearm_seq > st1.rearm_seq
    assert route_repair_exhaustion(conn, "run_1", st2) is True  # new episode -> new message
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE handler='runtime.repair_exhausted'")
        assert cur.fetchone()[0] == 2


def test_not_exhausted_does_not_route(conn, insert_stub_event):
    insert_stub_event(conn, event_id="e1", run_id="run_1", type="REPAIR_ATTEMPTED", stream_version=1)
    st = evaluate_repair_loop(conn, "run_1", max_attempts=3, attempt_event_types=ATTEMPT)
    assert st.exhausted is False
    assert route_repair_exhaustion(conn, "run_1", st) is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue")
        assert cur.fetchone()[0] == 0
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_repair_loop.py -q` → `ModuleNotFoundError: No module named 'featuregen.runtime.repair_loop'`.

- [ ] **(3) Write minimal implementation.** `src/featuregen/runtime/repair_loop.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn


@dataclass(frozen=True, slots=True)
class RepairLoopState:
    attempts_made: int
    max_attempts: int
    exhausted: bool
    rearm_seq: int = 0          # global_seq baseline of the current loop EPISODE (last re-arm)


def evaluate_repair_loop(
    conn: DbConn,
    run_id: str,
    *,
    max_attempts: int,
    attempt_event_types: Sequence[str],
    rearm_event_types: Sequence[str] = ("MANUAL_RETRY",),
) -> RepairLoopState:
    """Business repair loop (§5.6): count attempt events on the run stream SINCE the last
    re-arm (`manual_retry`). exhausted => route to a human via route_repair_exhaustion.
    `manual_retry` re-arms by advancing the baseline (rearm_seq) to its global_seq, so
    attempts after it start at zero and a fresh exhaustion can route again."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(MAX(global_seq), 0) FROM events "
            "WHERE run_id = %s AND type = ANY(%s)",
            (run_id, list(rearm_event_types)),
        )
        baseline = cur.fetchone()[0]
        cur.execute(
            "SELECT count(*) FROM events "
            "WHERE run_id = %s AND type = ANY(%s) AND global_seq > %s",
            (run_id, list(attempt_event_types), baseline),
        )
        attempts_made = cur.fetchone()[0]
    return RepairLoopState(attempts_made, max_attempts, attempts_made >= max_attempts, baseline)


def route_repair_exhaustion(
    conn: DbConn, run_id: str, state: RepairLoopState, *, aggregate: str = "run"
) -> bool:
    """On exhaustion route the run to a human (§5.6: 'exhaustion → human'). Enqueues exactly
    ONE idempotent 'runtime.repair_exhausted' work message onto the Phase 04 queue; a
    downstream handler opens the human task / failure gate (this phase never calls open_task
    directly, mirroring the cost breaker's trip_cost_breaker). Idempotent PER EPISODE: the
    message_id embeds state.rearm_seq, so a `manual_retry` re-arm (new baseline) lets a fresh
    exhaustion route again. Returns True if a new message was enqueued; a no-op (returns
    False) when the loop is not exhausted or the episode already routed."""
    if not state.exhausted:
        return False
    message_id = f"repair-exhausted:{run_id}:{state.rearm_seq}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'runtime.repair_exhausted', %s) "
            "ON CONFLICT (message_id) DO NOTHING",
            (message_id, f"{aggregate}:{run_id}",
             Jsonb({"run_id": run_id, "reason": "repair_exhausted",
                    "attempts_made": state.attempts_made,
                    "max_attempts": state.max_attempts})),
        )
        return cur.rowcount == 1
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_repair_loop.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: business repair loop — bounded attempts + exhaustion→human routing + manual_retry re-arm (§5.6)"`

---

## Task 10 — Cost-budget circuit breaker (per-run / per-request counter + auto-park)

A durable per-run cost counter (and per-request aggregate) trips the breaker at a ceiling and auto-parks the run by enqueueing one idempotent parking message — mirroring the §5.5 ladder (§5.6).

**Files:**
- Create: `src/featuregen/runtime/cost_budget.py`
- Test: `tests/featuregen/runtime/test_cost_budget.py`

**Interfaces:**
- Consumes: `DbConn`; `run_workflow_state` (Phase 01), `queue` (Phase 04); `insert_run_state` fixture (Task 1).
- Produces:
  - `CostCeilings(per_run: Optional[Decimal], per_request: Optional[Decimal], max_candidates: Optional[int])`
  - `CostBreakerOutcome(tripped: bool, ceiling: Optional[str], run_cost: Decimal, request_cost: Decimal)`
  - `record_cost(conn: DbConn, run_id: str, delta: Decimal) -> Decimal`
  - `request_cost(conn: DbConn, request_id: str) -> Decimal`
  - `check_cost_breaker(conn: DbConn, run_id: str, *, ceilings: CostCeilings) -> CostBreakerOutcome`
  - `trip_cost_breaker(conn: DbConn, run_id: str, *, ceiling: str, aggregate: str = "run") -> bool`

**TDD steps:**

- [ ] **(1) Write the failing test.** `tests/featuregen/runtime/test_cost_budget.py`:

```python
from __future__ import annotations

from decimal import Decimal

from featuregen.runtime.cost_budget import (
    CostCeilings,
    check_cost_breaker,
    record_cost,
    request_cost,
    trip_cost_breaker,
)


def test_record_cost_accumulates(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1")
    assert record_cost(conn, "run_1", Decimal("2.5")) == Decimal("2.5000")
    assert record_cost(conn, "run_1", Decimal("1.0")) == Decimal("3.5000")


def test_request_cost_sums_runs(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("4"))
    insert_run_state(conn, run_id="run_2", request_id="req_1", cost=Decimal("6"))
    assert request_cost(conn, "req_1") == Decimal("10.0000")


def test_breaker_trips_per_run(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("100"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_run=Decimal("100")))
    assert out.tripped is True and out.ceiling == "per_run"


def test_breaker_trips_per_request(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("60"))
    insert_run_state(conn, run_id="run_2", request_id="req_1", cost=Decimal("60"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_request=Decimal("100")))
    assert out.tripped is True and out.ceiling == "per_request"


def test_breaker_trips_on_candidates(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", candidates=5)
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(max_candidates=5))
    assert out.tripped is True and out.ceiling == "max_candidates"


def test_breaker_not_tripped(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1", cost=Decimal("1"))
    out = check_cost_breaker(conn, "run_1", ceilings=CostCeilings(per_run=Decimal("100")))
    assert out.tripped is False


def test_trip_auto_parks_idempotently(conn, insert_run_state):
    insert_run_state(conn, run_id="run_1", request_id="req_1")
    assert trip_cost_breaker(conn, "run_1", ceiling="per_run") is True
    assert trip_cost_breaker(conn, "run_1", ceiling="per_run") is False
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM queue WHERE message_id='cost-breaker:run_1:per_run'")
        assert cur.fetchone()[0] == 1
        cur.execute("SELECT handler FROM queue WHERE message_id='cost-breaker:run_1:per_run'")
        assert cur.fetchone()[0] == "runtime.auto_park"
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_cost_budget.py -q` → `ModuleNotFoundError: No module named 'featuregen.runtime.cost_budget'`.

- [ ] **(3) Write minimal implementation.** `src/featuregen/runtime/cost_budget.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from psycopg.types.json import Jsonb

from featuregen.contracts import DbConn


@dataclass(frozen=True, slots=True)
class CostCeilings:
    per_run: Optional[Decimal] = None
    per_request: Optional[Decimal] = None
    max_candidates: Optional[int] = None


@dataclass(frozen=True, slots=True)
class CostBreakerOutcome:
    tripped: bool
    ceiling: Optional[str] = None        # per_run|per_request|max_candidates
    run_cost: Decimal = Decimal(0)
    request_cost: Decimal = Decimal(0)


def record_cost(conn: DbConn, run_id: str, delta: Decimal) -> Decimal:
    """Add to the durable per-run cost counter (run_workflow_state.cost_units, §5.6) and
    return the new total. The caller gates double-counting via the external_command's first
    transition to 'succeeded'."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE run_workflow_state SET cost_units = cost_units + %s, updated_at = now() "
            "WHERE run_id = %s RETURNING cost_units",
            (delta, run_id),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        return row[0]


def request_cost(conn: DbConn, request_id: str) -> Decimal:
    """Per-request cost = SUM of per-run counters across the request's runs (§5.6)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_units), 0) FROM run_workflow_state WHERE request_id = %s",
            (request_id,),
        )
        return cur.fetchone()[0]


def check_cost_breaker(
    conn: DbConn, run_id: str, *, ceilings: CostCeilings
) -> CostBreakerOutcome:
    """Pure read of the durable counters; returns which ceiling (if any) is breached (§5.6).
    Per-run is checked first, then per-request, then candidate count."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT request_id, cost_units, candidates_explored "
            "FROM run_workflow_state WHERE run_id = %s",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise KeyError(run_id)
        request_id, run_cost, candidates = row
    req_cost = request_cost(conn, request_id)
    if ceilings.per_run is not None and run_cost >= ceilings.per_run:
        return CostBreakerOutcome(True, "per_run", run_cost, req_cost)
    if ceilings.per_request is not None and req_cost >= ceilings.per_request:
        return CostBreakerOutcome(True, "per_request", run_cost, req_cost)
    if ceilings.max_candidates is not None and candidates >= ceilings.max_candidates:
        return CostBreakerOutcome(True, "max_candidates", run_cost, req_cost)
    return CostBreakerOutcome(False, None, run_cost, req_cost)


def trip_cost_breaker(
    conn: DbConn, run_id: str, *, ceiling: str, aggregate: str = "run"
) -> bool:
    """Auto-park on ceiling (§5.6): enqueue exactly ONE parking work message (idempotent by a
    deterministic message_id) mirroring the §5.5 ladder's auto-park rung. Returns True if a
    new park message was enqueued, False if the run was already parked for this ceiling."""
    message_id = f"cost-breaker:{run_id}:{ceiling}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO queue (message_id, partition_key, handler, payload) "
            "VALUES (%s, %s, 'runtime.auto_park', %s) ON CONFLICT (message_id) DO NOTHING",
            (message_id, f"{aggregate}:{run_id}",
             Jsonb({"run_id": run_id, "reason": "cost_ceiling", "ceiling": ceiling})),
        )
        return cur.rowcount == 1
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_cost_budget.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: cost-budget circuit breaker + auto-park (§5.6)"`

---

## Task 11 — Pre-transaction blob orphan GC over Phase 02's `blob_index` (mark-sweep, quarantine, audit)

A rolled-back step leaves an orphan blob (possibly sensitive). GC marks unreferenced blobs against committed document refs, quarantines sensitive orphans for §9 erasure, sweeps the rest from the object store, and audits every run (§5.1). The `blob_index` table itself is **created by Phase 02** (its `0002_documents.sql`, Task 2 — the overview assigns the `blob_index` schema to Phase 02); this task does NOT create it (a second `CREATE TABLE blob_index` would error with `relation "blob_index" already exists` since the test harness applies every migration in lexical order). This task builds only the GC mechanism — `register_blob` and `mark_and_sweep` — over the existing table.

**Files:**
- Create: `src/featuregen/runtime/blob_gc.py`
- Modify: `tests/featuregen/runtime/conftest.py` (add `recording_deleter`, `recording_audit` fixtures)
- Test: `tests/featuregen/runtime/test_blob_gc.py`

**Interfaces:**
- Consumes: `DbConn`; `documents` + `blob_index` (Phase 02); `insert_stub_document` fixture (Task 1).
- Produces:
  - `BlobDeleter(Protocol)` with `delete(object_key: str) -> None`
  - `GcReport(ran_at: datetime, marked_orphan: tuple[str, ...], quarantined: tuple[str, ...], swept: tuple[str, ...])`
  - `GcAuditSink(Protocol)` with `record(report: GcReport) -> None`
  - `register_blob(conn: DbConn, *, blob_id: str, object_key: str, content_hash: str, classification: str, kms_key_id: Optional[str] = None, size_bytes: Optional[int] = None) -> str`
  - `mark_and_sweep(conn: DbConn, *, now: datetime, grace_seconds: int, deleter: BlobDeleter, auditor: GcAuditSink) -> GcReport`

**TDD steps:**

- [ ] **(1) Write the failing test.** First add fakes to `tests/featuregen/runtime/conftest.py`:

```python
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
```

Then `tests/featuregen/runtime/test_blob_gc.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from featuregen.runtime.blob_gc import mark_and_sweep, register_blob

UTC = timezone.utc
NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)


def _insert_blob(conn, blob_id, *, classification, referenced=False, created_at, object_key="k"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, referenced, created_at) "
            "VALUES (%s,%s,'sha256:h',%s,%s,%s)",
            (blob_id, object_key, classification, referenced, created_at),
        )


def _status(conn, blob_id):
    with conn.cursor() as cur:
        cur.execute("SELECT status, referenced, swept_at FROM blob_index WHERE blob_id=%s", (blob_id,))
        return cur.fetchone()


def test_register_blob_idempotent(conn):
    register_blob(conn, blob_id="blob_1", object_key="k1", content_hash="sha256:x",
                  classification="pii-erasable")
    register_blob(conn, blob_id="blob_1", object_key="k1", content_hash="sha256:x",
                  classification="pii-erasable")
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), status, referenced FROM blob_index WHERE blob_id='blob_1' GROUP BY status, referenced")
        count, status, referenced = cur.fetchone()
    assert count == 1 and status == "live" and referenced is False


def test_referenced_blob_marked_not_orphaned(conn, insert_stub_document, recording_deleter, recording_audit):
    old = NOW - timedelta(days=1)
    _insert_blob(conn, "blob_ref", classification="pii-erasable", created_at=old, object_key="kref")
    insert_stub_document(conn, doc_id="doc_1", body_ref="blob_ref")
    report = mark_and_sweep(conn, now=NOW, grace_seconds=3600,
                            deleter=recording_deleter, auditor=recording_audit)
    assert "blob_ref" not in report.marked_orphan
    status, referenced, _ = _status(conn, "blob_ref")
    assert status == "live" and referenced is True


def test_sensitive_orphan_quarantined_not_deleted(conn, recording_deleter, recording_audit):
    old = NOW - timedelta(days=1)
    _insert_blob(conn, "blob_pii", classification="pii-erasable", created_at=old, object_key="kpii")
    report = mark_and_sweep(conn, now=NOW, grace_seconds=3600,
                            deleter=recording_deleter, auditor=recording_audit)
    assert "blob_pii" in report.quarantined
    assert _status(conn, "blob_pii")[0] == "quarantined"
    assert "kpii" not in recording_deleter.deleted  # sensitive bodies are NOT swept here


def test_nonsensitive_orphan_swept(conn, recording_deleter, recording_audit):
    old = NOW - timedelta(days=1)
    _insert_blob(conn, "blob_gov", classification="governance-retained", created_at=old, object_key="kgov")
    report = mark_and_sweep(conn, now=NOW, grace_seconds=3600,
                            deleter=recording_deleter, auditor=recording_audit)
    assert "blob_gov" in report.swept
    status, _, swept_at = _status(conn, "blob_gov")
    assert status == "swept" and swept_at is not None
    assert "kgov" in recording_deleter.deleted


def test_young_blob_left_live(conn, recording_deleter, recording_audit):
    fresh = NOW - timedelta(seconds=10)
    _insert_blob(conn, "blob_new", classification="governance-retained", created_at=fresh)
    mark_and_sweep(conn, now=NOW, grace_seconds=3600,
                   deleter=recording_deleter, auditor=recording_audit)
    assert _status(conn, "blob_new")[0] == "live"


def test_gc_run_is_audited(conn, recording_deleter, recording_audit):
    mark_and_sweep(conn, now=NOW, grace_seconds=3600,
                   deleter=recording_deleter, auditor=recording_audit)
    assert len(recording_audit.reports) == 1
    assert recording_audit.reports[0].ran_at == NOW
```

- [ ] **(2) Run it, expect FAIL.** `pytest tests/featuregen/runtime/test_blob_gc.py -q` → `ModuleNotFoundError: No module named 'featuregen.runtime.blob_gc'`. (The `blob_index` relation already exists — Phase 02's `0002_documents.sql` created it — so the ONLY failure is the missing module.)

- [ ] **(3) Write minimal implementation.**

`src/featuregen/runtime/blob_gc.py` (no migration in this task — `blob_index` is owned and created by Phase 02):

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Protocol, runtime_checkable

from featuregen.contracts import DbConn


@runtime_checkable
class BlobDeleter(Protocol):
    def delete(self, object_key: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GcReport:
    ran_at: datetime
    marked_orphan: tuple[str, ...] = ()
    quarantined: tuple[str, ...] = ()
    swept: tuple[str, ...] = ()


@runtime_checkable
class GcAuditSink(Protocol):
    def record(self, report: GcReport) -> None: ...


def register_blob(
    conn: DbConn,
    *,
    blob_id: str,
    object_key: str,
    content_hash: str,
    classification: str,
    kms_key_id: Optional[str] = None,
    size_bytes: Optional[int] = None,
) -> str:
    """Index a blob written to the object store BEFORE the §5.1 transaction (status='live',
    referenced=false). Idempotent on blob_id. A committed *_ref later flips referenced=true
    (mark phase); a rolled-back step leaves it unreferenced -> an orphan for GC (§5.1)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, "
            "kms_key_id, size_bytes) VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (blob_id) DO NOTHING",
            (blob_id, object_key, content_hash, classification, kms_key_id, size_bytes),
        )
    return blob_id


def mark_and_sweep(
    conn: DbConn,
    *,
    now: datetime,
    grace_seconds: int,
    deleter: BlobDeleter,
    auditor: GcAuditSink,
) -> GcReport:
    """Mark-and-sweep unreferenced-blob GC (§5.1). MARK: live blobs that a committed
    document references -> referenced=true; live, unreferenced blobs older than the grace
    window with NO committed documents.body_ref pointing at them -> 'orphan'. QUARANTINE:
    pii-erasable orphans -> 'quarantined' (held for §9 erasure/retention, NOT deleted here).
    SWEEP: remaining (non-sensitive) orphans -> object-store delete + 'swept'. Every run is
    audited via the sink."""
    cutoff = now - timedelta(seconds=grace_seconds)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE blob_index b SET referenced = true "
            "WHERE b.status = 'live' AND b.referenced = false "
            "  AND EXISTS (SELECT 1 FROM documents d WHERE d.body_ref = b.blob_id)"
        )
        cur.execute(
            "UPDATE blob_index b SET status = 'orphan' "
            "WHERE b.status = 'live' AND b.referenced = false AND b.created_at < %s "
            "  AND NOT EXISTS (SELECT 1 FROM documents d WHERE d.body_ref = b.blob_id) "
            "RETURNING b.blob_id",
            (cutoff,),
        )
        marked = [r[0] for r in cur.fetchall()]
        cur.execute(
            "UPDATE blob_index SET status = 'quarantined' "
            "WHERE status = 'orphan' AND classification = 'pii-erasable' RETURNING blob_id"
        )
        quarantined = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT blob_id, object_key FROM blob_index WHERE status = 'orphan'")
        sweepable = cur.fetchall()
        swept = []
        for blob_id, object_key in sweepable:
            deleter.delete(object_key)
            cur.execute(
                "UPDATE blob_index SET status = 'swept', swept_at = %s WHERE blob_id = %s",
                (now, blob_id),
            )
            swept.append(blob_id)
    report = GcReport(
        ran_at=now,
        marked_orphan=tuple(marked),
        quarantined=tuple(quarantined),
        swept=tuple(swept),
    )
    auditor.record(report)
    return report
```

- [ ] **(4) Run tests, expect PASS.** `pytest tests/featuregen/runtime/test_blob_gc.py -q` → green.

- [ ] **(5) Commit.** `git add -A && git commit -m "SP-0 Phase 05: blob_index + mark-and-sweep GC (quarantine + audit, §5.1)"`

---

## Phase 05 done-check (spec §12 coverage map)

Run the whole phase suite — `pytest tests/featuregen/runtime/ -q` — and confirm these §12 rows are exercised:

- **Timers** — ladder built (Task 2) and fired across restarts via the poller (Task 3); overdue fire on recovery (`test_overdue_timer_claimed_on_recovery`, Task 3); late timer can't escalate an answered/changed gate (`test_cas_mismatch_suppressed`, `test_answered_task_suppressed`, `test_cancel_on_answer_voids_unfired_rungs`, Task 4); auto-park rung and cost breaker share one handler (`test_auto_park_rung_uses_canonical_handler`, Task 4); business calendar (Task 1).
- **External effects** — dispatcher crash after call: with handle → reconcile/no dup; without + no dedup → residual logged/flagged (no false dedup); with dedup → safe (Task 6); stale result after run advanced ignored, not applied (Task 7).
- **Retries & cost breaker** — transient backoff; permanent skips (`test_permanent_goes_to_dlq`); `Disposition.OK` rejected as a non-failure (`test_ok_disposition_is_rejected`, Task 8); `max_elapsed_time` cap; repair loop stops after N and **routes to a human** via `route_repair_exhaustion` (`test_exhaustion_routes_to_human_idempotently`, `test_rearm_allows_fresh_exhaustion_routing`, Task 9); cost ceiling auto-parks (Tasks 8, 9, 10).
- **Atomicity & blob GC** — rolled-back step leaves a blob → GC quarantines (sensitive) / sweeps (non-sensitive); GC audited (Task 11).
