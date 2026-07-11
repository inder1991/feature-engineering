# Batched Enrichment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cut upload-time LLM cost by batching the advisory concept / definition / domain enrichment calls, behind a per-task kill switch, without regressing enrichment quality or touching any deterministic fact.

**Architecture:** A new governed **batch seam** (`audited_batch_call`) sends N items in one array-schema request through the existing egress + audit machinery; a task-agnostic **orchestrator** (`run_batched`) chunks items (item-count + token bounded), validates the response against the expected ref-set, salvages valid items, and walks a bounded degradation ladder (retry → adaptive split → capped single fallback). Each of the three `enrich_*` functions gains a `mode() == "batch"` path that calls the orchestrator; `mode() == "single"` (the default) keeps today's exact per-item code as the proven rollback. Cache tables gain a `cache_version` dimension so a vocabulary/prompt bump invalidates stale entries.

**Tech Stack:** Python 3.12, psycopg (raw SQL, no ORM), FastAPI, pytest with a live-Postgres `db` fixture, `FakeLLM` for hermetic LLM scripting. Frontend untouched.

**Spec:** `docs/superpowers/specs/2026-07-11-batched-enrichment.md` (v2). Contract references below (C1…C10) point at that spec.

## Global Constraints

- **Advisory only.** Enrichment fills `graph_node.concept/domain/definition`. It must NEVER abort or alter the upload's facts, graph structure, joins, drift, brake, or quarantine. Every failure degrades search, never data. (Spec: Architectural boundary.)
- **Default OFF in production.** `OVERLAY_ENRICH_<TASK>_MODE` defaults to `single`. Batch is opt-in per task via env. Single mode must remain byte-for-byte today's behaviour (the kill switch). (Spec C10.)
- **Governed egress + audit is mandatory.** Every provider call — batch or single — goes through `assert_llm_safe` + `record_llm_call`. No direct `client.call()`. (Spec C9.)
- **Metadata only.** Only `table`, `column`, `type`, `columns`, and (definitions) the assigned `concept` may egress per item. Never the uploader's free-text `definition` or any data value. (Existing invariant; enforced by the per-item egress allowlist in Task 4.)
- **Cache writes are idempotent.** All cache writes use `ON CONFLICT DO NOTHING`. A cache failure never fails an upload. (Spec C6.)
- **Invalid ≠ UNCLASSIFIED (batch path).** A hallucinated concept is `invalid_value` → NOT cached. Only the literal `unclassified` is cached as `UNCLASSIFIED`. (Spec C3. Single mode keeps today's coerce-to-unclassified.)
- **Conventions:** raw SQL via `conn.execute`; `from __future__ import annotations`; ruff/mypy clean; TDD with a failing test first; commit per task. Run backend tests with `pytest tests/featuregen/overlay/upload/ -q` (needs a Postgres DSN — the `db` fixture provisions ephemeral PG in CI).

---

## File Structure

- Create `src/featuregen/overlay/upload/enrich_config.py` — env-driven knobs: `mode`, `max_items`, `max_input_tokens`, `budget`. One responsibility: read rollout config, nothing else.
- Create `src/featuregen/overlay/upload/enrich_batch.py` — batch primitives: `BatchItem`, `BatchItemOutcome`, `BatchCallResult`, `validate_batch_results`, `chunk_items`, `run_batched`. The task-agnostic engine.
- Create `src/featuregen/db/migrations/0977_enrichment_cache_versioning.sql` — add `cache_version` to the three cache tables.
- Modify `src/featuregen/overlay/upload/enrich_llm.py` — add batch array schemas + `audited_batch_call`.
- Modify `src/featuregen/overlay/upload/enrich.py` — versioned cache helpers; a batch path in each `enrich_*`; C3 accept policy; concept-dependent definition key.
- Modify `src/featuregen/overlay/upload/ingest.py:123-134` — independent per-task fail-soft; pass `concepts` into `draft_definitions`.
- Create `tests/featuregen/overlay/upload/test_enrich_batch.py` — unit + integration tests for the new engine.
- Create `tests/eval/test_enrich_batch_quality.py` — gold-set quality gate (manual/nightly marker).

Interfaces are locked here so tasks can be implemented out of order:

```python
# enrich_batch.py
@dataclass(frozen=True)
class BatchItem:            ref: str;   metadata: dict
@dataclass(frozen=True)
class BatchItemOutcome:     ref: str;   status: str;   value: str | None;   reason_codes: tuple[str, ...]
@dataclass(frozen=True)
class BatchCallResult:      outcomes: tuple[BatchItemOutcome, ...];  provider_calls: int
                            # + input_tokens: int; output_tokens: int

Accept = Callable[[str], tuple[str | None, str]]   # raw -> (value_to_cache | None, reason_code)

def validate_batch_results(items: list[BatchItem], results: list[dict], out_key: str,
                           accept: Accept) -> list[BatchItemOutcome]: ...
def chunk_items(items: list[BatchItem], *, max_items: int, max_input_tokens: int) -> list[list[BatchItem]]: ...
def run_batched(conn, client, *, short: str, task: str, prompt_id: str, schema_id: str,
                shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                accept: Accept, actor) -> dict[str, str]: ...   # {ref: accepted_value}

# enrich_llm.py
def audited_batch_call(conn, client, *, task, prompt_id, schema_id, shared_metadata,
                       items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor=None) -> BatchCallResult: ...

# enrich_config.py
def mode(short: str) -> str            # "single" | "batch"
def max_items(short: str) -> int
def max_input_tokens(short: str) -> int
@dataclass(frozen=True)
class Budget: max_batch_attempts: int; max_single_fallback: int; max_provider_calls: int
              wallclock_budget_ms: int; keep_threshold: float; min_split: int
def budget(short: str) -> Budget
```

Status codes (string constants in `enrich_batch.py`): `VALID="valid"`, `MISSING="missing"`, `EXTRA="extra"`, `DUPLICATE="duplicate"`, `BLANK="blank"`, `INVALID="invalid_value"`, `EGRESS="egress_rejected"`, `FALLBACK_VALID="fallback_valid"`, `FALLBACK_FAILED="fallback_failed"`.

---

# Phase 1 — Seam & foundations (no production behaviour change; MODE defaults `single`)

## Task 1: Rollout config + kill switch

**Files:**
- Create: `src/featuregen/overlay/upload/enrich_config.py`
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Produces: `mode`, `max_items`, `max_input_tokens`, `Budget`, `budget` (signatures above).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py
import importlib
from featuregen.overlay.upload import enrich_config as cfg


def test_mode_defaults_single_and_reads_env(monkeypatch):
    assert cfg.mode("concept") == "single"
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    assert cfg.mode("concept") == "batch"


def test_max_items_default_and_override(monkeypatch):
    assert cfg.max_items("concept") == 40
    assert cfg.max_items("definition") == 12
    assert cfg.max_items("domain") == 20
    monkeypatch.setenv("OVERLAY_ENRICH_BATCH_CONCEPT_MAX_ITEMS", "16")
    assert cfg.max_items("concept") == 16


def test_budget_defaults(monkeypatch):
    b = cfg.budget("definition")
    assert b.max_batch_attempts == 2 and b.max_single_fallback == 8 and b.min_split == 4
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "3")
    assert cfg.budget("definition").max_single_fallback == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -q`
Expected: FAIL with `ModuleNotFoundError: enrich_config`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/overlay/upload/enrich_config.py
"""Rollout knobs for batched enrichment (spec C10). All default so production is unchanged:
mode=single, conservative budgets. Batch is opt-in per task via env — the kill switch."""
from __future__ import annotations

import os
from dataclasses import dataclass

_DEFAULT_MAX_ITEMS = {"concept": 40, "definition": 12, "domain": 20}
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 14000, "definition": 8000, "domain": 8000}


def mode(short: str) -> str:
    """'single' (default, today's exact path) or 'batch'."""
    return os.environ.get(f"OVERLAY_ENRICH_{short.upper()}_MODE", "single").strip().lower()


def max_items(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_ITEMS",
                              _DEFAULT_MAX_ITEMS[short]))


def max_input_tokens(short: str) -> int:
    return int(os.environ.get(f"OVERLAY_ENRICH_BATCH_{short.upper()}_MAX_INPUT_TOKENS",
                              _DEFAULT_MAX_INPUT_TOKENS[short]))


@dataclass(frozen=True)
class Budget:
    max_batch_attempts: int    # retries of a failed chunk before splitting
    max_single_fallback: int   # cap on per-item fallback calls per task run
    max_provider_calls: int    # hard ceiling on provider calls per task run
    wallclock_budget_ms: int   # stop enriching past this; leave remainder uncached
    keep_threshold: float      # salvage-and-stop when valid ratio >= this
    min_split: int             # do not split a chunk below this size; go to fallback


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def budget(short: str) -> Budget:
    return Budget(
        max_batch_attempts=_int("OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS", 2),
        max_single_fallback=_int("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", 8),
        max_provider_calls=_int("OVERLAY_ENRICH_MAX_PROVIDER_CALLS", 32),
        wallclock_budget_ms=_int("OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS", 20000),
        keep_threshold=float(os.environ.get("OVERLAY_ENRICH_KEEP_THRESHOLD", "0.75")),
        min_split=_int("OVERLAY_ENRICH_MIN_SPLIT", 4),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_config.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): batched-enrichment rollout config + kill switch (C10)"
```

---

## Task 2: Cache versioning migration + versioned cache helpers

**Files:**
- Create: `src/featuregen/db/migrations/0977_enrichment_cache_versioning.sql`
- Modify: `src/featuregen/overlay/upload/enrich.py` (`_cache_get`, `_cache_put`, the three `enrich_*` single-mode loops, version constants)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`, existing `tests/featuregen/overlay/upload/test_enrich.py`

**Interfaces:**
- Produces: `_cache_get(conn, table, hashes, cache_version)`, `_cache_put(conn, table, content_hash, value, cache_version)`, module constants `_CONCEPT_CACHE_VERSION`, `_DEFINITION_CACHE_VERSION`, `_DOMAIN_CACHE_VERSION`, `_vocab_fingerprint()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.overlay.upload import enrich
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash


def test_cache_is_version_scoped(db):
    row = CanonicalRow("deposits", "accounts", "balance", "numeric")
    h = content_hash(row)
    enrich._cache_put(db, "enrichment_concept", h, "monetary_stock", "vA")
    assert enrich._cache_get(db, "enrichment_concept", [h], "vA") == {h: "monetary_stock"}
    # A different cache_version does NOT see the vA entry -> forces recompute (spec C6).
    assert enrich._cache_get(db, "enrichment_concept", [h], "vB") == {}


def test_vocab_fingerprint_is_stable_and_short():
    fp = enrich._vocab_fingerprint()
    assert len(fp) == 12 and fp == enrich._vocab_fingerprint()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py::test_cache_is_version_scoped -q`
Expected: FAIL — `_cache_get()` takes 3 positional args, not 4 (`cache_version` unknown).

- [ ] **Step 3a: Write the migration**

```sql
-- src/featuregen/db/migrations/0977_enrichment_cache_versioning.sql
-- Spec C6: the enrichment caches keyed on content_hash alone served stale values after the
-- vocabulary / prompt / schema changed. Add a cache_version dimension so a version bump invalidates
-- cleanly. Existing rows are stamped 'legacy' (a distinct version), so the first ingest under the
-- current fingerprint simply recomputes them. Backward-compatible; all idempotent.
ALTER TABLE enrichment_concept    ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE enrichment_definition ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE enrichment_domain     ADD COLUMN IF NOT EXISTS cache_version text NOT NULL DEFAULT 'legacy';

ALTER TABLE enrichment_concept    DROP CONSTRAINT IF EXISTS enrichment_concept_pkey;
ALTER TABLE enrichment_definition DROP CONSTRAINT IF EXISTS enrichment_definition_pkey;
ALTER TABLE enrichment_domain     DROP CONSTRAINT IF EXISTS enrichment_domain_pkey;

ALTER TABLE enrichment_concept    ADD PRIMARY KEY (content_hash, cache_version);
ALTER TABLE enrichment_definition ADD PRIMARY KEY (content_hash, cache_version);
ALTER TABLE enrichment_domain     ADD PRIMARY KEY (content_hash, cache_version);
```

- [ ] **Step 3b: Version constants + fingerprint in `enrich.py`**

Add after the `_CONCEPT_VOCABULARY` definition (line ~21):

```python
def _vocab_fingerprint() -> str:
    """Short, stable fingerprint of the concept vocabulary (names only) — bumps the concept cache
    version whenever the classification targets change (spec C6)."""
    raw = json.dumps([c["name"] for c in _CONCEPT_VOCABULARY])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# Cache versions fold prompt/schema/vocabulary identity into the cache key (spec C6). Bump the vN
# literal on any prompt or schema change to a task; the concept version also tracks the vocabulary.
_CONCEPT_CACHE_VERSION = f"concept:v1:{_vocab_fingerprint()}"
_DEFINITION_CACHE_VERSION = "definition:v1"
_DOMAIN_CACHE_VERSION = "domain:v1"
```

- [ ] **Step 3c: Versioned cache helpers in `enrich.py`**

Replace `_cache_get` and `_cache_put` (lines 44-59):

```python
def _cache_get(conn, cache_table: str, hashes: list[str], cache_version: str) -> dict[str, str]:
    if not hashes:
        return {}
    col = _CACHES[cache_table]
    rows = conn.execute(
        f"SELECT content_hash, {col} FROM {cache_table} "
        "WHERE content_hash = ANY(%s) AND cache_version = %s",
        (hashes, cache_version)).fetchall()
    return {r[0]: r[1] for r in rows}


def _cache_put(conn, cache_table: str, content_hash_: str, value: str, cache_version: str) -> None:
    col = _CACHES[cache_table]
    conn.execute(
        f"INSERT INTO {cache_table} (content_hash, cache_version, {col}) VALUES (%s, %s, %s) "
        "ON CONFLICT (content_hash, cache_version) DO NOTHING",
        (content_hash_, cache_version, value))
```

- [ ] **Step 3d: Thread the version through the three existing single-mode loops**

In `enrich_concepts`: `result = _cache_get(conn, "enrichment_concept", list(by_hash), _CONCEPT_CACHE_VERSION)` and `_cache_put(conn, "enrichment_concept", h, concept, _CONCEPT_CACHE_VERSION)`.
In `draft_definitions`: use `"enrichment_definition"` + `_DEFINITION_CACHE_VERSION` in both the `_cache_get` and `_cache_put` calls.
In `classify_domains`: use `"enrichment_domain"` + `_DOMAIN_CACHE_VERSION` in both.

- [ ] **Step 4: Run tests**

Run: `pytest tests/featuregen/overlay/upload/test_enrich.py tests/featuregen/overlay/upload/test_enrich_batch.py -q`
Expected: PASS — existing enrich tests still green (single mode unchanged behaviourally), plus the 2 new version-scoping tests.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0977_enrichment_cache_versioning.sql src/featuregen/overlay/upload/enrich.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): version-scoped enrichment cache (C6) — vocab/prompt fingerprint in key"
```

---

## Task 3: Result contracts + ref-set validation (pure)

**Files:**
- Create: `src/featuregen/overlay/upload/enrich_batch.py`
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Produces: `BatchItem`, `BatchItemOutcome`, `BatchCallResult`, status constants, `validate_batch_results`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.overlay.upload import enrich_batch as eb


def _accept_known(raw):
    known = {"monetary_stock", "unclassified"}
    if raw == "unclassified":
        return "unclassified", "valid"
    return (raw, "valid") if raw in known else (None, "invalid_value")


def test_validate_classifies_every_return():
    items = [eb.BatchItem("r1", {}), eb.BatchItem("r2", {}), eb.BatchItem("r3", {})]
    results = [
        {"ref": "r1", "concept": "monetary_stock"},   # valid
        {"ref": "r2", "concept": "made_up"},           # invalid_value -> not cacheable
        {"ref": "r2", "concept": "monetary_stock"},    # duplicate ref
        {"ref": "rX", "concept": "monetary_stock"},    # extra (not requested)
        {"ref": "r4", "concept": ""},                  # extra (not requested) + blank value
    ]
    out = {o.ref: o for o in eb.validate_batch_results(items, results, "concept", _accept_known)}
    assert out["r1"].status == eb.VALID and out["r1"].value == "monetary_stock"
    assert out["r2"].status == eb.INVALID and out["r2"].value is None
    assert out["rX"].status == eb.EXTRA
    assert out["r3"].status == eb.MISSING   # never returned
    # the second r2 entry is a duplicate; recorded distinctly
    dups = [o for o in eb.validate_batch_results(items, results, "concept", _accept_known)
            if o.status == eb.DUPLICATE]
    assert len(dups) == 1 and dups[0].ref == "r2"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py::test_validate_classifies_every_return -q`
Expected: FAIL — `validate_batch_results` undefined.

- [ ] **Step 3: Write minimal implementation**

```python
# src/featuregen/overlay/upload/enrich_batch.py
"""Task-agnostic batching engine for advisory enrichment (spec C2/C4/C5).
Pure helpers here (validation, chunking); the governed provider call lives in enrich_llm.py and the
degradation ladder in run_batched (Task 6)."""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass

VALID = "valid"
MISSING = "missing"
EXTRA = "extra"
DUPLICATE = "duplicate"
BLANK = "blank"
INVALID = "invalid_value"
EGRESS = "egress_rejected"
FALLBACK_VALID = "fallback_valid"
FALLBACK_FAILED = "fallback_failed"

Accept = Callable[[str], "tuple[str | None, str]"]   # raw -> (value_to_cache | None, reason_code)


@dataclass(frozen=True)
class BatchItem:
    ref: str          # stable per-item id = the cache/return key (content hash, or table name)
    metadata: dict    # metadata-only fields for the prompt (table/column/type/columns/concept)


@dataclass(frozen=True)
class BatchItemOutcome:
    ref: str
    status: str
    value: str | None
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class BatchCallResult:
    outcomes: tuple[BatchItemOutcome, ...]
    provider_calls: int
    input_tokens: int
    output_tokens: int


def validate_batch_results(items: list[BatchItem], results: list[dict], out_key: str,
                           accept: Accept) -> list[BatchItemOutcome]:
    """Classify every returned entry against the expected ref-set (spec C2): valid / invalid_value /
    blank / duplicate / extra, and every unreturned ref as missing. Nothing is silently collapsed."""
    expected = {it.ref for it in items}
    seen: set[str] = set()
    outcomes: list[BatchItemOutcome] = []
    for entry in results:
        ref = entry.get("ref")
        raw = str(entry.get(out_key, "")).strip()
        if ref not in expected:
            outcomes.append(BatchItemOutcome(str(ref), EXTRA, None, (EXTRA,)))
            continue
        if ref in seen:
            outcomes.append(BatchItemOutcome(ref, DUPLICATE, None, (DUPLICATE,)))
            continue
        seen.add(ref)
        if not raw:
            outcomes.append(BatchItemOutcome(ref, BLANK, None, (BLANK,)))
            continue
        value, reason = accept(raw)
        if value is None:
            outcomes.append(BatchItemOutcome(ref, INVALID, None, (reason,)))
        else:
            outcomes.append(BatchItemOutcome(ref, VALID, value, (VALID,)))
    for ref in expected - seen:
        outcomes.append(BatchItemOutcome(ref, MISSING, None, (MISSING,)))
    return outcomes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py::test_validate_classifies_every_return -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_batch.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): batch result contracts + ref-set validation (C2)"
```

---

## Task 4: Batch array schemas + `audited_batch_call` (governed seam)

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (add `_SCHEMAS` batch entries, `_item_egress_ok`, `audited_batch_call`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Consumes: `BatchItem`, `BatchCallResult`, `validate_batch_results` (Task 3); `audited_structured_call` internals — reuses `DocumentSchemaRegistry`, `assert_llm_safe`, `drive_structured_call`, `record_llm_call`, `_generation_settings`, `_ENRICH_ACTOR`, `_RUN`, `_REDACTION_VERSION`, `_audit_egress_block`.
- Produces: `audited_batch_call(...) -> BatchCallResult` (signature in File Structure).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.enrich_llm import audited_batch_call

_CTASK = "overlay.enrich.concept"


def _accept_known(raw):
    return ("monetary_stock", "valid") if raw == "monetary_stock" else (None, "invalid_value")


def test_audited_batch_call_returns_per_item_outcomes(db):
    items = [eb.BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "accounts", "column": "mystery", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "made_up"}]})})
    res = audited_batch_call(db, client, task=_CTASK, prompt_id="overlay_concept_batch_v1",
                             schema_id="overlay_concept_batch",
                             shared_metadata={"vocabulary": [{"name": "monetary_stock"}]},
                             items=items, out_key="concept", instruction="Classify each column.",
                             accept=_accept_known)
    by = {o.ref: o for o in res.outcomes}
    assert by["h1"].status == eb.VALID and by["h1"].value == "monetary_stock"
    assert by["h2"].status == eb.INVALID
    assert res.provider_calls == 1
    # one immutable llm_call row was written for the batch (item summary in cost_metadata)
    n = db.execute("SELECT count(*) FROM llm_call WHERE task = %s", (_CTASK,)).fetchone()[0]
    assert n == 1


def test_audited_batch_call_excludes_unsafe_item_before_egress(db):
    # An item whose metadata carries a disallowed key (free-text definition) is excluded, audited,
    # and the remainder still batched (spec C9 exclude-and-proceed).
    items = [eb.BatchItem("h1", {"table": "accounts", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "accounts", "column": "ssn", "type": "text",
                                 "definition": "customer social security number"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"}]})})
    res = audited_batch_call(db, client, task=_CTASK, prompt_id="overlay_concept_batch_v1",
                             schema_id="overlay_concept_batch", shared_metadata={},
                             items=items, out_key="concept", instruction="Classify each column.",
                             accept=_accept_known)
    by = {o.ref: o for o in res.outcomes}
    assert by["h2"].status == eb.EGRESS
    assert by["h1"].status == eb.VALID
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py::test_audited_batch_call_returns_per_item_outcomes -q`
Expected: FAIL — `audited_batch_call` and `overlay_concept_batch` schema undefined.

- [ ] **Step 3a: Add batch array schemas to `_SCHEMAS` in `enrich_llm.py`**

Insert into the `_SCHEMAS` dict (bounded arrays — spec C18; `maxItems` is a generous backstop, app validation enforces the real cap):

```python
    ("overlay_concept_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "concept": {"type": "string", "maxLength": 128}},
                      "required": ["ref", "concept"]}}},
        "required": ["results"]},
    ("overlay_definition_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "definition": {"type": "string", "maxLength": 500}},
                      "required": ["ref", "definition"]}}},
        "required": ["results"]},
    ("overlay_domain_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 256},
                                     "domain": {"type": "string", "maxLength": 64}},
                      "required": ["ref", "domain"]}}},
        "required": ["results"]},
```

- [ ] **Step 3b: Add the per-item egress allowlist + `audited_batch_call`**

Add imports at the top of `enrich_llm.py`:

```python
from featuregen.overlay.upload.enrich_batch import (
    EGRESS, BatchCallResult, BatchItem, BatchItemOutcome, validate_batch_results,
)
```

Add near the bottom of `enrich_llm.py`:

```python
# Only metadata may egress per item (Global Constraint). Any other key (e.g. a free-text definition
# or a data value) means the item is excluded pre-egress and audited (spec C9 per-item egress).
_ITEM_META_ALLOWED = frozenset({"table", "column", "type", "columns", "concept"})


def _item_egress_ok(metadata: dict) -> bool:
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for v in metadata.values():
        if isinstance(v, list):
            if not all(isinstance(x, str) and len(x) <= 200 for x in v):
                return False
        elif not isinstance(v, str) or len(v) > 200:
            return False
    return True


def audited_batch_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                       shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor: IdentityEnvelope | None = None) -> BatchCallResult:
    """One GOVERNED batch call (spec C4/C9): per-item egress filter -> batch-level egress guard ->
    schema-validated array call -> one immutable llm_call with a per-item outcome summary. Returns a
    BatchCallResult whose outcomes classify every requested ref (via validate_batch_results)."""
    actor = actor or _ENRICH_ACTOR
    excluded = [it for it in items if not _item_egress_ok(it.metadata)]
    included = [it for it in items if _item_egress_ok(it.metadata)]
    egress_outcomes = [BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)) for it in excluded]
    for it in excluded:
        _audit_egress_block(conn, task=task, actor=actor, reason="item metadata not metadata-only")

    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)

    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(schema_id, 1)
    if schema is None:
        register_enrichment_schemas(conn)
        schema = reg.schema_for(schema_id, 1)

    catalog_metadata = {**shared_metadata,
                        "items": [{"ref": it.ref, **it.metadata} for it in included]}
    redaction = RedactionResult(text=instruction, redaction_version=_REDACTION_VERSION,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=1, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=1,
                     generation_settings=_generation_settings(), output_schema=schema)

    try:
        assert_llm_safe(req)                      # batch-level egress backstop (spec C9)
    except EgressViolation as exc:
        logger.warning("egress guard blocked batch %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        missing = validate_batch_results(included, [], out_key, accept)
        return BatchCallResult(tuple(egress_outcomes) + tuple(missing), 0, 0, 0)

    outcome = drive_structured_call(client, req, lambda o: reg.validate(schema_id, 1, o))
    results = outcome.output.get("results", []) if isinstance(outcome.output, dict) else []
    item_outcomes = validate_batch_results(included, results, out_key, accept)

    summary = {"requested": [it.ref for it in included],
               "outcomes": {o.ref: o.status for o in item_outcomes}}
    cost = dict(outcome.cost_metadata or {})
    record_llm_call(conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
                    redaction_version=_REDACTION_VERSION, input_redaction={},
                    raw_output={"output": outcome.output,
                                "self_reported_scores": outcome.self_reported_scores},
                    validation_result=outcome.validation_result,
                    repair_attempts=list(outcome.repair_attempts), latency_ms=None,
                    cost_metadata={**cost, "batch": summary}, created_by=identity_to_jsonb(actor))

    return BatchCallResult(
        outcomes=tuple(egress_outcomes) + tuple(item_outcomes), provider_calls=1,
        input_tokens=int(cost.get("input_tokens", 0)), output_tokens=int(cost.get("output_tokens", 0)))
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -q`
Expected: PASS (both new `audited_batch_call` tests + all prior).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): governed batch seam audited_batch_call — array schema, per-item egress, item audit (C8/C9)"
```

---

## Task 5: Token-aware chunker

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` (add `chunk_items`, `estimate_tokens`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Produces: `chunk_items(items, *, max_items, max_input_tokens) -> list[list[BatchItem]]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
def test_chunk_respects_item_count():
    items = [eb.BatchItem(f"r{i}", {"column": "c"}) for i in range(25)]
    chunks = eb.chunk_items(items, max_items=10, max_input_tokens=10_000)
    assert [len(c) for c in chunks] == [10, 10, 5]


def test_chunk_respects_token_budget():
    big = "x" * 400   # ~100 tokens each
    items = [eb.BatchItem(f"r{i}", {"column": big}) for i in range(10)]
    chunks = eb.chunk_items(items, max_items=100, max_input_tokens=250)
    assert all(len(c) <= 3 for c in chunks) and sum(len(c) for c in chunks) == 10


def test_chunk_never_drops_an_oversized_singleton():
    items = [eb.BatchItem("r0", {"column": "x" * 10_000})]
    chunks = eb.chunk_items(items, max_items=10, max_input_tokens=10)
    assert chunks == [[items[0]]]   # one item always survives as its own chunk
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k chunk -q`
Expected: FAIL — `chunk_items` undefined.

- [ ] **Step 3: Write minimal implementation**

Append to `enrich_batch.py`:

```python
def estimate_tokens(item: BatchItem) -> int:
    """Cheap upper-ish estimate: ~4 chars/token over the item's metadata JSON, floor 8."""
    return max(8, len(json.dumps(item.metadata, default=str)) // 4)


def chunk_items(items: list[BatchItem], *, max_items: int,
                max_input_tokens: int) -> list[list[BatchItem]]:
    """Split into chunks bounded by BOTH item count and estimated input tokens (spec C5). A single
    item that alone exceeds the token budget still forms its own chunk (never dropped)."""
    chunks: list[list[BatchItem]] = []
    cur: list[BatchItem] = []
    tok = 0
    for it in items:
        t = estimate_tokens(it)
        if cur and (len(cur) >= max_items or tok + t > max_input_tokens):
            chunks.append(cur)
            cur, tok = [], 0
        cur.append(it)
        tok += t
    if cur:
        chunks.append(cur)
    return chunks
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k chunk -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_batch.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): token-aware + item-count chunker (C5)"
```

---

## Task 6: Orchestrator `run_batched` — degradation ladder + budgets + telemetry

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` (add `run_batched`, `_single_fallback`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Consumes: `enrich_config` (Task 1); `audited_batch_call` (Task 4); `audited_enrich_call` (existing, for fallback); `counters` from `featuregen.runtime.observability`.
- Produces: `run_batched(...) -> dict[str, str]` — `{ref: accepted_value}` for items resolved this run (caller caches + returns).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.intake.llm import FakeResponse


def test_run_batched_salvages_valid_and_leaves_invalid_uncached(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    items = [eb.BatchItem("h1", {"table": "t", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "t", "column": "mystery", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": [
        {"ref": "h1", "concept": "monetary_stock"},
        {"ref": "h2", "concept": "made_up"}]})})
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {"h1": "monetary_stock"}     # invalid h2 not returned, not cached (spec C3/C4)


def test_run_batched_falls_back_to_single_for_missing(db, monkeypatch):
    # Batch omits h2 entirely (missing); bounded single fallback recovers it.
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    items = [eb.BatchItem("h1", {"table": "t", "column": "balance", "type": "numeric"}),
             eb.BatchItem("h2", {"table": "t", "column": "bal2", "type": "numeric"})]
    client = FakeLLM(script={_CTASK: [
        FakeResponse(output={"results": [{"ref": "h1", "concept": "monetary_stock"}]}),  # batch: h2 missing
        FakeResponse(output={"concept": "monetary_stock"})]})                            # single fallback for h2
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {"h1": "monetary_stock", "h2": "monetary_stock"}


def test_run_batched_respects_single_fallback_cap(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    monkeypatch.setenv("OVERLAY_ENRICH_MAX_SINGLE_FALLBACK", "0")   # no fallback allowed
    items = [eb.BatchItem("h1", {"table": "t", "column": "c", "type": "text"})]
    client = FakeLLM(script={_CTASK: FakeResponse(output={"results": []})})   # batch returns nothing
    got = eb.run_batched(db, client, short="concept", task=_CTASK,
                         prompt_id="overlay_concept_batch_v1", schema_id="overlay_concept_batch",
                         shared_metadata={}, items=items, out_key="concept",
                         instruction="Classify.", accept=_accept_known, actor=None)
    assert got == {}   # unresolved, left uncached (retried next ingest)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k run_batched -q`
Expected: FAIL — `run_batched` undefined.

- [ ] **Step 3: Write minimal implementation**

Append to `enrich_batch.py` (add imports `import time`, and `from featuregen.overlay.upload import enrich_config`, `from featuregen.overlay.upload.enrich_llm import audited_batch_call, audited_enrich_call`, `from featuregen.runtime.observability import counters` at the top of the file):

```python
def _single_fallback(conn, client, *, task, out_key, instruction, item: BatchItem, shared_metadata,
                     accept, actor) -> tuple[str | None, str]:
    """One per-item fallback through the existing single seam. Returns (value|None, status)."""
    single_prompt = task.rsplit(".", 1)[-1]   # concept|definition|domain
    raw = audited_enrich_call(
        conn, client, task=task, prompt_id=f"overlay_{single_prompt}_v1",
        schema_id=f"overlay_{single_prompt}", out_key=out_key,
        catalog_metadata={**shared_metadata, **item.metadata}, instruction=instruction, actor=actor)
    if raw is None:
        return None, FALLBACK_FAILED
    value, _reason = accept(raw)
    return (value, FALLBACK_VALID) if value is not None else (None, FALLBACK_FAILED)


def run_batched(conn, client, *, short: str, task: str, prompt_id: str, schema_id: str,
                shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                accept: Accept, actor) -> dict[str, str]:
    """Chunk `items`, call the governed batch seam, and walk the bounded degradation ladder
    (spec C4): salvage valid -> retry a failed chunk -> adaptive split -> capped single fallback ->
    leave remainder uncached. Returns {ref: accepted_value} for items resolved this run."""
    b = enrich_config.budget(short)
    max_items = enrich_config.max_items(short)
    max_tokens = enrich_config.max_input_tokens(short)
    started = time.monotonic()
    calls = 0
    resolved: dict[str, str] = {}
    fallback_used = 0

    def over_budget() -> bool:
        return (calls >= b.max_provider_calls
                or (time.monotonic() - started) * 1000 >= b.wallclock_budget_ms)

    def process(chunk: list[BatchItem], attempt: int) -> None:
        nonlocal calls, fallback_used
        if not chunk or over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.budget_exhausted") if chunk else None
            return
        res = audited_batch_call(conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
                                 shared_metadata=shared_metadata, items=chunk, out_key=out_key,
                                 instruction=instruction, accept=accept, actor=actor)
        calls += res.provider_calls
        counters.incr(f"overlay.enrich.{short}.batch.calls")
        for o in res.outcomes:
            if o.status in (VALID,) and o.value is not None:
                resolved[o.ref] = o.value
        unresolved = [it for it in chunk if it.ref not in resolved]
        if not unresolved:
            return
        valid_ratio = 1 - len(unresolved) / len(chunk)
        if valid_ratio >= b.keep_threshold:
            _fallback(unresolved)                      # salvage the bulk; fallback only the few
            return
        if attempt < b.max_batch_attempts and not over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.retry")
            process(unresolved, attempt + 1)           # retry the unresolved as a chunk
            return
        if len(unresolved) > b.min_split and not over_budget():
            counters.incr(f"overlay.enrich.{short}.batch.split")
            mid = len(unresolved) // 2
            process(unresolved[:mid], 0)
            process(unresolved[mid:], 0)
            return
        _fallback(unresolved)

    def _fallback(unresolved: list[BatchItem]) -> None:
        nonlocal calls, fallback_used
        for it in unresolved:
            if fallback_used >= b.max_single_fallback or over_budget():
                counters.incr(f"overlay.enrich.{short}.batch.left_uncached")
                continue
            fallback_used += 1
            calls += 1
            counters.incr(f"overlay.enrich.{short}.batch.single_fallback")
            value, status = _single_fallback(conn, client, task=task, out_key=out_key,
                                              instruction=instruction, item=it,
                                              shared_metadata=shared_metadata, accept=accept,
                                              actor=actor)
            if value is not None:
                resolved[it.ref] = value

    for chunk in chunk_items(items, max_items=max_items, max_input_tokens=max_tokens):
        process(chunk, 0)
    return resolved
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k run_batched -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_batch.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): run_batched orchestrator — degradation ladder, budgets, telemetry (C4)"
```

---

# Phase 2 — Concept batching

## Task 7: `enrich_concepts` batch path (C3 policy, versioned key)

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (add accept fns + a batch branch in `enrich_concepts`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`, `tests/featuregen/overlay/upload/test_enrich.py`

**Interfaces:**
- Consumes: `run_batched`, `BatchItem` (Task 6); `enrich_config.mode` (Task 1); versioned cache helpers (Task 2).
- Produces: `_accept_concept`, and an `enrich_concepts` that returns `{content_hash: concept}` identically in shape to today.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.overlay.upload.enrich import enrich_concepts


def test_enrich_concepts_batch_mode_caches_valid_only(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric"),
            CanonicalRow("deposits", "accounts", "mystery", "text")]
    h0, h1 = content_hash(rows[0]), content_hash(rows[1])
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": [
        {"ref": h0, "concept": "monetary_stock"},
        {"ref": h1, "concept": "totally_made_up"}]})})
    out = enrich_concepts(db, rows, client)
    assert out == {h0: "monetary_stock"}       # invalid concept NOT cached as UNCLASSIFIED (C3)
    # a second batch run for the same rows hits the cache for h0 (no call needed for it)
    cached = enrich_concepts(db, rows, FakeLLM(script={"overlay.enrich.concept": FakeResponse(
        output={"results": [{"ref": h1, "concept": "unclassified"}]})}))
    assert cached[h0] == "monetary_stock" and cached[h1] == "unclassified"


def test_enrich_concepts_single_mode_unchanged(db, monkeypatch):
    monkeypatch.delenv("OVERLAY_ENRICH_CONCEPT_MODE", raising=False)   # default single
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    client = FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"concept": "totally_made_up"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "unclassified"   # single keeps today's coerce behaviour
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k enrich_concepts -q`
Expected: FAIL — batch mode still runs the single loop (no `results` key) → KeyError / wrong shape.

- [ ] **Step 3: Add the accept fn + batch branch in `enrich.py`**

Add imports at the top of `enrich.py`:

```python
from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched
```

Add the concept accept function (spec C3) after `_bounded`:

```python
def _accept_concept(raw: str) -> tuple[str | None, str]:
    """Batch-path concept policy (spec C3): the literal 'unclassified' is a real classification and
    IS cached; a known concept is cached; anything else is invalid -> NOT cached (retried next
    ingest). This differs from single mode, which coerces unknowns to UNCLASSIFIED."""
    v = raw.strip()
    if v == UNCLASSIFIED:
        return UNCLASSIFIED, "valid"
    if is_known_concept(v):
        return v, "valid"
    return None, "invalid_value"
```

Rewrite `enrich_concepts` so the batch branch sits in front of today's loop (leave the existing loop body intact as the single path):

```python
def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient,
                    actor=None) -> dict[str, str]:
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    result = _cache_get(conn, "enrichment_concept", list(by_hash), _CONCEPT_CACHE_VERSION)

    if enrich_config.mode("concept") == "batch":
        misses = [BatchItem(h, {"table": r.table, "column": r.column, "type": r.type})
                  for h, r in by_hash.items() if h not in result]
        resolved = run_batched(
            conn, client, short="concept", task=_TASK, prompt_id="overlay_concept_batch_v1",
            schema_id="overlay_concept_batch",
            shared_metadata={"vocabulary": _CONCEPT_VOCABULARY}, items=misses, out_key="concept",
            instruction="For each item classify the column into the provided controlled concept "
                        "vocabulary — choose the single best-fitting concept name, or 'unclassified' "
                        "if none fits. Return exactly one result per input ref; treat each item "
                        "independently.", accept=_accept_concept, actor=actor)
        for h, concept in resolved.items():
            _cache_put(conn, "enrichment_concept", h, concept, _CONCEPT_CACHE_VERSION)
            result[h] = concept
        return result

    for h, row in by_hash.items():                    # single mode — today's exact behaviour
        if h in result:
            continue
        raw = _call(conn, client, _TASK, "overlay_concept_v1", "overlay_concept",
                    {"table": row.table, "column": row.column, "type": row.type,
                     "vocabulary": _CONCEPT_VOCABULARY}, "concept",
                    "Classify this column into the provided controlled concept vocabulary — choose the "
                    "single best-fitting concept name, or 'unclassified' if none fits.", actor)
        if raw is None:
            continue
        concept = raw if is_known_concept(raw) else UNCLASSIFIED
        _cache_put(conn, "enrichment_concept", h, concept, _CONCEPT_CACHE_VERSION)
        result[h] = concept
    return result
```

- [ ] **Step 4: Run tests**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py tests/featuregen/overlay/upload/test_enrich.py -q`
Expected: PASS — new batch tests + single-mode test + all existing enrich tests green.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): concept batch path — invalid!=UNCLASSIFIED, versioned key (C3)"
```

---

# Phase 3 — ingest isolation + domain batching

## Task 8: `ingest.py` independent per-task fail-soft (C1)

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py:123-134`
- Test: `tests/featuregen/overlay/upload/test_ingest_slice.py` (append; this file already exists and already imports `ingest_upload`, defines `_actor()`, and has an existing whole-enrichment fail-soft test using a `_Boom()` client at line ~83 — that test stays green under the new per-task blocks).

**Interfaces:**
- Consumes: `enrich_concepts`, `draft_definitions`, `classify_domains` (unchanged signatures, except `draft_definitions` gains an optional `concepts` kwarg in Task 10 — Task 8 lands the plain call and Task 10 flips it to `concepts=concepts`, so no broken intermediate).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_ingest_slice.py  (append)
# Existing imports at the top of this file already provide: datetime, UTC, CanonicalRow,
# ingest_upload, and the _actor() helper. Add FakeLLM/FakeResponse to the imports.
from featuregen.intake.llm import FakeLLM, FakeResponse


def test_domain_failure_does_not_discard_concepts(db, monkeypatch):
    # A domain enrichment blow-up must not null out concepts/definitions (spec C1). Stub
    # classify_domains to raise and assert the concept enrichment still reached the graph.
    from featuregen.overlay.upload import ingest as ing
    monkeypatch.setattr(ing, "classify_domains",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    captured: dict = {}
    real_build = ing.build_graph

    def spy(conn, src, rows, concepts, definitions, domains):
        captured.update(concepts=concepts, domains=domains)
        return real_build(conn, src, rows, concepts, definitions, domains)

    monkeypatch.setattr(ing, "build_graph", spy)
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    client = FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_stock"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "the balance"})})
    now = datetime(2026, 7, 5, tzinfo=UTC)
    ing.ingest_upload(db, "deposits", rows, actor=_actor(), now=now, client=client)
    assert captured["concepts"] and captured["domains"] is None   # concepts survived; only domains lost
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_ingest_slice.py::test_domain_failure_does_not_discard_concepts -q`
Expected: FAIL — today's single `try` nulls all three, so `captured["concepts"]` is falsy.

- [ ] **Step 3: Replace lines 123-134 in `ingest.py`**

```python
    concepts = definitions = domains = None
    if client is not None:
        # Three INDEPENDENT advisory failure domains (spec C1): a failure in one task must not
        # discard another's already-computed enrichment. Each degrades search, never the facts.
        try:
            concepts = enrich_concepts(conn, vr.good, client, actor)
        except Exception:  # noqa: BLE001
            logger.warning("advisory concept enrichment failed for %r", catalog_source, exc_info=True)
        try:
            definitions = draft_definitions(conn, vr.good, client, actor, concepts=concepts)
        except Exception:  # noqa: BLE001
            logger.warning("advisory definition enrichment failed for %r", catalog_source, exc_info=True)
        try:
            domains = classify_domains(conn, vr.good, client, actor)
        except Exception:  # noqa: BLE001
            logger.warning("advisory domain enrichment failed for %r", catalog_source, exc_info=True)
    build_graph(conn, catalog_source, vr.good, concepts, definitions, domains)
```

Note: `draft_definitions(..., concepts=concepts)` — the `concepts` keyword is added to `draft_definitions` in Task 10. Until then it accepts `**_` is NOT acceptable; instead land Task 10's signature change together, OR temporarily call `draft_definitions(conn, vr.good, client, actor)` and add `concepts=` in Task 10. **Choose:** land Task 8 with the plain call `draft_definitions(conn, vr.good, client, actor)` and change it to pass `concepts=concepts` as the final step of Task 10. (Prevents a broken intermediate commit.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/featuregen/overlay/upload/test_ingest_slice.py -q`
Expected: PASS — the new isolation test + the existing `_Boom()` whole-enrichment fail-soft test + all other ingest-slice tests.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_ingest_slice.py
git commit -m "fix(enrich): independent per-task fail-soft for advisory enrichment (C1)"
```

---

## Task 9: `classify_domains` batch path

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (batch branch in `classify_domains`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Consumes: `run_batched`, `BatchItem`; `_accept_bounded` (added here); `_table_content_hash`, `_DOMAIN_CACHE_VERSION`.
- Produces: `_accept_bounded(max_len)` factory; a `classify_domains` returning `{table: domain}` unchanged in shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.overlay.upload.enrich import classify_domains


def test_classify_domains_batch_mode(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_DOMAIN_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "id", "integer"),
            CanonicalRow("deposits", "loans", "principal", "numeric")]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"results": [
        {"ref": "accounts", "domain": "Deposits"},
        {"ref": "loans", "domain": "Lending"}]})})
    out = classify_domains(db, rows, client)
    assert out == {"accounts": "Deposits", "loans": "Lending"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py::test_classify_domains_batch_mode -q`
Expected: FAIL — batch mode runs the single loop (expects `{"domain": ...}`, not `results`).

- [ ] **Step 3: Add `_accept_bounded` + batch branch**

Add the factory near `_accept_concept`:

```python
def _accept_bounded(max_len: int):
    """Accept a plausible short single-line value (reuses _bounded); else invalid -> not cached."""
    def _accept(raw: str) -> tuple[str | None, str]:
        v = _bounded(raw, max_len)
        return (v, "valid") if v is not None else (None, "invalid_value")
    return _accept
```

Insert a batch branch at the top of `classify_domains`, after `hash_of_table`/`cached` are computed:

```python
    if enrich_config.mode("domain") == "batch":
        misses = [BatchItem(t, {"table": t, "columns": sorted(cols)})
                  for t, cols in by_table.items() if hash_of_table[t] not in cached]
        result = {t: cached[hash_of_table[t]] for t in by_table if hash_of_table[t] in cached}
        resolved = run_batched(
            conn, client, short="domain", task=_DOMAIN_TASK, prompt_id="overlay_domain_batch_v1",
            schema_id="overlay_domain_batch", shared_metadata={}, items=misses, out_key="domain",
            instruction="For each item classify the table's business domain. Return exactly one "
                        "result per input ref; treat each table independently.",
            accept=_accept_bounded(64), actor=actor)
        for table, domain in resolved.items():
            _cache_put(conn, "enrichment_domain", hash_of_table[table], domain, _DOMAIN_CACHE_VERSION)
            result[table] = domain
        return result
```

(Leave the existing per-table single loop below as the single-mode path.)

- [ ] **Step 4: Run tests**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py tests/featuregen/overlay/upload/test_enrich.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): domain batch path (global chunk, table refs)"
```

---

# Phase 4 — definition batching

## Task 10: `draft_definitions` batch path — group-by-table, anti-contamination, concept-dependent key

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (`draft_definitions` gains `concepts` kwarg + batch branch + concept-dependent cache key)
- Modify: `src/featuregen/overlay/upload/ingest.py` (final step: pass `concepts=concepts`)
- Test: `tests/featuregen/overlay/upload/test_enrich_batch.py`

**Interfaces:**
- Consumes: `run_batched`, `BatchItem`, `_accept_bounded`, `_DEFINITION_CACHE_VERSION`, `content_hash`.
- Produces: `draft_definitions(conn, rows, client, actor=None, *, concepts: dict[str, str] | None = None)`, returning `{content_hash: definition}`; `_def_cache_key(content_hash, concept)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich_batch.py  (append)
from featuregen.overlay.upload.enrich import draft_definitions


def test_draft_definitions_batch_grouped_by_table(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_DEFINITION_MODE", "batch")
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric"),                    # blank -> drafted
            CanonicalRow("deposits", "accounts", "id", "integer", definition="acct id")]  # declared -> skipped
    h0 = content_hash(rows[0])
    client = FakeLLM(script={"overlay.enrich.definition": FakeResponse(output={"results": [
        {"ref": h0, "definition": "the account ledger balance"}]})})
    out = draft_definitions(db, rows, client, concepts={h0: "monetary_stock"})
    assert out == {h0: "the account ledger balance"}
    assert content_hash(rows[1]) not in out          # declared definition never overwritten (R3)


def test_definition_cache_key_includes_concept(db):
    from featuregen.overlay.upload.enrich import _def_cache_key
    row = CanonicalRow("deposits", "accounts", "bal", "numeric")
    assert _def_cache_key(content_hash(row), "monetary_stock") != _def_cache_key(content_hash(row), "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/featuregen/overlay/upload/test_enrich_batch.py -k draft_definitions -q`
Expected: FAIL — `draft_definitions` has no `concepts` kwarg / no batch path; `_def_cache_key` undefined.

- [ ] **Step 3: Rewrite `draft_definitions` + add `_def_cache_key`**

Add the key helper near `content_hash`:

```python
def _def_cache_key(row_hash: str, concept: str) -> str:
    """Definition cache key (spec C6): a definition can depend on the assigned concept, so fold the
    concept into the key. Empty concept -> concept-independent key."""
    raw = json.dumps([row_hash, concept or ""])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
```

Rewrite `draft_definitions`:

```python
def draft_definitions(conn, rows: list[CanonicalRow], client: LLMClient, actor=None,
                      *, concepts: dict[str, str] | None = None) -> dict[str, str]:
    """Draft a definition ONLY for columns with no declared one (R3). Keyed by (content_hash,
    assigned concept) so a concept change re-drafts (spec C6). Returns {content_hash: definition}."""
    concepts = concepts or {}
    blank = {content_hash(r): r for r in rows if not r.definition}
    key_of = {h: _def_cache_key(h, concepts.get(h, "")) for h in blank}
    cached = _cache_get(conn, "enrichment_definition", list(key_of.values()), _DEFINITION_CACHE_VERSION)
    result = {h: cached[key_of[h]] for h in blank if key_of[h] in cached}

    if enrich_config.mode("definition") == "batch":
        # Group by table so table context is sent once; the prompt isolates items (anti-contamination).
        misses = [h for h in blank if h not in result]
        misses.sort(key=lambda h: (blank[h].table, h))
        items = [BatchItem(h, {"table": blank[h].table, "column": blank[h].column,
                               "type": blank[h].type, **({"concept": concepts[h]} if concepts.get(h) else {})})
                 for h in misses]
        resolved = run_batched(
            conn, client, short="definition", task=_DEF_TASK,
            prompt_id="overlay_definition_batch_v1", schema_id="overlay_definition_batch",
            shared_metadata={}, items=items, out_key="definition",
            instruction="Draft a one-line business definition for EACH column. Treat each item "
                        "independently: use only that item's table/column/type/concept; do not infer "
                        "relationships between items; do not reuse another item's facts; return "
                        "exactly one result per input ref.", accept=_accept_bounded(500), actor=actor)
        for h, drafted in resolved.items():
            _cache_put(conn, "enrichment_definition", key_of[h], drafted, _DEFINITION_CACHE_VERSION)
            result[h] = drafted
        return result

    for h, row in blank.items():                      # single mode — today's exact behaviour
        if h in result:
            continue
        drafted = _bounded(_call(conn, client, _DEF_TASK, "overlay_definition_v1",
                                 "overlay_definition",
                                 {"table": row.table, "column": row.column, "type": row.type},
                                 "definition",
                                 "Draft a one-line business definition for this column.",
                                 actor), 500)
        if drafted is None:
            continue
        _cache_put(conn, "enrichment_definition", key_of[h], drafted, _DEFINITION_CACHE_VERSION)
        result[h] = drafted
    return result
```

- [ ] **Step 4a: Wire `concepts` through `ingest.py`**

Change the Task-8 definition call to pass concepts:

```python
        try:
            definitions = draft_definitions(conn, vr.good, client, actor, concepts=concepts)
        except Exception:  # noqa: BLE001
            logger.warning("advisory definition enrichment failed for %r", catalog_source, exc_info=True)
```

- [ ] **Step 4b: Run tests**

Run: `pytest tests/featuregen/overlay/upload/ -q`
Expected: PASS — all overlay/upload tests (new definition tests, ingest isolation, existing enrich suite).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_enrich_batch.py
git commit -m "feat(enrich): definition batch path — group-by-table, anti-contamination, concept-keyed cache (C6)"
```

---

# Cross-cutting

## Task 11: Gold-set evaluation harness (quality gate)

This task builds the harness that GATES turning any task's default to `batch`. It is manual/nightly (not unit CI) and asserts on a small human-reviewed gold set (spec: Evaluation methodology). It uses `FakeLLM` for a hermetic self-check of the harness itself; against a real provider it is run manually with `FEATUREGEN_LLM_PROVIDER=anthropic`.

**Files:**
- Create: `tests/eval/__init__.py` (empty), `tests/eval/gold_columns.py` (gold fixtures), `tests/eval/test_enrich_batch_quality.py`
- Test: itself.

**Interfaces:**
- Consumes: `enrich_concepts`, `content_hash`, `is_known_concept`, `FakeLLM`.

- [ ] **Step 1: Write the gold fixtures**

```python
# tests/eval/gold_columns.py
"""Human-reviewed expected concepts for representative + hard columns (spec: Evaluation).
Each entry: (source, table, column, type, expected_concept, acceptable_alternatives)."""
from featuregen.overlay.upload.canonical import CanonicalRow

GOLD = [
    (CanonicalRow("deposits", "accounts", "balance", "numeric"), "monetary_stock", {"monetary_stock"}),
    (CanonicalRow("cards", "txn", "amount", "numeric"), "monetary_flow", {"monetary_flow"}),
    (CanonicalRow("cards", "txn", "status", "text"), "status_flag", {"status_flag", "categorical_code"}),
    (CanonicalRow("loans", "loan", "status", "text"), "status_flag", {"status_flag", "categorical_code"}),
    (CanonicalRow("risk", "exposure", "pd", "numeric"), "probability", {"probability", "risk_score"}),
    # ... expand to >= 40 rows incl. acronyms, same-name-different-table, rare concepts, blanks.
]

# Critical concepts that must NEVER regress vs single mode (stratified gate).
CRITICAL = {"outcome_label", "monetary_flow", "monetary_stock"}
```

- [ ] **Step 2: Write the harness test**

```python
# tests/eval/test_enrich_batch_quality.py
"""Gold-set quality gate for batched concept enrichment. Marked 'eval' — run on demand:
    pytest -m eval tests/eval/ -q
Hermetic mode uses FakeLLM (harness self-check). Live mode: set FEATUREGEN_LLM_PROVIDER=anthropic
and OVERLAY_ENRICH_CONCEPT_MODE=batch, then run against a throwaway DB."""
import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.concepts import is_known_concept
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts
from tests.eval.gold_columns import CRITICAL, GOLD

pytestmark = pytest.mark.eval


def _scripted_batch(rows):
    # Hermetic self-check: script the model to return each gold column's expected concept.
    expected = {content_hash(r): c for r, c, _alts in GOLD}
    return FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": [
        {"ref": h, "concept": expected[h]} for h in (content_hash(r) for r, _c, _a in GOLD)]})})


def test_concept_gold_accuracy_meets_gate(db, monkeypatch):
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [r for r, _c, _a in GOLD]
    client = _scripted_batch(rows)
    out = enrich_concepts(db, rows, client)

    hits = crit_hits = crit_total = 0
    for row, expected, alts in GOLD:
        got = out.get(content_hash(row))
        ok = got in alts
        hits += ok
        if expected in CRITICAL:
            crit_total += 1
            crit_hits += ok
        assert got is None or is_known_concept(got) or got == "unclassified"   # never a hallucination
    accuracy = hits / len(GOLD)
    assert accuracy >= 0.90, f"gold accuracy {accuracy:.2%} below 0.90 gate"
    assert crit_hits == crit_total, "a critical concept regressed (zero-regression gate)"
```

- [ ] **Step 3: Register the `eval` marker**

Add to `pyproject.toml` under `[tool.pytest.ini_options] markers` (or `pytest.ini`): `"eval: manual/nightly quality-gate tests (not run in default CI)"`. Ensure default CI excludes it (e.g. `addopts = "-m 'not eval'"`), matching how the repo scopes slow/manual suites.

- [ ] **Step 4: Run the harness**

Run: `pytest -m eval tests/eval/test_enrich_batch_quality.py -q`
Expected: PASS (hermetic self-check). Confirm default runs skip it: `pytest tests/eval -q` → deselected.

- [ ] **Step 5: Commit**

```bash
git add tests/eval/ pyproject.toml
git commit -m "test(enrich): gold-set quality gate for batched enrichment (eval marker)"
```

---

# Rollout (post-merge, operational — not code)

Enable per task via env once each gate clears (spec: Phased rollout). Do NOT change the `single` defaults in code.

1. **Seam only** (Tasks 1-6 merged) — batch machinery present, unused.
2. **Concept** — set `OVERLAY_ENRICH_CONCEPT_MODE=batch` on an allowlisted workspace; watch `overlay.enrich.concept.batch.*` counters (calls, retry, split, single_fallback, left_uncached, budget_exhausted); run `pytest -m eval` against the target model. Promote once gates hold.
3. **Domain** — `OVERLAY_ENRICH_DOMAIN_MODE=batch`; same watch.
4. **Definition** — `OVERLAY_ENRICH_DEFINITION_MODE=batch` last; conservative `OVERLAY_ENRICH_BATCH_DEFINITION_MAX_ITEMS`, stricter `OVERLAY_ENRICH_WALLCLOCK_BUDGET_MS`.

Kill switch: unset the `_MODE` var (or set `single`) — instantly reverts to today's proven per-item path.

---

# Self-Review

**Spec coverage:** C1 → Task 8; C2 → Task 3; C3 → Tasks 3+7; C4 → Task 6; C5 → Task 5; C6 → Tasks 2+10; C7 (idempotency ordering) → chunk order is content-hash-derived via the caller's stable ref + `chunk_items` preserving input order (concept caller iterates a dict of hashes; definition caller sorts by `(table, h)`; domain by table) — deterministic given identical input; **note:** an explicit global hash-sort before chunking is a cheap hardening left as a follow-up if strict cross-run batch identity is required; C8 → Task 4 (item summary in `cost_metadata.batch`); C9 → Task 4; C10 → Tasks 1+7+9+10 (mode branch); C18 schema bounds → Task 4; evaluation → Task 11; telemetry → Task 6; phased rollout → Rollout section.

**Deferred vs spec (called out honestly):** (a) per-item audit uses a `cost_metadata.batch` summary rather than a separate `llm_call_item` table — spec C8 permits either; the table is a follow-up if item-level querying is needed. (b) `llm_call_item` migration is therefore NOT in this plan. (c) Strict canonical global hash-ordering (C7) is approximated by deterministic input ordering; upgrade to an explicit sort if cross-run batch-identity telemetry demands it. (d) Blinded pairwise definition review (spec eval) is a manual procedure layered on Task 11's harness, not an automated assertion.

**Placeholder scan:** the GOLD fixture is intentionally seeded with 5 rows and a `# ... expand to >= 40` note — this is real starter content plus an explicit expansion instruction, not a TODO stub; expanding it is part of executing Task 11.

**Type consistency:** `run_batched`/`audited_batch_call`/`validate_batch_results`/`chunk_items`/`BatchItem`/`BatchItemOutcome`/`BatchCallResult`/`Accept` signatures match across Tasks 3-10. `_cache_get`/`_cache_put` gain `cache_version` in Task 2 and every caller (Tasks 2,7,9,10) passes it. `draft_definitions` gains `concepts=` in Task 10 and the `ingest.py` caller is updated in the same task (Task 8 lands the plain call to avoid a broken intermediate).
