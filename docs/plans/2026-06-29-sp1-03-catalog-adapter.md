# SP-1 — Phase 3 — Catalog adapter (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp1-00-overview.md](2026-06-29-sp1-00-overview.md) (authoritative).

---

This phase builds `src/featuregen/overlay/catalog.py`: the per-fact authority boundary between the enterprise catalog and the overlay. The catalog adapter supplies **structural** metadata (object existence, columns, types, stable native oid) via `list_objects`/`fingerprint`, and answers **ML fact** queries via `get_fact` only when the catalog genuinely records that fact authoritatively. `PostgresCatalog` (reference adapter) reads `information_schema` + `pg_catalog` and records **none** of the five ML fact types, so its `get_fact` returns `None` for all of them and the overlay owns those facts (design §3.3, §4). `FixtureCatalog` is the in-memory test double that can mark per-fact authoritativeness and set owners.

**Depends on Phase 2** for `featuregen.overlay.identity` (`CatalogObjectRef`, `display_object_ref`). This phase adds **no migrations** and appends **no events** — it is pure read-side adapter code. Tasks 3.1 (in-memory) and 3.2 (real DB) are independent and may be implemented in either order.

---

### Task 3.1: `CatalogObject` / `CatalogFact` / `CatalogAdapter` Protocol + `FixtureCatalog`

**Files:**
- Create `src/featuregen/overlay/catalog.py` — dataclasses `CatalogObject`, `CatalogFact`; `CatalogAdapter` Protocol; `FixtureCatalog`.
- Create `tests/featuregen/overlay/test_catalog.py` — FixtureCatalog tests.
- (If absent — Phase 2 normally creates it) `tests/featuregen/overlay/__init__.py` (empty) so the test package imports.

**Interfaces:**
- Consumes (from `featuregen.overlay.identity`, Phase 2): `CatalogObjectRef`, `display_object_ref(ref) -> str` (renders `schema.table[.column]`, e.g. `"core.transactions.posted_at"`).
- Produces:
  ```python
  @dataclass(frozen=True, slots=True)
  class CatalogObject:
      object_ref: str; object_kind: str; schema: str; table: str
      column: str | None; data_type: str | None; native_oid: str | None
  @dataclass(frozen=True, slots=True)
  class CatalogFact:
      value: object; authoritative: bool
  class CatalogAdapter(Protocol):
      def list_objects(self) -> Iterable[CatalogObject]: ...
      def get_fact(self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None) -> CatalogFact | None: ...
      def owner_of(self, ref: CatalogObjectRef) -> str | None: ...
      def fingerprint(self) -> Mapping[str, CatalogObject]: ...
  class FixtureCatalog:  # in-memory CatalogAdapter test double
      def __init__(self, catalog_source: str = "fixture") -> None: ...
      def add_object(self, obj: CatalogObject) -> None: ...
      def set_fact(self, ref, fact_type, value, *, authoritative, use_case=None) -> None: ...
      def set_owner(self, ref, owner: str) -> None: ...
  ```

**Note:** `FixtureCatalog` is in-memory, so its tests are pure unit tests and intentionally do **not** take the `db`/`conn` fixture. The real-DB fixture is exercised by Task 3.2.

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/overlay/test_catalog.py`:
  ```python
  from featuregen.overlay.catalog import CatalogFact, CatalogObject, FixtureCatalog
  from featuregen.overlay.identity import CatalogObjectRef


  def test_fixture_catalog_objects_facts_and_owner():
      cat = FixtureCatalog(catalog_source="pg:core")
      txn = CatalogObjectRef(
          catalog_source="pg:core", object_kind="table", schema="core", table="transactions"
      )
      posted = CatalogObjectRef(
          catalog_source="pg:core", object_kind="column",
          schema="core", table="transactions", column="posted_at",
      )
      cat.add_object(CatalogObject(
          object_ref="core.transactions", object_kind="table", schema="core",
          table="transactions", column=None, data_type=None, native_oid="16500",
      ))
      cat.add_object(CatalogObject(
          object_ref="core.transactions.posted_at", object_kind="column", schema="core",
          table="transactions", column="posted_at",
          data_type="timestamp with time zone", native_oid=None,
      ))

      # list_objects returns exactly what was added.
      objs = {o.object_ref: o for o in cat.list_objects()}
      assert set(objs) == {"core.transactions", "core.transactions.posted_at"}
      assert objs["core.transactions.posted_at"].data_type == "timestamp with time zone"

      # get_fact returns a CatalogFact carrying the per-fact authoritative flag.
      cat.set_fact(posted, "availability_time",
                   {"column": "posted_at", "basis": "posted_at"}, authoritative=True)
      cat.set_fact(txn, "grain", {"columns": ["id"], "is_unique": True}, authoritative=False)

      avail = cat.get_fact(posted, "availability_time")
      assert avail == CatalogFact(value={"column": "posted_at", "basis": "posted_at"},
                                  authoritative=True)
      assert cat.get_fact(txn, "grain").authoritative is False
      # use_case participates in fact identity: a policy_tag without the use_case is a miss.
      cat.set_fact(posted, "policy_tag", {"decision": "deny", "basis": "pii"},
                   authoritative=True, use_case="fraud_scoring")
      assert cat.get_fact(posted, "policy_tag", use_case="fraud_scoring").value == {
          "decision": "deny", "basis": "pii"}
      assert cat.get_fact(posted, "policy_tag") is None
      # unknown fact -> None.
      assert cat.get_fact(posted, "scd_effective_dating") is None

      # owner_of returns the recorded owner, else None.
      cat.set_owner(txn, "user:alice")
      assert cat.owner_of(txn) == "user:alice"
      assert cat.owner_of(posted) is None

      # fingerprint is object_ref -> CatalogObject for change detection.
      fp = cat.fingerprint()
      assert fp["core.transactions"].native_oid == "16500"
      assert set(fp) == {"core.transactions", "core.transactions.posted_at"}
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_fixture_catalog_objects_facts_and_owner -v` — Expected: FAIL (`ModuleNotFoundError: No module named 'featuregen.overlay.catalog'`).

- [ ] **Minimal implementation.** Create `src/featuregen/overlay/catalog.py`:
  ```python
  """Catalog adapter: structural metadata + per-fact authority boundary (SP-1 design §4).

  The catalog supplies object existence/columns/types/native-oid via ``list_objects`` and
  ``fingerprint`` (structural facts, NOT overlay fact types), and answers ML-fact queries via
  ``get_fact`` only when it genuinely records that fact authoritatively. Authoritativeness is a
  property of each returned ``CatalogFact`` (per object/fact/use_case), not a global set.
  """
  from __future__ import annotations

  from collections.abc import Iterable, Mapping
  from dataclasses import dataclass
  from typing import Protocol, runtime_checkable

  from featuregen.overlay.identity import CatalogObjectRef, display_object_ref


  @dataclass(frozen=True, slots=True)
  class CatalogObject:
      """A structural catalog object (table or column). Not an ML fact."""

      object_ref: str
      object_kind: str
      schema: str
      table: str
      column: str | None
      data_type: str | None
      native_oid: str | None


  @dataclass(frozen=True, slots=True)
  class CatalogFact:
      """An ML fact the catalog records, with its per-fact authority flag."""

      value: object
      authoritative: bool


  @runtime_checkable
  class CatalogAdapter(Protocol):
      def list_objects(self) -> Iterable[CatalogObject]: ...

      def get_fact(
          self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
      ) -> CatalogFact | None: ...

      def owner_of(self, ref: CatalogObjectRef) -> str | None: ...

      def fingerprint(self) -> Mapping[str, CatalogObject]: ...


  class FixtureCatalog:
      """In-memory ``CatalogAdapter`` test double.

      Facts and owners are keyed by ``display_object_ref(ref)`` so that lookups use the exact same
      normalized identity the real adapters expose via ``CatalogObject.object_ref``.
      """

      def __init__(self, catalog_source: str = "fixture") -> None:
          self._catalog_source = catalog_source
          self._objects: dict[str, CatalogObject] = {}
          self._facts: dict[tuple[str, str, str | None], CatalogFact] = {}
          self._owners: dict[str, str] = {}

      def add_object(self, obj: CatalogObject) -> None:
          self._objects[obj.object_ref] = obj

      def set_fact(
          self,
          ref: CatalogObjectRef,
          fact_type: str,
          value: object,
          *,
          authoritative: bool,
          use_case: str | None = None,
      ) -> None:
          key = (display_object_ref(ref), fact_type, use_case)
          self._facts[key] = CatalogFact(value=value, authoritative=authoritative)

      def set_owner(self, ref: CatalogObjectRef, owner: str) -> None:
          self._owners[display_object_ref(ref)] = owner

      def list_objects(self) -> Iterable[CatalogObject]:
          return tuple(self._objects.values())

      def get_fact(
          self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
      ) -> CatalogFact | None:
          return self._facts.get((display_object_ref(ref), fact_type, use_case))

      def owner_of(self, ref: CatalogObjectRef) -> str | None:
          return self._owners.get(display_object_ref(ref))

      def fingerprint(self) -> Mapping[str, CatalogObject]:
          return dict(self._objects)
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_fixture_catalog_objects_facts_and_owner -v` — Expected: PASS.

- [ ] **Commit.** `git add src/featuregen/overlay/catalog.py tests/featuregen/overlay/test_catalog.py tests/featuregen/overlay/__init__.py && git commit -m "feat(overlay): catalog adapter contract + FixtureCatalog"`

---

### Task 3.2: `PostgresCatalog` (reference adapter — `information_schema` + `pg_catalog` oid)

**Files:**
- Modify `src/featuregen/overlay/catalog.py` — add `PostgresCatalog` class (append after `FixtureCatalog`); add `from psycopg.rows import dict_row` and `from featuregen.contracts.db import DbConn` to the imports near the top of the file.
- Modify `tests/featuregen/overlay/test_catalog.py` — add the real-DB test (uses the `db` fixture from `tests/featuregen/conftest.py`).

**Interfaces:**
- Consumes: `featuregen.overlay.identity.CatalogObjectRef`, `display_object_ref`; `featuregen.contracts.db.DbConn` (= `psycopg.Connection[Any]`); a live connection from the `db`/`conn` fixture. Reads `pg_catalog.pg_class`/`pg_namespace` (stable `oid`) and `information_schema.columns` (column names + `data_type`).
- Produces:
  ```python
  class PostgresCatalog:  # CatalogAdapter
      def __init__(self, conn: DbConn, *, catalog_source: str = "pg:core",
                   schemas: tuple[str, ...] = ("public",)) -> None: ...
      def list_objects(self) -> Iterable[CatalogObject]: ...      # tables (oid) + columns (data_type, "<table_oid>:<attnum>" native_oid)
      def get_fact(self, ref, fact_type, use_case=None) -> None:  # always None: records no ML facts
      def owner_of(self, ref) -> None:                            # always None: ownership not recorded
      def fingerprint(self) -> Mapping[str, CatalogObject]: ...   # object_ref -> CatalogObject (§8)
  ```

Steps:

- [ ] **Write the failing test.** Add to `tests/featuregen/overlay/test_catalog.py` (import is added at the top):
  ```python
  from featuregen.overlay.catalog import PostgresCatalog  # add to existing imports at top


  def test_postgres_catalog_reads_structure_and_returns_no_ml_facts(db):
      # DDL runs inside the test transaction (rolled back on teardown); information_schema and
      # pg_catalog reflect the uncommitted table within the same transaction.
      with db.cursor() as cur:
          cur.execute(
              "CREATE TABLE overlay_cat_probe ("
              "  id bigint PRIMARY KEY,"
              "  posted_at timestamptz NOT NULL,"
              "  amount numeric"
              ")"
          )

      cat = PostgresCatalog(db, catalog_source="pg:core", schemas=("public",))
      objs = {o.object_ref: o for o in cat.list_objects()}

      # The table object exists, with a stable native oid from pg_catalog.
      assert "public.overlay_cat_probe" in objs
      table_obj = objs["public.overlay_cat_probe"]
      assert table_obj.object_kind == "table"
      assert table_obj.column is None
      assert table_obj.data_type is None
      assert table_obj.native_oid is not None and table_obj.native_oid.isdigit()

      # Columns exist, with correct information_schema data types.
      assert objs["public.overlay_cat_probe.id"].object_kind == "column"
      assert objs["public.overlay_cat_probe.id"].data_type == "bigint"
      assert objs["public.overlay_cat_probe.posted_at"].data_type == "timestamp with time zone"
      assert objs["public.overlay_cat_probe.amount"].data_type == "numeric"

      # A column's native_oid is the composite "<table_oid>:<attnum>" (overview pin 16) so the
      # column has a stable identity that survives a rename (see the rename test below). The
      # table_oid portion matches the owning table's native oid.
      posted_native = objs["public.overlay_cat_probe.posted_at"].native_oid
      assert posted_native is not None
      tbl_oid, sep, attnum = posted_native.partition(":")
      assert sep == ":" and tbl_oid == table_obj.native_oid and attnum.isdigit()

      # information_schema records NONE of the five ML fact types authoritatively.
      table_ref = CatalogObjectRef(
          catalog_source="pg:core", object_kind="table",
          schema="public", table="overlay_cat_probe",
      )
      col_ref = CatalogObjectRef(
          catalog_source="pg:core", object_kind="column",
          schema="public", table="overlay_cat_probe", column="posted_at",
      )
      for fact_type in (
          "availability_time", "grain", "scd_effective_dating", "approved_join", "policy_tag",
      ):
          assert cat.get_fact(table_ref, fact_type) is None
      assert cat.get_fact(col_ref, "policy_tag", use_case="fraud_scoring") is None

      # Ownership is not recorded by the structural catalog.
      assert cat.owner_of(table_ref) is None

      # fingerprint includes the table keyed by object_ref, carrying its oid (rename detection §8).
      fp = cat.fingerprint()
      assert "public.overlay_cat_probe" in fp
      assert fp["public.overlay_cat_probe"].native_oid == table_obj.native_oid


  def test_postgres_catalog_column_native_oid_is_stable_across_rename(db):
      # A column's native_oid is "<table_oid>:<attnum>" (overview pin 16). pg_attribute.attnum is
      # fixed at creation and not reused on rename, so the SAME column keeps the SAME native_oid
      # after a RENAME COLUMN — letting change detection (Phase 7) track the rename instead of
      # degrading it to a drop+add.
      with db.cursor() as cur:
          cur.execute(
              "CREATE TABLE overlay_rename_probe ("
              "  id bigint PRIMARY KEY,"
              "  posted_at timestamptz NOT NULL"
              ")"
          )

      cat = PostgresCatalog(db, catalog_source="pg:core", schemas=("public",))
      before = {o.object_ref: o for o in cat.list_objects()}
      native_before = before["public.overlay_rename_probe.posted_at"].native_oid
      assert native_before is not None and ":" in native_before

      with db.cursor() as cur:
          cur.execute(
              "ALTER TABLE overlay_rename_probe RENAME COLUMN posted_at TO event_time"
          )

      after = {o.object_ref: o for o in cat.list_objects()}
      # The new name appears; the old name is gone.
      assert "public.overlay_rename_probe.event_time" in after
      assert "public.overlay_rename_probe.posted_at" not in after
      # ...but the native_oid is unchanged: same column identity across the rename.
      assert after["public.overlay_rename_probe.event_time"].native_oid == native_before
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/overlay/test_catalog.py -k postgres_catalog -v` — Expected: FAIL (`ImportError: cannot import name 'PostgresCatalog' from 'featuregen.overlay.catalog'`).

- [ ] **Minimal implementation.** Add the imports and the `PostgresCatalog` class to `src/featuregen/overlay/catalog.py`. Imports near the top (alongside the existing ones):
  ```python
  from psycopg.rows import dict_row

  from featuregen.contracts.db import DbConn
  ```
  Class appended after `FixtureCatalog`:
  ```python
  class PostgresCatalog:
      """Reference ``CatalogAdapter`` over a live PostgreSQL connection.

      Structural metadata only: existence/columns/types from ``information_schema`` and stable
      native object ids from ``pg_catalog`` (for rename detection, design §8 / overview pin 16) —
      a table's ``native_oid`` is its ``pg_class.oid``; a column's ``native_oid`` is the composite
      ``"<table_oid>:<attnum>"`` (``pg_attribute.attnum`` is not reused on rename, so column
      identity survives renames). It records NONE of the five ML fact types, so ``get_fact``
      returns ``None`` for all of them and the overlay owns those facts. ``owner_of`` returns
      ``None`` (ownership is not recorded here).
      """

      def __init__(
          self,
          conn: DbConn,
          *,
          catalog_source: str = "pg:core",
          schemas: tuple[str, ...] = ("public",),
      ) -> None:
          self._conn = conn
          self._catalog_source = catalog_source
          self._schemas = schemas

      def list_objects(self) -> Iterable[CatalogObject]:
          schemas = list(self._schemas)
          objects: list[CatalogObject] = []
          with self._conn.cursor(row_factory=dict_row) as cur:
              # Tables/views with their stable native oid from pg_catalog.
              cur.execute(
                  """
                  SELECT n.nspname AS sch, c.relname AS tbl, c.oid::text AS oid
                  FROM pg_catalog.pg_class c
                  JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                  WHERE c.relkind IN ('r', 'p', 'v', 'm')
                    AND n.nspname = ANY(%s)
                  ORDER BY n.nspname, c.relname
                  """,
                  (schemas,),
              )
              for row in cur.fetchall():
                  ref = CatalogObjectRef(
                      catalog_source=self._catalog_source,
                      object_kind="table",
                      schema=row["sch"],
                      table=row["tbl"],
                  )
                  objects.append(
                      CatalogObject(
                          object_ref=display_object_ref(ref),
                          object_kind="table",
                          schema=row["sch"],
                          table=row["tbl"],
                          column=None,
                          data_type=None,
                          native_oid=row["oid"],
                      )
                  )
              # Columns with their information_schema data types, plus a stable composite
              # native_oid of "<table_oid>:<attnum>" (design §8, overview pin 16). attnum is
              # assigned at column creation and is NOT reused on rename, so the column's identity
              # survives a rename — letting change detection track renames instead of degrading
              # them to drop/add. The table oid (pg_class.oid) and attnum (pg_attribute.attnum)
              # come from pg_catalog; data_type stays from information_schema for stable spelling.
              cur.execute(
                  """
                  SELECT isc.table_schema AS sch, isc.table_name AS tbl,
                         isc.column_name AS col, isc.data_type AS dtype,
                         c.oid::text AS table_oid, a.attnum AS attnum
                  FROM information_schema.columns isc
                  JOIN pg_catalog.pg_namespace n ON n.nspname = isc.table_schema
                  JOIN pg_catalog.pg_class c
                    ON c.relname = isc.table_name AND c.relnamespace = n.oid
                  JOIN pg_catalog.pg_attribute a
                    ON a.attrelid = c.oid AND a.attname = isc.column_name
                  WHERE isc.table_schema = ANY(%s)
                  ORDER BY isc.table_schema, isc.table_name, isc.ordinal_position
                  """,
                  (schemas,),
              )
              for row in cur.fetchall():
                  ref = CatalogObjectRef(
                      catalog_source=self._catalog_source,
                      object_kind="column",
                      schema=row["sch"],
                      table=row["tbl"],
                      column=row["col"],
                  )
                  objects.append(
                      CatalogObject(
                          object_ref=display_object_ref(ref),
                          object_kind="column",
                          schema=row["sch"],
                          table=row["tbl"],
                          column=row["col"],
                          data_type=row["dtype"],
                          native_oid=f"{row['table_oid']}:{row['attnum']}",
                      )
                  )
          return objects

      def get_fact(
          self, ref: CatalogObjectRef, fact_type: str, use_case: str | None = None
      ) -> CatalogFact | None:
          # information_schema records none of the five ML fact types authoritatively.
          return None

      def owner_of(self, ref: CatalogObjectRef) -> str | None:
          return None

      def fingerprint(self) -> Mapping[str, CatalogObject]:
          return {obj.object_ref: obj for obj in self.list_objects()}
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/overlay/test_catalog.py -k postgres_catalog -v` — Expected: PASS (both the structure test and the rename-stability test).

- [ ] **Run the whole catalog suite + lint.** `uv run pytest tests/featuregen/overlay/test_catalog.py -v && uv run ruff check src/featuregen/overlay/catalog.py` — Expected: both green (all catalog tests pass, no lint findings).

- [ ] **Commit.** `git add src/featuregen/overlay/catalog.py tests/featuregen/overlay/test_catalog.py && git commit -m "feat(overlay): PostgresCatalog over information_schema + pg_catalog oid"`

---

### Task 3.3: Single-source module-level adapter accessor (`register_catalog_adapter` / `current_catalog_adapter`)

Both `propose_fact` (Phase 4) and `run_profiler` (Phases 4/6) need the same `CatalogAdapter` instance without threading it through every command's `args`. This task adds **one** process-wide accessor in `catalog.py` — `register_catalog_adapter(adapter)` / `current_catalog_adapter() -> CatalogAdapter` — mirroring the SP-0 `register_command_authorizer(...)` / `current_authorizer()` module-global pattern (`authz/authorizer.py`). This is the **single source** for the overlay catalog adapter: use these exact names — there is no `register_catalog_adapter`/`register_catalog_adapter` variant. Downstream phases (`propose_fact`, `run_profiler`) read the adapter via `current_catalog_adapter()` rather than holding their own copy. A test-only `_clear_catalog_adapter()` reset keeps the module global from leaking between tests.

**Files:**
- Modify `src/featuregen/overlay/catalog.py` — add the module-global accessor (append after `PostgresCatalog`).
- Modify `tests/featuregen/overlay/test_catalog.py` — add the accessor test.

**Interfaces:**
- Produces:
  ```python
  def register_catalog_adapter(adapter: CatalogAdapter) -> None: ...
  def current_catalog_adapter() -> CatalogAdapter: ...   # raises RuntimeError if none registered
  def _clear_catalog_adapter() -> None: ...              # test-only reset
  ```

**Note:** `current_catalog_adapter()` **fails closed** — it raises `RuntimeError` when no adapter has been registered, rather than returning `None`, so callers never silently resolve facts against a missing catalog. `register_overlay()` (Phase 4 `bootstrap.py`) is the production caller of `register_catalog_adapter(...)`; overlay command/profiler tests register a `FixtureCatalog` and call `_clear_catalog_adapter()` on teardown (the overlay conftest's reset fixture).

Steps:

- [ ] **Write the failing test.** Add to `tests/featuregen/overlay/test_catalog.py` (extend the existing top-of-file import of `featuregen.overlay.catalog` to include the three new symbols):
  ```python
  import pytest

  from featuregen.overlay.catalog import (  # add to existing imports at top
      current_catalog_adapter,
      register_catalog_adapter,
      _clear_catalog_adapter,
  )


  def test_register_and_current_catalog_adapter():
      _clear_catalog_adapter()
      # Fails closed before anything is registered.
      with pytest.raises(RuntimeError):
          current_catalog_adapter()

      cat = FixtureCatalog(catalog_source="pg:core")
      register_catalog_adapter(cat)
      # Same instance is returned (single source for propose_fact + run_profiler).
      assert current_catalog_adapter() is cat

      # A second registration replaces the first (last writer wins).
      other = FixtureCatalog(catalog_source="pg:other")
      register_catalog_adapter(other)
      assert current_catalog_adapter() is other

      # Test-only reset restores the fail-closed state.
      _clear_catalog_adapter()
      with pytest.raises(RuntimeError):
          current_catalog_adapter()
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_register_and_current_catalog_adapter -v` — Expected: FAIL (`ImportError: cannot import name 'current_catalog_adapter' from 'featuregen.overlay.catalog'`).

- [ ] **Minimal implementation.** Append the module-global accessor to `src/featuregen/overlay/catalog.py` (after `PostgresCatalog`):
  ```python
  # --- Single-source overlay catalog adapter accessor ---------------------------------------
  # The process-wide adapter shared by ``propose_fact`` and ``run_profiler``. Mirrors the SP-0
  # ``register_command_authorizer`` / ``current_authorizer`` module-global pattern. This is the ONLY
  # holder for the overlay catalog adapter — downstream phases call ``current_catalog_adapter()``.
  _CATALOG_ADAPTER: CatalogAdapter | None = None


  def register_catalog_adapter(adapter: CatalogAdapter) -> None:
      """Register the process-wide overlay ``CatalogAdapter`` (last writer wins)."""
      global _CATALOG_ADAPTER
      _CATALOG_ADAPTER = adapter


  def current_catalog_adapter() -> CatalogAdapter:
      """Return the registered overlay ``CatalogAdapter``.

      Fails closed: raises ``RuntimeError`` if no adapter has been registered, so callers never
      resolve facts against a missing catalog.
      """
      if _CATALOG_ADAPTER is None:
          raise RuntimeError(
              "no catalog adapter registered; call register_catalog_adapter(...) "
              "(register_overlay() does this in production)"
          )
      return _CATALOG_ADAPTER


  def _clear_catalog_adapter() -> None:
      """Test-only reset of the module-global adapter (call from the overlay conftest)."""
      global _CATALOG_ADAPTER
      _CATALOG_ADAPTER = None
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_register_and_current_catalog_adapter -v` — Expected: PASS.

- [ ] **Run the whole catalog suite + lint.** `uv run pytest tests/featuregen/overlay/test_catalog.py -v && uv run ruff check src/featuregen/overlay/catalog.py` — Expected: both green. (`ruff` may flag the `_clear_catalog_adapter` import as unused in the test if you reorder imports — keep it used by the test body as written.)

- [ ] **Commit.** `git add src/featuregen/overlay/catalog.py tests/featuregen/overlay/test_catalog.py && git commit -m "feat(overlay): single-source catalog adapter accessor (register/current)"`
