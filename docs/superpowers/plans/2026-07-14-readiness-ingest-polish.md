# Readiness Visibility + Unrecognized-Headers Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Two Tier-1 polish items — (1) make the readiness "join" dimension reflect real approved_join state + expose the readiness views via API, and (2) return an honest status (and preserve the existing graph) when a CSV's headers aren't recognized.

**Architecture:** A small change to `overlay/upload/readiness.py` (join-dim wiring), a new read-only `api/routes/readiness.py` (two routes over the existing compute fns), a one-branch early-return in `overlay/upload/ingest.py` (the unrecognized-headers fix), and a light frontend readiness reader. No new DB migrations. **`api/routes/quarantine.py` is the route-module template.**

**Tech Stack:** Python 3.11 · FastAPI · psycopg3 · pytest (+ pytest-postgresql) · React/TS · vitest · uv.

**Spec:** `docs/superpowers/specs/2026-07-14-readiness-ingest-polish-design.md`. Every task's requirements implicitly include it.

## Global Constraints

- Readiness routes are READ-ONLY, RBAC `require_catalog_read` (a catalog read, not a governance action).
- The join-dim fix must NOT make a table "blocked" solely for having no joins — only a CONFLICTING relationship blocks; CONFIRMED/CANDIDATE_PROPOSED/WEAK/NO_CANDIDATES are non-blocking.
- ONE source of truth for relationship status — reuse `compute_relationship_readiness` (or its internal fold), do NOT re-derive.
- The unrecognized-headers fix fires ONLY when `vr.good == [] and vr.quarantined` (rows existed, none usable); it returns BEFORE `build_graph` (so an existing graph is never wiped) and PERSISTS the quarantine first. A partial upload (any good row) is unaffected.
- No new DB migrations; normal-upload + non-governance flows unchanged.
- Tests: `uv run pytest <path> -q`; frontend `cd frontend && npm test`.

## File Structure

- **Create** `src/featuregen/api/routes/readiness.py` — two read-only routes.
- **Modify** `src/featuregen/overlay/upload/readiness.py` — join-dim wiring (§1a).
- **Modify** `src/featuregen/overlay/upload/ingest.py` — the unrecognized-headers early-return (item 2).
- **Modify** `src/featuregen/api/app.py` — register the readiness router.
- **Modify/Create** frontend `api.ts` + a light readiness view.
- **Tests:** `tests/featuregen/overlay/upload/test_readiness_join_dim.py` (or extend the readiness tests), `tests/featuregen/api/test_readiness_routes.py`, `tests/featuregen/overlay/upload/test_ingest_unrecognized_headers.py` (or extend an ingest test), a frontend test.

---

### Task 1: Unrecognized-headers ingest fix (honest status + graph preserved)

**Files:**
- Modify: `src/featuregen/overlay/upload/ingest.py` (`ingest_upload`, right after `validate_rows` + the large-change brake, before `_table_facts`/`build_graph`)
- Test: `tests/featuregen/overlay/upload/test_ingest_unrecognized_headers.py`

**Interfaces:**
- Consumes: `validate_rows` (returns `ValidationResult{good, quarantined, structural_error}`), `persist_quarantine(conn, catalog_source, quarantined)`, `IngestResult(status, message, asserted, staled, quarantined)`. **READ `ingest_upload` (`ingest.py:691` onward) fully** — the existing `if vr.structural_error: return IngestResult("rejected", …)` early-return (`:700-701`) and the brake `held` return (`:708-710`) show the exact pattern + where persist_quarantine is called.

- [ ] **Step 1: Write the failing tests.**

```python
# test_ingest_unrecognized_headers.py — drive ingest_upload with rows that all quarantine.
from featuregen.overlay.upload.ingest import ingest_upload
from featuregen.overlay.upload.canonical import CanonicalRow

def _unrecognized_rows(src):   # rows with a source but NO table/column/type (headers didn't map)
    return [CanonicalRow(source=src, table="", column="", type="")]

def test_all_quarantine_returns_rejected_not_ingested(db_conn):
    res = ingest_upload(db_conn, "src", _unrecognized_rows("src"))
    assert res.status == "rejected"            # honest, not "ingested"
    assert res.asserted == 0
    assert res.quarantined >= 1
    assert "quarantin" in (res.message or "").lower() or "recogni" in (res.message or "").lower()

def test_all_quarantine_preserves_existing_graph(db_conn):
    # 1. ingest a good catalog -> non-empty graph
    good = [CanonicalRow(source="src", table="t", column="c", type="text")]
    ingest_upload(db_conn, "src", good)
    before = db_conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source='src'").fetchone()[0]
    assert before > 0
    # 2. re-upload an all-quarantine file for the same source
    ingest_upload(db_conn, "src", _unrecognized_rows("src"))
    after = db_conn.execute("SELECT count(*) FROM graph_node WHERE catalog_source='src'").fetchone()[0]
    assert after == before                     # graph UNCHANGED (not wiped)

def test_partial_upload_still_ingests_good_rows(db_conn):
    rows = [CanonicalRow(source="src2", table="t", column="c", type="text"),
            CanonicalRow(source="src2", table="", column="", type="")]   # one good, one bad
    res = ingest_upload(db_conn, "src2", rows)
    assert res.status == "ingested"            # partial success still ingests
```

Read the existing ingest tests' conftest for the real `db_conn`/connection fixture + how they call `ingest_upload` (the seeding used by `test_ingest*`); reuse it. Confirm the CanonicalRow constructor args.

- [ ] **Step 2: Run — the first two FAIL** (`test_all_quarantine_returns_rejected_not_ingested` gets "ingested"; `test_all_quarantine_preserves_existing_graph` sees the graph wiped to 0). `uv run pytest tests/featuregen/overlay/upload/test_ingest_unrecognized_headers.py -q`
- [ ] **Step 3: Implement.** In `ingest_upload`, after the brake `held` check and before `_table_facts`/`build_graph`:

```python
if not vr.good and vr.quarantined:
    # Every row quarantined -> nothing usable. Persist the quarantine so the reviewer can see the
    # rows, and return an HONEST non-success status. Crucially, return BEFORE build_graph so a
    # garbage/unrecognized-headers upload NEVER wipes an existing graph (mirrors the structural-error
    # early-return above).
    persist_quarantine(conn, catalog_source, vr.quarantined)
    return IngestResult(
        "rejected",
        f"no rows could be ingested — all {len(vr.quarantined)} quarantined "
        f"(check the file's headers include table/column/type, or that the FQNs resolve)",
        0, 0, len(vr.quarantined))
```

Confirm the exact `IngestResult(...)` positional shape + the `persist_quarantine` signature against the existing calls. Place it AFTER the brake return so a held upload still reports `held`.

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_ingest_unrecognized_headers.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/overlay/ -q` (fix any existing test that ASSERTED the old all-quarantine→"ingested" behavior — the old behavior was the bug; update it to expect "rejected"). `git add -A && git commit -m "fix(ingest): honest status + preserve existing graph when no rows are recognized"`

---

### Task 2: Wire the FeatureReadiness "join" dimension to real relationship state

**Files:**
- Modify: `src/featuregen/overlay/upload/readiness.py`
- Test: `tests/featuregen/overlay/upload/test_readiness_join_dim.py`

**Interfaces:**
- Consumes: `compute_relationship_readiness(conn, *, source, subset=None) -> tuple[RelationshipReadiness,...]` (`:575`), `RelationshipStatus` (`:455`), `_table_fact_status` (`:100`), the requirement-building loop (`:360-440` — READ it: how `_PHASE1_UNPROMOTED` entries become `ReadinessRequirement`s and how a requirement is classified blocking vs review from its `status`). `ReadinessRequirement.status ∈ {confirmed, proposed, missing, conflicting}`.

- [ ] **Step 1: Write the failing tests.** Seed each relationship state via the passc/join_governance seeding (reuse `tests/featuregen/overlay/upload/test_join_governance.py` helpers or the passc conftest), then compute readiness for the table:

```python
# test_readiness_join_dim.py
from featuregen.overlay.upload.readiness import compute_readiness

def _join_req(fr):   # the "join" ReadinessRequirement out of a FeatureReadiness
    reqs = list(fr.blocking_requirements) + list(fr.review_requirements)
    return next((r for r in reqs if r.name == "join"), None)   # confirm the requirement's name/id field

def test_no_joins_is_not_blocking(db_conn, seeded_table_no_joins):
    fr = compute_readiness(db_conn, source="src", subset="t")
    jr = _join_req(fr)
    # a table with no relationships must NOT appear as a blocking join requirement
    assert jr is None or jr not in fr.blocking_requirements

def test_verified_join_is_confirmed(db_conn, seeded_verified_join):   # table with a VERIFIED approved_join
    fr = compute_readiness(db_conn, source="src", subset="t")
    jr = _join_req(fr)
    assert jr is not None and jr.status == "confirmed" and jr not in fr.blocking_requirements

def test_proposed_join_is_review_not_blocking(db_conn, seeded_proposed_join):
    fr = compute_readiness(db_conn, source="src", subset="t")
    jr = _join_req(fr)
    assert jr is not None and jr.status == "proposed" and jr not in fr.blocking_requirements

def test_conflicting_join_blocks(db_conn, seeded_conflicting_join):   # two active fact_keys, same column pair
    fr = compute_readiness(db_conn, source="src", subset="t")
    jr = _join_req(fr)
    assert jr is not None and jr.status == "conflicting" and jr in fr.blocking_requirements
```

Read how `compute_readiness`/`ReadinessRequirement` name/identify the "join" requirement (the `.name`/`.requirement`/id field) and how blocking vs review is split — adjust the assertions to the real field names. Reuse the `test_join_governance.py` seeding for verified/proposed/conflicting states.

- [ ] **Step 2: Run — FAIL** (currently the join requirement is always `missing` + blocking regardless of state). `uv run pytest tests/featuregen/overlay/upload/test_readiness_join_dim.py -q`
- [ ] **Step 3: Implement.** Give the "join" requirement its own status derivation (in `_table_fact_status` add a `requirement == "join"` branch, OR a small `_join_requirement_status(conn, source, table)` the loop calls for the join entry). It must:
  - Compute the table's `RelationshipStatus` via `compute_relationship_readiness(conn, source=source, subset=<the table>)` (take the single table's entry; ONE source of truth — do not re-fold the event log yourself).
  - Map: `CONFIRMED → ("confirmed", CAUSE_NOT_PROMOTED)`; `CANDIDATE_PROPOSED`/`WEAK_CANDIDATES_ONLY → ("proposed", CAUSE_PROPOSED_UNCONFIRMED)`; `CONFLICTING → ("conflicting", CAUSE_INGESTION_ERROR)`; `NO_CANDIDATES → ("confirmed" or a non-blocking sentinel, CAUSE_NOT_PROMOTED)` — a table with no joins must NOT be a blocker (verify which status keeps it out of `blocking_requirements`; read the requirement-building loop's blocking rule — likely `status == "conflicting"` (and/or "missing") blocks, "proposed" → review, "confirmed" → satisfied. Pick the NO_CANDIDATES mapping that yields non-blocking, and add a comment).
  - Keep the DISTINCT 5-value `RelationshipReadiness` view untouched — this only fixes the coarse FeatureReadiness "join" gate. Update the `_PHASE1_UNPROMOTED`/`Phase 3 owns approved_join state` comments to reflect that the join dim is now wired.
- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/overlay/upload/test_readiness_join_dim.py -q`
- [ ] **Step 5: Regression + commit.** `uv run pytest tests/featuregen/overlay/ -q` (existing readiness tests may assert the old always-blocking join behavior — update them to the wired behavior; the old behavior was the noise this fixes). `git add -A && git commit -m "feat(readiness): wire the join dimension to real approved_join state (non-blocking except conflicting)"`

---

### Task 3: Readiness API routes

**Files:**
- Create: `src/featuregen/api/routes/readiness.py`
- Modify: `src/featuregen/api/app.py` (register the router)
- Test: `tests/featuregen/api/test_readiness_routes.py`

**Interfaces:**
- Consumes: `compute_relationship_readiness(conn, *, source, subset=None)`, `compute_readiness(conn, *, source, subset=None) -> FeatureReadiness` (`readiness.py:312`); `api/deps` (`get_conn`, `require_catalog_read`). **`api/routes/quarantine.py` is the template.** The `RelationshipReadiness`/`FeatureReadiness` dataclasses are frozen — serialize via `dataclasses.asdict` (confirm they're JSON-safe: enums → `.value`, tuples → lists; add a small serializer if `asdict` leaves a StrEnum/tuple that FastAPI can't encode — a StrEnum IS a str so it's usually fine).

- [ ] **Step 1: Failing test.**

```python
# test_readiness_routes.py — reuse the api conftest (client, X-Roles headers)
def _h(roles="catalog_viewer"): return {"X-User":"u","X-Roles":roles}

def test_relationships_route_lists_table_status(client, seed_verified_join_via_api):
    source = seed_verified_join_via_api
    r = client.get(f"/sources/{source}/readiness/relationships", headers=_h())
    assert r.status_code == 200
    rels = r.json()["relationships"]
    assert any(x["status"] == "confirmed" for x in rels)

def test_readiness_route_returns_feature_readiness(client, seed_source):
    r = client.get(f"/sources/{seed_source}/readiness", headers=_h())
    assert r.status_code == 200
    assert "operational_status" in r.json()

def test_readiness_requires_catalog_read(client, seed_source):
    # a principal with NO catalog:read permission -> 403 (use a role that lacks it, or no roles)
    r = client.get(f"/sources/{seed_source}/readiness/relationships", headers={"X-User":"u","X-Roles":"none"})
    assert r.status_code == 403
```

Reuse the joins/table-fact route tests' harness + seeding (a VERIFIED join via the propose+confirm path). Confirm which role lacks `catalog:read` for the 403 test (read `identity/permissions.py`).

- [ ] **Step 2: Run — FAIL** (routes missing). `uv run pytest tests/featuregen/api/test_readiness_routes.py -q`
- [ ] **Step 3: Implement `readiness.py`** (mirror `quarantine.py`):

```python
from dataclasses import asdict
from fastapi import APIRouter, Depends, Query
from featuregen.api.deps import get_conn, require_catalog_read
from featuregen.overlay.upload.readiness import compute_readiness, compute_relationship_readiness

router = APIRouter()

@router.get("/sources/{source}/readiness/relationships", dependencies=[Depends(require_catalog_read)])
def relationships(source: str, conn=Depends(get_conn), subset: str | None = Query(default=None)):
    rels = compute_relationship_readiness(conn, source=source, subset=subset)
    return {"source": source.strip().lower(), "relationships": [asdict(r) for r in rels]}

@router.get("/sources/{source}/readiness", dependencies=[Depends(require_catalog_read)])
def readiness(source: str, conn=Depends(get_conn), subset: str | None = Query(default=None)):
    return asdict(compute_readiness(conn, source=source, subset=subset))
```

If `asdict` yields something FastAPI can't JSON-encode (a nested non-dataclass, a set), add a small `_json(...)` normalizer (tuples→lists, StrEnum→str, sets→sorted lists). Register in `app.py`: `from featuregen.api.routes import readiness` + `app.include_router(readiness.router)` beside the others.

- [ ] **Step 4: Run — PASS.** `uv run pytest tests/featuregen/api/test_readiness_routes.py -q`
- [ ] **Step 5: Commit.** `uv run pytest tests/featuregen/api/ -q`; `git add -A && git commit -m "feat(api): read-only readiness routes — relationship + feature readiness"`

---

### Task 4: Light frontend readiness reader

**Files:**
- Modify: `frontend/src/api.ts`, `frontend/src/screens/GovernanceReviewScreen.tsx` (or a new small screen — see below)
- Test: a vitest case

**Interfaces:**
- Consumes: `GET /sources/{source}/readiness/relationships` (Task 3). Mirror `api.ts`'s `request` pattern. The governance screen already has a source input — a small "Readiness" section/panel there is the least-scope home.

- [ ] **Step 1: api.ts fn.**
```ts
export interface RelationshipReadiness { source: string; schema: string; table: string; status: string;
  confirmed_pairs: string[]; proposed_pairs: string[]; weak_pairs: string[]; conflicting_pairs: string[]; }
export const listRelationshipReadiness = (source: string) =>
  request<{source: string; relationships: RelationshipReadiness[]}>(`/sources/${encodeURIComponent(source)}/readiness/relationships`);
```
- [ ] **Step 2: A light readiness panel.** In `GovernanceReviewScreen` add a small "Relationship readiness" summary (fetched with the existing source, alongside the queues) OR a compact per-table status list: one row per table with a status badge (confirmed / candidate_proposed / weak / conflicting / no_candidates) + the pair counts. Read-only, no actions. Reuse the existing badge/`gj-*` styling; keep it small. If it doesn't fit the governance screen cleanly, a tiny standalone `ReadinessScreen` + nav entry is fine — but prefer the least-scope option.
- [ ] **Step 3: Test.** A vitest case: mock `listRelationshipReadiness` → one table `status:'confirmed'` → the panel renders the table + a "confirmed" badge.
- [ ] **Step 4: Run.** `cd frontend && npm test` + `npm run typecheck` + `npm run build`.
- [ ] **Step 5: Commit.** `git add -A && git commit -m "feat(frontend): relationship-readiness reader"`

---

## Self-Review

**Spec coverage:** §1a → Task 2; §1b → Task 3; §1c → Task 4; item 2 → Task 1. Acceptance 1→Task 2, 2→Task 3, 3+4→Task 1, 5→all (no migrations, read-only routes). No gap.

**Placeholder scan:** the test `_join_req`/field-name notes explicitly say "confirm the real field name" — the assertions are concrete once the implementer reads the `ReadinessRequirement` shape (which they must, to wire it correctly). Route code is complete. Ingest fix code is complete.

**Type consistency:** `compute_relationship_readiness(conn, *, source, subset)`, `compute_readiness(conn, *, source, subset)`, `RelationshipStatus`, `IngestResult(status, message, asserted, staled, quarantined)`, `persist_quarantine` — used consistently. The join-dim mapping (RelationshipStatus → ReadinessRequirement.status) is stated once (Task 2) and the routes serialize the existing dataclasses.

## Execution Handoff

Build **subagent-driven** (Fable implementers, Opus reviewers, whole-branch review before merge) on branch `readiness-ingest-polish`.
