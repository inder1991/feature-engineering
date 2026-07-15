# Phase 4 Governance Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A read-only governance dashboard — cross-source and per-source rollups of the governed pipeline (counts by fact type × status, rejected-by-category, queue health, a join calibration seed) — plus its on-demand analytics read model.

**Architecture:** A pure read model (`overlay/upload/governance_analytics.py`) folds three existing sources — the `human_tasks` queue, the `overlay_fact` event streams (confirm/reject), and the `pass_c_candidate_evidence` ledger — keyed by `fact_key`, into typed aggregates. Two read-only API routes expose it; a React screen renders it. No new write path, no new DB migration. **Templates: `overlay/upload/readiness.py` (the enumeration + fold seams), `api/routes/readiness.py` (the read-only route module), `GovernanceReviewScreen.tsx` (the screen/styling).**

**Tech Stack:** Python 3.11 · FastAPI · psycopg3 · pytest (+ pytest-postgresql) · React/TS · vitest · uv.

**Spec:** `docs/superpowers/specs/2026-07-15-phase4-governance-dashboard-design.md`. Every task's requirements implicitly include it.

## Global Constraints

- **Read-only, fail-soft.** No writes; no change to the governance engine, fold, or scoring. One unreadable/undecodable task, fact stream, or ledger row is skipped (with a `overlay.governance_analytics.*` counter), never aborting the dashboard (mirror `join_governance`/`readiness`).
- **On-demand** (no materialized table, no migration). Compute per request.
- **Governed fact types covered:** `approved_join`, `grain`, `availability_time`. Entity bridges are OUT (not governed yet); shape the read model so a 4th type slots in.
- **Statuses** (from `fold_overlay_state(...).status`): `DRAFT` / `PARTIALLY_CONFIRMED` → **pending**; `VERIFIED` → **confirmed**; `REJECTED` → **rejected**; `REVERIFY` / `STALE` → **needs_attention**.
- **RBAC:** the routes are `require_catalog_read` (a catalog read). No customer values rendered — counts, categories, signal names, scores only.
- **Source normalization:** compare/emit `source` lowercased+stripped (as the other readers do).
- **An unknown source → an empty-but-valid dashboard (all zeros), NOT a 404.** A malformed `{source}` selector → 400 (reuse the existing scoped-refs `ValueError`→422/400 handling only if a subset is accepted; the dashboard takes no subset, so a plain unknown source = zeros).
- No new DB migrations. Tests: `uv run pytest <path> -q`; frontend `cd frontend && npm test`.

## File Structure

- **Create** `src/featuregen/overlay/upload/governance_analytics.py` — the read model (all pure reads).
- **Create** `src/featuregen/api/routes/governance_dashboard.py` — two read-only routes.
- **Create** `frontend/src/screens/GovernanceDashboardScreen.tsx` — the dashboard.
- **Modify** `src/featuregen/api/app.py` (register router); `frontend/src/api.ts`, `nav.ts`, `App.tsx` (client fns + route); `frontend/src/screens/GovernanceReviewScreen.tsx` (honesty-fix copy).
- **Tests:** `tests/featuregen/overlay/upload/test_governance_analytics.py`, `tests/featuregen/api/test_governance_dashboard_routes.py`, a frontend test.

---

### Task 1: The analytics read model (`governance_analytics.py`)

**Files:**
- Create: `src/featuregen/overlay/upload/governance_analytics.py`
- Test: `tests/featuregen/overlay/upload/test_governance_analytics.py`

**Interfaces:**
- Consumes (READ these seams first): `overlay/store.load_fact`; `overlay/state.fold_overlay_state` (returns `.status ∈ {DRAFT,PARTIALLY_CONFIRMED,VERIFIED,REJECTED,REVERIFY,STALE}`); the `overlay_proposal` table (`SELECT fact_key, catalog_source, proposed_value FROM overlay_proposal WHERE fact_type = %s` — enumerates ALL facts of a type, any status; see `readiness.py:584`); the `pass_c_candidate_evidence` ledger (`SELECT catalog_source, from_ref, to_ref, fact_key, bucket, evidence_json FROM pass_c_candidate_evidence`; see `readiness.py:599`); the `human_tasks` table (`SELECT fact_key, created_at FROM human_tasks WHERE status='open'`); `overlay/facts.OVERLAY_FACT_REJECTED`; `overlay/upload/passc/types.JoinCandidateEvidenceV1` (evidence_json shape — for the calibration seed's `positive_signals`/`score`/`bucket`). The reject **category** is on the `OVERLAY_FACT_REJECTED` event payload (`payload["category"]`, nullable) — read it from the fact's stream.
- Produces:
  - Frozen dataclasses `FactTypeRollup`, `QueueHealth`, `CalibrationSeed`, `RecentActivity`, `SourceSummary`, `GovernanceDashboard` (fields per the spec §1).
  - `compute_governance_dashboard(conn, *, source: str | None = None, now=None) -> GovernanceDashboard`
  - `list_source_governance_summaries(conn, *, now=None) -> tuple[SourceSummary, ...]`

- [ ] **Step 1: Write the failing tests.** Seed governed facts across states + a ledger row, then assert the rollup. Use the passc/join_governance seeding + the table-fact propose path (read `tests/featuregen/overlay/upload/test_join_governance.py` + `test_table_fact_governance.py` for the exact seed/confirm/reject helpers; reuse them).

```python
# test_governance_analytics.py
from featuregen.overlay.upload.governance_analytics import (
    compute_governance_dashboard, list_source_governance_summaries)

def test_counts_by_type_and_status(passc_conn, seed_governed_facts):
    # seed_governed_facts: for source "src" -> 1 VERIFIED join, 1 DRAFT join, 1 REJECTED join (category "different_entity"),
    #                       1 VERIFIED grain
    dash = compute_governance_dashboard(passc_conn, source="src")
    joins = next(r for r in dash.fact_types if r.fact_type == "approved_join")
    assert joins.confirmed == 1 and joins.pending == 1 and joins.rejected == 1
    assert joins.rejected_by_category.get("different_entity") == 1
    grain = next(r for r in dash.fact_types if r.fact_type == "grain")
    assert grain.confirmed == 1

def test_calibration_seed_correlates_bucket_with_outcome(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    cs = dash.calibration_seed
    # the VERIFIED join had a 'strong' ledger row, the REJECTED join a 'strong' row -> strong: 1 confirmed, 1 rejected
    assert cs.confirm_rate_by_bucket["strong"]["confirmed"] >= 1

def test_queue_health_and_age(passc_conn, seed_governed_facts):
    dash = compute_governance_dashboard(passc_conn, source="src")
    assert dash.queue_health.open_depth >= 1
    assert dash.queue_health.oldest_pending_age_seconds is not None

def test_unknown_source_is_zeros_not_error(passc_conn):
    dash = compute_governance_dashboard(passc_conn, source="no-such-source")
    assert all(r.pending == 0 and r.confirmed == 0 and r.rejected == 0 for r in dash.fact_types)

def test_one_corrupt_fact_does_not_blank_the_dashboard(passc_conn, seed_governed_facts, seed_corrupt_proposal):
    # a proposal row whose fact stream/ref is unreadable -> skipped, the good counts still returned
    dash = compute_governance_dashboard(passc_conn, source="src")
    assert next(r for r in dash.fact_types if r.fact_type == "approved_join").confirmed >= 1

def test_cross_source_and_source_summaries(passc_conn, seed_two_sources):
    dash = compute_governance_dashboard(passc_conn, source=None)   # cross-source
    assert dash.scope == "catalog"
    sums = list_source_governance_summaries(passc_conn)
    assert len(sums) >= 2
```

Fill the `seed_*` fixtures from the real join/table-fact seeding helpers (a VERIFIED join = propose + dual-confirm; a REJECTED join = propose + reject with a category; a DRAFT = propose only; a strong ledger row is written by the Pass C propose path — or insert a `pass_c_candidate_evidence` row directly with `bucket='strong'` + an `evidence_json` = `asdict(JoinCandidateEvidenceV1(...))`).

- [ ] **Step 2: Run — FAIL** (module missing). `uv run pytest tests/featuregen/overlay/upload/test_governance_analytics.py -q`
- [ ] **Step 3: Implement `governance_analytics.py`.**
  - `_GOVERNED_FACT_TYPES = ("approved_join", "grain", "availability_time")`.
  - `_norm(s) = (s or "").strip().lower()`.
  - **Enumerate + fold, per fact type:** `SELECT fact_key, catalog_source FROM overlay_proposal WHERE fact_type=%s` (add `AND lower(btrim(catalog_source))=%s` when `source` given). For each row (in a per-row `try/except` → skip + `counters.incr("overlay.governance_analytics.fact_unreadable")`): `status = fold_overlay_state(load_fact(conn, fact_key)).status`; bucket the status per the Global Constraints mapping; if REJECTED, read the reject category from the stream (`[e for e in stream if e.type == OVERLAY_FACT_REJECTED][-1].payload.get("category") or "uncategorized"`) and increment `rejected_by_category`. Build one `FactTypeRollup` per type (always emit all 3 types, zeros if none). Reuse the loaded `stream` for both the fold and the reject-category read (don't load twice).
  - **Queue health:** for `source`, resolve the source's fact_keys (from the enumeration above) and `SELECT created_at FROM human_tasks WHERE status='open' AND fact_key = ANY(%s)`; for cross-source, `... WHERE status='open'`. `open_depth = len`; `oldest_pending_age_seconds = (now - min(created_at)).total_seconds()`; age buckets `lt_1d/1_7d/gt_7d` from each `created_at`. `now = now or datetime.now(UTC)`.
  - **Calibration seed (approved_join only):** `SELECT fact_key, bucket, evidence_json FROM pass_c_candidate_evidence WHERE fact_key IS NOT NULL` (+ source filter). For each, fold the fact's outcome (reuse the per-type fold results by fact_key — pass a `{fact_key: status}` map down so you don't re-fold); tally `confirm_rate_by_bucket[bucket]` (`confirmed`/`rejected` counts; `rate = confirmed/(confirmed+rejected)` or None); for a REJECTED join with a category, find the top-weight `positive_signal` in `evidence_json["positive_signals"]` (max `score_delta`/`weight`) and tally `reject_category_by_top_signal[top_signal][category]`. Empty dicts when no ledger data. Never raise on a malformed `evidence_json` (skip + counter).
  - **Recent activity:** count CONFIRMED/REJECTED events with `occurred_at >= now - timedelta(days=days)` (default `days=7`) across the enumerated streams, per type + a total. (Reuse the already-loaded streams.)
  - `compute_governance_dashboard`: assemble `GovernanceDashboard(scope="source" if source else "catalog", source=_norm(source) if source else None, generated_at=now.isoformat(), fact_types=..., queue_health=..., calibration_seed=..., recent_activity=...)`.
  - `list_source_governance_summaries`: `SELECT DISTINCT catalog_source FROM overlay_proposal`; per source, a compact fold (pending/confirmed/rejected counts + oldest open age) → `SourceSummary`. (May call a shared internal helper with `compute_governance_dashboard`.)
- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_governance_analytics.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/overlay/ -q`; `git add -A && git commit -m "feat(overlay): governance analytics read model (dashboard rollups + calibration seed)"`

---

### Task 2: The dashboard API routes (`governance_dashboard.py`)

**Files:**
- Create: `src/featuregen/api/routes/governance_dashboard.py`
- Modify: `src/featuregen/api/app.py` (register the router)
- Test: `tests/featuregen/api/test_governance_dashboard_routes.py`

**Interfaces:**
- Consumes: Task 1's `compute_governance_dashboard(conn, *, source=None, now=None)`, `list_source_governance_summaries(conn, *, now=None)`; `api/deps` (`get_conn`, `require_catalog_read`). **`api/routes/readiness.py` is the template** (read-only, `require_catalog_read`, `asdict` serialization).
- Produces: `GET /governance/dashboard` (cross-source + `sources`) and `GET /sources/{source}/governance/dashboard`.

- [ ] **Step 1: Failing test.**

```python
# test_governance_dashboard_routes.py — reuse the api conftest (client, X-Roles headers) + the joins/table-fact seeding
def _h(roles="catalog_viewer"): return {"X-User":"u","X-Roles":roles}

def test_cross_source_dashboard(client, seed_governed_facts_via_api):
    r = client.get("/governance/dashboard", headers=_h())
    assert r.status_code == 200
    body = r.json()
    assert "fact_types" in body and "sources" in body

def test_per_source_dashboard(client, seed_governed_facts_via_api):
    source = seed_governed_facts_via_api
    r = client.get(f"/sources/{source}/governance/dashboard", headers=_h())
    assert r.status_code == 200
    assert r.json()["source"] == source

def test_unknown_source_is_zeros_not_404(client):
    r = client.get("/sources/nope/governance/dashboard", headers=_h())
    assert r.status_code == 200
    assert all(ft["pending"] == 0 for ft in r.json()["fact_types"])

def test_requires_catalog_read(client):
    r = client.get("/governance/dashboard", headers={"X-User":"u","X-Roles":"none"})
    assert r.status_code == 403
```

Reuse the readiness/governance route tests' harness + a VERIFIED-join seed. Confirm which role lacks `catalog:read` (`identity/permissions.py`).

- [ ] **Step 2: Run — FAIL** (routes missing). `uv run pytest tests/featuregen/api/test_governance_dashboard_routes.py -q`
- [ ] **Step 3: Implement** (mirror `readiness.py`):

```python
from dataclasses import asdict
from fastapi import APIRouter, Depends
from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.overlay.upload.governance_analytics import (
    compute_governance_dashboard, list_source_governance_summaries)

router = APIRouter()

@router.get("/governance/dashboard", dependencies=[Depends(require_catalog_read)])
def dashboard(conn=Depends(get_conn)):
    dash = compute_governance_dashboard(conn, source=None)
    return {**asdict(dash), "sources": [asdict(s) for s in list_source_governance_summaries(conn)]}

@router.get("/sources/{source}/governance/dashboard", dependencies=[Depends(require_catalog_read)])
def source_dashboard(source: str, conn=Depends(get_conn)):
    return asdict(compute_governance_dashboard(conn, source=source))
```

If `asdict` yields a non-JSON-safe value (it won't — all fields are str/int/float/dict/nested dataclass), add a small `_json` normalizer. Register in `app.py`: `from featuregen.api.routes import governance_dashboard` + `app.include_router(governance_dashboard.router)`.

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/api/test_governance_dashboard_routes.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/api/ -q`; `git add -A && git commit -m "feat(api): read-only governance dashboard routes"`

---

### Task 3: The dashboard screen + the honesty fix

**Files:**
- Create: `frontend/src/screens/GovernanceDashboardScreen.tsx`
- Modify: `frontend/src/api.ts`, `frontend/src/nav.ts`, `frontend/src/App.tsx`, `frontend/src/screens/GovernanceReviewScreen.tsx`
- Test: `frontend/src/screens/GovernanceDashboardScreen.test.tsx`

**Interfaces:**
- Consumes: the Task-2 routes. **Mirror `GovernanceReviewScreen.tsx`** (the source input, `request`/fetch, `gj-*`/badge/card styling, `ApiError` handling) + `readiness.py`'s api.ts client fns.
- Produces: a `dashboard` route rendering the screen.

- [ ] **Step 1: api.ts fns + types.** Add (mirror `listRelationshipReadiness`):
```ts
export interface FactTypeRollup { fact_type: string; pending: number; confirmed: number; rejected: number; needs_attention: number; rejected_by_category: Record<string, number>; }
export interface GovernanceDashboard { scope: string; source: string | null; generated_at: string;
  fact_types: FactTypeRollup[]; queue_health: {open_depth:number; oldest_pending_age_seconds:number|null; age_buckets:Record<string,number>};
  calibration_seed: {confirm_rate_by_bucket: Record<string,{confirmed:number;rejected:number;rate:number|null}>; reject_category_by_top_signal: Record<string,Record<string,number>>};
  recent_activity: {days:number; confirmed:number; rejected:number};
  sources?: {source:string; pending:number; confirmed:number; rejected:number; oldest_pending_age_seconds:number|null}[]; }
export const getGovernanceDashboard = () => request<GovernanceDashboard>(`/governance/dashboard`);
export const getSourceGovernanceDashboard = (source: string) => request<GovernanceDashboard>(`/sources/${encodeURIComponent(source)}/governance/dashboard`);
```
- [ ] **Step 2: The screen.** `GovernanceDashboardScreen.tsx`: on mount fetch `getGovernanceDashboard()` (cross-source). Render: **summary cards** per `fact_type` (pending/confirmed/rejected/needs_attention, reusing the severity badge colors); a **rejected-by-category** list per type; **queue health** (open depth + oldest-age humanized + the age buckets); the **calibration seed** — a compact "confirm rate by bucket" table (strong vs weak) clearly labeled *"observation — signal vs. outcome; tuning is a later step"*; and a **cross-source table** from `sources[]` (each row → a link/button that refetches `getSourceGovernanceDashboard(source)` to scope the view). Reuse `gj-*`/card/badge classes; no new CSS system. Standard `ApiError` handling.
- [ ] **Step 3: Register the route.** `nav.ts`: add `'dashboard'` to `Route`/`ROUTES` + `ICONS`. `App.tsx`: import `GovernanceDashboardScreen`, add a `PAGES` entry (`{route:'dashboard', label:'Dashboard', eyebrow:'Governance', title:'Governance dashboard', description:'Pipeline rollups + outcomes.'}`) + a `{route === 'dashboard' && <GovernanceDashboardScreen/>}` branch. Update any nav-count test that asserts the number of nav items.
- [ ] **Step 4: The honesty fix.** In `GovernanceReviewScreen.tsx`, replace the copy that claims the reject category "feeds back into re-proposal" (grep for `feed`/`re-propos` — the reject-box helper text, ~4 sites) with an accurate line, e.g. *"Recorded and surfaced on the governance dashboard."* Do NOT change any reject behavior — copy only.
- [ ] **Step 5: Test + run.** `GovernanceDashboardScreen.test.tsx`: mock `getGovernanceDashboard` → one fact type with `confirmed:2, rejected:1, rejected_by_category:{different_entity:1}` + a `sources` row → assert the summary card shows the counts, the category renders, and the cross-source row renders. `cd frontend && npm test` + `npm run typecheck` + `npm run build`. Also assert (in the review screen's test or a grep) the old false "feeds back" string is gone.
- [ ] **Step 6: Commit.** `git add -A && git commit -m "feat(frontend): governance dashboard screen + correct the reject-category copy"`

---

## Self-Review

**Spec coverage:** §1 read model → Task 1 (counts/reject-by-category/queue/calibration-seed/recent-activity/source-summaries all covered); §2 API → Task 2; §3 frontend → Task 3; §4 honesty fix → Task 3 step 4; error handling (fail-soft, unknown-source-zeros) → Task 1 tests + Task 2 test; testing → each task; security (read-only, catalog_read, no values) → Task 2. Acceptance 1→Task 2, 2→Task 1, 3→Task 3, 4→Task 1/2, 5→all (no migration, no write). No gap.

**Placeholder scan:** the `seed_*` fixtures say "fill from the real join/table-fact seeding helpers" — the assertions are concrete; the seed lines are completed from named existing helpers (the same pattern every prior plan used). Route + read-model code is complete. No logic hand-waved.

**Type consistency:** `compute_governance_dashboard(conn, *, source=None, now=None) -> GovernanceDashboard`, `list_source_governance_summaries(conn, *, now=None)`, the dataclasses (`FactTypeRollup`/`QueueHealth`/`CalibrationSeed`/`RecentActivity`/`SourceSummary`/`GovernanceDashboard`) — used identically in Tasks 1/2, and the TS interfaces in Task 3 mirror the asdict shape. `_GOVERNED_FACT_TYPES` fixed at 3 types.

## Execution Handoff

Build **subagent-driven** (Fable implementers, Opus reviewers, whole-branch review before merge) on branch `phase4-governance-dashboard`.
