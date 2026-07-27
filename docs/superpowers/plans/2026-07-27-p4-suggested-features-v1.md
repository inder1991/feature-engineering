# P4 v1 — Suggested Features (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After an upload, show per-table AI-suggested features **without the user typing a hypothesis** — grouped by entity, with honest statuses — by exposing the deterministic engine that already exists.

**Architecture:** `gate1._template_candidates` already grounds all 153 templates against a catalog and runs each through the full gauntlet, with **no intent, no hypothesis and no LLM**. Its only call site is inside `build_considered_set`. v1 adds a thin public read function that calls it with `target_ref=None, now=None`, filters to one table, groups by entity, plus one read-only route and one read-only screen.

**Tech Stack:** Python 3.12, psycopg, FastAPI, pytest (`overlay_conn`/`db` fixtures), React/TypeScript + Vitest.

## Scope & Deferrals

- **IN:** a public suggestion function, a recipe-line renderer, one read-only `GET` route, one read-only screen.
- **OUT (v2, per spec):** Accept / Edit / Dismiss, the blocked-with-reason card (needs `ground_template` to return the unmet `Need`), "Find source", durable dismiss, any relevance **percentage**.
- **OUT (NFR):** caching/precompute, dashboards, telemetry, cost controls.

## Global Constraints

- **STRICTLY READ-ONLY.** v1 writes nothing — no evidence, no decisions, no intent, no contract. It must be impossible to govern or accept from this surface.
- **NO fake relevance %.** No percentage scorer exists. Render the **binding-quality** signal (`binding_by_id`, already returned) and/or ordinal order. Never invent a number.
- **Reuse, do not fork.** Call `_template_candidates`; do NOT re-implement grounding or the gauntlet. Statuses must come from the real `validation_status`, chips/notes from real `Template` fields and real `Requirement`s.
- **Read-scoping is mandatory:** thread `roles=identity.role_claims` exactly as `assist.py:85` does — suggestions must never reveal columns the caller can't see.
- **Verified engine contract** (main moves fast — re-check before coding): `_template_candidates(conn, *, catalog_source, roles, target_ref, now, templates=ALL_TEMPLATES, fresh_within=timedelta(hours=24))` returns an **8-tuple**: `(ideas, rejections, grounded_ids, rejected_ids, binding_by_id, incomplete_ids, contexts, keys_by_recipe)`. `rejections` are `{"name","reason","code"}` dicts.
- **`FeatureIdea` fields to render** (`feature_assist.py:472-499`): `name`, `description`, `grain_table`, `derives_pairs`, `rationale`, `operation_kind`, `measure_refs`, `grain_ref`, `time_ref`, `window`, `validation_status`, `requirements`.
- Do not modify `_template_candidates`, `ground_all`, `_validate_idea`, or any template.

## File Structure

- Create: `src/featuregen/overlay/upload/suggestions.py` — the public read function + recipe renderer.
- Create: `src/featuregen/api/routes/suggestions.py` — one `GET` route (register in the app router).
- Create: `tests/featuregen/overlay/upload/test_suggestions.py`, `tests/featuregen/api/routes/test_suggestions_route.py`.
- Create: `frontend/src/screens/SuggestedFeaturesScreen.tsx` (+ `.test.tsx`); modify `frontend/src/nav.ts`, `App.tsx`, `api.ts`.

---

### Task 1: `suggest_features_for_table` — the public read function

**Files:** Create `src/featuregen/overlay/upload/suggestions.py`; Test `tests/featuregen/overlay/upload/test_suggestions.py`.

**Interfaces:**
- Produces `suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=()) -> dict` returning:
  `{"catalog_source", "table", "summary": {"suggested", "clean_ready", "needs_review", "entities"}, "groups": [{"entity_ref", "entity_label", "suggestions": [...]}], "rejections": [...]}`
  where each suggestion is `{"name", "description", "recipe", "recipe_parts", "validation_status", "requirements": [{"code","operand","detail"}], "uses": [object_ref...], "binding_quality"}`.
- Consumes `gate1._template_candidates` (8-tuple above).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_suggestions.py
from featuregen.overlay.upload.suggestions import suggest_features_for_table

def test_suggests_features_for_a_table_without_any_hypothesis(overlay_conn, ftr_catalog):
    """The whole point: no intent, no hypothesis, no LLM — just the catalog."""
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    assert out["summary"]["suggested"] >= 1
    # counts are the REAL tri-state, not invented
    assert out["summary"]["clean_ready"] + out["summary"]["needs_review"] == out["summary"]["suggested"]
    s = out["groups"][0]["suggestions"][0]
    assert s["description"]          # Template.intent, a real SME sentence
    assert s["validation_status"] in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
    assert s["uses"]                 # the columns it binds

def test_only_this_tables_suggestions_are_returned(overlay_conn, ftr_catalog):
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    for g in out["groups"]:
        for s in g["suggestions"]:
            assert s["grain_table"] == ftr_catalog.table

def test_grouped_by_entity(overlay_conn, ftr_catalog):
    out = suggest_features_for_table(
        overlay_conn, catalog_source=ftr_catalog.source, table=ftr_catalog.table)
    labels = [g["entity_ref"] for g in out["groups"]]
    assert len(labels) == len(set(labels))          # one group per entity, no duplicates
    assert out["summary"]["entities"] == len(labels)

def test_writes_nothing(overlay_conn, ftr_catalog):
    """v1 is strictly read-only — the load-bearing guarantee."""
    def counts():
        return tuple(overlay_conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("field_evidence", "field_decision_event", "graph_node",
                               "contract_intent"))
    before = counts()
    suggest_features_for_table(overlay_conn, catalog_source=ftr_catalog.source,
                               table=ftr_catalog.table)
    assert counts() == before
```

Build `ftr_catalog` from the real FTR fixture (`read_ftr_glossary` + `to_glossary_upload` + `ingest_upload`), mirroring `tests/featuregen/overlay/upload/test_concept_cascade_provenance.py` — and **give it its own source name** (see the merge lesson: a generic name like `bank` collides in full-suite runs and the MF-6 guard will hold the upload).

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/featuregen/overlay/upload/test_suggestions.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/featuregen/overlay/upload/suggestions.py
"""P4 v1 — read-only per-table feature suggestions.

The engine already exists: `gate1._template_candidates` grounds the whole template registry against a
catalog and runs every candidate through the same gauntlet the LLM candidates clear — deterministically,
with NO intent, NO hypothesis and NO LLM. It simply had one call site, inside `build_considered_set`.
This module exposes it per table. It WRITES NOTHING.
"""
from featuregen.overlay.upload.contract.gate1 import _template_candidates


def suggest_features_for_table(conn, *, catalog_source: str, table: str, roles=()) -> dict:
    ideas, rejections, _grounded, _rejected, binding_by_id, _incomplete, _ctx, _keys = (
        _template_candidates(conn, catalog_source=catalog_source, roles=roles,
                             target_ref=None, now=None))          # no intent, no clock, no LLM
    mine = [i for i in ideas if i.grain_table == table]
    groups: dict[str, list[dict]] = {}
    for idea in mine:
        entity_ref = idea.grain_ref[1] if idea.grain_ref else ""
        groups.setdefault(entity_ref, []).append(_suggestion(idea, binding_by_id))
    clean = sum(1 for i in mine if i.validation_status == "DESIGN_CHECKED")
    return {
        "catalog_source": catalog_source, "table": table,
        "summary": {"suggested": len(mine), "clean_ready": clean,
                    "needs_review": len(mine) - clean, "entities": len(groups)},
        "groups": [{"entity_ref": ref, "entity_label": _entity_label(ref),
                    "suggestions": items} for ref, items in sorted(groups.items())],
        "rejections": rejections,
    }
```
`_suggestion(idea, binding_by_id)` builds the per-card dict (name, description, `grain_table`, recipe via Task 2, `validation_status`, requirements as `{code, operand, detail}`, `uses` = the distinct object_refs from `derives_pairs`, `binding_quality`). `_entity_label` derives a display label from the entity ref (e.g. its column name) — no invention beyond formatting.

- [ ] **Step 4: Run → pass**, then run the neighbouring suites for regressions: `uv run pytest tests/featuregen/overlay/upload/contract -q`.
- [ ] **Step 5: Commit** — `feat(p4): read-only per-table feature suggestions (no hypothesis, no LLM)`

---

### Task 2: The recipe renderer

**Files:** Modify `src/featuregen/overlay/upload/suggestions.py`; Test: extend Task 1's module.

**Interfaces:** `render_recipe(idea) -> str` over `operation_kind`, `measure_refs`, `grain_ref`, `time_ref`, `window`.

**Honesty constraint:** `aggregation`/`operation_kind` is a DOMAIN label (e.g. `trend`, `inflow_outflow`) — 152 distinct values, NOT a SQL verb. Do **not** invent a label→SQL map. Render the real label: `trend_90d(balance) BY cif_id OVER 90d [as_of_date]`. Legible and true.

- [ ] **Step 1: Failing tests** — a windowed idea renders operation, measure column(s), `BY` the grain column, `OVER` the window, `[time column]`; an idea with no window/time omits those clauses cleanly (no dangling `OVER`/`[]`).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** Subtract `grain_ref`/`time_ref` from `measure_refs` to get the true measure column(s) (`measure_refs` carries *all* bound pairs). Use the column name (last segment of `object_ref`), not the full ref.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `feat(p4): honest recipe-line renderer`

---

### Task 3: The read-only route

**Files:** Create `src/featuregen/api/routes/suggestions.py`; register the router; Test `tests/featuregen/api/routes/test_suggestions_route.py`.

**Interfaces:** `GET /catalog/{catalog_source}/tables/{table}/suggestions` → the Task-1 dict.

- [ ] **Step 1: Failing tests** — 200 returns the payload shape; **roles are threaded** (`roles=identity.role_claims`, mirroring `assist.py:85`) so a caller lacking a role sees a correspondingly scoped result; the route is **GET-only** (a POST/PUT to it 405s).
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement.** `router = APIRouter()`; guard with the same read dependency the catalog/assist reads use (verify the correct one — do NOT invent a permission); call `suggest_features_for_table(conn, catalog_source=..., table=..., roles=identity.role_claims)`; register the router where the others are registered.
- [ ] **Step 4: Run → pass** + `uv run pytest tests/featuregen/api -q` (compare failures against the known pre-existing baseline; introduce none).
- [ ] **Step 5: Commit** — `feat(p4): GET suggestions route (read-only, role-scoped)`

---

### Task 4: The read-only screen

**Files:** Create `frontend/src/screens/SuggestedFeaturesScreen.tsx` (+ test); modify `frontend/src/api.ts`, `nav.ts`, `App.tsx`.

**Design target:** the user's mockup (artifact `9bcf322f`) — summary counts, entity group headers with the grain column, per-card title/description/recipe/chips/uses.

**Divergences from the mockup (deliberate, per spec):** **no relevance %** (render binding-quality/order); **no Accept/Edit/Dismiss buttons in v1** — do not ship dead controls; state plainly that this view is read-only.

- [ ] **Step 1: Failing component test** — renders the summary counts; renders one group per entity with its grain column; a `DESIGN_CHECKED` card shows a "clean & ready" chip and a `NEEDS_EXTERNAL_VALIDATION` card shows "needs review" with its requirement text; **no Accept/Dismiss control is rendered**.
- [ ] **Step 2: Run** `cd frontend && npx vitest run src/screens/SuggestedFeaturesScreen.test.tsx` → FAIL. *(Full vitest hangs on worker-start in this env — run the single file; CI runs the suite.)*
- [ ] **Step 3: Implement** the screen + `api.ts` client + a `'suggested'` route. Follow the `asset` detail-sheet precedent (`App.tsx:138-145`) — a route excluded from the left rail — rather than adding to the 2549-line WorkbenchScreen.
- [ ] **Step 4: Run the single test file → pass.**
- [ ] **Step 5: Commit** — `feat(p4): read-only Suggested Features screen`

---

## Self-Review (author checklist — completed)

- **Read-only is tested, not asserted:** Task 1 has an explicit no-writes test over evidence/decision/graph/intent; Task 3 pins GET-only; Task 4 renders no action controls.
- **No fabricated data:** no relevance %; recipe uses the real operation label; statuses/requirements/descriptions all come from real code paths.
- **Reuse:** `_template_candidates` is called, never re-implemented; no template or gauntlet edits.
- **Engine contract verified against current main** (8-tuple return, `FeatureIdea` field names, `assist.py` roles pattern) — and the plan tells the implementer to re-verify, since main moves fast.
- **Merge lesson applied:** the test fixture must use its own non-generic source name.
- **Type consistency:** `suggest_features_for_table` / `render_recipe` signatures identical across tasks.

## Deferred (v2)
Blocked-with-reason cards (capture the unmet `Need` in `ground_template`), Accept → hypothesis prompt → existing governed flow, durable Dismiss, "Find source", a calibrated relevance score if it earns its place.
