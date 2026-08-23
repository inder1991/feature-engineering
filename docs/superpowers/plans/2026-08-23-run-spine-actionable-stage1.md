# Run Spine Actionable — Stage I Implementation Plan (invocation substrate + AUTHOR_FORMULA)

> ▲ **NO-GO — DO NOT EXECUTE (owner review, 2026-08-23).** Ten P0 blockers stand against this
> revision; the verdicts and the corrected 13-step sequence are recorded in the owner's review and
> the session ledger. Headline corrections a revision must fold: authoring input sealing + atomic
> output binding + legacy-route adapter move INTO Stage I (the spec's §13 ordering is binding);
> the client names a server-minted subject id, never raw identity facts; Task 4 targets a 1104
> schema that does not exist (candidate_origin/formula_strategy/strategy_identity_hash — spend
> binding is unresolved architecture); strategy is RESOLVED, never a constant; 1117 needs the
> composite invocation FK, action CHECK, subject/link append-only triggers, link→candidate
> agreement, and totality; gesture/outcome split (START vs CREATED/ATTACHED_EXISTING) with the
> guard purpose-independent; idempotency compares the stored request_content_hash over the FULL
> server-resolved request (409 IDEMPOTENCY_CONFLICT); 1106's decision identity must cover the
> whole payload and a read-only preflight must exist; spend reserves at AuditingClient.call
> (the physical seam), settles after egress, reconciles expired reservations against dispatch
> outcomes; PRE_SPINE runs are not actionable. Substrate remediation (1104–1106) precedes any
> plan revision.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The first run-centric trigger — a person opens `#/runs/{id}` and starts formula authoring for chosen candidates, through one governed invocation that binds authorization, decision and spend before any outbox row, with the run's attempt history derived, never stored twice.

**Architecture:** One immutable `feature_run_action_invocation` per gesture fans into per-subject immutable attempt headers carrying the §0.1.2 composite authorization/decision group (both FK targets already exist: 1100's `act_key`, 1106's `action_decision_revision_act_key`). Status is always derived from `formula_draft`; the spine stores no lifecycle, takes no lease, and owes no reconciler. The CAS on `feature_run_state` (idempotency looked up BEFORE the CAS) serializes run mutations; each subject's sealed input is 1104's `formula_draft_authoring_plan` row, written at request time so the worker re-reads and never re-resolves.

**Scope ruling (Stage II exists):** `GENERATE_PREVIEW` triggering, `feature_run_action_input` + preview links, §6.5 output binding at `generation_lane.py:560-564`, and `/re-execute` are Stage II — they need the atomicity work and the canonical-hash decision this stage does not. Stage I ships a complete, testable lane: trigger → governed draft → visible attempt.

**Tech Stack:** Python/FastAPI/psycopg + PostgreSQL migrations · React/TS/Vitest.

**Spec:** `docs/superpowers/specs/2026-08-22-feature-run-spine-design.md` (rev 3.1). Interface maps from the 2026-08-23 sweep are authoritative for shapes cited below; where this plan and a file disagree, the FILE wins and the deviation is reported.

## Global Constraints

- Worktree for execution: a NEW worktree branched from `feature/asset-detail-reapply` at the commit where Task 0's entry conditions pass. `git add <exact path>` only; NEVER `-A`; NEVER `git stash`.
- **Entry conditions (Task 0 verifies, execution stops if unmet):** migration `1106_action_decision_revision.sql` + `src/featuregen/materialize/action_decision.py` + its test are COMMITTED (they are in-flight 2A files today); the child/parent plan amendment (Task 0) is approved.
- Read-only rules carried from the foundation stand: no fork, no cancel, no `TRAIN_MODEL` rows, no `/retry` for AUTHOR_FORMULA (`request_draft` returns the cached row for a live identity; `DraftNotAnAnswer` at `formula_draft_store.py:479` now refuses terminal drafts — reuse is a READ, a second opinion is priced and deferred).
- Migration numbers: **1117 onward** (1107–1114 are reserved-with-no-file by the parent; 1109 is retired by Task 0's amendment; nothing referencing 1115's tables may sort before 1115 — lexical apply order, `migrations.py:260-272`).
- Server-side identity everywhere: `actor_subject` comes only from `identity.subject`; the grantee rule (authorization's `actor_subject == identity.subject`) is enforced at every spend of an authorization — the 1100 model has NO grantee column, so the route owns it (sweep hook: legacy safeguard lives only at `build_sets.py:282-292`).
- `AUTHOR_FORMULA`'s `resource_identity_hash` IS `retirement_scope_key(...)` (`retirement_scope.py:73`, jcs over exactly `considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash, definition_revision`). One spelling, never a second.
- API tests never `conn.commit()`. Tests never weaken an assertion to pass a constraint — fix seeds.
- Reason codes are closed-vocabulary: this stage registers `INPUT_IDENTITY_MISMATCH` is NOT needed yet (ATTACH here binds the same draft identity by construction); new codes introduced: none beyond blockers `ask()` already emits. Parent §5's three-part-commit rule applies if one appears.
- Baselines at plan time: backend 13322+ / frontend 1036 (re-measure at Task 0; the merged repair run is the reference).

---

### Task 0: Plan amendments + entry-condition gate (OWNER GATE)

**Files:**
- Modify: `docs/superpowers/plans/2026-08-22-recipe-to-code-llm-fallback.md` (§3.5 and D6's coordinator text)
- Modify: `docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md` (§0.1.3, the line-410 enumeration, §17's 1109 row; reserve 1117–1119)

**Interfaces:**
- Produces: the invocation model as the plans' authoritative shape; migration numbers 1117–1119 reserved; 1109 retired.

- [ ] **Step 1: Verify entry conditions** — `git ls-files --error-unmatch src/featuregen/db/migrations/1106_action_decision_revision.sql src/featuregen/materialize/action_decision.py` must succeed (2A committed them). If not: STOP, report — this plan depends on that landing, and must not carry another session's files.
- [ ] **Step 2: Check both plan files are clean** (`git status --short <file>`); STOP on third-party dirt.
- [ ] **Step 3: Amend the child plan** — replace §3.5's `code_generation_job`/`_member`/`_event`/`_action` block with the invocation model (quote spec §4.1's DDL verbatim), keeping two sentences verbatim as the sweep ruled they already match the spec: D6's *"Existing formula and generation queues remain the workers; the coordinator joins their durable states and advances only from recorded evidence"* and §3.5's *"`BLOCKED` is a product outcome; `FAILED` is a platform failure."* Zero code references exist (`grep -rn code_generation_job src/ tests/ frontend/src` is empty) — say so in the amendment: documentation-only.
- [ ] **Step 4: Amend the parent** — rewrite §0.1.3's job-shaped text to name the invocation + per-header authorization; update the line-410 enumeration; in §17 mark 1109 `RETIRED — superseded by the run-spine invocation model (1117); the number is never reused (lexical sort would place it before 1115's tables it references)`, and append rows: `1117 = run-spine Stage I: invocation, attempt headers, authoring subject child, links, events`, `1118/1119 = reserved, run-spine Stage II (preview links + output binding)`.
- [ ] **Step 5: STOP — owner approval of both amendments.** In an isolated worktree, approval may be deferred to the merge gate per the foundation's precedent — record which applies.
- [ ] **Step 6: Commit** (exact paths, one commit): `docs: dissolve code_generation_job into the run-spine invocation model; reserve 1117-1119`

---

### Task 1: Migration 1117 — invocation, attempt headers, authoring subject, link, events

**Files:**
- Create: `src/featuregen/db/migrations/1117_run_action_invocation.sql`
- Test: `tests/featuregen/db/test_migration_1117.py`

**Interfaces:**
- Consumes: 1100's `act_key` index `(action, resource_identity_hash, authorization_id)`; 1106's `action_decision_revision_act_key` `(action, resource_identity_hash, decision_id, authorization_id)`; 1115's `feature_run_identity`/`feature_run_state`; `contract_considered_revision`.
- Produces: tables `feature_run_action_invocation`, `feature_run_action_attempt`, `authoring_attempt_subject`, `authoring_attempt_link`, `feature_run_action_event` — exact shapes below; every later task builds on these columns.

- [ ] **Step 1: Write failing tests** (use `seed_run_chain` from `tests/featuregen/runs/_chain.py`; seed an authorization via `authorize_action` and a decision via `decide` — imports `from featuregen.materialize.action_authorization import ActionV1, authorize_action` / `from featuregen.materialize.action_decision import ActionRequestV1, decide`):

```python
import psycopg
import pytest
from featuregen.materialize.action_authorization import ActionV1, authorize_action
from featuregen.materialize.action_decision import ActionRequestV1, decide
from tests.featuregen.runs._chain import seed_run_chain

_SCOPE = "s" * 64


def _seed_auth_and_decision(db):
    auth = authorize_action(db, action=ActionV1.AUTHOR_FORMULA, resource_identity_hash=_SCOPE,
                            actor_subject="user:priya", environment_id="dev")
    decision_id, _ = decide(db, ActionRequestV1(action=ActionV1.AUTHOR_FORMULA,
                                                resource_identity_hash=_SCOPE),
                            authorization_id=auth.authorization_id)
    return auth.authorization_id, decision_id


def _insert_invocation(db, run_id, inv_id="inv-1"):
    db.execute(
        "INSERT INTO feature_run_action_invocation (invocation_id, generation_run_id, action, "
        "idempotency_key, request_content_hash, requested_by, requested_at) "
        "VALUES (%s, %s, 'AUTHOR_FORMULA', 'k1', 'rch', 'user:priya', now())", (inv_id, run_id))


def _insert_header(db, run_id, auth_id, dec_id, *, attempt_id="att-1", n=1, purpose="START"):
    db.execute(
        "INSERT INTO feature_run_action_attempt (attempt_id, invocation_id, generation_run_id, "
        "action, stage_subject_id, attempt_number, attempt_purpose, resource_identity_hash, "
        "action_authorization_revision_id, action_decision_revision_id, requested_by, requested_at) "
        "VALUES (%s, 'inv-1', %s, 'AUTHOR_FORMULA', %s, %s, %s, %s, %s, %s, 'user:priya', now())",
        (attempt_id, run_id, _SCOPE, n, purpose, _SCOPE, auth_id, dec_id))


def test_header_inserts_with_the_full_authorization_group(db):
    seed_run_chain(db, run_id="m1117-a")
    auth_id, dec_id = _seed_auth_and_decision(db)
    _insert_invocation(db, "m1117-a")
    _insert_header(db, "m1117-a", auth_id, dec_id)


def test_header_refuses_an_authorization_for_a_different_resource(db):
    seed_run_chain(db, run_id="m1117-b")
    auth_id, dec_id = _seed_auth_and_decision(db)
    _insert_invocation(db, "m1117-b")
    db_scope_mismatch = "x" * 64
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO feature_run_action_attempt (attempt_id, invocation_id, generation_run_id, "
            "action, stage_subject_id, attempt_number, attempt_purpose, resource_identity_hash, "
            "action_authorization_revision_id, action_decision_revision_id, requested_by, requested_at) "
            "VALUES ('att-x', 'inv-1', 'm1117-b', 'AUTHOR_FORMULA', %s, 1, 'START', %s, %s, %s, "
            "'user:priya', now())", (db_scope_mismatch, db_scope_mismatch, auth_id, dec_id))


def test_one_START_per_subject(db):
    seed_run_chain(db, run_id="m1117-c")
    auth_id, dec_id = _seed_auth_and_decision(db)
    _insert_invocation(db, "m1117-c")
    _insert_header(db, "m1117-c", auth_id, dec_id, attempt_id="att-1", n=1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _insert_header(db, "m1117-c", auth_id, dec_id, attempt_id="att-2", n=2, purpose="START")


def test_header_is_write_once(db):
    seed_run_chain(db, run_id="m1117-d")
    auth_id, dec_id = _seed_auth_and_decision(db)
    _insert_invocation(db, "m1117-d")
    _insert_header(db, "m1117-d", auth_id, dec_id)
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE feature_run_action_attempt SET attempt_number=9 WHERE attempt_id='att-1'")


def test_event_sequence_is_unique_per_attempt(db):
    seed_run_chain(db, run_id="m1117-e")
    auth_id, dec_id = _seed_auth_and_decision(db)
    _insert_invocation(db, "m1117-e")
    _insert_header(db, "m1117-e", auth_id, dec_id)
    db.execute("INSERT INTO feature_run_action_event (attempt_id, event_sequence, event_kind, "
               "actor_subject, detail) VALUES ('att-1', 1, 'requested', 'user:priya', '{}')")
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute("INSERT INTO feature_run_action_event (attempt_id, event_sequence, event_kind, "
                   "actor_subject, detail) VALUES ('att-1', 1, 'authorized', 'user:priya', '{}')")
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/featuregen/db/test_migration_1117.py -x -q` — FAIL: relations missing.
- [ ] **Step 3: Write the migration** (repo header style + `-- NOT APPLIED. This file is written, not run.`):

```sql
-- src/featuregen/db/migrations/1117_run_action_invocation.sql
-- Run-spine ACTIONABLE Stage I (spec rev 3.1 §4.1/§5/§6.3/§6.4/§6.6). One immutable invocation
-- per user gesture; per-subject immutable attempt HEADERS carrying §0.1.2's composite
-- authorization/decision group (targets: 1100's act_key, 1106's action_decision_revision_act_key,
-- both already the exact shapes these FKs need). STATUS IS NEVER STORED HERE — it derives from the
-- linked domain attempt (formula_draft). No lease, no fence, no reconciler debt (§4).
-- A BATCH IS AN UNORDERED SET, ruled: an authoring gesture's members carry no position — ordering
-- was a build-set fact (1092), never an authoring fact; attempt_number counts THIS RUN'S ASKS.
-- NOT APPLIED. This file is written, not run.

CREATE TABLE IF NOT EXISTS feature_run_action_invocation (
    invocation_id        text PRIMARY KEY CHECK (btrim(invocation_id) <> ''),
    generation_run_id    text NOT NULL REFERENCES feature_generation_run (generation_run_id),
    action               text NOT NULL CHECK (action IN (
                             'AUTHOR_FORMULA','GENERATE_PREVIEW','EXECUTE_SANDBOX',
                             'PUBLISH_SANDBOX','MATERIALIZE_PRODUCTION','PUBLISH_PRODUCTION')),
    idempotency_key      text NOT NULL CHECK (btrim(idempotency_key) <> ''),
    request_content_hash text NOT NULL CHECK (btrim(request_content_hash) <> ''),
    requested_by         text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (generation_run_id, action, idempotency_key)
);

CREATE TABLE IF NOT EXISTS feature_run_action_attempt (
    attempt_id            text PRIMARY KEY CHECK (btrim(attempt_id) <> ''),
    invocation_id         text NOT NULL REFERENCES feature_run_action_invocation (invocation_id),
    generation_run_id     text NOT NULL REFERENCES feature_generation_run (generation_run_id),
    action                text NOT NULL,
    stage_subject_id      text NOT NULL CHECK (btrim(stage_subject_id) <> ''),
    attempt_number        integer NOT NULL CHECK (attempt_number >= 1),
    attempt_purpose       text NOT NULL CHECK (attempt_purpose IN
                              ('START','RETRY','RE_EXECUTE','ATTACH')),
    resource_identity_hash text NOT NULL CHECK (btrim(resource_identity_hash) <> ''),
    action_authorization_revision_id text NOT NULL,
    action_decision_revision_id      text NOT NULL,
    requested_by          text NOT NULL CHECK (btrim(requested_by) <> ''),
    requested_at          timestamptz NOT NULL DEFAULT now(),

    UNIQUE (generation_run_id, action, stage_subject_id, attempt_number),
    -- §0.1.2's group, in act_key column order (action, resource_identity_hash, id):
    FOREIGN KEY (action, resource_identity_hash, action_authorization_revision_id)
        REFERENCES action_authorization_revision (action, resource_identity_hash, authorization_id),
    FOREIGN KEY (action, resource_identity_hash, action_decision_revision_id,
                 action_authorization_revision_id)
        REFERENCES action_decision_revision (action, resource_identity_hash, decision_id,
                                             authorization_id)
);
-- §5's database-level one-START guard: retries/re-executes/attaches append; a second START refuses.
CREATE UNIQUE INDEX IF NOT EXISTS feature_run_action_attempt_one_start
    ON feature_run_action_attempt (generation_run_id, action, stage_subject_id)
    WHERE attempt_purpose = 'START';

-- The typed subject child for AUTHOR_FORMULA (§6.3): the five candidate facts the scope key hashes.
-- stage_subject_id and resource_identity_hash both EQUAL retirement_scope_key over these five —
-- one spelling (retirement_scope.py:73); the child holds the real references a hash cannot.
CREATE TABLE IF NOT EXISTS authoring_attempt_subject (
    attempt_id             text PRIMARY KEY REFERENCES feature_run_action_attempt (attempt_id),
    considered_revision_id text NOT NULL REFERENCES contract_considered_revision (considered_revision_id),
    option_id              text NOT NULL CHECK (btrim(option_id) <> ''),
    planning_request_hash  text NOT NULL CHECK (btrim(planning_request_hash) <> ''),
    catalog_snapshot_hash  text NOT NULL CHECK (btrim(catalog_snapshot_hash) <> ''),
    definition_revision    text NOT NULL
);

-- One header, ONE domain attempt (§4/§6.4): the draft id only — the content hash is born at READY
-- and lives on outputs (Stage II), never on an immutable header (§6.4 [R3.1]).
CREATE TABLE IF NOT EXISTS authoring_attempt_link (
    attempt_id       text PRIMARY KEY REFERENCES feature_run_action_attempt (attempt_id),
    formula_draft_id text NOT NULL REFERENCES formula_draft (formula_draft_id)
);

-- The coordinator's OWN append-only stream (§6.6): orchestration facts only, sequence-ordered.
CREATE TABLE IF NOT EXISTS feature_run_action_event (
    attempt_id     text NOT NULL REFERENCES feature_run_action_attempt (attempt_id),
    event_sequence integer NOT NULL CHECK (event_sequence >= 1),
    event_kind     text NOT NULL CHECK (btrim(event_kind) <> ''),
    actor_subject  text NOT NULL CHECK (btrim(actor_subject) <> ''),
    detail         jsonb NOT NULL DEFAULT '{}'::jsonb,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (attempt_id, event_sequence)
);

CREATE OR REPLACE FUNCTION feature_run_action_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: an orchestration record that can be rewritten records '
                    'nothing', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER feature_run_action_invocation_no_change
    BEFORE UPDATE OR DELETE ON feature_run_action_invocation
    FOR EACH ROW EXECUTE FUNCTION feature_run_action_write_once();
CREATE OR REPLACE TRIGGER feature_run_action_attempt_no_change
    BEFORE UPDATE OR DELETE ON feature_run_action_attempt
    FOR EACH ROW EXECUTE FUNCTION feature_run_action_write_once();
CREATE OR REPLACE TRIGGER feature_run_action_event_no_change
    BEFORE UPDATE OR DELETE ON feature_run_action_event
    FOR EACH ROW EXECUTE FUNCTION feature_run_action_write_once();
```

- [ ] **Step 4: Run tests** — 5 passed; then `uv run pytest tests/featuregen/db -q` for no regressions.
- [ ] **Step 5: Commit** — `feat(runs): migration 1117 — invocation, attempt headers, subject, link, events`

---

### Task 2: The CAS — `feature_run_state` gets its first code

**Files:**
- Create: `src/featuregen/runs/coordination.py`
- Test: `tests/featuregen/runs/test_coordination.py`

**Interfaces:**
- Produces:
  - `class StaleRunState(RuntimeError)` — carries `.current_version`.
  - `find_invocation(conn, run_id, action, idempotency_key) -> str | None` — the BEFORE-CAS lookup (spec §5 [R3.1]): returns the existing `invocation_id` or None.
  - `@contextmanager run_state_locked(conn, run_id, *, expected_state_version: int)` — lazily mints the row (`INSERT … ON CONFLICT DO NOTHING`), `SELECT … FOR UPDATE`, raises `StaleRunState` on mismatch, yields the current version; the caller writes evidence; on clean exit executes `UPDATE feature_run_state SET state_version = state_version + 1`.
  - `next_attempt_number(conn, run_id, action, stage_subject_id) -> int` — `COALESCE(MAX(attempt_number),0)+1`, called only under the lock.

- [ ] **Step 1: Failing tests**

```python
import pytest
from psycopg.types.json import Jsonb
from featuregen.runs.coordination import (StaleRunState, find_invocation,
                                          next_attempt_number, run_state_locked)
from tests.featuregen.runs._chain import seed_run_chain


def test_lock_mints_lazily_and_increments_on_exit(db):
    seed_run_chain(db, run_id="cas-a")
    with run_state_locked(db, "cas-a", expected_state_version=0) as v:
        assert v == 0
    row = db.execute("SELECT state_version FROM feature_run_state "
                     "WHERE generation_run_id='cas-a'").fetchone()
    assert row == (1,)


def test_stale_version_refuses_with_the_current(db):
    seed_run_chain(db, run_id="cas-b")
    with run_state_locked(db, "cas-b", expected_state_version=0):
        pass
    with pytest.raises(StaleRunState) as e:
        with run_state_locked(db, "cas-b", expected_state_version=0):
            pass
    assert e.value.current_version == 1


def test_an_exception_inside_the_block_does_not_increment(db):
    seed_run_chain(db, run_id="cas-c")
    with pytest.raises(ValueError):
        with run_state_locked(db, "cas-c", expected_state_version=0):
            raise ValueError("evidence write failed")
    # the row exists (mint is its own statement) but the version did not move
    assert db.execute("SELECT state_version FROM feature_run_state "
                      "WHERE generation_run_id='cas-c'").fetchone() == (0,)


def test_idempotency_lookup_precedes_everything(db):
    seed_run_chain(db, run_id="cas-d")
    assert find_invocation(db, "cas-d", "AUTHOR_FORMULA", "k1") is None
    db.execute("INSERT INTO feature_run_action_invocation (invocation_id, generation_run_id, "
               "action, idempotency_key, request_content_hash, requested_by, requested_at) "
               "VALUES ('inv-d', 'cas-d', 'AUTHOR_FORMULA', 'k1', 'h', 'user:p', now())")
    assert find_invocation(db, "cas-d", "AUTHOR_FORMULA", "k1") == "inv-d"


def test_attempt_numbers_are_per_subject(db):
    seed_run_chain(db, run_id="cas-e")
    assert next_attempt_number(db, "cas-e", "AUTHOR_FORMULA", "subj-1") == 1
```

- [ ] **Step 2: Run to verify failure** — module missing.
- [ ] **Step 3: Implement**

```python
"""The run's coordination CAS (spec §5). Idempotency is consulted BEFORE the CAS — a timed-out
retry's first attempt already moved state_version, so CAS-first would answer the retry with a
version conflict and the key could never fire for the one case it exists for."""
from __future__ import annotations

from contextlib import contextmanager


class StaleRunState(RuntimeError):
    def __init__(self, current_version: int):
        super().__init__(f"run state moved: expected version differs (current={current_version}); "
                         "re-read the run and retry with the current version")
        self.current_version = current_version


def find_invocation(conn, run_id: str, action: str, idempotency_key: str) -> str | None:
    row = conn.execute(
        "SELECT invocation_id FROM feature_run_action_invocation "
        "WHERE generation_run_id = %s AND action = %s AND idempotency_key = %s",
        (run_id, action, idempotency_key)).fetchone()
    return row[0] if row else None


@contextmanager
def run_state_locked(conn, run_id: str, *, expected_state_version: int):
    conn.execute("INSERT INTO feature_run_state (generation_run_id) VALUES (%s) "
                 "ON CONFLICT (generation_run_id) DO NOTHING", (run_id,))
    current = conn.execute(
        "SELECT state_version FROM feature_run_state WHERE generation_run_id = %s FOR UPDATE",
        (run_id,)).fetchone()[0]
    if current != expected_state_version:
        raise StaleRunState(current)
    yield current
    conn.execute("UPDATE feature_run_state SET state_version = state_version + 1 "
                 "WHERE generation_run_id = %s", (run_id,))


def next_attempt_number(conn, run_id: str, action: str, stage_subject_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(attempt_number), 0) + 1 FROM feature_run_action_attempt "
        "WHERE generation_run_id = %s AND action = %s AND stage_subject_id = %s",
        (run_id, action, stage_subject_id)).fetchone()[0]
```

- [ ] **Step 4: Run** — 5 passed; `uv run pytest tests/featuregen/runs -q` no regressions.
- [ ] **Step 5: Commit** — `feat(runs): the coordination CAS — idempotency before the version check`

---

### Task 3: The authoring invocation service

**Files:**
- Create: `src/featuregen/runs/invocations.py`
- Test: `tests/featuregen/runs/test_invocations.py`

**Interfaces:**
- Consumes: Task 2's CAS; `authorize_action(conn, *, action, resource_identity_hash, actor_subject, environment_id) -> ActionAuthorizationV1`; `ask/decide(conn, ActionRequestV1(action=…, resource_identity_hash=…), authorization_id=…)`; `retirement_scope_key(*, considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash, definition_revision) -> str`; `request_draft(conn, *, formula_draft_id, considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, requested_by, requested_at, provider_contract_hash=None, strategy_identity_hash=None, now=None) -> tuple[str, bool]` (raises `DraftRetired`, `DraftNotAnAnswer`); Task 4's `record_authoring_plan` + `DEV_SPEND_ENVELOPE`.
- Produces: `start_author_formula(conn, identity: IdentityEnvelope, *, run_id: str, subjects: list[dict], idempotency_key: str, expected_state_version: int, environment_id: str = "dev", now: str) -> dict` where each subject dict carries the five candidate-fact keys plus `authoring_config_hash`. Returns `{"invocation_id", "attempts": [{"attempt_id", "stage_subject_id", "formula_draft_id", "purpose", "attempt_number"}], "state_version"}`.
- Semantics, each a test: idempotent replay returns the stored invocation WITHOUT touching the CAS; per subject → `retirement_scope_key` → `authorize_action` (grantee = `identity.subject` by construction) → `decide`; a refused decision (`allowed=False`) refuses THAT subject with its blockers, all-or-nothing per spec §4.1's atomic gesture (whole invocation aborts — the transaction rolls back, nothing half-written); `request_draft` created=True → purpose `START` + outbox enqueue (Task 5); created=False with a live draft → purpose `ATTACH` (same identity by construction — the scope key IS the draft's subject identity, so `INPUT_IDENTITY_MISMATCH` cannot arise for authoring); `DraftRetired`/`DraftNotAnAnswer` surface as that subject's refusal (again aborting the gesture, with the exception's own message); events written per attempt (`requested`=1, `authorized`=2, `decided`=3, `draft_linked`=4).

- [ ] **Step 1: Failing tests** — the four semantics above, driven through a seeded chain. Seed candidates with `seed_run_chain` + a second considered option; construct identity via `IdentityEnvelope(subject="user:priya", actor_kind="human", authenticated=True, auth_method="test", role_claims=("feature_engineer",))`. Assert: batch of 2 subjects → 1 invocation + 2 headers, each header's `resource_identity_hash == stage_subject_id == retirement_scope_key(**five_facts)`; replay with the same key returns the same invocation_id and writes nothing new (count rows); a stale `expected_state_version` raises `StaleRunState`; a second START for the same subject under a NEW key refuses via the partial index (assert `UniqueViolation` surfaces as the service's typed error `SubjectAlreadyStarted`).
- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** — the service wraps everything in the caller's transaction (no commits); `mint_id("inv")` / `mint_id("fratt")` from `featuregen.idgen`'s existing `mint_id`; `request_content_hash = jcs_sha256({"subjects": sorted(scope_keys)})` (unordered set, the Task 1 ruling); events via plain INSERTs with explicit sequences. Wrap the partial-index violation: `except psycopg.errors.UniqueViolation as e: raise SubjectAlreadyStarted(scope_key) from e`.
- [ ] **Step 4: Run** — all pass; runs suite green.
- [ ] **Step 5: Commit** — `feat(runs): start_author_formula — one governed gesture, per-subject headers`

---

### Task 4: The sealed input — 1104's plan row + the spend envelope

**Files:**
- Create: `src/featuregen/runs/authoring_plan.py`
- Test: `tests/featuregen/runs/test_authoring_plan.py`

**Interfaces:**
- Consumes: `authorize_spend(conn, *, action, actor_subject, job_identity, member_identities, provider_contract_hash, max_calls, max_tokens, currency, max_cost, pricing_version, expires_at) -> str`; 1104's `formula_draft_authoring_plan` table (read its exact columns from `1104_authoring_subject_and_plan.sql` before writing — the sweep records it is schema-only, no writer exists; the CHECK admits strategies `LLM_AUTHORED` and `REVIEWED_RECIPE_BLUEPRINT` only).
- Produces:
  - `DEV_SPEND_ENVELOPE = {"max_calls": 2, "max_tokens": 200_000, "currency": "USD", "max_cost": Decimal("5"), "pricing_version": "dev-v1"}` — the development budget for one draft (author + critic), a named constant so changing the budget is one diff.
  - `record_authoring_plan(conn, *, formula_draft_id, strategy: str, provider_contract_hash: str, authoring_config_hash: str, spend_authorization_id: str, expectation_generation=None) -> None` — the INSERT; strategy is always `'LLM_AUTHORED'` in Stage I (the deterministic lane is child-plan work; hook: `formula_draft_worker` always drives the LLM today).
  - `spend_for_draft(conn, *, identity_subject: str, formula_identity_hash: str, provider_contract_hash: str, now, ttl_hours: int = 24) -> str` — wraps `authorize_spend(action='AUTHOR_FORMULA', job_identity=formula_identity_hash, member_identities=[formula_identity_hash], …, expires_at=<now+ttl>)`.
- Ruling carried from the sweep: the worker RE-READS the plan and never re-resolves (1104:11-14); Stage I's worker change (Task 6) asserts the plan row exists for run-centric drafts.

- [ ] **Step 1: Failing tests** — plan row inserts and is 1:1 (second insert for the same draft hits the PK); a plan row without a real spend authorization id fails its FK (1105); `spend_for_draft` is idempotent (same identity+contract → same `sa-…` id).
- [ ] **Step 2–4: red → implement → green** (read 1104's exact column list first; the plan serves the schema).
- [ ] **Step 5: Commit** — `feat(runs): the authoring plan row + the development spend envelope`

---

### Task 5: Wire the draft mint into the gesture (outbox in the same transaction)

**Files:**
- Modify: `src/featuregen/runs/invocations.py` (the created=True branch calls Task 4 then enqueues)
- Test: extend `tests/featuregen/runs/test_invocations.py`

**Interfaces:**
- Consumes: how `api/routes/formula_drafts.py:138-160` enqueues today — read it and reuse the SAME outbox/enqueue helper it calls (the request row and queue message must land in one transaction, the route's own documented rule); `formula_identity(...)` (`formula_draft_store.py:260`) for the spend's job identity.
- Produces: for a `START`, the gesture's single transaction holds: invocation + header + subject child + spend authorization + plan row + draft row + outbox message + events. A crash anywhere rolls back everything — no draft with nobody to drive it, no header naming an absent draft.
- Order inside the subject loop (each numbered step is one INSERT/call): scope key → `authorize_action` → `decide` (refusal aborts) → `formula_identity` → `spend_for_draft` → `request_draft(provider_contract_hash=…, strategy_identity_hash=…)` → created? `record_authoring_plan` + enqueue : ATTACH → link row → events.

- [ ] **Step 1: Failing test** — a START gesture leaves exactly one outbox/queue row for the new draft (assert on the queue table the enqueue helper writes; find its name from the helper); an ATTACH gesture (draft already live) leaves ZERO new queue rows; a `decide` refusal (seed a refused authorization by constructing one with `permission_result='refused'` via direct INSERT with the trigger pattern from `test_action_authorization.py:146-147`) rolls back the entire gesture — zero invocations, zero drafts, zero queue rows.
- [ ] **Step 2–4: red → implement → green.**
- [ ] **Step 5: Commit** — `feat(runs): the gesture mints the draft, plan, spend and outbox atomically`

---

### Task 6: The worker honours the plan and the spend

**Files:**
- Modify: `src/featuregen/overlay/upload/formula_draft_worker.py` (before provider dispatch)
- Test: `tests/featuregen/overlay/upload/test_formula_draft_worker_spend.py`

**Interfaces:**
- Consumes: `reserve_spend(conn, *, spend_authorization_id, calls, tokens, cost, now, …) -> str` (raises `SpendExhausted`; takes `FOR UPDATE` on the authorization — the worker must be inside a transaction there); `settle_spend(conn, reservation_id, *, actual_calls, actual_tokens, actual_cost)`; the plan row from Task 4.
- Produces: for a draft that HAS a plan row (run-centric), the worker reserves before each provider call and settles after, using the plan's `spend_authorization_id`; `SpendExhausted` → the draft moves to `BLOCKED` with blocker code `LLM_SPEND_EXHAUSTED` (a NEW reason code — parent §5's three-part-commit applies: code + disposition row + test). For a draft with NO plan row (legacy direct route), behaviour is UNCHANGED — the legacy path keeps working until its route becomes an adapter (Stage II).
- Hazard carried from the sweep verbatim: `record_dispatch` opens its OWN connection and commits independently; the reservation must be on the WORKER's conn before `record_dispatch` fires, and `scope_locked`'s only-if-autocommit transaction shape (`retirement_scope.py:142-150`) is the template for not double-opening transactions.

- [ ] **Step 1: Failing tests** — run-centric draft: worker reserves then settles (rows in the reservation/settlement tables); exhausted envelope → draft BLOCKED with `LLM_SPEND_EXHAUSTED`, provider NEVER called (assert the fake client's call count is 0); legacy draft without a plan row: worker path identical to today (no reservation rows).
- [ ] **Step 2–4: red → implement → green**; run the full worker suite `uv run pytest tests/featuregen/overlay/upload -q -k worker`.
- [ ] **Step 5: Commit** — `feat(runs): the worker reserves and settles spend for run-centric drafts`

---

### Task 7: The API — `/plan` and `/start` for AUTHOR_FORMULA

**Files:**
- Modify: `src/featuregen/api/routes/feature_runs.py`
- Test: `tests/featuregen/api/test_feature_run_actions.py`

**Interfaces:**
- Consumes: Tasks 2–5; `run_detail`'s visibility rule; `get_identity`.
- Produces:
  - `POST /feature-runs/{run_id}/actions/AUTHOR_FORMULA/plan` — read-only preflight: per subject, the scope key, `ask()`'s verdict with blockers verbatim, and `cost: null` with the literal docstring ruling "no governed estimator exists; the spend ENVELOPE is disclosed instead" (returns `DEV_SPEND_ENVELOPE` fields).
  - `POST /feature-runs/{run_id}/actions/AUTHOR_FORMULA/start` — body `{"idempotency_key", "expected_state_version", "subjects": [five-fact dicts + authoring_config_hash]}`; 404 for absent/invisible run (byte-identical, the foundation's rule); 409 with `{"code": "STALE_RUN_STATE", "current_version": n}` on `StaleRunState`; 409 `SUBJECT_ALREADY_STARTED`; 403 never — object policy is the 404; the gesture's refusals return 422 with the decision's blockers verbatim.
  - Object-level policy on BOTH: owner or platform_admin, applied before anything (reuse `visibility_where` in a one-row check — the same 404 discipline as the reads).
- Note the write-authorization stack: `require_feature_generate` at the router level for these two POSTs (the read routes keep `require_feature_read`) — split the router or add per-route dependencies.

- [ ] **Step 1: Failing tests** — owner starts a batch of 2 → 202 with invocation + 2 attempts; replay same key → 200 SAME invocation id, no new rows; stale version → 409 with current_version; non-owner → 404 identical to absent; `data_owner` (lacks feature:generate) → 403 from the permission gate; plan endpoint returns blockers + envelope and writes NOTHING (assert zero decision rows after /plan — `ask` never writes).
- [ ] **Step 2–4: red → implement → green**; then `uv run pytest tests/featuregen/api -q` once.
- [ ] **Step 5: Commit** — `feat(runs): plan + start endpoints for AUTHOR_FORMULA`

---

### Task 8: The projection learns attempts — and pays three parked debts

**Files:**
- Modify: `src/featuregen/runs/projection.py`, `tests/featuregen/runs/test_projection_detail.py`
- Modify: `frontend/src/screens/RunDetailScreen.tsx` + test (STATE_LABEL completion only, 4 lines)

**Interfaces:**
- Produces, in `run_detail`: each authoring row gains `attempt_id`, `attempt_number`, `attempt_purpose` when a header links its draft (LEFT JOIN `authoring_attempt_link`→`feature_run_action_attempt`); `invocations: [{invocation_id, action, requested_by, requested_at, attempt_count}]`.
- Pays the ledger's parked items: (1) `BIND_SELECTIONS` derives from evidence — `SELECT count(*) FROM selection_formula_binding b JOIN feature_selection_revision s ON s.revision_id=b.selection_revision_id JOIN contract_considered_revision c ON c.considered_revision_id=s.considered_revision_id WHERE c.generation_run_id=%s` → `SUCCEEDED` when >0 (replacing `NOT_STARTED` hardcode at `projection.py:199` and `bind_selections: []` with the binding list); (2) the switch-off+pin-absent precedence test (delenv + `_drop_the_pin`, expect `GENERATION_DISABLED`); (3) `STATE_LABEL` completed to §7's 11 values (`WAITING_FOR_USER`, `UNKNOWN`, `NOT_APPLICABLE`, `OUTPUT_BINDING_INCOMPLETE` added).
- Adds the second TOTAL rail mapping: `RAIL_FROM_GENERATION_STATUS` over 1092's seven values (`REQUESTED/CLAIMED/RUNNING→IN_PROGRESS, SUCCEEDED→SUCCEEDED, REFUSED→BLOCKED, FAILED→FAILED, CANCELLED→CANCELLED`), pinned by an exhaustiveness test against the CHECK's literal set — consumed by Stage II, proven total now.

- [ ] **Step 1: Failing tests** — a linked draft's row carries the attempt fields; an unlinked (legacy) draft's row has them `None`; BIND_SELECTIONS shows SUCCEEDED after a binding exists (use `bind_ready_formula` from `tests/featuregen/materialize/crosswalk_fixtures.py` — it reads facts from the selection and needs a non-empty `formula_json`); the precedence test; the two mapping-totality tests.
- [ ] **Step 2–4: red → implement → green** (backend + the 4-line frontend map + its one assertion).
- [ ] **Step 5: Commit** — `feat(runs): attempts in the detail projection; three parked debts paid`

---

### Task 9: The pre-pin guard moves into the store

**Files:**
- Modify: `src/featuregen/overlay/upload/build_set_store.py` (`record_build_set`, first statement), `src/featuregen/api/routes/build_sets.py` (its `_refuse_pre_pin` now cites the store as the authority)
- Test: `tests/featuregen/materialize/test_store_pre_pin_guard.py`

**Interfaces:**
- Consumes: `pin_exists`/`PRE_PIN_REASON_CODE` from `src/featuregen/runs/pin.py`.
- Produces: `record_build_set` raises `BuildSetPrePin(RuntimeError)` (carrying `PRE_PIN_REASON_CODE`) when the pin table is absent — so the Stage II run-centric caller cannot route around the route-level guard (spec §9's follow-up, now load-bearing: the sweep proved the store has zero references today). The route maps the exception to its existing 409.

- [ ] **Step 1: Failing test** — with the pin dropped (rolled-back `DROP TABLE … CASCADE`, the established `_drop_the_pin` pattern), `record_build_set` raises `BuildSetPrePin`; with it present, unchanged behaviour (existing store tests stay green).
- [ ] **Step 2–4: red → implement → green**; run `tests/featuregen/api/routes/test_build_set_pre_pin_refusal.py` + the build-set store suites.
- [ ] **Step 5: Commit** — `feat(runs): the pre-pin refusal lives in the store, not only the route`

---

### Task 10: The trigger button — the ONLY thing that starts authoring

**Files:**
- Modify: `frontend/src/api.ts` (types + `planAuthorFormula` + `startAuthorFormula`), `frontend/src/screens/RunDetailScreen.tsx` + tests
- Modify: `deploy/kind/nginx.conf` / `frontend/vite.config.ts` ONLY if the parity test demands new prefixes (POSTs ride the existing `/feature-runs` prefix — expect no change; say so in the report if so).

**Interfaces:**
- Consumes: Task 7's endpoints; `FeatureRunDetail.state_version` — ADD `state_version` to the detail response in Task 8 (the client must send `expected_state_version`; a screen that invents 0 would stale-conflict forever).
- Produces: on the run detail, candidates with no draft get one checkbox each and ONE `Start formula authoring (n)` button — in an onClick only, never an effect (FeatureExecutionScreen's acceptance rule, restated in the file header); the click calls `/plan` first and shows blockers verbatim with the envelope; confirm calls `/start` with a client-minted `idempotency_key` (`crypto.randomUUID()`) and the detail's `state_version`; `STALE_RUN_STATE` → re-fetch and tell the user, never auto-retry the start; success re-fetches the detail (reads are safe).
- Tests: mock api; assert the button exists ONLY for draftless candidates; blockers render verbatim; a 409 stale response renders the refresh prompt and does NOT re-POST (spy call count stays 1).

- [ ] **Steps: red → implement → green** — `npx vitest run src/screens/RunDetailScreen.test.tsx`, then the full frontend suite once, `tsc -b` clean.
- [ ] **Commit** — `feat(runs): the start-authoring gesture on the run detail`

---

### Task 11: The journey test + full suites

**Files:**
- Create: `tests/featuregen/api/test_run_authoring_journey.py`

- [ ] **Step 1: The journey, one test** — seed a chain + identity row; `GET /feature-runs/{id}` (rail shows AUTHOR_FORMULA NOT_STARTED) → `POST …/AUTHOR_FORMULA/plan` (no rows written) → `POST …/start` (202; invocation + header + draft + plan + spend + queue row all present) → drive the worker once with the FakeLLM (the worker suite's existing fake — reserves, settles) → `GET /feature-runs/{id}` again: authoring row READY/SUCCEEDED with `attempt_id` populated, `state_version` incremented. Assert the grantee rule bites: a second user starting on the same run gets 404 (invisible), and platform_admin CAN start (spec §11 alignment).
- [ ] **Step 2: Full suites** — `uv run pytest tests -q` (read the summary line itself) and `cd frontend && npx vitest run`; both 0 failed, counts ≥ the Task 0 baseline.
- [ ] **Step 3: STOP — no deploy.** Migrations 1117 (and 1106 if newly landed) are files only; the deploy runbook order is 1115 → 1116 → 1117 → image, and the §0.1.0 development policy governs who may trigger.
- [ ] **Step 4: Commit** — `test(runs): the first run-centric journey — trigger to READY through governance`

---

## Self-review notes (applied)

- **Spec coverage:** §4.1 invocation → T1/T3 · §5 CAS/idempotency/START-guard/endpoints → T2/T7 · §6.3 typed subject → T1 · §6.4 header group + ATTACH → T1/T3 · §6.6 events → T1/T3 · §0.1.3 authorize+decide+spend before outbox → T3/T4/T5 · worker re-reads the plan + spend enforcement → T6 · §9 store guard → T9 · parked debts → T8 · journey → T11. Deliberately absent (Stage II): `feature_run_action_input` + sealing (preview-shaped), `preview_attempt_link` + the `generation_request` UNIQUE constraint, §6.5 output binding + `OUTPUT_BINDING_INCOMPLETE`, `/re-execute` + canonical hash, verification/publication mappings, `GENERATION_DISABLED` gating of trigger endpoints beyond the switch check the routes inherit from `require_generation_enabled`'s absence here (authoring is NOT behind the V2 switch — the draft lane predates it; T7's routes need no switch gate, and the rail's authoring column never claimed one).
- **Known execution risks, named:** 1104's exact column list (T4 reads the file first — the plan row's columns here are indicative, the schema wins); the outbox helper's real name (T5 reads the route); `formula_draft_authoring_plan`'s CHECK vocabulary; the queue-table name for T5's assertions; whether `feature:generate` maps to the right role bundle for T7's 403 test (read `permissions.py`).
- **Type consistency:** `StaleRunState.current_version` (T2) ↔ T7's 409 body ↔ T10's handling; `DEV_SPEND_ENVELOPE` (T4) ↔ T7's plan response; five-fact subject dicts spelled identically in T1's child table, T3's service, T7's body, T10's client.
