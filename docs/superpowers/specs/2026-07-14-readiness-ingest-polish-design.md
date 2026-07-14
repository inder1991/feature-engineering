# Readiness Visibility + Unrecognized-Headers Fix — Design

**Date:** 2026-07-14
**Status:** Approved direction (Tier-1 polish), pre-implementation
**Depends on:** the governed ingestion loop, merged to `main` at `861a14d`

## Goal

Two small, independent Tier-1 polish items that round out the governed ingestion experience:

1. **Readiness visibility** — (a) wire the FeatureReadiness "join" dimension to real `approved_join` state (it is hard-wired static-blocked today), and (b) expose the per-table `RelationshipReadiness` view (built in Phase 3A, unrouted) via an API route, so a reviewer can SEE what relationships were discovered/confirmed and what each table still needs.
2. **Unrecognized-headers fix** — when a CSV's headers aren't recognized (all rows quarantine, nothing usable), stop returning a misleading `status="ingested"` with an empty graph; return an honest status and preserve any existing graph.

These share nothing but a branch; kept together only to reduce merge churn.

## Item 1 — Readiness visibility

### Problem

- The FeatureReadiness "join" requirement (`readiness.py:82-90`, `_PHASE1_UNPROMOTED = (…, ("join","approved_join"))`) is NOT in `_FACT_TYPE_BY_REQUIREMENT`, so `_table_fact_status` returns `("missing", CAUSE_NOT_PROMOTED)` for it on EVERY table → `operational_status` reads "blocked on joins" for every table regardless of real state. The code comment says "Phase 3 owns approved_join state" — Phase 3A shipped, so it can now be wired.
- `compute_relationship_readiness(conn, *, source, subset=None) -> tuple[RelationshipReadiness, ...]` (`readiness.py:575`) already folds real per-table join state into `RelationshipStatus ∈ {no_candidates, candidate_proposed, weak_candidates_only, confirmed, conflicting}` — but NO API route exposes it (grep of `api/` finds no readiness route at all).

### §1a — Wire the "join" requirement to real relationship state

Make the "join" requirement reflect the table's `RelationshipStatus` instead of a static "missing", and — critically — stop it being a universal hard blocker (not every table needs a join). Mapping (RelationshipStatus → the 4-value `ReadinessRequirement.status` + blocking):
- `CONFIRMED` → `confirmed` (satisfied, non-blocking).
- `CANDIDATE_PROPOSED` → `proposed` (a review requirement — shown, non-blocking — like a Pass B pending grain).
- `WEAK_CANDIDATES_ONLY` → `proposed`/advisory, non-blocking (weak diagnostic).
- `CONFLICTING` → `conflicting`, **blocking** (a genuine irreconcilable pair a human must resolve — the ONE join case that should block).
- `NO_CANDIDATES` → non-blocking. A table with no discovered relationships is not broken; surface it as an advisory "no relationships discovered" note (cause `CAUSE_NOT_PROMOTED` retained), NOT a blocker.

Net effect: tables stop being falsely "blocked on joins"; only a real CONFLICTING relationship blocks; everything else is informational and reflects live state. Implement by giving the "join" requirement its own status derivation in `_table_fact_status` (or a small sibling) that calls the existing per-table relationship fold (reuse `compute_relationship_readiness` for the table, or its internal helper, so there is ONE source of truth for relationship status — do not re-derive). Keep the 4-value `ReadinessRequirement.status` vocabulary; `RelationshipStatus` maps INTO it (a separate CONFLICTING→conflicting bridge). The DISTINCT 5-value `RelationshipReadiness` view is unchanged (it stays the rich per-table detail; this only fixes the coarse FeatureReadiness gate).

### §1b — Expose readiness via API routes

New read-only routes (RBAC `require_catalog_read` — readiness is a catalog read, not a governance action):
- `GET /sources/{source}/readiness/relationships?subset=` → `compute_relationship_readiness(conn, source=source, subset=subset)` → `{source, relationships: [{scope, source, schema, table, status, confirmed_pairs, proposed_pairs, weak_pairs, conflicting_pairs}]}`.
- `GET /sources/{source}/readiness?subset=` → `compute_readiness(conn, source=source, subset=subset)` → the `FeatureReadiness` view (operational_status + blocking/review requirements + summary_scores) so the coarse gate (now join-aware) is visible too.
Both are thin transport over the existing read-only compute fns; `subset` is the existing schema-aware selector. A new `api/routes/readiness.py` module (mirror `quarantine.py`'s shape) registered in `app.py`.

### §1c — Frontend (light)

A small **Readiness** view/panel so the numbers are visible. Minimal: a per-table relationship-status list (a badge per table: confirmed / proposed / weak / conflicting / none) fetched from `GET .../readiness/relationships`, reachable from the governance screen or as its own light screen. Scope kept small — the API is the load-bearing deliverable; the frontend is a thin reader. May be deferred to a follow-up if it grows; the route + the join-dim wiring are the committed core.

## Item 2 — Unrecognized-headers fix

### Problem

`validate_rows` (`canonical.py:62`) emits `structural_error` only for `not rows` (empty) or `all(not r.source for r in rows)` (no source). A CSV whose rows have a source but whose headers don't map to `table/column/type` (or a glossary whose FQNs are all unresolvable) → every row quarantines → `vr.good == []` → but `structural_error is None`, so `ingest_upload` proceeds, `build_graph` runs on empty `vr.good` (which DELETEs + rebuilds → **wipes any existing graph to empty**), and returns `IngestResult("ingested", …)` with `asserted=0`. A misleading "success" that also destroys an existing catalog.

### Fix

In `ingest_upload` (`ingest.py`), after `validate_rows` + the large-change brake, add: if `not vr.good and vr.quarantined` (rows existed but NONE were usable), **persist the quarantine** (so the reviewer can see the specific rows) and **return early** with an honest non-success status — `IngestResult("rejected", "<clear reason>", asserted=0, staled=0, quarantined=len(vr.quarantined))`. The reason names the likely cause: "no rows could be ingested — all N quarantined (check the file's headers include table/column/type, or that the FQNs resolve)".

Key properties:
- **Returns BEFORE `build_graph`**, so a garbage/unrecognized upload NEVER wipes an existing graph (mirrors the existing `structural_error` early-return, which also preserves the graph). This is the more important half of the fix.
- Fires ONLY when `vr.good` is completely empty — a partial upload (some good, some quarantined) still ingests normally (`ingested`) with the quarantine reviewable.
- Correctly covers the glossary-all-unresolvable case (an unusable glossary → honest rejected, not a silent empty graph).
- Placed AFTER the brake so a held upload still reports `held`.

Reuse `"rejected"` (an existing non-success terminal) rather than inventing a status enum value — but unlike the structural-error `rejected`, this path PERSISTS the quarantine first (like the `held` path) so the rows are reviewable. The `IngestResult.message` carries the clear cause.

## What is reused (no change)

- `overlay/upload/readiness.py`: `compute_relationship_readiness`, `compute_readiness`, `RelationshipReadiness`/`RelationshipStatus`, `_table_fact_status`, `_PHASE1_UNPROMOTED`, `_scoped_refs` (subset selector).
- `overlay/upload/canonical.py:validate_rows` (unchanged — the fix reads its `vr.good`/`vr.quarantined`); `overlay/upload/ingest.py:ingest_upload` (the fix adds one early-return branch); `persist_quarantine`; `IngestResult`.
- API: `api/routes/quarantine.py` (route-module template), `api/deps.require_catalog_read`, `app.py` router registration.
- Frontend: `api.ts` (`request`), the screen/nav pattern.

## Architecture

- **New** `api/routes/readiness.py` — the two read-only readiness routes.
- **Modify** `overlay/upload/readiness.py` — §1a join-dim wiring (a small, contained change to the join requirement's status derivation).
- **Modify** `overlay/upload/ingest.py` — the item-2 early-return branch.
- **Modify** `app.py` — register the readiness router.
- **New/modify** frontend — the light readiness reader (api.ts fn + a small view).

## Testing

**Item 1 (pytest):**
- §1a: a source/table with NO approved_join → the FeatureReadiness "join" requirement is NON-blocking (operational_status not "blocked" solely because of joins). A table with a VERIFIED join → "join" confirmed. A table with a DRAFT proposal → "join" proposed (review, non-blocking). A table with a CONFLICTING pair → "join" conflicting + BLOCKING. (Reuse the passc/join_governance seeding to create each state.)
- §1b: `GET .../readiness/relationships` returns the per-table statuses for a source with a seeded VERIFIED join (→ confirmed) + a proposed one (→ candidate_proposed) + another source excluded; `subset` narrows to a table. `GET .../readiness` returns the FeatureReadiness view with the join dimension now reflecting real state. RBAC: `catalog_read` allowed; without it 403.

**Item 2 (pytest):**
- An upload whose rows all quarantine (unrecognized headers — rows with a source but no table/column/type) → `IngestResult.status == "rejected"` with a clear message + the quarantine persisted (reviewable) + `asserted == 0`. Prove it does NOT return "ingested".
- **Graph preservation:** ingest a good catalog (non-empty graph), then re-upload an all-quarantine file for the same source → the existing graph is UNCHANGED (not wiped to empty). Prove this fails pre-fix (build_graph wipes it).
- A partial upload (some good rows) still returns "ingested" and ingests the good rows (regression guard — the fix must NOT fire when any row is usable).
- A glossary with all-unresolvable FQNs → honest "rejected" (not silent empty graph).

**Frontend:** a light render test of the readiness reader (renders a table's relationship status badge from a mocked response).

## Acceptance criteria

1. The FeatureReadiness "join" requirement reflects real relationship state: non-blocking except a CONFLICTING pair; a table with no joins is not falsely "blocked."
2. `GET /sources/{source}/readiness/relationships` returns the per-table `RelationshipReadiness` (catalog_read-gated); `GET /sources/{source}/readiness` returns the join-aware FeatureReadiness.
3. An all-quarantine (unrecognized-headers) upload returns an honest non-success status with a clear message + persisted quarantine, and NEVER wipes an existing graph.
4. A partial upload still ingests its good rows (no regression); a glossary-all-unresolvable upload is honestly rejected.
5. No new DB migrations; non-governance/normal-upload flows unchanged; readiness routes are read-only.

## Build hygiene

Branch `readiness-ingest-polish` off `main` (`861a14d`). Subagent-driven (Fable implementers, Opus reviewers) + a whole-branch review before merge.
