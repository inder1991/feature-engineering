# Semantic-Eligibility Feature Generation — End-to-End Implementation Plan

> **SUPERSEDED FOR REMAINING WORK (2026-08-13):** the engine tranches (SE-0…SE-13 landed slices) stand as recorded in the acceptance rows below, but ALL remaining/unfinished clauses are superseded by `docs/superpowers/plans/2026-08-13-semantic-activation-and-one-engine-remediation.md`, authored from the validated external final review (27/29 findings confirmed at `333eaa3c`). That plan owns activation enforcement, the one-engine cutover, metadata enforcement, option-decision identity, the gold corpus, and the legacy-path deletion.

**Date:** 2026-08-11  
**Status:** Implementation plan only; no feature implementation is included in this artifact.  
**Second adversarial pass:** 2026-08-11 — every named file and interface verified against code, the live cluster measured. Folded: the measured provisional-flood gap (SE-0 gates + new task SE-4b + staged floors in SE-5), the SE-14 flag-shape bug, the Tranche-1 verdict-persistence bug, and nine missing implementation details (user-definition adapter, operand-class map, ranking design, real gate owners, migration reservations, suggestion-v4 mechanics, formula-readiness-at-birth, the egress seam, model-output binding behavior).  
**Rebased:** 2026-08-11 against `83488929` after architecture review — four corrections applied: (1) baseline re-audited against the completed BR-1..24 program (registry 191 → 317, BR-23 review governance and BR-24 rollout controls now exist and are deployed); (2) SE-14's rollout mode folded into the BR-24 control family instead of a parallel mechanism; (3) SE-2/SE-3's frozen-context scope rule specified (two-layer context, concept-closure shortlisting) with measured budget gates that precede merge; (4) delivery re-sequenced into three tranches so the recipe half ships user value while the LLM-intent half proves itself in shadow, and SE-10 pins the BR-23 review-validity fold at generation time.  
**Primary workflow:** hypothesis submission → governed scope → recipe and LLM feature candidates → semantic eligibility → physical binding → validation → human selection → governed feature contract.  
**Secondary workflow:** deterministic Suggested Features, which has no hypothesis and no LLM, reusing the same recipe eligibility and binding machinery.

## 1. Code baseline used for this plan

This plan was checked against the latest code present in the `feature/asset-detail-reapply` worktree, and **rebased against `83488929`** — `feat(review-ui): recipe-review screen — the BR-23 sign-off surface in the browser` — the head at which the entire BR-1..24 banking-recipe program is complete, merged to `main`, and deployed to the kind cluster.

What landed between the original review baseline (`6fee764e`, BR-17) and this rebase, all of which this plan now consumes rather than proposes:

- **BR-18..21:** the remaining family packs — the registry grew from 191 to 317 atomic recipes; the legacy-debt counter reached 0; coverage census is 75 AUTHORED_PRIMARY + 13 INTENTIONALLY_EMPTY with zero gaps pinned.
- **BR-22:** the gold execution corpus (hand-computed fixtures, property invariants, 12-mutation adversarial battery, leakage-boundary tests) — the semantic-confusion fixtures SE-4 needs partially exist here already.
- **BR-23:** the recipe-review store (migrations 1060+1061, append-only, revision-hash-keyed), the validity fold (`recipe_review_validity.py`), the review routes, and the Recipe reviews UI — live on the cluster. Review state is now a real queryable input, which SE-10 pins.
- **BR-24:** the rollout-control family (`recipe_rollout.py`): flags with frozen stage-honest defaults, per-family/per-catalog allowlists, the eight-reading `canary_gate` fold whose unmeasured inputs default to failing, and the operator runbook. SE-14 now extends this family instead of inventing a parallel mode mechanism.

Measured registry state at the rebased head (`83488929`; re-measure and re-pin in SE-0 if implementation starts from a later head):

| Measure | Current value |
|---|---:|
| Legacy templates retained for compatibility | 157 |
| Atomic V2 recipes | 317 |
| Legacy IDs with explicit V2 aliases | 157 |
| Deterministic-formula V2 recipes | 298 |
| Conceptual-pattern V2 recipes | 11 |
| Governed-model-output V2 recipes | 8 |
| Registered model-feature specs | 8 |
| Readiness: FORMULA_AUTHORABLE / FORMULA_BLOCKED / CONCEPTUAL_ONLY | 3 / 295 / 19 |
| V2 operands | 1,195 |
| Operands requiring declared authority for suggestions | 1,195 |
| Operands requiring governed authority for execution | 1,195 |
| Operands with an explicit economic role | 41 |
| Operands with an explicit relationship requirement | 6 |

The numbers matter because the implementation should consume the existing V2 contracts. It must not create another recipe schema or another independent recipe binder. Where a task or gate below counts recipes, the count must be derived from `len(V2_RECIPES)` at run time — never a hardcoded literal that stales the way the original 191 did.

**Measured live-catalog metadata state (kind cluster, 2026-08-11 — the fact the whole program stands on):**

| Measure | Current value |
|---|---:|
| Concept facts in `field_evidence` | 560 |
| …of which producer `llm`, strength `proposed` | **560 (100%)** |
| …declared or human-confirmed | **0** |
| Operand suggestion floors requiring at least `declared` | 1,195 of 1,195 |

The consequence is arithmetic, not opinion: under the section-7 authority matrix, **no operand binding on the current catalog can ever be `eligible` — every candidate everywhere lands `provisional`.** The plan calls this the provisional flood. It is addressed structurally (not hoped away) by three additions from the second adversarial pass: SE-0 measures and gates on this distribution, SE-4b builds the authority-bootstrap funnel that clears it at usable throughput, and SE-5 stages authority-floor enforcement behind shape/meaning enforcement so nothing user-visible degrades while the funnel fills. The approved live-catalog enrichment run — which has still never been executed — is a named precondition: a column with no concept at all can never enter a shortlist under the concept-closure rule.

## 2. Executive decision

The platform does **not** primarily have a metadata-delivery problem anymore. The current feature-context V5 path already sends rich column and table context to the LLM, including concepts, definitions, AI summaries, domain and sub-domain, business terms, BIAN/FIBO/process paths, party role, identifier namespace, table role, event-or-snapshot classification, relationships, operational facts, missing-context codes, and producer/strength authority labels.

The remaining quality problem is that this metadata is mostly informative rather than load-bearing:

1. The free-form LLM sees physical columns and decides both **what feature to invent** and **which physical columns to use** in the same call.
2. The relevance selector reduces prompt size by token overlap; it does not decide whether a column is semantically allowed to serve a particular operand role.
3. The deterministic validator checks useful structural conditions, but an LLM candidate has no authoritative typed operand declaration. Its `measure_refs` currently default to every `derives_from` pair.
4. The model's grounding notes are deliberately explanatory and cannot influence disposition.
5. The live hypothesis recipe lens still grounds the compatibility `ALL_TEMPLATES` registry. BR-17 makes V2 execution truth visible on Suggested Features contract V3, but it does not yet make `RecipeDefinitionV2` the direct Gate-1 grounding input.
6. A metadata snapshot is persisted only after candidate generation and validation. That records candidate inputs, but it does not make the earlier semantic selection a pure replay over a frozen context.

The implementation must split feature generation into two distinct decisions:

```text
WHAT should be computed?                 WHICH governed data can compute it?
------------------------------------     -------------------------------------------
LLM abstract FeatureIntent     ─────┐    semantic-capability compiler
                                    ├──> role-aware semantic eligibility
V2 RecipeDefinition            ─────┘    deterministic binding and source planning
                                          formula/PIT/safety validation
```

The LLM may propose feature meaning. It must no longer be the authority that assigns physical columns to load-bearing operand roles. Recipes and LLM ideas must converge on one deterministic planning request, one semantic-eligibility engine, one source/join planner, one validation vocabulary, and one persisted audit artifact.

## 3. Desired user outcome

After implementation, a user who submits a banking hypothesis should receive fewer but materially more credible features. Each candidate should answer five questions without requiring the user to reverse-engineer the catalog:

1. **What does this feature measure?**
2. **At what entity grain and time basis is it computed?**
3. **Which physical columns fill each required role, and why were they eligible?**
4. **What is proven, what is only proposed, and what still requires a check or human decision?**
5. **Is this a useful idea, a design-checked computation, a formula-ready feature, or a materializable feature?**

The system must stop producing superficially plausible banking features from semantically wrong columns. For example, a consent-modification timestamp must not become a transaction-activity timestamp, a KYC-completion date must not become customer tenure, a package code must not become product breadth, and one bureau date on a customer snapshot must not become bureau-event velocity.

## 4. Non-negotiable invariants

1. **No physical references in authoritative LLM intent output.** The LLM describes the computation in typed semantic roles. A deterministic stage binds those roles to columns.
2. **One planning contract for both origins.** A V2 recipe and an LLM feature intent normalize to the same `FeaturePlanningRequestV1` before any physical binding.
3. **One binding verdict per operand.** Formula generation, Suggested Features, Gate-1 candidates, and downstream contract drafting must consume the same persisted verdict rather than recomputing or guessing.
4. **Authority travels with every semantic value.** A value without its producer, strength, lifecycle, operational influence, and evidence reference cannot clear a role requirement.
5. **LLM-proposed metadata may retrieve and rank; it may not silently clear a declared or governed requirement.** It can yield a useful provisional candidate with a named action.
6. **A known contradiction blocks.** Missing evidence and contradictory evidence are different conditions and must have different codes and user actions.
7. **Table shape is load-bearing.** A snapshot cannot satisfy an event-stream requirement merely because it contains a date column.
8. **Business-event subtype is load-bearing.** `consent_modified_at`, `kyc_completed_at`, and `transaction_event_at` are all timestamps but are not interchangeable.
9. **Identifiers are never measures by default.** An entity identifier can bind an entity-key, join-key, grouping, or distinct-count role only when the feature intent explicitly needs that role.
10. **No prose parsing in a clearing path.** Definitions, AI summaries, business terms, BIAN paths, and grounding explanations may improve retrieval and display. A deterministic typed fact or governed mapping must clear the role.
11. **No hidden source choice.** Population, event source, reference source, and mapping source decisions use existing source-selection contracts or remain explicitly unresolved.
12. **No hidden join.** The exact governed relationship path and directional cardinality used by a candidate are persisted and shown.
13. **No post-selection recomputation.** Gate-1 drafting and confirmation consume the chosen candidate's frozen binding plan and semantic decision hashes.
14. **Validation and execution readiness remain separate axes.** `DESIGN_CHECKED` does not mean formula-validated or materialization-ready.
15. **Every refusal and provisional outcome is actionable.** The UI says whether the remedy is metadata confirmation, data profiling, relationship governance, policy setup, another source, formula authoring, or no possible fix.
16. **Read scope applies before capability compilation.** Hidden columns and relationships are absent, not reported as inaccessible alternatives.
17. **Old contracts remain verifiable.** V1/V2 suggestion identities, historical Template hashes, and stored considered revisions are not reinterpreted under new semantics.

## 5. Current workflow versus target workflow

### 5.1 Current hypothesis workflow

1. `POST /contract/intake` extracts a target ticket from the hypothesis.
2. The human confirms or corrects the target through `POST /contract/intake/target`.
3. `POST /contract/recognitions` classifies the redacted hypothesis and objective into the governed use-case taxonomy. It correctly sees no catalog columns.
4. The human confirms the proposed use-case scope and soft dimensions.
5. `POST /contract/considered-set` seals the scope and starts generation.
6. `build_considered_set` calls `recommend_feature_sets_report`.
7. `_candidate_columns` loads the read-scoped physical column universe.
8. `select_relevant_context` includes mandatory grain/as-of/entity columns and ranks the rest by token overlap.
9. The LLM returns a feature name, physical `derives_from`, free-text aggregation, grain table, description, rationale, and explanatory grounding.
10. `_validate_idea` grounds those physical refs and applies leakage, freshness, use, type, additivity, unit, currency, temporal, grain, and join checks.
11. Separately, the recipe lens grounds legacy `Template` objects and submits their physical bindings to the same validator.
12. The two sources are combined, ranked, persisted, and shown in Workbench.

The weakness is at steps 8–10: token relevance is mistaken for semantic eligibility, and the LLM's physical selection arrives before typed role validation exists.

### 5.2 Target hypothesis workflow

1. Keep intake, target confirmation, recognition, and human scope confirmation.
2. At generation-run start, assemble one read-scoped, repeatable-read `GenerationSemanticContextV1`. Freeze it in memory before model dispatch and seal its hashes durably before the considered revision or response is written.
3. Retrieve applicable V2 recipes directly from `V2_RECIPES` using their authored objectives.
4. Ask the LLM for abstract, typed `FeatureIntentV1` objects. Give it a bounded semantic capability inventory, not physical object references.
5. Normalize each V2 recipe and each LLM intent into `FeaturePlanningRequestV1`.
6. Compile every visible column/table into a typed `ColumnCapabilityV1` using the shared semantic bundle, operational facts, dataset profile, and relationship state.
7. Evaluate every required operand against capabilities using the same deterministic authority and banking-role policy.
8. Produce a bounded shortlist for each operand; keep eligible, provisional, blocked, and ambiguous outcomes separate.
9. Use existing source, temporal, join, and cross-catalog planners to assemble complete physical binding plans. Never let an operand choose a source independently of the whole feature.
10. Compile the V2 temporal declaration and the closed formula operation. Run the existing safety/use/leakage checks over typed measures, keys, filters, and time anchors rather than all refs as measures.
11. Persist the full decision, including losing candidates and reason codes, into the immutable considered revision.
12. Merge semantically equivalent recipe and LLM candidates. Prefer the recipe realization when it carries reviewed SME semantics; retain a genuinely different LLM intent.
13. Show the human a compact candidate card with progressive evidence and actionable blockers.
14. Gate-1 selection records the exact option and frozen plan. Drafting and confirmation revalidate freshness, not meaning.

## 6. Contracts to add

The following names are proposed as explicit implementation contracts. Exact module placement is specified later.

### 6.1 `FeatureIntentV1`

This is the LLM's allowed output. It describes a computation without naming physical data.

Required fields:

- `intent_id`: content-addressed ID over the full intent body.
- `display_name` and `business_definition`.
- `primary_objective` and optional `supporting_objectives`, restricted to confirmed scope.
- `computation_kind`: `deterministic_formula`, `conceptual_pattern`, or an existing registered `governed_model_output`.
- `output_id`, `output_type`, `unit_kind`, and `output_additivity`.
- `output_grain_entity`.
- `operation`: a closed Formula V2 operation/expression for `deterministic_formula`; absent for an honest conceptual pattern. No arbitrary aggregation prose.
- `operands`: ordered `RequiredOperandV1` values.
- `temporal`: typed anchor kind, window basis/unit/value, cutoff inclusivity, and knowledge-time requirement.
- `eligibility`: structured included/excluded event/status semantics and policy references.
- `currency_policy`, `null_policy`, `empty_population_policy`, and ratio denominator policy when applicable.
- `leakage_classification` and permitted/prohibited modelling stages.
- `rationale`: explanatory only.
- `generation_provenance`: prompt/schema/model/call reference and confirmed-scope hash.

Construction rules:

- a deterministic formula requires a closed, type-valid operation/expression;
- a conceptual pattern requires a named `conceptual_reason` and cannot claim formula readiness;
- a governed model output must resolve an existing, versioned model-feature spec supplied in the capability inventory; the LLM may not invent one;
- an unsupported but valuable idea is retained as conceptual with a formula-capability blocker, not forced into a misleading generic aggregation.

Forbidden fields:

- catalog source;
- table name;
- column name or object reference;
- SQL or expression text;
- invented policy IDs;
- free-text operand roles;
- a confidence score used as authority.

### 6.2 `RequiredOperandV1`

One exact role the feature needs:

- `role` and `operand_class`: measure, entity key, event timestamp, as-of timestamp, dimension, status, direction, policy input, currency, or relationship key;
- one primary controlled concept and optional controlled alternatives;
- `required` and multiplicity;
- allowed source grains;
- join role and temporal role;
- expected economic role where a generic financial concept is insufficient;
- expected unit kind, currency behavior, sign/direction semantics, and additivity behavior;
- relationship requirement and directional cardinality requirement;
- suggestion authority floor and execution authority floor;
- distinct-binding group;
- optional governed policy reference.

Where possible, this structure should be the neutral projection of existing `OperandSpecV2`, not a separately hand-maintained vocabulary.

### 6.3 `FeaturePlanningRequestV1`

The origin-neutral input to physical planning:

- source origin: `recipe_v2`, `llm_intent`, or `user_definition`;
- source definition ID, revision, and content hash;
- confirmed objective/scope IDs;
- atomic output contract;
- ordered operands;
- temporal, eligibility, leakage, and policy declarations;
- parameters and their resolved, identity-bearing values;
- computation kind and formula reference when one exists.

Adapters:

- `planning_request_from_recipe(definition, variant)`;
- `planning_request_from_feature_intent(intent)`;
- a restricted user-definition adapter that produces typed unresolved roles rather than passing physical refs straight through.

### 6.4 `ColumnCapabilityV1`

One immutable, read-scoped statement of what a physical column may contribute:

- source, schema-preserving logical ref, graph ref, table ref;
- physical and governed logical types;
- controlled concept and concept ancestry;
- entity and party role;
- identifier namespace and issuer scope;
- possible operand classes;
- economic role and sign/direction semantics;
- additivity, unit, and currency;
- grain/as-of status with authority;
- business-event type and temporal role;
- table source grain, data role, authority role, temporal storage model, primary entity, and event-or-snapshot classification;
- sensitivity/use-policy posture;
- relationship and cardinality facts;
- semantic-context and dataset-profile hashes;
- `missing_context` and conflict markers;
- evidence pins per value.

Every capability property must carry an authority class. The compiler may derive safe structural capabilities, such as “varchar is non-numeric,” but must not derive a banking meaning by parsing prose.

### 6.5 `OperandEligibilityVerdictV1`

One `(planning request, operand, column)` decision:

- status: `eligible`, `provisional`, `blocked`, or `not_applicable`;
- machine reason codes;
- matched controlled facts;
- authority floor required and authority observed;
- evidence/fact/decision IDs and content hashes;
- missing checks;
- human or operator resolution action;
- policy version and input hash.

Proposed initial reason-code groups:

| Group | Example codes |
|---|---|
| Meaning | `CONCEPT_MISMATCH`, `OPERAND_CLASS_MISMATCH`, `ECONOMIC_ROLE_UNPROVEN`, `BUSINESS_EVENT_MISMATCH` |
| Authority | `SEMANTIC_AUTHORITY_INSUFFICIENT`, `SEMANTIC_CONFLICT`, `PROPOSED_METADATA_ONLY` |
| Shape | `SOURCE_GRAIN_MISMATCH`, `SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW`, `IDENTIFIER_NOT_A_MEASURE` |
| Type/value | `TYPE_INCOMPATIBLE`, `ADDITIVITY_INCOMPATIBLE`, `UNIT_INCOMPATIBLE`, `CURRENCY_POLICY_MISSING` |
| Time | `EVENT_TIME_REQUIRED`, `AS_OF_TIME_REQUIRED`, `KNOWLEDGE_TIME_REQUIRED`, `TEMPORAL_POLICY_UNRESOLVED` |
| Relationship | `RELATIONSHIP_REQUIRED`, `DIRECTIONAL_CARDINALITY_UNPROVEN`, `JOIN_PATH_DENIED` |
| Governance | `PERSONAL_DATA_POLICY_REQUIRED`, `PROTECTED_CHARACTERISTIC_BLOCKED`, `STATUS_POLICY_UNRESOLVED` |
| Ambiguity | `REQUIRED_OPERAND_AMBIGUOUS`, `SOURCE_SELECTION_AMBIGUOUS`, `DISTINCT_BINDING_VIOLATED` |

These codes should map to the platform's existing product families: undecided, needs data check, structurally unsuitable, and needs setup.

### 6.6 `FeatureBindingPlanV1`

One complete physical realization. This must compose the existing `BindingPlanV1`, `DatasetSourceSelectionV1`, temporal-policy decision, and `PlanEnvelopeV1`; it is not a parallel planner model:

- planning-request ID and source provenance;
- exact ordered operand bindings and their verdict references;
- selected population, event, dimension, reference, and mapping datasets;
- dataset-profile and serving-policy hashes;
- selected physical-binding revision IDs;
- exact join/bridge path, realization hashes, and directional cardinalities;
- temporal row-selection policy and compiled PIT declaration;
- formula proposal/reference and engine capability result;
- read scope, catalog watermarks, policy versions, and semantic snapshot hash;
- overall binding state and blocker list;
- content-addressed physical plan ID.

### 6.7 Candidate state model

Do not replace several distinct axes with one badge. Persist and expose them independently:

- **Applicability:** in scope, supporting, out of scope.
- **Semantic binding:** bound, provisional, ambiguous, missing, blocked.
- **Design validation:** design checked, needs external validation, rejected.
- **Formula readiness:** conceptual only, formula blocked, formula authorable, formula validated.
- **Materialization readiness:** blocked or ready.
- **Predictive evidence:** untested, backtested, monitored; this remains outside design checking.

## 7. Semantic authority policy

Create one versioned authority matrix and make it the only policy consumed by the eligibility engine.

| Evidence/state | Retrieval/ranking | Clear suggestion binding | Clear execution binding |
|---|---:|---:|---:|
| Human confirmed/governed decision | Yes | Yes | Yes, subject to data/relationship checks |
| Source attested/declared typed fact | Yes | Yes when operand requires `declared` or lower | No when operand requires `governed` |
| Structural connector/parser fact | Yes | Yes for structural claims it can prove | Yes for that structural claim only |
| Profiler supported observation | Yes | May clear the named data check | May clear only the named data check and pinned dataset revision |
| Taxonomy-derived controlled mapping | Yes | Only if the mapping policy explicitly grants operational influence | Same rule; never by label alone |
| LLM proposed enrichment | Yes | Provisional only | No |
| Display-only current graph value | Yes | No unless its field policy grants influence | No |
| Missing | Yes, with penalty | Provisional or unresolved | No |
| Conflict, fork, hash mismatch, stale decision | No clearing | Block or needs review | Block |

Important application rules:

- A source-attested business term such as “Customer Number” improves retrieval. It does not become the controlled `customer_id` concept unless a governed glossary mapping says so.
- An AI-proposed `customer_id` concept may put a column in the entity-key shortlist, but it cannot satisfy a V2 operand whose suggestion floor is `declared`.
- A source-declared `is_as_of` fact may support a provisional suggestion if the recipe allows declared suggestion authority. It cannot prove an event timestamp or an executable PIT policy.
- Dataset `event_or_snapshot` or `table_role` values that are merely LLM proposed must never clear an event-source requirement.
- The existing LLM grounding explanation stays presentation-only and is never transformed into evidence.

## 8. End-to-end behavior using `public.bo_cib_customer.cust_num`

Assume the user submits: “Customers with declining recent transaction activity are more likely to become dormant in the next 90 days.”

### 8.1 What the current metadata says

The reviewed deployment evidence for `cust_num` includes:

- business term “Customer Number,” source-attested;
- definition identifying a corporate/institutional customer, source-attested;
- concept `customer_id`, AI-proposed;
- domain `Customer`, source-proposed;
- declared type `varchar(150)`;
- entity `customer`, hint;
- party role `subject`;
- identifier namespace scheme `cif`, issuer scope unresolved;
- source-declared grain on a customer snapshot;
- confirmed semantic link to a transaction-repository `cif_id`, but not executable because no governed realization exists;
- no governed entity assignment, no governed sensitivity, and no verified uniqueness/cardinality result.

### 8.2 Correct target behavior

1. The intake stage identifies and confirms the prediction target; `cust_num` is not silently treated as the label.
2. Recognition identifies the customer attrition/dormancy use case and target entity.
3. Applicable V2 recipes include activity recency/frequency/trend patterns. The LLM may independently propose a typed “90-day customer transaction activity trend” intent.
4. Both normalize to operands resembling:
   - customer entity key;
   - eligible transaction event;
   - transaction event timestamp;
   - optional transaction amount/count measure;
   - output as-of/cutoff.
5. `cust_num` is considered only for the customer-key role. It is never considered for the amount, event-time, or generic measure role because it is an identifier and varchar.
6. Its AI-proposed `customer_id` concept is below the recipe's current `declared` suggestion authority. The verdict is provisional with `SEMANTIC_AUTHORITY_INSUFFICIENT`, and the UI action is “Confirm this column as the governed customer/CIF identifier.”
7. `business_dt` may serve as the customer snapshot cutoff/as-of candidate, subject to its authority and temporal policy. It cannot serve as transaction event time.
8. `cust_cnsnt_mod_dt` is rejected for the activity-event timestamp role with `BUSINESS_EVENT_MISMATCH`; it means consent modification.
9. `cust_kyc_complete_dt` is rejected for customer origination/tenure unless an explicit recipe actually requests KYC completion.
10. `cust_smart_cust_pkg_cd` is rejected as a product-holding population or breadth measure; it is one package classification on a customer snapshot.
11. The confirmed semantic link to the transaction repository helps retrieve a possible event dataset, but the plan remains blocked until a governed executable relationship/realization and directional cardinality exist.
12. With no valid event source, the system does not show a fabricated dormancy or frequency trend. It shows a useful blocked candidate: “Transaction activity history is required; this customer snapshot and its dates cannot establish activity.”
13. Once a verified `cust_num → transaction.cif_id` realization, event timestamp, status policy, and event source are available, the same intent binds and advances without changing the feature's business meaning.

This behavior demonstrates the desired principle: rich metadata should prevent wrong features and explain missing prerequisites, not merely decorate a card after a wrong feature has already been selected.

## 9. Detailed implementation work breakdown

Tasks are numbered SE-0 through SE-14, plus SE-4b (the authority-bootstrap funnel, added by the second adversarial pass). Each task is independently reviewable and has an explicit acceptance gate.

### Task SE-0: Freeze the baseline and delimit BR-17 ownership

**Purpose:** Avoid implementing a second recipe cutover now that BR-17 has landed.

**Files to inspect/update:**

- `docs/superpowers/plans/2026-08-10-banking-recipe-production-readiness-and-expansion.md`
- `src/featuregen/overlay/upload/recipe_registry_v2.py`
- `src/featuregen/overlay/upload/templates.py`
- `src/featuregen/overlay/upload/suggestion_contract.py`
- `src/featuregen/overlay/upload/contract/gate1.py`
- `src/featuregen/overlay/upload/taxonomy/applicability.py`
- recipe/suggestion/contract tests

**Steps:**

1. Pin `83488929` as the rebased baseline, or re-run this audit if implementation starts from a later head. The audit was re-run once already (191 → 317 recipes); treat any further drift the same way.
2. Confirm and test the exact BR-program boundary:
   - V2 alias registry and V3 execution truth (BR-17): landed;
   - V1/V2 suggestion compatibility: retained;
   - review store, validity fold, review routes and UI (BR-23): landed and deployed — review state is a live input, not future work;
   - rollout-control family, canary-gate fold, frozen flag defaults (BR-24): landed and deployed — SE-14 extends it;
   - direct V2 Gate-1 grounding: not landed (verified at rebase: `gate1.py` still defaults to `ALL_TEMPLATES`);
   - direct V2 applicability over authored objectives: not landed;
   - direct V2 planner/formula binding: not landed (verified at rebase: `recipe_operand_policy.py` consumes `operand_class` for ambiguity codes only — `allowed_source_grains`, authority floors, join/temporal roles, and unit expectations are declared on all 1,195 operands and enforced nowhere).
3. Pin a test proving the hypothesis path still calls `ALL_TEMPLATES`; this is a migration baseline, not desired final behavior.
4. Pin the current V2 population, alias coverage, computation kinds, readiness counts, and operand authority counts — derived from the registry, not literals.
5. Freeze the current Workbench and Suggested Features response snapshots so new versions are additive.
6. **Measure and pin the live metadata-authority distribution** (first measurement recorded in §1: 560/560 concept facts are `llm/proposed`, zero declared or confirmed). This number is a release gate input: SE-4b's funnel exists to move it, and no authority floor is enforced (SE-5) until the funnel demonstrably moves it for at least one canary catalog.
7. **Measure and pin enrichment coverage** — the fraction of visible columns carrying ANY concept fact. The approved live-catalog enrichment run has never been executed; it is a precondition of the concept-closure rule and must be scheduled (with explicit operator approval — it spends LLM budget on a live cluster) before Tranche 2 shadow metrics mean anything.
8. Fold small code-truth drift found in the second pass into the audit: `recipe_applicability.py`'s docstring says "153 legacy recipes" against 157 registry templates — correct the docstring, and treat any similar count drift as an audit item, not a code review nit.

**Acceptance:** The team has one source-controlled checklist distinguishing BR-17 compatibility work from this semantic-planning program. No task below relies on ambiguous “active registry” wording.

> **ACCEPTED 2026-08-11 — commit `00d027e4`.** Audit doc: `docs/architecture/2026-08-11-semantic-eligibility-se0-baseline-audit.md`. Five boundary pins in `test_semantic_eligibility_baseline.py`. Live measurements recorded: 237 columns / 229 with active concept evidence (97% — enrichment is NOT this cluster's bottleneck, refining the plan's assumption; re-measure per target catalog) / 100% `llm/proposed`, zero declared or confirmed (authority IS the bottleneck; flood analysis confirmed). Docstring drift (153→registry-derived) corrected. Suite 10734 green.

### Task SE-1: Define and register the neutral planning contracts

**Create:**

- `src/featuregen/overlay/upload/feature_intent.py`
- `src/featuregen/overlay/upload/feature_planning_contracts.py`
- `tests/featuregen/overlay/upload/test_feature_intent.py`
- `tests/featuregen/overlay/upload/test_feature_planning_contracts.py`

**Modify:**

- `src/featuregen/contracts/contract_versions.py` or its existing registration call sites
- `src/featuregen/overlay/upload/recipe_contract_v2.py` only for a pure adapter if necessary; do not weaken V2 construction rules

**Steps:**

1. Implement the immutable contracts in section 6 with tuple-only nested members.
2. Register content versions for Feature Intent, Planning Request, Required Operand, Eligibility Verdict, and Binding Plan.
3. Canonicalize every behavior-bearing field automatically; adding a dataclass field must move its content hash.
4. Reject physical refs in `FeatureIntentV1` at construction and parse time.
5. Reuse closed vocabularies from `RecipeDefinitionV2`, Formula V2, binding roles, temporal roles, taxonomy, and concept registry.
6. Implement `planning_request_from_recipe` and prove every recipe in `V2_RECIPES` adapts successfully (317 at rebase — assert over `len(V2_RECIPES)`, never a literal, so pack growth cannot silently shrink the proof).
7. Implement `planning_request_from_feature_intent` with the same output shape.
8. Implement the third adapter §6.3 names — `planning_request_from_user_definition` — here, not later: it owns the restricted projection of a user-supplied definition into typed UNRESOLVED roles (physical refs the user typed become resolution hints for the binder, never pre-cleared bindings). SE-11's "no public endpoint bypasses enforced mode" depends on this adapter existing; `/features/recommend` and `/features/refine` are live routes today and would otherwise have no on-ramp.
9. Add mutation tests for omitted authority floors, output-changing parameters, invalid concepts, arbitrary operations, undeclared policies, and physical refs.

**Acceptance:** A recipe and an LLM intent describing the same atomic output produce structurally comparable planning requests. No caller below needs to know their origin to evaluate column eligibility.

> **ACCEPTED 2026-08-11 (pending suite gate at commit time).** `feature_planning_contracts.py` + `feature_intent.py`; contract versions `feature-planning-request@1` and `feature-intent@1` registered under one owner each; 18 tests including the all-of-`V2_RECIPES` adapt proof (registry-derived, 317 at acceptance), field-exhaustive hash proof, origin gating, strict parse with named physical-key refusals, and the convergence test (recipe vs intent → equal operands/output/temporal). Two authored deviations from the letter of §6: (1) the V2 spec dataclasses (`OutputSpecV2`/`TemporalSpecV2`/`EligibilitySpecV2`/`LeakageSpecV2`) are REUSED, not re-modeled — their construction rules ride along and a second driftable vocabulary cannot exist; (2) a deterministic LLM intent projects to planning as `conceptual_pattern` with a "formula pending" reason — the SE-6 step-11 readiness ceiling made structural: the planning contract's "deterministic ⟹ exact formula reference" invariant holds with no origin exceptions, and intents gain executability only through the governed formula seam.

### Task SE-2: Build a frozen generation semantic context before candidate selection

**Create:**

- `src/featuregen/overlay/upload/generation_semantic_context.py`
- `tests/featuregen/overlay/upload/test_generation_semantic_context.py`

**Modify:**

- `src/featuregen/overlay/upload/feature_metadata_snapshot.py`
- `src/featuregen/overlay/upload/semantic_context.py`
- `src/featuregen/overlay/upload/contract/gate1.py`
- `src/featuregen/api/routes/contract.py`
- snapshot migrations/tests if a new item kind or observation table is required

**Steps:**

1. Hoist candidate-universe loading out of each lens. One generation run loads the read-scoped column/table universe once.
2. Build the context in **two layers**, both under the existing repeatable-read connection, resolving the scope question deterministically rather than by "visible universe or a deterministically selected scope":
   - **Layer A (universal, cheap):** structural and index facts for the FULL visible universe — physical type, table membership, controlled concept (any authority), concept ancestry, entity, identifier namespace, party role, table source grain and event/snapshot classification. This is one batched read per fact family (the same shape `_candidate_columns` already loads today), and it is what operand shortlisting searches.
   - **Layer B (full capability, bounded):** complete `ColumnCapabilityV1` compilation — semantic bundles, dataset profiles, relationship state, evidence pins — computed ONLY for columns that enter an operand shortlist. The scope rule is the **concept closure**: a column is shortlisted for an operand only through a controlled-meaning match (concept, ancestry, namespace, or entity) evaluated over Layer A, and shortlists are bounded (16 candidates per role, SE-5), so Layer-B compilation is bounded by `roles × 16`, never by catalog width.
   - Determinism does not require eager compilation of everything: the repeatable-read snapshot is the authority, so lazily compiling a shortlist member inside the frozen snapshot yields the same capability object it would have yielded eagerly. The sealed hashes cover the snapshot identity (read-scope hash, projection watermarks, policy/registry versions) plus the per-column capability hashes actually consumed — which the verdicts pin.
3. Freeze the assembled context in memory before LLM dispatch. The LLM capability inventory, deterministic binder, and validators must all read the same immutable object. The LLM inventory is derived from Layer A (controlled vocabulary summaries), so it never waits on Layer-B compilation.
4. Seal, before the considered revision and response are written, the inputs that semantic eligibility consumed:
   - column operational facts;
   - semantic-context content hashes;
   - dataset-profile hashes or explicit absent-profile hashes;
   - relationship/realization revisions;
   - source/temporal policy revisions when consumed;
   - read-scope hash and projection watermarks;
   - policy/registry/config versions.
5. Extend the snapshot item-kind registry using the existing additive mechanism. Do not re-hash legacy `column_field` items.
6. Return an immutable in-memory `GenerationSemanticContextV1`; every downstream selector and validator consumes it rather than re-querying live `graph_node` for meaning.
7. Keep a narrow freshness revalidation at Gate-1 draft/confirm. Freshness may invalidate the plan; it may not silently recompute a different one.
8. Add a concurrency test: mutate metadata after the context is frozen and prove the current repeatable-read run keeps its original verdict while the next run gets a new hash.
9. Add read-scope tests proving hidden columns and hidden relationship endpoints never enter the context or omission details.
10. **Budget gates that precede merge, not enforcement.** History says this is where generation performance dies (the per-template column-scan defect was 157 redundant scans before the load-once fix). SE-2 does not merge until these are measured green on the kind cluster with the live CIB + FTR catalogs:
    - query count for Layer A is O(fact families), independent of column count — asserted by a query-count test;
    - query count for Layer B is O(shortlisted columns), asserted the same way;
    - added wall-clock for context assembly at current catalog scale: ≤ 2 s per generation run, measured, with the measurement recorded in the task's acceptance row;
    - LLM inventory bytes bounded and measured — the inventory must be smaller than the current per-column prose context it replaces, not larger.
11. **Preserve the C0 snapshot's fail-closed property.** Today a projection-lagged view ABORTS the considered set (route 503, nothing written — `gate1.py`'s snapshot is built before the considered-set INSERT precisely so lag cannot produce a half-true record). The new pre-generation seal must keep exactly that behavior: a context that cannot prove its watermarks refuses the run; it never degrades to an unsealed run.

**Acceptance:** The considered revision can prove the exact semantic and operational state used to create eligible shortlists. The snapshot is no longer merely a post-generation record of surviving refs. The scope rule is the concept closure over Layer A — named, tested, and not a per-run judgment call — and the measured budgets above are in the acceptance record.

### Task SE-3: Compile columns and tables into typed capabilities

**Create:**

- `src/featuregen/overlay/upload/column_capabilities.py`
- `tests/featuregen/overlay/upload/test_column_capabilities.py`

**Modify:**

- `src/featuregen/overlay/upload/semantic_context.py`
- `src/featuregen/overlay/upload/profile_vocab.py` only if a missing closed value is genuinely required
- `src/featuregen/overlay/upload/operational_facts.py`

**Steps:**

1. Implement `ColumnCapabilityV1` and a batch compiler over `GenerationSemanticContextV1`. The compiler serves SE-2's Layer B: full capabilities are compiled only for shortlist members reached through the concept-closure rule, inside the same repeatable-read snapshot — never eagerly for the whole catalog.
2. Separate capabilities into:
   - structural: type, key shape, table membership, visibility;
   - semantic: concept, identifier, event type, party/economic role;
   - operational: additivity, unit, currency, grain/as-of;
   - dataset: source grain, role, authority, temporal model, event/snapshot;
   - relational: join role, realization, cardinality;
   - policy: sensitivity and permitted use.
3. For every capability, attach the exact authority/evidence pins and a load-bearing boolean derived from the versioned policy.
4. Derive “identifier-like” and “non-numeric” only from controlled structural/semantic fields, never from a name regex used as authority.
5. Treat prose, paths, summaries, and business terms as retrieval signals only.
6. Mark conflicts, missing context, and projection lag explicitly.
7. Ensure table profile absence is a fact, not an inferred `snapshot` or `event` default.
8. Batch all store reads. Add a query-count test that fails on per-column N+1 behavior, and hold SE-3 to SE-2's measured budget gates (Layer-B queries O(shortlisted columns)).
9. **Create the governed concept→operand-class map — it does not exist today.** §6.4's "possible operand classes" is load-bearing (the identifier-is-never-a-measure rule rides on it) and the second-pass verification found no such mapping in `concepts.py` or `banking_policies.py`. Author it as a closed, reviewed table beside the concept registry (one owner module, content-versioned like `taxonomy/versions.py` pins), keyed by controlled concept with per-concept allowed operand classes; the compiler consumes it and refuses a concept absent from the map rather than guessing from type or name. Registry completeness is a test: every concept referenced by any V2 operand must have a row.

**Acceptance:** Every shortlisted column has one deterministic capability object, and a reviewer can trace each load-bearing capability to a source fact, decision, profile, or relationship revision. A column outside every shortlist has Layer-A facts only — and that is a documented property, not an accident.

### Task SE-4: Implement the versioned semantic-eligibility policy

**Create:**

- `src/featuregen/overlay/upload/semantic_eligibility.py`
- `src/featuregen/overlay/upload/semantic_eligibility_reasons.py`
- `tests/featuregen/overlay/upload/test_semantic_eligibility.py`
- `tests/featuregen/overlay/upload/test_semantic_authority_matrix.py`

**Modify:**

- `src/featuregen/overlay/upload/feature_assist.py` — the refusal-family owner is its REJECT-code vocabulary (there is no `explain.py`; the second-pass verification confirmed the file does not exist — do not create one, extend the existing closed vocabulary)
- `src/featuregen/overlay/upload/validation_requirements.py` for new external checks, not semantic refusals

**Steps:**

1. Encode the authority matrix from section 7 as a pure, content-hashed policy.
2. Evaluate cheap hard exclusions first: read scope, target leakage, protected use, impossible type, wrong operand class, and exact contradictions.
3. Evaluate controlled meaning: concept, event subtype, entity/party/economic role, and identifier namespace.
4. Evaluate dataset shape: allowed source grain, event/snapshot, temporal storage model, and authority role.
5. Evaluate operational compatibility: additivity, unit, currency, sign/direction, and policy inputs.
6. Evaluate relationship requirements and directional cardinality.
7. Return all applicable reason codes; do not stop at the first failure in the audit verdict. A separate `primary_reason_code` may use a fixed precedence for UI grouping.
8. Distinguish:
   - `blocked`: known incompatible or prohibited;
   - `provisional`: plausible but below the suggestion authority floor;
   - `eligible`: clears the suggestion floor;
   - `not_applicable`: no semantic match.
9. Never promote a provisional operand by combining several weak prose signals.
10. Add banking confusion fixtures covering identifiers-as-measures, snapshot dates as events, consent/KYC/transaction timestamps, balance versus flow, limit versus exposure, authorization versus settlement status, product package versus holding, and current snapshot versus lifecycle history.

**Acceptance:** The eligibility engine rejects every known bad binding class without consulting an LLM and retains a named path to resolution for incomplete-but-plausible bindings.

### Task SE-4b: Authority bootstrap — the confirmation funnel that makes eligibility reachable

**Purpose:** Close the measured provisional flood (§1: 560/560 live concept facts are `llm/proposed`; all 1,195 operand suggestion floors are `declared`; therefore zero bindings can be `eligible` today). Without this task the eligibility engine is technically correct and empty. The standing product constraint applies: per-field confirmation does not scale — attestation is confidence-gated and reviewed in bulk, by exception.

**Create:**

- `src/featuregen/overlay/upload/concept_confirmation_queue.py` — the bulk by-exception read model
- a bulk-confirmation route pair beside the existing field-decision routes (`governance:confirm`, optimistic concurrency per evidence hash, one decision event per column — bulk is a UI affordance, never a single blanket fact)
- a bulk-confirmation surface on the existing governance UI (grouped rows, not a new screen family)
- `tests` for the queue fold, the routes, and the per-column event fan-out

**Steps:**

1. Build the queue grouped by **concept, not column**: "these 41 columns carry proposed `customer_id` — confirm the batch, untick the exceptions." One screenful per concept, ordered by how load-bearing the concept is (how many V2 operands reference it).
2. Scope the default queue to the **shortlist-bearing columns only** — the binder needs the ~dozens of load-bearing columns per catalog confirmed, not 150K fields. The full backlog stays reachable behind a filter, honestly counted.
3. Each confirmation lands as an individual attributable field decision through the EXISTING decision machinery (same store, same events, same revision semantics as the asset-detail field-decision flow) — this task adds throughput, never a new authority kind.
4. Wire the existing `attest/` harness (P0, shipped) as the queue's ordering signal where its confidence outputs exist; record that this program is the consumer that justifies collecting the P2 human gold labels, which remain their own gated program.
5. Emit the funnel metric SE-0 gates on: declared-or-confirmed share of concept facts, overall and per catalog, before/after.

**Acceptance:** An SME can move one catalog's load-bearing columns from proposed to confirmed in one sitting, each as an attributable per-column decision; the SE-0 authority-distribution measurement visibly moves; and no mechanism introduced here can confirm a value without a named human and a pinned evidence hash.

> **ACCEPTED 2026-08-11 — backend `8c8d0ecf`, frontend `9c60d0cf`.** `concept_confirmation_queue.py` (batched 3-query read model; concept groups load-bearing-first by registry-derived operand references; per-column CAS anchors; AUTHORITY-based settledness — a concept confirm cascades re-resolution, so decision-event types cannot be the detector) + `GET/POST /governance/concept-confirmations` (per-item `apply_field_correction`: four-eyes, CAS, audit, one attributable decision per column; a stale anchor 409s its column alone; funnel metric in every response) + the Governance-screen panel (batch confirm/reject with untick-the-exceptions, per-item declines named beside landed siblings). Step 4 (attest/ ordering signal) DEFERRED until attest confidence outputs exist for concepts — the ordering falls back to reference counts, which is the load-bearing signal anyway. Suites: backend 10775, frontend 782.

### Task SE-5: Generalize the existing binder around the shared verdict

**Modify:**

- `src/featuregen/overlay/upload/recipe_operand_policy.py`
- `src/featuregen/overlay/upload/templates.py` only for compatibility adapters
- `src/featuregen/overlay/upload/tie_break.py`
- `src/featuregen/overlay/upload/recipe_grounding_context.py`
- related tests

**Steps:**

1. Change the binder input from `(V2 recipe, live columns)` to `(FeaturePlanningRequestV1, frozen capabilities)`.
2. Keep `bind_v2_operands` as a compatibility wrapper over the shared binder until all callers move.
3. Preserve its good properties:
   - exact controlled concept matching;
   - no automatic resolution of a required tie;
   - shared adjudicated tie-break store;
   - economic-role enforcement;
   - distinct-binding enforcement;
   - machine reason codes and resolution instructions.
4. Add enforcement that V2 already declares but the current binder does not fully consume:
   - `allowed_source_grains`;
   - `operand_class`;
   - suggestion/execution authority floors;
   - `join_role` and `temporal_role`;
   - unit/currency expectations;
   - status and relationship policies.
5. Rank only inside the eligible or provisional set. A high lexical/concept score must never outrank a hard incompatibility.
6. Reuse the existing candidate and assignment bounds — maximum 16 candidates per role and 4,096 combinations, with an explicit truncation outcome — but **move the constants into the shared binder**: today they live in `templates.py` (`MAX_GROUNDING_CANDIDATES_PER_NEED`, `MAX_GROUNDING_ASSIGNMENTS`), which BR-17 froze READ-ONLY; the new binder must not import from a frozen module.
7. Produce one persisted verdict tuple used by recipe formulas, LLM intents, Suggested Features, and Gate-1.
8. **Stage the enforcement in two steps — the flood defense.** Step one (ships with Tranche 1): enforce everything that needs NO confirmed metadata — `operand_class`, `allowed_source_grains`, unit/currency expectations, business-event subtype, identifier-not-a-measure. These kill the worst bindings immediately and cannot flood anything. Step two (gated): authority-floor enforcement runs in SHADOW — verdicts computed and persisted, floors not user-visible — until SE-0's authority-distribution gate shows SE-4b's funnel has cleared the flood for the target catalog. Flipping floors live is a per-catalog promotion through SE-14's gate fold, never a code default.
9. **`governed_model_output` requests bind narrowly.** The model-feature spec (BR-7A registry + readiness fold) is the computation carrier; the binder binds only the recipe's DECLARED context operands, produces no formula plan, and must never invent column bindings for the model's internal features. Candidates route the user to the model-spec surface for everything execution-shaped.

**Acceptance:** There is one role binder in production. The formula path and display path cannot disagree about which column fills an operand. Shape/meaning enforcement is live everywhere while authority floors demonstrably wait for the funnel — verified by a test that a proposed-only catalog still yields shape-checked provisional candidates, not an empty screen and not a floor-cleared lie.

> **SHAPE HALF ACCEPTED 2026-08-11 — commit `3f12a46e` (Tranche 1's step-8 first stage).** `shape_refusal` + `_shape_filter` in `bind_v2_operands`: IDENTIFIER_NOT_A_MEASURE (identifier concepts never serve measures, any type) and TYPE_INCOMPATIBLE (declared type outside the class family), filtered BEFORE tie logic so an impossible twin cannot manufacture a tie; all-refused → BLOCKED with refs+codes (contradiction ≠ absence); unknown types never refuse. Authority floors deliberately absent (SE-4b gate). Suite 10768; every existing consumer unchanged on clean catalogs. REMAINING for full SE-5: capability-input binder (removes the per-recipe column load + moves the bounds constants), the remaining declared-constraint enforcement (`allowed_source_grains` needs dataset-grain facts — SE-8's planners; unit/currency expectations beyond structural type; join/temporal roles), floors-in-shadow at Tranche 2.

### Task SE-6: Replace physical-column LLM generation with abstract Feature Intent generation

**Create:**

- `src/featuregen/overlay/upload/feature_intent_generation.py`
- feature-intent prompt/schema tests

**Modify:**

- `src/featuregen/overlay/upload/enrich_llm.py`
- `src/featuregen/overlay/upload/feature_assist.py`
- `src/featuregen/overlay/upload/contract/gate1.py`

**Steps:**

1. Register a closed structured-output schema for `feature_intents`.
2. Build a bounded semantic capability inventory containing controlled concepts, entities, source-grain classes, event types, available operation classes, and honest missing/authority summaries. Do not include object refs.
3. Name the egress seam precisely — "the existing egress sanitizer" is two different mechanisms and the second-pass verification found no module by that name: the user's hypothesis passes through `contract/intake.py`'s `redact_free_text` (keep), while uploaded column/table prose reaches the model via the feature-context V5 assembly in `feature_assist.py`. The new capability inventory replaces the latter, so it needs its own test proving no uploader-controlled prose enters the inventory unsanitized. Prefer controlled inventory tokens over repeated column prose to reduce cost and contamination.
4. Ask the model for atomic outputs and typed required operands. Explicitly forbid physical names, SQL, and invented policy references. Allow an honest conceptual pattern when the available closed formula grammar cannot express the idea.
5. Validate every output item independently. A malformed intent does not fail its siblings.
6. Restrict objectives to the human-confirmed scope. A model-proposed objective outside scope is rejected before binding. A governed-model output must reference a model-feature spec that the server offered; otherwise reject it as ungrounded.
7. Convert the existing critic to review business fit, causal logic, leakage proximity, and redundancy at the intent level. It must not suggest physical columns.
8. Refine feedback by producing a new Feature Intent revision, then rerun deterministic binding. Never patch a physical binding in place from a human's prose.
9. Retain the current physical `feature_ideas` path behind the single rollout mode for rollback only.
10. Do not increase the default provider-call count: abstract generation replaces the current generation call; binding is deterministic.
11. **State the formula-readiness ceiling explicitly so nobody relaxes the contract to "fix" it:** `FormulaReferenceV2` requires a reviewed `expectation_ref`, and a fresh LLM intent has none — so an LLM-origin deterministic candidate is born formula-BLOCKED and stays there until an expectation is authored and reviewed through the v2 expectation seam (`recipe_formula_expectations_v2.py`). This is the designed behavior, not a gap: the path from a good LLM intent to an executable feature runs through human formula authoring + review, exactly like a recipe's.

**Acceptance:** No new enforced LLM candidate contains a model-selected physical ref. Every surviving LLM idea has typed roles before the catalog is searched.

### Task SE-7: Make V2 recipes the direct hypothesis-workflow recipe source

**Modify:**

- `src/featuregen/overlay/upload/contract/gate1.py`
- `src/featuregen/overlay/upload/taxonomy/applicability.py`
- `src/featuregen/overlay/upload/taxonomy/recipe_applicability.py`
- `src/featuregen/overlay/upload/recipe_registry_v2.py`
- `src/featuregen/overlay/upload/recipe_variants.py`
- `src/featuregen/overlay/upload/recipe_temporal_v2.py`
- `src/featuregen/overlay/upload/recipe_readiness.py`
- tests for scoped V2 grounding

**Steps:**

1. Compute applicability directly from `RecipeDefinitionV2.primary_objective` and `supporting_objectives`; remove legacy tag inference from the new pipeline.
2. Resolve recipe parameters through the existing bounded variant mechanism before identity and binding.
3. Convert each applicable recipe into `FeaturePlanningRequestV1`.
4. Run the shared semantic binder and persist per-operand verdicts — in Tranche 1, "persist" means the existing `input_role_bindings` projection inside today's considered revision; the dedicated verdict schema is SE-10's (migrations 1062–1063) and this step upgrades to it when SE-10 lands.
5. Compile temporal semantics with `recipe_temporal_v2.compile_temporal`.
6. Fold formula, model-output, and materialization readiness using existing readiness modules.
7. Preserve legacy Template generation only for old suggestion contract V1/V2 and historical replay during the compatibility window.
8. Do not resolve a multi-output legacy alias to one atom implicitly. The hypothesis path starts with atomic V2 IDs and therefore needs no legacy alias.

**Acceptance:** A new Gate-1 recipe candidate is derived directly from one atomic V2 recipe and carries its exact output, operands, temporal contract, formula/readiness, review state, and content hash.

> **SE-6 WIRED `333eaa3c` (2026-08-13):** `llm_intent_candidates` — the audited intent call → neutral requests → the SHARED binder over the same frozen context → the same candidate carrier, assembled WITH recipes under semantic_v1 (signature merges twins into corroboration; fail-soft — the recipe lens never depends on the model; deterministic intents stay conceptual_pattern). Per-item fix: RecipeContractError now rejects the ITEM (INTENT_REJECTED_PARSE), never the batch. Remaining SE-6: intent-level critic conversion, refine-as-new-revision.

> **IN PROGRESS — parts 1+2 landed 2026-08-11.** Part 1 `34115733`: `recipe_planning_lens.py` — `v2_applicability` over authored objectives (no tags/crosswalk/aliases, exactly-one invariant kept) + `v2_recipe_candidates` assembling planning request + hash, SHARED `bind_v2_operands` verdicts, fail-closed binding fold, compiled temporal or named blocker, authored readiness, BR-23 review validity; `FEATUREGEN_SEMANTIC_PLANNING` closed-string mode (frozen `legacy` default). Part 2 `85c66f86`: `semantic_shadow` wiring in `build_considered_set` — route-resolved mode + scope, savepoint-isolated, divergence logged, response-invariant (tested three ways). Known + recorded: per-recipe column load until SE-5's capability binder. Part 3 `1eec207c` (2026-08-12): **semantic_v1 SERVES** — `semantic_projection.py` + the gate1 branch: under the mode (route-resolved; frozen `legacy` default untouched — SE-0's ALL_TEMPLATES pin unchanged) the recipe lens comes from the semantic engine (context → binder → fold → assembly → typed gauntlet) and legacy template grounding does not run (stub-explodes in test). The projection TRANSLATES onto the existing carriers: gauntlet requirements → exact legacy Requirement equivalents (IDENTIFIER_UNIQUENESS→GRAIN_IS_UNIQUE, EVENT_HISTORY_VERIFICATION→TEMPORAL_IS_POPULATED, minted via build_requirement; semantic code in detail), floor codes → RoleBinding.confirmation_required with the MEASURED producer/strength authority, refused/blocked/temporal-uncompiled → NAMED rejections. Observations persist in the request transaction on the serving path. Suite 10848. REMAINING for full acceptance + cutover (SE-14): dispositions still evaluate the legacy applicability universe under semantic_v1 (v2_applicability feeds the route at cutover); RecipeGroundingContextV1 confirm-time contexts are empty on the semantic path (drafting consumes them — verify degrade or project them before flipping the default); SE-0's gate1 pin updates in the cutover commit. Part 4 `0a6321da`: dispositions + in_scope_count fold the V2 UNIVERSE under semantic_v1 (`v2_applicability_as_result` — the V2 classification in the legacy ApplicabilityResult carrier); legacy machinery keeps the legacy object; the grounding-context residual VERIFIED a designed degrade (formula-shadow writes CAPTURE_INPUT_INCOMPLETE/PRIVATE_CONTEXT_MISSING; no confirm-time consumer).

### Task SE-8: Integrate source, temporal, join, and cross-catalog planning

**Modify:**

- `src/featuregen/overlay/upload/planner/plan.py`
- `src/featuregen/overlay/upload/planner/contracts.py`
- `src/featuregen/overlay/upload/planner/candidates.py`
- `src/featuregen/overlay/upload/planner/enumerate.py`
- `src/featuregen/overlay/upload/planner/declarations.py`
- `src/featuregen/overlay/upload/source_selector.py`
- `src/featuregen/overlay/upload/source_selection.py`
- `src/featuregen/overlay/upload/temporal_policy.py`
- `src/featuregen/overlay/upload/planner/plan_envelope.py`
- planner tests

**Steps:**

1. Generalize planner entry points from legacy `Template`/`Need` to `FeaturePlanningRequestV1`; use adapters for historical tests.
2. Resolve dataset needs as a feature-level decision, not one independent column at a time.
3. Require an explicit population dataset. Never infer it from a table name or primary entity.
4. Use `source_selector` when dataset profiles and source/temporal selection are enabled.
5. In an explicit single-catalog request with profile features disabled, permit only decisions that do not claim an authoritative source comparison. Stamp the limitation.
6. For event-required operands, reject current-only/snapshot sources unless the recipe explicitly asks for snapshot/as-of behavior.
7. Use exact relationship realizations, bridge facts, read scope, and directional cardinality. Preserve the selected path in the plan envelope.
8. Fail closed on ambiguous source, missing binding, insufficient authority, historical-current-only, SCD overlap, snapshot tie, unverified cardinality, or denied hop.
9. Compile aggregation, temporal, physical-read-set, and output policy over the selected plan using the existing compiler context.
10. Ensure the same planner can serve single-catalog and governed cross-catalog plans. Remove the rule that only cross-catalog ideas receive a real plan envelope.

**Acceptance:** Every user-visible candidate has an exact dataset and path story. No LLM or recipe candidate reaches Gate-1 with only a bag of columns and an inferred grain table.

> **IN PROGRESS — parts 1+2 landed 2026-08-12.** Part 1 `dd1db717` (step 1): `planner/requests.py` — `planning_probe` translates any `FeaturePlanningRequestV1` into the planner's native shape (synthetic Template, id-not-prose intent, enum-safe join/temporal role mapping) + `plan_planning_request` → the unchanged `plan_bindings`; LLM intents and V2 recipes enter the SAME planner, origin-blind. Part 2 `ce7e453c` (step 6): the dataset axis — Layer A context v3 carries `table_facts` (one added batched read, pin 2→3 queries), `ColumnCapabilityV1` gains `table_event_or_snapshot` + its own evidence authority (same single Layer-B query), and the fold blocks `event_timestamp` operands over declared-or-better `snapshot` tables with `SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW`. Proposed/display-only classifications block nothing and clear nothing (the typed gauntlet's `EVENT_HISTORY_VERIFICATION` owns the runtime half); as-of recipes never trip it (they ask for `as_of_timestamp` — the "unless explicitly snapshot behavior" clause is structural). The banking battery's deferred TABLE half is claimed; end-to-end compile test pins graph_hint vs source/attested. Suite 10832. Steps 2–3 landed `adb24704`: DatasetStoryV1 + fold_dataset_story — explicit population from the DECLARED grain the entity key bound (never table-name/entity inference), POPULATION_DATASET_UNDECLARED (new closed-vocabulary code, needs_setup) when no declared anchor exists, cross-dataset candidates carry candidate-level RELATIONSHIP_REQUIRED with tables named; every V2 candidate carries its story and the typed gauntlet folds the codes into candidate-level requirements (shadow/serving/v4 inherit free). Named carrier limitation: legacy Requirement cannot express 'declare the population' — removed at SE-14. REMAINING: profile-gated source selection + limitation stamps (steps 4–5 — honestly deferred: dataset profiles are ABSENT on every live catalog, nothing to compare yet), exact-path envelope preservation and remaining fail-closed causes (steps 7–10 — largely delivered today by the existing planner via `plan_planning_request`, but the cross-catalog "real plan envelope for all origins" rule (step 10) is unverified until SE-7's enforced projection exercises it).

### Task SE-9: Refactor the deterministic gauntlet to consume typed roles

**Modify:**

- `src/featuregen/overlay/upload/feature_assist.py`
- `src/featuregen/overlay/upload/validation_requirements.py`
- `src/featuregen/overlay/upload/grounding_trace.py`
- `src/featuregen/overlay/upload/pii_policy.py` and the personal-data checks inside `feature_assist.py` (the D14 data-use-policy gate) — there is no `pii_use_gate.py`; the second-pass verification confirmed the file does not exist — extend the real owners, do not create a parallel module
- related tests

**Steps:**

1. Extract a pure post-binding validator from `_validate_idea` that receives a frozen binding plan.
2. Apply numeric, additivity, unit, and currency checks only to typed measures and formula operands.
3. Apply uniqueness to entity/grain keys, population checks to population sources, temporal population/lag checks to time roles, and connectivity/cardinality checks to relationship roles.
4. Keep target leakage, protected characteristics, personal-data policy, freshness, and read-scope denial as hard safety gates.
5. Replace aggregation-word inference with the closed Feature Intent/Formula V2 operation wherever the new pipeline is active.
6. Preserve legacy validation behavior behind the rollback mode; do not silently reinterpret historical candidates.
7. Add new versioned external requirement schemas for source-grain verification, event-history verification, identifier uniqueness, relationship cardinality, status-policy coverage, and temporal-storage checks where runtime observation is genuinely required.
8. Record evaluated rule hashes, semantic-policy hash, operand verdict hashes, source/temporal decision hashes, and relationship realization hashes in the grounding trace.
9. Compute `validation_status` from actual unmet requirements. Stop emitting a generic `verification=DESIGN-CHECKED` badge that contradicts `NEEDS_EXTERNAL_VALIDATION`.

**Acceptance:** A customer ID is not checked as a numeric measure, an as-of key is not checked as currency-bearing, and every design status is reproducible from typed roles and pinned rules.

### Task SE-10: Assemble, deduplicate, rank, and persist the considered set

**Create:**

- `src/featuregen/overlay/upload/candidate_assembly.py`
- **migrations 1062–1063, reserved here** for semantic-eligibility audit observations and considered-revision extension (1060–1061 are BR-23's; this codebase has already had one migration double-allocation incident — reservations are written down, never implied by "next available")

**Modify:**

- `src/featuregen/overlay/upload/contract/gate1.py`
- `src/featuregen/overlay/upload/feature_metadata_snapshot.py`
- `src/featuregen/overlay/upload/recipe_grounding_context.py`
- `src/featuregen/api/feature_serialize.py`
- considered-revision migrations/readers/tests

**Steps:**

1. Define a semantic computation signature over output meaning, operation, grain, temporal contract, parameter values, and role bindings.
2. Merge equivalent recipe and LLM realizations. Prefer the V2 recipe as primary when it has the same semantics because it carries SME-authored policies and readiness. Record the LLM origin as corroborating provenance rather than showing a duplicate card. "Carries reviewed SME semantics" is now a store lookup, not an assumption: consult the BR-23 review store (`review_validity` at the recipe's canonical revision) and record the fold's answer — a recipe with CURRENT approval outranks an unreviewed twin for primacy, and an unreviewed recipe is still preferred over a bare LLM intent for its authored policies, honestly labeled unreviewed.
3. Do not merge candidates merely because names or physical refs match.
4. Rank only candidates whose binding state permits display. Keep blocked/provisional candidates in a separate actionable section when they convey meaningful missing prerequisites. **The ordering itself is designed here, because today nothing owns it** (deterministic ranking is OFF by default behind `FEATUREGEN_INTENT_RANKING`, and LLM ranking was never built): order by a deterministic composite key — semantic binding state, then review validity (a CURRENT-approved recipe outranks an unreviewed twin), then readiness, then applicability strength, with the stable content-addressed ID as the final tiebreak. No score is displayed without its basis; if the deployment opts out of ranking, the order is the authored registry order and the UI says so.
5. Extend the private considered revision with:
   - planning request body/hash;
   - semantic capability and context hashes;
   - every selected operand verdict and bounded losing shortlist;
   - source/row/join plan envelope;
   - formula/temporal compilation result;
   - readiness and design-validation axes;
   - resolution actions;
   - the recipe's review-validity fold result AT generation time — `current`, approved/missing roles, and the canonical revision hash it was folded at (BR-23's store is append-only, so this pin is the honest answer to "was this reviewed when the human selected it", robust to later edits and later approvals);
   - policy revisions.
6. Reuse the existing `RoleBinding`/`input_role_bindings` carrier as the compatibility projection of selected operand verdicts. Do not introduce a third independent role-binding structure on `FeatureIdea`; the private plan remains authoritative and the public role rows project from it.
7. Keep the public card payload bounded. Full audit detail should be retrievable by option ID from the same immutable revision, subject to the same actor/read scope.
8. Ensure option identity includes the semantic plan. A change in role, authority, source, relationship, temporal policy, or formula must mint another option.
9. On feedback, mint a fresh generation run and superseding revision exactly as the current governed flow does.

**Acceptance:** The exact candidate the human saw is the candidate that drafting governs. Equivalent recipe/LLM ideas do not clutter the screen, and semantic drift cannot preserve an old option ID.

> **IN PROGRESS — slices 1+2 landed.** Slice 1 `ef4bb4ec` (2026-08-11, migration 1062 USED): `semantic_candidate_store.py` — append-only `semantic_candidate_observation` rows (guard triggers incl. TRUNCATE) persisting verdicts + eligibility + policy hashes per shadow run; fleet metrics query them. Slice 2 `e471eb06` (2026-08-12, steps 1–4): `candidate_assembly.py` — `semantic_signature` (content hash of output meaning + operation + objective + grain + temporal contract + resolved parameter values + BOUND role assignments; origin/name/ref-blind — the merge key IS the meaning, so step 3 holds by construction), `assemble_candidates` (merge with primacy: authored recipe over LLM twin, CURRENT review over unreviewed — the candidate's own BR-23 fold result; losing twins ride as `CorroborationV1` provenance, honestly labeled), and the designed order: deterministic composite key (binding_state → review_validity → readiness → applicability_strength → signature tiebreak), `order_basis` STATED on the result, blocked/ambiguous/missing split into an actionable section with their named resolutions (never "ranked low"). Wired into the semantic shadow (assembly runs + logs ranked/actionable/merged-twins on real catalogs before anything serves it). REMAINING: step 5 (considered-revision extension — migration 1063, when semantic_v1 serves), steps 6–9 (compatibility projection onto input_role_bindings, bounded card payload + detail-by-option-id, option identity minting, feedback re-mint) — these land with SE-7's enforced projection.

### Task SE-11: Version the API and make backend capabilities explicit

**Modify:**

- `src/featuregen/api/routes/contract.py`
- `src/featuregen/api/feature_serialize.py`
- `src/featuregen/api/routes/assist.py`
- OpenAPI response models and route tests

**Steps:**

1. Add `POST /contract/considered-set?contract_version=2` and return top-level `contract_version: 2`. An omitted version preserves the current response during rollout. Do not make an old client infer the version from optional fields.
2. Keep the current response readable during rollout. The new client opts into the semantic candidate contract.
3. Add public, bounded candidate fields:
   - source origin and definition revision;
   - output/grain/window/operation;
   - semantic binding state;
   - role bindings with authority summaries;
   - design status and requirements;
   - formula/materialization readiness;
   - source/join/PIT summary;
   - blocker groups and actions;
   - provenance hashes suitable for an audit drawer.
4. Add an actor-scoped `GET /contract/considered-revisions/{revision_id}/options/{option_id}` detail endpoint for full eligibility and plan evidence rather than bloating every list response. Reuse the stored revision's actor/read-scope checks; do not query a wider live catalog to decorate it.
5. Make direct `/features/recommend`, `/features/refine`, and `/features/recipe` use the same pipeline or mark them compatibility-only. No public endpoint may remain a bypass around confirmed scope, typed intent, or semantic eligibility in enforced mode.
6. Return a typed 409 when an option's semantic snapshot or plan is stale and requires regeneration.
7. Expose pipeline version and rollout mode in generation-run provenance and a diagnostic endpoint.

**Acceptance:** Every screen can render the truth without reverse-engineering backend defaults, and no newer semantic field silently leaks into a frozen old contract.

> **IN PROGRESS — step 1 landed `d00e54ee` (2026-08-12).** `contract_version: Literal[1,2] = 1` on `ConsideredSetIn` (the client asks per request — never an env flag, never inferred): v2 responses carry top-level `contract_version` + `semantic_planning_mode` (the step-7 diagnostic); v1 byte-identical with a no-leak pin (new keys ABSENT); the emergency unscoped path refuses v2 with a typed 422 before the confirmation gate. The per-card semantic fields (generation_source, recipe_id, role bindings w/ measured authority + confirmation_required, validation_status, registry-typed requirements) already ride the v2 card serializer. Steps 3+4 landed `580a9e60`: v2 responses carry `considered_revision_id`+`considered_content_hash` (v2-only), and `GET /contract/considered-revisions/{id}/options/{option_id}` (read-scoped) serves the option's canonical identity from the hash-verified STORED revision (`verified_considered_revision_by_id` — corrupt revision = typed 409) + this run's semantic evidence for recipe-sourced options (observation-store join, newest row, honest absence). Steps 5+6 landed `342d1d76` — **SE-11 COMPLETE**: the four direct /features/* generation routes are compatibility-only (marked `compatibility_only: true`; typed 409 SEMANTIC_ENFORCED_USE_CONTRACT_PIPELINE under semantic_v1, pre-dispatch — leakage-check stays, it is a check not generation); the draft route re-verifies the option's sealed snapshot at choice time — drift → typed 409 SEMANTIC_SNAPSHOT_STALE with the comparator's reason (unverifiable = logged, not refused; compat snapshots must not brick drafting). Test drives real drift through a real sealed context pin.

### Task SE-12: Redesign the Workbench candidate experience around evidence and action

**Modify:**

- `frontend/src/api.ts`
- `frontend/src/screens/WorkbenchScreen.tsx`
- `frontend/src/screens/WorkbenchScreen.test.tsx`
- `frontend/src/index.css` or the current component styles

**Current UI gaps to correct:**

- The considered-set backend already emits `validation_status` and `requirements`, but `FeatureIdea` in `frontend/src/api.ts` does not model them and Workbench does not render them.
- Direct assist serialization can emit the LLM grounding explanation, but Gate-1's `_idea_json` projection currently omits it; therefore Workbench cannot receive or render it.
- The considered-set contract has no typed operand-role projection; it exposes only the untyped `derives_pairs` list.
- Workbench renders the older `verification` badge and states that all generated candidates are design-checked, even when the backend candidate is `NEEDS_EXTERNAL_VALIDATION`.
- Physical `derives_from` is shown as one untyped string list, so users cannot tell measures from keys, filters, and time anchors.
- Formula/materialization readiness is not presented on the hypothesis candidate card.
- Model grounding, when present, is not shown and is not clearly distinguished from platform evidence.

**Target card hierarchy:**

1. Header: feature name, origin, semantic-binding state, design status, execution readiness.
2. “What it computes”: business definition, entity grain, operation, window/cutoff, output unit/additivity.
3. “Inputs and why”: one row per typed role showing business label, physical ref, source dataset, authority, and eligibility outcome.
4. “Can it be built?”: source/join/PIT summary and the highest-priority unresolved action.
5. “Why this was proposed”: SME recipe definition or model rationale, explicitly labelled.
6. Progressive “Audit detail”: full reason codes, evidence IDs/hashes, losing ties, policies, and snapshot lineage.

**Interaction rules:**

1. `eligible + DESIGN_CHECKED` may be selected.
2. A semantic `provisional` or ambiguous binding is not Gate-1 selectable. Show it under “Could be useful if…” with a direct resolution action. After the metadata or relationship decision is recorded, regenerate under a fresh run and snapshot. This is stricter than an external data-quality requirement because the system does not yet know which physical meaning it would govern.
3. A `conceptual_pattern` is not selectable into a feature contract because it has no exact computation. Show it under “Ideas requiring design” with a formula/model-authoring action. A semantically bound deterministic candidate may still be selected while formula or materialization readiness is blocked; the resulting contract must retain that readiness state and cannot execute.
4. `blocked` appears in “Could be useful if…” only when it has a concrete remedy; structurally impossible candidates stay in rejections.
5. Requirements have task-oriented wording and owners: “Confirm customer identifier,” “Profile uniqueness,” “Govern join,” “Declare currency conversion,” or “Add event history.”
6. Keep validation and readiness chips visually separate.
7. Label LLM grounding “Model's rationale,” never “Evidence.”
8. Preserve keyboard access, heading structure, focus on regenerate/refine, and non-color status encoding.
9. Update empty states to distinguish no applicable ideas, no eligible data, blocked metadata, and provider failure.
10. Link semantic-confirmation actions to the existing asset field-decision experience focused on the relevant field, then return to a fresh generation run; do not let a candidate card directly self-approve its own metadata.

> **IN PROGRESS — slice 1 landed `9b487aa4` (2026-08-12).** The audit's first three gaps closed: `api.ts` models `validation_status` + typed `requirements` + `input_role_bindings` (exact wire shapes); the card's honest stamp — NEEDS_EXTERNAL_VALIDATION never wears "design-checked", wears amber "needs data checks (N)"; "Inputs and why" rows per typed role with measured authority + "needs confirmation" only where the floor is outstanding; checks render task-first (rule 5 vocabulary) with backend prose as fine print. Frontend 786 / backend 10860 green. REMAINING: card hierarchy sections 2/4/6 (computes/buildability/audit-drawer via the SE-11 detail endpoint), interaction rules 2–4 (selectability gating by binding state — needs semantic_v1 serving to populate states), rule 10 (field-decision deep link + fresh run), empty-state split (rule 9).

**Acceptance:** A user can explain why `cust_num` was used only as an entity key and what must be done before the feature is executable. No card calls a candidate design-checked when it carries unmet validation requirements.

### Task SE-13: Reuse the engine in deterministic Suggested Features

**Modify:**

- `src/featuregen/overlay/upload/suggestion_contract.py`
- `src/featuregen/api/routes/suggestions.py`
- `src/featuregen/overlay/upload/suggestions.py`
- `frontend/src/api.ts`
- `frontend/src/screens/SuggestedFeaturesScreen.tsx`
- `frontend/src/screens/SuggestionCard.tsx`
- associated tests

**Steps:**

1. Keep the workflow distinction: Suggested Features has no hypothesis, no target, and no LLM.
2. Retrieve V2 recipes applicable to the opened table/column neighborhood.
3. Normalize them into `FeaturePlanningRequestV1` and run the same capability compiler, semantic eligibility, binder, temporal compiler, planner, and validator.
4. Treat the clicked column as a highlight and retrieval anchor, never as a forced measure.
5. Introduce the new contract as **suggestion contract v4, by the same mechanics v3 used** — never by mutating frozen V1/V2 or BR-17 V3 semantics: extend `SUPPORTED_CONTRACT_VERSIONS` to `(1, 2, 3, 4)`; register the version in the contract-version registry (one owner); gate it behind a new BR-24-family boolean `FEATUREGEN_SUGGESTION_CONTRACT_V4` (default OFF, pinned by the frozen-configuration test); flag-off requests for `?contract_version=4` answer the same typed 422 shape v3 answers when off.
6. Populate currently empty `semantic_context_hashes` and `dataset_profile_hashes` from decisions actually consumed.
7. Emit exact V2 atomic recipe identity rather than using a legacy template as the computation carrier.
8. Show the same role-based inputs, authority, blockers, source/join/PIT summary, design status, and execution readiness as Workbench.
9. Preserve the bounded one-hop automatic neighborhood and disclose what was not searched.
10. For `cust_num`, show only recipes where it can serve an identifier/key role. Transaction features appear only if a governed path to transaction events exists.

**Acceptance:** The deterministic page and hypothesis Workbench cannot disagree about whether the same recipe/column binding is semantically valid. They may differ only in applicability/ranking because one has a hypothesis and one does not.

> **IN PROGRESS — slice 1 (backend contract) landed `e3ffd819` (2026-08-12).** Contract v4 = byte-frozen v3 + ONE addition: the `semantic` block from `semantic_parity_block` — the SAME lens/engine/context the Workbench serves from, run UNSCOPED (no hypothesis by design) and anchored to the opened table (bound-operand membership; retrieval anchor, never a forced measure). Explicit `?contract_version=4` per SE-11's pattern — step 5's `FEATUREGEN_SUGGESTION_CONTRACT_V4` boolean predates the no-flags steer and was deliberately NOT introduced (closed SUPPORTED set = the only gate; typed 422 preserved). **The acceptance is pinned by test**: page vs direct lens call agree on every binding_state (one engine ⟹ cannot disagree). `semantic_context_hash` populated (step 6 half). Slice 2 (frontend) landed `5f79f774`: the screen asks for v4 with ONE graceful step-down to v2 (both-refused = unsupported, copy updated); "What the planning engine says" section — bindable recipes in the designed order + review chips + corroboration lines, undecided under "Could be useful if…" with named resolutions (a to-do, never a low rank); asking for v4 also lights the v3 execution truth SuggestionCard always supported but nothing requested. Frontend 789 green. REMAINING: step 6's dataset_profile_hashes (needs SE-8 steps 4–5), step 7's full V2-identity carrier swap on the legacy hits, richer per-hit role/authority rows (step 8's Workbench parity on the CARDS themselves).

### Task SE-14: Add observability, evaluation, rollout, and rollback

**Create/modify:**

- semantic-planning shadow store, **migration 1064 (reserved here)** if the SE-10 artifacts prove insufficient for fleet metrics
- `tests/eval/` gold and mutation fixtures
- `deploy/kind/k8s/20-backend.yaml`
- deployment-bound tests
- runtime counters/structured logs

**PRE-LIVE SIMPLIFICATION (user steer, 2026-08-11): the tool is under development with no
production users — avoid unnecessary flags.** This amends the rollout design below and SE-13:

- `FEATUREGEN_SEMANTIC_PLANNING` remains the program's ONE control, and it is TEMPORARY: the
  shadow mode exists to validate the new engine against the legacy path on real runs; once the
  gold suites and shadow comparison hold, cut over to the semantic pipeline and DELETE the
  legacy generation path and the mode itself — never keep dual pipelines behind a switch
  indefinitely.
- SE-13 ships suggestion v4 by extending `SUPPORTED_CONTRACT_VERSIONS` and serving it —
  NO dedicated `FEATUREGEN_SUGGESTION_CONTRACT_V4` flag (that instruction is retracted).
- SE-11's considered-set v2 likewise needs no flag: the explicit `contract_version` query
  parameter is the whole opt-in.
- The staged canary percentages (actor/tenant, 10/50/100%) in the deployment sequence are
  production theater for a pre-live tool — collapsed to: validate in shadow on kind → flip →
  delete legacy.
- Existing BR-24 levers with no consumer (`RECIPE_CONTRACT_V2`, `FORMULA_V2`,
  `MATERIALIZATION`, both allowlists) are candidates for RETIREMENT rather than wiring, if the
  direct cutover supersedes the behavior they were reserved for.
- What this does NOT relax: gates guarding real externalities (LLM spend approval, deploy
  approval) and the activation invariant (review-validity + code-level checks at the first
  activation consumer) — those guard correctness and money, not rollout choreography.

**Rollout control — an extension of BR-24, never a parallel mechanism:**

BR-24 already shipped the platform's rollout-control system (`recipe_rollout.py`): flags with frozen stage-honest defaults pinned by the frozen-configuration test, per-family/per-catalog CSV allowlists, and a canary-gate fold whose unmeasured inputs default to the failing side. This program adds ONE member to that family rather than inventing a second mode system (the same argument Objection 3 makes against two binders applies to two rollout mechanisms):

- `FEATUREGEN_SEMANTIC_PLANNING` — a closed three-value MODE on `RecipeRolloutConfig`, default `legacy`, read through `RecipeRolloutConfig.from_env` and nowhere else. Mechanically it CANNOT be a `FLAG_DEFAULTS` member — that dict is `dict[str, bool]` parsed by `_truthy` — so it joins the config the way the CSV allowlists did: its own typed field with its own closed parser (an unknown value falls back to `legacy`, never raises at import), its own default pinned by the frozen-configuration test, same family and discipline, different type:
  - `legacy`: current generation and response;
  - `semantic_shadow`: current user response plus deterministic semantic-plan comparison;
  - `semantic_v1`: new typed-intent and shared-binding response.
- The resolved mode is captured once on `feature_generation_run`; lower layers receive the resolved config object and must not read environment variables independently — the rule `recipe_rollout.py` already enforces for its existing flags.
- The frozen-configuration test pins the `legacy` default, so a fresh deployment behaves exactly as today and every promotion is a reviewed decision.
- Promotion beyond shadow reuses BR-24's per-family/per-catalog allowlists where the candidate's recipes are family-scoped, and its release gates are expressed as a gate fold (`SemanticPlanningGateInputsV1`, same shape as `CanaryGateInputsV1`): every reading defaults to the failing side, an unmeasured gate blocks, and a promotion needs a verdict whose failure list is empty — never an aggregate score.

**Shadow metrics:**

- planning requests by origin and use case;
- eligible/provisional/blocked operands by class and reason;
- ambiguous required operands;
- source and join refusals;
- legacy accepted but semantic blocked, with reason;
- semantic accepted but legacy rejected;
- identifier-as-measure preventions;
- snapshot-as-event preventions;
- recipe/LLM semantic duplicates merged;
- metadata authority distribution used to clear suggestions;
- candidates missing semantic-context or dataset-profile hashes;
- query count, latency, context bytes, provider calls, and token cost;
- stale option revalidation failures.

**Gold suites:**

1. `cust_num` CIB customer example and its known confusing dates/codes.
2. Retail customer/account/transaction activity.
3. Corporate facilities, limits, drawn exposure, guarantees, invoices, and legal groups.
4. Payment authorization, settlement, return, reversal, refund, and mandate lifecycles.
5. Credit application, origination, delinquency, default, cure, and collections stages.
6. AML/fraud event-time and post-outcome leakage cases.
7. Deposits/ALM snapshots versus flows and contractual future horizons.
8. Markets/custody position snapshots versus trade/settlement events.
9. Multi-currency and sign/direction cases.
10. Ambiguous identifiers, missing issuer scope, duplicate keys, SCD overlap, and unverified cardinality.

**Release gates:**

- zero protected or target-leaking accepted candidates;
- zero identifier-as-generic-measure accepted candidates;
- zero event-window feature sourced only from a current snapshot;
- zero clearing decisions from LLM grounding prose or `llm/proposed` metadata when the operand floor is declared/governed;

> **IN PROGRESS — part 1 landed `ab0d4ba8` (2026-08-12).** `SemanticPlanningGateInputsV1` + `semantic_planning_gate` in `recipe_rollout.py` — the release gates above as a fold (four zero-tolerance readings verbatim + gold failures + unexplained divergence + shadow-evidence presence + rollback drill), every default the failing side, verdict = the SAME `CanaryGateVerdictV1` (one gate family). `semantic_shadow_metrics` over the observation store computes the measured inputs (state/origin distributions, reason counts, identifier/snapshot PREVENTIONS, run/context counts; empty store = observation_rows_present False = blocks). REMAINING: gold suites 1–10 as `tests/eval/` fixtures (the banking battery covers the eligibility-fold half of several), shadow-run divergence explanation workflow, the cutover commit itself (flip default → delete legacy path + mode → update SE-0's ALL_TEMPLATES pin in the same commit — needs the gate green on REAL shadow runs, which needs the 1062 deploy + funnel confirmations first).
- every displayed binding has a role, authority summary, and content hash;
- every complete candidate has a source and temporal plan or an explicit `not applicable` declaration;
- every unmet condition maps to a stable reason family and user action;
- every recipe in `V2_RECIPES` adapts to the planning contract (317 at rebase; asserted against `len(V2_RECIPES)`);
- all expected-buildable gold cases produce at least one correct realization;
- all expected-unbuildable gold cases refuse for the reviewed reason;
- no N+1 query regression;
- provider calls per generated lens do not exceed the existing default path;
- V1/V2/V3 compatibility snapshots remain unchanged;
- Workbench accessibility and interaction tests pass.

**Deployment sequence:**

1. Land contracts and pure evaluators with mode `legacy`.
2. Enable deterministic recipe semantic shadow in Kind; no additional LLM call.
3. Review `cust_num` and the banking confusion corpus with an SME/data owner.
4. Run sampled abstract-intent shadow and compare quality/cost against the current physical-column call.
5. Enable `semantic_v1` for internal users in Kind.
6. Enable the versioned Workbench UI.
7. Enable the new Suggested Features contract separately.
8. Canary by actor/tenant, then 10%, 50%, and 100%, with rollback to `legacy` preserving stored semantic artifacts for diagnosis.
9. Only after profile/source-policy gates pass, enable authoritative production source selection and materialization readiness claims.

**Acceptance:** Rollback is one mode change plus pod restart; it does not require a database rollback and does not invalidate new append-only audit records.

## 10. File-level implementation map

| Concern | Reuse | Primary changes |
|---|---|---|
| Intake, target, scope | Existing sealed flow | `api/routes/contract.py`, no semantic redesign |
| Abstract LLM ideas | Existing audited structured-call seam | new `feature_intent_generation.py`, `enrich_llm.py`, reduce physical work in `feature_assist.py` |
| Recipe meaning | `RecipeDefinitionV2`, V2 registry and variants | V2-to-planning adapter, Gate-1 source cutover |
| Rich metadata | `SemanticContextBundleV1` and `for_feature_generation` | new frozen generation context and capability compiler |
| Authority | evidence axes, operational facts, field decisions | one semantic authority matrix |
| Operand binding | `bind_v2_operands`, tie-break store | generic shared binder over capabilities |
| Source choice | `DatasetNeedV1`, `source_selector` | planning-request adapter and gated live use |
| Time choice | temporal policy and `recipe_temporal_v2` | shared temporal plan and role checks |
| Joins/cross-catalog | existing planner, realizations, bridges | generic planning request instead of Template |
| Formula/readiness | Formula V2, output authority, readiness fold | bind results into existing compilers |
| Safety validation | `_validate_idea`, use gate, requirements | typed post-binding gauntlet |
| Replay | metadata snapshot, considered revision, plan envelope | snapshot before selection; persist semantic verdicts |
| Hypothesis API/UI | considered-set and Workbench | explicit new candidate contract and evidence-first card |
| No-hypothesis API/UI | suggestion V3 and shared card | new version directly carrying V2 semantic plan |
| Quality | existing eval and banking recipe fixtures | semantic mutation corpus and shadow comparison |

## 11. Dependency order

```text
SE-0 baseline/BR-17 reconciliation
  ├── SE-4b authority bootstrap (parallel track; its funnel metric gates
  │     SE-5 floor enforcement and every Tranche-3 promotion)
  └── SE-1 neutral contracts
        ├── SE-2 frozen generation context
        │     └── SE-3 capability compiler
        │           └── SE-4 semantic eligibility
        │                 └── SE-5 shared binder
        ├── SE-6 abstract LLM intents ───────────────┐
        └── SE-7 direct V2 recipe source ────────────┤
                                                     ├── SE-8 source/join/temporal planner
                                                     │     └── SE-9 typed gauntlet
                                                     │           └── SE-10 assembly/persistence
                                                     │                 ├── SE-11 API
                                                     │                 │     └── SE-12 Workbench
                                                     │                 └── SE-13 Suggested Features
                                                     └───────────────────── SE-14 rollout/eval
```

SE-6 and SE-7 may be developed independently after the common contracts exist. Neither should ship in enforced mode until SE-4, SE-5, SE-8, SE-9, and SE-10 are complete.

**Delivery tranches.** The graph above is the dependency truth; delivery is sequenced so user value does not wait ten tasks (the review finding: as originally ordered, nothing user-visible lands before SE-10..12). Each tranche ends deployable:

- **Tranche 1 — the recipe half ships (SE-0, SE-1, SE-7, plus SE-5's SHAPE half).** Atomic V2 recipes become the direct Gate-1 recipe source, and the binder starts enforcing what needs no confirmed metadata — `operand_class`, `allowed_source_grains`, join/temporal roles, unit expectations, event subtype (SE-5 step 8's first stage; authority floors deliberately NOT here — on the measured catalog they would flood everything provisional on day one). Two boundaries stated so the tranche is implementable as written: verdicts travel through the EXISTING `input_role_bindings` carrier in-memory and into the considered revision as it stands today — durable verdict schema (migrations 1062–1063) stays in SE-10; and no frozen context, no LLM change, no new API version. This alone retires the two worst verified defects (the hypothesis path grounding legacy templates; declared constraints enforced nowhere).
- **Tranche 1b — the funnel opens in parallel (SE-4b).** The authority-bootstrap queue needs nothing from SE-2..5 and its output gates everything after: start it with Tranche 1, run it continuously. The live enrichment run (SE-0 step 7, operator-approved) precedes it — the funnel needs proposals to confirm.
- **Tranche 2 — the semantic engine proves itself in shadow (SE-2, SE-3, SE-4, SE-6 in `semantic_shadow`, plus SE-5's authority floors in shadow).** The frozen context, capability compiler, eligibility policy, and abstract-intent generation run beside the live path, producing the SE-14 shadow metrics (legacy-accepted-but-semantic-blocked and its inverse) and the `cust_num` corpus review — with zero change to what users see.
- **Tranche 3 — enforcement (SE-8, SE-9, SE-10, SE-11, SE-12, SE-13 under `semantic_v1`).** The shared planner, typed gauntlet, persisted decisions, versioned API, and the two surfaces — promoted per SE-14's gate fold, family by family where applicable, never on an aggregate score. **THE ACTIVATION INVARIANT (hard acceptance clause on every Tranche-3 task that serves, authors, or materializes from the V2 registry):** the FIRST activation consumer checks, IN CODE, that the recipe's BR-23 review validity is CURRENT at its canonical revision AND its family is allowlisted (`family_active`) — with a test proving an unapproved or un-allowlisted recipe is refused at that surface. Today "no production activation without current approval" is enforced only by runbook discipline because no activation surface exists; the moment one does, this clause is what keeps the rule from being vacuously true. Displaying a recipe with honest readiness (v3/v4 read surfaces) is NOT activation and never requires approval — activation means serving a computation, authoring its formula into the governed store, or materializing it.

SE-14's instrumentation is not a tranche: its shadow store, counters, and gold suites land WITH tranche 2 (they are how tranche 2 proves anything) and its promotion machinery lands with tranche 3.

## 12. Adversarial review of this proposal

### Objection 1: “If LLM-proposed concepts cannot clear a binding, most real catalogs will produce nothing.”

That risk is not hypothetical — it is measured: 560 of 560 live concept facts are `llm/proposed`, all 1,195 operand suggestion floors are `declared`, so on today's catalog NOTHING can clear a floor. The design answers with three mechanisms, not hope: **provisional** candidates with exact actions (never pretended design-checked or executable); the SE-4b bulk by-exception funnel that moves the authority distribution at usable throughput (per-field confirmation of a large catalog is a stated non-starter); and SE-5's staged enforcement, which keeps floors in shadow until the funnel demonstrably clears the flood per catalog. The product outcome is a shorter queue of high-value confirmations, not an empty screen or a false green screen.

### Objection 2: “Removing physical columns from the LLM will make ideas generic or impossible to build.”

The LLM still receives a bounded semantic capability inventory: available concepts, entities, event/source-grain classes, and supported operations. It loses only the authority to choose a physical ref. Gold evaluation must compare relevance and grounded acceptance before rollout.

### Objection 3: “Recipes already know their operands; a shared engine is unnecessary.”

Recipes declare what they need, but the reviewed binder does not yet consume every V2 constraint, and the hypothesis path still uses legacy templates. The shared engine makes V2 declarations operational and gives LLM intents the same safety rules. Keeping two binders would guarantee drift.

### Objection 4: “The existing gauntlet already catches wrong types and missing as-of columns.”

It catches structural errors after a model-selected binding. It does not know that consent modification is not customer activity, that a customer snapshot is not a transaction log, or that a generic monetary stock is not necessarily drawn credit exposure. Those are the exact gaps this policy closes.

### Objection 5: “Source-attested prose should be strong enough to bind.”

Source-attested prose is valuable but not a controlled role declaration. Parsing it in the clearing path would create an unversioned second classifier. The correct fix is a governed mapping from the source term to a controlled concept, after which the mapping itself is evidence.

### Objection 6: “Persisting the whole candidate universe is too expensive.”

Persist hashes and decision inputs, not duplicated prose blobs. Batch-load once, use bounded role shortlists, and keep losing-candidate detail in an append-only audit artifact. Benchmark and query-count gates precede enforcement. Reproducibility cannot be achieved by snapshotting only winners after the decision.

### Objection 7: “Showing blocked candidates will overwhelm users.”

Only actionable, hypothesis-relevant blocked candidates should appear, collapsed under “Could be useful if…”. Structurally impossible attempts remain in rejection detail. Default cards show one highest-priority action; the full reason set is progressive disclosure.

### Objection 8: “The V2 recipe cutover just landed, so this plan duplicates it.”

It does not. The BR program — complete, merged, and deployed at the rebase baseline — makes suggestion contract V3 report V2 execution truth, freezes legacy authoring, populates the registry to 317 reviewed-governable recipes, and ships the review store and rollout controls. This plan consumes all of that, then completes the separate missing steps: direct V2 hypothesis applicability, direct V2 binding, common physical planning, and shared semantic eligibility — the steps that make the BR investment reach the workflow users actually run.

### Objection 9: “A second API version will slow delivery.”

The current contracts have deliberate byte-stability guarantees. Adding unversioned load-bearing fields would make old and new clients disagree silently. An explicit version allows a safe canary and rollback; it is less work than debugging mixed semantics in one response.

### Objection 10: “The semantic snapshot is already implemented.”

The current snapshot is valuable but is built after candidate generation from refs found in the considered set. It does not freeze the capability universe before the LLM/binder decision. SE-2 extends rather than replaces it and preserves legacy item hashes.

### Objection 11: “Direct `/features/recommend` can remain a simpler power-user path.”

It may remain only as a compatibility path. In enforced mode it cannot bypass the same typed intent, semantic eligibility, and safety rules. A simpler UI is acceptable; a weaker authority path is not.

### Objection 12: “Why not ask another LLM to judge semantic eligibility?”

That would turn one probabilistic binding into two probabilistic votes and still provide no stable authority. LLMs may propose or explain. Eligibility must be deterministic over versioned metadata, governed decisions, observations, and policies.

## 13. Definition of done

The program is complete only when all of the following are true:

1. The hypothesis workflow generates typed abstract intents before physical binding.
2. Gate-1 recipes come directly from atomic V2 definitions.
3. Recipes and LLM ideas use one planning request, capability compiler, semantic policy, binder, and physical planner.
4. All physical operand roles and authority decisions are persisted before the human selects a candidate.
5. The `cust_num` example behaves as described in section 8 and the known bad date/code bindings are impossible.
6. Workbench accurately distinguishes provisional, externally validated, design-checked, formula-ready, and materialization-ready states.
7. Suggested Features reuses the same engine under a new version and populates real semantic/profile hashes.
8. Old suggestion contracts, Template hashes, and considered revisions remain verifiable.
9. The release gates in SE-14 pass on banking gold, mutation, API, UI, performance, and accessibility suites.
10. The deployment has a tested one-switch rollback to the legacy pipeline.
11. The authority-bootstrap funnel (SE-4b) has demonstrably moved the measured concept-authority distribution for at least one canary catalog, and authority-floor enforcement went live there through the gate fold — the provisional flood was cleared by confirmations, never by weakening a floor.

Until these conditions hold, “rich metadata is sent to the LLM” should not be treated as proof that rich metadata is improving feature quality.

**Named successors (explicitly out of this program's scope, so absence is a decision, not an oversight):**

- **Predictive evidence.** Nothing here backtests or monitors predictive value; `DESIGN_CHECKED` must never be read as "proven useful." The backtest/monitor loop over governed, materialized features is the natural next program once SE-14's gates hold.
- **Analysis-workspace convergence.** The analysis workspace plans over the same catalog with its own path today; it should eventually consume the same capability compiler and eligibility engine. Deliberately untouched here to keep this program bounded.
