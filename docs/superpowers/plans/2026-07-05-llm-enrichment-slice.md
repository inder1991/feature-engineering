# LLM Enrichment Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the first LLM enrichment — classify each column into a **controlled concept vocabulary** — reusing SP-2's `LLMClient` seam, **cached by content-hash** (so re-ingest never re-calls the LLM for unchanged columns), folded into the graph as a `concept`, and boosting search so a query like "money" surfaces `monetary_amount` columns.

**Architecture:** An `enrich_concepts(conn, rows, client)` step runs during ingest. For each column it computes a content-hash `(table|column|type|definition)`; a hit in `enrichment_concept` (a cache table) is reused; a miss calls `client.call(LLMRequest)` (a `FakeLLM` in tests — no network), validates the returned concept against a **fixed vocabulary** (unknown → `unclassified`), and caches it. `build_graph` reads the cache and writes each column node's `concept`, which is folded into the `search_doc` so concept terms are searchable and rank. Advisory only: a wrong concept degrades search, never a fact.

**Tech Stack:** Python 3.12, Postgres FTS, SP-2 `LLMClient`/`FakeLLM` seam (`featuregen.intake.llm`), `uv run pytest`. No new dependencies.

## Global Constraints

- **Reuse the SP-2 LLM seam, do not reinvent:** `from featuregen.intake.llm import LLMClient, LLMRequest, FakeLLM, FakeResponse`. Enrichment depends on the `LLMClient` **interface** (`.call(LLMRequest) -> LLMResult`); tests inject `FakeLLM(script={task: [FakeResponse(output=...)]})`. **No real provider in tests.**
- **Advisory, never load-bearing:** concept is enrichment — a wrong value = worse search, not a wrong fact. It is auto-applied (no human gate), and it never overrides a declared fact.
- **Controlled vocabulary only:** the LLM classifies *into* the fixed seed list; anything else → `unclassified` (never free-text — free concepts fragment the graph). 
- **Cache = incrementality + determinism:** enrichment is looked up by content-hash; the LLM is called only for new/changed columns. This is what keeps re-ingest cheap and replay-safe (a cached column reproduces its concept with no LLM). Full `ENRICHMENT_APPLIED` event-sourcing + `llm_call` audit-store trace (S4/L1) are named **follow-ons**, not this slice.
- **No raw data values to the LLM:** inputs are schema metadata only — `table`, `column`, `type`, declared `definition`. Never cell values.
- **Builds on merged graph+search** (`src/featuregen/overlay/upload/`): `build_graph`, `search`, `ingest_upload`, `CanonicalRow` (with `definition`). Reuse; do not fork.
- **Row access:** `conn.execute(...).fetchone()/fetchall()` returns **tuples**; use `dict_row` cursor for dict rows.
- **Migration numbering:** next is `0950`.
- **TDD, frequent commits.** `uv run pytest -q <file>` per task; real Postgres via `db`.

---

## File Structure

- `src/featuregen/overlay/upload/concepts.py` — the controlled vocabulary + `is_known_concept` (Task 1).
- `src/featuregen/db/migrations/0950_enrichment_concept.sql` — the cache table (Task 2).
- `src/featuregen/overlay/upload/enrich.py` — `enrich_concepts(conn, rows, client)` (Task 3).
- `src/featuregen/overlay/upload/graph.py` — MODIFY: read cache, write `concept`, fold into `search_doc` (Task 4).
- `src/featuregen/db/migrations/0951_graph_node_concept.sql` — add `concept` column to `graph_node` (Task 4).
- `src/featuregen/overlay/upload/ingest.py` — MODIFY: run `enrich_concepts` before `build_graph`; accept an optional `client` (Task 5).
- `src/featuregen/overlay/upload/search.py` — MODIFY: surface `concept` on `SearchHit` (Task 5).
- Tests: `tests/featuregen/overlay/upload/test_concepts.py`, `test_enrich.py`, and edits to `test_search.py`.

---

### Task 1: Controlled concept vocabulary

**Files:**
- Create: `src/featuregen/overlay/upload/concepts.py`
- Test: `tests/featuregen/overlay/upload/test_concepts.py`

**Interfaces:**
- Produces: `CONCEPTS: frozenset[str]`, `UNCLASSIFIED = "unclassified"`, `is_known_concept(c: str) -> bool`, `humanize(c: str) -> str` (underscores → spaces, for search_doc).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_concepts.py
from featuregen.overlay.upload.concepts import CONCEPTS, UNCLASSIFIED, is_known_concept, humanize


def test_vocabulary_is_controlled():
    assert "monetary_amount" in CONCEPTS
    assert "account_identifier" in CONCEPTS
    assert UNCLASSIFIED not in CONCEPTS          # the fallback is not itself a concept
    assert is_known_concept("monetary_amount") is True
    assert is_known_concept("made_up_thing") is False


def test_humanize_for_search():
    assert humanize("monetary_amount") == "monetary amount"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_concepts.py`
Expected: FAIL — `ModuleNotFoundError: ...concepts`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/concepts.py
from __future__ import annotations

UNCLASSIFIED = "unclassified"

CONCEPTS: frozenset[str] = frozenset({
    "monetary_amount",
    "account_identifier",
    "customer_identifier",
    "as_of_date",
    "effective_date",
    "timestamp",
    "count",
    "rate_or_ratio",
    "category_code",
    "pii",
    "free_text",
})


def is_known_concept(c: str) -> bool:
    return c in CONCEPTS


def humanize(c: str) -> str:
    return c.replace("_", " ")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_concepts.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/concepts.py tests/featuregen/overlay/upload/test_concepts.py
git commit -m "feat(enrich): controlled concept vocabulary"
```

---

### Task 2: Enrichment cache table

**Files:**
- Create: `src/featuregen/db/migrations/0950_enrichment_concept.sql`
- Test: covered by Task 3's tests (schema exercised there).

**Interfaces:**
- Produces table `enrichment_concept(content_hash text PRIMARY KEY, concept text NOT NULL, created_at timestamptz DEFAULT now())`.

- [ ] **Step 1: Write the migration**

```sql
-- src/featuregen/db/migrations/0950_enrichment_concept.sql
-- LLM enrichment cache: a column's classified concept keyed by a content-hash of its identity +
-- declared metadata (table|column|type|definition). A cache hit is reused with NO LLM call, which
-- is what makes re-ingest cheap and replay-safe. Advisory (concept only degrades search, never a
-- fact). Full ENRICHMENT_APPLIED event-sourcing + llm_call audit trace are later increments.
CREATE TABLE IF NOT EXISTS enrichment_concept (
    content_hash text        PRIMARY KEY,
    concept      text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
```

- [ ] **Step 2: Verify it applies**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_ingest_slice.py`
Expected: PASS (existing DB test still green — the new migration applied cleanly).

- [ ] **Step 3: Commit**

```bash
git add src/featuregen/db/migrations/0950_enrichment_concept.sql
git commit -m "feat(enrich): enrichment_concept cache table (migration 0950)"
```

---

### Task 3: `enrich_concepts` — cache-first LLM classification

**Files:**
- Create: `src/featuregen/overlay/upload/enrich.py`
- Test: `tests/featuregen/overlay/upload/test_enrich.py`

**Interfaces:**
- Consumes: `CanonicalRow`, `CONCEPTS`/`UNCLASSIFIED`/`is_known_concept`, `LLMClient`/`LLMRequest` (interface), `FakeLLM`/`FakeResponse` (tests).
- Produces:
  - `content_hash(row: CanonicalRow) -> str` — sha256 of `table|column|type|definition`.
  - `enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]` — returns `{content_hash: concept}` for the rows; a cache hit skips the LLM, a miss calls `client.call(...)`, validates the concept against the vocabulary (unknown → `UNCLASSIFIED`), writes the cache, and does NOT call the LLM twice for the same hash within one run.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_enrich.py
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts

_TASK = "overlay.enrich.concept"


def test_classifies_and_caches(db):
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")]
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_amount"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "monetary_amount"
    # Cached: a second run with a client that would raise is never called.
    cached = enrich_concepts(db, rows, _NeverCalledLLM())
    assert cached[content_hash(rows[0])] == "monetary_amount"


def test_unknown_concept_falls_back_to_unclassified(db):
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "totally_made_up"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "unclassified"


class _NeverCalledLLM:
    def call(self, request):
        raise AssertionError("LLM must not be called on a cache hit")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_enrich.py`
Expected: FAIL — `ModuleNotFoundError: ...enrich`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/enrich.py
from __future__ import annotations

import hashlib

from featuregen.intake.llm import LLMClient, LLMRequest
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import UNCLASSIFIED, is_known_concept

_TASK = "overlay.enrich.concept"


def content_hash(row: CanonicalRow) -> str:
    raw = f"{row.table}|{row.column}|{row.type}|{row.definition}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached(conn, hashes: list[str]) -> dict[str, str]:
    if not hashes:
        return {}
    rows = conn.execute(
        "SELECT content_hash, concept FROM enrichment_concept WHERE content_hash = ANY(%s)",
        (hashes,)).fetchall()
    return {r[0]: r[1] for r in rows}


def _classify(client: LLMClient, row: CanonicalRow) -> str:
    req = LLMRequest(
        task=_TASK,
        prompt_id="overlay_concept_v1",
        prompt_version=1,
        inputs={"table": row.table, "column": row.column, "type": row.type,
                "definition": row.definition},   # schema metadata only — no data values
        output_schema_id="overlay_concept",
        output_schema_version=1,
        generation_settings={"provider": "fake", "model": "test"},
    )
    concept = str(client.call(req).output.get("concept", "")).strip()
    return concept if is_known_concept(concept) else UNCLASSIFIED


def enrich_concepts(conn, rows: list[CanonicalRow], client: LLMClient) -> dict[str, str]:
    by_hash: dict[str, CanonicalRow] = {content_hash(r): r for r in rows}
    result = _cached(conn, list(by_hash))
    for h, row in by_hash.items():
        if h in result:
            continue
        concept = _classify(client, row)
        conn.execute(
            "INSERT INTO enrichment_concept (content_hash, concept) VALUES (%s, %s) "
            "ON CONFLICT (content_hash) DO NOTHING",
            (h, concept))
        result[h] = concept
    return result
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_enrich.py`
Expected: PASS (2 tests). If `FakeLLM` requires a `prompt_id`/`input_hash`-specific script rather than the task-key fallback, mirror an existing intake test's `FakeLLM` construction (grep `tests/featuregen/intake` for `FakeLLM(script=`); the task-key fallback form is documented in `intake/llm.py`'s `FakeLLM` docstring.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py tests/featuregen/overlay/upload/test_enrich.py
git commit -m "feat(enrich): cache-first concept classification via the LLMClient seam"
```

---

### Task 4: Fold `concept` into the graph + search_doc

**Files:**
- Create: `src/featuregen/db/migrations/0951_graph_node_concept.sql`
- Modify: `src/featuregen/overlay/upload/graph.py`
- Test: `tests/featuregen/overlay/upload/test_graph_build.py` (add a case)

**Interfaces:**
- `graph_node` gains `concept text NULL`.
- `build_graph(conn, catalog_source, rows, concepts: dict[str, str] | None = None)` — when `concepts` (a `{content_hash: concept}` map) is passed, each column node's `concept` is set and its humanized form is appended to `search_doc` (weight `C`). Default `None` keeps current behavior (no concept), so existing callers/tests are unaffected.

- [ ] **Step 1: Migration**

```sql
-- src/featuregen/db/migrations/0951_graph_node_concept.sql
-- Enrichment: the classified concept on a column node (advisory; drives the search sem signal).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS concept text NULL;
```

- [ ] **Step 2: Write the failing test (append to test_graph_build.py)**

```python
from featuregen.overlay.upload.enrich import content_hash


def test_build_graph_writes_concept_into_node_and_search(db):
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric")]
    concepts = {content_hash(rows[0]): "monetary_amount"}
    build_graph(db, "deposits", rows, concepts)
    concept = db.execute(
        "SELECT concept FROM graph_node WHERE object_ref='public.accounts.balance'").fetchone()[0]
    assert concept == "monetary_amount"
    # 'monetary' now matches the node via the folded concept text.
    hit = db.execute(
        "SELECT count(*) FROM graph_node WHERE object_ref='public.accounts.balance' "
        "AND search_doc @@ plainto_tsquery('english','monetary')").fetchone()[0]
    assert hit == 1
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: FAIL — `build_graph()` takes no `concepts` arg / `concept` column missing.

- [ ] **Step 4: Implement — extend `build_graph`**

In `graph.py`, import the humanizer and content-hash, extend the signature and the column insert. Replace the column-loop body:

```python
from featuregen.overlay.upload.concepts import humanize
from featuregen.overlay.upload.enrich import content_hash

# ... _SEARCH_DOC gains a 4th weighted term (concept, weight C):
_SEARCH_DOC = (
    "setweight(to_tsvector('english', coalesce(%s, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C')"
)


def build_graph(conn, catalog_source, rows, concepts=None):
    concepts = concepts or {}
    conn.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (catalog_source,))
    conn.execute("DELETE FROM graph_node WHERE catalog_source = %s", (catalog_source,))

    for table in {r.table for r in rows}:
        t_ref = _table_ref(table)
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, search_doc) "
            f"VALUES (%s, %s, 'table', %s, NULL, NULL, NULL, false, false, NULL, {_SEARCH_DOC})",
            (catalog_source, t_ref, table, table, "", table, ""))

    for r in rows:
        c_ref = _column_ref(r.table, r.column)
        concept = concepts.get(content_hash(r))
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, concept, search_doc) "
            f"VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, %s, {_SEARCH_DOC})",
            (catalog_source, c_ref, r.table, r.column, r.type, r.definition or None,
             r.is_grain, r.as_of, concept,
             r.column, r.definition, r.table, humanize(concept) if concept else ""))
        conn.execute(
            "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
            "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING",
            (catalog_source, _table_ref(r.table), c_ref))
```

Note: the table-node insert now passes an extra `""` for the 4th `search_doc` term (concept, empty for tables); keep the VALUES param order aligned with `_SEARCH_DOC`.

- [ ] **Step 5: Run + commit**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: PASS.

```bash
git add src/featuregen/db/migrations/0951_graph_node_concept.sql src/featuregen/overlay/upload/graph.py tests/featuregen/overlay/upload/test_graph_build.py
git commit -m "feat(enrich): fold concept into graph node + search_doc"
```

---

### Task 5: Wire enrichment into ingest + surface concept in search

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py` (accept optional `client`, run `enrich_concepts`, pass to `build_graph`)
- Modify: `src/featuregen/overlay/upload/search.py` (add `concept` to `SearchHit`)
- Test: `tests/featuregen/overlay/upload/test_search.py` (add an enrichment case)

**Interfaces:**
- `ingest_upload(conn, catalog_source, rows, *, actor, now=None, client: LLMClient | None = None)` — when `client` is provided, run `enrich_concepts(conn, vr.good, client)` and pass the map to `build_graph`; when `None`, behave exactly as today (no enrichment). Backward compatible.
- `SearchHit` gains `concept: str | None`; `search`'s SELECT returns `n.concept`.

- [ ] **Step 1: Write the failing test (append to test_search.py)**

```python
from featuregen.intake.llm import FakeLLM, FakeResponse


def test_search_uses_llm_concept(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]  # cryptic name, no definition
    client = FakeLLM(script={"overlay.enrich.concept":
                             FakeResponse(output={"concept": "monetary_amount"})})
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now, client=client).status == "ingested"
    # 'monetary' finds the cryptic 'bal' column only via its LLM-assigned concept.
    hits = search(db, "monetary", now=now)
    assert any(h.column == "bal" for h in hits)
    assert next(h for h in hits if h.column == "bal").concept == "monetary_amount"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_search.py`
Expected: FAIL — `ingest_upload()` has no `client` kwarg / `SearchHit` has no `concept`.

- [ ] **Step 3: Implement**

In `ingest.py`: add the import and thread `client`:

```python
from featuregen.overlay.upload.enrich import enrich_concepts
```

Change the signature to `def ingest_upload(conn, catalog_source, rows, *, actor, now=None, client=None)`, and replace the final `build_graph` call:

```python
    concepts = enrich_concepts(conn, vr.good, client) if client is not None else None
    build_graph(conn, catalog_source, vr.good, concepts)
    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined))
```

In `search.py`: add `n.concept` to the SELECT (after `n.catalog_source,`), add `concept: str | None` to `SearchHit`, and map it:

```python
# in _SQL SELECT list, add:  n.concept,
# in SearchHit dataclass, add:  concept: str | None
# in the comprehension, add:  concept=r["concept"],
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_search.py`
Expected: PASS.

- [ ] **Step 5: Full package + commit**

Run: `uv run pytest -q tests/featuregen/overlay/upload/`
Expected: PASS (all enrichment + graph + search + slice tests together).

```bash
git add src/featuregen/overlay/upload/ingest.py src/featuregen/overlay/upload/search.py tests/featuregen/overlay/upload/test_search.py
git commit -m "feat(enrich): wire concept enrichment into ingest + surface it in search"
```

---

## Self-Review

**Spec coverage (enrichment portion):**
- LLM enrichment reuses the SP-2 `LLMClient` seam (advisory, auto-applied): Tasks 3, 5. ✅
- Controlled concept vocabulary (no free-text fragmentation, R2): Tasks 1, 3. ✅
- Incremental / cache-first (H2 — never re-LLM an unchanged column): Tasks 2, 3. ✅
- Concept feeds search ranking (the `sem` signal, folded into `search_doc`): Tasks 4, 5. ✅
- No raw data values to the LLM (metadata only): Task 3. ✅
- **Deferred, named:** domain classification + LLM definition-drafting (same machinery, more outputs), full `ENRICHMENT_APPLIED` event-sourcing (S4) + `llm_call` audit-store trace (L1), a separate query-classification `sem` term (this slice folds concept into `search_doc` instead), the human review of low-confidence concepts, and read-authz. All later increments — not gaps in this slice.

**Placeholder scan:** No TBD/TODO; every code step has complete code; tests have real assertions. ✅

**Type consistency:** `content_hash(row) -> str`, `enrich_concepts(conn, rows, client) -> dict[str,str]`, `build_graph(conn, source, rows, concepts=None)`, `ingest_upload(..., client=None)`, `SearchHit.concept`, `CONCEPTS`/`UNCLASSIFIED`/`is_known_concept`/`humanize` — names/signatures consistent across tasks. `graph_node.concept` added once (0951), used in `build_graph` + `search`. ✅

**Known risks to verify during execution:**
- **Task 3, FakeLLM scripting:** confirm the constructor task-key fallback (`FakeLLM(script={task: FakeResponse})`) resolves for a request whose `prompt_id` differs — the `intake/llm.py` `FakeLLM` docstring says the task-key fallback matches on `request.task` alone, which is what this slice relies on. If a test needs per-column distinct concepts, pass a **sequence** (`{task: [FakeResponse(...), FakeResponse(...)]}`) consumed in order.
- **Task 4, tsvector param order:** `_SEARCH_DOC` now has FOUR positional params (column, definition, table, concept); every INSERT's VALUES must supply them in that exact trailing order (the table-node insert passes `""` for concept).
- **`LLMResult.output` shape:** the real `.call()` returns `LLMResult` with `.output` a dict; `_classify` reads `output["concept"]`. Confirm against `intake/llm.py` `LLMResult` (grounded: `output: dict`).
