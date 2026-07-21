# Phase 3C.2b-i-B (VERTICAL SLICE) — Governed Single-Operand Cross-Catalog Roll-Up — FINAL PLAN

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**HONEST SCOPE.** A **vertical slice**, not the multi-source FeatureIdea adapter. It proves one thing end-to-end on real FTR data: a governed **single-operand cross-catalog roll-up** — one computation operand (`SUM(TRAN_AMT)`) from the FTR transaction catalog, rolled over a VERIFIED bridge (`CIF_ID`) to a confirmed customer entity in a second catalog, faithfully normalized from an LLM proposal and two-axis-governed by A. With RECENCY/TREND (time) and RATIO/DIFFERENCE (ordering) deferred, A has **no** operation that *combines two cross-catalog operands*, so this proves cross-catalog **traversal**, not the multi-source combine. It **does not** complete the adapter and **does not** qualify 3C.2b-ii.

**Goal:** `LLM raw proposal → B normalization → A planning → internal governed result` — proven on the real FTR transaction export + a customer catalog, with the full quality chain (gauntlet, preservation, tri-state, authority, two-axis) intact and A unchanged.

**Tech Stack:** Python 3.12, frozen slotted dataclasses + lowercase-snake `StrEnum`, psycopg, pytest, `uv`.

## Global Constraints (boundaries)

- **`raw ≡ normalized ≡ generated`.** Run the existing `_vet` gauntlet (leakage, drift-freshness, read-scope/join authority, tri-state) on the raw proposal, THEN a raw/vetted **preservation** check (`PROPOSAL_LOSSY` on any drop/rewrite). Retain Slice-3 `validation_status` + `requirements`.
- **Server-derived trust inputs.** The identity map, authorized `CatalogScopeV1`, and confirmed non-null `target_entity` are derived server-side from authenticated roles + the exact candidate roster (`ConfirmedScope`, distinct from `CatalogScopeV1`); caller injection is test-only.
- **Authority, human-confirmed cohort.** Concept: source-attested stays in the permanent model, but the demo cohort is **human-confirmed concept only** (nothing attests `concept` today). Grain: VERIFIED grain fact. Bridge: real proposed→confirmed→projected. All via the **real four-eyes governance commands**, never `record_field_evidence`/raw-INSERT shortcuts.
- **Computation operands ≠ structural refs.** Split the proposal's refs into *computation operands* (A's operand slots) and *structural refs* (grain/entity keys → source_binding/target grain); preservation proves every raw ref lands in exactly one category.
- **Window captured, not consumed.** `RawFeatureProposalV1` carries the window; RECENCY/TREND → `OPERATION_DEFERRED`; ordered ops → `OPERAND_ORDER_AUTHORITY_MISSING`.
- **Two-axis governed only** (`resolution_status == resolved` AND `contract_result_status == resolved` + contract ids). **Bounded + isolated** (finite `CompileBudget`, operand limit, per-run savepoint; typed `TECHNICAL_FAILURE`/`BUDGET_TRUNCATED`).
- **Zero live-path change** (no edit to `build_considered_set`/`_reject_cross_catalog_llm`/considered set/`is_live`). **A UNCHANGED** (no `multisource_*` planner edit; time ops deferred). **Informative failure** (report the authority/topology gap; never weaken a rule).
- Commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Fixtures:** the real FTR transaction glossary is `FTR_Column_Mapping_final.csv` at repo root (LOCAL-ONLY, git-excluded — NEVER stage/commit it). The customer side of the spike is a representative customer-master slice ingested through the real path; the final real customer file drives T12.

**Reused surfaces:** `feature_assist._vet`; the real confirm commands (concept human-confirm via the field-evidence confirm path; grain `propose_fact`→`_confirm_grain`; bridge `propose_bridge`→confirm→`project_verified_bridge`); `ftr_adapter`/`glossary_reader`/`ingest` (real FTR ingest); `field_evidence.py`/`field_authority.py`; `concepts.py`; `resolve.py` `resolve_fact` + `facts.py` `fact_key`; `planner/multisource_contracts.py` + `planner/multisource_plan.py` (`plan_multi_source`); `ConfirmedScope`/`CatalogScopeV1`/`CompileBudget`.

---

### Task 0: Prerequisite — clean worktree, rebase, A-migration renumber, B enum
- [ ] Clean implementation worktree off `origin/main` (now `1017`); bring the branch's A commits onto that base (do NOT rebase in place — parallel WIP; finding #14).
- [ ] Renumber A's colliding `1010_multisource_assembly_shadow.sql` → **`1018_multisource_assembly_shadow.sql`**; fix every `1010` ref; `uv run pytest -k multisource -q` green on the new base.
- [ ] Create `planner/b_dispositions.py`: versioned `BDisposition(StrEnum)` (`governed`, `proposal_lossy`, `gauntlet_rejected`(+`_vet` `RejectCode`), `concept_authority_missing/_conflict/_stale`, `concept_not_in_registry`, `source_entity_ungoverned`, `structural_need_ungoverned`, `role_not_aggregatable`, `operation_unrecognized`, `operation_deferred`, `operand_order_authority_missing`, `contract_unresolved`, `technical_failure`, `budget_truncated`, `unresolved_operand`, `ambiguous_column_identity`) + `map_a_outcome(result)`. Test. Commit.

---

### Task 1: SPIKE — full chain on the REAL FTR transactions + a representative customer (GO/NO-GO)
**Files:** `planner/b_slice_spike.py` (thin helpers) + test.
The go/no-go, proving on the **real** `FTR_Column_Mapping_final.csv`:
```
real FTR transaction ingest + a representative customer-master slice (keyed by CIF_ID)
 → real human concept confirmation + real grain confirmation + real projected CIF_ID bridge
 → server-derived identity map + authorized CatalogScopeV1 + confirmed target_entity=customer
 → _vet gauntlet (safety + tri-state) → raw/vetted preservation
 → B normalization of ONE operand (SUM(TRAN_AMT) measure) → MultiSourcePlannerIntentV1
 → bounded plan_multi_source → assert BOTH axes resolved
```
- [ ] Ingest the real FTR file; confirm `TRAN_AMT` is a governable Measure and `CIF_ID` a confirmable customer key (already verified). Stand up a representative customer-master slice keyed by `CIF_ID` through the real ingest path.
- [ ] Failing e2e test driving the whole chain; establish authority via the real four-eyes commands; bounded `CompileBudget`.
- [ ] Implement the thin helpers; run → PASS, or truthful `BLOCKED` naming the exact failing link (authority/topology/contract gap) — **never weaken a rule**.
- [ ] Commit + record the demonstrated interfaces (server-scope-derivation signature, normalization signature, confirm sequence) for T2–T11.

---

### Task 2: `RawFeatureProposalV1` — lossless capture (#4)
**Files:** `planner/b_proposal.py` + test. Frozen `RawFeatureProposalV1(operands: tuple[str,...], operation: str|None, window: str|None, grain_hint: str|None, version)` capturing the raw LLM output BEFORE `_vet` (window inside it, captured not consumed).
- [ ] Test round-trip + that it carries the pre-`_vet` operand set verbatim. Implement. Commit.

### Task 3: Server-side trust derivation (#2, #9)
**Files:** `planner/b_scope.py` + test. `derive_request_context(conn, *, roles, generation_run_id) -> (identity_map, CatalogScopeV1, target_entity)` from authenticated roles + the exact candidate roster + `ConfirmedScope`.
- [ ] Test: a caller-claimed catalog for a bare column is IGNORED (server roster wins); overbroad scope rejected; non-null confirmed `target_entity` required. Implement. Commit.

### Task 4: Gauntlet + preservation + tri-state + operand categorization (#1, #3)
**Files:** `planner/b_gauntlet.py` + test. `run_gauntlet_and_preserve(conn, *, raw, target_ref, roles, now) -> VettedProposal | BDisposition`.
- [ ] Test: `_vet` runs with the server `target_ref`; a `_vet` hard failure (leakage/stale/read-scope) → `gauntlet_rejected` (carrying the `RejectCode`); a dropped/rewritten operand → `proposal_lossy`; `validation_status`+`requirements` retained; refs split into **computation operands** vs **structural refs** with full-coverage proof. Implement. Commit.

### Task 5: Concept-authority resolver — human-confirmed cohort (#5)
**Files:** `planner/b_concept_authority.py` + test. `resolve_planner_concept_binding(conn, logical_ref)` (accepted pairs only; human-confirmed cohort for the demo; precedence/conflict/missing/stale/rejected/registry/pending exactly; no `expected_concept`).
- [ ] Test each outcome via the real confirm path. Implement. Commit.

### Task 6: Refined computation-role policy (#5)
**Files:** `planner/b_role_policy.py` + test. `computation_role(concept)` — `MEASURE` only when genuinely numeric-aggregatable (`additivity ≠ n/a`) + `pit_role`/`entity_link` gating; total over every `group`; `impairment_stage`/`green_flag` → `ROLE_NOT_AGGREGATABLE`. Implement. Commit.

### Task 7: Governed source-binding + source-entity + composite grain (#4, #11)
**Files:** `planner/b_source_grain.py` + test. `resolve_source_binding(conn, adapter, *, catalog_source, object_ref, now) -> GovernedSourceBindingV1 | BDisposition`. Grain + `grain_fact_key` from the VERIFIED grain fact; `source_grain_entity` from the grain-key column's confirmed-concept `entity_link`; **composite grain** rules — one entity-linked key + partition keys; multiple agreeing entity links; conflicting → reject; no entity-linked key → `source_entity_ungoverned`.
- [ ] Test each composite case. Implement. Commit.

### Task 8: Operation-output policy (#8)
**Files:** `planner/b_operation.py` (closed alias grammar) + `planner/b_output_policy.py` + test. Fill `PathStrategyV1.output_type`/`output_additivity`/`external_type_required` + `FinalExpressionV1.output_additivity` from verified operational reads, fail-closed on fork/hash-mismatch/projection-lag; RECENCY/TREND → `OPERATION_DEFERRED`; RATIO/DIFFERENCE → `OPERAND_ORDER_AUTHORITY_MISSING`.
- [ ] Test the closed grammar + output-field derivation + fail-closed. Implement. Commit.

### Task 9: The adapter + bounded service entrypoint (#7, #10, #12)
**Files:** `planner/b_adapter.py` (`normalize_feature_idea`, consuming T2–T8) + `planner/b_service.py` (the executable entrypoint) + test. `govern_llm_idea(...) -> GovernedResult | BDisposition` — server-derived context → gauntlet+preservation → per-operand authority/role/source-binding → single-operand `MultiSourcePlannerIntentV1` → **bounded** `plan_multi_source` (finite `CompileBudget`, operand limit, savepoint) → `GovernedResult` ONLY on the **two-axis** pass (+ carry `validation_status`/`requirements`). Define the entrypoint's admin authorization, request schema, payload/operand limits, and audit behavior (name it a CLI/admin service function; it is NOT an HTTP considered-set route).
- [ ] Test: two-axis gate (assembly-resolved-but-contract-incomplete → `contract_unresolved`, never governed); bounds/isolation; server-side context. Implement. Commit.

### Task 10: Gate 1 — component qualification (real four-eyes authority)
**Files:** `planner/b_gate1_gold.py`, `planner/b_gate1.py` + test. Immutable gold whose authority is established through the **real** commands (concept four-eyes; grain `propose_fact`→`_confirm_grain`; bridge propose→confirm→`project_verified_bridge`). Positives normalize to the exact intent AND two-axis-govern to the exact plan; negatives reject with the exact `BDisposition`; deterministic replay; zero false resolves; operand + operation preservation; non-vacuous (reject-all fails positive coverage). Implement. Commit.

### Task 11: Neutrality
**Files:** test. `build_considered_set` byte-identical (B changed nothing there); B added no path to the considered set/snapshot/draft; no `multisource_*`/engine file edited; `b_*` imports have no import-time side effect. Run → PASS. Commit.

### Task 12: Real-two-source acceptance (gated on the real customer file)
**Files:** test (fixture = the real FTR transactions + your real customer file). Run `govern_llm_idea` end-to-end on **both real sources** (real customer file supplied in one of the accepted ingest shapes) → a two-axis-governed `SUM(TRAN_AMT)` per customer. **State the scope honestly:** proves real-two-source cross-catalog compatibility. If the real customer file exposes an ingest/authority gap, report it (do not weaken a rule).
- [ ] (Blocked only on the real customer file; everything above proceeds without it.)

---

## Self-Review
Addresses the review end-to-end: #1 gauntlet+preservation (T4); #2 server target_entity+scope (T3); #3 computation-vs-structural operands (T4); #4 window-in-proposal + entity authority (T2/T7); #5 human-confirmed cohort + refined role (T5/T6); #6/#7 time ops DEFERRED (A unchanged); #8 output policy (T8); #9 server-derived trust (T3); #10 real entrypoint (T9); #11 composite grain (T7); #12 bounds (T9); #13 B enum (T0); #14 clean worktree (T0); #15 real FTR (T1 real transactions; T12 real two-source). Honest scope stated. Spike-first; T1 is the go/no-go on real FTR; T12 is the only task gated on the real customer file — nothing else waits.
