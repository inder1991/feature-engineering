# Graph + Search Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the ingested catalog into a searchable graph — materialize table/column **nodes** and contains **edges** from the upload, then serve **ranked full-text search** (name + declared definition), boosted by graph signal (grain/as-of) and filtered by source freshness — deterministically, with no LLM.

**Architecture:** A `graph_node` + `graph_edge` pair of Postgres tables, rebuilt per source at the end of `ingest_upload` from the canonical rows (structure + declared metadata). Search is a single indexed Postgres full-text query (`tsvector` + `ts_rank_cd`) plus a graph-signal term and a hard freshness filter (join `overlay_drift_watermark`). No LLM: concept/domain/definition-drafting and the join edges (`approved_join`) are later increments; declared `definition`s (already a contract field) carry the "meaning" for now.

**Tech Stack:** Python 3.12, Postgres full-text search (`to_tsvector`/`plainto_tsquery`/`ts_rank_cd`, GIN index), `uv run pytest`. No new dependencies.

## Global Constraints

- **Builds on the merged slice** in `src/featuregen/overlay/upload/` (`CanonicalRow`, `ingest_upload`, `UploadCatalog`, `table_ref`). Reuse them; do not fork.
- **No LLM in this slice.** Search ranks over declared names + declared definitions only. Concept/domain/definition-drafting, `pgvector`, and relevance feedback are later increments.
- **Deterministic rebuild.** The graph for a source is fully rebuilt from the current upload's rows on each ingest (DELETE-then-insert per `catalog_source`) — a re-upload refreshes it; replay-safe because it derives from committed rows, invokes nothing external.
- **Freshness is a hard filter, not a rank weight** (mirrors `resolve_fact`): a source whose drift watermark is missing or older than `drift_freshness_sla` is **excluded** from results.
- **Row access convention:** `conn.execute(...).fetchone()/fetchall()` returns **tuples** (positional access); use `conn.cursor(row_factory=dict_row)` when dict rows are wanted. (Verified in the slice build.)
- **Migration numbering:** next file is `0945` (recent overlay series steps by 5: 0920, 0925, 0940). Migrations are plain numbered `.sql` in `src/featuregen/db/migrations/`, auto-applied in order (tests apply once per session).
- **TDD, frequent commits.** `uv run pytest -q <file>` per task; real Postgres via the `db` fixture.
- **Object identity** matches the slice: table `object_ref = f"public.{table}"`, column `f"public.{table}.{column}"`, `catalog_source = <source>`.

---

## File Structure

- `src/featuregen/db/migrations/0945_graph.sql` — `graph_node` + `graph_edge` tables + GIN index (Task 1).
- `src/featuregen/overlay/upload/canonical.py` — MODIFY: add `definition` field (Task 2).
- `src/featuregen/overlay/upload/csv_reader.py` — MODIFY: alias a `definition`/`description` column (Task 2).
- `src/featuregen/overlay/upload/graph.py` — `build_graph(conn, catalog_source, rows)` (Task 3).
- `src/featuregen/overlay/upload/ingest.py` — MODIFY: call `build_graph` (Task 4).
- `src/featuregen/overlay/upload/search.py` — `search(conn, query, *, now)` + `SearchHit` (Task 5).
- Tests under `tests/featuregen/overlay/upload/`: `test_graph_build.py`, `test_search.py` (+ edits to `test_canonical.py`, `test_csv_reader.py`).

---

### Task 1: Migration — graph node + edge tables

**Files:**
- Create: `src/featuregen/db/migrations/0945_graph.sql`
- Test: `tests/featuregen/overlay/upload/test_graph_build.py` (schema smoke test)

**Interfaces:**
- Produces tables `graph_node(catalog_source, object_ref, kind, table_name, column_name, data_type, definition, is_grain, is_as_of, search_doc tsvector)` PK `(catalog_source, object_ref)`, GIN index on `search_doc`; `graph_edge(catalog_source, kind, from_ref, to_ref)` PK `(catalog_source, kind, from_ref, to_ref)`.

- [ ] **Step 1: Write the failing schema smoke test**

```python
# tests/featuregen/overlay/upload/test_graph_build.py
def test_graph_tables_exist(db):
    # Both tables were created by the migration; a trivial count proves they exist.
    assert db.execute("SELECT count(*) FROM graph_node").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM graph_edge").fetchone()[0] == 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: FAIL — `relation "graph_node" does not exist`.

- [ ] **Step 3: Write the migration**

```sql
-- src/featuregen/db/migrations/0945_graph.sql
-- Graph + search slice: table/column nodes and contains edges, rebuilt per catalog_source at the
-- end of ingest from the canonical rows. search_doc is a weighted tsvector (column name > definition
-- > table) driving ranked full-text search. Deterministic; no LLM. Join edges (approved_join) and
-- concept/domain enrichment are later increments.
CREATE TABLE IF NOT EXISTS graph_node (
    catalog_source text     NOT NULL,
    object_ref     text     NOT NULL,
    kind           text     NOT NULL,           -- 'table' | 'column'
    table_name     text     NOT NULL,
    column_name    text     NULL,
    data_type      text     NULL,
    definition     text     NULL,
    is_grain       boolean  NOT NULL DEFAULT false,
    is_as_of       boolean  NOT NULL DEFAULT false,
    search_doc     tsvector NULL,
    PRIMARY KEY (catalog_source, object_ref)
);
CREATE INDEX IF NOT EXISTS graph_node_search_idx ON graph_node USING GIN (search_doc);

CREATE TABLE IF NOT EXISTS graph_edge (
    catalog_source text NOT NULL,
    kind           text NOT NULL,               -- 'contains'
    from_ref       text NOT NULL,
    to_ref         text NOT NULL,
    PRIMARY KEY (catalog_source, kind, from_ref, to_ref)
);
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: PASS (migration auto-applied; both counts are 0).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/db/migrations/0945_graph.sql tests/featuregen/overlay/upload/test_graph_build.py
git commit -m "feat(graph): graph_node + graph_edge tables with FTS index (migration 0945)"
```

---

### Task 2: Carry declared `definition` through the row + CSV reader

**Files:**
- Modify: `src/featuregen/overlay/upload/canonical.py` (add field)
- Modify: `src/featuregen/overlay/upload/csv_reader.py` (alias + read)
- Test: `tests/featuregen/overlay/upload/test_canonical.py`, `tests/featuregen/overlay/upload/test_csv_reader.py` (add cases)

**Interfaces:**
- `CanonicalRow` gains `definition: str = ""` (optional, after `as_of`). Existing positional construction is unaffected (new field is last with a default).
- `read_csv_rows` maps a `definition`/`description`/`comment`/`notes` header into `definition`.

- [ ] **Step 1: Write the failing tests (append)**

```python
# append to tests/featuregen/overlay/upload/test_canonical.py
def test_row_carries_definition():
    from featuregen.overlay.upload.canonical import CanonicalRow
    r = CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")
    assert r.definition == "ledger balance"
```

```python
# append to tests/featuregen/overlay/upload/test_csv_reader.py
def test_reads_definition_alias():
    text = "table,column,type,Description\naccounts,balance,numeric,Ledger balance\n"
    rows = read_csv_rows(text, source="deposits")
    assert rows[0].definition == "Ledger balance"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_canonical.py tests/featuregen/overlay/upload/test_csv_reader.py`
Expected: FAIL — `TypeError: ... unexpected keyword 'definition'` and missing definition.

- [ ] **Step 3: Implement**

In `canonical.py`, add the field to `CanonicalRow`:

```python
@dataclass(frozen=True, slots=True)
class CanonicalRow:
    source: str
    table: str
    column: str
    type: str
    is_grain: bool = False
    as_of: bool = False
    definition: str = ""
```

In `csv_reader.py`, add the alias and read it:

```python
_ALIASES = {
    "source": {"source", "system"},
    "table": {"table", "tablename"},
    "column": {"column", "columnname", "attribute"},
    "type": {"type", "datatype", "sqltype"},
    "is_grain": {"isgrain", "grain"},
    "as_of": {"asof", "asofcolumn"},
    "definition": {"definition", "description", "comment", "notes"},
}
```

and in the row build, add `definition=cell("definition")`:

```python
        rows.append(CanonicalRow(
            source=cell("source") or source,
            table=cell("table"), column=cell("column"), type=cell("type"),
            is_grain=flag("is_grain"), as_of=flag("as_of"),
            definition=cell("definition")))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_canonical.py tests/featuregen/overlay/upload/test_csv_reader.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/canonical.py src/featuregen/overlay/upload/csv_reader.py tests/featuregen/overlay/upload/test_canonical.py tests/featuregen/overlay/upload/test_csv_reader.py
git commit -m "feat(graph): carry declared definition through canonical row + CSV reader"
```

---

### Task 3: `build_graph` — materialize nodes + edges + search_doc

**Files:**
- Create: `src/featuregen/overlay/upload/graph.py`
- Test: `tests/featuregen/overlay/upload/test_graph_build.py` (add cases)

**Interfaces:**
- Consumes: `CanonicalRow` (with `definition`).
- Produces: `build_graph(conn, catalog_source: str, rows: list[CanonicalRow]) -> None` — DELETE the source's `graph_node`/`graph_edge` rows, then insert one **table** node per distinct table, one **column** node per row (with `data_type`, `definition`, `is_grain`, `is_as_of`), a **contains** edge table→column, and the weighted `search_doc` per node.
- `search_doc` weighting: column name `A`, definition `B`, table name `C` (a name hit outranks a definition hit outranks a table hit).

- [ ] **Step 1: Write the failing tests (append to test_graph_build.py)**

```python
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.graph import build_graph


def test_build_graph_materializes_nodes_edges(db):
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance"),
    ]
    build_graph(db, "deposits", rows)

    n = db.execute("SELECT count(*) FROM graph_node WHERE catalog_source='deposits'").fetchone()[0]
    assert n == 4  # 1 table + 3 columns
    kind = db.execute("SELECT kind FROM graph_node WHERE object_ref='public.accounts'").fetchone()[0]
    assert kind == "table"
    grain = db.execute(
        "SELECT is_grain FROM graph_node WHERE object_ref='public.accounts.id'").fetchone()[0]
    assert grain is True
    edges = db.execute(
        "SELECT count(*) FROM graph_edge WHERE catalog_source='deposits' AND kind='contains'"
    ).fetchone()[0]
    assert edges == 3


def test_build_graph_is_idempotent_rebuild(db):
    rows_v1 = [CanonicalRow("deposits", "accounts", "id", "integer"),
               CanonicalRow("deposits", "accounts", "old_col", "text")]
    build_graph(db, "deposits", rows_v1)
    rows_v2 = [CanonicalRow("deposits", "accounts", "id", "integer")]
    build_graph(db, "deposits", rows_v2)  # old_col dropped
    refs = {r[0] for r in db.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source='deposits'").fetchall()}
    assert "public.accounts.old_col" not in refs
    assert "public.accounts.id" in refs
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: FAIL — `ModuleNotFoundError: ...graph`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/graph.py
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow

_SCHEMA = "public"

# Weighted tsvector: column name (A) > definition (B) > table (C).
_SEARCH_DOC = (
    "setweight(to_tsvector('english', coalesce(%s, '')), 'A') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'B') || "
    "setweight(to_tsvector('english', coalesce(%s, '')), 'C')"
)


def _table_ref(table: str) -> str:
    return f"{_SCHEMA}.{table}"


def _column_ref(table: str, column: str) -> str:
    return f"{_SCHEMA}.{table}.{column}"


def build_graph(conn, catalog_source: str, rows: list[CanonicalRow]) -> None:
    conn.execute("DELETE FROM graph_edge WHERE catalog_source = %s", (catalog_source,))
    conn.execute("DELETE FROM graph_node WHERE catalog_source = %s", (catalog_source,))

    tables: set[str] = set()
    for r in rows:
        tables.add(r.table)

    for table in tables:
        t_ref = _table_ref(table)
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, search_doc) "
            f"VALUES (%s, %s, 'table', %s, NULL, NULL, NULL, false, false, {_SEARCH_DOC})",
            (catalog_source, t_ref, table, table, "", table))

    for r in rows:
        c_ref = _column_ref(r.table, r.column)
        conn.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "data_type, definition, is_grain, is_as_of, search_doc) "
            f"VALUES (%s, %s, 'column', %s, %s, %s, %s, %s, %s, {_SEARCH_DOC})",
            (catalog_source, c_ref, r.table, r.column, r.type, r.definition or None,
             r.is_grain, r.as_of, r.column, r.definition, r.table))
        conn.execute(
            "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref) "
            "VALUES (%s, 'contains', %s, %s) ON CONFLICT DO NOTHING",
            (catalog_source, _table_ref(r.table), c_ref))
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_graph_build.py`
Expected: PASS (3 tests: schema smoke + 2 build tests).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/graph.py tests/featuregen/overlay/upload/test_graph_build.py
git commit -m "feat(graph): build_graph materializes nodes/edges + weighted search_doc per source"
```

---

### Task 4: Wire `build_graph` into `ingest_upload`

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py`
- Test: `tests/featuregen/overlay/upload/test_ingest_slice.py` (add an assertion)

**Interfaces:**
- After the drift step in `ingest_upload`, call `build_graph(conn, catalog_source, vr.good)` so a successful ingest leaves the graph current. Held/rejected uploads do NOT build the graph (return before it).

- [ ] **Step 1: Add a failing assertion to the existing proving test**

Append to `test_slice_ingest_serve_drift_and_brake` (after `res1` is asserted "ingested"):

```python
    # The graph is materialized on ingest.
    node_count = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source='deposits'").fetchone()[0]
    assert node_count > 0
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_ingest_slice.py`
Expected: FAIL — `assert 0 > 0` (graph not built yet).

- [ ] **Step 3: Implement — call build_graph at the end of ingest**

In `ingest.py`, add the import and the call. Add near the other upload imports:

```python
from featuregen.overlay.upload.graph import build_graph
```

and in `ingest_upload`, immediately before `return IngestResult("ingested", ...)`:

```python
    build_graph(conn, catalog_source, vr.good)
    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined))
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_ingest_slice.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_ingest_slice.py
git commit -m "feat(graph): build the graph at the end of a successful ingest"
```

---

### Task 5: `search` — ranked full-text, graph-boosted, freshness-filtered

**Files:**
- Create: `src/featuregen/overlay/upload/search.py`
- Test: `tests/featuregen/overlay/upload/test_search.py`

**Interfaces:**
- Consumes: `graph_node`, `overlay_drift_watermark` (freshness), `ingest_upload` (to set up data).
- Produces:
  - `class SearchHit` dataclass: `object_ref: str, table: str, column: str | None, kind: str, data_type: str | None, definition: str | None, is_grain: bool, is_as_of: bool, catalog_source: str, score: float`.
  - `search(conn, query: str, *, now: datetime, fresh_within: timedelta = timedelta(hours=24), limit: int = 20) -> list[SearchHit]`.
- Ranking: `score = ts_rank_cd(search_doc, plainto_tsquery('english', query)) + graph_signal`, where `graph_signal = 0.5*is_grain + 0.3*is_as_of`. **Freshness is a hard filter:** exclude a `catalog_source` whose watermark is missing OR `now - last_completed_at > fresh_within`. Only rows matching `search_doc @@ plainto_tsquery` are returned, ordered by score desc, capped at `limit`.

- [ ] **Step 1: Write the failing end-to-end test**

```python
# tests/featuregen/overlay/upload/test_search.py
from datetime import datetime, timedelta, timezone

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.search import search


def _actor():
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal():
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))


def _ingest(db, now):
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow("deposits", "accounts", "balance", "numeric",
                     definition="customer ledger balance"),
    ]
    assert ingest_upload(db, "deposits", rows, actor=_actor(), now=now).status == "ingested"


def test_search_finds_by_name_and_definition(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)

    # 'balance' matches the column name.
    hits = search(db, "balance", now=now)
    assert any(h.object_ref == "public.accounts.balance" for h in hits)

    # 'customer' matches only the definition of balance.
    hits2 = search(db, "customer", now=now)
    assert any(h.column == "balance" for h in hits2)


def test_grain_column_outranks_plain_on_name(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)
    hits = search(db, "id", now=now)
    assert hits and hits[0].object_ref == "public.accounts.id"
    assert hits[0].is_grain is True


def test_stale_source_excluded(db):
    _seal()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    _ingest(db, now)
    # Query far in the future -> the source's watermark is older than the 24h SLA -> excluded.
    later = now + timedelta(days=3)
    assert search(db, "balance", now=later) == []

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_search.py`
Expected: FAIL — `ModuleNotFoundError: ...search`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/search.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.rows import dict_row

_SQL = """
SELECT n.object_ref, n.table_name, n.column_name, n.kind, n.data_type, n.definition,
       n.is_grain, n.is_as_of, n.catalog_source,
       ts_rank_cd(n.search_doc, plainto_tsquery('english', %(q)s))
         + (CASE WHEN n.is_grain THEN 0.5 ELSE 0 END)
         + (CASE WHEN n.is_as_of THEN 0.3 ELSE 0 END) AS score
FROM graph_node n
JOIN overlay_drift_watermark w ON w.catalog_source = n.catalog_source
WHERE n.search_doc @@ plainto_tsquery('english', %(q)s)
  AND w.last_completed_at >= %(cutoff)s
ORDER BY score DESC
LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class SearchHit:
    object_ref: str
    table: str
    column: str | None
    kind: str
    data_type: str | None
    definition: str | None
    is_grain: bool
    is_as_of: bool
    catalog_source: str
    score: float


def search(conn, query: str, *, now: datetime,
           fresh_within: timedelta = timedelta(hours=24), limit: int = 20) -> list[SearchHit]:
    cutoff = now - fresh_within
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SQL, {"q": query, "cutoff": cutoff, "limit": limit})
        rows = cur.fetchall()
    return [SearchHit(
        object_ref=r["object_ref"], table=r["table_name"], column=r["column_name"],
        kind=r["kind"], data_type=r["data_type"], definition=r["definition"],
        is_grain=r["is_grain"], is_as_of=r["is_as_of"], catalog_source=r["catalog_source"],
        score=float(r["score"])) for r in rows]
```

Note: freshness uses `overlay_drift_watermark.last_completed_at`, which `ingest_upload` advances via `detect_catalog_changes` (written at the ingest's `now`). The `JOIN` (not `LEFT JOIN`) means a source with no watermark is excluded — fail-closed by construction.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_search.py`
Expected: PASS (3 tests). If `test_grain_column_outranks_plain_on_name` returns the table node `public.accounts` ahead of the column (the table name "accounts" contains no "id", so it should not match), confirm the query `id` only matches the `id` column — adjust the test query only if the tokenizer folds unexpectedly; do not weaken the ranking.

- [ ] **Step 5: Run the full upload package + commit**

Run: `uv run pytest -q tests/featuregen/overlay/upload/`
Expected: PASS (all graph/search + slice tests together).

```bash
git add src/featuregen/overlay/upload/search.py tests/featuregen/overlay/upload/test_search.py
git commit -m "feat(graph): ranked full-text search (graph-boosted, freshness-filtered)"
```

---

## Self-Review

**Spec coverage (graph + search portions of the specs):**
- Graph nodes (table/column) + contains edges: Tasks 1, 3. ✅ (Join edges = later increment, no `approved_join` in the upload slice yet.)
- Declared definitions drive search: Task 2. ✅ (LLM definition-drafting = later increment.)
- Search ranking = full-text (`ts_rank_cd`, weighted `tsvector`) + graph signal (grain/as-of) + freshness hard-filter: Task 5. ✅ (Concept/domain `sem` term, homonym domain-scoping, `pgvector`, relevance feedback = later increments — no LLM in this slice.)
- Built as a rebuild over the current upload (deterministic, replay-safe): Tasks 3, 4. ✅
- Read-scope authz pre-filter (S3): **NOT** in this slice — there's no auth context wired yet; a documented follow-on (search must gate PII nodes by role before ranking). Recorded here as a known gap, not silently omitted.

**Placeholder scan:** No TBD/TODO; every code step has complete code; every test has real assertions. ✅

**Type consistency:** `build_graph(conn, catalog_source, rows)`, `search(conn, query, *, now, fresh_within, limit) -> list[SearchHit]`, `CanonicalRow.definition`, table/column `object_ref` строки (`public.{table}[.{column}]`) match the slice's identity convention. `graph_node`/`graph_edge` column names identical across the migration, `build_graph`, and `search`. ✅

**Known risks to verify during execution:**
- **Task 5, freshness:** `ingest_upload` writes the watermark at `now`; the `test_stale_source_excluded` test queries at `now + 3 days` so `last_completed_at (now) < cutoff (now+3d − 24h)` → excluded. If `detect_catalog_changes` did not write a watermark on the first ingest (e.g. it early-returned), the JOIN would exclude everything and `test_search_finds_by_name_and_definition` would fail — that surfaces a real wiring gap, not a test-tweak.
- **Task 3, tsvector params:** the `search_doc` expression takes three positional params (column/definition/table) appended after the column values in each INSERT — keep the parameter order aligned with the VALUES list exactly.
