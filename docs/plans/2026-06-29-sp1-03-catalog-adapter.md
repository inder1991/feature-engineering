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
      def list_objects(self) -> Iterable[CatalogObject]: ...      # tables (oid) + columns (data_type)
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
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_postgres_catalog_reads_structure_and_returns_no_ml_facts -v` — Expected: FAIL (`ImportError: cannot import name 'PostgresCatalog' from 'featuregen.overlay.catalog'`).

- [ ] **Minimal implementation.** Add the imports and the `PostgresCatalog` class to `src/featuregen/overlay/catalog.py`. Imports near the top (alongside the existing ones):
  ```python
  from psycopg.rows import dict_row

  from featuregen.contracts.db import DbConn
  ```
  Class appended after `FixtureCatalog`:
  ```python
  class PostgresCatalog:
      """Reference ``CatalogAdapter`` over a live PostgreSQL connection.

      Structural metadata only: existence/columns/types from ``information_schema`` and the stable
      native object id (``oid``) from ``pg_catalog`` (for rename detection, design §8). It records
      NONE of the five ML fact types, so ``get_fact`` returns ``None`` for all of them and the
      overlay owns those facts. ``owner_of`` returns ``None`` (ownership is not recorded here).
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
              # Columns with their information_schema data types.
              cur.execute(
                  """
                  SELECT table_schema AS sch, table_name AS tbl,
                         column_name AS col, data_type AS dtype
                  FROM information_schema.columns
                  WHERE table_schema = ANY(%s)
                  ORDER BY table_schema, table_name, ordinal_position
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
                          native_oid=None,
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

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/overlay/test_catalog.py::test_postgres_catalog_reads_structure_and_returns_no_ml_facts -v` — Expected: PASS.

- [ ] **Run the whole catalog suite + lint.** `uv run pytest tests/featuregen/overlay/test_catalog.py -v && uv run ruff check src/featuregen/overlay/catalog.py` — Expected: both green (all catalog tests pass, no lint findings).

- [ ] **Commit.** `git add src/featuregen/overlay/catalog.py tests/featuregen/overlay/test_catalog.py && git commit -m "feat(overlay): PostgresCatalog over information_schema + pg_catalog oid"`
