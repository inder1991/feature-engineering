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

The highest-value column in the design and the only one requiring new thought: walk from a pending fact to what depends on it. `overlay_fact_dependency` already indexes a bridge's two catalog endpoints (`projection.py:56-62`) — start there and establish what else is reachable before designing the walk.

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

Against the kind cluster: land on Governance Review with **no input**, see 10 items with the customer bridge first marked as blocking, confirm it, and observe `entity_bridge_edge` gain its first row with a non-null `confirmed_event_id`. Reject the 8 branch candidates in one action.

## Out of scope

- Wiring `materialize/` to read `entity_bridge_edge` — nothing under `materialize/` imports it today; that is the separate piece that makes a confirmed bridge reach generated code.
- Two-owner dual sign-off — `authority.py:136` defers it until a bridge is live-traversable.
- Readiness stays a status view; the design moves it out of the decision queue.
