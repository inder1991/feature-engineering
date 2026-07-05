# SP-2 Intake Retirement — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Retire the superseded SP-2 intake discovery + feature-contract aggregate (the loop + the new
`overlay/upload/contract/` flow replace it), keeping only what the overlay reuses — as a careful
worker-rewire + event-backbone-detach + delete, NOT a blind file deletion. Phase 6 of the
[hypothesis-feature-contract plan](2026-07-05-hypothesis-feature-contract-plan.md); rationale in the
[design](../specs/2026-07-05-hypothesis-feature-contract-design.md).

**Architecture:** The SP-2 intake is a *live registered* capability, not dormant dead code: its 12
`feature_contract` event schemas are registered into `event_registry()` (SP-0 `append` validates against
these), its command catalog + authz rows are seeded at worker bootstrap, and the aggregate is referenced by
the generic event backbone (`_append`/`serde`/`store`/`outbox`). Retirement therefore proceeds
inside-out: (1) pin the keep-set, (2) rewire the worker, (3) detach the aggregate from the backbone, (4)
delete modules + tests, (5) clean DB artifacts, (6) verify. Each phase leaves the suite green.

**Tech Stack:** Python 3.12, Postgres (event-sourced), `uv run pytest`.

## Global Constraints
- **Keep-set (reused outside the discovery flow — MUST NOT be broken):** `intake/redaction.py`,
  `intake/llm.py`, `intake/store.py`. **CORRECTION (review 2026-07-05):** `intake/llm.py` is NOT
  self-contained — `llm.py:26` imports `LLM_CALL_RECORDED` from delete-set `intake/events.py`, `:34`
  imports `append_feature_contract_event` from `store.py`, and `call_llm:558` appends to the
  `feature_contract` aggregate. So the keep-set must be **trimmed before delete** (Phase 1a): remove
  `call_llm` (used ONLY by delete-set) and the `events`/FC `store` imports from `llm.py`. The overlay uses
  `record_llm_call` (decoupled — writes only `llm_call`), NOT `call_llm`, so this is safe.
- **`intake/llm_claude.py`:** the real `LLMClient` adapter. **HARD KEEP — it now HAS a live caller**
  (superseding the earlier "zero external callers / deleting also defensible" note): the HTTP API's
  production entrypoint `featuregen.api.app.create_app_from_env` imports `ClaudeConfig`/`build_claude_llm`
  and constructs the adapter when `FEATUREGEN_LLM_PROVIDER=anthropic`. Deleting it breaks the API at
  import time. Treat it exactly like the rest of the keep-set (signatures frozen).
- **The overlay real-provider enrichment + the whole `overlay/upload/contract/` flow MUST keep passing** —
  they import `intake.llm` (LLMClient/LLMRequest/drive_structured_call/record_llm_call/compute_input_hash/
  STATUS_FAILED) and `intake.redaction` (assert_llm_safe/build_llm_inputs/RedactionResult/EgressViolation/
  DefaultIntentRedactor/IntentRedactor/_scan). Do not change these signatures.
- **No blind deletes.** Every module removed is first proven to have no live importer outside the delete-set.
- **The worker must still boot** and the full suite stay green after every phase.
- **TDD / frequent commits.** Real Postgres via the `db` fixture. Commit per phase.
- **Reversibility:** work on a branch; the retirement is one reviewable PR.

## Delete-set (target)
`candidates`, `scoring`, `mcv`, `doubt_router`, `critique`, `commands` (2549 lines), `read_model`,
`events` (the FC event types), `state`, `contract`, `bootstrap` (register_sp2/seed_sp2_authz),
`banking_catalog`, `catalog`, `blobs` — plus their ~40 test files.

---

## Phase 1 — Trim & pin the keep-set

**Files:** Modify `src/featuregen/intake/llm.py` (remove `call_llm` + the FC-append coupling).

- [ ] **Step 1 (VERIFIED coupling — must resolve, not just check):** `llm.py` imports delete-set
  `events`/`store` FC symbols and defines `call_llm` (appends `feature_contract`). Confirm `call_llm`'s
  only callers are delete-set: `grep -rn "call_llm(" src/featuregen | grep -v intake/llm.py` → expect only
  `candidates.py`/`critique.py`/`commands.py`. Then **delete `call_llm` from `llm.py`** and its imports of
  `intake.events` (`LLM_CALL_RECORDED`) + `append_feature_contract_event` from `store`. Keep
  `record_llm_call` (decoupled) untouched — the overlay depends on it.
- [ ] **Step 1b:** Grep the trimmed keep-set for any remaining delete-set dependency:
  `grep -nE "candidates|scoring|mcv|doubt_router|critique|commands|read_model|events|state|contract|bootstrap|banking_catalog|catalog|blobs" src/featuregen/intake/{redaction,llm,store}.py`
  Now expect no matches. Resolve any remainder before proceeding.
- [ ] **Step 2:** Confirm `record_llm_call` (intake/llm.py) does NOT append to the `feature_contract`
  aggregate (enrich_llm relies on it being decoupled): read the function; it must write only to the
  `llm_call` store. If it couples, extract the decoupled path the overlay already uses.
- [ ] **Step 3:** Run the overlay + contract suites to capture the green baseline:
  `uv run pytest -q tests/featuregen/overlay/`.
- [ ] **Step 4: Commit** (docs/notes only if anything moved) `chore(sp2-retire): pin keep-set, prove isolation`.

---

## Phase 2 — Rewire the worker off the SP-2 bootstrap

**Files:** Modify `src/featuregen/runtime/worker.py`; create `src/featuregen/runtime/_park.py` (or a
kept util) for the relocated helper; Test the worker-boot test + `tests/featuregen/runtime/`.

**Interfaces (Produces):** `_run_is_parked(...)` relocated out of `intake/commands.py` into a kept module,
same signature.

- [ ] **Step 1:** Relocate `_run_is_parked` from `intake/commands.py` to `runtime/_park.py` (it is a
  bounded-exhaustion guard, not SP-2-specific); update `worker.py:130` import. Write/keep a unit test for it.
- [ ] **Step 2:** Remove the `register_sp2(...)` + `seed_sp2_authz(conn)` calls from worker bootstrap
  (`worker.py:~515`). Leave a one-line comment noting SP-2 intake was retired (see this plan).
- [ ] **Step 3:** Run the worker-boot / runtime tests; fix fallout (a boot test may assert SP-2 commands
  registered — update it to assert they are absent).
- [ ] **Step 4: Commit** `refactor(sp2-retire): unmount SP-2 intake from the worker bootstrap`.

---

## Phase 3 — Detach the feature_contract aggregate from the event backbone

**Files:** Modify `src/featuregen/aggregates/_append.py`, `src/featuregen/events/serde.py`,
`src/featuregen/events/store.py`, `src/featuregen/runtime/outbox.py` (only where they special-case
`feature_contract`); Test the corresponding backbone tests.

- [ ] **Step 1:** For each of the four files, read every `feature_contract` reference and classify:
  generic (an aggregate-type entry in a registry/enum) vs behavioural (special-casing). List them.
- [ ] **Step 2:** Confirm no persisted FC events exist that need replay: check whether any test/prod flow
  appends `feature_contract` events outside the delete-set (Phase-1 grep already showed only
  `intake/*` + `enrich_llm.py`'s comment). Removing the event-type registration is safe iff nothing
  appends them post-retirement.
- [ ] **Step 3:** Remove the `feature_contract` registrations/branches (the aggregate no longer exists).
  Where a registry requires an entry, remove the entry; where serde/store branch on it, drop the branch.
- [ ] **Step 4:** Run the events/aggregates/outbox suites; fix fallout.
- [ ] **Step 5: Commit** `refactor(sp2-retire): detach feature_contract aggregate from the event backbone`.

---

## Phase 4 — Delete the discovery + contract modules and their tests

**Files:** Delete `src/featuregen/intake/{candidates,scoring,mcv,doubt_router,critique,commands,read_model,
events,state,contract,bootstrap,banking_catalog,catalog,blobs}.py` and their test files
(~40 under `tests/featuregen/intake/`). Modify `src/featuregen/intake/__init__.py` if it re-exports any.

- [ ] **Step 1:** Re-grep each module for live importers OUTSIDE the delete-set (defence in depth after
  Phases 2–3): `grep -rln "intake.<mod>" src/featuregen | grep -v intake/`. Expected: empty for every
  module. Any hit is an unresolved coupling — stop and resolve.
- [ ] **Step 2:** Delete the modules. Delete the ~40 discovery/contract test files (keep the ~15
  redaction/llm/store test files — list them explicitly from
  `grep -rl "redaction\|intake.llm\|intake.store" tests/featuregen/intake/`).
- [ ] **Step 3:** Fix `intake/__init__.py` and any lingering imports; run `uv run pytest -q` — iterate to green.
- [ ] **Step 4: Commit** `refactor(sp2-retire): delete SP-2 intake discovery + contract modules and tests`.

---

## Phase 5 — Clean the DB artifacts

**Files:** New migration `src/featuregen/db/migrations/0961_retire_sp2.sql`; consider `0508`/`0509`/`0510`
FC-table disposition.

- [ ] **Step 1:** Decide FC-table disposition. The `feature_contract_events` tables (0508) are now
  write-dead. Prefer **leave in place** (dropping tables in an append-only WORM store is itself risky) and
  instead stop writing them (already done in Phases 2–4). Document this in the migration comment.
- [ ] **Step 2:** Write `0961_retire_sp2.sql` (idempotent) to remove the seeded SP-2 authz rows +
  the `feature_contract` projection checkpoint that `seed_sp2_authz` created — so a fresh DB does not carry
  dead capability rows. `DELETE FROM authz_policy WHERE action IN (...SP-2 actions...);`
  `DELETE FROM projection_checkpoints WHERE projection_name = 'feature_contract';` (both idempotent).
- [ ] **Step 3:** Run the full suite (migration auto-applies via glob).
- [ ] **Step 4: Commit** `chore(sp2-retire): remove SP-2 authz rows + FC projection checkpoint (0961)`.

---

## Phase 6 — Verify end-to-end

- [ ] **Step 1:** `uv run pytest -q` — full suite green.
- [ ] **Step 2:** Worker boot test passes without SP-2 registration.
- [ ] **Step 3:** The overlay real-provider enrichment test + the `overlay/upload/contract/` suite pass
  (the keep-set is intact).
- [ ] **Step 4:** `grep -rn "intake\." src/featuregen | grep -v "intake/\(redaction\|llm\|store\|llm_claude\)"`
  returns nothing outside the keep-set — the retirement is complete.
- [ ] **Step 5:** Update memory + the hypothesis-feature-contract plan (mark Phase 6 done).
- [ ] **Step 6: Commit** `refactor(sp2-retire): complete — SP-2 intake retired to redaction+llm keep-set`.

---

## Self-Review checklist
- Keep-set (`redaction`/`llm`/`store`/`llm_claude`) untouched in signature; overlay + contract suites green
  after every phase.
- No module deleted while a live importer outside the delete-set remains (re-grepped in Phases 1, 3, 4).
- Event-backbone detach verified against "nothing appends feature_contract post-retirement."
- WORM/append-only tables left in place, not dropped; only dead authz/checkpoint rows removed.
- Worker boots without SP-2; boot test updated to assert absence.
- ~40 discovery test files deleted; ~15 keep-set test files retained (listed explicitly).

## Risk register (re-ordered per the 2026-07-05 review)
- **Keep-set coupling (HIGHEST — verified):** `intake/llm.py` imports delete-set `events`/`store` FC
  symbols and defines `call_llm`. Deleting `events.py` without first trimming `llm.py` (Phase 1) breaks the
  overlay + every overlay test (`from featuregen.intake.llm import FakeLLM`). This is the real blocker.
- **Event-backbone coupling (LOW — was mis-ranked as highest):** `_append`/`serde`/`store` reference a
  generic nullable `feature_contract_id` correlation column, `outbox` has one `aggregate=="feature_contract"`
  partition case, and `0508` has a dead CHECK arm. Nothing appends FC events post-retirement, so these are
  **leave-able in place** — no shim needed. Only remove if a later cleanup wants it.
- **`llm_claude` HAS a live caller (updated):** `featuregen.api.app.create_app_from_env` (the uvicorn
  `--factory` entrypoint) calls `build_claude_llm` when `FEATUREGEN_LLM_PROVIDER=anthropic`. The earlier
  "no callers / deleting defensible" assessment predates the API layer — `llm_claude.py` is a hard keep.
- **Scale:** ~2549-line `commands.py` + ~40 test files — expect several green-fix iterations in Phase 4.
