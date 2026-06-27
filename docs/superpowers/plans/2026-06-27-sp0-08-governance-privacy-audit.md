## Phase 08: Governance attributes, provenance/replay, privacy/retention & attempt-memory

**Goal:** Supply the *mechanism* for SP-0's governance/privacy machinery — typed `feature_version` governance slots + pure approval/activation guard hooks over them, the `ProvenanceEnvelope` plus builder/validator, labeled full-vs-privacy-degraded replay, body-classification + crypto-shred (governance-retention + legal-hold exemption) + key rotation that never rewrites events, authorized-and-logged audit reads, and the non-PII cross-aggregate attempt-memory store (values/thresholds stay in SP-9/SP-10/SP-12).

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (or superpowers:executing-plans). Every task is one red → green → commit cycle. The Global Constraints and shared contract in `2026-06-27-sp0-00-overview.md` are authoritative; do not redefine shared symbols.

---

### File structure (one responsibility per file)

```
src/sp0/contracts/provenance.py        # ProvenanceEnvelope dataclass (verbatim contract symbol; Phase 08 is authoritative)
src/sp0/governance/__init__.py         # package marker
src/sp0/governance/provenance.py       # build_provenance, validate_provenance, ProvenanceError
src/sp0/governance/attributes.py       # GovernanceAttributes slots + validate + (de)serialize to feature_versions row
src/sp0/governance/predicates.py       # 7 governance guard predicates + register_governance_predicates
src/sp0/governance/replay.py           # ReplayMode, ArtifactReplayStatus, ReplayResult, replay_run
src/sp0/privacy/__init__.py            # package marker
src/sp0/privacy/classification.py      # body-classification consts + assert_references_only + validate_classification
src/sp0/privacy/kms.py                 # KeyManager Protocol
src/sp0/privacy/legal_hold.py          # place_legal_hold / release_legal_hold / is_under_legal_hold
src/sp0/privacy/crypto_shred.py        # crypto_shred, ErasureOutcome, rotate_blob_key, BlobNotFoundError
src/sp0/privacy/audit_read.py          # read_audit, AuditView, AuditReadDenied
src/sp0/attempt_memory/__init__.py     # package marker
src/sp0/attempt_memory/store.py        # AttemptMemoryEntry, record_attempt, lookup_attempt, count_candidates_explored
src/sp0/db/migrations/0810_attempt_memory.sql   # attempt_memory (shared DDL, verbatim; Phase 08 owns creation)
src/sp0/db/migrations/0820_legal_holds.sql      # legal_holds (Phase-08-owned NET-NEW table; not in the overview shared DDL)
src/sp0/db/migrations/0830_erasure_audit.sql    # erasure_audit (Phase-08-owned NET-NEW table; not in the overview shared DDL)

tests/sp0/_prereq_phase08.sql          # TEST-ONLY verbatim shared tables (global_seq_seq/events/documents/blob_index/feature_versions/security_audit; owned by Phases 01/02/05/06/07)
tests/sp0/_phase08_db.py               # builds the Phase-08 `db` fixture (prereq + every src/sp0/db/migrations/08*.sql)
tests/sp0/governance/conftest.py       # re-exports the Phase-08 `db` fixture for governance tests
tests/sp0/privacy/conftest.py          # re-exports the Phase-08 `db` fixture for privacy tests
tests/sp0/attempt_memory/conftest.py   # re-exports the Phase-08 `db` fixture for attempt-memory tests

tests/sp0/governance/test_harness.py
tests/sp0/governance/test_provenance.py
tests/sp0/governance/test_attributes.py
tests/sp0/governance/test_attributes_roundtrip.py
tests/sp0/governance/test_predicates.py
tests/sp0/governance/test_replay.py
tests/sp0/privacy/test_classification.py
tests/sp0/privacy/test_legal_hold.py
tests/sp0/privacy/test_crypto_shred.py
tests/sp0/privacy/test_key_rotation.py
tests/sp0/privacy/test_audit_read.py
tests/sp0/attempt_memory/test_store.py
```

### Consumed module paths & test harness

- **Shared contract symbols** (`IdentityEnvelope`, `EventEnvelope`, `Command`, `GuardPredicate`, `GuardInputs`, `PredicateRegistry`, `DbConn`) are imported from `sp0.contracts` (the shared interfaces package established by Phase 01). They are used VERBATIM — never redefined here.
- **`ProvenanceEnvelope`** is the one shared contract symbol Phase 08 is *authoritative* for. Phase 01 currently inlines it in `sp0.contracts.envelopes`; Task 1 reconciles that to a single source of truth in `sp0.contracts.provenance` and re-exports it from both `sp0.contracts.envelopes` and `sp0.contracts.__init__`, so `from sp0.contracts import ProvenanceEnvelope` (Phase 02 tests), `from sp0.contracts.envelopes import ProvenanceEnvelope` (Phase 02 modules), and `from sp0.contracts.provenance import ProvenanceEnvelope` (Phase 08) ALL resolve to the same class (no divergent duplicate — the overview's clearLayers rule).
- **`load_stream`** (Phase 01) is imported from `sp0.events`. Only the *import path* is Phase 01's detail; the signature is the shared-contract one. We rely on the documented behaviour that `load_stream(..., expected=None)` returns stored events with no upcasting (so replay tests can seed raw event rows).
- **`authorize_command` + `record_security_event` + `AuditReadDenied`** (Phase 07) live in `sp0.authz.policy` / `sp0.security.audit`. `read_audit` (Task 12) consumes `authorize_command(conn, cmd: Command) -> AuthzDecision` (the ONLY Phase-07 authz entrypoint — there is no `is_authorized`) and `record_security_event(conn, *, event_type, actor, attempted_action, decision, reason=None, aggregate=None, aggregate_id=None, retention_class="regulator") -> str`, and re-exports Phase 07's `AuditReadDenied` (single shared exception class, not redefined).
- **`db` pytest fixture — Phase-08-owned (Task 0).** The wider suite is inconsistent about the DB harness (Phase 01 yields a `conn` fixture and applies a Python `MIGRATIONS` list, not a `*.sql` glob; Phase 06 and Phase 07 each create a `tests/sp0/conftest.py` `db` fixture with different loaders). Rather than depend on any of those, Phase 08 OWNS its own `db` fixture (Task 0) via per-package conftests under `tests/sp0/governance/`, `tests/sp0/privacy/`, `tests/sp0/attempt_memory/` (these shadow any parent `tests/sp0/conftest.py` `db`). The fixture drops+recreates `public`, applies `tests/sp0/_prereq_phase08.sql` (the shared `global_seq_seq`/`events`/`documents`/`blob_index`/`feature_versions`/`security_audit` objects transcribed verbatim — owned by Phases 01/02/05/06/07, test-only), then applies every `src/sp0/db/migrations/08*.sql` that exists (Phase 08's `0810`/`0820`/`0830`), and rolls back after each test. `pytest.ini`/`pyproject.toml` put `src` on `pythonpath`; `SP0_TEST_DSN` (default `postgresql:///sp0_test`) selects the test database.
- All `*_id` keys are ULID-style prefixed strings; tests mint them with `uuid.uuid4().hex` and the appropriate prefix.

---

### Task 0 — Phase 08 test harness: `db` fixture + verbatim prereq schema + `08*.sql` migrations

This task makes every later DB-backed task (4, 7–12) actually reach a green step. Without it the
consumed `db` fixture would not provision `feature_versions`/`events`/`documents`/`blob_index`/
`security_audit`, and those tests would error on missing relations instead of passing.

**Files:**
- Create: `tests/sp0/_prereq_phase08.sql`, `tests/sp0/_phase08_db.py`, `tests/sp0/governance/conftest.py`, `tests/sp0/privacy/conftest.py`, `tests/sp0/attempt_memory/conftest.py`
- Test: `tests/sp0/governance/test_harness.py`

**Interfaces:**
- Consumes: `psycopg`; the shared `global_seq_seq`/`events`/`documents`/`blob_index`/`feature_versions`/`security_audit` DDL (transcribed verbatim, test-only — canonical owners are Phases 01/02/05/06/07); `SP0_TEST_DSN` (default `postgresql:///sp0_test`).
- Produces: a function-scoped `db` fixture (open psycopg connection; schema rebuilt per test; rolled back after) shared by all Phase-08 test packages. It applies the prereq, then every `src/sp0/db/migrations/08*.sql` that exists, so each later task's migration is picked up automatically once created.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_harness.py
def test_prereq_schema_and_global_seq_present(db):
    assert db.execute("SELECT nextval('global_seq_seq')").fetchone()[0] >= 1
    for table in ("events", "documents", "blob_index", "feature_versions", "security_audit"):
        present = db.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (f"public.{table}",)
        ).fetchone()[0]
        assert present, f"missing prereq table {table}"
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_harness.py -q
```
Expected: `fixture 'db' not found` (no conftest yet).

3. Write minimal implementation. First the verbatim prereq DDL (test-only):
```sql
-- tests/sp0/_prereq_phase08.sql
-- TEST-ONLY prerequisite DDL for the Phase 08 suite. Canonical owners: Phase 01
-- (global_seq_seq, events), Phase 02 (documents), Phase 05 (blob_index), Phase 06
-- (feature_versions), Phase 07 (security_audit). Transcribed verbatim from the shared
-- "Database schema" DDL in 2026-06-27-sp0-00-overview.md so this phase is independently
-- runnable. NEVER imported by src/.
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

CREATE TABLE documents (
    doc_id              text        PRIMARY KEY,
    global_seq          bigint      NOT NULL DEFAULT nextval('global_seq_seq'),
    request_id          text        NULL,
    feature_id          text        NULL,
    run_id              text        NULL,
    stage               text        NOT NULL,
    schema_version      integer     NOT NULL,
    branch_role         text        NOT NULL CHECK (branch_role IN ('candidate','primary','rejected','repair')),
    derived_from        text[]      NOT NULL DEFAULT '{}',
    supersedes          text[]      NOT NULL DEFAULT '{}',
    body_ref            text        NULL,
    content_hash        text        NOT NULL,
    body_classification text        NOT NULL CHECK (body_classification IN ('pii-erasable','governance-retained')),
    actor               jsonb       NOT NULL,
    provenance          jsonb       NOT NULL,
    reject_reason       text        NULL,
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

CREATE TABLE blob_index (
    blob_id        text        PRIMARY KEY,
    object_key     text        NOT NULL,
    content_hash   text        NOT NULL,
    classification text        NOT NULL CHECK (classification IN ('pii-erasable','governance-retained')),
    kms_key_id     text        NULL,
    referenced     boolean     NOT NULL DEFAULT false,
    status         text        NOT NULL DEFAULT 'live'
                       CHECK (status IN ('live','orphan','quarantined','swept','shredded')),
    size_bytes     bigint      NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    swept_at       timestamptz NULL
);

CREATE TABLE feature_versions (
    feature_version_id            text        PRIMARY KEY,
    feature_id                    text        NOT NULL,
    produced_by_run               text        NOT NULL,
    base_feature_version_id       text        NULL REFERENCES feature_versions(feature_version_id),
    verification_stamp            text        NOT NULL
                                      CHECK (verification_stamp IN ('DESIGN','DATA','USEFULNESS-CHECKED')),
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
```
Then the fixture builder (reads the prereq + globs Phase-08 migrations each call, so a migration
added by a later task is applied automatically):
```python
# tests/sp0/_phase08_db.py
from __future__ import annotations

import glob
import os
from pathlib import Path

import psycopg
import pytest

_HERE = Path(__file__).resolve().parent          # tests/sp0
_REPO_ROOT = _HERE.parents[1]                     # repo root
_MIGRATIONS_DIR = _REPO_ROOT / "src" / "sp0" / "db" / "migrations"


def _build_schema() -> str:
    parts = [(_HERE / "_prereq_phase08.sql").read_text()]
    for path in sorted(glob.glob(str(_MIGRATIONS_DIR / "08*.sql"))):
        parts.append(Path(path).read_text())
    return "\n".join(parts)


@pytest.fixture
def db():
    dsn = os.environ.get("SP0_TEST_DSN", "postgresql:///sp0_test")
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    conn.execute(_build_schema())
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()
```
Then the three per-package conftests re-export the fixture (each puts `tests/sp0` on `sys.path` so
the bare `_phase08_db` module imports without assuming any package layout):
```python
# tests/sp0/governance/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/sp0
from _phase08_db import db  # noqa: E402,F401  (re-export the Phase-08 `db` fixture)
```
```python
# tests/sp0/privacy/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/sp0
from _phase08_db import db  # noqa: E402,F401
```
```python
# tests/sp0/attempt_memory/conftest.py
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/sp0
from _phase08_db import db  # noqa: E402,F401
```

4. Run the test, expect PASS:
```
python -m pytest tests/sp0/governance/test_harness.py -q
```
Expected: `1 passed`. (At this point no `08*.sql` exist yet, so only the prereq tables are present — exactly what this test asserts. Tasks 7–9 add the `0810`/`0820`/`0830` migrations, which the glob then applies.)

5. Commit:
```
git add -A && git commit -m "test(sp0-08): Phase 08 db harness (verbatim prereq schema + 08*.sql glob)"
```

---

### Task 1 — `ProvenanceEnvelope` dataclass (the §8 reproducibility envelope)

**Files:**
- Create: `src/sp0/contracts/provenance.py`
- Modify: `src/sp0/contracts/__init__.py`, `src/sp0/contracts/envelopes.py` (single-source re-export of `ProvenanceEnvelope`)
- Test: `tests/sp0/governance/test_provenance.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces: `ProvenanceEnvelope` (frozen, slots) — the verbatim shared-contract symbol; Phase 08 is authoritative. Defined once in `sp0.contracts.provenance` and re-exported from `sp0.contracts.envelopes` and `sp0.contracts.__init__` so all consumer import paths resolve to ONE class. Imported by Phase 01 (`EventEnvelope.provenance`), Phase 02 (`NewDocument.provenance`), and every handler.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_provenance.py
from dataclasses import FrozenInstanceError

import pytest

from sp0.contracts.provenance import ProvenanceEnvelope


def test_provenance_envelope_is_frozen_slotted_and_carries_replay_pins():
    prov = ProvenanceEnvelope(
        artifact_type="CONFIRMED_CONTRACT",
        schema_version=2,
        producing_component="sp2-intake@1.4.0",
        tool_versions={"llm_model": "m@1", "prompt_version": "p@3"},
        dsl_operation_catalog_version="ops@v9",
        source_snapshots=("delta:core.transactions@v8821",),
        event_registry_snapshot="events@v37",
        doc_registry_snapshot="docs@v11",
        evaluation_dataset_ref="doc_eval",
        holdout_partition_spec="oot:2025H2",
        random_seed=42,
        candidates_explored_count=3,
        external_refs=("llm_call:idem_1",),
    )
    assert prov.artifact_type == "CONFIRMED_CONTRACT"
    assert prov.tool_versions["llm_model"] == "m@1"
    assert prov.random_seed == 42
    assert not hasattr(prov, "__dict__")  # slots=True
    with pytest.raises(FrozenInstanceError):
        prov.schema_version = 9  # type: ignore[misc]


def test_provenance_envelope_defaults_are_empty():
    prov = ProvenanceEnvelope(artifact_type="DRAFT_CONTRACT", schema_version=1, producing_component="sp0@1")
    assert prov.tool_versions == {}
    assert prov.source_snapshots == ()
    assert prov.event_registry_snapshot is None


def test_provenance_envelope_resolves_to_one_class_across_import_paths():
    # The overview's clearLayers rule: a shared symbol must be ONE class, not duplicated per phase.
    from sp0.contracts import ProvenanceEnvelope as P_pkg
    from sp0.contracts.envelopes import ProvenanceEnvelope as P_env
    from sp0.contracts.provenance import ProvenanceEnvelope as P_mod

    assert P_pkg is P_env is P_mod
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_provenance.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.contracts.provenance'`.

3. Write minimal implementation (verbatim from the shared contract):
```python
# src/sp0/contracts/provenance.py
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional


@dataclass(frozen=True, slots=True)
class ProvenanceEnvelope:
    """Reproducibility envelope on every event/document (§8)."""
    artifact_type: str                        # matches the §3.7 stage/artifact enum casing
    schema_version: int
    producing_component: str                  # "sp2-intake@1.4.0"
    tool_versions: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: Optional[str] = None
    source_snapshots: tuple[str, ...] = ()
    event_registry_snapshot: Optional[str] = None  # pinned snapshot id (replay determinism)
    doc_registry_snapshot: Optional[str] = None
    evaluation_dataset_ref: Optional[str] = None
    holdout_partition_spec: Optional[str] = None
    random_seed: Optional[int] = None
    candidates_explored_count: Optional[int] = None
    external_refs: tuple[str, ...] = ()
```
Then make this the single source of truth. Phase 01 currently inlines a copy of `ProvenanceEnvelope`
in `envelopes.py`; replace that inline definition with a re-export, and re-export from the package
`__init__` too, so every consumer import path returns ONE class (overview's clearLayers rule). `provenance.py`
imports stdlib only, so this introduces no cycle:
```python
# src/sp0/contracts/envelopes.py  — replace the inline `class ProvenanceEnvelope` with:
from sp0.contracts.provenance import ProvenanceEnvelope  # single source of truth (Phase 08 authoritative)
```
```python
# src/sp0/contracts/__init__.py  — ensure this re-export is present and "ProvenanceEnvelope" stays in __all__:
from sp0.contracts.provenance import ProvenanceEnvelope
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_provenance.py -q
```
Expected: `3 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): ProvenanceEnvelope reproducibility envelope + single-source re-export (§8)"
```

---

### Task 2 — `build_provenance` + `validate_provenance` (reproducibility builder & replay-pin / no-inline-PII validation)

**Files:**
- Create: `src/sp0/governance/__init__.py`, `src/sp0/governance/provenance.py`
- Test: `tests/sp0/governance/test_provenance.py` (extend)

**Interfaces:**
- Consumes: `ProvenanceEnvelope` (Task 1).
- Produces: `build_provenance(...) -> ProvenanceEnvelope`; `validate_provenance(prov, *, require_replay_pins=False) -> None`; `ProvenanceError`.

**TDD steps:**

1. Write the failing test (append):
```python
# tests/sp0/governance/test_provenance.py  (append)
from sp0.governance.provenance import ProvenanceError, build_provenance, validate_provenance


def test_build_provenance_folds_named_tool_versions():
    prov = build_provenance(
        artifact_type="EVALUATION_REPORT",
        schema_version=2,
        producing_component="sp6-eval@2.0.0",
        llm_model="m@1",
        prompt_version="p@3",
        validator="iv@1",
        compiler="dsl@9",
        event_registry_snapshot="events@v37",
        doc_registry_snapshot="docs@v11",
        random_seed=7,
        candidates_explored_count=5,
        external_refs=("sandbox_run:job_9",),
    )
    assert prov.tool_versions == {
        "llm_model": "m@1", "prompt_version": "p@3", "validator": "iv@1", "compiler": "dsl@9",
    }
    assert prov.candidates_explored_count == 5
    validate_provenance(prov)  # well-formed => no raise


def test_validate_provenance_requires_component_and_positive_schema_version():
    with pytest.raises(ProvenanceError):
        validate_provenance(ProvenanceEnvelope(artifact_type="X", schema_version=1, producing_component=""))
    with pytest.raises(ProvenanceError):
        validate_provenance(ProvenanceEnvelope(artifact_type="X", schema_version=0, producing_component="c"))


def test_validate_provenance_rejects_inline_external_refs_and_missing_replay_pins():
    inline = ProvenanceEnvelope(
        artifact_type="X", schema_version=1, producing_component="c",
        external_refs=("this is a raw inline body, not a ref",),
    )
    with pytest.raises(ProvenanceError):
        validate_provenance(inline)
    no_pins = ProvenanceEnvelope(artifact_type="X", schema_version=1, producing_component="c")
    with pytest.raises(ProvenanceError):
        validate_provenance(no_pins, require_replay_pins=True)
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_provenance.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.governance.provenance'`.

3. Write minimal implementation:
```python
# src/sp0/governance/__init__.py
```
```python
# src/sp0/governance/provenance.py
from __future__ import annotations

import re
from typing import Mapping, Optional

from sp0.contracts.provenance import ProvenanceEnvelope

_REF_RE = re.compile(r"^[^\s:]+:[^\s]+$")  # a reference id token "kind:id" — never an inline body (§9)


class ProvenanceError(Exception):
    """Raised when a ProvenanceEnvelope is not well-formed for audit/replay (§8/§9)."""


def build_provenance(
    *,
    artifact_type: str,
    schema_version: int,
    producing_component: str,
    llm_model: Optional[str] = None,
    prompt_version: Optional[str] = None,
    validator: Optional[str] = None,
    compiler: Optional[str] = None,
    tool_versions: Optional[Mapping[str, str]] = None,
    dsl_operation_catalog_version: Optional[str] = None,
    source_snapshots: tuple[str, ...] = (),
    event_registry_snapshot: Optional[str] = None,
    doc_registry_snapshot: Optional[str] = None,
    evaluation_dataset_ref: Optional[str] = None,
    holdout_partition_spec: Optional[str] = None,
    random_seed: Optional[int] = None,
    candidates_explored_count: Optional[int] = None,
    external_refs: tuple[str, ...] = (),
) -> ProvenanceEnvelope:
    merged: dict[str, str] = dict(tool_versions or {})
    for key, value in (
        ("llm_model", llm_model), ("prompt_version", prompt_version),
        ("validator", validator), ("compiler", compiler),
    ):
        if value is not None:
            merged[key] = value
    return ProvenanceEnvelope(
        artifact_type=artifact_type,
        schema_version=schema_version,
        producing_component=producing_component,
        tool_versions=merged,
        dsl_operation_catalog_version=dsl_operation_catalog_version,
        source_snapshots=source_snapshots,
        event_registry_snapshot=event_registry_snapshot,
        doc_registry_snapshot=doc_registry_snapshot,
        evaluation_dataset_ref=evaluation_dataset_ref,
        holdout_partition_spec=holdout_partition_spec,
        random_seed=random_seed,
        candidates_explored_count=candidates_explored_count,
        external_refs=external_refs,
    )


def validate_provenance(prov: ProvenanceEnvelope, *, require_replay_pins: bool = False) -> None:
    if not prov.artifact_type:
        raise ProvenanceError("artifact_type is required")
    if not prov.producing_component:
        raise ProvenanceError("producing_component is required")
    if prov.schema_version <= 0:
        raise ProvenanceError("schema_version must be > 0")
    for ref in prov.external_refs:
        if not _REF_RE.match(ref):
            raise ProvenanceError(f"external_ref {ref!r} must be a 'kind:id' reference, not inline content (§9)")
    if require_replay_pins and not (prov.event_registry_snapshot and prov.doc_registry_snapshot):
        raise ProvenanceError("replay determinism requires event_registry_snapshot and doc_registry_snapshot (§8)")
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_provenance.py -q
```
Expected: `6 passed` (3 from Task 1 + 3 here).

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): provenance builder + replay-pin/no-inline-PII validation (§8/§9)"
```

---

### Task 3 — `GovernanceAttributes` typed slots + `validate_governance_attributes` (§3.8 mechanism, no policy values)

**Files:**
- Create: `src/sp0/governance/attributes.py`
- Test: `tests/sp0/governance/test_attributes.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces: `GovernanceAttributes` (frozen, slots); `validate_governance_attributes(attrs) -> None`; `GovernanceAttributeError`; `VERIFICATION_STAMPS = ("DESIGN","DATA","USEFULNESS-CHECKED")`; `APPROVAL_TYPES = ("EXPERIMENTAL","PRODUCTION")`. (Slot vocabulary is SP-0-normative; thresholds/use-case meaning stay in SP-9/10/12.)

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_attributes.py
import pytest

from sp0.governance.attributes import (
    APPROVAL_TYPES,
    VERIFICATION_STAMPS,
    GovernanceAttributes,
    GovernanceAttributeError,
    validate_governance_attributes,
)


def _attrs(**over):
    base = dict(
        feature_version_id="fv_1", feature_id="feat_1", produced_by_run="run_1",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="medium", approval_type="PRODUCTION",
    )
    base.update(over)
    return GovernanceAttributes(**base)


def test_vocabularies_match_ddl_check_constraints():
    assert VERIFICATION_STAMPS == ("DESIGN", "DATA", "USEFULNESS-CHECKED")
    assert APPROVAL_TYPES == ("EXPERIMENTAL", "PRODUCTION")


def test_valid_attributes_pass():
    validate_governance_attributes(_attrs(approved_use_cases=("churn", "fraud"), max_uses=10))


def test_unknown_verification_stamp_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(verification_stamp="USEFULNESS_CHECKED"))  # underscore is wrong


def test_unknown_approval_type_and_nonpositive_max_uses_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(approval_type="MAYBE"))
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(max_uses=0))


def test_empty_required_ids_rejected():
    with pytest.raises(GovernanceAttributeError):
        validate_governance_attributes(_attrs(feature_version_id=""))
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_attributes.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.governance.attributes'`.

3. Write minimal implementation:
```python
# src/sp0/governance/attributes.py
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Mapping, Optional

VERIFICATION_STAMPS: tuple[str, ...] = ("DESIGN", "DATA", "USEFULNESS-CHECKED")
APPROVAL_TYPES: tuple[str, ...] = ("EXPERIMENTAL", "PRODUCTION")


class GovernanceAttributeError(Exception):
    """Raised when feature-version governance attributes are not well-formed (§3.8)."""


@dataclass(frozen=True, slots=True)
class GovernanceAttributes:
    """Typed §3.8 governance slots on a feature_version. SP-0 owns the slots; the values/
    thresholds (risk-tier meaning, use-case matrices, required stamp) are policy (SP-9/10/12)."""
    feature_version_id: str
    feature_id: str
    produced_by_run: str
    verification_stamp: str                         # DESIGN | DATA | USEFULNESS-CHECKED
    risk_tier: str                                  # free string; ordering/ceiling is policy
    approval_type: str                              # EXPERIMENTAL | PRODUCTION
    base_feature_version_id: Optional[str] = None
    approved_use_cases: tuple[str, ...] = ()
    blocked_use_cases: tuple[str, ...] = ()
    required_artifact_refs: Mapping[str, str] = field(default_factory=dict)
    dsl_operation_catalog_version: Optional[str] = None
    conditions: tuple[str, ...] = ()
    expires_at: Optional[datetime] = None
    max_uses: Optional[int] = None
    reviewed_evidence_refs: tuple[str, ...] = ()
    immutable: bool = True


def validate_governance_attributes(attrs: GovernanceAttributes) -> None:
    for name in ("feature_version_id", "feature_id", "produced_by_run", "risk_tier"):
        if not getattr(attrs, name):
            raise GovernanceAttributeError(f"{name} is required")
    if attrs.verification_stamp not in VERIFICATION_STAMPS:
        raise GovernanceAttributeError(f"verification_stamp {attrs.verification_stamp!r} not in {VERIFICATION_STAMPS}")
    if attrs.approval_type not in APPROVAL_TYPES:
        raise GovernanceAttributeError(f"approval_type {attrs.approval_type!r} not in {APPROVAL_TYPES}")
    if attrs.max_uses is not None and attrs.max_uses <= 0:
        raise GovernanceAttributeError("max_uses must be None or a positive integer")
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_attributes.py -q
```
Expected: `5 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): typed feature-version governance attribute slots (§3.8)"
```

---

### Task 4 — `to_feature_version_row` / `from_feature_version_row` (round-trip against the real `feature_versions` table)

**Files:**
- Modify: `src/sp0/governance/attributes.py`
- Test: `tests/sp0/governance/test_attributes_roundtrip.py`

**Interfaces:**
- Consumes: `GovernanceAttributes` (Task 3); shared `feature_versions` DDL (owned/created by Phase 06, present via the `db` fixture); `psycopg.types.json.Json`.
- Produces: `to_feature_version_row(attrs, *, content_hash) -> dict[str, object]` (column→value, jsonb pre-wrapped); `from_feature_version_row(row: Mapping) -> GovernanceAttributes`.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_attributes_roundtrip.py
from datetime import datetime, timezone

from sp0.governance.attributes import (
    GovernanceAttributes,
    from_feature_version_row,
    to_feature_version_row,
)


def test_governance_attributes_round_trip_through_feature_versions(db):
    attrs = GovernanceAttributes(
        feature_version_id="fv_rt", feature_id="feat_rt", produced_by_run="run_rt",
        base_feature_version_id="fv_base",
        verification_stamp="USEFULNESS-CHECKED", risk_tier="high", approval_type="PRODUCTION",
        approved_use_cases=("churn", "fraud"), blocked_use_cases=("credit_decisioning",),
        required_artifact_refs={"evaluation_report": "doc_e", "monitoring_spec": "doc_m"},
        dsl_operation_catalog_version="ops@v9",
        conditions=("review quarterly",),
        expires_at=datetime(2026, 12, 31, tzinfo=timezone.utc),
        max_uses=100, reviewed_evidence_refs=("doc_r",),
    )
    row = to_feature_version_row(attrs, content_hash="sha256:abc")
    cols = list(row.keys())
    db.execute(
        f"INSERT INTO feature_versions ({', '.join(cols)}) "
        f"VALUES ({', '.join(['%s'] * len(cols))})",
        [row[c] for c in cols],
    )
    fetched = db.execute(
        "SELECT feature_version_id, feature_id, produced_by_run, base_feature_version_id, "
        "verification_stamp, risk_tier, approval_type, approved_use_cases, blocked_use_cases, "
        "required_artifact_refs, dsl_operation_catalog_version, approval, expires_at, immutable "
        "FROM feature_versions WHERE feature_version_id = %s",
        ("fv_rt",),
    ).fetchone()
    keys = (
        "feature_version_id", "feature_id", "produced_by_run", "base_feature_version_id",
        "verification_stamp", "risk_tier", "approval_type", "approved_use_cases", "blocked_use_cases",
        "required_artifact_refs", "dsl_operation_catalog_version", "approval", "expires_at", "immutable",
    )
    back = from_feature_version_row(dict(zip(keys, fetched)))
    assert back == attrs
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_attributes_roundtrip.py -q
```
Expected: `ImportError: cannot import name 'to_feature_version_row' from 'sp0.governance.attributes'`.

3. Write minimal implementation (append to `attributes.py`):
```python
# src/sp0/governance/attributes.py  (append)
from psycopg.types.json import Json


def to_feature_version_row(attrs: GovernanceAttributes, *, content_hash: str) -> dict[str, object]:
    approval = {
        "conditions": list(attrs.conditions),
        "expires_at": attrs.expires_at.isoformat() if attrs.expires_at else None,
        "max_uses": attrs.max_uses,
        "reviewed_evidence_refs": list(attrs.reviewed_evidence_refs),
    }
    return {
        "feature_version_id": attrs.feature_version_id,
        "feature_id": attrs.feature_id,
        "produced_by_run": attrs.produced_by_run,
        "base_feature_version_id": attrs.base_feature_version_id,
        "verification_stamp": attrs.verification_stamp,
        "risk_tier": attrs.risk_tier,
        "approval_type": attrs.approval_type,
        "approved_use_cases": list(attrs.approved_use_cases),
        "blocked_use_cases": list(attrs.blocked_use_cases),
        "required_artifact_refs": Json(dict(attrs.required_artifact_refs)),
        "dsl_operation_catalog_version": attrs.dsl_operation_catalog_version,
        "approval": Json(approval),
        "expires_at": attrs.expires_at,
        "content_hash": content_hash,
        "immutable": attrs.immutable,
    }


def from_feature_version_row(row: Mapping[str, object]) -> GovernanceAttributes:
    approval = dict(row.get("approval") or {})
    return GovernanceAttributes(
        feature_version_id=str(row["feature_version_id"]),
        feature_id=str(row["feature_id"]),
        produced_by_run=str(row["produced_by_run"]),
        base_feature_version_id=row.get("base_feature_version_id") or None,  # type: ignore[arg-type]
        verification_stamp=str(row["verification_stamp"]),
        risk_tier=str(row["risk_tier"]),
        approval_type=str(row["approval_type"]),
        approved_use_cases=tuple(row.get("approved_use_cases") or ()),
        blocked_use_cases=tuple(row.get("blocked_use_cases") or ()),
        required_artifact_refs=dict(row.get("required_artifact_refs") or {}),
        dsl_operation_catalog_version=row.get("dsl_operation_catalog_version") or None,  # type: ignore[arg-type]
        conditions=tuple(approval.get("conditions") or ()),
        expires_at=row.get("expires_at") or None,  # type: ignore[arg-type]
        max_uses=approval.get("max_uses"),
        reviewed_evidence_refs=tuple(approval.get("reviewed_evidence_refs") or ()),
        immutable=bool(row["immutable"]),
    )
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_attributes_roundtrip.py -q
```
Expected: `1 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): governance-attribute (de)serialization to feature_versions row (§3.8)"
```

---

### Task 5 — Governance guard predicates + `register_governance_predicates` (approval/activation hooks; pure, declared-inputs-only)

**Files:**
- Create: `src/sp0/governance/predicates.py`
- Test: `tests/sp0/governance/test_predicates.py`

**Interfaces:**
- Consumes: `GuardPredicate`, `GuardInputs`, `PredicateRegistry` (shared contract, Phase 03); `VERIFICATION_STAMPS` (Task 3).
- Produces: the 7 predicate singletons `verification_stamp_satisfies`, `approval_type_is`, `use_case_not_blocked`, `required_artifact_present`, `risk_tier_within_ceiling`, `approval_not_expired`, `max_uses_not_exceeded`; `GOVERNANCE_PREDICATES: tuple[GuardPredicate, ...]`; `register_governance_predicates(registry: PredicateRegistry) -> None`. Each is PURE, reads only its `declared_inputs`, never a mutable projection (§4.1). Thresholds (`required_stamp`, `ceiling_rank`, `as_of`, …) are resolved inputs supplied by policy.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_predicates.py
from datetime import datetime, timezone

import pytest

from sp0.governance.predicates import (
    GOVERNANCE_PREDICATES,
    approval_not_expired,
    approval_type_is,
    max_uses_not_exceeded,
    register_governance_predicates,
    required_artifact_present,
    risk_tier_within_ceiling,
    use_case_not_blocked,
    verification_stamp_satisfies,
)


def test_verification_stamp_ordering_uses_sp0_normative_rank():
    assert verification_stamp_satisfies(
        {"verification_stamp": "USEFULNESS-CHECKED", "required_stamp": "USEFULNESS-CHECKED"}) is True
    assert verification_stamp_satisfies(
        {"verification_stamp": "DATA", "required_stamp": "USEFULNESS-CHECKED"}) is False
    assert verification_stamp_satisfies(
        {"verification_stamp": "USEFULNESS-CHECKED", "required_stamp": "DATA"}) is True


def test_use_case_block_and_artifact_presence_and_type_and_tier():
    assert use_case_not_blocked({"target_use_case": "fraud", "blocked_use_cases": ("credit_decisioning",)}) is True
    assert use_case_not_blocked({"target_use_case": "credit_decisioning", "blocked_use_cases": ("credit_decisioning",)}) is False
    assert required_artifact_present({"required_artifact_refs": {"monitoring_spec": "doc_m"}, "artifact_name": "monitoring_spec"}) is True
    assert required_artifact_present({"required_artifact_refs": {"monitoring_spec": None}, "artifact_name": "monitoring_spec"}) is False
    assert required_artifact_present({"required_artifact_refs": {}, "artifact_name": "monitoring_spec"}) is False
    assert approval_type_is({"approval_type": "PRODUCTION", "expected_approval_type": "PRODUCTION"}) is True
    assert risk_tier_within_ceiling({"risk_tier_rank": 2, "ceiling_rank": 2}) is True
    assert risk_tier_within_ceiling({"risk_tier_rank": 3, "ceiling_rank": 2}) is False


def test_expiry_and_max_uses_are_deterministic_in_supplied_inputs():
    now = datetime(2026, 6, 27, tzinfo=timezone.utc)
    future = datetime(2026, 12, 31, tzinfo=timezone.utc)
    assert approval_not_expired({"expires_at": None, "as_of": now}) is True
    assert approval_not_expired({"expires_at": future, "as_of": now}) is True
    assert approval_not_expired({"expires_at": now, "as_of": future}) is False
    assert max_uses_not_exceeded({"max_uses": None, "uses_count": 99}) is True
    assert max_uses_not_exceeded({"max_uses": 3, "uses_count": 2}) is True
    assert max_uses_not_exceeded({"max_uses": 3, "uses_count": 3}) is False


def test_predicates_declare_only_the_inputs_they_read():
    assert verification_stamp_satisfies.declared_inputs == ("verification_stamp", "required_stamp")
    for predicate in GOVERNANCE_PREDICATES:
        assert isinstance(predicate.name, str) and predicate.name
        assert isinstance(predicate.declared_inputs, tuple)


def test_register_governance_predicates_registers_all_seven():
    class FakeRegistry:
        def __init__(self):
            self.registered = {}

        def register(self, predicate):
            if predicate.name in self.registered:
                raise AssertionError("re-registration is a load-time error")
            self.registered[predicate.name] = predicate

        def get(self, name):
            return self.registered[name]

        def evaluate(self, guard_expr, inputs):  # pragma: no cover - unused
            raise NotImplementedError

    reg = FakeRegistry()
    register_governance_predicates(reg)
    assert set(reg.registered) == {
        "verification_stamp_satisfies", "approval_type_is", "use_case_not_blocked",
        "required_artifact_present", "risk_tier_within_ceiling", "approval_not_expired",
        "max_uses_not_exceeded",
    }
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_predicates.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.governance.predicates'`.

3. Write minimal implementation:
```python
# src/sp0/governance/predicates.py
from __future__ import annotations

from dataclasses import dataclass

from sp0.contracts import GuardInputs, GuardPredicate, PredicateRegistry
from sp0.governance.attributes import VERIFICATION_STAMPS


@dataclass(frozen=True, slots=True)
class _VerificationStampSatisfies:
    name: str = "verification_stamp_satisfies"
    declared_inputs: tuple[str, ...] = ("verification_stamp", "required_stamp")

    def __call__(self, inputs: GuardInputs) -> bool:
        return VERIFICATION_STAMPS.index(inputs["verification_stamp"]) >= VERIFICATION_STAMPS.index(
            inputs["required_stamp"]
        )


@dataclass(frozen=True, slots=True)
class _ApprovalTypeIs:
    name: str = "approval_type_is"
    declared_inputs: tuple[str, ...] = ("approval_type", "expected_approval_type")

    def __call__(self, inputs: GuardInputs) -> bool:
        return inputs["approval_type"] == inputs["expected_approval_type"]


@dataclass(frozen=True, slots=True)
class _UseCaseNotBlocked:
    name: str = "use_case_not_blocked"
    declared_inputs: tuple[str, ...] = ("target_use_case", "blocked_use_cases")

    def __call__(self, inputs: GuardInputs) -> bool:
        return inputs["target_use_case"] not in tuple(inputs["blocked_use_cases"])


@dataclass(frozen=True, slots=True)
class _RequiredArtifactPresent:
    name: str = "required_artifact_present"
    declared_inputs: tuple[str, ...] = ("required_artifact_refs", "artifact_name")

    def __call__(self, inputs: GuardInputs) -> bool:
        refs = inputs["required_artifact_refs"]
        name = inputs["artifact_name"]
        return name in refs and bool(refs[name])


@dataclass(frozen=True, slots=True)
class _RiskTierWithinCeiling:
    name: str = "risk_tier_within_ceiling"
    declared_inputs: tuple[str, ...] = ("risk_tier_rank", "ceiling_rank")

    def __call__(self, inputs: GuardInputs) -> bool:
        return int(inputs["risk_tier_rank"]) <= int(inputs["ceiling_rank"])


@dataclass(frozen=True, slots=True)
class _ApprovalNotExpired:
    name: str = "approval_not_expired"
    declared_inputs: tuple[str, ...] = ("expires_at", "as_of")

    def __call__(self, inputs: GuardInputs) -> bool:
        expires_at = inputs["expires_at"]
        return expires_at is None or inputs["as_of"] <= expires_at


@dataclass(frozen=True, slots=True)
class _MaxUsesNotExceeded:
    name: str = "max_uses_not_exceeded"
    declared_inputs: tuple[str, ...] = ("max_uses", "uses_count")

    def __call__(self, inputs: GuardInputs) -> bool:
        max_uses = inputs["max_uses"]
        return max_uses is None or int(inputs["uses_count"]) < int(max_uses)


verification_stamp_satisfies = _VerificationStampSatisfies()
approval_type_is = _ApprovalTypeIs()
use_case_not_blocked = _UseCaseNotBlocked()
required_artifact_present = _RequiredArtifactPresent()
risk_tier_within_ceiling = _RiskTierWithinCeiling()
approval_not_expired = _ApprovalNotExpired()
max_uses_not_exceeded = _MaxUsesNotExceeded()

GOVERNANCE_PREDICATES: tuple[GuardPredicate, ...] = (
    verification_stamp_satisfies,
    approval_type_is,
    use_case_not_blocked,
    required_artifact_present,
    risk_tier_within_ceiling,
    approval_not_expired,
    max_uses_not_exceeded,
)


def register_governance_predicates(registry: PredicateRegistry) -> None:
    for predicate in GOVERNANCE_PREDICATES:
        registry.register(predicate)
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_predicates.py -q
```
Expected: `5 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): pure governance guard predicates + registration (§3.8/§4.1)"
```

---

### Task 6 — Body classification + `assert_references_only` (no raw PII inline, §9)

**Files:**
- Create: `src/sp0/privacy/__init__.py`, `src/sp0/privacy/classification.py`
- Test: `tests/sp0/privacy/test_classification.py`

**Interfaces:**
- Consumes: stdlib only.
- Produces: `PII_ERASABLE = "pii-erasable"`, `GOVERNANCE_RETAINED = "governance-retained"`, `BODY_CLASSIFICATIONS`; `validate_classification(classification) -> None`; `assert_references_only(payload, *, sensitive_fields) -> None`; `InlinePIIError`. (Matches the `body_classification`/`classification` CHECK values in the shared `documents`/`blob_index` DDL.)

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/privacy/test_classification.py
import pytest

from sp0.privacy.classification import (
    BODY_CLASSIFICATIONS,
    GOVERNANCE_RETAINED,
    PII_ERASABLE,
    InlinePIIError,
    assert_references_only,
    validate_classification,
)


def test_classification_values_match_ddl():
    assert PII_ERASABLE == "pii-erasable"
    assert GOVERNANCE_RETAINED == "governance-retained"
    assert BODY_CLASSIFICATIONS == ("pii-erasable", "governance-retained")
    validate_classification("pii-erasable")
    with pytest.raises(ValueError):
        validate_classification("public")


def test_references_only_accepts_blob_and_doc_refs():
    assert_references_only(
        {"raw_input_ref": "blob_abc", "confirmed_contract_ref": "doc_xyz"},
        sensitive_fields=("raw_input_ref", "confirmed_contract_ref"),
    )


def test_references_only_rejects_inline_bodies_and_skips_absent_fields():
    with pytest.raises(InlinePIIError):
        assert_references_only(
            {"raw_input_ref": {"text": "SSN 123-45-6789"}}, sensitive_fields=("raw_input_ref",)
        )
    with pytest.raises(InlinePIIError):
        assert_references_only(
            {"raw_input_ref": "the customer's salary is ..."}, sensitive_fields=("raw_input_ref",)
        )
    assert_references_only({}, sensitive_fields=("raw_input_ref",))  # absent => no raise
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/privacy/test_classification.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.privacy.classification'`.

3. Write minimal implementation:
```python
# src/sp0/privacy/__init__.py
```
```python
# src/sp0/privacy/classification.py
from __future__ import annotations

import re
from typing import Mapping

PII_ERASABLE = "pii-erasable"
GOVERNANCE_RETAINED = "governance-retained"
BODY_CLASSIFICATIONS: tuple[str, ...] = (PII_ERASABLE, GOVERNANCE_RETAINED)

_REF_RE = re.compile(r"^(blob|doc)_[A-Za-z0-9]+$")


class InlinePIIError(Exception):
    """Raised when a sensitive field carries an inline body instead of a reference (§9)."""


def validate_classification(classification: str) -> None:
    if classification not in BODY_CLASSIFICATIONS:
        raise ValueError(f"body classification {classification!r} not in {BODY_CLASSIFICATIONS}")


def assert_references_only(payload: Mapping[str, object], *, sensitive_fields: tuple[str, ...]) -> None:
    for name in sensitive_fields:
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str) or not _REF_RE.match(value):
            raise InlinePIIError(
                f"sensitive field {name!r} must be a 'blob_'/'doc_' reference, not inline content (§9)"
            )
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/privacy/test_classification.py -q
```
Expected: `3 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): body classification + no-raw-PII-inline reference guard (§9)"
```

---

### Task 7 — Attempt-memory store (migration + `record_attempt`/`lookup_attempt`/`count_candidates_explored`, §3.9)

**Files:**
- Create: `src/sp0/db/migrations/0810_attempt_memory.sql`, `src/sp0/attempt_memory/__init__.py`, `src/sp0/attempt_memory/store.py`
- Test: `tests/sp0/attempt_memory/test_store.py`

**Interfaces:**
- Consumes: `db` fixture; the shared `attempt_memory` DDL (verbatim — Phase 08 owns this table's creation).
- Produces: `AttemptMemoryEntry` (frozen, slots); `ATTEMPT_DISPOSITIONS`; `record_attempt(conn, *, definition_hash, disposition, score=None, reason=None, request_id=None, feature_id=None) -> None` (upsert by `definition_hash`, refreshes `last_seen`, preserves `first_seen`); `lookup_attempt(conn, definition_hash) -> AttemptMemoryEntry | None`; `count_candidates_explored(conn, *, request_id=None, feature_id=None) -> int` (feeds `provenance.candidates_explored_count`).

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/attempt_memory/test_store.py
import pytest

from sp0.attempt_memory.store import (
    ATTEMPT_DISPOSITIONS,
    AttemptMemoryEntry,
    count_candidates_explored,
    lookup_attempt,
    record_attempt,
)


def test_lookup_missing_returns_none(db):
    assert lookup_attempt(db, "h_absent") is None


def test_record_is_non_pii_exempt_and_upserts_by_hash(db):
    record_attempt(db, definition_hash="h1", disposition="explored", score=0.4, request_id="req_1")
    first = lookup_attempt(db, "h1")
    assert isinstance(first, AttemptMemoryEntry)
    assert first.disposition == "explored"
    assert first.crypto_shred_exempt is True  # §3.9: survives erasure of source bodies

    record_attempt(db, definition_hash="h1", disposition="rejected", reason="leaky", feature_id="feat_1")
    second = lookup_attempt(db, "h1")
    assert second.disposition == "rejected"
    assert second.reason == "leaky"
    assert second.request_id == "req_1"   # earlier value preserved
    assert second.feature_id == "feat_1"  # newly supplied value merged
    seen = db.execute(
        "SELECT first_seen <= last_seen FROM attempt_memory WHERE definition_hash = %s", ("h1",)
    ).fetchone()[0]
    assert seen is True


def test_invalid_disposition_rejected(db):
    assert "explored" in ATTEMPT_DISPOSITIONS
    with pytest.raises(ValueError):
        record_attempt(db, definition_hash="h2", disposition="bogus")


def test_count_candidates_explored_scopes_by_request_and_feature(db):
    record_attempt(db, definition_hash="a", disposition="explored", request_id="req_X")
    record_attempt(db, definition_hash="b", disposition="discarded", request_id="req_X")
    record_attempt(db, definition_hash="c", disposition="explored", request_id="req_Y", feature_id="feat_Z")
    assert count_candidates_explored(db, request_id="req_X") == 2
    assert count_candidates_explored(db, feature_id="feat_Z") == 1
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/attempt_memory/test_store.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.attempt_memory'` (or, once the package exists, a failure that `attempt_memory` table is missing because the migration is absent).

3. Write minimal implementation:
```sql
-- src/sp0/db/migrations/0810_attempt_memory.sql
-- attempt_memory — cross-aggregate dedup/exploration memory (§3.9). Owned by Phase 08.
-- Non-PII by construction; exempt from routine crypto-shred.
CREATE TABLE attempt_memory (
    definition_hash     text        PRIMARY KEY,                  -- content hash; never PII
    score               numeric     NULL,
    disposition         text        NOT NULL
                            CHECK (disposition IN ('explored','discarded','rejected','selected','promoted')),
    reason              text        NULL,
    request_id          text        NULL,
    feature_id          text        NULL,
    crypto_shred_exempt boolean     NOT NULL DEFAULT true,        -- survives erasure of source bodies
    first_seen          timestamptz NOT NULL DEFAULT now(),
    last_seen           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX attempt_memory_feature_idx ON attempt_memory (feature_id) WHERE feature_id IS NOT NULL;
```
```python
# src/sp0/attempt_memory/__init__.py
```
```python
# src/sp0/attempt_memory/store.py
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from sp0.contracts import DbConn

ATTEMPT_DISPOSITIONS: tuple[str, ...] = ("explored", "discarded", "rejected", "selected", "promoted")


@dataclass(frozen=True, slots=True)
class AttemptMemoryEntry:
    definition_hash: str
    disposition: str
    score: Optional[float] = None
    reason: Optional[str] = None
    request_id: Optional[str] = None
    feature_id: Optional[str] = None
    crypto_shred_exempt: bool = True


def record_attempt(
    conn: "DbConn",
    *,
    definition_hash: str,
    disposition: str,
    score: Optional[float] = None,
    reason: Optional[str] = None,
    request_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> None:
    if disposition not in ATTEMPT_DISPOSITIONS:
        raise ValueError(f"disposition {disposition!r} not in {ATTEMPT_DISPOSITIONS}")
    conn.execute(
        """
        INSERT INTO attempt_memory (definition_hash, disposition, score, reason, request_id, feature_id)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (definition_hash) DO UPDATE SET
            disposition = EXCLUDED.disposition,
            score       = COALESCE(EXCLUDED.score, attempt_memory.score),
            reason      = COALESCE(EXCLUDED.reason, attempt_memory.reason),
            request_id  = COALESCE(EXCLUDED.request_id, attempt_memory.request_id),
            feature_id  = COALESCE(EXCLUDED.feature_id, attempt_memory.feature_id),
            last_seen   = now()
        """,
        (definition_hash, disposition, score, reason, request_id, feature_id),
    )


def lookup_attempt(conn: "DbConn", definition_hash: str) -> Optional[AttemptMemoryEntry]:
    row = conn.execute(
        "SELECT definition_hash, disposition, score, reason, request_id, feature_id, crypto_shred_exempt "
        "FROM attempt_memory WHERE definition_hash = %s",
        (definition_hash,),
    ).fetchone()
    if row is None:
        return None
    return AttemptMemoryEntry(
        definition_hash=row[0],
        disposition=row[1],
        score=float(row[2]) if row[2] is not None else None,
        reason=row[3],
        request_id=row[4],
        feature_id=row[5],
        crypto_shred_exempt=bool(row[6]),
    )


def count_candidates_explored(
    conn: "DbConn", *, request_id: Optional[str] = None, feature_id: Optional[str] = None
) -> int:
    if request_id is not None:
        row = conn.execute(
            "SELECT count(*) FROM attempt_memory WHERE request_id = %s", (request_id,)
        ).fetchone()
    elif feature_id is not None:
        row = conn.execute(
            "SELECT count(*) FROM attempt_memory WHERE feature_id = %s", (feature_id,)
        ).fetchone()
    else:
        row = conn.execute("SELECT count(*) FROM attempt_memory").fetchone()
    return int(row[0])
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/attempt_memory/test_store.py -q
```
Expected: `4 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): cross-aggregate attempt-memory store + candidates-explored count (§3.9)"
```

---

### Task 8 — Legal holds (migration + `place_legal_hold`/`release_legal_hold`/`is_under_legal_hold`)

**Files:**
- Create: `src/sp0/db/migrations/0820_legal_holds.sql`, `src/sp0/privacy/legal_hold.py`
- Test: `tests/sp0/privacy/test_legal_hold.py`

**Interfaces:**
- Consumes: `db` fixture; `IdentityEnvelope` (shared contract); `psycopg.types.json.Json`; `dataclasses.asdict`.
- Produces: `legal_holds` table (Phase-08-owned); `place_legal_hold(conn, *, hold_id, scope_kind, scope_ref, reason, placed_by) -> None`; `release_legal_hold(conn, hold_id) -> None`; `is_under_legal_hold(conn, scope_kind, scope_ref) -> bool` (active = `released_at IS NULL`). Backs the §9 legal-hold/open-audit erasure exemption consumed by crypto-shred (Task 9).

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/privacy/test_legal_hold.py
from sp0.contracts import IdentityEnvelope
from sp0.privacy.legal_hold import is_under_legal_hold, place_legal_hold, release_legal_hold

ACTOR = IdentityEnvelope(
    subject="user:legal", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("compliance",),
)


def test_place_then_release_toggles_active_hold(db):
    assert is_under_legal_hold(db, "blob", "blob_h") is False
    place_legal_hold(db, hold_id="hold_1", scope_kind="blob", scope_ref="blob_h",
                     reason="litigation", placed_by=ACTOR)
    assert is_under_legal_hold(db, "blob", "blob_h") is True
    assert is_under_legal_hold(db, "blob", "blob_other") is False
    release_legal_hold(db, "hold_1")
    assert is_under_legal_hold(db, "blob", "blob_h") is False
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/privacy/test_legal_hold.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.privacy.legal_hold'`.

3. Write minimal implementation:
```sql
-- src/sp0/db/migrations/0820_legal_holds.sql
-- legal_holds — §9 legal-hold / open-audit erasure exemption. Phase-08-owned NET-NEW table
-- (not part of the overview shared DDL; nothing else references it).
CREATE TABLE legal_holds (
    hold_id      text        PRIMARY KEY,                         -- 'hold_...'
    scope_kind   text        NOT NULL
                     CHECK (scope_kind IN ('blob','feature','feature_version','request','run','subject')),
    scope_ref    text        NOT NULL,
    reason       text        NOT NULL,
    placed_by    jsonb       NOT NULL,                            -- IdentityEnvelope
    placed_at    timestamptz NOT NULL DEFAULT now(),
    released_at  timestamptz NULL
);
CREATE INDEX legal_holds_active_idx ON legal_holds (scope_kind, scope_ref) WHERE released_at IS NULL;
```
```python
# src/sp0/privacy/legal_hold.py
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

from psycopg.types.json import Json

from sp0.contracts import IdentityEnvelope

if TYPE_CHECKING:
    from sp0.contracts import DbConn


def place_legal_hold(
    conn: "DbConn",
    *,
    hold_id: str,
    scope_kind: str,
    scope_ref: str,
    reason: str,
    placed_by: IdentityEnvelope,
) -> None:
    conn.execute(
        "INSERT INTO legal_holds (hold_id, scope_kind, scope_ref, reason, placed_by) "
        "VALUES (%s, %s, %s, %s, %s)",
        (hold_id, scope_kind, scope_ref, reason, Json(asdict(placed_by))),
    )


def release_legal_hold(conn: "DbConn", hold_id: str) -> None:
    conn.execute("UPDATE legal_holds SET released_at = now() WHERE hold_id = %s", (hold_id,))


def is_under_legal_hold(conn: "DbConn", scope_kind: str, scope_ref: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM legal_holds "
        "WHERE scope_kind = %s AND scope_ref = %s AND released_at IS NULL LIMIT 1",
        (scope_kind, scope_ref),
    ).fetchone()
    return row is not None
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/privacy/test_legal_hold.py -q
```
Expected: `1 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): legal-hold mechanism for erasure exemption (§9)"
```

---

### Task 9 — `KeyManager` Protocol + `crypto_shred` (target pii-erasable; retain governance-of-active-versions & legal-hold; audited; security-stream & attempt-memory exempt, §9)

**Files:**
- Create: `src/sp0/db/migrations/0830_erasure_audit.sql`, `src/sp0/privacy/kms.py`, `src/sp0/privacy/crypto_shred.py`
- Test: `tests/sp0/privacy/test_crypto_shred.py`

**Interfaces:**
- Consumes: `db` fixture; shared `blob_index` DDL (Phase 02/05) and `security_audit` DDL (Phase 07) and `attempt_memory` (Task 7); `is_under_legal_hold` (Task 8); `GOVERNANCE_RETAINED` (Task 6); `IdentityEnvelope` (contract); `psycopg.types.json.Json`.
- Produces: `KeyManager` Protocol (`destroy(kms_key_id)`, `rotate(old_kms_key_id, object_key) -> str`); `GovernanceActiveResolver = Callable[[DbConn, str], bool]` + the fail-closed default `_default_governance_active` (returns `True` ⇒ retain); `ErasureOutcome` (frozen, slots: `blob_id`, `outcome`, `erasure_id`); `crypto_shred(conn, blob_ids, *, reason, requested_by, key_manager, governance_active=_default_governance_active) -> list[ErasureOutcome]`; `erasure_audit` table (records `shredded`/`retained_governance`/`retained_legal_hold`/`not_found`).
- **§9 retention nuance (mechanism only; values/predicate external).** Crypto-shred TARGETS `pii-erasable` bodies. A `governance-retained` body is auto-retained **only while its owning feature_version is active/governed** — decided by the injected `governance_active(conn, blob_id)` hook (the concrete blob→feature_version→`feature_active_versions` mapping and the active/governed predicate are owned by Phase 06 / SP-9/10/12). When the hook reports the owning version is no longer active/governed, that governance-retained body becomes erasable and is shredded like a pii-erasable body (outcome `shredded`). The default resolver retains (fail-closed), so callers that don't wire the hook never over-erase. Legal-held bodies (Task 8) are always exempt. Crypto-shred touches only `blob_index` (destroys per-body key + marks `status='shredded'`); never `security_audit` (regulator retention) or `attempt_memory` (crypto-shred-exempt).

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/privacy/test_crypto_shred.py
from sp0.attempt_memory.store import record_attempt
from sp0.contracts import IdentityEnvelope
from sp0.privacy.crypto_shred import ErasureOutcome, crypto_shred
from sp0.privacy.legal_hold import place_legal_hold

ACTOR = IdentityEnvelope(
    subject="user:dpo", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("privacy",),
)


class FakeKeyManager:
    def __init__(self):
        self.destroyed: set[str] = set()

    def destroy(self, kms_key_id):
        self.destroyed.add(kms_key_id)

    def rotate(self, old_kms_key_id, object_key):  # pragma: no cover - unused here
        return old_kms_key_id + "_v2"


def _blob(db, blob_id, classification, key):
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES (%s, %s, %s, %s, %s, 'live')",
        (blob_id, "k/" + blob_id, "sha256:x", classification, key),
    )


def _status(db, blob_id):
    return db.execute("SELECT status FROM blob_index WHERE blob_id = %s", (blob_id,)).fetchone()[0]


def test_crypto_shred_targets_pii_erasable_and_retains_the_rest(db):
    _blob(db, "blob_p", "pii-erasable", "k1")
    _blob(db, "blob_g", "governance-retained", "k2")
    _blob(db, "blob_h", "pii-erasable", "k3")
    place_legal_hold(db, hold_id="hold_h", scope_kind="blob", scope_ref="blob_h",
                     reason="audit", placed_by=ACTOR)
    record_attempt(db, definition_hash="keep_me", disposition="rejected", feature_id="feat_1")
    db.execute(
        "INSERT INTO security_audit (security_event_id, event_type, actor, attempted_action, decision, entry_hash) "
        "VALUES ('sec_keep', 'COMMAND_DENIED', '{}'::jsonb, 'create_run', 'denied', 'h0')"
    )

    km = FakeKeyManager()
    outcomes = crypto_shred(
        db, ["blob_p", "blob_g", "blob_h", "blob_missing"],
        reason="gdpr erasure", requested_by=ACTOR, key_manager=km,
    )
    by_id = {o.blob_id: o.outcome for o in outcomes}
    assert isinstance(outcomes[0], ErasureOutcome)
    assert by_id == {
        "blob_p": "shredded",
        "blob_g": "retained_governance",
        "blob_h": "retained_legal_hold",
        "blob_missing": "not_found",
    }
    assert km.destroyed == {"k1"}
    assert _status(db, "blob_p") == "shredded"
    assert _status(db, "blob_g") == "live"
    assert _status(db, "blob_h") == "live"

    # audited: one erasure_audit row per blob, with the outcome recorded
    assert db.execute("SELECT count(*) FROM erasure_audit").fetchone()[0] == 4
    # security stream + attempt-memory are exempt and untouched
    assert db.execute("SELECT count(*) FROM security_audit WHERE security_event_id='sec_keep'").fetchone()[0] == 1
    assert db.execute("SELECT count(*) FROM attempt_memory WHERE definition_hash='keep_me'").fetchone()[0] == 1


def test_governance_retained_body_of_ungoverned_version_is_erasable(db):
    # §9: retention is driven by the OWNING VERSION's governance status, not classification alone.
    # A governance-retained body whose feature_version is no longer active/governed becomes erasable.
    _blob(db, "blob_old_gov", "governance-retained", "k9")
    km = FakeKeyManager()
    outcomes = crypto_shred(
        db, ["blob_old_gov"],
        reason="owning version deprecated + erasure request", requested_by=ACTOR, key_manager=km,
        governance_active=lambda conn, blob_id: False,  # owning version no longer active/governed
    )
    assert outcomes[0].outcome == "shredded"
    assert km.destroyed == {"k9"}
    assert _status(db, "blob_old_gov") == "shredded"
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/privacy/test_crypto_shred.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.privacy.crypto_shred'`.

3. Write minimal implementation:
```sql
-- src/sp0/db/migrations/0830_erasure_audit.sql
-- erasure_audit — audited crypto-shred trail (§9). Phase-08-owned NET-NEW table (not part of the
-- overview shared DDL). Records erasures AND retentions.
CREATE TABLE erasure_audit (
    erasure_id     text        PRIMARY KEY,                       -- 'era_...'
    blob_id        text        NOT NULL,
    classification text        NULL,
    kms_key_id     text        NULL,
    reason         text        NOT NULL,
    requested_by   jsonb       NOT NULL,                          -- IdentityEnvelope
    outcome        text        NOT NULL
                       CHECK (outcome IN ('shredded','retained_governance','retained_legal_hold','not_found')),
    performed_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX erasure_audit_blob_idx ON erasure_audit (blob_id);
```
```python
# src/sp0/privacy/kms.py
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class KeyManager(Protocol):
    """Per-body KMS abstraction (§9). Destroying a key crypto-shreds its body; rotate re-wraps."""

    def destroy(self, kms_key_id: str) -> None: ...

    def rotate(self, old_kms_key_id: str, object_key: str) -> str:
        """Re-encrypt the body under a fresh key; return the new kms_key_id."""
```
```python
# src/sp0/privacy/crypto_shred.py
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Callable, Iterable, Optional

from psycopg.types.json import Json

from sp0.contracts import IdentityEnvelope
from sp0.privacy.classification import GOVERNANCE_RETAINED
from sp0.privacy.kms import KeyManager
from sp0.privacy.legal_hold import is_under_legal_hold

if TYPE_CHECKING:
    from sp0.contracts import DbConn

# Resolves whether a blob's owning feature_version is currently active/governed (=> retain).
# The concrete blob->feature_version->feature_active_versions mapping + active/governed predicate
# are policy/runtime, owned by Phase 06 / SP-9/10/12; SP-0 only defines the hook (§9).
GovernanceActiveResolver = Callable[["DbConn", str], bool]


def _default_governance_active(conn: "DbConn", blob_id: str) -> bool:
    """Fail-closed default: treat a governance-retained body as belonging to an active/governed
    version (retain). Callers wire a resolver that consults `feature_active_versions` to allow
    erasure of governance-retained bodies whose owning version is no longer active/governed (§9)."""
    return True


@dataclass(frozen=True, slots=True)
class ErasureOutcome:
    blob_id: str
    outcome: str                                   # shredded | retained_governance | retained_legal_hold | not_found
    erasure_id: str


def _record(
    conn: "DbConn",
    *,
    blob_id: str,
    classification: Optional[str],
    kms_key_id: Optional[str],
    reason: str,
    requested_by: IdentityEnvelope,
    outcome: str,
) -> ErasureOutcome:
    erasure_id = "era_" + uuid.uuid4().hex
    conn.execute(
        "INSERT INTO erasure_audit "
        "(erasure_id, blob_id, classification, kms_key_id, reason, requested_by, outcome) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
        (erasure_id, blob_id, classification, kms_key_id, reason, Json(asdict(requested_by)), outcome),
    )
    return ErasureOutcome(blob_id=blob_id, outcome=outcome, erasure_id=erasure_id)


def crypto_shred(
    conn: "DbConn",
    blob_ids: Iterable[str],
    *,
    reason: str,
    requested_by: IdentityEnvelope,
    key_manager: KeyManager,
    governance_active: GovernanceActiveResolver = _default_governance_active,
) -> list[ErasureOutcome]:
    """Crypto-shred pii-erasable bodies (§9): destroy the per-body key + mark status='shredded'.
    A governance-retained body is auto-retained ONLY while its owning feature_version is
    active/governed (decided by `governance_active(conn, blob_id)`); once the owning version is no
    longer active/governed it becomes erasable and is shredded. Legal-held bodies are always exempt.
    Operates ONLY over blob_index — the security stream and attempt-memory are exempt."""
    outcomes: list[ErasureOutcome] = []
    for blob_id in blob_ids:
        row = conn.execute(
            "SELECT classification, kms_key_id FROM blob_index WHERE blob_id = %s", (blob_id,)
        ).fetchone()
        if row is None:
            outcomes.append(_record(conn, blob_id=blob_id, classification=None, kms_key_id=None,
                                    reason=reason, requested_by=requested_by, outcome="not_found"))
            continue
        classification, kms_key_id = row
        if classification == GOVERNANCE_RETAINED and governance_active(conn, blob_id):
            outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                    reason=reason, requested_by=requested_by, outcome="retained_governance"))
            continue
        if is_under_legal_hold(conn, "blob", blob_id):
            outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                    reason=reason, requested_by=requested_by, outcome="retained_legal_hold"))
            continue
        # pii-erasable, OR governance-retained whose owning version is no longer active/governed.
        if kms_key_id is not None:
            key_manager.destroy(kms_key_id)
        conn.execute("UPDATE blob_index SET status = 'shredded', swept_at = now() WHERE blob_id = %s", (blob_id,))
        outcomes.append(_record(conn, blob_id=blob_id, classification=classification, kms_key_id=kms_key_id,
                                reason=reason, requested_by=requested_by, outcome="shredded"))
    return outcomes
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/privacy/test_crypto_shred.py -q
```
Expected: `2 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): crypto-shred targeting pii-erasable + governance-of-active-version/legal-hold exemption + audit (§9)"
```

---

### Task 10 — `rotate_blob_key` (key rotation without rewriting events, §9)

**Files:**
- Modify: `src/sp0/privacy/crypto_shred.py`
- Test: `tests/sp0/privacy/test_key_rotation.py`

**Interfaces:**
- Consumes: `db` fixture; shared `blob_index` DDL; shared `events` DDL + `global_seq_seq` (Phase 01); `KeyManager` (Task 9).
- Produces: `rotate_blob_key(conn, blob_id, *, key_manager) -> str` (re-wraps body, updates `blob_index.kms_key_id`, returns new key, leaves `events` untouched); `BlobNotFoundError`.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/privacy/test_key_rotation.py
import pytest

from sp0.privacy.crypto_shred import BlobNotFoundError, rotate_blob_key


class FakeKeyManager:
    def __init__(self):
        self.rotated: list[tuple[str, str]] = []

    def destroy(self, kms_key_id):  # pragma: no cover - unused here
        pass

    def rotate(self, old_kms_key_id, object_key):
        self.rotated.append((old_kms_key_id, object_key))
        return old_kms_key_id + "_v2"


def _seed_event(db):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES ('evt_1','run','run_1',1,'run_1','RUN_OPENED',1,1,"
        "'{\"subject\":\"s\"}'::jsonb, '{}'::jsonb, '{}'::jsonb, now())"
    )


def test_rotate_updates_key_and_leaves_events_untouched(db):
    _seed_event(db)
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES ('blob_r', 'k/blob_r', 'sha256:x', 'pii-erasable', 'k1', 'live')"
    )
    before = db.execute("SELECT count(*), max(event_id) FROM events").fetchone()

    km = FakeKeyManager()
    new_key = rotate_blob_key(db, "blob_r", key_manager=km)

    assert new_key == "k1_v2"
    assert km.rotated == [("k1", "k/blob_r")]
    assert db.execute("SELECT kms_key_id FROM blob_index WHERE blob_id='blob_r'").fetchone()[0] == "k1_v2"
    after = db.execute("SELECT count(*), max(event_id) FROM events").fetchone()
    assert after == before  # events never rewritten (§9)

    with pytest.raises(BlobNotFoundError):
        rotate_blob_key(db, "blob_absent", key_manager=km)
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/privacy/test_key_rotation.py -q
```
Expected: `ImportError: cannot import name 'BlobNotFoundError' from 'sp0.privacy.crypto_shred'`.

3. Write minimal implementation (append to `crypto_shred.py`):
```python
# src/sp0/privacy/crypto_shred.py  (append)
class BlobNotFoundError(Exception):
    """Raised when a referenced blob_id is absent from blob_index."""


def rotate_blob_key(conn: "DbConn", blob_id: str, *, key_manager: KeyManager) -> str:
    """Rotate a body's per-body KMS key WITHOUT rewriting any events (§9)."""
    row = conn.execute(
        "SELECT object_key, kms_key_id FROM blob_index WHERE blob_id = %s", (blob_id,)
    ).fetchone()
    if row is None:
        raise BlobNotFoundError(blob_id)
    object_key, old_key = row
    new_key = key_manager.rotate(old_key, object_key)
    conn.execute("UPDATE blob_index SET kms_key_id = %s WHERE blob_id = %s", (new_key, blob_id))
    return new_key
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/privacy/test_key_rotation.py -q
```
Expected: `1 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): per-body key rotation without rewriting events (§9)"
```

---

### Task 11 — `replay_run` (labeled full vs privacy-degraded replay, §8)

**Files:**
- Create: `src/sp0/governance/replay.py`
- Test: `tests/sp0/governance/test_replay.py`

**Interfaces:**
- Consumes: `load_stream(conn, aggregate, aggregate_id, *, upto_seq=None, expected=None)` (shared contract; imported from `sp0.events`); shared `documents` + `blob_index` DDL; `EventEnvelope` (contract).
- Produces: `ReplayMode` (`FULL`/`PRIVACY_DEGRADED`); `ArtifactReplayStatus` (frozen, slots: `doc_id`, `stage`, `intact`, `degraded_reason`); `ReplayResult` (frozen, slots: `run_id`, `mode`, `events`, `artifacts`, `degraded_artifacts`); `replay_run(conn, run_id, *, upto_seq=None, expected=None) -> ReplayResult`. Labels mode = `PRIVACY_DEGRADED` iff any committed doc's body is `shredded`; metadata-only docs (`body_ref IS NULL`) and `governance-retained` (never shredded) bodies stay intact.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/governance/test_replay.py
from sp0.governance.replay import ArtifactReplayStatus, ReplayMode, replay_run


def _seed_event(db, run_id):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES (%s,'run',%s,1,%s,'RUN_OPENED',1,1,"
        "'{\"subject\":\"s\",\"actor_kind\":\"service\",\"authenticated\":true,"
        "\"auth_method\":\"workload-identity\",\"role_claims\":[]}'::jsonb, '{}'::jsonb, "
        "'{\"artifact_type\":\"DRAFT_CONTRACT\",\"schema_version\":1,\"producing_component\":\"sp0@1\"}'::jsonb, now())",
        ("evt_" + run_id, run_id, run_id),
    )


def _blob(db, blob_id, classification, status):
    db.execute(
        "INSERT INTO blob_index (blob_id, object_key, content_hash, classification, kms_key_id, status) "
        "VALUES (%s, %s, 'sha256:x', %s, 'k', %s)",
        (blob_id, "k/" + blob_id, classification, status),
    )


def _doc(db, doc_id, run_id, stage, body_ref):
    db.execute(
        "INSERT INTO documents (doc_id, run_id, stage, schema_version, branch_role, content_hash, "
        "body_classification, actor, provenance, body_ref) "
        "VALUES (%s, %s, %s, 1, 'primary', 'sha256:x', %s, '{}'::jsonb, '{}'::jsonb, %s)",
        (doc_id, run_id, stage, "pii-erasable" if "p" in doc_id else "governance-retained", body_ref),
    )


def test_full_replay_when_all_bodies_intact(db):
    _seed_event(db, "run_full")
    _blob(db, "blob_ok", "pii-erasable", "live")
    _doc(db, "doc_p_ok", "run_full", "DRAFT_CONTRACT", "blob_ok")
    _doc(db, "doc_meta", "run_full", "ASSUMPTION_LEDGER", None)  # metadata-only, no body

    result = replay_run(db, "run_full")
    assert result.mode is ReplayMode.FULL
    assert result.degraded_artifacts == ()
    assert len(result.events) == 1
    assert all(isinstance(a, ArtifactReplayStatus) and a.intact for a in result.artifacts)


def test_privacy_degraded_replay_labels_shredded_artifacts(db):
    _seed_event(db, "run_deg")
    _blob(db, "blob_shred", "pii-erasable", "shredded")
    _blob(db, "blob_gov", "governance-retained", "live")
    _doc(db, "doc_p_shred", "run_deg", "DRAFT_CONTRACT", "blob_shred")
    _doc(db, "doc_g_keep", "run_deg", "CONFIRMED_CONTRACT", "blob_gov")

    result = replay_run(db, "run_deg")
    assert result.mode is ReplayMode.PRIVACY_DEGRADED
    assert result.degraded_artifacts == ("doc_p_shred",)
    degraded = {a.doc_id: a for a in result.artifacts}
    assert degraded["doc_p_shred"].intact is False
    assert "shred" in (degraded["doc_p_shred"].degraded_reason or "")
    assert degraded["doc_g_keep"].intact is True
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/governance/test_replay.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.governance.replay'`.

3. Write minimal implementation:
```python
# src/sp0/governance/replay.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Mapping, Optional

from sp0.contracts import EventEnvelope
from sp0.events import load_stream

if TYPE_CHECKING:
    from sp0.contracts import DbConn


class ReplayMode(str, Enum):
    FULL = "full"
    PRIVACY_DEGRADED = "privacy-degraded"


@dataclass(frozen=True, slots=True)
class ArtifactReplayStatus:
    doc_id: str
    stage: str
    intact: bool
    degraded_reason: Optional[str] = None


@dataclass(frozen=True, slots=True)
class ReplayResult:
    run_id: str
    mode: ReplayMode
    events: tuple[EventEnvelope, ...]
    artifacts: tuple[ArtifactReplayStatus, ...]
    degraded_artifacts: tuple[str, ...]


def replay_run(
    conn: "DbConn",
    run_id: str,
    *,
    upto_seq: Optional[int] = None,
    expected: Optional[Mapping[str, int]] = None,
) -> ReplayResult:
    """Reconstruct a run's decision trail and label it full vs privacy-degraded (§8). The event
    skeleton + provenance are always reconstructable; a body whose blob is crypto-shredded makes
    that artifact (and the whole replay) privacy-degraded."""
    events = tuple(load_stream(conn, "run", run_id, upto_seq=upto_seq, expected=expected))

    if upto_seq is None:
        rows = conn.execute(
            "SELECT doc_id, stage, body_ref FROM documents WHERE run_id = %s ORDER BY global_seq",
            (run_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT doc_id, stage, body_ref FROM documents "
            "WHERE run_id = %s AND global_seq <= %s ORDER BY global_seq",
            (run_id, upto_seq),
        ).fetchall()

    artifacts: list[ArtifactReplayStatus] = []
    degraded: list[str] = []
    for doc_id, stage, body_ref in rows:
        if body_ref is None:
            artifacts.append(ArtifactReplayStatus(doc_id=doc_id, stage=stage, intact=True))
            continue
        status_row = conn.execute(
            "SELECT status FROM blob_index WHERE blob_id = %s", (body_ref,)
        ).fetchone()
        status = status_row[0] if status_row is not None else "shredded"
        if status == "shredded":
            artifacts.append(ArtifactReplayStatus(
                doc_id=doc_id, stage=stage, intact=False, degraded_reason="body crypto-shredded"))
            degraded.append(doc_id)
        else:
            artifacts.append(ArtifactReplayStatus(doc_id=doc_id, stage=stage, intact=True))

    mode = ReplayMode.PRIVACY_DEGRADED if degraded else ReplayMode.FULL
    return ReplayResult(
        run_id=run_id,
        mode=mode,
        events=events,
        artifacts=tuple(artifacts),
        degraded_artifacts=tuple(degraded),
    )
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/governance/test_replay.py -q
```
Expected: `2 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): labeled full vs privacy-degraded replay (§8)"
```

---

### Task 12 — `read_audit` (authorized + logged audit read, §9/§6.2)

**Files:**
- Create: `src/sp0/privacy/audit_read.py`
- Test: `tests/sp0/privacy/test_audit_read.py`

**Interfaces:**
- Consumes: `replay_run` (Task 11); `Command` + `IdentityEnvelope` + `EventEnvelope` (contract); Phase 07's `AuditReadDenied` from `sp0.security.audit` — re-exported, NOT redefined (single shared exception class; the overview forbids divergent same-named definitions across phases); two injected Phase-07 callables (dependency-injected to avoid a hard `src/` import cycle, but matched to Phase 07's REAL signatures):
  - `authorize_command(conn, cmd: Command) -> AuthzDecision` — the ONLY Phase-07 authz entrypoint (there is no `is_authorized`). `read_audit` builds a synthetic `Command(action="read_audit", aggregate="run", aggregate_id=run_id, …)` and checks `decision.allowed`/`decision.reason`.
  - `record_security_event(conn, *, event_type: str, actor: IdentityEnvelope, attempted_action: str, decision: str, reason: str | None = None, aggregate: str | None = None, aggregate_id: str | None = None) -> str` (Phase 07 owns the `security_audit` tamper-evident chain; `decision` ∈ the DDL CHECK `denied`/`allowed_break_glass`/`flagged`).
- Produces: `AuditView` (frozen, slots: `run_id`, `events`, `mode`, `degraded_artifacts`); `read_audit(conn, *, run_id, actor, authorize_command, record_security_event, upto_seq=None) -> AuditView`; re-exports `AuditReadDenied`. Action vocabulary used: `"read_audit"` (the canonical §6.2 action — matches Phase 07's `authz_policy` seed rows for `auditor`/`compliance`/`owner`; an authorizer keyed on the action would deny anything else). Every read writes an `AUDIT_READ` security entry (`flagged` on allow, `denied` on deny); a denied read raises `AuditReadDenied` and never returns data.

**TDD steps:**

1. Write the failing test:
```python
# tests/sp0/privacy/test_audit_read.py
import pytest

from sp0.contracts import IdentityEnvelope
from sp0.governance.replay import ReplayMode
from sp0.privacy.audit_read import AuditReadDenied, AuditView, read_audit

ACTOR = IdentityEnvelope(
    subject="user:auditor", actor_kind="human", authenticated=True,
    auth_method="oidc", role_claims=("auditor",),
)


def _seed_event(db, run_id):
    db.execute(
        "INSERT INTO events (event_id, aggregate, aggregate_id, stream_version, run_id, type, "
        "schema_version, table_version, actor, payload, provenance, occurred_at) "
        "VALUES (%s,'run',%s,1,%s,'RUN_OPENED',1,1,"
        "'{\"subject\":\"s\",\"actor_kind\":\"service\",\"authenticated\":true,"
        "\"auth_method\":\"workload-identity\",\"role_claims\":[]}'::jsonb, '{}'::jsonb, "
        "'{\"artifact_type\":\"DRAFT_CONTRACT\",\"schema_version\":1,\"producing_component\":\"sp0@1\"}'::jsonb, now())",
        ("evt_" + run_id, run_id, run_id),
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, conn, *, event_type, actor, attempted_action, decision,
                 reason=None, aggregate=None, aggregate_id=None):
        self.calls.append((event_type, decision, attempted_action))
        return "sec_" + str(len(self.calls))


class _Decision:
    # Duck-types Phase 07's AuthzDecision(allowed, reason); read_audit reads only these.
    def __init__(self, allowed, reason=None):
        self.allowed = allowed
        self.reason = reason


def _allow(conn, cmd):
    assert cmd.action == "read_audit"   # canonical §6.2 action wired through to the authorizer
    return _Decision(True)


def _deny(conn, cmd):
    assert cmd.action == "read_audit"
    return _Decision(False, "no matching authz policy")


def test_authorized_read_returns_labeled_view_and_logs_audit_read(db):
    _seed_event(db, "run_a")
    rec = _Recorder()
    view = read_audit(
        db, run_id="run_a", actor=ACTOR,
        authorize_command=_allow,
        record_security_event=rec,
    )
    assert isinstance(view, AuditView)
    assert view.run_id == "run_a"
    assert view.mode is ReplayMode.FULL
    assert len(view.events) == 1
    assert rec.calls == [("AUDIT_READ", "flagged", "read_audit")]


def test_denied_read_logs_denial_and_raises_without_returning_data(db):
    _seed_event(db, "run_b")
    rec = _Recorder()
    with pytest.raises(AuditReadDenied):
        read_audit(
            db, run_id="run_b", actor=ACTOR,
            authorize_command=_deny,
            record_security_event=rec,
        )
    assert rec.calls == [("AUDIT_READ", "denied", "read_audit")]
```

2. Run it, expect FAIL:
```
python -m pytest tests/sp0/privacy/test_audit_read.py -q
```
Expected: `ModuleNotFoundError: No module named 'sp0.privacy.audit_read'`.

3. Write minimal implementation:
```python
# src/sp0/privacy/audit_read.py
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from sp0.contracts import Command, EventEnvelope, IdentityEnvelope
from sp0.governance.replay import ReplayMode, replay_run
from sp0.security.audit import AuditReadDenied  # single shared exception class (Phase 07 authoritative)

if TYPE_CHECKING:
    from sp0.contracts import DbConn

__all__ = ["AuditView", "AuditReadDenied", "read_audit"]

_ACTION = "read_audit"  # canonical §6.2 action vocabulary (matches Phase 07's authz_policy seed)

# authorize_command(conn, cmd) -> AuthzDecision (.allowed/.reason); record_security_event(...) -> str
AuthorizeCommand = Callable[..., object]
RecordSecurityEvent = Callable[..., str]


@dataclass(frozen=True, slots=True)
class AuditView:
    run_id: str
    events: tuple[EventEnvelope, ...]
    mode: ReplayMode
    degraded_artifacts: tuple[str, ...]


def read_audit(
    conn: "DbConn",
    *,
    run_id: str,
    actor: IdentityEnvelope,
    authorize_command: AuthorizeCommand,
    record_security_event: RecordSecurityEvent,
    upto_seq: Optional[int] = None,
) -> AuditView:
    """Authorized-and-logged audit read (§9/§6.2). Authorization is delegated to Phase 07's
    `authorize_command(conn, cmd) -> AuthzDecision` over a synthetic `read_audit` Command; every
    read (allow or deny) is recorded to the security stream via Phase 07's `record_security_event`.
    On deny: log AUDIT_READ/denied and raise `AuditReadDenied`. On allow: log AUDIT_READ/flagged
    and return the (privacy-degraded-labeled) reconstruction."""
    cmd = Command(
        action=_ACTION,
        aggregate="run",
        aggregate_id=run_id,
        args={},
        actor=actor,
        idempotency_key="audit_read:" + run_id + ":" + uuid.uuid4().hex,
    )
    decision = authorize_command(conn, cmd)
    if not decision.allowed:
        record_security_event(
            conn, event_type="AUDIT_READ", actor=actor, attempted_action=_ACTION,
            decision="denied", aggregate="run", aggregate_id=run_id,
            reason=getattr(decision, "reason", None) or "unauthorized audit read",
        )
        raise AuditReadDenied(f"actor {actor.subject!r} may not read audit for run {run_id!r}")

    record_security_event(
        conn, event_type="AUDIT_READ", actor=actor, attempted_action=_ACTION,
        decision="flagged", aggregate="run", aggregate_id=run_id, reason="audit read",
    )
    result = replay_run(conn, run_id, upto_seq=upto_seq)
    return AuditView(
        run_id=run_id,
        events=result.events,
        mode=result.mode,
        degraded_artifacts=result.degraded_artifacts,
    )
```

4. Run tests, expect PASS:
```
python -m pytest tests/sp0/privacy/test_audit_read.py -q
```
Expected: `2 passed`.

5. Commit:
```
git add -A && git commit -m "feat(sp0-08): authorized + logged audit read with degraded labeling (§9/§6.2)"
```

---

### Phase 08 closeout

Run the whole phase suite green before handing off:
```
python -m pytest tests/sp0/governance tests/sp0/privacy tests/sp0/attempt_memory -q
```
Expected: all phase tests pass. This phase delivers the §3.8/§3.9/§8/§9 *mechanism* only — verification-threshold values, risk-tier ordering/ceilings, use-case permission matrices, the active/governed predicate behind crypto-shred retention, retention cadences, and the concrete KMS/blob/security-chain bindings remain owned by SP-9/SP-10/SP-12 and the runtime phases. Cross-phase wiring points: the injected Phase-07 callables (`authorize_command(conn, cmd) -> AuthzDecision`, `record_security_event`), the re-exported `AuditReadDenied` (`sp0.security.audit`), the `ProvenanceEnvelope` single-source re-export (Task 1), the `governance_active` resolver hook (Task 9), and the `load_stream` import path (`sp0.events`). The Phase-08-owned `db` fixture (Task 0) provisions the shared tables verbatim plus Phase 08's `08*.sql` migrations, so the suite is self-contained; confirm the final Phase-01/07 names at integration and reconcile via the overview if they differ.
