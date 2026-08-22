# Feature Run Spine — Foundation Increment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A read-only run spine — durable run identity written at creation, an intent-grouped runs dashboard, run detail with honest stage states, and the backend refusal that protects migration 1101's no-backfill branch. No triggers, no re-run, no fork, no cancel, no profile-write endpoints.

**Architecture:** Three new spine tables (immutable `feature_run_identity` as a composite-FK-enforced chain, mutable `feature_run_profile` display row, `feature_run_state` CAS table shipped empty), two lineage FKs hardening the considered-revision bridge, an identity writer joined to the existing run-creation transaction, and a projection layer that DERIVES everything else from existing domain stores — the spine records no lifecycle of its own. New API: `GET /feature-runs` + `GET /feature-runs/{run_id}` with object-level read policy applied inside the query. Frontend: `#/runs` list + `#/runs/{run_id}` detail.

**Tech Stack:** Python 3 / FastAPI / psycopg / PostgreSQL migrations (file-based SQL, ledgered) · React + TypeScript + Vite + Vitest.

**Spec:** `docs/superpowers/specs/2026-08-22-feature-run-spine-design.md` (revision 3.1, approved). The plan argues from the spec; executors read both.

## Global Constraints

- **Worktree:** `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/asset-detail-reapply`, branch `feature/asset-detail-reapply`. It contains OTHER PEOPLE'S uncommitted work (the parent plan doc is modified; `src/featuregen/db/migrations/1100_action_authorization_revision.sql`, `src/featuregen/materialize/action_authorization.py` and its test are untracked). **`git add <exact path>` only — NEVER `git add -A`/`-u`. NEVER `git stash` or `git stash pop`** (shared stash stack holds other people's work).
- **Read-only increment:** no run-centric triggers, no invocations, no attempt headers, no fork, no cancel, no archive/profile-write endpoints. `feature_run_state` ships as a table with **zero rows minted**.
- **API tests must never `conn.commit()`** — the shared test connection rolls back on teardown; a commit leaks into every later suite.
- **Never deploy or upload to the live cluster** — code + tests only; deploys are an explicit owner decision.
- The one refusal reason code is **`BUILD_SET_DECLARATION_WITHHELD_PRE_PIN`** (spec §7).
- Run ids are **opaque**; both `grun_*` and `fgr_*` are runs; nothing parses a prefix.
- Migration files are idempotent (`CREATE TABLE IF NOT EXISTS` / `CREATE OR REPLACE`) and carry the repo's explanatory header including a `-- NOT APPLIED. This file is written, not run.` line.
- Roles/identity are server-derived; nothing reads roles from a request body.
- Baseline suites: backend 13154 passed / 20 skipped, frontend 985 — every task ends green.
- Migration numbers **1115/1116 are an assumption until Task 0's amendment is approved** — Task 0 is a hard gate for Tasks 1–2.

---

### Task 0: Reserve migrations 1115/1116 in the parent plan's §17 ledger (OWNER GATE)

**Files:**
- Modify: `docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md` (§17 allocation table)

**Interfaces:**
- Produces: the reserved numbers `1115_feature_run_spine_foundation.sql` and `1116_run_lineage_considered_fks.sql` that Tasks 1–2 create.

- [ ] **Step 1: Check the parent plan file for third-party uncommitted edits**

Run: `git status --short docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md`
If it prints ` M …` (dirty): **STOP. Do not edit or stage this file.** Report to the owner that the §17 amendment is blocked on in-flight edits to the same file, and wait. Only proceed when clean.

- [ ] **Step 2: Append two rows to the §17 allocation table**

Locate the table ending with the row for 1114 (`recipe_compiler_eval_attempt`). Append:

```markdown
| 1115 | **run-spine spec §6/§13 (foundation)** | `feature_run_identity` (composite-FK chain, write-once) + `feature_run_profile` + `feature_run_state` + the three additive UNIQUE chain indexes on `contract_generation_input` / `contract_considered_revision` / `catalog_metadata_snapshot` | foundation |
| 1116 | **run-spine spec §9 (foundation)** | simple FKs: `formula_draft.considered_revision_id` and `feature_selection_revision.considered_revision_id` → `contract_considered_revision` (live-measured 0 orphans) | foundation |
```

Directly beneath the table, add:

```markdown
▲ **Ordering note (run-spine foundation).** 1115/1116 may APPLY before 1104–1114 exist: the two
blocks are mutually independent (1115/1116's FKs reach only ≤1024 tables; nothing in 1100–1114
references a spine table), and `migrations.py` applies pending files deterministically by name with
a checksum ledger. This is a DOCUMENTED interleaving of two independent workstreams, not an
accidental out-of-order convention — the rule "numbers match apply order" holds within each
workstream.
```

- [ ] **Step 3: STOP — owner approval**

This amends an authoritative plan. Present the diff (`git diff docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md`) and wait for explicit approval. If the owner assigns different numbers, rename in Tasks 1–2 accordingly (files do not exist yet — renaming is free).

- [ ] **Step 4: Commit (exact path only)**

```bash
git add docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md
git commit -m "docs: reserve migrations 1115/1116 for the run-spine foundation (§17 amendment)"
```

---

### Task 1: Migration 1115 — the three spine tables and the chain indexes

**Files:**
- Create: `src/featuregen/db/migrations/1115_feature_run_spine_foundation.sql`
- Create: `tests/featuregen/db/test_migration_1115.py`
- Create: `tests/featuregen/runs/__init__.py` (empty), `tests/featuregen/runs/_chain.py` (seed helper)

**Interfaces:**
- Produces: tables `feature_run_identity`, `feature_run_profile`, `feature_run_state`; helper `seed_run_chain(conn, *, run_id, intent_id=None, considered_revision_id=None, snapshot_id=None, scope_id=None, recognition_id=None, subject='u1') -> dict` returning the ids it minted. Later tasks import it as `from tests.featuregen.runs._chain import seed_run_chain`.

- [ ] **Step 1: Write the seed helper** (`tests/featuregen/runs/_chain.py`)

```python
"""Seed one complete intent -> recognition -> run -> scope -> input -> considered -> snapshot chain.

Every parent 1115's composite FKs reference, with minimal NOT NULL columns, so a test can mint a
chain in one call. Ids default from run_id so two chains never collide."""
from psycopg.types.json import Jsonb


def seed_run_chain(conn, *, run_id, intent_id=None, considered_revision_id=None,
                   snapshot_id=None, scope_id=None, recognition_id=None, subject="u1"):
    intent_id = intent_id or f"{run_id}-intent"
    considered_revision_id = considered_revision_id or f"{run_id}-ccr"
    snapshot_id = snapshot_id or f"{run_id}-snap"
    scope_id = scope_id or f"{run_id}-scope"
    recognition_id = recognition_id or f"{run_id}-rec"
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis') ON CONFLICT DO NOTHING", (intent_id,))
    conn.execute(
        "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
        "VALUES (%s, %s, %s, '{}') ON CONFLICT DO NOTHING",
        (run_id, intent_id, Jsonb({"subject": subject})))
    conn.execute(
        "INSERT INTO intent_recognition_attempt (recognition_id, intent_id, input_hash, status, "
        "taxonomy_version, applicability_mapping_version, recognizer_model_id, prompt_version, "
        "recipe_registry_version) VALUES (%s, %s, %s, 'resolved', 'v1', 'v1', 'm', 'p1', 'r1') "
        "ON CONFLICT DO NOTHING", (recognition_id, intent_id, f"{run_id}-ih"))
    conn.execute(
        "INSERT INTO confirmed_generation_scope (scope_id, intent_id, generation_run_id, "
        "recognition_id, expansion, scope_mode, confirmation_source, confirmed_by) "
        "VALUES (%s, %s, %s, %s, 'none', 'scoped', 'user_confirmed', %s) ON CONFLICT DO NOTHING",
        (scope_id, intent_id, run_id, recognition_id, subject))
    conn.execute(
        "INSERT INTO contract_generation_input (generation_run_id, intent_id, recognition_id, "
        "confirmed_scope_id, redacted_hypothesis, recognition_input_content_hash, "
        "generation_input_content_hash, created_by) "
        "VALUES (%s, %s, %s, %s, 'h', 'rh', 'gh', %s) ON CONFLICT DO NOTHING",
        (run_id, intent_id, recognition_id, scope_id, Jsonb({"subject": subject})))
    conn.execute(
        "INSERT INTO catalog_metadata_snapshot (snapshot_id, generation_run_id, read_scope_hash, "
        "isolation_level, content_hash) VALUES (%s, %s, 'rs', 'repeatable read', 'ch') "
        "ON CONFLICT DO NOTHING", (snapshot_id, run_id))
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, metadata_snapshot_content_hash, "
        "considered_json, considered_content_hash, canonicalization_version) "
        "VALUES (%s, %s, %s, %s, 'ch', '{}'::jsonb, 'cch', 'v1') ON CONFLICT DO NOTHING",
        (considered_revision_id, intent_id, run_id, snapshot_id))
    return {"run_id": run_id, "intent_id": intent_id,
            "considered_revision_id": considered_revision_id, "snapshot_id": snapshot_id,
            "scope_id": scope_id, "recognition_id": recognition_id, "subject": subject}
```

Note: if `contract_generation_input`'s 1024 lineage trigger or `catalog_metadata_snapshot`'s NOT NULLs reject a column set here, open the migration (1024/1006/0974/0962/1021) and fix the helper's columns — the helper serves the schemas, never the reverse.

- [ ] **Step 2: Write the failing migration test** (`tests/featuregen/db/test_migration_1115.py`)

```python
"""Migration 1115: the spine's three tables, their triggers, and the chain's composite FKs."""
import psycopg
import pytest
from psycopg.types.json import Jsonb

from tests.featuregen.runs._chain import seed_run_chain

_IDENTITY_COLS = (
    "generation_run_id, workflow_definition_version, intent_id, confirmed_scope_id, "
    "generation_input_content_hash, considered_revision_id, considered_content_hash, "
    "metadata_snapshot_id, metadata_snapshot_content_hash, owner_subject, owner_tenant, "
    "root_generation_run_id, run_identity_hash, created_by")


def _insert_identity(conn, c, run_id=None):
    rid = run_id or c["run_id"]
    conn.execute(
        f"INSERT INTO feature_run_identity ({_IDENTITY_COLS}) "
        "VALUES (%s, 'V1', %s, %s, 'gh', %s, 'cch', %s, 'ch', %s, NULL, %s, 'idh', 'test')",
        (rid, c["intent_id"], c["scope_id"], c["considered_revision_id"],
         c["snapshot_id"], c["subject"], rid))


def test_identity_row_inserts_when_the_chain_exists(db):
    c = seed_run_chain(db, run_id="m1115-a")
    _insert_identity(db, c)
    row = db.execute("SELECT workflow_definition_version, owner_subject "
                     "FROM feature_run_identity WHERE generation_run_id='m1115-a'").fetchone()
    assert row == ("V1", "u1")


def test_identity_is_write_once(db):
    c = seed_run_chain(db, run_id="m1115-b")
    _insert_identity(db, c)
    with pytest.raises(psycopg.errors.RaiseException):
        db.execute("UPDATE feature_run_identity SET owner_subject='x' "
                   "WHERE generation_run_id='m1115-b'")


def test_chain_fk_refuses_a_considered_revision_from_another_run(db):
    a = seed_run_chain(db, run_id="m1115-c1")
    b = seed_run_chain(db, run_id="m1115-c2")
    mixed = {**a, "considered_revision_id": b["considered_revision_id"]}
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _insert_identity(db, mixed)


def test_root_parent_check(db):
    c = seed_run_chain(db, run_id="m1115-d")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            f"INSERT INTO feature_run_identity ({_IDENTITY_COLS}) "
            "VALUES ('m1115-d', 'V1', %s, %s, 'gh', %s, 'cch', %s, 'ch', 'u1', NULL, "
            "'someone-else', 'idh', 'test')",  # parent NULL but root != self
            (c["intent_id"], c["scope_id"], c["considered_revision_id"], c["snapshot_id"]))


def test_profile_and_state_tables_exist_and_state_ships_empty(db):
    db.execute("INSERT INTO feature_run_profile (generation_run_id, display_name) "
               "VALUES ('m1115-e', 'My run')")
    assert db.execute("SELECT count(*) FROM feature_run_state").fetchone()[0] == 0
```

- [ ] **Step 3: Run tests to verify they fail** — `uv run pytest tests/featuregen/db/test_migration_1115.py -x -q` — Expected: FAIL (`relation "feature_run_identity" does not exist`).

- [ ] **Step 4: Write the migration** (`src/featuregen/db/migrations/1115_feature_run_spine_foundation.sql`)

```sql
-- src/featuregen/db/migrations/1115_feature_run_spine_foundation.sql
-- Run-spine FOUNDATION (spec 2026-08-22-feature-run-spine-design.md rev 3.1, §6.1/§5/§13).
--
-- IDENTITY IS A CHAIN THE DATABASE REFUSES TO MIS-ASSEMBLE. Every business input is pinned by a
-- COMPOSITE foreign key carrying generation_run_id, so a row cannot combine valid identifiers from
-- unrelated runs and hash the false combination. All chain columns are NOT NULL — MATCH SIMPLE
-- cannot disarm the checks.
-- NO FORK COLUMNS, deliberately (spec §6.1 [R3.1]): forked_from_attempt_id / fork_plan_revision_id
-- arrive with the actionable increment's tables; a foundation FK to a table it does not create
-- would fail on a fresh database.
-- feature_run_state ships EMPTY: rows are minted lazily by the first run-centric mutation, and the
-- foundation performs none (read-only increment).
-- NOT APPLIED. This file is written, not run.

-- additive UNIQUE supersets of existing PKs, so the composite FKs below have targets
CREATE UNIQUE INDEX IF NOT EXISTS contract_generation_input_chain_key
    ON contract_generation_input (generation_run_id, intent_id, confirmed_scope_id);
CREATE UNIQUE INDEX IF NOT EXISTS contract_considered_revision_chain_key
    ON contract_considered_revision (considered_revision_id, generation_run_id, intent_id);
CREATE UNIQUE INDEX IF NOT EXISTS catalog_metadata_snapshot_chain_key
    ON catalog_metadata_snapshot (snapshot_id, generation_run_id);

CREATE TABLE IF NOT EXISTS feature_run_identity (
    generation_run_id  text PRIMARY KEY
        REFERENCES feature_generation_run (generation_run_id),
    workflow_definition_version text NOT NULL CHECK (workflow_definition_version = 'V1'),
    intent_id                   text NOT NULL,
    confirmed_scope_id          text NOT NULL,
    generation_input_content_hash text NOT NULL CHECK (btrim(generation_input_content_hash) <> ''),
    considered_revision_id      text NOT NULL,
    considered_content_hash     text NOT NULL CHECK (btrim(considered_content_hash) <> ''),
    metadata_snapshot_id        text NOT NULL,
    metadata_snapshot_content_hash text NOT NULL CHECK (btrim(metadata_snapshot_content_hash) <> ''),
    owner_subject               text NOT NULL CHECK (btrim(owner_subject) <> ''),
    owner_tenant                text NULL,
    root_generation_run_id      text NOT NULL,
    parent_generation_run_id    text NULL REFERENCES feature_run_identity (generation_run_id),
    run_identity_hash           text NOT NULL CHECK (btrim(run_identity_hash) <> ''),
    created_by                  text NOT NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),

    -- a root points to itself; parent NULL exactly for a root (spec §6.1)
    CONSTRAINT feature_run_identity_root_shape CHECK (
        (parent_generation_run_id IS NULL) = (generation_run_id = root_generation_run_id)),
    FOREIGN KEY (generation_run_id, intent_id, confirmed_scope_id)
        REFERENCES contract_generation_input (generation_run_id, intent_id, confirmed_scope_id),
    FOREIGN KEY (considered_revision_id, generation_run_id, intent_id)
        REFERENCES contract_considered_revision (considered_revision_id, generation_run_id, intent_id),
    FOREIGN KEY (metadata_snapshot_id, generation_run_id)
        REFERENCES catalog_metadata_snapshot (snapshot_id, generation_run_id)
);

CREATE OR REPLACE FUNCTION feature_run_identity_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'feature_run_identity is write-once: % not allowed on %',
        TG_OP, COALESCE(OLD.generation_run_id, '?');
END;
$$ LANGUAGE plpgsql;
CREATE OR REPLACE TRIGGER feature_run_identity_no_mutation
    BEFORE UPDATE OR DELETE ON feature_run_identity
    FOR EACH ROW EXECUTE FUNCTION feature_run_identity_write_once();

-- mutable DISPLAY metadata, identity-free (renaming a run re-keys nothing)
CREATE TABLE IF NOT EXISTS feature_run_profile (
    generation_run_id text PRIMARY KEY,
    display_name      text NULL,
    description       text NULL,
    archived          boolean NOT NULL DEFAULT false,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

-- the only mutable COORDINATION row per run; rows minted lazily, NONE in the foundation
CREATE TABLE IF NOT EXISTS feature_run_state (
    generation_run_id text PRIMARY KEY,
    state_version     bigint NOT NULL DEFAULT 0
);
```

- [ ] **Step 5: Run tests to verify they pass** — `uv run pytest tests/featuregen/db/test_migration_1115.py -q` — Expected: 5 passed. (The session fixture applies all migration files; a fresh test session picks 1115 up automatically.)

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/db/migrations/1115_feature_run_spine_foundation.sql tests/featuregen/db/test_migration_1115.py tests/featuregen/runs/__init__.py tests/featuregen/runs/_chain.py
git commit -m "feat(runs): migration 1115 — run identity chain, profile, state (foundation)"
```

---

### Task 2: Migration 1116 — the considered-revision lineage FKs

**Files:**
- Create: `src/featuregen/db/migrations/1116_run_lineage_considered_fks.sql`
- Create: `tests/featuregen/db/test_migration_1116.py`
- Modify: any test that seeds `formula_draft` / `feature_selection_revision` with a fabricated `considered_revision_id` (found by running the suite; fix pattern below)

**Interfaces:**
- Consumes: `seed_run_chain` from Task 1.
- Produces: FKs `formula_draft_considered_fk`, `feature_selection_revision_considered_fk`.

- [ ] **Step 1: Write the failing test**

```python
"""Migration 1116: the bridge is a constraint, not a convention (spec §9)."""
import psycopg
import pytest


def test_orphan_formula_draft_is_refused(db):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
            "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
            "definition_revision, formula_identity_hash, state, requested_by, requested_at) "
            "VALUES ('m1116-d', 'no-such-revision', 'o1', 'p', 'c', 'a', '', 'fih-m1116', "
            "'REQUESTED', 'u1', '2026-08-23T00:00:00Z')")
```

(If `formula_draft`'s NOT NULL column set differs, read `1090_formula_draft.sql` and match it — the test's point is only the FK violation.)

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/db/test_migration_1116.py -x -q` — Expected: FAIL (insert succeeds; no FK yet).

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/1116_run_lineage_considered_fks.sql
-- The considered-revision bridge, enforced (run-spine spec §9). Both columns were bare
-- `text NOT NULL CHECK` (1072:87, 1090:50) — a draft or selection could name a considered revision
-- that does not exist, and the run projection would silently drop it. Live-measured 2026-08-23:
-- 0 orphans in both tables (7 draft rows, 0 selection rows), so plain ADD CONSTRAINT validates.
-- Idempotent via the guarded DO block (ADD CONSTRAINT has no IF NOT EXISTS).
-- NOT APPLIED. This file is written, not run.
DO $$ BEGIN
    ALTER TABLE formula_draft
        ADD CONSTRAINT formula_draft_considered_fk
        FOREIGN KEY (considered_revision_id)
        REFERENCES contract_considered_revision (considered_revision_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
DO $$ BEGIN
    ALTER TABLE feature_selection_revision
        ADD CONSTRAINT feature_selection_revision_considered_fk
        FOREIGN KEY (considered_revision_id)
        REFERENCES contract_considered_revision (considered_revision_id);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
```

- [ ] **Step 4: Run the new test** — Expected: PASS.

- [ ] **Step 5: Run the suites that seed drafts/selections, fix seeds with the helper**

Run: `uv run pytest tests/featuregen -q 2>&1 | tail -20`
Expected failure mode: `ForeignKeyViolation … formula_draft_considered_fk` in tests that insert drafts/selections against fabricated considered ids (~10 files each were grep-counted). Fix pattern — in each failing test file, before the draft/selection insert:

```python
from tests.featuregen.runs._chain import seed_run_chain
seed_run_chain(db, run_id="<test-local-id>", considered_revision_id="<the id the test uses>")
```

Do not change what any test asserts — only make its seed satisfy the new constraint. Re-run until the full backend suite is green at the baseline count (+ the new tests).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/db/migrations/1116_run_lineage_considered_fks.sql tests/featuregen/db/test_migration_1116.py
git add <each test file you fixed, by exact path>
git commit -m "feat(runs): migration 1116 — enforce the considered-revision bridge"
```

---

### Task 3: The identity writer — `record_run_identity`

**Files:**
- Create: `src/featuregen/runs/__init__.py` (empty), `src/featuregen/runs/run_identity.py`
- Test: `tests/featuregen/runs/test_run_identity.py`

**Interfaces:**
- Consumes: `jcs_sha256` from `featuregen.canonical`; `IdentityEnvelope` from `featuregen.contracts.envelopes`.
- Produces: `record_run_identity(conn, generation_run_id: str, actor: IdentityEnvelope) -> str | None` — returns the `run_identity_hash` written (or already present), or `None` when the chain is incomplete. Task 4 calls it; Task 7's projection reads the table.

- [ ] **Step 1: Write the failing tests**

```python
from tests.featuregen._helpers import make_identity  # if absent, build IdentityEnvelope directly:
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.run_identity import record_run_identity
from tests.featuregen.runs._chain import seed_run_chain

_ENV = IdentityEnvelope(subject="priya", actor_kind="human", authenticated=True,
                        auth_method="test", role_claims=("feature_engineer",), tenant="t1")


def test_writes_identity_when_the_chain_is_complete(db):
    seed_run_chain(db, run_id="ri-a")
    h = record_run_identity(db, "ri-a", _ENV)
    assert h is not None
    row = db.execute(
        "SELECT owner_subject, owner_tenant, root_generation_run_id, run_identity_hash "
        "FROM feature_run_identity WHERE generation_run_id='ri-a'").fetchone()
    assert row == ("priya", "t1", "ri-a", h)


def test_returns_none_when_the_chain_is_incomplete(db):
    # run + intent only — no generation input, no considered revision, no snapshot
    from psycopg.types.json import Jsonb
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
               "VALUES ('ri-b-i', 'h', 'hypothesis') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
               "VALUES ('ri-b', 'ri-b-i', %s, '{}')", (Jsonb({"subject": "x"}),))
    assert record_run_identity(db, "ri-b", _ENV) is None
    assert db.execute("SELECT count(*) FROM feature_run_identity "
                      "WHERE generation_run_id='ri-b'").fetchone()[0] == 0


def test_idempotent_second_call_keeps_the_first_identity(db):
    seed_run_chain(db, run_id="ri-c")
    h1 = record_run_identity(db, "ri-c", _ENV)
    h2 = record_run_identity(db, "ri-c", _ENV)
    assert h1 == h2
```

(If `tests/featuregen/_helpers.py` exposes an identity factory, prefer it; the inline construction above is the fallback — check `IdentityEnvelope`'s required fields at `contracts/envelopes.py:17`.)

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/featuregen/runs/test_run_identity.py -x -q` — Expected: FAIL (`No module named 'featuregen.runs'`).

- [ ] **Step 3: Implement** (`src/featuregen/runs/run_identity.py`)

```python
"""The run's immutable identity row, written when the creation chain completes (spec §6.1).

The hash payload is EXPLICIT and never self-referential: exactly the thirteen fields below, no
timestamps. A run whose creation path lacks any chain link gets NO row — it renders PRE_SPINE, and
no identity is ever fabricated over absent inputs."""
from __future__ import annotations

from featuregen.canonical import jcs_sha256
from featuregen.contracts.envelopes import IdentityEnvelope

WORKFLOW_DEFINITION_V1 = "V1"


def record_run_identity(conn, generation_run_id: str, actor: IdentityEnvelope) -> str | None:
    gi = conn.execute(
        "SELECT intent_id, confirmed_scope_id, generation_input_content_hash "
        "FROM contract_generation_input WHERE generation_run_id = %s",
        (generation_run_id,)).fetchone()
    ccr = conn.execute(
        "SELECT considered_revision_id, considered_content_hash, metadata_snapshot_id, "
        "metadata_snapshot_content_hash FROM contract_considered_revision "
        "WHERE generation_run_id = %s", (generation_run_id,)).fetchone()
    if gi is None or ccr is None or ccr[2] is None or ccr[3] is None:
        return None  # honest absence: the chain is incomplete, so there is no identity to record
    intent_id, scope_id, input_hash = gi
    considered_id, considered_hash, snapshot_id, snapshot_hash = ccr
    payload = {
        "workflow_definition_version": WORKFLOW_DEFINITION_V1,
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "confirmed_scope_id": scope_id,
        "generation_input_content_hash": input_hash,
        "considered_revision_id": considered_id,
        "considered_content_hash": considered_hash,
        "metadata_snapshot_id": snapshot_id,
        "metadata_snapshot_content_hash": snapshot_hash,
        "owner_subject": actor.subject,
        "owner_tenant": actor.tenant,
        "root_generation_run_id": generation_run_id,   # foundation: every run is a root
        "parent_generation_run_id": None,
    }
    run_identity_hash = jcs_sha256(payload)
    conn.execute(
        "INSERT INTO feature_run_identity (generation_run_id, workflow_definition_version, "
        "intent_id, confirmed_scope_id, generation_input_content_hash, considered_revision_id, "
        "considered_content_hash, metadata_snapshot_id, metadata_snapshot_content_hash, "
        "owner_subject, owner_tenant, root_generation_run_id, parent_generation_run_id, "
        "run_identity_hash, created_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, %s, %s) "
        "ON CONFLICT (generation_run_id) DO NOTHING",
        (generation_run_id, WORKFLOW_DEFINITION_V1, intent_id, scope_id, input_hash,
         considered_id, considered_hash, snapshot_id, snapshot_hash,
         actor.subject, actor.tenant, generation_run_id, run_identity_hash, actor.subject))
    row = conn.execute("SELECT run_identity_hash FROM feature_run_identity "
                       "WHERE generation_run_id = %s", (generation_run_id,)).fetchone()
    return row[0]
```

- [ ] **Step 4: Run tests** — Expected: 3 passed.
- [ ] **Step 5: Commit** — `git add src/featuregen/runs/__init__.py src/featuregen/runs/run_identity.py tests/featuregen/runs/test_run_identity.py && git commit -m "feat(runs): record_run_identity — the chain-complete identity writer"`

---

### Task 4: Wire the writer into the run-creation transaction

**Files:**
- Modify: `src/featuregen/api/routes/contract.py` (the generate endpoint, directly after `build_considered_set` returns — around line 880)
- Test: `tests/featuregen/runs/test_identity_written_at_creation.py`

**Interfaces:**
- Consumes: `record_run_identity` (Task 3).
- Produces: every run minted through the scoped generate flow carries an identity row, in the same transaction as its considered revision and snapshot.

- [ ] **Step 1: Write the failing test** — an API-level test through the existing generate route is heavyweight (LLM client fakes); test the SEAM contract instead: after the route's step order (input → considered+snapshot → identity), identity exists. Mirror an existing generate-route test if one runs cheaply; otherwise test the call-order contract directly:

```python
"""The creation transaction writes identity AFTER the chain (spec §6.1: 'written in the same
transaction that completes run creation')."""
from featuregen.runs.run_identity import record_run_identity
from tests.featuregen.runs._chain import seed_run_chain
from featuregen.contracts.envelopes import IdentityEnvelope

_ENV = IdentityEnvelope(subject="s", actor_kind="human", authenticated=True,
                        auth_method="test", role_claims=())


def test_creation_order_input_considered_then_identity(db):
    c = seed_run_chain(db, run_id="wire-a")           # simulates steps the route performs
    assert record_run_identity(db, "wire-a", _ENV) is not None


def test_contract_route_calls_the_writer():
    import inspect
    from featuregen.api.routes import contract
    src = inspect.getsource(contract)
    assert "record_run_identity(" in src, (
        "the generate endpoint must write feature_run_identity in its creation transaction")
```

- [ ] **Step 2: Run to verify failure** — the second test FAILS (no call in `contract.py`).

- [ ] **Step 3: Modify `contract.py`** — add the import near the other overlay imports and the call immediately after the `cs = build_considered_set(...)` try/except block succeeds (same transaction; before the disposition lens at step 7):

```python
from featuregen.runs.run_identity import record_run_identity
...
    # Run-spine foundation (spec §6.1): the identity row joins the SAME transaction that created
    # the input lineage, considered revision and snapshot. Chain-incomplete paths (no sealed
    # recognition, legacy/unscoped) return None and stay PRE_SPINE — never fabricated.
    record_run_identity(conn, generation_run_id, identity)
```

- [ ] **Step 4: Run** `uv run pytest tests/featuregen/runs/ tests/featuregen/api/test_considered_set_contract_v2.py -q` — Expected: all pass (the existing contract-route suites prove the endpoint still works; if a route test now fails on the identity insert, the chain data in that test is incomplete — `record_run_identity` returns `None` harmlessly in that case, so investigate before changing anything).
- [ ] **Step 5: Commit** — `git add src/featuregen/api/routes/contract.py tests/featuregen/runs/test_identity_written_at_creation.py && git commit -m "feat(runs): write run identity in the creation transaction"`

---

### Task 5: The read policy — owner visibility, pre-spine included

**Files:**
- Create: `src/featuregen/runs/read_policy.py`
- Test: `tests/featuregen/runs/test_read_policy.py`

**Interfaces:**
- Produces:
  - `is_platform_admin(identity: IdentityEnvelope) -> bool` — `"platform_admin" in identity.role_claims`. **Spelling trap:** the functional role bundle in `permissions.py` is `platform_admin` (underscore); the governance-confirmer gate uses the RAW claim `platform-admin` (hyphen). This policy keys on the functional bundle name.
  - `visibility_where(identity) -> tuple[str, list]` — SQL fragment + params for the run list/detail queries, applied INSIDE the query before pagination and counts (spec §11). Empty fragment (`"TRUE"`, `[]`) for platform_admin.
- Owner derivation (spec §11 [R3.1]): a V1 run's owner is `feature_run_identity.owner_subject` (immutable); a pre-spine run's owner is `feature_generation_run.actor->>'subject'`; a pre-spine run with no subject is admin-only.

- [ ] **Step 1: Write the failing tests**

```python
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.read_policy import is_platform_admin, visibility_where


def _env(subject, *roles):
    return IdentityEnvelope(subject=subject, actor_kind="human", authenticated=True,
                            auth_method="test", role_claims=tuple(roles))


def test_admin_sees_everything():
    assert is_platform_admin(_env("a", "platform_admin"))
    frag, params = visibility_where(_env("a", "platform_admin"))
    assert frag == "TRUE" and params == []


def test_owner_predicate_covers_identity_and_pre_spine_actor():
    frag, params = visibility_where(_env("priya", "feature_engineer"))
    assert "fri.owner_subject" in frag and "fgr.actor" in frag
    assert params == ["priya"]


def test_hyphenated_confirmer_claim_is_not_the_functional_role():
    assert not is_platform_admin(_env("a", "platform-admin"))
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
"""Object-level run visibility (spec §11). Applied INSIDE queries, before pagination and counts —
a count over rows the caller may not see leaks the shape of other people's work.

Aliases are fixed by contract: every consumer query aliases feature_run_identity AS fri and
feature_generation_run AS fgr."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope


def is_platform_admin(identity: IdentityEnvelope) -> bool:
    return "platform_admin" in identity.role_claims


def visibility_where(identity: IdentityEnvelope) -> tuple[str, list]:
    if is_platform_admin(identity):
        return "TRUE", []
    # ONE comparison: a V1 run's owner is the immutable identity row; a pre-spine run falls back
    # to the mutable actor subject (spec §11 [R3.1]) — and a pre-spine run with no subject
    # compares NULL = <caller>, which is never true, so it is admin-only by construction.
    return ("COALESCE(fri.owner_subject, fgr.actor->>'subject') = %s", [identity.subject])
```

- [ ] **Step 4: Run tests** — Expected: 3 passed.
- [ ] **Step 5: Commit** — `git add src/featuregen/runs/read_policy.py tests/featuregen/runs/test_read_policy.py && git commit -m "feat(runs): object-level run visibility, pre-spine owners included"`

---

### Task 6: The run-list projection — intent-grouped, keyset-paginated

**Files:**
- Create: `src/featuregen/runs/projection.py`
- Test: `tests/featuregen/runs/test_projection_list.py`

**Interfaces:**
- Consumes: `visibility_where` (Task 5); tables from Task 1.
- Produces: `list_runs(conn, identity, *, limit=25, cursor=None) -> dict` with shape
  `{"groups": [{"intent_id": str|None, "hypothesis": str|None, "runs": [{"generation_run_id", "display_name", "pre_spine": bool, "owner_subject": str|None, "created_at": iso-str}]}], "next_cursor": str|None}`.
- **Pagination design decision (spec §12):** flat keyset over immutable keys `(created_at DESC, generation_run_id DESC)`, grouped by intent WITHIN the page. Immutable keys make the cursor stable under concurrent inserts; a group may split across pages, which the UI tolerates. Cursor format: `"<created_at.isoformat()>|<run_id>"`.

- [ ] **Step 1: Write the failing tests**

```python
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import list_runs
from featuregen.runs.run_identity import record_run_identity
from tests.featuregen.runs._chain import seed_run_chain

_ADMIN = IdentityEnvelope(subject="a", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("platform_admin",))
_OWNER = IdentityEnvelope(subject="u1", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("feature_engineer",))


def _mk(db, run_id, with_identity=True, subject="u1"):
    seed_run_chain(db, run_id=run_id, subject=subject)
    if with_identity:
        env = IdentityEnvelope(subject=subject, actor_kind="human", authenticated=True,
                               auth_method="test", role_claims=())
        record_run_identity(db, run_id, env)


def test_groups_by_intent_and_marks_pre_spine(db):
    _mk(db, "pl-a", with_identity=True)
    _mk(db, "pl-b", with_identity=False)      # chain seeded but no identity row -> pre-spine
    out = list_runs(db, _ADMIN, limit=50)
    runs = {r["generation_run_id"]: r for g in out["groups"] for r in g["runs"]}
    assert runs["pl-a"]["pre_spine"] is False
    assert runs["pl-b"]["pre_spine"] is True


def test_owner_sees_only_their_runs(db):
    _mk(db, "pl-c", subject="u1")
    _mk(db, "pl-d", subject="someone-else")
    out = list_runs(db, _OWNER, limit=50)
    ids = [r["generation_run_id"] for g in out["groups"] for r in g["runs"]]
    assert "pl-c" in ids and "pl-d" not in ids


def test_keyset_pagination_is_stable(db):
    for i in range(5):
        _mk(db, f"pl-p{i}")
    page1 = list_runs(db, _ADMIN, limit=2)
    assert page1["next_cursor"] is not None
    page2 = list_runs(db, _ADMIN, limit=2, cursor=page1["next_cursor"])
    ids1 = [r["generation_run_id"] for g in page1["groups"] for r in g["runs"]]
    ids2 = [r["generation_run_id"] for g in page2["groups"] for r in g["runs"]]
    assert not set(ids1) & set(ids2)
```

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (module missing).

- [ ] **Step 3: Implement `list_runs`**

```python
"""Run projections (spec §12): DERIVED from existing stores — the spine records no lifecycle."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.read_policy import visibility_where


def list_runs(conn, identity: IdentityEnvelope, *, limit: int = 25, cursor: str | None = None) -> dict:
    frag, params = visibility_where(identity)
    cursor_sql, cursor_params = "", []
    if cursor:
        created_at, _, run_id = cursor.partition("|")
        cursor_sql = "AND (fgr.created_at, fgr.generation_run_id) < (%s::timestamptz, %s)"
        cursor_params = [created_at, run_id]
    rows = conn.execute(
        f"""SELECT fgr.generation_run_id, fgr.intent_id, ci.hypothesis, fgr.created_at,
                   fri.generation_run_id IS NOT NULL AS has_identity,
                   COALESCE(fri.owner_subject, fgr.actor->>'subject') AS owner_subject,
                   frp.display_name
            FROM feature_generation_run fgr
            LEFT JOIN feature_run_identity fri USING (generation_run_id)
            LEFT JOIN feature_run_profile  frp USING (generation_run_id)
            LEFT JOIN contract_intent      ci  ON ci.intent_id = fgr.intent_id
            WHERE {frag} {cursor_sql}
            ORDER BY fgr.created_at DESC, fgr.generation_run_id DESC
            LIMIT %s""",
        (*params, *cursor_params, limit + 1)).fetchall()
    page, extra = rows[:limit], rows[limit:]
    groups: list[dict] = []
    for run_id, intent_id, hypothesis, created_at, has_identity, owner, display in page:
        if not groups or groups[-1]["intent_id"] != intent_id:
            groups.append({"intent_id": intent_id,
                           "hypothesis": hypothesis if intent_id else None, "runs": []})
        groups[-1]["runs"].append({
            "generation_run_id": run_id, "display_name": display,
            "pre_spine": not has_identity, "owner_subject": owner,
            "created_at": created_at.isoformat()})
    next_cursor = None
    if extra:
        last = page[-1]
        next_cursor = f"{last[3].isoformat()}|{last[0]}"
    return {"groups": groups, "next_cursor": next_cursor}
```

- [ ] **Step 4: Run tests** — Expected: 3 passed.
- [ ] **Step 5: Commit** — `git add src/featuregen/runs/projection.py tests/featuregen/runs/test_projection_list.py && git commit -m "feat(runs): intent-grouped run list projection with stable keyset pagination"`

---

### Task 7: The run-detail projection — milestones, drafts, and the honest rail

**Files:**
- Modify: `src/featuregen/runs/projection.py` (add detail + rail)
- Test: `tests/featuregen/runs/test_projection_detail.py`

**Interfaces:**
- Produces: `run_detail(conn, identity, run_id) -> dict | None` (None = not visible or absent; the route maps both to 404 so absence and denial are indistinguishable):

```
{"generation_run_id", "pre_spine", "owner_subject", "display_name", "description",
 "intent": {"intent_id", "hypothesis"} | None,
 "identity": {"run_identity_hash", "considered_revision_id", "metadata_snapshot_id"} | None,
 "milestones": {"choose_candidates": [{"option_id","considered_revision_id","chosen_at"}],
                 "bind_selections": []},
 "authoring": [{"formula_draft_id","option_id","state","rail_state",
                 "eligibility": "current"|"withdrawn", "retirement_reason": str|None}],
 "rail": [{"stage","state","reason_code": str|None}]}
```
- Produces: `RAIL_FROM_DRAFT_STATE: dict[str, str]` — the TOTAL mapping (spec §7 [R3.1]).

- [ ] **Step 1: Write the failing tests**

```python
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import RAIL_FROM_DRAFT_STATE, run_detail
from tests.featuregen.runs._chain import seed_run_chain

_ADMIN = IdentityEnvelope(subject="a", actor_kind="human", authenticated=True,
                          auth_method="test", role_claims=("platform_admin",))

_DRAFT_STATES = {"REQUESTED", "AUTHORING", "CRITIC_REVIEW", "VALIDATING",
                 "ADMISSION", "READY", "BLOCKED", "FAILED", "CANCELLED"}  # 1090's CHECK, verbatim


def test_rail_mapping_is_total_over_1090s_check():
    assert set(RAIL_FROM_DRAFT_STATE) == _DRAFT_STATES
    assert RAIL_FROM_DRAFT_STATE["READY"] == "SUCCEEDED"
    assert RAIL_FROM_DRAFT_STATE["BLOCKED"] == "BLOCKED"      # product result, not outage (1090)


def test_detail_shows_sockets_and_two_axes(db):
    from psycopg.types.json import Jsonb
    c = seed_run_chain(db, run_id="rd-a")
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, requested_by, requested_at) "
        "VALUES ('rd-a-d1', %s, 'o1', 'p', 'c', 'a', '', 'fih-rd-a', 'READY', 'u1', "
        "'2026-08-23T00:00:00Z')", (c["considered_revision_id"],))
    db.execute("INSERT INTO formula_draft_retirement (formula_draft_id, reason, retired_by, "
               "retired_at) VALUES ('rd-a-d1', 'CANDIDATE_SUPERSEDED', 'u1', now())")
    out = run_detail(db, _ADMIN, "rd-a")
    d = out["authoring"][0]
    assert d["rail_state"] == "SUCCEEDED" and d["eligibility"] == "withdrawn"   # two axes, §6.7
    by_stage = {s["stage"]: s for s in out["rail"]}
    for stage in ("EXECUTE_SANDBOX", "PUBLISH_SANDBOX", "MATERIALIZE_PRODUCTION",
                  "PUBLISH_PRODUCTION", "TRAIN_MODEL", "GENERATE_PREVIEW"):
        assert by_stage[stage]["state"] == "UNAVAILABLE"
    assert by_stage["GENERATE_PREVIEW"]["reason_code"] == "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"


def test_invisible_run_returns_none(db):
    seed_run_chain(db, run_id="rd-b", subject="someone-else")
    other = IdentityEnvelope(subject="u9", actor_kind="human", authenticated=True,
                             auth_method="test", role_claims=("feature_engineer",))
    assert run_detail(db, other, "rd-b") is None
```

(If `formula_draft_retirement`'s columns differ, read `1096_formula_draft_retirement.sql` and match — reason must be one of its CHECK values; `CANDIDATE_SUPERSEDED` appears in its comment, verify the exact spelling in the CHECK.)

- [ ] **Step 2: Run to verify failure** — Expected: FAIL (`RAIL_FROM_DRAFT_STATE` missing).

- [ ] **Step 3: Implement** — append to `projection.py`:

```python
#: TOTAL over 1090's CHECK — the exhaustiveness test pins it, so a tenth draft state cannot
#: appear without a mapping decision (the ACTIVATION_BLOCKER_DISPOSITIONS pattern).
RAIL_FROM_DRAFT_STATE = {
    "REQUESTED": "IN_PROGRESS", "AUTHORING": "IN_PROGRESS", "CRITIC_REVIEW": "IN_PROGRESS",
    "VALIDATING": "IN_PROGRESS", "ADMISSION": "IN_PROGRESS",
    "READY": "SUCCEEDED", "BLOCKED": "BLOCKED", "FAILED": "FAILED", "CANCELLED": "CANCELLED",
}

#: Five sockets (spec §7) + the foundation's sixth: GENERATE_PREVIEW is unavailable while the
#: 1101 pin is absent — its only entrance 409s (spec §7 [R3.1]), and a "ready" rail above a
#: refused endpoint would be the false rail the spec forbids.
_SOCKETS = (
    ("EXECUTE_SANDBOX", "WORKER_NOT_IMPLEMENTED"),
    ("PUBLISH_SANDBOX", "WORKER_NOT_IMPLEMENTED"),
    ("MATERIALIZE_PRODUCTION", "STATE_MACHINE_NOT_BUILT"),
    ("PUBLISH_PRODUCTION", "STATE_MACHINE_NOT_BUILT"),
    ("TRAIN_MODEL", "SUBSYSTEM_NOT_BUILT"),
)


def _pin_exists(conn) -> bool:
    return conn.execute("SELECT to_regclass('selection_formula_binding')").fetchone()[0] is not None


def run_detail(conn, identity, run_id: str) -> dict | None:
    frag, params = visibility_where(identity)
    row = conn.execute(
        f"""SELECT fgr.generation_run_id, fgr.intent_id, ci.hypothesis,
                   fri.generation_run_id IS NOT NULL, fri.run_identity_hash,
                   fri.considered_revision_id, fri.metadata_snapshot_id,
                   COALESCE(fri.owner_subject, fgr.actor->>'subject'),
                   frp.display_name, frp.description
            FROM feature_generation_run fgr
            LEFT JOIN feature_run_identity fri USING (generation_run_id)
            LEFT JOIN feature_run_profile  frp USING (generation_run_id)
            LEFT JOIN contract_intent      ci  ON ci.intent_id = fgr.intent_id
            WHERE fgr.generation_run_id = %s AND {frag}""",
        (run_id, *params)).fetchone()
    if row is None:
        return None
    (_, intent_id, hypothesis, has_identity, idh, ccr_id, snap_id,
     owner, display, description) = row
    choices = conn.execute(
        "SELECT option_id, considered_revision_id, chosen_at "
        "FROM contract_gate1_choice_revision WHERE generation_run_id = %s "
        "ORDER BY chosen_at", (run_id,)).fetchall()
    drafts = conn.execute(
        """SELECT d.formula_draft_id, d.option_id, d.state, r.reason
           FROM formula_draft d
           JOIN contract_considered_revision ccr
             ON ccr.considered_revision_id = d.considered_revision_id
           LEFT JOIN formula_draft_retirement r USING (formula_draft_id)
           WHERE ccr.generation_run_id = %s ORDER BY d.formula_draft_id""",
        (run_id,)).fetchall()
    authoring = [{
        "formula_draft_id": fid, "option_id": opt, "state": state,
        "rail_state": RAIL_FROM_DRAFT_STATE[state],
        "eligibility": "withdrawn" if reason else "current",     # two axes: outcome vs usability
        "retirement_reason": reason,
    } for fid, opt, state, reason in drafts]
    rail = [
        {"stage": "CHOOSE_CANDIDATES",
         "state": "SUCCEEDED" if choices else "NOT_STARTED", "reason_code": None},
        {"stage": "AUTHOR_FORMULA",
         "state": (sorted((d["rail_state"] for d in authoring),
                          key=["BLOCKED", "FAILED", "IN_PROGRESS", "CANCELLED",
                               "SUCCEEDED"].index)[0] if authoring else "NOT_STARTED"),
         "reason_code": None},
        {"stage": "BIND_SELECTIONS", "state": "NOT_STARTED", "reason_code": None},
        {"stage": "GENERATE_PREVIEW",
         "state": "UNAVAILABLE" if not _pin_exists(conn) else "NOT_STARTED",
         "reason_code": None if _pin_exists(conn) else "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"},
        *({"stage": s, "state": "UNAVAILABLE", "reason_code": code} for s, code in _SOCKETS),
    ]
    return {
        "generation_run_id": run_id, "pre_spine": not has_identity, "owner_subject": owner,
        "display_name": display, "description": description,
        "intent": {"intent_id": intent_id, "hypothesis": hypothesis} if intent_id else None,
        "identity": ({"run_identity_hash": idh, "considered_revision_id": ccr_id,
                      "metadata_snapshot_id": snap_id} if has_identity else None),
        "milestones": {
            "choose_candidates": [{"option_id": o, "considered_revision_id": c,
                                   "chosen_at": t.isoformat()} for o, c, t in choices],
            "bind_selections": []},
        "authoring": authoring,
        "rail": rail,
    }
```

The `AUTHOR_FORMULA` worst-of fold sorts by explicit severity order (`BLOCKED` outranks everything, spec §12) — never alphabetical.

- [ ] **Step 4: Run tests** — Expected: 3 passed.
- [ ] **Step 5: Commit** — `git add src/featuregen/runs/projection.py tests/featuregen/runs/test_projection_detail.py && git commit -m "feat(runs): run detail projection — milestones, two-axis authoring, honest rail"`

---

### Task 8: The API routes

**Files:**
- Create: `src/featuregen/api/routes/feature_runs.py`
- Modify: `src/featuregen/api/app.py` (one `app.include_router(feature_runs.router)` beside the other registrations at ~line 213, plus the import)
- Test: `tests/featuregen/api/test_feature_runs.py`

**Interfaces:**
- Consumes: `list_runs`, `run_detail` (Tasks 6–7); `get_conn`, `get_identity`, `require_feature_read` from `featuregen.api.deps`.
- Produces: `GET /feature-runs?limit=&cursor=` → 200 `{groups, next_cursor}`; `GET /feature-runs/{run_id}` → 200 detail | 404 (absent AND not-visible — indistinguishable by design).

- [ ] **Step 1: Write the failing tests**

```python
"""Run-list/detail routes. NEVER conn.commit() here — the shared test conn rolls back."""
from tests.featuregen.runs._chain import seed_run_chain


def _hdr(user, roles):
    return {"X-User": user, "X-Roles": roles}


def test_list_scopes_to_the_caller(client, conn):
    seed_run_chain(conn, run_id="api-a", subject="priya")
    seed_run_chain(conn, run_id="api-b", subject="other")
    body = client.get("/feature-runs", headers=_hdr("priya", "feature_engineer")).json()
    ids = [r["generation_run_id"] for g in body["groups"] for r in g["runs"]]
    assert "api-a" in ids and "api-b" not in ids


def test_admin_lists_all(client, conn):
    seed_run_chain(conn, run_id="api-c", subject="other")
    body = client.get("/feature-runs", headers=_hdr("a", "platform_admin")).json()
    ids = [r["generation_run_id"] for g in body["groups"] for r in g["runs"]]
    assert "api-c" in ids


def test_detail_404_hides_denial_from_absence(client, conn):
    seed_run_chain(conn, run_id="api-d", subject="other")
    r1 = client.get("/feature-runs/api-d", headers=_hdr("priya", "feature_engineer"))
    r2 = client.get("/feature-runs/does-not-exist", headers=_hdr("priya", "feature_engineer"))
    assert r1.status_code == 404 and r2.status_code == 404
    assert r1.json() == r2.json()          # indistinguishable: no shape leak


def test_detail_shape(client, conn):
    seed_run_chain(conn, run_id="api-e", subject="priya")
    body = client.get("/feature-runs/api-e", headers=_hdr("priya", "feature_engineer")).json()
    assert body["pre_spine"] is True       # chain seeded but no identity row
    assert {s["stage"] for s in body["rail"]} >= {"GENERATE_PREVIEW", "TRAIN_MODEL"}
```

**X-User note:** the stub builds `subject` from the X-User header — if `seed_run_chain`'s actor subject and the header subject must match for visibility, they do here ("priya"). If the stub prefixes subjects (e.g. `user:priya`), read `_auth_stub` handling in `deps.py`, then align the SEED (pass `subject=` accordingly), never the policy.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/featuregen/api/test_feature_runs.py -x -q` — Expected: 404 on every call (router not registered).

- [ ] **Step 3: Implement the router**

```python
"""Read-only run spine routes (spec §5/§11/§12). The spine DERIVES; it never stores lifecycle.

404 covers both absence and denial, deliberately: a distinguishable 403 would confirm the run id
exists — a shape leak the read policy exists to prevent."""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException

from featuregen.api.deps import get_conn, get_identity, require_feature_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.runs.projection import list_runs, run_detail

router = APIRouter(dependencies=[Depends(require_feature_read)])
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/feature-runs")
def feature_runs_list(conn: _Conn, identity: _Identity,
                      limit: int = 25, cursor: str | None = None) -> dict:
    return list_runs(conn, identity, limit=min(max(limit, 1), 100), cursor=cursor)


@router.get("/feature-runs/{run_id}")
def feature_run_detail(run_id: str, conn: _Conn, identity: _Identity) -> dict:
    detail = run_detail(conn, identity, run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return detail
```

Register in `app.py`: add `feature_runs` to the routes import list and `app.include_router(feature_runs.router)` next to `ingestion_runs`.

- [ ] **Step 4: Run tests** — Expected: 4 passed. Then `uv run pytest tests/featuregen/api -q` — no regressions.
- [ ] **Step 5: Commit** — `git add src/featuregen/api/routes/feature_runs.py src/featuregen/api/app.py tests/featuregen/api/test_feature_runs.py && git commit -m "feat(runs): GET /feature-runs list + detail with object-level policy"`

---

### Task 9: The build-set declaration refusal (protects 1101's no-backfill branch)

**Files:**
- Modify: `src/featuregen/api/routes/build_sets.py` (both POST handlers)
- Test: `tests/featuregen/api/test_build_set_pre_pin_refusal.py`

**Interfaces:**
- Produces: `POST /build-sets` and `POST /build-sets/generations` refuse `409 {"code": "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN", ...}` while `to_regclass('selection_formula_binding')` is NULL. **Self-retiring:** when parent migration 1101 applies, the guard passes with zero code change.

- [ ] **Step 1: Write the failing test**

```python
"""One caller before 1101 destroys the binding's zero-row NOT NULL branch (spec §7/§13).
The V2 switch must be ON for these routes to exist at all (they 404 otherwise)."""


def test_declare_refuses_pre_pin(client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")
    r = client.post("/build-sets", json={}, headers={"X-User": "u", "X-Roles": "feature_engineer"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN"


def test_guard_retires_itself_when_the_pin_table_exists(client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_GENERATION_V2_ENABLED", "1")
    conn.execute("CREATE TABLE IF NOT EXISTS selection_formula_binding (binding_id text PRIMARY KEY)")
    r = client.post("/build-sets", json={}, headers={"X-User": "u", "X-Roles": "feature_engineer"})
    assert r.status_code != 409 or "PRE_PIN" not in str(r.json())   # falls through to validation (422)
```

Check the env-var name the switch actually reads: `grep -rn "FEATUREGEN_GENERATION_V2_ENABLED\|def generation_enabled" src/featuregen/ | head`. If the switch is consulted differently in tests (a settings object, not an env var), mirror how `tests/.../test_build_sets*.py` (or the S13 suites) enable it — the existing build-set tests show the working pattern; copy it.

- [ ] **Step 2: Run to verify failure** — first test FAILS (422 or 2xx instead of 409).

- [ ] **Step 3: Implement** — add to `build_sets.py`, first statement inside BOTH `declare_build_set` and the generations POST handler:

```python
def _refuse_pre_pin(conn) -> None:
    """Spec §7 [R3.1]: while the 1101 binding table is absent, every build set written closes the
    zero-row NOT NULL branch — so declaration is withheld, server-side. Self-retiring: the moment
    1101 applies, to_regclass returns the relation and this refusal disappears."""
    if conn.execute("SELECT to_regclass('selection_formula_binding')").fetchone()[0] is None:
        raise HTTPException(status_code=409, detail={
            "code": "BUILD_SET_DECLARATION_WITHHELD_PRE_PIN",
            "message": "build-set declaration is withheld until the selection→formula binding "
                       "(migration 1101) exists — a build set written before the pin would force "
                       "a nullable column and a backfill of exactly the rows the pin constrains",
        })
```

Call `_refuse_pre_pin(conn)` before any validation in both handlers.

- [ ] **Step 4: Run** — both new tests pass; then `uv run pytest tests/featuregen/api/test_build_sets.py tests/featuregen/materialize -q`. Existing build-set route tests will now hit the 409 — fix their FIXTURES by creating the stub pin table (the pattern in the second test above) in a small autouse fixture inside those test files, not by weakening the guard. If a coordination conflict with in-flight 2A edits to `build_sets.py` arises (the file is currently clean, but 2A owns adjacent lines), STOP and coordinate before editing.
- [ ] **Step 5: Commit** — `git add src/featuregen/api/routes/build_sets.py tests/featuregen/api/test_build_set_pre_pin_refusal.py` plus each fixture-fixed test file by exact path; `git commit -m "feat(runs): withhold build-set declaration until the 1101 pin exists"`

---

### Task 10: Frontend API client

**Files:**
- Modify: `frontend/src/api.ts` (append types + two functions)
- Test: `frontend/src/api.test.ts` (append)

**Interfaces:**
- Produces (Task 12–14 consume):

```typescript
export interface FeatureRunSummary {
  generation_run_id: string; display_name: string | null; pre_spine: boolean
  owner_subject: string | null; created_at: string
}
export interface FeatureRunGroup {
  intent_id: string | null; hypothesis: string | null; runs: FeatureRunSummary[]
}
export interface FeatureRunList { groups: FeatureRunGroup[]; next_cursor: string | null }
export interface RunRailStage { stage: string; state: string; reason_code: string | null }
export interface RunAuthoringRow {
  formula_draft_id: string; option_id: string; state: string; rail_state: string
  eligibility: 'current' | 'withdrawn'; retirement_reason: string | null
}
export interface FeatureRunDetail {
  generation_run_id: string; pre_spine: boolean; owner_subject: string | null
  display_name: string | null; description: string | null
  intent: { intent_id: string; hypothesis: string } | null
  identity: { run_identity_hash: string; considered_revision_id: string
              metadata_snapshot_id: string } | null
  milestones: { choose_candidates: { option_id: string; considered_revision_id: string
                                     chosen_at: string }[]; bind_selections: unknown[] }
  authoring: RunAuthoringRow[]; rail: RunRailStage[]
}
export function listFeatureRuns(cursor?: string): Promise<FeatureRunList>
export function getFeatureRunDetail(runId: string): Promise<FeatureRunDetail>
```

- [ ] **Step 1: Write the failing test** (append to `api.test.ts`, mirroring its existing fetch-mock pattern — read the top of the file first and reuse its helpers):

```typescript
it('listFeatureRuns hits /feature-runs and forwards the cursor', async () => {
  const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ groups: [], next_cursor: null }), { status: 200 }))
  await listFeatureRuns('2026-08-23T00:00:00+00:00|grun_x')
  expect(String(spy.mock.calls[0][0])).toBe(
    '/feature-runs?cursor=2026-08-23T00%3A00%3A00%2B00%3A00%7Cgrun_x')
})

it('getFeatureRunDetail encodes the opaque id', async () => {
  const spy = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ generation_run_id: 'grun_x' }), { status: 200 }))
  await getFeatureRunDetail('grun_x')
  expect(String(spy.mock.calls[0][0])).toBe('/feature-runs/grun_x')
})
```

- [ ] **Step 2: Run to verify failure** — `cd frontend && npx vitest run src/api.test.ts` — Expected: FAIL (functions missing).

- [ ] **Step 3: Implement** — append to `api.ts` (the interfaces above verbatim, then):

```typescript
export function listFeatureRuns(cursor?: string): Promise<FeatureRunList> {
  const q = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return request(`/feature-runs${q}`)
}

export function getFeatureRunDetail(runId: string): Promise<FeatureRunDetail> {
  return request(`/feature-runs/${encodeURIComponent(runId)}`)
}
```

- [ ] **Step 4: Run tests** — Expected: PASS. Also confirm the Vite dev proxy forwards `/feature-runs` (check `frontend/vite.config.ts` proxy prefixes; add `'/feature-runs'` beside the existing API prefixes if it enumerates them — the Phase-G ingress memory says prefixes are enumerated).
- [ ] **Step 5: Commit** — `git add frontend/src/api.ts frontend/src/api.test.ts frontend/vite.config.ts && git commit -m "feat(runs): frontend client for the runs list and detail"`

---

### Task 11: The `runs` route in nav + App shell

**Files:**
- Modify: `frontend/src/nav.ts` (Route union, ROUTES, parseHash path-param support)
- Modify: `frontend/src/App.tsx` (pages entry, ICONS entry, render branch)
- Test: `frontend/src/nav.test.ts` (append)

**Interfaces:**
- Produces: route `'runs'`; `#/runs` → list; `#/runs/<id>` → params `run_id=<id>`. Screens from Tasks 12–13 are rendered by App: `{route === 'runs' && (params.get('run_id') ? <RunDetailScreen runId={...}/> : <RunsScreen navigate={navigate}/>)}`.

- [ ] **Step 1: Write the failing tests**

```typescript
it('parses #/runs as the runs list', () => {
  expect(parseHash('#/runs').route).toBe('runs')
})

it('parses #/runs/grun_x as detail with run_id param', () => {
  const { route, params } = parseHash('#/runs/grun_x')
  expect(route).toBe('runs')
  expect(params.get('run_id')).toBe('grun_x')
})
```

- [ ] **Step 2: Run to verify failure** — `npx vitest run src/nav.test.ts` — Expected: FAIL (falls back to 'overview').

- [ ] **Step 3: Implement in `nav.ts`** — add `'runs'` to the `Route` union and `ROUTES`; in `parseHash`, before the `known` check:

```typescript
  // The runs detail is the one path-param route: '#/runs/<opaque id>'. The id is opaque
  // (grun_/fgr_ alike) and percent-decoded; everything else stays query-string params.
  if (path.startsWith('runs/')) {
    const p = new URLSearchParams(query)
    p.set('run_id', decodeURIComponent(path.slice('runs/'.length)))
    return { route: 'runs', params: p }
  }
```

- [ ] **Step 4: Wire App.tsx** — add to the `pages` array (after the `registry` entry):

```typescript
  {
    route: 'runs',
    label: 'Runs',
    eyebrow: 'CATALOG · RUNS',
    title: 'Feature runs',
    description:
      'Every feature-generation workflow, grouped by hypothesis — open a run to see exactly '
      + 'what happened, stage by stage, and what its evidence pins.',
  },
```

Add an `ICONS['runs']` entry (copy a neighbour's SVG pattern — a simple three-bar/list glyph), and the render branch beside the other routes:

```tsx
        {route === 'runs' && (params.get('run_id')
          ? <RunDetailScreen runId={params.get('run_id') ?? ''} />
          : <RunsScreen navigate={navigate} />)}
```

(Screens arrive in Tasks 12–13; to keep this task green standalone, create both files now as minimal stubs rendering `<p>…</p>` — Tasks 12–13 replace their bodies, and this task's tests only exercise nav.)

- [ ] **Step 5: Run** — `npx vitest run src/nav.test.ts src/App.test.tsx` — Expected: PASS.
- [ ] **Step 6: Commit** — `git add frontend/src/nav.ts frontend/src/nav.test.ts frontend/src/App.tsx frontend/src/screens/RunsScreen.tsx frontend/src/screens/RunDetailScreen.tsx && git commit -m "feat(runs): the runs route — #/runs list and #/runs/{id} detail"`

---

### Task 12: RunsScreen — the grouped list

**Files:**
- Modify: `frontend/src/screens/RunsScreen.tsx` (replace the Task-11 stub)
- Test: `frontend/src/screens/RunsScreen.test.tsx`

**Interfaces:**
- Consumes: `listFeatureRuns` (Task 10); `navigate` prop `(route: Route, params?: Record<string, string>) => void`.
- Produces: intent-grouped list; "No hypothesis recorded" bucket; truncated copyable opaque ids; `—` for unset names; PRE_SPINE badge; a Load-more button driven by `next_cursor` (reads are safe to re-fire; nothing here mutates).

- [ ] **Step 1: Write the failing test** (mock the api module — mirror an existing screen test's setup):

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
vi.mock('../api', () => ({
  listFeatureRuns: vi.fn().mockResolvedValue({
    groups: [
      { intent_id: 'i1', hypothesis: 'Retail churn', runs: [
        { generation_run_id: 'grun_01M02SAZQQQQ', display_name: 'August build',
          pre_spine: false, owner_subject: 'priya', created_at: '2026-08-23T00:00:00+00:00' }] },
      { intent_id: null, hypothesis: null, runs: [
        { generation_run_id: 'fgr_legacy01', display_name: null, pre_spine: true,
          owner_subject: null, created_at: '2026-08-01T00:00:00+00:00' }] },
    ],
    next_cursor: null,
  }),
}))
import { RunsScreen } from './RunsScreen'

it('groups by hypothesis with an honest ungrouped bucket', async () => {
  render(<RunsScreen navigate={() => {}} />)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.getByText('No hypothesis recorded')).toBeInTheDocument()
  expect(screen.getByText('August build')).toBeInTheDocument()
  expect(screen.getAllByText('—').length).toBeGreaterThan(0)   // unset name renders as absence
  expect(screen.getByText(/Pre-spine/i)).toBeInTheDocument()
})
```

- [ ] **Step 2: Run to verify failure** — stub renders none of it.

- [ ] **Step 3: Implement** — a screen in the codebase's plain-React style (no new deps): `useEffect` fetch on mount, group sections with the hypothesis (or the literal heading `No hypothesis recorded`), rows showing truncated id (`id.slice(0, 13) + '…'`, full id in `title=` and a copy control via `navigator.clipboard.writeText(id)`), `display_name ?? '—'`, `Pre-spine` badge when `pre_spine`, owner, created date; row click → `navigate('runs', { run_id: id })`; `next_cursor` renders a *Load more* button appending the next page. Error state: render the `ApiError` message the server sent — no invented copy. Loading: a plain "Loading runs…" line.

- [ ] **Step 4: Run** — `npx vitest run src/screens/RunsScreen.test.tsx` — Expected: PASS.
- [ ] **Step 5: Commit** — `git add frontend/src/screens/RunsScreen.tsx frontend/src/screens/RunsScreen.test.tsx && git commit -m "feat(runs): RunsScreen — hypothesis-grouped run list"`

---

### Task 13: RunDetailScreen — the honest rail

**Files:**
- Modify: `frontend/src/screens/RunDetailScreen.tsx` (replace the Task-11 stub)
- Test: `frontend/src/screens/RunDetailScreen.test.tsx`

**Interfaces:**
- Consumes: `getFeatureRunDetail` (Task 10); prop `runId: string`.
- Produces: header (truncated copyable id, name or `—`, owner, hypothesis, PRE_SPINE label), the stage rail rendering every stage's `state` with the server's `reason_code` sentence VERBATIM next to `UNAVAILABLE` stages, milestones (chosen candidates), and the authoring table showing BOTH axes: outcome (`rail_state`) and eligibility (`Withdrawn — <retirement_reason>` when withdrawn). **No trigger buttons anywhere — this increment is read-only, and the absence of buttons is the design, not an omission.**

- [ ] **Step 1: Write the failing test**

```tsx
import { render, screen, waitFor } from '@testing-library/react'
import { vi } from 'vitest'
vi.mock('../api', () => ({
  getFeatureRunDetail: vi.fn().mockResolvedValue({
    generation_run_id: 'grun_01M02SAZQQQQ', pre_spine: false, owner_subject: 'priya',
    display_name: null, description: null,
    intent: { intent_id: 'i1', hypothesis: 'Retail churn' },
    identity: { run_identity_hash: 'h', considered_revision_id: 'c', metadata_snapshot_id: 's' },
    milestones: { choose_candidates: [
      { option_id: 'o1', considered_revision_id: 'c', chosen_at: '2026-08-23T00:00:00+00:00' }],
      bind_selections: [] },
    authoring: [{ formula_draft_id: 'd1', option_id: 'o1', state: 'READY',
                  rail_state: 'SUCCEEDED', eligibility: 'withdrawn',
                  retirement_reason: 'CANDIDATE_SUPERSEDED' }],
    rail: [
      { stage: 'CHOOSE_CANDIDATES', state: 'SUCCEEDED', reason_code: null },
      { stage: 'GENERATE_PREVIEW', state: 'UNAVAILABLE',
        reason_code: 'BUILD_SET_DECLARATION_WITHHELD_PRE_PIN' },
      { stage: 'TRAIN_MODEL', state: 'UNAVAILABLE', reason_code: 'SUBSYSTEM_NOT_BUILT' },
    ],
  }),
}))
import { RunDetailScreen } from './RunDetailScreen'

it('renders the rail honestly and both authoring axes, with no trigger buttons', async () => {
  render(<RunDetailScreen runId="grun_01M02SAZQQQQ" />)
  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.getByText('BUILD_SET_DECLARATION_WITHHELD_PRE_PIN')).toBeInTheDocument()
  expect(screen.getByText(/SUCCEEDED/)).toBeInTheDocument()          // outcome axis
  expect(screen.getByText(/Withdrawn/)).toBeInTheDocument()          // eligibility axis
  expect(screen.queryAllByRole('button').filter(b =>
    /run|re-run|retry|generate|execute|fork/i.test(b.textContent ?? ''))).toHaveLength(0)
})
```

- [ ] **Step 2: Run to verify failure** — stub fails.
- [ ] **Step 3: Implement** — plain-React detail screen per the Interfaces block; render `reason_code` strings verbatim (the server owns policy sentences — this screen holds none, the FeatureExecutionScreen rule).
- [ ] **Step 4: Run** — Expected: PASS.
- [ ] **Step 5: Commit** — `git add frontend/src/screens/RunDetailScreen.tsx frontend/src/screens/RunDetailScreen.test.tsx && git commit -m "feat(runs): RunDetailScreen — read-only rail, two-axis authoring"`

---

### Task 14: Full-suite verification and wrap-up

**Files:** none new.

- [ ] **Step 1: Backend** — `uv run pytest tests -q` — Expected: baseline 13154 + all new tests, 0 failed. Read the SUMMARY line itself (`grep -c "^FAILED"` against coloured output reports a false pass — handoff trap).
- [ ] **Step 2: Frontend** — `cd frontend && npx vitest run` — Expected: baseline 985 + new tests, 0 failed.
- [ ] **Step 3: Verify one test bites** — reintroduce a defect on purpose: comment out the `record_run_identity` call in `contract.py`, confirm `test_contract_route_calls_the_writer` FAILS, restore, confirm green.
- [ ] **Step 4: Final commit if anything is outstanding; verify `git status` shows ONLY the third-party in-flight files untouched** (`1100_action_authorization_revision.sql`, `action_authorization.py`, its test, and any parent-plan edits not yours).
- [ ] **Step 5: STOP — do not deploy.** Migrations 1115/1116 are files only. Applying them to the live cluster (which is at ledger 1099 with images at 1093 — the skew the handoff names) is an owner decision with its own runbook.

---

## Self-review notes (already applied)

- **Spec coverage:** §6.1 → Tasks 1/3/4 · §9 FKs → Task 2 · §11 → Tasks 5/8 · §12 → Tasks 6/7/12/13 · §7 sockets + rail mapping → Task 7 · §13 build-set refusal → Task 9 · §13 route → Tasks 10–13 · §17 amendment → Task 0. Deliberately absent per spec §13: `feature_run_state` rows, invocations, headers, triggers, fork, cancel, archive/profile writes, timeline events.
- **Known execution risks, named for the executor:** exact NOT NULL column sets in seed SQL (helper serves the schemas — fix the helper, never the schema); the auth-stub subject format (align seeds, never policy); the V2-switch enablement pattern in Task 9 (copy from existing build-set tests); `formula_draft_retirement` CHECK values (read 1096 before seeding one).
