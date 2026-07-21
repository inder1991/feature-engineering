# Phase 3C.2b-i-B (DEMO-GRADE) — FeatureIdea Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn an LLM-proposed cross-catalog `FeatureIdea` into a fully-governed `MultiSourcePlannerIntentV1` for the already-built A assembler — deterministically, on **real authority** (human-confirmed concept + VERIFIED grain fact) — so an internal demo on real sample CSVs shows a governed cross-catalog feature definition come out, with the full quality bar intact.

**Architecture:** A deterministic adapter: capture the raw LLM idea (pre-`_vet`, lossless) → resolve each operand's **concept authority** from real evidence (source-attested / human-confirmed only) → assign **computation roles** from a versioned policy over real `Concept` fields → normalize the operation through a closed grammar → resolve each operand's **source grain** to a VERIFIED grain fact → emit `MultiSourcePlannerIntentV1` (or reject with a typed reason) → hand to A's `plan_multi_source`. A **Gate-1 correctness gate** on real-authority-seeded gold proves it. Wired into `build_considered_set` behind the sandbox flag; flag-off is byte-identical.

**Tech Stack:** Python 3.12, `@dataclass(frozen=True, slots=True)` + lowercase-snake `StrEnum` (NOT pydantic), psycopg, pytest, `uv`. Under `src/featuregen/overlay/upload/`.

**Spec:** `docs/superpowers/specs/2026-07-19-phase3c2b-i-b-feature-idea-adapter-shadow-design.md` (this plan implements its **Gate-1 core**).

## Global Constraints

- **Quality bar is NON-NEGOTIABLE and NOT deferred:** the authority bar (source-attested OR human-confirmed concept; VERIFIED grain fact) and the Gate-1 correctness gate (zero false resolves, exact expected intents/plans, operand + operation preservation, adversarial rejects) are fully in scope. Never relax the bar to make the demo resolve more.
- **Authority, not display:** concept authority from raw lifecycle-active evidence, never `graph_node.concept`; source grain from a VERIFIED grain fact via `resolve_fact`, never `is_grain`/advisory.
- **Deterministic, fail-closed:** every reject is a typed `MultiSourceReason`-family reason; no guessing; no inference latitude in governed output.
- **Emit A's exact carrier:** `MultiSourcePlannerIntentV1`/`OperandSlotV1`/`GovernedSourceBindingV1`/`PathStrategyV1`/`FinalExpressionV1` from `planner/multisource_contracts.py` — never redefine. A independently revalidates `grain_fact_key`.
- **Behaviour-neutral flag-off:** with `FEATUREGEN_MULTISOURCE_ASSEMBLY_SHADOW`/the B flag off, `build_considered_set` is byte-identical (the blanket `_reject_cross_catalog_llm` still applies); B only engages in the sandbox with the flag on.
- **Order-sensitive ops deferred:** `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING` (no ordering from LLM). Demo covers `IDENTITY`/`COUNT`/`COUNT_DISTINCT`/`RECENCY`/`TREND`.
- **DEFERRED-FOR-DEMO, REQUIRED-FOR-REAL (do NOT build here; keep out of scope):** Gate 2 (real-population readiness); the automatic authority-**provisioning** capability (demo establishes authority via the human-confirmation command); the production capture-integrity telemetry (manifest/reconciliation/two-connection store), the worker/outbox topology, the authority-state fingerprint + canonical-input replay; the activation-interlock **live flip** and the removal of `find_cross_catalog_path`. Each stays deferred behind the flag; none is a quality guarantee.
- **Types:** frozen slotted dataclasses + lowercase-snake `StrEnum`. Version every policy. Commit trailer `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

**Reused surfaces (read before use):** `overlay/field_evidence.py` (`read_active_field_evidence`, `record_field_evidence`, `to_view`, `FieldEvidence`), `overlay/field_authority.py` (`EvidenceProducer`/`AssertionStrength`/`EvidenceLifecycle`), `overlay/upload/field_revalidation.py` (`active_disqualifiers_for`), `overlay/upload/concepts.py` (`CONCEPT_REGISTRY`, `Concept` fields group/pit_role/additivity/entity_link), `overlay/resolve.py` (`resolve_fact`) + `overlay/upload/facts.py` (`fact_key`), `overlay/upload/planner/multisource_contracts.py` (the intent carriers + `MultiSourceReason` + `PATH_AGG_TO_FUNCTION`), `overlay/upload/planner/multisource_plan.py` (`plan_multi_source`), `overlay/upload/feature_assist.py` (`FeatureIdea`, `_vet`:474), `overlay/upload/contract/gate1.py` (`build_considered_set`:341, `_reject_cross_catalog_llm`:404), A's Task-5.5 gold seeding (`propose_fact`/`_confirm_grain` for grain).

---

### Task 0: Prerequisite — flag + policy version constants

**Files:** Modify `src/featuregen/overlay/upload/planner/contracts.py` (or a B module) for constants.

- [ ] **Step 1:** Add `ROLE_POLICY_VERSION = "3c2bib.role.1.0.0"`, `OPERATION_ALIAS_VERSION = "3c2bib.op.1.0.0"`, and `FEATUREGEN_GOVERNED_LLM_XCAT_FLAG = "FEATUREGEN_GOVERNED_LLM_XCAT"` (the sandbox surface flag for B).
- [ ] **Step 2:** Verify import via `uv run python -c "..."`. Commit.

---

### Task 1: Concept-authority resolver

**Files:** Create `src/featuregen/overlay/upload/planner/b_concept_authority.py`; Test `tests/.../test_b_concept_authority.py`.

**Interfaces:** `resolve_planner_concept_binding(conn, logical_ref) -> PlannerConceptBinding | ConceptAuthorityRejection`. `PlannerConceptBinding(authoritative_concept, authority: ConceptAuthority[human_confirmed|source_attested], evidence_ids, evidence_set_hash, value_hash)`.

- [ ] **Step 1: Failing test** (spec §3) — seed concept evidence via `record_field_evidence` at `(HUMAN,CONFIRMED)` → binding `human_confirmed`; `(SOURCE,ATTESTED)` only → `source_attested`; conflicting human values → `CONCEPT_AUTHORITY_CONFLICT`; no accepted evidence in any lifecycle → `CONCEPT_AUTHORITY_MISSING`; accepted evidence exists but only `STALE`/`SUPERSEDED` → `CONCEPT_EVIDENCE_STALE`; only `REJECTED` → `CONCEPT_AUTHORITY_MISSING`; resolved value ∉ `CONCEPT_REGISTRY` → `CONCEPT_NOT_IN_REGISTRY`; a pending revalidation (`active_disqualifiers_for`) → `CONCEPT_REVALIDATION_PENDING`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** per spec §3: read active `concept` evidence via `read_active_field_evidence`+`to_view`; consider ONLY `(SOURCE,ATTESTED)`/`(HUMAN,CONFIRMED)`; precedence human>source; conflict at the winning authority → reject; a lifecycle-history read (all lifecycles, accepted pairs only) distinguishes missing/stale/rejected; registry-membership + `graph_node.concept` compared only for a `DISPLAY_CONCEPT_MISMATCH` diagnostic; DB error raises (caller classifies technical). No `expected_concept` input.
- [ ] **Step 4: Run → PASS. Commit.**

---

### Task 2: Deterministic computation-role policy

**Files:** Create `.../b_role_policy.py`; Test `.../test_b_role_policy.py`.

**Interfaces:** `COMPUTATION_ROLE_POLICY` (versioned, total over every `Concept.group`); `computation_role(concept: Concept) -> SemanticRole | RolePolicyReject`.

- [ ] **Step 1: Failing test** — `group ∈ {monetary, quantity_risk, accounting, regulatory_capital, esg, crypto}` → `MEASURE`; `group == temporal` **with an accepted `pit_role`** → `TIME`, but `temporal` with `pit_role == "none"` (e.g. `duration_tenure`, `vintage`) → NOT time (→ measure/reject per shape); `group == identifier` (with `entity_link`) → `COUNTED`; an unmapped group → `RolePolicyReject`. Policy is total (a test asserts every `Concept.group` value has a mapping).
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the total mapping over real `Concept` fields (`group`/`pit_role`/`entity_link`), versioned `ROLE_POLICY_VERSION`. TIME requires an accepted `pit_role` (spec §4, finding #10). Never trust Slice-3 `measure_refs`.
- [ ] **Step 4: Run → PASS. Commit.**

---

### Task 3: Operation normalization (closed grammar)

**Files:** Create `.../b_operation.py`; Test `.../test_b_operation.py`.

**Interfaces:** `OPERATION_ALIASES` (closed); `normalize_operation(raw_aggregation: str | None) -> NormalizedOp | OperationReject` producing a `FinalOperation` + the per-slot `PathAggregation`s + typed `window`/`time_slot` requirements.

- [ ] **Step 1: Failing test** — `"average"/"mean"→avg`, `"count distinct"/"nunique"→count_distinct`, `"trend"`+window→`TREND`, `"recency"/"latest"→RECENCY`; unknown/compound prose → `OPERATION_UNRECOGNIZED`; a windowed op without a typed window → `WINDOW_REQUIRED_UNSPECIFIED`; `RATIO`/`DIFFERENCE` → `OPERAND_ORDER_AUTHORITY_MISSING` (deferred). No free-text `trend_90d` parsing (spec §5).
- [ ] **Step 2/3/4:** implement the closed alias table (versioned) + shape requirements; no window/anchor inferred from arbitrary text. Run → PASS. Commit.

---

### Task 4: Governed source-grain resolution (→ `GovernedSourceBindingV1`)

**Files:** Create `.../b_source_grain.py`; Test `.../test_b_source_grain.py`.

**Interfaces:** `resolve_source_binding(conn, adapter, *, catalog_source, object_ref, now) -> GovernedSourceBindingV1 | StructuralReject`.

- [ ] **Step 1: Failing test** — a source table with a VERIFIED grain fact (seeded via `propose_fact`→`_confirm_grain`) → `GovernedSourceBindingV1(source_grain_entity, source_grain_key_refs[], grain_fact_key)` with the composite qualified key refs + the deterministic `fact_key`; no VERIFIED grain fact → `STRUCTURAL_NEED_UNGOVERNED`; the `grain_fact_key` equals `fact_key(table_ref, "grain")`.
- [ ] **Step 2/3/4:** resolve the source grain via `resolve_fact(conn, adapter, table_ref, "grain", now=)`; qualify+validate columns (reuse A's Task-4 `governed_endpoint` pattern on the SOURCE table); `grain_fact_key = fact_key(table_ref, "grain")`. **Never** manufacture from `graph_node`/`is_grain` (spec §6). Run → PASS. Commit.

---

### Task 5: The normalization adapter (FeatureIdea → intent | reject)

**Files:** Create `.../b_adapter.py`; Test `.../test_b_adapter.py`.

**Interfaces:** `normalize_feature_idea(conn, adapter, *, raw_candidate, identity_map, scope, roles, now) -> MultiSourcePlannerIntentV1 | AdapterReject`. Consumes Tasks 1-4.

- [ ] **Step 1: Failing test** — a raw cross-catalog idea whose operands have human-confirmed concepts + VERIFIED source grains + a recognized op → a well-formed `MultiSourcePlannerIntentV1` (exact operands/roles/path_strategy/source_binding/final_expression); a **lossy** raw candidate (a `derives_from` dropped by `_vet`) → `PROPOSAL_LOSSY`; a bare name resolving to two catalogs → `AMBIGUOUS_COLUMN_IDENTITY`; missing concept authority → `CONCEPT_AUTHORITY_MISSING`; missing source grain → `STRUCTURAL_NEED_UNGOVERNED`; `stddev`/order-sensitive → the deferred rejects.
- [ ] **Step 2/3/4:** capture the **raw pre-`_vet`** operand set + the request-time candidate identity map (a `derives_from` dropped by `_vet`:474 → `PROPOSAL_LOSSY`); classify cross-catalog span from the identity map; per operand resolve concept authority (T1) → role (T2) → source binding (T4); normalize the op (T3); build `PathStrategyV1` (aggregation + `ordering_anchor_concept` for `take_latest` + `output_additivity` from the concept's additivity + `external_type_required` when operational type unknown); assemble `MultiSourcePlannerIntentV1`; **preservation** — every raw operand appears once in the intent. Run → PASS. Commit.

---

### Task 6: Wire B into the generation flow (sandbox-flagged) + hand to A

**Files:** Modify `src/featuregen/overlay/upload/contract/gate1.py` (the `build_considered_set` cross-catalog branch); Test `tests/.../test_b_generation_wiring.py`.

- [ ] **Step 1: Failing test** — with the B flag ON in a confirmed scoped entity run, a cross-catalog LLM idea whose authority is established → B normalizes it, A (`plan_multi_source`) resolves it, and the governed cross-catalog feature is **surfaced as a considered option** (with its governed plan) instead of blanket-rejected; an idea without authority → surfaced as a **typed rejection** (cause-labelled), not silently dropped. With the flag OFF → `build_considered_set` byte-identical (blanket `_reject_cross_catalog_llm` still applies).
- [ ] **Step 2/3/4:** at the `_reject_cross_catalog_llm` boundary (gate1.py:404), gate on the B flag; when on, route each cross-catalog idea through `normalize_feature_idea` (T5) → `plan_multi_source` (A); a resolved governed plan becomes a considered option carrying its governed provenance (F4: a contract definition, never an attested join); a reject is surfaced cause-labelled. Flag-off path untouched (behaviour-neutral test). Run → PASS. Commit.

---

### Task 7: Gate 1 — component qualification (real-authority gold)

**Files:** Create `.../b_gate1_gold.py`, `.../b_gate1.py`; Test `tests/.../test_b_gate1.py`.

**Interfaces:** `seed_b_gold(conn, ...)` (authority via PRODUCTION COMMANDS: concept via `record_field_evidence` at HUMAN/CONFIRMED or SOURCE/ATTESTED, grain via `propose_fact`→`_confirm_grain`); `CORRECTNESS_GOLD` (immutable expected intents + expected A plans); `evaluate_b_gate1(...) -> BGate1Result`.

- [ ] **Step 1: Failing test** — the gate PASSES on the correct adapter: positive cases normalize to the EXACT expected `MultiSourcePlannerIntentV1` AND resolve end-to-end through A to the exact expected plan; negatives reject with the exact code; deterministic replay (same raw idea → identical normalized intent + disposition); **zero false resolves**; **operand + operation preservation** on resolves. A reject-everything adapter FAILS positive coverage. Fault controls (injected DB error) pass when exactly classified, excluded from the clean population.
- [ ] **Step 2/3/4:** author the gold (all authority seeded via the real governance write paths — no raw inserts, no relaxed bar); the gate runs B→A over it and asserts the §12-Gate-1 criteria. Prove non-vacuity (reject-all fails). Run → PASS. Commit.

---

### Task 8: End-to-end demo scenario + behaviour-neutrality

**Files:** Test `tests/.../test_b_demo_e2e.py`.

- [ ] **Step 1:** an end-to-end test on a real-CSV-shaped fixture: upload two catalogs → **human-confirm** concept + grain (via `record_field_evidence`/`_confirm_grain` — the real authority path) → a cross-catalog idea → B normalizes → A resolves → the governed cross-catalog feature definition is produced with its governed plan. Plus behaviour-neutrality: flag-off, `build_considered_set` output byte-identical to a captured golden; importing all `b_*` modules has no import-time side effect; no reused engine file edited.
- [ ] **Step 2:** Run → PASS. Commit.

---

## Self-Review

**Spec coverage (Gate-1 core):** §2 raw capture/lossy → T5; §3 concept authority → T1; §4 role policy → T2; §5 operation grammar → T3; §6 governed source binding → T4; the adapter + preservation → T5; the generation wiring (surfacing behind the flag) → T6; §12 Gate 1 → T7; §13 gold (production-command authority) → T7; behaviour-neutrality → T8. **Deferred (correctly absent):** §7 worker/outbox, §8 canonical-input/provenance replay, §10 production telemetry store, Gate 2 (§12), the provisioning capability (§3.1), the live flip.

**Placeholder scan:** T1/T2/T4/T5/T7 carry concrete signatures + test cases; T3/T6/T8 name exact reused seams. No TBD/"add validation".

**Type consistency:** `SemanticRole`/`PathAggregation`/`FinalOperation`/`MultiSourceReason` + the `MultiSourcePlannerIntentV1` tree are consumed from `multisource_contracts.py` unchanged; `PlannerConceptBinding`/`ConceptAuthority` (T1), `computation_role` (T2), `NormalizedOp` (T3), `resolve_source_binding` (T4) are produced then consumed by T5; T6 calls `normalize_feature_idea`+`plan_multi_source`; T7 drives the full chain.

**Quality-bar check:** the authority bar (T1 accepted-pairs-only; T4 VERIFIED-grain-only) and Gate 1 (T7 zero-false-resolve + exact-outcome + preservation) are present and non-vacuity-tested — the demo cannot resolve on relaxed authority.
