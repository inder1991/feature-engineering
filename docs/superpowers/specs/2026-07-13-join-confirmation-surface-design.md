# Join-Confirmation Surface — Design (v2, post-review)

**Date:** 2026-07-13
**Status:** Approved design (revised after architecture review), pre-implementation
**Depends on:** Phase 3A (Pass C governed join discovery), merged to `main` at `551b32d`

> **v2 changelog** — folds in a full architecture review. Material changes from v1: confirm/reject are **fact-key-based** (not task-id); RBAC gates on the **raw `platform-admin` claim** (matches the overlay); approver notes surface via a **dedicated stream reader**; the second confirmation **synchronously projects** the operational edge (VERIFIED alone is not operational); **UI revocation removed** (deferred); request bodies are **backend-validated**; the list endpoint is **bounded**; evidence reads are **version-tolerant**; the approve checklist has **baseline items**; generic-task confirmation is replaced by join-specific, **fact-type-validated** routes.

## Goal

Give a human reviewer an API and a React screen to **approve or reject the governed `approved_join` proposals that Pass C discovers**, driving the existing dual-admin confirmation loop end to end — so an uploaded catalog can go from *discovered joins* to *operational, approved joins* the feature planner can traverse.

## The problem this closes

Pass C files discovered joins as governed `approved_join` proposals that two distinct platform-admins must confirm before a VERIFIED join projects to an operational graph edge. The propose → confirm → dual-admin → verify → project loop **exists and is tested**, but has **no HTTP route or UI** (`confirm_fact`/`reject_fact`/`_confirm_approved_join` have zero references in `src/featuregen/api`). Enabling Pass C today produces proposals nothing can promote. This spec is that missing surface.

## Scope

**In:** the confirmation surface for **Pass C `approved_join` proposals only** — list, evidence, dual-admin confirm, structured reject, and synchronous projection on the final confirmation — API + screen shaped so Pass B grain/availability slot in later.

**Out (deferred):** Pass B grain/availability confirmation (single-confirmer; later build); value-verification of joins (no data plane); a verified/rejected **history view** and any **UI revoke** action; queue filters/sorting/bulk actions, reviewer notifications/assignment/delegation; server-side enforcement of the approve-gate checklist (client-side friction only).

## UX

The approved UX is the v2 mockup: evidence-forward, the match score advisory (not a verdict), a consequence line and a "matched on metadata, not value-verified" caution per join, **Approve gated client-side on a what-to-verify checklist**, **structured reject** (category + optional note), and the **first approver's note shown to the second**. This spec provides the data and endpoints that screen needs; it does not restate the visual design. One correction from v1: verified joins are **not** shown in this build (no history/revoke UI); auto-demotion remains a backend property.

## What is reused (no change)

Verified seams (file:line):
- `overlay/confirmation_commands.py:47` `confirm_fact(conn, cmd)`, `:184` `reject_fact(conn, cmd)`. `confirm_fact` reads `args["ref"]` (a typed `CatalogObjectRef`/`ApprovedJoinRef`), `args["fact_type"]`, `args.get("use_case")`, `args["target_event_id"]`, `args.get("value")`. Dispatches `_confirm_approved_join` when `fact_type=="approved_join" and authority.dual` (`:87-101`). `reject_fact` persists `args.get("reason")` verbatim (jsonb) onto `OVERLAY_FACT_REJECTED.payload["reason"]`.
- `overlay/join_confirmation.py:58` `_confirm_approved_join` — first confirmer → `OVERLAY_FACT_PARTIALLY_CONFIRMED` (closes that subject's task); **repeat same-subject denied** (`:116-120`); distinct second → `OVERLAY_FACT_CONFIRMED`. Authorized if `actor.subject in owners` OR (`authority.governance_queue and "platform-admin" in actor.role_claims`) (`:64-70`).
- `overlay/authority.py:44,118-125` — `Authority.dual`; both-owner-None ⇒ dual (Pass C, `owner_of → None`).
- `overlay/task_read.py:17` `get_task_proposal → TaskProposal` (`_types.py:103-116`); its `evidence: Evidence|None` (`evidence.py:56-73`) carries `metric_values`, for a join `= asdict(JoinCandidateEvidenceV1)` (`overlay/upload/passc/propose.py:149`; fields `types.py:15-22`). **`get_task_proposal` lacks confirmation status and approver identity — do not rely on it for those.**
- `overlay/upload/table_fact_projection.py:107-124` `list_open_table_fact_proposals` — reader pattern to mirror (iterate open `human_tasks`, `get_task_proposal` with a system reader `IdentityEnvelope(role_claims=("platform-admin",))`, post-filter `fact_type`).
- `overlay/store.py:83` `load_fact(conn, fact_key)`; `overlay/identity.py:32-43` `_ref_from_payload`, `:65` `fact_key(...)`; `overlay/state.py:19-27` `fold_overlay_state` → `OverlayState.status`.
- `overlay/upload/passc/projection.py` `project_confirmed_joins(conn, *, source, pairs, now=None)` — the reverse projector; `projection_lag(conn,"overlay")` guard (`projections/runner.py`).
- API/frontend templates: `api/routes/quarantine.py`, `api/app.py:88-99`, `api/deps.py:57-78`, `frontend/src/api.ts:17-67`, `frontend/src/screens/ReviewQueueScreen.tsx`, `frontend/src/nav.ts:5-8`, `App.tsx:106,206-219`.

## Architecture

Three new units + surgical edits:
1. **`overlay/upload/join_governance.py`** — read model (`list_open_approved_join_proposals`, an approval-stream reader) + the confirm/reject context bridge + the synchronous-projection helper. Pure domain; no HTTP.
2. **`api/routes/governance.py`** — three endpoints; thin transport over the domain calls, mapping errors to HTTP.
3. **`frontend/src/screens/GovernanceReviewScreen.tsx`** — the v2 mockup, wired to the endpoints.

### §1 — API endpoints (new `governance` router)

All three require `require_confirmer` (§5). Request bodies are Pydantic-validated (§2).

**`GET /sources/{source}/governance/joins?limit=&cursor=`** — list open `approved_join` proposals for `source`. **Bounded**: `limit` default 100, max 500; `cursor` reserved (opaque; `null` today). One row **per `fact_key`** (a dual join's two side-tasks collapse), with the underlying tasks exposed for transparency:
```json
{
  "source": "dpl_eib_compliance",
  "proposals": [
    {
      "fact_key": "…",                         // the action target
      "tasks": [ {"task_id":"…","side":"from","status":"open"},
                 {"task_id":"…","side":"to","status":"open"} ],
      "from": { "table":"comp_financial_tran_repos_dly", "column":"cif_id" },
      "to":   { "table":"customer_master_dly", "column":"cif_id" },
      "cardinality": "N:1",
      "proposed_direction": "from_to",
      "status": "PROPOSED | PARTIALLY_CONFIRMED",
      "approvals": [ {"subject":"a.rahman","display_name":null,"role":"platform-admin",
                      "note":"…|null","confirmed_at":"…"} ],
      "evidence_version": "JoinCandidateEvidenceV1",
      "evidence_parse_status": "parsed | partial | missing | invalid",
      "evidence": { "score":85, "positive_signals":[{"name":"…","weight":40}],
                    "negative_signals":[], "namespace_compatibility":"COMPATIBLE",
                    "namespace_reason_codes":[], "grain_status":"INFERRED_FROM_CONFIRMED_GRAIN",
                    "grain_evidence":["…"], "explanation":"…", "warnings":[] }
    }
  ],
  "next_cursor": null
}
```
`status` + `approvals` (incl. each note + `confirmed_at`) come from the **approval-stream reader** (§2), not `get_task_proposal`. Only `PROPOSED`/`PARTIALLY_CONFIRMED` are listed; VERIFIED/REJECTED are excluded.

**`POST /governance/joins/{fact_key}/confirm`** — body `ConfirmJoinRequest {note?: str≤1000}`. Loads the fact by `fact_key`, validates `fact_type=="approved_join"` (else 404), decodes the typed ref, derives the current `target_event_id` from the fold, dispatches `confirm_fact` with `actor=identity` and `args["note"]`. On the **second (distinct-admin) confirmation → VERIFIED**, the route **synchronously projects** the operational edge (§2). Response:
```json
{ "governance_status": "PARTIALLY_CONFIRMED | VERIFIED",
  "operational_projection": "not_applicable | pending | projected",
  "approvals": [ … ] }
```
`operational_projection` is `not_applicable` while PARTIALLY_CONFIRMED; on VERIFIED it is `projected` (lag==0, projection ran) or `pending` (projection lag>0 — projects on the next projection/ingest run).

**`POST /governance/joins/{fact_key}/reject`** — body `RejectJoinRequest {category: enum, note?: str≤1000}`. Same load+fact-type-validate, dispatches `reject_fact` with `args["reason"] = {category, note}`. Valid from `PROPOSED` **or** `PARTIALLY_CONFIRMED` (a partial-then-reject is terminal REJECTED; the prior partial approval remains in history). Response `{ "governance_status": "REJECTED", "category": "…" }`.

### §2 — Backend additions (`join_governance.py` + one command edit)

- **`list_open_approved_join_proposals(conn, source, *, limit) -> list[JoinProposalView]`** — mirror `list_open_table_fact_proposals`; keep `fact_type=="approved_join"`; `load_fact` + `_ref_from_payload` to recover `catalog_source` and filter to the **normalized** `source` (lowercased per `normalize_ref`); **collapse** the two side-tasks per `fact_key` to one view; shape evidence tolerantly (§ below). **Failure isolation:** an undecodable ref / wrong ref type / missing source on a *single* task is **skipped with a structured warning + metric** — one corrupt task never breaks the queue. A malformed `{source}` path → 400.
  ```
  JoinProposalView: fact_key, tasks: tuple[ProposalTaskRef], from, to, cardinality,
    proposed_direction, status: Literal["PROPOSED","PARTIALLY_CONFIRMED"],
    approvals: tuple[ApprovalView], evidence, evidence_version, evidence_parse_status
  ApprovalView: subject, display_name|None, role|None, note|None, confirmed_at|None
  ```
- **Approval-stream reader** — reconstruct `approvals` (subject, role, note, confirmed_at) by reading the fact stream's `OVERLAY_FACT_PARTIALLY_CONFIRMED` / `OVERLAY_FACT_CONFIRMED` events directly (each payload carries the confirmer + the new `note`). This is local to `join_governance.py`; it does **not** modify the shared `OverlayState.partial_confirmers` seam.
- **`load_join_confirmation_context(conn, fact_key) -> {ref, fact_type, use_case, target_event_id}`** — the fact-key→typed-ref bridge for confirm/reject. **Enforces `fact_type=="approved_join"`** (else raises → 404), so a non-join fact can never be driven through this surface.
- **`project_verified_join(conn, source, ref, *, now)`** — on VERIFIED, if `projection_lag(conn,"overlay")==0` call `project_confirmed_joins(conn, source=source, pairs=[ref], now=now)` (fail-soft, own savepoint) and report `projected`; if lag>0, skip and report `pending`. Makes an approved join operational immediately in the common case instead of waiting for a re-upload.
- **`confirm_fact` note (`confirmation_commands.py`)** — read `args.get("note")`, validate/trim, persist on the `OVERLAY_FACT_PARTIALLY_CONFIRMED` and `OVERLAY_FACT_CONFIRMED` payloads (mirrors `reject_fact`'s `reason`). Only edit to an existing command handler; backward-compatible.
- **Request models** (`governance.py`): `ConfirmJoinRequest{note: str|None, max_length=1000}`, `RejectJoinRequest{category: Literal["wrong_direction","wrong_cardinality","different_entity","not_a_real_key","needs_data_check"], note: str|None, max_length=1000}`. Trim whitespace; empty→None.

**Evidence tolerance:** the read model never crashes on evidence shape — missing `metric_values` → `evidence_parse_status="missing"`; a present-but-unparseable/older shape → `"invalid"` or `"partial"` with defaulted empty arrays and a `warnings` list; a clean parse → `"parsed"`. The UI renders "evidence unavailable / older proposal version" for non-`parsed` and still permits reject (and approval, with the baseline checklist).

### §3 — Decision-support content (deterministic, no LLM, frontend)

Computed in the frontend from the endpoint payload:
- **Baseline checklist (always present, even with sparse/absent evidence):** (1) reviewed the proposed **direction**; (2) reviewed the **cardinality**; (3) understand this is **metadata-matched, not value-verified**; (4) confirm it should become operational **if a second admin also approves**. This guarantees a non-empty, tick-able checklist regardless of evidence.
- **Evidence-derived checks (added on top when `parsed`/`partial`):** one plain-language claim per meaningful signal (`same_identifier_concept` → "Both sides are the same *&lt;concept&gt;* identifier — same namespace, not a look-alike"; `grain_status==INFERRED_FROM_CONFIRMED_GRAIN` → a check naming the grain column); corroborating-only signals (`same_column_name`, `same_bian_leaf`) labelled as such.
- **Consequence line (precise wording):** "If approved: the feature planner may traverse this governed join from `&lt;fromTable.column&gt;` to `&lt;toTable.column&gt;` as `&lt;cardinality&gt;`." plus a "proposed direction" note where direction is advisory. Do not imply broader permission than the approved edge grants.
- **Approve-gate** — Approve disabled until every checklist item (baseline + derived) is ticked. **Client-side friction only**; not backend authorization.

### §4 — Frontend

`GovernanceReviewScreen.tsx` (peer to `ReviewQueueScreen`), the v2 mockup: source input → `listJoinProposals(source)` → cards with evidence + derived checklist/consequence → Approve (gated) / Reject (structured) → session-local "resolved this cycle" map. New `api.ts` fns `listJoinProposals`, `confirmJoin(factKey, {note})`, `rejectJoin(factKey, {category, note})`. Register a `governance` route in `nav.ts` + `App.tsx`.
**Concurrency UX:** disable the action button on submit; on `409 "changed…"` refresh the queue; on `409 "already approved"` show a friendly message and refresh; never blind-retry.

### §5 — RBAC + the two-admin rule (Option A)

- New dependency **`require_confirmer`** (`api/deps.py`): asserts **`"platform-admin"` is in `identity.role_claims`** — the exact claim `_confirm_approved_join` authorizes on (`join_confirmation.py:68`). This is the raw role-**claim** (hyphen), deliberately *not* keyed on the RBAC permission bundle `platform_admin` (underscore), to avoid a route-passes-but-overlay-denies mismatch. A `governance:confirm` permission is added to the `platform_admin` bundle for **future** reconciliation only; the dependency does **not** rely on it in this build. (The underscore/hyphen split is a documented pre-existing quirk; not refactored here.)
- The **two distinct admins** rule stays inside the overlay (`_confirm_approved_join` denies a repeat subject); the API surfaces that denial (§6). The route does not re-implement distinctness.

### §6 — Error handling (overlay → HTTP)

- Unknown `fact_key`, or fact whose `fact_type != "approved_join"`, or undecodable ref on the action path → **404** (no state change; does not reveal unrelated task existence).
- Fact not in an open/partial state (already VERIFIED/REJECTED) → **409** "This proposal is no longer open."
- Missing `platform-admin` claim → **403** (from `require_confirmer`).
- Same-subject repeat confirm (`_confirm_approved_join` deny) → **409** "You already approved this — a different admin must confirm." (fact stays PARTIALLY_CONFIRMED).
- OCC / concurrent change (`ConcurrencyError`) → **409** "Changed since you loaded it — refresh."
- Malformed `{source}` path → **400**.
- **List-time** bad task/fact data → the proposal is **skipped + logged/metered**, never fails the whole GET. **Action-time** bad data → 404, no write.

### §7 — Testing (pytest, ephemeral PG; reuse the Phase 3A harness)

1. `GET` returns open joins for the source with evidence (score/signals/namespace/grain) + decoded from/to + cardinality + `status`; excludes VERIFIED/REJECTED and other sources; a dual join's two side-tasks collapse to one proposal exposing both `tasks` + the `fact_key` target.
2. **Deduped dual-task drive:** admin1 confirm (by fact_key) → PARTIALLY_CONFIRMED with note surfaced in the next GET's `approvals`; a *distinct* admin2 confirm (same fact_key) → VERIFIED; `operational_projection` is `projected` (lag==0) and `find_join_path` traverses.
3. Same-admin repeat confirm → 409 "different admin"; fact stays PARTIALLY_CONFIRMED.
4. `fact_type != "approved_join"` → confirm and reject both **404**, no overlay event written.
5. Reject with `{category, note}` → REJECTED, category+note on the reject payload; not operational. **Partial-then-reject:** admin1 confirm then admin2 reject → terminal REJECTED, prior partial approval remains in history, no longer listed.
6. RBAC: no `platform-admin` claim → 403 on all three.
7. Backend validation: bad category enum → 422; over-length note → 422; whitespace note normalized to None.
8. Evidence tolerance: a proposal with missing/invalid `metric_values` lists with `evidence_parse_status != "parsed"` and does not crash the queue.
9. Bounded listing: `limit` capped (default/max enforced); `next_cursor` present.
10. `operational_projection == "pending"` path when `projection_lag>0` (VERIFIED but not yet projected).
11. Frontend: one render+interaction test — a proposal renders with evidence; Approve is disabled until the (baseline+derived) checklist is fully ticked; confirm posts by fact_key.

### §8 — Security

- Approvals gated on the `platform-admin` claim; four-eyes (two distinct subjects) preserved by the overlay.
- No customer values egress: only metadata evidence (score/signals/namespace/grain/explanation) is rendered — the sanitized evidence Pass C already produced.
- Every approval, note, rejection, and category is retained on the fact stream for audit.

## File map

**New:** `overlay/upload/join_governance.py`; `api/routes/governance.py`; `frontend/src/screens/GovernanceReviewScreen.tsx`; `tests/featuregen/api/test_governance_routes.py` (+ a frontend test).
**Modified:** `overlay/confirmation_commands.py` (`confirm_fact` note); `api/deps.py` (`require_confirmer`); `identity/permissions.py` (`governance:confirm` on `platform_admin`, future-only); `api/app.py` (register router); `frontend/src/api.ts`, `nav.ts`, `App.tsx`.

## Acceptance criteria

1. A platform-admin lists a source's open discovered joins, each with evidence and current approval state.
2. A deduped join proposal exposes a valid remaining confirmation target (the `fact_key`) after the first admin approves.
3. Two *distinct* platform-admins confirming (by fact_key) take a join PROPOSED → PARTIALLY_CONFIRMED → VERIFIED; the same admin cannot approve twice (409).
4. On the second confirmation the join is **synchronously projected** (lag==0) and becomes operationally traversable; the response distinguishes `governance_status` from `operational_projection` (projected vs pending).
5. A task/fact whose type is not `approved_join` cannot be confirmed or rejected here (404, no event).
6. Rejecting records a structured category + note and keeps the join non-operational; partial-then-reject is terminal REJECTED and auditable.
7. The first approver's note is preserved through the stream reader and visible to the second approver.
8. Confirm/reject bodies are backend-validated (enum category, note length/type).
9. The list endpoint is bounded (limit cap; cursor reserved) and tolerant of evidence parse failures (queue never crashes).
10. A non-platform-admin is refused (403).
11. The screen matches the v2 mockup: evidence-forward, approve-gated on a baseline+derived checklist (client-side), structured reject, first-approver note; no verified-history/revoke UI; no customer values rendered.

## Build hygiene

Branch `confirmation-surface` off `main` (post-Phase-3A). Subagent-driven (Fable implementers, Opus reviewers) with a whole-branch review before merge.
