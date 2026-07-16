# Phase 3B.4 — Shadow Harness + Objective 3C Enablement Gate Design (v2)

**Status:** approved design v2 (2026-07-16), reshaped after a 15-finding adversarial review (7 Blocker / 8 High — all accepted; 3 verified as real bugs in merged 3B.3c). Successor to 3B.3c (merged `b9131ea`). Terminal deliverable: the objective, signed-off gate that governs whether 3C (enforcement) may be enabled.
**v1's errors:** capture integrity was unprovable (no record of the *expected* set); the replay fingerprint hashed the join graph, not the classifier's actual read-set; the wall-time budget was inert; selection could report a "selected" plan from a partially-compiled set; the persistence model conflated three orthogonal states; the statistical bound counted non-independent repeated contracts.

## 1. What 3B.4 is

3B.3c *computes* a contract classification and discards it (log-only). 3B.4 **persists** those classifications, **measures** the population, and defines the **objective, conjunctive enablement gate** for 3C. **The gate is "verdicts are trustworthy enough to enforce," NOT "enough plans resolve"** — a resolution-rate target would conflict with 3B.3c's validate-not-derive contract and pressure the system to fabricate.

Two required, complementary artifacts:
- **A curated gold set** — expert-labelled scenarios with expected verdicts. Proves *correctness* (esp. false-resolves, which the unlabelled durable population cannot detect). Versioned + content-hashed; signed.
- **The durable population report** — real shadow runs. Proves *representativeness, operational completeness, replay behaviour*.

**Both required; neither substitutes for the other.** Still shadow / flag-gated / behaviour-neutral; persists telemetry and computes readiness; enforces nothing.

## 2. Capture integrity: the dispatch manifest (fixes F1, F2)

A missing result row is not evidence — you cannot tell an un-run recipe from a lost write, and `resolve_catalog_scope`/`build_compiler_context` can fail *before* the recipe loop even starts (`shadow.py:35`, no rows at all). So capture integrity is proven against a **durable dispatch manifest written FIRST**, before any planning:

- **`planner_shadow_dispatch`** (append-only) — one row per shadow run, written at run start: `generation_run_id`, `catalog_scope_id`, `compile_flag`, the **exact eligible recipe-id set + its hash**, `expected_count`, `invocation_predicate` (the guard that fired the shadow: `catalog_source is None and target_entity is not None`), `applicability_version`, `producer_commit`, `compiler/registry versions`, `created_at`. This is the ground truth the population is reconciled against.

**Capture integrity = for every manifest, `expected_count` run-result rows exist, one per manifest recipe id.** A pre-loop failure leaves a manifest with zero results → *detected* as total loss, not invisible. `manifest.recipe_hash` lets the gate confirm the eligible set itself wasn't silently altered.

**Persistence must not be circular (F2):** recording a store failure *in the failing store* is unreliable. So:
- Each recipe's parent `run_result` + child `plan_observation` rows are written **atomically** (one transaction), on a **fresh fallback savepoint** (independent of the planning savepoint at `shadow.py:55` and the route savepoint at `contract.py:409`).
- The **independent loss signal** is the manifest-vs-results reconciliation (a manifest recipe with no result row = confirmed loss) **plus** a structured-log/metric `persistence_loss` counter emitted outside the DB write path. Gate 1 consumes the reconciliation, not a self-reported row.

## 3. The persistence model: three orthogonal axes + DB hygiene (fixes F10, F11)

`run_outcome` conflated planning, compile completeness, and persistence health. Split into three fields on `planner_shadow_run_result` (one row per `generation_run_id` × `recipe_id`):
- **`planner_outcome`** ∈ {`compiled`, `no_physical_plan`, `internal_error`, `no_authorized_catalog`, `template_not_found`} — the planning disposition.
- **`compile_outcome`** ∈ {`complete`, `partial`, `skipped`, `budget_exhausted`} — did EVERY candidate for the recipe compile? (see §5).
- **`capture_status`** ∈ {`persisted`, `persistence_partial`} — the write health.

**`planner_shadow_plan_observation`** (one row per candidate physical plan) persists — additive to what the metric needs (F11): `generation_run_id`, `recipe_id`, `physical_plan_id`, `contract_id`, **`path_resolution_status`** (the metric filters on it), `contract_resolution_status`, `declaration_status`, `contract_primary_reason_code`, `contract_reason_codes`, `bridge_count`/`tier`, `preference_rank` + selection evidence, the compiled declarations (canonical JSON), the enriched replay stamp (§6). **`is_selected` is DERIVED** by joining the parent's `selected_contract_physical_plan_id` — never a duplicated boolean that can disagree.

**DB hygiene (all three tables):** composite primary keys (`generation_run_id` + …); foreign keys to the manifest/run; DB-level CHECK constraints on every status enum; **WORM** — `REVOKE UPDATE, DELETE` (append-only, enforced, not just convention); **idempotent** writes (a retried run reconciles by `(generation_run_id, recipe_id, physical_plan_id)`; a divergent duplicate write is a validated conflict, not a silent overwrite); a `payload_schema_version` + payload hash on the JSON columns; canonical (sorted-key) JSON serialization. Key on **`generation_run_id`** (never the ambiguous `run_id`).

## 4. Selection trustworthiness: compile_completeness (fixes F6)

The C8 selection chooses the best among whatever COMPILED, ignoring budget-skipped physical plans (`plan.py:170`). So a "selected" plan can be the best of a *partial* set while a better, uncompiled candidate exists. **`is_selected` is eligible for the metric, the audit, and 3C ONLY when `compile_outcome == complete`.** A `partial`/`budget_exhausted` recipe's selection is *provisional* and excluded from the enforcement denominator (counted separately as an operational-completeness gap that Gate 1 surfaces).

## 5. The compiler-input fingerprint (fixes F3, F8) — the replay foundation

`realization_fingerprint` hashes only graph_node `(object_ref, kind, table_name, is_grain, concept)` + join edges — it **omits `additivity`, `is_as_of`, `entity`, `sensitivity`**, the exact `_Col` fields the classifier reads (additivity flips an aggregation verdict; is_as_of/entity/sensitivity feed temporal/safety/connectivity). A change to any of them changes the verdict **without moving `realization_fingerprint`** → a false `current`. So replay is founded on a new **`compiler_input_fingerprint`** — a role/scope-scoped hash over EVERY input this plan's classification actually consumed:
- the plan's read-set `_Col` rows in full (`additivity, is_as_of, entity, sensitivity, concept, is_grain, data_type` — read-scope-filtered exactly as the compiler loaded them),
- the realizations the path used (by `realization_id` + their cardinality/keys),
- the **scope-filtered** bridge fact-key set (§6 — not the global bridge fingerprint),
- the compiled declarations (aggregation/temporal/read-set),
- read-scope (the `authz_role_claims`), and all compiler + registry versions.

Scoped to the plan (not the whole catalog) so an unrelated change elsewhere doesn't drift it. This also completes the **frozen-input identity** (F8): a canonical **`compiler_input_hash`** over the above + `producer_commit`/build id + effective-config hash + REAL versions (fix `ROLE_RESOLUTION_VERSION="unknown"`). Reports compare only within a **homogeneous producer cohort** (never mix producer versions).

## 6. Replay stamp enrichment + scope-filtered bridge signal (fixes F4, F5)

Each persisted per-catalog stamp carries: the **`compiler_input_fingerprint`** (§5), the drift watermark + head_seq, and the **projection checkpoint** — but the checkpoint is a **LAG invariant, not an equality signal (F4)**: the checkpoint is global projection progress that advances for unrelated events, so requiring equality would falsely mark unchanged contracts `drifted`. The rule: `checkpoint >= relevant head_seq` → caught up; `checkpoint < head_seq` (missing/regressed/lagging) → `unverifiable`. Advancement alone is never drift.

The **bridge drift signal is scope-filtered, not global (F5)**: persist the exact scope-visible / plan-used bridge fact-key set (already in the replay envelope as `active_bridge_fact_keys`), not the global `bridge_fingerprint()` — an unrelated out-of-scope bridge change must not drift every stored plan.

## 7. `ReplayFreshness` — pure comparator + impure adapter (fixes F15, F4)

Split into a **pure comparator** (`stored evidence × current evidence → verdict`) and an **impure current-state adapter** (reads current fingerprints/checkpoints). The adapter reads under a **consistent snapshot or revalidation protocol** so multi-catalog current values are mutually consistent. States:
```python
class ReplayFreshness(StrEnum):
    current = "current"        # every scoped compiler_input_fingerprint + bridge set matches, checkpoint not lagging
    drifted = "drifted"        # a catalog-state input changed since compile
    incompatible = "incompatible"  # producer/compiler/registry VERSION mismatch — comparison is not meaningful (NOT drift)
    unverifiable = "unverifiable"  # a stamp is missing/incomplete, checkpoint lagging, stamp_consistency was unverifiable, or a current value can't be read
```
A **version mismatch is `incompatible`, not `drifted`** (F15). **`unverifiable`/`incompatible` must NEVER be reported/treated as `current`** (fail-closed). Never mutates the stored record.

## 8. The two-layer cause taxonomy (fixes F9)

Whether `physical_cardinality_unavailable` or `safety_rejected` is expected data, unsupported topology, or a bug depends on **evidence + expert expectation**, not the reason code alone. So cause-labelling is TWO layers:
- **Layer A — static reason CATEGORY** (`ReasonCategory`, a versioned map, MACHINE): every `ReasonCode` in the registry → its structural category (`missing_authoring` / `policy_or_catalog_state` / `topology_or_model` / `bounding` / `internal`). **Exhaustive over the whole `ReasonCode` registry** — a static test asserts every code is mapped *even if not observed in the window* (a new unmapped code fails the check → `operationally_unmeasured`).
- **Layer B — contextual classification** (per observation, EVIDENCE + EXPERT): is this observed reason, on its evidence, `expected`, `unsupported_topology`, or a `classifier_defect`? `classifier_defect` is a Layer-B determination, never inferred from the code.

Distinct terminal labels: **`operationally_unmeasured`** = a reason code with no Layer-A map entry (a registry gap); **`unknown`** = mapped but Layer-B-unclassifiable pending evidence. The release gate (§10) requires **zero `classifier_defect` (Layer B), zero `operationally_unmeasured` (Layer-A exhaustiveness), zero `unknown`** — it does NOT claim every unresolved result is missing authoring (a correct `safety_rejected` or unsupported-topology reject passes).

## 9. Curated gold set + `contract_eval` harness

Deterministic exact-match (the classifier is deterministic). New `planner/contract_eval.py` + `planner/contract_gold.py`:
- **Gold set** — curated scenarios (seeded catalog fixtures + recipe), each with the EXPECTED `declaration_status` / `contract_resolution_status` / primary reason / Layer-B cause / (for `resolved`) an **immutable expert assertion that the compiled contract is genuinely valid**. Covers the adversarial shapes (multi-grain, disconnected tables, non-additive fan-in, semi-additive-across-time, bridge roll-up, unsafe join key, ambiguity, freshness, take_latest-without-ordering). **Versioned + content-hashed**; each case carries immutable sample IDs + the expert label.
- **`evaluate()`** — exact-match verdict+cause; the **false-resolve check** is strictest: a gold case labelled "not a valid contract" that the classifier calls `resolved` is a FAILURE.
- **Stratified real-population audit** — samples stored `is_selected`+`complete` plans by **unique `compiler_input_hash`** (see §10 sampling), across non-overlapping strata, for expert inspection.

## 10. The conjunctive 3C enablement gate (every sub-gate; no averaging)

**Machine-computed:** gates 1, 2, 5, 6. **Human-labelled + signed:** gates 3, 4, 7. A human provides labels/approval but **cannot override a FAILED machine gate.**

1. **Capture integrity** — every manifest recipe id has a run-result row (`expected_count` reconciled); `recipe_hash` intact; `persistence_loss == 0`; pre-loop failures visible.
2. **Population explainability** — Layer-A map exhaustive over the registry; **zero** `operationally_unmeasured`; every observation Layer-B classified; **zero** `classifier_defect`, **zero** `unknown` in the window.
3. **No false resolves** — zero on the COMPLETE curated gold set AND zero in the stratified real-population audit (over `is_selected` + `compile_outcome==complete` only).
4. **Statistical bound** — sampling unit = a **unique frozen-input/contract-shape fingerprint (`compiler_input_hash`/`contract_id`)**; **dedupe retries + repeated traffic** (repeated runs of one contract are NOT independent); non-overlapping strata; preserve the random seed + sampling frame; finite-population correction. With zero observed false-resolves, the one-sided 95% upper bound ≈ 3/n (rule of three; Clopper-Pearson exact) → **~300 independent examples per gated stratum for a 1% bound**. The max acceptable bound is a signed POLICY parameter; 3B.4 provides the machinery.
5. **Replay stability** — for an UNCHANGED frozen envelope: identical `selected physical_plan_id`, `contract_id`, `declaration_status`, `declaration reason codes`. Do NOT require `contract_resolution_status` identity after drift — `contract_id` excludes freshness (`contracts.py:671`), so a changed freshness verdict under drift is CORRECT (`ReplayFreshness=drifted`), not instability.
6. **Drift detection** — 100% on controlled catalog / realization (incl. additivity/is_as_of/sensitivity) / bridge / projection / version mutations; `unverifiable`/`incompatible` never treated as `current`.
7. **Artifact integrity** — a machine-readable signed report: code commit, gold-set hash, query/evaluator version, registry versions, policy hash, report-input digest, observation window, **immutable sample IDs + expert labels**, signer authority, a **DETACHED approval/signature** (the evaluator cannot sign its own output), a verification command, **nonzero exit on any failed gate**.

## 11. Real-bug folds (from the review — merged 3B.3c defects)

- **Inert wall-time budget (F12):** `compile_ctx.now < budget.deadline` compares the FIXED deterministic `now` against `now + COMPILE_BUDGET` → always true; only the count limit fires. Fix: an **injectable monotonic clock** distinct from the deterministic `now` — the budget deadline uses the monotonic clock; it does NOT enter `contract_id`/the verdict (determinism preserved). A real elapsed-time test proves the deadline fires.
- **`take_latest` needs `anchor_binding`, not `pit_anchor` (F13):** `compile_temporal` sets `pit_anchor` from the metadata role even when no column is bound (`declarations.py:268`). The guard must require a BOUND ordering column (`anchor_binding is not None`); absent → a specific `aggregation_ordering_column_missing` reason (undeclared).
- **`AggregationFunction` extension invalidates sign-off:** extending the vocabulary bumps the aggregation-rule/registry version → the artifact-integrity check (gate 7) sees a version change → **prior sign-off is invalidated** by construction.

## 12. Contracts summary

New tables (migration `0997`): `planner_shadow_dispatch`, `planner_shadow_run_result`, `planner_shadow_plan_observation` (append-only, WORM, composite-keyed, CHECK-constrained) + `planner/shadow_store.py`. New: `PlannerOutcome`/`CompileOutcome`/`CaptureStatus`/`ReasonCategory` enums; `compiler_input_fingerprint`/`compiler_input_hash`; `ResolutionCause` (Layer B) + the versioned Layer-A map + exhaustiveness test; `ReplayFreshness` reader (pure comparator + impure adapter, +`incompatible`); `planner/contract_eval.py` + `planner/contract_gold.py`; `planner/shadow_report.py`; the signed enablement-report shape + verifier. Extended: `CatalogStateStampV1` + `compiler_input_fingerprint`/`projection_checkpoint`; `PlannerReplayEnvelopeV1` keeps the scope-filtered `active_bridge_fact_keys`; a real `ROLE_RESOLUTION_VERSION` + `producer_commit`/config hash; the monotonic-clock seam in `run_shadow_planner`/`plan_bindings`; the `take_latest` `anchor_binding` guard; rule-version bumps. Flag reuse: `FEATUREGEN_INTENT_CONTRACT_COMPILE` gates the store write too.

## 13. Task decomposition

- **D1 — migration 0997 + the three tables + store contracts.** Manifest + run_result + plan_observation; WORM/CHECK/composite-key/idempotent; `PlannerOutcome`/`CompileOutcome`/`CaptureStatus`; `planner/shadow_store.py` writers/readers; canonical JSON + payload schema hash.
- **D2 — dispatch manifest + capture-integrity wiring.** Write the manifest FIRST (before the loop, capturing pre-loop failures); atomic parent+children per recipe on a fresh fallback savepoint; the independent `persistence_loss` signal + manifest reconciliation. Behaviour-neutral (store only when compiling; response untouched).
- **D3 — `compiler_input_fingerprint` + `compiler_input_hash` + stamp enrichment.** The role/scope-scoped fingerprint over the classifier's real read-set; enrich `CatalogStateStampV1`; real `ROLE_RESOLUTION_VERSION` + producer/config; homogeneous-cohort discipline.
- **D4 — `ReplayFreshness` (pure comparator + impure snapshot adapter).** The 4 states; checkpoint-as-lag-invariant; scope-filtered bridge signal; version-mismatch→incompatible; unverifiable≠current.
- **D5 — two-layer cause taxonomy.** Layer-A map (exhaustive over the registry, static test) + Layer-B contextual/expert classification; `operationally_unmeasured` vs `unknown`.
- **D6 — compile_completeness (F6) + budget monotonic-clock fix (F12) + take_latest anchor_binding (F13).** The completeness gate on `is_selected`; injectable monotonic clock + elapsed-time test; the ordering-column guard.
- **D7 — curated gold set + `contract_eval`.** Versioned/hashed gold set + immutable expert labels; exact-match + false-resolve `evaluate()`; the stratified-audit sampler (dedup by `compiler_input_hash`, seeded, stratified).
- **D8 — population report + the 7-gate conjunctive evaluator + signed artifact.** Selected-candidate denominator (complete-only); per-dimension + per-cause; recipe-outcome + freshness distributions; the statistical-bound machinery (dedup, finite-population, sample-size); the machine-readable signed report + detached signature + verifier + nonzero exit; human-can't-override-a-failed-machine-gate.

## 14. Mandatory tests (expanded per the review's "missing tests")

Store round-trip + selected-only denominator; **pre-loop scope/context failure** (manifest exists, zero results → total-loss detected); **total store outage AND fallback-write failure** (independent loss signal fires); **partial candidate compilation** (`is_selected` excluded until `complete`); **real elapsed-time budget timeout** (monotonic clock, not count); **additivity / is_as_of / sensitivity change without watermark movement → `drifted`** (proves `compiler_input_fingerprint`, not `realization_fingerprint`); **unrelated projection checkpoint advancement → NOT drifted** (lag invariant); **out-of-scope bridge change → NOT drifted** (scope-filtered); **mixed producer versions → `incompatible`, cohort-separated**; **divergent duplicate write → validated conflict** (idempotency); **cause-map exhaustiveness over the registry** (unmapped code → `operationally_unmeasured`); **clustered/repeated-contract sampling deduped** (statistical unit = unique `compiler_input_hash`); **signed-report tampering → verification fails / nonzero exit**; gold-set exact-match + false-resolve; conjunctive gate (any sub-gate fails → gate fails; human can't override a failed machine gate); stability-vs-freshness (unchanged→identical id/verdict; drifted→same contract_id + `ReplayFreshness=drifted`); take_latest with/without a bound ordering column; behaviour-neutral (flag off → no store write, response byte-identical; full `tests/featuregen` green).

## 15. Phase boundaries

```
3B.3a/b/c   find → build → CLASSIFY (compute-only, honest-unresolved)
3B.4        PERSIST (manifest+run+plan, WORM) + fingerprint + replay-freshness + cause-label + gold set + conjunctive signed gate   ← this
3C          ENFORCE — only after the signed gate passes; live grounding + review UIs
3D          aggregation/temporal AUTHORING surfaces + multi-grain/multi-branch (move recipes unresolved→resolved)
```
