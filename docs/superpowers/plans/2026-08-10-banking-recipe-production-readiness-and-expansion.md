# Banking Recipe Production Readiness and Expansion — Detailed Implementation Plan

> Planning artifact only. This document does not authorize recipe, API, database, frontend, or production changes by itself.

**Goal:** Turn the current 157-recipe banking idea catalogue into a versioned, atomic, executable and honestly presented recipe product, then expand it with reusable transaction, account, customer, RBWM, CIB and specialist banking packs.

**Baseline reviewed:** origin/main at 58041a59 on 2026-08-10 (re-baselined the same day from afd75a62 — see "Baseline delta" below; the router-quality wave landed seven commits on top of the original baseline, five of which touch files this plan modifies). Re-verified against 58041a59: registry 157 recipes; 14 authored primary objectives; 1,122 total parameter combinations (the original 1,089 was measured at afd75a62 with the same registry — treat all remaining counts in this document as afd75a62-era estimates and let Task 1's audit regenerate every number as the authoritative baseline). Full suite at re-baseline: 10231 passed.

## Baseline delta — what landed between afd75a62 and 58041a59, and what it changes here

The router-quality plan (2026-08-10-template-router-quality.md) completed its engineering in this window. Consequences for this plan, task by task:

- **Tie-break adjudication is LIVE** (`FEATUREGEN_TIE_BREAK_BINDING=1` in the kind deployment): tied bindings are resolved by warmed, content-addressed model verdicts; unadjudicated ties fall back to the deterministic order with the tie honestly recorded (`BindingResolution.AMBIGUOUS` + `tied_candidate_refs`). Task 5 and invariant 5 are amended below to build on this rather than revert it — fail-closed-on-all-ties as originally written would unground ~19 of 53 live bindings and collapse the measured ftr funnel (9→17 suggestions).
- **Hypothesis-chosen parameters shipped** (Task 4b of the router plan, flag `FEATUREGEN_PARAM_CHOICE`, default off): `choose_params` through the governed seam, a `params_by_id` grounding seam on `ground_all_outcomes`, window-bearing feature names, `param_alternatives` on the card, and `semantic_parameter_binding_hash` already participating in candidate identity. Task 3 is recast below as extending this, not introducing it.
- **Alias hygiene + two-tier matching shipped** (router Task 1): a `Need` targeting a successorless retired alias now FAILS import validation; matching is two-tier (exact authored −4 over cross-alias canonical −3), and `Need.alternates` exists. Task 10 is amended below — it may not re-canonicalize a retired alias.
- **The intake build shipped end to end** (migration 1059; `/contract/intake` + confirm gate + Workbench screen): a signed target reading (`contract_intent.target_*`) now exists and feeds the near-label critic and use-case ordering. Orthogonal to this plan's surfaces, but `gate1.py` and `templates.py` diffs in Tasks 3/5/17 must be authored against the current tree.
- **Near-label critic, use-case ordering, selection telemetry shipped** (flags `FEATUREGEN_NEAR_LABEL_CRITIC`, `FEATUREGEN_USE_CASE_ORDERING`; `GET /contracts/selection-telemetry`). The `FeatureIdea` dataclass gained `near_label_verdict`/`near_label_rationale`/`param_alternatives` — contract-v3 field naming must not collide.
- **Migration pool**: 1059 is consumed; the pool is at 1060+. Any migration this plan draws (Task 23's `recipe_review_event`) must append to the D7 reservation table in `docs/architecture/2026-08-01-verified-interfaces-semantic-profiles.md` IN THE SAME COMMIT — the repository's standing discipline, stricter than "allocate at implementation time".

**Architecture:** Introduce Recipe Contract v2 alongside the current Template contract. V2 makes one recipe equal one output, replaces prose-only computation claims with typed formula and operational-policy references, derives PIT text from a typed temporal contract, fails closed on ambiguous operands, and exposes an explicit execution-readiness state through a new suggestion contract version. Existing v1/v2 suggestion clients and persisted feature records remain readable while the 157 legacy templates are migrated family by family. Formula-v1 remains immutable; a versioned Formula-v2 adds the operations the banking library actually requires.

**Technology:** Python 3.12, FastAPI, psycopg3, PostgreSQL, TypedFormula, React, TypeScript, Vitest and pytest.

## Task naming

Tasks in this document are numbered BR-1 through BR-24 (BR = banking recipes). The prefix exists because the concurrently-executing router-quality plan (2026-08-10-template-router-quality.md) has its own Task 1–7 numbering and the two plans touch adjacent surfaces — an unprefixed "Task 7" is ambiguous in any cross-plan discussion, review, or commit message. Internal shorthand like "Tasks 11–17" always means BR-11–BR-17.

## Why this is a program, not a recipe-editing task

The current library has several systemic constraints:

- 126 recipes offer multiple output measures but carry one aggregation and one additivity.
- 153 of 157 recipes are unassessed for formula authoring; only two are Formula-v1 authorable (VERIFIED at re-baseline 58041a59: `RECIPE_FORMULA_EXPECTATIONS` contains exactly `merchant_mcc_diversity` and `obligor_facility_count`).
- 145 recipes have user-facing identity collisions across their parameter space.
- Normal grounding binds the first parameter value rather than surfacing a bounded, explicit variant choice.
- Two recipes have demonstrably incorrect PIT placeholders.
- Ambiguous operand bindings may still become suggestions.
- Thirty-three recipes depend on declared downstream derivations; eleven explicitly admit that the required business concept does not exist.
- Only fourteen recipes have an authored primary objective, covering eight taxonomy leaves.

Adding more prose recipes before repairing these contracts would increase the number of ambiguous and non-executable suggestions. The implementation order below deliberately fixes the platform first, migrates the current content second, and expands the banking library third.

## Non-negotiable invariants

1. **One active recipe produces one atomic output.** A ratio and an amount are separate recipe revisions, even when they share operands.
2. **No readiness by implication.** Conceptual grounding, design validation, formula authorability, materialization readiness and predictive validation are separate states.
3. **Formula-v1 stays immutable.** Formula-v2 is additive and versioned; old hashes and replay envelopes continue to verify.
4. **Suggestion contracts v1 and v2 stay readable.** New readiness and parameter fields ship through contract v3, never as silent optional reinterpretations of v2.
5. **Ambiguity fails closed — with the live adjudication seam recognized.** No deterministic alphabetical tie-break may be represented as a valid operand choice. An ADJUDICATED tie — a warmed, content-addressed model verdict recorded per (template, need, tied-candidate content) through the governed seam, auditable and replayable — IS a valid choice on the discovery surface; this is live behavior today (`FEATUREGEN_TIE_BREAK_BINDING`) and this plan builds on it, never reverts it. An UNADJUDICATED tie fails closed in the V2 executable path: it may not become a formula-authorable or materialization-ready binding.
6. **PIT is compiled, not judged from prose.** Human-readable PIT text is a rendering of the typed temporal policy.
7. **Banking event lifecycle is explicit.** Authorization, booking, value, clearing, settlement, return, reversal and knowledge times are not interchangeable.
8. **Direction, sign, status and currency are governed inputs.** A monetary amount alone is not enough to compute an inflow, payment, exposure, revenue or runoff feature.
9. **Coverage is not inflated.** Primary coverage, supporting relevance and intentional emptiness remain separate metrics.
10. **No predictive-performance claim.** Formula correctness and banking validity do not establish model lift, fairness or suitability.
11. **No destructive migration.** Existing registered features, recipe revisions, suggestion v1/v2 payloads and audit hashes remain readable.
12. **Every production recipe has gold evidence.** Synthetic row-level cases cover ordinary, boundary, null, reversal, multi-currency and late-arriving-data behavior.

## Definition of done

The program is complete only when all of the following are true:

- Every active production recipe uses Recipe Contract v2.
- Every active production recipe has exactly one OutputSpec.
- Every output has a type, additivity, unit or unit policy, currency policy, null policy and denominator policy where applicable.
- Every parameter is classified as semantic, operational or governed-policy selection.
- Every bound parameter is represented in canonical identity and human display.
- No PIT placeholder is hand-authored.
- No ambiguous required binding is emitted as a suggestion.
- Every active recipe has an explicit primary objective and zero or more explicit supporting objectives.
- Every active recipe is classified as executable, blocked by named authority, conceptual-only, or retired; unassessed is forbidden.
- Every executable recipe has a reviewed formula expectation and gold cases.
- The suggestion UI distinguishes conceptual, governance-blocked, formula-validated and materialization-ready states.
- The coverage report has zero active recipes relying on legacy applicability inference.
- The legacy debt ratchet reaches zero.
- New transaction, account, customer/RBWM and CIB foundation packs meet the same gates before release.

## Review-finding traceability

| Review finding | Primary remediation |
|---|---|
| Multi-measure recipes with one output contract | Tasks 2, 11–17 |
| Parameter and display identity collisions | Task 3 |
| First-parameter-only grounding | Task 3 |
| 153 formula-unassessed recipes | Tasks 7, 11–17 |
| Two PIT placeholder defects | Task 4 |
| Prose-based PIT completeness | Task 4 |
| Ambiguous bindings still surface | Task 5 |
| Generic stock/flow operands bind the wrong banking quantity | Tasks 5, 10–17 |
| Missing status, direction, reversal and currency semantics | Tasks 5, 10, 18 |
| Downstream derivations presented like available computations | Tasks 7, 11–17 |
| Supporting tags inflate coverage | Task 9 |
| UI cannot communicate real execution readiness | Task 8 |
| Retail, credit, fraud, AML, collections, ALM, payments and specialist defects | Tasks 11–17 |
| Missing transaction/account/customer/RBWM/CIB packs | Tasks 18–21 |
| Existing tests miss banking semantics | Tasks 1, 22 |

## Delivery sequence

| Release increment | Scope | Exit gate |
|---|---|---|
| R0 | Audit, lint and debt ratchet | New recipe debt cannot increase |
| R1 | Recipe Contract v2, identity, temporal and binding correctness — INCLUDING the BR-23 review-event SCHEMA (types + append-only store + its D7-reserved migration, not the decision APIs) | Contract and grounding invariants pass; family-migration PRs can carry review records from day one |
| R2 | Formula-v2 and execution-readiness classification | Formula capability is versioned and fail-closed |
| R3 | Suggestion contract v3 and UI truthfulness | No conceptual suggestion looks executable |
| R4 | Migrate and correct the existing 157 recipes | Legacy debt is zero |
| R5 | Transaction and account foundation | First reusable atomic banking pack is executable |
| R6 | Customer/RBWM and CIB expansion | Priority business packs meet production gates |
| R7 | Specialist expansion and rollout | Shadow evidence, SME sign-off and operational SLOs pass |

---

## Task BR-1: Add the registry audit, debt baseline and CI ratchet

**Purpose:** Make every identified defect machine-countable before changing behavior. The first delivery prevents new debt and proves later tasks reduce it.

**Files:**

- Create: src/featuregen/overlay/upload/recipe_audit.py
- Create: src/featuregen/overlay/upload/recipe_audit_cli.py
- Create: tests/featuregen/overlay/upload/test_recipe_audit.py
- Create: docs/architecture/banking-recipe-debt-baseline.json
- Modify: pyproject.toml

**Audit checks:**

- recipe count and family count;
- output-measure count per recipe;
- parameter-combination count and distinct rendered identity count;
- PIT parameter references versus declared parameter names;
- explicit primary and supporting objectives;
- formula-authoring class;
- authored versus inferred source grain, join role and temporal role;
- unresolved or ambiguous source-entity role;
- declared downstream derivations;
- missing concepts explicitly admitted in notes or degrade text;
- monetary-flow recipes without direction/sign authority;
- monetary recipes without unit/currency policy;
- ambiguous binding disposition;
- formula expectation and gold-corpus coverage;
- legacy Template versus RecipeDefinitionV2 population.

**Steps:**

- [ ] Add a pure audit report over ALL_TEMPLATES and the future V2 registry.
- [ ] Record the reviewed baseline counts in banking-recipe-debt-baseline.json.
- [ ] Add a ratchet test that fails if any count worsens, while permitting intentional decreases.
- [ ] Add strict mode that requires every debt count to equal zero; strict mode remains off until Task 17.
- [ ] Add JSON and human-readable CLI output.
- [ ] Make the report identify exact recipe IDs, parameter names and conflicting output properties rather than only counts.
- [ ] Pin the two known PIT defects as failing examples.
- [ ] Pin at least one multi-measure/additivity conflict, one display collision and one ambiguous binding.

**Acceptance: DONE 2026-08-10.** The audit reproduces the reviewed numbers exactly where they were measured (157 recipes / 126 multi-measure / 145 identity collisions / 1,122 combinations / 33 downstream derivations / 14 primaries / 2 authorable) and supersedes the estimates with measured definitions ("eleven missing-concept admissions" → 15 under the documented regex; additivity conflicts → 72; the two PIT defects → EXACTLY the two known ids, caught mechanically as placeholders naming undeclared parameters). A new multi-measure legacy recipe fails the ratchet twice (multi_measure + legacy-not-in-V2); an unmatched PIT placeholder fails it; intentional decreases pass; a counter dropped from the baseline is itself a regression; strict mode works today and stays unwired until BR-17. The ambiguous-binding disposition is catalog-dependent by design — `dynamic_binding_audit` (proven on a tied fixture pinning dormancy_days) is the operator's per-catalog lens, outside static CI. Zero behavior changes; the audit is pure and read-only.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_recipe_audit.py -v
- uv run python -m featuregen.overlay.upload.recipe_audit_cli --format json

**Commit:** test(recipes): add the production-readiness debt audit and CI ratchet

---

## Task BR-2: Introduce Recipe Contract v2 with one atomic output

**Purpose:** Establish a schema capable of expressing banking computation honestly without editing all 157 constructors in one risky change.

**Files:**

- Create: src/featuregen/overlay/upload/recipe_contract_v2.py
- Create: src/featuregen/overlay/upload/recipe_registry_v2.py
- Create: src/featuregen/overlay/upload/recipe_legacy_adapter.py
- Create: tests/featuregen/overlay/upload/test_recipe_contract_v2.py
- Create: tests/featuregen/overlay/upload/test_recipe_grounding_context.py
- Modify: src/featuregen/overlay/upload/recipe_grounding_context.py

**Core types:**

- RecipeDefinitionV2
- OperandSpecV2
- OutputSpecV2
- ParameterSpecV2
- TemporalSpecV2
- EligibilitySpecV2
- LeakageSpecV2
- FormulaReferenceV2
- RecipeReviewV1
- RecipeReadiness

**Required RecipeDefinitionV2 fields:**

- stable recipe_id and revision schema version;
- family and one primary objective;
- explicit supporting objectives;
- business definition and intended decision context;
- computation kind: deterministic_formula, governed_model_output or conceptual_pattern;
- one OutputSpecV2;
- typed operand tuple;
- explicit source grain and output grain;
- typed temporal specification;
- immutable parameter tuple;
- eligibility and exclusion policies;
- leakage classification by permitted use-case stage;
- formula reference or explicit conceptual-only reason;
- SME review metadata;
- replacement and legacy aliases.

**Required OutputSpecV2 fields:**

- output_id and display label;
- logical data type;
- additivity;
- unit kind and unit policy;
- currency policy;
- null-input policy;
- empty-population policy;
- zero-denominator policy where applicable;
- valid range or constraint where meaningful;
- formula result scale/rounding;
- aggregation-over-entity and aggregation-over-time rules.

**Required OperandSpecV2 fields:**

- role and expected concept;
- required/optional;
- operand class;
- allowed source grains;
- join role;
- temporal role;
- distinct-binding group;
- unit and currency expectations;
- sign/direction expectation;
- status/lifecycle policy reference;
- relationship/cardinality requirement;
- authority level required for suggestion and for execution.

**Parameter classes:**

- semantic: changes the meaning and therefore the output identity;
- operational: changes execution behavior while preserving the output definition, such as a selected window;
- governed_policy: references a reviewed threshold, status set, exchange-rate or eligibility policy and may not be a free literal.

**Compatibility design:**

- Keep Template and ALL_TEMPLATES unchanged during R1.
- Project a legacy Template through recipe_legacy_adapter as conceptual-only unless an explicit reviewed V2 definition exists.
- Never infer an executable formula from legacy prose.
- Preserve canonical-recipe-v1 hashing for old contexts.
- Add canonical-recipe-v2 hashing for V2 definitions.
- A V2 replacement names legacy recipe IDs and names explicitly; there is no heuristic aliasing.
- Make all collection fields deeply immutable. Do not place a mutable dictionary inside a frozen dataclass.
- ROUTING RULE for in-flight SME content (re-baseline): the router plan's twelve 4c triage cards (2026-08-10-recipe-triage-4c.md, drafted and awaiting SME second-review) are authored DIRECTLY as RecipeDefinitionV2 once this task lands — never as new legacy Templates, which BR-17 makes a CI failure. If SME sign-off arrives before this task ships, the cards wait; content is cheaper to hold than to migrate twice.

**Steps:**

- [ ] Write constructor and validation tests before adding the registry.
- [ ] Reject more than one output.
- [ ] Reject a missing primary objective.
- [ ] Reject output additivity incompatible with its declared formula result.
- [ ] Reject a monetary output without a currency policy.
- [ ] Reject a ratio without zero-denominator behavior.
- [ ] Reject a parameter that has no class or no identity projection.
- [ ] Reject an executable recipe without an exact formula reference.
- [ ] Reject an empty allowed-source-grain set for executable operands.
- [ ] Implement canonical V2 serialization and hashing with exhaustiveness tests.
- [ ] Implement the legacy adapter and prove it always returns conceptual-only.
- [ ] Register one non-production probe recipe to prove end-to-end serialization; do not migrate a banking recipe yet.

**Acceptance: DONE 2026-08-10.** All ten core types shipped, frozen + tuple-only, validated at CONSTRUCTION (an invalid definition cannot exist long enough to serialize). The multi-output ambiguity is unconstructible twice over: one structural OutputSpecV2 AND the `measure`-parameter side door rejected by name. UNASSESSED does not exist in the V2 readiness vocabulary — it lives only on the adapter's LegacyRecipeProjectionV1, which also has NO formula field to fill (prose structurally cannot become executable). All 157 legacy templates proven to project conceptual-only; grounding untouched (full suite green). canonical-recipe-v1 is now PINNED by literal hash (the Need.alternates addition changed v1 hashes silently — the pin makes the next change loud and deliberate); canonical-recipe-v2 is fields()-driven so every field of every nested spec is hash-bearing by construction, proven by a recursive walk plus eight representative edits. Registry law at import: unique ids, explicit replacement only, no legacy-id squatting; `v2_replaced_legacy_ids` pipes straight into the BR-1 audit (proven: one replacement drops the migration counter to 156). The non-production probe (`v2_probe_posted_debit_amount`, the BR-18 exemplar's shape) exercises every nested spec end to end. NOTE for BR-3/BR-4/BR-5: the probe's FORMULA_BLOCKED readiness and the placeholder expectation ref are deliberate — expectation-registry membership is BR-7's readiness fold, not a construction rule.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_recipe_contract_v2.py tests/featuregen/overlay/upload/test_recipe_grounding_context.py -v

**Commit:** feat(recipes): introduce the atomic Recipe Contract v2

---

## Task BR-3: Correct candidate identity, naming and bounded parameter selection

**Purpose:** Ensure two different semantic or operational choices never look like the same feature, without generating an unbounded Cartesian product.

**Shipped baseline this task EXTENDS (re-baseline note):** router-plan Task 4b (38119e26) already delivers the push half — hypothesis-chosen parameters through the governed seam (`param_choice.py`: closed selection from the authored tuples, per-template content-addressed replay with abstains stored), an additive `params_by_id` seam on `ground_all_outcomes` re-guarded by `_bind_params`, window-bearing feature NAMES, the untaken alternatives named on the card (`param_alternatives`), and `semantic_parameter_binding_hash` already inside candidate identity — behind `FEATUREGEN_PARAM_CHOICE` (default off). This task adds the PULL half (user-driven bounded variant selection) and the classification model; both halves MUST flow through the same `params_by_id` grounding seam and the same parameterisation-bearing identity. Do not introduce a second identity or a second override channel.

**Files:**

- Modify: src/featuregen/overlay/upload/suggestion_identity.py
- Modify: src/featuregen/overlay/upload/recipe_grounding_context.py
- Modify: src/featuregen/overlay/upload/templates.py
- Create: src/featuregen/overlay/upload/recipe_variants.py
- Test: tests/featuregen/overlay/upload/test_suggestion_identity.py
- Create: tests/featuregen/overlay/upload/test_recipe_variants.py

**Identity rules:**

- Canonical candidate identity contains recipe revision, output_id, every bound parameter, ordered role binding, grain, time anchor and relationship-path assignment.
- Human display contains output meaning plus every meaning-bearing parameter.
- Windows use canonical suffixes such as 30d or 60min.
- Thresholds reference a governed policy label, not an unexplained number.
- A semantic parameter cannot be hidden in a generic aggregation label.
- Legacy suggestion v1/v2 identities remain untouched; corrected identities are emitted only in contract v3.

**Variant behavior:**

- Do not ground every parameter combination during a table page load.
- Ground one reviewed default variant and return the bounded allowed parameter schema.
- Permit an explicit read-only variant request with exact validated parameter bindings.
- Bound allowed values and reject unknown parameters.
- Do not accept arbitrary thresholds or formulas from the browser.
- A selected variant receives its own deterministic suggestion identity and formula expectation binding.

**Steps:**

- [ ] Add collision tests for window_min, horizon_days, measure, threshold, baseline and match-policy parameters.
- [ ] Add a display-name golden test for each parameter class.
- [ ] Introduce ParameterSelectionV2 and validate selections against ParameterSpecV2.
- [ ] Add a bounded variant resolver that never enumerates the full Cartesian product.
- [ ] Carry the selection into RecipeGroundingContextV2.
- [ ] Retain the current first-default behavior only through the legacy adapter.
- [ ] Add an audit check proving all V2 variants have distinct canonical identities.
- [ ] Define replacement aliases for any migrated recipe whose display name changes.

**Acceptance: DONE 2026-08-10** (V2-side; legacy identities untouched by construction). `recipe_variants.py`: `resolve_variant` (one validated selection at a time; default = first-allowed, the same rule the push half degrades to; unknown params and off-menu values REFUSED; the returned value is always the AUTHORED object), `parameter_schema` (bounded by parameter count — pages return the schema, never the product), `variant_identity` (recipe REVISION hash + output_id + every bound parameter), `enumerate_variant_identities` (AUDIT-ONLY, capped). A governed threshold selects its reviewed policy LABEL — a browser literal is refused. `suggestion_id_v3` registered beside the untouched v2 contract (same owner; emitted only via contract v3/BR-8); proven: same bindings + different selection = different v3 candidate, while the legacy id stays byte-stable. The audit gains ratcheted `v2_variant_identity_collision_recipes` (baseline 0, regenerated same-commit). Collision battery covers window_min / horizon_days / threshold / baseline / match_policy (`measure` is unconstructible since BR-2); a 30-day and 90-day instance of one output are two identities and two display names. DEVIATIONS, both deliberate: `templates.py` needed NO change (the Task-4b `params_by_id` seam already exists — one seam, two callers); "carry the selection into RecipeGroundingContextV2" waits for BR-17's registry cutover (the selection rides `ResolvedRecipeVariantV1` until V2 recipes ground). Replacement display aliases land per-family in BR-11..16.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_suggestion_identity.py tests/featuregen/overlay/upload/test_recipe_variants.py -v

**Commit:** fix(recipes): make every bound recipe parameter identity-bearing and selectable

---

## Task BR-4: Replace prose PIT declarations with a typed temporal contract

**Purpose:** Remove placeholder drift and distinguish banking event time, business effective time, processing time and knowledge time.

**Files:**

- Create: src/featuregen/overlay/upload/recipe_temporal_v2.py
- Modify: src/featuregen/overlay/upload/planner/declarations.py
- Modify: src/featuregen/overlay/upload/taxonomy/ranking_signals.py
- Modify: src/featuregen/overlay/upload/suggestion_contract.py
- Test: tests/featuregen/overlay/upload/test_recipe_temporal_v2.py
- Test: tests/featuregen/overlay/upload/taxonomy/test_ranking_signals.py
- Test: tests/featuregen/overlay/upload/planner/test_declarations.py

**Temporal model:**

- anchor kind: event, as_of, effective interval, contractual future date or pre-decision;
- event timestamp role;
- business-effective timestamp role;
- ingestion/knowledge timestamp role where late arrival matters;
- window basis, unit and boundaries;
- timezone and business calendar;
- observation cutoff inclusivity;
- future-horizon policy for contractual maturity inputs;
- snapshot selection policy such as latest-known-at-cutoff;
- late-arrival and backfill behavior;
- required external temporal-policy authority.

**Steps:**

- [ ] Implement TemporalSpecV2 validation and compiler output.
- [ ] Render human PIT text from the compiled object; remove author-edited placeholders from V2.
- [ ] Make PIT completeness consume compiler status and governed bindings, never keyword markers.
- [ ] Add distinct support for fraud pre-authorization state, booking/value/settlement event windows, as-of snapshots and contractual future horizons.
- [ ] Require knowledge-time semantics for any source that can be corrected or arrive late.
- [ ] Fix merchant_mcc_diversity by using a day-based trailing observation window and non-real-time wording unless a genuine pre-decision feed is bound.
- [ ] Fix maturity_ladder_runoff by binding horizon_days to a future contractual-maturity interval.
- [ ] Give obligor_facility_count its own trailing facility-activity temporal declaration rather than reusing prose about latest exposure, limit, covenant and utilization state.
- [ ] Add a mismatch test proving an undeclared temporal parameter cannot compile.
- [ ] Keep the legacy PIT string on v1/v2 payloads for compatibility; v3 exposes the rendered typed declaration and structured temporal fields.

**Acceptance: DONE 2026-08-10.** `recipe_temporal_v2.compile_temporal`: PIT text is a pure rendering of TemporalSpecV2 + the resolved selection — an unresolved placeholder is impossible (the renderer asserts none survive), and a hole is a NAMED blocker from a closed vocabulary (window_unbound / event_role_unbound / snapshot_policy_missing / pre_decision_authority_unproven / pre_decision_not_minute_grained / knowledge_time_missing), never worse prose: a blocked contract renders NOTHING. The four time shapes render distinctly (a forward ladder says "(cutoff, cutoff + 90d] — never a trailing observation window"); real-time wording is EARNED (governed pre-decision feed authority + minute grain, the merchant_mcc_diversity rule); a correctable source must declare knowledge time + late-arrival behavior. `pit_completeness_v2` consumes ONLY the compiler verdict — COMPLETE is structurally unreachable while any blocker exists; the keyword-marker path survives untouched for the legacy registry and dies with it at BR-17. The three registry fixes landed and revealed the defects' true anatomy: each was a SHARED pit constant borrowed by a recipe with different parameters (merchant_mcc_diversity borrowed the minute-grained fraud constant; maturity_ladder_runoff and obligor_facility_count borrowed state-lookback constants); each now carries its own declaration and the audit's placeholder counter is ZERO (baseline regenerated same-commit; BR-1's pin test became the zero-guard). DEVIATIONS: `planner/declarations.py` untouched (its pit_anchor derives from need metadata, which V2 operand temporal roles feed only at BR-17's cutover); `suggestion_contract.py` untouched (v1/v2 payloads keep the legacy string by NOT changing anything; the compiled declaration is exposed by contract v3 in BR-8).

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_recipe_temporal_v2.py tests/featuregen/overlay/upload/taxonomy/test_ranking_signals.py tests/featuregen/overlay/upload/planner/test_declarations.py -v

**Commit:** fix(recipes): compile point-in-time semantics from typed banking time roles

---

## Task BR-5: Fail closed on ambiguous or semantically incompatible operands

**Purpose:** Stop deterministic tie-breaking from turning uncertainty into a plausible-looking feature.

**Files:**

- Modify: src/featuregen/overlay/upload/templates.py
- Modify: src/featuregen/overlay/upload/need_metadata.py
- Modify: src/featuregen/overlay/upload/contract/gate1.py
- Modify: src/featuregen/overlay/upload/recipe_formula_authority.py
- Create: src/featuregen/overlay/upload/recipe_operand_policy.py
- Test: tests/featuregen/overlay/upload/test_templates.py
- Test: tests/featuregen/overlay/upload/test_need_metadata.py
- Create: tests/featuregen/overlay/upload/test_recipe_operand_policy.py

**Required behavior (amended at re-baseline to compose with the live tie-break architecture):**

- In the V2 EXECUTABLE path: a tied required operand with NO adjudicated verdict produces AMBIGUOUS_BINDING and no selected column. A tie resolved by a warmed verdict (the Task-2 seam in `tie_break.py` — content-addressed on the tied candidates' own enrichment text) is a valid, auditable binding and carries its verdict reference.
- The legacy discovery surface keeps today's live behavior byte-for-byte: verdict-bound where adjudicated, deterministic order where not, the tie always recorded (`tied_candidate_refs`). Reverting it would unground ~19 of 53 live bindings.
- Do NOT build a second tie mechanism: V2 consumes the SAME verdict store, and "adjudicated" means exactly `find_tie_break_verdict` returning a ranking valid for the tied set.
- An optional tied operand remains unresolved; it is never silently selected.
- A V2 executable recipe must author allowed source grains, join role and temporal role.
- Entity compatibility must match governed entity semantics.
- Generic monetary_stock and monetary_flow concepts require an additional economic-role constraint.
- Opposing legs such as income/expense or debit/credit require distinct physical bindings or governed sign authority.
- Cross-table operands require a governed path and acceptable directional cardinality.
- Unit and currency compatibility are evaluated per operand and output.

**Operational authority checks:**

- direction/sign convention;
- transaction lifecycle status set;
- reversal and correction treatment;
- fixed versus per-row currency;
- currency-conversion policy and rate timestamp;
- snapshot uniqueness and effective dating;
- active-record definition;
- facility/account/product lifecycle;
- relationship cardinality and allocation policy.

**Steps:**

- [ ] Change required tie handling in the V2 EXECUTABLE path to return an unbuildable outcome when no adjudicated verdict exists; consume the existing warmed-verdict store for the adjudicated case. Leave the legacy discovery path's live behavior unchanged (its own flag governs it).
- [ ] Add reason codes for ambiguous entity, measure, time, status, currency and relationship bindings.
- [ ] Add an explicit economic_role field or policy reference to OperandSpecV2.
- [ ] Extend authority envelopes to cover status, sign, currency, temporal and lifecycle decisions.
- [ ] Ensure formula and suggestion paths consume the same binding verdict.
- [ ] Prove an ambiguous generic balance cannot become DESIGN_CHECKED.
- [ ] Prove a deposit balance cannot satisfy a drawn-credit-exposure role merely because both are monetary_stock.
- [ ] Prove beneficiary bank cannot satisfy beneficiary/payee identity.
- [ ] Prove settlement status cannot satisfy authorization outcome.
- [ ] Add explicit user/governance resolution information to the rejection payload.

**Acceptance: DONE 2026-08-10** (V2 path; the legacy discovery surface untouched byte-for-byte per the amendment — its own flag governs it). `recipe_operand_policy.bind_v2_operands`: the ONE binding verdict both the formula and suggestion paths consume, over the SAME two-tier matcher grounding uses and the SAME tie-break verdict store the live surface reads (no second matcher, no second tie mechanism — the amendment's whole point, proven by storing a verdict through the live machinery under the V2 key `v2:{recipe_id}` and reading it back). An unadjudicated required tie is AMBIGUOUS with NO selected column, the tied candidates named, the resolution path stated ("adjudicate at ingest warming, or narrow the operand"); an adjudicated tie binds carrying the deliberation's content-addressed reference; an optional tie stays unbound, never silently selected; a missing required operand is visible with REQUIRED_OPERAND_MISSING. The semantic half: `OperandSpecV2.economic_role` (additive, hash-bearing) binds ONLY over human-confirmed `economic_role` field-evidence — the deposit-balance-as-drawn-exposure class is closed because NOTHING satisfies the role without evidence, and a wrongly-confirmed role blocks with the column's actual role named; opposing legs (one distinct_binding_group) refuse one physical column without a governed sign authority; a concept mismatch never binds structurally. `build_formula_authority_envelope` gains additive `required_economic_roles` rejecting ECONOMIC_ROLE_UNPROVEN over the same evidence — one verdict source, proven both refused and satisfied. Closed reason codes per operand class (AMBIGUOUS_ENTITY/MEASURE/TIME/STATUS/RELATIONSHIP_BINDING) feed BR-7's fold and BR-8's blocker groups. DEVIATIONS: `templates.py`/`gate1.py`/`need_metadata.py` untouched (legacy path byte-identical per the amendment; V2 operands author their own join/temporal roles, consumed at BR-17); the named beneficiary-bank / settlement-vs-authorization proofs use existing distinct concepts for the CLASS — the named concepts arrive with BR-10 and inherit the structural guarantee.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_templates.py tests/featuregen/overlay/upload/test_need_metadata.py tests/featuregen/overlay/upload/test_recipe_operand_policy.py tests/featuregen/overlay/upload/test_recipe_formula_authority.py -v

**Commit:** fix(recipes): reject ambiguous and banking-incompatible operand bindings

---

## Task BR-6: Add Formula-v2 without changing Formula-v1 identity

**Purpose:** Support the computations required by the banking catalogue while preserving every Formula-v1 hash, replay envelope and materialized artifact.

**Files:**

- Create: src/featuregen/formula/schema_v2.py
- Create: src/featuregen/formula/proposal_v2.schema.json
- Create: src/featuregen/formula/canonical_v2.py
- Create: src/featuregen/formula/parse_v2.py
- Create: src/featuregen/formula/capability_v2.py
- Create: src/featuregen/formula/operations_v2.py
- Create: src/featuregen/formula/output_authority_v2.py
- Modify: src/featuregen/formula/__init__.py
- Create: tests/featuregen/formula/test_schema_v2.py
- Create: tests/featuregen/formula/test_canonical_v2.py
- Create: tests/featuregen/formula/test_capability_v2.py
- Create: tests/featuregen/formula/gold_v2/

**Minimum Formula-v2 capability:**

- existing sum, row count, non-null count, distinct count, ratio and difference;
- minimum, maximum and average;
- first-known and last-known value at cutoff;
- lag, delta and date difference;
- recency;
- standard deviation and z-score;
- percentile and median;
- OLS slope;
- consecutive-run/streak;
- conditional expressions and flags;
- HHI/top-share concentration;
- future contractual-date inclusion;
- effective-dated lookup;
- explicit status, direction and reversal filters;
- currency conversion through a governed rate policy;
- account-to-customer and facility-to-obligor rollups through an allocation policy;
- multi-expression formulas over one governed relationship plan.

**Capability boundaries:**

- A computation outside Formula-v2 is conceptual-only or blocked; it is never approximated by prose.
- Cross-source formulas remain unsupported until a separate governed multisource capability is accepted.
- Model predictions, CLV projections, anomaly models and propensities are not ordinary deterministic formulas. They require a ModelFeatureSpec and model-governance workflow.
- Formula-v2 authoring is offline and versioned; execution engines must advertise supported operations.

**Steps:**

- [ ] Freeze all existing Formula-v1 gold hashes before adding imports.
- [ ] Add V2 structural schema and canonicalization with separate version pins.
- [ ] Add operations in small increments, each with positive, invalid and unsupported gold cases.
- [ ] Add version dispatch that parses v1 and v2 explicitly; never infer a version from body shape.
- [ ] Add output additivity and unit compatibility validation.
- [ ] Add execution-capability negotiation so an authorable formula is not assumed materializable on every engine.
- [ ] Extend replay and audit envelopes with the explicit formula schema version.
- [ ] Add compiler tests for SQL or target execution artifacts where the repository already supports materialization.
- [ ] Prove every existing Formula-v1 fixture remains byte-identical.

**Acceptance:**

- Formula-v1 canonical and replay tests are unchanged.
- Formula-v2 can exactly express the first transaction/account foundation tranche.
- Unsupported complex model features are classified, not approximated.
- Every operation has deterministic canonicalization and gold cases.

**Verification:**

- uv run pytest tests/featuregen/formula -v

**Commit series:** feat(formula-v2): add versioned banking operations in reviewable increments

**INCREMENT 1 DONE 2026-08-10** — the foundation: (a) the v1 FREEZE as a literal manifest test (every v1 gold fixture's pinned hash frozen in `test_schema_v2`; moving one v1 byte fails before review — step 1, mechanized); (b) `schema_v2.py` — v2-OWN body/expression dataclasses (a shared body type would let v2 evolution move v1 identity) importing only the frozen structural LEAVES (filters/windows/grains/params); vocabulary = v1's four + the first NEW group **min/max/avg** (v1's own gold corpus pins `avg` as unsupported — it graduates here) with `AGGREGATE_ADDITIVITY_V2`; (c) `proposal_v2.schema.json` + `parse_v2.py` with `parse_versioned` — the declared `formula_schema_version` field decides, NOTHING else: absent or unknown versions refuse loudly, a body shape is never sniffed, version pins are validated AS DATA; (d) `canonical_v2.py` — own JCS pipeline, fields()-driven (auto hash-bearing), version triple inside the hashed body so v1/v2 forms cannot collide; (e) `capability_v2.py` — grammar capability (single-source rule carried over) DISTINCT from engine negotiation: `EngineCapabilityV1` advertises supported operations and an authorable-but-unrunnable formula is `unsupported_engine` (the MATERIALIZATION_BLOCKED input), never conflated with a grammar gap; (f) `gold_v2/` — six fixtures pinning canonical bytes + sha256 under the v1 gate's own no-self-refresh discipline (ok×4, invalid, cross-source-unsupported). **INCREMENT 2 DONE 2026-08-10** — the distributional group: recency, stddev, percentile, median (all NON_ADDITIVE), plus the `aggregation_argument` mechanism — REQUIRED for percentile (p strictly inside (0,100)), FORBIDDEN elsewhere: a parameterized aggregate is declared, never smuggled into a label, and the argument is identity-bearing (p95 and p99 are two hashes). The four increment-1 fixtures were re-keyed in the same reviewed commit (the argument field joined the fields()-driven canonical form; v2 is pre-consumer, v1's freeze manifest untouched). Engine advertisement proven explicit: an engine that never advertised percentile refuses it even with a full basic vocabulary. Gold corpus now 12 fixtures (8 ok, 2 invalid, 1 unsupported, 1 argument-smuggling). **INCREMENT 3 DONE 2026-08-10** — the at-cutoff group: `last_known` / `first_known` (SEMI-additive, exactly like the balances they read — sums across accounts, never across time) and `zscore` (the standardized latest value over ONE window and operand — a single honest aggregate, not a three-formula composite; dimensionless), all event-time-ordered. `operations_v2.py` created as THE per-operation rule table (operand/argument/additivity/result-kind/order-sensitivity), TOTAL over the enum by test; the schema checker and the additivity view now consult it instead of special-casing. NO re-key this time — proven before writing: prior fixtures byte-stable (no new fields). Gold corpus 15. **INCREMENT 4 DONE 2026-08-10** — the offset fork: `WindowPolicyV2` (+`offset_periods` ∈ [0,12]; a negative offset is a future window in disguise, refused) makes LAG a body composition (the same aggregate at offset 1) and DELTA a difference body over offsets 0 and 1 — zero new operations for both, one identity-bearing field. `date_diff_avg` (row-level date arithmetic aggregated; the BR-12 payment-behaviour shape) brings the `second_operand` mechanism — required by rule-table row, forbidden elsewhere, same-table contained. Window offsets are an ENGINE capability too (`supports_window_offset`, default False — an engine that can aggregate but cannot shift windows honestly refuses a lag formula). Reviewed re-key (both fields joined the canonical form; v1 manifest untouched). Gold corpus 20 (14 ok). ALSO: the 1060 review store's event ordering flaked the day it shipped (same-transaction events share now(); same-millisecond ULIDs are not monotonic — "current" could resolve to a superseded event); migration 1061 adds `recorded_seq` BIGSERIAL and reads order by it — proven stable over 10 consecutive runs. **INCREMENT 5 DONE 2026-08-10** — trend and condition: `slope` (OLS over event time, RATE result — the balance_trend/txn_frequency_trend shape; a trend needs a quantity, operand required), `streak_periods` (longest consecutive run of window-unit periods with a qualifying row — salary_regularity's shape) and `any_match` (the honest boolean flag). The FILTER is the condition for both condition ops — no operand, no new structure, no re-key (byte-stability proven before writing). RESULT_KINDS += rate, flag. Gold corpus 24 (17 ok). **INCREMENT 6 DONE 2026-08-10** — concentration: `hhi` and `top_share`, where the operand is the GROUPING dimension and the second operand the OPTIONAL weighting measure (absent = row-count shares — a different feature with a different identity, honestly so; proven by hash). The rule vocabulary gains `optional` for second operands, scoped: `sum` still refuses one. Gold corpus 26 (19 ok); no re-key. **INCREMENT 7 DONE 2026-08-10** — the future horizon: `WindowBasisV2` adds `future_horizon` — (cutoff, cutoff+L] over contract terms knowable AT the cutoff (BR-4's contractual_future anchor's formula-side twin, the maturity_ladder_runoff shape), and with an offset it is the LADDER BUCKET: offset k reads (cutoff+kL, cutoff+(k+1)L] — the maturity ladder is the lag composition pointed forward. The semantic guard: ORDER-SENSITIVE operations refuse a future basis — they read observed history, and a future horizon has none (last_known and slope both proven refused). Forward horizons are an engine capability (`supports_future_horizon`). No re-key (the enum fork preserves string values — proven before writing). Gold corpus 29 (21 ok). **INCREMENT 8 DONE 2026-08-10** — the governed authority carriage: `AuthorityRefsV2` per expression (status / direction / reversal / currency-conversion policy references — never vacuous: an all-blank block refuses with "omit the block instead") and `allocation_policy_ref` per proposal (the joint-account / facility→obligor rollup rule). All IDENTITY-BEARING: the same computation with and without a reversal policy is two formulas, proven by hash. THE BR-18 CANONICAL EXEMPLAR (posted_debit_amount) is now fully expressible end to end — every policy its contract names, declared — plus the account→customer rollup variant carrying its allocation. Reviewed re-key #3 (both fields joined the canonical form). Resolution of each ref against its governed store (and refusing a monetary sum whose source needs conversion but declares none) is output authority's and BR-7's job — the schema's job is that the declaration exists. Gold corpus 32 (23 ok). **BR-6 COMPLETE 2026-08-10** (final increment): `output_authority_v2.resolve_output_v2` — the declarations get their teeth: a per-row-currency monetary operand in an expression declaring no conversion policy REFUSES (CURRENCY_CONVERSION_UNDECLARED — the 63-recipe audit class as a refusal, not a count); mixed-unit composites refuse (MIXED_UNITS); additivity FOLDS to the weakest term (a signed sum of date-diffs is non-additive whatever its author hoped; a delta of additive sums stays additive); result kinds type the output (flag→boolean, duration→days, count→integer); facts are INJECTED for purity and wire to the same governed `read_operational_value` facts at cutover — no second authority. Envelope threading verified PRESENT (replay records + the authoring manifest carry `formula_schema_version` from the proposal); the missing enforcement added where materialization actually exists: admission check 4b (`FORMULA_SCHEMA_UNSUPPORTED`) — the compiler consumes exactly v1, and any other declared version refuses rather than being silently compiled under v1 semantics; lifting it is an ENGINE capability decision. Fixture-driven SQL-artifact compiler tests for v2 bodies arrive with the engine that advertises them (BR-18/BR-24).

**INCREMENT 9 DONE 2026-08-10** — the operation groups CLOSE: `effective_at_cutoff` (the row whose [valid_from, valid_to) contains the cutoff, latest valid_from winning — valid_from is the window's event clock, valid_to the required second operand; without it state never ends and the lookup refuses; SEMI-additive like last_known) and the multi-expression MINIMUM: `signed_sum` — ≥2 uniquely-named ±1 terms (DSO + DIO − DPO, the working-capital cycle). Weights are deliberately not a thing; the plan's own risk table warns against an unbounded DSL, and anything richer stays honestly unsupported until a reviewed increment. No re-key (new fields live only on the new body type). Gold corpus 36 (25 ok). 21 operations, 4 body shapes. REMAINING: `operations_v2.py` + `output_authority_v2.py`; replay/audit envelope version threading; compiler tests.

---

## Task BR-7: Make execution readiness explicit and complete the formula evidence path

**Purpose:** Replace unassessed with a closed, audited readiness vocabulary and prevent conceptual recipes from appearing executable.

**Files:**

- Modify: src/featuregen/overlay/upload/recipe_formula_contracts.py
- Modify: src/featuregen/overlay/upload/recipe_formula_expectations.py
- Modify: src/featuregen/overlay/upload/recipe_formula_gate.py
- Modify: src/featuregen/overlay/upload/recipe_formula_gold.py
- Modify: src/featuregen/overlay/upload/recipe_formula_eval.py
- Modify: src/featuregen/overlay/upload/recipe_formula_shadow.py
- Create: src/featuregen/overlay/upload/recipe_readiness.py
- Test: tests/featuregen/overlay/upload/test_recipe_readiness.py
- Test: existing recipe_formula tests

**Closed readiness vocabulary:**

- CONCEPTUAL_ONLY: useful SME pattern, exact deterministic computation not available;
- FORMULA_BLOCKED: exact formula exists but named temporal, sign, unit, currency, join or type authority is unresolved;
- FORMULA_AUTHORABLE: exact expectation is in the reviewed registry and the engine can author it;
- FORMULA_VALIDATED: gold and provider evaluation gates pass;
- MATERIALIZATION_BLOCKED: formula is valid but the selected execution engine lacks capability or a required governed policy;
- MATERIALIZATION_READY: formula, authority, execution capability and artifact compilation pass;
- RETIRED: retained only for legacy resolution;
- UNASSESSED: permitted only in the legacy adapter and forbidden in the V2 production registry.

**Steps:**

- [ ] Add a pure readiness fold whose inputs are recipe definition, formula expectation, authority envelope, formula evaluation and execution-capability verdict.
- [ ] Make every state include machine-readable blocker codes.
- [ ] Require a reviewed formula expectation for FORMULA_AUTHORABLE.
- [ ] Require exact gold cases for FORMULA_VALIDATED.
- [ ] Require execution-capability proof for MATERIALIZATION_READY.
- [ ] Keep predictive validation outside this vocabulary.
- [ ] Expand recipe formula expectations from unary-only to Formula-v2 body shapes.
- [ ] Add positive, refusal, boundary and authority-drift gold cases per recipe.
- [ ] Add corpus partitioning by recipe pack so evaluation can be staged without weakening global integrity.
- [ ] Make the shadow gate report readiness by recipe and operation, not only aggregate success.
- [ ] Prevent legacy UNASSESSED recipes from being counted as formula-ready or design-checked in contract v3.
- [ ] Classify all thirty-three downstream-derivation recipes explicitly during Tasks 11–17.

**Minimum gold cases per executable recipe:**

- ordinary populated window;
- empty window;
- null measure;
- boundary timestamp at start and end;
- duplicate entity/event rows;
- reversal or correction when applicable;
- mixed status when applicable;
- mixed currency when applicable;
- late-arriving record when applicable;
- zero denominator for ratio;
- ambiguous binding refusal;
- missing authority refusal.

**Acceptance:**

- V2 registry validation fails on UNASSESSED.
- Conceptual recipes remain discoverable but cannot be registered or materialized as formulas.
- MATERIALIZATION_READY cannot be reached from prose, template notes or an LLM assertion.
- Readiness changes are auditable and content-hashed.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_recipe_readiness.py tests/featuregen/overlay/upload/test_recipe_formula_expectations.py tests/featuregen/overlay/upload/test_recipe_formula_gate.py tests/featuregen/overlay/upload/test_recipe_formula_gold.py -v

**Commit:** feat(recipes): add closed execution-readiness states and formula evidence gates

**Acceptance: DONE 2026-08-10 (the closed fold; the evidence-path expansions recorded as deviations below).** `recipe_readiness.fold_readiness` — ONE pure, total, MONOTONE fold (clearing a blocker can only move a recipe up), whose every input is the OUTPUT of a deterministic gate elsewhere: BR-2's `computation_kind`, BR-4's temporal blockers, BR-5's binding-verdict reason codes, expectation-registry membership, BR-6's grammar verdict and the SELECTED engine's capability verdict, plus governed-policy blockers. Every non-terminal state carries machine-readable blocker codes — the BR-4/BR-5 vocabularies pass through VERBATIM, so "why isn't this ready?" is a list of named facts. FORMULA_AUTHORABLE requires a reviewed expectation (its absence is itself a named blocker); FORMULA_VALIDATED requires the gold+provider gates; `engine_verdict=None` means NO engine selected — the recipe RESTS at FORMULA_VALIDATED honestly rather than being blamed for an engine nobody chose (MATERIALIZATION_* speaks only once an engine is named, and READY requires its proof). UNASSESSED does not exist in this vocabulary — it lives only on the legacy adapter's projection, and BR-8's v3 renders it as the idea it is (the "cannot be counted formula-ready" guard lands there, where counting happens). `governed_model_output` REFUSES the formula ladder (`model_feature_spec_owns_readiness` — BR-7A's states). Predictive validation stays outside the vocabulary. DEVIATIONS (deferred deliberately, not silently): expectation expansion to v2 body shapes, the per-recipe gold minimum corpus + partitioning by pack, and shadow per-recipe/per-operation reporting ride with the BR-11..17 pack work and BR-18's engine — the fold consumes their verdicts through inputs that already exist today; the 33 downstream-derivation classifications happen in Tasks 11–17 as the plan states.

---

## Task BR-7A: Introduce a separate governed model-feature contract

**Purpose:** Prevent propensities, forecasts, anomaly scores and accounting/risk model outputs from being forced into deterministic Formula-v2 or presented as if a short recipe description were an executable model.

**Files:**

- Create: src/featuregen/overlay/upload/model_feature_contract.py
- Create: src/featuregen/overlay/upload/model_feature_registry.py
- Create: src/featuregen/overlay/upload/model_feature_readiness.py
- Create: tests/featuregen/overlay/upload/test_model_feature_contract.py
- Modify: recipe_contract_v2.py and suggestion contract v3

**ModelFeatureSpec requirements:**

- stable model-feature ID and output contract;
- model family, registered model/version reference and owner;
- prediction grain and prediction timestamp;
- training-data cutoff and inference knowledge-time policy;
- target/label definition and outcome window;
- input feature-set revision;
- population, exclusions and permitted purpose;
- score type, calibration and valid range;
- model-risk validation status and expiry;
- fairness, privacy and suitability controls where applicable;
- monitoring, drift and fallback policy;
- lineage to the model artifact and inference run.

**Applies to current or planned outputs such as:**

- next-best-product propensity;
- churn/default/fraud/claims probability;
- CLV projection;
- behavioral runoff forecast;
- anomaly scores and typology scores;
- ECL, SICR and IFRS9 stage outputs;
- VaR, expected shortfall and model-produced Greeks;
- mortality/morbidity loadings;
- physical/transition risk scores;
- uplift and campaign-treatment effect.

**Rules:**

- A ModelFeatureSpec is not a FormulaReferenceV2.
- Deterministic preprocessing inputs may be Formula-v2 recipes, but the prediction remains a governed model output.
- A model output cannot be materialization-ready without a registered model version and valid model-governance decision.
- Model performance, calibration and fairness are not inferred from recipe metadata.
- Near-label and target-derived model inputs remain subject to use-case-specific leakage controls.

**Steps:**

- [ ] Add schema, canonical revision hash and validation.
- [ ] Add closed readiness states: MODEL_SPEC_BLOCKED, MODEL_REGISTERED, MODEL_VALIDATED, INFERENCE_READY and MODEL_RETIRED.
- [ ] Add lineage from model feature to deterministic input recipe revisions.
- [ ] Add contract-v3 presentation distinct from deterministic formulas.
- [ ] Reclassify current propensity, projection, anomaly and model-risk recipes during Tasks 11–17.
- [ ] Add refusal tests for an absent model version, expired validation, wrong prediction grain and post-cutoff training/inference data.

**Acceptance:**

- No model-produced output is described as Formula-validated.
- A valid deterministic input pack does not imply the model itself is approved.
- Contract v3 clearly identifies formula, model and conceptual outputs.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_model_feature_contract.py -v

**Commit:** feat(model-features): separate governed model outputs from deterministic recipes

**Acceptance: DONE 2026-08-10 (contract + ladder + registry; the v3 presentation lands with BR-8, reclassification with BR-11..17 — both plan-sequenced there).** `ModelFeatureSpecV1` — a prediction's own typed home, validated at CONSTRUCTION like RecipeDefinitionV2: model family (9 closed families covering the plan's applies-to list), registered model ref + version (`""` = honestly unregistered), owner, prediction grain + timestamp role, training-cutoff + inference knowledge-time policies, target definition + outcome window, input-feature-set revision, population/exclusions/purposes, score type + calibration + range, fairness/privacy/monitoring/fallback policies, and `leakage_classification` defaulting NEAR_LABEL (model outputs are guilty until classified). `canonical-model-feature-v1` is fields()-driven — editing the target definition or input pack stales every governance approval by lookup miss. `fold_model_readiness` mirrors BR-7's discipline (typed verdicts in, honest floor by default): MODEL_SPEC_BLOCKED → MODEL_REGISTERED → MODEL_VALIDATED → INFERENCE_READY, MODEL_RETIRED aside — validation currency distinguishes EXPIRED from ABSENT, and INFERENCE_READY needs capability proven at the declared grain on data inside the declared cutoffs. The four named refusals PROVEN: absent model version, expired validation, wrong prediction grain, post-cutoff data. Rules held: a ModelFeatureSpec is NOT a FormulaReferenceV2 (separate type; no formula field exists to fill); `input_recipe_revisions` is lineage only — a valid deterministic input pack does not imply the model is approved (separate ladders, meeting only in v3's presentation); nothing here is inferred from recipe metadata. Registry law at import (unique ids, construction-validated); `MODEL_FEATURES` is EMPTY until the BR-11..17 reclassification moves the propensity/projection/anomaly/model-risk recipes here deliberately, one review each.

---

## Task BR-8: Publish suggestion contract v3 and update the table and column experiences

**Purpose:** Let users distinguish a good idea from a computable, governed and materializable feature.

**Files:**

- Modify: src/featuregen/overlay/upload/suggestion_contract.py
- Modify: src/featuregen/overlay/upload/suggestions.py
- Modify: src/featuregen/api/routes/suggestions.py
- Modify: frontend/src/api.ts
- Modify: frontend/src/screens/SuggestedFeaturesScreen.tsx
- Modify: frontend/src/screens/SuggestionCard.tsx
- Modify: frontend/src/screens/AssetDetailScreen.tsx
- Modify: frontend/src/index.css
- Test: tests/featuregen/overlay/upload/test_suggestion_contract.py
- Test: tests/featuregen/api/routes/test_suggestions_route.py
- Test: frontend/src/screens/SuggestedFeaturesScreen.test.tsx
- Test: frontend/src/screens/SuggestionCard.user-summary.test.tsx
- Test: frontend/src/screens/AssetDetailScreen.dossier.test.tsx

**Compatibility:**

- contract_version=1 and contract_version=2 remain unchanged.
- contract_version=3 is explicit during rollout.
- Unknown v3 enum members render as words rather than crashing the frontend.
- No new write action is added to the read-only suggestions route.

**V3 suggestion fields:**

- recipe_contract_version;
- output_id, output label and output type;
- output additivity, unit and currency policy;
- execution_readiness;
- computation_kind and model-feature summary when applicable;
- readiness_blockers;
- formula schema version and formula summary when available;
- exact bound parameters;
- allowed parameter schema;
- typed temporal summary;
- selected grain and source event grain;
- status, sign, reversal and currency policy references;
- SME review status and review reference;
- primary objective and supporting objectives;
- leakage class and permitted modelling stages;
- replacement/legacy recipe references;
- binding ambiguity status;
- materialization capability status.

**UX requirements:**

- Use distinct language for conceptual only, governance blocked, formula validated and materialization ready.
- Keep design checked separate from execution readiness.
- Never use green success styling for conceptual or merely design-checked states.
- Show the exact output in the card title; do not title a card with a multi-output family phrase.
- Show the selected window and policy choices near the formula.
- Let the user inspect allowed variants without generating all variants.
- Explain each blocker in banking language and retain the machine code in the audit drawer.
- Group blockers into data meaning, time, currency, relationship, formula capability, governance and execution.
- Show primary objective separately from supporting relevance.
- Provide filters for executable, blocked and conceptual suggestions.
- Preserve the existing table-context and from-column explanation.
- On asset detail, show whether the opened column is a required operand, optional operand, grain, time or policy input.

**Steps:**

- [ ] Freeze v1/v2 OpenAPI and real-body fixtures.
- [ ] Add Pydantic v3 response models with extra fields forbidden.
- [ ] Add page summary counts by execution readiness.
- [ ] Add TypeScript v3 types and runtime-tolerant render vocabulary.
- [ ] Add a compact card summary and expanded technical/governance detail.
- [ ] Add the read-only parameter selector and v3 variant GET.
- [ ] Add explicit empty states for no conceptual match, ambiguous data, missing governance and unsupported formula.
- [ ] Add accessibility tests for heading hierarchy, status words, keyboard disclosure and non-color status communication.
- [ ] Add snapshot/fixture tests for one example in every readiness state.
- [ ] Keep v2 as the default until Task 24 rollout gates pass.

**Acceptance:**

- A user cannot mistake conceptual-only for executable.
- A formula-validated but currency-blocked recipe explains both facts.
- The card title uniquely identifies the output and selected variant.
- v1/v2 clients and tests are byte-compatible.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_suggestion_contract.py tests/featuregen/api/routes/test_suggestions_route.py -v
- cd frontend && npm test -- SuggestedFeaturesScreen SuggestionCard AssetDetailScreen

**Commit:** feat(suggestions): publish execution-ready recipe contract v3

**Acceptance: DONE 2026-08-10 (the v3 contract + card truthfulness; the v3-default screen experience rides the BR-24 rollout, recorded below).** Contract v3 is the v2 page plus ADDITIVE truth and nothing else — proven by deleting the additions and comparing EQUAL to the v2 body (route test), so v3 can never drift into a re-rendering that happens to agree today. The additions: the page's own declared `contract_version`, one `execution` block per hit (`recipe_contract_version` / `computation_kind` / `execution_readiness` / `readiness_blockers` with BR-8's seven display groups / `binding_ambiguity`), and the page-level `readiness_counts` tally. Today's whole legacy registry surfaces as what it IS: UNASSESSED conceptual ideas — rendered as "idea — execution not assessed", carried with ZERO blockers, because the absence of a review is not a defect (the no-blocked invariant) — while the two reviewed expectation anchors (merchant_mcc_diversity, obligor_facility_count) enter BR-7's REAL fold and surface FORMULA_AUTHORABLE (gold unproven, named, formula_capability group) or FORMULA_BLOCKED when the engine's own binding verdict was ambiguous (data_meaning group). The one binding blocker this surface asserts is its own (`ambiguous_operand_binding`) — BR-5's per-operand vocabulary speaks only where `bind_v2_operands` actually ran, never smuggled here. v1/v2 BYTE-FROZEN (pinned: v2 carries no execution key, no new top-level keys; v1's stability test unchanged); v3 explicit opt-in via `?contract_version=3`; unsupported-version parametrization moved 3→4, the reviewed path. OpenAPI publishes `FeatureSuggestionPageV3Response` extra-forbid, validated against the REAL body. Frontend: `execution?` is additive on the suggestion type; the card renders a readiness chip beside — never merged with — the design-check badge (SEPARATE axes), nothing readiness-shaped when the block is absent, no success fill on any state (even ready-to-materialize stays a quiet chip), unknown future enum members render as de-underscored WORDS (pinned by test), and the audit drawer explains each blocker in banking language with the machine code kept beside it. DEVIATIONS (rollout-staged, deliberate): the screens stay on v2 as their default fetch per the plan's own compatibility stage ("v2 default until Task 24 gates pass") — the executable/blocked/conceptual filters, parameter selector, variant GET, per-readiness empty states and the dedicated a11y sweep land WITH the BR-24 flip that makes v3 the lived experience; no index.css change was needed (existing badge/tone/detail classes carry the chip and drawer); AssetDetailScreen column-role display rides the same flip.

---

## Task BR-9: Correct taxonomy applicability and coverage accounting

**Purpose:** Stop supporting tags and legacy inference from looking like owned banking use-case coverage.

**Files:**

- Modify: src/featuregen/overlay/upload/taxonomy/coverage.py
- Modify: src/featuregen/overlay/upload/taxonomy/coverage_cli.py
- Modify: src/featuregen/overlay/upload/taxonomy/recipe_applicability.py
- Modify: src/featuregen/overlay/upload/taxonomy/use_cases.py where approved
- Test: tests/featuregen/overlay/upload/taxonomy/test_coverage_cli.py
- Test: tests/featuregen/overlay/upload/taxonomy/test_recipe_applicability.py
- Create: docs/architecture/banking-recipe-coverage-targets.json

**Coverage tiers:**

- AUTHORED_PRIMARY: a reviewed recipe owns the objective;
- AUTHORED_SUPPORTING: relevant input to the objective, not coverage;
- LEGACY_INFERRED: migration-only and never accepted by a release gate;
- INTENTIONALLY_EMPTY: reviewed future scope;
- ZERO: no primary or supporting recipe.

**Steps:**

- [ ] Require every V2 recipe to declare one selectable primary objective.
- [ ] Require supporting objectives to be explicit and different from primary.
- [ ] Remove effective coverage as a release-quality shortcut.
- [ ] Report recipe count and executable recipe count separately by leaf.
- [ ] Report conceptual-only versus formula-validated coverage.
- [ ] Report legacy-inferred recipes as debt.
- [ ] Add target coverage by release increment rather than demanding shallow coverage of every leaf.
- [ ] Review the thirteen intentionally-empty leaves; keep the status only with an owner and rationale.
- [ ] Create explicit backlog rows for the twenty-eight leaves currently lacking a primary recipe.
- [ ] Add coverage differential tests proving a supporting recipe cannot change primary coverage.

**Acceptance:**

- Primary coverage cannot increase when only a supporting tag is added.
- An objective is not called executable-covered unless at least one Formula-validated or Materialization-ready primary recipe exists.
- Legacy applicability inference reaches zero by Task 17.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/taxonomy -v
- uv run python -m featuregen.overlay.upload.taxonomy.coverage_cli --format json

**Commit:** fix(taxonomy): separate owned, supporting and executable recipe coverage

**Acceptance: DONE 2026-08-10 — and R3 closes here.** The five closed tiers via ONE pure fold (`coverage_tier`) whose ORDER is the semantics: supporting participates only after every primary question is answered, so a supporting tag can never move primary coverage — proven exhaustively over the whole input space (differential test), at the applicability level (an appended `segmentation` tag lands in `secondary` ONLY, even in-family, even though that leaf has no owner today), and strongest for AUTHORED recipes (the tag bag adds NOTHING — ownership and relevance are both explicit declarations, BR-2's construction rules already enforcing one selectable primary + supporting≠primary). The honest census: 8 AUTHORED_PRIMARY / 39 LEGACY_INFERRED / 28 AUTHORED_SUPPORTING / 13 INTENTIONALLY_EMPTY / 0 ZERO across the 88 selectable leaves. The executable/conceptual split reads the SAME machinery contract v3 renders (BR-7's fold over the reviewed expectation registry — never a parallel opinion): the two anchors rest at FORMULA_AUTHORABLE, which is deliberately NOT executable, so `executable_covered_leaves` is EMPTY and pinned so — an objective is not executable-covered until a Formula-validated/Materialization-ready primary exists. Legacy debt is REPORTED not laundered: 143 of 157 recipes still carry legacy-derived applicability, 39 leaves' only primaries are inference (the numbers Task 17 drives to zero). The GATE (`owned_discovery_coverage`) drops the effective-coverage shortcut (pinned absent from its output): the four release anchors must be tier AUTHORED_PRIMARY, every no-primary gap must have an explicit backlog row naming its owning increment (all 28 mapped to BR-11..16 in `banking-recipe-coverage-targets.json`), and every intentionally-empty leaf needs owner + rationale (all 13 documented; owners are ROLES, never invented names) — both refusal paths proven against doctored targets. Gate exit code preserved; verification CLI green. DEVIATION (deliberate): legacy debt and executable coverage inform but do not gate THIS release — failing the build for debt the plan already schedules would teach people to stop running the audit.

---

## Task BR-10: Add the canonical banking event, state and policy vocabulary

**Purpose:** Give recipes the semantic building blocks that eleven current definitions explicitly lack and many others currently approximate.

**Files:**

- Modify: src/featuregen/overlay/upload/concepts.py
- Modify: src/featuregen/overlay/upload/binding_roles.py
- Modify: src/featuregen/overlay/upload/operational_facts.py
- Modify: src/featuregen/overlay/upload/field_policies.py
- Modify: src/featuregen/data_agent/eligibility.py
- Modify: src/featuregen/data_agent/eligibility_store.py
- Create: src/featuregen/overlay/upload/banking_policies.py
- Create: tests/featuregen/overlay/upload/test_banking_concepts.py
- Create: tests/featuregen/overlay/upload/test_banking_policies.py

**Canonical transaction/event concepts:**

- transaction_id and original_transaction_id;
- account_id, card_id, merchant_id and beneficiary_id (counterparty identity is NOT a new identifier concept: `counterparty_id` is a RETIRED alias — D12.1 and the three-axis decision made counterparty a PARTY ROLE of the customer/party identifier. Counterparty-ness is expressed through `party_role` and Task 5's `economic_role`/network-role fields on the operand, never by reviving the alias);
- transaction amount and original amount;
- debit/credit direction and physical sign convention;
- transaction type, instrument, channel, rail, scheme and MCC;
- authorization outcome and authorization timestamp;
- booking status and booking timestamp;
- value date;
- clearing status and clearing timestamp;
- settlement status and settlement timestamp;
- return, rejection, refund, chargeback and reversal event/status/reason;
- currency, account currency and conversion-rate reference;
- counterparty country, merchant country and corridor direction;
- ingestion/knowledge timestamp.

**Canonical state/lifecycle concepts:**

- account open, close, active and dormant state;
- product holding valid_from and valid_to;
- approved limit, available limit, drawn principal and accrued interest;
- contractual due amount, minimum due and payment allocation;
- facility, covenant, collateral, guarantee and valuation lifecycle;
- promise amount, promise due date, promise outcome and kept/broken state;
- contact attempt, contact outcome and right-party-contact indicator;
- mandate state, scheduled instruction and execution outcome;
- claim identity, claim event, reserve, paid amount and claim status;
- invoice issue, due, approval, payment and credit-note event;
- LC/guarantee issue, amendment, utilization, expiry and rollover event.

**Governed policy types:**

- eligible transaction status policy;
- direction/sign policy;
- reversal and correction policy;
- active account/product/facility policy;
- currency conversion policy;
- business calendar and timezone policy;
- allocation policy for joint accounts and relationship rollups;
- threshold policy with jurisdiction, effective period and currency;
- risk-rating/corridor policy with effective dating;
- model output policy carrying model version, horizon and confidence;
- privacy, suitability, consent and purpose policy.

**Steps:**

- [ ] Add concepts only after checking no current governed concept already has the meaning.
- [ ] HARD RULE (re-baseline): no added concept may carry a name in `_LEGACY_ALIASES` (concepts.py) — as of 58041a59 a Need targeting a successorless retired alias fails import validation, and stored legacy values are handled by two-tier matching, so reviving an alias would both break validation and duplicate a governed decision.
- [ ] Define aliases without collapsing distinct lifecycle stages.
- [ ] Add entity links, PIT roles, units, additivity and sensitivity metadata.
- [ ] Add operational facts and evidence requirements for sign, status, reversal, currency and active-state policies.
- [ ] Reuse the existing eligibility-policy and authority stores rather than embedding source-specific status literals in recipes.
- [ ] Add enrichment prompt and projection coverage for new concepts.
- [ ] Add source-profile guidance so card authorization, core-ledger posting and payment settlement datasets are not classified as interchangeable transaction tables.
- [ ] Add tests that intentionally refuse authorization-from-settlement, payee-from-beneficiary-bank and contact-from-cost mappings.
- [ ] Add backwards-compatible aliases for valid old concept names; do not alias semantically different concepts.

**Acceptance:**

- Current recipes no longer need to state that chargeback, right-party contact, promise outcome, invoice lifecycle or installment schedule concepts do not exist.
- A source must declare or resolve its lifecycle/status policy before filtered formulas are materialization-ready.
- The concept registry can distinguish customer, account, facility, merchant, beneficiary, counterparty and legal group grains.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_concepts.py tests/featuregen/overlay/upload/test_banking_concepts.py tests/featuregen/overlay/upload/test_banking_policies.py -v

**Commit series:** feat(banking-semantics): add governed event, lifecycle and policy concepts

**Acceptance: DONE 2026-08-11 (two commits: `ffadc366` concepts, `5a7bfdc3` policy kinds).** THIRTY-FIVE concepts, every one grounded in an audit admission or the plan list, registry 324→359 (`concepts@3`): the four payment stages (authorization / posting / clearing / settlement) as DISTINCT statuses and timestamps whose descriptions name each other apart — the bank_bic mechanism, so a card-auth feed, a core-ledger posting table and a settlement feed can no longer be classified as interchangeable transaction tables (pinned: "never interchangeable"); return / chargeback / reversal kept as three lifecycles (the plan's no-collapsing rule, pinned); the acceptance's own list closed — chargeback_status, right_party_contact_flag, promise_outcome (+amount/due_date), invoice_status, the installment schedule (due_date, minimum_due_amount, payment_allocation), product_holding, account_status, notice_period, available_limit/drawn_principal, matching_status, instruction_execution_outcome, lc_guarantee_event, claim_status/claim_paid_amount, policy_loan_balance, customer_income, original_transaction_id (lineage in the SAME core_serial namespace), beneficiary_id (its own payee_registry namespace — the payee-from-beneficiary-bank refusal pinned), original_amount. Every grain the acceptance names is distinguishable (customer/account/facility/merchant/beneficiary/customer_group entity links; counterparty stays the party ROLE — `counterparty_id` untouched in `_LEGACY_ALIASES`, the hard rule held by test). All behaviour DECLARED (pit_role maturity for contractual-future dates, semi-additive stocks, is_a edges walking to governed roots); enrichment prompt + projection coverage is registry-DERIVED and pinned. THE POLICY HALF: `banking_policies.py` closes the eleven plan-named governed policy kinds with declaration schemas (a threshold without jurisdiction+effective_period+currency refuses) and NO second store — resolution homes are the existing mechanisms (eligibility IR for status/reversal incl. ReversalMode's refuse-unsupported-shapes, BR-5 envelope for direction, BR-7A spec for model_output); every BR-6 authority-ref field maps to its kind (total by introspection); and the acceptance sentence is mechanized end-to-end: `required_policy_kinds(filtered=True)` → `policy_blockers` → BR-7's fold lands FORMULA_BLOCKED with `policy_undeclared:eligible_status` NAMED, and declaring releases it. DEVIATIONS (deliberate): the 15 template prose admissions still SAY "no dedicated X concept" — cleaning each recipe's prose is exactly the BR-11..17 migration work (the ratchet holds 15, never worse); per-source policy DECLARATIONS (facts) arrive with catalog/pack work — the kinds, schemas and blockers are what BR-10 owed; no DataRole fork for the three transaction stages (concept-carried instead — a table-role fork would churn the Pass-B replay identity for no pack that needs it yet); eligibility-store/data-agent files untouched (reused as resolution homes, which is the step's own instruction).

---

## Task BR-11: Migrate and correct Retail/RBWM churn and cross-sell recipes

**Purpose:** Convert the current retail and cross-sell families into atomic outputs with account/customer lifecycle correctness.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/retail.py
- Create: src/featuregen/overlay/upload/recipes/cross_sell.py
- Create: tests/featuregen/overlay/upload/recipes/test_retail.py
- Create: tests/featuregen/overlay/upload/recipes/test_cross_sell.py
- Modify: src/featuregen/overlay/upload/recipe_registry_v2.py
- Modify: src/featuregen/overlay/upload/recipe_formula_expectations.py
- Modify: src/featuregen/overlay/upload/recipe_formula_gold.py

**Required retail corrections:**

- Split balance_trend into normalized_balance_slope and balance_slope.
- Add explicit latest-known snapshot selection and base-currency policy.
- Define eligible activity for dormancy; exclude failed, reversed, technical, closure and system-only events.
- Add an explicit as-of or pre-decision cutoff to dormancy.
- Split transaction-frequency halves ratio from transaction-count slope.
- Require eligible posted transaction status and event type for transaction-frequency recipes.
- Split inflow_outflow_ratio from net_transaction_flow.
- Require direction/sign authority; signed-amount inference is allowed only through a governed sign policy.
- Split salary_signal outputs into salary_credit_count, salary_credit_amount, salary_regularity and salary_confidence.
- Require credit direction, eligible posted state, stable payer identity and cadence; do not infer salary from category alone.
- Define product breadth from effective-dated active holdings, not only product_type and effective_date.
- Separate direct-debit mandate cancellation from failed/returned collection events.
- Replace fuzzy external-own-transfer matching with verified own-account relationship. Retain fuzzy matching only as conceptual-only with a privacy and false-match warning.
- Split RFM into recency, frequency and monetary atomic features; any combined score becomes a ModelFeatureSpec or reviewed deterministic scoring policy.
- Define account-to-customer allocation for joint accounts and multi-account relationships.

**Required cross-sell corrections:**

- Split every measure-bearing recipe into one output.
- Require an effective-dated eligible product universe for whitespace.
- Require product eligibility, suitability, affordability, consent, exclusions and availability before next-best-action materialization.
- Reclassify next_best_product_propensity as a model feature, not a deterministic formula recipe.
- Require campaign contact/impression, treatment, control and response events for campaign response.
- Separate descriptive response rate from predictive uplift.
- Reclassify CLV projection as a model/forecast feature; keep historical net revenue or margin as deterministic atomic recipes.
- Require an external or modelled total-wallet denominator for share of wallet. Internal product penetration must be named as internal penetration, not share of wallet.
- Require effective-dated, verified household membership and allocation for household rollups.
- Remove tenure-only upsell scoring unless a reviewed suitability policy is attached.

**Gold scenarios:**

- joint account with two customers;
- account closes mid-window;
- reversed salary-like credit;
- pension and internal-transfer lookalikes;
- direct-debit mandate cancelled without a returned payment;
- campaign response without prior treatment;
- product becomes ineligible during the window;
- customer has internal holdings but no external wallet estimate.

**Acceptance:**

- No retail recipe counts an unspecified event as customer activity.
- Net flow and inflow/outflow ratio have different output contracts.
- No cross-sell output claims eligibility or wallet share without its denominator/policy.
- Model outputs are visibly separated from deterministic features.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_retail.py tests/featuregen/overlay/upload/recipes/test_cross_sell.py -v

**Commit series:** feat(recipes-retail): migrate atomic RBWM churn and cross-sell recipes

**Acceptance: DONE 2026-08-11 (two commits: `3f5bad1e` retail, `ad4867a6` cross-sell).** All 22 legacy recipes (12 RETAIL_CHURN + 10 CROSS_SELL) explicitly replaced by 36 atomic V2 definitions plus the first TWO registered ModelFeatureSpecs — every plan correction a STRUCTURAL fact the tests read off the definitions, never prose: balance_trend split raw/normalized slope (latest-known snapshot policy + base-currency policy declared); dormancy over DECLARED eligible activity excluding the five ineligible classes by name, "never active" returning null; frequency halves-ratio vs count-slope; net flow and inflow/outflow ratio with DIFFERENT contracts (unit kind, additivity, empty-window answer — the acceptance sentence pinned literally); salary split four ways with payer-stability as an operand requirement and lookalikes excluded, confidence honestly conceptual; product breadth from effective-dated ACTIVE holdings (product_holding + valid_time — BR-10's concepts earning their keep); RFM three atoms + conceptual composite; mandate cancellation vs collection returns as two EVENTS on two source grains (mandate_event / payment_return_event); own-transfer executable ONLY over a VERIFIED own-account relationship, the fuzzy match surviving as conceptual with its PII/false-match warning; whitespace and internal penetration over the effective-dated ELIGIBLE product universe (policy_input operand); NBP propensity and CLV projection reclassified as governed_model_output referencing REGISTERED specs (unversioned → MODEL_SPEC_BLOCKED through BR-7A's real fold, proven); share_of_wallet conceptual until a total-wallet denominator exists, its computable neighbour NAMED internal penetration; campaign response descriptive, requiring recorded treatment, uplift left to models; household rollups over VERIFIED effective-dated membership + governed allocation; tenure-only upsell conceptual absent a reviewed suitability policy. Every deterministic recipe FORMULA_BLOCKED until its expectation is reviewed — nothing claims authorable by assertion. Migration counter 157→135 across the two commits, baseline regenerated each time; ALSO FIXED: `audit_registry`'s default never consulted the real V2 registry (the counter would have sat at 157 forever) — the default is now the production registry's replacements. Result class `dispersion` added (spread statistics are never summable). DEVIATIONS (deliberate): recipe_formula_expectations/gold untouched — the v2-body-shape expectation machinery is the BR-7-recorded deferral, and the gold scenarios land WITH it (their semantics are meanwhile encoded as eligibility structure the tests pin); joint-account allocation is a declared policy ref, its arithmetic arrives with the engine (BR-18); legacy retail/cross-sell templates stay in ALL_TEMPLATES until BR-17's cutover (the suggestion surface still grounds them; V2 feeds audit accounting only, by design).

---

## Task BR-12: Migrate and correct Credit Risk and Collections recipes

**Purpose:** Make facility/account obligations, payment allocation, delinquency and post-default stages explicit.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/credit.py
- Create: src/featuregen/overlay/upload/recipes/collections.py
- Create: tests/featuregen/overlay/upload/recipes/test_credit.py
- Create: tests/featuregen/overlay/upload/recipes/test_collections.py
- Modify: recipe registry, expectations and gold corpus

**Required credit corrections:**

- Replace generic monetary_stock roles with drawn principal, approved limit, available limit, EAD, collateral value or accrued balance as appropriate.
- Require facility_id as source grain for facility features; customer rollups are separate governed recipes.
- Require same-as-of and same-currency policy for utilization numerator and denominator.
- Split utilization level from utilization trend.
- Require contractual minimum due for minimum-payment behavior; percent-of-limit approximation remains conceptual-only.
- Require schedule due date, amount due and payment allocation for missed/partial payment counts.
- Add effective-dated collateral valuation, haircut, lien/seniority and collateral-allocation policy for LTV.
- Separate raw LTV, indexed LTV and LTV trend.
- Require covenant actual value, threshold, direction, unit, test date, waiver and cure state.
- Mark DPD, delinquency bucket, IFRS9 stage, SICR, forbearance and ECL inputs with permitted-use stages. They may be valid for monitoring/collections and leakage for origination/default prediction.
- Treat ECL and IFRS9 stage as model/accounting outputs with model/version provenance.
- Require bureau pull timestamp and knowledge timestamp; do not use a later bureau state at an earlier cutoff.

**Required collections corrections:**

- Move account/facility-level behavior to contract grain; add separate customer rollups.
- Require promise amount, promise due date, promise status and kept/broken outcome.
- Require payment-plan schedule and allocation for adherence.
- Replace cost_to_collect as a contact proxy with contact-attempt and right-party-contact events.
- Define cure, re-age and roll-forward transitions using state at window start and end.
- Add hardship request, assessment, arrangement and outcome lifecycle.
- Use exposure-at-default or defaulted-balance snapshot as the recovery denominator.
- Keep recovery and write-off recipes post-default only.
- Prevent write-off amount, recovery outcome and cure outcome from entering pre-default models.
- Split counts, rates, amounts, severities and durations into atomic outputs.

**Gold scenarios:**

- multiple facilities for one customer;
- payment posted but allocated to fees rather than principal;
- waiver active on a covenant test;
- collateral valuation after prediction cutoff;
- cure followed by re-default;
- broken promise partially paid;
- write-off after the modelling as-of;
- recovery in a different currency.

**Acceptance:**

- A deposit balance cannot ground a drawn-exposure recipe.
- A generic payment flow cannot establish a missed payment without a schedule.
- Pre-default applicability refuses post-default outcomes.
- Collections outputs are correct at facility grain before rollup.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_credit.py tests/featuregen/overlay/upload/recipes/test_collections.py -v

**Commit series:** feat(recipes-credit): migrate facility-correct risk and collections recipes

**Acceptance: DONE 2026-08-11 (two commits: `6491faf7` credit, `a0ede31e` collections).** All 26 legacy recipes (16 CREDIT_RISK + 10 COLLECTIONS) explicitly replaced by 34 atomic V2 definitions. THE ACCEPTANCE SENTENCES AS STRUCTURE: a deposit balance CANNOT ground a drawn-exposure recipe — every exposure operand names its economic role (drawn_credit_exposure / approved_credit_limit / exposure_at_default / collateral_valuation / defaulted_balance_snapshot), which BR-5 binds only over governed evidence; a generic payment flow CANNOT establish a missed payment — the schedule (due_date, scheduled_amount, payment_allocation — BR-10's concepts) is the operand set and the source grain; pre-default applicability REFUSES post-default outcomes — the near-label family (DPD, buckets, ECL, stage, SICR, forbearance) prohibits origination+default_prediction, and cure/recovery/write-off/cost outcomes are leakage-class OUTCOME with the same prohibition; collections outputs are correct at FACILITY grain before rollup — the one customer rollup is a separate recipe under `allocation:contract-to-customer-collections`. Structural corrections held by test: utilization level/trend split sharing the same-as-of/same-currency policy; contractual minimum due required (percent-of-limit stays conceptual); LTV raw/indexed/trend over effective-dated valuations with haircut/seniority/allocation policy (unsecured ≠ LTV zero; indexed NAMED indexed); covenant headroom carries actual/threshold/direction/unit/test-date/waiver-cure declared (a waived breach is never a silent pass); ECL and stage are deterministic READS of recorded accounting outputs under `model_output:ifrs9-ecl-engine` provenance — never computed, never unprovenanced; bureau reads declare knowledge time (a later bureau state cannot inform an earlier cutoff); promises read promise_amount/promise_due_date/promise_outcome (a broken promise partially paid counts its partial payment in the AMOUNT share, not the KEPT share); RPC reads contact attempt/outcome/flag events — cost_to_collect is cost efficiency, never a contact proxy; cure/roll-forward compare state at window START and END (cure followed by re-default reads NOT cured); recovery divides by the balance FROZEN at default under the recovery-currency policy. Result classes `extremum` + `flag` added (streaks are extrema; the contract's count-must-be-additive law held). Migration counter 135→109 across the two commits, baseline regenerated each time. DEVIATIONS (deliberate): expectations/gold ride the BR-7-recorded v2-shape deferral (gold scenarios encoded as eligibility structure meanwhile); a single exemplar customer rollup (worst days-in-collection) proves the rollup pattern — further rollups arrive on demand, each its own governed recipe.

---

## Task BR-13: Migrate and correct Fraud, AML and Payments recipes

**Purpose:** Model the payment lifecycle and financial-crime decision point precisely.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/fraud.py
- Create: src/featuregen/overlay/upload/recipes/aml.py
- Create: src/featuregen/overlay/upload/recipes/payments.py
- Create: tests/featuregen/overlay/upload/recipes/test_fraud.py
- Create: tests/featuregen/overlay/upload/recipes/test_aml.py
- Create: tests/featuregen/overlay/upload/recipes/test_payments.py
- Modify: recipe registry, expectations and gold corpus

**Required fraud corrections:**

- Define whether each recipe runs before authorization, after authorization or after booking.
- Require authorization outcome for card-testing and decline patterns.
- Add merchant, MCC, CNP/card-present, token/card continuity and small-amount policy.
- Replace beneficiary_bank with verified beneficiary/payee identity for first-time payee.
- Add customer/account/card source entity to merchant anomaly where the feature is customer-relative.
- Require effective threshold/control policy for just-under-limit behavior.
- Add device/account/card continuity rules for tokenization, reissue and shared legitimate devices.
- Split count, distinct-count, ratio, z-score and flag outputs.
- Correct merchant_mcc_diversity temporal wording and retain the Formula-v1/V2 count-distinct expectation.

**Required AML corrections:**

- Add jurisdiction, reporting threshold, effective period, currency and instrument for structuring.
- Require cash transaction/channel/instrument rather than ISO purpose code as a cash proxy.
- Make round-amount base currency-aware; remove unrelated mandatory purpose code.
- Add effective-dated corridor/country risk and transaction direction.
- Give nested correspondent flow an explicit respondent/correspondent entity grain.
- Add counterparty and account network roles to fan-in/fan-out and passthrough.
- Separate screening exposure, screening alert and confirmed match.
- Keep prior alert, case, SAR and investigator outcomes near-label and time-lagged.
- Add KYC/CDD status, refresh due date, beneficial ownership and correspondent due-diligence state.
- Add cryptocurrency on/off-ramp counterparty classification authority.

**Required payments corrections:**

- Replace settlement_status with authorization_outcome for authorization approval/decline.
- Add chargeback/dispute event, reason, lifecycle and original-transaction link.
- Add return/reject event and reason for return-payment outputs.
- Keep mandate state separate from collection execution outcome.
- Model initiation, authorization, booking, clearing, value and settlement timestamps separately.
- Use merchant/acquirer grain for merchant economics and card-scheme operations.
- Require fee basis, interchange, assessment, network cost and MDR amount/rate relationships.
- Split count, amount, rate and average outputs.

**Gold scenarios:**

- approved authorization later reversed;
- declined authorization with no settlement row;
- multiple beneficiaries at one beneficiary bank;
- chargeback linked to original transaction after the observation cutoff;
- return caused by mandate cancellation versus insufficient funds;
- threshold changes by jurisdiction and date;
- nested correspondent payment with respondent and ultimate originator.

**Acceptance:**

- Authorization and settlement are never interchangeable operands.
- Payee novelty is calculated on beneficiary identity, not bank identity.
- AML thresholds are effective-dated, jurisdictional and currency-aware.
- Alert/case outcomes cannot leak into behavior features.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_fraud.py tests/featuregen/overlay/upload/recipes/test_aml.py tests/featuregen/overlay/upload/recipes/test_payments.py -v

**Commit series:** feat(recipes-fincrime): migrate payment-lifecycle-correct fraud, AML and payments recipes

**Acceptance: DONE 2026-08-11 (`3135028c`).** All 33 legacy recipes (12 FRAUD + 11 AML + 10 PAYMENTS) explicitly replaced by 39 atomic V2 definitions. THE ACCEPTANCE SENTENCES AS STRUCTURE: authorization and settlement are NEVER interchangeable operands — every fraud recipe declares its lifecycle stage in `FRAUD_LIFECYCLE_STAGE` (total over the pack by test), post-authorization recipes read the authorization feed's own outcome and timestamp (BR-10's concepts), and the payments decline rate runs on `authorization_event` with settlement_status structurally absent; payee novelty is calculated on BENEFICIARY IDENTITY — `beneficiary_id` with "never the beneficiary bank" as the operand's relationship requirement, beneficiary_bank absent; AML thresholds are effective-dated, jurisdictional and currency-aware — the structuring threshold is a `threshold:` policy ref whose BR-10 declaration schema REQUIRES jurisdiction+effective_period+currency, and reads across jurisdictions or effective periods are excluded by name; alert/case outcomes CANNOT leak into behaviour features — the three alert-history recipes are near-label with origination prohibited AND read through knowledge time (system_time operand + knowledge_time_role), so an outcome recorded after the cutoff never informs it. Further structure: cash is channel+instrument, never the ISO purpose code (which keeps its one legitimate home, purpose_code_diversity); fan-in/fan-out, passthrough and nested-correspondent carry their two party/bank legs as DISTINCT binding groups — one physical column for two legs is refused, never merged (the counterparty-canonicalization lesson as contract law); nested correspondent runs at explicit respondent-bank grain; screening is THREE facts (exposure standard; alert and confirmed match near-label); corridor and VASP classifications are effective-dated `risk_corridor:` policy; chargebacks carry status+reason+original_transaction_id with post-cutoff disputes excluded; returns carry payment_return_status+return_reason_code with mandate state excluded (the retail pack owns it); settlement lag subtracts two NAMED stage timestamps in a distinct binding group; merchant economics run at merchant grain under the declared fee basis; impossible-travel stays CONCEPTUAL with the grammar gap named; `merchant_mcc_diversity` keeps its reviewed Formula-v1 count-distinct expectation verbatim and is the pack's one honestly-FORMULA_AUTHORABLE recipe, its temporal wording corrected. Migration counter 109→76, baseline regenerated same commit. DEVIATIONS (deliberate): gold scenarios ride the BR-7-recorded expectation deferral (their semantics encoded as eligibility structure); KYC/CDD-state recipes (AML correction 9) were never legacy recipes — they arrive as NEW recipes when a catalog carries KYC lifecycle data, tracked by the kyc backlog leaf; token/reissue continuity is a declared `active_state:` policy, its resolution arriving with the identity-graph work.

---

## Task BR-14: Migrate and correct Deposits/ALM and Markets recipes

**Purpose:** Separate liability cash-flow behavior, asset liquidity, rate sensitivity and model-produced market-risk measures.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/deposits_alm.py
- Create: src/featuregen/overlay/upload/recipes/markets.py
- Create: tests/featuregen/overlay/upload/recipes/test_deposits_alm.py
- Create: tests/featuregen/overlay/upload/recipes/test_markets.py
- Modify: recipe registry, expectations and gold corpus

**Required Deposits/ALM corrections:**

- Retire hqla_eligibility_contribution as a deposit-backed HQLA feature.
- Replace it with liability_cash_outflow_contribution and separate asset_hqla_buffer recipes.
- Require LCR runoff factor, stable/less-stable classification, insured/uninsured status, operational-deposit status and counterparty class.
- Require NSFR ASF factor, residual maturity, funding type and counterparty classification.
- Add actual paid deposit/customer rate to deposit beta; benchmark rate alone is insufficient.
- Define rate reset dates and lags for beta.
- Move repricing-gap output to account/book/bucket grain rather than customer grain.
- Require effective maturity, balance and scenario/runoff policy for maturity ladders.
- Require closure/break event, origination and contractual maturity for early withdrawal.
- Complete contractual_deposit_maturity_profile temporal policy and classify its future-horizon input correctly.
- Complete lagged_net_interest_flow sign authority.
- Split amount, share, ratio, beta, count and duration outputs.

**Required Markets corrections:**

- Require valuation timestamp, model version, horizon, confidence level, currency and scenario for VaR.
- Require instrument, position, valuation and model provenance for Greeks.
- Add netting set, legal enforceability, CSA, collateral and gross/net exposure for notional netting.
- Require counterparty/legal-entity grain and effective credit support.
- Distinguish limit amount, limit type, usage and breach event.
- Add desk/book hierarchy and allocation for concentration.
- Treat VaR, expected shortfall and model Greeks as governed model outputs, not raw deterministic aggregates.

**Gold scenarios:**

- deposit classification changes mid-period;
- benchmark rate changes before customer rate;
- term deposit broken before maturity;
- same deposit included in two maturity snapshots;
- unenforceable netting agreement;
- VaR rows with different confidence/horizon/model versions;
- collateral posted after cutoff.

**Acceptance:**

- No deposit recipe claims the liability itself is HQLA.
- Deposit beta cannot compute without customer/deposit rate.
- LCR/NSFR contributions use regulatory classification factors.
- Market model outputs preserve complete model/valuation provenance.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_deposits_alm.py tests/featuregen/overlay/upload/recipes/test_markets.py -v

**Commit series:** feat(recipes-treasury): migrate ALM and market-risk recipes with regulatory semantics

---

## Task BR-15: Migrate and correct Custody, Asset Management, Insurance, Islamic and ESG recipes

**Purpose:** Correct specialist product, accounting, valuation and methodology semantics.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/custody.py
- Create: src/featuregen/overlay/upload/recipes/asset_management.py
- Create: src/featuregen/overlay/upload/recipes/insurance.py
- Create: src/featuregen/overlay/upload/recipes/islamic.py
- Create: src/featuregen/overlay/upload/recipes/esg.py
- Create corresponding tests under tests/featuregen/overlay/upload/recipes/
- Modify: recipe registry, expectations and gold corpus

**Custody corrections:**

- Separate trade date, contractual settlement date and actual settlement date.
- Add settlement status, fail reason, market calendar and SSI authority.
- Define knowability only after contractual settlement date for a fail.
- Add corporate-action event, entitlement, election, response deadline and payment.
- Add security, account, market and counterparty grains.
- Split fail count, fail value, fail rate and fail age.

**Asset-management corrections:**

- Add fund, portfolio and share-class grain.
- Add valuation date/calendar, NAV version, subscriptions, redemptions and corporate actions.
- Separate market performance from investor flows and AUM movement.
- Add benchmark identity/methodology, fee basis and FX policy.
- Add liquidity buckets, redemption terms, gates and swing-pricing policy.
- Separate historical net flow from redemption-risk model output.

**Insurance corrections:**

- Add policy state, coverage period, exposure unit, claim identity and claim lifecycle.
- Separate written, earned and collected premium.
- Separate claim count, paid amount, incurred amount, reserve, severity and loss ratio.
- Correct additivity for frequency, severity and loss ratio.
- Add lapse, death, maturity and claim competing-risk/censoring treatment.
- Add reinsurance treaty, attachment, limit and recoverable lifecycle.
- Treat mortality/morbidity loading and claims-fraud scores as model outputs.

**Islamic corrections:**

- Add contract type, Sharia product structure and board/governance reference.
- Add profit pool, sharing ratio, benchmark/reference rate and actual profit rate.
- Add principal/profit split and installment due/payment schedule for Murabaha.
- Add prohibited-income classification and purification governance.
- Separate Takaful contribution, claim and participant-fund state.
- Avoid using interest terminology where profit-rate semantics apply.

**ESG corrections:**

- Add reporting entity, organizational boundary, reporting year, methodology and assurance status.
- Add scope, category and value-chain boundary to prevent double counting.
- Add PCAF attribution factor, financed amount, EVIC/revenue basis and data-quality score.
- Add scenario, horizon, hazard, asset location and vulnerability for physical risk.
- Add transition pathway, sector and target basis for transition alignment.
- Separate absolute emissions from intensity.

**Acceptance:**

- Every specialist output identifies the relevant accounting/valuation/methodology basis.
- Claims count, severity and loss ratio are separate outputs.
- Islamic recipes carry contract-specific semantics.
- ESG outputs cannot combine incompatible boundaries, years or methodologies.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_custody.py tests/featuregen/overlay/upload/recipes/test_asset_management.py tests/featuregen/overlay/upload/recipes/test_insurance.py tests/featuregen/overlay/upload/recipes/test_islamic.py tests/featuregen/overlay/upload/recipes/test_esg.py -v

**Commit series:** feat(recipes-specialist): migrate custody, AM, insurance, Islamic and ESG recipes

---

## Task BR-16: Migrate and correct Corporate/CIB recipes

**Purpose:** Model trade finance, working capital, legal-group exposure and transaction-banking structures at the correct lifecycle and grain.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/corporate_cib.py
- Create: tests/featuregen/overlay/upload/recipes/test_corporate_cib.py
- Modify: recipe registry, expectations and gold corpus

**Required corrections:**

- Add instrument identity and type for LC, guarantee, standby LC and contingent facilities.
- Add issue, amendment, utilization, expiry, claim and rollover events.
- Add invoice identity, issue date, due date, paid date, receivable amount, credit note/dilution and debtor identity.
- Add SCF buyer, supplier, program, invoice approval, payment date, terms and program limit.
- Require effective-dated legal hierarchy, control relationship and group membership.
- Add intra-group elimination and allocation policy.
- Add guarantee amount, enforceability, expiry, guarantor quality and wrong-way/correlation policy.
- Require AR, AP, inventory, revenue and COGS periods for working-capital cycle.
- Add participant accounts, pool type, sweep events and intraday timestamps for cash pooling.
- Split pool utilization, pool benefit and intraday peak.
- Add drawn exposure, contingent exposure, product line and stress threshold policy to cross-product stress.
- Separate stressed-line count, exposure trend and trade-flow decline.
- Keep obligor_facility_count atomic and formula-authorable.
- Require relationship/client profitability components before RAROC or wallet outputs.

**Gold scenarios:**

- LC amended and extended without true rollover;
- invoice partially paid and later credited;
- supplier changes SCF program;
- subsidiary leaves a group mid-window;
- guarantee expired before cutoff;
- notional pool with intraday deficit but positive end-of-day balance;
- product line has a limit but no drawn exposure.

**Acceptance:**

- No LC/guarantee output is computed from generic contingent exposure alone.
- DSO/dilution/debtor concentration require invoice lifecycle data.
- Group exposure respects effective legal hierarchy and elimination policy.
- Intraday peak cannot compile from daily snapshots.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_corporate_cib.py -v

**Commit series:** feat(recipes-cib): migrate lifecycle-correct corporate and transaction-banking recipes

---

## Task BR-17: Cut the active registry over to V2 and retire legacy debt

**Purpose:** Complete migration without deleting historical identity or breaking old contracts.

**Files:**

- Modify: src/featuregen/overlay/upload/recipe_registry_v2.py
- Modify: src/featuregen/overlay/upload/templates.py
- Modify: src/featuregen/overlay/upload/recipe_legacy_adapter.py
- Modify: src/featuregen/overlay/upload/contract/gate1.py
- Modify: audit and coverage baselines
- Test: all template, recipe, formula, suggestion and contract suites

**Steps:**

- [ ] Confirm every legacy recipe has a V2 replacement, explicit conceptual-only disposition or retirement record.
- [ ] Add source-controlled aliases from legacy recipe IDs to one or more atomic V2 recipes.
- [ ] Prevent one legacy multi-measure ID from resolving ambiguously; require the output alias.
- [ ] Switch contract-v3 grounding to the V2 registry.
- [ ] Keep v1/v2 suggestion generation on the legacy projection during the compatibility window.
- [ ] Make new recipe authoring through Template fail CI (this is also the enforcement of BR-2's routing rule for the 4c triage cards).
- [ ] Turn recipe audit strict mode on.
- [ ] Require zero UNASSESSED V2 recipes.
- [ ] Require zero legacy applicability inference in release coverage.
- [ ] Require zero PIT placeholder mismatches and zero display identity collisions.
- [ ] Mark the old registry read-only and document its removal criteria; do not delete it in this task.
- [ ] Update stale module documentation that still says templates are not wired or that the registry contains 153 recipes.

**Acceptance:**

- Contract v3 uses only RecipeDefinitionV2.
- All reviewed debt counters equal zero for the active registry.
- Existing v1/v2 API snapshots remain unchanged.
- Historical canonical-recipe-v1 contexts still verify.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload tests/featuregen/api/routes/test_suggestions_route.py tests/featuregen/formula -v
- cd frontend && npm test

**Commit:** refactor(recipes): cut the active suggestion registry over to atomic Recipe Contract v2

---

## Task BR-18: Add the transaction and account foundation packs

**Purpose:** Build reusable atomic primitives before authoring more domain composites.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/transaction_foundation.py
- Create: src/featuregen/overlay/upload/recipes/account_foundation.py
- Create: tests/featuregen/overlay/upload/recipes/test_transaction_foundation.py
- Create: tests/featuregen/overlay/upload/recipes/test_account_foundation.py
- Add reviewed Formula-v2 expectations and gold cases
- Extend concept/policy coverage only where Task 10 did not already do so

**Transaction foundation tranche A — existing Formula-v1/V2-basic capability:**

- posted_debit_transaction_count;
- posted_credit_transaction_count;
- posted_debit_amount;
- posted_credit_amount;
- net_posted_transaction_flow;
- posted_transaction_average_amount;
- distinct_transaction_counterparty_count;
- distinct_merchant_count;
- active_transaction_day_count;
- failed_transaction_count and failed_transaction_rate;
- reversal_count, reversal_amount and reversal_rate;
- refund_count and refund_amount;
- return_count and return_amount;
- cross_border_transaction_count and cross_border_amount;
- cash_withdrawal_count and cash_withdrawal_amount;
- fee_amount;
- transaction_recency_days.

**Transaction foundation tranche B — Formula-v2 analytical capability:**

- transaction_amount_median and percentile;
- inter_transaction_gap_average and percentile;
- transaction_velocity;
- burstiness;
- transaction_count_trend and amount trend;
- transaction_amount_volatility;
- day-of-week and month seasonality;
- counterparty concentration HHI and top-counterparty share;
- fan_in_counterparty_count and fan_out_counterparty_count;
- new_counterparty_flag;
- recurring_payment_regularity;
- salary_credit_regularity;
- subscription, bill, rent and loan-payment regularity;
- round_amount_share;
- FX transaction share and conversion spread where rate authority exists.

**Account foundation:**

- end_of_day_balance;
- average_daily_balance;
- minimum_daily_balance;
- maximum_daily_balance;
- balance_slope;
- normalized_balance_slope;
- balance_volatility;
- maximum_balance_drawdown;
- debit_turnover and credit_turnover;
- net_account_flow;
- available_balance;
- limit_headroom;
- overdraft_day_count;
- maximum_overdraft_depth;
- excess_limit_episode_count and duration;
- interest_paid/charged amount;
- fee_burden_amount and fee_burden_ratio;
- account_activity_recency;
- dormant_day_count and reactivation_flag;
- account_tenure_days;
- active_mandate_count;
- primary_salary_account_flag under a governed policy;
- account_closure and switch precursor features that remain pre-outcome.

**Canonical exemplar: posted_debit_amount**

The first exemplar must demonstrate the complete contract:

- output grain account;
- source event grain transaction;
- required transaction_id, account_id, amount, direction, eligible status, booking time and currency;
- original transaction/reversal link where the source supports corrections;
- governed direction/sign, eligible-status, reversal and currency policies;
- trailing-window parameter;
- sum of eligible debit economic amount;
- additive over account and time within one currency;
- base-currency conversion only through a governed rate at the correct timestamp;
- empty window returns zero;
- null amount is rejected or ignored according to the reviewed source policy;
- all boundary cases represented in gold data.

**Steps:**

- [ ] Author tranche A one output at a time.
- [ ] Reuse formula fragments only as code helpers; every recipe retains a distinct reviewed expectation and output contract.
- [ ] Add tranche B only after each required Formula-v2 operation is accepted.
- [ ] Add account-to-customer rollups as separate recipes with allocation policy, never by changing the account recipe grain.
- [ ] Add source eligibility examples for core ledger, card authorization, payment hub and ATM feeds.
- [ ] Require a policy choice when sources use signed values versus unsigned amount plus direction.
- [ ] Add performance tests for wide transaction tables and large parameter schemas.

**Acceptance:**

- At least one complete transaction pack can be grounded, formula-authored, gold-validated and compiled end to end.
- Reversals and failed transactions cannot silently inflate posted activity.
- Mixed currencies cannot be summed without a governed conversion.
- Account and customer grains are not conflated.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_transaction_foundation.py tests/featuregen/overlay/upload/recipes/test_account_foundation.py -v
- Run the recipe formula shadow/evaluation gate for the new packs.

**Commit series:** feat(recipes-foundation): add executable transaction and account primitives

---

## Task BR-19: Add the Customer, RBWM and Wealth expansion packs

**Purpose:** Build customer and relationship features from validated account/product primitives rather than re-deriving weak composites from generic columns.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/customer.py
- Create: src/featuregen/overlay/upload/recipes/rbwm.py
- Create: src/featuregen/overlay/upload/recipes/wealth.py
- Create corresponding tests and gold cases
- Modify taxonomy coverage targets

**Customer foundation:**

- customer_relationship_tenure;
- active_account_count by account/product class;
- active_product_family_count;
- relationship_balance and relationship_revenue using governed allocation;
- channel_active_day_count and channel diversity;
- customer_activity_recency;
- service-interaction count by outcome;
- complaint count, unresolved complaint count and resolution duration;
- verified household-member count;
- address/contactability quality only where privacy policy permits;
- customer-level rollups of account foundation features.

**RBWM product packs:**

- CASA operating-balance and salary-anchoring features;
- card utilization, revolve, payment and merchant behavior;
- mortgage payment, offset-account, prepayment and refinance behavior;
- personal-loan repayment and early-settlement behavior;
- overdraft usage, excess and cure behavior;
- deposit attrition and primacy-loss signals;
- acquisition, activation and first-90-day engagement;
- direct-debit, standing-order and recurring-bill continuity;
- digital adoption and assisted-service migration;
- service failure and recovery;
- financial-health and vulnerability indicators behind explicit policy and purpose controls.

**Next-best-action rules:**

- deterministic eligibility and exclusion are policy features;
- product propensity and uplift are ModelFeatureSpec outputs;
- a recommendation requires suitability, affordability, consent, channel availability and contact policy;
- campaign response requires a treatment record and cannot be used as a pre-treatment predictor without lagging;
- primary versus supporting use cases are explicit.

**Wealth pack:**

- net contribution and withdrawal flow;
- asset outflow and cash-drag behavior;
- portfolio concentration and diversification;
- risk-profile versus portfolio-risk mismatch;
- suitability-review due/overdue state;
- advisor interaction recency;
- mandate breach proximity;
- fee burden;
- realized/unrealized performance with valuation and FX policy;
- client and household rollups with effective membership.

**Steps:**

- [ ] Author account-level primitives first and customer rollups second.
- [ ] Require joint-account and household allocation policies.
- [ ] Separate deterministic historical behavior from prediction and recommendation.
- [ ] Add privacy/suitability gates to vulnerability and wealth recipes.
- [ ] Add explicit primary recipes for deposit attrition, primacy loss, customer segmentation, campaign and wealth asset outflow where approved.
- [ ] Add model-governance references for propensity, CLV, uplift and churn scores.

**Acceptance:**

- A customer feature can explain exactly which accounts and allocation rules contributed.
- No next-best-action suggestion bypasses eligibility or suitability.
- Wealth performance carries valuation, benchmark, fee and currency basis.
- Previously supporting-only RBWM/wealth objectives gain reviewed primary recipes where data permits.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_customer.py tests/featuregen/overlay/upload/recipes/test_rbwm.py tests/featuregen/overlay/upload/recipes/test_wealth.py -v

**Commit series:** feat(recipes-rbwm): add governed customer, retail and wealth packs

---

## Task BR-20: Add the CIB and Transaction Banking expansion packs

**Purpose:** Extend beyond the corrected current corporate recipes into the client, account, liquidity, trade and profitability capabilities expected from a CIB data product.

**Files:**

- Create: src/featuregen/overlay/upload/recipes/cib_client.py
- Create: src/featuregen/overlay/upload/recipes/transaction_banking.py
- Create: src/featuregen/overlay/upload/recipes/trade_finance.py
- Create: src/featuregen/overlay/upload/recipes/cib_risk.py
- Create corresponding tests and gold cases
- Modify taxonomy coverage targets

**CIB client and profitability:**

- operating_balance_average and volatility;
- product revenue by line;
- net relationship revenue;
- direct and allocated service cost;
- economic profit and RAROC only with capital/cost policy;
- revenue wallet and wallet share only with governed external/estimated denominator;
- relationship depth and product penetration;
- limit utilization and excess behavior;
- client/account/facility profitability rollups;
- onboarding/KYC completion and periodic-review state.

**Transaction banking and liquidity:**

- payment count/value by rail, corridor, currency and purpose;
- inbound/outbound concentration;
- operating-account primacy;
- cash-position volatility;
- intraday low/peak and liquidity usage;
- notional/physical pool sweep behavior;
- virtual-account utilization;
- receivables/payables flow;
- cash-concentration and payment-factory adoption;
- cross-border and FX conversion behavior.

**Trade finance and working capital:**

- LC issuance, utilization, amendment and expiry;
- guarantee issuance, utilization and claim;
- documentary discrepancy and processing duration;
- invoice approval, DSO, delinquency, dilution and debtor concentration;
- SCF program utilization and approved-to-paid duration;
- trade-flow trend;
- AR/AP/inventory cycle components;
- working-capital gap;
- contingent-to-funded conversion.

**CIB risk and treasury sales:**

- group and single-obligor exposure;
- collateral and guarantee coverage;
- covenant headroom;
- maturity wall;
- refinancing concentration;
- counterparty and wrong-way risk indicators;
- FX exposure and hedge ratio;
- hedge effectiveness under a documented methodology;
- facility cross-product stress;
- treasury-product adoption and revenue, without turning sales outcomes into predictors.

**Steps:**

- [ ] Define legal party, obligor, client group, account, facility, instrument and pool grains explicitly.
- [ ] Require legal hierarchy and allocation policies for every group rollup.
- [ ] Require intraday timestamps for intraday outputs.
- [ ] Separate contractual lifecycle events from accounting balances.
- [ ] Add primary coverage for receivables finance, cash management, obligor monitoring and credit mitigation.
- [ ] Keep RAROC, wallet and hedge effectiveness blocked until their denominators/methodologies are governed.

**Acceptance:**

- CIB features distinguish party, group, account, facility, instrument and pool grains.
- Trade-finance outputs follow instrument lifecycle rather than generic exposure.
- Profitability metrics state capital, cost and allocation methodologies.
- Intraday liquidity cannot compile from end-of-day data.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/recipes/test_cib_client.py tests/featuregen/overlay/upload/recipes/test_transaction_banking.py tests/featuregen/overlay/upload/recipes/test_trade_finance.py tests/featuregen/overlay/upload/recipes/test_cib_risk.py -v

**Commit series:** feat(recipes-cib): add client, transaction-banking, trade and risk packs

---

## Task BR-21: Add the remaining banking ecosystem packs by governed priority

**Purpose:** Complete high-value gaps without lowering the production admission standard.

**Files:** Create one module and test module per accepted pack under recipes and tests/recipes. Update coverage targets only after SME approval.

**Priority A — lending and credit lifecycle:**

- application and origination;
- affordability and disposable-income components;
- bureau and application bitemporality;
- vintage/month-on-book;
- utilization and payment behavior by product;
- prepayment/refinance;
- SME and obligor monitoring;
- collateral/guarantee mitigation;
- cure, hardship, workout and recovery.

**Priority B — Treasury/ALM:**

- deposit behavioral runoff;
- NII and EVE sensitivity;
- repricing gap and basis risk;
- wholesale funding and maturity concentration;
- intraday liquidity;
- encumbrance;
- FTP contribution;
- cash-management balances;
- LCR and NSFR component contribution.

**Priority C — financial crime:**

- card fraud;
- account takeover;
- APP scam;
- synthetic identity;
- merchant fraud;
- mule-account network;
- structuring;
- sanctions/screening;
- KYC/CDD;
- correspondent banking;
- TBML only after trade-document and goods/shipment semantics exist.

**Priority D — operations and servicing:**

- straight-through-processing rate;
- manual-touch count;
- exception/rework count;
- processing duration and SLA breach;
- queue age and workload;
- cost per case/transaction;
- complaints and service recovery;
- data-quality and reconciliation break metrics.

**Priority E — pricing and profitability:**

- fee realization and waiver;
- deposit-rate pricing;
- credit risk-based pricing;
- relationship pricing;
- product/customer margin;
- cost-to-income;
- economic profit;
- pricing exceptions.

**Priority F — specialist banking:**

- wealth client attrition and asset outflow;
- fund liquidity and performance;
- custody settlement and corporate actions;
- insurance underwriting, lapse and claims;
- Islamic product/accounting lifecycle;
- ESG climate transition, physical risk and financed emissions;
- regulatory reporting and data-quality control features.

**Admission rule:**

No pack is added merely to turn a zero-coverage leaf green. Each proposed recipe must identify:

- an end-user decision;
- a correct atomic output;
- the exact source and output grain;
- a formula or model-governance path;
- data prerequisites;
- point-in-time and knowledge-time semantics;
- regulatory/privacy/suitability constraints;
- a named SME owner;
- gold cases.

**Acceptance:**

- Pack priority is based on user value and data readiness, not taxonomy vanity.
- New recipes enter directly as V2; the legacy adapter is unavailable to them.
- Each added primary objective has at least one non-vacuous gold case and a named owner.

**Commit series:** feat(recipes-domain): add approved banking packs through Recipe Contract v2

---

## Task BR-22: Build the banking semantic gold corpus and adversarial test suite

**Purpose:** Test the meaning of the recipes, not only registry shape and route serialization.

**Files:**

- Create: tests/featuregen/recipes/gold/
- Create: tests/featuregen/recipes/fixtures/
- Create: tests/featuregen/recipes/test_gold_execution.py
- Create: tests/featuregen/recipes/test_property_invariants.py
- Create: tests/featuregen/recipes/test_leakage_boundaries.py
- Create: tests/featuregen/recipes/test_currency_and_sign.py
- Create: tests/featuregen/recipes/test_temporal_knowledge.py
- Create: tests/featuregen/recipes/test_grain_and_rollup.py
- Create: tests/featuregen/recipes/test_registry_mutation.py
- Extend API and frontend contract tests

**Synthetic datasets:**

- transaction ledger with posted, pending, failed, reversed, refunded and returned rows;
- card authorization feed separate from settlement feed;
- multi-currency account with changing rates;
- balance snapshots with duplicates and late corrections;
- joint accounts and effective household membership;
- facility, schedule, payment allocation, delinquency and collateral;
- mandate, payment instruction, return and cancellation;
- fraud device/card reissue and beneficiary history;
- AML corridor, threshold and risk-rating changes;
- deposit classification and contractual maturity;
- trade, netting set, CSA, collateral and valuation;
- invoice, SCF, LC and guarantee lifecycle;
- fund/share-class/NAV/flow lifecycle;
- policy, premium, claim and reinsurance lifecycle;
- Islamic contract and installment schedule;
- ESG reporting boundary and methodology versions.

**Test layers:**

1. **Schema:** invalid recipe definitions cannot construct.
2. **Lint:** debt counters, output count, parameter identity and objective ownership.
3. **Grounding:** exact, missing, optional, ambiguous and incompatible operand behavior.
4. **Authority:** sign, status, currency, temporal, grain and relationship facts.
5. **Formula:** canonicalization, authoring, critic, capability and replay.
6. **Row semantics:** expected output from synthetic rows.
7. **PIT:** future and late-known rows never enter earlier cutoffs.
8. **Leakage:** outcome and near-label fields are refused in disallowed modelling stages.
9. **Rollup:** allocation and cardinality prevent double counting.
10. **API:** v1/v2 compatibility and strict v3 contract.
11. **UI:** readiness language, blockers, variants and accessibility.
12. **Performance:** bounded recipe grounding and variant selection.

**Property invariants:**

- adding an ineligible failed transaction cannot change a posted-transaction feature;
- adding an exact reversal neutralizes the original event under the reviewed reversal policy;
- moving an event after the cutoff cannot change the earlier result;
- changing an unrelated currency cannot change a single-currency result;
- duplicating a dimension row cannot multiply a fact result when relationship cardinality is governed;
- splitting one customer into two accounts preserves allocated customer totals under the allocation policy;
- a ratio remains unchanged when numerator and denominator are scaled equally;
- an additive flow aggregates over disjoint time partitions;
- a semi-additive stock does not sum across snapshots;
- a non-additive ratio is never automatically summed.

**Adversarial mutations:**

- swap authorization and settlement status;
- bind beneficiary bank as beneficiary;
- bind deposit balance as drawn exposure;
- remove sign authority;
- change recipe measure without changing additivity;
- add an unmatched PIT parameter;
- change the formula without changing output review;
- create tied concept matches;
- change legal-group membership after cutoff;
- introduce a later model version;
- omit a response/control denominator;
- add a second currency without conversion.

**Steps:**

- [ ] Establish a small, reviewable JSON/CSV fixture format containing no real customer data.
- [ ] Build expected values independently from production formula code.
- [ ] Require at least one negative/refusal case per recipe.
- [ ] Add differential tests between authored formula and compiled artifact.
- [ ] Add mutation tests for systemic defects listed above.
- [ ] Add performance budgets for the table suggestion route and formula authoring.
- [ ] Publish gold-corpus version and content hash in evaluation artifacts.

**Acceptance:**

- The old semantic defects fail at least one test each.
- A formula implementation cannot pass by reusing the same computation to generate expected values.
- Every active executable recipe has linked gold cases.
- No fixture contains production PII or customer data.

**Verification:**

- uv run pytest tests/featuregen/recipes tests/featuregen/formula tests/featuregen/overlay/upload -v
- cd frontend && npm test

**Commit series:** test(recipes): add adversarial banking semantic and execution gold corpus

---

## Task BR-23: Add source-controlled SME review and governed activation

**Purpose:** Make recipe ownership and review visible, repeatable and revision-specific.

**Sequencing amendment (re-baseline): SCHEMA HALF DONE 2026-08-10** — RecipeReviewV1 (shipped with BR-2), the append-only `recipe_review_event` store (`recipe_review.py`: record/read/current-projection; supersedes chain validated same-recipe-only) and migration 1060 (D7 row appended in the same commit; 1034-idiom append-only guards — UPDATE/DELETE/TRUNCATE are database errors). Approval is revision-specific by LOOKUP MISS: `current_review` keys on the canonical-recipe-v2 hash, so an edited definition finds no approval with no flag to forget. Landed in R1 alongside BR-2, so the family migrations (BR-11–BR-16) have somewhere durable to record the SME decisions they produce as they produce them, instead of re-reviewing migrated families here. This task keeps the validity fold, the decision APIs, the invalidation wiring and the batch reports.

**Files:**

- Extend RecipeReviewV1 in recipe_contract_v2.py
- Create: src/featuregen/overlay/upload/recipe_review.py (schema half in R1; validity/API half here)
- Create: src/featuregen/api/routes/recipe_review.py
- Create: tests/featuregen/overlay/upload/test_recipe_review.py
- Create: tests/featuregen/api/routes/test_recipe_review_route.py
- The recipe_review_event migration ships in R1 per the sequencing amendment above (pool 1060+, D7 same-commit rule)
- Optionally add a governance screen only after the backend event model lands

**Review roles:**

- banking domain SME;
- data/semantic owner;
- formula/engineering reviewer;
- model-risk reviewer when near-label or model-produced;
- privacy/compliance reviewer when sensitive, suitability, consent or protected-purpose controls apply;
- treasury/regulatory/accounting specialist where the output depends on regulatory classification.

**Review event contents:**

- recipe_id and canonical recipe revision hash;
- output_id;
- decision: approved, changes_required, rejected, retired;
- reviewer identity and role;
- reviewed primary/supporting objectives;
- formula expectation hash;
- gold-corpus references;
- policy dependencies;
- permitted use cases and prohibited modelling stages;
- rationale and evidence references;
- timestamp and superseded review event.

**Rules:**

- Approval is revision-specific; changing formula, output, operands, temporal policy, additivity, currency or leakage constraints invalidates it.
- Wording-only changes may be classified separately but still re-revision the discovery presentation.
- A recipe cannot become production-active without current SME and engineering approval.
- Near-label and model-output recipes require the additional risk review.
- Reviewer roles are policy-controlled; recipe authors cannot self-approve every required role.
- Review does not assert predictive performance.

**Steps:**

- [ ] Implement pure review-validity calculation over recipe revision and required roles.
- [ ] Add append-only review events and current projection.
- [ ] Add read and decision APIs behind governance permissions and optimistic concurrency.
- [ ] Add audit links to formula expectation, gold cases and policy dependencies.
- [ ] Add review status to suggestion contract v3.
- [ ] Add invalidation when a recipe revision or dependency changes.
- [ ] Add batch review reports by family and readiness.
- [ ] Populate review owners for all migrated recipes before Task 24 activation.

**Acceptance:**

- No production-active recipe lacks current revision-specific approval.
- A changed formula automatically makes the previous approval stale.
- Review history remains immutable and attributable.
- Conceptual recipes can be SME-approved as ideas without becoming executable.

**Verification:**

- uv run pytest tests/featuregen/overlay/upload/test_recipe_review.py tests/featuregen/api/routes/test_recipe_review_route.py -v

**Commit series:** feat(recipe-governance): add revision-specific SME review and activation

---

## Task BR-24: Shadow, measure, canary and roll out safely

**Purpose:** Move from a source-controlled library to production behavior without a big-bang contract or identity change.

**Feature controls:**

- FEATUREGEN_RECIPE_CONTRACT_V2;
- FEATUREGEN_FORMULA_V2;
- FEATUREGEN_SUGGESTION_CONTRACT_V3;
- FEATUREGEN_RECIPE_V2_MATERIALIZATION;
- per-family activation allowlist;
- per-catalog canary allowlist.

**Shadow comparisons:**

- legacy grounded recipe versus V2 replacement;
- legacy display identity versus V3 identity;
- legacy binding versus V2 fail-closed binding;
- legacy PIT prose versus compiled temporal contract;
- legacy design status versus execution readiness;
- formula-authored output versus synthetic/controlled expected output;
- primary/supporting coverage before and after correction.

**Operational metrics:**

- registry count by readiness;
- grounded, unbuildable, ambiguous and policy-blocked counts;
- formula authoring success/refusal/technical failure;
- materialization compilation success;
- recipe suggestion latency by table width and join neighborhood;
- binding blocker frequency by concept/policy;
- parameter-variant request frequency;
- v1/v2/v3 client usage;
- stale SME review count;
- gold and shadow-gate status by recipe family;
- active primary coverage and executable primary coverage.

**Rollout stages:**

1. Audit only; no behavior changes.
2. V2 registry shadow population.
3. Contract v3 available by explicit query.
4. Internal SME/engineering users view v3.
5. Selected catalogs receive V2 suggestions, still read-only.
6. Formula-v2 authoring enabled for the transaction/account foundation.
7. Materialization enabled for approved recipes on one execution engine.
8. Expand by family after shadow and SLO gates.
9. Make v3 the frontend default.
10. Deprecate v1/v2 only after measured client retirement and a separate decision.

**Canary gates:**

- zero ambiguous required bindings emitted;
- zero PIT compilation errors;
- zero formula/gold mismatch;
- no visibility/read-scope regression;
- suggestion latency within the agreed route budget;
- no increase in unexplained empty states;
- all canary-active recipes currently SME-approved;
- rollback tested.

**Rollback:**

- Disable V2 family/canary activation without deleting V2 revisions.
- Keep v1/v2 route behavior available.
- Keep Formula-v1 authoring and stored artifacts untouched.
- Stop new materialization while preserving audit records.
- Never rewrite historical feature or suggestion identities.

**Steps:**

- [ ] Add flags and family/catalog allowlists with frozen configuration tests.
- [ ] Add dual-run comparison records without exposing V2 results to ordinary clients.
- [ ] Add dashboards and structured blocker metrics.
- [ ] Run a representative catalog set: retail ledger, cards, payments, lending, CIB, markets and one specialist source.
- [ ] Conduct SME acceptance using real metadata but no production row data where row access is unavailable.
- [ ] Execute formula evaluation with real provider evidence where required.
- [ ] Exercise rollback in a non-production environment.
- [ ] Promote families individually; do not promote the entire registry from an aggregate pass rate.
- [ ] Update runbooks and support diagnostics.

**Acceptance:**

- V2/v3 can be disabled without data loss or historical corruption.
- Every promoted family passes its own semantic, formula, authority and operational gates.
- Production metrics describe readiness truthfully and never use suggestion count as success.
- Legacy contract retirement, if desired, is a later explicitly approved project.

**Verification:**

- Full backend and frontend suites.
- Migration re-apply suite.
- Formula evaluation and shadow gates.
- Canary smoke tests on representative catalog metadata.
- Rollback rehearsal.

**Commit series:** feat(recipes-rollout): shadow and canary Recipe Contract v2 by family

---

## Recommended PR and commit boundaries

Do not implement this plan as one branch or one giant migration. The minimum safe boundaries are:

1. Audit and ratchet only.
2. Contract V2 types and canonicalization only.
3. Identity and bounded parameter selection.
4. Temporal compiler.
5. Binding fail-closed behavior.
6. Formula-v2 operation increments, one capability group per PR.
7. Readiness fold and formula evidence.
8. Suggestion v3 backend contract.
9. Suggestion v3 frontend.
10. Coverage correction.
11. Banking concepts and operational policies in reviewed groups.
12. One recipe family migration per PR where practical.
13. Foundation pack tranches.
14. Gold/adversarial corpus increments.
15. Governance/activation.
16. Shadow/canary rollout.

Every PR must state:

- which invariant it enforces;
- which old contract remains unchanged;
- which debt count decreases;
- which new reason/readiness codes are introduced;
- exact test commands and results;
- rollback or compatibility behavior.

## Dependency and parallel-work map

| Workstream | Can start after | Must finish before |
|---|---|---|
| Audit/ratchet | immediately | all content expansion |
| Recipe Contract v2 | audit interfaces frozen | family migration |
| Identity/variants | V2 parameter model | suggestion v3 |
| Temporal compiler | V2 temporal model | executable family migration |
| Binding authority | V2 operand model | executable family migration |
| Formula-v2 | V2 output/parameter semantics | formula validation of complex recipes |
| Suggestion v3 backend | V2/readiness shapes | frontend v3 |
| Frontend v3 | backend fixture frozen | v3 default rollout |
| Taxonomy coverage | V2 primary objective | active registry cutover |
| Family correction | V2, temporal and binding | legacy debt zero |
| New foundation packs | V2 and minimum Formula-v2 | RBWM/CIB composites |
| SME governance schema (events + migration) | revision hashing stable (R1, with BR-2) | family migration BR-11 |
| SME governance decisions (validity fold + APIs) | review schema populated | production activation |
| Rollout | family gates and review | default-client switch |

## Principal risks and controls

| Risk | Control |
|---|---|
| Recipe identity churn breaks historical references | V1 hashes remain immutable; explicit aliases; v3 identity only |
| Formula-v2 becomes an unbounded DSL project | Minimum operation set, versioned capability, unsupported remains valid state |
| Parameter expansion harms route performance | Default-only grounding plus bounded explicit variant resolution |
| More strict binding causes fewer suggestions | Honest blocker states and governance actions; never weaken correctness to recover count |
| SME migration becomes a bottleneck | Family ownership, atomic PRs, source-controlled review checklist and debt dashboard |
| Coverage falls after removing supporting inflation | Report the truth; prioritize valuable primary recipes rather than restoring vanity coverage |
| Source systems lack sign/status/currency policies | FORMULA_BLOCKED state and policy onboarding; no silent inference |
| Model features get forced into deterministic formulas | Separate ModelFeatureSpec and model-governance path |
| New UI overstates formula validation | Closed readiness vocabulary and explicit non-predictive disclaimer |
| Migration collides with concurrent DB plans | Allocate the next free migration number at implementation time; do not reserve one in this document |

## Final program acceptance checklist

- [ ] Recipe audit strict mode is green.
- [ ] Active V2 registry contains no multi-output, unassessed or legacy-inferred recipe.
- [ ] All V2 identity combinations are collision-free.
- [ ] All temporal declarations compile and render without placeholders.
- [ ] Ambiguous required operands are blocked.
- [ ] Direction, status, reversal, currency and relationship policies are governed where needed.
- [ ] Every executable recipe has a reviewed formula expectation and gold corpus.
- [ ] Suggestion v3 communicates execution readiness and blockers.
- [ ] Primary and supporting coverage are reported separately.
- [ ] All fifteen current families have been corrected or explicitly retired.
- [ ] Transaction and account foundation packs are materialization-ready on at least one engine.
- [ ] Customer/RBWM and CIB packs have reviewed primary coverage.
- [ ] Every active production recipe has current revision-specific SME approval.
- [ ] Shadow, canary, performance, read-scope and rollback gates pass.
- [ ] No existing registered feature, Formula-v1 artifact or suggestion v1/v2 client is broken.
