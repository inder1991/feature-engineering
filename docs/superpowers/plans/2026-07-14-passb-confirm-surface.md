# Pass B Confirm Surface Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the API + UI for a platform-admin to approve/reject the governed `grain`/`availability_time` facts Pass B proposes — single-confirmer, extending the existing `governance` surface and projecting a VERIFIED fact onto `graph_node`.

**Architecture:** One new domain module (`overlay/upload/table_fact_governance.py`) + extensions to `api/routes/governance.py` and `GovernanceReviewScreen.tsx`, over the UNCHANGED overlay single-path `confirm_fact`/`reject_fact`. The merged **joins surface is the structural template** — `overlay/upload/join_governance.py`, the join routes in `api/routes/governance.py`, and the joins tab in `GovernanceReviewScreen.tsx` — mirror them, adapting for single-confirmer table facts.

**Tech Stack:** Python 3.11 · FastAPI · psycopg3 · pytest (+ pytest-postgresql) · React/TS · vitest · uv.

**Spec:** `docs/superpowers/specs/2026-07-14-passb-confirm-surface-design.md`. Every task's requirements implicitly include it.

## Global Constraints

- Endpoints (parallel to joins): `GET /sources/{source}/governance/table-facts`; `POST /governance/table-facts/{fact_key}/confirm`; `POST /governance/table-facts/{fact_key}/reject`.
- `require_confirmer` (raw `platform-admin` claim) on all three (reuse from `api/deps.py`).
- **Single-confirmer:** `grain`/`availability_time` take the single `confirm_fact` path → one platform-admin confirm → VERIFIED (four-eyes via service proposer `_ENRICH_ACTOR` ≠ human). No partial state, no "different admin" rule, no dual referent guard.
- Confirm/reject **validate `fact_type ∈ {"grain","availability_time"}`** on the loaded fact → **404** otherwise (no event).
- On VERIFIED, **synchronously project** onto `graph_node` via `project_verified_table_fact`: drain the overlay projection on the request conn first, then `project_table_facts_for_ref`, then **verify the flag was actually set** — return `"projected"` only if `graph_node.is_grain`/`is_as_of` is now set for the table, else `"pending"` (honest under the drift-freshness SLA; fail-soft).
- Reject categories: `Literal["wrong_grain_columns","wrong_as_of_column","not_unique","needs_data_check"]`; note `max_length=1000`, trimmed, empty→None.
- Only `PROPOSED` (folded `DRAFT`) listed; VERIFIED/REJECTED excluded. `source` compared normalized (strip+lower). List bounded (`limit` default 100, max 500). No customer values rendered. No new DB migrations.
- Tests: `uv run pytest <path> -q`; API tests reuse the joins harness (`tests/featuregen/api/conftest.py` `make_client`/`client`, `X-Roles: platform-admin`; seed via the passc/table-fact propose path). Frontend: `cd frontend && npm test`.

## File Structure

- **Create** `src/featuregen/overlay/upload/table_fact_governance.py` — read model + `load_table_fact_confirmation_context` + `project_verified_table_fact`.
- **Modify** `src/featuregen/api/routes/governance.py` — three table-fact routes + request models.
- **Modify** `frontend/src/api.ts`, `frontend/src/screens/GovernanceReviewScreen.tsx` — client fns + the Grain & availability tab.
- **Tests:** `tests/featuregen/overlay/upload/test_table_fact_governance.py`, add to `tests/featuregen/api/test_governance_routes.py`, add to `GovernanceReviewScreen.test.tsx`.

---

### Task 1: `table_fact_governance.py` — read model + context bridge + synchronous projection

**Files:**
- Create: `src/featuregen/overlay/upload/table_fact_governance.py`
- Test: `tests/featuregen/overlay/upload/test_table_fact_governance.py`

**Interfaces:**
- Consumes: `overlay/upload/table_fact_projection.py` (`list_open_table_fact_proposals`, `_WORKLIST_READER`, `_TABLE_FACT_TYPES`, `project_table_facts_for_ref`); `overlay/store.load_fact`; `overlay/identity._ref_from_payload`, `fact_key`; `overlay/state.fold_overlay_state`; `overlay/_lifecycle._cas_target`; `overlay/projections/runner.projection_lag`+`run_projection`, `overlay/projections.OverlayProjection`; `overlay/upload/object_ref.normalize_ref`; `overlay/upload/upload_catalog.table_ref`. **Study `overlay/upload/join_governance.py` as the structural template** — this module is its table-fact sibling.
- Produces:
  - `class TableFactGovernanceNotFound(Exception)`
  - `list_open_table_fact_proposals_governance(conn, source: str, *, limit: int = 100) -> list[dict]` — dicts with keys `fact_key, task_id, target_event_id, fact_type, table, proposed_value, status ("PROPOSED"), origin ("llm_proposed_not_profiled"), advisory ({table_role,primary_entity,event_or_snapshot}), evidence_parse_status ("parsed"|"missing")`.
  - `load_table_fact_confirmation_context(conn, fact_key) -> {ref, fact_type, use_case, target_event_id}` (raises `TableFactGovernanceNotFound`).
  - `project_verified_table_fact(conn, source: str, ref, fact_type: str, *, now) -> "projected"|"pending"`.

- [ ] **Step 1: Write the failing tests.** Seed a grain proposal (via the Pass B propose path — read how `test_table_fact_projection.py` / the table_synth tests seed a grain proposal; simplest is a direct `propose_fact(Command("propose_fact","overlay_fact",None,{"ref":table_ref(src,"t"),"fact_type":"grain","proposed_value":{"columns":["cif_id"],"is_unique":True}}, _ENRICH_ACTOR, proposal_fingerprint(value)))`).

```python
# test_table_fact_governance.py
from featuregen.overlay.upload.table_fact_governance import (
    list_open_table_fact_proposals_governance, load_table_fact_confirmation_context,
    project_verified_table_fact, TableFactGovernanceNotFound)
from featuregen.overlay.identity import fact_key
from featuregen.overlay.upload.upload_catalog import table_ref

def test_lists_a_grain_proposal(passc_conn, seed_grain_proposal):   # helper seeds a DRAFT grain fact for source "src", table "t"
    out = list_open_table_fact_proposals_governance(passc_conn, "src")
    assert len(out) == 1
    p = out[0]
    assert p["fact_type"] == "grain"
    assert p["table"] == "t"
    assert p["status"] == "PROPOSED"
    assert p["proposed_value"]["columns"] == ["cif_id"]
    assert p["origin"] == "llm_proposed_not_profiled"

def test_excludes_other_sources(passc_conn, seed_grain_proposal):
    assert list_open_table_fact_proposals_governance(passc_conn, "other") == []

def test_context_bridge_returns_ref_and_accepted_target(passc_conn, seed_grain_proposal):
    key = fact_key(table_ref("src","t"), "grain", None)
    ctx = load_table_fact_confirmation_context(passc_conn, key)
    assert ctx["fact_type"] == "grain"
    assert ctx["target_event_id"]     # prove it's accepted by driving a real confirm in a sibling test

def test_context_rejects_non_table_fact(passc_conn, seed_join_proposal):   # an approved_join fact_key
    import pytest
    with pytest.raises(TableFactGovernanceNotFound):
        load_table_fact_confirmation_context(passc_conn, seed_join_proposal)

def test_project_sets_is_grain_on_fresh_watermark(passc_conn, seed_grain_proposal, confirm_as_admin, seed_graph_nodes, fresh_watermark):
    # confirm the grain to VERIFIED, then project
    ref = table_ref("src","t")
    ... # confirm_as_admin(ref, "grain")  -> VERIFIED
    status = project_verified_table_fact(passc_conn, "src", ref, "grain", now=None)
    assert status == "projected"
    # graph_node.is_grain is now true for src.t.cif_id
```

Read the passc/table-fact test fixtures for the exact seed/confirm/watermark helpers; reuse them.

- [ ] **Step 2: Run — FAIL** (module missing). `uv run pytest tests/featuregen/overlay/upload/test_table_fact_governance.py -q`
- [ ] **Step 3: Implement `table_fact_governance.py`** (mirror `join_governance.py`):
  - Read model: iterate `list_open_table_fact_proposals(conn)` (it already filters to `_TABLE_FACT_TYPES`); it returns `task_id, fact_type, object_ref, proposed_value, target_event_id, uniqueness_basis` — but NOT `fact_key`. Get the fact_key by re-deriving from the ref, OR extend `list_open_table_fact_proposals` to also return `fact_key` (simplest: SELECT `task_id, fact_key FROM human_tasks` and call `get_task_proposal` per task, like the join scan). For each: `load_fact(fact_key)`, `_ref_from_payload(stream[0].payload["catalog_object_ref"])` → recover `catalog_source` (`ref.catalog_source` for a table ref), filter to `normalize(source)`; `fold_overlay_state(stream).status` — keep only `DRAFT` (→ display `"PROPOSED"`); read the table's advisory fields best-effort (read `read_active_field_evidence` or the advisory field store for `table_role`/`primary_entity`/`event_or_snapshot` on the table ref — display-only, default null on any error); `evidence_parse_status` = `"parsed"` if `proposed_value` is a well-formed dict, else `"missing"`. Per-task try/except-skip (never abort the list). `limit = max(1, min(limit, 500))`.
  - `load_table_fact_confirmation_context`: `load_fact`; empty → raise; decode ref (raise on failure); `fact_type = stream[0].payload["fact_type"]`; if `fact_type not in ("grain","availability_time")` → raise `TableFactGovernanceNotFound`; `target_event_id = _cas_target(fold_overlay_state(stream))`; return `{ref, fact_type, use_case: None, target_event_id}`.
  - `project_verified_table_fact`: fail-soft wrapper (`with conn.transaction():` + except→"pending"). Drain: `while run_projection(conn, OverlayProjection()) >= 500: pass`. Then `project_table_facts_for_ref(conn, source=source, table=ref.table_name_or_object, declared_grain=set(), declared_as_of=set(), now=now)` (read the exact `table_ref`/`CatalogObjectRef` field for the table name). Then verify honestly: SELECT from `graph_node` whether any column of `(source, table)` has `is_grain=true` (for `fact_type=="grain"`) or `is_as_of=true` (for `"availability_time"`) with the fact-event-id set; return `"projected"` if set, else `"pending"`.
- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_table_fact_governance.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/overlay/ -q`; `git add -A && git commit -m "feat(overlay): table-fact governance read model + context bridge + synchronous projection"`

---

### Task 2: `governance.py` — table-fact routes (list / confirm / reject)

**Files:**
- Modify: `src/featuregen/api/routes/governance.py`
- Test: `tests/featuregen/api/test_governance_routes.py` (add)

**Interfaces:**
- Consumes: Task 1's `list_open_table_fact_proposals_governance`, `load_table_fact_confirmation_context`, `project_verified_table_fact`, `TableFactGovernanceNotFound`; the existing `require_confirmer`, `_clean`, `_deny_to_detail`, `get_conn`, `get_identity` in `governance.py`; `confirm_fact`, `reject_fact`, `Command`, `load_fact`, `fold_overlay_state`. **Mirror the join routes in the same file.**
- Produces: three routes registered on the existing `governance.router`.

- [ ] **Step 1: Failing test — single-admin confirm to VERIFIED under sealed config.**

```python
# add to test_governance_routes.py — reuse the sealed_config + graph_node seeding fixtures from the joins tests
def test_table_fact_confirm_single_admin_verifies_and_projects(client, seed_grain_proposal_via_api, sealed_config, seed_graph_nodes):
    source, fact_key = seed_grain_proposal_via_api            # a DRAFT grain fact for the source
    r = client.get(f"/sources/{source}/governance/table-facts", headers={"X-User":"p","X-Roles":"platform-admin"})
    assert r.status_code == 200 and r.json()["proposals"][0]["fact_key"] == fact_key
    r = client.post(f"/governance/table-facts/{fact_key}/confirm", json={"note":"grain looks right"},
                    headers={"X-User":"p","X-Roles":"platform-admin"})
    body = r.json()
    assert body["governance_status"] == "VERIFIED"
    assert body["operational_projection"] in ("projected","pending")
```

Plus: reject `{category,note}` → REJECTED + category on payload; non-table-fact fact_key → confirm/reject 404 no event; non-admin → 403; bad category → 422; over-length note → 422; unknown fact_key → 404; stale-watermark → VERIFIED + `operational_projection == "pending"`.

- [ ] **Step 2: Run — FAIL** (routes missing). `uv run pytest tests/featuregen/api/test_governance_routes.py -q -k table_fact`
- [ ] **Step 3: Implement** (add to `governance.py`, mirroring the join routes):

```python
class ConfirmTableFactRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)

class RejectTableFactRequest(BaseModel):
    category: Literal["wrong_grain_columns","wrong_as_of_column","not_unique","needs_data_check"]
    note: str | None = Field(default=None, max_length=1000)

@router.get("/sources/{source}/governance/table-facts", dependencies=[Depends(require_confirmer)])
def list_table_facts(source: str, conn=Depends(get_conn), limit: int = Query(default=100, ge=1, le=500)):
    return {"source": source.strip().lower(),
            "proposals": list_open_table_fact_proposals_governance(conn, source, limit=limit),
            "next_cursor": None}

@router.post("/governance/table-facts/{fact_key}/confirm", dependencies=[Depends(require_confirmer)])
def confirm_table_fact(fact_key: str, body: ConfirmTableFactRequest,
                       conn=Depends(get_conn), identity=Depends(get_identity)):
    try:
        ctx = load_table_fact_confirmation_context(conn, fact_key)
    except TableFactGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such table-fact proposal.")
    cmd = Command("confirm_fact","overlay_fact",fact_key,
                  {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
                   "target_event_id": ctx["target_event_id"], "note": _clean(body.note)},
                  identity, f"confirm:{fact_key}:{identity.subject}")
    result = confirm_fact(conn, cmd)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    status = fold_overlay_state(load_fact(conn, fact_key)).status
    projection = "not_applicable"
    if status == "VERIFIED":
        projection = project_verified_table_fact(conn, ctx["ref"].catalog_source, ctx["ref"], ctx["fact_type"], now=None)
    return {"governance_status": status, "operational_projection": projection}

@router.post("/governance/table-facts/{fact_key}/reject", dependencies=[Depends(require_confirmer)])
def reject_table_fact(fact_key: str, body: RejectTableFactRequest,
                      conn=Depends(get_conn), identity=Depends(get_identity)):
    try:
        ctx = load_table_fact_confirmation_context(conn, fact_key)
    except TableFactGovernanceNotFound:
        raise HTTPException(status_code=404, detail="No such table-fact proposal.")
    cmd = Command("reject_fact","overlay_fact",fact_key,
                  {"ref": ctx["ref"], "fact_type": ctx["fact_type"], "use_case": ctx["use_case"],
                   "target_event_id": ctx["target_event_id"], "reason": _clean(body.note), "category": body.category},
                  identity, f"reject:{fact_key}:{identity.subject}")
    result = reject_fact(conn, cmd)
    if not result.accepted:
        raise HTTPException(status_code=409, detail=_deny_to_detail(result.denied_reason))
    return {"governance_status": "REJECTED", "category": body.category}
```

Confirm the exact `Command(...)` positional/keyword shape against the join routes (they already construct it correctly). Match the `.catalog_source` attribute name on the decoded table ref (read the `CatalogObjectRef` dataclass). Routes self-ensure the adapter if the join routes do (reuse that call).

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/api/test_governance_routes.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/api/ -q`; `git add -A && git commit -m "feat(api): governance table-fact routes — confirm/reject grain & availability"`

---

### Task 3: `GovernanceReviewScreen` — the Grain & availability tab

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/screens/GovernanceReviewScreen.tsx`
- Test: `frontend/src/screens/GovernanceReviewScreen.test.tsx` (add)

**Interfaces:**
- Consumes: the Task-2 endpoints. **Mirror the joins tab** already in `GovernanceReviewScreen.tsx` (the card, checklist-gated Approve, structured reject, session-local resolved map, 409-reload concurrency UX) — adapt for single-confirmer table facts.
- Produces: a working "Grain & availability" tab.

- [ ] **Step 1: api.ts fns.** Mirror `listJoinProposals`/`confirmJoin`/`rejectJoin`:

```ts
export interface TableFactProposal {
  fact_key: string; fact_type: 'grain'|'availability_time'; table: string;
  proposed_value: any; status: 'PROPOSED'; origin: string;
  advisory: {table_role: string|null; primary_entity: string|null; event_or_snapshot: string|null};
  evidence_parse_status: string;
}
export const listTableFactProposals = (source: string) =>
  request<{source:string; proposals: TableFactProposal[]; next_cursor: string|null}>(`/sources/${encodeURIComponent(source)}/governance/table-facts`);
export const confirmTableFact = (factKey: string, body: {note?: string}) =>
  post(`/governance/table-facts/${encodeURIComponent(factKey)}/confirm`, body);
export const rejectTableFact = (factKey: string, body: {category: string; note?: string}) =>
  post(`/governance/table-facts/${encodeURIComponent(factKey)}/reject`, body);
```
Add `TABLE_FACT_REJECT_CATEGORIES = ['wrong_grain_columns','wrong_as_of_column','not_unique','needs_data_check'] as const`. Add `/governance` is already proxied (the joins work added it) — confirm `table-facts` paths are covered by the `/governance` prefix in `vite.config.ts`.

- [ ] **Step 2: The tab.** Wire the existing "Grain & availability" tab to `listTableFactProposals(source)` and render single-confirmer cards: the proposed grain columns (`proposed_value.columns`) or availability column+basis (`proposed_value.column`/`.basis`); the advisory context (table_role/primary_entity when present); the **consequence line** (grain: "one row = one <columns>; features aggregate to this grain"; availability: "point-in-time features read <column>"); the **"LLM-inferred, not value-profiled" caution**; the **checklist** (the 4 baseline items per fact_type from the spec §3) **gating Approve**; a structured **Reject** (the 4 categories). One Approve → `confirmTableFact(fact_key,{note})` → VERIFIED (no "1 of 2"). Session-local resolved map + 409-reload UX, same as the joins tab.
- [ ] **Step 3: Test.** Add a vitest case: mock `listTableFactProposals` → one grain proposal → the card renders `cif_id`; Approve disabled until the checklist is ticked; ticking enables; click → `confirmTableFact` called with the fact_key.
- [ ] **Step 4: Run.** `cd frontend && npm test` + `npm run typecheck` + `npm run build`. Fix any type errors.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat(frontend): GovernanceReviewScreen grain & availability tab — approve/reject Pass B facts"`

---

## Self-Review

**Spec coverage:** §1 endpoints → Task 2; §2 module → Task 1; §3 checklist/consequence (frontend) → Task 3; §4 frontend → Task 3; §5 RBAC/errors → Task 2 (reuses joins helpers); §6 tests → each task; §7 security → Tasks 2 (claim gate) + 1 (metadata-only). Acceptance 1–7 each map to a task's test. No gap.

**Placeholder scan:** the test `...` lines mark "read the passc/table-fact fixtures and fill the exact seed/confirm/watermark helper" — the assertions are concrete; the seed line is completed from named existing fixtures. Frontend markup references the merged joins tab as its concrete template. No logic hand-waved.

**Type consistency:** `fact_key` (str) keys confirm/reject; `list_open_table_fact_proposals_governance(conn, source, *, limit)`, `load_table_fact_confirmation_context(conn, fact_key) -> {ref,fact_type,use_case,target_event_id}`, `project_verified_table_fact(conn, source, ref, fact_type, *, now) -> "projected"|"pending"`, `TableFactGovernanceNotFound` — identical across Tasks 1/2. Response shapes match §1.

## Execution Handoff

Build **subagent-driven** (Fable implementers, Opus reviewers, whole-branch review before merge) on branch `passb-confirm-surface`.
