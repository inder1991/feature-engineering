## Phase 07: Identity, authorization, SoD, human-gate task model & security stream

**Goal:** Build the identity envelope construction/validation (OIDC humans + attested services), the one-vocabulary command authorization with denial routing to a tamper-evident security stream, segregation-of-duties (two-party four-eyes + three-party author≠validator≠approver), the durable human-gate task model (versioned tasks, quorum of distinct authorities with `quorum_of_role` enforcement, validated delegation, gate-SoD on the direct answer path too, staleness keyed to the task, conflict→escalation, cancellation including on run-advance), and break-glass dual-control with a mandatory after-the-fact review.

> Implements spec §6.1, §6.2, §6.3, §7. Builds against the shared SP-0 contract (overview `2026-06-27-sp0-00-overview.md`) VERBATIM; does not redefine shared DDL or interface signatures.

---

### Cross-phase Consumes (interfaces only)

This phase reads/writes tables owned by other phases through their declared shapes; it never reimplements their mechanisms.

- **Phase 01** — `global_seq_seq` (allocator for `security_audit.seq` defaults and `human_task_responses.answered_seq`); the `events` table (read-only: `resolve_run_author` reads `events.actor->>'subject'`); the `DbConn` alias (psycopg connection / open transaction).
- **Phase 05** — the `timers` table: `open_task` inserts the SLA→reminder→escalation→auto-park ladder rows (`status='scheduled'`, `cas_task_version` stamped for Phase 05's CAS-on-fire); `submit_human_signal`/`cancel_task` set `status='cancelled'` on answer/cancel (the answer side of the §5.5 timer/answer race; the fire side is Phase 05). `cas_task_version` is read by Phase 05's poller.
- **Phase 06** — the `Command`/`CommandResult` dataclasses and `execute_command` (Phase 06 OWNS `src/featuregen/contracts/commands.py`; this phase only transcribes those two dataclasses as a byte-identical, divergence-guarded bootstrap — it does not own the file). **Wiring points:** (1) Phase 06's `execute_command` calls `authorize_command(conn, cmd)`; on `allowed=False` it calls `record_denial(conn, cmd, reason)` (writes the security stream, NOT the domain stream) and returns `CommandResult(accepted=False, denied_reason=reason)` — this phase produces those two functions and the SoD they apply. (2) Phase 06's run-advancing lifecycle commands (or the Phase 03 state-machine transition) call this phase's `cancel_tasks_on_run_advance(conn, run_id)` to cancel open gate tasks when a run moves past their gate (§7). The triggering transition is owned upstream (Phase 03/06); the cancellation effect is owned here.

**Shared-contract bootstrap (verbatim, not redefined):** this phase transcribes — character-for-character from the overview's "Core interfaces" block — the dataclasses it is authoritative for or directly constructs: `IdentityEnvelope` (authoritative), `GateTaskSpec`/`SignalResult` (authoritative), and `Command`/`CommandResult` (Phase 06 authoritative — same file, imported, not altered). It also lays down a `DbConn` alias module. Every later phase imports these same symbols.

**Test prerequisite DDL (test-only):** the Phase 01/05 objects this phase's tests read (`global_seq_seq`, `events`, `timers`) are transcribed verbatim into `tests/featuregen/_prereq.sql` purely so the suite is independently runnable. The canonical migrations for those objects are owned by Phase 01/05; `_prereq.sql` is never imported by `src/`.

---

## File structure

```
src/featuregen/
  contracts/
    db.py                  # DbConn type alias (Phase 01-owned shape; transcribed)          [Task 3]
    identity.py            # IdentityEnvelope (verbatim) + identity_to_jsonb/from_jsonb       [Task 1]
    commands.py            # Command, CommandResult (Phase 06 OWNS; transcribed bootstrap only)  [Task 6]
    gates.py               # GateTaskSpec, SignalResult (verbatim)                            [Task 8/9]
  idgen.py                 # mint_id(prefix) ULID-style prefixed id helper                    [Task 3]
  identity/
    __init__.py                                                                               [Task 2]
    build.py               # build_human_identity / build_service_identity / validate_identity [Task 2]
  db/
    migrations/
      007_identity_authz_gates.sql   # authz_policy, security_audit, human_tasks, responses   [Task 3]
  security/
    __init__.py                                                                               [Task 4]
    audit.py               # record_security_event, record_denial, verify_chain, read_*       [Task 4/5]
    break_glass.py         # invoke_break_glass / open_break_glass_review / sign_off_*         [Task 10]
  authz/
    __init__.py                                                                               [Task 6]
    policy.py              # AuthzDecision, seed_authz_policy, authorize_command              [Task 6]
    sod.py                 # two_party_ok / three_party_disjoint / resolvers / enforce_sod    [Task 7]
  gates/
    __init__.py                                                                               [Task 8]
    duration.py            # parse_duration                                                   [Task 8]
    tasks.py               # open_task/cancel_task/cancel_tasks_on_run_advance/bump_task_version/
                           #   grant_task_delegation/submit_human_signal                       [Task 8/9]

tests/featuregen/
  _prereq.sql              # verbatim global_seq_seq + events + timers (test-only)            [Task 3]
  conftest.py              # `db` fixture (fresh schema per test, rollback teardown)          [Task 3]
  contracts/test_identity_serde.py                                                            [Task 1]
  identity/test_build.py                                                                      [Task 2]
  db/test_migration.py                                                                        [Task 3]
  security/test_audit.py                                                                      [Task 4]
  security/test_audit_read.py                                                                 [Task 5]
  authz/test_policy.py                                                                        [Task 6]
  authz/test_sod.py                                                                           [Task 7]
  gates/test_open_task.py                                                                     [Task 8]
  gates/test_submit_signal.py                                                                 [Task 9]
  security/test_break_glass.py                                                                [Task 10]
```

**Test environment:** all DB tests require a reachable PostgreSQL 15+; the suite reads its DSN from `SP0_TEST_DSN` (default `postgresql:///sp0_test`). Install deps once: `python3 -m pip install "psycopg[binary]>=3.1" pytest`.

---

## Task 1: IdentityEnvelope contract + jsonb (de)serialization

**Files:**
- Create: `pytest.ini` (repo root; puts `src/` on the import path for the whole suite)
- Create: `src/featuregen/contracts/__init__.py`
- Create: `src/featuregen/contracts/identity.py`
- Test: `tests/featuregen/contracts/test_identity_serde.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `IdentityEnvelope` (frozen/slots dataclass, verbatim from the shared contract); `identity_to_jsonb(env: IdentityEnvelope) -> dict[str, Any]`; `identity_from_jsonb(d: Mapping[str, Any]) -> IdentityEnvelope`. Round-trips `tuple` fields (`role_claims`, `groups`) through JSON lists. Used by `events.actor`/`security_audit.actor`/`consumers.registered_by` storage across phases.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/contracts/test_identity_serde.py
from featuregen.contracts.identity import (
    IdentityEnvelope,
    identity_to_jsonb,
    identity_from_jsonb,
)


def _human() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="user:raj",
        actor_kind="human",
        authenticated=True,
        auth_method="oidc",
        role_claims=("data_scientist", "approver"),
        groups=("payments-ds",),
        tenant="retail-bank",
        source_of_authority="iam-snapshot@2026-06-27T10:14Z",
    )


def test_to_jsonb_emits_lists_not_tuples():
    d = identity_to_jsonb(_human())
    assert d["subject"] == "user:raj"
    assert d["role_claims"] == ["data_scientist", "approver"]
    assert isinstance(d["role_claims"], list)
    assert d["groups"] == ["payments-ds"]
    assert d["break_glass"] is False
    assert d["attestation"] is None


def test_round_trip_is_identity():
    env = _human()
    assert identity_from_jsonb(identity_to_jsonb(env)) == env


def test_service_attestation_round_trips():
    svc = IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake-agent",),
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert identity_from_jsonb(identity_to_jsonb(svc)) == svc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/contracts/test_identity_serde.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sp0'` — `pytest.ini`'s `pythonpath = src` is already in place (created in Step 3 below), so `src/` is on the path; the failure is the not-yet-created `src/sp0` package tree, not a missing path.

- [ ] **Step 3: Write minimal implementation**

First put `src/` on the import path for the whole suite (this is the ONLY mechanism that makes `sp0` importable; it must exist from Task 1 onward so every task's failing→passing cycle is reproducible):

```ini
# pytest.ini  (repo root)
[pytest]
pythonpath = src
testpaths = tests
```

```python
# src/featuregen/contracts/__init__.py
```

```python
# src/featuregen/contracts/identity.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True, slots=True)
class IdentityEnvelope:
    """Identity-at-time-of-action for humans and services (§6.1)."""
    subject: str                              # "user:raj" | "service:intake-agent"
    actor_kind: str                           # "human" | "service"
    authenticated: bool
    auth_method: str                          # "oidc" | "workload-identity"
    role_claims: tuple[str, ...]              # AT TIME OF ACTION
    groups: tuple[str, ...] = ()
    tenant: Optional[str] = None
    on_behalf_of: Optional[str] = None
    impersonation: Optional[str] = None
    break_glass: bool = False
    source_of_authority: Optional[str] = None  # e.g. "iam-snapshot@2026-06-27T10:14Z"
    attestation: Optional[str] = None          # services: signed deploy id attesting role_claims


def identity_to_jsonb(env: IdentityEnvelope) -> dict[str, Any]:
    return {
        "subject": env.subject,
        "actor_kind": env.actor_kind,
        "authenticated": env.authenticated,
        "auth_method": env.auth_method,
        "role_claims": list(env.role_claims),
        "groups": list(env.groups),
        "tenant": env.tenant,
        "on_behalf_of": env.on_behalf_of,
        "impersonation": env.impersonation,
        "break_glass": env.break_glass,
        "source_of_authority": env.source_of_authority,
        "attestation": env.attestation,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/contracts/test_identity_serde.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add pytest.ini src/featuregen/contracts/__init__.py src/featuregen/contracts/identity.py tests/featuregen/contracts/test_identity_serde.py
git commit -m "feat(sp0-07): pytest pythonpath + IdentityEnvelope contract + jsonb serde"
```

---

## Task 2: Identity construction & validation (OIDC humans, attested services)

**Files:**
- Create: `src/featuregen/identity/__init__.py`
- Create: `src/featuregen/identity/build.py`
- Test: `tests/featuregen/identity/test_build.py`

**Interfaces:**
- Consumes: `IdentityEnvelope` (Task 1).
- Produces: `IdentityError(Exception)`; `validate_identity(env: IdentityEnvelope) -> None` (raises `IdentityError` if a human is not `oidc`/authenticated, or a service is not `workload-identity`/authenticated/**attested** — services may not self-assert role_claims, §6.1); `build_human_identity(*, subject, role_claims, auth_method="oidc", groups=(), tenant=None, source_of_authority=None, on_behalf_of=None, impersonation=None, break_glass=False) -> IdentityEnvelope`; `build_service_identity(*, subject, role_claims, attestation, groups=(), tenant=None, source_of_authority=None) -> IdentityEnvelope`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/identity/test_build.py
import pytest

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity.build import (
    IdentityError,
    build_human_identity,
    build_service_identity,
    validate_identity,
)


def test_build_human_is_oidc_authenticated():
    env = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    assert env.actor_kind == "human"
    assert env.auth_method == "oidc"
    assert env.authenticated is True
    assert env.role_claims == ("data_scientist",)
    assert env.attestation is None


def test_build_human_rejects_unprefixed_subject():
    with pytest.raises(IdentityError):
        build_human_identity(subject="raj", role_claims=["data_scientist"])


def test_build_service_requires_attestation():
    env = build_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert env.actor_kind == "service"
    assert env.auth_method == "workload-identity"
    assert env.attestation == "signed-deploy-id:sp2-intake@1.4.0"


def test_service_without_attestation_is_self_asserted_and_rejected():
    self_asserted = IdentityEnvelope(
        subject="service:rogue",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("approver",),
        attestation=None,
    )
    with pytest.raises(IdentityError):
        validate_identity(self_asserted)


def test_unauthenticated_is_rejected():
    anon = IdentityEnvelope(
        subject="user:ghost",
        actor_kind="human",
        authenticated=False,
        auth_method="oidc",
        role_claims=(),
    )
    with pytest.raises(IdentityError):
        validate_identity(anon)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/identity/test_build.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.identity'` — `pytest.ini` (Task 1) is on the path and `sp0` resolves as a namespace package via the `src/featuregen/contracts/` tree created in Task 1, so the missing module is specifically `featuregen.identity`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/identity/__init__.py
```

```python
# src/featuregen/identity/build.py
from __future__ import annotations

from typing import Iterable, Optional

from featuregen.contracts.identity import IdentityEnvelope


class IdentityError(Exception):
    """Raised when an IdentityEnvelope is malformed or not validly attested (§6.1)."""


def validate_identity(env: IdentityEnvelope) -> None:
    if not env.authenticated:
        raise IdentityError("actor not authenticated")
    if env.actor_kind == "service":
        if env.auth_method != "workload-identity":
            raise IdentityError("service actor must authenticate via workload-identity")
        if not env.attestation:
            raise IdentityError(
                "service role_claims must be attested by a signed deploy identity, "
                "not self-asserted"
            )
    elif env.actor_kind == "human":
        if env.auth_method != "oidc":
            raise IdentityError("human actor must authenticate via oidc")
    else:
        raise IdentityError(f"unknown actor_kind: {env.actor_kind}")


def build_human_identity(
    *,
    subject: str,
    role_claims: Iterable[str],
    auth_method: str = "oidc",
    groups: Iterable[str] = (),
    tenant: Optional[str] = None,
    source_of_authority: Optional[str] = None,
    on_behalf_of: Optional[str] = None,
    impersonation: Optional[str] = None,
    break_glass: bool = False,
) -> IdentityEnvelope:
    if not subject.startswith("user:"):
        raise IdentityError("human subject must be prefixed 'user:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="human",
        authenticated=True,
        auth_method=auth_method,
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        on_behalf_of=on_behalf_of,
        impersonation=impersonation,
        break_glass=break_glass,
        source_of_authority=source_of_authority,
        attestation=None,
    )
    validate_identity(env)
    return env


def build_service_identity(
    *,
    subject: str,
    role_claims: Iterable[str],
    attestation: str,
    groups: Iterable[str] = (),
    tenant: Optional[str] = None,
    source_of_authority: Optional[str] = None,
) -> IdentityEnvelope:
    if not subject.startswith("service:"):
        raise IdentityError("service subject must be prefixed 'service:'")
    env = IdentityEnvelope(
        subject=subject,
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        attestation=attestation,
        source_of_authority=source_of_authority,
    )
    validate_identity(env)
    return env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/identity/test_build.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/identity tests/featuregen/identity/test_build.py
git commit -m "feat(sp0-07): OIDC human + attested service identity construction/validation"
```

---

## Task 3: DB harness, id helper & Phase-07 migration

**Files:**
- Create: `src/featuregen/contracts/db.py`
- Create: `src/featuregen/idgen.py`
- Create: `src/featuregen/db/migrations/007_identity_authz_gates.sql`
- Create: `tests/featuregen/_prereq.sql`
- Create: `tests/featuregen/conftest.py`
- Test: `tests/featuregen/db/test_migration.py`

**Interfaces:**
- Consumes: PostgreSQL 15+ via `psycopg`.
- Produces: `DbConn` type alias (`src/featuregen/contracts/db.py`); `mint_id(prefix: str) -> str` returning `f"{prefix}_{...}"` ULID-style prefixed strings; the `007` migration creating `authz_policy`, `security_audit`, `human_tasks`, `human_task_responses` **verbatim** from the shared DDL, plus the Phase-07-internal supporting table `task_delegations` (not in the core contract; backs §7 delegation validation); the pytest `db` fixture yielding an open `psycopg.Connection` against a freshly-built schema, rolled back after each test.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/db/test_migration.py
def test_phase07_tables_exist(db):
    rows = db.execute(
        """
        SELECT table_name FROM information_schema.tables
         WHERE table_schema = 'public'
           AND table_name IN ('authz_policy','security_audit','human_tasks',
                              'human_task_responses','task_delegations')
         ORDER BY table_name
        """
    ).fetchall()
    assert [r[0] for r in rows] == [
        "authz_policy",
        "human_task_responses",
        "human_tasks",
        "security_audit",
        "task_delegations",
    ]


def test_prereq_objects_present(db):
    assert db.execute("SELECT nextval('global_seq_seq')").fetchone()[0] >= 1
    assert db.execute("SELECT to_regclass('public.events')").fetchone()[0] == "events"
    assert db.execute("SELECT to_regclass('public.timers')").fetchone()[0] == "timers"


def test_mint_id_prefixes():
    from featuregen.idgen import mint_id

    one = mint_id("task")
    two = mint_id("task")
    assert one.startswith("task_")
    assert one != two
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/db/test_migration.py -v`
Expected: FAIL — `conftest.py`/`db` fixture missing → `fixture 'db' not found`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/contracts/db.py
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    import psycopg

    DbConn = psycopg.Connection
else:  # runtime placeholder; all uses are stringified annotations
    DbConn = object
```

```python
# src/featuregen/idgen.py
from __future__ import annotations

import os
import time


def mint_id(prefix: str) -> str:
    """ULID-style prefixed id: monotic-ish time component + random suffix (§3.1)."""
    millis = int(time.time() * 1000)
    rand = os.urandom(8).hex()
    return f"{prefix}_{millis:013d}{rand}"
```

```sql
-- src/featuregen/db/migrations/007_identity_authz_gates.sql
-- Phase 07 owned tables (verbatim from the shared SP-0 contract). PostgreSQL 15+.
-- NOTE: authz_policy PK columns are NOT NULL by definition; the contract marks
-- `gate` NULL but the PK forces it NOT NULL, so non-gate rows use the '' sentinel.
CREATE TABLE authz_policy (
    action        text        NOT NULL,
    gate          text        NULL,
    permitted_role text       NOT NULL,
    actor_kind    text        NOT NULL CHECK (actor_kind IN ('human','service','any')),
    scope         text        NULL,
    PRIMARY KEY (action, gate, permitted_role, actor_kind)
);

CREATE TABLE security_audit (
    security_event_id text        PRIMARY KEY,
    seq               bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    event_type        text        NOT NULL,
    actor             jsonb       NOT NULL,
    attempted_action  text        NOT NULL,
    aggregate         text        NULL,
    aggregate_id      text        NULL,
    decision          text        NOT NULL
                          CHECK (decision IN ('denied','allowed_break_glass','flagged')),
    reason            text        NULL,
    prev_hash         text        NULL,
    entry_hash        text        NOT NULL,
    retention_class   text        NOT NULL DEFAULT 'regulator',
    occurred_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX security_audit_seq_idx   ON security_audit (seq);
CREATE INDEX security_audit_actor_idx ON security_audit ((actor->>'subject'));

CREATE TABLE human_tasks (
    task_id            text        PRIMARY KEY,
    task_version       integer     NOT NULL DEFAULT 1,
    run_id             text        NULL,
    feature_id         text        NULL,
    gate               text        NOT NULL
                           CHECK (gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
                                           'INDEPENDENT_VALIDATION','FINAL_APPROVAL')),
    required_inputs    text[]      NOT NULL DEFAULT '{}',
    eligible_assignees jsonb       NOT NULL,
    allowed_responses  text[]      NOT NULL,
    quorum_required    integer     NOT NULL DEFAULT 1,
    quorum_of_role     text        NULL,
    delegation_allowed boolean     NOT NULL DEFAULT true,
    sla                text        NULL,
    status             text        NOT NULL DEFAULT 'open'
                           CHECK (status IN ('open','answered','conflict','expired','cancelled','superseded')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX human_tasks_open_idx ON human_tasks (gate) WHERE status = 'open';

CREATE TABLE human_task_responses (
    task_id      text        NOT NULL REFERENCES human_tasks(task_id),
    subject      text        NOT NULL,
    response     text        NOT NULL,
    on_behalf_of text        NULL,
    answered_seq bigint      NOT NULL,
    answered_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, subject)
);

-- task_delegations — Phase-07 SUPPORTING table (NOT part of the shared core DDL; phase-internal,
-- referenced only by this phase). The core contract gives us `human_tasks.delegation_allowed`
-- and `human_task_responses.on_behalf_of` but no place to record WHO may answer on whose behalf.
-- This table records validated delegation grants so submit_human_signal can verify a REAL
-- delegation relationship exists and that the PRINCIPAL is itself an eligible assignee (§7).
CREATE TABLE task_delegations (
    task_id    text        NOT NULL REFERENCES human_tasks(task_id),
    principal  text        NOT NULL,                              -- eligible assignee granting authority
    delegate   text        NOT NULL,                              -- subject acting on the principal's behalf
    granted_by text        NOT NULL,                              -- who recorded the grant
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, principal, delegate)
);
```

```sql
-- tests/featuregen/_prereq.sql
-- TEST-ONLY prerequisite DDL. Owned by Phase 01 (global_seq_seq, events) and
-- Phase 05 (timers); transcribed verbatim from the shared contract so this
-- phase's suite is runnable in isolation. NEVER imported by src/.
CREATE SEQUENCE global_seq_seq AS bigint INCREMENT BY 1 START WITH 1 NO CYCLE CACHE 1;

CREATE TABLE events (
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

```python
# tests/featuregen/conftest.py
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest

_HERE = Path(__file__).parent
_PREREQ = (_HERE / "_prereq.sql").read_text()
_MIGRATION = (
    _HERE.parent.parent / "src" / "sp0" / "db" / "migrations" / "007_identity_authz_gates.sql"
).read_text()
_SCHEMA = _PREREQ + "\n" + _MIGRATION


@pytest.fixture
def db():
    dsn = os.environ.get("SP0_TEST_DSN", "postgresql:///sp0_test")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    conn.execute(_SCHEMA)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
```

(The import path is already configured: `pytest.ini` with `pythonpath = src` was created in Task 1, so this fixture's `import psycopg` and the suite's `import featuregen...` both resolve here.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/db/test_migration.py -v`
Expected: PASS (3 passed). (Requires a reachable Postgres at `SP0_TEST_DSN`.)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/contracts/db.py src/featuregen/idgen.py src/featuregen/db/migrations/007_identity_authz_gates.sql tests/featuregen/_prereq.sql tests/featuregen/conftest.py tests/featuregen/db/test_migration.py
git commit -m "feat(sp0-07): db test harness, id helper, phase-07 migration"
```

---

## Task 4: Security stream — append + tamper-evident hash chain

**Files:**
- Create: `src/featuregen/security/__init__.py`
- Create: `src/featuregen/security/audit.py`
- Test: `tests/featuregen/security/test_audit.py`

**Interfaces:**
- Consumes: `mint_id` (Task 3); `IdentityEnvelope`, `identity_to_jsonb` (Task 1); `security_audit` table (Task 3); `DbConn`.
- Produces: `record_security_event(conn, *, event_type, actor, attempted_action, decision, reason=None, aggregate=None, aggregate_id=None, retention_class="regulator") -> str` (returns `security_event_id`; appends to the **security stream**, never the domain stream; takes a transaction-scoped advisory lock so appends to the single chain are serialized — no genesis/same-prev fork under concurrency; chains `entry_hash = sha256(prev_hash | sec_id | event_type | subject | attempted_action | aggregate | aggregate_id | decision | reason)`); `record_denial(conn, cmd, reason: str) -> str` (`event_type='COMMAND_DENIED'`, `decision='denied'`); `verify_chain(conn) -> bool` (recomputes the whole chain in `seq` order).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/security/test_audit.py
from featuregen.identity.build import build_human_identity
from featuregen.security.audit import record_security_event, verify_chain


def test_append_chains_and_verifies(db):
    a = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    first = record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="activate",
        decision="denied",
        reason="no matching authz policy",
        aggregate="feature",
        aggregate_id="feature_1",
    )
    second = record_security_event(
        db,
        event_type="COMMAND_DENIED",
        actor=a,
        attempted_action="deprecate",
        decision="denied",
        reason="no matching authz policy",
    )
    assert first.startswith("sec_")
    assert second != first
    rows = db.execute(
        "SELECT prev_hash, entry_hash FROM security_audit ORDER BY seq ASC"
    ).fetchall()
    assert rows[0][0] is None                 # genesis prev_hash
    assert rows[1][0] == rows[0][1]           # chain links
    assert verify_chain(db) is True


def test_tampering_breaks_chain(db):
    a = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db, event_type="COMMAND_DENIED", actor=a,
        attempted_action="activate", decision="denied", reason="r1",
    )
    db.execute("UPDATE security_audit SET reason = 'edited' WHERE seq = 1")
    assert verify_chain(db) is False


def test_denial_lands_in_security_stream_not_events(db):
    from featuregen.security.audit import record_denial
    from types import SimpleNamespace

    a = build_human_identity(subject="user:mallory", role_claims=["data_scientist"])
    cmd = SimpleNamespace(action="activate", aggregate="feature",
                          aggregate_id="feature_9", actor=a)
    record_denial(db, cmd, "no matching authz policy")
    assert db.execute("SELECT count(*) FROM security_audit").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_concurrent_appends_keep_single_chain(db):
    # Two genuinely concurrent committers must NOT fork the tamper-evident chain
    # (no two genesis rows; the second must chain off the first). The advisory xact
    # lock in record_security_event serializes them.
    import os
    import threading

    import psycopg

    dsn = os.environ.get("SP0_TEST_DSN", "postgresql:///sp0_test")
    actor = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    ready = threading.Barrier(2)

    def worker():
        conn = psycopg.connect(dsn)
        try:
            ready.wait()                       # maximize contention on the empty table
            record_security_event(
                conn, event_type="COMMAND_DENIED", actor=actor,
                attempted_action="activate", decision="denied", reason="race",
            )
            conn.commit()
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rows = db.execute(
        "SELECT prev_hash, entry_hash FROM security_audit ORDER BY seq ASC"
    ).fetchall()
    assert len(rows) == 2
    assert sum(1 for r in rows if r[0] is None) == 1     # exactly one genesis — no fork
    assert rows[1][0] == rows[0][1]                       # second chains off the first
    assert verify_chain(db) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/security/test_audit.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.security'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/security/__init__.py
```

```python
# src/featuregen/security/audit.py
from __future__ import annotations

import hashlib
from typing import Any, Optional

from psycopg.types.json import Json

from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import IdentityEnvelope, identity_to_jsonb
from featuregen.idgen import mint_id

# Transaction-scoped advisory-lock key that serializes ALL appends to the single
# tamper-evident security chain (§6.2). Without it the chain can FORK: on an empty table
# `... ORDER BY seq DESC LIMIT 1 FOR UPDATE` locks no rows, so two concurrent transactions
# both read prev_hash=None and both insert genesis rows; more generally two writers can chain
# off the same prev. `pg_advisory_xact_lock` is released automatically on COMMIT/ROLLBACK, so
# it never leaks. (Re-acquiring it within one transaction is a no-op — multiple appends in the
# same §5.1 step are fine.)
_SECURITY_CHAIN_LOCK_KEY = 7_000_007


def _entry_hash(
    prev_hash: Optional[str],
    sec_id: str,
    event_type: str,
    subject: str,
    attempted_action: str,
    aggregate: Optional[str],
    aggregate_id: Optional[str],
    decision: str,
    reason: Optional[str],
) -> str:
    payload = "|".join(
        [
            prev_hash or "",
            sec_id,
            event_type,
            subject,
            attempted_action,
            aggregate or "",
            aggregate_id or "",
            decision,
            reason or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_security_event(
    conn: DbConn,
    *,
    event_type: str,
    actor: IdentityEnvelope,
    attempted_action: str,
    decision: str,
    reason: Optional[str] = None,
    aggregate: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    retention_class: str = "regulator",
) -> str:
    # Serialize chain appends so the prev_hash read + insert is atomic for the single chain
    # (fixes the empty-table / same-prev fork race; FOR UPDATE alone cannot lock a row that
    # does not exist yet).
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SECURITY_CHAIN_LOCK_KEY,))
    prev = conn.execute(
        "SELECT entry_hash FROM security_audit ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    prev_hash = prev[0] if prev else None
    sec_id = mint_id("sec")
    entry_hash = _entry_hash(
        prev_hash, sec_id, event_type, actor.subject, attempted_action,
        aggregate, aggregate_id, decision, reason,
    )
    conn.execute(
        """
        INSERT INTO security_audit
            (security_event_id, event_type, actor, attempted_action, aggregate,
             aggregate_id, decision, reason, prev_hash, entry_hash, retention_class)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        (
            sec_id, event_type, Json(identity_to_jsonb(actor)), attempted_action,
            aggregate, aggregate_id, decision, reason, prev_hash, entry_hash,
            retention_class,
        ),
    )
    return sec_id


def record_denial(conn: DbConn, cmd: Any, reason: str) -> str:
    """Route an authorization denial to the security stream (§6.2), not the domain stream."""
    return record_security_event(
        conn,
        event_type="COMMAND_DENIED",
        actor=cmd.actor,
        attempted_action=cmd.action,
        decision="denied",
        reason=reason,
        aggregate=cmd.aggregate,
        aggregate_id=cmd.aggregate_id,
    )


def verify_chain(conn: DbConn) -> bool:
    rows = conn.execute(
        """
        SELECT security_event_id, event_type, actor->>'subject', attempted_action,
               aggregate, aggregate_id, decision, reason, prev_hash, entry_hash
          FROM security_audit
         ORDER BY seq ASC
        """
    ).fetchall()
    prev_hash: Optional[str] = None
    for (sec_id, event_type, subject, attempted_action, aggregate, aggregate_id,
         decision, reason, row_prev, entry_hash) in rows:
        if row_prev != prev_hash:
            return False
        if _entry_hash(prev_hash, sec_id, event_type, subject, attempted_action,
                       aggregate, aggregate_id, decision, reason) != entry_hash:
            return False
        prev_hash = entry_hash
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/security/test_audit.py -v`
Expected: PASS (4 passed). (The concurrency test opens its own committing connections, so a reachable Postgres at `SP0_TEST_DSN` is required.)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/security/__init__.py src/featuregen/security/audit.py tests/featuregen/security/test_audit.py
git commit -m "feat(sp0-07): tamper-evident security stream append + denial routing"
```

---

## Task 5: Security stream — restricted, audited read

**Files:**
- Modify: `src/featuregen/security/audit.py`
- Test: `tests/featuregen/security/test_audit_read.py`

**Interfaces:**
- Consumes: `record_security_event` (Task 4); `IdentityEnvelope` (Task 1); `security_audit` table.
- Produces: `AuditReadDenied(Exception)`; `read_security_audit(conn, actor: IdentityEnvelope, *, limit: int = 100) -> list[tuple[str, str, str, str | None]]` — the **single** gate for security-stream reads (not routed through `execute_command`/`authz_policy`); restricted to a **valid identity** (`validate_identity`) carrying a `security`/`compliance` role_claim (§6.2), so a spoofed/unauthenticated envelope with a planted role claim is denied; **every read is itself audited** (§9): an allowed read writes an `AUDIT_READ`/`flagged` entry and returns `(security_event_id, event_type, decision, reason)` rows; an unauthorized read writes an `AUDIT_READ`/`denied` entry and raises `AuditReadDenied`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/security/test_audit_read.py
import pytest

from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity.build import build_human_identity
from featuregen.security.audit import (
    AuditReadDenied,
    read_security_audit,
    record_security_event,
)


def _seed(db):
    a = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    record_security_event(
        db, event_type="COMMAND_DENIED", actor=a,
        attempted_action="activate", decision="denied", reason="nope",
    )


def test_security_role_can_read_and_read_is_logged(db):
    _seed(db)
    sec = build_human_identity(subject="user:sec", role_claims=["security"])
    rows = read_security_audit(db, sec)
    types = {r[1] for r in rows}
    assert "COMMAND_DENIED" in types
    assert "AUDIT_READ" in types                      # the read logged itself
    logged = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert logged == [("flagged",)]


def test_feature_owner_cannot_read_security_stream(db):
    _seed(db)
    owner = build_human_identity(subject="user:owner", role_claims=["owner"])
    with pytest.raises(AuditReadDenied):
        read_security_audit(db, owner)
    denied = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert denied == [("denied",)]


def test_unauthenticated_envelope_with_security_role_is_denied(db):
    _seed(db)
    spoofed = IdentityEnvelope(
        subject="user:spoof", actor_kind="human", authenticated=False,
        auth_method="oidc", role_claims=("security",),
    )
    with pytest.raises(AuditReadDenied):
        read_security_audit(db, spoofed)
    decisions = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='AUDIT_READ'"
    ).fetchall()
    assert decisions == [("denied",)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/security/test_audit_read.py -v`
Expected: FAIL with `ImportError: cannot import name 'AuditReadDenied'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/security/audit.py`:

```python
class AuditReadDenied(Exception):
    """Raised when a non-security/compliance actor attempts to read the security stream (§6.2)."""


_AUDIT_READ_ROLES = ("security", "compliance")


def read_security_audit(
    conn: DbConn,
    actor: IdentityEnvelope,
    *,
    limit: int = 100,
) -> list[tuple[str, str, str, Optional[str]]]:
    # This function is the SINGLE enforcement path for security-stream reads: they are NOT
    # routed through execute_command/authz_policy, so there is no divergent second gate (the
    # authz_policy `read_security_audit` rows are intentionally absent — see Task 6). A role
    # claim alone is insufficient: the envelope must also be a VALID identity, else a spoofed
    # or unauthenticated envelope carrying a "security" claim could read the stream.
    from featuregen.identity.build import IdentityError, validate_identity

    try:
        validate_identity(actor)
        identity_ok = True
    except IdentityError:
        identity_ok = False
    allowed = identity_ok and any(r in actor.role_claims for r in _AUDIT_READ_ROLES)
    if not allowed:
        record_security_event(
            conn,
            event_type="AUDIT_READ",
            actor=actor,
            attempted_action="read_security_audit",
            decision="denied",
            reason="security stream read restricted to security/compliance",
        )
        raise AuditReadDenied("security stream read restricted to security/compliance")
    record_security_event(
        conn,
        event_type="AUDIT_READ",
        actor=actor,
        attempted_action="read_security_audit",
        decision="flagged",
        reason="security stream read",
    )
    rows = conn.execute(
        """
        SELECT security_event_id, event_type, decision, reason
          FROM security_audit
         ORDER BY seq ASC
         LIMIT %s
        """,
        (limit,),
    ).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/security/test_audit_read.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/security/audit.py tests/featuregen/security/test_audit_read.py
git commit -m "feat(sp0-07): restricted + self-audited security stream read"
```

---

## Task 6: Command authorization — one vocabulary, policy rows, attested services

**Files:**
- Create: `src/featuregen/contracts/commands.py`
- Create: `src/featuregen/authz/__init__.py`
- Create: `src/featuregen/authz/policy.py`
- Test: `tests/featuregen/authz/test_policy.py`

**Interfaces:**
- Consumes: `IdentityEnvelope` (Task 1); `validate_identity`, `IdentityError` (Task 2); `authz_policy` table (Task 3); `DbConn`; **`Command`/`CommandResult`** — owned by Phase 06 (`src/featuregen/contracts/commands.py`); this phase only transcribes them as an independence bootstrap (byte-identical to the overview, divergence-guarded by `test_command_contract_fields_match`) and never alters them.
- Produces: `AuthzDecision(allowed: bool, reason: Optional[str] = None)`; `seed_authz_policy(conn) -> None` (loads the §6.2 canonical vocabulary; non-gate rows use `gate=''`); `authorize_command(conn, cmd: Command) -> AuthzDecision` (matches a policy row by `(action, gate)` × role × `actor_kind` × `scope`; **service actors only match `service`/`any` rows when attested**; identity-invalid → denied).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/authz/test_policy.py
from featuregen.authz.policy import AuthzDecision, authorize_command, seed_authz_policy
from featuregen.contracts.commands import Command
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.identity.build import build_human_identity, build_service_identity


def _cmd(action, actor, *, aggregate="feature", aggregate_id="feature_1", args=None):
    return Command(
        action=action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        args=args or {},
        actor=actor,
        idempotency_key="idem_" + action,
    )


def test_authorized_human_action(db):
    seed_authz_policy(db)
    raj = build_human_identity(subject="user:raj", role_claims=["release"])
    assert authorize_command(db, _cmd("activate", raj)) == AuthzDecision(True)


def test_wrong_role_denied(db):
    seed_authz_policy(db)
    raj = build_human_identity(subject="user:raj", role_claims=["data_scientist"])
    decision = authorize_command(db, _cmd("activate", raj))
    assert decision.allowed is False
    assert decision.reason == "no matching authz policy"


def test_attested_service_authorized(db):
    seed_authz_policy(db)
    svc = build_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )
    assert authorize_command(
        db, _cmd("create_run", svc, aggregate="request", aggregate_id="request_1")
    ).allowed is True


def test_self_asserted_service_denied(db):
    seed_authz_policy(db)
    rogue = IdentityEnvelope(
        subject="service:rogue", actor_kind="service", authenticated=True,
        auth_method="workload-identity", role_claims=("intake-agent",), attestation=None,
    )
    decision = authorize_command(
        db, _cmd("create_run", rogue, aggregate="request", aggregate_id="request_1")
    )
    assert decision.allowed is False


def test_gate_scoped_action_uses_gate_column(db):
    seed_authz_policy(db)
    owner = build_human_identity(subject="user:do", role_claims=["data_owner"])
    ok = authorize_command(
        db, _cmd("submit_human_signal", owner, aggregate="run", aggregate_id="run_1",
                 args={"gate": "DATA_STEWARD", "task_id": "task_1"}),
    )
    assert ok.allowed is True
    wrong_gate = authorize_command(
        db, _cmd("submit_human_signal", owner, aggregate="run", aggregate_id="run_1",
                 args={"gate": "COMPLIANCE", "task_id": "task_1"}),
    )
    assert wrong_gate.allowed is False


def test_command_contract_fields_match():
    # The file is shared with Phase 06 (authoritative). Pin the transcribed dataclass
    # signatures to the overview contract so any divergence fails loudly here instead of
    # producing a silent clearLayers()-vs-clearFullLayers() mismatch across phases.
    import dataclasses

    from featuregen.contracts.commands import Command, CommandResult

    assert [f.name for f in dataclasses.fields(Command)] == [
        "action", "aggregate", "aggregate_id", "args", "actor",
        "idempotency_key", "expected_version",
    ]
    assert [f.name for f in dataclasses.fields(CommandResult)] == [
        "accepted", "aggregate_id", "produced_event_ids", "denied_reason",
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/authz/test_policy.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.authz'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/contracts/commands.py
# PHASE 06 IS AUTHORITATIVE for this file — it owns Command / CommandResult / execute_command /
# command_idempotency per the overview "Key Produces interfaces". Phase 07 only CONSUMES the two
# dataclasses below; it does NOT own them. They are transcribed CHARACTER-FOR-CHARACTER from the
# overview "Core interfaces" block purely so this phase is independently runnable before Phase 06
# lands (the same independence pattern as tests/featuregen/_prereq.sql and contracts/db.py). When Phase 06
# is present, its definition is authoritative and this transcription MUST be byte-identical — a
# divergence is a build-breaking bug. `test_command_contract_fields_match` (this task) pins the
# field signature so the two copies can never silently diverge.
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from featuregen.contracts.identity import IdentityEnvelope


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
```

```python
# src/featuregen/authz/__init__.py
```

```python
# src/featuregen/authz/policy.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from featuregen.contracts.commands import Command
from featuregen.contracts.db import DbConn
from featuregen.identity.build import IdentityError, validate_identity


@dataclass(frozen=True, slots=True)
class AuthzDecision:
    allowed: bool
    reason: Optional[str] = None


# §6.2 canonical action vocabulary. gate='' for non-gate actions (PK forbids NULL).
_POLICY_ROWS: tuple[tuple[str, str, str, str, Optional[str]], ...] = (
    ("create_request", "", "data_scientist", "human", None),
    ("create_request", "", "intake-agent", "service", None),
    ("create_run", "", "data_scientist", "human", None),
    ("create_run", "", "intake-agent", "service", None),
    ("select_candidate", "", "data_scientist", "human", None),
    ("submit_human_signal", "CLARIFICATION", "data_scientist", "human", None),
    ("submit_human_signal", "CLARIFICATION", "intake-agent", "service", None),
    ("submit_human_signal", "DATA_STEWARD", "data_owner", "human", None),
    ("submit_human_signal", "COMPLIANCE", "compliance", "human", None),
    ("submit_human_signal", "INDEPENDENT_VALIDATION", "validator", "human", None),
    ("submit_human_signal", "FINAL_APPROVAL", "approver", "human", None),
    ("open_task", "", "workflow", "service", None),
    ("activate", "", "release", "human", None),
    ("supersede", "", "release", "human", None),
    ("deprecate", "", "release", "human", None),
    ("retier", "", "release", "human", None),
    ("register_consumer", "", "owner", "human", None),
    ("deregister_consumer", "", "owner", "human", None),
    ("raise_monitoring_alert", "", "monitoring", "service", None),
    ("require_revalidation", "", "overlay", "service", None),
    ("record_revalidation_outcome", "", "overlay", "service", None),
    ("fact_confirmed_resume", "", "overlay", "service", None),
    ("cancel", "", "data_scientist", "human", None),
    ("withdraw", "", "data_scientist", "human", None),
    ("reject", "", "validator", "human", None),
    ("park", "", "data_scientist", "human", None),
    ("unpark", "", "data_scientist", "human", None),
    ("reopen_as_new_run", "", "data_scientist", "human", None),
    ("duplicate_of", "", "data_scientist", "human", None),
    ("manual_retry", "", "data_scientist", "human", None),
    ("resolve_degraded", "", "platform-admin", "human", None),
    ("migrate_workflow_version", "", "platform-admin", "human", None),
    ("migrate_feature_lifecycle_version", "", "platform-admin", "human", None),
    ("admin_correct", "", "platform-admin", "human", None),
    ("break_glass", "", "platform-admin", "human", None),
    ("read_audit", "", "auditor", "human", None),
    ("read_audit", "", "compliance", "human", None),
    ("read_audit", "", "owner", "human", None),
    # NOTE: security-stream reads are deliberately NOT authorized here. They are gated solely by
    # read_security_audit() (validate_identity + security/compliance role + self-audit). Adding
    # authz_policy rows would create a divergent second gate that the read path never consults.
)


def seed_authz_policy(conn: DbConn) -> None:
    for action, gate, role, kind, scope in _POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )


def _base_authorized(conn: DbConn, cmd: Command) -> AuthzDecision:
    try:
        validate_identity(cmd.actor)
    except IdentityError as exc:
        return AuthzDecision(False, str(exc))
    gate = cmd.args.get("gate", "") if cmd.action == "submit_human_signal" else ""
    rows = conn.execute(
        "SELECT permitted_role, actor_kind, scope FROM authz_policy WHERE action=%s AND gate=%s",
        (cmd.action, gate or ""),
    ).fetchall()
    for permitted_role, actor_kind, scope in rows:
        if actor_kind not in ("any", cmd.actor.actor_kind):
            continue
        if permitted_role not in cmd.actor.role_claims:
            continue
        if scope is not None and scope not in cmd.actor.groups:
            continue
        if cmd.actor.actor_kind == "service" and not cmd.actor.attestation:
            continue
        return AuthzDecision(True)
    return AuthzDecision(False, "no matching authz policy")


def authorize_command(conn: DbConn, cmd: Command) -> AuthzDecision:
    return _base_authorized(conn, cmd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/authz/test_policy.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/contracts/commands.py src/featuregen/authz/__init__.py src/featuregen/authz/policy.py tests/featuregen/authz/test_policy.py
git commit -m "feat(sp0-07): one-vocabulary command authorization with attested services"
```

---

## Task 7: Segregation of duties — two-party four-eyes & three-party validation

**Files:**
- Create: `src/featuregen/authz/sod.py`
- Modify: `src/featuregen/authz/policy.py` (wire `enforce_sod` into `authorize_command`)
- Test: `tests/featuregen/authz/test_sod.py`

**Interfaces:**
- Consumes: `AuthzDecision` (Task 6); `Command` (Task 6); `human_task_responses`/`human_tasks` (Task 3); `events` (Task 3, read-only); `DbConn`.
- Produces: `two_party_ok(requester: str, approver: str) -> bool`; `three_party_disjoint(author: str, validators: set[str], approver: str) -> bool`; `resolve_run_author(conn, run_id: str) -> Optional[str]` (run-stream first-event actor subject); `gather_gate_responders(conn, gate: str, *, run_id=None, feature_id=None) -> set[str]`; `gate_sod_reason(conn, *, gate, subject, run_id=None, feature_id=None) -> Optional[str]` (the shared gate-SoD predicate, reused by `submit_human_signal` so the direct call path enforces the same SoD); `enforce_sod(conn, cmd: Command) -> AuthzDecision` (FINAL_APPROVAL: approver≠requester and ≠ any INDEPENDENT_VALIDATION validator; INDEPENDENT_VALIDATION: validator≠author; compliance-sensitive `activate`/`supersede`/`deprecate`: actor≠`args['requested_by']`).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/authz/test_sod.py
from psycopg.types.json import Json

from featuregen.authz.policy import authorize_command, seed_authz_policy
from featuregen.authz.sod import (
    gather_gate_responders,
    resolve_run_author,
    three_party_disjoint,
    two_party_ok,
)
from featuregen.contracts.commands import Command
from featuregen.identity.build import build_human_identity


def _seed_run(db, run_id, author_subject):
    db.execute(
        """
        INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                            type, schema_version, table_version, actor, payload, provenance,
                            occurred_at)
        VALUES (%s,'run',%s,1,%s,'RUN_CREATED',1,1,%s,'{}'::jsonb,'{}'::jsonb, now())
        """,
        ("evt_" + run_id, run_id, run_id, Json({"subject": author_subject})),
    )


def _open_and_answer_iv(db, run_id, validator_subject):
    db.execute(
        """
        INSERT INTO human_tasks (task_id, run_id, gate, eligible_assignees, allowed_responses)
        VALUES ('task_iv', %s, 'INDEPENDENT_VALIDATION', '{"role":"validator"}'::jsonb,
                '{validate}')
        """,
        (run_id,),
    )
    db.execute(
        "INSERT INTO human_task_responses (task_id, subject, response, answered_seq) "
        "VALUES ('task_iv', %s, 'validate', 1)",
        (validator_subject,),
    )


def test_pure_helpers():
    assert two_party_ok("user:a", "user:b") is True
    assert two_party_ok("user:a", "user:a") is False
    assert three_party_disjoint("user:a", {"user:b"}, "user:c") is True
    assert three_party_disjoint("user:a", {"user:b"}, "user:b") is False
    assert three_party_disjoint("user:a", {"user:a"}, "user:c") is False


def test_resolvers(db):
    _seed_run(db, "run_1", "user:author")
    _open_and_answer_iv(db, "run_1", "user:val")
    assert resolve_run_author(db, "run_1") == "user:author"
    assert gather_gate_responders(db, "INDEPENDENT_VALIDATION", run_id="run_1") == {"user:val"}


def test_final_approval_blocks_requester_self_approval(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    author_as_approver = build_human_identity(
        subject="user:author", role_claims=["approver"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "FINAL_APPROVAL", "task_id": "task_fa"},
        actor=author_as_approver, idempotency_key="i1",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "four-eyes" in decision.reason


def test_three_party_blocks_validator_as_approver(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    _open_and_answer_iv(db, "run_1", "user:val")
    validator_as_approver = build_human_identity(
        subject="user:val", role_claims=["approver"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "FINAL_APPROVAL", "task_id": "task_fa"},
        actor=validator_as_approver, idempotency_key="i2",
    )
    decision = authorize_command(db, cmd)
    assert decision.allowed is False
    assert "validator" in decision.reason


def test_independent_validation_blocks_author_as_validator(db):
    seed_authz_policy(db)
    _seed_run(db, "run_1", "user:author")
    author_as_validator = build_human_identity(
        subject="user:author", role_claims=["validator"]
    )
    cmd = Command(
        action="submit_human_signal", aggregate="run", aggregate_id="run_1",
        args={"gate": "INDEPENDENT_VALIDATION", "task_id": "task_iv2"},
        actor=author_as_validator, idempotency_key="i3",
    )
    assert authorize_command(db, cmd).allowed is False


def test_compliance_sensitive_activate_needs_four_eyes(db):
    seed_authz_policy(db)
    rel = build_human_identity(subject="user:rel", role_claims=["release"])
    cmd = Command(
        action="activate", aggregate="feature", aggregate_id="feature_1",
        args={"compliance_sensitive": True, "requested_by": "user:rel"},
        actor=rel, idempotency_key="i4",
    )
    assert authorize_command(db, cmd).allowed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/authz/test_sod.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.authz.sod'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/authz/sod.py
from __future__ import annotations

from typing import Optional

from featuregen.authz.policy import AuthzDecision
from featuregen.contracts.commands import Command
from featuregen.contracts.db import DbConn


def two_party_ok(requester: str, approver: str) -> bool:
    return requester != approver


def three_party_disjoint(author: str, validators: set[str], approver: str) -> bool:
    return (
        author not in validators
        and approver not in validators
        and author != approver
    )


def resolve_run_author(conn: DbConn, run_id: str) -> Optional[str]:
    row = conn.execute(
        """
        SELECT actor->>'subject' FROM events
         WHERE aggregate='run' AND aggregate_id=%s
         ORDER BY stream_version ASC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    return row[0] if row else None


def gather_gate_responders(
    conn: DbConn,
    gate: str,
    *,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> set[str]:
    rows = conn.execute(
        """
        SELECT r.subject
          FROM human_task_responses r
          JOIN human_tasks t ON t.task_id = r.task_id
         WHERE t.gate = %s
           AND ((%s IS NOT NULL AND t.run_id = %s)
                OR (%s IS NOT NULL AND t.feature_id = %s))
        """,
        (gate, run_id, run_id, feature_id, feature_id),
    ).fetchall()
    return {r[0] for r in rows}


def gate_sod_reason(
    conn: DbConn,
    *,
    gate: Optional[str],
    subject: str,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> Optional[str]:
    """PURE-ish SoD predicate for a single gate answer, keyed to the EFFECTIVE authority
    (`subject`). Shared by BOTH the command-authz path (`enforce_sod`) and the direct
    `submit_human_signal` call path (§7) so the two can never diverge. Returns a denial reason
    string, or None when the answer satisfies SoD (§6.3)."""
    author = resolve_run_author(conn, run_id) if run_id else None
    if gate == "INDEPENDENT_VALIDATION":
        if author is not None and subject == author:
            return "independent validation requires validator != author"
    elif gate == "FINAL_APPROVAL":
        if author is not None and not two_party_ok(author, subject):
            return "four-eyes: approver != requester"
        validators = (
            gather_gate_responders(conn, "INDEPENDENT_VALIDATION", run_id=run_id)
            if run_id
            else set()
        )
        if validators and subject in validators:
            return "three-party: approver != validator"
    return None


def enforce_sod(conn: DbConn, cmd: Command) -> AuthzDecision:
    if cmd.action == "submit_human_signal":
        gate = cmd.args.get("gate")
        run_id = cmd.aggregate_id if cmd.aggregate == "run" else cmd.args.get("run_id")
        feature_id = (
            cmd.aggregate_id if cmd.aggregate == "feature" else cmd.args.get("feature_id")
        )
        reason = gate_sod_reason(
            conn, gate=gate, subject=cmd.actor.subject, run_id=run_id, feature_id=feature_id
        )
        return AuthzDecision(reason is None, reason)
    if cmd.action in ("activate", "supersede", "deprecate") and cmd.args.get(
        "compliance_sensitive"
    ):
        requested_by = cmd.args.get("requested_by")
        if requested_by is not None and not two_party_ok(requested_by, cmd.actor.subject):
            return AuthzDecision(
                False, "four-eyes: actor != requester for compliance-sensitive change"
            )
    return AuthzDecision(True)
```

Then wire SoD into `authorize_command` — replace the body in `src/featuregen/authz/policy.py`:

```python
def authorize_command(conn: DbConn, cmd: Command) -> AuthzDecision:
    base = _base_authorized(conn, cmd)
    if not base.allowed:
        return base
    from featuregen.authz.sod import enforce_sod  # local import avoids module cycle

    return enforce_sod(conn, cmd)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/authz/test_sod.py tests/featuregen/authz/test_policy.py -v`
Expected: PASS (11 passed — Task 6's 5 tests still green alongside the 6 SoD tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/authz/sod.py src/featuregen/authz/policy.py tests/featuregen/authz/test_sod.py
git commit -m "feat(sp0-07): two-party four-eyes + three-party validation SoD"
```

---

## Task 8: Human-gate task — open_task (+ SLA ladder), cancel, version bump

**Files:**
- Create: `src/featuregen/contracts/gates.py`
- Create: `src/featuregen/gates/__init__.py`
- Create: `src/featuregen/gates/duration.py`
- Create: `src/featuregen/gates/tasks.py`
- Test: `tests/featuregen/gates/test_open_task.py`

**Interfaces:**
- Consumes: `mint_id` (Task 3); `IdentityEnvelope` (Task 1); `human_tasks`/`timers` tables (Task 3); `DbConn`.
- Produces: `GateTaskSpec`/`SignalResult` (verbatim from the shared contract); `parse_duration(s: str) -> timedelta` (supports `Nd`/`Nh`/`Nm`); `GateError(Exception)`; `open_task(conn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str` (inserts `human_tasks` at `task_version=1` + schedules the reminder→sla→escalation→auto-park ladder in `timers`, stamping `cas_task_version=1` for Phase 05's CAS-on-fire); `cancel_task(conn, task_id, *, reason, new_status="cancelled") -> None` (cancels open task + its scheduled timers); `cancel_tasks_on_run_advance(conn, run_id, *, reason=..., new_status="cancelled") -> int` (cancels ALL open gate tasks + timers for a run when it advances past their gate — the §7 "cancellation on run advance" effect; the triggering transition is owned by Phase 03/06, which calls this); `bump_task_version(conn, task_id) -> int` (invalidates pending answers per §7 staleness).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/gates/test_open_task.py
from datetime import timedelta

import pytest

from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.duration import parse_duration
from featuregen.gates.tasks import (
    GateError,
    bump_task_version,
    cancel_task,
    cancel_tasks_on_run_advance,
    open_task,
)
from featuregen.identity.build import build_service_identity


def _spec(**kw):
    base = dict(
        gate="DATA_STEWARD",
        required_inputs=("confirmed_contract_ref",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "edit", "reject"),
        run_id="run_1",
        sla="7d",
    )
    base.update(kw)
    return GateTaskSpec(**base)


def _svc():
    return build_service_identity(
        subject="service:intake-agent", role_claims=["workflow"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )


def test_parse_duration():
    assert parse_duration("7d") == timedelta(days=7)
    assert parse_duration("3h") == timedelta(hours=3)
    assert parse_duration("30m") == timedelta(minutes=30)
    with pytest.raises(ValueError):
        parse_duration("7y")


def test_open_task_persists_and_schedules_ladder(db):
    task_id = open_task(db, _spec(), _svc())
    assert task_id.startswith("task_")
    row = db.execute(
        "SELECT task_version, gate, status, run_id FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == (1, "DATA_STEWARD", "open", "run_1")
    kinds = db.execute(
        "SELECT kind FROM timers WHERE task_id=%s ORDER BY fire_at", (task_id,)
    ).fetchall()
    assert {k[0] for k in kinds} == {"reminder", "sla", "escalation", "auto_park"}
    cas = db.execute(
        "SELECT DISTINCT cas_task_version FROM timers WHERE task_id=%s", (task_id,)
    ).fetchall()
    assert cas == [(1,)]


def test_bump_task_version(db):
    task_id = open_task(db, _spec(), _svc())
    assert bump_task_version(db, task_id) == 2
    with pytest.raises(GateError):
        bump_task_version(db, "task_missing")


def test_cancel_task_marks_status_and_cancels_timers(db):
    task_id = open_task(db, _spec(), _svc())
    cancel_task(db, task_id, reason="run advanced past gate")
    assert db.execute(
        "SELECT status FROM human_tasks WHERE task_id=%s", (task_id,)
    ).fetchone()[0] == "cancelled"
    remaining = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert remaining == 0


def test_cancel_tasks_on_run_advance_cancels_all_open_for_run(db):
    # Simulates the Phase-06/03 run-advance hook calling into this phase: every open gate task
    # for the run (and its timers) is cancelled in one shot.
    t1 = open_task(db, _spec(gate="DATA_STEWARD", required_inputs=("a_ref",)), _svc())
    t2 = open_task(
        db,
        _spec(gate="CLARIFICATION", required_inputs=("b_ref",),
              allowed_responses=("answer",)),
        _svc(),
    )
    assert t1 != t2
    n = cancel_tasks_on_run_advance(db, "run_1", reason="run advanced to next stage")
    assert n == 2
    statuses = {
        s[0]
        for s in db.execute(
            "SELECT status FROM human_tasks WHERE run_id='run_1'"
        ).fetchall()
    }
    assert statuses == {"cancelled"}
    sched = db.execute(
        "SELECT count(*) FROM timers WHERE status='scheduled'"
    ).fetchone()[0]
    assert sched == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/gates/test_open_task.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.gates'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/contracts/gates.py
# Verbatim from the shared SP-0 contract; Phase 07 authoritative.
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


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
# src/featuregen/gates/__init__.py
```

```python
# src/featuregen/gates/duration.py
from __future__ import annotations

from datetime import timedelta


def parse_duration(s: str) -> timedelta:
    unit, n = s[-1], int(s[:-1])
    if unit == "d":
        return timedelta(days=n)
    if unit == "h":
        return timedelta(hours=n)
    if unit == "m":
        return timedelta(minutes=n)
    raise ValueError(f"unsupported duration: {s!r}")
```

```python
# src/featuregen/gates/tasks.py
from __future__ import annotations

from datetime import datetime, timezone

from psycopg.types.json import Json

from featuregen.contracts.db import DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.gates.duration import parse_duration
from featuregen.idgen import mint_id


class GateError(Exception):
    """Raised on malformed/unknown human-gate task operations (§7)."""


def _task_aggregate(run_id, feature_id) -> tuple[str, str]:
    if run_id:
        return "run", run_id
    return "feature", feature_id


def open_task(conn: DbConn, spec: GateTaskSpec, actor: IdentityEnvelope) -> str:
    task_id = mint_id("task")
    conn.execute(
        """
        INSERT INTO human_tasks
            (task_id, task_version, run_id, feature_id, gate, required_inputs,
             eligible_assignees, allowed_responses, quorum_required, quorum_of_role,
             delegation_allowed, sla, status)
        VALUES (%s,1,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'open')
        """,
        (
            task_id, spec.run_id, spec.feature_id, spec.gate,
            list(spec.required_inputs), Json(dict(spec.eligible_assignees)),
            list(spec.allowed_responses), spec.quorum_required, spec.quorum_of_role,
            spec.delegation_allowed, spec.sla,
        ),
    )
    if spec.sla:
        base = datetime.now(timezone.utc)
        sla = parse_duration(spec.sla)
        agg, agg_id = _task_aggregate(spec.run_id, spec.feature_id)
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


def bump_task_version(conn: DbConn, task_id: str) -> int:
    row = conn.execute(
        "UPDATE human_tasks SET task_version = task_version + 1, updated_at=now() "
        "WHERE task_id=%s RETURNING task_version",
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    return row[0]


def cancel_task(
    conn: DbConn,
    task_id: str,
    *,
    reason: str,
    new_status: str = "cancelled",
) -> None:
    if new_status not in ("cancelled", "superseded"):
        raise GateError(f"invalid cancel status {new_status!r}")
    conn.execute(
        "UPDATE human_tasks SET status=%s, updated_at=now() WHERE task_id=%s AND status='open'",
        (new_status, task_id),
    )
    conn.execute(
        "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
        (task_id,),
    )


def cancel_tasks_on_run_advance(
    conn: DbConn,
    run_id: str,
    *,
    reason: str = "run advanced past gate",
    new_status: str = "cancelled",
) -> int:
    """Cancel every OPEN gate task (and its scheduled timers) for a run when the run advances
    past their gate — the §7 "cancellation on run advance" clause, made concrete.

    PHASE BOUNDARY: the advancing event / transition is emitted by the Phase 06 lifecycle
    command (or the Phase 03 state machine); that owner CALLS this Phase-07 mechanism inside the
    same §5.1 atomic step transaction. Phase 07 owns the cancellation effect; the trigger is
    upstream. Returns the number of tasks cancelled."""
    open_ids = conn.execute(
        "SELECT task_id FROM human_tasks WHERE run_id=%s AND status='open'",
        (run_id,),
    ).fetchall()
    for (task_id,) in open_ids:
        cancel_task(conn, task_id, reason=reason, new_status=new_status)
    return len(open_ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/gates/test_open_task.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/contracts/gates.py src/featuregen/gates tests/featuregen/gates/test_open_task.py
git commit -m "feat(sp0-07): human-gate open_task with SLA ladder, cancel, version bump"
```

---

## Task 9: Human-gate task — submit_human_signal (eligibility, SoD, quorum_of_role, validated delegation, staleness, conflict, idempotency)

**Files:**
- Modify: `src/featuregen/gates/tasks.py`
- Test: `tests/featuregen/gates/test_submit_signal.py`

**Interfaces:**
- Consumes: `SignalResult` (Task 8); `GateError` (Task 8); `mint_id` (Task 3); `human_tasks`/`human_task_responses`/`task_delegations`/`timers` (Task 3); `global_seq_seq` (Task 3); `gate_sod_reason` (Task 7); `IdentityEnvelope` (Task 1); `DbConn`.
- Produces: `IneligibleResponderError(GateError)`, `ResponseNotAllowedError(GateError)`, `SoDViolationError(GateError)`; `grant_task_delegation(conn, task_id, *, principal: IdentityEnvelope, delegate_subject, granted_by: IdentityEnvelope) -> None` (verifies the PRINCIPAL's eligibility — role + scope + quorum-role — and records a validated grant in `task_delegations`); `submit_human_signal(conn, task_id, *, response, actor, expected_task_version, on_behalf_of=None) -> SignalResult` — matches the shared-contract docstring exactly: **enforces eligibility + SoD (§6.3) + quorum of DISTINCT authorities**. Idempotent by the effective authority `coalesce(on_behalf_of, subject)`; staleness rejected only on `expected_task_version != task_version` (NOT run stream_version, §7); direct eligibility = `eligible_assignees.role` ∈ `role_claims`, (`scope` is None or ∈ `groups`), and (`quorum_of_role` is None or ∈ `role_claims`); delegated answers require `delegation_allowed`, a distinct principal, and a validated `task_delegations` grant (principal eligibility checked at grant time); SoD via `gate_sod_reason` keyed to the effective authority so the direct call path enforces the same SoD as the command path; response ∈ `allowed_responses`; quorum of **distinct authorities** with consistent answers → `answered` (cancels scheduled timers); inconsistent at quorum → `conflict` (cancels the SLA ladder, then schedules exactly one conflict-escalation timer — no double-escalation); late answers on a non-`open` task refused.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/gates/test_submit_signal.py
import pytest

from featuregen.contracts.gates import GateTaskSpec
from featuregen.gates.tasks import (
    IneligibleResponderError,
    ResponseNotAllowedError,
    SoDViolationError,
    bump_task_version,
    grant_task_delegation,
    open_task,
    submit_human_signal,
)
from featuregen.identity.build import build_human_identity, build_service_identity


def _svc():
    return build_service_identity(
        subject="service:intake-agent", role_claims=["workflow"],
        attestation="signed-deploy-id:sp2-intake@1.4.0",
    )


def _open(db, **kw):
    base = dict(
        gate="DATA_STEWARD",
        required_inputs=("confirmed_contract_ref",),
        eligible_assignees={"role": "data_owner", "scope": "core.transactions"},
        allowed_responses=("confirm", "edit", "reject"),
        run_id="run_1",
        sla="7d",
    )
    base.update(kw)
    return open_task(db, GateTaskSpec(**base), _svc())


def _owner(subject):
    return build_human_identity(
        subject=subject, role_claims=["data_owner"], groups=["core.transactions"]
    )


def test_single_quorum_answer_completes_and_cancels_timers(db):
    task_id = _open(db)
    res = submit_human_signal(
        db, task_id, response="confirm", actor=_owner("user:do1"),
        expected_task_version=1,
    )
    assert res.status == "answered"
    assert res.counted is True
    assert res.quorum_met is True
    sched = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert sched == 0


def test_duplicate_subject_is_idempotent(db):
    task_id = _open(db, quorum_required=2)
    submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                        expected_task_version=1)
    again = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                                expected_task_version=1)
    assert again.counted is False
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 1


def test_distinct_quorum_of_two_completes(db):
    task_id = _open(db, quorum_required=2)
    r1 = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                             expected_task_version=1)
    assert r1.quorum_met is False
    r2 = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do2"),
                             expected_task_version=1)
    assert r2.quorum_met is True
    assert r2.status == "answered"


def test_conflicting_quorum_escalates(db):
    task_id = _open(db, quorum_required=2)
    submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                        expected_task_version=1)
    res = submit_human_signal(db, task_id, response="reject", actor=_owner("user:do2"),
                              expected_task_version=1)
    assert res.status == "conflict"
    assert res.quorum_met is False
    escal = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND kind='escalation' "
        "AND idempotency_key LIKE '%%conflict-escalation'",
        (task_id,),
    ).fetchone()[0]
    assert escal == 1
    # the original SLA ladder was cancelled; only the conflict-escalation remains scheduled
    scheduled = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (task_id,)
    ).fetchone()[0]
    assert scheduled == 1


def test_stale_answer_rejected_on_version_change_not_run_advance(db):
    task_id = _open(db)
    bump_task_version(db, task_id)             # required_inputs changed -> task_version=2
    res = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                              expected_task_version=1)
    assert res.counted is False
    n = db.execute(
        "SELECT count(*) FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()[0]
    assert n == 0


def test_ineligible_role_rejected(db):
    task_id = _open(db)
    wrong = build_human_identity(subject="user:x", role_claims=["data_scientist"],
                                 groups=["core.transactions"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=wrong,
                            expected_task_version=1)


def test_wrong_scope_rejected(db):
    task_id = _open(db)
    wrong_scope = build_human_identity(subject="user:y", role_claims=["data_owner"],
                                       groups=["other.table"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=wrong_scope,
                            expected_task_version=1)


def test_response_not_allowed_rejected(db):
    task_id = _open(db)
    with pytest.raises(ResponseNotAllowedError):
        submit_human_signal(db, task_id, response="maybe", actor=_owner("user:do1"),
                            expected_task_version=1)


def test_late_answer_on_cancelled_task_refused(db):
    from featuregen.gates.tasks import cancel_task

    task_id = _open(db)
    cancel_task(db, task_id, reason="run advanced")
    res = submit_human_signal(db, task_id, response="confirm", actor=_owner("user:do1"),
                              expected_task_version=1)
    assert res.counted is False
    assert res.status == "cancelled"


def test_direct_submit_enforces_sod(db):
    # A DIRECT submit_human_signal call (not via execute_command) must still enforce SoD,
    # per the shared-contract docstring. Author of run_1 cannot validate their own run.
    from psycopg.types.json import Json

    db.execute(
        """
        INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id,
                            type, schema_version, table_version, actor, payload, provenance,
                            occurred_at)
        VALUES ('evt_run_1','run','run_1',1,'run_1','RUN_CREATED',1,1,%s,
                '{}'::jsonb,'{}'::jsonb, now())
        """,
        (Json({"subject": "user:author"}),),
    )
    task_id = _open(
        db, gate="INDEPENDENT_VALIDATION",
        eligible_assignees={"role": "validator"},
        allowed_responses=("validate", "reject"),
        required_inputs=("feature_plan_ref",),
    )
    author_as_validator = build_human_identity(
        subject="user:author", role_claims=["validator"]
    )
    with pytest.raises(SoDViolationError):
        submit_human_signal(db, task_id, response="validate",
                            actor=author_as_validator, expected_task_version=1)


def test_quorum_of_role_enforced_distinct_from_eligible_role(db):
    # eligible_assignees.role ("reviewer") is broader than quorum_of_role ("data_owner"):
    # an eligible reviewer who lacks the quorum role does NOT count and is refused; only
    # responders holding the quorum role advance the quorum.
    task_id = _open(
        db, gate="DATA_STEWARD",
        eligible_assignees={"role": "reviewer"},
        quorum_of_role="data_owner",
        quorum_required=2,
        allowed_responses=("confirm", "reject"),
    )
    only_reviewer = build_human_identity(subject="user:r0", role_claims=["reviewer"])
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=only_reviewer,
                            expected_task_version=1)
    a = build_human_identity(subject="user:a", role_claims=["reviewer", "data_owner"])
    b = build_human_identity(subject="user:b", role_claims=["reviewer", "data_owner"])
    r1 = submit_human_signal(db, task_id, response="confirm", actor=a,
                             expected_task_version=1)
    assert r1.quorum_met is False
    r2 = submit_human_signal(db, task_id, response="confirm", actor=b,
                             expected_task_version=1)
    assert r2.quorum_met is True
    assert r2.status == "answered"


def test_delegation_requires_grant_and_validates_principal(db):
    task_id = _open(db)        # DATA_STEWARD: eligible role=data_owner, scope=core.transactions
    delegate = build_human_identity(subject="user:assistant", role_claims=["intern"])

    # 1) no grant -> a delegated answer is refused
    with pytest.raises(IneligibleResponderError):
        submit_human_signal(db, task_id, response="confirm", actor=delegate,
                            expected_task_version=1, on_behalf_of="user:owner")

    # 2) granting for an INELIGIBLE principal is refused (principal eligibility verified here)
    ineligible = build_human_identity(
        subject="user:nobody", role_claims=["intern"], groups=["core.transactions"]
    )
    principal = build_human_identity(
        subject="user:owner", role_claims=["data_owner"], groups=["core.transactions"]
    )
    with pytest.raises(IneligibleResponderError):
        grant_task_delegation(db, task_id, principal=ineligible,
                              delegate_subject="user:assistant", granted_by=principal)

    # 3) valid grant -> the delegate may answer on the principal's behalf; the answer is
    #    attributed to the principal's authority (subject=delegate, on_behalf_of=principal)
    grant_task_delegation(db, task_id, principal=principal,
                          delegate_subject="user:assistant", granted_by=principal)
    res = submit_human_signal(db, task_id, response="confirm", actor=delegate,
                              expected_task_version=1, on_behalf_of="user:owner")
    assert res.counted is True
    assert res.status == "answered"
    assert res.quorum_met is True
    stored = db.execute(
        "SELECT subject, on_behalf_of FROM human_task_responses WHERE task_id=%s", (task_id,)
    ).fetchone()
    assert stored == ("user:assistant", "user:owner")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/gates/test_submit_signal.py -v`
Expected: FAIL with `ImportError: cannot import name 'IneligibleResponderError'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/featuregen/gates/tasks.py`. Two imports are added near the top: the existing `from featuregen.contracts.gates import GateTaskSpec` line becomes `from featuregen.contracts.gates import GateTaskSpec, SignalResult`, and a new top-level `from featuregen.authz.sod import gate_sod_reason` is added (acyclic: `featuregen.authz.sod` never imports `featuregen.gates`).

```python
class IneligibleResponderError(GateError):
    """Responder is not eligible (role/scope/quorum-role/delegation) for this gate (§7)."""


class ResponseNotAllowedError(GateError):
    """Response is not in the task's allowed_responses (§7)."""


class SoDViolationError(GateError):
    """The answer violates segregation-of-duties for the gate (author/validator/approver, §6.3).
    Enforced HERE so a DIRECT submit_human_signal caller gets the same SoD as the command path —
    matching the shared-contract docstring ('Enforces eligibility + SoD (§6.3) + quorum')."""


def grant_task_delegation(
    conn: DbConn,
    task_id: str,
    *,
    principal: IdentityEnvelope,
    delegate_subject: str,
    granted_by: IdentityEnvelope,
) -> None:
    """Record a validated delegation grant (§7 'validly-delegated subjects'). The PRINCIPAL's
    eligibility (role + scope + quorum-role) is verified HERE against the principal's own
    IdentityEnvelope; submit_human_signal then trusts the recorded grant. Without this, a
    delegated answer's principal eligibility and the existence of a real delegation relationship
    would never be checked."""
    row = conn.execute(
        """
        SELECT eligible_assignees, quorum_of_role, delegation_allowed, status
          FROM human_tasks WHERE task_id=%s FOR UPDATE
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    eligible, quorum_of_role, delegation_allowed, status = row
    if status != "open":
        raise GateError(f"task {task_id} is not open (status={status})")
    if not delegation_allowed:
        raise IneligibleResponderError("delegation not allowed for this task")
    if principal.subject == delegate_subject:
        raise IneligibleResponderError("delegation principal must differ from delegate")
    required_role = eligible.get("role")
    required_scope = eligible.get("scope")
    if required_role is not None and required_role not in principal.role_claims:
        raise IneligibleResponderError(f"principal lacks role {required_role!r}")
    if required_scope is not None and required_scope not in principal.groups:
        raise IneligibleResponderError(f"principal lacks scope {required_scope!r}")
    if quorum_of_role is not None and quorum_of_role not in principal.role_claims:
        raise IneligibleResponderError(f"principal lacks quorum role {quorum_of_role!r}")
    conn.execute(
        """
        INSERT INTO task_delegations (task_id, principal, delegate, granted_by)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT (task_id, principal, delegate) DO NOTHING
        """,
        (task_id, principal.subject, delegate_subject, granted_by.subject),
    )


def submit_human_signal(
    conn: DbConn,
    task_id: str,
    *,
    response: str,
    actor: IdentityEnvelope,
    expected_task_version: int,
    on_behalf_of: str | None = None,
) -> SignalResult:
    row = conn.execute(
        """
        SELECT task_version, run_id, feature_id, gate, eligible_assignees,
               allowed_responses, quorum_required, quorum_of_role, delegation_allowed, status
          FROM human_tasks WHERE task_id=%s FOR UPDATE
        """,
        (task_id,),
    ).fetchone()
    if row is None:
        raise GateError(f"unknown task {task_id}")
    (task_version, run_id, feature_id, gate, eligible, allowed_responses,
     quorum_required, quorum_of_role, delegation_allowed, status) = row

    # late answer on a closed task is refused
    if status in ("answered", "conflict", "expired", "cancelled", "superseded"):
        return SignalResult(task_id, status, counted=False, quorum_met=(status == "answered"))

    # staleness keyed to the task's required_inputs/version, NOT the run's stream_version
    if expected_task_version != task_version:
        return SignalResult(task_id, status, counted=False, quorum_met=False)

    if response not in allowed_responses:
        raise ResponseNotAllowedError(f"{response!r} not in {allowed_responses}")

    # ── eligibility / delegation ──────────────────────────────────────────────
    # The EFFECTIVE authority of record is the principal when delegated, else the actor.
    if on_behalf_of is not None:
        if not delegation_allowed:
            raise IneligibleResponderError("delegation not allowed for this task")
        if on_behalf_of == actor.subject:
            raise IneligibleResponderError("delegation principal must differ from delegate")
        grant = conn.execute(
            "SELECT 1 FROM task_delegations WHERE task_id=%s AND principal=%s AND delegate=%s",
            (task_id, on_behalf_of, actor.subject),
        ).fetchone()
        if grant is None:
            raise IneligibleResponderError(
                "no valid delegation grant for this delegate acting for the principal"
            )
        authority = on_behalf_of           # principal eligibility was verified at grant time
    else:
        required_role = eligible.get("role")
        required_scope = eligible.get("scope")
        if required_role is not None and required_role not in actor.role_claims:
            raise IneligibleResponderError(f"actor lacks role {required_role!r}")
        if required_scope is not None and required_scope not in actor.groups:
            raise IneligibleResponderError(f"actor lacks scope {required_scope!r}")
        # quorum_of_role: only responders holding this role count toward the quorum (§7); it may
        # legitimately differ from eligible_assignees.role, so it is checked independently.
        if quorum_of_role is not None and quorum_of_role not in actor.role_claims:
            raise IneligibleResponderError(f"actor lacks quorum role {quorum_of_role!r}")
        authority = actor.subject

    # ── segregation of duties (same predicate as the command-authz path) ──────
    sod_reason = gate_sod_reason(
        conn, gate=gate, subject=authority, run_id=run_id, feature_id=feature_id
    )
    if sod_reason is not None:
        raise SoDViolationError(sod_reason)

    # ── idempotent insert, keyed to the EFFECTIVE authority ───────────────────
    # Distinctness/quorum/SoD key on the authority (coalesce(on_behalf_of, subject)) so a single
    # principal cannot be double-counted via two delegates, nor both self-answer and be delegated.
    # ON CONFLICT additionally no-ops an identical acting subject.
    already = conn.execute(
        """
        SELECT 1 FROM human_task_responses
         WHERE task_id=%s AND coalesce(on_behalf_of, subject)=%s
        """,
        (task_id, authority),
    ).fetchone()
    if already is not None:
        counted = False
    else:
        seq = conn.execute("SELECT nextval('global_seq_seq')").fetchone()[0]
        conn.execute(
            """
            INSERT INTO human_task_responses
                (task_id, subject, response, on_behalf_of, answered_seq)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (task_id, subject) DO NOTHING
            """,
            (task_id, actor.subject, response, on_behalf_of, seq),
        )
        counted = True

    # ── quorum of DISTINCT authorities with consistent answers ────────────────
    rows = conn.execute(
        "SELECT subject, on_behalf_of, response FROM human_task_responses WHERE task_id=%s",
        (task_id,),
    ).fetchall()
    authorities: dict[str, str] = {}
    for subj, obo, resp in rows:
        authorities[obo or subj] = resp

    new_status, quorum_met = status, False
    if len(authorities) >= quorum_required:
        if len(set(authorities.values())) == 1:
            new_status, quorum_met = "answered", True
            conn.execute(
                "UPDATE human_tasks SET status='answered', updated_at=now() WHERE task_id=%s",
                (task_id,),
            )
            conn.execute(
                "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
                (task_id,),
            )
        else:
            new_status = "conflict"
            conn.execute(
                "UPDATE human_tasks SET status='conflict', updated_at=now() WHERE task_id=%s",
                (task_id,),
            )
            # Cancel the SLA ladder first, so a conflicted task does NOT double-escalate; the
            # single conflict-escalation timer below is the only one that should remain scheduled.
            conn.execute(
                "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
                (task_id,),
            )
            agg, agg_id = _task_aggregate(run_id, feature_id)
            conn.execute(
                """
                INSERT INTO timers
                    (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind,
                     fire_at, status, cas_task_version)
                VALUES (%s,%s,%s,%s,%s,'escalation', now(), 'scheduled', %s)
                ON CONFLICT (idempotency_key) DO NOTHING
                """,
                (mint_id("tmr"), f"{task_id}:conflict-escalation", agg, agg_id, task_id,
                 task_version),
            )
    return SignalResult(task_id, new_status, counted=counted, quorum_met=quorum_met)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/gates/test_submit_signal.py tests/featuregen/gates/test_open_task.py -v`
Expected: PASS (17 passed — 12 submit-signal tests + Task 8's 5 tests still green).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/gates/tasks.py tests/featuregen/gates/test_submit_signal.py
git commit -m "feat(sp0-07): submit_human_signal quorum/staleness/conflict/idempotency"
```

---

## Task 10: Break-glass — dual control + mandatory after-the-fact review

**Files:**
- Create: `src/featuregen/security/break_glass.py`
- Test: `tests/featuregen/security/test_break_glass.py`

**Interfaces:**
- Consumes: `record_security_event` (Task 4); `mint_id` (Task 3); `parse_duration` (Task 8); `IdentityEnvelope` (Task 1); `timers`/`security_audit` tables (Task 3); `DbConn`.
- Produces: `BreakGlassError(Exception)`; `invoke_break_glass(conn, *, actor, co_signer, attempted_action, aggregate=None, aggregate_id=None, sla="1d") -> str` (dual-control: two **distinct** `platform-admin`s; records `BREAK_GLASS`/`allowed_break_glass`; opens the review obligation; returns `review_id`); `open_break_glass_review(conn, *, actor, co_signer, attempted_action, aggregate, aggregate_id, sla) -> str` (records `BREAK_GLASS_REVIEW_REQUIRED`/`flagged` + schedules an escalation timer keyed to `review_id`); `sign_off_break_glass_review(conn, review_id, *, reviewer, invoker_subject, co_signer_subject) -> None` (independent reviewer ≠ invoker ≠ co-signer; records `BREAK_GLASS_REVIEW`/`flagged`; cancels the review timer).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/security/test_break_glass.py
import pytest

from featuregen.identity.build import build_human_identity
from featuregen.security.break_glass import (
    BreakGlassError,
    invoke_break_glass,
    sign_off_break_glass_review,
)


def _admin(subject):
    return build_human_identity(subject=subject, role_claims=["platform-admin"])


def test_invoke_requires_two_distinct_admins(db):
    a = _admin("user:adm1")
    with pytest.raises(BreakGlassError):
        invoke_break_glass(db, actor=a, co_signer=a, attempted_action="admin_correct")
    non_admin = build_human_identity(subject="user:b", role_claims=["data_scientist"])
    with pytest.raises(BreakGlassError):
        invoke_break_glass(
            db, actor=a, co_signer=non_admin, attempted_action="admin_correct"
        )


def test_invoke_records_break_glass_and_opens_review(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(
        db, actor=a, co_signer=b, attempted_action="admin_correct",
        aggregate="run", aggregate_id="run_9",
    )
    assert review_id.startswith("bgr_")
    rows = db.execute(
        "SELECT event_type, decision FROM security_audit ORDER BY seq ASC"
    ).fetchall()
    assert ("BREAK_GLASS", "allowed_break_glass") in rows
    assert ("BREAK_GLASS_REVIEW_REQUIRED", "flagged") in rows
    pending = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (review_id,)
    ).fetchone()[0]
    assert pending == 1


def test_review_must_be_independent(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(db, actor=a, co_signer=b, attempted_action="admin_correct")
    with pytest.raises(BreakGlassError):
        sign_off_break_glass_review(
            db, review_id, reviewer=a,
            invoker_subject="user:adm1", co_signer_subject="user:adm2",
        )


def test_review_sign_off_records_and_cancels_timer(db):
    a, b = _admin("user:adm1"), _admin("user:adm2")
    review_id = invoke_break_glass(db, actor=a, co_signer=b, attempted_action="admin_correct")
    reviewer = build_human_identity(subject="user:cmp", role_claims=["compliance"])
    sign_off_break_glass_review(
        db, review_id, reviewer=reviewer,
        invoker_subject="user:adm1", co_signer_subject="user:adm2",
    )
    signed = db.execute(
        "SELECT decision FROM security_audit WHERE event_type='BREAK_GLASS_REVIEW'"
    ).fetchall()
    assert signed == [("flagged",)]
    pending = db.execute(
        "SELECT count(*) FROM timers WHERE task_id=%s AND status='scheduled'", (review_id,)
    ).fetchone()[0]
    assert pending == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/featuregen/security/test_break_glass.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'featuregen.security.break_glass'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/security/break_glass.py
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from psycopg.types.json import Json

from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.gates.duration import parse_duration
from featuregen.idgen import mint_id
from featuregen.security.audit import record_security_event


class BreakGlassError(Exception):
    """Raised when break-glass dual-control or independent-review rules are violated (§6.3)."""


def open_break_glass_review(
    conn: DbConn,
    *,
    actor: IdentityEnvelope,
    co_signer: IdentityEnvelope,
    attempted_action: str,
    aggregate: Optional[str],
    aggregate_id: Optional[str],
    sla: str,
) -> str:
    review_id = mint_id("bgr")
    record_security_event(
        conn,
        event_type="BREAK_GLASS_REVIEW_REQUIRED",
        actor=actor,
        attempted_action=attempted_action,
        decision="flagged",
        reason=f"review_id={review_id}",
        aggregate=aggregate,
        aggregate_id=aggregate_id,
    )
    agg = aggregate or "request"
    agg_id = aggregate_id or review_id
    fire_at = datetime.now(timezone.utc) + parse_duration(sla)
    conn.execute(
        """
        INSERT INTO timers
            (timer_id, idempotency_key, aggregate, aggregate_id, task_id, kind, fire_at,
             status, payload)
        VALUES (%s,%s,%s,%s,%s,'escalation',%s,'scheduled',%s)
        """,
        (
            mint_id("tmr"), f"{review_id}:sla", agg, agg_id, review_id, fire_at,
            Json({"break_glass_review": review_id, "invoker": actor.subject,
                  "co_signer": co_signer.subject}),
        ),
    )
    return review_id


def invoke_break_glass(
    conn: DbConn,
    *,
    actor: IdentityEnvelope,
    co_signer: IdentityEnvelope,
    attempted_action: str,
    aggregate: Optional[str] = None,
    aggregate_id: Optional[str] = None,
    sla: str = "1d",
) -> str:
    if "platform-admin" not in actor.role_claims:
        raise BreakGlassError("break-glass invoker must be platform-admin")
    if "platform-admin" not in co_signer.role_claims:
        raise BreakGlassError("break-glass co-signer must be platform-admin")
    if co_signer.subject == actor.subject:
        raise BreakGlassError("dual control requires two distinct platform-admins")
    record_security_event(
        conn,
        event_type="BREAK_GLASS",
        actor=actor,
        attempted_action=attempted_action,
        decision="allowed_break_glass",
        reason=f"co_signer={co_signer.subject}",
        aggregate=aggregate,
        aggregate_id=aggregate_id,
    )
    return open_break_glass_review(
        conn,
        actor=actor,
        co_signer=co_signer,
        attempted_action=attempted_action,
        aggregate=aggregate,
        aggregate_id=aggregate_id,
        sla=sla,
    )


def sign_off_break_glass_review(
    conn: DbConn,
    review_id: str,
    *,
    reviewer: IdentityEnvelope,
    invoker_subject: str,
    co_signer_subject: str,
) -> None:
    if not any(r in reviewer.role_claims for r in ("compliance", "platform-admin")):
        raise BreakGlassError("break-glass reviewer must be compliance or platform-admin")
    if reviewer.subject in (invoker_subject, co_signer_subject):
        raise BreakGlassError(
            "break-glass review must be independent of invoker and co-signer"
        )
    record_security_event(
        conn,
        event_type="BREAK_GLASS_REVIEW",
        actor=reviewer,
        attempted_action=f"sign_off:{review_id}",
        decision="flagged",
        reason="signed_off",
    )
    conn.execute(
        "UPDATE timers SET status='cancelled' WHERE task_id=%s AND status='scheduled'",
        (review_id,),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/featuregen/security/test_break_glass.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/security/break_glass.py tests/featuregen/security/test_break_glass.py
git commit -m "feat(sp0-07): break-glass dual-control + mandatory independent review"
```

---

## Phase 07 verification (run after all tasks)

- [ ] **Full phase suite green**

Run: `python3 -m pytest tests/sp0 -v`
Expected: PASS — all Phase 07 tests across identity, security stream, authz, SoD, gates, break-glass.

Spec-coverage map (each §-requirement → task):
- §6.1 identity (OIDC humans / attested services, not self-asserted) → Tasks 1–2.
- §6.2 one-vocabulary command authz + denial→security stream (not domain) + restricted audited read (single gate: `validate_identity` + role + self-audit; security-stream-read authz_policy rows deliberately absent to avoid a divergent gate) + tamper-evident chain serialized under concurrency (advisory lock) → Tasks 4, 5, 6.
- §6.3 two-party four-eyes + three-party author≠validator≠approver (enforced on BOTH the command-authz path and the direct `submit_human_signal` path via the shared `gate_sod_reason`) + break-glass dual-control & review → Tasks 7, 9, 10.
- §7 task model: `task_version`, `open_task` + SLA ladder, eligible assignees (role+scope), `allowed_responses`, quorum of distinct **authorities** including `quorum_of_role` enforcement, delegation/`on_behalf_of` validated against a recorded `task_delegations` grant with principal-eligibility verified at grant time, conflict→escalation (SLA ladder cancelled to avoid double-escalation), idempotent by the effective authority, staleness keyed to `required_inputs`/version (not run stream_version), cancellation including run-advance (`cancel_tasks_on_run_advance`; trigger owned by Phase 03/06), timer/answer cancel-on-answer (CAS-on-fire is Phase 05) → Tasks 8, 9.
