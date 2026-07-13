# Join-Confirmation Surface — Design

**Date:** 2026-07-13
**Status:** Approved design, pre-implementation
**Depends on:** Phase 3A (Pass C governed join discovery), merged to `main` at `551b32d`

## Goal

Give a human reviewer an API and a React screen to **approve or reject the governed `approved_join` proposals that Pass C discovers**, driving the existing dual-admin confirmation loop end to end — so an uploaded catalog can go from *discovered joins* to *operational, approved joins* the feature planner can traverse.

## The problem this closes

Pass C (Phase 3A) discovers single-column joins and files them as governed `approved_join` proposals that two distinct platform-admins must confirm before a VERIFIED join projects to an operational graph edge. The entire propose → confirm → dual-admin → verify → project loop **exists and is tested**, but there is **no HTTP route and no UI** that reaches it: `confirm_fact` / `reject_fact` / `_confirm_approved_join` have zero references in `src/featuregen/api`. So enabling Pass C today produces proposals nothing can promote. This spec is that missing surface.

## Scope

**In scope (this build):** the confirmation surface for **Pass C `approved_join` proposals only** — list, evidence, dual-admin confirm, structured reject — with the API and screen shaped so Pass B grain/availability slot in later as another proposal type without rework.

**Out of scope (explicitly deferred):**
- Pass B grain/availability confirmation (architected for; a later build). Grain/availability use a single-confirmer flow, not dual.
- Value-verification of joins (no data plane; evidence stays metadata-only).
- Queue filters/sorting/bulk actions at scale, reviewer notifications/assignment, delegation.
- Server-side enforcement of the approve-gate checklist (it is client-side friction only — see §3).

## UX

The approved UX is the v2 mockup: an evidence-forward review queue where the match score is advisory (not a verdict), each proposed join shows a consequence line and a "matched on metadata, not value-verified" caution, **Approve is gated on ticking a what-to-verify checklist** (client-side friction against rubber-stamping), reject is **structured** (a reason category + optional note), and the **first approver's note is shown to the second**. Verified joins display as revocable. This design provides the data and endpoints that screen needs; it does not restate the visual design.

## What is reused (no change)

Verified seams (file:line):
- `overlay/confirmation_commands.py:47` `confirm_fact(conn, cmd)`, `:184` `reject_fact(conn, cmd)` — command handlers. `reject_fact` already persists `args.get("reason")` verbatim onto `OVERLAY_FACT_REJECTED.payload["reason"]` (`:196,:236`).
- `overlay/join_confirmation.py:58` `_confirm_approved_join` — the two-step dual flow, dispatched from `confirm_fact` when `fact_type=="approved_join" and authority.dual` (`confirmation_commands.py:87-101`). First confirmer → `OVERLAY_FACT_PARTIALLY_CONFIRMED`; a **repeat same-subject is denied** (`:116-120`); a distinct second confirmer → `OVERLAY_FACT_CONFIRMED` → VERIFIED. Authorized if `actor.subject in owners` OR (`authority.governance_queue and "platform-admin" in actor.role_claims`) (`:64-70`).
- `overlay/authority.py:44,118-125` — `Authority.dual`; both-owner-None ⇒ dual (two distinct governance approvals required), which is the Pass C case (`owner_of → None`).
- `overlay/task_read.py:17` `get_task_proposal(conn, task_id, actor) → TaskProposal` (`_types.py:103-116`): `object_ref` (display string), `fact_type`, `use_case`, `proposed_value`, `prior_value`, `target_event_id`, `evidence` (an `Evidence` dataclass or None).
- `overlay/evidence.py:56-73` `Evidence.metric_values`; for a Pass C join this is `asdict(JoinCandidateEvidenceV1)` (`overlay/upload/passc/propose.py:149`), fields (`overlay/upload/passc/types.py:15-22`): `score`, `positive_signals`/`negative_signals`, `namespace_compatibility`, `namespace_reason_codes`, `grain_evidence`, `missing_requirements`, `explanation`, `cardinality_status`, `bucket`, `proposed_direction`/`cardinality`, `column_pairs`, `from_ref`/`to_ref`, `source_snapshot_id`, versions.
- `overlay/upload/table_fact_projection.py:107-124` `list_open_table_fact_proposals` — the reader pattern to mirror (iterate open `human_tasks`, call `get_task_proposal` with a system reader `IdentityEnvelope(role_claims=("platform-admin",))`, post-filter `fact_type`).
- `overlay/store.py:83` `load_fact(conn, fact_key)`; `overlay/identity.py:32-43` `_ref_from_payload` (decodes a `catalog_object_ref` payload → typed `CatalogObjectRef`/`ApprovedJoinRef`), `:65` `fact_key(ref, fact_type, use_case)`.
- `overlay/state.py:19-27` `OverlayState.status` + `.partial_confirmers` (`list[{subject, role}]`); `fold_overlay_state(stream)`.
- API: `api/routes/quarantine.py` (route-module template), `api/app.py:88-99` (router registration), `api/deps.py:57-78` (`require_permission` factory + guards), `api/deps.py`/`get_identity` (injects `IdentityEnvelope`).
- Frontend: `frontend/src/api.ts:17-67` (`request`/`post` + `X-User`/`X-Roles`), `frontend/src/screens/ReviewQueueScreen.tsx` (screen template), `frontend/src/nav.ts:5-8` + `App.tsx:106,206-219` (route registry).

## Architecture

Three new units, each with one responsibility:

1. **`overlay/upload/join_governance.py`** (backend read model) — lists open `approved_join` proposals for a source and assembles a rich, UI-ready view (evidence + confirmation state + decoded refs). Pure reads.
2. **`api/routes/governance.py`** (HTTP surface) — three endpoints; thin, mapping domain calls + errors to HTTP.
3. **`frontend/src/screens/GovernanceReviewScreen.tsx`** (UI) — the v2 mockup, wired to the endpoints.

Plus small, surgical edits to existing files (a `note` arg on `confirm_fact`; a new RBAC dependency; router + nav registration).

### §1 — API endpoints (new `governance` router)

**`GET /sources/{source}/governance/joins`** — dependency `require_confirmer` (see §5).
Returns the open `approved_join` proposals for `source`:
```json
{
  "source": "DPL_EIB_COMPLIANCE",
  "proposals": [
    {
      "task_id": "…",
      "fact_key": "…",
      "target_event_id": "…",
      "from": { "table": "COMP_FINANCIAL_TRAN_REPOS_DLY", "column": "CIF_ID" },
      "to":   { "table": "CUSTOMER_MASTER_DLY", "column": "CIF_ID" },
      "cardinality": "N:1",
      "status": "PROPOSED | PARTIALLY_CONFIRMED",
      "approvals": [ { "subject": "a.rahman", "role": "…", "note": "…|null" } ],
      "evidence": {
        "score": 85,
        "positive_signals": [ { "name": "same_identifier_concept", "weight": 40 }, … ],
        "namespace_compatibility": "COMPATIBLE",
        "namespace_reason_codes": [ … ],
        "grain_status": "INFERRED_FROM_CONFIRMED_GRAIN",
        "grain_evidence": [ "CUSTOMER_MASTER_DLY.CIF_ID" ],
        "explanation": "…"
      }
    }
  ]
}
```
`status` and `approvals` (incl. each approver's note) come from folding the fact stream (`fold_overlay_state`), not from `get_task_proposal` (which lacks them). Only `PROPOSED`/`PARTIALLY_CONFIRMED` proposals are listed; VERIFIED/REJECTED are excluded (a later `?include=verified` can surface history — deferred).

**`POST /tasks/{task_id}/confirm`** — dependency `require_confirmer`.
Body: `{ "note": "optional string" }`. Loads the task's `fact_key` + `target_event_id`, decodes the typed ref, builds a `confirm_fact` `Command` (`actor = identity`), dispatches it. Response: `{ "status": "PARTIALLY_CONFIRMED | VERIFIED", "approvals": [ … ] }`. For a join this is the dual flow: first call → PARTIALLY_CONFIRMED, a distinct second admin's call → VERIFIED (the overlay projects the operational edge on the next ingest / projection run).

**`POST /tasks/{task_id}/reject`** — dependency `require_confirmer`.
Body: `{ "category": "wrong_direction|wrong_cardinality|different_entity|not_a_real_key|needs_data_check", "note": "optional" }`. Dispatches `reject_fact` with `args["reason"] = {category, note}` (stored verbatim as jsonb — no `reject_fact` change needed). Response: `{ "status": "REJECTED", "category": "…" }`.

### §2 — Backend additions

- **`join_governance.py::list_open_approved_join_proposals(conn, source) -> list[JoinProposalView]`** — mirror `list_open_table_fact_proposals`: iterate `status='open'` `human_tasks`, `get_task_proposal(…, _WORKLIST_READER)`, keep `fact_type=="approved_join"`; then `load_fact(fact_key)` → decode the ref via `_ref_from_payload` to recover `catalog_source` (neither `human_tasks` nor `TaskProposal.object_ref` carries it) and filter to `source`; fold the stream for `status` + `partial_confirmers` (+ their notes); shape the evidence. **De-duplicate** the dual-join's two side-tasks to one proposal per `fact_key` (a dual join opens two `human_tasks`, one per side — the list shows one row).
- **`join_governance.py::load_join_confirmation_context(conn, task_id) -> {ref, fact_type, use_case, target_event_id, fact_key}`** — the `task_id → typed ref` bridge the confirm/reject routes need (load the `human_tasks` row, `load_fact`, decode). One helper, used by both POST routes.
- **`confirm_fact` note (`confirmation_commands.py`)** — read `args.get("note")` and persist it on the `OVERLAY_FACT_PARTIALLY_CONFIRMED` and `OVERLAY_FACT_CONFIRMED` payloads (a `note` field), mirroring how `reject_fact` persists `reason`. Backward-compatible (absent → no note). This is the only edit to an existing command handler.

### §3 — Decision-support content (deterministic, no LLM)

The consequence line and the what-to-verify checklist are **deterministic templates derived from data already present**, computed in the frontend from the endpoint payload (kept out of the backend to avoid coupling copy to the API):
- **Checklist** — one plain-language check per meaningful evidence element: each `positive_signal` / `namespace_reason_code` maps to a fixed claim string (e.g. `same_identifier_concept` → "Both sides are the same *&lt;concept&gt;* identifier — same namespace, not a look-alike"); `grain_status == INFERRED_FROM_CONFIRMED_GRAIN` → a check naming the grain column; the corroborating-only signals (`same_column_name`, `same_bian_leaf`) are labelled as such.
- **Consequence line** — templated from the two table names / entities: "If approved: features can join `&lt;fromTable&gt;` to `&lt;toTable&gt;`. If wrong: …".
- **Approve-gate** — Approve is disabled until every checklist item is ticked. **Client-side friction only**; the backend does not require the checklist (confirmed with the user). The gate is UX, not authorization.

### §4 — Frontend

`GovernanceReviewScreen.tsx` (peer to `ReviewQueueScreen`), matching the v2 mockup: source input → `listJoinProposals(source)` → cards with evidence + the derived checklist/consequence → Approve (gated) / Reject (structured) → session-local "resolved this cycle" map, `ApiError` handled inline. New `api.ts` functions `listJoinProposals`, `confirmTask(taskId, {note})`, `rejectTask(taskId, {category, note})`. Register a new `governance` route in `nav.ts` + `App.tsx`.

### §5 — RBAC + the two-admin rule

- New dependency **`require_confirmer`** in `api/deps.py`: asserts `"platform-admin"` is in `identity.role_claims` — the exact claim the overlay's `_confirm_approved_join` authorizes on (`join_confirmation.py:68`). This is deliberately the **role-claim string** (`platform-admin`, hyphen), which is disjoint from the RBAC permission-bundle name `platform_admin` (underscore, `identity/permissions.py`). Reconciliation: add a `governance:confirm` permission to the `platform_admin` bundle **and** have the dependency accept either the `governance:confirm` permission or the raw `platform-admin` claim, so the surface is reachable by a correctly-provisioned admin while still matching what the overlay enforces. (Document the underscore/hyphen split as a pre-existing quirk; do not refactor the whole RBAC vocabulary here.)
- The **two distinct admins** rule is enforced *inside* the overlay (`_confirm_approved_join` denies a repeat subject); the API surfaces that denial (see §6). The route layer does not re-implement distinctness.

### §6 — Error handling (overlay → HTTP)

- Task not `open` / already resolved → **409** `{detail: "This proposal is no longer open."}`.
- Actor lacks the `platform-admin` claim → **403** (from `require_confirmer`).
- Same-subject repeat confirm (`_confirm_approved_join` deny) → **409** `{detail: "You already approved this — a different admin must confirm."}`.
- OCC / concurrent state change (`ConcurrencyError`) → **409** `{detail: "Changed since you loaded it — refresh."}`.
- Unknown `task_id` / undecodable ref → **404**.
Every failure is advisory; none corrupts state. The overlay's `OverlayCommandError`/deny reasons map to these.

### §7 — Testing

**API (pytest, ephemeral PG):**
1. `GET …/governance/joins` returns open joins for the source with evidence (score/signals/namespace/grain), decoded from/to, cardinality, and `status`; excludes VERIFIED/REJECTED and other sources; the two side-tasks of one dual join collapse to one proposal.
2. Full dual-admin happy path via the endpoints: admin1 confirm → `PARTIALLY_CONFIRMED` (+ note surfaced in the next GET's `approvals`); a *distinct* admin2 confirm → `VERIFIED`; after projection, `find_join_path` traverses (reuse Phase 3A harness).
3. Same-admin repeat confirm → 409 with the "different admin" detail; the fact stays PARTIALLY_CONFIRMED.
4. Reject with `{category, note}` → `REJECTED`, category persisted on the reject payload; not operational.
5. RBAC: no `platform-admin` claim → 403 on all three endpoints.
6. Unknown task / not-open task → 404 / 409.

**Frontend:** one render+interaction test (list renders a proposal with evidence; Approve is disabled until the checklist is ticked; confirm posts).

### §8 — Security

- Approvals gated on the `platform-admin` claim; four-eyes (two distinct subjects) preserved by the overlay.
- No customer values egress: the surface renders metadata evidence only (score/signals/namespace/grain/explanation) — the same evidence produced sanitized by Pass C.
- Every approval, rejection, note, and category is retained on the fact stream for audit (existing behavior).

## File map

**New:**
- `src/featuregen/overlay/upload/join_governance.py` — read model + confirm/reject context bridge.
- `src/featuregen/api/routes/governance.py` — the three endpoints.
- `frontend/src/screens/GovernanceReviewScreen.tsx` — the review screen.
- `tests/featuregen/api/test_governance_routes.py` (+ a frontend test).

**Modified:**
- `src/featuregen/overlay/confirmation_commands.py` — add optional `note` to `confirm_fact`.
- `src/featuregen/api/deps.py` — add `require_confirmer`.
- `src/featuregen/identity/permissions.py` — add `governance:confirm` to the `platform_admin` bundle.
- `src/featuregen/api/app.py` — register the governance router.
- `frontend/src/api.ts`, `frontend/src/nav.ts`, `frontend/src/App.tsx` — client functions + route registration.

## Acceptance criteria

1. A platform-admin can list a source's open discovered joins, each with its evidence and current approval state.
2. Two *distinct* platform-admins approving a join take it PROPOSED → PARTIALLY_CONFIRMED → VERIFIED, after which it is operationally traversable; the same admin cannot approve twice (409).
3. Rejecting a join records a structured category + note and keeps it non-operational.
4. The first approver's note is visible to the second approver.
5. A non-platform-admin is refused (403).
6. The screen matches the v2 mockup: evidence-forward, approve-gated-on-checklist (client-side), structured reject, first-approver note.
7. No new default behavior for anyone not using the surface; no customer values rendered or egressed.

## Build hygiene

Branch `confirmation-surface` off `main` (post-Phase-3A). Built subagent-driven (Fable implementers, Opus reviewers) with a whole-branch review before merge, as with Phase 3A.
