# Hypothesis-Driven Feature Contract — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Build the reconciled hypothesis-driven feature-contract flow — retire the redundant SP-2 intake
discovery + old assisted-definition machinery, salvage the governance half onto the working feature loop:
mandatory-hypothesis intake → loop discovery (anchor + scored alternatives + advisory) → Human Gate #1
(considered-set audit) → catalog-grounded contract authoring (draft→critique→refine→MCV) → human confirm →
governed, versioned, drift-linked contract.

**Architecture:** New `overlay/upload/contract/` package composes what's built — the feature loop
(`recommend_feature_sets`/`recommend_set`), the deterministic gauntlet (= MCV), the audited LLM seam
(`enrich_llm.audited_enrich_call`), `feature_freshness`/`features_affected_by` — with SP-2's **redactor +
egress guard** (`intake/redaction.py`) and **feature-contract events** (`intake/events.py`), re-grounded in
the upload catalog. Deterministic control flow; LLM at bounded nodes; human-gated writes; every LLM call audited.

**Tech Stack:** Python 3.12, Postgres (event-sourced), the audited LLM seam, `uv run pytest`.

## Global Constraints
- **Authority model (verbatim):** LLM suggests/critiques → platform validates (deterministic gauntlet) →
  human confirms → registry governs. AI never decides/auto-approves/silently-swaps-the-anchor.
- **No PII/raw data to the LLM** — metadata-only + the redactor/`assert_llm_safe` on any free text
  (hypothesis, definition).
- **Reuse, don't rebuild:** the loop, the gauntlet, the audited seam, `intake/redaction.py`,
  `intake/events.py` feature-contract event types.
- **Human-gated writes:** `CONTRACT_CONFIRMED` is the only write that governs a contract; Gate #1 confirms
  the chosen option.
- **TDD, frequent commits.** Real Postgres via the `db` fixture; `FakeLLM` for LLM nodes (no network).
- This is **multi-phase / multi-subsystem** — build and review **one phase at a time**; each phase is a
  coherent, independently-testable slice. Later phases get their own detailed task breakdown when reached.

---

## Phase 1 — Intake (mandatory hypothesis + optional definition + text redaction)

**Files:** Create `src/featuregen/overlay/upload/contract/__init__.py`, `.../intake.py`;
Test `tests/featuregen/overlay/upload/contract/test_intake.py`.

**Interfaces (Produces):**
- `@dataclass Intent{ intent_id, hypothesis, definition, intake_mode, redacted_hypothesis, redacted_definition, classification }`
- `submit_intent(*, hypothesis: str, definition: str = "", actor, redactor=None) -> Intent` — **denies**
  (raises `IntentValidationError`) when `hypothesis` is blank (no run created — resubmit, not a terminal
  reject); fixes `intake_mode` = `definition` if `definition` else `hypothesis` (immutable);
  redacts+classifies BOTH texts via the SP-2 redactor (`intake/redaction.py`), classified against its own
  scanner. **Persistence deferred to Phase 2** — the intent is recorded together with the considered-set at
  Gate #1, so Phase 1 stays a pure, DB-free intake unit (no `conn`).

- [ ] **Step 1: Failing test** — `submit_intent` with no hypothesis raises `IntentValidationError`; with a
  hypothesis sets `intake_mode='hypothesis'`; with both sets `intake_mode='definition'` and both redacted
  fields are populated (assert the raw text is NOT in the redacted output).
- [ ] **Step 2: Run — fails** (module missing).
- [ ] **Step 3: Implement** — reuse `IntentRedactor`/`build_llm_inputs`/`assert_llm_safe` from
  `intake/redaction.py`; grep an existing intake test for the redactor construction pattern.
- [ ] **Step 4: Run — passes.**
- [ ] **Step 5: Commit** `feat(contract): intent intake — mandatory hypothesis + text redaction`.

---

## Phase 2 — Gate #1 bridge (loop → considered-set + recorded choice)

**Files:** Create `.../contract/gate1.py`; Test `.../contract/test_gate1.py`.

**Interfaces:**
- `@dataclass ConsideredSet{ intent_id, anchor: FeatureIdea | None, alternatives: list[FeatureSet], advisory: list[str] }`
- `build_considered_set(conn, intent: Intent, client, *, entity=None, target_ref=None, now, roles) -> ConsideredSet`
  — runs the loop (`recommend_feature_sets` from the redacted hypothesis; `recommend_set` for the advisory
  pick); the anchor is the requester's definition (validated through the gauntlet) pre-selected.
- `confirm_gate1(conn, intent_id, *, chosen_source: str, chosen_option_id: str, actor, why: str = "") -> str`
  — records the considered set + choice + who + (conditionally) why (event); returns the chosen feature id.

- [ ] **Step 1: Failing test** — `build_considered_set` returns the anchor (from the definition) + scored
  alternatives (validated by the gauntlet); a leaky/stale alternative is absent (the loop already rejects it).
- [ ] **Step 2–4: Implement + pass** — thin composition over `recommend_feature_sets`/`recommend_set`.
- [ ] **Step 5: Failing test** — `confirm_gate1` records the choice + considered set; a bad `chosen_option_id`
  (not in the set) is rejected. Implement + pass.
- [ ] **Step 6: Commit** `feat(contract): Gate #1 — considered-set from the loop + recorded choice`.

---

## Phase 3 — Contract authoring (catalog-grounded, audited)

**Files:** Create `.../contract/author.py`; register a contract-draft output schema in `enrich_llm`;
Test `.../contract/test_author.py`.

**Interfaces:**
- `draft_contract(conn, chosen_feature_id, client, *, actor) -> ContractDraft{ definition, io_schema, grain,
  as_of, join_path, aggregation, unit, lineage, assumptions }` — via `audited_enrich_call` (new
  `overlay_contract` schema), grounded in the chosen feature's catalog metadata (column/table definitions,
  grain, as-of, additivity, entity, the deterministic cross-catalog join path). Emits `DRAFT_CONTRACT_PRODUCED`.

- [ ] Tasks: register schema → failing test (draft grounded, no raw data in inputs) → implement via the
  audited seam → pass → commit `feat(contract): catalog-grounded contract authoring (audited)`.

---

## Phase 4 — Critique → refine loop + MCV

**Files:** Create `.../contract/review.py`; Test `.../contract/test_review.py`.

**Interfaces:**
- `critique_contract(conn, draft, client, *, actor) -> list[Finding]` — adversarial LLM review (leakage/
  unsafe-aggregation/wrong-grain/undocumented-assumption/drift-fragility); emits `CONTRACT_CRITIQUED`.
- `refine_contract(conn, draft, findings, client, *, actor) -> ContractDraft` — emits `CONTRACT_REFINED`.
- `validate_minimum(conn, draft) -> (bool, reasons)` — **MCV = the deterministic gauntlet** (reuse
  `_validate_idea`'s checks: leakage/freshness/additivity/point-in-time/join-path); emits
  `MINIMUM_CONTRACT_VALIDATED` on pass.
- A bounded **critique→refine loop**, symmetric with the feature loop: **the deterministic MCV runs
  inside every pass** and its failures feed `refine` alongside the LLM critique — so refinement is driven
  by deterministic defects, not just LLM opinion, and the LLM never gates alone:
  ```
  for _ in range(budget):
      findings = critique_contract(draft) + validate_minimum(draft).reasons   # LLM + deterministic
      if not findings: break
      draft = refine_contract(draft, findings)
  ```

- [ ] Tasks: TDD each (critique flags a planted leak; refine clears it; MCV rejects an unsafe draft) →
  commit `feat(contract): critique→refine loop + deterministic MCV`.

---

## Phase 5 — Confirm + govern (versioned, drift-linked)

**Files:** `.../contract/govern.py`; Test `.../contract/test_govern.py`.

**Interfaces:**
- `confirm_contract(conn, draft, *, actor) -> contract_id` — the human gate; emits `CONTRACT_CONFIRMED`;
  registers a **versioned** contract; wires its derives-from into the feature layer (`register_feature` +
  `feature_derives_from`) so `feature_freshness`/`features_affected_by` apply.
- `contract_freshness(conn, contract_id, now)` / drift-impact — reuse the feature layer; a drifted source
  stales the contract (REVERIFY), read path fails closed.

- [ ] Tasks: TDD (confirm registers + versions; a re-confirm bumps the version; a stale source → contract
  REVERIFY) → commit `feat(contract): confirm + govern (versioned, drift-linked)`.

---

## Phase 6 — Retire the superseded SP-2 intake discovery — **DEFERRED (needs its own plan)**

**Pre-flight run 2026-07-05 — it is NOT a clean delete. Three blocking findings:**
1. **Wired into the production runtime:** `runtime/worker.py` imports `intake.commands` (`_run_is_parked`)
   + `intake.bootstrap` (`register_sp2`/`seed_sp2_authz`) — the SP-2 intake flow is mounted in the worker,
   not just dormant. Deleting the discovery modules breaks the worker; the worker must be rewired first.
   (Correction: `read_model` is NOT a worker import — it has no importer outside `intake/`.)
2. **The keep-set is entangled with the delete-set:** `intake/state.py` has `mcv_passed()`, folds
   `candidates`, and imports are shared with `mcv`/`commands`. The feature-contract `events`/`state`/
   `contract` (which Phases 1–5 reuse) cannot be cleanly separated from `candidates`/`scoring`/`mcv`/
   `doubt_router` — it is one aggregate. A split, not a delete.
3. **~459 test functions across 55 intake test files** exercise this flow — all need triage.

**Decision (2026-07-05):** DEFERRED. The dormant SP-2 discovery harms nothing, and Phases 1–5 deliver the
new contract flow without touching it. Retirement is a genuine refactor (worker rewire + aggregate split +
test triage) that deserves its own spec+plan — not a tail-end deletion. Do NOT execute blind.

---

## Self-Review checklist
- Every LLM node routes through the audited seam + redaction (no raw text/data egress).
- MCV is deterministic (the gauntlet); the LLM never gates.
- Human confirms at Gate #1 and CONTRACT_CONFIRMED; nothing auto-registers.
- Reuses named (loop, gauntlet, audited seam, redactor, contract events) — not re-implemented.
- Phase 6 delete is pre-flighted against importers before removal.
- No-DB limits honored: cross-catalog joins human-confirmed; no performance prediction.
