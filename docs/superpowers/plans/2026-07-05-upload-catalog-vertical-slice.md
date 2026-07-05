# Upload-Catalog Vertical Slice Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the upload-driven catalog spine end-to-end — canonical rows → ingest as a `catalog_source` → serve facts via the kept `resolve_fact` → re-upload with a dropped column → the dependent fact goes STALE → a truncated file is stopped by the large-change brake — reusing the kept overlay core, touching no governance code.

**Architecture:** A new `overlay/upload/` package sits *in front of* the kept core. `UploadCatalog` implements the existing `CatalogAdapter` protocol over parsed canonical rows (drift source). `ingest_upload` validates rows, runs the brake, asserts facts as **`OVERLAY_FACT_PROPOSED`+`OVERLAY_FACT_CONFIRMED` pairs** (reusing the kept projection/`resolve_fact` unchanged — auto-active, no approval), runs the projection, then drives drift via the kept `detect_catalog_changes` extended with a **task-free stale** path. A minimal CSV reader turns a file into canonical rows for a true file→served slice.

**Tech Stack:** Python 3.12, Postgres (event-sourced backbone), `uv run pytest`, jsonschema. No new dependencies (CSV via stdlib `csv`; Excel/`openpyxl` is the *next* increment, not this slice).

## Global Constraints

- **Reuse the kept core unchanged** except the one surgical task-free-stale parameter (Task 3): `catalog.py` (`CatalogAdapter`, `CatalogObject`), `store.py` (`append_overlay_event`), `projection.py` (`OverlayProjection`), `resolve.py` (`resolve_fact`), `catalog_changes.py` (`detect_catalog_changes`), `identity.py` (`fact_key`, `CatalogObjectRef`), `facts.py` (event-type constants, `validate_fact_value`), `projections.runner` (`run_projection`). **Touch no governance code** (`authority.py`, `reverify_tasks.py`, `confirmation_commands.py`, `join_confirmation.py`, `proposal_commands.py`, profiler, `expiry.py`).
- **Event model for the slice:** reuse `facts.OVERLAY_FACT_PROPOSED` then `facts.OVERLAY_FACT_CONFIRMED` appended back-to-back (auto-active). The clean single `FACT_ASSERTED` event (spec E1) is deferred to the governance-retirement phase — NOT this slice.
- **Slice fact scope:** `grain` (from `is_grain`) and `availability_time` (from `as_of`) only. `approved_join`, `policy_tag`/`sensitivity`, composite joins, `additivity`, `unit`/`currency`, SCD, entities, LLM mapping/enrichment, graph/search are **out of this slice** (later increments).
- **Object identity:** `schema="public"` constant, `catalog_source=<source>`; `object_ref = f"public.{table}"` for tables, `f"public.{table}.{column}"` for columns. `native_oid=None` (no DB); DROP detection is by snapshot set-difference, which needs no oid.
- **Facts are table-level:** one `grain` fact and one `availability_time` fact per table, keyed on the **table** `CatalogObjectRef` (`object_kind="table"`, `column=None`).
- **Drift guard requires config:** `resolve_fact`'s drift guard only engages when `current_overlay_config()` succeeds, so ingest/tests must seal an `OverlayConfig` via `register_overlay_config(...)`.
- **TDD, frequent commits.** `uv run pytest -q <file>` per task. Real Postgres via the `db` fixture (writes rolled back per test).
- **New package:** all new code under `src/featuregen/overlay/upload/`; all new tests under `tests/featuregen/overlay/upload/`.

---

## File Structure

- `src/featuregen/overlay/upload/__init__.py` — package marker.
- `src/featuregen/overlay/upload/canonical.py` — `CanonicalRow` dataclass + `validate_rows` (Task 1).
- `src/featuregen/overlay/upload/upload_catalog.py` — `UploadCatalog` adapter (Task 2).
- `src/featuregen/overlay/upload/brake.py` — `large_change_brake` (Task 4).
- `src/featuregen/overlay/upload/ingest.py` — `ingest_upload` orchestrator + `IngestResult` (Task 5).
- `src/featuregen/overlay/upload/csv_reader.py` — `read_csv_rows` (Task 6).
- `src/featuregen/overlay/catalog_changes.py` — MODIFY: thread `open_reverify: bool = True` (Task 3).
- Tests: `tests/featuregen/overlay/upload/{__init__.py,test_canonical.py,test_upload_catalog.py,test_task_free_stale.py,test_brake.py,test_ingest_slice.py,test_csv_reader.py}`.

---

### Task 1: Canonical row model + validation

**Files:**
- Create: `src/featuregen/overlay/upload/__init__.py` (empty)
- Create: `src/featuregen/overlay/upload/canonical.py`
- Test: `tests/featuregen/overlay/upload/__init__.py` (empty), `tests/featuregen/overlay/upload/test_canonical.py`

**Interfaces:**
- Produces:
  - `CanonicalRow` dataclass: `source: str, table: str, column: str, type: str, is_grain: bool = False, as_of: bool = False`.
  - `class RowError` dataclass: `row_index: int, message: str`.
  - `class ValidationResult` dataclass: `good: list[CanonicalRow], quarantined: list[RowError], structural_error: str | None`.
  - `validate_rows(rows: list[CanonicalRow]) -> ValidationResult`.
- Rules: required fields non-empty = `source, table, column, type`; a row missing any required field is **quarantined** (per-row), not fatal. If **every** row is missing `source` OR the list is empty → `structural_error` set (whole-file reject). Duplicate `(source, table, column)` with the *same* `type` → dedup (keep one); with a *different* `type` → quarantine the later one.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_canonical.py
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows


def _row(**kw):
    base = dict(source="deposits", table="accounts", column="id", type="integer")
    base.update(kw)
    return CanonicalRow(**base)


def test_valid_rows_pass_through():
    rows = [_row(column="id", is_grain=True), _row(column="posted_at", type="timestamp", as_of=True)]
    result = validate_rows(rows)
    assert len(result.good) == 2
    assert result.quarantined == []
    assert result.structural_error is None


def test_missing_required_field_quarantines_that_row_only():
    rows = [_row(column="id"), _row(column="", type="text")]  # blank column
    result = validate_rows(rows)
    assert len(result.good) == 1
    assert len(result.quarantined) == 1
    assert result.quarantined[0].row_index == 1


def test_empty_upload_is_structural_error():
    result = validate_rows([])
    assert result.structural_error is not None
    assert result.good == []


def test_duplicate_same_type_dedups_conflicting_type_quarantines():
    rows = [_row(column="id", type="integer"), _row(column="id", type="integer"),
            _row(column="id", type="text")]
    result = validate_rows(rows)
    assert len(result.good) == 1               # deduped identical
    assert len(result.quarantined) == 1        # conflicting type
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_canonical.py`
Expected: FAIL — `ModuleNotFoundError: featuregen.overlay.upload.canonical`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/canonical.py
from __future__ import annotations

from dataclasses import dataclass, field

_REQUIRED = ("source", "table", "column", "type")


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    source: str
    table: str
    column: str
    type: str
    is_grain: bool = False
    as_of: bool = False


@dataclass(frozen=True, slots=True)
class RowError:
    row_index: int
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    good: list[CanonicalRow] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)
    structural_error: str | None = None


def validate_rows(rows: list[CanonicalRow]) -> ValidationResult:
    if not rows:
        return ValidationResult(structural_error="empty upload: no rows")
    if all(not r.source for r in rows):
        return ValidationResult(structural_error="no row has a source")

    good: list[CanonicalRow] = []
    quarantined: list[RowError] = []
    seen: dict[tuple[str, str, str], str] = {}  # (source,table,column) -> type

    for i, r in enumerate(rows):
        missing = [f for f in _REQUIRED if not getattr(r, f)]
        if missing:
            quarantined.append(RowError(i, f"missing required field(s): {', '.join(missing)}"))
            continue
        key = (r.source, r.table, r.column)
        if key in seen:
            if seen[key] == r.type:
                continue  # identical duplicate -> dedup
            quarantined.append(RowError(i, f"conflicting type for {key}: {seen[key]} vs {r.type}"))
            continue
        seen[key] = r.type
        good.append(r)

    return ValidationResult(good=good, quarantined=quarantined, structural_error=None)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_canonical.py`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/__init__.py src/featuregen/overlay/upload/canonical.py tests/featuregen/overlay/upload/__init__.py tests/featuregen/overlay/upload/test_canonical.py
git commit -m "feat(upload): canonical row model + per-row-quarantine validation"
```

---

### Task 2: `UploadCatalog` adapter

**Files:**
- Create: `src/featuregen/overlay/upload/upload_catalog.py`
- Test: `tests/featuregen/overlay/upload/test_upload_catalog.py`

**Interfaces:**
- Consumes: `CanonicalRow` (Task 1); `CatalogObject`, `CatalogObjectRef` from the kept core.
- Produces: `class UploadCatalog` implementing `CatalogAdapter`:
  - `__init__(self, catalog_source: str, rows: list[CanonicalRow])`
  - attribute `catalog_source: str`
  - `list_objects(self) -> list[CatalogObject]` — one `CatalogObject(object_kind="table")` per distinct table + one `("column")` per row.
  - `fingerprint(self) -> dict[str, CatalogObject]` — the same objects keyed by `object_ref`.
  - `get_fact(self, ref, fact_type, use_case=None) -> None` (facts live in the overlay read model).
  - `owner_of(self, ref) -> None` (no ownership).
  - Helper module function `table_ref(catalog_source, table) -> CatalogObjectRef`.
- `object_ref` strings: table `f"public.{table}"`, column `f"public.{table}.{column}"`. `CatalogObject.schema="public"`, `native_oid=None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_upload_catalog.py
from featuregen.overlay.catalog import CatalogObject
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref


def test_fingerprint_has_table_and_column_objects():
    rows = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True),
            CanonicalRow("deposits", "accounts", "posted_at", "timestamp", as_of=True)]
    cat = UploadCatalog("deposits", rows)
    fp = cat.fingerprint()
    assert cat.catalog_source == "deposits"
    assert "public.accounts" in fp
    assert "public.accounts.id" in fp
    assert "public.accounts.posted_at" in fp
    assert isinstance(fp["public.accounts"], CatalogObject)
    assert fp["public.accounts"].object_kind == "table"
    assert fp["public.accounts.id"].object_kind == "column"
    assert fp["public.accounts.id"].data_type == "integer"


def test_get_fact_and_owner_are_none():
    cat = UploadCatalog("deposits", [CanonicalRow("deposits", "accounts", "id", "integer")])
    ref = table_ref("deposits", "accounts")
    assert cat.get_fact(ref, "grain") is None
    assert cat.owner_of(ref) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_upload_catalog.py`
Expected: FAIL — `ModuleNotFoundError: ...upload_catalog`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/upload_catalog.py
from __future__ import annotations

from featuregen.overlay.catalog import CatalogObject
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.canonical import CanonicalRow

_SCHEMA = "public"


def table_ref(catalog_source: str, table: str) -> CatalogObjectRef:
    return CatalogObjectRef(catalog_source=catalog_source, object_kind="table",
                            schema=_SCHEMA, table=table, column=None)


def _table_object_ref(table: str) -> str:
    return f"{_SCHEMA}.{table}"


def _column_object_ref(table: str, column: str) -> str:
    return f"{_SCHEMA}.{table}.{column}"


class UploadCatalog:
    def __init__(self, catalog_source: str, rows: list[CanonicalRow]) -> None:
        self.catalog_source = catalog_source
        self._rows = rows

    def _objects(self) -> dict[str, CatalogObject]:
        objs: dict[str, CatalogObject] = {}
        for r in self._rows:
            t_ref = _table_object_ref(r.table)
            objs.setdefault(t_ref, CatalogObject(
                object_ref=t_ref, object_kind="table", schema=_SCHEMA,
                table=r.table, column=None, data_type=None, native_oid=None))
            c_ref = _column_object_ref(r.table, r.column)
            objs[c_ref] = CatalogObject(
                object_ref=c_ref, object_kind="column", schema=_SCHEMA,
                table=r.table, column=r.column, data_type=r.type, native_oid=None)
        return objs

    def list_objects(self):
        return list(self._objects().values())

    def fingerprint(self):
        return self._objects()

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_upload_catalog.py`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/upload_catalog.py tests/featuregen/overlay/upload/test_upload_catalog.py
git commit -m "feat(upload): UploadCatalog adapter over canonical rows (drift source)"
```

---

### Task 3: Task-free stale in `detect_catalog_changes`

**Files:**
- Modify: `src/featuregen/overlay/catalog_changes.py` (thread `open_reverify` through `detect_catalog_changes` → `_stale_dependents` → `_stale_one`)
- Test: `tests/featuregen/overlay/upload/test_task_free_stale.py`

**Interfaces:**
- Consumes: existing `detect_catalog_changes(conn, adapter, *, actor, now=None)`, `_stale_dependents`, `_stale_one`.
- Produces: `detect_catalog_changes(conn, adapter, *, actor, now=None, open_reverify: bool = True)`. When `open_reverify=False`, a stale appends `OVERLAY_FACT_STALED` but does **not** call `resolve_authority`/`open_reverify_task`. Default `True` preserves all existing behavior/callers.

**Context:** In `catalog_changes.py`, `_stale_one` (≈lines 172-212) appends `OVERLAY_FACT_STALED` then unconditionally runs `resolve_authority(...)` + `open_reverify_task(...)`. Guard those two calls behind `if open_reverify:`. Thread the flag: `detect_catalog_changes` → `_stale_dependents(conn, adapter, change, *, actor, open_reverify)` → `_stale_one(conn, adapter, fact_key, *, change_ref, actor, open_reverify)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_task_free_stale.py
from datetime import datetime, timezone

from featuregen.identity import IdentityEnvelope
from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import OverlayProjection, current_fact
from featuregen.overlay.store import append_overlay_event
from featuregen.overlay.upload.upload_catalog import UploadCatalog
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.projections.runner import run_projection


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _assert_grain(db, source, table, cols):
    ref = CatalogObjectRef(catalog_source=source, object_kind="table",
                           schema="public", table=table, column=None)
    fk = fact_key(ref, "grain")
    value = {"columns": cols, "is_unique": True}
    draft = append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=_actor(), expected_version=0, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": "grain",
            "proposed_value": value, "proposal_fingerprint": "fp", "proposed_by": "upload"})
    append_overlay_event(db, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=_actor(), expected_version=1, payload={
            "value": value, "confirmers": [{"subject": "upload", "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})
    return fk


def test_stale_without_opening_task(db):
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    # Upload 1: table with a grain on 'id'; establish snapshot.
    rows1 = [CanonicalRow("deposits", "accounts", "id", "integer", is_grain=True)]
    fk = _assert_grain(db, "deposits", "accounts", ["id"])
    run_projection(db, OverlayProjection())
    detect_catalog_changes(db, UploadCatalog("deposits", rows1), actor=_actor(),
                           now=now, open_reverify=False)
    assert current_fact(db, fk)["status"] == "VERIFIED"

    # Upload 2: the 'id' column is gone -> drift should STALE the grain fact, no task.
    rows2 = [CanonicalRow("deposits", "accounts", "name", "text")]
    changes = detect_catalog_changes(db, UploadCatalog("deposits", rows2), actor=_actor(),
                                     now=now, open_reverify=False)
    run_projection(db, OverlayProjection())
    assert any(c.kind == "drop" for c in changes)
    assert current_fact(db, fk)["status"] == "STALE"
    # No reverify task row was opened.
    row = db.execute("SELECT count(*) AS n FROM overlay_reverify_task").fetchone()
    assert row["n"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_task_free_stale.py`
Expected: FAIL — `TypeError: detect_catalog_changes() got an unexpected keyword argument 'open_reverify'`.

- [ ] **Step 3: Implement — thread the flag**

In `src/featuregen/overlay/catalog_changes.py`:
1. Change `detect_catalog_changes(conn, adapter, *, actor, now=None)` signature to add `open_reverify: bool = True`, and pass it wherever it calls `_stale_dependents(...)`.
2. Change `_stale_dependents(conn, adapter, change, *, actor)` to `(..., *, actor, open_reverify: bool = True)` and pass `open_reverify` into each `_stale_one(...)` call.
3. Change `_stale_one(conn, adapter, fact_key, *, change_ref, actor)` to `(..., *, change_ref, actor, open_reverify: bool = True)`; wrap the existing `resolve_authority(...)` + `open_reverify_task(...)` block:

```python
        if open_reverify:
            authority = resolve_authority(conn, adapter, ref, state.fact_type)
            open_reverify_task(conn, fact_key=fact_key, fact_type=state.fact_type,
                               target_confirmed_event_id=state.confirmed_event_id,
                               authority=authority, actor=actor)
```

Leave the `OVERLAY_FACT_STALED` append (and its CAS/ConcurrencyError re-raise) exactly as-is, above that block.

- [ ] **Step 4: Run the new test AND the existing drift tests**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_task_free_stale.py tests/featuregen/overlay/test_catalog_changes.py tests/featuregen/overlay/test_source_qualified.py`
Expected: PASS — new test green; existing tests unchanged (default `open_reverify=True` preserves behavior).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/catalog_changes.py tests/featuregen/overlay/upload/test_task_free_stale.py
git commit -m "feat(upload): task-free stale path in detect_catalog_changes (open_reverify flag)"
```

---

### Task 4: Large-change brake

**Files:**
- Create: `src/featuregen/overlay/upload/brake.py`
- Test: `tests/featuregen/overlay/upload/test_brake.py`

**Interfaces:**
- Consumes: `UploadCatalog` (Task 2); the kept snapshot reader (query `overlay_catalog_object` directly by `catalog_source`).
- Produces:
  - `class BrakeResult` dataclass: `held: bool, reason: str | None, is_first_upload: bool`.
  - `large_change_brake(conn, catalog_source: str, upload: UploadCatalog, *, max_removed_frac: float = 0.30, min_removed_abs: int = 5, min_overlap_frac: float = 0.60) -> BrakeResult`.
- Rules: read prior object_refs from `overlay_catalog_object` for `catalog_source`. If prior is empty → `is_first_upload=True, held=False` (soft-gate; caller flags). Else compute `removed = prior - current`, `overlap = |prior ∩ current| / |prior|`. Hold if `len(removed) >= min_removed_abs AND len(removed)/len(prior) > max_removed_frac`, OR `overlap < min_overlap_frac`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_brake.py
from featuregen.overlay.upload.brake import large_change_brake, BrakeResult
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.upload_catalog import UploadCatalog


def _seed_snapshot(db, source, tables):
    for t in tables:
        db.execute(
            "INSERT INTO overlay_catalog_object (catalog_source, object_ref, native_oid, "
            "columns_fingerprint, type_fingerprint, updated_at) "
            "VALUES (%s, %s, NULL, NULL, NULL, now()) "
            "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
            (source, f"public.{t}"))


def _upload(source, tables):
    rows = [CanonicalRow(source, t, "id", "integer") for t in tables]
    return UploadCatalog(source, rows)


def test_first_upload_soft_gates(db):
    res = large_change_brake(db, "deposits", _upload("deposits", ["accounts"]))
    assert res.is_first_upload is True
    assert res.held is False


def test_normal_change_not_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", [f"t{i}" for i in range(9)]))
    assert res.held is False


def test_truncated_upload_is_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", ["t0", "t1"]))  # 80% removed
    assert res.held is True
    assert "remov" in res.reason.lower()


def test_wrong_source_low_overlap_is_held(db):
    _seed_snapshot(db, "deposits", [f"t{i}" for i in range(10)])
    res = large_change_brake(db, "deposits", _upload("deposits", [f"x{i}" for i in range(10)]))
    assert res.held is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_brake.py`
Expected: FAIL — `ModuleNotFoundError: ...brake`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/brake.py
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.upload_catalog import UploadCatalog


@dataclass(frozen=True, slots=True)
class BrakeResult:
    held: bool
    reason: str | None
    is_first_upload: bool


def _prior_refs(conn, catalog_source: str) -> set[str]:
    rows = conn.execute(
        "SELECT object_ref FROM overlay_catalog_object WHERE catalog_source = %s",
        (catalog_source,)).fetchall()
    return {r["object_ref"] for r in rows}


def large_change_brake(conn, catalog_source: str, upload: UploadCatalog, *,
                       max_removed_frac: float = 0.30, min_removed_abs: int = 5,
                       min_overlap_frac: float = 0.60) -> BrakeResult:
    prior = _prior_refs(conn, catalog_source)
    if not prior:
        return BrakeResult(held=False, reason=None, is_first_upload=True)

    current = set(upload.fingerprint())
    removed = prior - current
    overlap = len(prior & current) / len(prior)

    if len(removed) >= min_removed_abs and len(removed) / len(prior) > max_removed_frac:
        return BrakeResult(True, f"removes {len(removed)}/{len(prior)} objects "
                           f"(> {max_removed_frac:.0%})", False)
    if overlap < min_overlap_frac:
        return BrakeResult(True, f"overlap {overlap:.0%} < {min_overlap_frac:.0%} "
                           "(possible wrong source)", False)
    return BrakeResult(False, None, False)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_brake.py`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/brake.py tests/featuregen/overlay/upload/test_brake.py
git commit -m "feat(upload): large-change brake (absolute+relative removal + overlap)"
```

---

### Task 5: `ingest_upload` orchestrator + end-to-end proving test

**Files:**
- Create: `src/featuregen/overlay/upload/ingest.py`
- Test: `tests/featuregen/overlay/upload/test_ingest_slice.py`

**Interfaces:**
- Consumes: `validate_rows`/`CanonicalRow` (T1), `UploadCatalog`/`table_ref` (T2), `detect_catalog_changes(..., open_reverify=False)` (T3), `large_change_brake` (T4); kept `append_overlay_event`, `fact_key`, `facts.OVERLAY_FACT_*`, `OverlayProjection`, `run_projection`, `load_fact`.
- Produces:
  - `class IngestResult` dataclass: `status: str` (`"ingested" | "held" | "rejected"`), `reason: str | None`, `asserted: int`, `staled: int`, `quarantined: int`.
  - `ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *, actor, now=None) -> IngestResult`.
- Flow: validate → if `structural_error` return `rejected`. Build `UploadCatalog`. Brake → if `held` return `held` (assert nothing). For each table: assert a `grain` fact (columns with `is_grain`) and, if a column has `as_of`, an `availability_time` fact — each via PROPOSED+CONFIRMED, **skipping any fact whose stream already exists** (`load_fact` non-empty). `run_projection`. `detect_catalog_changes(..., open_reverify=False)` → count drops that staled. `run_projection` again. Return `ingested`.

- [ ] **Step 1: Write the failing end-to-end test (the slice's definition of done)**

```python
# tests/featuregen/overlay/upload/test_ingest_slice.py
from datetime import datetime, timezone

from featuregen.identity import IdentityEnvelope
from featuregen.overlay.config import OverlayConfig, register_overlay_config
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref


def _actor() -> IdentityEnvelope:
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _seal_config():
    register_overlay_config(OverlayConfig(
        ttl_default=None, ttl_min=None, ttl_max=None, ttl_jitter_fraction=0.0,
        renewal_grace=None, drift_scan_interval=None,
        drift_freshness_sla=__import__("datetime").timedelta(hours=24),
        profiler_require_restricted_role=False))


def test_slice_ingest_serve_drift_and_brake(db):
    _seal_config()
    now = datetime(2026, 7, 5, tzinfo=timezone.utc)
    source = "deposits"

    # Upload 1: accounts(id grain, posted_at as-of) + a second table so a later drop is <30%.
    rows1 = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "customers", "cust_id", "integer", is_grain=True),
    ]
    res1 = ingest_upload(db, source, rows1, actor=_actor(), now=now)
    assert res1.status == "ingested"

    cat1 = UploadCatalog(source, rows1)
    grain = resolve_fact(db, cat1, table_ref(source, "accounts"), "grain", now=now)
    assert grain.status == "VERIFIED"
    assert grain.value == {"columns": ["id"], "is_unique": True}
    avail = resolve_fact(db, cat1, table_ref(source, "accounts"), "availability_time", now=now)
    assert avail.status == "VERIFIED"

    # Upload 2: posted_at dropped -> availability_time fact STALE, grain still served.
    rows2 = [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "customers", "cust_id", "integer", is_grain=True),
    ]
    res2 = ingest_upload(db, source, rows2, actor=_actor(), now=now)
    assert res2.status == "ingested"
    assert res2.staled >= 1

    cat2 = UploadCatalog(source, rows2)
    avail2 = resolve_fact(db, cat2, table_ref(source, "accounts"), "availability_time", now=now)
    assert avail2.value is None                       # fail-closed
    assert avail2.status in ("STALE", "REVERIFY")
    grain2 = resolve_fact(db, cat2, table_ref(source, "accounts"), "grain", now=now)
    assert grain2.status == "VERIFIED"                # unaffected fact still served

    # Upload 3: truncated (only accounts.id) -> brake holds, nothing changes.
    rows3 = [CanonicalRow(source, "accounts", "id", "integer", is_grain=True)]
    res3 = ingest_upload(db, source, rows3, actor=_actor(), now=now)
    assert res3.status == "held"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_ingest_slice.py`
Expected: FAIL — `ModuleNotFoundError: ...ingest`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/ingest.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from featuregen.overlay import facts
from featuregen.overlay.catalog_changes import detect_catalog_changes
from featuregen.overlay.identity import fact_key, proposal_fingerprint
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.store import append_overlay_event, load_fact
from featuregen.overlay.upload.brake import large_change_brake
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.upload_catalog import UploadCatalog, table_ref
from featuregen.projections.runner import run_projection


@dataclass(frozen=True, slots=True)
class IngestResult:
    status: str            # "ingested" | "held" | "rejected"
    reason: str | None
    asserted: int
    staled: int
    quarantined: int


def _table_facts(source: str, rows: list[CanonicalRow]):
    """Yield (table, fact_type, value) for grain + availability_time facts."""
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    for table, trows in by_table.items():
        grain_cols = [r.column for r in trows if r.is_grain]
        if grain_cols:
            yield table, "grain", {"columns": grain_cols, "is_unique": True}
        as_of = next((r.column for r in trows if r.as_of), None)
        if as_of:
            yield table, "availability_time", {"column": as_of, "basis": "posted_at"}


def _assert_fact(conn, source: str, table: str, fact_type: str, value: dict, *, actor) -> bool:
    fk = fact_key(table_ref(source, table), fact_type)
    if load_fact(conn, fk):        # already asserted (slice: unchanged) -> skip (diff-append)
        return False
    draft = append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_PROPOSED,
        actor=actor, expected_version=0, payload={
            "catalog_object_ref": {"catalog_source": source, "object_kind": "table",
                                   "schema": "public", "table": table},
            "object_ref": f"public.{table}", "fact_type": fact_type,
            "proposed_value": value, "proposal_fingerprint": proposal_fingerprint(value),
            "proposed_by": actor.subject})
    append_overlay_event(conn, fact_key=fk, type=facts.OVERLAY_FACT_CONFIRMED,
        actor=actor, expected_version=1, payload={
            "value": value, "confirmers": [{"subject": actor.subject, "role": "data_owner"}],
            "expires_at": None, "confirms_event_id": draft.event_id})
    return True


def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None) -> IngestResult:
    vr = validate_rows(rows)
    if vr.structural_error:
        return IngestResult("rejected", vr.structural_error, 0, 0, len(vr.quarantined))

    upload = UploadCatalog(catalog_source, vr.good)
    brake = large_change_brake(conn, catalog_source, upload)
    if brake.held:
        return IngestResult("held", brake.reason, 0, 0, len(vr.quarantined))

    asserted = 0
    for table, fact_type, value in _table_facts(catalog_source, vr.good):
        if _assert_fact(conn, catalog_source, table, fact_type, value, actor=actor):
            asserted += 1

    run_projection(conn, OverlayProjection())
    changes = detect_catalog_changes(conn, upload, actor=actor, now=now, open_reverify=False)
    run_projection(conn, OverlayProjection())
    staled = sum(1 for c in changes if c.kind in ("drop", "type_change", "rename"))

    return IngestResult("ingested", None, asserted, staled, len(vr.quarantined))
```

- [ ] **Step 4: Run the proving test**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_ingest_slice.py`
Expected: PASS. If `availability_time` does not stale on the column drop, inspect whether `fact_dependencies` maps `availability_time.value["column"]` to `public.accounts.posted_at`; that dependency is what the drop stales. (Confirm via `SELECT * FROM overlay_fact_dependency` mid-test if debugging.)

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_ingest_slice.py
git commit -m "feat(upload): ingest_upload spine — validate/brake/assert/project/drift end-to-end"
```

---

### Task 6: Minimal CSV reader (file → canonical rows)

**Files:**
- Create: `src/featuregen/overlay/upload/csv_reader.py`
- Test: `tests/featuregen/overlay/upload/test_csv_reader.py`

**Interfaces:**
- Consumes: `CanonicalRow` (T1).
- Produces: `read_csv_rows(text: str, *, source: str) -> list[CanonicalRow]`.
- Header aliasing (case/space/underscore-insensitive): `table` ← {table, table_name}; `column` ← {column, column_name, attribute}; `type` ← {type, data_type, sql_type}; `is_grain` ← {is_grain, grain}; `as_of` ← {as_of, as_of_column, asof}. `source` comes from the `source` argument (a `source` column in the file overrides, if present). Boolean cells: `Y/yes/true/1` (case-insensitive) → True. Unknown columns ignored. Rows missing a recognized `column`/`type` still emit a `CanonicalRow` (validation quarantines them later — keep the reader dumb).

- [ ] **Step 1: Write the failing tests**

```python
# tests/featuregen/overlay/upload/test_csv_reader.py
from featuregen.overlay.upload.csv_reader import read_csv_rows


def test_reads_aliased_headers_and_booleans():
    text = (
        "Table Name,Attribute,SQL Type,Grain,As Of\n"
        "accounts,id,integer,Y,\n"
        "accounts,posted_at,timestamp,,yes\n")
    rows = read_csv_rows(text, source="deposits")
    assert len(rows) == 2
    assert rows[0].source == "deposits"
    assert rows[0].table == "accounts" and rows[0].column == "id"
    assert rows[0].type == "integer" and rows[0].is_grain is True and rows[0].as_of is False
    assert rows[1].as_of is True and rows[1].is_grain is False


def test_source_column_overrides_argument():
    text = "source,table,column,type\ncards,card_accounts,acct_id,integer\n"
    rows = read_csv_rows(text, source="fallback")
    assert rows[0].source == "cards"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_csv_reader.py`
Expected: FAIL — `ModuleNotFoundError: ...csv_reader`.

- [ ] **Step 3: Write the implementation**

```python
# src/featuregen/overlay/upload/csv_reader.py
from __future__ import annotations

import csv
import io

from featuregen.overlay.upload.canonical import CanonicalRow

_ALIASES = {
    "source": {"source", "system"},
    "table": {"table", "tablename"},
    "column": {"column", "columnname", "attribute"},
    "type": {"type", "datatype", "sqltype"},
    "is_grain": {"isgrain", "grain"},
    "as_of": {"asof", "asofcolumn"},
}
_TRUE = {"y", "yes", "true", "1"}


def _norm(h: str) -> str:
    return h.strip().lower().replace(" ", "").replace("_", "")


def _field_map(headers: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in headers:
        n = _norm(h)
        for field, variants in _ALIASES.items():
            if n in variants:
                out[field] = h
    return out


def read_csv_rows(text: str, *, source: str) -> list[CanonicalRow]:
    reader = csv.DictReader(io.StringIO(text))
    fmap = _field_map(reader.fieldnames or [])
    rows: list[CanonicalRow] = []
    for raw in reader:
        def cell(field: str) -> str:
            col = fmap.get(field)
            return (raw.get(col) or "").strip() if col else ""

        def flag(field: str) -> bool:
            return cell(field).lower() in _TRUE

        rows.append(CanonicalRow(
            source=cell("source") or source,
            table=cell("table"), column=cell("column"), type=cell("type"),
            is_grain=flag("is_grain"), as_of=flag("as_of")))
    return rows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest -q tests/featuregen/overlay/upload/test_csv_reader.py`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full new-package suite + commit**

Run: `uv run pytest -q tests/featuregen/overlay/upload/`
Expected: PASS (all tasks green together).

```bash
git add src/featuregen/overlay/upload/csv_reader.py tests/featuregen/overlay/upload/test_csv_reader.py
git commit -m "feat(upload): minimal CSV reader with header aliasing (file -> canonical rows)"
```

---

## Self-Review

**Spec coverage (slice scope of `2026-07-04-upload-mapping-enrichment-design.md` + pivot):**
- Read (CSV) → grid → canonical rows: Task 6 + Task 1. ✅ (Excel deferred — next increment.)
- Deterministic mapping (header aliasing): Task 6. ✅ (LLM mapping deferred.)
- Validate + graceful degradation (per-row quarantine, structural reject): Task 1. ✅
- Large-change brake (absolute+relative+overlap, first-upload soft-gate): Task 4. ✅
- Ingest → facts (auto-active, reuse projection) → drift (task-free stale) → serve (fail-closed): Task 5 + Task 3. ✅
- `UploadCatalog` adapter over the kept `CatalogAdapter` seam: Task 2. ✅
- Out of slice (documented in Global Constraints): approved_join/composite, policy_tag/sensitivity, additivity/unit/SCD/entity, LLM mapping+enrichment, graph/search, trace queries, read-authz, `FACT_ASSERTED` event, diff-append beyond skip-if-exists. These are later increments — not gaps in *this* plan.

**Placeholder scan:** No TBD/TODO; every code step has complete code; every test has real assertions. ✅

**Type consistency:** `CanonicalRow`, `UploadCatalog`/`table_ref`, `validate_rows`/`ValidationResult`, `large_change_brake`/`BrakeResult`, `ingest_upload`/`IngestResult`, `detect_catalog_changes(..., open_reverify=...)`, `read_csv_rows` — names/signatures used identically across tasks. Event payloads match the kept `OVERLAY_EVENT_SCHEMAS` (PROPOSED requires `catalog_object_ref, object_ref, fact_type, proposed_value, proposal_fingerprint, proposed_by`; CONFIRMED requires `value, confirmers, confirms_event_id`). ✅

**Known risk to verify during execution (Task 5, Step 4):** the drop-drives-stale of `availability_time` depends on `fact_dependencies` mapping the fact's `value["column"]` to the column `object_ref`. If it does not, Task 5 surfaces it as a failing assertion — treat as a real finding (the dependency derivation may need the availability_time column wired), not a test-tweak.
