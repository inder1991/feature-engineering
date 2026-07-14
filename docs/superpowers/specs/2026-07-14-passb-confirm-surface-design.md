# Pass B Confirm Surface (grain / availability) — Design

**Date:** 2026-07-14
**Status:** Approved design, pre-implementation
**Depends on:** the join-confirmation surface, merged to `main` at `ba89f55` (this EXTENDS it)

## Goal

Give a platform-admin an API + UI to **approve or reject the governed `grain` and `availability_time` facts that Pass B (table synthesis) proposes** — so an uploaded catalog's LLM-inferred table grain and as-of columns can become VERIFIED (and project onto `graph_node`) through the same governance surface the joins use. This is the second half of the "governed proposals a human confirms" story; the joins surface built the first (`approved_join`).

## The problem this closes

Pass B (`OVERLAY_TABLE_SYNTH`, `overlay/upload/table_synth.py`) proposes a table's `grain` (`{columns, is_unique}`) and `availability_time` (`{column, basis}`) as DRAFT governed facts under the service actor `_ENRICH_ACTOR`, so a human confirmer satisfies four-eyes. Like the joins before their surface, these proposals are **stranded**: enabling `OVERLAY_TABLE_SYNTH` produces grain/availability proposals that nothing can promote to VERIFIED, and a VERIFIED grain never reaches `graph_node.is_grain` without a re-ingest. This spec is that missing confirm surface. **Note:** file-*declared* grain/availability already auto-confirm at ingest (`_table_facts`/`_assert_fact`) — this surface is only for the LLM-*inferred* proposals Pass B makes for tables that did not declare them.

## Scope

**In:** confirm/reject for `grain` and `availability_time` governed proposals, **single-confirmer** (one platform-admin), extending the existing `governance` router + `GovernanceReviewScreen` (the "Grain & availability" tab the joins mockup stubbed). On VERIFIED, synchronously project the fact onto `graph_node` (honest projected/pending report, learning from the joins whole-branch review).

**Out (deferred / not applicable):**
- The advisory `table_role` / `primary_entity` / `event_or_snapshot` fields — these are RECOMMENDATION-ceilinged field *evidence*, never governed facts, so they are **displayed as context** but not confirmed here.
- Pass C joins (already shipped in the joins surface).
- Value-verification, a verified-history/revoke UI, queue filters/bulk — same deferrals as the joins surface.
- Server-side enforcement of the approve checklist (client-side friction only).

## Why single-confirmer (not dual)

`confirm_fact` enters the two-step dual flow **only** `if fact_type == "approved_join" and authority.dual` (`confirmation_commands.py:87`). `grain`/`availability_time` are single-object facts → they always take the single path (`:102-185`): `_actor_is_authority` (governance-queue platform-admin) + `proposer_ne_confirmer` (proposer `_ENRICH_ACTOR` ≠ human confirmer → four-eyes) → one confirm → VERIFIED. So there is no partial state, no "different admin" rule, and no dual-referent-guard complexity. The STALE/REVERIFY single-path referent check was already routed through `check_referents_exist` by the joins branch's Task 0, so a sealed-config re-confirm of a drift-STALEd grain is already correct — **no new referent work.**

## What is reused (no change)

- `overlay/confirmation_commands.py:47` `confirm_fact` (single path), `:184` `reject_fact` — the branch already added optional `note` to confirm and first-class `category` to reject; both apply here unchanged.
- `overlay/upload/table_fact_projection.py:14` `project_table_facts_for_ref(conn, *, source, table, declared_grain, declared_as_of, now)` — clears then sets `graph_node.is_grain`/`is_as_of` from `resolve_fact` (VERIFIED-only, drift-freshness-guarded). The synchronous-projection target.
- `overlay/upload/table_fact_projection.py:105` `list_open_table_fact_proposals(conn)` + `:100` `_WORKLIST_READER` — the existing thin read model over open `human_tasks` filtered to `_TABLE_FACT_TYPES = ("grain","availability_time")`. The governance read model wraps this.
- `overlay/store.load_fact`, `overlay/identity._ref_from_payload`/`fact_key`, `overlay/state.fold_overlay_state`, `overlay/_lifecycle._cas_target` — the same fact-key→ref bridge + CAS-target the joins surface uses.
- `overlay/projections/runner.projection_lag`/`run_projection` + `overlay/projections.OverlayProjection` — the drain-then-project sequence (from the joins `project_verified_join`).
- API: `api/routes/governance.py` (extend), `api/deps.require_confirmer` (the raw `platform-admin` claim gate — reuse), the `_clean`/`_deny_to_detail` helpers.
- Frontend: `GovernanceReviewScreen.tsx` (extend), `api.ts` (add fns), the tab UI already present.

## Architecture

One new backend module + extensions to the existing router and screen:

1. **`overlay/upload/table_fact_governance.py`** (new) — the read model, the confirm/reject context bridge, and the synchronous projection helper for grain/availability. Mirrors `join_governance.py`'s shape; pure domain.
2. **`api/routes/governance.py`** (extend) — three table-fact endpoints beside the join ones, reusing the same dependency + helpers.
3. **`frontend/src/screens/GovernanceReviewScreen.tsx`** (extend) — the "Grain & availability" tab.

### §1 — API endpoints (extend the `governance` router)

All require `require_confirmer` (raw `platform-admin` claim). Request bodies Pydantic-validated.

**`GET /sources/{source}/governance/table-facts?limit=`** (default 100, max 500) — open `grain`/`availability_time` proposals for `source`:
```json
{ "source": "dpl_eib_compliance",
  "proposals": [
    { "fact_key": "…", "task_id": "…", "target_event_id": "…",
      "fact_type": "grain" | "availability_time",
      "table": "comp_financial_tran_repos_dly",
      "proposed_value": { "columns": ["cif_id"], "is_unique": true } | { "column": "tran_date", "basis": "posted_at" },
      "status": "PROPOSED",
      "origin": "llm_proposed_not_profiled",
      "advisory": { "table_role": "…|null", "primary_entity": "…|null", "event_or_snapshot": "…|null" },
      "evidence_parse_status": "parsed" | "missing" } ],
  "next_cursor": null }
```
Only PROPOSED (DRAFT) are listed (single-confirmer → no partial; VERIFIED/REJECTED leave the list). `advisory` is read best-effort from the table's advisory field evidence (display-only); absent → nulls. The read model wraps `list_open_table_fact_proposals`, decodes each fact's ref to recover + filter `catalog_source` (normalized), and folds the stream for `status`. Bad data on one task is skipped + logged, never aborts the list (same failure-isolation posture as `join_governance`).

**`POST /governance/table-facts/{fact_key}/confirm`** — body `{ note?: str≤1000 }`. Loads context (`fact_type ∈ {grain, availability_time}` else 404), dispatches `confirm_fact` (single path) → VERIFIED. On VERIFIED, synchronously project (below). Response `{ governance_status: "VERIFIED", operational_projection: "projected"|"pending" }`. (No `approvals` array — single confirmer; the confirmer + note are on the fact stream for audit.)

**`POST /governance/table-facts/{fact_key}/reject`** — body `{ category, note? }` with `category ∈ {wrong_grain_columns, wrong_as_of_column, not_unique, needs_data_check}`. Dispatches `reject_fact` with `args={reason: note, category}` (the branch's first-class `category`). Response `{ governance_status: "REJECTED", category }`.

### §2 — Backend module (`table_fact_governance.py`)

- **`list_open_table_fact_proposals_governance(conn, source, *, limit=100) -> list[dict]`** — iterate `list_open_table_fact_proposals(conn)`; keep `fact_type ∈ {grain, availability_time}`; for each, `load_fact(fact_key)` + `_ref_from_payload` to recover `catalog_source`, filter to the normalized `source`; fold the stream for `status` (only `DRAFT→"PROPOSED"` listed); read the table's advisory fields best-effort (display-only); shape tolerantly (`evidence_parse_status` — grain/availability carry only a `proposed_value`, so `"parsed"` when the value is well-formed, `"missing"` when absent/unreadable). Per-task failure isolation. `limit` clamped 1..500. (`list_open_table_fact_proposals` today returns `task_id` but not `fact_key`; extend it, or SELECT `fact_key` alongside `task_id` here — mirror `join_governance`'s scan.)
- **`load_table_fact_confirmation_context(conn, fact_key) -> {ref, fact_type, use_case, target_event_id}`** — the fact-key→ref bridge; **raises `TableFactGovernanceNotFound`** if the stream is empty, the ref won't decode, or `fact_type ∉ {grain, availability_time}` (→ route 404, no event). `target_event_id = _cas_target(fold_overlay_state(stream))`.
- **`project_verified_table_fact(conn, source, ref, fact_type, *, now) -> "projected"|"pending"`** — drain the overlay projection on `conn` (`while run_projection(conn, OverlayProjection()) >= 500: pass`), then `project_table_facts_for_ref(conn, source=source, table=<ref.table>, declared_grain=set(), declared_as_of=set(), now=now)`. **Honest report** (learning from the joins whole-branch review): return `"projected"` only if the confirmed flag is actually set on `graph_node` for this table+fact_type after projection (`is_grain`/`grain_fact_event_id` for grain; `is_as_of`/`availability_fact_event_id` for availability), else `"pending"` (stale drift watermark, lag, or error). Fail-soft (own savepoint; any exception → `"pending"` + log). **Empty declared sets are correct here:** a table with a Pass B grain/availability *proposal* has no file-declared grain/as-of on that fact (Pass B skips when a VERIFIED claim governs the key), so the clear spares nothing that matters.

### §3 — Decision-support content (deterministic, frontend)

Computed in the frontend from the payload:
- **Baseline checklist (always present):** for **grain** — (1) reviewed the grain column(s); (2) understand **one row = one `<columns>`** and this determines how every feature aggregates; (3) understand this is **LLM-inferred, not data-profiled**; (4) confirm it should become the table's grain. For **availability** — (1) reviewed the as-of column + basis; (2) understand point-in-time features use this column; (3) LLM-inferred-not-profiled; (4) confirm. Approve is disabled until all are ticked (client-side friction only).
- **Consequence line:** grain → "If approved: one row of `<table>` is treated as one `<columns>`; features aggregate to this grain. If wrong: counts and per-entity features are miscomputed." availability → "If approved: point-in-time features read `<column>` as the as-of date."
- **"LLM-inferred, not value-profiled" caution** shown on every card (origin = `llm_proposed_not_profiled`).

### §4 — Frontend

Extend `GovernanceReviewScreen.tsx`: the "Grain & availability" tab (already in the layout) fetches `listTableFactProposals(source)` and renders single-confirmer cards — the proposed grain columns / availability column+basis, the advisory context, the consequence + caution, the checklist gating Approve, and a structured Reject (the 4 table-fact categories). One approval → VERIFIED (no "1 of 2"). New `api.ts` fns `listTableFactProposals`, `confirmTableFact(factKey,{note})`, `rejectTableFact(factKey,{category,note})`. The joins tab is unchanged.

### §5 — RBAC + error handling

`require_confirmer` on all three routes (unchanged). Error map (same as joins): unknown/undecodable/non-table-fact `fact_key` → 404 (no event); not-open (already VERIFIED/REJECTED) → 409; missing claim → 403; CAS-stale → 409 "refresh"; bad category / over-length note → 422; malformed source → 400. Single-confirmer has no "different admin" 409. List-time bad data → skip+log; action-time → 404.

### §6 — Testing

**API (pytest, ephemeral PG, the joins test harness):**
1. GET lists an open grain (+ availability) proposal for the source with proposed_value + status PROPOSED + origin; excludes other sources + VERIFIED/REJECTED.
2. Single-admin confirm UNDER SEALED CONFIG + graph_node seeded → VERIFIED, `operational_projection == "projected"`, and `graph_node.is_grain` is set for the grain columns (traversable by readiness immediately). (Sealed + seeded so it exercises the real referent gate + drift-freshness path.)
3. Stale-watermark → VERIFIED but `operational_projection == "pending"` and no `is_grain` set (honest).
4. `fact_type` not a table fact (e.g. an `approved_join` fact_key) → confirm/reject 404, no event.
5. Reject `{category, note}` → REJECTED + category on the payload.
6. RBAC: non-`platform-admin` → 403 on all three.
7. Backend validation: bad category → 422; over-length note → 422.

**Domain (`test_table_fact_governance.py`):** read-model listing + failure isolation; the context bridge (typed ref + accepted target_event_id; non-table-fact raises); `project_verified_table_fact` sets the flag on a fresh watermark and reports "pending" on a stale one.

**Frontend:** one render+interaction test — a grain proposal renders; Approve disabled until the checklist is ticked; confirm posts by fact_key.

### §7 — Security

Approvals gated on the `platform-admin` claim; four-eyes preserved by the overlay (service proposer ≠ human confirmer). No customer values rendered — grain columns/as-of column names + the LLM's proposed structure are metadata. Every confirm/reject/note/category audited on the fact stream.

## File map

**New:** `overlay/upload/table_fact_governance.py`; `tests/featuregen/overlay/upload/test_table_fact_governance.py`; frontend test additions.
**Modified:** `api/routes/governance.py` (3 routes + request models); `frontend/src/api.ts`, `frontend/src/screens/GovernanceReviewScreen.tsx`. Possibly `overlay/upload/table_fact_projection.py` (surface `fact_key` on the read model if not already available). No new DB migrations.

## Acceptance criteria

1. A platform-admin lists a source's open Pass B grain/availability proposals with their proposed value + advisory context.
2. A single platform-admin confirming takes a grain/availability PROPOSED → VERIFIED (four-eyes via service-proposer ≠ human).
3. On confirm the fact is synchronously projected onto `graph_node` (`is_grain`/`is_as_of` set) when the drift watermark is fresh; the response distinguishes `projected` vs `pending`, and a stale watermark honestly reports `pending` with no flag set.
4. A non-table-fact `fact_key` cannot be confirmed/rejected here (404, no event).
5. Rejecting records a structured category + note; a non-`platform-admin` is refused (403); bodies are backend-validated.
6. The "Grain & availability" tab matches the surface style: consequence + LLM-inferred caution, approve gated on a checklist (client-side), structured reject; no customer values.
7. The joins tab and all non-governance flows are unchanged; no new migrations; flag-off (nobody using the surface) behavior unchanged.

## Build hygiene

Branch `passb-confirm-surface` off `main` (post-joins-surface, `ba89f55`). Subagent-driven (Fable implementers, Opus reviewers) + a whole-branch review before merge.
