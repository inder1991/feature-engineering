# Phase 3B.3.0 — Bridge Freshness Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Make a governed cross-catalog entity bridge participate in the existing drift/freshness machinery — its TWO catalog endpoints land in `overlay_fact_dependency` so a change to either bridged catalog stales the bridge. This is the prerequisite Phase 3B.2B deferred to 3B.3; it does NOT enumerate binding plans.

**Architecture:** A bridge fact is two-source, so 3B.2B skipped it entirely in `OverlayProjection.apply` (the single-`catalog_source` `overlay_proposal`/`overlay_fact_state` read models can't model it). This plan keeps skipping those two read models but wires the ONE read model that IS two-source-capable — `overlay_fact_dependency` — by (1) adding an `entity_bridge` branch to `fact_dependencies` that returns both endpoints under their own sources, and (2) running dependency-index-only maintenance for bridge PROPOSED events. Bridge dependencies are immutable post-propose, so CONFIRMED/EXPIRED/… stay no-ops. Once the two endpoints are indexed, the existing `detect_catalog_changes → dependents_of → STALED` path stales the bridge with no new drift machinery.

**Tech Stack:** Python 3.11, PostgreSQL, psycopg, pytest (`db` fixture, per-test rollback). `uv run pytest/ruff/mypy`.

## Global Constraints

- **Behaviour-neutral for every non-bridge fact type.** Both edits are additive branches gated on `fact_type == facts.ENTITY_BRIDGE` / `payload["fact_type"] == "entity_bridge"`. NO existing `approved_join`/`grain`/`availability`/`scd` path changes. The full `tests/featuregen/` suite stays green.
- **Scope: freshness/dependency ONLY.** No planner, no binding enumeration, no plan contracts. This closes the bridge → `overlay_fact_dependency` gap and nothing else.
- **Two-source, own-source qualification.** Each bridge endpoint is indexed under ITS OWN `catalog_source` (a cross-catalog bridge's right side must be tracked under the right-catalog, or drift-staling + the read-time freshness guard both fail open). Mirror the `approved_join` two-endpoint pattern in `fact_dependencies`.
- **Single-source read models still skipped.** A bridge still creates NO `overlay_proposal` and NO `overlay_fact_state` row (its active state is the direct fold in `bridge_projection.py`). Only `overlay_fact_dependency` is populated.
- **Dep ref-object format matches the drift tracker + the approved_join deps:** table = `schema.table` (`table_obj`), column = `schema.table.column`. A bridge endpoint is a `CatalogObjectRef` dict `{catalog_source, object_kind, schema, table, column}`; index BOTH the table and the identifier column per endpoint, so a drop of either stales the bridge.
- **Tooling:** `uv run pytest <path> -q`, `uv run ruff check`, `uv run mypy`. ruff prefers `collections.abc`, forbids E402 in `src/**`. Commit trailer: the harness default co-author.

## Reused interfaces
- `overlay/dependencies.py`: `fact_dependencies(object_ref, fact_type, value, catalog_source) -> set[tuple[str,str]]`; `table_obj(ref) -> str`. `facts.ENTITY_BRIDGE == "entity_bridge"` (already registered).
- `overlay/projection.py`: `OverlayProjection.apply`; the `OVERLAY_FACT_PROPOSED` branch currently early-returns for `entity_bridge`. `overlay_fact_dependency(fact_key, catalog_source, ref_object)`.
- `overlay/projection.py`: `dependents_of(conn, catalog_source, object_ref) -> list[str]` (the reverse index drift-staling reads).
- Test harness: `db` fixture; `propose_bridge`/`derive_bridge_candidates` (`overlay/upload/bridge_propose.py`, `bridge_candidates.py`); `_two_catalog_customer` (`tests/featuregen/overlay/upload/test_bridge_candidates.py`); `_ENRICH_ACTOR`; `ensure_upload_catalog_adapter()`; `run_projection(conn, OverlayProjection())`.

---

### Task 1: `fact_dependencies` learns entity_bridge (pure)

**Files:**
- Modify: `src/featuregen/overlay/dependencies.py`
- Test: `tests/featuregen/overlay/test_dependencies.py` (create if absent, else append)

**Interfaces:**
- Produces: `fact_dependencies(_, "entity_bridge", {entity_id, left_ref, right_ref}, _) -> {(l_src, l_table), (r_src, r_table), (l_src, l_table.col), (r_src, r_table.col)}`.

- [ ] **Step 1: Write the failing test**

```python
from featuregen.overlay.dependencies import fact_dependencies


def _ref(source, table, col):
    return {"catalog_source": source, "object_kind": "column", "schema": "public",
            "table": table, "column": col}


def test_entity_bridge_dependencies_are_both_endpoints_under_own_source():
    value = {"entity_id": "customer",
             "left_ref": _ref("core", "customer_master", "customer_id"),
             "right_ref": _ref("crm", "customers", "customer_id")}
    deps = fact_dependencies("customer: ... <-> ...", "entity_bridge", value, "")
    assert deps == {
        ("core", "public.customer_master"),
        ("crm", "public.customers"),
        ("core", "public.customer_master.customer_id"),
        ("crm", "public.customers.customer_id"),
    }
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/test_dependencies.py -q` → FAIL (the bridge falls through to the single-source default `{("", "customer: ... <-> ...")}`).

- [ ] **Step 3: Add the branch** — in `src/featuregen/overlay/dependencies.py`, add BEFORE the `if fact_type == facts.APPROVED_JOIN:` block (or immediately after it):

```python
    if fact_type == facts.ENTITY_BRIDGE:
        lr, rr = value["left_ref"], value["right_ref"]
        l_src, r_src = lr["catalog_source"], rr["catalog_source"]
        l_obj, r_obj = table_obj(lr), table_obj(rr)
        # both endpoints, each under its OWN catalog_source: the table AND the identifier column, so a
        # drop/rename/retype of either endpoint stales the bridge. A bridge is unordered — indexing both
        # sides symmetrically is correct.
        return {
            (l_src, l_obj), (r_src, r_obj),
            (l_src, f"{l_obj}.{lr['column']}"), (r_src, f"{r_obj}.{rr['column']}"),
        }
```

Also extend the module docstring's one-line summary to mention the entity_bridge two-endpoint case (a bridge, like approved_join, references two sources).

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/test_dependencies.py -q` → PASS.

- [ ] **Step 5: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/dependencies.py tests/featuregen/overlay/test_dependencies.py
uv run mypy src/featuregen/overlay/dependencies.py
git add -A && git commit -m "feat(3b3.0): fact_dependencies indexes an entity_bridge's two endpoints (task 1)"
```

---

### Task 2: `OverlayProjection` indexes bridge dependencies (drift-stale wiring)

**Files:**
- Modify: `src/featuregen/overlay/projection.py`
- Test: `tests/featuregen/overlay/upload/test_bridge_projection.py` (append)

**Interfaces:**
- Consumes: Task 1's `entity_bridge` branch of `fact_dependencies`.
- Produces: a bridge PROPOSED event populates `overlay_fact_dependency` with the bridge's endpoints (both catalogs); `dependents_of(catalog_source, endpoint_ref)` returns the bridge fact_key from EITHER side; still NO `overlay_proposal`/`overlay_fact_state` row for the bridge.

- [ ] **Step 1: Write the failing tests** — append to `tests/featuregen/overlay/upload/test_bridge_projection.py`:

```python
def test_bridge_endpoints_land_in_the_dependency_index(db):
    from featuregen.overlay.projection import OverlayProjection, dependents_of
    from featuregen.projections.runner import run_projection
    ensure_upload_catalog_adapter()
    _two_catalog_customer(db)
    key = propose_bridge(db, derive_bridge_candidates(db)[0], actor=_ENRICH_ACTOR, now=_NOW)
    while run_projection(db, OverlayProjection()) >= 500:
        pass
    deps = set(db.execute(
        "SELECT catalog_source, ref_object FROM overlay_fact_dependency WHERE fact_key = %s",
        (key,)).fetchall())
    assert deps == {
        ("core", "public.customer_master"), ("crm", "public.customers"),
        ("core", "public.customer_master.customer_id"), ("crm", "public.customers.customer_id")}
    # the reverse index drift-staling reads finds the bridge from EITHER catalog side
    assert key in dependents_of(db, "core", "public.customer_master.customer_id")
    assert key in dependents_of(db, "crm", "public.customers.customer_id")
    # still NO single-source read-model rows for the two-source bridge
    assert db.execute("SELECT count(*) FROM overlay_proposal WHERE fact_key=%s", (key,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM overlay_fact_state WHERE fact_key=%s", (key,)).fetchone()[0] == 0
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/featuregen/overlay/upload/test_bridge_projection.py::test_bridge_endpoints_land_in_the_dependency_index -q` → FAIL (the bridge PROPOSED event early-returns, so `overlay_fact_dependency` is empty for the bridge).

- [ ] **Step 3: Replace the early-return with dependency-only maintenance** — in `src/featuregen/overlay/projection.py`, inside `OverlayProjection.apply`, the `OVERLAY_FACT_PROPOSED` branch, replace:

```python
            if payload.get("fact_type") == "entity_bridge":
                return
```
with:
```python
            if payload.get("fact_type") == "entity_bridge":
                # 3B.3.0: a bridge is two-source — the single-catalog_source overlay_proposal/_state read
                # models still don't model it (skip them), but its two catalog endpoints DO belong in
                # overlay_fact_dependency so catalog drift stales the bridge (detect_catalog_changes ->
                # dependents_of -> a STALED event on the bridge fact). Maintain ONLY the dependency index
                # here; bridge dependencies are immutable post-propose, so CONFIRMED/EXPIRED/… stay no-ops.
                conn.execute("DELETE FROM overlay_fact_dependency WHERE fact_key = %s", (fk,))
                for dep_source, ref_object in fact_dependencies(
                        payload["object_ref"], "entity_bridge", payload["proposed_value"], ""):
                    conn.execute(
                        "INSERT INTO overlay_fact_dependency (fact_key, catalog_source, ref_object) "
                        "VALUES (%s, %s, %s) ON CONFLICT (fact_key, catalog_source, ref_object) DO NOTHING",
                        (fk, dep_source, ref_object))
                return
```
(`fact_dependencies` is already imported at the top of `projection.py`. The `catalog_source` arg is passed `""` — the `entity_bridge` branch derives both sources from the value and ignores it. `_catalog_source(payload)` is NOT called for a bridge — that is the KeyError this guard has always avoided.)

- [ ] **Step 4: Run to verify it passes** — `uv run pytest tests/featuregen/overlay/upload/test_bridge_projection.py -q` → PASS (the prior 5 bridge-projection tests + the new one = 6).

- [ ] **Step 5: Behaviour-neutral proof (full suite)** — `uv run pytest tests/featuregen/ -q` → the pre-existing total **+1** new test, zero new failures (every other fact type's PROPOSED path is byte-identical; the change is gated on `entity_bridge`).

- [ ] **Step 6: Gates + commit**

```bash
uv run ruff check src/featuregen/overlay/projection.py tests/featuregen/overlay/upload/test_bridge_projection.py
uv run mypy src/featuregen/overlay/projection.py
git add -A && git commit -m "feat(3b3.0): OverlayProjection indexes bridge endpoints for drift-staling (task 2)"
```

---

## Exit criteria mapping

| 3B.3.0 requirement | Where satisfied |
|---|---|
| A bridge's two catalog endpoints are in `overlay_fact_dependency`, each under its own source | Task 1 `fact_dependencies` branch + Task 2 `test_bridge_endpoints_land_in_the_dependency_index` |
| Drift on EITHER bridged catalog can stale the bridge | Task 2 — `dependents_of(catalog, endpoint)` returns the bridge fact_key from both sides (the reverse index the existing `detect_catalog_changes → _stale_dependents` path reads) |
| Single-source read models still skip the two-source bridge | Task 2 — no `overlay_proposal`/`overlay_fact_state` row asserted |
| Behaviour-neutral; no planner / no binding enumeration | Task 2 Step 5 full-suite; both edits gated on `entity_bridge` |

## Self-Review

**Spec coverage:** the deferred 3B.2B integration (bridge → `overlay_fact_dependency`) is closed by the two additive branches; the drift-stale path itself is existing tested machinery (`detect_catalog_changes → dependents_of → STALED`), so proving the bridge is now IN the reverse index is sufficient. ✅
**Placeholder scan:** every step has complete code + real assertions. ✅
**Type consistency:** the `entity_bridge` branch returns `set[tuple[str,str]]` matching `fact_dependencies`' signature; the projection loop consumes `(dep_source, ref_object)` pairs exactly as the existing non-bridge path does. ✅
**Note:** CONFIRMED/EXPIRED/STALED bridge events remain no-ops here by design (bridge deps are immutable post-propose; the bridge's VERIFIED state is the direct fold in `bridge_projection.py`). A STALED event fired by drift lands on the bridge stream and is reflected by the next `project_verified_bridge` (which removes the edge when status != VERIFIED) — no change needed in this plan.
