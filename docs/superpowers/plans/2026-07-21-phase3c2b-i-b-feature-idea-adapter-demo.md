# Phase 3C.2b-i-B (VERTICAL SLICE) — Governed Single-Operand Cross-Catalog Roll-Up

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**HONEST SCOPE — read first.** This is a **vertical slice**, not the multi-source FeatureIdea adapter. It proves one thing end-to-end on real data: a governed **single-operand cross-catalog roll-up** — one computation operand from catalog A, rolled over a VERIFIED bridge to a confirmed target entity in catalog B, faithfully normalized from an LLM proposal and two-axis-governed by A. Because RECENCY/TREND (time) and RATIO/DIFFERENCE (ordering) are deferred, A has **no** operation here that *combines two cross-catalog operands* — so this slice proves cross-catalog **traversal**, not the multi-source combine. It **does not** complete the adapter and **does not** qualify 3C.2b-ii.

**Goal:** Prove the corrected chain in working code, then author the remaining tasks from the interfaces the spike demonstrates.

**Tech Stack:** Python 3.12, frozen slotted dataclasses + lowercase-snake `StrEnum`, psycopg, pytest, `uv`.

## Global Constraints (boundaries)

- **Run the gauntlet, don't bypass it.** The chain runs the existing deterministic `_vet` gauntlet (leakage, drift-freshness, read-scope/join authority, tri-state) on the raw proposal, THEN a raw/vetted **preservation** check. A safe-and-preserved proposal is the only thing that proceeds. Retain Slice-3 `validation_status` + `requirements` on the result.
- **Trust inputs are server-derived.** The identity map, the authorized `CatalogScopeV1`, and the confirmed non-null `target_entity` are derived server-side from authenticated roles + the exact candidate roster (`ConfirmedScope`, distinct from planner `CatalogScopeV1`). Caller injection is test-only.
- **Authority, human-confirmed cohort for the demo.** Concept authority: source-attested stays in the *permanent* model, but the demo cohort is **human-confirmed concept only** (nothing attests `concept` in production yet). Grain: VERIFIED grain fact. Bridge: real proposed→confirmed→projected. All established through the **real four-eyes governance commands**, never `record_field_evidence`/raw-INSERT shortcuts.
- **Window captured, not consumed.** `RawFeatureProposalV1` carries the window for lossless capture, but RECENCY/TREND reject with `OPERATION_DEFERRED`; the window is never consumed. Ordered ops → `OPERAND_ORDER_AUTHORITY_MISSING`.
- **Computation operands ≠ structural references.** The proposal's refs split into *computation operands* (A's operand slots) and *structural refs* (grain/entity keys → source_binding/target grain). Preservation proves **every** raw ref is accounted for in exactly one category.
- **Two-axis governed only.** A result is governed only when `resolution_status == resolved` AND `contract_result_status == resolved` with the selected contract ids.
- **Bounded + isolated.** A is called with a finite `CompileBudget` + operand limit + per-run savepoint; DB/infra → typed `TECHNICAL_FAILURE`, budget → `BUDGET_TRUNCATED`.
- **Zero live-path change.** No modification to `build_considered_set`/`_reject_cross_catalog_llm`/the considered set/`is_live`. B is a standalone bounded service function.
- **A is UNCHANGED.** No edit to any `multisource_*` planner module (A stays reviewed-clean; exact-time-binding is deferred with the time ops).
- **Informative failure.** If the real FTR fixture lacks a governed-able operand / grain / bridge topology, the spike reports the **authority gap** — it must NEVER weaken a rule to force a pass.
- Commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Reused surfaces:** `feature_assist._vet` (the gauntlet), the real confirm commands (concept human-confirm via the field-evidence confirm path; grain `propose_fact`→`_confirm_grain`; bridge `propose_bridge`→confirm→`project_verified_bridge`), `source_profile.py` (attested-field sets — concept is NOT among them), `field_evidence.py`/`field_authority.py` (evidence + producer/strength/lifecycle), `concepts.py` (`Concept` group/pit_role/additivity/entity_link), `resolve.py` `resolve_fact` (`grain`, `availability_time`) + `facts.py` `fact_key`, `planner/multisource_contracts.py` + `planner/multisource_plan.py` (`plan_multi_source(conn, adapter, *, intent, scope, roles, now, budget)`), `ConfirmedScope`/`CatalogScopeV1`, `CompileBudget`.

---

### Task 0: Prerequisite — clean worktree, rebase, A-migration renumber, constants

- [ ] **Step 1: Clean worktree.** Create a clean implementation worktree off `origin/main` (now at `1017_field_evidence_note`) — the shared tree holds the parallel session's uncommitted WIP, so do NOT rebase in place (finding #14). Bring the branch's A commits onto that base (rebase/cherry-pick in the worktree).
- [ ] **Step 2: Renumber A's colliding migration.** `1010_multisource_assembly_shadow.sql` collides with main's `1010_asset_detail_indexes` → rename to **`1018_multisource_assembly_shadow.sql`**; update every `1010` reference in `multisource_shadow_store.py` + A's tests. `uv run pytest -k multisource -q` must be green on the new base (A re-qualified, unchanged in behaviour).
- [ ] **Step 3: B disposition enum + constants (finding #13).** Create `planner/b_dispositions.py`: a versioned `BDisposition(StrEnum)` — `governed`, `proposal_lossy`, `gauntlet_rejected` (carrying the `_vet` `RejectCode`), `concept_authority_missing`/`_conflict`/`_stale`, `concept_not_in_registry`, `source_entity_ungoverned`, `structural_need_ungoverned`, `role_not_aggregatable`, `operation_unrecognized`, `operation_deferred`, `operand_order_authority_missing`, `contract_unresolved`, `technical_failure`, `budget_truncated`, `unresolved_operand`, `ambiguous_column_identity` — plus `B_DISPOSITION_VERSION` and an explicit `map_a_outcome(result) -> BDisposition` from A's `MultiSourcePlanningResultV1`. Test the enum + mapping.
- [ ] **Step 4: Commit.**

---

### Task 1: THE SPIKE — prove the full chain end-to-end on the real FTR sample (GO/NO-GO)

**Files:** Create `planner/b_slice_spike.py` (thin helpers only); Test `tests/.../test_b_slice_spike.py`.

**This is the go/no-go gate.** It proves, in one test on the **actual FTR sample the user provides** (not a hand-invented fixture), the entire corrected chain:

```
actual FTR upload + a second catalog
 → real human concept confirmation (four-eyes)
 → real grain confirmation (propose_fact → _confirm_grain)
 → real proposed → confirmed → projected bridge
 → server-derived identity map + authorized CatalogScopeV1 + confirmed target_entity
 → existing _vet gauntlet (safety + tri-state)
 → raw/vetted preservation check
 → B normalization of ONE computation operand → MultiSourcePlannerIntentV1
 → bounded plan_multi_source (finite CompileBudget)
 → assert BOTH axes resolved (resolution_status AND contract_result_status)
```

- [ ] **Step 1: Inspect the real FTR fixture FIRST.** Locate the actual FTR sample CSV(s) the user provides (ask for the path if not in `tests/fixtures`). Read the real columns/glossary. **Choose the operand from what's actually there** — a real additive-`SUM` measure or a governed `COUNT_DISTINCT` counted column — and a real second catalog + a real bridgeable entity topology. If none exists, STOP and report the **authority gap** (which of operand / grain / bridge / entity is missing) — do NOT invent a convenient column.
- [ ] **Step 2: Write the failing end-to-end test** driving the full chain above on the real fixture, asserting a two-axis-governed `MultiSourcePlanningResultV1` for the chosen single operand, established authority through the real four-eyes commands, server-derived scope, and a finite `CompileBudget`.
- [ ] **Step 3: Run → FAIL.** Build the thin spike helpers (upload+confirm orchestration reusing the real commands; the server-side scope/identity derivation; the single-operand normalization to `MultiSourcePlannerIntentV1` — SUM measure or COUNT_DISTINCT counted, with governed concept + source_binding + entity; the bounded A call).
- [ ] **Step 4: Run → PASS**, OR report a truthful `BLOCKED` naming the exact chain link that fails (authority gap or contract gap). **Do not weaken any rule to force green.** A truthful authority-gap report is a valid outcome.
- [ ] **Step 5: Commit** the spike + a short note recording the demonstrated interfaces (the exact server-scope-derivation signature, the normalization signature, the confirm-command sequence) — these seed the remaining tasks.

---

## Remaining tasks — authored from the spike's demonstrated interfaces (NOT before)

Per the sequencing decision, the detailed tasks below are written **after** Task 1 proves the chain, from the working code's interfaces (so they match reality, not assumptions):

- **T2 `RawFeatureProposalV1`** — versioned lossless capture of the raw LLM proposal incl. the window field (captured, not consumed); the pre-`_vet` operand set.
- **T3 Server-side derivation** — identity map + authorized `CatalogScopeV1` + confirmed non-null `target_entity` from authenticated roles + candidate roster (`ConfirmedScope`); no caller trust.
- **T4 Gauntlet + preservation + tri-state** — run `_vet` (with a server `target_ref`), reject its hard failures, compare raw vs vetted operands (`PROPOSAL_LOSSY` on any drop/rewrite), retain `validation_status`/`requirements`; split refs into **computation operands** vs **structural refs** and prove full coverage.
- **T5 Concept-authority resolver** — human-confirmed cohort for the demo (source-attested kept in the permanent model).
- **T6 Refined role policy** — `MEASURE` only when genuinely numeric-aggregatable (`additivity` ≠ n/a) + `pit_role`/`entity_link` gating; flags/categories rejected (`ROLE_NOT_AGGREGATABLE`).
- **T7 Governed source-binding** — grain + `grain_fact_key` from the VERIFIED grain fact; `source_grain_entity` from the grain-key column's confirmed-concept `entity_link`; **composite-grain** rules (entity key + partition keys; agreeing vs conflicting vs no entity-linked key — finding #11).
- **T8 Operation-output policy** — closed policy filling `PathStrategyV1.output_type`/`output_additivity`/`external_type_required` + `FinalExpressionV1.output_additivity` from verified operational reads, fail-closed on fork/hash-mismatch/projection-lag (finding #8).
- **T9 Bounded service entrypoint** — the actual CLI/admin service function (name, executable entrypoint, admin authorization, request schema, payload/operand limits, `CompileBudget`, savepoint isolation, audit) — resolve the "not-actually-an-endpoint" gap (#10, #12).
- **T10 Gate 1** — real four-eyes authority + real projected bridge + the **real FTR fixture** (state the compatibility scope; #8/#15); positives two-axis-govern to exact expected plans, negatives exact `BDisposition`, deterministic replay, zero false resolves, preservation; non-vacuous.
- **T11 Neutrality** — B changed nothing on the live path; no `multisource_*`/engine file edited beyond the (none) A change.

## Self-Review

Addresses the review: #1 gauntlet-run+preservation (T4); #2 target_entity+scope (T3); #3 computation-vs-structural operands (T4); #4 window-in-proposal + entity authority (T2/T7); #5 human-confirmed cohort + refined role (T5/T6); #6/#7 time ops DEFERRED (A unchanged); #8 output policy (T8); #9 server-derived trust (T3); #10 real entrypoint (T9); #11 composite grain (T7); #12 bounds (T9); #13 B enum (T0); #14 clean worktree (T0); #15 real FTR fixture (T1/T10). Honest scope stated (single-operand traversal, not multi-source combine; does not qualify 3C.2b-ii). Spike-first; remaining tasks authored from demonstrated interfaces.
