# Phase 3C.2b-i-B (DEMO-GRADE, INTERNAL ENDPOINT) — FeatureIdea Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Prove — through a **standalone internal demo / Gate-1 endpoint**, NOT the live considered set — that an LLM cross-catalog `FeatureIdea` can be deterministically normalized into a fully-governed `MultiSourcePlannerIntentV1` for A, on real authority, such that the generated feature faithfully matches the proposal. Guarantee `raw proposal ≡ normalized planner request ≡ generated feature`.

**Architecture:** `LLM raw proposal → B normalization → A planning → internal governed result / Gate-1 report`. B runs off to the side of the user workflow: it consumes the **raw** proposal (before `_vet` can silently drop operands), governs it, and reports. It never adds to the considered set, never enters snapshot/selection/draft, never depends on `is_live`.

**Tech Stack:** Python 3.12, frozen slotted dataclasses + lowercase-snake `StrEnum` (NOT pydantic), psycopg, pytest, `uv`.

**Spec:** `docs/superpowers/specs/2026-07-19-phase3c2b-i-b-feature-idea-adapter-shadow-design.md` (Gate-1 core, internal-endpoint delivery).

## Global Constraints

- **`raw ≡ normalized ≡ generated` is the whole point.** Capture the raw pre-`_vet` operand set; if `_vet` (or anything) would drop/rewrite an operand → `PROPOSAL_LOSSY`. Never govern a different feature than was proposed.
- **Authority, not display, and no shortcuts:** concept authority from real lifecycle-active evidence (source-attested / human-confirmed only, never `graph_node.concept`); source grain + the **source entity** from governed facts + the confirmed concept's `entity_link` (never assumed, never `is_grain`); the exact time column governed and **pinned** (never "first column matching a displayed concept").
- **Two-axis success:** a result is "governed" only when A returns `resolution_status == resolved` **AND** `contract_result_status == resolved` with the selected contract ids. Physical assembly succeeding is not enough.
- **Deterministic, fail-closed:** every reject is a typed reason; no inference latitude; ordering-sensitive ops (`RATIO`/`DIFFERENCE`) → `OPERAND_ORDER_AUTHORITY_MISSING`.
- **Emit A's exact carriers** from `planner/multisource_contracts.py`; A independently revalidates `grain_fact_key`.
- **Zero effect on the live path:** B is a standalone endpoint. It does NOT modify `build_considered_set`/`_reject_cross_catalog_llm`, does NOT add to the considered set, does NOT touch `is_live`. `build_considered_set` is byte-identical (Task 9 asserts it).
- **DEFERRED-FOR-DEMO, REQUIRED-FOR-REAL (out of scope; do NOT build):** the **considered-set integration** and its snapshot→selection→draft→confirm path (needs a multi-source plan-envelope that survives the workflow without recomputation/permissive fallback); ordering-sensitive ops; Gate 2 (real-population); the automatic authority-**provisioning** capability; the production capture-integrity telemetry / worker / canonical-input replay; the activation-interlock **live flip** and removing `find_cross_catalog_path`.
- Commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Reused surfaces:** `field_evidence.py` (`read_active_field_evidence`, `record_field_evidence`, `to_view`), `field_authority.py` (`EvidenceProducer`/`AssertionStrength`/`EvidenceLifecycle`), `field_revalidation.py` (`active_disqualifiers_for`), the **real human-confirmation write path** (proposal → a *different* human confirms → CAS/version check → projection — read how existing confirm flows do it, e.g. grain via `propose_fact`→`_confirm_grain`; concept via the field-evidence confirm path), `concepts.py` (`CONCEPT_REGISTRY`, `Concept` group/pit_role/additivity/entity_link), `resolve.py` (`resolve_fact`) + `facts.py` (`fact_key`), `planner/multisource_contracts.py` + `planner/multisource_plan.py` (`plan_multi_source`), `feature_assist.py` (`FeatureIdea`, `_vet`:474, the raw `derives_from`).

---

### Task 0: Prerequisite — rebase, migration reconciliation, constants

- [ ] **Step 1: Rebase the branch onto current origin/main and renumber A's migration.** origin/main is at `1017_field_evidence_note` and already contains `1010_asset_detail_indexes`, so A's committed `1010_multisource_assembly_shadow.sql` **collides**. Rebase the branch onto `origin/main`; rename `1010_multisource_assembly_shadow.sql` → the next free number (**1018**) and update every `1010` reference in `multisource_shadow_store.py`/tests. Re-run `uv run pytest -k multisource -q` to confirm A is green on the new base. **Do NOT `git add -A`** — the parallel session's WIP must stay untouched; stage only the renamed migration + its references. (If the rebase conflicts with the parallel WIP, stop and report — do not stash their work.)
- [ ] **Step 2: Confirm the free number** and that B needs **no** migration (the internal endpoint reports in-process / to a Gate-1 result object; the production telemetry store is deferred).
- [ ] **Step 3: Constants** — `ROLE_POLICY_VERSION`, `OPERATION_ALIAS_VERSION`. (No live/considered-set flag — B is a standalone endpoint, not a flagged branch in `build_considered_set`.)
- [ ] **Step 4: Commit.**

---

### Task 1: Concept-authority resolver

**Files:** Create `planner/b_concept_authority.py`; Test.
**Interfaces:** `resolve_planner_concept_binding(conn, logical_ref) -> PlannerConceptBinding | ConceptAuthorityRejection`.

- [ ] Failing test (spec §3): authority established via the **real** evidence-confirm path (source-attested via a technical-source profile, human-confirmed via the confirmation command) → correct authority class; conflict/missing/stale/rejected/not-in-registry/pending each → the exact reason. **No `expected_concept` input.**
- [ ] Implement per spec §3 (active evidence, accepted-pairs-only, precedence human>source, all-lifecycle history for missing/stale/rejected, registry membership, `DISPLAY_CONCEPT_MISMATCH` diagnostic only). Run → PASS. Commit.

---

### Task 2: Deterministic computation-role policy (REFINED — #5)

**Files:** Create `planner/b_role_policy.py`; Test.
**Interfaces:** `computation_role(concept: Concept) -> SemanticRole | RolePolicyReject`.

- [ ] Failing test — a `Concept` is `MEASURE` **only** when it is genuinely numeric-aggregatable: not just `group ∈ {monetary,...}` but ALSO an additive class (`additivity ∈ {additive, semi_additive, non_additive}`, not `n/a`) and no disqualifier. Specifically: `impairment_stage` (categorical/ordinal, `additivity=n/a`) → **NOT** MEASURE (→ reject `ROLE_NOT_AGGREGATABLE`); `green_flag` (a flag, `additivity=n/a`) → **NOT** MEASURE; `monetary_stock` (additive/semi) → MEASURE; a `temporal` concept with an accepted `pit_role` → TIME, with `pit_role=none` (`duration_tenure`/`vintage`) → NOT TIME; `identifier` with `entity_link` → COUNTED. Policy total over every `Concept.group`.
- [ ] Implement using `group` **and** `additivity` **and** `pit_role` **and** `entity_link` (versioned `ROLE_POLICY_VERSION`). A concept only earns MEASURE if it is truly numeric-aggregatable; a flag/category/ordinal is rejected, never coerced to a measure. Run → PASS. Commit.

---

### Task 3: Operation normalization + structured window (#6)

**Files:** Create `planner/b_operation.py`; Test.
**Interfaces:** `normalize_operation(raw_op: str | None, *, structured_window: str | None) -> NormalizedOp | OperationReject`.

- [ ] Failing test — closed alias table (avg/mean, count distinct/nunique, sum/total, etc.); unknown/compound → `OPERATION_UNRECOGNIZED`. **The window comes ONLY from a structured field, never parsed from text:** `TREND` with a valid `structured_window` (`"90d"`) → ok; `TREND` with none / only a free-text `"trend_90d"` → `WINDOW_REQUIRED_UNSPECIFIED` (the LLM must supply a structured window — see Task 6 on threading it). `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING`.
- [ ] Implement the closed grammar + structured-window requirement. Run → PASS. Commit.

---

### Task 4: Governed source-binding incl. source-ENTITY authority (#4)

**Files:** Create `planner/b_source_grain.py`; Test.
**Interfaces:** `resolve_source_binding(conn, adapter, *, catalog_source, object_ref, now) -> GovernedSourceBindingV1 | StructuralReject`.

- [ ] Failing test — a source table with a VERIFIED grain fact whose grain-key column has a **human-confirmed/source-attested concept carrying an `entity_link`** → `GovernedSourceBindingV1(source_grain_entity=<entity_link>, source_grain_key_refs=[<qualified keys>], grain_fact_key)`. A VERIFIED grain fact whose key column's concept is only display/LLM-proposed (no governed `entity_link`) → `SOURCE_ENTITY_UNGOVERNED` (the grain columns are proven, but the **entity** those columns key is NOT). No VERIFIED grain fact → `STRUCTURAL_NEED_UNGOVERNED`.
- [ ] Implement: grain columns + `grain_fact_key` from `resolve_fact("grain")` (reuse A's Task-4 endpoint pattern); derive `source_grain_entity` from the **governed** concept `entity_link` of the grain-key column (via Task-1 authority on that column), never assumed. Run → PASS. Commit.

---

### Task 5: Exact governed time-column binding (#3)

**Files:** Create `planner/b_time_binding.py`; Test. May extend `planner/multisource_contracts.py`/A to carry an **exact** anchor column ref (pinned) instead of a bare concept.

- [ ] Failing test — for a `TIME`/`take_latest`/`RECENCY`/`TREND` operand, B resolves a **specific governed time column** (a column whose concept is authoritative + has an accepted `pit_role`) and A **pins** it — a table with `transaction_date` + `posting_date` + `record_created_date` must bind the *governed* one, never "first match on a displayed concept." Ambiguous/ungoverned time column → `TIME_ANCHOR_UNGOVERNED`.
- [ ] Implement: B emits an exact time-column ref; extend the intent/A so the temporal anchor is a pinned column (not resolved by concept-first-match in `compile_temporal`). If this requires an A change, it is a small, reviewed extension of A's anchor handling — **not** a permissive fallback. Run → PASS. Commit.

---

### Task 6: The adapter — raw capture (pre-`_vet`) → intent | reject

**Files:** Create `planner/b_adapter.py`; Test.
**Interfaces:** `normalize_feature_idea(conn, adapter, *, raw_proposal, identity_map, structured_window, scope, roles, now) -> MultiSourcePlannerIntentV1 | AdapterReject`.

- [ ] Failing test — the adapter consumes the **RAW** proposal (the operand set BEFORE `_vet`); if `_vet` would drop/rewrite ANY operand → `PROPOSAL_LOSSY` (proven by a 3-operand proposal where `_vet` drops one). Per operand: concept authority (T1) → role (T2) → source binding incl entity (T4) → exact time binding for time operands (T5); operation via T3 with the threaded `structured_window`; assemble the exact `MultiSourcePlannerIntentV1`; **preservation** — every raw operand appears exactly once, in the right slot. Missing authority / ungoverned entity / ungoverned time / lossy / unrecognized op → the exact rejects.
- [ ] Implement, capturing the raw proposal + identity map directly (the endpoint supplies them — Task 7). Run → PASS. Commit.

---

### Task 7: The internal demo / Gate-1 endpoint (standalone — #1, #2, #9)

**Files:** Create `planner/b_demo_endpoint.py`; Test.
**Interfaces:** `govern_llm_idea(conn, adapter, *, raw_proposal, identity_map, structured_window, scope, roles, now) -> GovernedResult | Rejection`.

- [ ] Failing test — a raw cross-catalog proposal with established authority → `normalize_feature_idea` (T6) → `plan_multi_source` (A) → a `GovernedResult` **only when BOTH axes resolve** (`resolution_status == resolved` AND `contract_result_status == resolved`, with the selected contract ids); assembly-resolved-but-contract-incomplete → NOT a governed result (surfaced as `CONTRACT_UNRESOLVED`, never labeled governed — #7). A reject at any stage → a cause-labelled `Rejection`. This endpoint does NOT call `build_considered_set`, does NOT add to any considered set, does NOT read `is_live`.
- [ ] Implement the standalone chain + the strict two-axis governed-result gate. Run → PASS. Commit.

---

### Task 8: Gate 1 — component qualification (real four-eyes authority — #8)

**Files:** Create `planner/b_gate1_gold.py`, `planner/b_gate1.py`; Test.

- [ ] Failing test — the gate PASSES on the correct endpoint over immutable gold whose authority is established through the **REAL governance workflow**: concept via proposal → a *different* human confirms → CAS/version check → projection; grain via `propose_fact`→`_confirm_grain`; and the cross-catalog bridge via the **real** propose→confirm→**project_verified_bridge** path (not a raw INSERT). Positives normalize to the EXACT expected intent AND produce a two-axis-governed result with the exact expected plan; negatives reject with the exact code; deterministic replay; **zero false resolves**; operand + operation + time-binding preservation. A reject-everything endpoint FAILS positive coverage.
- [ ] Implement the gold via the real commands (no shortcuts, no relaxed bar) + the gate; prove non-vacuity. Run → PASS. Commit.

---

### Task 9: End-to-end demo + zero-live-impact neutrality

**Files:** Test.

- [ ] End-to-end on a real-CSV-shaped fixture: upload two catalogs → establish concept + grain + a VERIFIED bridge through the **real four-eyes workflow** → a raw cross-catalog proposal → `govern_llm_idea` → a two-axis-governed cross-catalog feature definition with its plan. **Neutrality:** assert `build_considered_set` is byte-identical to a captured golden (B changed nothing there); assert B added no path to the considered set / snapshot / draft; importing `b_*` modules has no import-time side effect; no reused engine file edited beyond the reviewed Task-5 anchor extension.
- [ ] Run → PASS. Commit.

---

## Self-Review

**Addresses the review:** #1 raw pre-`_vet` capture → T6; #2 no considered-set/snapshot/draft integration → T7 standalone + T9 neutrality; #3 exact pinned time column → T5; #4 source-entity authority from governed `entity_link` → T4; #5 refined role policy (additivity/pit_role/entity_link, not group-only) → T2; #6 structured window (no text parsing) → T3/T6; #7 two-axis governed-result gate → T7; #8 real four-eyes authority + real projected bridge → T8; #9 no `is_live` coupling → T7 standalone; #10 rebase + migration renumber → T0.

**Deferred (correctly, until their contracts exist):** considered-set integration + the multi-source plan-envelope that survives snapshot→selection→draft; ordering-sensitive ops; Gate 2; provisioning; production telemetry; the live flip.

**Quality equality guaranteed:** `raw ≡ normalized ≡ generated` is enforced by T6 preservation + `PROPOSAL_LOSSY`, T5 exact time binding, T4 entity authority, T2 no-coerce role policy, and T7's two-axis gate — with T8 proving it on real-authority gold. The demo cannot show a feature that is physically assembled but semantically different from the proposal.
