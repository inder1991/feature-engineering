# Phase 3B.4 — Shadow Harness + Objective 3C Enablement Gate Design (v3)

**Status:** approved design v3 (2026-07-16). v2 folded a 15-finding review; v3 folds a second 15-finding review (7 Blocker / 8 High — all accepted, 3 more verified as merged-3B.3c defects). Successor to 3B.3c (`b9131ea`). Terminal deliverable: the objective, signed-off gate governing whether 3C (enforcement) may be enabled.

## 1. What 3B.4 is

3B.3c *computes* a contract classification and discards it (log-only). 3B.4 **persists** those classifications, **measures** the population, and defines the **objective conjunctive enablement gate** for 3C. **The gate is "verdicts are trustworthy enough to enforce," NOT "enough plans resolve"** — a resolution-rate target conflicts with 3B.3c's validate-not-derive contract. Two required artifacts: a **curated gold set** (expert-labelled → proves correctness incl. false-resolves) and the **durable population report** (real runs → proves representativeness / operational completeness / replay behaviour). Both required. Still shadow; enforces nothing.

## 2. Telemetry flag + capture-integrity (fixes F3, F1, F2, F11, F15)

**A separate telemetry flag, not the compile flag (F3).** The compile flag (`FEATUREGEN_INTENT_CONTRACT_COMPILE`) controls whether plans are *compiled* (verdict computed). A NEW `FEATUREGEN_INTENT_SHADOW_TELEMETRY` flag controls whether 3B.4 *persists*. It is written **whenever the invocation predicate fires, independent of the compile flag** — otherwise a release window silently omits flag-off traffic (only `compile_flag=true` runs would be recorded). Default (both flags off) → zero writes → behaviour-neutral preserved. A release window runs telemetry-on; compile-off traffic during the window still gets a manifest + a run-result recording `compile_status=skipped`, so the window's full traffic is provably captured.

**The dispatch manifest — written FIRST, before scope resolution (F1, F2).** `planner_shadow_dispatch` (append-only), one row per shadow run, written at the very top of `run_shadow_planner` **before `resolve_catalog_scope`**: `generation_run_id`, the **exact eligible recipe-id set + its hash** (available — the route passes `applicability.eligible_ids`), `expected_count`, `invocation_predicate`, `compile_flag`, `telemetry_flag`, `applicability_version`, `producer_commit`, compiler/registry versions, `created_at`. It **does NOT carry `catalog_scope_id`** — that is a `resolve_catalog_scope` *output*, one of the pre-loop ops whose failure the manifest must survive (F1). The resolved scope id is stored on the run-result rows instead.

**Pre-loop failure must not roll back the manifest (F2).** The route wraps the whole `run_shadow_planner` call in one savepoint (`contract.py:409`). So D2 writes the manifest, then wraps `resolve_catalog_scope` + `build_compiler_context` in a **nested savepoint whose failure is caught INSIDE `run_shadow_planner`** (records a run-level `planner_outcome=preloop_failure` and returns normally) — so the outer route savepoint commits and **retains the manifest + the failure record**. `run_shadow_planner` never propagates a pre-loop exception.

**Capture integrity = for every manifest, `expected_count` run-result rows exist**, one per manifest recipe id; `recipe_hash` intact; pre-loop failures visible; the loss signal is the **manifest↔results reconciliation** (independent of any self-reported row) plus an external `persistence_loss` metric.

**Persistence write protocol (F11).** Per recipe: attempt an **atomic** parent(`run_result`)+children(`plan_observation`) write. On failure: roll it back, then a **second minimal-parent insert** (`run_result` only, `capture_status=persistence_partial`, no children) on a **fresh savepoint**. If *that* also fails, only the manifest reconciliation + the external loss signal remain (no circular self-report). All three tables: append-only, **`REVOKE UPDATE, DELETE, TRUNCATE`** (F15 — matching the 0974 precedent), composite PKs/FKs, DB CHECK constraints on every enum, idempotent writes keyed by `(generation_run_id, recipe_id[, physical_plan_id])` with divergent-duplicate = validated conflict, `payload_schema_version` + payload hash, canonical sorted-key JSON. Key on `generation_run_id`.

## 3. Persistence model: orthogonal axes (fixes F10)

`planner_shadow_run_result` (one row per `generation_run_id`×`recipe_id`) carries three ORTHOGONAL fields:
- **`planner_outcome`** ∈ {`compiled`, `no_physical_plan`, `internal_error`, `no_authorized_catalog`, `template_not_found`, `preloop_failure`}.
- **`compile_status`** ∈ {`complete`, `incomplete`, `not_applicable`} + **`incomplete_reason`** ∈ {`budget_count`, `budget_time`, `error`, `null`}, plus **counts** `path_resolved_eligible` / `compiled` / `skipped`. **Completeness is relative to PATH-RESOLVED candidates (F10):** only `source_to_target_resolved` plans are compile-eligible (`plan.py:162`), so `complete` = every path-resolved candidate compiled; `not_applicable` = the recipe produced no path-resolved candidate.
- **`capture_status`** ∈ {`persisted`, `persistence_partial`}.

`planner_shadow_plan_observation` (one per candidate physical plan): `physical_plan_id`, `contract_id`, `contract_input_hash` (§5), **`path_resolution_status`** (the metric filters on it — F12), `contract_resolution_status`, `declaration_status`, `contract_primary_reason_code`, `contract_reason_codes`, `bridge_count`/`tier`, `preference_rank`, the compiled declarations (canonical JSON) + their output hash (§5), the enriched replay stamp (§6). **`is_selected` is DERIVED** by joining the parent's `selected_contract_physical_plan_id` (never a duplicated boolean).

## 4. Two input identities (fixes F4, F5) — the replay foundation

`realization_fingerprint` omits `additivity`/`is_as_of`/`entity`/`sensitivity` — the `_Col` fields the classifier reads — so a verdict-changing edit doesn't move it. And a plan-scoped hash can't prove **selection** stability: discovery reads EVERY authorized column (`candidates.py:39`), so a new better candidate not read by the old selected plan changes the *selection* while the selected plan's inputs are unchanged (F5). So TWO identities, both **inputs-only — never classifier outputs (F4)**:
- **`planner_input_hash`** — over the FULL candidate/ranking universe (every authorized `_Col` row loaded by discovery, in full: `additivity, is_as_of, entity, sensitivity, concept, is_grain, data_type`; all realizations; the scope-filtered bridge set; read-scope; versions). Determines WHICH plan is selected → selection stability.
- **`contract_input_hash`** — over the SELECTED plan's consumed inputs only (its read-set `_Col` rows + used realizations + used bridges + read-scope + versions). Determines the contract VERDICT for that plan → verdict stability.

Both hash **pre-classification state only.** The compiled declarations + verdict are OUTPUTS — persisted separately with their own `output_hash`, compared for stability but never mixed into the input hashes (else a bug that changes an output would look like a changed input, masking instability). This also completes the frozen-input identity: a real `ROLE_RESOLUTION_VERSION` (currently `"unknown"`), `producer_commit`/build id, effective-config hash. Reports compare only within a **homogeneous producer cohort**.

## 5. Replay stamp + scope-filtered bridge signal (fixes prior F3/F4/F5 verified)

Each persisted per-catalog stamp carries the `contract_input_hash` component fingerprints + drift watermark/head_seq + the **projection checkpoint as a LAG invariant, not equality**: the checkpoint is global progress that advances for unrelated events, so `checkpoint >= relevant head_seq` → caught up; `checkpoint < head_seq` → `unverifiable`. Advancement alone is never drift. The **bridge drift signal is the scope-filtered fact-key set** (the envelope's `active_bridge_fact_keys`), NOT the global `bridge_fingerprint()` — an unrelated out-of-scope bridge change must not drift every stored plan.

## 6. `ReplayFreshness` — pure comparator + impure snapshot adapter (fixes F15-v2/F4-v2, F8-v2/F15-v2)

Split a **pure comparator** (`stored × current → verdict`) from an **impure adapter** that reads current fingerprints under a **consistent snapshot / revalidation protocol** (multi-catalog current values mutually consistent). States:
```python
class ReplayFreshness(StrEnum):
    current = "current"; drifted = "drifted"
    incompatible = "incompatible"   # producer/compiler/registry VERSION mismatch — comparison not meaningful (NOT drift)
    unverifiable = "unverifiable"   # stamp missing/incomplete, checkpoint lagging, stamp_consistency unverifiable, or a current value unreadable
```
Version mismatch → `incompatible` (not `drifted`). `unverifiable`/`incompatible` are **never `current`** (fail-closed). Never mutates the stored record.

## 7. Two-layer cause taxonomy (fixes F9-v2, F6)

- **Layer A — static reason CATEGORY** (`ReasonCategory`, versioned map, MACHINE): every `ReasonCode` → structural category (`missing_authoring`/`policy_or_catalog_state`/`topology_or_model`/`bounding`/`internal`). **Exhaustive over the whole registry** — a static test asserts every code is mapped even if unobserved; an unmapped code → `operationally_unmeasured`.
- **Layer B — contextual classification** (per DISTINCT observed shape, EVIDENCE + EXPERT): is this reason, on its evidence, `expected` / `unsupported_topology` / `classifier_defect`? Never inferred from the code alone.

`operationally_unmeasured` (a registry-map gap) is distinct from `unknown` (mapped but Layer-B-unclassifiable). `classifier_defect` is a Layer-B (human) determination over a **deduplicated population-review artifact** (distinct reason+evidence shapes, not every row).

## 8. Curated gold set + `contract_eval`

Deterministic exact-match. `planner/contract_gold.py` (versioned, content-hashed; seeded catalog fixtures + recipe; each case: expected `declaration_status`/`contract_resolution_status`/primary reason/Layer-B cause + an immutable expert `resolved`-validity assertion; immutable sample IDs; adversarial shapes incl. take_latest-without-ordering) + `planner/contract_eval.py` (`evaluate()` exact-match + strict false-resolve check; a stratified real-population audit sampling `is_selected`+`complete` plans by **distinct `contract_input_hash`**).

## 9. The population report (RESTORED explicit contract — fixes F12)

Over the store, within a homogeneous producer cohort and the release window:
- **`physically_resolved_but_contract_unresolved`** = **numerator** = count of (run×recipe) selected observations where `path_resolution_status == source_to_target_resolved` AND `is_selected` AND `compile_status == complete` AND `contract_resolution_status != resolved`; **denominator** = count of (run×recipe) selected observations where `path_resolution_status == source_to_target_resolved` AND `is_selected` AND `compile_status == complete`. **One observation per (generation_run_id, recipe_id)** (the selected plan). Incomplete/partial compiles are NOT in the denominator (and are gated separately — §10 gate 1).
- **Per-dimension + per-cause breakdown** — a plan with multiple reason codes is counted **once in the headline** (by the primary), and **once per distinct dimension/Layer-A category** in the breakdown (multi-reason counting is explicit: headline = by-primary, breakdown = by-each).
- **Recipe-outcome distribution** (from `run_result`: the `planner_outcome` × `compile_status` matrix, incl. `preloop_failure`, `template_not_found`, `no_authorized_catalog`, incomplete).
- **Replay-freshness distribution** over stored plans (`current`/`drifted`/`incompatible`/`unverifiable`).
The schema is fixed here so implementation and gate policy cannot pick different populations.

## 10. The conjunctive 3C enablement gate (every sub-gate; no averaging)

**Machine-only:** 1, 2a, 5, 6. **Human-labelled + signed:** 2b, 3, 4, 7.

1. **Capture + completeness integrity** — every manifest recipe id has a run-result row (`expected_count` reconciled, `recipe_hash` intact); `persistence_loss == 0`; pre-loop failures visible; **AND zero incomplete eligible recipes** (`compile_status==incomplete`) in the window, OR a **signed bounded-exclusion** that provably covers no risky stratum (F7) — incompleteness cannot silently bias the audited population toward easy plans.
2. **Population explainability** — **(2a, machine)** Layer-A map exhaustive over the registry; zero `operationally_unmeasured`. **(2b, human)** every distinct observed reason-shape Layer-B-labelled on a deduplicated population-review artifact; zero `classifier_defect`; zero `unknown` (F6 — the unlabelled population cannot prove zero defects; a human labels the deduped shapes).
3. **No false resolves** — zero on the COMPLETE gold set AND zero in the stratified real-population audit (over `is_selected`+`complete` only).
4. **Statistical bound** — the estimand is **future traffic** → **binomial Clopper-Pearson, NO finite-population correction** (F9). Sampling unit = **distinct contract shape** (`contract_input_hash`/`contract_id`), deduped (repeated runs are not independent), **shape-weighted** (we bound risk over untested input SHAPES, not traffic frequency); non-overlapping strata; preserved seed + sampling frame. Zero observed → one-sided 95% upper bound ≈ 3/n → **~300 distinct shapes per gated stratum for a 1% bound**. The max bound is a signed POLICY parameter. **Rare strata** (fewer than the required distinct shapes) FAIL the gate for that stratum unless signed-excluded with justification. 3B.4 provides the machinery.
5. **Replay stability — with an active procedure (F8).** Not vacuous: **compile each gold + audit case TWICE from the same frozen fixture** (deterministic → must be identical) AND require a minimum repeated-input cohort in the store; an EMPTY comparison set FAILS. Compare `selected physical_plan_id` (via `planner_input_hash` invariance), `contract_id`, `declaration_status`, declaration reason codes. Do NOT require `contract_resolution_status` identity after drift (`contract_id` excludes freshness — a changed freshness verdict under drift is correct, not instability). **Exclude budget-INCOMPLETE executions** from these comparisons (F13 — an operationally-truncated run is not a determinism datapoint).
6. **Drift detection** — 100% on controlled catalog (incl. additivity/is_as_of/sensitivity) / realization / bridge / projection / version mutations; `unverifiable`/`incompatible` never `current`.
7. **Artifact integrity** — a machine-readable signed report: code commit, gold-set hash, evaluator version, registry versions, policy hash, report-input digest, observation window, immutable sample IDs + expert labels, signer authority, a **DETACHED signature** (the evaluator cannot sign its own output), a verification command, **nonzero exit on any failed gate**. A human provides labels/approval but **cannot override a FAILED machine gate**.

## 11. Merged-3B.3c defect folds (from both reviews)

- **Inert wall-time budget (F12-v2/F13):** `compile_ctx.now < now+COMPILE_BUDGET` is always true (only the count expires). Fix: an **injectable monotonic clock** distinct from the deterministic `now` for the deadline. **Budget expiry is an OPERATIONAL outcome, not "determinism-preserved" (F13):** it changes `not_compiled`/reason codes/`compile_status`/possibly selection, so budget-incomplete executions are **excluded from deterministic-verdict comparisons** (gate 5). A real elapsed-time test proves the deadline fires.
- **`take_latest` stage-local ordering validity (F14):** `anchor_binding is not None` only proves a column was bound *somewhere*. The ordering column must be **available at the aggregation hop** (its table on the path at/before that hop, via connectivity placement) AND **survive prior grouping** (not aggregated away at an earlier hop). It becomes part of that stage's physical inputs + safety read-set. Absent/unavailable/not-surviving → `aggregation_ordering_column_missing` (undeclared).
- **`AggregationFunction` extension invalidates sign-off:** bumps the aggregation-rule/registry version → gate 7's registry-version check invalidates prior sign-off by construction.

## 12. Contracts summary

New tables (migration `0997`, WORM incl. TRUNCATE, composite-keyed, CHECK-constrained, idempotent): `planner_shadow_dispatch`, `planner_shadow_run_result`, `planner_shadow_plan_observation` + `planner/shadow_store.py`. New: `PlannerOutcome`/`CompileStatus`/`IncompleteReason`/`CaptureStatus`/`ReasonCategory`/`ResolutionCause` enums + the versioned Layer-A map + exhaustiveness test; `planner_input_hash` (full universe) + `contract_input_hash` (selected plan) — inputs-only + a separate output hash; `ReplayFreshness` reader (pure comparator + impure snapshot adapter, `incompatible`); the telemetry flag; `planner/contract_eval.py` + `planner/contract_gold.py` + `planner/shadow_report.py` (the §9 schema); the signed report + detached-signature verifier. Extended: `CatalogStateStampV1` (fingerprints + checkpoint); real `ROLE_RESOLUTION_VERSION` + `producer_commit`/config hash; the monotonic-clock seam; the `take_latest` stage-local guard; rule-version bumps.

## 13. Task decomposition

- **D1 — migration 0997 + three tables + store contracts.** WORM(incl. TRUNCATE)/CHECK/composite-key/idempotent; `PlannerOutcome`/`CompileStatus`/`IncompleteReason`/`CaptureStatus`; `shadow_store.py`; canonical JSON + payload schema hash + the two-phase write protocol (atomic; minimal-parent fallback).
- **D2 — telemetry flag + dispatch manifest + capture wiring.** Separate telemetry flag (independent of compile); manifest written FIRST (no `catalog_scope_id`); nested savepoint around scope/context caught internally (`preloop_failure`, return normally); manifest↔results reconciliation + external loss signal. Behaviour-neutral when telemetry off.
- **D3 — the two input hashes + stamp enrichment.** `planner_input_hash` (full universe) + `contract_input_hash` (selected plan), inputs-only + separate output hash; enrich `CatalogStateStampV1`; real `ROLE_RESOLUTION_VERSION` + producer/config; homogeneous-cohort discipline.
- **D4 — `ReplayFreshness` (pure comparator + impure snapshot adapter).** 4 states; checkpoint lag invariant; scope-filtered bridge signal; version-mismatch→incompatible; unverifiable/incompatible≠current.
- **D5 — two-layer cause taxonomy.** Layer-A exhaustive map + static test; Layer-B contextual/expert over a deduplicated review artifact; `operationally_unmeasured` vs `unknown`.
- **D6 — defect folds.** compile_status(path-resolved eligibility) + the completeness gate; monotonic-clock budget fix + elapsed-time test + exclude-incomplete-from-stability; `take_latest` stage-local ordering guard.
- **D7 — curated gold set + `contract_eval`.** Versioned/hashed gold set + immutable expert labels; exact-match + false-resolve; stratified-audit sampler (dedup by `contract_input_hash`, seeded, shape-weighted strata) + the double-compile stability procedure.
- **D8 — population report (§9 schema) + the 7-gate conjunctive evaluator + signed artifact.** Selected+complete denominator; per-dimension/per-cause with explicit multi-reason counting; recipe-outcome + freshness distributions; binomial bound machinery (dedup, shape-weighted, rare-stratum gate); the machine-readable signed report + detached signature + verifier + nonzero exit; human-can't-override-a-failed-machine-gate.

## 14. Mandatory tests

Store round-trip + selected-only-complete denominator; **pre-loop scope/context failure** (manifest survives, `preloop_failure` recorded, route savepoint retains it); **flag-off traffic captured** (telemetry on, compile off → manifest + `compile_status=skipped`); **total store outage AND fallback-write failure** (manifest reconciliation + external signal fire, no circular row); **two-phase write** (atomic fails → minimal-parent fallback appears); **incomplete eligible recipe fails gate 1** (or requires a signed exclusion); **partial candidate compilation** excluded from denominator; **real elapsed-time budget timeout** (monotonic clock) + budget-incomplete excluded from stability; **additivity/is_as_of/sensitivity change without watermark → `drifted`** (proves the input hash, not `realization_fingerprint`); **new candidate column changes selection but not the old plan's contract_input_hash → caught by `planner_input_hash`** (F5); **output change under fixed inputs → detected as instability, not a new input** (F4); **unrelated checkpoint advancement → NOT drifted**; **out-of-scope bridge change → NOT drifted**; **mixed producer versions → `incompatible`, cohort-separated**; **divergent duplicate write → validated conflict**; **cause-map exhaustiveness over the registry** (unmapped → `operationally_unmeasured`); **clustered/repeated-contract sampling deduped** (unit = distinct `contract_input_hash`); **rare stratum fails the bound**; **empty stability comparison fails** (F8); **signed-report tampering → verify fails / nonzero exit**; **human cannot override a failed machine gate**; gold-set exact-match + false-resolve; stability (unchanged→identical; drifted→same contract_id + `ReplayFreshness=drifted`); take_latest with/without an available-and-surviving ordering column; behaviour-neutral (both flags off → no writes, response byte-identical; full `tests/featuregen` green).

## 15. Phase boundaries

```
3B.3a/b/c   find → build → CLASSIFY (compute-only, honest-unresolved)
3B.4        PERSIST (manifest+run+plan, WORM) + dual input-hash + replay-freshness + two-layer cause + gold set + conjunctive signed gate   ← this
3C          ENFORCE — only after the signed gate passes; live grounding + review UIs
3D          aggregation/temporal AUTHORING surfaces + multi-grain/multi-branch (move recipes unresolved→resolved)
```
