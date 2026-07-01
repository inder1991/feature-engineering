# SP-2 — Phase 1 — SP-0 additive extensions (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative). Spec: [SP-2 design §2, §2.1, §5.4, §9.3, §11](../architecture/2026-07-01-sp2-intake-clarification-design.md).

---

This phase ships the **additive, backward-compatible SP-0 surface** that unblocks the rest of SP-2
(design §2.1). Exactly as SP-1 did with its `overlay` aggregate, SP-2 carries the Feature Contract
lifecycle on its **own event-sourced `feature_contract` aggregate** (folded from its stream —
`fold_feature_contract_state`, built in **P2**; **not** an SP-0 run-state, **not** the built-but-unused
`state_machine/engine.py`, **not** `run_workflow_state`). P1 adds that aggregate via an **additive
event-store aggregate-CHECK widening** (`0508_feature_contract_events.sql`, the exact recipe of SP-1's
`0504_overlay_events.sql`), registers the twelve SP-2 event-type schemas, threads the typed mirror id
`feature_contract_id` through the append envelope/store, ships the `append_feature_contract_event(...)`
wrapper (mirroring SP-1's `append_overlay_event`), widens the human-gate CHECK for the
`USE_CASE_ONBOARDING` gate + the `NEEDS_USE_CASE_ONBOARDING` park hold-state
(`0509_use_case_onboarding_gates.sql`, mirroring SP-1's `0505_overlay_gates.sql`), creates the
sensitive/write-once `llm_call` record store (`0510_llm_call_store.sql`; P3 fills the writer), and
introduces `seed_sp2_authz(conn)` (the additive `reject_intent` service authz row + the SP-2
command-capability rows + `register_primary_selected` wiring + the FC read-model checkpoint init).

All DDL changes to existing tables (`events`, `human_tasks`) are **new idempotent `.sql` files** under
`src/featuregen/db/migrations/` using `ALTER TABLE … DROP/ADD CONSTRAINT` and `ADD COLUMN IF NOT EXISTS`.
Stage-2 `.sql` files apply in **lexical** order, so a file that ALTERs an existing table MUST sort
**after** that table's `.sql` and after any prior widening it must preserve. SP-2 therefore uses the
`0508`–`0510` prefixes — **after** SP-0's `0070` gates and SP-1's `0504`–`0507` overlay files (so the
CHECK rebuilds preserve the overlay values SP-1 added), and **before** `0810` privacy. After each `.sql`
is added it is auto-globbed by `featuregen.db.migrations._sql_file_migrations()` and applied by the
session test fixture's one-time `apply_migrations`.

> **Postgres constraint-name note (load-bearing):** an *unnamed inline* single-column `CHECK` is
> auto-named `{table}_{column}_check` by PostgreSQL. SP-0's inline checks are named
> `events_aggregate_check` (on `events.aggregate`) and `human_tasks_gate_check` (on `human_tasks.gate`);
> the id-consistency invariant is the explicitly-named `events_aggregate_id_consistent`. SP-1's `0504`
> already rebuilt `events_aggregate_check` / `events_aggregate_id_consistent` (adding `overlay_fact`) and
> `0505` rebuilt `human_tasks_gate_check` (adding the two overlay gates). The migrations below
> `DROP CONSTRAINT IF EXISTS` those exact names and re-add the **further-widened** constraint under the
> **same** name — **preserving every value SP-0 and SP-1 added** (so re-runs and the overlay suite stay
> green).

> **Test-registry note:** the root harness resets the event-type registry **per test**
> (`tests/conftest.py::_reset_registry`), so — exactly like `tests/featuregen/overlay/conftest.py` — the
> new `tests/featuregen/intake/conftest.py` (Task 1.2) re-registers the SP-2 + phase-06 event schemas for
> every intake test so `append_event` validation passes.

---

### Task 1.1: `events` — `feature_contract` aggregate + `feature_contract_id` threaded through the append path

**Files:**
- Create: `src/featuregen/db/migrations/0508_feature_contract_events.sql`
- Modify: `src/featuregen/contracts/envelopes.py:51-55` (`EventEnvelope`), `:69-74` (`NewEvent`)
- Modify: `src/featuregen/events/store.py:17-28` (`_INSERT`), `:80-97` (params), `:118-137` (returned envelope)
- Modify: `src/featuregen/events/serde.py:79-99` (`row_to_event`)
- Modify: `src/featuregen/aggregates/_append.py:60-98` (`append` kwarg + `NewEvent` build)
- Modify: `src/featuregen/runtime/outbox.py:27-38` (`partition_key_for`)
- Test: `tests/featuregen/intake/test_feature_contract_events.py`

**Interfaces:**
- Consumes: `append(conn, *, aggregate, aggregate_id, type, payload, actor, provenance=None, request_id=None, feature_id=None, run_id=None, overlay_fact_id=None, caused_by=None, expected_version=None) -> EventEnvelope` (`featuregen.aggregates._append`); `event_registry().register_schema(type_name, schema_version, json_schema, owner, *, status="active")`; `build_service_identity(*, subject, role_claims, attestation, ...) -> IdentityEnvelope`; `partition_key_for(event: EventEnvelope) -> str` (`featuregen.runtime.outbox`); `apply_migrations(conn)`.
- Produces: `EventEnvelope.feature_contract_id: str | None`, `NewEvent.feature_contract_id: str | None`; `append(..., feature_contract_id=None)` keyword; an `events` table that accepts `aggregate='feature_contract'` with `aggregate_id == feature_contract_id` and `feature_id IS NULL` (correlation mirrors `request_id`/`run_id` MAY be set — the per-run contract lifecycle, §4.6/§11); `partition_key_for` returns `f"feature_contract:{feature_contract_id}"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_feature_contract_events.py
from __future__ import annotations

import psycopg
import pytest

from featuregen.aggregates._append import append
from featuregen.db.migrations import apply_migrations
from featuregen.events.registry import event_registry
from featuregen.identity.build import build_service_identity
from featuregen.runtime.outbox import partition_key_for

_FC = "fc_test0001"
_RUN = "run_test0001"


def _intake_actor():
    return build_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:intake@1.0.0",
    )


def _register(type_name="FEATURE_CONTRACT_TEST"):
    event_registry().register_schema(type_name, 1, {"type": "object"}, "featuregen-intake")


def test_feature_contract_append_succeeds(conn):
    _register()
    env = append(
        conn,
        aggregate="feature_contract",
        aggregate_id=_FC,
        feature_contract_id=_FC,
        run_id=_RUN,
        request_id="req_1",
        type="FEATURE_CONTRACT_TEST",
        payload={"k": "v"},
        actor=_intake_actor(),
    )
    assert env.aggregate == "feature_contract"
    assert env.feature_contract_id == _FC
    assert env.aggregate_id == _FC
    assert env.run_id == _RUN
    assert env.stream_version == 1
    row = conn.execute(
        "SELECT aggregate, aggregate_id, feature_contract_id, run_id, request_id, feature_id "
        "FROM events WHERE event_id=%s",
        (env.event_id,),
    ).fetchone()
    assert row == ("feature_contract", _FC, _FC, _RUN, "req_1", None)


def test_feature_contract_with_feature_id_is_rejected_by_consistency_check(conn):
    # A contract precedes any feature, so the feature_contract branch mandates feature_id IS NULL.
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "feature_contract_id, feature_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'feature_contract',%s,1,%s,'feat_x','FEATURE_CONTRACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad", _FC, _FC),
        )


def test_feature_contract_id_mismatch_is_rejected_by_consistency_check(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, "
            "feature_contract_id, type, schema_version, table_version, "
            "actor, payload, provenance, occurred_at) "
            "VALUES (%s,'feature_contract','fc_A',1,'fc_B','FEATURE_CONTRACT_TEST',1,1,"
            "'{}'::jsonb,'{}'::jsonb,'{}'::jsonb, now())",
            ("evt_bad2",),
        )


def test_request_and_overlay_appends_still_pass(conn):
    _register("REQ_TEST")
    env = append(
        conn,
        aggregate="request",
        aggregate_id="req_2",
        request_id="req_2",
        type="REQ_TEST",
        payload={},
        actor=_intake_actor(),
    )
    assert env.aggregate == "request"
    assert env.feature_contract_id is None


def test_partition_key_for_feature_contract(conn):
    _register()
    env = append(
        conn,
        aggregate="feature_contract",
        aggregate_id=_FC,
        feature_contract_id=_FC,
        run_id=_RUN,
        type="FEATURE_CONTRACT_TEST",
        payload={},
        actor=_intake_actor(),
    )
    assert partition_key_for(env) == f"feature_contract:{_FC}"


def test_feature_contract_events_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    col = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='events' AND column_name='feature_contract_id'"
    ).fetchone()
    assert col is not None
    chk = conn.execute(
        "SELECT 1 FROM pg_constraint WHERE conname='events_aggregate_id_consistent'"
    ).fetchone()
    assert chk is not None
    # Regression: SP-1's overlay_fact aggregate value must survive the rebuild.
    agg = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='events_aggregate_check'"
    ).fetchone()[0]
    assert "overlay_fact" in agg and "feature_contract" in agg
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/intake/test_feature_contract_events.py -v`
Expected: FAIL — `append()` rejects the unexpected `feature_contract_id` keyword (TypeError) / the `aggregate='feature_contract'` CHECK violation / no `feature_contract_id` column.

- [ ] **Step 3: Create the `0508_feature_contract_events.sql` migration**

```sql
-- src/featuregen/db/migrations/0508_feature_contract_events.sql
-- SP-2 Phase 1 (design §2, §2.1 #1): additive, backward-compatible extension of SP-0's `events`
-- table to host SP-2's own `feature_contract` aggregate — the Feature Contract lifecycle, FOLDED
-- from its stream (fold_feature_contract_state, §4.6/§11), never a projection. Same recipe as
-- SP-1's 0504_overlay_events.sql. Idempotent: re-running is a clean no-op. Adds an allowed
-- aggregate value; rewrites no existing row.

-- 1. Widen the aggregate CHECK to admit 'feature_contract'. This DROP/ADD PRESERVES the
--    'overlay_fact' value SP-1's 0504 added (0508 sorts after 0504, so it rebuilds on top of it).
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_check;
ALTER TABLE events ADD CONSTRAINT events_aggregate_check
    CHECK (aggregate IN ('request','feature','run','overlay_fact','feature_contract'));

-- 2. Typed mirror column for the feature_contract aggregate (aggregate_id == feature_contract_id).
ALTER TABLE events ADD COLUMN IF NOT EXISTS feature_contract_id text;

-- 3. Rebuild the id-consistency invariant with an explicit feature_contract branch. The contract
--    lifecycle is per-run, so a feature_contract event MAY carry its correlation mirrors
--    request_id/run_id (consumed by the get_contract read model, §13); feature_id is ALWAYS NULL
--    (a contract precedes any feature). The request/feature/run/overlay_fact branches are
--    preserved verbatim from SP-0 + SP-1's 0504.
ALTER TABLE events DROP CONSTRAINT IF EXISTS events_aggregate_id_consistent;
ALTER TABLE events ADD CONSTRAINT events_aggregate_id_consistent CHECK (
    (aggregate = 'request' AND aggregate_id = request_id) OR
    (aggregate = 'feature' AND aggregate_id = feature_id) OR
    (aggregate = 'run'     AND aggregate_id = run_id)     OR
    (aggregate = 'overlay_fact' AND aggregate_id = overlay_fact_id
        AND request_id IS NULL AND feature_id IS NULL AND run_id IS NULL) OR
    (aggregate = 'feature_contract' AND aggregate_id = feature_contract_id
        AND feature_id IS NULL)
);

-- 4. Partial index for per-contract lookups (mirrors events_overlay_fact_idx).
CREATE INDEX IF NOT EXISTS events_feature_contract_idx
    ON events (feature_contract_id) WHERE feature_contract_id IS NOT NULL;
```

- [ ] **Step 4: Thread `feature_contract_id` through `EventEnvelope` and `NewEvent`**

In `src/featuregen/contracts/envelopes.py`, add the field to `EventEnvelope` (after `overlay_fact_id`):

```python
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    feature_contract_id: str | None = None
    caused_by: str | None = None
```

And to `NewEvent` (after `overlay_fact_id`):

```python
    request_id: str | None = None
    feature_id: str | None = None
    run_id: str | None = None
    overlay_fact_id: str | None = None
    feature_contract_id: str | None = None
    caused_by: str | None = None
    occurred_at: datetime | None = None
```

- [ ] **Step 5: Thread `feature_contract_id` through `events/store.py`**

Update `_INSERT` to list the new column (add `feature_contract_id` to both the column list and the
VALUES list, after `overlay_fact_id`):

```python
_INSERT = """
INSERT INTO events (
    event_id, aggregate, aggregate_id, stream_version,
    request_id, feature_id, run_id, overlay_fact_id, feature_contract_id,
    type, schema_version, table_version,
    actor, payload, provenance, caused_by, occurred_at
) VALUES (
    %(event_id)s, %(aggregate)s, %(aggregate_id)s, %(stream_version)s,
    %(request_id)s, %(feature_id)s, %(run_id)s, %(overlay_fact_id)s, %(feature_contract_id)s,
    %(type)s, %(schema_version)s, %(table_version)s,
    %(actor)s, %(payload)s, %(provenance)s, %(caused_by)s, %(occurred_at)s
)
RETURNING global_seq, recorded_at
"""
```

Add the param (in the `params = {...}` dict, after `"overlay_fact_id"`):

```python
        "overlay_fact_id": new_event.overlay_fact_id,
        "feature_contract_id": new_event.feature_contract_id,
```

Add the field to the returned `EventEnvelope(...)` (after `overlay_fact_id=`):

```python
        overlay_fact_id=new_event.overlay_fact_id,
        feature_contract_id=new_event.feature_contract_id,
        caused_by=new_event.caused_by,
    )
```

- [ ] **Step 6: Thread `feature_contract_id` through `events/serde.py::row_to_event`**

Add the field to the constructed `EventEnvelope` (after `overlay_fact_id=row["overlay_fact_id"]`):

```python
        overlay_fact_id=row["overlay_fact_id"],
        feature_contract_id=row["feature_contract_id"],
        caused_by=row["caused_by"],
    )
```

- [ ] **Step 7: Add the `feature_contract_id` kwarg to `aggregates/_append.py::append`**

Add the keyword to the signature (after `overlay_fact_id`) and pass it into the `NewEvent`:

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
    feature_contract_id: str | None = None,
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
        feature_contract_id=feature_contract_id,
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

- [ ] **Step 8: Add the `feature_contract` branch to `runtime/outbox.py::partition_key_for`**

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
    if event.aggregate == "feature_contract":
        return f"feature_contract:{event.feature_contract_id or event.aggregate_id}"
    raise ValueError(f"unknown aggregate {event.aggregate!r}")
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/intake/test_feature_contract_events.py -v`
Expected: PASS (6 passed).

- [ ] **Step 10: Commit**

```bash
git add src/featuregen/db/migrations/0508_feature_contract_events.sql \
        src/featuregen/contracts/envelopes.py src/featuregen/events/store.py \
        src/featuregen/events/serde.py src/featuregen/aggregates/_append.py \
        src/featuregen/runtime/outbox.py tests/featuregen/intake/test_feature_contract_events.py
git commit -m "feat(intake): add feature_contract aggregate + feature_contract_id to events append path"
```

---

### Task 1.2: SP-2 event-type schemas (`intake/events.py` + `register_sp2_event_types`) + intake test harness

**Files:**
- Create: `src/featuregen/intake/__init__.py`
- Create: `src/featuregen/intake/events.py`
- Create: `tests/featuregen/intake/__init__.py`
- Create: `tests/featuregen/intake/conftest.py`
- Test: `tests/featuregen/intake/test_sp2_events.py`

**Interfaces:**
- Consumes: `event_registry()` + `EventSchemaRegistry.register_schema(type_name, schema_version, json_schema, owner, *, status="active")` (`featuregen.events.registry`); `register_phase06_event_types(registry)` (`featuregen.aggregates.events`) — RUN_*/lifecycle schemas the intake suite reuses.
- Produces: the twelve SP-2 event-type constants (`INTENT_SUBMITTED`, `DRAFT_CONTRACT_PRODUCED`, `CONTRACT_CRITIQUED`, `FIELD_AUTO_RESOLVED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED`, `CONTRACT_REFINED`, `MINIMUM_CONTRACT_VALIDATED`, `CONTRACT_CONFIRMED`, `USE_CASE_ONBOARDING_REQUESTED`, `INTENT_REJECTED`, `LLM_CALL_RECORDED`); the gate/park constants `USE_CASE_ONBOARDING_GATE = "USE_CASE_ONBOARDING"` and `NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"`; `SP2_EVENT_SCHEMAS: dict[str, dict]`, `SP2_EVENT_SCHEMA_VERSION = 1`, `SP2_OWNER = "featuregen-intake"`; `register_sp2_event_types(registry) -> None` — consumed by P4–P8 handlers (every FC event MUST be registered before append) and P9's `register_sp2`. Also creates the **R18** shared intake test scaffolding — `tests/featuregen/intake/__init__.py` + the ONE `tests/featuregen/intake/conftest.py` (autouse event-type registration + the four collaborator-seam fixtures `llm_client`/`intent_redactor`/`candidate_generator`/`intake_catalog`) — that P2/P4/P5/P7 later MODIFY/merge, never re-Create.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/intake/test_sp2_events.py
from __future__ import annotations

import pytest

from featuregen.contracts import SchemaValidationError
from featuregen.events.registry import event_registry
from featuregen.intake.events import (
    NEEDS_USE_CASE_ONBOARDING,
    SP2_EVENT_SCHEMAS,
    SP2_EVENT_SCHEMA_VERSION,
    USE_CASE_ONBOARDING_GATE,
    INTENT_REJECTED,
    INTENT_SUBMITTED,
    LLM_CALL_RECORDED,
    register_sp2_event_types,
)

_ALL_TWELVE = {
    "INTENT_SUBMITTED",
    "DRAFT_CONTRACT_PRODUCED",
    "CONTRACT_CRITIQUED",
    "FIELD_AUTO_RESOLVED",
    "CLARIFICATION_REQUESTED",
    "CLARIFICATION_ANSWERED",
    "CONTRACT_REFINED",
    "MINIMUM_CONTRACT_VALIDATED",
    "CONTRACT_CONFIRMED",
    "USE_CASE_ONBOARDING_REQUESTED",
    "INTENT_REJECTED",
    "LLM_CALL_RECORDED",
}


def test_all_twelve_fc_event_types_present():
    assert set(SP2_EVENT_SCHEMAS) == _ALL_TWELVE
    assert INTENT_SUBMITTED == "INTENT_SUBMITTED"
    assert LLM_CALL_RECORDED == "LLM_CALL_RECORDED"


def test_gate_and_park_constants():
    assert USE_CASE_ONBOARDING_GATE == "USE_CASE_ONBOARDING"
    assert NEEDS_USE_CASE_ONBOARDING == "NEEDS_USE_CASE_ONBOARDING"


def test_register_makes_every_type_writable():
    reg = event_registry()
    register_sp2_event_types(reg)
    for type_name in _ALL_TWELVE:
        reg.assert_writable(type_name, SP2_EVENT_SCHEMA_VERSION)  # active → no raise
        assert reg.max_active_versions()[type_name] == SP2_EVENT_SCHEMA_VERSION


def test_intent_submitted_schema_validates_required_and_enums():
    reg = event_registry()
    register_sp2_event_types(reg)
    # R2: the payload carries only SEMANTIC fields — id fields (feature_contract_id/run_id/request_id)
    # ride typed columns and are NOT in required[].
    good = {
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
    }
    reg.validate(INTENT_SUBMITTED, 1, good)  # no raise (no id fields needed)
    with pytest.raises(SchemaValidationError):
        reg.validate(INTENT_SUBMITTED, 1, {**good, "intake_mode": "guesswork"})  # closed enum
    with pytest.raises(SchemaValidationError):
        bad = dict(good)
        del bad["raw_input_ref"]
        reg.validate(INTENT_SUBMITTED, 1, bad)  # missing a SEMANTIC required field
    # R2: an id field is never required — a payload carrying one still validates (additive).
    reg.validate(INTENT_SUBMITTED, 1, {**good, "run_id": "run_1"})


def test_intent_rejected_classification_is_a_closed_enum():
    reg = event_registry()
    register_sp2_event_types(reg)
    base = {
        "feature_contract_id": "fc_1",
        "run_id": "run_1",
        "classification": "OUT_OF_SCOPE",
        "catalog_version": "bdc@2026-06-01",
    }
    reg.validate(INTENT_REJECTED, 1, base)  # no raise
    reg.validate(INTENT_REJECTED, 1, {**base, "classification": "PROHIBITED_DATA_CLASS"})
    with pytest.raises(SchemaValidationError):
        reg.validate(INTENT_REJECTED, 1, {**base, "classification": "MEH"})


def test_event_schemas_require_only_semantic_fields():
    # R2: no id field (feature_contract_id / run_id / request_id) appears in ANY required[].
    id_fields = {"feature_contract_id", "run_id", "request_id"}
    for type_name, schema in SP2_EVENT_SCHEMAS.items():
        assert id_fields.isdisjoint(schema["required"]), type_name
    assert SP2_EVENT_SCHEMAS[LLM_CALL_RECORDED]["required"] == ["llm_call_ref"]
    assert SP2_EVENT_SCHEMAS[INTENT_SUBMITTED]["required"] == [
        "intake_mode",
        "raw_input_ref",
        "raw_input_classification",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_sp2_events.py -v`
Expected: FAIL — `featuregen.intake` package / `events` module does not exist (ImportError).

- [ ] **Step 3: Create the `intake` package marker + the intake test-package marker (R18 scaffolding)**

```python
# src/featuregen/intake/__init__.py
"""SP-2 — Intake, Clarification and Human Gate #1 (a thin domain layer over SP-0)."""
```

```python
# tests/featuregen/intake/__init__.py
# R18: the intake test-package marker — CREATED by P1 (single-create); later phases MODIFY, never Create.
```

- [ ] **Step 4: Create `intake/events.py`**

```python
# src/featuregen/intake/events.py
"""SP-2 event-type constants + JSON schemas for the `feature_contract` aggregate (design §2.1 #2,
§9.3, §11). These are the contract-lifecycle events SP-2 emits on its own `feature_contract`
aggregate (admitted by the 0508 aggregate-CHECK widening); the terminal RUN outcomes
(RUN_REJECTED/RUN_WITHDRAWN/RUN_PARKED) stay on SP-0's `run` aggregate via its existing lifecycle
commands. Every schema is STRUCTURAL-ONLY and carries NO inline PII / no data values — bodies are
referenced (raw_input_ref / *_doc_id / *_ref), never inlined (append's assert_no_inline_pii, §9.4).
Schemas are additive-friendly (additionalProperties: true) so P4–P8 handlers may enrich payloads
without a schema bump; the registry's backward-compat rule treats added optional fields as
compatible."""

from __future__ import annotations

from collections.abc import Mapping

# ---- the twelve feature_contract-aggregate event types (§2.1 #2) ----
INTENT_SUBMITTED = "INTENT_SUBMITTED"
DRAFT_CONTRACT_PRODUCED = "DRAFT_CONTRACT_PRODUCED"
CONTRACT_CRITIQUED = "CONTRACT_CRITIQUED"
FIELD_AUTO_RESOLVED = "FIELD_AUTO_RESOLVED"
CLARIFICATION_REQUESTED = "CLARIFICATION_REQUESTED"
CLARIFICATION_ANSWERED = "CLARIFICATION_ANSWERED"
CONTRACT_REFINED = "CONTRACT_REFINED"
MINIMUM_CONTRACT_VALIDATED = "MINIMUM_CONTRACT_VALIDATED"
CONTRACT_CONFIRMED = "CONTRACT_CONFIRMED"
USE_CASE_ONBOARDING_REQUESTED = "USE_CASE_ONBOARDING_REQUESTED"
INTENT_REJECTED = "INTENT_REJECTED"
LLM_CALL_RECORDED = "LLM_CALL_RECORDED"

# ---- additive gate value + park hold-state SP-2 registers (§2.1 #6, §5.4, §11) ----
# The gate value is admitted by 0509's human_tasks_gate CHECK widening; the park hold-state is
# carried in SP-0's RUN_PARKED.waiting_on_fact (its base payload is unconstrained, run_lifecycle.py),
# so it needs no DB CHECK — this is the canonical constant handlers pass.
USE_CASE_ONBOARDING_GATE = "USE_CASE_ONBOARDING"
NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"

SP2_EVENT_SCHEMA_VERSION = 1
SP2_OWNER = "featuregen-intake"

# ---- reusable structural fragments (closed enums mirror the content-schema vocabularies, §4.0) ----
_ID = {"type": "string", "minLength": 1}
_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_INT = {"type": "integer"}
_BOOL = {"type": "boolean"}
_ARR = {"type": "array"}
_INTAKE_MODE = {"enum": ["definition", "hypothesis"]}
_RAW_CLASS = {"enum": ["clean", "contains_pii", "unscanned"]}
_REJECT_CLASS = {"enum": ["OUT_OF_SCOPE", "PROHIBITED_DATA_CLASS"]}
_ROUTED_TO = {"enum": ["human", "auto"]}


def _evt(properties: Mapping[str, dict], required: list[str]) -> dict:
    """Structural object schema, additive-friendly (additionalProperties: true) — see module note."""
    return {
        "type": "object",
        "properties": dict(properties),
        "required": list(required),
        "additionalProperties": True,
    }


# R2 — id fields (feature_contract_id / run_id / request_id) ride typed event columns and appear in
# NO required[]; each schema requires only its SEMANTIC fields (LLM_CALL_RECORDED -> ["llm_call_ref"]).
# Emitters put NO id fields in the payload (mirrors SP-1 overlay events not requiring overlay_fact_id).
SP2_EVENT_SCHEMAS: dict[str, dict] = {
    INTENT_SUBMITTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "request_id": _ID,
            "intake_mode": _INTAKE_MODE,
            "raw_input_ref": _STR,  # SP-0 encrypted blob_/doc_ ref — raw text is NEVER inline
            "raw_input_classification": _RAW_CLASS,
            "catalog_version": _NSTR,
        },
        ["intake_mode", "raw_input_ref", "raw_input_classification"],
    ),
    DRAFT_CONTRACT_PRODUCED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _ID,
            "assumption_ledger_ref": _NSTR,
            "candidate_doc_ids": _ARR,      # hypothesis mode: 1–3 candidate docs (§7)
            "open_fields": _ARR,
            "catalog_version": _NSTR,
        },
        ["draft_doc_id"],
    ),
    CONTRACT_CRITIQUED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "critique_call_ref": _NSTR,     # llm_call_ref of the CONTRACT_REVIEW critique (§6.4)
            "findings": _ARR,
        },
        [],
    ),
    FIELD_AUTO_RESOLVED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "field": _STR,                  # a semantic field path, e.g. "entity_grain"
            "value": {},                    # the chosen SEMANTIC default (never a data value, §9.4)
            "source": {"enum": ["llm", "default", "catalog"]},
            "ambiguity": {"type": "number"},
            "confidence": {"type": "number"},
        },
        ["field"],
    ),
    CLARIFICATION_REQUESTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _ID,
            "field": _STR,
            "blocks_progress": _BOOL,
            "routed_to": _ROUTED_TO,
        },
        ["task_id", "field"],
    ),
    CLARIFICATION_ANSWERED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _ID,
            "field": _NSTR,
            "renormalize": _BOOL,           # thin domain shadow: the re-normalization trigger (§2.1 #2)
        },
        ["task_id"],
    ),
    CONTRACT_REFINED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _ID,
            "supersedes": _ARR,
            "iteration": _INT,
        },
        ["draft_doc_id"],
    ),
    MINIMUM_CONTRACT_VALIDATED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "draft_doc_id": _NSTR,          # the final Draft the MCV checklist passed against
            "checks": {},                   # the deterministic MCV checklist result (§6.7)
        },
        [],
    ),
    CONTRACT_CONFIRMED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "confirmed_doc_id": _ID,        # the frozen CONFIRMED_CONTRACT document
            "confirmed_by": _NSTR,          # the authenticated requester subject (principal id)
            "requires_independent_validation": _BOOL,
            "selected_candidate": _NSTR,    # hypothesis mode: chosen candidate doc_id
        },
        ["confirmed_doc_id"],
    ),
    USE_CASE_ONBOARDING_REQUESTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "task_id": _NSTR,               # the governance onboarding gate task (§5.4)
            "catalog_version": _STR,
            "proposed_use_case": _NSTR,
        },
        ["catalog_version"],
    ),
    INTENT_REJECTED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "classification": _REJECT_CLASS,  # OUT_OF_SCOPE | PROHIBITED_DATA_CLASS (§5.4, §8.4)
            "reason": _NSTR,
            "catalog_version": _STR,          # stamped on every outcome (§4.5 completeness (c))
            "matched_class": _NSTR,           # the blocked_data_classes member, when prohibited
        },
        ["classification", "catalog_version"],
    ),
    LLM_CALL_RECORDED: _evt(
        {
            "feature_contract_id": _ID,
            "run_id": _ID,
            "llm_call_ref": _ID,            # → the sensitive llm_call record store (§9.3)
            "task": _STR,                   # structure_intent | contract_review | generate_candidates | renormalize
            "status": _NSTR,                # ok | repaired | retried | failed_into_clarification (§9.2)
        },
        ["llm_call_ref"],
    ),
}


def register_sp2_event_types(registry) -> None:
    """Register the twelve SP-2 feature_contract event schemas (schema_version=1) so append_event
    validation passes (Global Constraint: every new event type MUST be registered before any append).
    Idempotent — register_schema is an upsert; safe to call repeatedly (P4–P8, P9 register_sp2)."""
    for type_name, schema in SP2_EVENT_SCHEMAS.items():
        registry.register_schema(
            type_name, SP2_EVENT_SCHEMA_VERSION, schema, owner=SP2_OWNER, status="active"
        )
```

- [ ] **Step 5: Create the intake test harness (`conftest.py`)**

```python
# tests/featuregen/intake/conftest.py
"""The ONE shared intake test harness (R18) — CREATED by P1; P2/P4/P5/P7 MODIFY/merge (never
`Create`). Holds the autouse event-type registration + the four collaborator-seam fixtures
(`llm_client`, `intent_redactor`, `candidate_generator`, `intake_catalog`). Each seam fixture imports
its module + double LAZILY (inside the body) so P1's own suite — which never requests them — does not
depend on the not-yet-built P2/P3/P6 modules; the owning phase fleshes out the double it registers."""
import pytest

from featuregen.aggregates.events import register_phase06_event_types
from featuregen.events.registry import event_registry
from featuregen.intake.events import register_sp2_event_types


@pytest.fixture(autouse=True)
def _register_intake_event_types():
    # The event registry is reset PER TEST by the root harness (tests/conftest.py::_reset_registry),
    # so — exactly like tests/featuregen/overlay/conftest.py — re-register the SP-2 event schemas AND
    # the phase-06 RUN_*/lifecycle schemas for every intake test so append_event validation passes.
    register_phase06_event_types(event_registry())
    register_sp2_event_types(event_registry())


@pytest.fixture
def llm_client():
    """R10 llm seam — register a deterministic FakeLLM (P3 owns FakeLLM + the R19 script form)."""
    from featuregen.intake.llm import FakeLLM, register_llm_client

    client = FakeLLM(script={})
    register_llm_client(client)
    return client


@pytest.fixture
def intent_redactor():
    """R10 redactor seam — register the DefaultIntentRedactor (P3 owns it)."""
    from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor

    redactor = DefaultIntentRedactor()
    register_intent_redactor(redactor)
    return redactor


@pytest.fixture
def candidate_generator():
    """R10 candidate seam — register the StubCandidateGenerator (P6 owns it)."""
    from featuregen.intake.candidates import StubCandidateGenerator, register_candidate_generator

    generator = StubCandidateGenerator()
    register_candidate_generator(generator)
    return generator


@pytest.fixture
def intake_catalog():
    """R10 catalog seam — register a BankingDomainCatalog from the seed (P2 owns the loader)."""
    from featuregen.intake.catalog import load_banking_catalog_from_seed, register_intake_catalog

    catalog = load_banking_catalog_from_seed({})
    register_intake_catalog(catalog)
    return catalog
```

- [ ] **Step 6: Run the test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_sp2_events.py -v`
Expected: PASS (6 passed).

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/intake/__init__.py src/featuregen/intake/events.py \
        tests/featuregen/intake/__init__.py tests/featuregen/intake/conftest.py \
        tests/featuregen/intake/test_sp2_events.py
git commit -m "feat(intake): register the twelve SP-2 feature_contract event-type schemas + shared harness"
```

---

### Task 1.3: `append_feature_contract_event(...)` wrapper + `load_feature_contract(...)` (`intake/store.py`)

**Files:**
- Create: `src/featuregen/intake/store.py`
- Test: `tests/featuregen/intake/test_feature_contract_store.py`

**Interfaces:**
- Consumes: `append(conn, *, aggregate, aggregate_id, type, payload, actor, provenance=None, request_id=None, feature_id=None, run_id=None, overlay_fact_id=None, feature_contract_id=None, caused_by=None, expected_version=None) -> EventEnvelope` (Task 1.1); `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None) -> list[EventEnvelope]` (`featuregen.events.store`); `register_sp2_event_types(registry)` (Task 1.2); `ConcurrencyError` (`featuregen.contracts`).
- Produces: `append_feature_contract_event(conn, *, run_id: str, type: str, payload: Mapping, actor: IdentityEnvelope, request_id: str | None = None, provenance: ProvenanceEnvelope | None = None, expected_version: int | None = None, caused_by: str | None = None) -> EventEnvelope` (the SP-2 append seam — mirrors SP-1's `append_overlay_event`; **R1** sets `aggregate="feature_contract"` and `aggregate_id == feature_contract_id == run_id` (one contract per run), new streams open at `expected_version=0`); `load_feature_contract(conn, run_id: str) -> list[EventEnvelope]`. Consumed by P3(`call_llm`)/P4–P8 handlers + `fold_feature_contract_state` (P8).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/intake/test_feature_contract_store.py
from __future__ import annotations

import pytest

from featuregen.contracts import ConcurrencyError, SchemaValidationError
from featuregen.identity.build import build_service_identity
from featuregen.intake.events import CONTRACT_REFINED, INTENT_SUBMITTED
from featuregen.intake.store import append_feature_contract_event, load_feature_contract

_RUN = "run_store01"  # R1: feature_contract_id == run_id (one contract per run)


def _intake_actor():
    return build_service_identity(
        subject="service:intake-agent",
        role_claims=["intake-agent"],
        attestation="signed-deploy-id:intake@1.0.0",
    )


def _intent_payload():
    # R2: emitters put NO id fields in the payload — only the SEMANTIC fields.
    return {
        "intake_mode": "definition",
        "raw_input_ref": "blob_01H",
        "raw_input_classification": "clean",
    }


def test_open_stream_appends_at_version_0_and_carries_correlation(conn):
    env = append_feature_contract_event(
        conn,
        run_id=_RUN,
        request_id="req_store01",
        type=INTENT_SUBMITTED,
        payload=_intent_payload(),
        actor=_intake_actor(),
        expected_version=0,
    )
    assert env.aggregate == "feature_contract"
    # R1: the seam sets feature_contract_id == aggregate_id == run_id.
    assert env.feature_contract_id == _RUN
    assert env.aggregate_id == _RUN
    assert env.run_id == _RUN
    assert env.request_id == "req_store01"
    assert env.stream_version == 1


def test_load_feature_contract_returns_the_stream_in_order(conn):
    append_feature_contract_event(
        conn, run_id=_RUN, request_id="req_store01",
        type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
        expected_version=0,
    )
    append_feature_contract_event(
        conn, run_id=_RUN,
        type=CONTRACT_REFINED,
        payload={"draft_doc_id": "doc_v2"},
        actor=_intake_actor(), expected_version=1,
    )
    stream = load_feature_contract(conn, _RUN)
    assert [e.type for e in stream] == [INTENT_SUBMITTED, CONTRACT_REFINED]
    assert [e.stream_version for e in stream] == [1, 2]


def test_stale_expected_version_raises_concurrency(conn):
    append_feature_contract_event(
        conn, run_id=_RUN, request_id="req_store01",
        type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
        expected_version=0,
    )
    with pytest.raises(ConcurrencyError):
        append_feature_contract_event(
            conn, run_id=_RUN,
            type=CONTRACT_REFINED,
            payload={"draft_doc_id": "doc_v2"},
            actor=_intake_actor(), expected_version=0,  # stale — stream is already at 1
        )


def test_unregistered_type_fails_closed(conn):
    # Prove registration is load-bearing: an unknown FC event type is refused before any INSERT.
    from featuregen.events.registry import reset_event_registry
    reset_event_registry()  # wipe the autouse registrations for this one assertion
    with pytest.raises(SchemaValidationError):
        append_feature_contract_event(
            conn, run_id=_RUN, request_id="req_store01",
            type=INTENT_SUBMITTED, payload=_intent_payload(), actor=_intake_actor(),
            expected_version=0,
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_feature_contract_store.py -v`
Expected: FAIL — `featuregen.intake.store` does not exist (ImportError).

- [ ] **Step 3: Create `intake/store.py`**

```python
# src/featuregen/intake/store.py
"""The SP-2 append seam for the `feature_contract` aggregate (mirrors SP-1's overlay/store.py).
Never INSERTs into `events` directly (Global Constraint) — it rides SP-0's OCC/provenance/global_seq
helper. `aggregate_id == feature_contract_id == run_id` (R1 — one contract per run); request_id
rides as a correlation mirror (consumed by the get_contract read model, §13). New streams open at
expected_version=0; every FC event type MUST be registered (register_sp2_event_types) first."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from featuregen.aggregates._append import append
from featuregen.contracts import DbConn, EventEnvelope, IdentityEnvelope, ProvenanceEnvelope
from featuregen.events.store import load_stream


def append_feature_contract_event(
    conn: DbConn,
    *,
    run_id: str,
    type: str,
    payload: Mapping[str, Any],
    actor: IdentityEnvelope,
    request_id: str | None = None,
    provenance: ProvenanceEnvelope | None = None,
    expected_version: int | None = None,
    caused_by: str | None = None,
) -> EventEnvelope:
    """Append one feature_contract event via the SP-0 OCC helper (R1 — mirrors SP-1's
    append_overlay_event). One contract per run, so the seam sets
    `aggregate_id == feature_contract_id == run_id`; `run_id`/`request_id` also ride as correlation
    mirrors (consumed by the get_contract read model, §13). Raises ConcurrencyError if the stream is
    not exactly at `expected_version`, and SchemaValidationError if `type` is unregistered or the
    payload fails its schema (fail-closed, before any INSERT)."""
    return append(
        conn,
        aggregate="feature_contract",
        aggregate_id=run_id,
        feature_contract_id=run_id,
        type=type,
        payload=payload,
        actor=actor,
        run_id=run_id,
        request_id=request_id,
        provenance=provenance,
        expected_version=expected_version,
        caused_by=caused_by,
    )


def load_feature_contract(conn: DbConn, run_id: str) -> list[EventEnvelope]:
    """Load the full feature_contract event stream (stream_version ASC) — the input to
    fold_feature_contract_state (P8). `feature_contract_id == run_id` (R1)."""
    return load_stream(conn, "feature_contract", run_id)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_feature_contract_store.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/store.py \
        tests/featuregen/intake/test_feature_contract_store.py
git commit -m "feat(intake): add append_feature_contract_event + load_feature_contract seam"
```

---

### Task 1.4: `human_tasks` — the `USE_CASE_ONBOARDING` gate value + the `NEEDS_USE_CASE_ONBOARDING` park hold-state

**Files:**
- Create: `src/featuregen/db/migrations/0509_use_case_onboarding_gates.sql`
- Test: `tests/featuregen/intake/test_use_case_onboarding_gates.py`

**Interfaces:**
- Consumes: `open_task(conn, spec: GateTaskSpec, actor) -> str` (`featuregen.gates.tasks`); `GateTaskSpec(gate, required_inputs, eligible_assignees, allowed_responses, run_id=None, feature_id=None, quorum_required=1, quorum_of_role=None, delegation_allowed=True, sla=None, ...)` (`featuregen.contracts.gates`); `append(...)` (Task 1.1); `USE_CASE_ONBOARDING_GATE` / `NEEDS_USE_CASE_ONBOARDING` (Task 1.2); `build_service_identity(...)`; `apply_migrations(conn)`.
- Produces: `human_tasks_gate_check` admits `'USE_CASE_ONBOARDING'` (preserving CLARIFICATION/DATA_STEWARD/COMPLIANCE/INDEPENDENT_VALIDATION/FINAL_APPROVAL + SP-1's OVERLAY_DATA_OWNER/OVERLAY_COMPLIANCE); the `NEEDS_USE_CASE_ONBOARDING` park hold-state rides SP-0's `RUN_PARKED.waiting_on_fact` (no DB CHECK to widen); a partial index on open onboarding tasks. Consumed by P4 (`submit_intent` → park + `USE_CASE_ONBOARDING_REQUESTED`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_use_case_onboarding_gates.py
from __future__ import annotations

import psycopg
import pytest

from featuregen.aggregates._append import append
from featuregen.contracts.gates import GateTaskSpec
from featuregen.db.migrations import apply_migrations
from featuregen.gates.tasks import open_task
from featuregen.identity.build import build_service_identity
from featuregen.intake.events import NEEDS_USE_CASE_ONBOARDING, USE_CASE_ONBOARDING_GATE

_RUN = "run_onb01"


def _svc(subject="service:workflow", role="workflow"):
    return build_service_identity(
        subject=subject, role_claims=[role],
        attestation="signed-deploy-id:workflow@1.0.0",
    )


def _onboarding_spec(**kw):
    base = dict(
        gate=USE_CASE_ONBOARDING_GATE,
        required_inputs=("draft_ref",),
        eligible_assignees={"role": "governance", "scope": "use-case-onboarding"},
        allowed_responses=("onboard", "reject"),
        run_id=_RUN,
        delegation_allowed=False,
    )
    base.update(kw)
    return GateTaskSpec(**base)


def test_use_case_onboarding_gate_task_opens(db):
    task_id = open_task(db, _onboarding_spec(), _svc())
    row = db.execute(
        "SELECT gate, run_id, delegation_allowed FROM human_tasks WHERE task_id=%s",
        (task_id,),
    ).fetchone()
    assert row == ("USE_CASE_ONBOARDING", _RUN, False)


def test_base_and_overlay_gates_still_accepted(db):
    for gate in ("CLARIFICATION", "FINAL_APPROVAL", "OVERLAY_DATA_OWNER", "OVERLAY_COMPLIANCE"):
        task_id = open_task(db, _onboarding_spec(gate=gate), _svc())
        got = db.execute(
            "SELECT gate FROM human_tasks WHERE task_id=%s", (task_id,)
        ).fetchone()[0]
        assert got == gate


def test_gate_check_rejects_unknown_gate(db):
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO human_tasks (task_id, gate, eligible_assignees, allowed_responses) "
            "VALUES ('task_bad','NOT_A_GATE','{}'::jsonb, ARRAY['x'])"
        )


def test_run_parked_carries_needs_use_case_onboarding_hold_state(conn):
    # The park hold-state rides RUN_PARKED.waiting_on_fact (base payload is unconstrained).
    env = append(
        conn,
        aggregate="run",
        aggregate_id=_RUN,
        run_id=_RUN,
        type="RUN_PARKED",
        payload={"run_id": _RUN, "owner": "governance",
                 "waiting_on_fact": NEEDS_USE_CASE_ONBOARDING},
        actor=_svc(),
    )
    got = conn.execute(
        "SELECT payload->>'waiting_on_fact' FROM events WHERE event_id=%s", (env.event_id,)
    ).fetchone()[0]
    assert got == "NEEDS_USE_CASE_ONBOARDING"


def test_use_case_onboarding_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    chk = conn.execute(
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='human_tasks_gate_check'"
    ).fetchone()[0]
    assert "USE_CASE_ONBOARDING" in chk
    assert "OVERLAY_DATA_OWNER" in chk  # regression: SP-1's overlay gates survive the rebuild
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='human_tasks_use_case_onboarding_idx'"
    ).fetchone()
    assert idx is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/intake/test_use_case_onboarding_gates.py -v`
Expected: FAIL — inserting `gate='USE_CASE_ONBOARDING'` violates `human_tasks_gate_check`.

- [ ] **Step 3: Create the `0509_use_case_onboarding_gates.sql` migration**

```sql
-- src/featuregen/db/migrations/0509_use_case_onboarding_gates.sql
-- SP-2 Phase 1 (design §2.1 #6, §5.4, §11): admit a new-banking-use-case onboarding gate + park
-- hold-state, mirroring SP-1's 0505_overlay_gates.sql. Additive + idempotent.

-- 1. Widen the gate CHECK (auto-named human_tasks_gate_check) with USE_CASE_ONBOARDING. This
--    DROP/ADD PRESERVES SP-0's base gates AND SP-1's OVERLAY_DATA_OWNER/OVERLAY_COMPLIANCE (0509
--    sorts after 0505, so it rebuilds on top of them).
ALTER TABLE human_tasks DROP CONSTRAINT IF EXISTS human_tasks_gate_check;
ALTER TABLE human_tasks ADD CONSTRAINT human_tasks_gate_check CHECK (
    gate IN ('CLARIFICATION','DATA_STEWARD','COMPLIANCE',
             'INDEPENDENT_VALIDATION','FINAL_APPROVAL',
             'OVERLAY_DATA_OWNER','OVERLAY_COMPLIANCE',
             'USE_CASE_ONBOARDING')
);

-- 2. The NEEDS_USE_CASE_ONBOARDING park hold-state needs NO DDL: it rides SP-0's RUN_PARKED
--    payload field `waiting_on_fact` (the base payload is unconstrained — owner/waiting_on_fact,
--    run_lifecycle.py — and RUN_PARKED's registered event schema is additionalProperties:false over
--    exactly {run_id, owner, waiting_on_fact}, so the hold-state string fits without a schema bump).
--    The canonical constant is intake.events.NEEDS_USE_CASE_ONBOARDING. This comment is the
--    additive "registration" of the park-reason (no CHECK exists to widen).

-- 3. Partial index for open onboarding tasks by run (mirrors human_tasks_fact_idx).
CREATE INDEX IF NOT EXISTS human_tasks_use_case_onboarding_idx
    ON human_tasks (run_id) WHERE gate = 'USE_CASE_ONBOARDING' AND status = 'open';
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/intake/test_use_case_onboarding_gates.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0509_use_case_onboarding_gates.sql \
        tests/featuregen/intake/test_use_case_onboarding_gates.py
git commit -m "feat(intake): add USE_CASE_ONBOARDING gate + NEEDS_USE_CASE_ONBOARDING park hold-state"
```

---

### Task 1.5: the `llm_call` immutable record store + the fail-closed FC-status projection checkpoint

**Files:**
- Create: `src/featuregen/db/migrations/0510_llm_call_store.sql`
- Test: `tests/featuregen/intake/test_llm_call_store.py`

**Interfaces:**
- Consumes: `apply_migrations(conn)`; `projection_checkpoints(projection_name PK, checkpoint_seq, head_seq, is_analytics, updated_at)` (Stage-1 core DDL).
- Produces: the SP-2-owned append-only `llm_call` table (write-once record store, classified **sensitive / governance-retained / read-controlled**, §9.3) — columns `llm_call_ref (PK), feature_contract_id, run_id, task, provider, model, prompt_id, prompt_version, output_schema_id, output_schema_version, generation_settings, redaction_version, input_hash, redacted_input, input_redaction, raw_output, validation_result, repair_attempts, latency_ms, cost_metadata, created_at, created_by`; an idempotency-probe index `(run_id, task, input_hash)`; a `projection_checkpoints('feature_contract')` row (seq 0, non-analytics). **P3** implements `record_llm_call` / `read_llm_call` + the full-identity idempotency key over this table; **P8** builds the optional fail-closed FC-status projection on this checkpoint.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/intake/test_llm_call_store.py
from __future__ import annotations

from featuregen.db.migrations import apply_migrations

_EXPECTED_COLS = {
    "llm_call_ref", "feature_contract_id", "run_id", "task", "provider", "model",
    "prompt_id", "prompt_version", "output_schema_id", "output_schema_version",
    "generation_settings", "redaction_version", "input_hash", "redacted_input",
    "input_redaction", "raw_output", "validation_result", "repair_attempts",
    "latency_ms", "cost_metadata", "created_at", "created_by",
}


def test_llm_call_table_exists_with_full_provenance_columns(conn):
    apply_migrations(conn)
    reg = conn.execute("SELECT to_regclass('public.llm_call')").fetchone()[0]
    assert reg is not None
    cols = {
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='llm_call'"
        ).fetchall()
    }
    assert _EXPECTED_COLS <= cols


def test_feature_contract_projection_checkpoint_seeded(conn):
    apply_migrations(conn)
    row = conn.execute(
        "SELECT projection_name, checkpoint_seq, head_seq, is_analytics "
        "FROM projection_checkpoints WHERE projection_name='feature_contract'"
    ).fetchone()
    assert row == ("feature_contract", 0, 0, False)


def test_llm_call_migration_is_idempotent(conn):
    apply_migrations(conn)
    apply_migrations(conn)
    n = conn.execute(
        "SELECT count(*) FROM projection_checkpoints WHERE projection_name='feature_contract'"
    ).fetchone()[0]
    assert n == 1  # ON CONFLICT DO NOTHING — re-apply does not duplicate the row
    idx = conn.execute(
        "SELECT 1 FROM pg_indexes WHERE indexname='llm_call_idem_idx'"
    ).fetchone()
    assert idx is not None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/featuregen/intake/test_llm_call_store.py -v`
Expected: FAIL — `to_regclass('public.llm_call')` is `None`; no `'feature_contract'` checkpoint row.

- [ ] **Step 3: Create the `0510_llm_call_store.sql` migration**

```sql
-- src/featuregen/db/migrations/0510_llm_call_store.sql
-- SP-2 Phase 1 (design §2.1 #4, §9.3, Decision D9/D15): the SP-2-owned append-only `llm_call`
-- record store — an SP-0-style write-once artifact (like SP-1's overlay_evidence), referenced by
-- llm_call_ref, classified SENSITIVE / governance-retained / read-controlled. It stores the STORED
-- REDACTED (LLM-safe) input itself (redacted_input) — never the raw intent (that stays in SP-0's
-- encrypted raw_input_ref) — so a regulator can REPLAY the exact prompt (MRM/adverse-action). This
-- is a TABLE, not an event aggregate (no aggregate-CHECK change). P3 writes it via record_llm_call.
-- All CREATE ... IF NOT EXISTS; the checkpoint insert is ON CONFLICT DO NOTHING — fully idempotent.

CREATE TABLE IF NOT EXISTS llm_call (
    llm_call_ref          text        PRIMARY KEY,
    feature_contract_id   text        NULL,
    run_id                text        NOT NULL,
    task                  text        NOT NULL,
    provider              text        NOT NULL,
    model                 text        NOT NULL,
    prompt_id             text        NOT NULL,
    prompt_version        integer     NOT NULL,
    output_schema_id      text        NOT NULL,
    output_schema_version integer     NOT NULL,
    generation_settings   jsonb       NOT NULL DEFAULT '{}',   -- pinned; part of the idempotency key
    redaction_version     text        NOT NULL,                -- which IntentRedactor policy (§9.4)
    input_hash            text        NOT NULL,                -- sha256 of the exact redacted input
    redacted_input        jsonb       NOT NULL,                -- the STORED LLM-safe input (replayable)
    input_redaction       jsonb       NOT NULL DEFAULT '{}',   -- what was scrubbed (audit boundary)
    raw_output            jsonb       NULL,                    -- the model's structured output
    validation_result     jsonb       NOT NULL DEFAULT '{}',   -- ok|invalid|refusal|truncated|... (§9.2)
    repair_attempts       integer     NOT NULL DEFAULT 0,
    latency_ms            integer     NULL,
    cost_metadata         jsonb       NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            jsonb       NOT NULL                 -- identity_to_jsonb(service:intake-agent)
);

-- Idempotency probe for the P3 full-identity dedup key (§9.3, §12); the full-identity uniqueness is
-- enforced in the P3 record writer, this index accelerates the (run_id, task, input_hash) lookup.
CREATE INDEX IF NOT EXISTS llm_call_idem_idx ON llm_call (run_id, task, input_hash);

-- Checkpoint for the OPTIONAL fail-closed FC-status read-model projection (built in P8, secondary to
-- the fold, §11/§12). Seeded here so it exists after migrations, mirroring SP-1's 0507 overlay seed.
INSERT INTO projection_checkpoints (projection_name) VALUES ('feature_contract')
ON CONFLICT DO NOTHING;
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/featuregen/intake/test_llm_call_store.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0510_llm_call_store.sql \
        tests/featuregen/intake/test_llm_call_store.py
git commit -m "feat(intake): add the sensitive write-once llm_call record store + FC checkpoint"
```

---

### Task 1.6: `seed_sp2_authz(conn)` — the additive authz rows + `PRIMARY_SELECTED` wiring (`intake/bootstrap.py`)

**Files:**
- Create: `src/featuregen/intake/bootstrap.py`
- Test: `tests/featuregen/intake/test_sp2_bootstrap.py`

**Interfaces:**
- Consumes: `seed_authz_policy`-style `authz_policy` upsert (`INSERT ... ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING`, `authz/policy.py`); `register_primary_selected(conn)` (`featuregen.documents.primary`) — registers `PRIMARY_SELECTED` in the durable `event_type_registry` + the in-memory `event_registry()` singleton + seeds the `stage_primary` checkpoint; `projection_checkpoints` table.
- Produces: `seed_sp2_authz(conn) -> None` — idempotently seeds the eight SP-2 command-capability rows (incl. the additive **`("reject_intent","","intake-agent","service",None)`** rejection authority, §2.1 #5; SP-0's `reject` stays validator-only), wires `PRIMARY_SELECTED` for hypothesis-mode candidate promotion, and idempotently ensures the `feature_contract` checkpoint. **No onboarding-answer row** (deferred, §14). `_SP2_POLICY_ROWS: tuple[...]`. Consumed by P9's `register_sp2` + the E2E suite; fine-grained authority (request-owner guard, `confirmer_is_requester_human`, delegation-off) is enforced in the handlers/`mcv.py` guards, **not** here.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/intake/test_sp2_bootstrap.py
from __future__ import annotations

from featuregen.events.registry import event_registry
from featuregen.intake.bootstrap import seed_sp2_authz


def _authz_rows(conn):
    return {
        (r[0], r[1], r[2], r[3])
        for r in conn.execute(
            "SELECT action, gate, permitted_role, actor_kind FROM authz_policy"
        ).fetchall()
    }


def test_seed_adds_the_additive_rejection_authority(conn):
    seed_sp2_authz(conn)
    rows = _authz_rows(conn)
    assert ("reject_intent", "", "intake-agent", "service") in rows
    # SP-2 issues its OWN reject_intent — it never seeds/reuses SP-0's validator-only `reject`
    # (authz/policy.py:42 stays untouched; SoD holds).
    assert not any(action == "reject" for (action, _gate, _role, _kind) in rows)
    # No onboarding-answer row is added (deferred, §14).
    assert not any(action == "answer_use_case_onboarding" for (action, _g, _r, _k) in rows)


def test_seed_adds_all_eight_command_capability_rows(conn):
    seed_sp2_authz(conn)
    rows = _authz_rows(conn)
    expected = {
        ("submit_intent", "", "data_scientist", "human"),
        ("submit_intent", "", "intake-agent", "service"),
        ("answer_clarification", "", "data_scientist", "human"),
        ("select_candidate_doc", "", "data_scientist", "human"),
        ("open_gate1_task", "", "intake-agent", "service"),
        ("confirm_contract", "", "data_scientist", "human"),
        ("request_edit", "", "data_scientist", "human"),
        ("reject_intent", "", "intake-agent", "service"),
    }
    assert expected <= rows


def test_seed_wires_primary_selected_and_checkpoints(conn):
    seed_sp2_authz(conn)
    # PRIMARY_SELECTED registered durably + in-memory (so candidate-promotion appends validate).
    durable = conn.execute(
        "SELECT 1 FROM event_type_registry WHERE type_name='PRIMARY_SELECTED'"
    ).fetchone()
    assert durable is not None
    assert event_registry().max_active_versions().get("PRIMARY_SELECTED") == 1
    # stage_primary + feature_contract checkpoints exist.
    names = {
        r[0]
        for r in conn.execute(
            "SELECT projection_name FROM projection_checkpoints "
            "WHERE projection_name IN ('stage_primary','feature_contract')"
        ).fetchall()
    }
    assert names == {"stage_primary", "feature_contract"}


def test_seed_is_idempotent(conn):
    seed_sp2_authz(conn)
    seed_sp2_authz(conn)  # must not raise (ON CONFLICT DO NOTHING everywhere)
    n = conn.execute(
        "SELECT count(*) FROM authz_policy WHERE action='reject_intent'"
    ).fetchone()[0]
    assert n == 1
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_sp2_bootstrap.py -v`
Expected: FAIL — `featuregen.intake.bootstrap` does not exist (ImportError).

- [ ] **Step 3: Create `intake/bootstrap.py`**

```python
# src/featuregen/intake/bootstrap.py
"""SP-2 production wiring — the additive authz surface + candidate-promotion wiring (design §2.1 #5).
P1 introduces `seed_sp2_authz`; P9's `register_sp2(handler_registry)` will build on this module
(event schemas + contract schemas + commands). Same authz-row shape as authz.policy._POLICY_ROWS —
coarse command capability only; fine-grained authority (the SP-2-built request-owner guard,
confirmer_is_requester_human, delegation_allowed=False) lives in the command handlers + intake/mcv.py,
NOT in these rows (mirrors SP-1)."""

from __future__ import annotations

from featuregen.contracts.db import DbConn
from featuregen.documents.primary import register_primary_selected

# §2.1 #5 + the SP-2 command-capability rows. The additive rejection authority `reject_intent`
# admits the platform/service principal to issue OUT_OF_SCOPE / PROHIBITED_DATA_CLASS terminal
# outcomes (→ SP-0 RUN_REJECTED) — SP-0's `reject` (authz/policy.py:42) STAYS validator-only, and
# requester abandonment reuses SP-0's data-scientist-owned `withdraw`. NO onboarding-answer row is
# added (deferred, §14): the USE_CASE_ONBOARDING task uses SP-0's existing
# ("open_task","","workflow","service",None) row.
_SP2_POLICY_ROWS: tuple[tuple[str, str, str, str, str | None], ...] = (
    ("submit_intent", "", "data_scientist", "human", None),
    ("submit_intent", "", "intake-agent", "service", None),
    ("answer_clarification", "", "data_scientist", "human", None),
    ("select_candidate_doc", "", "data_scientist", "human", None),
    ("open_gate1_task", "", "intake-agent", "service", None),
    ("confirm_contract", "", "data_scientist", "human", None),
    ("request_edit", "", "data_scientist", "human", None),
    ("reject_intent", "", "intake-agent", "service", None),  # additive rejection authority (§2.1 #5)
)


def seed_sp2_authz(conn: DbConn) -> None:
    """Idempotently seed SP-2's authz rows, wire PRIMARY_SELECTED for hypothesis-mode candidate
    promotion (document-level primitive, §7.1), and ensure the (optional, P8) fail-closed FC-status
    read-model checkpoint. Every step is ON CONFLICT DO NOTHING / an idempotent registration."""
    for action, gate, role, kind, scope in _SP2_POLICY_ROWS:
        conn.execute(
            """
            INSERT INTO authz_policy (action, gate, permitted_role, actor_kind, scope)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (action, gate, permitted_role, actor_kind) DO NOTHING
            """,
            (action, gate, role, kind, scope),
        )
    # PRIMARY_SELECTED (SP-0 primitive) — registers the schema durably + in the in-memory singleton
    # and seeds the stage_primary checkpoint, so the P6 select_candidate_doc promotion appends validate.
    register_primary_selected(conn)
    # Optional fail-closed FC-status read-model checkpoint (P8) — also seeded by 0510; idempotent.
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) VALUES ('feature_contract') "
        "ON CONFLICT DO NOTHING"
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/featuregen/intake/test_sp2_bootstrap.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/bootstrap.py \
        tests/featuregen/intake/test_sp2_bootstrap.py
git commit -m "feat(intake): seed_sp2_authz — additive reject_intent authority + PRIMARY_SELECTED wiring"
```

---

## Phase 1 exit check

- [ ] **Run the whole SP-2 P1 surface + the touched SP-0/SP-1 suites green (no regression)**

Run: `uv run pytest tests/featuregen/intake/ tests/featuregen/events/ tests/featuregen/gates/ tests/featuregen/documents/ -v`
Expected: PASS — the three new migrations (`0508`/`0509`/`0510`) apply idempotently; the widened `events_aggregate_check` / `events_aggregate_id_consistent` / `human_tasks_gate_check` still admit every SP-0 + SP-1 value (the overlay and gate suites stay green); the `feature_contract_id` append path, the twelve registered FC event schemas, the `append_feature_contract_event` seam, the `llm_call` store, and `seed_sp2_authz` are all green.

- [ ] **Confirm the P1 → downstream contract is in place**

Verify the symbols the later phases bind to exist and match the overview's Shared Contract:
- `append(..., feature_contract_id=None)` + `EventEnvelope.feature_contract_id` + `partition_key_for` `feature_contract:` branch (Task 1.1) — used by every SP-2 handler.
- `register_sp2_event_types(registry)` + the twelve constants + `USE_CASE_ONBOARDING_GATE` / `NEEDS_USE_CASE_ONBOARDING` (Task 1.2) — P4–P8, P9.
- `append_feature_contract_event(...)` / `load_feature_contract(...)` (Task 1.3) — P3(`call_llm`)/P4–P8 + `fold_feature_contract_state` (P8).
- `USE_CASE_ONBOARDING` gate + `NEEDS_USE_CASE_ONBOARDING` park hold-state (Task 1.4) — P4.
- the `llm_call` store + `feature_contract` checkpoint (Task 1.5) — P3 (`record_llm_call`/`read_llm_call`), P8 (FC projection).
- `seed_sp2_authz(conn)` + the `reject_intent` service authz row + `PRIMARY_SELECTED` wiring (Task 1.6) — P9 `register_sp2`, P6 candidate promotion, P8 `reject_intent`.
