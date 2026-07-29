# Governance Review Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn Governance Review from a source-search form into a cross-catalog decision queue, and give entity bridges the confirmation surface they have never had.

**Architecture:** Four new read/write surfaces over machinery that already exists. Entity-bridge propose, authority, projection and demotion are all built and tested; what is missing is a *listing*, the HTTP routes, and a queue that spans catalogs. The frontend replaces a required text input with a filtered list.

**Tech Stack:** FastAPI + psycopg (backend), React + TypeScript + vitest (frontend), Postgres event-sourced overlay.

**Design reference:** the approved mockup — a queue sorted by consequence, grouped noise, server-decided actions, honest empty states.

---

## Verified interfaces — read before writing anything

Every line below was verified against the tree at `bb0155e6` on 2026-07-29. **Nothing in this plan may be implemented from memory; if an interface here disagrees with the code, the code wins and the plan is the defect.**

**Bridge machinery that EXISTS (do not rebuild):**

| Symbol | Location | Signature / note |
|---|---|---|
| `derive_bridge_candidates` | `overlay/upload/bridge_candidates.py:85` | `(conn, *, roles: Iterable[str] = ()) -> tuple[BridgeCandidateV1, ...]` — already read-scoped |
| `BridgeCandidateV1` | `bridge_candidates.py:33` | `candidate_id, entity_id, left_ref, right_ref, data_type_family, left_is_grain, right_is_grain` |
| `propose_bridge` | `overlay/upload/bridge_propose.py:28` | `(conn, candidate, *, actor, now=None) -> str` |
| `project_verified_bridge` | `overlay/upload/bridge_projection.py:31` | `(conn, ref: EntityBridgeRef, *, now) -> str` |
| `demote_bridge_edges` | `bridge_projection.py:50` | `(conn, fact_key_value: str) -> int` |
| `active_bridges` | `bridge_projection.py:57` | `(conn) -> tuple[ActiveBridgeV1, ...]` |
| `EntityBridgeRef` | `overlay/identity.py:33` | `entity_id, left_ref, right_ref` — **identity is UNORDERED**; `fact_key` canonicalizes endpoints |
| `load_fact` | `overlay/store.py:83` | `(conn, fact_key) -> list[EventEnvelope]` — reads the **event stream**, not a read model |
| `fold_overlay_state` | `overlay/state.py:53` | `(stream) -> OverlayState` — takes only the stream; **no `catalog_source` needed** |
| entity_bridge authority | `overlay/authority.py:132` | requires an `EntityBridgeRef`; **a SINGLE confirmation, four-eyes (proposer ≠ confirmer)**. Two-owner dual sign-off is explicitly deferred |

**Tables:**

- `entity_bridge_candidate_evidence` — durable candidate ledger. PK `(entity_id, left_catalog_source, left_object_ref, right_catalog_source, right_object_ref)`; carries `candidate_id`, `fact_key`, `proposed_event_id`, `data_type_family`, `evidence_json`, `derivation_version`, `updated_at`. CHECK: `left_catalog_source <> right_catalog_source`.
- `entity_bridge_edge` — the VERIFIED projection. `fact_key` PK, `confirmed_event_id` NULL until confirmed, `status`.

**Live state on the kind cluster (2026-07-29), for fixtures and acceptance:**

- 9 `entity_bridge` `OVERLAY_FACT_PROPOSED` events in `events`
- 9 rows in `entity_bridge_candidate_evidence` — **1 `customer`** (`cib: cust_num` ↔ `ftr: cif_id`, family `text`) and **8 `branch`** (a 4×2 cross-product)
- **0 rows in `entity_bridge_edge`** — nothing has ever been confirmed
- Catalogs: `cib` (112 nodes), `ftr` (127 nodes)

---

## ⚠️ Revision 2 (2026-07-29) — what an adversarial review changed

**Rev 1 assumed nothing proposed bridges. That was wrong — it only looked at `main`.**

`integration/ontology-data-agent` wires discovery into ingest (`ingest.py:461-468`), running
`derive_bridge_candidates` **globally, not scoped to the uploading catalog**, because — its comment —
*"a bridge needs both endpoints, so it only becomes derivable when the SECOND catalog lands."* Each
proposal gets its own savepoint so a failure can never fail the upload. This is why nine candidates
appeared when the customer CSV landed, and the data confirms the path: `derivation_version 1.0.0`,
a hashed `candidate_id` (not the spike's `b-spike:` prefix), and `type_basis: "declared"`.

**So there is no "wire up discovery" task. There is a one-argument defect instead.**

### 🔴 R1 — ingest proposes under the UPLOADING USER, which locks that user out forever

`ingest.py:468` calls `propose_bridge(conn, candidate, actor=actor)`. Four-eyes is enforced —
`authority.py:215` (`return proposed_by != actor.subject`) and `confirmation_commands.py:112`
(*"four-eyes: a proposer may not confirm the same fact"*). Therefore **whoever uploads a catalog can
never confirm the bridges that upload discovers.** On a small team the uploader and the reviewer are
routinely the same person, so bridges sit unconfirmed and nobody understands why.

`b_slice_spike.verify_bridge` shows the intended shape: it takes a **`service_actor`** and a
**`human_actor`**, and proposes with the service actor. The system proposes; a human disposes.

**Fix:** propose under a service actor. **This file belongs to the parallel stream — do NOT edit it
from this branch.** Route it to that owner (the A.9 precedent). It blocks the acceptance below.

### 🔴 R2 — the existing nine cannot be rescued by re-uploading

Verified at `state.py:67`: `if st.status not in (None, REJECTED): continue` — *"stray PROPOSED after
a confirm — ignore, do not regress to DRAFT."* The nine are DRAFT, so a second PROPOSED is
**ignored** and `draft_event_id` stays pinned to the original event. `proposed_by` remains
`user:inder`. An earlier claim in this plan that a re-upload would re-propose them properly was
**wrong**.

The only in-model rescue is **reject → re-propose under the service actor → confirm**, because the
fold reopens a DRAFT after REJECTED (`state.py:76-82` clears all prior-cycle carry-over). Whether the
proposer may reject their own proposal must be verified before relying on this — four-eyes is
specified for confirm.

### Other review findings folded in

- **I1 — Task 5's direction hint was backwards.** `overlay_fact_dependency` maps a fact to what it
  *depends on* (`resolve.py:41`: *"the DISTINCT catalog_sources a fact's referents span"*), and
  `dependents_of` runs the drift direction. Neither answers "what does this bridge block." The
  candidate path is `feature_derives_from` (`features.py:67`) — a feature spanning both endpoints'
  catalogs implies it needs the bridge. **Unverified; Task 5's stop-condition still applies.**
- **I2 — the unscoped route has no precedent.** Every governance route across all 21 registered
  routers is `/sources/{source}/…`. Rev 2 drops `GET /governance/entity-bridges` and puts the
  cross-catalog surface **only** on the queue (Task 4), which must carry its own scoping story
  rather than inheriting one by accident.
- **I3 — bulk reject was in the design and in no task.** Now Task 2b.
- **M1** — `use_case` is blank on all nine live events; the copied Command args assume it is present.
- **M2** — `governance_analytics.py:360` enumerates catalogs via `SELECT DISTINCT catalog_source FROM
  overlay_proposal`, which by design never sees a bridge. Do not reuse it in the queue.
- **M3** — no index supports source-filtering on `entity_bridge_candidate_evidence` (PK is a
  five-tuple). Deferred per the standing NFR directive; recorded so it is not rediscovered.

## ⚠️ The finding that shapes this plan

**`overlay_proposal` deliberately does not carry entity bridges, so the listing CANNOT follow the semantic-bindings pattern.**

`overlay/projection.py:50` short-circuits `entity_bridge` before the `overlay_proposal` insert. Its comment is normative:

> *"an entity_bridge fact is two-source; the single-catalog_source overlay read models (overlay_proposal/_state) don't model it and `_catalog_source` would KeyError on its `{entity_id,left_ref,right_ref}` ref. The bridge lifecycle uses a direct fold (bridge_projection), not this projection; later bridge events (CONFIRMED/EXPIRED/…) are inherently no-ops here."*

Only `overlay_fact_dependency` is maintained, so catalog drift still stales a bridge.

**Consequence:** `list_semantic_binding_proposals` (`semantic_binding_governance.py:261`) enumerates from `overlay_proposal`. A bridge listing built the same way returns **empty** — and an empty list reads as "no bridges pending" when nine are. **Enumerate from `entity_bridge_candidate_evidence` and fold each `fact_key` through `load_fact` + `fold_overlay_state` instead.** This is verified: the live read model has zero entity_bridge rows while the event stream has nine.

**Do not "fix" the projection to include bridges.** The exclusion is deliberate and the comment explains the KeyError that motivated it.

---

## Global Constraints

- **Read-scoping is mandatory on every listing.** `semantic_binding_governance.py:269` states the rule: a listing must not become an existence oracle — a row whose column is sensitivity-hidden for the caller's roles is **dropped**, fail-closed, so a platform-admin without `pii_reader` sees no count, id or existence leak. `derive_bridge_candidates` already takes `roles`; pass the caller's claims.
- **A denied command RETURNS 409, never raises.** Verified at `governance.py:369-373`: raising rolls back the `COMMAND_DENIED` audit row and releases the advisory lock (audit finding I-3).
- **`available_actions` is server-decided.** The listing tells the UI which of confirm/reject the server sanctions (`semantic_binding_governance.py:265`). Four-eyes greying is driven by this, never computed client-side.
- **Bad data on one row is skipped, never aborts the list** (`semantic_binding_governance.py:267`).
- **`limit` clamped 1..500.**
- Frozen slotted dataclasses + `StrEnum`; not pydantic, except FastAPI request bodies which follow the existing `Confirm*Request` pattern.
- Commit trailer: `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`

---

## File Structure

| File | Responsibility |
|---|---|
| `src/featuregen/overlay/upload/bridge_governance.py` | **new** — `list_bridge_proposals`, the context loader, `available_actions` |
| `src/featuregen/api/routes/governance.py` | **modify** — three entity-bridge routes |
| `src/featuregen/api/routes/catalogs.py` | **new** — `GET /catalogs` |
| `src/featuregen/overlay/upload/governance_queue.py` | **new** — the cross-catalog queue read model |
| `frontend/src/api.ts` | **modify** — client functions + types |
| `frontend/src/screens/GovernanceReviewScreen.tsx` | **rewrite** — queue landing |
| `frontend/src/screens/governance/*.tsx` | **new** — QueueRow, SummaryStrip, FilterBar, CandidateGroup |

---

## Task 1: `list_bridge_proposals`

**Files:** Create `src/featuregen/overlay/upload/bridge_governance.py`; Test `tests/featuregen/overlay/test_bridge_governance.py`

**Interfaces produced:**
- `list_bridge_proposals(conn, *, source: str | None = None, limit: int = 100, roles: Iterable[str] | None = None) -> list[dict]`
- `load_bridge_context(conn, fact_key: str, roles) -> dict | None` — returns `{ref: EntityBridgeRef, fact_type, use_case, target_event_id}` for the command layer

`source=None` means **all catalogs** — this is the cross-catalog case the existing routes cannot express. When a source IS given, a bridge matches if **either** endpoint is that source, because `EntityBridgeRef` identity is unordered (`identity.py:35`).

- [ ] **Step 1: Failing tests**
  - enumerates from `entity_bridge_candidate_evidence`, **not** `overlay_proposal` — seed a bridge PROPOSED event with an empty `overlay_proposal` and assert it is still listed (this is the regression guard for the finding above)
  - state comes from folding the event stream: DRAFT before confirm, VERIFIED after
  - `source='cib'` and `source='ftr'` each return the same customer bridge (unordered identity)
  - a bridge whose endpoint column is sensitivity-hidden for the caller's roles is **dropped entirely** — assert absence, not a redacted row
  - `available_actions` is `('confirm','reject')` for DRAFT and excludes `confirm` when `proposed_by == caller`
  - one malformed row does not abort the list
  - `limit` clamps at 500
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(governance): list entity-bridge proposals across catalogs`

**Test-strength check:** would a test pass against an implementation that returns every candidate regardless of roles, or that never folds state? If yes it asserts nothing.

---

## Task 2: Entity-bridge confirm / reject routes

**Files:** Modify `src/featuregen/api/routes/governance.py`; Test `tests/featuregen/api/test_governance_bridges.py`

**Routes** (mirroring `confirm_semantic_binding` at `governance.py:353`):
- `GET /sources/{source}/governance/entity-bridges` and `GET /governance/entity-bridges` (all catalogs)
- `POST /governance/entity-bridges/{fact_key}/confirm`
- `POST /governance/entity-bridges/{fact_key}/reject`

All carry `dependencies=[Depends(require_confirmer)]`.

**Routes** — note I2: only the source-scoped listing is added here. The cross-catalog surface lives
on the queue (Task 4), which owns its own scoping story.

Confirm builds `Command(action="confirm_fact", aggregate="overlay_fact", aggregate_id=fact_key, args={ref, fact_type, use_case, target_event_id, note}, actor=identity, idempotency_key=f"confirm:{fact_key}:{identity.subject}", expected_version=None)`, calls `confirm_fact`, and **on VERIFIED calls `project_verified_bridge(conn, ref, now=None)`** — the bridge equivalent of the semantic-binding projection call, reporting `"projected"`/`"pending"` honestly.

Reject uses `reject_fact` with `category` first-class and `reason` carrying only the note, then `demote_bridge_edges`.

- [ ] **Step 1: Failing tests**
  - confirming writes a row to `entity_bridge_edge` with `confirmed_event_id` **non-null** and `status='VERIFIED'`
  - **the proposer cannot confirm** — four-eyes returns 409, and the response is a `JSONResponse`, not a raised exception (assert the audit row survives)
  - a non-confirmer role is refused by the dependency
  - rejecting a VERIFIED bridge demotes the edge
  - confirm is idempotent under the same idempotency key
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(governance): confirm and reject entity bridges`

---

## Task 2b: Bulk reject

**Files:** Modify `governance.py`, `bridge_governance.py`; Test alongside Task 2

The approved design rejects the eight `branch` candidates in one action. They are a 4×2
cross-product of the same two facts, not eight findings, so one judgement should settle them.

**Semantics to decide and state, not discover:** each rejection is its own governed command with its
own idempotency key — there is no bulk command in the overlay, and inventing one would put eight
facts behind a single audit row. So this is N commands, and **partial failure is the normal case**:
the response must report per-item outcomes, and the UI must show which succeeded rather than a
single success or failure.

- [ ] **Step 1: Failing tests** — 8 rejections produce 8 audit rows; when 3 of 8 are denied the
      response reports 5 succeeded and 3 denied with reasons, and **does not roll back the 5**;
      re-running is idempotent
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(governance): reject a candidate group in one action`

---

## Task 3: `GET /catalogs`

**Files:** Create `src/featuregen/api/routes/catalogs.py`; Test `tests/featuregen/api/test_catalogs_route.py`

Returns the catalogs the caller may see, with a display label and pending counts. **Verified: no such route exists today** — every governance route is `/sources/{source}/…`, which is why the UI must ask the user to type a slug.

- [ ] **Step 1: Failing tests** — returns `cib` and `ftr`; read-scoped so a caller who cannot see a catalog does not learn it exists; counts match the queue
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(api): list catalogs so the UI never asks for a slug`

---

## Task 4: The cross-catalog queue

**Files:** Create `src/featuregen/overlay/upload/governance_queue.py`; modify `governance.py`; Test `tests/featuregen/overlay/test_governance_queue.py`

`GET /governance/queue` — one list across every catalog and every decision kind, each item carrying `kind`, `fact_key`, `catalogs`, a business-language `title`, `proposed_by`, `proposed_at`, `available_actions`, and `blocks` (Task 5 fills this; ship it as an empty tuple).

- [ ] **Step 1: Failing tests** — merges entity bridges, joins and table facts into one list; each kind's read-scoping is preserved through the merge (the merge must not widen visibility); an empty queue is distinguishable from an unreachable one
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(governance): one cross-catalog decision queue`

---

## Task 5: `blocks` — what a pending decision holds up

**Files:** Modify `governance_queue.py`; Test as above

The highest-value column in the design and the only one requiring new thought: walk from a pending fact to what depends on it.

**Do not start from `overlay_fact_dependency`** — rev 1 said to, and that was backwards. It maps a
fact to what it *depends on* (`resolve.py:41`), and `dependents_of` runs the drift direction
(changed column → stale facts). The candidate path is `feature_derives_from` (`features.py:67`): a
feature whose derives-from spans both of the bridge's catalogs implies it needs that bridge.
**This is unverified — establish it before building on it.**

- [ ] **Step 1: Failing tests** — the customer bridge reports the feature(s) it blocks; the 8 branch bridges report **zero**; a confirmed fact blocks nothing
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(governance): report what each pending decision blocks`

**Note:** if the walk cannot be established from existing indexes, **stop and report** rather than inventing a linkage. An invented blast radius is worse than an empty one.

---

## Task 6: The queue screen

**Files:** Rewrite `frontend/src/screens/GovernanceReviewScreen.tsx`; create `frontend/src/screens/governance/{SummaryStrip,FilterBar,QueueRow,CandidateGroup}.tsx`; modify `frontend/src/api.ts`; Test alongside each

Follow the approved mockup. **The source text input is deleted** — replaced by filter chips populated from `GET /catalogs`.

- [ ] **Step 1: Failing tests** — the screen renders the queue with **no user input first**; filter chips come from the API, never hardcoded; a row whose `available_actions` omits `confirm` renders the button disabled with the four-eyes reason; the 8 branch candidates render as **one** group with reject-all; an empty kind renders as settled, not as an error
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(ui): governance review becomes a decision queue`

**Note:** the full frontend vitest suite hangs on worker-start in this environment — run changed files individually and let CI run the suite.

---

## Task 7: The visual system

**Files:** `frontend/src/index.css`; the components from Task 6

Semantic palette (`awaiting`/`blocking`/`confirmed`) defined as tokens **distinct from the brand accent**, applied as a status chip plus a left severity rail. Tabular numerals on counts and ages. Monospace confined to real identifiers. Both themes via `prefers-color-scheme` **and** `:root[data-theme=…]`.

- [ ] **Step 1: Failing tests** — severity is conveyed by a class, not by colour alone (accessibility); both theme paths define every token
- [ ] **Step 2–5:** Run / implement / run / commit — `feat(ui): semantic status system for governance`

---

## Acceptance

**Prerequisite (R1): the ingest actor fix must land first**, on the parallel stream. Until it does,
every bridge is proposed by the uploader and no uploader can confirm one — so acceptance would be
testing a lockout, not a feature.

Against the kind cluster, once R1 is in: upload a catalog, watch the service actor propose the
bridges, land on Governance Review with **no input**, see the customer bridge first and marked as
blocking, confirm it, and observe `entity_bridge_edge` gain its first row with a non-null
`confirmed_event_id`. Reject the branch group in one action and see per-item outcomes.

The **existing nine stay unconfirmable** (R2) and are not part of acceptance. Rescuing them means
reject → re-propose → confirm, and whether their proposer may reject them is unverified.

**The design must handle "every item here is yours" as a first-class state, not an edge case.** The
approved mockup shows one greyed four-eyes row among many; before R1 lands, *every* row is greyed for
the uploading user. The screen should say whose decision each item waits on rather than presenting a
list of things the viewer cannot action.

## Out of scope

- Wiring `materialize/` to read `entity_bridge_edge` — nothing under `materialize/` imports it today; that is the separate piece that makes a confirmed bridge reach generated code.
- Two-owner dual sign-off — `authority.py:136` defers it until a bridge is live-traversable.
- Readiness stays a status view; the design moves it out of the decision queue.
