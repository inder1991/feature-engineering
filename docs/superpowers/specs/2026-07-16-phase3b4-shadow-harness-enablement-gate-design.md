# Phase 3B.4 — Shadow Harness + Objective 3C Enablement Gate Design

**Status:** approved design (2026-07-16). Successor to 3B.3c (contract resolvability classifier, merged `b9131ea`). Consumes 3B.3c's compiled `BindingPlanV1`/`BindingPlanningResultV1` (contract declarations + `audit_envelope`). Terminal deliverable: the objective, signed-off gate that governs whether 3C (enforcement) may be enabled.

## 1. What 3B.4 is (and the gate it defines)

3B.3c *computes* a contract classification and discards it (log-only). 3B.4 **persists** those classifications over real runs, **measures** the population, and defines the **objective enablement gate** for 3C — the same discipline as the 1A/1B gates (`recognition_eval`): nothing goes live until a measured signal says it's safe.

**The gate is "verdicts are trustworthy enough to enforce," NOT "enough plans resolve."** 3B.3c is deliberately validate-not-derive, so by design most plans are honestly `unresolved` for want of authoring. A resolution-rate target would directly conflict with that contract and would pressure the system to fabricate. So the gate is a **conjunctive readiness assessment** (§9) — every sub-gate must pass; a composite/averaged score is forbidden.

The gate rests on **two required, complementary artifacts**:
- **A curated gold set** — expert-labelled scenarios with expected verdicts. Proves *correctness* (esp. false-resolve detection, which the unlabelled durable population cannot do by itself). Versioned + hashed; a signed-off enablement artifact.
- **The durable population report** — the real shadow runs. Proves *representativeness, operational completeness, and replay behaviour* over production traffic.

Both are required. Neither substitutes for the other.

**In scope:** the two-table durable store (§3); replay-stamp enrichment (§4) then `ReplayFreshness` (§5); the 6-cause taxonomy + cause-labelling (§6); the curated gold set + `contract_eval` harness (§7); the population report keyed on *selected enforcement candidates* (§8); the conjunctive 3C gate + its statistical bound (§9); the two gating-note folds (§10). Migration `0997`.

**Out of scope:** 3C enforcement / live grounding / review UIs; any authoring surface (3D); multi-grain/multi-branch (3D). 3B.4 is still **shadow / flag-gated / behaviour-neutral** — it persists telemetry and computes readiness; it changes no response and enforces nothing.

## 2. Design posture

- **Persist, don't enforce.** The store is planner telemetry, NOT a governed `overlay_fact` (no four-eyes). Written by `run_shadow_planner` when the compile flag is on; never read by the live grounding path.
- **Capture integrity over convenience.** Every *eligible* (run × recipe) invocation is accounted for — including the recipe-with-no-template skip, no-authorized-catalog, internal_error, budget-exhaustion, and a store-write failure. A missing row is a capture defect, not an absent observation. The denominator must never be positively biased.
- **The absence of ground truth is explicit.** The durable population has no labels, so it cannot detect a false resolve. Only the gold set can. The gate names both.

## 3. The two-table durable store (fixes the positively-biased denominator)

A single plan-grained table loses every recipe that produced no selectable plan. So **two append-only tables** (migration `0997`):

- **`planner_shadow_run_result`** — ONE row per (generation_run_id, recipe_id): the recipe-level outcome. Columns: `run_id`, `recipe_id`, `catalog_scope_id`, `target_entity`, `result_status` (ingredient-axis), `contract_result_status`, `selected_contract_physical_plan_id`, `selected_contract_id`, `run_outcome` (a closed enum — see below), `candidate_plan_count`, `bounding` summary, `created_at`. This row exists even when there is NO plan.
  - `run_outcome ∈ {compiled_selected, no_physical_plan, internal_error, budget_exhausted_before_selection, no_authorized_catalog, template_not_found, persistence_partial}` — the recipe-level disposition that the plan table alone would drop.
- **`planner_shadow_plan_observation`** — ONE row per candidate physical plan: `run_id`, `recipe_id`, `physical_plan_id`, `contract_id`, `is_selected` (the enforcement candidate), `contract_resolution_status`, `declaration_status`, `contract_primary_reason_code`, `contract_reason_codes`, `bridge_count`/`tier`, the compiled declarations (aggregation/temporal/read-set as JSON), the enriched replay stamp (§4), `created_at`.

**Capture integrity:** `run_shadow_planner` writes exactly one `run_result` per eligible recipe id (the `template_not_found` skip becomes a row, not a silent `continue`). A store-write failure is caught and recorded (`persistence_partial` + a run-level `persistence_loss` counter) — never a silent drop. The write happens inside the per-recipe savepoint discipline already in place (a write failure isolates to that recipe, and the counter surfaces it).

## 4. Replay-stamp enrichment (REQUIRED before `ReplayFreshness`)

`CatalogStateStampV1` today carries only `head_seq` + `last_completed_at`; the compiler itself documents (`declarations.py:728`) that a graph rebuild can change realizations without advancing that watermark. Persisting only head_seq means replay would report `current` after meaningful drift. So each persisted per-catalog stamp is enriched to carry (additive contract change; bump the relevant versions):
- **per-catalog realization/graph fingerprint** (`realization_fingerprint` — the schema+graph+concept+derivation hash the compiler already computes at ctx-build but does not persist),
- **projection checkpoint** (`_checkpoint_seq(conn,"overlay")` at compile time),
- drift watermark + head sequence (present),
- and at the envelope level: the **active-bridge fingerprint** (or exact fact-key set) and **all compiler + registry versions** (mostly present in the envelope already — verify completeness).

`CatalogStateStampV1` gains `realization_fingerprint: str` + `projection_checkpoint: int`; the envelope gains/keeps `bridge_fingerprint`. C7's compile-time revalidation already computes these on the ctx — 3B.4 threads them onto the persisted stamp so replay can compare them.

## 5. `ReplayFreshness` (read-side; `unverifiable` is never `current`)

A pure read-side helper: given a stored plan's enriched stamps and the current catalog state, return `ReplayFreshness ∈ {current, drifted, unverifiable}`:
- **`current`** — every participating catalog's realization fingerprint + bridge fingerprint + projection checkpoint + head_seq match the persisted stamp.
- **`drifted`** — any fingerprint/head_seq/checkpoint differs (the graph moved since compile).
- **`unverifiable`** — a stamp is missing/incomplete, or `stamp_consistency` was already `unverifiable` at compile, or a required current value can't be read. **`unverifiable` must NEVER be reported/treated as `current`** (fail-closed).

Consumed by the population report + the drift-detection gate; never mutates the stored record (the immutable compile-time verdict stands).

## 6. The 6-cause taxonomy (not every non-authoring reason is a bug)

Cause-labelling borrows `readiness.py`'s discipline (it splits expected-deferred from genuine-error) but 3B.4 needs a finer taxonomy, because e.g. `safety_rejected` can be a CORRECT hard rejection and `ingredient_not_connected_to_path` can be genuinely unsupported topology — neither is a classifier bug. Every observed unresolved reason is mapped to exactly one **`ResolutionCause`**:

```python
class ResolutionCause(StrEnum):
    expected_missing_authoring = "expected_missing_authoring"       # aggregation_strategy_missing, semi_additive_temporal_strategy_missing, temporal_anchor_missing, aggregation_weight/components_missing
    expected_policy_or_catalog_state = "expected_policy_or_catalog_state"  # safety_rejected (correct hard block), unresolved_freshness, participating_catalog_stale, projection_lagging
    unsupported_topology_or_model = "unsupported_topology_or_model" # ingredient_not_connected_to_path, physical_cardinality_unavailable, aggregation_composition_unsupported, aggregation_axis_unsupported
    classifier_defect = "classifier_defect"                        # a reason that SHOULD NOT occur — a modelling/logic gap
    operationally_unmeasured = "operationally_unmeasured"          # observed but no cause rule maps it (e.g. a new reason code shipped without a taxonomy entry)
    unknown = "unknown"                                            # unmappable
```

A versioned `reason_code → ResolutionCause` map (`RESOLUTION_CAUSE_MAP`, its own registry version). **The release gate requires ZERO `unknown`, `classifier_defect`, or `operationally_unmeasured` observations** — it does NOT claim every unresolved result must be missing authoring. `safety_rejected`/topology causes are legitimate and pass. `operationally_unmeasured` deliberately catches a new reason code that shipped without a taxonomy entry (so the gate re-opens when the vocabulary grows).

## 7. Curated gold set + `contract_eval` harness

Modeled on `recognition_eval` but **deterministic exact-match** (not statistical recall — the classifier is deterministic). New `planner/contract_eval.py` + `planner/contract_gold.py`:
- **Gold set** — curated scenarios, each: a seeded catalog fixture (or a reference to one) + a recipe + the EXPECTED `declaration_status` / `contract_resolution_status` / primary reason / `ResolutionCause` / (for a `resolved` case) an expert assertion that the compiled contract is genuinely valid. Includes the adversarial shapes (multi-grain, disconnected tables, non-additive fan-in, semi-additive-across-time, bridge roll-up, unsafe join key, ambiguity, freshness). **Versioned + content-hashed**; carries the expert reviewer + sign-off.
- **`evaluate()`** — runs the compiler over the gold set, scores EXACT match of verdict + cause; the **false-resolve check** is the strictest: a gold case an expert labelled "not a valid contract" that the classifier calls `resolved` is a gold-set failure. Also a **stratified real-population audit**: sample stored `resolved` plans across strata (tier, family, dimension) for expert inspection — zero false resolves required.
- **`main()`** — a runnable report (the enablement artifact) recording code commit, gold-set hash, evaluator version, registry versions, observation window, reviewer, sign-off time (§9 artifact integrity).

## 8. Population report (denominator = selected enforcement candidates)

The headline metric uses **ONE observation per (generation_run_id, recipe_id) — the SELECTED contract plan** (`is_selected`). Lower-ranked candidates are retained in `planner_shadow_plan_observation` for diagnostics but MUST NOT inflate the enforcement denominator (they are not what 3C would enforce). The report over the store:
- **`physically_resolved_but_contract_unresolved`** — of selected plans that are `source_to_target_resolved` (path-resolved), the fraction whose `contract_resolution_status` is not `resolved`, broken down by dimension (connectivity / aggregation / temporal / safety / freshness) AND by `ResolutionCause`.
- Recipe-level outcome distribution (from `run_result`: no_physical_plan / internal_error / budget_exhausted / no_authorized_catalog / template_not_found / persistence_partial) — so the denominator is honest.
- Replay-freshness distribution over stored plans (`current`/`drifted`/`unverifiable`).
- Determinism/stability observations (§below).

## 9. The conjunctive 3C enablement gate (every sub-gate must pass)

3C may be enabled only when ALL hold in the release window (no averaging):

1. **Capture integrity** — 100% of eligible (run × recipe) invocations accounted for; `persistence_loss == 0`.
2. **Population explainability** — 100% of observed reason codes cause-labelled; **zero** `unknown` / `classifier_defect` / `operationally_unmeasured`.
3. **No false resolves** — zero on the COMPLETE curated gold set AND zero in the stratified real-population audit.
4. **Statistical bound** — a defined maximum acceptable one-sided upper confidence bound on the true false-resolve rate. *Determinism removes run randomness, not uncertainty about untested input shapes.* With zero observed false-resolves over an audit of N (per stratum), the one-sided 95% upper bound ≈ 3/N (rule of three; Clopper-Pearson exact). 3B.4 provides the MACHINERY (compute the bound from the audit sample); the **maximum acceptable upper bound is a signed POLICY parameter** (e.g. "≤ 1% at 95% one-sided"), not a hardcoded constant — and it sets the minimum audit sample size per stratum.

**Machine-computed vs human-labelled.** Gates 1, 2, 5, 6 are computed automatically from the store + controlled mutations. Gates 3, 4, 7 require **human expert input** — the gold-set validity labels, the stratified-audit inspection, and the sign-off — exactly like the 1A gold-set expert review. 3B.4 builds the harness that PRODUCES the signed enablement artifact; it does not auto-pass the gate. The final enablement remains a human decision recorded in the artifact.
5. **Replay stability** — 100% identity/verdict stability for UNCHANGED frozen inputs (§below).
6. **Drift detection** — 100% on controlled catalog / realization / bridge / projection / version mutations; `unverifiable` never treated as `current`.
7. **Artifact integrity** — the signed report records code commit, gold-set hash, query/evaluator version, registry versions, observation window, reviewer, sign-off time.

### Stability vs freshness (a required distinction)
"Replay stability" is about the DECLARATION, not freshness. For an UNCHANGED frozen envelope, require IDENTICAL: `selected physical_plan_id`, `contract_id`, `declaration_status`, `declaration reason codes`. Do **NOT** require the full `contract_resolution_status` to be identical after catalog drift — `contract_id` intentionally excludes freshness (`contracts.py:671`), so a *changed* freshness verdict is CORRECT when `ReplayFreshness=drifted`. Conflating the two would flag correct drift-detection as instability.

## 10. Gating-note folds (from the 3B.3c whole-branch review)

- **`take_latest` requires a bound ordering column** — thread `temporal.pit_anchor` into `_validate_stage`; a `take_latest`/temporal function is `sound` only when a temporal ordering anchor is BOUND (a real ordering column), not merely a non-null role. Absent → `undeclared` / a temporal reason. (Inert today — registry empty — but sound before the registry is populated.)
- **`AggregationFunction` extension invalidates sign-off** — extending the vocabulary (e.g. to the full spec §4 set) MUST bump the aggregation-rule / registry version, which **invalidates any prior gold-set sign-off** (§9 artifact integrity checks the registry versions). This makes the gate re-open on a vocabulary change by construction.

## 11. Contracts summary

New: `planner_shadow_run_result` + `planner_shadow_plan_observation` (migration 0997) + their reader/writer (`planner/shadow_store.py`); `RunOutcome` enum; `ResolutionCause` + `RESOLUTION_CAUSE_MAP` (+ version); `ReplayFreshness` READER (the enum exists from 3B.3c C1); `planner/contract_eval.py` + `planner/contract_gold.py`; the population report (`planner/shadow_report.py`); the signed enablement report shape. Extended: `CatalogStateStampV1` + `realization_fingerprint`/`projection_checkpoint`; `PlannerReplayEnvelopeV1` + `bridge_fingerprint` (if absent); `run_shadow_planner` writes the store when compiling; `_validate_stage` take_latest ordering guard; `PLAN_CONTRACT_VERSION`/rule-version bumps. Flag reuse: the existing `FEATUREGEN_INTENT_CONTRACT_COMPILE` gates the store write too (persist only when compiling).

## 12. Task decomposition

- **D1 — migration 0997 + store contracts + writer/reader.** The two append-only tables; `RunOutcome`; `planner/shadow_store.py` (`write_run_result`/`write_plan_observations`/readers); append-only, deterministic.
- **D2 — capture-integrity wiring into `run_shadow_planner`.** One `run_result` per eligible recipe (template_not_found → a row); plan observations per candidate; `persistence_partial` + `persistence_loss` on a write failure; behaviour-neutral (store write only when compiling; response untouched).
- **D3 — replay-stamp enrichment + `ReplayFreshness` reader.** Extend `CatalogStateStampV1` (+fingerprint/checkpoint) + envelope bridge fingerprint; thread from the compile-time ctx onto the persisted stamp; the pure `replay_freshness(stored, conn)` reader (`unverifiable` never `current`).
- **D4 — `ResolutionCause` taxonomy + cause-labelling.** The versioned map; `operationally_unmeasured` for an unmapped code; the labeller over the store.
- **D5 — curated gold set + `contract_eval`.** `contract_gold.py` (versioned, hashed, adversarial shapes + expert `resolved`-validity assertions) + `evaluate()` (exact-match + false-resolve check) + the stratified-audit sampler.
- **D6 — population report + the conjunctive gate + signed enablement report.** `shadow_report.py` (selected-candidate denominator; per-dimension + per-cause; recipe-outcome + freshness distributions); the stability-vs-freshness check; the 7-gate evaluator with the statistical bound; the artifact-integrity report.
- **D7 — the two gating-note folds** (take_latest ordering column; AggregationFunction version-bump-invalidates-sign-off).

## 13. Mandatory tests

1. Store round-trip: a compiled run persists exactly one `run_result` per eligible recipe (incl. `template_not_found`, `no_authorized_catalog`, `internal_error`, `budget_exhausted`) + one observation per candidate; denominator = selected only.
2. Capture integrity: a forced store-write failure → `persistence_partial` + `persistence_loss` incremented, never a silent drop; response unchanged.
3. Stamp enrichment: a graph rebuild that does NOT move head_seq → `ReplayFreshness=drifted` (proves the fingerprint, not head_seq, is the guard); a bridge change → drifted; a missing stamp → `unverifiable` (never `current`).
4. Cause-labelling: `safety_rejected`/topology → their legitimate causes (NOT classifier_defect); a reason code with no map entry → `operationally_unmeasured`.
5. Gold set: exact-match verdict+cause on every case; an expert-"invalid" case classified `resolved` → gold FAILURE; a stratified audit samples stored resolved plans.
6. Conjunctive gate: any single sub-gate failing → gate FAILS (no averaging); zero unknown/classifier_defect/operationally_unmeasured required; the statistical-bound sample-size math.
7. Stability vs freshness: unchanged frozen input → identical physical_plan_id/contract_id/declaration_status/declaration-reasons; a drifted catalog → SAME contract_id but `ReplayFreshness=drifted` (NOT flagged as instability).
8. take_latest ordering: `take_latest` with a bound ordering column → sound; without → undeclared/temporal reason.
9. AggregationFunction extension bumps the rule version → prior sign-off invalidated (artifact-integrity check fails).
10. Behaviour-neutral: flag off → no store write, response byte-identical; full `tests/featuregen` green.

## 14. Phase boundaries

```
3B.3a  find source-grain ingredient candidates
3B.3b  build governed physical source→target paths
3B.3c  CLASSIFY each path as a complete contract (compute-only, honest-unresolved)
3B.4   PERSIST + MEASURE + GATE — durable store, replay-freshness, cause-labelled population, curated gold set, conjunctive 3C enablement gate   ← this
3C     ENFORCE (only after the gate passes) — live grounding + review UIs
3D     aggregation/temporal AUTHORING surfaces (move recipes unresolved→resolved) + multi-grain/multi-branch
```
