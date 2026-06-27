## Phase 02: Document store, DAG lineage & document schema registry

**Goal:** Build the immutable, write-once staged-document DAG (body-by-reference + content hash, acyclic-by-construction lineage, immutable `branch_role`), the `PRIMARY_SELECTED` event + the one-live-primary projection resolved by `global_seq`, the document/artifact schema registry (reader-upcasters + deprecation lifecycle) with the normatively published stage/artifact enum, and the normative Draft schema (`raw_input` by encrypted reference + classification, `open_fields`/UNKNOWN handling, assumption-ledger linkage). Spec §3.4 / §3.5 / §3.7.

---

### Prerequisites consumed from earlier phases

This phase binds to these published Phase 01 interfaces (do not redefine them). If Phase 01 chose different module paths, adapt the imports — names, params, and return types are fixed.

- **Shared contract symbols** — `from sp0.contracts import (...)`:
  - `DbConn` — alias for the active `psycopg` (psycopg 3) `Connection`/transaction handle.
  - `IdentityEnvelope`, `ProvenanceEnvelope` — frozen envelopes (the `ProvenanceEnvelope` defining module is `sp0.contracts.envelopes`; import it from there inside `sp0.contracts.documents` to avoid an `__init__` import cycle).
  - `NewEvent`, `EventEnvelope`, `ConcurrencyError`.
  - `SchemaRegistry` (Protocol), `SchemaValidationError`, `Upcaster`.
  - `Projection` (Protocol), `ProjectionApplyError`, `run_projection(conn, projection, *, batch=500) -> int`.
- **Event store** — `from sp0.events import append_event, load_stream`:
  - `append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope` — validates payload against the event registry **reading `event_type_registry` on the passed `conn`** before insert.
- **Id minting** — `from sp0.ids import new_id`: `new_id(prefix: str) -> str` returns a ULID-style `"{prefix}_{26-char-ULID}"` (used for `evt_…`, `sec_…`, etc.).
- **Migrations** — `from sp0.db.migrate import apply_migrations`: `apply_migrations(conn: DbConn) -> None` applies every `src/sp0/db/migrations/*.sql` in lexical order, idempotently. Phase 02 only drops a new `0002_*.sql` file; the runner picks it up.
- **Phase 01 DDL this phase references:** `global_seq_seq` (sequence), `events`, `event_type_registry`, `registry_snapshots`, `projection_checkpoints`. The `LIKE event_type_registry` in this phase's migration requires `event_type_registry` to already exist (it does, from `0001_*.sql`).
- **Test fixture** — a root `tests/conftest.py` (Phase 01) exposes a `db` pytest fixture yielding a clean `DbConn` with all migrations applied, rolled back between tests. This phase's tests request `db` by name.
- **Project dependency** — `jsonschema` is a project dependency (added by Phase 01 for event-payload validation). This phase uses it for document-body validation.

**Cross-phase coordination (resolved, not assumed):**

- **`registry_snapshots` is created by Phase 01.** The overview lists it as "Phase 01/02", but ownership is assigned to **Phase 01**: the event `SchemaRegistry.snapshot_version()` (a Phase 01 deliverable) already requires this table, so Phase 01's `0001_*.sql` creates it. Phase 02's `0002_documents.sql` therefore does **not** create `registry_snapshots`; it only reads/writes it via `DocumentSchemaRegistry.snapshot_version()` (rows scoped by `registry='docs'`). If Phase 01's plan does not create it, that is a Phase 01 bug to raise back to the overview — Phase 02 must not add a second `CREATE TABLE` (it would conflict). This removes the "neither phase creates it" risk.
- **`projection_checkpoints` row ownership.** `run_projection` (Phase 01) consumes events with `global_seq > checkpoint_seq` for a projection keyed by `projection.name`. Phase 02 does **not** depend on whether Phase 01's `run_projection` self-initializes a missing checkpoint row: `register_primary_selected` (Task 7) idempotently inserts the `stage_primary` checkpoint row (`ON CONFLICT DO NOTHING`), so the runner always has a `checkpoint_seq=0` starting point. This is harmless if Phase 01 also upserts it.

---

### File structure

```
src/sp0/
  contracts/
    __init__.py                  # MODIFY: re-export NewDocument, Stage, STAGES, BRANCH_ROLES, BODY_CLASSIFICATIONS
    documents.py                 # CREATE: NewDocument (verbatim) + normative Stage enum + role/classification vocab
  db/migrations/
    0002_documents.sql           # CREATE: documents, stage_primary, blob_index, document_type_registry + write-once trigger
  documents/
    __init__.py                  # CREATE: package marker
    store.py                     # CREATE: append_document, get_document, compute_content_hash, DAG + structural validation
    registry.py                  # CREATE: DocumentSchemaRegistry (conforms to SchemaRegistry over document_type_registry)
    primary.py                   # CREATE: PRIMARY_SELECTED event + StagePrimaryProjection + current_primary
    draft.py                     # CREATE: normative Draft / Assumption-Ledger schema + validate_draft

tests/sp0/documents/
  conftest.py                    # CREATE: actor/provenance fixtures (build on root `db`)
  test_contracts.py              # Task 1
  test_migration.py              # Task 2
  test_store_append.py           # Task 3
  test_store_dag.py              # Task 4
  test_store_validation.py       # Task 5
  test_registry.py               # Task 6
  test_primary.py                # Task 7
  test_draft.py                  # Task 8
```

One responsibility per file. `store.py` is created in Task 3 and extended (Modify) in Tasks 4–5; `conftest.py` is created in Task 2 and extended in later tasks as noted.

---

## Task 1 — Document contract types + normative stage/artifact enum

**Files:**
- Create: `src/sp0/contracts/documents.py`
- Modify: `src/sp0/contracts/__init__.py`
- Test: `tests/sp0/documents/test_contracts.py`

**Interfaces:**
- Consumes: `from sp0.contracts.envelopes import ProvenanceEnvelope` (Phase 01).
- Produces:
  - `NewDocument` — frozen/slots dataclass (verbatim from the shared contract).
  - `Stage(str, Enum)` — the 14 normative stage/artifact names (§3.7).
  - `STAGES: tuple[str, ...]`, `BRANCH_ROLES: tuple[str, ...]`, `BODY_CLASSIFICATIONS: tuple[str, ...]`.

**TDD cycle 1.1 — the contract types exist and are frozen**

1. Write the failing test — `tests/sp0/documents/test_contracts.py`:

```python
from __future__ import annotations

import dataclasses

import pytest

from sp0.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    NewDocument,
    Stage,
)
from sp0.contracts.envelopes import ProvenanceEnvelope


def _prov() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT",
        schema_version=1,
        producing_component="sp0-test@0.0.0",
    )


def test_stage_enum_is_the_normative_published_set_in_order():
    assert STAGES == (
        "DRAFT_CONTRACT", "ASSUMPTION_LEDGER", "CONFIRMED_CONTRACT", "MAPPED_CONTRACT",
        "FEATURE_PLAN", "CANDIDATE_SQL", "VALIDATION_REPORT", "SANDBOX_RESULT", "DQ_REPORT",
        "EVALUATION_REPORT", "RISK_ASSESSMENT", "EXPLAINABILITY", "MONITORING_SPEC",
        "APPROVAL_RECORD",
    )
    assert Stage.CONFIRMED_CONTRACT.value == "CONFIRMED_CONTRACT"
    assert tuple(s.value for s in Stage) == STAGES


def test_branch_role_and_classification_vocab():
    assert BRANCH_ROLES == ("candidate", "primary", "rejected", "repair")
    assert BODY_CLASSIFICATIONS == ("pii-erasable", "governance-retained")


def test_new_document_is_frozen_with_defaults():
    doc = NewDocument(
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:abc",
        body_classification="pii-erasable",
        provenance=_prov(),
    )
    assert doc.body_ref is None
    assert doc.derived_from == () and doc.supersedes == ()
    assert doc.reject_reason is None
    assert dataclasses.is_dataclass(doc)
    with pytest.raises(dataclasses.FrozenInstanceError):
        doc.branch_role = "primary"  # type: ignore[misc]
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_contracts.py -q`
   - Expected: `ModuleNotFoundError: No module named 'sp0.contracts.documents'`.

3. Write minimal implementation — `src/sp0/contracts/documents.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from sp0.contracts.envelopes import ProvenanceEnvelope


class Stage(str, Enum):
    """Normatively published stage/artifact enum (§3.7)."""
    DRAFT_CONTRACT = "DRAFT_CONTRACT"
    ASSUMPTION_LEDGER = "ASSUMPTION_LEDGER"
    CONFIRMED_CONTRACT = "CONFIRMED_CONTRACT"
    MAPPED_CONTRACT = "MAPPED_CONTRACT"
    FEATURE_PLAN = "FEATURE_PLAN"
    CANDIDATE_SQL = "CANDIDATE_SQL"
    VALIDATION_REPORT = "VALIDATION_REPORT"
    SANDBOX_RESULT = "SANDBOX_RESULT"
    DQ_REPORT = "DQ_REPORT"
    EVALUATION_REPORT = "EVALUATION_REPORT"
    RISK_ASSESSMENT = "RISK_ASSESSMENT"
    EXPLAINABILITY = "EXPLAINABILITY"
    MONITORING_SPEC = "MONITORING_SPEC"
    APPROVAL_RECORD = "APPROVAL_RECORD"


STAGES: tuple[str, ...] = tuple(s.value for s in Stage)
BRANCH_ROLES: tuple[str, ...] = ("candidate", "primary", "rejected", "repair")
BODY_CLASSIFICATIONS: tuple[str, ...] = ("pii-erasable", "governance-retained")


@dataclass(frozen=True, slots=True)
class NewDocument:
    """A frozen document a handler emits (§3.4). derived_from MUST reference committed docs."""
    stage: str                                 # from the §3.7 stage/artifact enum
    schema_version: int
    branch_role: str                           # "candidate" | "primary" | "rejected" | "repair"
    content_hash: str
    body_classification: str                   # "pii-erasable" | "governance-retained"
    provenance: ProvenanceEnvelope
    body_ref: Optional[str] = None             # blob id (§9); None for fully-inline-safe metadata docs
    derived_from: tuple[str, ...] = ()         # committed doc_ids (lower global_seq)
    supersedes: tuple[str, ...] = ()
    reject_reason: Optional[str] = None        # required when branch_role == "rejected"
```

   Then append to `src/sp0/contracts/__init__.py` (downstream phases import these from the package root):

```python
from sp0.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    NewDocument,
    Stage,
)
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_contracts.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: NewDocument contract + normative stage/artifact enum

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 2 — Migration: documents, stage_primary, blob_index, document_type_registry + write-once trigger

**Files:**
- Create: `src/sp0/db/migrations/0002_documents.sql`
- Create: `tests/sp0/documents/conftest.py`
- Test: `tests/sp0/documents/test_migration.py`

**Interfaces:**
- Consumes: `apply_migrations` (Phase 01, via the `db` fixture); `event_type_registry`, `registry_snapshots` (Phase 01 DDL).
- Produces (physical DDL, verbatim from the shared contract): tables `documents` (+ row trigger `documents_no_mutation` calling `documents_write_once()`), `stage_primary` (+ unique index `one_live_primary_per_run_stage`), `blob_index`, `document_type_registry`.

> **Cross-phase note — `blob_index`:** this phase creates the `blob_index` table because documents reference blobs (`body_ref` + `content_hash` + `classification`) and the overview assigns the `blob_index` schema to Phase 02. Phase 05 builds the mark-and-sweep GC mechanism over it and is authoritative for GC behavior (`referenced`/`status` transitions, quarantine, crypto-shred). Phase 02 does **not** mark `referenced` or sweep — `body_ref` is stored as an opaque reference string.

**TDD cycle 2.1 — tables, indexes, and the write-once trigger**

1. Write the failing test — first add the shared fixtures `tests/sp0/documents/conftest.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import IdentityEnvelope, ProvenanceEnvelope


@pytest.fixture
def actor() -> IdentityEnvelope:
    return IdentityEnvelope(
        subject="service:intake-agent",
        actor_kind="service",
        authenticated=True,
        auth_method="workload-identity",
        role_claims=("intake",),
    )


@pytest.fixture
def provenance() -> ProvenanceEnvelope:
    return ProvenanceEnvelope(
        artifact_type="DRAFT_CONTRACT",
        schema_version=1,
        producing_component="sp0-test@0.0.0",
    )
```

   Then `tests/sp0/documents/test_migration.py`:

```python
from __future__ import annotations

import psycopg
import pytest


def _table_exists(db, name: str) -> bool:
    row = db.execute(
        "SELECT to_regclass(%s) IS NOT NULL", (f"public.{name}",)
    ).fetchone()
    return bool(row[0])


def test_phase02_tables_exist(db):
    for name in ("documents", "stage_primary", "blob_index", "document_type_registry"):
        assert _table_exists(db, name), name


def _insert_doc(db, doc_id: str) -> None:
    db.execute(
        """
        INSERT INTO documents
            (doc_id, stage, schema_version, branch_role, content_hash,
             body_classification, actor, provenance)
        VALUES (%s, 'DRAFT_CONTRACT', 1, 'candidate', 'sha256:x',
                'pii-erasable', '{}'::jsonb, '{}'::jsonb)
        """,
        (doc_id,),
    )


def test_documents_are_write_once_no_update(db):
    _insert_doc(db, "doc_wo_update")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute(
                "UPDATE documents SET branch_role='primary' WHERE doc_id='doc_wo_update'"
            )


def test_documents_are_write_once_no_delete(db):
    _insert_doc(db, "doc_wo_delete")
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute("DELETE FROM documents WHERE doc_id='doc_wo_delete'")


def test_one_live_primary_per_run_stage_is_unique(db):
    _insert_doc(db, "doc_primary_a")
    db.execute(
        "INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq) "
        "VALUES ('run_1', 'DRAFT_CONTRACT', 'doc_primary_a', 1)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        with db.transaction():
            db.execute(
                "INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq) "
                "VALUES ('run_1', 'DRAFT_CONTRACT', 'doc_primary_a', 2)"
            )
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_migration.py -q`
   - Expected: `test_phase02_tables_exist` fails (`assert False` for `documents`) because `0002_documents.sql` does not exist.

3. Write minimal implementation — `src/sp0/db/migrations/0002_documents.sql` (DDL verbatim from the shared contract, plus the write-once trigger this phase owns):

```sql
-- =========================================================================
-- documents — immutable staged document DAG (§3.4). Write-once; no UPDATE.
-- =========================================================================
CREATE TABLE documents (
    doc_id              text        PRIMARY KEY,                  -- 'doc_...'
    global_seq          bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    request_id          text        NULL,
    feature_id          text        NULL,
    run_id              text        NULL,
    stage               text        NOT NULL,                     -- stage/artifact enum (§3.7)
    schema_version      integer     NOT NULL,
    branch_role         text        NOT NULL CHECK (branch_role IN ('candidate','primary','rejected','repair')),
    derived_from        text[]      NOT NULL DEFAULT '{}',        -- inputs; MUST reference committed docs (lower global_seq)
    supersedes          text[]      NOT NULL DEFAULT '{}',
    body_ref            text        NULL,                         -- blob id in blob_index; payload by reference (§9)
    content_hash        text        NOT NULL,                     -- 'sha256:...'
    body_classification text        NOT NULL CHECK (body_classification IN ('pii-erasable','governance-retained')),
    actor               jsonb       NOT NULL,
    provenance          jsonb       NOT NULL,
    reject_reason       text        NULL,                         -- required when branch_role='rejected'
    created_at          timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT documents_stage_enum CHECK (stage IN (
        'DRAFT_CONTRACT','ASSUMPTION_LEDGER','CONFIRMED_CONTRACT','MAPPED_CONTRACT',
        'FEATURE_PLAN','CANDIDATE_SQL','VALIDATION_REPORT','SANDBOX_RESULT','DQ_REPORT',
        'EVALUATION_REPORT','RISK_ASSESSMENT','EXPLAINABILITY','MONITORING_SPEC','APPROVAL_RECORD'
    )),
    CONSTRAINT documents_reject_reason_present CHECK (
        branch_role <> 'rejected' OR reject_reason IS NOT NULL
    )
);
CREATE INDEX documents_run_stage_idx ON documents (run_id, stage);
CREATE INDEX documents_global_idx    ON documents (global_seq);

-- Write-once enforcement (no UPDATE/DELETE) — installed as a row trigger by Phase 02.
CREATE OR REPLACE FUNCTION documents_write_once() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'documents are write-once: % not allowed on doc_id=%',
        TG_OP, COALESCE(OLD.doc_id, NEW.doc_id);
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER documents_no_mutation
    BEFORE UPDATE OR DELETE ON documents
    FOR EACH ROW EXECUTE FUNCTION documents_write_once();

-- =========================================================================
-- stage_primary — projection of PRIMARY_SELECTED (§3.4). Fail-closed.
-- Enforces "one live primary per (run_id, stage)"; current = highest global_seq.
-- =========================================================================
CREATE TABLE stage_primary (
    run_id        text        NOT NULL,
    stage         text        NOT NULL,
    doc_id        text        NOT NULL REFERENCES documents(doc_id),
    selected_seq  bigint      NOT NULL,                           -- global_seq of the winning PRIMARY_SELECTED
    selected_at   timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_live_primary_per_run_stage ON stage_primary (run_id, stage);

-- =========================================================================
-- blob_index (documents-index) — object-store index for mark-and-sweep blob GC (§5.1).
-- Schema owned here; GC mechanism built by Phase 05.
-- =========================================================================
CREATE TABLE blob_index (
    blob_id        text        PRIMARY KEY,                       -- 'blob_...'
    object_key     text        NOT NULL,                          -- key in the S3-compatible store
    content_hash   text        NOT NULL,                          -- 'sha256:...'
    classification text        NOT NULL CHECK (classification IN ('pii-erasable','governance-retained')),
    kms_key_id     text        NULL,                              -- per-body KMS key (crypto-shred target)
    referenced     boolean     NOT NULL DEFAULT false,            -- set true once a committed *_ref points at it
    status         text        NOT NULL DEFAULT 'live'
                       CHECK (status IN ('live','orphan','quarantined','swept','shredded')),
    size_bytes     bigint      NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    swept_at       timestamptz NULL
);
CREATE INDEX blob_index_gc_idx ON blob_index (status) WHERE status IN ('orphan','quarantined');

-- =========================================================================
-- document_type_registry — versioned document/artifact schemas + upcasters (§3.7).
-- =========================================================================
CREATE TABLE document_type_registry (LIKE event_type_registry INCLUDING ALL);
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_migration.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: documents/stage_primary/blob_index/document_type_registry migration + write-once trigger

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 3 — Document store: append_document core + content hashing + read-back

**Files:**
- Create: `src/sp0/documents/__init__.py` (empty package marker)
- Create: `src/sp0/documents/store.py`
- Test: `tests/sp0/documents/test_store_append.py`

**Interfaces:**
- Consumes: `from sp0.contracts import DbConn, IdentityEnvelope`; `from sp0.contracts.documents import NewDocument`; `from sp0.ids import new_id`; `from psycopg.types.json import Jsonb`.
- Produces:
  - `append_document(conn, new_document, *, run_id=None, feature_id=None, request_id=None, actor) -> str` — inserts one frozen document inside the caller's open transaction (§5.1), allocating `doc_id` (`new_id("doc")`) + `global_seq` (DB default); returns the `doc_id`. The body is opaque-by-reference (`body_ref` + `content_hash`); body schema validation is the producer's job (Task 6/Task 8) before the blob is written.
  - `get_document(conn, doc_id) -> Optional[dict[str, Any]]`.
  - `compute_content_hash(body: bytes) -> str` returning `"sha256:<hex>"`.

**TDD cycle 3.1 — append a candidate document and read it back; global_seq is monotonic**

1. Write the failing test — `tests/sp0/documents/test_store_append.py`:

```python
from __future__ import annotations

from sp0.contracts.documents import NewDocument
from sp0.documents.store import append_document, compute_content_hash, get_document


def _candidate(provenance, content_hash="sha256:x", body_ref="blob_1"):
    return NewDocument(
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash=content_hash,
        body_classification="pii-erasable",
        provenance=provenance,
        body_ref=body_ref,
    )


def test_compute_content_hash_is_deterministic_and_prefixed():
    h = compute_content_hash(b"hello")
    assert h.startswith("sha256:")
    assert h == compute_content_hash(b"hello")
    assert h != compute_content_hash(b"world")


def test_append_document_returns_doc_id_and_stores_fields(db, actor, provenance):
    doc_id = append_document(
        db, _candidate(provenance), run_id="run_1", actor=actor
    )
    assert doc_id.startswith("doc_")
    row = get_document(db, doc_id)
    assert row["stage"] == "DRAFT_CONTRACT"
    assert row["branch_role"] == "candidate"
    assert row["run_id"] == "run_1"
    assert row["body_ref"] == "blob_1"
    assert row["content_hash"] == "sha256:x"
    assert row["body_classification"] == "pii-erasable"


def test_global_seq_is_monotonic_across_appends(db, actor, provenance):
    a = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    assert get_document(db, b)["global_seq"] > get_document(db, a)["global_seq"]


def test_get_document_returns_none_for_unknown(db):
    assert get_document(db, "doc_missing") is None
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_store_append.py -q`
   - Expected: `ModuleNotFoundError: No module named 'sp0.documents'`.

3. Write minimal implementation — `src/sp0/documents/__init__.py` (empty), then `src/sp0/documents/store.py`:

```python
from __future__ import annotations

import hashlib
from dataclasses import asdict
from typing import Any, Optional

from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, IdentityEnvelope
from sp0.contracts.documents import NewDocument
from sp0.ids import new_id

_GET_COLUMNS = (
    "doc_id", "global_seq", "request_id", "feature_id", "run_id", "stage",
    "schema_version", "branch_role", "derived_from", "supersedes", "body_ref",
    "content_hash", "body_classification", "reject_reason",
)


def compute_content_hash(body: bytes) -> str:
    """Content-address a body: 'sha256:<hex>' (§3.4)."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


def append_document(
    conn: DbConn,
    new_document: NewDocument,
    *,
    run_id: Optional[str] = None,
    feature_id: Optional[str] = None,
    request_id: Optional[str] = None,
    actor: IdentityEnvelope,
) -> str:
    """Insert one frozen document inside the caller's OPEN transaction (§5.1).
    Allocates doc_id + global_seq. The body is opaque-by-reference (body_ref +
    content_hash); structural and DAG validation are added in Tasks 4-5."""
    doc_id = new_id("doc")
    conn.execute(
        """
        INSERT INTO documents (
            doc_id, request_id, feature_id, run_id, stage, schema_version,
            branch_role, derived_from, supersedes, body_ref, content_hash,
            body_classification, actor, provenance, reject_reason
        ) VALUES (
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s
        )
        """,
        (
            doc_id, request_id, feature_id, run_id, new_document.stage,
            new_document.schema_version, new_document.branch_role,
            list(new_document.derived_from), list(new_document.supersedes),
            new_document.body_ref, new_document.content_hash,
            new_document.body_classification,
            Jsonb(asdict(actor)), Jsonb(asdict(new_document.provenance)),
            new_document.reject_reason,
        ),
    )
    return doc_id


def get_document(conn: DbConn, doc_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        f"SELECT {', '.join(_GET_COLUMNS)} FROM documents WHERE doc_id = %s",
        (doc_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(_GET_COLUMNS, row))
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_store_append.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: append_document core + content hashing + read-back

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 4 — DAG lineage validation (acyclicity by construction)

**Files:**
- Modify: `src/sp0/documents/store.py`
- Test: `tests/sp0/documents/test_store_dag.py`

**Interfaces:**
- Produces: `DagViolationError(Exception)`; `append_document` now rejects any `derived_from`/`supersedes` id that is not already committed. Existence-of-reference ⇒ lower `global_seq` ⇒ acyclic-by-construction (§3.4).

**TDD cycle 4.1 — edges may only point at already-committed docs**

1. Write the failing test — `tests/sp0/documents/test_store_dag.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts.documents import NewDocument
from sp0.documents.store import DagViolationError, append_document, get_document


def _doc(provenance, *, stage="CONFIRMED_CONTRACT", derived_from=(), supersedes=()):
    return NewDocument(
        stage=stage,
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="governance-retained",
        provenance=provenance,
        body_ref="blob_1",
        derived_from=tuple(derived_from),
        supersedes=tuple(supersedes),
    )


def test_derived_from_committed_doc_is_accepted(db, actor, provenance):
    draft = append_document(
        db, _doc(provenance, stage="DRAFT_CONTRACT"), run_id="run_1", actor=actor
    )
    confirmed = append_document(
        db, _doc(provenance, derived_from=(draft,)), run_id="run_1", actor=actor
    )
    assert get_document(db, confirmed)["derived_from"] == [draft]


def test_derived_from_unknown_doc_is_rejected(db, actor, provenance):
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, derived_from=("doc_does_not_exist",)),
            run_id="run_1", actor=actor,
        )


def test_supersedes_unknown_doc_is_rejected(db, actor, provenance):
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, supersedes=("doc_ghost",)),
            run_id="run_1", actor=actor,
        )


def test_rejecting_bad_edge_inserts_nothing(db, actor, provenance):
    before = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    with pytest.raises(DagViolationError):
        append_document(
            db, _doc(provenance, derived_from=("doc_ghost",)),
            run_id="run_1", actor=actor,
        )
    after = db.execute("SELECT count(*) FROM documents").fetchone()[0]
    assert after == before
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_store_dag.py -q`
   - Expected: `ImportError: cannot import name 'DagViolationError'` (and the unknown-edge appends would otherwise succeed).

3. Write minimal implementation — in `src/sp0/documents/store.py` add the error class and a validation helper, and call it at the top of `append_document` before minting the id:

```python
class DagViolationError(Exception):
    """Raised when derived_from/supersedes references a doc that is not already committed."""


def _validate_dag(conn: DbConn, new_document: NewDocument) -> None:
    refs = tuple(new_document.derived_from) + tuple(new_document.supersedes)
    if not refs:
        return
    found = {
        r[0]
        for r in conn.execute(
            "SELECT doc_id FROM documents WHERE doc_id = ANY(%s)", (list(refs),)
        ).fetchall()
    }
    missing = [r for r in refs if r not in found]
    if missing:
        raise DagViolationError(
            f"derived_from/supersedes reference uncommitted docs: {missing}"
        )
```

   Insert the call as the first statement inside `append_document`:

```python
    _validate_dag(conn, new_document)
    doc_id = new_id("doc")
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_store_dag.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: DAG lineage validation (acyclicity by construction)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 5 — Structural validation + branch_role immutability

**Files:**
- Modify: `src/sp0/documents/store.py`
- Test: `tests/sp0/documents/test_store_validation.py`

**Interfaces:**
- Produces: `DocumentValidationError(Exception)`; `append_document` now rejects unknown `stage`/`branch_role`/`body_classification` and a `rejected` document missing `reject_reason` with a typed error (the DB CHECKs are the backstop). `branch_role` immutability is structural — proven via the write-once trigger.

**TDD cycle 5.1 — structural invariants and immutable branch_role**

1. Write the failing test — `tests/sp0/documents/test_store_validation.py`:

```python
from __future__ import annotations

import psycopg
import pytest

from sp0.contracts.documents import NewDocument
from sp0.documents.store import (
    DocumentValidationError,
    append_document,
)


def _doc(provenance, **over):
    base = dict(
        stage="DRAFT_CONTRACT",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="pii-erasable",
        provenance=provenance,
        body_ref="blob_1",
    )
    base.update(over)
    return NewDocument(**base)


def test_unknown_stage_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, stage="NOT_A_STAGE"), run_id="r", actor=actor)


def test_unknown_branch_role_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(db, _doc(provenance, branch_role="winner"), run_id="r", actor=actor)


def test_unknown_classification_rejected(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(
            db, _doc(provenance, body_classification="secret"), run_id="r", actor=actor
        )


def test_rejected_requires_reject_reason(db, actor, provenance):
    with pytest.raises(DocumentValidationError):
        append_document(
            db, _doc(provenance, branch_role="rejected"), run_id="r", actor=actor
        )


def test_rejected_with_reason_is_accepted(db, actor, provenance):
    doc_id = append_document(
        db,
        _doc(provenance, branch_role="rejected", reject_reason="dup of feat_9"),
        run_id="r",
        actor=actor,
    )
    assert doc_id.startswith("doc_")


def test_branch_role_is_immutable_after_commit(db, actor, provenance):
    doc_id = append_document(db, _doc(provenance), run_id="r", actor=actor)
    with pytest.raises(psycopg.errors.RaiseException):
        with db.transaction():
            db.execute(
                "UPDATE documents SET branch_role='primary' WHERE doc_id=%s", (doc_id,)
            )
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_store_validation.py -q`
   - Expected: `ImportError: cannot import name 'DocumentValidationError'`.

3. Write minimal implementation — in `src/sp0/documents/store.py` add the error and validator, importing the vocab:

```python
from sp0.contracts.documents import (
    BODY_CLASSIFICATIONS,
    BRANCH_ROLES,
    STAGES,
    NewDocument,
)


class DocumentValidationError(Exception):
    """Raised when a NewDocument violates a structural invariant before insert."""


def _validate_structure(new_document: NewDocument) -> None:
    if new_document.stage not in STAGES:
        raise DocumentValidationError(f"unknown stage: {new_document.stage!r}")
    if new_document.branch_role not in BRANCH_ROLES:
        raise DocumentValidationError(f"unknown branch_role: {new_document.branch_role!r}")
    if new_document.body_classification not in BODY_CLASSIFICATIONS:
        raise DocumentValidationError(
            f"unknown body_classification: {new_document.body_classification!r}"
        )
    if new_document.branch_role == "rejected" and not new_document.reject_reason:
        raise DocumentValidationError("branch_role='rejected' requires reject_reason")
```

   Call it before `_validate_dag` in `append_document`:

```python
    _validate_structure(new_document)
    _validate_dag(conn, new_document)
    doc_id = new_id("doc")
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_store_validation.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: structural validation + immutable branch_role

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 6 — Document schema registry (validate, chained upcasters, deprecation, snapshot)

**Files:**
- Create: `src/sp0/documents/registry.py`
- Test: `tests/sp0/documents/test_registry.py`

**Interfaces:**
- Consumes: `from sp0.contracts import DbConn, SchemaRegistry, SchemaValidationError, Upcaster`; `document_type_registry`, `registry_snapshots` (DDL); `jsonschema`.
- Produces: `DocumentSchemaRegistry` — a second `SchemaRegistry` implementation (the contract states it is "implemented twice: events and documents") backed by `document_type_registry` (+ `registry_snapshots` for `snapshot_version`). Construct per-connection: `DocumentSchemaRegistry(conn)`. Implements `register_schema`, `validate` (status-agnostic so in-flight docs at deprecated/withdrawn versions stay readable), `register_upcaster`/`upcast` (chained, total — a missing step is a poison error), the §3.3 deprecation-lifecycle gate `assert_writable(type_name, schema_version)` (deprecated => no new writes; withdrawn => upcast-only), and `snapshot_version()` whose `registry_snapshots.contents` is exactly `{type_name: max_active_version}` (no extra keys).

**TDD cycle 6.1 — register_schema + validate**

1. Write the failing test — `tests/sp0/documents/test_registry.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.documents.registry import DocumentSchemaRegistry

_SCHEMA = {
    "type": "object",
    "required": ["x"],
    "properties": {"x": {"type": "integer"}},
    "additionalProperties": False,
}


def test_validate_accepts_conforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    reg.validate("FEATURE_PLAN", 1, {"x": 7})  # no raise


def test_validate_rejects_nonconforming_body(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("FEATURE_PLAN", 1, _SCHEMA, owner="sp0")
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 1, {"x": "not-an-int"})


def test_validate_unregistered_type_raises(db):
    reg = DocumentSchemaRegistry(db)
    with pytest.raises(SchemaValidationError):
        reg.validate("FEATURE_PLAN", 99, {"x": 1})
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_registry.py -q`
   - Expected: `ModuleNotFoundError: No module named 'sp0.documents.registry'`.

3. Write minimal implementation — `src/sp0/documents/registry.py`:

```python
from __future__ import annotations

from typing import Any, Mapping

import jsonschema
from psycopg.types.json import Jsonb

from sp0.contracts import DbConn, SchemaValidationError


class DocumentSchemaRegistry:
    """Document/artifact SchemaRegistry over document_type_registry (§3.7).

    Construct per-connection: DocumentSchemaRegistry(conn). This cycle (6.1)
    ships register_schema + validate + the private _load_schema only. Chained
    reader-upcasters are added in cycle 6.2; snapshot_version + the deprecation
    lifecycle (assert_writable, _active_max_versions) are added in cycle 6.3."""

    def __init__(self, conn: DbConn) -> None:
        self._conn = conn

    def register_schema(
        self,
        type_name: str,
        schema_version: int,
        json_schema: Mapping[str, Any],
        owner: str,
        *,
        status: str = "active",
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO document_type_registry
                (type_name, schema_version, json_schema, owner, status)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (type_name, schema_version) DO UPDATE
                SET json_schema = EXCLUDED.json_schema,
                    owner = EXCLUDED.owner,
                    status = EXCLUDED.status
            """,
            (type_name, schema_version, Jsonb(dict(json_schema)), owner, status),
        )

    def validate(self, type_name: str, schema_version: int, body: Mapping[str, Any]) -> None:
        """Validate body against the registered schema. STATUS-AGNOSTIC by design:
        deprecated/withdrawn versions stay READABLE for in-flight docs (§3.3); the
        "no new writes" rule is enforced separately by assert_writable (cycle 6.3)."""
        schema = self._load_schema(type_name, schema_version)
        try:
            jsonschema.validate(instance=dict(body), schema=schema)
        except jsonschema.ValidationError as exc:
            raise SchemaValidationError(
                f"{type_name}@v{schema_version}: {exc.message}"
            ) from exc

    def _load_schema(self, type_name: str, schema_version: int) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT json_schema FROM document_type_registry "
            "WHERE type_name=%s AND schema_version=%s",
            (type_name, schema_version),
        ).fetchone()
        if row is None:
            raise SchemaValidationError(
                f"unregistered type {type_name}@v{schema_version}"
            )
        return row[0]
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_registry.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: document schema registry — register + validate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

**TDD cycle 6.2 — chained reader-upcasters, total (missing step = poison)**

1. Write the failing test — append to `tests/sp0/documents/test_registry.py`:

```python
def test_upcast_chains_stepwise(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    reg.register_upcaster("DQ_REPORT", 2, 3, lambda b: {**b, "v3": True})
    out = reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)
    assert out == {"v1": True, "v2": True, "v3": True}


def test_upcast_missing_step_is_poison(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    with pytest.raises(SchemaValidationError):
        reg.upcast("DQ_REPORT", {"v1": True}, 1, 3)


def test_upcaster_must_be_stepwise():
    reg = DocumentSchemaRegistry.__new__(DocumentSchemaRegistry)
    reg._upcasters = {}
    with pytest.raises(ValueError):
        reg.register_upcaster("DQ_REPORT", 1, 3, lambda b: b)
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_registry.py -q -k upcast`
   - Expected: `AttributeError: 'DocumentSchemaRegistry' object has no attribute 'register_upcaster'` (cycle 6.1 shipped only `register_schema`/`validate`/`_load_schema`, so neither `register_upcaster` nor `upcast` exists yet).

3. Write minimal implementation — extend `src/sp0/documents/registry.py` with the upcaster machinery:
   - Add `Upcaster` to the contracts import, so the header reads
     `from sp0.contracts import DbConn, SchemaValidationError, Upcaster`.
   - Add the in-memory upcaster map to `__init__` (after `self._conn = conn`):
     `self._upcasters: dict[tuple[str, int, int], Upcaster] = {}`.
   - Add the two methods (place them after `register_schema`):

```python
    def register_upcaster(
        self, type_name: str, from_version: int, to_version: int, upcaster: Upcaster
    ) -> None:
        if to_version != from_version + 1:
            raise ValueError("upcasters must be stepwise: to_version == from_version + 1")
        self._upcasters[(type_name, from_version, to_version)] = upcaster

    def upcast(
        self, type_name: str, body: Mapping[str, Any], from_version: int, to_version: int
    ) -> Mapping[str, Any]:
        if to_version < from_version:
            raise ValueError("cannot downcast")
        current: Mapping[str, Any] = dict(body)
        for v in range(from_version, to_version):
            step = self._upcasters.get((type_name, v, v + 1))
            if step is None:
                raise SchemaValidationError(
                    f"missing upcaster {type_name} v{v}->v{v + 1} (poison)"
                )
            current = dict(step(current))
        return current
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_registry.py -q -k upcast`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: document schema registry — chained/total reader-upcasters

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

**TDD cycle 6.3 — deprecation lifecycle + snapshot_version**

This cycle realizes the full §3.3/§3.7 lifecycle: a `schema_version` may be marked
`deprecated` (**no new writes**) then `withdrawn` (**only reachable via upcast**),
while deprecated/withdrawn versions **remain readable for in-flight docs**. The
pinnable `snapshot_version` pins only the max *active* version per type, and its
`registry_snapshots.contents` is exactly `{type_name: max_active_version}` — no
extra/non-type keys (the shared-contract DDL documents `contents` as that map, so a
downstream consumer can iterate its keys as stage/artifact types safely).

1. Write the failing test — append to `tests/sp0/documents/test_registry.py`:

```python
def test_snapshot_version_is_idempotent_when_unchanged(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("RISK_ASSESSMENT", 1, {"type": "object"}, owner="sp0")
    first = reg.snapshot_version()
    assert first == reg.snapshot_version()


def test_snapshot_advances_when_active_set_changes(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("RISK_ASSESSMENT", 1, {"type": "object"}, owner="sp0")
    first = reg.snapshot_version()
    reg.register_schema("RISK_ASSESSMENT", 2, {"type": "object"}, owner="sp0")
    second = reg.snapshot_version()
    assert first != second
    assert second.startswith("docs@v")


def test_deprecated_versions_excluded_from_snapshot(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("EXPLAINABILITY", 1, {"type": "object"}, owner="sp0",
                        status="deprecated")
    reg.register_schema("MONITORING_SPEC", 1, {"type": "object"}, owner="sp0")
    snap_id = reg.snapshot_version()
    contents = db.execute(
        "SELECT contents FROM registry_snapshots WHERE snapshot_id=%s", (snap_id,)
    ).fetchone()[0]
    assert "MONITORING_SPEC" in contents
    assert "EXPLAINABILITY" not in contents


def test_snapshot_contents_has_only_type_keys(db):
    # Regression: contents must be exactly {type_name: max_active_version}.
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("MONITORING_SPEC", 3, {"type": "object"}, owner="sp0")
    snap_id = reg.snapshot_version()
    contents = db.execute(
        "SELECT contents FROM registry_snapshots WHERE snapshot_id=%s", (snap_id,)
    ).fetchone()[0]
    assert contents == {"MONITORING_SPEC": 3}
    assert all(not k.startswith("_") for k in contents)  # no '_digest' pollution


def test_active_version_is_writable(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("DQ_REPORT", 5, {"type": "object"}, owner="sp0")
    reg.assert_writable("DQ_REPORT", 5)  # no raise


def test_no_new_writes_at_deprecated_version(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("VALIDATION_REPORT", 1, {"type": "object"}, owner="sp0")
    reg.register_schema("VALIDATION_REPORT", 1, {"type": "object"}, owner="sp0",
                        status="deprecated")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("VALIDATION_REPORT", 1)


def test_no_new_writes_at_withdrawn_version(db):
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("SANDBOX_RESULT", 1, {"type": "object"}, owner="sp0",
                        status="withdrawn")
    with pytest.raises(SchemaValidationError):
        reg.assert_writable("SANDBOX_RESULT", 1)


def test_deprecated_version_still_readable_for_inflight(db):
    # §3.3: deprecated/withdrawn versions remain READABLE (validate) for in-flight docs.
    reg = DocumentSchemaRegistry(db)
    schema = {"type": "object", "required": ["x"],
              "properties": {"x": {"type": "integer"}}}
    reg.register_schema("DQ_REPORT", 1, schema, owner="sp0", status="deprecated")
    reg.validate("DQ_REPORT", 1, {"x": 1})  # no raise — still readable


def test_withdrawn_version_reachable_via_upcast(db):
    # §3.3: a withdrawn version is reachable only via upcast (old data upcast on read).
    reg = DocumentSchemaRegistry(db)
    reg.register_schema("DQ_REPORT", 1, {"type": "object"}, owner="sp0",
                        status="withdrawn")
    reg.register_schema("DQ_REPORT", 2, {"type": "object"}, owner="sp0")
    reg.register_upcaster("DQ_REPORT", 1, 2, lambda b: {**b, "v2": True})
    out = reg.upcast("DQ_REPORT", {"v1": True}, 1, 2)
    assert out == {"v1": True, "v2": True}
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_registry.py -q -k "snapshot or writable or deprecated or withdrawn"`
   - Expected: `AttributeError: 'DocumentSchemaRegistry' object has no attribute 'snapshot_version'` (and `assert_writable`) — cycles 6.1/6.2 shipped neither. (`test_withdrawn_version_reachable_via_upcast` exercises 6.2's `upcast` plus the new status handling.)

3. Write minimal implementation — extend `src/sp0/documents/registry.py` with the lifecycle gate, the snapshot, and the active-set helper:

```python
    def assert_writable(self, type_name: str, schema_version: int) -> None:
        """Block NEW writes at a non-active version (§3.3): `deprecated` => no new
        writes; `withdrawn` => upcast-only. Deprecated/withdrawn versions stay
        READABLE via validate()/upcast() for in-flight docs. Producers call this
        before writing a new document body at (type_name, schema_version)."""
        row = self._conn.execute(
            "SELECT status FROM document_type_registry "
            "WHERE type_name=%s AND schema_version=%s",
            (type_name, schema_version),
        ).fetchone()
        if row is None:
            raise SchemaValidationError(f"unregistered type {type_name}@v{schema_version}")
        if row[0] != "active":
            raise SchemaValidationError(
                f"{type_name}@v{schema_version} is {row[0]}: no new writes "
                f"(deprecated => no new writes; withdrawn => upcast-only) (§3.3)"
            )

    def snapshot_version(self) -> str:
        """Pinnable doc-registry snapshot id ('docs@vN') recorded in provenance for
        replay determinism (§3.3/§8). `contents` is exactly {type_name:
        max_active_version} (matches the shared-contract DDL — no extra keys).
        Idempotent: an unchanged active set returns the existing snapshot id."""
        contents = self._active_max_versions()
        existing = self._conn.execute(
            "SELECT snapshot_id FROM registry_snapshots "
            "WHERE registry='docs' AND contents = %s",
            (Jsonb(contents),),
        ).fetchone()
        if existing:
            return existing[0]
        n = self._conn.execute(
            "SELECT count(*) FROM registry_snapshots WHERE registry='docs'"
        ).fetchone()[0] + 1
        snapshot_id = f"docs@v{n}"
        self._conn.execute(
            "INSERT INTO registry_snapshots (snapshot_id, registry, contents) "
            "VALUES (%s, 'docs', %s)",
            (snapshot_id, Jsonb(contents)),
        )
        return snapshot_id

    def _active_max_versions(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT type_name, max(schema_version) FROM document_type_registry "
            "WHERE status='active' GROUP BY type_name"
        ).fetchall()
        return {name: ver for name, ver in rows}
```

   Idempotency uses jsonb value-equality (`contents = %s`), which is key-order- and
   whitespace-independent, so an unchanged active map maps back to the same snapshot.

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_registry.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: document registry deprecation lifecycle + snapshot_version

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 7 — PRIMARY_SELECTED event + one-live-primary projection (current = max global_seq)

**Files:**
- Create: `src/sp0/documents/primary.py`
- Test: `tests/sp0/documents/test_primary.py`

**Interfaces:**
- Consumes: `from sp0.contracts import DbConn, EventEnvelope, IdentityEnvelope, NewEvent, ProvenanceEnvelope, ProjectionApplyError, run_projection`; `from sp0.events import append_event`; `from psycopg.types.json import Jsonb`; `append_document` (Task 3).
- Produces:
  - `PRIMARY_SELECTED: str` constant + `PRIMARY_SELECTED_SCHEMA_VERSION: int` + `PRIMARY_SELECTED_JSON_SCHEMA`.
  - `register_primary_selected(conn: DbConn) -> None` — registers the event type into `event_type_registry` (so `append_event`'s validation passes) and ensures the `stage_primary` checkpoint row exists in `projection_checkpoints` (so the Phase 01 `run_projection` runner can consume the projection); both inserts idempotent.
  - `new_primary_selected(*, run_id, stage, doc_id, actor, provenance, caused_by=None) -> NewEvent` — canonical builder.
  - `StagePrimaryProjection` — a fail-closed `Projection` (`is_analytics=False`) maintaining `stage_primary` (current = highest `global_seq`); raises `ProjectionApplyError` if the selected doc is unknown for `(run_id, stage)`.
  - `current_primary(conn, run_id, stage) -> Optional[str]`.

**TDD cycle 7.1 — promotion is an explicit event; current primary follows global_seq; one row per (run, stage)**

1. Write the failing test — `tests/sp0/documents/test_primary.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import ProjectionApplyError, run_projection
from sp0.contracts.documents import NewDocument
from sp0.documents.primary import (
    StagePrimaryProjection,
    current_primary,
    new_primary_selected,
    register_primary_selected,
)
from sp0.documents.store import append_document
from sp0.events import append_event


def _candidate(provenance):
    return NewDocument(
        stage="CANDIDATE_SQL",
        schema_version=1,
        branch_role="candidate",
        content_hash="sha256:x",
        body_classification="governance-retained",
        provenance=provenance,
        body_ref="blob_1",
    )


def _emit_primary(db, *, run_id, doc_id, expected_version, actor, provenance):
    ev = new_primary_selected(
        run_id=run_id, stage="CANDIDATE_SQL", doc_id=doc_id,
        actor=actor, provenance=provenance,
    )
    return append_event(db, ev, expected_version=expected_version, table_version=1)


def test_current_primary_is_the_latest_by_global_seq(db, actor, provenance):
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_1", actor=actor)
    e1 = _emit_primary(db, run_id="run_1", doc_id=a, expected_version=0,
                       actor=actor, provenance=provenance)
    e2 = _emit_primary(db, run_id="run_1", doc_id=b, expected_version=1,
                       actor=actor, provenance=provenance)

    proj = StagePrimaryProjection()
    proj.apply(db, e1)
    proj.apply(db, e2)

    assert current_primary(db, "run_1", "CANDIDATE_SQL") == b
    count = db.execute(
        "SELECT count(*) FROM stage_primary WHERE run_id='run_1' AND stage='CANDIDATE_SQL'"
    ).fetchone()[0]
    assert count == 1  # one live primary per (run_id, stage)


def test_out_of_order_lower_seq_does_not_override(db, actor, provenance):
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_2", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_2", actor=actor)
    e1 = _emit_primary(db, run_id="run_2", doc_id=a, expected_version=0,
                       actor=actor, provenance=provenance)
    e2 = _emit_primary(db, run_id="run_2", doc_id=b, expected_version=1,
                       actor=actor, provenance=provenance)

    proj = StagePrimaryProjection()
    proj.apply(db, e2)   # higher seq first
    proj.apply(db, e1)   # lower seq must not win
    assert current_primary(db, "run_2", "CANDIDATE_SQL") == b


def test_projection_is_fail_closed_on_unknown_doc(db, actor, provenance):
    register_primary_selected(db)
    ev = _emit_primary(db, run_id="run_3", doc_id="doc_ghost", expected_version=0,
                       actor=actor, provenance=provenance)
    proj = StagePrimaryProjection()
    assert proj.is_analytics is False
    with pytest.raises(ProjectionApplyError):
        proj.apply(db, ev)


def test_current_primary_none_when_unselected(db):
    assert current_primary(db, "run_none", "CANDIDATE_SQL") is None


def test_run_projection_applies_in_global_seq_order(db, actor, provenance):
    # End-to-end: StagePrimaryProjection is consumable by the Phase 01 runner,
    # which feeds events in global_seq order off the projection's checkpoint row
    # (created by register_primary_selected — see step 3).
    register_primary_selected(db)
    a = append_document(db, _candidate(provenance), run_id="run_4", actor=actor)
    b = append_document(db, _candidate(provenance), run_id="run_4", actor=actor)
    _emit_primary(db, run_id="run_4", doc_id=a, expected_version=0,
                  actor=actor, provenance=provenance)
    _emit_primary(db, run_id="run_4", doc_id=b, expected_version=1,
                  actor=actor, provenance=provenance)

    applied = run_projection(db, StagePrimaryProjection())
    assert applied >= 2
    assert current_primary(db, "run_4", "CANDIDATE_SQL") == b
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_primary.py -q`
   - Expected: `ModuleNotFoundError: No module named 'sp0.documents.primary'` (every test in the file fails at import, including `test_run_projection_applies_in_global_seq_order`).

3. Write minimal implementation — `src/sp0/documents/primary.py`:

```python
from __future__ import annotations

from typing import Optional

from psycopg.types.json import Jsonb

from sp0.contracts import (
    DbConn,
    EventEnvelope,
    IdentityEnvelope,
    NewEvent,
    ProjectionApplyError,
    ProvenanceEnvelope,
)

PRIMARY_SELECTED = "PRIMARY_SELECTED"
PRIMARY_SELECTED_SCHEMA_VERSION = 1
PRIMARY_SELECTED_JSON_SCHEMA = {
    "type": "object",
    "required": ["doc_id", "stage"],
    "properties": {
        "doc_id": {"type": "string"},
        "stage": {"type": "string"},
    },
    "additionalProperties": False,
}


def register_primary_selected(conn: DbConn) -> None:
    """Register PRIMARY_SELECTED in the event registry so append_event validation
    passes, and ensure the StagePrimaryProjection checkpoint row exists so the Phase
    01 `run_projection` runner can consume it (§3.6). Both inserts are idempotent."""
    conn.execute(
        """
        INSERT INTO event_type_registry
            (type_name, schema_version, json_schema, owner, status)
        VALUES (%s, %s, %s, 'sp0', 'active')
        ON CONFLICT (type_name, schema_version) DO NOTHING
        """,
        (PRIMARY_SELECTED, PRIMARY_SELECTED_SCHEMA_VERSION,
         Jsonb(PRIMARY_SELECTED_JSON_SCHEMA)),
    )
    # Phase 02 owns creation of its own checkpoint row (idempotent). This makes the
    # projection-runner path self-sufficient regardless of whether Phase 01's
    # run_projection self-initializes a missing checkpoint row (see Prerequisites).
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name) "
        "VALUES ('stage_primary') ON CONFLICT (projection_name) DO NOTHING"
    )


def new_primary_selected(
    *,
    run_id: str,
    stage: str,
    doc_id: str,
    actor: IdentityEnvelope,
    provenance: ProvenanceEnvelope,
    caused_by: Optional[str] = None,
) -> NewEvent:
    """Canonical PRIMARY_SELECTED builder (§3.4). Promotion is an event, never an in-place flip."""
    return NewEvent(
        aggregate="run",
        aggregate_id=run_id,
        type=PRIMARY_SELECTED,
        schema_version=PRIMARY_SELECTED_SCHEMA_VERSION,
        payload={"doc_id": doc_id, "stage": stage},
        actor=actor,
        provenance=provenance,
        run_id=run_id,
        caused_by=caused_by,
    )


class StagePrimaryProjection:
    """Fail-closed projection of PRIMARY_SELECTED into stage_primary (§3.4)."""

    name = "stage_primary"
    is_analytics = False

    def apply(self, conn: DbConn, event: EventEnvelope) -> None:
        if event.type != PRIMARY_SELECTED:
            return
        run_id = event.run_id
        doc_id = event.payload["doc_id"]
        stage = event.payload["stage"]
        known = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id=%s AND run_id=%s AND stage=%s",
            (doc_id, run_id, stage),
        ).fetchone()
        if known is None:
            raise ProjectionApplyError(
                "run", run_id or "",
                f"PRIMARY_SELECTED references unknown doc {doc_id} for ({run_id},{stage})",
            )
        conn.execute(
            """
            INSERT INTO stage_primary (run_id, stage, doc_id, selected_seq)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (run_id, stage) DO UPDATE
                SET doc_id = EXCLUDED.doc_id,
                    selected_seq = EXCLUDED.selected_seq,
                    selected_at = now()
                WHERE stage_primary.selected_seq < EXCLUDED.selected_seq
            """,
            (run_id, stage, doc_id, event.global_seq),
        )

    def reset(self, conn: DbConn) -> None:
        conn.execute("TRUNCATE stage_primary")


def current_primary(conn: DbConn, run_id: str, stage: str) -> Optional[str]:
    """The live primary doc_id for (run_id, stage), or None (§3.4)."""
    row = conn.execute(
        "SELECT doc_id FROM stage_primary WHERE run_id=%s AND stage=%s",
        (run_id, stage),
    ).fetchone()
    return row[0] if row else None
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_primary.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: PRIMARY_SELECTED event + one-live-primary projection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Task 8 — Normative Draft schema (raw_input by reference + classification, open_fields, ledger linkage)

**Files:**
- Create: `src/sp0/documents/draft.py`
- Test: `tests/sp0/documents/test_draft.py`

**Interfaces:**
- Consumes: `from sp0.contracts import SchemaValidationError`; `DocumentSchemaRegistry` (Task 6).
- Produces:
  - `DRAFT_CONTRACT_JSON_SCHEMA`, `ASSUMPTION_LEDGER_JSON_SCHEMA`, version constants, `RAW_INPUT_CLASSIFICATIONS`, `INTAKE_MODES`, `UNKNOWN` (the §3.5 unresolved-field sentinel).
  - `DraftValidationError(SchemaValidationError)`.
  - `validate_draft(body: Mapping[str, Any]) -> None` — SP-0's envelope/required-field validation (§3.5): `raw_input` never inline; `raw_input_ref` + `raw_input_classification` present; `intake_mode` valid; `open_fields` is a list; `assumption_ledger_ref` present; and **any field whose value is the `UNKNOWN` sentinel MUST be listed in `open_fields`** (§3.5: "unresolved fields are UNKNOWN and listed in open_fields"). Deeper semantic validation remains SP-2's.
  - `draft_has_open_fields(body) -> bool` — a Draft with non-empty `open_fields` cannot pass Gate #1 (§3.5; the gate itself is Phase 07).
  - `register_draft_schemas(registry) -> None` — registers `DRAFT_CONTRACT` + `ASSUMPTION_LEDGER` into the document registry.

**TDD cycle 8.1 — validate_draft enforces the §3.5 / §9 invariants**

1. Write the failing test — `tests/sp0/documents/test_draft.py`:

```python
from __future__ import annotations

import pytest

from sp0.contracts import SchemaValidationError
from sp0.documents.draft import (
    UNKNOWN,
    DraftValidationError,
    draft_has_open_fields,
    register_draft_schemas,
    validate_draft,
)
from sp0.documents.registry import DocumentSchemaRegistry


def _valid_draft(**over):
    body = {
        "request_id": "req_1",
        "intake_mode": "hypothesis",
        "raw_input_ref": "blob_raw_1",
        "raw_input_classification": "contains_pii",
        "target": "churn",
        "entity": "customer",
        "feature_concept": "salary irregularity",
        "open_fields": ["lookback_window"],
        "assumption_ledger_ref": "doc_led_1",
        "status": "NEEDS_CLARIFICATION",
    }
    body.update(over)
    return body


def test_valid_draft_passes():
    validate_draft(_valid_draft())


def test_inline_raw_input_is_rejected():
    bad = _valid_draft()
    bad["raw_input"] = "Customer SSN 123-45-6789 churns when..."
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_missing_assumption_ledger_ref_rejected():
    bad = _valid_draft()
    del bad["assumption_ledger_ref"]
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_invalid_classification_rejected():
    with pytest.raises(DraftValidationError):
        validate_draft(_valid_draft(raw_input_classification="maybe"))


def test_invalid_intake_mode_rejected():
    with pytest.raises(DraftValidationError):
        validate_draft(_valid_draft(intake_mode="freeform"))


def test_open_fields_signal_for_gate1():
    assert draft_has_open_fields(_valid_draft(open_fields=["lookback_window"])) is True
    assert draft_has_open_fields(_valid_draft(open_fields=[])) is False


def test_unknown_value_must_be_listed_in_open_fields():
    # §3.5: a field set to the UNKNOWN sentinel MUST appear in open_fields.
    bad = _valid_draft(target=UNKNOWN, open_fields=["lookback_window"])  # 'target' unlisted
    with pytest.raises(DraftValidationError):
        validate_draft(bad)


def test_unknown_value_listed_in_open_fields_passes():
    validate_draft(_valid_draft(target=UNKNOWN, open_fields=["target", "lookback_window"]))


def test_draft_validation_error_is_a_schema_validation_error():
    assert issubclass(DraftValidationError, SchemaValidationError)


def test_registered_draft_schema_blocks_inline_raw_input(db):
    reg = DocumentSchemaRegistry(db)
    register_draft_schemas(reg)
    reg.validate("DRAFT_CONTRACT", 1, _valid_draft())  # ok
    bad = _valid_draft()
    bad["raw_input"] = "secret"
    with pytest.raises(SchemaValidationError):
        reg.validate("DRAFT_CONTRACT", 1, bad)


def test_assumption_ledger_schema_registered(db):
    reg = DocumentSchemaRegistry(db)
    register_draft_schemas(reg)
    reg.validate(
        "ASSUMPTION_LEDGER", 1,
        {"request_id": "req_1",
         "assumptions": [{"field": "lookback_window", "value": 90,
                          "rationale": "default"}]},
    )
```

2. Run it, expect FAIL:
   - `python -m pytest tests/sp0/documents/test_draft.py -q`
   - Expected: `ModuleNotFoundError: No module named 'sp0.documents.draft'`.

3. Write minimal implementation — `src/sp0/documents/draft.py`:

```python
from __future__ import annotations

from typing import Any, Mapping

from sp0.contracts import SchemaValidationError

RAW_INPUT_CLASSIFICATIONS: tuple[str, ...] = ("contains_pii", "clean", "unscanned")
INTAKE_MODES: tuple[str, ...] = ("hypothesis", "definition")
UNKNOWN = "UNKNOWN"   # §3.5 sentinel: an unresolved Draft field; MUST be in open_fields

DRAFT_CONTRACT_SCHEMA_VERSION = 1
ASSUMPTION_LEDGER_SCHEMA_VERSION = 1

_DRAFT_REQUIRED = (
    "request_id", "intake_mode", "raw_input_ref", "raw_input_classification",
    "open_fields", "assumption_ledger_ref", "status",
)

DRAFT_CONTRACT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": list(_DRAFT_REQUIRED),
    "not": {"required": ["raw_input"]},   # raw_input MUST NOT be inline (§9)
    "properties": {
        "request_id": {"type": "string"},
        "intake_mode": {"enum": list(INTAKE_MODES)},
        "raw_input_ref": {"type": "string"},
        "raw_input_classification": {"enum": list(RAW_INPUT_CLASSIFICATIONS)},
        "hypothesis": {"type": "string"},
        "target": {"type": "string"},
        "entity": {"type": "string"},
        "feature_concept": {"type": "string"},
        "source_concepts": {"type": "array", "items": {"type": "string"}},
        "candidate_calculations": {"type": "array", "items": {"type": "string"}},
        "open_fields": {"type": "array", "items": {"type": "string"}},
        "assumption_ledger_ref": {"type": "string"},
        "status": {"type": "string"},
    },
    "additionalProperties": True,
}

ASSUMPTION_LEDGER_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["request_id", "assumptions"],
    "properties": {
        "request_id": {"type": "string"},
        "assumptions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["field", "value", "rationale"],
                "properties": {
                    "field": {"type": "string"},
                    "value": {},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
    "additionalProperties": True,
}


class DraftValidationError(SchemaValidationError):
    """Raised when a Draft body violates the normative §3.5 Draft schema."""


def validate_draft(body: Mapping[str, Any]) -> None:
    """SP-0 envelope + required-field validation for a Draft (§3.5). Semantic
    validation is SP-2's. raw_input is never inline (§9) — reference only."""
    if "raw_input" in body:
        raise DraftValidationError(
            "raw_input must never be inline; use raw_input_ref + classification (§9)"
        )
    missing = [k for k in _DRAFT_REQUIRED if k not in body]
    if missing:
        raise DraftValidationError(f"Draft missing required fields: {missing}")
    if body["raw_input_classification"] not in RAW_INPUT_CLASSIFICATIONS:
        raise DraftValidationError(
            f"invalid raw_input_classification: {body['raw_input_classification']!r}"
        )
    if body["intake_mode"] not in INTAKE_MODES:
        raise DraftValidationError(f"invalid intake_mode: {body['intake_mode']!r}")
    if not isinstance(body["open_fields"], list):
        raise DraftValidationError("open_fields must be a list")
    # §3.5: unresolved fields are UNKNOWN and listed in open_fields. Any field whose
    # value is the UNKNOWN sentinel must therefore appear in open_fields.
    open_fields = body["open_fields"]
    unknown_unlisted = [
        k for k, v in body.items()
        if isinstance(v, str) and v == UNKNOWN and k not in open_fields
    ]
    if unknown_unlisted:
        raise DraftValidationError(
            f"fields set to UNKNOWN must be listed in open_fields: {unknown_unlisted} (§3.5)"
        )
    if not body.get("assumption_ledger_ref"):
        raise DraftValidationError("assumption_ledger_ref is required")


def draft_has_open_fields(body: Mapping[str, Any]) -> bool:
    """True if the Draft still has unresolved fields (cannot pass Gate #1, §3.5)."""
    return bool(body.get("open_fields"))


def register_draft_schemas(registry) -> None:
    """Register DRAFT_CONTRACT + ASSUMPTION_LEDGER in the document registry (§3.7)."""
    registry.register_schema(
        "DRAFT_CONTRACT", DRAFT_CONTRACT_SCHEMA_VERSION,
        DRAFT_CONTRACT_JSON_SCHEMA, owner="sp0",
    )
    registry.register_schema(
        "ASSUMPTION_LEDGER", ASSUMPTION_LEDGER_SCHEMA_VERSION,
        ASSUMPTION_LEDGER_JSON_SCHEMA, owner="sp0",
    )
```

4. Run tests, expect PASS:
   - `python -m pytest tests/sp0/documents/test_draft.py -q`

5. Commit:
   - `git add -A && git commit -m "SP-0 Phase 02: normative Draft + Assumption-Ledger schema (raw_input by reference)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Final gate — run the whole phase suite

- `python -m pytest tests/sp0/documents -q` — all tasks green together (catches cross-task regressions, e.g. the `sp0.contracts.__init__` re-exports and the registry/draft wiring).
- Commit any fixups:
  - `git add -A && git commit -m "SP-0 Phase 02: full documents suite green

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"`

---

## Spec §12 error-handling/test coverage owned by this phase

These cases are required by §12 and are realized by the tasks above (no "write tests for the above" — each is a concrete test):

- **Write-once / immutability** — UPDATE and DELETE on `documents` rejected by the trigger; `branch_role` immutable after commit (Task 2 `test_documents_are_write_once_*`; Task 5 `test_branch_role_is_immutable_after_commit`).
- **DAG acyclicity by construction** — `derived_from`/`supersedes` may reference only already-committed docs; unknown/forward references rejected and insert nothing (Task 4).
- **One live primary / current-by-`global_seq`** — promotion is an explicit `PRIMARY_SELECTED` event (no in-place flip); latest `global_seq` wins; out-of-order lower-seq does not override; exactly one row per `(run_id, stage)` (Task 7 cycle 7.1).
- **Fail-closed projection** — `StagePrimaryProjection` raises `ProjectionApplyError` on a `PRIMARY_SELECTED` for an unknown doc; `is_analytics is False` (Task 7 cycle 7.1).
- **Document schema upcast** — chained/total reader-upcasters; a missing step is poison (raises), never silently skipped (Task 6 cycle 6.2).
- **Schema deprecation lifecycle (§3.3/§3.7)** — `deprecated` => no new writes and `withdrawn` => upcast-only, both enforced by `assert_writable`; deprecated/withdrawn versions remain readable for in-flight docs (`validate` is status-agnostic; a withdrawn version is still reachable via `upcast`); deprecated versions are excluded from the pinnable `snapshot_version`, whose `contents` is exactly `{type_name: max_active_version}` (no `_digest`/non-type keys) (Task 6 cycle 6.3).
- **Projection-runner consumption** — `StagePrimaryProjection` is consumable by the Phase 01 `run_projection` runner in `global_seq` order; `register_primary_selected` idempotently seeds the `projection_checkpoints` row (Task 7 cycle 7.1 `test_run_projection_applies_in_global_seq_order`).
- **Privacy (no raw PII in bodies)** — Draft `raw_input` never inline (rejected both by `validate_draft` and by the registered JSON schema's `not`); `raw_input_classification` required + enum-checked; assumption-ledger linkage required; `open_fields` gate signal; any field set to the `UNKNOWN` sentinel must be listed in `open_fields` (§3.5) (Task 8).
