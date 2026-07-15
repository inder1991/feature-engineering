# Phase 4 — Governance Dashboard (observability foundation) — Design

**Date:** 2026-07-15
**Status:** Approved design, pre-implementation
**Depends on:** the governed ingestion pipeline (Pass C joins, Pass B grain/availability) + the confirm surfaces, all on `origin/main`

## Goal

Give operators a **read-only governance dashboard** — cross-source and per-source rollups of the governed pipeline's state and outcomes — so the machinery that is currently a set of undifferentiated per-item queues becomes **observable**. This is the first slice of Phase 4 (*"calibration + prioritized HITL + dashboards"*); it builds the analytics read model that the later calibration and prioritization slices consume.

## The problem this closes

The governed pipeline is rich but almost entirely un-observable (verified by a codebase survey):
- The HITL queue is read `ORDER BY created_at DESC` and nothing else — no rollup, no "how many pending / confirmed / rejected."
- The structured **reject categories** (`different_entity`, `wrong_cardinality`, …) are **write-only** — stamped on `OVERLAY_FACT_REJECTED` and never read back or aggregated.
- The Pass C **evidence** (`JoinCandidateEvidenceV1` — score, weighted signals, bucket) is fully persisted in the `pass_c_candidate_evidence` ledger, but **nothing ever joins it against the outcome** (confirmed vs rejected) — so "which signals over-fire" is unanswerable today.
- Counters are **in-process, reset on restart**, mostly failure telemetry; there are no confirmed/rejected outcome counters. `/metrics` dumps them raw; the frontend doesn't consume it.
- **No dashboard, no aggregate API, no calibration UI exists** — every route returns a per-item list or a single-scope diagnostic.

All three Phase-4 pillars need the same missing thing: a read model that joins the queue + the confirm/reject events (with category) + the evidence ledger by `fact_key`. This slice builds exactly that, and surfaces it as a dashboard.

## Scope

**In (this slice):** a pure read model (`governance_analytics.py`), a read-only aggregate API, and a Governance Dashboard screen showing point-in-time rollups + a "recent activity" count. Covers the two governed fact types that have a governance lifecycle today: **`approved_join`** and **`grain`/`availability_time`**.

**Deferred to later Phase-4 slices:**
- **Calibration** (slice 2) — actually *tuning* weights/thresholds or closing a reject-signal→scoring loop. This slice only *shows* the signal-vs-outcome correlation, read-only.
- **Prioritized HITL** (slice 3) — *re-ranking* the live queue (risk × unlock × evidence × age). This slice shows queue age/depth but does not reorder the queue.
- **Trend charts / time-series** — this slice is current-state aggregates + a recent-activity count, not historical trends.

**Out of scope:** **entity bridges** — they are not governed yet (no `human_tasks`, no confirm surface; shadow on the 3B/3C track), so they have no governance outcomes to roll up. The read model is shaped so a fourth fact type slots in later without rework.

## Architecture

Three new units, one small honesty fix:

1. **`overlay/upload/governance_analytics.py`** — the read model. Pure reads, fail-soft; no writes, no change to the governance engine.
2. **`api/routes/governance_dashboard.py`** — two read-only aggregate routes (`require_catalog_read`).
3. **`frontend/src/screens/GovernanceDashboardScreen.tsx`** — the dashboard (new screen + nav entry).
4. **Honesty fix** — correct the frontend copy that falsely claims the reject category "feeds back into re-proposal."

### §1 — The read model (`governance_analytics.py`, on-demand)

Computed **on-demand** per request (no materialized table, no new write path — the data volume is small, so live folding is cheap, always-fresh, and cannot drift). Fail-soft: one unreadable task/fact/ledger row is skipped with a counter, never blanks the result (same posture as the existing readiness readers).

**`compute_governance_dashboard(conn, *, source: str | None = None) -> GovernanceDashboard`** — `source=None` = cross-source (all catalogs); a source = that catalog. Derivation:
- **Enumerate the governed facts per fact type for the scope**, reusing the existing per-source enumeration: `approved_join` via the `overlay_proposal` read model + the `pass_c_candidate_evidence` ledger (the union `compute_relationship_readiness._relationship_candidates` already uses); `grain`/`availability_time` via the table-fact enumeration the governance/readiness readers use. Fold each `fact_key`'s current status from the event log (`fold_overlay_state`).
- **Counts by fact type × status:** `pending` (folded DRAFT / PARTIALLY_CONFIRMED — i.e. an open proposal), `confirmed` (VERIFIED), `rejected` (REJECTED). Plus `stale`/`reverify` folded into a small `needs_attention` bucket (a VERIFIED fact demoted by drift/expiry).
- **Rejected by category:** for each REJECTED fact, read its `OVERLAY_FACT_REJECTED` event's `category` (nullable) — aggregate `{category: count}` per fact type. (This is the write-only signal, now read.)
- **Queue health:** from the open `human_tasks` — `depth` (open count), `oldest_pending_age_seconds` (now − min(created_at)), and coarse age buckets (`<1d / 1–7d / >7d`). (`now` passed in / `datetime.now`.)
- **Calibration seed (approved_join only):** join each `approved_join` fact_key's folded outcome against its ledger `bucket` + `evidence_json` (`JoinCandidateEvidenceV1`). Produce: `confirm_rate_by_bucket` (`{strong: {confirmed, rejected, rate}, weak: {...}}`) and `reject_category_by_top_signal` (`{dominant_positive_signal: {category: count}}` — the top-weight signal on each rejected join × its reject category). Read-only correlation; changes no weight.
- **Recent activity:** count of confirm/reject events in the last N days (from the event `occurred_at`), per fact type — a lightweight "what happened lately" without a full time series.

Return a frozen `GovernanceDashboard` dataclass:
```
GovernanceDashboard:
  scope: "catalog" | "source"          # cross-source vs one source
  source: str | None
  generated_at: str                    # ISO (stamped by the route, not the pure fn — pass `now`)
  fact_types: tuple[FactTypeRollup, ...]
  queue_health: QueueHealth
  calibration_seed: CalibrationSeed
  recent_activity: RecentActivity
FactTypeRollup:
  fact_type: str                       # approved_join | grain | availability_time
  pending: int; confirmed: int; rejected: int; needs_attention: int
  rejected_by_category: dict[str, int] # category -> count (may include "uncategorized")
QueueHealth:
  open_depth: int; oldest_pending_age_seconds: int | None
  age_buckets: dict[str, int]          # "lt_1d" / "1_7d" / "gt_7d"
CalibrationSeed:                        # approved_join only; empty when no ledger data
  confirm_rate_by_bucket: dict[str, {confirmed:int, rejected:int, rate:float|None}]
  reject_category_by_top_signal: dict[str, dict[str, int]]
RecentActivity:
  days: int; confirmed: int; rejected: int  # per fact type in fact_types, plus a total
```
(`QueueHealth`, `CalibrationSeed`, `RecentActivity` hang off `GovernanceDashboard` — one per dashboard.)

Also **`list_source_governance_summaries(conn) -> tuple[SourceSummary, ...]`** for the cross-source overview: per source, `{source, pending, confirmed, rejected, oldest_pending_age_seconds}` — a compact table so the dashboard can show "which catalogs need attention" without folding every fact of every source into full detail. Cross-source `compute_governance_dashboard(source=None)` may reuse this + a scope-wide fold.

### §2 — API (`api/routes/governance_dashboard.py`)

Read-only, `require_catalog_read` (a catalog read, not a governance action). Serialize the frozen dataclasses via `dataclasses.asdict` (all fields are str / int / float / dict / nested dataclass — JSON-safe; add a small normalizer only if needed).
- `GET /governance/dashboard` → the cross-source `GovernanceDashboard` (`source=None`) + `sources: [SourceSummary]`.
- `GET /sources/{source}/governance/dashboard` → the per-source `GovernanceDashboard`.
Register the router in `api/app.py`. The `/governance` + `/sources` proxy prefixes are already covered.

### §3 — Frontend (`GovernanceDashboardScreen.tsx`)

A new **Dashboard** screen (there is no dashboard today; `OverviewScreen` is an empty stub — a dedicated governance dashboard is cleaner than fleshing out the stub). Reuse the existing `gj-*` / badge / card styling; no new styling system. Sections:
- **Summary cards** per fact type: pending / confirmed / rejected / needs-attention (with the severity coloring already in the app).
- **Rejected-by-category** breakdown (a small bar/list per fact type) — the insight that a category dominates.
- **Queue health:** open depth + oldest-pending age + the age buckets.
- **Calibration seed (joins):** a compact "confirm rate by bucket" (strong vs weak) + the top reject categories — read-only, clearly labeled as *observation, not tuning*.
- **Cross-source overview:** the `SourceSummary` table (which catalogs have the deepest/oldest queues), each row linking to that source's per-source dashboard.
New `api.ts` fns (`getGovernanceDashboard()`, `getSourceGovernanceDashboard(source)`), a `dashboard` route in `nav.ts` + `App.tsx`, session-local fetch with the standard `ApiError` handling.

### §4 — The honesty fix

`GovernanceReviewScreen.tsx` currently displays "the category feeds back into re-proposal" (verified false — the category is never consumed; only a per-*value* fingerprint sticky-denies re-proposal). Correct the copy to reflect reality: the reject category is **recorded and surfaced on the governance dashboard** (the actual feedback-into-scoring is the later calibration slice). One-line copy change across the reject boxes that carry it.

## Error handling

Read-only and fail-soft throughout: a single unreadable/undecodable task, fact stream, or ledger row is skipped with a `overlay.governance_analytics.*` counter, never aborting the dashboard (mirrors `join_governance` / `readiness`). A malformed `{source}` → 400 via the existing scoped-refs validation; an unknown source → an empty-but-valid dashboard (all zeros), not a 404 (an operator checking a source with no governance yet should see zeros, not an error). No path writes or changes fold/scoring behavior.

## Testing

**Read model (`tests/.../test_governance_analytics.py`):** seed proposals across each fact type × status (pending / confirmed / rejected-with-category / needs-attention) + ledger rows across buckets, then assert: the counts by type × status; rejected-by-category aggregation; queue depth + age buckets; the calibration seed (confirm-rate-by-bucket correlates the seeded outcomes; reject-category-by-top-signal); failure isolation (one corrupt fact/ledger row skipped, dashboard still returns). Cross-source vs per-source scope.

**API:** `GET /governance/dashboard` + the per-source route return the shape (JSON parses, no serialization 500); RBAC (catalog_read → 200, no catalog_read → 403); an unknown source → zeros not 404.

**Frontend:** one render test — mock the dashboard response → the summary cards + reject-by-category + the cross-source table render; the honesty-fix copy is corrected (assert the old false string is gone).

## Security

Read-only, `require_catalog_read`. No customer values — the dashboard is counts, categories, signal names, and scores (metadata/aggregates). No new write path; the governance engine and scoring are untouched.

## File map

**New:** `overlay/upload/governance_analytics.py`; `api/routes/governance_dashboard.py`; `frontend/src/screens/GovernanceDashboardScreen.tsx`; `tests/featuregen/overlay/upload/test_governance_analytics.py`; `tests/featuregen/api/test_governance_dashboard_routes.py`; a frontend test.
**Modified:** `api/app.py` (register router); `frontend/src/api.ts`, `nav.ts`, `App.tsx` (client fns + route); `GovernanceReviewScreen.tsx` (the honesty-fix copy). No new DB migrations.

## Acceptance criteria

1. `GET /governance/dashboard` (cross-source) and `GET /sources/{source}/governance/dashboard` return, per governed fact type, counts by status (pending/confirmed/rejected/needs-attention) + rejected-by-category, plus queue health, the join calibration seed, recent activity, and (cross-source) a per-source summary — read-only, `catalog_read`-gated.
2. The reject **category** is read and aggregated (was write-only); the join **evidence** is correlated against **outcome** (was never joined) — `confirm_rate_by_bucket` reflects seeded outcomes.
3. The Governance Dashboard screen renders the rollups; the false "category feeds back into re-proposal" copy is corrected.
4. Fail-soft: one corrupt task/fact/ledger row never blanks the dashboard; an unknown source returns zeros, not an error.
5. No new DB migrations; no write path; the governance engine, scoring, and non-governance flows are unchanged.

## Build hygiene

Branch `phase4-governance-dashboard` off `origin/main` (`34dff24`, includes Phase 3B.3a). Subagent-driven (Fable implementers, Opus reviewers) + a whole-branch review before merge.
