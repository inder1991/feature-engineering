# M0 — Walking Skeleton (governed churn flow UI) — Implementation Plan

> **For agentic workers:** bite-sized TDD tasks. Frontend-only; no backend changes.

**Goal:** a user drives the governed contract flow end-to-end — brief → considered set → confirm → a
`DESIGN-CHECKED` contract — in a minimal UI, on the app's existing session.

**Architecture:** the backend flow is fully built (`/contract/considered-set` → `/contract/draft` →
`/contract/confirm`, `GET /contracts`). M0 adds a thin React surface over it: Vite proxy + 5 `api.ts`
functions + 1 route + 1 `ContractScreen` (3 phases). Ride the existing `X-User/X-Roles` client (real
Bearer login is a separate app-wide increment — deviation from plan v2's "Bearer", noted deliberately).

**Tech stack:** React 19 + TS + Vite, hash router (`nav.ts`), Vitest + RTL. Commands: `npm run typecheck`
(`tsc -b`), `npm test` (`vitest run`), from `frontend/`.

## Global constraints
- No backend changes. No new auth. The governed path already earns `DESIGN-CHECKED` (honest) — M0 just
  drives it; the direct-`POST /features` false-stamp fix stays in A1.
- Follow existing patterns: `api.ts` `request<T>()`/`post<T>()`, colocated `*.test.tsx`, `vi.stubGlobal`
  for fetch, `vi.mock('./api', importOriginal)` for screens, hash-nav via `HashChangeEvent` in `act()`.
- TS `erasableSyntaxOnly` is on — no constructor parameter-property shorthand.

## Exact response shapes (confirmed from the backend)
```
considered-set → { intent_id, anchor: Idea|null, alternatives: [{lens, features: Idea[]}],
                   recommendation: {recommended_lens, reasoning, caveat}|null }
Idea           = { name, description, derives_from: string[], aggregation, grain_table,
                   derives_pairs: [string,string][], verification, critic_note, rationale }
draft          → { draft: Draft, unresolved: unknown[], intent_id }
Draft          = { feature_name, definition, grain_table, aggregation, as_of_column,
                   derives_from: string[], target_ref, derives_pairs: [string,string][], join_path: object[] }
confirm(body = {...Draft, intent_id}) → Contract = { contract_id, feature_id, feature_name, version }
GET /contracts → [{ contract_id, feature_id, feature_name, version, verification, created_at }]
```

---

## Task 1 — API client: contract functions + Vite proxy
**Files:** Modify `frontend/vite.config.ts` (add `/contract`,`/contracts` to proxy); Modify
`frontend/src/api.ts` (types + 5 fns); Test `frontend/src/api.test.ts`.
- [ ] Write failing tests: `contractConsideredSet` POSTs `/contract/considered-set` with `{hypothesis,
  objective,...}` + auth headers; `contractDraft` POSTs `/contract/draft`; `contractConfirm` POSTs
  `/contract/confirm` with `{...draft, intent_id}`; `listContracts` GETs `/contracts?limit=`.
- [ ] Add TS interfaces (`Idea`, `ConsideredSetResp`, `ContractDraft`, `DraftResp`, `Contract`,
  `ContractSummary`) + the 5 functions using `post<T>()` / `request<T>()`.
- [ ] Add `/contract` and `/contracts` to `vite.config.ts` proxy list.
- [ ] `npm test` green (api.test.ts); `npm run typecheck` green. Commit.

## Task 2 — Route + nav wiring
**Files:** Modify `frontend/src/nav.ts` (add `'contract'` to Route union + known routes); Modify
`frontend/src/App.tsx` (PAGES entry + render switch); Test `nav.test.ts`, `App.test.tsx`.
- [ ] Failing test: `parseHash('#/contract')` → `contract`; App renders the contract screen when nav'd.
- [ ] Add the route; add `ContractScreen` to the switch (stub component first if needed for the nav test).
- [ ] Green + typecheck. Commit.

## Task 3 — ContractScreen (brief → considered-set → confirm)
**Files:** Create `frontend/src/screens/ContractScreen.tsx`; Test
`frontend/src/screens/ContractScreen.test.tsx`.
- [ ] Failing test: fill brief (hypothesis + objective) → "Generate" calls `contractConsideredSet` →
  renders anchor + alternatives; pick one → "Draft" calls `contractDraft` → renders the draft +
  "safe, not proven" caveat; "Confirm & govern" calls `contractConfirm` → shows the minted contract id
  + `DESIGN-CHECKED`.
- [ ] Implement the screen as a local phase machine (`'brief'|'set'|'draft'|'done'`), carrying
  `intent_id` + the chosen option + the draft between phases; surface `unresolved` warnings + the
  recommendation caveat; ApiError → inline error.
- [ ] Green + typecheck + `npm run lint`. Commit.

## Done-when
A user navigates to the Contract screen, submits a churn hypothesis, sees the considered set, approves a
feature, and gets a governed `DESIGN-CHECKED` contract — all in the UI, on the app session. Frontend
suite + typecheck green. ✅ **Tasks 1–3 done** (commits 04bf3c0, fcac3a0, 7bb3be6).

---

## REVISED — fold governance INTO the "Generate features" (Workbench) screen (user decision, 2026-07-08)

**Decision:** the separate "Govern a feature" screen created a confusing two-door state; the Generate
screen is the better home. Governance becomes a step *inside* the Workbench, not a standalone screen.
**Generation path:** use **`considered-set`** as the single path — it's a strict superset of
`recommend-sets` (calls it internally) and is the only one that carries the persisted intent + snapshot
that the sign-off (`confirm`) requires. `recommend-sets` becomes redundant.

**Kept from M0:** the `api.ts` contract client (proven wiring). **Retired (at the end):** the separate
`ContractScreen` + the "Govern a feature" nav item.

**Integration task sequence:**
- **I1 — Workbench generates via `considered-set`.** Add a one-line hypothesis input; "Generate" calls
  `contractConsideredSet` (keeps the objective + scope); render the returned anchor + alternatives +
  advisory recommendation in the existing candidate UI. The generation is now governance-ready.
- **I2 — draft-vs-govern at approval.** The approval tray gains: **Save as draft** (quick) vs **Govern**
  (`contractDraft` → `contractConfirm` → a signed contract). Show the "safe, not proven" caveat + the
  minted contract id.
- **I3 — A1 honest lifecycle.** "Save as draft" stamps honest **`UNVERIFIED`** (introduce the rung,
  flip the `FeatureSpec` default, CHECK constraint, re-stamp migration + consumer-impact report).
- **I4 — cleanup.** Retire `ContractScreen` + the nav item; deprecate `recommend-sets`.

**Dependency:** I2's "Govern" works on today's backend; I2's "Save as draft" is honest only after I3.
Order: I1 → I2 (govern half) → I3 (honest draft) → I4.
